# SimplifyNext-Hackathon-3braincells

An agentic AI pipeline that monitors career-disruption signals (e.g. AI/automation
adoption trends), works out which parts of a specific person's role are actually
affected, and produces a personalised 30/60/90-day upskilling plan — grounded in
real SkillsFuture / WSG Singapore resources.

Built for the SimplifyNext Agentic AI Hackathon 2026, using AWS Bedrock
(Claude + Nova) and DynamoDB.

## Architecture

```
     ┌─ daily ─────────────────────────────────────────────────────┐
     │  Watcher ──▶ Signal Checker ──▶ Role Reader ──▶ Course      │
     │  (scans      (is the           (which of your    Finder     │
     │   feeds for   evidence          tasks change?)   (what can  │
     │   your saved  solid?)                             you join?)│
     │   roles)                                             │      │
     └──────────────────────────────────────────────────────┼──────┘
                                                            ▼
   Path Scout ──▶ Opportunity Finder ──▶ Prep Coach ◀── Plan Builder
   (where else     (job boards, events,   (résumé +      (30/60/90
    could you go?)  communities per path)  interview)     around your week)
                                              │
                                              ▼
                                         Translator ──▶ you
                                         (plain language)

   Progress Coach ──▶ back to Plan Builder when someone falls behind
```

Everything personalises around **saved roles**: the role you're in plus any
you've starred. Starring a role adds it to the Watcher's daily scan and gives
the Opportunity Finder another pathway to gather links for.

Each stage is a standalone CLI script under `agents/`, backed by DynamoDB tables
(seeded from `data/*.csv` via `scripts/load_data.py`) and calling Bedrock via
`scripts/config.py`'s model IDs. `agents/pipeline.py` runs the chain end to end;
`api/main.py` exposes it (plus each agent individually) over HTTP.

Model tier per agent is a deliberate cost/quality split, declared once in
`scripts/config.py`'s `AGENT_MODEL_TIER`: the judgment-heavy agents on Claude
Sonnet, and the ones doing structured matching or narrow triage (Course
Finder, Opportunity Finder, Progress, Watcher) on Nova Micro. **This is not
surfaced in the UI** — which model runs what is our implementation detail, not
something a user needs to read.

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

If the Resource Connector Agent ran first, the Planner receives its
`shortlist_resource_ids` and plans against that pre-filtered set instead of the
whole catalogue — a smaller prompt that already respects the person's budget
and weekly hours. It falls back to the full catalogue when run standalone.

### 4. Pathfinder Agent (`agents/pathfinder_agent.py`)
Takes `user_id` + `signal_id`. Ranks that person's candidate adjacent roles by
how relevant each becomes *given this signal*, using `pathfinder_results` as the
candidate list — it ranks, it never invents roles.

```bash
python agents/pathfinder_agent.py USER001 SIG001
```

### 5. Resource Connector Agent (`agents/resource_connector_agent.py`)
Takes `user_id` (and optionally the gaps Role Intelligence just flagged). Matches
each skill gap to 1-2 real programmes from the `resources` catalogue, respecting
budget, weekly hours and preferred course type. Returns fully merged resource
objects plus a flat `shortlist_resource_ids` for the Planner.

```bash
python agents/resource_connector_agent.py USER001
```

Defaults to Nova Micro — structured matching against a known list is routing,
not reasoning. Returns an empty list for a gap rather than forcing a bad match.

### 6. Progress Agent (`agents/progress_agent.py`)
Takes `user_id` (+ optional `--signal` to load that saved plan for context).
Decides which progress items are genuinely behind and whether that warrants a
re-plan. Skips Bedrock entirely when there's no progress data to judge.

```bash
python agents/progress_agent.py USER001
python agents/progress_agent.py USER001 --signal SIG001
```

### 7. Explainer Agent (`agents/explainer_agent.py`)
The only agent whose audience is the user, not the code. Takes the full pipeline
result and writes the human-facing copy: headline, summary, what changed, first
step, and one honest line on what they already have going for them.

```bash
python agents/explainer_agent.py USER001 SIG001
```

**Framing guardrail:** its system prompt bans probability-of-unemployment
language outright and forces task-level framing, because that framing is the
easiest way for this product to come across badly. Everything else in the
system produces data; this is where tone is decided.

