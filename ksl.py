import os
import re
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any

import aiosqlite
from PIL import Image
import pytesseract

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Logging configuration
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9018814832:AAEo82pjCyl5pnS920vfJ0f9wMYRQ9VrfMI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8171961583"))
UPI_ID = os.getenv("UPI_ID", "8144054897@ibl")
UPI_NAME = os.getenv("UPI_NAME", "DEBAKI JAYAPURIA")
OFFICIAL_CHANNEL_URL = os.getenv("OFFICIAL_CHANNEL_URL", "https://t.me/TLSUPDATEFF")
ANNOUNCEMENT_CHANNEL_ID = os.getenv("ANNOUNCEMENT_CHANNEL_ID", "@TLSUPDATEFF")
HOW_TO_USE_VIDEO_URL = "https://youtube.com/shorts/FjgLGg-ikRI?si=LEHliVgPKieV_rF_"

PAID_PLAYERS_CHANNEL_ID = os.getenv("PAID_PLAYERS_CHANNEL_ID", "-1002345678901")

QR_IMAGE_PATHS = [
    Path("qr.png"),
    Path("/storage/emulated/0/Download/qr.png"),
    Path("/storage/emulated/0/Download/1000003136.png"),
]

CHANNELS = [
    {"url": "https://t.me/+YVtgZ8b7g_01YTg1", "chat_id": None},
    {"url": "https://t.me/TLSUPDATEFF",       "chat_id": "@TLSUPDATEFF"},
    {"url": "https://t.me/codev2zzzzz",       "chat_id": "@codev2zzzzz"},
]

DB_PATH = "ksl_tournament.db"
WAITING_FF_DETAILS = 1

