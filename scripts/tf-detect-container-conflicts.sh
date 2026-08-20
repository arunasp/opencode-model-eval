#!/bin/bash
# tf-detect-container-conflicts.sh -- reports containers that already
# hold a name this configuration wants, for use as a Terraform
# `data "external"` program.
#
# WHY THIS EXISTS. Terraform plans against its own state, so a
# container it did not create is invisible to the plan. The operator
# approves an apply that looks clean, Terraform builds the volume, the
# network and three images, and only then fails at create time with
# "container name is already in use". The work is wasted and the
# approval was made on a plan that could not show the problem. This
# script surfaces the conflict as data, so a precondition can fail the
# PLAN instead -- at the point where the operator is already deciding.
#
# The collision is not the underlying constraint. Compose and
# Terraform publish the same ports, so only one of the two runtimes can
# exist at a time; renaming would just move the failure from a name
# conflict to a port conflict. Refusing early and naming the other
# runtime is the honest outcome.
#
# ADOPTION IS DELIBERATELY NOT OFFERED. Importing a Compose-created
# container into Terraform state would leave Compose believing it still
# owns something Terraform will later destroy -- two systems with
# conflicting beliefs about one container, which is worse than the
# conflict it resolves. The operator takes the other stack down
# explicitly.
#
# CONTRACT (hashicorp/external): stdin is the `query` object, stdout
# must be a flat JSON object of strings, and EVERY value is stored in
# terraform.tfstate in plaintext. This prints container names, ids and
# provenance only -- nothing from inside a container, no environment,
# no mounts.
#
# Exit non-zero ONLY when the check itself could not run. A conflict is
# a normal result reported through stdout, not a script failure: the
# precondition decides what a conflict means, this only reports facts.
set -euo pipefail

MANAGED_NAMES=(
  opencode-model-eval-server
  opencode-model-eval-discover
  opencode-model-eval-git-workspace
  opencode-model-eval-jupyter
)

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found on PATH; cannot check for container conflicts" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "docker daemon unreachable; cannot check for container conflicts" >&2
  exit 1
fi

conflicts=""
compose_owned=""
foreign=""

for name in "${MANAGED_NAMES[@]}"; do
  # `docker container inspect`, NOT `docker inspect`: the latter also
  # resolves IMAGES, and this project's images share their containers'
  # names, so every name reported a phantom conflict against its own
  # image. Found by running it -- the output named sha256 ids where
  # container ids belonged.
  if ! id="$(docker container inspect --format '{{.Id}}' "${name}" 2>/dev/null)"; then
    continue
  fi

  compose_project="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' "${name}" 2>/dev/null || true)"
  managed_by="$(docker container inspect --format \
    '{{index .Config.Labels "managed-by"}}' "${name}" 2>/dev/null || true)"

  short_id="${id:0:12}"
  if [ -n "${compose_project}" ] && [ "${compose_project}" != "<no value>" ]; then
    owner="compose:${compose_project}"
    compose_owned="${compose_owned}${compose_owned:+ }${name}"
  elif [ "${managed_by}" = "terraform" ]; then
    # Ours, but absent from state -- state was lost or moved. Still a
    # conflict: this script does not decide, it reports provenance so
    # the message can say which case it is.
    owner="terraform-orphan"
  else
    owner="unlabelled"
    foreign="${foreign}${foreign:+ }${name}"
  fi
  conflicts="${conflicts}${conflicts:+, }${name}(${short_id}, ${owner})"
done

if [ -n "${conflicts}" ]; then
  found="true"
else
  found="false"
fi

# jq is not assumed: the values are container names and short ids from
# our own fixed list, so no escaping is required beyond what is here.
printf '{"found":"%s","detail":"%s","compose_owned":"%s","foreign":"%s"}\n' \
  "${found}" "${conflicts}" "${compose_owned}" "${foreign}"
