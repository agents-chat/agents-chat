// Unit tests for the pure notification-bell logic (tiering, grouping, dedup).
//
// The functions under test live inline in templates/index.html between the
// `// <notif-logic>` … `// </notif-logic>` markers. We extract that exact block
// and run it under Node so the logic that decides "needs you vs update", groups
// a busy chat into one row, and collapses rapid repeats is tested for real —
// without a browser. Run: `node tests/test_notif_logic.mjs`.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import assert from 'node:assert/strict';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '..', 'templates', 'index.html'), 'utf8')
  + '\n' + readFileSync(join(__dirname, '..', 'static', 'app-shell.css'), 'utf8')
  + '\n' + readFileSync(join(__dirname, '..', 'static', 'app-runtime.js'), 'utf8');
const block = html.match(/\/\/ <notif-logic>[\s\S]*?\/\/ <\/notif-logic>/);
assert.ok(block, 'notif-logic block not found in templates/index.html');

const factory = new Function(
  block[0] + '\nreturn { notifNeedsYou, notifIsCap, notifTypeOf, groupNotifsForDisplay, notifDisplayBuckets, notifRecordDedup };'
);
const { notifNeedsYou, notifIsCap, notifTypeOf, groupNotifsForDisplay, notifDisplayBuckets, notifRecordDedup } = factory();

let passed = 0;
const failures = [];
function test(name, fn) {
  try { fn(); passed++; }
  catch (e) { failures.push(`${name}: ${e.message}`); }
}

const N = (o) => Object.assign(
  { id: 'i' + Math.random().toString(36).slice(2), chat_id: '1', chat_title: 'Chat',
    title: 't', body: 'b', event: 'agent_done', severity: 'info', ts: 1000, read: false },
  o
);

// ---- tiering: notifNeedsYou ----
test('needsYou true for hold/ask/approval events', () => {
  assert.equal(notifNeedsYou(N({ event: 'attention' })), true);
  assert.equal(notifNeedsYou(N({ event: 'gated_hold' })), true);
  assert.equal(notifNeedsYou(N({ event: 'gated_automation' })), true);
});
test('needsYou true for warn/error/critical severity regardless of event', () => {
  assert.equal(notifNeedsYou(N({ event: 'automation_done', severity: 'warn' })), true);
  assert.equal(notifNeedsYou(N({ event: 'agent_done', severity: 'error' })), true);
  assert.equal(notifNeedsYou(N({ severity: 'CRITICAL' })), true);  // case-insensitive
});
test('needsYou false for routine completions', () => {
  assert.equal(notifNeedsYou(N({ event: 'agent_done', severity: 'success' })), false);
  assert.equal(notifNeedsYou(N({ event: 'automation_done', severity: 'info' })), false);
  assert.equal(notifNeedsYou(null), false);
});

// ---- classification: notifTypeOf ----
test('typeOf maps events to coarse classes', () => {
  assert.equal(notifTypeOf(N({ event: 'attention' })), 'attention');
  assert.equal(notifTypeOf(N({ event: 'gated_hold' })), 'attention');
  assert.equal(notifTypeOf(N({ event: 'automation_done' })), 'automation');
  assert.equal(notifTypeOf(N({ event: 'agent_done' })), 'completion');
  assert.equal(notifTypeOf(N({ event: '' })), 'completion');
});

// ---- grouping: groupNotifsForDisplay ----
test('needs-you split from updates; both return group objects', () => {
  const list = [
    N({ id: 'a', chat_id: '5', event: 'gated_hold', severity: 'warn', title: 'Send draft?' }),
    N({ id: 'b', chat_id: '5', event: 'attention', severity: 'critical', title: 'Run failed' }),
    N({ id: 'c', chat_id: '5', event: 'agent_done' }),
  ];
  const { needsYou, groups } = groupNotifsForDisplay(list);
  assert.equal(needsYou.length, 2, 'two distinct needs-you (different titles)');
  assert.equal(needsYou[0].kind, 'needs');
  assert.equal(groups.length, 1, 'one update group for chat 5');
  assert.equal(groups[0].kind, 'update');
  assert.equal(groups[0].items.length, 1);
});
test('needs-you collapses identical repeats (chat+title), keeps distinct separate', () => {
  const list = [
    N({ id: 'a', chat_id: '5', event: 'attention', severity: 'warn', title: 'Lead needs you' }),
    N({ id: 'b', chat_id: '5', event: 'attention', severity: 'warn', title: 'Lead needs you' }),
    N({ id: 'c', chat_id: '5', event: 'attention', severity: 'warn', title: 'Lead needs you' }),
    N({ id: 'd', chat_id: '5', event: 'gated_hold', severity: 'warn', title: 'Send draft?' }),
  ];
  const { needsYou } = groupNotifsForDisplay(list);
  assert.equal(needsYou.length, 2, '3 identical alerts fold to 1; the distinct hold stays its own row');
  assert.ok(needsYou.find(g => g.items.length === 3), 'the three identical alerts grouped together');
});

