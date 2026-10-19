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

// potvrzení akce vlastním dialogem ve stránce: systémový confirm() prohlížeč umí potlačit
// (Firefox „nepovolit další dialogy“) a pak tlačítka tiše nic nedělají
let askDone = null;
function ask(text, okLabel = "Potvrdit") {
  if (askDone) askDone(false);
  return new Promise((resolve) => {
    const box = $("ask");
    $("ask-text").textContent = text;
    $("ask-ok").textContent = okLabel;
    const done = (v) => { box.hidden = true; askDone = null; resolve(v); };
    askDone = done;
    $("ask-ok").onclick = () => done(true);
    $("ask-cancel").onclick = () => done(false);
    box.hidden = false;
    try { $("ask-ok").focus(); } catch (_e) { /* fokus není podstatný */ }
  });
}
if (typeof document.addEventListener === "function") {
  document.addEventListener("keydown", (e) => { if (askDone && e.key === "Escape") askDone(false); });
}
// dotaz na krátký text (jméno skupiny…) ve stejném dialogu jako ask()
async function askText(text, value = "", okLabel = "Uložit", maxLength = 40) {
  const inp = $("ask-input");
  inp.hidden = false; inp.value = value; inp.maxLength = maxLength;
  const p = ask(text, okLabel);
  try { inp.focus(); if (inp.select) inp.select(); } catch (_e) { /* fokus není podstatný */ }
  inp.onkeydown = (e) => { if (e.key === "Enter") $("ask-ok").onclick(); };
  const ok = await p;
  inp.hidden = true;
  return ok ? inp.value.trim() : null;
}

// --- kontextové menu (pravé tlačítko, klávesa Menu / Shift+F10, tlačítko ⋯) -------------------------
// položka: {label, act, danger, disabled, hint, checked} | {sep: true} | {head: "text"}; falsy položky se vynechají
function ctxMenu(ev, title, items) {
  if (ev && ev.preventDefault) ev.preventDefault();
  if (ev && ev.stopPropagation) ev.stopPropagation();
  const m = $("ctx"); m.textContent = "";
  if (title) m.appendChild(el("div", title, "ctx-title"));
  const buttons = [];
  for (const it of items) {
    if (!it) continue;
    if (it.sep) { m.appendChild(el("div", null, "ctx-sep")); continue; }
    if (it.head) { m.appendChild(el("div", it.head, "ctx-head")); continue; }
    const b = el("button", null, "ctx-item" + (it.danger ? " danger" : "") + (it.checked ? " checked" : ""));
    b.type = "button"; b.setAttribute("role", "menuitem");
    b.append(el("span", it.checked ? "✓" : "", "ctx-mark"), el("span", it.label));
    if (it.key) b.appendChild(el("span", it.key, "ctx-key"));
    b.disabled = !!it.disabled;
    if (it.hint) b.title = it.hint;
    b.addEventListener("click", () => { ctxHide(); it.act(); });
    m.appendChild(b); buttons.push(b);
  }
  if (!buttons.length) return;
  m.hidden = false; ctxOpenedAt = Date.now();
  // pozice u kurzoru, ale vždy celé v okně
  const W = window.innerWidth || 1e4, H = window.innerHeight || 1e4;
  const w = m.offsetWidth || 220, h = m.offsetHeight || 200;
  let x = ev && ev.clientX != null ? ev.clientX : 0, y = ev && ev.clientY != null ? ev.clientY : 0;
  if ((!x && !y) && ev && ev.currentTarget && ev.currentTarget.getBoundingClientRect) {
    const r = ev.currentTarget.getBoundingClientRect(); x = r.left + 12; y = r.bottom - 4;  // z klávesnice
  }
  m.style.left = Math.max(4, Math.min(x, W - w - 4)) + "px";
  m.style.top = Math.max(4, Math.min(y, H - h - 4)) + "px";
  try { buttons.find((b) => !b.disabled).focus(); } catch (_e) { /* fokus není podstatný */ }
}
let ctxOpenedAt = 0;
function ctxHide() { const m = $("ctx"); if (m && !m.hidden) m.hidden = true; }
// posun stránky menu zavře – ale ne posun, který vyvolalo samo otevření (fokus řádku)
function ctxHideOnScroll() { if (Date.now() - ctxOpenedAt > 250) ctxHide(); }
// prvek s menu: pravé tlačítko i klávesnice (prvek musí jít zaměřit)
function withCtx(node, title, items) {
  node.addEventListener("contextmenu", (ev) => ctxMenu(ev, typeof title === "function" ? title() : title, items()));
  if (node.tabIndex == null || node.tabIndex < 0) node.tabIndex = 0;
  return node;
}
function moreButton(title, items) {
  const b = el("button", "⋯", "icon-btn more"); b.type = "button"; b.title = "Akce (nebo pravé tlačítko)";
  b.setAttribute("aria-haspopup", "menu");
  b.addEventListener("click", (ev) => ctxMenu(ev, typeof title === "function" ? title() : title, items()));
  return b;
}
if (typeof document.addEventListener === "function") {
  document.addEventListener("click", (e) => { const m = $("ctx"); if (m && !m.hidden && !m.contains(e.target)) ctxHide(); });
  document.addEventListener("keydown", (e) => {
    const m = $("ctx");
    if (!m || m.hidden) return;
    if (e.key === "Escape") { ctxHide(); e.preventDefault(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      const bs = [...m.querySelectorAll("button.ctx-item:not(:disabled)")];
      const i = bs.indexOf(document.activeElement);
      const n = bs[(i + (e.key === "ArrowDown" ? 1 : bs.length - 1)) % bs.length];
      if (n) n.focus();
      e.preventDefault();
    }
  });
  window.addEventListener("resize", ctxHide);
  document.addEventListener("scroll", ctxHideOnScroll, true);
}
function copyText(t, what) { navigator.clipboard.writeText(String(t)).then(() => msg(`${what} zkopírováno`)); }

// chyba ve skriptu se ukáže v UI, ne jen v konzoli (jinak tlačítko „nic nedělá“)
if (typeof window.addEventListener === "function") {
  window.addEventListener("error", (e) => msg("Chyba v UI: " + (e.message || e), true));
  window.addEventListener("unhandledrejection", (e) => msg("Chyba v UI: " + ((e.reason && e.reason.message) || e.reason), true));
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
  $("nav").hidden = !(which === "app" || which === "detail");
  // v detailu zůstává navigace: klik na sekci se vrátí do aplikace
  for (const [k] of VIEWS) { const b = $("nav-" + k); if (b) b.className = "navbtn" + (which === "app" && k === prefs.view ? " active" : ""); }
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
  pw.onclick = () => { showView("users"); $("pw-card").hidden = false; };
  who.append(pw, " ");
  who.appendChild(out);
  $("hdr-tenant").hidden = false;
  $("tenant-form").hidden = !me.superadmin;
  buildNav();
  showView(prefs.view || "overview");
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

// --- přehled: dlaždice s proklikem na seznam zařízení a problémů (s akcemi k řešení) ---
let ovDrill = null;          // klíč otevřené dlaždice
let securityCache = null;    // nálezy všech zařízení (načtou se při otevření dlaždice)
const OV_TILES = [
  // klíč, popis, výběr zařízení / problémů, třída podle počtu
  ["online", "Online", () => devicesCache.filter((d) => d.state === "adopted"), ""],
  ["offline", "Offline", () => devicesCache.filter((d) => d.state === "offline"), "bad"],
  ["pending", "Čeká na schválení", () => devicesCache.filter((d) => d.state === "pending"), "warn"],
  ["provisioning", "Provisioning", () => devicesCache.filter((d) => d.state === "provisioning"), ""],
  ["critical", "Kritické alerty", () => alertsCache.filter((a) => a.severity === "critical"), "bad"],
  ["warning", "Varování", () => alertsCache.filter((a) => a.severity === "warning"), "warn"],
  ["updates", "Dostupná aktualizace", () => devicesCache.filter((d) => d.update_available), ""],
  ["ros", "Známé problémy verzí", () => devicesCache.filter((d) => (d.warnings || []).some((w) => w.severity === "critical")), "bad"],
  ["security", "Bezpečnostní nálezy", null, "warn"],
];

function renderOverview() {
  const box = $("overview"); box.textContent = "";
  for (const [key, label, pick, cls] of OV_TILES) {
    const n = pick ? pick().length : devicesCache.reduce((s, d) => s + (Number(d.security_issues) || 0), 0);
    if (key === "provisioning" && !n) continue;
    const t = el("button", null, "tile" + (n && cls ? " " + cls : "") + (ovDrill === key ? " open" : ""));
    t.type = "button"; t.title = n ? "Klikni pro seznam" : "";
    t.setAttribute("aria-expanded", ovDrill === key ? "true" : "false");
    t.append(el("b", String(n)), el("span", label));
    t.addEventListener("click", () => { ovDrill = ovDrill === key ? null : key; renderOverview(); });
    box.appendChild(t);
  }
  renderDrill();
}

const devName = (d) => d.identity || d.serial;
function devOf(id) { return devicesCache.find((d) => d.id === id) || { id }; }

// tlačítko akce v řádku problému
function actBtn(label, fn, cls = "secondary") {
  const b = el("button", label, "small " + cls); b.type = "button";
  b.addEventListener("click", async (ev) => { if (ev.stopPropagation) ev.stopPropagation(); b.disabled = true; try { await fn(); } finally { b.disabled = false; } });
  return b;
}

// skupina „zařízení + jeho problémy“
function drillDevice(d, lines) {
  const g = el("div", null, "dr-dev");
  const h = el("div", null, "dr-head");
  h.append(el("span", "", "dr-dot " + (d.state || "")), el("b", devName(d)));
  if (d.identity && d.serial) h.appendChild(el("span", d.serial, "muted small mono"));
  if (d.site) h.appendChild(el("span", d.site, "wpill"));
  if (d.state !== "pending") {
    const open = actBtn("Detail", () => openDetail(d.id), "secondary"); h.appendChild(open);
    if (d.serial) h.appendChild(moreButton(devName(d), () => deviceMenu(devOf(d.id).serial ? devOf(d.id) : d)));
    withCtx(g, devName(d), () => deviceMenu(devOf(d.id).serial ? devOf(d.id) : d));
  }
  g.appendChild(h);
  for (const ln of lines) g.appendChild(ln);
  return g;
}
// řádek problému: závažnost, text, rada, akce
function drillLine(sev, text, hint, actions) {
  const r = el("div", null, "dr-line");
  r.appendChild(el("span", { critical: "kritické", warning: "varování", info: "info" }[sev] || "", "dr-sev " + (sev || "none")));
  const t = el("div", null, "dr-text"); t.appendChild(el("span", text));
  if (hint) t.appendChild(el("div", hint, "dr-hint"));
  r.appendChild(t);
  const a = el("div", null, "dr-acts"); for (const b of actions || []) if (b) a.appendChild(b);
  r.appendChild(a);
  return r;
}

// co dělat u alertu podle druhu
function alertReactions(a) {
  const d = devOf(a.device_id);
  const cmd = (kind, label) => actBtn(label, () => sendCommand(d, kind, label), "small");
  const ack = a.acknowledged_at ? null : actBtn("Potvrdit", async () => {
    try { await api("POST", `/tenants/${tenantId}/alerts/${a.id}/ack`); await loadAlerts(); renderOverview(); } catch (e) { msg(e.message, true); }
  });
  const R = {
    offline: ["Zkontroluj napájení a připojení k internetu – zařízení se po obnovení ozve samo.", []],
    tunnel_down: ["Tunel ke controlleru nejede; zařízení se přepne na záložní koncentrátor samo.", []],
    backup_stale: ["Záloha je stará – vyžádej novou.", [actBtn("Zálohovat teď", async () => {
      try { await api("POST", `/tenants/${tenantId}/devices/${a.device_id}/backup`, {}); msg("Záloha vyžádána"); } catch (e) { msg(e.message, true); } }, "small")]],
    wifi_package: ["Rádio potřebuje jiný Wi-Fi balíček.", [cmd("wifi_package", "Vyměnit Wi-Fi balíček")]],
    package_version: ["Balíčky mají různé verze – RouterOS je po restartu vypne.", [d.update_available ? actBtn(`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]],
    ros_issue: ["Verze RouterOS má známý problém.", [d.update_available ? actBtn(`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]],
    upgrade_failed: ["Aktualizace se nepovedla – zkus ji znovu, případně ručně přes Winbox.", [d.update_available ? actBtn("Zkusit znovu", () => upgradeDevice(d), "small") : null]],
    security_exposed: ["Router je otevřený z internetu.", [actBtn("Bezpečnostní nálezy", () => { ovDrill = "security"; renderOverview(); }, "small")]],
    rest_unreachable: ["Controller se k zařízení nedostane přes REST – obnov provisioning.", [cmd("provision", "Znovu provisioning")]],
    rest_pin_mismatch: ["Certifikát REST se změnil – obnov provisioning (nový otisk).", [cmd("provision", "Znovu provisioning")]],
    cert_changed: ["Certifikát zařízení se změnil – pokud jsi ho neměnil, prověř to.", [cmd("provision", "Znovu provisioning")]],
    change_unconfirmed: ["Změna čeká na potvrzení.", [actBtn("Plány změn", () => showView("plans"), "small")]],
    change_rolled_back: ["Zařízení změnu samo vrátilo (nepotvrdila se).", [actBtn("Plány změn", () => showView("plans"), "small")]],
    rogue_dhcp: ["Na síti odpovídá cizí DHCP server – Lokalita → Sítě a DHCP.", []],
    port_loop: ["Smyčka na portu – v detailu port vypni nebo zapni STP.", []],
    port_flapping: ["Port často padá – zkontroluj kabel a protějšek.", []],
    port_errors: ["Chyby na portu – kabel, konektor nebo duplex.", []],
    port_half_duplex: ["Port jede v half-duplexu – zkontroluj autonegociaci.", []],
    cpu_high: ["Vysoké vytížení CPU.", []], memory_high: ["Málo volné paměti.", []],
    ntp_unsynced: ["Čas není synchronizovaný – Lokalita → NTP.", []],
    dhcp_pool_full: ["DHCP pool je skoro plný – zvětši rozsah nebo zkrať lease.", []],
    config_drift: ["Konfigurace se liší od záměru – Lokalita → Sítě → Sladit řízená zařízení.", []],
  }[a.kind] || ["", []];
  return [R[0], [...R[1], ack]];
}

async function renderDrill() {
  const box = $("ov-drill");
  if (!ovDrill) { box.hidden = true; return; }
  const tile = OV_TILES.find((t) => t[0] === ovDrill);
  box.hidden = false; box.textContent = "";
  const head = el("div", null, "dr-title");
  head.appendChild(el("h2", tile[1]));
  const close = el("button", "×", "icon-btn"); close.type = "button"; close.title = "Zavřít";
  close.addEventListener("click", () => { ovDrill = null; renderOverview(); });
  head.appendChild(close);
  box.appendChild(head);
  const list = el("div", null, "dr-list");
  box.appendChild(list);
  const empty = (t) => list.appendChild(el("p", t, "muted"));
  const key = ovDrill;
  if (["online", "offline", "pending", "provisioning"].includes(key)) {
    const devs = tile[2]();
    for (const d of devs) {
      let line;
      if (key === "pending") line = drillLine(null, `${d.model || ""} · ${d.ros_version || ""}`, "Schval a rovnou zadej jméno, lokalitu a roli:", [approveControls(d), forgetButton(d)]);
      else if (key === "offline") line = drillLine("warning", `Naposledy ${fmtTime(d.last_checkin_at)} · tunel ${d.tunnel_ip || "–"}`,
        "Zkontroluj napájení a internet. Pokud zařízení už neexistuje, zapomeň ho.", [forgetButton(d)]);
      else if (key === "provisioning") line = drillLine(null, "Čeká na první check-in po schválení", "Zařízení si nastavení vyzvedne do minuty.", []);
      else line = drillLine(null, `${d.model || ""} · RouterOS ${d.ros_version || "?"} · check-in ${fmtTime(d.last_checkin_at)}`, d.note || "", []);
      list.appendChild(drillDevice(d, [line]));
    }
    if (!devs.length) empty("Žádné zařízení.");
  } else if (key === "critical" || key === "warning") {
    const by = {};
    for (const a of tile[2]()) (by[a.device_id] = by[a.device_id] || []).push(a);
    for (const [id, as] of Object.entries(by)) {
      const d = { ...devOf(id), identity: as[0].identity, serial: as[0].serial, site: as[0].site };
      list.appendChild(drillDevice(d, as.map((a) => { const [hint, acts] = alertReactions(a); return drillLine(a.severity, a.message, hint, acts); })));
    }
    if (!Object.keys(by).length) empty("Žádné otevřené alerty. 🎉");
  } else if (key === "updates") {
    const devs = tile[2]();
    for (const d of devs) {
      const crit = (d.warnings || []).filter((w) => w.severity === "critical");
      list.appendChild(drillDevice(d, [drillLine(crit.length ? "critical" : "info", `RouterOS ${d.ros_version} → ${d.latest_version} (kanál ${d.update_channel || "?"})`,
        crit.length ? crit.map((w) => w.text).join(" · ") : "Před aktualizací se udělá záloha; zařízení se restartuje.",
        [actBtn(`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small")])]));
    }
    if (devs.length > 1) list.appendChild(el("p", "Víc zařízení na lokalitě? Lokalita → Aktualizace postupuje od konce sítě k bráně.", "muted small"));
    if (!devs.length) empty("Všechna zařízení jsou aktuální.");
  } else if (key === "ros") {
    const devs = tile[2]();
    for (const d of devs) {
      list.appendChild(drillDevice(d, (d.warnings || []).map((w) => drillLine(w.severity, w.text, w.source ? `zdroj: ${w.source}` : "",
        [d.update_available ? actBtn(`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]))));
    }
    if (!devs.length) empty("Žádná zařízení se známým problémem verze.");
  } else if (key === "security") {
    list.appendChild(el("p", "Načítám nálezy…", "muted"));
    try { securityCache = await api("GET", `/tenants/${tenantId}/security`); } catch (e) { list.textContent = ""; return empty(e.message); }
    if (ovDrill !== key) return;
    list.textContent = "";
    for (const d of securityCache) {
      const fix = d.findings.filter((f) => f.fixable).map((f) => f.code);
      const lines = d.findings.map((f) => drillLine(f.severity, f.text, f.fix ? "Oprava: " + f.fix : "", [
        f.fixable ? actBtn(f.fix_label, () => hardeningFix(d, [f.code]), "small") : null,
        f.fix && !f.fixable ? actBtn("Kopírovat příkaz", () => copyText(f.fix, "Příkaz"), "small secondary") : null,
      ]));
      if (fix.length > 1) lines.push(drillLine(null, `Opravit vše automaticky (${fix.length})`, "Jeden plán s automatickým návratem, když se zařízení neozve.",
        [actBtn("Opravit vše", () => hardeningFix(d, fix), "small")]));
      if (d.plan_open) lines.unshift(drillLine("info", "Oprava už čeká v Plánech změn", "", [actBtn("Plány změn", () => showView("plans"), "small")]));
      list.appendChild(drillDevice(d, lines));
    }
    if (!securityCache.length) empty("Bez nálezů ✓");
  }
}

// oprava nálezu: plán s návratem → schválit hned (nebo čeká na druhého schvalovatele)
async function hardeningFix(d, codes) {
  if (!(await ask(`${devName(d)}: opravit ${codes.length === 1 ? "nález" : codes.length + " nálezy"}?\n\nVznikne plán změny s automatickým návratem: když se zařízení po změně do 5 minut neozve, vrátí se samo.`, "Opravit"))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/hardening-fix`, { codes });
    try {
      await api("POST", `/tenants/${tenantId}/plans/${r.plan_id}/approve`);
      msg(`${devName(d)}: ${r.labels.join(", ")} – posílám na zařízení`); watchDevice(d.id, devName(d));
    } catch (e) { msg(`Plán #${r.plan_id} vytvořen, čeká na schválení: ${e.message}`); }
    loadPlans(); renderDrill();
  } catch (e) { msg(e.message, true); }
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
  setBadge("overview", list.filter((d) => d.state === "pending").length, "warn");
  for (const d of list) {
    const tr = el("tr");
    if (d.state !== "pending") {
      tr.className = "clickable";
      tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "BUTTON" && ev.target.tagName !== "SELECT") openDetail(d.id); });
      tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target === tr) openDetail(d.id); });
    }
    withCtx(tr, d.identity || d.serial, () => deviceMenu(d));
    tr.appendChild(el("td", d.state, "state " + d.state));
    // identita z RouterOS a pod ní sériové číslo (identita může chybět u čekajících)
    const who = el("td", null, "dev-who");
    who.appendChild(el("b", d.identity || d.serial));
    if (d.identity_pending && d.identity_pending !== d.identity) {
      const pend = el("span", ` → ${d.identity_pending}`, "id-pending"); pend.title = "nové jméno čeká na potvrzení zařízením";
      who.appendChild(pend);
    }
    if (d.identity) who.appendChild(el("div", d.serial, "muted small mono"));
    if (d.state !== "pending" && d.state !== "removed") {
      who.title = "Dvojklik = přejmenovat";
      who.addEventListener("dblclick", (ev) => { ev.stopPropagation && ev.stopPropagation(); renameDevice(d); });
    }
    tr.appendChild(who);
    const nt = el("td", d.note || "", "dev-note" + (d.note ? "" : " muted"));
    if (d.note) nt.title = d.note;
    if (d.state !== "pending" && d.state !== "removed") {
      nt.title = (d.note ? d.note + "\n\n" : "") + "Dvojklik = upravit poznámku";
      nt.addEventListener("dblclick", (ev) => { ev.stopPropagation && ev.stopPropagation(); editNote(d); });
    }
    tr.appendChild(nt);
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
    if (d.state === "pending") act.append(approveControls(d), " ");
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
    if (d.state !== "removed") act.appendChild(forgetButton(d));
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  if (!list.length) {
    const tr = el("tr"); const td = el("td", "Žádná zařízení", "muted"); td.colSpan = 10;
    tr.appendChild(td); tb.appendChild(tr);
  }
  if (ovDrill) renderOverview();  // otevřený proklik z přehledu ukazuje aktuální stav
}

