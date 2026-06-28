import os
import sqlite3
import asyncio
import logging
import re
from datetime import datetime, timedelta
from flask import Flask
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ChatPermissions,
    Message
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

# ===================== НАСТРОЙКИ =====================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("Не задан TELEGRAM_TOKEN")

INITIAL_ADMINS = []
admins_env = os.environ.get("ADMIN_IDS", "")
if admins_env:
    INITIAL_ADMINS = [int(x.strip()) for x in admins_env.split(",") if x.strip()]

# ===================== ИНИЦИАЛИЗАЦИЯ =====================
storage = MemoryStorage()
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)
DB_NAME = "bot_data.db"

# ===================== БАЗА ДАННЫХ =====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        full_name TEXT,
        level TEXT DEFAULT 'Новичок',
        xp INTEGER DEFAULT 0,
        total_xp INTEGER DEFAULT 0,
        bio TEXT DEFAULT '',
        coins INTEGER DEFAULT 0,
        warnings_count INTEGER DEFAULT 0,
        is_muted BOOLEAN DEFAULT 0,
        mute_until TIMESTAMP,
        referred_by INTEGER,
        referral_count INTEGER DEFAULT 0,
        active_prefix TEXT,
        commission_discount REAL DEFAULT 0.0,
        no_queue BOOLEAN DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS guarantors (
        user_id INTEGER PRIMARY KEY,
        is_active BOOLEAN DEFAULT 1,
        current_deal_id INTEGER,
        total_deals INTEGER DEFAULT 0,
        rating REAL DEFAULT 0.0,
        feedback_count INTEGER DEFAULT 0,
        vip_chat_id INTEGER,
        commission_rate REAL DEFAULT 0.02
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        guarantor_id INTEGER,
        amount REAL,
        description TEXT,
        status TEXT DEFAULT 'pending',
        vip_chat_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        is_vip BOOLEAN DEFAULT 0,
        commission REAL DEFAULT 0.0,
        priority BOOLEAN DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS deal_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        amount REAL,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        priority BOOLEAN DEFAULT 0,
        status TEXT DEFAULT 'waiting'
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS store_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        description TEXT,
        price INTEGER,
        type TEXT,
        value TEXT,
        available BOOLEAN DEFAULT 1
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS user_items (
        user_id INTEGER,
        item_id INTEGER,
        applied BOOLEAN DEFAULT 0,
        PRIMARY KEY (user_id, item_id)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS prefix_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        prefix TEXT,
        status TEXT DEFAULT 'pending'
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS feedbacks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user INTEGER,
        to_user INTEGER,
        rating INTEGER CHECK(rating BETWEEN 1 AND 5),
        text TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        deleted BOOLEAN DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS scammers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        evidence TEXT,
        added_by INTEGER,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS scam_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reported_username TEXT NOT NULL,
        evidence TEXT,
        reporter_id INTEGER,
        status TEXT DEFAULT 'pending',
        admin_comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS levels (
        name TEXT PRIMARY KEY,
        xp_required INTEGER,
        bonus_coins INTEGER DEFAULT 0,
        commission_rate REAL DEFAULT 0.02,
        no_queue BOOLEAN DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS blacklist (
        word TEXT PRIMARY KEY
    )''')

    # Добавляем уровни по умолчанию
    cur.execute("SELECT COUNT(*) FROM levels")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO levels (name, xp_required, bonus_coins, commission_rate, no_queue) VALUES (?, ?, ?, ?, ?)",
            [
                ('Новичок', 0, 0, 0.02, 0),
                ('Продвинутый', 100, 50, 0.015, 0),
                ('Эксперт', 300, 150, 0.01, 1),
                ('Легенда', 600, 300, 0.005, 1)
            ]
        )

    # Настройки по умолчанию
    defaults = [
        ('vip_threshold_amount', '500'),
        ('vip_threshold_level', 'Эксперт'),
        ('moderation_enabled', '1'),
        ('filter_links', '1'),
        ('filter_badwords', '1'),
        ('xp_per_message', '1'),
        ('ref_bonus_coins', '50'),
        ('ref_bonus_xp', '10'),
        ('deal_xp_percent', '5'),
        ('default_commission', '0.02'),
        ('scam_action', 'mute'),
        ('admin_ids', ','.join(map(str, INITIAL_ADMINS))),
        ('commission_currency', 'coins')
    ]
    for key, value in defaults:
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    conn.commit()
    conn.close()

init_db()

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================
def is_admin(user_id: int) -> bool:
    if user_id in INITIAL_ADMINS:
        return True
    admins_str = get_setting('admin_ids')
    if admins_str:
        admin_list = [int(x.strip()) for x in admins_str.split(',') if x.strip()]
        if user_id in admin_list:
            return True
    return False

def get_setting(key):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def get_vip_chat_id():
    vip = get_setting('vip_chat_id')
    return int(vip) if vip else None

def get_user(user_id: int):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_by_username(username: str):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_level(user_id: int):
    user = get_user(user_id)
    return user[3] if user else 'Новичок'

def create_user_if_not_exists(user_id, username, full_name, referred_by=None):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?, ?, ?, ?)",
        (user_id, username, full_name, referred_by)
    )
    if referred_by:
        ref_coins = int(get_setting('ref_bonus_coins') or 50)
        ref_xp = int(get_setting('ref_bonus_xp') or 10)
        cur.execute("UPDATE users SET referral_count = referral_count + 1, coins = coins + ? WHERE user_id = ?",
                    (ref_coins, referred_by))
        add_xp(referred_by, ref_xp)
    conn.commit()
    conn.close()

def update_user_field(user_id, field, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    # безопасно: поле передаётся только из кода, не от пользователя
    cur.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
    conn.commit()
    conn.close()

def add_coins(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def get_coins(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT coins FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def add_xp(user_id, amount):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT xp, total_xp, level FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    new_xp = row[0] + amount
    total_xp = row[1] + amount
    current_level = row[2]
    levels = get_levels()
    new_level = None
    for level_name, xp_req, bonus, comm, noq in levels:
        if new_xp >= xp_req:
            new_level = level_name
    if new_level and new_level != current_level:
        for level_name, xp_req, bonus, comm, noq in levels:
            if level_name == new_level:
                add_coins(user_id, bonus)
                update_user_field(user_id, 'commission_discount', comm)
                update_user_field(user_id, 'no_queue', 1 if noq else 0)
                asyncio.create_task(bot.send_message(user_id, f"🎉 Вы достигли уровня {new_level}! Получено {bonus} монет."))
                break
    cur.execute(
        "UPDATE users SET xp = ?, total_xp = ?, level = ? WHERE user_id = ?",
        (new_xp, total_xp, new_level or current_level, user_id)
    )
    conn.commit()
    conn.close()
    return new_level

def get_levels():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name, xp_required, bonus_coins, commission_rate, no_queue FROM levels ORDER BY xp_required")
    rows = cur.fetchall()
    conn.close()
    return rows

def is_guarantor(user_id: int) -> bool:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM guarantors WHERE user_id = ? AND is_active = 1", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def add_guarantor(user_id, vip_chat_id=None, commission_rate=0.02):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO guarantors (user_id, vip_chat_id, commission_rate) VALUES (?, ?, ?)",
        (user_id, vip_chat_id, commission_rate)
    )
    conn.commit()
    conn.close()

def remove_guarantor(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM guarantors WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_free_guarantors():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, vip_chat_id, commission_rate FROM guarantors WHERE is_active = 1 AND current_deal_id IS NULL"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def set_guarantor_deal(user_id, deal_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET current_deal_id = ? WHERE user_id = ?", (deal_id, user_id))
    conn.commit()
    conn.close()

def clear_guarantor_deal(user_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET current_deal_id = NULL WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def create_deal_request(buyer_id, seller_id, amount, description, priority=0):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deal_requests (buyer_id, seller_id, amount, description, priority) VALUES (?, ?, ?, ?, ?)",
        (buyer_id, seller_id, amount, description, priority)
    )
    req_id = cur.lastrowid
    conn.commit()
    conn.close()
    return req_id

def get_active_deal_requests():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, buyer_id, seller_id, amount, description, priority FROM deal_requests WHERE status = 'waiting' ORDER BY priority DESC, created_at"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def set_deal_request_status(request_id, status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE deal_requests SET status = ? WHERE id = ?", (status, request_id))
    conn.commit()
    conn.close()

def create_deal(buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO deals (buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, is_vip, commission)
    )
    deal_id = cur.lastrowid
    conn.commit()
    conn.close()
    return deal_id

def get_deal(deal_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT * FROM deals WHERE id = ?", (deal_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_deal_status(deal_id, status):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE deals SET status = ? WHERE id = ?", (status, deal_id))
    if status == 'completed':
        cur.execute("UPDATE deals SET completed_at = CURRENT_TIMESTAMP WHERE id = ?", (deal_id,))
    conn.commit()
    conn.close()

def add_feedback(from_user, to_user, rating, text):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO feedbacks (from_user, to_user, rating, text) VALUES (?, ?, ?, ?)",
        (from_user, to_user, rating, text)
    )
    conn.commit()
    cur.execute("SELECT AVG(rating), COUNT(*) FROM feedbacks WHERE to_user = ? AND deleted = 0", (to_user,))
    avg, count = cur.fetchone()
    cur.execute(
        "UPDATE guarantors SET rating = ?, feedback_count = ? WHERE user_id = ?",
        (avg or 0, count or 0, to_user)
    )
    conn.commit()
    conn.close()

def delete_feedback(feedback_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE feedbacks SET deleted = 1 WHERE id = ?", (feedback_id,))
    conn.commit()
    conn.close()

def get_feedbacks_for_guarantor(guarantor_id, limit=10):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, from_user, rating, text, timestamp FROM feedbacks WHERE to_user = ? AND deleted = 0 ORDER BY timestamp DESC LIMIT ?",
        (guarantor_id, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def add_scammer(username, evidence, admin_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scammers (username, evidence, added_by) VALUES (?, ?, ?)",
        (username, evidence, admin_id)
    )
    conn.commit()
    conn.close()

def remove_scammer(username):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM scammers WHERE username = ?", (username,))
    conn.commit()
    conn.close()

def is_scammer(username):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT username FROM scammers WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    return row is not None

def add_scam_report(reported_username, evidence, reporter_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scam_reports (reported_username, evidence, reporter_id) VALUES (?, ?, ?)",
        (reported_username, evidence, reporter_id)
    )
    report_id = cur.lastrowid
    conn.commit()
    conn.close()
    return report_id

def get_pending_scam_reports():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, reported_username, evidence, reporter_id, created_at FROM scam_reports WHERE status = 'pending'")
    rows = cur.fetchall()
    conn.close()
    return rows

def approve_scam_report(report_id, admin_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT reported_username, evidence FROM scam_reports WHERE id = ?", (report_id,))
    row = cur.fetchone()
    if row:
        username, evidence = row
        add_scammer(username, evidence, admin_id)
        cur.execute("UPDATE scam_reports SET status = 'approved', admin_comment = 'Одобрено' WHERE id = ?", (report_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def reject_scam_report(report_id, comment):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE scam_reports SET status = 'rejected', admin_comment = ? WHERE id = ?", (comment, report_id))
    conn.commit()
    conn.close()

def get_store_items():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, price, type, value FROM store_items WHERE available = 1")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_store_item(name, description, price, type, value):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO store_items (name, description, price, type, value) VALUES (?, ?, ?, ?, ?)",
        (name, description, price, type, value)
    )
    conn.commit()
    conn.close()

def remove_store_item(item_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE store_items SET available = 0 WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()

def buy_item(user_id, item_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, price, type, value FROM store_items WHERE id = ? AND available = 1", (item_id,))
    item = cur.fetchone()
    if not item:
        conn.close()
        return None
    if get_coins(user_id) < item[1]:
        conn.close()
        return False
    update_user_field(user_id, 'coins', get_coins(user_id) - item[1])
    cur.execute("INSERT INTO user_items (user_id, item_id) VALUES (?, ?)", (user_id, item_id))
    conn.commit()
    conn.close()
    return item

def add_prefix_request(user_id, prefix):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO prefix_requests (user_id, prefix) VALUES (?, ?)",
        (user_id, prefix)
    )
    conn.commit()
    conn.close()

def get_pending_prefix_requests():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, prefix FROM prefix_requests WHERE status = 'pending'")
    rows = cur.fetchall()
    conn.close()
    return rows

def approve_prefix_request(request_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, prefix FROM prefix_requests WHERE id = ?", (request_id,))
    row = cur.fetchone()
    if row:
        user_id, prefix = row
        update_user_field(user_id, 'active_prefix', prefix)
        cur.execute("UPDATE prefix_requests SET status = 'approved' WHERE id = ?", (request_id,))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def reject_prefix_request(request_id):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE prefix_requests SET status = 'rejected' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()

def get_badwords():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT word FROM blacklist")
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

def add_badword(word):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO blacklist (word) VALUES (?)", (word,))
    conn.commit()
    conn.close()

def remove_badword(word):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM blacklist WHERE word = ?", (word,))
    conn.commit()
# ===================== СОСТОЯНИЯ ДЛЯ FSM =====================
class DealStates(StatesGroup):
    waiting_for_seller = State()
    waiting_for_amount = State()
    waiting_for_description = State()

class AdminStates(StatesGroup):
    waiting_for_level_name = State()
    waiting_for_level_xp = State()
    waiting_for_level_bonus = State()
    waiting_for_level_commission = State()
    waiting_for_level_noqueue = State()
    waiting_for_item_name = State()
    waiting_for_item_desc = State()
    waiting_for_item_price = State()
    waiting_for_item_type = State()
    waiting_for_item_value = State()
    waiting_for_badword = State()
    waiting_for_broadcast = State()
    waiting_for_scammer_username = State()
    waiting_for_scammer_evidence = State()
    waiting_for_feedback_delete = State()
    waiting_for_prefix_approve = State()
    waiting_for_prefix_reject = State()
    waiting_for_vip_threshold_amount = State()
    waiting_for_vip_threshold_level = State()
    waiting_for_user_search = State()
    waiting_for_user_xp = State()
    waiting_for_user_coins = State()
    waiting_for_user_level = State()
    waiting_for_remove_guarantor = State()
    waiting_for_remove_scammer = State()

class FeedbackStates(StatesGroup):
    waiting_for_guarantor = State()
    waiting_for_rating = State()
    waiting_for_text = State()

class BuyStates(StatesGroup):
    waiting_for_item_id = State()

class ReportScamStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_evidence = State()

# ===================== ОБРАБОТЧИКИ КОМАНД =====================
@dp.message(Command("start"))
async def start_cmd(message: Message, command: CommandObject):
    user = message.from_user
    referred_by = None
    if command.args:
        try:
            referred_by = int(command.args)
        except:
            pass
    create_user_if_not_exists(user.id, user.username, user.full_name, referred_by)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile"),
         InlineKeyboardButton(text="🔒 Создать сделку", callback_data="menu_deal")],
        [InlineKeyboardButton(text="🛒 Магазин", callback_data="menu_shop"),
         InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referral")],
        [InlineKeyboardButton(text="📋 Помощь", callback_data="menu_help"),
         InlineKeyboardButton(text="⚠️ Сообщить о скамере", callback_data="menu_report_scam")],
    ])
    if is_admin(user.id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="menu_admin")])
    await message.answer("👋 Добро пожаловать! Выберите действие:", reply_markup=keyboard)

# ------ Меню (обработчики callback) ------
@dp.callback_query(lambda c: c.data.startswith("menu_"))
async def menu_callback(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split("_")[1]
    if action == "profile":
        await show_profile(callback.message, callback.from_user.id)
    elif action == "deal":
        await create_deal_start(callback.message, callback.from_user.id, state)
    elif action == "shop":
        await show_shop(callback.message)
    elif action == "referral":
        await show_referral(callback.message, callback.from_user.id)
    elif action == "help":
        await show_help(callback.message)
    elif action == "admin":
        if is_admin(callback.from_user.id):
            await admin_panel(callback.message)
        else:
            await callback.answer("Нет доступа", show_alert=True)
    elif action == "main":
        await start_cmd(callback.message, None)
    elif action == "report_scam":
        await report_scam_start(callback.message, callback.from_user.id, state)
    await callback.answer()

# ------ Профиль ------
async def show_profile(message: Message, user_id: int = None):
    if not user_id:
        user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Напишите /start")
        return
    text = (
        f"📋 Профиль @{user[1] or 'без username'}\n"
        f"👤 Имя: {user[2]}\n"
        f"📊 Уровень: {user[3]}\n"
        f"⭐ Опыт: {user[4]} XP (всего {user[5]})\n"
        f"💰 Монеты: {user[8]}\n"
        f"⚠️ Предупреждения: {user[9]}\n"
        f"🔗 Рефералов: {user[12] or 0}\n"
        f"💳 Скидка на комиссию: {user[15] * 100}%\n"
        f"🚀 Без очереди: {'Да' if user[16] else 'Нет'}\n"
    )
    if user[14]:
        text += f"🏷️ Активный префикс: {user[14]}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать био", callback_data="edit_bio")],
        [InlineKeyboardButton(text="📝 Мои отзывы", callback_data="my_feedbacks")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    if is_admin(message.from_user.id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="menu_admin")])
    await message.edit_text(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "edit_bio")
async def edit_bio_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✏️ Отправьте новую биографию:")
    await state.set_state(AdminStates.waiting_for_level_name)  # временно используем состояние
    await callback.answer()

@dp.message(AdminStates.waiting_for_level_name)  # обработчик для био
async def process_bio(message: Message, state: FSMContext):
    update_user_field(message.from_user.id, 'bio', message.text)
    await state.clear()
    await message.answer("✅ Биография обновлена!")

@dp.callback_query(lambda c: c.data == "my_feedbacks")
async def my_feedbacks(callback: CallbackQuery):
    user_id = callback.from_user.id
    feedbacks = get_feedbacks_for_guarantor(user_id, 10)
    if not feedbacks:
        await callback.message.edit_text("📭 У вас пока нет отзывов.")
        return
    text = "📝 Ваши последние отзывы:\n\n"
    for fb in feedbacks:
        text += f"От пользователя {fb[1]} | Оценка: {'⭐'*fb[2]}\n{fb[3][:100]}\n{fb[4]}\n---\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_profile")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ------ Создание сделки ------
@dp.callback_query(lambda c: c.data == "menu_deal")
async def create_deal_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔒 Создание сделки.\nВведите @username продавца:")
    await state.set_state(DealStates.waiting_for_seller)
    await callback.answer()

@dp.message(DealStates.waiting_for_seller)
async def deal_seller(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    if not username:
        await message.answer("❌ Введите корректный username.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        await message.answer("❌ Пользователь не найден. Попробуйте ещё раз.")
        return
    seller_id = row[0]
    if seller_id == message.from_user.id:
        await message.answer("❌ Вы не можете быть продавцом сами себе.")
        return
    await state.update_data(seller_id=seller_id, seller_username=username)
    await message.answer("💰 Введите сумму сделки (в USDT):")
    await state.set_state(DealStates.waiting_for_amount)

@dp.message(DealStates.waiting_for_amount)
async def deal_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    await state.update_data(amount=amount)
    await message.answer("📝 Введите краткое описание сделки:")
    await state.set_state(DealStates.waiting_for_description)

@dp.message(DealStates.waiting_for_description)
async def deal_description(message: Message, state: FSMContext):
    description = message.text
    data = await state.get_data()
    buyer_id = message.from_user.id
    seller_id = data['seller_id']
    seller_username = data.get('seller_username', 'без username')
    amount = data['amount']

    buyer_level = get_user_level(buyer_id)
    seller_level = get_user_level(seller_id)
    levels = get_levels()
    priority = 0
    for level_name, xp_req, bonus, comm, noq in levels:
        if level_name == buyer_level or level_name == seller_level:
            if noq:
                priority = 1
                break

    request_id = create_deal_request(buyer_id, seller_id, amount, description, priority)
    free_guarantors = get_free_guarantors()
    if not free_guarantors:
        await message.answer("😔 Нет свободных гарантов. Ваша заявка будет в очереди.")
        await state.clear()
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Провести сделку", callback_data=f"take_deal_{request_id}")]
    ])

    for guarantor_id, vip_chat, comm in free_guarantors:
        try:
            await bot.send_message(
                guarantor_id,
                f"🔔 Новая заявка на сделку!\n"
                f"Покупатель: @{message.from_user.username or 'без username'}\n"
                f"Продавец: @{seller_username}\n"
                f"Сумма: {amount} USDT\n"
                f"Описание: {description}\n"
                f"Приоритет: {'Да' if priority else 'Нет'}",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить гаранта {guarantor_id}: {e}")

    await state.clear()
    await message.answer("✅ Заявка создана. Ожидайте, когда гарант примет сделку.")

@dp.callback_query(lambda c: c.data.startswith("take_deal_"))
async def take_deal(callback: CallbackQuery):
    request_id = int(callback.data.split("_")[2])
    guarantor_id = callback.from_user.id
    if not is_guarantor(guarantor_id):
        await callback.answer("Вы не являетесь гарантом", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT current_deal_id FROM guarantors WHERE user_id = ?", (guarantor_id,))
    row = cur.fetchone()
    if row and row[0] is not None:
        await callback.answer("У вас уже есть активная сделка", show_alert=True)
        conn.close()
        return
    conn.close()

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT buyer_id, seller_id, amount, description, priority FROM deal_requests WHERE id = ? AND status = 'waiting'",
        (request_id,)
    )
    request = cur.fetchone()
    if not request:
        conn.close()
        await callback.answer("Эта заявка уже не актуальна", show_alert=True)
        return
    cur.execute("UPDATE deal_requests SET status = 'taken' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()

    buyer_id, seller_id, amount, description, priority = request

    vip_threshold_amount = float(get_setting('vip_threshold_amount') or 500)
    vip_threshold_level = get_setting('vip_threshold_level') or 'Эксперт'
    buyer_level = get_user_level(buyer_id)
    seller_level = get_user_level(seller_id)
    levels = get_levels()
    level_names = [l[0] for l in levels]
    need_vip = False
    if amount >= vip_threshold_amount:
        need_vip = True
    if buyer_level in level_names and level_names.index(buyer_level) >= level_names.index(vip_threshold_level):
        need_vip = True
    if seller_level in level_names and level_names.index(seller_level) >= level_names.index(vip_threshold_level):
        need_vip = True

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT commission_rate, vip_chat_id FROM guarantors WHERE user_id = ?", (guarantor_id,))
    guarantor_data = cur.fetchone()
    conn.close()
    commission_rate = guarantor_data[0] if guarantor_data else 0.02
    vip_chat_id = guarantor_data[1] if guarantor_data else get_vip_chat_id()

    deal_id = create_deal(buyer_id, seller_id, guarantor_id, amount, description, vip_chat_id, need_vip, commission_rate)
    set_guarantor_deal(guarantor_id, deal_id)

    await bot.send_message(buyer_id, f"✅ Ваша сделка принята гарантом @{callback.from_user.username or 'без username'}\nСумма: {amount} USDT")
    await bot.send_message(seller_id, f"✅ Ваша сделка принята гарантом @{callback.from_user.username or 'без username'}\nСумма: {amount} USDT")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 VIP-офис", callback_data=f"deal_vip_{deal_id}")],
    ])
    if not need_vip:
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="💬 Общий чат", callback_data=f"deal_public_{deal_id}")])

    await callback.message.edit_text(
        f"🔑 Вы стали гарантом сделки #{deal_id}\n"
        f"Сумма: {amount} USDT\n"
        f"Описание: {description}\n"
        f"Выберите место проведения:",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("deal_vip_") or c.data.startswith("deal_public_"))
async def choose_place(callback: CallbackQuery):
    data = callback.data.split("_")
    place = data[1]
    deal_id = int(data[2])
    deal = get_deal(deal_id)
    if not deal:
        await callback.answer("Сделка не найдена", show_alert=True)
        return
    buyer_id, seller_id, guarantor_id, amount, description, status, vip_chat_id, created_at, completed_at, is_vip, commission = deal[1], deal[2], deal[3], deal[4], deal[5], deal[6], deal[7], deal[8], deal[9], deal[10], deal[11]

    if place == "vip":
        if not vip_chat_id:
            await callback.answer("VIP-чат не настроен у гаранта", show_alert=True)
            return
        try:
            link = await bot.create_chat_invite_link(
                vip_chat_id,
                member_limit=3,
                expire_date=datetime.now() + timedelta(hours=1)
            )
            invite_link = link.invite_link
        except Exception as e:
            await callback.answer(f"Ошибка создания ссылки: {e}", show_alert=True)
            return
        update_deal_status(deal_id, 'vip_created')
        await bot.send_message(buyer_id, f"🏢 Ссылка на VIP-офис: {invite_link}")
        await bot.send_message(seller_id, f"🏢 Ссылка на VIP-офис: {invite_link}")
        await callback.message.edit_text("✅ VIP-офис создан. Ссылки отправлены участникам.")
        await bot.send_message(guarantor_id, "🏢 VIP-офис создан. После завершения сделки напишите /complete в этом чате или в VIP-чате.")
    else:
        update_deal_status(deal_id, 'public')
        await callback.message.edit_text("💬 Сделка будет проведена в общем чате.")
        await bot.send_message(buyer_id, "💬 Сделка будет проведена в общем чате.")
        await bot.send_message(seller_id, "💬 Сделка будет проведена в общем чате.")
    await callback.answer()

# ------ Завершение сделки ------
@dp.message(Command("complete"))
async def complete_deal(message: Message):
    guarantor_id = message.from_user.id
    if not is_guarantor(guarantor_id):
        await message.answer("Вы не являетесь гарантом.")
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, buyer_id, seller_id, amount, vip_chat_id, is_vip, commission FROM deals WHERE guarantor_id = ? AND status IN ('vip_created', 'public')",
        (guarantor_id,)
    )
    deal = cur.fetchone()
    if not deal:
        conn.close()
        await message.answer("У вас нет активных сделок.")
        return
    deal_id, buyer_id, seller_id, amount, vip_chat_id, is_vip, commission = deal
    update_deal_status(deal_id, 'completed')
    clear_guarantor_deal(guarantor_id)

    cur.execute("UPDATE guarantors SET total_deals = total_deals + 1 WHERE user_id = ?", (guarantor_id,))
    conn.commit()
    conn.close()

    xp_percent = int(get_setting('deal_xp_percent') or 5)
    xp_gain = int(amount * xp_percent / 100) if xp_percent else 0
    add_xp(buyer_id, xp_gain)
    add_xp(seller_id, xp_gain)
    add_xp(guarantor_id, xp_gain)

    commission_currency = get_setting('commission_currency') or 'coins'
    if commission_currency == 'coins':
        commission_coins = int(amount * commission) if commission else 0
        add_coins(guarantor_id, commission_coins)
        commission_text = f"{commission_coins} монет"
    else:
        commission_usdt = amount * commission
        commission_text = f"{commission_usdt:.2f} USDT (не начисляется в боте)"

    await message.answer(f"✅ Сделка #{deal_id} завершена. Начислено {xp_gain} XP участникам, комиссия гаранта: {commission_text}.")
    await bot.send_message(buyer_id, f"✅ Сделка #{deal_id} завершена. Начислено {xp_gain} XP.")
    await bot.send_message(seller_id, f"✅ Сделка #{deal_id} завершена. Начислено {xp_gain} XP.")

    if is_vip and vip_chat_id:
        await message.answer("🔜 Через 5 минут все участники будут удалены из VIP-чата.")
        await asyncio.sleep(300)
        try:
            members = await bot.get_chat_administrators(vip_chat_id)
            for member in members:
                if not is_admin(member.user.id):
                    await bot.ban_chat_member(vip_chat_id, member.user.id)
                    await bot.unban_chat_member(vip_chat_id, member.user.id)
        except Exception as e:
            logging.error(f"Ошибка удаления участников: {e}")
        await message.answer("🗑️ VIP-офис очищен.")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Оставить отзыв о гаранте", callback_data=f"feedback_{guarantor_id}")]
    ])
    await bot.send_message(buyer_id, "📝 Пожалуйста, оцените работу гаранта.", reply_markup=keyboard)
    await bot.send_message(seller_id, "📝 Пожалуйста, оцените работу гаранта.", reply_markup=keyboard)

# ------ Отзывы ------
@dp.callback_query(lambda c: c.data.startswith("feedback_"))
async def feedback_start(callback: CallbackQuery, state: FSMContext):
    guarantor_id = int(callback.data.split("_")[1])
    await state.update_data(guarantor_id=guarantor_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{i} ⭐", callback_data=f"rate_{i}") for i in range(1, 6)]
    ])
    await callback.message.edit_text("Оцените гаранта (1-5):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("rate_"))
async def feedback_rating(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    await callback.message.edit_text("✏️ Напишите текстовый отзыв:")
    await state.set_state(FeedbackStates.waiting_for_text)
    await callback.answer()

@dp.message(FeedbackStates.waiting_for_text)
async def feedback_text(message: Message, state: FSMContext):
    data = await state.get_data()
    guarantor_id = data['guarantor_id']
    rating = data['rating']
    text = message.text
    add_feedback(message.from_user.id, guarantor_id, rating, text)
    await state.clear()
    await message.answer("✅ Спасибо за отзыв!")

# ------ Магазин ------
@dp.callback_query(lambda c: c.data == "menu_shop")
async def show_shop(message: Message):
    items = get_store_items()
    if not items:
        await message.edit_text("🛒 Магазин пуст.")
        return
    text = "🛒 Магазин:\n\n"
    for item in items:
        text += f"ID: {item[0]} | {item[1]} - {item[3]} монет\n  {item[2]}\n  Тип: {item[4]}, Значение: {item[5]}\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍️ Купить товар", callback_data="buy_item")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    await message.edit_text(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "buy_item")
async def buy_item_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID товара, который хотите купить:")
    await state.set_state(BuyStates.waiting_for_item_id)
    await callback.answer()

@dp.message(BuyStates.waiting_for_item_id)
async def process_buy_item(message: Message, state: FSMContext):
    try:
        item_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    result = buy_item(message.from_user.id, item_id)
    if result is None:
        await message.answer("❌ Товар не найден или недоступен.")
    elif result is False:
        await message.answer("❌ Недостаточно монет.")
    else:
        item_type, item_value = result[2], result[3]
        if item_type == 'prefix':
            add_prefix_request(message.from_user.id, item_value)
            await message.answer(f"✅ Товар '{result[0]}' куплен! Заявка на префикс отправлена администратору на одобрение.")
        else:
            await message.answer(f"✅ Товар '{result[0]}' куплен! Тип: {item_type}, значение: {item_value}.")
    await state.clear()

# ------ Рефералы ------
@dp.callback_query(lambda c: c.data == "menu_referral")
async def show_referral(message: Message, user_id: int = None):
    if not user_id:
        user_id = message.from_user.id
    user = get_user(user_id)
    if not user:
        await message.edit_text("❌ Вы не зарегистрированы.")
        return
    bot_username = (await bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    text = (
        f"👥 Ваша реферальная ссылка:\n{ref_link}\n\n"
        f"Приглашено: {user[12] or 0}\n"
        f"Бонус за приглашение: {get_setting('ref_bonus_coins')} монет и {get_setting('ref_bonus_xp')} XP"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    await message.edit_text(text, reply_markup=keyboard)

# ------ Помощь ------
@dp.callback_query(lambda c: c.data == "menu_help")
async def show_help(message: Message):
    text = (
        "📋 Помощь по боту\n\n"
        "🔒 Создать сделку: нажмите кнопку и следуйте инструкциям.\n"
        "🛒 Магазин: покупка префиксов, ролей, скидок.\n"
        "👥 Рефералы: ваша реферальная ссылка.\n"
        "👤 Профиль: просмотр и редактирование.\n"
        "⚠️ Сообщить о скамере: отправьте жалобу на мошенника.\n\n"
        "Для гарантов:\n"
        "- /complete - завершить активную сделку.\n"
        "- /setvipchat - установить свой VIP-чат.\n\n"
        "Для администраторов:\n"
        "- /admin - панель управления."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")]
    ])
    await message.edit_text(text, reply_markup=keyboard)

# ------ Сообщить о скамере ------
@dp.callback_query(lambda c: c.data == "menu_report_scam")
async def report_scam_start(message: Message, user_id: int, state: FSMContext):
    await message.edit_text("⚠️ Сообщение о скамере.\nВведите @username мошенника:")
    await state.set_state(ReportScamStates.waiting_for_username)
    await message.answer()

@dp.message(ReportScamStates.waiting_for_username)
async def report_scam_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    if not username:
        await message.answer("❌ Введите корректный username.")
        return
    await state.update_data(reported_username=username)
    await message.answer("📝 Введите доказательства (скриншоты, ссылки, описание):")
    await state.set_state(ReportScamStates.waiting_for_evidence)

@dp.message(ReportScamStates.waiting_for_evidence)
async def report_scam_evidence(message: Message, state: FSMContext):
    evidence = message.text
    data = await state.get_data()
    reported_username = data['reported_username']
    reporter_id = message.from_user.id
    report_id = add_scam_report(reported_username, evidence, reporter_id)
    for admin_id in INITIAL_ADMINS:
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_scam_{report_id}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_scam_{report_id}")]
            ])
            await bot.send_message(
                admin_id,
                f"🆕 Новая заявка на скамера #{report_id}\n"
                f"Username: @{reported_username}\n"
                f"Доказательства: {evidence}\n"
                f"От: @{message.from_user.username or 'без username'}",
                reply_markup=keyboard
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить админа {admin_id}: {e}")
    await state.clear()
    await message.answer("✅ Заявка отправлена на модерацию. Администраторы проверят её в ближайшее время.")

# ------ Обработчики одобрения/отклонения скам-заявок (админы) ------
@dp.callback_query(lambda c: c.data.startswith("approve_scam_"))
async def approve_scam(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    report_id = int(callback.data.split("_")[2])
    if approve_scam_report(report_id, callback.from_user.id):
        await callback.message.edit_text(f"✅ Заявка #{report_id} одобрена. Скамер добавлен в базу.")
        await callback.answer()
    else:
        await callback.answer("Ошибка при одобрении", show_alert=True)

@dp.callback_query(lambda c: c.data.startswith("reject_scam_"))
async def reject_scam(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    report_id = int(callback.data.split("_")[2])
    reject_scam_report(report_id, "Отклонено админом")
    await callback.message.edit_text(f"❌ Заявка #{report_id} отклонена.")
    await callback.answer()
# ===================== АДМИН-ПАНЕЛЬ (ОСНОВНОЕ МЕНЮ) =====================
@dp.callback_query(lambda c: c.data == "menu_admin")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.edit_text("⛔ Нет доступа.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 Управление гарантами", callback_data="admin_guarantors")],
        [InlineKeyboardButton(text="📊 Уровни и опыт", callback_data="admin_levels")],
        [InlineKeyboardButton(text="🏪 Управление магазином", callback_data="admin_shop_manage")],
        [InlineKeyboardButton(text="🔞 Модерация", callback_data="admin_moderation")],
        [InlineKeyboardButton(text="✅ Одобрение префиксов", callback_data="admin_prefixes")],
        [InlineKeyboardButton(text="👥 Управление пользователями", callback_data="admin_users")],
        [InlineKeyboardButton(text="🚫 Скам-база", callback_data="admin_scammers")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки VIP", callback_data="admin_vip_settings")],
        [InlineKeyboardButton(text="📝 Управление отзывами", callback_data="admin_feedbacks")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="menu_main")],
    ])
    await message.edit_text("👑 Админ-панель:", reply_markup=keyboard)

# ----- Управление гарантами -----
@dp.callback_query(lambda c: c.data == "admin_guarantors")
async def admin_guarantors(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, is_active, current_deal_id, total_deals, rating, feedback_count, vip_chat_id, commission_rate FROM guarantors"
    )
    rows = cur.fetchall()
    conn.close()
    text = "👑 Список гарантов:\n\n"
    if not rows:
        text += "Нет гарантов."
    for row in rows:
        text += f"ID: {row[0]} | Активен: {'Да' if row[1] else 'Нет'} | Сделок: {row[3]}\n"
        text += f"Рейтинг: {row[4]:.2f} (отзывов: {row[5]}) | VIP-чат: {row[6] or 'не задан'} | Комиссия: {row[7]*100}%\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить гаранта", callback_data="admin_add_guarantor")],
        [InlineKeyboardButton(text="❌ Удалить гаранта", callback_data="admin_remove_guarantor")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_guarantor")
async def add_guarantor_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID пользователя (число):")
    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_search)
async def process_add_guarantor(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    user = get_user(user_id)
    if not user:
        await message.answer("❌ Пользователь не найден.")
        return
    add_guarantor(user_id)
    await state.clear()
    await message.answer(f"✅ Гарант @{user[1] or 'без username'} добавлен.")

@dp.callback_query(lambda c: c.data == "admin_remove_guarantor")
async def remove_guarantor_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID пользователя, которого хотите удалить из гарантов:")
    await state.set_state(AdminStates.waiting_for_remove_guarantor)
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_guarantor)
async def process_remove_guarantor(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    if not is_guarantor(user_id):
        await message.answer("❌ Этот пользователь не является гарантом.")
        return
    remove_guarantor(user_id)
    await state.clear()
    await message.answer("✅ Гарант удалён.")

# ----- Управление уровнями -----
@dp.callback_query(lambda c: c.data == "admin_levels")
async def admin_levels(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    levels = get_levels()
    text = "📊 Текущие уровни:\n"
    for name, xp, bonus, comm, noq in levels:
        text += f"{name} — {xp} XP, бонус: {bonus} монет, комиссия: {comm*100}%, без очереди: {'Да' if noq else 'Нет'}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить уровень", callback_data="admin_add_level")],
        [InlineKeyboardButton(text="❌ Удалить уровень", callback_data="admin_del_level")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_level")
async def add_level_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название нового уровня:")
    await state.set_state(AdminStates.waiting_for_level_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_level_name)
async def process_level_name(message: Message, state: FSMContext):
    await state.update_data(level_name=message.text)
    await message.answer("Введите требуемое количество XP:")
    await state.set_state(AdminStates.waiting_for_level_xp)

@dp.message(AdminStates.waiting_for_level_xp)
async def process_level_xp(message: Message, state: FSMContext):
    try:
        xp = int(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_xp=xp)
    await message.answer("Введите бонусные монеты за достижение уровня:")
    await state.set_state(AdminStates.waiting_for_level_bonus)

@dp.message(AdminStates.waiting_for_level_bonus)
async def process_level_bonus(message: Message, state: FSMContext):
    try:
        bonus = int(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_bonus=bonus)
    await message.answer("Введите комиссию (в долях, например 0.02 = 2%):")
    await state.set_state(AdminStates.waiting_for_level_commission)

@dp.message(AdminStates.waiting_for_level_commission)
async def process_level_commission(message: Message, state: FSMContext):
    try:
        comm = float(message.text)
    except:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(level_commission=comm)
    await message.answer("Даёт ли уровень привилегию 'без очереди'? (1 - да, 0 - нет):")
    await state.set_state(AdminStates.waiting_for_level_noqueue)

@dp.message(AdminStates.waiting_for_level_noqueue)
async def process_level_noqueue(message: Message, state: FSMContext):
    try:
        noq = int(message.text)
    except:
        await message.answer("❌ Введите 0 или 1.")
        return
    data = await state.get_data()
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO levels (name, xp_required, bonus_coins, commission_rate, no_queue) VALUES (?, ?, ?, ?, ?)",
        (data['level_name'], data['level_xp'], data['level_bonus'], data['level_commission'], noq)
    )
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer("✅ Уровень добавлен!")

@dp.callback_query(lambda c: c.data == "admin_del_level")
async def del_level_start(callback: CallbackQuery):
    levels = get_levels()
    if len(levels) <= 1:
        await callback.answer("Нельзя удалить единственный уровень", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"del_level_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите уровень для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("del_level_"))
async def confirm_del_level(callback: CallbackQuery):
    name = callback.data.split("_", 2)[2]
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("DELETE FROM levels WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Уровень '{name}' удалён.")
    await admin_levels(callback)

# ----- Управление магазином -----
@dp.callback_query(lambda c: c.data == "admin_shop_manage")
async def admin_shop_manage(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_item")],
        [InlineKeyboardButton(text="❌ Удалить товар", callback_data="admin_remove_item")],
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list_items")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text("🏪 Управление магазином:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_item")
async def add_item_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название товара:")
    await state.set_state(AdminStates.waiting_for_item_name)
    await callback.answer()

@dp.message(AdminStates.waiting_for_item_name)
async def process_item_name(message: Message, state: FSMContext):
    await state.update_data(item_name=message.text)
    await message.answer("Введите описание товара:")
    await state.set_state(AdminStates.waiting_for_item_desc)

@dp.message(AdminStates.waiting_for_item_desc)
async def process_item_desc(message: Message, state: FSMContext):
    await state.update_data(item_desc=message.text)
    await message.answer("Введите цену в монетах:")
    await state.set_state(AdminStates.waiting_for_item_price)

@dp.message(AdminStates.waiting_for_item_price)
async def process_item_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    await state.update_data(item_price=price)
    await message.answer("Введите тип товара (prefix / role / discount):")
    await state.set_state(AdminStates.waiting_for_item_type)

@dp.message(AdminStates.waiting_for_item_type)
async def process_item_type(message: Message, state: FSMContext):
    typ = message.text.lower()
    if typ not in ['prefix', 'role', 'discount']:
        await message.answer("❌ Тип должен быть: prefix, role или discount.")
        return
    await state.update_data(item_type=typ)
    await message.answer("Введите значение товара (например, для prefix это текст префикса):")
    await state.set_state(AdminStates.waiting_for_item_value)

@dp.message(AdminStates.waiting_for_item_value)
async def process_item_value(message: Message, state: FSMContext):
    value = message.text
    data = await state.get_data()
    add_store_item(data['item_name'], data['item_desc'], data['item_price'], data['item_type'], value)
    await state.clear()
    await message.answer("✅ Товар добавлен!")

@dp.callback_query(lambda c: c.data == "admin_remove_item")
async def remove_item_start(callback: CallbackQuery):
    items = get_store_items()
    if not items:
        await callback.answer("Нет товаров для удаления", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{item[1]} (ID: {item[0]})", callback_data=f"remove_item_{item[0]}")] for item in items
    ])
    await callback.message.edit_text("Выберите товар для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_item_"))
async def confirm_remove_item(callback: CallbackQuery):
    item_id = int(callback.data.split("_")[2])
    remove_store_item(item_id)
    await callback.message.edit_text(f"✅ Товар удалён.")
    await admin_shop_manage(callback)

@dp.callback_query(lambda c: c.data == "admin_list_items")
async def admin_list_items(callback: CallbackQuery):
    items = get_store_items()
    if not items:
        text = "📭 Магазин пуст."
    else:
        text = "📋 Список товаров:\n\n"
        for item in items:
            text += f"ID: {item[0]} | {item[1]} - {item[3]} монет\n  {item[2]}\n  Тип: {item[4]}, Значение: {item[5]}\n\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_shop_manage")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Управление модерацией -----
@dp.callback_query(lambda c: c.data == "admin_moderation")
async def admin_moderation(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    moderation_enabled = get_setting('moderation_enabled') == '1'
    filter_links = get_setting('filter_links') == '1'
    filter_badwords = get_setting('filter_badwords') == '1'
    xp_per_message = get_setting('xp_per_message') or '1'
    text = (
        f"🔞 Настройки модерации:\n"
        f"Модерация включена: {'Да' if moderation_enabled else 'Нет'}\n"
        f"Фильтр ссылок: {'Да' if filter_links else 'Нет'}\n"
        f"Фильтр мата: {'Да' if filter_badwords else 'Нет'}\n"
        f"XP за сообщение: {xp_per_message}\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Переключить модерацию", callback_data="admin_toggle_moderation")],
        [InlineKeyboardButton(text="🔄 Переключить фильтр ссылок", callback_data="admin_toggle_links")],
        [InlineKeyboardButton(text="🔄 Переключить фильтр мата", callback_data="admin_toggle_badwords")],
        [InlineKeyboardButton(text="📝 Управление чёрным списком", callback_data="admin_blacklist")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_toggle_moderation")
async def toggle_moderation(callback: CallbackQuery):
    val = get_setting('moderation_enabled')
    new_val = '0' if val == '1' else '1'
    set_setting('moderation_enabled', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_toggle_links")
async def toggle_links(callback: CallbackQuery):
    val = get_setting('filter_links')
    new_val = '0' if val == '1' else '1'
    set_setting('filter_links', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_toggle_badwords")
async def toggle_badwords(callback: CallbackQuery):
    val = get_setting('filter_badwords')
    new_val = '0' if val == '1' else '1'
    set_setting('filter_badwords', new_val)
    await admin_moderation(callback)

@dp.callback_query(lambda c: c.data == "admin_blacklist")
async def admin_blacklist(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    words = get_badwords()
    text = "📋 Чёрный список слов:\n" + ("\n".join(words) if words else "Список пуст.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить слово", callback_data="admin_add_badword")],
        [InlineKeyboardButton(text="❌ Удалить слово", callback_data="admin_remove_badword")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_moderation")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_badword")
async def add_badword_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите слово для добавления в чёрный список:")
    await state.set_state(AdminStates.waiting_for_badword)
    await callback.answer()

@dp.message(AdminStates.waiting_for_badword)
async def process_add_badword(message: Message, state: FSMContext):
    word = message.text.lower().strip()
    add_badword(word)
    await state.clear()
    await message.answer(f"✅ Слово '{word}' добавлено в чёрный список.")

@dp.callback_query(lambda c: c.data == "admin_remove_badword")
async def remove_badword_start(callback: CallbackQuery):
    words = get_badwords()
    if not words:
        await callback.answer("Список пуст", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=word, callback_data=f"remove_badword_{word}")] for word in words
    ])
    await callback.message.edit_text("Выберите слово для удаления:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("remove_badword_"))
async def confirm_remove_badword(callback: CallbackQuery):
    word = callback.data.split("_", 2)[2]
    remove_badword(word)
    await callback.message.edit_text(f"✅ Слово '{word}' удалено из чёрного списка.")
    await admin_blacklist(callback)

# ----- Одобрение префиксов -----
@dp.callback_query(lambda c: c.data == "admin_prefixes")
async def admin_prefixes(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    pending = get_pending_prefix_requests()
    if not pending:
        await callback.message.edit_text("📭 Нет заявок на префиксы.")
        return
    text = "✅ Заявки на префиксы:\n\n"
    for req in pending:
        user = get_user(req[1])
        username = user[1] if user else 'без username'
        text += f"ID: {req[0]} | Пользователь: @{username} | Префикс: {req[2]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одобрить", callback_data=f"approve_prefix_{req[0]}"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"reject_prefix_{req[0]}")] for req in pending
    ])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("approve_prefix_"))
async def approve_prefix(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    if approve_prefix_request(request_id):
        await callback.message.edit_text("✅ Префикс одобрен.")
    else:
        await callback.answer("Ошибка", show_alert=True)
    await admin_prefixes(callback)

@dp.callback_query(lambda c: c.data.startswith("reject_prefix_"))
async def reject_prefix(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    reject_prefix_request(request_id)
    await callback.message.edit_text("❌ Префикс отклонён.")
    await admin_prefixes(callback)

# ----- Управление пользователями -----
@dp.callback_query(lambda c: c.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="📊 Топ пользователей", callback_data="admin_top_users")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text("👥 Управление пользователями:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_search_user")
async def search_user_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username или ID пользователя:")
    await state.set_state(AdminStates.waiting_for_user_search)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_search)
async def process_search_user(message: Message, state: FSMContext):
    query = message.text.strip()
    if query.startswith('@'):
        username = query.lstrip('@')
        user = get_user_by_username(username)
        if user:
            await show_user_info(message, user)
        else:
            await message.answer("❌ Пользователь не найден.")
    else:
        try:
            user_id = int(query)
            user = get_user(user_id)
            if user:
                await show_user_info(message, user)
            else:
                await message.answer("❌ Пользователь не найден.")
        except:
            await message.answer("❌ Введите корректный username или ID.")
    await state.clear()

async def show_user_info(message: Message, user):
    text = (
        f"👤 Информация о пользователе @{user[1] or 'без username'}\n"
        f"ID: {user[0]}\n"
        f"Уровень: {user[3]}\n"
        f"Опыт: {user[4]} XP (всего {user[5]})\n"
        f"Монеты: {user[8]}\n"
        f"Предупреждения: {user[9]}\n"
        f"Рефералов: {user[12] or 0}\n"
        f"Активный префикс: {user[14] or 'нет'}\n"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Начислить XP", callback_data=f"admin_addxp_{user[0]}")],
        [InlineKeyboardButton(text="💰 Начислить монеты", callback_data=f"admin_addcoins_{user[0]}")],
        [InlineKeyboardButton(text="📊 Сменить уровень", callback_data=f"admin_setlevel_{user[0]}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")],
    ])
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("admin_addxp_"))
async def admin_addxp_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    await callback.message.edit_text("Введите количество XP для начисления:")
    await state.set_state(AdminStates.waiting_for_user_xp)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_xp)
async def process_addxp(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    user_id = data['target_user_id']
    add_xp(user_id, amount)
    await state.clear()
    await message.answer(f"✅ Начислено {amount} XP.")

@dp.callback_query(lambda c: c.data.startswith("admin_addcoins_"))
async def admin_addcoins_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    await callback.message.edit_text("Введите количество монет для начисления:")
    await state.set_state(AdminStates.waiting_for_user_coins)
    await callback.answer()

@dp.message(AdminStates.waiting_for_user_coins)
async def process_addcoins(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    data = await state.get_data()
    user_id = data['target_user_id']
    add_coins(user_id, amount)
    await state.clear()
    await message.answer(f"✅ Начислено {amount} монет.")

@dp.callback_query(lambda c: c.data.startswith("admin_setlevel_"))
async def admin_setlevel_start(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split("_")[2])
    await state.update_data(target_user_id=user_id)
    levels = get_levels()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"setlevel_{user_id}_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите новый уровень:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("setlevel_"))
async def process_setlevel(callback: CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[1])
    level_name = parts[2]
    update_user_field(user_id, 'level', level_name)
    await callback.message.edit_text(f"✅ Уровень изменён на {level_name}.")
    await admin_users(callback)

@dp.callback_query(lambda c: c.data == "admin_top_users")
async def admin_top_users(callback: CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, level, total_xp, coins FROM users ORDER BY total_xp DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        text = "Нет пользователей."
    else:
        text = "🏆 Топ-10 пользователей по опыту:\n\n"
        for i, row in enumerate(rows, 1):
            text += f"{i}. @{row[1] or 'без username'} | Уровень: {row[2]} | XP: {row[3]} | Монет: {row[4]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Управление скам-базой -----
@dp.callback_query(lambda c: c.data == "admin_scammers")
async def admin_scammers(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, username, evidence, added_at FROM scammers ORDER BY id DESC LIMIT 20")
    rows = cur.fetchall()
    conn.close()
    text = "🚫 Скам-база:\n\n"
    if not rows:
        text += "Нет записей."
    else:
        for row in rows:
            text += f"ID: {row[0]} | @{row[1]} | {row[3]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить скамера", callback_data="admin_add_scammer")],
        [InlineKeyboardButton(text="❌ Удалить скамера", callback_data="admin_remove_scammer")],
        [InlineKeyboardButton(text="📋 Ожидающие заявки", callback_data="admin_pending_scam_reports")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_add_scammer")
async def add_scammer_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username скамера:")
    await state.set_state(AdminStates.waiting_for_scammer_username)
    await callback.answer()

@dp.message(AdminStates.waiting_for_scammer_username)
async def process_scammer_username(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    await state.update_data(scammer_username=username)
    await message.answer("Введите доказательства (текст или ссылки):")
    await state.set_state(AdminStates.waiting_for_scammer_evidence)

@dp.message(AdminStates.waiting_for_scammer_evidence)
async def process_scammer_evidence(message: Message, state: FSMContext):
    evidence = message.text
    data = await state.get_data()
    username = data['scammer_username']
    add_scammer(username, evidence, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ Скамер @{username} добавлен в базу.")

@dp.callback_query(lambda c: c.data == "admin_remove_scammer")
async def remove_scammer_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите @username скамера для удаления:")
    await state.set_state(AdminStates.waiting_for_remove_scammer)
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_scammer)
async def process_remove_scammer(message: Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    remove_scammer(username)
    await state.clear()
    await message.answer(f"✅ Скамер @{username} удалён из базы.")

@dp.callback_query(lambda c: c.data == "admin_pending_scam_reports")
async def admin_pending_scam_reports(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    reports = get_pending_scam_reports()
    if not reports:
        await callback.message.edit_text("📭 Нет ожидающих заявок на скамеров.")
        return
    text = "📋 Ожидающие заявки:\n\n"
    for rep in reports:
        text += f"ID: {rep[0]} | @{rep[1]} | От: {rep[3]} | {rep[4]}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_scam_{rep[0]}"),
         InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_scam_{rep[0]}")] for rep in reports
    ])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_scammers")])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# ----- Рассылка -----
@dp.callback_query(lambda c: c.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.message.edit_text("📢 Введите текст для рассылки (можно с Markdown):")
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.answer()

@dp.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    text = message.text
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    conn.close()
    count = 0
    for user in users:
        try:
            await bot.send_message(user[0], text, parse_mode="HTML")
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await state.clear()
    await message.answer(f"✅ Рассылка завершена. Отправлено {count} пользователям.")

# ----- Настройки VIP -----
@dp.callback_query(lambda c: c.data == "admin_vip_settings")
async def admin_vip_settings(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    current_amount = get_setting('vip_threshold_amount') or '500'
    current_level = get_setting('vip_threshold_level') or 'Эксперт'
    text = f"⚙️ Настройки VIP:\nПорог суммы: {current_amount} USDT\nПорог уровня: {current_level}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Изменить порог суммы", callback_data="admin_set_vip_amount")],
        [InlineKeyboardButton(text="📊 Изменить порог уровня", callback_data="admin_set_vip_level")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_set_vip_amount")
async def set_vip_amount_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый порог суммы для VIP (в USDT):")
    await state.set_state(AdminStates.waiting_for_vip_threshold_amount)
    await callback.answer()

@dp.message(AdminStates.waiting_for_vip_threshold_amount)
async def process_vip_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text)
        if amount < 0:
            raise ValueError
    except:
        await message.answer("❌ Введите положительное число.")
        return
    set_setting('vip_threshold_amount', str(amount))
    await state.clear()
    await message.answer(f"✅ Порог суммы установлен: {amount} USDT")

@dp.callback_query(lambda c: c.data == "admin_set_vip_level")
async def set_vip_level_start(callback: CallbackQuery, state: FSMContext):
    levels = get_levels()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=name, callback_data=f"set_vip_level_{name}")] for name, _, _, _, _ in levels
    ])
    await callback.message.edit_text("Выберите пороговый уровень для VIP:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("set_vip_level_"))
async def process_set_vip_level(callback: CallbackQuery):
    level_name = callback.data.split("_", 3)[3]
    set_setting('vip_threshold_level', level_name)
    await callback.message.edit_text(f"✅ Порог уровня установлен: {level_name}")
    await admin_vip_settings(callback)

# ----- Управление отзывами -----
@dp.callback_query(lambda c: c.data == "admin_feedbacks")
async def admin_feedbacks(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT id, from_user, to_user, rating, text, timestamp FROM feedbacks WHERE deleted = 0 ORDER BY timestamp DESC LIMIT 10")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        text = "📭 Нет отзывов."
    else:
        text = "📝 Последние отзывы:\n\n"
        for row in rows:
            text += f"ID: {row[0]} | От: {row[1]} | Гаранту: {row[2]} | Оценка: {'⭐'*row[3]}\n{row[4][:100]}\n{row[5]}\n---\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить отзыв", callback_data="admin_delete_feedback")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu_admin")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_delete_feedback")
async def delete_feedback_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите ID отзыва для удаления:")
    await state.set_state(AdminStates.waiting_for_feedback_delete)
    await callback.answer()

@dp.message(AdminStates.waiting_for_feedback_delete)
async def process_delete_feedback(message: Message, state: FSMContext):
    try:
        fb_id = int(message.text.strip())
    except:
        await message.answer("❌ Введите число.")
        return
    delete_feedback(fb_id)
    await state.clear()
    await message.answer(f"✅ Отзыв #{fb_id} удалён.")

# ===================== МОДЕРАЦИЯ (объединённый обработчик) =====================
@dp.message()
async def handle_all_messages(message: Message):
    # Проверка на новых участников
    if message.new_chat_members:
        for member in message.new_chat_members:
            username = member.username or ''
            if is_scammer(username):
                action = get_setting('scam_action') or 'mute'
                try:
                    if action == 'ban':
                        await bot.ban_chat_member(message.chat.id, member.id)
                        await message.answer(f"🚫 Пользователь @{username} забанен (найден в скам-базе).")
                    else:
                        await bot.restrict_chat_member(message.chat.id, member.id, permissions=ChatPermissions(can_send_messages=False))
                        await message.answer(f"🔇 Пользователь @{username} замучен (найден в скам-базе).")
                except Exception as e:
                    logging.error(f"Ошибка при блокировке {username}: {e}")
                for admin_id in INITIAL_ADMINS:
                    await bot.send_message(admin_id, f"🚨 Действие ({action}) применено к скамеру @{username} в чате {message.chat.title}")
        return

    # Модерация обычных сообщений
    if message.from_user.is_bot or is_admin(message.from_user.id):
        return

    # Проверка мута
    user = get_user(message.from_user.id)
    if user and user[9] == 1 and user[10]:
        try:
            mute_until = datetime.fromisoformat(user[10])
            if mute_until > datetime.now():
                await message.delete()
                return
            else:
                update_user_field(message.from_user.id, 'is_muted', 0)
                update_user_field(message.from_user.id, 'mute_until', None)
        except:
            pass

    # Проверка ссылок и мата
    if get_setting('moderation_enabled') == '1':
        if get_setting('filter_links') == '1':
            if re.search(r'(https?://[^\s]+)', message.text or ''):
                allowed = ['t.me', 'ton.org', 'telegram.org']
                if not any(dom in message.text for dom in allowed):
                    await message.delete()
                    sent = await message.answer("🚫 Ссылки запрещены.")
                    await asyncio.sleep(10)
                    await sent.delete()
                    return
        if get_setting('filter_badwords') == '1':
            badwords = get_badwords()
            if any(word in (message.text or '').lower() for word in badwords):
                await message.delete()
                sent = await message.answer("🚫 Нецензурная лексика запрещена.")
                await asyncio.sleep(10)
                await sent.delete()
                return

    # Начисление XP за сообщение
    xp_per_msg = int(get_setting('xp_per_message') or 0)
    if xp_per_msg > 0:
        add_xp(message.from_user.id, xp_per_msg)

# ===================== КОМАНДА ДЛЯ ГАРАНТА: УСТАНОВИТЬ VIP-ЧАТ =====================
@dp.message(Command("setvipchat"))
async def set_vip_chat_command(message: Message):
    if not is_guarantor(message.from_user.id) and not is_admin(message.from_user.id):
        await message.answer("⛔ Вы не являетесь гарантом или администратором.")
        return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Используйте: /setvipchat @канал (или ID чата)")
        return
    chat = args[1].strip()
    if chat.startswith('@'):
        try:
            chat_obj = await bot.get_chat(chat)
            chat_id = chat_obj.id
        except Exception as e:
            await message.answer(f"❌ Не удалось найти чат: {e}")
            return
    else:
        try:
            chat_id = int(chat)
        except:
            await message.answer("❌ Укажите корректный ID или username.")
            return
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("UPDATE guarantors SET vip_chat_id = ? WHERE user_id = ?", (chat_id, message.from_user.id))
    conn.commit()
    conn.close()
    await message.answer(f"✅ VIP-чат установлен (ID: {chat_id})")

# ===================== ЗАПУСК =====================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/health')
def health():
    return "OK"

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    from threading import Thread
    Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))).start()
    asyncio.run(main())

    conn.close()
