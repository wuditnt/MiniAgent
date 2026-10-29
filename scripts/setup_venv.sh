#!/usr/bin/env bash
# ============================================================
# mini-agent 环境搭建（Git Bash / WSL / macOS / Linux）
#
#   ./scripts/setup_venv.sh
#
#   代码迁移 / 部署到新机器后跑一次：
#     建 .venv  ->  装依赖  ->  生成 .env  ->  工具层自检
#
#   幂等：已存在的 .venv 直接复用，只补装 / 同步依赖。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
VENV="$ROOT/.venv"

# Windows 的 Git Bash 里解释器在 Scripts/，类 Unix 在 bin/
if [ -x "$VENV/Scripts/python.exe" ]; then
  PY="$VENV/Scripts/python.exe"
elif [ -x "$VENV/bin/python" ]; then
  PY="$VENV/bin/python"
else
  PY=""
fi

if [ -n "$PY" ]; then
  echo "==> 复用已存在的虚拟环境：$VENV"
else
  echo "==> 创建虚拟环境：$VENV"
  if command -v python3 >/dev/null 2>&1; then
    python3 -m venv "$VENV"
  elif command -v python >/dev/null 2>&1; then
    python -m venv "$VENV"
  else
    echo "[x] 未找到 python / python3，请先安装 Python 3.10+"
    exit 2
  fi
  if [ -x "$VENV/Scripts/python.exe" ]; then
    PY="$VENV/Scripts/python.exe"
  else
    PY="$VENV/bin/python"
  fi
fi

echo "==> 升级 pip"
"$PY" -m pip install --upgrade pip

echo "==> 安装依赖（requirements.txt）"
"$PY" -m pip install -r "$ROOT/requirements.txt"

echo "==> 安装本项目（可选，提供 mini-agent 命令）"
"$PY" -m pip install -e "$ROOT" -q || echo "[i] 可编辑安装失败，不影响从源码启动"

# .env 缺失时从模板生成（只含系统配置；模型在 Web UI 里配置）
if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "[i] 已由 .env.example 生成 .env（监听地址/端口等系统配置）"
fi

echo "==> 工具层自检（不需要 API Key）"
"$PY" "$ROOT/src/core/main.py" --selftest

echo
echo "==> 完成。接下来:"
echo "    1) 启动 --web，在浏览器「设置 → 模型」里配置模型（API Key 存本机 models.json）"
echo "    2) 启动开发后端: $PY src/core/main.py --web --port 8123 --host 0.0.0.0"
