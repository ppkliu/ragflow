"""migrate-datasets.py 的端到端測試：兩台假 RAGFlow（0.26 方言的來源、1.0 方言的目標）。

  python3 agentwiki/tests/test_migrate_datasets.py

假伺服器照兩版各自的契約回應：1.0 建 dataset 只收白名單欄位（多一個就 code 101）、改文件用 PATCH、
狀態欄位是 ingestion_status；0.x 用 chunk_method、PUT、run。
"""
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("migrate", os.path.join(HERE, "..", "migrate-datasets.py"))
migrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate)

V1_CREATE_FIELDS = {"name", "embedding_model", "parser_config", "language", "permission", "parser_id",
                    "pipeline_id", "pipeline_version_id", "parse_type"}


class Fake:
    """一台假 RAGFlow 的狀態。"""

    def __init__(self, dialect, version):
        self.dialect, self.version = dialect, version
        self.datasets, self.docs, self.files, self.calls = {}, {}, {}, []
        self.n = 0
        self.reject_parser_key = None   # 1.0：parser_config 有這個鍵就拒絕
        self.fail_names = set()         # 解析會失敗的檔名

    def nid(self):
        self.n += 1
        return f"{self.dialect}{self.n:04d}"

    def add_dataset(self, name, **kw):
        i = self.nid()
        self.datasets[i] = {"id": i, "name": name, **kw}
        self.docs[i] = []
        return i

    def add_doc(self, ds, name, content, state, chunks=0, **kw):
        i = self.nid()
        key = "ingestion_status" if self.dialect == "v1" else "run"
        self.docs[ds].append({"id": i, "name": name, "size": len(content), key: state, "chunk_count": chunks,
                              "progress": 1 if state in ("DONE", "COMPLETED") else 0, **kw})
        self.files[i] = content
        return i

    def doc(self, i):
        for docs in self.docs.values():
            for d in docs:
                if d["id"] == i:
                    return d
        return None


