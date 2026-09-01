import * as THREE from './vendor/three.module.min.js';

const MAX_DPR = 1.5;
const STYLE_ID = 'agent-chat-topology-styles';
const FALLBACK_ACCENT = '#c6a052';
const IDLE_YAW = 0.00038;
const RIBBON_HALF = 0.028;

function ensureStyles() {
  if (document.getElementById(STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = STYLE_ID;
  style.textContent = `
    .ac-topology-renderer{position:relative;width:100%;height:100%;min-height:320px;overflow:hidden;isolation:isolate;background:radial-gradient(62% 40% at 50% -6%,color-mix(in srgb,var(--topology-accent,var(--gold,var(--brass,#c6a052))) 20%,transparent),transparent 72%),radial-gradient(115% 88% at 50% 46%,transparent 38%,rgba(2,3,6,.8) 100%),linear-gradient(180deg,#171c27 0%,#0b0e15 54%,#05060a 100%)}
    .ac-topology-renderer canvas{position:absolute;inset:0;display:block;width:100%;height:100%;touch-action:none;cursor:grab}
    .ac-topology-renderer canvas:active{cursor:grabbing}
    .ac-topology-labels{position:absolute;inset:0;z-index:2;pointer-events:none;overflow:hidden}
    .ac-topology-label{position:absolute;left:0;top:0;max-width:158px;padding:4px 9px;border:1px solid color-mix(in srgb,var(--node-color,#c6a052) 40%,rgba(255,255,255,.16));border-radius:8px;background:color-mix(in srgb,var(--bg-elev,#0e1017) 78%,var(--node-color,#c6a052));backdrop-filter:blur(12px) saturate(1.2);-webkit-backdrop-filter:blur(12px) saturate(1.2);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 10px 22px rgba(0,0,0,.28);color:var(--text,#f7f5ef);font:500 10.5px/1.25 ui-sans-serif,system-ui,sans-serif;letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;pointer-events:auto;transform:translate(-50%,-50%);transition:opacity .12s ease,border-color .12s ease,background .12s ease}
    .ac-topology-label[data-topology-primary="true"]{padding:5px 10px;font-size:11px;font-weight:600}
    .ac-topology-label:hover,.ac-topology-label:focus-visible,.ac-topology-label.is-selected{z-index:3;max-width:220px;outline:none;border-color:var(--node-color,#c6a052);box-shadow:inset 0 1px 0 rgba(255,255,255,.1),0 12px 26px rgba(0,0,0,.34),0 0 0 1px color-mix(in srgb,var(--node-color,#c6a052) 28%,transparent)}
    .ac-topology-label[hidden]{display:none}
    @media(max-width:680px){.ac-topology-renderer{min-height:360px}.ac-topology-label{max-width:108px;font-size:9px}}
  `;
  document.head.appendChild(style);
}

function capabilityBlocker() {
  if (globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return 'reduced-motion';
  if (globalThis.navigator?.connection?.saveData) return 'save-data';
  return '';
}

function asPosition(value) {
  const v = Array.isArray(value) ? value : [0, 0, 0];
  return [Number(v[0]) || 0, Number(v[1]) || 0, Number(v[2]) || 0];
}

function colorValue(value) {
  const raw = String(value || '').trim();
  if (/^#[0-9a-f]{3,8}$/i.test(raw)) {
    if (raw.length === 4 || raw.length === 5) return `#${[...raw.slice(1)].map(ch => ch + ch).join('')}`;
    return raw.slice(0, 7);
  }
  const match = raw.match(/rgba?\(\s*([0-9.]+%?)\s*[,/\s]\s*([0-9.]+%?)\s*[,/\s]\s*([0-9.]+%?)/i);
  if (!match) return '';
  const hex = match.slice(1, 4).map(part => {
    const n = part.endsWith('%') ? parseFloat(part) * 2.55 : Number(part);
    return Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
  });
  return `#${hex.join('')}`;
}

function themeAccent(el) {
  const read = node => {
    if (!node || typeof getComputedStyle !== 'function') return '';
    const style = getComputedStyle(node);
    return colorValue(style.getPropertyValue('--gold')) || colorValue(style.getPropertyValue('--brass'));
  };
  return read(el) || read(document.documentElement) || FALLBACK_ACCENT;
}

function makeEnvMap(hex) {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 64;
  const ctx = canvas.getContext('2d');
  const sky = ctx.createLinearGradient(0, 0, 0, 64);
  sky.addColorStop(0, '#2a3242');
  sky.addColorStop(0.46, '#151a24');
  sky.addColorStop(0.54, '#0b0e14');
  sky.addColorStop(1, '#04050a');
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, 128, 64);
  const cool = ctx.createRadialGradient(38, 14, 0, 38, 14, 54);
  cool.addColorStop(0, '#f2f6ff');
  cool.addColorStop(0.35, 'rgba(190,206,240,.5)');
  cool.addColorStop(1, 'rgba(20,24,32,0)');
  ctx.fillStyle = cool;
  ctx.fillRect(0, 0, 128, 64);
  const warm = ctx.createRadialGradient(96, 44, 0, 96, 44, 46);
  warm.addColorStop(0, hex);
  warm.addColorStop(0.4, 'rgba(198,160,82,.32)');
  warm.addColorStop(1, 'rgba(20,24,32,0)');
  ctx.fillStyle = warm;
  ctx.fillRect(0, 0, 128, 64);
  const texture = new THREE.CanvasTexture(canvas);
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function makeRibbonGeometry(edges, byId) {
  const positions = [];
  const others = [];
  const sides = [];
  const colors = [];
  const index = [];
  const lift = new THREE.Color(0xffffff);
  let v = 0;
  for (const edge of edges) {
    const from = byId.get(edge.from);
    const to = byId.get(edge.to);
    const [x1, y1, z1] = from.position;
    const [x2, y2, z2] = to.position;
    positions.push(x1, y1, z1, x1, y1, z1, x2, y2, z2, x2, y2, z2);
    others.push(x2, y2, z2, x2, y2, z2, x1, y1, z1, x1, y1, z1);
    sides.push(-1, 1, -1, 1);
    const a = new THREE.Color(from.color).lerp(lift, 0.22);
    const b = new THREE.Color(to.color).lerp(lift, 0.22);
    colors.push(a.r, a.g, a.b, a.r, a.g, a.b, b.r, b.g, b.b, b.r, b.g, b.b);
    index.push(v, v + 1, v + 2, v + 1, v + 3, v + 2);
    v += 4;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute('aOther', new THREE.Float32BufferAttribute(others, 3));
  geometry.setAttribute('aSide', new THREE.Float32BufferAttribute(sides, 1));
  geometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));
  geometry.setIndex(index);
  return geometry;
}

export function createTopologyStage({ host, nodes = [], edges = [], selectedId = '', onSelect = null }) {
  if (!(host instanceof HTMLElement)) throw new TypeError('Topology stage requires a host element.');
  const blocked = capabilityBlocker();
  if (blocked) {
    const error = new Error(blocked === 'save-data' ? 'Save-Data is enabled.' : 'Reduce Motion is enabled.');
    error.code = blocked;
    throw error;
  }
  ensureStyles();

  const accentHex = themeAccent(host);
  const records = nodes.slice(0, 64).map((node, index) => ({
    id: String(node?.id || `node-${index}`),
    label: String(node?.label || node?.title || `Node ${index + 1}`),
    color: colorValue(node?.color) || FALLBACK_ACCENT,
    position: asPosition(node?.position),
    primary: !!node?.primary,
    kind: String(node?.kind || ''),
  }));
  const byId = new Map(records.map(record => [record.id, record]));
  const safeEdges = edges
    .map(edge => ({ from: String(edge?.from || ''), to: String(edge?.to || '') }))
    .filter(edge => byId.has(edge.from) && byId.has(edge.to));

  host.replaceChildren();
  const root = document.createElement('div');
  root.className = 'ac-topology-renderer';
  root.style.setProperty('--topology-accent', accentHex);
  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');
  canvas.tabIndex = -1;
  const labelLayer = document.createElement('div');
  labelLayer.className = 'ac-topology-labels';
  labelLayer.setAttribute('role', 'group');
  labelLayer.setAttribute('aria-label', 'Topology nodes');
  root.append(canvas, labelLayer);
  host.appendChild(root);

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'low-power' });
  } catch (error) {
    host.replaceChildren();
    throw error;
  }
  renderer.setPixelRatio(Math.min(MAX_DPR, Math.max(1, globalThis.devicePixelRatio || 1)));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.02;
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  const envMap = makeEnvMap(accentHex);
  scene.environment = envMap;
  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 90);
  camera.position.set(0, 1.05, 16.3);
  camera.lookAt(0, 0, 0);

  const accent = new THREE.Color(accentHex);
  scene.add(new THREE.HemisphereLight(accent.clone().lerp(new THREE.Color(0xd7e4ff), 0.55), 0x16181f, 0.46));
  const keyLight = new THREE.DirectionalLight(accent.clone().lerp(new THREE.Color(0xfff4e4), 0.35), 0.92);
  keyLight.position.set(5.2, 7.4, 6.2);
  scene.add(keyLight);
  const rimLight = new THREE.DirectionalLight(0x8aa7c4, 0.38);
  rimLight.position.set(-6.2, 1.6, -4.4);
  scene.add(rimLight);

  const topology = new THREE.Group();
  topology.rotation.set(-0.08, -0.12, 0);
  scene.add(topology);

  const nodeGeometry = new THREE.SphereGeometry(0.3, 20, 14);
  const ringGeometry = new THREE.TorusGeometry(0.42, 0.018, 8, 32);
  const ringMaterial = new THREE.MeshBasicMaterial({ color: accent, transparent: true, opacity: 0.92, depthWrite: false });
  const selectRing = new THREE.Mesh(ringGeometry, ringMaterial);
  selectRing.rotation.x = Math.PI / 2;
  selectRing.visible = false;
  topology.add(selectRing);

  const meshes = new Map();
  const materials = [];
  const labelButtons = new Map();

  for (const record of records) {
    const color = new THREE.Color(record.color);
    const material = new THREE.MeshPhysicalMaterial({
      color,
      emissive: color.clone().multiplyScalar(record.primary ? 0.18 : 0.08),
      emissiveIntensity: record.primary ? 0.34 : 0.16,
      roughness: 0.38,
      metalness: 0.14,
      clearcoat: 0.62,
      clearcoatRoughness: 0.24,
      envMapIntensity: 0.9,
    });
    materials.push(material);
    const mesh = new THREE.Mesh(nodeGeometry, material);
    mesh.position.fromArray(record.position);
    mesh.scale.set(record.primary ? 1.38 : 1, record.primary ? 0.92 : 1, record.primary ? 1.38 : 1);
    mesh.userData.id = record.id;
    topology.add(mesh);
    meshes.set(record.id, mesh);

    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'ac-topology-label';
    label.dataset.topologyNode = record.id;
    label.dataset.topologyPrimary = record.primary ? 'true' : 'false';
    label.setAttribute('aria-label', `${record.label}${record.kind ? `, ${record.kind}` : ''}`);
    label.style.setProperty('--node-color', record.color);
    label.textContent = record.label;
    label.addEventListener('click', () => select(record.id, true));
    label.addEventListener('keydown', event => {
      if (!['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const labels = [...labelButtons.values()];
      const current = labels.indexOf(label);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? labels.length - 1
        : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? (current + 1) % labels.length
        : (current - 1 + labels.length) % labels.length;
      labels[next]?.focus();
      requestRender();
    });
    label.addEventListener('focus', requestRender);
    label.addEventListener('blur', requestRender);
    labelLayer.appendChild(label);
    labelButtons.set(record.id, label);
  }

  let linkGeometry = null;
  let linkMaterial = null;
  if (safeEdges.length) {
    linkGeometry = makeRibbonGeometry(safeEdges, byId);
    linkMaterial = new THREE.MeshBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.55,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    // Widen each edge perpendicular to the eye vector instead of along world Y, so a
    // link keeps its weight from every angle rather than collapsing to a sliver.
    linkMaterial.onBeforeCompile = shader => {
      shader.vertexShader = shader.vertexShader
        .replace('#include <common>', '#include <common>\nattribute vec3 aOther;\nattribute float aSide;')
        .replace('#include <project_vertex>', [
          'vec4 mvPosition = modelViewMatrix * vec4( transformed, 1.0 );',
          'vec3 mvOther = ( modelViewMatrix * vec4( aOther, 1.0 ) ).xyz;',
          'vec3 ribbonOffset = cross( mvOther - mvPosition.xyz, -mvPosition.xyz );',
          'float ribbonLen = length( ribbonOffset );',
          'if ( ribbonLen > 1e-5 ) mvPosition.xyz += ( ribbonOffset / ribbonLen ) * ' + RIBBON_HALF.toFixed(4) + ' * aSide;',
          'gl_Position = projectionMatrix * mvPosition;',
        ].join('\n'));
    };
    topology.add(new THREE.Mesh(linkGeometry, linkMaterial));
  }

  const projected = new THREE.Vector3();
  const raycaster = new THREE.Raycaster();
  const pointerNdc = new THREE.Vector2();
  const pickable = [...meshes.values()];
  let hoveredId = '';
  let currentId = '';
  let disposed = false;
  let visible = true;
  let intersecting = true;
  let frame = 0;
  let introStart = performance.now();
  let targetYaw = topology.rotation.y;
  let targetPitch = topology.rotation.x;
  let targetZ = camera.position.z;
  let lastNow = performance.now();
  let velocityYaw = 0;
  let velocityPitch = 0;
  let dragging = false;
  let pointerId = null;
  let lastX = 0;
  let lastY = 0;

  function pickAt(event) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return '';
    pointerNdc.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointerNdc.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointerNdc, camera);
    return raycaster.intersectObjects(pickable, false)[0]?.object?.userData?.id || '';
  }

  function setHovered(id) {
    if (id === hoveredId) return;
    const previous = hoveredId;
    hoveredId = id;
    for (const nodeId of [previous, id]) {
      const mesh = nodeId && meshes.get(nodeId);
      if (!mesh) continue;
      const record = byId.get(nodeId);
      const selected = nodeId === currentId;
      mesh.material.emissiveIntensity = selected ? 0.4
        : nodeId === hoveredId ? 0.3
        : (record.primary ? 0.34 : 0.16);
    }
    canvas.style.cursor = hoveredId ? 'pointer' : '';
    requestRender();
  }

  function select(id, notify = false) {
    if (!meshes.has(id)) return;
    currentId = id;
    const selectedMesh = meshes.get(id);
    selectRing.visible = true;
    selectRing.position.copy(selectedMesh.position);
    selectRing.scale.setScalar(byId.get(id)?.primary ? 1.12 : 1);
    for (const [nodeId, mesh] of meshes) {
      const selected = nodeId === id;
      const primary = byId.get(nodeId).primary;
      mesh.material.emissiveIntensity = selected ? 0.4 : (primary ? 0.34 : 0.16);
      labelButtons.get(nodeId)?.classList.toggle('is-selected', selected);
      labelButtons.get(nodeId)?.setAttribute('aria-pressed', selected ? 'true' : 'false');
    }
    requestRender();
    if (notify && typeof onSelect === 'function') onSelect(id);
  }

  function positionLabels() {
    const width = Math.max(1, root.clientWidth);
    const height = Math.max(1, root.clientHeight);
    for (const [id, mesh] of meshes) {
      mesh.getWorldPosition(projected);
      projected.project(camera);
      const label = labelButtons.get(id);
      const onScreen = projected.z > -1 && projected.z < 1 && Math.abs(projected.x) < 1.14 && Math.abs(projected.y) < 1.14;
      const show = onScreen && (byId.get(id).primary || id === currentId || id === hoveredId || document.activeElement === label);
      label.hidden = !show;
      if (!show) continue;
      label.style.transform = `translate(-50%,-50%) translate(${Math.round((projected.x * .5 + .5) * width)}px,${Math.round((-projected.y * .5 + .5) * height)}px)`;
    }
  }

  function resize() {
    if (disposed) return;
    const width = Math.max(1, host.clientWidth);
    const height = Math.max(320, host.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    requestRender();
  }

  function render(now = performance.now()) {
    frame = 0;
    if (disposed || !visible || !intersecting || document.hidden) return;
    // Every rate below is expressed per 60Hz frame and then corrected by real elapsed
    // time, so a 120Hz display and a loaded 30fps frame drift, settle and decay at the
    // same wall-clock speed. Clamped so a restored tab resumes instead of teleporting.
    const step = Math.min(3, Math.max(0.001, (now - lastNow) / 16.6667));
    lastNow = now;
    const intro = Math.min(1, (now - introStart) / 620);
    const ease = 1 - Math.pow(1 - intro, 3);
    topology.scale.setScalar(0.86 + ease * 0.14);
    const settle = 1 - Math.pow(1 - 0.16, step);
    topology.rotation.y += (targetYaw - topology.rotation.y) * settle;
    topology.rotation.x += (targetPitch - topology.rotation.x) * settle;
    camera.position.z += (targetZ - camera.position.z) * settle;
    if (!dragging) {
      targetYaw += (IDLE_YAW + velocityYaw) * step;
      targetPitch += velocityPitch * step;
      const decay = Math.pow(0.88, step);
      velocityYaw *= decay;
      velocityPitch *= decay;
      const bounded = Math.max(-0.72, Math.min(0.48, targetPitch));
      if (bounded !== targetPitch) velocityPitch = 0;
      targetPitch = bounded;
    }
    renderer.render(scene, camera);
    positionLabels();
    frame = requestAnimationFrame(render);
  }

  function requestRender() {
    if (!frame && !disposed && visible && intersecting && !document.hidden) frame = requestAnimationFrame(render);
  }

  let pressX = 0;
  let pressY = 0;
  function pointerDown(event) {
    if (disposed) return;
    pressX = event.clientX;
    pressY = event.clientY;
    dragging = true;
    pointerId = event.pointerId;
    lastX = event.clientX;
    lastY = event.clientY;
    velocityYaw = velocityPitch = 0;
    canvas.setPointerCapture?.(pointerId);
  }
  function pointerMove(event) {
    if (!dragging) { setHovered(pickAt(event)); return; }
    if (event.pointerId !== pointerId) return;
    const dx = event.clientX - lastX;
    const dy = event.clientY - lastY;
    lastX = event.clientX;
    lastY = event.clientY;
    velocityYaw = dx * 0.0032;
    velocityPitch = dy * 0.0024;
    targetYaw += velocityYaw;
    targetPitch = Math.max(-0.72, Math.min(0.48, targetPitch + velocityPitch));
    requestRender();
  }
  function pointerUp(event) {
    if (event.pointerId !== pointerId) return;
    dragging = false;
    canvas.releasePointerCapture?.(pointerId);
    pointerId = null;
    // A press that never travelled is a click, not the end of an orbit.
    if (Math.abs(event.clientX - pressX) < 4 && Math.abs(event.clientY - pressY) < 4) {
      const hit = pickAt(event);
      if (hit) select(hit, true);
    }
    requestRender();
  }
  function pointerLeave() { setHovered(''); }
  function wheel(event) {
    event.preventDefault();
    targetZ = Math.max(9.4, Math.min(25, targetZ + event.deltaY * 0.015));
    requestRender();
  }
  function visibilityChange() {
    if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
    else { lastNow = performance.now(); requestRender(); }
  }

  canvas.addEventListener('pointerdown', pointerDown);
  canvas.addEventListener('pointermove', pointerMove);
  canvas.addEventListener('pointerup', pointerUp);
  canvas.addEventListener('pointercancel', pointerUp);
  canvas.addEventListener('pointerleave', pointerLeave);
  canvas.addEventListener('wheel', wheel, { passive: false });
  document.addEventListener('visibilitychange', visibilityChange);
  const resizeObserver = typeof globalThis.ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  if (resizeObserver) resizeObserver.observe(host);
  else globalThis.addEventListener?.('resize', resize);
  const intersectionObserver = typeof globalThis.IntersectionObserver === 'function'
    ? new IntersectionObserver(entries => {
      intersecting = entries.some(entry => entry.isIntersecting);
      if (!intersecting && frame) { cancelAnimationFrame(frame); frame = 0; }
      else requestRender();
    }, { threshold: 0.01 })
    : null;
  intersectionObserver?.observe(host);

  resize();
  select(selectedId && meshes.has(selectedId) ? selectedId : records[0]?.id || '', false);
  requestRender();

  return {
    setSelected(id) { select(String(id || ''), false); },
    setVisible(next) {
      visible = !!next;
      if (!visible && frame) { cancelAnimationFrame(frame); frame = 0; }
      else requestRender();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      if (frame) cancelAnimationFrame(frame);
      resizeObserver?.disconnect();
      if (!resizeObserver) globalThis.removeEventListener?.('resize', resize);
      intersectionObserver?.disconnect();
      document.removeEventListener('visibilitychange', visibilityChange);
      canvas.removeEventListener('pointerdown', pointerDown);
      canvas.removeEventListener('pointermove', pointerMove);
      canvas.removeEventListener('pointerup', pointerUp);
      canvas.removeEventListener('pointercancel', pointerUp);
      canvas.removeEventListener('pointerleave', pointerLeave);
      canvas.removeEventListener('wheel', wheel);
      nodeGeometry.dispose();
      ringGeometry.dispose();
      ringMaterial.dispose();
      envMap.dispose();
      materials.forEach(material => material.dispose());
      linkGeometry?.dispose();
      linkMaterial?.dispose();
      renderer.dispose();
      renderer.forceContextLoss?.();
      if (root.parentNode === host) host.replaceChildren();
    },
  };
}
