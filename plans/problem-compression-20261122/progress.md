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

Implementation is complete and ready for the requested PR against `main`.
The PR receipt is reported in the conversation; merging, production rollout and real-history acceptance are outside this source delivery.

Knowledge closeout: `pending` — configured knowledge mapping is unavailable in this cloud environment; owner is the user's configured knowledge workflow. Repository documentation will record this feature's contracts.
