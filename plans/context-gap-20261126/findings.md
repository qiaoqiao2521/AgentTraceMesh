# Findings

- Baseline main: `71be82bc4b8b65001c752702b24fa6711d1d1079`.
- Existing compression already retains original message/revision identities,
  corrections, tool results and stop candidates. It cannot infer current intent
  or prove a requirement complete.
- Existing recall is lexical, case-first and falls back only to an explicitly
  selected history index. Share it instead of building another search system.
- The missing layer is current-task gap selection and bounded retrieval receipts,
  not new storage. The caller judges relevance; rules expose observable gaps.
- SpecMesh already imports exact-bound native materials. A context recovery
  receipt is a candidate plan, not a replacement for that evidence interface.
- Optional private knowledge workflow configuration is unavailable in this cloud
  environment. No private knowledge source or user computer was accessed.
- Review found that the brief reader lacked system/store identity. It now uses
  the same source_store_id as compression/materials inside the selected read view;
  the real CLI regression compares identities against exported material items.
- Preserve bounded source diagnostics, validate all queries before the first
  retrieval, reject descriptive-only available evidence, and reserve a fallback
  history candidate when partial cases fill the display budget. These defects
  were reproduced and fixed, with original sources left unchanged.
- Handoff text must include expandable case/message identities, not only a generic
  instruction to inspect candidates. Both text variants now retain those locators.
- The pre-existing date-filter fixture assumes UTC. Its failure was reproduced on
  untouched `71be82b` under this host's local timezone. Full acceptance uses explicit
  `TZ=UTC`, as in the earlier compression plan; production date semantics and that
  unrelated fixture were not modified.
