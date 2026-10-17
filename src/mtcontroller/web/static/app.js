"use strict";
// Admin UI – vanilla JS, žádné externí knihovny (CSP: jen 'self').
// Veškerý text z API se vkládá přes textContent, nikdy přes innerHTML.

const $ = (id) => document.getElementById(id);
let csrf = "";
let me = null;
let tenantId = null;
let sites = [];

function msg(text, error = false) {
  const d = document.createElement("div");
  d.textContent = text;
  if (error) d.className = "error";
  $("msg").appendChild(d);
  setTimeout(() => d.remove(), error ? 8000 : 4000);
}

async function api(method, path, body) {
  const opt = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opt.headers["X-CSRF-Token"] = csrf;
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch("/api/admin" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401 && path !== "/login" && !path.startsWith("/mfa")) show("login");
    throw new Error(data.detail || ("HTTP " + r.status));
  }
  return data;
}

function show(which) {
  for (const s of ["login", "mfa", "app"]) $(s).hidden = s !== which;
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function fmtTime(t) {
  if (!t) return "–";
  const d = new Date(t);
  const ago = Math.round((Date.now() - d.getTime()) / 1000);
  if (ago < 120) return ago + " s";
  if (ago < 7200) return Math.round(ago / 60) + " min";
  return d.toLocaleString("cs-CZ");
}

// --- přihlášení -------------------------------------------------------------

async function boot() {
  try {
    me = await api("GET", "/me");
    csrf = me.csrf;
    if (!me.mfa_ok) return startMfa(false);
    return startApp();
  } catch (e) {
    show("login");
  }
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const r = await api("POST", "/login", { email: f.get("email"), password: f.get("password") });
    csrf = r.csrf;
    ev.target.reset();
    startMfa(r.mfa === "setup");
  } catch (e) { msg(e.message, true); }
});

async function startMfa(setup) {
  show("mfa");
  $("mfa-setup").hidden = true;
  if (setup || (me && !me.mfa_ok && !me.mfa_enrolled)) {
    try {
      const s = await api("POST", "/mfa/setup");
      $("mfa-secret").textContent = s.secret.replace(/(.{4})/g, "$1 ").trim();
      $("mfa-uri").textContent = s.uri;
      $("mfa-setup").hidden = false;
    } catch (e) { /* MFA už nastavené – stačí kód */ }
  }
}

$("mfa-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    await api("POST", "/mfa/verify", { code: f.get("code") });
    ev.target.reset();
    me = await api("GET", "/me");
    csrf = me.csrf;
    startApp();
  } catch (e) { msg(e.message, true); }
});

// --- aplikace ----------------------------------------------------------------

async function startApp() {
  show("app");
  const who = $("who");
  who.textContent = "";
  who.appendChild(el("span", me.email + (me.superadmin ? " (superadmin) " : " ")));
  const out = el("button", "Odhlásit", "small secondary");
  out.onclick = async () => { await api("POST", "/logout").catch(() => {}); location.reload(); };
  who.appendChild(out);
  $("tenant-form").hidden = !me.superadmin;
  await loadTenants();
}

async function loadTenants() {
  const list = await api("GET", "/tenants");
  const sel = $("tenant");
  sel.textContent = "";
  for (const t of list) {
    const o = el("option", `${t.name} (${t.devices} zař., ${t.tunnel_block})`);
    o.value = t.id;
    sel.appendChild(o);
  }
  if (!list.length) { tenantId = null; return; }
  if (!tenantId || !list.some((t) => t.id === tenantId)) tenantId = list[0].id;
  sel.value = tenantId;
  await loadTenant();
}

$("tenant").addEventListener("change", (ev) => { tenantId = ev.target.value; loadTenant(); });

$("tenant-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    const t = await api("POST", "/tenants", { name: new FormData(ev.target).get("name") });
    tenantId = t.id;
    ev.target.reset();
    msg("Tenant založen, blok " + t.tunnel_block);
    loadTenants();
  } catch (e) { msg(e.message, true); }
});

async function loadTenant() {
  await Promise.all([loadSites(), loadDevices(), loadAudit()]);
}

async function loadSites() {
  sites = await api("GET", `/tenants/${tenantId}/sites`);
  const ul = $("sites"), ts = $("token-site");
  ul.textContent = ""; ts.textContent = "";
  for (const s of sites) {
    ul.appendChild(el("li", `${s.name} – ${s.devices} zař.`));
    const o = el("option", s.name); o.value = s.id; ts.appendChild(o);
  }
  if (!sites.length) ul.appendChild(el("li", "Zatím žádná lokalita", "muted"));
}

