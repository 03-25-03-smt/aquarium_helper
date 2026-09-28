import os
import sqlite3
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
# DATABASE
# =========================

DB_FILE = "aquarium.db"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_name TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(date, task_id)
        )
    """)

    conn.commit()
    conn.close()


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
# DATABASE HELPERS
# =========================

def today_key():
    return datetime.now(TZ).date().isoformat()


def save_task(task_id):
    now = datetime.now(TZ)
    date = now.date().isoformat()
    timestamp = now.isoformat()

    conn = get_db()

    conn.execute(
        """
        INSERT OR IGNORE INTO tasks
        (date, task_id, task_name, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            date,
            task_id,
            TASKS[task_id],
            timestamp,
        ),
    )

    conn.commit()
    conn.close()


def complete_task(task_id):
    now = datetime.now(TZ)
    date = now.date().isoformat()
    timestamp = now.isoformat()

    conn = get_db()

    conn.execute(
        """
        UPDATE tasks
        SET completed_at = ?
        WHERE date = ?
        AND task_id = ?
        AND completed_at IS NULL
        """,
        (
            timestamp,
            date,
            task_id,
        ),
    )

    conn.commit()
    conn.close()


def get_today_tasks():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE date = ?
        ORDER BY sent_at
        """,
        (today_key(),),
    ).fetchall()

    conn.close()

    return rows


# =========================
# SEND TASK
# =========================

async def send_task(
    context: ContextTypes.DEFAULT_TYPE,
    task_id: str,
):
    task_name = TASKS[task_id]

    save_task(task_id)

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Выполнено",
                callback_data=f"done:{task_id}",
            )
        ]
    ]

    message = (
        "🐠 *Уход за аквариумом*\n\n"
        f"{task_name}\n\n"
        "Когда выполнишь — нажми кнопку ниже."
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
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    data = query.data

    if not data.startswith("done:"):
        return

    task_id = data.split(":", 1)[1]

    complete_task(task_id)

    task_name = TASKS[task_id]

    await query.edit_message_text(
        "✅ *Выполнено*\n\n"
        f"{task_name}\n\n"
        f"Время: {datetime.now(TZ).strftime('%H:%M')}",
        parse_mode="Markdown",
    )


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
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
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n\n"
        f"`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


# =========================
# TEST
# =========================

async def test(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await send_task(context, "feed")


# =========================
# DAILY REPORT
# =========================

async def daily_report(
    context: ContextTypes.DEFAULT_TYPE,
):
    rows = get_today_tasks()

    lines = [
        "📊 *Отчёт по аквариуму*",
        f"📅 {datetime.now(TZ).strftime('%d.%m.%Y')}",
        "",
    ]

    if not rows:
        lines.append("Сегодня задач ещё не было.")
    else:
        completed_count = 0

        for row in rows:
            if row["completed_at"]:
                completed_time = datetime.fromisoformat(
                    row["completed_at"]
                ).strftime("%H:%M")

                lines.append(
                    f"✅ {row['task_name']} — {completed_time}"
                )

                completed_count += 1

            else:
                lines.append(
                    f"❌ {row['task_name']} — не выполнено"
                )

        lines.extend([
            "",
            f"📈 Выполнено: {completed_count}/{len(rows)}",
        ])

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

    # 22:30 report
    job_queue.run_daily(
        daily_report,
        time=time(22, 30, tzinfo=TZ),
        name="daily_report",
    )


# =========================
# MAIN
# =========================

def main():

    init_db()

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
