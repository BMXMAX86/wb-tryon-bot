import os
import json
import sqlite3
import logging
import datetime
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from support import menu_keyboard, home_row, MAIN_MENU

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
LEGAL_FILE = os.path.join(_HERE, "legal.json")

# Постоянное хранилище: том Railway (RAILWAY_VOLUME_MOUNT_PATH) или папка из DATA_DIR
_VOLUME = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
DATA_DIR = os.environ.get("DATA_DIR", "").strip() or _VOLUME or os.path.join(_HERE, "data")
DB_PATH = os.path.join(DATA_DIR, "consents.db")
PERSISTENT = bool(os.environ.get("DATA_DIR", "").strip() or _VOLUME)

NOTICE_TEXT = (
    "<b>✨ Виртуальная примерка</b>\n\n"
    "Примерка создаётся искусственным интеллектом, носит ознакомительно-развлекательный характер. "
    "Цвет, размер и посадка могут отличаться от реальности.\n\n"
    "Решение о покупке вы принимаете самостоятельно.\n\n"
    "Для примерки необходимо обработать ваше фото."
)


# ─── Настройки текста согласия (legal.json) ──────────────────────────────────

def advice_enabled():
    """Совет стилиста (Anthropic) получает фото клиента — учитывается в тексте и версии согласия."""
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()) and os.environ.get("STYLIST_ADVICE", "") == "1"


def load_legal():
    try:
        with open(LEGAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"legal.json не прочитан: {e}")
        return {}


def current_version():
    v = str(load_legal().get("consent_version") or "1")
    return v + ("+advice" if advice_enabled() else "")


_REQUIRED = {
    "operator_details": "ИНН и ОГРНИП оператора",
    "operator_address": "адрес оператора",
    "contact": "контакт для обращений по данным",
    "fashn_entity": "юридическое лицо сервиса FASHN",
    "fashn_country": "страна сервиса FASHN",
    "fashn_retention": "срок хранения фото у сервиса FASHN по его условиям",
}
_REQUIRED_ADVICE = {
    "anthropic_entity": "юридическое лицо сервиса Anthropic",
    "anthropic_country": "страна сервиса Anthropic",
    "anthropic_retention": "срок хранения фото у сервиса Anthropic по его условиям",
}


def legal_missing():
    """Какие поля legal.json ещё не заполнены (для предупреждения в логе)."""
    legal = load_legal()
    need = dict(_REQUIRED)
    if advice_enabled():
        need.update(_REQUIRED_ADVICE)
    return [k for k in need if not str(legal.get(k) or "").strip()]


def _f(legal, key, hint):
    v = str(legal.get(key) or "").strip()
    return v if v else f"[ЗАПОЛНИТЬ: {hint}]"


