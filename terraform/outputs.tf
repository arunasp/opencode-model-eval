output "next_step" {
  description = "Run this after apply to tail the server, then use scripts/tf-select-and-run-eval.sh (or `make tf-eval MODEL=...`) for a cloud OR local eval run -- there's no static per-model container to tail anymore, cloud or local alike (see docker_container.discover's comment in main.tf, and the comment where docker_container.local_ollama used to be)."
  value = join("\n", [
    "docker logs -f ${docker_container.server.name}   # persistent opencode serve",
    "docker logs ${docker_container.jupyter.name}   # jupyter lab -- URL+token printed here on first start (http://localhost:${var.jupyter_port}/?token=...)",
    "http://localhost:${var.git_workspace_port}   # git-workspace's own opencode serve, once started (make tf-git-workspace) -- connect a client and specify provider/model per-session, same as server; there's no separate model picker for this role",
  ])
}

output "results_dirs" {
  description = "Host path where results land. Unlike the old per-model design, run_eval_client.py computes its own model-slug subdirectory under a single shared results/ root (provider_modelid, e.g. results/opencode_hy3-free/) -- there isn't a separate terraform-key-named directory per model anymore, since the eval containers all mount the same host results/ path."
  value       = abspath("${var.harness_root}/results")
}
