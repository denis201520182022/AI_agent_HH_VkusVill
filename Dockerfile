# --- Stage 1: Сборка зависимостей ---
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

# Устанавливаем зависимости для сборки (если нужны для psycopg2 или др.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Устанавливаем пакеты в локальную папку
RUN pip install --upgrade pip && \
    pip install --user --no-cache-dir -r requirements.txt


# --- Stage 2: Финальный образ ---
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow
# Добавляем путь к установленным пакетам из первого этапа
ENV PATH=/root/.local/bin:$PATH

WORKDIR /app

# Копируем только необходимые системные библиотеки (libpq для postgres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Копируем установленные библиотеки из builder
COPY --from=builder /root/.local /root/.local

# Копируем проект (убедись, что есть .dockerignore!)
COPY . .

RUN mkdir -p logs

CMD ["python", "run_hh_worker.py"]