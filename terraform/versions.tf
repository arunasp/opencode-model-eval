terraform {
  required_version = ">= 1.1.5"
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 4.5"
    }
  }
}

provider "docker" {
  # Uses the local Docker socket by default
  # (unix:///var/run/docker.sock on Linux/macOS). Override with a
  # `host = "..."` argument here if your Docker daemon lives elsewhere.
}
