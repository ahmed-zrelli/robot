/* Settings page: operator accounts + Security-watch trusted IPs.
 * Talks only to /api/settings* (behind the login). Every server-provided
 * string is escaped before reaching innerHTML (labels are free text). */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const when = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "—";
let state = null;

function flash(msg, ok) {
  const f = $("flash");
  f.textContent = msg;
  f.className = "flash " + (ok ? "ok" : "err");
  f.hidden = false;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => { f.hidden = true; }, 5000);
}

async function api(method, url, body) {
  const r = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && method === "GET") { location.assign("/login"); return null; }
  const data = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  return data;
}

async function load() {
  state = await api("GET", "/api/settings");
  if (!state) return;
  $("me").textContent = state.me || (state.auth_enabled ? "—" : "nobody (login off)");
  $("logoutForm").hidden = !state.auth_enabled;
  $("openWarn").hidden = state.auth_enabled;
  $("weakWarn").hidden = !state.weak_password;
  $("myAccount").hidden = !state.me;
  $("policyHint").textContent = `At least ${state.password_policy.min_length} characters, different from the username.`;
  const admin = state.role === "admin";
  $("me").textContent += state.role && state.auth_enabled ? ` (${state.role})` : "";
  document.querySelectorAll(".admin-only").forEach(el => { el.hidden = !admin; });
  if (!admin) {
    // Operator: the server returns only their own account and refuses
    // every admin route - the page just doesn't ask for them.
    document.title = "PGuard — My account";
    $("subtitle").textContent = "Change your password";
    $("pageTitle").textContent = "PGuard — My account";
    return;
  }

  $("userCount").textContent = state.users.length;
  $("userRows").innerHTML = state.users.map(u => `
    <tr><td><b>${esc(u.username)}</b>${u.username === state.me ? ' <span class="tag">you</span>' : ""}</td>
    <td><span class="role-tag ${u.role === "admin" ? "admin" : ""}">${esc(u.role)}</span></td>
    <td class="mono">${esc(when(u.created_at))}</td><td>${esc(u.created_by || "—")}</td>
    <td>${u.username !== state.me
      ? `<div class="row-btns"><button class="ghost-btn" data-role-user="${esc(u.username)}" data-role-to="${u.role === "admin" ? "operator" : "admin"}">${u.role === "admin" ? "Make operator" : "Make admin"}</button>
         <button class="ghost-btn danger-btn" data-del-user="${esc(u.username)}">Delete</button></div>` : ""}</td></tr>`).join("")
    || `<tr><td colspan="5" class="dim">No account yet.</td></tr>`;

  $("dashIpCount").textContent = state.dashboard_ips.length;
  $("dashIpRows").innerHTML = state.dashboard_ips.map(t => `
    <tr><td class="mono"><b>${esc(t.entry)}</b></td><td>${esc(t.label || "—")}</td>
    <td class="mono">${esc(when(t.added_at))}</td><td>${esc(t.added_by || "—")}</td>
    <td><button class="ghost-btn danger-btn" data-del-dash="${esc(t.entry)}">Remove</button></td></tr>`).join("")
    || `<tr><td colspan="5" class="dim">Empty list.</td></tr>`;
  $("dashIpMode").innerHTML = (state.dashboard_ips.length
    ? "Mode: <b class='teal'>restricted</b> — only the computers below (and this PC) can open the dashboard."
    : "Mode: <b class='warn-text'>open</b> — any computer that can reach the server sees the login page.")
    + ` Your computer appears as <b class="mono">${esc(state.your_ip)}</b>.`;

  $("ipCount").textContent = state.trusted_ips.length;
  $("ipRows").innerHTML = state.trusted_ips.map(t => `
    <tr><td class="mono"><b>${esc(t.ip)}</b></td><td>${esc(t.label || "—")}</td>
    <td class="mono">${esc(when(t.added_at))}</td><td>${esc(t.added_by || "—")}</td>
    <td><button class="ghost-btn danger-btn" data-del-ip="${esc(t.ip)}">Remove</button></td></tr>`).join("")
    || `<tr><td colspan="5" class="dim">Empty list.</td></tr>`;
  $("ipMode").innerHTML = state.trusted_ips.length
    ? "Mode: <b class='teal'>this list</b> decides who is trusted."
    : state.trusted_ips_env.length
      ? `Mode: list empty, so the <b>SECURITY_TRUSTED_IPS</b> setting is used (${esc(state.trusted_ips_env.join(", "))}).`
      : "Mode: <b class='warn-text'>automatic</b> — list empty, so whoever is connected when the dashboard starts is trusted. Add the dashboard's IP here to make it explicit.";
  loadPolicy();
  loadAudit(); // every change above is itself an audit entry
}

