# 衍生的 build runner：官方 runner + Go 編譯需要的 native library。
#
# 官方 Dockerfile 的 go-builder 階段 FROM infiniflow/github_action_runner:latest，並預期
# office_oxide / pdfium-static / pdf_oxide 與對應版本的 onnxruntime 已在 /opt/ragflow-native-libs/。
# :latest 不釘版本，v1.0.0 發佈後重建的 runner 沒有這三個 library、onnxruntime 也是舊版
# （1.23.2，v1.0.0 要 1.29.0），go build 會失敗。pack-prod.sh 先 build 這個
# image，再用 BuildKit 的 --build-context 讓官方 Dockerfile 的 FROM 改指向它，不必修改上游檔案。
#
# build context 是 repo 根目錄（需要 go.mod 與官方的 tools/scripts/download_dev_deps.py）。
ARG RUNNER_IMAGE=infiniflow/github_action_runner:latest
FROM ${RUNNER_IMAGE}
USER root
COPY go.mod /seed/go.mod
COPY tools/scripts/download_dev_deps.py /seed/tools/scripts/download_dev_deps.py
COPY agentwiki/seed_native_libs.py /seed/agentwiki/seed_native_libs.py
# GH_PROXY：官方腳本支援的 GitHub 下載鏡像（空字串 = 直連 github.com）
ARG GH_PROXY=
RUN GH_PROXY="${GH_PROXY}" uv run --no-project --with requests \
      python /seed/agentwiki/seed_native_libs.py /seed /opt/ragflow-native-libs \
 && rm -rf /seed \
 && ls /opt/ragflow-native-libs
