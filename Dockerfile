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

# --- Stage: server -------------------------------------------------------
# Deliberately LIGHT. This is what the `server` role actually needs to
# run `opencode serve` plus this project's local-Ollama auto-discovery
# script -- nothing else. Previously the single `harness` stage below
# bundled spaCy/onnxruntime/click/git into EVERY role including this
# one, even though server never calls cvv_scan.py, run_eval_client.py,
# or anything CVV-related. That's the exact "shared-foundation scope
# creep" pattern this project already caught once before in a
# different repo's CODEGEN.md (unused MQL5/Python sections inherited
# wholesale) -- same mechanism, this time as a container's dependency
# footprint instead of a doc's sections. Every build failure hit while
# developing this repo traced back to a dependency the server role
# never actually needed.
FROM base AS server

USER root

# python3 only -- no py3-pip. discover_local_ollama_models.py is
# stdlib-only (argparse/json/urllib/pathlib), confirmed by its own
# module docstring and this Dockerfile's own build history: no pip
# package has ever been required for it. ca-certificates is genuinely
# needed here (not just for local Ollama discovery, which is plain
# HTTP to a LAN address) -- opencode itself makes real outbound HTTPS
# calls to cloud provider APIs (OpenCode Zen, DeepSeek, Zhipu) FROM
# this container when eval/discover containers route requests through
# it, and needs a trust store to verify those.
#
# jq was in every stage's install list before this split and was
# NEVER actually invoked by anything running inside any container --
# only by scripts/extract-opencode-key.sh, a HOST-side script run
# directly on the development host before auth.json is even mounted in. Dropped
# entirely, not moved to harness -- confirmed via repo-wide grep
# before removing it, not assumed unused.
# setpriv: entrypoint.sh drops from root to WORKER_UID/WORKER_GID with
# it, so files written to the bind mounts (./results, ./notebooks) are
# owned by the host user instead of root. Alpine's busybox ships a
# setpriv APPLET carrying none of the flags needed (--reuid/--regid/
# --clear-groups), so the real one has to be installed; `apk add
# setpriv` replaces the busybox symlink at /bin/setpriv in place, and
# is 3 packages / 0.1 MiB, against 34 packages / 5.3 MiB for the whole
# util-linux metapackage. Measured on this base, not estimated.
# Debian's util-linux is not split the same way, hence the two names.
RUN if command -v apk >/dev/null 2>&1; then \
      apk add --no-cache ca-certificates python3 setpriv; \
    elif command -v apt-get >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 util-linux \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "FATAL: no supported package manager (apk/apt-get) found in base image" >&2; \
      exit 1; \
    fi

# Pin HOME explicitly rather than relying on whatever user/home the base
# image happens to ship with — removes the ambiguity of where
# ~/.local/share/opencode/auth.json and ~/.config/opencode resolve to,
# which otherwise depends on an unverified base-image convention.
ENV HOME=/home/harness

# Baked-in harness user, matching this deployment's own HOST_UID/
# HOST_GID, same shape as cicd_runner's worker/Dockerfile. Defaults to
# 1000:1000 (the real, observed uid:gid on this host) but stays a build
# arg, so the image can be rebuilt for a different host user. It also
# means entrypoint.sh's getent check finds the user already present and
# skips its runtime-creation branch entirely in the common case.
ARG WORKER_UID=1000
ARG WORKER_GID=1000
# Skip creation when the uid already exists in the base image -- most
# obviously when a build is itself running as root and the caller
# derived 0:0, which previously failed the whole build on
# "addgroup: gid '0' in use". The entrypoint already guards its runtime
# path with getent; this is the same guard at build time. An image
# without a baked user is not broken: the entrypoint creates one for
# whatever uid it is handed.
RUN if getent passwd "${WORKER_UID}" >/dev/null 2>&1; then \
      echo "uid ${WORKER_UID} already present in the base image, skipping user creation"; \
    else \
      if ! getent group "${WORKER_GID}" >/dev/null 2>&1; then \
        if command -v addgroup >/dev/null 2>&1 && ! command -v groupadd >/dev/null 2>&1; then \
          addgroup -g "${WORKER_GID}" harness; \
        else \
          groupadd --gid "${WORKER_GID}" harness; \
        fi; \
      fi; \
      if command -v adduser >/dev/null 2>&1 && ! command -v useradd >/dev/null 2>&1; then \
        adduser -u "${WORKER_UID}" -G "$(getent group "${WORKER_GID}" | cut -d: -f1)" \
          -h "${HOME}" -s /bin/sh -D harness; \
      else \
        useradd --uid "${WORKER_UID}" --gid "${WORKER_GID}" --home-dir "${HOME}" \
          --shell /bin/sh --no-create-home harness; \
      fi; \
    fi

