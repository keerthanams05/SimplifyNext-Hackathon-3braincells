"""
Pipeline orchestrator — runs the agents for one user_id + signal_id and
returns ONE consolidated result. This is what the API layer calls;
nothing outside this file should need to know there are six separate
agent scripts underneath.

Order, and why:
    Signal            does the evidence support the claim?
    Role Intelligence which of THIS person's tasks/gaps does it hit?
    Resource Connector which real programmes close those gaps? (cheap tier)
    Planner           sequence them into 30/60/90 days
    Pathfinder        which adjacent roles does this signal make relevant?
    Explainer         say all of the above in plain language (optional)

Usage:
    python agents/pipeline.py USER001 SIG001
    python agents/pipeline.py USER001 SIG001 --no-explain
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from signal_agent import run_signal_agent
from role_intelligence_agent import run_role_intelligence_agent
from resource_connector_agent import run_resource_connector_agent
from planner_agent import run_planner_agent
from pathfinder_agent import run_pathfinder_agent
from explainer_agent import run_explainer_agent


def run_full_pipeline(user_id: str, signal_id: str, model_key: str = "claude", explain: bool = True) -> dict:
    # verbose=False on every sub-agent call: each one would otherwise print
    # its own verdict, which is useful when running that agent standalone
    # but just duplicates output here — the pipeline prints ONE consolidated
    # result at the end instead. Hallucination WARNING prints (inside each
    # agent's validate_* function) are unconditional and still show up
    # regardless of verbose, since those matter even in pipeline mode.
    signal_verdict = run_signal_agent(signal_id, model_key=model_key, verbose=False)
    role_verdict = run_role_intelligence_agent(user_id, signal_id, model_key=model_key, verbose=False)

    # Match programmes to the gaps THIS signal raised (not every gap on
    # file), on the cheap tier, before the expensive planning call.
    resources = run_resource_connector_agent(
        user_id, gaps=role_verdict.get("skill_gaps"), verbose=False
    )

    # Pass the already-computed verdict through so the Planner doesn't
    # make a second, identical Bedrock call for the same thing, and hand
    # it the shortlist so it plans against pre-filtered programmes.
    plan = run_planner_agent(
        user_id, signal_id, model_key=model_key, verdict=role_verdict, verbose=False,
        shortlist_resource_ids=resources.get("shortlist_resource_ids"),
    )
    pathfinder = run_pathfinder_agent(user_id, signal_id, model_key=model_key, verbose=False)

    result = {
        "user_id": user_id,
        "signal_id": signal_id,
        "signal": signal_verdict,
        "role_intelligence": role_verdict,
        "resources": resources,
        "plan": plan,
        "pathfinder": pathfinder,
    }

    # The only user-facing agent. Optional because everything above is
    # already complete without it — skip it when you only need the data.
    if explain:
        result["explanation"] = run_explainer_agent(
            user_id, signal_id, pipeline_result=result, model_key=model_key, verbose=False
        )

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("signal_id")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--no-explain", action="store_true", help="skip the user-facing Explainer Agent")
    args = parser.parse_args()

    result = run_full_pipeline(args.user_id, args.signal_id, model_key=args.model, explain=not args.no_explain)
    print("\n\n=== FULL PIPELINE RESULT ===")
    print(json.dumps(result, indent=2))
