#!/usr/bin/env bash
# 直接從 git repo 部署 RAGFlow：git pull 之後一個指令就套用，不用打包、解壓。
#
# 為什麼要另外一個執行目錄：repo 的 docker/.env 是上游進版控的範本（公開預設密碼），compose
# 又用 env_file: .env 把它灌進容器，不能原地改。所以部署檔同步到 <repo>/runtime/（可用
# RAGFLOW_RUNTIME_DIR 改位置），結構跟部署包一樣：runtime/docker/、start.sh、stop.sh、
# gen-env.sh、setup-models.sh。runtime/docker/.env 一旦建立就不會被同步覆蓋。
# 執行目錄裡放一個內容是 * 的 .gitignore：裡面有帳密、帳密備份與 API key，放在哪都不會被 git 看見。
#
# 用法（在 repo 任何位置執行皆可）：
#   agentwiki/repo-deploy.sh init [--admin-email a@b.c] [--import-env <既有 docker/.env>]
#                                   建立 runtime/ 與 .env（帳密自動產生）。已經用部署包裝過、要接手
#                                   同一份資料時，用 --import-env 帶入那份 .env（密碼必須一致）
#   agentwiki/repo-deploy.sh up      同步部署檔並啟動（預設動作）
#   agentwiki/repo-deploy.sh update  git pull --ff-only → 同步 → 有變動才重啟受影響的服務
#   agentwiki/repo-deploy.sh build   在本機 build app image（需要外網），再 up
#   agentwiki/repo-deploy.sh apply [--dry-run] [--recreate] [--force]
#                                   改了 .env 之後用：比對上次成功套用的版本，依改到的鍵決定怎麼做
#                                   （只給 setup-models 用的鍵不重啟；網段要 down 再 up；其他 up，
#                                   compose 只重建設定有變的容器）。--recreate 一律強制重建 ragflow-cpu；
#                                   密碼與 project 名稱只在初始化時生效，改了會拒絕，--force 才照做
#   agentwiki/repo-deploy.sh down | status | logs [服務]
#   agentwiki/repo-deploy.sh setup-models [參數]   轉給 runtime/setup-models.sh
#
# image：預設用本機的 agentwiki-ragflow:<最近的 v* tag>。沒有的話先從部署包載入
#   （docker load -i <包>/images.tar），或 agentwiki/repo-deploy.sh build。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
RT="${RAGFLOW_RUNTIME_DIR:-$ROOT/runtime}"
ENV_FILE="$RT/docker/.env"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
ylw(){ printf '\033[33m%s\033[0m\n' "$*"; }
red(){ printf '\033[31m%s\033[0m\n' "$*" >&2; }

