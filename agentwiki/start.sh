#!/usr/bin/env bash
# 部署端：啟動 RAGFlow（compose project 取自 docker/.env 的 COMPOSE_PROJECT_NAME），
# 等到 API 健康檢查通過才結束。
#
# 用法：./start.sh            # 啟動並等待就緒（預設最多 900 秒）
#       START_TIMEOUT=1800 ./start.sh
set -euo pipefail
cd "$(dirname "$0")/docker"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
red(){ printf '\033[31m%s\033[0m\n' "$*"; }

[ -f .env ] || { red "找不到 docker/.env，請先執行 ./load.sh"; exit 1; }
getv(){ grep -E "^$1=" .env | tail -1 | cut -d= -f2-; }

PROJECT="$(getv COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-ragflow-v100}"
API_PORT="$(getv SVR_HTTP_PORT)"; WEB_PORT="$(getv SVR_WEB_HTTP_PORT)"
COMPOSE=(docker compose -p "$PROJECT" --env-file .env -f docker-compose.yml)

# Elasticsearch 需要 vm.max_map_count >= 262144，否則 es01 起不來、整組卡住
mmc="$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)"
if [ "$mmc" -lt 262144 ]; then
  ylw "⚠ vm.max_map_count=$mmc，Elasticsearch 需要至少 262144。請先執行："
  echo "    sudo sysctl -w vm.max_map_count=262144"
  echo "    echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-ragflow.conf   # 開機後保留"
  exit 1
fi

# 埠號衝突檢查：被別的程式（例如舊版 RAGFlow）佔用就停下，不要讓 compose 啟到一半
running="$("${COMPOSE[@]}" ps -q 2>/dev/null | wc -l)"
if [ "$running" -eq 0 ] && command -v ss >/dev/null; then
  busy=()
  for k in SVR_WEB_HTTP_PORT SVR_WEB_HTTPS_PORT SVR_HTTP_PORT ADMIN_SVR_HTTP_PORT ES_PORT \
           EXPOSE_MYSQL_PORT MINIO_PORT MINIO_CONSOLE_PORT KVROCKS_PORT EXPOSE_NATS_PORT \
           EXPOSE_NATS_MONITORING_PORT; do
    p="$(getv "$k")"; [ -n "$p" ] || continue
    ss -ltnH "sport = :$p" 2>/dev/null | grep -q . && busy+=("$k=$p")
  done
  if [ "${#busy[@]}" -gt 0 ]; then
    red "以下 host 埠號已被佔用：${busy[*]}"
    echo "請在 docker/.env 改成未使用的埠號後重跑。"
    exit 1
  fi
fi

bold "==> docker compose up -d（project: $PROJECT）"
"${COMPOSE[@]}" up -d

bold "==> 等待 API 就緒：http://127.0.0.1:${API_PORT}/api/v1/system/healthz"
deadline=$(( $(date +%s) + ${START_TIMEOUT:-900} ))
until curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/api/v1/system/healthz" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    red "逾時仍未就緒。查看：cd docker && ${COMPOSE[*]} logs --tail=200 ragflow-cpu"
    exit 1
  fi
  sleep 5
done

ver="$(curl -fsS --max-time 5 "http://127.0.0.1:${API_PORT}/api/v1/system/version" 2>/dev/null || true)"
grn "RAGFlow 已就緒 ${ver:+（$ver）}"
echo "  Web：http://<本機>:${WEB_PORT}"
echo "  API：http://<本機>:${API_PORT}/api/v1"
echo "  停止：./stop.sh"
