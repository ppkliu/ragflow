#!/usr/bin/env python3
"""把舊版 RAGFlow（0.24 / 0.26）的 dataset 搬到 1.0：下載原檔 → 1.0 建同名 dataset → 上傳 → 解析 → 比對。

兩版的儲存格式與 API 都變了，ES / MinIO 的檔案不能直接搬，只能「原檔重新上傳、重新解析」。
解析設定（chunk 方法、parser_config、語言、權限、說明）與文件自己的 chunk 方法、metadata 會一起帶過去。

kb-stack 建的 dataset（space_*）**預設略過**：那些由 kb-stack/scripts/ragflow-migrate.sh 走
kb-stack 自己的管線重建（Cognee 與 ragflow_document_id 才對得起來）。kbbench_* / kbstack_* 是
壓測與評測的暫用 dataset，也略過。

用法（在任何連得到兩邊 API 的主機上執行；只用 Python 標準函式庫）：
  agentwiki/migrate-datasets.py list   --src <舊版網址> --src-key env:SRC_KEY
  agentwiki/migrate-datasets.py plan   --src ... --src-key ... --dst <1.0 網址> --dst-key env:DST_KEY [--dataset a,b | --all]
  agentwiki/migrate-datasets.py run    --src ... --dst ... (--dataset a,b | --all) [--batch 10] [--no-parse]
  agentwiki/migrate-datasets.py verify --src ... --dst ... [--dataset a,b | --all]

  list    列出來源帳號看得到的 dataset：文件數、chunk 數、chunk 方法，標出會略過的
  plan    要建哪些 dataset、上傳幾份、多少 MB；不寫入任何東西
  run     真的搬。**可以重跑**：目標已有同名 dataset 就沿用，已有同名同大小的文件就略過，
          已上傳但還沒解析的會補解析。中斷了直接再跑一次
  verify  兩邊逐 dataset 比文件數、解析成功數、chunk 數，列出目標缺少或解析失敗的文件

離峰時段自動搬（見 timewindow.py 的時段寫法）：
  agentwiki/migrate-datasets.py run ... --window "mon-fri 22:30-07:00; sat-sun all"
      只在時段內做：時段外直接結束（代碼 3）；快結束時不再上傳、不再排下一批解析
      （一批要多久用前面各批估），等手上這批解析完就暫停（代碼 3），下次 run 接著做
  agentwiki/migrate-datasets.py schedule-install <run 的參數> [--window ...] [--every 15]
      裝一行 crontab，每 15 分鐘叫一次 run --window --scheduled；全部一致後自動移除，
      連續 3 次失敗也移除（同一個問題每 15 分鐘重試沒有意義）。key 要寫成 file:<路徑>
  agentwiki/migrate-datasets.py schedule-status | schedule-remove

  報告都附「dataset id 對照」（舊 id → 新 id，也輸出成 id-map.json）：其他 agent 工具若用 dataset id
  而不是名稱查詢，切到 1.0 時要照這張表改設定。kb-stack 的 space_* 由 ragflow-migrate.sh 重建後，
  用 verify --include-kb 也能拿到它們的對照。

選項：
  --src / --dst          base URL，例如 http://<主機>:9380（舊版）、http://<主機>:19383（1.0）
  --src-key / --dst-key  API key；寫成 env:變數名 時從環境變數讀、file:路徑 時從檔案讀
                         （key 不會留在 ps 與 shell history；排程只能用 file:，cron 沒有你的環境變數）
  --dataset a,b          只處理這些 dataset（名稱或 id）
  --all                  全部（run 沒給 --dataset 時必須明確加）
  --include-kb           連 kb-stack 的 space_* 一起搬（通常不要，見上）
  --embedding <模型>     目標 dataset 用的 embedding（預設：1.0 帳號的預設 embedding）。
                         舊版的 embedding 名稱帶舊供應商字樣，1.0 不一定認得，所以不沿用
  --batch <n>            一批觸發幾份解析，等這批結束才排下一批（預設 10；1.0 的解析只用 CPU）
  --batch-timeout <分>   一批最多等多久（預設 120）
  --no-parse             只上傳不解析
  --out <目錄>           報告位置（預設 AgentWiki/bak/ragflow-migrate/datasets-<時間>，
                         不在 AgentWiki 工作區時是 ~/ragflow-migrate/datasets-<時間>；排程固定用 datasets-scheduled）
  --window <時段>        只在這個時段內搬（run）；schedule-install 沒給時是 "mon-fri 22:30-07:00; sat-sun all"
  --every <分>           schedule-install：crontab 每幾分鐘叫一次（預設 15）

★ API key 決定看得到哪些 dataset：每個 key 只看得到自己帳號（tenant）的。其他 agent 工具若各用
  各的 RAGFlow 帳號，要對每個帳號各跑一次，目標端也用對應的 1.0 帳號的 key。
★ 0.24 與 0.26 是同一套 API（/api/v1、PUT 改文件），1.0 是新的（建 dataset 要 parse_type /
  parser_id、PATCH 改文件、ingestion_status）。兩邊各自偵測版本，不必指定。
★ 1.0 只拒絕不認得的欄位，不會默默丟掉：舊版的 parser_config 有 1.0 不收的鍵時，建 dataset 會失敗，
  這時改用 1.0 的預設 parser_config 重建並在報告註記，chunk 結果可能跟舊版不同。

★ 同一時間只會有一個 run（手動與排程的都算）：報告目錄上一層的 migrate-datasets.lock。

結束代碼：0 = 全部成功；1 = 有失敗、逾時或比對不一致；2 = 用法錯誤；
          3 = 時段外沒跑、或時段結束暫停（下次接著做）；4 = 已有一個 run 在跑
"""
import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench import Api, detect, doc_state, log, multipart  # noqa: E402
import timewindow  # noqa: E402

