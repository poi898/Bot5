import logging
import requests
from bs4 import BeautifulSoup
from telegram.ext import ApplicationBuilder, CommandHandler
from telegram.error import TelegramError
import asyncio
import re
import os
import json

# تنظیم لاگینگ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# فایل تنظیمات
CONFIG_FILE = 'config.json'
STATE_FILE = 'last_post_ids.json'
CHECK_INTERVAL = 30
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

# لود کردن تنظیمات
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            if 'channels' not in config:
                config['channels'] = []
            if 'admin_ids' not in config:
                config['admin_ids'] = []
            # انتقال تنظیمات قدیمی به ساختار جدید
            for channel in config['channels']:
                if 'word_replacements' not in channel:
                    channel['word_replacements'] = config.get('word_replacements', [])
                if 'blacklist' not in channel:
                    channel['blacklist'] = config.get('blacklist', [])
                if 'whitelist' not in channel:
                    channel['whitelist'] = config.get('whitelist', [])
                if 'is_active' not in channel:
                    channel['is_active'] = True  # پیش‌فرض: کپی فعال
            # حذف تنظیمات قدیمی از سطح اصلی
            config.pop('word_replacements', None)
            config.pop('blacklist', None)
            config.pop('whitelist', None)
            save_config(config)
            return config
    return {
        'channels': [],
        'admin_ids': [],
        'bot_token': None
    }

# ذخیره تنظیمات
def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

# لود کردن آخرین ID پست‌ها
def load_last_post_ids():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_last_post_id(source_url, post_id):
    last_post_ids = load_last_post_ids()
    last_post_ids[source_url] = post_id
    with open(STATE_FILE, 'w') as f:
        json.dump(last_post_ids, f)

async def notify_admins(application, message):
    """ارسال پیام به ادمین‌ها"""
    config = load_config()
    if config['admin_ids']:
        for admin_id in config['admin_ids']:
            try:
                await application.bot.send_message(chat_id=admin_id, text=message)
                logger.info(f"پیام به ادمین {admin_id} ارسال شد: {message[:50]}...")
            except TelegramError as e:
                logger.error(f"خطا در ارسال پیام به ادمین {admin_id}: {str(e)}")

async def send_to_channel(application, text, dest_channel):
    """ارسال متن به کانال مقصد"""
    try:
        await application.bot.send_message(chat_id=dest_channel, text=text, parse_mode=None)
        logger.info(f"متن ارسال شد به {dest_channel}: {text[:50]}...")
        return True
    except TelegramError as e:
        error_message = f"خطا در ارسال به کانال {dest_channel}: {str(e)}"
        logger.error(error_message)
        await notify_admins(application, error_message)
        return False

def replace_words(text, word_replacements):
    """جایگزینی یا حذف کلمات در متن"""
    for item in word_replacements:
        word = item['word']
        replacement = item['replacement']
        text = re.sub(r'\b' + re.escape(word) + r'\b', replacement, text)
    return text

def is_blacklisted(text, blacklist):
    """بررسی اینکه آیا متن شامل کلمات لیست سیاه است یا خیر"""
    for word in blacklist:
        if re.search(r'\b' + re.escape(word) + r'\b', text):
            return True
    return False

def is_whitelisted(text, whitelist):
    """بررسی اینکه آیا متن شامل حداقل یکی از کلمات لیست سفید است یا خیر"""
    if not whitelist:  # اگر لیست سفید خالی باشد، نیازی به بررسی نیست
        return True
    for word in whitelist:
        if re.search(r'\b' + re.escape(word) + r'\b', text):
            return True
    return False

