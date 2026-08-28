import os
import sqlite3
import json
import threading
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, send_from_directory

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    MessageEntity,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
)

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "autorent.db")

# --- Mini App API config ---
# PUBLIC_BASE_URL must be the public URL Railway gives this service,
# e.g. https://autosaleua-bot-production.up.railway.app (no trailing slash).
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
PHOTOS_DIR = os.getenv("PHOTOS_DIR", "listing_photos")
os.makedirs(PHOTOS_DIR, exist_ok=True)

CITY_ALIASES = {
    "київ": "Київ", "киев": "Київ", "kyiv": "Київ", "kiev": "Київ",
    "львів": "Львів", "львов": "Львів", "lviv": "Львів",
    "одеса": "Одеса", "одесса": "Одеса", "odesa": "Одеса", "odessa": "Одеса",
    "харків": "Харків", "харьков": "Харків", "kharkiv": "Харків",
    "дніпро": "Дніпро", "днепр": "Дніпро", "dnipro": "Дніпро",
    "запоріжжя": "Запоріжжя", "запорожье": "Запоріжжя", "zaporizhzhia": "Запоріжжя",
    "вінниця": "Вінниця", "винница": "Вінниця", "vinnytsia": "Вінниця",
    "полтава": "Полтава", "черкаси": "Черкаси", "черкассы": "Черкаси",
    "чернівці": "Чернівці", "черновцы": "Чернівці", "chernivtsi": "Чернівці",
    "івано-франківськ": "Івано-Франківськ", "ивано-франковск": "Івано-Франковськ",
    "тернопіль": "Тернопіль", "тернополь": "Тернопіль",
    "хмельницький": "Хмельницький", "хмельницкий": "Хмельницький",
    "житомир": "Житомир", "рівне": "Рівне", "ровно": "Рівне",
    "луцьк": "Луцьк", "луцк": "Луцьк", "ужгород": "Ужгород",
    "миколаїв": "Миколаїв", "николаев": "Миколаїв",
    "херсон": "Херсон", "суми": "Суми", "сумы": "Суми",
}

ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@AutoRentUA_Cars").strip()

# AutoSale UA custom emoji IDs supplied by the user.
CUSTOM_EMOJI = {
    "🚗": "5222330548583707187",
    "🔑": "5224734411714503138",
    "📍": "5222130196949280338",
    "📸": "5221956452637254917",
    "🔎": "5222340418418550338",
    "⛽": "5221984683457293239",
    "⚙️": "5224311670968463691",
    "📅": "5224502023919022547",
    "💰": "5221943043749357126",
    "🛡️": "5224494898568272219",
    "⭐": "5224294954955743978",
    "🔄": "5222319918539648844",
    "📢": "5224653902552538587",
    "📋": "5222170354893501372",
    "🛞": "5222139087531584886",
    "💺": "5224412903347626479",
    "🏠": "5222342522952527564",
    "💙": "5224630589470056827",
}


def custom_emoji_entities(text):
    """
    Build Telegram MessageEntity objects for our custom emoji placeholders.
    Telegram offsets/lengths are UTF-16 code units, not Python code points.
    """
    entities = []
    for emoji, custom_id in CUSTOM_EMOJI.items():
        start = 0
        while True:
            pos = text.find(emoji, start)
            if pos < 0:
                break
            prefix = text[:pos]
            emoji_text = text[pos:pos + len(emoji)]
            offset = len(prefix.encode("utf-16-le")) // 2
            length = len(emoji_text.encode("utf-16-le")) // 2
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=custom_id,
                )
            )
            start = pos + len(emoji)
    return sorted(entities, key=lambda e: e.offset)


