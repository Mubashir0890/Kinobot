import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, ChatJoinRequest, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.environ["ADMIN_IDS"].replace(" ", "").split(",") if x}
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


def touch(uid):
    db("INSERT INTO users(id,ts) VALUES(?,?) ON CONFLICT DO NOTHING", (uid, int(time.time())))


def get_film(code):
    r = db("SELECT * FROM films WHERE code=?", (str(code).strip().lower(),), True)
    return r[0] if r else None


def search(q):
    q = q.strip().casefold()
    return [f for f in db("SELECT code,name FROM films ORDER BY ts DESC", (), True)
            if q in f["name"].casefold()]


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


bot = Bot(TOKEN)
dp = Dispatcher()
ADM = F.from_user.id.in_(ADMINS)


def btn(text, cd=None, url=None, web=None):
    return InlineKeyboardButton(text=text, callback_data=cd, url=url,
                                web_app=WebAppInfo(url=web) if web else None)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


HOME_TXT = "Kino kodini yuboring yoki «Kinolarni qidirish» tugmasini bosing 👇"
ADM_KB = kb([[btn("➕ Kino qo'shish", "a:add"), btn("🗑 Kino o'chirish", "a:del")],
             [btn("🎞 Kinolar ro'yxati", "a:list"), btn("📢 Majburiy kanallar", "a:ch")],
             [btn("📊 Statistika", "a:stat"), btn("📣 Xabar yuborish", "a:bc")]])


def home_kb(uid):
    rows = [[btn("🔎 Kinolarni qidirish", web=WEBAPP_URL)]]
    if uid in ADMINS:
        rows.append([btn("⚙ Admin panel", "a:menu")])
    return kb(rows)


# ---------- Obunani tekshirish ----------
async def is_sub(uid, ch):
    try:
        m = await bot.get_chat_member(ch["chat_id"], uid)
        if m.status in ("member", "administrator", "creator"):
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
    rows.append([btn("✅ Tekshirish", "chk:" + (code or ""))])
    return kb(rows)


async def gate(uid, chat_id, code=None):
    miss = await missing(uid)
    if not miss:
        return True
    await bot.send_message(chat_id, "Davom etish uchun kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing 👇",
                           reply_markup=sub_kb(miss, code))
    return False


async def send_film(chat_id, f):
    await bot.copy_message(chat_id, STORAGE, f["msg_id"],
                           caption=f"🎬 {f['name']}\nKod: {f['code']}", protect_content=PROTECT)
    db("UPDATE films SET views=views+1 WHERE code=?", (f["code"],))


# ---------- Foydalanuvchi qismi ----------
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    touch(uid)
    arg = command.args or ""
    code = arg[5:] if arg.startswith("film_") else None
    if not await gate(uid, m.chat.id, code):
        return
    if code:
        f = get_film(code)
        if f:
            await send_film(m.chat.id, f)
            return
        await m.answer("Bunday kino topilmadi.")
    await m.answer(HOME_TXT, reply_markup=home_kb(uid))


@dp.callback_query(F.data.startswith("chk:"))
async def chk(cb: CallbackQuery):
    if await missing(cb.from_user.id):
        await cb.answer("Hali hammasiga obuna bo'lmadingiz", show_alert=True)
        return
    code = cb.data[4:]
    await cb.message.edit_text("Obuna tasdiqlandi ✅")
    f = get_film(code) if code else None
    if f:
        await send_film(cb.message.chat.id, f)
    else:
        await cb.message.answer(HOME_TXT, reply_markup=home_kb(cb.from_user.id))
    await cb.answer()


@dp.callback_query(F.data.startswith("f:"))
async def pick(cb: CallbackQuery):
    code = cb.data[2:]
    await cb.answer()
    if not await gate(cb.from_user.id, cb.message.chat.id, code):
        return
    f = get_film(code)
    if f:
        await send_film(cb.message.chat.id, f)


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
    await state.set_state(AddFilm.video)
    await cb.message.answer("Kinoni (video yoki fayl) yuboring.\nBekor qilish: /cancel")
    await cb.answer()