# Owned by the harness user rather than world-writable. The previous
# `chmod -R 0777` did nothing for the case it was meant to cover:
# /results and /notebooks are bind mounts at runtime, and a mount
# overlays the image directory's mode entirely. What it did cover was
# ${HOME}, which the entrypoint writes opencode.runtime.json into after
# the drop -- that is what the chown below is for.
RUN mkdir -p "${HOME}/.local/share/opencode" "${HOME}/.config/opencode" /task-suite /results \
    && chown -R "${WORKER_UID}:${WORKER_GID}" "${HOME}" /results

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY scripts/discover_local_ollama_models.py /usr/local/bin/discover_local_ollama_models.py
COPY scripts/session_reaper.py /usr/local/bin/session_reaper.py
COPY config/opencode.base.json /opt/harness/opencode.base.json
# --chmod on COPY requires BuildKit -- the development host's Docker (29.1.3) has
# no working buildx component ("BuildKit is enabled but the buildx
# component is missing or broken"), so the legacy builder is what
# actually runs here. Explicit RUN chmod instead: portable to both.
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/discover_local_ollama_models.py \
    /usr/local/bin/session_reaper.py \
    && chmod 0644 /opt/harness/opencode.base.json

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

# --- Stage: harness ------------------------------------------------------
# Extends `server` rather than `base` -- reuses its python3/entrypoint/
# config layers instead of duplicating them, and only adds what the
# eval-client/discover roles actually need on top: pip, the CVV scoring
# scripts, and their optional spaCy/onnxruntime enhancements (negation-
# aware claim detection, and axiom_cvv_verify.py's own semantic action-
# detection, which tries onnxruntime first and falls through to a
# TF-IDF implementation needing nothing beyond numpy if that genuinely
# isn't available). This is
# the stage `docker-compose.yml`'s discover/eval/local_ollama services
# build (server itself builds the `server` target above, not this one)
# -- and it's still the LAST stage in this file, so it remains the
# default `docker build`/`docker-compose build` target with no explicit
# --target needed for those roles, same as before this split.
FROM server AS harness

USER root

# Re-declared: an ARG's scope ends at its own stage, so the values used
# by the chown lines below would expand empty without this, even though
# the `server` stage above already declared them.
ARG WORKER_UID=1000
ARG WORKER_GID=1000

