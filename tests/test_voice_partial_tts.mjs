// Functional contract for streamed voice replies: summary-only autoplay must
// wait for the final durable message instead of enqueueing the whole partial.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const html = fs.readFileSync(path.join(here, '..', 'templates', 'index.html'), 'utf8')
  + '\n' + fs.readFileSync(path.join(here, '..', 'static', 'app-shell.css'), 'utf8')
  + '\n' + fs.readFileSync(path.join(here, '..', 'static', 'app-runtime.js'), 'utf8');
const start = html.indexOf('function completeSentencesForSpeech');
const end = html.indexOf('// Serialized autoplay queue', start);
assert.ok(start >= 0 && end > start, 'voice partial functions not found');
const block = html.slice(start, end);

const progressStart = html.indexOf('const LIVE_PROGRESS_TOOL_LINES');
const progressEnd = html.indexOf('function completeSentencesForSpeech', progressStart);
assert.ok(progressStart >= 0 && progressEnd > progressStart, 'live progress speech selector not found');
const selectLiveProgressSpeech = new Function(
  `${html.slice(progressStart, progressEnd)}\nreturn selectLiveProgressSpeech;`,
)();

const finalAutoplayStart = html.indexOf('function maybeAutoplayMessage');
const finalAutoplayEnd = html.indexOf('// Live-progress speech', finalAutoplayStart);
assert.ok(finalAutoplayStart >= 0 && finalAutoplayEnd > finalAutoplayStart, 'durable reply autoplay function not found');

assert.equal(
  selectLiveProgressSpeech("I’m updating only the guarantee section. I’ll verify the saved page next."),
  "I’ll verify the saved page next.",
  'the newest complete user-facing work sentence should be spoken',
);
assert.equal(
  selectLiveProgressSpeech("I'm reviewing the layout, preserving everything else while the draft is open"),
  "I'm reviewing the layout.",
  'a safe opening clause should survive a clipped progress line',
);
assert.equal(
  selectLiveProgressSpeech('Running: python scripts/rebuild.py --token $SECRET'),
  'Running that now.',
  'technical progress must collapse to an allowlisted phrase',
);
assert.equal(
  selectLiveProgressSpeech("I'm checking the API key at https://example.test now."),
  '',
  'credentials and URLs must never be repeated verbatim or inferred into speech',
);
assert.equal(
  selectLiveProgressSpeech('Atlas is thinking this one through…'),
  '',
  'generic fallback thinking text should remain visual-only',
);
assert.equal(
  selectLiveProgressSpeech("I'm reviewing scratch/private/report.md now."),
  '',
  'relative paths must not be repeated even in an otherwise natural update',
);

const roomStart = html.indexOf('function isProgressVoiceKey');
const roomEnd = html.indexOf('function stopFillerChain', roomStart);
assert.ok(roomStart >= 0 && roomEnd > roomStart, 'regular-chat progress narration not found');
const turnMatchStart = html.indexOf('function voiceChatEventMatchesTurn');
const turnMatchEnd = html.indexOf('function fillerBody', turnMatchStart);
assert.ok(turnMatchStart >= 0 && turnMatchEnd > turnMatchStart, 'Voice Chat turn matcher not found');
const roomBlock = `${html.slice(turnMatchStart, turnMatchEnd)}\n${html.slice(roomStart, roomEnd)}`;
{
  const spoken = [];
  const stopped = [];
  let now = 1000;
  const vc = { open: false };
  const roomState = {
    voicePrefs: { autoplay: true },
    mode: 'solo',
    typing: new Map([['codex', { id: 'task-live' }]]),
    agentsById: { codex: { name: 'Codex' } },
    voiceMessageKey: null,
    voicePendingKey: null,
    voiceEndResolve: null,
  };
  const makeRoomHarness = new Function(
    'vc', 'state', 'voiceEventCanAutoplay', 'selectLiveProgressSpeech',
    'speakMessage', 'stopVoicePlayback', 'performance', 'VC_NARRATE_MAX',
    `${roomBlock}\nreturn vc.onProgress;`,
  );
  const onProgress = makeRoomHarness(
    vc,
    roomState,
    () => true,
    selectLiveProgressSpeech,
    message => { spoken.push(message); return Promise.resolve(); },
    options => stopped.push(options),
    { now: () => now },
    8,
  );

  onProgress('codex', "I'm checking the live draft now.", true, { id: 'task-live' });
  assert.equal(spoken.length, 1, 'regular chat autoplay should narrate a live progress sentence');
  assert.equal(spoken[0].name, 'Codex live update');
  assert.equal(spoken[0].__voiceProgress, true);

  roomState.voiceMessageKey = 'progress:task-live:1:codex:full';
  now += 25;
  onProgress('codex', "I'm checking the live draft now. I'm verifying the audience count now.", true, { id: 'task-live' });
  assert.equal(spoken.length, 2, 'the next progress sentence should start immediately');
  assert.equal(spoken[1].text, "I'm verifying the audience count now.", 'a cumulative snapshot should select its newest sentence');
  assert.equal(stopped.length, 1, 'the next progress sentence should interrupt the previous one');

  roomState.voiceMessageKey = 'manual-message:codex:full';
  roomState.voiceEndResolve = () => {};
  now += 1000;
  onProgress('codex', "I'm checking one more setting.", true, { id: 'task-live' });
  assert.equal(spoken.length, 2, 'progress must not interrupt unrelated manual or prior-reply audio');
}

