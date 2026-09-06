"""
Progress Agent — tracks plan completion and decides whether to re-plan.

Takes a user_id. Fetches that person's progress records, computes their
overall completion percentage directly (no need for a model to do
arithmetic), and asks a Bedrock model to judge whether progress has
stalled badly enough to warrant triggering the Planner Agent again:

    {
      "user_id": "USER001",
      "overall_completion_pct": 62,
      "stalled_tasks": [...],
      "replan_needed": true,
      "reason": "..."
    }

This is the "replanning_trigger" tier in scripts/config.py's
AGENT_MODEL_TIER — a narrow yes/no decision against known criteria, so it
defaults to the cheaper Nova Micro model rather than Claude.

Usage:
    python agents/progress_agent.py USER001
    python agents/progress_agent.py USER001 --model claude
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from config import AWS_REGION, BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID, table_name

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)


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


def overall_completion_pct(progress_items: list[dict]) -> float:
    if not progress_items:
        return 0.0
    total = sum(p.get("completion_pct", 0) or 0 for p in progress_items)
    return round(total / len(progress_items), 1)


def build_system_prompt() -> str:
    return f"""You are the Progress Agent (replanning trigger) in a
career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access — never reject or downgrade something just because you
don't personally recognize it or can't verify it independently.

You will be given ONE person's progress across their current 30/60/90-day
plan tasks: each task's status and completion percentage, plus the
pre-computed overall completion percentage. Decide whether progress has
stalled badly enough that the plan should be regenerated (e.g. multiple
tasks stuck at "Not Started" or well behind where they should be), versus
progress that is simply normal/on track.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "user_id": "<given user_id>",
  "overall_completion_pct": <the given overall completion percentage, unchanged>,
  "stalled_tasks": [<task strings that look stalled or at risk>],
  "replan_needed": true or false,
  "reason": "<one or two sentences explaining the decision>"
}}"""


def build_user_prompt(user_id: str, progress_items: list[dict], overall_pct: float) -> str:
    payload = {
        "user_id": user_id,
        "overall_completion_pct": overall_pct,
        "tasks": [
            {
                "task": p.get("task"),
                "status": p.get("status"),
                "completion_pct": p.get("completion_pct"),
            }
            for p in progress_items
        ],
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
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def run_progress_agent(user_id: str, model_key: str = "nova") -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    progress_items = query_by_user("progress", user_id)
    if not progress_items:
        raise ValueError(f"No progress records found for user_id={user_id}")

    overall_pct = overall_completion_pct(progress_items)

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(user_id, progress_items, overall_pct)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    result["user_id"] = user_id
    result["overall_completion_pct"] = overall_pct

    print(f"\nProgress verdict for {user_id} (model: {model_id}):")
    print(json.dumps(result, indent=2))

    if result.get("replan_needed"):
        print(
            "\n  -> replan_needed: True. Trigger the Planner Agent again for this user "
            "(agents/planner_agent.py) with a fresh signal, or against their current "
            "skill_gaps directly."
        )

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("--model", choices=["claude", "nova"], default="nova")
    args = parser.parse_args()

    run_progress_agent(args.user_id, model_key=args.model)
