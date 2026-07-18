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
# Single final stage now -- no more per-model layer. Model selection was
# moved from a Docker build arg to an HTTP request parameter once
# `opencode serve`'s API was confirmed to accept providerID/modelID per
# request (server/routes/instance/httpapi/handlers/session.ts,
# session/prompt.ts's ModelRef schema). See docs/CODEGEN.md's Docker
# section for why this isn't a violation of "don't collapse stages for
# convenience" -- there's no per-model build left to cache against.
FROM base AS harness

USER root

# Base image distro isn't pinned by this Dockerfile (upstream ships both
# Debian Trixie and Alpine variants per anomalyco/opencode#19474) — detect
# the package manager rather than assume one.
#
# python3-pip and git added for the CVV scoring layer (cvv_scan.py,
# axiom_cvv_verify.py) and its optional negation-detection/semantic-
# fallback dependencies (spaCy + model, onnxruntime + tokenizers). This
# makes the harness layer meaningfully heavier than a bare opencode
# wrapper would be -- disclosed here rather than left for someone to
# discover via a slow build.
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
COPY --chmod=0755 scripts/run_eval_client.py /usr/local/bin/run_eval_client.py
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

# 4096 is this project's own chosen fixed port -- NOT opencode's default.
# Confirmed from source (cli/network.ts): real defaults are port=0
# (random) and hostname=127.0.0.1 (loopback only, unreachable from
# another container). Both explicitly overridden here so the client
# service (see docker-compose.yml) can reach this one predictably.
EXPOSE 4096
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
