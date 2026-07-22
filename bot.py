import os
import io
import logging
import asyncio
import sqlite3
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    ReplyKeyboardRemove,
    BotCommand
)

from r2_storage import R2Storage

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ И СРЕДЫ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "8816528903:AAGQ5_RYSCg8O1OtLx6SwGQmz2PFzpA75b0")
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", "-5521647972"))
SUPERVISORS = os.getenv("SUPERVISORS", "@nakkz0r")

supervisors_env = os.getenv("SUPERVISORS_IDS", "853815002")
SUPERVISORS_IDS = [int(i.strip()) for i in supervisors_env.split(",") if i.strip().replace('-', '').isdigit()]

PORT = int(os.getenv("PORT", 8080))
DB_FILE = "minibar.db"

# Инициализация хранилища Cloudflare R2
r2 = R2Storage()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            room_number TEXT PRIMARY KEY,
            status TEXT,
            details TEXT,
            photo_id TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_all_rooms_summary():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT room_number, status, details FROM rooms ORDER BY room_number")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "პარამეტრები ჯერ არ არის შევსებული."
    
    summary_lines = []
    for row in rows:
        room, status, details = row[:3]
        if details:
            summary_lines.append(f"• ნომერი {room}: {status} ({details})")
        else:
            summary_lines.append(f"• ნომერი {room}: {status}")
    
    return "\n".join(summary_lines)

# --- FSM СТАДИИ ---
class ReportForm(StatesGroup):
    room_number = State()
    status = State()
    loss = State()
    photo = State()

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(ReportForm.room_number)
    await message.answer("გამარჯობა! გთხოვთ, შეიყვანეთ ნომრის ნომერი (მაგალითად: 605):")

@router.message(Command("status"))
async def cmd_status(message: Message):
    all_summary = get_all_rooms_summary()
    status_message = f"📊 **მიმდინარე მდგომარეობა (სრული სია):**\n{all_summary}"
    await message.answer(status_message)

@router.message(Command("audit"))
async def cmd_audit(message: Message, bot: Bot):
    user_id = message.from_user.id
    
    if user_id not in SUPERVISORS_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ გთხოვთ, მიუთითოთ ნომერი. მაგალითად: `/audit 605`", parse_mode="Markdown")
        return
    
    room_to_check = args[1].strip()

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT status, details, photo_id FROM rooms WHERE room_number = ?", (room_to_check,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        await message.answer(f"❌ ნომერი {room_to_check} ბაზაში ვერ მოიძებნა ან ჯერ არ შემოწმებულა.")
        return

    status, details, photo_id = row
    if details:
        audit_text = f"🔍 **აუდიტი ნომრისთვის {room_to_check}:**\n• სტატუსი: {status}\n• დეტალები: {details}"
    else:
        audit_text = f"🔍 **აუდიტი ნომრისთვის {room_to_check}:**\n• სტატუსი: {status}"

    if photo_id:
        await bot.send_photo(message.chat.id, photo=photo_id, caption=audit_text)
    else:
        await message.answer(audit_text)

@router.message(ReportForm.room_number)
async def process_room(message: Message, state: FSMContext):
    await state.update_data(room=message.text)
    await state.set_state(ReportForm.status)
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="შევსებულია"), KeyboardButton(text="არ არის შევსებული")],
        [KeyboardButton(text="ცარიელია"), KeyboardButton(text="Out of order")]
    ], resize_keyboard=True)
    
    await message.answer("აირჩიეთ ნომრის სტატუსი:", reply_markup=kb)

@router.message(ReportForm.status)
async def process_status(message: Message, state: FSMContext):
    status_text = message.text
    await state.update_data(status=status_text)
    
    if status_text == "არ არის შევსებული":
        await state.set_state(ReportForm.loss)
        await message.answer("მიუთითეთ რა არის გამოყენებული (მაგალითად: 2 Kit-Kat, 1 Coca-Cola):", reply_markup=ReplyKeyboardRemove())
    else:
        await state.update_data(loss="")
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())

