"""Absolute message slots shared by derived-index writers and bounded readers.

Slots include context and other hidden records. Their order is the existing
reader order: normalized timestamp, then insertion ID. Old caches stay usable
without reparsing recordings or copying message bodies into a window sort.
"""


def message_indexes(messages, start_ts_ms):
    """Return slots in input order; equal timestamps retain insertion order."""
    order = sorted(range(len(messages)),
                   key=lambda n: (int(messages[n]["ts_ms"] or start_ts_ms), n))
    slots = [0] * len(messages)
    for slot, position in enumerate(order):
        slots[position] = slot
    return slots


def ordered_message_ids_sql(connection, session_id):
    """Return a two-column (id, message_index) query and its bound parameters.

    Nullable columns permit old running writers to keep their INSERT lists.
    Only a complete, atomically written session uses persisted slots. An old
    writer's INSERT OR REPLACE resets the session marker to NULL; an append
    changes its count. Both fall back without mixing old and new slots.
    idx_messages_session_ts(session_id, ts_ms) also covers the integer primary
    key, so this fallback needs no new full-cache index build or TEXT sort.
    """
    columns = {row[1] for row in connection.execute('PRAGMA table_info(messages)')}
    session_columns = {row[1] for row in connection.execute('PRAGMA table_info(sessions)')}
    if 'message_index' in columns and 'message_index_count' in session_columns:
        marker = connection.execute('SELECT message_index_count FROM sessions WHERE id=?',
                                    (session_id,)).fetchone()
        if marker and marker[0] is not None:
            count = connection.execute('SELECT COUNT(*) FROM messages WHERE session_id=?',
                                       (session_id,)).fetchone()[0]
            if marker[0] == count:
                return ('SELECT id, message_index FROM messages WHERE session_id=?', (session_id,))
    return ('''SELECT id, ROW_NUMBER() OVER (ORDER BY ts_ms,id)-1 AS message_index
               FROM messages WHERE session_id=?''', (session_id,))
