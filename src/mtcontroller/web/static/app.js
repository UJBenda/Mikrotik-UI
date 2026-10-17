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
  for (const s of ["login", "mfa", "app", "detail"]) $(s).hidden = s !== which;
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
  const pw = el("button", "Heslo", "small secondary");
  pw.onclick = () => { $("pw-card").hidden = !$("pw-card").hidden; };
  who.append(pw, " ");
  who.appendChild(out);
  $("tenant-form").hidden = !me.superadmin;
  if (me.superadmin) loadSystem();
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
  await Promise.all([loadSites(), loadDevices(), loadAudit(), loadPlans(), loadAlerts(), loadUsers()]);
  renderOverview();
  await loadSitePanel();
}

let alertsCache = [];

function renderOverview() {
  const box = $("overview"); box.textContent = "";
  const n = (st) => devicesCache.filter((d) => d.state === st).length;
  const tiles = [
    ["Online", n("adopted"), ""],
    ["Offline", n("offline"), n("offline") ? "bad" : ""],
    ["Čeká na schválení", n("pending"), n("pending") ? "warn" : ""],
    ["Provisioning", n("provisioning"), ""],
    ["Kritické alerty", alertsCache.filter((a) => a.severity === "critical").length, ""],
    ["Varování", alertsCache.filter((a) => a.severity === "warning").length, ""],
    ["Dostupná aktualizace", devicesCache.filter((d) => d.update_available).length, ""],
    ["Známé problémy verzí", devicesCache.filter((d) => (d.warnings || []).some((w) => w.severity === "critical")).length, ""],
    ["Bezpečnostní nálezy", devicesCache.reduce((n, d) => n + (Number(d.security_issues) || 0), 0), ""],
  ];
  tiles[8][2] = tiles[8][1] ? "warn" : "";
  tiles[4][2] = tiles[4][1] ? "bad" : "";
  tiles[5][2] = tiles[5][1] ? "warn" : "";
  tiles[7][2] = tiles[7][1] ? "bad" : "";
  for (const [label, value, cls] of tiles) {
    if (label === "Provisioning" && !value) continue;
    const t = el("div", null, "tile" + (cls ? " " + cls : ""));
    t.append(el("b", String(value)), el("span", label));
    box.appendChild(t);
  }
}

async function loadSites() {
  sites = await api("GET", `/tenants/${tenantId}/sites`);
  const ul = $("sites"), ts = $("token-site");
  ul.textContent = ""; ts.textContent = "";
  for (const s of sites) {
    ul.appendChild(el("li", `${s.name} – ${s.devices} zař.`));
    const o = el("option", s.name); o.value = s.id; ts.appendChild(o);
  }
  const busiestSite = sites.reduce((a, s) => (!a || (s.devices || 0) > (a.devices || 0) ? s : a), null);
  if (busiestSite) ts.value = busiestSite.id;
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
  devicesCache = list;
  for (const d of list) {
    const tr = el("tr");
    if (d.state !== "pending") {
      tr.className = "clickable";
      tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "BUTTON" && ev.target.tagName !== "SELECT") openDetail(d.id); });
    }
    tr.appendChild(el("td", d.state, "state " + d.state));
    tr.appendChild(el("td", d.serial));
    tr.appendChild(el("td", d.model));
    const verTd = el("td", d.ros_version);
    if (d.update_available) {
      const u = el("span", ` ↑ ${d.latest_version}`, "warn");
      u.title = `Dostupná aktualizace v kanálu ${d.update_channel || "?"}`;
      verTd.appendChild(u);
    }
    if (d.warnings && d.warnings.length) {
      const w = el("span", d.warnings.some((x) => x.severity === "critical") ? " ⚠" : " ⓘ", "warn");
      w.title = d.warnings.map((x) => x.text).join("\n");
      verTd.appendChild(w);
    }
    tr.appendChild(verTd);
    tr.appendChild(el("td", d.site || "–"));
    tr.appendChild(el("td", d.tunnel_ip || "–"));
    tr.appendChild(el("td", (d.last_status && d.last_status.sstp) || "–"));
    tr.appendChild(el("td", fmtTime(d.last_checkin_at)));
    const act = el("td");
    if (d.state === "pending") act.appendChild(approveControls(d));
    else {
      if (d.update_available && d.state === "adopted") {
        const b = el("button", `Aktualizovat → ${d.latest_version}`, "small");
        b.onclick = () => upgradeDevice(d);
        act.append(b, " ");
      }
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

async function sendCommand(d, kind, label, extra) {
  if (!confirm(`${label}: ${d.serial}?`)) return;
  try {
    await api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, Object.assign({ kind }, extra || {}));
    msg(`${label} naplánováno – provede se při dalším check-inu`);
  } catch (e) { msg(e.message, true); }
}

function upgradeDevice(d) {
  const warn = (d.warnings || []).filter((x) => x.severity !== "info").map((x) => "• " + x.text);
  const text = `Aktualizovat RouterOS na ${d.serial} z ${d.ros_version} na ${d.latest_version}?\n` +
    "Zařízení si verzi stáhne a restartuje se (výpadek ~2–5 min). Předtím se doporučuje záloha." +
    (warn.length ? "\n\nZnámé problémy současné verze:\n" + warn.join("\n") : "");
  if (!confirm(text)) return;
  api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, { kind: "upgrade", version: d.latest_version })
    .then(() => msg(`Aktualizace ${d.serial} na ${d.latest_version} naplánována – provede se při dalším check-inu`))
    .catch((e) => msg(e.message, true));
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

// --- lokalita: NTP politika, plány, topologie ------------------------------------------

let devicesCache = [];

function siteId() { return $("site-sel").value; }

