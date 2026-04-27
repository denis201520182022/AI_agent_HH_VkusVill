#hr_bot\utils\data_verification_notifier.py

import os
import logging
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from aiogram.types import BufferedInputFile
from aiogram.client.session.aiohttp import AiohttpSession
load_dotenv()

logger = logging.getLogger(__name__)

def esc(text):
    """Экранирование спецсимволов для MarkdownV2/Markdown"""
    return str(text).replace('_', '\\_').replace('*', '\\*').replace('[', '\\[').replace('`', '\\`').replace('>', '\\>')

def get_session():
    host = os.getenv("SQUID_PROXY_HOST", "").strip('"')
    port = os.getenv("SQUID_PROXY_PORT", "").strip('"')
    user = os.getenv("SQUID_PROXY_USER", "").strip('"')
    password = os.getenv("SQUID_PROXY_PASSWORD", "").strip('"')
    
    if host and port:
        proxy_url = f"http://{user}:{password}@{host}:{port}"
        return AiohttpSession(proxy=proxy_url)
    return None

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
        session = get_session()
        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN), session=session) as bot:
            
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
                await bot.send_message(chat_id=admin_id, text=alert_text, disable_notification=True)
            else:
                await bot.send_message(chat_id=admin_id, text=f"🚨 **INCIDENT: {esc(hh_response_id)}** (JSON слишком длинный, см. лог)")

            if history_text:
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"verification_conflict_{hh_response_id}.txt")
                await bot.send_document(chat_id=admin_id, document=file, caption="📜 История для анализа", disable_notification=True)

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
        session = get_session()


        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN), session=session) as bot:
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

            await bot.send_message(chat_id=admin_id, text=alert_text, disable_notification=True)

            if history_text:
                file = BufferedInputFile(history_text.encode('utf-8'), filename=f"date_conflict_{hh_response_id}.txt")
                await bot.send_document(chat_id=admin_id, document=file, caption="📜 История диалога", disable_notification=True)

            logger.info(f"Алерт по дате отправлен для диалога {dialogue_id}.")

    except Exception as e:
        logger.error(f"Ошибка при отправке алерта даты: {e}", exc_info=True)



async def send_cost_alert(
    dialogue_id: int,
    hh_response_id: str,
    total_cost_rub: float,         # Поменял местами и сделал главными
    total_cost_usd: float = 0.0,   # Сделал значение по умолчанию
    history: list = None,
    recruiter_name: str = "Неизвестен",
    vacancy_title: str = "Неизвестна"
):
    """
    Отправляет уведомление об аномально дорогом диалоге.
    """
    admin_id = "1975808643"
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token or not admin_id:
        logger.error("Не настроен TELEGRAM_BOT_TOKEN или ADMIN_TG_ID.")
        return

    try:
        session = get_session()
        async with Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN), session=session) as bot:
            
            # Ссылка на диалог в HH (для быстрого перехода)
            hh_link = f"https://spb.hh.ru/negotiations/{hh_response_id}"
            
            alert_text = (
                f"💰 **ALARM: Аномально дорогой диалог**\n\n"
                f"ID диалога: `{esc(dialogue_id)}`\n"
                f"Рекрутер: *{esc(recruiter_name)}*\n"
                f"Вакансия: *{esc(vacancy_title)}*\n\n"
                f"💵 **Затраты:**\n"
                f"Итого в RUB: `{esc(round(total_cost_rub, 2))} руб.`\n"
                # Добавим проверку USD
                + (f"Итого в USD: `${esc(round(total_cost_usd, 4))}`\n\n" if total_cost_usd else "\n") +
                f"🔗 [Открыть переписку на HH]({hh_link})\n\n"
                f"⚠️ *Рекомендуется проверить историю на предмет зацикливания.*"
            )

            # Формируем текстовый дамп истории
            history_lines = []
            for m in history:
                role = m.get('role', 'unknown')
                content = m.get('content', '')
                timestamp = m.get('timestamp_msk', 'no-time')
                history_lines.append(f"[{timestamp}] {role.upper()}: {content}")
            
            full_history_dump = "\n".join(history_lines)

            # Отправляем сообщение
            await bot.send_message(chat_id=admin_id, text=alert_text, disable_web_page_preview=True)

            # Отправляем файл с дампом
            if full_history_dump:
                file = BufferedInputFile(
                    full_history_dump.encode('utf-8'), 
                    filename=f"cost_dump_{hh_response_id}.txt"
                )
                await bot.send_document(
                    chat_id=admin_id, 
                    document=file, 
                    caption=f"📄 Полный дамп диалога {hh_response_id}"
                )

            logger.info(f"Алерт стоимости отправлен для диалога {dialogue_id} ({total_cost_rub} руб).")

    except Exception as e:
        logger.error(f"Ошибка при отправке алерта стоимости: {e}", exc_info=True)