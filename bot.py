import json
import logging
import requests
import base64
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN         = "8662684328:AAGNUtRqxmHOlKPvBGnUtSHXYFpzmHTAMyw"
ANTHROPIC_API_KEY = "ВСТАВЬ_СВОЙ_ANTHROPIC_API_KEY_СЮДА"
PRODUCTS_FILE     = "products.json"

# Фото бренда для приветствия (ссылка на фото с WB или прямая ссылка)
# Используем первый товар как обложку бренда
BRAND_COVER_ARTICLE = "982153895"

# Состояния диалога
BROWSING_CATALOG  = 1
ASKING_HEIGHT     = 2
ASKING_WEIGHT     = 3
ASKING_CHEST      = 4
ASKING_WAIST      = 5
WAITING_FOR_PHOTO = 6

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Размерная сетка ─────────────────────────────────────────────────────────

def calculate_size(chest: int, waist: int, size_table: list) -> dict:
    for row in size_table:
        if row["chest_min"] <= chest <= row["chest_max"] and \
           row["waist_min"] <= waist <= row["waist_max"]:
            return {"intl": row["intl"], "ru": row["ru"], "note": ""}

    def find_idx(value, min_key, max_key):
        for i, row in enumerate(size_table):
            if row[min_key] <= value <= row[max_key]:
                return i
        return len(size_table) - 1 if value > size_table[-1][max_key] else 0

    ci = find_idx(chest, "chest_min", "chest_max")
    wi = find_idx(waist, "waist_min", "waist_max")
    idx = max(ci, wi)
    chosen = size_table[idx]

    note = ""
    if ci != wi:
        note = (
            f"По груди → {size_table[ci]['intl']} ({size_table[ci]['ru']}), "
            f"по талии → {size_table[wi]['intl']} ({size_table[wi]['ru']}). "
            f"Рекомендуем взять больший: {chosen['intl']} ({chosen['ru']})."
        )
    return {"intl": chosen["intl"], "ru": chosen["ru"], "note": note}

# ─── Загрузка товаров ─────────────────────────────────────────────────────────

def load_products():
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# ─── WB API ───────────────────────────────────────────────────────────────────

def get_basket_number(vol: int) -> int:
    if vol <= 143:    return 1
    elif vol <= 287:  return 2
    elif vol <= 431:  return 3
    elif vol <= 719:  return 4
    elif vol <= 1007: return 5
    elif vol <= 1061: return 6
    elif vol <= 1115: return 7
    elif vol <= 1169: return 8
    elif vol <= 1313: return 9
    elif vol <= 1601: return 10
    elif vol <= 1655: return 11
    elif vol <= 1919: return 12
    elif vol <= 2045: return 13
    elif vol <= 2189: return 14
    elif vol <= 2405: return 15
    elif vol <= 2621: return 16
    elif vol <= 2837: return 17
    else:             return 18


def get_wb_image_url(article: str) -> str:
    """Формирует URL фото товара на WB."""
    vol    = int(article) // 100000
    part   = int(article) // 1000
    basket = get_basket_number(vol)
    return (
        f"https://basket-{basket:02d}.wbbasket.ru"
        f"/vol{vol}/part{part}/{article}/images/big/1.webp"
    )


