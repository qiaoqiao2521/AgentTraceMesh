#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { readFileSync } from 'node:fs';
import { MemoryStore, memoryRoot } from './store.js';

const { positionals, values } = parseArgs({
  allowPositionals: true,
  options: {
    'data-dir': { type: 'string' }, companion: { type: 'string', default: 'companion' },
    apply: { type: 'boolean', default: false },
  },
});
const [command, argument] = positionals;
const store = new MemoryStore(memoryRoot(values['data-dir'], values.companion));
try {
  let result;
  switch (command) {
    case 'list': result = store.search('', { activeOnly: false, limit: 200 }); break;
    case 'search': result = store.search(argument); break;
    case 'import-cm':
      if (!argument) throw new Error('Pass a selected CM MEMORY.md path; preview is the default.');
      result = store.importLegacy(readFileSync(argument, 'utf8'), { apply: values.apply, source: argument });
      break;
    default:
      console.log('Usage: tracemesh-memory list | search QUERY | import-cm FILE [--apply] [--data-dir DIR] [--companion NAME]');
  }
  if (result) {
    console.log(JSON.stringify(result, null, 2));
    if (result.errors?.length) process.exitCode = 1;
  }
} catch (error) {
  console.error('WARNING ' + error.message);
  process.exitCode = 1;
}
