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
  await loadSites();  // nejdřív lokality – výběr lokality u čekajících zařízení je potřebuje
  await Promise.all([loadDevices(), loadAudit(), loadPlans(), loadAlerts(), loadUsers()]);
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
  for (const w of ["topo-wrap", "clients-wrap", "wifi-wrap", "stp-wrap", "upg-wrap", "infra-wrap"]) $(w).hidden = true;
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

// --- předvolby zobrazení (jen v tomto prohlížeči) --------------------------------------------

const prefs = (() => { try { return JSON.parse(localStorage.getItem("mtc-view") || "{}"); } catch (e) { return {}; } })();
function savePrefs() { try { localStorage.setItem("mtc-view", JSON.stringify(prefs)); } catch (e) { /* bez úložiště */ } }

// přepínač ve stylu segmented control: options = [[hodnota, popisek], …]
function segmented(box, options, value, onChange) {
  box.textContent = "";
  box.className = "seg";
  for (const [v, label, disabled] of options) {
    const b = el("button", label, v === value ? "active" : "");
    b.type = "button";
    b.disabled = !!disabled;
    b.addEventListener("click", () => { if (v !== value) onChange(v); });
    box.appendChild(b);
  }
}

// --- panel portů s pohledy (linka, native VLAN, VLAN, PoE, chyby) ----------------------------

const PORT_RE = /^(ether|sfp|combo|qsfp)/;
const PORT_VIEWS = [["link", "Linka"], ["pvid", "Native VLAN"], ["vlan", "VLAN tagování"], ["poe", "PoE"], ["err", "Chyby"]];
let portCtx = null;

function portNum(name) {
  const m = String(name).match(/(\d+)(?!.*\d)/);
  return m ? Number(m[1]) : 0;
}

function shortName(s, n = 9) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function vlanSet(list) {
  const out = new Set();
  for (const x of list || []) for (const part of String(x).split(",")) if (part) out.add(part.trim());
  return out;
}

function renderPorts(devId, d) {
  const phys = (d.interfaces || []).filter((i) => i.type === "ether" && PORT_RE.test(i.name));
  const neigh = {};
  for (const n of d.neighbors || []) {
    for (const part of String(n.interface || "").split(/[,/]/)) if (part && !neigh[part]) neigh[part] = n;
  }
  const managed = new Set((devicesCache || []).map((x) => x.identity).filter(Boolean));
  const bySfp = (sfp) => phys.filter((i) => /^(sfp|qsfp)/.test(i.name) === sfp).sort((a, b) => portNum(a.name) - portNum(b.name));
  portCtx = { devId, phys, cu: bySfp(false), sfp: bySfp(true), neigh, managed };
  drawPorts();
}

function drawPorts() {
  const c = portCtx;
  const view = PORT_VIEWS.some(([v]) => v === prefs.portView) ? prefs.portView : "link";
  segmented($("d-ports-views"), PORT_VIEWS, view, (v) => { prefs.portView = v; savePrefs(); drawPorts(); });
  const box = $("d-ports"); box.textContent = "";
  $("d-vlan-filter").hidden = view !== "vlan";
  portLegend(view);
  if (!c.phys.length) { box.appendChild(el("p", "Žádné fyzické porty v inventuře", "muted")); return; }
  if (view === "vlan") return drawVlanMatrix(box);
  const row = el("div", null, "port-row");
  for (const group of [c.cu, c.sfp]) if (group.length) row.appendChild(portGroup(group, (i) => portTile(i, view), true));
  box.appendChild(row);
}

function drawVlanMatrix(box) {
  const c = portCtx;
  const vids = new Set();
  for (const i of c.phys) {
    const p = i.port || {};
    for (const v of [...vlanSet(p.tagged), ...vlanSet(p.untagged)]) vids.add(v);
    if (p.pvid) vids.add(String(p.pvid));
  }
  const q = $("d-vlan-filter").value.trim();
  const list = [...vids].filter((v) => !q || v.includes(q))
    .sort((a, b) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0));
  if (!list.length) { box.appendChild(el("p", vids.size ? "Žádná VLAN neodpovídá filtru" : "Porty nejsou v bridgi s VLAN", "muted")); return; }
  for (const vid of list) {
    const members = c.phys.filter((i) => vlanMember(i, vid)).length;
    const r = el("div", null, "vlan-row");
    const name = el("div", `VLAN ${vid}`, "vlan-name");
    name.appendChild(el("span", ` · ${members} port${members === 1 ? "" : members < 5 && members ? "y" : "ů"}`, "muted"));
    r.appendChild(name);
    const row = el("div", null, "port-row");
    for (const group of [c.cu, c.sfp]) if (group.length) row.appendChild(portGroup(group, (i) => vlanTile(i, vid), false));
    r.appendChild(row);
    box.appendChild(r);
  }
}

function vlanMember(i, vid) {
  const p = i.port || {};
  if (vlanSet(p.tagged).has(vid)) return "tagged";
  if (vlanSet(p.untagged).has(vid) || String(p.pvid) === vid) return "native";
  return null;
}

function portGroup(ports, tileFn, withLabels) {
  // víc než 8 portů: liché nahoře, sudé dole (jako na switchi); jinak jedna řada
  const twoRows = ports.length > 8;
  const top = twoRows ? ports.filter((_, k) => k % 2 === 0) : ports;
  const bottom = twoRows ? ports.filter((_, k) => k % 2 === 1) : [];
  const g = el("div", null, "port-group" + (withLabels ? "" : " compact"));
  g.style.gridTemplateColumns = `repeat(${top.length}, 52px)`;
  const place = (node, row, col) => { node.style.gridRow = String(row); node.style.gridColumn = String(col); g.appendChild(node); };
  const off = withLabels ? 1 : 0;
  top.forEach((i, k) => { if (withLabels) place(portLabel(i, "above"), 1, k + 1); place(tileWrap(i, tileFn), 1 + off, k + 1); });
  bottom.forEach((i, k) => { place(tileWrap(i, tileFn), 2 + off, k + 1); if (withLabels) place(portLabel(i, "below"), 4, k + 1); });
  return g;
}

