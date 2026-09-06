"""
Pipeline orchestrator — runs the agents for one user_id + signal_id and
returns ONE consolidated result. This is what the API layer calls;
nothing outside this file should need to know there are six separate
agent scripts underneath.

The chain is NOT a straight line, and running it as one made it slow.
What actually depends on what:

    Signal Checker      needs: the signal
    Role Reader         needs: the signal + the person
    Path Scout          needs: the signal + the person
        ^ none of those three need each other, so they run TOGETHER

    Course Finder       needs: Role Reader's gaps
    Plan Builder        needs: Role Reader's verdict + Course Finder's shortlist
    Translator          needs: everything above

So it's four waves, not six steps — the three slowest calls overlap
instead of queueing. `timings` in the result says where the time actually
went, so this stays measurable rather than assumed.

Running agents concurrently is only safe because they now share
thread-local AWS handles (scripts/aws_clients.py) — boto3 resources are
not thread-safe, and each thread gets its own.

Usage:
    python agents/pipeline.py USER001 SIG001
    python agents/pipeline.py USER001 SIG001 --no-explain
    python agents/pipeline.py USER001 SIG001 --serial     # for comparison
"""

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from signal_agent import run_signal_agent
from role_intelligence_agent import run_role_intelligence_agent
from resource_connector_agent import run_resource_connector_agent
from planner_agent import run_planner_agent
from pathfinder_agent import run_pathfinder_agent
from explainer_agent import run_explainer_agent


def _timed(timings: dict, name: str, fn, *args, **kwargs):
    """Run one agent and record how long it took."""
    started = time.perf_counter()
    try:
        return fn(*args, **kwargs)
    finally:
        timings[name] = round(time.perf_counter() - started, 2)


def run_full_pipeline(
    user_id: str,
    signal_id: str,
    model_key: str = "claude",
    explain: bool = True,
    parallel: bool = True,
) -> dict:
    # verbose=False on every sub-agent call: each one would otherwise print
    # its own verdict, which is useful when running that agent standalone
    # but just duplicates output here — the pipeline prints ONE consolidated
    # result at the end instead. Hallucination WARNING prints (inside each
    # agent's validate_* function) are unconditional and still show up
    # regardless of verbose, since those matter even in pipeline mode.
    timings = {}
    wall_started = time.perf_counter()

    # ---- Wave 1: the three agents that don't need each other -----------
    if parallel:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent") as pool:
            signal_future = pool.submit(
                _timed, timings, "signal", run_signal_agent,
                signal_id, model_key=model_key, verbose=False,
            )
            role_future = pool.submit(
                _timed, timings, "role_intelligence", run_role_intelligence_agent,
                user_id, signal_id, model_key=model_key, verbose=False,
            )
            pathfinder_future = pool.submit(
                _timed, timings, "pathfinder", run_pathfinder_agent,
                user_id, signal_id, model_key=model_key, verbose=False,
            )
            # .result() re-raises whatever the agent raised, so a failure
            # here surfaces exactly as it would have when serial.
            signal_verdict = signal_future.result()
            role_verdict = role_future.result()
            pathfinder = pathfinder_future.result()
    else:
        signal_verdict = _timed(timings, "signal", run_signal_agent,
                                signal_id, model_key=model_key, verbose=False)
        role_verdict = _timed(timings, "role_intelligence", run_role_intelligence_agent,
                              user_id, signal_id, model_key=model_key, verbose=False)
        pathfinder = _timed(timings, "pathfinder", run_pathfinder_agent,
                            user_id, signal_id, model_key=model_key, verbose=False)

    # ---- Wave 2: match programmes to the gaps this signal raised -------
    # Cheap tier, and it shrinks the Planner's prompt. Skipped entirely
    # when Role Reader found nothing, since there'd be no gaps to match.
    if role_verdict.get("skill_gaps") or role_verdict.get("affected_tasks"):
        resources = _timed(
            timings, "resource_connector", run_resource_connector_agent,
            user_id, gaps=role_verdict.get("skill_gaps"), verbose=False,
        )
    else:
        resources = {"user_id": user_id, "matches": [], "shortlist_resource_ids": []}

    # ---- Wave 3: sequence it -------------------------------------------
    # The verdict is passed through so the Planner doesn't repeat Wave 1's
    # Role Reader call; the shortlist narrows what it's allowed to cite.
    plan = _timed(
        timings, "planner", run_planner_agent,
        user_id, signal_id, model_key=model_key, verdict=role_verdict, verbose=False,
        shortlist_resource_ids=resources.get("shortlist_resource_ids"),
    )

    result = {
        "user_id": user_id,
        "signal_id": signal_id,
        "signal": signal_verdict,
        "role_intelligence": role_verdict,
        "resources": resources,
        "plan": plan,
        "pathfinder": pathfinder,
    }

    # ---- Wave 4: the only user-facing agent ----------------------------
    # Optional because everything above is already complete without it.
    if explain:
        result["explanation"] = _timed(
            timings, "explainer", run_explainer_agent,
            user_id, signal_id, pipeline_result=result, model_key=model_key, verbose=False,
        )

    timings["total_wall_clock"] = round(time.perf_counter() - wall_started, 2)
    timings["sum_of_agents"] = round(sum(v for k, v in timings.items() if k != "total_wall_clock"), 2)
    result["timings"] = timings
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id")
    parser.add_argument("signal_id")
    parser.add_argument("--model", choices=["claude", "nova"], default="claude")
    parser.add_argument("--no-explain", action="store_true", help="skip the user-facing Explainer Agent")
    parser.add_argument("--serial", action="store_true", help="run wave 1 one-at-a-time, to compare timings")
    args = parser.parse_args()

    result = run_full_pipeline(
        args.user_id, args.signal_id,
        model_key=args.model,
        explain=not args.no_explain,
        parallel=not args.serial,
    )
    print("\n\n=== FULL PIPELINE RESULT ===")
    print(json.dumps(result, indent=2))

    t = result["timings"]
    print("\n=== WHERE THE TIME WENT ===")
    for name, seconds in sorted(t.items(), key=lambda kv: -kv[1]):
        if name not in ("total_wall_clock", "sum_of_agents"):
            print(f"  {name:22} {seconds:6.2f}s")
    print(f"  {'-'*30}")
    print(f"  {'agents, added up':22} {t['sum_of_agents']:6.2f}s")
    print(f"  {'actual wall clock':22} {t['total_wall_clock']:6.2f}s"
          f"   (saved {max(0, round(t['sum_of_agents'] - t['total_wall_clock'], 2))}s by overlapping)")
