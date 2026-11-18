import json
import logging
import requests
import base64
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

# Состояния диалога
CHOOSING_PRODUCT  = 1
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

# ─── Расчёт размера по таблице конкретного товара ────────────────────────────

def calculate_size(chest: int, waist: int, size_table: list) -> dict:
    """
    Определяет размер по таблице товара (грудь + талия).
    Если параметры между двумя размерами — берём больший.
    Возвращает: intl, ru, note.
    """
    # 1. Точное попадание — оба параметра в диапазоне
    for row in size_table:
        if row["chest_min"] <= chest <= row["chest_max"] and \
           row["waist_min"] <= waist <= row["waist_max"]:
            return {"intl": row["intl"], "ru": row["ru"], "note": ""}

    # 2. Параметры дают разные размеры — найдём каждый отдельно
    def find_by_param(value, min_key, max_key):
        for i, row in enumerate(size_table):
            if row[min_key] <= value <= row[max_key]:
                return i
        # Если вышло за границы — ближайший
        if value < size_table[0][min_key]:
            return 0
        return len(size_table) - 1

    chest_idx = find_by_param(chest, "chest_min", "chest_max")
    waist_idx = find_by_param(waist, "waist_min", "waist_max")

    # Берём больший индекс (больший размер)
    if chest_idx >= waist_idx:
        chosen_idx = chest_idx
    else:
        chosen_idx = waist_idx

    chosen = size_table[chosen_idx]
    chest_row  = size_table[chest_idx]
    waist_row  = size_table[waist_idx]

    if chest_idx != waist_idx:
        note = (
            f"По груди → {chest_row['intl']} ({chest_row['ru']}), "
            f"по талии → {waist_row['intl']} ({waist_row['ru']}). "
            f"Рекомендуем взять больший: {chosen['intl']} ({chosen['ru']})."
        )
    else:
        note = ""

    return {"intl": chosen["intl"], "ru": chosen["ru"], "note": note}

# ─── Загрузка товаров ─────────────────────────────────────────────────────────

def load_products():
    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# ─── Парсинг WB ───────────────────────────────────────────────────────────────

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


def get_wb_product(article: str) -> dict | None:
    try:
        url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={article}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        products = data.get("data", {}).get("products", [])
        if not products:
            return None

        product   = products[0]
        name      = product.get("name", "Без названия")
        brand     = product.get("brand", "")
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

        vol    = int(article) // 100000
        part   = int(article) // 1000
        basket = get_basket_number(vol)
        image_url = (
            f"https://basket-{basket:02d}.wbbasket.ru"
            f"/vol{vol}/part{part}/{article}/images/big/1.webp"
        )

        return {
            "name": name, "brand": brand, "price": price,
            "image_url": image_url,
            "wb_url": f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
            "article": article,
            "available_sizes": available_sizes,
        }
    except Exception as e:
        logger.error(f"Ошибка парсинга WB артикул {article}: {e}")
        return None


def find_size_in_wb(available_sizes: list, ru_size: int, intl_size: str) -> dict | None:
    ru_str   = str(ru_size)
    intl_str = intl_size.upper()
    for s in available_sizes:
        name_up = s["name"].upper().strip()
        if name_up == ru_str or name_up == intl_str:
            return s
    for s in available_sizes:
        if ru_str in s["name"] or intl_str in s["name"].upper():
            return s
    return None


def build_wb_size_url(article: str, option_id) -> str:
    base = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
    return f"{base}?targetUrl=BP&size={option_id}" if option_id else base


