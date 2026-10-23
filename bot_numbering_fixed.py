import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, ContextTypes, MessageHandler, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("rdo-distribution")

DB_PATH = Path(os.getenv("DB_PATH", "data/bot.sqlite3"))
TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Telegram custom emoji IDs. Each tag includes a standard emoji fallback.
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

def em(key: str) -> str:
    emoji_id, fallback = EMOJI[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def now_local() -> datetime:
    # Store UTC timestamps consistently; display in server local time.
    return datetime.now(timezone.utc)

def db_connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db():
    with db_connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS group_counters (
            chat_id INTEGER PRIMARY KEY,
            next_number INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS distributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            source_user_id INTEGER,
            started_at TEXT NOT NULL,
            stopped_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('active','stopped')),
            UNIQUE(chat_id, source_message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dist_active
            ON distributions(chat_id, status, id);
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            distribution_id INTEGER NOT NULL REFERENCES distributions(id),
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            game_id TEXT NOT NULL,
            registration_number INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            registered_at TEXT NOT NULL,
            UNIQUE(distribution_id, user_id),
            UNIQUE(distribution_id, registration_number)
        );
        CREATE INDEX IF NOT EXISTS idx_regs_dist
            ON registrations(distribution_id, registration_number);
        """)
        # Migrate older databases from group-wide numbering to per-distribution
        # numbering without deleting any existing registrations.
        unique_indexes = db.execute("PRAGMA index_list(registrations)").fetchall()
        has_old_group_unique = False
        for index in unique_indexes:
            if index["unique"]:
                cols = [r["name"] for r in db.execute(
                    f"PRAGMA index_info('{index['name']}')"
                ).fetchall()]
                if cols == ["chat_id", "registration_number"]:
                    has_old_group_unique = True
                    break
        if has_old_group_unique:
            db.execute("DROP INDEX IF EXISTS idx_regs_dist")
            db.execute("""
                CREATE TABLE registrations_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    distribution_id INTEGER NOT NULL REFERENCES distributions(id),
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    display_name TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    registration_number INTEGER NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    registered_at TEXT NOT NULL,
                    UNIQUE(distribution_id, user_id),
                    UNIQUE(distribution_id, registration_number)
                )
            """)
            db.execute("""
                INSERT INTO registrations_new
                (id, distribution_id, chat_id, user_id, display_name, game_id,
                 registration_number, source_message_id, registered_at)
                SELECT id, distribution_id, chat_id, user_id, display_name, game_id,
                       registration_number, source_message_id, registered_at
                FROM registrations
            """)
            db.execute("DROP TABLE registrations")
            db.execute("ALTER TABLE registrations_new RENAME TO registrations")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_regs_dist
                           ON registrations(distribution_id, registration_number)""")
        # Migrate older installations if optional columns are ever added later.
        db.commit()

def is_group(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type in ("group", "supergroup"))

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        log.exception("Could not check administrator status")
        return False

def get_distribution_by_source(chat_id: int, message_id: int):
    with db_connect() as db:
        return db.execute(
            """SELECT * FROM distributions
               WHERE chat_id=? AND source_message_id=?""",
            (chat_id, message_id),
        ).fetchone()

def latest_active_distribution(chat_id: int):
    with db_connect() as db:
        return db.execute(
            """SELECT * FROM distributions
               WHERE chat_id=? AND status='active'
               ORDER BY id DESC LIMIT 1""",
            (chat_id,),
        ).fetchone()

