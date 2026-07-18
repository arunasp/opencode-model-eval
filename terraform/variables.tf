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

variable "models" {
  description = "Map of model slug -> {provider, id}. One docker_image + docker_container gets built/run per entry via for_each. `hy3` was verified against the live OpenCode Zen API (corrected from opencode-zen/hy3 to opencode/hy3-free -- the docs page omitted hy3-free while the live API listed it). `deepseek-v4-pro` and `glm-5-2` are still unverified guesses -- confirm against a live `opencode models --refresh` listing before relying on their results."
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