def scrape_channel(source_url):
    """اسکرپ کردن صفحه وب کانال با جایگزینی کلمات و بررسی لیست سیاه و سفید"""
    last_post_ids = load_last_post_ids()
    last_post_id = last_post_ids.get(source_url)
    config = load_config()
    
    # پیدا کردن کانال مربوطه
    channel_config = next((ch for ch in config['channels'] if ch['source_url'] == source_url), None)
    if not channel_config:
        logger.error(f"کانال {source_url} در تنظیمات پیدا نشد.")
        return None
    
    # بررسی وضعیت is_active
    if not channel_config.get('is_active', True):
        logger.info(f"کپی برای کانال {source_url} غیرفعال است.")
        return None

    word_replacements = channel_config.get('word_replacements', [])
    blacklist = channel_config.get('blacklist', [])
    whitelist = channel_config.get('whitelist', [])

    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(source_url, headers=headers, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        posts = soup.find_all('div', class_='tgme_widget_message')

        if not posts:
            logger.info(f"هیچ پستی در {source_url} پیدا نشد.")
            return None

        latest_post = posts[-1]
        post_data_attr = latest_post.get('data-post', '')
        if not post_data_attr:
            logger.warning(f"data-post در {source_url} پیدا نشد.")
            return None

        post_id_match = re.search(r'/(\d+)$', post_data_attr)
        post_id = post_id_match.group(1) if post_id_match else None

        text_div = latest_post.find('div', class_='tgme_widget_message_text')
        if text_div:
            raw_text = str(text_div)
            raw_text = re.sub(r'<a[^>]*>.*?</a>', '', raw_text)
            raw_text = re.sub(r'<br\s*/?>\s*<br\s*/?>', '\n\n', raw_text)
            raw_text = re.sub(r'<br\s*/?>', '\n', raw_text)
            from html import unescape
            raw_text = re.sub(r'<[^>]+>', '', raw_text)
            post_text = unescape(raw_text)
            
            # حذف حروف الفبای عربی/فارسی و اعداد عربی/فارسی
            post_text = re.sub(r'[\u0621-\u064A\u0660-\u0669\u06F0-\u06F9\u06A9\u06CC]+', '', post_text)
            
            lines = [line.rstrip() for line in post_text.split('\n')]
            cleaned_lines = []
            prev_empty = False
            for line in lines:
                if line.strip():
                    cleaned_lines.append(line)
                    prev_empty = False
                elif not prev_empty:
                    cleaned_lines.append('')
                    prev_empty = True
            post_text = '\n'.join(cleaned_lines).strip()
            # اعمال جایگزینی کلمات
            post_text = replace_words(post_text, word_replacements)

            # بررسی لیست سیاه
            if is_blacklisted(post_text, blacklist):
                logger.info(f"پست در {source_url} به دلیل وجود کلمه در لیست سیاه ارسال نشد: {post_text[:50]}...")
                return None

            # بررسی لیست سفید
            if not is_whitelisted(post_text, whitelist):
                logger.info(f"پست در {source_url} به دلیل عدم وجود کلمه در لیست سفید ارسال نشد: {post_text[:50]}...")
                return None

        else:
            post_text = None

        if post_text and post_id and post_id != last_post_id:
            save_last_post_id(source_url, post_id)
            logger.info(f"پست جدید در {source_url}: ID {post_id}, متن: {post_text[:50]}...")
            return post_text
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"خطا در درخواست HTTP برای {source_url}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"خطا در اسکرپینگ {source_url}: {str(e)}")
        return None

async def start(update, context):
    """نمایش پیام خوش‌آمدگویی و لیست دستورات"""
    help_text = (
        "به ربات خوش آمدید! 😊\n"
        "دستورات موجود:\n"
        "/start - نمایش این پیام\n"
        "/addsource <URL> <dest_channel> - افزودن کانال مبدأ و مقصد\n"
        "/removesource <URL> - حذف کانال مبدأ\n"
        "/adddestination <source_url> <dest_channel> - افزودن کانال مقصد به مبدأ\n"
        "/removedestination <source_url> <dest_channel> - حذف کانال مقصد از مبدأ\n"
        "/addword <source_url> <word> <replacement> - افزودن کلمه برای جایگزینی یا حذف در کانال\n"
        "/removeword <source_url> <word> - حذف کلمه از لیست جایگزینی کانال\n"
        "/addblack <source_url> <word> - افزودن کلمه به لیست سیاه کانال\n"
        "/removeblack <source_url> <word> - حذف کلمه از لیست سیاه کانال\n"
        "/addwhite <source_url> <word> - افزودن کلمه به لیست سفید کانال\n"
        "/removewhite <source_url> <word> - حذف کلمه از لیست سفید کانال\n"
        "/stopall - توقف کپی از همه کانال‌ها\n"
        "/startall - شروع کپی از همه کانال‌ها\n"
        "/stop <source_url> - توقف کپی از یک کانال خاص\n"
        "/startchannel <source_url> - شروع کپی از یک کانال خاص\n"
        "/getconfig - نمایش تنظیمات فعلی\n"
    )
    await update.message.reply_text(help_text)

async def add_source(update, context):
    """دستور افزودن کانال مبدأ و مقصد"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و حداقل یک کانال مقصد را وارد کنید. مثال:\n/addsource https://t.me/s/channel @destination")
            return
        source_url = args[0]
        dest_channel = args[1]
        if not source_url.startswith('https://t.me/s/'):
            await update.message.reply_text("URL نامعتبر است. باید با https://t.me/s/ شروع شود.")
            return
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                await update.message.reply_text(f"کانال مبدأ {source_url} قبلاً اضافه شده است.")
                return
        config['channels'].append({
            'source_url': source_url,
            'dest_channels': [dest_channel],
            'is_active': True,
            'word_replacements': [],
            'blacklist': [],
            'whitelist': []
        })
        save_config(config)
        await update.message.reply_text(f"کانال مبدأ {source_url} با مقصد {dest_channel} اضافه شد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def remove_source(update, context):
    """دستور حذف کانال مبدأ"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if not args:
            await update.message.reply_text("لطفاً URL کانال مبدأ را وارد کنید. مثال:\n/removesource https://t.me/s/channel")
            return
        source_url = args[0]
        config['channels'] = [ch for ch in config['channels'] if ch['source_url'] != source_url]
        save_config(config)
        last_post_ids = load_last_post_ids()
        if source_url in last_post_ids:
            del last_post_ids[source_url]
            with open(STATE_FILE, 'w') as f:
                json.dump(last_post_ids, f)
        await update.message.reply_text(f"کانال مبدأ {source_url} حذف شد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def add_destination(update, context):
    """دستور افزودن کانال مقصد به یک کانال مبدأ"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کانال مقصد را وارد کنید. مثال:\n/adddestination https://t.me/s/channel @destination")
            return
        source_url = args[0]
        dest_channel = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if dest_channel not in channel['dest_channels']:
                    channel['dest_channels'].append(dest_channel)
                    save_config(config)
                    await update.message.reply_text(f"کانال مقصد {dest_channel} به {source_url} اضافه شد.")
                else:
                    await update.message.reply_text(f"کانال مقصد {dest_channel} قبلاً برای {source_url} وجود دارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def remove_destination(update, context):
    """دستور حذف کانال مقصد از یک کانال مبدأ"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کانال مقصد را وارد کنید. مثال:\n/removedestination https://t.me/s/channel @destination")
            return
        source_url = args[0]
        dest_channel = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if dest_channel in channel['dest_channels']:
                    channel['dest_channels'].remove(dest_channel)
                    save_config(config)
                    await update.message.reply_text(f"کانال مقصد {dest_channel} از {source_url} حذف شد.")
                    if not channel['dest_channels']:
                        config['channels'].remove(channel)
                        save_config(config)
                        await update.message.reply_text(f"کانال مبدأ {source_url} به دلیل نداشتن مقصد حذف شد.")
                else:
                    await update.message.reply_text(f"کانال مقصد {dest_channel} برای {source_url} وجود ندارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def add_word(update, context):
    """دستور افزودن کلمه برای جایگزینی یا حذف در یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ، کلمه و (اختیاری) جایگزین را وارد کنید. مثال:\n/addword https://t.me/s/channel کلمه جایگزین\nیا برای حذف:\n/addword https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        replacement = args[2] if len(args) > 2 else ""
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                for item in channel['word_replacements']:
                    if item['word'] == word:
                        await update.message.reply_text(f"کلمه {word} قبلاً در لیست جایگزینی {source_url} وجود دارد.")
                        return
                channel['word_replacements'].append({'word': word, 'replacement': replacement})
                save_config(config)
                action = "حذف" if replacement == "" else f"جایگزینی با '{replacement}'"
                await update.message.reply_text(f"کلمه {word} برای {action} در {source_url} اضافه شد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def remove_word(update, context):
    """دستور حذف کلمه از لیست جایگزینی یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کلمه را وارد کنید. مثال:\n/removeword https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                channel['word_replacements'] = [item for item in channel['word_replacements'] if item['word'] != word]
                save_config(config)
                await update.message.reply_text(f"کلمه {word} از لیست جایگزینی {source_url} حذف شد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def add_black(update, context):
    """دستور افزودن کلمه به لیست سیاه یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کلمه را وارد کنید. مثال:\n/addblack https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if word not in channel['blacklist']:
                    channel['blacklist'].append(word)
                    save_config(config)
                    await update.message.reply_text(f"کلمه {word} به لیست سیاه {source_url} اضافه شد.")
                else:
                    await update.message.reply_text(f"کلمه {word} قبلاً در لیست سیاه {source_url} وجود دارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def remove_black(update, context):
    """دستور حذف کلمه از لیست سیاه یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کلمه را وارد کنید. مثال:\n/removeblack https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if word in channel['blacklist']:
                    channel['blacklist'].remove(word)
                    save_config(config)
                    await update.message.reply_text(f"کلمه {word} از لیست سیاه {source_url} حذف شد.")
                else:
                    await update.message.reply_text(f"کلمه {word} در لیست سیاه {source_url} وجود ندارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def add_white(update, context):
    """دستور افزودن کلمه به لیست سفید یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کلمه را وارد کنید. مثال:\n/addwhite https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if word not in channel['whitelist']:
                    channel['whitelist'].append(word)
                    save_config(config)
                    await update.message.reply_text(f"کلمه {word} به لیست سفید {source_url} اضافه شد.")
                else:
                    await update.message.reply_text(f"کلمه {word} قبلاً در لیست سفید {source_url} وجود دارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def remove_white(update, context):
    """دستور حذف کلمه از لیست سفید یک کانال"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("لطفاً URL کانال مبدأ و کلمه را وارد کنید. مثال:\n/removewhite https://t.me/s/channel کلمه")
            return
        source_url = args[0]
        word = args[1]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                if word in channel['whitelist']:
                    channel['whitelist'].remove(word)
                    save_config(config)
                    await update.message.reply_text(f"کلمه {word} از لیست سفید {source_url} حذف شد.")
                else:
                    await update.message.reply_text(f"کلمه {word} در لیست سفید {source_url} وجود ندارد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def stop_all(update, context):
    """دستور توقف کپی از همه کانال‌ها"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        if not config['channels']:
            await update.message.reply_text("هیچ کانالی تنظیم نشده است.")
            return
        for channel in config['channels']:
            channel['is_active'] = False
        save_config(config)
        await update.message.reply_text("کپی از همه کانال‌ها متوقف شد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def start_all(update, context):
    """دستور شروع کپی از همه کانال‌ها"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        if not config['channels']:
            await update.message.reply_text("هیچ کانالی تنظیم نشده است.")
            return
        for channel in config['channels']:
            channel['is_active'] = True
        save_config(config)
        await update.message.reply_text("کپی از همه کانال‌ها شروع شد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def stop_channel(update, context):
    """دستور توقف کپی از یک کانال خاص"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if not args:
            await update.message.reply_text("لطفاً URL کانال مبدأ را وارد کنید. مثال:\n/stop https://t.me/s/channel")
            return
        source_url = args[0]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                channel['is_active'] = False
                save_config(config)
                await update.message.reply_text(f"کپی از کانال {source_url} متوقف شد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def startchannel(update, context):
    """دستور شروع کپی از یک کانال خاص"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        args = context.args
        if not args:
            await update.message.reply_text("لطفاً URL کانال مبدأ را وارد کنید. مثال:\n/startchannel https://t.me/s/channel")
            return
        source_url = args[0]
        for channel in config['channels']:
            if channel['source_url'] == source_url:
                channel['is_active'] = True
                save_config(config)
                await update.message.reply_text(f"کپی از کانال {source_url} شروع شد.")
                return
        await update.message.reply_text(f"کانال مبدأ {source_url} پیدا نشد.")
    else:
        await update.message.reply_text("شما اجازه تغییر تنظیمات را ندارید.")

async def get_config(update, context):
    """نمایش تنظیمات فعلی"""
    config = load_config()
    user_id = update.effective_user.id

    if not config['admin_ids'] or user_id in config['admin_ids']:
        channels_info = ""
        for ch in config['channels']:
            channels_info += f"مبدأ: {ch['source_url']}\n"
            channels_info += f"  وضعیت: {'فعال' if ch.get('is_active', True) else 'غیرفعال'}\n"
            channels_info += f"  مقصد‌ها: {', '.join(ch['dest_channels'])}\n"
            channels_info += f"  کلمات برای جایگزینی/حذف:\n"
            channels_info += "\n".join(
                f"    کلمه: {item['word']}, جایگزین: {item['replacement'] or 'حذف'}"
                for item in ch['word_replacements']
            ) if ch['word_replacements'] else "    هیچ کلمه‌ای برای جایگزینی تنظیم نشده است.\n"
            channels_info += f"  لیست سیاه: {', '.join(ch['blacklist']) if ch['blacklist'] else 'خالی'}\n"
            channels_info += f"  لیست سفید: {', '.join(ch['whitelist']) if ch['whitelist'] else 'خالی'}\n"
        if not config['channels']:
            channels_info = "هیچ کانالی تنظیم نشده است."
        await update.message.reply_text(
            f"تنظیمات فعلی:\n"
            f"کانال‌ها:\n{channels_info}\n"
            f"توکن ربات: {'[مخفی]' if config['bot_token'] else 'تنظیم نشده'}\n"
            f"ادمین‌ها: {config['admin_ids'] if config['admin_ids'] else 'هیچ ادمینی تنظیم نشده'}"
        )
    else:
        await update.message.reply_text("شما اجازه مشاهده تنظیمات را ندارید.")

async def check_new_posts(application):
    """چک کردن پست‌های جدید برای هر کانال مبدأ"""
    config = load_config()
    if not config['channels']:
        error_message = "هیچ کانال مبدأ یا مقصدی تنظیم نشده است!"
        logger.error(error_message)
        await notify_admins(application, error_message)
        return

    for channel in config['channels']:
        source_url = channel['source_url']
        dest_channels = channel['dest_channels']
        for dest_channel in dest_channels[:]:
            try:
                await application.bot.get_chat(dest_channel)
            except TelegramError as e:
                error_message = f"دسترسی به کانال مقصد {dest_channel} ممکن نیست: {str(e)}"
                logger.error(error_message)
                await notify_admins(application, error_message)
                continue

        text = scrape_channel(source_url)
        if text:
            for dest_channel in dest_channels:
                await send_to_channel(application, text, dest_channel)

async def main():
    # لود تنظیمات
    config = load_config()

    # دریافت توکن از تنظیمات
    bot_token = config.get('bot_token')
    if not bot_token:
        logger.error("توکن ربات در فایل config.json پیدا نشد!")
        await notify_admins(None, "توکن ربات تنظیم نشده است!")
        return

    # ساخت اپلیکیشن
    application = ApplicationBuilder().token(bot_token).build()

    # اضافه کردن هندلرهای دستورات
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('addsource', add_source))
    application.add_handler(CommandHandler('removesource', remove_source))
    application.add_handler(CommandHandler('adddestination', add_destination))
    application.add_handler(CommandHandler('removedestination', remove_destination))
    application.add_handler(CommandHandler('addword', add_word))
    application.add_handler(CommandHandler('removeword', remove_word))
    application.add_handler(CommandHandler('addblack', add_black))
    application.add_handler(CommandHandler('removeblack', remove_black))
    application.add_handler(CommandHandler('addwhite', add_white))
    application.add_handler(CommandHandler('removewhite', remove_white))
    application.add_handler(CommandHandler('stopall', stop_all))
    application.add_handler(CommandHandler('startall', start_all))
    application.add_handler(CommandHandler('stop', stop_channel))
    application.add_handler(CommandHandler('startchannel', startchannel))
    application.add_handler(CommandHandler('getconfig', get_config))

    # تست اولیه ربات
    try:
        bot_info = await application.bot.get_me()
        logger.info(f"ربات راه‌اندازی شد: @{bot_info.username}")
        for channel in config['channels']:
            for dest_channel in channel['dest_channels']:
                try:
                    await application.bot.send_message(chat_id=dest_channel, text="تست: ربات با فرمت دقیق و خط خالی!")
                except TelegramError as e:
                    error_message = f"خطا در ارسال پیام تست به کانال مقصد {dest_channel}: {str(e)}"
                    logger.error(error_message)
                    await notify_admins(application, error_message)
    except TelegramError as e:
        logger.error(f"خطا در اتصال به تلگرام: {str(e)}")
        await notify_admins(application, f"خطا در اتصال به تلگرام: {str(e)}")
        return

    # تنظیم چک دوره‌ای
    if application.job_queue is None:
        logger.error("JobQueue در دسترس نیست! لطفاً python-telegram-bot[job-queue] را نصب کنید.")
        await notify_admins(application, "JobQueue در دسترس نیست!")
        return
    application.job_queue.run_repeating(check_new_posts, interval=CHECK_INTERVAL, first=5)

    # شروع ربات
    logger.info(f"ربات شروع به کار کرد. چک هر {CHECK_INTERVAL} ثانیه...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    # نگه داشتن ربات فعال
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("ربات متوقف شد.")
    finally:
        await application.stop()

if __name__ == '__main__':
    asyncio.run(main())