function tileWrap(i, tileFn) {
  const t = tileFn(i);
  t.type = "button";
  if (/^(sfp|qsfp)/.test(i.name)) { t.className += " sfp"; t.appendChild(el("span", /plus/.test(i.name) ? "SFP+" : "SFP", "tag")); }
  t.appendChild(el("span", String(portNum(i.name)), "num"));
  t.title = portTooltip(i);
  t.addEventListener("click", () => selectPortTraffic(i.name));
  if (trafficState.iface === i.name) t.className += " selected";
  return t;
}

function portLabel(i, pos) {
  const n = portCtx.neigh[i.name], p = i.port || {};
  const c = el("div", null, "port-label " + pos);
  const who = n ? (n.identity || n["mac-address"] || "") : "";
  const w = p.poe_power && Number(p.poe_power) > 0 ? `${Number(p.poe_power).toFixed(1)} W` : "";
  const name = el("span", shortName(who), portCtx.managed.has(who) ? "managed" : "");
  if (pos === "above") c.append(el("span", w, "poe-w"), name); else c.append(name, el("span", w, "poe-w"));
  if (n) c.title = `${n.identity || ""} ${n["mac-address"] || ""} ${n.board || n.platform || ""}`.trim();
  return c;
}

function linkClass(i) {
  const p = i.port || {};
  if (i.disabled) return "off";
  if (!i.running || (p.link && p.link !== "link-ok")) return "down";
  return p.rate && !/G/.test(p.rate) ? "fe" : "gbe";
}

function portTile(i, view) {
  const p = i.port || {};
  const lc = linkClass(i);
  const t = el("button", null, "port");
  if (view === "link") {
    t.className += " " + lc;
    if (p.full_duplex === false || p.stp_forwarding === false) t.className += " blk";
    const n = portCtx.neigh[i.name];
    const gw = n && (devicesCache || []).some((x) => x.identity === n.identity && x.role === "gateway");
    const uplink = p.stp_role === "root-port" || (!p.stp_role && gw);
    const poeOn = p.poe && /powered-on|on/.test(p.poe);
    t.appendChild(el("span", (poeOn ? "⚡" : "") + (uplink ? "↑" : ""), "ico"));
  } else if (view === "pvid") {
    t.className += " plain" + (lc === "down" || lc === "off" ? " dim" : "");
    t.appendChild(el("span", p.pvid ? String(p.pvid) : "–", "big"));
  } else if (view === "poe") {
    const w = Number(p.poe_power) || 0;
    if (w > 0) t.className += w >= 15 ? " poe3" : w >= 5 ? " poe2" : " poe1";
    else t.className += p.poe ? " plain" : " down";
    t.appendChild(el("span", w > 0 ? w.toFixed(1) : (p.poe ? "0" : ""), "big small"));
  } else if (view === "err") {
    const e = (Number(i.rx_error) || 0) + (Number(i.tx_error) || 0);
    const ld = Number(i.link_downs) || 0;
    t.className += e > 100 ? " err2" : e > 0 ? " err1" : lc === "down" || lc === "off" ? " down" : " plain";
    if (e) t.appendChild(el("span", e > 999 ? Math.round(e / 1000) + "k" : String(e), "big small"));
    // 1–2 výpadky od startu jsou běžné (zapojení), víc už stojí za pozornost
    if (ld >= 3) t.appendChild(el("span", `↯${ld}`, "ico br"));
  }
  return t;
}

function vlanTile(i, vid) {
  const m = vlanMember(i, vid);
  return el("button", null, "port " + (m === "tagged" ? "vt" : m === "native" ? "vn" : "down"));
}

function portTooltip(i) {
  const p = i.port || {}, n = portCtx.neigh[i.name];
  return [
    i.name + (i.comment ? ` – ${i.comment}` : ""),
    i.disabled ? "vypnutý" : (i.running ? `linka ${p.rate || "ok"}${p.full_duplex === false ? " half-duplex" : ""}` : "bez linky"),
    p.pvid ? `PVID ${p.pvid}` + (p.tagged && p.tagged.length ? `, tagované ${p.tagged.join(",")}` : "") : "",
    p.stp_role ? `STP ${p.stp_role}${p.stp_forwarding === false ? " (blokuje)" : ""}` : "",
    p.poe ? `PoE ${p.poe}${p.poe_power ? " " + p.poe_power + " W" : ""}` : "",
    p.sfp_rx_power ? `SFP RX ${p.sfp_rx_power} dBm` : "",
    n ? `soused: ${n.identity || n["mac-address"]}` : "",
    `chyby RX/TX ${i.rx_error || 0}/${i.tx_error || 0}, výpadky linky ${i.link_downs || 0}`,
  ].filter(Boolean).join("\n");
}

const LEGENDS = {
  link: [["gbe", "GbE+"], ["fe", "FE"], ["down", "bez linky"], ["off", "vypnutý"], [null, "⚡ PoE"], [null, "↑ uplink"], ["blk", "blokuje STP / half-duplex"]],
  pvid: [["plain", "číslo = native VLAN (PVID)"], ["dim", "port bez linky"]],
  vlan: [["vt", "tagovaná"], ["vn", "native (netagovaná)"], ["down", "není členem"]],
  poe: [["poe1", "< 5 W"], ["poe2", "5–15 W"], ["poe3", "≥ 15 W"], ["plain", "PoE bez odběru"], ["down", "bez PoE"]],
  err: [["plain", "bez chyb"], ["err1", "chyby"], ["err2", "> 100 chyb"], [null, "↯ výpadky linky (od 3)"]],
};

function portLegend(view) {
  const lg = $("d-ports-legend"); lg.textContent = "";
  for (const [cls, label] of LEGENDS[view]) {
    const s = el("span");
    if (cls) s.appendChild(el("i", null, "sw " + cls));
    s.appendChild(document.createTextNode(label));
    lg.appendChild(s);
  }
  if (view === "poe") {
    const total = portCtx.phys.reduce((a, i) => a + (Number((i.port || {}).poe_power) || 0), 0);
    lg.appendChild(el("b", `celkem ${total.toFixed(1)} W`));
  }
  lg.appendChild(el("span", "klik na port = jeho graf", "muted"));
}

$("d-vlan-filter").addEventListener("input", () => { if (portCtx) drawPorts(); });

// --- graf provozu (celé zařízení / port; provoz, chyby, zahozené) ------------------------------

