output "next_step" {
  description = "Run this after apply to tail each model's run as it completes."
  value = join("\n", concat(
    [for k, c in docker_container.model : "docker logs -f ${c.name}   # ${var.models[k].provider}/${var.models[k].id}"],
    ["docker logs -f ${docker_container.gemma4_local.name}   # ollama/gemma4:31b"]
  ))
}

output "results_dirs" {
  description = "Host paths where each model's artifact-backed results land."
  value = merge(
    { for k in keys(var.models) : k => abspath("${var.harness_root}/results/${k}") },
    { "gemma4-31b-local" = abspath("${var.harness_root}/results/gemma4-31b-local") }
  )
}
