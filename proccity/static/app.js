import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = id => document.getElementById(id);
const status = $('status'), tip = $('tip'), q = $('q'), topcpu = $('topcpu'), topmem = $('topmem');
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x070a12);
scene.fog = new THREE.FogExp2(0x070a12, 0.010);
const camera = new THREE.PerspectiveCamera(55, innerWidth / innerHeight, 0.1, 600);
camera.position.set(38, 34, 38);
const renderer = new THREE.WebGLRenderer({ canvas: $('city'), antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = !reduceMotion; controls.maxPolarAngle = Math.PI / 2.05;

scene.add(new THREE.HemisphereLight(0x8090c0, 0x101018, 0.9));
const sun = new THREE.DirectionalLight(0xfff0dd, 0.6); sun.position.set(30, 50, 20); scene.add(sun);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(800, 800), new THREE.MeshStandardMaterial({ color: 0x0b0f1c, roughness: 1 }));
ground.rotation.x = -Math.PI / 2; ground.position.y = -0.01; scene.add(ground);
scene.add(new THREE.GridHelper(400, 250, 0x1b2340, 0x121a30));

// one colour per user, stable across reloads
const userHue = new Map();
function colorFor(user) {
  if (!userHue.has(user)) { let h = 0; for (const c of user) h = (h * 31 + c.charCodeAt(0)) >>> 0; userHue.set(user, (h % 360) / 360); }
  return new THREE.Color().setHSL(userHue.get(user), 0.45, 0.42);
}

const box = new THREE.BoxGeometry(1, 1, 1); box.translate(0, 0.5, 0);   // grows from the ground
const buildings = new Map();   // pid -> { mesh, target:{w,h}, data, dying }
let centered = false, needle = '';

function matches(b) { return needle && b.name.toLowerCase().includes(needle); }

function restyle(e) {
  const b = e.data, m = e.mesh.material;
  const glow = Math.min(1, b.cpu / 100);          // one full core lights the windows fully
  const dim = needle && !matches(b);
  m.color.copy(colorFor(b.user)); if (dim) m.color.multiplyScalar(0.25);
  m.emissive.setHSL(matches(b) ? 0.58 : 0.10, 0.9, dim ? 0 : 0.05 + 0.45 * glow);
  m.opacity = dim ? 0.35 : 1; m.transparent = dim;
}

function upsert(b) {
  let e = buildings.get(b.pid);
  if (!e) {
    const mesh = new THREE.Mesh(box, new THREE.MeshStandardMaterial({ roughness: .6, metalness: .1 }));
    mesh.position.set(b.x, 0, b.z); mesh.scale.set(b.width, reduceMotion ? b.height : 0.01, b.width);
    scene.add(mesh);
    e = { mesh, target: { w: b.width, h: b.height }, data: b, dying: false };
    buildings.set(b.pid, e);
  }
  e.target = { w: b.width, h: b.height }; e.data = b; e.dying = false;
  restyle(e);
}

function fmtMiB(rss) { return (rss / 2 ** 20).toFixed(0) + ' MiB'; }

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
  const p = e.mesh.position;
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
    if (!centered && snap.buildings.length) {
      const cx = snap.buildings.reduce((a, b) => a + b.x, 0) / snap.buildings.length;
      const cz = snap.buildings.reduce((a, b) => a + b.z, 0) / snap.buildings.length;
      controls.target.set(cx, 0, cz); camera.position.set(cx + 38, 34, cz + 38); centered = true;
    }
    const totalRss = snap.buildings.reduce((a, b) => a + b.rss, 0);
    status.replaceChildren('proccity · ', bold(snap.buildings.length), ' processes · ',
      bold((totalRss / 2 ** 30).toFixed(1) + ' GiB'), ' resident · ', String(snap.cores), ' cores');
    renderTop(topcpu, [...snap.buildings].sort((a, b) => b.cpu - a.cpu).slice(0, 5), b => b.cpu.toFixed(0) + '%');
    renderTop(topmem, [...snap.buildings].sort((a, b) => b.rss - a.rss).slice(0, 5), b => fmtMiB(b.rss));
  } catch (err) { status.textContent = 'proccity · server unreachable'; }
}
function bold(t) { const b = document.createElement('b'); b.textContent = String(t); return b; }
setInterval(poll, 2000); poll();

q.addEventListener('input', () => { needle = q.value.trim().toLowerCase(); for (const e of buildings.values()) restyle(e); });

// hover
const ray = new THREE.Raycaster(); const mouse = new THREE.Vector2(-2, -2); let px = 0, py = 0;
addEventListener('pointermove', ev => {
  px = ev.clientX; py = ev.clientY;
  mouse.set((px / innerWidth) * 2 - 1, -(py / innerHeight) * 2 + 1);
});
function placeTip() {
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = (px + 14 + w > innerWidth ? px - 14 - w : px + 14) + 'px';
  tip.style.top = (py + 14 + h > innerHeight ? py - 14 - h : py + 14) + 'px';
}

const clock = new THREE.Clock();
function animate() {
  const dt = Math.min(clock.getDelta(), 0.1);
  const k = reduceMotion ? 1 : 1 - Math.exp(-dt * 4);   // smooth approach, or instant
  for (const [pid, e] of buildings) {
    const s = e.mesh.scale;
    if (e.dying) {
      s.y += (0 - s.y) * (reduceMotion ? 1 : k * 2);
      if (s.y < 0.02) { scene.remove(e.mesh); e.mesh.material.dispose(); buildings.delete(pid); }
      continue;
    }
    s.y += (e.target.h - s.y) * k; s.x += (e.target.w - s.x) * k; s.z += (e.target.w - s.z) * k;
  }
  ray.setFromCamera(mouse, camera);
  const hit = ray.intersectObjects([...buildings.values()].map(e => e.mesh), false)[0];
  if (hit) {
    const e = [...buildings.values()].find(e => e.mesh === hit.object);
    const d = e.data;
    tip.textContent = `${d.name}  (pid ${d.pid})\nuser    ${d.user}\ncpu     ${d.cpu.toFixed(1)}%\nrss     ${fmtMiB(d.rss)}\nthreads ${d.threads}\nstatus  ${d.status}`;
    tip.hidden = false; placeTip();
  } else { tip.hidden = true; }
  controls.update(); renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();
addEventListener('resize', () => { camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth, innerHeight); });
