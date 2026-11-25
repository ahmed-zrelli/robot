/* Robot Monitoring Dashboard — V0 frontend logic.
 * Connects to the backend WebSocket (which currently replays the reference
 * rosbag; later this becomes the live ROS1 bridge with no frontend change
 * needed, since the wire format stays the same: {type:'msg', topic, t, data}).
 */

const WS_PATH = "/ws/telemetry";

let ws = null;
let meta = null;                 // {duration_s, speed, topics, fault_bits, fix_types, diff_sources, session_id}
let lastSeen = {};                // topic -> {receivedAt(ms), data}
let topicState = {};              // topic -> latest state used by availability/topic tracking
let availTimer = null;
let pulseEvents = [];             // recent message arrival timestamps (ms) for the pulse strip
let healthHistory = [];           // every health event seen in the CURRENT session, oldest first

const $ = (id) => document.getElementById(id);

// Operator-side UI state (display only - never sent to the backend).
const ackKeys = new Set();        // active alert keys the operator acknowledged
let selectedAlert = null;         // event object shown in the Alert details panel
const timelineSamples = [];       // 1 Hz overall-state samples for the state timeline

// ---------- connection controls ----------

$("connectBtn").addEventListener("click", connect);
$("disconnectBtn").addEventListener("click", () => disconnect("user"));
$("healthHistoryToggle").addEventListener("click", () => $("healthHistory").classList.toggle("collapsed"));
document.querySelectorAll(".filter-chip").forEach(b =>
  b.addEventListener("click", (ev) => { ev.stopPropagation(); setHistoryFilter(b.dataset.filter, b); }));
$("groupToggle").addEventListener("click", (ev) => {
  ev.stopPropagation();
  groupIncidents = !groupIncidents;
  $("groupToggle").textContent = groupIncidents ? "Grouped" : "Flat";
  $("groupToggle").classList.toggle("active", groupIncidents);
  renderHealthHistory();
});
$("groupToggle").classList.add("active");
function exportReport(format) {
  const q = meta && meta.session_id
    ? `?format=${format}&session_id=${encodeURIComponent(meta.session_id)}`
    : `?format=${format}`;
  window.location.assign(`/api/report${q}`);
}
$("exportJson").addEventListener("click", (ev) => {
  ev.stopPropagation(); ev.preventDefault(); exportReport("json");
});
$("exportHtml").addEventListener("click", (ev) => {
  ev.stopPropagation(); ev.preventDefault(); exportReport("html");
});

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

  userClosed = false;
  cancelRetry();
  setConnPill("connecting");
  ws = new WebSocket(url);

  ws.onopen = () => {
    retryAttempt = 0;
    cancelRetry();
    connectedAt = Date.now();
    setConnPill("online");
    setAvail("3d", "online");
    $("connectBtn").disabled = true;
    $("disconnectBtn").disabled = false;
    $("speedSelect").disabled = true;
    startAvailabilityLoop();
  };

  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (e) {
      console.error("dropping malformed frame:", e);
      return;
    }
    if (msg.type === "meta") {
      meta = msg;
      // A brand-new WebSocket connection = a brand-new session_id from
      // the backend. Re-scope the Event History panel to THIS session
      // only, so a fresh connect doesn't keep showing a previous
      // connection's events mixed in (each connection's info stays its
      // own; old sessions are still fetchable via session_id, just not
      // shown here automatically).
      if (msg.session_id) loadHealthHistory(msg.session_id);
      // Speed test measures the CURRENT robot link - it's meaningless in
      // bag mode (nothing there to measure), and the backend rejects it
      // anyway once not connected to a robot. Mirror that here so the
      // button reflects reality instead of failing silently into "n/a".
      setSpeedTestEnabled(msg.source === "live");
      $("footer-status").textContent = msg.source === "live"
        ? "Source: live ROS telemetry via rosbridge · every event is logged to SQLite"
        : `Source: reference rosbag replay at ${msg.speed}× · every event is logged to SQLite`;
      resetTelemetryThroughput(); // bag mode never sends `telemetry`, so clear any leftover live-mode reading from a prior connection
      renderHealthBanner(); // stats bar: show NOMINAL as soon as the stream is up
    } else if (msg.type === "meta_update") {
      // live mode only: backend periodically reports real observed
      // per-topic rates (measured from the robot's actual message
      // arrivals) once it has enough samples to know them.
      if (msg.topics && meta) { Object.assign(meta.topics, msg.topics); }
      // Telemetry throughput - real ROS data rate, computed from the same
      // arrivals as the per-topic Hz above. Deliberately separate from
      // the Network Speed Test panel/button (see index.html notes): this
      // is passive and live-updating, that one is synthetic and on-demand.
      if (msg.telemetry) {
        $("tp-bytes").textContent = formatThroughput(msg.telemetry.bytes_per_sec);
        $("tp-msgs").textContent = `${msg.telemetry.msgs_per_sec.toFixed(1)} msg/s`;
      }
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

  ws.onclose = () => {
    if (userClosed) { disconnect("closed"); return; }
    // Unexpected drop (robot link, rosbridge restart, network blip,
    // server restart): full local reset (same as a manual disconnect -
    // predictable, matches current semantics), then automatic retry with
    // backoff. Applies in both modes - a dropped bag replay restarts it.
    disconnect("dropped");
    scheduleRetry();
  };
  ws.onerror = () => {
    if (userClosed) { disconnect("error"); return; }
    disconnect("error");
    scheduleRetry();
  };
}

function disconnect(reason) {
  if (reason === "user") userClosed = true;
  cancelRetry();
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  setConnPill("offline");
  $("connectBtn").disabled = false;
  $("disconnectBtn").disabled = true;
  $("speedSelect").disabled = false;
  setSpeedTestEnabled(false); // no robot link once disconnected, in any mode
  resetTelemetryThroughput(); // stale live-mode numbers shouldn't linger after disconnect
  stopAvailabilityLoop();
  setAvail("3d", "offline");
  connectedAt = null;
  clearAllTelemetry();
}

function setConnPill(state, label) {
  const pill = $("connPill");
  pill.classList.remove("online", "offline", "retrying");
  // Red "link lost" banner only for involuntary states (drop/retry/given up),
  // never for a user-initiated disconnect.
  const banner = $("dropBanner");
  if (banner) {
    banner.hidden = !(state === "retrying" || state === "failed");
    const last = $("drop-last");
    if (last) last.textContent = lastDataWall ? new Date(lastDataWall).toLocaleTimeString() : "—";
    const hint = $("drop-hint");
    if (hint) hint.textContent = state === "failed"
      ? "Retries exhausted · check backend (uvicorn :8123) and rosbridge :9090, then press Connect"
      : `Reconnecting automatically · ${label || ""}`;
  }
  if (state === "online") {
    pill.classList.add("online");
    setConnLabel("Connected");
  } else if (state === "connecting") {
    pill.classList.add("offline");
    setConnLabel("Connecting…");
  } else if (state === "retrying") {
    pill.classList.add("retrying");
    setConnLabel(label || "Retrying…");
  } else if (state === "failed") {
    pill.classList.add("offline");
    setConnLabel("Retries exhausted — Connect to try again");
  } else {
    pill.classList.add("offline");
    setConnLabel("Disconnected");
  }
}
function setConnLabel(text) { $("connLabel").textContent = text; }

