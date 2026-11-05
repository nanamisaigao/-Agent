#!/usr/bin/env sh
# 一键安装依赖并启动。用法：./start.sh  或  sh start.sh
set -eu

cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo
  echo "  [错误] 没有找到 Python。请先安装 Python 3.10 或更高版本。"
  echo "  https://www.python.org/downloads/"
  echo
  exit 1
fi

exec "$PY" bootstrap.py "$@"
