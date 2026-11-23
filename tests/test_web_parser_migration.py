"""Web startup must invalidate unchanged caches after parser upgrades."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from history_core.providers import parser_version
from history_core.sources import Indexer


class NoListenServer:
    def __init__(self, address, handler):
        self.address = address
        self.handler = handler

    def serve_forever(self):
        pass


class WebParserMigrationTests(unittest.TestCase):
    def test_linux_startup_reparses_unchanged_version_five_demo_caches(self):
        demo = Path(app.__file__).resolve().parent / 'demo'
        parsers = {'codex': app.parse_codex_session_file,
                   'claude': app.parse_claude_session_file}
        db_names = {'codex': 'index_linux.sqlite', 'claude': 'index_linux_claude.sqlite'}
        source_dirs = {'codex': demo / 'codex' / 'sessions',
                       'claude': demo / 'claude' / 'projects'}
        fingerprints = {path: (path.stat().st_mtime_ns, path.stat().st_size)
                        for directory in source_dirs.values()
                        for path in directory.rglob('*.jsonl')}
        cached_rows = {}

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / 'demo-isolated'
            cache.mkdir()
            for source, parse_file in parsers.items():
                seed = Indexer(source_dirs[source], cache, source,
                               db_filename=db_names[source], parse_file_fn=parse_file,
                               file_filter_fn=lambda path: not path.name.startswith('agent-'),
                               parser_version=5)
                try:
                    seed.scan_sessions()
                    rows = seed.conn.execute(
                        'SELECT file_path, mtime, file_signature FROM sessions ORDER BY file_path'
                    ).fetchall()
                    self.assertTrue(rows, f'{source} demo must seed an existing cache')
                    cached_rows[source] = [tuple(row) for row in rows]
                    with seed.conn:
                        seed.conn.execute('UPDATE messages SET text=?',
                                          (f'stale-parser-five:{source}',))
                finally:
                    seed.conn.close()

            with contextlib.ExitStack() as stack:
                background = stack.enter_context(patch.object(
                    app.SourceBackend, '_start_background_refresh', autospec=True))
                spies = {source: stack.enter_context(patch.object(
                    app, f'parse_{source}_session_file', wraps=parse_file))
                    for source, parse_file in parsers.items()}
                stack.enter_context(patch.object(app, 'ThreadingHTTPServer', NoListenServer))
                stack.enter_context(patch.object(app, 'detect_runtime_system', return_value='linux'))
                stack.enter_context(patch.object(app.sys, 'argv',
                                                  ['app.py', '--demo', '--data-dir', tmp]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                try:
                    app.main()
                    backends = {call.args[0].source: call.args[0]
                                for call in background.call_args_list}
                    for source in parsers:
                        with self.subTest(source=source):
                            indexer = backends[source].indexer
                            self.assertGreater(spies[source].call_count, 0,
                                               f'{source} unchanged old cache must be reparsed')
                            self.assertEqual(indexer.parser_version, parser_version(source))
                            versions = {row[0] for row in indexer.conn.execute(
                                'SELECT parser_version FROM sessions')}
                            self.assertEqual(versions, {parser_version(source)})
                            self.assertEqual([tuple(row) for row in indexer.conn.execute(
                                'SELECT file_path, mtime, file_signature FROM sessions ORDER BY file_path'
                            )], cached_rows[source], 'reparse must not depend on changed source files')
                            stale = indexer.conn.execute(
                                'SELECT COUNT(*) FROM messages WHERE text=?',
                                (f'stale-parser-five:{source}',)).fetchone()[0]
                            self.assertEqual(stale, 0, 'the old parsed messages must be replaced')
                finally:
                    for call in background.call_args_list:
                        backend = call.args[0]
                        if backend.indexer is not None:
                            backend.indexer.conn.close()

        self.assertEqual({path: (path.stat().st_mtime_ns, path.stat().st_size)
                          for path in fingerprints}, fingerprints,
                         'the bundled synthetic recordings remain unchanged')

    def test_windows_and_wsl_startup_use_the_shared_parser_version_table(self):
        registered = []

        def backend_stub(**kwargs):
            registered.append(kwargs)
            return SimpleNamespace(**kwargs, indexer=SimpleNamespace(
                db_path=Path(kwargs['data_dir']) / kwargs['db_filename']))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ['app.py', '--data-dir', str(root / 'cache')]
            for source in ('codex', 'claude', 'openclaw'):
                argv += [f'--{source}-dir', str(root / source),
                         f'--wsl-{source}-dir', str(root / 'wsl' / source)]
            argv += ['--hermes-state-db', str(root / 'hermes.sqlite'),
                     '--opencode-state-db', str(root / 'opencode.sqlite')]
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(app, 'SourceBackend', side_effect=backend_stub))
                bootstrapper = stack.enter_context(patch.object(app, 'WslBootstrapper'))
                stack.enter_context(patch.object(app, 'ThreadingHTTPServer', NoListenServer))
                stack.enter_context(patch.object(app, 'detect_runtime_system', return_value='windows'))
                stack.enter_context(patch.object(app.sys, 'argv', argv))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                app.main()
                bootstrapper.return_value.ensure.assert_not_called()

        sources = {(item['system'], item['source']): item for item in registered}
        for system in ('windows', 'wsl'):
            for source in ('codex', 'claude', 'openclaw'):
                with self.subTest(system=system, source=source):
                    self.assertEqual(sources[(system, source)]['parser_version'],
                                     parser_version(source))


if __name__ == '__main__':
    unittest.main()
