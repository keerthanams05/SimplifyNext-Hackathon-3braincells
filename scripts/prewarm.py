"""
Warm the pipeline cache before a demo.

Running the full chain live takes ~20 seconds and six Bedrock calls. That's
fine once, but a demo clicks the same story several times, and a judge
watching a spinner for twenty seconds is twenty seconds you don't get back.

This runs the pipeline ahead of time for the pairs you'll actually show, so
those clicks come back instantly. Anything you *don't* warm still runs live
— and the app has a "Run it live" button — so you can still demonstrate the
agents genuinely working when someone asks.

    python scripts/prewarm.py                    # first alert for every persona
    python scripts/prewarm.py --user USER004     # just Priya
    python scripts/prewarm.py --pairs USER004:SIG016 USER001:SIG002
    python scripts/prewarm.py --list             # what's cached now
    python scripts/prewarm.py --clear            # empty the cache

This SPENDS MONEY — one full pipeline run per pair, on the shared Bedrock
budget. It prints the plan and asks before running unless you pass --yes.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

from config import AWS_REGION, table_name  # noqa: E402
from pipeline import run_full_pipeline, cache_key  # noqa: E402

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)


def scan_all(short_table):
    table = dynamodb.Table(table_name(short_table))
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def default_pairs(only_user=None, per_user=1):
    """The first N signals that actually match each persona's role — i.e.
    exactly what they'd see at the top of their alerts list, which is what
    a demo clicks first."""
    users = scan_all("users")
    signals = scan_all("signals")
    pairs = []
    for u in sorted(users, key=lambda x: x["user_id"]):
        if only_user and u["user_id"] != only_user:
            continue
        matching = [s for s in signals if u.get("occupation") in (s.get("affected_roles") or [])]
        matching.sort(key=lambda s: s.get("signal_id", ""))
        for s in matching[:per_user]:
            pairs.append((u["user_id"], s["signal_id"], u.get("name"), s.get("title", "")[:52]))
    return pairs


def list_cache():
    try:
        items = scan_all("pipeline_cache")
    except ClientError as e:
        print(f"Can't read the cache: {e.response['Error']['Code']}")
        print("Has the table been created? python scripts/create_table.py")
        return 1
    if not items:
        print("Cache is empty — every click will run live (~20s each).")
        return 0
    print(f"{len(items)} cached run(s):")
    for i in sorted(items, key=lambda x: x["cache_key"]):
        print(f"  {i['cache_key']:34} warmed {i.get('cached_at', '?')}")
    return 0


def clear_cache():
    try:
        items = scan_all("pipeline_cache")
    except ClientError as e:
        print(f"Can't read the cache: {e.response['Error']['Code']}")
        return 1
    if not items:
        print("Cache is already empty.")
        return 0
    table = dynamodb.Table(table_name("pipeline_cache"))
    with table.batch_writer() as batch:
        for i in items:
            batch.delete_item(Key={"cache_key": i["cache_key"]})
    print(f"Cleared {len(items)} cached run(s).")
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", help="only this persona, e.g. USER004")
    parser.add_argument("--pairs", nargs="+", metavar="USER:SIGNAL",
                        help="exact pairs to warm, e.g. USER004:SIG016")
    parser.add_argument("--per-user", type=int, default=1,
                        help="how many of each persona's alerts to warm (default 1)")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--list", action="store_true", help="show what's cached and exit")
    parser.add_argument("--clear", action="store_true", help="empty the cache and exit")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    if args.list:
        return list_cache()
    if args.clear:
        return clear_cache()

    if args.pairs:
        pairs = []
        for raw in args.pairs:
            if ":" not in raw:
                print(f"'{raw}' should look like USER004:SIG016")
                return 1
            user_id, signal_id = raw.split(":", 1)
            pairs.append((user_id, signal_id, user_id, ""))
    else:
        pairs = default_pairs(args.user, args.per_user)

    if not pairs:
        print("Nothing to warm. Is the data loaded? python scripts/load_data.py")
        return 1

    print(f"About to run the full pipeline for {len(pairs)} pair(s) on the "
          f"{args.model} tier.\nThis calls Bedrock and spends real money:\n")
    for user_id, signal_id, name, title in pairs:
        print(f"  {user_id} / {signal_id}  {name}{' — ' + title if title else ''}")
    print(f"\nRoughly {len(pairs) * 20}s and {len(pairs) * 6} model calls in total.")

    if not args.yes:
        if input("\nGo ahead? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing run.")
            return 0

    print()
    warmed, failed = 0, []
    for user_id, signal_id, name, _ in pairs:
        started = time.perf_counter()
        try:
            # use_cache=True writes the result; a pair that's already warm
            # returns instantly and costs nothing, so re-running is cheap.
            result = run_full_pipeline(user_id, signal_id, model_key=args.model, use_cache=True)
            elapsed = time.perf_counter() - started
            if result.get("cached"):
                print(f"  {user_id} / {signal_id}  already warm (skipped)")
            else:
                print(f"  {user_id} / {signal_id}  warmed in {elapsed:.1f}s")
                warmed += 1
        except Exception as e:
            print(f"  {user_id} / {signal_id}  FAILED: {type(e).__name__}: {e}")
            failed.append(f"{user_id}/{signal_id}")

    print(f"\n{warmed} newly warmed, {len(pairs) - warmed - len(failed)} already cached, "
          f"{len(failed)} failed{': ' + ', '.join(failed) if failed else ''}")
    if warmed or not failed:
        print("Those clicks are now instant. Anything else still runs live.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
