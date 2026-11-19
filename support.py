import os
import re
import logging
from io import BytesIO

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ID чата менеджеров (группа или личный чат) — переменная MANAGER_CHAT_ID на Railway
MANAGER_CHAT_ID = os.environ.get("MANAGER_CHAT_ID", "").strip()

# Состояния диалога
MAIN_MENU = 20
SUPPORT   = 21

# Логотип лежит в корне репозитория (подойдёт любое из этих имён)
LOGO_BASE_URL = "https://raw.githubusercontent.com/BMXMAX86/wb-tryon-bot/main/"
LOGO_FILES    = ["logo.jpg", "logo.png", "logo.jpeg", "logo.webp", "IMG_6447.JPG", "IMG_6447.jpg"]

WELCOME_TEXT = (
    "Привет! 👋 Я бот бренда *Easy Style* 💛\n\n"
    "Помогу подобрать размер топа и сразу дам ссылку на Wildberries. "
    "А если что-то пойдёт не так с заказом или нужно задать вопрос о ваших данных — "
    "нажмите «💬 Вопрос по заказу», и подключится менеджер.\n\n"
    "Что хочешь сделать? 👇"
)
MENU_TEXT = "Что хочешь сделать дальше? 👇"

SUPPORT_PROMPT = (
    "Расскажи, что случилось 💬\n\n"
    "Напиши вопрос одним или несколькими сообщениями — можно приложить фото "
    "или скриншот заказа 📎 Я передам всё менеджеру, и он подключится к чату."
)
SUPPORT_SENT = (
    "Передала менеджеру ✅\n"
    "Он скоро подключится прямо в этом чате. Можешь дописать детали или приложить фото 📎"
)
SUPPORT_DONE = "Рада, что всё решилось 💛 Возвращайся, если понадобится помощь!"
SUPPORT_CLOSED = "Менеджер завершил диалог ✅ Если остались вопросы — пиши снова 💛"
SUPPORT_OFF = (
    "Менеджер сейчас недоступен 😔 Попробуй чуть позже "
    "или напиши нам в чат с продавцом на Wildberries."
)


def manager_id():
    try:
        return int(MANAGER_CHAT_ID) if MANAGER_CHAT_ID else None
    except ValueError:
        logger.error("MANAGER_CHAT_ID должен быть числом")
        return None


def menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Выбрать новый топ", callback_data="menu_catalog")],
        [InlineKeyboardButton("💬 Вопрос по заказу", callback_data="menu_support")],
    ])


def home_row():
    return [InlineKeyboardButton("🏠 В главное меню", callback_data="menu_main")]


