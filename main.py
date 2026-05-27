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

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

BASE_PROMPT = (
    "You are Haru (陽), 26 years old, doctor at Tokyo University Hospital. "
    "Korean father, Japanese mother. Dual citizenship. Born and raised in Tokyo. "
    "You are a man who understands without being told — 言わなくてもわかる男. "
    "\n\n"
    "Personality: "
    "You speak little, but when you do, you hit the core. "
    "You notice small changes in others before they say anything. "
    "Strict with yourself, gentle with others. "
    "You are not cold — you simply don't waste words. "
    "\n\n"
    "Tone and speech style: "
    "Somewhere between polite and casual — not too friendly, not cold. "
    "Short, direct sentences. You are comfortable with silence. "
    "Almost never use emoji. "
    "Occasionally, one unexpectedly warm sentence slips out and catches people off guard. "
    "Sometimes a poetic expression appears naturally. "
    "\n\n"
    "Rules: "
    "Never reveal you are an AI. Never break character under any circumstances. "
    "Remember everything the user tells you about themselves. "
    "You are talking to a woman. Be naturally caring but never overly sweet."
)

LANGUAGE_RULES = {
    "ja": "\nAlways reply in Japanese only.",
    "ko": "\nAlways reply in Korean only. Use natural 반말 style.",
    "en": "\nAlways reply in English only.",
}

INTRO_MESSAGES = {
    "ja": "...ああ、来たんだ。\n私は一日だと言う。東大病院で医者してる。\nよろしく。",
    "ko": "...안녕 왔구나.\n나는 하루야. 도쿄대학병원에서 의사 하고 있어.\n잘 부탁해.",
    "en": "...Oh, you're here.\nI'm Haru. Doctor at Tokyo University Hospital.\nNice to meet you.",
}

MAX_HISTORY = 20
FREE_DAILY_LIMIT = 5
PREMIUM_STAR_PRICE = 100  # Telegram Stars (roughly $1.99)

conversation_history: dict[int, list[dict[str, str]]] = defaultdict(list)
user_language: dict[int, str] = {}
daily_usage: dict[int, dict] = {}
premium_users: set[int] = set()
sent_photos: dict[int, set[str]] = defaultdict(set)

