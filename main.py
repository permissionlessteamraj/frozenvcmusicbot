# ultimate_bot_v2.py

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes, ConversationHandler, InlineQueryHandler
)
import os
import time
import json
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import random
import re
from collections import defaultdict
import threading
from functools import wraps
from typing import Dict, Any

# --- External Libraries/API (Simulated) ---
def analyze_image_with_ai(image_url: str) -> bool:
    logger.info(f"Simulating AI analysis for image: {image_url}")
    if "explicit" in image_url.lower() or "inappropriate" in image_url.lower():
        return True
    return False

def analyze_video_with_ai(video_url: str) -> bool:
    logger.info(f"Simulating AI analysis for video: {video_url}")
    if "violence" in video_url.lower() or "hate" in video_url.lower():
        return True
    return False

def translate_text_with_ai(text: str, target_lang: str = 'en') -> str:
    # A real implementation would use a service like Google Translate API
    return text

def get_sentiment_with_ai(text: str) -> str:
    # A real implementation would use a service like OpenAI's API
    toxic_words = ["gaali", "abuse", "madarchod", "fuck", "bhosdi", "motherfucker", "randi", "harami"]
    if any(word in text.lower() for word in toxic_words):
        return "negative"
    return "neutral"

# --- Configuration & Setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_IDS = [123456789, 987654321]
ADMIN_CHANNEL_ID = -1001234567890
DB_NAME = "bot_data.db"
FAQ_FILE = "faqs.json"
BOT_STATE_FILE = "bot_state.json"
MUTE_WARN_LIMIT = 3
BAN_WARN_LIMIT = 5

if not BOT_TOKEN:
    print("Error: BOT_TOKEN environment variable not set.")
    exit()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- State Management (Persists across restarts) ---
