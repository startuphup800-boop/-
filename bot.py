import os
import re
import time
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, List, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# ================== НАСТРОЙКИ ==================
TOKEN = os.getenv("8512126293:AAHrWZrB3hPUy_K6mIDjJaprG_0VXLTtUcE")
if not TOKEN:
    raise SystemExit("8512126293:AAHrWZrB3hPUy_K6mIDjJaprG_0VXLTtUcE is not set (Railway -> Variables)")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
if ADMIN_ID == 8364848803:
    # можно оставить 0, но админка не будет доступна пока не укажешь ADMIN_ID
    logging.warning("ADMIN_ID is not set. Set ADMIN_ID in Railway Variables.")

DB_PATH = os.getenv("DB_PATH", "bot.db")
REF_BONUS = int(os.getenv("REF_BONUS", "17000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


# ================== КЛАВИАТУРЫ ==================
def main_kb(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🤝 Пригласить")],
        [KeyboardButton(text="🏆 Топ игроков")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Менять баланс", callback_data="admin:set_balance")],
        [InlineKeyboardButton(text="🔇 Мут", callback_data="admin:mute")],
        [InlineKeyboardButton(text="🔊 Размут", callback_data="admin:unmute")],
        [InlineKeyboardButton(text="⛔ Бан", callback_data="admin:ban")],
        [InlineKeyboardButton(text="✅ Разбан", callback_data="admin:unban")],
        [InlineKeyboardButton(text="🏆 Топ игроков", callback_data="admin:top")],
    ])


def top_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 По балансу", callback_data="top:balance")],
        [InlineKeyboardButton(text="🤝 По приглашениям", callback_data="top:refs")],
    ])


# ================== БД ==================
CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    nick TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    referrals_count INTEGER NOT NULL DEFAULT 0,
    referred_by INTEGER,
    is_banned INTEGER NOT NULL DEFAULT 0,
    mute_until INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""

CREATE_META_SQL = """
CREATE TABLE IF NOT EXISTS meta(
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(CREATE_META_SQL)
        await db.commit()


async def upsert_user(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        now = int(time.time())
        await db.execute(
            """
            INSERT INTO users(user_id, username, created_at)
            VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """,
            (user_id, username or "", now)
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id, username, nick, balance, referrals_count, referred_by, is_banned, mute_until FROM users WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        return row


async def set_nick(user_id: int, nick: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET nick=? WHERE user_id=?", (nick, user_id))
        await db.commit()


async def add_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        await db.commit()


async def set_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance=? WHERE user_id=?", (amount, user_id))
        await db.commit()


async def set_ban(user_id: int, banned: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
        await db.commit()


async def set_mute(user_id: int, until_ts: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET mute_until=? WHERE user_id=?", (until_ts, user_id))
        await db.commit()


async def add_referral(referrer_id: int, referred_id: int):
    """
    Засчитываем реферал только один раз (если referred_by is NULL).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT referred_by FROM users WHERE user_id=?", (referred_id,))
        row = await cur.fetchone()
        if not row:
            return False
        referred_by = row[0]
        if referred_by is not None:
            return False
        await db.execute("UPDATE users SET referred_by=? WHERE user_id=?", (referrer_id, referred_id))
        await db.execute("UPDATE users SET referrals_count = referrals_count + 1 WHERE user_id=?", (referrer_id,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (REF_BONUS, referrer_id))
        await db.commit()
        return True


async def top_by_balance(limit: int = 5) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id, COALESCE(nick,''), COALESCE(username,''), balance
            FROM users
            ORDER BY balance DESC
            LIMIT ?
            """,
            (limit,)
        )
        return await cur.fetchall()


async def top_by_refs(limit: int = 5) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id, COALESCE(nick,''), COALESCE(username,''), referrals_count
            FROM users
            ORDER BY referrals_count DESC
            LIMIT ?
            """,
            (limit,)
        )
        return await cur.fetchall()


# ================== УТИЛИТЫ ==================
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def display_name(nick: str, username: str, user_id: int) -> str:
    if nick and nick.strip():
        return nick.strip()
    if username and username.strip():
        return f"@{username.strip().lstrip('@')}"
    return str(user_id)