async def reply_custom(update, text, **kwargs):
    return await update.message.reply_text(
        text,
        entities=custom_emoji_entities(text),
        **kwargs,
    )


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            language TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            ad_data TEXT,
            photos TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS testers (
            user_id INTEGER PRIMARY KEY,
            added_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "ad_data" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN ad_data TEXT")
    if "photos" not in columns:
        conn.execute("ALTER TABLE listings ADD COLUMN photos TEXT")
    if "photo_urls" not in columns:
        # Permanent public URLs for the mini app catalog (photos column keeps
        # Telegram file_ids, used only for reposting to the channel).
        conn.execute("ALTER TABLE listings ADD COLUMN photo_urls TEXT")
    conn.commit()
    conn.close()


def download_photo_permanently(file_id):
    """
    Downloads a Telegram photo (by file_id) once and saves it to disk under
    PHOTOS_DIR, returning a permanent public URL served by our own Flask API.
    Telegram file_ids/links are not stable public URLs, so this is required
    for the mini app catalog to be able to display images.
    """
    if not TOKEN:
        return None

    info = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getFile",
        params={"file_id": file_id},
        timeout=15,
    ).json()

    if not info.get("ok"):
        return None

    tg_file_path = info["result"]["file_path"]
    file_bytes = requests.get(
        f"https://api.telegram.org/file/bot{TOKEN}/{tg_file_path}",
        timeout=30,
    ).content

    ext = os.path.splitext(tg_file_path)[1] or ".jpg"
    local_name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(PHOTOS_DIR, local_name), "wb") as f:
        f.write(file_bytes)

    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/photos/{local_name}"
    # Fallback so nothing crashes if PUBLIC_BASE_URL isn't set yet — the
    # mini app just won't be able to load this image until it is.
    return f"/photos/{local_name}"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def is_admin(user_id):
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def upsert_user(update, context):
    user = update.effective_user
    conn = db()
    conn.execute(
        """
        INSERT INTO users(user_id, username, first_name, language, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            language=excluded.language
        """,
        (
            user.id,
            user.username,
            user.first_name,
            context.user_data.get("lang", "uk"),
            now_iso(),
        ),
    )
    conn.commit()
    conn.close()


def log_event(update, event_type):
    conn = db()
    conn.execute(
        "INSERT INTO events(user_id, event_type, created_at) VALUES (?, ?, ?)",
        (update.effective_user.id, event_type, now_iso()),
    )
    conn.commit()
    conn.close()


def period_count(table, date_column, start):
    conn = db()
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE {date_column} >= ?",
        (start.isoformat(),),
    ).fetchone()
    conn.close()
    return row["n"]


def total_count(table):
    conn = db()
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    conn.close()
    return row["n"]


def period_event_count(event_type, start):
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = ? AND created_at >= ?",
        (event_type, start.isoformat()),
    ).fetchone()
    conn.close()
    return row["n"]


def admin_dashboard():
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)
    year = now - timedelta(days=365)

    listings_total = total_count("listings")
    users_total = total_count("users")

    today_listings = period_count("listings", "created_at", today)
    week_listings = period_count("listings", "created_at", week)
    month_listings = period_count("listings", "created_at", month)
    year_listings = period_count("listings", "created_at", year)

    today_views = period_count("events", "created_at", today)
    week_views = period_count("events", "created_at", week)
    month_views = period_count("events", "created_at", month)
    year_views = period_count("events", "created_at", year)

    today_contacts = period_event_count("contact", today)
    week_contacts = period_event_count("contact", week)
    month_contacts = period_event_count("contact", month)
    year_contacts = period_event_count("contact", year)

    return (
        "📊 AutoSale UA — Dashboard\n\n"
        f"🚘 Объявления: {listings_total:,}\n"
        f"👤 Пользователи: {users_total:,}\n"
        f"⚡ Сегодня: {today_listings:,}\n"
        f"📈 Неделя: {week_listings:,}\n"
        f"📊 Месяц: {month_listings:,}\n"
        f"🏆 Год: {year_listings:,}\n"
        f"♾️ За всё время: {listings_total:,}\n"
        f"👁️ Просмотры: {today_views:,} / {week_views:,} / {month_views:,} / {year_views:,} / {total_count('events'):,}\n"
        f"💬 Переходы: {today_contacts:,} / {week_contacts:,} / {month_contacts:,} / {year_contacts:,} / "
        f"{period_event_count('contact', datetime.min.replace(tzinfo=timezone.utc)):,}"
    )


