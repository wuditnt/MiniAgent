#!/usr/bin/env bash
# ============================================================
# mini-agent 本地测试（Git Bash / WSL / macOS / Linux）
#
#   ./scripts/run_tests.sh              跑全部免费用例（不需要 API Key）
#   ./scripts/run_tests.sh loop         只跑名字含 loop 的套件
#   ./scripts/run_tests.sh --list       列出所有套件
#   ./scripts/run_tests.sh --real       额外跑真实 API 用例（会消耗 token）
#   ./scripts/run_tests.sh --reinstall  先按 requirements.txt 同步依赖
#
# 注意：本脚本住在 scripts/，但一切相对路径（.venv、requirements.txt、
#       tests/run_tests.py）都相对**项目根**解析，所以下面先 cd 到上一级。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

# Windows 的 Git Bash 里解释器在 Scripts/，类 Unix 在 bin/
if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  echo "[x] 没找到虚拟环境 .venv"
  echo
  echo "    先建一个："
  echo "      python -m venv .venv"
  echo "      .venv/bin/python -m pip install -r requirements.txt      # 类 Unix"
  echo "      .venv/Scripts/python.exe -m pip install -r requirements.txt  # Git Bash"
  echo
  echo "    或者直接跑 ./scripts/setup_venv.sh 一步到位。"
  exit 2
fi

if [ "${1:-}" = "--reinstall" ]; then
  echo "==> 同步依赖"
  "$PY" -m pip install -r requirements.txt
  shift
fi

if [ "${1:-}" = "--real" ]; then
  echo "==> 含真实 API 用例（会消耗 token）"
  # 真实用例自带开关：由 run_tests.py 的 --real 决定，这里只做提示
  shift
  exec "$PY" tests/run_tests.py --real "$@"
fi

echo "==> 解释器：$PY"
exec "$PY" tests/run_tests.py "$@"