$("site-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", `/tenants/${tenantId}/sites`, { name: new FormData(ev.target).get("name") });
    ev.target.reset();
    loadSites();
  } catch (e) { msg(e.message, true); }
});

$("token-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const site = new FormData(ev.target).get("site_id");
  if (!site) return msg("Nejdřív založ lokalitu", true);
  try {
    const t = await api("POST", `/tenants/${tenantId}/adoption-tokens`, { site_id: site });
    $("token-cmd").textContent = t.command;
    $("token-exp").textContent = new Date(t.expires_at).toLocaleString("cs-CZ");
    $("token-out").hidden = false;
  } catch (e) { msg(e.message, true); }
});

$("token-copy").addEventListener("click", () => {
  navigator.clipboard.writeText($("token-cmd").textContent).then(() => msg("Zkopírováno"));
});

async function loadDevices() {
  const list = await api("GET", `/tenants/${tenantId}/devices`);
  const tb = $("devices");
  tb.textContent = "";
  for (const d of list) {
    const tr = el("tr");
    tr.appendChild(el("td", d.state, "state " + d.state));
    tr.appendChild(el("td", d.serial));
    tr.appendChild(el("td", d.model));
    tr.appendChild(el("td", d.ros_version));
    tr.appendChild(el("td", d.site || "–"));
    tr.appendChild(el("td", d.tunnel_ip || "–"));
    tr.appendChild(el("td", (d.last_status && d.last_status.sstp) || "–"));
    tr.appendChild(el("td", fmtTime(d.last_checkin_at)));
    const act = el("td");
    if (d.state === "pending") act.appendChild(approveControls(d));
    else {
      for (const [kind, label, cls] of [["provision", "Znovu provisioning", "secondary"],
                                        ["reboot", "Restart", "secondary"],
                                        ["deprovision", "Odebrat", "danger"]]) {
        const b = el("button", label, "small " + cls);
        b.onclick = () => sendCommand(d, kind, label);
        act.appendChild(b);
        act.appendChild(document.createTextNode(" "));
      }
    }
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  if (!list.length) {
    const tr = el("tr"); const td = el("td", "Žádná zařízení", "muted"); td.colSpan = 9;
    tr.appendChild(td); tb.appendChild(tr);
  }
}

function approveControls(d) {
  const wrap = el("span");
  const site = el("select");
  for (const s of sites) { const o = el("option", s.name); o.value = s.id; site.appendChild(o); }
  if (d.site_id) site.value = d.site_id;
  const role = el("select");
  for (const r of ["unknown", "gateway", "switch", "ap", "ptp"]) { const o = el("option", r); o.value = r; role.appendChild(o); }
  const b = el("button", "Schválit", "small");
  b.onclick = async () => {
    if (!confirm(`Schválit zařízení ${d.serial} (${d.model})?`)) return;
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/approve`, { site_id: site.value, role: role.value });
      if (r.concentrator_errors.length) msg("Schváleno, ale koncentrátor: " + r.concentrator_errors.join("; "), true);
      else msg("Schváleno – provisioning proběhne při dalším check-inu");
      loadDevices();
    } catch (e) { msg(e.message, true); }
  };
  wrap.append(site, " ", role, " ", b);
  return wrap;
}

async function sendCommand(d, kind, label) {
  if (!confirm(`${label}: ${d.serial}?`)) return;
  try {
    await api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, { kind });
    msg(`${label} naplánováno – provede se při dalším check-inu`);
  } catch (e) { msg(e.message, true); }
}

async function loadAudit() {
  const list = await api("GET", `/tenants/${tenantId}/audit`);
  const tb = $("audit");
  tb.textContent = "";
  for (const a of list.slice(0, 50)) {
    const tr = el("tr");
    tr.append(el("td", new Date(a.at).toLocaleString("cs-CZ")), el("td", a.user || "–"),
              el("td", a.action), el("td", a.target || ""), el("td", a.ip || ""));
    tb.appendChild(tr);
  }
}

$("refresh").addEventListener("click", () => loadTenant());
setInterval(() => { if (!$("app").hidden && tenantId) { loadDevices(); } }, 15000);

boot();
