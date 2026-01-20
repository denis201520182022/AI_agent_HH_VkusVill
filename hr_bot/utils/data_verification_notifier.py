# hr_bot/utils/data_verification_notifier.py



import os
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

async def send_verification_alert(
    dialogue_id: int,
    hh_response_id: str,
    db_age: int,
    db_citizenship: str,
    llm_age: int,
    llm_citizenship: str,
    history_text: str,
    pending_messages_json: str  # <--- НОВЫЙ АРГУМЕНТ
):
    """
    Отправляет уведомление администратору о несовпадении данных.
    Включает сохраненные сообщения из очереди pending.
    """
    admin_id = "1975808643"
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token or not admin_id:
        logger.error("Не настроен TELEGRAM_BOT_TOKEN или ADMIN_TG_ID.")
        return

    try:
        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)) as bot:
            
            def esc(text):
                return str(text).replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`')

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
                f"💾 **Спасенный PENDING (JSON):**\n"
                f"Скопируйте это обратно в БД или возьмите текст отсюда:\n"
                f"```json\n{pending_messages_json}\n```\n\n"
                f"⛔ *Диалог переведен в 'admin_review', pending очищен.*"
            )

            # Отправляем текст (если он не слишком длинный)
            if len(alert_text) < 4090:
                await bot.send_message(chat_id=admin_id, text=alert_text)
            else:
                # Если JSON огромный, шлем частями
                await bot.send_message(chat_id=admin_id, text=f"🚨 **INCIDENT: {esc(hh_response_id)}** (см. файл)")

            # Отправляем историю файлом
            if history_text:
                from aiogram.types import BufferedInputFile
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"conflict_{hh_response_id}.txt")
                await bot.send_document(chat_id=admin_id, document=file, caption="📜 Полная история диалога")

            logger.info(f"Алерт верификации отправлен для диалога {dialogue_id}.")

    except Exception as e:
        logger.error(f"Ошибка при отправке алерта верификации: {e}", exc_info=True)