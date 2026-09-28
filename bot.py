from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐟 Привет! Я бот для ухода за аквариумом!"
    )


def main():
    app = Application.builder().token("TOKEN").build()

    app.add_handler(CommandHandler("start", start))

    print("🐟 Aquarium bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
