const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

// loadApp() replaces globalThis.process with a stub; keep the real one for
// exit-code signalling.
const nodeProcess = process;


class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...tokens) { tokens.forEach((t) => this.values.add(t)); }
  remove(...tokens) { tokens.forEach((t) => this.values.delete(t)); }
  toggle(token, force) {
    if (force === true) { this.values.add(token); return true; }
    if (force === false) { this.values.delete(token); return false; }
    if (this.values.has(token)) { this.values.delete(token); return false; }
    this.values.add(token); return true;
  }
  contains(token) { return this.values.has(token); }
}

class FakeElement {
  set innerHTML(value) { this._innerHTML = value; this.children = []; }
  get innerHTML() { return this._innerHTML || ""; }
  constructor(tagName = "div") {
    this.tagName = String(tagName || "div").toUpperCase();
    this.children = [];
    this.dataset = {};
    this.style = { setProperty(name, value) { this[name] = value; } };
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.eventListeners = new Map();
    this.textContent = "";
    this.innerHTML = "";
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.checked = true;
    this.parentNode = null;
    this.ownerDocument = null;
    this._rect = { top: 0, bottom: 0, width: 960, height: 640 };
  }
  appendChild(child) {
    if (!child) return child;
    if (child.tagName === '#FRAGMENT') {
      [...child.children].forEach(item => this.appendChild(item));
      child.children = [];
      return child;
    }
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...children) { this.children = []; children.forEach(child => this.appendChild(child)); }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }
  }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(type, handler) { this.eventListeners.set(type, handler); }
  removeEventListener(type) { this.eventListeners.delete(type); }
  focus() {}
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const match = selector.match(/^\.([\w-]+)(?:\[data-message-index="(\d+)"\])?$/);
    if (!match) return [];
    return descendants(this, el => ((el.className || '').split(' ').includes(match[1]) || el.classList?.contains(match[1]))
      && (match[2] === undefined || el.dataset.messageIndex === match[2]));
  }
  closest() { return null; }
  scrollIntoView() { this.scrolledIntoView = true; }
  scrollTo(value) {
    if (typeof value === "number") { this.scrollTop = value; return; }
    if (value && typeof value === "object" && Number.isFinite(value.top)) { this.scrollTop = value.top; }
  }
  getBoundingClientRect() { return this._rect; }
}

function createStorage() {
  const store = new Map();
  return {
    getItem(key) { return store.has(key) ? store.get(key) : null; },
    setItem(key, value) { store.set(key, String(value)); },
    removeItem(key) { store.delete(key); },
    clear() { store.clear(); },
  };
}

function createDocument() {
  const elements = new Map();
  const documentElement = new FakeElement("html");
  documentElement.dataset = {};
  const body = new FakeElement("body");
  const fakeWindow = {
    eventListeners: new Map(),
    addEventListener(type, callback) { this.eventListeners.set(type, callback); },
    removeEventListener() {},
    requestAnimationFrame(callback) { queueMicrotask(callback); return 1; },
    cancelAnimationFrame() {},
    setTimeout() { return 1; },
    clearTimeout() {},
    ResizeObserver: null,
    performance: { now: () => Date.now() },
    location: { search: "", pathname: "/", origin: "http://localhost" },
    history: {
      writes: [],
      pushState(_state, _title, url) { this.writes.push(["push", url]); fakeWindow.location.search = url.includes("?") ? url.slice(url.indexOf("?")) : ""; },
      replaceState(_state, _title, url) { this.writes.push(["replace", url]); fakeWindow.location.search = url.includes("?") ? url.slice(url.indexOf("?")) : ""; },
    },
  };
  documentElement.ownerDocument = { defaultView: fakeWindow };
  body.ownerDocument = { defaultView: fakeWindow };
  const roleInputs = ["user", "assistant", "system", "developer", "tool", "other"].map((role) => {
    const input = new FakeElement("input");
    input.dataset.role = role;
    input.checked = true;
    input.ownerDocument = { defaultView: fakeWindow };
    return input;
  });

  const document = {
    documentElement,
    body,
    defaultView: fakeWindow,
    getElementById(id) {
      if (!elements.has(id)) {
        const el = new FakeElement(["readerProject", "readerSource"].includes(id) ? "select" : "div");
        el.ownerDocument = document;
        elements.set(id, el);
      }
      return elements.get(id);
    },
    querySelectorAll(selector) {
      if (selector === ".roles input[type=checkbox]") return roleInputs;
      return [];
    },
    createElement(tagName) {
      const el = new FakeElement(tagName);
      el.ownerDocument = document;
      return el;
    },
    createDocumentFragment() {
      const el = new FakeElement("#fragment");
      el.ownerDocument = document;
      return el;
    },
    createTextNode(text) {
      return { nodeValue: String(text), textContent: String(text), parentNode: null };
    },
    createTreeWalker() { return { nextNode() { return null; } }; },
  };
  fakeWindow.document = document;
  return document;
}