def make_handler(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, payload, status=200, raw=None, ctype="application/json"):
            body = raw if raw is not None else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def handle_any(self, method):
            u = urlparse(self.path)
            path, q = u.path, parse_qs(u.query)
            fake.calls.append((method, path))
            if path == "/api/v1/system/version":
                return self.reply({"code": 0, "data": {"version": fake.version}} if fake.dialect == "v1"
                                  else {"code": 100}, 200 if fake.dialect == "v1" else 404)
            if path == "/v1/system/version":
                return self.reply({"code": 0, "data": fake.version} if fake.dialect == "v0" else {}, 200
                                  if fake.dialect == "v0" else 404)
            if self.headers.get("Authorization") != "Bearer k":
                return self.reply({"code": 109, "message": "auth"})
            if path == "/api/v1/datasets" and method == "GET":
                page, size = int(q.get("page", ["1"])[0]), int(q.get("page_size", ["30"])[0])
                rows = list(fake.datasets.values())
                return self.reply({"code": 0, "data": rows[(page - 1) * size: page * size]})
            if path == "/api/v1/datasets" and method == "POST":
                b = json.loads(self.body())
                if fake.dialect == "v1":
                    extra = set(b) - V1_CREATE_FIELDS
                    if extra:
                        return self.reply({"code": 101, "message": f"extra inputs are not permitted: {extra}"})
                    if fake.reject_parser_key and fake.reject_parser_key in (b.get("parser_config") or {}):
                        return self.reply({"code": 101, "message": "parser_config invalid"})
                    method_key = b.get("parser_id")
                else:
                    method_key = b.get("chunk_method")
                i = fake.add_dataset(b["name"], chunk_method=method_key, parser_config=b.get("parser_config", {}),
                                     **{k: b[k] for k in ("language", "permission", "embedding_model") if k in b})
                return self.reply({"code": 0, "data": fake.datasets[i]})
            m = re.fullmatch(r"/api/v1/datasets/([^/]+)", path)
            if m and method == "PUT":
                fake.datasets[m.group(1)].update(json.loads(self.body()))
                return self.reply({"code": 0})
            m = re.fullmatch(r"/api/v1/datasets/([^/]+)/documents", path)
            if m and method == "GET":
                page, size = int(q.get("page", ["1"])[0]), int(q.get("page_size", ["30"])[0])
                rows = fake.docs[m.group(1)][(page - 1) * size: page * size]
                return self.reply({"code": 0, "data": {"docs": rows, "total": len(fake.docs[m.group(1)])}})
            if m and method == "POST":
                raw = self.body()
                name = re.search(rb'filename="([^"]+)"', raw).group(1).decode()
                content = raw.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                state = "UNSTART"
                i = fake.add_doc(m.group(1), name, content, state)
                return self.reply({"code": 0, "data": [{"id": i, "name": name}]})
            m = re.fullmatch(r"/api/v1/datasets/([^/]+)/documents/([^/]+)", path)
            if m and method == "GET":
                if m.group(2) not in fake.files:
                    return self.reply({"code": 102, "message": "no such doc"})
                return self.reply(None, raw=fake.files[m.group(2)], ctype="application/octet-stream")
            if m and method in ("PUT", "PATCH"):
                if method != ("PATCH" if fake.dialect == "v1" else "PUT"):
                    return self.reply({"code": 100, "message": "method"}, 405)
                fake.doc(m.group(2)).update(json.loads(self.body()))
                return self.reply({"code": 0})
            m = re.fullmatch(r"/api/v1/datasets/([^/]+)/chunks", path)
            if m and method == "POST":
                for i in json.loads(self.body())["document_ids"]:
                    d = fake.doc(i)
                    bad = d["name"] in fake.fail_names
                    key = "ingestion_status" if fake.dialect == "v1" else "run"
                    d[key] = ("FAILED" if bad else "COMPLETED") if fake.dialect == "v1" else ("FAIL" if bad else "DONE")
                    d["chunk_count"] = 0 if bad else max(1, d["size"] // 4)
                    d["progress"] = 1
                    if bad:
                        d["progress_msg"] = "OCR error"
                return self.reply({"code": 0})
            return self.reply({"code": 100, "message": f"no route {method} {path}"}, 404)

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_PATCH(self):
            self.handle_any("PATCH")

    return H


def serve(fake):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = Fake("v0", "v0.26.4"), Fake("v1", "v1.0.0")
        self.s1, self.src_url = serve(self.src)
        self.s2, self.dst_url = serve(self.dst)
        a = self.src_id = self.src.add_dataset("agent_manuals", chunk_method="manual", parser_config={"chunk_token_num": 256},
                                 language="Chinese", permission="team", description="手冊", embedding_model="old@X")
        self.src.add_doc(a, "a.pdf", b"A" * 40, "DONE", chunks=7)
        self.src.add_doc(a, "b.docx", b"B" * 80, "DONE", chunks=9, chunk_method="table",
                         parser_config={"x": 1}, meta_fields={"owner": "ops"})
        self.src.add_doc(a, "bad.pdf", b"C" * 20, "DONE", chunks=2)
        self.src.add_dataset("space_eng", chunk_method="naive")          # kb-stack：預設略過
        self.src.add_dataset("kbbench_v024_x", chunk_method="naive")     # 壓測暫用：略過
        self.dst.fail_names = {"bad.pdf"}
        self.out = tempfile.mkdtemp()

    def tearDown(self):
        for srv in (self.s1, self.s2):
            srv.shutdown()
            srv.server_close()
        shutil.rmtree(self.out, ignore_errors=True)

    def report(self):
        with open(os.path.join(self.out, "report.md"), encoding="utf-8") as f:
            return f.read()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = migrate.main([argv[0], "--src", self.src_url, "--src-key", "k", "--dst", self.dst_url,
                                 "--dst-key", "k", "--out", self.out, "--batch", "2", *argv[1:]])
        return code, buf.getvalue()

    def test_plan_writes_nothing_and_skips_kb_and_temp(self):
        code, out = self.run_cli("plan")
        self.assertEqual(code, 0)
        self.assertIn("略過：kb-stack", out)
        self.assertIn("略過：暫用", out)
        self.assertIn("要上傳 3 份", out)
        self.assertEqual(self.dst.datasets, {})
        self.assertFalse(any(m in ("POST", "PUT", "PATCH") for m, _ in self.dst.calls))

    def test_run_requires_explicit_scope(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                open(os.devnull, "w") as dn, redirect_stderr(dn):
            migrate.main(["run", "--src", self.src_url, "--src-key", "k", "--dst", self.dst_url, "--dst-key", "k"])

    def test_run_migrates_settings_files_and_reports(self):
        migrate.wait_parsed.__defaults__ = (0,)  # 不必真的等輪詢間隔
        code, out = self.run_cli("run", "--all")
        self.assertEqual(code, 1, "bad.pdf 在 1.0 解析失敗，結束代碼要是 1")
        [ds] = [d for d in self.dst.datasets.values()]
        self.assertEqual(ds["name"], "agent_manuals")
        self.assertEqual(ds["chunk_method"], "manual")
        self.assertEqual(ds["parser_config"], {"chunk_token_num": 256})
        self.assertEqual(ds["language"], "Chinese")
        self.assertEqual(ds["description"], "手冊")
        self.assertNotIn("embedding_model", ds, "舊版的 embedding 名稱不沿用")
        docs = {d["name"]: d for d in self.dst.docs[ds["id"]]}
        self.assertEqual(set(docs), {"a.pdf", "b.docx", "bad.pdf"})
        self.assertEqual(self.dst.files[docs["a.pdf"]["id"]], b"A" * 40, "檔案內容要原樣搬過去")
        self.assertEqual(docs["b.docx"]["chunk_method"], "table", "文件自己的 chunk 方法用 PATCH 帶過去")
        self.assertEqual(docs["b.docx"]["meta_fields"], {"owner": "ops"})
        report = self.report()
        self.assertIn("| agent_manuals | 3 | 0 | 2 | 1 | 0 | 0 |", report)
        self.assertIn("舊版解析成功、新版沒有：bad.pdf", report)
        self.assertIn("OCR error", report)
        self.assertIn(f"| agent_manuals | `{self.src_id}` | `{ds['id']}` |", report)
        with open(os.path.join(self.out, "id-map.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"agent_manuals": {"old": self.src_id, "new": ds["id"]}})

        # 重跑：不重複上傳，只比對
        uploads = sum(1 for m, p in self.dst.calls if m == "POST" and p.endswith("/documents"))
        self.dst.fail_names = set()
        code2, _ = self.run_cli("run", "--all")
        self.assertEqual(sum(1 for m, p in self.dst.calls if m == "POST" and p.endswith("/documents")), uploads)
        self.assertEqual(len(self.dst.docs[ds["id"]]), 3)
        self.assertEqual(len(self.dst.datasets), 1)
        self.assertEqual(code2, 1, "bad.pdf 已經解析失敗過，重跑不會自動重排（要在 1.0 介面重新解析）")

    def test_parser_config_rejected_falls_back_to_default(self):
        migrate.wait_parsed.__defaults__ = (0,)
        self.dst.reject_parser_key = "chunk_token_num"
        self.dst.fail_names = set()
        code, _ = self.run_cli("run", "--dataset", "agent_manuals")
        self.assertEqual(code, 0)
        [ds] = list(self.dst.datasets.values())
        self.assertEqual(ds["parser_config"], {})
        self.assertIn("parser_config 不相容", self.report())

    def test_named_dataset_overrides_skip_rules_and_missing_name_errors(self):
        rows = migrate.select([{"name": "space_eng", "id": "1"}], ["space_eng"], False)
        self.assertEqual(rows[0][1], None)
        with self.assertRaises(SystemExit):
            migrate.select([{"name": "x", "id": "1"}], ["nope"], False)

    def test_verify_lists_missing_without_writing(self):
        code, _ = self.run_cli("verify", "--all")
        self.assertEqual(code, 1)
        self.assertFalse(any(m in ("POST", "PUT", "PATCH") for m, _ in self.dst.calls))
        self.assertIn("目標缺 3 份", self.report())

    def test_unstarted_states(self):
        self.assertTrue(migrate.unstarted({"run": "UNSTART", "progress": 0}))
        self.assertTrue(migrate.unstarted({"ingestion_status": "CREATED"}))
        self.assertFalse(migrate.unstarted({"run": "RUNNING", "progress": 0.3}))
        self.assertFalse(migrate.unstarted({"ingestion_status": "COMPLETED", "progress": 1}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
