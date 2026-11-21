"""Bounded lexical projection of complete JSONL records. Offsets always count source bytes."""
import codecs
import hashlib
import json
import re
import struct
import tempfile

PROJECTION_VERSION = 1
PROJECTED_DB = f'index.projected-v{PROJECTION_VERSION}.sqlite'
BLOCK_BYTES = 64 * 1024
FIELD_BYTES = 64 * 1024
RECORD_BYTES = 256 * 1024
IMAGE_MIN = 4096
PLACEHOLDER = '[TraceMesh omitted '
_HEADER = struct.Struct('>QQI')
_IMAGE_KEYS = {b'data', b'image', b'base64', b'image_url', b'image_data', b'b64_json', b'image_base64'}
_GENERIC_KEYS = {b'data', b'base64'}
_TYPE_KEYS = {b'type', b'media_type', b'mime_type', b'mimeType', b'mediaType'}
_URI_END = re.compile(rb'data:image\\?/([a-z0-9.+-]{1,64});base64,$', re.I)
_BASE64 = re.compile(rb'(?:[A-Za-z0-9+/=]+|\\/)+')
_STRING_END = re.compile(rb'["\\]')
_STRUCTURE = re.compile(rb'["{}\[\]:,]')
_ESCAPES = re.compile(rb'\\(?:["\\/bfnrt]|u[0-9A-Fa-f]{4})|\\|[\x00-\x1f]')


class _String:
    """Consume raw JSON string slices. Keep the suffix after each embedded image URI."""
    def __init__(self, key, stats):
        self.bare_image = key in _IMAGE_KEYS
        self.generic = key in _GENERIC_KEYS
        self.generic_runs = 0
        self.stats = stats
        self.length = 0
        self.kept = bytearray()
        self.run = bytearray()
        self.run_size = 0
        self.image_type = None
        self.overflow = False
        self.pending_slash = b''
        self.pending_escape = b''
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.invalid = False
        self.image_bytes = 0

    def _keep(self, data):
        if len(self.kept) + len(data) > FIELD_BYTES:
            self.overflow = True
        elif not self.overflow:
            self.kept.extend(data)

    def _end_run(self):
        if self.run_size:
            bare_generic = self.generic and self.image_type == 'base64'
            if self.image_type and self.run_size > (FIELD_BYTES if bare_generic else IMAGE_MIN):
                self.stats['image_fields'] += 1
                self.generic_runs += int(bare_generic)
                self.stats['omitted_bytes'] += self.run_size
                self.image_bytes += self.run_size
                self._keep(f'{PLACEHOLDER}{self.run_size} bytes of {self.image_type}]'.encode())
            elif self.run_size > FIELD_BYTES:
                self.overflow = True
            else:
                self._keep(self.run)
            self.run.clear()
            self.run_size = 0

    def feed(self, data, final=False):
        self.length += len(data)
        # Bytes erased from a string still have to be legal JSON string bytes.
        # Otherwise projecting a malformed field could manufacture a valid row.
        try:
            self.decoder.decode(data, final=final)
        except UnicodeError:
            self.invalid = True
        checked = self.pending_escape + data
        self.pending_escape = b''
        for match in _ESCAPES.finditer(checked):
            if len(match[0]) > 1:
                continue
            tail = checked[match.start():match.start()+6]
            if (not final and match[0] == b'\\' and
                    (tail == b'\\' or re.fullmatch(rb'\\u[0-9A-Fa-f]{0,3}', tail))):
                self.pending_escape = tail
                break
            self.invalid = True
            break
        data = self.pending_slash + data
        # JSON permits escaped slashes in base64. Keep a split escape together.
        self.pending_slash = b''
        if not final and data.endswith(b'\\'):
            data, self.pending_slash = data[:-1], b'\\'
        pos = 0
        for match in _BASE64.finditer(data):
            if pos < match.start():
                self._end_run()
                self._keep(data[pos:match.start()])
            if not self.run_size:
                header = _URI_END.search(self.kept[-96:])
                self.image_type = ('image/' + header[1].decode() if header else
                                   'base64' if self.bare_image and not self.kept else None)
            self.run_size += match.end() - match.start()
            if self.run_size <= (IMAGE_MIN if self.image_type and not self.generic else FIELD_BYTES):
                self.run.extend(data[match.start():match.end()])
            pos = match.end()
        if pos < len(data):
            self._end_run()
            self._keep(data[pos:])

    def finish(self):
        self.feed(b'', final=True)
        self._end_run()
        if self.overflow:
            self.stats['lossy_fields'] += 1
            self.stats['omitted_bytes'] += self.length - self.image_bytes
            return f'{PLACEHOLDER}{self.length} bytes of text]'.encode()
        return self.kept


