# --- Shared network --------------------------------------------------
# Raw docker_container resources (unlike Compose) don't get an implicit
# shared network with by-name DNS resolution -- create one explicitly so
# the eval container can reach the server container as "server:4096",
# matching docker-compose.yml's default OPENCODE_SERVER_URL.
resource "docker_network" "eval_net" {
  name = "opencode-model-eval-net"
}

# Shared between server (rw) and every client container (ro) -- lets
# run_eval_client.py capture opencode's own server-side log as a
# results artifact (server.log), which has the REAL underlying error
# (e.g. ProviderModelNotFoundError) behind a generic client-visible
# HTTP 500 wrapper. NOT a "previously invisible" fix -- --print-logs
# (entrypoint.sh) already mirrors this to stderr, so `docker logs
# server` shows it live. What this actually adds: docker logs belongs
# to the daemon, tied to the server container -- eval/discover have
# no access to it, and it isn't scoped to any one run. This volume
# makes it a per-run file artifact instead.
resource "docker_volume" "opencode_log" {
  name = "opencode-model-eval-log"
}

# --- auth-data/auth.json precondition -----------------------------------
# Hit live on Cyberdyne (2026-07-21): every docker_container resource
# below bind-mounts this same host path. Docker's bind-mount behavior
# for a source path that doesn't exist yet is to silently create an
# EMPTY DIRECTORY at that path rather than erroring -- so the file
# needs to exist as a real file BEFORE the first apply that creates any
# of the containers mounting it, not after. All containers hit the
# identical "credentials not found" failure the first time this ran,
# not just server -- entrypoint.sh's credential check runs before mode
# dispatch.
#
# data.external.auth_keys runs extract-opencode-key.sh --all via
# scripts/tf-extract-auth-keys.sh -- but ONLY that wrapper's stdout
# (a bare {"status":"ok","mode":"all"} confirmation) ever reaches
# Terraform, and stdout is what data "external" stores in state
# (confirmed against hashicorp/external's own docs: "All output
# values are stored in the Terraform state file"). The wrapper never
# prints the real key material -- extract-opencode-key.sh writes
# directly to auth-data/auth.json on disk, and this data source only
# checks its exit code.
#
# --all, not a provider list, because there's no longer a static model
# matrix to derive one from (see docker_container.discover below) --
# which provider a given eval run needs is resolved live via `opencode
# models --verbose` at run time, not known at auth-scoping time.
#
# fileexists() is documented to hard-error (not return false) if the
# path is a directory rather than missing entirely -- kept as a
# defense-in-depth backstop after extraction: if the phantom-directory
# bug ever recurs (e.g. extraction ran but something else clobbered
# the path afterward), this locals block itself fails plan/apply
# immediately with Terraform's own clear "X is a directory, not a
# file" message, before any container gets created.
data "external" "auth_keys" {
  program = ["bash", "${path.module}/../scripts/tf-extract-auth-keys.sh"]
  query = {}
}

locals {
  auth_file_path   = "${var.harness_root}/auth-data/auth.json"
  auth_file_exists = fileexists(local.auth_file_path)
}

resource "terraform_data" "auth_file_check" {
  depends_on = [data.external.auth_keys]
  lifecycle {
    precondition {
      condition     = local.auth_file_exists
      error_message = "auth-data/auth.json not found at ${local.auth_file_path} even after extraction ran. Check data.external.auth_keys's output above, or run 'bash scripts/extract-opencode-key.sh' manually to see the real error."
    }
  }
}

# --- Server image + container -----------------------------------------
# Deliberately a SEPARATE, LIGHTER image from docker_image.harness below
# -- server only needs the Dockerfile's `server` target (python3 +
# entrypoint.sh + discover_local_ollama_models.py + config), not the
# spaCy/onnxruntime/CVV-scoring layer that eval/discover/local_ollama
# need. Building it as one shared image with everyone else was the
# "shared-foundation scope creep" pattern this project's own CVV
# discipline already names -- see Dockerfile's `server` stage comment
# for the full reasoning. Triggers scoped to only the files THIS
# image's `server` target actually reads (dockerfile_sha1 still
# necessarily covers the whole file, since Docker can't hash a single
# stage in isolation) -- entrypoint.sh and config are real inputs;
# run_eval_client.py deliberately is NOT a trigger here, since the
# `server` stage never copies or reads it.
resource "docker_image" "server" {
  name = "opencode-model-eval-server:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    target     = "server"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
    }
  }

  triggers = {
    dockerfile_sha1 = filesha1("${var.harness_root}/Dockerfile")
    entrypoint_sha1 = filesha1("${var.harness_root}/entrypoint.sh")
    config_sha1     = filesha1("${var.harness_root}/config/opencode.base.json")
  }
}

