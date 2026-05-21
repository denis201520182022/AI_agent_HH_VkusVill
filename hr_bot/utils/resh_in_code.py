# hr_bot\utils\resh_in_code.py
def is_candidate_profile_complete(candidate) -> bool:
    """
    Проверяет, заполнены ли все обязательные поля анкеты в БД.
    """
    if not candidate:
        return False
        
    # Проверяем наличие значений (не None и не пустая строка)
    has_phone = bool(candidate.phone_number)
    has_city = bool(candidate.city)
    
    has_citizenship = bool(candidate.citizenship)
    # Возраст проверяем именно на None, так как 0 — это теоретически валидное число (хоть и не для работы)
    has_age = candidate.age is not None 

    return all([has_citizenship, has_age])

def check_candidate_eligibility(candidate, vacancy_title: str) -> bool:
    """
    Проверяет кандидата по жестким критериям (Возраст + Гражданство).
    Возвращает True, если кандидат подходит.
    """
    # 1. ОПРЕДЕЛЕНИЕ ТИПА ВАКАНСИИ
    title_lower = (vacancy_title or "").lower()
    
    # Ключевые слова для сборщиков (из вашего списка)
    picker_keywords = [
        'сборщик','сканировщик','сканировщик','комплектовщик','упаковщик'
    ]
    
    # Проверяем, является ли вакансия "сборочной"
    is_picker = any(word in title_lower for word in picker_keywords)
    
    # Устанавливаем верхний порог возраста
    max_age = 42 if is_picker else 58

    # 2. ПРОВЕРКА ВОЗРАСТА (14 - max_age включительно)
    if not (14 <= candidate.age <= max_age):
        return False
    
    # 3. ПРОВЕРКА ГРАЖДАНСТВА (оставляем без изменений)
    allowed_countries = ['рф', 'еаэс', "внж рф", "рвп рф", "россия", "рвп", "внж", "армения", "беларусь", "беларусия", "киргизия", "казахстан"]
    cand_citizen = (candidate.citizenship or "").lower().strip()
    
    if any(country in cand_citizen for country in allowed_countries):
        return True
        
    if 'внж' in cand_citizen or 'рвп' in cand_citizen or 'вид на жительство' in cand_citizen:
        return True
        
    return False