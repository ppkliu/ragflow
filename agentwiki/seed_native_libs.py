#!/usr/bin/env python3
"""把 Go 編譯要靜態連結的 native library 預先放進 runner image。

為什麼需要：官方 Dockerfile 的 go-builder 階段是 `FROM infiniflow/github_action_runner:latest`，
build.sh 預期 office_oxide / pdfium-static / pdf_oxide 已經預先放在 runner 的
/opt/ragflow-native-libs/。但 runner 用的是不釘版本的 :latest，v1.0.0 發佈之後重建的
runner 只剩 onnxruntime，而且是 1.23.2（v1.0.0 要 1.29.0），於是 `./build.sh --go` 直接失敗
（office_oxide native library not found / no ONNX Runtime 1.29.0 static lib）。

做法：直接 import 官方的 tools/scripts/download_dev_deps.py，用它的 URL、版本（office_oxide
的版本讀自 go.mod）、下載驗證與解壓函式，處理這三個 library 與 onnxruntime，輸出到指定目錄。
不重寫任何版本號或 URL，換官方版本時自動跟著走。

用法：python seed_native_libs.py <repo_root> <output_dir>
      （repo_root 需含 go.mod 與 tools/scripts/download_dev_deps.py）
"""
import importlib.util
import os
import sys


def main() -> int:
    repo_root, out_dir = (os.path.abspath(p) for p in sys.argv[1:3])
    script = os.path.join(repo_root, "tools", "scripts", "download_dev_deps.py")
    spec = importlib.util.spec_from_file_location("download_dev_deps", script)
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)

    goos, goarch = d.host_platform(os.environ.get("RAGFLOW_TARGET_OS"), os.environ.get("RAGFLOW_TARGET_ARCH"))
    print(f"Target platform: {goos}/{goarch}")
    os.makedirs(d.RAGFLOW_DEPS_DIR, exist_ok=True)
    os.chdir(d.RAGFLOW_DEPS_DIR)

    office_version = d.go_module_version("github.com/yfedoseev/office_oxide/go", d.REPO_ROOT)
    libs = [
        (d.pdfium_url(goos, goarch), d.PDFIUM_STATIC_ASSETS[(goos, goarch)], "pdfium-static",
         ["lib/libpdfium.a", "lib/libc++.a", "lib/libc++abi.a", "include/fpdfview.h"], None),
        (d.pdf_oxide_url(goos, goarch), d.PDF_OXIDE_ASSETS[(goos, goarch)], "pdf_oxide",
         [f"lib/{goos}_{goarch}/libpdf_oxide.a", "include/pdf_oxide.h"], None),
        (d.office_oxide_url(d.REPO_ROOT, goos, goarch), d.OFFICE_OXIDE_ASSETS[(goos, goarch)], "office_oxide",
         ["lib/liboffice_oxide.a", "include/office_oxide_c/office_oxide.h"], office_version),
    ]
    for url, filename, subdir, required, version in libs:
        print(f"Downloading {filename} from {url}")
        if not d._valid_download(filename):
            d.download_with_progress(url, filename)
            if not d._valid_download(filename):
                raise RuntimeError(f"Invalid downloaded dependency: {filename}")
        d._extract_native_archive(os.path.join(d.RAGFLOW_DEPS_DIR, filename),
                                  os.path.join(out_dir, subdir), required, version)
    # ONNX Runtime：runner 內建的版本也可能跟不上（2026-11-09 的 runner 是 1.23.2，v1.0.0 要
    # 1.29.0 patch2）。用官方腳本的下載（含 .sha256 sidecar 驗證）與解壓；解壓時會清掉
    # static_lib 底下其他版本，避免 build.sh 同時連結兩個版本。
    d.NATIVE_LIBS_DIR = out_dir    # _download_ort_archive 重新下載時會清這底下的舊解壓目錄
    ort_zip = d.ort_asset_name(d.ORT_VERSION, d.ort_target(goos, goarch))
    ort_dir = d.ort_normalized_dir(d.ORT_VERSION, goos, goarch)
    archive = os.path.join(d.RAGFLOW_DEPS_DIR, ort_zip)
    d._download_ort_archive(archive, goos, goarch)
    if not d.extract_onnxruntime(os.path.join(out_dir, "onnxruntime", "static_lib"), archive, ort_dir):
        raise RuntimeError(f"ONNX Runtime {d.ORT_VERSION} static archives were not extracted")

    print(f"✓ native libraries seeded under {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
