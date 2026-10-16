#!/usr/bin/env bash
# ============================================================
# 一步建好本地虚拟环境（Git Bash / WSL / macOS / Linux）
#
#   ./scripts/setup_venv.sh            自动挑一个 Python
#   ./scripts/setup_venv.sh python3.12 指定解释器
#
# 本脚本住在 scripts/，但 .venv 与 requirements.txt 都在项目根，
# 所以先 cd 到上一级。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

CANDIDATES=("${1:-}" python3 python)
BASEPY=""
for c in "${CANDIDATES[@]}"; do
  [ -z "$c" ] && continue
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys;raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null; then
      BASEPY="$(command -v "$c")"
      break
    fi
  fi
done

if [ -z "$BASEPY" ]; then
  echo "[x] 找不到 Python 3.10+。装一个再回来，或用参数指定：./setup_venv.sh python3.13"
  exit 2
fi

echo "==> 基础解释器：$BASEPY  ($("$BASEPY" -V 2>&1))"

if [ -x ".venv/bin/python" ] || [ -x ".venv/Scripts/python.exe" ]; then
  echo "==> .venv 已存在，跳过创建"
else
  echo "==> 创建 .venv"
  "$BASEPY" -m venv .venv
fi

if [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"; else PY=".venv/bin/python"; fi

echo "==> 升级 pip"
"$PY" -m pip install --upgrade pip -q

echo "==> 安装依赖"
"$PY" -m pip install -r requirements.txt

echo "==> 以可编辑模式安装本项目（提供 mini-agent 命令）"
"$PY" -m pip install -e . -q

echo
echo "==> 自检"
"$PY" -m core --selftest >/dev/null && echo "    工具层自检通过"

echo
echo "完成。接下来："
echo "  1) 复制 .env.example 为 .env，填 OPENAI_API_KEY"
echo "  2) 激活环境：source .venv/bin/activate   （Git Bash: source .venv/Scripts/activate）"
echo "  3) 跑测试：./scripts/run_tests.sh      进控制台：python -m core"