// ---------- auto-reconnect (scope reversal, was V0-excluded) ----------
// Unexpected drops retry with exponential backoff; user-initiated
// disconnects never retry. Manual override is free: Connect is usable
// during any wait (retries now), Disconnect cancels all retries.
// Telemetry resets fully on every drop (same as manual disconnect) -
// predictable beats clever here; availability goes stale naturally.
let userClosed = false;
let retryAttempt = 0;
let retryTimer = null;
let retryCountdown = null;
const RETRY_MAX = 10;

function retryDelay(attempt) {
  // 1, 2, 4, 8, 16, 30, 30, ... seconds
  return Math.min(30, 2 ** (attempt - 1));
}

function cancelRetry() {
  retryAttempt = 0;
  if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
  if (retryCountdown) { clearInterval(retryCountdown); retryCountdown = null; }
}

// Read-only introspection for the headless tests (no production use).
function getRetryState() {
  return { attempt: retryAttempt, armed: retryTimer !== null };
}

function scheduleRetry() {
  if (userClosed) return;
  retryAttempt += 1;
  if (retryAttempt > RETRY_MAX) {
    setConnPill("failed");
    return; // manual Connect remains available
  }
  const wait = retryDelay(retryAttempt);
  let left = wait;
  setConnPill("retrying", `retry ${retryAttempt}/${RETRY_MAX} in ${left}s`);
  retryCountdown = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(retryCountdown); retryCountdown = null; return; }
    setConnPill("retrying", `retry ${retryAttempt}/${RETRY_MAX} in ${left}s`);
  }, 1000);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    if (!userClosed) connect();
  }, wait * 1000);
}

// ---------- 3D robot bridge (procedural status view, robot3d.js) ----------
// app.js owns all data; robot3d.js owns all pixels. One snapshot object
// per call, fire-and-forget (the module renders on its own rAF loop).
function pushRobot3D() {
  if (!window.Robot3D) return;
  const spd = (topic) => {
    const s = lastSeen[topic];
    const v = s && s.data ? s.data.measured_velocity : NaN;
    return Number.isFinite(v) ? v : 0;
  };
  let critical = false, leftFault = false, rightFault = false;
  for (const e of activeHealth.values()) {
    if (e.severity === "critical") critical = true;
    if (e.key === "fault:left") leftFault = true;
    if (e.key === "fault:right") rightFault = true;
  }
  window.Robot3D.update({
    connected: !!(ws && ws.readyState === WebSocket.OPEN),
    critical, leftFault, rightFault,
    speeds: {
      left_front: spd("/left/front/feedback"),
      left_rear: spd("/left/rear/feedback"),
      right_front: spd("/right/front/feedback"),
      right_rear: spd("/right/rear/feedback"),
    },
  });
}

// ---------- telemetry handling ----------

function updateTopicState(topic, data, now) {
  // Keep per-topic state separate from the specialized renderers below.
  // This function must never be responsible for drawing a panel: existing
  // panels are rendered by handleTelemetry()'s switch, while availability
  // uses lastSeen/meta to determine whether a stream is healthy or quiet.
  topicState[topic] = {
    receivedAt: now,
    data,
    mode: meta && meta.topics && meta.topics[topic]
      ? meta.topics[topic].mode
      : (topic.endsWith("/cmd") ? "on_change" : "continuous"),
  };
}

function handleTelemetry(topic, data) {
  const now = performance.now();
  lastDataWall = Date.now();
  lastSeen[topic] = { receivedAt: now, data };
  updateTopicState(topic, data, now);
  pulseEvents.push(now);

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
    case "/ublox/rtk_correction": renderRtkCorrection(data); break;
    case "/left/front/cmd": renderMotorCommand("left-front", data); break;
    case "/left/rear/cmd": renderMotorCommand("left-rear", data); break;
    case "/right/front/cmd": renderMotorCommand("right-front", data); break;
    case "/right/rear/cmd": renderMotorCommand("right-rear", data); break;
    case "/coil_outputs": renderCoilOutputs(data); break;
    case "/nmea_sentence": renderNmea(data); break;
  }
  pushRobot3D(); // speeds/faults changed -> reflect in 3D view
}

function clearAllTelemetry() {
  lastSeen = {};
  topicState = {};
  pulseEvents = [];
  for (const k in motorPeak) delete motorPeak[k];
  document.querySelectorAll(".v").forEach(el => { if (el.dataset.keep !== "1") el.textContent = "--"; });
  $("batt-pct").innerHTML = `--<span class="unit">%</span>`;
  setBatteryRing(null);
  setStat("st-batt", "—");
  setStat("st-fix", "—");
  setStat("st-rate", "—");
  setFixBadge(null, "--");
  for (const k in chartSeries) chartSeries[k].length = 0;
  drawCharts();
  ["battery", "gps", "motors", "cmd-motors", "coil-outputs", "nmea"].forEach(g => setAvail(g, "offline"));
  document.querySelectorAll(".motor-grid .readouts").forEach(el => el.innerHTML = "");
  document.querySelectorAll(".fault-chip").forEach(el => { el.textContent = "--"; el.classList.remove("fault"); });
  const nmea = $("nmea-sentence");
  if (nmea) nmea.textContent = "--";
  activeHealth.clear();
  ackKeys.clear();
  selectedAlert = null;
  showAlertDetail(null);
  timelineSamples.length = 0;
  drawTimeline();
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
    ackKeys.delete(ev.key); // a cleared alert needs no acknowledgement; a refire must be re-acked
  }
  renderHealthBanner();
  if (selectedAlert && selectedAlert.key === ev.key) showAlertDetail(selectedAlert);

  // Unlike activeHealth (cleared on `state:"cleared"`), history keeps
  // every event forever - this is what actually answers "what happened
  // earlier that already went away".
  healthHistory.push({ ...ev, logged_at: Date.now() / 1000 });
  renderHealthHistory();
}

function eventRowHtml(e) {
  const hidx = healthHistory.indexOf(e);
  const sel = selectedAlert && selectedAlert === e ? " selected" : "";
  return `
    <li class="${e.severity}${e.kind === "failure" ? " failure-row" : ""}${e.state === "cleared" ? " cleared" : ""}${sel}" data-hidx="${hidx}">
      <span class="hl-dot"></span>
      <span class="hl-time">${fmtClock(e.logged_at)}</span>
      <span class="hl-signal">${escapeHtml(e.signal)}</span>
      <span class="hl-state">${escapeHtml(e.state)}</span>
      <span class="hl-msg">${escapeHtml(e.message)}</span>
    </li>
  `;
}

// ---------- incident grouping (display-only) ----------
// Detectors never negotiate (see handover) - but the dashboard may present
// co-active fault-family events as one incident card. This is a VIEW over
// healthHistory: the underlying array keeps every raw event, golden counts
// and the backend are untouched, and the toggle below switches back to the
// flat list. Family = hardware_fault + ml + their failure escalations
// (linked by key prefix, not by timing). Two family keys join one incident
// when their active intervals overlap or chain with gaps <=
// INCIDENT_BRIDGE_S (covers blip-split episodes like the 3357.5s fault=0
// reports without merging distinct episodes).
let groupIncidents = true;
const INCIDENT_BRIDGE_S = 60;
const INCIDENT_KINDS = new Set(["hardware_fault", "ml", "failure"]);

