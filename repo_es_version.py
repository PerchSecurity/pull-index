"""Infer Elasticsearch release version from snapshot repository ``index-N`` JSON."""

from __future__ import annotations

import re
from typing import Any

_DOCKER_PREFIX = "docker.elastic.co/elasticsearch/elasticsearch:"
_VERSION_PREFIX_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _normalize_version_tag(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    match = _VERSION_PREFIX_RE.match(s)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def _snapshot_covers_index(snap: dict[str, Any], index_name: str) -> bool:
    indices = snap.get("indices")
    if isinstance(indices, list):
        return index_name in indices
    return False


def _snapshot_name_matches(snap: dict[str, Any], requested: str) -> bool:
    for key in ("name", "snapshot"):
        value = snap.get(key)
        if isinstance(value, str) and value == requested:
            return True
    return False


def _snapshot_sort_key(snap: dict[str, Any]) -> float:
    for field in ("end_time_in_millis", "start_time_in_millis"):
        value = snap.get(field)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def detect_repository_es_version(
    repo_data: dict[str, Any],
    *,
    index_name: str,
    snapshot_name: str | None,
) -> str | None:
    """Return a ``major.minor.patch`` string from repository metadata, or ``None``."""
    snapshots = repo_data.get("snapshots")
    candidates: list[dict[str, Any]] = []
    if isinstance(snapshots, list):
        for item in snapshots:
            if isinstance(item, dict):
                candidates.append(item)

    chosen: dict[str, Any] | None = None
    if snapshot_name:
        for snap in candidates:
            if _snapshot_name_matches(snap, snapshot_name):
                chosen = snap
                break
    else:
        covering = [s for s in candidates if _snapshot_covers_index(s, index_name)]
        if covering:
            covering.sort(key=_snapshot_sort_key)
            chosen = covering[-1]

    if chosen is not None:
        version = _normalize_version_tag(chosen.get("version"))
        if version:
            return version

    return _normalize_version_tag(repo_data.get("min_version"))


def resolve_es_docker_image(
    repo_data: dict[str, Any],
    *,
    index_name: str,
    snapshot_name: str | None,
    override_image: str | None,
    fallback_image: str,
) -> tuple[str, str]:
    """Return ``(docker_image, reason)`` for ``export-documents``."""
    if override_image:
        return override_image, "from --es-image"

    detected = detect_repository_es_version(
        repo_data,
        index_name=index_name,
        snapshot_name=snapshot_name,
    )
    if detected:
        return f"{_DOCKER_PREFIX}{detected}", f"auto from repository metadata ({detected})"

    return fallback_image, f"fallback ({fallback_image}); no usable version in index-N"