async function loadApp({ fetchImpl, search = "", preferences = {}, legacy = false } = {}) {
  const repoDir = path.resolve(__dirname, "..");
  const sourcePath = path.join(repoDir, "static", "app.js");
  const localStorage = createStorage();
  const document = createDocument();
  if (legacy) document.getElementById('readerProject').tagName = 'DIV';
  document.defaultView.location = { pathname: "/history", search };
  document.defaultView.HVSettings = { getPreferences: () => preferences };
  // renderSessionHeader needs a working querySelector on the session header.
  const sessionTitleEl = new FakeElement("div");
  const sessionMetaEl = new FakeElement("div");
  const sessionHeaderEl = document.getElementById("sessionHeader");
  sessionHeaderEl.querySelector = (selector) => {
    if (selector === ".session-title") return sessionTitleEl;
    if (selector === ".session-meta") return sessionMetaEl;
    return null;
  };
  const defaultFetch = async () => ({
    ok: true,
    json: async () => ({ items: [], next_cursor: null, pagination_status: 'available', next_offset: 0, matches: [] }),
  });
  // Node 22 exposes a getter-only navigator; replace it for this DOM fixture.
  Object.defineProperty(globalThis, "navigator", { value: {}, writable: true, configurable: true });
  Object.assign(globalThis, {
    __CCHV_TEST__: true,
    __testApi: undefined,
    document,
    localStorage,
    navigator: { clipboard: { writeText: async () => {} } },
    window: document.defaultView,
    fetch: (...args) => (fetchImpl ? fetchImpl(...args) : defaultFetch()),
    requestAnimationFrame(callback) { queueMicrotask(callback); return 1; },
    cancelAnimationFrame() {},
    setTimeout() { return 1; },
    clearTimeout() {},
    alert() {},
    URLSearchParams,
    Element: FakeElement,
    NodeFilter: { SHOW_TEXT: 4 },
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    process: { env: { NODE_ENV: "test" } },
  });

  const href = `${pathToFileURL(sourcePath).href}?test=${Date.now()}-${Math.random()}`;
  await import(href);
  await flushAsync();
  return { api: globalThis.__testApi, localStorage, document, window: document.defaultView };
}

function flushAsync() {
  return new Promise((resolve) => setImmediate(resolve));
}

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}
function row(source, id = 'same-id', project = '/work/project', updated = 200) {
  return { system: 'linux', source, id, project, title: `${source} conversation`, updated_at: updated, source_revision: `rev-${source}` };
}
function fixture({ sessions, projects, intercept } = {}) {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    const parsed = new URL(url, 'http://localhost');
    if (intercept) {
      const value = intercept(parsed);
      if (value !== undefined) return value;
    }
    if (parsed.pathname === '/api/sources') return response({ runtime_system: 'linux', sources: ['codex', 'claude', 'codebuddy'].map(source => ({ system: 'linux', source, read_only: true })) });
    if (parsed.pathname === '/api/reuse/projects') return response({ items: projects || [{ project: '/work/project', session_count: 2 }], next_cursor: null, pagination_status: 'available' });
    if (parsed.pathname === '/api/reuse/sessions') return response({ items: sessions || [row('codex'), row('codebuddy')], next_cursor: null, pagination_status: 'available' });
    if (parsed.pathname.endsWith('/audit')) return response({}, 404);
    if (parsed.pathname.endsWith('/messages')) return response({ messages: [], total: 0, offset: 0 });
    const match = parsed.pathname.match(/^\/api\/([^/]+)\/([^/]+)\/session\/([^/]+)$/);
    if (match) return response({ session: { id: match[3], cwd: '/work/project', title: `${match[2]} conversation`, message_count: 0 } });
    return response({ items: [] });
  };
  return { calls, fetchImpl };
}

