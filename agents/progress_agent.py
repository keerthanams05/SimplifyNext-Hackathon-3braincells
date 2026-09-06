"""
Progress Agent — tracks how a person is doing against their upskilling
plan, flags drift, and decides whether a re-plan is warranted.

Takes a user_id (and, if you have one, the signal_id of a specific
generated plan to check progress against). Fetches that person's
progress records from `data/progress.csv` / the `progress` table, and
optionally the saved plan for that signal (from `generated_plans`, if
`planner_agent.py --save` was run for that user_id/signal_id pair).
Asks a Bedrock model — deliberately the cheap/narrow tier, since this is
closer to a rules-style yes/no judgment than open-ended synthesis — to:

  1. Decide which progress items are meaningfully behind schedule.
  2. Decide whether the overall picture warrants triggering a re-plan.
  3. Explain why in plain language.

    {
      "user_id": "USER001",
      "signal_id": "SIG001" or null,
      "progress_items": [
        {"progress_id": "PROG002", "task": "...", "status": "...", "completion_pct": 50}
      ],
      "behind_items": [<progress_id strings, subset of progress_items>],
      "needs_replan": true or false,
      "explanation": "..."
    }

Same two guardrails as the other agents:
  1. Grounded with today's real date + told not to fact-check via memory.
  2. Explicitly told to ONLY use progress_id values that actually appear
     in the list provided — a validate_ids() check afterward flags it in
     a WARNING if the model invents one anyway.

Note: unlike the other three agents, this one does NOT call Bedrock at
all if there is no progress data on file for the user — there is nothing
to judge, so returning a Bedrock-authored guess would just be noise.

Usage:
    python agents/progress_agent.py USER001
    python agents/progress_agent.py USER001 --signal SIG001   # also loads that saved plan for context
    python agents/progress_agent.py USER001 --model claude    # override the default (nova) tier
"""

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import AWS_REGION, BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID, table_name
from aws_clients import dynamodb, bedrock  # thread-safe shared handles
from agent_errors import AgentOutputError, MissingDataError  # noqa: E402


def decimal_to_native(obj):
    if isinstance(obj, list):
        return [decimal_to_native(v) for v in obj]
    if isinstance(obj, dict):
        return {k: decimal_to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def query_by_user(short_table, user_id):
    table = dynamodb.Table(table_name(short_table))
    items = table.query(KeyConditionExpression=Key("user_id").eq(user_id)).get("Items", [])
    return decimal_to_native(items)


def get_saved_plan(user_id: str, signal_id: str) -> dict | None:
    """Best-effort lookup of a previously --save'd Planner Agent output.
    Returns None (rather than raising) if generated_plans doesn't exist
    yet or nothing was saved for this pair — plan context is optional."""
    plan_id = f"PLAN-{user_id}-{signal_id}"
    try:
        table = dynamodb.Table(table_name("generated_plans"))
        item = table.get_item(Key={"plan_id": plan_id}).get("Item")
        return decimal_to_native(item) if item else None
    except ClientError:
        return None


def build_system_prompt(valid_progress_ids: list[str]) -> str:
    return f"""You are the Progress Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your training data has a cutoff before today, so you
will see tasks, plans, and dates from after that cutoff — that is
expected, not a sign of fabrication. Never reject or downgrade something
just because you don't personally recognize it or can't verify it
independently.

You will be given ONE person's upskilling progress records (each with a
status and a completion percentage), and optionally the 30/60/90-day plan
those tasks are meant to track. Your job:

1. Decide which progress items are meaningfully BEHIND — i.e. their
   status/completion suggests they are lagging what you'd expect at this
   point, not just "not finished yet." A task explicitly marked
   "Behind Schedule" always counts. A task "In Progress" at low
   completion may or may not count depending on context — use judgment,
   don't just flag everything that isn't 100%.
2. Decide whether the overall picture (how many items are behind, how far
   behind, and whether earlier phases are incomplete going into later
   ones) warrants triggering a re-plan, versus just needing encouragement
   or a nudge to keep going.
3. Explain your reasoning in plain language.

CRITICAL: you may ONLY use progress_id values from this exact list:
{valid_progress_ids}. Do not invent new IDs. If nothing is behind, return
an empty list rather than making something up.

Respond with ONLY a JSON object, no other text, no markdown fences, in
this exact shape:
{{
  "behind_items": [<progress_id strings from the allowed list only>],
  "needs_replan": true or false,
  "explanation": "<two to three sentences explaining the judgment>"
}}"""


def build_user_prompt(progress_items: list[dict], plan: dict | None) -> str:
    payload = {
        "progress_items": [
            {
                "progress_id": p.get("progress_id"),
                "task": p.get("task"),
                "status": p.get("status"),
                "completion_pct": p.get("completion_pct"),
            }
            for p in progress_items
        ],
        "current_plan_for_context": plan,  # may be None — that's fine, the model should still judge from status/completion alone
    }
    return json.dumps(payload, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 400, "temperature": 0.2},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AgentOutputError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def validate_ids(result: dict, valid_progress_ids: set):
    bad = set(result.get("behind_items", [])) - valid_progress_ids
    if bad:
        print(f"  WARNING: model invented progress_ids not in the real data: {bad}")


def enrich_result(result: dict, user_id: str, signal_id: str | None, progress_items: list[dict]) -> dict:
    """Never trust the model to echo back values we already know — set
    them ourselves. Attach the full trusted progress records so a
    frontend can render everything without a second lookup."""
    result["user_id"] = user_id
    result["signal_id"] = signal_id
    result["progress_items"] = [
        {
            "progress_id": p.get("progress_id"),
            "task": p.get("task"),
            "status": p.get("status"),
            "completion_pct": p.get("completion_pct"),
        }
        for p in progress_items
    ]
    # behind_items comes back from the model as bare progress_id strings —
    # that's fine here (unlike the other agents) since the frontend can
    # cross-reference against progress_items above by progress_id.
    return result


def run_progress_agent(user_id: str, signal_id: str = None, model_key: str = "nova", verbose: bool = True) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    progress_items = query_by_user("progress", user_id)
    if not progress_items:
        result = {
            "user_id": user_id,
            "signal_id": signal_id,
            "progress_items": [],
            "behind_items": [],
            "needs_replan": False,
            "explanation": "No progress records are on file for this person yet.",
        }
        if verbose:
            print(f"\nProgress verdict for {user_id}: no progress data on file.")
            print(json.dumps(result, indent=2))
        return result

    plan = get_saved_plan(user_id, signal_id) if signal_id else None
    valid_progress_ids = [p["progress_id"] for p in progress_items]

    system_prompt = build_system_prompt(valid_progress_ids)
    user_prompt = build_user_prompt(progress_items, plan)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    validate_ids(result, set(valid_progress_ids))
    result = enrich_result(result, user_id, signal_id, progress_items)

    if verbose:
        print(f"\nProgress verdict for {user_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("--signal", dest="signal_id", default=None, help="optional signal_id, to load that saved plan for context")
    parser.add_argument("--model", choices=["claude", "nova"], default="nova")
    args = parser.parse_args()

    run_progress_agent(args.user_id, signal_id=args.signal_id, model_key=args.model)