// odebrání jen v controlleru – když zařízení „Odebrat“ neprovede (nedostupné, zaseknuté)
function forgetButton(d) {
  const b = el("button", d.state === "pending" ? "Zahodit" : "Zapomenout", "small danger");
  b.title = "Odebrat jen z controlleru, bez úklidu na zařízení";
  b.onclick = () => forgetDevice(d);
  return b;
}

// akce zařízení pro pravé tlačítko na řádku (stejné jako tlačítka v řádku)
function deviceMenu(d) {
  if (d.state === "pending") return [{ label: "Zahodit", danger: true, act: () => forgetDevice(d) }];
  const live = d.state !== "removed";
  return [
    { label: "Otevřít detail", key: "Enter", act: () => openDetail(d.id) },
    d.update_available && d.state === "adopted" ? { label: `Aktualizovat → ${d.latest_version}`, act: () => upgradeDevice(d) } : null,
    { sep: true },
    { label: "Přejmenovat…", disabled: !canOperate() || d.state === "removed", act: () => renameDevice(d) },
    { label: d.note ? "Upravit poznámku…" : "Přidat poznámku…", disabled: !canOperate(), act: () => editNote(d) },
    { sep: true },
    { label: "Kopírovat sériové číslo", act: () => copyText(d.serial, "Sériové číslo") },
    d.tunnel_ip ? { label: "Kopírovat IP v tunelu", act: () => copyText(d.tunnel_ip, "IP") } : null,
    { sep: true },
    { label: "Znovu provisioning", disabled: !live, act: () => sendCommand(d, "provision", "Znovu provisioning") },
    { label: "Restart", disabled: !live, act: () => sendCommand(d, "reboot", "Restart") },
    { sep: true },
    { label: "Odebrat", danger: true, disabled: !live, act: () => sendCommand(d, "deprovision", "Odebrat") },
    live ? { label: "Zapomenout", danger: true, act: () => forgetDevice(d) } : null,
  ];
}

// přejmenování v RouterOS (/system identity); v controlleru se jméno změní po potvrzení zařízením
async function renameDevice(d, name) {
  if (name === undefined) {
    name = await askText(`Nové jméno pro ${d.identity || d.serial} (pošle se do RouterOS):`, d.identity_pending || d.identity || "", "Přejmenovat", 63);
    if (name === null) return false;
  }
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/identity`, { name });
    if (r.unchanged) { msg("Jméno je stejné"); return true; }
    msg(`Přejmenování na ${r.name} posláno` + ((r.aps || []).length ? ` – ${r.aps.length} AP dostane nové jméno CAPsMANu` : ""));
    loadDevices(); watchDevice(d.id, r.name);
    return true;
  } catch (e) { msg(e.message, true); return false; }
}

async function editNote(d) {
  const note = await askText(`Poznámka k ${d.identity || d.serial} (umístění, kontakt…):`, d.note || "", "Uložit", 500);
  if (note === null) return;
  try { await api("PATCH", `/tenants/${tenantId}/devices/${d.id}`, { note }); msg("Poznámka uložena"); loadDevices(); }
  catch (e) { msg(e.message, true); }
}

async function forgetDevice(d) {
  const name = d.identity || d.serial;
  const text = d.state === "pending"
    ? `Zahodit čekající zařízení ${name}? Lze ho kdykoli adoptovat znovu.`
    : `Zapomenout zařízení ${name}?\n\nOdebere ho jen z controlleru – zařízení nic nedostane. Použij, když „Odebrat“ nejde, ` +
      "protože zařízení neodpovídá. Klíče se zneplatní, fronta příkazů a plány se zruší, účet na koncentrátoru se vypne. " +
      "Zařízení pak jde znovu adoptovat (adopce stávající objekty mtc přepíše).";
  if (!(await ask(text, d.state === "pending" ? "Zahodit" : "Zapomenout"))) return;
  try { await api("POST", `/tenants/${tenantId}/devices/${d.id}/forget`, {}); msg(`${name}: odebráno z controlleru`); loadDevices(); }
  catch (e) { msg(e.message, true); }
}

function approveControls(d) {
  const wrap = el("span");
  const site = el("select");
  for (const s of sites) { const o = el("option", s.name); o.value = s.id; site.appendChild(o); }
  if (d.site_id) site.value = d.site_id;
  const role = el("select");
  for (const r of ["unknown", "gateway", "switch", "ap", "ptp"]) { const o = el("option", r); o.value = r; role.appendChild(o); }
  // jméno zařízení: pošle se hned po provisioningu (prázdné = ponechat to z RouterOS)
  const name = el("input", null, "id-input"); name.maxLength = 63; name.size = 14;
  name.placeholder = d.identity ? `jméno (${d.identity})` : "jméno, např. SW-sklad";
  name.title = "Jméno zařízení (/system identity) – nastaví se po schválení";
  const b = el("button", "Schválit", "small");
  b.onclick = async () => {
    const nm = name.value.trim();
    if (!(await ask(`Schválit zařízení ${d.serial} (${d.model})${nm ? ` jako ${nm}` : ""}?`))) return;
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/approve`, { site_id: site.value, role: role.value, identity: nm || null });
      if (r.concentrator_errors.length) msg("Schváleno, ale koncentrátor: " + r.concentrator_errors.join("; "), true);
      else msg("Schváleno – provisioning proběhne při dalším check-inu");
      loadDevices();
    } catch (e) { msg(e.message, true); }
  };
  wrap.append(name, " ", site, " ", role, " ", b);
  return wrap;
}

async function sendCommand(d, kind, label, extra) {
  if (!(await ask(`${label}: ${d.serial}?`))) return;
  try {
    await api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, Object.assign({ kind }, extra || {}));
    msg(`${label}: posílám na zařízení`);
    watchDevice(d.id, d.identity || d.serial);
  } catch (e) { msg(e.message, true); }
}

async function upgradeDevice(d) {
  const warn = (d.warnings || []).filter((x) => x.severity !== "info").map((x) => "• " + x.text);
  const text = `Aktualizovat RouterOS na ${d.serial} z ${d.ros_version} na ${d.latest_version}?\n` +
    "Zařízení si verzi stáhne a restartuje se (výpadek ~2–5 min). Předtím se doporučuje záloha." +
    (warn.length ? "\n\nZnámé problémy současné verze:\n" + warn.join("\n") : "");
  if (!(await ask(text))) return;
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
  for (const [, w] of SITE_TABS) $(w).hidden = true;
  $("ntp-result").textContent = "";
  if (!sites.length) return;
  openSiteTab(SITE_TABS.some(([k]) => k === prefs.siteTab) ? prefs.siteTab : "infra");
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

const PLAN_MODULE = { ntp: "NTP", ports: "port", dhcp: "DHCP", takeover: "převzetí do řízení", switch: "sladění se záměrem", dhcp_snooping: "DHCP snooping",
  capsman: "Wi-Fi (CAPsMAN)" };

async function loadPlans() {
  const list = await api("GET", `/tenants/${tenantId}/plans?status=draft`);
  const box = $("plans");
  box.textContent = "";
  setBadge("plans", list.length, "warn");
  if (!list.length) { box.appendChild(el("p", "Žádné plány nečekají na schválení.", "muted")); return; }
  for (const p of list) {
    const div = el("div", null, "plan");
    div.appendChild(el("strong", `${p.identity || p.serial} (${p.site || "–"}) – ${(PLAN_MODULE[p.module] || p.module).toUpperCase()}`));
    div.appendChild(el("div", p.summary));
    div.appendChild(el("pre", p.script));
    const ok = el("button", "Schválit a aplikovat", "small");
    ok.onclick = async () => {
      if (!(await ask("Aplikovat plán na zařízení? Controller ho pošle hned (když zařízení přes tunel neodpoví, nejpozději při dalším check-inu do minuty)."))) return;
      try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/approve`); msg("Plán schválen – posílám na zařízení"); loadPlans(); watchDevice(p.device_id, p.identity || p.serial); }
      catch (e) { msg(e.message, true); }
    };
    const no = el("button", "Zahodit", "small secondary");
    no.onclick = async () => { try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/discard`); loadPlans(); } catch (e) { msg(e.message, true); } };
    div.append(ok, " ", no);
    withCtx(div, `Plán #${p.id} · ${p.identity || p.serial}`, () => [
      { label: "Schválit a aplikovat", act: () => ok.onclick() },
      { label: "Zahodit", danger: true, act: () => no.onclick() },
      { sep: true },
      { label: "Otevřít zařízení", act: () => openDetail(p.device_id) },
      { label: "Kopírovat skript", act: () => copyText(p.script, "Skript") },
    ]);
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

async function loadTopology() {
  try {
    const t = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/topology`);
    drawTopology(t);
    $("topo-wrap").hidden = false;
  } catch (e) { msg(e.message, true); }
}
$("topo-btn").addEventListener("click", loadTopology);

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
  setBadge("alerts", list.length, crit ? "bad" : "warn");
  for (const a of list) {
    const tr = el("tr");
    tr.append(el("td", SEV_LABEL[a.severity] || a.severity, "sev-" + (a.severity === "critical" ? "error" : a.severity)),
              el("td", a.identity || a.serial), el("td", a.site || "–"), el("td", a.message, "wrap"), el("td", fmtTime(a.opened_at)));
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
  if (ovDrill) renderOverview();
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
  const list = clientsCache.filter((c) => !q || [c.hostname, c.ip, c.mac, c.device, c.port, c.vlan, c.wifi && c.wifi.ssid, c.ssid,
    c.vendor, c.note]
    .some((v) => v && String(v).toLowerCase().includes(q)));
  for (const c of list.slice(0, 1000)) {
    const tr = el("tr");
    if (c.online === false) tr.className = "muted";
    const ssid = c.wifi ? c.wifi.ssid : c.ssid, signal = c.wifi ? c.wifi.signal : c.signal;
    const wifi = ssid || signal ? `${ssid || ""} ${signal ? signal + " dBm" : ""}` : "";
    const name = el("td", c.hostname || "");
    if (isNew(c)) name.appendChild(el("span", " nový", "badge"));
    if (c.note) name.appendChild(el("div", c.note, "muted small"));
    const macTd = el("td", c.mac, "mono");
    if (c.vendor) macTd.appendChild(el("div", c.vendor, "muted small"));
    tr.append(name, el("td", c.ip || ""), macTd, el("td", c.device || "?"),
              el("td", (c.port || "") + (c.uncertain ? " ?" : "")), el("td", c.vlan || ""), el("td", wifi),
              el("td", c.first_seen ? fmtTime(c.first_seen) : "–"), el("td", c.online ? "teď" : fmtTime(c.last_seen)));
    tb.appendChild(tr);
  }
  if (!list.length) { const tr = el("tr"); const td = el("td", "Žádní klienti (inventura klientů běží každých 5 min)", "muted"); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
}

// --- Wi-Fi lokality --------------------------------------------------------------------

async function loadWifi() {
  try { renderWifi(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi`)); } catch (e) { msg(e.message, true); }
}
$("wifi-btn").addEventListener("click", loadWifi);

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
              el("td", r.ssid || ""), el("td", r.mode || ""), el("td", r.band || ""),
              el("td", r.freq_now ? `${chanNum(r.freq_now)} · ${r.freq_now} MHz${r.frequency === "auto" ? " (auto)" : ""}` : r.frequency || ""),
              el("td", r.width_now ? `${r.width_now} MHz` : r.width || ""), el("td", String(r.clients)),
              el("td", sig + (r.weak_clients ? ` (${r.weak_clients} slabých)` : ""),
                 r.signal_min != null && r.signal_min < -75 ? "warn" : ""));
    tb.appendChild(tr);
  }
  if (!w.radios.length) { const tr = el("tr"); const td = el("td", "Žádná Wi-Fi rádia (inventura běží každých 5 min)", "muted"); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
  renderChannelPlan(w.channel_plan);
  const ct = $("wifi-caps"); ct.textContent = "";
  $("wifi-caps-h").hidden = $("wifi-caps-t").hidden = !w.caps.length;
  for (const c of w.caps) {
    const tr = el("tr");
    tr.append(el("td", c.controller || ""), el("td", c.identity || c.name || ""), el("td", c.address || ""),
              el("td", c.board || ""), el("td", c.version || ""), el("td", c.state || ""));
    ct.appendChild(tr);
  }
}

function chanNum(f) { return f < 3000 ? (f - 2407) / 5 : (f - 5000) / 5; }

