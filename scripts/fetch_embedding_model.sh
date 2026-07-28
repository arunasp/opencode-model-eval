#!/bin/sh
# Fetches the all-MiniLM-L6-v2 ONNX model + tokenizer from a GitHub repo
# with the weights committed directly in-repo. Verified working source
# earlier this session -- no Hugging Face / Ollama network dependency,
# which matters inside an isolated container that may not have that
# egress allowed.
#
# POSIX sh, not bash: this image's apk install list never included
# bash (only jq/ca-certificates/python3/py3-pip/git), so a #!/bin/bash
# shebang failed with a confusing "not found" error at COPY+RUN time --
# the shell was failing to find the INTERPRETER, not the script file.
# `set -o pipefail` (the one bash-only bit) is dropped rather than
# worked around: there are no pipes anywhere in this script, so it was
# never doing anything here.
set -eu

DEST="${1:?usage: fetch_embedding_model.sh <dest-dir>}"
mkdir -p "$DEST"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth 1 https://github.com/clems4ever/all-minilm-l6-v2-go.git "$TMP/repo"

cp "$TMP/repo/all_minilm_l6_v2/model.onnx"     "$DEST/model.onnx"
cp "$TMP/repo/all_minilm_l6_v2/tokenizer.json" "$DEST/tokenizer.json"

echo "Embedding model installed at $DEST"
ls -lh "$DEST"
