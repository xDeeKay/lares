"""Shared Docker Engine API client accessor, used by both the container
collector and the API's live actions (stop/restart/logs).

docker.from_env() already respects DOCKER_HOST if set; this wrapper just
gives every caller one place to get a client from.
"""

import docker


def get_docker_client() -> docker.DockerClient:
    return docker.from_env()