function parentKey(key) {
  return key.startsWith("failure:") ? key.slice("failure:".length) : key;
}

function buildIncidents(events) {
  // Collect active intervals per family key (failures map to their parent
  // key), then chain intervals across ALL keys by start time: a chain
  // extends while the next interval starts within INCIDENT_BRIDGE_S of the
  // chain's running end (open intervals extend to Infinity). Chaining over
  // intervals rather than keys means the same fault key refiring after a
  // long quiet stretch correctly opens a new incident, while blip-split
  // episodes (fault=0 reports mid-fault) stay merged.
  const perKey = new Map(); // ORIGINAL key -> [[start, end|null]]
  events.forEach((e) => {
    if (!INCIDENT_KINDS.has(e.kind)) return;
    const k = e.key; // pair new/cleared within the original key only -
    if (e.state === "new") { // failures share parents for CHAINING (below),
      if (!perKey.has(k)) perKey.set(k, []); // never for pairing: mixing
      perKey.get(k).push([e.logged_at, null]); // keys in one LIFO stack
    } else if (e.state === "cleared" && perKey.has(k)) { // orphans the
      const arr = perKey.get(k); // earlier new (proven by test)
      if (arr.length && arr[arr.length - 1][1] === null) arr[arr.length - 1][1] = e.logged_at;
    }
  });
  const all = [];
  for (const [k, arr] of perKey) {
    // map failures to their parent key for chaining (only here, never above)
    const ck = k.startsWith("failure:") ? parentKey(k) : k;
    for (const [s, e] of arr) all.push({ key: ck, s, e });
  }
  all.sort((a, b) => a.s - b.s);
  const chains = [];
  for (const iv of all) {
    const last = chains[chains.length - 1];
    // Bridge gap: from the running end for closed chains; from the most
    // recent interval START for still-open chains (an open chain otherwise
    // extends to Infinity and would swallow every later interval - e.g. a
    // fault on the other side 80 min later).
    const ref = last ? (last.end === null ? last.lastStart : last.end) : -Infinity;
    if (last && iv.s - ref <= INCIDENT_BRIDGE_S) {
      last.keys.add(iv.key);
      last.lastStart = iv.s;
      last.end = iv.e === null ? null
        : (last.end === null ? null : Math.max(last.end, iv.e));
    } else {
      chains.push({ keys: new Set([iv.key]), end: iv.e, start: iv.s,
                    lastStart: iv.s, ivs: [iv] });
      chains[chains.length - 1].start = iv.s;
    }
  }
  // member event indices: family events whose (mapped) key is in the chain
  // and whose timestamp falls inside the chain span (open end = Infinity)
  return chains.map((c) => {
    const spanEnd = c.end === null ? Infinity : c.end;
    const idx = [];
    events.forEach((e, i) => {
      if (!INCIDENT_KINDS.has(e.kind)) return;
      const k = e.kind === "failure" ? parentKey(e.key) : e.key;
      if (c.keys.has(k) && e.logged_at >= c.start && e.logged_at <= spanEnd) idx.push(i);
    });
    return { keys: c.keys, idx, start: c.start, end: c.end, open: c.end === null };
  }).sort((a, b) => a.start - b.start);
}

function incidentTitle(g, events) {
  const hw = g.idx.map(i => events[i]).find(e => e.kind === "hardware_fault" && e.state === "new");
  if (hw) {
    const m = /Controller fault:\s*([A-Z_, ]+)/.exec(hw.message || "");
    return `Fault: ${(m && m[1]) || hw.signal}`;
  }
  const ml = g.idx.map(i => events[i]).find(e => e.kind === "ml");
  if (ml) return "ML anomaly episode";
  return "Incident";
}

function incidentCardHtml(g, events) {
  const members = g.idx.map(i => events[i]);
  const witnesses = [];
  if (members.some(e => e.kind === "hardware_fault")) witnesses.push("controller");
  if (members.some(e => e.kind === "ml")) witnesses.push("ML model");
  if (members.some(e => e.kind === "failure")) witnesses.push("escalation");
  const range = g.open ? `${fmtClock(g.start)} – ongoing` : `${fmtClock(g.start)} – ${fmtClock(g.end)}`;
  return `
    <li class="incident">
      <details>
        <summary><span class="hl-dot"></span>
          <span class="hl-signal">${escapeHtml(incidentTitle(g, events))}</span>
          <span class="hl-state">${members.length} events · ${escapeHtml(witnesses.join(" + "))}</span>
          <span class="hl-msg">${escapeHtml(range)}</span>
        </summary>
        <ul class="incident-members">${members.map(eventRowHtml).join("")}</ul>
      </details>
    </li>
  `;
}

// ---------- alert triage ----------
let historyFilter = "all"; // all | critical | warning | ml | rules | failure
function filterPass(e) {
  switch (historyFilter) {
    case "critical": return e.severity === "critical";
    case "warning": return e.severity === "warning";
    case "ml": return e.kind === "ml";
    case "rules": return e.kind !== "ml";
    case "failure": return e.kind === "failure";
    default: return true;
  }
}

function setHistoryFilter(f, btn) {
  historyFilter = f;
  document.querySelectorAll(".filter-chip").forEach(b => b.classList.toggle("active", b === btn));
  renderHealthHistory();
}

function renderHealthHistory() {
  const list = $("healthHistoryList");
  $("healthHistoryCount").textContent = `${healthHistory.length} event${healthHistory.length === 1 ? "" : "s"}`;

  // Filter first (severity/kind), then group the survivors. Newest first,
  // capped in the DOM for render performance - the backend DB keeps the
  // full history regardless of what's rendered here.
  const kept = [];
  healthHistory.forEach((e, i) => { if (filterPass(e)) kept.push(i); });
  let html;
  if (groupIncidents && historyFilter !== "ml" && historyFilter !== "failure") {
    // (single-kind filters render flat - nothing to group)
    const incidents = buildIncidents(kept.map(i => healthHistory[i]));
    const memberToInc = new Map();
    incidents.forEach((g, n) => g.idx.forEach(j => memberToInc.set(kept[j], n)));
    const emitted = new Set();
    const rows = [];
    for (let k = kept.length - 1; k >= 0; k--) {
      const i = kept[k];
      const n = memberToInc.get(i);
      if (n === undefined) { rows.push(eventRowHtml(healthHistory[i])); continue; }
      if (emitted.has(n)) continue;
      emitted.add(n);
      const g = incidents[n];
      const remapped = { ...g, idx: g.idx.map(j => kept[j]) };
      rows.push(incidentCardHtml(remapped, healthHistory));
    }
    html = rows;
  } else {
    html = kept.slice(-300).reverse().map(i => eventRowHtml(healthHistory[i]));
  }
  list.innerHTML = html.slice(0, 300).join("");
  setStat("st-events", String(healthHistory.length), healthHistory.length ? "teal" : "");
}

