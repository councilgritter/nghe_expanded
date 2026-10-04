"""Move the reading module's build artifacts to and from object storage.

    python -m reading.tools.r2_sync get      # before an ingest run
    python -m reading.tools.r2_sync put      # after one

**Why this exists.**  Ingest's idempotency lives in ``reading/data/reading.sqlite3``
— "already ingested at B1" is a row in that file — and the file is gitignored.  A
fresh checkout therefore has an *empty* database, and a run there re-simplifies every
article in the feed and pays for all of them again.  The same is true of the 15 MB
dictionary: without it the reconciler loses its compound constraint and segmentation
quality drops silently.

So a scheduled or on-demand run has to bring both files with it.  The repository
already carries R2 credentials for the listening drill's audio, so the reading
database rides the same bucket under its own prefix.

Credentials come from the environment only:

    R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    READING_R2_PREFIX   (optional, default ``reading``)

``boto3`` is imported lazily so the rest of the tooling does not need it installed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from reading.settings import settings as default_settings

DEFAULT_PREFIX = "reading"


def _client():
    try:
        import boto3
    except ImportError:  # pragma: no cover - depends on the deployment environment
        raise SystemExit(
            "boto3 is not installed.  Install it with:\n    pip install boto3"
        )
    missing = [
        name
        for name in ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(
            "R2 is not configured; missing " + ", ".join(missing) + ".\n"
            "Set them in the environment (in CI they come from repository secrets)."
        )
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _prefix() -> str:
    return (os.environ.get("READING_R2_PREFIX") or DEFAULT_PREFIX).strip("/")


def _targets(db_path: Path, dict_path: Path) -> list[tuple[str, Path]]:
    return [(f"{_prefix()}/reading.sqlite3", db_path), (f"{_prefix()}/dictionary.sqlite3", dict_path)]


def get(db_path: Path, dict_path: Path, verbose: bool = True) -> dict[str, str]:
    """Download whatever the bucket holds.  Missing objects are reported, not fatal."""
    client = _client()
    bucket = os.environ["R2_BUCKET"]
    result: dict[str, str] = {}
    for key, path in _targets(db_path, dict_path):
        try:
            client.download_file(bucket, key, str(path))
            result[key] = f"downloaded -> {path}"
        except Exception as exc:  # noqa: BLE001 - a missing object is a normal first run
            result[key] = f"not fetched ({exc})"
        if verbose:
            print(f"  {key}: {result[key]}")
    return result


def put(db_path: Path, dict_path: Path, verbose: bool = True) -> dict[str, str]:
    """Upload the files that exist.  A missing file is skipped, never invented."""
    client = _client()
    bucket = os.environ["R2_BUCKET"]
    result: dict[str, str] = {}
    for key, path in _targets(db_path, dict_path):
        if not path.is_file():
            result[key] = f"missing, not uploaded ({path})"
        else:
            client.upload_file(str(path), bucket, key)
            result[key] = f"uploaded {path} ({path.stat().st_size / 1048576:.1f} MB)"
        if verbose:
            print(f"  {key}: {result[key]}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["get", "put"])
    parser.add_argument("--db", default=None, help="reading database path")
    parser.add_argument("--dict", dest="dict_path", default=None, help="dictionary path")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else default_settings.db_path
    dict_path = Path(args.dict_path) if args.dict_path else default_settings.dict_path
    if args.action == "get":
        get(db_path, dict_path)
    else:
        put(db_path, dict_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
