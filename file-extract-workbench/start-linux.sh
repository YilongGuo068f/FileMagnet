#!/bin/bash
# 文件提取工作台 —— Linux 启动脚本
cd "$(dirname "$0")" || exit 1
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)' >/dev/null 2>&1; then
    exec "$c" -X utf8 engine/server.py "$@"
  fi
done
echo "需要 Python 3.8 或以上版本（例如：sudo apt install python3）"
exit 1