function fmtClock(unixSeconds) {
  if (!Number.isFinite(unixSeconds)) return "--:--:--";
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
    ? `${events.length} active alert${events.length === 1 ? "" : "s"}`
    : "No active anomalies";
  const label = $("stateLabel");
  if (label) label.textContent = overall === "critical" ? "CRITICAL" : overall === "warning" ? "WARNING" : "NOMINAL";
  const empty = $("healthEmpty");
  if (empty) empty.hidden = events.length > 0;
  const p3d = $("panel3d");
  if (p3d) p3d.classList.toggle("alarm", overall === "critical");
  const live = !!(ws && ws.readyState === WebSocket.OPEN);
  renderDetectors(events, live);
  setStat("st-system", live ? overall.toUpperCase() : "—",
          overall === "critical" ? "err" : overall === "warning" ? "warn" : (live ? "ok" : ""));
  setStat("st-active", String(events.length), events.length ? (overall === "critical" ? "err" : "warn") : "ok");

  // Unacknowledged first, then critical before warning.
  const rank = (e) => (ackKeys.has(e.key) ? 2 : 0) + (e.severity === "critical" ? 0 : 1);
  events.sort((a, b) => rank(a) - rank(b));
  list.innerHTML = events.map(e => `
    <li class="${e.severity}${ackKeys.has(e.key) ? " acked" : ""}${selectedAlert && selectedAlert.key === e.key ? " selected" : ""}" data-key="${escapeHtml(e.key)}">
      <span class="hl-dot"></span>
      <span class="hl-signal">${escapeHtml(e.signal)}</span>
      <span class="hl-msg">${escapeHtml(e.message)}</span>
      ${ackKeys.has(e.key) ? '<span class="ack-tag">ACK</span>' : ""}
    </li>
  `).join("");
  const nAck = events.filter(e => ackKeys.has(e.key)).length;
  if (nAck) setStat("st-active", `${events.length} (${nAck} ack)`, events.length - nAck ? (overall === "critical" ? "err" : "warn") : "ok");
  pushRobot3D(); // fault/critical state changed -> reflect in 3D view
}

// ---- battery ----

const POWER_SUPPLY_STATUS = { 0: "Unknown", 1: "Charging", 2: "Discharging", 3: "Not charging", 4: "Full" };

function renderBattery(d) {
  // Message spec documents percentage as a 0..1 fraction, but this robot's
  // actual feed populates it already as 0..100. Auto-detect which one we
  // got rather than hard-coding an assumption that could silently invert.
  const raw = d.percentage;
  const pct = Number.isFinite(raw) ? Math.round(raw > 1.5 ? raw : raw * 100) : null;
  $("batt-pct").innerHTML = pct !== null ? `${pct}<span class="unit">%</span>` : `--<span class="unit">%</span>`;
  setBatteryRing(pct);
  setStat("st-batt", pct !== null ? `${pct} %` : "—",
          pct === null ? "" : pct < 20 ? "err" : pct < 40 ? "warn" : "ok");

  $("batt-voltage").textContent = fmt(d.voltage, "V", 1);
  $("batt-current").textContent = fmt(d.current, "A", 2);
  $("batt-capacity").textContent = fmt(d.capacity, "Ah", 1) + (Number.isFinite(d.design_capacity) ? ` / ${d.design_capacity.toFixed(1)} Ah` : "");
  $("batt-charge").textContent = fmt(d.charge, "Ah", 1);
  $("batt-status").textContent = POWER_SUPPLY_STATUS[d.power_supply_status] ?? "--";
  $("batt-health").textContent = (meta && meta.power_supply_health && d.power_supply_health in meta.power_supply_health)
    ? meta.power_supply_health[d.power_supply_health] : "--";
  $("batt-tech").textContent = (meta && meta.power_supply_technology && d.power_supply_technology in meta.power_supply_technology)
    ? meta.power_supply_technology[d.power_supply_technology] : "--";
  $("batt-present").textContent = d.present === true ? "Yes" : (d.present === false ? "No" : "--");
  const cellv = Array.isArray(d.cell_voltage) ? d.cell_voltage : [];
  $("batt-cellv").textContent = cellv.length ? cellv.map(v => v.toFixed(2)).join(", ") + " V" : "--";
  $("batt-location").textContent = d.location || "--";
  $("batt-serial").textContent = d.serial_number || "--";
}

function renderCharging(d) {
  $("batt-charging").textContent = d.data === true ? "Yes" : (d.data === false ? "No" : "--");
}

// ---- motors ----

function radsToRpm(r) { return Number.isFinite(r) ? (r * 60 / (2 * Math.PI)) : null; }

const motorPeak = {}; // motor key -> session max |motor_current|, see renderMotor

function renderMotor(key, d) {
  const el = $(`motor-${key}`);
  // Session-relative load bar: scaled to this motor's own session peak,
  // not a hand-picked ampere constant (same self-calibrating convention
  // as the backend's percentile-gap staleness). A parked-then-driven
  // session rescales itself; absolute values stay in the rows below.
  const cur = Number.isFinite(d.motor_current) ? Math.abs(d.motor_current) : 0;
  motorPeak[key] = Math.max(motorPeak[key] || 0.001, cur);
  const pct = Math.min(100, (cur / motorPeak[key]) * 100);
  const rpm = radsToRpm(d.measured_velocity);
  el.innerHTML = `
    <div class="loadbar" title="|current| vs this motor's session peak (${motorPeak[key].toFixed(1)} A)"><div class="loadbar-fill" style="width:${pct.toFixed(0)}%"></div></div>
    <div class="ro"><span class="k">Speed</span><span class="v">${rpm !== null ? rpm.toFixed(0) : "--"} <span style="color:var(--text-faint)">rpm</span></span></div>
    <div class="ro"><span class="k">Command</span><span class="v">${fmt(d.commanded_velocity, "", 2)}</span></div>
    <div class="ro"><span class="k">Current</span><span class="v">${fmt(d.motor_current, "A", 2)}</span></div>
    <div class="ro"><span class="k">Supply current</span><span class="v">${fmt(d.supply_current, "A", 2)}</span></div>
    <div class="ro"><span class="k">Power</span><span class="v">${fmt((d.motor_power ?? NaN) * 100, "%", 0)}</span></div>
    <div class="ro"><span class="k">Motor temp</span><span class="v">${fmt(d.motor_temperature, "°C", 1)}</span></div>
    <div class="ro"><span class="k">Channel temp</span><span class="v">${fmt(d.channel_temperature, "°C", 1)}</span></div>
    <div class="ro"><span class="k">Supply V</span><span class="v">${fmt(d.supply_voltage, "V", 1)}</span></div>
    <div class="ro"><span class="k">Position</span><span class="v">${fmt(d.measured_position, "rad", 1)}</span></div>
  `;
}

function decodeBits(byte, bitsMap) {
  if (!bitsMap) return [];
  const active = [];
  for (const [bit, name] of Object.entries(bitsMap)) {
    if (byte & Number(bit)) active.push(name);
  }
  return active;
}

function decodeFaultBits(faultByte) {
  return decodeBits(faultByte, meta && meta.fault_bits);
}

