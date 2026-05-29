"""Export index documents from an S3 snapshot repository.

Elasticsearch stores snapshot *data* as Lucene binary blobs; turning those into JSON
documents requires Lucene (the same codecs Elasticsearch uses). This module instead
runs a **throwaway local Elasticsearch in Docker**, registers the same S3 repository,
restores one snapshot into that node, and streams ``_source`` via the scroll API to
JSON Lines — no connection to your production cluster, but it is still Elasticsearch
reading the blobs (the supported path to get actual documents).
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import string
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ES_IMAGE = "docker.elastic.co/elasticsearch/elasticsearch:8.11.0"


def _http_json(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: int = 300,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {"raw": detail}
        raise RuntimeError(f"HTTP {exc.code} for {url}: {parsed}") from exc


def _quote_index(index_name: str) -> str:
    return urllib.parse.quote(index_name, safe="")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _random_suffix(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def _wait_for_cluster_ready(es_base: str, timeout_sec: int = 240) -> None:
    deadline = time.time() + timeout_sec
    health_url = f"{es_base}/_cluster/health?wait_for_status=yellow&timeout=240s"
    while time.time() < deadline:
        try:
            _http_json("GET", health_url, timeout=260)
            return
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
    raise TimeoutError(f"Elasticsearch did not become ready within {timeout_sec}s at {es_base}")


def _normalize_base_path(prefix: str) -> str:
    return prefix.rstrip("/")


def _pick_snapshot_for_index(snapshots_payload: Any, index_name: str) -> str:
    if not isinstance(snapshots_payload, dict):
        raise RuntimeError("Unexpected response when listing snapshots.")
    snaps = snapshots_payload.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        raise RuntimeError("No snapshots found in the repository after registration.")

    candidates: list[tuple[float, str]] = []
    for snap in snaps:
        if not isinstance(snap, dict):
            continue
        if snap.get("state") != "SUCCESS":
            continue
        indices = snap.get("indices")
        if not isinstance(indices, list) or index_name not in indices:
            continue
        name = snap.get("snapshot")
        if not isinstance(name, str):
            continue
        end_ms = snap.get("end_time_in_millis")
        start_ms = snap.get("start_time_in_millis")
        sort_key = float(end_ms) if isinstance(end_ms, int | float) else 0.0
        if sort_key == 0.0 and isinstance(start_ms, int | float):
            sort_key = float(start_ms)
        candidates.append((sort_key, name))

    if not candidates:
        raise RuntimeError(
            f"No SUCCESS snapshot in this repository includes index {index_name!r}. "
            "Check the snapshot repository path and index name."
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _wait_for_restored_index(es_base: str, index_name: str, timeout_sec: int = 900) -> None:
    quoted = _quote_index(index_name)
    count_url = f"{es_base}/{quoted}/_count"
    deadline = time.time() + timeout_sec
    last_error: str | None = None
    while time.time() < deadline:
        try:
            status, body = _http_json("GET", count_url, timeout=60)
            if status == 200 and isinstance(body, dict) and "count" in body:
                return
        except RuntimeError as exc:
            last_error = str(exc)
        time.sleep(3)
    raise TimeoutError(
        f"Timed out waiting for restored index {index_name!r} to open. Last error: {last_error}"
    )


def _clear_scroll(es_base: str, scroll_id: str) -> None:
    try:
        _http_json(
            "DELETE",
            f"{es_base}/_search/scroll",
            {"scroll_id": [scroll_id]},
            timeout=60,
        )
    except RuntimeError:
        pass


def _scroll_export_sources(es_base: str, index_name: str, output_path: Path) -> int:
    quoted = _quote_index(index_name)
    search_url = f"{es_base}/{quoted}/_search?scroll=2m"
    status, resp = _http_json(
        "POST",
        search_url,
        {"size": 500, "query": {"match_all": {}}},
        timeout=300,
    )
    if status != 200 or not isinstance(resp, dict):
        raise RuntimeError("Unexpected response from initial search.")

    scroll_id = resp.get("_scroll_id")
    if not isinstance(scroll_id, str):
        raise RuntimeError("Search response missing _scroll_id; scroll API may have changed.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        with output_path.open("w", encoding="utf-8") as out:
            while True:
                hits = resp.get("hits", {})
                if not isinstance(hits, dict):
                    break
                hit_list = hits.get("hits")
                if not isinstance(hit_list, list) or not hit_list:
                    break
                for hit in hit_list:
                    if not isinstance(hit, dict):
                        continue
                    src = hit.get("_source")
                    if src is None:
                        line = {
                            "_id": hit.get("_id"),
                            "_index": hit.get("_index"),
                            "_no_source": True,
                        }
                    else:
                        line = src
                    out.write(json.dumps(line, ensure_ascii=False) + "\n")
                    count += 1

                st2, resp = _http_json(
                    "POST",
                    f"{es_base}/_search/scroll",
                    {"scroll": "2m", "scroll_id": scroll_id},
                    timeout=300,
                )
                if st2 != 200 or not isinstance(resp, dict):
                    break
                new_sid = resp.get("_scroll_id")
                if isinstance(new_sid, str):
                    scroll_id = new_sid
    finally:
        _clear_scroll(es_base, scroll_id)

    return count


def export_documents_via_ephemeral_elasticsearch(
    *,
    bucket: str,
    repo_prefix: str,
    index_name: str,
    output_path: Path,
    es_image: str = DEFAULT_ES_IMAGE,
    host_port: int | None = None,
    aws_region: str | None = None,
    snapshot_name: str | None = None,
) -> int:
    """Start Docker ES, restore the index from S3, write JSONL of ``_source``, tear down.

    Returns number of lines written.
    """
    if not _docker_available():
        raise RuntimeError(
            "Docker is required for export-documents. "
            "Pure-Python decoding of snapshot Lucene blobs is not supported."
        )

    region = (
        aws_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )

    port = host_port if host_port is not None else random.randint(32000, 60000)
    es_base = f"http://127.0.0.1:{port}"
    repo_name = f"pull_idx_{_random_suffix(8)}"
    container = f"pull-index-export-{_random_suffix(8)}"

    base_path = _normalize_base_path(repo_prefix)
    repo_settings: dict[str, Any] = {
        "bucket": bucket,
        "region": region,
    }
    if base_path:
        repo_settings["base_path"] = base_path

    docker_cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container,
        "-p",
        f"{port}:9200",
        "-e",
        "discovery.type=single-node",
        "-e",
        "xpack.security.enabled=false",
        "-e",
        "xpack.license.self_generated.type=basic",
        "-e",
        "ingest.geoip.downloader.enabled=false",
        "-e",
        "cluster.routing.allocation.disk.threshold_enabled=false",
        "-e",
        "ES_JAVA_OPTS=-Xms1g -Xmx1g",
    ]
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        val = os.environ.get(var)
        if val:
            docker_cmd.extend(["-e", f"{var}={val}"])
    docker_cmd.extend(["-e", f"AWS_DEFAULT_REGION={region}"])
    docker_cmd.append(es_image)

    try:
        proc = subprocess.run(docker_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                "Failed to start Elasticsearch Docker container.\n"
                f"docker stderr: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        container_id = proc.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id or ""):
            raise RuntimeError(f"Unexpected docker run output: {container_id!r}")

        _wait_for_cluster_ready(es_base)

        _http_json(
            "PUT",
            f"{es_base}/_snapshot/{repo_name}",
            {"type": "s3", "settings": repo_settings},
            timeout=120,
        )

        _, listed = _http_json("GET", f"{es_base}/_snapshot/{repo_name}/_all", timeout=120)
        snap = snapshot_name or _pick_snapshot_for_index(listed, index_name)

        _http_json(
            "POST",
            f"{es_base}/_snapshot/{repo_name}/{urllib.parse.quote(snap, safe='')}/_restore",
            {
                "indices": index_name,
                "include_global_state": False,
                "ignore_unavailable": False,
            },
            timeout=60,
        )

        _wait_for_restored_index(es_base, index_name)

        n = _scroll_export_sources(es_base, index_name, output_path)

        try:
            _http_json("DELETE", f"{es_base}/{_quote_index(index_name)}", timeout=120)
        except RuntimeError:
            pass
        try:
            _http_json("DELETE", f"{es_base}/_snapshot/{repo_name}", timeout=120)
        except RuntimeError:
            pass

        return n
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            text=True,
            check=False,
        )
