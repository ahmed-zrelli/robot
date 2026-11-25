// Headless tests for the history grouping/filter logic in frontend/app.js.
// app.js is browser code; this harness stubs the DOM/network/timers,
// evals the real file, and asserts on its pure functions. Run:
//   node scratch/test_frontend_logic.cjs   (exit nonzero on failure)
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const noop = () => {};
const mkEl = () => ({
  addEventListener: noop,
  classList: { add: noop, remove: noop, toggle: noop },
  style: {},
  textContent: "",
  innerHTML: "",
  disabled: false,
  dataset: {},
  title: "",
});
global.document = {
  getElementById: () => mkEl(),
  querySelectorAll: () => [],
};
global.window = { location: { assign: noop } };
global.location = { protocol: "http:", host: "x" };
global.fetch = async () => ({ json: async () => ({ events: [] }) });
global.setInterval = () => 0;
global.WebSocket = function () {};

const src = fs.readFileSync(
  path.join(__dirname, "..", "frontend", "app.js"), "utf8");
eval(src + "\n;global.__T = { buildIncidents, parentKey, incidentTitle, filterPass, setHistoryFilter, retryDelay, scheduleRetry, cancelRetry, getRetryState };");
const T = global.__T;

let n = 0;
const ev = (t, kind, key, state, signal = "s", sev = "warning", msg = "m") =>
  ({ logged_at: t, kind, key, state, signal, severity: sev, message: msg });

// 1. overlapping hw + ml + failure -> one incident, all witnesses
{
  const evs = [
    ev(100, "hardware_fault", "fault:left", "new", "left motor driver"),
    ev(101, "ml", "ml:anomaly", "new"),
    ev(105, "failure", "failure:fault:left", "new"),
    ev(200, "hardware_fault", "fault:left", "cleared"),
    ev(201, "ml", "ml:anomaly", "cleared"),
    ev(205, "failure", "failure:fault:left", "cleared"),
    ev(300, "statistical", "stat:x", "new"),
  ];
  const g = T.buildIncidents(evs);
  assert.strictEqual(g.length, 1, "one incident, got " + g.length);
  assert.strictEqual(g[0].idx.length, 6);
  assert.ok(!g[0].open, "closed after clears");
}

// 2. blip-split merges (<60s), distant episodes split
{
  const a = [ev(100, "hardware_fault", "fault:left", "new"),
             ev(110, "hardware_fault", "fault:left", "cleared"),
             ev(150, "hardware_fault", "fault:left", "new"),
             ev(160, "hardware_fault", "fault:left", "cleared")];
  assert.strictEqual(T.buildIncidents(a).length, 1, "40s gap merges");
  const b = [...a, ev(500, "hardware_fault", "fault:left", "new"),
             ev(510, "hardware_fault", "fault:left", "cleared")];
  assert.strictEqual(T.buildIncidents(b).length, 2, "340s gap splits");
}

// 3. open interval at end stays open; different sides stay separate
{
  const evs = [ev(100, "hardware_fault", "fault:left", "new"),
               ev(5000, "hardware_fault", "fault:right", "new"),
               ev(5010, "hardware_fault", "fault:right", "cleared")];
  const g = T.buildIncidents(evs);
  assert.strictEqual(g.length, 2);
  assert.ok(g[0].open, "left still open");
  assert.ok(!g[1].open, "right closed");
}

// 4. non-family events never grouped (no incidents from stats alone)
{
  const evs = [ev(100, "statistical", "stat:x", "new"),
               ev(200, "statistical", "stat:x", "cleared")];
  assert.strictEqual(T.buildIncidents(evs).length, 0);
}

// 5. title prefers the controller fault name
{
  const evs = [ev(100, "hardware_fault", "fault:left", "new",
                   "left motor driver", "critical",
                   "Controller fault: UNDERVOLTAGE"),
               ev(101, "ml", "ml:anomaly", "new")];
  const g = T.buildIncidents(evs);
  assert.strictEqual(T.incidentTitle(g[0], evs), "Fault: UNDERVOLTAGE");
}

// 6. filter predicate (rules/ml symmetry)
{
  const stubBtn = { classList: { toggle() {} } };
  T.setHistoryFilter("rules", stubBtn);
  assert.ok(T.filterPass({ severity: "warning", kind: "statistical" }));
  assert.ok(!T.filterPass({ severity: "warning", kind: "ml" }));
  T.setHistoryFilter("ml", stubBtn);
  assert.ok(T.filterPass({ severity: "warning", kind: "ml" }));
  assert.ok(!T.filterPass({ severity: "critical", kind: "hardware_fault" }));
  T.setHistoryFilter("all", stubBtn);
  assert.ok(T.filterPass({ severity: "critical", kind: "x" }));
}
// 7. reconnect backoff: 1,2,4,8,16,30,30...; exhaustion at 10 keeps
// manual Connect available; cancel disarms.
{
  assert.deepStrictEqual([1, 2, 3, 4, 5, 6, 7].map(T.retryDelay),
                         [1, 2, 4, 8, 16, 30, 30]);
  const held = [];
  const realTimeout = global.setTimeout;
  const realInterval = global.setInterval;
  global.setTimeout = (fn) => { held.push(fn); return held.length; };
  global.clearTimeout = () => {};
  global.setInterval = () => 0;
  global.clearInterval = () => {};
  for (let i = 0; i < 10; i++) T.scheduleRetry();
  assert.strictEqual(T.getRetryState().attempt, 10);
  assert.strictEqual(T.getRetryState().armed, true);
  T.scheduleRetry(); // 11th: exhausted, no new timer, manual Connect left
  assert.strictEqual(T.getRetryState().attempt, 11);
  assert.strictEqual(held.length, 10);
  T.cancelRetry();
  assert.deepStrictEqual(T.getRetryState(), { attempt: 0, armed: false });
  global.setTimeout = realTimeout;
  global.setInterval = realInterval;
}
// 8. 3D bridge: snapshots carry speeds + fault flags; no-ops cleanly
// without the module (window.Robot3D absent on first paint).
{
  const seen = [];
  global.window.Robot3D = { update(s) { seen.push(s); } };
  handleTelemetry("/left/front/feedback", {
    measured_velocity: 10, motor_current: 1, supply_current: 0,
    supply_voltage: 52, channel_temperature: 45, motor_temperature: -4,
    commanded_velocity: 0,
  });
  assert.ok(seen.length > 0, "push on telemetry");
  const last = seen[seen.length - 1];
  assert.strictEqual(last.speeds.left_front, 10);
  assert.strictEqual(last.connected, false);
  assert.strictEqual(last.leftFault, false);
  handleHealthEvent({ key: "fault:left", kind: "hardware_fault",
    severity: "critical", signal: "s", message: "m", state: "new" });
  const after = seen[seen.length - 1];
  assert.strictEqual(after.leftFault, true);
  assert.strictEqual(after.critical, true);
  delete global.window.Robot3D;
  handleTelemetry("/left/front/feedback", { measured_velocity: 1 });
  assert.ok(true, "no-throw without module");
}
console.log("frontend logic tests ok");