function renderDriverStatus(side, d) {
  $(`drv-${side}-temp`).textContent = fmt(d.ic_temperature, "°C", 1);
  $(`drv-${side}-volt`).textContent = fmt(d.internal_voltage, "V", 1);
  const adc = $(`drv-${side}-adc`);
  if (adc) adc.textContent = fmt(d.adc_voltage, "V", 2);
  const mode = $(`drv-${side}-status`);
  if (mode) {
    // `status` is a bitmask (mode bits + STALL_DETECTED/AT_LIMIT flags), not
    // a single value - can have several bits set at once, unlike `fault`
    // which the fault chips below reduce to "first active fault".
    const statusBits = decodeBits(d.status ?? 0, meta && meta.status_bits);
    mode.textContent = statusBits.length ? statusBits.join(", ") : (Number.isFinite(d.status) ? "none" : "--");
  }

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
  $("gps-lat").textContent = Number.isFinite(d.lat) ? d.lat.toFixed(7) + "°" : "--";
  $("gps-lon").textContent = Number.isFinite(d.lon) ? d.lon.toFixed(7) + "°" : "--";
  $("gps-height").textContent = fmt(d.height, "m", 2);
  $("gps-hmsl").textContent = fmt(d.hMSL, "m", 2);
  $("gps-speed").textContent = fmt(d.gSpeed, "m/s", 2);
  $("gps-velned").textContent = [d.velN, d.velE, d.velD].every(Number.isFinite)
    ? `${d.velN.toFixed(2)}, ${d.velE.toFixed(2)}, ${d.velD.toFixed(2)} m/s` : "--";
  // vehiculeHeadingValid gates vehiculeHeading per the message spec - show
  // the flag's "not valid" state rather than a heading number that isn't
  // trustworthy yet.
  $("gps-heading").textContent = d.vehiculeHeadingValid === false ? "not valid" : fmt(d.vehiculeHeading, "°", 1);
  $("gps-motion-heading").textContent = fmt(d.motionHeading, "°", 1) + (Number.isFinite(d.motionHeadingAcc) ? ` (±${d.motionHeadingAcc.toFixed(1)}°)` : "");
  $("gps-hacc").textContent = fmt(d.horAcc, "m", 3);
  $("gps-vacc").textContent = fmt(d.verAcc, "m", 3);
  $("gps-velacc").textContent = fmt(d.velAcc, "m/s", 3);
  $("gps-magdec").textContent = d.magDecValid === false ? "not valid"
    : fmt(d.magDec, "°", 2) + (Number.isFinite(d.magDecAcc) ? ` (±${d.magDecAcc.toFixed(2)}°)` : "");
}

function renderGnssStatus(d) {
  $("gps-sats").textContent = ("numSV" in d) ? d.numSV : "--";
  const fixName = (meta && meta.fix_types && d.fixType in meta.fix_types) ? meta.fix_types[d.fixType] : "--";
  setFixBadge(d.fixType, fixName);
  setStat("st-fix", fixName === "--" ? "—" : fixName.replace(/_/g, " "),
          FIX_GOOD.has(d.fixType) ? "teal" : (FIX_NONE.has(d.fixType) ? "err" : "ok"));
  const diff = d.diffMode ? ((meta && meta.diff_sources && d.diffSource in meta.diff_sources) ? meta.diff_sources[d.diffSource] : "on") : "off";
  $("gps-diff").textContent = diff;
  $("gps-carrier").textContent = (meta && meta.carrier_status && d.carrierStatus in meta.carrier_status) ? meta.carrier_status[d.carrierStatus] : "--";
}

function renderRtkCorrection(d) {
  $("gps-rtk").textContent = d === true || d.data === true ? "Available" : (d === false || d.data === false ? "No" : "--");
}

function renderMotorCommand(key, d) {
  const value = d && typeof d === "object" ? d.commanded_velocity : d;
  const el = $(`cmd-${key}`);
  if (!el) return;
  el.innerHTML = `
    <div class="ro"><span class="k">Commanded velocity</span><span class="v">${fmt(value, "", 2)}</span></div>
  `;
}

function renderCoilOutputs(d) {
  const values = Array.isArray(d) ? d : (Array.isArray(d.data) ? d.data : []);
  for (let i = 0; i < 4; i++) $(`coil-${i}`).textContent = values[i] ?? "--";
}

function renderNmea(d) {
  const value = typeof d === "string" ? d : (d && (d.sentence ?? d.data));
  $("nmea-sentence").textContent = value == null ? "--" : String(value);
}

// ---------- availability ----------

const AVAIL_GROUPS = {
  battery: ["/battery/state", "/charging"],
  gps: ["/ublox/gnss_fix", "/ublox/gnss_status", "/ublox/rtk_correction"],
  motors: ["/left/front/feedback", "/right/front/feedback", "/left/rear/feedback", "/right/rear/feedback",
           "/left_roboteq_driver/status", "/right_roboteq_driver/status"],
  "cmd-motors": ["/left/front/cmd", "/left/rear/cmd", "/right/front/cmd", "/right/rear/cmd"],
  "coil-outputs": ["/coil_outputs"],
  nmea: ["/nmea_sentence"],
};

function setAvail(group, state) {
  const el = $(`avail-${group}`);
  if (!el) return;
  el.classList.remove("online", "stale", "offline", "quiet");
  el.classList.add(state);
  el.textContent = state === "online" ? "ONLINE" : (state === "quiet" ? "QUIET" : (state === "stale" ? "STALE" : "OFFLINE"));
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
      // meta.topics[topic].hz is explicitly null right after connecting in
      // live mode (real rate isn't known yet - see main.py's "discovered"
      // handling), so `meta.topics[topic] ? ... .hz : 1` still evaluates
      // to null, not the intended 1Hz-assumed fallback, and 1000/null is
      // Infinity in JS - every topic read "online" immediately on its
      // first message instead of waiting out a sane assumed interval
      // until the first real meta_update lands ~2s later. Checked for
      // null/undefined explicitly instead of relying on truthiness.
      const rawHz = meta && meta.topics[topic] ? meta.topics[topic].hz : null;
      const hz = (rawHz === null || rawHz === undefined) ? 1 : rawHz;
      const expectedIntervalMs = (1000 / hz) / speed;
      const seen = lastSeen[topic];
      let state = "offline";
      if (seen) {
        const age = now - seen.receivedAt;
        if (meta && meta.topics && meta.topics[topic] && meta.topics[topic].mode === "on_change") {
          // Publish-on-change topics are not expected to publish continuously.
          // Silence after a valid command is therefore QUIET, never a dead/stale
          // sensor indication.
          state = age < Math.max(5000, expectedIntervalMs * 8) ? "online" : "quiet";
        } else {
          if (age < expectedIntervalMs * 3) state = "online";
          else if (age < expectedIntervalMs * 8) state = "stale";
          else state = "offline";
        };
      }
      // group state = best across its topics (online beats stale beats offline)
      if (state === "online") best = "online";
      else if (state === "stale" && best !== "online") best = "stale";
      else if (state === "quiet" && best === "offline") best = "quiet";
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

  svg.innerHTML = `<path d="${d}" fill="none" stroke="${sorted.length ? '#1FD1D1' : '#1A2B42'}" stroke-width="1.5" />`;
}

setInterval(() => { if (ws) drawPulse(); }, 120);

// ---------- speed test ----------
// Backend-proxied (browser -> backend -> speed-test target on the robot),
// deliberately matching how telemetry already works: the frontend never
// learns the robot's address, it only ever talks to its own backend. See
// backend/main.py's /api/speedtest/* routes and README "Connecting to the
// real robot" for the full reasoning. This replaced an earlier
// browser-direct version that hit a public Cloudflare endpoint as a
// placeholder before the real robot network path was confirmed.

$("speedTestBtn").addEventListener("click", runSpeedTest);

// Disabled until a live meta message confirms we're actually talking to a
// robot (see the "meta" handler above and disconnect() below) - not just
// "connected to the dashboard", which bag mode also satisfies.
setSpeedTestEnabled(false);

