"""
Prep Coach Agent — gets someone ready to actually apply.

Everything else in this system tells a person what's changing and what to
learn. This is the one that helps them go for the thing: it takes their
real tasks, skills and progress, plus a target role they've saved, and
produces application-ready material.

    {
      "user_id": "USER004",
      "target_role": "Finance Analyst",
      "readiness": {"score": 62, "verdict": "Nearly ready", "reason": "..."},
      "resume_bullets": [
        {"from_task": "Reconcile supplier statements",
         "bullet": "...", "why_it_works": "..."}
      ],
      "practice_questions": [
        {"question": "...", "what_theyre_checking": "...", "your_angle": "..."}
      ],
      "gaps_to_name": ["..."],
      "before_you_apply": ["..."]
    }

Two things it must not do, both enforced in the prompt:
  1. Never invent experience. Every résumé bullet must trace back to a
     task or skill that is actually on file for this person — the
     `from_task` field makes that auditable, and a bullet whose source
     doesn't match their real data is flagged by validate_bullets().
  2. Never coach someone to hide a gap. It names gaps honestly and gives
     them language for talking about what they're learning instead.

Usage:
    python agents/prep_agent.py USER004 "Finance Analyst"
    python agents/prep_agent.py USER004            # uses their top saved role
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


def build_system_prompt(valid_tasks: list[str], target_role: str) -> str:
    return f"""You are the Prep Coach in a career-guidance system for Singapore
workers. Your job is to get this person ready to apply for: {target_role}.

Today's real date is {date.today().isoformat()}. You will see roles, tools
and dates from after your training cutoff — that is expected, not a sign of
fabrication.

You will be given their real tasks, skills, skill gaps, learning progress
and the fit analysis for this target role. Produce material they could use
this week.

ABSOLUTE RULES:
- Never invent experience. Every résumé bullet must be a rewrite of one of
  THEIR actual tasks, listed here: {valid_tasks}. Put the exact task text
  you used in "from_task". If you can't ground a bullet in a real task,
  write fewer bullets.
- Never coach them to hide a gap. If they're missing something the role
  needs, name it and give them honest language for what they're doing
  about it. "I'm three weeks into a BI course and here's what I built" is
  a real answer; pretending is not.
- No inflated verbs for work that wasn't that. Rewriting "processed
  invoices" as "spearheaded financial transformation" is a failure.
- Singapore context: plain professional English, no US-style hype.

For résumé bullets: lead with what they did, make the scale or outcome
concrete where their data supports it, and connect it to what the target
role needs. For practice questions: the questions someone actually gets
asked moving from their current role into this one, including the awkward
one about why they're switching.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{
  "readiness": {{
    "score": <integer 0-100, how ready they are to apply today>,
    "verdict": "<one of: Not yet / Getting there / Nearly ready / Ready to apply>",
    "reason": "<one or two sentences, honest>"
  }},
  "resume_bullets": [
    {{
      "from_task": "<the exact task text from their list that this rewrites>",
      "bullet": "<the résumé line, one sentence>",
      "why_it_works": "<what the target role's hiring manager sees in it>"
    }}
  ],
  "practice_questions": [
    {{
      "question": "<a question they'd really be asked>",
      "what_theyre_checking": "<what the interviewer is actually testing>",
      "your_angle": "<how THIS person should approach it, using their real background>"
    }}
  ],
  "gaps_to_name": [<2-3 strings: gaps worth naming out loud, with the honest framing>],
  "before_you_apply": [<2-4 concrete things to do first, in order>]
}}"""


def build_user_prompt(persona, tasks, skills, gaps, progress, fit) -> str:
    payload = {
        "person": {
            "occupation": persona.get("occupation"),
            "years_experience": persona.get("years_experience"),
            "education": persona.get("education"),
            "employer_context": persona.get("employer_context"),
            "career_goal": persona.get("career_goal"),
        },
        "their_real_tasks": [
            {"task": t.get("task"), "skill_area": t.get("skill_area")} for t in tasks
        ],
        "their_skills": [
            {"skill": s.get("skill"), "proficiency": s.get("proficiency")} for s in skills
        ],
        "their_skill_gaps": [
            {"skill": g.get("skill"), "gap_priority": g.get("gap_priority")} for g in gaps
        ],
        "learning_so_far": [
            {"task": p.get("task"), "status": p.get("status"), "completion_pct": p.get("completion_pct")}
            for p in progress
        ],
        "fit_for_target_role": fit,
    }
    return json.dumps(payload, indent=2)


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 1400, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def validate_bullets(result: dict, valid_tasks: set):
    """A bullet that doesn't trace to a real task is invented experience —
    the one failure mode that could actually hurt someone in an interview,
    so it gets flagged loudly rather than silently passed on."""
    invented = [
        b.get("from_task")
        for b in result.get("resume_bullets", [])
        if b.get("from_task") not in valid_tasks
    ]
    if invented:
        print(f"  WARNING: résumé bullets not traceable to a real task: {invented}")


def run_prep_agent(user_id: str, target_role: str = None, model_key: str = "claude", verbose: bool = True) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    persona = get_item("users", {"user_id": user_id})
    if not persona:
        raise ValueError(f"No persona found: {user_id}")

    # Default to whatever they've said they're interested in, rather than
    # making the caller know the role name.
    if not target_role:
        saved = query_by_user("saved_roles", user_id)
        interested = [r for r in saved if r.get("status") == "interested"]
        if not interested:
            raise ValueError(f"No target role given and none saved as 'interested' for {user_id}")
        target_role = interested[0]["target_role"]

    tasks = query_by_user("role_tasks", user_id)
    skills = query_by_user("user_skills", user_id)
    gaps = query_by_user("skill_gaps", user_id)
    progress = query_by_user("progress", user_id)
    fit = get_item("pathfinder_results", {"user_id": user_id, "target_role": target_role})

    valid_tasks = [t["task"] for t in tasks]

    system_prompt = build_system_prompt(valid_tasks, target_role)
    user_prompt = build_user_prompt(persona, tasks, skills, gaps, progress, fit)

    raw_output = call_model(system_prompt, user_prompt, model_id)
    result = parse_agent_json(raw_output)
    validate_bullets(result, set(valid_tasks))

    result["user_id"] = user_id
    result["target_role"] = target_role

    if verbose:
        print(f"\nPrep Coach for {user_id} -> {target_role} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER004")
    parser.add_argument("target_role", nargs="?", default=None, help='e.g. "Finance Analyst"')
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    args = parser.parse_args()

    run_prep_agent(args.user_id, args.target_role, model_key=args.model)
