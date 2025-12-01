import os
import re
import requests
import urllib.parse
import telebot
from flask import Flask, request

# === КОНФИГ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
BASE_POINT = "Метро Харківська, Київ"

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

CITY_HINTS = [
    "Київ", "Киев", "Ірпінь", "Ирпень", "Гостомель", "Буча",
    "Чабани", "Крюківщина", "Білогородка", "Гнідин"
]


def extract_addresses(text: str):
    addresses = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    city_pattern = re.compile(r"(" + "|".join(CITY_HINTS) + r")", re.IGNORECASE)

    for line in lines:
        m = city_pattern.search(line)
        if not m:
            continue
        addr = line[m.start():].strip()
        addr = addr.replace("м.", "").replace("р.", "").strip(", ").strip()
        addresses.append(addr)

    seen = set()
    result = []
    for a in addresses:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


def build_maps_url(base: str, waypoints: list[str]) -> str:
    origin = urllib.parse.quote(base)
    destination = urllib.parse.quote(base)
    wp_encoded = [urllib.parse.quote(w) for w in waypoints]
    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&travelmode=driving"
        f"&origin={origin}&destination={destination}"
    )
    if waypoints:
        url += "&waypoints=" + "|".join(wp_encoded)
    return url


def get_distance_km(base: str, waypoints: list[str]) -> float:
    if not GOOGLE_API_KEY:
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
        print("Directions API error:", data.get("status"), data.get("error_message"))
        return -1

    route = data["routes"][0]
    legs = route.get("legs", [])
    meters = sum(leg["distance"]["value"] for leg in legs)
    km = round(meters / 1000.0, 1)
    return km


@bot.message_handler(func=lambda m: True)
def handle_route_message(message: telebot.types.Message):
    text = message.text
    addresses = extract_addresses(text)
    if not addresses:
        return

    maps_url = build_maps_url(BASE_POINT, addresses)
    distance_km = get_distance_km(BASE_POINT, addresses)

    lines = ["🚗 Маршрут на день (старт/фініш: м. Харківська):", ""]
    for i, addr in enumerate(addresses, start=1):
        lines.append(f"{i}) {addr}")
    lines.append("")
    lines.append(f"🔗 Маршрут: {maps_url}")

    if distance_km > 0:
        lines.append(f"📏 Дистанція: {distance_km} км")
    else:
        lines.append("📏 Не вдалося порахувати дистанцію (немає API ключа).")

    bot.reply_to(message, "\n".join(lines))


# === FLASK-ЧАСТЬ ДЛЯ WEBHOOK ===

@app.route("/" + TELEGRAM_TOKEN, methods=["POST"])
def webhook():
    update_json = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(update_json)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/", methods=["GET"])
def index():
    return "Bot is running", 200


if __name__ == "__main__":
    # URL сервиса на Render, например: https://route-bot.onrender.com
    # Render пробрасывает его в переменную окружения RENDER_EXTERNAL_URL
    base_url = os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        webhook_url = f"{base_url}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
    else:
        print("WARNING: RENDER_EXTERNAL_URL не задан, webhook не установится")

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
