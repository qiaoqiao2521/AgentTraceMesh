"""Independent compression counterexamples using synthetic public histories."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_core import HistoryReader


class CompressionReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def reader(self, name="first", *, call_ids=None):
        root = self.root / name
        source = root / "source"
        source.mkdir(parents=True)
        records = [{"type": "session_meta", "timestamp": "2026-11-20T00:00:00Z",
                    "payload": {"id": "same-session", "cwd": "/synthetic/lighting"}}]

        def add(payload):
            records.append({"type": "response_item",
                            "timestamp": f"2026-11-20T00:00:{len(records):02d}Z",
                            "payload": payload})

        def message(role, text):
            add({"type": "message", "role": role,
                 "content": [{"type": "input_text" if role == "user" else "output_text",
                              "text": text}]})

        message("user", "请检查灯光插件加载后没有效果的问题。")
        add({"type": "reasoning", "summary": [
            {"type": "summary_text", "text": "PRIVATE_REVIEW_SENTINEL"}]})
        for index, call_id in enumerate(call_ids or ["verified-call"]):
            add({"type": "function_call", "name": "exec_command", "call_id": call_id,
                 "arguments": json.dumps({"cmd": f"inspect-lighting-{index} token=SYNTHETIC_REVIEW_SECRET"})})
            add({"type": "function_call_output", "call_id": call_id,
                 "output": "Exit code: 0\nOutput:\noperator invoked; visible effect unchecked"})
        message("assistant", "已经修好了。")
        message("user", "不对，画面仍然没有变化，先停止。")
        message("user", "继续，先检查实际调用了哪个操作符。")
        path = source / "session.jsonl"
        path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                        encoding="utf-8")
        reader = HistoryReader("codex", source, root / "cache")
        self.addCleanup(reader.close)
        reader.refresh()
        return reader, path

    def test_public_evidence_keeps_hidden_slots_and_can_be_opened(self):
        reader, path = self.reader()
        original = path.read_bytes()
        packet = reader.compress("same-session")
        serialized = json.dumps(packet, ensure_ascii=False)
        self.assertNotIn("PRIVATE_REVIEW_SENTINEL", serialized)
        self.assertNotIn("SYNTHETIC_REVIEW_SECRET", serialized)
        self.assertTrue(packet["cases"])
        positions = set()
        for case in packet["cases"]:
            for evidence in case["evidence"]:
                if evidence["kind"] != "message":
                    continue
                positions.add(evidence["message_index"])
                self.assertEqual(evidence["source"], "codex")
                self.assertEqual(evidence["session_id"], "same-session")
                self.assertEqual(evidence["index_revision"], packet["index_revision"])
                self.assertTrue(evidence["store_id"])
                opened = reader.message(evidence["session_id"], evidence["message_index"],
                                        index_revision=evidence["index_revision"],
                                        text_offset=evidence.get("text_offset", 0))
                self.assertEqual(opened["message_index"], evidence["message_index"])
                self.assertNotIn("PRIVATE_REVIEW_SENTINEL", opened["text"])
        self.assertIn(0, positions)
        self.assertIn(5, positions)
        self.assertNotIn(1, positions)
        self.assertEqual(path.read_bytes(), original)

    def test_same_session_id_in_different_stores_cannot_overwrite_cases(self):
        first, _ = self.reader("first")
        second, _ = self.reader("second")
        left = first.compress("same-session")
        right = second.compress("same-session")
        self.assertTrue(left["cases"] and right["cases"])
        self.assertTrue({case["id"] for case in left["cases"]}.isdisjoint(
            {case["id"] for case in right["cases"]}))

    def test_case_identity_survives_unrelated_refresh_but_old_evidence_expires(self):
        reader, path = self.reader()
        before = reader.compress("same-session")
        other = [
            {"type": "session_meta", "timestamp": "2026-11-21T00:00:00Z",
             "payload": {"id": "unrelated-session", "cwd": "/synthetic/other"}},
            {"type": "response_item", "timestamp": "2026-11-21T00:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "Unrelated request"}]}}
        ]
        (path.parent / "unrelated.jsonl").write_text(
            "\n".join(json.dumps(record) for record in other) + "\n", encoding="utf-8")
        reader.refresh()
        after = reader.compress("same-session")
        self.assertNotEqual(before["index_revision"], after["index_revision"])
        self.assertEqual([case["id"] for case in before["cases"]],
                         [case["id"] for case in after["cases"]])
        with self.assertRaisesRegex(ValueError, "index_revision_changed"):
            reader.message("same-session", 0, index_revision=before["index_revision"])

    def test_generated_drafts_are_idempotent_after_redaction_and_preserve_manual_edit(self):
        from history_core.problem_cases import save_generated_cases

        reader, _ = self.reader()
        packet = reader.compress("same-session")
        directory = self.root / "saved-cases"
        save_generated_cases(directory, packet)
        files = list(directory.glob("*.json"))
        self.assertEqual(len(files), len(packet["cases"]))
        baseline = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        save_generated_cases(directory, packet)
        self.assertEqual(baseline,
                         {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in files})
        selected = files[0]
        edited = json.loads(selected.read_text(encoding="utf-8"))
        edited["now"]["judgment"] = "人工复核：只看到了操作符回执，画面效果没有通过。"
        edited["reuse"]["steps"] = ["先读取真实调用记录，再对比画面。"]
        selected.write_text(json.dumps(edited, ensure_ascii=False, indent=2), encoding="utf-8")
        expected = selected.read_bytes()
        save_generated_cases(directory, packet)
        self.assertEqual(selected.read_bytes(), expected)

    def test_tool_ids_that_are_ambiguous_or_lose_information_do_not_establish_pairs(self):
        fixtures = (
            ("duplicate", ["duplicate-id", "duplicate-id"], "ambiguous_call_id"),
            ("redacted", ["token=SYNTHETIC_ID_ONE", "token=SYNTHETIC_ID_TWO"],
             "redacted_or_truncated_call_id"),
            ("truncated", ["p" * 128 + "ONE", "p" * 128 + "TWO"],
             "redacted_or_truncated_call_id"),
        )
        for name, call_ids, pairing in fixtures:
            with self.subTest(name=name):
                reader, _ = self.reader(name, call_ids=call_ids)
                packet = reader.compress("same-session")
                attempts = [attempt for case in packet["cases"] for attempt in case["attempts"]]
                self.assertEqual(len(attempts), 4)
                self.assertEqual({attempt["pairing"] for attempt in attempts}, {pairing})
                self.assertTrue(all(attempt["task_success"] == "unknown" for attempt in attempts))
                self.assertTrue(all(case["now"]["task_status"] == "unknown" for case in packet["cases"]))
                if name == "redacted":
                    for value in ("SYNTHETIC_ID_ONE", "SYNTHETIC_ID_TWO"):
                        self.assertNotIn(value, json.dumps(packet))

    def test_recall_match_explanation_cannot_reintroduce_query_secrets(self):
        from history_core.problem_cases import recall_cases, save_generated_cases

        reader, _ = self.reader()
        directory = self.root / "saved-cases"
        save_generated_cases(directory, reader.compress("same-session"))
        result = recall_cases(directory, query="灯光 token=SYNTHETIC_RECALL_QUERY_SECRET")
        self.assertTrue(result["items"])
        self.assertNotIn("SYNTHETIC_RECALL_QUERY_SECRET", json.dumps(result))
        filtered = recall_cases(directory, query="灯光", project="token=SYNTHETIC_PROJECT_SECRET")
        self.assertNotIn("SYNTHETIC_PROJECT_SECRET", json.dumps(filtered))

    def test_edit_during_generated_save_is_detected_before_replacement(self):
        from history_core import problem_cases

        reader, _ = self.reader()
        packet = reader.compress("same-session")
        directory = self.root / "saved-cases"
        problem_cases.save_generated_cases(directory, packet)
        selected = directory / (packet["cases"][0]["id"] + ".json")
        updated = copy.deepcopy(packet)
        updated["cases"][0]["now"]["remaining"] = ["源会话有后续消息，需要更新草稿。"]
        edited = json.loads(selected.read_text(encoding="utf-8"))
        edited["now"]["judgment"] = "人工并发修订，必须保留。"
        expected = json.dumps(edited, ensure_ascii=False, indent=2).encode()
        real_fsync = os.fsync
        injected = False

        def write_manual_edit_after_temp_is_flushed(fd):
            nonlocal injected
            real_fsync(fd)
            if not injected:
                injected = True
                selected.write_bytes(expected)

        with patch.object(problem_cases.os, "fsync", side_effect=write_manual_edit_after_temp_is_flushed):
            report = problem_cases.save_generated_cases(directory, updated)
        self.assertTrue(injected)
        self.assertEqual(selected.read_bytes(), expected)
        self.assertTrue(report["partial"])
        self.assertEqual(report["protected"], 1)

    def test_case_save_authorship_is_protected_from_later_regeneration(self):
        from history_core.problem_cases import get_case, save_case, save_generated_cases

        reader, _ = self.reader()
        packet = reader.compress("same-session")
        directory = self.root / "saved-cases"
        save_generated_cases(directory, packet)
        identity = packet["cases"][0]["id"]
        authored = get_case(directory, identity)["case"]
        authored["now"]["judgment"] = "人工解释：程序运行成功，视觉效果尚未验收。"
        authored["reuse"]["avoid"] = ["避免只凭程序退出码就判定画面有效。"]
        save_case(directory, authored)
        selected = directory / (identity + ".json")
        expected = selected.read_bytes()
        save_generated_cases(directory, packet)
        self.assertEqual(selected.read_bytes(), expected)
        self.assertEqual(get_case(directory, identity)["interpretation"], "authored_annotation")


if __name__ == "__main__":
    unittest.main()
