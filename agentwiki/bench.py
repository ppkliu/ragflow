#!/usr/bin/env python3
"""RAGFlow 效能比較：上傳 → 解析 → 計時 → 記錄資源用量，輸出比較報表。

同一批檔案依序丟給每個 target（一次只跑一個，資源數字才不會互相干擾），每個 target：
  1) 建一個暫用 dataset（kbbench_<名稱>_<時間>）
  2) 上傳全部檔案，一次觸發解析，輪詢到全部完成 / 失敗 / 逾時
  3) 期間每隔幾秒用 docker stats 記錄該 compose project 每個容器的 CPU 與記憶體
  4) 有給 --question 時量檢索延遲
  5) 刪掉暫用 dataset（--keep 保留）

0.x（Python 後端）與 1.0（Go 後端）的 API 差異沿用 kb-stack 連接器（src/adapters/ragflow.mjs）
已驗證的處理方式：建 dataset 的欄位、文件狀態欄位（run / ingestion_status）。

用法（在主機上執行，需要能跑 docker 指令；只用 Python 標準函式庫）：
  agentwiki/bench.py --files <測試檔目錄> \\
      --target v100 http://127.0.0.1:19383 <1.0 的 API key> ragflow-v100 \\
      --target v024 http://127.0.0.1:9380  <0.24 的 API key> ragflow_docker \\
      [--question "某個問題" ...] [--out <輸出目錄>] [--keep]

  --target 名稱 base_url API_key compose_project [embedding 模型]
      compose project 用來找要記錄的容器（docker ps 的 com.docker.compose.project 標籤）。
      embedding 不給 = 用該帳號的預設 embedding 模型。
  1.0 的 key：/opt/AgentWiki/shared/env/ragflow.env 的 RAGFLOW_KB_API_KEY。
  0.x 的 key：在它的 Web → 個人設定 → API 建一把。
  API_key 寫成 env:變數名 時從環境變數讀（key 不會留在 ps 與 shell history）。

輸出（預設 ~/ragflow-bench/<時間>/）：report.md（比較報表）、result.json（原始結果）、
samples.csv（資源取樣，可以丟進試算表畫圖）。
"""
import argparse
import csv
import json
import mimetypes
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

SUFFIXES = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md",
            ".csv", ".html", ".htm", ".json", ".png", ".jpg", ".jpeg"}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- HTTP

