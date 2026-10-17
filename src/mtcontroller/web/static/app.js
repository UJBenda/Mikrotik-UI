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
  await Promise.all([loadSites(), loadDevices(), loadAudit(), loadPlans()]);
  await loadSitePanel();
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

// --- lokalita: NTP politika, plány, topologie ------------------------------------------

let devicesCache = [];

function siteId() { return $("site-sel").value; }

async function loadSitePanel() {
  const sel = $("site-sel");
  const prev = sel.value;
  sel.textContent = "";
  for (const s of sites) { const o = el("option", s.name); o.value = s.id; sel.appendChild(o); }
  if (prev && sites.some((s) => s.id === prev)) sel.value = prev;
  $("topo-wrap").hidden = true;
  $("ntp-result").textContent = "";
  if (!sites.length) return;
  const prim = $("ntp-primary");
  prim.textContent = "";
  for (const d of devicesCache.filter((x) => x.site_id === siteId() && x.state !== "pending")) {
    const o = el("option", `${d.identity || d.serial} (${d.model || "?"})`); o.value = d.id; prim.appendChild(o);
  }
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
  kv($("d-summary"), [
    ["Sériové číslo", dev.serial], ["RouterOS", dev.ros_version], ["Kanál / dostupná", sm.update_channel ? `${sm.update_channel} / ${sm.latest_version || "?"}` : null],
    ["RouterBOOT", sm.firmware ? `${sm.firmware}${sm.firmware_upgrade && sm.firmware_upgrade !== sm.firmware ? " → " + sm.firmware_upgrade : ""}` : null],
    ["Uptime", sm.uptime], ["CPU", sm.cpu_load != null ? `${sm.cpu_load} % (${sm.cpu || ""})` : null],
    ["Paměť", sm.memory_used_pct != null ? `${sm.memory_used_pct} %` : null], ["Úložiště", sm.hdd_used_pct != null ? `${sm.hdd_used_pct} %` : null],
    ["Lokalita", dev.site], ["Tunel", `${dev.tunnel_ip || "–"} přes ${dev.concentrator || "–"}`],
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
  const it = $("d-ifaces"); it.textContent = "";
  for (const i of d.interfaces) {
    const tr = el("tr");
    tr.append(el("td", i.name), el("td", i.type), el("td", i.disabled ? "vypnuté" : i.running ? "běží" : "neběží", i.running ? "" : "sev-warning"),
              el("td", i.mac || ""), el("td", i.link_downs || "0"), el("td", `${i.rx_error || 0} / ${i.tx_error || 0}`), el("td", i.comment || ""));
    it.appendChild(tr);
  }
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
setInterval(() => { if (!$("app").hidden && tenantId) { loadDevices(); } }, 15000);

boot();
