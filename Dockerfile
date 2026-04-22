# === STAGE 1: Сборка зависимостей ===
FROM python:3.11-slim-bookworm AS builder

# Отключаем кэш pip, чтобы не раздувать слои
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app

# Устанавливаем инструменты сборки (нужны для компиляции некоторых библиотек, например psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем библиотеки в локальную папку пользователя (--user)
# Это позволит просто скопировать папку .local в финальный образ
RUN pip install --upgrade pip && \
    pip install --user --no-cache-dir -r requirements.txt


# === STAGE 2: Финальный образ ===
FROM python:3.11-slim-bookworm

# Переменные окружения для стабильной работы Python
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow
# Прописываем путь к установленным библиотекам
ENV PATH=/root/.local/bin:$PATH

WORKDIR /app

# Устанавливаем ТОЛЬКО необходимые системные библиотеки времени выполнения (libpq5 для связи с Postgres)
# Чистим кэш apt сразу после установки в том же слое
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Копируем установленные Python-библиотеки из первого этапа
COPY --from=builder /root/.local /root/.local

# Копируем проект (ВАЖНО: наличие .dockerignore исключит папку venv)
COPY . .

# Создаем папку для логов и удаляем лишний мусор (pycache), если он вдруг попал
RUN mkdir -p logs && \
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Команда по умолчанию (будет переопределена в docker-compose)
CMD ["python", "run_hh_worker.py"]