async def channel_info(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(f"📢 Канал публикаций: {CHANNEL_USERNAME}")


async def admin(update, context):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(admin_dashboard())


async def add_tester(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /addtester TELEGRAM_ID")
        return

    tester_id = int(context.args[0])
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO testers(user_id, added_at) VALUES (?, ?)",
        (tester_id, now_iso()),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Тестировщик добавлен: {tester_id}")


async def remove_tester(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /removetester TELEGRAM_ID")
        return

    tester_id = int(context.args[0])
    conn = db()
    conn.execute("DELETE FROM testers WHERE user_id = ?", (tester_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Тестировщик удалён: {tester_id}")


async def list_testers(update, context):
    if not is_admin(update.effective_user.id):
        return

    conn = db()
    rows = conn.execute("SELECT user_id FROM testers ORDER BY added_at").fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Тестировщиков пока нет.")
        return

    text = "🧪 Тестировщики:\n\n" + "\n".join(
        f"• {row['user_id']}" for row in rows
    )
    await update.message.reply_text(text)


BRAND, MODEL, FUEL, YEAR, MILEAGE, TRANSMISSION, PRICE, CITY, DESCRIPTION, PHOTO, CONFIRM = range(11)

TEXT = {
    "uk": {
        "welcome": "🔑 Ласкаво просимо до AutoRent UA!\nОрендуйте та здавайте автомобілі в оренду.",
        "sell": "🔑 Здати авто в оренду",
        "my": "📋 Мої оголошення",
        "brand": "🚗 Введіть марку:",
        "model": "🔑 Введіть модель:",
        "fuel": "⛽ Оберіть пальне:",
        "year": "📅 Введіть рік:",
        "year_error": "📅 Введіть 4 цифри, 1950–2026.",
        "mileage": "🛞 Введіть пробіг, км:",
        "mileage_error": "🛞 Невірний формат.",
        "transmission": "⚙️ Оберіть коробку:",
        "price": "💰 Введіть ціну оренди за тиждень, грн:",
        "price_error": "💰 Введіть від 1 до 4 цифр.",
        "city": "📍 Введіть місто:",
        "city_error": "📍 Спробуйте ще раз.",
        "description": "📋 Введіть опис:",
        "description_error": "📋 Занадто довгий опис.",
        "photo": "📸 Надішліть фото автомобіля:",
        "photo_added": "📸 Фото додано: {n}/5",
        "photo_limit": "📸 Додано максимум 5 фото. Натисніть «Готово».",
        "done": "✅ Готово",
        "preview": "📋 Перевірте оголошення:",
        "publish": "🚀 Опублікувати",
        "cancel": "❌ Скасувати",
        "created": "🎉 Оголошення створено!",
        "cancelled": "Оголошення скасовано.",
        "my_msg": "📋 Ваші оголошення будуть доступні після підключення бази даних.",
        "petrol": "Бензин",
        "diesel": "Дизель",
        "hybrid": "Гібрид",
        "electric": "Електро",
        "gas": "Газ",
        "auto": "Автомат",
        "manual": "Механіка",
    },
}


def lang(context):
    return context.user_data.get("lang", "uk")


def tr(context, key, **kwargs):
    return TEXT[lang(context)][key].format(**kwargs)


def main_menu(context):
    return ReplyKeyboardMarkup(
        [
            [tr(context, "sell")],
            [tr(context, "my")],
        ],
        resize_keyboard=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update, context)
    log_event(update, "start")

    if context.args and context.args[0].lower() == "tester":
        conn = db()
        conn.execute(
            "INSERT OR IGNORE INTO testers(user_id, added_at) VALUES (?, ?)",
            (update.effective_user.id, now_iso()),
        )
        conn.commit()
        conn.close()

    is_sell_link = bool(context.args and context.args[0].lower() == "sell")
    context.user_data["lang"] = "uk"

    if is_sell_link:
        return await sell_start(update, context)

    await reply_custom(
        update,
        tr(context, "welcome"),
        reply_markup=main_menu(context),
    )
    return ConversationHandler.END


async def restart_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ad"] = {"photos": []}
    await reply_custom(
        update,
        tr(context, "brand"),
        reply_markup=ReplyKeyboardRemove(),
    )
    return BRAND


async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["ad"] = {"photos": []}
    await reply_custom(
        update,
        tr(context, "brand"),
        reply_markup=ReplyKeyboardRemove(),
    )
    return BRAND


async def invalid_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "❗ Невірний формат." if lang(context) == "uk" else "❗ Invalid format."
    await reply_custom(update, text)


async def get_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value or len(value) > 10:
        await reply_custom(update, tr(context, "brand"))
        return BRAND
    context.user_data["ad"]["brand"] = value
    await reply_custom(update, tr(context, "model"))
    return MODEL


async def get_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value or len(value) > 10:
        await reply_custom(update, tr(context, "model"))
        return MODEL

    context.user_data["ad"]["model"] = value
    await reply_custom(
        update,
        tr(context, "transmission"),
        reply_markup=ReplyKeyboardMarkup(
            [[tr(context, "auto"), tr(context, "manual")]],
            resize_keyboard=True,
        ),
    )
    return TRANSMISSION


async def get_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed = [
        tr(context, "petrol"),
        tr(context, "diesel"),
        tr(context, "hybrid"),
        tr(context, "electric"),
        tr(context, "gas"),
    ]
    if update.message.text not in allowed:
        await reply_custom(update, tr(context, "fuel"))
        return FUEL

    context.user_data["ad"]["fuel"] = update.message.text
    await reply_custom(
        update,
        tr(context, "year"),
        reply_markup=ReplyKeyboardRemove(),
    )
    return YEAR


async def get_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value.isdigit() or len(value) != 4 or not 1950 <= int(value) <= 2026:
        await reply_custom(update, tr(context, "year_error"))
        return YEAR

    context.user_data["ad"]["year"] = value
    await reply_custom(update, tr(context, "mileage"))
    return MILEAGE


async def get_mileage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value.isdigit() or len(value) > 6 or int(value) < 1:
        await reply_custom(update, tr(context, "mileage_error"))
        return MILEAGE

    context.user_data["ad"]["mileage"] = value
    await reply_custom(update, tr(context, "price"))
    return PRICE


async def get_transmission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed = [tr(context, "auto"), tr(context, "manual")]
    if update.message.text not in allowed:
        await reply_custom(update, tr(context, "transmission"))
        return TRANSMISSION

    context.user_data["ad"]["transmission"] = update.message.text
    keyboard = [
        [tr(context, "petrol"), tr(context, "diesel")],
        [tr(context, "hybrid"), tr(context, "electric")],
        [tr(context, "gas")],
    ]
    await reply_custom(
        update,
        tr(context, "fuel"),
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return FUEL


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if not value.isdigit() or len(value) > 4:
        await reply_custom(update, tr(context, "price_error"))
        return PRICE

    context.user_data["ad"]["price"] = value
    await reply_custom(update, tr(context, "city"))
    return CITY


async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    normalized = CITY_ALIASES.get(value.casefold())

    if not normalized:
        await reply_custom(
            update,
            "📍 Місто не знайдено. Спробуйте ще раз."
            if lang(context) == "uk"
            else "📍 City not found. Try again.",
        )
        return CITY

    context.user_data["ad"]["city"] = normalized
    await reply_custom(update, tr(context, "description"))
    return DESCRIPTION


async def get_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.message.text.strip()
    if len(value.splitlines()) > 7:
        await reply_custom(update, tr(context, "description_error"))
        return DESCRIPTION

    context.user_data["ad"]["description"] = value
    await reply_custom(update, tr(context, "photo"))
    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data["ad"]["photos"]

    if len(photos) >= 5:
        await reply_custom(update, tr(context, "photo_limit"))
        return PHOTO

    photos.append(update.message.photo[-1].file_id)

    if len(photos) == 1:
        await update.message.reply_text(
            tr(context, "done"),
            reply_markup=ReplyKeyboardMarkup(
                [[tr(context, "done")]],
                resize_keyboard=True,
            ),
        )
    return PHOTO


async def photo_invalid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_custom(
        update,
        "📸 Надішліть фото або натисніть «Готово»."
        if lang(context) == "uk"
        else "📸 Send a photo or press “Done”.",
    )
    return PHOTO


def build_ad_caption(ad):
    seller = ad.get("seller", "")
    return (
        f"📍 {ad['city']}\n"
        f"🚗 {ad['brand']}\n"
        f"🔑 {ad['model']}\n"
        f"📅 {ad['year']}\n"
        f"🛞 {ad['mileage']} км\n"
        f"⚙️ {ad['transmission']}\n"
        f"⛽ {ad['fuel']}\n"
        f"💰 {int(ad['price'])} грн/тиждень\n"
        f"🛡️ {seller}\n"
        f"📋 {ad['description']}"
    )


async def finish_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data["ad"]["photos"]
    if not photos:
        await reply_custom(update, tr(context, "photo"))
        return PHOTO

    ad = context.user_data["ad"]
    username = update.effective_user.username
    seller = f"@{username}" if username else update.effective_user.full_name
    ad["seller"] = seller

    preview = (
        f"📋 Перевірте оголошення:\n\n"
        f"📍 {ad['city']}\n"
        f"🚗 {ad['brand']}\n"
        f"🔑 {ad['model']}\n"
        f"📅 {ad['year']}\n"
        f"🛞 {ad['mileage']} км\n"
        f"⚙️ {ad['transmission']}\n"
        f"⛽ {ad['fuel']}\n"
        f"💰 {int(ad['price'])} грн/тиждень\n"
        f"🛡️ {seller}\n"
        f"📋 {ad['description']}\n\n"
    )

    await reply_custom(
        update,
        preview,
        reply_markup=ReplyKeyboardMarkup(
            [[tr(context, "publish")], [tr(context, "cancel")]],
            resize_keyboard=True,
        ),
    )
    return CONFIRM


async def send_channel_publication(context, caption, photos):
    entities = custom_emoji_entities(caption)

    if len(photos) == 1:
        return await context.bot.send_photo(
            chat_id=CHANNEL_USERNAME,
            photo=photos[0],
            caption=caption,
            caption_entities=entities,
        )

    media_group = [
        InputMediaPhoto(
            media=file_id,
            caption=caption if index == 0 else None,
            caption_entities=entities if index == 0 else None,
        )
        for index, file_id in enumerate(photos)
    ]
    return await context.bot.send_media_group(
        chat_id=CHANNEL_USERNAME,
        media=media_group,
    )


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == tr(context, "cancel"):
        context.user_data.pop("ad", None)
        await update.message.reply_text(
            tr(context, "cancelled"),
            reply_markup=main_menu(context),
        )
        return ConversationHandler.END

    if update.message.text == tr(context, "publish"):
        ad = context.user_data.get("ad")
        if not ad:
            await update.message.reply_text(
                tr(context, "cancelled"),
                reply_markup=main_menu(context),
            )
            return ConversationHandler.END

        photos = ad.get("photos", [])
        caption = build_ad_caption(ad)

        try:
            await send_channel_publication(context, caption, photos)

            photo_urls = [
                url for url in (download_photo_permanently(fid) for fid in photos)
                if url
            ]

            conn = db()
            conn.execute(
                "INSERT INTO listings(user_id, created_at, status, ad_data, photos, photo_urls) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    update.effective_user.id,
                    now_iso(),
                    "active",
                    json.dumps(ad, ensure_ascii=False),
                    json.dumps(photos),
                    json.dumps(photo_urls),
                ),
            )
            conn.commit()
            conn.close()
            log_event(update, "publish")

            await update.message.reply_text(
                tr(context, "created"),
                reply_markup=main_menu(context),
            )
        except Exception:
            await update.message.reply_text(
                "❗ Не вдалося опублікувати. Перевірте канал та права бота."
                if lang(context) == "uk"
                else "❗ Publication failed. Check the channel and bot permissions.",
                reply_markup=main_menu(context),
            )

        return ConversationHandler.END

    return CONFIRM


async def my_listings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    rows = conn.execute(
        "SELECT id, created_at, ad_data, photos FROM listings WHERE user_id = ? ORDER BY id DESC",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()

    if not rows:
        await reply_custom(
            update,
            "📋 У вас поки немає оголошень.",
            reply_markup=main_menu(context),
        )
        return

    buttons = []
    text_parts = ["📋 Мої оголошення\n"]

    for number, row in enumerate(rows, start=1):
        if row["ad_data"]:
            try:
                ad = json.loads(row["ad_data"])
                title = f"{ad.get('brand', '')} {ad.get('model', '')}".strip() or "Авто"
                year = ad.get("year", "")
                price = ad.get("price", "")
                city = ad.get("city", "")
                details = " • ".join(
                    part for part in [
                        title,
                        year,
                        f"{int(price)} грн/тижд" if str(price).isdigit() else price,
                        city,
                    ] if part
                )
                text_parts.append(f"{number}. 🚗 {details}")
                buttons.append([
                    InlineKeyboardButton(
                        f"🔄 Повторно розмістити №{number}",
                        callback_data=f"repost:{row['id']}",
                    )
                ])
            except (ValueError, TypeError, json.JSONDecodeError):
                text_parts.append(
                    f"{number}. ⚠️ Оголошення №{row['id']}: дані пошкоджені"
                )
        else:
            text_parts.append(
                f"{number}. ℹ️ Оголошення №{row['id']} створене у старій версії бота"
            )

    await reply_custom(
        update,
        "\n".join(text_parts),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else main_menu(context),
    )


async def repost_listing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        listing_id = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text("❗ Не вдалося знайти оголошення.")
        return

    conn = db()
    row = conn.execute(
        "SELECT ad_data, photos FROM listings WHERE id = ? AND user_id = ?",
        (listing_id, query.from_user.id),
    ).fetchone()
    conn.close()

    if not row or not row["ad_data"]:
        await query.message.reply_text(
            "ℹ️ Це оголошення створене до підключення збереження даних. Його потрібно створити заново."
        )
        return

    try:
        ad = json.loads(row["ad_data"])
        photos = json.loads(row["photos"] or "[]")
    except (ValueError, TypeError, json.JSONDecodeError):
        await query.message.reply_text("❗ Не вдалося відновити оголошення.")
        return

    if not photos:
        await query.message.reply_text("❗ У цього оголошення немає фото.")
        return

    caption = build_ad_caption(ad)

    try:
        await send_channel_publication(context, caption, photos)
        log_event(update, "repost")
        await query.message.reply_text(
            "🚀 Оголошення повторно опубліковано!",
            reply_markup=main_menu(context),
        )
    except Exception:
        await query.message.reply_text(
            "❗ Не вдалося повторно опублікувати. Перевірте канал та права бота.",
            reply_markup=main_menu(context),
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("ad", None)
    await update.message.reply_text(
        tr(context, "cancelled"),
        reply_markup=main_menu(context),
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_custom(
        update,
        tr(context, "welcome"),
        reply_markup=main_menu(context),
    )


# --- Mini App API (served alongside the bot on the same Railway service) ---

flask_app = Flask(__name__)


@flask_app.after_request
def add_cors_headers(response):
    # GitHub Pages (the mini app) fetches this cross-origin, so allow it.
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@flask_app.route("/photos/<path:filename>")
def serve_photo(filename):
    return send_from_directory(PHOTOS_DIR, filename)


@flask_app.route("/listings")
def listings_api():
    """
    Returns active listings as JSON, in the same shape the mini app's
    data.js CAR_LISTINGS array uses, so catalog.js only needs to switch
    from reading data.js to fetch()-ing this endpoint.
    """
    conn = db()
    rows = conn.execute(
        "SELECT id, ad_data, photo_urls FROM listings "
        "WHERE status = 'active' AND ad_data IS NOT NULL "
        "ORDER BY id DESC"
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        try:
            ad = json.loads(row["ad_data"])
        except (TypeError, json.JSONDecodeError):
            continue
        result.append({
            "id": row["id"],
            "type": "rent",
            "city": ad.get("city", ""),
            "brand": ad.get("brand", ""),
            "model": ad.get("model", ""),
            "year": ad.get("year", ""),
            "mileage": ad.get("mileage", ""),
            "transmission": ad.get("transmission", ""),
            "fuel": ad.get("fuel", ""),
            "price": ad.get("price", ""),
            "currency": "UAH",
            "period": "week",
            "description": ad.get("description", ""),
            "seller": ad.get("seller", ""),
            "photos": json.loads(row["photo_urls"] or "[]"),
        })

    return jsonify(result)


def run_flask():
    port = int(os.getenv("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port)


def main():
    init_db()
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(
                filters.Regex(r"^🔑 Здати авто в оренду$"),
                sell_start,
            ),
        ],
        states={
            BRAND: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_brand),
                MessageHandler(filters.ALL, invalid_text),
            ],
            MODEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_model),
                MessageHandler(filters.ALL, invalid_text),
            ],
            FUEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_fuel),
                MessageHandler(filters.ALL, invalid_text),
            ],
            YEAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_year),
                MessageHandler(filters.ALL, invalid_text),
            ],
            MILEAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_mileage),
                MessageHandler(filters.ALL, invalid_text),
            ],
            TRANSMISSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_transmission),
                MessageHandler(filters.ALL, invalid_text),
            ],
            PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_price),
                MessageHandler(filters.ALL, invalid_text),
            ],
            CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_city),
                MessageHandler(filters.ALL, invalid_text),
            ],
            DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_description),
                MessageHandler(filters.ALL, invalid_text),
            ],
            PHOTO: [
                MessageHandler(filters.PHOTO, get_photo),
                MessageHandler(filters.Regex(r"^✅ Готово$"), finish_photos),
                MessageHandler(filters.ALL, photo_invalid),
            ],
            CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(
                filters.Regex(r"^🔑 Здати авто в оренду$"),
                restart_sell,
            ),
        ],
    )

    app.add_handler(CommandHandler("sell", sell_start))
    app.add_handler(CommandHandler("my_ads", my_listings))
    app.add_handler(CallbackQueryHandler(repost_listing, pattern=r"^repost:\d+$"))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("channel", channel_info))
    app.add_handler(CommandHandler("addtester", add_tester))
    app.add_handler(CommandHandler("removetester", remove_tester))
    app.add_handler(CommandHandler("testers", list_testers))
    app.add_handler(conversation)

    app.add_handler(
        MessageHandler(
            filters.Regex(r"^📋 Мої оголошення$"),
            my_listings,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("AutoRent UA bot started — version 1")
    app.run_polling()


if __name__ == "__main__":
    main()
