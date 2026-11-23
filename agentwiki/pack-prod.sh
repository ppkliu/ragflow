#!/usr/bin/env bash
# 把 RAGFlow 正式環境打包成單一 .tar.gz，搬到另一台機器離線部署 / 更新。
# 做法比照 docmost/pack-prod.sh。輸出在 <repo>/dist/（已被 .gitignore 忽略）。
#
# 兩種包：
#   完整包（預設）：app image + compose 宣告的相依 image + 部署檔，首次部署用。
#   更新包（APP_ONLY=1）：只含 app image + 部署檔，換版用，資料保留。
#
# 用法（在 ragflow repo 根目錄或任何地方皆可）：
#   agentwiki/pack-prod.sh                 # 版本取最近的 v* tag（例如 v1.0.0）
#   agentwiki/pack-prod.sh v1.0.0          # 指定版本標籤
#   APP_ONLY=1 agentwiki/pack-prod.sh      # 只打包 app image（更新包）
#   SKIP_BUILD=1 agentwiki/pack-prod.sh    # 跳過 docker build（沿用既有 image）
#   NEED_MIRROR=1 agentwiki/pack-prod.sh   # build 時走官方 Dockerfile 的大陸鏡像
#   GH_PROXY=https://gh-proxy.com/ ...     # native library 改走 GitHub 下載鏡像
#
# 相依 image 的清單不是寫死的：用 docker compose config --images 依 docker/.env 的
# COMPOSE_PROFILES（預設 elasticsearch + cpu + metadata-mysql）算出來。改了
# DOC_ENGINE 之類的設定，清單會跟著變。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
grn(){ printf '\033[32m%s\033[0m\n' "$*"; }
red(){ printf '\033[31m%s\033[0m\n' "$*"; }

