#!/bin/bash
# Fetches the all-MiniLM-L6-v2 ONNX model + tokenizer from a GitHub repo
# with the weights committed directly in-repo. Verified working source
# earlier this session -- no Hugging Face / Ollama network dependency,
# which matters inside an isolated container that may not have that
# egress allowed.
set -euo pipefail

DEST="${1:?usage: fetch_embedding_model.sh <dest-dir>}"
mkdir -p "$DEST"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --depth 1 https://github.com/clems4ever/all-minilm-l6-v2-go.git "$TMP/repo"

cp "$TMP/repo/all_minilm_l6_v2/model.onnx"     "$DEST/model.onnx"
cp "$TMP/repo/all_minilm_l6_v2/tokenizer.json" "$DEST/tokenizer.json"

echo "Embedding model installed at $DEST"
ls -lh "$DEST"
