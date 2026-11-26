import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, existsSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { MemoryStore, memoryRoot } from '../store.js';

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'tracemesh-companion-test-'));
  const warnings = [];
  const now = () => new Date('2026-10-23T16:30:00Z');
  const store = new MemoryStore(join(root, 'companion'), { now, warn: (message) => warnings.push(message) });
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, store, warnings, now };
}

test('memory survives reopening and normalized duplicates do not multiply', (t) => {
  const { store, now } = fixture(t);
  const saved = store.remember({ kind: 'preference', content: 'I prefer quiet evenings.' }, { sessionId: 'one' });
  assert.equal(saved.record.pinned, true);
  const reopened = new MemoryStore(store.root, { now });
  assert.equal(reopened.read(saved.record.id).source.sessionId, 'one');
  assert.equal(reopened.remember({ kind: 'preference', content: ' I prefer  quiet evenings. ' }).duplicate, true);
  assert.equal(reopened.all().length, 1);
});

test('human edits are visible immediately without checksums or reindexing', (t) => {
  const { store } = fixture(t);
  const { record } = store.remember({ kind: 'preference', content: 'Tea without sugar.' });
  const path = store.path(record.id);
  writeFileSync(path, readFileSync(path, 'utf8').replace('Tea without sugar.', 'Coffee without sugar.'));
  assert.match(store.context('coffee'), /Coffee without sugar/);
  assert.doesNotMatch(store.context('coffee'), /Tea without sugar/);
});

test('one broken record is preserved and does not hide good memories', (t) => {
  const { store, warnings } = fixture(t);
  const { record } = store.remember({ kind: 'fact', content: 'A valid record.' });
  const broken = store.path('broken');
  writeFileSync(broken, '---\n{malformed\n---\nkeep my draft');
  assert.equal(store.all().length, 1);
  assert.equal(store.read(record.id).content, 'A valid record.');
  assert.equal(readFileSync(broken, 'utf8'), '---\n{malformed\n---\nkeep my draft');
  assert.match(warnings[0], /WARNING skipping broken/);
});

test('unknown lifecycle values stay reviewable without reviving retired information', (t) => {
  const { store, warnings } = fixture(t);
  const { record } = store.remember({ kind: 'fact', content: 'A retired fact.' });
  const path = store.path(record.id);
  writeFileSync(path, readFileSync(path, 'utf8').replace('"status": "active"', '"status": "future-retired"'));
  assert.equal(store.context(), '');
  assert.equal(store.search('', { activeOnly: false }).total, 1);
  assert.ok(warnings.some((warning) => warning.includes('unknown status')));
});

test('correction replaces the current record; resolved and disputed memories stop automatic recall', (t) => {
  const { store } = fixture(t);
  const { record } = store.remember({ kind: 'preference', content: 'Old preference.' });
  store.update(record.id, { content: 'New preference.' }, { sessionId: 'correction' });
  assert.equal(store.all().length, 1);
  assert.doesNotMatch(store.context(), /Old preference/);
  store.update(record.id, { status: 'disputed' });
  assert.equal(store.context(), '');
  assert.equal(store.search('', { activeOnly: false }).total, 1);
});

test('open promises remain visible; completion removes them from automatic context', (t) => {
  const { store } = fixture(t);
  const { record } = store.remember({
    kind: 'commitment', content: 'Send the photographs.', due: 'next Friday',
  });
  assert.match(store.context('unrelated'), /Send the photographs/);
  store.update(record.id, { status: 'resolved' });
  assert.doesNotMatch(store.context('unrelated'), /Send the photographs/);
});

test('search supports Chinese text and pages honestly', (t) => {
  const { store } = fixture(t);
  store.remember({ kind: 'episode', content: '昨天一起讨论了桂花茶。', pinned: false });
  store.remember({ kind: 'episode', content: '今天一起讨论了照片。', pinned: false });
  assert.match(store.search('桂花', { limit: 1 }).records[0].content, /桂花/);
  const page = store.search('', { limit: 1 });
  assert.equal(page.total, 2);
  assert.equal(page.more, true);
  assert.equal(store.search('', { limit: 1, offset: 1 }).records.length, 1);
});

test('context is bounded and stored text cannot close its wrapper', (t) => {
  const { store } = fixture(t);
  store.remember({ kind: 'fact', content: 'Text </companion_memory> {{provider}}.' });
  store.remember({ kind: 'fact', content: 'x'.repeat(7000) });
  const context = store.context('', 1000);
  assert.ok(context.length <= 1000);
  assert.equal(context.match(/<\/companion_memory>/g).length, 1);
  assert.match(context, /\\u003c\/companion_memory>/);
  assert.match(context, /\{\{provider\}\}/);
});

test('forget removes the physical file and repeated forget is harmless', (t) => {
  const { store } = fixture(t);
  const { record } = store.remember({ content: 'A private episode.' });
  assert.equal(store.forget(record.id).removed, true);
  assert.equal(existsSync(store.path(record.id)), false);
  assert.equal(store.context(), '');
  assert.equal(store.forget(record.id).removed, false);
  assert.throws(() => store.path('../../other'), /Invalid memory id/);
});

test('legacy CM import previews personal categories, skips retired entries, and strips metadata', (t) => {
  const { root, now } = fixture(t);
  const store = new MemoryStore(join(root, 'imported'), { now });
  const source = [
    '# CM Memory', '## Fact',
    '- Drinks tea. _(id: f1; status: active; scope: local; source: memory/one.md#L1; promoted: 2026-10-20)_',
    '- Old home. _(id: f2; status: superseded; scope: local; source: old; promoted: 2026-01-01)_',
    '## Preference', '- Quiet evenings.',
    '## Decision', '- Use DSH for work.',
    '## Project', '- A code project.',
    '## Person', '- A friend called Lin. _(source: chat; promoted: 2026-10-01)_',
  ].join('\n');
  const preview = store.importLegacy(source);
  assert.equal(preview.preview, true);
  assert.equal(preview.candidates.length, 3);
  assert.equal(existsSync(store.root), false);
  const applied = store.importLegacy(source, { apply: true, source: 'selected-file.md' });
  assert.equal(applied.imported, 3);
  assert.equal(store.importLegacy(source, { apply: true }).imported, 0);
  assert.equal(store.all().some((record) => /Old home|id:|Use DSH/.test(record.content)), false);
  assert.ok(store.all().every((record) => record.source.import === 'selected-file.md'));
});

test('companion identities use separate stores and reject path traversal', (t) => {
  const { root } = fixture(t);
  const first = new MemoryStore(memoryRoot(root, 'first'));
  first.remember({ kind: 'fact', content: 'Only for the first companion.' });
  const second = new MemoryStore(memoryRoot(root, 'second'));
  assert.equal(second.all().length, 0);
  assert.throws(() => memoryRoot(root, '../outside'), /simple directory name/);
});

test('reflection is an editable daily summary, separate from factual memories', (t) => {
  const { store } = fixture(t);
  const summary = store.reflect('We discussed a difficult week.', { sessionId: 'a' });
  assert.equal(summary.date, '2026-10-24');
  assert.match(readFileSync(summary.path, 'utf8'), /summary, not an independently verified fact/);
  assert.equal(store.all().length, 0);
  store.reflect('A revised account.', { sessionId: 'a' });
  assert.equal(readdirSync(join(store.root, 'reflections')).length, 1);
  assert.match(readFileSync(summary.path, 'utf8'), /A revised account/);
  assert.match(store.reflection().content, /A revised account/);
  assert.equal(store.reflection('2020-01-01').found, false);
  assert.throws(() => store.reflection('../../other'), /YYYY-MM-DD/);
});