function renderChannelPlan(p) {
  $("wifi-plan-wrap").hidden = !p || !p.items.length;
  if (!p || !p.items.length) return;
  const zm = p.changes === 1 ? "změna" : p.changes < 5 ? "změny" : "změn";
  $("wifi-plan-sum").textContent = `· ${p.changes ? `${p.changes} ${zm}` : "beze změn"}` +
    (p.before != null ? ` · překryvy kanálů ${p.before} → ${p.after}` : "");
  const tb = $("wifi-plan"); tb.textContent = "";
  for (const i of p.items) {
    const tr = el("tr", null, i.change ? "" : "muted");
    const now = i.now.freq ? `${i.now.channel} / ${i.now.width} MHz${i.now.auto ? " (auto)" : ""}` : "?";
    const nw = el("td", `${i.new.channel} / ${i.new.width} MHz${i.new.dfs ? " · DFS" : ""}`, i.change ? "plan-new" : "");
    const cmd = el("td");
    if (i.change && i.command) {
      const b = el("button", "Kopírovat", "small secondary"); b.type = "button";
      b.title = i.command;
      b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(i.command); msg("Příkaz zkopírován"); } catch (e) { msg(i.command); }
      });
      cmd.appendChild(b);
    }
    tr.append(el("td", i.device || "?"), el("td", i.interface), el("td", i.band === "5" ? "5 GHz" : "2,4 GHz"),
              el("td", now), nw, el("td", i.change ? i.reasons.join("; ") : "ponechat"), cmd);
    tb.appendChild(tr);
  }
  const ul = $("wifi-plan-notes"); ul.textContent = "";
  for (const n of p.notes) ul.appendChild(el("li", n));
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
// technik smí měnit provozní věci (skupina AP, výjimky portů), ne záměr a hesla
function canOperate() {
  return canManage() || (me && (me.tenants || {})[tenantId] === "technician");
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
        if (!(await ask(`Odebrat ${u.email} z tenanta?`))) return;
        try { await api("DELETE", `/tenants/${tenantId}/users/${u.id}`); loadUsers(); } catch (e) { msg(e.message, true); }
      };
      act.appendChild(b);
    }
    if (me.superadmin && u.mfa) {
      const b = el("button", "Reset MFA", "small secondary");
      b.onclick = async () => {
        if (!(await ask(`Resetovat MFA pro ${u.email}? Při dalším přihlášení si ho nastaví znovu.`))) return;
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

// --- navigace: levý panel se sekcemi, záložky lokality a detailu -----------------------------

const VIEWS = [["overview", "Přehled"], ["site", "Lokalita"], ["plans", "Plány změn"],
  ["alerts", "Alerty"], ["users", "Uživatelé"], ["audit", "Audit"], ["system", "Systém"]];
const navBadges = {};

function buildNav() {
  const nav = $("nav"); nav.textContent = "";
  for (const [key, label] of VIEWS) {
    if (key === "system" && !(me && me.superadmin)) continue;
    const b = el("button", null, "navbtn"); b.type = "button"; b.id = "nav-" + key;
    b.appendChild(el("span", label));
    const badge = el("span", "", "nbadge"); badge.hidden = true;
    b.appendChild(badge); navBadges[key] = badge;
    b.addEventListener("click", () => { if ($("app").hidden) { show("app"); loadDevices(); } showView(key); });
    nav.appendChild(b);
  }
}

function showView(key) {
  if (!VIEWS.some(([k]) => k === key) || (key === "system" && !(me && me.superadmin))) key = "overview";
  prefs.view = key; savePrefs();
  for (const [k] of VIEWS) {
    const v = $("view-" + k); if (v) v.hidden = k !== key;
    const b = $("nav-" + k); if (b) b.className = "navbtn" + (k === key ? " active" : "");
  }
}

function setBadge(key, n, cls) {
  const b = navBadges[key]; if (!b) return;
  b.textContent = n ? String(n) : ""; b.hidden = !n; b.className = "nbadge" + (cls ? " nb-" + cls : "");
}

const SITE_TABS = [["infra", "infra-wrap"], ["topo", "topo-wrap"], ["clients", "clients-wrap"], ["wifi", "wifi-wrap"],
  ["net", "net-wrap"], ["stp", "stp-wrap"], ["ntp", "ntp-wrap"], ["upg", "upg-wrap"], ["deploy", "deploy-wrap"]];

function activateSiteTab(key) {
  prefs.siteTab = key; savePrefs();
  for (const [k, w] of SITE_TABS) { $(w).hidden = k !== key; $(k + "-btn").className = k === key ? "active" : ""; }
}

// loadery záložek jsou definované níž; mapa se vyhodnotí až při volání
function openSiteTab(key) {
  activateSiteTab(key);
  const loaders = { infra: loadInfra, topo: loadTopology, clients: loadClients, wifi: loadWifi, net: loadNetworks,
    stp: loadStp, upg: loadUpgrade, deploy: loadDeploy };
  if (loaders[key]) loaders[key]();
}

const DETAIL_TABS = ["ports", "sec", "vlan", "hist", "ctl"];
function showDetailTab(key) {
  prefs.detailTab = key; savePrefs();
  for (const k of DETAIL_TABS) { $("dp-" + k).hidden = k !== key; $("dt-" + k).className = k === key ? "active" : ""; }
}
for (const k of DETAIL_TABS) $("dt-" + k).addEventListener("click", () => { showDetailTab(k); if (k === "ctl") loadControl(detailId); });

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
const PORT_VIEWS = [["link", "Linka"], ["pvid", "Native VLAN"], ["vlan", "VLAN tagování"], ["poe", "PoE"],
  ["err", "Chyby"], ["anom", "Anomálie"]];
const PORT_OPTS = [["portDev", "Připojená zařízení"], ["portTable", "Tabulka portů"]];
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

function portOpt(key) { return prefs[key] !== false; }

function renderPorts(devId, d) {
  const phys = (d.interfaces || []).filter((i) => i.type === "ether" && PORT_RE.test(i.name));
  const bySfp = (sfp) => phys.filter((i) => /^(sfp|qsfp)/.test(i.name) === sfp).sort((a, b) => portNum(a.name) - portNum(b.name));
  portCtx = { devId, phys, cu: bySfp(false), sfp: bySfp(true), vnames: d.vlan_names || {} };
  drawPorts();
}

function drawPortOpts() {
  const box = $("d-ports-opts"); box.textContent = "";
  for (const [key, label] of PORT_OPTS) {
    const b = el("button", label, "toggle" + (portOpt(key) ? " on" : ""));
    b.type = "button";
    b.setAttribute("aria-pressed", portOpt(key) ? "true" : "false");
    b.addEventListener("click", () => { prefs[key] = !portOpt(key); savePrefs(); drawPorts(); });
    box.appendChild(b);
  }
}

function drawPorts() {
  const c = portCtx;
  const view = PORT_VIEWS.some(([v]) => v === prefs.portView) ? prefs.portView : "link";
  segmented($("d-ports-views"), PORT_VIEWS, view, (v) => { prefs.portView = v; savePrefs(); drawPorts(); });
  drawPortOpts();
  const box = $("d-ports"); box.textContent = "";
  $("d-vlan-filter").hidden = view !== "vlan";
  portLegend(view);
  drawPortTable();
  if (!c.phys.length) { box.appendChild(el("p", "Žádné fyzické porty v inventuře", "muted")); return; }
  if (view === "vlan") return drawVlanMatrix(box);
  const row = el("div", null, "port-row");
  const labels = portOpt("portDev");
  const two = c.cu.length > 8;
  for (const group of [c.cu, c.sfp]) if (group.length) row.appendChild(portGroup(group, (i) => portTile(i, view), labels, two));
  box.appendChild(row);
}

function vlanTitle(vid) {
  const v = portCtx.vnames[vid];
  return v && v.name ? `${v.name} (${vid})` : `VLAN ${vid}`;
}

function drawVlanMatrix(box) {
  const c = portCtx;
  const vids = new Set();
  for (const i of c.phys) {
    const p = i.port || {};
    for (const v of [...vlanSet(p.tagged), ...vlanSet(p.untagged)]) vids.add(v);
    if (p.pvid) vids.add(String(p.pvid));
  }
  const q = $("d-vlan-filter").value.trim().toLowerCase();
  const hit = (v) => !q || v.includes(q) || vlanTitle(v).toLowerCase().includes(q)
    || String((c.vnames[v] || {}).subnet || "").includes(q);
  const list = [...vids].filter(hit).sort((a, b) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0));
  if (!list.length) { box.appendChild(el("p", vids.size ? "Žádná VLAN neodpovídá filtru" : "Porty nejsou v bridgi s VLAN", "muted")); return; }
  for (const vid of list) {
    const members = c.phys.filter((i) => vlanMember(i, vid)).length;
    const r = el("div", null, "vlan-row");
    const head = el("div", null, "vlan-head");
    const name = el("div", vlanTitle(vid), "vlan-name");
    name.appendChild(el("span", ` · ${members} port${members === 1 ? "" : members < 5 && members ? "y" : "ů"}`, "muted"));
    head.appendChild(name);
    const sub = (c.vnames[vid] || {}).subnet;
    if (sub) head.appendChild(el("span", sub, "vlan-subnet"));
    r.appendChild(head);
    const row = el("div", null, "port-row");
    for (const group of [c.cu, c.sfp]) if (group.length) row.appendChild(portGroup(group, (i) => vlanTile(i, vid), false, c.cu.length > 8));
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

function portGroup(ports, tileFn, withLabels, twoRowPanel = false) {
  // víc než 8 portů: liché nahoře, sudé dole (jako na switchi); jinak jedna řada.
  // SFP vedle dvouřadého panelu jdou taky do dvou řad (25 nahoře, 26 dole).
  const twoRows = ports.length > 8 || (twoRowPanel && ports.length > 1);
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

// --- ikony připojených zařízení (jednoduché SVG, barva z CSS) ---
const DEV_ICONS = {
  router: [["rect", { x: 3, y: 9, width: 18, height: 8, rx: 2 }], ["circle", { cx: 7, cy: 13, r: 1 }], ["circle", { cx: 11, cy: 13, r: 1 }],
    ["path", { d: "M8 9 6 4M16 9l2-5" }]],
  switch: [["rect", { x: 2, y: 8, width: 20, height: 8, rx: 1.5 }], ["path", { d: "M5 12h2M9 12h2M13 12h2M17 12h2" }]],
  ap: [["circle", { cx: 12, cy: 12, r: 8.5 }], ["circle", { cx: 12, cy: 12, r: 2.5 }]],
  camera: [["rect", { x: 3, y: 6, width: 13, height: 8, rx: 2 }], ["circle", { cx: 9.5, cy: 10, r: 2 }],
    ["path", { d: "M16 9l5-2v7l-5-2M8 14v5h6" }]],
  pc: [["rect", { x: 3, y: 4, width: 18, height: 12, rx: 1.5 }], ["path", { d: "M9 20h6M12 16v4" }]],
  server: [["rect", { x: 4, y: 3, width: 16, height: 5, rx: 1 }], ["rect", { x: 4, y: 10, width: 16, height: 5, rx: 1 }],
    ["rect", { x: 4, y: 17, width: 16, height: 4, rx: 1 }], ["path", { d: "M7 5.5h1M7 12.5h1" }]],
  nas: [["rect", { x: 6, y: 2.5, width: 12, height: 19, rx: 2 }], ["path", { d: "M9 7h6M9 11h6M9 15h6" }]],
  printer: [["rect", { x: 3, y: 9, width: 18, height: 8, rx: 1.5 }], ["path", { d: "M7 9V3h10v6M7 14h10v7H7z" }]],
  phone: [["rect", { x: 4, y: 4, width: 16, height: 16, rx: 2 }], ["path", { d: "M8 8h8M8 12h2M12 12h2M16 12h0M8 16h2M12 16h2" }]],
  tv: [["rect", { x: 2, y: 5, width: 20, height: 12, rx: 1.5 }], ["path", { d: "M8 21h8" }]],
  unknown: [["circle", { cx: 12, cy: 12, r: 8.5 }], ["path", { d: "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 16.5v.5" }]],
};
const KIND_LABEL = { router: "router", switch: "switch", ap: "AP", camera: "kamera", pc: "počítač", server: "server",
  nas: "NAS", printer: "tiskárna", phone: "telefon", tv: "TV", unknown: "zařízení" };

function devIcon(kind, cls = "") {
  const s = svgEl("svg", { viewBox: "0 0 24 24", class: "dev-ico " + (kind || "unknown") + " " + cls, "aria-hidden": "true" });
  for (const [tag, attrs] of DEV_ICONS[kind] || DEV_ICONS.unknown) s.appendChild(svgEl(tag, attrs));
  return s;
}

function portLabel(i, pos) {
  const n = i.connected, p = i.port || {};
  const c = el("div", null, "port-label " + pos);
  const w = p.poe_power && Number(p.poe_power) > 0 ? `${Number(p.poe_power).toFixed(2)} W` : "";
  if (!n) { c.appendChild(el("span", w, "poe-w")); return c; }
  const name = el("span", shortName(n.name), n.managed ? "managed" : "");
  const ico = el("span", null, "dev" + (n.managed ? " managed" : "") + (i.running ? "" : " gone"));
  ico.appendChild(devIcon(n.kind));
  if (pos === "above") c.append(el("span", w, "poe-w"), name, ico); else c.append(ico, name, el("span", w, "poe-w"));
  c.title = [n.name, KIND_LABEL[n.kind] || "", n.model || "", n.ip || "", n.mac || "",
    n.count > 1 ? `${n.count} MAC adres za portem` : ""].filter(Boolean).join("\n");
  return c;
}

function linkClass(i) {
  const p = i.port || {};
  if (i.disabled) return "off";
  if (!i.running || (p.link && p.link !== "link-ok")) return "down";
  return p.rate && !/G/.test(p.rate) ? "fe" : "gbe";
}

function isUplink(i) {
  const p = i.port || {}, n = i.connected;
  return p.stp_role === "root-port" || (!p.stp_role && !!n && n.managed && n.kind === "router");
}

function portTile(i, view) {
  const p = i.port || {};
  const lc = linkClass(i);
  const t = el("button", null, "port");
  if (view === "link") {
    t.className += " " + lc;
    if (p.full_duplex === false || p.stp_forwarding === false) t.className += " blk";
    if (p.loop) t.className += " loop";
    const poeOn = p.poe && /powered-on|on/.test(p.poe);
    t.appendChild(el("span", (p.loop ? "↻" : "") + (poeOn ? "⚡" : "") + (isUplink(i) ? "↑" : ""), "ico"));
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
  } else if (view === "anom") {
    const a = i.anomaly ? i.anomaly.score : 0;
    t.className += a >= 50 ? " err2" : a >= 20 ? " err1" : a > 0 ? " poe1" : lc === "down" || lc === "off" ? " down" : " plain";
    t.appendChild(el("span", a ? String(a) : "0", "big small"));
  }
  return t;
}

function vlanTile(i, vid) {
  const m = vlanMember(i, vid);
  return el("button", null, "port " + (m === "tagged" ? "vt" : m === "native" ? "vn" : "down"));
}

function portTooltip(i) {
  const p = i.port || {}, n = i.connected, a = i.anomaly;
  return [
    i.name + (i.comment ? ` – ${i.comment}` : ""),
    i.disabled ? "vypnutý" : (i.running ? `linka ${p.rate || "ok"}${p.full_duplex === false ? " half-duplex" : ""}` : "bez linky"),
    p.pvid ? `PVID ${p.pvid}` + (p.tagged && p.tagged.length ? `, tagované ${p.tagged.join(",")}` : "") : "",
    p.stp_role ? `STP ${p.stp_role}${p.stp_forwarding === false ? " (blokuje)" : ""}` : "",
    p.poe ? `PoE ${p.poe}${p.poe_power ? " " + p.poe_power + " W" : ""}` : "",
    p.loop ? "SMYČKA – loop protect port vypnul" : "",
    p.sfp_rx_power ? `SFP RX ${p.sfp_rx_power} dBm` : "",
    n ? `připojeno: ${n.name}${n.ip ? " " + n.ip : ""}` : "",
    a ? `anomálie ${a.score}: ${a.reasons.join(", ")}` : "",
    `chyby RX/TX ${i.rx_error || 0}/${i.tx_error || 0}, výpadky linky ${i.link_downs || 0}`,
  ].filter(Boolean).join("\n");
}

const LEGENDS = {
  link: [["gbe", "GbE+"], ["fe", "FE"], ["down", "bez linky"], ["off", "vypnutý"], [null, "⚡ PoE"], [null, "↑ uplink"], ["blk", "blokuje STP / half-duplex"], ["loop", "↻ smyčka"]],
  pvid: [["plain", "číslo = native VLAN (PVID)"], ["dim", "port bez linky"]],
  vlan: [["vt", "tagovaná"], ["vn", "native (netagovaná)"], ["down", "není členem"]],
  poe: [["poe1", "< 5 W"], ["poe2", "5–15 W"], ["poe3", "≥ 15 W"], ["plain", "PoE bez odběru"], ["down", "bez PoE"]],
  err: [["plain", "bez chyb"], ["err1", "chyby"], ["err2", "> 100 chyb"], [null, "↯ výpadky linky (od 3)"]],
  anom: [["plain", "0 = v pořádku"], ["poe1", "1–19"], ["err1", "20–49"], ["err2", "50–100"], [null, "skóre za 24 h: výpadky, chyby, duplex"]],
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
  if (view === "anom") {
    const worst = portCtx.phys.reduce((a, i) => Math.max(a, i.anomaly ? i.anomaly.score : 0), 0);
    lg.appendChild(el("b", `nejhorší port ${worst}`));
  }
  lg.appendChild(el("span", "klik na port = graf a nastavení", "muted"));
}

// --- tabulka portů (jako UniFi) ---
const PT_COLS = [
  ["port", "Port", (i) => portNum(i.name) + (/^(sfp|qsfp)/.test(i.name) ? 1000 : 0)],
  ["name", "Název", (i) => i.comment || ""],
  ["anom", "Anomálie", (i) => (i.anomaly ? i.anomaly.score : 0)],
  ["stp", "STP", (i) => (i.port || {}).stp_role || ""],
  ["conn", "Připojeno", (i) => (i.connected ? i.connected.name : "")],
  ["poe", "PoE", (i) => Number((i.port || {}).poe_power) || ((i.port || {}).poe ? 0.001 : -1)],
  ["speed", "Rychlost", (i) => (i.running ? rateNum((i.port || {}).rate) : -1)],
  ["mac", "MAC", (i) => (i.connected ? i.connected.mac || "" : "")],
  ["ip", "IP", (i) => ipNum(i.connected && i.connected.ip)],
  ["prof", "Profil", (i) => i.profile || ""],
  ["pvid", "Native VLAN", (i) => Number((i.port || {}).pvid) || 0],
  ["tag", "Tagované", (i) => ((i.port || {}).tagged || []).join(",")],
];
const STP_LABEL = { "root-port": "root port", "designated-port": "designated", "alternate-port": "alternate",
  "backup-port": "backup", disabled: "vypnuto" };

function rateNum(r) {
  const m = String(r || "").match(/^([\d.]+)([MG])/);
  return m ? Number(m[1]) * (m[2] === "G" ? 1000 : 1) : 0;
}

function ipNum(ip) {
  const p = String(ip || "").split(".");
  return p.length === 4 ? p.reduce((a, x) => a * 256 + (Number(x) || 0), 0) : 0;
}

function drawPortTable() {
  const wrap = $("d-ptable-wrap");
  wrap.hidden = !portOpt("portTable") || !portCtx.phys.length;
  if (wrap.hidden) return;
  const sort = PT_COLS.some(([k]) => k === prefs.ptSort) ? prefs.ptSort : "port";
  const desc = !!prefs.ptDesc;
  const h = $("d-ptable-h"); h.textContent = "";
  for (const [key, label] of PT_COLS) {
    const th = el("th", label + (key === sort ? (desc ? " ▾" : " ▴") : ""), key === sort ? "sorted" : "");
    th.addEventListener("click", () => {
      if (prefs.ptSort === key) prefs.ptDesc = !desc; else { prefs.ptSort = key; prefs.ptDesc = key === "anom" || key === "poe"; }
      savePrefs(); drawPortTable();
    });
    h.appendChild(th);
  }
  const fn = PT_COLS.find(([k]) => k === sort)[2];
  const rows = [...portCtx.cu, ...portCtx.sfp].sort((a, b) => {
    const x = fn(a), y = fn(b);
    const r = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "cs");
    return (desc ? -r : r) || portNum(a.name) - portNum(b.name);
  });
  const tb = $("d-ptable-b"); tb.textContent = "";
  for (const i of rows) {
    const p = i.port || {}, n = i.connected, a = i.anomaly;
    const tr = el("tr", null, trafficState.iface === i.name ? "selected" : "");
    const pc = el("td", null, "pt-port");
    const sq = el("span", String(portNum(i.name)), "pt-sq " + linkClass(i));
    pc.append(sq, el("span", i.name, "muted"));
    tr.appendChild(pc);
    tr.appendChild(el("td", i.comment || ""));
    const ac = el("td", a ? String(a.score) : "0", "pt-anom " + (!a ? "ok" : a.score >= 50 ? "bad" : a.score >= 20 ? "warn" : "low"));
    if (a) ac.title = a.reasons.join(", ");
    tr.appendChild(ac);
    tr.appendChild(el("td", p.stp_role ? (STP_LABEL[p.stp_role] || p.stp_role) + (p.edge ? " · edge" : "") : "–",
      p.stp_forwarding === false ? "warn" : ""));
    const cc = el("td", null, "pt-conn");
    if (n) { cc.appendChild(devIcon(n.kind, "sm")); cc.appendChild(el("span", n.name, n.managed ? "managed" : "")); }
    tr.appendChild(cc);
    const w = Number(p.poe_power) || 0;
    tr.appendChild(el("td", p.poe ? (w > 0 ? `${w.toFixed(1)} W` : (/off/.test(p.poe) ? "vyp." : "0 W")) : "–"));
    const sp = el("td", i.disabled ? "vypnutý" : i.running ? (p.rate || "linka") + (p.full_duplex === false ? " HD" : "") : "–",
      i.running ? (linkClass(i) === "fe" ? "fe" : "gbe") : "muted");
    tr.appendChild(sp);
    tr.appendChild(el("td", n && n.mac ? n.mac.toLowerCase() : "", "mono"));
    tr.appendChild(el("td", n && n.ip ? n.ip : ""));
    tr.appendChild(el("td", i.profile || "–", i.profile ? "" : "muted"));
    tr.appendChild(el("td", p.pvid ? vlanTitle(String(p.pvid)) : "–"));
    tr.appendChild(el("td", (p.tagged || []).join(", ") || "–", (p.tagged || []).length ? "" : "muted"));
    tr.addEventListener("click", () => selectPortTraffic(i.name));
    tb.appendChild(tr);
  }
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
      if (!(await ask("Povolit controlleru měnit porty tohoto zařízení? Každá změna se aplikuje s automatickým návratem."))) return;
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
  let loop = null;
  if (st.loop_protect) {
    loop = el("select");
    for (const [v, l] of [["default", "výchozí"], ["on", "zapnuto"], ["off", "vypnuto"]]) { const o = el("option", l); o.value = v; loop.appendChild(o); }
    loop.value = st.loop_protect;
    field("Loop protect", loop);
    if (st.loop_status === "disable") f.appendChild(el("p", "Loop protect port právě vypnul – za portem je smyčka.", "sev-error"));
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
      if (!pf || !(await ask(`Smazat profil „${pf.name}“? Porty se tím nezmění.`))) return;
      try { await api("DELETE", `/tenants/${tenantId}/port-profiles/${pf.id}`); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    pr.append(sel, del);
    box.insertBefore(pr, f);
  }
  const desired = () => {
    const d = { disabled: !enabled.checked, comment: comment.value };
    if (poe) d.poe = poe.value;
    if (loop) d.loop_protect = loop.value;
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
  go.onclick = async () => { if ((await ask("Použít změnu portu? Když se zařízení po změně do 5 minut neozve, vrátí ji samo."))) send(true); };
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
      if (!(await ask(`Vypnout a zapnout napájení na ${port} (zařízení na portu se restartuje)?`))) return;
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

// úprava zařízení v detailu (lokalita, role, poznámka) – bez handleru by formulář odeslal stránku a vše zahodil
$("d-edit").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (!detailId) return;
  try {
    await api("PATCH", `/tenants/${tenantId}/devices/${detailId}`,
              { site_id: $("d-edit-site").value, role: $("d-edit-role").value, note: $("d-edit-note").value });
    const dev = (devicesCache || []).find((x) => x.id === detailId) || { id: detailId };
    const nm = $("d-edit-name").value.trim();
    if (nm && nm !== (dev.identity_pending || dev.identity)) await renameDevice(dev, nm);
    msg("Uloženo");
    loadDevices();
    openDetail(detailId);
  } catch (e) { msg(e.message, true); }
});

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

let upgList = [];      // [{c, on}] v aktuálním pořadí
let upgAuto = [];

async function loadUpgrade() {
  let u;
  try { u = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/upgrade`); } catch (e) { return msg(e.message, true); }
  $("upg-wrap").hidden = false;
  upgAuto = u.candidates.map((c) => c.device_id);
  upgList = u.candidates.map((c) => ({ c, on: true }));
  renderUpgCands();
  const running = u.jobs.some((j) => j.status === "running");
  $("upg-start").hidden = !u.candidates.length || running;
  $("upg-auto").hidden = !u.candidates.length || running;
  const ul = $("upg-jobs"); ul.textContent = "";
  for (const j of u.jobs) {
    const li = el("li", `#${j.id} ${UPG_JOB[j.status] || j.status} – ${fmtTime(j.created_at)} (${j.created_by || "?"})` +
                        (j.message ? `: ${j.message}` : ""), j.status === "stopped" ? "sev-warning" : "");
    if (j.status === "running") {
      const b = el("button", "Zastavit", "small danger");
      b.onclick = async () => {
        if (!(await ask("Zastavit další kroky? Právě probíhající aktualizace zařízení doběhne."))) return;
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

function upgManual() {
  const ids = upgList.filter((x) => x.on).map((x) => x.c.device_id);
  return ids.length !== upgAuto.length || ids.some((id, i) => id !== upgAuto[i]);
}

function renderUpgCands() {
  const tb = $("upg-cands"); tb.textContent = "";
  let n = 0;
  upgList.forEach((x, i) => {
    const tr = el("tr", null, x.on ? "" : "muted");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = x.on;
    cb.addEventListener("change", () => { x.on = cb.checked; renderUpgCands(); });
    const mv = (d) => {
      const b = el("button", d < 0 ? "↑" : "↓", "small secondary"); b.type = "button";
      b.disabled = i + d < 0 || i + d >= upgList.length;
      b.addEventListener("click", () => { const t = upgList[i]; upgList[i] = upgList[i + d]; upgList[i + d] = t; renderUpgCands(); });
      return b;
    };
    const tdc = el("td"); tdc.appendChild(cb);
    const tdm = el("td"); tdm.append(mv(-1), " ", mv(1));
    const c = x.c;
    tr.append(tdc, el("td", x.on ? String(++n) : "–"), el("td", c.label), el("td", c.role),
              el("td", c.depth == null ? "?" : c.depth === 0 ? "brána" : `${c.depth} ${c.depth === 1 ? "skok" : c.depth < 5 ? "skoky" : "skoků"}`),
              el("td", `${c.from} → ${c.version}`), tdm);
    tb.appendChild(tr);
  });
  if (!upgList.length) {
    const tr = el("tr"); const td = el("td", "Žádné online zařízení s dostupnou aktualizací", "muted"); td.colSpan = 7;
    tr.appendChild(td); tb.appendChild(tr);
  }
  $("upg-mode").textContent = upgList.length ? (upgManual() ? "ruční pořadí / výběr" : "automaticky od konce sítě") : "";
}

$("upg-btn").addEventListener("click", () => loadUpgrade());
$("upg-auto").addEventListener("click", () => {
  const by = Object.fromEntries(upgList.map((x) => [x.c.device_id, x.c]));
  upgList = upgAuto.map((id) => ({ c: by[id], on: true }));
  renderUpgCands();
});
$("upg-start").addEventListener("click", async () => {
  const chosen = upgList.filter((x) => x.on);
  if (!chosen.length) return msg("Vyber aspoň jedno zařízení", true);
  const manual = upgManual();
  if (!(await ask(`Spustit postupnou aktualizaci ${chosen.length} zařízení (${manual ? "ruční pořadí" : "od konce sítě"})?\n` +
                  chosen.map((x, i) => `${i + 1}. ${x.c.label}`).join("\n") +
                  "\n\nKaždé se restartuje (výpadek ~2–5 min na zařízení).", "Spustit"))) return;
  try {
    await api("POST", `/tenants/${tenantId}/sites/${siteId()}/upgrade`, manual ? { order: chosen.map((x) => x.c.device_id) } : {});
    msg("Postupná aktualizace spuštěna"); loadUpgrade();
  } catch (e) { msg(e.message, true); }
});

// --- infrastruktura lokality (strom WAN → gateway → switche → AP) ----------------------------

let infraData = null;

async function loadInfra() {
  try { infraData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/infra`); } catch (e) { return msg(e.message, true); }
  $("infra-wrap").hidden = false;
  renderInfra();
}
$("infra-btn").addEventListener("click", loadInfra);
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

// --- sítě lokality a ochrana proti cizím DHCP (DHCP snooping) ---------------------------------

let netData = null;

async function loadNetworks() {
  try { netData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/networks`); }
  catch (e) { return msg(e.message, true); }
  renderNetworks(netData);
  loadIntent();
}

$("net-btn").addEventListener("click", loadNetworks);

const yesNo = (v) => el("td", v ? "✓" : "–", v ? "ok" : "muted");

function renderNetworks(n) {
  $("net-wrap").hidden = false;
  const ul = $("net-warn"); ul.textContent = "";
  for (const w of n.warnings) ul.appendChild(el("li", w, "sev-warning"));
  const tb = $("net-rows"); tb.textContent = "";
  for (const x of n.networks) {
    const tr = el("tr");
    const nm = el("td", null, "net-name");
    nm.append(el("i", null, "dot " + (x.dhcp.mode ? "on" : "")), document.createTextNode(x.comment ? `${x.comment} (${x.name})` : x.name));
    const d = x.dhcp;
    const leases = d.mode === "server" && d.size != null ? `${d.used} / ${d.size}` : "–";
    const pct = d.mode === "server" && d.size ? d.used / d.size : 0;
    tr.append(nm, el("td", x.vlan_id != null ? String(x.vlan_id) + (x.tagged ? "" : " (untagged)") : "–"),
              el("td", x.router), el("td", x.subnet),
              el("td", d.mode === "server" ? "Server" : d.mode === "relay" ? `Relay → ${d.target || "?"}` : "–", d.mode ? "" : "muted"),
              leaseCell(x, leases, pct),
              el("td", x.available != null ? String(x.available) : "–"),
              el("td", x.devices != null ? String(x.devices) : "–"));
    tb.appendChild(tr);
  }
  if (!n.networks.length) { const tr = el("tr"); const td = el("td", "Žádné sítě s adresou (inventura běží každých 5 min)", "muted"); td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); }
  const bt = $("net-bridges"); bt.textContent = "";
  for (const b of n.bridges) {
    const tr = el("tr");
    tr.append(el("td", b.device), el("td", b.bridge), el("td", String(b.ports)),
              el("td", b.protocol || "?", b.protocol === "none" ? "warn" : ""), yesNo(b.vlan_filtering), yesNo(b.igmp_snooping),
              yesNo(b.dhcp_snooping), el("td", b.trusted.join(", ") || "–", b.trusted.length ? "" : "muted"));
    bt.appendChild(tr);
  }
  renderSnooping(n.snooping);
}

// --- leases a statické IP jednoho DHCP serveru -------------------------------------------------

function leaseCell(x, text, pct) {
  const td = el("td", null, pct >= 0.9 ? "bad" : pct >= 0.75 ? "warn" : "");
  if (x.dhcp.mode !== "server") { td.textContent = text; return td; }
  const b = el("button", text, "linkbtn"); b.type = "button";
  b.title = "Leases, statické IP a mapa adres";
  b.addEventListener("click", () => loadLeases(x.router_id, x.dhcp.name, x));
  td.appendChild(b);
  return td;
}

const LEASE_KIND = { static: "rezervace", dynamic: "dynamická", manual: "ruční IP" };
const LEASE_FILTERS = [["all", "Vše"], ["static", "Rezervace"], ["dynamic", "Dynamické"], ["manual", "Ruční IP"], ["warn", "Problémy"]];
const MAP_LABEL = { gateway: "brána", static: "rezervace", dynamic: "dynamická", manual: "ruční IP", pool: "volná v poolu",
  free: "volná mimo pool", conflict: "konflikt" };
let leaseCtx = null;

async function loadLeases(devId, server, net) {
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/devices/${devId}/dhcp/${encodeURIComponent(server)}`); }
  catch (e) { return msg(e.message, true); }
  leaseCtx = { devId, server, net: net || (leaseCtx && leaseCtx.net), v, filter: (leaseCtx && leaseCtx.filter) || "all", edit: null };
  renderLeases();
}

$("lease-close").addEventListener("click", () => { $("lease-wrap").hidden = true; leaseCtx = null; });
$("lease-q").addEventListener("input", () => leaseCtx && drawLeaseRows());
$("lease-new").addEventListener("click", () => { if (leaseCtx) { leaseCtx.edit = { mac: "", ip: leaseCtx.v.suggest }; drawLeaseRows(); } });
$("lease-csv").addEventListener("click", () => {
  if (!leaseCtx) return;
  const esc = (x) => `"${String(x == null ? "" : x).replace(/"/g, '""')}"`;
  const lines = [["IP", "MAC", "Výrobce", "Hostname", "Typ", "Popis", "Poznámka", "DHCP options", "Kde"].map(esc).join(";")];
  for (const r of leaseCtx.v.leases) {
    lines.push([r.ip, r.mac, r.vendor, r.hostname, LEASE_KIND[r.kind], r.comment, r.note, r.options.join(","), whereText(r)].map(esc).join(";"));
  }
  const a = el("a"); a.href = URL.createObjectURL(new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv" }));
  a.download = `leases-${leaseCtx.server}.csv`; a.click();
});

function plural(n, one, few, many) { return `${n} ${n === 1 ? one : n >= 2 && n <= 4 ? few : many}`; }

function whereText(r) {
  const w = r.where;
  if (!w || !w.device) return "";
  return w.ssid ? `${w.device} · ${w.ssid}${w.signal ? " " + w.signal : ""}` : `${w.device} ${w.port || ""}`.trim();
}

function renderLeases() {
  const c = leaseCtx, v = c.v;
  $("lease-wrap").hidden = false;
  const n = c.net;
  $("lease-title").textContent = `${n && n.comment ? n.comment + " · " : ""}${v.subnet || "?"} · DHCP ${v.server} (${v.interface}) na ${v.device}`;
  const sm = v.summary, box = $("lease-sum"); box.textContent = "";
  const chip = (t, cls) => box.appendChild(el("span", t, "sumchip " + (cls || "")));
  chip(plural(sm.static, "rezervace", "rezervace", "rezervací"), "static");
  chip(plural(sm.dynamic, "dynamická", "dynamické", "dynamických"), "dynamic");
  chip(plural(sm.manual, "s ruční IP", "s ruční IP", "s ruční IP"), "manual");
  chip(`volné v poolu ${sm.pool_free} / ${sm.pool_size}`);
  if (sm.conflicts) chip(plural(sm.conflicts, "konflikt", "konflikty", "konfliktů") + " IP", "conflict");
  chip(`brána ${v.gateway || "?"} · pool ${v.pool.join(", ") || "–"}${v.lease_time ? " · lease " + v.lease_time : ""}`, "muted");
  const bn = $("lease-banner"); bn.textContent = "";
  if (v.module_mode !== "manage") {
    const b = el("div", null, "banner info");
    b.appendChild(document.createTextNode("Správa DHCP je na tomto zařízení vypnutá – změny se uloží jako plán ke schválení. "));
    if (canManage()) {
      const en = el("button", "Povolit správu DHCP", "small"); en.type = "button";
      en.onclick = async () => {
        if (!(await ask(`Povolit controlleru měnit DHCP leases na ${v.device}? Každá změna se aplikuje s automatickým návratem.`))) return;
        try { await api("POST", `/tenants/${tenantId}/devices/${c.devId}/modules`, { module: "dhcp", mode: "manage" }); loadLeases(c.devId, c.server); }
        catch (e) { msg(e.message, true); }
      };
      b.appendChild(en);
    }
    bn.appendChild(b);
  }
  if (!v.oui) bn.appendChild(el("div", "Výrobce podle MAC: na serveru chybí balík ieee-data (apt install ieee-data).", "banner"));
  for (const p of v.pending) bn.appendChild(el("div", `Čeká na aplikaci (${p.status}): ${p.summary}`, "banner warn"));
  drawLeaseMap();
  segmented($("lease-filter"), LEASE_FILTERS, c.filter, (f) => { c.filter = f; renderLeases(); });
  drawLeaseRows();
}

function drawLeaseMap() {
  const v = leaseCtx.v, box = $("lease-map"); box.textContent = "";
  const lg = $("lease-map-legend"); lg.textContent = "";
  if (!v.map) { box.appendChild(el("p", "Mapa adres jen pro sítě do /22.", "muted")); return; }
  const base = v.map.first.split(".").map(Number);
  const start = ((base[0] * 256 + base[1]) * 256 + base[2]) * 256 + base[3];
  const ipOf = (k) => { const x = start + k; return [x >>> 24 & 255, x >>> 16 & 255, x >>> 8 & 255, x & 255].join("."); };
  const byIp = {};
  for (const r of v.leases) (byIp[r.ip] = byIp[r.ip] || []).push(r);
  v.map.cells.forEach((st, k) => {
    const ip = ipOf(k);
    const cls = st.endsWith("-conflict") ? "conflict" : st;
    const b = el("button", null, "ipc " + cls); b.type = "button";
    const rs = byIp[ip] || [];
    b.title = `${ip} – ${MAP_LABEL[cls]}` + rs.map((r) => `\n${r.mac} ${r.hostname || ""} ${r.vendor || ""}`).join("");
    b.addEventListener("click", () => {
      if (rs.length) { $("lease-q").value = ip; leaseCtx.filter = "all"; renderLeases(); }
      else if (st !== "gateway") { leaseCtx.edit = { mac: "", ip }; drawLeaseRows(); }
    });
    box.appendChild(b);
  });
  for (const k of ["gateway", "static", "dynamic", "manual", "conflict", "pool", "free"]) {
    const s = el("span"); s.append(el("i", null, "sw ipc " + k), document.createTextNode(MAP_LABEL[k])); lg.appendChild(s);
  }
  lg.appendChild(el("span", "klik na volnou adresu = nová rezervace", "muted"));
}

function drawLeaseRows() {
  const c = leaseCtx, tb = $("lease-rows"); tb.textContent = "";
  const qv = $("lease-q").value.trim().toLowerCase();
  if (c.edit && !c.edit.mac) tb.appendChild(leaseEditRow(null));
  for (const r of c.v.leases) {
    if (c.filter === "warn" ? !r.warnings.length : c.filter !== "all" && r.kind !== c.filter) continue;
    const hay = [r.ip, r.mac, r.hostname, r.vendor, r.note, r.comment].join(" ").toLowerCase();
    if (qv && !hay.includes(qv)) continue;
    const tr = el("tr", null, "lease " + r.kind + (r.warnings.length ? " haswarn" : ""));
    const ipc = el("td", null, "mono");
    ipc.appendChild(el("b", r.ip || "–"));
    if (r.reserved_ip && r.reserved_ip !== r.ip) ipc.appendChild(el("span", ` → ${r.reserved_ip}`, "muted"));
    const name = el("td");
    name.appendChild(el("span", r.hostname || "–", r.hostname ? "" : "muted"));
    if (r.comment) name.appendChild(el("div", r.comment, "muted small"));
    const kind = el("td", null);
    kind.appendChild(el("span", LEASE_KIND[r.kind], "kchip " + r.kind));
    if (r.block_access) kind.appendChild(el("span", "blokován", "kchip conflict"));
    const note = el("td", r.note || "", "note");
    tr.append(ipc, el("td", r.mac, "mono"), el("td", r.vendor || "–", r.vendor ? "" : "muted"), name, kind,
              el("td", whereText(r) || "–", whereText(r) ? "" : "muted"), note,
              el("td", r.options.join(", ") || "–", r.options.length ? "" : "muted"));
    const act = el("td");
    const eb = el("button", "Upravit", "small secondary"); eb.type = "button";
    eb.addEventListener("click", () => { c.edit = c.edit && c.edit.mac === r.mac ? null : { mac: r.mac }; drawLeaseRows(); });
    withCtx(tr, r.ip || r.mac, () => [
      { label: r.kind === "dynamic" ? "Udělat statickou (rezervace)…" : "Upravit…", act: () => { c.edit = { mac: r.mac }; drawLeaseRows(); } },
      { sep: true },
      r.ip ? { label: "Kopírovat IP", act: () => copyText(r.ip, "IP") } : null,
      { label: "Kopírovat MAC", act: () => copyText(r.mac, "MAC") },
      r.hostname ? { label: "Kopírovat hostname", act: () => copyText(r.hostname, "Hostname") } : null,
    ]);
    act.appendChild(eb);
    tr.appendChild(act);
    tb.appendChild(tr);
    if (r.warnings.length) {
      const wr = el("tr", null, "lease-warn"); const td = el("td", "⚠ " + r.warnings.join(" · "), "sev-warning"); td.colSpan = 9;
      wr.appendChild(td); tb.appendChild(wr);
    }
    if (c.edit && c.edit.mac === r.mac) tb.appendChild(leaseEditRow(r));
  }
}

function leaseEditRow(r) {
  const c = leaseCtx, v = c.v;
  const tr = el("tr", null, "lease-edit-row"); const td = el("td"); td.colSpan = 9; tr.appendChild(td);
  const f = el("div", null, "pe-grid");
  const field = (label, input) => { const l = el("label", null, "pe-field"); l.append(el("span", label), input); f.appendChild(l); return input; };
  let mac = null;
  if (!r) { mac = field("MAC adresa", el("input")); mac.placeholder = "AA:BB:CC:DD:EE:FF"; mac.size = 18; }
  const ipBox = el("span", null, "ipbox");
  const ip = el("input"); ip.size = 13; ip.value = (r ? (r.reserved_ip || r.ip) : c.edit.ip) || "";
  const sug = el("button", "volná mimo pool", "small secondary"); sug.type = "button";
  sug.addEventListener("click", () => { if (v.suggest) ip.value = v.suggest; });
  ipBox.append(ip, sug);
  field(r && r.kind === "dynamic" ? "Statická IP (rezervace)" : "IP adresa", ipBox);
  const comment = field("Popis na MikroTiku", el("input")); comment.maxLength = 100; comment.value = r ? r.comment : "";
  comment.placeholder = "jen ASCII, uloží se do leasu";
  const note = field("Poznámka (jen controller)", el("textarea")); note.maxLength = 500; note.rows = 2; note.value = r ? r.note : "";
  const optBox = el("div", null, "pe-tags");
  const optChecks = [];
  for (const o of v.options) {
    const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.value = o.name;
    cb.checked = !!r && r.options.includes(o.name); optChecks.push(cb);
    l.title = `kód ${o.code} = ${o.value}`; l.append(cb, document.createTextNode(` ${o.name} (${o.code})`)); optBox.appendChild(l);
  }
  if (!v.options.length) optBox.appendChild(el("span", "na zařízení zatím nejsou žádné", "muted"));
  field("DHCP options", optBox);
  // nová option
  const nb = el("div", null, "inline newopt");
  const preset = el("select");
  const o0 = el("option", "+ nová option…"); o0.value = ""; preset.appendChild(o0);
  for (const p of v.presets) { const o = el("option", `${p.code} – ${p.label}`); o.value = String(p.code); preset.appendChild(o); }
  const oc = el("option", "vlastní kód"); oc.value = "custom"; preset.appendChild(oc);
  const oName = el("input"); oName.placeholder = "jméno"; oName.size = 10;
  const oCode = el("input"); oCode.type = "number"; oCode.min = 1; oCode.max = 254; oCode.placeholder = "kód"; oCode.size = 4;
  const oType = el("select");
  for (const [t, l] of [["ip", "IP"], ["text", "text"], ["hex", "hex"]]) { const o = el("option", l); o.value = t; oType.appendChild(o); }
  const oVal = el("input"); oVal.placeholder = "hodnota"; oVal.size = 18;
  for (const x of [oName, oCode, oType, oVal]) x.hidden = true;
  preset.addEventListener("change", () => {
    const p = v.presets.find((x) => String(x.code) === preset.value);
    for (const x of [oName, oCode, oType, oVal]) x.hidden = !preset.value;
    if (p) { oCode.value = p.code; oType.value = p.type; oName.value = `opt${p.code}`; }
    else if (preset.value === "custom") { oCode.value = ""; oName.value = ""; }
  });
  nb.append(preset, oName, oCode, oType, oVal);
  field("Přidat DHCP option", nb);
  const al = field("Address-listy (firewall)", el("input")); al.value = r ? r.address_lists : ""; al.placeholder = "např. tiskarny,iot";
  const blk = el("input"); blk.type = "checkbox"; blk.checked = !!r && r.block_access;
  field("Zablokovat přístup", blk);
  td.appendChild(f);
  const res = el("div", null, "muted");
  const bar = el("div", null, "pe-actions");
  const isStatic = r && r.kind === "static";
  const save = el("button", (isStatic ? "Uložit" : "Vytvořit rezervaci") + (canManage() ? " a použít" : " jako plán"), "small");
  save.type = "button";
  const cancel = el("button", "Zavřít", "small secondary"); cancel.type = "button";
  cancel.addEventListener("click", () => { c.edit = null; drawLeaseRows(); });
  const fail = (e) => { res.textContent = e.message; res.className = "sev-error"; };
  const saveNote = async (theMac) => {
    if (!theMac || note.value === (r ? r.note : "")) return true;
    try { await api("PUT", `/tenants/${tenantId}/client-notes/${encodeURIComponent(theMac)}`, { note: note.value }); return true; }
    catch (e) { fail(e); return false; }
  };
  const send = async (desired, theMac) => {
    try {
      const out = await api("POST", `/tenants/${tenantId}/devices/${c.devId}/dhcp/${encodeURIComponent(c.server)}/lease`,
        { mac: theMac, desired, apply: canManage() });
      msg((out.command_id ? "Změna odeslána – aplikuje se s automatickým návratem" : "Plán uložen ke schválení") +
          (out.warnings && out.warnings.length ? " · " + out.warnings.join(" · ") : ""));
      c.edit = null; loadLeases(c.devId, c.server);
    } catch (e) { fail(e); }
  };
  save.addEventListener("click", async () => {
    const theMac = r ? r.mac : mac.value.trim();
    if (!(await saveNote(theMac))) return;
    const d = { address: ip.value.trim(), comment: comment.value, options: optChecks.filter((x) => x.checked).map((x) => x.value),
      address_lists: al.value.trim(), block_access: blk.checked };
    if (preset.value && oVal.value.trim()) {
      d.new_options = [{ name: oName.value.trim(), code: Number(oCode.value), type: oType.value, value: oVal.value.trim() }];
      d.options.push(oName.value.trim());
    }
    send(d, theMac);
  });
  if (r && !isStatic) {
    const nb2 = el("button", "Uložit jen poznámku", "small secondary"); nb2.type = "button";
    nb2.addEventListener("click", async () => { if (await saveNote(r.mac)) { msg("Poznámka uložena"); c.edit = null; loadLeases(c.devId, c.server); } });
    bar.appendChild(nb2);
  }
  bar.prepend(save);
  bar.appendChild(cancel);
  if (r && r.kind === "static") {
    const rm = el("button", "Zrušit rezervaci", "small danger"); rm.type = "button";
    rm.addEventListener("click", async () => { if ((await ask(`Zrušit rezervaci ${r.ip} pro ${r.mac}?`))) send({ remove: true }, r.mac); });
    bar.appendChild(rm);
  }
  td.append(bar, res);
  return tr;
}

const MOD_LABEL = { ntp: "NTP", ports: "porty", dhcp_snooping: "DHCP snooping", dhcp: "DHCP leases" };

const DG_STATUS = { plan: ["změna", "warn"], ok: ["nastaveno", "ok"], blocked: ["nelze bezpečně", "bad"], skip: ["přeskočeno", "muted"] };

function renderSnooping(sn) {
  const rg = $("dg-rogue"); rg.textContent = "";
  for (const r of sn.rogue) rg.appendChild(el("li", `${r.device} ${r.interface}: cizí DHCP server ${r.servers}`, "sev-error"));
  const src = $("dg-sources"); src.textContent = "";
  const names = {};
  for (const d of sn.devices) names[d.device_id] = d.device;
  for (const x of sn.sources) {
    const li = el("li", x.label);
    src.appendChild(li);
  }
  for (const [k, m] of (sn.policy.sources || []).entries()) {
    const li = el("li", `ručně: ${m.label} – ${names[m.device_id] || "?"} ${m.port}${m.ip ? ` (${m.ip})` : ""} `, "muted");
    const rm = el("button", "Odebrat", "small secondary"); rm.type = "button";
    rm.addEventListener("click", () => saveDhcpPolicy((sn.policy.sources || []).filter((_, i) => i !== k)));
    li.appendChild(rm); src.appendChild(li);
  }
  const mi = $("dg-missing"); mi.textContent = "";
  for (const m of sn.missing) {
    mi.appendChild(el("p", `${m.label}: nevím, kde v síti je (není mezi zařízeními ani klienty). Zadej port, za kterým leží.`, "banner warn"));
  }
  const canM = canManage();
  $("dg-manual").hidden = !canM;
  const sel = $("dg-m-dev"); sel.textContent = "";
  for (const d of sn.devices) { const o = el("option", d.device); o.value = d.device_id; sel.appendChild(o); }
  if (sn.missing.length) $("dg-m-ip").value = sn.missing[0].ip;
  const tb = $("dg-devices"); tb.textContent = "";
  for (const d of sn.devices) {
    const tr = el("tr");
    const [stxt, scls] = DG_STATUS[d.status] || [d.status, ""];
    const tp = el("td");
    for (const t of d.trusted) { const sp = el("span", t.port, "chip"); sp.title = t.why.join("\n"); tp.appendChild(sp); }
    if (!d.trusted.length) tp.appendChild(el("span", "–", "muted"));
    const notes = [...d.reasons, ...d.warnings, ...(d.alerts.length ? [`DHCP alert na ${d.alerts.join(", ")}`] : [])];
    tr.append(el("td", d.device), el("td", stxt, "dg-" + scls), tp, el("td", d.keep.join(", ") || "–", d.keep.length ? "" : "muted"),
              el("td", d.bridges.map((b) => b.bridge + (b.now ? " (už zapnuto)" : "")).join(", ") || "–"),
              el("td", notes.join(" · "), d.status === "blocked" ? "sev-error" : "muted"),
              dgModeCell(d));
    tb.appendChild(tr);
  }
  $("dg-generate").hidden = !sn.devices.some((d) => d.status === "plan");
  $("dg-result").textContent = "";
}

function dgModeCell(d) {
  const td = el("td", d.module_mode === "manage" ? "spravuje" : "jen sleduje ", d.module_mode === "manage" ? "" : "muted");
  if (d.module_mode !== "manage" && d.status === "plan" && canManage()) {
    const b = el("button", "Povolit správu", "small secondary"); b.type = "button";
    b.addEventListener("click", async () => {
      if (!(await ask(`Povolit controlleru měnit DHCP snooping na ${d.device}? Každá změna jde přes schválení plánu s automatickým návratem.`))) return;
      try { await api("POST", `/tenants/${tenantId}/devices/${d.device_id}/modules`, { module: "dhcp_snooping", mode: "manage" }); loadNetworks(); }
      catch (e) { msg(e.message, true); }
    });
    td.appendChild(b);
  }
  return td;
}

async function saveDhcpPolicy(sources) {
  try {
    await api("POST", `/tenants/${tenantId}/sites/${siteId()}/policy/dhcp_snooping`,
      { sources, rogue_alerts: netData ? netData.snooping.policy.rogue_alerts !== false : true });
    await loadNetworks();
  } catch (e) { msg(e.message, true); }
}

$("dg-manual").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const cur = (netData && netData.snooping.policy.sources) || [];
  const ip = $("dg-m-ip").value.trim();
  saveDhcpPolicy([...cur, { device_id: $("dg-m-dev").value, port: $("dg-m-port").value.trim(),
    label: $("dg-m-label").value.trim() || (ip ? `DHCP server ${ip}` : "ruční zdroj"), ip: ip || null }]);
});

$("dg-generate").addEventListener("click", async () => {
  try {
    const r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/plans/dhcp_snooping`);
    const n = (r.devices || []).filter((d) => d.plan_id).length;
    $("dg-result").textContent = `${n} plánů čeká na schválení v Plánech změn (zařízení musí mít modul DHCP snooping v režimu správy)`;
    loadPlans();
  } catch (e) { msg(e.message, true); }
});

async function loadStp() {
  try { renderStp(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/stp`)); } catch (e) { msg(e.message, true); }
}
$("stp-btn").addEventListener("click", loadStp);

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
  showDetailTab(DETAIL_TABS.includes(prefs.detailTab) ? prefs.detailTab : "ports");
  if (prefs.detailTab === "ctl") loadControl(id);
  if (!watch || watch.id !== id) { $("d-live").hidden = true; watchDevice(id, null, true); }
  const dev = d.device, sm = d.summary;
  $("d-title").textContent = `${dev.identity || dev.serial} – ${dev.model || ""} (${dev.state})`;
  const w = $("d-warn"); w.textContent = "";
  for (const x of d.warnings) w.appendChild(el("p", (x.severity === "critical" ? "⚠ " : "ⓘ ") + x.text, "sev-" + (x.severity === "critical" ? "error" : "warning")));
  if (dev.inventory_error) w.appendChild(el("p", "Inventura: " + dev.inventory_error, "sev-warning"));
  const pk = d.packages;
  if (pk && pk.wifi && !pk.wifi.ok) {
    const p = el("p", `⚠ Wi-Fi ovladač: ${pk.wifi.problem}`, "sev-warning");
    if (pk.wifi.fix && dev.state === "adopted") {
      const b = el("button", `Vyměnit na ${pk.wifi.fix.install}`, "small"); b.type = "button";
      b.addEventListener("click", () => sendCommand(dev, "wifi_package",
        `Stáhnout ${pk.wifi.fix.install}` + (pk.wifi.fix.remove.length ? `, odinstalovat ${pk.wifi.fix.remove.join(", ")}` : "") +
        " a restartovat (Wi-Fi konfigurace rádií se změní, AP pak převezme CAPsMAN)"));
      p.append(" ", b);
    }
    w.appendChild(p);
  }
  if (pk && pk.mismatch && pk.mismatch.length)
    w.appendChild(el("p", `⚠ Balíčky s jinou verzí než RouterOS (po restartu budou vypnuté): ${pk.mismatch.join(", ")}`, "sev-warning"));
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
    fw.onclick = async () => {
      if (!(await ask(`Aktualizovat RouterBOOT ${sm.firmware} → ${sm.firmware_upgrade}? Zařízení se restartuje.`))) return;
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
  $("d-edit-name").value = dev.identity_pending || dev.identity || "";
  $("d-edit-name").placeholder = dev.serial;
  $("d-edit-name").title = dev.identity_pending && dev.identity_pending !== dev.identity
    ? `Nové jméno ${dev.identity_pending} čeká na potvrzení zařízením` : "Jméno v RouterOS (/system identity)";
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
    ["Uptime", sm.uptime], ["CPU", sm.cpu_load != null ? `${sm.cpu_load} %${sm.cpu ? ` (${sm.cpu})` : ""}` : null],
    ["Paměť", sm.memory_used_pct != null ? `${sm.memory_used_pct} %` : null], ["Úložiště", sm.hdd_used_pct != null ? `${sm.hdd_used_pct} %` : null],
    ["Lokalita", dev.site], ["Poznámka", dev.note], ["Tunel", `${dev.tunnel_ip || "–"} přes ${dev.concentrator || "–"}`],
    ["Check-in", fmtTime(dev.last_checkin_at)], ["Inventura", fmtTime(dev.inventory_at)], ["Záloha", fmtTime(dev.backup_at)],
  ]);
  // moduly
  const mt = $("d-modules"); mt.textContent = "";
  for (const [mod, mode] of Object.entries(d.modules)) {
    const tr = el("tr"); tr.appendChild(el("th", MOD_LABEL[mod] || mod));
    const td = el("td", mode === "manage" ? "spravuje " : "jen sleduje ");
    if (MOD_LABEL[mod]) {
      const b = el("button", mode === "manage" ? "Přepnout na sledování" : "Povolit správu", "small secondary");
      b.onclick = async () => {
        const to = mode === "manage" ? "observe" : "manage";
        if (to === "manage" && !(await ask(`Povolit controlleru měnit ${MOD_LABEL[mod]} na tomto zařízení (vždy až po schválení plánu)?`))) return;
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
                 neighbor: (e) => (e.new ? "ph-up" : "ph-down"), loop: (e) => (e.new === "ano" ? "ph-down" : "ph-up") };

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

// přepínání záložek lokality – registrováno po loaderech, aby se po načtení skryly ostatní panely
for (const [k] of SITE_TABS) $(k + "-btn").addEventListener("click", () => activateSiteTab(k));


// --- řízený režim: záměr lokality ----------------------------------------------------------------

let intentData = null;
let intentNets = [];

async function loadIntent() {
  try { intentData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/intent`); }
  catch (e) { return msg(e.message, true); }
  const cur = intentData.intent;
  intentNets = cur ? cur.networks.map((n) => ({ ...n })) : [];
  $("intent-native").checked = cur ? cur.mgmt_native : true;
  $("intent-auto").checked = cur ? cur.auto_trunk : true;
  $("intent-suggest").hidden = !intentData.suggested || !intentData.suggested.networks.length;
  $("intent-state").textContent = cur ? `– ${cur.networks.length} sítí` : "– zatím nenastaven";
  renderIntent(cur ? cur.access_vlan : null);
  const ul = $("intent-devs"); ul.textContent = "";
  for (const d of intentData.devices) {
    const li = el("li", `${d.name} · ${d.managed ? "řízené" : "sledované"}`, d.managed ? "managed" : "");
    li.title = d.model || "";
    ul.appendChild(li);
  }
  $("intent-result").textContent = "";
  const can = canManage();
  for (const id of ["intent-save", "intent-add", "intent-sync", "intent-suggest", "ssid-add", "wifi-deploy", "wifi-save"]) $(id).disabled = !can;
  renderIntentWifi();
}

// --- Wi-Fi: CAPsMAN, SSID (karty + editor), skupiny AP (tabule s přetahováním) ---
let wifiGroups = [];          // [{id, name}] – ukládá se se záměrem lokality
const apSel = new Set();      // vybraná AP na tabuli (Ctrl+klik)
const BAND_LABEL = { "2.4,5": "2,4 + 5 GHz", "5": "5 GHz", "2.4": "2,4 GHz" };
const CHIP_LABEL = { ax: "ax", ac: "ac", legacy: "starý" };
const sitePath = () => `/tenants/${tenantId}/sites/${siteId()}`;

function wifiPolicy() {
  const cur = intentData.intent;
  return (cur && cur.wifi) || { capsman_device_id: null, max_width: 80, dfs: true, country: "Czech Republic", groups: [] };
}
const netName = (vid) => { const n = intentNets.find((x) => String(x.vlan_id) === String(vid)); return n ? n.name : `VLAN ${vid}`; };
const groupName = (gid) => { const g = wifiGroups.find((x) => x.id === gid); return g ? g.name : "?"; };
const siteAps = () => intentData.devices.filter((d) => d.role === "ap" || (d.wifi && d.role !== "gateway"));

function renderIntentWifi() {
  const w = wifiPolicy();
  wifiGroups = (w.groups || []).map((g) => ({ ...g }));
  const host = $("wifi-host"); host.textContent = "";
  const none = el("option", "— nevybráno —"); none.value = ""; host.appendChild(none);
  for (const d of intentData.devices.filter((x) => x.managed)) {
    const o = el("option", `${d.name}${d.model ? " (" + d.model + ")" : ""}`); o.value = d.id; host.appendChild(o);
  }
  if (!intentData.devices.some((x) => x.managed)) none.textContent = "— nejdřív převezmi zařízení do řízení —";
  host.value = w.capsman_device_id || "";
  $("wifi-width").value = String(w.max_width || 80);
  $("wifi-dfs").checked = w.dfs !== false;
  $("wifi-country").value = w.country || "Czech Republic";
  $("wifi-result").textContent = "";
  renderSsidCards();
  renderApBoard();
  $("apg-add").disabled = !canManage();
  $("wifi-deploy").disabled = !canManage() || !w.capsman_device_id || !(intentData.ssids || []).length;
  $("wifi-deploy").title = !w.capsman_device_id ? "nejdřív vyber a ulož zařízení s CAPsMANem" : "";
}

// kde se SSID vysílá – text pro kartu
function ssidWhere(x) {
  return (x.groups || []).length ? x.groups.map(groupName).join(", ") : "všechna AP";
}

function ssidMenu(x) {
  const can = canManage();
  return [
    { label: "Upravit…", act: () => openSsid(x), key: "Enter" },
    { label: x.enabled ? "Vypnout" : "Zapnout", disabled: !can, act: () => patchSsid(x, { enabled: !x.enabled }) },
    { label: "Roaming (FT)", checked: x.ft !== false, disabled: !can, act: () => patchSsid(x, { ft: x.ft === false }) },
    { label: "Izolace klientů", checked: !!x.isolation, disabled: !can, act: () => patchSsid(x, { isolation: !x.isolation }) },
    { sep: true },
    { head: "Vysílat na" },
    { label: "všech AP", checked: !(x.groups || []).length, disabled: !can, act: () => patchSsid(x, { groups: [] }) },
    ...wifiGroups.map((g) => ({ label: g.name, checked: (x.groups || []).includes(g.id), disabled: !can,
                                act: () => patchSsid(x, { groups: toggled(x.groups || [], g.id) }) })),
    { sep: true },
    (x.ppsk || []).length ? { label: "Zobrazit hesla více hesel", disabled: !can, act: () => revealPpsk(x) } : null,
    { label: "Duplikovat…", disabled: !can, act: () => openSsid({ ...x, id: null, ssid: x.ssid + "-2", ppsk: [] }) },
    { label: "Smazat", danger: true, disabled: !can, act: () => deleteSsid(x) },
  ];
}
const toggled = (arr, v) => (arr.includes(v) ? arr.filter((a) => a !== v) : [...arr, v]);

function renderSsidCards() {
  const box = $("ssid-cards"); box.textContent = "";
  for (const x of intentData.ssids || []) {
    const card = el("div", null, "ssid-card" + (x.enabled === false ? " off" : ""));
    const head = el("div", null, "sc-head");
    head.append(el("span", "📶", "sc-ico"), el("b", x.ssid), moreButton(x.ssid, () => ssidMenu(x)));
    card.appendChild(head);
    const meta = el("div", null, "sc-meta");
    meta.append(el("span", netName(x.vlan_id), "wpill vlan"), el("span", BAND_LABEL[x.bands] || x.bands, "wpill"));
    if (x.ft !== false) meta.appendChild(el("span", "FT", "wpill ok"));
    if (x.isolation) meta.appendChild(el("span", "izolace", "wpill"));
    if (x.enabled === false) meta.appendChild(el("span", "vypnuto", "wpill bad"));
    card.appendChild(meta);
    if ((x.ppsk || []).length) {
      const pp = el("div", null, "sc-ppsk");
      pp.append(el("span", `${x.ppsk.length}× heslo`, "badge-ax"), el("span", " → " + [...new Set(x.ppsk.map((e) => netName(e.vlan_id)))].join(", "), "muted"));
      pp.title = "Více hesel s VLAN – vysílá se jen na cAP ax";
      card.appendChild(pp);
      // AP s 802.11ac tam, kde by se SSID mělo vysílat: tam ho nebude
      const ac = siteAps().filter((d) => d.chip !== "ax" && (!(x.groups || []).length || x.groups.includes(d.ap_group)));
      if (ac.length) {
        const wn = el("div", `⚠ na ${ac.length} AP bez ax se nevysílá`, "sc-warn");
        wn.title = ac.map((d) => d.name).join(", ");
        card.appendChild(wn);
      }
    }
    card.appendChild(el("div", "📍 " + ssidWhere(x), "sc-where"));
    card.addEventListener("click", (ev) => { if (ev.target.tagName !== "BUTTON") openSsid(x); });
    card.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openSsid(x); });
    withCtx(card, x.ssid, () => ssidMenu(x));
    box.appendChild(card);
  }
  const add = el("button", null, "ssid-card add"); add.type = "button";
  add.append(el("span", "+", "sc-plus"), el("span", "Nová Wi-Fi síť"));
  add.disabled = !canManage();
  add.addEventListener("click", () => openSsid({ vlan_id: intentData.intent ? intentData.intent.access_vlan : null }));
  box.appendChild(add);
}

// rychlá změna z menu: celé SSID znovu (heslo a více hesel beze změny)
async function patchSsid(x, change) {
  const body = { ssid: x.ssid, vlan_id: x.vlan_id, bands: x.bands, isolation: !!x.isolation, ft: x.ft !== false,
                 enabled: x.enabled !== false, groups: x.groups || [], ...change };
  try {
    const r = await api("PUT", `${sitePath()}/ssids/${x.id}`, body);
    for (const w of r.warnings || []) msg(w);
    msg(`Wi-Fi ${x.ssid} upravena – na AP se projeví po „Nasadit Wi-Fi“`); loadIntent();
  } catch (e) { msg(e.message, true); }
}

async function deleteSsid(x) {
  if (!(await ask(`Smazat Wi-Fi ${x.ssid}? Z AP zmizí po dalším nasazení Wi-Fi.`, "Smazat"))) return false;
  try { await api("DELETE", `${sitePath()}/ssids/${x.id}`); msg(`Wi-Fi ${x.ssid} smazána`); loadIntent(); return true; }
  catch (e) { msg(e.message, true); return false; }
}

async function revealPpsk(x) {
  try {
    const r = await api("POST", `${sitePath()}/ssids/${x.id}/ppsk/reveal`, {});
    const lines = (r.ppsk || []).map((e) => `${e.name} (${netName(e.vlan_id)}): ${e.psk}`);
    await ask(`${x.ssid} – hesla (zobrazení je v auditu):\n\n${lines.join("\n")}`, "Zavřít");
    return r.ppsk || [];
  } catch (e) { msg(e.message, true); return null; }
}

// --- editor SSID ---
let sd = null;  // {x, bands, groups, ppsk:[{name, vlan_id, psk, saved}], ppskDirty}
function openSsid(x) {
  sd = { x, bands: x.bands || "2.4,5", groups: [...(x.groups || [])], ppskDirty: false,
         ppsk: (x.ppsk || []).map((e) => ({ name: e.name, vlan_id: e.vlan_id, psk: "", saved: true })) };
  $("sd-title").textContent = x.id ? `Wi-Fi ${x.ssid}` : "Nová Wi-Fi síť";
  $("sd-ssid").value = x.ssid || "";
  const v = $("sd-vlan"); v.textContent = "";
  for (const n of intentNets) { const o = el("option", `${n.name} (VLAN ${n.vlan_id})`); o.value = String(n.vlan_id); v.appendChild(o); }
  if (x.vlan_id != null) v.value = String(x.vlan_id);
  $("sd-psk").value = ""; $("sd-psk").type = "password";
  $("sd-psk").placeholder = x.id ? "uloženo (prázdné = beze změny)" : "8–63 znaků";
  $("sd-en").checked = x.enabled !== false; $("sd-ft").checked = x.ft !== false; $("sd-iso").checked = !!x.isolation;
  $("sd-delete").hidden = !x.id; $("sd-ppsk-reveal").hidden = !x.id || !(x.ppsk || []).length;
  $("sd-csv-box").hidden = true; $("sd-warn").textContent = "";
  for (const id of ["sd-save", "sd-delete", "sd-ppsk-add", "sd-ppsk-csv"]) $(id).disabled = !canManage();
  sdBands(); sdGroups(); sdPpsk();
  $("ssid-dlg").hidden = false;
  try { $("sd-ssid").focus(); } catch (_e) { /* fokus není podstatný */ }
}
function sdClose() { $("ssid-dlg").hidden = true; sd = null; }
function sdBands() {
  segmented($("sd-bands"), Object.entries(BAND_LABEL), sd.bands, (b) => { sd.bands = b; sdBands(); });
}
function sdGroups() {
  const box = $("sd-groups"); box.textContent = "";
  const all = el("button", "všechna AP", "gchip" + (sd.groups.length ? "" : " on")); all.type = "button";
  all.addEventListener("click", () => { sd.groups = []; sdGroups(); });
  box.appendChild(all);
  for (const g of wifiGroups) {
    const b = el("button", g.name, "gchip" + (sd.groups.includes(g.id) ? " on" : "")); b.type = "button";
    const n = siteAps().filter((d) => d.ap_group === g.id).length;
    b.title = `${n} AP ve skupině`;
    b.addEventListener("click", () => { sd.groups = toggled(sd.groups, g.id); sdGroups(); });
    box.appendChild(b);
  }
  if (!wifiGroups.length) box.appendChild(el("span", "skupiny AP založíš na tabuli pod kartami", "muted small"));
}
function sdPpsk() {
  const tb = $("sd-ppsk"); tb.textContent = "";
  for (const e of sd.ppsk) {
    const tr = el("tr");
    const nm = el("input"); nm.value = e.name; nm.maxLength = 40; nm.placeholder = "např. Účetní";
    nm.addEventListener("input", () => { e.name = nm.value; sd.ppskDirty = true; });
    const vl = el("select");
    for (const n of intentNets) { const o = el("option", `${n.name} (${n.vlan_id})`); o.value = String(n.vlan_id); vl.appendChild(o); }
    vl.value = String(e.vlan_id); vl.addEventListener("change", () => { e.vlan_id = Number(vl.value); sd.ppskDirty = true; });
    const pk = el("input"); pk.type = "password"; pk.autocomplete = "new-password"; pk.maxLength = 63; pk.value = e.psk;
    pk.placeholder = e.saved ? "uloženo" : "8–63 znaků";
    pk.addEventListener("input", () => { e.psk = pk.value; sd.ppskDirty = true; });
    const rm = el("button", "×", "icon-btn"); rm.type = "button"; rm.title = "Odebrat heslo";
    rm.addEventListener("click", () => { sd.ppsk.splice(sd.ppsk.indexOf(e), 1); sd.ppskDirty = true; sdPpsk(); });
    const td = (...xs) => { const c = el("td"); c.append(...xs); return c; };
    tr.append(td(nm), td(vl), td(pk), td(rm));
    tb.appendChild(tr);
  }
  if (!sd.ppsk.length) {
    const tr = el("tr"); const c = el("td", "Jedno heslo pro všechny (hlavní heslo a VLAN SSID).", "muted"); c.colSpan = 4;
    tr.appendChild(c); tb.appendChild(tr);
  }
}
$("sd-ppsk-add").addEventListener("click", () => {
  const used = new Set(sd.ppsk.map((e) => e.vlan_id));
  const n = intentNets.find((x) => !used.has(Number(x.vlan_id)) && x.purpose !== "mgmt") || intentNets[0];
  sd.ppsk.push({ name: "", vlan_id: n ? Number(n.vlan_id) : Number($("sd-vlan").value), psk: "", saved: false });
  sd.ppskDirty = true; sdPpsk();
});
$("sd-ppsk-csv").addEventListener("click", () => { $("sd-csv-box").hidden = !$("sd-csv-box").hidden; });
$("sd-csv-ok").addEventListener("click", () => {
  // jméno;vlan;heslo (oddělovač ; , nebo tab); VLAN jako číslo nebo jméno sítě
  let n = 0; const bad = [];
  for (const line of $("sd-csv").value.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const [name, vlan, psk] = line.split(/[;,\t]/).map((s) => (s || "").trim());
    const net = intentNets.find((x) => String(x.vlan_id) === vlan || (x.name || "").toLowerCase() === (vlan || "").toLowerCase());
    if (!name || !net) { bad.push(line); continue; }
    const ex = sd.ppsk.find((e) => e.name.toLowerCase() === name.toLowerCase());
    if (ex) { ex.vlan_id = Number(net.vlan_id); if (psk) ex.psk = psk; } else sd.ppsk.push({ name, vlan_id: Number(net.vlan_id), psk: psk || "", saved: false });
    n++;
  }
  sd.ppskDirty = true; sdPpsk();
  $("sd-csv").value = bad.join("\n");
  msg(`Načteno ${n} hesel` + (bad.length ? ` – ${bad.length} řádků nejde přečíst (zůstaly v poli)` : ""), !!bad.length);
  if (!bad.length) $("sd-csv-box").hidden = true;
});
$("sd-ppsk-reveal").addEventListener("click", async () => {
  const r = await revealPpsk(sd.x);
  if (!r || !sd) return;
  for (const e of sd.ppsk) { const f = r.find((x) => x.name.toLowerCase() === e.name.toLowerCase()); if (f && !e.psk) e.psk = f.psk; }
  sdPpsk();
});
$("sd-psk-eye").addEventListener("click", () => { $("sd-psk").type = $("sd-psk").type === "password" ? "text" : "password"; });
$("sd-close").addEventListener("click", sdClose);
$("sd-cancel").addEventListener("click", sdClose);
$("sd-delete").addEventListener("click", async () => { if (await deleteSsid(sd.x)) sdClose(); });
$("sd-save").addEventListener("click", async () => {
  const x = sd.x;
  const body = { ssid: $("sd-ssid").value.trim(), vlan_id: Number($("sd-vlan").value), bands: sd.bands,
                 isolation: $("sd-iso").checked, ft: $("sd-ft").checked, enabled: $("sd-en").checked,
                 psk: $("sd-psk").value, groups: sd.groups };
  if (sd.ppskDirty || !x.id) body.ppsk = sd.ppsk.map((e) => ({ name: e.name.trim(), vlan_id: Number(e.vlan_id), psk: e.psk }));
  const ul = $("sd-warn"); ul.textContent = "";
  try {
    const r = x.id ? await api("PUT", `${sitePath()}/ssids/${x.id}`, body) : await api("POST", `${sitePath()}/ssids`, body);
    for (const w of r.warnings || []) msg(w);
    msg(`Wi-Fi ${body.ssid} uložena – na AP se projeví po „Nasadit Wi-Fi“`);
    sdClose(); loadIntent();
  } catch (e) { ul.appendChild(el("li", e.message, "sev-error")); }
});
if (typeof document.addEventListener === "function") {
  document.addEventListener("keydown", (e) => { if (sd && e.key === "Escape" && $("ask").hidden) sdClose(); });
}

// --- tabule skupin AP ---
function apMenu(d) {
  const ids = apSel.has(d.id) && apSel.size > 1 ? [...apSel] : [d.id];
  const can = canOperate();
  return [
    { label: "Otevřít detail", act: () => openDetail(d.id) },
    { sep: true },
    { head: ids.length > 1 ? `Přesunout ${ids.length} AP do` : "Přesunout do" },
    { label: "bez skupiny", checked: ids.length === 1 && !d.ap_group, disabled: !can, act: () => moveAps(ids, null) },
    ...wifiGroups.map((g) => ({ label: g.name, checked: ids.length === 1 && d.ap_group === g.id, disabled: !can,
                                act: () => moveAps(ids, g.id) })),
    { label: "Nová skupina…", disabled: !canManage(), act: async () => { const g = await addGroup(); if (g) moveAps(ids, g.id); } },
    { sep: true },
    { label: apSel.has(d.id) ? "Zrušit výběr" : "Vybrat", act: () => { apSel.has(d.id) ? apSel.delete(d.id) : apSel.add(d.id); renderApBoard(); } },
  ];
}
function groupMenu(g) {
  const can = canManage();
  const members = siteAps().filter((d) => (d.ap_group || null) === (g ? g.id : null));
  return [
    { label: "Vybrat všechna AP", disabled: !members.length, act: () => { for (const d of members) apSel.add(d.id); renderApBoard(); } },
    g ? { label: "Přejmenovat…", disabled: !can, act: () => renameGroup(g) } : null,
    g ? { label: "Nová Wi-Fi jen pro tuto skupinu…", disabled: !can,
          act: () => openSsid({ vlan_id: intentData.intent ? intentData.intent.access_vlan : null, groups: [g.id] }) } : null,
    g ? { sep: true } : null,
    g ? { label: "Smazat skupinu", danger: true, disabled: !can, act: () => deleteGroup(g) } : null,
  ];
}

function renderApBoard() {
  const board = $("apg-board"); board.textContent = "";
  const aps = siteAps();
  for (const id of [...apSel]) if (!aps.some((d) => d.id === id)) apSel.delete(id);
  const cols = [null, ...wifiGroups];
  for (const g of cols) {
    const col = el("div", null, "apg-col" + (g ? "" : " nogroup"));
    const head = el("div", null, "apg-head");
    const members = aps.filter((d) => (d.ap_group || null) === (g ? g.id : null));
    head.append(el("b", g ? g.name : "Bez skupiny"), el("span", String(members.length), "apg-n"), moreButton(g ? g.name : "Bez skupiny", () => groupMenu(g)));
    if (g) head.addEventListener("dblclick", () => renameGroup(g));
    col.appendChild(head);
    // SSID, která se v této skupině vysílají
    const ss = el("div", null, "apg-ssids");
    const here = (intentData.ssids || []).filter((x) => x.enabled !== false && (g ? !(x.groups || []).length || x.groups.includes(g.id) : !(x.groups || []).length));
    for (const x of here) {
      const p = el("span", x.ssid, "wpill" + ((x.groups || []).length ? " vlan" : ""));
      p.title = (x.groups || []).length ? "jen ve vybraných skupinách" : "na všech AP";
      p.addEventListener("click", () => openSsid(x));
      withCtx(p, x.ssid, () => ssidMenu(x));
      ss.appendChild(p);
    }
    if (!here.length) ss.appendChild(el("span", "žádné SSID", "muted small"));
    col.appendChild(ss);
    const list = el("div", null, "apg-list");
    for (const d of members) list.appendChild(apChip(d));
    if (!members.length) list.appendChild(el("div", g ? "přetáhni sem AP" : "—", "apg-empty"));
    col.appendChild(list);
    // přetahování
    col.addEventListener("dragover", (ev) => { ev.preventDefault(); col.className = col.className.replace(/ drop$/, "") + " drop"; });
    col.addEventListener("dragleave", () => { col.className = col.className.replace(/ drop$/, ""); });
    col.addEventListener("drop", (ev) => {
      ev.preventDefault(); col.className = col.className.replace(/ drop$/, "");
      const id = ev.dataTransfer && ev.dataTransfer.getData("text/plain");
      if (!id) return;
      const ids = apSel.has(id) ? [...apSel] : [id];
      if (ids.every((x) => (aps.find((d) => d.id === x) || {}).ap_group === (g ? g.id : undefined) ||
                           ((aps.find((d) => d.id === x) || {}).ap_group || null) === (g ? g.id : null))) return;
      moveAps(ids, g ? g.id : null);
    });
    withCtx(col, g ? g.name : "Bez skupiny", () => groupMenu(g));
    board.appendChild(col);
  }
  if (!aps.length) board.appendChild(el("p", "Na lokalitě zatím není žádné AP s Wi-Fi.", "muted"));
}

function apChip(d) {
  const c = el("div", null, "ap-chip" + (apSel.has(d.id) ? " sel" : "") + (d.state === "offline" ? " offline" : ""));
  c.draggable = canOperate();
  c.append(el("span", "", "ap-dot"), el("span", d.name, "ap-name"));
  if (d.chip) {
    const b = el("span", CHIP_LABEL[d.chip] || d.chip, "ap-std " + d.chip);
    b.title = d.chip === "ax" ? "802.11ax (wifi-qcom) – umí i více hesel s VLAN"
      : d.chip === "ac" ? "802.11ac (wifi-qcom-ac) – SSID s více hesly se tu nevysílá" : "starý ovladač wireless – CAPsMAN v7 nepodporuje";
    c.appendChild(b);
  }
  c.title = `${d.model || ""}${d.managed ? "" : " · zatím neřízené"}`.trim();
  c.addEventListener("dragstart", (ev) => { if (ev.dataTransfer) { ev.dataTransfer.setData("text/plain", d.id); ev.dataTransfer.effectAllowed = "move"; } });
  c.addEventListener("click", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { apSel.has(d.id) ? apSel.delete(d.id) : apSel.add(d.id); renderApBoard(); }
    else openDetail(d.id);
  });
  withCtx(c, () => (apSel.has(d.id) && apSel.size > 1 ? `${apSel.size} vybraných AP` : d.name), () => apMenu(d));
  return c;
}

async function moveAps(ids, group) {
  try {
    const r = await api("POST", `${sitePath()}/ap-groups`, { devices: ids, group });
    msg(`${r.moved || ids.length} AP → ${group ? groupName(group) : "bez skupiny"} · projeví se po „Nasadit Wi-Fi“`);
    apSel.clear(); loadIntent();
  } catch (e) { msg(e.message, true); }
}

// skupiny jsou součást záměru: uloží se celé nastavení Wi-Fi
async function saveGroups(groups, done) {
  const w = wifiPolicy();
  const body = { ...intentData.intent, wifi: { ...w, groups } };
  try { const r = await api("PUT", `${sitePath()}/intent`, body); msg(done); loadIntent(); return r; }
  catch (e) { msg(e.message, true); return null; }
}
async function addGroup() {
  if (!intentData.intent) { msg("Nejdřív ulož záměr lokality (sítě)", true); return null; }
  const name = await askText("Jméno nové skupiny AP (např. Sklad, 1. patro):", "", "Založit");
  if (!name) return null;
  const r = await saveGroups([...wifiGroups, { name }], `Skupina ${name} založena`);
  return r && r.intent ? (r.intent.wifi.groups || []).find((g) => g.name === name) || null : null;
}
async function renameGroup(g) {
  const name = await askText("Nové jméno skupiny:", g.name);
  if (!name || name === g.name) return;
  saveGroups(wifiGroups.map((x) => (x.id === g.id ? { ...x, name } : x)), `Skupina přejmenována na ${name}`);
}
async function deleteGroup(g) {
  const n = siteAps().filter((d) => d.ap_group === g.id).length;
  const s = (intentData.ssids || []).filter((x) => (x.groups || []).includes(g.id)).length;
  if (!(await ask(`Smazat skupinu ${g.name}?\n\n${n} AP se vrátí mezi AP bez skupiny, ${s} SSID ji přestane uvádět ` +
                  "(SSID bez jiné skupiny se pak vysílá na všech AP).", "Smazat"))) return;
  saveGroups(wifiGroups.filter((x) => x.id !== g.id), `Skupina ${g.name} smazána`);
}
$("apg-add").addEventListener("click", () => addGroup());

$("ssid-add").addEventListener("click", () => openSsid({ vlan_id: intentData.intent ? intentData.intent.access_vlan : null }));
$("wifi-deploy").addEventListener("click", async () => {
  let w;
  try { w = await api("GET", `${sitePath()}/wifi/preview`); } catch (e) { return msg(e.message, true); }
  const ul = $("wifi-result"); ul.textContent = "";
  for (const b of w.blockers || []) ul.appendChild(el("li", b, "sev-error"));
  for (const r of w.radios || []) {
    const li = el("li", `${r.device} ${r.interface}: ${r.band} GHz, kanál ${r.channel ?? "?"} / ${r.width} MHz – ${r.ssids.join(", ")}`);
    if (r.group) li.appendChild(el("span", groupName(r.group), "wpill"));
    if (r.chip) li.appendChild(el("span", CHIP_LABEL[r.chip] || r.chip, "ap-std " + r.chip));
    ul.appendChild(li);
  }
  for (const n of w.notes || []) ul.appendChild(el("li", n, "muted"));
  for (const t of w.tables || []) {
    if (t.table.length && t.sha !== t.installed) ul.appendChild(el("li", `${t.device}: VLAN rádií se na AP doladí samy (${t.table.length} rozhraní)`, "muted"));
  }
  if ((w.blockers || []).length) return;
  if (!(await ask(`${w.summary}\n\nVytvořit plán pro CAPsMAN? Po schválení se Wi-Fi na AP krátce přenastaví (klienti se znovu připojí).`, "Vytvořit plán"))) return;
  try {
    const r = await api("POST", `${sitePath()}/wifi/plan`, {});
    msg(`Plán Wi-Fi #${r.plan_id} čeká na schválení v Plánech změn.`); loadPlans();
  } catch (e) { msg(e.message, true); }
});

function intentInput(n, key, cls, ph) {
  const i = el("input", null, cls);
  i.value = key === "dns" ? (n.dns || []).join(", ") : (n[key] == null ? "" : String(n[key]));
  if (ph) i.placeholder = ph;
  i.addEventListener("input", () => { n[key] = key === "vlan_id" ? i.value.trim() : i.value; if (key === "vlan_id" || key === "name") renderAccess(); });
  const td = el("td"); td.appendChild(i);
  return td;
}

function renderIntent(access) {
  const tb = $("intent-rows"); tb.textContent = "";
  for (const n of intentNets) {
    const tr = el("tr");
    const pu = el("select");
    for (const [k, label] of Object.entries(intentData.purposes)) { const o = el("option", label); o.value = k; pu.appendChild(o); }
    pu.value = n.purpose || "lan";
    pu.addEventListener("change", () => { n.purpose = pu.value; });
    const tdp = el("td"); tdp.appendChild(pu);
    const rm = el("button", "×", "small secondary"); rm.type = "button"; rm.title = "Odebrat síť";
    rm.addEventListener("click", () => { intentNets.splice(intentNets.indexOf(n), 1); renderIntent($("intent-access").value); });
    const tdr = el("td"); tdr.appendChild(rm);
    tr.append(intentInput(n, "name", "", "např. Kancelář"), intentInput(n, "vlan_id", "vid", "10"), tdp,
              intentInput(n, "subnet", "", "192.168.10.0/24"), intentInput(n, "gateway", "", "192.168.10.1"),
              intentInput(n, "dns", "", "volitelné"), tdr);
    tb.appendChild(tr);
  }
  if (!intentNets.length) {
    const tr = el("tr"); const td = el("td", "Žádné sítě – přidej je nebo je načti ze stávající sítě.", "muted"); td.colSpan = 7;
    tr.appendChild(td); tb.appendChild(tr);
  }
  renderAccess(access);
}

function renderAccess(keep) {
  const sel = $("intent-access");
  const prev = keep != null ? String(keep) : sel.value;
  sel.textContent = "";
  for (const n of intentNets) {
    const o = el("option", `${n.name || "?"} (VLAN ${n.vlan_id || "?"})`); o.value = String(n.vlan_id); sel.appendChild(o);
  }
  if ([...sel.children].some((o) => o.value === prev)) sel.value = prev;
}

$("intent-add").addEventListener("click", () => {
  intentNets.push({ name: "", vlan_id: "", purpose: intentNets.some((n) => n.purpose === "mgmt") ? "lan" : "mgmt", subnet: "", gateway: "", dns: [] });
  renderIntent($("intent-access").value);
});
$("intent-suggest").addEventListener("click", () => {
  const sg = intentData.suggested;
  intentNets = sg.networks.map((n) => ({ ...n }));
  $("intent-native").checked = sg.mgmt_native; $("intent-auto").checked = sg.auto_trunk;
  renderIntent(sg.access_vlan);
  msg("Návrh načten ze sítí na zařízeních – zkontroluj jména a účely a ulož.");
});
$("intent-save").addEventListener("click", () => saveIntent());
$("wifi-save").addEventListener("click", () => saveIntent());
async function saveIntent() {
  const body = {
    networks: intentNets.map((n) => ({ ...n, dns: typeof n.dns === "string" ? n.dns : (n.dns || []).join(",") })),
    access_vlan: $("intent-access").value, mgmt_native: $("intent-native").checked, auto_trunk: $("intent-auto").checked,
    wifi: { capsman_device_id: $("wifi-host").value || null, max_width: Number($("wifi-width").value),
            dfs: $("wifi-dfs").checked, country: $("wifi-country").value.trim(), groups: wifiGroups },
  };
  try { await api("PUT", `/tenants/${tenantId}/sites/${siteId()}/intent`, body); msg("Záměr lokality uložen"); loadIntent(); }
  catch (e) { msg(e.message, true); }
}
const SYNC_RESULT = { ok: "beze změn", plan: "plán čeká na schválení", auto: "jen přidání VLAN – aplikuje se samo",
  skipped: "přeskočeno", blocked: "nelze" };
$("intent-sync").addEventListener("click", async () => {
  let r;
  try { r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/intent/reconcile`, {}); }
  catch (e) { return msg(e.message, true); }
  const ul = $("intent-result"); ul.textContent = "";
  r.devices = r.devices || [];
  for (const d of r.devices) {
    ul.appendChild(el("li", `${d.device}: ${SYNC_RESULT[d.result] || d.result}${d.summary ? " – " + d.summary : ""}${d.reason ? " (" + d.reason + ")" : ""}`,
                      d.result === "blocked" ? "sev-warning" : ""));
  }
  if (!r.devices.length) ul.appendChild(el("li", "Na lokalitě zatím není žádné řízené zařízení.", "muted"));
  if (r.devices.some((d) => d.result === "plan")) loadPlans();
});

// --- řízený režim: převzetí a výjimky zařízení -------------------------------------------------------

let ctlData = null;
let ctlOver = {};
const CTL_CLASS = { factory: "tovární / prázdná konfigurace", simple: "jednoduchá (bridge bez VLAN)",
  conforming: "bridge s VLAN filtrováním", custom: "vlastní konfigurace (router, firewall, tunely…)" };
const MODE_LABEL = { trunk: "trunk", access: "přístup", off: "vypnuto" };

async function loadControl(id) {
  if (!id) return;
  try { ctlData = await api("GET", `/tenants/${tenantId}/devices/${id}/takeover`); }
  catch (e) { return msg(e.message, true); }
  if (id !== detailId) return;
  renderControl();
}

function listInto(ul, items, cls) {
  ul.textContent = "";
  for (const x of items || []) ul.appendChild(el("li", x, cls));
}

function renderControl() {
  const c = ctlData;
  ctlOver = JSON.parse(JSON.stringify((c.device_intent && c.device_intent.ports) || {}));
  const b = $("ctl-badge");
  b.textContent = c.managed ? "řízené" : "sledované"; b.className = "badge " + (c.managed ? "managed" : "observed");
  const intro = c.managed
    ? `Konfiguraci zařízení nastavuje controller podle záměru lokality (od ${fmtTime(c.managed_at)}). Změny sítí nebo výjimek se projeví sladěním.`
    : "Převzetí smaže současnou konfiguraci a nasadí ji načisto podle záměru lokality. Předtím se udělá záloha; když se zařízení po převzetí do 12 minut neozve přes tunel, samo se z ní vrátí.";
  $("ctl-intro").textContent = intro;
  listInto($("ctl-blockers"), c.blockers, "sev-error");
  listInto($("ctl-warnings"), c.warnings, "sev-warning");
  const cls = c.classification;
  $("ctl-removed-d").hidden = c.managed || !cls || !cls.removed.length;
  if (cls) {
    listInto($("ctl-removed"), cls.removed);
    $("ctl-removed-n").textContent = `(${cls.removed.length}) – konfigurace: ${CTL_CLASS[cls.class] || cls.class}`;
  }
  $("ctl-body").hidden = !c.model;
  if (!c.model) return;
  renderCtlPorts();
  const vt = $("ctl-vlans"); vt.textContent = "";
  const name = (p) => { if (p === "bridge") return "správa zařízení"; const x = c.model.ports.find((q) => q.port === p); return x ? x.name : p; };
  for (const v of c.model.vlans) {
    const tr = el("tr");
    tr.append(el("td", String(v.vid) + (v.vid === c.model.mgmt.vid ? " (mgmt)" : "")), el("td", v.name),
              el("td", v.tagged.map(name).join(", ") || "–", v.tagged.length ? "" : "muted"),
              el("td", v.untagged.map(name).join(", ") || "–", v.untagged.length ? "" : "muted"));
    vt.appendChild(tr);
  }
  const role = $("ctl-role");
  role.textContent = "";
  for (const [v, l] of [["switch", "switch"], ["ap", "AP + switch (Wi-Fi z CAPsMANu)"]]) { const o = el("option", l); o.value = v; role.appendChild(o); }
  role.value = (c.device_intent && c.device_intent.role) || c.model.role || "switch";
  const apOk = c.wifi === true && !!c.capsman;
  role.children[1].disabled = !apOk && role.value !== "ap";
  $("ctl-role-why").textContent = c.wifi === false ? "rádia jen se starým balíkem wireless – AP nejde řídit"
    : c.wifi == null ? "zařízení nemá Wi-Fi" : !c.capsman ? "AP jde vybrat až po výběru CAPsMANu v záměru lokality (Wi-Fi)"
    : role.value === "ap" ? `rádia řídí CAPsMAN na ${c.capsman}` : "";
  const dm = (c.device_intent && c.device_intent.mgmt) || {};
  $("ctl-mgmt-mode").value = dm.mode || "";
  $("ctl-mgmt-addr").value = dm.address || "";
  $("ctl-mgmt-addr").hidden = dm.mode !== "static";
  const m = c.model.mgmt, path = c.model.path || {};
  $("ctl-mgmt-now").textContent = `→ ${m.mode === "static" ? "statická " + m.address : "DHCP"} ve VLAN ${m.vid}${m.native ? " (na trunkách bez tagu)" : " (tagovaně)"}` +
    (path.address ? `; dnes ${path.address} přes ${path.l3}` : "");
  const rc = $("ctl-reconcile"); rc.textContent = "";
  if (c.reconcile) {
    if (!c.reconcile.changes.length) rc.appendChild(el("li", "Zařízení odpovídá záměru.", "muted"));
    for (const x of c.reconcile.changes) rc.appendChild(el("li", "rozdíl: " + x));
  }
  const custom = cls && cls.class === "custom";
  $("ctl-custom-l").hidden = !custom || c.managed;
  $("ctl-custom").checked = false;
  const go = $("ctl-go");
  go.hidden = c.managed;
  go.disabled = !canManage() || (c.blockers || []).length > 0 || !!(c.active_plan && c.active_plan.module === "takeover");
  go.title = c.active_plan ? `plán #${c.active_plan.id} je ve stavu ${c.active_plan.status}` : "";
}

function renderCtlPorts() {
  const c = ctlData;
  const tb = $("ctl-ports"); tb.textContent = "";
  const nets = c.model.vlans;
  for (const p of c.model.ports) {
    const tr = el("tr");
    const sel = el("select"); sel.setAttribute("aria-label", "Nastavení portu " + p.name);
    const opts = [["", "automaticky"], ...nets.map((v) => [`access:${v.vid}`, `přístup – ${v.name} (${v.vid})`]), ["trunk", "trunk (všechny sítě)"], ["off", "vypnuto"]];
    for (const [v, label] of opts) { const o = el("option", label); o.value = v; sel.appendChild(o); }
    const o = ctlOver[p.name];
    sel.value = !o ? "" : o.mode === "access" ? `access:${o.vlan || ""}` : o.mode;
    sel.disabled = c.model.uplink === p.port;
    sel.addEventListener("change", () => {
      if (!sel.value) delete ctlOver[p.name];
      else if (sel.value.startsWith("access:")) ctlOver[p.name] = { mode: "access", vlan: Number(sel.value.slice(7)) };
      else ctlOver[p.name] = { mode: sel.value };
    });
    const tds = el("td"); tds.appendChild(sel);
    const net = nets.find((v) => v.vid === p.pvid);
    const res = p.mode === "access" ? `přístup – ${net ? net.name : "VLAN " + p.pvid}` : p.mode === "trunk"
      ? (c.model.mgmt.native ? "trunk, mgmt bez tagu" : "trunk, vše tagované") : "vypnuto";
    tr.append(el("td", p.name + (p.name !== p.port ? ` (${p.port})` : "")), tds, el("td", res, "mode-" + p.mode),
              el("td", p.reason + (p.trusted ? " · DHCP trusted" : ""), "muted"));
    tb.appendChild(tr);
  }
}

$("ctl-mgmt-mode").addEventListener("change", () => { $("ctl-mgmt-addr").hidden = $("ctl-mgmt-mode").value !== "static"; });
$("ctl-save").addEventListener("click", async () => {
  const mode = $("ctl-mgmt-mode").value;
  const body = { ports: ctlOver, mgmt: mode ? { mode, address: $("ctl-mgmt-addr").value.trim() } : {}, role: $("ctl-role").value };
  try { await api("PUT", `/tenants/${tenantId}/devices/${detailId}/intent`, body); msg("Výjimky uloženy"); loadControl(detailId); }
  catch (e) { msg(e.message, true); }
});
$("ctl-go").addEventListener("click", async () => {
  const c = ctlData;
  const custom = c.classification && c.classification.class === "custom";
  if (custom && !$("ctl-custom").checked) return msg("Zařízení má vlastní konfiguraci – nejdřív potvrď, že bude smazána.", true);
  const n = (c.classification ? c.classification.removed.length : 0);
  if (!(await ask(`Vytvořit plán převzetí? Po schválení se zařízení zazálohuje, resetuje a nastaví podle záměru (výpadek ~2–3 min).` +
                  (n ? `\n\nZanikne: ${n} položek současné konfigurace.` : ""), "Vytvořit plán"))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/takeover`, { confirm_custom: custom && $("ctl-custom").checked });
    msg(`Plán převzetí #${r.plan_id} čeká na schválení v Plánech změn.`);
    loadPlans();
    loadControl(detailId);
  } catch (e) { msg(e.message, true); }
});


// --- průběh změny na zařízení (doručení → provedení → potvrzení → nová inventura) ----------------------

const PLAN_STATE = { queued: "čeká na doručení", confirming: "provedeno, čeká na potvrzení spojení", applied: "hotovo",
  failed: "selhalo", rolled_back: "vráceno zpět" };
const CMD_STATE = { queued: "čeká na doručení", sent: "zařízení provádí", done: "hotovo", failed: "selhalo" };
let watch = null;

function stopWatch() { if (watch) { clearInterval(watch.timer); watch = null; } }

// sleduje rozpracované změny zařízení; quiet = jen když něco běží (otevření detailu)
function watchDevice(id, label, quiet) {
  if (!id) return;
  stopWatch();
  const w = { id, label, t0: Date.now(), seenBusy: false, doneAt: null, invAt: undefined, timer: null };
  watch = w;
  const bar = () => (detailId === id ? $("d-live") : null);
  const setBar = (text, cls) => { const b = bar(); if (!b) return; b.hidden = false; b.className = "live" + (cls ? " " + cls : ""); b.textContent = text; };
  const tick = async () => {
    if (watch !== w) return;
    let a;
    try { a = await api("GET", `/tenants/${tenantId}/devices/${id}/activity`); } catch (_e) { return; }
    if (watch !== w) return;
    if (a.busy) {
      w.seenBusy = true;
      const p = a.plans.find((x) => x.status === "queued" || x.status === "confirming");
      const c = a.commands.find((x) => x.status === "queued" || x.status === "sent");
      const sent = p && p.status === "queued" && c && c.status === "sent";
      const what = p ? `${(PLAN_MODULE[p.module] || p.module)}: ${sent ? "zařízení provádí změnu" : PLAN_STATE[p.status] || p.status}`
        : c ? `${c.kind}: ${CMD_STATE[c.status] || c.status}` : "probíhá změna";
      setBar(`${what} (${Math.round((Date.now() - w.t0) / 1000)} s)`);
    } else if (!w.seenBusy) {
      if (quiet) { stopWatch(); return; }
      w.seenBusy = true;  // změna proběhla dřív, než se stav poprvé načetl
    } else if (!w.doneAt) {
      w.doneAt = Date.now(); w.invAt = a.inventory_at;
      const last = a.plans[0];
      const bad = last && (last.status === "failed" || last.status === "rolled_back") ||
        (!last && a.commands[0] && a.commands[0].status === "failed");
      setBar(bad ? `Změna ${PLAN_STATE[last ? last.status : "failed"]} – podrobnosti v historii zařízení` : "Změna provedena – načítám nový stav zařízení…",
             bad ? "fail" : "done");
      msg(bad ? `${label || "Zařízení"}: změna ${PLAN_STATE[last ? last.status : "failed"]}` : `${label || "Zařízení"}: změna provedena`, !!bad);
      loadPlans();
      if (bar()) openDetail(id);
    } else if ((a.inventory_at && a.inventory_at !== w.invAt) || Date.now() - w.doneAt > 60000) {
      stopWatch();
      if (detailId === id && !$("detail").hidden) { await openDetail(id); setBar("Stav zařízení aktuální", "done"); setTimeout(() => { const b = bar(); if (b) b.hidden = true; }, 4000); }
      return;
    }
    if (Date.now() - w.t0 > 15 * 60000) stopWatch();
  };
  w.timer = setInterval(tick, 2000);
  tick();
}


// --- nasazení: výchozí konfigurace podle role a výchozí hesla ---------------------------------------

async function loadDeploy() {
  const tb = $("deploy-roles"); tb.textContent = "";
  $("deploy-rotate").disabled = !canManage();
  if (!canManage()) {
    const tr = el("tr"); const td = el("td", "Výchozí konfiguraci (obsahuje klíč lokality) vidí jen správce tenanta.", "muted");
    td.colSpan = 3; tr.appendChild(td); tb.appendChild(tr);
  } else {
    let d;
    try { d = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/defaults`); } catch (e) { return msg(e.message, true); }
    renderDeploy(d);
  }
  loadFactoryPasswords();
}

function renderDeploy(d) {
  const tb = $("deploy-roles"); tb.textContent = "";
  for (const r of d.roles) {
    const tr = el("tr");
    const a = el("a", `mtc-default-${r.role}.rsc`);
    a.href = `/api/admin/tenants/${tenantId}/sites/${siteId()}/defaults/${r.role}.rsc`;
    a.setAttribute("download", `mtc-default-${r.role}.rsc`);
    const td1 = el("td"); td1.appendChild(a);
    const pre = el("pre", r.one_liner);
    const cp = el("button", "Kopírovat", "small secondary"); cp.type = "button";
    cp.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(r.one_liner); msg("Příkaz zkopírován"); } catch (_e) { msg("Kopírování nejde – označ text ručně", true); }
    });
    const td2 = el("td"); td2.append(pre, cp);
    const role = el("td"); role.append(el("strong", r.label), el("div", `v controlleru: ${r.device_role}`, "muted"));
    tr.append(role, td1, td2);
    tb.appendChild(tr);
  }
}

$("deploy-btn").addEventListener("click", () => loadDeploy());
$("deploy-rotate").addEventListener("click", async () => {
  if (!(await ask("Vyměnit klíč lokality? Všechny dříve stažené výchozí konfigurace a příkazy přestanou fungovat " +
                  "(zařízení, která se ještě neohlásila, se neohlásí). Adoptovaných zařízení se to netýká.", "Vyměnit"))) return;
  try { renderDeploy(await api("POST", `/tenants/${tenantId}/sites/${siteId()}/defaults`, {})); msg("Klíč lokality vyměněn"); }
  catch (e) { msg(e.message, true); }
});

async function loadFactoryPasswords() {
  let rows;
  try { rows = await api("GET", `/tenants/${tenantId}/factory-passwords`); } catch (e) { return msg(e.message, true); }
  const tb = $("fpw-rows"); tb.textContent = "";
  for (const r of rows) {
    const tr = el("tr");
    const dev = r.device_id ? `${r.identity || "?"} (${r.state})` : "zatím neadoptované";
    const pw = el("td");
    const show = el("button", "Zobrazit", "small secondary"); show.type = "button"; show.disabled = !canManage();
    show.addEventListener("click", async () => {
      try {
        const x = await api("POST", `/tenants/${tenantId}/factory-passwords/${encodeURIComponent(r.serial)}/reveal`, {});
        pw.textContent = ""; pw.appendChild(el("span", x.password, "pw-shown"));
        setTimeout(() => { pw.textContent = ""; pw.appendChild(show); }, 20000);
      } catch (e) { msg(e.message, true); }
    });
    pw.appendChild(show);
    const del = el("button", "×", "small secondary"); del.type = "button"; del.title = "Smazat"; del.disabled = !canManage();
    del.addEventListener("click", async () => {
      if (!(await ask(`Smazat uložené heslo pro ${r.serial}?`, "Smazat"))) return;
      try { await api("DELETE", `/tenants/${tenantId}/factory-passwords/${encodeURIComponent(r.serial)}`); loadFactoryPasswords(); }
      catch (e) { msg(e.message, true); }
    });
    const tdd = el("td"); tdd.appendChild(del);
    tr.append(el("td", r.serial), el("td", r.note || "–", r.note ? "" : "muted"), el("td", dev, r.device_id ? "" : "muted"), pw, tdd);
    tb.appendChild(tr);
  }
  if (!rows.length) { const tr = el("tr"); const td = el("td", "Zatím žádná hesla.", "muted"); td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr); }
  for (const id of ["fpw-import"]) $(id).disabled = !canManage();
}

$("fpw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", `/tenants/${tenantId}/factory-passwords`,
              { serial: $("fpw-serial").value, password: $("fpw-pass").value, note: $("fpw-note").value });
    $("fpw-serial").value = ""; $("fpw-pass").value = ""; $("fpw-note").value = "";
    msg("Heslo uloženo (šifrovaně)"); loadFactoryPasswords();
  } catch (err) { msg(err.message, true); }
});
$("fpw-import").addEventListener("click", async () => {
  try {
    const r = await api("POST", `/tenants/${tenantId}/factory-passwords/import`, { csv: $("fpw-csv").value });
    $("fpw-import-res").textContent = `importováno ${r.imported}` + (r.errors.length ? `, chyby: ${r.errors.join("; ")}` : "");
    if (!r.errors.length) $("fpw-csv").value = "";
    loadFactoryPasswords();
  } catch (e) { msg(e.message, true); }
});
