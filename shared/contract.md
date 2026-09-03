# Career Guardian — Lambda Function Contract

This is the shared "JSON contract" for the pipeline. Every Lambda function is listed here with its owner, exact input/output JSON shape, and which DynamoDB tables (see `shared/schema.md`) it reads/writes.

**Rule: update this file the moment your function's input or output shape changes.** Silent drift here is the #1 way parallel work breaks. Flag schema-affecting changes in the group chat before merging.

Pipeline order: `market-watcher` → `change-validation` → `role-intelligence` → `resource-connector` / `pathfinder` → `planner` → `action` → `evaluator` (loops back to `planner` on re-plan).

---

## Person A

### `market-watcher`
- **Owner:** Person A
- **Status:** Not built for demo — `signals` table is pre-seeded from `signals.csv`, this agent is skipped (see `shared/schema.md` §5, Build note)
- **Input:** _(future: scrape/ingest trigger, none for demo)_
- **Output (signal shape written to `signals`):**
```json
{
  "signal_id": "SIG001",
  "title": "string",
  "sector": "string",
  "technology": "string",
  "date": "YYYY-MM-DD",
  "source": "string",
  "source_url": "string",
  "evidence": "string",
  "severity": "High | Medium | Low",
  "frequency": "High | Medium | Low",
  "source_credibility": "High | Medium | Low",
  "affected_roles": ["string"],
  "signal_summary": "string"
}
```
- **Tables touched:** writes `signals`

### `change-validation`
- **Owner:** Person A
- **Input:**
```json
{ "signal_id": "SIG001" }
```
- **Output:** (fast path: return `signals.expected_signal_output` as-is; real path: Bedrock-generated, same shape)
```json
{
  "validated": true,
  "relevance_score": 0.0,
  "severity": "High | Medium | Low",
  "reason": "string"
}
```
- **Tables touched:** reads `signals`

### `resource-connector`
- **Owner:** Person A
- **Input:**
```json
{
  "user_id": "USER001",
  "skill_gaps": [{ "skill": "string", "gap_priority": "High | Medium | Low" }],
  "budget": "Low | Medium | High",
  "preferred_learning_style": "string"
}
```
- **Output:**
```json
{
  "user_id": "USER001",
  "recommended_resources": [
    {
      "resource_id": "RES001",
      "name": "string",
      "provider": "string",
      "type": "string",
      "funding": "string",
      "url": "string"
    }
  ]
}
```
- **Tables touched:** reads `resources`

---

## Person B

### `role-intelligence`
- **Owner:** Person B
- **Input:**
```json
{ "signal_id": "SIG001", "user_id": "USER001" }
```
- **Output:** (fast path: return matching entry from `signals.expected_role_output`; real path: Bedrock-generated, same shape)
```json
{
  "persona": "USER001",
  "affected_tasks": ["TASK001"],
  "impact_level": "High | Medium | Low",
  "affected_skills": ["string"]
}
```
- **Tables touched:** reads `signals`, `role_tasks`; writes/updates `skill_gaps`

### `pathfinder`
- **Owner:** Person B
- **Input:**
```json
{ "user_id": "USER001", "target_role": "string" }
```
- **Output:**
```json
{
  "user_id": "USER001",
  "target_role": "string",
  "similarity_score": 0.0,
  "transferable_skills": "string",
  "gap_to_role": "string",
  "reason": "string"
}
```
- **Tables touched:** reads `users`, `user_skills`; writes `pathfinder_results`

---

## Person C

### `planner`
- **Owner:** Person C
- **Input:**
```json
{
  "user_id": "USER001",
  "skill_gaps": [{ "skill": "string", "gap_priority": "High | Medium | Low" }],
  "recommended_resources": [{ "resource_id": "RES001" }],
  "pathfinder_result": { "target_role": "string", "gap_to_role": "string" }
}
```
- **Output:** (one item per 30/60/90 phase, matches `plans` table rows)
```json
[
  {
    "plan_id": "PLAN001",
    "user_id": "USER001",
    "phase": "Days 1-30",
    "goal": "string",
    "milestones": "string",
    "tasks": "string",
    "resource_ids": ["RES001"],
    "hours_per_week": 0
  }
]
```
- **Tables touched:** reads `skill_gaps`, `resources`, `pathfinder_results`; writes `plans`

### `action`
- **Owner:** Person C
- **Input:**
```json
{ "user_id": "USER001", "plan_id": "PLAN001" }
```
- **Output:**
```json
{
  "user_id": "USER001",
  "plan_id": "PLAN001",
  "active_tasks": ["string"],
  "status": "started | in_progress"
}
```
- **Tables touched:** reads `plans`; writes `progress`

### `evaluator`
- **Owner:** Person C
- **Input:**
```json
{ "user_id": "USER001" }
```
- **Output:**
```json
{
  "user_id": "USER001",
  "overall_completion_pct": 0,
  "stalled_tasks": ["string"],
  "replan_needed": true,
  "reason": "string"
}
```
- **Tables touched:** reads `progress`; on `replan_needed: true`, triggers `planner` again (the demo's re-plan loop)

---

## How to update this doc

1. Change your function's input/output shape only in your own section.
2. If the change affects another function's input (i.e. you're changing what you hand downstream), post it in the group chat before merging to `main`.
3. Keep field names identical to `shared/schema.md` table columns wherever the JSON is just a table row — don't introduce a parallel naming scheme.
