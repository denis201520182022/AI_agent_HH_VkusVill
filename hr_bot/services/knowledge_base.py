import time
import logging
import re
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

load_dotenv() 

logger = logging.getLogger(__name__)

DOCUMENT_ID = os.getenv('GOOGLE_DOCS_ID')
SCOPES = ['https://www.googleapis.com/auth/documents.readonly']
SERVICE_ACCOUNT_FILE = 'credentials.json' 
CACHE_TTL_SECONDS = 120

_cached_prompt_library = None
_cache_timestamp = 0

def _parse_vacancies(vacancies_raw_text: str) -> list:
    """
    Парсит сплошной текст с вакансиями в список словарей.
    Обновленная версия: гибкий поиск городов.
    """
    vacancies_list = []
    # Используем &&& как разделитель
    vacancy_blocks = vacancies_raw_text.strip().split('\n&&&\n')

    for block in vacancy_blocks:
        if not block.strip():
            continue

        lines = block.strip().split('\n')
        titles_str = lines[0]
        
        # Чистим строку с названиями от артефактов
        if titles_str.strip().startswith('—'):
            titles_str = titles_str.strip()[1:].strip()
        # Убираем пояснения в скобках в названии, если они есть в первой строке (но не города в скобках)
        

        titles = [t.strip().lower() for t in titles_str.split(',')]
        
        cities = []
        for line in lines:
            cleaned_line = line.strip().lower()
            # Улучшенная проверка: ищем и "город:", и "города:" с точкой и без
            if 'город' in cleaned_line and ':' in cleaned_line:
                try:
                    # Разделяем по двоеточию, берем правую часть
                    cities_part = line.split(':', 1)[1]
                    # Удаляем точки, если они попали в начало
                    cities_part = cities_part.replace('.', '') 
                    
                    # Разбиваем по запятым
                    cities = [c.strip() for c in cities_part.split(',')]
                    
                    # Очистка от мусора (например, если попала точка в конце)
                    cities = [c.rstrip('.') for c in cities if c]
                except IndexError:
                    cities = []
                break 
        
        vacancies_list.append({
            "titles": titles,
            "cities": cities,
            "description": block.strip()
        })
    return vacancies_list

def get_prompt_library():
    """
    Читает Google Doc, парсит его в библиотеку блоков {marker: text}
    и кэширует результат.
    """
    global _cached_prompt_library, _cache_timestamp

    if _cached_prompt_library and (time.time() - _cache_timestamp < CACHE_TTL_SECONDS):
        return _cached_prompt_library

    logger.debug("Кэш библиотеки промптов устарел, обновляю и парсю из Google Docs...")
    try:
        # Проверка наличия файла с ключами
        if not os.path.exists(SERVICE_ACCOUNT_FILE):
             logger.error(f"Файл ключей {SERVICE_ACCOUNT_FILE} не найден! Убедитесь, что запускаете скрипт из корня проекта.")
             return {}

        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        service = build('docs', 'v1', credentials=creds)

        document = service.documents().get(documentId=DOCUMENT_ID).execute()
        content = document.get('body').get('content')
        
        full_text = ''
        for value in content:
            if 'paragraph' in value:
                elements = value.get('paragraph').get('elements')
                for elem in elements:
                    full_text += elem.get('textRun', {}).get('content', '')

        # Парсинг блоков
        prompt_library = {}
        # Находим все маркеры вида #WORD#
        markers = re.findall(r"(#\w+#)", full_text)
        # Разделяем текст по этим маркерам
        parts = re.split(r"(#\w+#)", full_text)

        current_marker = None
        for part in parts:
            if not part.strip():
                continue
            if part in markers:
                current_marker = part
            elif current_marker:
                prompt_library[current_marker] = part.strip()
                current_marker = None
        
        # Отдельно обрабатываем вакансии
        if '#START_VACANCIES#' in prompt_library:
            vacancies_raw = prompt_library.pop('#START_VACANCIES#')
            vacancies_raw = vacancies_raw.replace('#END_VACANCIES#', '')
            prompt_library['vacancies'] = _parse_vacancies(vacancies_raw)
        else:
            prompt_library['vacancies'] = []

        logger.debug(f"Библиотека промптов успешно загружена. Найдено блоков: {len(prompt_library)}")
        
        _cached_prompt_library = prompt_library
        _cache_timestamp = time.time()
        
        return prompt_library

    except Exception as e:
        logger.error(f"ОШИБКА при чтении и парсинге Google Doc: {e}", exc_info=True)
        if _cached_prompt_library:
            logger.warning("Возвращаю старую версию библиотеки из кэша.")
            return _cached_prompt_library
        
        # Аварийный фолбэк
        return {"#ROLE_AND_STYLE#": "Ты - Hr компании ВкусВилл.", "vacancies": []}

# === БЛОК ОТЛАДКИ (ЗАПУСТИТСЯ ТОЛЬКО ПРИ ПРЯМОМ ВЫЗОВЕ) ===
if __name__ == '__main__':
    print("⏳ ЗАПУСК ОТЛАДКИ: Подключение к Google Docs...")
    
    library = get_prompt_library()
    
    if not library:
        print("❌ Не удалось загрузить библиотеку.")
    else:
        print("\n✅ Библиотека загружена!")
        print("-" * 50)
        
        vacancies = library.get('vacancies', [])
        print(f"📦 ВСЕГО ВАКАНСИЙ НАЙДЕНО: {len(vacancies)}")
        print("-" * 50)
        
        for i, vac in enumerate(vacancies, 1):
            print(f"\n🔹 ВАКАНСИЯ #{i}")
            print(f"   📋 Названия (Titles): {vac['titles']}")
            print(f"   🏙️  Города (Cities): {vac['cities']}")
            
            # Проверка для "Сборщика"
            if 'сборщик' in vac['titles'][0]:
                if 'пушкин' not in [c.lower() for c in vac['cities']]:
                     print("   ⚠️  ВНИМАНИЕ: Город 'Пушкин' НЕ найден в этой вакансии!")
                else:
                     print("   ✅ Город 'Пушкин' найден.")
            
            print("-" * 20)
            
        print("\n🔍 Проверьте списки городов выше. Если города '[]', значит парсер не понял строку с городами.")