async function loadSitePanel() {
  const sel = $("site-sel");
  const prev = sel.value;
  sel.textContent = "";
  for (const s of sites) { const o = el("option", s.name); o.value = s.id; sel.appendChild(o); }
  // výchozí lokalita: ta s nejvíce zařízeními (ne první podle abecedy)
  const busiest = sites.reduce((a, s) => (!a || (s.devices || 0) > (a.devices || 0) ? s : a), null);
  sel.value = prev && sites.some((s) => s.id === prev) ? prev : (busiest ? busiest.id : "");
  // panely jsou vázané na lokalitu – při změně je zavřít, ať neukazují data jiné lokality
  for (const w of ["topo-wrap", "clients-wrap", "wifi-wrap", "stp-wrap", "upg-wrap"]) $(w).hidden = true;
  $("ntp-result").textContent = "";
  if (!sites.length) return;
  const prim = $("ntp-primary");
  prim.textContent = "";
  const siteDevs = devicesCache.filter((x) => x.site_id === siteId() && x.state !== "pending");
  for (const d of siteDevs) {
    const o = el("option", `${d.identity || d.serial} (${d.model || "?"})`); o.value = d.id; prim.appendChild(o);
  }
  prim.value = siteDevs[0] ? siteDevs[0].id : "";
  const pol = (await api("GET", `/tenants/${tenantId}/sites/${siteId()}/policy/ntp`)).policy;
  const f = $("ntp-form");
  if (pol) {
    prim.value = pol.primary_device_id;
    f.upstream.value = pol.upstream.join(", ");
    f.timezone.value = pol.timezone;
  } else { f.upstream.value = ""; f.timezone.value = ""; }
}

$("site-sel").addEventListener("change", () => loadSitePanel());

$("ntp-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const upstream = f.upstream.value.split(",").map((x) => x.trim()).filter(Boolean);
  try {
    await api("POST", `/tenants/${tenantId}/sites/${siteId()}/policy/ntp`, {
      primary_device_id: f.primary_device_id.value, upstream: upstream.length ? upstream : undefined,
      timezone: f.timezone.value.trim() || undefined });
    msg("NTP politika uložena");
  } catch (e) { msg(e.message, true); }
});

$("ntp-plan").addEventListener("click", async () => {
  try {
    const res = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/plans/ntp`);
    const out = $("ntp-result");
    out.textContent = "";
    const ul = el("ul");
    for (const r of res) {
      const what = r.result === "plan" ? `plán: ${r.summary}` : r.result === "ok" ? "beze změn" : `přeskočeno – ${r.reason}`;
      ul.appendChild(el("li", `${r.identity || r.serial}: ${what}`));
    }
    out.appendChild(ul);
    loadPlans();
  } catch (e) { msg(e.message, true); }
});

async function loadPlans() {
  const list = await api("GET", `/tenants/${tenantId}/plans?status=draft`);
  const box = $("plans");
  box.textContent = "";
  if (!list.length) { box.appendChild(el("p", "Žádné plány nečekají na schválení.", "muted")); return; }
  for (const p of list) {
    const div = el("div", null, "plan");
    div.appendChild(el("strong", `${p.identity || p.serial} (${p.site || "–"}) – ${p.module.toUpperCase()}`));
    div.appendChild(el("div", p.summary));
    div.appendChild(el("pre", p.script));
    const ok = el("button", "Schválit a aplikovat", "small");
    ok.onclick = async () => {
      if (!confirm("Aplikovat plán na zařízení? Provede se při dalším check-inu.")) return;
      try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/approve`); msg("Plán schválen"); loadPlans(); }
      catch (e) { msg(e.message, true); }
    };
    const no = el("button", "Zahodit", "small secondary");
    no.onclick = async () => { try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/discard`); loadPlans(); } catch (e) { msg(e.message, true); } };
    div.append(ok, " ", no);
    box.appendChild(div);
  }
}
$("plans-refresh").addEventListener("click", () => loadPlans());

const SVG = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs, text) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}

$("topo-btn").addEventListener("click", async () => {
  try {
    const t = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/topology`);
    drawTopology(t);
    $("topo-wrap").hidden = false;
  } catch (e) { msg(e.message, true); }
});

