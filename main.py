import asyncio
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
from html import escape
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatJoinRequest, ChatMemberUpdated, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
_ADMIN_LIST = [int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x]
ADMINS = set(_ADMIN_LIST)
OWNER = int(os.getenv("OWNER_ID", _ADMIN_LIST[0]))  # havola yaratish huquqi faqat egasida
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
STORAGE = int(os.environ["STORAGE_CHANNEL"])          # kinolar ombori (yopiq kanal ID)
PROTECT = os.getenv("PROTECT", "1") == "1"            # kinoni uzatish/saqlashni taqiqlash
DATABASE_URL = os.getenv("DATABASE_URL")              # bo'lsa Postgres, bo'lmasa SQLite
DB_PATH = os.getenv("DB_PATH", "/data/kino.db")
BASE = os.path.dirname(os.path.abspath(__file__))

if not DATABASE_URL:
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except OSError:
        DB_PATH = os.path.join(BASE, "kino.db")


# ---------- Baza (SQLite yoki Postgres) ----------
def db(sql, args=(), fetch=False):
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        c = psycopg2.connect(DATABASE_URL)
        try:
            with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql.replace("?", "%s"), args)
                return [dict(r) for r in cur.fetchall()] if fetch else None
        finally:
            c.close()
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(sql, args)
        c.commit()
        return [dict(r) for r in cur.fetchall()] if fetch else None
    finally:
        c.close()


def init_db():
    db("CREATE TABLE IF NOT EXISTS users(id BIGINT PRIMARY KEY, ts BIGINT)")
    db("CREATE TABLE IF NOT EXISTS films(code TEXT PRIMARY KEY, name TEXT, msg_id BIGINT, views INTEGER DEFAULT 0, ts BIGINT)")
    db("CREATE TABLE IF NOT EXISTS channels(chat_id BIGINT PRIMARY KEY, title TEXT, mode TEXT, link TEXT)")
    db("CREATE TABLE IF NOT EXISTS reqs(chat_id BIGINT, uid BIGINT, PRIMARY KEY(chat_id, uid))")
    db("CREATE TABLE IF NOT EXISTS series(id BIGINT PRIMARY KEY, name TEXT, poster TEXT, ts BIGINT)")
    db("CREATE TABLE IF NOT EXISTS series_eps(sid BIGINT, ep INTEGER, msg_id BIGINT, PRIMARY KEY(sid, ep))")
    db("CREATE TABLE IF NOT EXISTS links(name TEXT PRIMARY KEY, by_id BIGINT, ts BIGINT)")
    db("CREATE TABLE IF NOT EXISTS link_users(name TEXT, uid BIGINT, is_new INTEGER, ts BIGINT, PRIMARY KEY(name, uid))")
    db("CREATE TABLE IF NOT EXISTS emoji(slot TEXT PRIMARY KEY, eid TEXT)")
    db("CREATE TABLE IF NOT EXISTS view_log(ts BIGINT, uid BIGINT, kind TEXT, ref TEXT)")
    db("CREATE TABLE IF NOT EXISTS series_views(sid BIGINT PRIMARY KEY, views INTEGER DEFAULT 0)")
    db("CREATE TABLE IF NOT EXISTS prices(kind TEXT, ref TEXT, price BIGINT, card TEXT, holder TEXT, PRIMARY KEY(kind, ref))")
    db("CREATE TABLE IF NOT EXISTS buys(uid BIGINT, kind TEXT, ref TEXT, ts BIGINT, PRIMARY KEY(uid, kind, ref))")
    db("CREATE TABLE IF NOT EXISTS pays(id BIGINT PRIMARY KEY, uid BIGINT, kind TEXT, ref TEXT, status TEXT, ts BIGINT, file_uid TEXT)")
    db("CREATE TABLE IF NOT EXISTS pending(uid BIGINT PRIMARY KEY, kind TEXT, ref TEXT, ts BIGINT)")
    for ddl in ("ALTER TABLE films ADD COLUMN cat TEXT DEFAULT 'film'", "ALTER TABLE series ADD COLUMN code TEXT"):
        try:
            db(ddl)
        except Exception:
            pass  # ustun allaqachon bor


def touch(uid):
    db("INSERT INTO users(id,ts) VALUES(?,?) ON CONFLICT DO NOTHING", (uid, int(time.time())))


def get_film(code):
    r = db("SELECT * FROM films WHERE code=?", (str(code).strip().lower(),), True)
    return r[0] if r else None


def search(q):
    q = q.strip().casefold()
    return [f for f in db("SELECT code, name, COALESCE(cat,'film') AS cat FROM films ORDER BY ts DESC", (), True)
            if q in f["name"].casefold()]


def get_series(sid):
    r = db("SELECT * FROM series WHERE id=?", (int(sid),), True)
    return r[0] if r else None


def get_series_by_code(code):
    r = db("SELECT * FROM series WHERE LOWER(code)=?", (str(code).strip().lower(),), True)
    return r[0] if r else None


def code_taken(code, skip_series=None, skip_film=None):
    """Kod kino/multfilm yoki serialda bandmi? (band bo'lsa nimada ekanini qaytaradi)"""
    code = str(code).strip().lower()
    f = get_film(code)
    if f and f["code"] != (skip_film or ""):
        return "kinoda"
    sr = get_series_by_code(code)
    if sr and sr["id"] != skip_series:
        return "serialda"
    return None


def search_series(q):
    q = q.strip().casefold()
    return [s for s in db("SELECT id,name FROM series ORDER BY id DESC", (), True)
            if q in s["name"].casefold()]


_posters = {}
_lock = asyncio.Lock()


# ---------- Telegram initData ----------
def verify_init(init):
    try:
        d = dict(parse_qsl(init, keep_blank_values=True))
        h = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), h):
            return None
        if time.time() - int(d.get("auth_date", "0")) > 86400:
            return None
        return json.loads(d["user"])["id"]
    except Exception:
        return None


# ---------- Premium emoji (ruxsat bo'lmasa oddiy emojiga qaytadi) ----------
SLOTS = {"hello": "👋", "search": "🔎", "admin": "⚙", "check": "✅", "film": "🎬", "series": "📺", "top": "🏆"}
SLOT_INFO = {"hello": "salomlashuv xabari", "search": "«Kinolarni qidirish» tugmasi", "admin": "«Admin panel» tugmasi",
             "check": "«Tekshirish» tugmasi", "film": "kino sarlavhasi", "series": "serial sarlavhasi", "top": "Top 10 sarlavhasi"}
_eids = {}
_plain = {}
_prem_broken = False
_icons_on = False
_CE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def load_emoji():
    global _eids, _prem_broken, _icons_on
    _eids = {r["slot"]: r["eid"] for r in db("SELECT slot,eid FROM emoji", (), True)}
    _icons_on = "_icons" in _eids
    _eids.pop("_icons", None)
    _prem_broken = False


def em(slot):
    i = None if _prem_broken else _eids.get(slot)
    return f'<tg-emoji emoji-id="{i}">{SLOTS[slot]}</tg-emoji>' if i else SLOTS[slot]


STK_SLOTS = {"hello": "Salomlashuv (/start)", "check": "Obuna tasdiqlanganda", "paid": "To'lov tasdiqlanganda"}


async def send_stk(chat_id, slot):
    fid = _eids.get("st_" + slot)
    if fid:
        try:
            await bot.send_sticker(chat_id, fid)
        except Exception:
            pass


def _strip_premium(method):
    for f in ("text", "caption"):
        v = getattr(method, f, None)
        if isinstance(v, str) and "<tg-emoji" in v:
            setattr(method, f, _CE.sub(r"\1", v))
    rows = getattr(getattr(method, "reply_markup", None), "inline_keyboard", None) or []
    for row in rows:
        for b in row:
            if getattr(b, "icon_custom_emoji_id", None):
                b.icon_custom_emoji_id = None
                b.text = _plain.get(b.text, b.text)


class SafeBot(Bot):
    async def __call__(self, method, *a, **kw):
        global _prem_broken
        try:
            return await super().__call__(method, *a, **kw)
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if _prem_broken or not any(k in msg for k in ("emoji", "icon", "entit", "button")):
                raise
            _prem_broken = True  # premium emoji ruxsat etilmagan: oddiy emojiga o'tamiz
            _strip_premium(method)
            return await super().__call__(method, *a, **kw)


bot = SafeBot(TOKEN)
dp = Dispatcher()
ADM = F.from_user.id.in_(ADMINS)


