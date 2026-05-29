# Index Automation

Small CLI utility for reading Elasticsearch snapshot repository metadata from S3 and optionally exporting **documents** from snapshots.

## Why `download` is not your index data

Snapshot **data** objects under `indices/<repo-index-id>/<shard>/` are mostly **Lucene binary** segment files (plus SMILE-encoded manifests like `snap-*.dat`). Turning those blobs into JSON documents means running the same **Lucene codecs** Elasticsearch uses. There is no small, stable “pure Python” parser for arbitrary ES/Lucene versions.

To get **actual `_source` documents as JSON**, this project includes **`export-documents`**, which starts a **throwaway Elasticsearch in Docker**, registers the **same S3 repository**, restores one snapshot into that local node, and streams hits to **JSON Lines** (one JSON object per line). That does not touch your production cluster, but it **is** still Elasticsearch reading the blobs (the supported decoding path).

## Requirements

- Python managed by `uv`
- AWS CLI v2 configured with credentials that can access the snapshot bucket
- For **`export-documents`**: [Docker](https://docs.docker.com/get-docker/) and network access to pull `docker.elastic.co/elasticsearch/elasticsearch` (override with `--es-image`). AWS keys are passed into the container via `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` from your environment (IRSA-style `AWS_WEB_IDENTITY_TOKEN_FILE` is not auto-mounted; use static keys or extend the script to `-v` mount the token file).

## Usage

Run commands with `uv run`:

```bash
uv run main.py list s3://my-snapshot-bucket/path/to/repo
```

This command:

1. Shows the repository metadata files needed (`index.latest` when present and latest `index-N`)
2. Prompts for confirmation
3. Downloads those files under `./downloads`
4. Saves discovered index names to `./index-list.json`

Download a specific index payload as raw JSON:

```bash
uv run main.py download s3://my-snapshot-bucket/path/to/repo my-index-name
```

This command:

1. Resolves the index in repository metadata
2. Shows all files that will be downloaded for that index payload
3. Prompts for confirmation
4. Downloads any missing files under `./downloads` (objects already present from a previous `list` or `download` with the same `--download-dir` are skipped)
5. Writes `./output/<index-name>.json` with **snapshot repository** metadata: the `repository_index_registration` object from the repo `index-N` file (the small `id` / `shard_generations` / `snapshots` record), plus **`snapshot_shard_metadata`**: each downloaded `indices/<index id>/meta-*` file parsed as JSON. This is **not** the live cluster index definition and **not** your source documents; those exist only inside snapshot data blobs and require an Elasticsearch restore to read as an index.

Export **documents** (each line is one JSON object: the document `_source`, or a small placeholder if `_source` was not stored):

```bash
uv run main.py export-documents s3://my-snapshot-bucket/path/to/repo my-index-name
```

Writes `./output/<index-name>-documents.jsonl` unless `--output-file` is set. **Elasticsearch Docker image** defaults to the version read from the repository ``index-N`` file: the chosen snapshot’s ``version`` field when present, otherwise ``min_version``. Use ``--es-image`` if that tag does not exist on Docker Hub or restore fails. Use `--snapshot NAME` for a specific snapshot.

## Repository listing cache

The slow S3 listing that finds `index-N` (and optional `index.latest`) for a snapshot repository is cached under **`.pull-index/generation-files.json`**, keyed by bucket and prefix. After the first successful `list` or `download` for a given repository URI, later commands for the same repository skip that listing and print that they are using the cached file list.

Delete `.pull-index/` (or that JSON file) if the repository layout in S3 may have changed and you need a fresh listing.

## Optional flags

- `--download-dir` to choose a different download directory (`list` and `download` must use the same path for `download` to reuse cached snapshot files)
- **`download`:** `--output-file` overrides `./output/<index-name>.json` (see “Download” step 5 for contents)
- **`export-documents`:** `--output-file`, `--es-image`, `--host-port`, `--aws-region`, `--snapshot` (see Usage above)

---

Use `cold-qa01` as an example: https://us-east-1.console.aws.amazwinlogbeat-unknown-2023.06.05-000001on.com/s3/buckets/cold-qa01?region=us-east-1&prefix=2022-Q1/&showversions=false

```
uv run main.py list s3://cold-qa01/2022-Q1/
uv run main.py download s3://cold-qa01/2022-Q1/ 'webroot-unknown-2023.06.14-000001'
uv run main.py export-documents s3://cold-qa01/2022-Q1/ 'webroot-unknown-2023.06.14-000001'
```