# ==================== DATABASE LAYER ====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                verified INTEGER DEFAULT 0,
                balance INTEGER DEFAULT 0,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_stats (
                user_id INTEGER PRIMARY KEY,
                tournaments_played INTEGER DEFAULT 0,
                tournaments_won INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                mode TEXT NOT NULL,
                match_date TEXT NOT NULL,
                match_time TEXT NOT NULL,
                slots INTEGER NOT NULL,
                entry_fee INTEGER DEFAULT 0,
                prize TEXT NOT NULL,
                rules TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                room_id TEXT,
                room_password TEXT,
                winner_id INTEGER,
                winner_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                payment_mode TEXT DEFAULT 'free',
                ff_uid TEXT,
                ff_nickname TEXT,
                channel_msg_id INTEGER,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tournament_id, user_id),
                FOREIGN KEY (tournament_id) REFERENCES tournaments(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        try:
            await conn.execute("ALTER TABLE registrations ADD COLUMN ff_uid TEXT;")
            await conn.execute("ALTER TABLE registrations ADD COLUMN ff_nickname TEXT;")
            await conn.execute("ALTER TABLE registrations ADD COLUMN channel_msg_id INTEGER;")
        except Exception:
            pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referee_id INTEGER NOT NULL UNIQUE,
                points_rewarded INTEGER DEFAULT 10,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (referee_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS verified_payments (
                utr TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        default_rules = (
            "📌 <b>KSL Tournament Rules:</b>\n"
            "1. No hacks, scripts, ya 3rd-party config allow nahi hain.\n"
            "2. Match time se 10 minute pehle Room ID & Pass bot par diya jayega.\n"
            "3. Toxicity ya abuse karne par player disqualify kar diya jayega."
        )
        default_support = (
            "🆘 <b>KSL Official Support:</b>\n\n"
            "Tournament queries, payment support ya dispute ke liye admin se direct sampark karein."
        )

        await conn.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('rules', ?)", (default_rules,))
        await conn.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('support', ?)", (default_support,))
        await conn.commit()

async def upsert_user(user_id: int, username: Optional[str], first_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user_id, username or "", first_name))
        await conn.execute("INSERT OR IGNORE INTO player_stats (user_id) VALUES (?)", (user_id,))
        await conn.commit()

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT u.*, s.tournaments_played, s.tournaments_won, s.points
            FROM users u
            LEFT JOIN player_stats s ON u.user_id = s.user_id
            WHERE u.user_id = ?
        """, (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def set_verified(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE users SET verified = 1 WHERE user_id = ?", (user_id,))
        await conn.commit()

async def get_leaderboard_top10() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT u.user_id, u.first_name, u.username, s.points, s.tournaments_won, s.tournaments_played
            FROM player_stats s
            JOIN users u ON s.user_id = u.user_id
            ORDER BY s.points DESC, s.tournaments_won DESC
            LIMIT 10
        """) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

async def get_user_rank(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("""
            SELECT COUNT(*) + 1 FROM player_stats
            WHERE points > (SELECT points FROM player_stats WHERE user_id = ?)
        """, (user_id,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else 1

async def record_referral(referrer_id: int, referee_id: int) -> bool:
    if referrer_id == referee_id:
        return False
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            async with conn.execute("SELECT user_id FROM users WHERE user_id = ?", (referee_id,)) as cur:
                if await cur.fetchone():
                    return False
            async with conn.execute("SELECT id FROM referrals WHERE referee_id = ?", (referee_id,)) as cur:
                if await cur.fetchone():
                    return False

            await conn.execute("""
                INSERT INTO referrals (referrer_id, referee_id, points_rewarded)
                VALUES (?, ?, 10)
            """, (referrer_id, referee_id))
            
            await conn.execute("""
                UPDATE player_stats SET points = points + 10 WHERE user_id = ?
            """, (referrer_id,))
            await conn.commit()
            return True
        except Exception:
            return False

async def get_referral_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else 0

async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else default

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)", (key, value))
        await conn.commit()

# ==================== HELPERS ====================
USER_COOLDOWNS: Dict[int, float] = {}

def check_anti_spam(user_id: int) -> bool:
    now = time.time()
    last = USER_COOLDOWNS.get(user_id, 0.0)
    if now - last < 1.0:
        return False
    USER_COOLDOWNS[user_id] = now
    return True

def parse_fee(fee_val) -> int:
    if isinstance(fee_val, int):
        return fee_val
    numbers = re.findall(r"\d+", str(fee_val))
    return int(numbers[0]) if numbers else 0

def get_qr_path():
    for p in QR_IMAGE_PATHS:
        if p.exists():
            return p
    return None

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# ==================== MENUS & KEYBOARDS ====================
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🏆 Available Tournaments", callback_data="menu_tournaments")],
        [
            InlineKeyboardButton("📊 Leaderboard", callback_data="menu_leaderboard"),
            InlineKeyboardButton("📜 Tournament History", callback_data="menu_history"),
        ],
        [
            InlineKeyboardButton("👤 My Profile", callback_data="menu_profile"),
            InlineKeyboardButton("👥 Refer & Earn", callback_data="menu_referral"),
        ],
        [
            InlineKeyboardButton("💰 Add Money", callback_data="menu_add_money"),
            InlineKeyboardButton("💳 Check Balance", callback_data="menu_check_balance"),
        ],
        [
            InlineKeyboardButton("📋 Rules & Support", callback_data="menu_rules_support"),
            InlineKeyboardButton("▶️ How to Use", url=HOW_TO_USE_VIDEO_URL),
        ],
    ]
    if is_admin(user_id):
        buttons.append([InlineKeyboardButton("⚙️ Admin Control Panel", callback_data="menu_admin_panel")])
    return InlineKeyboardMarkup(buttons)

def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Add Free Match", callback_data="admin_guide_addfree"),
            InlineKeyboardButton("➕ Add Paid Match", callback_data="admin_guide_addpaid"),
        ],
        [
            InlineKeyboardButton("🗑 Remove Free Match", callback_data="admin_rem_list:free"),
            InlineKeyboardButton("🗑 Remove Paid Match", callback_data="admin_rem_list:paid"),
        ],
        [
            InlineKeyboardButton("📢 Send Room (/join)", callback_data="admin_guide_join"),
            InlineKeyboardButton("👑 Declare Winner", callback_data="admin_guide_winner"),
        ],
        [
            InlineKeyboardButton("💵 Add User Balance", callback_data="admin_guide_balance"),
            InlineKeyboardButton("📊 View Admin Stats", callback_data="admin_view_stats"),
        ],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main_menu")],
    ])

def channels_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i, ch in enumerate(CHANNELS, start=1):
        rows.append([InlineKeyboardButton(f"📢 Join Channel {i}", url=ch["url"])])
    rows.append([InlineKeyboardButton("✅ Verify Membership", callback_data="verify_membership")])
    return InlineKeyboardMarkup(rows)

async def check_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    for ch in CHANNELS:
        chat_id = ch.get("chat_id")
        if not chat_id:
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
                return False
            if member.status == ChatMemberStatus.RESTRICTED and not getattr(member, "is_member", True):
                return False
        except Exception:
            return False
    return True