def get_wb_product(article: str) -> dict | None:
    try:
        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={article}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Origin": "https://www.wildberries.ru",
            "Referer": "https://www.wildberries.ru/",
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        product   = products[0]
        name      = product.get("name", "Без названия")
        brand     = product.get("brand", "Easy Style")
        sizes_raw = product.get("sizes", [])

        available_sizes = []
        for s in sizes_raw:
            orig_name = s.get("origName", "").strip()
            size_name = s.get("name", "").strip()
            stocks    = s.get("stocks", [])
            in_stock  = any(st.get("qty", 0) > 0 for st in stocks)
            if in_stock:
                available_sizes.append({
                    "name":      orig_name or size_name,
                    "option_id": s.get("optionId"),
                })

        price_data = sizes_raw[0].get("price", {}) if sizes_raw else {}
        price      = price_data.get("total", 0) // 100

        return {
            "name": name, "brand": brand, "price": price,
            "image_url": get_wb_image_url(article),
            "wb_url": f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
            "article": article,
            "available_sizes": available_sizes,
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга WB артикул {article}: {e}")
        return None


def find_size_in_wb(available_sizes, ru_size, intl_size):
    ru_str, intl_str = str(ru_size), intl_size.upper()
    for s in available_sizes:
        if s["name"].upper().strip() in (ru_str, intl_str):
            return s
    for s in available_sizes:
        if ru_str in s["name"] or intl_str in s["name"].upper():
            return s
    return None


def build_wb_size_url(article, option_id):
    base = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
    return f"{base}?targetUrl=BP&size={option_id}" if option_id else base


def download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return None

# ─── Виртуальная примерка ─────────────────────────────────────────────────────

def virtual_tryon(user_photo_bytes, product_image_bytes, product_name, size_info, measurements):
    try:
        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Посмотри на эти два изображения. "
                            f"Первое — фото человека, второе — товар '{product_name}'. "
                            f"Размер: {size_info['intl']} (российский {size_info['ru']}). "
                            f"Параметры: грудь {measurements['chest']} см, талия {measurements['waist']} см, "
                            f"рост {measurements['height']} см, вес {measurements['weight']} кг. "
                            f"Опиши позитивно как товар будет смотреться, прокомментируй посадку, "
                            f"дай 1-2 совета по сочетанию. По-русски, как стилист. Не более 5 предложений."
                        )
                    },
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                  "data": base64.b64encode(user_photo_bytes).decode()}},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                  "data": base64.b64encode(product_image_bytes).decode()}},
                ]
            }]
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]
    except Exception as e:
        logger.error(f"Ошибка примерки: {e}")
        return None

# ─── Отправка каталога (каждый товар = карточка с фото) ──────────────────────