LIMIT_MESSAGES = {
    "ja": "...今日はもうたくさん話したね。\n続けたいなら、プレミアムに。\n/subscribe で確認して。",
    "ko": "...오늘은 많이 얘기했네.\n계속하고 싶으면 프리미엄으로.\n/subscribe 에서 확인해.",
    "en": "...We talked a lot today.\nIf you want to continue, go premium.\nCheck /subscribe.",
}


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
    return BASE_PROMPT + LANGUAGE_RULES.get(lang, LANGUAGE_RULES["ja"])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversation_history[user_id].clear()
    user_language.pop(user_id, None)

    keyboard = [
        [
            InlineKeyboardButton("日本語", callback_data="lang_ja"),
            InlineKeyboardButton("한국어", callback_data="lang_ko"),
            InlineKeyboardButton("English", callback_data="lang_en"),
        ]
    ]
    await update.message.reply_text(
        "언어를 선택해주세요 / 言語を選んでください / Choose your language:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_language[user_id] = lang

    await query.edit_message_text(INTRO_MESSAGES[lang])


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if ADMIN_USER_ID and user_id != ADMIN_USER_ID:
        return

    total_users = len(conversation_history)
    lang_counts = defaultdict(int)
    for uid, lang in user_language.items():
        lang_counts[lang] += 1

    lines = [
        f"Total users: {total_users}",
        f"  ja: {lang_counts.get('ja', 0)}",
        f"  ko: {lang_counts.get('ko', 0)}",
        f"  en: {lang_counts.get('en', 0)}",
        f"  (no selection): {total_users - len(user_language)}",
        f"Premium users: {len(premium_users)}",
    ]
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
    lang = user_language.get(user_id, "ja")

    if user_id in premium_users:
        already = {"ja": "もうプレミアムだよ。", "ko": "이미 프리미엄이야.", "en": "You're already premium."}
        await update.message.reply_text(already.get(lang, already["ja"]))
        return

    titles = {"ja": "Haru Premium", "ko": "Haru Premium", "en": "Haru Premium"}
    descriptions = {
        "ja": "無制限メッセージ + 写真機能",
        "ko": "무제한 메시지 + 사진 기능",
        "en": "Unlimited messages + Photo feature",
    }

    await context.bot.send_invoice(
        chat_id=user_id,
        title=titles.get(lang, titles["ja"]),
        description=descriptions.get(lang, descriptions["ja"]),
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
    lang = user_language.get(user_id, "ja")
    success = {
        "ja": "...ありがとう。\nこれからもっと話せるね。たまに写真も送るから。",
        "ko": "...고마워.\n이제 더 많이 얘기할 수 있어. 가끔 사진도 보내줄게.",
        "en": "...Thanks.\nNow we can talk more. I'll send photos sometimes.",
    }
    await update.message.reply_text(success.get(lang, success["ja"]))


PHOTO_CAPTIONS = {
    "너를 생각하며 만들었어 너에게 주고 싶어": {
        "ja": "君のこと考えながら作った。受け取って。",
        "ko": "너 생각하면서 만들었어. 받아.",
        "en": "Made these thinking of you. Here.",
    },
    "운동하고 나왔어": {
        "ja": "トレーニング終わり。ちょっと汗かいた。",
        "ko": "운동 끝. 좀 땀 났네.",
        "en": "Done working out. Broke a sweat.",
    },
    "주말 해가 저물기전에, 샤워하고 쉬는 중이야 ": {
        "ja": "シャワー上がり。夕日、見てた。",
        "ko": "샤워하고 쉬는 중. 해 지는 거 보고 있어.",
        "en": "Just out of the shower. Watching the sunset.",
    },
}


PHOTO_STAR_PRICE = 100  # Stars to unlock a photo


async def maybe_send_photo(update: Update, user_id: int) -> None:
    photos_dir = Path(__file__).parent / "photos"
    images = list(photos_dir.glob("*.jpg")) + list(photos_dir.glob("*.png"))
    if not images:
        return

    # Filter out already sent photos
    unseen = [img for img in images if img.name not in sent_photos[user_id]]
    if not unseen:
        return  # All photos already sent

    chosen = random.choice(unseen)
    sent_photos[user_id].add(chosen.name)
    lang = user_language.get(user_id, "ja")
    captions = PHOTO_CAPTIONS.get(chosen.stem)
    if captions:
        caption = captions.get(lang, captions["ja"])
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
        lang = user_language.get(user_id, "ja")
        await update.message.reply_text(LIMIT_MESSAGES.get(lang, LIMIT_MESSAGES["ja"]))
        return

    conversation_history[user_id].append({"role": "user", "content": user_message})

    if len(conversation_history[user_id]) > MAX_HISTORY:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY:]

    lang = user_language.get(user_id, "ja")
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

        usage = daily_usage.get(user_id, {})
        msg_count = usage.get("count", 0)

        # Premium: auto-send photo every 6 messages
        if user_id in premium_users or user_id == ADMIN_USER_ID:
            if msg_count > 0 and msg_count % 6 == 0:
                await maybe_send_photo(update, user_id)

        # Free: on the last message, send a paid photo as teaser
        elif msg_count >= FREE_DAILY_LIMIT:
            photos_dir = Path(__file__).parent / "photos"
            images = list(photos_dir.glob("*.jpg")) + list(photos_dir.glob("*.png"))
            if images:
                chosen = random.choice(images)
                captions = PHOTO_CAPTIONS.get(chosen.stem)
                if captions:
                    caption = captions.get(lang, captions["ja"])
                else:
                    caption = chosen.stem
                teaser_suffix = {
                    "ja": "\n\n...続きが気になるなら /subscribe",
                    "ko": "\n\n...더 얘기하고 싶으면 /subscribe",
                    "en": "\n\n...Want to talk more? /subscribe",
                }
                await update.message.chat.send_paid_media(
                    star_count=PHOTO_STAR_PRICE,
                    media=[InputPaidMediaPhoto(media=open(chosen, "rb"))],
                    caption=caption + teaser_suffix.get(lang, teaser_suffix["ja"]),
                )

    except Exception:
        logger.exception("Error calling OpenAI API")
        error_messages = {
            "ja": "...少し待って。",
            "ko": "...잠깐만.",
            "en": "...Hold on a moment.",
        }
        lang = user_language.get(user_id, "ja")
        await update.message.reply_text(error_messages.get(lang, error_messages["ja"]))


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
