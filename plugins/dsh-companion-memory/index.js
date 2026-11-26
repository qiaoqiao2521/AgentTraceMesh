import { defineTool } from '@deepseek-ai/dsh-tools';
import z from '@deepseek-ai/schemastery';
import { MemoryStore, memoryRoot, KINDS, STATUSES } from './store.js';

export const name = 'companion-memory';
export const inject = ['tools', 'systemPrompt'];
export const Config = z.object({
  companion: z.string().default('companion'),
  dataDir: z.string().description('Optional local base directory; companion identity is appended.'),
  maxContextChars: z.number().min(512).default(6000),
  timeZone: z.string().default('Asia/Shanghai'),
});

const GUIDANCE = [
  'You have editable personal companion memory across sessions.',
  'Use companion_memory to remember stable personal facts, preferences, meaningful shared episodes, and promises.',
  'Save selectively during conversation; never archive every turn or tool output.',
  'Use the existing dialogue model. No extra extraction model or background worker is needed.',
  'Ground records in this conversation. Distinguish observations from uncertain feelings.',
  'For writes, supply a short exact evidence quote when useful; do not copy the whole conversation.',
  'Never invent shared experiences, treat roleplay as real biography, or store passwords and credentials.',
  'Before correcting or forgetting, search/review for the actual id. Update that record; do not append a contradictory copy.',
  'Do not recreate forgotten or corrected memories from older chat history without a new user instruction.',
  'Mark fulfilled promises resolved. A stored due date is context, not a scheduled reminder.',
  'When the user requests a recap, reflect saves one editable daily summary. It is not proof of inferred facts.',
  'Use reflect without content to read the latest daily summary; supply date to read another day.',
  'Honour requests to forget. Explain that native DSH chat history is separate if asked about complete erasure.',
  'Keep project decisions and code knowledge in project files; this tool is for the person and the relationship.',
].join('\n');

function primary(agent) {
  return !!agent && agent.session?.header?.origin !== 'subagent';
}

function userSource(agent, message) {
  return {
    sessionId: String(agent.id),
    messageId: String(message.id || ''),
    quote: message.content.filter((part) => part.type === 'text').map((part) => part.text).join('\n').slice(0, 1800),
  };
}

export function apply(ctx, config = {}) {
  const warn = (text) => console.warn('[companion-memory] ' + text);
  const store = new MemoryStore(memoryRoot(config.dataDir, config.companion), { warn, timeZone: config.timeZone });
  const sources = new WeakMap();

  const turns = new WeakMap();
  ctx.on('agent/pre-step', async ({ agent, signal, turn }, next) => {
    const decision = await next();
    if (!signal.aborted && decision.kind === 'enter' && primary(agent)) {
      if (turns.get(agent) !== turn) sources.delete(agent);
      turns.set(agent, turn);
      const user = decision.messages.filter((message) => message.source?.kind === 'user').at(-1);
      if (user) sources.set(agent, userSource(agent, user));
    }
    return decision;
  });

  ctx.systemPrompt.section({
    name: 'companion-memory:usage', order: 3200, interpolate: false,
    text: ({ agent }) => primary(agent) ? GUIDANCE : '',
  });
  ctx.systemPrompt.context({
    name: 'companion-memory:recall', order: 300, interpolate: false,
    text: ({ agent }) => {
      if (!primary(agent)) return '';
      try { return store.context(sources.get(agent)?.quote || '', config.maxContextChars ?? 6000); }
      catch (error) { warn('WARNING memory unavailable: ' + error.message); return ''; }
    },
  });

  ctx.tools.register(defineTool({
    name: 'companion_memory',
    description: 'Manage personal companion memory. Remember selectively; search, review, correct, forget, resolve promises, or save a daily reflection.',
    parameters: {
      action: { type: 'string', required: true, enum: ['remember', 'search', 'review', 'update', 'forget', 'resolve', 'reflect'] },
      id: { type: 'string', description: 'Existing id from search/review; required for update, forget, resolve.' },
      content: { type: 'string', description: 'Grounded personal memory, corrected content, or requested daily reflection.' },
      kind: { type: 'string', enum: KINDS },
      pinned: { type: 'boolean', description: 'Keep in core context. Personal facts/preferences/people default to true.' },
      importance: { type: 'number', description: 'Relative importance, clamped to 0..1.' },
      due: { type: 'string', description: 'Optional human-readable commitment due date; creates no timer.' },
      status: { type: 'string', enum: STATUSES },
      query: { type: 'string' },
      evidence: { type: 'string', description: 'Optional short exact quote from the latest direct user message.' },
      date: { type: 'string', description: 'Reflection read date, YYYY-MM-DD; omit for latest.' },
      limit: { type: 'integer', description: 'Result page size, clamped to 1..200.' },
      offset: { type: 'integer', description: 'Result page offset, defaults to zero.' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, result) => [{ type: 'text', text: JSON.stringify(result) }],
    },
    execute(args, exec) {
      exec.signal.throwIfAborted();
      if (!primary(exec.agent)) return { ok: false, warning: 'Personal memory requires a primary conversation.' };
      const accepted = sources.get(exec.agent);
      const source = accepted && { sessionId: accepted.sessionId, messageId: accepted.messageId };
      if (args.evidence && accepted) {
        if (accepted.quote.includes(args.evidence)) source.quote = args.evidence;
        else warn('WARNING evidence quote did not match the accepted user message; retaining message identity only.');
      }
      try {
        let result;
        if (args.action === 'reflect' && !args.content) {
          result = store.reflection(args.date);
        } else if (args.action === 'search' || args.action === 'review') {
          result = store.search(args.query, {
            limit: args.limit ?? (args.action === 'review' ? 50 : 8),
            offset: args.offset, activeOnly: args.action !== 'review',
          });
        } else {
          if (!source) return { ok: false, warning: 'No accepted direct user message is available as a source.' };
          switch (args.action) {
            case 'remember': result = store.remember(args, source); break;
            case 'update': result = store.update(args.id, args, source); break;
            case 'forget': result = store.forget(args.id); break;
            case 'resolve': result = store.update(args.id, { status: 'resolved' }, source); break;
            case 'reflect': result = store.reflect(args.content, source); break;
            default: return { ok: false, warning: 'Unknown memory action.' };
          }
        }
        return { ok: true, action: args.action, ...result };
      } catch (error) {
        warn('WARNING ' + args.action + ': ' + error.message);
        return { ok: false, action: args.action, warning: error.message };
      }
    },
  }));
}
