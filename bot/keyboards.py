from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from bot import limits


def _fmt(left: float) -> str:
    return "∞" if left == float("inf") else str(int(left))


async def main_menu_keyboard(bot: Bot, user_id: int) -> InlineKeyboardMarkup:
    rel_left = await limits.remaining(bot, user_id, "relationships")
    teeth_left = await limits.remaining(bot, user_id, "teeth")
    concept_left = await limits.remaining(bot, user_id, "concept")
    rows = [
        [InlineKeyboardButton(f"Отношения (осталось {_fmt(rel_left)})", callback_data="menu:relationships")],
        [InlineKeyboardButton(f"Зубы (осталось {_fmt(teeth_left)})", callback_data="menu:teeth")],
        [InlineKeyboardButton(f"Концепция (осталось {_fmt(concept_left)})", callback_data="menu:concept")],
    ]
    return InlineKeyboardMarkup(rows)


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="menu:back")]])


def menu_and_teeth_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("В меню", callback_data="menu:back")],
            [InlineKeyboardButton("Зубы", callback_data="menu:teeth")],
        ]
    )