// ---- caps (agent usage-limit health) ----
test('notifIsCap detects cap by event or stable title/body', () => {
  assert.equal(notifIsCap(N({ event: 'cap' })), true);
  assert.equal(notifIsCap(N({ event: 'attention', title: 'Codex: usage limit' })), true);
  assert.equal(notifIsCap(N({ event: 'attention', title: 'whatever', body: 'ChatGPT usage limit reached — resets at 4' })), true);
  assert.equal(notifIsCap(N({ event: 'attention', title: 'Lead has a question' })), false);
});
test('caps are NOT needs-you (they auto-recover)', () => {
  assert.equal(notifNeedsYou(N({ event: 'attention', severity: 'warn', title: 'Codex: usage limit' })), false);
});
test('a genuine info-severity question is STILL needs-you (not demoted)', () => {
  assert.equal(notifNeedsYou(N({ event: 'attention', severity: 'info', title: 'Lead has a question' })), true);
});
test('a success note is not needs-you (e.g. resolved "limit reset")', () => {
  assert.equal(notifNeedsYou(N({ event: 'attention', severity: 'success', title: 'Codex Limit Reset' })), false);
});
test('caps collapse GLOBALLY across chats into one update-tier group', () => {
  const list = [
    N({ id: 'a', chat_id: '3559', agent_id: 'codex', event: 'attention', severity: 'warn', title: 'Codex: usage limit' }),
    N({ id: 'b', chat_id: '3585', agent_id: 'codex', event: 'attention', severity: 'warn', title: 'Codex: usage limit' }),
    N({ id: 'c', chat_id: '40', agent_id: 'codex', event: 'attention', severity: 'warn', title: 'Codex: usage limit' }),
  ];
  const { needsYou, groups } = groupNotifsForDisplay(list);
  assert.equal(needsYou.length, 0, 'caps never land in Needs you');
  assert.equal(groups.length, 1, 'three chats, one global cap row');
  assert.equal(groups[0].kind, 'cap');
  assert.equal(groups[0].items.length, 3);
});
test('same needs-you title in different chats is NOT merged', () => {
  const list = [
    N({ id: 'a', chat_id: '5', event: 'attention', severity: 'error', title: 'Run failed' }),
    N({ id: 'b', chat_id: '9', event: 'attention', severity: 'error', title: 'Run failed' }),
  ];
  assert.equal(groupNotifsForDisplay(list).needsYou.length, 2);
});
test('updates grouped by chat with correct counts + unread + latest ts', () => {
  const list = [
    N({ id: 'a', chat_id: '5', ts: 3000, read: false }),
    N({ id: 'b', chat_id: '5', ts: 2000, read: true }),
    N({ id: 'c', chat_id: '5', ts: 1000, read: false }),
    N({ id: 'd', chat_id: '9', ts: 2500, read: false }),
  ];
  const { groups } = groupNotifsForDisplay(list);
  assert.equal(groups.length, 2);
  const g5 = groups.find(g => g.chat_id === '5');
  assert.equal(g5.items.length, 3);
  assert.equal(g5.unread, 2);
  assert.equal(g5.ts, 3000, 'group ts is the newest member');
});
test('group order follows newest-first first-seen input order', () => {
  const list = [N({ id: 'a', chat_id: '9' }), N({ id: 'b', chat_id: '5' })];
  const { groups } = groupNotifsForDisplay(list);
  assert.deepEqual(groups.map(g => g.chat_id), ['9', '5']);
});
test('chatless notifs each get their own singleton bucket', () => {
  const list = [N({ id: 'a', chat_id: '' }), N({ id: 'b', chat_id: '' })];
  const { groups } = groupNotifsForDisplay(list);
  assert.equal(groups.length, 2, 'not merged into one blind bucket');
});
test('empty / nullish input is safe', () => {
  assert.deepEqual(groupNotifsForDisplay(null), { needsYou: [], groups: [] });
  assert.deepEqual(groupNotifsForDisplay([]), { needsYou: [], groups: [] });
});

