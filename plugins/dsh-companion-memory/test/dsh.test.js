import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { randomUUID } from 'node:crypto';
import { Context } from '@deepseek-ai/cordis';
import LlmRuntime, { LlmAdapter, createUserMessage } from '@deepseek-ai/dsh-llm';
import Sessions, { SessionId } from '@deepseek-ai/dsh-session';
import Projections from '@deepseek-ai/dsh-session-projection';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import Tools from '@deepseek-ai/dsh-tools';
import Agents from '@deepseek-ai/dsh-agent';
import AgentLoop from '@deepseek-ai/dsh-agent-loop';
import * as Companion from '../index.js';
import { MemoryStore, memoryRoot } from '../store.js';

// A scripted provider exercises the real DSH loop without network calls or credentials.
class ScriptedProvider extends LlmAdapter {
  constructor(script) { super(); this.script = script; this.requests = []; }
  async resolveModel(provider, model) { return { provider, id: model, name: model }; }
  async *stream(request) {
    this.requests.push(request);
    const step = this.script.shift();
    if (!step) throw new Error('Unexpected model request');
    yield* typeof step === 'function' ? step(request) : step;
  }
}

function text(value = 'done') {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: value },
    { type: 'block-end', index: 0, block: { type: 'text', text: value } },
    { type: 'finish', reason: { kind: 'stop' } },
  ];
}

function call(args) {
  const id = randomUUID();
  const argumentsJson = JSON.stringify(args);
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'tool-call-delta', index: 0, id, name: 'companion_memory', argumentsDelta: argumentsJson },
    { type: 'block-end', index: 0, block: { type: 'tool-call', id, name: 'companion_memory', arguments: argumentsJson } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ];
}

async function harness(t, dataDir, script) {
  const ctx = new Context();
  t.after(() => ctx.fiber.dispose());
  for (const plugin of [LlmRuntime, Sessions, Projections, SystemPrompt, Tools, Agents]) await ctx.plugin(plugin);
  await ctx.plugin(AgentLoop, { agents: [] });
  const fiber = await ctx.plugin(Companion, { dataDir, companion: 'friend', maxContextChars: 6000 });
  const provider = new ScriptedProvider(script);
  ctx.llm.registerAdapter(['scripted'], provider);
  const agent = await ctx.agentLoop.create(SessionId(randomUUID()), { provider: 'scripted', model: 'offline' });
  return { ctx, agent, provider, fiber };
}

async function send(agent, value, kind = 'user') {
  agent.followup(createUserMessage({ content: [{ type: 'text', text: value }], source: { kind } }));
  await agent.whenIdle();
}

function root(t) {
  const path = mkdtempSync(join(tmpdir(), 'tracemesh-dsh-integration-'));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  return path;
}

function allText(request) {
  return JSON.stringify(request.messages);
}

test('real DSH tool call writes a sourced memory; a fresh runtime recalls it', async (t) => {
  const dataDir = root(t);
  const first = await harness(t, dataDir, [
    call({ action: 'remember', kind: 'preference', content: 'Prefers osmanthus tea.', evidence: 'I prefer osmanthus tea.' }), text('remembered'),
  ]);
  await send(first.agent, 'Please remember that I prefer osmanthus tea.');
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  const records = store.all();
  assert.equal(records.length, 1);
  assert.equal(records[0].source.sessionId, first.agent.id);
  assert.match(records[0].source.quote, /osmanthus/);
  assert.ok(records[0].source.messageId);
  assert.equal(first.provider.requests.length, 2);
  await first.ctx.fiber.dispose();

  const second = await harness(t, dataDir, [text('recalled')]);
  await send(second.agent, 'What drink do I prefer?');
  assert.match(allText(second.provider.requests[0]), /Prefers osmanthus tea/);
  assert.equal(second.provider.requests.length, 1);
  assert.ok(second.provider.requests[0].tools.some((tool) => tool.name === 'companion_memory'));
});

