const $ = id => document.getElementById(id);
const listOf = value => Array.isArray(value) ? value : [];
const state = {
  view: 'projects', project: null, query: '', selected: new Map(), selectionVersion: 0, selectionPurpose: 'handoff',
  preview: null, diagnostic: null, diagnosticShown: false, healthSequence: 0, selectionSequence: 0,
  pages: Object.fromEntries(['search', 'projects', 'timeline', 'cases'].map(key => [key, {sequence: 0, items: [], cursor: null, params: {}}])),
  caseId: '', casePreview: null, caseSequence: 0,
};
const workspaceViews = ['projects', 'search', 'cases'];
function text(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content == null ? '' : String(content);
  if (className) node.className = className;
  return node;
}
function button(label, action, className = 'btn small') {
  const node = text('button', label, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function dateLabel(value) {
  if (value == null) return '时间未知';
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : '时间未知';
}
function isStale(item, now = Date.now()) {
  const stamp = new Date(item.updated_at).getTime();
  return item.updated_at != null && Number.isFinite(stamp) && now - stamp > 7 * 86400000;
}
function historyLink(item, label, messageIndex, query = '') {
  const link = text('a', label);
  const params = new URLSearchParams({system: item.system || 'linux', source: item.source || '', session: item.id || item.session_id || ''});
  const project = item.project ?? state.project;
  if (project != null) params.set('project', String(project));
  if (Number.isInteger(messageIndex) && messageIndex >= 0) {
    params.set('message', String(messageIndex));
    if (item.source_revision) params.set('source_revision', item.source_revision);
  }
  if (query) params.set('q', query);
  link.href = `/history?${params.toString()}`;
  return link;
}
async function request(path, options) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error(`HTTP ${response.status || 'response'}`); }
  if (!response.ok) throw new Error(data.error || data.code || `HTTP ${response.status}`);
  return data;
}
function errorAdvice(error, fallback = '请检查来源状态后重试。') {
  const code = String(error?.message || error || 'unknown');
  if (/review_requirement_required/.test(code)) return '请先搜索原始需求，展开“查看并选择证据”，勾选至少一条用户完整消息；审计摘要和助手回复不能替代需求。';
  if (/index_revision_changed|index_revision_required|cursor.*(?:expired|invalid|changed)|invalid_cursor/.test(code)) return '索引已变化，请重新搜索或重新选择项目，从第一页读取。';
  if (/source_changed|context_revision|revision_mismatch|stale_selection|selection_stale/.test(code)) return '来源内容已变化，请重新搜索并重新选择证据。';
  if (/selection_body_limit_exceeded/.test(code)) return '所选完整消息超出 8000 字符导出上限，未截断导出。请减少选择或改选审计摘要。';
  if (/selection_message_outside_verified_scope/.test(code)) return '完整消息不在已核对范围内，请改选可核对的审计摘要。';
  if (/not_found|source_missing/.test(code)) return '请检查已配置的来源路径；更改启动配置后重新启动应用。';
  if (/unreadable|permission|access_denied/.test(code)) return '请检查当前用户是否有来源读取权限。';
  if (/unsupported/.test(code)) return '当前来源暂不支持此操作，可打开完整会话核对。';
  if (/indexing|busy/.test(code)) return '来源正在建立索引，请等待后重新检查。';
  return fallback;
}
function evidenceFailed(item) {
  return !!item.evidence_status && !['available', 'ready', 'captured'].includes(item.evidence_status);
}
function post(path, data) {
  return request(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
}
function warnings(container, data) {
  container.replaceChildren();
  if (container === $('projectsWarnings')) {
    if (listOf(data.errors).length || listOf(data.content_warnings).length || data.partial) {
      const notice = text('p', '部分来源暂未读全，当前项目列表可能不完整。 ', 'work-warning');
      notice.append(button('查看来源状态', () => window.HVSettings?.open('sources')));
      container.append(notice);
    }
    if (data.truncated) container.append(text('p', '项目列表达到读取上限。可使用“搜索历史”按项目路径缩小范围。', 'work-warning'));
    return;
  }
  for (const error of listOf(data.errors)) container.append(text('p', `部分来源未能读取：${error.source || '未知来源'}（${error.error || error.error_code || '读取失败'}）。以下结果并非全部历史。`, 'work-warning'));
  for (const warning of listOf(data.content_warnings)) container.append(text('p', `${sourceLabel(warning.source)} 有 ${warning.incomplete_sessions} 个会话正文未完整解码；搜索仅覆盖可读取内容。`, 'work-warning'));
  if (data.partial && !listOf(data.errors).length && !data.truncated) container.append(text('p', '部分结果或摘录未完整读取。请缩小筛选范围，或打开完整会话核对。', 'work-warning'));
  if (data.pagination_status === 'restart_after_source_error') container.append(text('p', '部分来源读取失败，本次只能查看第一页。请选择可用来源重新搜索，或修复来源后重试。', 'work-warning'));
  if (data.truncated) container.append(text('p', '本次结果达到读取上限。请增加关键词、项目或日期筛选以缩小范围。', 'work-warning'));
}
function meta(item) {
  const node = text('div', '', 'result-meta');
  node.append(text('span', item.source || '来源未知'), text('span', dateLabel(item.updated_at || item.last_activity)));
  node.append(text('span', item.project || '项目未绑定', 'result-project'));
  if (item.content_status && item.content_status !== 'decoded_text') {
    node.append(text('span', item.content_status === 'partial_unsupported_steps' ? '部分步骤未解码，正文不完整' : '仅元数据，正文不可用', 'work-warning'));
  }
  return node;
}
function section(label, values, empty) {
  const node = text('section', '', 'work-fact');
  node.append(text('h4', label));
  if (values.length) {
    const list = text('ul', '');
    for (const value of values) list.append(text('li', value));
    node.append(list);
  } else node.append(text('p', empty, 'muted'));
  return node;
}
const description = value => typeof value === 'string' ? value : value?.summary || value?.description || '';
function hasSourceRevision(item) {
  const provenance = item.provenance || {};
  return provenance.status === 'captured' && [provenance.content_revision, provenance.context_revision].some(value => typeof value === 'string' && /^sha256:[0-9a-f]{64}$/.test(value));
}
function observedFailures(item) {
  const count = item.failures?.observed_error_count;
  return Number.isSafeInteger(count) && count >= 0 ? count : null;
}
let rawPanelCounter = 0;
function rawRecord(item, ref, label = '核对原始记录') {
  const wrapper = text('div', '', 'raw-record');
  if (['agy', 'antigravity', 'zcode', 'mcode'].includes(item.source)) {
    wrapper.append(text('p', '此来源提供公开消息交接；请通过“核对完整消息”查看原文。数据库行或混合内部块不作为原始 JSONL 展开。', 'muted'));
    return wrapper;
  }
  const line = ref.raw_ref?.line_no ?? ref.line_no;
  const evidenceId = ref.id || ref.evidence_id;
  if (!hasSourceRevision(item) || (!evidenceId && !Number.isInteger(line))) {
    wrapper.append(text('p', '缺少可核对的来源修订或原始行，暂不能精确展开；可打开完整会话核对。', 'muted'));
    return wrapper;
  }
  const panel = text('div', ''); panel.hidden = true; panel.id = `raw-record-${++rawPanelCounter}`;
  let sequence = 0;
  const open = button(label, async () => {
    if (!panel.hidden) { sequence++; panel.hidden = true; open.setAttribute('aria-expanded', 'false'); return; }
    panel.hidden = false; open.setAttribute('aria-expanded', 'true');
    panel.replaceChildren(text('p', '正在核对原始记录…', 'muted'));
    const current = ++sequence;
    try {
      const provenance = item.provenance;
      const params = new URLSearchParams({system: item.system || 'linux', source: item.source || '', session: item.id || item.session_id || '', content_revision: provenance.content_revision || 'unknown', context_revision: provenance.context_revision || 'unknown'});
      if (item.store_id) params.set('store_id', item.store_id);
      if (evidenceId) params.set('evidence_id', evidenceId);
      else params.set('line_no', String(line));
      const data = await request(`/api/reuse/raw?${params.toString()}`);
      if (current !== sequence) return;
      if (typeof data.text !== 'string' || (!Number.isInteger(data.line_no) && !/^\/messages\/\d+$/.test(data.json_pointer || ''))) throw new Error('invalid_raw_record');
      const location = Number.isInteger(data.line_no) ? `原始行 ${data.line_no}` : `原始记录 ${data.json_pointer}`;
      panel.replaceChildren(text('p', `${location}；历史记录，不代表当前代码状态。`, 'muted'));
      const raw = text('pre', data.text); raw.setAttribute('tabindex', '0'); raw.setAttribute('aria-label', location); panel.append(raw);
      if (data.truncated) panel.append(text('p', '原始记录过长，当前仅显示有界摘录。', 'work-warning'));
    } catch (error) {
      if (current !== sequence) return;
      panel.replaceChildren(text('p', `原始记录读取失败（${error.message}）。${errorAdvice(error, '关闭后可重试，或打开完整会话核对。')}`, 'work-warning work-error'));
    }
  });
  open.setAttribute('aria-expanded', 'false'); open.setAttribute('aria-controls', panel.id);
  wrapper.append(open, panel);
  return wrapper;
}
function selectionFor(item, ref) {
  const provenance = item.provenance || {};
  return {system: item.system || 'linux', source: item.source || '', store_id: item.store_id,
    session_id: item.id || item.session_id, evidence_id: ref.id, message_index: ref.message_index,
    content_revision: provenance.content_revision, context_revision: provenance.context_revision};
}
function selectionKey(selection) {
  return JSON.stringify([selection.system, selection.source, selection.store_id || '', selection.session_id, selection.evidence_id, selection.message_index]);
}
function invalidatePreview() {
  invalidateMaterial();
  state.selectionVersion++;
  state.selectionSequence++;
  state.preview = null;
  $('selectionExport').hidden = true;
  $('selectionMarkdown').textContent = '';
  $('selectionStatus').textContent = '';
}
function toggleSelection(item, ref, checked) {
  if (checked && evidenceFailed(item)) {
    $('selectionStatus').textContent = '证据读取失败，不能加入交接。请重新读取会话后再选。';
    syncSelectionControls();
    return false;
  }
  const selection = selectionFor(item, ref);
  const key = selectionKey(selection);
  if (checked && !state.selected.has(key) && state.selected.size >= 5) {
    $('selectionStatus').textContent = '最多选择 5 条证据。请先移除不需要的条目。';
    syncSelectionControls();
    return false;
  }
  invalidatePreview();
  if (checked) state.selected.set(key, {selection, title: item.title || item.goal || item.id, summary: ref.summary || `证据 ${ref.id}`, role: ref.role});
  else state.selected.delete(key);
  renderSelection();
  return true;
}
function syncSelectionControls() {
  for (const input of document.querySelectorAll('[data-evidence-selection]')) {
    input.checked = state.selected.has(input.dataset.evidenceSelection);
  }
}
function evidenceSection(item) {
  if (evidenceFailed(item)) {
    const node = section('证据', [], '证据读取失败或来源不支持，本次内容不能加入交接。');
    node.append(text('p', errorAdvice(item.error || item.error_code || item.evidence_status, '请重新读取会话，或打开完整会话核对。'), 'work-warning'));
    node.append(historyLink(item, '打开完整会话核对'));
    return node;
  }
  const node = section('证据', listOf(item.verified).map(check => `历史验证：${check.command || '命令未记录'}（${check.status || 'unknown'}）`), '未提取到验证依据，请进入历史核对。');
  for (const ref of listOf(item.evidence)) {
    const row = text('div', '', 'evidence-item');
    const label = text('label', '', 'evidence-choice');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.dataset.evidenceSelection = selectionKey(selectionFor(item, ref));
    input.checked = state.selected.has(input.dataset.evidenceSelection);
    const selectable = !!ref.id && hasSourceRevision(item);
    input.disabled = !selectable;
    if (!selectable) input.title = '缺少可核对的来源修订，暂不能加入交接。';
    input.addEventListener('change', () => toggleSelection(item, ref, input.checked));
    const location = Number.isInteger(ref.message_index) ? `审计事件 ${ref.message_index}` : `证据 ${ref.id || '未知'}`;
    label.append(input, text('span', `审计摘要（导出摘要）：${location}${Number.isInteger(ref.line_no) ? ` / 原始行 ${ref.line_no}` : ''}：${ref.summary || '查看原文'}`));
    row.append(label, rawRecord(item, ref));
    if (!selectable) row.append(text('p', '来源修订未知，暂不能选择；仍可查看原文。', 'muted'));
    node.append(row);
  }
  node.append(text('p', '命令成功与历史自报不等于当前代码已验证；当前 Git HEAD 也不是历史基线证明。', 'muted'));
  node.append(historyLink(item, '打开完整会话'));
  return node;
}
function fileChanges(item, activeFile = '') {
  const changes = listOf(item.file_changes);
  const node = section('成果', listOf(item.changed).map(change => `历史变更：${change.path || description(change) || '路径未记录'}`), '未提取到变更记录；不代表没有成果。');
  const statuses = {available: '已提取', known: '已提取', partial: '部分可用', unavailable: '不可用', unsupported: '来源不支持', unknown: '未知', attempted: '记录过修改尝试', none: '未记录'};
  if (item.file_status) node.append(text('p', `文件记录：${statuses[item.file_status] || '未知'}。`, 'muted'));
  if (item.diff_status) node.append(text('p', `补丁记录：${statuses[item.diff_status] || '未知'}。`, 'muted'));
  if (!changes.length) {
    node.append(text('p', '历史补丁不可用；不能据此判断当前文件是否已修改。', 'muted'));
    return node;
  }
  const normalizedFile = String(activeFile).replaceAll('\\', '/').replace(/^\.\//, '');
  for (const change of changes) {
    const path = String(change.path || '路径未记录');
    const normalizedPath = path.replaceAll('\\', '/').replace(/^\.\//, '');
    const selected = normalizedFile && (normalizedPath === normalizedFile || normalizedPath.endsWith(`/${normalizedFile}`));
    const row = text('div', '', `file-change${selected ? ' file-change-selected' : ''}`);
    row.append(text('p', `${selected ? '当前筛选文件：' : ''}${path}`, 'file-change-path'));
    const changeStatus = {attempted: '历史记录：尝试修改；执行结果与当前文件状态未验证。', success: '历史工具执行成功；当前文件状态未验证。', succeeded: '历史工具执行成功；当前文件状态未验证。', failed: '历史工具执行失败；是否在后续修复及当前文件状态未知。'};
    row.append(text('p', changeStatus[change.status] || '历史修改结果未知；当前文件状态未验证。', 'muted'));
    if (change.scope) row.append(text('p', `历史记录范围：${change.scope}`, 'muted'));
    if (typeof change.diff === 'string' && change.diff.trim()) {
      const diff = text('details', '', 'historical-diff');
      diff.append(text('summary', '查看历史补丁（纯文本）'));
      const patch = text('pre', change.diff); patch.setAttribute('tabindex', '0'); patch.setAttribute('aria-label', `${path} 的历史补丁`);
      diff.append(patch); row.append(diff);
    } else row.append(text('p', '历史补丁不可用。请打开原文核对修改意图。', 'muted'));
    row.append(rawRecord(item, change, '核对修改原始记录'));
    node.append(row);
  }
  return node;
}
function facts(item, activeFile = '') {
  const node = text('div', '', 'work-facts');
  const labels = {completed: '历史自报完成 · 当前成果未验证', partial: '历史记录部分完成', failed: '历史记录有失败', blocked: '历史记录有阻塞', unknown: '历史进展未知'};
  node.append(section('进展', [labels[item.status] || labels.unknown], '历史进展未知'));
  node.append(fileChanges(item, activeFile));
  const blockers = listOf(item.blockers).map(description);
  const errorCount = observedFailures(item);
  if (errorCount > 0) blockers.unshift(`历史审计共观察到 ${errorCount} 次失败；当前是否解除未知。后续成功记录不代表这些失败已解决。`);
  else if (errorCount === 0) blockers.push('历史审计未观察到失败；不代表当前运行没有问题。');
  if (!blockers.length && ['failed', 'blocked'].includes(item.status)) blockers.push('历史记录出现失败或阻塞，需核对是否解除。');
  node.append(section('阻塞', blockers, '未提取到历史阻塞；当前是否受阻未知。'));
  node.append(section('待决定', listOf(item.decisions).map(description), '未记录待决定事项；实时审批状态未知。'));
  const next = !item.next_action || String(item.next_action).startsWith('Session outcome is ') ? '核对未完成项与当前代码后继续' : item.next_action;
  node.append(section('下一步', [next, ...listOf(item.remaining).map(description)], '核对当前项目后继续'));
  node.append(evidenceSection(item));
  return node;
}
function record(item, now = Date.now(), activeFile = '') {
  const node = text('details', '', 'work-record');
  const errors = observedFailures(item);
  node.append(text('summary', `${item.goal || item.title || '未命名会话'}${errors > 0 ? ` / 历史失败 ${errors} 次` : ''}`), meta(item));
  if (isStale(item, now)) node.append(text('p', '超过 7 天未见新记录，续接前请核对当前项目。', 'work-warning'));
  node.append(facts(item, activeFile));
  return node;
}
function matchingMessages(item, snippets, query, heading = '命中消息') {
  const node = text('section', '', 'matched-message-choices');
  node.append(text('h4', heading));
  node.append(text('p', '下方展示命中摘录。选择后导出经修订核对的完整消息；合计超过 8000 字符会明确拒绝，不会悄悄截断。', 'muted'));
  for (const snippet of snippets) {
    if (!Number.isInteger(snippet.message_index) || snippet.message_index < 0) continue;
    const ref = {message_index: snippet.message_index, summary: snippet.text || '', role: snippet.role};
    const row = text('div', '', 'evidence-item');
    const label = text('label', '', 'evidence-choice');
    const input = document.createElement('input'); input.type = 'checkbox';
    input.dataset.evidenceSelection = selectionKey(selectionFor(item, ref));
    input.checked = state.selected.has(input.dataset.evidenceSelection);
    input.disabled = evidenceFailed(item) || !hasSourceRevision(item) || !!item.provenance?.truncated;
    input.addEventListener('change', () => toggleSelection(item, ref, input.checked));
    label.append(input, text('span', `完整消息 ${snippet.message_index}${snippet.role ? `（${snippet.role}）` : ''}：${snippet.text || '打开命中原文核对'}`));
    row.append(label, historyLink(item, '核对完整消息', snippet.message_index, query));
    if (input.disabled) row.append(text('p', '完整消息暂无法绑定到已核对的来源修订。请打开完整会话核对并另行提供需求原文；审计摘要不能替代验收需求。', 'muted'));
    node.append(row);
  }
  return node;
}
function reviewRequests(item) {
  const node = text('section', '', 'review-requests');
  const panel = text('div', ''); panel.hidden = true;
  const status = text('p', '', 'muted'); status.setAttribute('role', 'status');
  const rows = text('div', '');
  let sequence = 0, nextOffset = 0;
  const more = button('更多用户消息', () => load(true)); more.hidden = true;
  async function load(append = false) {
    const current = ++sequence;
    status.textContent = '正在读取用户原文…'; more.disabled = true;
    const params = new URLSearchParams({system:item.system || 'linux', source:item.source,
      session:item.id || item.session_id, source_revision:item.source_revision || '',
      content_revision:item.provenance?.content_revision || 'unknown', context_revision:item.provenance?.context_revision || 'unknown',
      offset:String(append ? nextOffset : 0), limit:'10'});
    if (item.store_id) params.set('store_id', item.store_id);
    try {
      const data = await request(`/api/reuse/review-requests?${params.toString()}`);
      if (current !== sequence || panel.hidden) return;
      if (!Array.isArray(data.items)) throw new Error('invalid_review_requests');
      if (!append) rows.replaceChildren();
      if (data.items.length) rows.append(matchingMessages(item, data.items, '', '用户需求与后续修订'));
      nextOffset = data.next_offset;
      more.hidden = nextOffset == null;
      status.textContent = data.total ? `共 ${data.total} 条用户消息，已显示 ${nextOffset ?? data.total} 条。请核对需求以及后续纠正；用户消息不一定都是本次需求。` : '未找到用户原文，不能据此推断没有需求。';
    } catch (error) {
      if (current !== sequence || panel.hidden) return;
      rows.replaceChildren(); more.hidden = true;
      status.textContent = `无法读取用户需求（${error.message}）。${/selection_message_outside_verified_scope/.test(error.message) ? '此会话超出可导出范围，请打开完整会话另行核对原文；不要用摘要替代。' : errorAdvice(error)}`;
    } finally { if (current === sequence) more.disabled = false; }
  }
  const open = button('选择用户需求原文', async () => {
    panel.hidden = !panel.hidden; open.setAttribute('aria-expanded', String(!panel.hidden));
    if (panel.hidden) { sequence++; return; }
    await load();
  });
  open.setAttribute('aria-expanded', 'false');
  panel.append(status, rows, more); node.append(open, panel);
  return node;
}
function searchResult(item, query) {
  const node = text('article', '', 'reuse-result');
  const title = text('h3', '');
  const snippets = listOf(item.snippets);
  title.append(historyLink(item, item.title || '未命名会话', snippets[0]?.message_index, query));
  node.append(title, meta(item));
  if (item.match_reason) node.append(text('p', `命中依据：${description(item.match_reason) || String(item.match_reason)}`, 'muted'));
  for (const snippet of snippets) {
    const block = text('div', '', 'result-snippet');
    block.append(text('div', snippet.text || ''), historyLink(item, `打开命中原文${snippet.role ? `（${snippet.role}）` : ''}`, snippet.message_index, query));
    node.append(block);
  }
  const detail = text('div', '', 'evidence-panel');
  detail.hidden = true;
  let sequence = 0;
  const open = button('查看并选择证据', async () => {
    if (!detail.hidden) { detail.hidden = true; open.setAttribute('aria-expanded', 'false'); sequence++; return; }
    detail.hidden = false;
    open.setAttribute('aria-expanded', 'true');
    detail.replaceChildren(text('p', '正在读取这次会话的证据…', 'muted'));
    const current = ++sequence;
    try {
      const params = new URLSearchParams({system: item.system || 'linux', source: item.source || '', session: item.id || ''});
      if (item.source_revision) params.set('source_revision', item.source_revision);
      const data = await request(`/api/reuse/evidence?${params.toString()}`);
      if (current !== sequence) return;
      const evidence = data.item || data;
      if (evidenceFailed(evidence)) throw new Error(evidence.error || evidence.error_code || evidence.evidence_status);
      if (!Array.isArray(evidence.evidence)) throw new Error('invalid_evidence');
      const boundItem = {...item, ...evidence};
      detail.replaceChildren();
      detail.append(reviewRequests(boundItem));
      if (snippets.length) detail.append(matchingMessages(boundItem, snippets, query));
      detail.append(facts(boundItem));
    } catch (error) {
      if (current !== sequence) return;
      detail.replaceChildren(text('p', `证据读取失败（${error.message}）。${errorAdvice(error, '关闭后可重新打开重试。')}`, 'work-warning work-error'));
    }
  });
  const panelId = `evidence-${++evidencePanelCounter}`;
  detail.id = panelId;
  open.setAttribute('aria-expanded', 'false');
  open.setAttribute('aria-controls', panelId);
  const actions = text('div', '', 'result-actions');
  actions.append(open);
  if (item.project != null) actions.append(button('查看工作记录', () => selectProject(item.project)));
  node.append(actions, detail);
  return node;
}
let evidencePanelCounter = 0;
const pageElements = {
  search: {list: 'searchResults', status: 'searchStatus', warnings: 'searchWarnings', more: 'searchMore', label: '结果'},
  projects: {list: 'projectList', status: 'projectsStatus', warnings: 'projectsWarnings', more: 'projectsMore', label: '项目'},
  timeline: {list: 'timelineResults', status: 'timelineStatus', warnings: 'timelineWarnings', more: 'timelineMore', label: '会话'},
};
function renderPage(kind) {
  const page = state.pages[kind], elements = pageElements[kind], list = $(elements.list);
  list.replaceChildren();
  for (const item of page.items) {
    if (kind === 'search') list.append(searchResult(item, page.params.q || ''));
    else if (kind === 'timeline') list.append(record(item, Date.now(), page.params.file || ''));
    else {
      const row = text('div', '', 'project-row');
      const choice = text('a', '', 'project-choice');
      const project = item.project ?? '';
      choice.href = `/history?${new URLSearchParams({project}).toString()}`;
      const identity = text('span', '', 'project-identity');
      identity.append(text('strong', item.label || project || '未绑定项目'), text('span', project || '这些会话未记录项目目录', 'project-path muted'));
      const activity = text('span', '', 'project-activity muted');
      const providers = [...new Set(listOf(item.sources).map(source => sourceLabel(source.source)))];
      activity.append(text('span', `${item.session_count || 0} 个对话${providers.length ? ` · ${providers.join('、')}` : ''}`), text('span', dateLabel(item.last_activity)));
      choice.append(identity, activity);
      const records = button('查看工作记录', () => selectProject(project), 'btn small project-records');
      records.setAttribute('aria-label', `查看 ${item.label || project || '未绑定项目'} 的工作记录`);
      records.setAttribute('aria-expanded', String(state.project === project && !$('timelineSection').hidden));
      records.setAttribute('aria-controls', 'timelineSection');
      row.append(choice, records);
      list.append(row);
    }
  }
  $(elements.status).textContent = page.items.length ? kind === 'projects' ? `${page.items.length} 个项目，按最近活动排列。选择项目即可阅读对话。` : `已显示 ${page.items.length} 个${elements.label}。` : kind === 'search' ? '没有匹配结果。试试更短的关键词，或清除来源、项目与日期筛选。' : kind === 'projects' ? '还没有项目记录。请在设置中检查来源；索引完成后刷新列表。' : '当前项目与文件条件下没有会话。清除文件筛选后重试。';
  $(elements.more).hidden = !page.cursor;
  $(elements.more).disabled = false;
}
async function loadPage(kind, params, append = false) {
  const page = state.pages[kind], elements = pageElements[kind], sequence = ++page.sequence;
  if (!append) { page.items = []; page.cursor = null; page.params = {...params}; $(elements.list).replaceChildren(); }
  const query = new URLSearchParams(page.params);
  query.set('limit', '20');
  if (append && page.cursor) query.set('cursor', page.cursor);
  $(elements.more).disabled = true;
  $(elements.more).hidden = true;
  $(elements.status).textContent = `正在读取${elements.label}…`;
  $(elements.warnings).replaceChildren();
  try {
    const data = await request(`/api/reuse/${kind}?${query.toString()}`);
    if (sequence !== page.sequence) return;
    if (!Array.isArray(data.items)) throw new Error('invalid_response');
    page.items = append ? page.items.concat(data.items) : data.items;
    page.cursor = data.pagination_status === 'restart_after_source_error' ? null : data.next_cursor || null;
    renderPage(kind);
    warnings($(elements.warnings), data);
  } catch (error) {
    if (sequence !== page.sequence) return;
    page.items = []; page.cursor = null;
    $(elements.list).replaceChildren();
    $(elements.more).hidden = true;
    $(elements.status).textContent = `读取失败（${error.message}）。${errorAdvice(error, '请重新搜索或重新读取。')}已清除旧结果。`;
  }
}
function persistSearch(params) {
  if (!window.history?.replaceState || !window.location) return;
  const query = new URLSearchParams({view: 'search'});
  if (state.selectionPurpose === 'review') query.set('purpose', 'review');
  for (const key of ['q', 'source', 'project', 'start', 'end']) if (params[key]) query.set(key, params[key]);
  const value = query.toString();
  window.history.replaceState(null, '', `${window.location.pathname || '/'}${value ? `?${value}` : ''}${window.location.hash || ''}`);
}
function setSourceFilter(value) {
  const select = $('reuseSource');
  if (value && !Array.from(select.children).some(option => option.value === value)) {
    const option = text('option', `${value}（未配置或状态未知）`); option.value = value; select.append(option);
  }
  select.value = value || '';
}
function restoreSearchFromUrl(searchValue = window.location?.search || '') {
  const params = new URLSearchParams(searchValue);
  if (!params.has('q') && params.get('view') !== 'search') return false;
  for (const [id, key] of [['reuseQuery', 'q'], ['reuseProject', 'project'], ['reuseStart', 'start'], ['reuseEnd', 'end']]) $(id).value = params.get(key) || '';
  setSourceFilter(params.get('source') || '');
  if (params.get('purpose') === 'review') setSelectionPurpose('review');
  return true;
}
function searchParams() {
  const result = {q: $('reuseQuery').value.trim()};
  for (const [id, key] of [['reuseSource', 'source'], ['reuseProject', 'project'], ['reuseStart', 'start'], ['reuseEnd', 'end']]) if ($(id).value.trim()) result[key] = $(id).value.trim();
  return result;
}
async function search(append = false) {
  setView('search', false);
  if (append) return loadPage('search', null, true);
  const params = searchParams();
  persistSearch(params);
  state.query = params.q;
  if (!params.q) {
    state.pages.search.sequence++; state.pages.search.items = []; state.pages.search.cursor = null;
    $('searchResults').replaceChildren(); $('searchWarnings').replaceChildren(); $('searchMore').hidden = true;
    $('searchStatus').textContent = '输入记得的关键词、报错或文件名，再开始搜索。';
    return;
  }
  return loadPage('search', params);
}
function updateLayout() {
  const hasSelectionPanel = state.view !== 'cases' && (state.view === 'search' || (state.view === 'projects' && !$('timelineSection').hidden) || state.selected.size > 0);
  $('selectionPanel').hidden = !hasSelectionPanel;
  $('reuseLayout').className = `reuse-layout${hasSelectionPanel ? '' : ' projects-home'}`;
}
function setView(view, persist = true) {
  state.view = view;
  for (const name of workspaceViews) {
    const selected = name === view;
    $(`${name}View`).hidden = !selected;
    $(`${name}Tab`).setAttribute('aria-selected', String(selected));
    $(`${name}Tab`).setAttribute('tabindex', selected ? '0' : '-1');
    $(`${name}Tab`).className = `tab${selected ? ' active' : ''}`;
  }
  updateLayout();
  if (view !== 'cases' && state.healthSequence === 0) loadHealth();
  if (persist && window.history?.replaceState) {
    if (view === 'search') persistSearch(state.pages.search.params);
    else if (view === 'cases') persistCaseLocation();
    else window.history.replaceState(null, '', `${window.location?.pathname || '/'}${window.location?.hash || ''}`);
  }
  if (view === 'projects' && !state.pages.projects.items.length) return loadPage('projects', {});
}
function showProjects() {
  state.project = null;
  $('timelineSection').hidden = true;
  renderPage('projects');
  return setView('projects');
}
function selectProject(project) {
  if (project == null) return;
  state.project = String(project);
  setView('projects');
  $('timelineSection').hidden = false;
  updateLayout();
  $('timelineTitle').textContent = state.project || '未绑定项目的历史';
  $('timelineFile').value = '';
  renderPage('projects');
  return loadPage('timeline', {project: state.project});
}
function timeline(append = false) {
  if (state.project === null) return;
  if (append) return loadPage('timeline', null, true);
  const params = {project: state.project};
  if ($('timelineFile').value.trim()) params.file = $('timelineFile').value.trim();
  return loadPage('timeline', params);
}

function persistCaseLocation() {
  if (!window.history?.replaceState || !window.location) return;
  const params = new URLSearchParams({view: 'cases'});
  for (const key of ['q', 'project']) if (state.pages.cases.params[key]) params.set(key, state.pages.cases.params[key]);
  if (state.caseId) params.set('case', state.caseId);
  window.history.replaceState(null, '', `${window.location.pathname || '/'}?${params}${window.location.hash || ''}`);
}
function caseDisplayText(value) {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  try {
    return JSON.stringify(value) ?? '未知';
  } catch {
    return '未知';
  }
}
function renderCaseList() {
  $('casesList').replaceChildren();
  for (const item of state.pages.cases.items) {
    const card = text('article', '', 'reuse-result');
    card.append(text('h3', caseDisplayText(item.title || item.id)));
    if (item.question) card.append(text('p', caseDisplayText(item.question)));
    if (item.project || listOf(item.tags).length) card.append(text('p', [caseDisplayText(item.project), listOf(item.tags).map(caseDisplayText).join('、')].filter(Boolean).join(' · '), 'muted'));
    if (item.now?.judgment) card.append(text('p', `现在的判断：${caseDisplayText(item.now.judgment)}`));
    if (item.next_action) card.append(text('p', `下一步：${caseDisplayText(item.next_action)}`, 'muted'));
    for (const warning of listOf(item.warnings)) card.append(text('p', `内容提示：${caseDisplayText(warning)}`, 'muted'));
    const open = button('阅读案例', () => openCase(item.id), 'btn small case-choice');
    open.setAttribute('aria-current', String(item.id === state.caseId));
    card.append(open);
    $('casesList').append(card);
  }
}
function caseErrorText(error) {
  if (typeof error === 'string') return error;
  return [error?.id || error?.path || error?.file, error?.error || error?.code || '无法读取'].filter(Boolean).map(caseDisplayText).join('：');
}
function clearCaseDetail() {
  state.caseSequence++;
  state.caseId = '';
  state.casePreview = null;
  $('caseDetail').hidden = true;
  $('caseMarkdown').textContent = '';
  $('caseDetailStatus').textContent = '';
  $('caseOrigin').textContent = '';
  $('caseEditablePath').textContent = '';
  $('caseCopy').disabled = true;
  $('caseDownload').disabled = true;
}
async function loadCases(append = false) {
  const page = state.pages.cases, sequence = ++page.sequence;
  if (append && page.nextOffset == null) return;
  const previousItems = page.items, previousNextOffset = page.nextOffset;
  if (!append) { page.items = []; page.nextOffset = null; $('casesList').replaceChildren(); }
  const params = new URLSearchParams(page.params);
  params.set('offset', String(append ? page.nextOffset : 0));
  params.set('limit', '20');
  $('casesMore').disabled = true;
  $('casesMore').hidden = true;
  $('casesWarnings').replaceChildren();
  $('casesStatus').textContent = '正在读取问题案例…';
  try {
    const data = await request(`/api/reuse/cases?${params}`);
    if (sequence !== page.sequence) return;
    if (!Array.isArray(data.items)) throw new Error('invalid_cases');
    page.items = append ? page.items.concat(data.items) : data.items;
    page.nextOffset = Number.isInteger(data.next_offset) && data.next_offset > Number(params.get('offset')) ? data.next_offset : null;
    page.loaded = true;
    renderCaseList();
    const partial = data.partial || listOf(data.errors).length > 0;
    $('casesStatus').textContent = page.items.length
      ? `已显示 ${page.items.length} 个案例${Number.isFinite(data.total) ? `，共 ${data.total} 个匹配` : ''}。`
      : partial ? '尚未读到匹配案例，部分文件读取失败，当前结果不完整。' : '没有匹配案例。试试问题中的关键词，或清除项目筛选。';
    if (partial) $('casesWarnings').append(text('p', '部分案例文件读取失败，当前列表不完整；其余案例仍可阅读。', 'work-warning'));
    for (const error of listOf(data.errors)) {
      $('casesWarnings').append(text('p', caseErrorText(error), 'muted'));
    }
    $('casesMore').hidden = page.nextOffset == null;
  } catch (error) {
    if (sequence !== page.sequence) return;
    if (append) {
      page.items = previousItems; page.nextOffset = previousNextOffset;
      renderCaseList();
      $('casesMore').hidden = page.nextOffset == null;
      $('casesStatus').textContent = `加载更多案例失败（${error.message}）。已保留 ${page.items.length} 个案例，可再次点击“加载更多案例”重试。`;
    } else {
      page.items = []; page.nextOffset = null;
      $('casesList').replaceChildren();
      $('casesStatus').textContent = `案例读取失败（${error.message}）。可点击“刷新案例”重试，未将失败视为零匹配。`;
    }
  } finally {
    if (sequence === page.sequence) $('casesMore').disabled = false;
  }
}
async function searchCases(append = false) {
  if (!append) {
    state.pages.cases.params = {};
    for (const [key, id] of [['q', 'casesQuery'], ['project', 'casesProject']]) {
      if ($(id).value.trim()) state.pages.cases.params[key] = $(id).value.trim();
    }
    clearCaseDetail();
  }
  persistCaseLocation();
  return loadCases(append);
}
function showCases() {
  setView('cases');
  if (!state.pages.cases.loaded) return searchCases();
}
async function openCase(id, {persist = true} = {}) {
  const sequence = ++state.caseSequence;
  state.caseId = String(id || '');
  state.casePreview = null;
  $('caseDetail').hidden = false;
  $('caseDetailTitle').textContent = '案例正文';
  $('caseMarkdown').textContent = '';
  $('caseOrigin').textContent = '';
  $('caseEditablePath').textContent = '';
  $('caseCopy').disabled = true;
  $('caseDownload').disabled = true;
  $('caseDetailStatus').textContent = '正在读取案例正文…';
  if (persist) persistCaseLocation();
  renderCaseList();
  const requestedId = state.caseId;
  try {
    const data = await request(`/api/reuse/case?${new URLSearchParams({id: requestedId})}`);
    if (sequence !== state.caseSequence) return;
    if (data.editable_path) $('caseEditablePath').textContent = `可编辑 JSON：${data.editable_path}。保存修改后点击“刷新案例”。`;
    if (!data.case && (data.partial || listOf(data.errors).length)) {
      throw new Error(`案例文件未能读取：${listOf(data.errors).map(caseErrorText).join('；') || '读取结果不完整'}`);
    }
    if (typeof data.markdown !== 'string' || data.case?.id !== requestedId) throw new Error('invalid_case');
    state.casePreview = data;
    $('caseDetailTitle').textContent = data.case.title || requestedId;
    $('caseMarkdown').textContent = data.markdown;
    $('caseOrigin').textContent = data.origin === 'example' ? '随附示例：保存同 ID 的本地 JSON 可覆盖示例。' : '本地案例';
    $('caseEditablePath').textContent = data.editable_path ? `可编辑 JSON：${data.editable_path}。保存修改后点击“刷新案例”。` : '修改本地案例 JSON 后，点击“刷新案例”重新读取。';
    $('caseDetailStatus').textContent = '与 Agent 读取的同一份正文。属于 Agent 或用户解释，来源引用未重新核对，当前任务未验收。';
    $('caseCopy').disabled = false;
    $('caseDownload').disabled = false;
  } catch (error) {
    if (sequence !== state.caseSequence) return;
    $('caseDetailStatus').textContent = `案例正文读取失败（${error.message}）。可刷新重试，旧正文已清除。`;
  }
}
function refreshCases() {
  const id = state.caseId;
  return Promise.all([loadCases(), id ? openCase(id, {persist: false}) : Promise.resolve()]);
}
async function copyCase() {
  const preview = state.casePreview;
  if (!preview) return;
  try {
    await navigator.clipboard.writeText(preview.markdown);
    if (state.casePreview === preview) $('caseDetailStatus').textContent = '已复制同一份案例 Markdown；当前任务未验收。';
  } catch {
    if (state.casePreview === preview) $('caseDetailStatus').textContent = '浏览器未允许复制。可选择下方正文，或下载 Markdown。';
  }
}
function downloadCase() {
  if (!state.casePreview) return;
  const filename = String(state.casePreview.case.id).replace(/[^\w.-]+/g, '-');
  download(`case-${filename}.md`, state.casePreview.markdown, 'text/markdown;charset=utf-8');
}

function setSelectionPurpose(purpose) {
  state.selectionPurpose = purpose === 'review' ? 'review' : 'handoff';
  $('selectionPurpose').value = state.selectionPurpose;
  invalidatePreview();
  renderSelection();
}
function renderSelection() {
  updateLayout();
  $('materialPreview').disabled = !state.selected.size;
  $('selectionCount').textContent = `${state.selected.size} / 5`;
  $('selectionPreview').disabled = !state.selected.size;
  $('selectionClear').disabled = !state.selected.size;
  const review = state.selectionPurpose === 'review';
  $('selectionPreview').textContent = review ? '生成验收交接预览' : '生成交接预览';
  $('selectionGuide').textContent = review
    ? '先搜索并选择用户原始需求、后续纠正，再选实现或测试线索。至少一条用户完整消息，最多 5 条、共 8000 字符。这里只生成验收包，不会自动调用模型或读取当前代码。'
    : '从会话中选择证据，核对预览后复制或下载。最多 5 条。';
  $('selectionList').replaceChildren();
  for (const [key, item] of state.selected) {
    const row = text('div', '', 'selection-item');
    row.append(text('p', `${item.selection.source} / ${item.title}`), text('p', item.selection.evidence_id ? '将导出审计摘要' : `将导出完整消息 ${item.selection.message_index}${item.role === 'user' ? ' · 用户原文' : item.role === 'assistant' ? ' · 助手历史陈述' : ''}（当前仅显示摘录）`, 'muted'), text('p', item.summary));
    row.append(button('移除', () => { invalidatePreview(); state.selected.delete(key); renderSelection(); }));
    $('selectionList').append(row);
  }
  syncSelectionControls();
}
async function previewSelection() {
  if (!state.selected.size) return;
  const version = state.selectionVersion, sequence = ++state.selectionSequence;
  const review = state.selectionPurpose === 'review';
  state.preview = null; $('selectionExport').hidden = true; $('selectionMarkdown').textContent = '';
  $('selectionPreview').disabled = true;
  $('selectionStatus').textContent = '正在核对所选证据并生成预览…';
  try {
    const data = await post(review ? '/api/reuse/review-preview' : '/api/reuse/selection', {selections: Array.from(state.selected.values(), item => item.selection)});
    if (version !== state.selectionVersion || sequence !== state.selectionSequence) return;
    if (typeof data.markdown !== 'string' || !Array.isArray(data.items) || data.authorization !== 'context_only') throw new Error('invalid_selection');
    if (review && (data.schema_version !== 'history.review-packet.v1' || data.code_verification !== 'not_performed')) throw new Error('invalid_review_packet');
    state.preview = data;
    $('selectionMarkdown').textContent = data.markdown;
    $('selectionExport').hidden = false;
    $('selectionStatus').textContent = review ? '验收包已就绪，尚未验收代码。核对需求与后续纠正后，把此包交给当前项目中的高级模型。' : '预览已就绪。请核对下方内容，再复制或下载。';
  } catch (error) {
    if (version !== state.selectionVersion || sequence !== state.selectionSequence) return;
    const advice = review && /selection_body_limit_exceeded|selection_message_outside_verified_scope/.test(error.message)
      ? '需求原文超出导出范围，未生成不完整验收包。请分批交接，或在完整会话中核对原文；不要用摘要替代需求。'
      : errorAdvice(error, '来源可能已变化，请重新读取并选择证据。');
    $('selectionStatus').textContent = `无法生成交接（${error.message}）。${advice}`;
  } finally {
    if (version === state.selectionVersion && sequence === state.selectionSequence) $('selectionPreview').disabled = !state.selected.size;
  }
}
const materialFields = ['title', 'result', 'problem', 'steps', 'lesson'];
let materialSequence = 0, materialPreviewData = null;
function materialInput() {
  return {selections: Array.from(state.selected.values(), item => item.selection),
    fields: Object.fromEntries(materialFields.map(key => [key, $('material' + key[0].toUpperCase() + key.slice(1)).value]))};
}
function invalidateMaterial() {
  materialSequence++;
  materialPreviewData = null;
  $('materialOutput').hidden = true;
  $('materialMarkdown').textContent = '';
  $('materialExport').disabled = true;
  $('materialStatus').textContent = '';
  $('materialPreview').disabled = !state.selected.size;
}
function materialError(error) {
  const messages = {material_title_result_required: '请填写素材标题和完成了什么。',
    material_existing_file_changed: '目标素材已被人工修改，未覆盖。请下载这份新预览并人工合并。',
    material_preview_changed: '内容已变化，请重新预览后导出。',
    material_export_not_configured: '尚未配置素材目录，可先下载 Markdown。',
    material_field_limit: '填写内容超过长度上限，请精简后重试。'};
  return messages[error.message] || errorAdvice(error, '操作未完成，请重新预览后重试。');
}
async function previewMaterial() {
  if (!state.selected.size) return;
  invalidateMaterial();
  const sequence = materialSequence;
  $('materialPreview').disabled = true;
  $('materialStatus').textContent = '正在核对证据并生成素材…';
  try {
    const data = await post('/api/reuse/material-preview', materialInput());
    if (sequence !== materialSequence) return;
    if (data.schema_version !== 'history.material.v1' || typeof data.markdown !== 'string') throw new Error('invalid_material');
    materialPreviewData = data;
    $('materialMarkdown').textContent = data.markdown;
    $('materialNotice').textContent = data.redaction_notice;
    $('materialDestination').textContent = data.export_enabled ? `素材目录：${data.destination}` : '未配置素材目录。可下载 Markdown；目录由启动参数 --material-dir 配置。';
    $('materialOutput').hidden = false;
    $('materialExport').disabled = !data.export_enabled;
    $('materialStatus').textContent = '请核对成果、证据和隐私，再导出。这份素材仍需人工核实。';
  } catch (error) {
    if (sequence === materialSequence) $('materialStatus').textContent = materialError(error);
  } finally {
    if (sequence === materialSequence) $('materialPreview').disabled = !state.selected.size;
  }
}
async function exportMaterial() {
  const preview = materialPreviewData, sequence = materialSequence;
  if (!preview || !preview.export_enabled) return;
  $('materialExport').disabled = true;
  $('materialStatus').textContent = '正在核对并保存当前预览…';
  try {
    const result = await post('/api/reuse/material-export', {...materialInput(), revision: preview.revision});
    if (sequence !== materialSequence) return;
    $('materialStatus').textContent = `${result.status === 'already_exists' ? '已存在相同素材，没有重复写入' : '已导出素材'}：${result.filename}。尚未进入公众号草稿箱。`;
  } catch (error) {
    if (sequence === materialSequence) $('materialStatus').textContent = materialError(error);
  } finally {
    if (sequence === materialSequence) $('materialExport').disabled = false;
  }
}
function download(name, contents, type) {
  const url = URL.createObjectURL(new Blob([contents], {type}));
  const anchor = text('a', ''); anchor.href = url; anchor.download = name;
  document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function downloadSelection(format) {
  if (!state.preview) return;
  const name = state.preview.schema_version === 'history.review-packet.v1' ? 'review-handoff' : 'history-evidence';
  download(`${name}.${format}`, format === 'md' ? state.preview.markdown : JSON.stringify(state.preview, null, 2), format === 'md' ? 'text/markdown;charset=utf-8' : 'application/json');
}
async function copySelection() {
  if (!state.preview) return;
  const preview = state.preview;
  try {
    await navigator.clipboard.writeText(preview.markdown);
    if (state.preview === preview) $('selectionStatus').textContent = '已复制 Markdown。';
  } catch {
    if (state.preview === preview) $('selectionStatus').textContent = '浏览器未允许复制。可从预览选择文本，或下载 Markdown。';
  }
}
function sourceLabel(source) {
  return {codebuddy: 'CodeBuddy (cbc)', gemini: 'Gemini CLI', opencode: 'OpenCode',
    codex: 'Codex', claude: 'Claude Code', pi: 'pi', copilot: 'GitHub Copilot', zcode: 'ZCode', prime: 'Prime Agent', agy: 'AGY CLI', mcode: 'MiniMax Code (mcode)', antigravity: 'Antigravity'}[source] || source || '未知来源';
}
function renderHealth(data) {
  if (!Array.isArray(data.sources)) throw new Error('invalid_health');
  const sources = $('sourceStatus'); sources.replaceChildren();
  const labels = {readable: '可读取', ready: '可读取', available: '可读取', empty: '暂无记录', missing: '路径不存在', not_found: '路径不存在', unreadable: '没有读取权限', unsupported: '来源格式暂不支持', indexing: '正在建立索引', unknown: '状态未知', unavailable: '不可用', error: '读取失败', stale: '需要刷新'};
  const nextSteps = {
    empty: '来源暂无已索引记录。已有历史时可刷新此来源；也可用 python3 app.py --demo 体验。',
    missing: '检查已配置的来源路径；更改启动配置后重新启动应用。',
    not_found: '检查已配置的来源路径；更改启动配置后重新启动应用。',
    unreadable: '检查当前用户是否有来源文件和目录的读取权限，再重试。',
    unsupported: '当前来源格式暂不支持。可在原生工具中打开记录，或检查应用版本。',
    indexing: '正在建立索引，请稍后点击“重新检查”；无需重复刷新。',
    unknown: '暂不能确认来源状态。请重新检查；仍失败时查看脱敏诊断。',
    stale: '索引可能落后于历史文件。点击“刷新此来源”后重新搜索。',
    error: '检查来源路径与读取权限，然后重试；仍失败时查看脱敏诊断。',
    unavailable: '来源当前不可用。检查路径与读取权限后重新检查。',
  };
  const chosen = $('reuseSource').value;
  $('reuseSource').replaceChildren();
  const all = text('option', '全部来源'); all.value = ''; $('reuseSource').append(all);
  const seen = new Set();
  for (const source of data.sources) {
    if (!seen.has(source.source)) { const option = text('option', sourceLabel(source.source)); option.value = source.source || ''; $('reuseSource').append(option); seen.add(source.source); }
    const row = text('div', '', 'source-row');
    row.append(text('h3', `${sourceLabel(source.source)} / ${source.error_code === 'not_configured' ? '未加载' : source.status === 'partial' ? '部分可读取' : labels[source.status] || source.status || '状态未知'}`));
    row.append(text('p', `记录数：${Number.isFinite(source.count) ? source.count : '未知'}；上次刷新：${dateLabel(source.last_refreshed_at)}`, 'muted'));
    if (source.path) row.append(text('p', source.path, 'source-path'));
    if (source.error_code) row.append(text('p', `读取状态：${source.error_code}`, 'work-warning'));
    if (source.next_step || nextSteps[source.status]) row.append(text('p', source.next_step || nextSteps[source.status]));
    if (source.command) row.append(text('code', source.command));
    const refresh = button('刷新此来源', async () => {
      refresh.disabled = true;
      const outcome = text('p', '正在刷新此来源…', 'muted'); row.append(outcome);
      try {
        await post('/api/reuse/refresh', {system: source.system || 'linux', source: source.source});
        outcome.textContent = '刷新完成。重新搜索后查看最新记录。';
        invalidatePreview(); renderSelection();
        await loadHealth();
      } catch (error) { outcome.textContent = `刷新失败（${error.message}）。${errorAdvice(error, '请检查路径或权限后重试。')}`; refresh.disabled = false; }
    });
    refresh.disabled = ['indexing', 'unsupported'].includes(source.status) || (source.status === 'not_found' && source.path == null);
    row.append(refresh); sources.append(row);
  }
  if (!data.sources.length) sources.append(text('p', '尚未发现已配置的来源。先运行 python3 app.py --demo 体验，或在启动应用时配置真实来源。', 'work-warning'));
  setSourceFilter(chosen);
  $('demoNotice').hidden = !data.demo;
  const active = data.sources.filter(source => Number(source.count) > 0).length;
  $('workspaceStatus').textContent = `${data.demo ? '演示记录' : '本地历史'}${data.version ? ` ${data.version}` : ''} · ${active} 个来源有记录`;
  state.diagnostic = data.diagnostic && typeof data.diagnostic === 'object' ? data.diagnostic : null;
  state.diagnosticShown = false;
  $('diagnosticPreviewBtn').disabled = !state.diagnostic;
  $('diagnosticPreviewBtn').setAttribute('aria-expanded', 'false');
  $('diagnosticPreview').hidden = true; $('diagnosticText').textContent = '';
}
async function loadHealth() {
  const sequence = ++state.healthSequence;
  $('workspaceStatus').textContent = '正在检查本地来源…';
  try {
    const data = await request('/api/reuse/health');
    if (sequence !== state.healthSequence) return;
    renderHealth(data);
  } catch (error) {
    if (sequence !== state.healthSequence) return;
    $('sourceStatus').replaceChildren(); $('demoNotice').hidden = true;
    state.diagnostic = null; state.diagnosticShown = false;
    $('diagnosticPreviewBtn').disabled = true; $('diagnosticPreviewBtn').setAttribute('aria-expanded', 'false');
    $('diagnosticPreview').hidden = true; $('diagnosticText').textContent = '';
    $('workspaceStatus').textContent = `来源检查失败（${error.message}）。请在设置中重新检查，或确认本地服务仍在运行。`;
  }
}
function previewDiagnostic() {
  if (!state.diagnostic) return;
  state.diagnosticShown = !state.diagnosticShown;
  $('diagnosticPreview').hidden = !state.diagnosticShown;
  $('diagnosticPreviewBtn').setAttribute('aria-expanded', String(state.diagnosticShown));
  $('diagnosticText').textContent = state.diagnosticShown ? JSON.stringify(state.diagnostic, null, 2) : '';
}
$('reuseSearchForm').addEventListener('submit', event => { event.preventDefault(); search(); });
$('searchMore').addEventListener('click', () => search(true));
$('projectsMore').addEventListener('click', () => loadPage('projects', null, true));
$('projectsRetry').addEventListener('click', () => loadPage('projects', {}));
$('timelineMore').addEventListener('click', () => timeline(true));
$('timelineFilter').addEventListener('submit', event => { event.preventDefault(); timeline(); });
$('timelineClear').addEventListener('click', () => { $('timelineFile').value = ''; timeline(); });
$('timelineClose').addEventListener('click', showProjects);
$('casesSearchForm').addEventListener('submit', event => { event.preventDefault(); searchCases(); });
$('casesMore').addEventListener('click', () => searchCases(true));
$('casesRefresh').addEventListener('click', refreshCases);
$('caseCopy').addEventListener('click', copyCase);
$('caseDownload').addEventListener('click', downloadCase);
function activateView(view) {
  return view === 'projects' ? showProjects() : view === 'cases' ? showCases() : setView(view);
}
for (const view of workspaceViews) {
  $(`${view}Tab`).addEventListener('click', () => activateView(view));
  $(`${view}Tab`).addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? workspaceViews[0] : event.key === 'End' ? workspaceViews.at(-1)
      : workspaceViews[(workspaceViews.indexOf(view) + (event.key === 'ArrowLeft' ? -1 : 1) + workspaceViews.length) % workspaceViews.length];
    activateView(next);
    $(`${next}Tab`).focus();
  });
}
for (const key of materialFields) $('material' + key[0].toUpperCase() + key.slice(1)).addEventListener('input', invalidateMaterial);
$('materialPreview').addEventListener('click', previewMaterial);
$('materialExport').addEventListener('click', exportMaterial);
$('materialDownload').addEventListener('click', () => { if (materialPreviewData) download(materialPreviewData.filename, materialPreviewData.markdown, 'text/markdown;charset=utf-8'); });
$('selectionClear').addEventListener('click', () => { invalidatePreview(); state.selected.clear(); renderSelection(); });
$('selectionPreview').addEventListener('click', previewSelection);
$('selectionPurpose').addEventListener('change', () => { setSelectionPurpose($('selectionPurpose').value); if (state.view === 'search') persistSearch(searchParams()); });
$('reviewStart').addEventListener('click', () => { setSelectionPurpose('review'); setView('search'); $('reuseQuery').focus(); });
$('selectionCopy').addEventListener('click', copySelection);
$('selectionDownloadMd').addEventListener('click', () => downloadSelection('md'));
$('selectionDownloadJson').addEventListener('click', () => downloadSelection('json'));
$('workspaceRetry').addEventListener('click', loadHealth);
$('diagnosticPreviewBtn').addEventListener('click', previewDiagnostic);
$('diagnosticDownload').addEventListener('click', () => { if (state.diagnostic && state.diagnosticShown) download('history-diagnostic.json', JSON.stringify(state.diagnostic, null, 2), 'application/json'); });
try {
  const theme = localStorage.getItem('historyViewer.ui.codeTheme');
  if (['light', 'slate', 'warm', 'forest', 'grape', 'dark'].includes(theme)) document.documentElement.setAttribute('data-code-theme', theme);
} catch { /* Storage is optional in local and private browser contexts. */ }
async function bootstrapWorkspace() {
  const target = new URLSearchParams(window.location?.search || '');
  if (target.get('view') === 'cases') {
    $('casesQuery').value = target.get('q') || '';
    $('casesProject').value = target.get('project') || '';
    state.pages.cases.params = {};
    for (const key of ['q', 'project']) if (target.get(key)) state.pages.cases.params[key] = target.get(key);
    clearCaseDetail();
    setView('cases', false);
    $('workspaceStatus').textContent = '问题案例可独立读取；来源状态可在“诊断与帮助”中按需检查。';
    await Promise.all([loadCases(), target.get('case') ? openCase(target.get('case'), {persist: false}) : Promise.resolve()]);
    return;
  }
  await loadHealth();
  if (state.view === 'cases' || state.pages.search.sequence) return;
  if (restoreSearchFromUrl()) {
    setView('search', false);
    if ($('reuseQuery').value.trim()) await search();
  } else await setView('projects', false);
}
if (typeof window !== 'undefined') window.__workspaceTestApi = {previewMaterial, exportMaterial, invalidateMaterial, state, record, facts, matchingMessages, restoreSearchFromUrl, bootstrapWorkspace, fileChanges, rawRecord, hasSourceRevision, errorAdvice, historyLink, searchResult, selectionFor, toggleSelection, renderSelection, previewSelection, copySelection, downloadSelection, renderHealth, loadHealth, previewDiagnostic, search, loadPage, renderPage, setView, showProjects, selectProject, timeline, isStale, loadCases, searchCases, showCases, openCase, refreshCases, copyCase, downloadCase};
renderSelection();
bootstrapWorkspace();