command -v docker >/dev/null || { red "需要 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { red "需要 Docker Compose v2"; exit 1; }

VERSION="${1:-$(git describe --tags --abbrev=0 --match='v*' 2>/dev/null || echo latest)}"
APP_IMAGE="${RAGFLOW_IMAGE:-agentwiki-ragflow:${VERSION}}"
APP_ONLY="${APP_ONLY:-0}"
OUT_DIR="$ROOT/dist"
if [ "$APP_ONLY" = "1" ]; then BUNDLE="ragflow-prod-update-${VERSION}"; else BUNDLE="ragflow-prod-${VERSION}"; fi
STAGE="${OUT_DIR}/${BUNDLE}"
TARBALL="${OUT_DIR}/${BUNDLE}.tar.gz"

bold "==> [1/6] build app image: ${APP_IMAGE}  (APP_ONLY=${APP_ONLY})"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "  SKIP_BUILD=1，沿用既有 image"
  docker image inspect "$APP_IMAGE" >/dev/null 2>&1 || { red "找不到 $APP_IMAGE，請先 build"; exit 1; }
else
  # Dockerfile 用 git describe 把版本寫進 image 的 /ragflow/VERSION，需要 v* tag 在本機
  git rev-parse -q --verify "refs/tags/${VERSION}" >/dev/null \
    || echo "  ⚠ 本機沒有 tag ${VERSION}，image 內的版本號會是 commit hash（git fetch upstream tag ${VERSION}）"
  # 官方 runner（:latest，不釘版本）缺 Go 靜態連結需要的 native library，先 build 補齊的衍生
  # runner，再用 --build-context 讓官方 Dockerfile 的 FROM 指向它（見 agentwiki/runner.Dockerfile）
  RUNNER_IMAGE="agentwiki-ragflow-runner:${VERSION}"
  echo "  build runner: ${RUNNER_IMAGE}"
  docker pull -q infiniflow/github_action_runner:latest >/dev/null
  echo "    base runner: $(docker image inspect infiniflow/github_action_runner:latest --format '{{index .RepoDigests 0}}' 2>/dev/null)"
  docker build -f "$HERE/runner.Dockerfile" --build-arg GH_PROXY="${GH_PROXY:-}" -t "$RUNNER_IMAGE" .
  docker build --build-arg NEED_MIRROR="${NEED_MIRROR:-0}" \
    --build-context "infiniflow/github_action_runner:latest=docker-image://${RUNNER_IMAGE}" \
    -t "$APP_IMAGE" .
fi
# 額外打 latest 標籤，讓手動改 .env 時也有一個固定名字可用
docker tag "$APP_IMAGE" agentwiki-ragflow:latest
# repo-deploy.sh build 用：只要本機 image，不打包
if [ "${BUILD_ONLY:-0}" = "1" ]; then
  grn "BUILD_ONLY=1：已 build ${APP_IMAGE}，不打包"
  exit 0
fi

bold "==> [2/6] 解析相依 image（完整包才會打包）"
DEP_IMAGES="$(RAGFLOW_IMAGE="$APP_IMAGE" docker compose --project-directory docker \
  -f docker/docker-compose.yml --env-file docker/.env config --images \
  | sort -u | grep -vxF "$APP_IMAGE" || true)"
[ -n "$DEP_IMAGES" ] || { red "compose 沒有列出任何相依 image，檢查 docker/.env 的 COMPOSE_PROFILES"; exit 1; }
SAVE_IMAGES="$APP_IMAGE agentwiki-ragflow:latest"
if [ "$APP_ONLY" = "1" ]; then
  echo "    APP_ONLY：只含 app，跳過相依 image"
else
  echo "$DEP_IMAGES" | sed 's/^/    /'
  for img in $DEP_IMAGES; do
    docker image inspect "$img" >/dev/null 2>&1 || { echo "  pull $img"; docker pull "$img"; }
  done
  SAVE_IMAGES="$SAVE_IMAGES $DEP_IMAGES"
fi

bold "==> [3/6] 準備部署檔（stage: ${STAGE}）"
# 暫存目錄若被拿去當部署目錄（有 docker/.env），刪掉就會連帳密一起丟失，資料將無法連線
if [ -f "$STAGE/docker/.env" ]; then
  red "$STAGE 裡有 docker/.env，看起來被當成部署目錄在用。請把部署搬到 dist/ 以外的地方再打包。"
  exit 1
fi
rm -rf "$STAGE"; mkdir -p "$STAGE/docker"
# 只複製進版控的 docker/ 檔案：執行期資料（docker/data、ragflow-logs…）不能被帶走
git ls-files -z docker | while IFS= read -r -d '' f; do
  rel="${f#docker/}"
  mkdir -p "$STAGE/docker/$(dirname "$rel")"
  cp -p "$f" "$STAGE/docker/$rel"
done
# 官方 docker/.env 是帶預設密碼的範本；包內改名成 .env.example，真正的 .env 在目標機產生
mv "$STAGE/docker/.env" "$STAGE/docker/.env.example"
echo "RAGFLOW_IMAGE=${APP_IMAGE}" > "$STAGE/image.env"
# AgentWiki 範本在 repo 裡是 agentwiki/.env.example；包內改名成看得見的 agentwiki.env.example
cp "$HERE/.env.example" "$STAGE/agentwiki.env.example"
cp "$HERE/gen-env.sh" "$HERE/setup-models.sh" "$HERE/start.sh" "$HERE/stop.sh" "$STAGE/"
chmod +x "$STAGE/gen-env.sh" "$STAGE/setup-models.sh" "$STAGE/start.sh" "$STAGE/stop.sh"

bold "==> [4/6] 匯出 image -> images.tar"
# shellcheck disable=SC2086
docker save $SAVE_IMAGES -o "$STAGE/images.tar"

bold "==> [5/6] 產生部署端腳本與說明"
# 兩種包共用：把 KEY=VAL 寫進 .env（有就取代、沒有就附加；值原樣寫入，不經 sed 解讀）。
# 用 cat 覆寫而不是 mv：保留 .env 的 600 權限（裡面有帳密）
SETKV='set_kv(){ local f="$1" k="$2" v="$3"; if grep -qE "^${k}=" "$f"; then
  awk -v k="$k" -v v="$v" -F= '"'"'BEGIN{OFS="="} $1==k{print k"="v; next} {print}'"'"' "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
  else printf "%s=%s\n" "$k" "$v" >> "$f"; fi; }'

if [ "$APP_ONLY" = "1" ]; then
  # ---- 更新包：只換 app image，資料保留 ----
  cat > "$STAGE/update.sh" <<UPD