def download_image(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        logger.error(f"Ошибка скачивания изображения: {e}")
        return None

# ─── Виртуальная примерка через Claude AI ────────────────────────────────────

def virtual_tryon(
    user_photo_bytes: bytes,
    product_image_bytes: bytes,
    product_name: str,
    size_info: dict,
    measurements: dict,
) -> str | None:
    try:
        user_b64    = base64.b64encode(user_photo_bytes).decode()
        product_b64 = base64.b64encode(product_image_bytes).decode()

        size_text = (
            f"Размер покупателя: {size_info['intl']} (российский {size_info['ru']}). "
            f"Параметры: грудь {measurements['chest']} см, "
            f"талия {measurements['waist']} см, "
            f"рост {measurements['height']} см, вес {measurements['weight']} кг."
        )

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
                            f"Первое — фото человека, второе — товар '{product_name}' с Wildberries. "
                            f"{size_text} "
                            f"Опиши подробно и позитивно, как этот товар будет смотреться на этом человеке: "
                            f"подходит ли по стилю, цвету, пропорциям. "
                            f"Прокомментируй посадку с учётом размера и параметров. "
                            f"Дай 1-2 совета по сочетанию с другой одеждой. "
                            f"Пиши по-русски, дружелюбно, как личный стилист. Не более 5 предложений."
                        )
                    },
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": user_b64}
                    },
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": product_b64}
                    },
                ]
            }]
        }

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json=payload, headers=headers, timeout=30
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    except Exception as e:
        logger.error(f"Ошибка виртуальной примерки: {e}")
        return None

# ─── Вспомогательные функции ─────────────────────────────────────────────────

async def send_catalog(message, context):
    products = load_products()
    keyboard = [
        [InlineKeyboardButton(f"👗 {p['name']}", callback_data=f"product_{p['id']}")]
        for p in products
    ]
    await message.reply_text(
        "👇 Выбери товар из каталога:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "👋 Привет! Я помогу примерить одежду виртуально и подобрать правильный размер.\n\n"
        "👇 Выбери товар из каталога:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"👗 {p['name']}", callback_data=f"product_{p['id']}")]
            for p in load_products()
        ])
    )
    return CHOOSING_PRODUCT


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.replace("product_", ""))
    catalog    = load_products()
    cat_item   = next((p for p in catalog if p["id"] == product_id), None)
    if not cat_item:
        await query.message.reply_text("❌ Товар не найден.")
        return CHOOSING_PRODUCT

    await query.message.reply_text("⏳ Загружаю данные с Wildberries...")
    wb_data = get_wb_product(cat_item["article"])

    if not wb_data:
        await query.message.reply_text(
            f"⚠️ Не удалось загрузить данные для артикула {cat_item['article']}.\n"
            "Проверь артикул в products.json"
        )
        return CHOOSING_PRODUCT

    # Объединяем данные WB + размерная сетка из products.json
    wb_data["size_table"] = cat_item.get("size_table", [])
    context.user_data["selected_product"] = wb_data

    sizes_text = ""
    if wb_data["available_sizes"]:
        s_list = ", ".join(s["name"] for s in wb_data["available_sizes"])
        sizes_text = f"📐 Доступные размеры: {s_list}\n"

    # Показываем размерную сетку товара
    size_table_text = ""
    if cat_item.get("size_table"):
        rows = []
        for row in cat_item["size_table"]:
            rows.append(
                f"  {row['intl']} ({row['ru']}) — "
                f"грудь {row['chest_min']}–{row['chest_max']}, "
                f"талия {row['waist_min']}–{row['waist_max']}"
            )
        size_table_text = "\n📏 *Размерная сетка товара:*\n" + "\n".join(rows) + "\n"

    img_bytes = download_image(wb_data["image_url"])
    caption = (
        f"🛍 *{wb_data['brand']} — {wb_data['name']}*\n"
        f"💰 Цена: *{wb_data['price']} ₽*\n"
        f"{sizes_text}"
        f"{size_table_text}\n"
        f"Введи свой *рост* (в см), например: `170`"
    )

    keyboard = [
        [InlineKeyboardButton("🛒 Купить на WB (без примерки)", url=wb_data["wb_url"])],
        [InlineKeyboardButton("◀️ Назад к каталогу", callback_data="back_to_catalog")],
    ]

    if img_bytes:
        await query.message.reply_photo(
            photo=BytesIO(img_bytes),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await query.message.reply_text(
            caption, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return ASKING_HEIGHT


async def ask_height(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        height = int(update.message.text.strip())
        if not (100 <= height <= 220):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи рост числом от 100 до 220 см, например: `170`",
            parse_mode="Markdown"
        )
        return ASKING_HEIGHT

    context.user_data["height"] = height
    await update.message.reply_text(
        "Теперь напиши свой *вес* (в кг), например: `65`",
        parse_mode="Markdown"
    )
    return ASKING_WEIGHT


async def ask_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        weight = int(update.message.text.strip())
        if not (30 <= weight <= 250):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи вес числом от 30 до 250 кг, например: `65`",
            parse_mode="Markdown"
        )
        return ASKING_WEIGHT

    context.user_data["weight"] = weight
    await update.message.reply_text(
        "📏 Введи обхват *груди* (в см), например: `88`\n\n"
        "_Измеряй по самому выступающему месту._",
        parse_mode="Markdown"
    )
    return ASKING_CHEST


async def ask_chest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chest = int(update.message.text.strip())
        if not (60 <= chest <= 160):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи обхват груди числом от 60 до 160 см.",
            parse_mode="Markdown"
        )
        return ASKING_CHEST

    context.user_data["chest"] = chest
    await update.message.reply_text(
        "📏 Введи обхват *талии* (в см), например: `72`\n\n"
        "_Самое узкое место живота._",
        parse_mode="Markdown"
    )
    return ASKING_WAIST


