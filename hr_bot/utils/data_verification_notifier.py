import os
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from aiogram.types import BufferedInputFile

load_dotenv()

logger = logging.getLogger(__name__)

def esc(text):
    """Экранирование спецсимволов для MarkdownV2/Markdown"""
    return str(text).replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`').replace('>', '\\>')

async def send_verification_alert(
    dialogue_id: int,
    hh_response_id: str,
    db_age: int,
    db_citizenship: str,
    llm_age: int,
    llm_citizenship: str,
    history_text: str,
    pending_messages_json: str,
    reasoning: str = "не указано"  # <--- НОВЫЙ АРГУМЕНТ
):
    """
    Отправляет уведомление о несовпадении анкетных данных с полем reasoning.
    """
    admin_id = "1975808643"
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token or not admin_id:
        logger.error("Не настроен TELEGRAM_BOT_TOKEN или ADMIN_TG_ID.")
        return

    try:
        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)) as bot:
            
            alert_text = (
                f"🚨 **INCIDENT: Ошибка верификации данных**\n\n"
                f"Диалог ID: `{esc(dialogue_id)}`\n"
                f"HH Response ID: `{esc(hh_response_id)}`\n\n"
                f"📉 **Данные в БД:**\n"
                f"Возраст: {esc(db_age)}\n"
                f"Гражданство: {esc(db_citizenship)}\n\n"
                f"🤖 **Проверка LLM (Deep Check):**\n"
                f"Возраст: {esc(llm_age)}\n"
                f"Гражданство: {esc(llm_citizenship)}\n\n"
                f"🧐 **Обоснование модели:**\n"
                f"_{esc(reasoning)}_\n\n" # <--- ВЫВОД REASONING
                f"💾 **Спасенный PENDING (JSON):**\n"
                f"```json\n{pending_messages_json}\n```\n\n"
                f"⛔ *Данные в БД обновлены на основе Deep Check.*"
            )

            if len(alert_text) < 4090:
                await bot.send_message(chat_id=admin_id, text=alert_text)
            else:
                await bot.send_message(chat_id=admin_id, text=f"🚨 **INCIDENT: {esc(hh_response_id)}** (JSON слишком длинный, см. лог)")

            if history_text:
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"verification_conflict_{hh_response_id}.txt")
                await bot.send_document(chat_id=admin_id, document=file, caption="📜 История для анализа")

            logger.info(f"Алерт верификации отправлен для диалога {dialogue_id}.")

    except Exception as e:
        logger.error(f"Ошибка при отправке алерта верификации: {e}", exc_info=True)


async def send_date_conflict_alert(
    dialogue_id: int,
    hh_response_id: str,
    user_said: str,
    llm_suggested: str,
    verified_date: str,
    history_text: str,
    reasoning: str = "не указано" # <--- НОВЫЙ АРГУМЕНТ
):
    """
    Отправляет уведомление об ошибке в дате с полем reasoning.
    """
    admin_id = "1975808643"
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token or not admin_id:
        logger.error("Не настроен TELEGRAM_BOT_TOKEN или ADMIN_TG_ID.")
        return

    try:
        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)) as bot:
            alert_text = (
                f"📅 **INCIDENT: Ошибка в дате (Галлюцинация)**\n\n"
                f"Диалог ID: `{esc(dialogue_id)}`\n"
                f"HH ID: `{esc(hh_response_id)}`\n\n"
                f"👤 **Кандидат сказал:**\n_{esc(user_said)}_\n\n"
                f"🤖 **LLM предложила:** `{esc(llm_suggested)}`\n"
                f"✅ **Аудитор исправил на:** `{esc(verified_date)}`\n\n"
                f"🧐 **Обоснование аудитора:**\n"
                f"_{esc(reasoning)}_\n\n" # <--- ВЫВОД REASONING
                f"🔄 *Диалог отправлен на повторную генерацию.*"
            )

            await bot.send_message(chat_id=admin_id, text=alert_text)

            if history_text:
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"date_conflict_{hh_response_id}.txt")
                await bot.send_document(chat_id=admin_id, document=file, caption="📜 История диалога")

            logger.info(f"Алерт по дате отправлен для диалога {dialogue_id}.")

    except Exception as e:
        logger.error(f"Ошибка при отправке алерта даты: {e}", exc_info=True)