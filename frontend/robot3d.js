/* Procedural PGuard 3D status view (no mesh assets).
 *
 * Dimensions from the spec table (Table 1.1: 150 x 110 x 160 cm), Y-up,
 * meters. Body frame: FORWARD = +X (sensor mast end), LEFT = -Z,
 * RIGHT = +Z. Wheel axles run along Z, so wheels roll along X and the two
 * wheels of one SIDE share the same Z sign. (The previous revision put
 * left/right on +/-X, which made a "left" fault light the two wheels at
 * one END of the robot instead of the two on its left side.)
 *
 * Every visual encodes a LIVE signal: wheels turn at measured velocity,
 * a faulted side's wheels glow red, the beacon pulses and the hull blinks
 * red while any critical event is active. Decorative status, never a
 * measurement.
 *
 * Driven exclusively through window.Robot3D.update(snapshot) by app.js,
 * which owns all data. This module owns pixels only.
 */
import * as THREE from "three";

const container = document.getElementById("robot3d");
const canvas = document.getElementById("robot3d-canvas");
const fallback = document.getElementById("robot3d-fallback");

let renderer = null;
try {
  renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
} catch (e) {
  if (container) container.style.display = "none";
  if (fallback) fallback.hidden = false;
}

const state = {
  connected: false,
  critical: false,
  leftFault: false,
  rightFault: false,
  speeds: { left_front: 0, left_rear: 0, right_front: 0, right_rear: 0 },
};

// Wheel spots in the body frame: [x (fwd), z (right)].
export const WHEEL_SPOTS = {
  left_front:  [ 0.55, -0.62],
  left_rear:   [-0.55, -0.62],
  right_front: [ 0.55,  0.62],
  right_rear:  [-0.55,  0.62],
};

const CAM_HOME = { yaw: 0.78, pitch: 0.42, dist: 4.3 };
const cam = { ...CAM_HOME };
let autoRotate = true;