{
  const spoken = [];
  let now = 1000;
  const vc = {
    open: true, awaitingReply: true, speaking: false, agentId: 'codex',
    fillerToken: 4, _nSaid: 0, _nLastAt: 0, _nLastLine: '',
  };
  const roomState = {
    voiceConfig: { server_speech: true },
    voicePrefs: { autoplay: true },
    mode: 'solo',
    typing: new Map([['codex', { id: 'task-live' }]]),
    agentsById: { codex: { name: 'Codex' } },
    voiceMessageKey: null,
    voicePendingKey: null,
    voiceEndResolve: null,
  };
  const makeLiveHarness = new Function(
    'vc', 'state', 'voiceEventCanAutoplay', 'selectLiveProgressSpeech',
    'speakMessage', 'stopVoicePlayback', 'performance', 'VC_NARRATE_MAX', 'vcSay',
    `${roomBlock}\nreturn vc.onProgress;`,
  );
  const onProgress = makeLiveHarness(
    vc,
    roomState,
    () => true,
    selectLiveProgressSpeech,
    () => Promise.resolve(),
    () => {},
    { now: () => now },
    8,
    line => {
      spoken.push(line);
      vc._nLastLine = line;
      vc._nLastAt = now;
    },
  );

  onProgress('codex', "I'm checking the live draft now.", true, { id: 'task-live' });
  assert.deepEqual(spoken, ["I'm checking the live draft now."], 'Chat Live should narrate the first safe sentence');

  now += 25;
  onProgress('codex', "I'm checking the live draft now. I'm verifying the audience count now.", true, { id: 'task-live' });
  assert.deepEqual(
    spoken,
    ["I'm checking the live draft now.", "I'm verifying the audience count now."],
    'Chat Live should replace an older spoken sentence with the newest cumulative sentence',
  );
}

{
  const queued = [];
  const state = {
    activeChatId: 'chat-1', mode: 'solo', voiceReplyStreams: new Map(),
    voiceAutoplaySeen: new Set(),
  };
  const factory = new Function(
    'state', 'voiceEventCanAutoplay', 'cleanTextForSpeech', 'enqueueSpeech',
    `${html.slice(finalAutoplayStart, finalAutoplayEnd)}\nreturn maybeAutoplayMessage;`,
  );
  const autoplay = factory(state, () => true, text => text, message => queued.push(message));
  const finalMessage = {
    id: 'task-final', chat_id: 'chat-1', role: 'agent', agent_id: 'codex',
    text: 'The complete reply is ready.', ts: 100,
  };
  autoplay(finalMessage);
  autoplay(finalMessage);
  assert.equal(queued.length, 1, 'a duplicate durable SSE reply must enter autoplay only once');
}

function makeHarness(autoplayFull) {
  const queued = [];
  const state = {
    mode: 'solo',
    voicePrefs: { autoplayFull },
    voiceReplyStreams: new Map(),
    agentsById: { atlas: { name: 'Atlas' }, codex: { name: 'Codex' } },
  };
  const factory = new Function(
    'state', 'voiceEventCanAutoplay', 'cleanTextForSpeech', 'enqueueSpeech', 'voiceTimingMark',
    `function isStaleReplyPartial(){ return false; }\n${block}\nreturn maybeSpeakPartialReply;`,
  );
  const speakPartial = factory(
    state,
    () => true,
    (text) => String(text || '').replace(/\s+/g, ' ').trim(),
    (message) => queued.push(message),
    () => {},
  );
  return { queued, state, speakPartial };
}

{
  const h = makeHarness(true);
  h.speakPartial({
    id: 'task-codex', agent_id: 'codex',
    text: "I'm checking the draft now. I'm updating the layout next.",
  });
  assert.equal(h.queued.length, 0, 'Codex work commentary must not also enter the final-answer queue');
  assert.equal(h.state.voiceReplyStreams.size, 0, 'Codex waits for one durable final reply');
}

{
  const h = makeHarness(false);
  h.speakPartial({ id: 'task-1', agent_id: 'atlas', text: 'First sentence. Second sentence! trailing' });
  assert.equal(h.queued.length, 0, 'summary mode must not stream full partial replies');
  assert.equal(h.state.voiceReplyStreams.size, 0, 'summary mode must leave final-message fallback untouched');
}

{
  const h = makeHarness(true);
  h.speakPartial({ id: 'task-2', agent_id: 'atlas', text: 'First sentence. Second sentence! trailing' });
  assert.equal(h.queued.length, 2, 'full-reply mode should stream completed sentences');
  assert.deepEqual(h.queued.map((m) => m.text), ['First sentence.', 'Second sentence!']);
  assert.equal(h.state.voiceReplyStreams.get('task-2').emitted, 2);
}

console.log('✓ live thought TTS preempts and durable replies play once');
