# SimplifyNext-Hackathon-3braincells

An agentic AI pipeline that monitors career-disruption signals (e.g. AI/automation
adoption trends), works out which parts of a specific person's role are actually
affected, and produces a personalised 30/60/90-day upskilling plan — grounded in
real SkillsFuture / WSG Singapore resources.

Built for the SimplifyNext Agentic AI Hackathon 2026, using AWS Bedrock
(Claude + Nova) and DynamoDB.

## Architecture

```
Signal  ──▶  Signal Agent  ──▶  Role Intelligence Agent  ──▶  Planner Agent
(raw)        (validate &          (which of THIS person's        (30/60/90-day
              score it)            tasks/gaps does it hit)        plan, grounded
                                                                   in real resources)
```

Each stage is a standalone CLI script under `agents/`, backed by DynamoDB tables
(seeded from `data/*.csv` via `scripts/load_data.py`) and calling Bedrock via
`scripts/config.py`'s model IDs.

**Two guardrails baked into every agent's prompt**, learned from testing:
1. Each system prompt states today's real date and explicitly tells the model
   not to reject unfamiliar signals/dates as fabrication just because they're
   after its training cutoff.
2. Each agent that returns IDs (`task_id`, `gap_id`, `resource_id`) is told to
   ONLY use IDs from an explicit allow-list passed in the prompt, and a
   `validate_*` function checks the model's output against that list afterward
   and prints a `WARNING` if it invented one. **Always check for that warning
   before trusting an agent's output** — it means the model hallucinated an ID
   that doesn't exist in your data.

## Agents

### 1. Signal Agent (`agents/signal_agent.py`)
Takes a `signal_id`, validates it against evidence quality/sourcing, and scores
relevance (0-100) and severity (Low/Medium/High).

```bash
python agents/signal_agent.py SIG001
python agents/signal_agent.py SIG001 --model nova
python agents/signal_agent.py SIG001 --save   # writes verdict back to DynamoDB
```

### 2. Role Intelligence Agent (`agents/role_intelligence_agent.py`)
Takes `user_id` + `signal_id`. Pulls that person's own `role_tasks` and
`skill_gaps` from DynamoDB, and asks the model which of *this specific
person's* tasks/gaps are actually affected by *this specific signal* — not a
generic "does this signal matter" answer.

```bash
python agents/role_intelligence_agent.py USER001 SIG001
python agents/role_intelligence_agent.py USER001 SIG001 --model nova
```

Returns empty `affected_tasks`/`affected_skills`/`skill_gaps` when the signal
genuinely doesn't apply to that person (e.g. a software-engineering signal
against a marketing or finance persona) — confirmed working for
USER002/SIG005 and USER003/SIG009.

### 3. Planner Agent (`agents/planner_agent.py`)
Takes `user_id` + `signal_id`. Reuses the Role Intelligence Agent's verdict for
that pair, pulls the full `resources` catalogue (SkillsFuture/WSG programmes),
and asks the model for a 3-phase (Days 1-30 / 31-60 / 61-90) upskilling plan,
citing only real `resource_id`s.

```bash
python agents/planner_agent.py USER001 SIG001
python agents/planner_agent.py USER001 SIG001 --model nova
python agents/planner_agent.py USER001 SIG001 --save   # writes to `generated_plans` table (create it first, see below)
```

**Short-circuit behaviour:** if the Role Intelligence verdict for a
user/signal pair is empty (signal doesn't affect them), the Planner does NOT
call Bedrock to invent a plan — it returns `"phases": []` with a
`"no_plan_reason"` explaining why. This was added after testing showed the
model would otherwise fabricate a low-effort plan even for clearly irrelevant
signals (e.g. a marketing-sector signal against a finance persona).

## Data model

DynamoDB tables (seeded via `scripts/load_data.py` from `data/*.csv`,
table names resolved through `scripts/config.py`'s `table_name()`):

| Short name    | Partition key | Source CSV              | Notes |
|---------------|---------------|--------------------------|-------|
| `signals`     | `signal_id`   | `data/signals.csv`       | Raw disruption signals |
| `role_tasks`  | `user_id`     | `data/role_tasks.csv`    | Per-person current tasks |
| `skill_gaps`  | `user_id`     | `data/skill_gaps.csv`    | Per-person skill gaps, prioritised |
| `resources`   | `resource_id` (assumed) | `data/resources.csv` | Global — SkillsFuture/WSG programmes. No per-user key, so the Planner Agent scans the whole table. |
| `current_skills` | — | `data/current_skills.csv` | Not yet wired into an agent |
| `pathfinder`  | — | `data/pathfinder.csv`    | Reference alt-career-path data — not yet wired into an agent |
| `progress`    | — | `data/progress.csv`      | Not yet wired into an agent |
| `personas`    | — | `data/personas.csv`      | Persona definitions (Sarah/USER001 = SWE, Daniel/USER002 = Marketing, Michelle/USER003 = Finance) |
| `plans_30_60_90` | `user_id` | `data/plans_30_60_90.csv` | **Reference/gold-standard plans** for demo sanity-checking — not read by the Planner Agent, which generates its own |

`generated_plans` (partition key `plan_id`, string) does **not exist yet** —
create it in `scripts/create_table.py` before using `planner_agent.py --save`.

## Setup

```bash
# One-time AWS SSO setup
aws configure sso   # profile name: hack2026, account 479575346211
aws sso login --profile hack2026
export AWS_PROFILE=hack2026

# Python env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Seed DynamoDB (once per fresh account/session)
python scripts/create_table.py
python scripts/load_data.py
```

Bedrock model IDs and AWS/Bedrock regions live in `scripts/config.py`
(`CLAUDE_MODEL_ID`, `NOVA_MICRO_MODEL_ID`, `AWS_REGION`, `BEDROCK_REGION`).

## Known issues / open items

- **`.env` handling**: make sure no real credentials are committed. Use
  `.env.example` with variable names only; add `.env` to `.gitignore`.
- **`--save` paths are untested** for both `signal_agent.py` and
  `planner_agent.py` in this environment — confirm target tables exist before
  relying on them in the demo.
- **`resources` table partition key** — assumed to be `resource_id`; confirm
  against `scripts/create_table.py`.
- Not every `user_id` × `signal_id` combination has been run through the full
  pipeline end-to-end yet.

## Roadmap

- [x] Signal Agent
- [x] Role Intelligence Agent
- [x] Planner Agent
- [ ] Pathfinder Agent (alternate career-path recommendations, using `data/pathfinder.csv` as reference)
- [ ] Progress Agent (tracks plan completion against `data/progress.csv`, flags drift / re-plans)