def consent_text():
    L = load_legal()
    operator = "; ".join([
        _f(L, "operator_name", "наименование оператора"),
        _f(L, "operator_details", _REQUIRED["operator_details"]),
        _f(L, "operator_address", _REQUIRED["operator_address"]),
    ])
    processors = (
        "• сервис виртуальной примерки FASHN (api.fashn.ai): "
        f"{_f(L, 'fashn_entity', _REQUIRED['fashn_entity'])}, "
        f"страна: {_f(L, 'fashn_country', _REQUIRED['fashn_country'])}. "
        "Ему передаются ваше фото и фото товара для создания изображения. "
        f"Срок хранения у сервиса: {_f(L, 'fashn_retention', _REQUIRED['fashn_retention'])}.\n"
    )
    if advice_enabled():
        processors += (
            "• сервис Anthropic (api.anthropic.com), формирует совет стилиста: "
            f"{_f(L, 'anthropic_entity', _REQUIRED_ADVICE['anthropic_entity'])}, "
            f"страна: {_f(L, 'anthropic_country', _REQUIRED_ADVICE['anthropic_country'])}. "
            "Ему передаётся ваше фото. "
            f"Срок хранения у сервиса: {_f(L, 'anthropic_retention', _REQUIRED_ADVICE['anthropic_retention'])}.\n"
        )
    processors += "• хостинг бота — Railway, регион EU West: данные проходят через его серверы в памяти.\n"

    return (
        f"СОГЛАСИЕ НА ОБРАБОТКУ ПЕРСОНАЛЬНЫХ ДАННЫХ (версия {current_version()})\n\n"
        f"1. Оператор: {operator}. Бренд: Easy Style. "
        f"Связь по вопросам данных: {_f(L, 'contact', _REQUIRED['contact'])} "
        "или раздел «🛡 Мои данные» в этом боте.\n\n"
        "2. Цель: создание виртуальной примерки одежды по вашему фото и подбор размера.\n\n"
        "3. Какие данные: ваш идентификатор в Telegram; фото, которое вы отправляете для примерки; "
        "рост, вес, обхват груди и талии, которые вы вводите.\n\n"
        "4. Действия с данными: получение, передача обработчикам (п. 5) для создания изображения, "
        "получение результата и отправка его вам, удаление из памяти бота после примерки. "
        "Фото и введённые параметры не записываются нами на диск и не хранятся после примерки. "
        "Мы храним только запись о вашем согласии: Telegram ID, дата и время, версия этого текста.\n\n"
        f"5. Обработчики:\n{processors}"
        "Обработка выполняется за пределами РФ.\n\n"
        "6. Срок: согласие действует до отзыва. Запись о согласии хранится, пока вы не удалите "
        "данные в разделе «🛡 Мои данные».\n\n"
        "7. Отзыв: в любой момент в разделе «🛡 Мои данные» (или командой /privacy) "
        "либо по контакту из п. 1. После отзыва фото больше не обрабатываются.\n\n"
        "Нажимая «✅ Согласен на обработку фото», вы даёте согласие на указанную обработку."
    )


# ─── Хранилище согласий (SQLite) ─────────────────────────────────────────────

def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute(
        "CREATE TABLE IF NOT EXISTS consents ("
        "telegram_id INTEGER PRIMARY KEY, version TEXT NOT NULL, granted INTEGER NOT NULL, "
        "granted_at TEXT NOT NULL, revoked_at TEXT)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS consent_log ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, "
        "event TEXT NOT NULL, version TEXT NOT NULL, at TEXT NOT NULL)"
    )
    return c


def init_db():
    with closing(_conn()):
        pass


def consent_status(uid):
    """(version, granted, granted_at, revoked_at) или None."""
    try:
        with closing(_conn()) as c:
            return c.execute(
                "SELECT version, granted, granted_at, revoked_at FROM consents WHERE telegram_id = ?", (uid,)
            ).fetchone()
    except Exception as e:
        logger.error(f"Ошибка чтения согласия: {e}")
        return None


def has_consent(uid):
    """Действующее согласие на текущую версию текста. При ошибке хранилища — нет согласия."""
    row = consent_status(uid)
    return bool(row and row[1] == 1 and row[0] == current_version())


def grant_consent(uid):
    ver, now = current_version(), _now()
    try:
        with closing(_conn()) as c:
            c.execute(
                "INSERT INTO consents (telegram_id, version, granted, granted_at, revoked_at) "
                "VALUES (?, ?, 1, ?, NULL) ON CONFLICT(telegram_id) DO UPDATE SET "
                "version = excluded.version, granted = 1, granted_at = excluded.granted_at, revoked_at = NULL",
                (uid, ver, now),
            )
            c.execute("INSERT INTO consent_log (telegram_id, event, version, at) VALUES (?, 'granted', ?, ?)", (uid, ver, now))
            c.commit()
        logger.info("согласие на обработку фото получено")
        return True
    except Exception as e:
        logger.error(f"Не удалось сохранить согласие: {e}")
        return False


def revoke_consent(uid):
    row = consent_status(uid)
    if not row:
        return True
    now = _now()
    try:
        with closing(_conn()) as c:
            c.execute("UPDATE consents SET granted = 0, revoked_at = ? WHERE telegram_id = ?", (now, uid))
            c.execute("INSERT INTO consent_log (telegram_id, event, version, at) VALUES (?, 'revoked', ?, ?)", (uid, row[0], now))
            c.commit()
        logger.info("согласие отозвано")
        return True
    except Exception as e:
        logger.error(f"Не удалось отозвать согласие: {e}")
        return False