def manager_keyboard(client_id):
    """Кнопки под сообщением клиента в чате менеджера (ID клиента зашит в кнопку)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("💬 Ответить", callback_data=f"mgr_reply:{client_id}"),
        InlineKeyboardButton("✅ Закрыть диалог", callback_data=f"mgr_close:{client_id}"),
    ]])


def _download_logo():
    for name in LOGO_FILES:
        try:
            r = requests.get(LOGO_BASE_URL + name, timeout=8)
            if r.status_code == 200 and len(r.content) > 1000:
                return r.content
        except Exception as e:
            logger.error(f"Логотип {name}: {e}")
    return None


async def send_main_menu(bot, chat_id, context, with_logo=False):
    """Главное меню. С логотипом — для приветствия."""
    if with_logo:
        logo = context.bot_data.get("logo_file_id") or _download_logo()
        if logo:
            try:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=logo if isinstance(logo, str) else BytesIO(logo),
                    caption=WELCOME_TEXT,
                    parse_mode="Markdown",
                    reply_markup=menu_keyboard(),
                )
                if msg.photo:
                    context.bot_data["logo_file_id"] = msg.photo[-1].file_id
                return
            except Exception as e:
                logger.error(f"Не удалось отправить логотип: {e}")
        await bot.send_message(chat_id=chat_id, text=WELCOME_TEXT,
                               parse_mode="Markdown", reply_markup=menu_keyboard())
        return
    await bot.send_message(chat_id=chat_id, text=MENU_TEXT, reply_markup=menu_keyboard())


def _client_for(context, target):
    """ID клиента по сообщению менеджера, на которое ответили."""
    if target is None:
        return None
    relay = context.bot_data.get("relay", {})
    if target.message_id in relay:
        return relay[target.message_id]
    m = re.search(r"🆔\s*(\d+)", (target.text or target.caption or ""))
    return int(m.group(1)) if m else None


async def _finish_support(context, user, reason):
    """Сообщить менеджеру, что клиент закончил диалог."""
    card_id = context.user_data.get("support_card")
    mgr = manager_id()
    if mgr:
        mud = context.application.user_data.get(mgr)
        if mud and mud.get("reply_to") == user.id:
            mud.pop("reply_to", None)
    if card_id and mgr:
        try:
            await context.bot.send_message(
                chat_id=mgr,
                text=f"ℹ️ Клиент {user.full_name} (🆔 {user.id}) {reason}",
                reply_to_message_id=card_id,
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить менеджера: {e}")


async def go_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _finish_support(context, update.effective_user, "вернулся в главное меню")
    context.user_data.clear()
    await send_main_menu(context.bot, query.message.chat_id, context)
    return MAIN_MENU


async def ask_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if not manager_id():
        await context.bot.send_message(chat_id=chat_id, text=SUPPORT_OFF, reply_markup=menu_keyboard())
        return MAIN_MENU
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=chat_id, text=SUPPORT_PROMPT,
        reply_markup=InlineKeyboardMarkup([home_row()]),
    )
    return SUPPORT


async def client_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любое сообщение клиента в режиме «вопрос по заказу» уходит менеджеру."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    mgr = manager_id()

    # ответы менеджера в его чате обрабатываются отдельно
    if mgr and chat_id == mgr and (msg.reply_to_message or context.user_data.get("reply_to")):
        return SUPPORT

    if not mgr:
        await msg.reply_text(SUPPORT_OFF, reply_markup=menu_keyboard())
        return MAIN_MENU

    user = update.effective_user
    relay = context.bot_data.setdefault("relay", {})
    card_id = context.user_data.get("support_card")
    first = card_id is None

    try:
        if first:
            uname = f" (@{user.username})" if user.username else ""
            card = await context.bot.send_message(
                chat_id=mgr,
                text=(
                    "💬 Вопрос по заказу\n"
                    f"👤 {user.full_name}{uname}\n"
                    f"🆔 {user.id}\n\n"
                    "Нажми «💬 Ответить» и напиши сообщение — оно уйдёт клиенту.\n"
                    "Закончить: «✅ Закрыть диалог»."
                ),
                reply_markup=manager_keyboard(user.id),
            )
            card_id = card.message_id
            context.user_data["support_card"] = card_id
            relay[card_id] = user.id

        copied = await context.bot.copy_message(
            chat_id=mgr, from_chat_id=chat_id, message_id=msg.message_id,
            reply_to_message_id=card_id,
            reply_markup=manager_keyboard(user.id),
        )
        relay[copied.message_id] = user.id
        logger.info("клиент -> менеджеру: сообщение передано")
    except Exception as e:
        logger.error(f"Не удалось передать сообщение менеджеру: {e}")
        await msg.reply_text(SUPPORT_OFF, reply_markup=menu_keyboard())
        return MAIN_MENU

    if first:
        await msg.reply_text(
            SUPPORT_SENT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Вопрос решён", callback_data="support_done")],
                home_row(),
            ]),
        )
    return SUPPORT


async def end_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _finish_support(context, update.effective_user, "завершил диалог — вопрос решён ✅")
    context.user_data.clear()
    await context.bot.send_message(chat_id=query.message.chat_id, text=SUPPORT_DONE,
                                   reply_markup=menu_keyboard())
    return MAIN_MENU


def _is_manager(update):
    mgr = manager_id()
    chat = update.effective_chat
    return bool(mgr) and chat is not None and chat.id == mgr


async def _send_to_client(context, client_id, msg):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Вопрос решён", callback_data="support_done")],
        home_row(),
    ])
    if msg.text:
        await context.bot.send_message(
            chat_id=client_id,
            text=f"👩‍💼 Менеджер Easy Style:\n{msg.text}",
            reply_markup=keyboard,
        )
    else:
        await context.bot.send_message(chat_id=client_id, text="👩‍💼 Менеджер Easy Style:")
        await context.bot.copy_message(
            chat_id=client_id, from_chat_id=msg.chat_id, message_id=msg.message_id,
            reply_markup=keyboard,
        )


async def manager_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Менеджер нажал «💬 Ответить»: его следующие сообщения уходят этому клиенту."""
    query = update.callback_query
    if not _is_manager(update):
        await query.answer("Эта кнопка только для менеджера", show_alert=True)
        return
    client_id = int(query.data.split(":")[1])
    context.user_data["reply_to"] = client_id
    logger.info("менеджер включил режим ответа")
    await query.answer("Пишите ответ — он уйдёт клиенту")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "✍️ Пишите ответ — каждое ваше сообщение уйдёт клиенту (можно с фото и файлами).\n"
            "Когда вопрос решён — нажмите «Закрыть диалог»."
        ),
        reply_to_message_id=query.message.message_id,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Закрыть диалог", callback_data=f"mgr_close:{client_id}"),
        ]]),
    )


