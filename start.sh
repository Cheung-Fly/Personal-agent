#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "已生成 .env，请先填写 LLM_API_KEY 等配置再重新运行"; exit 1; }
pip install -r requirements.txt
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000
