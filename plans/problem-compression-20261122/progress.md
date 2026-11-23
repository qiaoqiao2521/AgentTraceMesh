# Progress

## 2026-11-22

- Read project constraints and current history/case interfaces.
- Created branch `codex/problem-compression-20261122` from `38a044e`.
- Implemented `HistoryReader.compress` and the `compress` CLI: bounded public problem drafts, original message slots, correction/stop signals, paired tool evidence and explicit unknown outcomes.
- Implemented protected generated-case persistence and `recall`: case-first lexical lookup, bounded result/evidence summaries and optional explicit-source history fallback.
- Protected authored/directly edited cases and prevented a narrower evidence window from replacing a more complete draft.
- Independent review completed with no remaining P1/P2 findings. Counterexamples cover private slots, stale revisions, call-ID collisions, cross-store identities, redaction and edits during save.

## Verification

- `TZ=UTC python3 -m unittest discover -s tests`: 931 tests, 930 passed, 1 existing optional test skipped; Python 3.12.14.
- `node --test tests/test_*.js`: all 9 frontend test files passed; Node 24.22.3.
- `python3 scripts/verify_release.py --report <scratch>/compression-release-smoke.json`: passed on Linux; original synthetic sources, existing caches and isolated home remained unchanged. No artifact publication or installation on the user's device.
- `git diff --check`: passed.
- The first full Python run exposed the expected public-reader whitelist update and an existing UTC fixture assumption in `test_reuse_http`. The latter reproduces on unchanged `38a044e` under the host's Pacific timezone. UTC regression passes; no date-filter production code or unrelated test was changed.

## Delivery

Opened [PR #5](https://github.com/qiaoqiao2521/AgentTraceMesh/pull/5) at `13ff149`.
The user subsequently authorized immediate merge and a single remaining `main` branch.
Before merging, integrated new main `7216e1c`: retained both `recall` and `omni` routes;
preserved projected-capture loss/coverage and rejected placeholder call IDs, with two additional regression tests.
New main includes pytest function tests, so CI now installs the verified test-only pytest version and runs the complete pytest collection; application runtime remains dependency-free.
Final integration verification: `TZ=UTC python3 -m pytest -q tests` with test-only pytest 9.2.0: **950 passed, 1 skipped, 439 subtests passed**. The isolated source-release smoke passed again, with synthetic sources, existing cache and home unchanged. Remote merge and branch-cleanup receipts are reported on PR #5/in the conversation.
Production rollout and real-history acceptance remain outside this source delivery.

Knowledge closeout: `pending` — configured knowledge mapping is unavailable in this cloud environment; owner is the user's configured knowledge workflow. Repository documentation will record this feature's contracts.

## 2026-11-22 — SpecMesh material identity follow-up

The user authorized fixing the integration finding and pushing directly to `main`, without a PR.
Public `HistoryReader.materials` / CLI exports now retain the compression store namespace;
the shared digest uses the frozen selected root without resolving live paths during export.
Existing case IDs, old `local` material packets and their exact bindings are preserved.
The public Reader rejects a selected root whose ancestor is subsequently redirected by a symlink.

- Regression first failed on the original store mismatch, then passed through real CLI compression, save, recall, full-case expansion and materials, including hidden slots and a nonzero text offset.
- Full source regression with an isolated test-only pytest 9.2.0: **954 passed, 1 skipped, 441 subtests passed** (Python 3.12.14, `TZ=UTC`, 45.28s). Initial stdlib-only discovery could not import two pytest-based modules; the complete pytest run resolves that test-environment gap. Runtime dependencies are unchanged.
- Source-release smoke passed: synthetic source, existing cache and home unchanged; no installation or release publication.
- SpecMesh's optional native regression passed with the original case identity used directly in its binding. Its full suite passed **125 tests** with the explicit local checkout supplied; the pre-fix material export and original binding from the prior verification still matched.
- Independent review and six focused assertions passed; no remaining P1/P2 finding. Both repositories passed `git diff --check`.

Source delivery uses ordinary fast-forward `main` pushes with remote-head readback; receipts are reported in the conversation. This does not claim a Windows device trial or current private-history verification.