### 8. Opportunity Finder (`agents/opportunity_finder_agent.py`)
Takes `user_id`. For each saved role, gathers the real places to go: job
boards, communities, professional bodies, events, hackathons — grouped into
plain-language sections ("Where the jobs are posted", "People doing this
already", "Things to turn up to").

```bash
python agents/opportunity_finder_agent.py USER004
python agents/opportunity_finder_agent.py USER004 --role "Finance Analyst"
```

**The model never writes a URL.** Every link comes out of `opportunities.csv`
or `resources.csv` by ID, and `filter_to_real_ids()` drops anything that isn't
a real ID before it reaches a person. A hallucinated course link in a careers
product is the worst failure this app could have, so the model only chooses
which catalogue rows belong on which pathway.

### 9. Prep Coach (`agents/prep_agent.py`)
Takes `user_id` + a target role (defaults to their top starred role). Produces
résumé bullets, practice questions, an honest readiness score, and what to do
before applying.

```bash
python agents/prep_agent.py USER004 "Finance Analyst"
python agents/prep_agent.py USER004
```

Two hard rules in the prompt: **never invent experience** (every bullet must
name the real task it rewrites, and `validate_bullets()` warns if one doesn't
trace back), and **never coach someone to hide a gap** — it names gaps and
gives honest language for what they're doing about them.

### 10. Market Watcher (`agents/market_watcher_agent.py`)
The daily scan. Builds a watch list from everyone's saved roles, pulls
headlines from `data/watch_sources.csv`, drops anything already in `signals`
(by URL and by normalised title, since stories get re-syndicated), and triages
the rest on the cheap tier.

```bash
python agents/market_watcher_agent.py --source seeded --dry-run  # offline, no spend
python agents/market_watcher_agent.py --dry-run                  # real feeds, no writes
python agents/market_watcher_agent.py                            # the daily run
```

Survivors are written as **unvalidated** signals (`severity: "Unrated"`,
`validated: false`) for the Signal Checker to assess — the Watcher finds
things, it never decides they're true. A dead feed is skipped with a warning
rather than failing the run.

**Running it daily:** EventBridge `rate(1 day)` → Lambda, or cron
(`0 7 * * * python agents/market_watcher_agent.py`), or a GitHub Actions
schedule. Feed URLs in `watch_sources.csv` are marked VERIFY — check them
before the demo.

### Running the whole chain

```bash
python agents/pipeline.py USER001 SIG001              # all agents, incl. plain-language output
python agents/pipeline.py USER001 SIG001 --no-explain # data only, skips the Explainer
```

## Data model

DynamoDB tables (seeded via `scripts/load_data.py` from `data/*.csv`,
table names resolved through `scripts/config.py`'s `table_name()`):

| Short name    | Partition key | Source CSV              | Notes |
|---------------|---------------|--------------------------|-------|
| `signals`     | `signal_id`   | `data/signals.csv`       | Raw disruption signals |
| `role_tasks`  | `user_id`     | `data/role_tasks.csv`    | Per-person current tasks |
| `skill_gaps`  | `user_id`     | `data/skill_gaps.csv`    | Per-person skill gaps, prioritised |
| `resources`   | `resource_id` | `data/resources.csv` | Global — 17 SkillsFuture/WSG/IMDA programmes. No per-user key, so it gets scanned whole. |
| `user_skills` | `user_id` | `data/current_skills.csv` | Read by Role Intelligence (as context) and Pathfinder |
| `pathfinder_results` | `user_id` | `data/pathfinder.csv` | Candidate adjacent roles the Pathfinder Agent ranks |
| `progress`    | `user_id` | `data/progress.csv`      | Read by the Progress Agent |
| `users`       | `user_id` | `data/personas.csv`      | 6 Singapore personas — see below |
| `saved_roles` | `user_id` + `target_role` | `data/saved_roles.csv` | **What everything personalises around** — the role you're in plus the ones you've starred |
| `opportunities` | `opportunity_id` | `data/opportunities.csv` | 20 real SG job boards, communities, events and competitions |
| `plans_30_60_90` | `user_id` | `data/plans_30_60_90.csv` | **Reference/gold-standard plans** for demo sanity-checking — not read by the Planner Agent, which generates its own |

`generated_plans` (partition key `plan_id`) is in `TABLE_SCHEMA` — re-run
`python scripts/create_table.py` (safe to re-run) to create it before using
`planner_agent.py --save`.

### Personas

| ID | Who | Role | Sector | Why they're in the set |
|----|-----|------|--------|------------------------|
| USER001 | Sarah Tan, 29 | Software Engineer | Technology | Upskill-in-place case; the main demo persona |
| USER002 | Daniel | Digital Marketing Executive | Marketing | Content production under generative AI |
| USER003 | Michelle | Accounts Executive | Finance | Finance automation case |
| USER004 | Priya Nair, 41 | Accounts Payable Officer | Finance | Mid-career, low budget, 4 hrs/week — tests whether plans respect real constraints |
| USER005 | Marcus Ong, 27 | QA Engineer | Technology | Highest-exposure tech task set (agents write and run tests) |
| USER006 | Siti Rahmah, 34 | Social Media Executive | Marketing | Has a task explicitly `Behind Schedule`, so the re-plan loop can be demoed |

USER004-006 were added for Singapore coverage across exposure levels and
budgets. No new signals were invented for them: every row in `signals.csv`
cites a real source URL, so the new personas were given roles the **existing**
evidence genuinely covers, and those roles were added to the `affected_roles`
of the signals in their sector.

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

### Running the API + frontend

```bash
uvicorn api.main:app --reload --port 8000
# then open frontend/index.html in a browser
```

| Endpoint | What it does |
|----------|--------------|
| `/api/personas` | Persona picker |
| `/api/profile?user_id=` | Profile + skills |
| `/api/alerts?user_id=` | Signals relevant to their role (no LLM call) |
| `/api/agents` | The agent directory — who works on your case, and on which model tier |
| `/api/pipeline?user_id=&signal_id=&explain=` | The whole chain |
| `/api/resources?user_id=` | Course Finder on its own (cheap tier) |
| `/api/explain?user_id=&signal_id=` | Plain-language output only |
| `/api/progress?user_id=` | Progress check + re-plan decision |
| `/api/replan?user_id=&signal_id=` | Re-plan if they're behind |
| `GET/POST/DELETE /api/saved-roles` | Star, list and un-star future roles |
| `/api/opportunities?user_id=&role=` | Job boards, communities and events per pathway |
| `/api/prep?user_id=&target_role=` | Résumé lines, practice questions, readiness |
| `/api/watch-list` | What the daily scan is currently watching |
| `POST /api/watch-run?dry_run=true` | Kick the daily scan by hand (for demos) |

## Known issues / open items

- **`.env` handling**: make sure no real credentials are committed. Use
  `.env.example` with variable names only; add `.env` to `.gitignore`.
- **`--save` paths are untested** for both `signal_agent.py` and
  `planner_agent.py` in this environment — confirm target tables exist before
  relying on them in the demo.
- Not every `user_id` × `signal_id` combination has been run through the full
  pipeline end-to-end yet — USER004-006 in particular are unverified against
  live Bedrock.
- **The full pipeline is 6 serial Bedrock calls.** Signal and Pathfinder don't
  depend on Role Intelligence, so they could run concurrently — but the agents
  share module-level `boto3.resource` objects, which aren't thread-safe, so
  that needs a per-thread client before it's safe to parallelise.
- **`/api/alerts` matches occupation to `affected_roles` by exact string.**
  All six personas match cleanly today; any wording drift silently empties
  someone's dashboard.
- **The frontend interpolates API data straight into `innerHTML`.** Fine with
  trusted seeded data, worth escaping before this goes anywhere real.
- New resource rows (RES013-017) point at real Singapore schemes but their
  funding text defers to the official page — **verify before the demo**, same
  as the existing rows say.

## Roadmap

- [x] Signal Agent
- [x] Role Intelligence Agent
- [x] Planner Agent
- [x] Pathfinder Agent
- [x] Resource Connector Agent
- [x] Progress Agent + re-plan loop
- [x] Explainer Agent (plain-language layer)
- [x] Opportunity Finder (job boards, communities, events per pathway)
- [x] Prep Coach (résumé + interview prep)
- [x] Market Watcher — daily scan over saved roles
- [x] Saved roles (star a future role, it joins the daily scan)
- [ ] Front end rebuilt on the pastel design direction
- [ ] Course Finder / Plan Builder as interactive pickers rather than
      generated text (API returns options; the UI needs to render them)
