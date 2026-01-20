import datetime
from zoneinfo import ZoneInfo



def _generate_calendar_context() -> str:
    """
    Генерирует текстовый блок с календарем и правилами работы с датами.
    """
    moscow_tz = ZoneInfo("Europe/Moscow")
    current_datetime_utc = datetime.datetime.now(moscow_tz)
    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    
    # Словарь для склонения в "следующий/следующая/следующее"
    weekday_next_form = {
        "понедельник": "Следующий понедельник",
        "вторник": "Следующий вторник",
        "среда": "Следующая среда",
        "четверг": "Следующий четверг",
        "пятница": "Следующая пятница",
        "суббота": "Следующая суббота",
        "воскресенье": "Следующее воскресенье"
    }

    current_weekday = weekdays_ru[current_datetime_utc.weekday()]
    current_date_str = current_datetime_utc.strftime("%Y-%m-%d")
    current_time_str = current_datetime_utc.strftime("%H:%M")

    # Собираем информацию о днях недели для отслеживания повторений
    weekday_occurrences = {}
    
    calendar_context_lines = []
    for i in range(14):
        date_cursor = current_datetime_utc + datetime.timedelta(days=i)
        wd_name = weekdays_ru[date_cursor.weekday()]
        date_str = date_cursor.strftime("%Y-%m-%d")
        
        # Отслеживаем, сколько раз встречался этот день недели
        if wd_name not in weekday_occurrences:
            weekday_occurrences[wd_name] = 0
        weekday_occurrences[wd_name] += 1
        
        # Формируем префикс и суффикс
        prefix = ""
        suffix = ""
        
        if i == 0:
            prefix = "(СЕГОДНЯ) "
            suffix = " ← ТЫ ЗДЕСЬ"
            day_label = wd_name.capitalize()
        elif i == 1:
            prefix = "(ЗАВТРА) "
            day_label = wd_name.capitalize()
        elif i == 2:
            prefix = "(ПОСЛЕЗАВТРА) "
            day_label = wd_name.capitalize()
        else:
            # Для остальных дней
            if weekday_occurrences[wd_name] == 2:
                # Второе упоминание дня недели - добавляем "Следующий"
                day_label = weekday_next_form[wd_name]
            else:
                # Первое упоминание или третье+ - просто название с заглавной
                day_label = wd_name.capitalize()
        
        calendar_context_lines.append(f"{prefix}{date_str} {day_label}{suffix}")

    calendar_string = "\n".join(calendar_context_lines)

    calendar_context = (
        f"\n\n[CRITICAL CALENDAR CONTEXT]\n"
        f"ТЕКУЩАЯ ДАТА И ВРЕМЯ (МСК): {current_datetime_utc.strftime('%Y-%m-%d %H:%M')}\n"
        f"СЕГОДНЯ: {current_weekday}, {current_date_str}\n\n"
        f"СЕЙЧАС: {current_time_str} (МСК)\n"
        f"⚠️ ВАЖНО: Ты ОЧЕНЬ ПЛОХО считаешь даты в уме. НИКОГДА НЕ ВЫЧИСЛЯЙ ДАТЫ САМОСТОЯТЕЛЬНО!\n"
        f"Используй ТОЛЬКО эту таблицу (таблица начинается с СЕГОДНЯ и идет на 14 дней вперед):\n\n"
        f"{calendar_string}\n\n"
        f"ПРАВИЛА РАБОТЫ С ДАТАМИ:\n"
        f"1. Кандидат говорит просто день недели ('понедельник', 'вторник'):\n"
        f"   → Найди ПЕРВУЮ строку с этим днем (без слова 'Следующий')\n"
        f"   → Скопируй дату из этой строки\n\n"
        f"2. Если кандидат говорит 'СЛЕДУЮЩИЙ [день недели]' (например, 'следующий понедельник'):\n"
        f"   → Бери такой день из списка выше, где написано 'СЛЕДУЮЩИЙ [день недели]' (например, 'следующий понедельник')\n\n"
        f"   → Скопируй дату из этой строки\n\n"
        f"3. Если кандидат называет день недели, который совпадает с СЕГОДНЯ:\n"
        f"   → ОБЯЗАТЕЛЬНО уточни: 'Вы имеете в виду сегодня или через неделю?'\n\n"
        f"4. Если кандидат говорит 'сегодня', 'завтра', 'послезавтра':\n"
        f"   → Ищи в списке пометку 'СЕГОДНЯ', 'ЗАВТРА' или 'ПОСЛЕЗАВТРА'\n\n"
        f"5. Если кандидат называет дату:\n"
        f"   → Не отказывай ему, ищи в списке эту дату и бери ее\n\n"
        f"6. ВСЕГДА копируй дату ТОЧНО из таблицы в формате YYYY-MM-DD\n"
        f"7. НИКОГДА не изобретай даты сам - только из этой таблицы!\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"ПРИМЕРЫ:\n"
        f"═══════════════════════════════════════════════════════════\n"
        f"Кандидат: 'понедельник' → Ты ищешь 'Понедельник'\n"
        f"Кандидат: 'следующий понедельник' → Ты ищешь строчку 'Следующий понедельник'\n"
        f"Кандидат: 'завтра' → Ты ищешь строчку с пометкой '(ЗАВТРА)'\n"
    )
    return calendar_context


print(_generate_calendar_context())