async function run() {
  const basic = fixture();
  let app = await loadApp(basic);
  assert.equal(app.api.getReaderState().project, null);
  assert.equal(basic.calls.some(url => /\/sessions\?/.test(url)), false, 'fresh reader waits for project selection');
  assert.match(app.document.getElementById('messages').children.at(-1).textContent, /选择项目/);
  await app.api.chooseReaderProject('/work/project');
  assert.equal(app.api.getCurrentSession().title, 'codex conversation', 'latest project session opens automatically');
  assert.equal(new URLSearchParams(app.window.location.search).get('project'), '/work/project');
  assert.equal(basic.calls.filter(url => url.startsWith('/api/reuse/sessions?')).length, 1);
  assert.equal(app.api.getReaderState().items.length, 2, 'same session ID from different providers is retained');
  await app.api.openReaderSession(row('codebuddy'));
  assert.equal(app.api.getCurrentSession().title, 'codebuddy conversation');
  assert.equal(new URLSearchParams(app.window.location.search).get('source'), 'codebuddy');
  assert.match(app.api.getReaderState().activeKey, /codebuddy/);
  assert.equal(basic.calls.some(url => /^\/api\/linux\/[^/]+\/sessions/.test(url)), false, 'project reader never calls old global list');

  const exactProject = '/work/项目 with "quotes"';
  const encoded = fixture({ sessions: [] });
  app = await loadApp(encoded);
  await app.api.chooseReaderProject(exactProject, { source: 'codebuddy' });
  const encodedRequest = new URL(encoded.calls.find(url => url.startsWith('/api/reuse/sessions?')), 'http://localhost');
  assert.equal(encodedRequest.searchParams.get('project'), exactProject);
  assert.equal(encodedRequest.searchParams.get('source'), 'codebuddy');
  await app.api.chooseReaderProject('');
  assert.equal(app.api.getReaderState().project, '', 'unbound project is a real group, not the choose placeholder');
  assert.equal(app.api.getReaderState().source, '', 'changing project resets source filter');
  assert.equal(new URLSearchParams(app.window.location.search).get('project'), '');

  const restore = fixture();
  app = await loadApp({ ...restore, search: '?project=%2Fwork%2Fproject' });
  await flushAsync();
  assert.equal(app.api.getCurrentSession().title, 'codex conversation');
  assert.equal(app.window.history.writes.some(([method]) => method === 'push'), false, 'bootstrap replaces rather than creates navigation history');
  app.window.location.search = '?project=';
  await app.window.eventListeners.get('popstate')();
  assert.equal(app.api.getReaderState().project, '');

  const deep = fixture();
  app = await loadApp({ ...deep, search: '?system=linux&source=codebuddy&session=same-id&source_revision=deep-rev&message=0&q=needle' });
  await flushAsync();
  assert.equal(app.api.getReaderState().project, '/work/project', 'legacy deep link associates the conversation with cwd');
  assert.equal(app.api.getCurrentSession().title, 'codebuddy conversation');
  assert.equal(deep.calls.some(url => url.includes('/codebuddy/session/same-id?source_revision=deep-rev')), true);
  assert.equal(new URLSearchParams(app.window.location.search).get('q'), 'needle', 'original evidence context survives initial deep link');
  await app.api.openReaderSession(row('codex'));
  assert.equal(new URLSearchParams(app.window.location.search).has('q'), false);
  assert.equal(new URLSearchParams(app.window.location.search).has('message'), false);

  let resolveSlow;
  const race = fixture({ intercept(url) {
    if (url.pathname === '/api/reuse/sessions' && url.searchParams.get('project') === '/slow') {
      return new Promise(resolve => { resolveSlow = resolve; });
    }
    if (url.pathname === '/api/reuse/sessions') return response({ items: [row('codebuddy', 'fast', '/fast')], next_cursor: null, pagination_status: 'available' });
  } });
  app = await loadApp(race);
  const slow = app.api.chooseReaderProject('/slow');
  await app.api.chooseReaderProject('/fast');
  resolveSlow(response({ items: [row('codex', 'stale', '/slow')], next_cursor: null, pagination_status: 'available' }));
  await slow;
  assert.equal(app.api.getReaderState().project, '/fast');
  assert.equal(app.api.getCurrentSession().id, 'fast');
  assert.equal(app.api.getReaderState().items[0].id, 'fast');

  let resolveOldSession;
  const sessionRace = fixture({ intercept(url) {
    if (url.pathname === '/api/linux/codex/session/old') return new Promise(resolve => { resolveOldSession = resolve; });
  } });
  app = await loadApp(sessionRace);
  const old = app.api.openReaderSession(row('codex', 'old'));
  await app.api.openReaderSession(row('codebuddy', 'new'));
  resolveOldSession(response({}, 409));
  await old;
  assert.equal(app.api.getCurrentSession().id, 'new', 'stale HTTP error must not overwrite a newer conversation');
  assert.equal(app.document.getElementById('messages').children.some(item => /索引已变化/.test(item.textContent)), false);

  let page = 0;
  const paging = fixture({ intercept(url) {
    if (url.pathname === '/api/reuse/sessions') {
      ++page;
      if (page === 1) return response({ items: [row('codex')], next_cursor: 'bound-cursor', pagination_status: 'available' });
      assert.equal(url.searchParams.get('cursor'), 'bound-cursor');
      return response({ items: [row('codebuddy')], next_cursor: null, pagination_status: 'available' });
    }
  } });
  app = await loadApp(paging);
  await app.api.chooseReaderProject('/work/project');
  await app.api.fetchReaderSessions({ append: true });
  assert.equal(app.api.getReaderState().items.length, 2);
  assert.equal(app.api.getReaderState().cursor, '');

  app = await loadApp({ ...fixture(), preferences: { fontSize: 17, toolsCollapsed: false, auditExpanded: true } });
  await app.api.chooseReaderProject('/work/project');
  assert.equal(app.api.getToolsCollapsedByDefault(), false, 'tool setting survives opening a conversation');
  assert.equal(app.document.getElementById('sessionReview').open, true);
  assert.equal(app.document.getElementById('messages').style['--reader-font-size'], '17px');
  app.api.setConversationFind(true);
  assert.equal(app.document.getElementById('conversationFind').hidden, false);
  app.api.setConversationFind(false);
  assert.equal(app.document.getElementById('conversationFind').hidden, true);
  assert.equal(app.document.body.classList.contains('reader-has-session'), true);
  app.document.getElementById('readerToggleList').eventListeners.get('click')();
  assert.equal(app.document.body.classList.contains('reader-show-list'), true);
  assert.equal(app.document.getElementById('readerToggleList').getAttribute('aria-expanded'), 'true');
  await app.api.openReaderSession(row('codebuddy'));
  assert.equal(app.document.body.classList.contains('reader-show-list'), false, 'mobile list closes after conversation selection');

  let projectPages = 0;
  const manyProjects = fixture({ intercept(url) {
    if (url.pathname !== '/api/reuse/projects') return undefined;
    assert.equal(url.searchParams.get('limit'), '20', 'cross-source project API caps each page at 20');
    projectPages++;
    if (projectPages === 1) return response({ items: Array.from({length: 20}, (_, n) => ({project: `/project-${n}`})), next_cursor: 'project-cursor', pagination_status: 'available' });
    assert.equal(url.searchParams.get('cursor'), 'project-cursor');
    return response({ items: [{project: '/project-20'}], next_cursor: null, pagination_status: 'available' });
  } });
  app = await loadApp(manyProjects);
  assert.equal(app.document.getElementById('readerProjectsMore').hidden, false);
  await app.api.loadReaderProjects({append: true});
  assert.deepEqual(app.api.getReaderState().projects.map(row => row.project), Array.from({length: 21}, (_, n) => `/project-${n}`));
  assert.equal(app.document.getElementById('readerProjectsMore').hidden, true);

  let failPage = true;
  const stalePage = fixture({ intercept(url) {
    if (url.pathname !== '/api/reuse/sessions') return undefined;
    if (url.searchParams.has('cursor') && failPage) return response({}, 409);
    return response({items: [row('codex')], next_cursor: 'old-cursor', pagination_status: 'available'});
  } });
  app = await loadApp(stalePage);
  await app.api.chooseReaderProject('/work/project');
  await app.api.fetchReaderSessions({append: true});
  assert.equal(app.api.getReaderState().items.length, 1, 'expired pagination leaves previous rows visible');
  assert.match(app.document.getElementById('readerListStatus').textContent, /索引已变化/);
  failPage = false;
  await app.document.getElementById('readerListStatus').children.at(-1).eventListeners.get('click')();
  assert.equal(app.api.getReaderState().items.length, 1, 'refresh restarts pagination instead of duplicating old rows');
  const failedSource = fixture({ intercept(url) {
    if (url.pathname === '/api/reuse/projects') return response({items: [{project: '/partial'}], next_cursor: 'must-not-follow', pagination_status: 'restart_after_source_error', partial: true, errors: [{source: 'codex', error: 'unavailable'}]});
    if (url.pathname === '/api/reuse/sessions') return response({items: [row('codex')], next_cursor: 'must-not-follow', pagination_status: 'restart_after_source_error', partial: true, errors: [{source: 'codex', error: 'unavailable'}]});
  } });
  app = await loadApp(failedSource);
  assert.equal(app.document.getElementById('readerProjectsMore').hidden, true, 'source error blocks project pagination even if response carries a cursor');
  const beforeProjects = failedSource.calls.length;
  await app.api.loadReaderProjects({append: true});
  assert.equal(failedSource.calls.length, beforeProjects);
  await app.api.chooseReaderProject('/partial');
  assert.equal(app.api.getReaderState().cursor, '', 'source error blocks session pagination');
  const beforeSessions = failedSource.calls.length;
  await app.api.fetchReaderSessions({append: true});
  assert.equal(failedSource.calls.length, beforeSessions);
  const limitedContent = fixture({ intercept(url) {
    const warning = {partial: true, errors: [], content_warnings: [{source: 'agy', status: 'partial'}], pagination_status: 'available'};
    if (url.pathname === '/api/reuse/projects') return response({...warning, items: [{project: '/decoded'}], next_cursor: 'project-more'});
    if (url.pathname === '/api/reuse/sessions') return response({...warning, items: [row('codex')], next_cursor: 'session-more'});
  } });
  app = await loadApp(limitedContent);
  assert.match(app.document.getElementById('readerListStatus').textContent, /部分历史正文未完整解码/);
  assert.equal(app.document.getElementById('readerProjectsMore').hidden, false, 'coverage warning retains project pagination');
  await app.api.chooseReaderProject('/decoded');
  assert.match(app.document.getElementById('readerListStatus').textContent, /部分历史正文未完整解码/);
  assert.doesNotMatch(app.document.getElementById('readerListStatus').textContent, /来源暂不可用/);
  assert.equal(app.api.getReaderState().cursor, 'session-more', 'coverage warning retains session pagination');
  const insightNavigation = fixture({ intercept(url) {
    const sessionId = url.pathname.split('/').at(-1);
    if (['usage-other', 'briefing-other'].includes(sessionId)) {
      return response({session: {id: sessionId, cwd: `/work/${sessionId}`, title: sessionId, message_count: 0}});
    }
    if (url.pathname === '/api/reuse/sessions' && url.searchParams.get('project') !== '/work/project') {
      const project = url.searchParams.get('project');
      return response({items: [row('codex', project.split('/').at(-1), project)], next_cursor: null, pagination_status: 'available'});
    }
  } });
  app = await loadApp(insightNavigation);
  await app.api.chooseReaderProject('/work/project');
  for (const [panel, sessionId] of [['usageContent', 'usage-other'], ['briefingContent', 'briefing-other']]) {
    const clicked = new FakeElement('div');
    clicked.dataset.usageSession = sessionId;
    clicked.closest = selector => selector === '[data-usage-session]' ? clicked : null;
    const pushCount = app.window.history.writes.filter(([method]) => method === 'push').length;
    await app.document.getElementById(panel).eventListeners.get('click')({target: clicked});
    assert.equal(app.api.getCurrentSession().id, sessionId);
    assert.equal(app.api.getReaderState().project, `/work/${sessionId}`, `${panel} infers target project from actual session cwd`);
    assert.equal(app.api.getReaderState().items[0].id, sessionId);
    assert.equal(app.api.getReaderState().activeKey, app.api.readerSessionKey(row('codex', sessionId)));
    const target = new URLSearchParams(app.window.location.search);
    assert.equal(target.get('session'), sessionId);
    assert.equal(target.get('project'), `/work/${sessionId}`);
    assert.equal(target.get('source'), 'codex');
    assert.equal(target.get('system'), 'linux');
    assert.equal(target.has('message'), false);
    assert.equal(app.window.history.writes.filter(([method]) => method === 'push').length, pushCount + 1);
    await app.api.restoreReaderLocation();
    assert.equal(app.api.getCurrentSession().id, sessionId, 'refresh resolves the conversation opened from insight panel');
  }
  console.log('Project reader navigation: 14 scenarios passed');
}
function descendants(element, predicate) {
  return (element.children || []).flatMap(child => [ ...(predicate(child) ? [child] : []), ...descendants(child, predicate) ]);
}