# Redeclared explicitly rather than relying on inheritance from
# `server` -- found live on the development host that invocations of THIS stage's
# image (docker_container.local_ollama via Terraform, a raw `docker
# run` in scripts/tf-select-and-run-eval.sh, and the same latent gap in
# docker-compose.yml's eval/local-ollama-defaults) intermittently ran
# their command ("eval-client") directly instead of through
# entrypoint.sh, even though the Dockerfile text correctly inherits
# ENTRYPOINT from `server`. the development host's Docker uses the legacy builder,
# not BuildKit (see docker_image.server's own build-arg comment above)
# -- the legacy builder has a known history of not always correctly
# propagating inherited image CONFIG metadata (as opposed to layers)
# through multi-stage builds under certain cache conditions. This line
# is textually redundant with server's own ENTRYPOINT, but removes any
# dependency on that inheritance actually working, regardless of the
# exact mechanism.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# spacy's own `python3 -m spacy download <model>` command internally
# shells out to a SEPARATE pip subprocess to install the model wheel --
# that inner call does NOT inherit the Dockerfile's own explicit
# `--break-system-packages` flag (that flag only applies to the outer
# `pip install spacy click` invocation below), and hits Alpine's
# PEP-668 "externally-managed-environment" block on its own. Setting
# this env var is pip's documented equivalent of passing
# --break-system-packages to every pip invocation, inherited by
# subprocesses -- fixes the inner call without needing to control it
# directly. Confirmed against multiple independent sources describing
# this exact "tool internally calls pip without the flag" scenario.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# git-workspace's dedicated permission profile (bash/edit allowed --
# safe here because this container is isolated, not because commands
# are narrowed to git specifically) -- only needed in this stage since
# it's the only one with git installed. Default OPENCODE_CONFIG stays
# the base config inherited from `server`; the git-workspace container
# overrides it per-container, same pattern as OPENCODE_MODEL_ID for
# local-ollama.
COPY config/opencode.git-workspace.json /opt/harness/opencode.git-workspace.json
RUN chmod 0644 /opt/harness/opencode.git-workspace.json

