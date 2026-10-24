import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("rdo-distribution")
DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
TOKEN = os.getenv("BOT_TOKEN", "").strip()

EMOJI = {
    "duplicate": ("5766928188353549811", "❎"),
    "turn": ("5215492745900077682", "✅"),
    "crown": ("5789939321750427039", "👑"),
    "start": ("5461151367559141950", "🎉"),
    "register": ("5271887539102295688", "👍"),
    "stop": ("5915740979729535606", "💞"),
    "time": ("5413704112220949842", "⏰"),
    "date": ("6071398415672020586", "📆"),
    "list": ("6334391113020868435", "📋"),
    "member": ("5373012449597335010", "👤"),
}

def em(key):
    emoji_id, fallback = EMOJI[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db

def init_db():
    with db_connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS group_counters (
            chat_id INTEGER PRIMARY KEY, next_number INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
            source_user_id INTEGER, started_at TEXT NOT NULL, stopped_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','stopped')),
            UNIQUE(chat_id, source_message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dist_active
            ON distributions(chat_id,status,id);
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL REFERENCES distributions(id),
            chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL, game_id TEXT NOT NULL,
            registration_number INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            UNIQUE(distribution_id,user_id), UNIQUE(distribution_id,registration_number)
        );
        """)

        # Migrate older databases without deleting registrations or changing their numbers.
        # Older versions enforced uniqueness across the whole chat; numbers are now
        # unique within each distribution instead.
        indexes = db.execute("PRAGMA index_list(registrations)").fetchall()
        has_old_chat_unique = False
        for index in indexes:
            if index["unique"]:
                columns = [
                    row["name"] for row in db.execute(
                        f'PRAGMA index_info("{index["name"]}")'
                    ).fetchall()
                ]
                if columns == ["chat_id", "registration_number"]:
                    has_old_chat_unique = True
                    break

        if has_old_chat_unique:
            db.execute("""
                CREATE TABLE registrations_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    distribution_id INTEGER NOT NULL REFERENCES distributions(id),
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL, game_id TEXT NOT NULL,
                    registration_number INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
                    registered_at TEXT NOT NULL,
                    UNIQUE(distribution_id,user_id),
                    UNIQUE(distribution_id,registration_number)
                )
            """)
            db.execute("""
                INSERT INTO registrations_new
                    (id,distribution_id,chat_id,user_id,display_name,game_id,
                     registration_number,source_message_id,registered_at)
                SELECT id,distribution_id,chat_id,user_id,display_name,game_id,
                       registration_number,source_message_id,registered_at
                FROM registrations
            """)
            db.execute("DROP TABLE registrations")
            db.execute("ALTER TABLE registrations_new RENAME TO registrations")

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_regs_dist
            ON registrations(distribution_id,registration_number)
        """)

def is_group(update):
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))

async def is_admin(update, context):
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        log.exception("Could not check administrator status")
        return False

def get_distribution_by_source(chat_id, message_id):
    with db_connect() as db:
        return db.execute(
            "SELECT * FROM distributions WHERE chat_id=? AND source_message_id=?",
            (chat_id, message_id),
        ).fetchone()

def latest_active_distribution(chat_id):
    with db_connect() as db:
        return db.execute(
            """SELECT * FROM distributions WHERE chat_id=? AND status='active'
               ORDER BY id DESC LIMIT 1""", (chat_id,)
        ).fetchone()

def create_distribution(chat_id, source_message_id, source_user_id):
    with db_connect() as db:
        existing = db.execute(
            "SELECT * FROM distributions WHERE chat_id=? AND source_message_id=?",
            (chat_id, source_message_id),
        ).fetchone()
        if existing:
            return existing, False
        cur = db.execute(
            """INSERT INTO distributions
               (chat_id,source_message_id,source_user_id,started_at,status)
               VALUES(?,?,?,?, 'active')""",
            (chat_id, source_message_id, source_user_id,
             datetime.now(timezone.utc).isoformat()),
        )
        return db.execute(
            "SELECT * FROM distributions WHERE id=?", (cur.lastrowid,)
        ).fetchone(), True

def allocate_number(db, distribution_id):
    # Caller starts an IMMEDIATE transaction, so simultaneous registrations
    # cannot allocate the same next number.
    row = db.execute(
        "SELECT COALESCE(MAX(registration_number), 0) + 1 AS next_number "
        "FROM registrations WHERE distribution_id=?",
        (distribution_id,),
    ).fetchone()
    return row["next_number"]