function drawTopology(t) {
  const svg = $("topo");
  svg.textContent = "";
  if (!t.nodes.length) { svg.appendChild(svgEl("text", { x: 20, y: 30 }, "Zatím žádná data o sousedech.")); return; }
  // úrovně: BFS od uzlu s nejvíc spoji
  const adj = {};
  for (const n of t.nodes) adj[n.id] = [];
  for (const e of t.edges) { adj[e.a].push(e.b); adj[e.b].push(e.a); }
  const root = t.nodes.filter((n) => n.managed).sort((a, b) => adj[b.id].length - adj[a.id].length)[0] || t.nodes[0];
  const level = { [root.id]: 0 };
  const queue = [root.id];
  while (queue.length) {
    const cur = queue.shift();
    for (const nb of adj[cur]) if (!(nb in level)) { level[nb] = level[cur] + 1; queue.push(nb); }
  }
  let maxL = Math.max(...Object.values(level));
  for (const n of t.nodes) if (!(n.id in level)) level[n.id] = ++maxL;  // nespojené uzly dole
  const rows = {};
  for (const n of t.nodes) (rows[level[n.id]] = rows[level[n.id]] || []).push(n);
  const nRows = Object.keys(rows).length;
  const H = Math.max(200, nRows * 110 + 40);
  svg.setAttribute("viewBox", `0 0 800 ${H}`);
  const pos = {};
  for (const [l, ns] of Object.entries(rows)) {
    ns.forEach((n, i) => { pos[n.id] = { x: (800 / (ns.length + 1)) * (i + 1), y: 50 + Number(l) * 110 }; });
  }
  for (const e of t.edges) {
    const a = pos[e.a], b = pos[e.b];
    svg.appendChild(svgEl("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y }));
    const pa = e.ports[e.a], pb = e.ports[e.b];
    if (pa) svg.appendChild(svgEl("text", { x: a.x + (b.x - a.x) * 0.2 + 4, y: a.y + (b.y - a.y) * 0.2 - 4, class: "port" }, pa));
    if (pb) svg.appendChild(svgEl("text", { x: b.x + (a.x - b.x) * 0.2 + 4, y: b.y + (a.y - b.y) * 0.2 - 4, class: "port" }, pb));
  }
  for (const n of t.nodes) {
    const p = pos[n.id];
    const cls = !n.managed ? "ext" : n.state === "offline" ? "offline" : "managed";
    const c = svgEl("circle", { cx: p.x, cy: p.y, r: 12, class: cls });
    c.appendChild(svgEl("title", {}, `${n.label}${n.model ? " – " + n.model : ""}${n.address ? " – " + n.address : ""}`));
    svg.appendChild(c);
    svg.appendChild(svgEl("text", { x: p.x, y: p.y + 28, "text-anchor": "middle" }, n.label));
    if (n.managed) c.addEventListener("click", () => openDetail(n.id));
  }
}

const SEV_LABEL = { critical: "kritické", warning: "varování", info: "info" };

async function loadAlerts() {
  const list = await api("GET", `/tenants/${tenantId}/alerts`);
  alertsCache = list;
  const tb = $("alerts");
  tb.textContent = "";
  const crit = list.filter((a) => a.severity === "critical").length;
  $("alerts-count").textContent = list.length ? `${list.length}${crit ? ` (${crit} kritických)` : ""}` : "žádné";
  for (const a of list) {
    const tr = el("tr");
    tr.append(el("td", SEV_LABEL[a.severity] || a.severity, "sev-" + (a.severity === "critical" ? "error" : a.severity)),
              el("td", a.identity || a.serial), el("td", a.site || "–"), el("td", a.message), el("td", fmtTime(a.opened_at)));
    const act = el("td");
    if (a.acknowledged_at) act.appendChild(el("span", `potvrdil ${a.acknowledged_by || ""}`, "muted"));
    else {
      const b = el("button", "Potvrdit", "small secondary");
      b.onclick = async () => { try { await api("POST", `/tenants/${tenantId}/alerts/${a.id}/ack`); loadAlerts(); } catch (e) { msg(e.message, true); } };
      act.appendChild(b);
    }
    tr.appendChild(act);
    tb.appendChild(tr);
  }
}
$("alerts-refresh").addEventListener("click", () => loadAlerts());

let clientsCache = [];

async function loadClients() {
  try {
    const hist = $("clients-history").checked ? "?history=1" : "";
    clientsCache = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/clients${hist}`);
    $("clients-wrap").hidden = false;
    renderClients();
  } catch (e) { msg(e.message, true); }
}
$("clients-btn").addEventListener("click", () => loadClients());
$("clients-history").addEventListener("change", () => loadClients());

function isNew(c) {
  return c.first_seen && Date.now() - new Date(c.first_seen).getTime() < 24 * 3600 * 1000;
}

let csearchTimer = null;
$("csearch").addEventListener("input", () => {
  clearTimeout(csearchTimer);
  csearchTimer = setTimeout(async () => {
    const q = $("csearch").value.trim();
    const ul = $("csearch-out"); ul.textContent = "";
    if (q.length < 2 || !tenantId) return;
    let list;
    try { list = await api("GET", `/tenants/${tenantId}/clients?q=${encodeURIComponent(q)}`); } catch (e) { return msg(e.message, true); }
    for (const c of list) {
      ul.appendChild(el("li", `${c.hostname || "?"} ${c.ip || ""} ${c.mac} – ${c.site}: ${c.device || "?"} ${c.port || ""}` +
                              (c.vlan ? ` VLAN ${c.vlan}` : "") + (c.ssid ? ` (${c.ssid})` : "") +
                              (c.online ? " · připojen" : ` · naposledy ${fmtTime(c.last_seen)}`), c.online ? "" : "muted"));
    }
    if (!list.length) ul.appendChild(el("li", "Nic nenalezeno (historie se plní každých 5 min)", "muted"));
  }, 300);
});
$("clients-filter").addEventListener("input", () => renderClients());

function renderClients() {
  const q = $("clients-filter").value.trim().toLowerCase();
  const tb = $("clients");
  tb.textContent = "";
  const list = clientsCache.filter((c) => !q || [c.hostname, c.ip, c.mac, c.device, c.port, c.vlan, c.wifi && c.wifi.ssid, c.ssid]
    .some((v) => v && String(v).toLowerCase().includes(q)));
  for (const c of list.slice(0, 1000)) {
    const tr = el("tr");
    if (c.online === false) tr.className = "muted";
    const ssid = c.wifi ? c.wifi.ssid : c.ssid, signal = c.wifi ? c.wifi.signal : c.signal;
    const wifi = ssid || signal ? `${ssid || ""} ${signal ? signal + " dBm" : ""}` : "";
    const name = el("td", c.hostname || "");
    if (isNew(c)) name.appendChild(el("span", " nový", "badge"));
    tr.append(name, el("td", c.ip || ""), el("td", c.mac), el("td", c.device || "?"),
              el("td", (c.port || "") + (c.uncertain ? " ?" : "")), el("td", c.vlan || ""), el("td", wifi),
              el("td", c.first_seen ? fmtTime(c.first_seen) : "–"), el("td", c.online ? "teď" : fmtTime(c.last_seen)));
    tb.appendChild(tr);
  }
  if (!list.length) { const tr = el("tr"); const td = el("td", "Žádní klienti (inventura klientů běží každých 5 min)", "muted"); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
}

// --- Wi-Fi lokality --------------------------------------------------------------------

$("wifi-btn").addEventListener("click", async () => {
  try { renderWifi(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi`)); } catch (e) { msg(e.message, true); }
});

