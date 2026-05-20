from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


INDEX_GEN_PATTERN = re.compile(r"index-(\d+)$")


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


def download_files(bucket: str, files: list[str], download_dir: Path) -> list[Path]:
    downloaded: list[Path] = []
    for key in files:
        destination = build_local_download_path(download_dir, bucket, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        run_aws_command(["s3", "cp", s3_uri(bucket, key), str(destination)])
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


def save_json_output(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def handle_list(args: argparse.Namespace) -> None:
    bucket, prefix = parse_s3_uri(args.s3_uri)
    files_to_download = find_index_generation_files(bucket, prefix)
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


def handle_download(args: argparse.Namespace) -> None:
    bucket, prefix = parse_s3_uri(args.s3_uri)
    files_to_download = find_index_generation_files(bucket, prefix)

    latest_index_key = next(key for key in files_to_download if INDEX_GEN_PATTERN.search(Path(key).name))
    repo_data = json.loads(read_s3_file_text(bucket, latest_index_key))

    indices = repo_data.get("indices")
    if not isinstance(indices, dict) or args.index_name not in indices:
        raise RuntimeError(f"Index '{args.index_name}' not found in snapshot repository data.")

    index_payload = indices[args.index_name]
    index_id = normalize_index_id(index_payload)
    extra_files: list[str] = []
    if index_id:
        extra_files = discover_index_meta_files(bucket, prefix, index_id)

    all_files = sorted(set(files_to_download + extra_files))
    if not confirm_download(all_files):
        print("Download cancelled.")
        return

    staging_dir = Path(args.download_dir).expanduser().resolve()
    download_files(bucket, all_files, staging_dir)

    output_path = Path(args.output_file).expanduser().resolve()
    save_json_output(output_path, index_payload)
    print(f"Saved raw JSON for index '{args.index_name}' to {output_path}")


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
        help="Download metadata for a specific index and save its raw JSON.",
    )
    download_parser.add_argument("s3_uri", help="Snapshot repository path, e.g. s3://bucket/prefix")
    download_parser.add_argument("index_name", help="Exact index name to export as JSON")
    download_parser.add_argument(
        "--output-file",
        default="./index.json",
        help="File path where the specific index raw JSON payload is saved.",
    )
    download_parser.set_defaults(func=handle_download)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
