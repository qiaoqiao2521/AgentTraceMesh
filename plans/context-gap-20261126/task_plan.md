# Context gaps and action recovery

## Goal and authorization

Implement the user's 2026-11-26 request directly on main and synchronize SpecMesh.
Find what blocks the next action, retrieve only relevant evidence, and carry the
result into existing compression/handoff. Preserve local-first stdlib runtime.

## Scope

- Reuse case recall and the selected read-only history index.
- Add bounded current-task gap routing, caller-carried duplicate/retry receipts,
  and explicit next actions. No gap means zero retrieval.
- Expose historical missing-context candidates through compression; the current
  Agent checks applicability before retrieving. Never automatically resume a stop.
- Extend handoff optionally, keeping historical and current-task state separate.
- Synchronize SpecMesh's progressive entry, evidence boundary and minimal templates.

## Acceptance

Zero calls with adequate declared evidence; exact project/source scope; no implicit
refresh; repeated queries suppressed; misses/partial/conflict remain unresolved;
old CLI behavior compatible; evidence and redaction preserved; no remote execution.
Test fixtures are synthetic and cloud-local. No user-machine installation or token
saving claims. Publish only reviewed changes, without force-pushing.

## Current

Source implementation, integration and independent review complete. Verification
and delivery boundaries are recorded in progress.md; Git history identifies the
published source revision.
