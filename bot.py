import os
import re
import requests
import telebot
from flask import Flask, request
import urllib.parse
import sqlite3
from datetime import datetime, timedelta, timezone, date
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# === CONFIG ===

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Базовая точка по умолчанию (если не переопределена командой /setbase для чата)
DEFAULT_BASE_POINT = "Харківське шосе 19А, Київ"

DB_PATH = "routes.db"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Подсказки городов/локаций для поиска адресных строк
CITY_HINTS = [
    "Київ", "Киев",
    "Ірпінь", "Ирпень",
    "Гостомель", "Буча",
    "Чабани", "Крюківщина", "Крюковщина",
    "Білогородка", "Гнідин", "Святопетрівське",
    "Вишневе", "Солом‘янка", "Соломянка",
]


# === DB HELPERS ===

def init_db():
    """Создаем таблицы, если их ещё нет."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # Логи маршрутов
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                msg_timestamp INTEGER NOT NULL,
                distance_km REAL NOT NULL,
                raw_text TEXT
            )
            """
        )
        # Настройки чата (старт/финиш)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                chat_id INTEGER PRIMARY KEY,
                base_point TEXT NOT NULL
            )
            """
        )
        conn.commit()


def log_route(chat_id: int, msg_timestamp: int, distance_km: float, raw_text: str):
    """Сохраняем один маршрут в базу."""
    if distance_km <= 0:
        return
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO routes (chat_id, msg_timestamp, distance_km, raw_text) VALUES (?, ?, ?, ?)",
            (chat_id, msg_timestamp, distance_km, raw_text),
        )
        conn.commit()


def sum_distance_for_period(chat_id: int, start_ts: int, end_ts: int) -> float:
    """Сумма километров по чату за период [start_ts, end_ts]."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(distance_km), 0)
            FROM routes
            WHERE chat_id = ?
              AND msg_timestamp BETWEEN ? AND ?
            """,
            (chat_id, start_ts, end_ts),
        )
        row = cur.fetchone()
        return float(row[0] or 0.0)


def set_base_point(chat_id: int, base_point: str):
    """Сохраняем старт/финиш точку для конкретного чата."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settings (chat_id, base_point)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET base_point = excluded.base_point
            """,
            (chat_id, base_point),
        )
        conn.commit()


def get_base_point(chat_id: int) -> str:
    """Получаем старт/финиш точку для чата, если нет — возвращаем дефолтную."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT base_point FROM settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = cur.fetchone()
        if row:
            return row[0]
    return DEFAULT_BASE_POINT


# === ADDRESS EXTRACTION ===

def extract_addresses(text: str):
    """
    Извлекаем адресные строки:
    - либо содержат город из CITY_HINTS
    - либо содержат "вул./вулиця/ул./просп./шосе" + цифру (улица + дом)
    Если в строке нет города, подставляем ", Київ" по умолчанию.
    """
    addresses = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    street_re = re.compile(
        r"(вул\.|вулиця|улица|ул\.|просп\.|пр-т|проспект|шосе|ш\.)",
        re.IGNORECASE,
    )

    for line in lines:
        lower = line.lower()
        has_city = any(city.lower() in lower for city in CITY_HINTS)
        has_street = bool(street_re.search(line))
        has_number = bool(re.search(r"\d", line))

        if not (has_city or (has_street and has_number)):
            continue

        addr = line.strip()

        # если в строке нет города вообще, добавим ", Київ"
        if not any(city.lower() in addr.lower() for city in CITY_HINTS):
            addr = addr + ", Київ"

        addresses.append(addr)

    # Убираем дубли, сохраняем порядок
    result = []
    seen = set()
    for a in addresses:
        if a not in seen:
            seen.add(a)
            result.append(a)

    return result


# === URL BUILDER (кодируем для безопасной ссылки) ===

def encode_point(point: str) -> str:
    """
    Кодируем адрес для URL:
    пробелы и кириллица → %D0..., %20 и т.д.,
    чтобы Telegram видел ссылку как одно целое.
    """
    return urllib.parse.quote(point, safe="")


def build_maps_url(base: str, waypoints: list[str]) -> str:
    """
    Формат:
    https://www.google.com/maps/dir/POINT1/POINT2/.../POINTN
    где POINT* уже кодированы.
    """
    points = [base] + waypoints + [base]
    encoded_points = [encode_point(p) for p in points]
    path = "/".join(encoded_points)
    return "https://www.google.com/maps/dir/" + path


# === DISTANCE COUNTING ===

def get_distance_km(base: str, waypoints: list[str]) -> float:
    """Считаем дистанцию через Google Directions API (сырые строки, без encode_point)."""
    if not GOOGLE_API_KEY:
        print("Нет GOOGLE_MAPS_API_KEY!")
        return -1

    params = {
        "origin": base,
        "destination": base,
        "mode": "driving",
        "language": "uk",
        "region": "ua",
        "key": GOOGLE_API_KEY,
    }

    if waypoints:
        params["waypoints"] = "optimize:true|" + "|".join(waypoints)

    resp = requests.get(
        "https://maps.googleapis.com/maps/api/directions/json",
        params=params,
        timeout=10,
    )

    data = resp.json()

    if data.get("status") != "OK":
        print("Directions API error:", data)
        return -1

    meters = sum(leg["distance"]["value"] for leg in data["routes"][0]["legs"])
    return round(meters / 1000.0, 1)