def parse_set_balance(text: str) -> Optional[Tuple[int, int]]:
    # формат: "айди сумма" или "айди:сумма"
    text = text.strip()
    m = re.match(r"^(\d+)\s+(-?\d+)$", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)\s*[:;,\-]\s*(-?\d+)$", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def parse_id_and_minutes(text: str) -> Optional[Tuple[int, int]]:
    # формат: "айди минуты"
    text = text.strip()
    m = re.match(r"^(\d+)\s+(\d+)$", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def parse_id_only(text: str) -> Optional[int]:
    text = text.strip()
    if text.isdigit():
        return int(text)
    return None


async def check_restrictions(message: Message) -> bool:
    """
    True = можно обрабатывать дальше
    False = запрещено (бан/мут)
    """
    uid = message.from_user.id
    row = await get_user(uid)
    if not row:
        return True

    _user_id, _username, _nick, _bal, _refs, _ref_by, banned, mute_until = row
    now = int(time.time())

    if banned == 1:
        # бан = вообще ничего нельзя
        try:
            await message.answer("⛔ Вы забанены. Доступ к боту запрещён.")
        except:
            pass
        return False

    if mute_until and mute_until > now:
        left = mute_until - now
        mins = max(1, left // 60)
        try:
            await message.answer(f"🔇 Вы в муте. Осталось примерно: {mins} мин.")
        except:
            pass
        return False

    return True


# ================== FSM-ЛАЙТ (админские режимы) ==================
@dataclass
class AdminState:
    mode: str = ""  # set_balance / mute / unmute / ban / unban
    created_at: int = 0

ADMIN_STATES: dict[int, AdminState] = {}


def set_admin_mode(admin_id: int, mode: str):
    ADMIN_STATES[admin_id] = AdminState(mode=mode, created_at=int(time.time()))


def pop_admin_mode(admin_id: int) -> str:
    st = ADMIN_STATES.pop(admin_id, None)
    return st.mode if st else ""


def peek_admin_mode(admin_id: int) -> str:
    st = ADMIN_STATES.get(admin_id)
    if not st:
        return ""
    # автосброс через 10 минут
    if int(time.time()) - st.created_at > 600:
        ADMIN_STATES.pop(admin_id, None)
        return ""
    return st.mode


# ================== BOT ==================
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


@dp.message(CommandStart())
async def start_cmd(message: Message):
    uid = message.from_user.id
    username = message.from_user.username or ""
    await upsert_user(uid, username)

    # рефералка: /start 123456
    parts = (message.text or "").split()
    if len(parts) >= 2:
        ref = parts[1].strip()
        if ref.isdigit():
            ref_id = int(ref)
            if ref_id != uid:
                await upsert_user(ref_id, None)
                ok = await add_referral(ref_id, uid)
                if ok:
                    await message.answer(f"✅ Вы перешли по приглашению!\n🎁 Пригласивший получил +{REF_BONUS} к балансу.")
                else:
                    # уже был реферал/не первый раз
                    pass

    row = await get_user(uid)
    nick = row[2] if row else ""
    if not nick:
        # поставим ник по умолчанию (username или id)
        await set_nick(uid, username if username else f"User{uid}")

    await message.answer(
        "👋 Привет! Бот запущен.\n\n"
        "• 👤 Профиль — твой баланс и рефералы\n"
        "• 🤝 Пригласить — твоя ссылка\n"
        "• 🏆 Топ игроков — рейтинг\n",
        reply_markup=main_kb(is_admin(uid))
    )


@dp.message(F.text)
async def any_text(message: Message):
    # запрет писать если мут/бан
    if not await check_restrictions(message):
        return

    uid = message.from_user.id
    username = message.from_user.username or ""
    await upsert_user(uid, username)

    txt = (message.text or "").strip()

    # --- Админ режимы ---
    if is_admin(uid):
        mode = peek_admin_mode(uid)
        if mode:
            if mode == "set_balance":
                parsed = parse_set_balance(txt)
                if not parsed:
                    await message.answer("❌ Формат: <b>айди сумма</b>\nПример: <code>123456789 50000</code>")
                    return
                target_id, amount = parsed
                await upsert_user(target_id, None)
                await set_balance(target_id, amount)
                pop_admin_mode(uid)
                await message.answer(f"✅ Баланс пользователю <code>{target_id}</code> установлен: <b>{amount}</b>", reply_markup=main_kb(True))
                return

            if mode == "mute":
                parsed = parse_id_and_minutes(txt)
                if not parsed:
                    await message.answer("❌ Формат: <b>айди минуты</b>\nПример: <code>123456789 60</code>")
                    return
                target_id, mins = parsed
                until = int(time.time()) + mins * 60
                await upsert_user(target_id, None)
                await set_mute(target_id, until)
                pop_admin_mode(uid)
                await message.answer(f"🔇 Пользователь <code>{target_id}</code> в муте на <b>{mins}</b> мин.", reply_markup=main_kb(True))
                return

            if mode == "unmute":
                target_id = parse_id_only(txt)
                if not target_id:
                    await message.answer("❌ Формат: <b>айди</b>\nПример: <code>123456789</code>")
                    return
                await upsert_user(target_id, None)
                await set_mute(target_id, 0)
                pop_admin_mode(uid)
                await message.answer(f"🔊 Мут снят с <code>{target_id}</code>", reply_markup=main_kb(True))
                return

            if mode == "ban":
                target_id = parse_id_only(txt)
                if not target_id:
                    await message.answer("❌ Формат: <b>айди</b>\nПример: <code>123456789</code>")
                    return
                await upsert_user(target_id, None)
                await set_ban(target_id, True)
                pop_admin_mode(uid)
                await message.answer(f"⛔ Пользователь <code>{target_id}</code> забанен.", reply_markup=main_kb(True))
                return

            if mode == "unban":
                target_id = parse_id_only(txt)
                if not target_id:
                    await message.answer("❌ Формат: <b>айди</b>\nПример: <code>123456789</code>")
                    return
                await upsert_user(target_id, None)
                await set_ban(target_id, False)
                pop_admin_mode(uid)
                await message.answer(f"✅ Пользователь <code>{target_id}</code> разбанен.", reply_markup=main_kb(True))
                return

    # --- обычные кнопки ---
    if txt == "👤 Профиль":
        row = await get_user(uid)
        if not row:
            await message.answer("❌ Профиль не найден. Напиши /start")
            return
        _id, usern, nick, bal, refs, ref_by, banned, mute_until = row
        name = display_name(nick, usern, uid)
        await message.answer(
            f"👤 <b>Профиль</b>\n"
            f"• Ник: <b>{name}</b>\n"
            f"• Баланс: <b>{bal}</b>\n"
            f"• Приглашения: <b>{refs}</b>\n",
            reply_markup=main_kb(is_admin(uid))
        )
        return

    if txt == "🤝 Пригласить":
        # ссылка на приглашение
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={uid}"
        await message.answer(
            "🤝 <b>Твоя реф-ссылка:</b>\n"
            f"{link}\n\n"
            f"🎁 За приглашение: +<b>{REF_BONUS}</b> к балансу",
            reply_markup=main_kb(is_admin(uid))
        )
        return

    if txt == "🏆 Топ игроков":
        await message.answer("🏆 Выбери рейтинг:", reply_markup=top_kb())
        return

    if txt == "🛠 Админка":
        if not is_admin(uid):
            await message.answer("❌ Нет доступа.")
            return
        await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
        return

    # дефолт
    await message.answer("Выбери кнопку в меню 👇", reply_markup=main_kb(is_admin(uid)))


@dp.callback_query(F.data.startswith("top:"))
async def top_cb(call: CallbackQuery):
    uid = call.from_user.id
    # запрет если мут/бан (на всякий)
    fake_msg = Message.model_validate({**call.message.model_dump(), "from_user": call.from_user})
    if not await check_restrictions(fake_msg):
        await call.answer()
        return

    data = call.data

    if data == "top:balance":
        rows = await top_by_balance(5)
        text = "🏆 <b>Топ игроков по балансу</b>\n\n"
        if not rows:
            text += "Пока пусто."
        else:
            for i, (user_id, nick, username, bal) in enumerate(rows, start=1):
                name = display_name(nick, username, user_id)
                text += f"{i}. <b>{name}</b> — <b>{bal}</b>\n"
        await call.message.answer(text, reply_markup=main_kb(is_admin(uid)))
        await call.answer()
        return

    if data == "top:refs":
        rows = await top_by_refs(5)
        text = "🏆 <b>Топ по приглашениям</b>\n\n"
        if not rows:
            text += "Пока пусто."
        else:
            for i, (user_id, nick, username, refs) in enumerate(rows, start=1):
                name = display_name(nick, username, user_id)
                text += f"{i}. <b>{name}</b> — <b>{refs}</b>\n"
        await call.message.answer(text, reply_markup=main_kb(is_admin(uid)))
        await call.answer()
        return

    await call.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def admin_cb(call: CallbackQuery):
    uid = call.from_user.id
    if not is_admin(uid):
        await call.answer("Нет доступа", show_alert=True)
        return

    data = call.data

    if data == "admin:set_balance":
        set_admin_mode(uid, "set_balance")
        await call.message.answer(
            "💰 <b>Смена баланса</b>\n"
            "Напиши: <b>айди сумма</b>\n"
            "Пример: <code>123456789 50000</code>"
        )
        await call.answer()
        return

    if data == "admin:mute":
        set_admin_mode(uid, "mute")
        await call.message.answer(
            "🔇 <b>Мут</b>\n"
            "Напиши: <b>айди минуты</b>\n"
            "Пример: <code>123456789 60</code>"
        )
        await call.answer()
        return

    if data == "admin:unmute":
        set_admin_mode(uid, "unmute")
        await call.message.answer(
            "🔊 <b>Размут</b>\n"
            "Напиши: <b>айди</b>\n"
            "Пример: <code>123456789</code>"
        )
        await call.answer()
        return

    if data == "admin:ban":
        set_admin_mode(uid, "ban")
        await call.message.answer(
            "⛔ <b>Бан</b>\n"
            "Напиши: <b>айди</b>\n"
            "Пример: <code>123456789</code>"
        )
        await call.answer()
        return

    if data == "admin:unban":
        set_admin_mode(uid, "unban")
        await call.message.answer(
            "✅ <b>Разбан</b>\n"
            "Напиши: <b>айди</b>\n"
            "Пример: <code>123456789</code>"
        )
        await call.answer()
        return

    if data == "admin:top":
        await call.message.answer("🏆 Выбери рейтинг:", reply_markup=top_kb())
        await call.answer()
        return

    await call.answer()


async def main():
    await init_db()
    logging.info("Start polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

