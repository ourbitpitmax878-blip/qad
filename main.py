import asyncio
import logging
import os
import re
import secrets
import contextlib
from threading import Thread
from flask import Flask
from telegram import (Update, ReplyKeyboardMarkup, KeyboardButton,
                      InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          ConversationHandler, filters, ContextTypes, CallbackQueryHandler,
                          ApplicationHandlerStop, TypeHandler)
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
import html
import traceback
import json

# =======================================================
#  بخش ۱: تنظیمات اولیه و پیکربندی
# =======================================================

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Environment Variables & Constants ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8235083147:AAGUWM3QPg6i7B3nw0lGbi8ERZlyI0wU4pQ")
OWNER_ID = int(os.environ.get("OWNER_ID", 8241063918))

TEHRAN_TIMEZONE = ZoneInfo("Asia/Tehran")

# --- In-Memory Database (دیتابیس درون حافظه‌ای) ---
# هشدار: تمام این اطلاعات با هر بار ری‌استارت ربات پاک می‌شوند.
GLOBAL_USERS = {}
GLOBAL_SETTINGS = {}
GLOBAL_TRANSACTIONS = {}
GLOBAL_BETS = {}
GLOBAL_CHANNELS = {}

# (شمارنده‌های سراسری برای ID ها)
TX_ID_COUNTER = 1
BET_ID_COUNTER = 1


def init_memory_db():
    """Initializes the in-memory settings with default values."""
    logging.info("Initializing in-memory database...")
    default_settings = {
        'credit_price': '1000',
        'initial_balance': '10',
        'referral_reward': '5',
        'bet_tax_rate': '2',
        'card_number': 'هنوز تنظیم نشده',
        'card_holder': 'هنوز تنظیم نشده',
        'bet_photo_file_id': 'None',
        'forced_channel_lock': 'false'
    }
    
    for key, value in default_settings.items():
        if key not in GLOBAL_SETTINGS:
            GLOBAL_SETTINGS[key] = value
    logging.info("Default settings loaded into memory.")

# --- Global Variables & State Management ---
BOT_EVENT_LOOP = None

# --- Conversation Handler States ---
# (تغییر: حذف AWAIT_REMOVE_CHANNEL و مرتب‌سازی مجدد)
(ADMIN_MENU, AWAIT_ADMIN_REPLY, AWAIT_DEPOSIT_AMOUNT, AWAIT_DEPOSIT_RECEIPT,
 AWAIT_SUPPORT_MESSAGE, AWAIT_ADMIN_SUPPORT_REPLY,
 AWAIT_NEW_CHANNEL, AWAIT_BET_PHOTO,
 AWAIT_ADMIN_SET_BALANCE, AWAIT_ADMIN_TAX, AWAIT_ADMIN_CREDIT_PRICE,
 AWAIT_ADMIN_REFERRAL_PRICE, AWAIT_ADMIN_SET_BALANCE_ID,
 AWAIT_MANAGE_USER_ID, AWAIT_MANAGE_USER_ROLE,
 AWAIT_ADMIN_SET_CARD_NUMBER, AWAIT_ADMIN_SET_CARD_HOLDER
) = range(17)


# =======================================================
#  بخش ۲: وب اپلیکیشن Flask (فقط برای Health Check)
# =======================================================
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    """Health check endpoint for Render."""
    return "Bet Bot is running.", 200

# =======================================================
#  بخش ۳: توابع کمکی ربات (جایگزین دیتابیس)
# =======================================================

async def get_setting_async(name):
    """Gets a setting from the in-memory GLOBAL_SETTINGS."""
    return GLOBAL_SETTINGS.get(name)

async def set_setting_async(name, value):
    """Sets a setting in the in-memory GLOBAL_SETTINGS."""
    GLOBAL_SETTINGS[name] = str(value)

async def get_user_async(user_id):
    """
    Retrieves a user document from in-memory GLOBAL_USERS,
    creating it if it doesn't exist.
    """
    if user_id in GLOBAL_USERS:
        user_doc = GLOBAL_USERS[user_id]
        # (اطمینان از موجودی ادمین در هر بار فراخوانی)
        if user_doc.get('is_admin') and user_doc.get('balance', 0) < 1000000000:
            user_doc['balance'] = 1000000000
            GLOBAL_USERS[user_id] = user_doc
        return user_doc

    # (کاربر وجود ندارد، یکی جدید بساز)
    try:
        initial_balance_val_str = GLOBAL_SETTINGS.get('initial_balance', '10')
        initial_balance_val = int(initial_balance_val_str)
    except (ValueError, TypeError):
        initial_balance_val = 10

    is_owner = (user_id == OWNER_ID)
    balance_on_create = 1000000000 if is_owner else initial_balance_val

    new_user_doc = {
        'user_id': user_id,
        'balance': balance_on_create,
        'is_admin': is_owner,
        'is_owner': is_owner,
        'referred_by': None,
        'is_moderator': False
    }
    GLOBAL_USERS[user_id] = new_user_doc
    return new_user_doc

def get_user_display_name(user):
    """Gets a safe display name for a user (username or first/last name)."""
    if user.username:
        return f"@{user.username}"
    
    name = user.first_name
    if user.last_name:
        name += f" {user.last_name}"
    # (از نام HTML-safe برای جلوگیری از خطاهای قالب‌بندی استفاده می‌کنیم)
    return html.escape(name)

