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

variable "host_uid" {
  description = "uid every container runs as, passed both as a build arg (baking a matching passwd entry into the image) and as WORKER_UID at runtime (read by entrypoint.sh, which drops privileges before exec'ing anything). Without it the containers run as root and everything they write to a bind-mounted host path -- results/, notebooks/ -- is left root-owned. Compose reads the same value from HOST_UID; Terraform has no equivalent of the Makefile's `id -u`, so this default has to be stated. 1000 is the observed uid on Cyberdyne."
  type        = number
  default     = 1000
}

variable "host_gid" {
  description = "gid counterpart to var.host_uid. See its description."
  type        = number
  default     = 1000
}

variable "container_nproc_limit" {
  description = "nproc ulimit applied to every container. RLIMIT_NPROC is enforced per real uid system-wide rather than per container, and this daemon's own default was measured at 128:256 -- low enough that every exec following entrypoint.sh's uid switch fails with EAGAIN (`Resource temporarily unavailable`) for any binary. This is a prerequisite of the privilege drop, not a tuning knob; cicd_runner passes the same 8192 to every worker for the same reason."
  type        = number
  default     = 8192
}

variable "serve_port" {
  description = "Host port mapped to the server container's opencode serve port (4096 internal, fixed by this project -- not opencode's own default, which is a random port on 127.0.0.1 only. See entrypoint.sh. Default changed from 4096 to 49604 -- Cyberdyne also runs Axiom's own separate opencode serve instance, and 4096 risked colliding with it. Picked from IANA's dynamic/private port range (49152-65535, RFC 6335); not verified against Axiom's actual chosen port, since that's outside this repo's config. Deliberately DIFFERENT from docker-compose.yml's own default (49605) -- confirmed live that sharing one default meant Compose and Terraform could never both be up at once without a real port-bind collision, even though README's \"Two deployment paths\" section already frames both as legitimately simultaneous."
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

variable "opencode_ollama_base_url" {
  description = "OpenAI-compat baseURL the SERVER container's opencode local/ollama provider (@ai-sdk/openai-compatible) uses. MUST end in /v1 -- Ollama's Go router (server/routes.go) registers the OpenAI-compat surface literally at /v1/chat/completions etc, and the SDK does not inject /v1 itself. host.docker.internal:host-gateway only reaches services bound to 0.0.0.0; if Ollama is bound to 127.0.0.1 only (its default), this will not work until Ollama is started with OLLAMA_HOST=0.0.0.0:11434."
  type        = string
  default     = "http://host.docker.internal:11434/v1"
}

variable "ollama_tags_url" {
  description = "Ollama's native /api/tags endpoint (NOT the OpenAI-compat /v1 path) -- used by discover_local_ollama_models.py at server startup to auto-detect installed models, same host/port as var.ollama_native_base_url."
  type        = string
  default     = "http://host.docker.internal:11434/api/tags"
}

variable "ollama_native_base_url" {
  description = "Ollama's native API root (/api/chat, /api/generate, /api/ps -- no /v1) used by the JUPYTER container's notebooks and by run_eval_client.py's warm-up/unload logic. Distinct from var.opencode_ollama_base_url: Ollama registers these as two disjoint route trees server-side, so one URL cannot serve both consumers. Same host.docker.internal caveat as above."
  type        = string
  default     = "http://host.docker.internal:11434"
}

variable "session_reaper_enabled" {
  description = "Runs scripts/session_reaper.py as a background loop inside the SERVER container, alongside opencode serve (see entrypoint.sh). opencode has no native session TTL -- this closes sessions an abruptly disconnected client never aborted itself, which was keeping local/ollama models persistently loaded."
  type        = bool
  default     = true
}

variable "local_session_ttl_s" {
  description = "Idle threshold applied to sessions whose Session.Info.model.providerID matches the local/ollama provider key (see OPENCODE_OLLAMA_PROVIDER_KEY). Deliberately aggressive (10min default): sustained local/ollama residency is the actual resource cost this exists to cut. Provider-scoping confirmed reliable via source (session/prompt.ts:672-689 -- setAgentModel() always fires on a session's first message, since session.model starts unset), not guessed."
  type        = number
  default     = 600
}

variable "session_ttl_s" {
  description = "Fallback idle threshold for everything else session_reaper.py sees: non-local providers, and sessions that haven't sent a first message yet (no model to scope by). Kept above run_eval_client.py's own 50min QUOTA_WAIT_THRESHOLD_S (3000s default) so it never preempts a legitimate cloud quota-retry wait -- var.local_session_ttl_s is the one doing the actual aggressive cleanup work."
  type        = number
  default     = 3600
}

variable "session_reaper_poll_interval_s" {
  description = "How often session_reaper.py calls GET /session and checks each session's idle time against var.local_session_ttl_s or var.session_ttl_s."
  type        = number
  default     = 120
}

variable "opencode_global_config_path" {
  description = "Host path to your own real opencode global config (~/.config/opencode/opencode.json). Mounted read-only into every container that runs opencode, at the same path opencode's own config.ts loadGlobal() expects (confirmed via source: xdg-basedir's config dir, joined with the app name -- ~/.config/opencode on a standard XDG-compliant Linux setup). This is what supplies provider/model declarations now -- see REQUIREMENTS.md for the format expected. Loaded BEFORE this project's own OPENCODE_CONFIG, which only overlays the permission/baseURL overrides each role actually needs to differ (confirmed via source: OPENCODE_CONFIG is a merge on top, not a replacement)."
  type        = string
  default     = "~/.config/opencode/opencode.json"
}