function setSpeedTestEnabled(enabled) {
  const btn = $("speedTestBtn");
  btn.disabled = !enabled;
  btn.title = enabled ? "" : "Connect to a live robot to run a speed test (not available in bag mode)";
}

function resetTelemetryThroughput() {
  $("tp-bytes").textContent = "-- KB/s";
  $("tp-msgs").textContent = "-- msg/s";
}

function formatThroughput(bytesPerSec) {
  // KB/s is the natural unit for the low-rate telemetry this project
  // actually sees (small JSON messages at a few Hz) - Mbps (used by the
  // Network Speed Test panel) would round tiny-but-real numbers to 0.0
  // and make it look broken when it's just genuinely low-bandwidth.
  return `${(bytesPerSec / 1000).toFixed(2)} KB/s`;
}

async function runSpeedTest() {
  const btn = $("speedTestBtn");
  const wasEnabled = !btn.disabled;
  btn.disabled = true;
  btn.textContent = "Testing…";
  $("st-down").textContent = "…";
  $("st-up").textContent = "…";
  $("st-ping").textContent = "…";

  try {
    const pingResp = await fetch("/api/speedtest/ping");
    const ping = await pingResp.json();
    $("st-ping").textContent = ping.ok ? `${ping.ms.toFixed(0)} ms` : "n/a";
    if (!ping.ok) console.error("ping test failed:", ping.error);
  } catch (e) {
    $("st-ping").textContent = "n/a";
    console.error("ping test failed:", e);
  }

  try {
    const downResp = await fetch("/api/speedtest/download");
    const down = await downResp.json();
    $("st-down").textContent = down.ok ? `${down.mbps.toFixed(1)} Mbps` : "n/a";
    if (!down.ok) console.error("download test failed:", down.error);
  } catch (e) {
    $("st-down").textContent = "n/a";
    console.error("download test failed:", e);
  }

  try {
    const upResp = await fetch("/api/speedtest/upload", { method: "POST" });
    const up = await upResp.json();
    $("st-up").textContent = up.ok ? `${up.mbps.toFixed(1)} Mbps` : "n/a";
    if (!up.ok) console.error("upload test failed:", up.error);
  } catch (e) {
    $("st-up").textContent = "n/a";
    console.error("upload test failed:", e);
  }

  btn.disabled = !wasEnabled;
  btn.textContent = "Run speed test";
}

// ---------- formatting ----------

function fmt(val, unit, decimals) {
  if (val === undefined || val === null || !Number.isFinite(val)) return "--";
  return `${val.toFixed(decimals)} ${unit}`;
}

// Health signal/message strings embed topic names, which come from the
// robot (rosapi discovery) - not trustworthy HTML. Escape before they
// reach innerHTML in the health renderers below.
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- dashboard chrome: stats bar, gauges, clock, live charts ----------
// Display-only helpers. They read the same lastSeen/activeHealth state the
// panels use and never feed anything back into detection.

var connectedAt = null;   // wall ms of the current WebSocket open (header uptime)
var lastDataWall = null;  // wall ms of the last telemetry message (link-lost banner)
const FIX_GOOD = new Set([3, 4]);    // 3D, GNSS+DR
const FIX_NONE = new Set([0, 1, 5]); // NO_FIX, DEAD_RECKONING, TIME_ONLY: no usable position

// One row per detector family in backend/health_monitor.py (event `kind`).
const DETECTORS = [
  { label: "Controller faults", kinds: ["hardware_fault"], hint: "Roboteq fault bitmask" },
  { label: "Statistical baselines", kinds: ["statistical"], hint: "rolling z-score" },
  { label: "Command vs. speed", kinds: ["residual"], hint: "slip / stall residual" },
  { label: "GNSS positioning", kinds: ["gnss"], hint: "no usable fix" },
  { label: "ML ensemble", kinds: ["ml"], hint: "Isolation Forest + AE" },
  { label: "Sensor liveness", kinds: ["sensor_health", "sensor_failure"], hint: "frozen / dead fields" },
  { label: "Topic comms", kinds: ["comms"], hint: "stale streams" },
  { label: "Failure escalation", kinds: ["failure"], hint: "persistent → capability lost" },
];

function renderDetectors(events, live) {
  const box = $("detectors");
  if (!box) return;
  box.innerHTML = DETECTORS.map(d => {
    const hits = events.filter(e => d.kinds.includes(e.kind));
    const tone = !live ? "idle" : hits.some(e => e.severity === "critical") ? "crit" : hits.length ? "warn" : "ok";
    const txt = !live ? "—" : hits.length ? `${hits.length} active` : "OK";
    return `<div class="det ${tone}" title="${escapeHtml(d.hint)}"><span class="det-dot"></span><span class="det-name">${escapeHtml(d.label)}</span><span class="det-v">${txt}</span></div>`;
  }).join("");
}

function setStat(id, text, tone) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = "stat-v" + (tone ? " " + tone : "");
}

const RING_LEN = 2 * Math.PI * 40;
function setBatteryRing(pct) {
  const ring = $("batt-ring");
  if (!ring) return;
  const p = pct === null || !Number.isFinite(pct) ? 0 : Math.max(0, Math.min(100, pct));
  ring.style.strokeDashoffset = String(RING_LEN * (1 - p / 100));
  ring.style.stroke = pct === null ? "var(--border)" : p < 20 ? "var(--err)" : p < 40 ? "var(--warn)" : "var(--ok)";
}

function setFixBadge(fixType, name) {
  const b = $("gps-fix");
  if (!b) return;
  b.textContent = name;
  b.className = "fix-badge" + (fixType === null || fixType === undefined ? ""
    : FIX_GOOD.has(fixType) ? " good" : FIX_NONE.has(fixType) ? " bad" : " mid");
  const dot = $("sky-dot");
  if (dot) dot.classList.toggle("on", fixType !== null && fixType !== undefined && !FIX_NONE.has(fixType));
}