async def manager_close_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Менеджер нажал «✅ Закрыть диалог»."""
    query = update.callback_query
    if not _is_manager(update):
        await query.answer("Эта кнопка только для менеджера", show_alert=True)
        return
    client_id = int(query.data.split(":")[1])
    if context.user_data.get("reply_to") == client_id:
        context.user_data.pop("reply_to", None)
    context.application.user_data.get(client_id, {}).pop("support_card", None)
    try:
        await context.bot.send_message(chat_id=client_id, text=SUPPORT_CLOSED,
                                       reply_markup=menu_keyboard())
        logger.info("менеджер закрыл диалог")
        await query.answer("Диалог закрыт")
        await context.bot.send_message(chat_id=query.message.chat_id, text="Диалог закрыт ✅")
    except Exception as e:
        logger.error(f"Не удалось закрыть диалог: {e}")
        await query.answer("Не получилось уведомить клиента", show_alert=True)


async def manager_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сообщение менеджера клиенту: в режиме «Ответить» или через Reply."""
    msg = update.effective_message
    target = msg.reply_to_message
    client_id = _client_for(context, target) if target else None
    if not client_id:
        client_id = context.user_data.get("reply_to")
    logger.info(f"сообщение менеджера: reply={bool(target)}, клиент найден={bool(client_id)}")
    if not client_id:
        if target:
            await msg.reply_text(
                "Не поняла, какому клиенту отвечать 🤔 Нажми «💬 Ответить» под сообщением клиента."
            )
        return
    try:
        await _send_to_client(context, client_id, msg)
        logger.info("менеджер -> клиенту: доставлено")
        await msg.reply_text("✅ Отправлено клиенту", disable_notification=True)
    except Exception as e:
        logger.error(f"Не удалось отправить ответ клиенту: {e}")
        await msg.reply_text(f"Не получилось отправить клиенту: {e}")


async def manager_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Менеджер закрывает диалог командой /close (в ответ на сообщение клиента)."""
    msg = update.effective_message
    client_id = _client_for(context, msg.reply_to_message)
    if not client_id:
        await msg.reply_text("Ответь командой /close на карточку клиента или на его сообщение.")
        return
    context.application.user_data.get(client_id, {}).pop("support_card", None)
    if context.user_data.get("reply_to") == client_id:
        context.user_data.pop("reply_to", None)
    try:
        await context.bot.send_message(chat_id=client_id, text=SUPPORT_CLOSED,
                                       reply_markup=menu_keyboard())
        await msg.reply_text("Диалог закрыт ✅")
    except Exception as e:
        logger.error(f"Не удалось закрыть диалог: {e}")
        await msg.reply_text(f"Не получилось уведомить клиента: {e}")


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/id — показывает ID чата (нужен для MANAGER_CHAT_ID)."""
    await update.effective_message.reply_text(
        f"ID этого чата: {update.effective_chat.id}\n"
        f"Твой ID: {update.effective_user.id}"
    )
