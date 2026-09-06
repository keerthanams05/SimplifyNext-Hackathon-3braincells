"""
Shared config for CareerGuardian AWS scripts.

This is the single source of truth for table names and key structure —
everyone on the team should import from here rather than hardcoding
table/attribute names in their own agent code. If the schema needs to
change, change it here once.
"""

import os
from dotenv import load_dotenv

load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
S3_RAW_PREFIX = os.getenv("S3_RAW_PREFIX", "data/raw/")
TABLE_PREFIX = os.getenv("DYNAMODB_TABLE_PREFIX", "careerguardian_")

BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
CLAUDE_MODEL_ID = os.getenv("CLAUDE_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
NOVA_MICRO_MODEL_ID = os.getenv("NOVA_MICRO_MODEL_ID", "amazon.nova-micro-v1:0")

# Which model each agent should use, decided by task complexity, not
# by default — this is a deliberate cost/quality tradeoff worth calling
# out explicitly in the pitch. "claude" and "nova" here map to the two
# model IDs above; agent code should look up its own key rather than
# hardcoding a model choice.
AGENT_MODEL_TIER = {
    "signal_agent": "claude",         # judges evidence credibility — needs real reasoning
    "role_intelligence": "claude",    # explains *why* a task is affected — needs nuance
    "planner": "claude",              # synthesizes a sequenced, personalized plan
    "pathfinder": "claude",           # comparative reasoning across career paths
    "resource_connector": "nova",     # matching against a known, structured resource list
    "opportunity_finder": "nova",     # same: picks from a curated link catalogue, never writes URLs
    "replanning_trigger": "nova",     # narrow yes/no decision against known criteria
    "explainer": "claude",            # user-facing text — quality matters for the demo
    "prep_coach": "claude",           # rewriting someone's real experience — needs care
    "market_watcher": "nova",         # triage of incoming headlines against a watch list
}


def model_for_agent(agent_key: str) -> str:
    tier = AGENT_MODEL_TIER.get(agent_key, "claude")
    return CLAUDE_MODEL_ID if tier == "claude" else NOVA_MICRO_MODEL_ID


def table_name(short_name: str) -> str:
    """e.g. table_name('users') -> 'careerguardian_users'"""
    return f"{TABLE_PREFIX}{short_name}"


# Recommended schema, following README.txt's 9-table structure
# (skill_gaps added as a 10th lookup table since the dataset ships it
# as ground-truth demo data, even though the agent can also compute it live).
#
# Each entry: short table name -> (partition_key, sort_key_or_None)
TABLE_SCHEMA = {
    "users": ("user_id", None),
    "user_skills": ("user_id", "skill"),
    "role_tasks": ("user_id", "task_id"),
    "skill_gaps": ("user_id", "gap_id"),
    "signals": ("signal_id", None),
    "resources": ("resource_id", None),
    "plans": ("user_id", "plan_id"),
    "progress": ("user_id", "progress_id"),
    "pathfinder_results": ("user_id", "target_role"),
    "generated_plans": ("plan_id", None),
    # Roles the person has told us they're in or curious about. This is
    # what the Market Watcher scans for daily and what the Opportunity
    # Finder organises its links around.
    "saved_roles": ("user_id", "target_role"),
    "opportunities": ("opportunity_id", None),
    # Cached pipeline results, so a repeat click doesn't pay for six
    # Bedrock calls again. Key is user + signal + model.
    "pipeline_cache": ("cache_key", None),
}

# Maps each DynamoDB table to the CSV file that feeds it, and which
# columns in that CSV are JSON-encoded strings that need parsing
# before the row can be written.
CSV_TO_TABLE = {
    "personas.csv": {
        "table": "users",
        "json_columns": ["alternative_career_interest", "current_skills", "tasks", "skills_gap"],
    },
    "current_skills.csv": {
        "table": "user_skills",
        "json_columns": [],
    },
    "role_tasks.csv": {
        "table": "role_tasks",
        "json_columns": [],
    },
    "skill_gaps.csv": {
        "table": "skill_gaps",
        "json_columns": [],
    },
    "signals.csv": {
        "table": "signals",
        "json_columns": ["affected_roles", "expected_signal_output", "expected_role_output"],
    },
    "resources.csv": {
        "table": "resources",
        "json_columns": [],
    },
    "plans_30_60_90.csv": {
        "table": "plans",
        "json_columns": [],
    },
    "progress.csv": {
        "table": "progress",
        "json_columns": [],
    },
    "pathfinder.csv": {
        "table": "pathfinder_results",
        "json_columns": [],
    },
    "saved_roles.csv": {
        "table": "saved_roles",
        "json_columns": [],
    },
    "opportunities.csv": {
        "table": "opportunities",
        "json_columns": [],
    },
}