function renderWifi(w) {
  $("wifi-wrap").hidden = false;
  const sm = w.summary;
  $("wifi-summary").textContent = `${sm.aps} zařízení, ${sm.radios} rádií, ${sm.clients} klientů` +
    (sm.ssids.length ? ` · SSID: ${sm.ssids.join(", ")}` : "");
  const ul = $("wifi-warn"); ul.textContent = "";
  for (const x of w.warnings) ul.appendChild(el("li", x.text, "sev-" + (x.severity === "info" ? "info" : "warning")));
  const tb = $("wifi-radios"); tb.textContent = "";
  for (const r of w.radios) {
    const tr = el("tr");
    if (r.disabled) tr.className = "muted";
    const sig = r.signal_avg != null ? `${r.signal_avg} / ${r.signal_min} dBm` : "";
    tr.append(el("td", r.device || "?"),
              el("td", r.interface + (r.master ? ` (virt. na ${r.master})` : "") + (r.capsman ? " · CAPsMAN" : "") +
                       (r.disabled ? " · vypnuté" : !r.running ? " · neběží" : "")),
              el("td", r.ssid || ""), el("td", r.mode || ""), el("td", r.band || ""), el("td", r.frequency || ""),
              el("td", r.width || ""), el("td", String(r.clients)),
              el("td", sig + (r.weak_clients ? ` (${r.weak_clients} slabých)` : ""),
                 r.signal_min != null && r.signal_min < -75 ? "warn" : ""));
    tb.appendChild(tr);
  }
  if (!w.radios.length) { const tr = el("tr"); const td = el("td", "Žádná Wi-Fi rádia (inventura běží každých 5 min)", "muted"); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
  const ct = $("wifi-caps"); ct.textContent = "";
  $("wifi-caps-h").hidden = $("wifi-caps-t").hidden = !w.caps.length;
  for (const c of w.caps) {
    const tr = el("tr");
    tr.append(el("td", c.controller || ""), el("td", c.identity || c.name || ""), el("td", c.address || ""),
              el("td", c.board || ""), el("td", c.version || ""), el("td", c.state || ""));
    ct.appendChild(tr);
  }
}

// --- stav systému (superadmin) -------------------------------------------------------------

async function loadSystem() {
  let s;
  try { s = await api("GET", "/system"); } catch (e) { return; }
  $("system-card").hidden = false;
  const w = s.worker;
  const wtxt = !w ? "worker zatím nezapsal stav – běží? (systemctl status mtc-worker)"
    : w.stale ? `worker NEBĚŽÍ – poslední kolo před ${Math.round(w.age_seconds / 60)} min (journalctl -u mtc-worker)`
    : `worker běží (kolo ${w.tick}, před ${w.age_seconds} s)`;
  const dev = Object.entries(s.devices || {}).map(([k, v]) => `${k} ${v}`).join(", ") || "žádná";
  const p = $("system-summary"); p.textContent = "";
  p.append(el("span", wtxt, !w || w.stale ? "sev-error" : ""),
           ` · zařízení: ${dev} · fronta příkazů: ${(s.commands.queued || 0)} čeká, ${(s.commands.sent || 0)} odesláno` +
           ` · otevřené alerty: ${Object.values(s.alerts || {}).reduce((a, b) => a + b, 0)}` +
           ` · DB ${(s.db_bytes / 1048576).toFixed(1)} MB · verze ${s.version}`);
  const tb = $("system-steps"); tb.textContent = "";
  for (const [name, st] of Object.entries((w && w.steps) || {})) {
    const tr = el("tr");
    tr.append(el("td", name), el("td", `${st.ms} ms`),
              el("td", st.error ? st.error : (st.result === null || st.result === undefined ? "ok" : String(st.result)),
                 st.error ? "sev-error" : ""));
    tb.appendChild(tr);
  }
}

$("system-refresh").addEventListener("click", () => loadSystem());

// --- uživatelé tenanta a vlastní heslo -----------------------------------------------------

const ROLE_LABEL = { tenant_admin: "správce", technician: "technik", readonly: "jen čtení" };

function canManage() {
  return me && (me.superadmin || (me.tenants || {})[tenantId] === "tenant_admin");
}

async function loadUsers() {
  $("users-card").hidden = !canManage();
  if (!canManage()) return;
  const rs = $("user-role");
  if (!rs.children.length) for (const [r, l] of Object.entries(ROLE_LABEL)) { const o = el("option", l); o.value = r; rs.appendChild(o); }
  rs.value = "technician";
  let list;
  try { list = await api("GET", `/tenants/${tenantId}/users`); } catch (e) { return msg(e.message, true); }
  const tb = $("users"); tb.textContent = "";
  for (const u of list) {
    const tr = el("tr");
    const sel = el("select");
    for (const [r, l] of Object.entries(ROLE_LABEL)) { const o = el("option", l); o.value = r; sel.appendChild(o); }
    sel.value = u.role;
    const self = u.email === me.email;
    sel.disabled = self;
    sel.addEventListener("change", async () => {
      try { await api("PATCH", `/tenants/${tenantId}/users/${u.id}`, { role: sel.value }); msg("Role změněna"); }
      catch (e) { msg(e.message, true); loadUsers(); }
    });
    const act = el("td");
    if (!self) {
      const b = el("button", "Odebrat", "small danger");
      b.onclick = async () => {
        if (!confirm(`Odebrat ${u.email} z tenanta?`)) return;
        try { await api("DELETE", `/tenants/${tenantId}/users/${u.id}`); loadUsers(); } catch (e) { msg(e.message, true); }
      };
      act.appendChild(b);
    }
    if (me.superadmin && u.mfa) {
      const b = el("button", "Reset MFA", "small secondary");
      b.onclick = async () => {
        if (!confirm(`Resetovat MFA pro ${u.email}? Při dalším přihlášení si ho nastaví znovu.`)) return;
        try { await api("POST", `/users/${u.id}/reset-mfa`); loadUsers(); } catch (e) { msg(e.message, true); }
      };
      act.append(" ", b);
    }
    const td = el("td"); td.appendChild(sel);
    tr.append(el("td", u.email + (u.locked ? " (zamčeno)" : "") + (u.is_active ? "" : " (neaktivní)")), td,
              el("td", u.mfa ? "ano" : "zatím ne"), el("td", fmtTime(u.last_login)), act);
    tb.appendChild(tr);
  }
}

$("user-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const r = await api("POST", `/tenants/${tenantId}/users`, { email: f.get("email"), role: f.get("role") });
    const t = $("user-temp");
    t.hidden = !r.temp_password;
    t.textContent = r.temp_password
      ? `Nový účet ${r.email}: dočasné heslo ${r.temp_password} – předej ho bezpečně, zobrazí se jen teď. MFA si nastaví při prvním přihlášení.`
      : "";
    if (!r.temp_password) msg(`${r.email} přidán do tenanta`);
    ev.target.reset();
    loadUsers();
  } catch (e) { msg(e.message, true); }
});

$("pw-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    await api("POST", "/me/password", { old: f.get("old"), new: f.get("new") });
    msg("Heslo změněno, ostatní relace odhlášeny");
    ev.target.reset();
    $("pw-card").hidden = true;
  } catch (e) { msg(e.message, true); }
});

// --- panel portů (jako přední panel switche) ----------------------------------------------

const PORT_RE = /^(ether|sfp|combo|qsfp)/;

function portNum(name) {
  const m = String(name).match(/(\d+)(?!.*\d)/);
  return m ? Number(m[1]) : 0;
}

function shortName(s, n = 9) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function renderPorts(devId, d) {
  const box = $("d-ports"); box.textContent = "";
  const phys = (d.interfaces || []).filter((i) => i.type === "ether" && PORT_RE.test(i.name));
  if (!phys.length) { box.appendChild(el("p", "Žádné fyzické porty v inventuře", "muted")); return; }
  const managed = new Set((devicesCache || []).map((x) => x.identity).filter(Boolean));
  const neigh = {};
  for (const n of d.neighbors || []) {
    for (const part of String(n.interface || "").split(/[,/]/)) {
      if (part && !neigh[part]) neigh[part] = n;
    }
  }
  const sfp = phys.filter((i) => /^(sfp|qsfp)/.test(i.name)).sort((a, b) => portNum(a.name) - portNum(b.name));
  const cu = phys.filter((i) => !/^(sfp|qsfp)/.test(i.name)).sort((a, b) => portNum(a.name) - portNum(b.name));
  for (const group of [cu, sfp]) if (group.length) box.appendChild(portGroup(devId, group, neigh, managed));
}

