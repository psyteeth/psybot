from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot import limits


async def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rel_left = await limits.remaining(user_id, "relationships")
    teeth_left = await limits.remaining(user_id, "teeth")
    concept_left = await limits.remaining(user_id, "concept")
    rows = [
        [InlineKeyboardButton(f"Отношения (осталось {rel_left})", callback_data="menu:relationships")],
        [InlineKeyboardButton(f"Зубы (осталось {teeth_left})", callback_data="menu:teeth")],
        [InlineKeyboardButton(f"Концепция (осталось {concept_left})", callback_data="menu:concept")],
    ]
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="menu:back")]])