CRON_TAG = "# agentwiki-migrate-datasets:" + os.path.abspath(__file__)
SCHEDULED_DIR = "datasets-scheduled"
UPLOAD_EST_S = 60     # 估一份上傳要多久（含下載）
MARGIN_S = 300        # 時段結束前留的餘量

SKIP_PREFIXES_KB = ("space_",)
SKIP_PREFIXES_TEMP = ("kbbench_", "kbstack_")
PAGE = 100


# ---------------------------------------------------------------- 兩版差異

class Side:
    """一邊的 RAGFlow：API、方言（v0 = 0.x，v1 = 1.0）與版本。"""

    def __init__(self, label, base, key):
        self.label = label
        self.api = Api(base, key)
        self.dialect, self.version = detect(self.api)

    def __str__(self):
        return f"{self.label} {self.api.base}（{self.version}，{self.dialect} API）"

    # 列表 -----------------------------------------------------------
    def datasets(self):
        out, page = [], 1
        while True:
            data = self.api.ok("GET", f"/api/v1/datasets?page={page}&page_size={PAGE}", what=f"{self.label} 列 dataset")
            rows = data if isinstance(data, list) else (data or {}).get("datasets", [])
            out += rows
            if len(rows) < PAGE:
                return out
            page += 1

    def find_dataset(self, name):
        for d in self.datasets():
            if d.get("name") == name:
                return d
        return None

    def documents(self, ds_id):
        out, page = [], 1
        while True:
            data = self.api.ok("GET", f"/api/v1/datasets/{ds_id}/documents?page={page}&page_size={PAGE}",
                               what=f"{self.label} 列文件")
            rows = data if isinstance(data, list) else (data or {}).get("docs", [])
            out += rows
            if len(rows) < PAGE:
                return out
            page += 1

    # 寫入 -----------------------------------------------------------
    def create_dataset(self, src, embedding, with_parser_config=True):
        body = {"name": src["name"]}
        method = chunk_method(src)
        if self.dialect == "v1":
            body.update({"parse_type": 1, "parser_id": method})
        else:
            body["chunk_method"] = method
        for k in ("language", "permission"):
            if src.get(k):
                body[k] = src[k]
        if with_parser_config and isinstance(src.get("parser_config"), dict) and src["parser_config"]:
            body["parser_config"] = src["parser_config"]
        if embedding:
            body["embedding_model"] = embedding
        st, p = self.api.call("POST", "/api/v1/datasets", body)
        if st >= 400 or p.get("code") != 0:
            raise RuntimeError(f"建立 dataset 失敗：HTTP {st} code={p.get('code')} {p.get('message')}")
        ds = p["data"]
        # 1.0 建立時不收 description，建好再補
        if src.get("description"):
            self.api.call("PUT", f"/api/v1/datasets/{ds['id']}", {"description": src["description"]})
        return ds

    def upload(self, ds_id, name, content):
        raw, ctype = multipart("file", name, content)
        st, p = self.api.call("POST", f"/api/v1/datasets/{ds_id}/documents", raw=raw, content_type=ctype, timeout=900)
        data = p.get("data") if isinstance(p.get("data"), list) else []
        if st >= 400 or p.get("code") != 0 or not data:
            raise RuntimeError(f"上傳失敗：HTTP {st} code={p.get('code')} {p.get('message')}")
        return data[0]["id"]

    def update_document(self, ds_id, doc_id, body):
        verb = "PATCH" if self.dialect == "v1" else "PUT"
        st, p = self.api.call(verb, f"/api/v1/datasets/{ds_id}/documents/{doc_id}", body)
        return st < 400 and p.get("code") == 0, p.get("message")

    def parse(self, ds_id, doc_ids):
        st, p = self.api.call("POST", f"/api/v1/datasets/{ds_id}/chunks", {"document_ids": doc_ids})
        if st == 404:  # 更早期 0.x 只有逐檔端點
            for d in doc_ids:
                self.api.ok("POST", f"/api/v1/datasets/{ds_id}/documents/{d}/parse", {}, what="觸發解析")
        elif st >= 400 or p.get("code") != 0:
            raise RuntimeError(f"觸發解析失敗：HTTP {st} code={p.get('code')} {p.get('message')}")

    # 讀檔 -----------------------------------------------------------
    def download(self, ds_id, doc_id, timeout=900):
        req = urllib.request.Request(f"{self.api.base}/api/v1/datasets/{ds_id}/documents/{doc_id}",
                                     headers={"Authorization": f"Bearer {self.api.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body, ctype = r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"下載失敗：HTTP {e.code}") from e
        # 錯誤時兩版都回 200 + JSON（code != 0）；真正的 JSON 檔案沒有 code 欄位
        if "json" in ctype.lower() and len(body) < 65536:
            try:
                p = json.loads(body)
                if isinstance(p, dict) and "code" in p and p.get("code") != 0:
                    raise RuntimeError(f"下載失敗：code={p.get('code')} {p.get('message')}")
            except ValueError:
                pass
        return body


def _now():
    return time.time()


class Deadline:
    """--window：時段結束前停手。排進 RAGFlow 的解析中途停不下來，所以是「不再開始新的」。"""

    def __init__(self, until=None):
        self.until = until          # epoch 秒；None = 不限
        self.batches = []           # 各批解析實際耗時（秒）
        self.paused = False

    def batch_est(self):
        b = self.batches
        return max(sum(b) / len(b), b[-1]) if b else 0

    def allows(self, est):
        """剩下的時間夠做完 est 秒（加餘量）才回 True；不夠就記下暫停。"""
        if self.until is not None and self.until - _now() < est + MARGIN_S:
            self.paused = True
        return not self.paused


def chunk_method(obj):
    return obj.get("chunk_method") or obj.get("parser_id") or "naive"


def num(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if isinstance(v, (int, float)):
            return v
    return 0


def raw_state(doc):
    return str(doc.get("ingestion_status", doc.get("run", ""))).strip().upper()


def unstarted(doc):
    """已上傳、從沒排過解析（0.x run=UNSTART/0；1.0 UNSTART/CREATED）。進行中的不重排，免得打斷。"""
    return raw_state(doc) in ("UNSTART", "0", "CREATED", "") and not num(doc, "progress")


def skip_reason(ds, include_kb):
    name = ds.get("name", "")
    if name.startswith(SKIP_PREFIXES_TEMP):
        return "暫用（壓測 / 評測）"
    if not include_kb and name.startswith(SKIP_PREFIXES_KB):
        return "kb-stack（用 ragflow-migrate.sh 重建）"
    return None


def select(datasets, wanted, include_kb):
    """回傳 [(dataset, 略過原因 或 None)]。wanted 指名的不看略過規則（明確要搬就搬）。"""
    out = []
    for d in datasets:
        if wanted is not None:
            if d.get("name") in wanted or d.get("id") in wanted:
                out.append((d, None))
            continue
        out.append((d, skip_reason(d, include_kb)))
    if wanted is not None:
        found = {d.get("name") for d, _ in out} | {d.get("id") for d, _ in out}
        missing = [w for w in wanted if w not in found]
        if missing:
            raise SystemExit(f"來源找不到 dataset：{', '.join(missing)}")
    return out


def mb(b):
    return f"{(b or 0) / 1048576:.1f}"


# ---------------------------------------------------------------- 動作

def cmd_list(src, args):
    rows = select(src.datasets(), None, args.include_kb)
    print(f"{'dataset':40} {'文件':>6} {'chunks':>8} {'chunk 方法':12} 備註")
    for d, why in rows:
        print(f"{d.get('name', '')[:40]:40} {num(d, 'document_count', 'doc_num'):>6} "
              f"{num(d, 'chunk_count', 'chunk_num'):>8} {chunk_method(d):12} {('略過：' + why) if why else ''}")
    n = sum(1 for _, why in rows if not why)
    print(f"\n共 {len(rows)} 個，預設會搬 {n} 個（--include-kb 連 space_* 一起；--dataset 可指名）。")
    print("★ 只列得出這把 API key 所屬帳號的 dataset；其他帳號要用它們自己的 key 再列一次。")
    return 0


def index_by_name(docs):
    idx = {}
    for d in docs:
        idx.setdefault(d.get("name"), []).append(d)
    return idx


def match(target_idx, src_doc):
    """目標已有同名、同大小的文件就是同一份（重跑時略過）。大小缺漏時只比名稱。"""
    for d in target_idx.get(src_doc.get("name"), []):
        if not src_doc.get("size") or not d.get("size") or d.get("size") == src_doc.get("size"):
            return d
    return None


def plan_one(src, dst, ds):
    docs = src.documents(ds["id"])
    target = dst.find_dataset(ds["name"])
    tidx = index_by_name(dst.documents(target["id"])) if target else {}
    todo = [d for d in docs if not match(tidx, d)]
    return {"name": ds["name"], "src_id": ds["id"], "docs": len(docs), "target": target["id"] if target else None,
            "upload": len(todo), "upload_bytes": sum(num(d, "size") for d in todo)}


def cmd_plan(src, dst, args, wanted):
    rows = [(d, why) for d, why in select(src.datasets(), wanted, args.include_kb)]
    plans = []
    print(f"{'dataset':40} {'目標':10} {'來源文件':>8} {'要上傳':>6} {'MB':>8}")
    for d, why in rows:
        if why:
            print(f"{d['name'][:40]:40} 略過：{why}")
            continue
        p = plan_one(src, dst, d)
        plans.append(p)
        print(f"{p['name'][:40]:40} {'沿用' if p['target'] else '新建':10} {p['docs']:>8} {p['upload']:>6} {mb(p['upload_bytes']):>8}")
    print(f"\n要上傳 {sum(p['upload'] for p in plans)} 份、{mb(sum(p['upload_bytes'] for p in plans))} MB。"
          "這是檢查模式，沒有寫入任何東西；確定後改用 run。")
    return plans


def wait_parsed(dst, ds_id, ids, timeout_s, poll=10):
    """等這批文件解析結束，回傳 {doc_id: 目標文件}（state 不是 done/failed/canceled 的算逾時）。"""
    pending, final = set(ids), {}
    deadline, last = time.monotonic() + timeout_s, time.monotonic()
    while pending and time.monotonic() < deadline:
        time.sleep(poll)
        for d in dst.documents(ds_id):
            if d.get("id") in pending and doc_state(d) != "pending":
                final[d["id"]] = d
                pending.discard(d["id"])
                log(f"     {doc_state(d):8} {d.get('name')}（{num(d, 'chunk_count', 'chunk_num')} chunks）")
        if pending and time.monotonic() - last > 60:
            log(f"     等待中：{len(pending)} 份未完成")
            last = time.monotonic()
    return final, pending


def migrate_one(src, dst, ds, args, dl=None):
    dl = dl or Deadline()
    res = {"name": ds["name"], "src_id": ds["id"], "uploaded": 0, "skipped": 0, "failed": [], "notes": [],
           "paused": False, "parsed": {"done": 0, "failed": 0, "canceled": 0, "timeout": 0}}
    log(f"== {ds['name']}（{chunk_method(ds)}）")
    target = dst.find_dataset(ds["name"])
    if target:
        res["notes"].append("目標已有同名 dataset，沿用")
        log("   目標已有同名 dataset，沿用")
    else:
        try:
            target = dst.create_dataset(ds, args.embedding)
        except RuntimeError as e:
            if "parser_config" not in ds or not ds.get("parser_config"):
                raise
            log(f"   帶舊版 parser_config 建立失敗（{e}），改用 1.0 預設 parser_config")
            res["notes"].append(f"parser_config 不相容，用 1.0 預設：{e}")
            target = dst.create_dataset(ds, args.embedding, with_parser_config=False)
        log(f"   已建立 dataset {target['id']}")
    res["dst_id"] = target["id"]
    ds_method = chunk_method(ds)

    tidx = index_by_name(dst.documents(target["id"]))
    to_parse, names = [], {}
    for d in src.documents(ds["id"]):
        have = match(tidx, d)
        if have:
            res["skipped"] += 1
            if not args.no_parse and unstarted(have):
                to_parse.append(have["id"])  # 上次上傳了、沒排到解析
                names[have["id"]] = d.get("name")
            continue
        if not dl.allows(UPLOAD_EST_S):
            log("   ⏸ 離峰時段快結束，不再上傳")
            break
        try:
            content = src.download(ds["id"], d["id"])
            new_id = dst.upload(target["id"], d["name"], content)
        except RuntimeError as e:
            res["failed"].append({"file": d.get("name"), "error": str(e)})
            log(f"   ✗ {d.get('name')}：{e}")
            continue
        res["uploaded"] += 1
        # 文件自己的設定與 metadata（跟 dataset 不同時才改）
        patch = {}
        if chunk_method(d) != ds_method:
            patch["chunk_method"] = chunk_method(d)
            if isinstance(d.get("parser_config"), dict) and d["parser_config"]:
                patch["parser_config"] = d["parser_config"]
        if isinstance(d.get("meta_fields"), dict) and d["meta_fields"]:
            patch["meta_fields"] = d["meta_fields"]
        if patch:
            ok, msg = dst.update_document(target["id"], new_id, patch)
            if not ok:
                res["notes"].append(f"{d.get('name')}：文件設定沒帶過去（{', '.join(patch)}）：{msg}")
        log(f"   ↑ {d.get('name')}（{mb(len(content))} MB）")
        to_parse.append(new_id)
        names[new_id] = d.get("name")

    if not (args.no_parse or not to_parse):
        parse_batches(dst, target, to_parse, names, args, dl, res)
    res["paused"] = dl.paused
    return res


def parse_batches(dst, target, to_parse, names, args, dl, res):
    for i in range(0, len(to_parse), args.batch):
        batch = to_parse[i:i + args.batch]
        if not dl.allows(dl.batch_est()):
            log(f"   ⏸ 離峰時段快結束，剩 {len(to_parse) - i} 份還沒排解析（下次 run 會補排）")
            return
        log(f"   解析 [{min(i + args.batch, len(to_parse))}/{len(to_parse)}]")
        t0 = _now()
        dst.parse(target["id"], batch)
        final, pending = wait_parsed(dst, target["id"], batch, args.batch_timeout * 60)
        dl.batches.append(_now() - t0)
        for d in final.values():
            res["parsed"][doc_state(d)] = res["parsed"].get(doc_state(d), 0) + 1
            if doc_state(d) != "done":
                res["failed"].append({"file": d.get("name"),
                                      "error": f"解析{doc_state(d)}：{str(d.get('progress_msg') or '')[-300:]}"})
        res["parsed"]["timeout"] += len(pending)
        for pid in pending:
            res["failed"].append({"file": names.get(pid, pid), "error": f"這批等了 {args.batch_timeout} 分鐘仍未完成"})


def compare_one(src, dst, ds):
    sdocs = src.documents(ds["id"])
    target = dst.find_dataset(ds["name"])
    row = {"name": ds["name"], "src_id": ds["id"], "dst_id": None, "src_docs": len(sdocs),
           "src_done": sum(1 for d in sdocs if doc_state(d) == "done"),
           "src_chunks": sum(num(d, "chunk_count", "chunk_num") for d in sdocs),
           "dst_docs": 0, "dst_done": 0, "dst_failed": 0, "dst_chunks": 0, "missing": [], "not_done": []}
    if not target:
        row["missing"] = [d.get("name") for d in sdocs]
        return row
    tdocs = dst.documents(target["id"])
    tidx = index_by_name(tdocs)
    row.update({"dst_id": target["id"], "dst_docs": len(tdocs),
                "dst_done": sum(1 for d in tdocs if doc_state(d) == "done"),
                "dst_failed": sum(1 for d in tdocs if doc_state(d) == "failed"),
                "dst_chunks": sum(num(d, "chunk_count", "chunk_num") for d in tdocs)})
    for d in sdocs:
        t = match(tidx, d)
        if not t:
            row["missing"].append(d.get("name"))
        elif doc_state(t) != "done" and doc_state(d) == "done":
            row["not_done"].append(f"{d.get('name')}（{doc_state(t)}）")
    return row


def row_ok(r):
    return not r["missing"] and not r["not_done"]


def write_report(out, src, dst, args, results, compare, paused=False):
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "result.json"), "w") as f:
        json.dump({"src": str(src), "dst": str(dst), "results": results, "compare": compare},
                  f, ensure_ascii=False, indent=2)
    L = [f"# RAGFlow dataset 搬移（{datetime.now():%Y-%m-%d %H:%M}）", "",
         f"- 來源：{src.version}（{src.dialect} API）", f"- 目標：{dst.version}（{dst.dialect} API）",
         f"- embedding：{args.embedding or '目標帳號預設'}"]
    if getattr(args, "window", None):
        L.append(f"- 時段：`{args.window}`")
    if paused:
        L.append("- ⏸ **離峰時段結束，暫停**：還沒做完的下次 run 會接著做（下面的比對因此還不一致）")
    L.append("")
    if results:
        L += ["## 搬移", "", "| dataset | 上傳 | 已存在略過 | 解析成功 | 解析失敗 | 逾時 | 上傳失敗 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
        for r in results:
            up_fail = sum(1 for f in r["failed"] if f["error"].startswith(("下載", "上傳")))
            L.append(f"| {r['name']} | {r['uploaded']} | {r['skipped']} | {r['parsed']['done']} | "
                     f"{r['parsed']['failed']} | {r['parsed']['timeout']} | {up_fail} |")
        L.append("")
        for r in results:
            for n in r["notes"]:
                L.append(f"- `{r['name']}`：{n}")
            for f in r["failed"][:20]:
                L.append(f"- `{r['name']}` ✗ `{f['file']}`：{f['error']}")
        L.append("")
    L += ["## 比對（以名稱 + 大小對應文件）", "",
          "| dataset | 來源文件 | 來源已解析 | 來源 chunks | 目標文件 | 目標已解析 | 目標失敗 | 目標 chunks | chunk 比 | 結果 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for c in compare:
        ratio = f"{c['dst_chunks'] / c['src_chunks']:.2f}" if c["src_chunks"] else "—"
        L.append(f"| {c['name']} | {c['src_docs']} | {c['src_done']} | {c['src_chunks']} | {c['dst_docs']} | "
                 f"{c['dst_done']} | {c['dst_failed']} | {c['dst_chunks']} | {ratio} | {'✓' if row_ok(c) else '✗'} |")
    L.append("")
    for c in compare:
        if c["missing"]:
            L.append(f"- `{c['name']}` 目標缺 {len(c['missing'])} 份：{', '.join(map(str, c['missing'][:10]))}")
        if c["not_done"]:
            L.append(f"- `{c['name']}` 舊版解析成功、新版沒有：{', '.join(c['not_done'][:10])}")
    L += ["", "chunk 比：1.0 與舊版的切法、預設 parser_config 不同，數量不會完全一樣；"
          "差很多（例如 < 0.5 或 > 2）時看該 dataset 的 chunk 方法與 parser_config。", ""]
    L += ["## dataset id 對照（其他工具用 id 查詢時照這張改）", "", "| dataset | 舊 id | 新 id |", "|---|---|---|"]
    for c in compare:
        L.append(f"| {c['name']} | `{c['src_id']}` | {('`' + c['dst_id'] + '`') if c['dst_id'] else '（目標還沒有）'} |")
    L.append("")
    with open(os.path.join(out, "id-map.json"), "w") as f:
        json.dump({c["name"]: {"old": c["src_id"], "new": c["dst_id"]} for c in compare}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write("\n".join(L))
    return os.path.join(out, "report.md")


def default_out():
    here = os.path.dirname(os.path.abspath(__file__))
    bak = os.path.normpath(os.path.join(here, "..", "..", "bak"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.isdir(bak) and os.access(bak, os.W_OK):
        return os.path.join(bak, "ragflow-migrate", f"datasets-{stamp}")
    return os.path.join(os.path.expanduser("~"), "ragflow-migrate", f"datasets-{stamp}")


def key_of(v):
    if v and v.startswith("env:"):
        k = os.environ.get(v[4:])
        if not k:
            raise SystemExit(f"環境變數 {v[4:]} 沒有設定")
        return k
    if v and v.startswith("file:"):
        try:
            with open(os.path.expanduser(v[5:]), encoding="utf-8") as f:
                k = f.read().strip()
        except OSError as e:
            raise SystemExit(f"讀不到 key 檔 {v[5:]}：{e}") from e
        if not k:
            raise SystemExit(f"key 檔 {v[5:]} 是空的")
        return k
    return v


# ---------------------------------------------------------------- 排程

def lock_run(out):
    """同一時間只能有一個 run。回傳要一直留著的檔案物件；已有人在跑就回 None。"""
    base = os.path.dirname(os.path.abspath(out))
    os.makedirs(base, exist_ok=True)
    f = open(os.path.join(base, "migrate-datasets.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def crontab_lines():
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError as e:
        raise SystemExit("這台沒有 crontab 指令（裝 cron，或用 systemd timer 定時跑 run --window --scheduled）") from e
    return r.stdout.splitlines() if r.returncode == 0 else []


def crontab_write(lines):
    subprocess.run(["crontab", "-"], input="".join(x + "\n" for x in lines), text=True, check=True)


def schedule_remove(reason):
    lines = crontab_lines()
    keep = [x for x in lines if CRON_TAG not in x]
    if len(keep) == len(lines):
        log("沒有安裝排程")
        return False
    crontab_write(keep)
    log(f"已移除排程的 crontab（{reason}）")
    return True


def scheduled_out(args):
    return args.out or os.path.join(os.path.dirname(default_out()), SCHEDULED_DIR)


RUN_FLAGS = ("--src", "--src-key", "--dst", "--dst-key", "--dataset", "--embedding", "--batch", "--batch-timeout")


def schedule_install(args, wanted):
    for k in ("src_key", "dst_key"):
        if not str(getattr(args, k) or "").startswith("file:"):
            raise SystemExit(f"排程的 --{k.replace('_', '-')} 要寫成 file:<路徑>：cron 沒有你的環境變數，"
                             "直接寫 key 會留在 crontab 裡。例如 printf '%s' \"$SRC_KEY\" > ~/.ragflow-src.key; chmod 600 ~/.ragflow-src.key")
    if not 1 <= args.every <= 59:
        raise SystemExit("--every 要是 1–59 的分鐘數")
    window = args.window or timewindow.DEFAULT
    segs = timewindow.parse(window)
    out = os.path.abspath(scheduled_out(args))
    os.makedirs(out, exist_ok=True)
    for k in ("src_key", "dst_key"):
        key_of(getattr(args, k))   # 現在就讀一次，key 檔有問題不要等到半夜才發現
    cmd = [sys.executable, os.path.abspath(__file__), "run"]
    for flag in RUN_FLAGS:
        v = getattr(args, flag[2:].replace("-", "_"))
        if v is not None:
            cmd += [flag, str(v)]
    for flag in ("--all", "--include-kb", "--no-parse"):
        if getattr(args, flag[2:].replace("-", "_")):
            cmd.append(flag)
    cmd += ["--window", window, "--scheduled", "--out", out]
    path = ":".join(dict.fromkeys([os.path.dirname(sys.executable), "/usr/local/bin", "/usr/bin", "/bin"]))
    line = (f"*/{args.every} * * * * PATH={shlex.quote(path)} {' '.join(shlex.quote(c) for c in cmd)} "
            f">> {shlex.quote(os.path.join(out, 'cron.log'))} 2>&1 {CRON_TAG}")
    if "%" in line:
        raise SystemExit("參數或路徑裡有 %（crontab 會當成換行），換個 --out 或 key 檔路徑")
    crontab_write([x for x in crontab_lines() if CRON_TAG not in x] + [line])
    for f in ("done", "failures"):
        if os.path.exists(os.path.join(out, f)):
            os.remove(os.path.join(out, f))
    now = datetime.now()
    log("已安裝排程（crontab -l 可以看到）")
    log(f"  時段：{window}；每 {args.every} 分鐘檢查一次；dataset：{','.join(wanted) if wanted else '全部'}")
    if timewindow.is_open(segs, now):
        log(f"  現在就在時段內，{args.every} 分鐘內開始")
    else:
        nxt = timewindow.opens_at(segs, now)
        log(f"  下次開始：{nxt:%m-%d（%a）%H:%M}" if nxt else "  這個時段永遠不會開始")
    log(f"  報告與每天的日誌：{out}；進度：schedule-status；取消：schedule-remove")
    return 0


def schedule_status(args):
    lines = [x for x in crontab_lines() if CRON_TAG in x]
    print("crontab：" + ("已安裝" if lines else "沒有安裝"))
    for x in lines:
        print(f"  {x}")
    out = scheduled_out(args)
    if lines:
        try:
            parts = shlex.split(lines[0].split(CRON_TAG)[0])
            window = parts[parts.index("--window") + 1]
            out = parts[parts.index("--out") + 1]
            segs, now = timewindow.parse(window), datetime.now()
            if timewindow.is_open(segs, now):
                end = timewindow.closes_at(segs, now)
                print(f"現在：時段內（{window}），到 {end:%m-%d（%a）%H:%M}" if end else f"現在：時段內（{window}）")
            else:
                nxt = timewindow.opens_at(segs, now)
                print(f"現在：時段外（{window}），下次開始 {nxt:%m-%d（%a）%H:%M}" if nxt else f"現在：時段外（{window}）")
        except (ValueError, IndexError):
            pass
    lock = lock_run(os.path.join(out, "x"))
    print("執行中：" + ("否" if lock else "是"))
    if lock:
        lock.close()
    for name, label in (("done", "已全部完成"), ("failures", "連續失敗次數（達 3 次會停止排程）")):
        p = os.path.join(out, name)
        if os.path.exists(p):
            with open(p) as f:
                print(f"{label}：{f.read().strip() or '是'}")
    logs = sorted(x for x in os.listdir(out) if x.startswith("schedule-") and x.endswith(".log")) if os.path.isdir(out) else []
    if logs:
        print(f"最近的日誌 {os.path.join(out, logs[-1])}：")
        with open(os.path.join(out, logs[-1]), encoding="utf-8") as f:
            for x in f.read().splitlines()[-10:]:
                print(f"  {x}")
    if os.path.exists(os.path.join(out, "report.md")):
        print(f"報告：{os.path.join(out, 'report.md')}")
    return 0


def after_scheduled(out, code):
    """--scheduled 的 run 結束後：完成就移除排程，連續失敗 3 次也移除。"""
    fp = os.path.join(out, "failures")
    if code == 0:
        open(os.path.join(out, "done"), "w").close()
        if os.path.exists(fp):
            os.remove(fp)
        schedule_remove("全部一致")
    elif code == 3:
        if os.path.exists(fp):
            os.remove(fp)
    elif code == 1:
        n = 1
        if os.path.exists(fp):
            with open(fp) as f:
                n = int(f.read().strip() or 0) + 1
        with open(fp, "w") as f:
            f.write(str(n))
        log(f"第 {n} 次連續失敗")
        if n >= 3:
            schedule_remove(f"連續 {n} 次失敗，看 {os.path.join(out, 'report.md')} 修好再 schedule-install")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="migrate-datasets.py", add_help=True,
                                 description="把舊版 RAGFlow 的 dataset 搬到 1.0（說明見檔頭）")
    ap.add_argument("action", choices=["list", "plan", "run", "verify",
                                       "schedule-install", "schedule-status", "schedule-remove"])
    ap.add_argument("--src")
    ap.add_argument("--src-key")
    ap.add_argument("--dst")
    ap.add_argument("--dst-key")
    ap.add_argument("--dataset")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--include-kb", action="store_true")
    ap.add_argument("--embedding")
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--batch-timeout", type=int, default=120)
    ap.add_argument("--no-parse", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--window")
    ap.add_argument("--every", type=int, default=15)
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.action == "schedule-status":
        return schedule_status(args)
    if args.action == "schedule-remove":
        schedule_remove("手動移除")
        return 0
    if not (args.src and args.src_key):
        ap.error(f"{args.action} 需要 --src 與 --src-key")

    if args.batch < 1:
        ap.error("--batch 要是正整數")
    wanted = [s.strip() for s in args.dataset.split(",") if s.strip()] if args.dataset else None
    if args.action in ("run", "schedule-install") and wanted is None and not args.all:
        ap.error(f"{args.action} 要搭配 --dataset <a,b> 或 --all（避免一不小心搬全部）")
    if args.action != "list" and not (args.dst and args.dst_key):
        ap.error(f"{args.action} 需要 --dst 與 --dst-key")
    if args.scheduled and not args.window:
        ap.error("--scheduled 要搭配 --window")
    if args.window:
        try:
            segs = timewindow.parse(args.window)
        except ValueError as e:
            ap.error(f"--window 寫錯：{e}")
    if args.action == "schedule-install":
        return schedule_install(args, wanted)

    dl, out = Deadline(), args.out or default_out()
    if args.action == "run":
        if args.window:
            now = datetime.now()
            if not timewindow.is_open(segs, now):
                if not args.scheduled:   # 排程每 15 分鐘叫一次，時段外不留紀錄
                    nxt = timewindow.opens_at(segs, now)
                    log(f"現在不在離峰時段（{args.window}），不跑；下次開始 "
                        + (f"{nxt:%m-%d（%a）%H:%M}" if nxt else "：永不"))
                return 3
            end = timewindow.closes_at(segs, now)
            dl = Deadline(end.timestamp() if end else None)
        lock = lock_run(out)
        if lock is None:
            if not args.scheduled:
                log("已經有一個 run 在跑（migrate-datasets.lock），這次不跑")
            return 4
        if not args.scheduled:
            try:
                return run_main(ap, args, wanted, dl, out)
            finally:
                lock.close()
        # 排程：每天一個日誌檔；完成或連續失敗時移除排程
        os.makedirs(out, exist_ok=True)
        code, saved = 1, sys.stdout
        with open(os.path.join(out, f"schedule-{datetime.now():%Y%m%d}.log"), "a", encoding="utf-8") as f:
            sys.stdout = f
            try:
                log(f"===== 排程啟動（{args.window}）=====")
                code = run_main(ap, args, wanted, dl, out)
            finally:
                after_scheduled(out, code)
                log(f"===== 結束（結束代碼 {code}）=====")
                sys.stdout = saved
                lock.close()
        return code
    return run_main(ap, args, wanted, dl, out)


def run_main(ap, args, wanted, dl, out):
    src = Side("來源", args.src, key_of(args.src_key))
    log(f"{src}")
    if args.action == "list":
        return cmd_list(src, args)

    dst = Side("目標", args.dst, key_of(args.dst_key))
    log(f"{dst}")
    if src.api.base == dst.api.base:
        ap.error("--src 與 --dst 是同一台")
    if dst.dialect != "v1":
        log(f"⚠ 目標是 {dst.version}，不是 1.0；照樣搬，但這支工具是為了搬到 1.0 寫的")

    if args.action == "plan":
        cmd_plan(src, dst, args, wanted)
        return 0

    chosen = [d for d, why in select(src.datasets(), wanted, args.include_kb) if not why]
    results = []
    if args.action == "run":
        for d in chosen:
            if dl.paused:
                break
            try:
                results.append(migrate_one(src, dst, d, args, dl))
            except RuntimeError as e:
                log(f"   ✗ {d['name']}：{e}")
                results.append({"name": d["name"], "src_id": d["id"], "uploaded": 0, "skipped": 0, "notes": [],
                                "failed": [{"file": "（dataset）", "error": str(e)}],
                                "parsed": {"done": 0, "failed": 0, "canceled": 0, "timeout": 0}})
    compare = [compare_one(src, dst, d) for d in chosen]
    path = write_report(out, src, dst, args, results, compare, paused=dl.paused)
    bad = [c["name"] for c in compare if not row_ok(c)]
    failed = sum(len(r["failed"]) for r in results)
    log(f"報告：{path}")
    log(f"比對：{len(compare) - len(bad)}/{len(compare)} 個 dataset 一致" + (f"；不一致：{', '.join(bad)}" if bad else ""))
    if args.action == "run" and failed:
        log(f"有 {failed} 份文件失敗，見報告；修好後再跑一次 run 會接著做")
    if dl.paused:
        log("⏸ 離峰時段結束，暫停；下次 run（或排程）會接著做")
        return 3
    return 1 if bad or failed else 0


if __name__ == "__main__":
    sys.exit(main())
