import os
import sqlite3
from datetime import time, datetime, timedelta
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

USERS = [
    BROTHER_ID,
]

# =========================
# TIMEZONE
# =========================

TZ = ZoneInfo("Europe/Prague")


# =========================
# DATABASE
# =========================

DB_FILE = "/app/data/aquarium.db"

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
            completed_by INTEGER,
            completed_by_name TEXT,
            UNIQUE(date, task_id)
        )
    """)

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN completed_by INTEGER"
        )
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN completed_by_name TEXT"
        )
    except sqlite3.OperationalError:
        pass

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


def complete_task(
    task_id,
    user_id,
    user_name,
):
    now = datetime.now(TZ)
    date = now.date().isoformat()
    timestamp = now.isoformat()

    conn = get_db()

    conn.execute(
        """
        UPDATE tasks
        SET completed_at = ?,
            completed_by = ?,
            completed_by_name = ?
        WHERE date = ?
        AND task_id = ?
        AND completed_at IS NULL
        """,
        (
            timestamp,
            user_id,
            user_name,
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

def get_overdue_tasks():
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE date = ?
        AND completed_at IS NULL
        """,
        (today_key(),),
    ).fetchall()

    conn.close()

    now = datetime.now(TZ)
    overdue = []

    for row in rows:
        sent_at = datetime.fromisoformat(row["sent_at"])

        if now - sent_at >= timedelta(hours=1):
            overdue.append(row)

    return overdue

async def today(update, context):
    rows = get_today_tasks()

    lines = [
        "📋 *Аквариум сегодня*",
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

                completed_by = row["completed_by_name"] or "Неизвестно"

                lines.append(
                    f"✅ {row['task_name']}\n"
                    f"   👤 {completed_by} — 🕐 {completed_time}"
                )

                completed_count += 1
            else:
                lines.append(
                    f"⏳ {row['task_name']}\n"
                    f"   Не выполнено"
                )

            lines.append("")

        lines.append(
            f"📈 Выполнено: {completed_count}/{len(rows)}"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )

async def history(update, context):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE date >= date('now', 'localtime', '-6 days')
        ORDER BY date DESC, sent_at DESC
        """
    ).fetchall()

    conn.close()

    if not rows:
        await update.message.reply_text(
            "📚 История пока пустая."
        )
        return

    lines = [
        "📚 *История аквариума*",
        "Последние 7 дней:",
        "",
    ]

    current_date = None

    for row in rows:
        if row["date"] != current_date:
            current_date = row["date"]

            date_obj = datetime.fromisoformat(row["date"])

            lines.append(
                f"📅 *{date_obj.strftime('%d.%m.%Y')}*"
            )

        if row["completed_at"]:
            completed_time = datetime.fromisoformat(
                row["completed_at"]
            ).strftime("%H:%M")

            completed_by = row["completed_by_name"] or "Неизвестно"

            lines.append(
                f"  ✅ {row['task_name']}\n"
                f"     👤 {completed_by} — {completed_time}"
            )
        else:
            lines.append(
                f"  ❌ {row['task_name']}\n"
                f"     Не выполнено"
            )

        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )
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

    for user_id in USERS:
        await context.bot.send_message(
            chat_id=user_id,
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

    user = query.from_user
    user_id = user.id

    if user_id == BROTHER_ID:
        user_name = "Брат"
    elif user_id == OWNER_ID:
        user_name = "Влад"
    else:
        user_name = user.first_name or "Неизвестный пользователь"

    complete_task(
        task_id,
        user_id,
        user_name,
    )

    task_name = TASKS[task_id]

    await query.edit_message_text(
        "✅ *Выполнено*\n\n"
        f"{task_name}\n\n"
        f"👤 Выполнил: {user_name}\n"
        f"🕐 Время: {datetime.now(TZ).strftime('%H:%M')}",
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

                completed_by = row["completed_by_name"] or "Неизвестно"

                lines.append(
                    f"✅ {row['task_name']}\n"
                    f"   🕐 {completed_time} — 👤 {completed_by}"
                )

                completed_count += 1

            else:

                lines.append(
                    f"❌ {row['task_name']}\n"
                    f"   Не выполнено"
                )

            lines.append("")

        lines.append(
            f"📈 Выполнено: {completed_count}/{len(rows)}"
        )

    await context.bot.send_message(
        chat_id=OWNER_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )
# =========================
# SCHEDULE
# =========================

def setup_schedule(app):
    job_queue = app.job_queue

    # Каждый день — 07:00
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

    # Каждый день — 14:30
    job_queue.run_daily(
        lambda context: send_task(context, "light_on"),
        time=time(14, 30, tzinfo=TZ),
        name="light_on",
    )

    # Каждый день — 21:00
    job_queue.run_daily(
        lambda context: send_task(context, "light_off"),
        time=time(21, 0, tzinfo=TZ),
        name="light_off",
    )

    # Каждый день — 22:00
    job_queue.run_daily(
        lambda context: send_task(context, "air_off"),
        time=time(22, 0, tzinfo=TZ),
        name="air_off",
    )

    # Воскресенье — 12:00
    job_queue.run_daily(
        lambda context: send_task(context, "water"),
        time=time(12, 0, tzinfo=TZ),
        days=(6,),
        name="water",
    )

    # Среда + воскресенье — 12:00
    job_queue.run_daily(
        lambda context: send_task(context, "filter"),
        time=time(12, 0, tzinfo=TZ),
        days=(2, 6),
        name="filter",
    )

    # Каждый день — 22:30
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
        CommandHandler("today", today)
    )

    app.add_handler(
        CommandHandler("history", history)
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
