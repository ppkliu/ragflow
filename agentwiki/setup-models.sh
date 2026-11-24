#!/usr/bin/env bash
# 部署端：RAGFlow 啟動後，自動建立 kb-stack 要用的服務帳號、embedding 模型供應商與 API key。
#
# 為什麼需要它：1.0 開源版不從設定檔讀模型供應商（service_conf.yaml 的 user_default_llm 在開源版
# 被忽略），只能在啟動後用 Web 或 REST API 設定。這支腳本把那些 API 呼叫串起來，可以重複執行。
#
# 做的事（每一步都會先查，已經有的就沿用）：
#   1) 用超級管理員登入 Admin API（DEFAULT_SUPERUSER_EMAIL / _PASSWORD）
#   2) 建立一般使用者（服務帳號）：超級管理員不能登入 Web、也不能呼叫資料 API，
#      dataset、模型、API key 都屬於一般使用者
#   3) 在服務帳號下加入模型供應商、instance 與 embedding 模型，實際打一次 embedding 驗證
#   4) 設成服務帳號的預設 embedding 模型
#   5) 產生（或沿用）API key
#   6) 印出 kb-stack 要填的三行設定，並寫進 kb-stack.ragflow.env
#
# 設定來源（後者覆蓋前者）：docker/.env → 環境變數 → 命令列參數。
# docker/.env 裡的鍵（由 agentwiki.env.example 帶入）：
#   RAGFLOW_SETUP_EMBEDDING_BASE_URL   embedding 服務的 OpenAI 相容 base URL，要含 /v1（必填）
#   RAGFLOW_SETUP_EMBEDDING_MODEL      模型名稱，跟 embedding 服務回報的一致（必填）
#   RAGFLOW_SETUP_EMBEDDING_API_KEY    embedding 服務的 key（沒有驗證就留空）
#   RAGFLOW_SETUP_EMBEDDING_MAX_TOKENS 預設 8192
#   RAGFLOW_SETUP_PROVIDER             預設 OpenAI-API-Compatible
#   RAGFLOW_SETUP_INSTANCE             預設 agentwiki
#   RAGFLOW_SERVICE_USER_EMAIL         服務帳號，預設 kb-stack@agentwiki.local
#   RAGFLOW_SERVICE_USER_PASSWORD      留空 = 自動產生並寫回 docker/.env
#
# 用法（在部署目錄，也就是含 docker/ 的那一層，RAGFlow 已經 ./start.sh 起來之後）：
#   ./setup-models.sh
#   ./setup-models.sh --base-url http://<embedding主機>:<埠>/v1 --model bge-m3
#   ./setup-models.sh --dry-run          # 只印出會用的設定，不呼叫 API
#   ./setup-models.sh --host <主機>      # RAGFlow 不在本機時（預設 127.0.0.1）
#   ./setup-models.sh --public-host <主機>  # 印給 kb-stack 的 RAGFLOW_BASE_URL 用這個主機名
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
for a in "$@"; do
  case "$a" in
    -h|--help) sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done
command -v python3 >/dev/null 2>&1 || { echo "需要 python3（只用標準函式庫）" >&2; exit 1; }
exec python3 - "$DIR" "$@" <<'PY'
import argparse, base64, json, os, secrets, string, sys, time
import urllib.error, urllib.request

deploy_dir = sys.argv[1]
ap = argparse.ArgumentParser(prog="setup-models.sh", add_help=False)
ap.add_argument("--dir", default=deploy_dir)
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--public-host", default=None)
ap.add_argument("--base-url"); ap.add_argument("--model"); ap.add_argument("--api-key")
ap.add_argument("--max-tokens", type=int); ap.add_argument("--provider"); ap.add_argument("--instance")
ap.add_argument("--user-email"); ap.add_argument("--wait", type=int, default=300)
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args(sys.argv[2:])

def c(code, s): return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
def bold(s): print(c("1", s))
def ok(s): print(c("32", s))
def warn(s): print(c("33", s))
def die(s, rc=1):
    print(c("31", s), file=sys.stderr); sys.exit(rc)

env_file = os.path.join(args.dir, "docker", ".env")
if not os.path.isfile(env_file):
    die(f"找不到 {env_file}：先跑 ./load.sh（或 ./gen-env.sh）與 ./start.sh")