test('actual DSH conversations correct, complete and forget without duplicate authority', async (t) => {
  const dataDir = root(t);
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  const pref = store.remember({ kind: 'preference', content: 'Old drink.' }).record;
  const promise = store.remember({ kind: 'commitment', content: 'Send a photograph.' }).record;
  const h = await harness(t, dataDir, [
    call({ action: 'update', id: pref.id, content: 'New drink.' }), text(),
    call({ action: 'resolve', id: promise.id }), text(),
    call({ action: 'forget', id: pref.id }), text(),
  ]);
  await send(h.agent, 'Correct my drink preference.');
  assert.equal(store.all().length, 2);
  assert.equal(store.read(pref.id).content, 'New drink.');
  await send(h.agent, 'The photograph has been sent.');
  assert.equal(store.read(promise.id).status, 'resolved');
  await send(h.agent, 'Forget the drink preference.');
  assert.equal(store.all().length, 1);
  assert.equal(store.context(), '');
});

test('manual edits reach the next native DSH request; unload removes tools and contexts', async (t) => {
  const dataDir = root(t);
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  const pref = store.remember({ kind: 'preference', content: 'Previous preference.' }).record;
  const h = await harness(t, dataDir, [text(), text(), text()]);
  await send(h.agent, 'hello');
  assert.match(allText(h.provider.requests[0]), /Previous preference/);
  writeFileSync(store.path(pref.id), readFileSync(store.path(pref.id), 'utf8').replace('Previous preference.', 'Edited preference.'));
  await send(h.agent, 'hello again');
  const latestContext = h.provider.requests[1].messages.filter((message) =>
    message.source?.kind === 'runtime-context' || message.source?.kind === 'system-prompt');
  assert.match(JSON.stringify(latestContext), /Edited preference/);
  await h.fiber.dispose();
  assert.equal(h.ctx.tools.schemas().some((tool) => tool.name === 'companion_memory'), false);
});

test('a later non-human turn cannot write using a stale human source', async (t) => {
  const dataDir = root(t);
  const h = await harness(t, dataDir, [
    text(), call({ action: 'remember', kind: 'fact', content: 'Invented user biography.' }), text(),
  ]);
  await send(h.agent, 'hello');
  await send(h.agent, 'background event', 'test-background');
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  assert.equal(store.all().length, 0);
  const result = h.agent.session.snapshotEvents().find((event) =>
    event.type === 'tool/result' && JSON.stringify(event.data).includes('No accepted direct user message'));
  assert.ok(result);
});

test('background child agents do not receive personal memory or write into it', async (t) => {
  const dataDir = root(t);
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  store.remember({ kind: 'fact', content: 'Private personal information.' });
  const h = await harness(t, dataDir, [call({ action: 'remember', content: 'Child output.' }), text()]);
  const child = await h.ctx.agents.create({
    sessionId: SessionId(randomUUID()),
    meta: { origin: 'subagent', parentSession: h.agent.id },
    parentAgent: h.agent,
    agentOptions: { provider: 'scripted', model: 'offline' },
  });
  await send(child.agent, 'Do a coding task.');
  assert.doesNotMatch(allText(h.provider.requests[0]), /Private personal information/);
  assert.equal(store.all().length, 1);
  assert.match(JSON.stringify(child.agent.session.snapshotEvents()), /primary conversation/);
  await child.dispose();
});

test('daily reflections are readable through the native tool and writes omit unrelated user text', async (t) => {
  const dataDir = root(t);
  const h = await harness(t, dataDir, [
    call({ action: 'remember', kind: 'fact', content: 'Likes tea.' }), text(),
    call({ action: 'reflect', content: 'We discussed tea.' }), text(),
    call({ action: 'reflect' }), text(),
  ]);
  await send(h.agent, 'I like tea. This extra sentence should not be duplicated into a memory file.');
  const store = new MemoryStore(memoryRoot(dataDir, 'friend'));
  assert.equal(store.all()[0].source.quote, undefined);
  await send(h.agent, 'Summarize today.');
  await send(h.agent, 'Read our latest summary.');
  assert.match(JSON.stringify(h.agent.session.snapshotEvents()), /We discussed tea/);
  assert.match(store.reflection().content, /We discussed tea/);
});