function portGroup(devId, ports, neigh, managed) {
  // víc než 8 portů: liché nahoře, sudé dole (jako na switchi); jinak jedna řada
  const twoRows = ports.length > 8;
  const top = twoRows ? ports.filter((_, i) => i % 2 === 0) : ports;
  const bottom = twoRows ? ports.filter((_, i) => i % 2 === 1) : [];
  const g = el("div", null, "port-group");
  g.style.gridTemplateColumns = `repeat(${top.length}, 52px)`;
  const place = (node, row, col) => { node.style.gridRow = String(row); node.style.gridColumn = String(col); g.appendChild(node); };
  const label = (i, pos) => {
    const n = neigh[i.name], p = i.port || {};
    const c = el("div", null, "port-label " + pos);
    const who = n ? (n.identity || n["mac-address"] || "") : "";
    const w = p.poe_power && Number(p.poe_power) > 0 ? `${Number(p.poe_power).toFixed(1)} W` : "";
    const name = el("span", shortName(who), managed.has(who) ? "managed" : "");
    if (pos === "above") c.append(el("span", w, "poe-w"), name); else c.append(name, el("span", w, "poe-w"));
    if (n) c.title = `${n.identity || ""} ${n["mac-address"] || ""} ${n.board || n.platform || ""}`.trim();
    return c;
  };
  top.forEach((i, k) => { place(label(i, "above"), 1, k + 1); place(portTile(devId, i, neigh, managed), 2, k + 1); });
  bottom.forEach((i, k) => { place(portTile(devId, i, neigh, managed), 3, k + 1); place(label(i, "below"), 4, k + 1); });
  return g;
}

function portTile(devId, i, neigh, managed) {
  const p = i.port || {};
  const n = neigh[i.name];
  let cls = "port";
  if (i.disabled) cls += " off";
  else if (!i.running || (p.link && p.link !== "link-ok")) cls += " down";
  else if (/G/.test(p.rate || "")) cls += " gbe";
  else if (p.rate) cls += " fe";
  else cls += " gbe";
  if (p.full_duplex === false || p.stp_forwarding === false) cls += " blk";
  // uplink: root port STP, bez STP port ke spravované gateway
  const gw = n && (devicesCache || []).some((x) => x.identity === n.identity && x.role === "gateway");
  const uplink = p.stp_role === "root-port" || (!p.stp_role && gw);
  const t = el("button", null, cls);
  t.type = "button";
  const poeOn = p.poe && /powered-on|on/.test(p.poe);
  if (/^(sfp|qsfp)/.test(i.name)) { t.className += " sfp"; t.appendChild(el("span", /plus/.test(i.name) ? "SFP+" : "SFP", "tag")); }
  t.append(el("span", String(portNum(i.name)), "num"), el("span", (poeOn ? "⚡" : "") + (uplink ? "↑" : ""), "ico"));
  t.title = [
    i.name + (i.comment ? ` – ${i.comment}` : ""),
    i.disabled ? "vypnutý" : (i.running ? `linka ${p.rate || "ok"}${p.full_duplex === false ? " half-duplex" : ""}` : "bez linky"),
    p.pvid ? `PVID ${p.pvid}` + (p.tagged && p.tagged.length ? `, tagované ${p.tagged.join(",")}` : "") : "",
    p.stp_role ? `STP ${p.stp_role}${p.stp_forwarding === false ? " (blokuje)" : ""}` : "",
    p.poe ? `PoE ${p.poe}${p.poe_power ? " " + p.poe_power + " W" : ""}` : "",
    p.sfp_rx_power ? `SFP RX ${p.sfp_rx_power} dBm` : "",
    n ? `soused: ${n.identity || n["mac-address"]}` : "",
    `chyby RX/TX ${i.rx_error || 0}/${i.tx_error || 0}, výpadky ${i.link_downs || 0}`,
  ].filter(Boolean).join("\n");
  t.addEventListener("click", () => showTraffic(devId, i.name));
  return t;
}

// --- graf provozu rozhraní -------------------------------------------------------------------

let trafficIface = null;

function fmtBps(v) {
  if (v == null) return "–";
  const u = [["G", 1e9], ["M", 1e6], ["k", 1e3]];
  for (const [n, f] of u) if (v >= f) return (v / f).toFixed(v / f >= 100 ? 0 : 1) + ` ${n}b/s`;
  return `${Math.round(v)} b/s`;
}

async function showTraffic(devId, name) {
  trafficIface = name;
  const hours = $("d-traffic-range").value || "24";
  let r;
  try {
    r = await api("GET", `/tenants/${tenantId}/devices/${devId}/traffic?interface=${encodeURIComponent(name)}&hours=${hours}`);
  } catch (e) { return msg(e.message, true); }
  if (detailId !== devId || trafficIface !== name) return;
  $("d-traffic").hidden = false;
  $("d-traffic-h").textContent = `Provoz ${name} `;
  drawTraffic(r);
}

$("d-traffic-range").addEventListener("change", () => { if (detailId && trafficIface) showTraffic(detailId, trafficIface); });

