from __future__ import annotations

import base64
import logging
from datetime import timezone
from io import BytesIO
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.audio_pipeline import build_audio_plan
from bot.config import settings
from bot.db import Database, SessionRecord
from bot.keyboards import MAIN_REPLY_KEYBOARD, recent_dialog_actions, saved_dialog_actions
from bot.openrouter_client import OpenRouterClient
from bot.prompting import build_badge, build_system_prompt, model_for_intent, route_name
from bot.router_logic import RouteDecision, detect_intent


logger = logging.getLogger(__name__)


def build_router(db: Database, llm: OpenRouterClient) -> Router:
    router = Router()

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
        # Keep recent and saved collections within configured limits.
        await db.trim_recent_sessions(user_id, settings.recent_sessions_limit)
        await db.trim_saved_sessions(user_id, settings.saved_sessions_limit)

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
        route = route_name(decision.intent)
        context_messages = await build_text_context(session.id, build_system_prompt(decision.intent))
        context_messages.append({"role": "user", "content": user_text})

        llm_result = await llm.chat(
            model=model,
            route=route,
            messages=context_messages,
            enable_web_search=decision.use_web_search,
        )
        answer_text = llm_result.text
        if transcription_prefix:
            answer_text = f"{transcription_prefix}\n\n{answer_text}"

        await save_assistant_reply(
            session_id=session.id,
            user_text=user_text,
            assistant_text=answer_text,
            user_content_type=user_content_type,
        )
        await trim_user_lists(user_id)

        if not session.badge_sent:
            badge = render_badge(decision, model)
            answer_text = f"{badge}\n\n{answer_text}"
            await db.mark_badge_sent(session.id)

        await message.answer(answer_text, reply_markup=MAIN_REPLY_KEYBOARD)

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

    @router.message(F.voice | F.audio)
    async def voice_message(message: Message, bot: Bot) -> None:
        user_id = await ensure_user(message)
        session = await ensure_active_session(user_id)

        audio = message.voice or message.audio
        if audio is None:
            await message.answer("Не удалось прочитать аудио.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        file = await bot.get_file(audio.file_id)
        buffer = BytesIO()
        await bot.download_file(file.file_path, buffer)
        audio_bytes = buffer.getvalue()
        mime_type = getattr(audio, "mime_type", None)

        try:
            audio_plan = await build_audio_plan(
                audio_bytes,
                file_path=file.file_path,
                mime_type=mime_type,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Audio normalization failed")
            await message.answer(
                "Не удалось подготовить аудио для транскрибации. "
                "Попробуйте отправить голосовое еще раз.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return

        try:
            transcript = await llm.transcribe_audio(
                audio_plan.primary_bytes,
                audio_plan.primary_format,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Audio transcription primary attempt failed (format=%s, note=%s)",
                audio_plan.primary_format,
                audio_plan.note,
            )
            if audio_plan.fallback_bytes and audio_plan.fallback_format:
                try:
                    transcript = await llm.transcribe_audio(
                        audio_plan.fallback_bytes,
                        audio_plan.fallback_format,
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
            await message.answer(
                "Ошибка при обработке голосового сообщения.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )

    @router.message(F.photo)
    async def image_message(message: Message) -> None:
        user_id = await ensure_user(message)
        session = await ensure_active_session(user_id)

        if not message.photo:
            await message.answer("Не удалось обработать изображение.", reply_markup=MAIN_REPLY_KEYBOARD)
            return

        largest_photo = message.photo[-1]
        bot = message.bot
        file = await bot.get_file(largest_photo.file_id)
        buffer = BytesIO()
        await bot.download_file(file.file_path, buffer)
        image_bytes = buffer.getvalue()

        user_prompt = (message.caption or "").strip() or "Опиши изображение и выдели важные детали."
        decision = detect_intent(user_prompt, has_photo=True, has_audio=False)
        model = model_for_intent(decision.intent)
        route = route_name(decision.intent)

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

        try:
            llm_result = await llm.chat(
                model=model,
                route=route,
                messages=context_messages,
                enable_web_search=False,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Vision request failed")
            await message.answer(
                "Не удалось обработать изображение через модель.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )
            return

        answer_text = llm_result.text
        await save_assistant_reply(
            session_id=session.id,
            user_text=f"[image] {user_prompt}",
            assistant_text=answer_text,
            user_content_type="image",
        )
        await trim_user_lists(user_id)

        if not session.badge_sent:
            badge = render_badge(decision, model)
            answer_text = f"{badge}\n\n{answer_text}"
            await db.mark_badge_sent(session.id)

        await message.answer(answer_text, reply_markup=MAIN_REPLY_KEYBOARD)

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
            await message.answer(
                "Ошибка при обработке запроса. Попробуйте еще раз.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )

    return router