if (renderer) {
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0d0d1a);
  scene.fog = new THREE.Fog(0x0d0d1a, 7, 16);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  const target = new THREE.Vector3(0, 0.7, 0);

  // Lighting: teal key, blue fill, white rim (team dashboard palette).
  scene.add(new THREE.AmbientLight(0x99bbcc, 0.55));
  const key = new THREE.DirectionalLight(0x00c8c8, 1.25);
  key.position.set(3, 5, 3.5);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -3, right: 3, top: 3, bottom: -3, near: 0.5, far: 15 });
  scene.add(key);
  const fill = new THREE.DirectionalLight(0x3b82f6, 0.5);
  fill.position.set(-4, 2.5, -3);
  scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, 0.35);
  rim.position.set(0, 2, -5);
  scene.add(rim);

  const grid = new THREE.GridHelper(14, 28, 0x00b4b4, 0x1c3455);
  grid.material.transparent = true;
  grid.material.opacity = 0.28;
  scene.add(grid);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(14, 14), new THREE.ShadowMaterial({ opacity: 0.35 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = 0.002;
  ground.receiveShadow = true;
  scene.add(ground);

  const hullMat = new THREE.MeshStandardMaterial({ color: 0x0f2d45, metalness: 0.65, roughness: 0.32, emissive: 0xff0000, emissiveIntensity: 0 });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x0a1827, metalness: 0.5, roughness: 0.6 });
  const accentMat = new THREE.MeshStandardMaterial({ color: 0x00c8c8, metalness: 0.9, roughness: 0.15, emissive: 0x00b4b4, emissiveIntensity: 0.25 });
  const glassMat = new THREE.MeshStandardMaterial({ color: 0x07101c, metalness: 0.95, roughness: 0.08 });

  const robot = new THREE.Group();
  const add = (geo, mat, x, y, z) => {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    m.castShadow = true;
    m.receiveShadow = true;
    robot.add(m);
    return m;
  };

  add(new THREE.BoxGeometry(1.5, 0.5, 1.0), hullMat, 0, 0.58, 0);              // hull
  add(new THREE.BoxGeometry(1.515, 0.035, 1.015), accentMat, 0, 0.7, 0);       // teal trim band
  add(new THREE.BoxGeometry(1.05, 0.32, 0.78), hullMat, -0.08, 1.02, 0);        // upper deck
  add(new THREE.BoxGeometry(0.02, 0.18, 0.7), glassMat, 0.76, 0.62, 0);         // front sensor window
  add(new THREE.CylinderGeometry(0.055, 0.07, 0.34, 16), darkMat, 0.35, 1.35, 0); // mast
  add(new THREE.BoxGeometry(0.34, 0.16, 0.24), darkMat, 0.35, 1.58, 0);         // camera head
  add(new THREE.CylinderGeometry(0.075, 0.075, 0.06, 24), accentMat, 0.35, 1.69, 0); // lidar puck
  // Bumpers front / rear
  add(new THREE.BoxGeometry(0.06, 0.12, 0.9), darkMat, 0.78, 0.38, 0);
  add(new THREE.BoxGeometry(0.06, 0.12, 0.9), darkMat, -0.78, 0.38, 0);

  const beaconMat = new THREE.MeshStandardMaterial({ color: 0x330d0d, emissive: 0xff2222, emissiveIntensity: 0 });
  add(new THREE.CylinderGeometry(0.035, 0.045, 0.08, 12), beaconMat, -0.45, 1.22, 0);

  // One material per side so faulted sides glow independently.
  const mkSideMat = () => new THREE.MeshStandardMaterial({ color: 0x14171b, metalness: 0.1, roughness: 0.9, emissive: 0xff2222, emissiveIntensity: 0 });
  const leftMat = mkSideMat();
  const rightMat = mkSideMat();
  const hubMat = new THREE.MeshStandardMaterial({ color: 0x00b4b4, metalness: 0.8, roughness: 0.3 });
  const wheels = {};
  for (const [key, [x, z]] of Object.entries(WHEEL_SPOTS)) {
    const wheel = new THREE.Group();
    const tyreGeo = new THREE.CylinderGeometry(0.25, 0.25, 0.2, 28);
    tyreGeo.rotateX(Math.PI / 2); // axle along Z (left-right)
    const tyre = new THREE.Mesh(tyreGeo, key.startsWith("left") ? leftMat : rightMat);
    tyre.castShadow = true;
    wheel.add(tyre);
    // Hub spokes make rotation visible.
    const spokeGeo = new THREE.BoxGeometry(0.36, 0.05, 0.22);
    for (let i = 0; i < 3; i++) {
      const sp = new THREE.Mesh(spokeGeo, hubMat);
      sp.rotation.z = (i * Math.PI) / 3;
      wheel.add(sp);
    }
    wheel.position.set(x, 0.25, z);
    robot.add(wheel);
    wheels[key] = wheel;
  }
  scene.add(robot);

  // --- orbit controls (dependency-free: drag = orbit, wheel = zoom) ---
  let drag = null;
  canvas.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY }; canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) return;
    cam.yaw -= (e.clientX - drag.x) * 0.008;
    cam.pitch = Math.min(1.35, Math.max(0.05, cam.pitch + (e.clientY - drag.y) * 0.006));
    drag = { x: e.clientX, y: e.clientY };
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    cam.dist = Math.min(9, Math.max(2.2, cam.dist * (1 + Math.sign(e.deltaY) * 0.08)));
  }, { passive: false });

  const autoBtn = document.getElementById("r3d-auto");
  const resetBtn = document.getElementById("r3d-reset");
  if (autoBtn) autoBtn.addEventListener("click", () => {
    autoRotate = !autoRotate;
    autoBtn.classList.toggle("active", autoRotate);
  });
  if (resetBtn) resetBtn.addEventListener("click", () => Object.assign(cam, CAM_HOME));

  const fit = () => {
    const w = container.clientWidth || 600;
    const h = container.clientHeight || 340;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  window.addEventListener("resize", fit);
  if (window.ResizeObserver) new ResizeObserver(fit).observe(container);
  fit();

  let prev = performance.now();
  const tick = (now) => {
    const dt = Math.min(0.1, (now - prev) / 1000);
    prev = now;
    if (autoRotate && !drag) cam.yaw += 0.25 * dt;
    camera.position.set(
      target.x + cam.dist * Math.cos(cam.pitch) * Math.sin(cam.yaw),
      target.y + cam.dist * Math.sin(cam.pitch),
      target.z + cam.dist * Math.cos(cam.pitch) * Math.cos(cam.yaw));
    camera.lookAt(target);

    if (state.connected) {
      for (const [key, w] of Object.entries(wheels)) {
        const omega = Number(state.speeds[key]) || 0;
        // rad/s straight from the encoder; display-capped so 100+ rad/s
        // doesn't alias into a backwards-looking strobe.
        const shown = Math.sign(omega) * Math.min(Math.abs(omega), 12);
        if (Math.abs(omega) > 0.05) w.rotation.z -= shown * dt;
      }
    }
    const blink = state.connected && state.critical ? (Math.sin(now / 160) > 0 ? 1 : 0) : 0;
    hullMat.emissiveIntensity = blink * 0.45;
    beaconMat.emissiveIntensity = !state.connected ? 0 : state.critical ? 1.2 + Math.sin(now / 180) : 0.08;
    const sideGlow = (fault) => (state.connected && fault ? 0.6 + 0.4 * Math.sin(now / 200) : 0);
    leftMat.emissiveIntensity = sideGlow(state.leftFault);
    rightMat.emissiveIntensity = sideGlow(state.rightFault);
    accentMat.emissiveIntensity = state.connected ? 0.35 : 0.05;
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

window.Robot3D = {
  update(s) {
    state.connected = !!s.connected;
    state.critical = !!s.critical;
    state.leftFault = !!s.leftFault;
    state.rightFault = !!s.rightFault;
    if (s.speeds) {
      for (const k of Object.keys(state.speeds)) {
        const v = Number(s.speeds[k]);
        state.speeds[k] = Number.isFinite(v) ? v : 0;
      }
    }
  },
};
