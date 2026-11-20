#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "未找到 uv，请先安装 uv 并重新打开终端。" >&2
    exit 1
fi

# 显式选择项目环境，避免使用终端中已激活的其他虚拟环境。
export UV_PROJECT_ENVIRONMENT="$PWD/.venv"
uv sync --frozen --no-dev --python 3.12
if [ ! -f .env ]; then cp .env.example .env; fi
exec uv run --no-sync python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
