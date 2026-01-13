# Используем полную версию Python 3.11 на базе Debian Bookworm
FROM python:3.11-bookworm

# Устанавливаем рабочую директорию
WORKDIR /app

# Устанавливаем часовой пояс (важно для логов и расписаний)
ENV TZ=Europe/Moscow
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Сначала копируем зависимости (для кэширования слоев Docker)
COPY requirements.txt .

# Обновляем pip и устанавливаем зависимости
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь остальной код проекта
COPY . .

# Создаем папку для логов, чтобы не было ошибок прав доступа
RUN mkdir -p logs

# Переменная окружения для корректного вывода логов в Docker
ENV PYTHONUNBUFFERED=1

# Команда по умолчанию (будет переопределена в docker-compose)
CMD ["python", "run_hh_worker.py"]