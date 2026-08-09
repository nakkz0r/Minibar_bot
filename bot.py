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
SUPERVISORS = os.getenv("SUPERVISORS", "@BwpBatumiFO, @devadze_tamari, @ANNAMARIAAA24")

admins_env = os.getenv("ADMIN_IDS", "853815002")
ADMIN_IDS = [int(i.strip()) for i in admins_env.split(",") if i.strip().replace('-', '').isdigit()]

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

    cursor.execute("SELECT room_number, status, details FROM rooms ORDER BY room_number")
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return "პარამეტრები ჯერ არ არის შევსებული."
    
    summary_lines = []

    for row in rows:
        room, status, details = row[0], row[1], row[2]
        if status in ("ცარიელია", "ცარიელია სტუმარმა ითხოვა გამოტანა"):
            disp_status = "ცარიელია სტუმარმა ითხოვა გამოტანა"
            summary_lines.append(f"• <b>ნომერი {room}</b>: {disp_status}")
        elif status == "არ არის შევსებული" and details:
            summary_lines.append(f"• <b>ნომერი {room}</b>: {status} ({details})")
        else:
            summary_lines.append(f"• <b>ნომერი {room}</b>: {status}")

    return "\n".join(summary_lines)

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
    status_message = f"📊 <b>მიმდინარე მდგომარეობა (სრული სია):</b>\n{all_summary}"
    await message.answer(status_message, parse_mode="HTML")

@router.message(Command("audit"))
async def cmd_audit(message: Message, bot: Bot):
    user_id = message.from_user.id
    
    if user_id not in SUPERVISORS_IDS and user_id not in ADMIN_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ გთხოვთ, მიუთითოთ ნომერი. მაგალითად: <code>/audit 605</code>", parse_mode="HTML")
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
        audit_text = f"🔍 <b>აუდიტი ნომრისთვის {room_to_check}:</b>\n• სტატუსი: {status}\n• დეტალები: {details}"
    else:
        audit_text = f"🔍 <b>აუდიტი ნომრისთვის {room_to_check}:</b>\n• სტატუსი: {status}"

    if photo_id:
        try:
            await bot.send_photo(message.chat.id, photo=photo_id, caption=audit_text, parse_mode="HTML")
        except Exception:
            await message.answer(audit_text, parse_mode="HTML")
    else:
        await message.answer(audit_text, parse_mode="HTML")

@router.message(Command("product", "Product"))
async def cmd_product(message: Message):
    user_id = message.from_user.id
    
    if user_id not in SUPERVISORS_IDS and user_id not in ADMIN_IDS:
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
    total_present = {item_name: 0 for item_name in MINIBAR_CATALOG}

    for row in rows:
        status, json_str = row[1], row[3]
        if status == "შევსებულია":
            for item_name, max_qty in MINIBAR_CATALOG.items():
                total_present[item_name] += max_qty
        elif status == "არ არის შევსებული":
            missing_dict = {}
            if json_str:
                try:
                    missing_dict = json.loads(json_str)
                except Exception:
                    pass
            for item_name, max_qty in MINIBAR_CATALOG.items():
                missing_qty = missing_dict.get(item_name, 0)
                present_qty = max(0, max_qty - missing_qty)
                total_present[item_name] += present_qty
        else:
            # "ცარიელია სტუმარმა ითხოვა გამოტანა", "Out of order" -> 0 items present
            pass

    total_present_count = sum(total_present.values())

    msg = f"📦 <b>პროდუქციის ნაშთის ანგარიში (Наличие продукции в номерах)</b>\n\n"
    msg += f"🏨 შემოწმებული ნომრები (Проверено номеров): {total_rooms_checked}\n"
    msg += f"📊 <b>სულ არის ნომრებში (Всего в номерах): {total_present_count} шт.</b>\n\n"

    for item_name, qty in total_present.items():
        max_possible = MINIBAR_CATALOG[item_name] * total_rooms_checked
        msg += f"• <b>{item_name}</b>: {qty} / {max_possible} ცალი\n"

    await message.answer(msg, parse_mode="HTML")

@router.message(Command("clear", "reset"))
async def cmd_clear(message: Message):
    user_id = message.from_user.id
    
    if user_id not in ADMIN_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ ადმინისტრატორისთვის.")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM rooms")
    conn.commit()
    conn.close()

    # Синхронизируем очищенную базу с Cloudflare R2
    asyncio.create_task(r2.upload_db(DB_FILE))

    await message.answer("🗑️ <b>ყველა მონაცემი წარმატებით წაიშალა! (База данных номеров полностью очищена).</b>", parse_mode="HTML")

@router.message(ReportForm.room_number)
async def process_room(message: Message, state: FSMContext):
    await state.update_data(room=message.text)
    await state.set_state(ReportForm.status)
    
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="შევსებულია"), KeyboardButton(text="არ არის შევსებული")],
        [KeyboardButton(text="ცარიელია სტუმარმა ითხოვა გამოტანა"), KeyboardButton(text="Out of order")]
    ], resize_keyboard=True)
    
    await message.answer("აირჩიეთ ნომრის სტატუსი:", reply_markup=kb)

