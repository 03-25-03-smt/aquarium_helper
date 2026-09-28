import os
from datetime import time, datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)


# =========================
# USERS
# =========================

BROTHER_ID = 5546793025
OWNER_ID = 674005288


# =========================
# TIMEZONE
# =========================

TZ = ZoneInfo("Europe/Prague")


# =========================
# TASKS
# =========================

TASKS = {
    "feed": "🐟 Покормить рыбок",
    "air_on": "💨 Включить воздух",
    "light_on": "💡 Включить свет",
    "light_off": "💡 Выключить свет",
    "air_off": "💨 Выключить воздух",
    "water": "💧 Подмена воды",
    "filter": "🧽 Почистить губку фильтра",
}


# =========================
# COMPLETED TASKS
# =========================

completed = {}


def today_key():
    return datetime.now(TZ).date().isoformat()


def mark_completed(task_id):
    date = today_key()

    if date not in completed:
        completed[date] = set()

    completed[date].add(task_id)


def is_completed(task_id):
    date = today_key()

    return (
        date in completed
        and task_id in completed[date]
    )


# =========================
# SEND TASK
# =========================

async def send_task(
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str
):
    task_name = TASKS[task_id]

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Выполнено",
                callback_data=f"done:{task_id}"
            )
        ]
    ]

    message = (
        f"🐠 **Уход за аквариумом**\n\n"
        f"{task_name}\n\n"
        f"Когда выполнишь — нажми кнопку ниже."
    )

    await context.bot.send_message(
        chat_id=BROTHER_ID,
        text=message,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================
# BUTTON HANDLER
# =========================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query

    await query.answer()

    data = query.data

    if not data.startswith("done:"):
        return

    task_id = data.split(":", 1)[1]

    mark_completed(task_id)

    task_name = TASKS[task_id]

    await query.edit_message_text(
        f"✅ **Выполнено**\n\n"
        f"{task_name}\n\n"
        f"Время: {datetime.now(TZ).strftime('%H:%M')}",
        parse_mode="Markdown",
    )


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "🐟 Привет!\n\n"
        "Я слежу за уходом за аквариумом.\n\n"
        "Каждый день я буду напоминать тебе "
        "о необходимых задачах."
    )


# =========================
# ID
# =========================

async def get_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n\n"
        f"`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


# =========================
# DAILY REPORT
# =========================

async def daily_report(
    context: ContextTypes.DEFAULT_TYPE
):
    date = today_key()

    done = completed.get(date, set())

    lines = [
        f"📊 **Отчёт по аквариуму**",
        f"📅 {datetime.now(TZ).strftime('%d.%m.%Y')}",
        "",
    ]

    for task_id, task_name in TASKS.items():

        # Tasks that may not happen every day
        if task_id == "water":
            weekday = datetime.now(TZ).weekday()

            if weekday != 6:
                continue

        if task_id == "filter":
            weekday = datetime.now(TZ).weekday()

            if weekday not in (2, 6):
                continue

        if task_id in done:
            lines.append(f"✅ {task_name}")
        else:
            lines.append(f"❌ {task_name}")

    lines.append("")

    await context.bot.send_message(
        chat_id=OWNER_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


# =========================
# SCHEDULE
# =========================

def setup_schedule(app: Application):

    job_queue = app.job_queue

    # 07:00
    job_queue.run_daily(
        lambda context: send_task(context, "feed"),
        time=time(7, 0, tzinfo=TZ),
        name="feeding",
    )

    job_queue.run_daily(
        lambda context: send_task(context, "air_on"),
        time=time(7, 0, tzinfo=TZ),
        name="air_on",
    )

    # 14:30
    job_queue.run_daily(
        lambda context: send_task(context, "light_on"),
        time=time(14, 30, tzinfo=TZ),
        name="light_on",
    )

    # 21:00
    job_queue.run_daily(
        lambda context: send_task(context, "light_off"),
        time=time(21, 0, tzinfo=TZ),
        name="light_off",
    )

    # 22:00
    job_queue.run_daily(
        lambda context: send_task(context, "air_off"),
        time=time(22, 0, tzinfo=TZ),
        name="air_off",
    )

    # Sunday 12:00
    job_queue.run_daily(
        lambda context: send_task(context, "water"),
        time=time(12, 0, tzinfo=TZ),
        days=(6,),
        name="water",
    )

    # Wednesday + Sunday 12:00
    job_queue.run_daily(
        lambda context: send_task(context, "filter"),
        time=time(12, 0, tzinfo=TZ),
        days=(2, 6),
        name="filter",
    )

    # Daily report 22:30
    job_queue.run_daily(
        daily_report,
        time=time(22, 30, tzinfo=TZ),
        name="daily_report",
    )


# =========================
# MAIN
# =========================

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_task(context, "feed")


def main():

    app = Application.builder().token(
        os.environ["BOT_TOKEN"]
    ).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("id", get_id)
    )

    app.add_handler(
        CommandHandler("test", test)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    setup_schedule(app)

    print("🐟 Aquarium Helper started")

    app.run_polling()


if __name__ == "__main__":
    main()