#!/usr/bin/env bash
# 部署端：只換 RAGFlow app image，相依服務與資料保留。
# 用法：./update.sh <既有部署目錄>   （該目錄需含 docker/.env）
set -euo pipefail
# 先把部署目錄轉成絕對路徑再 cd，否則相對路徑會被解讀成相對於這個包的目錄
DEPLOY_DIR="\$(cd "\${1:?用法: ./update.sh <既有部署目錄，含 docker/.env>}" && pwd)"
cd "\$(dirname "\$0")"
ENV_FILE="\$DEPLOY_DIR/docker/.env"
[ -f "\$ENV_FILE" ] || { echo "在 \$DEPLOY_DIR/docker 找不到 .env"; exit 1; }
${SETKV}

echo "==> docker load 新 app image"
docker load -i images.tar
NEWTAG=\$(grep '^RAGFLOW_IMAGE=' image.env | cut -d= -f2-)

# 部署檔可能隨新版改變；被覆寫的檔案先備份，.env 永遠不動
BK="\$DEPLOY_DIR/docker/.bak-\$(date +%Y%m%d-%H%M%S)"
echo "==> 更新部署檔（被覆寫的舊檔備份到 \$BK）"
( cd docker && find . -type f ! -name .env ! -name .env.example ) | while read -r f; do
  if [ -f "\$DEPLOY_DIR/docker/\$f" ] && ! cmp -s "docker/\$f" "\$DEPLOY_DIR/docker/\$f"; then
    mkdir -p "\$BK/\$(dirname "\$f")"; cp -p "\$DEPLOY_DIR/docker/\$f" "\$BK/\$f"
  fi
  mkdir -p "\$DEPLOY_DIR/docker/\$(dirname "\$f")"; cp -p "docker/\$f" "\$DEPLOY_DIR/docker/\$f"
done
cp docker/.env.example "\$DEPLOY_DIR/docker/.env.example"
cp agentwiki.env.example gen-env.sh setup-models.sh start.sh stop.sh "\$DEPLOY_DIR/"

echo "==> \$ENV_FILE 的 RAGFLOW_IMAGE -> \$NEWTAG"
set_kv "\$ENV_FILE" RAGFLOW_IMAGE "\$NEWTAG"

echo "==> 重建 app 容器（相依服務與資料保留）"
( cd "\$DEPLOY_DIR" && ./start.sh )
UPD
  chmod +x "$STAGE/update.sh"

  cat > "$STAGE/UPDATE.md" <<EOF
# RAGFlow 更新包（${BUNDLE}）

只換 app image（${APP_IMAGE}），**不動**相依服務（Elasticsearch / MySQL / MinIO / Kvrocks / NATS）
與資料。