def register_member(distribution_id, chat_id, user_id, display_name, game_id, source_message_id):
    with db_connect() as db:
        # Serialize the duplicate check + number allocation + insert.
        db.execute("BEGIN IMMEDIATE")
        dist = db.execute(
            "SELECT * FROM distributions WHERE id=? AND chat_id=?",
            (distribution_id, chat_id),
        ).fetchone()
        if not dist or dist["status"] != "active":
            return "closed", None
        previous = db.execute(
            "SELECT * FROM registrations WHERE distribution_id=? AND user_id=?",
            (distribution_id, user_id),
        ).fetchone()
        if previous:
            return "duplicate", previous
        number = allocate_number(db, distribution_id)
        cur = db.execute(
            """INSERT INTO registrations
               (distribution_id,chat_id,user_id,display_name,game_id,
                registration_number,source_message_id,registered_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (distribution_id, chat_id, user_id, display_name, game_id, number,
             source_message_id, datetime.now(timezone.utc).isoformat()),
        )
        return "ok", db.execute(
            "SELECT * FROM registrations WHERE id=?", (cur.lastrowid,)
        ).fetchone()

def list_registrations(distribution_id):
    with db_connect() as db:
        return db.execute(
            """SELECT * FROM registrations WHERE distribution_id=?
               ORDER BY registration_number""", (distribution_id,)
        ).fetchall()

def stop_distribution(distribution_id, chat_id):
    with db_connect() as db:
        cur = db.execute(
            """UPDATE distributions SET status='stopped',stopped_at=?
               WHERE id=? AND chat_id=? AND status='active'""",
            (datetime.now(timezone.utc).isoformat(), distribution_id, chat_id),
        )
        return cur.rowcount == 1

def display_time(value):
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")

def member_label(row):
    return (f'{em("member")} <b>{escape(row["display_name"])}</b>\n'
            f'   الرقم: <code>{row["registration_number"]}</code>\n'
            f'   ID اللعبة: <code>{escape(row["game_id"])}</code>')

async def send_long_html(message, text):
    while len(text) > 3900:
        split_at = text.rfind("\n\n", 0, 3900)
        if split_at < 1:
            split_at = 3900
        await message.reply_text(text[:split_at], parse_mode=ParseMode.HTML)
        text = text[split_at:].lstrip()
    if text:
        await message.reply_text(text, parse_mode=ParseMode.HTML)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not is_group(update) or not update.effective_user:
        return
    text = (msg.text or "").strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    admin = await is_admin(update, context)

    # Friendly greeting for every regular text message in the group.
    # The supplied custom emoji ID is placed at the end of the greeting.
    if user.id != context.bot.id and text:
        await msg.reply_text(
            'هلا يا عيوني <tg-emoji emoji-id="5915873784413296476">❤️</tg-emoji>',
            parse_mode=ParseMode.HTML,
        )

    if text == "توزيع":
        if not admin:
            return
        if not msg.reply_to_message:
            await msg.reply_text("يرجى الرد على الرسالة الأصلية بكلمة «توزيع».")
            return
        dist, created = create_distribution(
            chat_id, msg.reply_to_message.message_id, user.id
        )
        if not created:
            await msg.reply_text("يوجد توزيع مرتبط بهذه الرسالة مسبقاً.")
            return
        await msg.reply_text(
            f'{em("start")} <b>بدأ توزيع جديد!</b>\n'
            f'رقم التوزيع: <code>#{dist["id"]}</code>\n'
            f'{em("date")} التاريخ والوقت: <code>{display_time(dist["started_at"])}</code>\n\n'
            "للتسجيل: رد على الرسالة الأصلية نفسها وأرسل ID اللعبة.",
            parse_mode=ParseMode.HTML,
        )
        return

    if text in ("قائمة", "وقف توزيع"):
        if not admin:
            return
        dist = latest_active_distribution(chat_id)
        if not dist:
            await msg.reply_text("لا يوجد توزيع نشط حالياً.")
            return
        rows = list_registrations(dist["id"])
        if text == "قائمة":
            header = (f'{em("list")} <b>قائمة التوزيع #{dist["id"]}</b>\n'
                      f'عدد المسجلين: <b>{len(rows)}</b>\n\n')
            body = "\n\n".join(member_label(r) for r in rows)
            await send_long_html(msg, header + (body or "لا يوجد أعضاء مسجلون بعد."))
            return
        stop_distribution(dist["id"], chat_id)
        rows = list_registrations(dist["id"])
        header = (f'{em("stop")} <b>تم إيقاف التوزيع #{dist["id"]}</b>\n'
                  f'إجمالي المسجلين: <b>{len(rows)}</b>\n\n')
        body = "\n\n".join(member_label(r) for r in rows)
        await send_long_html(msg, header + (body or "لم يسجل أي عضو."))
        return

    if admin and re.fullmatch(r"\d+", text):
        with db_connect() as db:
            target_dist = db.execute(
                """SELECT * FROM distributions WHERE chat_id=?
                   ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC
                   LIMIT 1""",
                (chat_id,),
            ).fetchone()
            row = None
            if target_dist:
                row = db.execute(
                    """SELECT * FROM registrations
                       WHERE distribution_id=? AND registration_number=?""",
                    (target_dist["id"], int(text)),
                ).fetchone()
        if row:
            try:
                # Custom check after "وصل دورك", custom crown after "اقبل وجون".
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f'<b>وصل دورك {em("turn")} اقبل وجون {em("crown")}</b>',
                    reply_to_message_id=row["source_message_id"],
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.exception("Could not send turn notification")
                await msg.reply_text("تعذر إرسال التنبيه؛ تحقق من صلاحيات البوت في القروب.")
        return

    if msg.reply_to_message and text and len(text) <= 200:
        dist = get_distribution_by_source(chat_id, msg.reply_to_message.message_id)
        if not dist:
            return
        if dist["status"] != "active":
            await msg.reply_text("هذا التوزيع متوقف والتسجيل مغلق.")
            return
        status, row = register_member(
            dist["id"], chat_id, user.id,
            user.full_name or user.username or str(user.id),
            text, msg.message_id,
        )
        if status == "duplicate":
            await msg.reply_text(
                f'{em("duplicate")} أنت مسجل مسبقاً في هذا التوزيع، '
                f'ورقم تسجيلك السابق هو <b>{row["registration_number"]}</b>.',
                parse_mode=ParseMode.HTML,
            )
        elif status == "ok":
            await msg.reply_text(
                f'{em("register")} تم تسجيلك بنجاح!\n'
                f'رقم تسجيلك: <b>{row["registration_number"]}</b>',
                parse_mode=ParseMode.HTML,
            )

async def post_init(application):
    init_db()
    log.info("Database initialized; bot is starting.")

def main():
    if not TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Starting Telegram polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
