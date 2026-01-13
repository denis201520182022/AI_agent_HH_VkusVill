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








# Сборка образов
docker compose build

# Запуск в фоне
docker compose up -d

Посмотреть статус всех контейнеров:
docker compose ps

Посмотреть логи (все вместе):
docker compose logs -f


docker compose logs -f hh_scanner