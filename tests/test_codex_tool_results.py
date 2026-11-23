"""Codex tool evidence survives structured native result envelopes."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from history_core.providers import parser_version
from history_core.reader import HistoryReader
from history_core.sources import (_codex_format_tool_result, _codex_summarize_tool_result,
                                  parse_codex_session_file)


class CodexToolResultTests(unittest.TestCase):
    def test_native_object_matches_json_string(self):
        value = {"output": "READBACK_OK", "metadata": {"exit_code": 0, "duration_seconds": 0.25}}
        encoded = json.dumps(value)
        self.assertEqual(_codex_format_tool_result("shell_command", "c1", value),
                         _codex_format_tool_result("shell_command", "c1", encoded))
        self.assertEqual(_codex_summarize_tool_result("shell_command", value),
                         _codex_summarize_tool_result("shell_command", encoded))

    def test_list_text_blocks_keep_body_and_error(self):
        value = [{"type": "text", "text": "FIRST_RESULT"},
                 {"output": "FAILED_RESULT", "exit_code": 2}]
        text = _codex_format_tool_result("exec", "c1", value)
        self.assertIn("FIRST_RESULT", text)
        self.assertIn("FAILED_RESULT", text)
        self.assertIn("Exit code: 2", text)
        summary = _codex_summarize_tool_result("exec", value)
        self.assertTrue(summary["is_error"])
        self.assertEqual(summary["exit_code"], 2)

    def test_text_block_json_is_literal_not_result_metadata(self):
        raw = '{"output":"BODY_RESULT","exit_code":2,"isError":true}'
        value = {"content": [{"type": "text", "text": raw}]}
        self.assertIn(raw, _codex_format_tool_result("exec", raw_output=value))
        summary = _codex_summarize_tool_result("exec", value)
        self.assertEqual(summary["output_preview"], raw)
        self.assertIsNone(summary["exit_status"])
        self.assertIsNone(summary["exit_code"])

    def test_deep_json_list_has_bounded_fallback(self):
        raw = '[' * 500 + '{"type":"text","text":"DEPTH_OK"}' + ']' * 500
        self.assertIn("[unsupported tool output: nesting limit]", _codex_format_tool_result("exec", raw_output=raw))
        self.assertIsNone(_codex_summarize_tool_result("exec", raw)["exit_status"])

    def test_content_envelope_and_explicit_error_without_exit_code(self):
        value = {"content": [{"type": "text", "text": "ACCESS_DENIED"}], "isError": True}
        self.assertIn("ACCESS_DENIED", _codex_format_tool_result("read", raw_output=value))
        summary = _codex_summarize_tool_result("read", value)
        self.assertEqual(summary["exit_status"], "error")
        self.assertTrue(summary["is_error"])
        self.assertIsNone(summary["exit_code"])

    def test_error_overrides_zero_exit_code(self):
        value = {"output": "failure detail", "metadata": {"exit_code": 0}, "error": {"message": "TOOL_DENIED"}}
        text = _codex_format_tool_result("read", raw_output=value)
        self.assertIn("Status: error", text)
        self.assertIn("TOOL_DENIED", text)
        self.assertTrue(_codex_summarize_tool_result("read", value)["is_error"])

    def test_failure_in_batch_is_not_hidden_by_later_success(self):
        value = [{"output": "FAILED", "exit_code": 3}, {"output": "OK", "exit_code": 0}]
        summary = _codex_summarize_tool_result("exec", value)
        self.assertEqual(summary["exit_code"], 3)
        self.assertEqual(summary["exit_status"], "error")

    def test_plain_text_metadata_matches_expanded_and_collapsed_status(self):
        text = _codex_format_tool_result("shell_command", raw_output="Exit code: 1\nWall time: 0.1 seconds\nOutput:\nFAILED")
        self.assertIn("Status: error", text)
        self.assertIn("Wall time: 0.1 seconds", text)

    def test_plain_wrapper_metadata_does_not_scan_business_stdout(self):
        body = "PUBLIC_BUSINESS_TRACE\nExit code: 7\nWall time: 999 seconds"
        for header, expected_time in (("Exit code: 0\nWall time: 0.1 seconds", "0.1 seconds"),
                                      ("Exit code: 0", None)):
            with self.subTest(header=header):
                raw = header + "\nOutput:\n" + body
                text = _codex_format_tool_result("shell_command", raw_output=raw)
                summary = _codex_summarize_tool_result("shell_command", raw)
                self.assertIn(body, text)
                self.assertIn("Status: ok\nExit code: 0", text)
                self.assertEqual(summary["exit_code"], 0)
                self.assertFalse(summary["is_error"])
                if expected_time is None:
                    self.assertNotIn("Wall time:", text.split("Output:", 1)[0])
                else:
                    self.assertIn("Wall time: " + expected_time, text.split("Output:", 1)[0])

    def test_media_payloads_are_not_expanded_or_previewed(self):
        content = [{"type": "text", "text": "VISIBLE_TEXT"},
                   {"type": "image", "data": "BASE64_SENTINEL", "mimeType": "image/png"},
                   {"type": "audio", "data": "BASE64_SENTINEL"},
                   {"type": "resource", "resource": {"blob": "BASE64_SENTINEL"}}]
        for value in (content, json.dumps(content)):
            with self.subTest(value_type=type(value).__name__):
                text = _codex_format_tool_result("mcp", raw_output=value)
                self.assertIn("VISIBLE_TEXT", text)
                self.assertIn("[image content omitted]", text)
                self.assertIn("[audio content omitted]", text)
                self.assertIn("[resource content omitted]", text)
                self.assertNotIn("BASE64_SENTINEL", text)
                self.assertNotIn("BASE64_SENTINEL", _codex_summarize_tool_result("mcp", value)["output_preview"])

    def test_empty_and_unknown_shapes_are_visible_without_invented_success(self):
        for value in (None, "", [], {"output": ""}):
            with self.subTest(value=value):
                self.assertIn("[empty tool output]", _codex_format_tool_result("exec", raw_output=value))
                self.assertIsNone(_codex_summarize_tool_result("exec", value)["exit_status"])
        for value in (42, {"unknown": "RAW_UNSUPPORTED"}, {"type": ["unknown"]}):
            with self.subTest(value=value):
                self.assertIn("[unsupported tool output:", _codex_format_tool_result("exec", raw_output=value))
                self.assertIsNone(_codex_summarize_tool_result("exec", value)["exit_status"])

    def test_ordinary_json_stdout_is_preserved_as_text(self):
        raw = '{"answer": 42}'
        self.assertIn(raw, _codex_format_tool_result("shell_command", raw_output=raw))

    def test_business_json_stdout_with_status_is_preserved(self):
        raw = '{"status":"healthy","result":"READBACK_OK"}'
        self.assertIn(raw, _codex_format_tool_result("shell_command", raw_output=raw))
        summary = _codex_summarize_tool_result("shell_command", raw)
        self.assertEqual(summary["output_preview"], raw)
        self.assertIsNone(summary["exit_status"])

    def test_business_json_with_envelope_named_fields_stays_literal(self):
        for key, value in (("text", "description"), ("content", "description"),
                           ("metadata", {"source": "synthetic"})):
            with self.subTest(key=key):
                raw = json.dumps({key: value, "result": "PUBLIC_BODY_LOST", "status": "error"})
                self.assertIn(raw, _codex_format_tool_result("shell_command", raw_output=raw))
                summary = _codex_summarize_tool_result("shell_command", raw)
                self.assertEqual(summary["output_preview"], raw)
                self.assertIsNone(summary["exit_status"])
                self.assertFalse(summary["is_error"])

    def test_native_business_json_keeps_fields_and_unknown_status(self):
        for key, value in (("text", "description"), ("content", "description"),
                           ("metadata", {"source": "synthetic"})):
            business = {key: value, "result": "PUBLIC_NATIVE_BODY_LOST", "status": "error"}
            for raw in (business, json.dumps(business)):
                with self.subTest(key=key, native=isinstance(raw, dict)):
                    text = _codex_format_tool_result("shell_command", raw_output=raw)
                    self.assertIn("PUBLIC_NATIVE_BODY_LOST", text)
                    body = text.split("````\n", 1)[1].rsplit("\n````", 1)[0]
                    self.assertEqual(json.loads(body), business)
                    summary = _codex_summarize_tool_result("shell_command", raw)
                    self.assertIn("PUBLIC_NATIVE_BODY_LOST", summary["output_preview"])
                    self.assertIsNone(summary["exit_status"])
                    self.assertFalse(summary["is_error"])

    def test_native_and_string_known_envelopes_and_blocks_remain_equivalent(self):
        fixtures = [
            {"output": "FAILED_KNOWN_RESULT", "exit_code": 2, "wall_time_seconds": 0.1},
            {"content": [{"type": "text", "text": "KNOWN_BLOCK_RESULT"}], "isError": True},
            {"type": "output_text", "text": "KNOWN_TYPED_RESULT"},
            [{"type": "input_text", "text": "KNOWN_LIST_RESULT"}],
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                encoded = json.dumps(fixture)
                self.assertEqual(_codex_format_tool_result("read", raw_output=fixture),
                                 _codex_format_tool_result("read", raw_output=encoded))
                self.assertEqual(_codex_summarize_tool_result("read", fixture),
                                 _codex_summarize_tool_result("read", encoded))

    def test_envelope_json_body_stays_literal(self):
        raw = '{"status":"healthy","result":"READBACK_OK"}'
        value = {"output": raw, "metadata": {"exit_code": 0}}
        for envelope in (value, json.dumps(value)):
            with self.subTest(envelope_type=type(envelope).__name__):
                self.assertIn(raw, _codex_format_tool_result("shell_command", raw_output=envelope))
                summary = _codex_summarize_tool_result("shell_command", envelope)
                self.assertEqual(summary["output_preview"], raw)
                self.assertEqual(summary["exit_status"], "ok")

    def test_envelope_object_body_keeps_application_fields(self):
        value = {"output": {"answer": "READBACK_OK", "status": "healthy", "error": "historical"},
                 "metadata": {"exit_code": 0}}
        for envelope in (value, json.dumps(value)):
            with self.subTest(envelope_type=type(envelope).__name__):
                text = _codex_format_tool_result("shell_command", raw_output=envelope)
                self.assertIn('"answer": "READBACK_OK"', text)
                self.assertIn('"status": "healthy"', text)
                self.assertIn('"error": "historical"', text)
                self.assertNotIn("unsupported", text)
                self.assertEqual(_codex_summarize_tool_result("shell_command", envelope)["exit_status"], "ok")

    def test_object_body_serialization_omits_nested_media_bytes(self):
        value = {"output": {"answer": "READBACK_OK", "attachment": {"type": "image", "data": "BASE64_SENTINEL"}},
                 "metadata": {"exit_code": 0}}
        for envelope in (value, json.dumps(value)):
            with self.subTest(envelope_type=type(envelope).__name__):
                text = _codex_format_tool_result("mcp", raw_output=envelope)
                self.assertIn("READBACK_OK", text)
                self.assertIn("[image content omitted]", text)
                self.assertNotIn("BASE64_SENTINEL", text)
                self.assertNotIn("BASE64_SENTINEL", _codex_summarize_tool_result("mcp", envelope)["output_preview"])

    def test_plain_json_stdout_omits_nested_media_bytes(self):
        raw = json.dumps({"answer": "READBACK_OK", "attachment": {"type": "image", "data": "BASE64_SENTINEL"}})
        for value, expected_status in ((raw, None), ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\n" + raw, "ok")):
            with self.subTest(wrapped=expected_status is not None):
                text = _codex_format_tool_result("mcp", raw_output=value)
                self.assertIn("READBACK_OK", text)
                self.assertIn("[image content omitted]", text)
                self.assertNotIn("BASE64_SENTINEL", text)
                summary = _codex_summarize_tool_result("mcp", value)
                self.assertNotIn("BASE64_SENTINEL", summary["output_preview"])
                self.assertEqual(summary["exit_status"], expected_status)

    def test_plain_data_uri_does_not_expand_media_bytes(self):
        raw = "data:image/png;base64,BASE64_SENTINEL"
        for value, expected_status in ((raw, None), ("Exit code: 0\nWall time: 0.1 seconds\nOutput:\n" + raw, "ok")):
            with self.subTest(wrapped=expected_status is not None):
                text = _codex_format_tool_result("mcp", raw_output=value)
                self.assertIn("[encoded media content omitted]", text)
                self.assertNotIn("BASE64_SENTINEL", text)
                summary = _codex_summarize_tool_result("mcp", value)
                self.assertNotIn("BASE64_SENTINEL", summary["output_preview"])
                self.assertEqual(summary["exit_status"], expected_status)


class CodexToolReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.logs = self.root / "sessions"
        self.logs.mkdir()
        self.cache = self.root / "cache"
        self.path = self.logs / "structured.jsonl"
        records = [{"type": "session_meta", "timestamp": "2026-10-08T00:00:00Z",
                    "payload": {"id": "structured", "cwd": "/synthetic/tools"}}]
        payloads = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Check tool evidence"}]},
            {"type": "function_call", "name": "shell_command", "call_id": "c1", "arguments": '{"command":"verify"}'},
            {"type": "function_call_output", "call_id": "c1", "output": {"output": "READBACK_OK", "metadata": {"exit_code": 0}}},
            {"type": "custom_tool_call", "name": "read", "call_id": "c2", "input": "read fixture"},
            {"type": "custom_tool_call_output", "call_id": "c2", "content": [{"type": "text", "text": "READER_ERROR_DETAIL"}], "isError": True},
            {"type": "function_call_output", "call_id": "c3", "output": '{"status":"healthy","result":"JSON_STDOUT_OK"}'},
            {"type": "function_call_output", "call_id": "c4", "output": {
                "output": '{"status":"healthy","result":"JSON_BODY_OK"}', "metadata": {"exit_code": 0}}},
            {"type": "function_call_output", "call_id": "c5", "output": {
                "output": {"answer": "OBJECT_BODY_OK"}, "metadata": {"exit_code": 0}}},
        ]
        records += [{"type": "response_item", "timestamp": f"2026-10-08T00:00:0{i + 1}Z", "payload": p}
                    for i, p in enumerate(payloads)]
        self.path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        self.original = self.path.read_bytes()

    def tearDown(self):
        self.assertEqual(self.path.read_bytes(), self.original, "reader changed the source recording")

    def _write_status_fixture(self):
        path = self.logs / "parser-edges.jsonl"
        business = '{"text":"short description","result":"PUBLIC_BODY_LOST","status":"error"}'
        outputs = [
            {"output": {"output": "PAYLOAD_METADATA_FAILED", "metadata": {"exit_code": 2}}, "isError": False},
            {"output": "Exit code: 2\nWall time: 0.1 seconds\nOutput:\nPLAIN_WRAPPER_FAILED", "duration_seconds": 0.25},
            {"output": "Exit code: 0\nOutput:\nPUBLIC_BUSINESS_TRACE\nExit code: 7"},
            {"output": business},
        ]
        records = [{"type": "session_meta", "timestamp": "2026-10-08T00:01:00Z",
                    "payload": {"id": "parser-edges", "cwd": "/synthetic/tools"}}]
        for number, output in enumerate(outputs):
            records.extend([
                {"type": "response_item", "timestamp": f"2026-10-08T00:01:{number * 2 + 1:02d}Z",
                 "payload": {"type": "function_call", "name": "shell_command", "call_id": f"edge-{number}", "arguments": '{"command":"synthetic-check"}'}},
                {"type": "response_item", "timestamp": f"2026-10-08T00:01:{number * 2 + 2:02d}Z",
                 "payload": {"type": "function_call_output", "call_id": f"edge-{number}", **output}},
            ])
        path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        return path, business

    def test_native_payload_metadata_preserves_supported_inner_status(self):
        path, business = self._write_status_fixture()
        before = path.read_bytes()
        results = [row for row in parse_codex_session_file(path)["messages"] if row["kind"] == "tool_result"]
        for result in results[:2]:
            self.assertEqual(result["tool_summary"]["exit_code"], 2)
            self.assertEqual(result["tool_summary"]["exit_status"], "error")
            self.assertTrue(result["tool_result_error"])
        self.assertEqual(results[0]["tool_summary"]["output_preview"], "PAYLOAD_METADATA_FAILED")
        self.assertEqual(results[1]["tool_summary"]["output_preview"], "PLAIN_WRAPPER_FAILED")
        self.assertEqual(results[2]["tool_summary"]["exit_code"], 0)
        self.assertFalse(results[2]["tool_result_error"])
        self.assertIn("PUBLIC_BUSINESS_TRACE\nExit code: 7", results[2]["text"])
        self.assertIn(business, results[3]["text"])
        self.assertIsNone(results[3]["tool_summary"]["exit_status"])
        self.assertEqual(path.read_bytes(), before)

    def test_public_reader_native_and_string_business_json_preserve_body_and_omit_media(self):
        path = self.logs / "native-business.jsonl"
        business = {"text": "short description", "result": "PUBLIC_NATIVE_BODY_LOST", "status": "error",
                    "attachment": {"type": "image", "data": "BASE64_NATIVE_SENTINEL"}}
        records = [{"type": "session_meta", "timestamp": "2026-10-08T00:02:00Z",
                    "payload": {"id": "native-business", "cwd": "/synthetic/tools"}}]
        for number, output in enumerate((business, json.dumps(business)), 1):
            records.append({"type": "response_item", "timestamp": f"2026-10-08T00:02:0{number}Z",
                            "payload": {"type": "function_call_output", "call_id": f"native-business-{number}", "output": output}})
        path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with HistoryReader("codex", self.logs, self.cache) as reader:
            self.assertEqual(reader.refresh()["status"], "refreshed")
            self.assertEqual([row["id"] for row in reader.search(query="PUBLIC_NATIVE_BODY_LOST")["items"]], ["native-business"])
            page = reader.search(query="PUBLIC_NATIVE_BODY_LOST", brief=True)
            snippets = page["items"][0]["snippets"]
            self.assertEqual(len(snippets), 2)
            self.assertTrue(all("PUBLIC_NATIVE_BODY_LOST" in row["text"] for row in snippets))
            self.assertEqual(reader.search(query="BASE64_NATIVE_SENTINEL")["items"], [])
            activity = reader.activity(date="2026-10-08", timezone="UTC", session_id="native-business", evidence_limit=4)
            results = [row for row in activity["items"][0]["evidence"] if row["kind"] == "tool_result"]
            self.assertEqual(len(results), 2)
            expected = dict(business, attachment="[image content omitted]")
            for result in results:
                body = result["text"].split("````\n", 1)[1].rsplit("\n````", 1)[0]
                self.assertEqual(json.loads(body), expected)
                self.assertNotIn("Status: error", result["text"])
                self.assertNotIn("BASE64_NATIVE_SENTINEL", result["text"])
                self.assertNotIn("tool_result_error", result["locator"])
        self.assertEqual(path.read_bytes(), before)

    def test_public_reader_invalid_tool_ids_do_not_erase_valid_messages_or_pair(self):
        path = self.logs / "invalid-ids.jsonl"
        records = [{"type": "session_meta", "timestamp": "2026-10-08T00:03:00Z",
                    "payload": {"id": "invalid-ids", "cwd": "/synthetic/tools"}},
                   {"type": "response_item", "timestamp": "2026-10-08T00:03:01Z",
                    "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "PUBLIC_BEFORE_INVALID_IDS"}]}}]
        invalid_ids = ({"bad": "unhashable"}, [], 42, "", "   ", None)
        for number, call_id in enumerate(invalid_ids):
            custom = number % 2 == 1
            records.extend([
                {"type": "response_item", "timestamp": f"2026-10-08T00:03:{number * 2 + 2:02d}Z",
                 "payload": {"type": "custom_tool_call" if custom else "function_call", "name": "shell_command", "call_id": call_id,
                             "input" if custom else "arguments": "synthetic-invalid-id"}},
                {"type": "response_item", "timestamp": f"2026-10-08T00:03:{number * 2 + 3:02d}Z",
                 "payload": {"type": "custom_tool_call_output" if custom else "function_call_output", "call_id": call_id,
                             "output": f"PUBLIC_UNPAIRED_BODY_{number}"}},
            ])
        records.extend([
            {"type": "response_item", "timestamp": "2026-10-08T00:03:20Z",
             "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "PUBLIC_AFTER_INVALID_IDS"}]}},
            {"type": "response_item", "timestamp": "2026-10-08T00:03:21Z",
             "payload": {"type": "function_call", "name": "shell_command", "call_id": "valid-following", "arguments": '{"command":"synthetic-check"}'}},
            {"type": "response_item", "timestamp": "2026-10-08T00:03:22Z",
             "payload": {"type": "function_call_output", "call_id": "valid-following", "output": "Exit code: 0\nOutput:\nPUBLIC_VALID_PAIRED_BODY"}},
        ])
        path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        before = path.read_bytes()
        with HistoryReader("codex", self.logs, self.cache) as reader:
            self.assertEqual(reader.refresh()["status"], "refreshed")
            for keyword in ("PUBLIC_BEFORE_INVALID_IDS", "PUBLIC_AFTER_INVALID_IDS", "PUBLIC_VALID_PAIRED_BODY"):
                self.assertEqual([row["id"] for row in reader.search(query=keyword)["items"]], ["invalid-ids"])
        results = [row for row in parse_codex_session_file(path)["messages"] if row["kind"] == "tool_result"]
        self.assertEqual(len(results), len(invalid_ids) + 1)
        for result in results[:-1]:
            self.assertIsNone(result["tool_call_id"])
            self.assertEqual(result["tool_summary"]["name"], "tool")
            self.assertNotIn("Call ID:", result["text"])
        self.assertEqual(results[-1]["tool_call_id"], "valid-following")
        self.assertEqual(results[-1]["tool_summary"]["name"], "shell_command")
        self.assertEqual(path.read_bytes(), before)

    def test_public_reader_refresh_migrates_formal_v6_bad_body_and_status(self):
        path, business = self._write_status_fixture()
        before = path.read_bytes()
        with patch("history_core.reader.parser_version", return_value=6):
            with HistoryReader("codex", self.logs, self.cache) as old:
                self.assertEqual(old.refresh()["status"], "refreshed")
        database = next(self.cache.glob("machine-codex-*/index.sqlite"))
        # Reproduce the committed v6 projection while preserving the native
        # recording identity, file signature, timestamp and bytes.
        old_rows = [
            ("Tool result: shell_command\nStatus: ok\nOutput:\nPAYLOAD_METADATA_FAILED", "ok", None, False),
            ("Tool result: shell_command\nOutput:\nPLAIN_WRAPPER_FAILED", None, None, None),
            ("Tool result: shell_command\nStatus: error\nExit code: 7\nOutput:\nPUBLIC_BUSINESS_TRACE", "error", 7, True),
            ("Tool result: shell_command\nStatus: error\nOutput:\nshort description", "error", None, True),
        ]
        with sqlite3.connect(database) as db:
            rows = db.execute("SELECT id,activity_meta_json FROM messages WHERE session_id='parser-edges' AND kind='tool_result' ORDER BY ts_ms,id").fetchall()
            self.assertEqual(len(rows), len(old_rows))
            for (row_id, raw_meta), (text, status, code, error) in zip(rows, old_rows):
                locator = json.loads(raw_meta)
                locator.pop("tool_result_error", None)
                if error is not None:
                    locator["tool_result_error"] = error
                summary = {"name": "shell_command", "exit_status": status, "exit_code": code,
                           "is_error": bool(error), "output_preview": text}
                db.execute("UPDATE messages SET text=?,tool_summary_json=?,activity_meta_json=? WHERE id=?",
                           (text, json.dumps(summary), json.dumps(locator), row_id))
            db.execute("UPDATE sessions SET parser_version=6,search_blob='short description' WHERE id='parser-edges'")
        with HistoryReader("codex", self.logs, self.cache) as reader:
            self.assertEqual(reader.search(query="PUBLIC_BODY_LOST")["items"], [])
            revision = reader.search()["index_revision"]
            self.assertEqual(reader.refresh()["status"], "refreshed")
            page = reader.search(query="PUBLIC_BODY_LOST", brief=True)
            self.assertEqual([item["id"] for item in page["items"]], ["parser-edges"])
            self.assertNotEqual(page["index_revision"], revision)
            self.assertTrue(any("PUBLIC_BODY_LOST" in row["text"] for row in page["items"][0]["snippets"]))
            activity = reader.activity(date="2026-10-08", timezone="UTC", session_id="parser-edges", evidence_limit=12)
            results = [row for row in activity["items"][0]["evidence"] if row["kind"] == "tool_result"]
            self.assertEqual(len(results), 4)
            for result in results[:2]:
                self.assertIn("Status: error\nExit code: 2", result["text"])
                self.assertTrue(result["locator"]["tool_result_error"])
            self.assertIn("Status: ok\nExit code: 0", results[2]["text"])
            self.assertFalse(results[2]["locator"]["tool_result_error"])
            self.assertIn(business, results[3]["text"])
            self.assertNotIn("Status: error", results[3]["text"])
            self.assertNotIn("tool_result_error", results[3]["locator"])
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT parser_version FROM sessions WHERE id='parser-edges'").fetchone()[0], parser_version('codex'))
        self.assertEqual(path.read_bytes(), before)

    def test_native_parser_content_fallback_and_top_level_error(self):
        parsed = parse_codex_session_file(self.path)
        results = [row for row in parsed["messages"] if row["kind"] == "tool_result"]
        self.assertEqual(results[0]["raw_ref"], {"line_no": 4})
        self.assertIn("READBACK_OK", results[0]["text"])
        self.assertFalse(results[0]["tool_result_error"])
        self.assertIn("READER_ERROR_DETAIL", results[1]["text"])
        self.assertTrue(results[1]["tool_result_error"])
        self.assertEqual(results[1]["tool_call_id"], "c2")

    def test_public_reader_refresh_search_activity_and_old_cache_reparse(self):
        with patch("history_core.reader.parser_version", return_value=5):
            with HistoryReader("codex", self.logs, self.cache) as old:
                self.assertEqual(old.refresh()["status"], "refreshed")
        database = next(self.cache.glob("machine-codex-*/index.sqlite"))
        with sqlite3.connect(database) as db:
            # Reproduce the old cache's lost object bodies without changing
            # the native recording's bytes, size, timestamp, or identity.
            db.execute("UPDATE messages SET text='Tool result: shell_command', tool_summary_json=NULL WHERE kind='tool_result'")
            db.execute("UPDATE sessions SET search_blob='Check tool evidence', parser_version=5")
        with HistoryReader("codex", self.logs, self.cache) as reader:
            self.assertEqual(reader.refresh()["status"], "refreshed")
            self.assertEqual([row["id"] for row in reader.search(query="READBACK_OK")["items"]], ["structured"])
            for keyword in ("JSON_STDOUT_OK", "JSON_BODY_OK", "OBJECT_BODY_OK"):
                self.assertEqual([row["id"] for row in reader.search(query=keyword)["items"]], ["structured"])
            activity = reader.activity(date="2026-10-08", timezone="Asia/Shanghai", refresh=True,
                                       session_id="structured", evidence_limit=10)
            results = [e for e in activity["items"][0]["evidence"] if e["kind"] == "tool_result"]
            self.assertEqual(len(results), 5)
            self.assertIn("READBACK_OK", results[0]["text"])
            self.assertIn("READER_ERROR_DETAIL", results[1]["text"])
            self.assertIn("Status: error", results[1]["text"])
            self.assertIn('{"status":"healthy","result":"JSON_STDOUT_OK"}', results[2]["text"])
            self.assertIn('{"status":"healthy","result":"JSON_BODY_OK"}', results[3]["text"])
            self.assertIn('"answer": "OBJECT_BODY_OK"', results[4]["text"])
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT parser_version FROM sessions").fetchone()[0], parser_version("codex"))
        self.assertEqual(parser_version("codex"), 9)
        self.assertEqual(parser_version("claude"), 6)


if __name__ == "__main__":
    unittest.main()
