import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { MemoryStore } from '../store.js';

const plugin = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'tracemesh-memory-migration-'));
  const dshHome = join(root, 'existing-dsh');
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, dshHome };
}

function cli(args, dshHome, { status = 0 } = {}) {
  const result = spawnSync(process.execPath, [join(plugin, 'cli.js'), ...args], {
    env: { ...process.env, DSH_HOME: dshHome }, encoding: 'utf8', timeout: 10000,
  });
  assert.equal(result.status, status, result.stderr || result.error?.message);
  return JSON.parse(result.stdout);
}

function oldRecord(root, id = 'existing-preference') {
  const directory = join(root, 'memories');
  mkdirSync(directory, { recursive: true });
  const content = 'Prefers quiet evenings.';
  const metadata = {
    kind: 'preference', status: 'active', pinned: true, importance: 0.5,
    due: '', createdAt: '2026-10-23T16:30:00.000Z', updatedAt: '2026-10-23T16:30:00.000Z',
    source: { sessionId: 'existing-session', messageId: 'existing-user-message' },
  };
  const bytes = '---\n' + JSON.stringify(metadata, null, 2) + '\n---\n\n' + content + '\n';
  const path = join(directory, id + '.md');
  writeFileSync(path, bytes);
  return { path, bytes, content, metadata };
}

test('relocated CLI reads the existing DSH store without moving or rewriting records', (t) => {
  const { dshHome } = fixture(t);
  const existing = join(dshHome, 'companion-memory', 'friend');
  const record = oldRecord(existing);
  const reflectionDir = join(existing, 'reflections');
  mkdirSync(reflectionDir);
  const reflection = '# 2026-10-24\n\nAn existing editable summary.\n';
  writeFileSync(join(reflectionDir, '2026-10-24.md'), reflection);

  const result = cli(['list', '--companion', 'friend'], dshHome);
  assert.equal(result.total, 1);
  assert.equal(result.records[0].id, 'existing-preference');
  assert.equal(result.records[0].content, record.content);
  assert.deepEqual(result.records[0].source, record.metadata.source);
  assert.equal(readFileSync(record.path, 'utf8'), record.bytes);
  assert.equal(new MemoryStore(existing).reflection().content, reflection);
  assert.deepEqual(readdirSync(dshHome), ['companion-memory']);
  assert.deepEqual(readdirSync(join(dshHome, 'companion-memory')), ['friend']);
});

test('relocated CLI respects the existing explicit base directory and companion identity', (t) => {
  const { root, dshHome } = fixture(t);
  const dataDir = join(root, 'selected-memory-base');
  const record = oldRecord(join(dataDir, 'workshop-friend'));
  const result = cli(['list', '--data-dir', dataDir, '--companion', 'workshop-friend'], dshHome);
  assert.equal(result.total, 1);
  assert.equal(result.records[0].id, 'existing-preference');
  assert.equal(readFileSync(record.path, 'utf8'), record.bytes);
  assert.equal(existsSync(dshHome), false);
  assert.equal(cli(['list', '--data-dir', dataDir, '--companion', 'other'], dshHome).total, 0);
  assert.deepEqual(readdirSync(dataDir), ['workshop-friend']);
});

test('legacy file import is explicit, preview-only by default, and idempotent through the new CLI', (t) => {
  const { root, dshHome } = fixture(t);
  const legacy = join(root, 'selected-MEMORY.md');
  const original = [
    '# CM Memory', '## Preference',
    '- Prefers quiet evenings. _(id: old-one; status: active; scope: local; source: chat#L1; promoted: 2026-10-23)_',
    '## Fact',
    '- A retired address. _(id: old-two; status: superseded; scope: local; source: chat#L2; promoted: 2026-10-23)_',
    '## Project', '- This project must stay in SpecMesh.',
  ].join('\n');
  writeFileSync(legacy, original);
  const args = ['import-cm', legacy, '--companion', 'friend'];
  const preview = cli(args, dshHome);
  assert.equal(preview.preview, true);
  assert.equal(preview.candidates.length, 1);
  assert.equal(preview.skipped, 2);
  assert.equal(existsSync(dshHome), false);

  const applied = cli([...args, '--apply'], dshHome);
  assert.equal(applied.imported, 1);
  const before = cli(['list', '--companion', 'friend'], dshHome).records[0];
  assert.equal(cli([...args, '--apply'], dshHome).imported, 0);
  const after = cli(['list', '--companion', 'friend'], dshHome).records[0];
  assert.equal(after.id, before.id);
  assert.equal(after.source.import, legacy);
  assert.match(after.source.legacyMetadata, /source: chat#L1/);
  assert.equal(readFileSync(legacy, 'utf8'), original);
});

test('legacy apply reports a failed item with a nonzero CLI status instead of claiming success', (t) => {
  const { root, dshHome } = fixture(t);
  const legacy = join(root, 'selected-MEMORY.md');
  writeFileSync(legacy, '## Fact\n- A selected personal fact.\n');
  const blockedDirectory = join(root, 'not-a-directory');
  writeFileSync(blockedDirectory, 'preserve this existing file');
  const result = cli(['import-cm', legacy, '--apply', '--data-dir', blockedDirectory], dshHome, { status: 1 });
  assert.equal(result.preview, false);
  assert.equal(result.imported, 0);
  assert.equal(result.errors.length, 1);
  assert.equal(readFileSync(blockedDirectory, 'utf8'), 'preserve this existing file');
  assert.equal(existsSync(dshHome), false);
});
