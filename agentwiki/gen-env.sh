#!/usr/bin/env bash
# 部署端：備妥 RAGFlow 的 docker/.env，並自動產生帳號密碼。
#
#   1) docker/.env 不存在：以官方範本 docker/.env.example 為底，套上 agentwiki.env.example
#      （AgentWiki 的 project 名稱、埠號、帳密欄位）建立它。
#   2) 帳密欄位若是空值或官方公開預設值（infini_rag_flow / admin），換成隨機產生的值。
#   3) 印出管理員帳密，並把 docker/.env 權限改成 600。
#
# 用法（在部署目錄，也就是含 docker/ 的那一層）：
#   ./gen-env.sh                     # 建立 / 補齊 docker/.env，已有的值不動
#   ./gen-env.sh --dry-run           # 只列出會做什麼，不寫檔
#   ./gen-env.sh --admin-email a@b.c # 指定第一個超級管理員的 email
#   ./gen-env.sh --rotate            # 全部帳密重新產生（只在還沒有任何資料時允許）
#   ./gen-env.sh --show              # 印出 docker/.env 目前的帳密
#   ./gen-env.sh --dir <部署目錄>    # 不在部署目錄執行時指定
#
# ★ 這些密碼只在第一次啟動、資料初始化時生效。偵測到這個 compose project 已經有 volume
#   時，任何帳密都不會被改（改了只會讓服務連不上自己的資料）。
set -euo pipefail

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
red(){ printf '\033[31m%s\033[0m\n' "$*" >&2; }

DIR="$(cd "$(dirname "$0")" && pwd)"
DRY=0; ROTATE=0; SHOW=0; ADMIN_EMAIL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --rotate) ROTATE=1 ;;
    --show) SHOW=1 ;;
    --admin-email) ADMIN_EMAIL="${2:?--admin-email 需要一個值}"; shift ;;
    --dir) DIR="$(cd "${2:?--dir 需要一個目錄}" && pwd)"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) red "不認得的參數：$1（--help 看用法）"; exit 2 ;;
  esac
  shift
done

ENV_FILE="$DIR/docker/.env"
BASE_TEMPLATE="$DIR/docker/.env.example"
# 部署包裡叫 agentwiki.env.example；直接在 repo 的 agentwiki/ 下執行時是 .env.example
OVERLAY="$DIR/agentwiki.env.example"
[ -f "$OVERLAY" ] || OVERLAY="$(dirname "$0")/.env.example"

# 帳密欄位：產生器負責的全部鍵。DEFAULT_SUPERUSER_PASSWORD 是 1.0 的正式名稱，
# ADMIN_DEFAULT_PASSWORD 是舊名（官方範本仍是 admin），兩者寫成同一個值。
SECRET_KEYS=(ELASTIC_PASSWORD MYSQL_PASSWORD MINIO_PASSWORD REDIS_PASSWORD DEFAULT_SUPERUSER_PASSWORD)
PUBLIC_DEFAULTS='^(|infini_rag_flow|admin|CHANGE_ME)$'

# 在 repo 原始碼裡，docker/.env 是上游進版控的範本：絕不能拿來當部署用的 .env 改寫
if command -v git >/dev/null 2>&1 \
   && git -C "$DIR" ls-files --error-unmatch docker/.env >/dev/null 2>&1; then
  red "$ENV_FILE 是上游進版控的範本，不是部署用的 .env。"
  red "請在部署包解開的目錄執行（agentwiki/pack-prod.sh 打包後 tar -xzf 的那一層），或用 --dir 指定。"
  exit 1
fi

