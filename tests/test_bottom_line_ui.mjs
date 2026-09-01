import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';


const indexSource = readFileSync(new URL('../templates/index.html', import.meta.url), 'utf8')
  + '\n' + readFileSync(new URL('../static/app-shell.css', import.meta.url), 'utf8')
  + '\n' + readFileSync(new URL('../static/app-runtime.js', import.meta.url), 'utf8');


function extractFunctionSource(name, nextName) {
  const start = indexSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name} in templates/index.html`);
  const end = indexSource.indexOf(`function ${nextName}(`, start);
  assert.notEqual(end, -1, `missing ${nextName} after ${name}`);
  const segment = indexSource.slice(start, end);
  // Comments often describe the next helper between declarations. Trim at this
  // function's last script-indented closing brace so a trailing `//` cannot
  // swallow the `);` added by extractFunction below.
  const close = segment.lastIndexOf('\n  }');
  assert.notEqual(close, -1, `missing closing brace for ${name}`);
  return segment.slice(0, close + 4).trim();
}


function extractFunction(name, nextName, bindings = {}) {
  const declaration = extractFunctionSource(name, nextName);
  const names = Object.keys(bindings);
  return Function(...names, `"use strict"; return (${declaration});`)(
    ...names.map(name => bindings[name]),
  );
}


const splitAuthoredBottomLine = extractFunction(
  'splitAuthoredBottomLine',
  'renderAuthoredBottomLine',
);


test('parses an inline authored bottom line and preserves the body', () => {
  assert.deepEqual(
    splitAuthoredBottomLine(
      'BOTTOM LINE: Ship the repair.\n\nReader, the opener stays visible.\n\nThe deeper detail follows.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, the opener stays visible.\n\nThe deeper detail follows.',
    },
  );
});


test('parses a bottom line placed on the line after its marker', () => {
  assert.deepEqual(
    splitAuthoredBottomLine(
      'BOTTOM LINE:\nShip the repair.\n\nReader, the opener stays visible.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, the opener stays visible.',
    },
  );
  assert.deepEqual(
    splitAuthoredBottomLine(
      'BOTTOM LINE:\n\nShip the repair.\n\nReader, the opener stays visible.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, the opener stays visible.',
    },
  );
});


test('parses bold and Markdown-heading marker variants', () => {
  assert.deepEqual(
    splitAuthoredBottomLine(
      '**BOTTOM LINE:**\n\nShip the repair.\n\nReader, the opener stays visible.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, the opener stays visible.',
    },
  );
  assert.deepEqual(
    splitAuthoredBottomLine(
      '## Bottom Line\n\nShip the repair.\n\nReader, the opener stays visible.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, the opener stays visible.',
    },
  );
});


test('promotes an exact marker found later while preserving the surrounding reply', () => {
  assert.deepEqual(
    splitAuthoredBottomLine(
      'Reader, this opener came first.\n\nBOTTOM LINE: Ship the repair.\n\nThe deeper detail remains.',
    ),
    {
      text: 'Ship the repair.',
      body: 'Reader, this opener came first.\n\nThe deeper detail remains.',
    },
  );
});


test('rejects ordinary words that merely begin with bottom line', () => {
  assert.equal(
    splitAuthoredBottomLine('BOTTOM LINEAGE is not a marker.'),
    null,
  );
  assert.equal(
    splitAuthoredBottomLine('Bottom lines are useful writing tools.'),
    null,
  );
});


test('uses structured metadata as a markerless fallback without consuming the body', () => {
  const rawReply = 'Owner, this provider returned the reply without a textual marker.';
  assert.deepEqual(
    splitAuthoredBottomLine(rawReply, 'Ship the repair.'),
    {
      text: 'Ship the repair.',
      body: rawReply,
    },
  );
});


