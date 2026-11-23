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
getv(){ { grep -E "^$1=" .env || true; } | tail -1 | cut -d= -f2-; }

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

# 埠號衝突：被別的程式（例如同一台上的舊版 RAGFlow）佔用時，預設自動換成往上找到的第一個
# 空埠並寫回 .env（AUTO_PORTS=0 改成只報錯停下）。這些都是 host 端埠號，容器之間用服務名稱
# 加固定的容器內埠號連線，換了不影響內部連線；只有外部連進來的位址會變。
# 只在這個 project 沒有容器在跑時檢查：在跑的話，埠號就是自己佔的。
PORT_KEYS=(SVR_WEB_HTTP_PORT SVR_WEB_HTTPS_PORT SVR_HTTP_PORT ADMIN_SVR_HTTP_PORT ES_PORT
           EXPOSE_MYSQL_PORT MINIO_PORT MINIO_CONSOLE_PORT KVROCKS_PORT EXPOSE_NATS_PORT
           EXPOSE_NATS_MONITORING_PORT)
port_busy(){ ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
# 寫回 .env：用 cat 覆寫，保留權限，也保留 .env 是指向 shared/env 的 symlink
set_kv(){ awk -v k="$1" -v v="$2" -F= 'BEGIN{OFS="="} $1==k{print k"="v; next} {print}' .env > .env.tmp \
  && cat .env.tmp > .env && rm -f .env.tmp; }
running="$("${COMPOSE[@]}" ps -q 2>/dev/null | wc -l)"
if [ "$running" -eq 0 ]; then
  if ! command -v ss >/dev/null; then
    ylw "⚠ 沒有 ss 指令，無法檢查埠號衝突"
  else
    taken=" "; busy=()
    for k in "${PORT_KEYS[@]}"; do
      p="$(getv "$k")"; [ -n "$p" ] || continue
      if port_busy "$p"; then busy+=("$k"); else taken="$taken$p "; fi
    done
    if [ "${#busy[@]}" -gt 0 ]; then
      if [ "${AUTO_PORTS:-1}" != "1" ]; then
        red "以下 host 埠號已被佔用：$(for k in "${busy[@]}"; do printf '%s=%s ' "$k" "$(getv "$k")"; done)"
        echo "請在 docker/.env 改成未使用的埠號後重跑（或拿掉 AUTO_PORTS=0 讓 start.sh 自動換）。"
        exit 1
      fi
      bk="$(readlink -f .env).bak-$(date +%Y%m%d-%H%M%S)"; cp -p .env "$bk"
      ylw "==> 以下 host 埠號已被其他程式佔用，自動改用空埠（原 .env 備份在 $bk）："
      for k in "${busy[@]}"; do
        old="$(getv "$k")"; new=$((old + 1))
        # 往上找：沒人在聽、也沒被這份 .env 的其他鍵用掉
        while [ "$new" -le 65535 ] && { port_busy "$new" || [[ "$taken" == *" $new "* ]]; }; do
          new=$((new + 1))
        done
        [ "$new" -le 65535 ] || { red "找不到 $k 可用的埠"; exit 1; }
        set_kv "$k" "$new"; taken="$taken$new "
        echo "    $k：$old → $new"
      done
      API_PORT="$(getv SVR_HTTP_PORT)"; WEB_PORT="$(getv SVR_WEB_HTTP_PORT)"
      if printf '%s\n' "${busy[@]}" | grep -qx SVR_HTTP_PORT; then
        ylw "    ★ API 埠變了：kb-stack 的 RAGFLOW_BASE_URL 要用 :${API_PORT}（./setup-models.sh 會印出新值）"
      fi
    fi
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
