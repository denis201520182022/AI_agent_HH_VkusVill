import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.orm import joinedload
from sqlalchemy import select

# Импортируем твои модели и сессию
# Предполагается, что скрипт лежит в корне проекта
from hr_bot.db.models import SyncSessionLocal, Dialogue, Candidate, Vacancy, TrackedRecruiter

# Константы как в твоем проекте
try:
    SPB_TIMEZONE = ZoneInfo("Europe/Moscow")
except Exception:
    SPB_TIMEZONE = datetime.timezone(datetime.timedelta(hours=3))

def mask_fio(fio: str) -> str:
    """Простая маскировка ФИО (Иван Иванович И. -> Иван И.)"""
    if not fio: return "Скрыто"
    parts = fio.split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1][0]}."
    return parts[0]

def is_system_command(content: str) -> bool:
    return content.startswith("[SYSTEM COMMAND]")

def format_dialogue_to_text(dialogue: Dialogue):
    """Формирует текстовый блок одного диалога в стиле твоих TG-уведомлений"""
    candidate = dialogue.candidate
    vacancy = dialogue.vacancy
    
    safe_vacancy_title = vacancy.title if vacancy else "Не указана"
    safe_city = vacancy.city if vacancy else "Не указан"
    safe_masked_name = mask_fio(candidate.full_name) if candidate else "Неизвестен"
    
    lines = []
    lines.append("=" * 60)
    lines.append(f"=== ИСТОРИЯ ДИАЛОГА (FOLLOW-UP) ===")
    lines.append(f"ID отклика: {dialogue.hh_response_id}")
    
    if dialogue.response_created_at:
        resp_time_msk = dialogue.response_created_at.astimezone(SPB_TIMEZONE)
        lines.append(f"Время отклика (МСК): {resp_time_msk.strftime('%d.%m.%Y в %H:%M:%S')}")
    
    lines.append(f"Кандидат: {safe_masked_name}")
    lines.append(f"Вакансия: {safe_vacancy_title}, {safe_city}")
    if candidate and candidate.phone_number:
        lines.append(f"Телефон: {candidate.phone_number}")
    lines.append(f"Результат интервью: {dialogue.interview_result or '—'}")
    lines.append("-" * 50)
    lines.append("")

    if dialogue.history:
        for entry in dialogue.history:
            role = entry.get('role')
            content = entry.get('content')
            if not content or is_system_command(content):
                continue

            # Обработка времени сообщения (из поля timestamp_msk, как в твоем коде)
            timestamp_raw = entry.get('timestamp_msk', '')
            timestamp_clean = timestamp_raw.split('.')[0][:-7] if timestamp_raw else ''
            timestamp_prefix = f"[{timestamp_clean}]" if timestamp_clean else ''

            if role == 'user':
                lines.append(f"{timestamp_prefix} 👤 Кандидат: {content}")
            elif role == 'assistant':
                lines.append(f"{timestamp_prefix} 🤖 Бот: {content}")
            lines.append("") # Пустая строка для читаемости
    else:
        lines.append("[История сообщений отсутствует]")

    lines.append("\n")
    return "\n".join(lines)

def run_export():
    session = SyncSessionLocal()
    output_file = f"followup_export_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    
    try:
        print(f"🔍 Ищу диалоги с was_in_followup=True...")
        
        # Используем предзагрузку (joinedload), чтобы скрипт летал
        query = (
            session.query(Dialogue)
            .filter(Dialogue.was_in_followup == True)
            .options(
                joinedload(Dialogue.candidate),
                joinedload(Dialogue.vacancy),
                joinedload(Dialogue.recruiter)
            )
            .order_by(Dialogue.last_updated.desc())
        )
        
        dialogues = query.all()
        
        if not dialogues:
            print("❌ Диалогов для выгрузки не найдено.")
            return

        print(f"✅ Найдено диалогов: {len(dialogues)}. Начинаю запись в файл...")

        with open(output_file, "w", encoding="utf-8") as f:
            f.write(f"ОТЧЕТ ПО FOLLOW-UP ДИАЛОГАМ\n")
            f.write(f"Дата выгрузки: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}\n")
            f.write(f"Всего записей: {len(dialogues)}\n\n")
            
            for d in dialogues:
                f.write(format_dialogue_to_text(d))
        
        print(f"🚀 Успешно! Файл сохранен: {output_file}")

    except Exception as e:
        print(f"💥 Критическая ошибка при экспорте: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    run_export()