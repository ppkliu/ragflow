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
| `agentwiki/setup-models.sh` | 目標機 | 啟動後建立服務帳號、embedding 模型供應商與 API key，印出 kb-stack 設定 |
| `agentwiki/repo-deploy.sh` | 有 repo 的主機 | 直接從 git repo 部署：`init` / `up` / `update`（git pull 後套用）/ `build` |
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
nano docker/.env           # 填 RAGFLOW_SETUP_EMBEDDING_BASE_URL / RAGFLOW_SETUP_EMBEDDING_MODEL
./setup-models.sh          # 服務帳號 + embedding 模型 + API key，最後印出 kb-stack 要填的三行
./gen-env.sh --show        # 之後要查帳密
```

### docker/.env 與帳密自動產生（`gen-env.sh`）

**.env 集中存放在 `/opt/AgentWiki/shared/env/ragflow.env`。** 主機上有 `/opt/AgentWiki` 時，gen-env.sh 把
真正的 `.env` 建在那裡（跟 `adapter.env`、`kb-stack.env` 同一處，換版不動），部署目錄的 `docker/.env`
只是指向它的 symlink。好處：

- 部署目錄或 repo 的 `runtime/` 整個被刪掉，帳密還在；重新解包或 `repo-deploy.sh init` 會自動接回同一份。
- 部署包與 `repo-deploy.sh` 兩種模式共用同一份 `.env`，切換時不會出現兩套帳密。
- 已經是一般檔的 `docker/.env`，下次跑 gen-env.sh（load.sh 會跑）時自動搬過去；集中位置已有內容不同的
  `.env` 時會停下來，不擅自取捨。

`/opt/AgentWiki/shared/env` 要是部署帳號寫得進去（沒有權限時 gen-env.sh 會印出 `sudo mkdir/chown`
指令）。指定其他位置用 `--env-store <絕對路徑>` 或 `RAGFLOW_ENV_STORE`，不要集中存放用 `--no-env-store`。
改帳密前的備份（`.bak-<時間>`）放在真正的檔案旁邊。

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
| `--env-store <路徑>` / `--no-env-store` | 指定 .env 集中存放位置 / 不集中存放 |

產生的帳密：`ELASTIC_PASSWORD`、`MYSQL_PASSWORD`、`MINIO_PASSWORD`、`REDIS_PASSWORD`、
`DEFAULT_SUPERUSER_PASSWORD`（舊名 `ADMIN_DEFAULT_PASSWORD` 寫成同一個值）。要用自己指定的值，
在 load.sh 之前填進 `agentwiki.env.example`，或之後直接改 `docker/.env`，gen-env.sh 不會覆寫有值的鍵。
只用英數字，是因為這些值會經過 compose 變數替換、YAML 與連線字串，特殊字元在這幾處的轉義規則各不相同。

- **帳密只在第一次啟動、資料初始化時生效。** 之後再改只會讓服務連不上自己的資料。gen-env.sh
  偵測到這個 compose project 已經有 volume 時，任何帳密都不會改，只列出仍是預設值的鍵。
- 改帳密前會把原本的 `.env` 備份成 `.env.bak-<時間>`；`.env` 與備份的權限都是 600。
- 在 repo 原始碼目錄執行會被擋下：那裡的 `docker/.env` 是上游進版控的範本，不是部署用的。
- **`vm.max_map_count` 至少 262144**，Elasticsearch 才起得來。start.sh 會檢查並印出修正指令。
- 第一次啟動時（官方 compose 帶 `--init-superuser`）用上面的 email 與密碼建立超級管理員。
  **它只能登入 Admin（19381）**：1.0 的 Web 登入與一般 API 都會擋掉超級管理員（`super admin should
  not login to this page`、`Super user shouldn't access the URL`）。Web 登入、dataset、模型與 API key
  都要用一般使用者，由下一節的 `setup-models.sh` 建立。

### 模型供應商不能從 .env 設定