def http(method, url, key=None, body=None, raw=None, content_type=None, timeout=120):
    """回傳 (HTTP 狀態, 解析過的 JSON 或 None)。不丟 HTTPError，由呼叫端看狀態。"""
    data, headers = None, {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read()
    try:
        return status, json.loads(text or b"{}")
    except ValueError:
        return status, None


def multipart(field, filename, content):
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n').encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


class Api:
    def __init__(self, base, key):
        self.base, self.key = base.rstrip("/"), key

    def call(self, method, path, body=None, **kw):
        status, payload = http(method, self.base + path, self.key, body=body, **kw)
        return status, payload or {}

    def ok(self, method, path, body=None, what="", **kw):
        status, p = self.call(method, path, body, **kw)
        if status >= 400 or p.get("code") != 0:
            raise RuntimeError(f"{what or path} 失敗：HTTP {status} code={p.get('code')} {p.get('message')}")
        return p.get("data")


# ---------------------------------------------------------------- 0.x / 1.0 差異

def detect(api):
    """回傳 (方言 v0|v1, 版本字串)。1.0 的版本端點不用金鑰，0.x 的在 /v1 底下、要金鑰。"""
    for path, key in (("/api/v1/system/version", None), ("/v1/system/version", api.key)):
        status, p = http("GET", api.base + path, key)
        if status == 200 and p and p.get("code") == 0:
            d = p.get("data")
            ver = d.get("version") if isinstance(d, dict) else d
            if ver:
                major = str(ver).lstrip("vV").split(".")[0]
                if major.isdigit():
                    return ("v1" if int(major) >= 1 else "v0"), str(ver)
                if path.startswith("/api"):
                    return "v1", str(ver)
    return "v0", "unknown"


def dataset_body(dialect, name, chunk_method, embedding):
    body = ({"name": name, "parse_type": 1, "parser_id": chunk_method} if dialect == "v1"
            else {"name": name, "chunk_method": chunk_method})
    if embedding:
        body["embedding_model"] = embedding
    return body


def doc_state(doc):
    """0.x 用 run（文字或 0–5），1.0 用 ingestion_status。"""
    s = str(doc.get("ingestion_status", doc.get("run", ""))).strip().upper()
    if s in ("DONE", "3", "COMPLETED"):
        return "done"
    if s in ("FAIL", "4", "FAILED"):
        return "failed"
    if s in ("CANCEL", "2", "STOPPED"):
        return "canceled"
    return "pending"


# ---------------------------------------------------------------- 資源取樣

def parse_bytes(s):
    s = s.strip()
    units = [("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024),
             ("TB", 1000**4), ("GB", 1000**3), ("MB", 1000**2), ("kB", 1000), ("B", 1)]
    for u, m in units:
        if s.endswith(u):
            try:
                return float(s[: -len(u)]) * m
            except ValueError:
                return 0.0
    return 0.0


class Sampler(threading.Thread):
    """每 interval 秒對一個 compose project 的容器跑一次 docker stats --no-stream。"""

    def __init__(self, project, interval):
        super().__init__(daemon=True)
        self.project, self.interval = project, interval
        self.samples, self.phase, self.t0 = [], "baseline", time.monotonic()
        self.stop_evt, self.error = threading.Event(), None

    def containers(self):
        out = subprocess.run(["docker", "ps", "-q", "--filter",
                              f"label=com.docker.compose.project={self.project}"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or "docker ps 失敗")
        return out.stdout.split()

    def run(self):
        try:
            ids = self.containers()
            if not ids:
                raise RuntimeError(f"compose project「{self.project}」沒有在跑的容器")
            while not self.stop_evt.is_set():
                out = subprocess.run(["docker", "stats", "--no-stream", "--format",
                                      "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *ids],
                                     capture_output=True, text=True, timeout=60)
                t = round(time.monotonic() - self.t0, 1)
                for line in out.stdout.splitlines():
                    parts = line.split("\t")
                    if len(parts) != 3:
                        continue
                    name, cpu, mem = parts
                    try:
                        cpu_v = float(cpu.strip().rstrip("%") or 0)
                    except ValueError:
                        cpu_v = 0.0
                    self.samples.append({"t": t, "phase": self.phase, "container": name,
                                         "cpu": cpu_v, "mem": parse_bytes(mem.split("/")[0])})
                self.stop_evt.wait(self.interval)
        except Exception as e:  # 取樣失敗不影響計時，只在報表註記
            self.error = str(e)


# ---------------------------------------------------------------- 一個 target

def run_target(t, files, args, stamp):
    api = Api(t["url"], t["key"])
    res = {"name": t["name"], "url": t["url"], "project": t["project"], "files": [],
           "errors": [], "retrieval": []}
    res["dialect"], res["version"] = detect(api)
    log(f"== {t['name']}：{t['url']}（{res['version']}，{res['dialect']} API）")

    sampler = None
    if args.no_stats or not shutil.which("docker"):
        res["stats_error"] = "未記錄（--no-stats 或沒有 docker 指令）"
    else:
        sampler = Sampler(t["project"], args.interval)
        sampler.start()
        time.sleep(min(args.interval * 2, 10))  # 先記幾筆閒置基準

    ds_id = None
    try:
        # 1) dataset：方言偵測錯時換另一種欄位重送一次（同 kb-stack）
        name = f"kbbench_{t['name']}_{stamp}"
        dialect = res["dialect"]
        st, p = api.call("POST", "/api/v1/datasets", dataset_body(dialect, name, args.chunk_method, t["embedding"]))
        if st >= 400 or p.get("code") != 0:
            other = "v0" if dialect == "v1" else "v1"
            st2, p2 = api.call("POST", "/api/v1/datasets", dataset_body(other, name, args.chunk_method, t["embedding"]))
            if st2 >= 400 or p2.get("code") != 0:
                raise RuntimeError(f"建立 dataset 失敗：{p.get('message')} / {p2.get('message')}")
            p, res["dialect"] = p2, other
        ds_id = p["data"]["id"]
        res["dataset"] = {"name": name, "id": ds_id}
        log(f"   dataset {name}（{ds_id}）")

        # 2) 上傳
        if sampler:
            sampler.phase = "upload"
        t_up = time.monotonic()
        docs = {}
        for f in files:
            with open(f, "rb") as fh:
                raw, ctype = multipart("file", os.path.basename(f), fh.read())
            t1 = time.monotonic()
            st, p = api.call("POST", f"/api/v1/datasets/{ds_id}/documents", raw=raw, content_type=ctype, timeout=600)
            data = p.get("data") if isinstance(p.get("data"), list) else []
            doc_id = data[0].get("id") if data else None
            item = {"file": os.path.basename(f), "bytes": os.path.getsize(f),
                    "upload_s": round(time.monotonic() - t1, 2), "id": doc_id}
            if not doc_id:
                item["state"] = "upload_failed"
                item["message"] = f"HTTP {st} {p.get('message')}"
            else:
                docs[doc_id] = item
            res["files"].append(item)
        res["upload_s"] = round(time.monotonic() - t_up, 2)
        log(f"   上傳 {len(docs)}/{len(files)} 份，{res['upload_s']}s")
        if not docs:
            raise RuntimeError("沒有任何檔案上傳成功")

        # 3) 解析
        if sampler:
            sampler.phase = "parse"
        t_parse = time.monotonic()
        st, p = api.call("POST", f"/api/v1/datasets/{ds_id}/chunks", {"document_ids": list(docs)})
        if st == 404:  # 更早期 0.x 只有逐檔端點
            for d in docs:
                api.ok("POST", f"/api/v1/datasets/{ds_id}/documents/{d}/parse", {}, what="觸發解析")
        elif st >= 400 or p.get("code") != 0:
            raise RuntimeError(f"觸發解析失敗：HTTP {st} {p.get('message')}")

        deadline = t_parse + args.timeout
        last_report = time.monotonic()
        while True:
            pending = 0
            page = 1
            while True:
                data = api.ok("GET", f"/api/v1/datasets/{ds_id}/documents?page={page}&page_size=100",
                              what="查詢文件狀態")
                rows = data if isinstance(data, list) else (data or {}).get("docs", [])
                for d in rows:
                    item = docs.get(d.get("id"))
                    if not item or item.get("state") in ("done", "failed", "canceled"):
                        continue
                    state = doc_state(d)
                    if state == "pending":
                        pending += 1
                        item["progress"] = d.get("progress")
                        continue
                    item.update({"state": state, "done_at_s": round(time.monotonic() - t_parse, 1),
                                 "process_duration_s": (float(d["process_duration"])
                                                        if isinstance(d.get("process_duration"), (int, float))
                                                        else None),
                                 "chunks": d.get("chunk_count"), "tokens": d.get("token_count")})
                    if state != "done":
                        item["message"] = str(d.get("progress_msg") or "")[-300:]
                    log(f"   {state:8} {item['file']}（{item['done_at_s']}s，{item.get('chunks')} chunks）")
                if len(rows) < 100:
                    break
                page += 1
            unseen = sum(1 for i in docs.values() if "state" not in i)
            if unseen == 0:
                break
            if time.monotonic() > deadline:
                for i in docs.values():
                    i.setdefault("state", "timeout")
                res["errors"].append(f"解析逾時（{args.timeout}s），{unseen} 份未完成")
                break
            if time.monotonic() - last_report > 60:
                log(f"   等待中：{unseen} 份未完成（已 {int(time.monotonic() - t_parse)}s）")
                last_report = time.monotonic()
            time.sleep(args.poll)
        res["parse_wall_s"] = round(time.monotonic() - t_parse, 1)
        log(f"   解析結束，{res['parse_wall_s']}s")

        # 4) 檢索延遲
        if args.question:
            if sampler:
                sampler.phase = "retrieval"
            for q in args.question:
                for _ in range(args.retrieval_runs):
                    t1 = time.monotonic()
                    st, p = api.call("POST", "/api/v1/retrieval", {"question": q, "dataset_ids": [ds_id]})
                    ms = round((time.monotonic() - t1) * 1000)
                    data = p.get("data") or {}
                    hits = len(data.get("chunks", [])) if isinstance(data, dict) else None
                    res["retrieval"].append({"question": q, "ms": ms, "ok": st < 400 and p.get("code") == 0,
                                             "hits": hits, "message": p.get("message")})

        # 5) 冷卻：看解析完記憶體有沒有降回來
        if sampler and args.cooldown > 0:
            sampler.phase = "cooldown"
            log(f"   冷卻 {args.cooldown}s（記錄解析後的資源）")
            time.sleep(args.cooldown)
    except Exception as e:
        res["errors"].append(str(e))
        log(f"   ✗ {e}")
    finally:
        if sampler:
            sampler.stop_evt.set()
            sampler.join(timeout=90)
            res["samples"] = sampler.samples
            if sampler.error:
                res["stats_error"] = sampler.error
        if ds_id and not args.keep:
            st, p = api.call("DELETE", "/api/v1/datasets", {"ids": [ds_id]})
            if st >= 400 or p.get("code") != 0:
                res["errors"].append(f"刪除暫用 dataset 失敗：{p.get('message')}（請手動刪 {ds_id}）")
    return res


# ---------------------------------------------------------------- 報表

def pct(values, q):
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def fmt(v, unit="", nd=1):
    if v is None:
        return "—"
    return f"{v:,.{nd}f}{unit}" if isinstance(v, float) else f"{v:,}{unit}"


def gib(b):
    return None if b is None else b / 1024**3


def summarize(r):
    files = r["files"]
    done = [f for f in files if f.get("state") == "done"]
    mb = sum(f["bytes"] for f in files) / 1024**2
    wall = r.get("parse_wall_s")
    dur = [float(f["process_duration_s"]) for f in done
           if isinstance(f.get("process_duration_s"), (int, float))]
    ret = [x["ms"] for x in r["retrieval"] if x["ok"]]
    s = {
        "ok": f"{len(done)} / {len(files)}",
        "upload": r.get("upload_s"), "wall": wall,
        "docs_per_min": (len(done) / wall * 60) if wall else None,
        "mb_per_min": (sum(f["bytes"] for f in done) / 1024**2 / wall * 60) if wall else None,
        "dur_avg": statistics.mean(dur) if dur else None, "dur_p50": pct(dur, .5),
        "dur_p95": pct(dur, .95), "dur_max": max(dur) if dur else None,
        "chunks": sum(f.get("chunks") or 0 for f in done), "tokens": sum(f.get("tokens") or 0 for f in done),
        "ret_avg": statistics.mean(ret) if ret else None, "ret_p95": pct(ret, .95),
        "mb": mb,
    }
    # 資源：每個容器的基準 / 解析期間尖峰 / 冷卻結束
    per = {}
    for x in r.get("samples", []):
        per.setdefault(x["container"], []).append(x)
    rows = []
    for c, xs in sorted(per.items()):
        base = [x["mem"] for x in xs if x["phase"] == "baseline"]
        busy = [x for x in xs if x["phase"] in ("upload", "parse", "retrieval")]
        cool = [x["mem"] for x in xs if x["phase"] == "cooldown"]
        rows.append({
            "container": c,
            "mem_base": gib(statistics.mean(base)) if base else None,
            "mem_peak": gib(max(x["mem"] for x in busy)) if busy else None,
            "mem_end": gib(cool[-1]) if cool else None,
            "cpu_avg": statistics.mean(x["cpu"] for x in busy) if busy else None,
            "cpu_peak": max(x["cpu"] for x in busy) if busy else None,
        })
    # 整個 project 的總記憶體尖峰（同一次取樣加總）
    by_t = {}
    for x in r.get("samples", []):
        if x["phase"] in ("upload", "parse", "retrieval"):
            by_t[x["t"]] = by_t.get(x["t"], 0) + x["mem"]
    s["mem_total_peak"] = gib(max(by_t.values())) if by_t else None
    s["containers"] = rows
    return s


def write_report(results, files, args, out_dir):
    sums = {r["name"]: summarize(r) for r in results}
    total_mb = sum(os.path.getsize(f) for f in files) / 1024**2
    kinds = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        kinds[ext] = kinds.get(ext, 0) + 1
    L = [f"# RAGFlow 效能比較（{datetime.now():%Y-%m-%d %H:%M}）", "",
         f"- 測試檔案：{len(files)} 份，共 {total_mb:,.1f} MB（"
         + "、".join(f"{k} {v}" for k, v in sorted(kinds.items())) + "）",
         f"- 解析方法：`{args.chunk_method}`；各 target 依序執行，一次只跑一個",
         f"- 資源取樣：每 {args.interval}s 一次 docker stats；尖峰取上傳到檢索之間，"
         f"「結束」是解析完冷卻 {args.cooldown}s 後", ""]
    names = [r["name"] for r in results]
    head = "| 項目 | " + " | ".join(names) + " |"
    sep = "|---|" + "---|" * len(names)
    rows = [
        ("版本", lambda r, s: f"{r['version']}（{r['dialect']}）"),
        ("解析成功", lambda r, s: s["ok"]),
        ("上傳耗時", lambda r, s: fmt(s["upload"], " s")),
        ("**全部解析完成（牆鐘）**", lambda r, s: "**" + fmt(s["wall"], " s") + "**"),
        ("吞吐量", lambda r, s: f"{fmt(s['docs_per_min'], ' 份/分')}，{fmt(s['mb_per_min'], ' MB/分')}"),
        ("單份解析時間（伺服器記錄）平均 / p50 / p95 / 最大",
         lambda r, s: " / ".join(fmt(s[k], " s") for k in ("dur_avg", "dur_p50", "dur_p95", "dur_max"))),
        ("chunks / tokens", lambda r, s: f"{fmt(s['chunks'])} / {fmt(s['tokens'])}"),
        ("檢索延遲 平均 / p95", lambda r, s: f"{fmt(s['ret_avg'], ' ms', 0)} / {fmt(s['ret_p95'], ' ms', 0)}"),
        ("**整組記憶體尖峰**", lambda r, s: "**" + fmt(s["mem_total_peak"], " GiB", 2) + "**"),
    ]
    L += ["## 總覽", "", head, sep]
    by_name = {r["name"]: r for r in results}
    for label, fn in rows:
        L.append(f"| {label} | " + " | ".join(fn(by_name[n], sums[n]) for n in names) + " |")
    L.append("")

    for r in results:
        s = sums[r["name"]]
        L += [f"## {r['name']}：各容器資源", ""]
        if r.get("stats_error"):
            L += [f"> 資源取樣：{r['stats_error']}", ""]
        if s["containers"]:
            L += ["| 容器 | 閒置記憶體 | 尖峰記憶體 | 結束記憶體 | 平均 CPU | 尖峰 CPU |", "|---|---|---|---|---|---|"]
            for c in s["containers"]:
                L.append(f"| {c['container']} | {fmt(c['mem_base'], ' GiB', 2)} | {fmt(c['mem_peak'], ' GiB', 2)}"
                         f" | {fmt(c['mem_end'], ' GiB', 2)} | {fmt(c['cpu_avg'], '%')} | {fmt(c['cpu_peak'], '%')} |")
            L.append("")
        L += [f"### {r['name']}：各檔案", "",
              "| 檔案 | 大小 | 狀態 | 完成時間點 | 伺服器解析時間 | chunks | 說明 |", "|---|---|---|---|---|---|---|"]
        for f in r["files"]:
            L.append(f"| {f['file']} | {f['bytes'] / 1024**2:,.2f} MB | {f.get('state', '—')} "
                     f"| {fmt(f.get('done_at_s'), ' s')} | {fmt(f.get('process_duration_s'), ' s')} "
                     f"| {fmt(f.get('chunks'))} | {str(f.get('message') or '').replace('|', '/')[:120]} |")
        L.append("")
        if r["errors"]:
            L += ["**錯誤：**", ""] + [f"- {e}" for e in r["errors"]] + [""]

    L += ["## 怎麼讀", "",
          "- **牆鐘時間**是從觸發解析到最後一份完成，含排隊；**伺服器解析時間**是 RAGFlow 記在每份文件上的"
          " `process_duration`。並行數不同的版本，牆鐘時間比較有意義。",
          "- 兩邊的解析器設定（0.x 的 DeepDoc、1.0 的 general 管線、並行 worker 數、embedding 模型）"
          "不同時，比較的是「整套預設行為」，不是單一元件。",
          "- 閒置記憶體裡的 Elasticsearch 主要是 JVM 啟動時預留的 heap，不代表資料量。",
          "- 另一套 RAGFlow 同時在跑時，它的閒置負載仍在主機上，數字會略受影響。", ""]
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return path


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--files", required=True, help="測試檔案的目錄（不遞迴）或單一檔案")
    ap.add_argument("--target", nargs="+", action="append", required=True,
                    metavar="ARG", help="名稱 base_url API_key compose_project [embedding]")
    ap.add_argument("--chunk-method", default="naive", help="解析方法（預設 naive，1.0 的 general 別名）")
    ap.add_argument("--question", action="append", help="量檢索延遲用的問題，可給多次")
    ap.add_argument("--retrieval-runs", type=int, default=5, help="每個問題問幾次（預設 5）")
    ap.add_argument("--interval", type=float, default=5, help="資源取樣間隔秒數（預設 5）")
    ap.add_argument("--poll", type=float, default=3, help="狀態輪詢間隔秒數（預設 3）")
    ap.add_argument("--timeout", type=int, default=3600, help="每個 target 的解析逾時秒數（預設 3600）")
    ap.add_argument("--cooldown", type=int, default=30, help="解析完再記錄幾秒資源（預設 30）")
    ap.add_argument("--out", help="輸出目錄（預設 ~/ragflow-bench/<時間>）")
    ap.add_argument("--keep", action="store_true", help="保留暫用 dataset（預設刪除）")
    ap.add_argument("--no-stats", action="store_true", help="不記錄 docker stats")
    args = ap.parse_args()

    targets = []
    for t in args.target:
        if len(t) not in (4, 5):
            ap.error(f"--target 要 4 或 5 個值（名稱 base_url API_key compose_project [embedding]）：{t}")
        key = t[2]
        if key.startswith("env:"):  # env:變數名 → 從環境變數讀，key 不會出現在 ps / shell history
            key = os.environ.get(key[4:], "")
            if not key:
                ap.error(f"環境變數 {t[2][4:]} 是空的")
        targets.append({"name": t[0], "url": t[1], "key": key, "project": t[3],
                        "embedding": t[4] if len(t) == 5 else None})

    if os.path.isdir(args.files):
        files = sorted(os.path.join(args.files, n) for n in os.listdir(args.files)
                       if os.path.splitext(n)[1].lower() in SUFFIXES
                       and os.path.isfile(os.path.join(args.files, n)))
    else:
        files = [args.files]
    if not files:
        sys.exit(f"{args.files} 裡沒有可上傳的檔案（支援 {' '.join(sorted(SUFFIXES))}）")

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    out_dir = args.out or os.path.join(os.path.expanduser("~"), "ragflow-bench", stamp)
    os.makedirs(out_dir, exist_ok=True)
    log(f"{len(files)} 份檔案、{len(targets)} 個 target，輸出到 {out_dir}")

    results = [run_target(t, files, args, stamp) for t in targets]

    with open(os.path.join(out_dir, "samples.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["target", "t_s", "phase", "container", "cpu_pct", "mem_mib"])
        for r in results:
            for x in r.get("samples", []):
                w.writerow([r["name"], x["t"], x["phase"], x["container"], x["cpu"], round(x["mem"] / 1024**2, 1)])
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump([{k: v for k, v in r.items() if k != "samples"} for r in results], fh,
                  ensure_ascii=False, indent=2)
    report = write_report(results, files, args, out_dir)
    log(f"完成：{report}")
    sys.exit(1 if any(r["errors"] for r in results) else 0)


if __name__ == "__main__":
    main()
