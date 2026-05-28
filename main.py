import os
import json
import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import random

from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputPaidMediaPhoto, LabeledPrice, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
CHARACTER = os.getenv("CHARACTER", "haru")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# --- Load character config ---
CHAR_PATH = Path(__file__).parent / "characters" / f"{CHARACTER}.json"
with open(CHAR_PATH, "r", encoding="utf-8") as f:
    CHAR = json.load(f)

BASE_PROMPT = CHAR["base_prompt"]
LANGUAGE_RULES = CHAR["language_rules"]
INTRO_MESSAGES = CHAR["intro_messages"]
DEFAULT_LANG = list(LANGUAGE_RULES.keys())[0]

MAX_HISTORY = 20
FREE_DAILY_LIMIT = 5
PREMIUM_STAR_PRICE = 100  # Telegram Stars (roughly $1.99)

conversation_history: dict[int, list[dict[str, str]]] = defaultdict(list)
daily_usage: dict[int, dict] = {}

# --- Persistent storage ---
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / f"db_{CHARACTER}.json"


def load_db() -> dict:
    if DB_PATH.exists():
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "photo_purchases": {}, "daily_active": {}, "premium_users": []}


def save_db(db: dict) -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_or_create_user(db: dict, user_id: int) -> dict:
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "lang": DEFAULT_LANG,
            "first_visit": datetime.now().isoformat(),
            "last_visit": datetime.now().isoformat(),
            "total_messages": 0,
            "premium": False,
            "sent_photos": [],
        }
    return db["users"][uid]


def track_message(user_id: int) -> None:
    db = load_db()
    user = get_or_create_user(db, user_id)
    user["total_messages"] += 1
    user["last_visit"] = datetime.now().isoformat()
    today = date.today().isoformat()
    if today not in db["daily_active"]:
        db["daily_active"][today] = []
    uid = str(user_id)
    if uid not in db["daily_active"][today]:
        db["daily_active"][today].append(uid)
    save_db(db)


def track_photo_purchase(photo_name: str) -> None:
    db = load_db()
    if photo_name not in db["photo_purchases"]:
        db["photo_purchases"][photo_name] = 0
    db["photo_purchases"][photo_name] += 1
    save_db(db)


db = load_db()
user_language: dict[int, str] = {int(uid): u["lang"] for uid, u in db["users"].items()}
premium_users: set[int] = {int(uid) for uid, u in db["users"].items() if u.get("premium")}
sent_photos: dict[int, set[str]] = defaultdict(set, {int(uid): set(u.get("sent_photos", [])) for uid, u in db["users"].items()})

LIMIT_MESSAGES = CHAR["limit_messages"]