1.0 開源版**沒有**從設定檔帶入模型供應商的功能。`docker/service_conf.yaml.template` 裡雖然還有
`user_default_llm`（0.x 時代可以寫 factory、api_key、base_url），但 1.0 的 Go 後端讀它的
`ParseDefaultModelsConfig` 在開源版是空函式（`internal/server/config/stub.go`，註解寫明 the OSS
build has none），只有企業版會讀。所以填了也不會生效、不會報錯。

供應商與模型一律在啟動後設定：Web 的「模型供應商」頁，或 REST API。這些設定存在 MySQL，跟著
volume 保留，換 app image 不用重設。`setup-models.sh` 把 REST API 的步驟串起來。

### 服務帳號、模型與 API key（`setup-models.sh`）

RAGFlow 起來之後，在 `docker/.env` 填兩個值再執行：

```bash
RAGFLOW_SETUP_EMBEDDING_BASE_URL=http://<embedding 主機>:<埠>/v1   # OpenAI 相容，要含 /v1
RAGFLOW_SETUP_EMBEDDING_MODEL=<模型名稱>                            # 跟 embedding 服務回報的一致
```
```bash
./setup-models.sh              # 也可以用 --base-url / --model 直接給；--dry-run 只印設定
```

| 步驟 | API | 已存在時 |
|---|---|---|
| 1. 超級管理員登入 Admin | `POST :19381/api/v1/admin/login` | — |
| 2. 服務帳號（預設 `kb-stack@agentwiki.local`） | `POST /api/v1/auth/login`，不存在就 `POST /api/v1/admin/users` | 密碼跟 `.env` 對不上就用 Admin API 重設成 `.env` 的值 |
| 3. 供應商 / instance / 模型 | `PUT /api/v1/providers`、`POST .../instances`、`POST .../instances/{i}/models` | 沿用，**不改** instance 的 base URL 與 key |
| 驗證 | `POST /api/v1/embeddings` 實際算一次向量 | — |
| 4. 預設 embedding 模型 | `PATCH /api/v1/models/default` | 覆寫 |
| 5. API key | `POST /api/v1/system/tokens` | 沿用 `.env` 記錄的 `RAGFLOW_KB_API_KEY` |
| 6. 輸出 | 印出並寫入 `kb-stack.ragflow.env`（600） | — |

- 服務帳號密碼留空時自動產生，與 API key 一起寫回 `docker/.env`（`RAGFLOW_SERVICE_USER_PASSWORD`、
  `RAGFLOW_KB_API_KEY`），`./gen-env.sh --show` 會列出。Web 也用這個帳號登入。
- 供應商預設 `OpenAI-API-Compatible`。它底層用 vLLM 的驅動，允許私有網段，所以 embedding 服務在
  內網 IP 不需要開 `ALLOW_ANY_HOST`。RAGFlow 會在 base URL 後面直接接 `embeddings`，所以要含 `/v1`。
- kb-stack 的 `RAGFLOW_EMBEDDING_MODEL` 在 1.0 是 **`<模型>@<instance>@<供應商>`**，例如
  `bge-m3@agentwiki@OpenAI-API-Compatible`（0.x 是 `<模型>@<廠商>`）。腳本會直接印出正確的值。
- 要換 embedding 服務的位址或 key：instance 已存在時腳本不會改，請到 Web 的模型供應商頁修改，或刪掉
  instance 後重跑。
- 需要 `python3`（只用標準函式庫）。

## 二之一、直接從 git repo 部署（`repo-deploy.sh`）

主機上有這個 repo、想 `git pull` 之後一個指令就套用，不必每次打包、解壓時用。image 不在 repo 裡，
仍然要先有：從部署包載入（`docker load -i <包>/images.tar`，最省事），或 `repo-deploy.sh build`
在本機 build（需要外網）。

```bash
cd AgentWiki/ragflow
agentwiki/repo-deploy.sh init --admin-email you@example.com   # 建 runtime/ 與 .env（帳密自動產生）
agentwiki/repo-deploy.sh up                                   # 同步部署檔並啟動
nano runtime/docker/.env                                      # 填 RAGFLOW_SETUP_EMBEDDING_*
agentwiki/repo-deploy.sh setup-models                         # 服務帳號、模型、API key

# 之後每次更新
agentwiki/repo-deploy.sh update     # git pull --ff-only → 同步 → 有變動才重啟受影響的服務
agentwiki/repo-deploy.sh status | logs [服務] | down
```

