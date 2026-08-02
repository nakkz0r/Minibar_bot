import os
import io
import json
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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    BotCommand
)

from r2_storage import R2Storage

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ И СРЕДЫ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "8816528903:AAGQ5_RYSCg8O1OtLx6SwGQmz2PFzpA75b0")
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", "-5521647972"))
SUPERVISORS = os.getenv("SUPERVISORS", "@devadze_tamari, @BwpBatumiFO")

supervisors_env = os.getenv("SUPERVISORS_IDS", "853815002")
SUPERVISORS_IDS = [int(i.strip()) for i in supervisors_env.split(",") if i.strip().replace('-', '').isdigit()]

PORT = int(os.getenv("PORT", 8080))
DB_FILE = "minibar.db"

# --- ЭТАЛОННЫЙ СОСТАВ ПОЛНОГО МИНИ-БАРА (15 наименований, 19 единиц продукции) ---
MINIBAR_CATALOG = {
    "Cola Clasic": 2,
    "Cola Zero": 2,
    "Kit-Kat": 2,
    "Sandora Mini": 2,
    "XL": 1,
    "Chips": 1,
    "Nuts": 1,
    "Geo. Nuts": 1,
    "Borjomi": 1,
    "Wine": 1,
    "Kaiaki": 1,
    "Estrella": 1,
    "Whisky": 1,
    "Chacha": 1,
    "Brendy": 1
}
TOTAL_FULL_ITEMS = sum(MINIBAR_CATALOG.values())

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
            photo_id TEXT,
            json_details TEXT
        )
    """)
    # Миграция: проверка наличия колонки json_details
    cursor.execute("PRAGMA table_info(rooms)")
    columns = [row[1] for row in cursor.fetchall()]
    if "json_details" not in columns:
        cursor.execute("ALTER TABLE rooms ADD COLUMN json_details TEXT")
    conn.commit()
    conn.close()

def get_all_rooms_summary():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(rooms)")
    columns = [row[1] for row in cursor.fetchall()]
    if "json_details" not in columns:
        cursor.execute("ALTER TABLE rooms ADD COLUMN json_details TEXT")
        conn.commit()

    cursor.execute("SELECT room_number, status, details, json_details FROM rooms ORDER BY room_number")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "პარამეტრები ჯერ არ არის შევსებული."
    
    summary_lines = []
    total_restock = {}

    for row in rows:
        room, status, details, json_str = row[0], row[1], row[2], row[3]
        if details:
            summary_lines.append(f"• ნომერი {room}: {status} ({details})")
        else:
            summary_lines.append(f"• ნომერი {room}: {status}")

        if json_str:
            try:
                item_dict = json.loads(json_str)
                for item_name, qty in item_dict.items():
                    total_restock[item_name] = total_restock.get(item_name, 0) + qty
            except Exception:
                pass

    result = "\n".join(summary_lines)
    if total_restock:
        restock_items_str = "\n".join([f"  • {item}: {qty} ცალი" for item, qty in sorted(total_restock.items())])
        total_qty = sum(total_restock.values())
        result += f"\n\n📦 **სულ შესავსებია საწყობიდან (Всего для пополнения): {total_qty} шт.**\n{restock_items_str}"
    
    return result

# --- ВСПОМОГАТЕЛЬНЫЕ КНОПКИ ИНВЕНТАРИЗАЦИИ ---
def build_inventory_keyboard(selected_items: dict):
    keyboard = []
    items_list = list(MINIBAR_CATALOG.items())
    
    # Сетка по 2 кнопки в ряд
    for i in range(0, len(items_list), 2):
        row = []
        name1, max1 = items_list[i]
        val1 = selected_items.get(name1, 0)
        btn1_text = f"✅ {name1}: {val1}/{max1}" if val1 > 0 else f"{name1} ({max1})"
        row.append(InlineKeyboardButton(text=btn1_text, callback_data=f"item:{name1}"))
        
        if i + 1 < len(items_list):
            name2, max2 = items_list[i+1]
            val2 = selected_items.get(name2, 0)
            btn2_text = f"✅ {name2}: {val2}/{max2}" if val2 > 0 else f"{name2} ({max2})"
            row.append(InlineKeyboardButton(text=btn2_text, callback_data=f"item:{name2}"))
            
        keyboard.append(row)
        
    total_selected = sum(selected_items.values())
    confirm_text = f"✅ დადასტურება (არ არის: {total_selected} ცალი)" if total_selected > 0 else "✅ დადასტურება (ყველაფერი ადგილზეა)"
    keyboard.append([InlineKeyboardButton(text=confirm_text, callback_data="confirm_inventory")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
    cursor.execute("PRAGMA table_info(rooms)")
    columns = [row[1] for row in cursor.fetchall()]
    if "json_details" not in columns:
        cursor.execute("ALTER TABLE rooms ADD COLUMN json_details TEXT")
        conn.commit()

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

@router.message(Command("product", "Product"))
async def cmd_product(message: Message):
    user_id = message.from_user.id
    
    if user_id not in SUPERVISORS_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(rooms)")
    columns = [row[1] for row in cursor.fetchall()]
    if "json_details" not in columns:
        cursor.execute("ALTER TABLE rooms ADD COLUMN json_details TEXT")
        conn.commit()

    cursor.execute("SELECT room_number, status, details, json_details FROM rooms")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await message.answer("ℹ️ ბაზაში ჯერ არ არის მონაცემები ნომრების შესახებ.")
        return

    total_rooms_checked = len(rows)
    total_missing = {}

    for row in rows:
        json_str = row[3]
        if json_str:
            try:
                item_dict = json.loads(json_str)
                for item_name, qty in item_dict.items():
                    total_missing[item_name] = total_missing.get(item_name, 0) + qty
            except Exception:
                pass

    total_missing_count = sum(total_missing.values())

    msg = f"📦 **პროდუქციის ანგარიში (Отчет по продукции во всех мини-барах)**\n\n"
    msg += f"🏨 შემოწმებული ნომრები (Проверено номеров): {total_rooms_checked}\n\n"

    if total_missing_count == 0:
        msg += "✅ **ყველა მინი-ბარი სრულად შევსებულია! (Все мини-бары полные, израсходованных товаров нет).**"
    else:
        msg += f"⚠️ **სულ აკლია / შესავსებია (Всего израсходовано / к пополнению): {total_missing_count} шт.**\n\n"
        for item_name, max_per_room in MINIBAR_CATALOG.items():
            missing_qty = total_missing.get(item_name, 0)
            if missing_qty > 0:
                msg += f"• **{item_name}**: {missing_qty} ცალი\n"

    await message.answer(msg, parse_mode="Markdown")

@router.message(Command("clear", "reset"))
async def cmd_clear(message: Message):
    user_id = message.from_user.id
    
    if user_id not in SUPERVISORS_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM rooms")
    conn.commit()
    conn.close()

    # Синхронизируем очищенную базу с Cloudflare R2
    asyncio.create_task(r2.upload_db(DB_FILE))

    await message.answer("🗑️ **ყველა მონაცემი წარმატებით წაიშალა! (База данных номеров полностью очищена).**", parse_mode="Markdown")

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
        await state.update_data(selected_items={})
        kb = build_inventory_keyboard({})
        await message.answer(
            "📋 **მონიშნეთ რომელი პროდუქცია აკლია (выберите выпитое):**\n"
            "(Нажмите на товар, чтобы указать кол-во: 1 или 2)",
            reply_markup=ReplyKeyboardRemove()
        )
        await message.answer(
            "აირჩიეთ აკლებული პროდუქტები:",
            reply_markup=kb
        )
    elif status_text == "ცარიელია":
        full_missing = MINIBAR_CATALOG.copy()
        details_list = [f"{k}: {v}" for k, v in full_missing.items()]
        details_text = f"გამოყენებულია: {', '.join(details_list)} (სულ {TOTAL_FULL_ITEMS} ცალი)"
        json_str = json.dumps(full_missing)
        
        await state.update_data(loss=details_text, json_details=json_str)
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())
    else:
        await state.update_data(loss="", json_details="")
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())

@router.callback_query(ReportForm.loss, F.data.startswith("item:"))
async def process_item_toggle(callback: CallbackQuery, state: FSMContext):
    item_name = callback.data.split(":", 1)[1]
    if item_name not in MINIBAR_CATALOG:
        await callback.answer()
        return
        
    data = await state.get_data()
    selected = data.get("selected_items", {})
    
    max_qty = MINIBAR_CATALOG[item_name]
    curr_qty = selected.get(item_name, 0)
    new_qty = (curr_qty + 1) % (max_qty + 1)
    
    if new_qty == 0:
        selected.pop(item_name, None)
    else:
        selected[item_name] = new_qty
        
    await state.update_data(selected_items=selected)
    kb = build_inventory_keyboard(selected)
    
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await callback.answer()

@router.callback_query(ReportForm.loss, F.data == "confirm_inventory")
async def process_inventory_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_items", {})
    
    if selected:
        details_list = [f"{k}: {v}" for k, v in selected.items()]
        details_text = f"გამოყენებულია: {', '.join(details_list)}"
        json_str = json.dumps(selected)
    else:
        details_text = ""
        json_str = ""
        
    await state.update_data(loss=details_text, json_details=json_str)
    await state.set_state(ReportForm.photo)
    
    await callback.message.edit_text(
        f"✅ არჩევანი შენახულია!\n{details_text if details_text else '• ყველა პროდუქტი ადგილზეა.'}"
    )
    await callback.message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:")
    await callback.answer()

@router.message(ReportForm.photo, F.photo)
async def process_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    room = data['room']
    status = data['status']
    loss = data.get('loss', "")
    json_details = data.get('json_details', "")
    photo_id = message.photo[-1].file_id
    
    # 1. Сохранение в локальную SQLite
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO rooms (room_number, status, details, photo_id, json_details) VALUES (?, ?, ?, ?, ?)", 
        (room, status, loss, photo_id, json_details)
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
        BotCommand(command="audit", description="ნომრის აუდიტი (Аудит номера /audit <номер>)"),
        BotCommand(command="product", description="პროდუქციის ანგარიში (Отчет по продукции)"),
        BotCommand(command="clear", description="ბაზის გასუფთავება (Очистить базу данных)")
    ]
    await bot.set_my_commands(commands)
    
    logging.info("Starting Telegram Bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
