"""Optional, manually started SMTP delivery for the local notification hub."""
from argparse import ArgumentParser
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
import sys
import time

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from automation.daily_alerts import DailyAlertQueue
from email_delivery import EmailDelivery, EmailSettings
from hub import read_settings


def utc_now():
    return datetime.now(timezone.utc)


def main():
    parser = ArgumentParser(description='Дополнительная отправка уведомлений по почте')
    parser.add_argument('--data-dir', required=True, help='папка настроек приёмника')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='запустить отправку')
    run.add_argument('--host', required=True)
    run.add_argument('--port', type=int, required=True)
    run.add_argument('--sender', required=True)
    run.add_argument('--recipient', required=True)
    run.add_argument('--username', default='')
    run.add_argument('--security', choices=('starttls', 'ssl'), default='starttls')
    run.add_argument('--once', action='store_true', help='обработать не более одного уведомления')
    commands.add_parser('review', help='показать сообщения с неизвестным результатом')
    resolve = commands.add_parser('resolve', help='после проверки почты разрешить повтор или подтвердить приём')
    resolve.add_argument('alert_id', type=int)
    resolve.add_argument('--action', choices=('retry', 'accepted'), required=True)
    args = parser.parse_args()
    directory = Path(args.data_dir).expanduser().resolve()
    config = read_settings(directory)
    database = directory / 'alerts.sqlite'
    if not database.is_file():
        raise SystemExit('База уведомлений не найдена. Проверьте папку и восстановите резервную копию.')
    queue = DailyAlertQueue(database, grouping=config['grouping'], timezone_name=config['timezone'])
    if args.command == 'run':
        password = getpass('Пароль SMTP: ') if args.username else ''
        settings = EmailSettings(args.host, args.port, args.sender, args.recipient,
                                 args.username, password, args.security)
    else:
        settings = EmailSettings('localhost', 465, 'local@example.test', 'local@example.test', security='ssl')
    delivery = EmailDelivery(queue, settings)
    if args.command == 'review':
        for item in delivery.review():
            print(f"Обращение {item['alert_id']}: {item['state']}, {item['updated_at']}")
        return
    if args.command == 'resolve':
        if not delivery.resolve(args.alert_id, retry=args.action == 'retry', now=utc_now()):
            raise SystemExit('Не удалось разрешить событие: проверьте ID и остановите отправку на время проверки.')
        print('Решение сохранено.')
        return
    print('Почтовая доставка запущена. Остановить: Ctrl+C.')
    try:
        while True:
            result = delivery.run_one(now=utc_now())
            if result not in ('empty', 'busy'):
                print(f'Результат обработки: {result}', flush=True)
            if args.once:
                break
            if result in ('empty', 'busy', 'retryable', 'uncertain', 'stale'):
                time.sleep(5)
    except KeyboardInterrupt:
        print('\nДоставка остановлена.')


if __name__ == '__main__':
    main()
