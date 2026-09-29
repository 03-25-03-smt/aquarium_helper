import os
import logging
import sqlite3
import tempfile
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.error import TelegramError
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
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


def user_display_name(user):
    if user.id == BROTHER_ID:
        return "Брат"
    if user.id == OWNER_ID:
        return "Влад"
    return user.first_name or "Неизвестный пользователь"


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

# Тестовые задачи хранятся отдельно от настоящих, чтобы не мешать
# реальному учёту и не показываться в /today, /history, отчёте и статистике
TEST_TASKS = {
    "test": "🧪 Тестовая задача",
    "overdue_test": "🧪 Тестовая просроченная задача",
}

ALL_TASKS = {**TASKS, **TEST_TASKS}


def is_test_task(task_id):
    return task_id in TEST_TASKS


def exclude_test_sql():
    """SQL-условие, которое убирает тестовые задачи из выборки."""
    placeholders = ", ".join("?" for _ in TEST_TASKS)
    return f"AND task_id NOT IN ({placeholders})", tuple(TEST_TASKS)


# =========================
# TIMINGS
# =========================

REMIND_AFTER = timedelta(minutes=30)    # повторное напоминание брату
OVERDUE_AFTER = timedelta(hours=1)      # предупреждение о просрочке обоим
SNOOZE_FOR = timedelta(minutes=15)      # кнопка «Отложить»
MAX_SNOOZES = 3                         # сколько раз можно отложить одну задачу
MISSED_TASK_WINDOW = timedelta(hours=3) # досылать пропущенные при перезапуске задачи не старше 3 ч

REPORT_TIME = time(23, 15, tzinfo=TZ)   # ежедневный отчёт
WEEKLY_STATS_TIME = time(23, 20, tzinfo=TZ)  # недельная статистика (воскресенье)


# =========================
# SCHEDULE CONFIG
# =========================

# ВАЖНО: в python-telegram-bot v20+ дни недели в run_daily:
# 0 = воскресенье, 1 = понедельник, ..., 6 = суббота
SUNDAY, MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY = range(7)
ALL_DAYS = tuple(range(7))

# Разгрузочный день: рыбок не кормим, брату приходит напоминание НЕ кормить
FASTING_DAYS = (THURSDAY,)
FEED_DAYS = tuple(d for d in ALL_DAYS if d not in FASTING_DAYS)

# Расписание по умолчанию. Время можно поменять командой /settime —
# изменения хранятся в базе и переживают перезапуск.
DEFAULT_SCHEDULE = [
    # (task_id, (час, минута), дни недели)
    ("feed",      (7, 0),   FEED_DAYS),
    ("air_on",    (7, 0),   ALL_DAYS),
    ("light_on",  (14, 30), ALL_DAYS),
    ("light_off", (21, 0),  ALL_DAYS),
    ("air_off",   (22, 0),  ALL_DAYS),
    ("water",     (12, 0),  (SUNDAY,)),
    # Одна задача может встречаться несколько раз с разным временем в разные дни
    ("filter",    (19, 0),  (WEDNESDAY,)),
    ("filter",    (12, 0),  (SUNDAY,)),
]

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

# Короткие названия в порядке PTB (0 = воскресенье)
PTB_DAY_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"]


def to_ptb_weekday(day):
    """Python: пн = 0 ... вс = 6  →  PTB: вс = 0 ... сб = 6"""
    return (day.weekday() + 1) % 7


def days_text(days):
    if tuple(days) == ALL_DAYS:
        return "каждый день"

    missing = [d for d in ALL_DAYS if d not in days]
    if len(missing) == 1:
        return f"каждый день, кроме {PTB_DAY_SHORT[missing[0]]}"

    # Порядок с понедельника
    order = [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY]
    return ", ".join(PTB_DAY_SHORT[d] for d in order if d in days)


# =========================
# DATABASE
# =========================

