"""Bounded JSONL prefixes from one pinned descriptor; no source writes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone

DEFAULT_CAPTURE_BYTES = 128 * 1024 * 1024
MAX_CAPTURE_LINE_BYTES = 8 * 1024 * 1024


class CaptureError(ValueError):
    pass


def stat_signature(value):
    return json.dumps([value.st_mtime_ns, value.st_ctime_ns, value.st_size,
                       value.st_dev, value.st_ino])


class JsonlCapture:
    """Hold the FD until callers finish consuming the immutable prefix.

    Verification attests the bytes read at two observations, not continuous
    immutability or a filesystem-atomic snapshot under arbitrary rewrites.
    """

    def __init__(self, path, *, max_bytes=DEFAULT_CAPTURE_BYTES, capture_mode='strict', directory_fd=None):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError('invalid_capture_budget')
        if capture_mode not in ('strict', 'projected'):
            raise ValueError('invalid_capture_mode')
        self.capture_mode = capture_mode
        self.projection = None
        self.directory_fd = directory_fd
        self.path = path
        self.max_bytes = max_bytes
        self.stream = None

    def __enter__(self):
        flags = (os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        if self.path.is_symlink():
            raise CaptureError('capture_source_symlink')
        fd = (os.open(self.path, flags) if self.directory_fd is None else
              os.open(self.path.name, flags, dir_fd=self.directory_fd))
        self.stream = os.fdopen(fd, 'rb')
        try:
            self.initial = os.fstat(fd)
            if not stat.S_ISREG(self.initial.st_mode):
                raise CaptureError('capture_regular_file_required')
            self._check_identity()
            if self.initial.st_size > self.max_bytes:
                raise CaptureError('capture_budget_exceeded')
            if self.capture_mode == 'projected':
                from .projection import ProjectedRecords
                self.projection = ProjectedRecords()
                self.captured_through, self.prefix_hash = self.projection.capture(self.stream, self.initial.st_size)
                if not self.captured_through:
                    raise CaptureError('capture_no_complete_lines')
                return self
            remaining = self.initial.st_size
            lines = []
            while remaining:
                line = self.stream.readline(min(remaining, MAX_CAPTURE_LINE_BYTES + 1))
                if not line:
                    raise CaptureError('capture_source_truncated')
                remaining -= len(line)
                if len(line) > MAX_CAPTURE_LINE_BYTES:
                    raise CaptureError('capture_line_budget_exceeded')
                if line.endswith(b'\n'):
                    lines.append(line)
                else:
                    # Only the initial-size tail can be incomplete. Never chase
                    # an appender past that size or decode its partial UTF-8.
                    if remaining:
                        raise CaptureError('capture_line_budget_exceeded')
            self.content = b''.join(lines)
            self.captured_through = len(self.content)
            if not self.content:
                raise CaptureError('capture_no_complete_lines')
            self.prefix_hash = hashlib.sha256(self.content).hexdigest()
            return self
        except BaseException:
            self.__exit__()
            raise

    def _check_identity(self):
        current = self.path.lstat()
        if stat.S_ISLNK(current.st_mode):
            raise CaptureError('capture_source_replaced')
        if (current.st_dev, current.st_ino) != (self.initial.st_dev, self.initial.st_ino):
            raise CaptureError('capture_source_replaced')

    def verify(self):
        self._check_identity()
        # Bypass BufferedReader's retained bytes: seek(0) on that object can
        # otherwise hash its old read buffer instead of the changed source.
        os.lseek(self.stream.fileno(), 0, os.SEEK_SET)
        remaining = self.captured_through
        observed_hash = hashlib.sha256()
        while remaining:
            block = os.read(self.stream.fileno(), min(remaining, 1024 * 1024))
            if not block:
                raise CaptureError('capture_source_truncated')
            observed_hash.update(block)
            remaining -= len(block)
        final = os.fstat(self.stream.fileno())
        self._check_identity()
        if final.st_size < self.initial.st_size:
            raise CaptureError('capture_source_truncated')
        if observed_hash.hexdigest() != self.prefix_hash:
            raise CaptureError('capture_prefix_changed')
        if final.st_size == self.initial.st_size and stat_signature(final) != stat_signature(self.initial):
            raise CaptureError('capture_nonappend_change')
        length = self.captured_through
        self.metadata = {
            'status': 'captured',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'captured_through': length,
            'observed_size': final.st_size,
            'prefix_hash': self.prefix_hash,
            'trailing_bytes': final.st_size - length,
            'partial': final.st_size != length,
            'identity': {'dev': self.initial.st_dev, 'ino': self.initial.st_ino},
        }
        # Existing strict readers can accept only a complete, stable file.
        # A prefix must never masquerade as the latest whole-file stat.
        self.signature = (stat_signature(final) if not self.metadata['partial']
                          else json.dumps({'capture': self.metadata}, sort_keys=True))
        if self.projection:
            self.metadata.update(self.projection.metadata())
            self.signature = json.dumps(['projected', self.metadata['projection_version'], stat_signature(final),
                                         length, self.prefix_hash])
        return self.metadata

    def __exit__(self, *exc):
        self.stream.close()
        if self.projection:
            self.projection.spool.close()
