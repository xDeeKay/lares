"""Image update checker: compares a running container's local image digest
against the registry's current digest for the same tag.

Docker Hub only for now (the common case for home lab images). Any other
registry (ghcr.io, private registries, etc) returns None (unknown) rather
than guessing, same fail-safe-to-"don't know" posture as the disk freshness
classifier: better to silently not flag an update than to falsely claim one
is available.
"""

import logging

import requests

logger = logging.getLogger(__name__)

DOCKERHUB_AUTH_URL = "https://auth.docker.io/token"
DOCKERHUB_REGISTRY_URL = "https://registry-1.docker.io"
MANIFEST_ACCEPT_HEADER = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)


def _parse_dockerhub_ref(image_ref: str) -> tuple[str, str] | None:
    """Return (repo, tag) if image_ref looks like a Docker Hub image, else None."""
    ref = image_ref.split("@", 1)[0]  # ignore any existing digest pin for tag lookup
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" in last_segment:
        name, tag = ref.rsplit(":", 1)
    else:
        name, tag = ref, "latest"

    first_part = name.split("/", 1)[0]
    looks_like_other_registry = "." in first_part or ":" in first_part or first_part == "localhost"
    if looks_like_other_registry:
        return None

    repo = name if "/" in name else f"library/{name}"
    return repo, tag


def _get_dockerhub_token(repo: str) -> str | None:
    try:
        resp = requests.get(
            DOCKERHUB_AUTH_URL,
            params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("token")
    except requests.RequestException as exc:
        logger.debug("dockerhub auth failed for %s: %s", repo, type(exc).__name__)
        return None


def _get_remote_digest(repo: str, tag: str) -> str | None:
    token = _get_dockerhub_token(repo)
    if not token:
        return None
    try:
        resp = requests.get(
            f"{DOCKERHUB_REGISTRY_URL}/v2/{repo}/manifests/{tag}",
            headers={"Authorization": f"Bearer {token}", "Accept": MANIFEST_ACCEPT_HEADER},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.headers.get("Docker-Content-Digest")
    except requests.RequestException as exc:
        logger.debug("manifest fetch failed for %s:%s: %s", repo, tag, type(exc).__name__)
        return None


def check_for_update(image_ref: str, local_repo_digests: list[str]) -> bool | None:
    """True/False if determinable, None if unknown (unsupported registry,
    network failure, or no local digest to compare against)."""
    parsed = _parse_dockerhub_ref(image_ref)
    if parsed is None:
        return None
    repo, tag = parsed

    remote_digest = _get_remote_digest(repo, tag)
    if remote_digest is None:
        return None

    local_digests = {d.split("@", 1)[1] for d in local_repo_digests if "@" in d}
    if not local_digests:
        return None  # e.g. a locally-built image with no registry digest at all

    return remote_digest not in local_digests
