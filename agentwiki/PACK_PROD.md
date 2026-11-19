# RAGFlow 離線打包流程（agentwiki/pack-prod.sh）

把 RAGFlow（官方 v1.0.0 原始碼 build 出的 app image）連同所有相依 image 與部署檔打包成單一
`.tar.gz`，搬到內網主機**離線部署**。目標機不需連網、不需原始碼。做法比照
`docmost/pack-prod.sh`（見 `docmost/PACK_PROD.md`）。

這個目錄是 AgentWiki 自己加的，其他檔案都是上游原樣。打包產物在 `<repo>/dist/`，已被上游的
`.gitignore` 忽略。

## 流程總覽

```
══════════════ 建置機（ragflow/ 原始碼，需連外網）══════════════
 分支 agentwiki/v1.0.0（= 官方 tag v1.0.0 + 本目錄）
   │
   ├─ 首次部署 ── agentwiki/pack-prod.sh
   │     → docker build app + pull 相依 image + save + 部署檔
   │     → dist/ragflow-prod-v1.0.0.tar.gz（+ .sha256、load-select.sh）  [完整包]
   │
   └─ 只換 app ── APP_ONLY=1 agentwiki/pack-prod.sh
         → dist/ragflow-prod-update-<ver>.tar.gz                         [更新包]
                              │  scp / USB
                              ▼
══════════════ 目標機（離線，Docker + Compose v2）══════════════
 ./load-select.sh <dir>   或手動：
   完整包 ─▶ tar -xzf → ./load.sh → 編輯 docker/.env → ./start.sh
   更新包 ─▶ tar -xzf → ./update.sh <既有部署目錄>
```

| 腳本 | 在哪台 | 做什麼 |
|---|---|---|
| `agentwiki/pack-prod.sh` | 建置機 | 打包完整包 / 更新包（`APP_ONLY=1`） |
| `agentwiki/load-select.sh` | 目標機 | 掃描 / 選擇 / 確認 / 載入 tar.gz（打包時會複製到 `dist/`） |
| `load.sh`（包內生成） | 目標機 | 完整包：`docker load` + 產生 `docker/.env` |
| `update.sh`（包內生成） | 目標機 | 更新包：換 app image，資料保留 |
| `start.sh` / `stop.sh` | 目標機 | 啟動並等到健康檢查通過 / 停止（volume 保留） |

## 一、建置機

前置需求：Docker Engine（含 BuildKit）、Compose v2、可連外網。build 會拉
`infiniflow/ragflow_deps:latest`（模型與字型資產，數 GB）、`infiniflow/github_action_runner`，
並下載 npm 與 Go 套件。

```bash
cd ragflow
git switch agentwiki/v1.0.0
git fetch upstream tag v1.0.0 --no-tags     # image 內的版本號靠這個 tag（git describe）
agentwiki/pack-prod.sh                      # 完整包，版本取最近的 v* tag
# 其他用法
SKIP_BUILD=1 agentwiki/pack-prod.sh         # 沿用本機已 build 的 agentwiki-ragflow:v1.0.0
APP_ONLY=1 agentwiki/pack-prod.sh           # 只打包 app image（更新包）
NEED_MIRROR=1 agentwiki/pack-prod.sh        # build 時改走官方 Dockerfile 內建的大陸鏡像
RAGFLOW_IMAGE=myrepo/ragflow:rc1 agentwiki/pack-prod.sh
```

腳本的 6 個步驟：

1. 先 build 衍生 runner `agentwiki-ragflow-runner:<版本>`（見下方「build runner 缺 native
   library」），再 `docker build` 出 `agentwiki-ragflow:<版本>`（另加 `:latest`）
2. 用 `docker compose config --images` 依 `docker/.env` 的 `COMPOSE_PROFILES` 算出相依 image，
   沒有就 `pull`。預設是 Elasticsearch + MySQL + MinIO（`pgsty/silo`）+ Kvrocks + NATS
3. 複製**進版控的** `docker/` 檔案（執行期資料不會被帶走），官方 `docker/.env` 改名成
   `.env.example`
4. `docker save` → `images.tar`
5. 產生部署端 `load.sh`／`update.sh` 與說明
6. 打包成 `dist/ragflow-prod-<版本>.tar.gz`，並產生 `.sha256`

## 二、目標機

```bash
sha256sum -c ragflow-prod-v1.0.0.tar.gz.sha256
tar -xzf ragflow-prod-v1.0.0.tar.gz && cd ragflow-prod-v1.0.0
./load.sh                  # docker load + 產生 docker/.env（第一次會套用 agentwiki.env）
nano docker/.env           # 改掉預設密碼；確認埠號
./start.sh                 # 啟動，等到 /api/v1/system/healthz 通過
```

- **預設密碼要在第一次啟動前改。** `ELASTIC_PASSWORD`、`MYSQL_PASSWORD`、`MINIO_PASSWORD`、
  `REDIS_PASSWORD` 的官方預設值是 `infini_rag_flow`，load.sh 會列出還沒改的。這些密碼只在資料
  初始化時生效，之後再改只會造成連線失敗。
- **`vm.max_map_count` 至少 262144**，Elasticsearch 才起得來。start.sh 會檢查並印出修正指令。
- 第一次啟動用 `--init-superuser` 建立管理員（官方 compose 的預設），登入 Web 後到右上角頭像
  → API 產生 API key，填進 kb-stack 的 `RAGFLOW_API_KEY`。