# ==================== HANDLERS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if context.args and context.args[0].startswith("ref_"):
        try:
            ref_str = context.args[0].split("_")[1]
            if ref_str.isdigit():
                ref_id = int(ref_str)
                if await record_referral(ref_id, user.id):
                    try:
                        await context.bot.send_message(
                            chat_id=ref_id,
                            text=f"🎉 <b>Referral Success!</b>\n{user.first_name} joined using your link. +10 Points awarded!",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Referral error: {e}")

    await upsert_user(user.id, user.username, user.first_name)
    user_data = await get_user(user.id)

    if user_data and user_data["verified"]:
        await update.effective_message.reply_text(
            f"🏆 <b>Welcome to KSL TOURNAMENT</b>\n\nWelcome back, <b>{user.first_name}</b>!\nSelect from the options below:",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(user.id),
        )
        return

    welcome_msg = (
        f"🏆 <b>WELCOME TO KSL TOURNAMENT</b> 🎮\n\n"
        f"Namaste, <b>{user.first_name}</b>!\n"
        "Free Fire MAX automated tournaments platform.\n\n"
        "🔐 <b>Security Verification:</b>\n"
        "Niche diye gaye channels ko join karke <b>Verify Membership</b> button dabayein."
    )
    await update.effective_message.reply_text(
        welcome_msg,
        parse_mode=ParseMode.HTML,
        reply_markup=channels_keyboard(),
    )

async def verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not check_anti_spam(user.id):
        return

    ok = await check_membership(context, user.id)
    if not ok:
        await query.message.reply_text(
            "❌ Verification fail ho gaya! Sabhi channels join karein aur dubara verify dabayein.",
            reply_markup=channels_keyboard(),
        )
        return

    await set_verified(user.id)
    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=user.id,
        text=f"✅ <b>Verification Successful!</b>\n\nSwagat hai KSL Tournament mein, <b>{user.first_name}</b>!",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(user.id),
    )

# ==================== AUTO SCREENSHOT VERIFICATION ====================
async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    photo = update.effective_message.photo[-1]
    msg = await update.effective_message.reply_text("🔍 <i>Screenshot scan kiya ja raha hai, kripya thoda wait karein...</i>", parse_mode=ParseMode.HTML)

    file_path = f"payment_{user.id}_{int(time.time())}.jpg"
    new_file = await context.bot.get_file(photo.file_id)
    await new_file.download_to_drive(file_path)

    try:
        loop = asyncio.get_running_loop()
        extracted_text = await loop.run_in_executor(None, lambda: pytesseract.image_to_string(Image.open(file_path)))
    except Exception as e:
        logger.error(f"OCR Error: {e}")
        await msg.edit_text("❌ Screenshot read karne me problem aayi. Kripya clear screenshot send karein ya Admin se contact karein.")
        if os.path.exists(file_path):
            os.remove(file_path)
        return

    if os.path.exists(file_path):
        os.remove(file_path)

    text_clean = extracted_text.replace(" ", "").upper()
    utr_match = re.search(r"(?:UTR|REF|UPI|TXN|NO|NUMBER)?[:#-]?([0-9]{12})", text_clean)
    amt_match = re.search(r"(?:INR|RS|₹|PAID|AMOUNT)?[:\s]?([0-9]{2,4})(?:\.00)?", extracted_text.upper())

    if not utr_match:
        await msg.edit_text(
            "❌ <b>UTR Number scan nahi ho saka!</b>\n\n"
            "Kripya aisa screenshot bhejein jisme <b>12-digit UTR/UPI Ref No.</b> saaf dikh raha ho, ya direct Admin ko forward karein.",
            parse_mode=ParseMode.HTML
        )
        return

    utr = utr_match.group(1)
    amount = int(amt_match.group(1)) if amt_match else 50

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT utr FROM verified_payments WHERE utr = ?", (utr,)) as cur:
            if await cur.fetchone():
                await msg.edit_text("⚠️ <b>Duplicate Payment!</b> Ye payment pehle se hi bot mein use ho chuki hai.", parse_mode=ParseMode.HTML)
                return

        await conn.execute("INSERT INTO verified_payments (utr, user_id, amount) VALUES (?, ?, ?)", (utr, user.id, amount))
        await conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user.id))
        await conn.commit()

        async with conn.execute("SELECT balance FROM users WHERE user_id = ?", (user.id,)) as cur:
            new_bal = (await cur.fetchone())[0]

    await msg.edit_text(
        f"✅ <b>Payment Verified Successfully!</b>\n\n"
        f"💰 Added Amount: <b>₹{amount}</b>\n"
        f"🆔 UTR Number: <code>{utr}</code>\n"
        f"💳 Total Wallet Balance: <b>₹{new_bal}</b>\n\n"
        "Aapka balance turant update kar diya gaya hai. Ab aap matches join kar sakte hain!",
        parse_mode=ParseMode.HTML
    )

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"💵 <b>Auto Payment Verified</b>\nUser: {user.first_name} (ID: <code>{user.id}</code>)\nAmount: ₹{amount}\nUTR:
