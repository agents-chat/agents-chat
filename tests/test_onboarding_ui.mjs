import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';


const source = readFileSync(
  new URL('../static/app-runtime.js', import.meta.url), 'utf8',
);


function extractFunction(name, bindings = {}) {
  const sig = source.indexOf(`function ${name}(`);
  assert.notEqual(sig, -1, `missing ${name}`);
  const start = source.slice(sig - 6, sig) === 'async ' ? sig - 6 : sig;
  let parens = 0;
  let bodyStart = -1;
  for (let i = source.indexOf('(', sig); i < source.length; i++) {
    if (source[i] === '(') parens++;
    else if (source[i] === ')' && --parens === 0) {
      bodyStart = source.indexOf('{', i + 1);
      break;
    }
  }
  assert.notEqual(bodyStart, -1, `missing body for ${name}`);
  let depth = 0;
  let end = -1;
  for (let i = bodyStart; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}' && --depth === 0) { end = i + 1; break; }
  }
  assert.notEqual(end, -1, `unbalanced ${name}`);
  const names = Object.keys(bindings);
  return Function(...names, `"use strict"; return (${source.slice(start, end)});`)(
    ...names.map(key => bindings[key]),
  );
}


test('hidden onboarding controls stay hidden by metadata and legacy text', () => {
  const firstAsk = "I'm here. Introduce yourself in two short sentences as my plain-language business setup guide.";
  const hidden = extractFunction('_onbHiddenControlMessage', { ONB_FIRST_ASK: firstAsk });
  assert.equal(hidden({ role:'system', text:'internal', metadata:{ kind:'onboarding_control' } }), true);
  assert.equal(hidden({ role:'user', text:'anything', metadata:{ kind:'onboarding_first_ask' } }), true);
  assert.equal(hidden({ role:'user', text:firstAsk, metadata:{} }), true);
  assert.equal(hidden({
    role:'system', text:'[Trusted onboarding desktop setup]\nprivate control', metadata:{},
  }), true);
  assert.equal(hidden({ role:'agent', text:'Welcome', metadata:{} }), false);
});


test('ordinary live chat suppresses private onboarding instructions', () => {
  const firstAsk = "I'm here. Introduce yourself in two short sentences as my plain-language business setup guide.";
  const hiddenControl = extractFunction('_onbHiddenControlMessage', { ONB_FIRST_ASK:firstAsk });
  const hidden = extractFunction('_ordinaryChatHiddenOnboardingMessage', {
    _onbHiddenControlMessage:hiddenControl,
  });
  assert.equal(hidden({
    role:'user', text:'private durable instruction',
    metadata:{ kind:'onboarding_guide_internal', visible_text:'Connect my other desktop agents.' },
  }), true);
  assert.equal(hidden({ role:'user', text:firstAsk, metadata:{} }), true);
  assert.equal(hidden({
    role:'user', metadata:{},
    text:'Agents Chat just completed the real desktop-agent connection pass. Use only this authoritative result for this Community workspace.',
  }), true);
  assert.equal(hidden({
    role:'user', metadata:{},
    text:'Calendar is not ready yet according to Agents Chat. Explain that result in plain business language and lead me through exactly one next step. Never ask me to paste a password, API key, token, or recovery code into chat.',
  }), true);
  assert.equal(hidden({
    role:'system', metadata:{},
    text:'[Trusted onboarding desktop setup]\none-time instructions',
  }), true);
  assert.equal(hidden({ role:'user', text:'Can you help with my calendar?', metadata:{} }), false);
  assert.equal(hidden({ role:'agent', text:'Calendar is ready.', metadata:{} }), false);
});


test('generated guide instructions keep friendly copy after durable hydration', () => {
  const display = extractFunction('_onbMessageDisplayText', {
    stripProductGuideOpenLines: text => text,
  });
  assert.equal(display({
    role:'user',
    text:'long private guide instruction',
    metadata:{
      kind:'onboarding_guide_internal',
      visible_text:"I'm back from Calendar.",
    },
  }), "I'm back from Calendar.");
  assert.equal(display({
    role:'user',
    text:'Agents Chat just completed the real desktop-agent connection pass. private result',
    metadata:{},
  }), 'Connect my other desktop agents.');
  assert.equal(display({
    role:'user',
    text:'Calendar is not ready yet according to Agents Chat. Explain that result in plain business language and lead me through exactly one next step. Never ask me to paste a password, API key, token, or recovery code into chat.',
    metadata:{},
  }), "I'm back from Calendar.");
});


test('hydration preserves friendly visible copy and unmatched optimistic messages', () => {
  let onboarding = { guideAgentId:'codex' };
  const hasLiveAgent = extractFunction('_onbHasLiveAgent', { _onb:onboarding });
  const matches = extractFunction('_onbHydratedMessageMatches');
  const merge = extractFunction('_onbMergeHydratedThread', {
    _onbHydratedMessageMatches:matches,
    _onbHasLiveAgent:hasLiveAgent,
  });
  const current = [
    {
      role:'user', agent_id:null, text:'Long authoritative desktop result', ts:10,
      _pendingUser:true, _visibleText:'Connect my other desktop agents.',
    },
    { role:'user', agent_id:null, text:'Still waiting to echo', ts:11, _pendingUser:true },
    {
      role:'system', text:'That setup reply failed. Try again.',
      _localOnboardingNote:true, _onbLifecycleKey:'retry:1',
    },
  ];
  const remote = [
    { role:'user', agent_id:null, text:'Long authoritative desktop result', ts:10, metadata:{} },
  ];
  const merged = merge(current, remote);
  assert.equal(merged.length, 3);
  assert.equal(merged[0]._visibleText, 'Connect my other desktop agents.');
  assert.equal(merged[0]._pendingUser, false);
  assert.equal(merged[1].text, 'Still waiting to echo');
  assert.equal(merged[1]._pendingUser, true);
  assert.equal(merged[2]._onbLifecycleKey, 'retry:1');
});


test('desktop onboarding inventory excludes optional provider and local-agent catalog rows', () => {
  const inventory = extractFunction('_onbDesktopInventory');
  const result = inventory({ agents:[
    { id:'codex', name:'Codex', state:'connected' },
    { id:'claude', name:'Claude', state:'needs_sign_in' },
    { id:'perplexity', name:'Perplexity', state:'found' },
    { id:'hermes', name:'Hermes', state:'manual' },
    { id:'openrouter', name:'OpenRouter', state:'setup_needed' },
  ] });
  assert.deepEqual(result.connected.map(agent => agent.id), ['codex']);
  assert.deepEqual(result.needsSignIn.map(agent => agent.id), ['claude']);
  assert.deepEqual(result.stillFound.map(agent => agent.id), ['perplexity']);
  assert.equal(Object.hasOwn(result, 'optional'), false);
});


test('onboarding guide allows the generated selective agent action', () => {
  const allowed = extractFunction('_onbGeneratedOpenTargetAllowed');
  assert.equal(allowed('connect-agent'), true);
  assert.equal(allowed('settings.calendar'), true);
  assert.equal(allowed('settings.users'), false);
});