async def ask_waist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        waist = int(update.message.text.strip())
        if not (50 <= waist <= 160):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Введи обхват талии числом от 50 до 160 см.",
            parse_mode="Markdown"
        )
        return ASKING_WAIST

    context.user_data["waist"] = waist
    chest   = context.user_data["chest"]
    product = context.user_data.get("selected_product", {})

    # Расчёт по размерной сетке ЭТОГО товара
    size_table = product.get("size_table", [])
    if not size_table:
        await update.message.reply_text(
            "⚠️ У этого товара нет размерной сетки в products.json. "
            "Добавь её и перезапусти бота."
        )
        return CHOOSING_PRODUCT

    size_info = calculate_size(chest, waist, size_table)
    context.user_data["size_info"]    = size_info
    context.user_data["measurements"] = {
        "height": context.user_data["height"],
        "weight": context.user_data["weight"],
        "chest":  chest,
        "waist":  waist,
    }

    # Ищем размер на WB
    matched_size = find_size_in_wb(
        product.get("available_sizes", []),
        size_info["ru"],
        size_info["intl"]
    )
    context.user_data["matched_size"] = matched_size

    if matched_size:
        buy_url     = build_wb_size_url(product["article"], matched_size.get("option_id"))
        size_status = f"✅ Размер *{matched_size['name']}* есть в наличии!"
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
    buy_url      = context.user_data.get("buy_url", product.get("wb_url", "") if product else "")
    matched_size = context.user_data.get("matched_size")

    if not product or not size_info:
        await update.message.reply_text("⚠️ Сначала выбери товар. Нажми /start")
        return CHOOSING_PRODUCT

    await update.message.reply_text("✨ Анализирую образ, подожди несколько секунд...")

    photo       = update.message.photo[-1]
    photo_file  = await photo.get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    product_img_bytes = download_image(product["image_url"])
    if not product_img_bytes:
        await update.message.reply_text("❌ Не удалось загрузить фото товара. Попробуй ещё раз.")
        return WAITING_FOR_PHOTO

    analysis = virtual_tryon(
        bytes(photo_bytes), product_img_bytes,
        product["name"], size_info, measurements
    )

    size_line = (
        f"📐 Твой размер: *{size_info['intl']} ({size_info['ru']})*"
        + (" — в наличии ✅" if matched_size else " — нет в наличии ⚠️")
    )
    btn_label = (
        f"🛒 Купить размер {matched_size['name'] if matched_size else size_info['intl']} на WB"
    )

    await update.message.reply_text(
        f"👗 *{product['brand']} — {product['name']}*\n"
        f"{size_line}\n\n"
        f"{analysis if analysis else '⚠️ Не удалось выполнить анализ образа, но размер подобран!'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_label, url=buy_url)],
            [InlineKeyboardButton("👗 Выбрать другой товар", callback_data="back_to_catalog")],
        ])
    )
    return CHOOSING_PRODUCT


async def back_to_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await send_catalog(query.message, context)
    return CHOOSING_PRODUCT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Сброс. Нажми /start чтобы начать заново.")
    return ConversationHandler.END

# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_PRODUCT: [
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
