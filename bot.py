# =============================================================
#  Minibar Bot — версия 4
#
#  v1 (код-ревью): секреты в .env, экранирование HTML, валидация ввода,
#     /cancel, надёжные бэкапы R2.
#  v2: эмодзи-статусы 🟢🟡🔴🚧, /help,
#     каталог в базе (/catalog, /additem, /setqty, /renameitem, /delitem).
#  v3: цены и счёт гостя (/setprice),
#     автоматизация смены (AUTO_CLEAR_TIME),
#     защита от перезаписи номера, /myid, ошибки админам в личку,
#     /photos.
#  v4:
#   • Главное меню кнопками (по ролям) — команды набирать не обязательно
#   • В сводке по каждому номеру видно, ЧЕГО НЕ ХВАТАЕТ (что доложить)
#   • /product упрощён: просто список, что сейчас есть в номерах
#   • Закрытие смены подтверждается вводом слова: წაშლა / УДАЛИТЬ
# =============================================================

import os
import re
import sys
import json
import html
import time
import logging
import asyncio
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
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
    BotCommand,
    ErrorEvent,
    FSInputFile,
    InputMediaPhoto,
)

from r2_storage import R2Storage

# --- НАСТРОЙКИ ЛОГИРОВАНИЯ И СРЕДЫ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
load_dotenv()


def _require_env(name: str) -> str:
    """Обязательная переменная окружения. Без неё бот не стартует."""
    value = (os.getenv(name) or "").strip()
    if not value:
        logging.critical(
            f"Переменная окружения {name} не задана! "
            f"Заполните .env (см. .env.example) или настройки хостинга."
        )
        sys.exit(1)
    return value


