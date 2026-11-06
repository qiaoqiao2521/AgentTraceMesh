"""Query-cost regressions use VM work and projections, not wall-clock limits."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from history_core.sources import Indexer


class SearchQueryCostTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = self.root / 'source'; source.mkdir()
        cache = self.root / 'cache'; cache.mkdir()
        self.index = Indexer(source, cache, 'codex')
        self.addCleanup(self.index.conn.close)

    def seed(self, sid, stamp, messages):
        with self.index.conn:
            self.index.conn.execute('''INSERT INTO sessions
                (id,file_path,start_ts_ms,end_ts_ms,title,cwd,message_count,files_touched_json)
                VALUES(?,?,?,?,?,?,?,?)''',
                (sid, str(self.root / (sid+'.jsonl')), stamp, stamp, 'query cost', '/fixture',
                 len(messages), '{"local":[]}'))
            self.index.conn.executemany('''INSERT INTO messages(session_id,ts_ms,role,kind,text)
                VALUES(?,?,?,?,?)''', [(sid,n,*row) for n,row in enumerate(messages)])

    def page(self, query='alpha beta', role='user', limit=3, offset=0):
        return self.index._list_tool_evidence_page(query,None,None,limit,offset,None,None,role=role)

    def test_role_candidate_cost_does_not_grow_with_other_role_noise(self):
        self.seed('selected',1,[('user','message','alpha'),('user','message','beta')])

        def measured():
            ticks = [0]
            @contextmanager
            def budget(index):
                index.conn.set_progress_handler(lambda: ticks.__setitem__(0,ticks[0]+1) or 0,1)
                try:
                    yield index.conn
                finally:
                    index.conn.set_progress_handler(None,0)
            with patch('history_core.reuse.query_budget',budget):
                result = self.page(query='alpha missing_user_term')
            self.assertEqual(result['items'],[], 'tool words cannot satisfy the user query')
            return ticks[0]

        before = measured()
        with self.index.conn:
            self.index.conn.executemany('''INSERT INTO messages(session_id,ts_ms,role,kind,text)
                VALUES('selected',?,'tool','tool_result','missing_user_term')''',[(n+2,) for n in range(5000)])
        after = measured()
        self.assertLessEqual(after, before+100,
                             'role search must filter other-role records in the index')
        self.assertEqual([r['id'] for r in self.page()['items']],['selected'])

    def test_session_metadata_is_read_only_after_candidate_pagination(self):
        for n in range(50):
            self.seed('s%03d'%n,n,[('user','message','alpha beta')])
        conn = self.index.conn
        columns = [row[1] for row in conn.execute('PRAGMA table_info(sessions)')]
        seen = []
        conn.create_function('observe_metadata',1,lambda value: seen.append(1) or value)
        conn.execute('ALTER TABLE sessions RENAME TO stored_sessions')
        projection = ','.join('observe_metadata(files_touched_json) AS files_touched_json'
                              if name=='files_touched_json' else '"'+name+'"' for name in columns)
        conn.execute('CREATE VIEW sessions AS SELECT '+projection+' FROM stored_sessions')
        first = self.page(limit=3)
        self.assertEqual([r['id'] for r in first['items']],['s049','s048','s047'])
        self.assertTrue(first['has_more'])
        self.assertLessEqual(len(seen),4,'only this page and its lookahead may read metadata')
        seen.clear()
        second = self.page(limit=3,offset=3)
        self.assertEqual([r['id'] for r in second['items']],['s046','s045','s044'])
        self.assertLessEqual(len(seen),4)

    def test_tool_ranking_preserves_ascii_case_literals_and_nul_tail(self):
        samples = [('原文','prefix\0原文',1),('AbC','prefix\0aBc',1),
                   ('100%_\\','prefix\x00100%_\\',1),('ä','prefix\0Ä',0)]
        for n,(query,text,expected) in enumerate(samples):
            with self.subTest(query=query):
                old,new = 'old-%d'%n,'new-%d'%n
                self.seed(old,n*2,[('tool','tool_result',text)])
                self.seed(new,n*2+1,[('user','message','metadata match only')])
                self.index.conn.execute('UPDATE sessions SET title=? WHERE id IN (?,?)',
                                        (query,old,new))
                result = self.page(query=query,role=None,limit=10)
                rows = {r['id']:r for r in result['items']}
                self.assertEqual(rows[old]['tool_match_terms'],expected)
                self.assertEqual(result['items'][0]['id'],old if expected else new)


if __name__=='__main__':
    unittest.main()
