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
from bot.keyboards import MAIN_REPLY_KEYBOARD, recent_dialog_actions, saved_dialog_actions
from bot.openrouter_client import OpenRouterClient
from bot.prompting import build_badge, build_system_prompt, model_for_intent, route_name
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

    async def ensure_user(message: Message) -> int:
        if message.from_user is None:
            raise RuntimeError("Message has no user context")
        user = message.from_user
        await db.upsert_user(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
        return user.id

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

    def render_badge(decision: RouteDecision, model: str) -> str:
        return build_badge(decision.intent, model=model, use_web_search=decision.use_web_search)

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

    async def stream_into_placeholder(
        placeholder: Message,
        *,
        model: str,
        route: str,
        messages: list[dict[str, Any]],
        enable_web_search: bool,
        display_prefix: str,
    ) -> str:
        """
        Stream LLM response into an existing placeholder message.
        Returns the full accumulated LLM text (without display_prefix).
        Raises on stream failure.
        """
        llm_text = ""
        last_edit = 0.0

        async for chunk in llm.stream_chat(
            model=model,
            route=route,
            messages=messages,
            enable_web_search=enable_web_search,
        ):
            llm_text += chunk
            now = time.monotonic()
            if now - last_edit >= _EDIT_INTERVAL:
                try:
                    await placeholder.edit_text(_truncate(display_prefix + llm_text))
                    last_edit = now
                except Exception:  # noqa: BLE001
                    pass

        # Final edit to ensure complete text is shown
        try:
            await placeholder.edit_text(_truncate(display_prefix + llm_text))
        except Exception:  # noqa: BLE001
            pass

        return llm_text

    async def run_text_pipeline(
        *,
        message: Message,
        user_id: int,
        session: SessionRecord,
        user_text: str,
        user_content_type: str = "text",
        transcription_prefix: str | None = None,
    ) -> None:
        decision = detect_intent(user_text, has_photo=False, has_audio=False)
        model = model_for_intent(decision.intent)
        context_messages = await build_text_context(session.id, build_system_prompt(decision.intent))
        context_messages.append({"role": "user", "content": user_text})

        badge_line = render_badge(decision, model) if not session.badge_sent else ""

        # Build what the user sees above the LLM answer
        prefix_parts = [p for p in [badge_line, transcription_prefix] if p]
        display_prefix = "\n\n".join(prefix_parts) + ("\n\n" if prefix_parts else "")

        # Placeholder message — also attaches the reply keyboard
        placeholder = await message.answer("⌛", reply_markup=MAIN_REPLY_KEYBOARD)

        try:
            llm_text = await stream_into_placeholder(
                placeholder,
                model=model,
                route=route_name(decision.intent),
                messages=context_messages,
                enable_web_search=decision.use_web_search,
                display_prefix=display_prefix,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Streaming LLM request failed")
            await placeholder.edit_text("Ошибка при обработке запроса. Попробуйте еще раз.")
            return

        if not llm_text:
            await placeholder.edit_text("Пустой ответ от модели.")
            return

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

        await message.answer("🕘 Последние 10 диалогов:", reply_markup=MAIN_REPLY_KEYBOARD)
        for session in sessions:
            ts = session.updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            text = f"#{session.id} · {ts}\n{session.title}"
            await message.answer(
                text,
                reply_markup=recent_dialog_actions(session.id, saved=session.is_saved),
            )

    async def show_saved_dialogs(message: Message, user_id: int) -> None:
        sessions = await db.list_saved_sessions(user_id, settings.saved_sessions_limit)
        if not sessions:
            await message.answer("Сохраненных диалогов пока нет.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        await message.answer("⭐ Сохраненные диалоги:", reply_markup=MAIN_REPLY_KEYBOARD)
        for session in sessions:
            ts = session.updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            text = f"#{session.id} · {ts}\n{session.title}"
            await message.answer(text, reply_markup=saved_dialog_actions(session.id))

    # ---------------------------------------------------------- command handlers

    @router.message(Command("start"))
    async def start_cmd(message: Message) -> None:
        user_id = await ensure_user(message)
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
        session = await db.create_and_activate_session(user_id)
        await trim_user_lists(user_id)
        await message.answer(
            f"Создан новый диалог #{session.id}.",
            reply_markup=MAIN_REPLY_KEYBOARD,
        )

    @router.message(Command("history"))
    @router.message(F.text == "🕘 Последние 10")
    async def history_dialogs(message: Message) -> None:
        user_id = await ensure_user(message)
        await show_recent_dialogs(message, user_id)

    @router.message(Command("saved"))
    @router.message(F.text == "⭐ Сохраненные")
    async def saved_dialogs(message: Message) -> None:
        user_id = await ensure_user(message)
        await show_saved_dialogs(message, user_id)

    # -------------------------------------------------------- callback handlers

    @router.callback_query(F.data.startswith("open:"))
    async def open_session_callback(callback: CallbackQuery) -> None:
        if callback.from_user is None or callback.data is None:
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
        await callback.answer("Диалог активирован.")
        if callback.message:
            await callback.message.answer(
                f"Подключен диалог #{session.id}: {session.title}",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )

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
        model = model_for_intent(decision.intent)

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

        badge_line = render_badge(decision, model) if not session.badge_sent else ""
        display_prefix = (badge_line + "\n\n") if badge_line else ""

        placeholder = await message.answer("⌛", reply_markup=MAIN_REPLY_KEYBOARD)

        try:
            llm_text = await stream_into_placeholder(
                placeholder,
                model=model,
                route=route_name(decision.intent),
                messages=context_messages,
                enable_web_search=False,
                display_prefix=display_prefix,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Vision streaming request failed")
            await placeholder.edit_text("Не удалось обработать изображение через модель.")
            return

        if not llm_text:
            await placeholder.edit_text("Пустой ответ от модели.")
            return

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
