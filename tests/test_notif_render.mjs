// Executes the REAL bell render functions (from the index.html <notif-render>
// block) against seeded notifications, in a Node sandbox with the few leaf deps
// stubbed. This catches render-time errors (a ReferenceError that node --check
// can't see) and asserts the actual DOM output for the Activity center's workflow
// views: Needs you, unread Updates, and handled History. Run:
// `node tests/test_notif_render.mjs`.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'templates', 'index.html'), 'utf8')
  + '\n' + readFileSync(join(__dirname, '..', 'static', 'app-shell.css'), 'utf8')
  + '\n' + readFileSync(join(__dirname, '..', 'static', 'app-runtime.js'), 'utf8');
const grab = (tag) => {
  const m = html.match(new RegExp(`// <${tag}>[\\s\\S]*?// </${tag}>`));
  assert.ok(m, `${tag} block not found in index.html`);
  return m[0];
};

// Fake DOM + leaf-dep stubs the render block leans on.
const host = { innerHTML: '' };
const harness = `
  const escapeHtml = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const _notifIconHtml = (n) => '[ico]';
  const _notifDot = (sev) => '<span class="notif-dot" data-d="' + sev + '"></span>';
  const notifRelTime = (ts) => '2m ago';
  let _notifFilter = 'needs';
  let _notifSelected = '';
  const _notifExpanded = new Set();
  function renderNotifFocus() {}
  function renderNotifDetail() {}
  const window = { lucide: { createIcons() {} } };
  const document = { getElementById: (id) => (id === 'notifList' ? host : null) };
  ${grab('notif-logic')}
  ${grab('notif-render')}
  // expose for the test driver
  globalThis.__api = {
    renderNotifList,
    _notifExpanded,
    setFilter: (f) => { _notifFilter = f; _notifSelected = ''; },
  };
`;
const ctx = { host, globalThis: undefined, console };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(harness, ctx);
const api = ctx.__api;

// Seed: 2 holds (needs you) in chat 5; 3 completions in chat 7; 1 completion in chat 9.
const state = {
  notifs: [
    { id: 'h1', chat_id: '5', chat_title: 'Tax filing', title: 'Lead is waiting on you', body: 'Send now or hold?', event: 'gated_hold', severity: 'warn', ts: 9000, read: false },
    { id: 'h2', chat_id: '5', chat_title: 'Tax filing', title: 'Run failed', body: 'KeyError', event: 'attention', severity: 'error', ts: 8000, read: false },
    { id: 'c1', chat_id: '7', chat_title: 'Analyze video', title: 'Claude finished', body: 'reply one', event: 'agent_done', severity: 'success', ts: 7000, read: false },
    { id: 'c2', chat_id: '7', chat_title: 'Analyze video', title: 'Lead finished', body: 'reply two', event: 'agent_done', severity: 'success', ts: 6000, read: true },
    { id: 'c3', chat_id: '7', chat_title: 'Analyze video', title: 'Lead finished', body: 'reply three', event: 'agent_done', severity: 'success', ts: 5000, read: false },
    { id: 's1', chat_id: '9', chat_title: 'Blog post', title: 'Agents finished', body: 'done', event: 'agent_done', severity: 'success', ts: 4000, read: false },
  ],
};
ctx.state = state;   // render block reads global `state`

let passed = 0;
const failures = [];
const test = (name, fn) => { try { fn(); passed++; } catch (e) { failures.push(`${name}: ${e.message}`); } };

