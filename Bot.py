import asyncio
import logging
import os
import sys
import time
from threading import Thread
from flask import Flask
from typing import Callable, Dict, Any, Awaitable

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# ===== КОНФИГ =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "96266")
DB_PATH = "weather.db"

START_TIME = time.time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

# ===== FLASK =====
app = Flask(__name__)

@app.route("/")
def index():
    return "Weather bot is running"

def run_flask():
    try:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
    except Exception as e:
        logger.error(f"Flask error: {e}")

# ===== BOT =====
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ===== МЕНЮ =====
def main_menu():
    kb = [
        [KeyboardButton(text="🌤 Погода в Казани")],
        [KeyboardButton(text="🏙 Другой город")],
        [KeyboardButton(text="📍 Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_menu():
    kb = [
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="📋 Логи")],
        [KeyboardButton(text="🚫 Бан")],
        [KeyboardButton(text="🔙 Выйти")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

BUTTONS = {
    "🌤 Погода в Казани", "🏙 Другой город", "📍 Помощь",
    "📊 Статистика", "📢 Рассылка", "📋 Логи", "🚫 Бан", "🔙 Выйти",
}

user_states = {}

# ===== БД =====
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users
                            (user_id INTEGER PRIMARY KEY,
                             username TEXT, first_name TEXT, joined_at INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS logs
                            (id INTEGER PRIMARY KEY AUTOINCREMENT,
                             user_id INTEGER, city TEXT, ts INTEGER)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS banned
                            (user_id INTEGER PRIMARY KEY)''')
        await db.commit()

async def register_user(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, joined_at) VALUES (?, ?, ?, ?)",
            (user.id, user.username or "", user.first_name, int(time.time()))
        )
        await db.commit()

async def log_request(user_id: int, city: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO logs (user_id, city, ts) VALUES (?, ?, ?)",
            (user_id, city, int(time.time()))
        )
        await db.commit()

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

# ===== MIDDLEWARE (БАН) =====
class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        # Пропускаем /unban и /admin
        if isinstance(event, types.Message):
            text = event.text or ""
            if text.startswith("/unban") or text.startswith("/admin"):
                return await handler(event, data)

        # Проверяем бан
        if await is_banned(user.id):
            if isinstance(event, types.Message):
                await event.answer("🚫 Ты забанен. Доступ запрещён.")
            return

        return await handler(event, data)

dp.message.middleware(BanMiddleware())
dp.callback_query.middleware(BanMiddleware())

# ===== API =====
GEO_API = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_API = "https://api.open-meteo.com/v1/forecast"

async def get_coordinates(city: str):
    params = {"name": city, "count": 1, "language": "ru", "format": "json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GEO_API, params=params, timeout=10) as resp:
                data = await resp.json()
                if data.get("results"):
                    r = data["results"][0]
                    return {
                        "lat": r["latitude"],
                        "lon": r["longitude"],
                        "name": r["name"],
                        "country": r.get("country", ""),
                    }
    except Exception as e:
        logger.error(f"Геокодинг: {e}")
    return None

async def get_weather(lat: float, lon: float):
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto",
        "forecast_days": 3,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(WEATHER_API, params=params, timeout=10) as resp:
                return await resp.json()
    except Exception as e:
        logger.error(f"Погода: {e}")
    return None

def weather_emoji(code: int) -> str:
    if code == 0: return "☀️"
    if code in (1, 2, 3): return "⛅"
    if code in (45, 48): return "🌫️"
    if code in (51, 53, 55, 56, 57): return "🌦️"
    if code in (61, 63, 65, 66, 67): return "🌧️"
    if code in (71, 73, 75, 77): return "❄️"
    if code in (80, 81, 82): return "🌧️"
    if code in (85, 86): return "❄️"
    if code in (95, 96, 99): return "⛈️"
    return "🌡️"

def weather_description(code: int) -> str:
    d = {
        0: "Ясно", 1: "Малооблачно", 2: "Облачно", 3: "Пасмурно",
        45: "Туман", 48: "Изморозь",
        51: "Морось", 53: "Морось", 55: "Морось",
        61: "Дождь", 63: "Дождь", 65: "Ливень",
        71: "Снег", 73: "Снег", 75: "Снегопад",
        80: "Ливень", 81: "Ливень", 82: "Ливень",
        95: "Гроза", 96: "Гроза с градом", 99: "Гроза с градом",
    }
    return d.get(code, "Неизвестно")

async def send_weather(message: types.Message, city: str):
    msg = await message.answer(f"🔍 Ищу погоду в {city}...")
    coords = await get_coordinates(city)
    if not coords:
        await msg.edit_text(f"❌ Город '{city}' не найден.")
        return

    weather = await get_weather(coords["lat"], coords["lon"])
    if not weather or "current" not in weather:
        await msg.edit_text("❌ Не удалось получить погоду.")
        return

    c = weather["current"]
    d = weather["daily"]

    text = (
        f"🌍 {coords['name']}, {coords['country']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{weather_emoji(c['weather_code'])} {weather_description(c['weather_code'])}\n"
        f"🌡 Температура: {c['temperature_2m']}°C\n"
        f"🤔 Ощущается: {c['apparent_temperature']}°C\n"
        f"💧 Влажность: {c['relative_humidity_2m']}%\n"
        f"💨 Ветер: {c['wind_speed_10m']} км/ч\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 Прогноз на 3 дня:\n"
    )
    for i in range(min(3, len(d["time"]))):
        text += f"  {d['time'][i]}: {weather_emoji(d['weather_code'][i])} {d['temperature_2m_min'][i]}...{d['temperature_2m_max'][i]}°C\n"

    await msg.edit_text(text)
    await log_request(message.from_user.id, coords["name"])

# ===== ХЕНДЛЕРЫ =====
@dp.message(CommandStart())
async def start_cmd(message: types.Message):
    await register_user(message.from_user)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n"
        f"Я показываю погоду в Казани и других городах России.\n"
        f"Выбери действие:",
        reply_markup=main_menu()
    )

@dp.message(Command("weather"))
async def weather_cmd(message: types.Message):
    args = message.text.split(maxsplit=1)
    city = args[1].strip() if len(args) > 1 else "Казань"
    await send_weather(message, city)

@dp.message(F.text == "🌤 Погода в Казани")
async def weather_kazan(message: types.Message):
    await send_weather(message, "Казань")

@dp.message(F.text == "🏙 Другой город")
async def weather_other(message: types.Message):
    user_states[message.from_user.id] = "awaiting_city"
    await message.answer("Напиши название города (например, Москва):")

@dp.message(F.text == "📍 Помощь")
async def help_cmd(message: types.Message):
    await message.answer(
        "📖 Как пользоваться:\n"
        "• Нажми «🌤 Погода в Казани» — покажет погоду в Казани\n"
        "• Нажми «🏙 Другой город» — и напиши любой город\n"
        "• Или напиши /weather Москва\n\n"
        "Данные: Open-Meteo (бесплатно, без ключей)"
    )

# ===== АДМИНКА =====
@dp.message(Command("admin"))
async def admin_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Нет доступа.")
        return
    user_states[message.from_user.id] = "awaiting_password"
    await message.answer("🔐 Введи пароль:")

@dp.message(F.text == "🔙 Выйти")
async def exit_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    user_states.pop(message.from_user.id, None)
    await message.answer("Вышел из админки.", reply_markup=main_menu())

@dp.message(F.text == "📊 Статистика")
async def stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            users = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM logs") as cur:
            logs = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM banned") as cur:
            banned = (await cur.fetchone())[0]
        async with db.execute("SELECT city, COUNT(*) as c FROM logs GROUP BY city ORDER BY c DESC LIMIT 5") as cur:
            top = await cur.fetchall()

    uptime = int(time.time() - START_TIME)
    hours = uptime // 3600
    minutes = (uptime % 3600) // 60

    text = (
        f"📊 Статистика\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: {users}\n"
        f"🔍 Запросов: {logs}\n"
        f"🚫 Забанено: {banned}\n"
        f"⏱ Uptime: {hours}ч {minutes}мин\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏙 Топ городов:\n"
    )
    for t in top:
        text += f"  {t[0]} — {t[1]}\n"
    await message.answer(text)

@dp.message(F.text == "📢 Рассылка")
async def broadcast_prompt(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    user_states[message.from_user.id] = "broadcast"
    await message.answer("📢 Напиши текст рассылки:")

@dp.message(F.text == "📋 Логи")
async def logs(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, city, ts FROM logs ORDER BY id DESC LIMIT 30") as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("Логов нет.")
        return
    text = "📋 Последние 30 запросов:\n"
    for r in rows:
        dt = time.strftime("%d.%m %H:%M", time.localtime(r[2]))
        text += f"{dt} | user {r[0]} | {r[1]}\n"
    await message.answer(text)

@dp.message(F.text == "🚫 Бан")
async def ban_prompt(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    user_states[message.from_user.id] = "ban_user"
    await message.answer("🚫 Введи ID пользователя для бана (или /unban ID):")

@dp.message(Command("unban"))
async def unban(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.split()[1])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM banned WHERE user_id = ?", (uid,))
            await db.commit()
        await message.answer(f"✅ Пользователь {uid} разбанен.")
    except (IndexError, ValueError):
        await message.answer("Формат: /unban ID")

# ===== ВВОД =====
@dp.message(F.text & ~F.text.startswith("/") & ~F.text.in_(BUTTONS))
async def handle_input(message: types.Message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    # Пароль админа
    if state == "awaiting_password":
        if message.text.strip() == ADMIN_PASSWORD:
            user_states[user_id] = "admin"
            await message.answer("🔧 Админ-панель:", reply_markup=admin_menu())
        else:
            user_states.pop(user_id, None)
            await message.answer("❌ Неверный пароль.")
        return

    # Рассылка
    if state == "broadcast":
        user_states[user_id] = "admin"
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users") as cur:
                users = await cur.fetchall()
        sent = 0
        for u in users:
            try:
                await bot.send_message(u[0], message.text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await message.answer(f"✅ Отправлено: {sent}/{len(users)}", reply_markup=admin_menu())
        return

    # Бан
    if state == "ban_user":
        user_states[user_id] = "admin"
        try:
            uid = int(message.text.strip())
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
                await db.commit()
            await message.answer(f"✅ Пользователь {uid} забанен.", reply_markup=admin_menu())
        except ValueError:
            await message.answer("❌ ID должен быть числом.", reply_markup=admin_menu())
        return

    # Город
    if state == "awaiting_city":
        user_states.pop(user_id, None)
        await send_weather(message, message.text.strip())
        return

    # Если ничего не подошло — считаем городом
    await send_weather(message, message.text.strip())

# ===== MAIN =====
async def main():
    await init_db()
    Thread(target=run_flask, daemon=True).start()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Weather bot запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