function hms(totalS) {
  const s = Math.max(0, Math.floor(totalS));
  return [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map(n => String(n).padStart(2, "0")).join(":");
}

function tickHeader() {
  const clock = $("hdr-clock");
  if (clock) clock.textContent = new Date().toLocaleTimeString([], { hour12: false });
  const up = $("hdr-uptime");
  if (up) up.textContent = hms(connectedAt ? (Date.now() - connectedAt) / 1000 : 0);
  if (ws && ws.readyState === WebSocket.OPEN) {
    const now = performance.now();
    const n = pulseEvents.filter(t => now - t < 1000).length;
    setStat("st-rate", `${n} msg/s`, n ? "teal" : "warn");
  }
}

// Live trend charts: sampled at 2 Hz from the latest feedback messages,
// 120 samples = 60 s of wall clock (at 20x bag replay that is ~20 min of
// robot time - the axis is the operator's clock, not the recording's).
const CHART_N = 120;
const MOTOR_KEYS = ["left_front", "right_front", "left_rear", "right_rear"];
const MOTOR_TOPIC = (k) => "/" + k.replace("_", "/") + "/feedback";
const CHART_COLORS = ["#00B4B4", "#3B82F6", "#FACC15", "#A855F7"];
var chartSeries = { speed: [], current: [], batt: [] };

function sampleCharts() {
  if (!(ws && ws.readyState === WebSocket.OPEN)) return;
  const sp = [], cu = [];
  for (const k of MOTOR_KEYS) {
    const s = lastSeen[MOTOR_TOPIC(k)];
    const d = s && s.data;
    const rpm = d ? radsToRpm(d.measured_velocity) : null;
    sp.push(rpm === null ? NaN : rpm);
    cu.push(d && Number.isFinite(d.motor_current) ? Math.abs(d.motor_current) : NaN);
  }
  const b = lastSeen["/battery/state"];
  const push = (arr, v) => { arr.push(v); if (arr.length > CHART_N) arr.shift(); };
  push(chartSeries.speed, sp);
  push(chartSeries.current, cu);
  push(chartSeries.batt, [b && b.data && Number.isFinite(b.data.current) ? b.data.current : NaN]);
  drawCharts();
}

function drawChart(id, rows, colors, symmetric) {
  const cv = $(id);
  if (!cv || !cv.getContext) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 300, h = cv.clientHeight || 92;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  let lo = Infinity, hi = -Infinity;
  rows.forEach(r => r.forEach(v => { if (Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
  if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
  if (symmetric) { const m = Math.max(Math.abs(lo), Math.abs(hi), 1); lo = -m; hi = m; }
  else { lo = Math.min(0, lo); if (hi - lo < 1) hi = lo + 1; }
  const padL = 34, padR = 6, padT = 6, padB = 6;
  const X = (i) => padL + (i / (CHART_N - 1)) * (w - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
  g.font = "9px Consolas, monospace";
  g.fillStyle = "#6B8299";
  g.strokeStyle = "#1A2B42";
  g.lineWidth = 1;
  [lo, (lo + hi) / 2, hi].forEach(v => {
    const y = Math.round(Y(v)) + 0.5;
    g.setLineDash([3, 3]);
    g.beginPath(); g.moveTo(padL, y); g.lineTo(w - padR, y); g.stroke();
    g.setLineDash([]);
    g.fillText(Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1), 2, y + 3);
  });
  const off = CHART_N - rows.length;
  colors.forEach((c, s) => {
    g.strokeStyle = c; g.lineWidth = 1.6; g.beginPath();
    let pen = false;
    rows.forEach((r, i) => {
      const v = r[s];
      if (!Number.isFinite(v)) { pen = false; return; }
      const x = X(i + off), y = Y(v);
      if (pen) g.lineTo(x, y); else { g.moveTo(x, y); pen = true; }
    });
    g.stroke();
  });
  if (!rows.length) {
    g.fillStyle = "#3E5168"; g.font = "11px Segoe UI, sans-serif"; g.textAlign = "center";
    g.fillText("waiting for telemetry…", (w + padL) / 2, h / 2 + 4); g.textAlign = "start";
  }
}

function drawCharts() {
  drawChart("chart-speed", chartSeries.speed, CHART_COLORS, true);
  drawChart("chart-current", chartSeries.current, CHART_COLORS, false);
  drawChart("chart-batt", chartSeries.batt, ["#F97316"], false);
}

setInterval(tickHeader, 1000);
setInterval(sampleCharts, 500);
if (typeof window.addEventListener === "function") window.addEventListener("resize", drawCharts);
tickHeader();
drawCharts();
setBatteryRing(null);
renderHealthBanner();

// ---------- alert details: meaning + recommended action ----------
// Operator guidance per detector family (event `kind`) and per Roboteq fault
// bit. Written as "what to check", never as a diagnosis: rule events say
// what was observed, the operator decides what it means on site.
const CAPABILITY_LABEL = {
  mobility: "Mobility (drive train)", localization: "Localization (GNSS)",
  power: "Power (battery / supply)", supervision: "Supervision (sensor data)",
};
const KIND_LABEL = {
  hardware_fault: "Controller fault (Roboteq bitmask)", statistical: "Statistical baseline (rolling z-score)",
  residual: "Command vs. measured speed", gnss: "GNSS positioning", ml: "ML ensemble (Isolation Forest + Autoencoder)",
  sensor_health: "Sensor liveness (suspected)", sensor_failure: "Sensor liveness (proven dead)",
  comms: "Topic communications", failure: "Failure escalation",
};
const FAULT_ACTIONS = {
  UNDERVOLTAGE: ["Controller supply voltage fell below its limit - compare with the battery voltage: if the battery is fine, the problem is on the power path.",
                 "Inspect main fuse, contactor/relay, connectors and harness between battery and the controller on that side.",
                 "Check the battery under load (voltage sag) before restarting the mission."],
  OVERVOLTAGE: ["Bus voltage too high - usually regenerative braking or charger connection.",
                "Check deceleration settings, charger state and battery connection."],
  OVERHEAT: ["Reduce load or stop; let the controller cool down.",
             "Check heatsink, ventilation and ambient temperature; look for a blocked wheel causing high current."],
  SHORT_CIRCUIT: ["Stop the robot immediately and keep it disabled.",
                  "Inspect motor cables and connectors for damage before re-enabling."],
  EMERGENCY_STOP: ["Emergency-stop input is active: check physical E-stop buttons, bumpers and the safety chain wiring.",
                   "Confirm whether the stop was intended before resetting."],
  SEPEX_EXCITATION_FAULT: ["Field-excitation problem: check the motor field wiring."],
  MOSFET_FAILURE: ["Power-stage failure suspected: stop and have the controller inspected or replaced."],
  STARTUP_CONFIG_FAULT: ["Controller configuration invalid at start-up: verify controller configuration and firmware."],
};
const KIND_GUIDE = {
  hardware_fault: { means: "The motor controller itself reported a fault bit. This is the controller's own diagnosis, not an inference from the dashboard.",
    act: ["Identify the fault name below and follow its procedure.", "Faults on both sides at once usually point to a common cause (battery, main power, E-stop chain)."] },
  statistical: { means: "A value moved far outside its own recent behaviour (rolling 180 s baseline, separate for idle and driving). Unusual, not a diagnosis.",
    act: ["Open the trend charts: is it one motor (local cause: wheel, motor, cable) or all of them (common cause: battery, terrain, payload)?",
          "Voltage → check battery and power path; current → check mechanical drag or terrain; temperature → check cooling and duty cycle."] },
  residual: { means: "The measured wheel speed is not following the commanded speed: slip, stall, blocked wheel or encoder problem.",
    act: ["Check the wheel for an obstacle, mud or a steep slope (traction).", "Compare with the other wheels; if only one wheel, inspect its motor, brake and encoder cable."] },
  gnss: { means: "The receiver has no usable position (NO_FIX, dead-reckoning only or time only). 2D and GNSS+DR are normal on this robot.",
    act: ["Check sky view (buildings, trees, indoor area), antenna and cable.", "Pause GNSS-dependent navigation until the fix is back; check the RTK correction link."] },
  sensor_health: { means: "A field has been bit-identical for the whole window: possibly frozen or disconnected. Suspicion only - may be normal while idle.",
    act: ["Check the sensor's ROS node and wiring.", "If the robot is moving and the value still does not change, treat the sensor as unavailable."] },
  sensor_failure: { means: "The sensor reports a physically impossible constant (a known 'unwired' sentinel). Its values are not robot state.",
    act: ["Ignore this sensor's readings for decisions.", "Schedule repair of the sensor wiring."] },
  comms: { means: "A topic stopped publishing for longer than its own usual rhythm. Silence alone does not prove the robot is broken.",
    act: ["Check the publishing node (rosnode list / rostopic hz) and rosbridge.", "Run the network speed test: good network + no data = ROS side; bad network = link (RV55 / Ethernet)."] },
  ml: { means: "The Isolation Forest and/or Autoencoder found the joint pattern of currents and voltage drops unlike normal operation. 'Largest deviation' points at the most unusual signal.",
    act: ["Check the subsystem named in 'largest deviation' (vdrop = controller supply minus battery voltage).", "Look at rule alerts active at the same time (grouped incident) to confirm."] },
  failure: { means: "A problem persisted long enough to claim that a core capability of the robot is impaired.",
    act: ["Treat the capability below as unavailable: pause or abort the mission.", "Dispatch maintenance with this report (Export HTML)."] },
};

function alertGuidance(ev) {
  const base = KIND_GUIDE[ev.kind] || { means: "Event raised by the health monitor.", act: ["Check the raw message below."] };
  let act = base.act.slice();
  const src = ev.kind === "failure" ? (activeHealth.get(parentKey(ev.key)) || ev) : ev;
  const m = /Controller fault:\s*([A-Z_, ]+)/.exec(src.message || ev.message || "");
  if (m) {
    const names = m[1].split(",").map(s => s.trim()).filter(Boolean);
    const extra = [];
    names.forEach(n => (FAULT_ACTIONS[n] || []).forEach(a => extra.push(`${n}: ${a}`)));
    if (extra.length) act = ev.kind === "failure" ? act.concat(extra) : extra.concat(act.slice(1));
  }
  return { means: base.means, act };
}

function showAlertDetail(ev) {
  const panel = $("alertDetail");
  const body = $("ad-body"), empty = $("ad-empty"), sev = $("ad-sev");
  if (!panel || !body) return;
  if (panel.classList) panel.classList.remove("crit", "warn");
  if (!ev) { body.hidden = true; if (empty) empty.hidden = false; if (sev) sev.hidden = true; return; }
  body.hidden = false; if (empty) empty.hidden = true;
  const crit = ev.severity === "critical";
  if (panel.classList) panel.classList.add(crit ? "crit" : "warn");
  if (sev) { sev.hidden = false; sev.textContent = (ev.severity || "").toUpperCase(); sev.className = "health-badge " + (crit ? "crit" : "warn"); }
  const active = activeHealth.has(ev.key);
  $("ad-title").textContent = ev.signal || ev.key;
  const when = ev.logged_at ? fmtClock(ev.logged_at) : "";
  $("ad-meta").textContent = `${active ? "ACTIVE" : (ev.state === "cleared" ? "CLEARED" : "NOT ACTIVE")}${when ? " · " + when : ""} · key ${ev.key}`;
  $("ad-kind").textContent = KIND_LABEL[ev.kind] || ev.kind;
  $("ad-cap").textContent = ev.capability ? (CAPABILITY_LABEL[ev.capability] || ev.capability) : "none claimed";
  const g = alertGuidance(ev);
  $("ad-means").textContent = g.means;
  $("ad-action").innerHTML = g.act.map(a => `<li>${escapeHtml(a)}</li>`).join("");
  $("ad-raw").textContent = ev.message || "—";
  const btn = $("ad-ack");
  if (btn) {
    btn.hidden = !active;
    const acked = ackKeys.has(ev.key);
    btn.textContent = acked ? "✓ Acknowledged — click to undo" : "Acknowledge (operator has seen it)";
    if (btn.classList) btn.classList.toggle("acked", acked);
  }
}

function selectAlert(ev) {
  selectedAlert = ev;
  showAlertDetail(ev);
  renderHealthBanner();
  renderHealthHistory();
}

(function wireAlertClicks() {
  const act = $("healthList"), hist = $("healthHistoryList"), btn = $("ad-ack");
  if (act && act.addEventListener) act.addEventListener("click", (e) => {
    const li = e.target.closest && e.target.closest("li[data-key]");
    if (li) selectAlert(activeHealth.get(li.dataset.key) || null);
  });
  if (hist && hist.addEventListener) hist.addEventListener("click", (e) => {
    const li = e.target.closest && e.target.closest("li[data-hidx]");
    if (!li) return;
    e.stopPropagation();
    const ev = healthHistory[Number(li.dataset.hidx)];
    if (ev) selectAlert(activeHealth.get(ev.key) && ev.state === "new" ? activeHealth.get(ev.key) : ev);
  });
  if (btn && btn.addEventListener) btn.addEventListener("click", () => {
    if (!selectedAlert || !activeHealth.has(selectedAlert.key)) return;
    if (ackKeys.has(selectedAlert.key)) ackKeys.delete(selectedAlert.key); else ackKeys.add(selectedAlert.key);
    showAlertDetail(selectedAlert);
    renderHealthBanner();
  });
})();

// ---------- robot state timeline (last 10 min, 1 sample/s) ----------
const TIMELINE_N = 600;
function sampleTimeline() {
  const live = !!(ws && ws.readyState === WebSocket.OPEN);
  let s = 0; // 0 = no link
  if (live) {
    // Acknowledged ≠ resolved: acked problems still show, but dimmed, so a
    // NEW unacknowledged problem stands out against a known one.
    const evs = [...activeHealth.values()];
    const open = evs.filter(e => !ackKeys.has(e.key));
    if (open.some(e => e.severity === "critical")) s = 3;
    else if (open.length) s = 2;
    else if (evs.some(e => e.severity === "critical")) s = 5;
    else if (evs.length) s = 4;
    else s = 1;
  }
  timelineSamples.push(s);
  if (timelineSamples.length > TIMELINE_N) timelineSamples.shift();
  drawTimeline();
}
function drawTimeline() {
  const cv = $("stateTimeline");
  if (!cv || !cv.getContext) return;
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || 600, h = cv.clientHeight || 34;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  // 0 no link · 1 nominal · 2 warning · 3 critical · 4 warning (acked) · 5 critical (acked)
  const col = ["#1A2B42", "#22C55E", "#F97316", "#EF4444", "#7A4A24", "#7A2E33"];
  const frac = [0, 0.42, 0.72, 1, 0.72, 1];
  const bw = w / TIMELINE_N, off = TIMELINE_N - timelineSamples.length;
  timelineSamples.forEach((s, i) => {
    g.fillStyle = col[s];
    const hh = s === 0 ? 4 : (h - 6) * frac[s];
    g.fillRect((i + off) * bw, h - 3 - hh, Math.max(1, bw - 0.3), hh);
  });
  if (!timelineSamples.length) {
    g.fillStyle = "#3E5168"; g.font = "11px Segoe UI, sans-serif"; g.textAlign = "center";
    g.fillText("state history starts when you connect", w / 2, h / 2 + 4); g.textAlign = "start";
  }
}
setInterval(sampleTimeline, 1000);
if (typeof window.addEventListener === "function") window.addEventListener("resize", drawTimeline);
drawTimeline();
showAlertDetail(null);

// Kiosk / wall-screen use: http://<host>:8123/?autoconnect=1[&speed=20]
(function autoConnectFromUrl() {
  if (typeof URLSearchParams === "undefined" || !location.search) return;
  const q = new URLSearchParams(location.search);
  if (q.get("speed")) $("speedSelect").value = q.get("speed");
  if (q.get("autoconnect") === "1") connect();
})();

drawPulse();
