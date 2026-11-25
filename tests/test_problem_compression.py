"""Problem extraction on isolated synthetic indexes, never personal histories."""
import json
from pathlib import Path
import tempfile
import unittest

from history_core import HistoryReader
from history_core.problem_compression import BODY_PAGE_CHARS, EXCERPT_CHARS, compress
from history_core.problem_cases import _normalize
from history_core.sources import Indexer
import test_uniform_brief as native_fixtures


class ProblemCompressionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        sessions, cache = self.root / "sessions", self.root / "cache"
        sessions.mkdir(); cache.mkdir()
        self.indexer = Indexer(sessions, cache, "codex")
        self.addCleanup(self.indexer.conn.close)
        with self.indexer.conn:
            self.indexer.conn.execute("INSERT INTO sessions(id,title,cwd,start_ts_ms,end_ts_ms,value_score) "
                                     "VALUES('chosen','Synthetic','/fixture',1,999,0)")
        self.position = 0

    def message(self, text, role="user", kind="message", *, call_id=None, summary=None):
        with self.indexer.conn:
            self.indexer.conn.execute("INSERT INTO messages(session_id,ts_ms,role,kind,text,activity_meta_json,tool_summary_json) "
                "VALUES('chosen',?,?,?,?,?,?)", (self.position + 1, role, kind, text,
                    json.dumps({"tool_call_id": call_id}) if call_id else "{}",
                    json.dumps(summary) if summary else None))
        index = self.position
        self.position += 1
        return index

    def packet(self, **kwargs):
        return compress(self.indexer, "chosen", source=self.indexer.source, store_id="isolated-store",
                        index_revision="snapshot-one", system="linux", **kwargs)

    def native(self, source):
        return native_fixtures.UniformBriefTests.native(self, source)

    def test_context_candidates_expose_actual_missing_records_and_stop_conditions(self):
        self.message('检查灯光插件')
        self.message('check-lighting', 'assistant', 'tool_use', call_id='missing-result')
        self.message('先暂停，等待输出图')
        case = self.packet()['cases'][0]
        candidates = {item['id']: item for item in case['context_candidates']}
        self.assertEqual(candidates['tool-result']['evidence'], ['m1'])
        self.assertEqual(candidates['stop-condition']['kind'], 'project')
        self.assertEqual(candidates['stop-condition']['current_applicability'], 'unknown')
        self.assertEqual(case['now']['task_status'], 'unknown')

    def test_repeated_failure_is_a_tool_check_candidate_not_an_executable_retry(self):
        self.message('检查程序')
        for identity in ('one', 'two'):
            self.message('DO_NOT_RUN_THIS', 'assistant', 'tool_use', call_id=identity, summary={'name': 'shell'})
            self.message('failed', 'tool', 'tool_result', call_id=identity, summary={'exit_code': 1})
        case = self.packet()['cases'][0]
        repeated = next(item for item in case['context_candidates'] if item['id'] == 'repeated-failure')
        self.assertEqual(repeated['kind'], 'tool')
        self.assertEqual(repeated['evidence'], ['m1', 'm2', 'm3', 'm4'])
        self.assertNotIn('DO_NOT_RUN_THIS', json.dumps(repeated))

    def test_plain_request_has_no_automatic_context_gap(self):
        self.message('说明当前函数的参数')
        self.message('参数来自函数签名。', 'assistant')
        self.assertEqual(self.packet()['cases'][0]['context_candidates'], [])

    def test_problem_turns_keep_correction_failed_attempt_and_success_claim_separate(self):
        self.message("让灯光插件改变当前场景")
        self.message("exec setup", "assistant", "tool_use", call_id="a", summary={"name": "shell"})
        self.message("failed", "tool", "tool_result", call_id="a", summary={"name": "shell", "exit_code": 1})
        self.message("不对，还是没有用，需要检查是否调用插件")
        self.message("方案是先核查插件调用", "assistant")
        self.message("exec inspect", "assistant", "tool_use", call_id="b", summary={"name": "shell"})
        self.message("command finished", "tool", "tool_result", call_id="b", summary={"name": "shell", "exit_code": 0})
        self.message("已经完成", "assistant")
        self.message("另外，给我整理背景素材")
        self.message("已整理", "assistant")
        packet = self.packet()
        self.assertEqual(len(packet["cases"]), 2)
        case = packet["cases"][0]
        self.assertEqual(case["now"]["task_status"], "unknown")
        self.assertEqual([attempt["status"] for attempt in case["attempts"]], ["error", "ok"])
        self.assertTrue(all(attempt["pairing"] == "matched_call_id" for attempt in case["attempts"]))
        kinds = {signal["kind"] for signal in case["signals"]}
        self.assertTrue({"tool_error", "user_correction_or_continuation_candidate", "assistant_completion_claim"} <= kinds)
        self.assertEqual(len(case["requirements"]), 2)
        self.assertEqual(case["stopping"]["reason"], "unknown")
        self.assertEqual(case["stopping"]["boundary"], "next_request")
        for candidate in packet["cases"]:
            _, warnings = _normalize(candidate)
            self.assertEqual(warnings, [])

    def test_success_wording_in_tool_output_does_not_replace_structured_failure(self):
        self.message("检查测试")
        self.message("all tests passed but failed wrapper", "tool", "tool_result", summary={"exit_code": 7})
        self.message("completed", "assistant")
        case = self.packet()["cases"][0]
        self.assertEqual(case["attempts"][0]["status"], "error")
        self.assertEqual(case["now"]["adopted"], "unknown")
        self.assertEqual(case["now"]["task_status"], "unknown")

    def test_failed_historical_command_is_not_promoted_to_reuse_steps(self):
        self.message("检查程序")
        self.message("DO_NOT_RECOMMEND_THIS_COMMAND", "assistant", "tool_use", call_id="failed")
        self.message("failure", "tool", "tool_result", call_id="failed", summary={"exit_code": 9})
        case = self.packet()["cases"][0]
        self.assertIn("DO_NOT_RECOMMEND_THIS_COMMAND", case["attempts"][0]["input_excerpt"])
        self.assertNotIn("DO_NOT_RECOMMEND_THIS_COMMAND", json.dumps(case["reuse"]))
        self.assertIn("核对", case["reuse"]["steps"][0])
        self.assertEqual(case["now"]["adopted"], "unknown")

    def test_signal_and_evolution_excerpt_center_the_actual_match_with_correct_offsets(self):
        request = "请检查 " + "x" * 1500 + " 卡住具体节点 " + "z" * 200
        response = "y" * 1700 + " 已经完成 " + "q" * 100
        self.message(request)
        self.message(response, "assistant")
        case = self.packet()["cases"][0]
        friction = next(signal for signal in case["signals"] if signal["kind"] == "friction_candidate")
        completion = next(signal for signal in case["signals"] if signal["kind"] == "assistant_completion_claim")
        for signal, body, marker in ((friction, request, "卡住具体节点"), (completion, response, "已经完成")):
            self.assertIn(marker, signal["text"])
            self.assertGreater(signal["text_offset"], 360)
            self.assertEqual(signal["text_offset_basis"], "indexed_text")
            self.assertEqual(signal["text"], body[signal["text_offset"]:signal["text_offset"] + EXCERPT_CHARS])
        event = next(event for event in case["evolution"] if event["kind"] == "assistant_completion_claim")
        self.assertEqual(event["text"], completion["text"])
        self.assertEqual(event["text_offset"], completion["text_offset"])
        # The ordinary evidence excerpt still expands from the first omitted
        # character, independently of the focused signal's display window.
        self.assertEqual(case["evidence"][1]["next_text_offset"], EXCERPT_CHARS)

    def test_stop_and_wait_are_attributed_wording_candidates(self):
        self.message("检查服务")
        self.message("等待用户提供日志", "assistant")
        self.message("停止任何工作，先保存证据")
        case = self.packet()["cases"][0]
        self.assertEqual({candidate["kind"] for candidate in case["stopping"]["candidates"]},
                         {"waiting_wording", "user_stop_request"})
        self.assertTrue(all(candidate["basis"] == "wording" for candidate in case["stopping"]["candidates"]))
        self.assertEqual(case["stopping"]["reason"], "unknown")
        self.assertEqual(case["now"]["task_status"], "unknown")

    def test_negated_stop_is_not_an_observed_stop(self):
        self.message("先不要停止，继续排查")
        self.message("刚才停过，现在继续")
        for case in self.packet()["cases"]:
            self.assertEqual(case["stopping"]["candidates"], [])
            self.assertEqual(case["stopping"]["reason"], "unknown")

    def test_page_boundary_is_not_a_task_boundary_or_cross_page_tool_pair(self):
        self.message("修复启动")
        self.message("start", "assistant", "tool_use", call_id="same")
        self.message("ok", "tool", "tool_result", call_id="same", summary={"exit_code": 0})
        self.message("继续吧")
        self.message("done", "assistant")
        first = self.packet(limit=2)
        self.assertTrue(first["has_more"])
        self.assertTrue(first["partial"])
        self.assertEqual(first["next_start"], 2)
        self.assertEqual(first["cases"][0]["attempts"][0]["pairing"], "result_not_in_page")
        second = self.packet(start=first["next_start"])
        self.assertIsNone(second["next_start"])
        self.assertTrue(second["partial"])
        case = second["cases"][0]
        self.assertTrue(case["compression"]["boundary"]["starts_mid_problem"])
        self.assertIsNone(case["compression"]["boundary"]["request_anchor"])
        self.assertEqual(case["attempts"][0]["pairing"], "call_not_in_page")
        self.assertNotEqual(first["cases"][0]["id"], case["id"])

    def test_stable_request_id_does_not_depend_on_page_limit_or_index_revision(self):
        self.message("保存同一请求")
        self.message("进度", "assistant")
        first = self.packet(limit=1)["cases"][0]["id"]
        self.assertEqual(first, self.packet(limit=100)["cases"][0]["id"])
        other_revision = compress(self.indexer, "chosen", source="codex", store_id="isolated-store",
                                  index_revision="snapshot-two")["cases"][0]["id"]
        self.assertEqual(first, other_revision)

    def test_partial_capture_and_provider_coverage_are_not_lost_on_case_save(self):
        self.message("检查问题")
        capture = {"status": "captured", "captured_through": 64, "observed_size": 80,
                   "trailing_bytes": 16, "partial": True, "path": "do-not-copy"}
        with self.indexer.conn:
            self.indexer.conn.execute("UPDATE sessions SET activity_metadata_json=? WHERE id='chosen'",
                                      (json.dumps({"source_capture": capture}),))
        self.indexer._coverage = {"chosen": {"content_status": "partial", "unsupported_steps": ["unknown"]}}
        packet = self.packet()
        self.assertTrue(packet["partial"])
        self.assertEqual(packet["source_capture"]["trailing_bytes"], 16)
        self.assertNotIn("do-not-copy", json.dumps(packet))
        case = packet["cases"][0]
        self.assertTrue(case["compression"]["partial"])
        self.assertIn("source_capture_partial", case["compression"]["reasons"])
        self.assertIn("source_content_partial", case["compression"]["reasons"])
        normalized, _ = _normalize(case)
        self.assertEqual(normalized["compression"]["source_capture"]["observed_size"], 80)

    def test_projected_call_id_collision_never_pairs_and_retains_loss_coverage(self):
        source = self.root / "projected-source"
        source.mkdir()
        payloads = [
            ("session_meta", {"id": "projected", "cwd": "/fixture"}),
            ("response_item", {"type": "message", "role": "user", "content": [{"text": "检查问题"}]}),
            ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "a" * 70000,
                               "arguments": '{"cmd":"echo fixture"}'}),
            ("response_item", {"type": "function_call_output", "call_id": "b" * 70000,
                               "output": "Exit code: 0\nOutput:\nok"}),
        ]
        path = source / "projected.jsonl"
        path.write_text("".join(json.dumps({"type": kind, "payload": payload,
                         "timestamp": "2026-11-22T00:00:00Z"}, ensure_ascii=False) + "\n"
                         for kind, payload in payloads))
        original = path.read_bytes()
        with HistoryReader("codex", source, self.root / "projected-cache", capture_mode="projected") as reader:
            receipt = reader.refresh()
            capture = receipt["sessions"][0]["source_capture"]
            self.assertFalse(capture["partial"])  # Whole bytes captured, but fields omitted.
            self.assertEqual(capture["lossy_fields"], 2)
            packet = reader.compress("projected")
            self.assertTrue(packet["partial"])
            self.assertEqual(packet["source_capture"]["capture_mode"], "projected")
            self.assertEqual(packet["source_capture"]["index_coverage"], "partial")
            self.assertEqual(packet["source_capture"]["omitted_bytes"], 140000)
            case = packet["cases"][0]
            self.assertTrue(case["compression"]["partial"])
            self.assertIn("source_projection_partial", case["compression"]["reasons"])
            self.assertEqual(len(case["attempts"]), 2)
            self.assertTrue(all(attempt["pairing"] == "projected_call_id_unavailable"
                                for attempt in case["attempts"]))
            self.assertTrue(all(len(attempt["evidence"]) == 1 for attempt in case["attempts"]))
            normalized, _ = _normalize(case)
            self.assertEqual(normalized["compression"]["source_capture"]["lossy_fields"], 2)
        self.assertEqual(path.read_bytes(), original)

    def test_projection_skipped_records_and_images_remain_visible_without_private_metadata(self):
        self.message("检查公开记录")
        for projection in (
            {"index_coverage": "partial", "skipped_lines": 1},
            {"index_coverage": "images_omitted", "image_fields": 1},
        ):
            with self.subTest(projection=projection):
                capture = {"status": "captured", "partial": False, "capture_mode": "projected",
                           "projection_version": 1, "audit_coverage": "projected_records", **projection,
                           "identity": {"ino": 999}, "path": "PRIVATE_PATH_NOT_EXPORTED"}
                with self.indexer.conn:
                    self.indexer.conn.execute("UPDATE sessions SET activity_metadata_json=? WHERE id='chosen'",
                                              (json.dumps({"source_capture": capture}),))
                packet = self.packet()
                self.assertTrue(packet["partial"])
                self.assertTrue(packet["cases"][0]["compression"]["partial"])
                self.assertEqual(packet["source_capture"]["index_coverage"], projection["index_coverage"])
                self.assertNotIn("PRIVATE_PATH_NOT_EXPORTED", json.dumps(packet))
                self.assertNotIn("identity", packet["source_capture"])

    def test_compact_evidence_preserves_expandable_offsets_and_hidden_analysis_limit(self):
        self.message("请求 " + "长" * 2500 + "已完成")
        self.message("普通过程解释 " + "a" * 900, "assistant")
        self.message("another ordinary note", "assistant")
        packet = self.packet()
        case = packet["cases"][0]
        evidence = case["evidence"][0]
        self.assertEqual(len(evidence["text"]), EXCERPT_CHARS)
        self.assertEqual(evidence["next_text_offset"], EXCERPT_CHARS)
        self.assertEqual(evidence["analysis_window_chars"], BODY_PAGE_CHARS)
        self.assertTrue(evidence["analysis_window_truncated"])
        self.assertTrue(evidence["truncated"])
        self.assertIn(1, case["compression"]["omitted_non_signal_message_indexes"])
        self.assertNotIn("普通过程解释", json.dumps(packet, ensure_ascii=False))
        self.assertTrue(packet["partial"])
        self.assertEqual(case["compression"]["inspected_message_count"], 3)
        self.assertEqual(case["compression"]["retained_message_count"], 2)

    def test_secrets_are_masked_before_extracting_title_requirement_and_tool_fields(self):
        self.message("检查 token=SECRET_123456789 然后修复")
        self.message("token=OTHER_SECRET", "assistant", "tool_use", call_id="safe",
                     summary={"name": "shell", "headline": "token=OTHER_SECRET"})
        self.message("没有结果", "assistant")
        raw = json.dumps(self.packet(), ensure_ascii=False)
        self.assertNotIn("SECRET_123456789", raw)
        self.assertNotIn("OTHER_SECRET", raw)

    def test_tool_ids_never_pair_across_different_problem_segments(self):
        self.message("第一个问题")
        self.message("do one", "assistant", "tool_use", call_id="one")
        self.message("另外，第二个问题")
        self.message("ok", "tool", "tool_result", call_id="one", summary={"exit_code": 0})
        cases = self.packet()["cases"]
        self.assertEqual(len(cases), 2)
        self.assertEqual([case["attempts"][0]["pairing"] for case in cases], ["outside_problem_segment"] * 2)

    def test_native_projection_retains_public_slots_and_never_reads_private_reasoning(self):
        for source in ("opencode", "zcode", "hermes"):
            with self.subTest(source=source):
                idx, path = self.native(source)
                before = path.read_bytes()
                packet = compress(idx, "chosen", source=source, store_id=source,
                                  index_revision="native-snapshot")
                self.assertEqual([evidence["message_index"] for case in packet["cases"] for evidence in case["evidence"]], [0, 3])
                text = json.dumps(packet)
                self.assertNotIn("HIDDEN_ONLY", text)
                self.assertNotIn("PRIVATE_REASONING", text)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(packet["cases"][0]["attempts"][0]["pairing"], "missing_call_id")
                self.assertEqual(packet["cases"][0]["now"]["task_status"], "unknown")

    def test_native_untimestamped_records_are_disclosed_without_raw_fallback(self):
        idx, _ = self.native("hermes")
        # The native source stays read-only; this synthetic source is opened by
        # a separate writer before the next compression read begins.
        import sqlite3
        with sqlite3.connect(self.root / "hermes.db") as connection:
            connection.execute("INSERT INTO messages VALUES(99,'chosen',NULL,'user','request without time','','','')")
        packet = compress(idx, "chosen", source="hermes", store_id="native", index_revision="native-snapshot")
        self.assertTrue(packet["partial"])
        self.assertIn("native_untimestamped_slots_not_projected", packet["coverage"]["reasons"])

    def test_no_messages_and_missing_session_are_distinct(self):
        packet = self.packet()
        self.assertEqual(packet["cases"], [])
        self.assertFalse(packet["has_more"])
        with self.assertRaisesRegex(ValueError, "session_not_found"):
            compress(self.indexer, "absent", source="codex", store_id="x", index_revision="rev")

    def test_invalid_bounds_and_identity_do_not_start_reads(self):
        for kwargs, error in (({"start": -1}, "invalid_start"), ({"start": True}, "invalid_start"),
                              ({"limit": 0}, "invalid_limit"), ({"limit": 101}, "invalid_limit"),
                              ({"limit": True}, "invalid_limit")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, error):
                self.packet(**kwargs)
        with self.assertRaisesRegex(ValueError, "source_identity_mismatch"):
            compress(self.indexer, "chosen", source="claude", store_id="x", index_revision="r")
        with self.assertRaisesRegex(ValueError, "index_revision_required"):
            compress(self.indexer, "chosen", source="codex", store_id="x", index_revision="")


if __name__ == "__main__":
    unittest.main()
