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

variable "jupyter_port" {
  description = "Host port mapped to the jupyter container's Jupyter Lab port (8888 internal, Jupyter's own conventional default -- no collision concern verified against anything else on Cyberdyne, unlike serve_port's 4096-vs-Axiom history)."
  type        = number
  default     = 8888
}

variable "git_workspace_port" {
  description = "Host port mapped to the git-workspace container's own opencode serve port (4096 internal, same as server -- but a DIFFERENT external port, since server already publishes 4096 internal to var.serve_port on the host and both containers can't publish the same external port simultaneously). Picked from the same IANA dynamic/private range as serve_port, one above jupyter_port's conventional 8888 isn't relevant here -- this just needs to not collide with serve_port (49604) or anything else already running."
  type        = number
  default     = 49606
}

variable "ollama_base_url" {
  description = "URL the SERVER container (bridge networking) uses to reach a host-run Ollama instance. host.docker.internal:host-gateway only reaches services bound to 0.0.0.0; if Ollama is bound to 127.0.0.1 only (its default), this will not work until Ollama is started with OLLAMA_HOST=0.0.0.0:11434."
  type        = string
  default     = "http://host.docker.internal:11434/v1"
}

variable "ollama_base_port" {
  description = "PORT the SERVER container (bridge networking) uses to reach a host-run Ollama instance. host.docker.internal:host-gateway only reaches services bound to 0.0.0.0; if Ollama is bound to 127.0.0.1 only (its default), this will not work until Ollama is started with OLLAMA_HOST=0.0.0.0:11434."
  type        = number
  default     = 11434
}

variable "ollama_tags_url" {
  description = "Ollama's native /api/tags endpoint (NOT the OpenAI-compat /v1 path) -- used by discover_local_ollama_models.py at server startup to auto-detect installed models, same host/port as var.ollama_base_url."
  type        = string
  default     = "http://host.docker.internal:11434/api/tags"
}


