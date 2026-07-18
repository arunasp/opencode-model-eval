# --- Shared network --------------------------------------------------
# Raw docker_container resources (unlike Compose) don't get an implicit
# shared network with by-name DNS resolution -- create one explicitly so
# the eval container can reach the server container as "server:4096",
# matching docker-compose.yml's default OPENCODE_SERVER_URL.
resource "docker_network" "eval_net" {
  name = "opencode-model-eval-net"
}

# --- Server image + container -----------------------------------------
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
  image = docker_image.harness.image_id
  command = ["serve"]

  # This IS a persistent service now, unlike the old per-model batch
  # containers -- must_run = true reflects that a stopped server is
  # drift, not an expected end state.
  must_run = true
  attach   = false
  rm       = false

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

# --- Local model (Ollama-backed) ----------------------------------------
# Still needs network_mode = "host" to reach a host-run Ollama instance,
# which the shared eval_net doesn't provide -- kept as its own resource
# for that reason, same as before, but now shares docker_image.harness
# too rather than a separate per-model build.
resource "docker_container" "gemma4_local" {
  name  = "opencode-model-eval-gemma4-31b-local"
  image = docker_image.harness.image_id

  command = ["eval-client"]

  must_run     = false
  attach       = false
  rm           = false
  network_mode = "host"
  # network_mode = "host" is Linux-only in Docker; this resource will not
  # behave the same under Docker Desktop on macOS/Windows. Not resolved
  # by this config — same caveat as docker-compose.yml. Also: on host
  # networking, "server:4096" won't resolve (no user-defined network DNS)
  # -- set OPENCODE_SERVER_URL to the host's actual reachable address.

  env = [
    "OPENCODE_SERVER_URL=${var.gemma4_local_server_url}",
    "OPENCODE_MODEL_PROVIDER=ollama",
    "OPENCODE_MODEL_ID=gemma4:31b",
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