# Single, static image now -- no per-model build. Model selection moved
# from a Docker build arg to an HTTP request parameter once opencode
# serve's API was confirmed to accept providerID/modelID per request.
# See docs/CODEGEN.md's Docker section and README for the full reasoning.
resource "docker_image" "harness" {
  name = "opencode-model-eval-harness:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
    }
  }

  triggers = {
    dockerfile_sha1           = filesha1("${var.harness_root}/Dockerfile")
    entrypoint_sha1           = filesha1("${var.harness_root}/entrypoint.sh")
    run_eval_client_sha1      = filesha1("${var.harness_root}/scripts/run_eval_client.py")
    config_sha1               = filesha1("${var.harness_root}/config/opencode.base.json")
    git_workspace_config_sha1 = filesha1("${var.harness_root}/config/opencode.git-workspace.json")
  }
}

resource "docker_container" "server" {
  name  = "opencode-model-eval-server"
  image = docker_image.server.image_id
  command = ["serve"]
  depends_on = [terraform_data.auth_file_check]

  # This IS a persistent service now, unlike the old per-model batch
  # containers -- must_run = true reflects that a stopped server is
  # drift, not an expected end state.
  must_run = true
  attach   = false
  rm       = false

  # Linux-only (matches Cyberdyne): routes host.docker.internal to the
  # docker host's bridge gateway. See var.ollama_base_url's description
  # for the 0.0.0.0-bind caveat. Unverified beyond "resolves the
  # networking path correctly on paper" -- no Docker/Ollama access in
  # the environment this was authored in.
  host {
    host = "host.docker.internal"
    ip   = "host-gateway"
  }

  env = [
    "OPENCODE_OLLAMA_BASE_URL=${var.ollama_base_url}",
    "OPENCODE_OLLAMA_TAGS_URL=${var.ollama_tags_url}",
  ]

  networks_advanced {
    name    = docker_network.eval_net.name
    aliases = ["server"]
  }

  ports {
    internal = 4096
    external = var.serve_port
  }

  volumes {
    host_path      = abspath("${var.harness_root}/auth-data/auth.json")
    container_path = "/home/harness/.local/share/opencode/auth.json"
    read_only      = true
  }

  volumes {
    volume_name    = docker_volume.opencode_log.name
    container_path = "/home/harness/.local/share/opencode/log"
    # No read_only -- server is the only writer, needs it read-write.
  }
}