def delete_user_data(uid):
    try:
        with closing(_conn()) as c:
            c.execute("DELETE FROM consents WHERE telegram_id = ?", (uid,))
            c.execute("DELETE FROM consent_log WHERE telegram_id = ?", (uid,))
            c.commit()
        logger.info("данные пользователя удалены")
        return True
    except Exception as e:
        logger.error(f"Не удалось удалить данные: {e}")
        return False


# ─── Экраны и кнопки ─────────────────────────────────────────────────────────

def _consent_kb(product_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Согласие на обработку персональных данных", callback_data=f"consent_doc:{product_id}")],
        [InlineKeyboardButton("✅ Согласен на обработку фото", callback_data=f"consent_yes:{product_id}")],
        [InlineKeyboardButton("↩️ Продолжить без примерки", callback_data="consent_no")],
    ])


async def ask_consent(bot, chat_id, product_id=0):
    """Короткое уведомление перед первой примеркой."""
    await bot.send_message(chat_id=chat_id, text=NOTICE_TEXT, parse_mode="HTML",
                           reply_markup=_consent_kb(product_id))


async def consent_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полный текст согласия отдельным сообщением."""
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split(":")[1])
    text = consent_text()
    parts = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        keyboard = None
        if last:
            keyboard = _consent_kb(product_id) if product_id else InlineKeyboardMarkup([home_row()])
        await context.bot.send_message(chat_id=query.message.chat_id, text=part, reply_markup=keyboard)


def _privacy_view(uid):
    row = consent_status(uid)
    if row and row[1] == 1 and row[0] == current_version():
        state = f"дано {row[2]} UTC"
    elif row and row[1] == 1:
        state = "нужно подтвердить заново (текст согласия обновился)"
    elif row:
        state = f"отозвано {row[3]} UTC"
    else:
        state = "не дано"
    text = (
        "🛡 Мои данные\n\n"
        f"Согласие на обработку фото: {state}.\n"
        "Фото и введённые параметры мы не храним: они используются только во время примерки. "
        "Сохраняется лишь запись о согласии (Telegram ID, дата и время, версия текста)."
    )
    rows = []
    if row and row[1] == 1:
        rows.append([InlineKeyboardButton("🚫 Отозвать согласие", callback_data="privacy_revoke")])
    if row:
        rows.append([InlineKeyboardButton("🗑 Удалить мои данные", callback_data="privacy_delete")])
    rows.append([InlineKeyboardButton("📄 Текст согласия", callback_data="consent_doc:0")])
    rows.append(home_row())
    return text, InlineKeyboardMarkup(rows)


async def _show_privacy(bot, chat_id, uid, prefix=""):
    text, keyboard = _privacy_view(uid)
    await bot.send_message(chat_id=chat_id, text=prefix + text, reply_markup=keyboard)


async def privacy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await _show_privacy(context.bot, query.message.chat_id, update.effective_user.id)
    return MAIN_MENU


async def privacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_privacy(context.bot, update.effective_chat.id, update.effective_user.id)
    return MAIN_MENU


async def privacy_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    if not revoke_consent(uid):
        await query.answer("Не получилось отозвать согласие. Попробуй ещё раз.", show_alert=True)
        return MAIN_MENU
    await query.answer("Согласие отозвано")
    await _show_privacy(context.bot, query.message.chat_id, uid,
                        prefix="Готово: фото больше не обрабатываются. ")
    return MAIN_MENU


async def privacy_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    if not delete_user_data(uid):
        await query.answer("Не получилось удалить данные. Попробуй ещё раз.", show_alert=True)
        return MAIN_MENU
    context.user_data.clear()
    await query.answer("Данные удалены")
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🗑 Готово: запись о согласии и введённые параметры удалены.",
        reply_markup=menu_keyboard(),
    )
    return MAIN_MENU
