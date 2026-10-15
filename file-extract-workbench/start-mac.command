#!/bin/bash
# 文件提取工作台 —— macOS 启动脚本（双击运行）
cd "$(dirname "$0")" || exit 1

# 去掉“来自互联网”的隔离标记，避免系统逐个拦截
xattr -dr com.apple.quarantine . 2>/dev/null

PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,8) else 1)' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done

if [ -n "$PY" ]; then
  exec "$PY" -X utf8 engine/server.py "$@"
fi

echo ""
echo "============================================================"
echo " 这台 Mac 还没有可用的 Python 3。"
echo " 如果刚才弹出了“安装命令行开发者工具”的窗口，请点“安装”，"
echo " 装好后再双击本文件即可（只需安装一次）。"
echo " 也可以从 https://www.python.org/downloads/ 下载安装 Python 3。"
echo "============================================================"
read -n 1 -s -r -p "按任意键关闭此窗口…"
exit 1
