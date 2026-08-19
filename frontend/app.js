/* Robot Monitoring Dashboard — V0 frontend logic.
 * Connects to the backend WebSocket (which currently replays the reference
 * rosbag; later this becomes the live ROS2 bridge with no frontend change
 * needed, since the wire format stays the same: {type:'msg', topic, t, data}).
 */

const WS_PATH = "/ws/telemetry";

let ws = null;
let meta = null;                 // {duration_s, speed, topics, fault_bits, fix_types, diff_sources, session_id}
let lastSeen = {};                // topic -> {receivedAt(ms), data}
let availTimer = null;
let pulseEvents = [];             // recent message arrival timestamps (ms) for the pulse strip
let healthHistory = [];           // every health event seen in the CURRENT session, oldest first

const $ = (id) => document.getElementById(id);

// ---------- connection controls ----------

$("connectBtn").addEventListener("click", connect);
$("disconnectBtn").addEventListener("click", () => disconnect("user"));
$("healthHistoryToggle").addEventListener("click", () => $("healthHistory").classList.toggle("collapsed"));

loadHealthHistory(); // populate from backend log immediately, even before Connect is clicked
                      // (no session_id yet - backend defaults to the most recent session)

async function loadHealthHistory(sessionId) {
  try {
    const q = sessionId ? `session_id=${encodeURIComponent(sessionId)}` : "";
    const res = await fetch(`/api/health-history?limit=500&${q}`);
    const body = await res.json();
    healthHistory = body.events || [];
    renderHealthHistory();
  } catch (e) {
    console.error("failed to load health history:", e);
  }
}

