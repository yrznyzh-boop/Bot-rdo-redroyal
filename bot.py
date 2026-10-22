import logging
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "rdo_bot.sqlite3")
LOCAL_TZ = ZoneInfo("Europe/Berlin")

# These are Telegram custom-emoji IDs supplied by the bot owner.
CUSTOM_EMOJI = {
    "duplicate": ("5766928188353549811", "❎"),
    "turn": ("5215492745900077682", "✅"),
    "crown": ("5789939321750427039", "👑"),
    "party": ("5461151367559141950", "🎉"),
    "registered": ("5271887539102295688", "👍"),
    "stopped": ("5915740979729535606", "💞"),
    "time": ("5413704112220949842", "⏰"),
    "date": ("6071398415672020586", "📆"),
    "list": ("6334391113020868435", "📋"),
    "member": ("5373012449597335010", "👤"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rdo_distribution_bot")


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_database():
    with connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS distributions (
                chat_id INTEGER NOT NULL,
                original_message_id INTEGER NOT NULL,
                started_by INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (chat_id, original_message_id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS registrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                original_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                game_id TEXT NOT NULL,
                registration_message_id INTEGER NOT NULL,
                registered_at TEXT NOT NULL,
                UNIQUE (chat_id, original_message_id, user_id)
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS registrations_by_distribution
            ON registrations(chat_id, original_message_id, id)
        """)


def custom_emoji(key):
    emoji_id, fallback = CUSTOM_EMOJI[key]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def plain_fallback(html_text):
    text = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', html_text)
    text = re.sub(r'<b>(.*?)</b>', r'\1', text)
    text = re.sub(r'<code>(.*?)</code>', r'\1', text)
    return text


async def send_message(context, chat_id, text, reply_to=None):
    kwargs = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": ParseMode.HTML,
        "allow_sending_without_reply": True,
    }
    if reply_to is not None:
        kwargs["reply_to_message_id"] = reply_to
    try:
        return await context.bot.send_message(**kwargs)
    except BadRequest as exc:
        # If Telegram rejects a custom-emoji entity, retry using ordinary emoji.
        log.warning("Custom emoji message rejected; retrying fallback: %s", exc)
        kwargs.pop("parse_mode", None)
        kwargs["text"] = plain_fallback(text)
        return await context.bot.send_message(**kwargs)


async def user_is_admin(update, context):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except TelegramError as exc:
        log.warning("Admin check failed: %s", exc)
        return False


def get_distribution(con, chat_id, original_message_id):
    return con.execute(
        """SELECT * FROM distributions
           WHERE chat_id=? AND original_message_id=?""",
        (chat_id, original_message_id),
    ).fetchone()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "بوت توزيع عينات Red Dead Online يعمل.\\n"
        "المشرف: أرسل رسالة في القروب ثم رد عليها بكلمة توزيع.\\n"
        "الأعضاء: ردّوا على الرسالة الأصلية وأرسلوا ID اللعبة.\\n"
        "أوامر المشرف: قائمة، وقف توزيع. ولإعطاء الدور أرسل رقم العضو كرسالة عادية."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    text = message.text.strip()
    chat_id = message.chat_id
    replied = message.reply_to_message

    # Begin a separate distribution tied to the message being replied to.
    if text == "توزيع" and replied:
        if not await user_is_admin(update, context):
            return

        now = datetime.now(LOCAL_TZ)
        with connect() as con:
            previous = get_distribution(con, chat_id, replied.message_id)
            if previous and previous["active"]:
                await send_message(context, chat_id, "التوزيع مفتوح بالفعل.")
                return

            con.execute(
                """INSERT OR REPLACE INTO distributions
                   (chat_id, original_message_id, started_by, started_at, active)
                   VALUES (?, ?, ?, ?, 1)""",
                (chat_id, replied.message_id, user.id, now.isoformat(timespec="seconds")),
            )
            # Starting again on the same original message creates a fresh round.
            con.execute(
                "DELETE FROM registrations WHERE chat_id=? AND original_message_id=?",
                (chat_id, replied.message_id),
            )

        start_text = (
            f'{custom_emoji("date")} تاريخ البداية: <b>{now:%Y-%m-%d}</b>\\n'
            f'{custom_emoji("time")} وقت البداية: <b>{now:%H:%M:%S}</b>\\n'
            "للتسجيل، ردّوا على الرسالة الأصلية وأرسلوا ID اللعبة."
        )
        await send_message(context, chat_id, start_text, replied.message_id)
        return

    # Admin-only controls; they must be replies to the original message.
    if text in ("قائمة", "وقف توزيع") and replied:
        if not await user_is_admin(update, context):
            return

        with connect() as con:
            distribution = get_distribution(con, chat_id, replied.message_id)
            if not distribution:
                await send_message(context, chat_id, "ما لقيت توزيع مرتبط بهذه الرسالة.")
                return

            registrations = con.execute(
                """SELECT * FROM registrations
                   WHERE chat_id=? AND original_message_id=? ORDER BY id""",
                (chat_id, replied.message_id),
            ).fetchall()

            if text == "قائمة":
                lines = [f'{custom_emoji("list")} <b>قائمة المسجلين ({len(registrations)})</b>']
                for number, entry in enumerate(registrations, start=1):
                    lines.append(
                        f'{custom_emoji("member")} {number}. '
                        f'{entry["display_name"]} — <code>{entry["game_id"]}</code>'
                    )
                if not registrations:
                    lines.append("لا يوجد مسجلون حتى الآن.")
                await send_message(context, chat_id, "\\n".join(lines))
                return

            if not distribution["active"]:
                await send_message(context, chat_id, "هذا التوزيع متوقف من قبل.")
                return

            con.execute(
                """UPDATE distributions SET active=0
                   WHERE chat_id=? AND original_message_id=?""",
                (chat_id, replied.message_id),
            )
            started = datetime.fromisoformat(distribution["started_at"])
            elapsed = str(datetime.now(LOCAL_TZ) - started).split(".")[0]

        report = (
            f'{custom_emoji("stopped")} <b>تم إيقاف التوزيع</b>\\n'
            f'عدد المسجلين: <b>{len(registrations)}</b>\\n'
            f'وقت البداية: <b>{started:%Y-%m-%d %H:%M:%S}</b>\\n'
            f'مدة التوزيع: <b>{elapsed}</b>'
        )
        await send_message(context, chat_id, report, replied.message_id)
        return

    # Admin sends a number as a standalone message, not as a reply.
    # The newest active distribution containing that number is selected.
    if re.fullmatch(r"[0-9]+", text) and replied is None:
        if not await user_is_admin(update, context):
            return

        number = int(text)
        selected = None
        with connect() as con:
            active_distributions = con.execute(
                """SELECT original_message_id FROM distributions
                   WHERE chat_id=? AND active=1 ORDER BY started_at DESC""",
                (chat_id,),
            ).fetchall()

            for dist in active_distributions:
                rows = con.execute(
                    """SELECT * FROM registrations
                       WHERE chat_id=? AND original_message_id=? ORDER BY id""",
                    (chat_id, dist["original_message_id"]),
                ).fetchall()
                if 1 <= number <= len(rows):
                    selected = rows[number - 1]
                    break

        if selected:
            turn_text = (
                f'وصل دورك {custom_emoji("turn")} '
                f'{custom_emoji("crown")}{custom_emoji("party")} اقبل وجون'
            )
            await send_message(
                context, chat_id, turn_text, selected["registration_message_id"]
            )
        return

    # Register anyone replying directly to an active distribution's original post.
    if replied and text not in ("توزيع", "قائمة", "وقف توزيع"):
        with connect() as con:
            distribution = get_distribution(con, chat_id, replied.message_id)
            if not distribution or not distribution["active"]:
                return

            previous = con.execute(
                """SELECT id FROM registrations
                   WHERE chat_id=? AND original_message_id=? AND user_id=?""",
                (chat_id, replied.message_id, user.id),
            ).fetchone()

            if previous:
                number = con.execute(
                    """SELECT COUNT(*) AS total FROM registrations
                       WHERE chat_id=? AND original_message_id=? AND id<=?""",
                    (chat_id, replied.message_id, previous["id"]),
                ).fetchone()["total"]
                await send_message(
                    context,
                    chat_id,
                    f'أنت مسجل من قبل، رقمك هو <b>{number}</b> {custom_emoji("duplicate")}',
                    message.message_id,
                )
                return

            display_name = user.full_name or user.username or str(user.id)
            con.execute(
                """INSERT INTO registrations
                   (chat_id, original_message_id, user_id, display_name,
                    game_id, registration_message_id, registered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    chat_id, replied.message_id, user.id, display_name, text,
                    message.message_id, datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                ),
            )
            number = con.execute(
                """SELECT COUNT(*) AS total FROM registrations
                   WHERE chat_id=? AND original_message_id=?""",
                (chat_id, replied.message_id),
            ).fetchone()["total"]

        await send_message(
            context,
            chat_id,
            f'تم تسجيلك، رقمك هو <b>{number}</b> {custom_emoji("registered")}',
            message.message_id,
        )


def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add your BotFather token as an environment variable."
        )

    init_database()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("RDO distribution bot is running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