async function runConversationAids() {
  const first = {...row('codex', 'same'), store_id: 'codex-store', related_group: {id: 'g', label: '修复登录刷新失败', reason: '同项目且标题相同', total: 3}};
  const second = {...row('claude', 'same'), store_id: 'claude-store', related_group: first.related_group};
  const third = {...row('codex', 'same'), store_id: 'another-store', related_group: first.related_group};
  let page = 0;
  const grouped = fixture({intercept(url) {
    if (url.pathname !== '/api/reuse/sessions') return;
    return response({items: ++page === 1 ? [first, second] : [third], next_cursor: page === 1 ? 'next' : null, pagination_status: 'available'});
  }});
  let app = await loadApp(grouped);
  await app.api.chooseReaderProject('/work/project');
  const list = app.document.getElementById('sessionList');
  assert.equal(list.children.length, 1, 'related rows share one expandable group');
  assert.equal(list.children[0].open, true, 'active conversation remains visible in its group');
  assert.match(list.children[0].children[0].textContent, /2 \/ 3/);
  assert.equal(descendants(list, el => el.dataset.sessionId).length, 2);
  assert.equal(grouped.calls.some(url => url.includes('key-messages')), false, 'markers do not load until requested');
  await app.api.fetchReaderSessions({append: true});
  assert.equal(app.api.getReaderState().items.length, 3, 'same source ID in another store is retained');
  assert.equal(list.children.length, 1, 'append joins existing group');
  assert.match(list.children[0].children[0].textContent, /3 \/ 3/);
  assert.equal(new Set(descendants(list, el => el.dataset.sessionId).map(el => JSON.stringify([el.dataset.source, el.dataset.storeId, el.dataset.sessionId]))).size, 3);
  app.api.setReaderGrouping(false);
  assert.equal(descendants(list, el => el.tagName === 'DETAILS').length, 0);
  assert.equal(descendants(list, el => el.dataset.sessionId).length, 3, 'ungrouping loses no sessions');
  assert.equal(app.document.getElementById('readerGroupRelated').getAttribute('aria-pressed'), 'false');
  app.api.renderSessions([{...first, pinned: true}, second]);
  assert.equal(descendants(list, el => el.dataset.sessionId).length, 2, 'ungrouped pinned rows still render');
  assert.equal(descendants(list, el => el.classList.contains('pinned')).length, 1);
  const legacy = await loadApp({legacy: true});
  legacy.api.renderSessions([{...first, pinned: true}, second]);
  const legacyList = legacy.document.getElementById('sessionList');
  assert.equal(descendants(legacyList, el => el.dataset.sessionId).length, 2, 'legacy pinned rows still render');
  assert.equal(descendants(legacyList, el => el.classList.contains('pinned')).length, 1);

  const payload = (selected, extra = {}) => ({system: selected.system, source: selected.source, store_id: selected.store_id,
    id: selected.id, source_revision: selected.source_revision, status: 'available', total_messages: 2107,
    partial: false, truncated: false, items: [{message_index: 2106, role: 'assistant', kinds: ['decision', 'verification'], text: '<not markup> 最后一次复测', truncated: false}], ...extra});
  let markerResult = payload(first);
  let resolveMarker;
  let delayed = false;
  const marked = fixture({sessions: [first, second], intercept(url) {
    if (url.pathname !== '/api/reuse/key-messages') return;
    assert.equal(url.searchParams.get('source_revision'), 'rev-codex');
    assert.equal(url.searchParams.get('store_id'), 'codex-store');
    if (delayed) return new Promise(resolve => {resolveMarker = resolve;});
    return response(markerResult);
  }});
  app = await loadApp(marked);
  await app.api.chooseReaderProject('/work/project');
  await app.api.toggleKeyMessages();
  const panel = app.document.getElementById('keyMessagesPanel');
  const markers = app.document.getElementById('keyMessagesList');
  const status = app.document.getElementById('keyMessagesStatus');
  assert.equal(panel.hidden, false);
  assert.equal(markers.children.length, 1);
  const link = markers.children[0].children[0];
  const url = new URL(link.href, 'http://localhost');
  assert.equal(url.searchParams.get('message'), '2106', 'absolute index is not renumbered');
  assert.equal(url.searchParams.get('source_revision'), 'rev-codex');
  assert.equal(url.searchParams.get('store_id'), 'codex-store');
  assert.match(link.textContent, /方案线索 \/ 提及验证/);
  assert.equal(markers.children[0].children[1].textContent, '<not markup> 最后一次复测');
  markerResult = payload(first, {partial: true, truncated: true});
  await app.api.fetchKeyMessages();
  assert.match(status.textContent, /尚非完整范围/);
  markerResult = payload(first, {status: 'unsupported', items: []});
  await app.api.fetchKeyMessages();
  assert.match(status.textContent, /暂不支持/);
  assert.equal(markers.children.length, 0, 'unsupported clears previous nodes');
  markerResult = payload(first, {store_id: 'wrong-store'});
  await app.api.fetchKeyMessages();
  assert.match(status.textContent, /旧线索已清除/);
  assert.equal(markers.children.length, 0);
  delayed = true;
  const pending = app.api.fetchKeyMessages();
  await app.api.openReaderSession(second);
  resolveMarker(response(payload(first)));
  await pending;
  assert.equal(panel.hidden, true, 'session change closes old panel');
  assert.equal(markers.children.length, 0, 'late response cannot leak across sources with same session ID');
  assert.equal(status.textContent, '');

  app = await loadApp(fixture({sessions: [first], intercept(url) {
    if (url.pathname === '/api/reuse/key-messages') return response({error: 'index_revision_changed'}, 409);
  }}));
  await app.api.chooseReaderProject('/work/project');
  await app.api.toggleKeyMessages();
  assert.match(app.document.getElementById('keyMessagesStatus').textContent, /索引已变化/);
  assert.equal(app.document.getElementById('keyMessagesList').children.length, 0);
  await app.api.openReaderSession({...first, source_revision: ''});
  await app.api.toggleKeyMessages();
  assert.match(app.document.getElementById('keyMessagesStatus').textContent, /没有索引修订/);
  await app.api.chooseReaderProject(null);
  assert.equal(app.document.getElementById('toggleKeyMessages').disabled, true);
  assert.equal(app.document.getElementById('keyMessagesPanel').hidden, true);

  const toolDeepLink = fixture({sessions: [first], intercept(url) {
    if (url.pathname === '/api/linux/codex/session/same') return response({session: {id: 'same', cwd: '/work/project', title: 'Failure', message_total: 2}});
    if (url.pathname.endsWith('/messages')) return response({messages: [
      {message_index: 0, role: 'user', kind: 'message', text: 'request'},
      {message_index: 1, role: 'tool', kind: 'tool_result', text: 'Error: LoginExpired', ts_ms: 1},
    ], offset: 0, total: 2});
  }});
  app = await loadApp({...toolDeepLink, search: '?system=linux&source=codex&session=same&message=1&source_revision=rev-codex&store_id=codex-store&project=%2Fwork%2Fproject'});
  const tool = app.document.getElementById('messages').querySelector('.msg[data-message-index="1"]');
  assert.ok(tool, 'deep link renders its target');
  assert.equal(tool.classList.contains('tool-expanded'), true, 'navigation must rerender the expanded tool, not only change its in-memory flag');
  assert.equal(tool.querySelector('.msg-body').hidden, false);
  assert.match(tool.querySelector('.msg-body').innerHTML, /LoginExpired/);
  assert.equal(tool.scrolledIntoView, true, 'scroll uses the replacement, expanded DOM node');
  assert.match(app.document.getElementById('historyTarget').textContent, /已定位消息 1/);
  console.log('Conversation reading aids: grouping, identities, links, disclosure and races passed');
}
async function runLongConversationRegressions() {
  const selected = {...row('codex', 'long'), store_id: 'codex-store'};
  const browseMessages = [
    {message_index: 2100, role: 'user', kind: 'message', text: 'current request'},
    {message_index: 2101, role: 'tool', kind: 'tool_result', text: 'Exit code: 0\nlatest output'},
    {message_index: 2102, role: 'assistant', kind: 'message', text: 'short server preview', is_truncated: true, char_count: 9000},
  ];
  const makeFixture = (intercept) => fixture({sessions: [selected], intercept(url) {
    if (intercept) {
      const result = intercept(url);
      if (result !== undefined) return result;
    }
    if (url.pathname === '/api/linux/codex/session/long') return response({session: {id: 'long', cwd: '/work/project', title: 'Long recording', message_total: 2103}});
    if (url.pathname === '/api/linux/codex/session/long/messages') return response({messages: browseMessages, offset: 2100, total: 2103});
  }});
  const runSearch = async (app, term = 'needle') => {
    app.document.getElementById('sessionSearch').value = term;
    await app.api.refreshSessionSearch();
  };
  const failures = [];
  const check = async (label, fn) => {
    try { await fn(); }
    catch (err) { failures.push(`${label}: ${err.message}`); }
  };

  await check('absolute tool timeline', async () => {
    const app = await loadApp(makeFixture());
    await app.api.chooseReaderProject('/work/project');
    const timeline = app.api.getToolTimelineElement();
    assert.match(timeline.innerHTML, /data-message-index="2101"/, 'tool timeline must keep the original message index');
    assert.match(timeline.innerHTML, /top:50%/, 'visual position remains relative to the loaded window');
    const dot = new FakeElement('div');
    dot.dataset.messageIndex = '2101';
    dot.closest = selector => selector === '[data-message-index]' ? dot : null;
    timeline.eventListeners.get('click')({target: dot});
    await flushAsync();
    const target = app.document.getElementById('messages').querySelector('.msg[data-message-index="2101"]');
    assert.equal(target.scrolledIntoView, true);
    assert.equal(target.classList.contains('tool-expanded'), true);
  });

  await check('nonzero-window full message', async () => {
    const fullText = 'full server message '.repeat(500);
    const source = makeFixture(url => {
      if (!url.pathname.endsWith('/message/2102')) return;
      assert.equal(url.searchParams.get('source_revision'), 'rev-codex');
      return response({message: {...browseMessages[2], text: fullText, char_count: fullText.length}});
    });
    const app = await loadApp(source);
    await app.api.chooseReaderProject('/work/project');
    const toggle = new FakeElement('button');
    toggle.dataset.messageToggle = '2102';
    toggle.closest = selector => selector === '[data-message-toggle]' ? toggle : null;
    await app.document.getElementById('messages').eventListeners.get('click')({target: toggle});
    assert.equal(source.calls.some(url => url.includes('/message/2102?')), true, 'expansion must fetch the original message, not index into the small window');
    const message = app.document.getElementById('messages').querySelector('.msg[data-message-index="2102"]');
    assert.match(message.querySelector('.msg-body').innerHTML, /full server message/);
  });

  await check('Clear restores browse window', async () => {
    const app = await loadApp(makeFixture(url => {
      if (url.pathname.endsWith('/messages/search')) return response({matches: [{message_index: 7, role: 'assistant', kind: 'message', excerpt_text: 'old needle result'}], match_count: 1, message_match_count: 1});
    }));
    await app.api.chooseReaderProject('/work/project');
    await runSearch(app);
    assert.ok(app.document.getElementById('messages').querySelector('.msg[data-message-index="7"]'));
    app.document.getElementById('clearSessionSearch').eventListeners.get('click')();
    assert.equal(app.document.getElementById('sessionSearch').value, '');
    const indexes = app.document.getElementById('messages').querySelectorAll('.msg').map(el => el.dataset.messageIndex);
    assert.deepEqual(indexes, ['2100', '2101', '2102'], 'Clear must restore the retained browse window');
  });

  await check('revision-bound search and stale response', async () => {
    let expired = false;
    let delayed = false;
    let resolveSearch;
    const source = makeFixture(url => {
      if (!url.pathname.endsWith('/messages/search')) return;
      if (delayed) return new Promise(resolve => { resolveSearch = resolve; });
      return expired ? response({error: 'index_revision_changed'}, 409)
        : response({matches: [{message_index: 7, role: 'assistant', kind: 'message', excerpt_text: 'old needle result'}], match_count: 1, message_match_count: 1});
    });
    const app = await loadApp(source);
    await app.api.chooseReaderProject('/work/project');
    await runSearch(app);
    const searchUrl = new URL(source.calls.find(url => url.includes('/messages/search?')), 'http://localhost');
    assert.equal(searchUrl.searchParams.get('source_revision'), 'rev-codex', 'search must use the same revision as browsing');
    assert.ok(app.document.getElementById('messages').querySelector('.msg[data-message-index="7"]'));
    expired = true;
    await runSearch(app);
    assert.equal(app.document.getElementById('messages').querySelector('.msg[data-message-index="7"]'), null, 'a stale search must remove the old result');
    assert.match(app.document.getElementById('sessionSearchCount').textContent, /索引已变化.*重新打开会话/, 'revision conflict must prompt a reload instead of reporting zero matches');
    const assertStale = () => {
      assert.equal(app.document.getElementById('messages').querySelectorAll('.msg').filter(el => el.dataset.messageIndex !== undefined).length, 0, 'expired browse messages must not return');
      assert.match(app.document.getElementById('messages').children.at(-1).innerHTML, /索引已变化.*重新打开会话/);
      assert.doesNotMatch(app.document.getElementById('messages').children.at(-1).textContent, /No messages/);
    };
    await app.document.getElementById('clearSessionSearch').eventListeners.get('click')();
    assertStale();
    await runSearch(app, '');
    assertStale();
    app.document.getElementById('clearRenderCache').eventListeners.get('click')();
    assertStale();
    const searchesBeforeReload = source.calls.filter(url => url.includes('/messages/search?')).length;
    await runSearch(app, 'new query');
    assertStale();
    assert.equal(source.calls.filter(url => url.includes('/messages/search?')).length, searchesBeforeReload, 'expired revision is not retried until the conversation is reopened');
    expired = false;
    await app.api.openReaderSession({...selected, source_revision: 'rev-new'});
    assert.ok(app.document.getElementById('messages').querySelector('.msg[data-message-index="2100"]'), 'reopening loads a usable browse window');
    assert.doesNotMatch(app.document.getElementById('sessionSearchCount').textContent, /索引已变化/);
    await runSearch(app);
    assert.ok(app.document.getElementById('messages').querySelector('.msg[data-message-index="7"]'), 'reopening also restores searching');
    const newSearch = new URL(source.calls.filter(url => url.includes('/messages/search?')).at(-1), 'http://localhost');
    assert.equal(newSearch.searchParams.get('source_revision'), 'rev-new');
    delayed = true;
    const pending = runSearch(app);
    await app.api.openReaderSession(row('claude', 'new'));
    resolveSearch(response({error: 'index_revision_changed'}, 409));
    await pending;
    assert.equal(app.api.getCurrentSession().id, 'new');
    assert.equal(app.document.getElementById('sessionSearchCount').textContent, '', 'late revision failures must not overwrite the newly selected conversation');
  });
  assert.deepEqual(failures, [], failures.join('\n'));
  console.log('Long conversation regressions: absolute navigation, expansion, Clear and revision-bound search passed');
}

