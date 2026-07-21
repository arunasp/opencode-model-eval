# --- Shared network --------------------------------------------------
# Raw docker_container resources (unlike Compose) don't get an implicit
# shared network with by-name DNS resolution -- create one explicitly so
# the eval container can reach the server container as "server:4096",
# matching docker-compose.yml's default OPENCODE_SERVER_URL.
resource "docker_network" "eval_net" {
  name = "opencode-model-eval-net"
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
    dockerfile_sha1        = filesha1("${var.harness_root}/Dockerfile")
    entrypoint_sha1        = filesha1("${var.harness_root}/entrypoint.sh")
    run_eval_client_sha1   = filesha1("${var.harness_root}/scripts/run_eval_client.py")
    config_sha1            = filesha1("${var.harness_root}/config/opencode.base.json")
  }
}

resource "docker_container" "server" {
  name  = "opencode-model-eval-server"
  image = docker_image.server.image_id
  command = ["serve"]

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
}

# --- Model discovery -----------------------------------------------------
# Standalone -- `opencode models --verbose` doesn't require a running
# serve instance, so this doesn't depend on docker_container.server.
resource "docker_container" "discover" {
  name  = "opencode-model-eval-discover"
  image = docker_image.harness.image_id

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
}

# --- Eval runs -----------------------------------------------------------
# One container per model in var.models, but NO per-model image -- every
# entry shares docker_image.harness and only differs by runtime env vars
# (OPENCODE_MODEL_PROVIDER/ID), talking to the one server container over
# the shared network. This is the concrete difference from the old
# design: adding a model here costs zero rebuild.
resource "docker_container" "eval" {
  for_each = var.models

  name  = "opencode-model-eval-eval-${each.key}"
  image = docker_image.harness.image_id

  command = ["eval-client"]

  must_run = false
  attach   = false
  rm       = false

  networks_advanced {
    name = docker_network.eval_net.name
  }

  env = [
    "OPENCODE_SERVER_URL=http://server:4096",
    "OPENCODE_MODEL_PROVIDER=${each.value.provider}",
    "OPENCODE_MODEL_ID=${each.value.id}",
  ]

  volumes {
    host_path      = abspath("${var.harness_root}/task-suite")
    container_path = "/task-suite"
    read_only      = true
  }

  volumes {
    host_path      = abspath("${var.harness_root}/auth-data/auth.json")
    container_path = "/home/harness/.local/share/opencode/auth.json"
    read_only      = true
  }

  volumes {
    host_path      = abspath("${var.harness_root}/results")
    container_path = "/results"
    read_only      = false
  }

  depends_on = [docker_container.server]
  # NOTE: Terraform's depends_on, like Compose's, only orders container
  # creation -- it does not wait for the server to actually be listening.
  # entrypoint.sh's eval-client mode polls before running, same gap
  # covered the same way as in docker-compose.yml.
}

# --- Local models (Ollama-backed) ---------------------------------------
# Still needs network_mode = "host" to reach a host-run Ollama instance,
# which the shared eval_net doesn't provide -- kept as its own resource
# type for that reason. for_each over var.local_ollama_models, same
# pattern as docker_container.eval above: one container per model, all
# sharing docker_image.harness, zero rebuild cost to add a model here.
resource "docker_container" "local_ollama" {
  for_each = var.local_ollama_models

  name  = "opencode-model-eval-${each.key}"
  image = docker_image.harness.image_id

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
    host_path      = abspath("${var.harness_root}/auth-data/auth.json")
    container_path = "/home/harness/.local/share/opencode/auth.json"
    read_only      = true
  }

  volumes {
    host_path      = abspath("${var.harness_root}/results")
    container_path = "/results"
    read_only      = false
  }
}
