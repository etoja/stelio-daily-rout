import os
import re
import requests
import urllib.parse
import telebot
from flask import Flask, request

# ==== КОНФИГ ====

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")

# Старт / финиш маршрута
BASE_POINT = "Метро Харківська, Київ"

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в переменных окружения")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)

# Города / локации, которые чаще всего у тебя встречаются в адресах
CITY_HINTS = [
    "Київ", "Киев",
    "Ірпінь", "Ирпень",
    "Гостомель", "Буча",
    "Чабани", "Крюківщина",
    "Білогородка", "Гнідин",
    "Крюковщина", "Святопетрівське",
    "Борщагівка"
]


def extract_addresses(text: str):
    """
    Из текста вытаскиваем строки, в которых есть название города из CITY_HINTS.
    Берём адрес от упоминания города до конца строки.
    Возвращаем список уникальных адресов в порядке появления.
    """
    addresses = []
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    city_pattern = re.compile(r"(" + "|".join(CITY_HINTS) + r")", re.IGNORECASE)

    for line in lines:
        m = city_pattern.search(line)
        if not m:
            continue

        addr = line[m.start():].strip()
        # Немного подчистить мусор
        addr = addr.replace("м.", "").replace("р.", "").strip(", ").strip()
        addresses.append(addr)

    # Уникализируем, но сохраняем порядок
    seen = set()
    result = []
    for a in addresses:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return result


def build_maps_url(base: str, waypoints: list[str]) -> str:
    """
    Строим ссылку вида:
    https://www.google.com/maps/dir/?api=1&travelmode=driving&origin=...&destination=...&waypoints=...
    Адреса передаём как обычный текст, urlencode делает кодирование один раз корректно.
    """
    params = {
        "api": "1",
        "travelmode": "driving",
        "origin": base,
        "destination": base,
    }

    if waypoints:
        # Живой текст адресов, разделённый "|"
        params["waypoints"] = "|".join(waypoints)

    # safe="|, " — не кодировать разделитель waypoints и запятые
    query = urllib.parse.urlencode(params, safe="|, ")

    return "https://www.google.com/maps/dir/?" + query


def get_distance_km(base: str, waypoints: list[str]) -> float:
    """
    Считаем дистанцию через Google Directions API.
    Возвращаем километры (одна цифра после запятой) или -1, если не получилось.
    """
    if not GOOGLE_API_KEY:
        print("WARNING: GOOGLE_MAPS_API_KEY не задан")
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
        # Пусть Google сам оптимизирует порядок точек
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
    """
    Любое текстовое сообщение → пробуем вытащить адреса.
    Если адреса найдены — отвечаем маршрутом.
    Если нет — молчим (чтобы бот не мешал в чате).
    """
    if not message.text:
        return

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
        lines.append("📏 Не вдалося порахувати дистанцію (немає API ключа або помилка).")

    bot.reply_to(message, "\n".join(lines))


# ==== FLASK + WEBHOOK ====


@app.route("/" + TELEGRAM_TOKEN, methods=["POST"])
def webhook():
    """
    Сюда Telegram шлёт апдейты (webhook).
    """
    update_json = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(update_json)
    bot.process_new_updates([update])
    return "OK", 200


@app.route("/", methods=["GET"])
def index():
    """
    Просто проверка, что сервис жив.
    """
    return "Bot is running", 200


if __name__ == "__main__":
    # Если Render прокинул внешний URL — ставим webhook автоматически
    base_url = os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        webhook_url = f"{base_url.rstrip('/')}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        bot.set_webhook(url=webhook_url)
        print("Webhook set to:", webhook_url)
    else:
        print("WARNING: RENDER_EXTERNAL_URL не задан, webhook нужно выставить вручную")

    port = int(os.environ.get("PORT", 5000))
    print(f"Bot started on port {port}...")
    app.run(host="0.0.0.0", port=port)
