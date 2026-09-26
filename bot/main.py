import logging

from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from bot import db
from bot.config import TELEGRAM_BOT_TOKEN
from bot.handlers import concept, menu, relationships, teeth

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def build_application():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

    db.init_db()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Порядок важен: ветки должны идти до общих /start и текстового фолбэка,
    # чтобы их собственные fallback-обработчики (/cancel, /start, menu:back)
    # успевали перехватить апдейт, пока разговор активен.
    application.add_handler(relationships.conv_handler)
    application.add_handler(teeth.conv_handler)
    application.add_handler(concept.conv_handler)

    application.add_handler(CommandHandler("start", menu.start_command))
    application.add_handler(CommandHandler("limits", menu.limits_command))
    application.add_handler(CallbackQueryHandler(menu.back_to_menu_callback, pattern="^menu:back$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu.fallback_text))

    return application


def main() -> None:
    application = build_application()
    logger.info("Бот запускается (long polling)")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