# py3-pip and git added here, not in the server stage: only this
# stage's scripts need them (pip for the CVV scoring layer's
# dependencies, git for fetch_embedding_model.sh's clone below).
# python3/ca-certificates are already present, inherited from `server`.
RUN if command -v apk >/dev/null 2>&1; then \
      apk add --no-cache py3-pip git; \
    elif command -v apt-get >/dev/null 2>&1; then \
      apt-get update && apt-get install -y --no-install-recommends python3-pip git \
      && rm -rf /var/lib/apt/lists/*; \
    else \
      echo "FATAL: no supported package manager (apk/apt-get) found in base image" >&2; \
      exit 1; \
    fi

# click added explicitly -- confirmed upstream bug (explosion/spaCy#13971,
# open): spacy/cli/_util.py does `from click import NoSuchOption` but
# spaCy never lists click as its own dependency, relying on typer to
# pull it in transitively. typer>=0.26 stopped depending on click, so
# it silently stopped being installed. Hit live on the development host:
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
#
# This is the BEST-CASE semantic backend when it installs -- a real
# learned transformer embedding generalizes across paraphrases/word
# choice a TF-IDF fallback can't (e.g. "invoked the tooling" vs "ran
# the command" share no vocabulary at all, but mean the same thing).
# axiom_cvv_verify.py tries this first and falls through to its own
# TF-IDF implementation (needs nothing beyond numpy, works everywhere)
# only if this genuinely isn't available -- not down to marker-only
# matching, which was the old, more brittle floor.
RUN pip install --break-system-packages --no-cache-dir onnxruntime tokenizers numpy \
    || echo "WARN: onnxruntime/tokenizers install failed -- semantic" \
            "action-detection (axiom_cvv_verify.py) will use its TF-IDF" \
            "fallback instead of the higher-fidelity transformer" \
            "embedding. Non-fatal by design: the code already handles" \
            "this via try/except, and TF-IDF is still meaningfully" \
            "better than marker-only matching. As of writing this" \
            "install is EXPECTED to fail on Alpine (musllinux) base" \
            "images and/or Python 3.14 -- onnxruntime has no published" \
            "wheel for either (microsoft/onnxruntime#25737, open)." >&2

RUN mkdir -p /task-suite /results && chown -R "${WORKER_UID}:${WORKER_GID}" /results

COPY scripts/discover_and_select_model.py /usr/local/bin/discover_and_select_model.py
COPY scripts/run_eval_client.py /usr/local/bin/run_eval_client.py
COPY scripts/tools/cvv_scan.py /opt/harness/tools/cvv_scan.py
COPY scripts/tools/axiom_cvv_verify.py /opt/harness/tools/axiom_cvv_verify.py
RUN chmod 0755 /usr/local/bin/discover_and_select_model.py /usr/local/bin/run_eval_client.py \
    && chmod 0644 /opt/harness/tools/cvv_scan.py /opt/harness/tools/axiom_cvv_verify.py

# Embedding model for axiom_cvv_verify.py's best-case (onnxruntime-
# backed) semantic action-detection path. Sourced from a GitHub repo
# with the ONNX weights committed directly in-repo -- no Hugging
# Face/Ollama dependency, which matters here since this build may run
# in a network-restricted CI environment that only allowlists package
# registries + GitHub. Harmless if onnxruntime itself failed to
# install above (e.g. Alpine+3.14) -- axiom_cvv_verify.py's own
# try/except falls through to TF-IDF either way, whether it's because
# the runtime or the model files are the thing that's missing.
COPY scripts/fetch_embedding_model.sh /usr/local/bin/fetch_embedding_model.sh
RUN chmod 0755 /usr/local/bin/fetch_embedding_model.sh \
    && /usr/local/bin/fetch_embedding_model.sh "${HOME}/.cache/axiom-cvv/all-minilm-l6-v2" \
    && chown -R "${WORKER_UID}:${WORKER_GID}" "${HOME}/.cache"
ENV AXIOM_CVV_EMBEDDING_MODEL_DIR="${HOME}/.cache/axiom-cvv/all-minilm-l6-v2"

# CMD is inherited from `server` (["serve"]) -- eval-client/discover
# roles override it per-service via docker-compose's `command:` /
# terraform's `command` argument, same as before this split.

# --- Stage: jupyter --------------------------------------------------------
# Item 9 of the pinned backlog: a persistent Jupyter server purely for
# hand-authoring/debugging custom-test notebooks (mode 3 of the settled
# custom-test design -- see memory/README's "Custom-test design"
# section). This is NOT the headless papermill execution path that
# actually runs a notebook scenario during a normal eval -- that's a
# separate, still-not-built piece of the same design (papermill isn't
# even installed here). Extends `harness` (reuses its python3/pip/git
# layers) rather than `server` or `base`, since notebooks need the same
# CVV tooling already installed there to be useful for authoring
# CVV-kind scenarios.
FROM harness AS jupyter

USER root

# Same re-declaration as the harness stage above, same reason.
ARG WORKER_UID=1000
ARG WORKER_GID=1000

# jupyterlab for the authoring server itself; ipywidgets for the model
# picker in harness_notebook.py. The picker is optional by design -- the
# library falls back to environment and the discovery file when
# ipywidgets is absent, so a notebook still runs where this package is
# not installed.
#
# papermill is deliberately NOT installed yet. The headless execution
# path it would serve is not built, and an unused dependency in an image
# is a claim about capability that nothing backs.
RUN pip install --break-system-packages --no-cache-dir jupyterlab ipywidgets

RUN mkdir -p /notebooks && chown -R "${WORKER_UID}:${WORKER_GID}" /notebooks
WORKDIR /notebooks

EXPOSE 8888
# This stage used to clear the inherited ENTRYPOINT (`ENTRYPOINT []`)
# and call `jupyter lab` straight from CMD, because entrypoint.sh only
# recognized 'serve'/'eval-client' and 'jupyter' hit its unknown-mode
# branch. entrypoint.sh now has a real jupyter mode, so the entrypoint
# is inherited again -- which is what puts this stage back on the
# privilege-drop path. It matters most here: jupyter is the role that
# writes to a host bind mount (./notebooks) on every notebook save, so
# bypassing the drop is exactly how those files ended up root-owned.
# The flags that used to live in CMD now live in entrypoint.sh's
# jupyter mode, including the removal of --allow-root, which would
# otherwise mask a drop that silently failed.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["jupyter"]
