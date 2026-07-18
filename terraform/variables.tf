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
  description = "Host port mapped to the server container's opencode serve port (4096 internal, fixed by this project -- not opencode's own default, which is a random port on 127.0.0.1 only. See entrypoint.sh."
  type        = number
  default     = 4096
}

variable "gemma4_local_server_url" {
  description = "URL the gemma4-31b-local container (host networking, no shared-network DNS) uses to reach the server container. Defaults to localhost since network_mode=host puts it on the same network namespace as the Docker host -- adjust if your server container's published port differs from var.serve_port for some reason."
  type        = string
  default     = "http://localhost:4096"
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
