# Problem compression and recall

## User intent

Compress problem-solving experience for retrieval: requirements, corrections,
friction, stopping points, actual tools and results, and reusable next steps.
The user authorized implementation and a GitHub pull request on 2026-11-22,
then explicitly requested immediate merge and one remaining `main` branch.

## Scope

1. Extract bounded, revision-bound problem drafts from public conversation records.
2. Save drafts in existing problem cases, idempotently and without replacing authored edits.
3. Recall cases by the current question; optionally fall back to an explicitly selected history index.
4. Verify source immutability, evidence identity, incomplete records, CLI integration and existing regressions.
5. Merge PR #5 into current main, resolve integration conflicts and remove extra branches while retaining existing work/proposal history. Installation remains separate.
6. Follow-up authorized on 2026-11-22: fix the public compression/materials store-identity mismatch and push directly to `main`. Preserve existing case IDs, frozen-material semantics and exact downstream bindings.

Status: implementation and integration with newer main `7216e1c` verified; authorized remote delivery is recorded on PR #5 and in the conversation.

Reuse existing source readers, local JSON case storage and standard-library runtime.
Interpretation stays with the calling Agent; no background model service or implicit transcript upload.

## Acceptance

- Request, correction, failure and stop candidates retain source/message/revision references.
- Tool results are not promoted to task completion; missing context remains unknown.
- Repeated generation is idempotent and cannot overwrite an edited or authored case.
- Recall exposes the case's state, applicability, steps and evidence; a miss is not historical absence.
- Pagination keeps private slots and rejects stale revisions.
- Automated synthetic verification passes; no production-machine or real-history claims.
