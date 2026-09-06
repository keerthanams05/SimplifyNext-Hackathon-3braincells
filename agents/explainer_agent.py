"""
Explainer Agent — turns the pipeline's structured JSON into plain language
a person would actually want to read.

Every other agent produces data for machines: IDs, scores, priorities.
This one is the only agent whose audience is the user. It takes a full
pipeline result and writes the human-facing copy the app shows:

    {
      "user_id": "USER001",
      "signal_id": "SIG001",
      "headline": "Coding agents are changing how you write and test code",
      "summary": "...",
      "what_changed": ["...", "...", "..."],
      "first_step": "...",
      "reassurance": "..."
    }

Framing guardrail (on top of the usual date grounding): the README is
explicit that this product does NOT tell people their job is at risk. It
tells them which TASKS are changing. The system prompt below enforces
that, and bans probability-of-unemployment language outright — that
framing is the single easiest way for this demo to come across badly.

Usage:
    python agents/explainer_agent.py USER001 SIG001
    python agents/explainer_agent.py USER001 SIG001 --model nova
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import BEDROCK_REGION, CLAUDE_MODEL_ID, NOVA_MICRO_MODEL_ID
from aws_clients import dynamodb, bedrock  # thread-safe shared handles
from agent_errors import AgentOutputError, MissingDataError  # noqa: E402

SYSTEM_PROMPT = f"""You are the Explainer Agent in a career-guidance system for
Singapore workers. You are the only agent in this system whose output is read
by the person themselves, so you write for them, not for a developer.

Today's real date is {date.today().isoformat()}. You will see signals and
dates from after your training cutoff — that is expected, not a sign of
fabrication.

You will be given the system's full structured analysis for ONE person and
ONE disruption signal: the validated signal, which of their tasks and skill
gaps it affects, their 30/60/90-day plan, and adjacent career options.
Rewrite it as plain, calm, specific language.

HOW TO FRAME IT — this matters more than anything else here:
- Talk about TASKS changing, never about their job being at risk. Never
  state or imply a probability of unemployment, redundancy, or being
  replaced. That framing is banned.
- Be specific to this person's actual tasks and named programmes. Vague
  encouragement ("upskilling is important!") is worse than saying nothing.
- Write like a well-informed friend who works in their industry: direct,
  warm, no hype, no corporate filler, no exclamation marks.
- Use Singapore context naturally where the data supports it (SkillsFuture
  Credit, WSG programmes) — but never invent funding details that aren't
  in the data you were given.
- Short sentences. No jargon the person wouldn't use themselves. Say
  "AI tools that write code" rather than "agentic SDLC transformation".

If the analysis shows this signal does NOT affect this person's tasks, say
that plainly and briefly — do not manufacture concern to fill space.

Respond with ONLY a JSON object, no other text, no markdown fences, in this
exact shape:
{{
  "headline": "<max 9 words, what is changing for them, in their own vocabulary>",
  "summary": "<2-3 sentences: what the signal is, and what it means for their day-to-day work specifically>",
  "what_changed": [<2-4 short strings, each naming one specific task of theirs and how it shifts>],
  "first_step": "<one concrete action for this week, referencing a named programme from the plan if there is one>",
  "reassurance": "<one sentence, honest not saccharine, on what they already have going for them — reference their real existing skills>"
}}"""


def call_model(system_prompt: str, user_prompt: str, model_id: str) -> str:
    response = bedrock.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 700, "temperature": 0.4},
    )
    return response["output"]["message"]["content"][0]["text"]


def parse_agent_json(raw_text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise AgentOutputError(f"Model did not return valid JSON.\nRaw output:\n{raw_text}") from e


def build_user_prompt(pipeline_result: dict) -> str:
    """Trim the pipeline result to what actually informs the copy — the
    Explainer doesn't need IDs, scores it won't mention, or the full
    resource catalogue."""
    signal = pipeline_result.get("signal") or {}
    role = pipeline_result.get("role_intelligence") or {}
    plan = pipeline_result.get("plan") or {}
    pathfinder = pipeline_result.get("pathfinder") or {}

    payload = {
        "signal": {
            "title": signal.get("title"),
            "sector": signal.get("sector"),
            "technology": signal.get("technology"),
            "severity": signal.get("severity"),
            "why_it_was_validated": signal.get("reason"),
        },
        "their_affected_tasks": role.get("affected_tasks", []),
        "their_skill_gaps": role.get("skill_gaps", []),
        "analysis_explanation": role.get("explanation"),
        "their_plan": {
            "phases": [
                {
                    "phase": p.get("phase"),
                    "goal": p.get("goal"),
                    "tasks": p.get("tasks"),
                    "hours_per_week": p.get("hours_per_week"),
                    "programmes": [r.get("name") for r in p.get("resources", [])],
                }
                for p in plan.get("phases", [])
            ],
            "no_plan_reason": plan.get("no_plan_reason"),
        },
        "adjacent_roles": [
            {
                "target_role": o.get("target_role"),
                "similarity_score": o.get("similarity_score"),
                "transferable_skills": o.get("transferable_skills"),
            }
            for o in pathfinder.get("pathfinder_options", [])
        ],
    }
    return json.dumps(payload, indent=2)


def run_explainer_agent(
    user_id: str,
    signal_id: str,
    pipeline_result: dict = None,
    model_key: str = "claude",
    verbose: bool = True,
) -> dict:
    model_id = CLAUDE_MODEL_ID if model_key == "claude" else NOVA_MICRO_MODEL_ID

    # Normally the pipeline hands us its own result. Run standalone (e.g.
    # from the CLI), we run the pipeline ourselves first.
    if pipeline_result is None:
        from pipeline import run_full_pipeline

        pipeline_result = run_full_pipeline(user_id, signal_id, model_key=model_key, explain=False)

    user_prompt = build_user_prompt(pipeline_result)
    raw_output = call_model(SYSTEM_PROMPT, user_prompt, model_id)
    result = parse_agent_json(raw_output)

    result["user_id"] = user_id
    result["signal_id"] = signal_id

    if verbose:
        print(f"\nExplainer copy for {user_id} / {signal_id} (model: {model_id}):")
        print(json.dumps(result, indent=2))

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", help="e.g. USER001")
    parser.add_argument("signal_id", help="e.g. SIG001")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    args = parser.parse_args()

    run_explainer_agent(args.user_id, args.signal_id, model_key=args.model)
