"""
Loads the CSVs into the DynamoDB tables defined in config.py.

Assumes create_table.py has already been run.

SOURCE OF TRUTH IS THE REPO, NOT THE BUCKET. The CSVs in data/ are
version-controlled and reviewed; the S3 copy is a snapshot somebody
uploaded at some point. This script used to download from S3 first, which
silently overwrote the repo's data/ files with older bucket copies and
then loaded those — so edits committed to git never reached DynamoDB and
the working tree came back modified. Local is now the default.

Usage:
    python scripts/load_data.py                       # load from the repo's data/
    python scripts/load_data.py --from-s3             # pull from the bucket first (old behaviour)
    python scripts/load_data.py --push-to-s3          # load locally, then refresh the bucket
    python scripts/load_data.py --only personas.csv   # just one file, useful while debugging
"""

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from config import AWS_REGION, S3_BUCKET_NAME, S3_RAW_PREFIX, CSV_TO_TABLE, table_name

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_DATA_DIR = PROJECT_ROOT / "data"
PROCESSED_DIR = PROJECT_ROOT / "processed"

s3 = boto3.client("s3", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def resolve_csv(csv_filename: str, from_s3: bool) -> Path:
    """Return the path to load. Defaults to the repo copy; only reaches for
    S3 when explicitly asked, and even then won't clobber a local file that
    the bucket doesn't have."""
    LOCAL_DATA_DIR.mkdir(exist_ok=True)
    local_path = LOCAL_DATA_DIR / csv_filename

    if not from_s3:
        if not local_path.exists():
            raise FileNotFoundError(
                f"{local_path} is missing. Either restore it (git checkout data/) "
                f"or run with --from-s3 to pull it from the bucket."
            )
        print(f"  reading {local_path.relative_to(PROJECT_ROOT)}")
        return local_path

    key = f"{S3_RAW_PREFIX}{csv_filename}"
    try:
        print(f"  downloading s3://{S3_BUCKET_NAME}/{key} -> {local_path}")
        s3.download_file(S3_BUCKET_NAME, key, str(local_path))
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey") or not local_path.exists():
            raise
        print(f"  not in S3 — using the local copy at {local_path.relative_to(PROJECT_ROOT)}")
    return local_path


def upload_to_s3(csv_filename: str):
    """Refresh the bucket from the repo, so the S3 snapshot stops drifting."""
    key = f"{S3_RAW_PREFIX}{csv_filename}"
    s3.upload_file(str(LOCAL_DATA_DIR / csv_filename), S3_BUCKET_NAME, key)
    print(f"  uploaded -> s3://{S3_BUCKET_NAME}/{key}")


def to_dynamo_safe(value):
    """
    Recursively converts a Python object into something DynamoDB's boto3
    resource API will accept: floats -> Decimal, NaN -> None.

    Trick: round-trip through JSON with parse_float=Decimal. This handles
    arbitrarily nested lists/dicts (e.g. the parsed JSON columns) in one shot.
    """
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def parse_row(row: dict, json_columns: list[str]) -> dict:
    """Cleans one CSV row: parses JSON-string columns, drops NaN -> None, casts numerics."""
    clean = {}
    for key, value in row.items():
        if pd.isna(value):
            clean[key] = None
            continue

        if key in json_columns and isinstance(value, str):
            try:
                clean[key] = json.loads(value)
            except json.JSONDecodeError:
                # not actually valid JSON for this row — keep the raw string
                # rather than silently dropping data
                clean[key] = value
        else:
            clean[key] = value

    return to_dynamo_safe(clean)


def load_csv_into_table(csv_filename: str, table_short_name: str, json_columns: list[str],
                        from_s3: bool = False, push_to_s3: bool = False):
    local_path = resolve_csv(csv_filename, from_s3)
    df = pd.read_csv(local_path)

    full_table_name = table_name(table_short_name)
    table = dynamodb.Table(full_table_name)

    print(f"  loading {len(df)} rows from {csv_filename} into {full_table_name}")
    with table.batch_writer() as batch:
        for _, row in df.iterrows():
            item = parse_row(row.to_dict(), json_columns)
            batch.put_item(Item=item)

    if push_to_s3:
        upload_to_s3(csv_filename)

    return len(df)


def main(only: str | None = None, from_s3: bool = False, push_to_s3: bool = False):
    PROCESSED_DIR.mkdir(exist_ok=True)
    summary = {}

    print(f"Source: {'S3 bucket' if from_s3 else 'the repo (data/)'}")
    for csv_filename, spec in CSV_TO_TABLE.items():
        if only and csv_filename != only:
            continue
        print(f"\n{csv_filename} -> {spec['table']}")
        count = load_csv_into_table(csv_filename, spec["table"], spec["json_columns"],
                                    from_s3=from_s3, push_to_s3=push_to_s3)
        summary[csv_filename] = count

    manifest_path = PROCESSED_DIR / "load_summary.json"
    manifest_path.write_text(json.dumps(summary, indent=2))
    print(f"\nDone. Row counts written to {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Load just one CSV filename, e.g. personas.csv")
    parser.add_argument("--from-s3", action="store_true",
                        help="pull the CSVs from the bucket first (overwrites your local data/)")
    parser.add_argument("--push-to-s3", action="store_true",
                        help="after loading, refresh the bucket from the repo")
    args = parser.parse_args()
    main(only=args.only, from_s3=args.from_s3, push_to_s3=args.push_to_s3)