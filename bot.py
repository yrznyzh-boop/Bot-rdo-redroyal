import os, re, sqlite3, logging
from datetime import datetime
from html import escape
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN=os.getenv("BOT_TOKEN","").strip()
DB=os.getenv("DB_PATH","rdo_bot.sqlite3")
logging.basicConfig(level=logging.INFO)
log=logging.getLogger("rdo")

EM={"party":"5461151367559141950","registered":"5271887539102295688","duplicate":"5766928188353549811","turn":"5215492745900077682","stopped":"5915740979729535606","date":"6071398415672020586"}
def em(k, fallback): return f'<tg-emoji emoji-id="{EM[k]}">{fallback}</tg-emoji>' if k in EM else fallback
def connect():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
 c.execute("CREATE TABLE IF NOT EXISTS dist(id INTEGER PRIMARY KEY, chat INTEGER, source INTEGER, started TEXT, active INTEGER DEFAULT 1)")
 c.execute("""CREATE TABLE IF NOT EXISTS reg(id INTEGER PRIMARY KEY, did INTEGER, uid INTEGER, name TEXT, game TEXT, msg INTEGER, UNIQUE(did,uid))""")
 c.commit(); return c
async def admin(u,ctx):
 try: return (await ctx.bot.get_chat_member(u.effective_chat.id,u.effective_user.id)).status in ("administrator","creator")
 except Exception: return False
async def start(u,ctx):
 await u.effective_message.reply_text("المشرف: رد على الرسالة الأصلية بكلمة توزيع.\\nالعضو: رد على الرسالة الأصلية واكتب ID اللعبة.\\nالمشرف: قائمة أو وقف توزيع، أو أرسل رقم التسجيل لإعطاء الدور.")
async def handle(u,ctx):
 m=u.effective_message; ch=u.effective_chat; user=u.effective_user
 if not m or not user or ch.type=="private" or not m.text: return
 t=m.text.strip(); isadm=await admin(u,ctx); c=connect()
 if t=="توزيع" and m.reply_to_message and isadm:
  src=m.reply_to_message.message_id
  old=c.execute("SELECT id FROM dist WHERE chat=? AND source=? AND active=1",(ch.id,src)).fetchone()
  if old: await m.reply_text("هذا التوزيع مفتوح بالفعل."); c.close(); return
  stamp=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
  cur=c.execute("INSERT INTO dist(chat,source,started) VALUES(?,?,?)",(ch.id,src,stamp)); did=cur.lastrowid;c.commit();c.close()
  await m.reply_text(f'{em("party","🎉")} بدأ التوزيع #{did}\\n{em("date","📆")} {stamp}',parse_mode=ParseMode.HTML);return
 if t=="قائمة" and isadm:
  rows=c.execute("SELECT d.id did,r.id rid,r.name,r.game FROM dist d LEFT JOIN reg r ON r.did=d.id WHERE d.chat=? AND d.active=1 ORDER BY d.id,r.id",(ch.id,)).fetchall();c.close()
  if not rows: await m.reply_text("لا يوجد توزيع نشط.");return
  out=[]; last=None
  for x in rows:
   if x["did"]!=last: out.append(f'التوزيع #{x["did"]}');last=x["did"]
   if x["rid"] is not None: out.append(f'{x["rid"]}. {escape(x["name"])} — <code>{escape(x["game"])}</code>')
  await m.reply_text("\\n".join(out),parse_mode=ParseMode.HTML);return
 if t=="وقف توزيع" and isadm:
  rows=c.execute("SELECT id FROM dist WHERE chat=? AND active=1",(ch.id,)).fetchall()
  if not rows: c.close();await m.reply_text("ما فيه توزيع نشط.");return
  ids=[x["id"] for x in rows]
  for did in ids:c.execute("UPDATE dist SET active=0 WHERE id=?",(did,))
  counts=[c.execute("SELECT COUNT(*) FROM reg WHERE did=?",(x,)).fetchone()[0] for x in ids];c.commit();c.close()
  await m.reply_text(em("stopped","💞")+" تم إيقاف التوزيع\\n"+"\\n".join(f"#{x}: {n} عضو" for x,n in zip(ids,counts)),parse_mode=ParseMode.HTML);return
 if isadm and re.fullmatch(r"\\d+",t) and not m.reply_to_message:
  n=int(t);x=c.execute("SELECT r.msg FROM reg r JOIN dist d ON d.id=r.did WHERE d.chat=? AND d.active=1 AND r.id=? ORDER BY d.id DESC LIMIT 1",(ch.id,n)).fetchone();c.close()
  if not x: await m.reply_text("ما لقيت الرقم في التوزيعات النشطة.");return
  await ctx.bot.send_message(ch.id,em("turn","✅")+" وصل دورك اقبل وجون",reply_to_message_id=x["msg"],parse_mode=ParseMode.HTML);return
 if m.reply_to_message and not isadm:
  x=c.execute("SELECT id FROM dist WHERE chat=? AND source=? AND active=1 ORDER BY id DESC LIMIT 1",(ch.id,m.reply_to_message.message_id)).fetchone()
  if not x:c.close();return
  prev=c.execute("SELECT id FROM reg WHERE did=? AND uid=?",(x["id"],user.id)).fetchone()
  if prev:c.close();await m.reply_text(f'{em("duplicate","❎")} أنت مسجل من قبل، رقمك {prev["id"]}',parse_mode=ParseMode.HTML);return
  cur=c.execute("INSERT INTO reg(did,uid,name,game,msg) VALUES(?,?,?,?,?)",(x["id"],user.id,user.full_name,t,m.message_id));n=cur.lastrowid;c.commit();c.close()
  await m.reply_text(f'{em("registered","👍")} تم تسجيلك، رقمك {n}',parse_mode=ParseMode.HTML);return
 c.close()
def main():
 if not TOKEN: raise RuntimeError("أضف BOT_TOKEN في متغيرات البيئة في الاستضافة.")
 connect().close()
 app=Application.builder().token(TOKEN).build()
 app.add_handler(CommandHandler("start",start))
 app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle))
 log.info("Polling started")
 app.run_polling(drop_pending_updates=True)
if __name__=="__main__":main()