# --- Model discovery -----------------------------------------------------
# Standalone -- `opencode models --verbose` doesn't require a running
# serve instance, so this doesn't depend on docker_container.server.
#
# This is now the ONLY path to a cloud eval run, static or otherwise --
# the fixed matrix (var.models / docker_container.eval, one container
# per hardcoded provider/model pair) is gone. It covered exactly 3
# entries, one of which (hy3) was confirmed broken and the other two
# (deepseek-v4-pro, glm-5-2) had no matching provider credential in
# auth.json at all (confirmed live: only xai/groq/opencode/google/
# opencode-go/openrouter/huggingface/nvidia/poolside are configured on
# Cyberdyne) -- a static list just goes stale the moment the account's
# actual provider set changes, the same category of problem
# discover_local_ollama_models.py already solves for local models by
# querying live instead of hardcoding. scripts/tf-select-and-run-eval.sh
# is the new entry point: runs this container to resolve a
# provider/model (either live discovery or a direct --model override),
# then runs a one-off eval-client container against that result --
# mirroring how scripts/select-and-run-eval.sh's `eval` target already
# works on the Compose side (a generic runnable target, not one
# resource per model).
resource "docker_container" "discover" {
  name  = "opencode-model-eval-discover"
  image = docker_image.harness.image_id
  depends_on = [terraform_data.auth_file_check]

  entrypoint = ["python3", "/usr/local/bin/discover_and_select_model.py"]
  # To select a specific model instead of running discovery:
  # command = ["--model", "zhipu/glm-5.2"]

  must_run = false
  attach   = false
  rm       = false

  volumes {
    host_path      = abspath("${var.harness_root}/auth-data/auth.json")
    container_path = "/home/harness/.local/share/opencode/auth.json"
    read_only      = true
  }

  volumes {
    host_path      = abspath("${var.harness_root}/results/discovered")
    container_path = "/results"
    read_only      = false
  }

  volumes {
    volume_name    = docker_volume.opencode_log.name
    container_path = "/home/harness/.local/share/opencode/log"
    # Hit live on Cyberdyne: this was read_only = true, on the wrong
    # assumption (see docker_container.server's comment above) that
    # server is the only writer. `opencode models --verbose` here is a
    # raw CLI invocation, not a request to the server -- the opencode
    # CLI writes its own log file on ANY invocation, serve or not, and
    # a read-only mount made that write fail: "Unknown: FileSystem.open
    # (/home/harness/.local/share/opencode/log/opencode.log)". Read-write
    # here too. (eval-client mode, used by local_ollama and cloud eval
    # runs, is NOT affected by this -- confirmed in entrypoint.sh it
    # execs run_eval_client.py, pure Python/urllib against the remote
    # server over HTTP, never invoking the local opencode binary.)
  }
}

# --- Git-scoped one-shot workspace ---------------------------------------
# Only role in this project with git (and general bash/edit) allowed
# (config/opencode.git-workspace.json overrides OPENCODE_CONFIG's default
# bash:"deny"/edit:"deny" with bash:"allow"/edit:"allow" -- not narrowed to
# git specifically, since normal dev-workflow commands against whatever
# gets cloned are a legitimate use of this workspace too). Deliberately
# isolated to make that safe: no /results, /task-suite, or opencode-log
# mount, so there is no host path or other service's data reachable from
# inside this container regardless of what a bash command does with
# cd/-C/--git-dir. must_run=false/rm=false mirrors docker_container.discover's
# on-demand pattern, not docker_container.server's persistent one -- this
# is meant to be created fresh per task, not kept running.
resource "docker_container" "git_workspace" {
  name  = "opencode-model-eval-git-workspace"
  image = docker_image.harness.image_id
  depends_on = [terraform_data.auth_file_check]

  entrypoint = ["/usr/local/bin/entrypoint.sh", "serve"]

  env = [
    "OPENCODE_CONFIG=/opt/harness/opencode.git-workspace.json",
  ]

  must_run = false
  attach   = false
  rm       = false

  # Confirmed missing: without this, nothing could reach this
  # container's opencode serve instance at all, regardless of model
  # selection. Model selection itself needs no config here -- same
  # mechanism as docker_container.server (which also never sets
  # OPENCODE_MODEL and works fine live): whatever connects to this
  # serve instance (opencode CLI's /connect, or an HTTP call) specifies
  # provider/model per-session, exactly like run_eval_client.py already
  # does against server. There's no separate model-picker to build for
  # this role -- it's the same request-level mechanism, just a
  # different serve instance/port.
  ports {
    internal = 4096
    external = var.git_workspace_port
  }

  volumes {
    host_path      = abspath("${var.harness_root}/auth-data/auth.json")
    container_path = "/home/harness/.local/share/opencode/auth.json"
    read_only      = true
  }
}

resource "docker_image" "jupyter" {
  name = "opencode-model-eval-jupyter:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    target     = "jupyter"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
    }
  }

  triggers = {
    dockerfile_sha1 = filesha1("${var.harness_root}/Dockerfile")
  }
}

# Persistent, start/stop-controlled like docker_container.server --
# NOT one-shot like discover/git_workspace, since this is meant to stay
# up across an authoring session. See harness-control.sh's "Start/Stop
# Jupyter" menu entries and `make tf-jupyter-up`/`tf-jupyter-down`.
resource "docker_container" "jupyter" {
  name  = "opencode-model-eval-jupyter"
  image = docker_image.jupyter.image_id

  must_run = true
  attach   = false
  rm       = false

  ports {
    internal = 8888
    external = var.jupyter_port
  }

  # Host bind mount, not a named volume -- confirmed live that a named
  # volume (the original design here) genuinely doesn't appear
  # anywhere in the repo's host filesystem, same as opencode-log by
  # design. Fine for an internal log nobody browses directly, wrong for
  # notebooks someone actually wants to find/open/back up on the host.
  volumes {
    host_path      = abspath("${var.harness_root}/notebooks")
    container_path = "/notebooks"
  }
}

