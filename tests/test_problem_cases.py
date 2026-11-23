"""Synthetic, editable problem cases; no native histories or models."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_core import problem_cases
from history_core.problem_cases import default_case_directory, get_case, list_cases, save_case


class ProblemCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.directory = self.base / "editable"
        self.examples = self.base / "examples"
        self.examples.mkdir()
        examples_patch = patch.object(problem_cases, "EXAMPLE_DIRECTORY", self.examples)
        examples_patch.start()
        self.addCleanup(examples_patch.stop)

    def data(self, identity="feedback"):
        return {
            "id": identity, "title": "三路反馈怎样改变方案", "project": "/synthetic/project",
            "question": "如何找出改变判断的专业纠偏？", "tags": ["反馈", "视频"],
            "now": {"judgment": "先按问题看解释，再看证据", "adopted": ["问题案例"],
                    "task_status": "历史报告，当前未复测", "knowledge_value": "有条件的复用候选",
                    "applicability": ["用户提供了明确专业纠偏"], "remaining": ["执行新的迁移试验"]},
            "requirements": [{"text": "保留用户纠偏", "state": "authored", "evidence": ["message-1"]}],
            "evolution": [
                {"id": "early", "kind": "proposal", "actor": "primary_assistant",
                 "text": "只给历史分页 paging", "evidence": ["message-1"]},
                {"id": "correction", "kind": "correction", "actor": "user",
                 "text": "分页不能说明为什么改变，先展示纠正后的判断", "evidence": ["message-1"],
                 "replaces": ["early"]}],
            "reuse": {"conditions": ["问题与条件相似"], "steps": ["先核对本次问题的条件，再选证据"],
                      "avoid": ["不要把早期建议当现行要求"], "unknowns": ["并非普遍效果证明"]},
            "evidence": [{"id": "message-1", "kind": "message", "system": "local",
                          "source": "codex", "store_id": "synthetic-store", "session_id": "native@recording-old",
                          "index_revision": "old-index-revision", "content_revision": "unknown",
                          "context_revision": "unknown", "message_index": 2,
                          "text_offset": 4000, "text_offset_basis": "indexed_text",
                          "locator": {"path": "/missing/synthetic.jsonl", "raw_ref": {"line_no": 7}},
                          "text": "这条证据并未在当前任务重新验证"}],
        }

    def write(self, root, data):
        root.mkdir(parents=True, exist_ok=True)
        path = root / (data["id"] + ".json")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_default_directory_and_get_do_not_create_directories(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.base / "data"),
                                     "LOCALAPPDATA": str(self.base / "data")}):
            expected = self.base / "data" / "agenttracemesh" / "problem-cases"
            self.assertEqual(default_case_directory(), expected)
            self.assertEqual(list_cases()["items"], [])
            with self.assertRaisesRegex(ValueError, "case_not_found"):
                get_case(None, "missing")
            self.assertFalse(expected.exists())
        self.assertFalse(self.directory.exists())

    def test_save_read_update_same_id_and_canonical_markdown(self):
        data = self.data()
        first = save_case(self.directory, data)
        self.assertEqual(first["origin"], "local")
        self.assertEqual(first["editable_path"], str(self.directory / "feedback.json"))
        self.assertEqual(first["markdown"], get_case(self.directory, "feedback")["markdown"])
        self.assertEqual(first["case"]["evidence"], data["evidence"])
        for key, value in (("interpretation", "authored_annotation"),
                           ("source_verification", "references_not_revalidated"),
                           ("current_verification", "not_performed")):
            self.assertEqual(first[key], value)
        data["now"]["judgment"] = "人工确认了新条件"
        updated = save_case(self.directory, data)
        self.assertEqual(updated["case"]["now"]["judgment"], "人工确认了新条件")
        self.assertEqual(list(self.directory.iterdir()), [self.directory / "feedback.json"])
        self.assertIn("## 现在怎样判断", updated["markdown"])
        self.assertIn("## 为什么改变", updated["markdown"])
        self.assertIn("## 下次怎样做", updated["markdown"])

    def test_minimum_fields_default_to_unknown_and_keep_unresolved_cases(self):
        result = save_case(self.directory, {"id": "open", "title": "尚无结论"})
        case = result["case"]
        self.assertEqual(case["question"], "unknown")
        self.assertEqual(case["now"]["judgment"], "unknown")
        self.assertEqual(case["now"]["task_status"], "unknown")
        self.assertEqual(case["reuse"]["steps"], "unknown")
        self.assertIn("case_evidence_missing", result["warnings"])
        self.assertFalse(result["partial"])
        self.assertEqual(list_cases(self.directory)["items"][0]["next_action"], "unknown")

    def test_redact_annotations_and_evidence_body_without_changing_identity(self):
        data = self.data()
        data["question"] = 'password="private-question"'
        data["now"]["remaining"] = ["api_key=private-note"]
        data["evidence"][0]["text"] = "token=private-evidence <script>alert(1)</script> ```"
        identity = {key: value for key, value in data["evidence"][0].items() if key != "text"}
        result = save_case(self.directory, data)
        persisted = (self.directory / "feedback.json").read_text()
        for secret in ("private-question", "private-note", "private-evidence"):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, persisted)
        actual = {key: value for key, value in result["case"]["evidence"][0].items() if key != "text"}
        self.assertEqual(actual, identity)
        self.assertIn("````text", result["markdown"])
        self.assertIn("<script>alert(1)</script>", result["markdown"])
        self.assertEqual(data["question"], 'password="private-question"')

    def test_nonfinite_local_json_is_isolated_from_other_cases_and_http_json(self):
        save_case(self.directory, self.data("good"))
        expected_errors = set()
        for name, number in (("nan", float("nan")), ("infinity", float("inf")),
                             ("negative-infinity", -float("inf"))):
            identity = "bad-" + name
            data = self.data(identity)
            data["now"]["task_status"] = number
            self.write(self.directory, data)
            expected_errors.add(identity)
        result = list_cases(self.directory)
        self.assertTrue(result["partial"])
        self.assertEqual([item["id"] for item in result["items"]], ["good"])
        self.assertEqual({error["id"] for error in result["errors"]}, expected_errors)
        json.dumps(result, allow_nan=False)
        for identity in expected_errors:
            shown = get_case(self.directory, identity)
            self.assertTrue(shown["partial"])
            self.assertIsNone(shown["case"])
            json.dumps(shown, allow_nan=False)

    def test_nonfinite_save_is_value_error_without_creating_data_directory(self):
        for number in (float("nan"), float("inf"), -float("inf")):
            data = self.data()
            data["now"]["task_status"] = number
            with self.subTest(number=number), self.assertRaises(ValueError):
                save_case(self.directory, data)
            self.assertFalse(self.directory.exists())

    def test_structured_secret_values_and_secret_keys_are_masked_in_all_exits(self):
        data = self.data()
        secrets = ("SYNTHETIC_SECRET_9281", "SYNTHETIC_KEY_SECRET_9282", "SYNTHETIC_BODY_SECRET_9283")
        data["now"]["adopted"] = [{"password": secrets[0]}, {"token=" + secrets[1]: "metadata"}]
        data["authorization"] = "context_only"
        data["evidence"][0]["authorization"] = "context_only"
        data["evidence"][0]["body"] = {"access_token": secrets[2]}
        preserved = {key: data["evidence"][0][key] for key in
                     ("id", "source", "session_id", "store_id", "index_revision", "locator")}
        saved = save_case(self.directory, data)
        shown = get_case(self.directory, "feedback")
        listed = list_cases(self.directory)
        outputs = [json.dumps(value) for value in (saved, shown, listed)]
        outputs.append((self.directory / "feedback.json").read_text())
        for secret in secrets:
            for output in outputs:
                self.assertNotIn(secret, output)
        self.assertEqual(saved["case"]["authorization"], "context_only")
        self.assertEqual(shown["case"]["evidence"][0]["authorization"], "context_only")
        for key, value in preserved.items():
            self.assertEqual(shown["case"]["evidence"][0][key], value)

    def test_secret_fields_mask_complete_values_for_all_json_shapes(self):
        data = self.data()
        data["now"]["remaining"] = [
            {"password": {"value": "SYNTHETIC_OBJECT_SECRET_2985"}},
            {"token": [{"value": "SYNTHETIC_LIST_SECRET_2986"}]},
            {"client_secret": 57402987},
        ]
        data["authorization"] = "context_only"
        data["evidence"][0]["authorization"] = "context_only"
        data["evidence"][0]["body"] = {"api_key": {"value": "SYNTHETIC_BODY_SECRET_2988"}}
        identity = {key: value for key, value in data["evidence"][0].items()
                    if key not in ("body", "authorization")}

        saved = save_case(self.directory, data)
        shown = get_case(self.directory, "feedback")
        listed = list_cases(self.directory)
        outputs = [json.dumps(value) for value in (saved, shown, listed)]
        outputs.append((self.directory / "feedback.json").read_text())
        for secret in ("SYNTHETIC_OBJECT_SECRET_2985", "SYNTHETIC_LIST_SECRET_2986",
                       "57402987", "SYNTHETIC_BODY_SECRET_2988"):
            for output in outputs:
                self.assertNotIn(secret, output)
        self.assertEqual(saved["case"]["now"]["remaining"], [
            {"password": "[REDACTED]"}, {"token": "[REDACTED]"},
            {"client_secret": "[REDACTED]"},
        ])
        self.assertEqual(shown["case"]["authorization"], "context_only")
        self.assertEqual(shown["case"]["evidence"][0]["authorization"], "context_only")
        for key, value in identity.items():
            self.assertEqual(shown["case"]["evidence"][0][key], value)

    def test_direct_json_edits_are_visible_without_refresh_or_hash_lock(self):
        save_case(self.directory, self.data())
        path = self.directory / "feedback.json"
        data = json.loads(path.read_text())
        data["title"] = "人工改名"
        data["now"]["judgment"] = "人工二改立即生效"
        data["custom_human_field"] = "仍保留自由字段"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        read = get_case(self.directory, "feedback")
        self.assertEqual(read["case"]["now"]["judgment"], "人工二改立即生效")
        self.assertEqual(read["case"]["custom_human_field"], "仍保留自由字段")
        self.assertEqual(list_cases(self.directory, query="人工二改")["total"], 1)

    def test_earlier_fragment_query_still_returns_correction_and_next_action(self):
        save_case(self.directory, self.data())
        result = list_cases(self.directory, query="paging")
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["now"]["judgment"], "先按问题看解释，再看证据")
        self.assertEqual(item["next_action"], "先核对本次问题的条件，再选证据")
        detail = get_case(self.directory, item["id"])
        self.assertIn("分页不能说明为什么改变", detail["markdown"])
        self.assertEqual(detail["case"]["evolution"][1]["replaces"], ["early"])
        self.assertNotIn("value_score", item)

    def test_examples_overlay_and_invalid_local_override_are_explicit(self):
        self.write(self.examples, self.data())
        example = get_case(self.directory, "feedback")
        self.assertEqual(example["origin"], "example")
        self.assertEqual(example["editable_path"], str(self.directory / "feedback.json"))
        self.assertFalse(self.directory.exists())
        data = self.data()
        data["title"] = "本地覆写"
        self.write(self.directory, data)
        self.assertEqual(get_case(self.directory, "feedback")["origin"], "local")
        self.assertEqual(list_cases(self.directory)["items"][0]["title"], "本地覆写")
        (self.directory / "feedback.json").write_text("{broken")
        listing = list_cases(self.directory)
        self.assertTrue(listing["partial"])
        self.assertEqual(listing["items"], [])
        self.assertEqual(listing["errors"][0]["id"], "feedback")
        broken = get_case(self.directory, "feedback")
        self.assertTrue(broken["partial"])
        self.assertIsNone(broken["case"])
        self.assertEqual(broken["origin"], "local")

    def test_bad_json_and_nonregular_files_do_not_hide_other_cases(self):
        save_case(self.directory, self.data("good"))
        (self.directory / "broken.json").write_text("{bad")
        (self.directory / "folder.json").mkdir()
        result = list_cases(self.directory)
        self.assertTrue(result["partial"])
        self.assertEqual([item["id"] for item in result["items"]], ["good"])
        self.assertEqual({error["id"] for error in result["errors"]}, {"broken", "folder"})
        missing_hits = list_cases(self.directory, query="absentword")
        self.assertEqual(missing_hits["total"], 0)
        self.assertTrue(missing_hits["partial"])
        self.assertTrue(missing_hits["errors"])

    def test_filename_id_mismatch_is_reported_not_loaded_as_another_case(self):
        path = self.write(self.directory, self.data("requested"))
        path.write_text(json.dumps(self.data("different")))
        self.assertIsNone(get_case(self.directory, "requested")["case"])
        result = list_cases(self.directory)
        self.assertEqual(result["items"], [])
        self.assertIn("case_filename_id_mismatch", result["errors"][0]["detail"])

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"), "requires POSIX FIFO")
    def test_nonregular_case_is_rejected_without_blocking(self):
        save_case(self.directory, self.data("good"))
        fifo = self.directory / "fifo.json"
        os.mkfifo(fifo)
        real_open = os.open

        def checked_open(path, flags, *args, **kwargs):
            if str(path) == fifo.name:
                self.assertTrue(flags & os.O_NONBLOCK)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(problem_cases.os, "open", side_effect=checked_open):
            result = list_cases(self.directory)
        self.assertEqual([item["id"] for item in result["items"]], ["good"])
        self.assertTrue(result["partial"])
        self.assertIn("case_regular_file_required", result["errors"][0]["detail"])

    def test_pagination_project_filter_and_stable_id_order(self):
        for identity in ("c", "a", "b"):
            save_case(self.directory, self.data(identity))
        first = list_cases(self.directory, limit=2)
        self.assertEqual([item["id"] for item in first["items"]], ["a", "b"])
        self.assertEqual((first["next_offset"], first["total"]), (2, 3))
        second = list_cases(self.directory, offset=2, limit=2)
        self.assertEqual([item["id"] for item in second["items"]], ["c"])
        self.assertIsNone(second["next_offset"])
        self.assertEqual(list_cases(self.directory, project="another")["total"], 0)
        self.assertEqual(list_cases(self.directory, project="/synthetic/project")["total"], 3)
        for offset, limit in ((-1, 2), (0, 0), (True, 2), (0, False)):
            with self.subTest(offset=offset, limit=limit), self.assertRaisesRegex(ValueError, "pagination"):
                list_cases(self.directory, offset=offset, limit=limit)

    def test_missing_evidence_and_unknown_relationship_are_warnings(self):
        data = self.data()
        data["evolution"][1]["evidence"] = ["missing-evidence"]
        data["evolution"][1]["replaces"] = ["unknown-proposal"]
        result = save_case(self.directory, data)
        self.assertTrue(any("missing_evidence_reference" in warning for warning in result["warnings"]))
        self.assertTrue(any("unknown_replacement_reference" in warning for warning in result["warnings"]))
        self.assertFalse(result["partial"])
        self.assertEqual(result["case"]["now"]["judgment"], data["now"]["judgment"])

    def test_markdown_renders_the_authored_views_and_compact_artifact_locator(self):
        data = self.data()
        data["evidence"] = [{"id": "message-1", "kind": "artifact",
                             "repository": "qiaoqiao2521/TraceMesh", "path": "docs/example.md",
                             "commit": "historical-commit", "line_start": 10, "line_end": 20,
                             "text": "这是当时记录，当前未核验"}]
        result = save_case(self.directory, data)
        markdown = result["markdown"]
        self.assertIn("- 问题案例", markdown)
        self.assertIn("记录角色：user", markdown)
        self.assertIn("显式替代：early", markdown)
        self.assertIn("状态：authored", markdown)
        self.assertIn("qiaoqiao2521/TraceMesh / docs/example.md@historical-commit；行 10–20", markdown)
        self.assertIn("这是当时记录，当前未核验", markdown)
        self.assertNotIn('"repository":', markdown)
        self.assertNotIn('"replaces":', markdown)
        self.assertEqual(result["case"]["evidence"], data["evidence"])

    def test_saved_reference_does_not_revalidate_history_or_execute_commands(self):
        data = self.data()
        marker = self.base / "must-not-exist"
        data["evolution"][0]["text"] = f"touch {marker}"
        with patch("history_core.evidence.selection_bundle", side_effect=AssertionError("no live evidence")), patch(
                "history_core.indexed_materials.indexed_materials", side_effect=AssertionError("no historical refresh")):
            saved = save_case(self.directory, data)
            shown = get_case(self.directory, data["id"])
            listed = list_cases(self.directory)
        self.assertFalse(marker.exists())
        self.assertEqual(shown["case"]["evidence"][0]["index_revision"], "old-index-revision")
        self.assertEqual(saved["markdown"], shown["markdown"])
        self.assertEqual(listed["current_verification"], "not_performed")

    def test_invalid_ids_and_symlinks_cannot_escape_the_case_directory(self):
        for identity in ("../outside", "/absolute", "a/b", "a\\b", ".", "..", "a:stream"):
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "case_id_invalid"):
                save_case(self.directory, {"id": identity, "title": "不应写出"})
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, "case_id_invalid"):
                get_case(self.directory, identity)
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(self.data()))
        self.directory.mkdir()
        link = self.directory / "feedback.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        before = outside.read_bytes()
        with self.assertRaisesRegex(ValueError, "symlink"):
            save_case(self.directory, self.data())
        shown = get_case(self.directory, "feedback")
        self.assertTrue(shown["partial"])
        self.assertIsNone(shown["case"])
        self.assertEqual(outside.read_bytes(), before)
        self.assertTrue(list_cases(self.directory)["partial"])

    def test_failed_atomic_replace_preserves_previous_edit_and_removes_temporary(self):
        save_case(self.directory, self.data())
        original = (self.directory / "feedback.json").read_bytes()
        data = self.data()
        data["title"] = "不会取代旧文件"
        with patch.object(problem_cases.os, "replace", side_effect=OSError("replace interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                save_case(self.directory, data)
        self.assertEqual((self.directory / "feedback.json").read_bytes(), original)
        self.assertEqual(list(self.directory.iterdir()), [self.directory / "feedback.json"])

    def test_without_directory_fd_save_get_update_use_absolute_file_paths(self):
        real_open, real_replace = os.open, os.replace
        replacements = []

        def open_file(path, flags, *args, **kwargs):
            self.assertNotIn("dir_fd", kwargs)
            self.assertNotEqual(Path(path), self.directory)
            self.assertTrue(Path(path).is_absolute())
            return real_open(path, flags, *args, **kwargs)

        def replace_file(source, destination, **kwargs):
            self.assertEqual(kwargs, {})
            self.assertEqual(Path(source).parent, self.directory)
            self.assertEqual(Path(destination), self.directory / "feedback.json")
            replacements.append((source, destination))
            return real_replace(source, destination)

        with patch.object(problem_cases, "_supports_directory_fd", return_value=False), patch.object(
                problem_cases.os, "open", side_effect=open_file), patch.object(
                problem_cases.os, "replace", side_effect=replace_file):
            first = save_case(self.directory, self.data())
            self.assertEqual(get_case(self.directory, "feedback")["markdown"], first["markdown"])
            data = self.data()
            data["title"] = "普通路径更新"
            updated = save_case(self.directory, data)
            self.assertEqual(get_case(self.directory, "feedback")["case"]["title"], "普通路径更新")
            self.assertEqual(list_cases(self.directory)["items"][0]["title"], "普通路径更新")
        self.assertEqual(len(replacements), 2)
        self.assertEqual(first["editable_path"], updated["editable_path"])

    def test_without_directory_fd_replace_failure_keeps_previous_and_cleans_temporary(self):
        with patch.object(problem_cases, "_supports_directory_fd", return_value=False):
            save_case(self.directory, self.data())
            original = (self.directory / "feedback.json").read_bytes()
            data = self.data()
            data["title"] = "替换失败应保留旧件"
            with patch.object(problem_cases.os, "replace", side_effect=OSError("fallback replace interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    save_case(self.directory, data)
            self.assertEqual((self.directory / "feedback.json").read_bytes(), original)
            self.assertEqual(list(self.directory.iterdir()), [self.directory / "feedback.json"])

    def test_without_directory_fd_keeps_symlink_and_nonregular_protection(self):
        self.directory.mkdir()
        target = self.directory / "feedback.json"
        target.mkdir()
        with patch.object(problem_cases, "_supports_directory_fd", return_value=False):
            with self.assertRaisesRegex(ValueError, "regular_file_required"):
                save_case(self.directory, self.data())
        target.rmdir()
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(self.data()))
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        original = outside.read_bytes()
        with patch.object(problem_cases, "_supports_directory_fd", return_value=False):
            with self.assertRaisesRegex(ValueError, "case_file_symlink"):
                save_case(self.directory, self.data())
            self.assertTrue(get_case(self.directory, "feedback")["partial"])
        self.assertEqual(outside.read_bytes(), original)

    def test_exclusive_temporary_collision_does_not_remove_existing_file(self):
        self.directory.mkdir()
        existing = self.directory / ".case-fixed.tmp"
        existing.write_text("existing unrelated temporary")
        with patch.object(problem_cases, "_supports_directory_fd", return_value=False), patch.object(
                problem_cases.secrets, "token_hex", return_value="fixed"):
            with self.assertRaises(FileExistsError):
                save_case(self.directory, self.data())
        self.assertEqual(existing.read_text(), "existing unrelated temporary")

    def test_malformed_optional_fields_default_without_losing_the_case(self):
        data = {"id": "partial-shape", "title": "宽容可选字段", "now": "manual note",
                "reuse": None, "tags": "not a list", "requirements": {}, "evolution": None,
                "evidence": {"text": "not a list"}}
        result = save_case(self.directory, data)
        self.assertEqual(result["case"]["now"]["judgment"], "unknown")
        self.assertTrue(result["warnings"])
        self.assertFalse(result["partial"])
        self.assertEqual(list_cases(self.directory)["total"], 1)


if __name__ == "__main__":
    unittest.main()