## 步驟（目標機，需先有用完整包做過的部署目錄）
\`\`\`bash
tar -xzf ${BUNDLE}.tar.gz && cd ${BUNDLE}
./update.sh /path/to/your/deploy-dir
\`\`\`

update.sh 會：載入新 app image → 更新部署檔（被覆寫的舊檔備份到 \`docker/.bak-<時間>/\`，
\`docker/.env\` 不動）→ 改 \`docker/.env\` 的 \`RAGFLOW_IMAGE\` → \`./start.sh\`。

## 注意
- **跨大版本不要用更新包。** RAGFlow 的資料遷移在容器啟動時自動執行，有些版本之間
  不可逆（例如 v0.27.2 → v1.0.0-rc1），也不能跳版。更新前先看官方升級說明並備份。
- 回滾：保留上一版更新包，重跑 \`./update.sh <部署目錄>\`。新版若做過資料遷移，換回舊
  image 不會回滾資料，要用更新前的備份還原。
EOF

else
  # ---- 完整包：首次部署 ----
  cat > "$STAGE/load.sh" <<LOAD
#!/usr/bin/env bash
# 部署端：載入 image 並備妥 docker/.env。執行後依畫面提示啟動。
set -euo pipefail
cd "\$(dirname "\$0")"
${SETKV}

echo "==> docker load images.tar"
docker load -i images.tar

# 建立 docker/.env（官方範本 + agentwiki.env.example）並自動產生帳密；參數原樣轉給 gen-env.sh，
# 例如 ./load.sh --admin-email ops@example.com
./gen-env.sh "\$@"
ENV_FILE=docker/.env
NEWTAG=\$(grep '^RAGFLOW_IMAGE=' image.env | cut -d= -f2-)
set_kv "\$ENV_FILE" RAGFLOW_IMAGE "\$NEWTAG"

echo
echo "下一步："
echo "  1) 需要的話確認 docker/.env 的埠號（帳密已自動產生，要自訂請在第一次啟動前改）"
echo "  2) 啟動：./start.sh"
echo "  3) 在 docker/.env 填 RAGFLOW_SETUP_EMBEDDING_BASE_URL / _MODEL，再跑 ./setup-models.sh"
LOAD
  chmod +x "$STAGE/load.sh"

  DEP_LIST="$(echo "$DEP_IMAGES" | sed 's/^/  - /')"
  cat > "$STAGE/DEPLOY.md" <<EOF
# RAGFlow 部署包（${BUNDLE}）

離線部署 RAGFlow。本包已含所有 image，目標機**不需連網、不需原始碼**。

## 內容
| 檔案 | 說明 |
|---|---|
| images.tar | app（${APP_IMAGE}）+ 相依 image（清單見下） |
| docker/ | 官方 compose、設定範本（\`.env.example\`）、entrypoint 等部署檔 |
| agentwiki.env.example | 建立 \`docker/.env\` 時套用：compose project 名稱、host 埠號、帳密欄位 |
| image.env | 本次 app image 標籤（RAGFLOW_IMAGE） |
| load.sh | 載入 image + 呼叫 gen-env.sh 備妥 \`docker/.env\` |
| gen-env.sh | 建立 \`docker/.env\` 並自動產生帳密（\`--show\` 查看、\`--dry-run\` 預覽） |
| setup-models.sh | 啟動後建立服務帳號、embedding 模型供應商與 API key，印出 kb-stack 設定 |
| start.sh / stop.sh | 啟動（等到 API 健康檢查通過）/ 停止（volume 保留） |

相依 image：
${DEP_LIST}

## 前置需求
- Docker Engine 24+，Docker Compose v2
- \`vm.max_map_count >= 262144\`（Elasticsearch 需要；start.sh 會檢查）
- 建議至少 4 核 CPU、16 GB RAM、50 GB 磁碟（官方最低建議）

## 步驟
\`\`\`bash
tar -xzf ${BUNDLE}.tar.gz && cd ${BUNDLE}
./load.sh                  # docker load + 產生 docker/.env（帳密自動產生，最後印出管理員帳密）
./start.sh                 # 啟動並等待就緒，完成後印出網址
./setup-models.sh          # 建立服務帳號、embedding 模型與 API key（先在 docker/.env 填 RAGFLOW_SETUP_*）
./gen-env.sh --show        # 之後要查帳密
\`\`\`

管理員 email 要自訂就在 load.sh 加 \`--admin-email you@example.com\`。所有帳密只在第一次啟動時
生效；已有資料後 gen-env.sh 不會再改任何帳密。

## 跟舊版 RAGFlow 並存
預設 compose project 是 \`ragflow-v100\`，容器、volume、network 都帶這個前綴，不會碰到舊版
（舊版官方部署的 project 通常是 \`docker\`）。host 埠號預設改成 18080（Web）/ 19380（API）等，
見 \`agentwiki.env.example\`；start.sh 會先檢查埠號有沒有被佔用。

## 驗證
\`\`\`bash
curl -s http://127.0.0.1:19380/api/v1/system/healthz
cd docker && docker compose -p ragflow-v100 --env-file .env -f docker-compose.yml ps
\`\`\`
EOF
fi

bold "==> [6/6] 打包 -> ${TARBALL}"
tar -czf "$TARBALL" -C "$OUT_DIR" "$BUNDLE"
rm -rf "$STAGE"        # 只留最終 tar.gz
( cd "$OUT_DIR" && sha256sum "$(basename "$TARBALL")" > "$(basename "$TARBALL").sha256" )
# 目標機挑包用（掃描目錄 → 選擇 → 確認 → 載入），跟 tar.gz 一起搬過去
cp "$HERE/load-select.sh" "$OUT_DIR/" && chmod +x "$OUT_DIR/load-select.sh"

SIZE=$(du -h "$TARBALL" | cut -f1)
grn "完成：${TARBALL} (${SIZE})"
if [ "$APP_ONLY" = "1" ]; then
  echo "目標機：tar -xzf ${BUNDLE}.tar.gz && cd ${BUNDLE} && ./update.sh <既有部署目錄>"
else
  echo "目標機：tar -xzf ${BUNDLE}.tar.gz && cd ${BUNDLE} && ./load.sh && ./start.sh"
fi