const TR_METRICS = [["bps", "Provoz"], ["err", "Chyby"], ["drop", "Zahozené"]];
const TR_DIRS = [["all", "Vše"], ["rx", "↓ Přijato"], ["tx", "↑ Odesláno"]];
const TR_RANGES = [[6, "6H"], [24, "1D"], [168, "1T"], [720, "1M"], [8760, "1R"]];
const trafficState = {
  iface: "*", lastPort: null,
  metric: prefs.trMetric || "bps", dir: prefs.trDir || "all", hours: Number(prefs.trHours) || 24,
};
let trafficData = null;

function fmtBps(v) {
  if (v == null) return "–";
  const u = [["G", 1e9], ["M", 1e6], ["k", 1e3]];
  for (const [n, f] of u) if (v >= f) return (v / f).toFixed(v / f >= 10 ? 0 : 1) + ` ${n}b/s`;
  return `${Math.round(v)} b/s`;
}

function fmtVal(v) {
  if (trafficState.metric === "bps") return fmtBps(v);
  return v == null ? "–" : `${Math.round(v)}`;
}

function selectPortTraffic(name) {
  trafficState.lastPort = name;
  showTraffic(detailId, name);
  loadPortEditor(name);
}

// --- editor portu (změny s automatickým návratem) ---------------------------------------------