def load_bot_state():
    try:
        with open(BOT_STATE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ticket_counter": 1, "active_tickets": {}}

def save_bot_state(state):
    with open(BOT_STATE_FILE, 'w') as f:
        json.dump(state, f)

# --- Decorators for Permissions and Checks ---
def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ADMIN_USER_IDS:
            await update.message.reply_text("⛔️ You are not authorized to use this command.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def requires_reply(func):
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
            first_name TEXT,
            join_date TEXT,
            reputation REAL DEFAULT 0.0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS warns (
            warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            timestamp TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            user_id INTEGER,
            timestamp TEXT,
            details TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            messages_sent INTEGER DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            reputation REAL DEFAULT 0.0
        )
    ''')
    conn.commit()
    conn.close()

def db_add_warn(user_id, moderator_id, reason):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO warns (user_id, moderator_id, reason, timestamp) VALUES (?, ?, ?, ?)",
              (user_id, moderator_id, reason, datetime.now().isoformat()))
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
    
def get_user_reputation(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT reputation FROM user_stats WHERE user_id=?", (user_id,))
    reputation = c.fetchone()
    conn.close()
    return reputation[0] if reputation else 0.0

def update_reputation(user_id, change):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE user_stats SET reputation = reputation + ? WHERE user_id = ?", (change, user_id))
    conn.commit()
    conn.close()

# --- Global State ---
bot_state = load_bot_state()
faqs = {}
user_activity = defaultdict(list)
flood_cache = defaultdict(lambda: {'count': 0, 'time': time.time()})

# --- 1. Ultra-Advanced Welcome & Verification ---
async def send_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        
        try:
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=member.id,
                permissions=ChatMember(can_send_messages=False, can_send_media_messages=False).permissions
            )
        except Exception as e:
            logger.error(f"Failed to restrict new member {member.id}: {e}")

        welcome_text = (
            f"🌟 Namaste, {member.mention_html()}! 🌟\n\n"
            "Humari community me aapka swagat hai! 🥳\n\n"
            "Aapki suraksha ke liye, kripya neeche diye gaye button par click karke **verify** kare ki aap bot nahi hain. 🙏"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Verify You're Human", callback_data=f"verify_{member.id}")]
        ])
        
        await update.message.reply_photo(
            photo="https://i.imgur.com/G350oKk.png",
            caption=welcome_text,
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, join_date) VALUES (?, ?, ?, ?)",
                  (member.id, member.username, member.first_name, datetime.now().isoformat()))
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
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Great! {query.from_user.mention_html()}, you're all set. Please read the rules here: /rules"
        )
    except Exception as e:
        logger.error(f"Failed to un-mute user after verification: {e}")
        await query.answer("Verification successful, but I couldn't grant you full access. Please contact an admin.")

# --- 2. Smart Anti-Spam & Anti-Abuse (Autonomous) ---
async def anti_spam_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMIN_USER_IDS:
        return
    
    # Dynamic Anti-flood based on reputation
    reputation = get_user_reputation(user_id)
    rate_limit_seconds = 0.5 + (1 - reputation / 100) * 1.5 # 0.5 sec for trusted, 2 sec for new
    
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
        return

    # AI-Powered Spam Detection
    message_text = update.message.text or ""
    sentiment = get_sentiment_with_ai(message_text)
    
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
        # Auto-punishment based on reputation
        if reputation < 20: # Example threshold
            await context.bot.restrict_chat_member(
                chat_id=update.effective_chat.id,
                user_id=user_id,
                permissions=ChatMember(can_send_messages=False).permissions,
                until_date=datetime.now() + timedelta(hours=1)
            )
        return
    
    # AI-Powered Media Moderation
    media_url = ""
    if update.message.photo:
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        media_url = file.file_path
    elif update.message.video:
        file = await context.bot.get_file(update.message.video.file_id)
        media_url = file.file_path
    
    if media_url and (analyze_image_with_ai(media_url) or analyze_video_with_ai(media_url)):
        await update.message.delete()
        await context.bot.send_message(
            ADMIN_CHANNEL_ID,
            f"**Inappropriate Media Detected** from {update.effective_user.mention_html()}."
        )
        await update.effective_chat.send_message(f"🚫 {update.effective_user.mention_html()} Your media was flagged as inappropriate and has been deleted.")
        return

# --- 3. Pro-Level Moderation ---
@restricted
@requires_reply
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_to_ban = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_ban.id)
        await update.message.reply_text(f"🔨 User {user_to_ban.mention_html()} has been banned.", parse_mode='HTML')
        update_reputation(user_to_ban.id, -20) # Reinforcement Learning: Punish reputation on manual ban
    except Exception as e:
        await update.message.reply_text(f"❌ Could not ban user. Error: {e}")

@restricted
@requires_reply
async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_to_warn = update.message.reply_to_message.from_user
    reason = " ".join(context.args) if context.args else "No reason given."
    
    db_add_warn(user_to_warn.id, update.effective_user.id, reason)
    warn_count = db_get_warn_count(user_to_warn.id)
    update_reputation(user_to_warn.id, -5) # Reinforcement Learning: Decrease reputation on warn
    
    await update.message.reply_text(f"⚠️ User {user_to_warn.mention_html()} has been warned. Total warns: {warn_count}")

    if warn_count >= BAN_WARN_LIMIT:
        await context.bot.ban_chat_member(update.effective_chat.id, user_to_warn.id)
        await update.message.reply_text(f"🚨 User {user_to_warn.mention_html()} reached the warning limit ({BAN_WARN_LIMIT}) and has been **auto-banned**.")
    elif warn_count >= MUTE_WARN_LIMIT:
        await context.bot.restrict_chat_member(
            chat_id=update.effective_chat.id,
            user_id=user_to_warn.id,
            permissions=ChatMember(can_send_messages=False).permissions,
            until_date=datetime.now() + timedelta(days=1)
        )
        await update.message.reply_text(f"🔇 User {user_to_warn.mention_html()} reached the mute limit ({MUTE_WARN_LIMIT}) and has been **auto-muted** for 24 hours.")

# --- 4. Ticket & Support System ---
TICKET_MESSAGE, TICKET_PRIORITY = range(2)
async def start_ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ticket_id = bot_state["ticket_counter"]
    bot_state["ticket_counter"] += 1
    context.user_data['active_ticket_id'] = ticket_id
    
    await update.message.reply_text(f"📝 Your ticket #{ticket_id} has been created. Please describe your issue.")
    return TICKET_MESSAGE

async def receive_ticket_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    last_ticket_id = context.user_data.get('active_ticket_id')
    if not last_ticket_id: return ConversationHandler.END

    priority_buttons = [
        [InlineKeyboardButton("Normal", callback_data=f"priority_{last_ticket_id}_normal")],
        [InlineKeyboardButton("Urgent", callback_data=f"priority_{last_ticket_id}_urgent")]
    ]
    await update.message.reply_text(
        "Is this an urgent issue?",
        reply_markup=InlineKeyboardMarkup(priority_buttons)
    )
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
    except (FileNotFound-Error, json.JSONDecodeError):
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

# --- 6. Broadcast & Mentions ---
@restricted
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_text = " ".join(context.args)
    if not message_text:
        return await update.message.reply_text("❌ Please provide a message to broadcast.")
        
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    user_ids = [row[0] for row in c.fetchall()]
    conn.close()

    success_count = 0
    fail_count = 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(user_id, message_text)
            success_count += 1
            time.sleep(0.1)
        except Exception:
            fail_count += 1
    
    await update.message.reply_text(f"✅ Broadcast complete.\nSent to {success_count} users, failed for {fail_count} users.")

# --- 7. Analytics & Smart Logs ---
async def log_messages_and_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    db_update_user_stats(user_id, "messages_sent")
    update_reputation(user_id, 0.1) # Reinforcement Learning: Reward reputation for active participation

# --- 8. AI/Smart Features ---
async def auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text.lower()
    
    for keyword, answer in faqs.items():
        if keyword in message_text:
            return await update.message.reply_text(f"💡 I think this might help you:\n\n{answer}")

# --- 9. Scheduler & Automation ---
def run_scheduled_tasks(application):
    def scheduler_thread():
        while True:
            now = datetime.now()
            if now.hour == 10 and now.minute == 0:
                # Send daily stats to admin channel
                pass
            
            if now.weekday() == 0 and now.hour == 11 and now.minute == 0:
                # Auto-remove inactive users
                pass
            
            time.sleep(60)
    
    threading.Thread(target=scheduler_thread, daemon=True).start()

# --- 10. Leaderboard and Referral ---
@restricted
async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT user_id, messages_sent, reputation FROM user_stats ORDER BY reputation DESC LIMIT 10", conn)
    conn.close()
    
    leaderboard_text = "🏆 **Top 10 Members by Reputation** 🏆\n\n"
    for index, row in df.iterrows():
        user_id = row['user_id']
        messages = row['messages_sent']
        reputation = row['reputation']
        
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT first_name FROM users WHERE user_id=?", (user_id,))
        user_name = c.fetchone()[0]
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
    
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO, anti_spam_check))
    
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CallbackQueryHandler(lambda u, c: inline_moderation_callback(u, c), pattern='^mod_'))
    
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
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("leaderboard", leaderboard_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, auto_reply))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Sticker.ALL, log_messages_and_activity))
    
    run_scheduled_tasks(application)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
