systemctl stop hh_main.service
systemctl status hh_reminders.service

journalctl -u hh_main.service -f
journalctl -u hh_reminders.service -f










journalctl -u hh_scanner.service -f
journalctl -u hh_worker.service -f
journalctl -u hh_reminders.service -f

systemctl restart hh_scanner
systemctl restart hh_worker
systemctl restart hh_reminders