@router.message(ReportForm.loss)
async def process_loss(message: Message, state: FSMContext):
    await state.update_data(loss=message.text)
    await state.set_state(ReportForm.photo)
    await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:")

@router.message(ReportForm.photo, F.photo)
async def process_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    room = data['room']
    status = data['status']
    loss = data.get('loss', "")
    photo_id = message.photo[-1].file_id
    
    details_text = f"გამოყენებულია: {loss}" if loss else ""
    
    # 1. Сохранение в локальную SQLite
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO rooms (room_number, status, details, photo_id) VALUES (?, ?, ?, ?)", 
        (room, status, details_text, photo_id)
    )
    conn.commit()
    conn.close()
    
    # 2. Асинхронный бэкап базы данных в Cloudflare R2
    asyncio.create_task(r2.upload_db(DB_FILE))

    # 3. Асинхронное сохранение фотографии в Cloudflare R2
    try:
        photo_file = await bot.get_file(photo_id)
        photo_bytes_io = await bot.download_file(photo_file.file_path)
        if photo_bytes_io:
            photo_bytes = photo_bytes_io.read()
            asyncio.create_task(r2.upload_photo(photo_bytes, f"photos/room_{room}_{photo_id}.jpg"))
    except Exception as e:
        logging.error(f"Не удалось загрузить фото в R2: {e}")

    all_summary = get_all_rooms_summary()
    
    if loss:
        report_text = (
            f"📦 **მინი-ბარის ანგარიში**\n\n"
            f"🏨 ნომერი: {room}\n"
            f"🟢 სტატუსი: {status}\n"
            f"⚠️ გამოყენებულია: {loss}\n"
            f"👤 თანამშრომელი: {message.from_user.full_name}\n\n"
            f"📊 **მიმდინარე მდგომარეობა (სრული სია):**\n{all_summary}\n\n"
            f"🔔 **პასუხისმგებლები:** {SUPERVISORS}"
        )
    else:
        report_text = (
            f"📦 **მინი-ბარის ანგარიში**\n\n"
            f"🏨 ნომერი: {room}\n"
            f"🟢 სტატუსი: {status}\n"
            f"👤 თანამშრომელი: {message.from_user.full_name}\n\n"
            f"📊 **მიმდინარე მდგომარეობა (სრული სია):**\n{all_summary}\n\n"
            f"🔔 **პასუხისმგებლები:** {SUPERVISORS}"
        )
    
    await bot.send_photo(REPORT_CHAT_ID, photo=photo_id, caption=report_text)
    await message.answer("ანგარიში წარმატებით გაიგზავნა!")
    await state.clear()

# --- HTTP СЕРВЕР ДЛЯ RENDER И UPTIMEROBOT ---
async def handle_healthcheck(request):
    return web.Response(text="Minibar Bot is running!", status=200)

async def start_healthcheck_server(port: int):
    app = web.Application()
    app.router.add_get('/', handle_healthcheck)
    app.router.add_get('/health', handle_healthcheck)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Healthcheck HTTP server successfully started on port {port}")

# --- ЗАПУСК БОТА ---
async def main():
    # 1. Восстановление БД из R2 при запуске
    logging.info("Checking for database backup in Cloudflare R2...")
    await r2.download_db(DB_FILE)
    
    # 2. Инициализация локальной таблицы
    init_db()

    # 3. Запуск встроенного HTTP-сервера для Render & UptimeRobot
    await start_healthcheck_server(PORT)

    bot = Bot(token=TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    
    commands = [
        BotCommand(command="start", description="შემოწმების დაწყება (Начать проверку)"),
        BotCommand(command="status", description="ყველა ნომრის სტატუსი (Общий статус)"),
        BotCommand(command="audit", description="ნომრის აუდიტი (Аудит номера /audit <номер>)")
    ]
    await bot.set_my_commands(commands)
    
    logging.info("Starting Telegram Bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