command -v docker >/dev/null || { red "需要 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { red "需要 Docker Compose v2"; exit 1; }

getv(){ { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -1 | cut -d= -f2-; }
set_kv(){
  local f="$1" k="$2" v="$3"
  if grep -qE "^${k}=" "$f"; then
    awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k{print k"="v; next} {print}' "$f" > "$f.tmp" \
      && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
  else
    printf '%s=%s\n' "$k" "$v" >> "$f"
  fi
}
compose(){
  local project; project="$(getv COMPOSE_PROJECT_NAME)"
  local files=(-f "$RT/docker/docker-compose.yml")
  # start.sh 產生的網段覆寫檔：少了它 compose 會以為網路設定變了
  [ -f "$RT/docker/agentwiki-network.override.yml" ] && files+=(-f "$RT/docker/agentwiki-network.override.yml")
  docker compose -p "${project:-ragflow-v100}" --project-directory "$RT/docker" \
    --env-file "$ENV_FILE" "${files[@]}" "$@"
}
default_image(){
  local ver; ver="$(git -C "$ROOT" describe --tags --abbrev=0 --match='v*' 2>/dev/null || echo latest)"
  echo "agentwiki-ragflow:${ver}"
}
need_env(){ [ -f "$ENV_FILE" ] || { red "還沒初始化：先跑 agentwiki/repo-deploy.sh init"; exit 1; }; }

# 同步部署檔到 runtime/。輸出有變動的 docker/ 檔案（相對路徑），供 update 決定重啟範圍。
# 被覆寫的舊檔備份到 runtime/docker/.bak-<時間>/；docker/.env 永遠不動。
CHANGED_DOCKER=()
sync_files(){
  mkdir -p "$RT/docker"
  [ -f "$RT/.gitignore" ] || printf '# repo-deploy.sh 的執行目錄：含帳密，整個目錄不進版控\n*\n' > "$RT/.gitignore"
  local bk="$RT/docker/.bak-$(date +%Y%m%d-%H%M%S)" f rel dst
  CHANGED_DOCKER=()
  while IFS= read -r -d '' f; do
    rel="${f#docker/}"
    # 官方 .env 是範本：同步成 .env.example，真正的 .env 由 gen-env.sh 產生
    [ "$rel" = ".env" ] && dst="$RT/docker/.env.example" || dst="$RT/docker/$rel"
    if [ -f "$dst" ] && cmp -s "$ROOT/$f" "$dst"; then continue; fi
    if [ -f "$dst" ]; then
      mkdir -p "$bk/$(dirname "$rel")"; cp -p "$dst" "$bk/$rel"
    fi
    mkdir -p "$(dirname "$dst")"; cp -p "$ROOT/$f" "$dst"
    CHANGED_DOCKER+=("$rel")
  done < <(git -C "$ROOT" ls-files -z docker)
  cp -p "$HERE/.env.example" "$RT/agentwiki.env.example"
  for s in gen-env.sh setup-models.sh start.sh stop.sh; do
    cp -p "$HERE/$s" "$RT/$s"; chmod +x "$RT/$s"
  done
}

# 新版本加的設定鍵：只補 .env 沒有的鍵，從不覆寫既有值（帳密在初始化後不能改）
merge_new_keys(){
  local src added=() line k
  for src in "$RT/docker/.env.example" "$RT/agentwiki.env.example"; do
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in ''|\#*) continue ;; esac
      k="${line%%=*}"
      grep -qE "^${k}=" "$ENV_FILE" && continue
      printf '%s\n' "$line" >> "$ENV_FILE"; added+=("$k")
    done < "$src"
  done
  [ "${#added[@]}" -eq 0 ] || ylw "  .env 補上新版本的設定鍵：${added[*]}"
}

ensure_image(){
  local img; img="$(getv RAGFLOW_IMAGE)"
  case "$img" in ""|infiniflow/*) img="$(default_image)"; set_kv "$ENV_FILE" RAGFLOW_IMAGE "$img" ;; esac
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    red "本機沒有 image $img。"
    red "  從部署包載入：docker load -i <ragflow-prod-*>/images.tar"
    red "  或在本機 build（需要外網）：agentwiki/repo-deploy.sh build"
    exit 1
  fi
  echo "  image：$img"
}

# 上次成功套用的 .env 快照（含帳密，600；runtime/ 整個不進版控）。apply 拿它比對改了哪些鍵。
APPLIED="$RT/.env.applied"
save_applied(){ cp "$ENV_FILE" "$APPLIED" && chmod 600 "$APPLIED"; }
# 有變動的鍵（新增、刪除、改值都算）
env_changes(){
  local norm='/^[A-Za-z_][A-Za-z0-9_]*=/p'
  diff <(sed -n "$norm" "$APPLIED" | sort) <(sed -n "$norm" "$ENV_FILE" | sort) \
    | sed -n 's/^[<>] \([^=]*\)=.*/\1/p' | sort -u
}

cmd="${1:-up}"; [ $# -gt 0 ] && shift
case "$cmd" in
  init)
    import=""; pass=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --import-env) import="${2:?--import-env 需要檔案路徑}"; shift ;;
        *) pass+=("$1") ;;
      esac
      shift
    done
    bold "==> 同步部署檔到 $RT"
    sync_files
    if [ -f "$ENV_FILE" ]; then
      ylw "  $ENV_FILE 已存在，不重建（要重來請先移走它）"
    elif [ -n "$import" ]; then
      [ -f "$import" ] || { red "找不到 $import"; exit 1; }
      cp -p "$import" "$ENV_FILE"; chmod 600 "$ENV_FILE"
      grn "  已帶入 $import（帳密與 project 名稱沿用，接手同一份資料）"
      merge_new_keys
    fi
    "$RT/gen-env.sh" --dir "$RT" ${pass[@]+"${pass[@]}"}
    ensure_image
    grn "完成。啟動：agentwiki/repo-deploy.sh up"
    ;;
  up)
    need_env
    bold "==> 同步部署檔"
    sync_files
    [ "${#CHANGED_DOCKER[@]}" -eq 0 ] || echo "  更新：${CHANGED_DOCKER[*]}"
    merge_new_keys
    ensure_image
    "$RT/start.sh"
    save_applied
    ;;
  update)
    need_env
    bold "==> git pull --ff-only（$(git -C "$ROOT" branch --show-current)）"
    before="$(git -C "$ROOT" rev-parse HEAD)"
    git -C "$ROOT" pull --ff-only
    after="$(git -C "$ROOT" rev-parse HEAD)"
    [ "$before" = "$after" ] && echo "  已是最新（${after:0:9}）" || echo "  ${before:0:9} → ${after:0:9}"
    bold "==> 同步部署檔"
    sync_files
    merge_new_keys
    ensure_image
    if [ "${#CHANGED_DOCKER[@]}" -eq 0 ]; then
      grn "  docker/ 沒有變動（腳本已更新）；容器不需要重啟"
      # 容器沒在跑時順手啟動
      [ -n "$(compose ps -q 2>/dev/null)" ] || "$RT/start.sh"
      save_applied
    else
      echo "  docker/ 變動：${CHANGED_DOCKER[*]}"
      # compose up -d 會重建設定有變的服務；掛進容器的檔案（entrypoint.sh、
      # service_conf.yaml.template）內容變了 compose 看不出來，要另外重啟 app
      "$RT/start.sh"
      if printf '%s\n' "${CHANGED_DOCKER[@]}" | grep -qE '^(entrypoint\.sh|service_conf\.yaml\.template)$'; then
        bold "==> 掛載檔變動，重啟 ragflow-cpu"
        compose restart ragflow-cpu
        "$RT/start.sh"
      fi
      save_applied
    fi
    ;;
  build)
    bold "==> 本機 build app image（需要外網；可加 GH_PROXY / NEED_MIRROR）"
    BUILD_ONLY=1 "$HERE/pack-prod.sh"
    if [ -f "$ENV_FILE" ]; then
      set_kv "$ENV_FILE" RAGFLOW_IMAGE "$(default_image)"
      "$0" up
    else
      grn "image 已 build。接著：agentwiki/repo-deploy.sh init && agentwiki/repo-deploy.sh up"
    fi
    ;;
  apply)
    need_env
    dry=0; recreate=0; force=0
    for a in "$@"; do
      case "$a" in
        --dry-run|-n) dry=1 ;; --recreate) recreate=1 ;; --force) force=1 ;;
        *) red "apply 不認得的參數：$a"; exit 2 ;;
      esac
    done
    setup=(); danger=(); net=(); runtime=()
    if [ -f "$APPLIED" ]; then
      while IFS= read -r k; do
        [ -n "$k" ] || continue
        case "$k" in
          # 只有 setup-models.sh 讀；PICKED 是 start.sh 自己寫的
          RAGFLOW_SETUP_*|RAGFLOW_SERVICE_USER_*|RAGFLOW_KB_API_KEY|RAGFLOW_NETWORK_SUBNET_PICKED) setup+=("$k") ;;
          # 只在資料初始化時生效；project 名稱換了等於另起一套空的
          COMPOSE_PROJECT_NAME|*_PASSWORD|MINIO_USER|DEFAULT_SUPERUSER_EMAIL) danger+=("$k") ;;
          RAGFLOW_NETWORK_SUBNET) net+=("$k") ;;
          *) runtime+=("$k") ;;
        esac
      done < <(env_changes)
      bold "==> .env 與上次套用的版本相比"
      [ "${#setup[@]}" -eq 0 ]   || echo "  只給 setup-models 用：${setup[*]}"
      [ "${#net[@]}" -eq 0 ]     || echo "  網段：${net[*]}"
      [ "${#runtime[@]}" -eq 0 ] || echo "  容器設定：${runtime[*]}"
      [ "${#danger[@]}" -eq 0 ]  || red  "  只在初始化時生效：${danger[*]}"
      if [ $((${#setup[@]} + ${#net[@]} + ${#runtime[@]} + ${#danger[@]})) -eq 0 ]; then
        echo "  沒有變動"
      fi
    else
      bold "==> 沒有上次套用的紀錄（$APPLIED）"
      echo "  照 up 處理：compose 只重建設定有變的容器。成功後記下這次的 .env，之後就能精確比對。"
      runtime=("(未知)")
    fi

    if [ "${#danger[@]}" -gt 0 ] && [ "$force" != 1 ]; then
      red "拒絕套用：帳密與 project 名稱只在第一次初始化時生效。已有資料時改了，只會讓服務連不上"
      red "資料庫，不會換掉密碼；改 COMPOSE_PROJECT_NAME 則會另起一套空的 RAGFlow。"
      red "請把這些鍵改回原值（備份在 $(dirname "$(readlink -f "$ENV_FILE")")/ragflow.env.bak-*）。"
      red "確定要照做（例如還沒有任何資料）：加 --force"
      exit 1
    fi

    if [ "${#net[@]}" -gt 0 ]; then plan="down → up（重建網路）"
    elif [ "${#runtime[@]}" -gt 0 ] || [ "${#danger[@]}" -gt 0 ]; then plan="up（重建設定有變的容器）"
    elif [ "$recreate" = 1 ]; then plan="強制重建 ragflow-cpu"
    else plan="不需要重啟"
    fi
    [ "$recreate" = 1 ] && [ "$plan" != "強制重建 ragflow-cpu" ] && plan="$plan，再強制重建 ragflow-cpu"
    echo "  動作：$plan"
    if [ "${#setup[@]}" -gt 0 ]; then
      ylw "  setup-models 用的設定改了：要套用請再跑 agentwiki/repo-deploy.sh setup-models --public-host <主機>"
    fi
    [ "$dry" = 1 ] && exit 0

    sync_files
    case "$plan" in
      down*) "$RT/stop.sh"; merge_new_keys; ensure_image; "$RT/start.sh" ;;
      up*)   merge_new_keys; ensure_image; "$RT/start.sh" ;;
    esac
    if [ "$recreate" = 1 ]; then
      bold "==> 強制重建 ragflow-cpu"
      compose up -d --force-recreate ragflow-cpu
      "$RT/start.sh"
    fi
    save_applied
    grn "完成。這次的 .env 已記為套用基準。"
    ;;
  down)    need_env; "$RT/stop.sh" ;;
  status)  need_env; compose ps ;;
  logs)    need_env; compose logs -f --tail=200 "${@:-ragflow-cpu}" ;;
  setup-models) need_env; sync_files; "$RT/setup-models.sh" "$@" ;;
  -h|--help|help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) red "不認得的動作：$cmd（help 看用法）"; exit 2 ;;
esac
