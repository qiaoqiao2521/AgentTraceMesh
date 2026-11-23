"""Short, read-only snapshots of an existing derived message index."""
from collections import OrderedDict
from contextlib import contextmanager
import copy
import hashlib
from pathlib import Path
import sqlite3
import stat
import threading

from .providers import NATIVE_SOURCES


def reader_revision(indexer):
    """Use the public file reader's revision identity, not reuse's cursor hash."""
    if getattr(indexer, 'sessions_dir', None) is None:
        raise ValueError('indexed_snapshot_unsupported')
    with indexer.lock:
        row = indexer.conn.execute(
            "SELECT value FROM reader_state WHERE key = 'revision'").fetchone()
    if row is None or not isinstance(row[0], str):
        raise ValueError('indexed_snapshot_unsupported')
    return hashlib.sha256(
        (indexer.source + str(indexer.sessions_dir) + row[0]).encode()).hexdigest()


def _database_path(indexer):
    if (getattr(indexer, 'sessions_dir', None) is None
            or getattr(indexer, 'source', None) in NATIVE_SOURCES
            or getattr(indexer, 'db_path', None) is None):
        raise ValueError('indexed_snapshot_unsupported')
    path = Path(indexer.db_path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('cache_symlink_not_allowed')
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('index_database_regular_file_required')
    return path


@contextmanager
def indexed_read_view(indexer):
    """Reuse query methods on a private connection without holding writer locks."""
    path = _database_path(indexer)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        view = copy.copy(indexer)
        view.conn = connection
        view.lock = threading.RLock()
        view._session_preview_cache = OrderedDict()
        try:
            # This first read pins both revision and subsequent rows to one snapshot.
            reader_revision(view)
        except sqlite3.OperationalError as exc:
            if 'no such table: reader_state' in str(exc):
                raise ValueError('indexed_snapshot_unsupported') from exc
            raise
        yield view
    finally:
        try:
            connection.set_progress_handler(None, 0)
            connection.rollback()
        finally:
            connection.close()
