"""
Downloads the CSVs from S3, parses them, and loads them into the
DynamoDB tables defined in config.py.

Assumes create_tables.py has already been run.

Usage:
    python scripts/load_data.py
    python scripts/load_data.py --only personas.csv   # load just one file, useful while debugging
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


def download_from_s3(csv_filename: str) -> Path:
    """Downloads one CSV from S3 into the local data/ folder and returns its
    path. Falls back to the copy already in data/ if S3 doesn't have it —
    otherwise adding a new CSV to the repo means nobody can load it until
    someone remembers to re-upload the bucket."""
    LOCAL_DATA_DIR.mkdir(exist_ok=True)
    local_path = LOCAL_DATA_DIR / csv_filename
    key = f"{S3_RAW_PREFIX}{csv_filename}"
    try:
        print(f"  downloading s3://{S3_BUCKET_NAME}/{key} -> {local_path}")
        s3.download_file(S3_BUCKET_NAME, key, str(local_path))
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchKey") or not local_path.exists():
            raise
        print(f"  not in S3 yet — using the local copy at {local_path}")
    return local_path


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


def load_csv_into_table(csv_filename: str, table_short_name: str, json_columns: list[str]):
    local_path = download_from_s3(csv_filename)
    df = pd.read_csv(local_path)

    full_table_name = table_name(table_short_name)
    table = dynamodb.Table(full_table_name)

    print(f"  loading {len(df)} rows from {csv_filename} into {full_table_name}")
    with table.batch_writer() as batch:
        for _, row in df.iterrows():
            item = parse_row(row.to_dict(), json_columns)
            batch.put_item(Item=item)

    return len(df)


def main(only: str | None = None):
    PROCESSED_DIR.mkdir(exist_ok=True)
    summary = {}

    for csv_filename, spec in CSV_TO_TABLE.items():
        if only and csv_filename != only:
            continue
        print(f"\n{csv_filename} -> {spec['table']}")
        count = load_csv_into_table(csv_filename, spec["table"], spec["json_columns"])
        summary[csv_filename] = count

    manifest_path = PROCESSED_DIR / "load_summary.json"
    manifest_path.write_text(json.dumps(summary, indent=2))
    print(f"\nDone. Row counts written to {manifest_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="Load just one CSV filename, e.g. personas.csv")
    args = parser.parse_args()
    main(only=args.only)