def create_distribution(chat_id: int, source_message_id: int, source_user_id: int):
    stamp = now_local().isoformat()
    with db_connect() as db:
        existing = db.execute(
            "SELECT * FROM distributions WHERE chat_id=? AND source_message_id=?",
            (chat_id, source_message_id),
        ).fetchone()
        if existing:
            return existing, False
        cur = db.execute(
            """INSERT INTO distributions
               (chat_id, source_message_id, source_user_id, started_at, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (chat_id, source_message_id, source_user_id, stamp),
        )
        row = db.execute("SELECT * FROM distributions WHERE id=?", (cur.lastrowid,)).fetchone()
        return row, True

def allocate_number(db, chat_id: int, distribution_id: int) -> int:
    """Allocate the next registration number within this distribution only."""
    row = db.execute(
        """SELECT COALESCE(MAX(registration_number), 0) + 1
           FROM registrations
           WHERE distribution_id=?""",
        (distribution_id,),
    ).fetchone()
    return row[0]

def register_member(distribution_id: int, chat_id: int, user_id: int,
                    display_name: str, game_id: str, source_message_id: int):
    with db_connect() as db:
        dist = db.execute(
            "SELECT * FROM distributions WHERE id=? AND chat_id=?",
            (distribution_id, chat_id),
        ).fetchone()
        if not dist or dist["status"] != "active":
            return "closed", None
        previous = db.execute(
            """SELECT * FROM registrations
               WHERE distribution_id=? AND user_id=?""",
            (distribution_id, user_id),
        ).fetchone()
        if previous:
            return "duplicate", previous
        number = allocate_number(db, chat_id, distribution_id)
        cur = db.execute(
            """INSERT INTO registrations
               (distribution_id,chat_id,user_id,display_name,game_id,
                registration_number,source_message_id,registered_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (distribution_id, chat_id, user_id, display_name, game_id,
             number, source_message_id, now_local().isoformat()),
        )
        row = db.execute("SELECT * FROM registrations WHERE id=?", (cur.lastrowid,)).fetchone()
        return "ok", row

def list_registrations(distribution_id: int):
    with db_connect() as db:
        return db.execute(
            """SELECT * FROM registrations WHERE distribution_id=?
               ORDER BY registration_number""",
            (distribution_id,),
        ).fetchall()

def stop_distribution(distribution_id: int, chat_id: int):
    with db_connect() as db:
        cur = db.execute(
            """UPDATE distributions SET status='stopped', stopped_at=?
               WHERE id=? AND chat_id=? AND status='active'""",
            (now_local().isoformat(), distribution_id, chat_id),
        )
        return cur.rowcount == 1

def display_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")

def member_label(row) -> str:
    return (
        f'{em("member")} <b>{escape(row["display_name"])}</b>\n'
        f'   الرقم: <code>{row["registration_number"]}</code>\n'
        f'   ID اللعبة: <code>{escape(row["game_id"])}</code>'
    )

async def send_member_report(message, header: str, rows):
    # Keep each member block intact so HTML tags are never split across messages.
    await message.reply_text(header, parse_mode=ParseMode.HTML)
    if not rows:
        await message.reply_text("لا يوجد أعضاء مسجلون بعد.")
        return
    chunk = ""
    for row in rows:
        block = member_label(row) + "\n\n"
        if chunk and len(chunk) + len(block) > 3500:
            await message.reply_text(chunk, parse_mode=ParseMode.HTML)
            chunk = ""
        chunk += block
    if chunk:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not is_group(update) or not update.effective_user:
        return
    text = (msg.text or "").strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    admin = await is_admin(update, context)

    # Start a distribution by replying "توزيع" to a normal group message.
    if text == "توزيع":
        if not admin:
            return
        if not msg.reply_to_message:
            await msg.reply_text("يرجى الرد على الرسالة الأصلية بكلمة «توزيع».")
            return
        source = msg.reply_to_message
        dist, created = create_distribution(chat_id, source.message_id, user.id)
        if not created:
            await msg.reply_text("يوجد توزيع مرتبط بهذه الرسالة مسبقاً.")
            return
        started = display_time(dist["started_at"])
        await msg.reply_text(
            f'{em("start")} <b>بدأ توزيع جديد!</b>\n'
            f'رقم التوزيع: <code>#{dist["id"]}</code>\n'
            f'{em("date")} التاريخ والوقت: <code>{started}</code>\n\n'
            'للتسجيل: رد على الرسالة الأصلية نفسها وأرسل ID اللعبة.',
            parse_mode=ParseMode.HTML,
        )
        return

    # Admin commands are plain messages, not replies.
    if text in ("قائمة", "وقف توزيع"):
        if not admin:
            return
        dist = latest_active_distribution(chat_id)
        if not dist:
            await msg.reply_text("لا يوجد توزيع نشط حالياً.")
            return
        rows = list_registrations(dist["id"])
        if text == "قائمة":
            header = (
                f'{em("list")} <b>قائمة التوزيع #{dist["id"]}</b>\n'
                f'عدد المسجلين: <b>{len(rows)}</b>\n\n'
            )
            await send_member_report(msg, header, rows)
            return
        stop_distribution(dist["id"], chat_id)
        rows = list_registrations(dist["id"])
        header = (
            f'{em("stop")} <b>تم إيقاف التوزيع #{dist["id"]}</b>\n'
            f'إجمالي المسجلين: <b>{len(rows)}</b>\n\n'
        )
        await send_member_report(msg, header, rows)
        return

    # A bare integer from an admin awards the turn. Registration numbers are
    # unique within a group across simultaneous distributions to avoid ambiguity.
    if admin and not msg.reply_to_message and re.fullmatch(r"\d+", text):
        number = int(text)
        with db_connect() as db:
            row = db.execute(
                """SELECT * FROM registrations
                   WHERE chat_id=? AND registration_number=?""",
                (chat_id, number),
            ).fetchone()
        if row:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f'{em("turn")} <b>وصل دورك اقبل وجون</b>',
                    reply_to_message_id=row["source_message_id"],
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                log.exception("Could not send turn notification")
                await msg.reply_text("تعذر إرسال التنبيه؛ تحقق من صلاحيات البوت في القروب.")
        return

    # Registration only by replying directly to the distribution's source message.
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

async def post_init(application: Application):
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
