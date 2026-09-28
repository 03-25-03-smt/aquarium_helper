import os
import logging
import sqlite3
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx логирует каждый запрос polling — слишком шумно
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


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

# На Railway к /app/data должен быть подключён Volume,
# иначе база будет стираться при каждом деплое/перезапуске.
DB_FILE = os.environ.get("DB_FILE", "/app/data/aquarium.db")


def md(text):
    """Экранирует текст для parse_mode="Markdown" (имена пользователей и т.п.)."""
    return escape_markdown(str(text), version=1)


async def safe_send(context, chat_id, **kwargs):
    """Отправка сообщения, которая не роняет job, если пользователь
    не нажал /start или заблокировал бота."""
    try:
        await context.bot.send_message(chat_id=chat_id, **kwargs)
        return True
    except TelegramError as e:
        logger.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
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
            "ALTER TABLE tasks ADD COLUMN completed_by_name TEXT"
        )
    except sqlite3.OperationalError:
        pass

    try:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN overdue_notified INTEGER DEFAULT 0"
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

# Тестовые задачи хранятся отдельно от настоящих, чтобы не мешать
# реальному учёту и не показываться в /today, /history и отчёте
TEST_TASKS = {
    "test": "🧪 Тестовая задача",
    "overdue_test": "🧪 Тестовая просроченная задача",
}

ALL_TASKS = {**TASKS, **TEST_TASKS}

OVERDUE_AFTER = timedelta(hours=1)


def is_test_task(task_id):
    return task_id in TEST_TASKS


def exclude_test_sql():
    """SQL-условие, которое убирает тестовые задачи из выборки."""
    placeholders = ", ".join("?" for _ in TEST_TASKS)
    return f"AND task_id NOT IN ({placeholders})", tuple(TEST_TASKS)


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

    if is_test_task(task_id):
        # Тестовую задачу каждый раз создаём заново,
        # чтобы /test можно было повторять сколько угодно
        sql = "INSERT OR REPLACE"
    else:
        # Настоящая задача за день создаётся один раз
        sql = "INSERT OR IGNORE"

    conn.execute(
        f"""
        {sql} INTO tasks
        (date, task_id, task_name, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            date,
            task_id,
            ALL_TASKS[task_id],
            timestamp,
        ),
    )

    conn.commit()
    conn.close()

    return date


def complete_task(
    task_id,
    user_id,
    user_name,
    date,
):
    """Отмечает задачу выполненной. Возвращает строку из БД
    (или None, если задачи нет)."""
    now = datetime.now(TZ)
    timestamp = now.isoformat()

    conn = get_db()

    cursor = conn.execute(
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

    # True — задачу отметили именно сейчас (а не повторное нажатие кнопки)
    just_completed = cursor.rowcount > 0

    conn.commit()

    row = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND task_id = ?",
        (date, task_id),
    ).fetchone()

    conn.close()

    return row, just_completed

def get_today_tasks():
    """Задачи за сегодня без тестовых."""
    conn = get_db()

    exclude, params = exclude_test_sql()

    rows = conn.execute(
        f"""
        SELECT *
        FROM tasks
        WHERE date = ?
        {exclude}
        ORDER BY sent_at
        """,
        (today_key(), *params),
    ).fetchall()

    conn.close()

    return rows

def get_overdue_tasks(include_test=True):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE date = ?
        AND completed_at IS NULL
        ORDER BY sent_at
        """,
        (today_key(),),
    ).fetchall()

    conn.close()

    now = datetime.now(TZ)
    overdue = []

    for row in rows:
        if not include_test and is_test_task(row["task_id"]):
            continue

        sent_at = datetime.fromisoformat(row["sent_at"])

        if now - sent_at >= OVERDUE_AFTER:
            overdue.append(row)

    return overdue

def mark_overdue_notified(task_id):
    conn = get_db()

    conn.execute("""
        UPDATE tasks
        SET overdue_notified = 1
        WHERE date = ? AND task_id = ?
    """, (today_key(), task_id))

    conn.commit()
    conn.close()


