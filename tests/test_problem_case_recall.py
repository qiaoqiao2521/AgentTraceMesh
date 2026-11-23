"""Generated case ownership and bounded lexical recall; synthetic data only."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_core import problem_cases
from history_core.problem_cases import get_case, recall_cases, save_case, save_generated_cases


class ProblemCaseRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "cases"
        self.examples = self.root / "examples"
        self.examples.mkdir()
        examples = patch.object(problem_cases, "EXAMPLE_DIRECTORY", self.examples)
        examples.start()
        self.addCleanup(examples.stop)

    def case(self, identity="draft-one"):
        return {
            "id": identity, "title": "灯光插件没有生效", "question": "舞台灯光插件加载失败后如何检查？",
            "project": "/synthetic/project", "now": {"task_status": "unknown", "remaining": ["效果尚待验证"]},
            "reuse": {"conditions": ["相同插件与软件版本"], "steps": ["核对调用记录中的操作符"]},
            "stopping": {"reason": "unknown", "boundary": "indexed_end"},
            "attempts": [{"tool": "stage.setup", "status": "ok", "input_excerpt": "token=synthetic-secret-1",
                          "current_verification": "not_performed"}],
            "evidence": [{"id": "m1", "kind": "message", "source": "codex", "system": "local",
                          "store_id": "store-a", "session_id": "session-a", "message_index": 1,
                          "index_revision": "revision-1", "text": "已调用，效果未核验"}],
        }

    def packet(self, *cases):
        return {"cases": list(cases), "partial": False}

    def test_identical_generation_does_not_rewrite_redacted_normalized_case(self):
        draft = self.case()
        first = save_generated_cases(self.directory, self.packet(draft))
        self.assertEqual(first["saved"], 1)
        path = self.directory / "draft-one.json"
        content, modified = path.read_bytes(), path.stat().st_mtime_ns
        repeated = save_generated_cases(self.directory, self.packet(draft))
        self.assertEqual(repeated["unchanged"], 1)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (content, modified))
        self.assertNotIn(b"synthetic-secret-1", content)
        shown = get_case(self.directory, "draft-one")
        self.assertEqual(shown["interpretation"], "rule_derived_draft")
        self.assertIn("规则提炼的待核对草稿", shown["markdown"])
        self.assertNotIn("generation", draft)

    def test_new_recorded_evidence_updates_only_the_same_untouched_source(self):
        draft = self.case()
        save_generated_cases(self.directory, self.packet(draft))
        updated = copy.deepcopy(draft)
        updated["evidence"][0]["index_revision"] = "revision-2"
        updated["now"]["remaining"] = ["新增回执仍待验证"]
        result = save_generated_cases(self.directory, self.packet(updated))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(get_case(self.directory, "draft-one")["case"]["evidence"][0]["index_revision"], "revision-2")
        cross_source = copy.deepcopy(updated)
        cross_source["evidence"][0]["store_id"] = "another-store"
        protected = save_generated_cases(self.directory, self.packet(cross_source))
        self.assertEqual(protected["protected"], 1)
        self.assertEqual(protected["items"][0]["reason"], "source_identity_changed")

    def test_direct_and_explicit_authored_edits_are_never_overwritten(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                directory = self.directory / str(explicit)
                draft = self.case()
                save_generated_cases(directory, self.packet(draft))
                path = directory / "draft-one.json"
                changed = json.loads(path.read_text())
                changed["now"]["judgment"] = "人工确认，应先检查对象选择"
                changed["reuse"]["steps"] = ["先选择目标对象"]
                if explicit:
                    save_case(directory, changed)
                else:
                    path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
                before = path.read_bytes()
                result = save_generated_cases(directory, self.packet(draft))
                self.assertEqual(result["protected"], 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(get_case(directory, "draft-one")["interpretation"], "authored_annotation")

    def test_raw_edits_cannot_be_hidden_by_normalization_or_redaction(self):
        for field in ("default", "secret"):
            with self.subTest(field=field):
                directory = self.directory / field
                save_generated_cases(directory, self.packet(self.case()))
                path = directory / "draft-one.json"
                data = json.loads(path.read_text())
                if field == "default":
                    del data["now"]["knowledge_value"]
                else:
                    data["attempts"][0]["input_excerpt"] = "token=manually-edited-secret"
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                before = path.read_bytes()
                self.assertEqual(save_generated_cases(directory, self.packet(self.case()))["protected"], 1)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(get_case(directory, "draft-one")["interpretation"], "authored_annotation")
                self.assertEqual(recall_cases(directory, query="灯光")["items"][0]["interpretation"], "authored_annotation")

    def test_narrower_new_revision_cannot_discard_a_saved_problem_tail(self):
        draft = self.case()
        draft["compression"] = {"boundary": {"first_message_index": 0, "last_message_index": 20,
                                             "request_anchor": 0}, "partial": False}
        save_generated_cases(self.directory, self.packet(draft))
        path = self.directory / "draft-one.json"
        before = path.read_bytes()
        narrower = copy.deepcopy(draft)
        narrower["compression"]["boundary"]["last_message_index"] = 4
        narrower["compression"]["partial"] = True
        narrower["evidence"][0]["index_revision"] = "newer-revision"
        result = save_generated_cases(self.directory, self.packet(narrower))
        self.assertEqual(result["items"][0]["reason"], "narrower_evidence_window")
        self.assertEqual(result["protected"], 1)
        self.assertEqual(path.read_bytes(), before)
        wider = copy.deepcopy(draft)
        wider["compression"]["boundary"]["last_message_index"] = 25
        self.assertEqual(save_generated_cases(self.directory, self.packet(wider))["updated"], 1)

    def test_unknown_author_and_bundled_example_keep_their_explanations(self):
        authored = self.case()
        save_case(self.directory, authored)
        before = (self.directory / "draft-one.json").read_bytes()
        self.assertEqual(save_generated_cases(self.directory, self.packet(authored))["protected"], 1)
        self.assertEqual((self.directory / "draft-one.json").read_bytes(), before)
        example = self.case("example")
        save_case(self.examples, example)
        result = save_generated_cases(self.directory, self.packet(example))
        self.assertEqual(result["items"][0]["reason"], "existing_example_case")
        self.assertFalse((self.directory / "example.json").exists())

    def test_edit_while_temporary_is_written_is_detected_before_replacement(self):
        draft = self.case()
        save_generated_cases(self.directory, self.packet(draft))
        path = self.directory / "draft-one.json"
        edited = json.loads(path.read_text())
        edited["title"] = "期间人工修改"
        real_fsync = os.fsync
        edited_during_save = []

        def modify_then_sync(fd):
            if not edited_during_save:
                path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
                edited_during_save.append(True)
            return real_fsync(fd)

        updated = self.case()
        updated["title"] = "程序重新生成"
        with patch.object(problem_cases.os, "fsync", side_effect=modify_then_sync):
            result = save_generated_cases(self.directory, self.packet(updated))
        self.assertEqual(result["protected"], 1)
        self.assertEqual(json.loads(path.read_text())["title"], "期间人工修改")
        self.assertEqual([item.name for item in self.directory.iterdir()], ["draft-one.json"])

    def test_invalid_generated_item_does_not_prevent_other_saved_cases(self):
        result = save_generated_cases(self.directory, self.packet({"id": "../escape", "title": "bad"}, self.case()))
        self.assertEqual((result["error"], result["saved"]), (1, 1))
        self.assertTrue(result["partial"])
        self.assertFalse((self.root / "escape.json").exists())

    def test_case_created_after_absence_check_is_not_replaced(self):
        real_link = os.link
        written = []

        def create_then_link(source, target, **kwargs):
            Path(target).write_text(json.dumps({"id": "draft-one", "title": "并发人工案例"}), encoding="utf-8")
            written.append(True)
            return real_link(source, target, **kwargs)

        with patch.object(problem_cases.os, "link", side_effect=create_then_link):
            result = save_generated_cases(self.directory, self.packet(self.case()))
        self.assertTrue(written)
        self.assertEqual(result["protected"], 1)
        self.assertEqual(json.loads((self.directory / "draft-one.json").read_text())["title"], "并发人工案例")

    def test_recall_prioritizes_problem_match_then_authored_case_and_keeps_unknowns(self):
        save_generated_cases(self.directory, self.packet(self.case("a-generated")))
        authored = self.case("z-authored")
        authored["now"]["judgment"] = "人工解释已保留"
        save_case(self.directory, authored)
        incidental = self.case("incidental")
        incidental["title"] = incidental["question"] = "另一个无关问题"
        incidental["evidence"][0]["text"] = "灯光插件没有生效"
        save_case(self.directory, incidental)
        result = recall_cases(self.directory, query="灯光插件没有生效")
        self.assertEqual([item["id"] for item in result["items"]], ["z-authored", "a-generated", "incidental"])
        self.assertEqual(result["items"][1]["now"]["task_status"], "unknown")
        self.assertEqual(result["items"][1]["stopping"]["reason"], "unknown")
        self.assertEqual(result["current_verification"], "not_performed")
        self.assertEqual(result["coverage"], "saved_case_catalog_only")
        self.assertEqual(result["items"][0]["evidence"][0]["index_revision"], "revision-1")
        self.assertNotIn("synthetic-secret-1", json.dumps(result))

    def test_recall_chinese_bigrams_english_tokens_and_project_filter(self):
        save_case(self.directory, self.case())
        english = self.case("english")
        english.update(title="SSH timeout after deployment", question="Connection fails", project="/other")
        save_case(self.directory, english)
        chinese = recall_cases(self.directory, query="灯光加载", project="/synthetic/project")
        self.assertEqual([item["id"] for item in chinese["items"]], ["draft-one"])
        self.assertIn("灯光", chinese["items"][0]["match"]["matched_terms"])
        self.assertEqual([item["id"] for item in recall_cases(self.directory, query="ssh TIMEOUT")["items"]], ["english"])
        self.assertEqual(recall_cases(self.directory, query="ssh TIMEOUT", project="/synthetic/project")["items"], [])

    def test_bounded_recall_retains_case_partial_and_broken_overlay(self):
        draft = self.case()
        draft["compression"] = {"partial": True}
        draft["reuse"]["steps"] = ["检验" * 1000] * 20
        draft["evidence"] *= 20
        save_generated_cases(self.directory, self.packet(draft))
        example = self.case("example")
        save_case(self.examples, example)
        (self.directory / "example.json").write_text("{broken", encoding="utf-8")
        result = recall_cases(self.directory, query="灯光", limit=1)
        self.assertEqual([item["id"] for item in result["items"]], ["draft-one"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["errors"])
        item = result["items"][0]
        self.assertLessEqual(len(item["evidence"]), 3)
        self.assertLessEqual(len(item["reuse"]["steps"]), 3)
        self.assertLessEqual(len(item["reuse"]["steps"][0]), 280)
        self.assertEqual(item["expand"]["case_id"], "draft-one")

    def test_recall_preserves_source_truncation_offsets_and_marks_short_display(self):
        draft = self.case()
        draft["evidence"][0].update(text="示" * 360, truncated=True, partial=True,
                                    analysis_window_truncated=True, char_count=2000, text_offset=0,
                                    next_text_offset=360, binding="index_revision")
        save_generated_cases(self.directory, self.packet(draft))
        evidence = recall_cases(self.directory, query="灯光")["items"][0]["evidence"][0]
        self.assertEqual(len(evidence["text"]), 280)
        self.assertTrue(evidence["display_excerpt_truncated"])
        self.assertTrue(evidence["truncated"])
        self.assertTrue(evidence["partial"])
        self.assertTrue(evidence["analysis_window_truncated"])
        self.assertEqual(evidence["next_text_offset"], 360)
        self.assertEqual(evidence["char_count"], 2000)
        self.assertEqual(evidence["binding"], "index_revision")

    def test_recall_scan_budget_is_explicit(self):
        save_case(self.directory, self.case("a"))
        save_case(self.directory, self.case("b"))
        with patch.object(problem_cases, "_MAX_RECALL_CASES", 1):
            result = recall_cases(self.directory, query="灯光")
        self.assertTrue(result["partial"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["scanned_cases"], 1)

    def test_recall_does_not_create_directories_and_validates_request_bounds(self):
        self.assertEqual(recall_cases(self.directory, query="灯光")["items"], [])
        self.assertFalse(self.directory.exists())
        for query, limit in (("", 5), ("x" * 301, 5), ("x", 0), ("x", True), ("x", 21)):
            with self.subTest(query=query, limit=limit), self.assertRaises(ValueError):
                recall_cases(self.directory, query=query, limit=limit)


if __name__ == "__main__":
    unittest.main()
