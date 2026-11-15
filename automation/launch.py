"""Launch the isolated synthetic demo and open its local address."""
import argparse
import sys
import webbrowser

from .server import LocalServer


def main():
    parser = argparse.ArgumentParser(description='Локальная учебная панель обращений')
    parser.add_argument('--no-browser', action='store_true', help='не открывать браузер')
    args = parser.parse_args()
    try:
        server = LocalServer(0)
    except (OSError, ValueError):
        print('Не удалось запустить панель. Проверьте наличие файлов приложения.', file=sys.stderr)
        return 1
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Учебная панель: {url}', flush=True)
    print('Для остановки закройте это окно или нажмите Ctrl+C.', flush=True)
    if not args.no_browser:
        try:
            if not webbrowser.open(url):
                print('Не удалось открыть браузер автоматически. Откройте адрес выше вручную.', flush=True)
        except Exception:
            print('Не удалось открыть браузер автоматически. Откройте адрес выше вручную.', flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
