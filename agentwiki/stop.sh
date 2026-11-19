#!/usr/bin/env bash
# 部署端：停止 RAGFlow。資料（docker volume）保留，下次 ./start.sh 會接續使用。
set -euo pipefail
cd "$(dirname "$0")/docker"
[ -f .env ] || { echo "找不到 docker/.env"; exit 1; }
PROJECT="$(grep -E '^COMPOSE_PROJECT_NAME=' .env | tail -1 | cut -d= -f2-)"
PROJECT="${PROJECT:-ragflow-v100}"
echo "==> docker compose down（project: $PROJECT，volume 保留）"
docker compose -p "$PROJECT" --env-file .env -f docker-compose.yml down