function connect() {
  const speed = $("speedSelect").value;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}${WS_PATH}?speed=${speed}`;

  setConnPill("connecting");
  ws = new WebSocket(url);

  ws.onopen = () => {
    setConnPill("online");
    $("connectBtn").disabled = true;
    $("disconnectBtn").disabled = false;
    $("speedSelect").disabled = true;
    startAvailabilityLoop();
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "meta") {
      meta = msg;
      // A brand-new WebSocket connection = a brand-new session_id from
      // the backend. Re-scope the Event History panel to THIS session
      // only, so a fresh connect doesn't keep showing a previous
      // connection's events mixed in (each connection's info stays its
      // own; old sessions are still fetchable via session_id, just not
      // shown here automatically).
      if (msg.session_id) loadHealthHistory(msg.session_id);
    } else if (msg.type === "meta_update") {
      // live mode only: backend periodically reports real observed
      // per-topic rates (measured from the robot's actual message
      // arrivals) once it has enough samples to know them.
      if (meta) Object.assign(meta.topics, msg.topics);
    } else if (msg.type === "msg") {
      handleTelemetry(msg.topic, msg.data);
    } else if (msg.type === "health_event") {
      handleHealthEvent(msg);
    } else if (msg.type === "eof") {
      setConnLabel("End of recording");
    } else if (msg.type === "error") {
      console.error("backend error:", msg.message);
    }
  };

  ws.onclose = () => disconnect("closed");
  ws.onerror = () => disconnect("error");
}

function disconnect(reason) {
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  setConnPill("offline");
  $("connectBtn").disabled = false;
  $("disconnectBtn").disabled = true;
  $("speedSelect").disabled = false;
  stopAvailabilityLoop();
  clearAllTelemetry();
}

function setConnPill(state) {
  const pill = $("connPill");
  pill.classList.remove("online", "offline");
  if (state === "online") {
    pill.classList.add("online");
    setConnLabel("Connected");
  } else if (state === "connecting") {
    pill.classList.add("offline");
    setConnLabel("Connecting…");
  } else {
    pill.classList.add("offline");
    setConnLabel("Disconnected");
  }
}
function setConnLabel(text) { $("connLabel").textContent = text; }

// ---------- telemetry handling ----------

function handleTelemetry(topic, data) {
  lastSeen[topic] = { receivedAt: performance.now(), data };
  pulseEvents.push(performance.now());

  switch (topic) {
    case "/battery/state": renderBattery(data); break;
    case "/charging": renderCharging(data); break;
    case "/left/front/feedback": renderMotor("left_front", data); break;
    case "/right/front/feedback": renderMotor("right_front", data); break;
    case "/left/rear/feedback": renderMotor("left_rear", data); break;
    case "/right/rear/feedback": renderMotor("right_rear", data); break;
    case "/left_roboteq_driver/status": renderDriverStatus("left", data); break;
    case "/right_roboteq_driver/status": renderDriverStatus("right", data); break;
    case "/ublox/gnss_fix": renderGnssFix(data); break;
    case "/ublox/gnss_status": renderGnssStatus(data); break;
  }
}

function clearAllTelemetry() {
  lastSeen = {};
  pulseEvents = [];
  document.querySelectorAll(".v").forEach(el => { if (el.dataset.keep !== "1") el.textContent = "--"; });
  $("batt-pct").innerHTML = `--<span class="unit">%</span>`;
  $("batt-bar").style.width = "0%";
  ["battery", "gps", "motors"].forEach(g => setAvail(g, "offline"));
  document.querySelectorAll(".motor-grid .readouts").forEach(el => el.innerHTML = "");
  document.querySelectorAll(".fault-chip").forEach(el => { el.textContent = "--"; el.classList.remove("fault"); });
  activeHealth.clear();
  renderHealthBanner();

  // Event History is per-session too - clear it on disconnect so a gap
  // between disconnect and the next connect doesn't keep showing the
  // just-ended session's events. (Closes the race where health_event
  // messages could otherwise land on the OLD array during the brief
  // async window before the next session's loadHealthHistory() fetch
  // resolves and overwrites it.)
  healthHistory = [];
  renderHealthHistory();
}

// ---------- system health ----------
// See backend/health_monitor.py for exactly what this can and can't claim:
// hardware faults are real (from the controller itself); statistical flags
// mean "unusual", not "diagnosed"; nothing here predicts future failures.

const activeHealth = new Map(); // key -> event

function handleHealthEvent(ev) {
  if (ev.state === "new") {
    activeHealth.set(ev.key, ev);
  } else if (ev.state === "cleared") {
    activeHealth.delete(ev.key);
  }
  renderHealthBanner();

  // Unlike activeHealth (cleared on `state:"cleared"`), history keeps
  // every event forever - this is what actually answers "what happened
  // earlier that already went away".
  healthHistory.push({ ...ev, logged_at: Date.now() / 1000 });
  renderHealthHistory();
}

function renderHealthHistory() {
  const list = $("healthHistoryList");
  $("healthHistoryCount").textContent = `${healthHistory.length} event${healthHistory.length === 1 ? "" : "s"}`;

  // Newest first, capped in the DOM for render performance - the backend
  // DB (backend/dashboard.db, scoped to this session) keeps the full
  // history regardless of what's rendered here.
  const shown = healthHistory.slice(-300).reverse();
  list.innerHTML = shown.map(e => `
    <li class="${e.severity}${e.state === "cleared" ? " cleared" : ""}">
      <span class="hl-dot"></span>
      <span class="hl-time">${fmtClock(e.logged_at)}</span>
      <span class="hl-signal">${e.signal}</span>
      <span class="hl-state">${e.state}</span>
      <span class="hl-msg">${e.message}</span>
    </li>
  `).join("");
}

function fmtClock(unixSeconds) {
  if (!isFinite(unixSeconds)) return "--:--:--";
  return new Date(unixSeconds * 1000).toLocaleTimeString();
}

function renderHealthBanner() {
  const banner = $("healthBanner");
  const list = $("healthList");
  const events = [...activeHealth.values()];

  const overall = events.some(e => e.severity === "critical") ? "critical"
                : events.length ? "warning" : "ok";

  banner.classList.remove("ok", "warning", "critical");
  banner.classList.add(overall);
  $("healthBadge").textContent = overall.toUpperCase();
  $("healthSub").textContent = events.length
    ? `${events.length} active`
    : "No active anomalies";

  events.sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "critical" ? -1 : 1));
  list.innerHTML = events.map(e => `
    <li class="${e.severity}">
      <span class="hl-dot"></span>
      <span class="hl-signal">${e.signal}</span>
      <span class="hl-msg">${e.message}</span>
    </li>
  `).join("");
}

// ---- battery ----

const POWER_SUPPLY_STATUS = { 0: "Unknown", 1: "Charging", 2: "Discharging", 3: "Not charging", 4: "Full" };

function renderBattery(d) {
  // Message spec documents percentage as a 0..1 fraction, but this robot's
  // actual feed populates it already as 0..100. Auto-detect which one we
  // got rather than hard-coding an assumption that could silently invert.
  const raw = d.percentage;
  const pct = isFinite(raw) ? Math.round(raw > 1.5 ? raw : raw * 100) : null;
  $("batt-pct").innerHTML = pct !== null ? `${pct}<span class="unit">%</span>` : `--<span class="unit">%</span>`;
  $("batt-bar").style.width = pct !== null ? `${pct}%` : "0%";
  $("batt-bar").style.background = pct !== null && pct < 20 ? "var(--err)" : (pct !== null && pct < 40 ? "var(--warn)" : "var(--ok)");

  $("batt-voltage").textContent = fmt(d.voltage, "V", 1);
  $("batt-current").textContent = fmt(d.current, "A", 2);
  $("batt-temp").textContent = ("temperature" in d) ? fmt(d.temperature, "°C", 1) : "n/a in feed";
  $("batt-capacity").textContent = fmt(d.capacity, "Ah", 1) + (isFinite(d.design_capacity) ? ` / ${d.design_capacity.toFixed(1)} Ah` : "");
  $("batt-status").textContent = POWER_SUPPLY_STATUS[d.power_supply_status] ?? "--";
}

function renderCharging(d) {
  $("batt-charging").textContent = d.data === true ? "Yes" : (d.data === false ? "No" : "--");
}

// ---- motors ----

function radsToRpm(r) { return isFinite(r) ? (r * 60 / (2 * Math.PI)) : null; }

function renderMotor(key, d) {
  const el = $(`motor-${key}`);
  const rpm = radsToRpm(d.measured_velocity);
  el.innerHTML = `
    <div class="ro"><span class="k">Speed</span><span class="v">${rpm !== null ? rpm.toFixed(0) : "--"} <span style="color:var(--text-faint)">rpm</span></span></div>
    <div class="ro"><span class="k">Current</span><span class="v">${fmt(d.motor_current, "A", 2)}</span></div>
    <div class="ro"><span class="k">Power</span><span class="v">${fmt((d.motor_power ?? NaN) * 100, "%", 0)}</span></div>
    <div class="ro"><span class="k">Motor temp</span><span class="v">${fmt(d.motor_temperature, "°C", 1)}</span></div>
    <div class="ro"><span class="k">Supply V</span><span class="v">${fmt(d.supply_voltage, "V", 1)}</span></div>
    <div class="ro"><span class="k">Position</span><span class="v">${fmt(d.measured_position, "rad", 1)}</span></div>
  `;
}

function decodeFaultBits(faultByte) {
  if (!meta || !meta.fault_bits) return [];
  const active = [];
  for (const [bit, name] of Object.entries(meta.fault_bits)) {
    if (faultByte & Number(bit)) active.push(name);
  }
  return active;
}

function renderDriverStatus(side, d) {
  $(`drv-${side}-temp`).textContent = fmt(d.ic_temperature, "°C", 1);
  $(`drv-${side}-volt`).textContent = fmt(d.internal_voltage, "V", 1);

  const faults = decodeFaultBits(d.fault ?? 0);
  const keys = side === "left" ? ["left_front", "left_rear"] : ["right_front", "right_rear"];
  keys.forEach(k => {
    const chip = $(`fault-${k}`);
    if (faults.length === 0) {
      chip.textContent = "OK";
      chip.classList.remove("fault");
    } else {
      chip.textContent = faults[0];
      chip.classList.add("fault");
    }
  });
}

// ---- GPS ----

function renderGnssFix(d) {
  $("gps-lat").textContent = isFinite(d.lat) ? d.lat.toFixed(7) + "°" : "--";
  $("gps-lon").textContent = isFinite(d.lon) ? d.lon.toFixed(7) + "°" : "--";
  $("gps-height").textContent = fmt(d.height, "m", 2);
  $("gps-speed").textContent = fmt(d.gSpeed, "m/s", 2);
  $("gps-heading").textContent = fmt(d.vehiculeHeading, "°", 1);
  $("gps-hacc").textContent = fmt(d.horAcc, "m", 3);
}

function renderGnssStatus(d) {
  $("gps-sats").textContent = ("numSV" in d) ? d.numSV : "--";
  $("gps-fix").textContent = (meta && meta.fix_types && d.fixType in meta.fix_types) ? meta.fix_types[d.fixType] : "--";
  const diff = d.diffMode ? ((meta && meta.diff_sources && d.diffSource in meta.diff_sources) ? meta.diff_sources[d.diffSource] : "on") : "off";
  $("gps-diff").textContent = diff;
}

// ---------- availability ----------

const AVAIL_GROUPS = {
  battery: ["/battery/state", "/charging"],
  gps: ["/ublox/gnss_fix", "/ublox/gnss_status", "/ublox/rtk_correction"],
  motors: ["/left/front/feedback", "/right/front/feedback", "/left/rear/feedback", "/right/rear/feedback",
           "/left_roboteq_driver/status", "/right_roboteq_driver/status"],
};

function setAvail(group, state) {
  const el = $(`avail-${group}`);
  if (!el) return;
  el.classList.remove("online", "stale", "offline");
  el.classList.add(state);
  el.textContent = state === "online" ? "ONLINE" : (state === "stale" ? "STALE" : "OFFLINE");
}

function startAvailabilityLoop() {
  availTimer = setInterval(updateAvailability, 500);
}
function stopAvailabilityLoop() {
  if (availTimer) clearInterval(availTimer);
  availTimer = null;
  drawPulse();
}

function updateAvailability() {
  const now = performance.now();
  const speed = meta ? meta.speed : 20;

  for (const [group, topics] of Object.entries(AVAIL_GROUPS)) {
    let best = "offline";
    for (const topic of topics) {
      const hz = meta && meta.topics[topic] ? meta.topics[topic].hz : 1;
      const expectedIntervalMs = (1000 / hz) / speed;
      const seen = lastSeen[topic];
      let state = "offline";
      if (seen) {
        const age = now - seen.receivedAt;
        if (age < expectedIntervalMs * 3) state = "online";
        else if (age < expectedIntervalMs * 8) state = "stale";
        else state = "offline";
      }
      // group state = best across its topics (online beats stale beats offline)
      if (state === "online") best = "online";
      else if (state === "stale" && best !== "online") best = "stale";
    }
    setAvail(group, best);
  }

  drawPulse();
}

// ---------- pulse strip (signature element) ----------

function drawPulse() {
  const svg = $("pulseSvg");
  const now = performance.now();
  const windowMs = 4000;
  pulseEvents = pulseEvents.filter(t => now - t < windowMs);

  const w = 300, h = 34, mid = h / 2;
  let d = `M 0 ${mid}`;
  const sorted = pulseEvents.slice(-60);
  for (const t of sorted) {
    const x = w - ((now - t) / windowMs) * w;
    d += ` L ${x.toFixed(1)} ${mid} L ${x.toFixed(1)} ${(mid - 10).toFixed(1)} L ${(x + 2).toFixed(1)} ${mid}`;
  }
  d += ` L ${w} ${mid}`;

  svg.innerHTML = `<path d="${d}" fill="none" stroke="${sorted.length ? '#3ECF8E' : '#2A3138'}" stroke-width="1.5" />`;
}

setInterval(() => { if (ws) drawPulse(); }, 120);

// ---------- speed test ----------
// Best-effort, client-side, browser-driven test against a public CDN endpoint.
// This is deliberately independent of the robot telemetry stream (per plan:
// "network test" and "robot telemetry" are different things) and is a
// placeholder until the actual 4G/robot network path is confirmed.

$("speedTestBtn").addEventListener("click", runSpeedTest);

async function runSpeedTest() {
  const btn = $("speedTestBtn");
  btn.disabled = true;
  btn.textContent = "Testing…";
  $("st-down").textContent = "…";
  $("st-up").textContent = "…";
  $("st-ping").textContent = "…";

  try {
    // ping: a few small timed requests
    const pings = [];
    for (let i = 0; i < 4; i++) {
      const t0 = performance.now();
      await fetch("https://speed.cloudflare.com/__down?bytes=1000", { cache: "no-store" });
      pings.push(performance.now() - t0);
    }
    const ping = Math.min(...pings);
    $("st-ping").textContent = `${ping.toFixed(0)} ms`;

    // download: read a bounded stream for up to ~4s, measure throughput
    const bytesTarget = 20_000_000;
    const t0 = performance.now();
    const resp = await fetch(`https://speed.cloudflare.com/__down?bytes=${bytesTarget}`, { cache: "no-store" });
    const reader = resp.body.getReader();
    let received = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.length;
      const elapsed = (performance.now() - t0) / 1000;
      if (elapsed > 5) { reader.cancel(); break; }
    }
    const elapsed = (performance.now() - t0) / 1000;
    const mbps = (received * 8) / elapsed / 1_000_000;
    $("st-down").textContent = `${mbps.toFixed(1)} Mbps`;
  } catch (e) {
    $("st-down").textContent = "n/a";
    $("st-ping").textContent = "n/a";
    console.error("speed test failed:", e);
  }

  // upload test omitted for V0 - needs a confirmed upload endpoint/backend
  $("st-up").textContent = "n/a (V0)";

  btn.disabled = false;
  btn.textContent = "Run speed test";
}

// ---------- formatting ----------

function fmt(val, unit, decimals) {
  if (val === undefined || val === null || !isFinite(val)) return "--";
  return `${val.toFixed(decimals)} ${unit}`;
}

drawPulse();