def btn(text, cd=None, url=None, web=None, slot=None):
    plain = f"{SLOTS[slot]} {text}" if slot else text
    kw = dict(callback_data=cd, url=url, web_app=WebAppInfo(url=web) if web else None)
    eid = None if (_prem_broken or not slot or not _icons_on) else _eids.get(slot)
    if eid:
        try:
            b = InlineKeyboardButton(text=text, icon_custom_emoji_id=eid, **kw)
            _plain[text] = plain
            return b
        except Exception:
            pass
    return InlineKeyboardButton(text=plain, **kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home_txt():
    return f"{em('hello')} Kino kodini yuboring yoki «Kinolarni qidirish» tugmasini bosing 👇"


ADM_KB = kb([[btn("➕ Kino qo'shish", "a:add"), btn("🗑 Kino o'chirish", "a:del")],
             [btn("🧸 Multfilm qo'shish", "a:addc"), btn("💰 To'lovlar", "a:pays")],
             [btn("📺 Serial qo'shish", "a:ser"), btn("🔗 Havolalar", "a:links")],
             [btn("🎞 Kinolar ro'yxati", "a:list"), btn("📢 Majburiy kanallar", "a:ch")],
             [btn("📊 Statistika", "a:stat"), btn("📣 Xabar yuborish", "a:bc")],
             [btn("🏆 Top 10", "a:top"), btn("💎 Premium emoji", "a:emoji")],
             [btn("🎭 Stikerlar", "a:stk")]])


def home_kb(uid):
    rows = [[btn("Kinolarni qidirish", web=WEBAPP_URL, slot="search")]]
    if uid in ADMINS:
        rows.append([btn("Admin panel", "a:menu", slot="admin")])
    return kb(rows)


# ---------- Obunani tekshirish ----------
AUTO_DROP = os.getenv("AUTO_DROP", "1") == "1"  # 0 qo'ysangiz, avtomatik uzilish o'chadi


async def disconnect_channel(chat_id):
    ch = db("SELECT title FROM channels WHERE chat_id=? AND mode='request'", (chat_id,), True)
    if not ch:
        return
    db("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    db("DELETE FROM reqs WHERE chat_id=?", (chat_id,))
    for a in ADMINS:
        try:
            await bot.send_message(a, f"🔌 «{ch[0]['title']}» majburiy obunadan uzildi: so'rov yuborgan foydalanuvchi tasdiqlandi.\n"
                                      "Qayta ulash: Admin panel → Majburiy kanallar.")
        except Exception:
            pass


async def is_sub(uid, ch):
    try:
        m = await bot.get_chat_member(ch["chat_id"], uid)
        if m.status in ("member", "administrator", "creator"):
            if (AUTO_DROP and ch["mode"] == "request"
                    and db("SELECT 1 AS x FROM reqs WHERE chat_id=? AND uid=?", (ch["chat_id"], uid), True)):
                await disconnect_channel(ch["chat_id"])  # bu odam so'rov yuborgan edi va tasdiqlangan
            return True
        if m.status == "restricted" and getattr(m, "is_member", False):
            return True
    except Exception:
        pass
    if ch["mode"] == "request":
        return bool(db("SELECT 1 AS x FROM reqs WHERE chat_id=? AND uid=?", (ch["chat_id"], uid), True))
    return False


async def missing(uid):
    if uid in ADMINS:
        return []
    chs = db("SELECT * FROM channels", (), True)
    res = await asyncio.gather(*[is_sub(uid, c) for c in chs])
    return [c for c, ok in zip(chs, res) if not ok]


def sub_kb(miss, code=None):
    rows = [[btn("📢 " + c["title"], url=c["link"])] for c in miss]
    rows.append([btn("Tekshirish", "chk:" + (code or ""), slot="check")])
    return kb(rows)


async def gate(uid, chat_id, code=None):
    miss = await missing(uid)
    if not miss:
        return True
    await bot.send_message(chat_id, "Davom etish uchun kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing 👇",
                           reply_markup=sub_kb(miss, code))
    return False


def log_view(uid, kind, ref):
    if uid not in ADMINS:  # admin sinovlari statistikani buzmasin
        db("INSERT INTO view_log(ts,uid,kind,ref) VALUES(?,?,?,?)", (int(time.time()), uid, kind, str(ref)))


def price_of(kind, ref):
    r = db("SELECT price, card, holder FROM prices WHERE kind=? AND ref=?", (kind, str(ref)), True)
    return r[0] if r and (r[0]["price"] or 0) > 0 else None


def has_buy(uid, kind, ref):
    return uid in ADMINS or bool(db("SELECT 1 AS x FROM buys WHERE uid=? AND kind=? AND ref=?", (uid, kind, str(ref)), True))


def money(n):
    return f"{int(n):,}".replace(",", " ")


def item_name(kind, ref):
    if kind == "film":
        f = get_film(ref)
        return f["name"] if f else str(ref)
    s = get_series(int(ref)) if str(ref).isdigit() else None
    return s["name"] if s else str(ref)


async def paywall(chat_id, kind, ref, name):
    """Pullik bo'lsa va sotib olinmagan bo'lsa: to'lov ma'lumotini yuboradi va False qaytaradi."""
    pr = price_of(kind, ref)
    if not pr or has_buy(chat_id, kind, ref):
        return True
    db("INSERT INTO pending(uid,kind,ref,ts) VALUES(?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
       "kind=excluded.kind, ref=excluded.ref, ts=excluded.ts", (chat_id, kind, str(ref), int(time.time())))
    holder = f"\n👤 {escape(pr['holder'])}" if pr["holder"] else ""
    await bot.send_message(
        chat_id,
        f"🔒 <b>{escape(name)}</b> — pullik.\n\nNarxi: <b>{money(pr['price'])} so'm</b>\n"
        f"💳 Karta: <code>{escape(pr['card'])}</code>{holder}\n\n"
        "To'lovni qilgach, <b>chek</b> (screenshot) rasmini shu yerga yuboring. Tasdiqlangach darhol yuboriladi.",
        parse_mode="HTML")
    return False


async def send_film(chat_id, f):
    if not await paywall(chat_id, "film", f["code"], f["name"]):
        return
    await bot.copy_message(chat_id, STORAGE, f["msg_id"], parse_mode="HTML", protect_content=PROTECT,
                           caption=f"{em('film')} <b>{escape(f['name'])}</b>\nKod: <code>{escape(f['code'])}</code>")
    db("UPDATE films SET views=views+1 WHERE code=?", (f["code"],))
    log_view(chat_id, "film", f["code"])


async def copy_store(chat_id, msg_id):
    for _ in range(5):
        try:
            return await bot.copy_message(STORAGE, chat_id, msg_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    raise RuntimeError("flood")


PAGE = 40


def eps_kb(sid, page=0):
    eps = [r["ep"] for r in db("SELECT ep FROM series_eps WHERE sid=? ORDER BY ep", (sid,), True)]
    if not eps:
        return None
    part = eps[page * PAGE:(page + 1) * PAGE]
    rows = [[btn(f"{e}-qism", f"e:{sid}:{e}") for e in part[i:i + 4]] for i in range(0, len(part), 4)]
    nav = []
    if page > 0:
        nav.append(btn("⬅️", f"sp:{sid}:{page - 1}"))
    if (page + 1) * PAGE < len(eps):
        nav.append(btn("➡️", f"sp:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    return kb(rows)


async def send_series(chat_id, sid):
    s = get_series(sid)
    if not s:
        await bot.send_message(chat_id, "Serial topilmadi.")
        return
    if not await paywall(chat_id, "ser", sid, s["name"]):
        return
    n = db("SELECT COUNT(*) AS n FROM series_eps WHERE sid=?", (sid,), True)[0]["n"]
    code_line = f"\nKod: <code>{escape(s['code'])}</code>" if s.get("code") else ""
    await bot.send_photo(chat_id, s["poster"], parse_mode="HTML", reply_markup=eps_kb(sid),
                         caption=f"{em('series')} <b>{escape(s['name'])}</b>{code_line}\nQismlar: {n}\nKerakli qismni tanlang 👇")


async def send_episode(chat_id, sid, ep):
    r = db("SELECT msg_id FROM series_eps WHERE sid=? AND ep=?", (sid, ep), True)
    s = get_series(sid)
    if not (r and s):
        return False
    if not await paywall(chat_id, "ser", sid, s["name"]):
        return True
    await bot.copy_message(chat_id, STORAGE, r[0]["msg_id"], parse_mode="HTML", protect_content=PROTECT,
                           caption=f"{em('series')} <b>{escape(s['name'])}</b> — {ep}-qism")
    db("INSERT INTO series_views(sid,views) VALUES(?,1) ON CONFLICT(sid) DO UPDATE SET views=series_views.views+1", (sid,))
    log_view(chat_id, "ser", sid)
    return True


async def deliver(chat_id, code):
    if code.startswith("film_"):
        f = get_film(code[5:])
        if f:
            await send_film(chat_id, f)
            return True
    elif code.startswith("ser_") and code[4:].isdigit():
        if get_series(int(code[4:])):
            await send_series(chat_id, int(code[4:]))
            return True
    elif code.startswith("ep_"):
        parts = code[3:].split("_")
        if len(parts) == 2 and all(x.isdigit() for x in parts):
            return await send_episode(chat_id, int(parts[0]), int(parts[1]))
    return False


# ---------- Foydalanuvchi qismi ----------
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    is_new = not db("SELECT 1 AS x FROM users WHERE id=?", (uid,), True)
    touch(uid)
    arg = command.args or ""
    code = arg if arg.startswith(("film_", "ser_", "ep_")) else None
    if arg and not code and db("SELECT 1 AS x FROM links WHERE name=?", (arg.lower(),), True):
        db("INSERT INTO link_users(name,uid,is_new,ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
           (arg.lower(), uid, 1 if is_new else 0, int(time.time())))
    if not await gate(uid, m.chat.id, code):
        return
    if code:
        if await deliver(m.chat.id, code):
            return
        await m.answer("Topilmadi.")
    await send_stk(m.chat.id, "hello")
    await m.answer(home_txt(), parse_mode="HTML", reply_markup=home_kb(uid))


@dp.callback_query(F.data.startswith("chk:"))
async def chk(cb: CallbackQuery):
    if await missing(cb.from_user.id):
        await cb.answer("Hali hammasiga obuna bo'lmadingiz", show_alert=True)
        return
    code = cb.data[4:]
    await cb.message.edit_text("Obuna tasdiqlandi ✅")
    await send_stk(cb.message.chat.id, "check")
    if not (code and await deliver(cb.message.chat.id, code)):
        await cb.message.answer(home_txt(), parse_mode="HTML", reply_markup=home_kb(cb.from_user.id))
    await cb.answer()


@dp.callback_query(F.data.startswith("f:"))
async def pick(cb: CallbackQuery):
    code = cb.data[2:]
    await cb.answer()
    if not await gate(cb.from_user.id, cb.message.chat.id, "film_" + code):
        return
    f = get_film(code)
    if f:
        await send_film(cb.message.chat.id, f)


@dp.callback_query(F.data.startswith("s:"))
async def ser_pick(cb: CallbackQuery):
    sid = cb.data[2:]
    await cb.answer()
    if not await gate(cb.from_user.id, cb.message.chat.id, "ser_" + sid):
        return
    if sid.isdigit():
        await send_series(cb.message.chat.id, int(sid))


@dp.callback_query(F.data.startswith("sp:"))
async def ser_page(cb: CallbackQuery):
    _, sid, page = cb.data.split(":")
    await cb.message.edit_reply_markup(reply_markup=eps_kb(int(sid), int(page)))
    await cb.answer()


@dp.callback_query(F.data.startswith("e:"))
async def ep_cb(cb: CallbackQuery):
    _, sid, ep = cb.data.split(":")
    await cb.answer()
    if not await gate(cb.from_user.id, cb.message.chat.id, "ser_" + sid):
        return
    await send_episode(cb.message.chat.id, int(sid), int(ep))


@dp.chat_join_request()
async def join_req(e: ChatJoinRequest):
    # So'rovni bot tasdiqlamaydi, faqat yozib qo'yadi. Tasdiqlashni admin o'zi qiladi.
    if db("SELECT 1 AS x FROM channels WHERE chat_id=?", (e.chat.id,), True):
        db("INSERT INTO reqs(chat_id,uid) VALUES(?,?) ON CONFLICT DO NOTHING", (e.chat.id, e.from_user.id))


# ---------- Admin panel (ikkala admin uchun bir xil) ----------
class AddFilm(StatesGroup):
    video = State()
    code = State()
    name = State()


class DelFilm(StatesGroup):
    code = State()


class AddChan(StatesGroup):
    chat = State()
    mode = State()


class Broadcast(StatesGroup):
    msg = State()


class AddSer(StatesGroup):
    name = State()
    code = State()
    poster = State()
    eps = State()


class SerPoster(StatesGroup):
    photo = State()


class NewLink(StatesGroup):
    name = State()


class EmojiPick(StatesGroup):
    wait = State()


class StickerPick(StatesGroup):
    wait = State()


@dp.message(Command("admin"), ADM)
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙ Admin panel", reply_markup=ADM_KB)


@dp.callback_query(F.data == "a:menu", ADM)
async def a_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("⚙ Admin panel", reply_markup=ADM_KB)
    await cb.answer()


@dp.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Bekor qilindi.", reply_markup=ADM_KB if m.from_user.id in ADMINS else home_kb(m.from_user.id))


# --- kino qo'shish ---
@dp.callback_query(F.data == "a:add", ADM)
async def a_add(cb: CallbackQuery, state: FSMContext):
    await state.update_data(cat="film")
    await state.set_state(AddFilm.video)
    await cb.message.answer("Kinoni (video yoki fayl) yuboring.\nBekor qilish: /cancel")
    await cb.answer()


@dp.callback_query(F.data == "a:addc", ADM)
async def a_addc(cb: CallbackQuery, state: FSMContext):
    await state.update_data(cat="cartoon")
    await state.set_state(AddFilm.video)
    await cb.message.answer("🧸 Multfilmni (video yoki fayl) yuboring.\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(AddFilm.video, F.video | F.document, ADM)
async def af_video(m: Message, state: FSMContext):
    try:
        cp = await copy_store(m.chat.id, m.message_id)
    except Exception:
        await m.answer("Ombor kanaliga yuborib bo'lmadi. Bot kanalda admin ekanini va STORAGE_CHANNEL to'g'riligini tekshiring.")
        return
    await state.update_data(msg_id=cp.message_id)
    await state.set_state(AddFilm.code)
    await m.answer("Kino kodini yozing (lotin harf, raqam, _ yoki -; masalan 105):")


@dp.message(AddFilm.video, ADM)
async def af_video_bad(m: Message):
    await m.answer("Video yoki fayl yuboring. Bekor qilish: /cancel")


@dp.message(AddFilm.code, F.text, ADM)
async def af_code(m: Message, state: FSMContext):
    code = m.text.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", code):
        await m.answer("Kod faqat lotin harf, raqam, _ yoki - bo'lsin (32 belgigacha). Qayta yozing:")
        return
    if get_series_by_code(code):
        await m.answer("Bu kod serialda band. Boshqa kod yozing:")
        return
    await state.update_data(code=code)
    await state.set_state(AddFilm.name)
    note = "\n⚠️ Bu kod band, eski kino almashtiriladi." if get_film(code) else ""
    await m.answer("Kino nomini yozing:" + note)


@dp.message(AddFilm.name, F.text, ADM)
async def af_name(m: Message, state: FSMContext):
    d = await state.get_data()
    name = m.text.strip()[:80]
    cat = "cartoon" if d.get("cat") == "cartoon" else "film"
    db("INSERT INTO films(code,name,msg_id,views,ts,cat) VALUES(?,?,?,0,?,?) "
       "ON CONFLICT(code) DO UPDATE SET name=excluded.name, msg_id=excluded.msg_id, cat=excluded.cat",
       (d["code"], name, d["msg_id"], int(time.time()), cat))
    await state.clear()
    sect = "Multfilmlar" if cat == "cartoon" else "Kinolar"
    await m.answer(f"✅ Qo'shildi\nKod: {d['code']}\nNom: {name}\nWeb ilovada «{sect}» bo'limida ko'rinadi.", reply_markup=ADM_KB)


# --- kino o'chirish ---
@dp.callback_query(F.data == "a:del", ADM)
async def a_del(cb: CallbackQuery, state: FSMContext):
    await state.set_state(DelFilm.code)
    await cb.message.answer("O'chiriladigan kino kodini yozing. Bekor qilish: /cancel")
    await cb.answer()


@dp.message(DelFilm.code, F.text, ADM)
async def df_code(m: Message, state: FSMContext):
    f = get_film(m.text)
    if not f:
        await m.answer("Bunday kod yo'q. Qayta yozing yoki /cancel")
        return
    db("DELETE FROM films WHERE code=?", (f["code"],))
    db("DELETE FROM prices WHERE kind='film' AND ref=?", (f["code"],))
    db("DELETE FROM buys WHERE kind='film' AND ref=?", (f["code"],))
    try:
        await bot.delete_message(STORAGE, f["msg_id"])
    except Exception:
        pass
    await state.clear()
    await m.answer(f"🗑 O'chirildi: {f['code']} — {f['name']}", reply_markup=ADM_KB)


# --- ro'yxat va statistika ---
@dp.callback_query(F.data == "a:list", ADM)
async def a_list(cb: CallbackQuery):
    rows = db("SELECT code, name, views, COALESCE(cat,'film') AS cat FROM films ORDER BY ts DESC", (), True)
    text = "\n".join(f"{'🧸 ' if r['cat'] == 'cartoon' else ''}{r['code']} — {r['name']} (👁 {r['views']})" for r in rows) or "Hozircha kino yo'q."
    await cb.message.answer(text[:4000], reply_markup=ADM_KB)
    await cb.answer()


@dp.callback_query(F.data == "a:stat", ADM)
async def a_stat(cb: CallbackQuery):
    u = db("SELECT COUNT(*) AS n FROM users", (), True)[0]["n"]
    f = db("SELECT COUNT(*) AS n, COALESCE(SUM(views),0) AS v FROM films", (), True)[0]
    c = db("SELECT COUNT(*) AS n FROM channels", (), True)[0]["n"]
    sr = db("SELECT COUNT(*) AS n FROM series", (), True)[0]["n"]
    ep = db("SELECT COUNT(*) AS n FROM series_eps", (), True)[0]["n"]
    await cb.message.answer(f"👥 Foydalanuvchilar: {u}\n🎬 Kinolar: {f['n']}\n📺 Seriallar: {sr} ({ep} qism)\n"
                            f"👁 Kinolar ko'rilishi: {f['v']}\n📢 Majburiy kanallar: {c}", reply_markup=ADM_KB)
    await cb.answer()


# --- majburiy kanallar ---
@dp.callback_query(F.data == "a:ch", ADM)
async def a_ch(cb: CallbackQuery):
    rows = []
    for c in db("SELECT chat_id,title,mode FROM channels", (), True):
        kind = "so'rov" if c["mode"] == "request" else "oddiy"
        rows.append([btn(f"🗑 {c['title']} ({kind})", f"dc:{c['chat_id']}")])
    rows.append([btn("➕ Kanal qo'shish", "a:addch")])
    await cb.message.answer("📢 Majburiy obuna kanallari (o'chirish uchun bosing):", reply_markup=kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("dc:"), ADM)
async def dc(cb: CallbackQuery):
    db("DELETE FROM channels WHERE chat_id=?", (int(cb.data[3:]),))
    await cb.answer("O'chirildi")
    await cb.message.delete()


@dp.callback_query(F.data == "a:addch", ADM)
async def a_addch(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddChan.chat)
    await cb.message.answer("Kanal @username yoki ID (-100...) yuboring. Bot kanalda admin bo'lishi kerak.\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(AddChan.chat, F.text, ADM)
async def ac_chat(m: Message, state: FSMContext):
    await state.update_data(raw=m.text.strip())
    await state.set_state(AddChan.mode)
    await m.answer("Obuna turini tanlang:", reply_markup=kb([[btn("Oddiy obuna", "cm:open")],
                                                            [btn("So'rov bilan qo'shilish", "cm:request")]]))


@dp.callback_query(AddChan.mode, F.data.startswith("cm:"), ADM)
async def ac_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data[3:]
    raw = (await state.get_data())["raw"]
    chat = int(raw) if raw.lstrip("-").isdigit() else (raw if raw.startswith("@") else "@" + raw)
    try:
        c = await bot.get_chat(chat)
        me = await bot.get_chat_member(c.id, (await bot.me()).id)
        if me.status not in ("administrator", "creator"):
            raise ValueError("not admin")
        if mode == "request":
            link = (await bot.create_chat_invite_link(c.id, name="kino", creates_join_request=True)).invite_link
        elif c.username:
            link = "https://t.me/" + c.username
        else:
            link = (await bot.create_chat_invite_link(c.id, name="kino")).invite_link
    except Exception:
        await cb.message.answer("Kanal topilmadi yoki bot kanalda admin emas (havola yaratish ruxsati ham kerak).", reply_markup=ADM_KB)
        await state.clear()
        await cb.answer()
        return
    db("INSERT INTO channels(chat_id,title,mode,link) VALUES(?,?,?,?) "
       "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, mode=excluded.mode, link=excluded.link",
       (c.id, c.title, mode, link))
    await state.clear()
    await cb.message.answer(f"✅ Qo'shildi: {c.title}", reply_markup=ADM_KB)
    await cb.answer()


# --- xabar yuborish ---
_tasks = set()


async def do_broadcast(admin_chat, src_chat, msg_id):
    ok = bad = 0
    for r in db("SELECT id FROM users", (), True):
        try:
            await bot.copy_message(r["id"], src_chat, msg_id)
            ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await bot.send_message(admin_chat, f"📣 Yuborildi: {ok}, yetmadi: {bad}", reply_markup=ADM_KB)


@dp.callback_query(F.data == "a:bc", ADM)
async def a_bc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.msg)
    await cb.message.answer("Hammaga yuboriladigan xabarni yuboring (matn, rasm yoki video).\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(Broadcast.msg, ADM)
async def bc_msg(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Yuborish boshlandi…")
    t = asyncio.create_task(do_broadcast(m.chat.id, m.chat.id, m.message_id))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# --- seriallar ---
@dp.callback_query(F.data == "a:ser", ADM)
async def a_ser(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[btn(f"📺 {s['name']}" + (f" [{s['code']}]" if s["code"] else "") + " (+qism)", f"sa:{s['id']}"),
             btn("🖼", f"sph:{s['id']}"), btn("🗑", f"sd:{s['id']}")]
            for s in db("SELECT id,name,code FROM series ORDER BY id", (), True)]
    rows.append([btn("➕ Yangi serial", "a:newser")])
    await cb.message.answer("📺 Seriallar. Qism qo'shish uchun nomini bosing (🖼 rasmni almashtirish, 🗑 o'chirish):", reply_markup=kb(rows))
    await cb.answer()


@dp.callback_query(F.data == "a:newser", ADM)
async def a_newser(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddSer.name)
    await cb.message.answer("Serial nomini yozing. Bekor qilish: /cancel")
    await cb.answer()


@dp.message(AddSer.name, F.text, ADM)
async def ser_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()[:80])
    await state.set_state(AddSer.code)
    await m.answer("Serial kodini yozing (lotin harf, raqam, _ yoki -; masalan merlin). Foydalanuvchi shu kodni yozib serialni oladi:")


@dp.message(AddSer.code, F.text, ADM)
async def ser_code(m: Message, state: FSMContext):
    code = m.text.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", code):
        await m.answer("Kod faqat lotin harf, raqam, _ yoki - bo'lsin (32 gacha). Qayta yozing:")
        return
    taken = code_taken(code)
    if taken:
        await m.answer(f"Bu kod {taken} band. Boshqa kod yozing:")
        return
    await state.update_data(code=code)
    await state.set_state(AddSer.poster)
    await m.answer("Endi serial rasmini (poster) yuboring:")


@dp.message(AddSer.code, ADM)
async def ser_code_bad(m: Message):
    await m.answer("Kodni matn bilan yozing. Bekor qilish: /cancel")


@dp.message(AddSer.poster, F.photo, ADM)
async def ser_poster(m: Message, state: FSMContext):
    d = await state.get_data()
    async with _lock:
        sid = db("SELECT COALESCE(MAX(id),0) AS n FROM series", (), True)[0]["n"] + 1
        db("INSERT INTO series(id,name,poster,ts,code) VALUES(?,?,?,?,?)",
           (sid, d["name"], m.photo[-1].file_id, int(time.time()), d.get("code")))
    await state.update_data(sid=sid)
    await state.set_state(AddSer.eps)
    await m.answer(f"«{d['name']}» yaratildi ✅ (kod: {d.get('code')})\nEndi qismlarni (video yoki fayl) yuboring. Hammasini birdaniga "
                   "yuborsangiz ham bo'ladi (10 ta va undan ko'p).\nTugatgach /tugatish yozing.")


@dp.message(AddSer.poster, ADM)
async def ser_poster_bad(m: Message):
    await m.answer("Rasm (photo) yuboring. Bekor qilish: /cancel")


@dp.callback_query(F.data.startswith("sa:"), ADM)
async def sa(cb: CallbackQuery, state: FSMContext):
    s = get_series(int(cb.data[3:]))
    if not s:
        await cb.answer("Serial topilmadi")
        return
    await state.update_data(sid=s["id"], name=s["name"])
    await state.set_state(AddSer.eps)
    await cb.message.answer(f"«{s['name']}» ga yangi qismlarni yuboring (birdaniga ko'p yuborsangiz ham bo'ladi).\n"
                            "Tugatgach /tugatish yozing.")
    await cb.answer()


_buf = {}


def enqueue(chat_id, sid, msg_id):
    b = _buf.setdefault(chat_id, {"ids": [], "task": None, "ok": 0, "bad": 0})
    b["sid"] = sid
    b["ids"].append(msg_id)
    if b["task"] is None:
        b["task"] = asyncio.create_task(_worker(chat_id))


async def _worker(chat_id):
    b = _buf[chat_id]
    await asyncio.sleep(3)  # hammasi yetib kelishini kutamiz
    await bot.send_message(chat_id, "⏳ Qismlar qabul qilinmoqda, kuting…")
    while b["ids"]:
        ids = sorted(b["ids"])  # tartib: admin yuborgan xabar raqami bo'yicha
        b["ids"].clear()
        for mid in ids:
            try:
                cp = await copy_store(chat_id, mid)
            except Exception:
                b["bad"] += 1
                continue
            async with _lock:
                ep = db("SELECT COALESCE(MAX(ep),0) AS n FROM series_eps WHERE sid=?", (b["sid"],), True)[0]["n"] + 1
                db("INSERT INTO series_eps(sid,ep,msg_id) VALUES(?,?,?)", (b["sid"], ep, cp.message_id))
            b["ok"] += 1
        await asyncio.sleep(2)  # yana yangi kelganlar bormi
    _buf.pop(chat_id, None)
    total = db("SELECT COUNT(*) AS n FROM series_eps WHERE sid=?", (b["sid"],), True)[0]["n"]
    text = f"✅ {b['ok']} ta qism qabul qilindi (serialda jami {total} qism)."
    if b["bad"]:
        text += f"\n⚠️ {b['bad']} ta qism yuborilmadi, ularni qayta yuboring."
    await bot.send_message(chat_id, text + "\nYana qism yuboring yoki /tugatish yozing.")


@dp.message(Command("tugatish"), AddSer.eps, ADM)
async def ser_done(m: Message, state: FSMContext):
    b = _buf.get(m.chat.id)
    if b and b["task"]:
        try:
            await b["task"]  # hamma qism ombor kanaliga o'tib bo'lguncha kutamiz
        except Exception:
            pass
    sid = (await state.get_data())["sid"]
    s = get_series(sid)
    n = db("SELECT COUNT(*) AS n FROM series_eps WHERE sid=?", (sid,), True)[0]["n"]
    await state.clear()
    await m.answer(f"✅ Tayyor: {s['name']} — {n} qism.\nWeb ilovada «Seriallar» bo'limida ko'rinadi.", reply_markup=ADM_KB)


@dp.message(AddSer.eps, F.video | F.document, ADM)
async def ser_ep(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    enqueue(m.chat.id, sid, m.message_id)


@dp.message(AddSer.eps, ADM)
async def ser_ep_bad(m: Message):
    await m.answer("Video yoki fayl yuboring. Tugatish uchun /tugatish yozing.")


@dp.callback_query(F.data.startswith("sd:"), ADM)
async def sd(cb: CallbackQuery):
    s = get_series(int(cb.data[3:]))
    if not s:
        await cb.answer("Serial topilmadi")
        return
    await cb.message.answer(f"«{s['name']}» seriali va barcha qismlari o'chiriladi. Ishonchingiz komilmi?",
                            reply_markup=kb([[btn("✅ Ha, o'chirish", f"sdy:{s['id']}"), btn("❌ Yo'q", "a:ser")]]))
    await cb.answer()


@dp.callback_query(F.data.startswith("sdy:"), ADM)
async def sdy(cb: CallbackQuery):
    sid = int(cb.data[4:])
    ids = [r["msg_id"] for r in db("SELECT msg_id FROM series_eps WHERE sid=?", (sid,), True)]
    for i in range(0, len(ids), 100):
        try:
            await bot.delete_messages(STORAGE, ids[i:i + 100])
        except Exception:
            pass
    db("DELETE FROM series_eps WHERE sid=?", (sid,))
    db("DELETE FROM series WHERE id=?", (sid,))
    db("DELETE FROM series_views WHERE sid=?", (sid,))
    db("DELETE FROM prices WHERE kind='ser' AND ref=?", (str(sid),))
    db("DELETE FROM buys WHERE kind='ser' AND ref=?", (str(sid),))
    _posters.pop(sid, None)
    await cb.message.answer("🗑 Serial o'chirildi.", reply_markup=ADM_KB)
    await cb.answer()


# --- serial rasmini almashtirish ---
@dp.callback_query(F.data.startswith("sph:"), ADM)
async def sph(cb: CallbackQuery, state: FSMContext):
    s = get_series(int(cb.data[4:]))
    if not s:
        await cb.answer("Serial topilmadi")
        return
    await state.update_data(sid=s["id"])
    await state.set_state(SerPoster.photo)
    await cb.message.answer(f"«{s['name']}» uchun yangi rasmni yuboring. Bekor qilish: /cancel")
    await cb.answer()


@dp.message(SerPoster.photo, F.photo, ADM)
async def sph_photo(m: Message, state: FSMContext):
    sid = (await state.get_data())["sid"]
    db("UPDATE series SET poster=? WHERE id=?", (m.photo[-1].file_id, sid))
    _posters.pop(sid, None)
    await state.clear()
    await m.answer("🖼 Rasm almashtirildi ✅", reply_markup=ADM_KB)


@dp.message(SerPoster.photo, ADM)
async def sph_bad(m: Message):
    await m.answer("Rasm (photo) yuboring. Bekor qilish: /cancel")


# --- shaxsiy havolalar (yaratish faqat egasiga, ko'rish ikkala adminga) ---
@dp.callback_query(F.data == "a:links", ADM)
async def a_links(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uname = (await bot.me()).username
    rows = db("SELECT l.name, (SELECT COUNT(*) FROM link_users u WHERE u.name=l.name) AS n, "
              "(SELECT COUNT(*) FROM link_users u WHERE u.name=l.name AND u.is_new=1) AS nw "
              "FROM links l ORDER BY l.ts DESC", (), True)
    text = "🔗 Havolalar\n\n" + ("\n".join(
        f"🔗 {r['name']}\nhttps://t.me/{uname}?start={r['name']}\n👥 {r['n']} odam (yangi: {r['nw']})\n" for r in rows)
        or "Hozircha havola yo'q.")
    markup = None
    if cb.from_user.id == OWNER:
        markup = kb([[btn("➕ Havola yaratish", "a:newlink")]] + [[btn(f"🗑 {r['name']}", f"ld:{r['name']}")] for r in rows[:20]])
    else:
        text += "\nHavolani faqat bot egasi yaratadi."
    await cb.message.answer(text[:4000], reply_markup=markup, disable_web_page_preview=True)
    await cb.answer()


@dp.callback_query(F.data == "a:newlink", F.from_user.id == OWNER)
async def a_newlink(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewLink.name)
    await cb.message.answer("Havola uchun nom yozing (lotin harf, raqam, _ yoki -; masalan insta1).\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(NewLink.name, F.text, F.from_user.id == OWNER)
async def newlink_name(m: Message, state: FSMContext):
    name = m.text.strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name) or name.startswith(("film_", "ser_", "ep_")):
        await m.answer("Nom faqat lotin harf, raqam, _ yoki - bo'lsin (32 gacha), film_, ser_, ep_ bilan boshlanmasin. Qayta yozing:")
        return
    if db("SELECT 1 AS x FROM links WHERE name=?", (name,), True):
        await m.answer("Bu nom band. Boshqa nom yozing:")
        return
    db("INSERT INTO links(name,by_id,ts) VALUES(?,?,?)", (name, m.from_user.id, int(time.time())))
    await state.clear()
    uname = (await bot.me()).username
    await m.answer(f"✅ Havola tayyor:\nhttps://t.me/{uname}?start={name}\n\nShu havola orqali kirganlar sanaladi. "
                   "Statistikani ikkala admin «🔗 Havolalar» bo'limida ko'radi.", reply_markup=ADM_KB)


@dp.callback_query(F.data.startswith("ld:"), F.from_user.id == OWNER)
async def link_del(cb: CallbackQuery):
    name = cb.data[3:]
    db("DELETE FROM link_users WHERE name=?", (name,))
    db("DELETE FROM links WHERE name=?", (name,))
    await cb.answer("O'chirildi")
    await cb.message.answer(f"🗑 Havola o'chirildi: {name}", reply_markup=ADM_KB)


@dp.chat_member()
async def on_member(e: ChatMemberUpdated):
    # So'rov yuborgan odam tasdiqlanib, kanalga qo'shilsa: kanal majburiy obunadan uzib qo'yiladi
    if not AUTO_DROP or e.new_chat_member.status != "member" or not getattr(e, "via_join_request", False):
        return
    if db("SELECT 1 AS x FROM reqs WHERE chat_id=? AND uid=?", (e.chat.id, e.new_chat_member.user.id), True):
        await disconnect_channel(e.chat.id)


# --- Top 10 statistikasi ---
@dp.callback_query(F.data == "a:top", ADM)
async def a_top(cb: CallbackQuery):
    fl = db("SELECT code,name,views FROM films ORDER BY views DESC, ts DESC", (), True)[:10]
    sl = db("SELECT s.id AS id, s.name AS name, COALESCE(v.views,0) AS views FROM series s "
            "LEFT JOIN series_views v ON v.sid=s.id ORDER BY COALESCE(v.views,0) DESC, s.id DESC", (), True)[:10]
    medal = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]

    def lines(rows):
        return "\n".join(f"{medal[i]} {escape(r['name'])} — 👁 {r['views'] or 0}" for i, r in enumerate(rows)) or "Hozircha yo'q."
    text = (f"{em('top')} <b>Eng ko'p ko'rilgan kinolar (Top 10)</b>\n\n{lines(fl)}\n\n"
            f"📺 <b>Seriallar (qismlar ko'rilishi)</b>\n\n{lines(sl)}")
    await cb.message.answer(text[:4000], parse_mode="HTML", reply_markup=ADM_KB)
    await cb.answer()


# --- Premium emoji (Premium bo'lmasa ham ID olish yo'llari) ---
@dp.callback_query(F.data == "a:emoji", ADM)
async def a_emoji(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[btn(f"{SLOTS[k]} {SLOT_INFO[k]}" + (" ✅" if k in _eids else ""), f"em:{k}")] for k in SLOTS]
    rows += [[btn("🎭 Stikerlar", "a:stk")], [btn("⬅️ Admin panel", "a:menu")]]
    await cb.message.answer(
        "💎 <b>Premium emoji</b>\n\nPremium ham, ID ko'rish ham shart emas:\n"
        "1) Quyidan joyni tanlang.\n"
        "2) Premium emoji ishlatilgan istalgan xabarni (masalan boshqa kanal yoki do'stingizdan) botga <b>forward</b> qiling. "
        "Bot emojini o'zi oladi va saqlaydi.\n\n"
        "Qo'shimcha: /emoji PAKET_HAVOLASI, /testemoji joy, /emojis, /delemoji joy.\n\n"
        "⚠️ Telegram ba'zan bot egasidan Premium yoki Fragment username talab qiladi. Ruxsat bo'lmasa bot oddiy emoji ko'rsatadi; "
        "u holda «🎭 Stikerlar» bo'limidan foydalaning, u har doim ishlaydi.",
        parse_mode="HTML", reply_markup=kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("em:"), ADM)
async def em_pick(cb: CallbackQuery, state: FSMContext):
    slot = cb.data[3:]
    if slot not in SLOTS:
        await cb.answer()
        return
    await state.update_data(slot=slot)
    await state.set_state(EmojiPick.wait)
    await cb.message.answer(f"«{SLOT_INFO[slot]}» uchun premium emoji ishlatilgan xabarni botga forward qiling "
                            "(yoki emoji ID'sini yozing).\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(EmojiPick.wait, ADM)
async def em_set(m: Message, state: FSMContext):
    ids = [e.custom_emoji_id for e in (m.entities or m.caption_entities or []) if e.type == "custom_emoji"]
    if not ids and m.text and m.text.strip().isdigit():
        ids = [m.text.strip()]
    if not ids:
        await m.answer("Premium emoji topilmadi. Premium emoji ishlatilgan xabarni forward qiling (yoki ID yozing). Bekor qilish: /cancel")
        return
    slot = (await state.get_data())["slot"]
    try:
        sts = await bot.get_custom_emoji_stickers([ids[0]])
        if not sts:
            raise ValueError("yo'q")
    except Exception:
        await m.answer("Bu emoji ID Telegram'da topilmadi. Boshqa xabarni forward qiling.")
        return
    db("INSERT INTO emoji(slot,eid) VALUES(?,?) ON CONFLICT(slot) DO UPDATE SET eid=excluded.eid", (slot, ids[0]))
    load_emoji()
    await state.clear()
    await m.answer(f"✅ «{SLOT_INFO[slot]}» uchun {sts[0].emoji or ''} saqlandi.\nTekshirish: /testemoji {slot}", reply_markup=ADM_KB)


@dp.callback_query(F.data == "a:stk", ADM)
async def a_stk(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for k, t in STK_SLOTS.items():
        has_s = bool(_eids.get("st_" + k))
        rows.append([btn(t + (" ✅" if has_s else ""), f"stk:{k}")] + ([btn("🗑", f"stkd:{k}")] if has_s else []))
    rows.append([btn("⬅️ Admin panel", "a:menu")])
    await cb.message.answer(
        "🎭 <b>Stikerlar</b>\n\nJoyni tanlang va botga stikerni yuboring. Istalgan stiker bo'ladi: animatsiyali, premium ham "
        "(premium stikerni boshqa chatdan forward qilsangiz ham bo'ladi). Bot shu stikerni tegishli paytda o'zi yuboradi. "
        "Premium obuna ham, maxsus ruxsat ham kerak emas.", parse_mode="HTML", reply_markup=kb(rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("stk:"), ADM)
async def stk_pick(cb: CallbackQuery, state: FSMContext):
    slot = cb.data[4:]
    if slot not in STK_SLOTS:
        await cb.answer()
        return
    await state.update_data(slot=slot)
    await state.set_state(StickerPick.wait)
    await cb.message.answer(f"«{STK_SLOTS[slot]}» uchun stikerni yuboring (yoki forward qiling).\nBekor qilish: /cancel")
    await cb.answer()


@dp.callback_query(F.data.startswith("stkd:"), ADM)
async def stk_del(cb: CallbackQuery):
    slot = cb.data[5:]
    db("DELETE FROM emoji WHERE slot=?", ("st_" + slot,))
    load_emoji()
    await cb.answer("O'chirildi")
    await cb.message.answer(f"🗑 «{STK_SLOTS.get(slot, slot)}» stikeri o'chirildi.", reply_markup=ADM_KB)


@dp.message(StickerPick.wait, F.sticker, ADM)
async def stk_set(m: Message, state: FSMContext):
    slot = (await state.get_data())["slot"]
    db("INSERT INTO emoji(slot,eid) VALUES(?,?) ON CONFLICT(slot) DO UPDATE SET eid=excluded.eid", ("st_" + slot, m.sticker.file_id))
    load_emoji()
    await state.clear()
    await m.answer("✅ Stiker saqlandi. Foydalanuvchiga shunday yuboriladi:")
    await send_stk(m.chat.id, slot)
    await m.answer("Tayyor.", reply_markup=ADM_KB)


@dp.message(StickerPick.wait, ADM)
async def stk_bad(m: Message):
    await m.answer("Stiker yuboring (yoki forward qiling). Bekor qilish: /cancel")


@dp.message(Command("emoji"), ADM)
async def cmd_emoji(m: Message, command: CommandObject):
    name = (command.args or "").strip().rstrip("/").split("/")[-1].split("?")[0]
    if not name:
        await m.answer("Paket havolasini yozing: /emoji https://t.me/addemoji/NOMI")
        return
    try:
        st = await bot.get_sticker_set(name)
    except Exception:
        await m.answer("Paket topilmadi. Havola t.me/addemoji/... ko'rinishida bo'lsin.")
        return
    items = [x for x in st.stickers if getattr(x, "custom_emoji_id", None)]
    if not items:
        await m.answer("Bu emoji paketi emas (oddiy stiker paketi).")
        return
    lines = [f"{i}) {x.emoji or ''} <code>{x.custom_emoji_id}</code>" for i, x in enumerate(items, 1)]
    chunk = ""
    for ln in lines:
        if len(chunk) + len(ln) > 3500:
            await m.answer(chunk, parse_mode="HTML")
            chunk = ""
        chunk += ln + "\n"
    await m.answer(chunk + "\nKerakli ID'ni bosib nusxalang va: /setemoji slot ID", parse_mode="HTML")


@dp.message(Command("setemoji"), ADM)
async def cmd_setemoji(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[0] not in SLOTS or not parts[1].isdigit():
        await m.answer("Format: /setemoji slot ID\nSlotlar: " + ", ".join(SLOTS))
        return
    try:
        sts = await bot.get_custom_emoji_stickers([parts[1]])
        if not sts:
            raise ValueError("yo'q")
    except Exception:
        await m.answer("Bunday emoji ID topilmadi.")
        return
    db("INSERT INTO emoji(slot,eid) VALUES(?,?) ON CONFLICT(slot) DO UPDATE SET eid=excluded.eid", (parts[0], parts[1]))
    load_emoji()
    await m.answer(f"✅ {parts[0]} uchun {sts[0].emoji or ''} saqlandi. Tekshirish uchun /start yuboring.")


@dp.message(Command("delemoji"), ADM)
async def cmd_delemoji(m: Message, command: CommandObject):
    slot = (command.args or "").strip()
    if slot not in SLOTS:
        await m.answer("Slotlar: " + ", ".join(SLOTS))
        return
    db("DELETE FROM emoji WHERE slot=?", (slot,))
    load_emoji()
    await m.answer(f"🗑 {slot} o'chirildi.")


@dp.message(Command("emojis"), ADM)
async def cmd_emojis(m: Message):
    await m.answer("\n".join(f"{k}: {_eids.get(k, '—')}" for k in SLOTS) +
                   f"\n\nTugma ikonkalari: {'yoqilgan' if _icons_on else 'oʻchiq'} (/btnicons on yoki off)")


@dp.message(Command("btnicons"), ADM)
async def cmd_btnicons(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if arg not in ("on", "off"):
        await m.answer(f"Tugma ikonkalari hozir: {'yoqilgan' if _icons_on else 'oʻchiq'}.\n/btnicons on yoki /btnicons off")
        return
    if arg == "on":
        db("INSERT INTO emoji(slot,eid) VALUES('_icons','1') ON CONFLICT(slot) DO UPDATE SET eid='1'")
    else:
        db("DELETE FROM emoji WHERE slot='_icons'")
    load_emoji()
    await m.answer("Tugma ikonkalari " + ("yoqildi. Ko'rinmasa, /btnicons off qiling." if arg == "on" else "o'chirildi (oddiy emoji ishlaydi).") +
                   "\nTekshirish: /start")


@dp.message(Command("testemoji"), ADM)
async def cmd_testemoji(m: Message, command: CommandObject):
    slot = (command.args or "search").strip()
    eid = _eids.get(slot)
    if slot not in SLOTS or not eid:
        await m.answer("Avval /setemoji bilan o'rnating. Slotlar: " + ", ".join(SLOTS))
        return
    await m.answer(f'1) Matn ichida: <tg-emoji emoji-id="{eid}">{SLOTS[slot]}</tg-emoji>  — premium emoji ko\'rinyaptimi?',
                   parse_mode="HTML")
    try:
        kbd = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Tugma sinovi", callback_data="noop",
                                                                           icon_custom_emoji_id=eid)]])
        await m.answer("2) Tugmada ikonka ko'rinyaptimi?", reply_markup=kbd)
    except Exception as e:
        await m.answer(f"Tugma sinovi bajarilmadi: {e}")
    load_emoji()


@dp.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


def _has_ce(m: Message):
    return any(e.type == "custom_emoji" for e in (m.entities or m.caption_entities or []))


@dp.message(_has_ce, ADM, StateFilter(None))
async def got_ce(m: Message):
    ids = []
    for e in (m.entities or m.caption_entities or []):
        if e.type == "custom_emoji" and e.custom_emoji_id not in ids:
            ids.append(e.custom_emoji_id)
    chars = {}
    try:
        for x in await bot.get_custom_emoji_stickers(ids[:200]):
            chars[x.custom_emoji_id] = x.emoji or ""
    except Exception:
        pass
    await m.answer("Topilgan premium emoji ID'lar:\n" + "\n".join(
        f"{i}) {chars.get(v, '')} <code>{v}</code>" for i, v in enumerate(ids, 1)) + "\n\nO'rnatish: /setemoji slot ID",
        parse_mode="HTML")


# --- to'lovlar ---
@dp.callback_query(F.data == "a:pays", ADM)
async def a_pays(cb: CallbackQuery):
    rows = db("SELECT kind, ref, price FROM prices WHERE price > 0", (), True)
    lines = [f"{'📺' if r['kind'] == 'ser' else '🎬'} {escape(item_name(r['kind'], r['ref']))} — {money(r['price'])} so'm" for r in rows]
    ok = db("SELECT COUNT(*) AS n FROM pays WHERE status='ok'", (), True)[0]["n"]
    wait = db("SELECT COUNT(*) AS n FROM pays WHERE status='pending'", (), True)[0]["n"]
    await cb.message.answer(
        "💰 <b>Pullik kontent</b>\n\n" + ("\n".join(lines) or "Hozircha pullik kontent yo'q.") +
        f"\n\n✅ Tasdiqlangan to'lovlar: {ok}\n⏳ Kutilayotgan cheklar: {wait}\n\n"
        "Narx va kartani web ilovada o'zgartirasiz: kino yoki serialni bosing → 💰 Narx va karta.",
        parse_mode="HTML", reply_markup=ADM_KB)
    await cb.answer()


@dp.message(F.photo | F.document, ~ADM, StateFilter(None))
async def receipt(m: Message):
    uid = m.from_user.id
    pd = db("SELECT kind, ref FROM pending WHERE uid=?", (uid,), True)
    if not pd:
        return  # to'lov so'ralmagan, oddiy rasmga e'tibor bermaymiz
    kind, ref = pd[0]["kind"], pd[0]["ref"]
    pr = price_of(kind, ref)
    if not pr:
        await m.answer("Bu kontent endi bepul. Kodni qayta yuboring.")
        return
    fid = m.photo[-1].file_unique_id if m.photo else m.document.file_unique_id
    dup = db("SELECT 1 AS x FROM pays WHERE file_uid=?", (fid,), True)
    async with _lock:
        pid = db("SELECT COALESCE(MAX(id),0) AS n FROM pays", (), True)[0]["n"] + 1
        db("INSERT INTO pays(id,uid,kind,ref,status,ts,file_uid) VALUES(?,?,?,?,?,?,?)",
           (pid, uid, kind, ref, "pending", int(time.time()), fid))
    note = (f"🧾 Yangi chek #{pid}\n👤 {m.from_user.full_name} (id {uid})\n"
            f"{'📺' if kind == 'ser' else '🎬'} {item_name(kind, ref)}\n💰 {money(pr['price'])} so'm")
    if dup:
        note += "\n⚠️ Bu chek rasmi avval ham yuborilgan!"
    kbd = kb([[btn("✅ Tasdiqlash", f"pa:{pid}"), btn("❌ Rad etish", f"pn:{pid}")]])
    for a in ADMINS:
        try:
            await m.copy_to(a, caption=note, reply_markup=kbd)
        except Exception:
            pass
    await m.answer("Chek qabul qilindi ✅ Tasdiqlanishini kuting, odatda bir necha daqiqa.")


@dp.callback_query(F.data.regexp(r"^p[an]:\d+$"), ADM)
async def pay_decide(cb: CallbackQuery):
    act, pid = cb.data.split(":")
    row = db("SELECT * FROM pays WHERE id=?", (int(pid),), True)
    if not row:
        await cb.answer("Topilmadi")
        return
    p = row[0]
    if p["status"] != "pending":
        await cb.answer("Bu chek allaqachon ko'rib chiqilgan", show_alert=True)
        return
    who = cb.from_user.full_name
    if act == "pa":
        db("UPDATE pays SET status='ok' WHERE id=?", (p["id"],))
        db("INSERT INTO buys(uid,kind,ref,ts) VALUES(?,?,?,?) ON CONFLICT DO NOTHING", (p["uid"], p["kind"], p["ref"], int(time.time())))
        db("DELETE FROM pending WHERE uid=?", (p["uid"],))
        mark = f"✅ Tasdiqladi: {who}"
        try:
            await send_stk(p["uid"], "paid")
            await bot.send_message(p["uid"], "✅ To'lov tasdiqlandi! Mana kontentingiz 👇")
            if p["kind"] == "film":
                f = get_film(p["ref"])
                if f:
                    await send_film(p["uid"], f)
            else:
                await send_series(p["uid"], int(p["ref"]))
        except Exception:
            mark += " (foydalanuvchiga yetib bormadi)"
    else:
        db("UPDATE pays SET status='no' WHERE id=?", (p["id"],))
        mark = f"❌ Rad etdi: {who}"
        try:
            await bot.send_message(p["uid"], "Chek tasdiqlanmadi. Iltimos, to'lov chekini aniq rasmda qayta yuboring.")
        except Exception:
            pass
    try:
        await cb.message.edit_caption(caption=(cb.message.caption or "") + f"\n\n{mark}", reply_markup=None)
    except Exception:
        pass
    await cb.answer(mark)


# --- /stats: faqat adminlar uchun ---
PERIODS = [(86400, "1 kunlik"), (43200, "12 soatlik"), (18000, "5 soatlik"), (3600, "1 soatlik")]
PERIOD_NAME = {86400: "1 kun", 43200: "12 soat", 18000: "5 soat", 3600: "1 soat"}


def period_kb(mode):
    btns = [btn(f"{lbl} analiz", f"st:{mode}:{sec}") for sec, lbl in PERIODS]
    return kb([btns[:2], btns[2:], [btn("⬅️ Bo'limlar", "st:menu")]])


def stats_general(since, name):
    total = db("SELECT COUNT(*) AS n FROM users", (), True)[0]["n"]
    new = db("SELECT COUNT(*) AS n FROM users WHERE ts >= ?", (since,), True)[0]["n"]
    v = db("SELECT COUNT(*) AS n, COUNT(DISTINCT uid) AS u FROM view_log WHERE ts >= ?", (since,), True)[0]
    top = db("SELECT kind, ref, COUNT(*) AS n FROM view_log WHERE ts >= ? GROUP BY kind, ref ORDER BY COUNT(*) DESC",
             (since,), True)[:10]
    lines = "\n".join(f"{i}. {'📺' if t['kind'] == 'ser' else '🎬'} {escape(item_name(t['kind'], t['ref']))} — 👁 {t['n']}"
                      for i, t in enumerate(top, 1)) or "Bu davrda ko'rishlar bo'lmadi."
    return (f"📊 <b>Umumiy analiz</b> (oxirgi {name})\n\n"
            f"🆕 Botga kelgan yangi odamlar: <b>{new}</b>\n👤 Jami foydalanuvchilar: {total}\n"
            f"👁 Ko'rishlar: <b>{v['n']}</b> (noyob odam: {v['u']})\n\n🔥 <b>Eng ko'p ko'rilganlar:</b>\n{lines}")


def stats_links(since, name):
    rows = db("SELECT name, ts FROM links ORDER BY ts DESC", (), True)[:15]
    if not rows:
        return "🔗 Havolalar hali yo'q. Admin panel → 🔗 Havolalar bo'limida yaratiladi."
    out = []
    for r in rows:
        c = db("SELECT COUNT(*) AS n, COALESCE(SUM(is_new),0) AS nw FROM link_users WHERE name=? AND ts >= ?",
               (r["name"], since), True)[0]
        tot = db("SELECT COUNT(*) AS n FROM link_users WHERE name=?", (r["name"],), True)[0]["n"]
        top = db("SELECT v.kind AS kind, v.ref AS ref, COUNT(*) AS n FROM view_log v "
                 "JOIN link_users l ON l.uid = v.uid AND l.name = ? WHERE v.ts >= ? "
                 "GROUP BY v.kind, v.ref ORDER BY COUNT(*) DESC", (r["name"], since), True)[:3]
        films = ", ".join(f"{escape(item_name(t['kind'], t['ref']))} ({t['n']})" for t in top) or "—"
        out.append(f"🔗 <b>{escape(r['name'])}</b>\n   👥 {c['n']} odam (yangi: {c['nw']}) · jami: {tot}\n   🎬 {films}")
    return f"🔗 <b>Havolalar analizi</b> (oxirgi {name})\n\n" + "\n\n".join(out)


@dp.message(Command("stats"), ADM)
async def cmd_stats(m: Message):
    await m.answer("📊 <b>Statistika</b>\n\nQaysi bo'limni ko'rasiz?", parse_mode="HTML",
                   reply_markup=kb([[btn("📊 Umumiy", "st:g"), btn("🔗 Havolalar", "st:l")]]))


@dp.callback_query(F.data == "st:menu", ADM)
async def st_back(cb: CallbackQuery):
    await cb.message.edit_text("📊 Statistika\n\nQaysi bo'limni ko'rasiz?",
                               reply_markup=kb([[btn("📊 Umumiy", "st:g"), btn("🔗 Havolalar", "st:l")]]))
    await cb.answer()


@dp.callback_query(F.data.in_({"st:g", "st:l"}), ADM)
async def st_menu(cb: CallbackQuery):
    await cb.message.edit_text("📊 Davrni tanlang:", reply_markup=period_kb(cb.data[3:]))
    await cb.answer()


@dp.callback_query(F.data.regexp(r"^st:[gl]:\d+$"), ADM)
async def st_show(cb: CallbackQuery):
    _, mode, sec = cb.data.split(":")
    sec = int(sec)
    since = int(time.time()) - sec
    name = PERIOD_NAME.get(sec, f"{sec // 60} daqiqa")
    text = stats_general(since, name) if mode == "g" else stats_links(since, name)
    try:
        await cb.message.edit_text(text[:4000], parse_mode="HTML", reply_markup=period_kb(mode))
    except TelegramBadRequest:
        pass  # xabar o'zgarmagan bo'lsa
    await cb.answer()


# ---------- Kod yoki nom yozilganda ----------
@dp.message(F.text, ~F.text.startswith("/"), StateFilter(None))
async def text(m: Message):
    uid = m.from_user.id
    touch(uid)
    q = m.text.strip()
    f = get_film(q)
    sr = None if f else get_series_by_code(q)
    gc = ("film_" + f["code"]) if f else (("ser_" + str(sr["id"])) if sr else None)
    if not await gate(uid, m.chat.id, gc):
        return
    if f:
        await send_film(m.chat.id, f)
        return
    if sr:
        await send_series(m.chat.id, sr["id"])
        return
    rows = [[btn(("🧸 " if r["cat"] == "cartoon" else "🎬 ") + r["name"], "f:" + r["code"])] for r in search(q)[:10]]
    rows += [[btn("📺 " + r["name"], "s:" + str(r["id"]))] for r in search_series(q)[:10]]
    if not rows:
        await m.answer("Topilmadi 😕 Kodni tekshiring yoki «Kinolarni qidirish» tugmasini bosing.", reply_markup=home_kb(uid))
        return
    await m.answer("Topilganlar:", reply_markup=kb(rows))


# ---------- Veb qism (Mini App) ----------
def uid_of(r):
    return verify_init(r.headers.get("X-Init-Data", ""))


async def index(_):
    return web.FileResponse(os.path.join(BASE, "index.html"), headers={"Cache-Control": "no-store"})


async def health(_):
    return web.Response(text="ok")


async def state_api(r):
    uid = uid_of(r)
    if not uid:
        return web.json_response({"ok": False})
    touch(uid)
    miss = await missing(uid)
    data = {"ok": True, "bot": (await bot.me()).username, "films": [], "series": [], "admin": uid in ADMINS,
            "missing": [{"title": c["title"], "link": c["link"]} for c in miss]}
    if not miss:
        adm = uid in ADMINS
        prc = {(x["kind"], x["ref"]): x for x in db("SELECT kind, ref, price, card, holder FROM prices WHERE price > 0", (), True)}
        mine = {(x["kind"], x["ref"]) for x in db("SELECT kind, ref FROM buys WHERE uid=?", (uid,), True)}

        def paid_info(kind, ref):
            pp = prc.get((kind, str(ref)))
            if not pp:
                return {"price": 0, "owned": True}
            out = {"price": pp["price"], "owned": adm or (kind, str(ref)) in mine}
            if adm:
                out.update(card=pp["card"], holder=pp["holder"] or "")
            return out
        data["films"] = [{**f, **paid_info("film", f["code"])} for f in db(
            "SELECT code, name, COALESCE(views,0) AS views, COALESCE(cat,'film') AS cat FROM films ORDER BY ts DESC", (), True)]
        vw = {r["sid"]: (r["views"] or 0) for r in db("SELECT sid, views FROM series_views", (), True)}
        by = {}
        for r in db("SELECT sid, ep FROM series_eps ORDER BY sid, ep", (), True):
            by.setdefault(r["sid"], []).append(r["ep"])
        data["series"] = [{"id": x["id"], "name": x["name"], "code": x["code"] or "", "n": len(by[x["id"]]), "eps": by[x["id"]],
                           "v": hashlib.md5(x["poster"].encode()).hexdigest()[:8],
                           "views": vw.get(x["id"], 0), **paid_info("ser", x["id"])}
                          for x in db("SELECT id, name, poster, code FROM series ORDER BY id DESC", (), True) if x["id"] in by]
        data["top"] = [r for r in db("SELECT code,name,views FROM films ORDER BY views DESC, ts DESC", (), True)[:10]
                       if (r["views"] or 0) > 0]
    return web.json_response(data)


async def poster(r):
    try:
        sid = int(r.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound()
    data = _posters.get(sid)
    if data is None:
        s = get_series(sid)
        if not s:
            raise web.HTTPNotFound()
        f = await bot.get_file(s["poster"])
        buf = io.BytesIO()
        await bot.download_file(f.file_path, buf)
        data = buf.getvalue()
        if len(_posters) > 200:
            _posters.clear()
        _posters[sid] = data
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


async def film_code(r):
    if uid_of(r) not in ADMINS:
        return web.json_response({"error": "Ruxsat yo'q"}, status=403)
    try:
        d = await r.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)
    old, new = str(d.get("old", "")).strip().lower(), str(d.get("new", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", new):
        return web.json_response({"error": "Kod faqat lotin harf, raqam, _ yoki - bo'lsin (32 gacha)"}, status=400)
    if not get_film(old):
        return web.json_response({"error": "Kino topilmadi"}, status=404)
    if new != old and code_taken(new):
        return web.json_response({"error": "Bu kod band"}, status=400)
    db("UPDATE films SET code=? WHERE code=?", (new, old))
    for tbl in ("prices", "buys", "pending", "pays"):
        db(f"UPDATE {tbl} SET ref=? WHERE kind='film' AND ref=?", (new, old))
    return web.json_response({"ok": True})


async def series_code(r):
    if uid_of(r) not in ADMINS:
        return web.json_response({"error": "Ruxsat yo'q"}, status=403)
    try:
        d = await r.json()
        sid, code = int(d.get("id")), str(d.get("code", "")).strip().lower()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)
    if not get_series(sid):
        return web.json_response({"error": "Serial topilmadi"}, status=404)
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", code):
        return web.json_response({"error": "Kod faqat lotin harf, raqam, _ yoki - bo'lsin (32 gacha)"}, status=400)
    taken = code_taken(code, skip_series=sid)
    if taken:
        return web.json_response({"error": f"Bu kod {taken} band"}, status=400)
    db("UPDATE series SET code=? WHERE id=?", (code, sid))
    return web.json_response({"ok": True})


async def film_cat(r):
    if uid_of(r) not in ADMINS:
        return web.json_response({"error": "Ruxsat yo'q"}, status=403)
    try:
        d = await r.json()
    except Exception:
        return web.json_response({"error": "Noto'g'ri so'rov"}, status=400)
    code, cat = str(d.get("code", "")).strip().lower(), d.get("cat")
    if cat not in ("film", "cartoon") or not get_film(code):
        return web.json_response({"error": "Topilmadi"}, status=400)
    db("UPDATE films SET cat=? WHERE code=?", (cat, code))
    return web.json_response({"ok": True})


async def price_set(r):
    if uid_of(r) not in ADMINS:
        return web.json_response({"error": "Ruxsat yo'q"}, status=403)
    try:
        d = await r.json()
        kind, ref = d.get("kind"), str(d.get("ref", "")).strip()
        if kind == "film":
            ref = ref.lower()
            found = bool(get_film(ref))
        elif kind == "ser":
            found = ref.isdigit() and bool(get_series(int(ref)))
        else:
            found = False
        if not found:
            return web.json_response({"error": "Kino yoki serial topilmadi"}, status=400)
        price = int(d.get("price") or 0)
        if price <= 0:
            db("DELETE FROM prices WHERE kind=? AND ref=?", (kind, ref))
            return web.json_response({"ok": True})
        if price > 100_000_000:
            return web.json_response({"error": "Narx juda katta"}, status=400)
        card = re.sub(r"\D", "", str(d.get("card", "")))
        if not (12 <= len(card) <= 19):
            return web.json_response({"error": "Karta raqami 12-19 ta raqamdan iborat bo'lsin"}, status=400)
        card = " ".join(card[i:i + 4] for i in range(0, len(card), 4))
        holder = str(d.get("holder", "")).strip()[:40]
    except Exception:
        return web.json_response({"error": "Noto'g'ri ma'lumot"}, status=400)
    db("INSERT INTO prices(kind,ref,price,card,holder) VALUES(?,?,?,?,?) ON CONFLICT(kind,ref) DO UPDATE SET "
       "price=excluded.price, card=excluded.card, holder=excluded.holder", (kind, ref, price, card, holder))
    return web.json_response({"ok": True})


async def main():
    init_db()
    load_emoji()
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/health", health), web.get("/api/state", state_api),
                    web.get("/poster/{id}", poster),
                    web.post("/api/admin/film-code", film_code),
                    web.post("/api/admin/price", price_set), web.post("/api/admin/film-cat", film_cat),
                    web.post("/api/admin/series-code", series_code)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