async function runRecordingAliasResumeRegressions() {
  const cases = [
    {session: {...row('codex', 'native@recording-0123456789abcdef')}, conflict: true},
    {session: {...row('claude', 'native@recording-fedcba9876543210')}, conflict: true},
    {session: {...row('codex', 'marked-conflict'), activity_metadata: {identity_conflict: true, native_session_id: 'native'}}, conflict: true},
    {session: {...row('claude', 'top-level-conflict'), identity_conflict: true}, conflict: true},
    {session: row('codex', 'ordinary-native-id'), conflict: false},
    {session: row('claude', 'ordinary@recording-note'), conflict: false},
  ];
  const source = fixture({sessions: [cases[0].session], intercept(url) {
    const match = url.pathname.match(/^\/api\/linux\/(codex|claude)\/session\/([^/]+)$/);
    if (!match) return;
    const found = cases.find(item => item.session.source === match[1] && item.session.id === decodeURIComponent(match[2]));
    if (found) return response({session: {...found.session, cwd: '/work/project', message_count: 0}});
  }});
  const app = await loadApp(source);
  for (const {session, conflict} of cases) {
    await app.api.openReaderSession(session);
    const meta = app.document.getElementById('sessionHeader').querySelector('.session-meta').textContent;
    for (const id of ['copyResumeId', 'copyResumeCmdPs', 'copyResumeCmdWsl']) {
      assert.equal(app.document.getElementById(id).disabled, conflict, `${session.id}: resume copy controls follow recording identity`);
    }
    if (conflict) {
      assert.equal(app.document.getElementById('resumeValue').textContent, '-');
      assert.equal(app.document.getElementById('resumeCmdPs').textContent, '-');
      assert.equal(app.document.getElementById('resumeCmdWsl').textContent, '-');
      assert.match(meta, /冲突录制.*无法精确原生续接/);
    } else {
      assert.equal(app.document.getElementById('resumeValue').textContent, session.id);
      assert.match(app.document.getElementById('resumeCmdPs').textContent, session.source === 'codex' ? /codex resume/ : /claude -r/);
      assert.doesNotMatch(meta, /冲突录制/);
    }
  }
  console.log('Conflicting recording resume: aliases and metadata blocked, ordinary native IDs preserved');
}

run().then(runConversationAids).then(runLongConversationRegressions).then(runRecordingAliasResumeRegressions).catch(err => { console.error(err); nodeProcess.exitCode = 1; });
