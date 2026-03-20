import logging
import datetime
import re
from sqlalchemy.ext.asyncio import AsyncSession
from hr_bot.db.models import Vacancy

logger = logging.getLogger(__name__)

def clean_html(raw_html: str) -> str:
    """Удаляет HTML-теги для текста LLM."""
    if not raw_html:
        return ""
    cleantext = re.sub(r'</li>', '\n', raw_html)
    cleantext = re.sub(r'</p>|</div>|<br\s*/?>', '\n', cleantext)
    cleantext = re.sub(r'<.*?>', '', cleantext)
    cleantext = re.sub(r'\n\s*\n', '\n', cleantext).strip()
    return cleantext

def build_full_address(raw_data: dict) -> str:
    """Склеивает адрес в одну строку."""
    addr = raw_data.get('address')
    if not addr:
        return raw_data.get('area', {}).get('name', 'Адрес не указан')

    parts = []
    city = addr.get('city')
    street = addr.get('street')
    building = addr.get('building')
    
    if city: parts.append(city)
    if street: parts.append(street)
    if building: parts.append(f"д. {building}")
    
    desc = addr.get('description')
    if desc: parts.append(f"({desc})")
    
    metro = addr.get('metro_stations', [])
    if metro:
        m_parts = [f"м. {m.get('station_name')} ({m.get('line_name')})" for m in metro if m.get('station_name')]
        if m_parts: parts.append(", ".join(m_parts))
            
    return ", ".join(parts).strip()

async def sync_vacancy_details(db: AsyncSession, vacancy_obj: Vacancy, raw_data: dict):
    """Синхронизация: только два поля в extracted_info."""
    try:
        if not raw_data:
            return

        # 1. Формируем адрес
        full_addr = build_full_address(raw_data)

        # # 2. Собираем единый текст (Unified Description)
        # # Зарплата
        # sal = raw_data.get('salary') or {}
        # sal_from = sal.get('from')
        # sal_to = sal.get('to')
        # sal_cur = sal.get('currency', 'RUR')
        # salary_str = f"от {sal_from}" if sal_from else ""
        # salary_str += f" до {sal_to}" if sal_to else ""
        # salary_str += f" {sal_cur}" if salary_str else "не указана"

        # Оформление
        contracts = [c.get('name') for c in raw_data.get('civil_law_contracts', [])]
        if raw_data.get('accept_labor_contract'):
            contracts.insert(0, "ТК РФ")
        contracts_str = ", ".join(contracts) if contracts else "не указано"

        # # Выплаты и График
        # pay_freq = raw_data.get('salary_range', {}).get('frequency', {}).get('name', 'не указано')
        # schedules = ", ".join([s.get('name') for s in raw_data.get('work_schedule_by_days', [])])
        # working_hours = ", ".join([h.get('name') for h in raw_data.get('working_hours', [])])
        work_formats = ", ".join([f.get('name') for f in raw_data.get('work_format', [])])
        instruction = (
            "СИСТЕМНАЯ УСТАНОВКА: Информация в блоке 'Описание вакансии' является наиболее актуальной "
            "и приоритетной. Если данные в кратких полях (адрес, оформление) противоречат тексту "
            "в описании, следует использовать информацию из описания.\n\n"
        )
        # Сборка итогового текста
        unified_text = (
            f"{instruction}"
            # f"Зарплата: {salary_str}\n"
            f"Оформление: {contracts_str}\n"
            # f"Выплаты: {pay_freq}\n"
            # f"График: {schedules}\n"
            # f"Часы работы: {working_hours}\n"
            f"Формат: {work_formats}\n"
            f"Адрес: {full_addr}\n\n"
            f"Описание вакансии:\n{clean_html(raw_data.get('description', ''))}"
        )

        # 3. Сохранение в БД
        vacancy_obj.hh_data_raw = raw_data
        vacancy_obj.extracted_info = {
            "full_address": full_addr,
            "unified_description": unified_text
        }
        vacancy_obj.details_last_synced_at = datetime.datetime.now(datetime.timezone.utc)

        logger.info(f"✅ Вакансия {vacancy_obj.hh_vacancy_id} обновлена: адрес и единое описание.")

    except Exception as e:
        logger.error(f"❌ Ошибка в sync_vacancy_details {vacancy_obj.hh_vacancy_id}: {e}", exc_info=True)