test('agent chooser replaces the old bulk-first guide action', () => {
  const chips = extractFunction('_onbGuideChips', {
    _onb:{ firstTurnRetryAvailable:false, agentPickerOpen:false, desktopPass:false },
  });
  assert.deepEqual(chips()[0], ['team', 'Choose which desktop agents to connect']);

  const pickerChips = extractFunction('_onbGuideChips', {
    _onb:{ firstTurnRetryAvailable:false, agentPickerOpen:true, desktopPass:false },
  });
  assert.deepEqual(pickerChips()[0], ['agents-done', 'Done choosing agents']);
});


test('private local setup is offered only after the owner names that agent', () => {
  const named = extractFunction('_onbNamedLocalAgentRequest', { _onb:null });
  assert.equal(named([
    { role:'agent', text:'I can help.' },
    { role:'user', text:'Please connect only MiniMax and Hermes.' },
  ]), 'Please connect only MiniMax and Hermes.');
  assert.equal(named([
    { role:'user', text:'Connect my other desktop agents.', metadata:{ kind:'onboarding_guide_internal' } },
  ]), '');
  assert.equal(named([{ role:'user', text:'What does Hermes mean?' }]), '');
});


test('an existing orchestrator cannot turn a failed teammate pass green', () => {
  const state = {
    communityConnectBusy:false,
    communityConnectReport:{
      status:'attention', connected_before:['codex'], newly_ready:[],
      already_ready:[], unresolved:['claude'],
      results:[{ id:'claude', ok:false, detail:'Sign in needed', action:'failed' }],
    },
  };
  const escapeHtml = value => String(value ?? '');
  const reportHtml = extractFunction('communityConnectReportHtml', { state, escapeHtml });
  const html = reportHtml({
    ready_count:1,
    agents:[
      { id:'codex', name:'Codex', state:'connected' },
      { id:'claude', name:'Claude', state:'needs_sign_in' },
    ],
  });
  assert.match(html, /Some agents still need one more step/);
  assert.match(html, /Claude/);
  assert.match(html, /Sign in needed/);
  assert.doesNotMatch(html, /1 agent is connected and ready/);
  assert.doesNotMatch(html, /circle-check/);
});


test('guide operation lock is acquired before asynchronous work begins', () => {
  const onboarding = {
    guideBusy:false, remoteGuideBusy:false, guideOpSeq:0, guideOperation:null,
  };
  const begin = extractFunction('_onbBeginGuideOperation', {
    _onb:onboarding,
    state:{ communityConnectBusy:false },
  });
  const first = begin('send');
  const duplicate = begin('send');
  assert.ok(first);
  assert.equal(first.owner, onboarding);
  assert.equal(onboarding.guideBusy, true);
  assert.equal(duplicate, null);
});


function guideLifecycle(owner, { recordWin=() => {} } = {}) {
  const identity = extractFunction('_onbMessageIdentity');
  const expected = extractFunction('_onbExpectedGuideAgentMessage');
  const snapshot = extractFunction('_onbGuideServerSnapshot');
  const agentKeys = extractFunction('_onbAgentMessageKeys', {
    _onbMessageIdentity:identity,
  });
  const begin = extractFunction('_onbBeginRemoteGuideWait', {
    _onbAgentMessageKeys:agentKeys,
  });
  const finish = extractFunction('_onbFinishRemoteGuideWait');
  const hidden = extractFunction('_onbHiddenControlMessage', {
    ONB_FIRST_ASK:'first ask',
  });
  const observeRemote = extractFunction('_onbSettleHydratedRemoteGuideReply', {
    _onbExpectedGuideAgentMessage:expected,
    _onbHiddenControlMessage:hidden,
    _onbMessageIdentity:identity,
  });
  const observeLocal = extractFunction('_onbSettleHydratedGuideReply', {
    _onbExpectedGuideAgentMessage:expected,
    _onbMessageIdentity:identity,
  });
  const append = extractFunction('_onbAppendGuideLifecycleNote');
  const failureText = extractFunction('_onbGuideFailureText');
  const apply = extractFunction('_onbApplyGuideServerState', {
    _onbGuideServerSnapshot:snapshot,
    _onbSettleHydratedGuideReply:observeLocal,
    _onbSettleHydratedRemoteGuideReply:observeRemote,
    _onbAppendGuideLifecycleNote:append,
    _onbGuideFailureText:failureText,
    _onbBeginRemoteGuideWait:begin,
    _onbFinishRemoteGuideWait:finish,
    _onbRecordFirstWin:recordWin,
  });
  return { identity, expected, snapshot, agentKeys, begin, finish, observeRemote, observeLocal, append, failureText, apply };
}


test('the exact server lifecycle contract reconstructs busy without trusting an orphan row', () => {
  const owner = {
    guideAgentId:'codex', guideThread:[
      { role:'user', text:'first ask', metadata:{ kind:'onboarding_first_ask' } },
    ],
  };
  const { apply } = guideLifecycle(owner);

  const orphaned = apply(owner, {
    guide_working:false,
    working_agent_id:null,
    working_turn_id:null,
    first_turn_status:'orphaned',
    first_turn_attempt:1,
    first_turn_error:'The conductor stopped before replying',
    first_turn_retryable:true,
  }, owner.guideThread);
  assert.equal(orphaned.working, false);
  assert.equal(owner.remoteGuideBusy, undefined);
  assert.equal(owner.firstTurnRetryAvailable, true);
  assert.match(owner.guideThread.at(-1).text, /try the introduction again/i);

  const running = apply(owner, {
    guide_working:true,
    working_agent_id:'codex',
    working_turn_id:'first-turn-2',
    first_turn_status:'retrying',
    first_turn_attempt:2,
    first_turn_error:null,
    first_turn_retryable:true,
  }, owner.guideThread);
  assert.equal(running.working, true);
  assert.equal(owner.remoteGuideBusy, true);
  assert.equal(owner.remoteGuideAgentId, 'codex');
  assert.equal(owner.remoteTurnId, 'first-turn-2');
  assert.equal(owner.firstTurnRetryAvailable, false);
});


test('only an authoritative never-started first turn auto-sends once', () => {
  const owner = {
    firstTurnStatus:'not_started', firstTurnAttempt:0, firstAskSent:false,
    guideBusy:false, remoteGuideBusy:false, guideOpenBusy:false, guideServerWorking:false,
  };
  const sends = [];
  const kick = extractFunction('_onbMaybeKickFirstAsk', {
    _onb:owner,
    state:{ communityConnectBusy:false },
    ONB_FIRST_ASK:'first ask',
    onbGuideSend:async (text, options) => { sends.push({ text, options }); return true; },
  });
  assert.equal(kick(), true);
  assert.equal(kick(), false);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].options.turnKind, 'first-intro');

  owner.firstAskSent = false;
  owner.firstTurnStatus = 'orphaned';
  owner.firstTurnAttempt = 1;
  assert.equal(kick(), false);
  assert.equal(sends.length, 1);
});


