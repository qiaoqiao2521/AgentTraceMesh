"""Backup and recording-identity regressions using synthetic source files only."""
import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from history_core import sources
from history_core.sources import Indexer


def recording(identity, text):
    return (
        json.dumps({"timestamp": "2026-10-31T00:00:00Z", "type": "session_meta",
                    "payload": {"id": identity, "cwd": "/synthetic/project"}}) + "\n" +
        json.dumps({"timestamp": "2026-10-31T00:00:01Z", "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": text}]}}) + "\n"
    ).encode()


class BackupRecordingSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.cache = self.base / "cache"
        self.source.mkdir()
        self.cache.mkdir()
        self.indexer = Indexer(self.source, self.cache, "codex")
        self.addCleanup(self.indexer.conn.close)
        self.backups = self.base / "backups"

    def write(self, relative="session.jsonl", identity="abcdefgh-native", text="first"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(recording(identity, text))
        return path

    def scan(self, **kwargs):
        self.indexer.scan_sessions(build_audits=False, **kwargs)

    def identities(self):
        return {row["file_path"]: row["id"] for row in
                self.indexer.conn.execute("SELECT id,file_path FROM sessions")}

    def texts(self, identity):
        return [row[0] for row in self.indexer.conn.execute(
            "SELECT text FROM messages WHERE session_id=? ORDER BY id", (identity,))]

    def backup_contents(self):
        return {path.read_bytes() for path in self.backups.rglob("*.jsonl")}

    def test_three_successive_archives_keep_every_version(self):
        expected = set()
        for text in ("first version", "second version", "third version"):
            path = self.write(text=text)
            expected.add(path.read_bytes())
            self.scan()
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (True, "archived"))
            self.assertFalse(path.exists())
        self.assertEqual(len(list(self.backups.iterdir())), 3)
        self.assertEqual(self.backup_contents(), expected)

    def test_append_immediately_before_source_unlink_remains_in_backup(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        unlink = Path.unlink

        def append_then_unlink(candidate, *args, **kwargs):
            if candidate == path:
                with path.open("ab") as stream:
                    stream.write(b"late history\n")
            return unlink(candidate, *args, **kwargs)

        with patch.object(Path, "unlink", append_then_unlink):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (True, "archived"))
        self.assertEqual(self.backup_contents(), {original + b"late history\n"})

    def test_writer_held_open_after_unlink_still_appends_to_backup(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        with path.open("ab") as writer:
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (True, "archived"))
            self.assertFalse(path.exists())
            writer.write(b"history from open writer\n")
            writer.flush()
        self.assertEqual(self.backup_contents(), {original + b"history from open writer\n"})

    def test_project_delete_keeps_same_name_and_same_prefix_all_three(self):
        paths = [self.write(f"copy-{i}/session.jsonl", f"abcdefgh-{i}", f"body {i}")
                 for i in range(3)]
        expected = {path.read_bytes() for path in paths}
        self.scan()
        ok, detail, count, directory = self.indexer.delete_project_sessions(
            "/synthetic/project", self.backups)
        self.assertEqual((ok, detail, count), (True, "deleted", 3))
        self.assertTrue(Path(directory).is_dir())
        self.assertEqual(self.backup_contents(), expected)
        self.assertEqual(len(list(Path(directory).iterdir())), 3)
        self.assertFalse(any(path.exists() for path in paths))
        self.assertEqual(self.identities(), {})

    def test_failed_link_preserves_source_previous_backups_and_index(self):
        path = self.write()
        original = path.read_bytes()
        self.backups.mkdir()
        previous = self.backups / path.name
        previous.write_bytes(b"older backup")
        self.scan()

        with patch.object(sources.os, "link", side_effect=PermissionError("link unavailable")), self.assertLogs(
                "history_core.sources", level="WARNING"):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (False, "backup_failed"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(previous.read_bytes(), b"older backup")
        self.assertEqual(list(self.backups.iterdir()), [previous])
        self.assertEqual(self.texts("abcdefgh-native"), ["first"])

    def test_cross_device_or_unsupported_link_never_deletes_source(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        failures = (OSError(errno.EXDEV, "cross-device link"),
                    OSError(errno.ENOTSUP, "hard links unsupported"), NotImplementedError())
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(
                    sources.os, "link", side_effect=failure), self.assertLogs(
                    "history_core.sources", level="WARNING"):
                self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                                 (False, "backup_failed"))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(self.backups.iterdir()), [])
            self.assertIn(str(path), self.identities())

    def test_relative_source_symlink_preserves_target_inode_in_backup(self):
        target = self.base / "original.jsonl"
        original = recording("abcdefgh-native", "symlink history")
        target.write_bytes(original)
        path = self.source / "session.jsonl"
        try:
            path.symlink_to("../original.jsonl")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.scan()
        self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                         (True, "archived"))
        self.assertFalse(path.is_symlink())
        self.assertEqual(target.read_bytes(), original)
        backup = next(self.backups.iterdir())
        self.assertFalse(backup.is_symlink())
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_ino, target.stat().st_ino)

    def test_exclusive_backup_does_not_follow_an_existing_target_symlink(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        self.backups.mkdir()
        outside = self.base / "must-not-be-created"
        reserved = self.backups / path.name
        try:
            reserved.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                         (True, "archived"))
        self.assertTrue(reserved.is_symlink())
        self.assertFalse(outside.exists())
        actual = [candidate for candidate in self.backups.iterdir() if not candidate.is_symlink()]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].read_bytes(), original)

    def test_failed_flush_preserves_source_and_index(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        with patch.object(sources.os, "fsync", side_effect=OSError("disk unavailable")), self.assertLogs(
                "history_core.sources", level="WARNING"):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (False, "backup_failed"))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertIn(str(path), self.identities())

    def test_failed_source_removal_keeps_complete_backup_and_index(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        real_unlink, real_fsync = Path.unlink, sources.os.fsync
        events = []

        def sync(fd):
            events.append("synced")
            return real_fsync(fd)

        def unlink(candidate, *args, **kwargs):
            if candidate == path:
                events.append("remove source")
                raise PermissionError("source busy")
            return real_unlink(candidate, *args, **kwargs)

        with patch.object(sources.os, "fsync", side_effect=sync), patch.object(
                Path, "unlink", unlink), self.assertLogs("history_core.sources", level="WARNING"):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (False, "backup_failed"))
        self.assertEqual(events[-1], "remove source")
        self.assertTrue(events[:-1])
        self.assertTrue(all(event == "synced" for event in events[:-1]))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.backup_contents(), {original})
        self.assertIn(str(path), self.identities())

    def test_appending_during_sync_is_preserved_in_backup(self):
        path = self.write()
        original = path.read_bytes()
        self.scan()
        fsync = sources.os.fsync
        appended = False

        def append_during_sync(fd):
            nonlocal appended
            if not appended:
                with path.open("ab") as stream:
                    stream.write(b"additional history\n")
                appended = True
            return fsync(fd)

        with patch.object(sources.os, "fsync", side_effect=append_during_sync):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (True, "archived"))
        self.assertFalse(path.exists())
        self.assertEqual(self.backup_contents(), {original + b"additional history\n"})

    def test_project_delete_continues_other_items_and_reports_partial(self):
        failed = self.write("one/session.jsonl", "abcdefgh-one", "fail this link")
        succeeded = self.write("two/session.jsonl", "abcdefgh-two", "keep this link")
        original, expected = failed.read_bytes(), succeeded.read_bytes()
        self.scan()
        link = sources.os.link

        def fail_one(original, destination):
            if Path(original) == failed:
                raise OSError("one link failed")
            return link(original, destination)

        with patch.object(sources.os, "link", side_effect=fail_one), self.assertLogs(
                "history_core.sources", level="WARNING"):
            result = self.indexer.delete_project_sessions("/synthetic/project", self.backups)
        self.assertEqual(result[:3], (False, "partial", 1))
        self.assertEqual(failed.read_bytes(), original)
        self.assertFalse(succeeded.exists())
        self.assertEqual(self.identities(), {str(failed): "abcdefgh-one"})
        self.assertEqual(self.backup_contents(), {expected})

    def test_weak_cleanup_reports_total_failure_without_erasing_index(self):
        path = self.write()
        self.scan()
        with patch.object(sources.os, "link", side_effect=OSError("cannot link")), self.assertLogs(
                "history_core.sources", level="WARNING"):
            result = self.indexer.cleanup_weak_sessions(self.backups)
        self.assertEqual(result[:3], (False, "backup_failed", 0))
        self.assertTrue(path.exists())
        self.assertIn(str(path), self.identities())

    def test_missing_source_does_not_claim_an_archive_was_created(self):
        path = self.write()
        self.scan()
        path.unlink()
        with self.assertLogs("history_core.sources", level="WARNING"):
            self.assertEqual(self.indexer.archive_session("abcdefgh-native", self.backups),
                             (False, "backup_failed"))
        self.assertIn(str(path), self.identities())

    def test_normal_scan_preserves_duplicate_recordings_with_either_flag(self):
        first = self.write("first.jsonl", text="first recording")
        second = self.write("second.jsonl", text="second recording")
        for flag in (False, True, False):
            with self.subTest(preserve_recordings=flag):
                self.scan(preserve_recordings=flag)
                identities = self.identities()
                self.assertEqual(len(identities), 2)
                self.assertNotEqual(identities[str(first)], identities[str(second)])
                self.assertEqual(self.texts(identities[str(first)]), ["first recording"])
                self.assertEqual(self.texts(identities[str(second)]), ["second recording"])
                for path, text in ((first, "first recording"), (second, "second recording")):
                    found = self.indexer.list_sessions_page(q=text)["items"]
                    self.assertEqual([item["id"] for item in found], [identities[str(path)]])
        aliased = next(identity for identity in identities.values() if "@recording-" in identity)
        metadata = self.indexer.conn.execute(
            "SELECT activity_metadata_json FROM sessions WHERE id=?", (aliased,)).fetchone()[0]
        self.assertEqual(json.loads(metadata)["native_session_id"], "abcdefgh-native")
        detail = self.indexer.get_session(aliased)
        self.assertEqual(detail["session"]["id"], aliased)
        self.assertIs(detail["session"]["identity_conflict"], True)
        self.assertEqual(detail["session"]["native_session_id"], "abcdefgh-native")

    def test_session_detail_exposes_only_recording_identity_metadata(self):
        self.write()
        self.scan()
        metadata = {"identity_conflict": True, "native_session_id": "native",
                    "source_capture": {"private_capture_detail": "not a session identity"}}
        with self.indexer.conn:
            self.indexer.conn.execute("UPDATE sessions SET activity_metadata_json=?",
                                      (json.dumps(metadata),))
        detail = self.indexer.get_session("abcdefgh-native")["session"]
        self.assertIs(detail["identity_conflict"], True)
        self.assertEqual(detail["native_session_id"], "native")
        self.assertNotIn("activity_metadata_json", detail)
        self.assertNotIn("source_capture", detail)
        self.assertNotIn("private_capture_detail", json.dumps(detail))
        for broken in ("{malformed", "[]", "null"):
            with self.subTest(metadata=broken), self.indexer.conn:
                self.indexer.conn.execute("UPDATE sessions SET activity_metadata_json=?", (broken,))
                clean = self.indexer.get_session("abcdefgh-native", include_messages=False)["session"]
                self.assertEqual(clean["id"], "abcdefgh-native")
                self.assertNotIn("identity_conflict", clean)
                self.assertNotIn("native_session_id", clean)

    def test_normal_rescan_updates_both_without_changing_recording_keys(self):
        first = self.write("first.jsonl", text="first recording")
        second = self.write("second.jsonl", text="second recording")
        self.scan()
        identities = self.identities()
        self.write(first.name, text="first recording edited")
        self.write(second.name, text="second recording edited")
        self.scan()
        self.assertEqual(self.identities(), identities)
        self.assertEqual(self.texts(identities[str(first)]), ["first recording edited"])
        self.assertEqual(self.texts(identities[str(second)]), ["second recording edited"])

    def test_normal_scan_recovers_recording_missing_from_legacy_index(self):
        old = self.write("old.jsonl", text="old history must return")
        latest = self.write("latest.jsonl", text="legacy winning recording")
        # Reproduce the old cache after INSERT OR REPLACE retained only latest.
        old_payload = self.indexer._parse_file_fn(latest)
        with self.indexer.lock, self.indexer.conn:
            self.indexer._store_parsed_session(None, old_payload, latest.stat().st_mtime,
                                              "legacy-signature", latest, [latest], build_audits=False)
        self.assertEqual(self.identities(), {str(latest): "abcdefgh-native"})
        self.scan()
        identities = self.identities()
        self.assertEqual(len(identities), 2)
        self.assertEqual(self.texts(identities[str(old)]), ["old history must return"])
        self.assertEqual(self.texts(identities[str(latest)]), ["legacy winning recording"])
        self.scan()
        self.assertEqual(self.identities(), identities)

    def test_recording_alias_stays_stable_after_native_owner_disappears(self):
        self.write("first.jsonl", text="first recording")
        self.write("second.jsonl", text="second recording")
        self.scan()
        identities = self.identities()
        native = next(Path(path) for path, identity in identities.items() if identity == "abcdefgh-native")
        alias_path, alias_id = next((Path(path), identity) for path, identity in identities.items()
                                   if identity != "abcdefgh-native")
        native.unlink()
        self.write(alias_path.name, text="surviving recording edited")
        self.scan()
        self.assertEqual(self.identities(), {str(alias_path): alias_id})
        self.assertEqual(self.texts(alias_id), ["surviving recording edited"])


if __name__ == "__main__":
    unittest.main()
