# main.py

import logging


import os
import time
import json
import sqlite3
import re
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
import threading

# --- Configuration & Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_IDS = [123456789, 987654321] # Replace with your actual admin user IDs
ADMIN_CHANNEL_ID = -1001234567890 # Replace with your admin channel ID
DB_NAME = "bot_data.db"
FAQ_FILE = "faqs.json"

if not BOT_TOKEN:
    print("Error: BOT_TOKEN environment variable not set.")
    exit()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions and Decorators ---
def get_user_reputation(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT reputation FROM user_stats WHERE user_id=?", (user_id,))
    reputation = c.fetchone()
    conn.close()
    return reputation[0] if reputation else 50.0

def update_reputation(user_id, change):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE user_stats SET reputation = MAX(0, MIN(100, reputation + ?)) WHERE user_id = ?", (change, user_id))
    conn.commit()
    conn.close()

def get_sentiment(text: str) -> str:
    toxic_words = ["gaali", "abuse", "madarchod", "fuck", "bhosdi", "motherfucker", "randi", "harami"]
    if any(word in text.lower() for word in toxic_words):
        return "negative"
    return "neutral"

def restricted(func):
    """A decorator that restricts a command to admins only."""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await update.message.reply_text("⛔️ You are not authorized to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def requires_reply(func):
    """A decorator that ensures a command is a reply to another message."""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        if not update.message.reply_to_message:
            await update.message.reply_text("❌ Please reply to a user's message to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- Database & Persistence ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS warns (
            warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            messages_sent INTEGER DEFAULT 0,
            reputation REAL DEFAULT 50.0
        )
    ''')
    conn.commit()
    conn.close()

def db_add_warn(user_id, moderator_id, reason):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO warns (user_id, moderator_id, reason) VALUES (?, ?, ?)",
              (user_id, moderator_id, reason))
    conn.commit()
    conn.close()

def db_get_warn_count(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM warns WHERE user_id=?", (user_id,))
    count = c.fetchone()[0]
    conn.close()
    return count

def db_update_user_stats(user_id, field, amount=1):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute(f"INSERT OR IGNORE INTO user_stats (user_id) VALUES (?)", (user_id,))
    c.execute(f"UPDATE user_stats SET {field} = {field} + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

# --- Global State & Caches ---
flood_cache = defaultdict(lambda: {'count': 0, 'time': time.time()})
faqs = {}
ticket_counter = 1
active_tickets = {}

# --- 1. Proactive Welcome & Verification ---
async def send_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=member.id,
                permissions=ChatMember(can_send_messages=False).permissions
            )
        except Exception as e:
            logger.error(f"Failed to restrict new member {member.id}: {e}")
        welcome_text = (
            f"🌟 Namaste, {member.mention_html()}! 🌟\n\n"
            "Humari community me aapka swagat hai! 🥳\n\n"
            "Aapki suraksha ke liye, kripya neeche diye gaye button par click karke **verify** kare ki aap bot nahi hain. 🙏"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Verify You're Human", callback_data=f"verify_{member.id}")]])
        await update.message.reply_photo(
            photo="https://i.imgur.com/G350oKk.png",
            caption=welcome_text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
                  (member.id, member.username, member.first_name))
        conn.commit()
        conn.close()

async def handle_verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id_to_verify = int(query.data.split('_')[1])
    if query.from_user.id != user_id_to_verify:
        await query.answer("❌ This button is not for you. 🚫")
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id=query.message.chat_id,
            user_id=user_id_to_verify,
            permissions=ChatMember(can_send_messages=True, can_send_media_messages=True).permissions
        )
        await query.edit_message_caption(
            caption=f"✅ {query.from_user.mention_html()} has been verified. Welcome! 🎉",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Failed to un-mute user after verification: {e}")
        await query.answer("Verification successful, but I couldn't grant you full access. Please contact an admin.")

# --- 2. AI-Powered Anti-Spam & Moderation ---
async def anti_spam_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_USER_IDS:
        return
    
    reputation = get_user_reputation(user_id)
    rate_limit_seconds = 0.5 + (100 - reputation) / 100 * 1.5
    
    current_time = time.time()
    if current_time - flood_cache[user_id]['time'] < rate_limit_seconds:
        flood_cache[user_id]['count'] += 1
    else:
        flood_cache[user_id]['count'] = 1
        flood_cache[user_id]['time'] = current_time
    
    if flood_cache[user_id]['count'] > 5:
        await update.message.delete()
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=user_id,
            permissions=ChatMember(can_send_messages=False).permissions,
            until_date=datetime.now() + timedelta(minutes=15)
        )
        await update.effective_chat.send_message(f"🚨 {update.effective_user.mention_html()} has been muted for 15 minutes due to message flooding.")
        update_reputation(user_id, -10)
        return

    message_text = update.message.text or ""
    sentiment = get_sentiment(message_text)
    
    if sentiment == "negative":
        await update.message.delete()
        moderation_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Warn", callback_data=f"mod_warn_{user_id}"),
             InlineKeyboardButton("🔇 Mute", callback_data=f"mod_mute_{user_id}"),
             InlineKeyboardButton("🔨 Ban", callback_data=f"mod_ban_{user_id}")]
        ])
        await context.bot.send_message(
            ADMIN_CHANNEL_ID,
            f"**Toxic Message Detected** from {update.effective_user.mention_html()}.\n\nMessage: `{message_text}`",
            reply_markup=moderation_keyboard,
            parse_mode='Markdown'
        )
        await update.effective_chat.send_message(f"🚫 {update.effective_user.mention_html()} Your message was flagged as toxic and has been deleted.")
        update_reputation(user_id, -15)
        return

# --- 3. Enhanced Moderation Commands ---
@restricted
@requires_reply
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_to_ban = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_ban.id)
        await update.message.reply_text(f"🔨 User {user_to_ban.mention_html()} has been banned.", parse_mode='HTML')
        update_reputation(user_to_ban.id, -25)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not ban user. Error: {e}")

@restricted
@requires_reply
async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_to_warn = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "No reason given."
    
    db_add_warn(user_to_warn.id, update.effective_user.id, reason)
    warn_count = db_get_warn_count(user_to_warn.id)
    update_reputation(user_to_warn.id, -5)
    
    await update.message.reply_text(f"⚠️ User {user_to_warn.mention_html()} has been warned. Total warns: {warn_count}")

    if warn_count >= 5:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_warn.id)
        await update.message.reply_text(f"🚨 User {user_to_warn.mention_html()} reached the warning limit (5) and has been **auto-banned**.")
    elif warn_count >= 3:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=user_to_warn.id,
            permissions=ChatMember(can_send_messages=False).permissions,
            until_date=datetime.now() + timedelta(days=1)
        )
        await update.message.reply_text(f"🔇 User {user_to_warn.mention_html()} reached the mute limit (3) and has been **auto-muted** for 24 hours.")

# --- 4. Ticket & Support System ---
TICKET_MESSAGE, TICKET_PRIORITY = range(2)
async def start_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    global ticket_counter
    ticket_id = ticket_counter
    ticket_counter += 1
    
    active_tickets[ticket_id] = {'user_id': update.effective_user.id, 'status': 'open', 'messages': [], 'priority': 'normal'}
    await update.message.reply_text(f"📝 Your ticket #{ticket_id} has been created. Please describe your issue.")
    return TICKET_MESSAGE

async def receive_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    last_ticket_id = ticket_counter - 1
    active_tickets[last_ticket_id]['messages'].append(update.message.text)
    
    priority_buttons = [
        [InlineKeyboardButton("Normal", callback_data=f"priority_{last_ticket_id}_normal")],
        [InlineKeyboardButton("Urgent", callback_data=f"priority_{last_ticket_id}_urgent")]
    ]
    await update.message.reply_text("Is this an urgent issue?", reply_markup=InlineKeyboardMarkup(priority_buttons))
    return TICKET_PRIORITY

async def set_ticket_priority(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    parts = query.data.split('_')
    ticket_id = int(parts[1])
    priority = parts[2]
    
    await query.edit_message_text(f"Ticket #{ticket_id} priority set to **{priority.upper()}**.")
    await context.bot.send_message(
        ADMIN_CHANNEL_ID,
        f"🚨 **New Ticket!** 🚨\n\n**ID:** {ticket_id}\n**User:** {query.from_user.mention_html()}\n**Priority:** {priority.upper()}"
    )
    return ConversationHandler.END

# --- 5. FAQ & Knowledge Base ---
def load_faqs():
    try:
        with open(FAQ_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_faqs(data):
    with open(FAQ_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

async def manage_faq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        return await update.message.reply_text("Usage: /faq [add|edit|delete|list|search] [keyword] [answer]")
    
    command = args[0].lower()
    if command == "add":
        keyword = args[1].lower()
        answer = " ".join(args[2:])
        faqs[keyword] = answer
        save_faqs(faqs)
        await update.message.reply_text(f"✅ FAQ for '{keyword}' has been added.")
    elif command == "search":
        query = " ".join(args[1:]).lower()
        if query in faqs:
            await update.message.reply_text(f"**FAQ Answer:** {faqs[query]}", parse_mode='Markdown')
        else:
            await update.message.reply_text("🤷‍♂️ Sorry, I couldn't find an answer for that.")

# --- 6. Analytics & Logs ---
async def log_messages_and_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_update_user_stats(user_id, "messages_sent")
    update_reputation(user_id, 0.1)

# --- 7. Scheduler & Automation ---
def run_scheduled_tasks(application):
    def scheduler_thread():
        while True:
            time.sleep(60)
    threading.Thread(target=scheduler_thread, daemon=True).start()

# --- 8. Leaderboard and User Info ---
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id, messages_sent, reputation FROM user_stats ORDER BY reputation DESC LIMIT 10")
    results = c.fetchall()
    conn.close()
    
    leaderboard_text = "🏆 **Top 10 Members by Reputation** 🏆\n\n"
    if not results:
        leaderboard_text += "No members found yet."
    else:
        for index, row in enumerate(results):
            user_id = row[0]
            messages = row[1]
            reputation = row[2]
            
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("SELECT first_name FROM users WHERE user_id=?", (user_id,))
            user_name_tuple = c.fetchone()
            user_name = user_name_tuple[0] if user_name_tuple else "Unknown User"
            conn.close()
            
            leaderboard_text += f"{index + 1}. **{user_name}** - Rep: {reputation:.2f} | Msgs: {messages}\n"
    
    await update.message.reply_text(leaderboard_text, parse_mode='Markdown')

# --- Main Bot Logic ---
def main() -> None:
    init_db()
    faqs.update(load_faqs())
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, send_welcome_message))
    application.add_handler(CallbackQueryHandler(handle_verification_callback, pattern='^verify_'))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_spam_check))
    
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("warn", warn_command))
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start_ticket", start_ticket_command)],
        states={
            TICKET_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticket_message)],
            TICKET_PRIORITY: [CallbackQueryHandler(set_ticket_priority, pattern='^priority_')]
        },
        fallbacks=[CommandHandler("cancel_ticket", lambda u, c: ConversationHandler.END)]
    )
    application.add_handler(conv_handler)
    
    application.add_handler(CommandHandler("faq", manage_faq_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, log_messages_and_activity))
    
    run_scheduled_tasks(application)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
