resource "docker_image" "model" {
  for_each = var.models

  name = "opencode-model-eval-${each.key}:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
      MODEL_PROVIDER = each.value.provider
      MODEL_ID       = each.value.id
    }
  }

  # Content-hashed rebuild triggers, not just "ran apply again" — plan
  # only shows a rebuild diff when something that actually matters
  # changed. Same approach as ctx-squid-test-harness's main.tf.
  triggers = {
    dockerfile_sha1        = filesha1("${var.harness_root}/Dockerfile")
    entrypoint_sha1        = filesha1("${var.harness_root}/entrypoint.sh")
    run_test_ladder_sha1   = filesha1("${var.harness_root}/scripts/run_test_ladder.py")
    config_sha1            = filesha1("${var.harness_root}/config/opencode.base.json")
    model_provider          = each.value.provider
    model_id                = each.value.id
  }
}

resource "docker_container" "model" {
  for_each = var.models

  name  = "opencode-model-eval-${each.key}"
  image = docker_image.model[each.key].image_id

  # This is a batch job, not a persistent service: entrypoint.sh runs the
  # fixed task suite once and exits. must_run = false so a completed
  # (stopped) container isn't treated as configuration drift on the next
  # plan. Unlike ctx-squid-test-harness's interactive TESTPLAN.md case,
  # there's no "sleep infinity" idle container to exec into here — the
  # container's own exit code and the results/ volume are the actual
  # signal.
  must_run = false
  attach   = false
  rm       = false # keep the stopped container inspectable: `docker logs`, exit code

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
    host_path      = abspath("${var.harness_root}/results/${each.key}")
    container_path = "/results"
    read_only      = false
  }
}

# --- Model discovery -----------------------------------------------------
# Honest limitation, not smoothed over: Terraform's plan/apply model
# doesn't fit "run this, read its stdout, use it as input" within a
# single apply the way docker-compose's discover -> adhoc chain does.
# This resource runs discovery and lets you read the result out of
# results/discovered/discovered-model.env afterward -- it's a two-phase
# workflow (apply once to discover, then add the resolved provider/id to
# var.models and apply again), not a single-command pipeline. For a
# smoother discovery-to-run flow, use `docker compose run discover` then
# `docker compose run --env-file results/discovered-model.env adhoc`
# instead.
resource "docker_image" "discover" {
  name = "opencode-model-eval-discover:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    target     = "harness"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
    }
  }

  triggers = {
    dockerfile_sha1 = filesha1("${var.harness_root}/Dockerfile")
    discover_sha1   = filesha1("${var.harness_root}/scripts/discover_and_select_model.py")
  }
}

resource "docker_container" "discover" {
  name  = "opencode-model-eval-discover"
  image = docker_image.discover.image_id

  entrypoint = ["python3", "/usr/local/bin/discover_and_select_model.py"]
  # To select a specific model instead of running discovery, override
  # with: command = ["--model", "zhipu/glm-5.2"]

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

# --- Local model (Ollama-backed) ----------------------------------------
# Kept as a separate resource pair rather than folded into the for_each
# map above: it needs network_mode = "host" to reach a host-run Ollama
# instance, which the cloud-routed models don't need and shouldn't carry.
# Mirrors docker-compose.yml's separate gemma4-31b-local service for the
# same reason.
resource "docker_image" "gemma4_local" {
  name = "opencode-model-eval-gemma4-31b-local:latest"

  build {
    context    = var.harness_root
    dockerfile = "Dockerfile"
    build_args = {
      OPENCODE_IMAGE = var.opencode_image
      OPENCODE_REF   = var.opencode_ref
      MODEL_PROVIDER = "ollama"
      MODEL_ID       = "gemma4:31b"
    }
  }

  triggers = {
    dockerfile_sha1      = filesha1("${var.harness_root}/Dockerfile")
    entrypoint_sha1      = filesha1("${var.harness_root}/entrypoint.sh")
    run_test_ladder_sha1 = filesha1("${var.harness_root}/scripts/run_test_ladder.py")
    config_sha1          = filesha1("${var.harness_root}/config/opencode.base.json")
  }
}

resource "docker_container" "gemma4_local" {
  name  = "opencode-model-eval-gemma4-31b-local"
  image = docker_image.gemma4_local.image_id

  must_run     = false
  attach       = false
  rm           = false
  network_mode = "host"
  # network_mode = "host" is Linux-only in Docker; this resource will not
  # behave the same under Docker Desktop on macOS/Windows. Not resolved
  # by this config — same caveat as docker-compose.yml.

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
    host_path      = abspath("${var.harness_root}/results/gemma4-31b-local")
    container_path = "/results"
    read_only      = false
  }
}