async function loadPortEditor(port) {
  const box = $("port-edit");
  let st;
  try { st = await api("GET", `/tenants/${tenantId}/devices/${detailId}/ports/${encodeURIComponent(port)}`); }
  catch (e) { box.hidden = true; return; }
  box.hidden = false; box.textContent = "";
  const head = el("div", null, "pe-head");
  head.append(el("b", `Port ${st.port}`), el("span", st.comment ? ` – ${st.comment}` : "", "muted"));
  if (st.uplink) head.appendChild(el("span", "uplink – změna může odpojit zařízení (vrátí se samo)", "badge warnb"));
  const close = el("button", "×", "small secondary pe-close"); close.type = "button";
  close.onclick = () => { box.hidden = true; };
  head.appendChild(close);
  box.appendChild(head);
  if (st.module_mode !== "manage") {
    const b = el("div", null, "banner info");
    b.appendChild(document.createTextNode("Správa portů je na tomto zařízení vypnutá (jen sleduje). "));
    const en = el("button", "Povolit správu portů", "small"); en.type = "button";
    en.onclick = async () => {
      if (!confirm("Povolit controlleru měnit porty tohoto zařízení? Každá změna se aplikuje s automatickým návratem.")) return;
      try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/modules`, { module: "ports", mode: "manage" }); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    b.appendChild(en);
    box.appendChild(b);
  }
  const f = el("div", null, "pe-grid");
  const field = (label, input) => { const l = el("label", null, "pe-field"); l.append(el("span", label), input); f.appendChild(l); return input; };
  const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = !st.disabled;
  field("Zapnutý", enabled);
  const comment = el("input"); comment.maxLength = 80; comment.value = st.comment || ""; comment.placeholder = "např. kamera vjezd";
  field("Popis", comment);
  let poe = null;
  if (st.poe !== null) {
    poe = el("select");
    for (const [v, l] of [["auto-on", "auto"], ["forced-on", "vždy zapnuto"], ["off", "vypnuto"]]) { const o = el("option", l); o.value = v; poe.appendChild(o); }
    poe.value = st.poe;
    field("PoE", poe);
  }
  let pvid = null, edge = null, tagBox = null, addVlan = null;
  const tagChecks = [];
  if (st.bridge) {
    pvid = el("input"); pvid.type = "number"; pvid.min = 1; pvid.max = 4094; pvid.value = st.pvid || 1;
    field("Native VLAN (PVID)", pvid);
    edge = el("select");
    for (const [v, l] of [["auto", "auto"], ["yes", "ano (koncové zařízení)"], ["no", "ne"]]) { const o = el("option", l); o.value = v; edge.appendChild(o); }
    edge.value = st.edge || "auto";
    field("STP edge", edge);
    tagBox = el("div", null, "pe-tags");
    const all = [...new Set([...(st.known_vlans || []), ...(st.tagged || [])])].sort((a, b) => a - b);
    for (const v of all) {
      const l = el("label", null, "check"); const c = el("input"); c.type = "checkbox"; c.value = v;
      c.checked = (st.tagged || []).includes(v);
      tagChecks.push(c);
      l.append(c, document.createTextNode(` ${v}`)); tagBox.appendChild(l);
    }
    addVlan = el("input"); addVlan.placeholder = "další VLAN, např. 50,60"; addVlan.size = 14;
    tagBox.appendChild(addVlan);
    field("Tagované VLAN", tagBox);
    if ((st.tagged_locked || []).length)
      f.appendChild(el("p", `Řádky VLAN ${st.tagged_locked.join("; ")} obsahují víc ID – ty upravuj v RouterOS.`, "muted"));
    if (!st.vlan_filtering) f.appendChild(el("p", "Bridge nemá zapnuté vlan-filtering – PVID a tagování se zatím neuplatní.", "sev-warning"));
  }
  box.appendChild(f);
  // profily portů: výběr vyplní formulář, použití jde stejnou cestou (plán s návratem)
  if (st.bridge || poe) {
    const pr = el("div", null, "pe-prof");
    pr.appendChild(el("span", "Profil portu", "muted"));
    const sel = el("select"); sel.setAttribute("aria-label", "Profil portu");
    const o0 = el("option", "— vlastní nastavení —"); o0.value = ""; sel.appendChild(o0);
    for (const pf of st.profiles || []) { const o = el("option", pf.name); o.value = String(pf.id); sel.appendChild(o); }
    sel.value = st.profile != null ? String(st.profile) : "";
    const del = el("button", "Smazat profil", "small secondary"); del.type = "button";
    del.hidden = !sel.value;
    sel.addEventListener("change", () => {
      del.hidden = !sel.value;
      const pf = (st.profiles || []).find((x) => String(x.id) === sel.value);
      if (!pf) return;
      if (pvid && pf.pvid != null) pvid.value = pf.pvid;
      if (edge && pf.edge) edge.value = pf.edge;
      if (poe && pf.poe) poe.value = pf.poe;
      if (tagBox && pf.tagged) {
        for (const c of tagChecks) c.checked = pf.tagged.includes(c.value);
        addVlan.value = pf.tagged.filter((v) => !tagChecks.some((c) => c.value === v)).join(",");
      }
    });
    del.onclick = async () => {
      const pf = (st.profiles || []).find((x) => String(x.id) === sel.value);
      if (!pf || !confirm(`Smazat profil „${pf.name}“? Porty se tím nezmění.`)) return;
      try { await api("DELETE", `/tenants/${tenantId}/port-profiles/${pf.id}`); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    pr.append(sel, del);
    box.insertBefore(pr, f);
  }
  const desired = () => {
    const d = { disabled: !enabled.checked, comment: comment.value };
    if (poe) d.poe = poe.value;
    if (pvid) d.pvid = Number(pvid.value);
    if (edge) d.edge = edge.value;
    if (tagBox) {
      const t = tagChecks.filter((c) => c.checked).map((c) => c.value);
      for (const v of String(addVlan.value || "").split(/[ ,;]+/)) if (v) t.push(v);
      d.tagged = t;
    }
    return d;
  };
  const send = async (apply) => {
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/ports/${encodeURIComponent(port)}/plan`, { desired: desired(), apply });
      msg(apply ? `Odesláno: ${r.summary}. Zařízení změnu provede do minuty; když se do 5 min neozve, samo ji vrátí.`
                : `Plán uložen: ${r.summary} (čeká na schválení v Plánech změn)`);
      for (const w of r.warnings || []) msg(w, true);
      loadPlans();
    } catch (e) { msg(e.message, true); }
  };
  const acts = el("div", null, "pe-acts");
  const go = el("button", "Použít (s automatickým návratem)", "small"); go.type = "button";
  go.onclick = () => { if (confirm("Použít změnu portu? Když se zařízení po změně do 5 minut neozve, vrátí ji samo.")) send(true); };
  const save = el("button", "Uložit jako plán", "small secondary"); save.type = "button"; save.onclick = () => send(false);
  acts.append(go, save);
  if (st.bridge || poe) {
    const mk = el("button", "Uložit jako profil…", "small secondary"); mk.type = "button";
    mk.onclick = async () => {
      const name = prompt("Jméno profilu (např. Kamery, AP trunk):");
      if (!name) return;
      const d = desired();
      try {
        await api("POST", `/tenants/${tenantId}/port-profiles`, { name, pvid: d.pvid, tagged: d.tagged, poe: d.poe, edge: d.edge });
        msg(`Profil „${name}“ uložen`); loadPortEditor(port);
      } catch (e) { msg(e.message, true); }
    };
    acts.appendChild(mk);
  }
  if (st.poe && st.poe !== "off") {
    const pc = el("button", "Restart PoE (power-cycle)", "small secondary"); pc.type = "button";
    pc.onclick = async () => {
      if (!confirm(`Vypnout a zapnout napájení na ${port} (zařízení na portu se restartuje)?`)) return;
      try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/commands`, { kind: "poe_cycle", port }); msg("Power-cycle naplánován"); }
      catch (e) { msg(e.message, true); }
    };
    acts.appendChild(pc);
  }
  box.appendChild(acts);
  if ((st.history || []).length) {
    box.appendChild(el("h4", "Historie portu (30 dní)"));
    const ul = el("ul", null, "ph-list");
    for (const e of st.history) ul.appendChild(phItem(e, false));
    box.appendChild(ul);
  }
}

async function showTraffic(devId, name) {
  if (name !== undefined) trafficState.iface = name;
  if (trafficState.iface !== "*") trafficState.lastPort = trafficState.iface;
  drawTrafficBar();
  if (portCtx) drawPorts();
  let r;
  try {
    r = await api("GET", `/tenants/${tenantId}/devices/${devId}/traffic?interface=${encodeURIComponent(trafficState.iface)}&hours=${trafficState.hours}`);
  } catch (e) { return msg(e.message, true); }
  if (detailId !== devId || r.interface !== trafficState.iface) return;
  trafficData = r;
  $("d-traffic").hidden = false;
  drawTraffic();
}

function drawTrafficBar() {
  const s = trafficState;
  const reload = () => { savePrefs(); showTraffic(detailId); };
  segmented($("tr-scope"), [["*", "Celé zařízení"], [s.lastPort || "", s.lastPort ? `Port ${s.lastPort}` : "Port (klikni na port)", !s.lastPort]],
            s.iface, (v) => { s.iface = v; showTraffic(detailId); });
  segmented($("tr-metric"), TR_METRICS, s.metric, (v) => { s.metric = prefs.trMetric = v; savePrefs(); drawTrafficBar(); drawTraffic(); });
  segmented($("tr-dir"), TR_DIRS, s.dir, (v) => { s.dir = prefs.trDir = v; savePrefs(); drawTrafficBar(); drawTraffic(); });
  segmented($("tr-range"), TR_RANGES, s.hours, (v) => { s.hours = prefs.trHours = v; reload(); });
}

function trafficSeries() {
  const s = trafficState, hourly = trafficData && trafficData.resolution === "1h";
  const keys = s.metric === "bps" ? ["rx", "tx"] : s.metric === "err" ? ["rx_err", "tx_err"] : ["rx_drop", "tx_drop"];
  const out = [];
  if (s.dir !== "tx") out.push({ key: keys[0], cls: "rx", label: "↓ Přijato", max: s.metric === "bps" && hourly ? "rx_max" : null });
  if (s.dir !== "rx") out.push({ key: keys[1], cls: "tx", label: "↑ Odesláno", max: s.metric === "bps" && hourly ? "tx_max" : null });
  return out;
}

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (v <= m * p) return m * p;
  return 10 * p;
}

function drawTraffic() {
  const svg = $("d-traffic-svg"); svg.textContent = "";
  // viewBox = skutečná šířka v pixelech, ať se text os neroztahuje
  const W = Math.max(400, Math.round(svg.clientWidth || (svg.parentNode && svg.parentNode.clientWidth) || 800)), H = 240;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const r = trafficData, s = trafficState;
  $("d-traffic-h").textContent = s.iface === "*" ? "Provoz – celé zařízení (součet fyzických portů)" : `Provoz – port ${s.iface}`;
  const now = $("tr-now"); now.textContent = "";
  $("tr-tip").hidden = true;
  if (!r) return;
  const pts = r.points || [];
  const series = trafficSeries();
  if (pts.length < 2) {
    svg.style.display = "none";
    $("d-traffic-info").textContent = "Zatím málo dat – vzorek se ukládá každých 5 minut, první body do ~10 min po adopci.";
    return;
  }
  svg.style.display = "";
  const last = pts[pts.length - 1];
  for (const se of series) now.appendChild(el("span", `${se.label} ${fmtVal(last[se.key])}`, "now-" + se.cls));
  const L = 68, R = 12, T = 12, B = 26;
  const t0 = new Date(pts[0].t).getTime(), t1 = new Date(last.t).getTime();
  let peak = 0;
  for (const q of pts) for (const se of series) peak = Math.max(peak, Number(q[se.key]) || 0, se.max ? Number(q[se.max]) || 0 : 0);
  // počty (chyby, zahozené) jen v celých číslech
  const max = s.metric === "bps" ? niceMax(peak) : Math.max(4, Math.ceil(peak / 4) * 4);
  const x = (t) => L + (W - L - R) * (t1 > t0 ? (new Date(t).getTime() - t0) / (t1 - t0) : 0);
  const y = (v) => T + (H - T - B) * (1 - (Number(v) || 0) / max);
  for (const f of [0, 0.25, 0.5, 0.75, 1]) {
    svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y(max * f), y2: y(max * f), class: "grid" }));
    svg.appendChild(svgEl("text", { x: L - 6, y: y(max * f) + 4, class: "ylab" }, fmtVal(max * f)));
  }
  const fmtT = (t) => new Date(t).toLocaleString("cs-CZ", s.hours > 48 ? { day: "numeric", month: "numeric", hour: "2-digit" }
                                                                         : { hour: "2-digit", minute: "2-digit" });
  for (let k = 0; k <= 4; k++) {
    const tt = t0 + (t1 - t0) * k / 4;
    svg.appendChild(svgEl("text", { x: L + (W - L - R) * k / 4, y: H - 6, class: "xlab" + (k === 0 ? " first" : k === 4 ? " last" : "") }, fmtT(tt)));
  }
  for (const se of series) {
    const line = pts.map((q) => `${x(q.t).toFixed(1)},${y(q[se.key]).toFixed(1)}`);
    svg.appendChild(svgEl("polygon", { points: `${x(pts[0].t).toFixed(1)},${y(0)} ${line.join(" ")} ${x(last.t).toFixed(1)},${y(0)}`, class: "area " + se.cls }));
    if (se.max) svg.appendChild(svgEl("polyline", { points: pts.map((q) => `${x(q.t).toFixed(1)},${y(q[se.max]).toFixed(1)}`).join(" "), class: se.cls + " max" }));
    svg.appendChild(svgEl("polyline", { points: line.join(" "), class: se.cls }));
  }
  const guide = svgEl("line", { x1: 0, x2: 0, y1: T, y2: H - B, class: "guide", visibility: "hidden" });
  svg.appendChild(guide);
  const dots = series.map((se) => { const c = svgEl("circle", { r: 3.5, class: "dot " + se.cls, visibility: "hidden" }); svg.appendChild(c); return c; });
  $("d-traffic-info").textContent = (r.resolution === "1h" ? "Hodinové průměry" + (s.metric === "bps" ? ", čárkovaně špičky" : " (součet za hodinu)")
                                                         : "5min vzorky" + (s.metric === "bps" ? "" : " (počet za 5 min)")) + ` · maximum ${fmtVal(peak)}`;
  const tip = $("tr-tip");
  svg.onmousemove = (ev) => {
    const rect = svg.getBoundingClientRect();
    const vx = (ev.clientX - rect.left) / rect.width * W;
    let best = 0, bd = Infinity;
    pts.forEach((q, k) => { const d = Math.abs(x(q.t) - vx); if (d < bd) { bd = d; best = k; } });
    const q = pts[best], gx = x(q.t);
    guide.setAttribute("x1", gx); guide.setAttribute("x2", gx); guide.setAttribute("visibility", "visible");
    series.forEach((se, k) => { dots[k].setAttribute("cx", gx); dots[k].setAttribute("cy", y(q[se.key])); dots[k].setAttribute("visibility", "visible"); });
    tip.textContent = "";
    tip.appendChild(el("b", new Date(q.t).toLocaleString("cs-CZ", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" })));
    for (const se of series) {
      const row = el("div", null, "tip-" + se.cls);
      row.append(el("span", se.label), el("span", fmtVal(q[se.key]) + (se.max && q[se.max] != null ? ` (špička ${fmtVal(q[se.max])})` : "")));
      tip.appendChild(row);
    }
    tip.hidden = false;
    const px = (gx / W) * rect.width;
    tip.style.left = `${Math.min(px + 14, rect.width - 200)}px`;
  };
  svg.onmouseleave = () => {
    tip.hidden = true;
    guide.setAttribute("visibility", "hidden");
    for (const d of dots) d.setAttribute("visibility", "hidden");
  };
}

let resizeTimer = null;
if (typeof window.addEventListener === "function") {
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (trafficData && !$("detail").hidden) drawTraffic(); }, 150);
  });
}

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

// --- infrastruktura lokality (strom WAN → gateway → switche → AP) ----------------------------

let infraData = null;

$("infra-btn").addEventListener("click", async () => {
  try { infraData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/infra`); } catch (e) { return msg(e.message, true); }
  $("infra-wrap").hidden = false;
  renderInfra();
});
$("infra-ext").addEventListener("change", () => { if (infraData) renderInfra(); });