class _Line:
    def __init__(self):
        self.out = bytearray()
        self.string = None
        self.short = None
        self.escape = False
        self.last_string = self.key = None
        self.depth = self.fields = 0
        self.containers = []
        self.invalid = False
        self.stats = dict(image_fields=0, lossy_fields=0, omitted_bytes=0)

    def _body(self, data):
        if self.string or len(self.short) + len(data) > IMAGE_MIN:
            if self.string is None:
                self.string = _String(self.key, self.stats)
                self.string.feed(self.short)
                self.short.clear()
            self.string.feed(data)
        else:
            self.short.extend(data)

    def feed(self, data):
        pos = 0
        while pos < len(data) and not self.invalid:
            if len(self.out) > RECORD_BYTES:
                self.invalid = True
                break
            if self.short is not None:
                # At record start short is None. While quoted, backslashes
                # protect exactly one following byte, even across read blocks.
                start = pos
                if self.escape:
                    pos += 1
                    self.escape = False
                while match := _STRING_END.search(data, pos):
                    pos = match.end()
                    if data[match.start()] == 34:
                        break
                    if pos == len(data):
                        self.escape = True
                        match = None
                        break
                    pos += 1
                if not match:
                    self._body(data[start:])
                    return
                self._body(data[start:match.start()])
                body = self.short if self.string is None else self.string.finish()
                if self.string and self.string.invalid:
                    self.invalid = True
                    return
                if self.containers:
                    frame = self.containers[-1]
                    if self.key in _TYPE_KEYS and (body.startswith(b'image/') or body in
                            (b'image', b'input_image', b'output_image', b'image_url')):
                        frame['image'] = True
                    if self.string and self.string.generic_runs:
                        frame['runs'] += self.string.generic_runs
                        frame['fields'] += 1
                self.out.extend(body)
                self.out.append(34)
                self.last_string = bytes(body) if len(body) <= 96 else None
                self.short = self.string = self.key = None
                continue
            match = _STRUCTURE.search(data, pos)
            if not match:
                self.out.extend(data[pos:])
                return
            self.out.extend(data[pos:match.end()])
            char = data[match.start()]
            pos = match.end()
            if char == 34:
                self.short = bytearray()
            elif char == 58:
                self.key = self.last_string
                self.fields += 1
            else:
                self.key = None
                if char in b'{[':
                    self.depth += 1
                    self.containers.append(dict(image=False, runs=0, fields=0))
                elif char in b'}]':
                    self.depth -= 1
                    if self.containers:
                        frame = self.containers.pop()
                        # A generic "data" member may precede its MIME sibling.
                        # Without an image discriminator this is a lossy text field.
                        if frame['runs'] and not frame['image']:
                            self.stats['image_fields'] -= frame['runs']
                            self.stats['lossy_fields'] += frame['fields']
            if self.depth > 128 or self.fields > 16384:
                self.invalid = True


class ProjectedRecords:
    """Replay a private temporary file, one bounded record per iteration."""
    def __init__(self):
        self.spool = tempfile.TemporaryFile()
        self.stats = dict(image_fields=0, lossy_fields=0, omitted_bytes=0, omitted_lines=0,
                          skipped_lines=0, complete_lines=0)
        self.ref = {}
        self.model = ''

    def capture(self, stream, size):
        digest = hashlib.sha256()
        through = start = offset = 0
        committed = None
        line = None
        while offset < size:
            block = stream.read(min(BLOCK_BYTES, size - offset))
            if not block:
                raise ValueError('capture_source_truncated')
            last_lf = block.rfind(b'\n') + 1
            if last_lf:
                digest.update(block[:last_lf])
                committed = digest.copy()
                through = offset + last_lf
            digest.update(block[last_lf:])
            pos = 0
            while pos < len(block):
                lf = block.find(b'\n', pos)
                end = lf if lf >= 0 else len(block)
                part = block[pos:end]
                if (line is None and lf >= 0 and len(part) <= IMAGE_MIN
                        and part.count(b'{') + part.count(b'[') <= 128):
                    self._write(part, start, offset + lf + 1)
                else:
                    if line is None:
                        line = _Line()
                    line.feed(part)
                    if lf >= 0:
                        self._write(line.out, start, offset + lf + 1, line)
                if lf >= 0:
                    line = None
                    start = offset + lf + 1
                pos = end + 1
            offset += len(block)
        return through, committed.hexdigest() if committed else None

    def _write(self, body, start, end, line=None):
        stats = self.stats
        stats['complete_lines'] += 1
        invalid = line and (line.invalid or line.short is not None or len(body) > RECORD_BYTES)
        if not invalid and body.strip():
            try:
                obj = json.loads(body.decode('utf-8'))
                invalid = not isinstance(obj, dict)
                if b'\\ud' in body.lower():
                    json.dumps(obj, ensure_ascii=False).encode('utf-8')
                if not invalid and not self.model and isinstance(obj.get('model'), str):
                    self.model = obj['model']
            except (ValueError, UnicodeError, RecursionError):
                invalid = True
        if invalid:
            body = b'!unavailable projected record!'
            stats['skipped_lines'] += 1
            stats['omitted_bytes'] += end - start - 1
        elif line:
            for key, value in line.stats.items():
                stats[key] += value
        if invalid or (line and line.stats['omitted_bytes']):
            stats['omitted_lines'] += 1
        self.spool.write(_HEADER.pack(start, end, len(body)))
        self.spool.write(body)

    def lines(self):
        self.spool.seek(0)
        for line_no, header in enumerate(iter(lambda: self.spool.read(_HEADER.size), b''), 1):
            start, end, length = _HEADER.unpack(header)
            self.ref = dict(line_no=line_no, byte_start=start, byte_end=end)
            yield self.spool.read(length).decode('utf-8') + '\n'

    def metadata(self):
        return dict(projection_version=PROJECTION_VERSION, capture_mode='projected',
                    index_coverage='partial' if self.stats['lossy_fields'] or self.stats['skipped_lines']
                                   else 'images_omitted' if self.stats['image_fields'] else 'complete',
                    **self.stats)

    def audit(self, session):
        from audit.extractor import _codex_events, build_audit_from_events

        def events():
            for number, line in enumerate(self.lines(), 1):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get('type') != 'response_item':
                    continue
                yield from _codex_events(obj, number)

        self.audit_coverage = 'projected_records'
        return build_audit_from_events(events(), session_id=session['id'], source='codex',
                                       model=self.model, started_at=session['start_ts_ms'],
                                       ended_at=session['end_ts_ms'], parse_errors=self.stats['skipped_lines'],
                                       summary_db=session['messages'].conn)