## 三、跟舊版 RAGFlow 並存

1.0 的 compose 沒有寫死 `container_name`，所以容器、volume、network 都跟著 compose project
名稱走。`agentwiki.env` 把 project 設成 `ragflow-v100`，舊版官方部署通常是 `docker`，兩者互不
相干。

會撞的只有 host 埠號，`agentwiki.env` 全部改開：

| 用途 | 變數 | 官方預設 | 這裡 |
|---|---|---|---|
| Web（nginx） | `SVR_WEB_HTTP_PORT` / `SVR_WEB_HTTPS_PORT` | 80 / 443 | 18080 / 18443 |
| API | `SVR_HTTP_PORT` | 9380 | 19380 |
| Admin | `ADMIN_SVR_HTTP_PORT` | 9381 | 19381 |
| Elasticsearch | `ES_PORT` | 1200 | 11200 |
| MySQL | `EXPOSE_MYSQL_PORT` | 3306 | 13306 |
| MinIO / Console | `MINIO_PORT` / `MINIO_CONSOLE_PORT` | 9000 / 9001 | 19000 / 19001 |
| Kvrocks | `KVROCKS_PORT` | 6379 | 26379 |
| NATS / 監控 | `EXPOSE_NATS_PORT` / `EXPOSE_NATS_MONITORING_PORT` | 4222 / 8222 | 14222 / 18222 |

這些變數都只用在 host 端：容器之間一律用服務名稱加容器內固定埠號連線（`service_conf.yaml.template`
寫死 `minio:9000`、`es01:9200`；Kvrocks 在 `ragflow-cpu` 的 environment 另外固定成 6379），改了
不影響內部連線。start.sh 啟動前會檢查這些埠號有沒有被佔用。

主機上沒有舊版、想用官方埠號時，在 `load.sh` 之前把 `agentwiki.env` 的對應行刪掉即可。

## 四、kb-stack 要怎麼接

kb-stack 只透過 REST API 使用 RAGFlow。新部署接上前要先完成 kb-stack 的 1.0 相容修改
（`kb-stack/docs/RAGFLOW_V1_MIGRATION_PLAN.md`），再把 `RAGFLOW_BASE_URL` 指到
`http://<主機>:19380`（API 埠，或經 nginx 的 18080）、填新的 `RAGFLOW_API_KEY` 與
`RAGFLOW_EMBEDDING_MODEL`（要先在新 RAGFlow 的模型供應商頁加好 embedding 模型）。

這次是**全新安裝**：舊的 0.26.4 保留不動，新的 RAGFlow 與 Cognee 資料庫由 kb-wiki 的附件重新
解析、切片產生，不做 0.26.4 → 1.0 的資料遷移（那條路要依序經過 v0.27.2 與 v1.0.0-rc1，見
`kb-stack/docs/RAGFLOW_UPGRADE_ANALYSIS.md` 第五節）。

## 五、注意事項

- **build runner 缺 native library（2026-11-19 實際踩到）。** 官方 Dockerfile 的 go-builder 階段
  `FROM infiniflow/github_action_runner:latest`，`build.sh --go` 預期 office_oxide、pdfium-static、
  pdf_oxide 與 onnxruntime 1.29.0 已經預先放在 runner 的 `/opt/ragflow-native-libs/`。v1.0.0
  發佈後 runner 被重建（2026-11-09 那版只剩 onnxruntime，而且是 1.23.2），直接 build 會依序失敗在
  `Error: office_oxide native library not found` 與 `no ONNX Runtime 1.29.0 static lib`。
  `agentwiki/runner.Dockerfile` 在官方 runner 上用官方的 `tools/scripts/download_dev_deps.py`
  （經 `agentwiki/seed_native_libs.py` 只取這四個 native 依賴，版本、URL 與 sha256 驗證都沿用官方
  腳本，office_oxide 版本讀自 `go.mod`）補齊，pack-prod.sh 再用
  `docker build --build-context infiniflow/github_action_runner:latest=docker-image://agentwiki-ragflow-runner:<版本>`
  讓官方 Dockerfile 改用它。上游檔案一行都沒改。下載走 github.com，連不到時加
  `GH_PROXY=https://gh-proxy.com/`。
- **build 的輸入沒有完全釘版本。** 官方 Dockerfile 用的是 `infiniflow/ragflow_deps:latest` 與
  `infiniflow/github_action_runner:latest`，不同時間 build 可能拿到不同的模型資產或工具版本。
  要可重現，就保留打出來的 tar.gz，換版前不要重 build 同一個版本號。
- **架構要一致**：x86_64 打的包不能在 arm64 跑。
- **image 內的版本號**來自 build 當下的 `git describe`。在 `agentwiki/v1.0.0` 分支加了 commit
  之後再 build，會變成 `v1.0.0-<n>-g<hash>`，代表「v1.0.0 加上本地修改」。
- **DOC_ENGINE**：預設 Elasticsearch。要改 Infinity，在建置機改 `docker/.env` 的 `DOC_ENGINE`
  再打包，相依 image 清單會自動跟著變；目標機的 `docker/.env` 也要一致。
- **`.env` 不入包**：只帶 `.env.example`，真正的密碼在目標機填。
- **升級分支**：之後要換到新的官方版本，從新的 tag 開 `agentwiki/v<版本>` 分支，再把這個目錄
  cherry-pick 過去。這個目錄不碰任何上游檔案，所以不會有衝突。