def read_env(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    return out

def write_env(path, key, value):
    """取代或附加 KEY=VALUE；就地覆寫內容，保留檔案權限（600）。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    done = False
    for i, line in enumerate(lines):
        if line.startswith(key + "="):
            lines[i] = f"{key}={value}"; done = True
    if not done:
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, f"{key}={value}")
        else:
            lines.append(f"{key}={value}")
    with open(path, "r+", encoding="utf-8") as f:
        f.seek(0); f.write("\n".join(lines)); f.truncate()

env = read_env(env_file)
def pick(cli, key, default=""):
    if cli not in (None, ""):
        return cli
    if os.environ.get(key):
        return os.environ[key]
    return env.get(key) or default

cfg = {
    "base_url": pick(args.base_url, "RAGFLOW_SETUP_EMBEDDING_BASE_URL"),
    "model": pick(args.model, "RAGFLOW_SETUP_EMBEDDING_MODEL"),
    "api_key": pick(args.api_key, "RAGFLOW_SETUP_EMBEDDING_API_KEY"),
    "max_tokens": int(pick(args.max_tokens and str(args.max_tokens), "RAGFLOW_SETUP_EMBEDDING_MAX_TOKENS", "8192")),
    "provider": pick(args.provider, "RAGFLOW_SETUP_PROVIDER", "OpenAI-API-Compatible"),
    "instance": pick(args.instance, "RAGFLOW_SETUP_INSTANCE", "agentwiki"),
    "user_email": pick(args.user_email, "RAGFLOW_SERVICE_USER_EMAIL", "kb-stack@agentwiki.local"),
    "user_password": pick(None, "RAGFLOW_SERVICE_USER_PASSWORD"),
}
admin_email = env.get("DEFAULT_SUPERUSER_EMAIL") or "admin@ragflow.io"
admin_password = env.get("DEFAULT_SUPERUSER_PASSWORD") or env.get("ADMIN_DEFAULT_PASSWORD") or ""
api_port = env.get("SVR_HTTP_PORT") or "9380"
admin_port = env.get("ADMIN_SVR_HTTP_PORT") or "9381"
api = f"http://{args.host}:{api_port}/api/v1"
admin_api = f"http://{args.host}:{admin_port}/api/v1/admin"

missing = [k for k, v in (("RAGFLOW_SETUP_EMBEDDING_BASE_URL / --base-url", cfg["base_url"]),
                          ("RAGFLOW_SETUP_EMBEDDING_MODEL / --model", cfg["model"])) if not v]
if missing:
    die("缺少必填設定：" + "、".join(missing) + "\n  填在 docker/.env，或用命令列參數給。")
if not cfg["base_url"].rstrip("/").endswith("/v1"):
    warn(f"⚠ base URL「{cfg['base_url']}」不是以 /v1 結尾：RAGFlow 會直接在後面接 embeddings，"
         "多數 OpenAI 相容服務需要 .../v1。")
embedding_id = f"{cfg['model']}@{cfg['instance']}@{cfg['provider']}"

bold("==> 設定")
print(f"  RAGFlow API   {api}")
print(f"  Admin API     {admin_api}（{admin_email}）")
print(f"  服務帳號      {cfg['user_email']}")
print(f"  供應商        {cfg['provider']} / instance {cfg['instance']}")
print(f"  embedding     {cfg['model']} ← {cfg['base_url']}（max_tokens {cfg['max_tokens']}，"
      f"{'有' if cfg['api_key'] else '無'} API key）")
print(f"  kb-stack 識別 {embedding_id}")
if args.dry_run:
    sys.exit(0)
if not admin_password:
    die("docker/.env 沒有 DEFAULT_SUPERUSER_PASSWORD：先跑 ./gen-env.sh")

class ApiError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg); self.code = code

def call(method, url, body=None, token=None, want_header=False, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, headers, status = r.read(), r.headers, r.status
    except urllib.error.HTTPError as e:
        raw, headers, status = e.read(), e.headers, e.code
    except urllib.error.URLError as e:
        raise ApiError(f"{method} {url} 連不上：{e.reason}")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise ApiError(f"{method} {url} → HTTP {status}，不是 JSON：{raw[:200]!r}")
    # 1.0 的錯誤多半是 HTTP 200 + 非 0 的 code
    if payload.get("code") not in (0, None) or status >= 400:
        raise ApiError(f"{method} {url} → HTTP {status} code={payload.get('code')}："
                       f"{payload.get('message')}", payload.get("code"))
    return (payload.get("data"), headers.get("Authorization")) if want_header else payload.get("data")

def step(n, s): bold(f"==> [{n}/6] {s}")

# 密碼一律送 base64(明文)：RAGFlow 存的是 hash(base64(明文))（Web 前端送 RSA(base64(明文))，
# 第一個超級管理員也是這樣建的）。伺服器解不開 RSA 時會原樣使用收到的字串，所以送 base64
# 等同前端解密後的值；送明文會被當成另一個密碼，登入得到 code 109 do not match。
# 建帳號、改密碼也要送同樣的格式，否則建出來的帳號 Web 登不進去。
def wire_pw(p): return base64.b64encode(p.encode()).decode()

# ---- 等 API 就緒 ----
deadline = time.time() + args.wait
while True:
    try:
        call("GET", f"{api}/system/healthz", timeout=5); break
    except ApiError as e:
        if time.time() > deadline:
            die(f"RAGFlow API 在 {args.wait} 秒內沒有就緒：{e}\n  先跑 ./start.sh")
        time.sleep(5)

try:
    # ---- 1) 超級管理員登入 Admin API ----
    step(1, "超級管理員登入 Admin API")
    _, admin_token = call("POST", f"{admin_api}/login",
                          {"email": admin_email, "password": wire_pw(admin_password)}, want_header=True)
    if not admin_token:
        die("Admin 登入成功但沒有回傳 Authorization header")
    ok("  ok")

    # ---- 2) 服務帳號 ----
    step(2, f"服務帳號 {cfg['user_email']}")
    if not cfg["user_password"]:
        alphabet = string.ascii_letters + string.digits
        cfg["user_password"] = "".join(secrets.choice(alphabet) for _ in range(24))
        write_env(env_file, "RAGFLOW_SERVICE_USER_EMAIL", cfg["user_email"])
        write_env(env_file, "RAGFLOW_SERVICE_USER_PASSWORD", cfg["user_password"])
        print("  密碼已產生並寫回 docker/.env（RAGFLOW_SERVICE_USER_PASSWORD）")

    def user_login():
        _, tok = call("POST", f"{api}/auth/login",
                      {"email": cfg["user_email"], "password": wire_pw(cfg["user_password"])}, want_header=True)
        return tok

    try:
        user_token = user_login()
        print("  已存在，登入 ok")
    except ApiError as e:
        msg = str(e)
        if "not registered" in msg:
            call("POST", f"{admin_api}/users",
                 {"username": cfg["user_email"], "password": wire_pw(cfg["user_password"]), "role": "user"},
                 token=admin_token)
            print("  已建立")
        elif "do not match" in msg:
            # 帳號是這支腳本管的：密碼跟 docker/.env 對不上，就以 .env 為準重設
            call("PUT", f"{admin_api}/users/{urllib.request.quote(cfg['user_email'])}/password",
                 {"new_password": wire_pw(cfg["user_password"])}, token=admin_token)
            warn("  密碼與 docker/.env 不一致，已用 Admin API 重設成 .env 的值")
        else:
            raise
        user_token = user_login()
    if not user_token:
        die("服務帳號登入成功但沒有回傳 Authorization header")
    ok("  ok")

    # ---- 3) 供應商、instance、模型 ----
    step(3, f"模型供應商 {cfg['provider']} / {cfg['instance']} / {cfg['model']}")
    prov = urllib.request.quote(cfg["provider"], safe="")
    inst = urllib.request.quote(cfg["instance"], safe="")
    call("PUT", f"{api}/providers", {"provider_name": cfg["provider"]}, token=user_token)
    instances = call("GET", f"{api}/providers/{prov}/instances", token=user_token) or []
    model_info = {"model_name": cfg["model"], "model_type": ["embedding"], "max_tokens": cfg["max_tokens"]}
    if any(i.get("instance_name") == cfg["instance"] for i in instances):
        print(f"  instance「{cfg['instance']}」已存在")
        models = call("GET", f"{api}/providers/{prov}/instances/{inst}/models", token=user_token) or []
        if any(m.get("name") == cfg["model"] for m in models):
            print(f"  模型「{cfg['model']}」已存在")
        else:
            call("POST", f"{api}/providers/{prov}/instances/{inst}/models", model_info, token=user_token)
            print(f"  已加入模型「{cfg['model']}」")
        warn("  instance 已存在時不會改它的 base URL / API key；要改請到 Web 的模型供應商頁，"
             "或刪掉 instance 後重跑。")
    else:
        call("POST", f"{api}/providers/{prov}/instances",
             {"instance_name": cfg["instance"], "api_key": cfg["api_key"], "base_url": cfg["base_url"],
              "model_info": [model_info]}, token=user_token)
        print(f"  已建立 instance「{cfg['instance']}」與模型「{cfg['model']}」")

    # 實測：用 Web「測試連線」同一支 API，由 RAGFlow 容器實際對 embedding 服務打一次，結果記在
    # 模型的 verify 狀態。不用 POST /embeddings：它要求請求帶 dimension、模型登記 max_dimension
    # 與 max_batch_size，否則回 code 400 input dimension <= 0；帶了 dimension 又會轉成
    # OpenAI 的 dimensions 參數送給 embedding 服務，不支援這個參數的服務可能拒絕。
    inst_info = call("GET", f"{api}/providers/{prov}/instances/{inst}", token=user_token) or {}
    inst_id = inst_info.get("id") if isinstance(inst_info, dict) else None
    if not inst_id:
        die(f"查不到 instance「{cfg['instance']}」的 id，無法實測連線")
    try:
        call("POST", f"{api}/providers/{prov}/connection",
             {"use_saved_credentials": True, "instance_id": inst_id,
              "model_info": [{"model_name": cfg["model"], "model_type": ["embedding"]}]},
             token=user_token, timeout=120)
    except ApiError as e:
        die(f"embedding 實測失敗（RAGFlow 容器 → {cfg['base_url']}）：{e}\n"
            "  檢查：base URL 要含 /v1、模型名稱跟服務回報的一致、從 RAGFlow 容器連得到那個位址"
            "（docker exec <ragflow-cpu 容器> curl -s <base URL>/models）。")
    ok("  embedding 實測 ok（RAGFlow 容器 → embedding 服務）")

    # ---- 4) 預設 embedding 模型 ----
    step(4, "設為服務帳號的預設 embedding 模型")
    call("PATCH", f"{api}/models/default",
         {"model_provider": cfg["provider"], "model_instance": cfg["instance"],
          "model_name": cfg["model"], "model_type": "embedding"}, token=user_token)
    ok("  ok")

    # ---- 5) API key ----
    step(5, "API key")
    keys = call("GET", f"{api}/system/tokens", token=user_token) or []
    tokens = {k.get("token") for k in keys if isinstance(k, dict)}
    api_key = env.get("RAGFLOW_KB_API_KEY", "")
    if api_key and api_key in tokens:
        print("  沿用 docker/.env 記錄的 key（RAGFLOW_KB_API_KEY）")
    else:
        created = call("POST", f"{api}/system/tokens", {"name": "kb-stack"}, token=user_token)
        api_key = (created or {}).get("token", "")
        if not api_key:
            die(f"建立 API key 沒有回傳 token：{created!r}")
        write_env(env_file, "RAGFLOW_KB_API_KEY", api_key)
        print("  已建立，記錄在 docker/.env（RAGFLOW_KB_API_KEY）")
    call("GET", f"{api}/datasets?page_size=1", token=f"Bearer {api_key}")
    ok("  用這把 key 呼叫 /datasets ok")
except ApiError as e:
    die(f"失敗：{e}")

# ---- 6) 給 kb-stack 的設定 ----
step(6, "kb-stack 設定")
public_host = args.public_host or ("<這台主機>" if args.host in ("127.0.0.1", "localhost") else args.host)
lines = [
    f"RAGFLOW_BASE_URL=http://{public_host}:{api_port}",
    f"RAGFLOW_API_KEY={api_key}",
    f"RAGFLOW_EMBEDDING_MODEL={embedding_id}",
]
out = os.path.join(args.dir, "kb-stack.ragflow.env")
with open(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
    f.write("# 由 setup-models.sh 產生：貼進 kb-stack 的 .env，再 docker compose up -d\n")
    f.write("\n".join(lines) + "\n")
for l in lines:
    print("  " + l)
print(f"  （也寫在 {out}，權限 600）")
print()
print(f"Web 登入請用服務帳號 {cfg['user_email']}（密碼見 ./gen-env.sh --show 或 docker/.env 的"
      " RAGFLOW_SERVICE_USER_PASSWORD）；超級管理員只能登入 Admin。")
PY
