from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import timezone
from io import BytesIO
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.audio_pipeline import build_audio_plan
from bot.config import settings
from bot.db import Database, SessionRecord
from bot.document_pipeline import extract_document_text
from bot.keyboards import MAIN_REPLY_KEYBOARD, model_select_keyboard, recent_dialog_actions, saved_dialog_actions
from bot.openrouter_client import OpenRouterClient
from bot.prompting import SELECTABLE_MODELS, build_badge, build_system_prompt, model_for_intent, route_name
from bot.router_logic import RouteDecision, detect_intent


logger = logging.getLogger(__name__)

_EDIT_INTERVAL = 1.5   # seconds between progressive edits during streaming
_MAX_MSG_LEN = 4096    # Telegram message length limit


def _truncate(text: str) -> str:
    if len(text) <= _MAX_MSG_LEN:
        return text
    return text[: _MAX_MSG_LEN - 20] + "\n\n…[обрезано]"


async def _keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    """Send typing action repeatedly until stop is set (max every 4 s)."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=4.0)
        except asyncio.TimeoutError:
            pass


def build_router(db: Database, llm: OpenRouterClient) -> Router:
    router = Router()
    max_audio_bytes = settings.audio_max_file_size_mb * 1024 * 1024
    max_document_bytes = settings.document_max_file_size_mb * 1024 * 1024

    # ------------------------------------------------------------------ helpers

    async def ensure_user(message: Message) -> int | None:
        if message.from_user is None:
            raise RuntimeError("Message has no user context")
        user = message.from_user
        await db.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )

        # Parse whitelist (empty = open access for backwards compatibility)
        allowed: set[str] = {
            u.strip().lower().lstrip("@")
            for u in settings.allowed_usernames.split(",")
            if u.strip()
        }
        if not allowed:
            return user.id  # no whitelist configured — open to everyone

        # Already approved by a previous visit — fast path
        if await db.is_user_approved(user.id):
            return user.id

        # First visit: check username against whitelist
        username = (user.username or "").lower()
        if username and username in allowed:
            await db.approve_user(user.id)
            return user.id

        # Access denied
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return None

    async def ensure_active_session(user_id: int) -> SessionRecord:
        return await db.ensure_active_session(user_id)

    async def build_text_context(session_id: int, system_prompt: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        history = await db.get_messages(session_id, limit=settings.max_context_messages)
        for msg in history:
            if msg.role not in ("user", "assistant"):
                continue
            messages.append({"role": msg.role, "content": msg.content_text})
        return messages

    def render_badge(decision: RouteDecision, model: str, model_override: str | None = None) -> str:
        return build_badge(decision.intent, model=model, use_web_search=decision.use_web_search, model_override=model_override)

    async def save_assistant_reply(
        *,
        session_id: int,
        user_text: str,
        assistant_text: str,
        user_content_type: str = "text",
    ) -> None:
        await db.add_message(session_id, role="user", content_type=user_content_type, content_text=user_text)
        await db.ensure_session_title(session_id, fallback_text=user_text)
        await db.add_message(session_id, role="assistant", content_type="text", content_text=assistant_text)

    async def trim_user_lists(user_id: int) -> None:
        await db.trim_recent_sessions(user_id, settings.recent_sessions_limit)
        await db.trim_saved_sessions(user_id, settings.saved_sessions_limit)

    async def send_tracked(target: Message, user_id: int, text: str, **kwargs: Any) -> Message:
        """Send a message and save its ID for later cleanup."""
        sent = await target.answer(text, **kwargs)
        await db.save_bot_message(user_id, sent.message_id)
        return sent

    async def run_text_pipeline(
        *,
        message: Message,
        user_id: int,
        session: SessionRecord,
        user_text: str,
        user_content_type: str = "text",
        transcription_prefix: str | None = None,
    ) -> None:
        assert message.bot is not None
        decision = detect_intent(user_text, has_photo=False, has_audio=False)
        model = session.model_override if session.model_override else model_for_intent(decision.intent)
        context_messages = await build_text_context(session.id, build_system_prompt(decision.intent))
        context_messages.append({"role": "user", "content": user_text})

        badge_line = render_badge(decision, model, model_override=session.model_override) if not session.badge_sent else ""

        # Show typing while waiting for LLM
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            _keep_typing(message.bot, message.chat.id, stop_typing)
        )

        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                llm_result = await llm.chat(
                    model=model,
                    route=route_name(decision.intent),
                    messages=context_messages,
                    enable_web_search=decision.use_web_search,
                )
        except TimeoutError:
            logger.warning("LLM request timed out after %ss", settings.request_timeout_seconds)
            await message.answer(
                "Запрос занял слишком много времени. Попробуйте ещё раз.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return
        finally:
            stop_typing.set()
            await typing_task

        llm_text = llm_result.text

        # Badge: send as separate small italic message (not mixed into response)
        if badge_line:
            await message.answer(f"<i>{badge_line}</i>", parse_mode="HTML")

        # Build final display text (transcription prefix only, no badge)
        display_prefix = (transcription_prefix + "\n\n") if transcription_prefix else ""
        answer_text = _truncate(display_prefix + llm_text)

        # Use Markdown for web search results so citation links are clickable
        parse_mode = "Markdown" if llm_result.used_web_tool else None
        await message.answer(answer_text, reply_markup=MAIN_REPLY_KEYBOARD, parse_mode=parse_mode)

        # Save to DB: assistant text includes transcription prefix, not badge
        assistant_saved = f"{transcription_prefix}\n\n{llm_text}" if transcription_prefix else llm_text
        await save_assistant_reply(
            session_id=session.id,
            user_text=user_text,
            assistant_text=assistant_saved,
            user_content_type=user_content_type,
        )
        await trim_user_lists(user_id)
        if badge_line:
            await db.mark_badge_sent(session.id)

    # ---------------------------------------------------------------- list views

    async def show_recent_dialogs(message: Message, user_id: int) -> None:
        sessions = await db.list_recent_sessions(user_id, settings.recent_sessions_limit)
        if not sessions:
            await message.answer("Пока нет диалогов.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        await send_tracked(message, user_id, "🕘 <b>Последние диалоги</b>", parse_mode="HTML", reply_markup=MAIN_REPLY_KEYBOARD)
        for session in sessions:
            ts = session.updated_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")
            text = f"<b>{session.title}</b>\n<i>#{session.id} · {ts}</i>"
            await send_tracked(
                message,
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=recent_dialog_actions(session.id, saved=session.is_saved),
            )

    async def show_saved_dialogs(message: Message, user_id: int) -> None:
        sessions = await db.list_saved_sessions(user_id, settings.saved_sessions_limit)
        if not sessions:
            await message.answer("Сохраненных диалогов пока нет.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        await send_tracked(message, user_id, "⭐ <b>Сохранённые диалоги</b>", parse_mode="HTML", reply_markup=MAIN_REPLY_KEYBOARD)
        for session in sessions:
            ts = session.updated_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")
            text = f"<b>{session.title}</b>\n<i>#{session.id} · {ts}</i>"
            await send_tracked(
                message,
                user_id,
                text,
                parse_mode="HTML",
                reply_markup=saved_dialog_actions(session.id),
            )

    # ---------------------------------------------------------- command handlers

    @router.message(Command("start"))
    async def start_cmd(message: Message) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        await ensure_active_session(user_id)
        await message.answer(
            "Бот готов к работе.\n"
            "Используйте кнопки ниже: новый диалог, последние 10, сохраненные.",
            reply_markup=MAIN_REPLY_KEYBOARD,
        )

    @router.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await message.answer(
            "/new — новый диалог\n"
            "/history — последние 10 диалогов\n"
            "/saved — сохраненные диалоги\n"
            "/reset — то же, что /new",
            reply_markup=MAIN_REPLY_KEYBOARD,
        )

    @router.message(Command("new", "reset"))
    @router.message(F.text == "🆕 Новый диалог")
    async def new_dialog(message: Message) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        session = await db.create_and_activate_session(user_id)
        await trim_user_lists(user_id)
        await message.answer(
            f"✨ <b>Новый диалог</b> <i>#{session.id}</i>",
            parse_mode="HTML",
            reply_markup=MAIN_REPLY_KEYBOARD,
        )

    @router.message(Command("history"))
    @router.message(F.text == "🕘 Последние 10")
    async def history_dialogs(message: Message) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        await show_recent_dialogs(message, user_id)

    @router.message(Command("saved"))
    @router.message(F.text == "⭐ Сохраненные")
    async def saved_dialogs(message: Message) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        await show_saved_dialogs(message, user_id)

    @router.message(Command("balance"))
    @router.message(F.text == "💳 Баланс")
    async def balance_cmd(message: Message) -> None:
        try:
            data = await llm.get_balance()
            info = data.get("data", {})
            usage = info.get("usage", 0) or 0
            limit = info.get("limit")
            label = info.get("label") or "—"

            if limit:
                remaining = max(0.0, limit - usage)
                balance_line = f"Остаток: ${remaining:.4f} из ${limit:.2f}"
            else:
                balance_line = "Лимит: не установлен"

            text = (
                f"💳 OpenRouter баланс\n\n"
                f"Ключ: {label}\n"
                f"Потрачено: ${usage:.4f}\n"
                f"{balance_line}"
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to fetch OpenRouter balance")
            text = "Не удалось получить баланс. Проверьте OPENROUTER_API_KEY."

        await message.answer(text, reply_markup=MAIN_REPLY_KEYBOARD)

    @router.message(F.text == "🤖 Модель")
    async def model_cmd(message: Message) -> None:
        await message.answer(
            "Выберите модель для текущего диалога:",
            reply_markup=model_select_keyboard(),
        )

    @router.callback_query(F.data.startswith("setmodel:"))
    async def set_model_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        user_id = callback.from_user.id
        raw = callback.data.split(":", 1)[1]
        model_id: str | None = None if raw == "auto" else raw

        session = await db.ensure_active_session(user_id)
        await db.set_model_override(session.id, model_id)

        if model_id is None:
            label = "🤖 Авто (smart routing)"
        else:
            label = next((name for name, mid in SELECTABLE_MODELS if mid == model_id), model_id)

        await callback.answer("Модель сохранена.")
        if callback.message:
            await callback.message.edit_text(
                f"<i>🔒 Модель: {label}</i>",
                parse_mode="HTML",
            )

    # -------------------------------------------------------- callback handlers

    @router.callback_query(F.data.startswith("open:"))
    async def open_session_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None or callback.message is None:
            return
        user_id = callback.from_user.id
        try:
            session_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный идентификатор.", show_alert=True)
            return
        session = await db.get_session(user_id, session_id)
        if session is None:
            await callback.answer("Диалог не найден.", show_alert=True)
            return

        await db.set_active_session(user_id, session_id)
        await callback.answer()

        assert callback.message.bot is not None
        bot = callback.message.bot
        chat_id = callback.message.chat.id

        # --- удаляем все отслеженные сообщения бота (список диалогов) ---
        tracked_ids = await db.pop_bot_messages(user_id)
        # также пробуем удалить само сообщение с кнопкой (на случай отсутствия в трекере)
        all_to_delete = set(tracked_ids) | {callback.message.message_id}
        for mid in all_to_delete:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:  # noqa: BLE001
                pass  # уже удалено или старше 48ч — игнорируем

        # --- загружаем историю и показываем спойлером ---
        history = await db.get_messages(session_id, limit=10)
        icon = "⭐" if session.is_saved else "🕘"

        if history:
            parts: list[str] = []
            for msg in history:
                if msg.role == "user":
                    snippet = msg.content_text[:300]
                    if len(msg.content_text) > 300:
                        snippet += "…"
                    parts.append(f"👤 {snippet}")
                elif msg.role == "assistant":
                    snippet = msg.content_text[:500]
                    if len(msg.content_text) > 500:
                        snippet += "…"
                    parts.append(f"🤖 {snippet}")

            spoiler_body = "\n\n".join(parts)
            count = len(history)
            text = (
                f"{icon} <b>{session.title}</b>\n\n"
                f"<tg-spoiler>{spoiler_body}</tg-spoiler>\n\n"
                f"<i>↑ последние {count} сообщ. · продолжайте диалог</i>"
            )
        else:
            text = f"{icon} <b>{session.title}</b>\n\n<i>История пуста · начните диалог</i>"

        await callback.message.answer(text, parse_mode="HTML", reply_markup=MAIN_REPLY_KEYBOARD)

    @router.callback_query(F.data.startswith("save:"))
    async def save_session_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        user_id = callback.from_user.id
        try:
            session_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный идентификатор.", show_alert=True)
            return
        saved = await db.set_saved(user_id, session_id, True)
        if not saved:
            await callback.answer("Не удалось сохранить.", show_alert=True)
            return
        await db.trim_saved_sessions(user_id, settings.saved_sessions_limit)
        await callback.answer("Диалог сохранен.")
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=saved_dialog_actions(session_id))

    @router.callback_query(F.data.startswith("unsave:"))
    async def unsave_session_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        user_id = callback.from_user.id
        try:
            session_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный идентификатор.", show_alert=True)
            return
        unsaved = await db.set_saved(user_id, session_id, False)
        if not unsaved:
            await callback.answer("Не удалось открепить.", show_alert=True)
            return
        await callback.answer("Диалог откреплен.")
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=recent_dialog_actions(session_id, saved=False)
            )

    @router.callback_query(F.data.startswith("delete:"))
    async def delete_session_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
            return
        user_id = callback.from_user.id
        try:
            session_id = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer("Некорректный идентификатор.", show_alert=True)
            return
        deleted = await db.delete_session(user_id, session_id)
        if not deleted:
            await callback.answer("Диалог уже удален.", show_alert=True)
            return
        await db.ensure_active_session(user_id)
        await callback.answer("Диалог удален.")
        if callback.message:
            await callback.message.edit_text("Диалог удален.")

    # --------------------------------------------------------- message handlers

    @router.message(F.voice | F.audio)
    async def voice_message(message: Message, bot: Bot) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        session = await ensure_active_session(user_id)

        audio = message.voice or message.audio
        if audio is None:
            await message.answer("Не удалось прочитать аудио.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        duration = getattr(audio, "duration", None)
        if isinstance(duration, int) and duration > 0 and duration > settings.audio_max_duration_seconds:
            await message.answer(
                f"Аудио слишком длинное для обработки.\nЛимит: {settings.audio_max_duration_seconds} сек.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return

        file_size = getattr(audio, "file_size", None)
        if isinstance(file_size, int) and file_size > max_audio_bytes:
            await message.answer(
                f"Аудио слишком большое для обработки.\nЛимит: {settings.audio_max_file_size_mb} MB.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return

        # Show typing while downloading + normalizing + transcribing
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))

        try:
            file = await bot.get_file(audio.file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, buffer)
            audio_bytes = buffer.getvalue()
            mime_type = getattr(audio, "mime_type", None)

            if len(audio_bytes) > max_audio_bytes:
                await message.answer(
                    f"Файл аудио после загрузки превышает лимит.\nЛимит: {settings.audio_max_file_size_mb} MB.",
                    reply_markup=MAIN_REPLY_KEYBOARD,
                )
                return

            try:
                audio_plan = await build_audio_plan(
                    audio_bytes,
                    file_path=file.file_path,
                    mime_type=mime_type,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Audio normalization failed")
                await message.answer(
                    "Не удалось подготовить аудио для транскрибации. Попробуйте отправить голосовое еще раз.",
                    reply_markup=MAIN_REPLY_KEYBOARD,
                )
                return

            try:
                transcript = await llm.transcribe_audio(audio_plan.primary_bytes, audio_plan.primary_format)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Audio transcription primary attempt failed (format=%s, note=%s)",
                    audio_plan.primary_format,
                    audio_plan.note,
                )
                if audio_plan.fallback_bytes and audio_plan.fallback_format:
                    try:
                        transcript = await llm.transcribe_audio(
                            audio_plan.fallback_bytes, audio_plan.fallback_format
                        )
                        logger.warning(
                            "Audio transcription succeeded with fallback (format=%s)",
                            audio_plan.fallback_format,
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("Audio transcription fallback failed")
                        await message.answer(
                            "Не удалось транскрибировать голосовое сообщение.",
                            reply_markup=MAIN_REPLY_KEYBOARD,
                        )
                        return
                else:
                    await message.answer(
                        "Не удалось транскрибировать голосовое сообщение.",
                        reply_markup=MAIN_REPLY_KEYBOARD,
                    )
                    return
        finally:
            stop_typing.set()
            await typing_task

        transcription_prefix = f"📝 Транскрипция:\n{transcript}"
        try:
            await run_text_pipeline(
                message=message,
                user_id=user_id,
                session=session,
                user_text=transcript,
                user_content_type="voice",
                transcription_prefix=transcription_prefix,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Voice text pipeline failed")
            await message.answer("Ошибка при обработке голосового сообщения.", reply_markup=MAIN_REPLY_KEYBOARD)

    @router.message(F.photo)
    async def image_message(message: Message) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        session = await ensure_active_session(user_id)

        if not message.photo:
            await message.answer("Не удалось обработать изображение.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        assert message.bot is not None
        largest_photo = message.photo[-1]
        file = await message.bot.get_file(largest_photo.file_id)
        buffer = BytesIO()
        await message.bot.download_file(file.file_path, buffer)
        image_bytes = buffer.getvalue()

        user_prompt = (message.caption or "").strip() or "Опиши изображение и выдели важные детали."
        decision = detect_intent(user_prompt, has_photo=True, has_audio=False)
        model = session.model_override if session.model_override else model_for_intent(decision.intent)

        context_messages = await build_text_context(session.id, build_system_prompt(decision.intent))
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        context_messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                ],
            }
        )

        badge_line = render_badge(decision, model, model_override=session.model_override) if not session.badge_sent else ""

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            _keep_typing(message.bot, message.chat.id, stop_typing)
        )

        try:
            async with asyncio.timeout(settings.request_timeout_seconds):
                llm_result = await llm.chat(
                    model=model,
                    route=route_name(decision.intent),
                    messages=context_messages,
                    enable_web_search=False,
                )
        except TimeoutError:
            logger.warning("Vision LLM request timed out")
            await message.answer(
                "Запрос занял слишком много времени. Попробуйте ещё раз.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("Vision request failed")
            await message.answer(
                "Не удалось обработать изображение через модель.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return
        finally:
            stop_typing.set()
            await typing_task

        llm_text = llm_result.text
        if badge_line:
            await message.answer(f"<i>{badge_line}</i>", parse_mode="HTML")
        await message.answer(_truncate(llm_text), reply_markup=MAIN_REPLY_KEYBOARD)

        await save_assistant_reply(
            session_id=session.id,
            user_text=f"[image] {user_prompt}",
            assistant_text=llm_text,
            user_content_type="image",
        )
        await trim_user_lists(user_id)
        if badge_line:
            await db.mark_badge_sent(session.id)

    @router.message(F.document)
    async def document_message(message: Message, bot: Bot) -> None:
        user_id = await ensure_user(message)
        if user_id is None:
            return
        session = await ensure_active_session(user_id)

        doc = message.document
        if doc is None:
            await message.answer("Не удалось прочитать документ.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        file_size = getattr(doc, "file_size", None)
        if isinstance(file_size, int) and file_size > max_document_bytes:
            await message.answer(
                f"Документ слишком большой.\nЛимит: {settings.document_max_file_size_mb} MB.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return

        # Show typing while downloading + extracting text
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(bot, message.chat.id, stop_typing))

        try:
            file = await bot.get_file(doc.file_id)
            buffer = BytesIO()
            await bot.download_file(file.file_path, buffer)
            doc_bytes = buffer.getvalue()

            file_name = doc.file_name or "document"
            mime_type = getattr(doc, "mime_type", None)

            try:
                extracted_text = extract_document_text(
                    doc_bytes,
                    file_name=file_name,
                    mime_type=mime_type,
                    max_chars=settings.document_max_extracted_chars,
                )
            except ValueError as exc:
                await message.answer(str(exc), reply_markup=MAIN_REPLY_KEYBOARD)
                return
            except Exception:  # noqa: BLE001
                logger.exception("Document text extraction failed")
                await message.answer("Не удалось извлечь текст из документа.", reply_markup=MAIN_REPLY_KEYBOARD)
                return
        finally:
            stop_typing.set()
            await typing_task

        user_prompt = (message.caption or "").strip() or "Кратко изложи содержание документа."
        user_text = f"{user_prompt}\n\n---\n{extracted_text}"

        try:
            await run_text_pipeline(
                message=message,
                user_id=user_id,
                session=session,
                user_text=user_text,
                user_content_type="document",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Document text pipeline failed")
            await message.answer("Ошибка при обработке документа.", reply_markup=MAIN_REPLY_KEYBOARD)

    @router.message(F.text)
    async def text_message(message: Message) -> None:
        text = (message.text or "").strip()
        if not text:
            return

        user_id = await ensure_user(message)
        if user_id is None:
            return
        session = await ensure_active_session(user_id)

        try:
            await run_text_pipeline(
                message=message,
                user_id=user_id,
                session=session,
                user_text=text,
                user_content_type="text",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Text request failed")
            await message.answer("Ошибка при обработке запроса. Попробуйте еще раз.", reply_markup=MAIN_REPLY_KEYBOARD)

    return router
