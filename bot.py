import os
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

from support import (
    MAIN_MENU, SUPPORT, manager_id, home_row, send_main_menu,
    go_main_menu, ask_support, end_support, client_message,
    manager_message, manager_reply_button, manager_close_button, manager_close, id_command,
)

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN         = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
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
logging.getLogger("httpx").setLevel(logging.WARNING)  # без токена в логах

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


# ─── Загрузка size_ids из CSV на GitHub ──────────────────────────────────────

GITHUB_CSV_URL = "https://raw.githubusercontent.com/BMXMAX86/wb-tryon-bot/main/size_ids.csv"

_size_ids_cache = {}
_size_ids_last_fetch = 0

def load_size_ids() -> dict:
    """
    Читает size_ids.csv с GitHub и возвращает словарь:
    { "982153895": {"S": 123, "M": 456, ...}, ... }
    Кэшируется на 5 минут чтобы не грузить GitHub при каждом сообщении.
    """
    import time
    global _size_ids_cache, _size_ids_last_fetch

    now = time.time()
    if _size_ids_cache and (now - _size_ids_last_fetch) < 300:
        return _size_ids_cache

    try:
        resp = requests.get(GITHUB_CSV_URL, timeout=8)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        if not lines:
            return _size_ids_cache

        headers = [h.strip() for h in lines[0].split(",")]
        sizes   = headers[2:]  # S, M, L, XL, XXL и любые новые

        result = {}
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) < 2:
                continue
            article = parts[0].strip()
            ids = {}
            for i, size in enumerate(sizes):
                val = parts[2 + i].strip() if (2 + i) < len(parts) else ""
                if val and val.isdigit():
                    ids[size] = int(val)
            result[article] = ids

        _size_ids_cache     = result
        _size_ids_last_fetch = now
        logger.info(f"size_ids загружены: {len(result)} артикулов")
        return result

    except Exception as e:
        logger.error(f"Ошибка загрузки size_ids.csv: {e}")
        return _size_ids_cache  # возвращаем старый кэш если есть

# ─── WB API ───────────────────────────────────────────────────────────────────

STOCK_CSV_URL = "https://raw.githubusercontent.com/BMXMAX86/wb-tryon-bot/main/stock.csv"
WB_API_TOKEN  = os.environ.get("WB_API_TOKEN", "")

_manual_stock_cache = {}
_manual_stock_fetch = 0
_seller_stock_cache = {}
_seller_stock_fetch = 0


def load_manual_stock() -> dict:
    """
    stock.csv на GitHub: article,name,S,M,L,XL,XXL — 1 есть, 0 нет, пусто = неизвестно.
    Возвращает { "артикул": {"S": True, "M": False, ...} }. Кэш 2 минуты.
    """
    import time
    global _manual_stock_cache, _manual_stock_fetch
    now = time.time()
    if _manual_stock_cache and (now - _manual_stock_fetch) < 120:
        return _manual_stock_cache
    try:
        resp = requests.get(STOCK_CSV_URL, timeout=8)
        resp.raise_for_status()
        lines = resp.text.strip().splitlines()
        headers = [h.strip() for h in lines[0].split(",")]
        sizes = headers[2:]
        result = {}
        for line in lines[1:]:
            parts = [x.strip() for x in line.split(",")]
            if not parts or not parts[0]:
                continue
            row = {}
            for i, size in enumerate(sizes):
                val = parts[2 + i] if (2 + i) < len(parts) else ""
                if val in ("0", "1"):
                    row[size] = (val == "1")
            result[parts[0]] = row
        _manual_stock_cache = result
        _manual_stock_fetch = now
        return result
    except Exception as e:
        logger.error(f"Ошибка загрузки stock.csv: {e}")
        return _manual_stock_cache


def load_seller_stock() -> dict:
    """
    Остатки из официального API продавца WB (нужна переменная WB_API_TOKEN).
    Возвращает { "nmId": {"S": количество, ...} }. Кэш 10 минут (лимит API — 1 запрос в минуту).
    """
    import time
    global _seller_stock_cache, _seller_stock_fetch
    if not WB_API_TOKEN:
        return {}
    now = time.time()
    if _seller_stock_fetch and (now - _seller_stock_fetch) < 600:
        return _seller_stock_cache
    try:
        resp = requests.get(
            "https://statistics-api.wildberries.ru/api/v1/supplier/stocks",
            params={"dateFrom": "2019-06-20"},
            headers={"Authorization": WB_API_TOKEN},
            timeout=20,
        )
        resp.raise_for_status()
        result = {}
        for item in resp.json():
            nm   = str(item.get("nmId", ""))
            size = str(item.get("techSize", "")).strip().upper()
            qty  = int(item.get("quantity", 0) or 0)
            result.setdefault(nm, {})
            result[nm][size] = result[nm].get(size, 0) + qty
        _seller_stock_cache = result
        _seller_stock_fetch = now
        logger.info(f"Остатки из API продавца: {len(result)} артикулов")
        return result
    except Exception as e:
        logger.error(f"Ошибка API продавца WB: {e}")
        _seller_stock_fetch = now - 480  # повторить через 2 минуты
        return _seller_stock_cache


