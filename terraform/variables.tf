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
  description = "Map of model slug -> {provider, id}. One docker_image + docker_container gets built/run per entry via for_each. Provider/id values here are unverified against a live `opencode models --refresh` listing — confirm before relying on results (see README)."
  type = map(object({
    provider = string
    id       = string
  }))
  default = {
    "hy3" = {
      provider = "opencode-zen"
      id       = "hy3"
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
