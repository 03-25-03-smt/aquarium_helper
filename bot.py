import os

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐟 Привет! Я бот для ухода за аквариумом!\n\n"
        "Используй /id, чтобы узнать свой Telegram ID."
    )


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n\n"
        f"`{user.id}`",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(
        os.environ["BOT_TOKEN"]
    ).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", get_id))

    print("🐟 Aquarium bot started")

    app.run_polling()


if __name__ == "__main__":
    main()
