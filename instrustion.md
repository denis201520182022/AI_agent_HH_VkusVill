
source venv/bin/activate

alembic revision --autogenerate -m "Cnfn2"
alembic upgrade head



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

docker compose up -d --build


http://212.193.26.118:3000





{container=~"vv_(worker|scanner|reminders).*"}

{container=~"vv_scanner_.*"}

{container=~"vv_worker_.*"}

{container=~"vv_(worker|scanner)_36_49_47"}

{container="vv_tg_bot"}

