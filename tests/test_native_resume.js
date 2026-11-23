const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('function isWindowsPath('), source.indexOf('function getSourceLabel('));
const commands = source.slice(source.indexOf('function buildResumeInvocation('), source.indexOf('function clamp('));
const context = vm.createContext({currentWslDistro: 'Ubuntu'});
vm.runInContext(helpers + commands, context);
const id = 'ses_Example123';
for (const system of ['linux', 'windows', 'wsl']) {
  assert.equal(context.buildResumeInvocation(system, 'opencode', id), `opencode --session ${id}`);
  for (const invalid of ['-', '', 'ses_a;touch /tmp/pwn', 'ses_$(id)', 'ses_a\nwhoami', '--continue']) {
    assert.equal(context.buildResumeInvocation(system, 'opencode', invalid), '');
  }
}
assert.equal(context.buildResumeCommands('linux', 'opencode', '/tmp/a b', id).ps,
  `cd '/tmp/a b' && opencode --session ${id}`);
assert.equal(context.buildResumeCommands('windows', 'opencode', 'C:\\a b', id).ps,
  `Set-Location -LiteralPath 'C:\\a b'; opencode --session ${id}`);
assert.match(context.buildResumeCommands('wsl', 'opencode', '/tmp/a b', id).ps, /wsl.exe -d Ubuntu/);
assert.match(context.buildResumeCommands('wsl', 'opencode', '/tmp/a b', id).wsl, /opencode --session ses_Example123$/);
assert.equal(context.buildResumeInvocation('linux', 'codex', 'abc'), 'codex resume abc');
assert.equal(context.buildResumeInvocation('linux', 'hermes', 'abc'), '');
for (const source of ['codex', 'claude']) {
  const alias = 'native@recording-0123456789abcdef';
  assert.equal(context.buildResumeInvocation('linux', source, alias), '', 'a recording alias is not an exact native resume target');
  assert.equal(context.buildResumeCommands('linux', source, '/tmp/repo', alias).ps, '-');
  assert.notEqual(context.buildResumeInvocation('linux', source, 'ordinary@recording-note'), '', 'the internal alias suffix check must not reject ordinary IDs');
}
assert.equal(context.buildResumeInvocation('linux', 'codebuddy', 'abc-123'), 'cbc --resume abc-123');
for (const bad of ['--help', 'a;echo hi', 'a$(id)', 'a\nwhoami']) {
  assert.equal(context.buildResumeInvocation('linux', 'codebuddy', bad), '');
}
console.log('Native resume commands: passed');