async def send_catalog(update_or_message, context, is_callback=False):
    """Отправляет каждый товар как отдельную карточку с фото."""
    products = load_products()

    if is_callback:
        chat_id = update_or_message.message.chat_id
        bot     = update_or_message.message.bot
    else:
        chat_id = update_or_message.effective_chat.id
        bot     = context.bot

    await bot.send_message(chat_id=chat_id, text="👇 Выбери товар — нажми *Примерить* под карточкой:", parse_mode="Markdown")

    for p in products:
        article   = p["article"]
        photo_url = p.get("photo_url", "")
        wb_url    = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        keyboard  = InlineKeyboardMarkup([
            [InlineKeyboardButton("👗 Примерить", callback_data=f"product_{p['id']}")],
            [InlineKeyboardButton("🛒 Купить на WB", url=wb_url)],
        ])
        caption = f"*{p['name']}*"

        sent = False
        if photo_url:
            try:
                img_resp = requests.get(photo_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Referer": "https://www.wildberries.ru/",
                }, timeout=10)
                if img_resp.status_code == 200 and len(img_resp.content) > 1000:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=BytesIO(img_resp.content),
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                    sent = True
            except Exception as e:
                logger.error(f"Ошибка фото {article}: {e}")

        if not sent:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="Markdown",
                reply_markup=keyboard
            )

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    # Приветственное фото — берём из первого товара в products.json
    cover_img = None
    try:
        first_product = load_products()[0]
        cover_url = first_product.get("photo_url", "")
        if cover_url:
            r = requests.get(cover_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://www.wildberries.ru/",
            }, timeout=10)
            if r.status_code == 200 and len(r.content) > 1000:
                cover_img = r.content
    except Exception:
        pass

    welcome_text = (
        "👋 Привет! Я бот бренда *Easy Style*.\n\n"
        "Помогу тебе:\n"
        "📐 Подобрать правильный размер\n"
        "👗 Виртуально примерить топ\n"
        "🛒 Перейти к покупке на Wildberries\n\n"
        "Выбери топ из каталога ниже 👇"
    )

    if cover_img:
        await update.message.reply_photo(
            photo=BytesIO(cover_img),
            caption=welcome_text,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(welcome_text, parse_mode="Markdown")

    await send_catalog(update, context, is_callback=False)
    return BROWSING_CATALOG


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает сбор параметров после нажатия «Примерить»."""
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.replace("product_", ""))
    catalog    = load_products()
    cat_item   = next((p for p in catalog if p["id"] == product_id), None)
    if not cat_item:
        await query.message.reply_text("❌ Товар не найден.")
        return BROWSING_CATALOG

    # Получаем данные с WB
    wb_data = get_wb_product(cat_item["article"])
    if not wb_data:
        wb_data = {
            "name": cat_item["name"], "brand": "Easy Style", "price": 0,
            "image_url": get_wb_image_url(cat_item["article"]),
            "wb_url": f"https://www.wildberries.ru/catalog/{cat_item['article']}/detail.aspx",
            "article": cat_item["article"], "available_sizes": [],
        }

    wb_data["size_table"] = cat_item.get("size_table", [])
    context.user_data["selected_product"] = wb_data

    price_text = f"💰 Цена: *{wb_data['price']} ₽*\n" if wb_data["price"] else ""
    sizes_text = ""
    if wb_data["available_sizes"]:
        s_list = ", ".join(s["name"] for s in wb_data["available_sizes"])
        sizes_text = f"📦 В наличии: {s_list}\n"

    await query.message.reply_text(
        f"✅ Ты выбрала: *{wb_data['name']}*\n"
        f"{price_text}"
        f"{sizes_text}\n"
        f"📏 Подберём твой размер!\n\n"
        f"Введи свой *рост* (в см), например: `170`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад к каталогу", callback_data="back_to_catalog")],
        ])
    )
    return ASKING_HEIGHT


async def ask_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        height = int(update.message.text.strip())
        if not (100 <= height <= 220): raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи рост числом от 100 до 220 см, например: `170`", parse_mode="Markdown")
        return ASKING_HEIGHT
    context.user_data["height"] = height
    await update.message.reply_text("Теперь напиши свой *вес* (в кг), например: `65`", parse_mode="Markdown")
    return ASKING_WEIGHT


async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = int(update.message.text.strip())
        if not (30 <= weight <= 250): raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи вес числом от 30 до 250 кг, например: `65`", parse_mode="Markdown")
        return ASKING_WEIGHT
    context.user_data["weight"] = weight
    await update.message.reply_text(
        "📏 Введи обхват *груди* (в см), например: `88`\n\n_Измеряй по самому выступающему месту._",
        parse_mode="Markdown"
    )
    return ASKING_CHEST


async def ask_chest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chest = int(update.message.text.strip())
        if not (60 <= chest <= 160): raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи обхват груди числом от 60 до 160 см.", parse_mode="Markdown")
        return ASKING_CHEST
    context.user_data["chest"] = chest
    await update.message.reply_text(
        "📏 Введи обхват *талии* (в см), например: `72`\n\n_Самое узкое место живота._",
        parse_mode="Markdown"
    )
    return ASKING_WAIST


async def ask_waist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        waist = int(update.message.text.strip())
        if not (50 <= waist <= 160): raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введи обхват талии числом от 50 до 160 см.", parse_mode="Markdown")
        return ASKING_WAIST

    context.user_data["waist"] = waist
    chest   = context.user_data["chest"]
    product = context.user_data.get("selected_product", {})

    size_table = product.get("size_table", [])
    if not size_table:
        await update.message.reply_text("⚠️ У этого товара нет размерной сетки.")
        return BROWSING_CATALOG

    size_info = calculate_size(chest, waist, size_table)
    context.user_data["size_info"]    = size_info
    context.user_data["measurements"] = {
        "height": context.user_data["height"],
        "weight": context.user_data["weight"],
        "chest": chest, "waist": waist,
    }

    matched = find_size_in_wb(product.get("available_sizes", []), size_info["ru"], size_info["intl"])
    context.user_data["matched_size"] = matched

    if matched:
        buy_url     = build_wb_size_url(product["article"], matched.get("option_id"))
        size_status = f"✅ Размер *{matched['name']}* есть в наличии!"
    else:
        buy_url   = product.get("wb_url", "")
        avail     = ", ".join(s["name"] for s in product.get("available_sizes", []))
        size_status = (
            f"⚠️ Размера *{size_info['ru']} ({size_info['intl']})* сейчас нет.\n"
            f"Доступны: {avail or 'уточни на сайте'}"
        )

    context.user_data["buy_url"] = buy_url
    note_text = f"\n\n💡 _{size_info['note']}_" if size_info.get("note") else ""

    await update.message.reply_text(
        f"📐 *Твой размер: {size_info['intl']} (российский {size_info['ru']})*\n"
        f"{size_status}{note_text}\n\n"
        f"📸 Отправь своё фото для виртуальной примерки!\n"
        f"_Лучше всего — фото в полный рост на светлом фоне._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Купить без примерки", url=buy_url)],
            [InlineKeyboardButton("◀️ Назад к каталогу", callback_data="back_to_catalog")],
        ])
    )
    return WAITING_FOR_PHOTO


async def handle_user_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product      = context.user_data.get("selected_product")
    size_info    = context.user_data.get("size_info")
    measurements = context.user_data.get("measurements")
    buy_url      = context.user_data.get("buy_url", "")
    matched      = context.user_data.get("matched_size")

    if not product or not size_info:
        await update.message.reply_text("⚠️ Сначала выбери товар. Нажми /start")
        return BROWSING_CATALOG

    await update.message.reply_text("✨ Анализирую образ, подожди несколько секунд...")

    photo_bytes       = await (await update.message.photo[-1].get_file()).download_as_bytearray()
    product_img_bytes = download_image(product["image_url"])

    if not product_img_bytes:
        await update.message.reply_text("❌ Не удалось загрузить фото товара.")
        return WAITING_FOR_PHOTO

    analysis = virtual_tryon(bytes(photo_bytes), product_img_bytes, product["name"], size_info, measurements)

    size_line = (
        f"📐 Твой размер: *{size_info['intl']} ({size_info['ru']})*"
        + (" — в наличии ✅" if matched else " — нет в наличии ⚠️")
    )

    await update.message.reply_text(
        f"👗 *{product['name']}*\n{size_line}\n\n"
        f"{analysis if analysis else '⚠️ Анализ недоступен. Но размер подобран — жми Купить!'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"🛒 Купить размер {matched['name'] if matched else size_info['intl']} на WB",
                url=buy_url
            )],
            [InlineKeyboardButton("👗 Выбрать другой товар", callback_data="back_to_catalog")],
        ])
    )
    return BROWSING_CATALOG


async def back_to_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await send_catalog(query, context, is_callback=True)
    return BROWSING_CATALOG


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сброс. Нажми /start чтобы начать заново.")
    return ConversationHandler.END

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BROWSING_CATALOG: [
                CallbackQueryHandler(show_product, pattern=r"^product_\d+$"),
                CallbackQueryHandler(back_to_catalog, pattern="^back_to_catalog$"),
            ],
            ASKING_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_height)],
            ASKING_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_weight)],
            ASKING_CHEST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_chest)],
            ASKING_WAIST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_waist)],
            WAITING_FOR_PHOTO: [
                MessageHandler(filters.PHOTO, handle_user_photo),
                CallbackQueryHandler(back_to_catalog, pattern="^back_to_catalog$"),
                CallbackQueryHandler(show_product, pattern=r"^product_\d+$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