def _parse_ids(env_name: str) -> list:
    """Разбор списка Telegram ID через запятую."""
    ids = []
    for part in (os.getenv(env_name) or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_hhmm(env_name: str):
    """Время вида ЧЧ:ММ из переменной окружения; пусто/неверно → выключено."""
    value = (os.getenv(env_name) or "").strip()
    if not value:
        return None
    if not TIME_RE.fullmatch(value):
        logging.warning(f"{env_name}='{value}' — неверный формат (нужно ЧЧ:ММ). Функция отключена.")
        return None
    return value


TOKEN = _require_env("BOT_TOKEN")

try:
    REPORT_CHAT_ID = int(_require_env("REPORT_CHAT_ID"))
except ValueError:
    logging.critical("REPORT_CHAT_ID должен быть числом (ID чата, например -1001234567890).")
    sys.exit(1)

ADMIN_IDS = _parse_ids("ADMIN_IDS")
SUPERVISORS_IDS = _parse_ids("SUPERVISORS_IDS")

if not ADMIN_IDS:
    logging.warning("ADMIN_IDS не задан — админ-команды будут недоступны никому.")
if not SUPERVISORS_IDS:
    logging.warning("SUPERVISORS_IDS не задан — команды супервайзеров будут недоступны никому.")

# Автоматизация смены (время Тбилиси). Пусто — выключено.
AUTO_CLEAR_TIME = _parse_hhmm("AUTO_CLEAR_TIME")     # напр. 06:00 — архив + очистка базы

try:
    PORT = int(os.getenv("PORT", "8080"))
except ValueError:
    PORT = 8080

DB_FILE = "minibar.db"

# Грузия живёт по постоянному UTC+4 (без перевода часов)
TBILISI_TZ = timezone(timedelta(hours=4))

# Номер комнаты: 1–4 цифры и необязательная буква (605, 1201, 605A).
ROOM_RE = re.compile(r"^\d{1,4}[A-Za-z]?$")
PRICE_RE = re.compile(r"^\d{1,4}([.,]\d{1,2})?$")

# Лимит Telegram — 4096 символов; берём с запасом
MAX_MSG_LEN = 4000
MAX_CAPTION_LEN = 1000

CURRENCY = "₾"

# --- ЭТАЛОННЫЙ СОСТАВ МИНИ-БАРА ---
DEFAULT_CATALOG = {
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

STATUS_FULL = "შევსებულია"
STATUS_NOT_FULL = "არ არის შევსებული"
STATUS_EMPTY = "ცარიელია სტუმარმა ითხოვა გამოტანა"

# Инициализация хранилища Cloudflare R2
r2 = R2Storage()

# Ссылки на фоновые задачи, чтобы их не убрал сборщик мусора
_bg_tasks = set()
# Бэкапы базы делаем по одному за раз
_db_backup_lock = asyncio.Lock()
# Троттлинг уведомлений об ошибках админам
_last_error_dm = {"ts": 0.0}


def esc(text) -> str:
    """Экранирование HTML: '<', '>', '&' в данных пользователей не ломают разметку."""
    return html.escape(str(text if text is not None else ""))


def now_tbilisi() -> str:
    return datetime.now(TBILISI_TZ).strftime("%d.%m.%Y %H:%M")


def fmt_money(amount: float) -> str:
    return f"{amount:.2f} {CURRENCY}"


def status_emoji(status: str) -> str:
    """Цветовой индикатор статуса номера."""
    status = status or ""
    if status == STATUS_FULL:
        return "🟢"
    if status == STATUS_NOT_FULL:
        return "🟡"
    if status in ("ცარიელია", STATUS_EMPTY):
        return "🔴"
    if "out of order" in status.lower():
        return "🚧"
    return "⚪"


# --- РАБОТА С БАЗОЙ ДАННЫХ ---
def db_connect():
    return sqlite3.connect(DB_FILE, timeout=10)


def init_db():
    """Создание таблиц и миграции. Вызывается один раз при старте."""
    with closing(db_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_number TEXT PRIMARY KEY,
                status TEXT,
                details TEXT,
                photo_id TEXT,
                json_details TEXT,
                photo_type TEXT,
                checked_by TEXT,
                checked_at TEXT
            )
        """)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(rooms)")}
        for column in ("json_details", "photo_type", "checked_by", "checked_at"):
            if column not in existing:
                conn.execute(f"ALTER TABLE rooms ADD COLUMN {column} TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                qty INTEGER NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0
            )
        """)
        cat_cols = {row[1] for row in conn.execute("PRAGMA table_info(catalog)")}
        if "price" not in cat_cols:
            conn.execute("ALTER TABLE catalog ADD COLUMN price REAL NOT NULL DEFAULT 0")

        count = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT INTO catalog (name, qty, position) VALUES (?, ?, ?)",
                [(name, qty, pos) for pos, (name, qty) in enumerate(DEFAULT_CATALOG.items(), start=1)]
            )
            logging.info("Каталог товаров заполнен эталонным составом (первый запуск).")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                closed_at TEXT,
                rooms_checked INTEGER,
                total_units INTEGER,
                total_amount REAL,
                consumed_json TEXT,
                staff_json TEXT
            )
        """)
        conn.commit()


def get_catalog() -> dict:
    """Текущий каталог: {название: эталонное количество}, в заданном порядке."""
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT name, qty FROM catalog ORDER BY position, id").fetchall()
    return {name: qty for name, qty in rows}


def get_catalog_with_ids() -> list:
    """[(id, название, количество, цена), ...] — для кнопок и /catalog."""
    with closing(db_connect()) as conn:
        return conn.execute("SELECT id, name, qty, price FROM catalog ORDER BY position, id").fetchall()


def get_prices() -> dict:
    """{название: цена}."""
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT name, price FROM catalog").fetchall()
    return {name: (price or 0) for name, price in rows}


def calc_charge(json_str: str, prices: dict) -> float:
    """Сумма к оплате гостем по JSON расхода комнаты."""
    if not json_str:
        return 0.0
    try:
        data = json.loads(json_str)
    except Exception:
        return 0.0
    total = 0.0
    for name, qty in data.items():
        try:
            total += float(prices.get(name, 0) or 0) * int(qty)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def room_charge(status: str, json_str: str, catalog: dict, prices: dict) -> float:
    if json_str:
        return calc_charge(json_str, prices)
    if status in ("ცარიელია", STATUS_EMPTY):
        return round(sum(float(prices.get(name, 0) or 0) * qty for name, qty in catalog.items()), 2)
    return 0.0


def _format_missing(json_str: str, details: str) -> str:
    """Краткий список «чего не хватает» по комнате: 'Wine 1, Cola 2'."""
    if json_str:
        try:
            data = json.loads(json_str)
            if data:
                return ", ".join(f"{name} {qty}" for name, qty in data.items())
        except Exception:
            pass
    if details:
        text = details.strip()
        if text.startswith("გამოყენებულია:"):
            text = text[len("გამოყენებულია:"):].strip()
        return text
    return ""


def get_all_rooms_summary() -> str:
    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT room_number, status, details, json_details FROM rooms "
            "ORDER BY CAST(room_number AS INTEGER), room_number"
        ).fetchall()

    if not rows:
        return "ჯერ არცერთი ნომერი არ არის შემოწმებული."

    total_units = sum(get_catalog().values())
    counts = {"🟢": 0, "🟡": 0, "🔴": 0, "🚧": 0, "⚪": 0}
    summary_lines = []
    for room, status, details, json_str in rows:
        emoji = status_emoji(status)
        counts[emoji] += 1
        if status in ("ცარიელია", STATUS_EMPTY):
            summary_lines.append(
                f"{emoji} <b>{esc(room)}</b> — აკლია: სრული მინი-ბარის პროდუქცია ({total_units} ერთ.)"
            )
        elif status == STATUS_NOT_FULL:
            missing = _format_missing(json_str, details)
            if missing:
                summary_lines.append(f"{emoji} <b>{esc(room)}</b> — აკლია: {esc(missing)}")
            else:
                summary_lines.append(f"{emoji} <b>{esc(room)}</b> — {esc(status)}")
        else:
            summary_lines.append(f"{emoji} <b>{esc(room)}</b> — {esc(status)}")

    counter_parts = [f"{emoji} {n}" for emoji, n in counts.items() if n > 0]
    header = f"<b>{' · '.join(counter_parts)}</b> — სულ: {len(rows)}"
    return header + "\n\n" + "\n".join(summary_lines)


def aggregate_present(rows, catalog: dict):
    total_present = {item_name: 0 for item_name in catalog}
    manual_rooms = 0

    for status, json_str, details in rows:
        if status == STATUS_FULL:
            for item_name, max_qty in catalog.items():
                total_present[item_name] += max_qty
        elif status == STATUS_NOT_FULL:
            missing_dict = {}
            if json_str:
                try:
                    missing_dict = json.loads(json_str)
                except Exception:
                    pass
            if not missing_dict and details:
                manual_rooms += 1
                continue
            for item_name, max_qty in catalog.items():
                missing_qty = missing_dict.get(item_name, 0)
                if not isinstance(missing_qty, int):
                    missing_qty = 0
                total_present[item_name] += max(0, max_qty - missing_qty)
        else:
            pass

    return total_present, manual_rooms


def aggregate_consumed(rows, catalog: dict):
    consumed = {item_name: 0 for item_name in catalog}
    manual_rooms = 0

    for status, json_str, details in rows:
        if status in ("ცარიელია", STATUS_EMPTY):
            for item_name, max_qty in catalog.items():
                consumed[item_name] += max_qty
        elif status == STATUS_NOT_FULL:
            missing_dict = {}
            if json_str:
                try:
                    missing_dict = json.loads(json_str)
                except Exception:
                    pass
            if not missing_dict and details:
                manual_rooms += 1
                continue
            for item_name, max_qty in catalog.items():
                missing_qty = missing_dict.get(item_name, 0)
                if not isinstance(missing_qty, int):
                    missing_qty = 0
                consumed[item_name] += min(max_qty, max(0, missing_qty))

    return consumed, manual_rooms


# --- РАЗБИВКА ДЛИННЫХ СООБЩЕНИЙ ---
def split_text(text: str, limit: int = MAX_MSG_LEN) -> list:
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


async def answer_chunked(message: Message, text: str, reply_markup=None, **kwargs):
    parts = split_text(text)
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=reply_markup if i == len(parts) - 1 else None, **kwargs)


async def send_chunked(bot: Bot, chat_id: int, text: str, **kwargs):
    for part in split_text(text):
        await bot.send_message(chat_id, part, **kwargs)


# --- БЭКАПЫ В CLOUDFLARE R2 ---
def _make_db_snapshot(snapshot_path: str):
    src = sqlite3.connect(DB_FILE, timeout=10)
    try:
        dst = sqlite3.connect(snapshot_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def backup_db_to_r2(object_name: str = None) -> bool:
    if not r2.enabled:
        return False
    async with _db_backup_lock:
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            await asyncio.to_thread(_make_db_snapshot, tmp_path)
            return await r2.upload_db(tmp_path, object_name)
        except Exception as e:
            logging.error(f"Не удалось сделать бэкап БД в R2: {e}")
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def schedule_bg(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# --- ЗАКРЫТИЕ СМЕНЫ И ПЛАНИРОВЩИК ---
async def close_shift() -> dict:
    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT status, json_details, details FROM rooms").fetchall()
        if not rows:
            return None
        staff = conn.execute(
            "SELECT checked_by, COUNT(*) FROM rooms "
            "WHERE checked_by IS NOT NULL AND checked_by != '' GROUP BY checked_by"
        ).fetchall()

    catalog = get_catalog()
    prices = get_prices()
    consumed, _ = aggregate_consumed(rows, catalog)
    total_units = sum(consumed.values())
    total_amount = round(sum(room_charge(status, j, catalog, prices) for status, j, _ in rows), 2)
    closed_at = now_tbilisi()

    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO history (closed_at, rooms_checked, total_units, total_amount, consumed_json, staff_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (closed_at, len(rows), total_units, total_amount, json.dumps(consumed), json.dumps(dict(staff)))
        )
        conn.commit()

    archive_name = f"backups/minibar_{datetime.now(TBILISI_TZ).strftime('%Y%m%d_%H%M%S')}.db"
    archived = await backup_db_to_r2(archive_name)

    with closing(db_connect()) as conn:
        conn.execute("DELETE FROM rooms")
        conn.commit()

    schedule_bg(backup_db_to_r2())

    return {
        "rooms": len(rows),
        "units": total_units,
        "amount": total_amount,
        "archive": archive_name if archived else None,
        "closed_at": closed_at,
    }


def _shift_result_text(result: dict, auto: bool) -> str:
    title = (
        "🌅 <b>ცვლა დაიხურა ავტომატურად (Смена закрыта автоматически)</b>"
        if auto else
        "🗑️ <b>ყველა მონაცემი წარმატებით წაიშალა! (База данных номеров полностью очищена).</b>"
    )
    line = f"🏨 ნომრები: {result['rooms']} · 📦 გახარჯულია: {result['units']} ერთ."
    if result["amount"] > 0:
        line += f" · 💰 {fmt_money(result['amount'])}"
    text = f"{title}\n{line}"
    if result["archive"]:
        text += f"\n💾 არქივი R2-ში: <code>{esc(result['archive'])}</code>"
    return text


async def shift_scheduler(bot: Bot):
    fired = {}
    logging.info(
        f"Планировщик смены запущен: авто-очистка {AUTO_CLEAR_TIME or 'выкл'} (Тбилиси)."
    )
    while True:
        try:
            now = datetime.now(TBILISI_TZ)
            hhmm = now.strftime("%H:%M")
            today = now.strftime("%Y-%m-%d")

            if AUTO_CLEAR_TIME and hhmm == AUTO_CLEAR_TIME and fired.get("clear") != today:
                fired["clear"] = today
                result = await close_shift()
                if result:
                    await send_chunked(bot, REPORT_CHAT_ID, _shift_result_text(result, auto=True))
                    logging.info(f"Смена закрыта автоматически: {result['rooms']} номеров")
                else:
                    logging.info("Автоочистка: база пуста, пропущено")
        except Exception:
            logging.exception("Ошибка в планировщике смены")
        await asyncio.sleep(20)


# --- КНОПКИ ---
def build_status_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=STATUS_FULL), KeyboardButton(text=STATUS_NOT_FULL)],
        [KeyboardButton(text=STATUS_EMPTY), KeyboardButton(text="Out of order")]
    ], resize_keyboard=True)


def build_inventory_keyboard(selected_items: dict) -> InlineKeyboardMarkup:
    keyboard = []
    items_list = get_catalog_with_ids()

    for i in range(0, len(items_list), 2):
        row = []
        for item_id, name, max_qty, _price in items_list[i:i + 2]:
            val = selected_items.get(name, 0)
            btn_text = f"✅ {name}: {val}/{max_qty}" if val > 0 else f"{name} ({max_qty})"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"item:{item_id}"))
        keyboard.append(row)

    total_selected = sum(selected_items.values())
    confirm_text = (
        f"✅ დადასტურება (არ არის: {total_selected} ცალი)"
        if total_selected > 0 else "✅ დადასტურება (ყველაფერი ადგილზეა)"
    )
    keyboard.append([InlineKeyboardButton(text=confirm_text, callback_data="confirm_inventory")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def is_supervisor(user_id: int) -> bool:
    return user_id in SUPERVISORS_IDS or user_id in ADMIN_IDS


# --- ГЛАВНОЕ МЕНЮ (кнопки вместо команд) ---
BTN_STATUS = "📊 სტატუსი"
BTN_HELP = "ℹ️ დახმარება"
BTN_PRODUCT = "📦 პროდუქცია"
BTN_PHOTOS = "📷 ფოტოები"
BTN_CATALOG = "🛒 კატალოგი"
BTN_CLOSE_SHIFT = "🗑 ცვლის დახურვა"

MENU_BUTTONS = {
    BTN_STATUS, BTN_HELP, BTN_PRODUCT,
    BTN_PHOTOS, BTN_CATALOG, BTN_CLOSE_SHIFT,
}


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    """Главное меню. Состав кнопок зависит от роли пользователя."""
    rows = [
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HELP)],
    ]
    if is_supervisor(user_id):
        rows.append([KeyboardButton(text=BTN_PRODUCT), KeyboardButton(text=BTN_PHOTOS)])
        rows.append([KeyboardButton(text=BTN_CATALOG)])
    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton(text=BTN_CLOSE_SHIFT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


CLEAR_CONFIRM_WORDS = {"удалить", "წაშლა", "delete"}


def is_clear_confirm(text: str) -> bool:
    return (text or "").strip().lower() in CLEAR_CONFIRM_WORDS


def _parse_item_args(raw: str):
    parts = (raw or "").strip().split()
    if len(parts) < 2 or not parts[-1].isdigit():
        return None, None
    qty = int(parts[-1])
    name = " ".join(parts[:-1]).strip()
    if not (1 <= qty <= 9) or not (1 <= len(name) <= 40):
        return None, None
    return name, qty


def _parse_price_args(raw: str):
    parts = (raw or "").strip().split()
    if len(parts) < 2 or not PRICE_RE.fullmatch(parts[-1]):
        return None, None
    price = round(float(parts[-1].replace(",", ".")), 2)
    name = " ".join(parts[:-1]).strip()
    if not (1 <= len(name) <= 40):
        return None, None
    return name, price


# --- FSM СТАДИИ ---
class ReportForm(StatesGroup):
    room_number = State()
    status = State()
    loss = State()
    photo = State()


class ClearForm(StatesGroup):
    confirm = State()


router = Router()


# --- ОСНОВНЫЕ КОМАНДЫ ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "გამარჯობა! აირჩიეთ მოქმედება ღილაკებით 👇",
        reply_markup=main_menu_kb(message.from_user.id)
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    menu = main_menu_kb(message.from_user.id)
    current = await state.get_state()
    if current is None:
        await message.answer("გასაუქმებელი არაფერია. აირჩიეთ მოქმედება 👇", reply_markup=menu)
        return
    await state.clear()
    await message.answer("❌ შემოწმება გაუქმებულია. აირჩიეთ მოქმედება 👇", reply_markup=menu)


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    text = f"🆔 თქვენი Telegram ID: <code>{message.from_user.id}</code>"
    if message.chat.id != message.from_user.id:
        text += f"\n💬 ამ ჩატის ID: <code>{message.chat.id}</code>"
    text += "\n\n(Эти числа нужны для настройки ADMIN_IDS / SUPERVISORS_IDS / REPORT_CHAT_ID)"
    await message.answer(text, reply_markup=main_menu_kb(message.from_user.id))


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>ინსტრუქცია / Инструкция</b>\n\n"
        "🔍 /audit <code>605</code> — ნომრის აუდიტი (Аудит номера)\n"
        "📊 /status — ყველა ნომრის სია (Список номеров)\n"
        "🆔 /myid — ჩემი ID (Мой Telegram ID)\n\n"
        "⌨️ ყველა მოქმედება ხელმისაწვდომია მენიუს ღილაკებით ქვემოთ\n"
        "      (Все действия доступны кнопками меню под строкой ввода)"
    )

    if is_supervisor(message.from_user.id):
        text += (
            "\n\n<b>👔 სუპერვაიზერებისთვის / Для супервайзеров:</b>\n"
            "• /audit <code>605</code> — детали и фото по номеру\n"
            "• /product — сколько продукции осталось в номерах\n"
            "• /photos — все фото смены альбомом\n"
            "• /catalog — текущий ассортимент и цены"
        )
    if message.from_user.id in ADMIN_IDS:
        text += (
            "\n\n<b>🔧 ადმინისთვის / Для администратора:</b>\n"
            "• /clear — закрыть смену (подтверждение словом: <code>წაშლა</code> / <code>УДАЛИТЬ</code>)\n"
            "• /additem <code>Red Bull 2</code> — добавить товар (эталон 2 шт)\n"
            "• /setqty <code>Wine 2</code> — изменить эталонное количество\n"
            f"• /setprice <code>Wine 25.50</code> — цена в {CURRENCY} (0 — убрать)\n"
            "• /renameitem <code>Cola Clasic | Cola Classic</code> — переименовать\n"
            "• /delitem <code>Red Bull</code> — убрать товар из каталога"
        )
        if AUTO_CLEAR_TIME:
            text += f"\n⏰ Включено: автозакрытие смены в {AUTO_CLEAR_TIME} (Тбилиси)."

    await answer_chunked(message, text, reply_markup=main_menu_kb(message.from_user.id))


@router.message(Command("status"))
async def cmd_status(message: Message):
    all_summary = get_all_rooms_summary()
    status_message = f"📊 <b>მიმდინარე მდგომარეობა (სრული სია):</b>\n{all_summary}"
    await answer_chunked(message, status_message, reply_markup=main_menu_kb(message.from_user.id))


@router.message(Command("audit"))
async def cmd_audit(message: Message, bot: Bot):
    if not is_supervisor(message.from_user.id):
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ გთხოვთ, მიუთითოთ ნომერი. მაგალითად: <code>/audit 605</code>")
        return

    room_to_check = args[1].strip()

    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT status, details, photo_id, photo_type, checked_by, checked_at, json_details "
            "FROM rooms WHERE room_number = ?",
            (room_to_check,)
        ).fetchone()

    if not row:
        await message.answer(f"❌ ნომერი {esc(room_to_check)} ბაზაში ვერ მოიძებნა ან ჯერ არ შემოწმებულა.")
        return

    status, details, photo_id, photo_type, checked_by, checked_at, json_details = row
    lines = [
        f"🔍 <b>აუდიტი ნომრისთვის {esc(room_to_check)}:</b>",
        f"{status_emoji(status)} სტატუსი: {esc(status)}",
    ]
    if details:
        lines.append(f"• დეტალები: {esc(details)}")
    charge = room_charge(status, json_details, get_catalog(), get_prices())
    if charge > 0:
        lines.append(f"• 💰 თანხა (к оплате гостем): {fmt_money(charge)}")
    if checked_by:
        lines.append(f"• 👤 შეამოწმა: {esc(checked_by)}")
    if checked_at:
        lines.append(f"• 🕒 {esc(checked_at)}")
    audit_text = "\n".join(lines)

    if photo_id:
        try:
            if photo_type == "document":
                await bot.send_document(message.chat.id, document=photo_id, caption=audit_text)
            else:
                await bot.send_photo(message.chat.id, photo=photo_id, caption=audit_text)
        except Exception:
            await message.answer(audit_text)
    else:
        await message.answer(audit_text)


@router.message(Command("product", "Product"))
async def cmd_product(message: Message):
    if not is_supervisor(message.from_user.id):
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    with closing(db_connect()) as conn:
        rows = conn.execute("SELECT status, json_details, details FROM rooms").fetchall()

    if not rows:
        await message.answer("ℹ️ ბაზაში ჯერ არ არის მონაცემები ნომრების შესახებ.")
        return

    catalog = get_catalog()
    total_present, manual_rooms = aggregate_present(rows, catalog)
    total_present_count = sum(total_present.values())

    msg = "📦 <b>რა არის ნომრებში (Что есть в номерах)</b>\n"
    msg += f"🏨 შემოწმებულია: {len(rows)} ნომერი\n\n"

    for item_name, qty in total_present.items():
        msg += f"• {esc(item_name)} — {qty}\n"

    msg += f"\n📊 <b>სულ: {total_present_count} ერთ.</b>"

    if manual_rooms:
        msg += (
            f"\n\n⚠️ {manual_rooms} ნომერი შეყვანილია ტექსტით და არ არის ჩათვლილი "
            f"(введено текстом, в подсчёте не учтено — см. /status)."
        )

    await answer_chunked(message, msg, reply_markup=main_menu_kb(message.from_user.id))


@router.message(Command("photos"))
async def cmd_photos(message: Message, bot: Bot):
    if not is_supervisor(message.from_user.id):
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    with closing(db_connect()) as conn:
        rows = conn.execute(
            "SELECT room_number, photo_id, photo_type, checked_by FROM rooms "
            "WHERE photo_id IS NOT NULL AND photo_id != '' "
            "ORDER BY CAST(room_number AS INTEGER), room_number"
        ).fetchall()

    if not rows:
        await message.answer("ℹ️ ამ ცვლაში ფოტოები ჯერ არ არის.")
        return

    photos = [(room, pid, by) for room, pid, ptype, by in rows if ptype != "document"]
    documents = [(room, pid, by) for room, pid, ptype, by in rows if ptype == "document"]

    await message.answer(f"📷 ფოტოები ამ ცვლაში: {len(rows)}", reply_markup=main_menu_kb(message.from_user.id))
    try:
        for i in range(0, len(photos), 10):
            chunk = photos[i:i + 10]
            if len(chunk) == 1:
                room, pid, by = chunk[0]
                await bot.send_photo(message.chat.id, photo=pid, caption=f"№{room} · {by or ''}".strip())
            else:
                media = [
                    InputMediaPhoto(media=pid, caption=f"№{room} · {by or ''}".strip())
                    for room, pid, by in chunk
                ]
                await bot.send_media_group(message.chat.id, media=media)
            await asyncio.sleep(1)

        for room, pid, by in documents:
            await bot.send_document(message.chat.id, document=pid, caption=f"№{room} · {by or ''}".strip())
            await asyncio.sleep(0.5)
    except Exception as e:
        logging.exception(f"Ошибка отправки фото-альбома: {e}")
        await message.answer("⚠️ ზოგიერთი ფოტოს გაგზავნა ვერ მოხერხდა.")


@router.message(Command("clear", "reset"))
async def cmd_clear(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ ადმინისტრატორისთვის.")
        return

    await state.set_state(ClearForm.confirm)
    await message.answer(
        "⚠️ <b>ცვლის დახურვა — წაიშლება ყველა შემოწმებული ნომერი!</b>\n"
        "(Итоги уйдут в историю, база — в архив R2, список проверок очистится.)\n\n"
        "დასადასტურებლად დაწერეთ სიტყვა: <code>წაშლა</code>\n"
        "(Для подтверждения напишите слово: <code>УДАЛИТЬ</code>)\n\n"
        "ნებისმიერი სხვა ტექსტი გააუქმებს. (Любой другой текст — отмена.)",
        reply_markup=ReplyKeyboardRemove()
    )


@router.message(ClearForm.confirm)
async def process_clear_confirm(message: Message, state: FSMContext):
    await state.clear()
    menu = main_menu_kb(message.from_user.id)

    if not message.text or not is_clear_confirm(message.text):
        await message.answer("❌ ცვლის დახურვა გაუქმებულია. (Закрытие смены отменено.)", reply_markup=menu)
        return

    result = await close_shift()
    if result is None:
        await message.answer("ℹ️ ბაზა ისედაც ცარიელია — წასაშლელი არაფერია.", reply_markup=menu)
        return

    await message.answer(_shift_result_text(result, auto=False), reply_markup=menu)


# --- УПРАВЛЕНИЕ КАТАЛОГОМ ---
@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    if not is_supervisor(message.from_user.id):
        await message.answer("⛔ ეს ბრძანება ხელმისაწვდომია მხოლოდ სუპერვაიზერებისთვის.")
        return

    items = get_catalog_with_ids()
    if not items:
        await message.answer("ℹ️ კატალოგი ცარიელია. დაამატეთ: /additem <code>Cola 2</code>")
        return

    lines = ["🛒 <b>მინი-ბარის კატალოგი / Каталог мини-бара:</b>", ""]
    total = 0
    for idx, (_, name, qty, price) in enumerate(items, start=1):
        line = f"{idx}. <b>{esc(name)}</b> — {qty} ც."
        if price and price > 0:
            line += f" · {fmt_money(price)}"
        lines.append(line)
        total += qty
    lines.append("")
    lines.append(f"სულ: {len(items)} დასახელება / {total} ერთეული")
    if message.from_user.id in ADMIN_IDS:
        lines.append("\nშეცვლა / Изменить: /additem, /setqty, /setprice, /renameitem, /delitem (см. /help)")
    await answer_chunked(message, "\n".join(lines), reply_markup=main_menu_kb(message.from_user.id))


@router.message(Command("additem"))
async def cmd_additem(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ მხოლოდ ადმინისტრატორისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    name, qty = _parse_item_args(args[1] if len(args) > 1 else "")
    if not name:
        await message.answer(
            "⚠️ ფორმატი: /additem <code>სახელი რაოდენობა</code>\n"
            "მაგალითად: /additem <code>Red Bull 2</code> (რაოდენობა 1–9)"
        )
        return

    with closing(db_connect()) as conn:
        exists = conn.execute("SELECT 1 FROM catalog WHERE name = ?", (name,)).fetchone()
        if exists:
            await message.answer(f"⚠️ «{esc(name)}» უკვე არის კატალოგში. რაოდენობის შესაცვლელად: /setqty")
            return
        max_pos = conn.execute("SELECT COALESCE(MAX(position), 0) FROM catalog").fetchone()[0]
        conn.execute("INSERT INTO catalog (name, qty, position) VALUES (?, ?, ?)", (name, qty, max_pos + 1))
        conn.commit()

    schedule_bg(backup_db_to_r2())
    await message.answer(
        f"✅ დამატებულია: <b>{esc(name)}</b> — {qty} ც.\n"
        f"ფასის დასაყენებლად: /setprice <code>{esc(name)} 10</code>"
    )


@router.message(Command("setqty"))
async def cmd_setqty(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ მხოლოდ ადმინისტრატორისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    name, qty = _parse_item_args(args[1] if len(args) > 1 else "")
    if not name:
        await message.answer("⚠️ ფორმატი: /setqty <code>სახელი რაოდენობა</code>, მაგ: /setqty <code>Wine 2</code>")
        return

    with closing(db_connect()) as conn:
        cur = conn.execute("UPDATE catalog SET qty = ? WHERE name = ?", (qty, name))
        conn.commit()
        if cur.rowcount == 0:
            await message.answer(f"❌ «{esc(name)}» კატალოგში ვერ მოიძებნა. სია: /catalog")
            return

    schedule_bg(backup_db_to_r2())
    await message.answer(f"✅ განახლდა: <b>{esc(name)}</b> — ახლა {qty} ც.")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ მხოლოდ ადმინისტრატორისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    name, price = _parse_price_args(args[1] if len(args) > 1 else "")
    if name is None:
        await message.answer(
            "⚠️ ფორმატი: /setprice <code>სახელი ფასი</code>\n"
            f"მაგალითად: /setprice <code>Wine 25.50</code> (в {CURRENCY}; 0 — убрать)"
        )
        return

    with closing(db_connect()) as conn:
        cur = conn.execute("UPDATE catalog SET price = ? WHERE name = ?", (price, name))
        conn.commit()
        if cur.rowcount == 0:
            await message.answer(f"❌ «{esc(name)}» კატალოგში ვერ მოიძებნა. სია: /catalog")
            return

    schedule_bg(backup_db_to_r2())
    if price > 0:
        await message.answer(f"💰 ფასი დაყენებულია: <b>{esc(name)}</b> — {fmt_money(price)}")
    else:
        await message.answer(f"💰 ფასი მოხსნილია: <b>{esc(name)}</b>")


@router.message(Command("delitem"))
async def cmd_delitem(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ მხოლოდ ადმინისტრატორისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    name = args[1].strip() if len(args) > 1 else ""
    if not name:
        await message.answer("⚠️ ფორმატი: /delitem <code>სახელი</code>, მაგ: /delitem <code>Red Bull</code>")
        return

    with closing(db_connect()) as conn:
        cur = conn.execute("DELETE FROM catalog WHERE name = ?", (name,))
        conn.commit()
        if cur.rowcount == 0:
            await message.answer(f"❌ «{esc(name)}» კატალოგში ვერ მოიძებნა. სია: /catalog")
            return

    schedule_bg(backup_db_to_r2())
    await message.answer(f"🗑️ წაშლილია კატალოგიდან: <b>{esc(name)}</b>")


@router.message(Command("renameitem"))
async def cmd_renameitem(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ მხოლოდ ადმინისტრატორისთვის.")
        return

    args = (message.text or "").split(maxsplit=1)
    raw = args[1] if len(args) > 1 else ""
    if "|" not in raw:
        await message.answer(
            "⚠️ ფორმატი: /renameitem <code>ძველი | ახალი</code>\n"
            "მაგალითად: /renameitem <code>Cola Clasic | Cola Classic</code>"
        )
        return

    old_name, new_name = (part.strip() for part in raw.split("|", 1))
    if not old_name or not new_name or len(new_name) > 40:
        await message.answer("⚠️ შეამოწმეთ სახელები (ახალი სახელი — 40 სიმბოლომდე).")
        return

    with closing(db_connect()) as conn:
        if not conn.execute("SELECT 1 FROM catalog WHERE name = ?", (old_name,)).fetchone():
            await message.answer(f"❌ «{esc(old_name)}» კატალოგში ვერ მოიძებნა. სია: /catalog")
            return
        if conn.execute("SELECT 1 FROM catalog WHERE name = ?", (new_name,)).fetchone():
            await message.answer(f"⚠️ «{esc(new_name)}» უკვე არსებობს კატალოგში.")
            return

        conn.execute("UPDATE catalog SET name = ? WHERE name = ?", (new_name, old_name))

        migrated = 0
        for room, json_str, details in conn.execute(
            "SELECT room_number, json_details, details FROM rooms"
        ).fetchall():
            changed = False
            new_json = json_str
            if json_str:
                try:
                    data = json.loads(json_str)
                    if old_name in data:
                        data[new_name] = data.pop(old_name)
                        new_json = json.dumps(data)
                        changed = True
                except Exception:
                    pass
            new_details = details
            if details and old_name in details:
                new_details = details.replace(old_name, new_name)
                changed = True
            if changed:
                conn.execute(
                    "UPDATE rooms SET json_details = ?, details = ? WHERE room_number = ?",
                    (new_json, new_details, room)
                )
                migrated += 1
        conn.commit()

    schedule_bg(backup_db_to_r2())
    text = f"✅ გადარქმეულია: <b>{esc(old_name)}</b> → <b>{esc(new_name)}</b>"
    if migrated:
        text += f"\n🔄 განახლდა {migrated} შენახული შემოწმებაც."
    await message.answer(text)


# КНОПКИ ГЛАВНОГО МЕНЮ
@router.message(StateFilter(None), F.text.in_(MENU_BUTTONS))
async def menu_buttons(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    if text == BTN_STATUS:
        await cmd_status(message)
    elif text == BTN_HELP:
        await cmd_help(message)
    elif text == BTN_PRODUCT:
        await cmd_product(message)
    elif text == BTN_PHOTOS:
        await cmd_photos(message, bot)
    elif text == BTN_CATALOG:
        await cmd_catalog(message)
    elif text == BTN_CLOSE_SHIFT:
        await cmd_clear(message, state)


# --- СЦЕНАРИЙ ПРОВЕРКИ НОМЕРА ---
@router.message(ReportForm.room_number, F.text)
async def process_room(message: Message, state: FSMContext):
    room = (message.text or "").strip()
    if not ROOM_RE.fullmatch(room):
        await message.answer(
            "⚠️ ნომრის ფორმატი არასწორია. გთხოვთ, შეიყვანეთ ნომერი ციფრებით "
            "(მაგალითად: 605):"
        )
        return

    with closing(db_connect()) as conn:
        existing = conn.execute(
            "SELECT status, checked_by, checked_at FROM rooms WHERE room_number = ?",
            (room,)
        ).fetchone()

    if existing:
        ex_status, ex_by, ex_at = existing
        await state.update_data(pending_room=room)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔁 დიახ, თავიდან", callback_data="room_overwrite"),
            InlineKeyboardButton(text="❌ არა", callback_data="room_abort"),
        ]])
        info = f"{status_emoji(ex_status)} {esc(ex_status)}"
        if ex_by:
            info += f" · 👤 {esc(ex_by)}"
        if ex_at:
            info += f" · 🕒 {esc(ex_at)}"
        await message.answer(
            f"⚠️ ნომერი <b>{esc(room)}</b> უკვე შემოწმებულია ამ ცვლაში:\n{info}\n\n"
            f"თავიდან შევამოწმოთ? (Перепроверить и перезаписать?)",
            reply_markup=kb
        )
        return

    await state.update_data(room=room)
    await state.set_state(ReportForm.status)
    await message.answer("აირჩიეთ ნომრის სტატუსი:", reply_markup=build_status_keyboard())


@router.callback_query(ReportForm.room_number, F.data == "room_overwrite")
async def cb_room_overwrite(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    room = data.get("pending_room")
    if not room:
        await callback.answer()
        return
    await state.update_data(room=room)
    await state.set_state(ReportForm.status)
    try:
        await callback.message.edit_text(f"🔁 ნომერი {esc(room)} — ხელახალი შემოწმება:")
    except Exception:
        pass
    await callback.message.answer("აირჩიეთ ნომრის სტატუსი:", reply_markup=build_status_keyboard())
    await callback.answer()


@router.callback_query(ReportForm.room_number, F.data == "room_abort")
async def cb_room_abort(callback: CallbackQuery):
    try:
        await callback.message.edit_text("❌ კარგი. შეიყვანეთ სხვა ნომრის ნომერი:")
    except Exception:
        pass
    await callback.answer()


@router.message(ReportForm.room_number)
async def process_room_invalid(message: Message):
    await message.answer("⚠️ გთხოვთ, შეიყვანეთ ნომრის ნომერი ტექსტით (მაგალითად: 605):")


@router.message(ReportForm.status, F.text)
async def process_status(message: Message, state: FSMContext):
    status_text = (message.text or "").strip()

    if status_text in ("ცარიელია", STATUS_EMPTY):
        status_text = STATUS_EMPTY
        await state.update_data(status=status_text)
        full_missing = get_catalog()
        details_list = [f"{k}: {v}" for k, v in full_missing.items()]
        details_text = f"გამოყენებულია: {', '.join(details_list)} (სულ {sum(full_missing.values())} ცალი)"
        json_str = json.dumps(full_missing)

        await state.update_data(loss=details_text, json_details=json_str)
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())
    elif status_text == STATUS_NOT_FULL:
        await state.update_data(status=status_text, selected_items={}, loss=None, json_details=None)
        await state.set_state(ReportForm.loss)
        kb = build_inventory_keyboard({})
        await message.answer(
            "📋 <b>მონიშნეთ რომელი პროდუქცია აკლია (выберите выпитое):</b>\n"
            "(Нажмите на товар, чтобы указать кол-во: 1 или 2)",
            reply_markup=ReplyKeyboardRemove()
        )
        await message.answer("აირჩიეთ აკლებული პროდუქტები:", reply_markup=kb)
    elif "out of order" in status_text.lower():
        await state.update_data(status=status_text, loss="", json_details="")
        await save_and_send_report(message, state, message.bot, photo_id=None)
    elif status_text == STATUS_FULL:
        await state.update_data(status=status_text, loss="", json_details="")
        await state.set_state(ReportForm.photo)
        await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer(
            "⚠️ გთხოვთ, აირჩიოთ სტატუსი ღილაკებით 👇",
            reply_markup=build_status_keyboard()
        )


@router.message(ReportForm.status)
async def process_status_invalid(message: Message):
    await message.answer("⚠️ გთხოვთ, აირჩიოთ სტატუსი ღილაკებით 👇", reply_markup=build_status_keyboard())


@router.callback_query(ReportForm.loss, F.data.startswith("item:"))
async def process_item_toggle(callback: CallbackQuery, state: FSMContext):
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer()
        return

    with closing(db_connect()) as conn:
        row = conn.execute("SELECT name, qty FROM catalog WHERE id = ?", (int(raw_id),)).fetchone()
    if not row:
        await callback.answer("⚠️ პროდუქტი ვერ მოიძებნა (კატალოგი შეიცვალა).", show_alert=True)
        return

    item_name, max_qty = row
    data = await state.get_data()
    selected = data.get("selected_items", {}) or {}

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


def _loss_from_selected(selected: dict):
    if selected:
        details_list = [f"{k}: {v}" for k, v in selected.items()]
        return f"გამოყენებულია: {', '.join(details_list)}", json.dumps(selected)
    return "", ""


@router.callback_query(ReportForm.loss, F.data == "confirm_inventory")
async def process_inventory_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get("selected_items", {}) or {}
    details_text, json_str = _loss_from_selected(selected)

    await state.update_data(loss=details_text, json_details=json_str)
    await state.set_state(ReportForm.photo)

    try:
        await callback.message.edit_text(
            f"✅ არჩევანი შენახულია!\n{details_text if details_text else '• ყველა პროდუქტი ადგილზეა.'}"
        )
    except Exception:
        pass
    await callback.message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:")
    await callback.answer()


@router.message(ReportForm.loss, F.photo | F.document)
@router.message(ReportForm.photo, F.photo | F.document)
async def process_photo(message: Message, state: FSMContext, bot: Bot):
    if message.photo:
        photo_id, photo_type = message.photo[-1].file_id, "photo"
    else:
        mime = (message.document.mime_type or "")
        if not mime.startswith("image/"):
            await message.answer("⚠️ გთხოვთ, გამოაგზავნოთ ფოტო (სურათი).")
            return
        photo_id, photo_type = message.document.file_id, "document"

    current_state = await state.get_state()
    if current_state == ReportForm.loss.state:
        data = await state.get_data()
        if not data.get("loss"):
            details_text, json_str = _loss_from_selected(data.get("selected_items", {}) or {})
            await state.update_data(loss=details_text, json_details=json_str)

    await save_and_send_report(message, state, bot, photo_id, photo_type)


@router.message(ReportForm.loss, F.text)
async def process_loss_text(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("აირჩიეთ პროდუქტები ღილაკებით ან დაწერეთ ტექსტით.")
        return
    await state.update_data(loss=f"გამოყენებულია: {text}", json_details="")
    await state.set_state(ReportForm.photo)
    await message.answer("გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება:")


@router.message(ReportForm.loss)
async def process_loss_other(message: Message):
    await message.answer("აირჩიეთ პროდუქტები ღილაკებით, დაწერეთ ტექსტით ან გამოაგზავნეთ ფოტო. გაუქმება: /cancel")


@router.message(ReportForm.photo)
async def process_photo_invalid(message: Message):
    await message.answer("📷 გთხოვთ, გამოაგზავნოთ ფოტო-მტკიცებულება. გაუქმება: /cancel")


async def save_and_send_report(message: Message, state: FSMContext, bot: Bot,
                                photo_id: str = None, photo_type: str = "photo"):
    data = await state.get_data()
    room = data.get('room', 'Н/Д')
    status = data.get('status', 'Н/Д')
    loss = data.get('loss') or ""
    json_details = data.get('json_details') or ""
    checked_by = message.from_user.full_name if message.from_user else ""
    checked_at = now_tbilisi()

    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO rooms "
            "(room_number, status, details, photo_id, json_details, photo_type, checked_by, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (room, status, loss, photo_id or "", json_details, photo_type if photo_id else "", checked_by, checked_at)
        )
        conn.commit()

    schedule_bg(backup_db_to_r2())

    if photo_id:
        try:
            photo_file = await bot.get_file(photo_id)
            photo_bytes_io = await bot.download_file(photo_file.file_path)
            if photo_bytes_io:
                photo_bytes = photo_bytes_io.read()
                schedule_bg(r2.upload_photo(photo_bytes, f"photos/room_{room}_{photo_id}.jpg"))
        except Exception as e:
            logging.error(f"Не удалось загрузить фото в R2: {e}")

    all_summary = get_all_rooms_summary()

    header_lines = [
        "📦 <b>მინი-ბარის ანგარიში</b>",
        "",
        f"🏨 ნომერი: {esc(room)}",
        f"{status_emoji(status)} სტატუსი: {esc(status)}",
    ]
    if loss and "ცარიელია" not in status:
        header_lines.append(f"⚠️ {esc(loss)}")
    charge = room_charge(status, json_details, get_catalog(), get_prices())
    if charge > 0:
        header_lines.append(f"💰 თანხა (к оплате гостем): {fmt_money(charge)}")
    header_lines.append(f"👤 თანამშრომელი: {esc(checked_by)}")
    header_lines.append(f"🕒 {checked_at}")
    header_text = "\n".join(header_lines)

    summary_block = f"📊 <b>მიმდინარე მდგომარეობა (სრული სია):</b>\n{all_summary}"

    report_sent = True
    try:
        if photo_id:
            send_media = bot.send_document if photo_type == "document" else bot.send_photo
            media_kwargs = {"document": photo_id} if photo_type == "document" else {"photo": photo_id}
            # Фотография отправляется с подробностями отчёта под ней
            await send_media(REPORT_CHAT_ID, caption=header_text, **media_kwargs)
            await send_chunked(bot, REPORT_CHAT_ID, summary_block)
        else:
            await send_chunked(bot, REPORT_CHAT_ID, f"{header_text}\n\n{summary_block}")
    except Exception as e:
        report_sent = False
        logging.error(f"Не удалось отправить отчет в чат {REPORT_CHAT_ID}: {e}")

    menu = main_menu_kb(message.from_user.id if message.from_user else 0)
    if report_sent:
        await message.answer("ანგარიში წარმატებით გაიგზავნა! ✅", reply_markup=menu)
    else:
        await message.answer(
            "⚠️ მონაცემები შენახულია, მაგრამ ანგარიშის გაგზავნა ჯგუფში ვერ მოხერხდა. "
            "(Данные сохранены, но отчёт в рабочий чат не ушёл — сообщите администратору.)",
            reply_markup=menu
        )
    await state.clear()


@router.message(StateFilter(None))
async def fallback(message: Message):
    await message.answer(
        "აირჩიეთ მოქმედება ღილაკებით 👇",
        reply_markup=main_menu_kb(message.from_user.id)
    )


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot):
    logging.exception(f"Необработанная ошибка: {event.exception}")

    try:
        if event.update.message:
            await event.update.message.answer(
                "⚠️ მოხდა შეცდომა. სცადეთ თავიდან: /start\n(Произошла ошибка, попробуйте ещё раз.)"
            )
        elif event.update.callback_query:
            await event.update.callback_query.answer("⚠️ შეცდომა. სცადეთ თავიდან.", show_alert=True)
    except Exception:
        pass

    now_ts = time.time()
    if now_ts - _last_error_dm["ts"] > 300:
        _last_error_dm["ts"] = now_ts
        err_text = (
            f"🚨 <b>Minibar Bot: ошибка</b>\n"
            f"<code>{esc(str(event.exception)[:300])}</code>\n"
            f"🕒 {now_tbilisi()} · подробности в логах сервера"
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, err_text)
            except Exception:
                pass

    return True


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
    logging.info("Checking for database backup in Cloudflare R2...")
    await r2.download_db(DB_FILE)

    init_db()

    await start_healthcheck_server(PORT)

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    commands = [
        BotCommand(command="start", description="მთავარი მენიუ (Главное меню)"),
        BotCommand(command="audit", description="ნომრის აუდიტი (Аудит номера /audit <номер>)"),
        BotCommand(command="status", description="ყველა ნომრის სტატუსი (Общий статус)"),
        BotCommand(command="help", description="ინსტრუქცია (Инструкция)"),
        BotCommand(command="myid", description="ჩემი ID (Мой Telegram ID)"),
        BotCommand(command="product", description="პროდუქციის ანგარიში (Отчет по продукции)"),
        BotCommand(command="photos", description="ცვლის ფოტოები (Фото смены)"),
        BotCommand(command="catalog", description="კატალოგი (Каталог мини-бара)"),
        BotCommand(command="clear", description="ცვლის დახურვა (Закрыть смену)")
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        logging.warning(f"Не удалось установить меню команд: {e}")

    if AUTO_CLEAR_TIME:
        schedule_bg(shift_scheduler(bot))

    logging.info("Starting Telegram Bot polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await backup_db_to_r2()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")