- **為什麼有 `runtime/`**（預設在 repo 根目錄，`RAGFLOW_RUNTIME_DIR` 可改）：repo 的 `docker/.env` 是
  上游範本（公開預設密碼），compose 又用 `env_file: .env` 把它灌進容器，不能原地改。部署檔同步到
  `runtime/`，結構跟部署包一樣，所以 `start.sh`、`gen-env.sh`、`setup-models.sh` 在兩種模式下行為相同。
  `runtime/` 自帶內容為 `*` 的 `.gitignore`：裡面有 `.env`、`.env.bak-*`（帳密備份）與
  `kb-stack.ragflow.env`（API key），上游 `.gitignore` 只忽略 `.env` 本身，其他會被 `git add` 掃到。
- **同步規則**：`git ls-files docker` 的檔案逐一比對，有變才覆寫，舊檔備份到
  `runtime/docker/.bak-<時間>/`；`runtime/docker/.env` 永遠不被覆寫。新版本加的設定鍵只補進 `.env`
  沒有的，既有值（尤其帳密）不動。
- **`update` 的重啟範圍**：`docker/` 沒變只更新腳本、不碰容器；有變就 `compose up -d`（重建設定有變的
  服務）；`entrypoint.sh` 或 `service_conf.yaml.template` 變了再重啟 `ragflow-cpu`，因為它們是掛進
  容器的檔案，compose 看不出內容變化。
- **RAGFlow 本身（Go 程式與前端）在 image 裡**，`git pull` 換不掉。換版要 `repo-deploy.sh build`，
  或在建置機 build 後傳更新包、`docker load` 再 `repo-deploy.sh up`。
- **`dist/` 自帶內容為 `*` 的 `.gitignore`**（pack-prod.sh 建立），數 GB 的打包產物不依賴上游 `.gitignore` 才被排除。
- **不要在 `dist/` 裡解開部署包來跑。** pack-prod.sh 每次會清掉 `dist/ragflow-prod-<版本>/` 暫存目錄；
  它發現裡面有 `docker/.env` 時會拒絕打包，避免把正在用的帳密刪掉。部署請放 `dist/` 以外。
- **接手部署包裝好的資料**：`init --import-env /opt/.../docker/.env`，帳密與 project 名稱沿用。兩種模式
  用同一個 compose project 時，先 `./stop.sh` 停掉部署包那份，不要同時跑。

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
不影響內部連線。

**埠號被佔用時 start.sh 會自動避開。** 這個 project 沒有容器在跑時，start.sh 逐一檢查上表的埠號；被其他
程式（例如同一台上的舊版 RAGFlow）佔用的，往上找第一個沒人在聽、也沒被同一份 `.env` 其他鍵用掉的埠，
寫回 `.env`（原檔先備份成 `.bak-<時間>`，symlink 與 600 權限保留），再啟動。API 埠變了會提醒 kb-stack
的 `RAGFLOW_BASE_URL` 要跟著改，`setup-models.sh` 印出的是新值。要改回「只報錯停下」：`AUTO_PORTS=0 ./start.sh`。

主機上沒有舊版、想用官方埠號時，在 `load.sh` 之前把包內 `agentwiki.env.example` 的對應行刪掉即可。

## 四、kb-stack 要怎麼接

kb-stack 只透過 REST API 使用 RAGFlow。新部署接上前要先完成 kb-stack 的 1.0 相容修改
（`kb-stack/docs/RAGFLOW_V1_MIGRATION_PLAN.md`），再把 `RAGFLOW_BASE_URL` 指到
`http://<主機>:19380`（API 埠，或經 nginx 的 18080）、填新的 `RAGFLOW_API_KEY` 與
`RAGFLOW_EMBEDDING_MODEL`。三個值 `setup-models.sh` 都會印出，也寫在部署目錄的
`kb-stack.ragflow.env`。

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
