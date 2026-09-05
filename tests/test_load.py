"""
Quick sanity check — not a full test suite, just enough to confirm the
load worked before anyone builds agent logic on top of it.

Usage:
    python tests/test_load.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import boto3
from config import AWS_REGION, table_name

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)

SARAH_ID = "USER001"


def check(short_table_name: str, key: dict, description: str):
    table = dynamodb.Table(table_name(short_table_name))
    response = table.get_item(Key=key)
    item = response.get("Item")
    status = "OK" if item else "MISSING"
    print(f"[{status}] {description}")
    return item


def check_query(short_table_name: str, key_condition, description: str):
    table = dynamodb.Table(table_name(short_table_name))
    response = table.query(KeyConditionExpression=key_condition)
    count = response.get("Count", 0)
    status = "OK" if count > 0 else "MISSING"
    print(f"[{status}] {description} ({count} items)")
    return response.get("Items", [])


def main():
    from boto3.dynamodb.conditions import Key

    print("Checking Sarah's data made it into DynamoDB...\n")

    check("users", {"user_id": SARAH_ID}, "Sarah's persona record")
    check_query("user_skills", Key("user_id").eq(SARAH_ID), "Sarah's current skills")
    check_query("role_tasks", Key("user_id").eq(SARAH_ID), "Sarah's role tasks")
    check_query("skill_gaps", Key("user_id").eq(SARAH_ID), "Sarah's skill gaps")
    check_query("plans", Key("user_id").eq(SARAH_ID), "Sarah's 30/60/90 plan")
    check_query("pathfinder_results", Key("user_id").eq(SARAH_ID), "Sarah's pathfinder options")

    check("signals", {"signal_id": "SIG001"}, "Signal SIG001 (agentic coding disruption)")
    check("resources", {"resource_id": "RES001"}, "Resource RES001 (MySkillsFuture directory)")

    print("\nIf anything says MISSING, re-run load_data.py with --only <that_file>.csv and check for errors.")


if __name__ == "__main__":
    main()