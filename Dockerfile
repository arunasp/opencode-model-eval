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

# click added explicitly -- confirmed upstream bug (explosion/spaCy#13971,
# open): spacy/cli/_util.py does `from click import NoSuchOption` but
# spaCy never lists click as its own dependency, relying on typer to
# pull it in transitively. typer>=0.26 stopped depending on click, so
# it silently stopped being installed. Hit live on Cyberdyne:
# "ModuleNotFoundError: No module named 'click'" during `spacy
# download`, after pip reported all 41 packages installed successfully.
RUN pip install --break-system-packages --no-cache-dir spacy click \
    && python3 -m spacy download en_core_web_sm \
    || echo "WARN: spaCy/en_core_web_sm install failed -- negation-aware" \
            "claim detection (axiom_cvv_verify.py) will fall back to" \
            "its original, non-negation-aware behavior. Non-fatal by" \
            "design: the code already handles this via try/except." >&2

# Separate RUN, deliberately: onnxruntime has zero published wheels for
# musllinux (Alpine) on ANY Python version, and none for Python 3.14 on
# ANY platform, as of writing (confirmed against
# microsoft/onnxruntime#25737, still open). Bundling this with spaCy's
# install in one pip invocation meant onnxruntime's guaranteed failure
# on Alpine+3.14 base images silently took spaCy down with it too, even
# though spaCy itself had a working wheel available -- pip resolves a
# single invocation's requirement set atomically. Splitting these
# preserves whichever optional enhancement CAN install on a given base
# image instead of an all-or-nothing failure across both.
RUN pip install --break-system-packages --no-cache-dir onnxruntime tokenizers numpy \
    || echo "WARN: onnxruntime/tokenizers install failed -- semantic" \
            "action-detection fallback (axiom_cvv_verify.py) will fall" \
            "back to marker-only backing detection. Non-fatal by" \
            "design: the code already handles this via try/except. As" \
            "of writing this is EXPECTED on Alpine (musllinux) base" \
            "images and/or Python 3.14 -- onnxruntime has no published" \
            "wheel for either (microsoft/onnxruntime#25737, open)." >&2

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

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/discover_and_select_model.py /usr/local/bin/discover_and_select_model.py
COPY scripts/discover_local_ollama_models.py /usr/local/bin/discover_local_ollama_models.py
COPY scripts/run_eval_client.py /usr/local/bin/run_eval_client.py
COPY scripts/tools/cvv_scan.py /opt/harness/tools/cvv_scan.py
COPY scripts/tools/axiom_cvv_verify.py /opt/harness/tools/axiom_cvv_verify.py
COPY config/opencode.base.json /opt/harness/opencode.base.json
# --chmod on COPY requires BuildKit -- Cyberdyne's Docker (29.1.3) has
# no working buildx component ("BuildKit is enabled but the buildx
# component is missing or broken"), so the legacy builder is what
# actually runs here. Explicit RUN chmod instead: portable to both.
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
      /usr/local/bin/discover_and_select_model.py \
      /usr/local/bin/discover_local_ollama_models.py \
      /usr/local/bin/run_eval_client.py \
    && chmod 0644 /opt/harness/tools/cvv_scan.py \
      /opt/harness/tools/axiom_cvv_verify.py \
      /opt/harness/opencode.base.json

# Embedding model for axiom_cvv_verify.py's optional semantic action-
# detection fallback. Sourced from a GitHub repo with the ONNX weights
# committed directly in-repo -- no Hugging Face/Ollama dependency, which
# matters here since this build may run in a network-restricted CI
# environment that only allowlists package registries + GitHub.
COPY scripts/fetch_embedding_model.sh /usr/local/bin/fetch_embedding_model.sh
RUN chmod 0755 /usr/local/bin/fetch_embedding_model.sh \
    && /usr/local/bin/fetch_embedding_model.sh "${HOME}/.cache/axiom-cvv/all-minilm-l6-v2"
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