async def check_overdue_tasks(context):
    rows = get_overdue_tasks()

    for row in rows:
        if row["overdue_notified"]:
            continue

        sent_time = datetime.fromisoformat(
            row["sent_at"]
        ).strftime("%H:%M")

        for user_id in [BROTHER_ID, OWNER_ID]:
            await safe_send(
                context,
                user_id,
                text=(
                    "⚠️ *Просроченная задача*\n\n"
                    f"{md(row['task_name'])}\n\n"
                    f"Запланировано: {sent_time}\n"
                    "Задача ещё не выполнена."
                ),
                parse_mode="Markdown",
            )

        # Помечаем даже если отправка не удалась — иначе будет спам каждую минуту
        mark_overdue_notified(row["task_id"])

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

                completed_by = md(row["completed_by_name"] or "Неизвестно")

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

    # Дата считается по времени Праги, а не по времени сервера (на Railway это UTC)
    week_ago = (datetime.now(TZ).date() - timedelta(days=6)).isoformat()

    exclude, params = exclude_test_sql()

    rows = conn.execute(
        f"""
        SELECT *
        FROM tasks
        WHERE date >= ?
        {exclude}
        ORDER BY date DESC, sent_at DESC
        """,
        (week_ago, *params),
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

            completed_by = md(row["completed_by_name"] or "Неизвестно")

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


async def overdue(update, context):
    """Показывает задачи, которые не выполнены дольше часа."""
    # Владельцу показываем и тестовые задачи — чтобы проверять /overdue_test
    include_test = update.effective_user.id == OWNER_ID

    rows = get_overdue_tasks(include_test=include_test)

    if not rows:
        await update.message.reply_text(
            "👍 Просроченных задач нет."
        )
        return

    now = datetime.now(TZ)

    lines = [
        "⚠️ *Просроченные задачи*",
        "",
    ]

    for row in rows:
        sent_at = datetime.fromisoformat(row["sent_at"])
        minutes = int((now - sent_at).total_seconds() // 60)
        hours, minutes = divmod(minutes, 60)

        lines.append(
            f"⏳ {row['task_name']}\n"
            f"   Запланировано: {sent_at.strftime('%H:%M')} "
            f"(прошло {hours} ч {minutes} мин)"
        )
        lines.append("")

    lines.append(f"Всего: {len(rows)}")

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
    """Отправляет задачу всем из USERS.
    Возвращает список ID, кому сообщение реально дошло."""
    task_name = ALL_TASKS[task_id]

    date = save_task(task_id)

    # Дата в callback_data нужна, чтобы кнопка, нажатая после полуночи,
    # отметила задачу за правильный день
    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Выполнено",
                callback_data=f"done:{date}:{task_id}",
            )
        ]
    ]

    message = (
        "🐠 *Уход за аквариумом*\n\n"
        f"{task_name}\n\n"
        "Когда выполнишь — нажми кнопку ниже."
    )

    delivered = []

    for user_id in USERS:
        ok = await safe_send(
            context,
            user_id,
            text=message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        if ok:
            delivered.append(user_id)

    return delivered


async def scheduled_task(context: ContextTypes.DEFAULT_TYPE):
    """Callback для JobQueue: task_id передаётся через job.data."""
    await send_task(context, context.job.data)

# =========================
# BUTTON HANDLER
# =========================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    data = query.data or ""

    if not data.startswith("done:"):
        return

    parts = data.split(":")

    if len(parts) == 3:
        _, date, task_id = parts
    else:
        # Старый формат кнопок без даты: done:<task_id>
        date, task_id = today_key(), parts[1]

    if task_id not in ALL_TASKS:
        return

    user = query.from_user
    user_id = user.id

    if user_id == BROTHER_ID:
        user_name = "Брат"
    elif user_id == OWNER_ID:
        user_name = "Влад"
    else:
        user_name = user.first_name or "Неизвестный пользователь"

    row, just_completed = complete_task(
        task_id,
        user_id,
        user_name,
        date,
    )

    task_name = ALL_TASKS[task_id]

    if row is None or row["completed_at"] is None:
        await query.edit_message_text(
            f"⚠️ Не удалось найти задачу «{task_name}» в базе."
        )
        return

    completed_time = datetime.fromisoformat(
        row["completed_at"]
    ).strftime("%H:%M")

    await query.edit_message_text(
        "✅ *Выполнено*\n\n"
        f"{task_name}\n\n"
        f"👤 Выполнил: {md(row['completed_by_name'])}\n"
        f"🕐 Время: {completed_time}",
        parse_mode="Markdown",
    )

    # Мгновенное уведомление владельцу: только при первом нажатии
    # и только если выполнил не сам владелец
    if just_completed and user_id != OWNER_ID:
        sent_time = datetime.fromisoformat(row["sent_at"]).strftime("%H:%M")
        test_mark = "🧪 (тест) " if is_test_task(task_id) else ""

        await safe_send(
            context,
            OWNER_ID,
            text=(
                f"✅ {test_mark}*{md(row['completed_by_name'])}* выполнил:\n\n"
                f"{task_name}\n\n"
                f"📨 Напоминание: {sent_time}\n"
                f"🕐 Выполнено: {completed_time}"
            ),
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
    # Тестовые команды — только для владельца
    if update.effective_user.id != OWNER_ID:
        return

    # Отдельная тестовая задача: не трогает настоящую «Покормить рыбок»
    delivered = await send_task(context, "test")

    if delivered:
        names = ", ".join(
            "Брат" if uid == BROTHER_ID else str(uid)
            for uid in delivered
        )
        await update.message.reply_text(
            f"🧪 Тестовая задача отправлена: {names}"
        )
    else:
        await update.message.reply_text(
            "⚠️ Тестовая задача никому не дошла.\n"
            "Проверь, что брат нажал /start у бота."
        )


async def overdue_test(update, context):
    if update.effective_user.id != OWNER_ID:
        return

    conn = get_db()

    test_time = datetime.now(TZ) - timedelta(hours=2)

    conn.execute("""
        INSERT OR REPLACE INTO tasks
        (date, task_id, task_name, sent_at, completed_at, completed_by, completed_by_name, overdue_notified)
        VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0)
    """, (
        today_key(),
        "overdue_test",
        TEST_TASKS["overdue_test"],
        test_time.isoformat(),
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🧪 Тестовая просроченная задача создана.\n"
        "Она считается отправленной 2 часа назад.\n\n"
        "Уведомление придёт в течение минуты.\n"
        "Проверить список можно командой /overdue."
    )


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

                completed_by = md(row["completed_by_name"] or "Неизвестно")

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

    await safe_send(
        context,
        OWNER_ID,
        text="\n".join(lines),
        parse_mode="Markdown",
    )
# =========================
# SCHEDULE
# =========================

def setup_schedule(app):
    job_queue = app.job_queue

    # ВАЖНО: в python-telegram-bot v20+ дни недели в run_daily:
    # 0 = воскресенье, 1 = понедельник, ..., 6 = суббота
    SUNDAY = 0
    WEDNESDAY = 3

    schedule = [
        # (task_id, время, дни недели)
        ("feed",      time(7, 0, tzinfo=TZ),   None),  # каждый день 07:00
        ("air_on",    time(7, 0, tzinfo=TZ),   None),  # каждый день 07:00
        ("light_on",  time(14, 30, tzinfo=TZ), None),  # каждый день 14:30
        ("light_off", time(21, 0, tzinfo=TZ),  None),  # каждый день 21:00
        ("air_off",   time(22, 0, tzinfo=TZ),  None),  # каждый день 22:00
        ("water",     time(12, 0, tzinfo=TZ),  (SUNDAY,)),             # вс 12:00
        ("filter",    time(12, 0, tzinfo=TZ),  (WEDNESDAY, SUNDAY)),  # ср + вс 12:00
    ]

    for task_id, run_time, days in schedule:
        kwargs = {"days": days} if days else {}
        job_queue.run_daily(
            scheduled_task,
            time=run_time,
            data=task_id,
            name=task_id,
            **kwargs,
        )

    # Каждый день — 23:15.
    # Позже последней задачи (22:00) + час на выполнение + проверка просрочки в 23:00,
    # чтобы «Выключить воздух» успел попасть в отчёт с правильным статусом
    job_queue.run_daily(
        daily_report,
        time=time(23, 15, tzinfo=TZ),
        name="daily_report",
    )

    # Проверка просроченных задач раз в минуту
    job_queue.run_repeating(
        check_overdue_tasks,
        interval=60,
        first=60,
        name="overdue_checker",
    )


async def error_handler(update, context):
    logger.error("Ошибка при обработке апдейта", exc_info=context.error)

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
        CommandHandler("overdue", overdue)
    )

    app.add_handler(
        CommandHandler("test", test)
    )

    app.add_handler(
        CommandHandler("overdue_test", overdue_test)
    )

    app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    app.add_error_handler(error_handler)

    setup_schedule(app)

    logger.info("🐟 Aquarium Helper started")

    app.run_polling()


if __name__ == "__main__":
    main()
