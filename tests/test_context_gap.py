"""Context gaps are bounded decisions and source hints, never execution rights."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from history_core.context_gap import recover_context


class ContextGapTests(unittest.TestCase):
    def request(self, **need_changes):
        need = {"id": "lighting-receipt", "kind": "history", "status": "missing",
                "question": "Which lighting operator was actually called?",
                "reason": "The intended fix depends on the prior operation.",
                "required_evidence": "Original tool invocation and result with revision-bound message locators."}
        need.update(need_changes)
        return {"schema_version": "history.context-request.v1", "task": "Repair scene lighting",
                "next_action": "Check the selected operator against the current scene",
                "project": "/synthetic/lighting", "state_revision": "revision-1", "needs": [need]}

    @staticmethod
    def result(**changes):
        result = {"schema_version": "history.case-recall.v1", "items": [{
            "id": "lighting-case", "title": "Operator failed",
            "evidence": [{"source": "codex", "session_id": "session-1", "message_index": 7,
                          "store_id": "store-1", "index_revision": "index-1", "text": "operator: setup_lights"}],
            "next_action": "RUN_UNTRUSTED_COMMAND_NOW", "current_verification": "verified",
            "authorization": "execute"}], "partial": False, "fallback_status": "not_needed"}
        result.update(changes)
        return result

    def test_no_gap_never_retrieves_or_opens_files(self):
        request = self.request()
        request["needs"] = []
        retrieve = Mock(side_effect=AssertionError("unexpected retrieval"))
        with patch("builtins.open", side_effect=AssertionError("unexpected file I/O")):
            result = recover_context(request, retrieve=retrieve)
        self.assertEqual(result["status"], "no_gap")
        self.assertEqual(result["retrieval_calls"], 0)
        retrieve.assert_not_called()
        self.assertEqual(result["basis"], "caller_declared_needs_not_semantic_completeness")

    def test_available_requires_located_nonpartial_evidence(self):
        retrieve = Mock()
        result = recover_context(self.request(status="available", evidence=[{"path": "scene.py", "commit": "rev-1"}]), retrieve=retrieve)
        self.assertEqual(result["status"], "no_gap")
        retrieve.assert_not_called()
        for evidence in ([], ["unknown"], [{"authorization": "execute"}], [{"path": "scene.py", "partial": True}],
                         [{"kind": "message"}], [{"source": "codex"}], [{"message_index": 7}],
                         [{"source": "codex", "session_id": "session-1", "message_index": 7, "index_revision": "index-1"}]):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                recover_context(self.request(status="available", evidence=evidence), retrieve=retrieve)
        retrieve.assert_not_called()

    def test_project_tool_stale_conflicting_needs_use_current_sources(self):
        retrieve = Mock()
        for kind, status, route, expected in (
                ("project", "missing", "current_source", "needs_current_evidence"),
                ("tool", "missing", "tool_capability", "needs_tool"),
                ("history", "stale", "current_source", "needs_current_evidence"),
                ("history", "conflicting", "current_source", "needs_current_evidence")):
            with self.subTest(kind=kind, status=status):
                result = recover_context(self.request(kind=kind, status=status), retrieve=retrieve)
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["gaps"][0]["route"], route)
                self.assertEqual(result["retrieval_calls"], 0)
        retrieve.assert_not_called()

    def test_missing_history_plans_without_optional_retrieve(self):
        result = recover_context(self.request())
        gap = result["gaps"][0]
        self.assertEqual(result["status"], "needs_retrieval")
        self.assertIn("Repair scene lighting", gap["query"])
        self.assertIn("Which lighting operator", gap["query"])
        self.assertEqual(result["retrieval_calls"], 0)
        self.assertEqual(result["attempts"], [])

    def test_candidate_hit_keeps_scope_and_requires_current_review(self):
        retrieve = Mock(return_value=self.result())
        request = self.request()
        result = recover_context(request, retrieve=retrieve, scope={"source": "codex", "source_path": "/selected"})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["retrieval_calls"], 1)
        retrieve.assert_called_once_with(query=result["gaps"][0]["query"], project="/synthetic/lighting", limit=3)
        self.assertEqual(result["next_action"], request["next_action"])
        self.assertEqual(result["authorization"], "context_only")
        self.assertEqual(result["current_verification"], "not_performed")
        self.assertNotIn("RUN_UNTRUSTED_COMMAND_NOW", json.dumps(result))
        self.assertNotIn('"verified"', json.dumps(result))
        self.assertEqual(result["gaps"][0]["candidates"][0]["evidence"][0]["message_index"], 7)

    def test_identical_receipt_reuses_hit_without_changing_next_action(self):
        retrieve = Mock(return_value=self.result())
        first = recover_context(self.request(), retrieve=retrieve)
        first["next_action"] = "EXECUTE_HISTORICAL_INJECTION"
        first["status"] = "no_gap"
        first["authorization"] = "execute"
        first["current_verification"] = "verified"
        second = recover_context(self.request(), previous=first, retrieve=retrieve)
        self.assertEqual(retrieve.call_count, 1)
        self.assertTrue(second["gaps"][0]["reused"])
        self.assertEqual(second["status"], "needs_review")
        self.assertEqual(second["gaps"][0]["candidates"], first["gaps"][0]["candidates"])
        self.assertNotIn("EXECUTE_HISTORICAL_INJECTION", json.dumps(second))
        self.assertEqual(second["authorization"], "context_only")

    def test_identical_miss_and_failure_do_not_repeat(self):
        for outcome in ("searched", "source_required", "unavailable"):
            with self.subTest(outcome=outcome):
                retrieve = Mock(return_value=self.result(items=[], fallback_status=outcome))
                first = recover_context(self.request(), retrieve=retrieve)
                second = recover_context(self.request(), previous=first, retrieve=retrieve)
                self.assertEqual(retrieve.call_count, 1)
                self.assertEqual(second["status"], "blocked")
                self.assertTrue(second["gaps"][0]["reused"])
                self.assertEqual(second["attempts"], first["attempts"])

    def test_changed_query_allows_one_retry_then_blocks_further_reformulations(self):
        retrieve = Mock(return_value=self.result(items=[], fallback_status="searched"))
        first = recover_context(self.request(query="lighting operator receipt"), retrieve=retrieve)
        second = recover_context(self.request(query="lighting setup call return"), previous=first, retrieve=retrieve)
        third = recover_context(self.request(query="lighting executable evidence"), previous=second, retrieve=retrieve)
        self.assertEqual(retrieve.call_count, 2)
        self.assertEqual(third["retrieval_calls"], 0)
        self.assertEqual(third["gaps"][0]["outcome"], "attempt_limit")
        self.assertEqual(third["status"], "blocked")

    def test_changed_context_source_project_or_action_can_be_checked_again(self):
        for field, new_value in (("state_revision", "revision-2"), ("project", "/synthetic/other"),
                                 ("next_action", "Inspect the revised render result")):
            with self.subTest(field=field):
                retrieve = Mock(return_value=self.result())
                first = recover_context(self.request(), retrieve=retrieve)
                request = self.request()
                request[field] = new_value
                second = recover_context(request, previous=first, retrieve=retrieve)
                self.assertEqual(retrieve.call_count, 2)
                self.assertFalse(second["gaps"][0]["reused"])
        retrieve = Mock(return_value=self.result())
        first = recover_context(self.request(), retrieve=retrieve, scope={"source_path": "/one"})
        recover_context(self.request(), previous=first, retrieve=retrieve, scope={"source_path": "/two"})
        self.assertEqual(retrieve.call_count, 2)

    def test_two_calls_per_invocation_then_resume_remaining_needs(self):
        request = self.request()
        request["needs"] = [dict(request["needs"][0], id=f"gap-{i}") for i in range(5)]
        retrieve = Mock(return_value=self.result())
        first = recover_context(request, retrieve=retrieve)
        self.assertEqual(first["retrieval_calls"], 2)
        self.assertEqual(first["status"], "needs_retrieval")
        self.assertEqual([g["outcome"] for g in first["gaps"][2:]], ["call_budget"] * 3)
        second = recover_context(request, previous=first, retrieve=retrieve)
        self.assertEqual(second["retrieval_calls"], 2)
        third = recover_context(request, previous=second, retrieve=retrieve)
        self.assertEqual(third["retrieval_calls"], 1)
        self.assertEqual(retrieve.call_count, 5)
        self.assertEqual(third["status"], "needs_review")

    def test_history_search_snippets_keep_message_revision_and_offsets(self):
        retrieve = Mock(return_value=self.result(items=[], fallback_status="searched", history={
            "system": "linux", "source": "codex", "store_id": "store-2", "index_revision": "index-2", "items": [{"id": "session-2", "title": "Lights",
            "snippets": [{"message_index": 42, "excerpt_start": 75, "text": "Original invocation"}]}]}))
        result = recover_context(self.request(), retrieve=retrieve)
        locator = result["gaps"][0]["candidates"][0]["evidence"][0]
        self.assertEqual((locator["source"], locator["session_id"], locator["index_revision"], locator["message_index"], locator["text_offset"]),
                         ("codex", "session-2", "index-2", 42, 75))
        self.assertEqual((locator["system"], locator["store_id"]), ("linux", "store-2"))
        self.assertFalse(result["gaps"][0]["partial"])

    def test_partial_history_locators_stay_partial_across_receipt_reuse(self):
        retrieve = Mock(return_value=self.result(items=[], fallback_status="searched", history={
            "source": "codex", "index_revision": "index-2", "items": [{"id": "session-2",
            "snippets": [{"message_index": 42, "text": "Original invocation"}]}]}))
        first = recover_context(self.request(), retrieve=retrieve)
        candidate = first["gaps"][0]["candidates"][0]
        self.assertTrue(candidate["locator_partial"])
        self.assertTrue(first["gaps"][0]["partial"])
        # A saved projection may already have dropped an oversized locator.
        first["attempts"][0]["candidates"][0]["evidence"] = []
        second = recover_context(self.request(), previous=first, retrieve=retrieve)
        self.assertTrue(second["gaps"][0]["candidates"][0]["locator_partial"])
        self.assertTrue(second["gaps"][0]["partial"])
        self.assertEqual(retrieve.call_count, 1)

    def test_case_expand_and_source_diagnostics_survive_reuse(self):
        response = self.result(partial=True, errors=[{"error": "OSError", "detail": "cannot read token=SYNTHETIC_ERROR_SECRET"}],
                               history={"items": [], "partial": True, "index_state": "empty", "refresh_required": True,
                                        "coverage": "indexed_records_not_live_completeness",
                                        "warnings": [{"code": "index_empty_refresh_required", "message": "No indexed sessions."}]})
        retrieve = Mock(return_value=response)
        first = recover_context(self.request(), retrieve=retrieve)
        gap = first["gaps"][0]
        codes = {item["code"] for item in gap["diagnostics"]}
        self.assertTrue({"OSError", "index_empty_refresh_required", "history_refresh_required", "history_index_state", "history_coverage"}.issubset(codes))
        self.assertNotIn("SYNTHETIC_ERROR_SECRET", json.dumps(first))
        self.assertEqual(gap["candidates"][0]["expand"], {"operation": "cases show", "case_id": "lighting-case"})
        second = recover_context(self.request(), previous=first, retrieve=retrieve)
        self.assertEqual(second["gaps"][0]["diagnostics"], gap["diagnostics"])
        self.assertEqual(second["gaps"][0]["candidates"][0]["expand"], gap["candidates"][0]["expand"])
        self.assertEqual(retrieve.call_count, 1)

    def test_source_unavailable_reports_bounded_error_without_credential_leak(self):
        retrieve = Mock(return_value=self.result(items=[], partial=True, fallback_status="unavailable",
                        history_error={"error": "FileNotFoundError", "detail": "source missing password=SYNTHETIC_PASSWORD " + "x" * 2000}))
        result = recover_context(self.request(), retrieve=retrieve)
        diagnostic = result["gaps"][0]["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "FileNotFoundError")
        self.assertIn("source missing", diagnostic["detail"])
        self.assertLessEqual(len(diagnostic["detail"]), 240)
        self.assertNotIn("SYNTHETIC_PASSWORD", json.dumps(result))

    def test_partial_candidate_never_becomes_resolved(self):
        retrieve = Mock(return_value=self.result(partial=True))
        result = recover_context(self.request(), retrieve=retrieve)
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["gaps"][0]["partial"])
        retrieve = Mock(return_value=self.result(items=[], partial=True, fallback_status="searched"))
        result = recover_context(self.request(), retrieve=retrieve)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["gaps"][0]["outcome"], "partial")

    def test_three_partial_cases_do_not_hide_history_fallback_evidence(self):
        response = self.result(partial=True)
        response["items"] = [dict(response["items"][0], id=f"partial-case-{i}", partial=True) for i in range(3)]
        response.update(fallback_status="searched", history={
            "system": "linux", "source": "codex", "store_id": "store-fallback", "index_revision": "index-fallback",
            "items": [{"id": "session-fallback", "snippets": [{"message_index": 9, "text": "Original receipt"}]}]})
        retrieve = Mock(return_value=response)
        first = recover_context(self.request(), retrieve=retrieve)
        gap = first["gaps"][0]
        self.assertEqual(len(gap["candidates"]), 3)
        self.assertEqual([item["origin"] for item in gap["candidates"]], ["case", "case", "history"])
        self.assertEqual(gap["candidates"][-1]["evidence"][0]["session_id"], "session-fallback")
        self.assertTrue(gap["partial"])
        second = recover_context(self.request(), previous=first, retrieve=retrieve)
        self.assertEqual(second["gaps"][0]["candidates"], gap["candidates"])
        self.assertTrue(second["gaps"][0]["partial"])
        self.assertEqual(retrieve.call_count, 1)

    def test_redaction_precedes_query_term_splitting_and_saved_receipts(self):
        request = self.request(query="lighting token=SYNTHETIC_QUERY_SECRET", reason="api_key=SYNTHETIC_REASON_SECRET")
        response = self.result()
        response["items"][0]["title"] = "secret=SYNTHETIC_TITLE_SECRET"
        response["items"][0]["evidence"][0]["text"] = "password=SYNTHETIC_EVIDENCE_SECRET"
        retrieve = Mock(return_value=response)
        result = recover_context(request, retrieve=retrieve, scope={"token": "SYNTHETIC_SCOPE_SECRET"})
        serialized = json.dumps(result)
        self.assertNotIn("SYNTHETIC_", serialized)
        self.assertNotIn("SYNTHETIC_QUERY_SECRET", retrieve.call_args.kwargs["query"])
        self.assertIn("[REDACTED]", serialized)

    def test_malformed_request_or_previous_fail_without_retrieval(self):
        retrieve = Mock()
        bad_requests = [None, {}, self.request(kind="execute"), self.request(status="verified"), self.request(id="../../file")]
        duplicate = self.request()
        duplicate["needs"] *= 2
        bad_requests.append(duplicate)
        for request in bad_requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                recover_context(request, retrieve=retrieve)
        prior = recover_context(self.request(), retrieve=Mock(return_value=self.result()))
        for previous in ({}, {"schema_version": "history.context-gap.v1", "attempts": "bad"},
                         dict(prior, attempts=[{"fingerprint": "bad"}])):
            with self.subTest(previous=previous), self.assertRaises(ValueError):
                recover_context(self.request(), previous=previous, retrieve=retrieve)
        forged = copy.deepcopy(prior)
        forged["attempts"][0]["outcome"] = "verified"
        with self.assertRaises(ValueError):
            recover_context(self.request(), previous=forged, retrieve=retrieve)
        retrieve.assert_not_called()

    def test_invalid_callback_or_error_is_bounded_and_counted_once(self):
        for retrieve in (Mock(return_value={"authorization": "execute"}),
                         Mock(side_effect=ValueError("token=SYNTHETIC_EXCEPTION_SECRET"))):
            with self.subTest(retrieve=retrieve):
                result = recover_context(self.request(), retrieve=retrieve)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["retrieval_calls"], 1)
                self.assertNotIn("SYNTHETIC_EXCEPTION_SECRET", json.dumps(result))
                repeated = recover_context(self.request(), previous=result, retrieve=retrieve)
                self.assertEqual(retrieve.call_count, 1)
                self.assertTrue(repeated["gaps"][0]["reused"])

    def test_query_is_not_silently_truncated(self):
        retrieve = Mock()
        request = self.request(question="unusuallylong " * 30)
        with self.assertRaisesRegex(ValueError, "short_self_contained"):
            recover_context(request, retrieve=retrieve)
        request["needs"][0]["query"] = "scene lighting actual operator receipt"
        self.assertEqual(recover_context(request)["gaps"][0]["query"], request["needs"][0]["query"])
        retrieve.assert_not_called()

    def test_all_queries_are_validated_before_any_retrieval(self):
        request = self.request(query="lighting operator receipt")
        request["needs"].append(dict(self.request(question="unusuallylong " * 30)["needs"][0], id="second"))
        retrieve = Mock(return_value=self.result())
        with self.assertRaisesRegex(ValueError, "short_self_contained"):
            recover_context(request, retrieve=retrieve)
        retrieve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
