"""
Pathfinder Agent — alternate career-path recommendations.

Takes a user_id + target_role. Fetches that persona's own current skills
(plus occupation/career-goal context), and asks a Bedrock model how well
their existing skills transfer into the target role — matching the shape
of data/pathfinder.csv:

    {
      "user_id": "USER001",
      "target_role": "AI Application Engineer",
      "similarity_score": 82,
      "transferable_skills": "...",
      "gap_to_role": "...",
      "reason": "..."
    }

Same guardrail as the other agents: grounded with today's real date and
told not to fact-check via memory. There's no ID allow-list here (the
output is free text, not IDs), so no validate_* step is needed.

Usage:
    python agents/pathfinder_agent.py USER001 "AI Application Engineer"
    python agents/pathfinder_agent.py USER001 "AI Application Engineer" --model nova
    python agents/pathfinder_agent.py USER001 "AI Application Engineer" --save
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


def get_item(short_table, key):
    table = dynamodb.Table(table_name(short_table))
    item = table.get_item(Key=key).get("Item")
    return decimal_to_native(item) if item else None


def query_by_user(short_table, user_id):
    table = dynamodb.Table(table_name(short_table))
    items = table.query(KeyConditionExpression=Key("user_id").eq(user_id)).get("Items", [])
    return decimal_to_native(items)


def build_system_prompt() -> str:
    return f"""You are the Pathfinder Agent in a career-disruption-monitoring system.

Today's real date is {date.today().isoformat()}. You do not have real-time
internet access and your training data has a cutoff before today, so you
may see roles, skills, or context from after that cutoff — that is
expected, not a sign of fabrication. Never reject or downgrade something
just because you don't personally recognize it or can't verify it
independently.

You will be given ONE person's profile (occupation, career goal, current
skills) and ONE target role they are considering moving into. Judge how
well their existing skills transfer into that target role.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "user_id": "<given user_id>",
  "target_role": "<given target_role>",
  "similarity_score": <integer 0-100, how strongly this person's current skills/experience transfer to the target role>,
  "transferable_skills": "<comma-separated list of this person's current skills that transfer well>",
  "gap_to_role": "<comma-separated list of skills/areas they'd still need to close the gap>",
  "reason": "<one or two sentences explaining the judgment>"
}}"""


def build_user_prompt(persona: dict, skills: list[dict], target_role: str) -> str:
    payload = {
        "target_role": target_role,
        "persona": {
            "occupation": persona.get("occupation"),
            "industry": persona.get("industry"),
            "years_experience": persona.get("years_experience"),
            "career_goal": persona.get("career_goal"),
            "preferred_direction": persona.get("preferred_direction"),
            "willing_to_change_career": persona.get("willing_to_change_career"),
            "alternative_career_interest": persona.get("alternative_career_interest"),
        },
        "current_skills": [
            {
                "skill": s.get("skill"),
                "role": s.get("role"),
                "proficiency": s.get("proficiency"),
            }
            for s in skills
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


def save_result(user_id: str, target_role: str, result: dict):
    table = dynamodb.Table(table_name("pathfinder_results"))
    table.put_item(Item={"user_id": user_id, "target_role": target_role, **result})


def run_pathfinder_agent(user_id: str, target_role: str, model_key: str = "claude", save: bool = False) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    persona = get_item("users", {"user_id": user_id})
    if not persona:
        raise ValueError(f"No persona found: {user_id}")

    skills = query_by_user("user_skills", user_id)
    if not skills:
        raise ValueError(f"No user_skills found for user_id={user_id}")

    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(persona, skills, target_role)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    result["user_id"] = user_id
    result["target_role"] = target_role

    print(f"\nPathfinder verdict for {user_id} -> {target_role} (model: {model_id}):")
    print(json.dumps(result, indent=2))

    reference = get_item("pathfinder_results", {"user_id": user_id, "target_role": target_role})
    if reference:
        print("\n(dataset reference — NOT shown to the agent, for your own sanity-check only)")
        print(json.dumps(reference, indent=2))

    if save:
        save_result(user_id, target_role, result)
        print(f"\nSaved pathfinder result for {user_id} / {target_role} to DynamoDB")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("target_role", help='e.g. "AI Application Engineer"')
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--save", action="store_true", help="write the result back to DynamoDB")
    args = parser.parse_args()

    run_pathfinder_agent(args.user_id, args.target_role, model_key=args.model, save=args.save)
