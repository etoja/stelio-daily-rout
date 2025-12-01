import os
import re
import requests
import telebot
from flask import Flask, request
import urllib.parse

# === CONFIG ===

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

BASE_POINT = "Метро Харківська, Київ"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан!")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

CITY_HINTS = [
    "Київ", "Киев",
    "Ірпінь", "Ирпень",
    "Гостомель", "Буча",
    "Чабани", "Крюківщина", "Крюковщина",
    "Білогородка", "Гнідин", "Святопетрівське",
    "Вишневе", "Солом‘янка"
]


# === ADDRESS EXTRACTION ===

def extract_addresses(text: str):
    """Извлекаем адреса из сообщения."""
    addresses = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    pattern = re.compile(r"(" + "|".join(CITY_HINTS) + r")", re.IGNORECASE)

    for line in lines:
        m = pattern.search(line)
        if not m:
            continue

        addr = line[m.start():].strip()
        addr = addr.replace("м.", "").replace("р.", "").strip(", ").strip()
        addresses.append(addr)

    # Убираем дубли, сохраняем порядок
    result = []
    seen = set()
    for a in addresses:
        if a not in seen:
            seen.add(a)
            result.append(a)

    return result


# === URL BUILDER (кодируем, чтобы НЕ было пробелов) ===

def encode_point(point: str) -> str:
    """
    Кодируем адрес для URL.
    Все пробелы и кириллица превращаются в %D0... и %20,
    чтобы Telegram видел ссылку как одно целое и не рвал её.
    """
    return urllib.parse.quote(point, safe="")  # ничего не оставляем «сырым»


def build_maps_url(base: str, waypoints: list[str]) -> str:
    """
    Формат:
    https://www.google.com/maps/dir/Точка1/Точка2/.../ТочкаN
    (но все точки уже процодированы encode_point)
    """
    points = [base] + waypoints + [base]
    encoded_points = [encode_point(p) for p in points]
    path = "/".join(encoded_points)
    return "https://www.google.com/maps/dir/" + path


# === DISTANCE COUNTING ===

def get_distance_km(base: str, waypoints: list[str]) -> float:
    """Считаем дистанцию через Google Directions API (НЕ кодированные строки!)."""
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
        timeout=10
    )

    data = resp.json()

    if data.get("status") != "OK":
        print("Directions API error:", data)
        return -1

    meters = sum(leg["distance"]["value"] for leg in data["routes"][0]["legs"])
    return round(meters / 1000.0, 1)


# === BOT HANDLER ===

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not message.text:
        return

    addresses = extract_addresses(message.text)

    if not addresses:
        return  # если нет адресов — молчим

    maps_url = build_maps_url(BASE_POINT, addresses)
    distance = get_distance_km(BASE_POINT, addresses)

    reply_lines = ["🚗 Маршрут на день (старт/фініш: м. Харківська):", ""]

    for i, a in enumerate(addresses, start=1):
        reply_lines.append(f"{i}) {a}")

    reply_lines.append("")
    # ТУТ уже закодированная строка без пробелов — Telegram не порвёт ссылку
    reply_lines.append(f"🔗 Маршрут: {maps_url}")

    if distance > 0:
        reply_lines.append(f"📏 Дистанція: {distance} км")
    else:
        reply_lines.append("📏 Не вдалося порахувати дистанцію.")

    text = "\n".join(reply_lines)
    bot.reply_to(message, text)  # БЕЗ parse_mode, обычный текст


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
    base_url = os.getenv("RENDER_EXTERNAL_URL")

    if base_url:
        webhook_url = f"{base_url.rstrip('/')}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
    else:
        print("WARNING: RENDER_EXTERNAL_URL не задан. Надо поставить вебхук вручную.")

    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