test('renders exactly one bottom-line card before the reply body', () => {
  const renderOne = extractFunction('renderOne', 'messageToolsHtml', {
    state: {
      agentsById: {
        codex: { id: 'codex', name: 'Codex', color: '#5577ff' },
      },
    },
    splitAuthoredBottomLine,
    enhancedBottomLineData: message => message.bottomLine || null,
    renderBottomLine: message => (
      message.bottomLineLoading ? '[[LOADING]]' : '[[ENHANCED]]'
    ),
    renderAuthoredBottomLine: bottom => (
      bottom ? `[[AUTHORED:${bottom.text}]]` : ''
    ),
    renderCollapsibleBody: message => `[[BODY:${message.text}]]`,
    renderAttachments: () => '',
    renderArtifactChips: () => '',
    renderCanvasPromote: () => '',
    renderTurnMetadata: () => '',
    agentBadgeHtml: () => 'C',
    agentHasDesktopSurface: () => false,
    messageOverflowHtml: () => '',
    messageToolsHtml: () => '',
    escapeHtml: value => String(value ?? ''),
    formatTime: () => 'now',
  });
  const message = {
    role: 'agent',
    agent_id: 'codex',
    name: 'Codex',
    text: 'Owner, markerless body.',
    ts: 7,
    metadata: { bottom_line: 'Ship the repair.' },
  };

  const authored = renderOne(message, 0);
  assert.match(authored, /\[\[AUTHORED:Ship the repair\.\]\]/);
  assert.doesNotMatch(authored, /\[\[ENHANCED\]\]/);
  assert.ok(
    authored.indexOf('[[AUTHORED:') < authored.indexOf('[[BODY:'),
    'authored bottom line must render before the body',
  );

  const enhanced = renderOne(
    { ...message, bottomLine: { text: 'Enhanced takeaway.' } },
    0,
  );
  assert.match(enhanced, /\[\[ENHANCED\]\]/);
  assert.doesNotMatch(enhanced, /\[\[AUTHORED:/);
  assert.ok(
    enhanced.indexOf('[[ENHANCED]]') < enhanced.indexOf('[[BODY:'),
    'enhanced bottom line must replace the authored card and render first',
  );
});


test('automation digest turns keep the same first-card and enhancement contract', () => {
  const message = {
    role: 'agent',
    agent_id: 'codex',
    name: 'Codex',
    text: 'Owner, automation detail.',
    ts: 11,
    metadata: { bottom_line: 'Automation needs attention.' },
    attachments: [{ id: 'proof' }],
    artifacts: [{ id: 'chart' }],
  };
  const state = { thread: [message] };
  const renderCollapsibleBody = extractFunction(
    'renderCollapsibleBody',
    'renderTurnMetadata',
    {
      COLLAPSE_MIN_CHARS: 600,
      COLLAPSE_MIN_REST: 160,
      COLLAPSE_MIN_HEAD: 280,
      expandedMessages: new Set(),
      state,
      renderMarkdown: text => String(text ?? ''),
      escapeHtml: value => String(value ?? ''),
    },
  );
  const renderPulseTurn = extractFunction('renderPulseTurn', 'ensurePulseRuns', {
    state,
    splitAuthoredBottomLine,
    enhancedBottomLineData: item => item.bottomLine || null,
    renderBottomLine: item => (
      item.bottomLineLoading ? '[[LOADING]]' : '[[ENHANCED]]'
    ),
    renderAuthoredBottomLine: bottom => (
      bottom ? `[[AUTHORED:${bottom.text}]]` : ''
    ),
    renderCollapsibleBody,
    renderAttachments: () => '[[ATTACHMENTS]]',
    renderArtifactChips: () => '[[ARTIFACTS]]',
    escapeHtml: value => String(value ?? ''),
    formatTime: () => 'now',
  });

  const authored = renderPulseTurn(message);
  assert.match(authored, /\[\[AUTHORED:Automation needs attention\.\]\]/);
  assert.doesNotMatch(authored, /\[\[ENHANCED\]\]/);
  assert.match(authored, /data-act="bottomline" data-i="0"/);
  assert.match(authored, /\[\[ATTACHMENTS\]\]/);
  assert.match(authored, /\[\[ARTIFACTS\]\]/);
  assert.ok(
    authored.indexOf('[[AUTHORED:') < authored.indexOf('Owner, automation detail.'),
    'automation bottom line must render before its body',
  );

  message.bottomLine = { text: 'Enhanced automation takeaway.' };
  const enhanced = renderPulseTurn(message);
  assert.match(enhanced, /\[\[ENHANCED\]\]/);
  assert.doesNotMatch(enhanced, /\[\[AUTHORED:/);
  assert.ok(
    enhanced.indexOf('[[ENHANCED]]') < enhanced.indexOf('Owner, automation detail.'),
    'enhanced automation bottom line must replace the authored card and render first',
  );

  const longTail = `${'Useful detail. '.repeat(140)}TAIL_SENTINEL`;
  const longMessage = {
    ...message,
    bottomLine: null,
    text: `BOTTOM LINE: Keep the whole automation reply.\n\n${'Opening paragraph. '.repeat(20).trim()}\n\n${longTail}`,
    ts: 12,
    metadata: {},
  };
  state.thread.push(longMessage);
  state.thread.push({ ...message, ts: 13, text: 'later turn' });
  const collapsed = renderPulseTurn(longMessage);
  assert.match(collapsed, /data-act="toggle-more"/);
  assert.match(collapsed, /data-more-body hidden/);
  assert.match(collapsed, /TAIL_SENTINEL/);
  assert.ok(
    collapsed.indexOf('[[AUTHORED:') < collapsed.indexOf('Opening paragraph.'),
    'automation collapse must retain the first-card-first order',
  );
});


test('collapsed automation headline uses the parsed bottom line, not its marker', () => {
  const digestPreview = extractFunction('digestPreview', 'renderPulseTurn');
  const renderPulseRow = extractFunction('renderPulseRow', 'renderPulseFix', {
    pulseRunOpen: new Set(),
    pulseHealth: () => ({ color: '#22c55e', icon: '' }),
    splitAuthoredBottomLine,
    digestPreview,
    renderPulseTurn: () => '',
    renderPulseFix: () => '',
    escapeHtml: value => String(value ?? ''),
    formatTime: () => 'now',
  });
  const message = {
    role: 'agent',
    text: 'BOTTOM LINE: Ship the repair.\n\nThe automation detail follows.',
    metadata: {},
  };
  const rendered = renderPulseRow(
    'schedule-1',
    {
      rid: 12,
      ts: 12,
      text: message.text,
      msgs: [message],
      artifacts: [],
    },
    null,
  );

  assert.match(rendered, />Ship the repair\.<\/span>/);
  assert.doesNotMatch(rendered, /BOTTOM LINE/);

  const cached = renderPulseRow(
    'schedule-1',
    {
      rid: 12,
      ts: 12,
      text: message.text,
      msgs: [message],
      artifacts: [],
    },
    {
      byKey: {
        12: {
          headline: 'BOTTOM LINE: Cached automation needs attention.\n\nCached detail.',
          health: 'amber',
        },
      },
    },
  );
  assert.match(cached, />Cached automation needs attention\.<\/span>/);
  assert.doesNotMatch(cached, /BOTTOM LINE/);
  assert.doesNotMatch(cached, /Cached detail/);
});
