# Используем облегченную версию Debian. Она на 600-700МБ меньше обычной.
FROM python:3.11-slim-bookworm

# Устанавливаем переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

WORKDIR /app

# Устанавливаем системные зависимости
# Мы объединяем установку и очистку кэша в один слой для уменьшения веса
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Копируем только файл зависимостей
COPY requirements.txt .

# Устанавливаем библиотеки
# --no-cache-dir не сохраняет скачанные .whl файлы внутри образа
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем проект (ВАЖНО: наличие .dockerignore исключит мусор)
COPY . .

# Создаем папку для логов
RUN mkdir -p logs

CMD ["python", "run_hh_worker.py"]