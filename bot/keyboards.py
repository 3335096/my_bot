from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🆕 Новый диалог"), KeyboardButton(text="🕘 Последние 10")],
        [KeyboardButton(text="⭐ Сохраненные")],
    ],
    resize_keyboard=True,
)


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
