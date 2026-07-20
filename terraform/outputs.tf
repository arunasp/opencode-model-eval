output "next_step" {
  description = "Run this after apply to tail the server and each model's eval run as it completes."
  value = join("\n", concat(
    ["docker logs -f ${docker_container.server.name}   # persistent opencode serve"],
    [for k, c in docker_container.eval : "docker logs -f ${c.name}   # ${var.models[k].provider}/${var.models[k].id}"],
    [for k, c in docker_container.local_ollama : "docker logs -f ${c.name}   # ollama/${var.local_ollama_models[k]}"]
  ))
}

output "results_dirs" {
  description = "Host path where results land. Unlike the old per-model design, run_eval_client.py computes its own model-slug subdirectory under a single shared results/ root (provider_modelid, e.g. results/opencode_hy3-free/) -- there isn't a separate terraform-key-named directory per model anymore, since the eval containers all mount the same host results/ path."
  value       = abspath("${var.harness_root}/results")
}
