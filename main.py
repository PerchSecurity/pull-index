from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import snapshot_export
from repo_es_version import resolve_es_docker_image

INDEX_GEN_PATTERN = re.compile(r"index-(\d+)$")

GENERATION_FILES_CACHE_VERSION = 1
GENERATION_FILES_CACHE_PATH = Path(".pull-index") / "generation-files.json"


def parse_s3_uri(s3_uri: str) -> tuple[str, str]:
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    prefix = parsed.path.lstrip("/")
    return parsed.netloc, prefix


def run_aws_command(args: list[str], expect_json: bool = False) -> Any:
    result = subprocess.run(
        ["aws", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AWS CLI command failed")

    if not expect_json:
        return result.stdout

    stdout = result.stdout.strip()
    if not stdout:
        return {}
    return json.loads(stdout)


def key_with_prefix(prefix: str, name: str) -> str:
    if not prefix:
        return name
    return f"{prefix.rstrip('/')}/{name}"


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def list_objects(bucket: str, prefix: str) -> list[str]:
    response = run_aws_command(
        ["s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--output", "json"],
        expect_json=True,
    )
    return [item["Key"] for item in response.get("Contents", [])]


def object_exists(bucket: str, key: str) -> bool:
    result = subprocess.run(
        ["aws", "s3api", "head-object", "--bucket", bucket, "--key", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def detect_latest_index_file(bucket: str, prefix: str) -> str:
    repo_prefix = prefix.rstrip("/")
    if repo_prefix:
        list_prefix = f"{repo_prefix}/"
    else:
        list_prefix = ""

    keys = list_objects(bucket, list_prefix)
    generations: list[tuple[int, str]] = []
    for key in keys:
        relative_key = key[len(list_prefix) :] if list_prefix else key
        match = INDEX_GEN_PATTERN.match(relative_key)
        if match:
            generations.append((int(match.group(1)), key))

    if not generations:
        raise RuntimeError("No index-N file found in the provided S3 snapshot path.")

    _, latest_key = max(generations, key=lambda item: item[0])
    return latest_key


def build_local_download_path(base_dir: Path, bucket: str, key: str) -> Path:
    return base_dir / bucket / key


def confirm_download(files: list[str]) -> bool:
    print("Files that will be downloaded:")
    for file_path in files:
        print(f"  - {file_path}")
    answer = input("Continue downloading these files? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def download_s3_object_to_path(bucket: str, key: str, destination: Path) -> None:
    """Copy an S3 object to a local path using the AWS CLI.

    Stdout and stderr are not captured so the CLI can show transfer progress
    (progress bars require an interactive terminal).
    """
    result = subprocess.run(
        ["aws", "s3", "cp", s3_uri(bucket, key), str(destination)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to download {s3_uri(bucket, key)}")


def download_files(
    bucket: str,
    files: list[str],
    download_dir: Path,
    *,
    skip_existing: bool = False,
) -> list[Path]:
    downloaded: list[Path] = []
    total = len(files)
    for index, key in enumerate(files, start=1):
        destination = build_local_download_path(download_dir, bucket, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if skip_existing and destination.is_file() and destination.stat().st_size > 0:
            print(f"[{index}/{total}] {key} (already present, skipping download)", flush=True)
        else:
            print(f"[{index}/{total}] {key}", flush=True)
            download_s3_object_to_path(bucket, key, destination)
        downloaded.append(destination)
    return downloaded


def read_json_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def extract_index_names(repo_data: dict[str, Any]) -> list[str]:
    indices = repo_data.get("indices")
    if isinstance(indices, dict):
        return sorted(indices.keys())
    raise RuntimeError("Repository data does not contain an indices object.")


def find_index_generation_files(bucket: str, prefix: str) -> list[str]:
    latest_index_key = detect_latest_index_file(bucket, prefix)
    index_latest_key = key_with_prefix(prefix, "index.latest")
    files = [latest_index_key]
    if object_exists(bucket, index_latest_key):
        files.insert(0, index_latest_key)
    return files


def normalized_repo_cache_key(bucket: str, prefix: str) -> str:
    p = prefix.rstrip("/")
    return f"s3://{bucket}/{p}/" if p else f"s3://{bucket}/"


def _cached_generation_files_valid(files: Any) -> bool:
    if not isinstance(files, list) or not files:
        return False
    return any(
        isinstance(key, str) and INDEX_GEN_PATTERN.search(Path(key).name) for key in files
    )


def load_generation_files_cache(cache_key: str) -> list[str] | None:
    if not GENERATION_FILES_CACHE_PATH.is_file():
        return None
    try:
        data = json.loads(GENERATION_FILES_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != GENERATION_FILES_CACHE_VERSION:
        return None
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None
    files = entries.get(cache_key)
    if not _cached_generation_files_valid(files):
        return None
    return [str(k) for k in files]


def save_generation_files_cache(cache_key: str, files: list[str]) -> None:
    GENERATION_FILES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, list[str]] = {}
    if GENERATION_FILES_CACHE_PATH.is_file():
        try:
            old = json.loads(GENERATION_FILES_CACHE_PATH.read_text(encoding="utf-8"))
            if (
                isinstance(old, dict)
                and old.get("version") == GENERATION_FILES_CACHE_VERSION
            ):
                raw = old.get("entries")
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(k, str) and isinstance(v, list):
                            entries[k] = [str(x) for x in v]
        except (OSError, json.JSONDecodeError):
            pass
    entries[cache_key] = list(files)
    payload = {"version": GENERATION_FILES_CACHE_VERSION, "entries": entries}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path = GENERATION_FILES_CACHE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(GENERATION_FILES_CACHE_PATH)


def resolve_index_generation_files(repository_uri: str) -> tuple[str, str, list[str]]:
    """Resolve bucket, prefix, and index generation object keys, using a local cache when possible."""
    bucket, prefix = parse_s3_uri(repository_uri)
    cache_key = normalized_repo_cache_key(bucket, prefix)
    repo_root = s3_uri(bucket, f"{prefix.rstrip('/')}/" if prefix else "")
    cached = load_generation_files_cache(cache_key)
    if cached is not None:
        print(
            f"Using cached index generation file list for {repo_root} "
            f"({GENERATION_FILES_CACHE_PATH}).",
            flush=True,
        )
        return bucket, prefix, cached
    print(
        f"Searching {repo_root} for index-N repository files (this can take a few seconds)...",
        flush=True,
    )
    files = find_index_generation_files(bucket, prefix)
    save_generation_files_cache(cache_key, files)
    return bucket, prefix, files


def read_s3_file_text(bucket: str, key: str) -> str:
    result = subprocess.run(
        ["aws", "s3", "cp", s3_uri(bucket, key), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Failed to read {s3_uri(bucket, key)}")
    return result.stdout


def read_index_metadata_text(bucket: str, key: str, download_dir: Path) -> str:
    """Load repository index JSON from the local download cache when present, else from S3."""
    cache_path = build_local_download_path(download_dir, bucket, key)
    if cache_path.is_file() and cache_path.stat().st_size > 0:
        print(f"Loading repository metadata from existing file {cache_path}", flush=True)
        return cache_path.read_text(encoding="utf-8")
    print(f"Reading repository metadata from {s3_uri(bucket, key)}...", flush=True)
    return read_s3_file_text(bucket, key)


def save_json_output(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def handle_list(args: argparse.Namespace) -> None:
    print(f"Listing indices from {args.s3_uri}", flush=True)
    bucket, prefix, files_to_download = resolve_index_generation_files(args.s3_uri)
    if not confirm_download(files_to_download):
        print("Download cancelled.")
        return

    download_dir = Path(args.download_dir).expanduser().resolve()
    downloaded = download_files(bucket, files_to_download, download_dir)

    index_file = next(path for path in downloaded if INDEX_GEN_PATTERN.search(path.name))
    repo_data = read_json_file(index_file)
    index_names = extract_index_names(repo_data)

    output_path = Path(args.output_file).expanduser().resolve()
    save_json_output(output_path, index_names)
    print(f"Saved {len(index_names)} index names to {output_path}")


def normalize_index_id(index_payload: Any) -> str | None:
    if isinstance(index_payload, dict):
        raw_id = index_payload.get("id")
        if isinstance(raw_id, str):
            return raw_id
    return None


def discover_index_meta_files(bucket: str, prefix: str, index_id: str) -> list[str]:
    base = key_with_prefix(prefix, f"indices/{index_id}/")
    keys = list_objects(bucket, base)
    return [key for key in keys if "/meta-" in key]


def read_meta_snapshot_files(
    bucket: str, download_dir: Path, object_keys: list[str]
) -> dict[str, Any]:
    """Load JSON from downloaded indices/.../meta-* objects (shard snapshot metadata)."""
    loaded: dict[str, Any] = {}
    for key in object_keys:
        if "/meta-" not in key:
            continue
        path = build_local_download_path(download_dir, bucket, key)
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            loaded[key] = read_json_file(path)
        except json.JSONDecodeError:
            loaded[key] = {
                "_error": "invalid_json",
                "path": str(path),
            }
    return loaded


def handle_download(args: argparse.Namespace) -> None:
    print(
        f"Resolving index {args.index_name!r} in snapshot repository {args.s3_uri}",
        flush=True,
    )
    bucket, prefix, files_to_download = resolve_index_generation_files(args.s3_uri)

    latest_index_key = next(key for key in files_to_download if INDEX_GEN_PATTERN.search(Path(key).name))
    download_dir = Path(args.download_dir).expanduser().resolve()
    repo_data = json.loads(read_index_metadata_text(bucket, latest_index_key, download_dir))

    indices = repo_data.get("indices")
    if not isinstance(indices, dict) or args.index_name not in indices:
        raise RuntimeError(f"Index '{args.index_name}' not found in snapshot repository data.")

    index_payload = indices[args.index_name]
    index_id = normalize_index_id(index_payload)
    extra_files: list[str] = []
    if index_id:
        meta_prefix = key_with_prefix(prefix, f"indices/{index_id}/")
        print(f"Listing shard metadata objects under {s3_uri(bucket, meta_prefix)}...", flush=True)
        extra_files = discover_index_meta_files(bucket, prefix, index_id)

    all_files = sorted(set(files_to_download + extra_files))
    if not confirm_download(all_files):
        print("Download cancelled.")
        return

    download_files(bucket, all_files, download_dir, skip_existing=True)

    meta_by_key = read_meta_snapshot_files(bucket, download_dir, all_files)

    # The `indices[index_name]` entry in the repository index-N file is only
    # registration data (internal id, snapshot UUIDs). Per-snapshot shard
    # details live in indices/<id>/meta-* blobs; merge those when present.
    output_payload: dict[str, Any] = {
        "index_name": args.index_name,
        "index_id": index_id,
        "repository_index_registration": index_payload,
        "snapshot_shard_metadata": meta_by_key,
    }

    if args.output_file is not None:
        output_path = Path(args.output_file).expanduser().resolve()
    else:
        output_dir = Path("./output").expanduser().resolve()
        output_path = output_dir / f"{args.index_name}.json"
    save_json_output(output_path, output_payload)
    print(
        f"Saved snapshot repository metadata for index {args.index_name!r} to {output_path}",
        flush=True,
    )


def handle_export_documents(args: argparse.Namespace) -> None:
    """Export indexed documents by restoring the snapshot into a local Docker Elasticsearch."""
    print(
        f"Exporting documents for index {args.index_name!r} from {args.s3_uri} "
        "(ephemeral local Elasticsearch in Docker reads the S3 blobs).",
        flush=True,
    )
    bucket, prefix, files_to_download = resolve_index_generation_files(args.s3_uri)
    latest_index_key = next(
        key for key in files_to_download if INDEX_GEN_PATTERN.search(Path(key).name)
    )
    download_dir = Path(args.download_dir).expanduser().resolve()
    repo_data = json.loads(read_index_metadata_text(bucket, latest_index_key, download_dir))

    indices = repo_data.get("indices")
    if not isinstance(indices, dict) or args.index_name not in indices:
        raise RuntimeError(f"Index {args.index_name!r} not found in snapshot repository metadata.")

    if args.output_file is not None:
        output_path = Path(args.output_file).expanduser().resolve()
    else:
        output_dir = Path("./output").expanduser().resolve()
        output_path = output_dir / f"{args.index_name}-documents.jsonl"

    es_image, image_reason = resolve_es_docker_image(
        repo_data,
        index_name=args.index_name,
        snapshot_name=args.snapshot,
        override_image=args.es_image,
        fallback_image=snapshot_export.DEFAULT_ES_IMAGE,
    )
    print(f"Using Elasticsearch Docker image {es_image} ({image_reason}).", flush=True)

    n = snapshot_export.export_documents_via_ephemeral_elasticsearch(
        bucket=bucket,
        repo_prefix=prefix,
        index_name=args.index_name,
        output_path=output_path,
        es_image=es_image,
        host_port=args.host_port,
        aws_region=args.aws_region,
        snapshot_name=args.snapshot,
    )
    print(f"Wrote {n} JSON lines to {output_path}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Work with Elasticsearch snapshot repository index files in S3.",
    )
    parser.add_argument(
        "--download-dir",
        default="./downloads",
        help="Local directory used for downloaded snapshot files.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list",
        help="Download repository index metadata and save available index names.",
    )
    list_parser.add_argument("s3_uri", help="Snapshot repository path, e.g. s3://bucket/prefix")
    list_parser.add_argument(
        "--output-file",
        default="./index-list.json",
        help="File path where the discovered index name list is saved.",
    )
    list_parser.set_defaults(func=handle_list)

    download_parser = subparsers.add_parser(
        "download",
        help="Download snapshot repository metadata for an index (registration + meta-* shards).",
    )
    download_parser.add_argument("s3_uri", help="Snapshot repository path, e.g. s3://bucket/prefix")
    download_parser.add_argument("index_name", help="Exact index name to export as JSON")
    download_parser.add_argument(
        "--output-file",
        default=None,
        help="JSON output path (default: ./output/<index-name>.json). Not Elasticsearch documents.",
    )
    download_parser.set_defaults(func=handle_download)

    export_parser = subparsers.add_parser(
        "export-documents",
        help=(
            "Export index documents as JSON Lines using a temporary local Elasticsearch "
            "(Docker) that restores from the same S3 snapshot repository."
        ),
    )
    export_parser.add_argument("s3_uri", help="Snapshot repository path, e.g. s3://bucket/prefix")
    export_parser.add_argument("index_name", help="Exact index name to export (must exist in a snapshot)")
    export_parser.add_argument(
        "--output-file",
        default=None,
        help="JSONL output path (default: ./output/<index-name>-documents.jsonl).",
    )
    export_parser.add_argument(
        "--es-image",
        default=None,
        help=(
            "Elasticsearch Docker image (default: auto from index-N snapshot "
            "`version` or `min_version`; override if restore fails for your repo)."
        ),
    )
    export_parser.add_argument(
        "--host-port",
        type=int,
        default=None,
        metavar="PORT",
        help="Host port mapped to Elasticsearch 9200 (default: random free port).",
    )
    export_parser.add_argument(
        "--aws-region",
        default=None,
        help="AWS region for the S3 repository (default: AWS_REGION / AWS_DEFAULT_REGION or us-east-1).",
    )
    export_parser.add_argument(
        "--snapshot",
        default=None,
        metavar="NAME",
        help="Snapshot name to restore (default: latest SUCCESS snapshot that contains this index).",
    )
    export_parser.set_defaults(func=handle_export_documents)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, RuntimeError, json.JSONDecodeError, TimeoutError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
