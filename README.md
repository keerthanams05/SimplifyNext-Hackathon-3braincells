# SimplifyNext-Hackathon-3braincells — Career Guardian

Career Guardian tracks how AI/automation is reshaping specific *tasks* within a person's job — not "your job is at risk," but "these tasks are changing, here's your 30/60/90-day plan to adapt." Built as a pipeline of AWS Lambda agents backed by shared DynamoDB tables and Amazon Bedrock, orchestrated with Step Functions.

## Pipeline

`market-watcher` → `change-validation` → `role-intelligence` → `resource-connector` / `pathfinder` → `planner` → `action` → `evaluator` (loops back to `planner` on re-plan)

## Repo layout

```
/market-watcher        Person A
/change-validation      Person A
/resource-connector     Person A
/role-intelligence      Person B
/pathfinder             Person B
/planner                Person C
/action                 Person C
/evaluator              Person C
/shared                 schema, contract doc, seed data
```

- `shared/schema.md` — the reconciled DynamoDB schema (single source of truth, matches the provided dataset's own field names)
- `shared/contract.md` — the JSON contract for every Lambda: owner, input shape, output shape, tables touched. Update this the moment your function's I/O shape changes.
- `shared/seed_data/` — drop the dataset CSVs here for loading into DynamoDB

## Working in parallel

- Each person works inside their own folder(s) above, on their own branch.
- DynamoDB tables and schema are shared and locked — flag schema changes in the group chat before changing `shared/schema.md`.
- Write and test each Lambda locally against the seeded data before wiring it into the pipeline.
- Sync/integrate at agreed checkpoints (not continuously) — chain functions together via Step Functions and fix what breaks.
- Use the cheapest Bedrock model (Nova Micro/Haiku) while developing; save stronger models for final demo runs — Bedrock budget is shared.

See `shared/contract.md` for exact per-function I/O.
