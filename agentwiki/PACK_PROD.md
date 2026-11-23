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
   完整包 ─▶ tar -xzf → ./load.sh（自動產生 docker/.env 與帳密）→ ./start.sh
   更新包 ─▶ tar -xzf → ./update.sh <既有部署目錄>
```

| 腳本 | 在哪台 | 做什麼 |
|---|---|---|
| `agentwiki/pack-prod.sh` | 建置機 | 打包完整包 / 更新包（`APP_ONLY=1`） |
| `agentwiki/load-select.sh` | 目標機 | 掃描 / 選擇 / 確認 / 載入 tar.gz（打包時會複製到 `dist/`） |
| `load.sh`（包內生成） | 目標機 | 完整包：`docker load` + 呼叫 gen-env.sh |
| `agentwiki/gen-env.sh` | 目標機 | 建立 `docker/.env`（官方範本 + `agentwiki/.env.example`）並自動產生帳密 |
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
./load.sh                  # docker load + gen-env.sh：建立 docker/.env、自動產生帳密
./start.sh                 # 啟動，等到 /api/v1/system/healthz 通過
./gen-env.sh --show        # 之後要查帳密
```

### docker/.env 與帳密自動產生（`gen-env.sh`）

`docker/.env` 由三層組成：官方範本 `docker/.env.example` → AgentWiki 範本
`agentwiki.env.example`（repo 裡是 `agentwiki/.env.example`：project 名稱、埠號、帳密欄位）→
`gen-env.sh` 產生的帳密。load.sh 會自動呼叫 gen-env.sh，參數原樣轉過去。

| 用法 | 做什麼 |
|---|---|
| `./load.sh` 或 `./gen-env.sh` | `.env` 不存在就建立；空值或公開預設值（`infini_rag_flow`、`admin`）的帳密換成 24 字元隨機英數字；已設定的值不動，重跑不會變 |
| `--admin-email you@example.com` | 指定第一個超級管理員的 email（預設 `admin@ragflow.io`） |
| `--dry-run` | 只列出會改哪些鍵 |
| `--rotate` | 所有帳密重新產生（只在還沒有資料時允許） |
| `--show` | 印出目前的帳密 |

產生的帳密：`ELASTIC_PASSWORD`、`MYSQL_PASSWORD`、`MINIO_PASSWORD`、`REDIS_PASSWORD`、
`DEFAULT_SUPERUSER_PASSWORD`（舊名 `ADMIN_DEFAULT_PASSWORD` 寫成同一個值）。要用自己指定的值，
在 load.sh 之前填進 `agentwiki.env.example`，或之後直接改 `docker/.env`，gen-env.sh 不會覆寫有值的鍵。
只用英數字，是因為這些值會經過 compose 變數替換、YAML 與連線字串，特殊字元在這幾處的轉義規則各不相同。

- **帳密只在第一次啟動、資料初始化時生效。** 之後再改只會讓服務連不上自己的資料。gen-env.sh
  偵測到這個 compose project 已經有 volume 時，任何帳密都不會改，只列出仍是預設值的鍵。
- 改帳密前會把原本的 `.env` 備份成 `.env.bak-<時間>`；`.env` 與備份的權限都是 600。
- 在 repo 原始碼目錄執行會被擋下：那裡的 `docker/.env` 是上游進版控的範本，不是部署用的。
- **`vm.max_map_count` 至少 262144**，Elasticsearch 才起得來。start.sh 會檢查並印出修正指令。
- 第一次啟動時（官方 compose 帶 `--init-superuser`）用上面的 email 與密碼建立超級管理員。登入 Web
  後到右上角頭像 → API 產生 API key，填進 kb-stack 的 `RAGFLOW_API_KEY`。

### 模型供應商不能從 .env 設定

1.0 開源版**沒有**從設定檔帶入模型供應商的功能。`docker/service_conf.yaml.template` 裡雖然還有
`user_default_llm`（0.x 時代可以寫 factory、api_key、base_url），但 1.0 的 Go 後端讀它的
`ParseDefaultModelsConfig` 在開源版是空函式（`internal/server/config/stub.go`，註解寫明 the OSS
build has none），只有企業版會讀。所以填了也不會生效、不會報錯。

供應商與模型一律在啟動後設定：Web 的「模型供應商」頁，或 REST API（`PUT /api/v1/providers`、
`POST /api/v1/providers/{provider}/instances`、`.../instances/{instance}/models`，用超級管理員
登入 `POST /api/v1/auth/login` 拿到的 token）。這些設定存在 MySQL，跟著 volume 保留，換 app image
不用重設。

## 三、跟舊版 RAGFlow 並存

1.0 的 compose 沒有寫死 `container_name`，所以容器、volume、network 都跟著 compose project
名稱走。`agentwiki/.env.example` 把 project 設成 `ragflow-v100`，舊版官方部署通常是 `docker`，
兩者互不相干。

會撞的只有 host 埠號，`agentwiki/.env.example` 全部改開：

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

主機上沒有舊版、想用官方埠號時，在 `load.sh` 之前把包內 `agentwiki.env.example` 的對應行刪掉即可。

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
