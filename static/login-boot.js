/* Door only: sign-in, first-run setup, then load the operating room. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  function getDeviceId() {
    let id = '';
    try { id = localStorage.getItem('orchestratorDeviceId') || ''; } catch {}
    if (!/^[A-Za-z0-9_-]{8,96}$/.test(id)) {
      if (window.crypto?.randomUUID) id = window.crypto.randomUUID().replace(/-/g, '');
      else id = `dev${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
      try { localStorage.setItem('orchestratorDeviceId', id); } catch {}
    }
    document.cookie = `orchestrator_device_id=${encodeURIComponent(id)}; Max-Age=${60 * 60 * 24 * 365}; SameSite=Strict; Path=/`;
    return id;
  }
  const DEVICE_ID = getDeviceId();

  (() => {
    const screen = document.getElementById('loginScreen');
    if (!screen) return;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)');
    const panel = document.getElementById('loginForm');
    const shell = panel?.closest('.login-card-shell');
    const mark = screen.querySelector('.login-watermark');
    let frame = 0, tx = innerWidth / 2, ty = innerHeight * 0.4, x = tx, y = ty;
    const clearMotion = () => {
      panel && (panel.style.transform = '');
      mark && (mark.style.transform = 'translateY(-50%)');
    };
    addEventListener('pointermove', event => { tx = event.clientX; ty = event.clientY; }, { passive: true });
    const loop = () => {
      if (screen.classList.contains('hidden') || reduce.matches) {
        frame = 0;
        if (reduce.matches) clearMotion();
        return;
      }
      x += (tx - x) * 0.09; y += (ty - y) * 0.09;
      screen.style.setProperty('--mx', x + 'px');
      screen.style.setProperty('--my', y + 'px');
      if (shell && panel) {
        const rect = shell.getBoundingClientRect();
        if (rect.width) {
          const dx = (x - (rect.left + rect.width / 2)) / rect.width;
          const dy = (y - (rect.top + rect.height / 2)) / rect.height;
          const cx = Math.max(-0.9, Math.min(0.9, dx));
          const cy = Math.max(-0.9, Math.min(0.9, dy));
          panel.style.transform = `rotateY(${cx * 6}deg) rotateX(${-cy * 5}deg)`;
          panel.style.setProperty('--cx', x - rect.left + 'px');
          panel.style.setProperty('--cy', y - rect.top + 'px');
        }
      }
      if (mark) mark.style.transform = `translateY(-50%) translate(${-(x / innerWidth - 0.5) * 36}px,${-(y / innerHeight - 0.5) * 24}px)`;
      frame = requestAnimationFrame(loop);
    };
    const start = () => { if (!frame && !reduce.matches) frame = requestAnimationFrame(loop); };
    reduce.addEventListener?.('change', () => {
      if (reduce.matches) { if (frame) cancelAnimationFrame(frame); frame = 0; clearMotion(); }
      else start();
    });
    new MutationObserver(start).observe(screen, { attributes: true, attributeFilter: ['class'] });
    if (!screen.classList.contains('hidden')) start();
  })();

  const loginBackgroundState = new Map();
  const loginSetupState = { checked: false, pending: false, minimum: 12, usernameMin: 3, usernameMax: 32 };

  function setLoginMode(pending, data = {}) {
    loginSetupState.pending = !!pending;
    loginSetupState.minimum = Number(data.minimum_password_length) || 12;
    loginSetupState.usernameMin = Number(data.username_min_length) || 3;
    loginSetupState.usernameMax = Number(data.username_max_length) || 32;
    const form = $('#loginForm'), email = $('#loginEmail'), displayName = $('#loginDisplayName');
    const password = $('#loginPassword'), confirm = $('#loginPasswordConfirm');
    if (form) form.autocomplete = pending ? 'off' : 'on';
    if (displayName && pending) displayName.value = '';
    if (email) {
      email.readOnly = false;
      email.autocomplete = pending ? 'off' : 'username';
      email.name = pending ? 'new-agent-chat-username' : 'identifier';
      email.placeholder = pending ? 'e.g. jamie or jamie@example.com' : 'Your username or email';
      if (pending) email.value = '';
    }
    if (password) {
      password.autocomplete = pending ? 'new-password' : 'current-password';
      password.name = pending ? 'new-agent-chat-password' : 'password';
      password.placeholder = pending ? 'At least 12 characters' : 'Enter your password';
      if (pending) password.value = '';
    }
    if (confirm) {
      confirm.required = !!pending;
      confirm.name = pending ? 'confirm-agent-chat-password' : 'password-confirm';
      if (pending) confirm.value = '';
    }
    $('#loginSetupNote')?.classList.toggle('hidden', !pending);
    $('#loginNameGroup')?.classList.toggle('hidden', !pending);
    $('#loginConfirmGroup')?.classList.toggle('hidden', !pending);
    $('#loginPasswordHint')?.classList.toggle('hidden', !pending);
    if ($('#loginTitle')) $('#loginTitle').textContent = pending ? 'Make it yours.' : 'Welcome back.';
    if ($('#loginIntro')) $('#loginIntro').textContent = pending
      ? 'Your installation is ready. Create your private sign-in to begin.'
      : 'Sign in to open your workspace and reconnect your agents.';
    if ($('#loginModeBadge')) $('#loginModeBadge').textContent = pending ? 'First launch · about one minute' : 'Private local access';
    if ($('#loginIdentifierLabel')) $('#loginIdentifierLabel').textContent = pending ? 'Choose a username or email' : 'Username or email';
    if ($('#loginEmailHint')) $('#loginEmailHint').textContent = pending ? 'Either one works' : 'Your account';
    if ($('#loginPasswordLabel')) $('#loginPasswordLabel').textContent = pending ? 'Create password' : 'Password';
    if ($('#loginPasswordRequirement')) $('#loginPasswordRequirement').textContent = pending ? `${loginSetupState.minimum}+ characters` : '';
    if ($('#loginSubmitText')) $('#loginSubmitText').textContent = pending ? 'Create my workspace' : 'Enter Agents Chat';
    if (pending && $('#loginError')) $('#loginError').textContent = '';
  }

  async function prepareLoginExperience() {
    if (loginSetupState.checked) return;
    loginSetupState.checked = true;
    const submit = $('#loginSubmit');
    if (submit) submit.disabled = true;
    try {
      const response = await fetch('/api/auth/setup/status', { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      setLoginMode(response.ok && !!data.pending, data);
    } catch { setLoginMode(false); }
    if (submit) submit.disabled = false;
  }

  function toggleLoginPassword(button) {
    const input = button.dataset.loginPasswordToggle === 'confirm' ? $('#loginPasswordConfirm') : $('#loginPassword');
    if (!input) return;
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    button.textContent = show ? 'Hide' : 'Show';
    button.setAttribute('aria-pressed', show ? 'true' : 'false');
    button.setAttribute('aria-label', `${show ? 'Hide' : 'Show'} ${button.dataset.loginPasswordToggle === 'confirm' ? 'confirmation ' : ''}password`);
    input.focus();
  }

  function setLoginBackgroundInert(active) {
    const screen = $('#loginScreen');
    if (!screen) return;
    if (active) {
      [...document.body.children].forEach(node => {
        if (!(node instanceof HTMLElement) || node === screen || ['SCRIPT', 'STYLE'].includes(node.tagName)) return;
        if (!loginBackgroundState.has(node)) {
          loginBackgroundState.set(node, {
            hadInert: node.hasAttribute('inert'),
            inertValue: node.getAttribute('inert'),
          });
        }
        node.setAttribute('inert', '');
      });
      return;
    }
    loginBackgroundState.forEach(({ hadInert, inertValue }, node) => {
      if (!node.isConnected) return;
      if (hadInert) node.setAttribute('inert', inertValue ?? '');
      else node.removeAttribute('inert');
    });
    loginBackgroundState.clear();
  }

  function setLoginVisible(visible) {
    const screen = $('#loginScreen');
    if (!screen) return;
    screen.classList.toggle('hidden', !visible);
    screen.classList.toggle('flex', visible);
    screen.setAttribute('aria-hidden', visible ? 'false' : 'true');
    setLoginBackgroundInert(visible);
    if (!screen.dataset.focusTrapBound) {
      screen.dataset.focusTrapBound = '1';
      screen.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab' || screen.classList.contains('hidden')) return;
        const focusable = [...screen.querySelectorAll('input:not([disabled]), button:not([disabled])')]
          .filter(el => !el.hidden && el.getClientRects().length);
        if (!focusable.length) return;
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
    }
    if (visible) {
      prepareLoginExperience();
      setTimeout(() => (loginSetupState.pending ? $('#loginDisplayName') : ($('#loginEmail')?.value ? $('#loginPassword') : $('#loginEmail')))?.focus(), 30);
    }
  }

  async function submitLogin(e) {
    e.preventDefault();
    const err = $('#loginError');
    if (err) err.textContent = '';
    const btn = $('#loginSubmit');
    if (btn) btn.disabled = true;
    const identifier = ($('#loginEmail')?.value || '').trim();
    const displayName = ($('#loginDisplayName')?.value || '').trim();
    const password = $('#loginPassword')?.value || '';
    const loginChoice = identifier.toLowerCase();
    if (loginSetupState.pending) {
      const confirmation = $('#loginPasswordConfirm')?.value || '';
      if (!displayName || displayName.length > 80) {
        if (err) err.textContent = 'Enter the name you want shown in your chats.';
        if (btn) btn.disabled = false;
        $('#loginDisplayName')?.focus();
        return;
      }
      const validUsername = /^[a-z0-9][a-z0-9._-]{2,31}$/.test(loginChoice);
      const validEmail = loginChoice.length <= 254 && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(loginChoice);
      if (!validUsername && !validEmail) {
        if (err) err.textContent = 'Choose a 3–32 character username or enter a valid email address.';
        if (btn) btn.disabled = false;
        $('#loginEmail')?.focus();
        return;
      }
      if (password.length < loginSetupState.minimum) {
        if (err) err.textContent = `Use at least ${loginSetupState.minimum} characters for your password.`;
        if (btn) btn.disabled = false;
        $('#loginPassword')?.focus();
        return;
      }
      if (password !== confirmation) {
        if (err) err.textContent = 'Those passwords do not match yet.';
        if (btn) btn.disabled = false;
        $('#loginPasswordConfirm')?.focus();
        return;
      }
    }
    let r;
    try {
      r = await fetch(loginSetupState.pending ? '/api/auth/setup' : '/api/auth/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Device-ID': DEVICE_ID },
        body: JSON.stringify(loginSetupState.pending
          ? { display_name: displayName, identifier: loginChoice, password }
          : { identifier, password }),
      });
    } catch {
      if (err) err.textContent = 'Network error — is the server reachable?';
      if (btn) btn.disabled = false;
      return;
    }
    if (btn) btn.disabled = false;
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      if (err) err.textContent = loginSetupState.pending
        ? (detail.error || 'Setup could not be completed. Please try again.')
        : 'Sign-in failed — check your username or email and password.';
      $('#loginPassword')?.focus();
      return;
    }
    location.reload();
  }

  function loadOperatingRoom() {
    const root = document.documentElement;
    const hearth = root.dataset.hearthHref;
    if (hearth && !document.querySelector('link[href*="hearth.css"]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = hearth;
      document.head.appendChild(link);
    }
    const src = root.dataset.appRuntime;
    if (!src || document.querySelector('script[src*="/static/app-runtime.js"]')) return;
    const script = document.createElement('script');
    script.src = src;
    script.async = false;
    document.body.appendChild(script);
  }

  async function bootDoor() {
    $('#loginForm')?.addEventListener('submit', submitLogin);
    $$('[data-login-password-toggle]').forEach(button => button.addEventListener('click', () => toggleLoginPassword(button)));
    let r;
    try { r = await fetch('/api/auth/me', { headers: { 'X-Device-ID': DEVICE_ID } }); }
    catch {
      setLoginVisible(true);
      const err = $('#loginError');
      if (err) err.textContent = 'Could not reach Agents Chat — check the connection and sign in again.';
      return;
    }
    if (r.ok) {
      await hydrateRoamingPrefs();
      loadOperatingRoom();
      return;
    }
    setLoginVisible(true);
  }

  async function hydrateRoamingPrefs() {
    // Apply account prefs BEFORE the operating-room script parses localStorage.
    // Doing this later forced a location.reload() that could spin the tab forever.
    try {
      const r = await fetch('/api/settings/ui-prefs', { headers: { 'X-Device-ID': DEVICE_ID } });
      if (!r.ok) return;
      const server = (await r.json())?.prefs;
      if (!server || typeof server !== 'object') return;
      for (const [key, value] of Object.entries(server)) {
        if (typeof key !== 'string' || !key) continue;
        const next = value == null ? '' : String(value);
        try {
          if (localStorage.getItem(key) !== next) localStorage.setItem(key, next);
        } catch {}
      }
      const saved = localStorage.getItem('ac-theme');
      const themes = ['dark', 'light', 'signal', 'signal-light', 'emerald'];
      const darkThemes = ['dark', 'signal', 'emerald'];
      const theme = themes.includes(saved)
        ? saved
        : (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
      const root = document.documentElement;
      root.dataset.theme = theme;
      root.classList.toggle('dark', darkThemes.includes(theme));
    } catch {}
  }

  bootDoor();
})();
