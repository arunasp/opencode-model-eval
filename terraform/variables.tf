variable "harness_root" {
  description = "Path to the opencode-model-eval directory (the Docker build context) — one level up from this terraform/ dir."
  type        = string
  default     = ".."
}

variable "opencode_image" {
  description = "Base opencode image repository, passed through as a build arg."
  type        = string
  default     = "ghcr.io/anomalyco/opencode"
}

variable "opencode_ref" {
  description = "Tag or digest of the base opencode image. Defaults to \"latest\" as a placeholder, not a recommendation — resolve and pin a sha256 digest before treating results as reproducible. See README."
  type        = string
  default     = "latest"
}

variable "serve_port" {
  description = "Host port mapped to the server container's opencode serve port (4096 internal, fixed by this project -- not opencode's own default, which is a random port on 127.0.0.1 only. See entrypoint.sh. Default changed from 4096 to 49604 -- Cyberdyne also runs Axiom's own separate opencode serve instance, and 4096 risked colliding with it. Picked from IANA's dynamic/private port range (49152-65535, RFC 6335); not verified against Axiom's actual chosen port, since that's outside this repo's config."
  type        = number
  default     = 49604
}

variable "local_server_url" {
  description = "URL local-model eval containers (host networking, no shared-network DNS) use to reach the server container. Defaults to localhost since network_mode=host puts them on the same network namespace as the Docker host. Port must match var.serve_port -- kept as a separate variable rather than interpolated from it because Terraform doesn't allow referencing one variable's value in another variable's default."
  type        = string
  default     = "http://localhost:49604"
}

variable "ollama_base_url" {
  description = "URL the SERVER container (bridge networking, not host mode) uses to reach a host-run Ollama instance -- distinct from local_server_url above, which is the opposite direction (eval-client -> server). host.docker.internal:host-gateway only reaches services bound to 0.0.0.0; if Ollama is bound to 127.0.0.1 only (its default), this will not work until Ollama is started with OLLAMA_HOST=0.0.0.0:11434."
  type        = string
  default     = "http://host.docker.internal:11434/v1"
}

variable "ollama_tags_url" {
  description = "Ollama's native /api/tags endpoint (NOT the OpenAI-compat /v1 path) -- used by discover_local_ollama_models.py at server startup to auto-detect installed models, same host/port as var.ollama_base_url."
  type        = string
  default     = "http://host.docker.internal:11434/api/tags"
}

variable "local_ollama_models" {
  description = "Map of local-model slug -> Ollama model ID, matching `ollama list` on the host. All share the SAME docker_image.harness (no per-model build) and network_mode=host (to reach the server via local_server_url). Model IDs verified against Cyberdyne's `ollama list` as of 2026-07-20 -- re-check that listing before relying on a name here if it's since changed."
  type        = map(string)
  default = {
    "gemma4-local"             = "gemma4:31b"
    "nemotron-3-nano-local"    = "nemotron-3-nano:30b"
    "qwen3-coder-local"        = "qwen3-coder:30b"
    "qwen3-coder-fixed-local"  = "qwen3-coder-fixed:30b"
    "qwen2.5-coder-local"      = "qwen2.5-coder:7b"
  }
}

variable "models" {
  description = "Map of model slug -> {provider, id}. Every entry shares the SAME docker_image.harness now (no per-model build) -- adding a model here costs zero rebuild, just a new docker_container.eval[key] with different runtime env vars. `hy3` was verified against the live OpenCode Zen API (corrected from opencode-zen/hy3 to opencode/hy3-free -- the docs page omitted hy3-free while the live API listed it). `deepseek-v4-pro` and `glm-5-2` are still unverified guesses -- confirm against a live `opencode models --refresh` listing before relying on their results."
  type = map(object({
    provider = string
    id       = string
  }))
  default = {
    "hy3" = {
      provider = "opencode"
      id       = "hy3-free"
    }
    "deepseek-v4-pro" = {
      provider = "deepseek"
      id       = "deepseek-v4-pro"
    }
    "glm-5-2" = {
      provider = "zhipu"
      id       = "glm-5.2"
    }
  }
}