@dp.message(AddFilm.video, F.video | F.document, ADM)
async def af_video(m: Message, state: FSMContext):
    try:
        cp = await bot.copy_message(STORAGE, m.chat.id, m.message_id)
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
    await state.update_data(code=code)
    await state.set_state(AddFilm.name)
    note = "\n⚠️ Bu kod band, eski kino almashtiriladi." if get_film(code) else ""
    await m.answer("Kino nomini yozing:" + note)


@dp.message(AddFilm.name, F.text, ADM)
async def af_name(m: Message, state: FSMContext):
    d = await state.get_data()
    name = m.text.strip()[:80]
    db("INSERT INTO films(code,name,msg_id,views,ts) VALUES(?,?,?,0,?) "
       "ON CONFLICT(code) DO UPDATE SET name=excluded.name, msg_id=excluded.msg_id",
       (d["code"], name, d["msg_id"], int(time.time())))
    await state.clear()
    await m.answer(f"✅ Qo'shildi\nKod: {d['code']}\nNom: {name}\nWeb ilovada ham ko'rinadi.", reply_markup=ADM_KB)


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
    try:
        await bot.delete_message(STORAGE, f["msg_id"])
    except Exception:
        pass
    await state.clear()
    await m.answer(f"🗑 O'chirildi: {f['code']} — {f['name']}", reply_markup=ADM_KB)


# --- ro'yxat va statistika ---
@dp.callback_query(F.data == "a:list", ADM)
async def a_list(cb: CallbackQuery):
    rows = db("SELECT code,name,views FROM films ORDER BY ts DESC", (), True)
    text = "\n".join(f"{r['code']} — {r['name']} (👁 {r['views']})" for r in rows) or "Hozircha kino yo'q."
    await cb.message.answer(text[:4000], reply_markup=ADM_KB)
    await cb.answer()


@dp.callback_query(F.data == "a:stat", ADM)
async def a_stat(cb: CallbackQuery):
    u = db("SELECT COUNT(*) AS n FROM users", (), True)[0]["n"]
    f = db("SELECT COUNT(*) AS n, COALESCE(SUM(views),0) AS v FROM films", (), True)[0]
    c = db("SELECT COUNT(*) AS n FROM channels", (), True)[0]["n"]
    await cb.message.answer(f"👥 Foydalanuvchilar: {u}\n🎬 Kinolar: {f['n']}\n👁 Jami ko'rishlar: {f['v']}\n📢 Majburiy kanallar: {c}",
                            reply_markup=ADM_KB)
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


# ---------- Kod yoki nom yozilganda ----------
@dp.message(F.text, ~F.text.startswith("/"), StateFilter(None))
async def text(m: Message):
    uid = m.from_user.id
    touch(uid)
    q = m.text.strip()
    f = get_film(q)
    if not await gate(uid, m.chat.id, f["code"] if f else None):
        return
    if f:
        await send_film(m.chat.id, f)
        return
    res = search(q)[:10]
    if not res:
        await m.answer("Topilmadi 😕 Kodni tekshiring yoki «Kinolarni qidirish» tugmasini bosing.", reply_markup=home_kb(uid))
        return
    await m.answer("Topilgan kinolar:", reply_markup=kb([[btn("🎬 " + r["name"], "f:" + r["code"])] for r in res]))


# ---------- Veb qism (Mini App) ----------
def uid_of(r):
    return verify_init(r.headers.get("X-Init-Data", ""))


async def index(_):
    return web.FileResponse(os.path.join(BASE, "index.html"))


async def health(_):
    return web.Response(text="ok")


async def state_api(r):
    uid = uid_of(r)
    if not uid:
        return web.json_response({"ok": False})
    touch(uid)
    miss = await missing(uid)
    data = {"ok": True, "bot": (await bot.me()).username, "films": [],
            "missing": [{"title": c["title"], "link": c["link"]} for c in miss]}
    if not miss:
        data["films"] = db("SELECT code,name FROM films ORDER BY ts DESC", (), True)
    return web.json_response(data)


async def main():
    init_db()
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/health", health), web.get("/api/state", state_api)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
