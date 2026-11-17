#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'Для запуска нужен Python 3.9 или новее.'
  exit 1
fi
python3 notification-hub/launcher.py
result=$?
if [ "$result" -ne 0 ]; then
  printf '%s\n' 'Окно можно закрыть после прочтения ошибки.'
  read -r -p 'Нажмите Enter для выхода. ' _
fi
exit "$result"