# На Railway к /app/data должен быть подключён Volume,
# иначе база будет стираться при каждом деплое/перезапуске.
DB_FILE = os.environ.get("DB_FILE", "/app/data/aquarium.db")


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def add_column(conn, definition):
    """ALTER TABLE, который не падает, если колонка уже есть."""
    try:
        conn.execute(f"ALTER TABLE tasks ADD COLUMN {definition}")
    except sqlite3.OperationalError:
        pass


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

    # Миграции для старых баз
    add_column(conn, "completed_by_name TEXT")
    add_column(conn, "overdue_notified INTEGER DEFAULT 0")
    add_column(conn, "reminded INTEGER DEFAULT 0")
    add_column(conn, "remind_at TEXT")
    add_column(conn, "overdue_at TEXT")
    add_column(conn, "snooze_count INTEGER DEFAULT 0")
    add_column(conn, "cant_at TEXT")
    add_column(conn, "cant_reason TEXT")
    add_column(conn, "cant_by_name TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedule_overrides (
            task_id TEXT PRIMARY KEY,
            time TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS achievements (
            streak INTEGER PRIMARY KEY,
            achieved_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# ---------- settings ----------

def get_setting(key):
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()


def delete_setting(key):
    conn = get_db()
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()
    conn.close()


# ---------- pause ----------

PAUSE_FOREVER = "forever"


def get_pause_until():
    """None — паузы нет; PAUSE_FOREVER — до /resume; иначе datetime."""
    value = get_setting("paused_until")

    if value is None or value == PAUSE_FOREVER:
        return value

    return datetime.fromisoformat(value)


def is_paused():
    until = get_pause_until()

    if until is None:
        return False
    if until == PAUSE_FOREVER:
        return True

    return datetime.now(TZ) < until


def pause_text():
    until = get_pause_until()

    if until == PAUSE_FOREVER:
        return "до команды /resume"

    return f"до {until.strftime('%d.%m.%Y %H:%M')}"


# ---------- schedule ----------

def get_schedule():
    """Расписание с учётом изменений из /settime.
    Возвращает список (task_id, time, days)."""
    conn = get_db()
    overrides = {
        row["task_id"]: row["time"]
        for row in conn.execute("SELECT * FROM schedule_overrides")
    }
    conn.close()

    schedule = []

    for task_id, (hour, minute), days in DEFAULT_SCHEDULE:
        if task_id in overrides:
            hour, minute = map(int, overrides[task_id].split(":"))

        schedule.append((task_id, time(hour, minute, tzinfo=TZ), days))

    return schedule


def set_schedule_override(task_id, value):
    conn = get_db()

    if value is None:
        conn.execute(
            "DELETE FROM schedule_overrides WHERE task_id = ?", (task_id,)
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO schedule_overrides (task_id, time) VALUES (?, ?)",
            (task_id, value),
        )

    conn.commit()
    conn.close()


def tasks_for_date(day):
    """Список (время, task_id) из расписания на указанную дату, по времени."""
    ptb_weekday = to_ptb_weekday(day)

    result = [
        (run_time, task_id)
        for task_id, run_time, days in get_schedule()
        if ptb_weekday in days
    ]

    return sorted(result, key=lambda item: (item[0].hour, item[0].minute))


def is_fasting_day(day):
    return to_ptb_weekday(day) in FASTING_DAYS


def feed_time():
    for task_id, run_time, _ in get_schedule():
        if task_id == "feed":
            return run_time


# =========================
# HELPERS
# =========================

def md(text):
    """Экранирует текст для parse_mode="Markdown" (имена, причины и т.п.)."""
    return escape_markdown(str(text), version=1)


async def safe_send(context, chat_id, **kwargs):
    """Отправка сообщения, которая не роняет job, если пользователь
    не нажал /start или заблокировал бота.
    context может быть и Application — у него тоже есть .bot"""
    try:
        await context.bot.send_message(chat_id=chat_id, **kwargs)
        return True
    except TelegramError as e:
        logger.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return False


def is_owner(update):
    return update.effective_user.id == OWNER_ID


def today_key():
    return datetime.now(TZ).date().isoformat()


def parse_dt(value):
    return datetime.fromisoformat(value) if value else None


def hhmm(value):
    return parse_dt(value).strftime("%H:%M")


def remind_at_of(row):
    # Для старых записей без remind_at считаем от sent_at
    return parse_dt(row["remind_at"]) or parse_dt(row["sent_at"]) + REMIND_AFTER


def overdue_at_of(row):
    return parse_dt(row["overdue_at"]) or parse_dt(row["sent_at"]) + OVERDUE_AFTER


def format_task_row(row, indent="", pending_icon="⏳"):
    """Одна задача для /today, /history и отчёта."""
    if row["completed_at"]:
        who = md(row["completed_by_name"] or "Неизвестно")
        return (
            f"{indent}✅ {row['task_name']}\n"
            f"{indent}   👤 {who} — 🕐 {hhmm(row['completed_at'])}"
        )

    if row["cant_at"]:
        who = md(row["cant_by_name"] or "Неизвестно")
        reason = md(row["cant_reason"] or "без причины")
        return (
            f"{indent}🚫 {row['task_name']}\n"
            f"{indent}   👤 {who} не смог: {reason}"
        )

    extra = ""
    if row["snooze_count"]:
        extra = f" (откладывал {row['snooze_count']} р.)"

    return (
        f"{indent}{pending_icon} {row['task_name']}\n"
        f"{indent}   Не выполнено{extra}"
    )


# =========================
# KEYBOARDS
# =========================

MENU_TODAY = "📋 Сегодня"
MENU_TOMORROW = "🗓 Завтра"
MENU_OVERDUE = "⚠️ Просрочки"
MENU_HISTORY = "📚 История"
MENU_STATS = "📊 Статистика"
MENU_HELP = "❓ Помощь"

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [MENU_TODAY, MENU_TOMORROW],
        [MENU_OVERDUE, MENU_HISTORY],
        [MENU_STATS, MENU_HELP],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

CANT_REASONS = {
    "away": "🏠 Не дома",
    "nothing": "📦 Нечем (нет корма/средств)",
    "broken": "🛠 Что-то сломалось",
    "other": "✍️ Другая причина",
}


def task_keyboard(date, task_id, snooze_count=0):
    # Дата в callback_data нужна, чтобы кнопка, нажатая после полуночи,
    # отметила задачу за правильный день
    rows = [
        [InlineKeyboardButton("✅ Выполнено", callback_data=f"done:{date}:{task_id}")],
    ]

    second = []
    if snooze_count < MAX_SNOOZES:
        second.append(
            InlineKeyboardButton(
                f"⏰ Отложить на {int(SNOOZE_FOR.total_seconds() // 60)} мин",
                callback_data=f"snooze:{date}:{task_id}",
            )
        )
    second.append(
        InlineKeyboardButton("❌ Не могу", callback_data=f"cant:{date}:{task_id}")
    )
    rows.append(second)

    return InlineKeyboardMarkup(rows)


def reasons_keyboard(date, task_id):
    rows = [
        [InlineKeyboardButton(text, callback_data=f"why:{date}:{task_id}:{code}")]
        for code, text in CANT_REASONS.items()
    ]
    rows.append(
        [InlineKeyboardButton("↩️ Назад", callback_data=f"back:{date}:{task_id}")]
    )
    return InlineKeyboardMarkup(rows)


# =========================
# DATABASE HELPERS
# =========================

def save_task(task_id):
    """Создаёт запись о задаче на сегодня.
    Возвращает (date, created): created=False, если задача уже была."""
    now = datetime.now(TZ)
    date = now.date().isoformat()

    conn = get_db()

    if is_test_task(task_id):
        # Тестовую задачу каждый раз создаём заново,
        # чтобы /test можно было повторять сколько угодно
        sql = "INSERT OR REPLACE"
    else:
        # Настоящая задача за день создаётся один раз
        sql = "INSERT OR IGNORE"

    cursor = conn.execute(
        f"""
        {sql} INTO tasks
        (date, task_id, task_name, sent_at, remind_at, overdue_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            task_id,
            ALL_TASKS[task_id],
            now.isoformat(),
            (now + REMIND_AFTER).isoformat(),
            (now + OVERDUE_AFTER).isoformat(),
        ),
    )

    created = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return date, created


def get_task(date, task_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM tasks WHERE date = ? AND task_id = ?",
        (date, task_id),
    ).fetchone()
    conn.close()
    return row


def complete_task(task_id, user_id, user_name, date):
    """Отмечает задачу выполненной.
    Возвращает (row, just_completed)."""
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
        (datetime.now(TZ).isoformat(), user_id, user_name, date, task_id),
    )

    # True — задачу отметили именно сейчас (а не повторное нажатие кнопки)
    just_completed = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return get_task(date, task_id), just_completed


def snooze_task(date, task_id):
    """Откладывает задачу. Возвращает (row, ok)."""
    now = datetime.now(TZ)
    row = get_task(date, task_id)

    if row is None or row["completed_at"] or row["cant_at"]:
        return row, False

    if (row["snooze_count"] or 0) >= MAX_SNOOZES:
        return row, False

    remind_at = now + SNOOZE_FOR
    # Предупреждение о просрочке сдвигаем, чтобы после напоминания
    # у брата было ещё 30 минут
    overdue_at = max(overdue_at_of(row), remind_at + REMIND_AFTER)

    conn = get_db()
    conn.execute(
        """
        UPDATE tasks
        SET remind_at = ?,
            reminded = 0,
            overdue_at = ?,
            snooze_count = COALESCE(snooze_count, 0) + 1
        WHERE date = ? AND task_id = ?
        """,
        (remind_at.isoformat(), overdue_at.isoformat(), date, task_id),
    )
    conn.commit()
    conn.close()

    return get_task(date, task_id), True


def cant_task(date, task_id, user_name, reason):
    """Отмечает «не могу». Возвращает (row, ok)."""
    conn = get_db()

    cursor = conn.execute(
        """
        UPDATE tasks
        SET cant_at = ?,
            cant_reason = ?,
            cant_by_name = ?
        WHERE date = ?
        AND task_id = ?
        AND completed_at IS NULL
        AND cant_at IS NULL
        """,
        (datetime.now(TZ).isoformat(), reason, user_name, date, task_id),
    )

    ok = cursor.rowcount > 0

    conn.commit()
    conn.close()

    return get_task(date, task_id), ok


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


def get_pending_tasks():
    """Невыполненные сегодняшние задачи (включая тестовые), без «не могу»."""
    conn = get_db()

    rows = conn.execute(
        """
        SELECT *
        FROM tasks
        WHERE date = ?
        AND completed_at IS NULL
        AND cant_at IS NULL
        ORDER BY sent_at
        """,
        (today_key(),),
    ).fetchall()

    conn.close()

    return rows


def get_overdue_tasks(include_test=True):
    now = datetime.now(TZ)

    return [
        row
        for row in get_pending_tasks()
        if (include_test or not is_test_task(row["task_id"]))
        and now >= overdue_at_of(row)
    ]


def set_task_flag(date, task_id, column):
    # column — только из кода, не из пользовательского ввода
    conn = get_db()
    conn.execute(
        f"UPDATE tasks SET {column} = 1 WHERE date = ? AND task_id = ?",
        (date, task_id),
    )
    conn.commit()
    conn.close()


# =========================
# STATS & STREAKS
# =========================

ACHIEVEMENTS = {
    3: "🥉 3 дня без пропусков",
    7: "🥈 Неделя без пропусков",
    14: "🥇 Две недели без пропусков",
    30: "🏆 Месяц без пропусков",
    100: "👑 100 дней без пропусков",
}


def compute_streaks():
    """Серия = подряд идущие дни, где выполнены ВСЕ задачи.
    Дни без задач (пауза, бот выключен) серию не прерывают.
    Сегодняшний день засчитывается, только когда всё уже выполнено.
    Возвращает (текущая, лучшая)."""
    exclude, params = exclude_test_sql()

    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT date,
               COUNT(*) AS total,
               SUM(completed_at IS NOT NULL) AS done
        FROM tasks
        WHERE 1 = 1 {exclude}
        GROUP BY date
        ORDER BY date
        """,
        params,
    ).fetchall()
    conn.close()

    today = today_key()
    current = best = 0

    for row in rows:
        full = row["done"] == row["total"]

        if row["date"] == today and not full:
            # День ещё не закончился — не ломаем серию
            continue

        current = current + 1 if full else 0
        best = max(best, current)

    return current, best


def get_achieved():
    conn = get_db()
    rows = conn.execute("SELECT streak FROM achievements ORDER BY streak").fetchall()
    conn.close()
    return [row["streak"] for row in rows]


def award_new_achievements():
    """Сохраняет новые достижения. Возвращает список новых."""
    current, _ = compute_streaks()
    achieved = set(get_achieved())

    new = [m for m in ACHIEVEMENTS if current >= m and m not in achieved]

    if new:
        conn = get_db()
        conn.executemany(
            "INSERT OR IGNORE INTO achievements (streak, achieved_at) VALUES (?, ?)",
            [(m, datetime.now(TZ).isoformat()) for m in new],
        )
        conn.commit()
        conn.close()

    return new


def build_stats_text(days=7):
    today = datetime.now(TZ).date()
    start = today - timedelta(days=days - 1)

    exclude, params = exclude_test_sql()

    conn = get_db()
    rows = conn.execute(
        f"""
        SELECT *
        FROM tasks
        WHERE date >= ?
        {exclude}
        """,
        (start.isoformat(), *params),
    ).fetchall()
    conn.close()

    lines = [
        f"📊 *Статистика за {days} дней*",
        f"{start.strftime('%d.%m')} — {today.strftime('%d.%m.%Y')}",
        "",
    ]

    if not rows:
        lines.append("Данных пока нет.")
        return "\n".join(lines)

    total = len(rows)
    done = [r for r in rows if r["completed_at"]]
    cant = [r for r in rows if not r["completed_at"] and r["cant_at"]]
    not_done = total - len(done) - len(cant)
    snoozes = sum(r["snooze_count"] or 0 for r in rows)

    reaction = [
        parse_dt(r["completed_at"]) - parse_dt(r["sent_at"]) for r in done
    ]
    on_time = sum(1 for d in reaction if d <= OVERDUE_AFTER)

    pct = round(len(done) * 100 / total)

    lines.append(f"✅ Выполнено: {len(done)} из {total} ({pct}%)")

    if reaction:
        avg_min = int(sum(d.total_seconds() for d in reaction) / len(reaction) // 60)
        lines.append(f"⏱ Вовремя (в течение часа): {on_time}")
        lines.append(f"🕐 Среднее время реакции: {avg_min} мин")

    lines.append(f"⏰ Откладывал: {snoozes} р.")
    lines.append(f"🚫 Не смог: {len(cant)}")
    lines.append(f"❌ Не выполнено: {not_done}")
    lines.append("")

    current, best = compute_streaks()
    lines.append(f"🔥 Текущая серия: {current} дн.")
    lines.append(f"🏆 Лучшая серия: {best} дн.")

    achieved = get_achieved()
    if achieved:
        lines.append("")
        lines.append("🎖 *Достижения:*")
        for m in achieved:
            lines.append(f"   {ACHIEVEMENTS.get(m, f'{m} дней')}")

    return "\n".join(lines)


# =========================
# SEND TASK
# =========================

async def send_task(context, task_id, note=None):
    """Отправляет задачу всем из USERS.
    Возвращает список ID, кому сообщение дошло.
    Если настоящая задача сегодня уже отправлялась — повторно не шлёт."""
    task_name = ALL_TASKS[task_id]

    date, created = save_task(task_id)

    if not created:
        logger.info("Задача %s за %s уже была отправлена", task_id, date)
        return []

    message = f"🐠 *Уход за аквариумом*\n\n{task_name}\n\n"

    if note:
        message += f"{note}\n\n"

    message += "Когда выполнишь — нажми кнопку ниже."

    delivered = []

    for user_id in USERS:
        ok = await safe_send(
            context,
            user_id,
            text=message,
            parse_mode="Markdown",
            reply_markup=task_keyboard(date, task_id),
        )

        if ok:
            delivered.append(user_id)

    return delivered


async def scheduled_task(context: ContextTypes.DEFAULT_TYPE):
    """Callback для JobQueue: task_id передаётся через job.data."""
    if is_paused():
        logger.info("Пауза — задача %s пропущена", context.job.data)
        return

    await send_task(context, context.job.data)


async def fasting_notice(context: ContextTypes.DEFAULT_TYPE):
    if is_paused():
        return

    for user_id in USERS:
        await safe_send(
            context,
            user_id,
            text=(
                "🚫 *Сегодня разгрузочный день*\n\n"
                "Рыбок сегодня *НЕ кормим* — это полезно для их пищеварения "
                "и чистоты воды.\n\n"
                "Остальные задачи — как обычно."
            ),
            parse_mode="Markdown",
        )


# =========================
# PENDING TASKS CHECKER
# =========================

async def check_pending_tasks(context):
    """Раз в минуту: снятие паузы, повторные напоминания, просрочки."""
    until = get_pause_until()

    if until not in (None, PAUSE_FOREVER) and datetime.now(TZ) >= until:
        delete_setting("paused_until")

        for user_id in [*USERS, OWNER_ID]:
            await safe_send(
                context,
                user_id,
                text="▶️ Пауза закончилась. Напоминания снова включены.",
            )

    if is_paused():
        return

    now = datetime.now(TZ)

    for row in get_pending_tasks():
        date, task_id = row["date"], row["task_id"]

        # 1) Повторное напоминание брату
        if not row["reminded"] and now >= remind_at_of(row):
            for user_id in USERS:
                await safe_send(
                    context,
                    user_id,
                    text=(
                        "🔔 *Напоминание*\n\n"
                        f"{row['task_name']}\n\n"
                        f"Задача пришла в {hhmm(row['sent_at'])} "
                        "и ещё не выполнена."
                    ),
                    parse_mode="Markdown",
                    reply_markup=task_keyboard(
                        date, task_id, row["snooze_count"] or 0
                    ),
                )

            # Помечаем даже если отправка не удалась — иначе будет спам
            set_task_flag(date, task_id, "reminded")

        # 2) Просрочка — обоим
        if not row["overdue_notified"] and now >= overdue_at_of(row):
            for user_id in [BROTHER_ID, OWNER_ID]:
                await safe_send(
                    context,
                    user_id,
                    text=(
                        "⚠️ *Просроченная задача*\n\n"
                        f"{row['task_name']}\n\n"
                        f"Запланировано: {hhmm(row['sent_at'])}\n"
                        "Задача ещё не выполнена."
                    ),
                    parse_mode="Markdown",
                )

            set_task_flag(date, task_id, "overdue_notified")


# =========================
# BUTTON HANDLER
# =========================

def parse_callback(data):
    """done:<date>:<task>, snooze:..., cant:..., back:..., why:<date>:<task>:<code>
    Старый формат: done:<task>"""
    parts = data.split(":")
    action = parts[0]

    if len(parts) == 2:
        return action, today_key(), parts[1], None
    if len(parts) == 3:
        return action, parts[1], parts[2], None
    if len(parts) == 4:
        return action, parts[1], parts[2], parts[3]

    return None, None, None, None


async def notify_owner_cant(context, row, user_id):
    if user_id == OWNER_ID:
        return

    await safe_send(
        context,
        OWNER_ID,
        text=(
            f"🚫 *{md(row['cant_by_name'])}* не может выполнить:\n\n"
            f"{row['task_name']}\n\n"
            f"Причина: {md(row['cant_reason'])}"
        ),
        parse_mode="Markdown",
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, date, task_id, extra = parse_callback(query.data or "")

    if task_id not in ALL_TASKS:
        await query.answer()
        return

    user = query.from_user
    user_name = user_display_name(user)
    task_name = ALL_TASKS[task_id]

    # ---------- ✅ Выполнено ----------
    if action == "done":
        await query.answer()

        row, just_completed = complete_task(task_id, user.id, user_name, date)

        if row is None or row["completed_at"] is None:
            await query.edit_message_text(
                f"⚠️ Не удалось найти задачу «{task_name}» в базе."
            )
            return

        completed_time = hhmm(row["completed_at"])

        await query.edit_message_text(
            "✅ *Выполнено*\n\n"
            f"{task_name}\n\n"
            f"👤 Выполнил: {md(row['completed_by_name'])}\n"
            f"🕐 Время: {completed_time}",
            parse_mode="Markdown",
        )

        # Мгновенное уведомление владельцу: только при первом нажатии
        # и только если выполнил не сам владелец
        if just_completed and user.id != OWNER_ID:
            test_mark = "🧪 (тест) " if is_test_task(task_id) else ""

            await safe_send(
                context,
                OWNER_ID,
                text=(
                    f"✅ {test_mark}*{md(row['completed_by_name'])}* выполнил:\n\n"
                    f"{task_name}\n\n"
                    f"📨 Напоминание: {hhmm(row['sent_at'])}\n"
                    f"🕐 Выполнено: {completed_time}"
                ),
                parse_mode="Markdown",
            )
        return

    # ---------- ⏰ Отложить ----------
    if action == "snooze":
        row, ok = snooze_task(date, task_id)

        if not ok:
            if row is not None and (row["completed_at"] or row["cant_at"]):
                text = "Эта задача уже закрыта."
            else:
                text = f"Больше откладывать нельзя (максимум {MAX_SNOOZES} раза)."
            await query.answer(text, show_alert=True)
            return

        await query.answer("Отложено")

        left = MAX_SNOOZES - row["snooze_count"]

        await query.edit_message_text(
            "⏰ *Отложено*\n\n"
            f"{task_name}\n\n"
            f"Напомню ещё раз в {hhmm(row['remind_at'])}.\n"
            f"Можно отложить ещё: {left} р.",
            parse_mode="Markdown",
        )
        return

    # ---------- ❌ Не могу → выбор причины ----------
    if action == "cant":
        await query.answer()
        await query.edit_message_reply_markup(
            reply_markup=reasons_keyboard(date, task_id)
        )
        return

    # ---------- ↩️ Назад ----------
    if action == "back":
        await query.answer()
        row = get_task(date, task_id)
        await query.edit_message_reply_markup(
            reply_markup=task_keyboard(
                date, task_id, (row["snooze_count"] or 0) if row else 0
            )
        )
        return

    # ---------- причина ----------
    if action == "why":
        await query.answer()

        if extra == "other":
            # Следующее текстовое сообщение будет причиной
            context.user_data["awaiting_reason"] = (date, task_id)

            await query.edit_message_text(
                "✍️ *Напиши причину одним сообщением*\n\n"
                f"{task_name}",
                parse_mode="Markdown",
            )
            return

        reason = CANT_REASONS.get(extra, "Без причины")
        row, ok = cant_task(date, task_id, user_name, reason)

        if not ok:
            await query.edit_message_text("Эта задача уже закрыта.")
            return

        await query.edit_message_text(
            "🚫 *Не получилось*\n\n"
            f"{task_name}\n\n"
            f"Причина: {md(reason)}\n"
            "Я передал Владу.",
            parse_mode="Markdown",
        )

        await notify_owner_cant(context, row, user.id)
        return

    await query.answer()


async def reason_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит текст причины после кнопки «Другая причина»."""
    pending = context.user_data.pop("awaiting_reason", None)

    if pending is None:
        await update.message.reply_text(
            "Не понял 🤔 Выбери действие в меню или напиши /help",
            reply_markup=MAIN_MENU,
        )
        return

    date, task_id = pending
    reason = update.message.text.strip()[:300]

    row, ok = cant_task(
        date, task_id, user_display_name(update.effective_user), reason
    )

    if not ok:
        await update.message.reply_text("Эта задача уже закрыта.")
        return

    await update.message.reply_text(
        f"🚫 Записал: «{reason}».\nЯ передал Владу.",
        reply_markup=MAIN_MENU,
    )

    await notify_owner_cant(context, row, update.effective_user.id)


# =========================
# COMMANDS — ДЛЯ ВСЕХ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐟 Привет!\n\n"
        "Я слежу за уходом за аквариумом.\n\n"
        "Каждый день я буду напоминать тебе о необходимых задачах. "
        "Кнопки меню внизу экрана, а все команды — в /help.",
        reply_markup=MAIN_MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "❓ Команды бота",
        "",
        "📋 Для всех:",
        "/today — задачи на сегодня и их статус",
        "/tomorrow — задачи на завтра и время",
        "/overdue — просроченные задачи",
        "/history — история за 7 дней",
        "/stats — статистика, серия 🔥 и достижения",
        "/id — твой Telegram ID",
        "/help — это сообщение",
        "",
        "🔘 Кнопки под задачей:",
        "✅ Выполнено — отметить задачу",
        f"⏰ Отложить — напомню через {int(SNOOZE_FOR.total_seconds() // 60)} мин "
        f"(до {MAX_SNOOZES} раз)",
        "❌ Не могу — выбрать причину, Влад получит уведомление",
    ]

    if is_owner(update):
        lines += [
            "",
            "🔧 Только для тебя (владелец):",
            "/schedule — всё расписание и названия задач",
            "/settime <задача> <ЧЧ:ММ> — поменять время",
            "   пример: /settime light_on 15:00",
            "/settime <задача> default — вернуть время по умолчанию",
            "/pause — поставить напоминания на паузу",
            "/pause <дни> — пауза на N дней, пример: /pause 3",
            "/resume — снять паузу",
            "/backup — прислать файл базы данных",
            "/test — отправить брату тестовую задачу",
            "/overdue_test — создать тестовую просроченную задачу",
            "",
            "📨 Автоматически тебе приходит:",
            "• уведомление, когда брат выполнил задачу или не может",
            f"• предупреждение о просрочке (через {int(OVERDUE_AFTER.total_seconds() // 3600)} ч)",
            f"• отчёт за день в {REPORT_TIME.strftime('%H:%M')}",
            f"• статистика за неделю по воскресеньям в {WEEKLY_STATS_TIME.strftime('%H:%M')}",
            "• сообщение при перезапуске бота",
        ]

    # Без parse_mode: в названиях команд есть «_», они ломают Markdown
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🆔 Твой Telegram ID:\n\n`{update.effective_user.id}`",
        parse_mode="Markdown",
    )


async def today(update, context):
    rows = get_today_tasks()

    lines = [
        "📋 *Аквариум сегодня*",
        f"📅 {datetime.now(TZ).strftime('%d.%m.%Y')}",
        "",
    ]

    if is_paused():
        lines += [f"⏸ Напоминания на паузе {md(pause_text())}", ""]

    if is_fasting_day(datetime.now(TZ).date()):
        lines += ["🚫 Разгрузочный день — рыбок не кормим", ""]

    if not rows:
        lines.append("Сегодня задач ещё не было.")
    else:
        completed_count = 0

        for row in rows:
            lines.append(format_task_row(row))
            lines.append("")
            if row["completed_at"]:
                completed_count += 1

        lines.append(f"📈 Выполнено: {completed_count}/{len(rows)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def tomorrow(update, context):
    """Показывает задачи брата на завтра по расписанию."""
    day = datetime.now(TZ).date() + timedelta(days=1)
    items = tasks_for_date(day)

    lines = [
        "🗓 *Задачи на завтра*",
        f"📅 {day.strftime('%d.%m.%Y')}, {WEEKDAYS_RU[day.weekday()]}",
        "",
    ]

    if is_paused():
        until = get_pause_until()
        start_of_day = datetime.combine(day, time(0, 0), tzinfo=TZ)
        if until == PAUSE_FOREVER or until > start_of_day:
            lines += [f"⏸ Напоминания на паузе {md(pause_text())}", ""]

    if is_fasting_day(day):
        lines.append(
            f"🚫 {feed_time().strftime('%H:%M')} — Разгрузочный день, НЕ кормить"
        )

    if not items:
        lines.append("Задач нет.")
    else:
        for run_time, task_id in items:
            lines.append(f"🕐 {run_time.strftime('%H:%M')} — {TASKS[task_id]}")

        lines.append("")
        lines.append(f"Всего: {len(items)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def overdue(update, context):
    """Показывает задачи, срок которых прошёл."""
    # Владельцу показываем и тестовые задачи — чтобы проверять /overdue_test
    rows = get_overdue_tasks(include_test=is_owner(update))

    if not rows:
        await update.message.reply_text("👍 Просроченных задач нет.")
        return

    now = datetime.now(TZ)
    lines = ["⚠️ *Просроченные задачи*", ""]

    for row in rows:
        sent_at = parse_dt(row["sent_at"])
        minutes = int((now - sent_at).total_seconds() // 60)
        hours, minutes = divmod(minutes, 60)

        lines.append(
            f"⏳ {row['task_name']}\n"
            f"   Запланировано: {sent_at.strftime('%H:%M')} "
            f"(прошло {hours} ч {minutes} мин)"
        )
        lines.append("")

    lines.append(f"Всего: {len(rows)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history(update, context):
    # Дата считается по времени Праги, а не по времени сервера (на Railway это UTC)
    week_ago = (datetime.now(TZ).date() - timedelta(days=6)).isoformat()

    exclude, params = exclude_test_sql()

    conn = get_db()
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
        await update.message.reply_text("📚 История пока пустая.")
        return

    lines = ["📚 *История аквариума*", "Последние 7 дней:", ""]

    current_date = None

    for row in rows:
        if row["date"] != current_date:
            current_date = row["date"]
            date_obj = datetime.fromisoformat(row["date"])
            lines.append(f"📅 *{date_obj.strftime('%d.%m.%Y')}*")

        lines.append(format_task_row(row, indent="  ", pending_icon="❌"))
        lines.append("")

    text = "\n".join(lines)

    # Ограничение Telegram — 4096 символов
    if len(text) > 4000:
        text = text[:4000].rsplit("\n", 1)[0] + "\n\n…"

    await update.message.reply_text(text, parse_mode="Markdown")


async def stats(update, context):
    await update.message.reply_text(build_stats_text(), parse_mode="Markdown")


# =========================
# COMMANDS — ТОЛЬКО ВЛАДЕЛЕЦ
# =========================

async def schedule_cmd(update, context):
    if not is_owner(update):
        return

    lines = ["🗓 *Расписание*", ""]

    for task_id, run_time, days in sorted(
        get_schedule(), key=lambda item: (item[1].hour, item[1].minute)
    ):
        lines.append(
            f"🕐 {run_time.strftime('%H:%M')} — {TASKS[task_id]}\n"
            f"   `{task_id}` · {days_text(days)}"
        )

    fasting = ", ".join(PTB_DAY_SHORT[d] for d in FASTING_DAYS)

    lines += [
        "",
        f"🚫 Разгрузочный день: {fasting}",
        f"📊 Отчёт: {REPORT_TIME.strftime('%H:%M')}",
        f"📈 Статистика: вс {WEEKLY_STATS_TIME.strftime('%H:%M')}",
    ]

    if is_paused():
        lines.append(f"⏸ Пауза {md(pause_text())}")

    lines += ["", "Поменять время: /settime `light_on` 15:00"]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def settime(update, context):
    if not is_owner(update):
        return

    usage = (
        "Использование:\n"
        "/settime <задача> <ЧЧ:ММ>\n"
        "/settime <задача> default\n\n"
        "Задачи: " + ", ".join(TASKS) + "\n"
        "Пример: /settime light_on 15:00"
    )

    if len(context.args) != 2:
        await update.message.reply_text(usage)
        return

    task_id, value = context.args[0].lower(), context.args[1].lower()

    if task_id not in TASKS:
        await update.message.reply_text(f"❌ Нет задачи «{task_id}».\n\n{usage}")
        return

    if value == "default":
        set_schedule_override(task_id, None)
    else:
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError:
            await update.message.reply_text(
                f"❌ Неверное время «{value}». Нужно ЧЧ:ММ, например 07:30"
            )
            return

        set_schedule_override(task_id, parsed.strftime("%H:%M"))

    setup_task_jobs(context.application)

    new_time = next(t for tid, t, _ in get_schedule() if tid == task_id)

    await update.message.reply_text(
        f"✅ {TASKS[task_id]} — теперь в {new_time.strftime('%H:%M')}"
        + (" (по умолчанию)" if value == "default" else "")
        + "\n\nПроверить: /schedule или /tomorrow"
    )


async def pause(update, context):
    if not is_owner(update):
        return

    if context.args:
        try:
            days = int(context.args[0])
            if days < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Укажи число дней, например: /pause 3\n"
                "Или просто /pause — до команды /resume"
            )
            return

        until = datetime.now(TZ) + timedelta(days=days)
        set_setting("paused_until", until.isoformat())
    else:
        set_setting("paused_until", PAUSE_FOREVER)

    text = f"⏸ Напоминания на паузе {pause_text()}."

    await update.message.reply_text(text + "\n\nСнять паузу: /resume")

    for user_id in USERS:
        await safe_send(context, user_id, text=text)


async def resume(update, context):
    if not is_owner(update):
        return

    if get_pause_until() is None:
        await update.message.reply_text("▶️ Паузы и так нет.")
        return

    delete_setting("paused_until")

    text = "▶️ Пауза снята. Напоминания снова включены."

    await update.message.reply_text(text)

    for user_id in USERS:
        await safe_send(context, user_id, text=text)


async def backup(update, context):
    if not is_owner(update):
        return

    # Копируем через sqlite backup API — так копия целостная,
    # даже если бот в этот момент пишет в базу
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        src = get_db()
        dst = sqlite3.connect(tmp_path)
        src.backup(dst)
        dst.close()
        src.close()

        stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H-%M")

        with open(tmp_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"aquarium_{stamp}.db",
                caption="💾 Резервная копия базы аквариума",
            )
    finally:
        os.remove(tmp_path)


async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    # Отдельная тестовая задача: не трогает настоящую «Покормить рыбок»
    delivered = await send_task(context, "test")

    if delivered:
        names = ", ".join(
            "Брат" if uid == BROTHER_ID else str(uid) for uid in delivered
        )
        await update.message.reply_text(f"🧪 Тестовая задача отправлена: {names}")
    else:
        await update.message.reply_text(
            "⚠️ Тестовая задача никому не дошла.\n"
            "Проверь, что брат нажал /start у бота."
        )


async def overdue_test(update, context):
    if not is_owner(update):
        return

    test_time = datetime.now(TZ) - timedelta(hours=2)

    conn = get_db()
    # remind_at / overdue_at = NULL → считаются от sent_at, т.е. уже в прошлом
    conn.execute(
        """
        INSERT OR REPLACE INTO tasks
        (date, task_id, task_name, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            today_key(),
            "overdue_test",
            TEST_TASKS["overdue_test"],
            test_time.isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🧪 Тестовая просроченная задача создана.\n"
        "Она считается отправленной 2 часа назад.\n\n"
        "В течение минуты брату придёт напоминание, "
        "а вам обоим — предупреждение о просрочке.\n"
        "Проверить список можно командой /overdue."
    )


# =========================
# MENU BUTTONS
# =========================

MENU_ACTIONS = {
    MENU_TODAY: today,
    MENU_TOMORROW: tomorrow,
    MENU_OVERDUE: overdue,
    MENU_HISTORY: history,
    MENU_STATS: stats,
    MENU_HELP: help_cmd,
}


async def menu_handler(update, context):
    handler = MENU_ACTIONS.get(update.message.text)
    if handler:
        await handler(update, context)


# =========================
# DAILY REPORT & WEEKLY STATS
# =========================

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    if is_paused():
        return

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
            lines.append(format_task_row(row, pending_icon="❌"))
            lines.append("")
            if row["completed_at"]:
                completed_count += 1

        lines.append(f"📈 Выполнено: {completed_count}/{len(rows)}")

    current, _ = compute_streaks()
    lines.append(f"🔥 Серия без пропусков: {current} дн.")

    await safe_send(
        context, OWNER_ID, text="\n".join(lines), parse_mode="Markdown"
    )

    # Достижения — обоим
    for streak in award_new_achievements():
        for user_id in [*USERS, OWNER_ID]:
            await safe_send(
                context,
                user_id,
                text=(
                    "🎉 *Новое достижение!*\n\n"
                    f"{ACHIEVEMENTS[streak]}\n\n"
                    "Так держать! 🐠"
                ),
                parse_mode="Markdown",
            )


async def weekly_stats(context: ContextTypes.DEFAULT_TYPE):
    text = build_stats_text()

    for user_id in [*USERS, OWNER_ID]:
        await safe_send(context, user_id, text=text, parse_mode="Markdown")


# =========================
# SCHEDULE
# =========================

TASK_JOB_PREFIX = "task:"
FASTING_JOB = "fasting_notice"


def setup_task_jobs(app):
    """(Пере)создаёт задачи по расписанию. Вызывается при старте и после /settime."""
    job_queue = app.job_queue

    for job in job_queue.jobs():
        if job.name and (job.name.startswith(TASK_JOB_PREFIX) or job.name == FASTING_JOB):
            job.schedule_removal()

    for task_id, run_time, days in get_schedule():
        job_queue.run_daily(
            scheduled_task,
            time=run_time,
            days=days,
            data=task_id,
            name=TASK_JOB_PREFIX + task_id,
        )

    # Напоминание «НЕ кормить» — в то же время, когда обычно кормление
    job_queue.run_daily(
        fasting_notice,
        time=feed_time(),
        days=FASTING_DAYS,
        name=FASTING_JOB,
    )


def setup_schedule(app):
    job_queue = app.job_queue

    setup_task_jobs(app)

    # Позже последней задачи (22:00) + час на выполнение + проверка просрочки в 23:00,
    # чтобы «Выключить воздух» успел попасть в отчёт с правильным статусом
    job_queue.run_daily(daily_report, time=REPORT_TIME, name="daily_report")

    job_queue.run_daily(
        weekly_stats,
        time=WEEKLY_STATS_TIME,
        days=(SUNDAY,),
        name="weekly_stats",
    )

    # Снятие паузы, повторные напоминания и просрочки — раз в минуту
    job_queue.run_repeating(
        check_pending_tasks,
        interval=60,
        first=60,
        name="pending_checker",
    )


# =========================
# STARTUP
# =========================

async def on_startup(app: Application):
    """Досылает задачи, пропущенные пока бот был выключен,
    и сообщает владельцу о перезапуске."""
    now = datetime.now(TZ)
    resent = []

    if not is_paused():
        for run_time, task_id in tasks_for_date(now.date()):
            planned = datetime.combine(
                now.date(), time(run_time.hour, run_time.minute), tzinfo=TZ
            )

            if not (planned <= now <= planned + MISSED_TASK_WINDOW):
                continue

            if get_task(now.date().isoformat(), task_id):
                continue  # уже отправляли

            note = (
                f"⚠️ Пришло с опозданием: должно было в "
                f"{planned.strftime('%H:%M')} (бот перезапускался)."
            )

            if await send_task(app, task_id, note=note):
                resent.append(f"{planned.strftime('%H:%M')} {TASKS[task_id]}")

    lines = [f"🔄 Бот перезапущен в {now.strftime('%H:%M')}"]

    if is_paused():
        lines.append(f"⏸ Напоминания на паузе {pause_text()}")
    elif resent:
        lines.append("\nДосланы пропущенные задачи:")
        lines += [f"• {item}" for item in resent]
    else:
        lines.append("Пропущенных задач нет.")

    await safe_send(app, OWNER_ID, text="\n".join(lines))


async def error_handler(update, context):
    logger.error("Ошибка при обработке апдейта", exc_info=context.error)


# =========================
# MAIN
# =========================

def main():
    init_db()

    app = (
        Application.builder()
        .token(os.environ["BOT_TOKEN"])
        .post_init(on_startup)
        .build()
    )

    commands = {
        "start": start,
        "help": help_cmd,
        "id": get_id,
        "today": today,
        "tomorrow": tomorrow,
        "overdue": overdue,
        "history": history,
        "stats": stats,
        # владелец
        "schedule": schedule_cmd,
        "settime": settime,
        "pause": pause,
        "resume": resume,
        "backup": backup,
        "test": test,
        "overdue_test": overdue_test,
    }

    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))

    app.add_handler(CallbackQueryHandler(button_handler))

    # Кнопки меню — раньше обработчика причин
    app.add_handler(MessageHandler(filters.Text(list(MENU_ACTIONS)), menu_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, reason_text_handler)
    )

    app.add_error_handler(error_handler)

    setup_schedule(app)

    logger.info("🐟 Aquarium Helper started")

    app.run_polling()


if __name__ == "__main__":
    main()
