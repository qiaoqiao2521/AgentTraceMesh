# Findings

- Merge integration: new main `7216e1c` adds projected capture. A complete physical prefix can still omit fields, records or images, so compression keeps projection coverage and marks incomplete evidence explicitly. Placeholder call IDs must never pair unrelated tool records.
- New main also adds pytest function tests; unittest discovery imports but does not execute those functions. CI must install the test-only runner and use the full pytest collection.

- Baseline: `38a044e54104f7a2a0995e1e7b8d689e61e98afd` on `main`.
- `audit/handoff.py` is session-continuation oriented; decisions are empty and remaining work may be generic.
- `conversation_navigation.py` offers wording hints and `search --brief` offers bounded paired tool evidence.
- `problem_cases.py` already stores authored explanations and provides CLI/HTTP/Web reading.
- The missing connection is automatic draft extraction plus question-first recall, not another case database.
- Public display indices include private slots. Indexed sources need a fixed SQLite read view and native sources need revision checks.
- Knowledge workflow mapping is unavailable in this cloud checkout; no private knowledge store was scanned.
- Review caught and fixed query-secret leakage through split recall terms, possible loss of later evidence on a narrower regeneration, misleading reuse of failed commands as next steps, and off-prefix signal excerpts.
- Full-suite date-filter failure was reproduced on the untouched base under Pacific local time. The existing fixture assumes UTC; the full suite passes with explicit `TZ=UTC` without changing production date semantics.
