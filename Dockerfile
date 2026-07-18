# syntax=docker/dockerfile:1

# --- Stage: base -------------------------------------------------------
# The official opencode image, unmodified. Pin OPENCODE_REF to a digest
# (docker pull ghcr.io/anomalyco/opencode:<tag> then read the resolved
# sha256 with `docker inspect`) for true immutability. A mutable tag like
# "latest" defeats the point of this stage — it can change under you
# between builds.
#
# Verified 2026-07-14 against https://opencode.ai/docs/: the project was
# renamed from ghcr.io/sst/opencode (deprecated, returns 401) to
# ghcr.io/anomalyco/opencode. Re-verify this if the pull fails — the
# project has moved registries before.
ARG OPENCODE_IMAGE=ghcr.io/anomalyco/opencode
ARG OPENCODE_REF=latest

FROM ${OPENCODE_IMAGE}:${OPENCODE_REF} AS base

# --- Stage: harness ------------------------------------------------------
# Shared test-harness layer, identical across every model under test.
# Adds only: jq (for JSON result post-processing), the entrypoint script,
# and a base opencode.json template. No model identity here — that's the
# whole point of keeping this layer common and cacheable.
FROM base AS harness

USER root

# Base image distro isn't pinned by this Dockerfile (upstream ships both
# Debian Trixie and Alpine variants per anomalyco/opencode#19474) — detect
# the package manager rather than assume one.
#
# python3-pip and git added for the CVV scoring layer (cvv_scan.py,
# axiom_cvv_verify.py) and its optional negation-detection/semantic-
# fallback dependencies (spaCy + model, onnxruntime + tokenizers). This
# makes the harness layer meaningfully heavier than before -- disclosed
# here rather than left for someone to discover via a slow build.
RUN if command -v apk >/dev/null 2>&1; then \
      apk add --no-cache jq ca-certificates python3 py3-pip git; \
    elif command -v apt-get >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends jq ca-certificates python3 python3-pip git \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "FATAL: no supported package manager (apk/apt-get) found in base image" >&2; \
      exit 1; \
    fi

RUN pip install --break-system-packages --no-cache-dir spacy onnxruntime tokenizers numpy \
    && python3 -m spacy download en_core_web_sm

# Pin HOME explicitly rather than relying on whatever user/home the base
# image happens to ship with — removes the ambiguity of where
# ~/.local/share/opencode/auth.json and ~/.config/opencode resolve to,
# which otherwise depends on an unverified base-image convention.
ENV HOME=/home/harness
RUN mkdir -p "${HOME}/.local/share/opencode" "${HOME}/.config/opencode" /task-suite /results \
    && chmod -R 0777 "${HOME}" /results
# NOTE: 0777 on the harness home dir is a deliberate relaxation, not an
# oversight — this image may run under an arbitrary/overridden UID via
# `docker-compose run --user`, and this is a local evaluation tool, not a
# multi-tenant service. Tighten this if you run it anywhere less trusted.

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chmod=0755 scripts/discover_and_select_model.py /usr/local/bin/discover_and_select_model.py
COPY --chmod=0755 scripts/run_test_ladder.py /usr/local/bin/run_test_ladder.py
COPY --chmod=0644 scripts/tools/cvv_scan.py /opt/harness/tools/cvv_scan.py
COPY --chmod=0644 scripts/tools/axiom_cvv_verify.py /opt/harness/tools/axiom_cvv_verify.py
COPY --chmod=0644 config/opencode.base.json /opt/harness/opencode.base.json

# Embedding model for axiom_cvv_verify.py's optional semantic action-
# detection fallback. Sourced from a GitHub repo with the ONNX weights
# committed directly in-repo -- no Hugging Face/Ollama dependency, which
# matters here since this build may run in a network-restricted CI
# environment that only allowlists package registries + GitHub.
COPY --chmod=0755 scripts/fetch_embedding_model.sh /usr/local/bin/fetch_embedding_model.sh
RUN /usr/local/bin/fetch_embedding_model.sh "${HOME}/.cache/axiom-cvv/all-minilm-l6-v2"
ENV AXIOM_CVV_EMBEDDING_MODEL_DIR="${HOME}/.cache/axiom-cvv/all-minilm-l6-v2"

ENV OPENCODE_CONFIG=/opt/harness/opencode.base.json
WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# --- Stage: model (built once per model under test) ---------------------
# Thin layer on top of the shared, cached harness. Carries only the model
# identity as build args/env/labels — no files change here, so this layer
# is nearly free to rebuild per model and keeps a provenance record
# (`docker inspect` on the resulting image shows exactly which model it
# was built for) alongside the immutable base and shared harness below it.
FROM harness AS model

ARG MODEL_PROVIDER
ARG MODEL_ID

RUN test -n "${MODEL_PROVIDER}" || (echo "FATAL: MODEL_PROVIDER build-arg is required" >&2 && exit 1)
RUN test -n "${MODEL_ID}" || (echo "FATAL: MODEL_ID build-arg is required" >&2 && exit 1)

ENV OPENCODE_MODEL_PROVIDER=${MODEL_PROVIDER} \
    OPENCODE_MODEL_ID=${MODEL_ID}

LABEL eval.model.provider="${MODEL_PROVIDER}" \
      eval.model.id="${MODEL_ID}" \
      eval.harness.version="1"