class ProjectedMessages:
    """Stage bounded batches before the parser has determined the final recording ID."""
    def __init__(self, conn, projection):
        self.conn = conn
        self.projection = projection
        self.count = self.unknown_time = self.unparsed = self.batch_bytes = 0
        self.batch = []
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS projected_messages '
                     '(ts INTEGER, role, kind, text, summary, verified INTEGER, meta)')
        conn.execute('DELETE FROM projected_messages')
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS projected_calls (id TEXT PRIMARY KEY, name TEXT)')
        conn.execute('DELETE FROM projected_calls')

    def __setitem__(self, call_id, name):
        self.conn.execute('INSERT OR REPLACE INTO projected_calls VALUES (?,?)', (call_id, name))

    def get(self, call_id):
        row = self.conn.execute('SELECT name FROM projected_calls WHERE id=?', (call_id,)).fetchone()
        return row[0] if row else None

    def append(self, msg):
        from .sources import message_activity_fields
        msg['raw_ref'] = {**self.projection.ref, **msg.get('raw_ref', {})}
        ts = msg['ts_ms']
        raw = msg['kind'].startswith('raw_json')
        self.count += 1
        self.unparsed += raw
        self.unknown_time += (msg['role'] in ('user', 'assistant', 'tool') and
                              not msg['kind'].startswith('context') and (not ts or raw))
        meta = message_activity_fields(msg)
        summary = json.dumps(msg['tool_summary']) if 'tool_summary' in msg else None
        size = len(msg['text'].encode('utf-8')) + len(meta.encode('utf-8')) + len(summary or '') + 128
        if self.batch and (self.batch_bytes + size > 4 * 1024 * 1024 or len(self.batch) >= 512):
            self.flush()
        self.batch.append((ts, msg['role'], msg['kind'], msg['text'], summary, ts if ts and not raw else None, meta))
        self.batch_bytes += size

    def flush(self):
        self.conn.executemany('INSERT INTO projected_messages VALUES(?,?,?,?,?,?,?)', self.batch)
        self.batch.clear()
        self.batch_bytes = 0

    def publish(self, session):
        self.flush()
        self.conn.execute('''
            WITH ordinals AS MATERIALIZED (
                SELECT rowid AS seq, ROW_NUMBER() OVER (ORDER BY COALESCE(NULLIF(ts,0),?),rowid)-1 AS slot
                FROM projected_messages
            )
            INSERT INTO messages(session_id, ts_ms, role, kind, text, tool_summary_json, activity_ts_ms,
                                 activity_meta_json, message_index)
            SELECT ?, COALESCE(NULLIF(ts,0),?), role, kind, text, summary, verified, meta,
                   (SELECT slot FROM ordinals WHERE seq=m.rowid)
            FROM projected_messages m ORDER BY rowid''', (session['start_ts_ms'], session['id'], session['start_ts_ms']))
        self.conn.execute('DELETE FROM projected_messages')
        self.conn.execute('DELETE FROM projected_calls')
        return dict(messages_without_verified_time=self.unknown_time, unparsed_indexed_records=self.unparsed)