@router.message(ReportForm.status)
async def process_status(message: Message, state: FSMContext):
    status_text = message.text
    
    if status_text in ("ცარიელია", "ცარიელია სტუმარმა ითხოვა გამოტანა"):
        status_text = "ცარიელია სტუმარმა ითხოვა გამოტანა"
        await state.update_data(status=status_text)
        full_missing = MINIBAR_CATALOG.copy()
        details_list = [f"{k}: {v}" for k, v in full_missing.items()]
        details_text = f"გამოყენებულია: {', '.join(details_list)} (სულ {TOTAL_FULL_ITEMS} ცალი)"
        json_str = json.dumps(full_missing)
        
        await state.update_data(loss=details_text, json_details=json_str)
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())
    elif status_text == "არ არის შევსებული":
        await state.update_data(status=status_text)
        await state.set_state(ReportForm.loss)
        await state.update_data(selected_items={})
        kb = build_inventory_keyboard({})
        await message.answer(
            "📋 <b>მონიშნეთ რომელი პროდუქცია აკლია (выберите выпитое):</b>\n"
            "(Нажмите на товар, чтобы указать кол-во: 1 или 2)",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        await message.answer(
            "აირჩიეთ აკლებული პროდუქტები:",
            reply_markup=kb
        )
    elif "out of order" in status_text.lower():
        await state.update_data(status=status_text)
        await save_and_send_report(message, state, message.bot, photo_id=None)
    else:
        await state.update_data(status=status_text)
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

# Ручная передача наименований расхода текстом в стадии loss
@router.message(ReportForm.loss, F.text)
async def process_loss_text(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(loss=f"გამოყენებულია: {text}")
    await state.set_state(ReportForm.photo)
    await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:")

# Единая функция сохранения и отправки отчета
async def save_and_send_report(message: Message, state: FSMContext, bot: Bot, photo_id: str = None):
    data = await state.get_data()
    room = data.get('room', 'Н/Д')
    status = data.get('status', 'Н/Д')
    loss = data.get('loss', "")
    json_details = data.get('json_details', "")
    
    # 1. Сохранение в локальную SQLite
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO rooms (room_number, status, details, photo_id, json_details) VALUES (?, ?, ?, ?, ?)", 
        (room, status, loss, photo_id or "", json_details)
    )
    conn.commit()
    conn.close()
    
    # 2. Асинхронный бэкап базы данных в Cloudflare R2
    asyncio.create_task(r2.upload_db(DB_FILE))

    # 3. Асинхронное сохранение фотографии в Cloudflare R2
    if photo_id:
        try:
            photo_file = await bot.get_file(photo_id)
            photo_bytes_io = await bot.download_file(photo_file.file_path)
            if photo_bytes_io:
                photo_bytes = photo_bytes_io.read()
                asyncio.create_task(r2.upload_photo(photo_bytes, f"photos/room_{room}_{photo_id}.jpg"))
        except Exception as e:
            logging.error(f"Не удалось загрузить фото в R2: {e}")

    all_summary = get_all_rooms_summary()
    
    if loss and "ცარიელია" not in status:
        header_text = (
            f"📦 <b>მინი-ბარის ანგარიში</b>\n\n"
            f"🏨 ნომერი: {room}\n"
            f"🟢 სტატუსი: {status}\n"
            f"⚠️ {loss}\n"
            f"👤 თანამშრომელი: {message.from_user.full_name}"
        )
    else:
        header_text = (
            f"📦 <b>მინი-ბარის ანგარიში</b>\n\n"
            f"🏨 ნომერი: {room}\n"
            f"🟢 სტატუსი: {status}\n"
            f"👤 თანამშრომელი: {message.from_user.full_name}"
        )

    full_report = (
        f"{header_text}\n\n"
        f"📊 <b>მიმდინარე მდგომარეობა (სრული სია):</b>\n{all_summary}\n\n"
        f"🔔 <b>პასუხისმგებლები:</b> {SUPERVISORS}"
    )
    
    try:
        if photo_id:
            if len(full_report) <= 1000:
                await bot.send_photo(REPORT_CHAT_ID, photo=photo_id, caption=full_report, parse_mode="HTML")
            else:
                await bot.send_photo(REPORT_CHAT_ID, photo=photo_id, caption=header_text, parse_mode="HTML")
                summary_msg = (
                    f"📊 <b>მიმდინარე მდგომარეობა (სრული სია):</b>\n{all_summary}\n\n"
                    f"🔔 <b>პასუხისმგებლები:</b> {SUPERVISORS}"
                )
                await bot.send_message(REPORT_CHAT_ID, text=summary_msg, parse_mode="HTML")
        else:
            await bot.send_message(REPORT_CHAT_ID, text=full_report, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить отчет в чат {REPORT_CHAT_ID}: {e}")

    await message.answer("ანგარიში წარმატებით გაიგზავნა!", reply_markup=ReplyKeyboardRemove())
    await state.clear()

# Универсальный обработчик получения фото или документа-картинки
@router.message(ReportForm.loss, F.photo | F.document)
@router.message(ReportForm.photo, F.photo | F.document)
async def process_photo(message: Message, state: FSMContext, bot: Bot):
    photo_id = None
    if message.photo:
        photo_id = message.photo[-1].file_id
    elif message.document:
        photo_id = message.document.file_id
        
    await save_and_send_report(message, state, bot, photo_id)

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