// ---- workflow views: Needs you / Updates / History ----
test('display buckets separate attention, unread routine, and handled routine', () => {
  const list = [
    N({ id: 'need-read', chat_id: '5', event: 'gated_hold', severity: 'warn', read: true }),
    N({ id: 'update', chat_id: '7', event: 'agent_done', severity: 'success', read: false }),
    N({ id: 'history', chat_id: '9', event: 'automation_done', severity: 'success', read: true }),
  ];
  const { needsYou, updates, history } = notifDisplayBuckets(list);
  assert.deepEqual(needsYou.flatMap(g => g.items.map(n => n.id)), ['need-read'], 'attention stays visible until dismissed');
  assert.deepEqual(updates.flatMap(g => g.items.map(n => n.id)), ['update']);
  assert.deepEqual(history.flatMap(g => g.items.map(n => n.id)), ['history']);
});
test('display buckets are null-safe', () => {
  assert.deepEqual(notifDisplayBuckets(null), { needsYou: [], updates: [], history: [] });
});

// ---- dedup: notifRecordDedup ----
test('same chat + same routine event within window merges in place', () => {
  const list = [N({ id: 'old', chat_id: '5', event: 'agent_done', ts: 1000, read: true, body: 'old' })];
  const { list: out, merged } = notifRecordDedup(list, N({ id: 'new', chat_id: '5', event: 'agent_done', ts: 1020, body: 'new' }), 45000);
  assert.equal(merged, true);
  assert.equal(out.length, 1, 'no new row stacked');
  assert.equal(out[0].id, 'new');
  assert.equal(out[0].body, 'new');
  assert.equal(out[0].ts, 1020);
  assert.equal(out[0].read, false, 'merged row reverts to unread');
});
test('different event does not merge', () => {
  const list = [N({ chat_id: '5', event: 'agent_done', ts: 1000 })];
  const { merged } = notifRecordDedup(list, N({ chat_id: '5', event: 'automation_done', ts: 1010 }), 45000);
  assert.equal(merged, false);
});
test('outside the window does not merge', () => {
  const list = [N({ chat_id: '5', event: 'agent_done', ts: 1000 })];
  const { list: out, merged } = notifRecordDedup(list, N({ chat_id: '5', event: 'agent_done', ts: 99000 }), 45000);
  assert.equal(merged, false);
  assert.equal(out.length, 2);
});
test('needs-you incoming is never merged', () => {
  const list = [N({ chat_id: '5', event: 'attention', severity: 'warn', ts: 1000 })];
  const { merged } = notifRecordDedup(list, N({ chat_id: '5', event: 'attention', severity: 'warn', ts: 1010 }), 45000);
  assert.equal(merged, false, 'two holds must both stay');
});
test('chatless incoming is never merged', () => {
  const list = [N({ chat_id: '', event: 'agent_done', ts: 1000 })];
  const { merged } = notifRecordDedup(list, N({ chat_id: '', event: 'agent_done', ts: 1010 }), 45000);
  assert.equal(merged, false);
});
test('empty list just prepends', () => {
  const { list: out, merged } = notifRecordDedup([], N({ id: 'first' }), 45000);
  assert.equal(merged, false);
  assert.equal(out.length, 1);
  assert.equal(out[0].id, 'first');
});

if (failures.length) {
  console.error(`\n✗ ${failures.length} failed, ${passed} passed:\n  ` + failures.join('\n  '));
  process.exit(1);
}
console.log(`✓ all ${passed} notif-logic tests passed`);
