from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from bot.prompting import SELECTABLE_MODELS


MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🆕 Новый диалог"), KeyboardButton(text="🕘 Последние 10")],
        [KeyboardButton(text="⭐ Сохраненные"), KeyboardButton(text="💳 Баланс")],
        [KeyboardButton(text="🤖 Модель")],
    ],
    resize_keyboard=True,
)


def model_select_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for display_name, model_id in SELECTABLE_MODELS:
        callback = f"setmodel:{model_id}" if model_id else "setmodel:auto"
        buttons.append([InlineKeyboardButton(text=display_name, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def recent_dialog_actions(session_id: int, saved: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="Открыть", callback_data=f"open:{session_id}"),
            InlineKeyboardButton(text="Удалить", callback_data=f"delete:{session_id}"),
        ]
    ]
    if saved:
        buttons.append([
            InlineKeyboardButton(text="Открепить", callback_data=f"unsave:{session_id}"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="Сохранить", callback_data=f"save:{session_id}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def saved_dialog_actions(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Открыть", callback_data=f"open:{session_id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"delete:{session_id}"),
            ],
            [
                InlineKeyboardButton(text="Открепить", callback_data=f"unsave:{session_id}"),
            ],
        ]
    )