# === HELPERS ДЛЯ ПЕРИОДОВ ===

def get_last_week_range():
    """Прошлая календарная неделя (Пн–Вс)."""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    prev_monday = this_monday - timedelta(days=7)
    prev_sunday = prev_monday + timedelta(days=6)
    return prev_monday, prev_sunday


def get_this_week_range():
    """Текущая неделя: с понедельника по сегодня."""
    today = datetime.now(timezone.utc).date()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday, today


def get_last_month_range():
    """Прошлый месяц: с 1-го по последний день предыдущего месяца."""
    today = datetime.now(timezone.utc).date()
    first_this_month = date(today.year, today.month, 1)
    last_prev_month = first_this_month - timedelta(days=1)
    first_prev_month = date(last_prev_month.year, last_prev_month.month, 1)
    return first_prev_month, last_prev_month


def get_this_month_range():
    """Текущий месяц: с 1-го по сегодня."""
    today = datetime.now(timezone.utc).date()
    first_this_month = date(today.year, today.month, 1)
    return first_this_month, today


def sum_for_date_range(chat_id: int, start_date: date, end_date: date) -> float:
    """Считает километраж за диапазон дат (по датам)."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=timezone.utc)
    return sum_distance_for_period(chat_id, int(start_dt.timestamp()), int(end_dt.timestamp()))


# === COMMANDS: /week, /thisweek, /period, /setbase, /report, /help ===

@bot.message_handler(commands=["week"])
def handle_week(message: telebot.types.Message):
    """
    /week — отчёт за прошлую календарную неделю (Пн–Вс) для этого чата.
    """
    chat_id = message.chat.id
    start_date, end_date = get_last_week_range()
    total_km = sum_for_date_range(chat_id, start_date, end_date)

    reply = (
        f"📆 Отчёт за прошлую неделю "
        f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
        f"🚗 Общий пробег: {round(total_km, 1)} км"
    )
    bot.reply_to(message, reply)


@bot.message_handler(commands=["thisweek"])
def handle_this_week(message: telebot.types.Message):
    """
    /thisweek — отчёт за текущую неделю (с понедельника по сегодня).
    """
    chat_id = message.chat.id
    start_date, end_date = get_this_week_range()
    total_km = sum_for_date_range(chat_id, start_date, end_date)

    reply = (
        f"📆 Отчёт за текущую неделю "
        f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
        f"🚗 Общий пробег: {round(total_km, 1)} км"
    )
    bot.reply_to(message, reply)


@bot.message_handler(commands=["period"])
def handle_period(message: telebot.types.Message):
    """
    /period YYYY-MM-DD YYYY-MM-DD
    Например:
    /period 2025-11-01 2025-11-30
    """
    chat_id = message.chat.id
    parts = message.text.strip().split()
    if len(parts) != 3:
        bot.reply_to(
            message,
            "Формат: /period YYYY-MM-DD YYYY-MM-DD\n"
            "Например: /period 2025-11-01 2025-11-30",
        )
        return

    try:
        start_date = datetime.strptime(parts[1], "%Y-%m-%d").date()
        end_date = datetime.strptime(parts[2], "%Y-%m-%d").date()
    except ValueError:
        bot.reply_to(message, "Неверный формат даты. Используй YYYY-MM-DD.")
        return

    if end_date < start_date:
        bot.reply_to(message, "Конечная дата раньше начальной 🤔")
        return

    total_km = sum_for_date_range(chat_id, start_date, end_date)

    reply = (
        f"📆 Отчёт за период {start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}:\n"
        f"🚗 Общий пробег: {round(total_km, 1)} км"
    )
    bot.reply_to(message, reply)


@bot.message_handler(commands=["setbase"])
def handle_set_base(message: telebot.types.Message):
    """
    /setbase НОВЫЙ АДРЕС
    Пример:
    /setbase Art Mall, ул. Заболотного 37, Киев
    """
    chat_id = message.chat.id
    parts = message.text.split(" ", 1)

    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            message,
            "Использование:\n\n"
            "/setbase Харківське шосе 19А, Київ\n"
            "/setbase Art Mall, ул. Заболотного 37, Киев",
        )
        return

    new_base = parts[1].strip()
    set_base_point(chat_id, new_base)

    bot.reply_to(
        message,
        f"✅ Новая точка старта/финиша установлена:\n{new_base}",
    )


@bot.message_handler(commands=["report"])
def handle_report(message: telebot.types.Message):
    """
    /report — показать кнопки выбора типового периода:
      - прошлая / текущая неделя
      - прошлый / текущий месяц
      - ручной ввод (/period)
    """
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("Прошлая неделя", callback_data="report:last_week"),
        InlineKeyboardButton("Текущая неделя", callback_data="report:this_week"),
    )
    markup.row(
        InlineKeyboardButton("Прошлый месяц", callback_data="report:last_month"),
        InlineKeyboardButton("Текущий месяц", callback_data="report:this_month"),
    )
    markup.row(
        InlineKeyboardButton("Ввести даты вручную", callback_data="report:manual"),
    )

    bot.reply_to(
        message,
        "Выбери период для отчёта:",
        reply_markup=markup,
    )


@bot.message_handler(commands=["help", "start"])
def handle_help(message: telebot.types.Message):
    text = (
        "📘 Команды бота:\n\n"
        "/week – отчёт за прошлую неделю\n"
        "/thisweek – отчёт за текущую неделю\n"
        "/period YYYY-MM-DD YYYY-MM-DD – отчёт за указанный период\n"
        "/report – меню выбора периода (кнопки)\n"
        "/setbase АДРЕС – изменить точку старта/финиша\n"
        "/help – показать это меню\n\n"
        "Чтобы посчитать маршрут: просто пришли сообщение с адресами, "
        "бот построит маршрут, посчитает километраж и сохранит в статистику 🚗"
    )
    bot.reply_to(message, text)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("report:"))
def handle_report_callback(call):
    chat_id = call.message.chat.id
    data = call.data.split(":", 1)[1]

    if data == "last_week":
        start_date, end_date = get_last_week_range()
        total_km = sum_for_date_range(chat_id, start_date, end_date)
        text = (
            f"📆 Отчёт за прошлую неделю "
            f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
            f"🚗 Общий пробег: {round(total_km, 1)} км"
        )
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(chat_id, text)

    elif data == "this_week":
        start_date, end_date = get_this_week_range()
        total_km = sum_for_date_range(chat_id, start_date, end_date)
        text = (
            f"📆 Отчёт за текущую неделю "
            f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
            f"🚗 Общий пробег: {round(total_km, 1)} км"
        )
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(chat_id, text)

    elif data == "last_month":
        start_date, end_date = get_last_month_range()
        total_km = sum_for_date_range(chat_id, start_date, end_date)
        text = (
            f"📆 Отчёт за прошлый месяц "
            f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
            f"🚗 Общий пробег: {round(total_km, 1)} км"
        )
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(chat_id, text)

    elif data == "this_month":
        start_date, end_date = get_this_month_range()
        total_km = sum_for_date_range(chat_id, start_date, end_date)
        text = (
            f"📆 Отчёт за текущий месяц "
            f"({start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}):\n"
            f"🚗 Общий пробег: {round(total_km, 1)} км"
        )
        bot.answer_callback_query(call.id, "Готово ✅")
        bot.send_message(chat_id, text)

    elif data == "manual":
        bot.answer_callback_query(call.id)
        bot.send_message(
            chat_id,
            "Пришли команду в формате:\n"
            "/period YYYY-MM-DD YYYY-MM-DD\n"
            "Например: /period 2025-11-01 2025-11-30",
        )


# === MAIN HANDLER ДЛЯ МАРШРУТОВ ===

@bot.message_handler(func=lambda m: True)
def handle_message(message: telebot.types.Message):
    # команды выше уже обработаны
    if message.text is None:
        return
    if message.text.startswith("/"):
        return

    addresses = extract_addresses(message.text)

    if not addresses:
        return  # если нет адресов — молчим

    base = get_base_point(message.chat.id)
    maps_url = build_maps_url(base, addresses)
    distance = get_distance_km(base, addresses)

    # логируем в базу
    log_route(
        chat_id=message.chat.id,
        msg_timestamp=message.date,  # unix timestamp от Telegram
        distance_km=distance,
        raw_text=message.text,
    )

    reply_lines = [f"🚗 Маршрут на день (старт/финиш: {base}):", ""]

    for i, a in enumerate(addresses, start=1):
        reply_lines.append(f"{i}) {a}")

    reply_lines.append("")
    reply_lines.append(f"🔗 Маршрут: {maps_url}")

    if distance > 0:
        reply_lines.append(f"📏 Дистанция: {distance} км")
    else:
        reply_lines.append("📏 Не удалось посчитать дистанцию.")

    text = "\n".join(reply_lines)
    bot.reply_to(message, text)


# === FLASK / WEBHOOK ===

@app.route("/" + TELEGRAM_TOKEN, methods=["POST"])
def telegram_webhook():
    update_json = request.data.decode("utf-8")
    update = telebot.types.Update.de_json(update_json)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


if __name__ == "__main__":
    init_db()

    base_url = os.getenv("RENDER_EXTERNAL_URL")

    if base_url:
        webhook_url = f"{base_url.rstrip('/')}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
    else:
        print("WARNING: RENDER_EXTERNAL_URL не задан. Надо поставить вебхук вручную.")

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