function miniPorts(ports) {
  const strip = el("div", null, "strip");
  const rows = ports.length > 8 ? 2 : 1;
  strip.style.gridTemplateRows = `repeat(${rows}, 7px)`;
  for (const p of ports) { const q = el("span", null, "pp " + p.cls); q.title = p.name; strip.appendChild(q); }
  return strip;
}

function renderInfra() {
  const d = infraData;
  const warn = $("infra-warn"); warn.textContent = "";
  for (const w of d.warnings) {
    const b = el("div", null, "banner " + (w.severity === "warning" ? "warn" : "info"));
    b.append(el("b", w.severity === "warning" ? "⚠ " : "ⓘ "), document.createTextNode(w.text));
    warn.appendChild(b);
  }
  // seznam vlevo
  const list = $("infra-list"); list.textContent = "";
  for (const n of d.nodes) {
    const c = el("div", null, "infra-dev" + (n.state === "offline" ? " offline" : ""));
    const head = el("div", null, "row1");
    head.append(el("b", n.label), el("span", n.model || "", "muted"));
    c.appendChild(head);
    if (n.ports.length) c.appendChild(miniPorts(n.ports));
    const foot = el("div", null, "row2 muted");
    foot.append(el("span", (n.version || "").split(" ")[0]), el("span", n.poe_w ? `⚡ ${n.poe_w} W` : ""),
                el("span", `CPU ${n.cpu != null ? n.cpu + " %" : "–"}`), el("span", `RAM ${n.mem != null ? n.mem + " %" : "–"}`));
    c.appendChild(foot);
    c.addEventListener("click", () => openDetail(n.id));
    list.appendChild(c);
  }
  drawInfra();
}

