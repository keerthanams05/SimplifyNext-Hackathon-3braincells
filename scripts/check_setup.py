"""
Pre-demo check: is everything actually wired up?

Runs in seconds and spends nothing — it makes no Bedrock calls. Checks the
things that have actually broken on us: tables missing, tables empty, data
that doesn't cross-reference, a region mismatch between DynamoDB and the
CSVs you think you loaded.

    python scripts/check_setup.py

Exit code 0 if everything's ready, 1 if something needs fixing. Every
failure line says what to run.
"""

import csv
import json
import sys
from pathlib import Path

import boto3
# ProfileNotFound and NoCredentialsError are BotoCoreError, not ClientError —
# catching only ClientError meant "you haven't logged in yet", the single most
# likely state on a fresh laptop, came out as a raw traceback.
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import AWS_REGION, BEDROCK_REGION, CSV_TO_TABLE, TABLE_SCHEMA, table_name

DATA = Path(__file__).resolve().parent.parent / "data"

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
problems = []


def fail(msg, fix):
    problems.append((msg, fix))
    print(f"{BAD} {msg}")
    print(f"         fix: {fix}")


def local_rows(filename):
    with open(DATA / filename, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def check_local_data():
    print("\nLocal CSVs")
    total = 0
    for filename in CSV_TO_TABLE:
        path = DATA / filename
        if not path.exists():
            fail(f"{filename} is missing", "git checkout -- data/")
            continue
        try:
            rows = local_rows(filename)
        except Exception as e:
            fail(f"{filename} won't parse: {e}", "check for an unquoted comma in a text field")
            continue
        total += len(rows)
        header = len(rows[0]) if rows else 0
        ragged = [i for i, r in enumerate(rows, 2) if len(r) != header]
        if ragged:
            fail(f"{filename} has ragged rows at line(s) {ragged[:3]}", "quote any field containing a comma")
    print(f"{OK} {len(CSV_TO_TABLE)} CSVs parse, {total} rows total")

    # cross-references
    resources = {r["resource_id"] for r in local_rows("resources.csv")}
    dangling = {i for p in local_rows("plans_30_60_90.csv")
                for i in p["resource_ids"].split(",") if i and i not in resources}
    if dangling:
        fail(f"plans reference resources that don't exist: {sorted(dangling)}", "fix resource_ids in plans_30_60_90.csv")
    else:
        print(f"{OK} every plan's resource_ids resolve")

    users = {u["user_id"] for u in local_rows("personas.csv")}
    occupations = {u["occupation"] for u in local_rows("personas.csv")}
    signals = local_rows("signals.csv")
    for occ in sorted(occupations):
        n = sum(1 for s in signals if occ in json.loads(s["affected_roles"]))
        if n == 0:
            fail(f"'{occ}' matches no signals — that persona's dashboard will be empty",
                 "add the role to affected_roles on the signals for its sector")
    print(f"{OK} all {len(users)} personas have signals that match their role")


def check_aws():
    print(f"\nAWS (region {AWS_REGION}, Bedrock in {BEDROCK_REGION})")
    try:
        who = boto3.client("sts", region_name=AWS_REGION).get_caller_identity()
        print(f"{OK} signed in as {who['Arn'].split('/')[-1]} (account {who['Account']})")
    except ProfileNotFound as e:
        fail(f"{e}",
             "the AWS CLI isn't set up yet. Install it "
             "(https://awscli.amazonaws.com/AWSCLIV2.msi on Windows), open a NEW "
             "terminal, then: aws configure sso")
        return
    except (BotoCoreError, ClientError) as e:
        fail(f"no working AWS credentials: {type(e).__name__}",
             "aws sso login --profile hack2026   (PowerShell: $env:AWS_PROFILE=\"hack2026\")")
        return

    dynamodb = boto3.client("dynamodb", region_name=AWS_REGION)
    try:
        existing = set(dynamodb.list_tables()["TableNames"])
    except (BotoCoreError, ClientError) as e:
        fail(f"can't list DynamoDB tables: {e}", "check your IAM permissions and the region")
        return

    wanted = {table_name(t) for t in TABLE_SCHEMA}
    missing = wanted - existing
    if missing:
        fail(f"{len(missing)} table(s) missing: {sorted(missing)}", "python scripts/create_table.py")
    else:
        print(f"{OK} all {len(wanted)} tables exist")

    print("\nRow counts (DynamoDB vs the repo)")
    resource = boto3.resource("dynamodb", region_name=AWS_REGION)
    for filename, spec in CSV_TO_TABLE.items():
        name = table_name(spec["table"])
        if name not in existing:
            continue
        try:
            live = resource.Table(name).scan(Select="COUNT")["Count"]
        except (BotoCoreError, ClientError) as e:
            fail(f"can't scan {name}: {e}", "check IAM permissions")
            continue
        expected = len(local_rows(filename))
        if live == 0:
            fail(f"{name} is EMPTY (repo has {expected})", "python scripts/load_data.py")
        elif live < expected:
            fail(f"{name} has {live} rows, repo has {expected} — stale data",
                 "python scripts/load_data.py  (loads from the repo, not S3)")
        else:
            print(f"{OK} {name:34} {live:3} rows")


def main():
    print("CareerGuardian setup check")
    check_local_data()
    try:
        check_aws()
    except Exception as e:
        # Whatever goes wrong talking to AWS, this script's job is to explain
        # it — never to add a traceback on top of the problem.
        fail(f"unexpected AWS error: {type(e).__name__}: {e}",
             "check the AWS CLI is installed and you've run: aws sso login --profile hack2026")

    print()
    if problems:
        print(f"{len(problems)} thing(s) need fixing before the demo:")
        for msg, fix in problems:
            print(f"  - {msg}\n      {fix}")
        return 1
    print("Everything checks out. Start the app with:")
    print("  uvicorn api.main:app --reload --port 8000")
    print("  python -m http.server 8080 --directory frontend")
    return 0


if __name__ == "__main__":
    sys.exit(main())
