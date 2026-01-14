
source venv/bin/activate

systemctl stop hh_main.service
systemctl status hh_reminders.service
journalctl -u hh_main.service -f
journalctl -u hh_reminders.service -f
systemctl disable hh_scanner hh_worker hh_reminders hh_main
journalctl -u hh_scanner.service -f
journalctl -u hh_worker.service -f
journalctl -u hh_reminders.service -f
systemctl restart hh_scanner
systemctl restart hh_worker
systemctl restart hh_reminders
systemctl stop hh_scanner
systemctl stop hh_worker
systemctl stop hh_reminders




# Этот воркер будет обслуживать ТОЛЬКО рекрутеров 1 и 5
python run_hh_worker.py --recruiter_ids 1 5





source venv/bin/activate



# Сборка образов
docker compose build

# Запуск в фоне
docker compose up -d

Посмотреть статус всех контейнеров:
docker compose ps

Посмотреть логи (все вместе):
docker compose logs -f


docker compose logs -f hh_scanner
docker compose restart hh_scanner

docker compose logs -f hh_scanner_r1


# 1. Останавливаем и удаляем текущие контейнеры и сеть
docker compose down

# 2. Собираем заново без использования кэша
docker compose build --no-cache

# 3. Запускаем
docker compose up -d



http://212.193.26.118:3000



📖 Инструкция по мониторингу и управлению (HH Bot)
📊 Часть 1: Просмотр логов в Grafana
Вход: http://<IP-ТВОЕГО-СЕРВЕРА>:3000
Раздел: Меню слева (компас) -> Explore.
Источник данных (сверху слева): Loki.
В Loki запросы пишутся на языке LogQL. Вот основные команды, которые тебе понадобятся. Вставляй их в строку запроса и жми синюю кнопку Run query (справа сверху).
1. Посмотреть логи КОНКРЕТНОГО типа воркеров
Так как Docker добавляет префиксы и цифры к именам, используем поиск по частичному совпадению (=~).
Все Воркеры (Обработчики):
code
Logql
{container=~".*hh_worker.*"}
Все Сканеры:
code
Logql
{container=~".*hh_scanner.*"}
Напоминатор:
code
Logql
{container=~".*hh_reminders.*"}
Telegram Бот:
code
Logql
{container=~".*tg_bot.*"}
2. Посмотреть логи ВСЕХ ботов (без инфраструктуры)
Чтобы увидеть общую картину работы системы, но без шума от базы данных, редиса и самой графаны.
code
Logql
{container=~".*(hh_worker|hh_scanner|hh_reminders|tg_bot).*"}
3. Фильтрация по тексту (Поиск ошибок)
LogQL позволяет фильтровать строки через пайп |.
Найти только ОШИБКИ во всех воркерах:
code
Logql
{container=~".*hh_worker.*"} |= "ERROR"
Найти только критические ошибки (CRITICAL):
code
Logql
{container=~".*"} |= "CRITICAL"
Найти конкретный ID кандидата (Trace):
code
Logql
{container=~".*"} |= "4993057389"
Исключить лишнее (например, убрать Debug):
code
Logql
{container=~".*hh_worker.*"} != "DEBUG"
4. Режим Live (Реальное время)
Справа сверху есть кнопка Live. Нажми её после ввода запроса, и логи будут бежать по экрану как в "Матрице" (аналог tail -f).
🛠 Часть 2: Управление контейнерами (Docker Compose)
Все команды выполняются из папки с docker-compose.yml.
1. Масштабирование (Scaling) 🚀
Как добавить мощности, если очередь диалогов растет.
Увеличить количество Воркеров до 10:
code
Bash
docker compose up -d --scale hh_worker=10 --no-deps hh_worker
(Флаг --no-deps ускоряет процесс, не перезагружая базу и Redis).
Увеличить количество Сканеров до 3:
(Работает, только если у сервиса hh_scanner убран параметр container_name в конфиге).
code
Bash
docker compose up -d --scale hh_scanner=3 --no-deps hh_scanner
Вернуть как было (уменьшить):
Просто запусти команду с меньшим числом. Лишние контейнеры удалятся сами.
code
Bash
docker compose up -d --scale hh_worker=1
2. Запуск для конкретных рекрутеров (VIP) 🎯
Вариант А: Одноразовый запуск (Временный)
Если нужно срочно просканировать рекрутера ID 123, не меняя конфиг. Контейнер отработает и удалится.
code
Bash
# Запуск сканера для ID 123
docker compose run --rm hh_scanner python run_hh_scanner.py --recruiter_ids 123

# Запуск воркера для ID 123
docker compose run --rm hh_worker python run_hh_worker.py --recruiter_ids 123
Вариант Б: Постоянный VIP-воркер
Если рекрутер 123 всегда должен обрабатываться отдельно.
Открой docker-compose.yml.
Скопируй блок сервиса hh_worker и назови его, например, hh_worker_vip.
В строке command напиши: python run_hh_worker.py --recruiter_ids 123.
Примени изменения: docker compose up -d.
3. Обновление кода 🔄
Если ты изменил .py файлы, контейнеры нужно пересобрать.
Обновить только Воркеры (быстро):
code
Bash
docker compose up -d --build --no-deps hh_worker
Обновить всё (полная пересборка):
code
Bash
docker compose up -d --build
Полная пересборка с нуля (если менял requirements.txt):
code
Bash
docker compose build --no-cache
docker compose up -d
4. Экстренные ситуации 🚨
Сброс "зависших" блокировок Redis:
Если контейнеры были убиты жестко и новые воркеры висят (не могут получить семафор).
code
Bash
# Удалить все ключи семафоров и локов (только для БД 0)
redis-cli -n 0 keys "semaphore:*" | xargs -r redis-cli -n 0 del
redis-cli -n 0 keys "refresh_token_lock:*" | xargs -r redis-cli -n 0 del
Посмотреть статус всех контейнеров:
code
Bash
docker compose ps
Перезагрузка PgBouncer (если база тупит):
code
Bash
docker compose restart pgbouncer