"""Start the local notification hub and open its dashboard on macOS."""
from pathlib import Path
import sys
import threading
import urllib.request
import webbrowser

from hub import build_servers, create_settings


def start(data_dir, *, device_port=8765, dashboard_port=8766, opener=webbrowser.open):
    data_dir = Path(data_dir).expanduser()
    if data_dir.exists():
        required = ('settings.json', 'device-tokens.json', 'alerts.sqlite')
        if not data_dir.is_dir() or any(not (data_dir / name).is_file() for name in required):
            raise ValueError('Папка данных существует, но настройки или база отсутствуют. Восстановите недостающие файлы.')
    else:
        create_settings(data_dir, [], 'client', 'Europe/Moscow')
    device, dashboard = build_servers(data_dir, device_port, dashboard_port)
    servers = (device, dashboard)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    url = f'http://127.0.0.1:{dashboard.server_address[1]}/'
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError('Панель не отвечает.')
        try:
            if not opener(url):
                print(f'Откройте панель вручную: {url}', flush=True)
        except Exception:
            print(f'Откройте панель вручную: {url}', flush=True)
    except Exception:
        for server in servers:
            server.shutdown()
            server.server_close()
        raise
    return servers, threads, url


def main():
    data_dir = Path.home() / 'Library' / 'Application Support' / 'Lena Alerts'
    try:
        servers, threads, url = start(data_dir)
    except Exception as error:
        print(f'Не удалось запустить уведомления: {error}', file=sys.stderr, flush=True)
        return 1
    print(f'Панель запущена: {url}', flush=True)
    print('Для завершения закройте окно или нажмите Ctrl+C.', flush=True)
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