function drawTraffic(r) {
  const svg = $("d-traffic-svg"); svg.textContent = "";
  const pts = r.points || [];
  const W = 800, H = 220, L = 60, R = 10, T = 10, B = 25;
  if (pts.length < 2) {
    svg.style.display = "none";
    $("d-traffic-info").textContent = "Zatím málo dat – vzorek se ukládá každých 5 minut, první body do ~10 min po adopci.";
    return;
  }
  svg.style.display = "";
  const t0 = new Date(pts[0].t).getTime(), t1 = new Date(pts[pts.length - 1].t).getTime();
  const peak = (p) => Math.max(p.rx || 0, p.tx || 0, p.rx_max || 0, p.tx_max || 0);
  const max = Math.max(1, ...pts.map(peak));
  const x = (t) => L + (W - L - R) * (t1 > t0 ? (new Date(t).getTime() - t0) / (t1 - t0) : 0);
  const y = (v) => T + (H - T - B) * (1 - (v || 0) / max);
  for (const f of [0, 0.5, 1]) {
    svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y(max * f), y2: y(max * f) }));
    svg.appendChild(svgEl("text", { x: 2, y: y(max * f) + 4 }, fmtBps(max * f)));
  }
  const line = (key, cls) => {
    const p = pts.filter((q) => q[key] != null).map((q) => `${x(q.t).toFixed(1)},${y(q[key]).toFixed(1)}`).join(" ");
    if (p) svg.appendChild(svgEl("polyline", { points: p, class: cls }));
  };
  if (r.resolution === "1h") { line("rx_max", "rx max"); line("tx_max", "tx max"); }
  line("rx", "rx"); line("tx", "tx");
  const fmtT = (t) => new Date(t).toLocaleString("cs-CZ", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });
  svg.appendChild(svgEl("text", { x: L, y: H - 6 }, fmtT(pts[0].t)));
  svg.appendChild(svgEl("text", { x: W - R - 90, y: H - 6 }, fmtT(pts[pts.length - 1].t)));
  const last = pts[pts.length - 1];
  $("d-traffic-info").textContent = `RX (modře) ${fmtBps(last.rx)}, TX (oranžově) ${fmtBps(last.tx)} · špička ${fmtBps(max)}` +
    (r.resolution === "1h" ? " · hodinové průměry, čárkovaně špičky" : " · 5min vzorky");
}

$("d-edit").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!detailId) return;
  try {
    await api("PATCH", `/tenants/${tenantId}/devices/${detailId}`,
              { site_id: $("d-edit-site").value, role: $("d-edit-role").value, note: $("d-edit-note").value });
    msg("Uloženo");
    loadDevices();
    openDetail(detailId);
  } catch (e) { msg(e.message, true); }
});

// --- release notes ------------------------------------------------------------------------

async function loadReleaseNotes(id) {
  let n;
  try { n = await api("GET", `/tenants/${tenantId}/devices/${id}/release-notes`); } catch (e) { return; }
  if (detailId !== id || !n.target) return;
  $("d-notes").hidden = false;
  $("d-notes-h").textContent = `Co přinese ${n.target}` +
    (n.versions.length ? ` (changelogy ${n.versions.map((v) => v.version).join(", ")})` : "");
  const ul = $("d-notes-list"); ul.textContent = "";
  for (const e of n.relevant.slice(0, 200)) {
    const li = el("li", `${e.component ? e.component + " – " : ""}${e.text}`, e.important ? "sev-warning" : "");
    li.title = e.version;
    ul.appendChild(li);
  }
  $("d-notes-more").textContent = !n.versions.length
    ? "Changelog zatím není stažený (controller ho stahuje jednou denně z download.mikrotik.com)."
    : `Zobrazeny změny týkající se funkcí tohoto zařízení a důležité změny (!); dalších změn: ${n.other_count}.` +
      (n.complete ? "" : " Changelog cílové verze zatím chybí.");
}

// --- postupná aktualizace lokality -----------------------------------------------------------

const UPG_STEP = { pending: "čeká", running: "probíhá", done: "hotovo", skipped: "přeskočeno", failed: "chyba" };
const UPG_JOB = { running: "běží", done: "dokončeno", stopped: "zastaveno", cancelled: "zrušeno" };

async function loadUpgrade() {
  let u;
  try { u = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/upgrade`); } catch (e) { return msg(e.message, true); }
  $("upg-wrap").hidden = false;
  const ol = $("upg-cands"); ol.textContent = "";
  for (const c of u.candidates) ol.appendChild(el("li", `${c.label} (${c.role}) ${c.from} → ${c.version}`));
  const running = u.jobs.some((j) => j.status === "running");
  $("upg-start").hidden = !u.candidates.length || running;
  if (!u.candidates.length) ol.appendChild(el("li", "Žádné online zařízení s dostupnou aktualizací", "muted"));
  const ul = $("upg-jobs"); ul.textContent = "";
  for (const j of u.jobs) {
    const li = el("li", `#${j.id} ${UPG_JOB[j.status] || j.status} – ${fmtTime(j.created_at)} (${j.created_by || "?"})` +
                        (j.message ? `: ${j.message}` : ""), j.status === "stopped" ? "sev-warning" : "");
    if (j.status === "running") {
      const b = el("button", "Zastavit", "small danger");
      b.onclick = async () => {
        if (!confirm("Zastavit další kroky? Právě probíhající aktualizace zařízení doběhne.")) return;
        try { await api("POST", `/tenants/${tenantId}/upgrade-jobs/${j.id}/cancel`); loadUpgrade(); } catch (e) { msg(e.message, true); }
      };
      li.append(" ", b);
    }
    const steps = el("ol");
    for (const st of j.steps) {
      steps.appendChild(el("li", `${st.label}: ${st.from || "?"} → ${st.version} – ${UPG_STEP[st.status] || st.status}` +
                                 (st.message ? ` (${st.message})` : ""), st.status === "failed" ? "sev-error" : ""));
    }
    li.appendChild(steps);
    ul.appendChild(li);
  }
}

$("upg-btn").addEventListener("click", () => loadUpgrade());
$("upg-start").addEventListener("click", async () => {
  const n = $("upg-cands").children.length;
  if (!confirm(`Spustit postupnou aktualizaci ${n} zařízení? Každé se restartuje (výpadek ~2–5 min na zařízení).`)) return;
  try { await api("POST", `/tenants/${tenantId}/sites/${siteId()}/upgrade`); msg("Postupná aktualizace spuštěna"); loadUpgrade(); }
  catch (e) { msg(e.message, true); }
});

// --- STP lokality ------------------------------------------------------------------------