$("pwForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("pwNew").value !== $("pwNew2").value) return flash("The two new passwords differ.", false);
  const r = await api("POST", "/api/settings/password", { current: $("pwCurrent").value, new: $("pwNew").value });
  if (r && r.ok) { e.target.reset(); flash("Password changed. Your other sessions were signed out.", true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

$("userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("POST", "/api/settings/users", { username: $("newUser").value, password: $("newUserPw").value, role: $("newUserRole").value });
  if (r && r.ok) {
    const first = !state.auth_enabled;
    e.target.reset();
    flash(first ? "Account created - the login is now ON. Sign in with it." : "Account created.", true);
    if (first) setTimeout(() => location.assign("/login"), 1500); else load();
  } else flash(r ? r.error : "Request failed", false);
});

$("ipForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("POST", "/api/settings/trusted-ips", { ip: $("newIp").value, label: $("newIpLabel").value });
  if (r && r.ok) { e.target.reset(); flash("Trusted IP saved.", true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

$("dashIpForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("POST", "/api/settings/dashboard-ips", { entry: $("newDashIp").value, label: $("newDashIpLabel").value });
  if (r && r.ok) { e.target.reset(); flash("Computer allowed.", true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

document.addEventListener("click", async (e) => {
  const d = e.target.closest("[data-del-dash]");
  if (d && confirm(`Remove ${d.dataset.delDash} from the dashboard access list?`)) {
    const r = await api("DELETE", `/api/settings/dashboard-ips/${d.dataset.delDash.split("/").map(encodeURIComponent).join("/")}`);
    flash(r && r.ok ? "Removed." : (r ? r.error : "Request failed"), !!(r && r.ok)); load();
  }
  const u = e.target.closest("[data-del-user]");
  const ru = e.target.closest("[data-role-user]");
  if (ru && confirm(`Make "${ru.dataset.roleUser}" ${ru.dataset.roleTo === "admin" ? "an admin" : "an operator"}?`)) {
    const r = await api("POST", `/api/settings/users/${encodeURIComponent(ru.dataset.roleUser)}/role`, { role: ru.dataset.roleTo });
    flash(r && r.ok ? "Role changed - it applies immediately." : (r ? r.error : "Request failed"), !!(r && r.ok)); load();
  }
  const ip = e.target.closest("[data-del-ip]");
  if (u && confirm(`Delete account "${u.dataset.delUser}"? Its open sessions end immediately.`)) {
    const r = await api("DELETE", `/api/settings/users/${encodeURIComponent(u.dataset.delUser)}`);
    flash(r && r.ok ? "Account deleted." : (r ? r.error : "Request failed"), !!(r && r.ok)); load();
  }
  if (ip && confirm(`Remove ${ip.dataset.delIp} from the trusted list?`)) {
    const r = await api("DELETE", `/api/settings/trusted-ips/${encodeURIComponent(ip.dataset.delIp)}`);
    flash(r && r.ok ? "Removed." : (r ? r.error : "Request failed"), !!(r && r.ok)); load();
  }
});

load();

// ---------- robot connection + security policy ----------
const SOURCE_LABEL = { settings: "set in Settings", env: "from environment", default: "default" };
let policyKeys = [];

async function loadPolicy() {
  const r = await api("GET", "/api/settings/policy");
  if (!r || !r.ok) return;
  const rb = r.robot;
  $("robotSource").textContent = SOURCE_LABEL[rb.source] || rb.source;
  $("robotNow").innerHTML = `Dashboards connect to <b class="mono teal">${esc(rb.host)}:${esc(rb.port)}</b>`
    + ` · speed test <span class="mono">${esc(rb.speedtest_host)}:${esc(rb.speedtest_port)}</span>`
    + (rb.updated_by ? ` · changed by <b>${esc(rb.updated_by)}</b> ${esc(when(rb.updated_at))}` : "");
  if (document.activeElement?.form !== $("robotForm")) {
    $("robotHost").value = rb.host; $("robotPort").value = rb.port; $("robotStPort").value = rb.speedtest_port;
  }
  policyKeys = r.policy.map(p => p.key);
  $("policyRows").innerHTML = r.policy.map(p => `
    <label class="policy-row"><span class="pl">${esc(p.label)}
        <small>${esc(p.help)} Range ${esc(p.min)}–${esc(p.max)}, default ${esc(p.default)}.${p.updated_by ? ` Changed by ${esc(p.updated_by)}.` : ""}</small></span>
      <span class="pv"><input type="number" id="pol_${esc(p.key)}" value="${esc(p.value)}" min="${esc(p.min)}" max="${esc(p.max)}" step="${p.integer ? 1 : "any"}"
        class="${p.value !== p.default ? "changed" : ""}" required><em>${esc(p.unit)}</em></span></label>`).join("");
}

function robotBody() {
  return { host: $("robotHost").value.trim(), port: Number($("robotPort").value), speedtest_port: Number($("robotStPort").value) };
}

$("robotTest").addEventListener("click", async () => {
  const out = $("robotTestResult");
  out.textContent = "Testing…"; out.className = "hint";
  const r = await api("POST", "/api/settings/robot/test", robotBody());
  if (!r || !r.ok) { out.textContent = r ? r.error : "Request failed"; out.className = "hint err-text"; return; }
  out.textContent = r.reachable ? `✓ rosbridge port answers (${r.ms} ms)` : `✗ No answer: ${r.error}`;
  out.className = "hint " + (r.reachable ? "ok-text" : "err-text");
});

$("robotForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("POST", "/api/settings/robot", robotBody());
  if (r && r.ok) { flash(`Robot address saved. ${r.reconnected} open dashboard connection(s) are reconnecting.`, true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

$("robotReset").addEventListener("click", async () => {
  const r = await api("DELETE", "/api/settings/robot");
  if (r && r.ok) { flash(`Back to the default address (${r.robot.host}:${r.robot.port}).`, true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

$("policyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  for (const k of policyKeys) body[k] = Number($("pol_" + k).value);
  const r = await api("POST", "/api/settings/policy", body);
  if (r && r.ok) { flash(r.changed ? `Policy saved (${r.changed} value(s) changed).` : "Nothing changed.", true); load(); }
  else flash(r ? r.error : "Request failed", false);
});

// ---------- audit log ----------
const EVENT_LABEL = {
  login_ok: "Signed in", login_failed: "Failed login", lockout: "IP locked out",
  login_refused_locked: "Login while locked", logout: "Signed out", access_blocked: "Blocked computer",
  password_changed: "Password changed", password_change_refused: "Password change refused",
  user_created: "Account created", user_deleted: "Account deleted",
  trusted_ip_added: "Trusted IP added", trusted_ip_removed: "Trusted IP removed",
  dashboard_ip_added: "Dashboard access added", dashboard_ip_removed: "Dashboard access removed",
  policy_changed: "Policy changed", robot_address_changed: "Robot address changed",
  alert_ack: "Alert acknowledged", alert_unack: "Acknowledgement undone", server_started: "Server started",
};

async function loadAudit() {
  const r = await api("GET", "/api/audit?limit=300" + ($("auditWarnOnly").checked ? "&level=warning" : ""));
  if (!r || !r.ok) return;
  $("auditCount").textContent = r.entries.length;
  $("auditRows").innerHTML = r.entries.map(a => `
    <tr class="${a.level === "warning" ? "audit-warn" : ""}"><td class="mono">${esc(when(a.at))}</td>
    <td><b>${esc(EVENT_LABEL[a.event] || a.event)}</b></td><td>${esc(a.actor || "—")}</td>
    <td class="mono">${esc(a.ip || "—")}</td><td>${esc(a.detail || "")}</td></tr>`).join("")
    || `<tr><td colspan="5" class="dim">Nothing recorded yet.</td></tr>`;
  const i = r.integrity, el = $("auditIntegrity");
  el.className = "integrity " + (i.ok ? "ok" : "bad");
  el.textContent = i.ok ? `✓ Integrity verified — ${i.count} entries, hash chain intact`
                        : `✗ TAMPERING DETECTED — ${i.reason}. Entries before it: ${i.count} verified.`;
}
$("auditRefresh").addEventListener("click", loadAudit);
$("auditWarnOnly").addEventListener("change", loadAudit);