# 鍵不存在時回空字串且不回傳失敗（set -e + pipefail 下，失敗的 $(getv) 會直接結束腳本）
getv(){ { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
# 把 KEY=VAL 寫進 .env：有就取代、沒有就附加；值原樣寫入，不經 sed 解讀
set_kv(){
  local f="$1" k="$2" v="$3"
  if grep -qE "^${k}=" "$f"; then
    awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k{print k"="v; next} {print}' "$f" > "$f.tmp" \
      && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
  else
    printf '%s=%s\n' "$k" "$v" >> "$f"
  fi
}
# 只用英數字：值會進 compose 變數替換、YAML 與連線字串，特殊字元在這些地方各有轉義規則
gen_secret(){
  local s=""
  while [ "${#s}" -lt 24 ]; do
    s="$s$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 48 || true)"
  done
  printf '%s' "${s:0:24}"
}

if [ "$SHOW" = "1" ]; then
  [ -f "$ENV_FILE" ] || { red "找不到 $ENV_FILE"; exit 1; }
  bold "==> $ENV_FILE 的帳密"
  echo "  超級管理員：$(getv DEFAULT_SUPERUSER_EMAIL || true) / $(getv DEFAULT_SUPERUSER_PASSWORD || true)"
  echo "  服務帳號（Web 登入、kb-stack）：$(getv RAGFLOW_SERVICE_USER_EMAIL || true) / $(getv RAGFLOW_SERVICE_USER_PASSWORD || true)"
  echo "  kb-stack API key：$(getv RAGFLOW_KB_API_KEY || true)"
  for k in ELASTIC_PASSWORD MYSQL_PASSWORD MINIO_USER MINIO_PASSWORD REDIS_PASSWORD; do
    echo "  $k=$(getv "$k" || true)"
  done
  exit 0
fi

# ---- 1) 建立 docker/.env ----
CREATED=0
if [ ! -f "$ENV_FILE" ]; then
  [ -f "$BASE_TEMPLATE" ] || { red "找不到官方範本 $BASE_TEMPLATE"; exit 1; }
  [ -f "$OVERLAY" ] || { red "找不到 AgentWiki 範本（agentwiki.env.example）"; exit 1; }
  if [ "$DRY" = "1" ]; then
    echo "[dry-run] 會以 docker/.env.example 為底、套上 $(basename "$OVERLAY") 建立 $ENV_FILE"
  else
    cp "$BASE_TEMPLATE" "$ENV_FILE"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|\#*) continue ;; esac
      k="${line%%=*}"; v="${line#*=}"
      set_kv "$ENV_FILE" "$k" "$v"
    done < "$OVERLAY"
    CREATED=1
    grn "==> 已建立 $ENV_FILE（官方範本 + $(basename "$OVERLAY")）"
  fi
fi

# dry-run 且 .env 還不存在：後面沒有東西可以讀，到此為止
if [ ! -f "$ENV_FILE" ]; then
  echo "[dry-run] 之後會為 ${SECRET_KEYS[*]} 產生隨機值"
  exit 0
fi

# ---- 2) 資料已初始化就不准動帳密 ----
PROJECT="$(getv COMPOSE_PROJECT_NAME)"; PROJECT="${PROJECT:-ragflow-v100}"
HAS_DATA=0
if command -v docker >/dev/null 2>&1; then
  if [ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null)" ]; then
    HAS_DATA=1
  fi
else
  ylw "⚠ 找不到 docker 指令，無法確認 project「$PROJECT」是否已有資料；若已啟動過，請不要改帳密。"
fi

if [ "$HAS_DATA" = "1" ]; then
  weak=()
  for k in "${SECRET_KEYS[@]}"; do
    [[ "$(getv "$k")" =~ $PUBLIC_DEFAULTS ]] && weak+=("$k")
  done
  ylw "==> compose project「$PROJECT」已經有資料 volume：帳密已在初始化時固定，不做任何修改。"
  if [ "${#weak[@]}" -gt 0 ]; then
    ylw "    仍是空值或公開預設值：${weak[*]}"
    ylw "    要換掉只能備份資料後重建（./stop.sh 之後刪掉這個 project 的 volume 再重跑），"
    ylw "    或在各服務內部用它自己的指令改密碼，再同步改 docker/.env。"
  fi
  [ -z "$ADMIN_EMAIL" ] || ylw "    --admin-email 也已無效：超級管理員在第一次啟動時就建立了。"
  exit 0
fi

# ---- 3) 產生帳密 ----
changes=()
if [ -n "$ADMIN_EMAIL" ]; then
  changes+=("DEFAULT_SUPERUSER_EMAIL=$ADMIN_EMAIL")
fi
for k in "${SECRET_KEYS[@]}"; do
  cur="$(getv "$k")"
  if [ "$ROTATE" = "1" ] || [[ "$cur" =~ $PUBLIC_DEFAULTS ]]; then
    changes+=("$k=$(gen_secret)")
  fi
done

if [ "${#changes[@]}" -eq 0 ]; then
  grn "==> 帳密都已設定，沒有需要產生的。"
else
  if [ "$DRY" = "1" ]; then
    echo "[dry-run] 會設定：$(printf '%s ' "${changes[@]%%=*}")"
    exit 0
  fi
  if [ "$CREATED" = "0" ]; then
    bk="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$ENV_FILE" "$bk"; chmod 600 "$bk"
    echo "==> 修改前的 .env 備份在 $bk"
  fi
  for kv in "${changes[@]}"; do
    set_kv "$ENV_FILE" "${kv%%=*}" "${kv#*=}"
  done
  # 舊名 ADMIN_DEFAULT_PASSWORD 跟正式名稱寫成同一個值，免得 admin 這個公開預設值殘留
  set_kv "$ENV_FILE" ADMIN_DEFAULT_PASSWORD "$(getv DEFAULT_SUPERUSER_PASSWORD)"
  grn "==> 已產生：$(printf '%s ' "${changes[@]%%=*}")"
fi
chmod 600 "$ENV_FILE"

echo
bold "超級管理員（第一次啟動時建立；只能登入 Admin，Web 與 API key 用 ./setup-models.sh 建的服務帳號）"
echo "  帳號：$(getv DEFAULT_SUPERUSER_EMAIL)"
echo "  密碼：$(getv DEFAULT_SUPERUSER_PASSWORD)"
echo "  其他帳密都在 $ENV_FILE（權限 600），之後可用 ./gen-env.sh --show 查看。"
