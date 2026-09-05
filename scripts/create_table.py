"""
Creates all CareerGuardian DynamoDB tables in one go.

Run this once (safe to re-run — it skips tables that already exist).

Usage:
    python scripts/create_tables.py
"""

import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION, TABLE_SCHEMA, table_name

dynamodb = boto3.client("dynamodb", region_name=AWS_REGION)


def create_table(short_name: str, partition_key: str, sort_key: str | None):
    full_name = table_name(short_name)

    key_schema = [{"AttributeName": partition_key, "KeyType": "HASH"}]
    attr_defs = [{"AttributeName": partition_key, "AttributeType": "S"}]

    if sort_key:
        key_schema.append({"AttributeName": sort_key, "KeyType": "RANGE"})
        attr_defs.append({"AttributeName": sort_key, "AttributeType": "S"})

    try:
        dynamodb.create_table(
            TableName=full_name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",  # no need to guess capacity for a hackathon
        )
        print(f"Creating {full_name} ... waiting for it to become active")
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=full_name)
        print(f"  {full_name} is active")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"  {full_name} already exists, skipping")
        else:
            raise


def main():
    for short_name, (pk, sk) in TABLE_SCHEMA.items():
        create_table(short_name, pk, sk)
    print("\nAll tables ready.")


if __name__ == "__main__":
    main()