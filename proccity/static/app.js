import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = id => document.getElementById(id);
const status = $('status'), tip = $('tip'), q = $('q'), topcpu = $('topcpu'), topmem = $('topmem');
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---- a cartoon afternoon ---------------------------------------------------------------
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x9edcff);
scene.fog = new THREE.FogExp2(0xbfe9ff, 0.006);
const camera = new THREE.PerspectiveCamera(50, innerWidth / innerHeight, 0.1, 800);
camera.position.set(38, 34, 38);
const renderer = new THREE.WebGLRenderer({ canvas: $('city'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = !reduceMotion; controls.maxPolarAngle = Math.PI / 2.1;

scene.add(new THREE.HemisphereLight(0xffffff, 0x88aa66, 1.1));
const sunLight = new THREE.DirectionalLight(0xfff4d6, 1.2); sunLight.position.set(40, 60, 20); scene.add(sunLight);

// three-step gradient map = cel shading
const toonSteps = new THREE.DataTexture(new Uint8Array([90, 170, 255]), 3, 1, THREE.RedFormat);
toonSteps.minFilter = toonSteps.magFilter = THREE.NearestFilter; toonSteps.needsUpdate = true;
const toon = (color, extra = {}) => new THREE.MeshToonMaterial({ color, gradientMap: toonSteps, ...extra });

// grass, with a lazy sun and a few clouds that are just spheres in a trench coat
const ground = new THREE.Mesh(new THREE.PlaneGeometry(1200, 1200), toon(0x7ccf7a));
ground.rotation.x = -Math.PI / 2; ground.position.y = -0.02; scene.add(ground);
const sun = new THREE.Mesh(new THREE.SphereGeometry(9, 24, 16), new THREE.MeshBasicMaterial({ color: 0xffe066 }));
sun.position.set(-160, 120, -220); scene.add(sun);
const clouds = [];
for (let i = 0; i < 9; i++) {
  const g = new THREE.Group(); const puffs = 3 + (i % 3);
  for (let j = 0; j < puffs; j++) {
    const m = new THREE.Mesh(new THREE.SphereGeometry(4 + (j % 2) * 2, 16, 12), toon(0xffffff));
    m.position.set(j * 4.5 - puffs * 2, (j % 2) * 1.5, (j % 3) - 1); g.add(m);
  }
  g.position.set(-200 + i * 50, 60 + (i % 4) * 8, -140 + (i % 3) * 60); g.userData.v = 1.5 + (i % 3);
  scene.add(g); clouds.push(g);
}

// ---- palette: pastel per user, sickly green for the undead ------------------------------
const userHue = new Map();
function hueFor(user) {
  if (!userHue.has(user)) { let h = 0; for (const c of user) h = (h * 31 + c.charCodeAt(0)) >>> 0; userHue.set(user, (h % 360) / 360); }
  return userHue.get(user);
}
// every neighbourhood (district) gets its own pastel; a laptop has one user, so colouring by
// user would paint the whole town one shade. The roof carries the user instead.
const GOLDEN = 0.61803398875;
function bodyColor(b) {
  if (b.status === 'zombie') return new THREE.Color(0x8fd18b);
  if (b.status === 'stopped') return new THREE.Color(0xb0b0b0);
  return new THREE.Color().setHSL((b.district * GOLDEN) % 1, 0.6, 0.66);
}
function roofColor(b) { return new THREE.Color().setHSL(hueFor(b.user), 0.7, 0.42); }

// ---- geometry shared by every building ---------------------------------------------------
const box = new THREE.BoxGeometry(1, 1, 1); box.translate(0, 0.5, 0);           // grows from the ground
const boxEdges = new THREE.EdgesGeometry(box);
const roofGeo = new THREE.ConeGeometry(0.78, 0.5, 4); roofGeo.rotateY(Math.PI / 4); roofGeo.translate(0, 0.25, 0);
const outlineMat = new THREE.LineBasicMaterial({ color: 0x24233a });

const buildings = new Map();   // pid -> { body, roof, target:{w,h}, data, dying, wobble }
const plates = new Map();      // district -> mesh
let centered = false, needle = '';

function matches(b) { return needle && b.name.toLowerCase().includes(needle); }

function restyle(e) {
  const b = e.data, m = e.body.material;
  const glow = Math.min(1, b.cpu / 100);          // one full core lights the windows fully
  const dim = needle && !matches(b);
  m.color.copy(bodyColor(b)); if (dim) m.color.lerp(new THREE.Color(0x9edcff), 0.7);
  m.emissive.setHSL(matches(b) ? 0.58 : 0.08, 1, dim ? 0 : 0.35 * glow);
  e.roof.material.color.copy(roofColor(b)); if (dim) e.roof.material.color.lerp(new THREE.Color(0x9edcff), 0.7);
  e.wobble = b.cpu > 60 ? Math.min(1, (b.cpu - 60) / 100) : 0;
}

function upsert(b) {
  let e = buildings.get(b.pid);
  if (!e) {
    const body = new THREE.Mesh(box, toon(0xffffff, { emissive: 0x000000 }));
    body.add(new THREE.LineSegments(boxEdges, outlineMat));
    body.position.set(b.x, 0, b.z); body.scale.set(b.width, reduceMotion ? b.height : 0.01, b.width);
    const roof = new THREE.Mesh(roofGeo, toon(0xffffff));
    roof.position.set(b.x, 0, b.z); roof.scale.set(b.width, b.width, b.width);
    scene.add(body, roof);
    e = { body, roof, target: { w: b.width, h: b.height }, data: b, dying: false, wobble: 0, born: performance.now() };
    buildings.set(b.pid, e);
  }
  e.target = { w: b.width, h: b.height }; e.data = b; e.dying = false;
  restyle(e);
}

// district plates: a slab of asphalt under each block so the city reads as blocks
function replate(list) {
  const ext = new Map();
  for (const b of list) {
    const x = ext.get(b.district) || { x0: b.x, x1: b.x, z0: b.z, z1: b.z };
    x.x0 = Math.min(x.x0, b.x); x.x1 = Math.max(x.x1, b.x); x.z0 = Math.min(x.z0, b.z); x.z1 = Math.max(x.z1, b.z);
    ext.set(b.district, x);
  }
  for (const [d, x] of ext) {
    let p = plates.get(d);
    if (!p) { p = new THREE.Mesh(new THREE.BoxGeometry(1, 0.12, 1), toon(0x5c5f73)); scene.add(p); plates.set(d, p); }
    p.position.set((x.x0 + x.x1) / 2, 0.06, (x.z0 + x.z1) / 2);
    p.scale.set(x.x1 - x.x0 + 1.9, 1, x.z1 - x.z0 + 1.9);
  }
  for (const [d, p] of plates) if (!ext.has(d)) { scene.remove(p); p.geometry.dispose(); plates.delete(d); }
}

// the mayor: whoever hoards the most memory wears the crown
const crown = new THREE.Group();
crown.add(new THREE.Mesh(new THREE.TorusGeometry(0.42, 0.09, 8, 16), new THREE.MeshBasicMaterial({ color: 0xffd23f })));
for (let i = 0; i < 5; i++) {
  const spike = new THREE.Mesh(new THREE.ConeGeometry(0.1, 0.35, 4), new THREE.MeshBasicMaterial({ color: 0xffd23f }));
  const a = (i / 5) * Math.PI * 2; spike.position.set(Math.cos(a) * 0.42, 0.18, Math.sin(a) * 0.42); crown.add(spike);
}
crown.rotation.x = Math.PI / 2; crown.visible = false; scene.add(crown);
let mayorPid = null;

// smoke puffs for buildings burning CPU
const puffPool = Array.from({ length: 90 }, () => {
  const m = new THREE.Mesh(new THREE.SphereGeometry(0.22, 8, 6), new THREE.MeshBasicMaterial({ color: 0x777788, transparent: true, opacity: 0 }));
  m.visible = false; scene.add(m); return { m, life: 0 };
});
function puff(x, y, z) {
  const p = puffPool.find(p => !p.m.visible); if (!p) return;
  p.m.visible = true; p.life = 1; p.m.position.set(x + (Math.random() - .5) * .3, y, z + (Math.random() - .5) * .3); p.m.scale.setScalar(1);
}

function fmtMiB(rss) { return (rss / 2 ** 20).toFixed(0) + ' MiB'; }
function quip(b) {
  if (b.status === 'zombie') return '🧟 undead. waiting for a parent to notice.';
  if (b.cpu >= 100) return '🔥 burning more than one whole core.';
  if (b.cpu >= 50) return '🏃 sweating.';
  if (b.rss >= 2 ** 30) return '🐘 owns more RAM than your first computer.';
  if (b.pid === mayorPid) return '👑 the Mayor. biggest landlord in town.';
  if (b.status === 'sleeping' || b.status === 'idle') return '😴 zzz.';
  if (b.threads >= 64) return '🐙 more threads than a sweater.';
  return '🙂 minding its own business.';
}

function renderTop(list, items, fmt) {
  list.replaceChildren(...items.map(b => {
    const li = document.createElement('li'), btn = document.createElement('button');
    btn.type = 'button';
    const name = document.createElement('span'), val = document.createElement('span');
    name.textContent = `${b.name} · ${b.pid}`; val.textContent = fmt(b);
    btn.append(name, val); btn.addEventListener('click', () => flyTo(b.pid)); li.append(btn);
    return li;
  }));
}

function flyTo(pid) {
  const e = buildings.get(pid); if (!e) return;
  const p = e.body.position;
  controls.target.set(p.x, e.target.h / 2, p.z);
  camera.position.set(p.x + 9, e.target.h + 8, p.z + 9);
}

async function poll() {
  try {
    const r = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    const snap = await r.json();
    const alive = new Set();
    for (const b of snap.buildings) { upsert(b); alive.add(b.pid); }
    for (const [pid, e] of buildings) if (!alive.has(pid)) e.dying = true;
    replate(snap.buildings);
    mayorPid = snap.buildings.reduce((m, b) => (b.rss > (m?.rss ?? -1) ? b : m), null)?.pid ?? null;
    if (!centered && snap.buildings.length) {
      const cx = snap.buildings.reduce((a, b) => a + b.x, 0) / snap.buildings.length;
      const cz = snap.buildings.reduce((a, b) => a + b.z, 0) / snap.buildings.length;
      controls.target.set(cx, 0, cz); camera.position.set(cx + 38, 34, cz + 38); centered = true;
      homeTarget = new THREE.Vector3(cx, 0, cz);
    }
    const hot = snap.buildings.filter(b => b.cpu >= 50).length;
    // real machine memory, not a sum of RSS (which double-counts shared pages)
    const pct = snap.mem_total ? Math.round(100 * snap.mem_used / snap.mem_total) : null;
    status.replaceChildren('🏙️ population ', bold(snap.buildings.length), ' · rent ',
      bold(pct === null ? '?' : pct + '%'), ' of ', (snap.mem_total / 2 ** 30).toFixed(0), ' GiB · ',
      String(snap.cores), ' lanes · ',
      bold(hot), hot === 1 ? ' building on fire' : ' buildings on fire');
    renderTop(topcpu, [...snap.buildings].sort((a, b) => b.cpu - a.cpu).slice(0, 5), b => b.cpu.toFixed(0) + '%');
    renderTop(topmem, [...snap.buildings].sort((a, b) => b.rss - a.rss).slice(0, 5), b => fmtMiB(b.rss));
    // poll at the server's own cadence: asking faster returns the same bytes, slower misses samples
    const want = Math.max(500, Math.round((snap.interval || 2) * 1000));
    if (want !== pollMs) { pollMs = want; clearInterval(pollTimer); pollTimer = setInterval(poll, pollMs); }
  } catch (err) { status.textContent = '🏙️ city hall is not answering (server unreachable)'; }
}
function bold(t) { const b = document.createElement('b'); b.textContent = String(t); return b; }
let pollMs = 2000, pollTimer = setInterval(poll, pollMs); poll();

q.addEventListener('input', () => { needle = q.value.trim().toLowerCase(); for (const e of buildings.values()) restyle(e); });

// ---- walking around: arrows / WASD move, Q E rotate, + - zoom, H home; same via the D-pad ---
const KEYS = { ArrowUp: 'up', KeyW: 'up', ArrowDown: 'down', KeyS: 'down', ArrowLeft: 'left', KeyA: 'left',
  ArrowRight: 'right', KeyD: 'right', KeyQ: 'rotl', KeyE: 'rotr', Equal: 'in', NumpadAdd: 'in',
  Minus: 'out', NumpadSubtract: 'out', KeyH: 'home' };
const held = new Set();
let homeTarget = null;
function pressed(k, on) {
  if (k === 'home') { if (on && homeTarget) { controls.target.copy(homeTarget); camera.position.set(homeTarget.x + 38, 34, homeTarget.z + 38); } return; }
  on ? held.add(k) : held.delete(k);
  document.querySelectorAll(`#pad button[data-k="${k}"]`).forEach(b => b.classList.toggle('held', on));
}
addEventListener('keydown', ev => {
  if (ev.target === q) return;                       // typing in the search box
  const k = KEYS[ev.code]; if (!k) return;
  ev.preventDefault(); pressed(k, true);
});
addEventListener('keyup', ev => { const k = KEYS[ev.code]; if (k) pressed(k, false); });
addEventListener('blur', () => { for (const k of [...held]) pressed(k, false); });
for (const b of document.querySelectorAll('#pad button')) {
  const k = b.dataset.k;
  b.addEventListener('pointerdown', ev => { ev.preventDefault(); b.setPointerCapture(ev.pointerId); pressed(k, true); });
  for (const evn of ['pointerup', 'pointercancel', 'lostpointercapture']) b.addEventListener(evn, () => pressed(k, false));
  b.addEventListener('keydown', ev => { if (ev.code === 'Space' || ev.code === 'Enter') { ev.preventDefault(); pressed(k, true); } });
  b.addEventListener('keyup', ev => { if (ev.code === 'Space' || ev.code === 'Enter') pressed(k, false); });
}
const _fwd = new THREE.Vector3(), _right = new THREE.Vector3(), _off = new THREE.Vector3();
function walk(dt) {
  if (!held.size) return;
  const dist = camera.position.distanceTo(controls.target);
  const speed = Math.max(6, dist * 0.6) * dt;         // farther out, faster
  camera.getWorldDirection(_fwd); _fwd.y = 0; _fwd.normalize();
  _right.crossVectors(_fwd, camera.up).normalize();
  const move = new THREE.Vector3();
  if (held.has('up')) move.add(_fwd); if (held.has('down')) move.sub(_fwd);
  if (held.has('right')) move.add(_right); if (held.has('left')) move.sub(_right);
  if (move.lengthSq()) { move.normalize().multiplyScalar(speed); camera.position.add(move); controls.target.add(move); }
  if (held.has('rotl') || held.has('rotr')) {
    _off.subVectors(camera.position, controls.target);
    _off.applyAxisAngle(camera.up, (held.has('rotl') ? 1 : -1) * 1.6 * dt);
    camera.position.copy(controls.target).add(_off);
  }
  if (held.has('in') || held.has('out')) {
    _off.subVectors(camera.position, controls.target);
    const f = held.has('in') ? Math.pow(0.35, dt) : Math.pow(1 / 0.35, dt);
    if ((f < 1 && _off.length() > 6) || (f > 1 && _off.length() < 300)) _off.multiplyScalar(f);
    camera.position.copy(controls.target).add(_off);
  }
}

// hover (and tap: touch never fires pointermove before a tap, so a tap sets the ray too)
const ray = new THREE.Raycaster(); const mouse = new THREE.Vector2(-2, -2); let px = 0, py = 0;
function aim(ev) {
  px = ev.clientX; py = ev.clientY;
  mouse.set((px / innerWidth) * 2 - 1, -(py / innerHeight) * 2 + 1);
}
addEventListener('pointermove', aim);
renderer.domElement.addEventListener('pointerdown', aim);
function placeTip() {
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = (px + 14 + w > innerWidth ? px - 14 - w : px + 14) + 'px';
  tip.style.top = (py + 14 + h > innerHeight ? py - 14 - h : py + 14) + 'px';
}

const clock = new THREE.Clock(); let t = 0;
function animate() {
  const dt = Math.min(clock.getDelta(), 0.1); t += dt;
  const k = reduceMotion ? 1 : 1 - Math.exp(-dt * 4);   // smooth approach, or instant
  for (const [pid, e] of buildings) {
    const s = e.body.scale;
    if (e.dying) {
      // cartoon death: squash out sideways, then vanish
      s.y += (0 - s.y) * (reduceMotion ? 1 : k * 2.5); s.x += (e.target.w * 1.6 - s.x) * k * 2; s.z = s.x;
      e.roof.scale.setScalar(Math.max(0.001, s.y * 0.5));
      if (s.y < 0.02) { scene.remove(e.body, e.roof); e.body.material.dispose(); e.roof.material.dispose(); buildings.delete(pid); if (pid === mayorPid) crown.visible = false; }
      continue;
    }
    // newborns overshoot a little, like a spring
    const bornAgo = (performance.now() - e.born) / 1000;
    const bounce = (!reduceMotion && bornAgo < 0.9) ? 1 + 0.18 * Math.sin(bornAgo * 9) * (1 - bornAgo) : 1;
    s.y += (e.target.h * bounce - s.y) * k; s.x += (e.target.w - s.x) * k; s.z += (e.target.w - s.z) * k;
    e.body.rotation.z = (!reduceMotion && e.wobble) ? Math.sin(t * 22 + pid) * 0.035 * e.wobble : 0;
    e.roof.position.set(e.body.position.x, s.y, e.body.position.z); e.roof.rotation.z = e.body.rotation.z;
    e.roof.scale.setScalar(s.x);
    if (!reduceMotion && e.wobble && Math.random() < dt * 6 * e.wobble) puff(e.body.position.x, s.y + 0.6, e.body.position.z);
    if (pid === mayorPid) { crown.visible = true; crown.position.set(e.body.position.x, s.y + 0.55 * s.x + 0.35, e.body.position.z); crown.rotation.z = t * 0.8; }
  }
  for (const p of puffPool) if (p.m.visible) {
    p.life -= dt / 1.6; if (p.life <= 0) { p.m.visible = false; continue; }
    p.m.position.y += dt * 1.6; p.m.scale.addScalar(dt * 1.4); p.m.material.opacity = 0.55 * p.life;
  }
  if (!reduceMotion) for (const c of clouds) { c.position.x += c.userData.v * dt; if (c.position.x > 260) c.position.x = -260; }
  ray.setFromCamera(mouse, camera);
  const hit = ray.intersectObjects([...buildings.values()].map(e => e.body), false)[0];
  if (hit) {
    const e = [...buildings.values()].find(e => e.body === hit.object);
    const d = e.data;
    tip.textContent = `${d.name}  (pid ${d.pid})\nuser    ${d.user}\ncpu     ${d.cpu.toFixed(1)}%\nrss     ${fmtMiB(d.rss)}\nthreads ${d.threads}\nstatus  ${d.status}\n\n${quip(d)}`;
    tip.hidden = false; placeTip();
  } else { tip.hidden = true; }
  walk(dt);
  controls.update(); renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