def get_size_availability(article: str, size: str):
    """True — есть, False — нет, None — неизвестно. Сначала API продавца, потом stock.csv."""
    seller = load_seller_stock()
    if article in seller:
        return seller[article].get(size.strip().upper(), 0) > 0
    manual = load_manual_stock().get(article, {})
    if size in manual:
        return manual[size]
    return None

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


GITHUB_PHOTOS_URL = "https://raw.githubusercontent.com/BMXMAX86/wb-tryon-bot/main/photos"

def get_github_photo(article, n=1):
    """Фото товара из папки photos на GitHub: артикул_номер.webp/jpg/png"""
    for ext in ["webp", "jpg", "jpeg", "png"]:
        try:
            r = requests.get(f"{GITHUB_PHOTOS_URL}/{article}_{n}.{ext}", timeout=8)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception as e:
            logger.error(f"Фото {article}_{n}.{ext}: {e}")
    return None


def image_media_type(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"

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
                    {"type": "image", "source": {"type": "base64", "media_type": image_media_type(user_photo_bytes),
                                                  "data": base64.b64encode(user_photo_bytes).decode()}},
                    {"type": "image", "source": {"type": "base64", "media_type": image_media_type(product_image_bytes),
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

    bot = context.bot
    if is_callback:
        chat_id = update_or_message.message.chat_id
    else:
        chat_id = update_or_message.effective_chat.id

    await bot.send_message(chat_id=chat_id, text="Вот наши топы 💛 Нажми «Примерить» под понравившимся — подберу размер и покажу, как он сядет ✨", parse_mode="Markdown")

    for p in products:
        article     = p["article"]
        photo_nums  = p.get("photos", [1])
        wb_url      = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        keyboard    = InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Примерить этот топ", callback_data=f"product_{p['id']}")],
            [InlineKeyboardButton("🛍 Смотреть на Wildberries", url=wb_url)],
        ])

        # Скачиваем все доступные фото из папки photos на GitHub
        media_items = []
        for i, n in enumerate(photo_nums):
            img_data = get_github_photo(article, n)
            if img_data:
                media_items.append((BytesIO(img_data), i == 0))

        if media_items:
            if len(media_items) == 1:
                # Одно фото — с кнопками
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=media_items[0][0],
                    caption=f"*{p['name']}*",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            else:
                # Галерея — несколько фото через media group, кнопки отдельно
                from telegram import InputMediaPhoto
                media_group = [
                    InputMediaPhoto(
                        media=item[0],
                        caption=f"*{p['name']}*" if item[1] else None,
                        parse_mode="Markdown" if item[1] else None
                    )
                    for item in media_items
                ]
                await bot.send_media_group(chat_id=chat_id, media=media_group)
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"👆 *{p['name']}*",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
        else:
            # Нет фото — только текст с кнопками
            await bot.send_message(
                chat_id=chat_id,
                text=f"*{p['name']}*",
                parse_mode="Markdown",
                reply_markup=keyboard
            )

    await bot.send_message(
        chat_id=chat_id,
        text="Выбери топ выше 👆 или вернись в меню",
        reply_markup=InlineKeyboardMarkup([home_row()])
    )

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    # Приветствие с логотипом и главное меню
    await send_main_menu(context.bot, update.effective_chat.id, context, with_logo=True)
    return MAIN_MENU


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
            [InlineKeyboardButton("🔙 Выбрать другой топ", callback_data="back_to_catalog")],
            home_row(),
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

    # Ссылка на размер + проверка наличия (API продавца / stock.csv)
    all_size_ids = load_size_ids()
    article      = product["article"]
    base_url     = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
    sizes_order  = [row["intl"] for row in size_table]
    wanted       = size_info["intl"]

    def size_url(art, size_name):
        sid  = all_size_ids.get(art, {}).get(size_name)
        page = f"https://www.wildberries.ru/catalog/{art}/detail.aspx"
        return f"{page}?size={sid}" if sid else page

    avail      = {s: get_size_availability(article, s) for s in sizes_order}
    state      = avail.get(wanted)
    buy_url    = size_url(article, wanted)
    buy_label  = f"💛 Купить {wanted} на Wildberries"
    extra_rows = []

    if state is True:
        size_status = f"✅ Размер *{wanted} ({size_info['ru']})* есть в наличии!"

    elif state is False:
        idx      = sizes_order.index(wanted) if wanted in sizes_order else 0
        in_stock = [s for s in sizes_order if avail.get(s) is True]
        # ближайший размер в наличии; при равном расстоянии — больший
        in_stock.sort(key=lambda s: (abs(sizes_order.index(s) - idx), 0 if sizes_order.index(s) > idx else 1))

        if in_stock:
            alt       = in_stock[0]
            buy_url   = size_url(article, alt)
            buy_label = f"💛 Купить {alt} на Wildberries"
            have      = ", ".join(s for s in sizes_order if avail.get(s) is True)
            size_status = (
                f"⚠️ Размер *{wanted}* на этой модели закончился.\n"
                f"Есть: *{have}*. Ближайший к твоим меркам — *{alt}*."
            )
        else:
            buy_url   = base_url
            buy_label = "🛍 Открыть товар на Wildberries"
            size_status = f"❌ На этой модели размер *{wanted}* и остальные размеры сейчас закончились."

        # другие модели, где нужный размер есть в наличии
        for p2 in load_products():
            if p2["article"] == article:
                continue
            if get_size_availability(p2["article"], wanted) is True:
                extra_rows.append([InlineKeyboardButton(
                    f"🔁 {p2['name'][:40]} — {wanted}",
                    url=size_url(p2["article"], wanted)
                )])
            if len(extra_rows) >= 3:
                break
        if extra_rows:
            size_status += f"\n\nМожно выбрать другую модель в размере *{wanted}* 👇"

    else:
        size_status = (
            f"🔗 Размер *{wanted} ({size_info['ru']})* — наличие проверить не удалось, "
            f"на странице WB выбери доступный размер."
        )

    context.user_data["buy_label"]  = buy_label
    context.user_data["extra_rows"] = extra_rows
    context.user_data["buy_url"] = buy_url
    note_text = f"\n\n💡 _{size_info['note']}_" if size_info.get("note") else ""

    await update.message.reply_text(
        f"📐 *Твой размер: {size_info['intl']} (российский {size_info['ru']})*\n"
        f"{size_status}{note_text}\n\n"
        f"📸 Отправь своё фото для виртуальной примерки!\n"
        f"_Лучше всего — фото в полный рост на светлом фоне._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(buy_label, url=buy_url)],
            *extra_rows,
            [InlineKeyboardButton("🔙 Выбрать другой топ", callback_data="back_to_catalog")],
            home_row(),
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
    product_img_bytes = get_github_photo(product["article"], 1) or download_image(product["image_url"])

    if not product_img_bytes:
        await update.message.reply_text("❌ Не удалось загрузить фото товара.")
        return WAITING_FOR_PHOTO

    analysis = virtual_tryon(bytes(photo_bytes), product_img_bytes, product["name"], size_info, measurements)

    size_line = f"📐 Твой размер: *{size_info['intl']} ({size_info['ru']})*"

    await update.message.reply_text(
        f"👗 *{product['name']}*\n{size_line}\n\n"
        f"{analysis if analysis else '⚠️ Анализ недоступен. Но размер подобран — жми Купить!'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                context.user_data.get("buy_label", f"💛 Купить размер {size_info['intl']} на Wildberries"),
                url=buy_url
            )],
            *context.user_data.get("extra_rows", []),
            [InlineKeyboardButton("✨ Подобрать другой топ", callback_data="back_to_catalog")],
            home_row(),
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
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан: добавь переменную BOT_TOKEN в Variables на Railway")
    app = Application.builder().token(BOT_TOKEN).build()

    common = [
        CallbackQueryHandler(go_main_menu, pattern="^menu_main$"),
        CallbackQueryHandler(back_to_catalog, pattern="^menu_catalog$"),
        CallbackQueryHandler(ask_support, pattern="^menu_support$"),
        CallbackQueryHandler(end_support, pattern="^support_done$"),
    ]
    back = CallbackQueryHandler(back_to_catalog, pattern="^back_to_catalog$")

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)] + common + [back],
        states={
            MAIN_MENU: common,
            BROWSING_CATALOG: [CallbackQueryHandler(show_product, pattern=r"^product_\d+$"), back] + common,
            ASKING_HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_height), back] + common,
            ASKING_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_weight), back] + common,
            ASKING_CHEST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_chest), back] + common,
            ASKING_WAIST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_waist), back] + common,
            WAITING_FOR_PHOTO: [
                MessageHandler(filters.PHOTO, handle_user_photo),
                CallbackQueryHandler(show_product, pattern=r"^product_\d+$"),
                back,
            ] + common,
            SUPPORT: [MessageHandler(~filters.COMMAND & filters.ChatType.PRIVATE, client_message)] + common,
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    mgr = manager_id()
    if mgr:
        app.add_handler(CallbackQueryHandler(manager_reply_button, pattern=r"^mgr_reply:\d+$"), group=-1)
        app.add_handler(CallbackQueryHandler(manager_close_button, pattern=r"^mgr_close:\d+$"), group=-1)
        app.add_handler(MessageHandler(filters.Chat(chat_id=mgr) & ~filters.COMMAND, manager_message), group=-1)
        app.add_handler(CommandHandler("close", manager_close, filters=filters.Chat(chat_id=mgr) & filters.REPLY), group=-1)
    else:
        logger.warning("MANAGER_CHAT_ID не задан — связь с менеджером отключена")
    app.add_handler(CommandHandler("id", id_command))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