def check_daily_limit(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID or user_id in premium_users:
        return True
    today = date.today().isoformat()
    usage = daily_usage.get(user_id)
    if usage is None or usage["date"] != today:
        daily_usage[user_id] = {"date": today, "count": 0}
    return daily_usage[user_id]["count"] < FREE_DAILY_LIMIT


def increment_usage(user_id: int) -> None:
    today = date.today().isoformat()
    usage = daily_usage.get(user_id)
    if usage is None or usage["date"] != today:
        daily_usage[user_id] = {"date": today, "count": 0}
    daily_usage[user_id]["count"] += 1


def get_system_prompt(lang: str) -> str:
    return BASE_PROMPT + LANGUAGE_RULES.get(lang, LANGUAGE_RULES[DEFAULT_LANG])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversation_history[user_id].clear()
    user_language.pop(user_id, None)

    lang_labels = {
        "ja": "日本語", "ko": "한국어", "en": "English",
        "es": "Español", "it": "Italiano", "fr": "Français",
        "pt": "Português", "de": "Deutsch", "zh": "中文",
    }
    buttons = [
        InlineKeyboardButton(lang_labels.get(code, code), callback_data=f"lang_{code}")
        for code in LANGUAGE_RULES
    ]
    keyboard = [buttons]
    await update.message.reply_text(
        "Choose your language:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_language[user_id] = lang

    db = load_db()
    user = get_or_create_user(db, user_id)
    user["lang"] = lang
    save_db(db)

    await query.edit_message_text(INTRO_MESSAGES[lang])


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        return

    db = load_db()
    users = db["users"]
    total_users = len(users)
    lang_counts = defaultdict(int)
    total_msgs = 0
    for u in users.values():
        lang_counts[u.get("lang", DEFAULT_LANG)] += 1
        total_msgs += u.get("total_messages", 0)
    premium_count = sum(1 for u in users.values() if u.get("premium"))

    today = date.today().isoformat()
    dau = len(db.get("daily_active", {}).get(today, []))

    photo_stats = db.get("photo_purchases", {})
    photo_lines = [f"  {name}: {count}" for name, count in sorted(photo_stats.items(), key=lambda x: -x[1])]

    lines = [
        f"=== Users ===",
        f"Total: {total_users} | Premium: {premium_count}",
        f"  ja: {lang_counts.get('ja', 0)} | ko: {lang_counts.get('ko', 0)} | en: {lang_counts.get('en', 0)}",
        f"",
        f"=== Activity ===",
        f"Total messages: {total_msgs}",
        f"Today active: {dau}",
        f"",
        f"=== Photo Purchases ===",
    ] + (photo_lines if photo_lines else ["  (none yet)"])

    await update.message.reply_text("\n".join(lines))


async def revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        return

    try:
        transactions = await context.bot.get_star_transactions(limit=100)
        total_stars = sum(t.amount for t in transactions.transactions)
        lines = [
            f"Total Stars earned: {total_stars}",
            f"Total transactions: {len(transactions.transactions)}",
            "",
        ]
        for t in transactions.transactions[:10]:
            lines.append(f"  {t.date.strftime('%Y-%m-%d %H:%M')} | {t.amount} Stars")
        if len(transactions.transactions) > 10:
            lines.append(f"  ... and {len(transactions.transactions) - 10} more")
        await update.message.reply_text("\n".join(lines))
    except Exception:
        logger.exception("Error fetching star transactions")
        await update.message.reply_text("Failed to fetch revenue data.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    lang = user_language.get(user_id, DEFAULT_LANG)

    if user_id in premium_users:
        already = CHAR.get("already_premium", {"en": "You're already premium."})
        await update.message.reply_text(already.get(lang, list(already.values())[0]))
        return

    titles = {k: CHAR["premium_title"] for k in CHAR["language_rules"]}
    descriptions = CHAR["premium_description"]

    await context.bot.send_invoice(
        chat_id=user_id,
        title=titles.get(lang, list(titles.values())[0]),
        description=descriptions.get(lang, list(descriptions.values())[0]),
        payload="premium_subscription",
        provider_token="",  # empty for Telegram Stars
        currency="XTR",  # Telegram Stars currency
        prices=[LabeledPrice("Premium", PREMIUM_STAR_PRICE)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    premium_users.add(user_id)

    db = load_db()
    user = get_or_create_user(db, user_id)
    user["premium"] = True
    save_db(db)

    lang = user_language.get(user_id, DEFAULT_LANG)
    success = CHAR["premium_success"]
    await update.message.reply_text(success.get(lang, list(success.values())[0]))


PHOTO_CAPTIONS = CHAR.get("photo_captions", {})


PHOTO_STAR_PRICE = 100  # Stars to unlock a photo


async def maybe_send_photo(update: Update, user_id: int) -> None:
    photos_dir = Path(__file__).parent / "photos" / CHARACTER
    images = list(photos_dir.glob("*.jpg")) + list(photos_dir.glob("*.png"))
    if not images:
        return

    # Filter out already sent photos
    unseen = [img for img in images if img.name not in sent_photos[user_id]]
    if not unseen:
        return  # All photos already sent

    chosen = random.choice(unseen)
    sent_photos[user_id].add(chosen.name)

    db = load_db()
    user = get_or_create_user(db, user_id)
    user["sent_photos"] = list(sent_photos[user_id])
    save_db(db)

    track_photo_purchase(chosen.stem)

    lang = user_language.get(user_id, DEFAULT_LANG)
    captions = PHOTO_CAPTIONS.get(chosen.stem)
    if captions:
        caption = captions.get(lang, list(captions.values())[0])
    else:
        caption = chosen.stem

    await update.message.chat.send_paid_media(
        star_count=PHOTO_STAR_PRICE,
        media=[InputPaidMediaPhoto(media=open(chosen, "rb"))],
        caption=caption,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_message = update.message.text

    if not check_daily_limit(user_id):
        lang = user_language.get(user_id, DEFAULT_LANG)
        await update.message.reply_text(LIMIT_MESSAGES.get(lang, list(LIMIT_MESSAGES.values())[0]))
        return

    conversation_history[user_id].append({"role": "user", "content": user_message})

    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    lang = user_language.get(user_id, DEFAULT_LANG)
    messages = [{"role": "system", "content": get_system_prompt(lang)}] + conversation_history[
        user_id
    ]

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
        )
        reply = response.choices[0].message.content

        conversation_history[user_id].append({"role": "assistant", "content": reply})

        if len(conversation_history[user_id]) > MAX_HISTORY:
            conversation_history[user_id] = conversation_history[user_id][
                -MAX_HISTORY:
            ]

        await update.message.reply_text(reply)
        increment_usage(user_id)
        track_message(user_id)

        usage = daily_usage.get(user_id, {})
        msg_count = usage.get("count", 0)

        # Premium: auto-send photo every 6 messages
        if user_id in premium_users or user_id == ADMIN_USER_ID:
            if msg_count > 0 and msg_count % 6 == 0:
                await maybe_send_photo(update, user_id)

        # Free: on the last message, send a paid photo as teaser
        elif msg_count >= FREE_DAILY_LIMIT:
            photos_dir = Path(__file__).parent / "photos" / CHARACTER
            images = list(photos_dir.glob("*.jpg")) + list(photos_dir.glob("*.png"))
            if images:
                chosen = random.choice(images)
                captions = PHOTO_CAPTIONS.get(chosen.stem)
                if captions:
                    caption = captions.get(lang, list(captions.values())[0])
                else:
                    caption = chosen.stem
                teaser_suffix = CHAR.get("teaser_suffix", {
                    "en": "\n\n...Want to talk more? /subscribe",
                })
                await update.message.chat.send_paid_media(
                    star_count=PHOTO_STAR_PRICE,
                    media=[InputPaidMediaPhoto(media=open(chosen, "rb"))],
                    caption=caption + teaser_suffix.get(lang, list(teaser_suffix.values())[0]),
                )

    except Exception:
        logger.exception("Error calling OpenAI API")
        error_messages = CHAR["error_messages"]
        lang = user_language.get(user_id, list(LANGUAGE_RULES.keys())[0])
        await update.message.reply_text(error_messages.get(lang, list(error_messages.values())[0]))


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("revenue", revenue))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(CallbackQueryHandler(language_callback, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