$("stp-btn").addEventListener("click", async () => {
  try { renderStp(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/stp`)); } catch (e) { msg(e.message, true); }
});

function renderStp(s) {
  $("stp-wrap").hidden = false;
  $("stp-roots").textContent = s.roots.length
    ? "Root bridge: " + s.roots.map((r) => (r.device || "nespravované zařízení") + ` (${r.root_id})`).join(", ")
    : "Žádný bridge se zapnutým STP";
  const ul = $("stp-warn"); ul.textContent = "";
  for (const x of s.warnings) ul.appendChild(el("li", x.text, "sev-" + (x.severity === "info" ? "info" : "warning")));
  const tb = $("stp-bridges"); tb.textContent = "";
  for (const b of s.bridges) {
    const tr = el("tr");
    tr.append(el("td", b.device || "?"), el("td", b.bridge), el("td", b.protocol, b.protocol === "none" ? "muted" : ""),
              el("td", b.root ? "ano" : (b.root_device || b.root_id || "–")), el("td", b.root_port || "–"),
              el("td", b.root_path_cost != null ? String(b.root_path_cost) : "–"), el("td", b.managed_links.join(", ")));
    tb.appendChild(tr);
  }
  if (!s.bridges.length) { const tr = el("tr"); const td = el("td", "Žádné bridge (inventura běží každých 5 min)", "muted"); td.colSpan = 7; tr.appendChild(td); tb.appendChild(tr); }
}

// --- detail zařízení -----------------------------------------------------------------

let detailId = null;

function kv(tbody, rows) {
  tbody.textContent = "";
  for (const [k, v] of rows) {
    if (v === undefined || v === null || v === "") continue;
    const tr = el("tr"); tr.append(el("th", k), el("td", v)); tbody.appendChild(tr);
  }
}

async function openDetail(id) {
  detailId = id;
  let d;
  try { d = await api("GET", `/tenants/${tenantId}/devices/${id}`); } catch (e) { return msg(e.message, true); }
  show("detail");
  const dev = d.device, sm = d.summary;
  $("d-title").textContent = `${dev.identity || dev.serial} – ${dev.model || ""} (${dev.state})`;
  const w = $("d-warn"); w.textContent = "";
  for (const x of d.warnings) w.appendChild(el("p", (x.severity === "critical" ? "⚠ " : "ⓘ ") + x.text, "sev-" + (x.severity === "critical" ? "error" : "warning")));
  if (dev.inventory_error) w.appendChild(el("p", "Inventura: " + dev.inventory_error, "sev-warning"));
  const listed = (devicesCache || []).find((x) => x.id === id);
  const up = $("d-upgrade");
  up.hidden = !(listed && listed.update_available && dev.state === "adopted");
  if (!up.hidden) { up.textContent = `Aktualizovat → ${listed.latest_version}`; up.onclick = () => upgradeDevice(listed); }
  $("d-notes").hidden = true;
  if (!up.hidden) loadReleaseNotes(id);
  const fw = $("d-firmware");
  fw.hidden = !(dev.state === "adopted" && sm.firmware && sm.firmware_upgrade && sm.firmware_upgrade !== sm.firmware);
  if (!fw.hidden) {
    fw.textContent = `RouterBOOT → ${sm.firmware_upgrade}`;
    fw.onclick = () => {
      if (!confirm(`Aktualizovat RouterBOOT ${sm.firmware} → ${sm.firmware_upgrade}? Zařízení se restartuje.`)) return;
      api("POST", `/tenants/${tenantId}/devices/${id}/commands`, { kind: "firmware" })
        .then(() => msg("Aktualizace RouterBOOT naplánována")).catch((e) => msg(e.message, true));
    };
  }
  const es = $("d-edit-site"); es.textContent = "";
  for (const st of sites) { const o = el("option", st.name); o.value = st.id; es.appendChild(o); }
  es.value = dev.site_id || "";
  const er = $("d-edit-role"); er.textContent = "";
  for (const r of ["unknown", "gateway", "switch", "ap", "ptp"]) { const o = el("option", r); o.value = r; er.appendChild(o); }
  er.value = dev.role || "unknown";
  $("d-edit-note").value = dev.note || "";
  const sec = $("d-security"); sec.textContent = "";
  if (!d.security) sec.appendChild(el("li", "Zatím bez inventury", "muted"));
  else if (!d.security.length) sec.appendChild(el("li", "Bez nálezů ✓", "muted"));
  for (const f of d.security || []) {
    const li = el("li", (f.severity === "critical" ? "⚠ " : "") + f.text,
                  "sev-" + (f.severity === "critical" ? "error" : f.severity));
    if (f.fix) { li.appendChild(document.createTextNode(" → ")); li.appendChild(el("code", f.fix)); }
    sec.appendChild(li);
  }
  const da = $("d-alerts"); da.textContent = "";
  for (const a of d.alerts || []) da.appendChild(el("li", a.message, "sev-" + (a.severity === "critical" ? "error" : a.severity)));
  kv($("d-summary"), [
    ["Sériové číslo", dev.serial], ["RouterOS", dev.ros_version], ["Kanál / dostupná", sm.update_channel ? `${sm.update_channel} / ${sm.latest_version || "?"}` : null],
    ["RouterBOOT", sm.firmware ? `${sm.firmware}${sm.firmware_upgrade && sm.firmware_upgrade !== sm.firmware ? " → " + sm.firmware_upgrade : ""}` : null],
    ["Uptime", sm.uptime], ["CPU", sm.cpu_load != null ? `${sm.cpu_load} % (${sm.cpu || ""})` : null],
    ["Paměť", sm.memory_used_pct != null ? `${sm.memory_used_pct} %` : null], ["Úložiště", sm.hdd_used_pct != null ? `${sm.hdd_used_pct} %` : null],
    ["Lokalita", dev.site], ["Poznámka", dev.note], ["Tunel", `${dev.tunnel_ip || "–"} přes ${dev.concentrator || "–"}`],
    ["Check-in", fmtTime(dev.last_checkin_at)], ["Inventura", fmtTime(dev.inventory_at)], ["Záloha", fmtTime(dev.backup_at)],
  ]);
  // moduly
  const mt = $("d-modules"); mt.textContent = "";
  for (const [mod, mode] of Object.entries(d.modules)) {
    const tr = el("tr"); tr.appendChild(el("th", mod));
    const td = el("td", mode === "manage" ? "spravuje " : "jen sleduje ");
    if (mod === "ntp") {
      const b = el("button", mode === "manage" ? "Přepnout na sledování" : "Povolit správu", "small secondary");
      b.onclick = async () => {
        const to = mode === "manage" ? "observe" : "manage";
        if (to === "manage" && !confirm("Povolit controlleru měnit NTP na tomto zařízení (vždy až po schválení plánu)?")) return;
        try { await api("POST", `/tenants/${tenantId}/devices/${id}/modules`, { module: mod, mode: to }); openDetail(id); }
        catch (e) { msg(e.message, true); }
      };
      td.appendChild(b);
    }
    tr.appendChild(td); mt.appendChild(tr);
  }
  if (d.ntp) kv($("d-ntp"), [["Klient", d.ntp.client_enabled ? "zapnutý" : "vypnutý"], ["Servery", d.ntp.servers.join(", ") || "–"],
                             ["Stav", d.ntp.status], ["NTP server", d.ntp.server_enabled ? "zapnutý" : "vypnutý"], ["Časová zóna", d.ntp.timezone]]);
  // VLAN
  const vs = $("d-vlan-status"), vl = $("d-vlan"); vl.textContent = "";
  $("d-vlan-preview").hidden = true;
  if (d.vlan) {
    const label = { conforming: "odpovídá modelu", simple: "bez VLAN", legacy: "starý způsob – doporučena migrace", unknown: "bez dat" };
    vs.textContent = label[d.vlan.status] || d.vlan.status; vs.className = "badge " + d.vlan.status;
    for (const f of d.vlan.findings) {
      const li = el("li", f.text + (f.objects.length ? " – " + f.objects.join(", ") : ""), "sev-" + f.severity);
      vl.appendChild(li);
    }
    $("d-vlan-plan").hidden = d.vlan.status !== "legacy";
  }
  // rozhraní a sousedé
  renderPorts(id, d);
  const it = $("d-ifaces"); it.textContent = "";
  $("d-traffic").hidden = true;
  for (const i of d.interfaces) {
    const tr = el("tr");
    tr.className = "clickable";
    tr.title = "Graf provozu";
    tr.addEventListener("click", () => showTraffic(id, i.name));
    const p = i.port || {};
    let link = "";
    if (p.rate) link = p.rate + (p.full_duplex === false ? " half-duplex" : "");
    else if (p.link && p.link !== "link-ok") link = p.link;
    if (p.sfp_rx_power) link += ` (SFP RX ${p.sfp_rx_power} dBm)`;
    const vlan = [p.pvid && p.pvid !== "1" ? `PVID ${p.pvid}` : (p.pvid ? "PVID 1" : ""),
                  p.untagged && p.untagged.length ? `U ${p.untagged.join(",")}` : "",
                  p.tagged && p.tagged.length ? `T ${p.tagged.join(",")}` : ""].filter(Boolean).join(" · ");
    const poe = p.poe ? p.poe + (p.poe_power ? ` ${p.poe_power} W` : "") : "";
    const stp = p.stp_role ? p.stp_role.replace("-port", "") + (p.stp_forwarding === false ? " (blokuje)" : "") + (p.edge ? " · edge" : "") : "";
    tr.append(el("td", i.name), el("td", i.type), el("td", i.disabled ? "vypnuté" : i.running ? "běží" : "neběží", i.running ? "" : "sev-warning"),
              el("td", link, p.full_duplex === false ? "sev-warning" : ""), el("td", vlan), el("td", stp, p.stp_forwarding === false ? "sev-warning" : ""), el("td", poe),
              el("td", i.mac || ""), el("td", i.link_downs || "0"), el("td", `${i.rx_error || 0} / ${i.tx_error || 0}`), el("td", i.comment || ""));
    it.appendChild(tr);
  }
  $("d-ifaces-n").textContent = `(${d.interfaces.length})`;
  $("d-neigh-n").textContent = `(${(d.neighbors || []).length})`;
  const nt = $("d-neigh"); nt.textContent = "";
  for (const n of d.neighbors) {
    const tr = el("tr");
    tr.append(el("td", n.interface), el("td", n.identity || ""), el("td", n.board || n.platform || ""), el("td", n.address || ""), el("td", n["mac-address"] || ""));
    nt.appendChild(tr);
  }
  // zálohy
  const bt = $("d-backups"); bt.textContent = ""; $("d-diff").hidden = true;
  for (const b of d.backups) {
    const tr = el("tr");
    tr.append(el("td", new Date(b.taken_at).toLocaleString("cs-CZ")), el("td", b.reason), el("td", `${Math.round(b.size_bytes / 1024)} kB`),
              el("td", b.changed ? "ano" : "–"));
    const act = el("td");
    if (b.changed) {
      const df = el("button", "Rozdíl", "small secondary");
      df.onclick = async () => {
        try { const r = await api("GET", `/tenants/${tenantId}/devices/${id}/backups/${b.id}/diff`);
              const pre = $("d-diff"); pre.textContent = r.diff || "Bez rozdílu."; pre.hidden = false; }
        catch (e) { msg(e.message, true); }
      };
      act.appendChild(df);
    }
    const a = el("a", " Stáhnout"); a.href = `/api/admin/tenants/${tenantId}/devices/${id}/backups/${b.id}/download`;
    a.title = "Celý export včetně hesel (jen admin tenanta, auditováno)";
    act.appendChild(a);
    tr.appendChild(act); bt.appendChild(tr);
  }
  if (!d.backups.length) { const tr = el("tr"); const td = el("td", "Zatím žádná záloha", "muted"); td.colSpan = 5; tr.appendChild(td); bt.appendChild(tr); }
  // historie
  const ht = $("d-history"); ht.textContent = "";
  const hist = d.plans.map((p) => [p.created_at, `plán ${p.module}: ${p.summary}`, p.status])
    .concat(d.commands.map((c) => [c.created_at, `příkaz ${c.kind}`, c.status]))
    .sort((a, b) => (a[0] < b[0] ? 1 : -1)).slice(0, 25);
  for (const [t, what, st] of hist) { const tr = el("tr"); tr.append(el("td", new Date(t).toLocaleString("cs-CZ")), el("td", what), el("td", st)); ht.appendChild(tr); }
}

$("d-close").addEventListener("click", () => { show("app"); loadDevices(); });
$("d-backup").addEventListener("click", async () => {
  try { const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/backup`); msg(r.detail); } catch (e) { msg(e.message, true); }
});
$("d-vlan-plan").addEventListener("click", async () => {
  try {
    const r = await api("GET", `/tenants/${tenantId}/devices/${detailId}/vlan-migration`);
    const p = r.plan;
    const steps = $("d-vlan-steps"), notes = $("d-vlan-notes");
    steps.textContent = ""; notes.textContent = "";
    if (!p.ok) for (const b of p.blockers) notes.appendChild(el("li", "Nelze automaticky: " + b, "sev-error"));
    for (const s of p.steps) steps.appendChild(el("li", s));
    for (const w of p.warnings) notes.appendChild(el("li", w));
    $("d-vlan-script").textContent = p.script || "";
    $("d-vlan-preview").hidden = false;
  } catch (e) { msg(e.message, true); }
});
setInterval(() => {
  if (!$("app").hidden && tenantId) Promise.all([loadDevices(), loadAlerts()]).then(renderOverview).catch(() => {});
}, 15000);

boot();