test('renders without throwing and produces markup', () => {
  api.renderNotifList();
  assert.ok(host.innerHTML.length > 0);
});
test('Needs you view contains only the two actionable rows', () => {
  assert.match(host.innerHTML, /notif-section-hd needs/);
  assert.match(host.innerHTML, /Prioritized/);
  assert.ok(host.innerHTML.includes('Lead is waiting on you'));
  assert.ok(host.innerHTML.includes('Run failed'));
  assert.ok(!host.innerHTML.includes('Claude finished'));
});
test('Updates view groups only unread routine activity', () => {
  api.setFilter('updates');
  api.renderNotifList();
  assert.match(host.innerHTML, /notif-group/);
  assert.match(host.innerHTML, /2 events/);
  assert.match(host.innerHTML, /2 unread/);
  assert.ok(!host.innerHTML.includes('reply two'), 'read activity is reserved for History');
});
test('collapsed group previews the latest reply but hides the rest', () => {
  assert.ok(host.innerHTML.includes('reply one'), 'latest reply shows as the group preview');
  assert.ok(!host.innerHTML.includes('notif-children'), 'no expanded children container');
  assert.ok(!host.innerHTML.includes('reply three'), 'older children stay hidden when collapsed');
});
test('singleton chat 9 renders as a plain row, not a group', () => {
  assert.ok(host.innerHTML.includes('Agents finished'));
  assert.match(host.innerHTML, /Unread updates/);
});
test('expanding chat 7 reveals its children as nested rows', () => {
  api._notifExpanded.add('c7');   // update-group key is 'c' + chat_id
  api.renderNotifList();
  assert.match(host.innerHTML, /notif-children/);
  assert.ok(host.innerHTML.includes('reply one'));
  assert.ok(host.innerHTML.includes('reply three'));
});
test('re-firing cap alert collapses to one event bundle across chats', () => {
  api._notifExpanded.clear();
  api.setFilter('updates');
  state.notifs = [
    { id: 'k1', chat_id: '3559', chat_title: 'hi', title: 'Codex: usage limit', body: 'ChatGPT usage limit reached', event: 'attention', severity: 'warn', ts: 9000, read: false },
    { id: 'k2', chat_id: '3559', chat_title: 'hi', title: 'Codex: usage limit', body: 'ChatGPT usage limit reached', event: 'attention', severity: 'warn', ts: 8000, read: false },
    { id: 'k3', chat_id: '3559', chat_title: 'hi', title: 'Codex: usage limit', body: 'ChatGPT usage limit reached', event: 'attention', severity: 'warn', ts: 7000, read: false },
    { id: 'k4', chat_id: '3559', chat_title: 'hi', title: 'Codex: usage limit', body: 'ChatGPT usage limit reached', event: 'attention', severity: 'warn', ts: 6000, read: false },
    { id: 'corr', chat_id: '40', chat_title: 'Executive strategy', title: 'Codex Limit Correction', body: 'I did not reset', event: 'attention', severity: 'warn', ts: 5500, read: false },
  ];
  api.renderNotifList();
  // 4 identical alerts → one routine-monitoring bundle. The correction remains
  // a separate attention item in Needs you instead of being swallowed.
  assert.match(host.innerHTML, /4 events/);
  assert.ok(!host.innerHTML.includes('Codex Limit Correction'));
  // The 4 copies are NOT rendered as 4 separate rows while collapsed.
  assert.ok(!host.innerHTML.includes('notif-children'), 'collapsed by default');
  api.setFilter('needs');
  api.renderNotifList();
  assert.ok(host.innerHTML.includes('Codex Limit Correction'), 'distinct attention alert not swallowed');
});
test('History view contains read routine activity and excludes unread activity', () => {
  state.notifs = [
    { id: 'c1', chat_id: '7', chat_title: 'Analyze video', title: 'Claude finished', body: 'reply one', event: 'agent_done', severity: 'success', ts: 7000, read: false },
    { id: 'c2', chat_id: '7', chat_title: 'Analyze video', title: 'Lead finished', body: 'reply two', event: 'agent_done', severity: 'success', ts: 6000, read: true },
    { id: 'c3', chat_id: '7', chat_title: 'Analyze video', title: 'Lead finished', body: 'reply three', event: 'agent_done', severity: 'success', ts: 5000, read: false },
  ];
  api._notifExpanded.clear();
  api.setFilter('history');
  api.renderNotifList();
  assert.match(host.innerHTML, /Recently handled/);
  assert.ok(host.innerHTML.includes('reply two'));
  assert.ok(!host.innerHTML.includes('reply one'));
  assert.ok(!host.innerHTML.includes('reply three'));
});

if (failures.length) {
  console.error(`\n✗ ${failures.length} failed, ${passed} passed:\n  ` + failures.join('\n  '));
  process.exit(1);
}
console.log(`✓ all ${passed} notif-render tests passed`);
