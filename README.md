# Index Automation

Small CLI utility for reading Elasticsearch snapshot repository metadata from S3.

## Requirements

- Python managed by `uv`
- AWS CLI v2 configured with credentials that can access the snapshot bucket

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
4. Downloads files under `./downloads`
5. Saves raw JSON for the target index to `./index.json`

## Optional flags

- `--download-dir` to choose a different download directory
- `--output-file` to choose the output JSON file path

---

Use `cold-qa01` as an example: https://us-east-1.console.aws.amazwinlogbeat-unknown-2023.06.05-000001on.com/s3/buckets/cold-qa01?region=us-east-1&prefix=2022-Q1/&showversions=false

```
uv run main.py list s3://cold-qa01/2022-Q1/
uv run main.py download s3://cold-qa01/2022-Q1/ 'webroot-unknown-2023.06.14-000001'
```