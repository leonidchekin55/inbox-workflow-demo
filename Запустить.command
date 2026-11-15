#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/env python3 -m automation.launch
