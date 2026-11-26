#!/usr/bin/env bash
# 部署端：完整包一鍵安裝。前置檢查 → 驗證 sha256 → load.sh → start.sh → setup-models.sh。
# 每一步都可以重跑：已載入的 image、已產生的帳密、已建立的服務帳號與模型都會沿用。
#
# 用法（在解開的完整包目錄裡）：
#   ./install.sh
#   ./install.sh --admin-email ops@example.com \
#                --embedding-url http://<embedding主機>:<埠>/v1 --embedding-model bge-m3
#   ./install.sh --check              # 只做前置檢查，不動任何東西
#   ./install.sh --no-setup-models    # 只裝到啟動為止，模型之後再跑 ./setup-models.sh
#
# 參數：
#   --admin-email <email>      第一個超級管理員（只在第一次初始化時生效，預設 admin@ragflow.io）
#   --embedding-url <url>      OpenAI 相容 embedding base URL，要含 /v1（寫進 docker/.env）
#   --embedding-model <名稱>   跟 embedding 服務回報的一致（寫進 docker/.env）
#   --embedding-key <key>      embedding 服務的 key（沒有驗證就不給）
#   --public-host <主機>       印給 kb-stack 的 RAGFLOW_BASE_URL 用這個主機名（預設本機 IP）
#   --env-store <路徑> / --no-env-store   原樣轉給 gen-env.sh
#   --skip-sha                 不找、不驗 ../<包名>.tar.gz.sha256
#   --check                    只做前置檢查
#   --no-setup-models          不跑 setup-models.sh
#   -y                         不詢問
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
BUNDLE="$(basename "$HERE")"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
red(){ printf '\033[31m%s\033[0m\n' "$*"; }

ADMIN_EMAIL=""; EMB_URL=""; EMB_MODEL=""; EMB_KEY=""; PUBLIC_HOST=""
CHECK_ONLY=0; SETUP_MODELS=1; SKIP_SHA=0; ASSUME_YES=0
GEN_ENV_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-email) ADMIN_EMAIL="${2:?--admin-email 需要一個值}"; shift ;;
    --embedding-url) EMB_URL="${2:?--embedding-url 需要一個值}"; shift ;;
    --embedding-model) EMB_MODEL="${2:?--embedding-model 需要一個值}"; shift ;;
    --embedding-key) EMB_KEY="${2:?--embedding-key 需要一個值}"; shift ;;
    --public-host) PUBLIC_HOST="${2:?--public-host 需要一個值}"; shift ;;
    --env-store) GEN_ENV_ARGS+=(--env-store "${2:?--env-store 需要檔案路徑}"); shift ;;
    --no-env-store) GEN_ENV_ARGS+=(--no-env-store) ;;
    --skip-sha) SKIP_SHA=1 ;;
    --check) CHECK_ONLY=1 ;;
    --no-setup-models) SETUP_MODELS=0 ;;
    -y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) red "不認得的參數：$1（--help 看用法）"; exit 2 ;;
  esac
  shift
done
if [ -n "$ADMIN_EMAIL" ]; then GEN_ENV_ARGS+=(--admin-email "$ADMIN_EMAIL"); fi

for f in images.tar image.env load.sh gen-env.sh start.sh setup-models.sh docker/docker-compose.yml; do
  [ -e "$f" ] || { red "包內缺少 $f：這不是完整包（更新包請用 ./update.sh <部署目錄>）"; exit 1; }
done

getinfo(){ { grep -E "^$1=" BUILD_INFO 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }

# ---------------------------------------------------------------- 前置檢查
bold "==> [1/5] 前置檢查"
FAIL=0
# 提示行用 if 而不是 [ ] && echo：後者在沒有提示時回傳 1，set -e 會讓整支腳本在「只是警告」時就結束
need(){ if eval "$2"; then grn "  ✓ $1"; else red "  ✗ $1"; if [ -n "${3:-}" ]; then echo "      $3"; fi; FAIL=1; fi; return 0; }
warnif(){ if eval "$2"; then grn "  ✓ $1"; else ylw "  ⚠ $1"; if [ -n "${3:-}" ]; then echo "      $3"; fi; fi; return 0; }

need "docker 可用" "docker info >/dev/null 2>&1" "裝 Docker Engine，並確認目前帳號在 docker 群組（或用 sudo 執行）"
need "Docker Compose v2" "docker compose version >/dev/null 2>&1" "需要 docker compose plugin（v2），不是舊的 docker-compose"
warnif "python3（setup-models.sh 需要）" "command -v python3 >/dev/null 2>&1" "沒有 python3 時只能裝到啟動為止，模型改在 Web 設定"
mmc="$(cat /proc/sys/vm/max_map_count 2>/dev/null || echo 0)"
need "vm.max_map_count=$mmc（需要 >= 262144）" "[ $mmc -ge 262144 ]" \
  "sudo sysctl -w vm.max_map_count=262144 && echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-ragflow.conf"

HOST_ARCH="$(docker info --format '{{.Architecture}}' 2>/dev/null || uname -m)"
PKG_ARCH="$(getinfo ARCH)"
case "$HOST_ARCH" in x86_64) HOST_ARCH=amd64 ;; aarch64) HOST_ARCH=arm64 ;; esac
if [ -n "$PKG_ARCH" ]; then
  need "CPU 架構一致（包：$PKG_ARCH，主機：$HOST_ARCH）" "[ '$PKG_ARCH' = '$HOST_ARCH' ]" "這個包不能在這台機器上跑，要在同架構的建置機重打"
fi

DROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
IMG_GB=$(( ( $(stat -c %s images.tar) + 1073741823 ) / 1073741824 ))
NEED_GB=$(( IMG_GB * 2 + 20 ))   # 解開後的 layer 約為 tar 的 1~2 倍，再留 20 GB 給資料
FREE_GB="$(df -BG --output=avail "$DROOT" 2>/dev/null | tail -1 | tr -dc 0-9 || true)"
if [ -n "$FREE_GB" ]; then
  warnif "Docker 目錄 $DROOT 可用 ${FREE_GB}G（建議 >= ${NEED_GB}G）" "[ $FREE_GB -ge $NEED_GB ]" \
    "空間不夠時 docker load 會中途失敗；Elasticsearch 在磁碟使用超過 90% 時也會把索引設成唯讀"
fi
MEM_GB=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1048576 ))
warnif "記憶體 ${MEM_GB}G（官方建議 >= 16G）" "[ $MEM_GB -ge 15 ]"

if [ "$SKIP_SHA" = "0" ]; then
  SHA="../${BUNDLE}.tar.gz.sha256"
  if [ -f "$SHA" ]; then
    need "sha256 驗證 ${BUNDLE}.tar.gz" "( cd .. && sha256sum -c --quiet '$(basename "$SHA")' >/dev/null 2>&1 )" \
      "檔案在傳輸中損壞，請重傳 tar.gz"
  else
    ylw "  ⚠ 找不到 $SHA，跳過 sha256 驗證（解包前請自行 sha256sum -c）"
  fi
fi

if [ -f BUILD_INFO ]; then
  echo "  包資訊：$(getinfo VERSION)  commit $(getinfo GIT_COMMIT)  打包於 $(getinfo PACKED_AT)"
fi
[ "$FAIL" = "0" ] || { red "前置檢查沒過，請依上面的提示修正後重跑"; exit 1; }
if [ "$CHECK_ONLY" = "1" ]; then grn "前置檢查通過（--check，不做其他事）"; exit 0; fi

if [ "$ASSUME_YES" = "0" ] && [ -t 0 ]; then
  printf '開始安裝 %s？[y/N] ' "$BUNDLE"; read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "取消"; exit 1 ;; esac
fi

# ---------------------------------------------------------------- 載入 image + .env
bold "==> [2/5] 載入 image、建立 docker/.env（load.sh）"
./load.sh ${GEN_ENV_ARGS[@]+"${GEN_ENV_ARGS[@]}"}

# 有給 embedding 參數就寫進 .env，之後重跑 setup-models.sh 不必再帶。
# 用 cat 覆寫：保留 symlink（真正的檔案在 /opt/AgentWiki/shared/env）與 600 權限
set_kv(){ local f="$1" k="$2" v="$3"; if grep -qE "^${k}=" "$f"; then
  awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k{print k"="v; next} {print}' "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
  else printf "%s=%s\n" "$k" "$v" >> "$f"; fi; }
ENV_FILE=docker/.env
if [ -n "$EMB_URL" ]; then set_kv "$ENV_FILE" RAGFLOW_SETUP_EMBEDDING_BASE_URL "$EMB_URL"; fi
if [ -n "$EMB_MODEL" ]; then set_kv "$ENV_FILE" RAGFLOW_SETUP_EMBEDDING_MODEL "$EMB_MODEL"; fi
if [ -n "$EMB_KEY" ]; then set_kv "$ENV_FILE" RAGFLOW_SETUP_EMBEDDING_API_KEY "$EMB_KEY"; fi
getv(){ { grep -E "^$1=" "$ENV_FILE" || true; } | tail -1 | cut -d= -f2-; }

# ---------------------------------------------------------------- 啟動
bold "==> [3/5] 啟動並等待健康檢查（start.sh）"
./start.sh

# ---------------------------------------------------------------- 模型與 API key
bold "==> [4/5] 服務帳號、embedding 模型、API key（setup-models.sh）"
if [ "$SETUP_MODELS" = "0" ]; then
  ylw "  --no-setup-models：略過。之後執行 ./setup-models.sh"
elif ! command -v python3 >/dev/null 2>&1; then
  ylw "  沒有 python3：略過。請在 Web 的模型供應商頁設定，或裝 python3 後執行 ./setup-models.sh"
elif [ -z "$(getv RAGFLOW_SETUP_EMBEDDING_BASE_URL)" ] || [ -z "$(getv RAGFLOW_SETUP_EMBEDDING_MODEL)" ]; then
  ylw "  docker/.env 沒有 RAGFLOW_SETUP_EMBEDDING_BASE_URL / _MODEL：略過。"
  echo "    填好後執行 ./setup-models.sh，或重跑 ./install.sh --embedding-url ... --embedding-model ..."
else
  SM_ARGS=()
  if [ -n "$PUBLIC_HOST" ]; then SM_ARGS+=(--public-host "$PUBLIC_HOST"); fi
  ./setup-models.sh ${SM_ARGS[@]+"${SM_ARGS[@]}"}
fi

# ---------------------------------------------------------------- 結果
bold "==> [5/5] 完成"
WEB_PORT="$(getv SVR_WEB_HTTP_PORT)"; API_PORT="$(getv SVR_HTTP_PORT)"; ADM_PORT="$(getv ADMIN_SVR_HTTP_PORT)"
H="${PUBLIC_HOST:-$(hostname -I 2>/dev/null | awk '{print $1}')}"; H="${H:-127.0.0.1}"
echo "  Web   : http://${H}:${WEB_PORT:-18080}   （用服務帳號登入）"
echo "  API   : http://${H}:${API_PORT:-19380}"
echo "  Admin : http://${H}:${ADM_PORT:-19381}   （超級管理員只能登入這裡）"
echo "  帳密  : ./gen-env.sh --show"
if [ -f kb-stack.ragflow.env ]; then echo "  kb-stack 設定：$(pwd)/kb-stack.ragflow.env"; fi
echo "  停止  : ./stop.sh（volume 保留）"