# --- Keyboards ---
def get_main_keyboard(user_doc):
    if user_doc.get('is_admin'):
        # (منوی ساده برای ادمین)
        keyboard = [
            [KeyboardButton("💰 موجودی"), KeyboardButton("👑 پنل ادمین")],
        ]
    else:
        # (منوی عادی برای کاربران)
        keyboard = [
            [KeyboardButton("💰 موجودی"), KeyboardButton("💳 افزایش اعتبار")],
            [KeyboardButton("🎁 کسب اعتبار رایگان"), KeyboardButton("💬 پشتیبانی")],
        ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# (تغییر: دکمه تنظیم شماره کارت به دو دکمه مجزا تقسیم شد)
admin_keyboard = ReplyKeyboardMarkup([
    [KeyboardButton("📊 آمار کلی"), KeyboardButton("💳 تنظیم شماره کارت")],
    [KeyboardButton("👤 تنظیم صاحب کارت"), KeyboardButton("مدیریت کاربر")],
    [KeyboardButton("💰 تنظیم موجودی کاربر"), KeyboardButton("📈 تنظیم قیمت اعتبار")],
    [KeyboardButton("🎁 تنظیم پاداش دعوت"), KeyboardButton("📉 تنظیم مالیات (۰-۱۰۰)")],
    [KeyboardButton("➕ افزودن کانال عضویت"), KeyboardButton("➖ حذف کانال عضویت")],
    [KeyboardButton("👁‍🗨 لیست کانال‌های عضویت"), KeyboardButton("✅/❌ قفل عضویت اجbاری")],
    [KeyboardButton("🖼 تنظیم عکس شرط"), KeyboardButton("🗑 حذف عکس شرط")],
    [KeyboardButton("⬅️ بازگشت به منوی اصلی")]
], resize_keyboard=True)

bet_group_keyboard = ReplyKeyboardMarkup([
    [KeyboardButton("موجودی 💰")],
    [KeyboardButton("شرط 100"), KeyboardButton("شرط 500")],
    [KeyboardButton("شرط 1000"), KeyboardButton("شرط 5000")]
], resize_keyboard=True)

# =======================================================
#  بخش ۴: سیستم عضویت اجباری (نسخه Async)
# =======================================================

# (تغییر: این تابع بازطراحی شده تا فقط کانال‌های مورد نیاز را بسازد)
async def get_specific_join_keyboard(channels: list) -> InlineKeyboardMarkup | None:
    """Creates the keyboard for the forced join message for specific channels."""
    if not channels:
        return None

    keyboard_buttons = []
    for channel in channels:
        # (اطمینان از داشتن 'channel_link' و 'channel_username')
        link = channel.get('channel_link', 'https://telegram.org')
        username = channel.get('channel_username', 'کانال')
        keyboard_buttons.append([
            InlineKeyboardButton(f"عضویت در {username}", url=link)
        ])

    keyboard_buttons.append([InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_join_membership")])
    return InlineKeyboardMarkup(keyboard_buttons)

# (تغییر: کل این تابع با منطق جدید و قوی‌تر جایگزین شده است)
async def membership_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    A high-priority handler that checks channel membership before allowing any other handler to run.
    """
    user = update.effective_user
    query = update.callback_query
    
    if not user:
        return  # (کاربری برای بررسی وجود ندارد)

    if user.id == OWNER_ID:
        return  # (مالک معاف است)

    forced_lock_str = await get_setting_async("forced_channel_lock")
    forced_lock = forced_lock_str == 'true'
    
    if not forced_lock:
        return  # (ویژگی غیرفعال است)

    channels = list(GLOBAL_CHANNELS.values())
    
    if not channels:
        return  # (ویژگی فعال است، اما کانالی تنظیم نشده)

    not_joined_channels = []

    for channel in channels:
        channel_username = channel['channel_username']
        try:
            member = await context.bot.get_chat_member(channel_username, user.id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined_channels.append(channel)
        except Exception as e:
            # (خطا را ثبت کن، اما فرض کن عضو نیست)
            logging.error(f"Failed to check membership for user {user.id} in channel {channel_username}: {e}")
            not_joined_channels.append(channel)
            # (تلاش برای اطلاع‌رسانی به مالک، اما کاربر را به خاطر این خطا مسدود نکن)
            with contextlib.suppress(Exception):
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=f"⚠️ **خطا در بررسی عضویت اجbاری** ⚠️\n"
                         f"ربات نتوانست عضویت کاربر `{user.id}` را در کانال `{channel_username}` بررسی کند.\n"
                         f"**دلیل احتمالی:** ربات در کانال ادمین نیست یا یوزرنیم اشتباه است.\n"
                         f"**خطای اصلی:** `{e}`",
                    parse_mode=ParseMode.MARKDOWN
                )

    # --- مدیریت دکمه "بررسی عضویت" ---
    if query and query.data == "check_join_membership":
        await query.answer()

        if not not_joined_channels:
            # (کاربر بررسی را پاس کرد)
            await query.message.delete()
            user_doc = await get_user_async(user.id)
            await context.bot.send_message(
                chat_id=user.id,
                text="✅ عضویت شما تایید شد. خوش آمدید!\nحالا می‌توانید از امکانات ربات استفاده کنید.",
                reply_markup=get_main_keyboard(user_doc)
            )
        else:
            # (کاربر بررسی را رد شد)
            await query.answer("❌ شما هنوز عضو تمام کانال‌ها/گروه‌ها نشده‌اید.", show_alert=True)
            # (ارسال مجدد پیام فقط با کانال‌های باقیمانده)
            keyboard = await get_specific_join_keyboard(not_joined_channels)
            await query.message.edit_text(
                "⚪️ لطفا در کانال/گروه‌های *باقیمانده* زیر عضو شوید و سپس دکمه بررسی را بزنید:",
                reply_markup=keyboard
            )
        
        # (پردازش این آپدیت را متوقف کن، چه موفق چه ناموفق)
        raise ApplicationHandlerStop

    # --- مسدود کردن کاربر اگر عضو نباشد ---
    if not_joined_channels:
        # (کاربر عضو نیست و دکمه "بررسی" را نزده است)
        keyboard = await get_specific_join_keyboard(not_joined_channels)
        
        # (ساخت متن پیام)
        channels_list_text = "\n".join([f"- {ch['channel_username']}" for ch in not_joined_channels])
        text = (
            "⚪️ برای استفاده از ربات، لطفا ابتدا در تمام کانال/گروه‌های زیر عضو شوید و سپس دکمه «بررسی عضویت» را بزنید:\n"
            f"{channels_list_text}"
        )

        if query:
            # (اگر کلیک روی دکمه‌ای (غیر از بررسی) بوده، کلیک را پاسخ بده و پیام جدید بفرست)
            await query.answer("⛔️ ابتدا باید عضو کانال‌ها شوید.", show_alert=True)
            await context.bot.send_message(
                chat_id=user.id,
                text=text,
                reply_markup=keyboard
            )
        elif update.effective_message:
            # (اگر پیام متنی بوده، فقط پاسخ بده)
            await update.effective_message.reply_text(
                text=text,
                reply_markup=keyboard
            )
        
        # (مسدود کردن تمام هندلرهای دیگر)
        raise ApplicationHandlerStop

    # (اگر به اینجا برسد، یعنی کاربر عضو است، پس اجازه بده آپدیت ادامه یابد)
    return

# =======================================================
#  بخش ۵: مدیریت دستورات کاربران (نسخه Async)
# =======================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_doc = await get_user_async(user.id)

    if user_doc.get('is_admin'):
        # (خواندن آمار از حافظه)
        total_users = len(GLOBAL_USERS)
        pending_tx = sum(1 for tx in GLOBAL_TRANSACTIONS.values() if tx['status'] == 'pending')

        admin_welcome_text = (
            f"👑 سلام ادمین عزیز، به پنل مدیریت خوش آمدید!\n\n"
            f"📊 **آمار ربات (درون حافظه‌ای):**\n"
            f"  -  👥 **تعداد کل کاربران:** {total_users:,}\n"
            f"  -  🧾 **تراکنش‌های در انتظار:** {pending_tx:,}"
        )
        await update.message.reply_text(admin_welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_keyboard(user_doc))
    else:
        # Referral logic
        if context.args and len(context.args) > 0:
            try:
                referrer_id = int(context.args[0])
                if referrer_id != user.id and not user_doc.get('referred_by'):
                    # (آپدیت حافظه)
                    GLOBAL_USERS[user.id]['referred_by'] = referrer_id
                    
                    reward_str = await get_setting_async('referral_reward')
                    try:
                        reward = int(reward_str or 5)
                    except (ValueError, TypeError):
                        reward = 5

                    # (اطمینان از وجود معرف و آپدیت موجودی او)
                    referrer_doc = await get_user_async(referrer_id)
                    referrer_doc['balance'] += reward
                    
                    # (تغییر: اضافه کردن نام کاربر جدید به پیام)
                    new_user_display_name = get_user_display_name(user)
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"🎁 تبریک! کاربر {new_user_display_name} از طریق لینک شما وارد ربات شد و شما {reward} اعتبار پاداش گرفتید."
                    )
            except (ValueError, TypeError):
                pass

        await update.message.reply_text(
            "👋 به ربات شرط‌بندی خوش آمدید.",
            reply_markup=get_main_keyboard(user_doc)
        )

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_doc = await get_user_async(update.effective_user.id)
    price_str = await get_setting_async('credit_price')
    try:
        price = int(price_str or 1000)
    except (ValueError, TypeError):
        price = 1000
        
    balance_toman = user_doc.get('balance', 0) * price
    await update.message.reply_text(
        f"💰 موجودی شما: **{user_doc.get('balance', 0):,}** اعتبار\n"
        f" معادل: `{balance_toman:,}` تومان",
        parse_mode=ParseMode.MARKDOWN
    )

async def support_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لطفا پیام خود را برای ارسال به پشتیبانی بنویسید:", reply_markup=ReplyKeyboardRemove())
    return AWAIT_SUPPORT_MESSAGE

async def process_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_doc = await get_user_async(user.id)
    
    # (پیدا کردن ادمین‌ها در حافظه)
    admins = [u for u in GLOBAL_USERS.values() if u.get('is_admin')]
    
    text = f"📨 پیام پشتیبانی جدید از کاربر: {user.mention_html()}\n\n`{update.message.text}`"
    reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ پاسخ به کاربر", callback_data=f"reply_support_{user.id}_{update.message.message_id}")
    ]])

    for admin in admins:
        try:
            await context.bot.send_message(chat_id=admin['user_id'], text=text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.warning(f"Could not send support message to admin {admin['user_id']}: {e}")

    await update.message.reply_text("✅ پیام شما با موفقیت برای تیم پشتیبانی ارسال شد.", reply_markup=get_main_keyboard(user_doc))
    return ConversationHandler.END

async def get_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={update.effective_user.id}"
    
    reward_str = await get_setting_async('referral_reward')
    try:
        reward = int(reward_str or 5)
    except (ValueError, TypeError):
        reward = 5

    await update.message.reply_text(
        f"🎁 لینک دعوت شما:\n\n`{link}`\n\n"
        f"با هر دعوت موفق، {reward} اعتبار دریافت کنید!",
        parse_mode=ParseMode.MARKDOWN
    )

# --- Deposit Conversation ---
async def deposit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لطفا تعداد اعتباری که قصد خرید دارید را وارد کنید:", reply_markup=ReplyKeyboardRemove())
    return AWAIT_DEPOSIT_AMOUNT

async def process_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
        if amount <= 0: raise ValueError
        
        price_str = await get_setting_async('credit_price')
        try:
            price = int(price_str or 1000)
        except (ValueError, TypeError):
            price = 1000
            
        total_cost = amount * price
        context.user_data['deposit_amount'] = amount

        card_number = await get_setting_async('card_number') or "شماره کارتی تنظیم نشده"
        card_holder = await get_setting_async('card_holder') or "نامی تنظیم نشده"

        await update.message.reply_text(
            f"مبلغ قابل پرداخت برای `{amount}` اعتبار: `{total_cost:,}` تومان\n\n"
            f"لطفا مبلغ را به کارت زیر واریز کرده و سپس عکس رسید را ارسال کنید:\n"
            f"شماره کارت: `{card_number}`\n"
            f"صاحب حساب: `{card_holder}`",
            parse_mode=ParseMode.MARKDOWN
        )
        return AWAIT_DEPOSIT_RECEIPT
    except (ValueError, TypeError):
        await update.message.reply_text("❌ لطفا یک عدد صحیح و مثبت وارد کنید.")
        return AWAIT_DEPOSIT_AMOUNT

async def process_deposit_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TX_ID_COUNTER
    if not update.message.photo:
        await update.message.reply_text("❌ لطفا عکس رسید پرداخت را ارسال کنید.")
        return AWAIT_DEPOSIT_RECEIPT

    user = update.effective_user
    user_doc = await get_user_async(user.id)
    amount = context.user_data['deposit_amount']
    receipt_file_id = update.message.photo[-1].file_id

    # (ساخت تراکنش در حافظه)
    tx_id = TX_ID_COUNTER
    GLOBAL_TRANSACTIONS[tx_id] = {
        'tx_id': tx_id,
        'user_id': user.id,
        'amount': amount,
        'receipt_file_id': receipt_file_id,
        'status': 'pending',
        'timestamp': datetime.now(timezone.utc)
    }
    TX_ID_COUNTER += 1 # (افزایش شمارنده سراسری)
    
    caption = (f"🧾 درخواست افزایش اعتبار جدید (ID: {tx_id})\n"
               f"کاربر: {user.mention_html()}\n"
               f"تعداد اعتبار: `{amount}`")

    reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تایید", callback_data=f"tx_approve_{tx_id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"tx_reject_{tx_id}")
    ]])

    admins = [u for u in GLOBAL_USERS.values() if u.get('is_admin')]
    
    for admin in admins:
        try:
            await context.bot.send_photo(chat_id=admin['user_id'], photo=receipt_file_id, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            logging.warning(f"Could not send receipt to admin {admin['user_id']}: {e}")

    await update.message.reply_text("✅ رسید شما برای ادمین ارسال شد. پس از تایید، اعتبار شما شارژ خواهد شد.", reply_markup=get_main_keyboard(user_doc))
    context.user_data.clear()
    return ConversationHandler.END

# =======================================================
#  بخش ۶: مدیریت دستورات ادمین (نسخه Async)
# =======================================================
async def admin_panel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_doc = await get_user_async(update.effective_user.id)
    if not user_doc.get('is_admin'):
        await update.message.reply_text("⛔️ شما دسترسی به این بخش را ندارید.")
        return ConversationHandler.END

    await update.message.reply_text("👑 به پنل ادمین خوش آمدید:", reply_markup=admin_keyboard)
    return ADMIN_MENU

# (تغییر: تابع جدید برای نمایش لیست کانال‌ها جهت حذف)
async def show_channels_for_removal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows an inline keyboard of channels for removal."""
    channels = list(GLOBAL_CHANNELS.values())
    
    if not channels:
        await update.message.reply_text("هیچ کانالی برای حذف کردن وجود ندارد.", reply_markup=admin_keyboard)
        return ADMIN_MENU

    keyboard = []
    for channel in channels:
        # (استفاده از channel_username به عنوان شناسه یکتا)
        keyboard.append([
            InlineKeyboardButton(
                channel['channel_username'], 
                callback_data=f"admin_remove_{channel['channel_username']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("لغو", callback_data="admin_remove_cancel")])
    
    await update.message.reply_text(
        "لطفا کانالی که می‌خواهید حذف شود را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    # (در استیت ادمین باقی می‌مانیم، عملیات توسط کالبک هندل می‌شود)
    return ADMIN_MENU

async def process_admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    context.user_data['admin_choice'] = choice

    # (تغییر: حذف "➖ حذف کانال عضویت" از لیست راهنماها)
    prompts = {
        "💳 تنظیم شماره کارت": "لطفا شماره کارت جدید را وارد کنید:",
        "👤 تنظیم صاحب کارت": "لطفا نام صاحب حساب جدید را وارد کنید:",
        "💰 تنظیم موجودی کاربر": "ابتدا آیدی عددی کاربر را وارد کنید:",
        "📈 تنظیم قیمت اعتبار": "قیمت جدید هر اعتبار به تومان را وارد کنید:",
        "🎁 تنظیم پاداش دعوت": "پاداش هر دعوت موفق به اعتبار را وارد کنید:",
        "📉 تنظیم مالیات (۰-۱۰۰)": "درصد مالیات (بین ۰ تا ۱۰۰) را وارد کنید:",
        "➕ افزودن کانال عضویت": "یوزرنیم کانال/گروه با @ (مثل @channel) یا لینک کامل (مثل https://t.me/channel) را ارسال کنید:",
        # "➖ حذف کانال عضویت" removed from here
        "🖼 تنظیم عکس شرط": "لطفا عکس مورد نظر برای شرط را ارسال کنید."
    }

    # (تغییر: به‌روزرسانی منطق برای هدایت به استیت‌های جدید)
    if choice in prompts:
        await update.message.reply_text(prompts[choice], reply_markup=ReplyKeyboardRemove())
        if choice == "➕ افزودن کانال عضویت":
            return AWAIT_NEW_CHANNEL
        # (تغییر: حذف بلوک 'elif' برای '➖ حذف کانال عضویت')
        elif choice == "🖼 تنظیم عکس شرط":
            return AWAIT_BET_PHOTO
        elif choice == "💰 تنظیم موجودی کاربر":
            return AWAIT_ADMIN_SET_BALANCE_ID
        elif choice == "📉 تنظیم مالیات (۰-۱۰۰)":
            return AWAIT_ADMIN_TAX
        elif choice == "📈 تنظیم قیمت اعتبار":
            return AWAIT_ADMIN_CREDIT_PRICE
        elif choice == "🎁 تنظیم پاداش دعوت":
            return AWAIT_ADMIN_REFERRAL_PRICE
        elif choice == "💳 تنظیم شماره کارت":
            return AWAIT_ADMIN_SET_CARD_NUMBER
        elif choice == "👤 تنظیم صاحب کارت":
            return AWAIT_ADMIN_SET_CARD_HOLDER
        else:
            return AWAIT_ADMIN_REPLY
    
    # (تغییر: '➖ حذف کانال عضویت' اکنون به این بلوک می‌افتد)
    elif choice == "➖ حذف کانال عضویت":
        return await show_channels_for_removal(update, context) # (فراخوانی تابع جدید)
            
    elif choice == "مدیریت کاربر":
        await update.message.reply_text("آیدی عددی کاربر مورد نظر را وارد کنید:", reply_markup=ReplyKeyboardRemove())
        return AWAIT_MANAGE_USER_ID

    elif choice == "✅/❌ قفل عضویت اجbاری":
        current_lock_str = await get_setting_async('forced_channel_lock')
        new_lock = not (current_lock_str == 'true')
        await set_setting_async('forced_channel_lock', 'true' if new_lock else 'false')
        status = "فعال" if new_lock else "غیرفعال"
        await update.message.reply_text(f"✅ قفل عضویت در کانال اجbاری {status} شد.")
        return ADMIN_MENU

    elif choice == "👁‍🗨 لیست کانال‌های عضویت":
        channels = list(GLOBAL_CHANNELS.values())
        if not channels:
            await update.message.reply_text("هیچ کانالی برای عضویت اجbاری تنظیم نشده است.", reply_markup=admin_keyboard)
            return ADMIN_MENU

        message = "لیست کانال‌های عضویت اجbاری:\n\n"
        for i, channel in enumerate(channels, 1):
            message += f"{i}. {channel['channel_username']} ({channel['channel_link']})\n"

        await update.message.reply_text(message, reply_markup=admin_keyboard)
        return ADMIN_MENU
    
    elif choice == "📊 آمار کلی":
        total_users = len(GLOBAL_USERS)
        pending_tx = sum(1 for tx in GLOBAL_TRANSACTIONS.values() if tx['status'] == 'pending')
        total_balance = sum(u.get('balance', 0) for u in GLOBAL_USERS.values())

        admin_welcome_text = (
            f"📊 **آمار ربات (درون حافظه‌ای):**\n"
            f"  -  👥 **تعداد کل کاربران:** {total_users:,}\n"
            f"  -  💰 **مجموع اعتبار کاربران:** {total_balance:,}\n"
            f"  -  🧾 **تراکنش‌های در انتظار:** {pending_tx:,}"
        )
        await update.message.reply_text(admin_welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard)
        return ADMIN_MENU

    elif choice == "🗑 حذف عکس شرط":
        await set_setting_async('bet_photo_file_id', 'None')
        await update.message.reply_text("✅ عکس شرط با موفقیت حذف شد.", reply_markup=admin_keyboard)
        return ADMIN_MENU

    elif choice == "⬅️ بازگشت به منوی اصلی":
        user_doc = await get_user_async(update.effective_user.id)
        await update.message.reply_text("بازگشت به منوی اصلی...", reply_markup=get_main_keyboard(user_doc))
        return ConversationHandler.END

async def process_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles simple text replies for admin settings."""
    last_choice = context.user_data.get('admin_choice')
    reply = update.message.text.strip()
    
    try:
        # (منطق "تنظیم شماره کارت" به توابع اختصاصی منتقل شده است)
        # (این تابع در حال حاضر توسط هیچ انتخابی استفاده نمی‌شود)
        logging.warning(f"process_admin_reply was called unexpectedly with choice: {last_choice}")
        await update.message.reply_text("✅ عملیات انجام شد.", reply_markup=admin_keyboard)

    except (ValueError, IndexError, TypeError) as e:
        logging.error(f"Admin reply error for choice '{last_choice}': {e}")
        await update.message.reply_text(f"❌ ورودی نامعتبر است. {e}", reply_markup=admin_keyboard)
    except Exception as e:
        logging.error(f"Unexpected admin reply error: {e}")
        await update.message.reply_text(f"❌ خطایی ناشناخته رخ داد.", reply_markup=admin_keyboard)

    context.user_data.pop('admin_choice', None)
    return ADMIN_MENU

# (تغییر: افزودن توابع جدید برای مدیریت تنظیمات کارت)
async def process_admin_set_card_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new card number."""
    try:
        card_number = update.message.text.strip()
        if not card_number:
            raise ValueError("شماره کارت نمی‌تواند خالی باشد")
        
        await set_setting_async('card_number', card_number)
        await update.message.reply_text(f"✅ شماره کارت با موفقیت به `{card_number}` تنظیم شد.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard)
    except ValueError as e:
        logging.error(f"Error setting card number: {e}")
        await update.message.reply_text(f"❌ ورودی نامعتبر است. لطفا شماره کارت را دوباره وارد کنید.\n({e})")
        return AWAIT_ADMIN_SET_CARD_NUMBER
    
    context.user_data.clear()
    return ADMIN_MENU

async def process_admin_set_card_holder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new card holder name."""
    try:
        card_holder = update.message.text.strip()
        if not card_holder:
            raise ValueError("نام صاحب کارت نمی‌تواند خالی باشد")
        
        await set_setting_async('card_holder', card_holder)
        await update.message.reply_text(f"✅ نام صاحب حساب با موفقیت به `{card_holder}` تنظیم شد.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard)
    except ValueError as e:
        logging.error(f"Error setting card holder: {e}")
        await update.message.reply_text(f"❌ ورودی نامعتبر است. لطفا نام صاحب حساب را دوباره وارد کنید.\n({e})")
        return AWAIT_ADMIN_SET_CARD_HOLDER
    
    context.user_data.clear()
    return ADMIN_MENU


# --- New Admin Conversation Handlers ---

async def process_manage_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gets the user ID for managing roles."""
    try:
        target_user_id = int(update.message.text.strip())
        context.user_data['target_user_id_manage'] = target_user_id
        
        user_doc = await get_user_async(target_user_id) # (کاربر را می‌سازد یا می‌گیرد)
        
        role_keyboard = ReplyKeyboardMarkup([
            [KeyboardButton("ادمین"), KeyboardButton("مادریتور")],
            [KeyboardButton("کاربر عادی"), KeyboardButton("لغو")]
        ], resize_keyboard=True)
        
        await update.message.reply_text(f"لطفا نقش جدید را برای کاربر `{target_user_id}` انتخاب کنید:",
                                        parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=role_keyboard)
        return AWAIT_MANAGE_USER_ROLE
    except ValueError:
        await update.message.reply_text("❌ آیدی عددی نامعتبر است. لطفا دوباره تلاش کنید.", reply_markup=admin_keyboard)
        context.user_data.clear()
        return ADMIN_MENU

async def process_manage_user_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new role for the user."""
    try:
        role = update.message.text.strip()
        target_user_id = context.user_data.pop('target_user_id_manage', None)
        admin_doc = await get_user_async(update.effective_user.id)

        if role == "لغو":
            await update.message.reply_text("عملیات لغو شد.", reply_markup=admin_keyboard)
            context.user_data.clear()
            return ADMIN_MENU

        if not target_user_id:
            await update.message.reply_text("❌ خطای داخلی. لطفا دوباره از پنل ادمین شروع کنید.", reply_markup=admin_keyboard)
            return ADMIN_MENU
            
        if not admin_doc.get('is_owner'):
            await update.message.reply_text("⛔️ فقط مالک اصلی ربات می‌تواند نقش‌ها را تغییر دهد.", reply_markup=admin_keyboard)
            return ADMIN_MENU
            
        if target_user_id == OWNER_ID:
            await update.message.reply_text("❌ شما نمی‌توانید نقش مالک اصلی را تغییر دهید.", reply_markup=admin_keyboard)
            return ADMIN_MENU

        target_user_doc = await get_user_async(target_user_id) # (اطمینان از وجود کاربر)
        initial_balance_str = await get_setting_async('initial_balance')
        initial_balance = int(initial_balance_str or 10)

        message = ""

        if role == "ادمین":
            target_user_doc['is_admin'] = True
            target_user_doc['is_moderator'] = False
            target_user_doc['balance'] = 1000000000
            message = f"✅ کاربر `{target_user_id}` به **ادمین** ارتقا یافت و ۱ میلیارد اعتبار دریافت کرد."
        
        elif role == "مادریتور":
            target_user_doc['is_admin'] = False
            target_user_doc['is_moderator'] = True
            message = f"✅ کاربر `{target_user_id}` به **مادریتور** ارتقا یافت. (دسترسی به پنل ادمین ندارد)"
            
        elif role == "کاربر عادی":
            target_user_doc['is_admin'] = False
            target_user_doc['is_moderator'] = False
            target_user_doc['balance'] = initial_balance
            message = f"✅ کاربر `{target_user_id}` به **کاربر عادی** تنزل یافت و موجودی‌اش بازنشانی شد."
            
        else:
            await update.message.reply_text("❌ نقش انتخاب شده نامعتبر است.", reply_markup=admin_keyboard)
            return ADMIN_MENU

        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard)
        
    except Exception as e:
        logging.error(f"Error managing user role: {e}")
        await update.message.reply_text("❌ خطایی در تغییر نقش رخ داد.", reply_markup=admin_keyboard)
    
    context.user_data.clear()
    return ADMIN_MENU


async def process_admin_set_balance_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gets the user ID for setting balance."""
    try:
        target_user_id = int(update.message.text.strip())
        context.user_data['target_user_id_balance'] = target_user_id
        
        await get_user_async(target_user_id) # (ایجاد کاربر در صورت عدم وجود)
        
        await update.message.reply_text(f"حالا مقدار موجودی جدید را برای کاربر `{target_user_id}` وارد کنید:", parse_mode=ParseMode.MARKDOWN)
        return AWAIT_ADMIN_SET_BALANCE
    except ValueError:
        await update.message.reply_text("❌ آیدی عددی نامعتبر است. لطفا دوباره تلاش کنید.", reply_markup=admin_keyboard)
        return ADMIN_MENU

async def process_admin_set_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new balance for the user."""
    try:
        new_balance = int(update.message.text.strip())
        target_user_id = context.user_data.pop('target_user_id_balance', None)

        if target_user_id is None:
            await update.message.reply_text("❌ خطای داخلی. لطفا دوباره از پنل ادمین شروع کنید.", reply_markup=admin_keyboard)
            return ADMIN_MENU

        target_user_doc = await get_user_async(target_user_id) # (گرفتن یا ساختن کاربر)
        target_user_doc['balance'] = new_balance # (آپدیت موجودی در حافظه)
        
        await update.message.reply_text(f"✅ موجودی کاربر `{target_user_id}` با موفقیت به {new_balance:,} اعتبار تغییر یافت.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard)
    except ValueError:
        await update.message.reply_text("❌ مبلغ نامعتبر است. لطفا یک عدد وارد کنید.")
        return AWAIT_ADMIN_SET_BALANCE
    except Exception as e:
        logging.error(f"Error setting balance: {e}")
        await update.message.reply_text("❌ خطایی در تنظیم موجودی رخ داد.", reply_markup=admin_keyboard)
    
    return ADMIN_MENU

async def process_admin_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new tax rate."""
    try:
        tax_rate = int(update.message.text.strip())
        if not 0 <= tax_rate <= 100:
            raise ValueError("Tax rate must be between 0 and 100")
        
        await set_setting_async('bet_tax_rate', str(tax_rate))
        await update.message.reply_text(f"✅ مالیات شرط‌بندی با موفقیت روی {tax_rate}% تنظیم شد.", reply_markup=admin_keyboard)
    except ValueError:
        await update.message.reply_text("❌ درصد نامعتبر است. لطفا یک عدد بین ۰ تا ۱۰۰ وارد کنید.")
        return AWAIT_ADMIN_TAX
    return ADMIN_MENU

async def process_admin_credit_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new credit price."""
    try:
        price = int(update.message.text.strip())
        if price <= 0:
            raise ValueError("Price must be positive")
        
        await set_setting_async('credit_price', str(price))
        await update.message.reply_text(f"✅ قیمت هر اعتبار با موفقیت روی {price:,} تومان تنظیم شد.", reply_markup=admin_keyboard)
    except ValueError:
        await update.message.reply_text("❌ قیمت نامعتبر است. لطفا یک عدد مثبت وارد کنید.")
        return AWAIT_ADMIN_CREDIT_PRICE
    return ADMIN_MENU

async def process_admin_referral_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the new referral reward."""
    try:
        reward = int(update.message.text.strip())
        if reward < 0:
            raise ValueError("Reward cannot be negative")
        
        await set_setting_async('referral_reward', str(reward))
        await update.message.reply_text(f"✅ پاداش دعوت (رفرال) با موفقیت روی {reward:,} اعتبار تنظیم شد.", reply_markup=admin_keyboard)
    except ValueError:
        await update.message.reply_text("❌ پاداش نامعتبر است. لطفا یک عدد وارد کنید.")
        return AWAIT_ADMIN_REFERRAL_PRICE
    return ADMIN_MENU

# --- End of New Admin Handlers ---

async def process_new_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.message.text.strip()
    channel_username = None
    channel_link = None

    if reply.startswith('@'):
        channel_username = reply
        channel_link = f"https://t.me/{reply[1:]}"
    elif "t.me/" in reply:
        try:
            username = reply.split("t.me/")[-1].split('/')[0]
            if not username: raise ValueError("Invalid link")
            channel_username = f"@{username}"
            channel_link = f"https://t.me/{username}"
        except Exception as e:
            logging.warning(f"Could not parse channel link: {reply} - Error: {e}")
            await update.message.reply_text("❌ لینک نامعتبر است. لطفا یوزرنیم با @ یا لینک کامل t.me را ارسال کنید.", reply_markup=admin_keyboard)
            return AWAIT_NEW_CHANNEL
    else:
        await update.message.reply_text("❌ ورودی نامعتبر است. لطفا یوزرنیم با @ (مثل @channel) یا لینک کامل (مثل https://t.me/channel) ارسال کنید.", reply_markup=admin_keyboard)
        return AWAIT_NEW_CHANNEL

    try:
        chat = await context.bot.get_chat(channel_username)
        member = await chat.get_member(context.bot.id)
        if member.status not in ['administrator', 'creator']:
             await update.message.reply_text(f"⚠️ **هشدار:** ربات در کانال {channel_username} ادمین نیست. عضویت اجbاری کار نخواهد کرد مگر اینکه ربات را ادمین کنید.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ **هشدار:** ربات نتوانست کانال {channel_username} را بررسی کند. خطا: {e}\n"
                                        f"لطفا مطمئن شوید یوزرنیم/لینک صحیح است و ربات عضو کانال می‌باشد (و برای بررسی عضویت، باید ادمین هم باشد).",
                                        parse_mode=ParseMode.MARKDOWN)

    # (افزودن به دیکشنری حافظه)
    GLOBAL_CHANNELS[channel_username] = {
        'channel_username': channel_username,
        'channel_link': channel_link
    }

    await update.message.reply_text(f"✅ کانال {channel_username} با موفقیت اضافه/آپدیت شد.", reply_markup=admin_keyboard)
    context.user_data.clear()
    return ADMIN_MENU

# (تغییر: این تابع دیگر استفاده نمی‌شود و حذف شده است)
# async def process_remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE): ...

async def process_bet_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("❌ لطفا یک عکس ارسال کنید.", reply_markup=admin_keyboard)
        return AWAIT_BET_PHOTO

    file_id = update.message.photo[-1].file_id
    await set_setting_async('bet_photo_file_id', file_id)
    await update.message.reply_text("✅ عکس شرط با موفقیت تنظیم شد.", reply_markup=admin_keyboard)
    context.user_data.clear()
    return ADMIN_MENU

async def admin_support_reply_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')
    target_user_id = int(data[2])
    context.user_data['reply_to_user'] = target_user_id
    await query.message.reply_text(f"لطفا پاسخ خود را برای کاربر با آیدی {target_user_id} بنویسید:", reply_markup=ReplyKeyboardRemove())
    return AWAIT_ADMIN_SUPPORT_REPLY

async def process_admin_support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    target_user_id = context.user_data.get('reply_to_user')
    if not target_user_id: return ConversationHandler.END

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"✉️ پاسخ پشتیبانی:\n\n{update.message.text}"
        )
        await update.message.reply_text("✅ پاسخ شما برای کاربر ارسال شد.", reply_markup=admin_keyboard)
    except Exception as e:
        await update.message.reply_text(f"❌ ارسال پیام به کاربر ناموفق بود: {e}", reply_markup=admin_keyboard)

    context.user_data.clear()
    return ADMIN_MENU

# =======================================================
#  بخش ۷: مدیریت Callback Query و پیام‌های عمومی (نسخه Async)
# =======================================================
async def cancel_bet_job(context: ContextTypes.DEFAULT_TYPE):
    """Job to cancel a bet if it's not joined within the time limit."""
    job = context.job
    bet_id = job.data['bet_id']
    chat_id = job.data['chat_id']
    message_id = job.data['message_id']
    
    # (بررسی شرط در حافظه)
    if bet_id in GLOBAL_BETS and GLOBAL_BETS[bet_id]['status'] == 'pending':
        deleted_bet = GLOBAL_BETS.pop(bet_id) # (حذف شرط از حافظه)
        
        logging.info(f"Bet {bet_id} expired and was cancelled.")
        try:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=f"⏰ شرط‌بندی روی مبلغ {deleted_bet['amount']} اعتبار منقضی شد.",
                reply_markup=None
            )
        except Exception:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⏰ شرط‌بندی روی مبلغ {deleted_bet['amount']} اعتبار منقضی شد.",
                    reply_markup=None
                )
            except Exception as e:
                logging.warning(f"Could not edit expired bet message {message_id}: {e}")

# (تغییر: تابع جدید برای مدیریت کالبک حذف کانال)
async def handle_channel_removal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin's choice of channel to remove."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "admin_remove_cancel":
        await query.edit_message_text("عملیات لغو شد.")
        return

    # (استخراج یوزرنیم از "admin_remove_@channelname")
    channel_username = data.replace("admin_remove_", "")
    
    if channel_username in GLOBAL_CHANNELS:
        del GLOBAL_CHANNELS[channel_username]
        logging.info(f"Admin {query.from_user.id} removed channel {channel_username}")
        await query.edit_message_text(f"✅ کانال {channel_username} با موفقیت حذف شد.")
    else:
        logging.warning(f"Admin {query.from_user.id} tried to remove non-existent channel {channel_username}")
        await query.edit_message_text(f"❌ کانال {channel_username} یافت نشد (شاید قبلا حذف شده باشد).")


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles general callback queries."""
    query = update.callback_query
    
    # (تغییر: بررسی کالبک‌های حذف کانال قبل از هر چیز)
    if query.data.startswith("admin_remove_"):
        await handle_channel_removal_callback(update, context) # (این تابع خودش query.answer() را صدا می‌زند)
        return
    
    # (تغییر: query.answer() به اینجا منتقل شد تا برای همه کالبک‌های دیگر اجرا شود)
    await query.answer()
    user_id = query.from_user.id
    data = query.data.split('_')
    action = data[0]

    # (کالبک "check_join_membership" اکنون در membership_check_handler مدیریت می‌شود و به اینجا نمی‌رسد)

    if action == "tx":
        tx_id = int(data[2])
        try:
            tx = GLOBAL_TRANSACTIONS.get(tx_id)
            
            if not tx:
                await query.edit_message_caption(caption=query.message.caption_html + "\n\n(تراکنش یافت نشد)", parse_mode=ParseMode.HTML)
                return

            if tx.get('status') != 'pending':
                await query.answer("این تراکنش قبلا پردازش شده است.", show_alert=True)
                return

            if data[1] == "approve":
                user_doc = await get_user_async(tx['user_id'])
                user_doc['balance'] += tx['amount']
                tx['status'] = 'approved'
                
                await query.edit_message_caption(caption=query.message.caption_html + "\n\n<b>✅ تایید شد.</b>", parse_mode=ParseMode.HTML)
                await context.bot.send_message(tx['user_id'], f"✅ پرداخت شما برای {tx['amount']} اعتبار تایید و موجودی شما شارژ شد.")
            elif data[1] == "reject":
                tx['status'] = 'rejected'
                
                await query.edit_message_caption(caption=query.message.caption_html + "\n\n<b>❌ رد شد.</b>", parse_mode=ParseMode.HTML)
                await context.bot.send_message(tx['user_id'], f"❌ پرداخت شما برای {tx['amount']} اعتبار رد شد.")
        except Exception as e:
            logging.error(f"Error processing transaction callback: {e}")
            await query.answer("خطا در پردازش تراکنش.", show_alert=True)

    elif action == "bet": # e.g., bet_join_{bet_id}
        bet_id = int(data[2])
        bet = GLOBAL_BETS.get(bet_id)
        user = query.from_user

        if not bet:
            try:
                await query.edit_message_text("این شرط دیگر فعال نیست.")
            except: pass
            return

        # Cancel action
        if data[1] == "cancel":
            if user.id != bet['proposer_id']:
                await query.answer("شما شروع کننده این شرط نیستید.", show_alert=True)
                return
            if bet.get('status') != 'pending':
                await query.answer("این شرط دیگر برای لغو در دسترس نیست.", show_alert=True)
                return

            # Remove job
            if context.job_queue:
                current_jobs = context.job_queue.get_jobs_by_name(f"bet_timeout_{bet_id}")
                for job in current_jobs:
                    job.schedule_removal()
            
            # (حذف از حافظه)
            GLOBAL_BETS.pop(bet_id, None)
            
            await query.answer("✅ شرط با موفقیت لغو شد.", show_alert=False)
            try:
                await query.edit_message_caption(caption=f"❌ شرط توسط {bet['proposer_username']} لغو شد.", reply_markup=None)
            except Exception:
                try:
                    await query.edit_message_text(f"❌ شرط توسط {bet['proposer_username']} لغو شد.", reply_markup=None)
                except: pass
            return

        # Join action
        if data[1] == "join":
            if user.id == bet['proposer_id']:
                await query.answer("شما نمی‌توانید به شرط خودتان بپیوندید.", show_alert=True)
                return
            
            # (چک کردن همزمان برای جلوگیری از پیوستن همزمان دو نفر)
            if bet.get('status') != 'pending':
                await query.answer("متاسفانه کس دیگری زودتر به این شرط پیوست.", show_alert=True)
                return
            
            # (آپدیت وضعیت شرط در حافظه)
            # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
            opponent_display_name = get_user_display_name(user)
            bet['status'] = 'active'
            bet['opponent_id'] = user.id
            bet['opponent_username'] = opponent_display_name
            
            joiner_doc = await get_user_async(user.id)
            if joiner_doc['balance'] < bet['amount']:
                # Rollback bet status
                bet['status'] = 'pending'
                bet['opponent_id'] = None
                bet['opponent_username'] = None
                await query.answer("موجودی شما برای پیوستن به این شرط کافی نیست.", show_alert=True)
                return

            # Remove timeout job
            if context.job_queue:
                current_jobs = context.job_queue.get_jobs_by_name(f"bet_timeout_{bet_id}")
                for job in current_jobs:
                    job.schedule_removal()
                    logging.info(f"Removed bet timeout job for successfully joined bet {bet_id}")

            await query.answer("✅ شما به شرط پیوستید! در حال انتخاب برنده...", show_alert=False)
            try:
                await query.edit_message_caption(caption="🎲 در حال انتخاب برنده...", reply_markup=None)
            except:
                try: await query.edit_message_text("🎲 در حال انتخاب برنده...", reply_markup=None)
                except: pass

            await asyncio.sleep(1)

            # 1. Deduct from both participants
            amount = bet['amount']
            proposer_doc = await get_user_async(bet['proposer_id'])
            proposer_doc['balance'] -= amount
            joiner_doc['balance'] -= amount

            # 2. Randomly select winner
            proposer_id = bet['proposer_id']
            opponent_id = user.id
            winner_id = secrets.choice([proposer_id, opponent_id])

            # 3. Calculate prize and tax
            total_pot = amount * 2
            tax_rate_str = await get_setting_async('bet_tax_rate')
            try:
                tax_rate = int(tax_rate_str or 0)
            except (ValueError, TypeError):
                tax_rate = 0
            
            tax = round(total_pot * (tax_rate / 100))
            prize = total_pot - tax

            # 4. Give prize to winner and tax to owner
            winner_doc = await get_user_async(winner_id)
            winner_doc['balance'] += prize
            
            if tax > 0 and bet['proposer_id'] != OWNER_ID and user.id != OWNER_ID:
                owner_doc = await get_user_async(OWNER_ID)
                owner_doc['balance'] += tax
                logging.info(f"Transferred {tax} credit tax from bet {bet_id} to owner {OWNER_ID}")

            # 5. Determine usernames
            # (تغییر: استفاده از نام‌های نمایشی ذخیره شده)
            if winner_id == proposer_id:
                winner_display_name = bet['proposer_username']
                loser_display_name = opponent_display_name
            else:
                winner_display_name = opponent_display_name
                loser_display_name = bet['proposer_username']

            # 6. Delete the bet
            GLOBAL_BETS.pop(bet_id, None)

            # 7. Construct result message
            result_text = (
                f"♦️ — نتیجه شرط — ♦️\n"
                f"| 🏆 | : برنده : {winner_display_name}\n"
                f"| ❌ | : بازنده : {loser_display_name}\n"
                f"| 🎁 | جایزه: {prize:,} اعتبار\n"
                f"| 📉 | مالیات: {tax:,} اعتبار (از کل مبلغ)\n"
                f"♦️ — @{context.bot.username} — ♦️"
            )

            try:
                await query.edit_message_caption(caption=result_text, reply_markup=None)
            except Exception:
                try: await query.edit_message_text(text=result_text, reply_markup=None)
                except Exception as e: logging.error(f"Failed to edit bet message {bet_id}: {e}")

async def group_balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'موجودی' in groups."""
    if not update.message: return

    sender = update.effective_user
    target_user = sender
    reply_to_message = update.message.reply_to_message

    if reply_to_message and reply_to_message.from_user:
        sender_doc = await get_user_async(sender.id)
        if sender_doc.get('is_admin') or sender_doc.get('is_moderator'):
            target_user = reply_to_message.from_user

    target_user_doc = await get_user_async(target_user.id)
    price_str = await get_setting_async('credit_price')
    try:
        price = int(price_str or 1000)
    except (ValueError, TypeError):
        price = 1000
    toman_value = target_user_doc['balance'] * price

    # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
    target_display_name = get_user_display_name(target_user)
    text = (
        f"👤 کاربر: {target_display_name}\n"
        f"💰 موجودی اعتبار: {target_user_doc['balance']:,}\n"
        f"💳 معادل تخمینی: {toman_value:,.0f} تومان"
    )
    await update.message.reply_text(text)

async def transfer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles credit transfers in groups (reply with 'انتقال 100')."""
    if not update.message or not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return

    sender = update.effective_user
    receiver = update.message.reply_to_message.from_user

    try:
        # (تغییر: استخراج مبلغ از متن پیام بر اساس رگکس جدید)
        match = re.search(r'(\d+)', update.message.text)
        if not match:
            return  # (این نباید اتفاق بیفتد اگر رگکس درست باشد)
        
        amount = int(match.group(1))
        
        if amount <= 0:
            await update.message.reply_text("مبلغ انتقال باید مثبت باشد.")
            return
    except (ValueError, TypeError):
        await update.message.reply_text("خطا در خواندن مبلغ.")
        return 

    try:
        sender_doc = await get_user_async(sender.id)

        if sender.id == receiver.id:
            await update.message.reply_text("انتقال به خود امکان‌پذیر نیست.")
            return

        if sender_doc['balance'] < amount:
            await update.message.reply_text("موجودی شما کافی نیست.")
            return

        receiver_doc = await get_user_async(receiver.id) # Ensure receiver exists

        sender_doc['balance'] -= amount
        receiver_doc['balance'] += amount

        # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
        sender_display_name = get_user_display_name(sender)
        receiver_display_name = get_user_display_name(receiver)

        text = (
            f"✅ انتقال موفق ✅\n\n"
            f"👤 از: {sender_display_name}\n"
            f"👥 به: {receiver_display_name}\n"
            f"💰 مبلغ: {amount:,} اعتبار"
        )
        await update.message.reply_text(text)
    except Exception as e:
        logging.error(f"Error during transfer: {e}")
        await update.message.reply_text("خطایی در هنگام انتقال رخ داد.")

async def start_bet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Starts a bet with inline buttons."""
    global BET_ID_COUNTER
    if not update.message: return
    
    proposer = update.effective_user

    match = re.search(r'(\d+)', update.message.text)
    if not match: return
    try:
        amount = int(match.group(1))
        if amount <= 0: return
    except (ValueError, TypeError):
        return

    proposer_doc = await get_user_async(proposer.id)
    if proposer_doc['balance'] < amount:
        await update.message.reply_text("موجودی شما برای این شرط کافی نیست.")
        return
        
    bet_id = BET_ID_COUNTER
    # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
    proposer_display_name = get_user_display_name(proposer)
    GLOBAL_BETS[bet_id] = {
        'bet_id': bet_id,
        'proposer_id': proposer.id,
        'proposer_username': proposer_display_name, # (ذخیره نام نمایشی)
        'amount': amount,
        'chat_id': update.effective_chat.id,
        'status': 'pending',
        'created_at': datetime.now(timezone.utc)
    }
    BET_ID_COUNTER += 1
        
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ پیوستن", callback_data=f"bet_join_{bet_id}"),
            InlineKeyboardButton("❌ لغو شرط", callback_data=f"bet_cancel_{bet_id}")
        ]
    ])

    # (تغییر: استفاده از نام نمایشی بدون @ اضافه)
    proposer_mention = proposer_display_name
    text = (
        f"♦️ — شرط جدید (ID: {bet_id}) — ♦️\n"
        f"| 💰 | مبلغ شرط : {amount:,} اعتبار\n"
        f"| 👤 | سازنده : {proposer_mention}\n"
        f"♦️ — @{context.bot.username} — ♦️"
    )

    sent_message = None
    photo_id = await get_setting_async('bet_photo_file_id')

    try:
        if photo_id and photo_id != 'None':
            sent_message = await update.message.reply_photo(photo=photo_id, caption=text, reply_markup=keyboard)
        else:
            sent_message = await update.message.reply_text(text, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Failed to send bet message: {e}")
        if photo_id and photo_id != 'None':
            try: sent_message = await update.message.reply_text(text, reply_markup=keyboard)
            except: return
        else: return
    
    if not sent_message: return

    if context.job_queue:
        context.job_queue.run_once(
            cancel_bet_job,
            120, # 120 seconds timeout
            data={
                'bet_id': bet_id,
                'chat_id': update.effective_chat.id,
                'message_id': sent_message.message_id
            },
            name=f"bet_timeout_{bet_id}"
        )
    else:
        logging.warning("JobQueue not available. Bet timeout will not be scheduled.")

async def deduct_balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin 'کسر' command."""
    if not update.message or not update.message.reply_to_message:
        return

    admin_user = update.effective_user
    admin_doc = await get_user_async(admin_user.id)
    if not (admin_doc.get('is_admin') or admin_doc.get('is_moderator')):
        return

    target_user = update.message.reply_to_message.from_user
    if target_user.id == admin_user.id:
        await update.message.reply_text("شما نمی‌توانید از خودتان اعتبار کسر کنید.")
        return
    if target_user.id == OWNER_ID:
        await update.message.reply_text("شما نمی‌توانید از مالک اصلی اعتبار کسر کنید.")
        return

    match = re.search(r'(\d+)', update.message.text)
    if not match:
        await update.message.reply_text("لطفا مقدار عددی برای کسر را مشخص کنید. مثال: کسر 500")
        return

    try:
        amount_to_deduct = int(match.group(1))
        if amount_to_deduct <= 0:
            await update.message.reply_text("مقدار کسر باید یک عدد مثبت باشد.")
            return
    except (ValueError, TypeError):
        await update.message.reply_text("مقدار وارد شده نامعتبر است.")
        return

    target_doc = await get_user_async(target_user.id)
    # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
    target_display_name = get_user_display_name(target_user)
    if target_doc.get('balance', 0) < amount_to_deduct:
        await update.message.reply_text(f"کاربر {target_display_name} موجودی کافی برای کسر {amount_to_deduct:,} اعتبار را ندارد.")
        return

    target_doc['balance'] -= amount_to_deduct
    
    # (تغییر: استفاده از تابع کمکی برای نام نمایشی)
    admin_display_name = get_user_display_name(admin_user)
    tehran_time = datetime.now(TEHRAN_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
    receipt_text = (
        f"❌ {amount_to_deduct:,} اعتبار از {target_display_name} کسر شد.\n"
        f"🧾 رسید کسر:\n"
        f"📤 ادمین/مادریتور: {admin_display_name}\n"
        f"📥 کاربر: {target_display_name}\n"
        f"💰 مقدار: {amount_to_deduct:,}\n"
        f"⏰ {tehran_time}"
    )
    await update.message.reply_text(receipt_text)

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_doc = await get_user_async(update.effective_user.id)
    await update.message.reply_text("عملیات لغو شد.", reply_markup=get_main_keyboard(user_doc))
    context.user_data.clear()
    return ConversationHandler.END

async def show_bet_keyboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the quick bet reply keyboard in groups."""
    await update.message.reply_text("منوی شرط:", reply_markup=bet_group_keyboard)

# =======================================================
#  بخش ۸: تابع اصلی و اجرای ربات
# =======================================================
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    logging.info(f"Starting minimal Flask health check server on 0.0.0.0:{port}")
    try:
        web_app.run(host='0.0.0.0', port=port)
    except Exception as e:
        logging.error(f"Failed to start Flask health check server: {e}")

async def post_init(application: Application):
    """Actions to run after the bot is initialized."""
    global BOT_EVENT_LOOP
    BOT_EVENT_LOOP = asyncio.get_running_loop()
    
    init_memory_db() # <--- راه‌اندازی حافظه به جای دیتابیس
    logging.info("In-memory settings verified.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log the error."""
    logging.error("Exception while handling an update:", exc_info=context.error)

    if "Conflict: terminated by other getUpdates request" in str(context.error):
        logging.warning("Conflict error detected. Ensure only one bot instance is running.")
        return

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    if isinstance(update, Update):
        update_str = json.dumps(update.to_dict(), indent=2, ensure_ascii=False)
    else:
        update_str = str(update)

    message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(update_str)}</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<pre>{html.escape(tb_string)}</pre>"
    )

    if len(message) > 4096:
        message = message[:4090] + "...</pre>"
        
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Failed to send error log to owner: {e}")

if __name__ == "__main__":
    if not BOT_TOKEN:
        logging.fatal("BOT_TOKEN environment variable is not set. Exiting.")
        exit(1)

    logging.info("Starting Flask app in a background thread...")
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # --- Conversation Handlers ---
    # (تغییر: حذف AWAIT_REMOVE_CHANNEL از استیت‌ها)
    admin_conv_states = {
        ADMIN_MENU: [
            MessageHandler(filters.Regex("^(💳 تنظیم شماره کارت|👤 تنظیم صاحب کارت|مدیریت کاربر)$"), process_admin_choice),
            MessageHandler(filters.Regex("^(➕ افزودن کانال عضویت|➖ حذف کانال عضویت|🖼 تنظیم عکس شرط)$"), process_admin_choice),
            MessageHandler(filters.Regex(r"^(💰 تنظیم موجودی کاربر|📈 تنظیم قیمت اعتبار|🎁 تنظیم پاداش دعوت|📉 تنظیم مالیات \(۰-۱۰۰\))$"), process_admin_choice),
            MessageHandler(filters.Regex("^(✅/❌ قفل عضویت اجbاری|👁‍🗨 لیست کانال‌های عضویت|📊 آمار کلی|🗑 حذف عکس شرط)$"), process_admin_choice),
            MessageHandler(filters.Regex("^⬅️ بازگشت به منوی اصلی$"), process_admin_choice),
        ],
        AWAIT_ADMIN_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_reply)],
        AWAIT_ADMIN_SET_CARD_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_set_card_number)],
        AWAIT_ADMIN_SET_CARD_HOLDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_set_card_holder)],
        AWAIT_NEW_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_new_channel)],
        # (AWAIT_REMOVE_CHANNEL) حذف شد
        AWAIT_BET_PHOTO: [MessageHandler(filters.PHOTO, process_bet_photo)],
        AWAIT_ADMIN_SET_BALANCE_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_set_balance_id)],
        AWAIT_ADMIN_SET_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_set_balance)],
        AWAIT_ADMIN_TAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_tax)],
        AWAIT_ADMIN_CREDIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_credit_price)],
        AWAIT_ADMIN_REFERRAL_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_referral_price)],
        AWAIT_MANAGE_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_manage_user_id)],
        AWAIT_MANAGE_USER_ROLE: [
            MessageHandler(filters.Regex("^(ادمین|مادریتور|کاربر عادی|لغو)$"), process_manage_user_role)
        ],
    }

    admin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👑 پنل ادمین$"), admin_panel_entry)],
        states=admin_conv_states,
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        conversation_timeout=600
    )

    deposit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💳 افزایش اعتبار$"), deposit_entry)],
        states={
            AWAIT_DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_deposit_amount)],
            AWAIT_DEPOSIT_RECEIPT: [MessageHandler(filters.PHOTO, process_deposit_receipt)]
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        conversation_timeout=300
    )
    support_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💬 پشتیبانی$"), support_entry)],
        states={ AWAIT_SUPPORT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_support_message)] },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        conversation_timeout=300
    )

    admin_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_support_reply_entry, pattern="^reply_support_")],
        states={
            AWAIT_ADMIN_SUPPORT_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_admin_support_reply)]
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False,
        conversation_timeout=300
    )

    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=10,
        write_timeout=10,
        connect_timeout=10,
        pool_timeout=10
    )
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init) # <--- استفاده از post_init برای راه‌اندازی حافظه
        .build()
    )

    # --- Add handlers ---
    # (تغییر: هندلر عضویت اجباری با اولویت -1 اضافه شد)
    application.add_handler(TypeHandler(Update, membership_check_handler), group=-1)
    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.Regex("^💰 موجودی$"), show_balance))
    application.add_handler(MessageHandler(filters.Regex("^🎁 کسب اعتبار رایگان$"), get_referral_link))
    application.add_handler(admin_conv)
    application.add_handler(deposit_conv)
    application.add_handler(support_conv)
    application.add_handler(admin_reply_conv)

    # Group Handlers
    application.add_handler(MessageHandler(filters.Regex(r'^(شرط|بت)$') & filters.ChatType.GROUPS, show_bet_keyboard_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^(شرطبندی|شرط) \d+$') & filters.ChatType.GROUPS, start_bet_handler))
    
    # (تغییر: رگکس برای انتقال وجه به صورت "انتقال 100" در ریپلای)
    application.add_handler(MessageHandler(filters.Regex(r'^(انتقال|transfer)\s+(\d+)$') & filters.REPLY & filters.ChatType.GROUPS, transfer_handler))
    
    application.add_handler(MessageHandler(filters.Regex(r'^موجودی$') & filters.ChatType.GROUPS, group_balance_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^(کسر اعتبار|کسر) \d+$') & filters.REPLY & filters.ChatType.GROUPS, deduct_balance_handler))
    application.add_handler(MessageHandler(filters.Regex(r'^موجودی 💰$') & filters.ChatType.GROUPS, group_balance_handler))

    # (تغییر: هندلر کالبک عمومی برای مدیریت همه کالبک‌ها از جمله حذف کانال)
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    logging.info("Starting Telegram Bot (Polling)...")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES, 
            drop_pending_updates=True
        )
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Fatal error in bot: {e}")
        raise