function drawInfra() {
  const d = infraData, svg = $("infra-svg"); svg.textContent = "";
  const NW = 250, NH = 66, GX = 76, LV = 180, WH = 56, TOP = 20, EW = 150, EH = 40;
  const showExt = $("infra-ext").checked;
  const byId = {}, kids = {};
  for (const n of d.nodes) { byId[n.id] = n; kids[n.id] = []; }
  for (const n of d.nodes) if (n.parent && byId[n.parent]) kids[n.parent].push(n);
  if (showExt) {
    // nespravovaní sousedé jako listy stromu; sousedy na WAN portu zastupuje karta WAN
    const wanPorts = new Set(d.wans.map((w) => `${w.device_id}|${w.port}`));
    d.externals.filter((e) => byId[e.parent] && !wanPorts.has(`${e.parent}|${e.port}`)).slice(0, 80).forEach((e, k) => {
      kids[e.parent].push({ id: "ext" + k, ext: true, label: e.label, platform: e.platform, parent_port: e.port,
                            level: byId[e.parent].level + 1, parent: e.parent });
    });
  }
  const all = [...d.nodes, ...Object.values(kids).flat().filter((x) => x.ext)];
  for (const n of all) kids[n.id] = kids[n.id] || [];
  // rozložení: listy dostanou sloupce, rodič je uprostřed nad dětmi
  let slot = 0;
  const pos = {};
  const place = (n) => {
    const ch = kids[n.id];
    if (!ch.length) { pos[n.id] = slot; slot += n.ext ? 0.7 : 1; return; }
    ch.forEach(place);
    pos[n.id] = (pos[ch[0].id] + pos[ch[ch.length - 1].id]) / 2;
  };
  const roots = d.nodes.filter((n) => !n.parent || !byId[n.parent]);
  roots.forEach((r) => { place(r); slot += 0.3; });
  const wanRoot = d.wans.length ? 1 : 0;
  const levels = Math.max(0, ...all.map((n) => n.level));
  const W = Math.max(slot, Math.max(1, d.wans.length) * 1.5) * (NW + GX) + 60;
  const H = TOP + wanRoot * (WH + 90) + (levels + 1) * LV;
  svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const cx = (n) => 60 + pos[n.id] * (NW + GX) + NW / 2;
  const y = (n) => TOP + wanRoot * (WH + 90) + n.level * LV;
  const g = (cls) => svgEl("g", cls ? { class: cls } : {});
  const num = (p) => String(p || "").replace(/^\D+/, "") || "?";
  const badge = (x0, y0, text, kind) => {
    const b = g("pbadge " + kind);
    b.appendChild(svgEl("rect", { x: x0 - 30, y: y0 - 14, width: 60, height: 28, rx: 7 }));
    b.appendChild(svgEl("rect", { x: x0 - 26, y: y0 - 10, width: 20, height: 20, rx: 5, class: "ico" }));
    b.appendChild(svgEl("text", { x: x0 - 16, y: y0 + 5, class: "icot" }, kind === "wan" ? "◎" : kind === "down" ? "↓" : "↑"));
    b.appendChild(svgEl("text", { x: x0 + 12, y: y0 + 5, class: "pnum" }, text));
    return b;
  };
  const curve = (x1, y1, x2, y2, cls) => svgEl("path", { d: `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`, class: "link " + cls });
  const links = g(), nodes = g();
  svg.append(links, nodes);
  // WAN nahoře
  const root = roots[0];
  d.wans.forEach((w, k) => {
    const host = byId[w.device_id] || root;
    const n = d.wans.length;
    const wx = cx(host) + (k - (n - 1) / 2) * 360, wy = TOP;
    const card = g("wan" + (w.active ? "" : " standby"));
    card.appendChild(svgEl("rect", { x: wx - 160, y: wy, width: 320, height: WH, rx: 10, class: "card" }));
    card.appendChild(svgEl("text", { x: wx - 144, y: wy + 23, class: "title" }, `WAN · ${w.interface || "?"}`));
    card.appendChild(svgEl("text", { x: wx - 144, y: wy + 43, class: "sub" },
      `${w.address || "bez adresy"}${w.gateway ? " → " + w.gateway : ""}${w.dhcp ? " (DHCP)" : ""}`));
    card.appendChild(svgEl("text", { x: wx + 144, y: wy + 23, class: "stat " + (w.active ? "ok" : "muted") }, w.active ? "aktivní" : "záloha"));
    if (w.distance) card.appendChild(svgEl("text", { x: wx + 144, y: wy + 43, class: "stat muted" }, `distance ${w.distance}`));
    nodes.appendChild(card);
    const px = cx(host) + (k - (n - 1) / 2) * 70, py = y(host) - 26;
    links.appendChild(curve(wx, wy + WH, px, py - 14, w.active ? "up" : "standby"));
    nodes.appendChild(badge(px, py, num(w.port), "wan"));
  });
  for (const n of all) {
    const x = cx(n), top = y(n);
    const ch = kids[n.id];
    ch.forEach((c, k) => {
      const bx = x + (k - (ch.length - 1) / 2) * 70, by = top + NH + 26;
      links.appendChild(curve(bx, by + 14, cx(c), c.ext ? y(c) : y(c) - 40, c.ext ? "ext" : c.state === "offline" ? "down" : "up"));
      nodes.appendChild(badge(bx, by, num(c.parent_port), "down"));
      if (!c.ext) nodes.appendChild(badge(cx(c), y(c) - 26, c.uplink_port ? num(c.uplink_port) : "?", "up"));
    });
    if (n.ext) {
      const pill = g("ext");
      pill.appendChild(svgEl("rect", { x: x - EW / 2, y: top, width: EW, height: EH, rx: 8 }));
      pill.appendChild(svgEl("text", { x, y: top + 17, class: "t1" }, shortName(n.label || "?", 18)));
      pill.appendChild(svgEl("text", { x, y: top + 32, class: "t2" }, n.platform ? shortName(n.platform, 20) : "nespravované"));
      nodes.appendChild(pill);
      continue;
    }
    const card = g("node" + (n.state === "offline" ? " offline" : ""));
    card.appendChild(svgEl("title", {}, [n.label, n.model, n.version, n.stp_root ? `STP root, priorita ${n.stp_priority ?? "?"}` : "",
                                         n.poe_w ? `PoE ${n.poe_w} W` : ""].filter(Boolean).join("\n")));
    card.appendChild(svgEl("rect", { x: x - NW / 2, y: top, width: NW, height: NH, rx: 10, class: "card" }));
    card.appendChild(svgEl("circle", { cx: x - NW / 2 + 16, cy: top + 22, r: 5, class: "dot " + (n.state || "") }));
    card.appendChild(svgEl("text", { x: x - NW / 2 + 28, y: top + 27, class: "title" }, shortName(n.label, 18)));
    card.appendChild(svgEl("text", { x: x - NW / 2 + 28, y: top + 48, class: "sub" }, shortName(n.model || n.role || "", 20)));
    card.appendChild(svgEl("text", { x: x + NW / 2 - 12, y: top + 27, class: "stat" }, n.cpu != null ? `${n.cpu} % CPU` : ""));
    card.appendChild(svgEl("text", { x: x + NW / 2 - 12, y: top + 48, class: "stat" }, n.mem != null ? `${n.mem} % RAM` : ""));
    if (n.stp_root) {
      const cr = g("crown");
      cr.appendChild(svgEl("rect", { x: x - NW / 2 - 50, y: top + 4, width: 44, height: NH - 8, rx: 8 }));
      cr.appendChild(svgEl("text", { x: x - NW / 2 - 28, y: top + 27, class: "c1" }, "♛"));
      cr.appendChild(svgEl("text", { x: x - NW / 2 - 28, y: top + 43, class: "c2" }, "STP"));
      cr.appendChild(svgEl("text", { x: x - NW / 2 - 28, y: top + 55, class: "c2" }, "ROOT"));
      card.appendChild(cr);
    }
    card.addEventListener("click", () => openDetail(n.id));
    nodes.appendChild(card);
  }
  if (!d.nodes.length) svg.appendChild(svgEl("text", { x: 20, y: 40, class: "sub" }, "V lokalitě zatím nejsou spravovaná zařízení s inventurou."));
}

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
  renderFirewall(d.firewall);
  phState.port = ""; loadPortHistory(id);
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
    if (mod === "ntp" || mod === "ports") {
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
  trafficState.iface = "*"; trafficState.lastPort = null; trafficData = null;
  $("port-edit").hidden = true;
  renderPorts(id, d);
  showTraffic(id, "*");
  const it = $("d-ifaces"); it.textContent = "";
  for (const i of d.interfaces) {
    const tr = el("tr");
    tr.className = "clickable";
    tr.title = "Graf provozu";
    tr.addEventListener("click", () => selectPortTraffic(i.name));
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
  const PLAN_ST = { draft: "návrh", queued: "ve frontě", confirming: "čeká na potvrzení", applied: "provedeno",
                    rolled_back: "vráceno zařízením", failed: "selhalo", superseded: "zastaralý", discarded: "zahozen" };
  const hist = d.plans.map((p) => [p.created_at, `plán ${p.module}: ${p.summary}`, PLAN_ST[p.status] || p.status])
    .concat(d.commands.filter((c) => c.kind !== "confirm_plan").map((c) => [c.created_at, `příkaz ${c.kind}`, c.status]))
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

// --- firewall jako zóny (UniFi zone-based firewall), zatím jen čtení ----------------------------

const FW_VIEWS = [["matrix", "Matice zón"], ["filter", "Pravidla filtru"], ["nat", "NAT"]];
let fwData = null;

function fmtCount(n) {
  if (n == null) return "";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " G";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + " k";
  return String(n);
}

function renderFirewall(fw) {
  fwData = fw;
  const box = $("d-fw"); box.textContent = "";
  const notes = $("d-fw-notes"); notes.textContent = "";
  $("d-fw-detail").hidden = true;
  if (!fw) {
    $("d-fw-views").textContent = "";
    box.appendChild(el("p", "Zatím bez inventury firewallu", "muted"));
    return;
  }
  const view = FW_VIEWS.some(([v]) => v === prefs.fw) ? prefs.fw : "matrix";
  segmented($("d-fw-views"), FW_VIEWS, view, (v) => { prefs.fw = v; savePrefs(); renderFirewall(fwData); });
  for (const n of fw.notes || []) notes.appendChild(el("li", "⚠ " + n, "sev-warning"));
  if (view === "matrix") fwMatrix(box, fw);
  else fwRules(box, view === "nat" ? fw.nat : fw.rules, view === "nat");
}

function fwCell(c, src, dst, fw) {
  const td = el("td", "", "fw-cell fw-" + c.decision);
  td.appendChild(el("span", c.decision === "allow" ? "Povoleno" : "Blokováno", "fw-dec"));
  if (c.exceptions.length) td.appendChild(el("small", ` +${c.exceptions.length} výj.`));
  td.title = c.note || (c.rule != null ? `pravidlo #${c.rule}` : "");
  td.addEventListener("click", () => fwShow(fw, src, dst, c));
  return td;
}

function fwMatrix(box, fw) {
  if (!fw.zones.length) { box.appendChild(el("p", "Žádné zóny (seznamy rozhraní)", "muted")); return; }
  const t = el("table", "", "fw-matrix");
  const head = el("tr"); head.appendChild(el("th", "z \\ do", "muted"));
  for (const z of fw.zones) head.appendChild(el("th", z));
  head.appendChild(el("th", "Router"));
  t.appendChild(head);
  for (const src of fw.zones) {
    const tr = el("tr");
    const th = el("th", src);
    th.title = (fw.members[src] || []).join(", ") || "bez členů";
    tr.appendChild(th);
    for (const dst of fw.zones) tr.appendChild(fwCell(fw.matrix[src][dst], src, dst, fw));
    tr.appendChild(fwCell(fw.router[src], src, null, fw));
    t.appendChild(tr);
  }
  box.appendChild(t);
  const rt = fw.return_traffic || {};
  box.appendChild(el("p", `Návratový provoz (established/related): forward ${rt.forward ? "povolen ✓" : "chybí"}, ` +
    `vstup do routeru ${rt.input ? "povolen ✓" : "chybí"}. Invalid: ${fw.drop_invalid && fw.drop_invalid.forward ? "zahazuje se ✓" : "nezahazuje se"}.`, "muted"));
  box.appendChild(el("p", "Úpravy pravidel budou následovat s automatickým návratem (commit-confirm). Klikněte na buňku pro pravidla.", "muted"));
}

function fwShow(fw, src, dst, c) {
  $("d-fw-detail").hidden = false;
  $("d-fw-detail-h").textContent = `${src} → ${dst || "Router"}: ${c.decision === "allow" ? "povoleno" : "blokováno"}` +
    (c.note ? ` (${c.note})` : "");
  const ol = $("d-fw-detail-list"); ol.textContent = "";
  const byIdx = {};
  for (const r of fw.rules) byIdx[r.index] = r;
  for (const i of c.exceptions) {
    const r = byIdx[i];
    if (r) ol.appendChild(el("li", `výjimka #${i}: ${r.action} – ${r.summary}${r.comment ? " (" + r.comment + ")" : ""}`));
  }
  const r = c.rule != null ? byIdx[c.rule] : null;
  ol.appendChild(el("li", r ? `rozhoduje #${r.index}: ${r.action} – ${r.summary}${r.comment ? " (" + r.comment + ")" : ""}`
    : "rozhoduje výchozí accept RouterOS (žádné pravidlo)", "fw-" + c.decision));
}

function fwRules(box, rows, nat) {
  if (!rows || !rows.length) { box.appendChild(el("p", "Žádná pravidla", "muted")); return; }
  const t = el("table", "", "fw-rules");
  const head = el("tr");
  for (const h of ["#", "Chain", "Akce", "Podmínky", "Paketů", "Komentář"]) head.appendChild(el("th", h));
  t.appendChild(head);
  for (const r of rows) {
    const tr = el("tr", "", r.disabled ? "fw-disabled" : "");
    tr.appendChild(el("td", String(r.index) + (r.dynamic ? " D" : "")));
    tr.appendChild(el("td", r.chain || ""));
    const act = String(r.action || "");
    const cls = /^(drop|reject|tarpit)$/.test(act) ? "block" : /^(accept|fasttrack-connection)$/.test(act) ? "allow" : "other";
    const td = el("td"); td.appendChild(el("span", act, "pill fw-" + cls)); tr.appendChild(td);
    tr.appendChild(el("td", r.summary + (r.invalid ? " (neplatné)" : "")));
    tr.appendChild(el("td", fmtCount(r.packets), "num"));
    tr.appendChild(el("td", r.comment || "", "muted"));
    t.appendChild(tr);
  }
  box.appendChild(t);
  if (!nat) box.appendChild(el("p", "D = dynamické pravidlo (vytvořené RouterOS), přeškrtnuté = vypnuté.", "muted"));
}

// --- historie portů (Port Time Machine) -------------------------------------------------------

const phState = { days: 7, port: "" };
const PH_CLS = { link: (e) => (e.new === "up" ? "ph-up" : "ph-down"), flaps: () => "ph-down", disabled: () => "ph-cfg",
                 neighbor: (e) => (e.new ? "ph-up" : "ph-down") };

function phItem(e, withPort) {
  const li = el("li", null, (PH_CLS[e.field] ? PH_CLS[e.field](e) : "ph-cfg"));
  const t = new Date(e.at);
  li.appendChild(el("span", t.toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit" }), "ph-time"));
  if (withPort) li.appendChild(el("b", e.port + " "));
  li.appendChild(el("span", e.text));
  li.title = t.toLocaleString("cs-CZ");
  return li;
}

async function loadPortHistory(id) {
  const q = `days=${phState.days}` + (phState.port ? `&port=${encodeURIComponent(phState.port)}` : "");
  let h;
  try { h = await api("GET", `/tenants/${tenantId}/devices/${id}/port-history?${q}`); } catch (e) { return; }
  if (detailId !== id) return;
  segmented($("d-ph-range"), [[1, "24 h"], [7, "7 dní"], [30, "30 dní"], [90, "90 dní"]], phState.days,
            (v) => { phState.days = v; loadPortHistory(id); });
  const sel = $("d-ph-port");
  if (!phState.port) {
    sel.textContent = "";
    const o0 = el("option", "všechny porty"); o0.value = ""; sel.appendChild(o0);
    const ports = [...new Set(h.events.map((e) => e.port))].sort((a, b) => portNum(a) - portNum(b));
    for (const p of ports) { const o = el("option", p); o.value = p; sel.appendChild(o); }
    sel.onchange = () => { phState.port = sel.value; loadPortHistory(id); };
  }
  sel.value = phState.port;
  const n = h.events.length;
  $("d-ph-n").textContent = n ? `(${n} ${n === 1 ? "změna" : n < 5 ? "změny" : "změn"})` : "";
  const box = $("d-ph"); box.textContent = "";
  if (!h.events.length) { box.appendChild(el("p", "Za zvolené období beze změn (historie se plní s každou inventurou).", "muted")); return; }
  let day = null, ul = null;
  for (const e of h.events) {
    const dd = new Date(e.at).toLocaleDateString("cs-CZ", { weekday: "short", day: "numeric", month: "numeric" });
    if (dd !== day) { day = dd; box.appendChild(el("h4", dd, "ph-day")); ul = el("ul", null, "ph-list"); box.appendChild(ul); }
    ul.appendChild(phItem(e, !phState.port));
  }
}