test('visible reply plus terminal still waits for authoritative not-working hydration', () => {
  let wins = 0;
  const old = { id:'old', role:'agent', agent_id:'codex', text:'Earlier reply', ts:10 };
  const onboarding = {
    guideBusy:true, guideOperation:{ id:1 }, guideAgentId:'codex',
    activeTurnId:'turn-1', activeTurnSettled:false,
    activeTurnReplyReceived:false, activeTurnDoneReceived:false,
    activeTurnWinOnReply:true, activeTurnWinKind:'chat',
    activeTurnAgentId:'codex',
  };
  const lifecycle = guideLifecycle(onboarding, { recordWin:() => { wins += 1; } });
  onboarding.activeTurnPriorAgentKeys = new Set([lifecycle.identity(old)]);
  const settle = extractFunction('_onbSettleGuideTurn', {
    _onb:onboarding,
    _onbExpectedGuideAgentMessage:lifecycle.expected,
    _onbMessageIdentity:lifecycle.identity,
  });

  assert.equal(settle('turn-1'), true);
  assert.equal(onboarding.activeTurnDoneReceived, true);
  assert.equal(onboarding.activeTurnSettled, false);

  const reply = {
    id:'fresh', role:'agent', agent_id:'codex', turn_id:'turn-1', text:'Ready', ts:20,
  };
  assert.equal(settle('turn-1', reply), true);
  assert.equal(onboarding.activeTurnReplyReceived, true);
  assert.equal(onboarding.activeTurnSettled, false);
  assert.equal(wins, 0);

  lifecycle.apply(onboarding, {
    guide_working:true,
    working_agent_id:'codex', working_turn_id:'turn-1',
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, [old, reply]);
  assert.equal(onboarding.activeTurnSettled, false);
  assert.equal(wins, 0);

  lifecycle.apply(onboarding, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, [old, reply]);
  assert.equal(onboarding.activeTurnSettled, true);
  assert.equal(wins, 1);
});


test('authoritative local release finishes the HUD before the send lock cleanup', async () => {
  const reply = {
    id:'fresh', role:'agent', agent_id:'codex', turn_id:'turn-1', text:'Ready', ts:20,
  };
  const owner = {
    phase:'guide', answers:{}, guideDataSeq:1, hydrateInFlight:null,
    guideBusy:true, remoteGuideBusy:false, guideServerWorking:true,
    guideOperation:{ id:1 }, guideAgentId:'codex', activeTurnId:'turn-1',
    activeTurnReplyReceived:true, guideThread:[reply], serverThreadSig:'same',
  };
  const hud = [];
  const hydrate = extractFunction('_onbHydrateThread', {
    _onb:owner,
    apiFetch:async () => ({
      ok:true,
      json:async () => ({
        chat_id:'guide-chat', agent_id:'codex', agent_name:'Codex', messages:[reply],
      }),
    }),
    _onbVisibleMessages:messages => messages,
    _onbThreadSig:() => 'same',
    _onbApplyGuideServerState:localOwner => {
      localOwner.guideServerWorking = false;
      localOwner.activeTurnSettled = true;
      return { working:false, released:true, failed:false };
    },
    _onbMergeHydratedThread:thread => thread,
    state:{ communityDiscovery:{ agents:[] }, agentsById:{} },
    $:selector => selector === '#onbGuideThread' ? {} : null,
    renderOnboarding:() => {},
    _onbPaintGuide:() => {},
    _onbZipThink:text => { hud.push({ kind:'think', text }); },
    _onbZipFinish:(ok, text) => { hud.push({ kind:'finish', ok, text }); },
    _onbHasLiveAgent:() => true,
    _onbOrchName:() => 'Codex',
  });

  const result = await hydrate();
  assert.equal(result.applied, true);
  assert.equal(result.lifecycle.released, true);
  assert.equal(owner.guideBusy, true, 'duplicate-send lock remains owned until send cleanup');
  assert.equal(owner.guideAgentName, 'Codex');
  assert.deepEqual(hud, [{ kind:'finish', ok:true, text:'Got it' }]);
});


test('late discovery restores the persisted conductor identity without rebuilding its bubbles', async () => {
  const launchStart = source.indexOf('function launchOnboarding(onb)');
  const launchEnd = source.indexOf('\n  function closeOnboarding()', launchStart);
  const launchSource = source.slice(launchStart, launchEnd);
  assert.match(launchSource, /orchestrator_id:\s*saved\.orchestrator_id \|\| ''/);
  assert.match(launchSource, /guideAgentId:\s*answers\.orchestrator_id \|\| ''/);

  const imageNode = () => ({
    src:'', writes:0,
    setAttribute(name, value) {
      if (name === 'src') { this.src = value; this.writes += 1; }
    },
  });
  const faceNode = image => ({
    color:'', writes:0,
    style:{ setProperty(name, value) {
      if (name === '--orch') { this.owner.color = value; this.owner.writes += 1; }
    }, owner:null },
    querySelector:selector => selector === 'img' ? image : null,
  });
  const headerImage = imageNode();
  const headerFace = faceNode(headerImage);
  headerFace.style.owner = headerFace;
  const bubbleImage = imageNode();
  const bubbleFace = faceNode(bubbleImage);
  bubbleFace.style.owner = bubbleFace;
  const bubbleName = { textContent:'Orchestrator' };
  let threadRebuilds = 0;
  const focusedAction = { kind:'copy' };
  const thread = {
    focusedAction,
    querySelectorAll(selector) {
      if (selector === '[data-onb-guide-face]') return [bubbleFace];
      if (selector === '[data-onb-guide-name]') return [bubbleName];
      return [];
    },
  };
  Object.defineProperty(thread, 'innerHTML', { set() { threadRebuilds += 1; } });
  const connection = { textContent:'' };
  const name = { textContent:'' };
  const status = { textContent:'stale', hidden:false };
  const nodes = {
    '#onbGuideFace':headerFace,
    '#onbGuideConnection':connection,
    '#onbGuideName':name,
    '#onbGuideStatusDetail':status,
    '#onbGuideThread':thread,
  };
  const owner = {
    phase:'guide', guideAgentId:'codex',
    orchReady:true, orchReason:'old failure', guideThread:[
      { role:'agent', agent_id:'codex', text:'Welcome back' },
    ],
    threadSig:'unchanged-thread',
  };
  const identityPaint = extractFunction('_onbPaintGuideIdentity', {
    _onb:owner, _onbHasLiveAgent:() => true,
    $:selector => nodes[selector] || null,
  });

  identityPaint({ id:'codex' });
  assert.equal(name.textContent, 'Orchestrator');
  assert.equal(bubbleImage.src, '/static/agents-chat-mark-dark.svg?v=2');

  const discovery = {
    community:true,
    agents:[{
      id:'codex', name:'Codex', persona_url:'/static/codex.svg', color:'#33c374',
    }],
  };
  const state = {
    user:{ id:1 }, onboardingActive:true, communityDiscovery:null,
    communityDiscoveryUnavailable:false,
  };
  let guideRenders = 0;
  const loadDiscovery = extractFunction('loadCommunityAgentDiscovery', {
    state, _onb:owner,
    backgroundSingleFlight:(_key, work) => work(),
    apiFetch:async () => ({ ok:true, status:200, json:async () => discovery }),
    installAgentRoster:() => {}, renderTargetChips:() => {},
    renderModeControl:() => {}, renderThreads:() => {}, renderInspector:() => {},
    renderOnboarding:() => {
      guideRenders += 1;
      identityPaint(state.communityDiscovery.agents[0]);
    },
    toast:() => {},
  });

  assert.equal(await loadDiscovery({ silent:true }), discovery);
  assert.equal(guideRenders, 1, 'guide must repaint when late discovery resolves');
  assert.equal(name.textContent, 'Codex');
  assert.equal(headerImage.src, '/static/codex.svg');
  assert.equal(headerFace.color, '#33c374');
  assert.equal(bubbleName.textContent, 'Codex');
  assert.equal(bubbleImage.src, '/static/codex.svg');
  assert.equal(bubbleFace.color, '#33c374');
  assert.equal(connection.textContent, 'Your orchestrator · connected');
  assert.equal(status.textContent, '');
  assert.equal(status.hidden, true);
  assert.equal(threadRebuilds, 0);
  assert.equal(thread.focusedAction, focusedAction);
  assert.equal(owner.threadSig, 'unchanged-thread');

  const bubbleWrites = bubbleImage.writes + bubbleFace.writes;
  identityPaint(discovery.agents[0]);
  assert.equal(bubbleImage.writes + bubbleFace.writes, bubbleWrites,
    'unchanged identity must not touch already-painted bubbles');
  const paintStart = source.indexOf('function _onbPaintGuide(orch)');
  const paintEnd = source.indexOf('\n  function _onbParkZip()', paintStart);
  const paintSource = source.slice(paintStart, paintEnd);
  assert.match(paintSource, /data-onb-guide-face/);
  assert.match(paintSource, /data-onb-guide-name/);
});


test('remote reply remains locked while working and releases only after cleanup', () => {
  const firstAsk = { role:'user', text:'first ask', metadata:{ kind:'onboarding_first_ask' } };
  const owner = { guideAgentId:'codex', guideThread:[firstAsk] };
  const { apply } = guideLifecycle(owner);
  const working = {
    guide_working:true,
    working_agent_id:'codex', working_turn_id:'first-1',
    first_turn_status:'working', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:true,
  };

  apply(owner, working, owner.guideThread);
  assert.equal(owner.remoteGuideBusy, true);
  const reply = {
    id:'reply-1', role:'agent', agent_id:'codex', turn_id:'first-1', text:'Ready',
  };
  apply(owner, { ...working, first_turn_status:'complete' }, [...owner.guideThread, reply]);
  assert.equal(owner.remoteGuideBusy, true);
  assert.equal(owner.remoteTurnReplyReceived, true);

  apply(owner, {
    ...working, guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'complete', first_turn_retryable:false,
  }, [...owner.guideThread, reply]);
  assert.equal(owner.remoteGuideBusy, false);
  assert.equal(owner.guideThread.some(message => /failed|without a visible reply/i.test(message.text || '')), false);
});


test('reopen recognizes an already-persisted active reply by authoritative start time', () => {
  const old = { role:'agent', agent_id:'codex', text:'Old answer', ts:1000 };
  const activeReply = { role:'agent', agent_id:'codex', text:'New answer', ts:5050 };
  const owner = { guideAgentId:'codex', guideThread:[old, activeReply] };
  const lifecycle = guideLifecycle(owner);
  lifecycle.apply(owner, {
    guide_working:true,
    working:{ started_ts:5000, first_turn:false },
    working_agent_id:'codex', working_turn_id:'later-turn',
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, owner.guideThread);
  assert.equal(owner.remoteGuideBusy, true);
  assert.equal(owner.remoteTurnReplyReceived, true);

  lifecycle.apply(owner, {
    guide_working:false,
    working:{ started_ts:null, first_turn:false },
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, owner.guideThread);
  assert.equal(owner.remoteGuideBusy, false);
  assert.equal(owner.guideThread.some(message => /without a visible reply/i.test(message.text || '')), false);
});


test('a terminal local turn with no reply hydrates once then unlocks with retry guidance', () => {
  let hydrates = 0;
  const owner = {
    guideBusy:true, guideOperation:{ id:1 }, guideAgentId:'codex',
    activeTurnId:'turn-missing', activeTurnAgentId:'codex',
    activeTurnSettled:false, activeTurnReplyReceived:false,
    activeTurnDoneReceived:false, activeTurnPriorAgentKeys:new Set(),
    guideThread:[],
  };
  const lifecycle = guideLifecycle(owner);
  const settle = extractFunction('_onbSettleGuideTurn', {
    _onb:owner,
    _onbExpectedGuideAgentMessage:lifecycle.expected,
    _onbMessageIdentity:lifecycle.identity,
  });
  const hydrateAfterTerminal = extractFunction('_onbHydrateAfterGuideTerminal', {
    _onbHydrateThread:() => { hydrates += 1; },
  });
  assert.equal(settle('turn-missing'), true);
  assert.equal(hydrateAfterTerminal(owner, 'turn-missing'), true);
  assert.equal(hydrateAfterTerminal(owner, 'turn-missing'), false);
  assert.equal(hydrates, 1);
  assert.equal(owner.activeTurnSettled, false);

  const outcome = lifecycle.apply(owner, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, []);
  assert.equal(outcome.failed, true);
  assert.equal(owner.activeTurnSettled, true);
  assert.equal(owner.activeTurnFailed, true);
  assert.match(owner.guideThread.at(-1).text, /finished without a visible reply/i);
});


test('an already-complete first ask releases a stale retry without a false failure', () => {
  const priorReply = { role:'agent', agent_id:'codex', text:'Welcome', ts:10 };
  const owner = {
    guideBusy:true, guideOperation:{ id:1 }, guideAgentId:'codex',
    activeTurnId:'retry-stale', activeTurnAgentId:'codex',
    activeTurnWinKind:'first-intro', activeTurnWinOnReply:false,
    activeTurnSettled:false, activeTurnReplyReceived:false,
    activeTurnPriorAgentKeys:new Set(), guideThread:[priorReply],
  };
  const lifecycle = guideLifecycle(owner);
  const outcome = lifecycle.apply(owner, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'complete', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:false,
  }, owner.guideThread);
  assert.equal(outcome.released, true);
  assert.equal(outcome.failed, false);
  assert.equal(owner.activeTurnSettled, true);
  assert.equal(owner.activeTurnReplyReceived, true);
});


test('Back transfers a local active turn into a locked remote wait', () => {
  const owner = {
    guideBusy:true, guideOperation:{ id:3 }, guideOpSeq:3,
    guideAgentId:'codex', activeTurnId:'later-1', activeTurnAgentId:'codex',
    activeTurnReplyReceived:false, activeTurnDoneReceived:false,
    activeTurnPriorAgentKeys:new Set(['id:old']), guideThread:[],
  };
  const lifecycle = guideLifecycle(owner);
  const cancel = extractFunction('_onbCancelGuideOperation', {
    _onb:owner,
    _onbBeginRemoteGuideWait:lifecycle.begin,
    _onbFinishRemoteGuideWait:lifecycle.finish,
  });
  cancel({ preserveRemote:true });
  assert.equal(owner.guideBusy, false);
  assert.equal(owner.activeTurnId, '');
  assert.equal(owner.remoteGuideBusy, true);
  assert.equal(owner.remoteTurnId, 'later-1');
  assert.equal(owner.remoteGuideAgentId, 'codex');
  assert.deepEqual([...owner.remoteTurnPriorAgentKeys], ['id:old']);
});


test('remote live wait accepts only a genuinely new message from the expected conductor', () => {
  const old = { id:'old', role:'agent', agent_id:'codex', turn_id:'turn-1', text:'Old' };
  const owner = { guideAgentId:'codex', guideThread:[old] };
  const lifecycle = guideLifecycle(owner);
  const observeLive = extractFunction('_onbObserveLiveRemoteGuideReply', {
    _onbExpectedGuideAgentMessage:lifecycle.expected,
    _onbMessageIdentity:lifecycle.identity,
  });
  lifecycle.begin(owner, 'turn-1', 'codex');

  assert.equal(observeLive(owner, old, true), false);
  assert.equal(observeLive(owner, {
    id:'wrong-agent', role:'agent', agent_id:'minimax', turn_id:'turn-1', text:'No',
  }), false);
  assert.equal(observeLive(owner, {
    id:'wrong-turn', role:'agent', agent_id:'codex', turn_id:'turn-2', text:'No',
  }), false);
  const fresh = {
    id:'fresh', role:'agent', agent_id:'codex', turn_id:'turn-1', text:'Ready',
  };
  assert.equal(observeLive(owner, fresh), true);
  assert.equal(observeLive(owner, fresh), false);
  assert.equal(owner.remoteGuideBusy, true);
  assert.equal(owner.remoteTurnReplyReceived, true);
});


test('working agent identity survives a conductor switch and failed first turn becomes retryable', () => {
  const owner = {
    guideAgentId:'minimax',
    guideThread:[{ id:'old', role:'agent', agent_id:'codex', text:'Earlier' }],
  };
  const lifecycle = guideLifecycle(owner);
  lifecycle.apply(owner, {
    guide_working:true,
    working_agent_id:'codex', working_turn_id:'first-codex',
    first_turn_status:'working', first_turn_attempt:1,
    first_turn_error:null, first_turn_retryable:true,
  }, owner.guideThread);
  assert.equal(owner.remoteGuideAgentId, 'codex');

  const observeLive = extractFunction('_onbObserveLiveRemoteGuideReply', {
    _onbExpectedGuideAgentMessage:lifecycle.expected,
    _onbMessageIdentity:lifecycle.identity,
  });
  assert.equal(observeLive(owner, {
    id:'new-minimax', role:'agent', agent_id:'minimax', turn_id:'first-codex', text:'Wrong owner',
  }), false);

  lifecycle.apply(owner, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'orphaned', first_turn_attempt:1,
    first_turn_error:'The prior conductor stopped', first_turn_retryable:true,
  }, owner.guideThread);
  assert.equal(owner.remoteGuideBusy, false);
  assert.equal(owner.firstTurnRetryAvailable, true);
  assert.match(owner.guideThread.at(-1).text, /try the introduction again/i);
});


test('late guide navigation is rejected after either phase or token changes', () => {
  const owner = { phase:'orchestrator', guideNavSeq:7 };
  const current = extractFunction('_onbGuideNavigationCurrent', { _onb:owner });
  assert.equal(current(owner, 7, 'orchestrator'), true);
  owner.phase = 'lock';
  assert.equal(current(owner, 7, 'orchestrator'), false);
  owner.phase = 'orchestrator';
  owner.guideNavSeq = 8;
  assert.equal(current(owner, 7, 'orchestrator'), false);
});


test('first-turn retry sends one new correlated first-intro turn', async () => {
  const owner = {
    firstTurnRetryAvailable:true, guideBusy:false, remoteGuideBusy:false,
    guideOpenBusy:false, guideServerWorking:false,
  };
  const calls = [];
  let shouldSend = true;
  const retry = extractFunction('_onbRetryFirstAsk', {
    _onb:owner,
    state:{ communityConnectBusy:false },
    _onbRepaintGuideOwner:() => true,
    ONB_FIRST_ASK:'first ask',
    onbGuideSend:async (text, options) => {
      calls.push({ text, options });
      return shouldSend;
    },
  });
  assert.equal(await retry(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].text, 'first ask');
  assert.deepEqual(calls[0].options, {
    silent:true, winOnReply:false, turnKind:'first-intro',
  });

  owner.firstTurnRetryAvailable = true;
  shouldSend = false;
  assert.equal(await retry(), false);
  assert.equal(owner.firstTurnRetryAvailable, true);
  assert.doesNotMatch(source, /retry_first_turn/);
  assert.match(source, /first_turn_status/);
  assert.match(source, /working_agent_id/);
  assert.match(source, /working_turn_id/);
});


test('the silent first ask repaints the owned guide lock immediately', () => {
  const owner = { phase:'guide', guideAgentId:'codex', orchPick:null };
  let painted = 0;
  let rendered = 0;
  const repaint = extractFunction('_onbRepaintGuideOwner', {
    _onb:owner,
    state:{ communityDiscovery:{ agents:[{ id:'codex', name:'Codex' }] }, agentsById:{} },
    $:selector => selector === '#onbGuideThread' ? {} : null,
    renderOnboarding:() => { rendered += 1; },
    _onbPaintGuide:orch => { assert.equal(orch.name, 'Codex'); painted += 1; },
  });
  assert.equal(repaint(owner), true);
  assert.equal(painted, 1);
  assert.equal(rendered, 0);
  assert.equal(repaint({ phase:'guide' }), false);
});


test('a failed automatic first ask becomes explicit retry instead of a timer loop', async () => {
  const owner = {
    firstTurnStatus:'not_started', firstTurnAttempt:0,
    firstAskSent:false, firstTurnRetryAvailable:false,
    guideBusy:false, remoteGuideBusy:false, guideOpenBusy:false,
    guideServerWorking:false,
  };
  let sends = 0;
  const kick = extractFunction('_onbMaybeKickFirstAsk', {
    _onb:owner,
    state:{ communityConnectBusy:false },
    ONB_FIRST_ASK:'first ask',
    onbGuideSend:async () => { sends += 1; return false; },
    _onbRepaintGuideOwner:() => true,
  });
  assert.equal(kick(), true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(owner.firstAskSent, true);
  assert.equal(owner.firstTurnRetryAvailable, true);
  assert.equal(kick(), false);
  assert.equal(sends, 1);
});


test('a stale not-working hydrate cannot settle a turn before admission resolves', () => {
  const owner = {
    guideBusy:true, guideOperation:{ id:1 }, guideAgentId:'codex',
    activeTurnId:'turn-pending', activeTurnAgentId:'codex',
    activeTurnSettled:false, activeTurnReplyReceived:false,
    activeTurnPriorAgentKeys:new Set(), admissionPendingTurnId:'turn-pending',
    guideThread:[],
  };
  const { apply } = guideLifecycle(owner);
  const stale = apply(owner, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'not_started', first_turn_attempt:0,
    first_turn_error:null, first_turn_retryable:false,
  }, []);
  assert.equal(stale.admissionPending, true);
  assert.equal(owner.activeTurnSettled, false);
  assert.equal(owner.guideBusy, true);

  owner.guideBusy = false;
  owner.guideOperation = null;
  owner.activeTurnId = '';
  owner.remoteGuideBusy = true;
  owner.remoteTurnId = 'turn-pending';
  owner.remoteGuideAgentId = 'codex';
  owner.remoteTurnReplyReceived = false;
  const staleAfterBack = apply(owner, {
    guide_working:false,
    working_agent_id:null, working_turn_id:null,
    first_turn_status:'not_started', first_turn_attempt:0,
    first_turn_error:null, first_turn_retryable:false,
  }, []);
  assert.equal(staleAfterBack.admissionPending, true);
  assert.equal(owner.remoteGuideBusy, true);
});


test('a rejected guide send removes only its optimistic bubble and restores manual text', () => {
  const prior = { role:'user', text:'same words' };
  const optimistic = { role:'user', text:'same words', _pendingUser:true };
  const owner = { guideThread:[prior, optimistic] };
  const input = { value:'' };
  const discard = extractFunction('_onbDiscardGuideOptimistic');
  assert.equal(discard(owner, optimistic, input, 'same words', false), true);
  assert.deepEqual(owner.guideThread, [prior]);
  assert.equal(input.value, 'same words');
  assert.equal(discard(owner, optimistic, input, 'same words', false), false);
});


test('ambiguous admission keeps only a same-turn or identity-unknown attachment', () => {
  const rejected = extractFunction('_onbAdmissionRejectedBySnapshot');
  assert.equal(rejected({ guideServerWorking:false, remoteGuideBusy:false }, 'mine'), true);
  assert.equal(rejected({
    guideServerWorking:true, remoteGuideBusy:true,
    guideWorkingTurnId:'other', remoteTurnId:'other',
  }, 'mine'), true);
  assert.equal(rejected({
    guideServerWorking:true, remoteGuideBusy:false,
    guideWorkingTurnId:'mine',
  }, 'mine'), false);
  assert.equal(rejected({
    guideServerWorking:true, remoteGuideBusy:true,
    guideWorkingTurnId:'', remoteTurnId:'',
  }, 'mine'), false);
});


test('guide open failure stays on conductor selection and cannot launch a fallback chat', async () => {
  const owner = {
    phase:'orchestrator', guideNavSeq:0, guideDataSeq:0,
    guideOpenBusy:false, answers:{ address_as:'Morgan', orchestrator_id:'codex' },
    orchPick:{ id:'codex', name:'Codex' }, guideThread:[],
  };
  let kicks = 0;
  let gotos = 0;
  let renders = 0;
  const open = extractFunction('onbOpenGuide', {
    _onb:owner,
    state:{ agentsById:{ codex:{ name:'Codex' } } },
    _onbRepaintGuideOwner:() => true,
    apiFetch:async () => ({ ok:false, json:async () => ({ error:'server unavailable' }) }),
    _onbGuideNavigationCurrent:(candidate, token, phase) => candidate === owner
      && candidate.guideNavSeq === token && candidate.phase === phase,
    _onbAppendGuideLifecycleNote:() => true,
    toast:() => {},
    renderOnboarding:() => { renders += 1; },
    _onbMaybeKickFirstAsk:() => { kicks += 1; return true; },
    _onbGoto:() => { gotos += 1; },
  });
  assert.equal(await open(), false);
  assert.equal(owner.phase, 'orchestrator');
  assert.equal(owner.guideOpenBusy, false);
  assert.equal(owner.guideOpenError, 'server unavailable');
  assert.equal(kicks, 0);
  assert.equal(gotos, 0);
  assert.equal(renders, 1);

  const start = source.indexOf('async function onbGuideSend(');
  const end = source.indexOf('\n  function _onbOrchName(', start);
  assert.ok(start >= 0 && end > start);
  const sendSource = source.slice(start, end);
  assert.doesNotMatch(sendSource, /\/api\/chats\/new/);
  assert.match(sendSource, /responseSink:\s*sendResult/);
  assert.match(sendSource, /workingTurnId\s*===\s*turnId/);
  assert.match(sendSource, /ambiguousAdmission/);
  assert.match(sendSource, /await _onbHydrateThread\(\{ fresh:true \}\)/);
  assert.match(sendSource, /_onbHydrateThread\(\{ fresh:true \}\)/);
  assert.doesNotMatch(sendSource, /lastSendError/);
});


test('a targeted onboarding settings handoff re-anchors after async settings hydration', async () => {
  let settingsGate = null;
  let gateRemovals = 0;
  let calendarScrolls = 0;
  let releaseSettings = () => {};
  const document = {
    getElementById(id) {
      if (id === 'firstOpenSettings') return settingsGate;
      if (id === 'settingsCalendar') {
        return { scrollIntoView:() => { calendarScrolls += 1; } };
      }
      return null;
    },
  };
  const state = { onboardingActive:false, view:'chat' };
  const setView = view => {
    assert.equal(view, 'settings');
    state.viewNavigationToken = (state.viewNavigationToken || 0) + 1;
    state.view = view;
    settingsGate = {
      removed:false,
      remove() { this.removed = true; gateRemovals += 1; settingsGate = null; },
    };
    return new Promise(resolve => { releaseSettings = resolve; });
  };
  const open = extractFunction('openGuideScreen', {
    state,
    _onb:null,
    _onbPushNote:() => {},
    closeSheet:() => {},
    setView,
    document,
    requestAnimationFrame:fn => { fn(); return 1; },
  });

  const opening = open('settings.calendar', { fromOnboarding:true });
  assert.equal(gateRemovals, 1);
  assert.equal(settingsGate, null);
  assert.equal(calendarScrolls, 1);
  releaseSettings();
  await opening;
  assert.equal(calendarScrolls, 2);

  await open('settings');
  assert.ok(settingsGate);
  assert.equal(settingsGate.removed, false);
  assert.equal(gateRemovals, 1);
});


test('a stale targeted settings handoff cannot pull the user back after later generic settings navigation', async () => {
  let releaseSettings = () => {};
  let calendarScrolls = 0;
  const state = { onboardingActive:false, view:'chat' };
  const document = {
    getElementById(id) {
      if (id === 'settingsCalendar') return { scrollIntoView:() => { calendarScrolls += 1; } };
      return null;
    },
  };
  const open = extractFunction('openGuideScreen', {
    state,
    _onb:null,
    _onbPushNote:() => {},
    closeSheet:() => {},
    setView:view => {
      state.viewNavigationToken = (state.viewNavigationToken || 0) + 1;
      state.view = view;
      return new Promise(resolve => { releaseSettings = resolve; });
    },
    document,
    requestAnimationFrame:fn => { fn(); return 1; },
  });

  const opening = open('settings.calendar', { fromOnboarding:true });
  assert.equal(calendarScrolls, 1);
  // A later ordinary Settings click keeps the same view name, but advances the
  // general navigation generation and must invalidate this earlier deep link.
  state.viewNavigationToken += 1;
  state.view = 'settings';
  releaseSettings();
  await opening;
  assert.equal(calendarScrolls, 1);
});


test('secure setup exposes a visible keyboard-focused return navigation control', () => {
  let appended = null;
  let focused = false;
  const button = {
    onclick:null,
    focus:({ preventScroll }) => { assert.equal(preventScroll, true); focused = true; },
  };
  const banner = {
    id:'', className:'', attrs:{}, innerHTML:'',
    setAttribute(name, value) { this.attrs[name] = value; },
    querySelector:selector => selector === 'button' ? button : null,
  };
  const document = {
    getElementById:() => null,
    createElement:tag => { assert.equal(tag, 'div'); return banner; },
    body:{ appendChild:value => { appended = value; } },
  };
  const show = extractFunction('_onbShowReturnBanner', {
    document,
    _onbOrchName:() => 'Codex',
    escapeHtml:value => String(value),
    onbResumeSecureSetup:() => {},
    iconize:() => {},
    requestAnimationFrame:fn => { fn(); return 1; },
  });

  show('Codex');
  assert.equal(appended, banner);
  assert.equal(banner.className, 'onb-return-banner');
  assert.equal(banner.attrs.role, 'navigation');
  assert.equal(banner.attrs['aria-label'], 'Setup navigation');
  assert.match(banner.innerHTML, /onb-return-button/);
  assert.match(banner.innerHTML, /Return to setup with Codex/);
  assert.equal(typeof button.onclick, 'function');
  assert.equal(focused, true);
});


test('a completed onboarding guide renders OPEN controls as explicit actions only in its persisted chat', () => {
  const state = {
    onboardingActive:false,
    activeChatId:'guide-chat',
    chats:{
      'guide-chat':{ title:'Your orchestrator', skill_name:'__onboarding_guide__' },
      ordinary:{ title:'Customer follow-up' },
    },
    agentsById:{ codex:{ id:'codex', name:'Codex', color:'#33c374' } },
  };
  const escapeHtml = value => String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const productGuideOpenRe = extractFunction('productGuideOpenRe');
  const stripProductGuideOpenLines = extractFunction('stripProductGuideOpenLines', {
    productGuideOpenRe,
  });
  const isOnboardingGuideChat = extractFunction('isOnboardingGuideChat', {
    _onb:null, state,
  });
  const allowedOpenTarget = extractFunction('_onbGeneratedOpenTargetAllowed');
  const openActions = extractFunction('_onbOpenActions', {
    productGuideOpenRe,
    _onbGeneratedOpenTargetAllowed:allowedOpenTarget,
  });
  const actionLabel = extractFunction('_onbActionLabel');
  const renderActions = extractFunction('renderInactiveOnboardingGuideActions', {
    _onbActionLabel:actionLabel, escapeHtml,
  });
  const renderOne = extractFunction('renderOne', {
    state,
    isOnboardingGuideChat,
    _onbOpenActions:openActions,
    stripProductGuideOpenLines,
    renderInactiveOnboardingGuideActions:renderActions,
    splitAuthoredBottomLine:() => null,
    enhancedBottomLineData:() => null,
    renderBottomLine:() => '', renderAuthoredBottomLine:() => '',
    renderCollapsibleBody:message => `[[BODY:${message.text}]]`,
    renderAttachments:() => '', renderArtifactChips:() => '',
    renderCanvasPromote:() => '', renderTurnMetadata:() => '',
    agentBadgeHtml:() => 'C', messageToolsHtml:() => '',
    escapeHtml, formatTime:() => 'now',
  });
  const guideMessage = {
    role:'agent', agent_id:'codex', chat_id:'guide-chat',
    text:'I can help connect your calendar.\n\nOPEN: settings.calendar', ts:1,
  };

  const completedGuide = renderOne(guideMessage, 0);
  assert.match(completedGuide, /\[\[BODY:I can help connect your calendar\.\]\]/);
  assert.doesNotMatch(completedGuide, /OPEN:/);
  assert.match(completedGuide, /data-act="guide-open"/);
  assert.match(completedGuide, /data-guide-open="settings\.calendar"/);
  assert.match(completedGuide, /aria-label="Open secure Calendar settings"/);
  assert.match(completedGuide, />Open secure Calendar settings<\/span>/);

  const rejectedGuideAction = renderOne({
    ...guideMessage,
    text:'I cannot open that privileged screen.\n\nOPEN: secrets',
  }, 1);
  assert.doesNotMatch(rejectedGuideAction, /data-act="guide-open"/);
  assert.doesNotMatch(rejectedGuideAction, /data-guide-open="secrets"/);
  assert.doesNotMatch(rejectedGuideAction, /OPEN: secrets/);

  // A completed user's cold page load begins explicitly inactive, so persisted
  // guide actions render before any onboarding lifecycle method runs.
  assert.equal(state.onboardingActive, false);
  assert.match(source, /const state = \{\s*user: null,[^\n]*\n\s*onboardingActive: false,/);

  const ordinary = renderOne({ ...guideMessage, chat_id:'ordinary' }, 2);
  assert.match(ordinary, /OPEN: settings\.calendar/);
  assert.doesNotMatch(ordinary, /data-act="guide-open"/);

  state.onboardingActive = true;
  const activeOnboarding = renderOne(guideMessage, 3);
  assert.match(activeOnboarding, /OPEN: settings\.calendar/);
  assert.doesNotMatch(activeOnboarding, /data-act="guide-open"/);
});


test('persisted guide action routes the exact target with onboarding settings semantics', () => {
  const calls = [];
  const allowedOpenTarget = extractFunction('_onbGeneratedOpenTargetAllowed');
  const openAction = extractFunction('openInactiveOnboardingGuideAction', {
    _onbGeneratedOpenTargetAllowed:allowedOpenTarget,
    openGuideScreen:(target, options) => calls.push({ target, options }),
  });

  assert.equal(openAction('settings.calendar'), true);
  assert.deepEqual(calls, [{
    target:'settings.calendar', options:{ fromOnboarding:true },
  }]);
  assert.equal(openAction(''), false);
  assert.equal(openAction('secrets'), false);
  assert.equal(openAction('settings.users'), false);
  assert.equal(calls.length, 1);

  const clickStart = source.indexOf("if (t.dataset.act === 'guide-open')");
  const clickEnd = source.indexOf("if (t.dataset.act === 'open-canvas')", clickStart);
  assert.ok(clickStart >= 0 && clickEnd > clickStart);
  const delegatedClick = source.slice(clickStart, clickEnd);
  assert.match(delegatedClick, /openInactiveOnboardingGuideAction\(t\.dataset\.guideOpen \|\| ''\)/);
});


test('persisted guide message tools copy speak and quote only visible text', () => {
  const state = {
    onboardingActive:false,
    activeChatId:'guide-chat',
    chats:{ 'guide-chat':{ skill_name:'__onboarding_guide__' }, ordinary:{} },
  };
  const productGuideOpenRe = extractFunction('productGuideOpenRe');
  const stripProductGuideOpenLines = extractFunction('stripProductGuideOpenLines', {
    productGuideOpenRe,
  });
  const isOnboardingGuideChat = extractFunction('isOnboardingGuideChat', {
    _onb:null, state,
  });
  const actionText = extractFunction('inactiveOnboardingGuideMessageText', {
    state, isOnboardingGuideChat, stripProductGuideOpenLines,
  });
  const guideMessage = {
    role:'agent', chat_id:'guide-chat',
    text:'Use the secure button below.\n\nOPEN:settings.calendar',
  };

  assert.equal(actionText(guideMessage), 'Use the secure button below.');
  assert.equal(
    actionText({ ...guideMessage, chat_id:'ordinary' }),
    guideMessage.text,
  );
  state.onboardingActive = true;
  assert.equal(actionText(guideMessage), guideMessage.text);

  const toolsStart = source.indexOf('const actionText = inactiveOnboardingGuideMessageText(m);');
  const toolsEnd = source.indexOf("} else if (t.dataset.act === 'retry')", toolsStart);
  assert.ok(toolsStart >= 0 && toolsEnd > toolsStart);
  const tools = source.slice(toolsStart, toolsEnd);
  assert.match(tools, /copyText\(actionText\)/);
  assert.match(tools, /speakMessage\(\{ \.\.\.m, text:actionText \}, i\)/);
  assert.match(tools, /actionText\.split/);
  assert.doesNotMatch(tools, /copyText\(m\.text/);
});


test('the active onboarding overlay keeps its existing OPEN action contract', () => {
  const productGuideOpenRe = extractFunction('productGuideOpenRe');
  const stripProductGuideOpenLines = extractFunction('stripProductGuideOpenLines', {
    productGuideOpenRe,
  });
  const displayText = extractFunction('_onbMessageDisplayText', {
    stripProductGuideOpenLines,
  });
  const allowedOpenTarget = extractFunction('_onbGeneratedOpenTargetAllowed');
  const openActions = extractFunction('_onbOpenActions', {
    productGuideOpenRe,
    _onbGeneratedOpenTargetAllowed:allowedOpenTarget,
  });
  const actionLabel = extractFunction('_onbActionLabel');
  const bubbleActions = extractFunction('_onbBubbleActionsHtml', {
    _onbOpenActions:openActions,
    _onbActionLabel:actionLabel,
    escapeHtml:value => String(value ?? ''),
  });
  const message = {
    role:'agent', text:'Let me open that securely.\n\nOPEN: settings.calendar',
  };

  assert.equal(displayText(message), 'Let me open that securely.');
  const html = bubbleActions(message, 4);
  assert.match(html, /data-onb-open="settings\.calendar"/);
  assert.match(html, /Open secure Calendar settings/);
  assert.doesNotMatch(html, /data-act="guide-open"/);

  const rejected = bubbleActions({
    role:'agent', text:'No privileged navigation.\n\nOPEN:secrets',
  }, 5);
  assert.doesNotMatch(rejected, /data-onb-open="secrets"/);
});


test('leaving onboarding rehydrates the persisted guide chat without a page refresh', async () => {
  const sameChatCalls = [];
  const openSame = extractFunction('_onbOpenPersistedGuideChat', {
    state:{ activeChatId:'guide-chat', chatSwitchCommitted:false },
    switchChat:async target => { sameChatCalls.push(`switch:${target}`); },
    beginChatTransition:(target, label) => {
      sameChatCalls.push(`begin:${target}:${label}`);
    },
    _killEs:() => { sameChatCalls.push('kill'); },
    wsConnect:reason => { sameChatCalls.push(`connect:${reason}`); },
  });
  assert.equal(await openSame('guide-chat'), true);
  assert.deepEqual(sameChatCalls, [
    'begin:guide-chat:Opening your orchestrator…',
    'kill',
    'connect:onboarding-complete',
  ]);
  assert.equal(await openSame(''), false);

  const switchedCalls = [];
  const openDifferent = extractFunction('_onbOpenPersistedGuideChat', {
    state:{ activeChatId:'other-chat' },
    switchChat:async target => { switchedCalls.push(`switch:${target}`); },
    beginChatTransition:(target, label) => {
      switchedCalls.push(`begin:${target}:${label}`);
    },
    _killEs:() => { switchedCalls.push('kill'); },
    wsConnect:reason => { switchedCalls.push(`connect:${reason}`); },
  });
  assert.equal(await openDifferent('guide-chat'), true);
  assert.deepEqual(switchedCalls, ['switch:guide-chat']);

  const enterStart = source.indexOf("enterButton?.addEventListener('click', async () => {");
  const enterEnd = source.indexOf('\n      });', enterStart);
  assert.ok(enterStart >= 0 && enterEnd > enterStart);
  const enterSource = source.slice(enterStart, enterEnd);
  const captureAt = enterSource.indexOf('const guideChatId = String(owner.guideChatId)');
  const closeAt = enterSource.indexOf('closeOnboarding()');
  const reopenAt = enterSource.indexOf('await _onbOpenPersistedGuideChat(guideChatId)');
  assert.ok(captureAt >= 0 && captureAt < closeAt);
  assert.ok(reopenAt > closeAt);
});


test('the same-chat onboarding transition opens its authoritative SSE immediately', async () => {
  const state = {
    activeChatId:'guide-chat', switchingChatId:null, chatSwitchCommitted:false,
    switchTimeout:null, es:{ readyState:1, close() {} }, reconnectAttempts:0,
    lastEventTs:0,
  };
  const overlays = [];
  const begin = extractFunction('beginChatTransition', {
    state,
    closeAllThreadMenus:() => {}, closeSheet:() => {},
    setChatSwitchOverlay:(show, label) => { overlays.push({ show, label }); },
    renderThreadBody:() => {}, renderAppView:() => {}, renderSubHeader:() => {},
    setTimeout:() => 77, clearTimeout:() => {}, console,
    CHAT_SWITCH_TIMEOUT_MS:12000,
    finishChatTransition:() => {}, wsConnect:() => {},
  });
  const openedUrls = [];
  class TestEventSource {
    constructor(url) { this.readyState = 0; openedUrls.push(url); }
    close() {}
  }
  const kill = () => {
    state.es?.close?.();
    state.es = null;
  };
  const connect = extractFunction('wsConnect', {
    state, idleSignedOut:false, setStatus:() => {}, _killEs:kill,
    URLSearchParams, DEVICE_ID:'test-device', location:{ origin:'http://127.0.0.1:8086' },
    EventSource:TestEventSource, scheduleReconnect:() => {}, console,
  });
  const open = extractFunction('_onbOpenPersistedGuideChat', {
    state, switchChat:async () => {}, beginChatTransition:begin,
    _killEs:kill, wsConnect:connect,
  });

  assert.equal(await open('guide-chat'), true);
  assert.equal(state.switchingChatId, 'guide-chat');
  assert.equal(state.chatSwitchCommitted, true);
  assert.deepEqual(overlays, [{ show:true, label:'Opening your orchestrator…' }]);
  assert.deepEqual(openedUrls, [
    'http://127.0.0.1:8086/api/stream?device_id=test-device&chat_id=guide-chat',
  ]);
});


test('the onboarding exit waits for a real guide chat and blocks duplicate clicks', () => {
  const canEnter = extractFunction('_onbCanEnterApp');
  assert.equal(canEnter(null), false);
  assert.equal(canEnter({ guideWin:'chat', guideChatId:'', guideOpenBusy:false }), false);
  assert.equal(canEnter({ guideWin:'chat', guideChatId:'guide-chat', guideOpenBusy:true }), false);
  assert.equal(canEnter({ guideWin:'chat', guideChatId:'guide-chat', enteringApp:true }), false);
  assert.equal(canEnter({ guideWin:'chat', guideChatId:'guide-chat' }), true);

  const paintStart = source.indexOf('function _onbPaintGuide(orch)');
  const paintEnd = source.indexOf('\n  function _onbParkZip()', paintStart);
  assert.ok(paintStart >= 0 && paintEnd > paintStart);
  const paintSource = source.slice(paintStart, paintEnd);
  assert.match(paintSource, /const enterReady = _onbCanEnterApp\(_onb\)/);
  assert.match(paintSource, /disabled aria-disabled="true"/);
  assert.match(paintSource, /owner\.enteringApp = true/);
  assert.match(paintSource, /enterButton\.disabled = true/);
  assert.match(paintSource, /if \(_onb !== owner\) return/);
});