# Prints jupyter's connect URL+token straight to the terraform apply's
# own console during apply -- deliberately a null_resource/local-exec,
# NOT a data "external": that pattern (see data.external.auth_keys's
# own comment above) writes every result into terraform.tfstate in
# PLAINTEXT, confirmed against hashicorp/external's own docs. A live
# Jupyter token is a real credential; local-exec's output streams to
# the console and is never captured into any tracked Terraform
# attribute, so nothing ends up in state. triggers.always_run forces
# this to actually re-run on every apply that touches jupyter (a
# fixed/static trigger would only run once, on first creation).
resource "null_resource" "jupyter_connect_info" {
  depends_on = [docker_container.jupyter]

  triggers = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    # Retries briefly -- docker_container.jupyter reporting "creation
    # complete" only means the container process started, not that
    # Jupyter's own server has finished booting and printed its token
    # banner yet (observed live: this can lag by a second or two).
    command = <<-EOT
      for i in 1 2 3 4 5; do
        url="$(docker logs ${docker_container.jupyter.name} 2>&1 | grep -oE 'http://[^ ]*token=[a-f0-9]+' | tail -n1 || true)"
        if [ -n "$url" ]; then
          echo "jupyter connect URL: $url"
          exit 0
        fi
        sleep 2
      done
      echo "jupyter connect URL not found in logs yet (container may still be starting) -- retry: docker logs ${docker_container.jupyter.name}"
    EOT
  }
}

# --- Local models (Ollama-backed) ---------------------------------------
# Still needs network_mode = "host" to reach a host-run Ollama instance,
# which the shared eval_net doesn't provide -- kept as its own resource
# type for that reason. for_each over var.local_ollama_models: one
# container per model, all sharing docker_image.harness, zero rebuild
# cost to add a model here. Unlike cloud models (now discovery-only,
# see docker_container.discover above), local models are still a
# for_each over a fixed variable -- they're not a stale-goes-stale
# static guess the way the old cloud matrix was, since
# discover_local_ollama_models.py already re-derives what's actually
# installed at container startup independent of this list.
resource "docker_container" "local_ollama" {
  for_each = var.local_ollama_models

  name  = "opencode-model-eval-${each.key}"
  image = docker_image.harness.image_id
  # No depends_on = [terraform_data.auth_file_check] here, deliberately:
  # entrypoint.sh's credential check is conditional on mode + provider
  # now -- eval-client runs targeting local/ollama don't need real
  # credentials, since Ollama needs no authentication at all (config's
  # "apiKey": "ollama" is a placeholder string). server/discover/eval
  # still depend on the check -- see their resource blocks.

  entrypoint = ["/usr/local/bin/entrypoint.sh"]
  command = ["eval-client"]

  must_run     = false
  attach       = false
  rm           = false
  network_mode = "host"
  # network_mode = "host" is Linux-only in Docker; these resources will
  # not behave the same under Docker Desktop on macOS/Windows. Also: on
  # host networking, "server:4096" won't resolve (no user-defined
  # network DNS) -- var.local_server_url points at localhost instead,
  # which works because the server container publishes its port to the
  # Docker host (see docker_container.server's ports block).

  env = [
    "OPENCODE_SERVER_URL=${var.local_server_url}",
    "OPENCODE_MODEL_PROVIDER=local/ollama",
    "OPENCODE_MODEL_ID=${each.value}",
  ]

  volumes {
    host_path      = abspath("${var.harness_root}/task-suite")
    container_path = "/task-suite"
    read_only      = true
  }

  volumes {
    host_path      = abspath("${var.harness_root}/results")
    container_path = "/results"
    read_only      = false
  }

  volumes {
    volume_name    = docker_volume.opencode_log.name
    container_path = "/home/harness/.local/share/opencode/log"
    read_only      = true
  }
}
