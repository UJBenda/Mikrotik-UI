"use strict";
// Admin UI – vanilla JS, žádné externí knihovny (CSP: jen 'self').
// Veškerý text z API se vkládá přes textContent, nikdy přes innerHTML.

const $ = (id) => document.getElementById(id);

// --- překlady ---------------------------------------------------------------------------------
// Zdrojové texty jsou česky a slouží jako klíče (jako gettext). Slovník jazyka načte i18n.js před
// tímto skriptem do window.MTC_DICT; bez slovníku (čeština, testy) se vrací zdrojový text.
//   __("Uložit")                        prostý text
//   __`Smazat ${name}?`                 text s hodnotami – klíč ve slovníku je "Smazat {0}?"
const MTC_DICT = (typeof window !== "undefined" && window.MTC_DICT) || null;
const LOCALE = (typeof window !== "undefined" && window.MTC_LOCALE) || "cs-CZ";
function __(strings, ...vals) {
  if (typeof strings === "string") return (MTC_DICT && MTC_DICT[strings]) || strings;
  let key = strings[0];
  for (let i = 1; i < strings.length; i++) key += `{${i - 1}}` + strings[i];
  const out = (MTC_DICT && MTC_DICT[key]) || key;
  return out.replace(/\{(\d+)\}/g, (m, i) => (Number(i) < vals.length ? String(vals[Number(i)]) : m));
}
let csrf = "";
let me = null;
let tenantId = null;
let sites = [];

// výběr jazyka: seznam ze serveru (balíčkové i vlastní slovníky), volba se pamatuje v prohlížeči
(async () => {
  const sel = $("lang-sel");
  if (!sel || typeof fetch !== "function") return;
  const cur = (typeof window !== "undefined" && window.MTC_LANG) || "cs";
  let langs = [{ code: "cs", language: "Čeština" }];
  try { const r = await fetch("/i18n/index.json"); if (r.ok) langs = (await r.json()).languages || langs; } catch (e) { /* jen čeština */ }
  sel.textContent = "";
  for (const l of langs) {
    const o = document.createElement("option"); o.value = l.code; o.textContent = l.language; sel.appendChild(o);
  }
  sel.value = cur;
  sel.addEventListener("change", () => {
    try { localStorage.setItem("mtc-lang", sel.value); } catch (e) { /* soukromý režim */ }
    location.reload();
  });
})();

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
function ask(text, okLabel = __("Potvrdit")) {
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
async function askText(text, value = "", okLabel = __("Uložit"), maxLength = 40) {
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
  const b = el("button", "⋯", "icon-btn more"); b.type = "button"; b.title = __("Akce (nebo pravé tlačítko)");
  b.setAttribute("aria-haspopup", "menu");
  b.addEventListener("click", (ev) => ctxMenu(ev, typeof title === "function" ? title() : title, items()));
  return b;
}
if (typeof document.addEventListener === "function") {
  document.addEventListener("click", (e) => { const m = $("ctx"); if (m && !m.hidden && !m.contains(e.target)) ctxHide(); });
  // Esc zavře panel editoru portu (když není otevřené menu ani dotaz)
  document.addEventListener("keydown", (e) => {
    const pe = $("port-edit"), m = $("ctx");
    if (e.key === "Escape" && pe && !pe.hidden && (!m || m.hidden) && !askDone) pe.hidden = true;
  });
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
function copyText(t, what) { navigator.clipboard.writeText(String(t)).then(() => msg(__`${what} zkopírováno`)); }

// chyba ve skriptu se ukáže v UI, ne jen v konzoli (jinak tlačítko „nic nedělá“)
if (typeof window.addEventListener === "function") {
  window.addEventListener("error", (e) => msg(__("Chyba v UI: ") + (e.message || e), true));
  window.addEventListener("unhandledrejection", (e) => msg(__("Chyba v UI: ") + ((e.reason && e.reason.message) || e.reason), true));
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
  // detail zařízení je panel vpravo nad aplikací (jako v UniFi) – aplikace zůstává vidět
  if (which === "detail") { $("detail").hidden = false; return; }
  for (const s of ["login", "mfa", "app"]) $(s).hidden = s !== which;
  $("detail").hidden = true;
  $("nav").hidden = which !== "app";
  $("gs").hidden = which !== "app";
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
  return d.toLocaleString(LOCALE);
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

// QR kód z matice ze serveru (řádky znaků 1/0) jako SVG – vždy tmavý na bílém, s tichým okrajem
function drawQr(box, rows) {
  box.textContent = ""; box.hidden = !rows || !rows.length;
  if (box.hidden || typeof document.createElementNS !== "function") return;
  const NS = "http://www.w3.org/2000/svg", n = rows.length, q = 4;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${n + 2 * q} ${n + 2 * q}`); svg.setAttribute("shape-rendering", "crispEdges");
  const bg = document.createElementNS(NS, "rect");
  bg.setAttribute("width", "100%"); bg.setAttribute("height", "100%"); bg.setAttribute("fill", "#fff");
  let d = "";
  rows.forEach((row, r) => { for (let c = 0; c < n; c++) if (row[c] === "1") d += `M${c + q} ${r + q}h1v1h-1z`; });
  const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); p.setAttribute("fill", "#000");
  svg.append(bg, p); box.appendChild(svg);
}

async function startMfa(setup) {
  show("mfa");
  $("mfa-setup").hidden = true;
  if (setup || (me && !me.mfa_ok && !me.mfa_enrolled)) {
    try {
      const s = await api("POST", "/mfa/setup");
      $("mfa-secret").textContent = s.secret.replace(/(.{4})/g, "$1 ").trim();
      $("mfa-uri").textContent = s.uri;
      drawQr($("mfa-qr"), s.qr);
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
  const id = el("div", null, "hmenu-h"); id.append(el("strong", me.email), el("small", me.superadmin ? " superadmin" : ""));
  const out = el("button", __("Odhlásit"), "hitem");
  out.type = "button";
  out.onclick = async () => { await api("POST", "/logout").catch(() => {}); location.reload(); };
  const pw = el("button", __("Změnit heslo"), "hitem");
  pw.type = "button";
  pw.onclick = () => { hmenuClose(); showView("users"); $("pw-card").hidden = false; };
  // jazyk patří k uživateli: po přihlášení se přepínač přestěhuje do jeho nabídky
  const lang = document.querySelector ? document.querySelector(".lang-pick") : null;
  who.append(id, pw);
  if (lang) { lang.classList && lang.classList.add("in-menu"); who.appendChild(lang); }
  who.appendChild(out);
  $("umenu-btn").textContent = me.email;
  $("umenu").hidden = false; $("hdr-bell").hidden = false;
  $("hdr-tenant").hidden = false;
  $("tenant-card").hidden = !me.superadmin;
  buildNav();
  showView(prefs.view || "dash");
  if (me.superadmin) { loadSystem(); loadBackups(); loadTls(); loadNetsvc(); }
  await loadTenants();
}

async function loadTenants() {
  const list = await api("GET", "/tenants");
  const sel = $("tenant");
  sel.textContent = "";
  for (const t of list) {
    const o = el("option", __`${t.name} (${t.devices} zař., ${t.tunnel_block})`);
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
    msg(__("Tenant založen, blok ") + t.tunnel_block);
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

// --- Uložit = nasadit: neblokující ukazatel průběhu (plány s automatickým návratem běží dál) ---
const APPLY_FINAL = { applied: __("hotovo"), rolled_back: __("vráceno zařízením"), failed: __("selhalo"), superseded: __("zastaralé"), discarded: __("zahozeno") };
let applyTimer = null;
function showApply(ap) {
  if (!ap) return;
  const items = [...(ap.devices || []), ...(ap.wifi ? [ap.wifi] : [])];
  for (const x of items.filter((i) => i.result === "blocked")) msg(`${x.device || __("zařízení")}: ${x.reason}`, true);
  const drafts = items.filter((i) => i.result === "draft");
  if (drafts.length) msg(__`${drafts.length === 1 ? __("Změna čeká") : __`${drafts.length} změn čeká`} na schválení – ${drafts[0].reason || __("Aktivita → Změny")}`, true);
  loadPlans();
  const going = items.filter((i) => i.result === "applying" && i.plan_id);
  if (!going.length) return;
  const bar = $("apply-bar");
  const n = new Set(going.map((x) => x.device_id)).size;
  const t0 = Date.now();
  bar.hidden = false; bar.className = "apply-bar";
  bar.textContent = __`Aplikuji změny na ${n} ${n === 1 ? __("zařízení") : n < 5 ? __("zařízeních") : __("zařízeních")} (automatický návrat do 5 min při ztrátě spojení)…`;
  clearInterval(applyTimer);
  const ids = going.map((x) => x.plan_id).join(",");
  const tick = async () => {
    let st;
    try { st = await api("GET", `/tenants/${tenantId}/plans/status?ids=${ids}`); } catch (_e) { return; }
    const left = st.filter((x) => !APPLY_FINAL[x.status]);
    const bad = st.filter((x) => ["rolled_back", "failed"].includes(x.status));
    if (left.length && Date.now() - t0 < 8 * 60000) {
      bar.textContent = __`Aplikuji změny: ${st.length - left.length}/${st.length} hotovo (automatický návrat do 5 min při ztrátě spojení)…`;
      return;
    }
    clearInterval(applyTimer);
    bar.className = "apply-bar " + (bad.length ? "fail" : left.length ? "" : "done");
    bar.textContent = bad.length ? __`Změna na ${bad.map((x) => x.device).join(", ")} ${APPLY_FINAL[bad[0].status]} – podrobnosti v Aktivitě → Změny`
      : left.length ? __("Změny se ještě aplikují – průběh v Aktivitě → Změny") : __("Hotovo");
    setTimeout(() => { bar.hidden = true; }, bad.length ? 15000 : 5000);
    loadPlans();
  };
  applyTimer = setInterval(tick, 3000);
}

// --- přehled: dlaždice s proklikem na seznam zařízení a problémů (s akcemi k řešení) ---
let ovDrill = null;          // klíč otevřené dlaždice
let securityCache = null;    // nálezy všech zařízení (načtou se při otevření dlaždice)
const OV_TILES = [
  // klíč, popis, výběr zařízení / problémů, třída podle počtu
  ["online", __("Online"), () => siteDevices().filter((d) => d.state === "adopted"), ""],
  ["offline", __("Offline"), () => siteDevices().filter((d) => d.state === "offline"), "bad"],
  ["pending", __("Čeká na schválení"), () => siteDevices().filter((d) => d.state === "pending"), "warn"],
  ["provisioning", __("Provisioning"), () => siteDevices().filter((d) => d.state === "provisioning"), ""],
  ["critical", __("Kritické alerty"), () => siteAlerts().filter((a) => a.severity === "critical"), "bad"],
  ["warning", __("Varování"), () => siteAlerts().filter((a) => a.severity === "warning"), "warn"],
  ["updates", __("Dostupná aktualizace"), () => siteDevices().filter((d) => d.update_available), ""],
  ["ros", __("Známé problémy verzí"), () => siteDevices().filter((d) => (d.warnings || []).some((w) => w.severity === "critical")), "bad"],
  ["security", __("Bezpečnostní nálezy"), null, "warn"],
];

// zařízení vybrané lokality; čekající na adopci bez lokality patří ke všem
function siteDevices() {
  const sid = siteId();
  return devicesCache.filter((d) => !sid || d.site_id === sid || (d.state === "pending" && !d.site_id));
}
function siteAlerts() {
  const ids = new Set(siteDevices().map((d) => d.id));
  return alertsCache.filter((a) => !a.device_id || ids.has(a.device_id));
}

function renderOverview() {
  // prázdná lokalita: místo nul první kroky
  $("onboard").hidden = !(siteId() && devicesLoaded && !siteDevices().length);
  const box = $("overview"); box.textContent = "";
  for (const [key, label, pick, cls] of OV_TILES) {
    const n = pick ? pick().length : siteDevices().reduce((s, d) => s + (Number(d.security_issues) || 0), 0);
    if (key === "provisioning" && !n) continue;
    const t = el("button", null, "tile" + (n && cls ? " " + cls : "") + (ovDrill === key ? " open" : ""));
    t.type = "button"; t.title = n ? __("Klikni pro seznam") : "";
    t.setAttribute("aria-expanded", ovDrill === key ? "true" : "false");
    t.append(el("b", String(n)), el("span", label));
    t.addEventListener("click", () => { ovDrill = ovDrill === key ? null : key; renderOverview(); });
    box.appendChild(t);
  }
  renderDrill();
}

const devName = (d) => d.identity || d.serial;
// lokální režim = controller v LAN, bez tunelů (tunnel_ip je pak adresa správy zařízení)
const isLocal = () => !!(me && me.mode === "local");
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
    const open = actBtn(__("Detail"), () => openDetail(d.id), "secondary"); h.appendChild(open);
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
  r.appendChild(el("span", { critical: __("kritické"), warning: __("varování"), info: "info" }[sev] || "", "dr-sev " + (sev || "none")));
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
  const ack = a.acknowledged_at ? null : actBtn(__("Potvrdit"), async () => {
    try { await api("POST", `/tenants/${tenantId}/alerts/${a.id}/ack`); await loadAlerts(); renderOverview(); } catch (e) { msg(e.message, true); }
  });
  const R = {
    offline: [__("Zkontroluj napájení a připojení k internetu – zařízení se po obnovení ozve samo."), []],
    tunnel_down: [__("Tunel ke controlleru nejede; zařízení se přepne na záložní koncentrátor samo."), []],
    backup_stale: [__("Záloha je stará – vyžádej novou."), [actBtn(__("Zálohovat teď"), async () => {
      try { await api("POST", `/tenants/${tenantId}/devices/${a.device_id}/backup`, {}); msg(__("Záloha vyžádána")); } catch (e) { msg(e.message, true); } }, "small")]],
    wifi_package: [__("Rádio potřebuje jiný Wi-Fi balíček."), [cmd("wifi_package", __("Vyměnit Wi-Fi balíček"))]],
    package_version: [__("Balíčky mají různé verze – RouterOS je po restartu vypne."), [d.update_available ? actBtn(__`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]],
    ros_issue: [__("Verze RouterOS má známý problém."), [d.update_available ? actBtn(__`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]],
    upgrade_failed: [__("Aktualizace se nepovedla – zkus ji znovu, případně ručně přes Winbox."), [d.update_available ? actBtn(__("Zkusit znovu"), () => upgradeDevice(d), "small") : null]],
    security_exposed: [__("Router je otevřený z internetu."), [actBtn(__("Bezpečnostní nálezy"), () => { ovDrill = "security"; renderOverview(); }, "small")]],
    rest_unreachable: [__("Controller se k zařízení nedostane přes REST – obnov provisioning."), [cmd("provision", __("Znovu provisioning"))]],
    rest_pin_mismatch: [__("Certifikát REST se změnil – obnov provisioning (nový otisk)."), [cmd("provision", __("Znovu provisioning"))]],
    cert_changed: [__("Certifikát zařízení se změnil – pokud jsi ho neměnil, prověř to."), [cmd("provision", __("Znovu provisioning"))]],
    change_unconfirmed: [__("Změna čeká na potvrzení."), [actBtn(__("Změny"), () => openSub("activity", "plans"), "small")]],
    change_rolled_back: [__("Zařízení změnu samo vrátilo (nepotvrdila se)."), [actBtn(__("Změny"), () => openSub("activity", "plans"), "small")]],
    rogue_dhcp: [__("Na síti odpovídá cizí DHCP server – Nastavení lokality → Sítě."), []],
    port_loop: [__("Smyčka na portu – v detailu port vypni nebo zapni STP."), []],
    port_flapping: [__("Port často padá – zkontroluj kabel a protějšek."), []],
    port_errors: [__("Chyby na portu – kabel, konektor nebo duplex."), []],
    port_half_duplex: [__("Port jede v half-duplexu – zkontroluj autonegociaci."), []],
    link_down: [__("Bezdrátový spoj je dole – zkontroluj napájení a směrování protistrany."), [actBtn(__("Spoje"), () => openSub("devices", "links"), "small")]],
    link_degraded: [__("Bezdrátový spoj je horší než obvykle – na kartě spoje je vidět, co a kdy se změnilo."), [actBtn(__("Spoje"), () => openSub("devices", "links"), "small")]],
    sfp_power: [__("Optický výkon na SFP klesl proti obvyklému – vyčisti konektory, zkontroluj vlákno a modul."), []],
    cpu_high: [__("Vysoké vytížení CPU."), []], memory_high: [__("Málo volné paměti."), []],
    anomaly: [__("Zařízení se chová jinak než obvykle v tuto hodinu (porovnání s posledními 14 dny) – podívej se na časovou osu, co se změnilo."), []],
    ntp_unsynced: [__("Čas není synchronizovaný – Nastavení lokality → Systém a nasazení."), []],
    dhcp_pool_full: [__("DHCP pool je skoro plný – zvětši rozsah nebo zkrať lease."), []],
    config_drift: [__("Konfigurace se liší od záměru – Nastavení lokality → Sítě → Sladit řízená zařízení."), []],
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
  const close = el("button", "×", "icon-btn"); close.type = "button"; close.title = __("Zavřít");
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
      if (key === "pending") line = drillLine(null, `${d.model || ""} · ${d.ros_version || ""}`, __("Schval a rovnou zadej jméno, lokalitu a roli:"), [approveControls(d), forgetButton(d)]);
      else if (key === "offline") line = drillLine("warning", __`Naposledy ${fmtTime(d.last_checkin_at)} · ${isLocal() ? "IP" : __("tunel")} ${d.tunnel_ip || "–"}`,
        __("Zkontroluj napájení a internet. Pokud zařízení už neexistuje, zapomeň ho."), [forgetButton(d)]);
      else if (key === "provisioning") line = drillLine(null, __("Čeká na první check-in po schválení"), __("Zařízení si nastavení vyzvedne do minuty."), []);
      else line = drillLine(null, `${d.model || ""} · RouterOS ${d.ros_version || "?"} · check-in ${fmtTime(d.last_checkin_at)}`, d.note || "", []);
      list.appendChild(drillDevice(d, [line]));
    }
    if (!devs.length) empty(__("Žádné zařízení."));
  } else if (key === "critical" || key === "warning") {
    const by = {};
    for (const a of tile[2]()) (by[a.device_id] = by[a.device_id] || []).push(a);
    for (const [id, as] of Object.entries(by)) {
      const d = { ...devOf(id), identity: as[0].identity, serial: as[0].serial, site: as[0].site };
      list.appendChild(drillDevice(d, as.map((a) => { const [hint, acts] = alertReactions(a); return drillLine(a.severity, a.message, hint, acts); })));
    }
    if (!Object.keys(by).length) empty(__("Žádné otevřené alerty. 🎉"));
  } else if (key === "updates") {
    const devs = tile[2]();
    for (const d of devs) {
      const crit = (d.warnings || []).filter((w) => w.severity === "critical");
      list.appendChild(drillDevice(d, [drillLine(crit.length ? "critical" : "info", __`RouterOS ${d.ros_version} → ${d.latest_version} (kanál ${d.update_channel || "?"})`,
        crit.length ? crit.map((w) => w.text).join(" · ") : __("Před aktualizací se udělá záloha; zařízení se restartuje."),
        [actBtn(__`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small")])]));
    }
    if (devs.length > 1) list.appendChild(el("p", __("Víc zařízení na lokalitě? Nastavení lokality → Systém a nasazení postupuje od konce sítě k bráně."), "muted small"));
    if (!devs.length) empty(__("Všechna zařízení jsou aktuální."));
  } else if (key === "ros") {
    const devs = tile[2]();
    for (const d of devs) {
      list.appendChild(drillDevice(d, (d.warnings || []).map((w) => drillLine(w.severity, w.text, w.source ? __`zdroj: ${w.source}` : "",
        [d.update_available ? actBtn(__`Aktualizovat → ${d.latest_version}`, () => upgradeDevice(d), "small") : null]))));
    }
    if (!devs.length) empty(__("Žádná zařízení se známým problémem verze."));
  } else if (key === "security") {
    list.appendChild(el("p", __("Načítám nálezy…"), "muted"));
    try { securityCache = await api("GET", `/tenants/${tenantId}/security`); } catch (e) { list.textContent = ""; return empty(e.message); }
    if (ovDrill !== key) return;
    list.textContent = "";
    for (const d of securityCache) {
      const fix = d.findings.filter((f) => f.fixable).map((f) => f.code);
      const lines = d.findings.map((f) => drillLine(f.severity, f.text, f.fix ? __("Oprava: ") + f.fix : "", [
        f.fixable ? actBtn(f.fix_label, () => hardeningFix(d, [f.code]), "small") : null,
        f.fix && !f.fixable ? actBtn(__("Kopírovat příkaz"), () => copyText(f.fix, __("Příkaz")), "small secondary") : null,
      ]));
      if (fix.length > 1) lines.push(drillLine(null, __`Opravit vše automaticky (${fix.length})`, __("Jeden plán s automatickým návratem, když se zařízení neozve."),
        [actBtn(__("Opravit vše"), () => hardeningFix(d, fix), "small")]));
      if (d.plan_open) lines.unshift(drillLine("info", __("Oprava už čeká ve Změnách"), "", [actBtn(__("Změny"), () => openSub("activity", "plans"), "small")]));
      list.appendChild(drillDevice(d, lines));
    }
    if (!securityCache.length) empty(__("Bez nálezů ✓"));
  }
}

// oprava nálezu: plán s návratem → schválit hned (nebo čeká na druhého schvalovatele)
async function hardeningFix(d, codes) {
  if (!(await ask(__`${devName(d)}: opravit ${codes.length === 1 ? __("nález") : codes.length + __(" nálezy")}?\n\nVznikne plán změny s automatickým návratem: když se zařízení po změně do 5 minut neozve, vrátí se samo.`, __("Opravit")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/hardening-fix`, { codes });
    try {
      await api("POST", `/tenants/${tenantId}/plans/${r.plan_id}/approve`);
      msg(__`${devName(d)}: ${r.labels.join(", ")} – posílám na zařízení`); watchDevice(d.id, devName(d));
    } catch (e) { msg(__`Plán #${r.plan_id} vytvořen, čeká na schválení: ${e.message}`); }
    loadPlans(); renderDrill();
  } catch (e) { msg(e.message, true); }
}

async function loadSites() {
  sites = await api("GET", `/tenants/${tenantId}/sites`);
  const ul = $("sites"), ts = $("token-site");
  ul.textContent = ""; ts.textContent = "";
  for (const s of sites) {
    // klik = otevřít lokalitu (Přehled a topologie)
    const li = el("li", null, "clickable");
    li.append(el("b", s.name), el("span", __` – ${s.devices} zař.`, "muted"));
    li.addEventListener("click", () => { $("site-sel").value = s.id; showView("dash"); loadSitePanel(); });
    ul.appendChild(li);
    const o = el("option", s.name); o.value = s.id; ts.appendChild(o);
  }
  const busiestSite = sites.reduce((a, s) => (!a || (s.devices || 0) > (a.devices || 0) ? s : a), null);
  if (busiestSite) ts.value = busiestSite.id;
  if (!sites.length) ul.appendChild(el("li", __("Zatím žádná lokalita"), "muted"));
}

$("site-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", `/tenants/${tenantId}/sites`, { name: new FormData(ev.target).get("name") });
    ev.target.reset();
    loadSites();
  } catch (e) { msg(e.message, true); }
});

// --- Přidat zařízení: jeden dialog – příkaz, čekání na ohlášení, pak rovnou adopce -----------
let addTimer = null, addKnown = null, devicesLoaded = false;
$("ob-net").addEventListener("click", () => openSub("settings", "net"));
$("ob-wifi").addEventListener("click", () => openSub("settings", "wifi"));
$("ob-add").addEventListener("click", () => openAdd());
async function newToken() {
  const site = $("token-site").value;
  if (!site) return msg(__("Nejdřív založ lokalitu"), true);
  try {
    const t = await api("POST", `/tenants/${tenantId}/adoption-tokens`, { site_id: site });
    $("token-cmd").textContent = t.command;
    $("token-exp").textContent = new Date(t.expires_at).toLocaleString(LOCALE);
    $("token-out").hidden = false;
  } catch (e) { msg(e.message, true); }
  const st = sites.find((x) => x.id === site);
  $("add-auto").checked = !!(st && st.auto_adopt);
}
$("token-form").addEventListener("submit", (ev) => { ev.preventDefault(); newToken(); });
$("token-site").addEventListener("change", newToken);

async function openAdd() {
  if (!canOperate()) return msg(__("Přidávat zařízení může technik nebo správce"), true);
  $("token-site").value = siteId() || (sites[0] || {}).id || "";
  $("token-site-f").hidden = sites.length < 2;
  $("add-auto-f").hidden = !canManage();
  $("add-wait").textContent = __("Čekám na zařízení…"); $("add-wait").className = "add-wait muted";
  $("add-dlg").hidden = false;
  addKnown = new Set(devicesCache.map((d) => d.id));
  await newToken();
  clearInterval(addTimer);
  addTimer = setInterval(addPoll, 4000);
}
async function addPoll() {
  if ($("add-dlg").hidden) return clearInterval(addTimer);
  try { await loadDevices(); } catch (_e) { return; }
  const d = devicesCache.find((x) => !addKnown.has(x.id));
  if (!d) return;
  addKnown.add(d.id);
  const w = $("add-wait"); w.textContent = ""; w.className = "add-wait found";
  w.appendChild(el("b", __`Ohlásilo se: ${d.model || __("zařízení")} (${d.serial}) `));
  if (d.state === "pending") w.appendChild(actBtn(__("Adoptovat…"), () => { addClose(); openAdopt(d); }, "primary"));
  else w.appendChild(el("span", __("– adoptuje se automaticky"), "muted"));
}
function addClose() { $("add-dlg").hidden = true; clearInterval(addTimer); }
$("add-close").addEventListener("click", addClose);
$("add-dev").addEventListener("click", openAdd);
$("add-dev2").addEventListener("click", openAdd);
$("add-auto").addEventListener("change", async (ev) => {
  const site = $("token-site").value, on = ev.target.checked;
  if (on && !(await ask(__("Zapnout automatickou adopci v této lokalitě? Každé zařízení, které se ohlásí platným příkazem nebo klíčem lokality, ") +
                        __("se schválí bez tvého potvrzení. Bez ptaní se přepíše jen tovární konfigurace.")))) { ev.target.checked = false; return; }
  try {
    await api("PUT", `/tenants/${tenantId}/sites/${site}/auto-adopt`, { enabled: on });
    const st = sites.find((x) => x.id === site); if (st) st.auto_adopt = on;
    msg(on ? __("Automatická adopce zapnuta") : __("Automatická adopce vypnuta"));
  } catch (e) { ev.target.checked = !on; msg(e.message, true); }
});

// nespravované MikroTiky, které řízená zařízení vidí jako sousedy → nabídnout přidání
async function loadFound() {
  const box = $("dev-found"); box.hidden = true;
  const sid = siteId(); if (!sid || !canOperate()) return;
  let t;
  try { t = await api("GET", `/tenants/${tenantId}/sites/${sid}/topology`); } catch (_e) { return; }
  if (sid !== siteId()) return;
  const found = (t.nodes || []).filter((n) => !n.managed && /mikrotik/i.test(String(n.platform || "")));
  if (!found.length) return;
  box.textContent = ""; box.hidden = false;
  box.appendChild(el("h4", __`Nalezeno v síti (${found.length}) – MikroTiky, které ještě nespravuješ`));
  for (const n of found) {
    const r = el("div", null, "df-row");
    r.append(el("b", n.label), el("span", [n.model, n.version, n.address].filter(Boolean).join(" · "), "muted"),
             actBtn(__("Přidat…"), openAdd, "small"));
    box.appendChild(r);
  }
}

$("token-copy").addEventListener("click", () => {
  navigator.clipboard.writeText($("token-cmd").textContent).then(() => msg(__("Zkopírováno")));
});


// odebrání jen v controlleru – když zařízení „Odebrat“ neprovede (nedostupné, zaseknuté)
function forgetButton(d) {
  const b = el("button", d.state === "pending" ? __("Zahodit") : __("Zapomenout"), "small danger");
  b.title = __("Odebrat jen z controlleru, bez úklidu na zařízení");
  b.onclick = () => forgetDevice(d);
  return b;
}

// akce zařízení pro pravé tlačítko na řádku (stejné jako tlačítka v řádku)
function deviceMenu(d) {
  if (d.state === "pending") return [{ label: __("Zahodit"), danger: true, act: () => forgetDevice(d) }];
  const live = d.state !== "removed";
  return [
    { label: __("Otevřít detail"), key: "Enter", act: () => openDetail(d.id) },
    d.update_available && d.state === "adopted" ? { label: __`Aktualizovat → ${d.latest_version}`, act: () => upgradeDevice(d) } : null,
    { sep: true },
    { label: __("Přejmenovat…"), disabled: !canOperate() || d.state === "removed", act: () => renameDevice(d) },
    { label: __("Nastavit jako CAPsMAN lokality…"), disabled: !canManage() || !d.site_id,
      hint: d.managed ? "" : __("CAPsMAN může běžet jen na řízeném zařízení"), act: () => setCapsmanHost(d.id, d.site_id) },
    { label: d.note ? __("Upravit poznámku…") : __("Přidat poznámku…"), disabled: !canOperate(), act: () => editNote(d) },
    { sep: true },
    { label: __("Kopírovat sériové číslo"), act: () => copyText(d.serial, __("Sériové číslo")) },
    d.tunnel_ip ? { label: isLocal() ? __("Kopírovat IP správy") : __("Kopírovat IP v tunelu"), act: () => copyText(d.tunnel_ip, "IP") } : null,
    { sep: true },
    { label: __("Znovu provisioning"), disabled: !live, act: () => sendCommand(d, "provision", __("Znovu provisioning")) },
    { label: __("Restart"), disabled: !live, act: () => sendCommand(d, "reboot", __("Restart")) },
    { sep: true },
    { label: __("Odebrat"), danger: true, disabled: !live, act: () => sendCommand(d, "deprovision", __("Odebrat")) },
    live ? { label: __("Zapomenout"), danger: true, act: () => forgetDevice(d) } : null,
  ];
}

// přejmenování v RouterOS (/system identity); v controlleru se jméno změní po potvrzení zařízením
async function renameDevice(d, name) {
  if (name === undefined) {
    name = await askText(__`Nové jméno pro ${d.identity || d.serial} (pošle se do RouterOS):`, d.identity_pending || d.identity || "", __("Přejmenovat"), 63);
    if (name === null) return false;
  }
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/identity`, { name });
    if (r.unchanged) { msg(__("Jméno je stejné")); return true; }
    msg(__`Přejmenování na ${r.name} posláno` + ((r.aps || []).length ? __` – ${r.aps.length} AP dostane nové jméno CAPsMANu` : ""));
    loadDevices(); watchDevice(d.id, r.name);
    return true;
  } catch (e) { msg(e.message, true); return false; }
}

async function editNote(d) {
  const note = await askText(__`Poznámka k ${d.identity || d.serial} (umístění, kontakt…):`, d.note || "", __("Uložit"), 500);
  if (note === null) return;
  try { await api("PATCH", `/tenants/${tenantId}/devices/${d.id}`, { note }); msg(__("Poznámka uložena")); loadDevices(); }
  catch (e) { msg(e.message, true); }
}

async function forgetDevice(d) {
  const name = d.identity || d.serial;
  const text = d.state === "pending"
    ? __`Zahodit čekající zařízení ${name}? Lze ho kdykoli adoptovat znovu.`
    : __`Zapomenout zařízení ${name}?\n\nOdebere ho jen z controlleru – zařízení nic nedostane. Použij, když „Odebrat“ nejde, ` +
      __("protože zařízení neodpovídá. Klíče se zneplatní, fronta příkazů a plány se zruší, účet na koncentrátoru se vypne. ") +
      __("Zařízení pak jde znovu adoptovat (adopce stávající objekty mtc přepíše).");
  if (!(await ask(text, d.state === "pending" ? __("Zahodit") : __("Zapomenout")))) return;
  try { await api("POST", `/tenants/${tenantId}/devices/${d.id}/forget`, {}); msg(__`${name}: odebráno z controlleru`); loadDevices(); }
  catch (e) { msg(e.message, true); }
}

// adopce na jeden krok: jméno, role, skupina AP – zbytek (převzetí, Wi-Fi) udělá controller sám
function approveControls(d) {
  const b = el("button", __("Adoptovat…"), "small primary"); b.type = "button";
  b.disabled = !canOperate();
  b.onclick = () => openAdopt(d);
  return b;
}

// výchozí role podle modelu: AP řady (cAP, wAP, hAP…) → AP + switch, routery → jen sledovat
function suggestMode(d, hasNets) {
  const m = String(d.model || "");
  if (/^(cAP|wAP|hAP|Audience|Chateau|wsAP|mAP)/i.test(m)) return hasNets ? "ap" : "switch";
  if (/^(CCR|RB5009|RB4011|RB3011|RB1100|hEX|RB750|RB760|L009|RB2011UiAS-IN)/i.test(m)) return "monitor";
  return "switch";
}

let adoptDev = null;
async function openAdopt(d) {
  adoptDev = d;
  $("ad-sub").textContent = `${d.serial}${d.model ? " · " + d.model : ""}${d.ros_version ? " · " + d.ros_version : ""}`;
  $("ad-name").value = d.identity && d.identity !== "MikroTik" ? d.identity : ""; $("ad-name").placeholder = __("např. SW-sklad");
  const ss = $("ad-site"); ss.textContent = "";
  for (const st of sites) { const o = el("option", st.name); o.value = st.id; ss.appendChild(o); }
  ss.value = d.site_id || siteId() || (sites[0] || {}).id || "";
  $("ad-site-f").hidden = sites.length < 2 || !!d.site_id;
  await adoptSiteChanged(true);
  $("adopt-dlg").hidden = false;
  try { $("ad-name").focus(); } catch (_e) { /* fokus není podstatný */ }
}
async function adoptSiteChanged(suggest) {
  let it = null;
  try { it = await api("GET", `/tenants/${tenantId}/sites/${$("ad-site").value}/intent`); } catch (_e) { it = null; }
  const nets = !!(it && it.intent && it.intent.networks.length);
  const groups = (it && it.intent && (it.intent.wifi || {}).groups) || [];
  const g = $("ad-group"); g.textContent = "";
  const o0 = el("option", __("bez skupiny (všechna SSID)")); o0.value = ""; g.appendChild(o0);
  for (const x of groups) { const o = el("option", x.name); o.value = x.id; g.appendChild(o); }
  adoptState.groups = groups.length; adoptState.nets = nets;
  if (suggest) { const m = suggestMode(adoptDev, nets); for (const k of ["switch", "ap", "monitor"]) $("ad-m-" + k).checked = k === m; }
  adoptModeChanged();
}
const adoptState = { groups: 0, nets: false };
function adoptMode() { return ["switch", "ap", "monitor"].find((k) => $("ad-m-" + k).checked) || "switch"; }
function adoptModeChanged() {
  const m = adoptMode();
  $("ad-group-f").hidden = m !== "ap" || !adoptState.groups;
  $("ad-note").textContent = m === "monitor" ? __("Konfigurace zařízení se nezmění.")
    : !adoptState.nets ? __("Lokalita zatím nemá sítě – převzetí počká, až je založíš (Nastavení lokality → Sítě).")
    : __("Po schválení: správa, inventura") + (m === "ap" ? __(", ovladač Wi-Fi") : "") + __(" a převzetí do řízení podle sítí lokality. ") +
      __("Tovární a jednoduchou konfiguraci controller přepíše sám (záloha a návrat při výpadku); vlastní konfiguraci (firewall, víc bridgů) nejdřív ukáže a zeptá se.");
}
for (const k of ["switch", "ap", "monitor"]) $("ad-m-" + k).addEventListener("change", adoptModeChanged);
$("ad-site").addEventListener("change", () => adoptSiteChanged(false));
function adoptClose() { $("adopt-dlg").hidden = true; adoptDev = null; }
$("ad-close").addEventListener("click", adoptClose);
$("ad-cancel").addEventListener("click", adoptClose);
$("ad-ok").addEventListener("click", async () => {
  const d = adoptDev; if (!d) return;
  const mode = adoptMode();
  const body = { site_id: $("ad-site").value, mode, identity: $("ad-name").value.trim() || null,
                 ap_group: mode === "ap" && $("ad-group").value ? $("ad-group").value : null };
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${d.id}/approve`, body);
    if (r.concentrator_errors.length) msg(__("Schváleno, ale koncentrátor: ") + r.concentrator_errors.join("; "), true);
    else msg(mode === "monitor" ? __("Schváleno – zařízení se bude jen sledovat") : __("Schváleno – controller zařízení nastaví sám, průběh je u zařízení"));
    adoptClose(); loadDevices();
  } catch (e) { msg(e.message, true); }
});

// průběh adopce na jeden krok (řádek zařízení, panel)
const FLOW_STEP = { provision: __("zakládám správu"), inventory: __("inventura"), wifi_pkg: __("ovladač Wi-Fi"), takeover: __("převzetí"),
  takeover_wait: __("převzetí"), wifi: __("Wi-Fi"), needs_confirm: __("čeká na rozhodnutí"), failed: __("nepovedlo se"), done: __("hotovo") };
function flowActive(d) { return d.adopt_flow && d.adopt_flow.step !== "done"; }
function flowChip(d) {
  const f = d.adopt_flow;
  const cls = f.step === "failed" ? "bad" : f.step === "needs_confirm" ? "warn" : "run";
  const c = el("span", (f.step === "failed" || f.step === "needs_confirm" ? "⚠ " : "⟳ ") + (FLOW_STEP[f.step] || f.step), "flow-chip " + cls);
  c.title = f.msg || "";
  return c;
}
async function adoptDecide(d, action) {
  const t = { overwrite: __`Přepsat konfiguraci ${d.identity || d.serial} načisto? Před resetem se udělá záloha; když se zařízení neozve, samo se z ní vrátí.`,
              monitor: __`Ponechat ${d.identity || d.serial} jen ke sledování? Konfigurace se nezmění.`, retry: __("Zkusit adopci znovu od inventury?") }[action];
  if (!(await ask(t))) return;
  try { await api("POST", `/tenants/${tenantId}/devices/${d.id}/adopt`, { action }); msg(__("Uloženo")); loadDevices(); if (detailId === d.id) openDetail(d.id); }
  catch (e) { msg(e.message, true); }
}
async function startManage(d, target) {
  if (!(await ask(__`Převzít ${d.identity || d.serial} do řízení jako ${target === "ap" ? "AP + switch" : "switch"}? ` +
                  __("Porty a VLAN pak nastavuje controller podle sítí lokality. Vlastní konfiguraci (firewall, víc bridgů) ") +
                  __("nejdřív ukáže a zeptá se; jinak převezme sám se zálohou a návratem při výpadku.")))) return;
  try { await api("POST", `/tenants/${tenantId}/devices/${d.id}/adopt`, { action: "manage", target }); msg(__("Převzetí běží – průběh je u zařízení")); await loadDevices(); openDetail(d.id); }
  catch (e) { msg(e.message, true); }
}
function flowBanner(d) {
  const f = d.adopt_flow;
  const b = el("div", null, "banner " + (f.step === "failed" ? "warn" : f.step === "needs_confirm" ? "warn" : "info"));
  b.appendChild(el("b", __`Adopce: ${FLOW_STEP[f.step] || f.step}. `));
  b.appendChild(document.createTextNode(f.msg || ""));
  if (f.step === "needs_confirm" && f.removed && f.removed.length) b.appendChild(el("div", __("Smaže se: ") + f.removed.slice(0, 8).join(" · "), "muted small"));
  const acts = el("div", null, "intent-acts");
  if (f.step === "needs_confirm") acts.append(actBtn(__("Přepsat načisto"), () => adoptDecide(d, "overwrite"), "primary"), actBtn(__("Ponechat ke sledování"), () => adoptDecide(d, "monitor")));
  if (f.step === "failed") acts.append(actBtn(__("Zkusit znovu"), () => adoptDecide(d, "retry"), ""), actBtn(__("Ponechat ke sledování"), () => adoptDecide(d, "monitor")));
  if (acts.children.length) b.appendChild(acts);
  return b;
}

async function sendCommand(d, kind, label, extra) {
  if (!(await ask(`${label}: ${d.serial}?`))) return;
  try {
    await api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, Object.assign({ kind }, extra || {}));
    msg(__`${label}: posílám na zařízení`);
    watchDevice(d.id, d.identity || d.serial);
  } catch (e) { msg(e.message, true); }
}

async function upgradeDevice(d) {
  const warn = (d.warnings || []).filter((x) => x.severity !== "info").map((x) => "• " + x.text);
  const text = __`Aktualizovat RouterOS na ${d.serial} z ${d.ros_version} na ${d.latest_version}?\n` +
    __("Zařízení si verzi stáhne a restartuje se (výpadek ~2–5 min). Předtím se doporučuje záloha.") +
    (warn.length ? __("\n\nZnámé problémy současné verze:\n") + warn.join("\n") : "");
  if (!(await ask(text))) return;
  api("POST", `/tenants/${tenantId}/devices/${d.id}/commands`, { kind: "upgrade", version: d.latest_version })
    .then(() => msg(__`Aktualizace ${d.serial} na ${d.latest_version} naplánována – provede se při dalším check-inu`))
    .catch((e) => msg(e.message, true));
}

async function loadAudit() {
  const list = await api("GET", `/tenants/${tenantId}/audit`);
  const tb = $("audit");
  tb.textContent = "";
  for (const a of list.slice(0, 50)) {
    const tr = el("tr");
    tr.append(el("td", new Date(a.at).toLocaleString(LOCALE)), el("td", a.user || "–"),
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
  $("ntp-result").textContent = "";
  if (!sites.length) return;
  // otevřená sekce lokality se načte znovu (data jiné lokality nesmí zůstat vidět)
  renderOverview(); renderDevices();
  if (SITE_VIEWS.includes(prefs.view)) { const t = subOf(prefs.view); if (t) { activateSub(prefs.view, t[0]); t[3](); } }
}

// NTP politika lokality (Nastavení → Systém a nasazení)
async function loadNtpPolicy() {
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
    msg(__("NTP politika uložena"));
  } catch (e) { msg(e.message, true); }
});

$("ntp-plan").addEventListener("click", async () => {
  try {
    const res = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/plans/ntp`);
    const out = $("ntp-result");
    out.textContent = "";
    const ul = el("ul");
    for (const r of res) {
      const what = r.result === "plan" ? r.summary : r.result === "ok" ? __("beze změn") : __`přeskočeno – ${r.reason}`;
      ul.appendChild(el("li", `${r.identity || r.serial}: ${what}`));
    }
    out.appendChild(ul);
    await approveAll(res.filter((r) => r.plan_id));
  } catch (e) { msg(e.message, true); }
});

// plány vytvořené tlačítkem „Nasadit“: správce je rovnou schválí (s automatickým návratem), technik je nechá ke schválení
async function approveAll(items) {
  if (!items.length) return 0;
  if (!canManage()) { msg(__`${items.length} ${items.length === 1 ? __("změna čeká") : __("změn čeká")} na schválení správcem (Aktivita → Změny)`); loadPlans(); return items.length; }
  const devices = [];
  for (const x of items) {
    const it = { device_id: x.device_id || x.id || String(x.plan_id), device: x.device || x.identity || x.serial, plan_id: x.plan_id };
    try { await api("POST", `/tenants/${tenantId}/plans/${x.plan_id}/approve`, {}); it.result = "applying"; }
    catch (e) { it.result = "draft"; it.reason = e.message; }
    devices.push(it);
  }
  showApply({ devices });
  return items.length;
}

const PLAN_MODULE = { ntp: "NTP", ports: "port", dhcp: "DHCP", takeover: __("převzetí do řízení"), switch: __("sladění se záměrem"), dhcp_snooping: "DHCP snooping",
  capsman: __("Wi-Fi (CAPsMAN)") };

async function loadPlans() {
  const list = await api("GET", `/tenants/${tenantId}/plans?status=draft`);
  const box = $("plans");
  box.textContent = "";
  plansOpen = list.length; activityBadge();
  if (!list.length) { box.appendChild(el("p", __("Žádné plány nečekají na schválení."), "muted")); return; }
  for (const p of list) {
    const div = el("div", null, "plan");
    div.appendChild(el("strong", `${p.identity || p.serial} (${p.site || "–"}) – ${(PLAN_MODULE[p.module] || p.module).toUpperCase()}`));
    div.appendChild(el("div", p.summary));
    div.appendChild(el("pre", p.script));
    const ok = el("button", __("Schválit a aplikovat"), "small");
    ok.onclick = async () => {
      if (!(await ask(__("Aplikovat plán na zařízení? Controller ho pošle hned (když zařízení přes tunel neodpoví, nejpozději při dalším check-inu do minuty).")))) return;
      try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/approve`); msg(__("Plán schválen – posílám na zařízení")); loadPlans(); watchDevice(p.device_id, p.identity || p.serial); }
      catch (e) { msg(e.message, true); }
    };
    const no = el("button", __("Zahodit"), "small secondary");
    no.onclick = async () => { try { await api("POST", `/tenants/${tenantId}/plans/${p.id}/discard`); loadPlans(); } catch (e) { msg(e.message, true); } };
    div.append(ok, " ", no);
    withCtx(div, __`Plán #${p.id} · ${p.identity || p.serial}`, () => [
      { label: __("Schválit a aplikovat"), act: () => ok.onclick() },
      { label: __("Zahodit"), danger: true, act: () => no.onclick() },
      { sep: true },
      { label: __("Otevřít zařízení"), act: () => openDetail(p.device_id) },
      { label: __("Kopírovat skript"), act: () => copyText(p.script, __("Skript")) },
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
    await loadWlMap();
    // spoj, který sousedé (LLDP/MNDP) neohlásili, se do topologie doplní z naměřených bezdrátových spojů
    const have = new Set(t.edges.map((e) => [e.a, e.b].sort().join("|"))), ids = new Set(t.nodes.map((n) => n.id));
    for (const [k, l] of Object.entries(infraWl)) if (!have.has(k) && ids.has(l.a.device_id) && ids.has(l.b.device_id))
      t.edges.push({ a: l.a.device_id, b: l.b.device_id, ports: { [l.a.device_id]: l.a.iface, [l.b.device_id]: l.b.iface } });
    drawTopology(t);
  } catch (e) { msg(e.message, true); }
}

// Topologie jako strom zleva doprava: každé zařízení má vlastní řádek, popisek je uvnitř rámečku
// a porty spoje jsou nad vodorovným úsekem u potomka – nic se nepřekrývá ani při desítkách sousedů.
const TOPO = { boxW: 190, boxH: 26, colW: 380, rowH: 36, pad: 16 };

function drawTopology(t) {
  const svg = $("topo");
  svg.textContent = "";
  if (!t.nodes.length) { svg.appendChild(svgEl("text", { x: 20, y: 30 }, __("Zatím žádná data o sousedech."))); return; }
  const byId = Object.fromEntries(t.nodes.map((n) => [n.id, n]));
  const adj = {};
  for (const n of t.nodes) adj[n.id] = [];
  for (const e of t.edges) { adj[e.a].push([e.b, e]); adj[e.b].push([e.a, e]); }
  const nat = (a, b) => String(a.label).localeCompare(String(b.label), LOCALE, { numeric: true });
  // kostra: BFS od řízeného zařízení s nejvíc spoji; nespojené části jako další stromy pod sebou
  const parent = {}, kids = {}, via = {}, seen = new Set(), roots = [];
  const starts = [...t.nodes].sort((x, y) => (y.managed ? 1 : 0) - (x.managed ? 1 : 0) || adj[y.id].length - adj[x.id].length || nat(x, y));
  for (const r of starts) {
    if (seen.has(r.id)) continue;
    roots.push(r.id); seen.add(r.id);
    const queue = [r.id];
    while (queue.length) {
      const cur = queue.shift();
      kids[cur] = [];
      const next = adj[cur].filter(([nb]) => !seen.has(nb)).sort((x, y) =>
        (byId[y[0]].managed ? 1 : 0) - (byId[x[0]].managed ? 1 : 0) || nat(byId[x[0]], byId[y[0]]));
      for (const [nb, e] of next) {
        if (seen.has(nb)) continue;
        seen.add(nb); parent[nb] = cur; via[nb] = e; kids[cur].push(nb); queue.push(nb);
      }
    }
  }
  // rozmístění: list = další volný řádek, rodič uprostřed svých potomků
  const pos = {};
  let row = 0, depthMax = 0;
  const place = (id, depth) => {
    depthMax = Math.max(depthMax, depth);
    if (!kids[id].length) { pos[id] = { d: depth, r: row++ }; return; }
    for (const k of kids[id]) place(k, depth + 1);
    pos[id] = { d: depth, r: (pos[kids[id][0]].r + pos[kids[id][kids[id].length - 1]].r) / 2 };
  };
  for (const r of roots) { place(r, 0); row += 0.5; }
  const X = (id) => TOPO.pad + pos[id].d * TOPO.colW, Y = (id) => TOPO.pad + pos[id].r * TOPO.rowH + TOPO.boxH / 2;
  const W = TOPO.pad * 2 + depthMax * TOPO.colW + TOPO.boxW, H = TOPO.pad * 2 + Math.max(1, row - 0.5) * TOPO.rowH;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  const short = (v, n) => (String(v).length > n ? String(v).slice(0, n - 1) + "…" : String(v));
  // spoje kostry: z pravé hrany rodiče po společné svislici k levé hraně potomka
  for (const id of Object.keys(parent)) {
    const p = parent[id], e = via[id];
    const x1 = X(p) + TOPO.boxW, y1 = Y(p), x2 = X(id), y2 = Y(id), bus = x1 + 22;
    svg.appendChild(svgEl("path", { d: `M${x1} ${y1}H${bus}V${y2}H${x2}`, class: "link" }));
    // výpadek jen na úseku k potomkovi – společná svislice patří i ostatním
    if (byId[id].state === "offline" || byId[p].state === "offline") svg.appendChild(svgEl("path", { d: `M${bus} ${y2}H${x2}`, class: "link down" }));
    const wlk = wlOf(p, id);
    if (wlk) {
      // bezdrátový spoj: úsek k potomkovi tečkovaně v barvě stavu, značka ≋, klik otevře kartu spoje
      svg.appendChild(svgEl("path", { d: `M${bus} ${y2}H${x2}`, class: "link wless s-" + wlk.state }));
      svg.appendChild(svgEl("text", { x: bus + 12, y: y2 + 15, class: "wl-mark s-" + wlk.state }, "≋"));
      const hit = svgEl("path", { d: `M${bus} ${y2}H${x2}`, class: "link hit" });
      hit.appendChild(svgEl("title", {}, wlTitle(wlk)));
      hit.addEventListener("click", () => openLink(wlk.id));
      svg.appendChild(hit);
    }
    const pp = e.ports[p], pc = e.ports[id];
    const label = [pp, pc].filter(Boolean).map((v) => short(v, 16)).join(" → ");
    if (label) {
      const tx = svgEl("text", { x: x2 - 8, y: y2 - 5, class: "port", "text-anchor": "end" }, label);
      tx.appendChild(svgEl("title", {}, `${byId[p].label} ${pp || "?"} → ${byId[id].label} ${pc || "?"}`));
      svg.appendChild(tx);
    }
  }
  // ostatní spoje (smyčky, záložní linky): čárkovaně obloukem
  for (const e of t.edges) {
    if (via[e.a] === e || via[e.b] === e || !pos[e.a] || !pos[e.b]) continue;
    const xa = X(e.a) + TOPO.boxW, ya = Y(e.a), xb = X(e.b) + TOPO.boxW, yb = Y(e.b), out = Math.max(xa, xb) + 40;
    const wx = wlOf(e.a, e.b);
    const ln = svgEl("path", { d: `M${xa} ${ya}C${out} ${ya} ${out} ${yb} ${xb} ${yb}`, class: wx ? "link wless s-" + wx.state : "link extra" });
    ln.appendChild(svgEl("title", {}, wx ? wlTitle(wx) : __`další spoj: ${byId[e.a].label} ${e.ports[e.a] || ""} ↔ ${byId[e.b].label} ${e.ports[e.b] || ""}`));
    if (wx) { ln.addEventListener("click", () => openLink(wx.id)); ln.setAttribute("class", "link wless clickable s-" + wx.state); }
    svg.appendChild(ln);
  }
  for (const n of t.nodes) {
    const x = X(n.id), y = Y(n.id) - TOPO.boxH / 2;
    const cls = !n.managed ? "ext" : n.state === "offline" ? "offline" : "managed";
    const g = svgEl("g", { class: "node " + cls });
    g.appendChild(svgEl("rect", { x, y, width: TOPO.boxW, height: TOPO.boxH, rx: 7 }));
    g.appendChild(svgEl("circle", { cx: x + 12, cy: y + TOPO.boxH / 2, r: 4 }));
    g.appendChild(svgEl("text", { x: x + 23, y: y + TOPO.boxH / 2 + 4 }, short(n.label, 24)));
    g.appendChild(svgEl("title", {}, [n.label, n.model, n.address, n.managed ? (n.state === "offline" ? "offline" : "online") : __("nespravované")]
      .filter(Boolean).join(" – ")));
    if (n.managed) g.addEventListener("click", () => openDetail(n.id));
    svg.appendChild(g);
  }
}

const SEV_LABEL = { critical: __("kritické"), warning: __("varování"), info: "info" };

async function loadAlerts() {
  const list = await api("GET", `/tenants/${tenantId}/alerts`);
  alertsCache = list;
  const tb = $("alerts");
  tb.textContent = "";
  const crit = list.filter((a) => a.severity === "critical").length;
  $("alerts-count").textContent = list.length ? `${list.length}${crit ? __` (${crit} kritických)` : ""}` : __("žádné");
  alertsOpen = [list.length, crit]; activityBadge();
  loadMaint();
  for (const a of list) {
    const tr = el("tr");
    tr.append(el("td", SEV_LABEL[a.severity] || a.severity, "sev-" + (a.severity === "critical" ? "error" : a.severity)),
              el("td", a.identity || a.serial), el("td", a.site || "–"), el("td", a.message, "wrap"), el("td", fmtTime(a.opened_at)));
    const act = el("td");
    if (a.acknowledged_at) act.appendChild(el("span", __`potvrdil ${a.acknowledged_by || ""}`, "muted"));
    else {
      const b = el("button", __("Potvrdit"), "small secondary");
      b.onclick = async () => { try { await api("POST", `/tenants/${tenantId}/alerts/${a.id}/ack`); loadAlerts(); } catch (e) { msg(e.message, true); } };
      act.appendChild(b);
    }
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  if (ovDrill) renderOverview();
}
$("alerts-refresh").addEventListener("click", () => loadAlerts());

// --- servisní okno lokality (upozornění se během něj neposílají) -----------------------------
let maintData = null;
// konec okna je v budoucnu → vždy konkrétní čas, ne „před X min“
const maintTime = (iso) => new Date(iso).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });

async function loadMaint() {
  const bar = $("maint-bar"); bar.textContent = ""; bar.hidden = true;
  $("maint-btn").hidden = !siteId() || !canOperate();
  if (!siteId()) return;
  try { maintData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/maintenance`); } catch (e) { return; }
  const a = maintData.active;
  if (!a && !maintData.upgrade) return;
  bar.hidden = false;
  bar.appendChild(el("span", a ? __`Servisní okno do ${maintTime(a.to)}${a.note ? ` – ${a.note}` : ""}. Upozornění z lokality se neposílají.`
                               : __("Běží postupná aktualizace – upozornění z lokality se zatím neposílají.")));
  if (a && canOperate()) {
    const b = el("button", __("Ukončit"), "small secondary"); b.type = "button";
    b.onclick = () => endMaint(a.id);
    bar.appendChild(b);
  }
}

async function endMaint(id) {
  try { await api("DELETE", `/tenants/${tenantId}/sites/${siteId()}/maintenance/${id}`); msg(__("Servisní okno ukončeno")); loadMaint(); renderMaintList(); }
  catch (e) { msg(e.message, true); }
}

function renderMaintList() {
  const ul = $("maint-list"); ul.textContent = "";
  for (const w of (maintData && maintData.windows) || []) {
    const li = el("li", `${w.active ? __("běží") : "od " + maintTime(w.from)} do ${maintTime(w.to)}${w.note ? ` – ${w.note}` : ""} `);
    const b = el("button", w.active ? __("Ukončit") : __("Zrušit"), "small secondary"); b.type = "button";
    b.onclick = async () => { await endMaint(w.id); await loadMaint(); renderMaintList(); };
    li.appendChild(b); ul.appendChild(li);
  }
}

$("maint-btn").addEventListener("click", () => { $("maint-dlg").hidden = false; renderMaintList(); });
$("maint-close").addEventListener("click", () => { $("maint-dlg").hidden = true; });
$("maint-start").addEventListener("click", async () => {
  try {
    maintData = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/maintenance`,
                          { minutes: Number($("maint-len").value), note: $("maint-note").value.trim() });
    $("maint-dlg").hidden = true; $("maint-note").value = "";
    msg(__("Servisní okno zahájeno – upozornění z lokality se neposílají")); loadMaint();
  } catch (e) { msg(e.message, true); }
});

let clientsCache = [];
// stav pohledu Klienti: typ připojení, období osy, vybraný okamžik (null = teď)
const clView = { kind: "all", hours: 24, at: null, timeline: null };
const clIsWifi = (c) => !!(c.wifi || c.ssid || c.signal || c.was_wifi);
const clClock = (t) => new Date(t).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" });

async function loadClients() {
  if (clView.site !== siteId()) { clView.site = siteId(); clView.timeline = null; clView.at = null; }
  try {
    const q = clView.at ? `?at=${encodeURIComponent(clView.at)}` : $("clients-history").checked ? "?history=1" : "";
    clientsCache = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/clients${q}`);
    clFilterOptions();
    renderClients();
  } catch (e) { msg(e.message, true); }
  if (!clView.timeline) loadClientTimeline();
}
$("clients-history").addEventListener("change", () => loadClients());

async function loadClientTimeline() {
  try { clView.timeline = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/clients/timeline?hours=${clView.hours}`); }
  catch (e) { clView.timeline = null; }
  const tl = clView.timeline, sl = $("cl-slider");
  const n = tl ? tl.points.length : 0;
  sl.max = String(Math.max(0, n - 1));
  if (!clView.at) sl.value = sl.max;
  $("cl-time-note").textContent = !tl || !tl.since ? __("Historie připojení se teprve začíná zaznamenávat (každých 5 minut).")
    : new Date(tl.since) > new Date(tl.from) ? __`Historie připojení se zaznamenává od ${clClock(tl.since)}.` : "";
  drawClientChart();
  clTimeLabel();
}

// sloupcový graf počtu klientů: Wi-Fi tmavší část, kabel světlejší; klik = skok na okamžik
function drawClientChart() {
  const box = $("cl-chart"); box.textContent = "";
  const tl = clView.timeline;
  if (!tl || !tl.points.length || typeof document.createElementNS !== "function") return;
  const W = 1000, H = 120, n = tl.points.length, bw = W / n;
  const max = Math.max(1, ...tl.points.map((p) => p.total));
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });
  const sel = Number($("cl-slider").value);
  tl.points.forEach((p, i) => {
    const wifi = clView.kind === "wired" ? 0 : p.wifi, wired = clView.kind === "wifi" ? 0 : p.total - p.wifi;
    const h1 = (wifi / max) * (H - 6), h2 = (wired / max) * (H - 6), x = i * bw;
    const g = svgEl("g", { class: "bar" + (clView.at && i === sel ? " sel" : "") });
    g.appendChild(svgEl("rect", { x, y: 0, width: bw, height: H, class: "hit" }));
    if (h2) g.appendChild(svgEl("rect", { x: x + bw * 0.12, y: H - h1 - h2, width: bw * 0.76, height: h2, class: "wired" }));
    if (h1) g.appendChild(svgEl("rect", { x: x + bw * 0.12, y: H - h1, width: bw * 0.76, height: h1, class: "wifi" }));
    g.appendChild(svgEl("title", {}, __`${clClock(p.t)} – ${p.total} klientů (${p.wifi} Wi-Fi, ${p.total - p.wifi} kabel)`));
    g.addEventListener("click", () => { $("cl-slider").value = String(i); clSeek(); });
    svg.appendChild(g);
  });
  box.appendChild(svg);
}

function clTimeLabel() {
  const tl = clView.timeline, i = Number($("cl-slider").value);
  const p = tl && tl.points[i];
  $("cl-now").hidden = !clView.at;
  $("cl-time-label").textContent = clView.at ? clClock(clView.at) : __("Teď");
  $("cl-time-sum").textContent = !p ? "" : clView.at
    ? __`připojeno ${p.total} klientů – ${p.wifi} Wi-Fi, ${p.total - p.wifi} kabel`
    : __`nejvíc za období: ${Math.max(...tl.points.map((x) => x.total))} klientů`;
  $("clients-history").disabled = !!clView.at;
}

let clSeekTimer = null;
function clSeek() {
  const tl = clView.timeline, i = Number($("cl-slider").value);
  if (!tl || !tl.points.length) return;
  clView.at = i >= tl.points.length - 1 ? null : tl.points[i].t;   // úplně vpravo = teď
  clTimeLabel(); drawClientChart();
  clearTimeout(clSeekTimer);
  clSeekTimer = setTimeout(() => loadClients(), 250);
}
$("cl-slider").addEventListener("input", clSeek);
$("cl-now").addEventListener("click", () => { $("cl-slider").value = $("cl-slider").max; clSeek(); });
for (const h of [24, 168, 720]) {
  $("cl-range-" + h).addEventListener("click", () => {
    clView.hours = h; clView.at = null;
    for (const x of [24, 168, 720]) $("cl-range-" + x).className = x === h ? "active" : "";
    loadClientTimeline(); loadClients();
  });
}
for (const k of ["all", "wifi", "wired"]) {
  $("cl-kind-" + k).addEventListener("click", () => {
    clView.kind = k;
    for (const x of ["all", "wifi", "wired"]) $("cl-kind-" + x).className = x === k ? "active" : "";
    drawClientChart(); renderClients();
  });
}

// filtry klientů v levém panelu: kam je klient připojený, Wi-Fi síť, VLAN, výrobce (s počty)
const CL_FACETS = [
  ["state", __("Stav"), (c) => c.online === false ? "off" : "on", (v) => v === "on" ? __("Online") : __("Offline")],
  ["dev", __("Připojeno k"), (c) => c.device || null],
  ["ssid", __("Wi-Fi síť"), (c) => (c.wifi ? c.wifi.ssid : c.ssid) || null],
  ["vlan", __("Síť (VLAN)"), (c) => c.vlan ? String(c.vlan) : null, (v) => "VLAN " + v],
  ["vendor", __("Výrobce"), (c) => c.vendor || null]];
clView.f = {};
const clKindOk = (c) => clView.kind === "all" || (clView.kind === "wifi") === clIsWifi(c);
function clFilterOptions() {
  facetGroups($("cl-facets"), "cl", clientsCache, CL_FACETS, clView.f, () => renderClients(), clKindOk);
  $("cl-clear").disabled = !facetAny(clView.f) && !$("clients-filter").value;
}
$("cl-clear").addEventListener("click", () => { clView.f = {}; $("clients-filter").value = ""; renderClients(); });

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
                              (c.online ? __(" · připojen") : __` · naposledy ${fmtTime(c.last_seen)}`), c.online ? "" : "muted"));
    }
    if (!list.length) ul.appendChild(el("li", __("Nic nenalezeno (historie se plní každých 5 min)"), "muted"));
  }, 300);
});
$("clients-filter").addEventListener("input", () => renderClients());

// --- klienti: popisek, ikona, logo (výrobce nebo vlastní) -------------------------------------------
const CLIENT_ICONS = { phone: "📱", tablet: "📲", laptop: "💻", desktop: "🖥️", printer: "🖨️", camera: "📷", tv: "📺",
  nas: "🗄️", server: "🧮", iot: "🔌", voip: "☎️", speaker: "🔊", game: "🎮", watch: "⌚", car: "🚗", ap: "📡",
  router: "🛜", switch: "🔀", light: "💡", sensor: "🌡️", lock: "🔒", other: "❔" };
const ICON_LABEL = { phone: __("telefon"), tablet: "tablet", laptop: "notebook", desktop: __("počítač"), printer: __("tiskárna"), camera: __("kamera"),
  tv: "TV", nas: "NAS", server: "server", iot: "IoT", voip: __("IP telefon"), speaker: __("reproduktor"), game: __("konzole"), watch: __("hodinky"),
  car: "auto", ap: "AP", router: "router", switch: "switch", light: __("světlo"), sensor: __("senzor"), lock: __("zámek"), other: __("jiné") };

const logoUrl = (ref) => `/api/admin/tenants/${tenantId}/client-logos/${ref}`;
function vendorInitials(v) {
  const w = String(v || "").replace(/[,.].*$/, "").split(/\s+/).filter(Boolean);
  return (w.length > 1 ? w[0][0] + w[1][0] : (w[0] || "?").slice(0, 2)).toUpperCase();
}
function vendorHue(v) { let h = 0; for (const ch of String(v || "")) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; }
// avatar klienta: logo klienta → logo výrobce → ikona → iniciály výrobce
function clientAvatar(c, size) {
  const a = el("span", null, "cav" + (size ? " " + size : ""));
  if (c.logo) {
    const img = el("img"); img.src = logoUrl(c.logo); img.alt = ""; img.loading = "lazy"; a.appendChild(img);
  } else if (c.icon && CLIENT_ICONS[c.icon]) {
    a.textContent = CLIENT_ICONS[c.icon]; a.className += " ico";
  } else {
    a.textContent = c.vendor && !/náhodná|random/i.test(c.vendor) ? vendorInitials(c.vendor) : "?";
    a.className += " ini"; a.style.setProperty && a.style.setProperty("--h", vendorHue(c.vendor_key || c.vendor));
  }
  a.title = [c.vendor, c.icon ? ICON_LABEL[c.icon] : ""].filter(Boolean).join(" · ");
  return a;
}
const clientName = (c) => c.label || c.hostname || c.ip || c.mac;

function clientMenu(c) {
  return [
    { label: __("Upravit popisek, ikonu a logo…"), key: "Enter", act: () => openClient(c) },
    { label: __("Statická IP a DHCP options…"), disabled: !canOperate(), act: () => clientDhcp(c) },
    { sep: true },
    { label: __("Kopírovat MAC"), act: () => copyText(c.mac, "MAC") },
    c.ip ? { label: __("Kopírovat IP"), act: () => copyText(c.ip, "IP") } : null,
    c.device_id ? { label: __`Otevřít ${c.device || __("zařízení")}`, act: () => openDetail(c.device_id) } : null,
    { sep: true },
    { label: __("Wi-Fi časová osa"), act: () => openJourney(c.mac) },
  ];
}

// síla signálu: čtyři dílky jako na telefonu
function signalBars(dbm) {
  const v = Number(dbm), lvl = !isFinite(v) ? 0 : v >= -60 ? 4 : v >= -67 ? 3 : v >= -75 ? 2 : 1;
  const w = el("span", null, "sig sig-" + lvl);
  for (let i = 1; i <= 4; i++) w.appendChild(el("i", null, i <= lvl ? "on" : ""));
  w.title = `${dbm} dBm`;
  return w;
}

function renderClients() {
  const q = $("clients-filter").value.trim().toLowerCase();
  clFilterOptions();
  const tb = $("clients");
  tb.textContent = "";
  const list = clientsCache.filter((c) => {
    const wifi = clIsWifi(c), ssid = c.wifi ? c.wifi.ssid : c.ssid;
    if (clView.kind === "wifi" && !wifi) return false;
    if (clView.kind === "wired" && wifi) return false;
    if (!facetOk(CL_FACETS, clView.f, c)) return false;
    return !q || [c.label, c.hostname, c.ip, c.mac, c.device, c.port, c.vlan, ssid, c.vendor, c.note]
      .some((v) => v && String(v).toLowerCase().includes(q));
  });
  const nWifi = list.filter(clIsWifi).length;
  $("cl-count").textContent = `${list.length}${list.length !== clientsCache.length ? ` z ${clientsCache.length}` : ""}` +
    (clView.kind === "all" && list.length ? __` · ${nWifi} Wi-Fi · ${list.length - nWifi} kabel` : "");
  for (const c of list.slice(0, 1000)) {
    const tr = el("tr", null, "clickable" + (c.online === false ? " muted" : ""));
    const ssid = c.wifi ? c.wifi.ssid : c.ssid, signal = c.wifi ? c.wifi.signal : c.signal;
    const av = el("td", null, "cav-td"); av.appendChild(clientAvatar(c));
    const name = el("td");
    name.appendChild(el("b", clientName(c)));
    if (isNew(c)) name.appendChild(el("span", __(" nový"), "badge"));
    if (c.label && c.hostname && c.label !== c.hostname) name.appendChild(el("div", c.hostname, "muted small"));
    if (c.note) name.appendChild(el("div", c.note, "muted small"));
    // připojení: Wi-Fi = AP a síť, kabel = switch a port
    const conn = el("td");
    const wifi = clIsWifi(c);
    conn.appendChild(el("span", wifi ? __("Wi-Fi") : __("Kabel"), "cl-kind " + (wifi ? "wifi" : "wired")));
    conn.appendChild(document.createTextNode(" " + (c.device || "?")));
    conn.appendChild(el("div", wifi ? (ssid || "") : (c.port || "") + (c.uncertain ? " ?" : ""), "muted small"));
    const sig = el("td");
    if (signal) { sig.appendChild(signalBars(signal)); sig.appendChild(el("span", ` ${signal} dBm`, "muted small")); }
    const macTd = el("td", c.mac, "mono");
    if (c.vendor) macTd.appendChild(el("div", c.vendor, "muted small"));
    const more = el("td"); more.appendChild(moreButton(clientName(c), () => clientMenu(c)));
    const last = clView.at ? (c.ended_at ? `do ${clClock(c.ended_at)}` : __("stále připojen")) : c.online ? __("teď") : fmtTime(c.last_seen);
    tr.append(av, name, conn, el("td", c.ip || ""), el("td", c.vlan ? `VLAN ${c.vlan}` : ""), sig, macTd,
              el("td", c.first_seen ? fmtTime(c.first_seen) : "–"), el("td", last), more);
    tr.addEventListener("click", (ev) => { if (ev.target.tagName !== "BUTTON") openClient(c); });
    withCtx(tr, clientName(c), () => clientMenu(c));
    tb.appendChild(tr);
  }
  if (!list.length) {
    const tr = el("tr");
    const td = el("td", clientsCache.length ? __("Filtru nic neodpovídá") : clView.at ? __("V tu dobu nebyl připojený žádný klient (nebo se historie ještě nezaznamenávala)")
      : __("Žádní klienti (inventura klientů běží každých 5 min)"), "muted");
    td.colSpan = 10; tr.appendChild(td); tb.appendChild(tr);
  }
}

// --- editor klienta ---
let clEdit = null;   // {c, icon, upload: "client"|"vendor"}
function openClient(c) {
  clEdit = { c, icon: c.icon || "" };
  $("cl-title").textContent = clientName(c);
  $("cl-sub").textContent = `${c.mac}${c.vendor ? " · " + c.vendor : ""}`;
  $("cl-avatar").textContent = ""; $("cl-avatar").appendChild(clientAvatar(c, "big"));
  $("cl-label").value = c.label || ""; $("cl-label").placeholder = c.hostname || __("např. Tiskárna 2. patro");
  $("cl-note").value = c.note || "";
  $("cl-addr").textContent = [c.ip ? `IP ${c.ip}` : __("IP zatím neznámá"), c.vlan ? `VLAN ${c.vlan}` : "",
    c.device ? `${c.device}${c.port ? " · " + c.port : ""}` : ""].filter(Boolean).join(" · ");
  $("cl-logo-vendor").hidden = !c.vendor_key;
  $("cl-logo-vendor").textContent = c.vendor_key ? __`Nahrát logo výrobce (${c.vendor.replace(/[,.].*$/, "")})…` : "";
  $("cl-logo-del").hidden = !c.logo;
  $("cl-warn").textContent = "";
  for (const id of ["cl-save", "cl-logo-client", "cl-logo-vendor", "cl-logo-del", "cl-dhcp", "cl-fix"]) $(id).disabled = !canOperate();
  $("cl-fix").hidden = !c.ip; $("cl-fix-res").textContent = "";
  $("cl-fix").textContent = c.ip ? __`Pevná IP ${c.ip} (rezervace v DHCP)` : __("Pevná IP");
  clIcons();
  $("clt-jr").hidden = !(c.wifi || c.ssid || c.signal);
  clTab("edit");
  $("cl-dlg").hidden = false;
  try { $("cl-label").focus(); } catch (_e) { /* fokus není podstatný */ }
}
function clIcons() {
  const g = $("cl-icons"); g.textContent = "";
  const none = el("button", "auto", "icon-pick" + (clEdit.icon ? "" : " on")); none.type = "button"; none.title = __("podle výrobce / loga");
  none.addEventListener("click", () => { clEdit.icon = ""; clIcons(); });
  g.appendChild(none);
  for (const [k, e] of Object.entries(CLIENT_ICONS)) {
    const b = el("button", e, "icon-pick" + (clEdit.icon === k ? " on" : "")); b.type = "button"; b.title = ICON_LABEL[k];
    b.setAttribute("aria-label", ICON_LABEL[k]);
    b.addEventListener("click", () => { clEdit.icon = k; clIcons(); });
    g.appendChild(b);
  }
}
function clClose() { $("cl-dlg").hidden = true; clEdit = null; }
// panel klienta: nastavení / Wi-Fi časová osa
function clTab(key) {
  $("clp-edit").hidden = key !== "edit"; $("clp-jr").hidden = key !== "jr";
  $("clt-edit").className = key === "edit" ? "active" : ""; $("clt-jr").className = key === "jr" ? "active" : "";
}
$("clt-edit").addEventListener("click", () => clTab("edit"));
$("clt-jr").addEventListener("click", () => { if (clEdit) openJourney(clEdit.c.mac); });
// pevná IP na jeden klik: rezervace aktuální adresy na DHCP serveru, který klienta obsluhuje
$("cl-fix").addEventListener("click", async () => {
  const c = clEdit && clEdit.c; if (!c) return;
  const res = $("cl-fix-res"); res.className = "muted small"; res.textContent = __("Hledám DHCP server…");
  try {
    const r = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/clients/${encodeURIComponent(c.mac)}/dhcp`);
    const out = await api("POST", `/tenants/${tenantId}/devices/${r.device_id}/dhcp/${encodeURIComponent(r.server)}/lease`,
                          { mac: c.mac, desired: { address: c.ip }, apply: canManage() });
    res.textContent = (out.command_id ? __`Rezervace ${c.ip} na ${r.device} se nastavuje (automatický návrat do 5 min při ztrátě spojení)`
                                      : __("Rezervace uložena jako změna ke schválení")) +
      (out.warnings && out.warnings.length ? " · " + out.warnings.join(" · ") : "");
  } catch (e) { res.textContent = e.message; res.className = "sev-error small"; }
});
$("cl-close").addEventListener("click", clClose);
$("cl-cancel").addEventListener("click", clClose);
$("cl-save").addEventListener("click", async () => {
  const c = clEdit.c;
  try {
    await api("PUT", `/tenants/${tenantId}/client-notes/${encodeURIComponent(c.mac)}`,
              { label: $("cl-label").value, note: $("cl-note").value, icon: clEdit.icon });
    msg(__`${c.mac}: uloženo`); clClose(); reloadClientViews();
  } catch (e) { $("cl-warn").textContent = ""; $("cl-warn").appendChild(el("li", e.message, "sev-error")); }
});
$("cl-dhcp").addEventListener("click", () => { const c = clEdit.c; clClose(); clientDhcp(c); });
$("cl-logo-client").addEventListener("click", () => { clEdit.upload = "client"; $("cl-file").click(); });
$("cl-logo-vendor").addEventListener("click", () => { clEdit.upload = "vendor"; $("cl-file").click(); });
$("cl-logo-del").addEventListener("click", async () => {
  const c = clEdit.c, ref = String(c.logo || "").split("?")[0];
  if (!ref) return;
  const isVendor = ref.startsWith("vendor/");
  if (!(await ask(isVendor ? __`Odebrat logo výrobce ${c.vendor}? Zmizí u všech jeho klientů.` : __("Odebrat logo tohoto klienta?"), __("Odebrat")))) return;
  try { await api("DELETE", logoUrl(ref).replace("/api/admin", "")); msg(__("Logo odebráno")); clClose(); reloadClientViews(); }
  catch (e) { msg(e.message, true); }
});
$("cl-file").addEventListener("change", async () => {
  const f = $("cl-file").files && $("cl-file").files[0];
  $("cl-file").value = "";
  if (!f || !clEdit) return;
  const c = clEdit.c, scope = clEdit.upload;
  try {
    const data = await shrinkImage(f);
    const key = scope === "client" ? c.mac : c.vendor;
    await api("PUT", `/tenants/${tenantId}/client-logos/${scope}/${encodeURIComponent(key)}`, { data });
    msg(scope === "vendor" ? __`Logo výrobce ${c.vendor} nahráno` : __("Logo klienta nahráno"));
    clClose(); reloadClientViews();
  } catch (e) { msg(e.message, true); }
});
// obrázek zmenšit na max. 128 px (PNG, nebo WebP když by byl PNG velký) – na server jde base64 bez hlavičky
function shrinkImage(file) {
  return new Promise((resolve, reject) => {
    if (file.size > 5 * 1024 * 1024) return reject(new Error(__("obrázek je moc velký (max. 5 MB)")));
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, 128 / Math.max(img.width, img.height));
      const cv = document.createElement("canvas");
      cv.width = Math.max(1, Math.round(img.width * k)); cv.height = Math.max(1, Math.round(img.height * k));
      cv.getContext("2d").drawImage(img, 0, 0, cv.width, cv.height);
      URL.revokeObjectURL(url);
      let out = cv.toDataURL("image/png");
      if (out.length > 80000) out = cv.toDataURL("image/webp", 0.85);
      resolve(out.split(",")[1]);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error(__("soubor není obrázek"))); };
    img.src = url;
  });
}
function reloadClientViews() {
  if (!$("view-clients").hidden) loadClients();
  if (!$("view-dash").hidden && !$("infra-wrap").hidden) loadInfra();
}

// statická IP a DHCP options klienta: najít jeho DHCP server a otevřít editor leasu na záložce Sítě
async function clientDhcp(c) {
  let r;
  try { r = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/clients/${encodeURIComponent(c.mac)}/dhcp`); }
  catch (e) { return msg(e.message, true); }
  showView("settings"); activateSub("settings", "net");
  await loadNetworks();
  await loadLeases(r.device_id, r.server);
  if (!leaseCtx) return;
  $("lease-q").value = c.mac;
  leaseCtx.filter = "all";
  leaseCtx.edit = leaseCtx.v.leases.some((x) => x.mac === c.mac) ? { mac: c.mac } : { mac: "", ip: leaseCtx.v.suggest, presetMac: c.mac };
  renderLeases();
  try { $("lease-wrap").scrollIntoView({ behavior: "smooth", block: "start" }); } catch (_e) { /* jen posun */ }
}

// --- Wi-Fi lokality --------------------------------------------------------------------

async function loadWifi() {
  try { renderWifi(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi`)); } catch (e) { msg(e.message, true); }
  loadRoaming();
}

// CAPsMAN lokality: kde běží a jak ho nastavit (výběr z řízených zařízení lokality)
async function renderCapsmanCard() {
  const box = $("capsman-card"); box.textContent = "";
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/intent`); } catch (_e) { return; }
  const host = v.intent && v.intent.wifi && v.intent.wifi.capsman_device_id;
  const hd = host && v.devices.find((d) => d.id === host);
  const managed = v.devices.filter((d) => d.managed);
  const t = el("div", null, "cm-text");
  if (hd) t.append(el("b", "📡 CAPsMAN lokality: " + hd.name), el("span", __(" · rádia AP řídí odsud; SSID a nasazení v Nastavení lokality → Wi-Fi"), "muted"));
  else if (!v.intent) t.append(el("b", __("CAPsMAN zatím není")), el("span", __(" – nejdřív nadefinuj sítě lokality (Nastavení lokality → Sítě)."), "muted"));
  else if (!managed.length) t.append(el("b", __("CAPsMAN zatím není")), el("span", __(" – může běžet jen na řízeném zařízení. Převezmi switch do řízení (detail → Řízení) nebo ho nasaď s výchozí konfigurací „switch + CAPsMAN“."), "muted"));
  else t.append(el("b", __("CAPsMAN zatím není")), el("span", __(" – vyber řízené zařízení, nejlépe páteřní switch."), "muted"));
  box.appendChild(t);
  if (v.intent && managed.length && canManage()) {
    const sel = el("select");
    const o0 = el("option", hd ? __("— vypnout CAPsMAN —") : __("— vyber zařízení —")); o0.value = ""; sel.appendChild(o0);
    for (const d of managed) { const o = el("option", `${d.name}${d.model ? " (" + d.model + ")" : ""}`); o.value = d.id; sel.appendChild(o); }
    sel.value = host || "";
    const b = el("button", hd ? __("Změnit") : __("Nastavit jako CAPsMAN"), "small"); b.type = "button";
    b.addEventListener("click", () => setCapsmanHost(sel.value || null));
    box.append(sel, b);
  }
}

async function setCapsmanHost(deviceId, siteOverride) {
  const sid = siteOverride || siteId();
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/sites/${sid}/intent`); } catch (e) { return msg(e.message, true); }
  if (!v.intent) return msg(__("Lokalita nemá záměr – nejdřív nadefinuj sítě (Nastavení lokality → Sítě)"), true);
  const d = deviceId && v.devices.find((x) => x.id === deviceId);
  if (deviceId && (!d || !d.managed)) return msg(__("CAPsMAN může běžet jen na řízeném zařízení – nejdřív ho převezmi (detail → Řízení)"), true);
  const text = deviceId ? __`Nastavit ${d.name} jako CAPsMAN lokality?\n\nRádia všech AP s rolí „AP + switch“ pak řídí tento CAPsMAN. ` +
    __("Na zařízení se nic nezmění, dokud v Nastavení lokality → Wi-Fi nedáš „Nasadit Wi-Fi“.") : __("Vypnout CAPsMAN v záměru lokality?");
  if (!(await ask(text, deviceId ? __("Nastavit") : __("Vypnout")))) return;
  const wifi = { ...(v.intent.wifi || {}), capsman_device_id: deviceId || null };
  try {
    const r = await api("PUT", `/tenants/${tenantId}/sites/${sid}/intent`, { ...v.intent, wifi });
    msg(deviceId ? __`${d.name} je CAPsMAN lokality` : __("CAPsMAN vypnut v záměru"));
    showApply(r.apply);
    if (!$("view-settings").hidden && !$("wifiset-wrap").hidden) { renderCapsmanCard(); loadIntent(); }
  } catch (e) { msg(e.message, true); }
}

// cizí a podvržená AP ze skenu rádií (Air Marshal)
// --- optimalizace kanálů a výkonu podle měření okolí ----------------------------------------
let rfoptData = null;

async function loadRfopt() {
  const box = $("rfopt-card"); box.textContent = ""; box.hidden = true;
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi/optimize`); } catch (e) { return; }
  rfoptData = v;
  box.hidden = false;
  const head = el("div", null, "rogue-head");
  head.appendChild(el("h3", __("Kanály a výkon")));
  if (canOperate()) {
    const b = el("button", __("Optimalizovat kanály"), "small primary"); b.type = "button";
    b.onclick = openRfopt;
    head.appendChild(b);
  }
  box.appendChild(head);
  const scanned = v.aps.filter((a) => a.scanned_at).length;
  box.appendChild(el("p", v.pinned
    ? __`Kanály jsou nastavené podle měření okolí z ${fmtTime(v.pinned.at)}.`
    : __`Kanály se rozdělují jen podle počtu AP. Měření okolí má ${scanned} z ${v.aps.length} AP` +
      (v.changes && scanned ? __` – optimalizace navrhuje ${v.changes} ${v.changes === 1 ? __("změnu") : v.changes < 5 ? __("změny") : __("změn")}.` : "."),
    "muted small"));
}

function renderRfopt() {
  const v = rfoptData;
  if (!v) return;
  const scanned = v.aps.filter((a) => a.scanned_at).length;
  $("rfopt-scan-state").textContent = __`Měření má ${scanned} z ${v.aps.length} AP` + (v.missing.length ? __` (chybí: ${v.missing.join(", ")})` : "");
  $("rfopt-sum").textContent = scanned
    ? __`Rušení ${v.before} → ${v.after} · ${v.changes ? `${v.changes} ${v.changes === 1 ? __("změna kanálu") : v.changes < 5 ? __("změny kanálu") : __("změn kanálu")}` : __("kanály beze změny")}`
    : __("Zatím bez měření – spusť „Změřit okolí“.");
  const tb = $("rfopt-rows"); tb.textContent = "";
  for (const i of v.items) {
    const tr = el("tr", null, i.change ? "" : "muted");
    const now = i.now.freq ? `${i.now.channel} / ${i.now.width} MHz` : "?";
    const tx = el("td", i.tx_power != null ? `${i.tx_power} dBm` : "max");
    tx.title = i.tx_reason || "";
    if (/výkon −/.test(i.tx_reason || "")) tx.className = "plan-new";
    const fr = el("td", String(i.foreign));
    if (i.strongest) fr.title = __`nejsilnější: ${i.strongest.ssid || __("(skryté)")} ${i.strongest.signal} dBm, kanál ${i.strongest.channel}`;
    tr.append(el("td", `${i.device || "?"}${i.scanned ? "" : __(" (bez měření)")}`), el("td", i.band === "5" ? "5 GHz" : "2,4 GHz"),
              el("td", now), el("td", `${i.new.channel} / ${i.new.width} MHz${i.new.dfs ? " · DFS" : ""}`, i.change ? "plan-new" : ""),
              el("td", `${i.now.cost != null ? i.now.cost : "?"} → ${i.new.cost}`), fr, tx);
    tb.appendChild(tr);
  }
  const ul = $("rfopt-notes"); ul.textContent = "";
  for (const n of v.notes) ul.appendChild(el("li", n));
  $("rfopt-reset").hidden = !v.pinned;
  $("rfopt-apply").disabled = !scanned;
}

async function refreshRfopt() {
  try {
    rfoptData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi/optimize?lower_tx=${$("rfopt-tx").checked ? 1 : 0}`);
    renderRfopt();
  } catch (e) { msg(e.message, true); }
}

function openRfopt() { $("rfopt-dlg").hidden = false; renderRfopt(); }
$("rfopt-close").addEventListener("click", () => { $("rfopt-dlg").hidden = true; loadRfopt(); });
$("rfopt-tx").addEventListener("change", refreshRfopt);
$("rfopt-scan").addEventListener("click", async () => {
  const aps = (rfoptData ? rfoptData.aps : []).filter((a) => a.online);
  if (!aps.length) return msg(__("Žádné AP není online"), true);
  if (!(await ask(__`Změřit okolí ze ${aps.length} AP? Jedno po druhém, klienti se na každém na pár sekund odpojí.`))) return;
  const b = $("rfopt-scan"); b.disabled = true;
  let n = 0;
  for (const a of aps) {
    n += 1;
    $("rfopt-scan-state").textContent = __`Měřím ${a.device} (${n}/${aps.length})…`;
    try { await api("POST", `/tenants/${tenantId}/sites/${siteId()}/wifi/rogue`, { device_id: a.device_id }); }
    catch (e) { msg(`${a.device}: ${e.message}`, true); }
  }
  b.disabled = false;
  refreshRfopt();
});
$("rfopt-apply").addEventListener("click", async () => {
  const ch = rfoptData ? rfoptData.changes : 0;
  if (!(await ask(__`Nasadit optimalizované kanály${$("rfopt-tx").checked ? __(" a výkon") : ""}? ` +
                  (ch ? __("Při přeladění se klienti na dotčených AP krátce odpojí.") : __("Kanály se nemění, připne se jen současný stav."))))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/wifi/optimize`, { lower_tx: $("rfopt-tx").checked });
    $("rfopt-dlg").hidden = true;
    msg(__`Optimalizace nasazena: rušení ${r.before} → ${r.after}`); showApply(r.apply); loadRfopt();
  } catch (e) { msg(e.message, true); }
});
$("rfopt-reset").addEventListener("click", async () => {
  if (!(await ask(__("Zrušit optimalizaci? Kanály se vrátí k rozdělení podle počtu AP.")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/wifi/optimize`, { action: "reset" });
    $("rfopt-dlg").hidden = true; showApply(r.apply); loadRfopt();
  } catch (e) { msg(e.message, true); }
});

async function loadRogue(radios) {
  const box = $("rogue-card"); box.textContent = "";
  const aps = new Map();
  for (const r of radios || []) if (r.device_id && !r.master) aps.set(r.device_id, r.device || r.device_id);
  const head = el("div", null, "rogue-head");
  head.appendChild(el("h3", __("Cizí a podvržená AP")));
  if (aps.size && canOperate()) {
    const sel = el("select"); sel.setAttribute("aria-label", __("AP ke skenu"));
    for (const [id, name] of aps) { const o = el("option", name); o.value = id; sel.appendChild(o); }
    const b = el("button", __("Skenovat okolí"), "small secondary"); b.type = "button";
    b.title = __("Rádia AP na ~8 s přepnou do skenu – klienti na nich se na chvíli odpojí");
    b.onclick = async () => {
      if (!(await ask(__`Skenovat okolí z ${aps.get(sel.value)}? Klienti na jeho rádiích se na pár sekund odpojí.`))) return;
      b.disabled = true;
      try {
        const r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/wifi/rogue`, { device_id: sel.value });
        msg(__`Sken: ${r.found} sítí – podvržených ${r.rogue}, sousedních ${r.neighbor}, našich ${r.own}`, r.rogue > 0);
        for (const e of r.errors || []) msg(e, true);
        loadRogue(radios);
      } catch (e) { msg(e.message, true); } finally { b.disabled = false; }
    };
    head.append(sel, b);
  }
  box.appendChild(head);
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi/rogue`); } catch (e) { return; }
  const list = v.bss || [];
  const rogue = list.filter((b) => b.kind === "rogue");
  const neigh = list.filter((b) => b.kind === "neighbor");
  if (!list.length) {
    box.appendChild(el("p", __("Zatím bez skenu. Spusť sken ručně, nebo zapni noční sken v Nastavení lokality → Wi-Fi."), "muted small"));
    return;
  }
  box.appendChild(el("p", __`${rogue.filter((b) => !b.acknowledged).length} podvržených (cizí AP s naším SSID) · ${neigh.length} sousedních sítí` +
                     __` · poslední sken ${fmtTime(list.reduce((a, b) => (a > b.last_seen ? a : b.last_seen), ""))}`,
                     rogue.some((b) => !b.acknowledged) ? "sev-error" : "muted"));
  const t = el("table", null, "mini");
  const th = el("tr"); for (const h of ["", "SSID", "BSSID", __("Kanál"), __("Signál"), __("Zabezpečení"), __("Viděl"), ""]) th.appendChild(el("th", h));
  t.appendChild(th);
  for (const b of [...rogue, ...neigh.slice(0, 30)]) {
    const tr = el("tr", null, b.kind === "rogue" && !b.acknowledged ? "rogue-row" : "");
    tr.append(el("td", b.kind === "rogue" ? (b.acknowledged ? __("známé") : __("⚠ podvržené")) : __("soused")), el("td", b.ssid || __("(skryté)")),
              el("td", b.bssid, "mono"), el("td", b.frequency ? `${chanNum(b.frequency)} · ${b.frequency}` : "–"),
              el("td", b.signal != null ? `${b.signal} dBm` : "–"), el("td", b.security || "–", "muted"), el("td", b.seen_by_name || ""));
    const td = el("td");
    if (b.kind === "rogue" && canManage()) {
      const a = el("button", b.acknowledged ? __("Zrušit označení") : __("Označit jako známé"), "small secondary"); a.type = "button";
      a.onclick = async () => {
        try { await api("POST", `/tenants/${tenantId}/sites/${siteId()}/wifi/rogue`, { bssid: b.bssid, ack: !b.acknowledged }); loadRogue(radios); }
        catch (e) { msg(e.message, true); }
      };
      td.appendChild(a);
    }
    tr.appendChild(td);
    t.appendChild(tr);
  }
  box.appendChild(t);
}

function renderWifi(w) {
  renderCapsmanCard();
  loadRfopt();
  loadRogue(w.radios);
  const sm = w.summary;
  $("wifi-summary").textContent = __`${sm.aps} zařízení, ${sm.radios} rádií, ${sm.clients} klientů` +
    (sm.ssids.length ? ` · SSID: ${sm.ssids.join(", ")}` : "");
  const ul = $("wifi-warn"); ul.textContent = "";
  for (const x of w.warnings) ul.appendChild(el("li", x.text, "sev-" + (x.severity === "info" ? "info" : "warning")));
  const tb = $("wifi-radios"); tb.textContent = "";
  for (const r of w.radios) {
    const tr = el("tr");
    if (r.disabled) tr.className = "muted";
    const sig = r.signal_avg != null ? `${r.signal_avg} / ${r.signal_min} dBm` : "";
    tr.append(el("td", r.device || "?"),
              el("td", r.interface + (r.master ? __` (virt. na ${r.master})` : "") + (r.capsman ? " · CAPsMAN" : "") +
                       (r.disabled ? __(" · vypnuté") : !r.running ? __(" · neběží") : "")),
              el("td", r.ssid || ""), el("td", r.mode || ""), el("td", r.band || ""),
              el("td", r.freq_now ? `${chanNum(r.freq_now)} · ${r.freq_now} MHz${r.frequency === "auto" ? " (auto)" : ""}` : r.frequency || ""),
              el("td", r.width_now ? `${r.width_now} MHz` : r.width || ""), el("td", String(r.clients)),
              el("td", sig + (r.weak_clients ? __` (${r.weak_clients} slabých)` : ""),
                 r.signal_min != null && r.signal_min < -75 ? "warn" : ""));
    tb.appendChild(tr);
  }
  if (!w.radios.length) { const tr = el("tr"); const td = el("td", __("Žádná Wi-Fi rádia (inventura běží každých 5 min)"), "muted"); td.colSpan = 9; tr.appendChild(td); tb.appendChild(tr); }
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
  const zm = p.changes === 1 ? __("změna") : p.changes < 5 ? __("změny") : __("změn");
  $("wifi-plan-sum").textContent = `· ${p.changes ? `${p.changes} ${zm}` : __("beze změn")}` +
    (p.before != null ? __` · překryvy kanálů ${p.before} → ${p.after}` : "");
  const tb = $("wifi-plan"); tb.textContent = "";
  for (const i of p.items) {
    const tr = el("tr", null, i.change ? "" : "muted");
    const now = i.now.freq ? `${i.now.channel} / ${i.now.width} MHz${i.now.auto ? " (auto)" : ""}` : "?";
    const nw = el("td", `${i.new.channel} / ${i.new.width} MHz${i.new.dfs ? " · DFS" : ""}`, i.change ? "plan-new" : "");
    const cmd = el("td");
    if (i.change && i.command) {
      const b = el("button", __("Kopírovat"), "small secondary"); b.type = "button";
      b.title = i.command;
      b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(i.command); msg(__("Příkaz zkopírován")); } catch (e) { msg(i.command); }
      });
      cmd.appendChild(b);
    }
    tr.append(el("td", i.device || "?"), el("td", i.interface), el("td", i.band === "5" ? "5 GHz" : "2,4 GHz"),
              el("td", now), nw, el("td", i.change ? i.reasons.join("; ") : __("ponechat")), cmd);
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
  const wtxt = !w ? __("worker zatím nezapsal stav – běží? (systemctl status mtc-worker)")
    : w.stale ? __`worker NEBĚŽÍ – poslední kolo před ${Math.round(w.age_seconds / 60)} min (journalctl -u mtc-worker)`
    : __`worker běží (kolo ${w.tick}, před ${w.age_seconds} s)`;
  const dev = Object.entries(s.devices || {}).map(([k, v]) => `${k} ${v}`).join(", ") || __("žádná");
  const p = $("system-summary"); p.textContent = "";
  p.append(el("span", wtxt, !w || w.stale ? "sev-error" : ""),
           __` · zařízení: ${dev} · fronta příkazů: ${(s.commands.queued || 0)} čeká, ${(s.commands.sent || 0)} odesláno` +
           __` · otevřené alerty: ${Object.values(s.alerts || {}).reduce((a, b) => a + b, 0)}` +
           __` · DB ${(s.db_bytes / 1048576).toFixed(1)} MB · verze ${s.version}`);
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

// --- zálohy controlleru (superadmin) ------------------------------------------------------

const BK_DAYS = [__("Po"), __("Út"), __("St"), __("Čt"), __("Pá"), __("So"), __("Ne")];
const BT_HELP = {
  nextcloud: __("Heslo aplikace vytvoříš v Nextcloudu: Nastavení → Zabezpečení → Vytvořit nové heslo aplikace."),
  gdrive: __("Google Cloud Console → API a služby: zapni Google Drive API, OAuth klient typu „TVs and Limited Input devices“. ")
    + __("Aplikaci přepni do produkce, jinak přihlášení po 7 dnech vyprší. Controller uvidí jen soubory, které sám vytvoří."),
  dropbox: __("Dropbox App Console → Create app: „Scoped access“, „App folder“, v Permissions zaškrtni files.content.write a files.content.read. ")
    + __("Stačí App key, tajemství aplikace se nezadává. Zálohy budou ve složce Aplikace/<název aplikace>."),
  onedrive: __("Microsoft Entra → Registrace aplikací: účty v libovolné organizaci i osobní, „Povolit toky veřejného klienta“ = Ano, ")
    + __("oprávnění Files.ReadWrite.AppFolder. Zálohy budou ve složce Aplikace/<název aplikace>."),
};
// retence slovy: „14 posledních + 12 měsíčních“
function retText(keep, weekly, monthly) {
  return [__`${keep} posledních`, weekly ? __`${weekly} týdenních` : "", monthly ? __`${monthly} měsíčních` : ""].filter(Boolean).join(" + ");
}
function bkRetSum() {
  const k = Number($("bk-keep").value) || 0, w = Number($("bk-weekly").value) || 0, m = Number($("bk-monthly").value) || 0;
  const yrs = m / 12;
  const reach = m ? (m % 12 === 0 ? (yrs === 1 ? __("1 rok") : yrs < 5 ? __`${yrs} roky` : __`${yrs} let`) : __`${m} měsíců`) : w ? __`${w} týdnů` : __`${k} záloh`;
  $("bk-ret-sum").textContent = __`Na serveru zůstane nejvýš ${k + w + m} záloh (${retText(k, w, m)}), nejstarší sahá asi ${reach} zpět. `
    + (w || m ? __("Z každého týdne a měsíce se nechává první záloha.") : "");
}
let bkTargets = [];
let btEdit = null;
const btPolls = {};

function bkDaysInit() {
  const box = $("bk-days");
  if (box.children.length) return;
  BK_DAYS.forEach((d, i) => {
    const l = el("label"); const c = el("input"); c.type = "checkbox"; c.id = "bk-day-" + (i + 1);
    l.append(c, d); box.appendChild(l);
  });
}

async function loadBackups() {
  let b;
  try { b = await api("GET", "/system/backups"); } catch (e) { return; }
  $("backup-card").hidden = false;
  bkDaysInit();
  const st = b.status || {};
  const last = b.backups[0];
  const plan = b.plan || {};
  const when = plan.enabled ? `${plan.days.length === 7 ? __("denně") : plan.days.map((d) => BK_DAYS[d - 1]).join(", ")} v ${plan.time}` : __("automaticky vypnuto");
  const txt = b.pending ? __("Záloha čeká na spuštění…")
    : st.state === "running" ? __("Záloha právě běží…")
    : st.state === "error" ? __`Poslední záloha selhala: ${st.error || "?"}`
    : last ? __`Poslední záloha ${fmtTime(last.at)} · ${when} · ponechává se ${retText(plan.keep || b.keep, plan.weekly, plan.monthly)} v ${b.dir}`
    : __`Zatím žádná záloha · ${when} · ponechává se ${retText(plan.keep || b.keep, plan.weekly, plan.monthly)} v ${b.dir}`;
  const p = $("backup-summary"); p.textContent = "";
  p.appendChild(el("span", txt, st.state === "error" ? "sev-error" : ""));
  const limit = (b.max_gap_hours || 24) + 6;
  const old = plan.enabled && last && (Date.now() - new Date(last.at).getTime()) > limit * 3600000;
  if (old) p.append(" ", el("span", __("· poslední záloha je starší, než plán dovoluje"), "sev-warning"));
  const al = $("backup-alerts"); al.textContent = "";
  for (const a of Object.values(b.alerts || {})) al.appendChild(el("li", a.message, a.severity === "critical" ? "sev-error" : "sev-warning"));
  // plán
  $("bk-en").checked = !!plan.enabled; $("bk-time").value = plan.time || "02:30"; $("bk-keep").value = plan.keep || 14;
  $("bk-weekly").value = plan.weekly || 0; $("bk-monthly").value = plan.monthly || 0; bkRetSum();
  for (let d = 1; d <= 7; d++) $("bk-day-" + d).checked = (plan.days || []).includes(d);
  // cíle
  bkTargets = b.targets || [];
  renderTargets();
  const tb = $("backup-list"); tb.textContent = "";
  for (const x of b.backups) {
    const a = el("a", __("Stáhnout"));
    a.href = "/api/admin/system/backups/" + encodeURIComponent(x.name);
    a.setAttribute("download", x.name);
    const tr = el("tr");
    const dl = el("td"); dl.appendChild(a);
    tr.append(el("td", x.name, "mono"), el("td", fmtBytes(x.size)), el("td", fmtTime(x.at)), dl);
    tb.appendChild(tr);
  }
  $("backup-now").disabled = !!b.pending || st.state === "running";
  const uploading = bkTargets.some((t) => t.status.force);
  if (b.pending || st.state === "running" || uploading) setTimeout(() => { if (!$("backup-card").hidden) loadBackups(); }, 5000);
}

function targetState(t) {
  const s = t.status || {};
  if (!t.enabled) return [__("vypnuto"), "muted"];
  if (t.auth) return [null, ""];
  if (t.kind !== "nextcloud" && !t.authorized) return [s.auth_error ? __`nepřihlášeno – ${s.auth_error}` : __("nepřihlášeno"), "sev-warning"];
  if (s.force) return [__("odesílá se…"), ""];
  if (s.last_error) return [__`chyba: ${s.last_error}`, "sev-error"];
  if (s.last_ok_at) return [__("v pořádku"), "sev-ok"];
  return [__("čeká na první zálohu"), "muted"];
}

function renderTargets() {
  const tb = $("bt-list"); tb.textContent = "";
  for (const t of bkTargets) {
    const tr = el("tr");
    const where = t.status.where || (t.kind === "nextcloud" ? `${t.config.url} · ${t.config.folder}`
      : t.kind === "gdrive" ? __`Google Drive · ${t.config.folder}`
      : t.kind === "dropbox" ? __`Dropbox · složka aplikace${t.config.folder ? "/" + t.config.folder : ""}` : __("OneDrive · složka aplikace"));
    const [label, cls] = targetState(t);
    const stTd = el("td", label, cls);
    if (t.auth && t.auth.paste) {
      // Dropbox: otevřít odkaz, schválit a kód, který Dropbox ukáže, vložit sem
      const a = el("a", __("1. Otevři Dropbox a povol přístup")); a.href = t.auth.url; a.target = "_blank"; a.rel = "noopener";
      const inp = el("input"); inp.placeholder = __("2. sem vlož kód z Dropboxu"); inp.className = "bt-paste"; inp.setAttribute("aria-label", __("Kód z Dropboxu"));
      const ok = el("button", __("Dokončit"), "small"); ok.type = "button";
      ok.addEventListener("click", async () => {
        try { await api("POST", `/system/backup-targets/${t.id}/code`, { code: inp.value.trim() }); msg(__`${t.name}: přihlášeno`); } catch (e) { msg(e.message, true); }
        loadBackups();
      });
      const box = el("div", null, "bt-pastebox"); box.append(a, inp, ok); stTd.textContent = ""; stTd.appendChild(box);
    } else if (t.auth) {
      stTd.append(__("Zadej kód "), el("span", t.auth.user_code, "bt-code"), " na ");
      const a = el("a", t.auth.url); a.href = t.auth.url; a.target = "_blank"; a.rel = "noopener";
      stTd.append(a);
      pollTarget(t);
    }
    const s = t.status || {};
    const lastTd = el("td", s.last_ok_at ? fmtTime(s.last_ok_at) : "–");
    if (s.last_file) lastTd.title = s.last_file + (s.last_size ? ` (${fmtBytes(s.last_size)})` : "");
    const acts = el("div", null, "bt-acts");
    const btn = (label, fn, sec = true) => { const x = el("button", label, "small" + (sec ? " secondary" : "")); x.type = "button";
      x.addEventListener("click", fn); acts.appendChild(x); return x; };
    if (t.kind !== "nextcloud" && !t.auth) btn(t.authorized ? __("Přihlásit znovu") : __("Přihlásit"), () => targetAuth(t), !!t.authorized);
    if (t.kind === "nextcloud" || t.authorized) {
      btn(__("Test spojení"), () => targetCheck(t)).title = __("Zkusí přihlášení a složku, nic nenahrává");
      btn(__("Ověřit zálohu"), () => targetVerify(t), false).title = __("Zkontroluje, že poslední záloha v úložišti opravdu je a má správnou velikost");
      btn(__("Odeslat teď"), () => targetAction(t, "upload", __("Poslední záloha se odešle")));
    }
    btn(__("Upravit"), () => openTarget(t));
    btn("×", () => deleteTarget(t)).title = __("Smazat cíl");
    const actTd = el("td"); actTd.appendChild(acts);
    const nameTd = el("td", `${t.name} `);
    nameTd.append(el("span", `${t.kind_label} · ${retText(t.keep, t.weekly, t.monthly)}`, "muted"), el("br"), el("span", where, "muted small"));
    const v = s.verified, vTd = el("td", !v ? __("zatím neověřeno") : v.ok ? __`✓ záloha je v úložišti (${v.count} celkem)`
      : v.missing ? __("✗ poslední záloha v úložišti chybí") : v.size_ok === false ? __("✗ záloha má jinou velikost než na serveru") : __("na serveru zatím žádná záloha není"),
      !v ? "muted" : v.ok ? "sev-ok" : v.newest ? "sev-error" : "muted");
    if (v) { vTd.appendChild(el("small", " " + fmtTime(s.verified_at), "muted")); vTd.title = [v.newest, v.bytes ? __`v úložišti ${fmtBytes(v.bytes)}` : ""].filter(Boolean).join("\n"); }
    tr.append(nameTd, stTd, lastTd, vTd, actTd);
    tb.appendChild(tr);
  }
  $("bt-quick").hidden = bkTargets.length > 0;
  if (!bkTargets.length) {
    const tr = el("tr"); const td = el("td", __("Zatím se zálohuje jen na tento server. Přidej externí úložiště tlačítky nahoře."), "muted");
    td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr);
  }
}

async function targetVerify(t) {
  try {
    const v = await api("POST", `/system/backup-targets/${t.id}/verify`, {});
    if (v.ok) msg(__`${t.name}: záloha ${v.newest} v úložišti je a velikost sedí (celkem ${v.count} záloh)`);
    else msg(v.missing ? __`${t.name}: poslední záloha ${v.newest} v úložišti chybí – zkus „Odeslat teď“` : v.newest ? __`${t.name}: záloha v úložišti má jinou velikost – odešli ji znovu` : __("Na serveru zatím žádná záloha není – nejdřív klikni na Zálohovat teď"), true);
  } catch (e) { msg(e.message, true); }
  loadBackups();
}
async function targetAction(t, action, okMsg) {
  try { await api("POST", `/system/backup-targets/${t.id}/${action}`, {}); if (okMsg) msg(okMsg); } catch (e) { msg(e.message, true); }
  loadBackups();
}
async function targetCheck(t) {
  try { const r = await api("POST", `/system/backup-targets/${t.id}/check`, {}); msg(__("Spojení v pořádku: ") + r.where); }
  catch (e) { msg(e.message, true); }
  loadBackups();
}
async function targetAuth(t) {
  try { await api("POST", `/system/backup-targets/${t.id}/auth`, {}); } catch (e) { msg(e.message, true); }
  loadBackups();
}
function pollTarget(t) {
  if (btPolls[t.id]) return;
  btPolls[t.id] = setTimeout(async () => {
    delete btPolls[t.id];
    if ($("backup-card").hidden) return;
    let r;
    try { r = await api("POST", `/system/backup-targets/${t.id}/poll`, {}); } catch (e) { msg(e.message, true); return; }
    if (r.state === "done") { msg(__`${t.name}: přihlášeno`); loadBackups(); }
    else if (r.state === "error") { msg(`${t.name}: ${r.error}`, true); loadBackups(); }
    else pollTarget(t);
  }, Math.max(5, (t.auth && t.auth.interval) || 5) * 1000);
}
async function deleteTarget(t) {
  if (!await ask(__`Smazat cíl ${t.name}? Zálohy, které už v cloudu jsou, zůstanou.`, __("Smazat"))) return;
  try { await api("DELETE", `/system/backup-targets/${t.id}`); msg(__("Cíl smazán")); } catch (e) { msg(e.message, true); }
  loadBackups();
}

function btKindUi() {
  const k = $("bt-kind").value;
  const show = (cls, on) => { for (const x of document.getElementsByClassName(cls)) x.hidden = !on; };
  show("bt-nc", k === "nextcloud"); show("bt-oauth", k !== "nextcloud"); show("bt-gd", k === "gdrive");
  show("bt-od", k === "onedrive"); show("bt-folder", k !== "onedrive");
  $("bt-cid-l").textContent = k === "onedrive" ? __("Application (client) ID") : k === "dropbox" ? __("App key") : __("Client ID");
  $("bt-folder").placeholder = k === "dropbox" ? __("volitelná podsložka") : "MikroTik-Controller";
  $("bt-help").textContent = BT_HELP[k];
}
function openTarget(t, kind) {
  btEdit = t || null;
  const c = (t && t.config) || {};
  $("bt-title").textContent = t ? __`Upravit: ${t.name}` : __("Nové externí úložiště");
  $("bt-kind").value = t ? t.kind : kind || "nextcloud"; $("bt-kind").disabled = !!t;
  $("bt-name").value = t ? t.name : ""; $("bt-keep").value = t ? t.keep : 14;
  $("bt-weekly").value = t ? t.weekly || 0 : 0; $("bt-monthly").value = t ? t.monthly || 0 : 0;
  $("bt-url").value = c.url || ""; $("bt-user").value = c.user || ""; $("bt-pass").value = "";
  $("bt-cid").value = c.client_id || ""; $("bt-csec").value = ""; $("bt-tenant").value = c.tenant || "";
  $("bt-folder").value = c.folder || "";
  $("bt-keepsecret").hidden = !t;
  btKindUi();
  $("bt-dlg").hidden = false;
}
function closeTarget() { $("bt-dlg").hidden = true; btEdit = null; }
$("bt-kind").addEventListener("change", btKindUi);
$("bt-add").addEventListener("click", () => openTarget(null));
for (const b of $("bt-quick").getElementsByTagName ? $("bt-quick").getElementsByTagName("button") : [])
  b.addEventListener("click", () => openTarget(null, b.getAttribute("data-kind")));
$("bt-close").addEventListener("click", closeTarget);
$("bt-cancel").addEventListener("click", closeTarget);
$("bt-save").addEventListener("click", async () => {
  const k = $("bt-kind").value;
  const body = { name: $("bt-name").value.trim(), keep: Number($("bt-keep").value), weekly: Number($("bt-weekly").value) || 0,
                 monthly: Number($("bt-monthly").value) || 0 };
  if (k === "nextcloud") Object.assign(body, { url: $("bt-url").value.trim(), user: $("bt-user").value.trim(),
    password: $("bt-pass").value, folder: $("bt-folder").value.trim() });
  if (k === "gdrive") Object.assign(body, { client_id: $("bt-cid").value.trim(), client_secret: $("bt-csec").value,
    folder: $("bt-folder").value.trim() });
  if (k === "onedrive") Object.assign(body, { client_id: $("bt-cid").value.trim(), tenant: $("bt-tenant").value.trim() || "common" });
  if (k === "dropbox") Object.assign(body, { client_id: $("bt-cid").value.trim(), folder: $("bt-folder").value.trim() });
  if (btEdit && k === "nextcloud" && !body.password) delete body.password;
  if (btEdit && k === "gdrive" && !body.client_secret) delete body.client_secret;
  try {
    if (btEdit) await api("PATCH", `/system/backup-targets/${btEdit.id}`, body);
    else {
      const r = await api("POST", "/system/backup-targets", { kind: k, ...body });
      if (k !== "nextcloud") await api("POST", `/system/backup-targets/${r.id}/auth`, {});
    }
    closeTarget();
    msg(k === "nextcloud" ? __("Cíl uložen – ověř spojení") : k === "dropbox" ? __("Uloženo – dokonči přihlášení: odkaz a pole pro kód jsou v tabulce") : __("Uloženo – dokonči přihlášení kódem v tabulce"));
  } catch (e) { msg(e.message, true); return; }
  loadBackups();
});

for (const id of ["bk-keep", "bk-weekly", "bk-monthly"]) $(id).addEventListener("input", bkRetSum);
for (const b of $("bk-plan").getElementsByTagName ? $("bk-plan").getElementsByTagName("button") : []) {
  const ret = b.getAttribute && b.getAttribute("data-ret");
  if (ret) b.addEventListener("click", () => { [$("bk-keep").value, $("bk-weekly").value, $("bk-monthly").value] = ret.split(","); bkRetSum(); });
}
$("bk-plan").addEventListener("submit", async (e) => {
  e.preventDefault();
  const days = [1, 2, 3, 4, 5, 6, 7].filter((d) => $("bk-day-" + d).checked);
  try {
    await api("PUT", "/system/backup-plan", { enabled: $("bk-en").checked, time: $("bk-time").value, days,
                                              keep: Number($("bk-keep").value), weekly: Number($("bk-weekly").value) || 0,
                                              monthly: Number($("bk-monthly").value) || 0 });
    msg(__("Plán záloh uložen"));
  } catch (err) { msg(err.message, true); }
  loadBackups();
});

$("backup-now").addEventListener("click", async () => {
  try { await api("POST", "/system/backups", {}); msg(__("Záloha spuštěna")); } catch (e) { msg(e.message, true); }
  loadBackups();
});

// --- certifikát HTTPS (superadmin) ---------------------------------------------------------

// --- čas a DNS pro řízená zařízení (NTP vždy z controlleru) ----------------------------------
async function loadNetsvc() {
  let r;
  try { r = await api("GET", "/system/netsvc"); } catch (_e) { return; }
  $("ns-card").hidden = false;
  $("ns-ip").textContent = r.controller_ip;
  $("ns-ntp").value = r.netsvc.ntp_upstream.join(", ");
  $("ns-dns").value = r.netsvc.dns.join(", ");
  const st = r.status || {};
  $("ns-state").textContent = r.pending ? __("nastavuji NTP server…")
    : st.at ? (st.ok ? __`NTP server běží (${fmtTime(st.at)}), povoleno pro ${(st.allow || []).join(", ")}` : __`chyba: ${st.error}`) : "";
  $("ns-state").className = st.ok === false ? "sev-error small" : "muted small";
  if (r.pending) setTimeout(loadNetsvc, 3000);
}
$("ns-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const split = (v) => v.split(/[ ,;]+/).filter(Boolean);
  try {
    await api("PUT", "/system/netsvc", { ntp_upstream: split($("ns-ntp").value), dns: split($("ns-dns").value) });
    msg(__("Uloženo – NTP controlleru se přenastaví, DNS dostanou řízená zařízení při dalším sladění"));
    loadNetsvc();
  } catch (e) { msg(e.message, true); }
});

const TLS_KIND = { own_ca: __("CA controlleru"), letsencrypt: "Let's Encrypt", custom: __("vlastní") };
const TLS_SLOT = { admin: __("administrace"), checkin: "check-in" };
let tlsLocal = false;

async function loadTls() {
  let t;
  try { t = await api("GET", "/system/tls"); } catch (e) { return; }
  $("tls-card").hidden = false;
  tlsLocal = t.local;
  $("tls-slots").hidden = t.local;
  const tb = $("tls-list"); tb.textContent = "";
  for (const slot of t.local ? ["admin"] : ["admin", "checkin"]) {
    const i = t.slots[slot];
    const tr = el("tr");
    const name = t.local ? __("správa i check-in") : TLS_SLOT[slot];
    if (!i || i.error) {
      tr.append(el("td", name), el("td", i ? i.error : __("chybí"), "sev-error"), el("td", ""), el("td", ""), el("td", ""));
    } else {
      const exp = el("td", __`${new Date(i.not_after).toLocaleDateString(LOCALE)} (${i.days_left} dní)`, i.days_left < 14 ? "sev-error" : i.days_left < 30 ? "sev-warning" : "");
      tr.append(el("td", name), el("td", TLS_KIND[i.kind] || (i.self_signed ? "self-signed" : "?")),
                el("td", i.names.join(", "), "mono"), el("td", i.issuer), exp);
    }
    tb.appendChild(tr);
  }
  const st = t.status || {};
  const p = $("tls-summary"); p.textContent = "";
  const own = Object.values(t.slots).some((i) => i && i.kind === "own_ca");
  const txt = t.pending ? __("Změna certifikátu probíhá…")
    : st.state === "error" ? __`Poslední změna selhala (běží předchozí certifikát): ${st.error || "?"}`
    : st.state === "done" ? __`Naposledy změněno ${fmtTime(st.at)}` + (st.warnings && st.warnings.length ? " – " + st.warnings.join("; ") : "")
    : own ? __("Běží certifikát z CA controlleru – prohlížeč ho bez importu CA označí za nedůvěryhodný. Nastav Let's Encrypt nebo vlastní.")
    : "";
  p.appendChild(el("span", txt, st.state === "error" ? "sev-error" : ""));
  $("tls-apply").disabled = !!t.pending;
  if (t.pending) setTimeout(() => { if (!$("tls-card").hidden) loadTls(); }, 4000);
}

const TLS_RADIOS = ["tls-a-le", "tls-a-custom", "tls-a-own"];
function tlsAction() { const r = TLS_RADIOS.find((id) => $(id).checked); return r ? $(r).value : "letsencrypt"; }
function tlsForm() {
  const a = tlsAction();
  $("tls-le").hidden = a !== "letsencrypt";
  $("tls-custom").hidden = a !== "custom";
}
for (const id of TLS_RADIOS) $(id).addEventListener("change", tlsForm);
for (const [f, t] of [["tls-cert-file", "tls-cert"], ["tls-key-file", "tls-key"]]) {
  $(f).addEventListener("change", async () => { const x = $(f).files[0]; if (x) $(t).value = await x.text(); });
}
$("tls-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const action = tlsAction();
  const slots = tlsLocal ? ["admin"] : ["admin", "checkin"].filter((s) => $("tls-slot-" + s).checked);
  const body = { action, slots };
  if (action === "letsencrypt") body.email = $("tls-email").value.trim();
  if (action === "custom") { body.cert = $("tls-cert").value; body.key = $("tls-key").value; }
  if (action !== "own_ca" && (tlsLocal || slots.includes("checkin"))
      && !await ask(__("Zařízení se hlásí přes tento certifikát. Bez důvěry ve veřejné CA (RouterOS 7.19+) se přestanou hlásit. Pokračovat?"), __("Pokračovat"))) return;
  try {
    const r = await api("POST", "/system/tls", body);
    $("tls-warn").textContent = (r.warnings || []).join("; ");
    $("tls-key").value = ""; $("tls-key-file").value = "";
    msg(__("Žádost o certifikát odeslána"));
  } catch (err) { msg(err.message, true); }
  loadTls();
});

// --- uživatelé tenanta a vlastní heslo -----------------------------------------------------

const ROLE_LABEL = { tenant_admin: __("správce"), technician: __("technik"), readonly: __("jen čtení") };

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
      try { await api("PATCH", `/tenants/${tenantId}/users/${u.id}`, { role: sel.value }); msg(__("Role změněna")); }
      catch (e) { msg(e.message, true); loadUsers(); }
    });
    const act = el("td");
    if (!self) {
      const b = el("button", __("Odebrat"), "small danger");
      b.onclick = async () => {
        if (!(await ask(__`Odebrat ${u.email} z tenanta?`))) return;
        try { await api("DELETE", `/tenants/${tenantId}/users/${u.id}`); loadUsers(); } catch (e) { msg(e.message, true); }
      };
      act.appendChild(b);
    }
    if (me.superadmin && u.mfa) {
      const b = el("button", __("Reset MFA"), "small secondary");
      b.onclick = async () => {
        if (!(await ask(__`Resetovat MFA pro ${u.email}? Při dalším přihlášení si ho nastaví znovu.`))) return;
        try { await api("POST", `/users/${u.id}/reset-mfa`); loadUsers(); } catch (e) { msg(e.message, true); }
      };
      act.append(" ", b);
    }
    const td = el("td"); td.appendChild(sel);
    tr.append(el("td", u.email + (u.locked ? __(" (zamčeno)") : "") + (u.is_active ? "" : __(" (neaktivní)"))), td,
              el("td", u.mfa ? __("ano") : __("zatím ne")), el("td", fmtTime(u.last_login)), act);
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
      ? __`Nový účet ${r.email}: dočasné heslo ${r.temp_password} – předej ho bezpečně, zobrazí se jen teď. MFA si nastaví při prvním přihlášení.`
      : "";
    if (!r.temp_password) msg(__`${r.email} přidán do tenanta`);
    ev.target.reset();
    loadUsers();
  } catch (e) { msg(e.message, true); }
});

$("pw-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    await api("POST", "/me/password", { old: f.get("old"), new: f.get("new") });
    msg(__("Heslo změněno, ostatní relace odhlášeny"));
    ev.target.reset();
    $("pw-card").hidden = true;
  } catch (e) { msg(e.message, true); }
});

// --- předvolby zobrazení (jen v tomto prohlížeči) --------------------------------------------

const prefs = (() => { try { return JSON.parse(localStorage.getItem("mtc-view") || "{}"); } catch (e) { return {}; } })();
function savePrefs() { try { localStorage.setItem("mtc-view", JSON.stringify(prefs)); } catch (e) { /* bez úložiště */ } }

// --- navigace: levý panel se sekcemi, záložky lokality a detailu -----------------------------

// lokalita: pět sekcí jako v UniFi; dole celý tenant (lokality, uživatelé, systém)
// [klíč, popisek v ikonové liště, celý název (tooltip)]
const VIEWS = [["dash", __("Přehled"), __("Přehled a topologie")], ["devices", __("Zařízení")], ["ports", __("Porty")], ["clients", __("Klienti")],
  ["settings", __("Nastavení"), __("Nastavení lokality")], ["activity", __("Aktivita"), __("Aktivita a audit")],
  ["tenant", __("Lokality"), __("Všechny lokality")], ["users", __("Uživatelé")], ["backups", __("Zálohy")], ["system", __("Systém")]];
const SITE_VIEWS = ["dash", "devices", "ports", "clients", "settings", "activity"];
const navBadges = {};

// ikony navigace (16 px, obrys)
const NAV_ICON = {
  dash: "M2 9l6-6 6 6M4 8v5h8V8", devices: "M2 4h12v5H2zM2 11h12M5 6.5h.01M8 6.5h.01", clients: "M3 3h10v7H3zM1.5 13h13",
  ports: "M2 4h12v8H2zM5 7v3M8 7v3M11 7v3",
  settings: "M3 4h10M3 8h10M3 12h10M6 2.5v3M10 6.5v3M5 10.5v3", activity: "M1.5 8h3l2-5 3 10 2-5h3",
  tenant: "M2 14V4l5-2v12M7 6l7 2v6M1 14h14", users: "M8 8a3 3 0 100-6 3 3 0 000 6zM2.5 14c.6-3 2.7-4.5 5.5-4.5s4.9 1.5 5.5 4.5",
  system: "M3 3h10v4H3zM3 9h10v4H3zM5.5 5h.01M5.5 11h.01",
  backups: "M4.5 12.5a3 3 0 0 1-.4-6 4 4 0 0 1 7.7-.6 3.2 3.2 0 0 1-.3 6.6zM8 11V7.5M6.3 9 8 7.3 9.7 9",
};
function navIcon(key) {
  const NS = "http://www.w3.org/2000/svg";
  if (typeof document.createElementNS !== "function") return el("span");
  const svg = document.createElementNS(NS, "svg"); svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(NS, "path"); p.setAttribute("d", NAV_ICON[key] || ""); svg.appendChild(p);
  return svg;
}

// položky správy zákazníka (lokality, uživatelé, systém) nejsou v levém pruhu, ale v nabídce „Správa“ v záhlaví
const TENANT_MENU = { tenant: __("všechny lokality zákazníka"), users: __("účty, role, dvoufázové přihlášení"),
  backups: __("plán, retence, kopie do cloudu"), system: __("certifikáty, čas a DNS") };
function buildNav() {
  const nav = $("nav-items"); nav.textContent = "";
  const menu = $("tmenu-list"); while (menu.children.length > 1) menu.removeChild(menu.children[menu.children.length - 1]);
  for (const [key, label, full] of VIEWS) {
    if ((key === "system" || key === "backups") && !(me && me.superadmin)) continue;
    const inMenu = key in TENANT_MENU;
    const b = el("button", null, "navbtn"); b.type = "button"; b.id = "nav-" + key; b.title = inMenu ? "" : full || label;
    const lab = el("span", null, "nlabel");
    lab.append(navIcon(key), document.createTextNode(label));
    b.appendChild(lab);
    if (inMenu) b.appendChild(el("small", TENANT_MENU[key]));
    const badge = el("span", "", "nbadge"); badge.hidden = true;
    b.appendChild(badge); navBadges[key] = badge;
    b.addEventListener("click", () => { hmenuClose(); if ($("app").hidden) { show("app"); loadDevices(); } showView(key, true); });
    (inMenu ? menu : nav).appendChild(b);
  }
}

// rozbalovací nabídky v záhlaví (správa zákazníka, uživatel): jedna otevřená, zavře klik mimo nebo Esc
const HMENUS = [["tmenu-btn", "tmenu-list"], ["umenu-btn", "umenu-list"]];
function hmenuClose() { for (const [b, l] of HMENUS) { $(l).hidden = true; $(b).setAttribute("aria-expanded", "false"); } }
for (const [b, l] of HMENUS) $(b).addEventListener("click", (e) => {
  e.stopPropagation(); const open = $(l).hidden; hmenuClose();
  if (open) { $(l).hidden = false; $(b).setAttribute("aria-expanded", "true"); }
});
for (const [, l] of HMENUS) $(l).addEventListener("click", (e) => e.stopPropagation());
if (typeof document.addEventListener === "function") {
  document.addEventListener("click", hmenuClose);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hmenuClose(); });
}
$("hdr-bell").addEventListener("click", () => openSub("activity", alertsOpen[0] ? "alerts" : plansOpen ? "plans" : "tl"));

// podzáložky sekcí: [klíč, tlačítko, panel, loader]; loadery jsou definované níž (vyhodnotí se při volání)
const SUBTABS = {
  dash: [["infra", "infra-btn", "infra-wrap", () => loadInfra()], ["topo", "topo-btn", "topo-wrap", () => loadTopology()],
         ["stp", "stp-btn", "stp-wrap", () => loadStp()]],
  devices: [["list", "devlist-btn", "devlist-wrap", () => loadDevices()], ["sports", "sports-btn", "sports-wrap", () => loadSitePorts()],
            ["links", "links-btn", "links-wrap", () => loadLinks()]],
  clients: [["clients", null, null, () => loadClients()]],
  ports: [["ports", null, null, () => loadPortMap()]],
  settings: [["net", "net-btn", "net-wrap", () => loadNetworks()], ["wifi", "wifi-btn", "wifiset-wrap", () => { loadIntent(); loadWifi(); }],
             ["prof", "prof-btn", "prof-wrap", () => loadProfiles()],
             ["devbk", "devbk-btn", "devbk-wrap", () => loadDevBackups()],
             ["sys", "sys-btn", "sys-wrap", () => { $("token-site").value = siteId(); loadNtpPolicy(); loadUpgrade(); loadDeploy(); }]],
  activity: [["tl", "tl-btn", "tl-wrap", () => loadTimeline()], ["avail", "avail-btn", "avail-wrap", () => loadAvail()], ["alerts", "alerts-btn", "alerts-wrap", () => loadAlerts()], ["plans", "plans-btn", "plans-wrap", () => loadPlans()],
             ["audit", "audit-btn", "audit-wrap", () => loadAudit()]],
};
function subOf(view) {
  const tabs = SUBTABS[view]; if (!tabs) return null;
  const want = (prefs.sub || {})[view];
  return tabs.find((t) => t[0] === want) || tabs[0];
}
function activateSub(view, key) {
  prefs.sub = { ...(prefs.sub || {}), [view]: key }; savePrefs();
  for (const [k, btn, pane] of SUBTABS[view]) {
    if (pane) $(pane).hidden = k !== key;
    if (btn) $(btn).className = k === key ? "active" : "";
  }
}
function openSub(view, key) {
  if (prefs.view !== view) showView(view);
  activateSub(view, key);
  const t = SUBTABS[view].find((x) => x[0] === key);
  if (t && tenantId && (siteId() || !SITE_VIEWS.includes(view))) t[3]();
}
for (const [view, tabs] of Object.entries(SUBTABS)) for (const [k, btn] of tabs) if (btn) $(btn).addEventListener("click", () => openSub(view, k));

function showView(key, load) {
  if (!VIEWS.some(([k]) => k === key) || ((key === "system" || key === "backups") && !(me && me.superadmin))) key = "dash";
  prefs.view = key; savePrefs();
  for (const [k] of VIEWS) {
    const v = $("view-" + k); if (v) v.hidden = k !== key;
    const b = $("nav-" + k); if (b) b.className = "navbtn" + (k === key ? " active" : "");
  }
  $("nav-site").hidden = !SITE_VIEWS.includes(key);
  const t = subOf(key);
  if (t) { activateSub(key, t[0]); if (load && tenantId && siteId()) t[3](); }
  if (key === "dash") renderOverview();
  if (key === "backups" && load) loadBackups();
}

// --- rychlé hledání v záhlaví (Ctrl+K): kde co je + zařízení ----------------------------------------------
// [popisek, kde to je, klíčová slova, pohled, podzáložka]
const GS_PLACES = [
  [__("Mapa lokality"), __("Přehled"), "mapa infrastruktura wan map overview", "dash", "infra"],
  [__("Topologie"), __("Přehled"), "topologie strom spoje topology links", "dash", "topo"],
  ["STP", __("Přehled"), "stp rstp root smycka loop", "dash", "stp"],
  [__("Seznam zařízení"), __("Zařízení"), "zarizeni adopce pridat switch ap devices adopt", "devices", "list"],
  [__("Porty všech switchů"), __("Zařízení"), "porty poe vlan ports", "devices", "sports"],
  [__("Porty: schéma, VLAN, provoz"), __("Porty"), "porty port poe vlan rychlost linka schema ports speed stats provoz", "ports", null],
  [__("Bezdrátové spoje: signál, ztrátovost, anomálie"), __("Zařízení"), "spoje spoj ptp link signal ztratovost odezva ruseni bezdratovy wireless", "devices", "links"],
  [__("Zálohy zařízení: výběr, pravidla, verze"), __("Nastavení"), "zaloha zalohy zarizeni konfigurace export verze obnova retence backup restore", "settings", "devbk"],
  [__("Klienti"), __("Klienti"), "klienti wifi mac ip clients history historie", "clients", null],
  [__("Sítě (VLAN, podsítě)"), __("Nastavení"), "site vlan podsit dhcp subnet networks zamer", "settings", "net"],
  [__("Wi-Fi (SSID, hesla, kanály)"), __("Nastavení"), "wifi ssid heslo capsman kanaly channels password", "settings", "wifi"],
  [__("Profily portů"), __("Nastavení"), "profily portu trunk access port profiles", "settings", "prof"],
  [__("Aktualizace RouterOS a firmware"), __("Nastavení"), "aktualizace upgrade update routerboot firmware automaticke", "settings", "sys"],
  [__("Přidat zařízení, výchozí konfigurace, NTP"), __("Nastavení"), "adopce pridat netinstall vychozi konfigurace ntp cas adopt defaults time", "settings", "sys"],
  [__("Časová osa"), __("Aktivita"), "casova osa timeline udalosti historie co se stalo vypadek events", "activity", "tl"],
  [__("Dostupnost zařízení"), __("Aktivita"), "dostupnost uptime vypadky sla availability outages", "activity", "avail"],
  [__("Alerty"), __("Aktivita"), "alerty upozorneni problemy alerts servisni okno maintenance", "activity", "alerts"],
  [__("Změny (plány ke schválení)"), __("Aktivita"), "zmeny plany schvaleni prevzeti changes plans approve", "activity", "plans"],
  [__("Audit"), __("Aktivita"), "audit log kdo co historie", "activity", "audit"],
  [__("Všechny lokality"), __("Lokality"), "lokality sites tenant", "tenant", null],
  [__("Uživatelé a přístupy"), __("Uživatelé"), "uzivatele hesla mfa role users", "users", null],
  [__("Systém (certifikáty, čas a DNS)"), __("Systém"), "system certifikat dns ntp smtp", "system", null],
  [__("Zálohy controlleru: plán, retence, Nextcloud, Google Drive, Dropbox"), __("Zálohy"), "zaloha zalohy backup retence rotace plan cloud nextcloud dropbox google drive onedrive obnova", "backups", null],
];
const gsNorm = (x) => String(x || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
let gsItems = [], gsSel = 0;

function gsSearch(qs) {
  const words = gsNorm(qs).split(/\s+/).filter(Boolean);
  const out = [];
  if (!words.length) return out;
  const hit = (text) => words.every((w) => text.includes(w));
  for (const d of devicesCache) {
    if (hit(gsNorm([d.identity, d.serial, d.model, d.note, d.tunnel_ip, d.role].join(" "))))
      out.push({ label: d.identity || d.serial, where: __("zařízení") + (d.model ? " · " + d.model : ""), go: () => openDetail(d.id) });
  }
  // brána, WAN, firewall: nastavení je v detailu brány (záložka Řízení)
  const gw = devicesCache.find((d) => d.role === "gateway");
  if (gw && hit("brana gateway wan internet pppoe firewall zony zones dhcp server nat"))
    out.push({ label: __("Brána: internet (WAN), DHCP, firewall"), where: (gw.identity || gw.serial) + " → " + __("Řízení"),
               go: async () => { await openDetail(gw.id); openDetailTab("manage"); } });
  for (const [label, where, kw, view, sub] of GS_PLACES) {
    if (view === "system" && !(me && me.superadmin)) continue;
    if (hit(gsNorm(label + " " + where) + " " + kw))
      out.push({ label, where, go: () => { if ($("app").hidden) show("app"); if (sub) openSub(view, sub); else showView(view, true); } });
  }
  return out.slice(0, 12);
}

function gsRender() {
  const ul = $("gs-list"); ul.textContent = "";
  gsItems = gsSearch($("gs-in").value);
  gsSel = Math.min(gsSel, Math.max(gsItems.length - 1, 0));
  ul.hidden = !$("gs-in").value.trim();
  if (!gsItems.length) { ul.appendChild(el("li", __("Nic nenalezeno"), "muted")); return; }
  gsItems.forEach((it, i) => {
    const li = el("li", null, i === gsSel ? "sel" : "");
    li.append(el("span", it.label), el("small", it.where));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); gsGo(i); });
    ul.appendChild(li);
  });
}

function gsGo(i) {
  const it = gsItems[i]; if (!it) return;
  $("gs-in").value = ""; $("gs-list").hidden = true; $("gs-in").blur();
  it.go();
}

$("gs-in").addEventListener("input", () => { gsSel = 0; gsRender(); });
$("gs-in").addEventListener("focus", gsRender);
$("gs-in").addEventListener("blur", () => { $("gs-list").hidden = true; });
$("gs-in").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (gsItems.length) { gsSel = (gsSel + (e.key === "ArrowDown" ? 1 : gsItems.length - 1)) % gsItems.length; gsRender(); }
  } else if (e.key === "Enter") { e.preventDefault(); gsGo(gsSel); }
  else if (e.key === "Escape") { e.stopPropagation(); $("gs-in").value = ""; $("gs-list").hidden = true; $("gs-in").blur(); }
});
if (typeof document.addEventListener === "function") document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && String(e.key).toLowerCase() === "k" && !$("gs").hidden) { e.preventDefault(); $("gs-in").focus(); $("gs-in").select(); }
});

// --- časová osa lokality: všechny události na jedné ose (hledání příčiny problému) ------------------------

const TL_CATS = [["alert", __("Alerty")], ["port", __("Porty")], ["client", __("Klienti")], ["wifi", "Wi-Fi"],
                 ["change", __("Změny konfigurace")], ["audit", __("Zásahy správce")]];
const TL_RANGES = [[1, __("1 h")], [6, __("6 h")], [24, __("24 h")], [168, __("7 dní")], [720, __("30 dní")]];
const tl = { hours: 24, until: null, minutes: null, cats: new Set(TL_CATS.map((c) => c[0])), device: "", mac: "", macName: "", q: "", data: null };

function tlText(e) {
  const dev = e.device || "";
  if (e.cat === "alert") return (e.kind === "resolved" ? __("pominulo: ") : "") + (e.text || e.a || "");
  if (e.cat === "port") return `${e.port}: ${e.text || ""}`;
  if (e.cat === "client") {
    const where = [e.via, e.port].filter(Boolean).join(" · ");
    return (e.kind === "connected" ? __`${e.client} se připojil` : __`${e.client} se odpojil`) + (where ? ` – ${where}` : "");
  }
  if (e.cat === "wifi") return e.kind === "roamed" ? __`${e.client} přešel na ${dev}` + (e.a ? ` (${e.a} dBm)` : "")
    : __`${e.client}: neúspěšné přihlášení k Wi-Fi` + (e.text ? ` (${e.text})` : "");
  if (e.cat === "change") return (e.kind === "applied" ? __("změna provedena: ") : e.kind === "failed" ? __("změna selhala: ") : __("změna vrácena zpět: ")) + (e.text || "");
  return `${e.user || ""}: ${e.kind}`;
}

async function loadTimeline() {
  if (!tenantId || !siteId()) return;
  const qs = [tl.minutes ? "minutes=" + tl.minutes : "hours=" + tl.hours];
  if (tl.until) qs.push("until=" + encodeURIComponent(tl.until));
  if (tl.cats.size < TL_CATS.length) qs.push("cats=" + ([...tl.cats].join(",") || "none"));
  if (tl.device) qs.push("device=" + encodeURIComponent(tl.device));
  if (tl.mac) qs.push("mac=" + encodeURIComponent(tl.mac));
  try { tl.data = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/timeline?${qs.join("&")}`); }
  catch (e) { return msg(e.message, true); }
  renderTimeline();
}

function tlZoom(untilIso, minutes) { tl.until = untilIso; tl.minutes = minutes; loadTimeline(); }

function renderTimeline() {
  const d = tl.data; if (!d) return;
  segmented($("tl-range"), TL_RANGES, tl.minutes ? null : tl.hours, (h) => { tl.hours = h; tl.minutes = null; tl.until = null; loadTimeline(); });
  // zařízení
  const ds = $("tl-device"); ds.textContent = "";
  const all = el("option", __("všechna zařízení")); all.value = ""; ds.appendChild(all);
  for (const x of d.devices) { const o = el("option", x.name); o.value = x.id; ds.appendChild(o); }
  ds.value = tl.device;
  // kategorie
  const cb = $("tl-cats"); cb.textContent = "";
  for (const [k, label] of TL_CATS) {
    const b = el("button", label, "tl-cat tl-c-" + k + (tl.cats.has(k) ? " on" : "")); b.type = "button";
    b.setAttribute("aria-pressed", tl.cats.has(k) ? "true" : "false");
    b.addEventListener("click", () => { if (tl.cats.has(k)) tl.cats.delete(k); else tl.cats.add(k); loadTimeline(); });
    cb.appendChild(b);
  }
  // štítky aktivních omezení (klient, výběr času)
  const chips = $("tl-chips"); chips.textContent = "";
  const chip = (text, off) => { const b = el("button", text + " ×", "tl-chip"); b.type = "button"; b.addEventListener("click", off); chips.appendChild(b); };
  if (tl.mac) chip(__`klient ${tl.macName || tl.mac}`, () => { tl.mac = ""; tl.macName = ""; loadTimeline(); });
  if (tl.minutes) chip(__("vybraný úsek"), () => { tl.minutes = null; tl.until = null; loadTimeline(); });
  // graf: sloupec = úsek času, výška = počet událostí, barva = nejhorší závažnost; klik přiblíží
  const ch = $("tl-chart"); ch.textContent = "";
  const max = Math.max(1, ...d.buckets.map((b) => b.n));
  const stepMin = Math.max(1, Math.round(d.step_seconds / 60));
  d.buckets.forEach((b, i) => {
    const col = el("button", null, "tl-col tl-s-" + b.sev + (b.n ? "" : " empty")); col.type = "button";
    const bar = el("span"); bar.style.height = (b.n ? Math.max(8, Math.round(100 * b.n / max)) : 2) + "%"; col.appendChild(bar);
    const t0 = new Date(b.at), t1 = new Date(t0.getTime() + d.step_seconds * 1000);
    col.title = `${fmtTime(b.at)} – ${t1.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })}: ${b.n}`;
    col.disabled = !b.n || stepMin <= 2;
    col.addEventListener("click", () => tlZoom(t1.toISOString(), Math.max(2, stepMin)));
    ch.appendChild(col);
  });
  $("tl-from").textContent = fmtTime(d.since); $("tl-to").textContent = fmtTime(d.until);
  // seznam po dnech
  const q = gsNorm(tl.q).trim();
  const list = $("tl-list"); list.textContent = "";
  let day = "", shown = 0;
  for (const e of d.events) {
    const text = tlText(e);
    if (q && !gsNorm([text, e.device, e.client, e.mac, e.port, e.user].join(" ")).includes(q)) continue;
    const at = new Date(e.at), dkey = at.toLocaleDateString(LOCALE, { weekday: "long", day: "numeric", month: "long" });
    if (dkey !== day) { day = dkey; list.appendChild(el("h4", dkey, "tl-day")); }
    const row = el("div", null, "tl-row tl-s-" + e.sev);
    row.append(el("time", at.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", second: "2-digit" })), el("span", "", "tl-dot"));
    row.appendChild(el("span", (TL_CATS.find((c) => c[0] === e.cat) || [0, e.cat])[1], "tl-tag tl-c-" + e.cat));
    const body = el("span", null, "tl-body");
    if (e.device_id && e.cat !== "wifi") {
      const a = el("a", e.device); a.href = "#"; a.title = __("Otevřít zařízení");
      a.addEventListener("click", (ev) => { ev.preventDefault(); openDetail(e.device_id); });
      body.append(a, document.createTextNode(" · "));
    }
    body.appendChild(document.createTextNode(text));
    if (e.user && e.cat === "change") body.appendChild(el("small", " – " + e.user, "muted"));
    row.appendChild(body);
    const acts = el("span", null, "tl-acts");
    if (e.mac && !tl.mac) {
      const b = el("button", __("jen klient"), "small secondary"); b.type = "button"; b.title = __("Ukázat jen události tohoto klienta");
      b.addEventListener("click", () => { tl.mac = e.mac; tl.macName = e.client || e.mac; loadTimeline(); });
      acts.appendChild(b);
    }
    const around = el("button", __("okolí ±5 min"), "small secondary"); around.type = "button";
    around.title = __("Co všechno se dělo 5 minut před a po – napříč zařízeními i klienty");
    around.addEventListener("click", () => {
      tl.device = ""; tl.mac = ""; tl.macName = ""; tl.q = ""; $("tl-q").value = ""; tl.cats = new Set(TL_CATS.map((c) => c[0]));
      tlZoom(new Date(at.getTime() + 5 * 60000).toISOString(), 10);
    });
    acts.appendChild(around);
    row.appendChild(acts);
    list.appendChild(row);
    shown++;
  }
  if (!shown) list.appendChild(el("p", __("V tomto úseku se nic nestalo."), "muted"));
  $("tl-info").textContent = d.total > d.events.length ? __`zobrazeno posledních ${d.events.length} z ${d.total} událostí – zpřesni filtr nebo klikni na sloupec`
    : __`${shown} událostí`;
}

$("tl-device").addEventListener("change", () => { tl.device = $("tl-device").value; loadTimeline(); });
$("tl-q").addEventListener("input", () => { tl.q = $("tl-q").value; renderTimeline(); });
$("tl-refresh").addEventListener("click", () => { if (!tl.minutes) tl.until = null; loadTimeline(); });
// z detailu zařízení: časová osa jen pro toto zařízení
function openTimelineFor(deviceId) {
  tl.device = deviceId || ""; tl.mac = ""; tl.macName = ""; tl.minutes = null; tl.until = null;
  if (!$("detail").hidden) closeDetail();
  if ($("app").hidden) show("app");
  openSub("activity", "tl");
}
$("d-timeline").addEventListener("click", () => openTimelineFor(detailId));
$("cl-timeline").addEventListener("click", () => {
  const c = clEdit && clEdit.c; if (!c) return;
  $("cl-dlg").hidden = true;
  openTimelineFor("");
  tl.mac = c.mac; tl.macName = clientName(c); tl.hours = Math.max(tl.hours, 168);
  loadTimeline();
});

// --- dostupnost zařízení (Aktivita → Dostupnost): proužek dnů, procenta, výpadky ---------------------------

let availDays = 30;
function fmtSpan(sec) {
  if (!sec) return "–";
  if (sec < 90) return __`${Math.round(sec)} s`;
  if (sec < 5400) return __`${Math.round(sec / 60)} min`;
  if (sec < 172800) return __`${(sec / 3600).toFixed(1)} h`;
  return __`${(sec / 86400).toFixed(1)} d`;
}
const availClass = (p) => p === null || p === undefined ? "none" : p >= 99.9 ? "ok" : p >= 99 ? "warn" : "bad";
const fmtPct = (p) => p === null || p === undefined ? "–" : (p >= 99.995 ? "100" : p.toFixed(p >= 99 ? 2 : 1)) + " %";

async function loadAvail() {
  if (!tenantId || !siteId()) return;
  let d;
  try { d = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/availability?days=${availDays}`); }
  catch (e) { return msg(e.message, true); }
  segmented($("avail-range"), [[7, __("7 dní")], [30, __("30 dní")], [90, __("90 dní")]], availDays, (v) => { availDays = v; loadAvail(); });
  $("avail-sum").textContent = d.avg_pct === null ? "" : __`průměr lokality ${fmtPct(d.avg_pct)}`;
  const tb = $("avail-rows"); tb.textContent = "";
  const first = new Date(d.first_day + "T00:00:00Z");
  for (const x of d.devices) {
    const tr = el("tr"); tr.className = "clickable";
    const name = el("td"); name.append(el("strong", x.name), el("small", " " + (x.model || ""), "muted"));
    const strip = el("div", null, "avail-strip");
    x.daily.forEach((p, i) => {
      const c = el("span", null, "av-" + availClass(p));
      const day = new Date(first.getTime() + i * 86400000).toLocaleDateString(LOCALE, { day: "numeric", month: "numeric" });
      c.title = day + ": " + (p === null ? __("zařízení ještě nebylo přidané") : fmtPct(p));
      strip.appendChild(c);
    });
    const stripTd = el("td"); stripTd.appendChild(strip);
    const pct = el("td", fmtPct(x.pct), "num av-t-" + availClass(x.pct));
    const last = x.last[0];
    const lastTd = el("td", !last ? "–" : fmtTime(last.from) + " · " + (last.to ? fmtSpan(last.seconds) : __("trvá")), last && !last.to ? "av-t-bad" : "");
    tr.append(name, stripTd, pct, el("td", String(x.outages), "num"), el("td", fmtSpan(x.down_seconds), "num"),
              el("td", fmtSpan(x.longest_seconds), "num"), lastTd);
    tr.addEventListener("click", () => { tl.hours = Math.max(tl.hours, availDays >= 30 ? 720 : 168); openTimelineFor(x.id); });
    tb.appendChild(tr);
  }
  if (!d.devices.length) { const td = el("td", __("V lokalitě zatím nejsou žádná zařízení."), "muted"); td.colSpan = 7; const tr = el("tr"); tr.appendChild(td); tb.appendChild(tr); }
}

let plansOpen = 0, alertsOpen = [0, 0];
function activityBadge() { setBadge("activity", plansOpen + alertsOpen[0], alertsOpen[1] ? "bad" : "warn"); }

function setBadge(key, n, cls) {
  if (key === "activity") { const hb = $("hdr-bell-n"); hb.textContent = n ? String(n) : ""; hb.hidden = !n; hb.className = "hbell-n" + (cls ? " nb-" + cls : ""); }
  const b = navBadges[key]; if (!b) return;
  b.textContent = n ? String(n) : ""; b.hidden = !n; b.className = "nbadge" + (cls ? " nb-" + cls : "");
}

// panel zařízení: Přehled · Porty · Wi-Fi · Diagnostika a logy (ta má vlastní podzáložky)
const DETAIL_TABS = ["over", "ports", "manage", "wifi", "diag"];
const DIAG_TABS = ["sec", "vlan", "tools", "logs"];
const DETAIL_LOADERS = { over: (id) => loadControl(id), manage: (id) => loadControl(id), wifi: (id) => loadDevWifi(id), logs: (id) => loadLogs(id), tools: (id) => loadBw(id) };
function showDetailTab(key) {
  if (DIAG_TABS.includes(key)) { prefs.diagTab = key; key = "diag"; }
  if (!DETAIL_TABS.includes(key) || $("dt-" + key).hidden) key = "over";
  prefs.detailTab = key; savePrefs();
  for (const k of DETAIL_TABS) { $("dp-" + k).hidden = k !== key; $("dt-" + k).className = k === key ? "active" : ""; }
  const dk = DIAG_TABS.includes(prefs.diagTab) ? prefs.diagTab : "sec";
  for (const k of DIAG_TABS) { $("dp-" + k).hidden = k !== dk; $("dt-" + k).className = k === dk ? "active" : ""; }
  return key === "diag" ? dk : key;
}
function openDetailTab(key) {
  const shown = showDetailTab(key);
  if (DETAIL_LOADERS[shown] && detailId) DETAIL_LOADERS[shown](detailId);
}
for (const k of [...DETAIL_TABS, ...DIAG_TABS]) $("dt-" + k).addEventListener("click", () => openDetailTab(k));
// panel detailu: úzký vedle seznamu, tlačítkem jde rozšířit (volba se pamatuje)
function detailWide(on) {
  $("detail").className = "dev-drawer" + (on ? " wide" : "");
  $("d-wide").setAttribute("aria-pressed", String(!!on)); $("d-wide").title = on ? __("Zúžit panel") : __("Rozšířit panel");
}
$("d-wide").addEventListener("click", () => { prefs.detailWide = !prefs.detailWide; savePrefs(); detailWide(prefs.detailWide); });
detailWide(!!prefs.detailWide);

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
const PORT_VIEWS = [["link", __("Přehled")], ["pvid", __("Native VLAN")], ["vlan", __("VLAN tagování")], ["poe", "PoE"],
  ["err", __("Chyby")], ["anom", __("Anomálie")]];
const PORT_OPTS = [["portDev", __("Připojená zařízení")], ["portTable", __("Tabulka portů")]];
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
  if (!portCtx || portCtx.devId !== devId) { portPick.clear(); portMulti = false; profCache = null; $("port-sel").hidden = true; }
  const bonds = d.bonds || [];
  const bondOf = {};
  for (const b of bonds) for (const m of b.slaves) bondOf[m] = b.name;
  portCtx = { devId, phys, cu: bySfp(false), sfp: bySfp(true), vnames: d.vlan_names || {}, bonds, bondOf, siteVlans: d.site_vlans,
              bondIfaces: (d.interfaces || []).filter((i) => i.type === "bond") };
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
  const m = el("button", __("Vybrat více portů"), "toggle" + (portMulti ? " on" : ""));
  m.type = "button"; m.setAttribute("aria-pressed", portMulti ? "true" : "false");
  m.title = __("Hromadná úprava, profil na víc portů, LACP (taky Ctrl/Shift + klik)");
  m.addEventListener("click", () => { portMulti = !portMulti; if (!portMulti) portPick.clear(); drawPorts(); drawPortPick(); });
  box.appendChild(m);
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
  drawSiteVlans(box);
  if (!c.phys.length) { box.appendChild(el("p", __("Žádné fyzické porty v inventuře"), "muted")); return; }
  if (view === "vlan") return drawVlanMatrix(box);
  const row = el("div", null, "port-row");
  const labels = portOpt("portDev");
  const two = c.cu.length > 8;
  for (const group of [c.cu, c.sfp]) if (group.length) row.appendChild(portGroup(group, (i) => portTile(i, view), labels, two));
  box.appendChild(row);
  drawBonds(box);
}

// --- VLAN lokality: po adopci na switchi chybí – jedním plánem je přidat (uplink tagovaně) ---------
function drawSiteVlans(box) {
  const sv = portCtx.siteVlans;
  if (!sv || (!sv.missing.length && sv.vlan_filtering)) return;
  const b = el("div", null, "banner warn");
  const parts = [];
  if (sv.missing.length) parts.push(__`na zařízení chybí VLAN ${sv.missing.join(", ")}`);
  if (!sv.vlan_filtering) parts.push(__`bridge ${sv.bridge} nemá zapnuté vlan-filtering`);
  b.appendChild(el("span", __`Lokalita má ${sv.site_vlans.length} sítí – ${parts.join(", ")}. `));
  const go = el("button", __("Připravit VLAN lokality"), "small"); go.type = "button";
  go.disabled = !canManage();
  go.title = __("Přidá všechny sítě lokality do bridge, na uplinku tagovaně, a zapne vlan-filtering. Porty zůstanou ve VLAN 1; když se switch do 5 min neozve, vše se vrátí.");
  go.onclick = async () => {
    if (!(await ask(__`Přidat VLAN lokality (${sv.site_vlans.join(", ")}) na ${sv.bridge}? Uplink je dostane tagovaně, porty zůstanou ve VLAN 1. Když se switch do 5 min neozve, změna se vrátí.`))) return;
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/bonds`, { kind: "site_vlans", apply: true });
      msg(__`Odesláno: ${r.summary}${(r.warnings || []).length ? " – " + r.warnings.join("; ") : ""}`); loadPlans();
    } catch (e) { msg(e.message, true); }
  };
  b.appendChild(go);
  box.appendChild(b);
}

// --- LACP bondy: řádek pod porty, klik = nastavení VLAN na bondu, pravé tlačítko = akce --------
function drawBonds(box) {
  const c = portCtx;
  if (!c.bonds.length) return;
  if (!profCache) api("GET", `/tenants/${tenantId}/port-profiles`).then((r) => { profCache = r; }).catch(() => { profCache = []; });
  const wrap = el("div", null, "bond-row");
  wrap.appendChild(el("span", "LACP", "bond-h"));
  for (const b of c.bonds) {
    const iface = c.bondIfaces.find((i) => i.name === b.name) || { name: b.name };
    const chip = el("button", null, "bond-chip " + (b.running ? "up" : "down") + (trafficState.iface === b.name ? " selected" : ""));
    chip.type = "button";
    chip.append(el("b", b.name), el("span", ` · ${b.slaves.map((m) => portNum(m)).join(", ")}`, "muted"));
    if (b.mlag_id) chip.appendChild(el("span", ` · MLAG ${b.mlag_id}`, "muted"));
    chip.title = __`${b.name} (802.3ad) – porty ${b.slaves.join(", ")}${b.running ? "" : __(" – NEBĚŽÍ (protistrana bez LACP?)")}\nklik = VLAN a profil bondu · pravé tlačítko = akce`;
    chip.addEventListener("click", () => selectPortTraffic(b.name));
    chip.addEventListener("contextmenu", (ev) => ctxMenu(ev, `${b.name} · ${b.slaves.join(", ")}`, bondMenu(b, iface)));
    wrap.appendChild(chip);
  }
  box.appendChild(wrap);
}

function bondMenu(b, iface) {
  const can = canManage();
  const profs = profCache || [];
  if (!profCache) api("GET", `/tenants/${tenantId}/port-profiles`).then((r) => { profCache = r; }).catch(() => { profCache = []; });
  return [
    !b.running ? { label: __("⚠ bond neběží – protistrana musí mít LACP (802.3ad)"), disabled: true } : null,
    { label: __("VLAN a nastavení bondu…"), key: "Enter", act: () => selectPortTraffic(b.name) },
    { label: __("Graf provozu"), act: () => showTraffic(detailId, b.name) },
    profs.length ? { head: __("Profil (VLAN)") } : null,
    ...profs.slice(0, 8).map((pf) => ({ label: pf.name, disabled: !can, act: () => bulkPorts([b.name], { profile_id: pf.id }) })),
    { sep: true },
    b.mtc ? { label: __("Zrušit bond (porty zpět do bridge)"), danger: true, disabled: !can, act: () => removeBond(b) }
          : { label: __("Bond nevytvořil controller – ruší se v RouterOS"), disabled: true },
  ];
}

async function removeBond(b) {
  if (!(await ask(__`Zrušit bond ${b.name}? Porty ${b.slaves.join(", ")} se vrátí do bridge s VLAN bondu. Vypni LACP i na protistraně.`))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/bonds`, { kind: "bond_remove", name: b.name, apply: true });
    msg(__`Odesláno: ${r.summary}`); showApply(r.apply); loadPlans();
  } catch (e) { msg(e.message, true); }
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
  if (!list.length) { box.appendChild(el("p", vids.size ? __("Žádná VLAN neodpovídá filtru") : __("Porty nejsou v bridgi s VLAN"), "muted")); return; }
  for (const vid of list) {
    const members = c.phys.filter((i) => vlanMember(i, vid)).length;
    const r = el("div", null, "vlan-row");
    const head = el("div", null, "vlan-head");
    const name = el("div", vlanTitle(vid), "vlan-name");
    name.appendChild(el("span", ` · ${nPorts(members)}`, "muted"));
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
  t.title = portTooltip(i) + __("\nCtrl/Shift + klik = výběr více portů · pravé tlačítko = akce");
  t.addEventListener("contextmenu", (ev) => ctxMenu(ev, portMenuTitle(i), portMenu(i)));
  t.addEventListener("click", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || portMulti) { togglePortPick(i.name); return; }
    selectPortTraffic(i.name);
  });
  if (trafficState.iface === i.name) t.className += " selected";
  if (portPick.has(i.name)) t.className += " picked";
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
const KIND_LABEL = { router: "router", switch: "switch", ap: "AP", camera: __("kamera"), pc: __("počítač"), server: "server",
  nas: "NAS", printer: __("tiskárna"), phone: __("telefon"), tv: "TV", unknown: __("zařízení") };

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
    n.count > 1 ? __`${n.count} MAC adres za portem` : ""].filter(Boolean).join("\n");
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
    // přehled: barva = linka, odznaky v rozích = PoE, uplink, STP, chyby/anomálie
    t.className += " " + lc;
    for (const b of portBadges(i)) {
      const s = el("span", b.text, "pb " + b.pos + " " + b.cls);
      s.setAttribute("aria-label", b.title);
      t.appendChild(s);
    }
    const lv = portLevel(i);
    if (lv) t.className += " lv-" + lv;
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

// stav portu pro přehled: problémy (STP, smyčka, chyby, anomálie, half-duplex) a informace (PoE, uplink)
function portIssues(i) {
  const p = i.port || {}, a = i.anomaly, out = [];
  const e = (Number(i.rx_error) || 0) + (Number(i.tx_error) || 0);
  if (p.loop) out.push(["bad", __("smyčka – loop protect port vypnul")]);
  if (p.poe && /fault|overload|short|error/i.test(p.poe)) out.push(["bad", __`PoE porucha (${p.poe})`]);
  if (p.stp_forwarding === false && i.running) out.push(["warn", __`STP blokuje (${STP_LABEL[p.stp_role] || p.stp_role || "?"})`]);
  if (p.full_duplex === false && i.running) out.push(["warn", "half-duplex"]);
  if (e > 100) out.push(["bad", __`${e} chyb RX/TX`]); else if (e > 0) out.push(["warn", __`${e} chyb RX/TX`]);
  if (a && a.score >= 50) out.push(["bad", __`anomálie ${a.score}: ${a.reasons.join(", ")}`]);
  else if (a && a.score >= 20) out.push(["warn", __`anomálie ${a.score}: ${a.reasons.join(", ")}`]);
  if ((Number(i.link_downs) || 0) >= 3) out.push(["warn", __`${i.link_downs}× výpadek linky`]);
  return out;
}

function portLevel(i) {
  const is = portIssues(i);
  return is.some(([l]) => l === "bad") ? "bad" : is.length ? "warn" : "";
}

function portBadges(i) {
  const p = i.port || {}, out = [];
  const w = Number(p.poe_power) || 0;
  const poeOn = w > 0 || (p.poe && /powered-on/.test(p.poe));
  if (p.poe && /fault|overload|short|error/i.test(p.poe)) out.push({ pos: "tl", cls: "bad", text: "⚡", title: __("PoE porucha") });
  else if (poeOn) out.push({ pos: "tl", cls: w >= 15 ? "poe3" : "poe", text: "⚡", title: `PoE ${w ? w.toFixed(1) + " W" : __("zapnuto")}` });
  if (isUplink(i)) out.push({ pos: "tr", cls: "up", text: "↑", title: "uplink" });
  else if (portCtx && portCtx.bondOf && portCtx.bondOf[i.name]) out.push({ pos: "tr", cls: "bond", text: "⛓", title: __`v bondu ${portCtx.bondOf[i.name]}` });
  if (p.loop) out.push({ pos: "br", cls: "bad", text: "↻", title: __("smyčka") });
  else if (p.stp_forwarding === false && i.running) out.push({ pos: "br", cls: "stp", text: "⊘", title: __("STP blokuje") });
  const lv = portLevel(i);
  if (lv && !p.loop) out.push({ pos: "bl2", cls: lv, text: "!", title: portIssues(i).map(([, t]) => t).join(", ") });
  if (p.full_duplex === false && i.running) out.push({ pos: "mid", cls: "hd", text: "HD", title: "half-duplex" });
  return out;
}

function vlanTile(i, vid) {
  const m = vlanMember(i, vid);
  return el("button", null, "port " + (m === "tagged" ? "vt" : m === "native" ? "vn" : "down"));
}

function portTooltip(i) {
  const p = i.port || {}, n = i.connected, a = i.anomaly;
  return [
    i.name + (i.comment ? ` – ${i.comment}` : ""),
    ...portIssues(i).map(([l, t]) => (l === "bad" ? "⛔ " : "⚠ ") + t),
    i.disabled ? __("vypnutý") : (i.running ? __`linka ${p.rate || "ok"}${p.full_duplex === false ? " half-duplex" : ""}` : __("bez linky")),
    p.pvid ? `PVID ${p.pvid}` + (p.tagged && p.tagged.length ? __`, tagované ${p.tagged.join(",")}` : "") : "",
    p.stp_role ? `STP ${p.stp_role}${p.stp_forwarding === false ? __(" (blokuje)") : ""}` : "",
    p.poe ? `PoE ${p.poe}${p.poe_power ? " " + p.poe_power + " W" : ""}` : "",
    p.loop ? __("SMYČKA – loop protect port vypnul") : "",
    p.sfp_rx_power ? `SFP RX ${p.sfp_rx_power} dBm` : "",
    n ? __`připojeno: ${n.name}${n.ip ? " " + n.ip : ""}` : "",
    a ? __`anomálie ${a.score}: ${a.reasons.join(", ")}` : "",
    __`chyby RX/TX ${i.rx_error || 0}/${i.tx_error || 0}, výpadky linky ${i.link_downs || 0}`,
  ].filter(Boolean).join("\n");
}

const LEGENDS = {
  link: [["gbe", "GbE+"], ["fe", "FE"], ["down", __("bez linky")], ["off", __("vypnutý")], [null, "⚡ PoE"], [null, "↑ uplink"],
    [null, __("⊘ STP blokuje")], [null, __("↻ smyčka")], ["lv-warn", __("! varování")], ["lv-bad", __("! problém (chyby, anomálie, PoE porucha)")]],
  pvid: [["plain", __("číslo = native VLAN (PVID)")], ["dim", __("port bez linky")]],
  vlan: [["vt", __("tagovaná")], ["vn", __("native (netagovaná)")], ["down", __("není členem")]],
  poe: [["poe1", "< 5 W"], ["poe2", "5–15 W"], ["poe3", "≥ 15 W"], ["plain", __("PoE bez odběru")], ["down", __("bez PoE")]],
  err: [["plain", __("bez chyb")], ["err1", __("chyby")], ["err2", __("> 100 chyb")], [null, __("↯ výpadky linky (od 3)")]],
  anom: [["plain", __("0 = v pořádku")], ["poe1", "1–19"], ["err1", "20–49"], ["err2", "50–100"], [null, __("skóre za 24 h: výpadky, chyby, duplex")]],
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
    lg.appendChild(el("b", __`celkem ${total.toFixed(1)} W`));
  }
  if (view === "anom") {
    const worst = portCtx.phys.reduce((a, i) => Math.max(a, i.anomaly ? i.anomaly.score : 0), 0);
    lg.appendChild(el("b", __`nejhorší port ${worst}`));
  }
  if (view === "link") {
    const bad = portCtx.phys.filter((i) => portLevel(i)).length;
    if (bad) lg.appendChild(el("b", __`${bad} ${bad === 1 ? __("port potřebuje") : __("porty potřebují")} pozornost`));
  }
  lg.appendChild(el("span", __("klik = nastavení a graf · pravé tlačítko = akce"), "muted"));
}

// --- tabulka portů (jako UniFi) ---
const PT_COLS = [
  ["port", __("Port"), (i) => portNum(i.name) + (/^(sfp|qsfp)/.test(i.name) ? 1000 : 0)],
  ["name", __("Název"), (i) => i.comment || ""],
  ["anom", __("Anomálie"), (i) => (i.anomaly ? i.anomaly.score : 0)],
  ["stp", "STP", (i) => (i.port || {}).stp_role || ""],
  ["conn", __("Připojeno"), (i) => (i.connected ? i.connected.name : "")],
  ["poe", "PoE", (i) => Number((i.port || {}).poe_power) || ((i.port || {}).poe ? 0.001 : -1)],
  ["speed", __("Rychlost"), (i) => (i.running ? rateNum((i.port || {}).rate) : -1)],
  ["mac", "MAC", (i) => (i.connected ? i.connected.mac || "" : "")],
  ["ip", "IP", (i) => ipNum(i.connected && i.connected.ip)],
  ["prof", __("Profil"), (i) => i.profile || ""],
  ["pvid", __("Native VLAN"), (i) => Number((i.port || {}).pvid) || 0],
  ["tag", __("Tagované"), (i) => ((i.port || {}).tagged || []).join(",")],
];
const STP_LABEL = { "root-port": "root port", "designated-port": "designated", "alternate-port": "alternate",
  "backup-port": "backup", disabled: __("vypnuto") };

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
    const r = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), LOCALE);
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
    tr.appendChild(el("td", p.poe ? (w > 0 ? `${w.toFixed(1)} W` : (/off/.test(p.poe) ? __("vyp.") : "0 W")) : "–"));
    const sp = el("td", i.disabled ? __("vypnutý") : i.running ? (p.rate || __("linka")) + (p.full_duplex === false ? " HD" : "") : "–",
      i.running ? (linkClass(i) === "fe" ? "fe" : "gbe") : "muted");
    tr.appendChild(sp);
    tr.appendChild(el("td", n && n.mac ? n.mac.toLowerCase() : "", "mono"));
    tr.appendChild(el("td", n && n.ip ? n.ip : ""));
    tr.appendChild(el("td", i.profile || "–", i.profile ? "" : "muted"));
    tr.appendChild(el("td", p.pvid ? vlanTitle(String(p.pvid)) : "–"));
    tr.appendChild(el("td", (p.tagged || []).join(", ") || "–", (p.tagged || []).length ? "" : "muted"));
    tr.addEventListener("click", () => selectPortTraffic(i.name));
    tr.addEventListener("contextmenu", (ev) => ctxMenu(ev, portMenuTitle(i), portMenu(i)));
    tb.appendChild(tr);
  }
}

$("d-vlan-filter").addEventListener("input", () => { if (portCtx) drawPorts(); });

// --- graf provozu (celé zařízení / port; provoz, chyby, zahozené) ------------------------------

const TR_METRICS = [["bps", __("Provoz")], ["err", __("Chyby")], ["drop", __("Zahozené")]];
const TR_DIRS = [["all", __("Vše")], ["rx", __("↓ Přijato")], ["tx", __("↑ Odesláno")]];
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

// --- výběr více portů: hromadná úprava, profil, LACP ------------------------------------------

const portPick = new Set();
let portMulti = false;
let profCache = null;

function togglePortPick(name) {
  if (portPick.has(name)) portPick.delete(name); else portPick.add(name);
  drawPorts();
  drawPortPick();
}

// české skloňování podle počtu; překlad má pro každý tvar vlastní text
const nPorts = (n) => (n === 1 ? __`${n} port` : n >= 2 && n <= 4 ? __`${n} porty` : __`${n} portů`);
const nSw = (n) => (n === 1 ? __`${n} switch` : __`${n} switche`);
const nSwitches = (n) => (n === 1 ? __`${n} switchi` : __`${n} switchích`);

function clearPortPick() { portPick.clear(); portMulti = false; if (portCtx) drawPorts(); drawPortPick(); }

async function drawPortPick() {
  const box = $("port-sel");
  box.textContent = "";
  box.hidden = !portPick.size && !portMulti;
  if (box.hidden) return;
  const names = [...portPick].sort((a, b) => portNum(a) - portNum(b));
  const head = el("div", null, "ps-head");
  head.append(el("b", names.length ? __`Vybráno ${names.length}: ${names.join(", ")}` : __("Klikni na porty, které chceš vybrat")));
  const x = el("button", __("Zrušit výběr"), "small secondary"); x.type = "button"; x.onclick = clearPortPick;
  head.appendChild(x);
  box.appendChild(head);
  if (!names.length) return;
  if (!profCache) { try { profCache = await api("GET", `/tenants/${tenantId}/port-profiles`); } catch (e) { profCache = []; } }
  const bar = el("div", null, "ps-bar");
  const sel = el("select"); sel.setAttribute("aria-label", __("Profil"));
  const o0 = el("option", __("— profil —")); o0.value = ""; sel.appendChild(o0);
  for (const pf of profCache) { const o = el("option", pf.name); o.value = String(pf.id); sel.appendChild(o); }
  const useP = el("button", __("Použít profil"), "small"); useP.type = "button";
  useP.onclick = () => { if (!sel.value) return msg(__("Vyber profil"), true); bulkPorts(names, { profile_id: Number(sel.value) }); };
  bar.append(sel, useP);
  const act = (label, desired, cls = "small secondary") => {
    const b = el("button", label, cls); b.type = "button";
    b.onclick = () => bulkPorts(names, { desired });
    bar.appendChild(b);
  };
  act(__("PoE zapnout"), { poe: "auto-on" }); act(__("PoE vypnout"), { poe: "off" });
  act(__("Porty vypnout"), { disabled: true }); act(__("Porty zapnout"), { disabled: false });
  if (names.length >= 2) {
    const lacp = el("button", __("LACP z vybraných"), "small"); lacp.type = "button";
    lacp.onclick = () => openLacp(names);
    bar.appendChild(lacp);
  }
  box.appendChild(bar);
}

// --- kontextové menu portu: vše k portu na jednom místě, bez hledání v záložkách -------------

function portMenuTitle(i) {
  const n = i.connected;
  return `${i.name}${i.comment ? " – " + i.comment : ""}${n ? " · " + n.name : ""}`;
}

function portCmd(i, kind, question) {
  return async () => {
    if (!(await ask(question))) return;
    try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/commands`, { kind, port: i.name }); msg(__("Odesláno zařízení")); }
    catch (e) { msg(e.message, true); }
  };
}

function portDesired(i, desired, what) {
  return async () => {
    if (!(await ask(__`${what} na ${i.name}? Změna se použije s automatickým návratem.`))) return;
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/ports/${encodeURIComponent(i.name)}/plan`,
                          { desired, apply: true });
      msg(__`Odesláno: ${r.summary}`); for (const w of r.warnings || []) msg(w, true); loadPlans();
    } catch (e) { msg(e.message, true); }
  };
}

async function portTool(i, tool, params) {
  await loadPortEditor(i.name);
  const out = $("port-edit").querySelector(".tool-out");
  if (out) runTool(tool, params, out);
}

function portMenu(i) {
  const p = i.port || {}, n = i.connected, can = canManage();
  const uplink = isUplink(i);
  const poeOn = p.poe && !/off/.test(p.poe);
  const profs = profCache || [];
  if (!profCache) api("GET", `/tenants/${tenantId}/port-profiles`).then((r) => { profCache = r; }).catch(() => { profCache = []; });
  const issues = portIssues(i);
  return [
    ...issues.map(([l, t]) => ({ label: (l === "bad" ? "⛔ " : "⚠ ") + t, disabled: true })),
    issues.length ? { sep: true } : null,
    { label: __("Upravit port…"), key: "Enter", act: () => selectPortTraffic(i.name) },
    { label: __("Graf provozu"), act: () => { showTraffic(detailId, i.name); const t = $("d-traffic"); if (t && t.scrollIntoView) t.scrollIntoView({ behavior: "smooth", block: "start" }); } },
    profs.length ? { head: __("Profil") } : null,
    ...profs.slice(0, 8).map((pf) => ({ label: pf.name, checked: i.profile === pf.name, disabled: !can,
                                        act: () => bulkPorts([i.name], { profile_id: pf.id }) })),
    { head: __("Port") },
    i.disabled
      ? { label: __("Zapnout port"), disabled: !can, act: portDesired(i, { disabled: false }, __("Zapnout port")) }
      : { label: __("Vypnout port"), disabled: !can, danger: uplink, hint: uplink ? __("uplink – zařízení bude nedostupné, změna se vrátí") : "",
          act: portDesired(i, { disabled: true }, uplink ? __("Vypnout UPLINK (zařízení se odpojí, změna se sama vrátí)") : __("Vypnout port")) },
    { label: __("Restart linky"), disabled: !can || i.disabled,
      act: portCmd(i, "port_bounce", __`Restartovat linku ${i.name}? Port se na 5 s vypne.` + (uplink ? __(" Je to uplink – zařízení bude chvíli nedostupné.") : "")) },
    p.poe != null ? { head: "PoE" } : null,
    p.poe != null && poeOn ? { label: __("Restart PoE (power-cycle)"), disabled: !can,
      act: portCmd(i, "poe_cycle", __`Vypnout a zapnout napájení na ${i.name}? Zařízení na portu se restartuje.`) } : null,
    p.poe != null ? (poeOn ? { label: __("Vypnout PoE"), disabled: !can, act: portDesired(i, { poe: "off" }, __("Vypnout PoE")) }
                           : { label: __("Zapnout PoE"), disabled: !can, act: portDesired(i, { poe: "auto-on" }, __("Zapnout PoE")) }) : null,
    { head: __("Diagnostika") },
    n && n.ip ? { label: __`Ping ${n.ip}`, act: () => portTool(i, "ping", { address: n.ip, count: 4 }) } : null,
    n && n.mac && !n.managed ? { label: __("Probudit (Wake-on-LAN)"), act: () => portTool(i, "wol", { mac: n.mac }) } : null,
    { label: __("Test kabelu"), disabled: !can, act: async () => {
      if (await ask(__`Test kabelu na ${i.name}? Na některých switchích se linka během testu na chvíli přeruší.`)) portTool(i, "cable_test", { port: i.name });
    } },
    { label: __("Blikat LED (15 s)"), act: () => portTool(i, "blink", { port: i.name, seconds: 15 }) },
    { sep: true },
    { label: portPick.has(i.name) ? __("Odebrat z výběru") : __("Přidat do výběru (hromadně, LACP)"), act: () => togglePortPick(i.name) },
    n && n.mac ? { label: __("Kopírovat MAC ") + n.mac.toLowerCase(), act: () => copyText(n.mac.toLowerCase(), "MAC") } : null,
  ];
}

// --- dialog LACP: porty libovolně (nemusí být vedle sebe), jen 802.3ad, VLAN rovnou na bondu ----
let lacpNames = [];
async function openLacp(names) {
  lacpNames = names;
  if (!profCache) { try { profCache = await api("GET", `/tenants/${tenantId}/port-profiles`); } catch (_e) { profCache = []; } }
  $("lacp-ports").textContent = names.join(", ");
  const sel = $("lacp-prof"); sel.textContent = "";
  for (const pf of profCache || []) { const o = el("option", pf.name); o.value = String(pf.id); sel.appendChild(o); }
  $("lacp-v-prof").disabled = !(profCache || []).length;
  $("lacp-v-copy").checked = true;
  $("lacp-copy-from").textContent = names[0];
  $("lacp-pvid").value = ""; $("lacp-tagged").value = "";
  lacpUi();
  $("lacp-dlg").hidden = false;
}
function lacpUi() {
  $("lacp-prof").disabled = !$("lacp-v-prof").checked;
  $("lacp-manual").hidden = !$("lacp-v-man").checked;
}
for (const id of ["lacp-v-copy", "lacp-v-prof", "lacp-v-man"]) $(id).addEventListener("change", lacpUi);
$("lacp-cancel").addEventListener("click", () => { $("lacp-dlg").hidden = true; });
$("lacp-ok").addEventListener("click", async () => {
  const body = { kind: "lacp", ports: lacpNames, apply: true, hash: $("lacp-hash").value, lacp_rate: $("lacp-rate").value };
  if ($("lacp-v-prof").checked) body.profile_id = Number($("lacp-prof").value);
  if ($("lacp-v-man").checked) {
    const tagged = $("lacp-tagged").value.split(/[ ,;]+/).filter(Boolean);
    body.vlan = { tagged };
    if ($("lacp-pvid").value) body.vlan.pvid = Number($("lacp-pvid").value);
  }
  if (!(await ask(__`Spojit ${lacpNames.join(", ")} do LACP bondu (802.3ad)? Protistrana musí mít LACP taky; ` +
                  __("když se zařízení do 5 minut neozve, změna se sama vrátí.")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/bonds`, body);
    $("lacp-dlg").hidden = true;
    msg(__`Odesláno: ${r.summary}`); for (const w of r.warnings || []) msg(w, true);
    showApply(r.apply); clearPortPick(); loadPlans();
  } catch (e) { msg(e.message, true); }
});

async function bulkPorts(names, body) {
  const what = body.profile_id ? __`profil „${(profCache.find((p) => p.id === body.profile_id) || {}).name}“` : __("změnu");
  if (!(await ask(__`Použít ${what} na ${nPorts(names.length)}? ` +
                  __("Jeden plán s automatickým návratem.")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/ports/bulk`, { targets: [{ device_id: detailId, ports: names }], ...body, apply: true });
    for (const x of r.results) {
      if (x.error) msg(x.error, true);
      else { msg(__`Odesláno: ${x.summary || __("VLAN portů podle sítí lokality")}`); for (const w of x.warnings || []) msg(w, true); showApply(x.apply); }
    }
    clearPortPick(); loadPlans();
  } catch (e) { msg(e.message, true); }
}

async function runTool(tool, params, out) {
  out.hidden = false; out.textContent = __("Běží…");
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/tools/${tool}`, params);
    out.textContent = toolText(r);
  } catch (e) { out.textContent = e.message; }
}

function toolText(r) {
  if (r.tool === "ping") {
    const lines = r.replies.map((x) => `${x.seq ?? ""}  ${x.host || ""}  ${x.time_ms != null ? x.time_ms + " ms" : x.status || "timeout"}`);
    lines.push(__`odesláno ${r.sent}, přijato ${r.received}, ztráta ${r.loss_pct ?? "?"} %` +
               (r.avg_ms != null ? __` · min/prům/max ${r.min_ms}/${r.avg_ms}/${r.max_ms} ms` : ""));
    return lines.join("\n");
  }
  if (r.tool === "traceroute") return r.hops.map((h) => `${String(h.hop).padStart(2)}  ${h.address || "*"}  ${h.avg_ms != null ? h.avg_ms + " ms" : ""}  ${h.loss || ""}`).join("\n") || __("bez odpovědi");
  if (r.tool === "cable_test") {
    if (!r.pairs.length) return __`stav: ${r.status || "?"}` + (r.status === "link-ok" ? __(" (linka běží – páry jde měřit jen bez linky)") : "");
    return __`stav: ${r.status || "?"}\n` + r.pairs.map((p, k) => __`pár ${k + 1}: ${p.status}${p.length_m != null ? ` (${p.length_m} m)` : ""}`).join("\n");
  }
  return __("Hotovo");
}

$("d-tools-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const tool = $("d-tool").value, arg = $("d-tool-arg").value.trim();
  runTool(tool, tool === "wol" ? { mac: arg } : { address: arg, count: 4 }, $("d-tool-out"));
});

// --- editor portu (změny s automatickým návratem) ---------------------------------------------

async function loadPortEditor(port) {
  const box = $("port-edit");
  let st;
  try { st = await api("GET", `/tenants/${tenantId}/devices/${detailId}/ports/${encodeURIComponent(port)}`); }
  catch (e) { box.hidden = true; return; }
  box.hidden = false; box.textContent = "";
  const head = el("div", null, "pe-head");
  head.append(el("b", __`Port ${st.port}`), el("span", st.comment ? ` – ${st.comment}` : "", "muted"));
  if (st.uplink) head.appendChild(el("span", __("uplink – změna může odpojit zařízení (vrátí se samo)"), "badge warnb"));
  const close = el("button", "×", "small secondary pe-close"); close.type = "button";
  close.onclick = () => { box.hidden = true; };
  head.appendChild(close);
  box.appendChild(head);
  if (st.module_mode !== "manage") {
    const b = el("div", null, "banner info");
    b.appendChild(document.createTextNode(__("Správa portů je na tomto zařízení vypnutá (jen sleduje). ")));
    const en = el("button", __("Povolit správu portů"), "small"); en.type = "button";
    en.onclick = async () => {
      if (!(await ask(__("Povolit controlleru měnit porty tohoto zařízení? Každá změna se aplikuje s automatickým návratem.")))) return;
      try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/modules`, { module: "ports", mode: "manage" }); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    b.appendChild(en);
    box.appendChild(b);
  }
  const f = el("div", null, "pe-grid");
  const field = (label, input) => { const l = el("label", null, "pe-field"); l.append(el("span", label), input); f.appendChild(l); return input; };
  const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = !st.disabled;
  field(__("Zapnutý"), enabled);
  const comment = el("input"); comment.maxLength = 80; comment.value = st.comment || ""; comment.placeholder = __("např. kamera vjezd");
  if (st.is_bond) {
    comment.disabled = true;
    f.appendChild(el("p", __`LACP bond (802.3ad) z portů ${st.slaves.join(", ")}. VLAN, PVID a profil se nastavují tady na bondu.`, "muted"));
  } else field(__("Popis"), comment);
  let poe = null;
  if (st.poe !== null) {
    poe = el("select");
    for (const [v, l] of [["auto-on", "auto"], ["forced-on", __("vždy zapnuto")], ["off", __("vypnuto")]]) { const o = el("option", l); o.value = v; poe.appendChild(o); }
    poe.value = st.poe;
    field("PoE", poe);
  }
  let loop = null;
  if (st.loop_protect) {
    loop = el("select");
    for (const [v, l] of [["default", __("výchozí")], ["on", __("zapnuto")], ["off", __("vypnuto")]]) { const o = el("option", l); o.value = v; loop.appendChild(o); }
    loop.value = st.loop_protect;
    field(__("Loop protect"), loop);
    if (st.loop_status === "disable") f.appendChild(el("p", __("Loop protect port právě vypnul – za portem je smyčka."), "sev-error"));
  }
  let wdOn = null, wdAddr = null, wdTime = null;
  if (st.poe_watchdog) {
    const w = st.poe_watchdog;
    const wb = el("div", null, "pe-wd");
    wdOn = el("input"); wdOn.type = "checkbox"; wdOn.checked = !!w.enabled;
    wdAddr = el("input"); wdAddr.size = 17; wdAddr.placeholder = __("IP nebo MAC zařízení");
    wdAddr.value = w.address || st.watchdog_suggest || "";
    wdTime = el("input"); wdTime.type = "number"; wdTime.min = 10; wdTime.max = 3600; wdTime.value = w.enabled ? w.timeout : 60;
    const l = el("label", null, "check"); l.append(wdOn, document.createTextNode(__(" restartovat napájení, když ")));
    wb.append(l, wdAddr, document.createTextNode(__(" neodpoví ")), wdTime, document.createTextNode(" s"));
    if (!w.address && st.watchdog_suggest) wb.appendChild(el("span", __` (navrženo podle zařízení na portu)`, "muted"));
    field(__("PoE hlídač"), wb);
  }
  if (st.bond) f.appendChild(el("p", __`Port je v bondu ${st.bond} – VLAN a PVID se nastavují na bondu.`, "muted"));
  let pvid = null, edge = null, tagBox = null, addVlan = null, lockIn = null;
  const tagChecks = [];
  if (st.bridge) {
    pvid = el("input"); pvid.type = "number"; pvid.min = 1; pvid.max = 4094; pvid.value = st.pvid || 1;
    field(__("Native VLAN (PVID)"), pvid);
    edge = el("select");
    for (const [v, l] of [["auto", "auto"], ["yes", __("ano (koncové zařízení)")], ["no", __("ne")]]) { const o = el("option", l); o.value = v; edge.appendChild(o); }
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
    addVlan = el("input"); addVlan.placeholder = __("další VLAN, např. 50,60"); addVlan.size = 14;
    tagBox.appendChild(addVlan);
    field(__("Tagované VLAN"), tagBox);
    if ((st.tagged_locked || []).length)
      f.appendChild(el("p", __`Řádky VLAN ${st.tagged_locked.join("; ")} obsahují víc ID – ty upravuj v RouterOS.`, "muted"));
    if (!st.vlan_filtering) f.appendChild(el("p", __("Bridge nemá zapnuté vlan-filtering – PVID a tagování se zatím neuplatní."), "sev-warning"));
    lockIn = el("input"); lockIn.size = 17; lockIn.placeholder = __("MAC – prázdné = odemčeno"); lockIn.value = st.lock || "";
    field(__("Zámek portu na MAC"), lockIn);
  }
  box.appendChild(f);
  // profily portů: výběr vyplní formulář, použití jde stejnou cestou (plán s návratem)
  if (st.bridge || poe) {
    const pr = el("div", null, "pe-prof");
    pr.appendChild(el("span", __("Profil portu"), "muted"));
    const sel = el("select"); sel.setAttribute("aria-label", __("Profil portu"));
    const o0 = el("option", __("— vlastní nastavení —")); o0.value = ""; sel.appendChild(o0);
    for (const pf of st.profiles || []) { const o = el("option", pf.name); o.value = String(pf.id); sel.appendChild(o); }
    sel.value = st.profile != null ? String(st.profile) : "";
    const del = el("button", __("Smazat profil"), "small secondary"); del.type = "button";
    del.hidden = !sel.value;
    sel.addEventListener("change", () => {
      del.hidden = !sel.value;
      const pf = (st.profiles || []).find((x) => String(x.id) === sel.value);
      if (!pf) return;
      if (pvid && pf.pvid != null) pvid.value = pf.pvid;
      if (edge && pf.edge) edge.value = pf.edge;
      if (poe && pf.poe) poe.value = pf.poe;
      if (tagBox && pf.tagged) {
        // „všechny VLAN“: sítě lokality kromě native VLAN
        const want = pf.all_vlans ? (st.site_vlans || []).filter((v) => String(v) !== String(pvid ? pvid.value : pf.pvid)) : pf.tagged;
        for (const c of tagChecks) c.checked = want.includes(c.value);
        addVlan.value = pf.all_vlans ? "" : pf.tagged.filter((v) => !tagChecks.some((c) => c.value === v)).join(",");
      }
      if (wdOn && pf.poe_watchdog != null) { wdOn.checked = pf.poe_watchdog; if (pf.poe_watchdog_timeout) wdTime.value = pf.poe_watchdog_timeout; }
    });
    const upd = el("button", __("Uložit do profilu"), "small secondary"); upd.type = "button";
    upd.title = __("Přepsat vybraný profil nastavením z formuláře");
    upd.onclick = async () => {
      const pf = (st.profiles || []).find((x) => String(x.id) === sel.value);
      if (!pf || !(await ask(__`Přepsat profil „${pf.name}“ tímto nastavením? Porty se tím samy nezmění.`))) return;
      try { await api("PUT", `/tenants/${tenantId}/port-profiles/${pf.id}`, profileBody(pf.name)); msg(__("Profil uložen")); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    const builtin = () => ((st.profiles || []).find((x) => String(x.id) === sel.value) || {}).builtin;
    sel.addEventListener("change", () => { upd.hidden = !sel.value || builtin(); del.hidden = !sel.value || builtin(); });
    upd.hidden = !sel.value || builtin(); del.hidden = del.hidden || builtin();
    del.onclick = async () => {
      const pf = (st.profiles || []).find((x) => String(x.id) === sel.value);
      if (!pf || !(await ask(__`Smazat profil „${pf.name}“? Porty se tím nezmění.`))) return;
      try { await api("DELETE", `/tenants/${tenantId}/port-profiles/${pf.id}`); loadPortEditor(port); }
      catch (e) { msg(e.message, true); }
    };
    pr.append(sel, upd, del);
    box.insertBefore(pr, f);
  }
  function profileBody(name) {
    const d = desired();
    const b = { name, pvid: d.pvid, tagged: d.tagged, poe: d.poe, edge: d.edge };
    if (d.poe_watchdog) { b.poe_watchdog = d.poe_watchdog.enabled; if (d.poe_watchdog.enabled) b.poe_watchdog_timeout = d.poe_watchdog.timeout; }
    return b;
  }
  const desired = () => {
    const d = { disabled: !enabled.checked };
    if (!st.is_bond) d.comment = comment.value;
    if (poe) d.poe = poe.value;
    if (loop) d.loop_protect = loop.value;
    if (pvid) d.pvid = Number(pvid.value);
    if (edge) d.edge = edge.value;
    if (tagBox) {
      const t = tagChecks.filter((c) => c.checked).map((c) => c.value);
      for (const v of String(addVlan.value || "").split(/[ ,;]+/)) if (v) t.push(v);
      d.tagged = t;
    }
    if (wdOn) d.poe_watchdog = wdOn.checked ? { enabled: true, address: wdAddr.value.trim(), timeout: Number(wdTime.value) } : { enabled: false };
    if (lockIn) d.lock = lockIn.value.trim();
    return d;
  };
  const send = async (apply, force) => {
    try {
      const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/ports/${encodeURIComponent(port)}/plan`,
                          { desired: force ? { ...desired(), force: true } : desired(), apply });
      msg(apply || r.managed ? __`Odesláno: ${r.summary}. Zařízení změnu provede do minuty; když se do 5 min neozve, samo ji vrátí.`
                : __`Plán uložen: ${r.summary} (čeká na schválení v Aktivitě → Změny)`);
      showApply(r.apply);
      for (const w of r.warnings || []) msg(w, true);
      loadPlans();
    } catch (e) {
      // předpověď odříznutí: změna by přerušila cestu ke správě – jde vynutit
      if (!force && /odpojilo od controlleru/.test(e.message) && canManage()) {
        if (await ask(__`${e.message}\n\nPřesto provést?`, __("Přesto provést"))) return send(apply, true);
        return;
      }
      msg(e.message, true);
    }
  };
  const acts = el("div", null, "pe-acts");
  // řízené zařízení: Uložit = nasadit (VLAN podle sítí lokality), sledované: použít nebo jen plán
  const go = el("button", st.managed ? __("Uložit") : __("Použít (s automatickým návratem)"), "small primary"); go.type = "button";
  go.title = __("Když se zařízení po změně do 5 minut neozve, vrátí ji samo");
  go.onclick = async () => { if (st.managed || (await ask(__("Použít změnu portu? Když se zařízení po změně do 5 minut neozve, vrátí ji samo.")))) send(true); };
  acts.append(go);
  if (!canManage()) { go.textContent = __("Navrhnout změnu (schválí správce)"); go.onclick = () => send(false); }
  if (st.bridge || poe) {
    const mk = el("button", __("Uložit jako profil…"), "small secondary"); mk.type = "button";
    mk.onclick = async () => {
      const name = prompt(__("Jméno profilu (např. Kamery, AP trunk):"));
      if (!name) return;
      try {
        await api("POST", `/tenants/${tenantId}/port-profiles`, profileBody(name));
        msg(__`Profil „${name}“ uložen`); loadPortEditor(port);
      } catch (e) { msg(e.message, true); }
    };
    acts.appendChild(mk);
  }
  if (st.poe && st.poe !== "off") {
    const pc = el("button", __("Restart PoE (power-cycle)"), "small secondary"); pc.type = "button";
    pc.onclick = async () => {
      if (!(await ask(__`Vypnout a zapnout napájení na ${port} (zařízení na portu se restartuje)?`))) return;
      try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/commands`, { kind: "poe_cycle", port }); msg(__("Power-cycle naplánován")); }
      catch (e) { msg(e.message, true); }
    };
    acts.appendChild(pc);
  }
  const toolOut = el("pre", "", "tool-out"); toolOut.hidden = true;
  const bounce = el("button", __("Restart portu"), "small secondary"); bounce.type = "button";
  bounce.title = __("Vypnout a za 5 s zapnout linku (zařízení na portu se znovu připojí)");
  bounce.onclick = async () => {
    if (!(await ask(__`Restartovat linku ${port}? Port se na 5 s vypne.` + (st.uplink ? __(" Je to uplink – zařízení bude chvíli nedostupné.") : "")))) return;
    try { await api("POST", `/tenants/${tenantId}/devices/${detailId}/commands`, { kind: "port_bounce", port }); msg(__("Restart portu odeslán")); }
    catch (e) { msg(e.message, true); }
  };
  const cable = el("button", __("Test kabelu"), "small secondary"); cable.type = "button";
  cable.onclick = async () => {
    if (!(await ask(__`Test kabelu na ${port}? Na některých switchích se linka během testu na chvíli přeruší.`))) return;
    runTool("cable_test", { port }, toolOut);
  };
  const blink = el("button", __("Blikat LED"), "small secondary"); blink.type = "button";
  blink.title = __("Port 15 s bliká – najdeš ho v racku");
  blink.onclick = () => runTool("blink", { port, seconds: 15 }, toolOut);
  if (st.is_bond) {
    const b = (portCtx && portCtx.bonds || []).find((x) => x.name === port);
    if (b && b.mtc) {
      const rm = el("button", __("Zrušit bond"), "small secondary danger"); rm.type = "button"; rm.onclick = () => removeBond(b);
      acts.appendChild(rm);
    }
  } else acts.append(bounce, cable, blink);
  box.appendChild(acts);
  box.appendChild(toolOut);
  if ((st.history || []).length) {
    box.appendChild(el("h4", __("Historie portu (30 dní)")));
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
  segmented($("tr-scope"), [["*", __("Celé zařízení")], [s.lastPort || "", s.lastPort ? __`Port ${s.lastPort}` : __("Port (klikni na port)"), !s.lastPort]],
            s.iface, (v) => { s.iface = v; showTraffic(detailId); });
  segmented($("tr-metric"), TR_METRICS, s.metric, (v) => { s.metric = prefs.trMetric = v; savePrefs(); drawTrafficBar(); drawTraffic(); });
  segmented($("tr-dir"), TR_DIRS, s.dir, (v) => { s.dir = prefs.trDir = v; savePrefs(); drawTrafficBar(); drawTraffic(); });
  segmented($("tr-range"), TR_RANGES, s.hours, (v) => { s.hours = prefs.trHours = v; reload(); });
}

function trafficSeries() {
  const s = trafficState, hourly = trafficData && trafficData.resolution === "1h";
  const keys = s.metric === "bps" ? ["rx", "tx"] : s.metric === "err" ? ["rx_err", "tx_err"] : ["rx_drop", "tx_drop"];
  const out = [];
  if (s.dir !== "tx") out.push({ key: keys[0], cls: "rx", label: __("↓ Přijato"), max: s.metric === "bps" && hourly ? "rx_max" : null });
  if (s.dir !== "rx") out.push({ key: keys[1], cls: "tx", label: __("↑ Odesláno"), max: s.metric === "bps" && hourly ? "tx_max" : null });
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
  $("d-traffic-h").textContent = s.iface === "*" ? __("Provoz – celé zařízení (součet fyzických portů)") : __`Provoz – port ${s.iface}`;
  const now = $("tr-now"); now.textContent = "";
  $("tr-tip").hidden = true;
  if (!r) return;
  const pts = r.points || [];
  const series = trafficSeries();
  if (pts.length < 2) {
    svg.style.display = "none";
    $("d-traffic-info").textContent = __("Zatím málo dat – vzorek se ukládá každých 5 minut, první body do ~10 min po adopci.");
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
  const fmtT = (t) => new Date(t).toLocaleString(LOCALE, s.hours > 48 ? { day: "numeric", month: "numeric", hour: "2-digit" }
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
  $("d-traffic-info").textContent = (r.resolution === "1h" ? __("Hodinové průměry") + (s.metric === "bps" ? __(", čárkovaně špičky") : __(" (součet za hodinu)"))
                                                         : __("5min vzorky") + (s.metric === "bps" ? "" : __(" (počet za 5 min)"))) + __` · maximum ${fmtVal(peak)}`;
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
    tip.appendChild(el("b", new Date(q.t).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" })));
    for (const se of series) {
      const row = el("div", null, "tip-" + se.cls);
      row.append(el("span", se.label), el("span", fmtVal(q[se.key]) + (se.max && q[se.max] != null ? __` (špička ${fmtVal(q[se.max])})` : "")));
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
    msg(__("Uloženo"));
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
  $("d-issues").hidden = false;
  $("d-notes-h").textContent = __`Co přinese ${n.target}` +
    (n.versions.length ? __` (changelogy ${n.versions.map((v) => v.version).join(", ")})` : "");
  const ul = $("d-notes-list"); ul.textContent = "";
  for (const e of n.relevant.slice(0, 200)) {
    const li = el("li", `${e.component ? e.component + " – " : ""}${e.text}`, e.important ? "sev-warning" : "");
    li.title = e.version;
    ul.appendChild(li);
  }
  $("d-notes-more").textContent = !n.versions.length
    ? __("Changelog zatím není stažený (controller ho stahuje jednou denně z download.mikrotik.com).")
    : __`Zobrazeny změny týkající se funkcí tohoto zařízení a důležité změny (!); dalších změn: ${n.other_count}.` +
      (n.complete ? "" : __(" Changelog cílové verze zatím chybí."));
}

// --- postupná aktualizace lokality -----------------------------------------------------------

const UPG_STEP = { pending: __("čeká"), running: __("probíhá"), soak: __("sleduje se"), done: __("hotovo"), skipped: __("přeskočeno"), failed: __("chyba") };
const UPG_JOB = { running: __("běží"), done: __("dokončeno"), stopped: __("zastaveno"), cancelled: __("zrušeno") };

let upgList = [];      // [{c, on}] v aktuálním pořadí
let upgAuto = [];

async function loadUpgrade() {
  let u;
  try { u = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/upgrade`); } catch (e) { return msg(e.message, true); }
  upgAuto = u.candidates.map((c) => c.device_id);
  upgList = u.candidates.map((c) => ({ c, on: true }));
  renderUpgCands();
  renderAutoUpdate(u.auto);
  const running = u.jobs.some((j) => j.status === "running");
  $("upg-start").hidden = !u.candidates.length || running;
  $("upg-auto").hidden = !u.candidates.length || running;
  const ul = $("upg-jobs"); ul.textContent = "";
  for (const j of u.jobs) {
    const li = el("li", `#${j.id} ${UPG_JOB[j.status] || j.status} – ${fmtTime(j.created_at)} (${j.created_by || "?"})` +
                        (j.message ? `: ${j.message}` : ""), j.status === "stopped" ? "sev-warning" : "");
    if (j.status === "running") {
      const b = el("button", __("Zastavit"), "small danger");
      b.onclick = async () => {
        if (!(await ask(__("Zastavit další kroky? Právě probíhající aktualizace zařízení doběhne.")))) return;
        try { await api("POST", `/tenants/${tenantId}/upgrade-jobs/${j.id}/cancel`); loadUpgrade(); } catch (e) { msg(e.message, true); }
      };
      li.append(" ", b);
    }
    const steps = el("ol");
    for (const st of j.steps) {
      steps.appendChild(el("li", `${st.label}: ${st.kind === "firmware" ? "RouterBOOT " + (st.fw || "") : `${st.from || "?"} → ${st.version}`} – ${UPG_STEP[st.status] || st.status}` +
                                 (st.message ? ` (${st.message})` : ""), st.status === "failed" ? "sev-error" : ""));
    }
    li.appendChild(steps);
    ul.appendChild(li);
  }
}

// --- automatické a naplánované aktualizace --------------------------------------------------
let auPol = null;
const AU_DAYS = [__("Po"), __("Út"), __("St"), __("Čt"), __("Pá"), __("So"), __("Ne")];

function renderAutoUpdate(a) {
  auPol = a || { enabled: false, days: [1, 2, 3], from: "02:00", to: "05:00", delay_days: 7, canary: 10, scheduled: null, last: null };
  const can = canManage();
  $("upg-auto-form").hidden = !can; $("upg-sched-row").hidden = !can;
  $("au-enabled").checked = !!auPol.enabled;
  $("au-from").value = auPol.from; $("au-to").value = auPol.to; $("au-delay").value = String(auPol.delay_days);
  const box = $("au-days"); box.textContent = "";
  AU_DAYS.forEach((name, i) => {
    const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.checked = auPol.days.includes(i);
    cb.id = "au-day-" + i;
    l.append(cb, " " + name); box.appendChild(l);
  });
  $("au-opts").className = "au-opts" + (auPol.enabled ? "" : " off");
  $("au-last").textContent = auPol.last ? __`Naposledy ${fmtTime(auPol.last.at)}: ${auPol.last.result}` : "";
  const info = $("upg-sched-info"); info.textContent = "";
  if (auPol.scheduled) {
    const n = auPol.scheduled.order ? __`${auPol.scheduled.order.length} vybraná zařízení` : __("všechna zařízení s aktualizací");
    info.append(__`Naplánováno na ${new Date(auPol.scheduled.at).toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" })} (${n}) `);
    const b = el("button", __("Zrušit"), "small secondary"); b.type = "button";
    b.onclick = () => saveAutoUpdate({ scheduled: null }, __("Naplánovaná aktualizace zrušena"));
    info.appendChild(b);
  }
}

function autoUpdateForm() {
  return { enabled: $("au-enabled").checked, days: AU_DAYS.map((_, i) => i).filter((i) => $("au-day-" + i).checked),
           from: $("au-from").value, to: $("au-to").value, delay_days: Number($("au-delay").value),
           canary: $("upg-canary").checked ? 10 : 0, scheduled: auPol ? auPol.scheduled : null };
}

async function saveAutoUpdate(change, done) {
  try {
    const r = await api("PUT", `/tenants/${tenantId}/sites/${siteId()}/autoupdate`, { ...autoUpdateForm(), ...change });
    renderAutoUpdate(r); msg(done);
  } catch (e) { msg(e.message, true); }
}

$("au-enabled").addEventListener("change", () => { $("au-opts").className = "au-opts" + ($("au-enabled").checked ? "" : " off"); });
$("upg-auto-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  saveAutoUpdate({}, $("au-enabled").checked ? __("Automatické aktualizace zapnuty") : __("Automatické aktualizace vypnuty"));
});
$("upg-sched").addEventListener("click", async () => {
  const v = $("upg-sched-at").value;
  if (!v) return msg(__("Zadej datum a čas"), true);
  const chosen = upgList.filter((x) => x.on);
  if (!chosen.length) return msg(__("Vyber aspoň jedno zařízení"), true);
  const when = new Date(v);
  if (!(await ask(__`Naplánovat aktualizaci ${chosen.length} zařízení na ${when.toLocaleString(LOCALE, { dateStyle: "short", timeStyle: "short" })}?` +
                  `\n\n${chosen.map((x, i) => `${i + 1}. ${x.c.label}`).join("\n")}`, __("Naplánovat")))) return;
  saveAutoUpdate({ scheduled: { at: when.toISOString(), order: chosen.map((x) => x.c.device_id) } }, __("Aktualizace naplánována"));
});

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
              el("td", c.depth == null ? "?" : c.depth === 0 ? __("brána") : `${c.depth} ${c.depth === 1 ? __("skok") : c.depth < 5 ? __("skoky") : __("skoků")}`),
              el("td", c.kind === "firmware" ? __`jen RouterBOOT ${c.fw}` : `${c.from} → ${c.version}`), tdm);
    tb.appendChild(tr);
  });
  if (!upgList.length) {
    const tr = el("tr"); const td = el("td", __("Žádné online zařízení s dostupnou aktualizací"), "muted"); td.colSpan = 7;
    tr.appendChild(td); tb.appendChild(tr);
  }
  $("upg-mode").textContent = upgList.length ? (upgManual() ? __("ruční pořadí / výběr") : __("automaticky od konce sítě")) : "";
}

$("upg-auto").addEventListener("click", () => {
  const by = Object.fromEntries(upgList.map((x) => [x.c.device_id, x.c]));
  upgList = upgAuto.map((id) => ({ c: by[id], on: true }));
  renderUpgCands();
});
$("upg-start").addEventListener("click", async () => {
  const chosen = upgList.filter((x) => x.on);
  if (!chosen.length) return msg(__("Vyber aspoň jedno zařízení"), true);
  const manual = upgManual();
  const canary = $("upg-canary").checked ? 10 : 0;
  if (!(await ask(__`Spustit postupnou aktualizaci ${chosen.length} zařízení (${manual ? __("ruční pořadí") : __("od konce sítě")})?\n` +
                  chosen.map((x, i) => `${i + 1}. ${x.c.label}`).join("\n") +
                  __("\n\nKaždé se restartuje (výpadek ~2–5 min na zařízení).") +
                  (canary && chosen.length > 1 ? __`\nPrvní zařízení se po aktualizaci ${canary} min sleduje; když nebude v pořádku, zbytek zůstane beze změny.` : ""),
                  __("Spustit")))) return;
  try {
    await api("POST", `/tenants/${tenantId}/sites/${siteId()}/upgrade`,
              { ...(manual ? { order: chosen.map((x) => x.c.device_id) } : {}), canary });
    msg(__("Postupná aktualizace spuštěna")); loadUpgrade();
  } catch (e) { msg(e.message, true); }
});

// --- infrastruktura lokality (strom WAN → gateway → switche → AP) ----------------------------

let infraData = null;

// --- porty lokality: hromadná úprava napříč switchi, profily, MLAG -----------------------------

let spData = null;
const spPick = new Set();  // "deviceId|port"

async function loadProfiles() {
  try { profCache = await api("GET", `/tenants/${tenantId}/port-profiles`); } catch (e) { return msg(e.message, true); }
  drawProfiles();
}

async function loadSitePorts() {
  try { spData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/ports`); } catch (e) { return msg(e.message, true); }
  profCache = spData.profiles;
  spPick.clear();
  drawSitePorts();
  drawMlag();
}

function spRows() {
  const q = $("sp-filter").value.trim().toLowerCase();
  const out = [];
  for (const d of (spData && spData.devices) || []) {
    for (const p of d.ports) {
      if ($("sp-poe").checked && !p.poe) continue;
      if ($("sp-up").checked && !p.running) continue;
      const hay = [d.device, p.port, p.comment, p.neighbor, p.profile, p.pvid, (p.tagged || []).join(","), p.bond].join(" ").toLowerCase();
      if (q && !hay.includes(q)) continue;
      out.push([d, p]);
    }
  }
  return out;
}

function drawSitePorts() {
  const tb = $("sp-body"); tb.textContent = "";
  const rows = spRows();
  for (const [d, p] of rows) {
    const key = `${d.device_id}|${p.port}`;
    const tr = el("tr", null, spPick.has(key) ? "selected" : "");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = spPick.has(key);
    cb.disabled = !!p.bond;
    cb.title = p.bond ? __`port je v bondu ${p.bond}` : "";
    cb.addEventListener("change", () => { if (cb.checked) spPick.add(key); else spPick.delete(key); tr.className = cb.checked ? "selected" : ""; drawSpSel(); });
    const c0 = el("td"); c0.appendChild(cb); tr.appendChild(c0);
    const dev = el("td", d.device + (d.online ? "" : __(" (offline)")), d.online ? "" : "muted");
    tr.append(dev, el("td", p.port, "mono"), el("td", p.comment || "", "muted"), el("td", p.neighbor || ""));
    tr.appendChild(el("td", p.poe ? (p.poe === "off" ? __("vyp.") : __("zap.")) + (p.watchdog ? __(" · hlídač") : "") : "–", p.poe ? "" : "muted"));
    tr.append(el("td", p.pvid ?? "–"), el("td", (p.tagged || []).join(", ") || "–"));
    tr.appendChild(el("td", p.bond ? `bond ${p.bond}` : p.profile || "–", p.profile || p.bond ? "" : "muted"));
    const stt = p.disabled ? __("vypnutý") : p.running ? __("linka") : __("bez linky");
    tr.appendChild(el("td", stt + (p.lock ? " · 🔒" : ""), p.running ? "" : "muted"));
    tr.addEventListener("dblclick", () => openDetail(d.device_id));
    tb.appendChild(tr);
  }
  if (!rows.length) { const tr = el("tr"); const td = el("td", __("Žádné porty (spravované switche s inventurou)"), "muted"); td.colSpan = 10; tr.appendChild(td); tb.appendChild(tr); }
  $("sp-all").checked = rows.length > 0 && rows.filter(([, p]) => !p.bond).every(([d, p]) => spPick.has(`${d.device_id}|${p.port}`));
  drawSpSel();
}

$("sp-filter").addEventListener("input", () => { if (spData) drawSitePorts(); });
$("sp-poe").addEventListener("change", () => { if (spData) drawSitePorts(); });
$("sp-up").addEventListener("change", () => { if (spData) drawSitePorts(); });
$("sp-all").addEventListener("change", () => {
  for (const [d, p] of spRows()) { if (p.bond) continue; const k = `${d.device_id}|${p.port}`; if ($("sp-all").checked) spPick.add(k); else spPick.delete(k); }
  drawSitePorts();
});

function spTargets() {
  const by = {};
  for (const k of spPick) { const [did, port] = k.split("|"); (by[did] = by[did] || []).push(port); }
  return Object.entries(by).map(([device_id, ports]) => ({ device_id, ports }));
}

function drawSpSel() {
  const box = $("sp-sel"); box.textContent = "";
  box.hidden = !spPick.size;
  if (!spPick.size) return;
  const t = spTargets();
  const head = el("div", null, "ps-head");
  head.appendChild(el("b", __`Vybráno ${nPorts(spPick.size)} na ${nSwitches(t.length)}`));
  const x = el("button", __("Zrušit výběr"), "small secondary"); x.type = "button"; x.onclick = () => { spPick.clear(); drawSitePorts(); };
  head.appendChild(x);
  box.appendChild(head);
  const bar = el("div", null, "ps-bar");
  const sel = el("select"); sel.setAttribute("aria-label", __("Profil"));
  const o0 = el("option", __("— profil —")); o0.value = ""; sel.appendChild(o0);
  for (const pf of profCache || []) { const o = el("option", pf.name); o.value = String(pf.id); sel.appendChild(o); }
  const go = el("button", __("Použít profil"), "small"); go.type = "button";
  go.onclick = () => { if (!sel.value) return msg(__("Vyber profil"), true); spBulk({ profile_id: Number(sel.value) }); };
  bar.append(sel, go);
  const b = (label, desired) => { const x2 = el("button", label, "small secondary"); x2.type = "button"; x2.onclick = () => spBulk({ desired }); bar.appendChild(x2); };
  b(__("PoE zapnout"), { poe: "auto-on" }); b(__("PoE vypnout"), { poe: "off" }); b(__("Porty vypnout"), { disabled: true }); b(__("Porty zapnout"), { disabled: false });
  const pv = el("input"); pv.type = "number"; pv.min = 1; pv.max = 4094; pv.placeholder = "PVID"; pv.style.width = "80px";
  const pvb = el("button", __("Nastavit native VLAN"), "small secondary"); pvb.type = "button";
  pvb.onclick = () => { if (!pv.value) return; spBulk({ desired: { pvid: Number(pv.value) } }); };
  bar.append(pv, pvb);
  const plan = el("label", null, "check"); const pc = el("input"); pc.type = "checkbox"; pc.id = "sp-onlyplan";
  plan.append(pc, document.createTextNode(__(" jen uložit jako plán")));
  bar.appendChild(plan);
  box.appendChild(bar);
}

async function spBulk(body) {
  const apply = !($("sp-onlyplan") && $("sp-onlyplan").checked);
  const t = spTargets();
  if (!(await ask(__`${apply ? __("Použít") : __("Připravit plán")} na ${nPorts(spPick.size)} (${nSw(t.length)})? ` +
                  __("Na každém switchi jeden plán s automatickým návratem.")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/ports/bulk`, { targets: t, ...body, apply });
    let ok = 0;
    for (const x of r.results) {
      const dn = ((spData.devices || []).find((d) => d.device_id === x.device_id) || {}).device || "switch";
      if (x.error) msg(`${dn}: ${x.error}`, true); else { ok++; for (const w of x.warnings || []) msg(`${dn}: ${w}`, true); showApply(x.apply); }
    }
    if (ok) msg(apply ? __`Odesláno na ${nSw(ok)} – změny se do 5 min samy vrátí, když se switch neozve` : __`Uloženo ${ok} plánů ke schválení`);
    loadPlans(); setTimeout(loadSitePorts, 1500);
  } catch (e) { msg(e.message, true); }
}

function drawProfiles() {
  const box = $("sp-profiles"); box.textContent = "";
  box.appendChild(el("h3", __("Profily portů")));
  box.appendChild(el("p", __("Jako v UniFi: PoE, native VLAN, tagované VLAN, STP edge a PoE hlídač. Použij na vybrané porty výš, nebo v editoru portu."), "muted small"));
  const tbl = el("table", null, "mini");
  const th = el("tr"); for (const h of [__("Jméno"), __("Native"), __("Tagované"), "PoE", __("Hlídač"), ""]) th.appendChild(el("th", h));
  tbl.appendChild(th);
  for (const pf of profCache || []) {
    const tr = el("tr");
    tr.append(el("td", pf.name), el("td", pf.pvid ?? "–"), el("td", pf.all_vlans ? __("všechny VLAN lokality") : pf.tagged ? pf.tagged.join(", ") || __("žádné") : "–"),
              el("td", pf.poe || "–"), el("td", pf.poe_watchdog ? __`ano (${pf.poe_watchdog_timeout} s)` : pf.poe_watchdog === false ? __("ne") : "–"));
    const td = el("td");
    const e = el("button", __("Upravit"), "small secondary"); e.type = "button"; e.onclick = () => profileForm(pf);
    const d = el("button", __("Smazat"), "small secondary"); d.type = "button";
    d.onclick = async () => {
      if (!(await ask(__`Smazat profil „${pf.name}“? Porty se tím nezmění.`))) return;
      try { await api("DELETE", `/tenants/${tenantId}/port-profiles/${pf.id}`); loadProfiles(); } catch (er) { msg(er.message, true); }
    };
    if (pf.builtin) td.appendChild(el("span", __("vestavěný"), "muted small"));
    else td.append(e, d);
    tr.appendChild(td); tbl.appendChild(tr);
  }
  box.appendChild(tbl);
  const n = el("button", __("Nový profil"), "small"); n.type = "button"; n.onclick = () => profileForm(null);
  box.appendChild(n);
}

function profileForm(pf) {
  const box = $("sp-profiles");
  const old = box.querySelector(".pf-form"); if (old) old.remove();
  const f = el("form", null, "pf-form pe-grid");
  const field = (label, input) => { const l = el("label", null, "pe-field"); l.append(el("span", label), input); f.appendChild(l); return input; };
  const name = field(__("Jméno"), el("input")); name.required = true; name.maxLength = 64; name.value = pf ? pf.name : "";
  const pvid = field(__("Native VLAN (PVID)"), el("input")); pvid.type = "number"; pvid.min = 1; pvid.max = 4094; pvid.value = pf && pf.pvid != null ? pf.pvid : "";
  const tagged = field(__("Tagované VLAN"), el("input")); tagged.placeholder = __("např. 30,40,50"); tagged.value = pf && pf.tagged ? pf.tagged.join(",") : "";
  const poe = field("PoE", el("select"));
  for (const [v, l] of [["", __("neměnit")], ["auto-on", __("zapnuto (auto)")], ["forced-on", __("vždy zapnuto")], ["off", __("vypnuto")]]) { const o = el("option", l); o.value = v; poe.appendChild(o); }
  poe.value = pf && pf.poe ? pf.poe : "";
  const edge = field("STP edge", el("select"));
  for (const [v, l] of [["", __("neměnit")], ["auto", "auto"], ["yes", __("ano (koncové zařízení)")], ["no", __("ne")]]) { const o = el("option", l); o.value = v; edge.appendChild(o); }
  edge.value = pf && pf.edge ? pf.edge : "";
  const wd = field(__("PoE hlídač"), el("select"));
  for (const [v, l] of [["", __("neměnit")], ["1", __("zapnout (restart napájení bez odezvy)")], ["0", __("vypnout")]]) { const o = el("option", l); o.value = v; wd.appendChild(o); }
  wd.value = pf && pf.poe_watchdog != null ? (pf.poe_watchdog ? "1" : "0") : "";
  const wdt = field(__("Hlídač: bez odezvy (s)"), el("input")); wdt.type = "number"; wdt.min = 10; wdt.max = 3600; wdt.value = (pf && pf.poe_watchdog_timeout) || 60;
  const save = el("button", pf ? __("Uložit profil") : __("Vytvořit profil"), "small");
  const cancel = el("button", __("Zrušit"), "small secondary"); cancel.type = "button"; cancel.onclick = () => f.remove();
  const acts = el("div", null, "pe-acts"); acts.append(save, cancel); f.appendChild(acts);
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { name: name.value.trim(), pvid: pvid.value ? Number(pvid.value) : null,
      tagged: tagged.value.trim() ? tagged.value.split(/[ ,;]+/).filter(Boolean) : null, poe: poe.value || null, edge: edge.value || null,
      poe_watchdog: wd.value === "" ? null : wd.value === "1", poe_watchdog_timeout: Number(wdt.value) };
    try {
      if (pf) await api("PUT", `/tenants/${tenantId}/port-profiles/${pf.id}`, body);
      else await api("POST", `/tenants/${tenantId}/port-profiles`, body);
      msg(__("Profil uložen")); loadProfiles();
    } catch (er) { msg(er.message, true); }
  });
  box.appendChild(f);
  name.focus();
}

function drawMlag() {
  const box = $("sp-mlag"); box.textContent = "";
  box.appendChild(el("h3", "LACP a MLAG"));
  const bonds = [];
  for (const d of (spData && spData.devices) || []) for (const b of d.bonds || []) bonds.push([d, b]);
  if (bonds.length) {
    const ul = el("ul", null, "plain");
    for (const [d, b] of bonds) {
      const li = el("li", `${d.device}: ${b.name} (${b.slaves.join(", ")})${b.mlag_id ? ` · MLAG ${b.mlag_id}` : ""}${b.running ? "" : __(" · neběží")}`);
      if (b.mtc) {
        const rm = el("button", __("Zrušit"), "small secondary"); rm.type = "button";
        rm.onclick = async () => {
          if (!(await ask(__`Zrušit bond ${b.name} na ${d.device}? Porty se vrátí do bridge s nastavením bondu. Vypni LACP i na protistraně.`))) return;
          try { const r = await api("POST", `/tenants/${tenantId}/devices/${d.device_id}/bonds`, { kind: "bond_remove", name: b.name, apply: true });
            msg(__`Odesláno: ${r.summary}`); loadPlans(); } catch (e) { msg(e.message, true); }
        };
        li.append(" ", rm);
      }
      ul.appendChild(li);
    }
    box.appendChild(ul);
  } else box.appendChild(el("p", __("Žádné bondy. LACP vytvoříš výběrem portů v detailu switche (Vybrat více portů → LACP)."), "muted small"));
  const sw = ((spData && spData.devices) || []).filter((d) => d.ports.length);
  if (sw.length < 2) return;
  box.appendChild(el("h4", __("MLAG mezi dvěma switchi")));
  box.appendChild(el("p", __("Klient (server, switch) připojený do obou switchů jedním LACP bondem. Peer port = přímé propojení switchů. Vytvoří dva plány (A primární); aplikuj je v Aktivitě → Změny. Ověř v labu."), "muted small"));
  const f = el("form", null, "pe-grid");
  const side = (label) => {
    const wrap = el("div", null, "mlag-side");
    wrap.appendChild(el("b", label));
    const dsel = el("select"); for (const d of sw) { const o = el("option", d.device); o.value = d.device_id; dsel.appendChild(o); }
    const peer = el("select"), mem = el("input");
    mem.placeholder = __("porty ke klientovi, např. ether10");
    const fill = () => { peer.textContent = ""; const d = sw.find((x) => x.device_id === dsel.value); for (const p of d.ports) { const o = el("option", p.port); o.value = p.port; peer.appendChild(o); } };
    dsel.addEventListener("change", fill); fill();
    const l1 = el("label", null, "pe-field"); l1.append(el("span", "Switch"), dsel);
    const l2 = el("label", null, "pe-field"); l2.append(el("span", __("Peer port")), peer);
    const l3 = el("label", null, "pe-field"); l3.append(el("span", __("Porty ke klientovi")), mem);
    wrap.append(l1, l2, l3);
    f.appendChild(wrap);
    return { dsel, peer, mem };
  };
  const A = side(__("Switch A (primární)")), B = side(__("Switch B"));
  if (sw[1]) { B.dsel.value = sw[1].device_id; B.dsel.dispatchEvent(new Event("change")); }
  const idl = el("label", null, "pe-field"); const mid = el("input"); mid.type = "number"; mid.min = 1; mid.value = 10;
  idl.append(el("span", "mlag-id"), mid); f.appendChild(idl);
  const go = el("button", __("Připravit plány MLAG"), "small"); f.appendChild(go);
  f.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const ports = (x) => x.mem.value.split(/[ ,;]+/).filter(Boolean);
    const body = {
      a: { device_id: A.dsel.value, peer_port: A.peer.value, members: [{ ports: ports(A), mlag_id: Number(mid.value) }] },
      b: { device_id: B.dsel.value, peer_port: B.peer.value, members: [{ ports: ports(B), mlag_id: Number(mid.value) }] },
    };
    try {
      const r = await api("POST", `/tenants/${tenantId}/mlag`, body);
      msg(__`Plány MLAG připravené (${r.plans.length}) – schval je v Aktivitě → Změny, nejdřív A, pak B`);
      for (const p of r.plans) for (const w of p.warnings || []) msg(w, true);
      loadPlans();
    } catch (e) { msg(e.message, true); }
  });
  box.appendChild(f);
}


// --- zálohy zařízení: které zálohovat, společná pravidla zákazníka, verze a porovnání ----------------------
let dbk = null;
const DBK_REASON = { scheduled: __("plánovaná"), manual: __("ruční"), request: __("vyžádaná"), "pre-change": __("před změnou") };
function dbkSum() {
  if (!dbk) return;
  const follow = $("dbk-follow").checked, pl = dbk.plan || {};
  const k = follow ? pl.keep : Number($("dbk-keep").value) || 0, w = follow ? pl.weekly : Number($("dbk-weekly").value) || 0, m = follow ? pl.monthly : Number($("dbk-monthly").value) || 0;
  for (const id of ["dbk-every", "dbk-keep", "dbk-weekly", "dbk-monthly"]) $(id).disabled = follow;
  const when = follow ? (pl.enabled ? __`ve stejný čas jako záloha controlleru (${pl.time})` : __("denně (plán zálohy controlleru je vypnutý)")) : $("dbk-every").selectedOptions && $("dbk-every").selectedOptions[0] ? $("dbk-every").selectedOptions[0].textContent : "";
  $("dbk-sum").textContent = __`Každé vybrané zařízení se zálohuje ${when}; u každého zůstane nejvýš ${k + (w || 0) + (m || 0)} verzí (${retText(k, w, m)}).`
    + ($("dbk-changes").checked ? " " + __("Verze beze změny se neukládají, takže seznam verzí = seznam změn.") : "");
}
async function loadDevBackups(method, body) {
  if (!tenantId) return;
  try {
    if (method) await api(method, `/tenants/${tenantId}/device-backups`, body);
    dbk = await api("GET", `/tenants/${tenantId}/device-backups?site=${siteId() || ""}`);
  } catch (e) { return msg(e.message, true); }
  const p = dbk.policy, pl = dbk.plan || {};
  $("dbk-follow").checked = p.mode === "follow"; $("dbk-custom").checked = p.mode !== "follow";
  $("dbk-follow-l").textContent = __("Řídit se zálohou controlleru") + (pl.enabled ? ` (${pl.time}, ${retText(pl.keep, pl.weekly, pl.monthly)})` : " " + __("(plán je vypnutý)"));
  $("dbk-every").value = String([6, 12, 24, 168].includes(p.every_hours) ? p.every_hours : 24);
  $("dbk-keep").value = p.keep; $("dbk-weekly").value = p.weekly; $("dbk-monthly").value = p.monthly; $("dbk-changes").checked = !!p.only_changes;
  dbkSum();
  const tb = $("dbk-list"); tb.textContent = "";
  const devs = dbk.devices || [];
  $("dbk-count").textContent = __`${devs.filter((d) => d.enabled).length} z ${devs.length} se zálohuje`;
  $("dbk-all").checked = devs.length > 0 && devs.every((d) => d.enabled);
  for (const d of devs) {
    const tr = el("tr", null, d.enabled ? "" : "off");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = !!d.enabled; cb.setAttribute("aria-label", __`Zálohovat ${d.name}`);
    cb.addEventListener("change", () => loadDevBackups("POST", { device_ids: [d.id], enabled: cb.checked }));
    const c0 = el("td"); c0.appendChild(cb);
    const nm = el("td"); nm.append(el("strong", d.name), el("small", " " + (d.model || ""), "muted"));
    const st = el("td", !d.enabled ? __("nezálohuje se") : d.error ? __("chyba: ") + d.error : !d.reachable || d.state !== "adopted" ? __("zařízení není dostupné") : d.versions ? __("v pořádku") : __("čeká na první zálohu"),
                  !d.enabled ? "muted" : d.error ? "sev-error" : d.versions && d.reachable ? "sev-ok" : "muted");
    const acts = el("td"), box = el("div", null, "bt-acts");
    const vb = el("button", __`Verze (${d.versions})`, "small secondary"); vb.type = "button"; vb.disabled = !d.versions;
    vb.addEventListener("click", () => openDevVersions(d));
    const nb = el("button", __("Zálohovat teď"), "small secondary"); nb.type = "button"; nb.disabled = !d.enabled || d.state !== "adopted";
    nb.addEventListener("click", async () => {
      try { await api("POST", `/tenants/${tenantId}/devices/${d.id}/backup`, {}); msg(__`${d.name}: záloha proběhne do minuty`); } catch (e) { msg(e.message, true); }
    });
    box.append(vb, nb); acts.appendChild(box);
    tr.append(c0, nm, el("td", d.managed ? __("řízené") : __("sledované"), "muted"), el("td", d.backup_at ? fmtTime(d.backup_at) : "–"),
              el("td", d.versions ? `${d.versions}` + (d.bytes ? ` · ${fmtBytes(d.bytes)}` : "") : "–"), el("td", d.last_change ? fmtTime(d.last_change) : "–"), st, acts);
    tb.appendChild(tr);
  }
  if (!devs.length) { const tr = el("tr"), td = el("td", __("V této lokalitě není žádné adoptované zařízení."), "muted"); td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); }
}
for (const id of ["dbk-follow", "dbk-custom", "dbk-every", "dbk-keep", "dbk-weekly", "dbk-monthly", "dbk-changes"]) {
  $(id).addEventListener("change", dbkSum); $(id).addEventListener("input", dbkSum);
}
$("dbk-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await loadDevBackups("PUT", { mode: $("dbk-follow").checked ? "follow" : "custom", every_hours: Number($("dbk-every").value), only_changes: $("dbk-changes").checked,
                                keep: Number($("dbk-keep").value), weekly: Number($("dbk-weekly").value) || 0, monthly: Number($("dbk-monthly").value) || 0 });
  msg(__("Pravidla záloh zařízení uložena"));
});
$("dbk-all").addEventListener("change", () => {
  if (!dbk) return;
  loadDevBackups("POST", { device_ids: dbk.devices.map((d) => d.id), enabled: $("dbk-all").checked });
});
async function openDevVersions(d) {
  let r;
  try { r = await api("GET", `/tenants/${tenantId}/devices/${d.id}/backups`); } catch (e) { return msg(e.message, true); }
  $("dbv-title").textContent = __`Verze záloh: ${d.name}`;
  const tb = $("dbv-list"); tb.textContent = ""; $("dbv-diff").hidden = true;
  for (const v of r.versions) {
    const tr = el("tr");
    const when = el("td"); when.append(document.createTextNode(new Date(v.taken_at).toLocaleString(LOCALE)), el("small", v.latest ? " · " + __("aktuální") : v.changed ? " · " + __("změna") : "", v.latest ? "sev-ok" : "muted"));
    const acts = el("td"), box = el("div", null, "bt-acts");
    if (!v.latest) {
      const cmp = el("button", __("Porovnat s aktuální"), "small secondary"); cmp.type = "button";
      cmp.addEventListener("click", async () => {
        try {
          const x = await api("GET", `/tenants/${tenantId}/devices/${d.id}/backups/${v.id}/diff?against=latest`);
          const pre = $("dbv-diff"); pre.textContent = x.diff || __("Tato verze je stejná jako aktuální konfigurace."); pre.hidden = false;
          for (const row of tb.children) row.className = row === tr ? "selected" : "";
        } catch (e) { msg(e.message, true); }
      });
      box.appendChild(cmp);
    }
    const a = el("a", __("Stáhnout"), "small"); a.href = `/api/admin/tenants/${tenantId}/devices/${d.id}/backups/${v.id}/download`;
    a.title = __("Celý export včetně hesel (jen admin tenanta, auditováno)");
    box.appendChild(a); acts.appendChild(box);
    tr.append(when, el("td", DBK_REASON[v.reason] || v.reason, "muted"), el("td", fmtBytes(v.size), "muted"), acts);
    tb.appendChild(tr);
  }
  $("dbv-dlg").hidden = false;
}
$("dbv-close").addEventListener("click", () => { $("dbv-dlg").hidden = true; });
$("dbk-ext").addEventListener("click", () => {
  if (me && me.superadmin) { showView("backups", true); return; }
  msg(__("Externí úložiště záloh nastavuje provozovatel controlleru (superadmin) ve Správa → Zálohy."), true);
});

// --- bezdrátové spoje: seznam a karta spoje (provoz, signál, rychlost, ztrátovost, rušení na jedné ose) -------
const wl = { links: null, sel: null, hours: 24, data: null, traffic: null };
const wlOpt = () => (prefs.wl = prefs.wl || { marks: true, usual: true });
const WL_STATE = { ok: __("V pořádku"), degraded: __("Zhoršený"), bad: __("Špatný"), down: __("Výpadek"), unknown: __("Bez měření") };
const WL_CODE = { down: __("výpadek"), signal: __("propad signálu"), weak: __("slabý signál"), rate: __("pád rychlosti"), loss: __("ztrátovost"),
  noise: __("šum na kanálu"), reconnect: __("přepojení"), flap: __("časté přepojování"), asym: __("nesouměrný signál") };
const wlMb = (v) => v == null ? "–" : (v / 1e6 >= 100 ? Math.round(v / 1e6) : (v / 1e6).toFixed(1).replace(".", ",")) + " Mb/s";
const wlN = (v, unit, d = 0) => v == null ? "–" : Number(v).toFixed(d).replace(".", ",").replace("-", "−") + (unit ? " " + unit : "");
const wlPair = (a, b, unit) => a == null && b == null ? "–" : `${wlN(a)} / ${wlN(b, unit)}`;
function wlWhy(w) {
  const sideName = (x) => wl.data && x.side ? " (" + (x.side === "a" ? wl.data.a.device : wl.data.b.device) + ")" : "";
  return ({
    down: () => __("spoj je dole – protistrana není připojená"),
    signal: () => __`signál ${wlN(w.now, "dBm")}, obvykle ${wlN(w.usual, "dBm")}` + sideName(w),
    weak: () => __`slabý signál ${wlN(w.now, "dBm")}` + sideName(w),
    rate: () => __`rychlost spoje ${wlMb(w.now)}, obvykle ${wlMb(w.usual)}`,
    loss: () => __`ztrátovost ${wlN(w.now, "%", 1)}`,
    asym: () => __`strany se slyší o ${w.now} dB jinak – zkontroluj nasměrování a výkon`,
    noise: () => __`šum na kanálu ${wlN(w.now, "dBm")}, obvykle ${wlN(w.usual, "dBm")} – pravděpodobně cizí vysílač`,
    flap: () => __`${w.now}× přepojení za 24 h`,
  }[w.code] || (() => w.code))();
}
const wlBadge = (st, score) => el("span", (WL_STATE[st] || st) + (score != null && st !== "down" ? ` · ${score}` : ""), "wl-badge s-" + st);

async function loadLinks() {
  if (!tenantId || !siteId()) return;
  try { wl.links = (await api("GET", `/tenants/${tenantId}/sites/${siteId()}/links`)).links || []; } catch (e) { return msg(e.message, true); }
  if (wl.sel && !wl.links.some((l) => l.id === wl.sel)) wl.sel = null;
  if (wl.sel) return loadLink();
  renderLinks();
}

async function loadLink() {
  const id = wl.sel; if (!id) return renderLinks();
  let d;
  try { d = await api("GET", `/tenants/${tenantId}/links/${id}?hours=${wl.hours}`); } catch (e) { wl.sel = null; renderLinks(); return msg(e.message, true); }
  if (wl.sel !== id) return;
  wl.data = d; wl.traffic = null;
  renderLinks();
  try {
    const t = await api("GET", `/tenants/${tenantId}/devices/${d.a.device_id}/traffic?hours=${wl.hours}&interface=${encodeURIComponent(d.a.iface)}`);
    if (wl.sel === id) { wl.traffic = t.points || []; renderLinks(); }
  } catch (_e) { /* graf provozu je navíc – bez něj karta funguje */ }
}

function renderLinks() {
  const side = $("wl-side"), main = $("wl-main"); side.textContent = ""; main.textContent = "";
  const links = wl.links || [];
  if (!wl.sel || !wl.data) {
    side.appendChild(el("p", __("Bezdrátové spoje mezi spravovanými zařízeními. Každých 5 minut se měří signál obou stran, rychlost spoje a pingem přes spoj ztrátovost a odezva."), "muted wl-note"));
    const bad = links.filter((l) => l.state !== "ok" && l.state !== "unknown").length;
    for (const [k, v] of [[__("Spojů"), links.length], [__("Mimo normál"), bad]]) { const r = el("div", null, "pm-kv"); r.append(el("span", k), el("span", String(v))); side.appendChild(r); }
    if (!links.length) { main.appendChild(el("p", __("V této lokalitě zatím žádný bezdrátový spoj mezi spravovanými zařízeními není. Spoj se objeví sám, jakmile jsou obě strany adoptované a vidí se v registraci."), "muted wl-empty")); return; }
    const t = el("table", null, "wl-t"), th = el("tr");
    for (const h of [__("Spoj"), __("Stav"), __("Signál"), __("Rychlost spoje"), __("Ztrátovost"), __("Odezva"), __("Přepojení 24 h"), __("Měřeno")]) th.appendChild(el("th", h));
    const head = el("thead"); head.appendChild(th); t.appendChild(head);
    const tb = el("tbody");
    for (const l of links) {
      const n = l.now || {}, u = l.usual || {}, tr = el("tr", null, "clickable");
      const nm = el("td"); nm.append(el("strong", l.name), el("small", ` ${l.a.iface} · ${l.b.iface}` + (l.muted ? " · " + __("ztlumený") : ""), "muted"));
      const st = el("td"); st.appendChild(wlBadge(l.state, l.score));
      const sg = el("td", wlPair(n.signal_a, n.signal_b, "dBm")); if (u.signal_a != null) sg.appendChild(el("small", " " + __`obvykle ${wlPair(u.signal_a, u.signal_b)}`, "muted"));
      tr.append(nm, st, sg, el("td", wlMb(n.rate_ab) + " / " + wlMb(n.rate_ba)), el("td", wlN(n.loss, "%", 1), n.loss >= 2 ? "wl-bad" : ""),
                el("td", wlN(n.rtt_avg, "ms", 1)), el("td", String(l.flaps || 0)), el("td", fmtTime(l.sampled_at), "muted"));
      tr.title = (l.why || []).map((w) => wlWhy(w)).join("\n");
      tr.addEventListener("click", () => { wl.sel = l.id; wl.data = null; loadLink(); });
      tb.appendChild(tr);
    }
    t.appendChild(tb); main.appendChild(t);
    return;
  }
  const d = wl.data, n = d.now || {}, u = d.usual || {}, o = wlOpt();
  const back = el("button", "‹ " + __`Všechny spoje (${links.length})`, "ux-link"); back.type = "button";
  back.addEventListener("click", () => { wl.sel = null; wl.data = null; renderLinks(); });
  const title = el("div", null, "wl-title"); title.append(el("strong", d.name), wlBadge(d.state, d.score));
  const ends = el("div", null, "wl-ends");
  for (const [x, cls, role] of [[d.a, "a", "AP"], [d.b, "b", __("klient")]]) {
    const b = el("button", null, "wl-end"); b.type = "button";
    b.append(el("i", "", "wl-dot " + cls), el("strong", x.device), el("small", `${role} · ${x.iface}` + (x.state === "adopted" ? "" : " · offline"), "muted"));
    b.addEventListener("click", () => openDetail(x.device_id)); ends.appendChild(b);
  }
  side.append(back, title, ends);
  const kv = (k, v, sub, cls) => { const r = el("div", null, "wl-kv"); const val = el("span", v, cls || ""); if (sub) val.appendChild(el("small", sub, "muted")); r.append(el("span", k), val); side.appendChild(r); };
  const dev = (code) => (d.why || []).some((w) => w.code === code) ? "wl-warn" : "";
  kv(__("Signál"), wlPair(n.signal_a, n.signal_b, "dBm"), u.signal_a != null ? __`obvykle ${wlPair(u.signal_a, u.signal_b)}` : "", dev("signal") || dev("weak"));
  if (n.snr_a != null || n.snr_b != null) kv("SNR", wlPair(n.snr_a, n.snr_b, "dB"));
  kv(__("Rychlost spoje"), wlMb(n.rate_ab) + " / " + wlMb(n.rate_ba), u.rate_ab != null ? __`obvykle ${wlMb(u.rate_ab)}` : "", dev("rate"));
  kv(__("Ztrátovost"), wlN(n.loss, "%", 1), d.ping_ip ? __`ping 10× na ${d.ping_ip}` : __("bez adresy pro ping"), dev("loss") && "wl-bad");
  kv(__("Odezva"), wlN(n.rtt_avg, "ms", 1), n.rtt_max != null ? __`nejvíc ${wlN(n.rtt_max, "ms", 1)}` : "");
  if (n.ccq_a != null || n.ccq_b != null) kv(d.kind === "w60g" ? __("Kvalita přenosu") : "CCQ", wlPair(n.ccq_a, n.ccq_b, "%"));
  if (n.noise != null) kv(__("Šum na kanálu"), wlN(n.noise, "dBm"), u.noise != null ? __`obvykle ${wlN(u.noise, "dBm")}` : "", dev("noise"));
  kv(__("Přepojení za 24 h"), String(d.flaps || 0), "", dev("flap"));
  kv(__("Měřeno"), fmtTime(d.sampled_at));
  if (!u.signal_a && !u.rate_ab) side.appendChild(el("p", __`Obvyklý stav se teprve učí (${u.n || 0} měření, potřeba aspoň 36).`, "muted wl-note"));
  const sw = (label, key, get, set) => {
    const l = el("label", null, "ux-switch"), i = el("input"); i.type = "checkbox"; i.checked = get();
    i.addEventListener("change", () => set(i.checked)); l.append(el("span", label), i, el("i")); side.appendChild(l);
  };
  wlBw(side, d);
  sw(__("Značky anomálií"), "marks", () => !!o.marks, (v) => { o.marks = v; savePrefs(); renderLinks(); });
  sw(__("Obvyklý stav (čárkovaně)"), "usual", () => !!o.usual, (v) => { o.usual = v; savePrefs(); renderLinks(); });
  sw(__("Nehlásit upozornění"), "muted", () => !!d.muted, async (v) => {
    try { wl.data = await api("PUT", `/tenants/${tenantId}/links/${d.id}?hours=${wl.hours}`, { muted: v }); const l = links.find((x) => x.id === d.id); if (l) l.muted = v; } catch (e) { msg(e.message, true); }
    renderLinks();
  });

  // hlavní část: období, štítky odchylek, graf, seznam úseků
  const top = el("div", null, "wl-top"), chips = el("div", null, "wl-chips"), seg = el("div", null, "seg");
  for (const w of d.why || []) chips.appendChild(el("span", "▲ " + wlWhy(w), "wl-chip " + (w.code === "down" || (w.code === "loss" && w.now >= 10) ? "bad" : "warn")));
  if (!(d.why || []).length && d.state === "ok") chips.appendChild(el("span", __("Spoj je ve svém normálu."), "muted"));
  for (const [h, t] of [[1, "1 h"], [24, "24 h"], [168, __("7 dní")], [720, __("30 dní")]]) {
    const b = el("button", t, wl.hours === h ? "active" : ""); b.type = "button"; b.setAttribute("aria-pressed", String(wl.hours === h));
    b.addEventListener("click", () => { wl.hours = h; loadLink(); }); seg.appendChild(b);
  }
  top.append(chips, seg); main.appendChild(top);
  const lg = el("div", null, "wl-legend");
  for (const [cls, t] of [["rx", __("příjem")], ["tx", __("odesílání")], ["a", d.a.device], ["b", d.b.device], ["usual", __("obvyklý stav")], ["loss", __("ztrátovost / šum")], ["rtt", __("odezva")]]) { const x = el("span"); x.append(el("i", "", "wl-l " + cls), document.createTextNode(t)); lg.appendChild(x); }
  main.appendChild(lg);
  const box = el("div", null, "wl-chart"); main.appendChild(box);
  if (!(d.points || []).length) box.appendChild(el("p", __("Za zvolené období není žádné měření."), "muted wl-empty"));
  else wlChart(box, d, o);
  const eps = (d.episodes || []).slice().reverse();
  if (eps.length) {
    const list = el("div", null, "wl-eps");
    for (const e of eps.slice(0, 12)) {
      const r = el("div", null, "wl-ep"), bad = e.codes.includes("down") || (e.worst.loss || 0) >= 10;
      const mins = Math.round((new Date(e.to) - new Date(e.from)) / 60000) + Math.round(d.bucket / 60);
      r.append(el("i", "", "wl-dot " + (bad ? "bad" : "warn")), el("span", new Date(e.from).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }), "muted wl-ep-t"),
               el("span", e.codes.map((c) => WL_CODE[c] || c).join(", ") + (e.worst.signal != null && e.codes.includes("signal") ? ` (${wlN(e.worst.signal, "dBm")})` : "") + (e.worst.loss ? `, ${__("ztráta až")} ${wlN(e.worst.loss, "%", 1)}` : "")),
               el("span", mins >= 120 ? __`trvalo ${Math.round(mins / 60)} h` : __`trvalo ${mins} min`, "muted"));
      list.appendChild(r);
    }
    main.appendChild(list);
  }
}

// měření propustnosti přes spoj (existující test mezi dvěma zařízeními): poslední výsledek + tlačítko
function wlBw(side, d) {
  const box = el("div", null, "wl-bw"); side.appendChild(box);
  const key = d.id, st = (wl.bw = wl.bw && wl.bw.key === key ? wl.bw : { key, last: undefined, running: false });
  const draw = () => {
    box.textContent = "";
    const t = st.last, r = t && t.result;
    const row = el("div", null, "wl-kv"), val = el("span", st.running ? __("měřím…") : r ? `↓ ${fmtBps(r.rx_avg || 0)}  ↑ ${fmtBps(r.tx_avg || 0)}` : t && t.status === "failed" ? __("selhalo") : "–");
    if (t && !st.running) val.appendChild(el("small", t.status === "failed" ? (t.error || "") : fmtTime(t.finished_at || t.started_at), "muted"));
    row.append(el("span", __("Propustnost")), val); box.appendChild(row);
    const b = el("button", __("Změřit propustnost"), "secondary small"); b.type = "button"; b.disabled = st.running || !d.ping_ip || d.state === "down";
    b.title = d.ping_ip ? __("Test rychlosti přes spoj, asi 10 s. Po dobu testu spoj plně zatíží.") : __("Chybí adresa protistrany ve společné síti.");
    b.addEventListener("click", async () => {
      let out;
      try { out = await api("POST", `/tenants/${tenantId}/devices/${d.a.device_id}/bwtest`, { to: d.b.device_id, address: d.ping_ip, duration: 10, direction: "both", protocol: "tcp" }); }
      catch (e) { return msg(e.message, true); }
      st.running = true; draw();
      for (let i = 0; i < 25 && st.running; i++) {
        await new Promise((res) => setTimeout(res, 2000));
        let v; try { v = await api("GET", `/tenants/${tenantId}/devices/${d.a.device_id}/bwtest`); } catch (_e) { continue; }
        const mine = (v.tests || []).find((x) => x.id === out.id);
        if (mine && mine.status !== "running") { st.last = mine; st.running = false; }
      }
      st.running = false;
      if (wl.bw === st && wl.sel === key) draw();
    });
    box.appendChild(b);
  };
  draw();
  if (st.last === undefined) {
    st.last = null;
    api("GET", `/tenants/${tenantId}/devices/${d.a.device_id}/bwtest`).then((v) => {
      st.last = (v.tests || []).find((x) => x.status !== "running" && [x.from_device, x.to_device].includes(d.b.device_id)) || null;
      if (wl.bw === st && wl.sel === key) draw();
    }).catch(() => {});
  }
}

// graf spoje: pásy se společnou časovou osou; odchylky podbarvené přes všechny pásy.
// Kreslí se v pixelech skutečné šířky (písmo se nezvětšuje s oknem), křivky jsou vyhlazené.
function wlSmooth(run) {
  // Catmull-Rom → Bézier s mírným napětím; u 2 bodů úsečka
  if (run.length < 3) return run.map(([x, y], i) => (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)).join("");
  let d = `M${run[0][0].toFixed(1)} ${run[0][1].toFixed(1)}`;
  for (let i = 0; i < run.length - 1; i++) {
    const p0 = run[Math.max(0, i - 1)], p1 = run[i], p2 = run[i + 1], p3 = run[Math.min(run.length - 1, i + 2)], k = 0.18;
    d += `C${(p1[0] + (p2[0] - p0[0]) * k).toFixed(1)} ${(p1[1] + (p2[1] - p0[1]) * k).toFixed(1)} ${(p2[0] - (p3[0] - p1[0]) * k).toFixed(1)} ${(p2[1] - (p3[1] - p1[1]) * k).toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  return d;
}
function wlChart(box, d, o) {
  if (typeof document.createElementNS !== "function") return;
  const pts = d.points, u = d.usual || {}, now = d.now || {};
  const W = Math.max(640, Math.round(box.clientWidth || 1000)), X0 = 148, X1 = W - 92;
  const t1 = Date.now(), t0 = t1 - d.hours * 3600e3, gapMs = d.bucket * 2500;
  const X = (t) => X0 + (X1 - X0) * Math.min(1, Math.max(0, (new Date(t).getTime() - t0) / (t1 - t0)));
  const any = (k) => pts.some((p) => p[k] != null);
  const nice = (v) => { const m = Math.pow(10, Math.floor(Math.log10(v || 1))), f = v / m; return (f <= 1 ? 1 : f <= 1.5 ? 1.5 : f <= 2 ? 2 : f <= 3 ? 3 : f <= 5 ? 5 : f <= 7.5 ? 7.5 : 10) * m; };
  const bands = [];
  const tr = wl.traffic || [];
  if (tr.length > 1) bands.push({ title: __("Provoz"), h: 76, now: `↓ ${fmtBps((tr[tr.length - 1] || {}).rx || 0)}   ↑ ${fmtBps((tr[tr.length - 1] || {}).tx || 0)}`,
    lo: 0, hi: nice(Math.max(1e6, ...tr.map((x) => Math.max(x.rx || 0, x.tx || 0)))), fmt: (v) => fmtBps(v),
    series: [{ pts: tr, k: "rx", cls: "rx", area: true }, { pts: tr, k: "tx", cls: "tx", area: true }] });
  const sig = pts.flatMap((p) => [p.signal_a, p.signal_b]).filter((v) => v != null);
  if (sig.length) bands.push({ title: __("Signál"), h: 60, now: wlPair(now.signal_a, now.signal_b, "dBm"), sub: u.signal_a != null ? __`obvykle ${wlPair(u.signal_a, u.signal_b)}` : "",
    lo: Math.min(-80, Math.floor(Math.min(...sig) / 10) * 10), hi: Math.max(-50, Math.ceil(Math.max(...sig) / 10) * 10), fmt: (v) => wlN(v, "dBm"),
    usual: o.usual ? [u.signal_a, u.signal_b] : [], series: [{ pts, k: "signal_a", cls: "a" }, { pts, k: "signal_b", cls: "b" }] });
  const rates = pts.flatMap((p) => [p.rate_ab, p.rate_ba]).filter((v) => v != null);
  if (rates.length) bands.push({ title: __("Rychlost spoje"), h: 44, now: wlMb(now.rate_ab), sub: u.rate_ab != null ? __`obvykle ${wlMb(u.rate_ab)}` : "",
    lo: 0, hi: nice(Math.max(...rates)), fmt: (v) => wlMb(v), usual: o.usual ? [u.rate_ab] : [], series: [{ pts, k: "rate_ab", cls: "a", step: true }, { pts, k: "rate_ba", cls: "b", step: true }] });
  if (any("loss") || any("rtt_avg")) {
    const lmax = nice(Math.max(5, ...pts.map((p) => p.loss || 0))), rmax = nice(Math.max(10, ...pts.map((p) => p.rtt_avg || 0)));
    bands.push({ title: __("Ztrátovost a odezva"), h: 56, now: `${wlN(now.loss, "%", 1)} · ${wlN(now.rtt_avg, "ms", 1)}`, lo: 0, hi: lmax,
      fmt: (v) => `${wlN(v, "%")} · ${wlN(v / lmax * rmax, "ms")}`, series: [{ pts, k: "loss", cls: "loss", area: true }, { pts, k: "rtt_avg", cls: "rtt", lo: 0, hi: rmax }] });
  }
  if (any("ccq_a") || any("ccq_b")) bands.push({ title: d.kind === "w60g" ? __("Kvalita přenosu") : "CCQ", h: 40, now: wlPair(now.ccq_a, now.ccq_b, "%"), lo: 0, hi: 100, fmt: (v) => wlN(v, "%"),
    series: [{ pts, k: "ccq_a", cls: "a" }, { pts, k: "ccq_b", cls: "b" }] });
  if (any("noise")) { const ns = pts.map((p) => p.noise).filter((v) => v != null);
    bands.push({ title: __("Šum na kanálu"), h: 40, now: wlN(now.noise, "dBm"), sub: u.noise != null ? __`obvykle ${wlN(u.noise, "dBm")}` : "", lo: Math.floor((Math.min(...ns) - 3) / 5) * 5, hi: Math.ceil((Math.max(...ns) + 3) / 5) * 5,
      fmt: (v) => wlN(v, "dBm"), usual: o.usual ? [u.noise] : [], series: [{ pts, k: "noise", cls: "loss" }] }); }
  let y = 14; for (const b of bands) { b.y = y; y += b.h + 18; }
  const H = y + 8, bottom = y - 18;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "wl-svg", role: "img", "aria-label": __`Průběh spoje ${d.name}` });
  // svislé vodicí čáry času (jemné), popisky dole
  const step = d.hours <= 1 ? 600e3 : d.hours <= 24 ? 3 * 3600e3 : d.hours <= 168 ? 86400e3 : 5 * 86400e3;
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
    const dt = new Date(t), x = X(t).toFixed(1), lab = d.hours <= 24 ? dt.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }) : dt.toLocaleDateString(LOCALE, { day: "numeric", month: "numeric" });
    svg.appendChild(svgEl("line", { x1: x, x2: x, y1: 14, y2: bottom, class: "wl-grid" }));
    svg.appendChild(svgEl("text", { x, y: H - 2, class: "wl-ax mid" }, lab));
  }
  if (o.marks) for (const e of d.episodes || []) {
    const bad = e.codes.includes("down") || (e.worst.loss || 0) >= 10, xa = X(e.from) - 1, xb = X(new Date(e.to).getTime() + d.bucket * 1000) + 1;
    svg.appendChild(svgEl("rect", { x: xa.toFixed(1), y: 14, width: Math.max(3, xb - xa).toFixed(1), height: bottom - 14, rx: 2, class: "wl-ep-bg " + (bad ? "bad" : "warn") }));
    svg.appendChild(svgEl("rect", { x: xa.toFixed(1), y: 10, width: Math.max(3, xb - xa).toFixed(1), height: 3, rx: 1.5, class: "wl-ep-mark " + (bad ? "bad" : "warn") }));
  }
  for (const b of bands) {
    const mid = b.y + b.h / 2;
    svg.appendChild(svgEl("line", { x1: X0, x2: X1, y1: b.y + b.h + 0.5, y2: b.y + b.h + 0.5, class: "wl-base" }));
    svg.appendChild(svgEl("line", { x1: X0, x2: X1, y1: mid, y2: mid, class: "wl-grid" }));
    svg.appendChild(svgEl("text", { x: 0, y: b.y + 11, class: "wl-bt" }, b.title));
    svg.appendChild(svgEl("text", { x: 0, y: b.y + 27, class: "wl-bn" }, b.now));
    if (b.sub && b.h >= 44) svg.appendChild(svgEl("text", { x: 0, y: b.y + 41, class: "wl-ax" }, b.sub));
    svg.appendChild(svgEl("text", { x: X1 + 8, y: b.y + 8, class: "wl-ax" }, b.fmt(b.hi)));
    svg.appendChild(svgEl("text", { x: X1 + 8, y: b.y + b.h, class: "wl-ax" }, b.fmt(b.lo)));
    const Y = (v, lo = b.lo, hi = b.hi) => b.y + b.h - Math.min(1, Math.max(0, (v - lo) / ((hi - lo) || 1))) * (b.h - 2);
    for (const v of b.usual || []) if (v != null) svg.appendChild(svgEl("line", { x1: X0, x2: X1, y1: Y(v).toFixed(1), y2: Y(v).toFixed(1), class: "wl-usual" }));
    for (const s of b.series) {
      s.Y = (v) => Y(v, s.lo, s.hi);
      // souvislé úseky: mezera v měření nebo výpadek čáru přeruší
      let run = [], last = 0;
      const flush = () => {
        if (run.length > 1) {
          const line = s.step ? run.map(([x, yy], i) => (i ? `H${x.toFixed(1)}V${yy.toFixed(1)}` : `M${x.toFixed(1)} ${yy.toFixed(1)}`)).join("") : wlSmooth(run);
          if (s.area) svg.appendChild(svgEl("path", { d: `${line}L${run[run.length - 1][0].toFixed(1)} ${b.y + b.h}L${run[0][0].toFixed(1)} ${b.y + b.h}Z`, class: "wl-a " + s.cls }));
          svg.appendChild(svgEl("path", { d: line, class: "wl-s " + s.cls }));
        } else if (run.length === 1) svg.appendChild(svgEl("circle", { cx: run[0][0].toFixed(1), cy: run[0][1].toFixed(1), r: 1.5, class: "wl-p " + s.cls }));
        run = [];
      };
      for (const p of s.pts) {
        const t = new Date(p.t).getTime(), v = p[s.k];
        if (t < t0) continue;
        if (v == null || (last && t - last > gapMs)) flush();
        if (v != null) { run.push([X(p.t), s.Y(v)]); last = t; }
      }
      flush();
    }
  }
  const cur = svgEl("line", { x1: 0, x2: 0, y1: 14, y2: bottom, class: "wl-cur", visibility: "hidden" }); svg.appendChild(cur);
  const dots = svgEl("g", { visibility: "hidden" }); svg.appendChild(dots);
  box.appendChild(svg);
  const tip = el("div", null, "wl-tip"); tip.hidden = true; box.appendChild(tip);
  const nearest = (arr, t) => { let best = null; for (const p of arr) if (!best || Math.abs(new Date(p.t) - t) < Math.abs(new Date(best.t) - t)) best = p; return best; };
  svg.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect(), x = (ev.clientX - r.left) / r.width * W;
    if (x < X0 || x > X1) { tip.hidden = true; cur.setAttribute("visibility", "hidden"); dots.setAttribute("visibility", "hidden"); return; }
    const t = t0 + (x - X0) / (X1 - X0) * (t1 - t0), best = nearest(pts, t);
    if (!best) return;
    const bx = X(best.t); cur.setAttribute("x1", bx); cur.setAttribute("x2", bx); cur.setAttribute("visibility", "visible");
    // tečky na křivkách v místě kurzoru
    dots.textContent = ""; dots.setAttribute("visibility", "visible");
    for (const b of bands) for (const s of b.series) {
      const p = s.pts === pts ? best : nearest(s.pts, new Date(best.t).getTime());
      if (p && p[s.k] != null) dots.appendChild(svgEl("circle", { cx: X(p.t).toFixed(1), cy: s.Y(p[s.k]).toFixed(1), r: 3, class: "wl-dotc " + s.cls }));
    }
    tip.textContent = "";
    tip.appendChild(el("strong", new Date(best.t).toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" })));
    const row = (k, v, cls) => { const rr = el("div", null, "wl-tip-r"); rr.append(el("span", k), el("b", v, cls || "")); tip.appendChild(rr); };
    if (!best.up) row(__("Spoj"), __("dole"), "bad");
    else {
      const tp = tr.length ? nearest(tr, new Date(best.t).getTime()) : null;
      if (tp) row(__("Provoz"), `↓ ${fmtBps(tp.rx || 0)}  ↑ ${fmtBps(tp.tx || 0)}`);
      row(__("Signál"), wlPair(best.signal_a, best.signal_b, "dBm"));
      row(__("Rychlost"), `${wlMb(best.rate_ab)} / ${wlMb(best.rate_ba)}`);
      if (best.loss != null) row(__("Ztrátovost"), wlN(best.loss, "%", 1), best.loss >= 2 ? "bad" : "");
      if (best.rtt_avg != null) row(__("Odezva"), wlN(best.rtt_avg, "ms", 1));
      if (best.noise != null) row(__("Šum"), wlN(best.noise, "dBm"));
    }
    const devs = (d.episodes || []).find((e) => new Date(e.from) <= new Date(best.t) && new Date(best.t) <= new Date(e.to));
    if (devs || best.reconnect) tip.appendChild(el("div", (devs ? devs.codes : ["reconnect"]).map((c) => WL_CODE[c] || c).join(", "), "wl-tip-w"));
    tip.hidden = false;
    const left = (bx / W) * r.width + 14;
    // pozice přes CSSOM – atribut style by zablokovala CSP (style-src 'self') a bublina by spadla pod graf
    tip.style.left = (left + 216 > r.width ? left - 244 : left) + "px";
    tip.style.top = Math.max(0, Math.min(r.height - 150, ev.clientY - r.top - 30)) + "px";
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; cur.setAttribute("visibility", "hidden"); dots.setAttribute("visibility", "hidden"); });
}
// při změně šířky okna se graf překreslí na novou šířku (písmo zůstává stejně velké)
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  let wlResize = 0;
  window.addEventListener("resize", () => { clearTimeout(wlResize); wlResize = setTimeout(() => { if (wl.sel && wl.data && !$("links-wrap").hidden) renderLinks(); }, 150); });
}

// --- sekce Porty: schéma portů s připojenými zařízeními, filtry, přehled VLAN a provoz -------------------------
const pm = { data: null, dev: "", f: {}, sel: null, chartFor: null };
const pmOpt = () => (prefs.pm = prefs.pm || { diagram: true, conn: true, vlan: false, stats: false });
function pmSpeed(p) {
  if (p.disabled) return "off";
  if (!p.running) return "down";
  const r = String(p.rate || "");
  return /^10M/i.test(r) ? "10" : /^100M/i.test(r) ? "100" : /^1G/i.test(r) ? "1g" : /^2\.5G/i.test(r) ? "2g5" : /^\d+G/i.test(r) ? "10g" : "up";
}
const PM_SPEED = { 10: "10 Mb/s", 100: "100 Mb/s", "1g": "1 Gb/s", "2g5": "2,5 Gb/s", "10g": "10 Gb/s +", up: __("linka"), down: __("bez linky"), off: __("vypnutý") };
const pmPoe = (p) => !p.poe ? "none" : p.poe_w > 0 ? "on" : p.poe === "off" ? "off" : "idle";
const PM_POE = { on: __("Napájí"), idle: __("Zapnuto, bez odběru"), off: __("PoE vypnuté"), none: __("Bez PoE") };
const pmNet = (vid) => { const n = ((pm.data && pm.data.networks) || []).find((x) => String(x.vid) === String(vid)); return n ? `${n.name} (${vid})` : "VLAN " + vid; };
const pmConn = (p) => p.peer ? "dev" : (p.clients || []).length ? "client" : p.neighbor ? "other" : "none";
const PM_FACETS = [
  ["link", __("Stav"), (x) => x.p.disabled ? "off" : x.p.running ? "up" : "down", (v) => ({ up: __("S linkou"), down: __("Bez linky"), off: __("Vypnutý") })[v], true],
  ["speed", __("Rychlost linky"), (x) => x.p.running && !x.p.disabled ? pmSpeed(x.p) : null, (v) => PM_SPEED[v]],
  ["health", __("Zdraví portu"), (x) => pmHealth(x.p).level || "ok", (v) => PM_HEALTH[v] || v],
  ["poe", "PoE", (x) => pmPoe(x.p), (v) => PM_POE[v]],
  ["conn", __("Připojeno"), (x) => pmConn(x.p), (v) => ({ dev: __("Spravované zařízení"), client: __("Klient"), other: __("Jiný soused"), none: __("Nic") })[v]],
  ["pvid", __("Nativní VLAN"), (x) => x.p.pvid ? String(x.p.pvid) : null, pmNet],
  ["stp", "STP", (x) => x.p.stp_role || null],
  ["profile", __("Profil"), (x) => x.p.bond ? "bond " + x.p.bond : x.p.profile || null]];
// zdraví portu: chyby podle druhu (poměr k provozu), výpadky linky, duplex, optika
const PM_ERR = { fcs: "FCS", align: __("zarovnání"), fragment: __("fragmenty"), jabber: "jabber", size: __("délka rámce"), overflow: __("přetečení"),
  late_collision: __("pozdní kolize"), other: __("nosná"), generic: __("chyby (souhrn)") };
const PM_ERR_HINT = { fcs: __("kabel, konektor nebo rušení"), align: __("kabel nebo neshoda duplexu"), fragment: __("kabel, rušení nebo kolize"),
  jabber: __("vadná síťová karta protistrany"), size: __("neshoda MTU"), overflow: __("port nestíhá – přetížení, flow control"),
  late_collision: __("neshoda duplexu nebo příliš dlouhý kabel"), other: __("problém s linkou"), generic: __("kabel, duplex nebo SFP") };
const PM_HEALTH = { critical: __("Vážné chyby"), warning: __("Varování"), note: __("Drobnosti"), ok: __("V pořádku") };
const pmHealth = (p) => p.health || { level: "ok", why: [] };
const pmPct = (r) => r == null ? "" : (r * 100 >= 1 ? (r * 100).toFixed(1) : r * 100 >= 0.1 ? (r * 100).toFixed(2) : (r * 100).toFixed(3)).replace(".", ",") + " %";
function pmWhy(p) {
  const h = pmHealth(p), out = [];
  for (const w of h.why || []) {
    if (w === "errors") out.push(__`${h.errors_1h} chyb za poslední hodinu` + (h.ratio != null ? ` (${pmPct(h.ratio)} ${__("rámců")})` : "") + (h.top ? ` – ${PM_ERR[h.top] || h.top}: ${PM_ERR_HINT[h.top] || ""}` : ""));
    else if (w === "flap") out.push(__`${h.link_downs}× výpadek linky za 24 h – kabel, konektor nebo protějšek`);
    else if (w === "sfp") out.push(__`optický výkon ${h.sfp_rx} dBm, obvykle ${h.sfp_usual} dBm – konektor, vlákno nebo modul`);
    else if (w === "half") out.push(__("half-duplex – neshoda autonegociace s protistranou"));
    else if (w === "slow") out.push(__("jen 100 Mb/s – u gigabitového zařízení typicky vadný pár v kabelu"));
  }
  return out;
}
const pmErrText = (p) => { const h = pmHealth(p); return h.errors_24h ? `${h.errors_24h}` + (h.top ? " · " + (PM_ERR[h.top] || h.top) : "") : h.link_downs ? __`${h.link_downs}× linka` : "–"; };
const pmNum = (name) => String(name || "").replace(/^\D+/, "") || String(name || "");
const pmKey = (d, p) => d.device_id + "|" + p.port;
// co je na portu: spravované zařízení (s jeho portem), klient(i), jiný soused
function pmWhat(p) {
  if (p.peer) return p.peer.device + (p.peer.port ? " · " + p.peer.port : "");
  const c = p.clients || [];
  if (c.length) return (c[0].name || c[0].mac) + (c.length > 1 ? ` +${c.length - 1}` : "");
  return p.neighbor || "";
}

async function loadPortMap() {
  if (!tenantId || !siteId()) return;
  try { pm.data = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/ports`); } catch (e) { return msg(e.message, true); }
  if (pm.dev && !pm.data.devices.some((d) => d.device_id === pm.dev)) pm.dev = "";
  renderPortMap();
}

function pmItems() {
  const out = [];
  for (const d of (pm.data && pm.data.devices) || []) if (!pm.dev || d.device_id === pm.dev) for (const p of d.ports) out.push({ d, p });
  return out;
}

// mřížka portů jednoho zařízení: liché nahoře, sudé dole (jako na čelním panelu); SFP zvlášť
function pmGrid(d, cell, labels) {
  const face = el("div", null, "pm-face");
  const groups = [d.ports.filter((p) => !p.sfp), d.ports.filter((p) => p.sfp)];
  for (const g of groups) {
    if (!g.length) continue;
    const grp = el("div", null, "pm-grp"), two = g.length > 8;
    for (let i = 0; i < g.length; i += two ? 2 : 1) {
      const col = el("div", null, "pm-col"), a = g[i], b = two ? g[i + 1] : null;
      if (labels) col.appendChild(el("span", labels(a), "pm-lab"));
      col.appendChild(cell(a));
      if (two) { col.appendChild(b ? cell(b) : el("span", "", "pm-gap")); if (labels) col.appendChild(el("span", b ? labels(b) : "", "pm-lab")); }
      grp.appendChild(col);
    }
    face.appendChild(grp);
  }
  return face;
}

function renderPortMap() {
  const o = pmOpt(), data = pm.data; if (!data) return;
  // výběr zařízení
  const sel = $("pm-dev"); sel.textContent = "";
  const all = el("option", __("Všechny porty")); all.value = ""; sel.appendChild(all);
  for (const d of data.devices) if (d.ports.length) { const op = el("option", d.device + (d.online ? "" : __(" (offline)"))); op.value = d.device_id; sel.appendChild(op); }
  sel.value = pm.dev;
  for (const k of ["diagram", "conn", "vlan", "stats"]) $("pm-o-" + k).checked = !!o[k];
  const cur = data.devices.find((d) => d.device_id === pm.dev);
  const info = $("pm-info"); info.textContent = "";
  if (cur) {
    const used = cur.ports.filter((p) => p.running).length, w = cur.ports.reduce((a, p) => a + (p.poe_w || 0), 0);
    for (const [k, v] of [[__("Model"), cur.model || "–"], [__("Porty s linkou"), `${used} / ${cur.ports.length}`], [__("PoE celkem"), w ? w.toFixed(1) + " W" : "–"]]) {
      const r = el("div", null, "pm-kv"); r.append(el("span", k), el("span", v)); info.appendChild(r);
    }
    const a = el("button", __("Otevřít zařízení"), "ux-link"); a.type = "button"; a.addEventListener("click", () => openDetail(cur.device_id)); info.appendChild(a);
  }
  const items = pmItems();
  facetGroups($("pm-facets"), "pm", items, PM_FACETS, pm.f, () => renderPortMap());
  $("pm-clear").disabled = !facetAny(pm.f);
  const ok = (x) => facetOk(PM_FACETS, pm.f, x);
  const shown = new Set(items.filter(ok).map((x) => pmKey(x.d, x.p)));
  const devs = data.devices.filter((d) => d.ports.length && (!pm.dev || d.device_id === pm.dev));
  // schéma
  const dg = $("pm-diagram"); dg.textContent = ""; dg.hidden = !o.diagram;
  const tile = (d) => (p) => {
    const sp = pmSpeed(p), key = pmKey(d, p);
    const hl = pmHealth(p).level;
    const b = el("button", null, `pm-tile s-${sp}` + (shown.has(key) ? "" : " dim") + (pm.sel === key ? " sel" : "") + (hl && hl !== "ok" ? " h-" + hl : "")); b.type = "button";
    b.appendChild(el("span", pmNum(p.port), "pm-n"));
    const marks = (p.poe_w > 0 ? "ϟ" : "") + (p.stp_role === "root-port" ? "↑" : "") + (p.bond ? "≡" : "");
    if (marks) b.appendChild(el("span", marks, "pm-m"));
    b.title = [`${d.device} · ${p.port}`, p.comment, PM_SPEED[sp], p.poe_w > 0 ? `PoE ${p.poe_w} W` : "", pmWhat(p),
               p.pvid ? __`nativní ${pmNet(p.pvid)}` : "", ...pmWhy(p).map((w) => "⚠ " + w)].filter(Boolean).join("\n");
    b.setAttribute("aria-label", b.title.replace(/\n/g, ", "));
    b.addEventListener("click", () => { pm.sel = pm.sel === key ? null : key; renderPortMap(); });
    b.addEventListener("dblclick", () => openDetail(d.device_id));
    return b;
  };
  for (const d of devs) {
    const box = el("div", null, "pm-dev");
    if (!pm.dev) {
      const h = el("div", null, "pm-dev-h"), a = el("a", d.device); a.href = "#";
      a.addEventListener("click", (e) => { e.preventDefault(); pm.dev = d.device_id; pm.sel = null; renderPortMap(); });
      h.append(a, el("small", " " + [d.model, d.online ? "" : __("offline")].filter(Boolean).join(" · "), "muted")); box.appendChild(h);
    }
    box.appendChild(pmGrid(d, tile(d), o.conn ? (p) => pmWhat(p).split(" · ")[0] : null));
    dg.appendChild(box);
  }
  // legenda
  const lg = $("pm-legend"); lg.textContent = ""; lg.hidden = !o.diagram;
  for (const k of ["10", "100", "1g", "2g5", "10g", "down", "off"]) { const s = el("span"); s.append(el("i", "", "pm-tile s-" + k), document.createTextNode(PM_SPEED[k])); lg.appendChild(s); }
  lg.append(el("span", "ϟ " + __("napájí PoE")), el("span", "↑ " + __("uplink")), el("span", "≡ bond"));
  for (const k of ["critical", "warning"]) { const s = el("span"); s.append(el("i", "", "pm-dot h-" + k), document.createTextNode(PM_HEALTH[k])); lg.appendChild(s); }
  pmPortDetail();
  // VLAN: nativní VLAN portů + síť po síti (tagovaná / nativní / není)
  const vb = $("pm-vlan"); vb.textContent = ""; vb.hidden = !o.vlan;
  if (o.vlan) {
    for (const d of devs) {
      const box = el("div", null, "pm-dev");
      box.appendChild(el("h4", (pm.dev ? "" : d.device + " – ") + __("Nativní VLAN portů")));
      box.appendChild(pmGrid(d, (p) => { const c = el("span", p.pvid ? String(p.pvid) : "–", "pm-vbox" + (shown.has(pmKey(d, p)) ? "" : " dim")); c.title = `${p.port}: ${p.pvid ? pmNet(p.pvid) : "–"}`; return c; }));
      const vids = new Set((data.networks || []).map((n) => String(n.vid)));
      for (const p of d.ports) { if (p.pvid) vids.add(String(p.pvid)); for (const t of p.tagged || []) vids.add(String(t)); }
      for (const vid of [...vids].sort((a, b) => Number(a) - Number(b))) {
        const net = (data.networks || []).find((n) => String(n.vid) === vid);
        if (!d.ports.some((p) => String(p.pvid) === vid || (p.tagged || []).map(String).includes(vid))) continue;
        const h = el("div", null, "pm-vh"); h.append(el("span", pmNet(vid)), el("span", net && net.subnet ? net.subnet : "", "muted")); box.appendChild(h);
        box.appendChild(pmGrid(d, (p) => {
          const st = String(p.pvid) === vid ? "native" : (p.tagged || []).map(String).includes(vid) ? "tagged" : "none";
          const c = el("span", "", "pm-vbox v-" + st); c.title = `${p.port}: ` + ({ native: __("nativní (netagovaná)"), tagged: __("tagovaná"), none: __("není na portu") })[st]; return c;
        }));
      }
      vb.appendChild(box);
    }
    const lg2 = el("div", null, "pm-legend");
    for (const [k, t] of [["tagged", __("tagovaná")], ["native", __("nativní")], ["none", __("není na portu")]]) { const s2 = el("span"); s2.append(el("i", "", "pm-vbox v-" + k), document.createTextNode(t)); lg2.appendChild(s2); }
    vb.insertBefore(lg2, vb.children[0] || null);
  }
  // provoz
  const stb = $("pm-stats"); stb.hidden = !o.stats;
  if (o.stats) pmStats(cur);
  // tabulka
  const tb = $("pm-body"); tb.textContent = "";
  let n = 0;
  for (const { d, p } of items) {
    const key = pmKey(d, p);
    if (!shown.has(key) || (pm.sel && pm.sel !== key)) continue;
    n++;
    const tr = el("tr", null, "clickable" + (pm.sel === key ? " selected" : "") + (p.running ? "" : " off"));
    const pt = el("td"); pt.append(el("i", "", "pm-tile mini s-" + pmSpeed(p)), document.createTextNode(" " + pmNum(p.port)));
    const dv = el("td"); const da = el("a", d.device); da.href = "#"; da.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openDetail(d.device_id); }); dv.appendChild(da);
    const cn = el("td");
    if (p.peer) { const a = el("a", p.peer.device); a.href = "#"; a.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); openDetail(p.peer.device_id); }); cn.appendChild(a); if (p.peer.port) cn.appendChild(el("span", " · " + p.peer.port, "muted")); }
    else cn.textContent = pmWhat(p) || "–";
    const c0 = (p.clients || [])[0];
    const sp = pmSpeed(p);
    tr.append(pt, dv, el("td", p.comment || p.port, p.comment ? "" : "muted"), el("td", p.stp_role || "–", "muted"), cn,
              el("td", p.poe ? (p.poe_w > 0 ? p.poe_w + " W" : PM_POE[pmPoe(p)]) : "–", p.poe_w > 0 ? "" : "muted"),
              el("td", (sp === "10g" || sp === "up") && p.rate ? p.rate : PM_SPEED[sp], "pm-sp s-" + sp), pmErrCell(p),
              el("td", c0 ? c0.mac + ((p.clients || []).length > 1 ? ` +${p.clients.length - 1}` : "") : "–", "mono"), el("td", c0 && c0.ip ? c0.ip : "–"),
              el("td", p.bond ? "bond " + p.bond : p.profile || "–", p.profile || p.bond ? "" : "muted"),
              el("td", p.pvid ? pmNet(p.pvid) : "–"), el("td", (p.tagged || []).join(", ") || "–", "muted"));
    tr.addEventListener("click", () => { pm.sel = pm.sel === key ? null : key; renderPortMap(); });
    tb.appendChild(tr);
  }
  if (!n) { const tr = el("tr"), td = el("td", __("Žádné porty neodpovídají filtru."), "muted"); td.colSpan = 13; tr.appendChild(td); tb.appendChild(tr); }
  $("pm-count").textContent = pm.sel ? __("vybraný port – kliknutím na něj výběr zrušíš") : n === items.length ? __`${n} portů` : __`${n} z ${items.length} portů`;
}

function pmErrCell(p) {
  const h = pmHealth(p), td = el("td", null, "pm-err h-" + (h.level || "ok"));
  if (h.level === "critical" || h.level === "warning") td.appendChild(el("i", "", "pm-dot h-" + h.level));
  td.appendChild(document.createTextNode(pmErrText(p)));
  const kinds = Object.entries(h.kinds || {}).sort((a, b) => b[1] - a[1]).map(([k, n]) => `${PM_ERR[k] || k}: ${n}`);
  td.title = [...pmWhy(p), kinds.length ? __("za 24 h – ") + kinds.join(", ") : "", h.drops ? __`zahozeno ${h.drops}` : "", h.pause ? __`pause rámců ${h.pause}` : ""].filter(Boolean).join("\n");
  return td;
}

// vybraný port: proč je označený, chyby podle druhu a hodinový průběh chyb (48 h)
async function pmPortDetail() {
  const box = $("pm-port"), data = pm.data;
  let d = null, p = null;
  if (pm.sel && data) { const [id, name] = pm.sel.split("|"); d = data.devices.find((x) => x.device_id === id); p = d && d.ports.find((x) => x.port === name); }
  box.hidden = !p;
  if (!p) { pm.errFor = null; return; }
  const h = pmHealth(p), head = () => {
    box.textContent = "";
    const t = el("div", null, "pm-port-h");
    t.append(el("strong", `${d.device} · ${p.port}` + (p.comment ? ` (${p.comment})` : "")), el("span", PM_HEALTH[h.level] || PM_HEALTH.ok, "pm-badge h-" + (h.level || "ok")));
    const open = el("button", __("Upravit port"), "ux-link"); open.type = "button"; open.addEventListener("click", () => openDetail(d.device_id)); t.appendChild(open);
    box.appendChild(t);
    for (const w of pmWhy(p)) box.appendChild(el("p", "⚠ " + w, "pm-why"));
    const kinds = Object.entries(h.kinds || {}).sort((a, b) => b[1] - a[1]);
    if (kinds.length || h.drops || h.pause || h.collision) {
      const k = el("div", null, "pm-kinds");
      for (const [name, n] of kinds) { const c = el("span"); c.append(el("b", String(n)), document.createTextNode(" " + (PM_ERR[name] || name))); c.title = PM_ERR_HINT[name] || ""; k.appendChild(c); }
      for (const [n, t2] of [[h.drops, __("zahozeno")], [h.collision, __("kolize")], [h.pause, __("pause rámce")]]) if (n) { const c = el("span", null, "muted"); c.append(el("b", String(n)), document.createTextNode(" " + t2)); k.appendChild(c); }
      box.appendChild(k);
    } else if (!(h.why || []).length) box.appendChild(el("p", __("Za posledních 24 h bez chyb."), "muted"));
  };
  head();
  if (pm.errFor === pm.sel && pm.errPts) return pmErrBars(box, pm.errPts);
  const want = pm.sel; pm.errFor = want; pm.errPts = null;
  let r;
  try { r = await api("GET", `/tenants/${tenantId}/devices/${d.device_id}/port-errors?hours=48&interface=${encodeURIComponent(p.port)}`); } catch (e) { return; }
  if (pm.sel !== want) return;
  pm.errPts = r.points || [];
  pmErrBars(box, pm.errPts);
}

function pmErrBars(box, pts) {
  if (!pts.some((x) => x.errors > 0)) return;
  const max = Math.max(...pts.map((x) => x.errors)), by = {};
  for (const x of pts) by[String(x.t).slice(0, 13)] = x;
  const bars = el("div", null, "pm-bars"); bars.setAttribute("role", "img"); bars.setAttribute("aria-label", __("Chyby portu po hodinách (48 h)"));
  const end = new Date(pts[pts.length - 1].t).getTime();
  for (let i = 47; i >= 0; i--) {
    const t = new Date(end - i * 3600e3), x = by[t.toISOString().slice(0, 13)], b = el("i", "", "h-" + (x ? x.level : "ok"));
    b.style.height = (x && x.errors ? Math.max(8, Math.round(100 * x.errors / max)) : 2) + "%";
    b.title = `${fmtTime(t.toISOString())}: ${x ? x.errors : 0} ` + __("chyb");
    bars.appendChild(b);
  }
  const cap = el("div", null, "pm-st-x"); cap.append(el("span", __("před 48 h")), el("span", __`chyby po hodinách, nejvíc ${max}`), el("span", __("teď")));
  box.append(bars, cap);
}

// provoz: součet fyzických portů zařízení, nebo vybraný port (posledních 24 h)
async function pmStats(cur) {
  const box = $("pm-stats");
  if (!cur) { box.textContent = ""; box.appendChild(el("p", __("Provoz se kreslí pro jedno zařízení – vyber ho nahoře v panelu."), "muted")); pm.chartFor = null; return; }
  const port = pm.sel && pm.sel.startsWith(cur.device_id + "|") ? pm.sel.split("|")[1] : "*";
  const want = cur.device_id + "|" + port;
  if (pm.chartFor === want) return;
  pm.chartFor = want;
  let r;
  try { r = await api("GET", `/tenants/${tenantId}/devices/${cur.device_id}/traffic?hours=24&interface=${encodeURIComponent(port)}`); }
  catch (e) { pm.chartFor = null; box.textContent = ""; box.appendChild(el("p", __`Provoz se nepodařilo načíst: ${e.message}`, "muted")); return; }
  if (pm.chartFor !== want) return;
  box.textContent = "";
  const pts = r.points || [];
  const head = el("div", null, "pm-st-h");
  const last = pts[pts.length - 1] || {};
  head.append(el("strong", port === "*" ? __("Provoz všech portů (24 h)") : __`Provoz portu ${port} (24 h)`),
              el("span", "↓ " + fmtBps(last.rx || 0), "pm-rx"), el("span", "↑ " + fmtBps(last.tx || 0), "pm-tx"));
  box.appendChild(head);
  if (pts.length < 2 || typeof document.createElementNS !== "function") { box.appendChild(el("p", __("Zatím není dost měření."), "muted")); return; }
  const W = 1000, H = 190, max = Math.max(1000, ...pts.map((x) => Math.max(x.rx || 0, x.tx || 0)));
  const t0 = new Date(pts[0].t).getTime(), t1 = new Date(pts[pts.length - 1].t).getTime() || t0 + 1;
  const X = (t) => (new Date(t).getTime() - t0) / Math.max(1, t1 - t0) * W, Y = (v) => H - 4 - (v / max) * (H - 14);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", class: "pm-chart", role: "img", "aria-label": __("Graf provozu") });
  for (const [k, cls] of [["tx", "pm-a-tx"], ["rx", "pm-a-rx"]]) {
    const line = pts.map((x) => `${X(x.t).toFixed(1)},${Y(x[k] || 0).toFixed(1)}`).join(" ");
    svg.appendChild(svgEl("polygon", { points: `0,${H} ${line} ${W},${H}`, class: cls }));
    svg.appendChild(svgEl("polyline", { points: line, class: cls + " l" }));
  }
  box.appendChild(svg);
  const ax = el("div", null, "pm-st-x"); ax.append(el("span", fmtTime(pts[0].t)), el("span", __`špička ${fmtBps(max)}`), el("span", __("teď"))); box.appendChild(ax);
}

$("pm-dev").addEventListener("change", () => { pm.dev = $("pm-dev").value; pm.sel = null; pm.chartFor = null; renderPortMap(); });
for (const k of ["diagram", "conn", "vlan", "stats"]) $("pm-o-" + k).addEventListener("change", () => { pmOpt()[k] = $("pm-o-" + k).checked; savePrefs(); pm.chartFor = null; renderPortMap(); });
$("pm-clear").addEventListener("click", () => { pm.f = {}; pm.sel = null; renderPortMap(); });

// --- skrývací levý panel filtrů (rozvržení .ux): úchyt na hraně panelu, volba se pamatuje pro každou sekci ----
for (const b of document.querySelectorAll ? document.querySelectorAll(".ux-toggle") : []) {
  const box = $(b.dataset.ux), key = "mtc.side." + b.dataset.ux;
  const set = (show) => {
    box.classList.toggle("noside", !show);
    const label = show ? __("Skrýt panel filtrů") : __("Zobrazit panel filtrů");
    b.textContent = show ? "‹" : "›"; b.title = label; b.setAttribute("aria-label", label); b.setAttribute("aria-expanded", show ? "true" : "false");
  };
  b.addEventListener("click", () => {
    const show = box.classList.contains("noside");
    set(show);
    try { localStorage.setItem(key, show ? "1" : "0"); } catch (_e) { /* jen pohodlí */ }
  });
  try { if (localStorage.getItem(key) === "0") set(false); } catch (_e) { /* jen pohodlí */ }
}

// --- mapové ovládání SVG: kolečko = zoom ke kurzoru, tažení = posun, dvojklik = přiblížit, špetka na dotyku ----
// Obsah si drží své souřadnice (0..W × 0..H); mění se jen viewBox podle {x, y, k}. Pohled přežije
// překreslení dat; nově se nastaví jen poprvé nebo tlačítkem „Vejít do okna“.
function mapControl(box, svg) {
  const st = { x: 0, y: 0, k: 1, W: 0, H: 0, set: false, user: false };   // user = člověk už pohledem hýbal
  if (!box || !svg || typeof box.getBoundingClientRect !== "function") return { content() {}, fit() {} };
  const size = () => { const r = box.getBoundingClientRect(); return [Math.max(50, r.width), Math.max(50, r.height)]; };
  const apply = () => {
    const [w, h] = size();
    svg.setAttribute("width", w); svg.setAttribute("height", h);
    svg.setAttribute("viewBox", `${st.x} ${st.y} ${w / st.k} ${h / st.k}`);
  };
  const clampK = (k) => Math.min(4, Math.max(0.15, k));
  const zoomAt = (px, py, k) => {            // px, py = bod v okně, který má zůstat na místě
    k = clampK(k); st.user = true;
    st.x += px / st.k - px / k; st.y += py / st.k - py / k; st.k = k; apply();
  };
  const fit = () => {
    const [w, h] = size();
    // skutečný rozsah nakresleného obsahu (karty můžou přesahovat vlevo od nuly); bez něj rozměr plátna
    let x0 = 0, y0 = 0, cw = st.W, chh = st.H;
    try { const bb = svg.getBBox(); if (bb && bb.width > 0 && bb.height > 0) { x0 = bb.x; y0 = bb.y; cw = bb.width; chh = bb.height; } } catch (_e) { /* skryté SVG */ }
    st.k = clampK(Math.min(w / (cw + 48), h / (chh + 48), 1.25));
    st.x = x0 + (cw - w / st.k) / 2; st.y = y0 - 24 / st.k; apply();
  };
  const one = () => { const [w] = size(); st.k = 1; st.x = (st.W - w) / 2; st.y = -10; apply(); };
  const local = (e) => { const r = box.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  box.addEventListener("wheel", (e) => {
    e.preventDefault();
    const [px, py] = local(e);
    zoomAt(px, py, st.k * Math.exp(-(e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY) * (e.ctrlKey ? 0.01 : 0.0018)));
  }, { passive: false });
  const pts = new Map();
  let drag = null, moved = false, pinch = null;
  box.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    if (e.target && e.target.closest && e.target.closest(".map-ctl, .map-tab")) return;
    pts.set(e.pointerId, local(e));
    if (pts.size === 1) { drag = { p: local(e), x: st.x, y: st.y }; moved = false; }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), k: st.k }; drag = null; }
  });
  box.addEventListener("pointermove", (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, local(e));
    if (pinch && pts.size === 2) {
      const [a, b] = [...pts.values()];
      zoomAt((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, pinch.k * Math.hypot(a[0] - b[0], a[1] - b[1]) / Math.max(1, pinch.d));
      moved = true; return;
    }
    if (!drag) return;
    const [px, py] = local(e), dx = px - drag.p[0], dy = py - drag.p[1];
    if (!moved && Math.hypot(dx, dy) < 5) return;       // drobný pohyb při kliknutí není tažení
    if (!moved) { moved = true; box.classList.add("grabbing"); try { box.setPointerCapture(e.pointerId); } catch (_e) { /* není nutné */ } }
    st.x = drag.x - dx / st.k; st.y = drag.y - dy / st.k; st.user = true; apply();
  });
  const end = (e) => {
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (!pts.size) { drag = null; box.classList.remove("grabbing"); }
  };
  box.addEventListener("pointerup", end); box.addEventListener("pointercancel", end);
  // po tažení nesmí „kliknutí“ otevřít zařízení, nad kterým se tažení pustilo
  box.addEventListener("click", (e) => { if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; } }, true);
  box.addEventListener("dblclick", (e) => {
    if (e.target && e.target.closest && e.target.closest(".map-ctl, .map-tab, .node, .ext")) return;
    const [px, py] = local(e); zoomAt(px, py, st.k * 1.6);
  });
  for (const b of box.querySelectorAll("[data-map]")) b.addEventListener("click", () => {
    const [w, h] = size(), m = b.dataset.map;
    if (m === "in") zoomAt(w / 2, h / 2, st.k * 1.3); else if (m === "out") zoomAt(w / 2, h / 2, st.k / 1.3);
    else if (m === "fit") { fit(); st.user = false; } else { one(); st.user = true; }
  });
  // změna velikosti okna (i první zobrazení skryté záložky): dokud člověk pohledem nehýbal, znovu vejít do okna
  if (typeof ResizeObserver === "function") new ResizeObserver(() => { if (st.set) { if (st.user) apply(); else fit(); } }).observe(box);
  return {
    // po vykreslení obsahu o rozměrech W × H: poprvé vejít do okna, jinak pohled nechat
    // voláno před dokreslením prvků → vejít do okna až po něm (rozsah obsahu se měří z nakresleného)
    content(W, H) { st.W = W; st.H = H; st.set = true; if (st.user) apply(); else { fit(); setTimeout(() => { if (!st.user) fit(); }, 0); } },
    fit,
  };
}
const infraMap = mapControl($("infra-canvas"), $("infra-svg"));

$("infra-list-btn").addEventListener("click", () => {
  const hide = !$("infra-list").hidden;
  setInfraList(!hide);
  try { localStorage.setItem("mtc.infraList", hide ? "0" : "1"); } catch (_e) { /* jen pohodlí */ }
});
function setInfraList(show) {
  $("infra-list").hidden = !show;
  const b = $("infra-list-btn"), label = show ? __("Skrýt seznam zařízení") : __("Zobrazit seznam zařízení");
  b.setAttribute("aria-expanded", show ? "true" : "false");
  b.textContent = show ? "‹" : "›"; b.title = label; b.setAttribute("aria-label", label);
  $("infra-list").parentNode.classList.toggle("nolist", !show);
}
try { if (localStorage.getItem("mtc.infraList") === "0") setInfraList(false); } catch (_e) { /* jen pohodlí */ }

// --- časová osa vedle mapy: události lokality na svislé ose (nahoře teď), klik zvýrazní zařízení v mapě ------
// Neumí vrátit mapu do minulého stavu – ukazuje, kdy se co stalo, a vede na podrobnou osu v Aktivitě.
const itl = { hours: 24, data: null };
const ITL_H = 620;                     // výška osy v px
function itlShow(on) {
  $("infra-tl").hidden = !on; $("infra-tl-on").checked = on;
  $("infra-tl").parentNode.classList.toggle("notl", !on);
}
$("infra-tl-on").addEventListener("change", () => {
  const on = $("infra-tl-on").checked; itlShow(on);
  try { localStorage.setItem("mtc.infraTl", on ? "1" : "0"); } catch (_e) { /* jen pohodlí */ }
  if (on) loadInfraTimeline();
});
try { if (localStorage.getItem("mtc.infraTl") === "0") itlShow(false); } catch (_e) { /* jen pohodlí */ }

async function loadInfraTimeline() {
  if ($("infra-tl").hidden || !tenantId || !siteId()) return;
  try { itl.data = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/timeline?hours=${itl.hours}&cats=alert,port,change,audit`); }
  catch (_e) { return; }
  renderInfraTimeline();
}

function itlHighlight(ids) {
  for (const n of $("infra-svg").querySelectorAll ? $("infra-svg").querySelectorAll(".node.hl") : []) n.classList.remove("hl");
  for (const id of ids) {
    const n = $("infra-svg").querySelector ? $("infra-svg").querySelector(`.node[data-dev="${id}"]`) : null;
    if (n) n.classList.add("hl");
  }
}

function renderInfraTimeline() {
  const d = itl.data; if (!d) return;
  segmented($("itl-range"), [[6, __("6 h")], [24, __("24 h")], [168, __("7 d")]], itl.hours, (h) => { itl.hours = h; loadInfraTimeline(); });
  const ax = $("itl-axis"); ax.textContent = ""; $("itl-pop").hidden = true;
  ax.style.height = ITL_H + "px";
  const t1 = new Date(d.until).getTime(), t0 = new Date(d.since).getTime(), span = t1 - t0;
  const y = (t) => Math.round((t1 - t) / span * (ITL_H - 30)) + 24;          // nahoře teď, dolů do minulosti
  ax.appendChild(el("div", null, "itl-bar"));
  const now = el("div", __("TEĎ"), "itl-now"); ax.appendChild(now);
  // popisky času: 8 dílků
  for (let i = 1; i <= 8; i++) {
    const t = t1 - span * i / 8, lab = el("div", null, "itl-tick"); lab.style.top = y(t) + "px";
    lab.textContent = new Date(t).toLocaleString(LOCALE, itl.hours > 24 ? { weekday: "short", hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit" });
    ax.appendChild(lab);
  }
  // události shluknout po 14 px, ať se značky nepřekrývají
  const groups = new Map();
  for (const e of d.events) {
    const k = Math.round(y(new Date(e.at).getTime()) / 14);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(e);
  }
  const rank = { critical: 3, warning: 2, ok: 1, info: 0 };
  for (const [k, evs] of groups) {
    const sev = evs.reduce((a, e) => (rank[e.sev] > rank[a] ? e.sev : a), "info");
    const b = el("button", evs.length > 1 ? String(evs.length) : "", "itl-ev tl-s-" + sev); b.type = "button";
    b.style.top = (k * 14 - 9) + "px";
    b.title = evs.slice(0, 6).map((e) => `${new Date(e.at).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })} ${e.device ? e.device + ": " : ""}${tlText(e)}`).join("\n");
    b.setAttribute("aria-label", b.title);
    b.addEventListener("click", () => {
      itlHighlight(evs.map((e) => e.device_id).filter(Boolean));
      const pop = $("itl-pop"); pop.textContent = ""; pop.hidden = false; pop.style.top = Math.min(ITL_H - 120, k * 14 + 30) + "px";
      for (const e of evs.slice(0, 8)) {
        const row = el("div", null, "itl-row tl-s-" + e.sev);
        row.append(el("time", new Date(e.at).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })), el("span", (e.device ? e.device + " · " : "") + tlText(e)));
        pop.appendChild(row);
      }
      if (evs.length > 8) pop.appendChild(el("div", __`a dalších ${evs.length - 8}`, "muted"));
      const go = el("button", __("Co se dělo kolem (±5 min)"), "ux-link"); go.type = "button";
      go.addEventListener("click", () => {
        tl.device = ""; tl.mac = ""; tl.macName = ""; tl.cats = new Set(TL_CATS.map((c) => c[0]));
        tl.until = new Date(new Date(evs[0].at).getTime() + 5 * 60000).toISOString(); tl.minutes = 10;
        openSub("activity", "tl");
      });
      pop.appendChild(go);
    });
    ax.appendChild(b);
  }
  $("itl-sum").textContent = d.total ? __`${d.total} událostí` : __("bez událostí");
}

let infraWl = {};
// bezdrátové spoje lokality podle dvojice zařízení – pro mapu i topologii
async function loadWlMap() {
  infraWl = {};
  try { for (const l of (await api("GET", `/tenants/${tenantId}/sites/${siteId()}/links`)).links || []) infraWl[[l.a.device_id, l.b.device_id].sort().join("|")] = l; } catch (_e) { /* mapa funguje i bez nich */ }
}
const wlOf = (a, b) => infraWl[[a, b].sort().join("|")];
const wlTitle = (l) => [__`Bezdrátový spoj ${l.name}`, (WL_STATE[l.state] || l.state) + (l.score != null ? ` · ${l.score}` : ""), ...(l.why || []).map((w) => wlWhy(w)), __("Kliknutím otevřeš kartu spoje.")].join("\n");
function openLink(id) { wl.sel = id; wl.data = null; openSub("devices", "links"); }
async function loadInfra() {
  try { infraData = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/infra`); } catch (e) { return msg(e.message, true); }
  // bezdrátové spoje: čára v mapě dostane barvu podle stavu a otevírá kartu spoje
  await loadWlMap();
  renderInfra();
  loadInfraTimeline();
}
$("infra-ext").addEventListener("change", () => { if (infraData) renderInfra(); });
$("infra-clients").addEventListener("change", () => {
  try { localStorage.setItem("mtc.infraClients", $("infra-clients").checked ? "1" : ""); } catch (_e) { /* jen pohodlí */ }
  if (infraData) renderInfra();
});
try { $("infra-clients").checked = localStorage.getItem("mtc.infraClients") === "1"; } catch (_e) { /* jen pohodlí */ }
const INFRA_CLIENTS_MAX = 10;  // na zařízení; zbytek jako „+N dalších“

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
    if (n.kind === "hub") continue;  // nespravovaný switch jen ve stromu
    const c = el("div", null, "infra-dev" + (n.state === "offline" ? " offline" : ""));
    const head = el("div", null, "row1");
    head.append(el("b", n.label), el("span", n.model || "", "muted"));
    c.appendChild(head);
    if (n.ports.length) c.appendChild(miniPorts(n.ports));
    const foot = el("div", null, "row2 muted");
    foot.append(el("span", (n.version || "").split(" ")[0]), el("span", n.poe_w ? `⚡ ${n.poe_w} W` : ""),
                el("span", `CPU ${n.cpu != null ? n.cpu + " %" : "–"}`), el("span", `RAM ${n.mem != null ? n.mem + " %" : "–"}`));
    c.appendChild(foot);
    // klienti zařízení s MAC (rozbalí se klikem na počet)
    const cl = n.clients || [];
    if (cl.length) {
      const t = el("button", `👥 ${cl.length} ${cl.length === 1 ? __("klient") : cl.length < 5 ? __("klienti") : __("klientů")}`, "linkbtn infra-ccount");
      t.type = "button";
      const ul = el("ul", null, "infra-clients"); ul.hidden = true;
      for (const x of cl) {
        const li = el("li", null, "clickable");
        li.append(clientAvatar(x, "sm"), el("span", clientName(x), "nm"), el("span", x.mac, "mono muted"),
                  el("span", x.ssid || x.port || "", "muted"));
        li.title = [x.vendor, x.ip, x.vlan ? "VLAN " + x.vlan : "", x.note].filter(Boolean).join(" · ");
        li.addEventListener("click", (ev) => { if (ev.stopPropagation) ev.stopPropagation(); openClient(x); });
        withCtx(li, clientName(x), () => clientMenu(x));
        ul.appendChild(li);
      }
      t.addEventListener("click", (ev) => { if (ev.stopPropagation) ev.stopPropagation(); ul.hidden = !ul.hidden; });
      c.append(t, ul);
    }
    c.addEventListener("click", () => openDetail(n.id));
    list.appendChild(c);
  }
  drawInfra();
}

function drawInfra() {
  const d = infraData, svg = $("infra-svg"); svg.textContent = "";
  const NW = 250, NH = 66, GX = 76, LV = 180, WH = 56, TOP = 20, EW = 172, EH = 40;
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
  if ($("infra-clients").checked) {
    // klienti jako listy pod svým zařízením (port / SSID na spoji), nejvýš INFRA_CLIENTS_MAX na zařízení
    for (const n of d.nodes) {
      const cl = n.clients || [];
      cl.slice(0, INFRA_CLIENTS_MAX).forEach((c, k) => kids[n.id].push({ id: `cl-${n.id}-${k}`, client: c, parent_port: c.port,
        level: n.level + 1, parent: n.id }));
      if (cl.length > INFRA_CLIENTS_MAX) kids[n.id].push({ id: `clm-${n.id}`, more: cl.length - INFRA_CLIENTS_MAX, dev: n,
        level: n.level + 1, parent: n.id });
    }
  }
  const leaf = (x) => x.ext || x.client || x.more;
  const all = [...d.nodes, ...Object.values(kids).flat().filter(leaf)];
  for (const n of all) kids[n.id] = kids[n.id] || [];
  // rozložení: listy dostanou sloupce, rodič je uprostřed nad dětmi
  let slot = 0;
  const pos = {};
  const place = (n) => {
    const ch = kids[n.id];
    if (!ch.length) { pos[n.id] = slot; slot += leaf(n) ? 0.7 : 1; return; }
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
  infraMap.content(W, H);
  const cx = (n) => 60 + pos[n.id] * (NW + GX) + NW / 2;
  const y = (n) => TOP + wanRoot * (WH + 90) + n.level * LV;
  const g = (cls) => svgEl("g", cls ? { class: cls } : {});
  const num = (p) => String(p || "").replace(/^\D+/, "") || (p ? String(p).slice(0, 12) : "?");   // přejmenovaný port bez čísla → jméno
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
    card.appendChild(svgEl("text", { x: wx - 144, y: wy + 23, class: "title" }, w.upstream ? __("Brána lokality · mimo správu") : `WAN · ${w.interface || "?"}`));
    card.appendChild(svgEl("text", { x: wx - 144, y: wy + 43, class: "sub" }, w.upstream ? __`výchozí brána ${w.gateway || "?"}` :
      `${w.address || __("bez adresy")}${w.gateway ? " → " + w.gateway : ""}${w.dhcp ? " (DHCP)" : ""}`));
    card.appendChild(svgEl("text", { x: wx + 144, y: wy + 23, class: "stat " + (w.upstream ? "muted" : w.active ? "ok" : "muted") },
      w.upstream ? "" : w.active ? __("aktivní") : __("záloha")));
    if (w.distance) card.appendChild(svgEl("text", { x: wx + 144, y: wy + 43, class: "stat muted" }, `distance ${w.distance}`));
    nodes.appendChild(card);
    const px = cx(host) + (k - (n - 1) / 2) * 70, py = y(host) - 26;
    if (w.port) {
      links.appendChild(curve(wx, wy + WH, px, py - 14, w.active ? "up" : "standby"));
      nodes.appendChild(badge(px, py, num(w.port), "wan"));
    } else links.appendChild(curve(wx, wy + WH, px, y(host), w.upstream ? "ext" : w.active ? "up" : "standby"));
  });
  for (const n of all) {
    const x = cx(n), top = y(n);
    const ch = kids[n.id];
    ch.forEach((c, k) => {
      const bx = x + (k - (ch.length - 1) / 2) * 70, by = top + NH + 26;
      const wlk = leaf(c) ? null : infraWl[[n.id, c.id].sort().join("|")];
      const cv = curve(bx, by + 14, cx(c), leaf(c) ? y(c) : y(c) - 40, (leaf(c) ? (c.client ? "client" : "ext") : c.state === "offline" ? "down" : "up") + (wlk ? " wless s-" + wlk.state : ""));
      links.appendChild(cv);
      if (wlk) {
        // širší neviditelná čára = pohodlný cíl pro klik; popisek se stavem spoje
        const hit = curve(bx, by + 14, cx(c), y(c) - 40, "hit");
        hit.appendChild(svgEl("title", {}, wlTitle(wlk)));
        hit.addEventListener("click", (e) => { e.stopPropagation(); openLink(wlk.id); });
        links.appendChild(hit);
        nodes.appendChild(svgEl("text", { x: (bx + cx(c)) / 2, y: (by + 14 + y(c) - 40) / 2 + 4, class: "wl-mark s-" + wlk.state }, "≋"));
      }
      const fromHub = n.kind === "hub";
      if (!c.more && !fromHub) nodes.appendChild(badge(bx, by, wlk || (c.client && c.client.ssid) ? "≋" : num(c.parent_port), "down"));
      if (!leaf(c) && c.kind !== "hub") nodes.appendChild(badge(cx(c), y(c) - 26, wlk ? "≋" : c.uplink_port ? num(c.uplink_port) : "?", "up"));
    });
    if (n.client) {
      // klient: avatar (logo / ikona / iniciály výrobce), jméno a MAC
      const c = n.client, pill = g("client");
      pill.appendChild(svgEl("title", {}, [clientName(c), c.mac, c.vendor, c.ip, c.ssid ? `${c.ssid} ${c.signal || ""}` : c.port].filter(Boolean).join("\n")));
      pill.appendChild(svgEl("rect", { x: x - EW / 2, y: top, width: EW, height: EH + 6, rx: 8 }));
      if (c.logo) pill.appendChild(svgEl("image", { href: logoUrl(c.logo), x: x - EW / 2 + 6, y: top + 7, width: 32, height: 32, preserveAspectRatio: "xMidYMid meet" }));
      else {
        pill.appendChild(svgEl("circle", { cx: x - EW / 2 + 22, cy: top + 23, r: 15, class: "cav-c", style: `--h:${vendorHue(c.vendor_key || c.vendor)}` }));
        pill.appendChild(svgEl("text", { x: x - EW / 2 + 22, y: top + 28, class: c.icon ? "cav-e" : "cav-t" },
          c.icon && CLIENT_ICONS[c.icon] ? CLIENT_ICONS[c.icon] : vendorInitials(c.vendor)));
      }
      pill.appendChild(svgEl("text", { x: x - EW / 2 + 44, y: top + 20, class: "t1 l" }, shortName(clientName(c), 14)));
      pill.appendChild(svgEl("text", { x: x - EW / 2 + 44, y: top + 36, class: "t2 l mono" }, c.mac));
      pill.addEventListener("click", () => openClient(c));
      nodes.appendChild(pill);
      continue;
    }
    if (n.more) {
      const pill = g("ext more");
      pill.appendChild(svgEl("rect", { x: x - EW / 2, y: top, width: EW, height: EH, rx: 8 }));
      pill.appendChild(svgEl("text", { x, y: top + 25, class: "t1" }, __`+ ${n.more} dalších klientů`));
      pill.addEventListener("click", () => { showView("clients"); $("clients-filter").value = n.dev.label || ""; loadClients(); });
      nodes.appendChild(pill);
      continue;
    }
    if (n.ext) {
      const pill = g("ext");
      pill.appendChild(svgEl("rect", { x: x - EW / 2, y: top, width: EW, height: EH, rx: 8 }));
      pill.appendChild(svgEl("text", { x, y: top + 17, class: "t1" }, shortName(n.label || "?", 18)));
      pill.appendChild(svgEl("text", { x, y: top + 32, class: "t2" }, n.platform ? shortName(n.platform, 20) : __("nespravované")));
      nodes.appendChild(pill);
      continue;
    }
    if (n.kind === "hub") {
      // nespravovaný switch / síť: zařízení se na něm vidí navzájem (MNDP), přímé spoje nejdou určit
      const hub = g("hub");
      hub.appendChild(svgEl("title", {}, __("Zařízení se vidí na stejném portu – je mezi nimi nespravovaný switch nebo síť.\nPro přesnou mapu zapni LLDP.")));
      hub.appendChild(svgEl("rect", { x: x - NW / 2, y: top + 8, width: NW, height: NH - 16, rx: 10, class: "card" }));
      hub.appendChild(svgEl("text", { x, y: top + 31, class: "title c" }, __("⇄ nespravovaný switch / síť")));
      hub.appendChild(svgEl("text", { x, y: top + 48, class: "sub c" }, __`propojuje ${n.members.length} zařízení`));
      nodes.appendChild(hub);
      continue;
    }
    const card = g("node" + (n.state === "offline" ? " offline" : ""));
    if (n.id) card.setAttribute("data-dev", n.id);
    card.appendChild(svgEl("title", {}, [n.label, n.model, n.version, n.stp_root ? __`STP root, priorita ${n.stp_priority ?? "?"}` : "",
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
  if (!d.nodes.length) svg.appendChild(svgEl("text", { x: 20, y: 40, class: "sub" }, __("V lokalitě zatím nejsou spravovaná zařízení s inventurou.")));
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


const yesNo = (v) => el("td", v ? "✓" : "–", v ? "ok" : "muted");

function renderNetworks(n) {
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
              el("td", d.mode === "server" ? __("Server") : d.mode === "relay" ? __`Relay → ${d.target || "?"}` : "–", d.mode ? "" : "muted"),
              leaseCell(x, leases, pct),
              el("td", x.available != null ? String(x.available) : "–"),
              el("td", x.devices != null ? String(x.devices) : "–"));
    tb.appendChild(tr);
  }
  if (!n.networks.length) { const tr = el("tr"); const td = el("td", __("Žádné sítě s adresou (inventura běží každých 5 min)"), "muted"); td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); }
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
  b.title = __("Leases, statické IP a mapa adres");
  b.addEventListener("click", () => loadLeases(x.router_id, x.dhcp.name, x));
  td.appendChild(b);
  return td;
}

const LEASE_KIND = { static: __("rezervace"), dynamic: __("dynamická"), manual: __("ruční IP") };
const LEASE_FILTERS = [["all", __("Vše")], ["static", __("Rezervace")], ["dynamic", __("Dynamické")], ["manual", __("Ruční IP")], ["warn", __("Problémy")]];
const MAP_LABEL = { gateway: __("brána"), static: __("rezervace"), dynamic: __("dynamická"), manual: __("ruční IP"), pool: __("volná v poolu"),
  free: __("volná mimo pool"), conflict: __("konflikt") };
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
  const lines = [["IP", "MAC", __("Výrobce"), __("Hostname"), __("Typ"), __("Popis"), __("Poznámka"), "DHCP options", __("Kde")].map(esc).join(";")];
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
  chip(plural(sm.static, __("rezervace"), __("rezervace"), __("rezervací")), "static");
  chip(plural(sm.dynamic, __("dynamická"), __("dynamické"), __("dynamických")), "dynamic");
  chip(plural(sm.manual, __("s ruční IP"), __("s ruční IP"), __("s ruční IP")), "manual");
  chip(__`volné v poolu ${sm.pool_free} / ${sm.pool_size}`);
  if (sm.conflicts) chip(plural(sm.conflicts, __("konflikt"), __("konflikty"), __("konfliktů")) + " IP", "conflict");
  chip(__`brána ${v.gateway || "?"} · pool ${v.pool.join(", ") || "–"}${v.lease_time ? __(" · lease ") + v.lease_time : ""}`, "muted");
  const bn = $("lease-banner"); bn.textContent = "";
  if (v.module_mode !== "manage") {
    const b = el("div", null, "banner info");
    b.appendChild(document.createTextNode(__("Správa DHCP je na tomto zařízení vypnutá – změny se uloží jako plán ke schválení. ")));
    if (canManage()) {
      const en = el("button", __("Povolit správu DHCP"), "small"); en.type = "button";
      en.onclick = async () => {
        if (!(await ask(__`Povolit controlleru měnit DHCP leases na ${v.device}? Každá změna se aplikuje s automatickým návratem.`))) return;
        try { await api("POST", `/tenants/${tenantId}/devices/${c.devId}/modules`, { module: "dhcp", mode: "manage" }); loadLeases(c.devId, c.server); }
        catch (e) { msg(e.message, true); }
      };
      b.appendChild(en);
    }
    bn.appendChild(b);
  }
  if (!v.oui) bn.appendChild(el("div", __("Výrobce podle MAC: na serveru chybí balík ieee-data (apt install ieee-data)."), "banner"));
  for (const p of v.pending) bn.appendChild(el("div", __`Čeká na aplikaci (${p.status}): ${p.summary}`, "banner warn"));
  drawLeaseNet();
  drawLeaseMap();
  segmented($("lease-filter"), LEASE_FILTERS, c.filter, (f) => { c.filter = f; renderLeases(); });
  drawLeaseRows();
}

// nastavení sítě DHCP pro všechny klienty: DNS, NTP, doména, globální DHCP options (/ip dhcp-server network)
function drawLeaseNet() {
  const c = leaseCtx, v = c.v, n = v.network;
  const box = $("lease-net-body"); box.textContent = "";
  $("lease-net").hidden = false;
  if (!n) {
    $("lease-net-sum").textContent = __("– síť DHCP serveru nenalezena");
    box.appendChild(el("p", __`Pro podsíť ${v.subnet || "?"} na zařízení chybí /ip dhcp-server network – klienti nedostanou bránu ani DNS.`, "sev-warning"));
    return;
  }
  $("lease-net-sum").textContent = `– DNS ${n.dns.join(", ") || __("z routeru")}${n.options.length ? __(" · options ") + n.options.join(", ") : ""}`;
  const f = el("div", null, "pe-grid");
  const field = (label, input, hint) => {
    const l = el("label", null, "pe-field"); l.append(el("span", label), input);
    if (hint) l.appendChild(el("small", hint, "muted"));
    f.appendChild(l); return input;
  };
  const dns = field(__("DNS servery"), el("input"), __("prázdné = router sám (brána)")); dns.value = n.dns.join(", "); dns.placeholder = __("např. 192.168.10.1, 1.1.1.1");
  const ntp = field(__("NTP servery"), el("input")); ntp.value = n.ntp.join(", "); ntp.placeholder = __("volitelné");
  const dom = field(__("Doména"), el("input")); dom.value = n.domain; dom.placeholder = __("např. firma.local");
  const optBox = el("div", null, "pe-tags");
  const checks = [];
  for (const o of v.options) {
    const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.value = o.name;
    cb.checked = n.options.includes(o.name); checks.push(cb);
    l.title = __`kód ${o.code} = ${o.value}`; l.append(cb, document.createTextNode(` ${o.name} (${o.code})`)); optBox.appendChild(l);
  }
  if (!v.options.length) optBox.appendChild(el("span", __("na zařízení zatím nejsou žádné – přidej novou níže"), "muted"));
  field(__("DHCP options pro všechny klienty"), optBox, __("konkrétnímu klientovi přidáš options v jeho rezervaci"));
  // nová option rovnou přiřazená síti
  const nb = el("div", null, "inline newopt");
  const preset = el("select");
  const o0 = el("option", __("+ nová option…")); o0.value = ""; preset.appendChild(o0);
  for (const p of v.presets) { const o = el("option", `${p.code} – ${p.label}`); o.value = String(p.code); preset.appendChild(o); }
  const oc = el("option", __("vlastní kód")); oc.value = "custom"; preset.appendChild(oc);
  const oName = el("input"); oName.placeholder = __("jméno"); oName.size = 10;
  const oCode = el("input"); oCode.type = "number"; oCode.min = 1; oCode.max = 254; oCode.placeholder = __("kód"); oCode.size = 4;
  const oType = el("select");
  for (const [t, l] of [["ip", "IP"], ["text", "text"], ["hex", "hex"]]) { const o = el("option", l); o.value = t; oType.appendChild(o); }
  const oVal = el("input"); oVal.placeholder = __("hodnota"); oVal.size = 18;
  for (const x of [oName, oCode, oType, oVal]) x.hidden = true;
  preset.addEventListener("change", () => {
    const p = v.presets.find((x) => String(x.code) === preset.value);
    for (const x of [oName, oCode, oType, oVal]) x.hidden = !preset.value;
    if (p) { oCode.value = p.code; oType.value = p.type; oName.value = `opt${p.code}`; }
    else if (preset.value === "custom") { oCode.value = ""; oName.value = ""; }
  });
  nb.append(preset, oName, oCode, oType, oVal);
  field(__("Přidat DHCP option"), nb);
  box.appendChild(f);
  const res = el("div", null, "muted");
  const save = el("button", __("Uložit nastavení sítě") + (canManage() ? __(" a použít") : __(" jako plán")), "small"); save.type = "button";
  save.disabled = !canOperate();
  save.addEventListener("click", async () => {
    const list = (s) => s.split(/[,\s]+/).filter(Boolean);
    const desired = { dns: list(dns.value), ntp: list(ntp.value), domain: dom.value.trim(),
      options: checks.filter((x) => x.checked).map((x) => x.value) };
    if (preset.value && oVal.value.trim()) {
      desired.new_options = [{ name: oName.value.trim(), code: Number(oCode.value), type: oType.value, value: oVal.value.trim() }];
      desired.options.push(oName.value.trim());
    }
    try {
      const out = await api("POST", `/tenants/${tenantId}/devices/${c.devId}/dhcp/${encodeURIComponent(c.server)}/network`,
        { desired, apply: canManage() });
      msg((out.command_id ? __("Nastavení sítě odesláno – aplikuje se s automatickým návratem") : __("Plán uložen ke schválení")) +
          (out.warnings && out.warnings.length ? " · " + out.warnings.join(" · ") : ""));
      loadLeases(c.devId, c.server);
    } catch (e) { res.textContent = e.message; res.className = "sev-error"; }
  });
  const bar = el("div", null, "pe-actions"); bar.appendChild(save);
  box.append(bar, res);
}


function drawLeaseMap() {
  const v = leaseCtx.v, box = $("lease-map"); box.textContent = "";
  const lg = $("lease-map-legend"); lg.textContent = "";
  if (!v.map) { box.appendChild(el("p", __("Mapa adres jen pro sítě do /22."), "muted")); return; }
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
  lg.appendChild(el("span", __("klik na volnou adresu = nová rezervace"), "muted"));
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
    if (r.block_access) kind.appendChild(el("span", __("blokován"), "kchip conflict"));
    const note = el("td", r.note || "", "note");
    tr.append(ipc, el("td", r.mac, "mono"), el("td", r.vendor || "–", r.vendor ? "" : "muted"), name, kind,
              el("td", whereText(r) || "–", whereText(r) ? "" : "muted"), note,
              el("td", r.options.join(", ") || "–", r.options.length ? "" : "muted"));
    const act = el("td");
    const eb = el("button", __("Upravit"), "small secondary"); eb.type = "button";
    eb.addEventListener("click", () => { c.edit = c.edit && c.edit.mac === r.mac ? null : { mac: r.mac }; drawLeaseRows(); });
    withCtx(tr, r.ip || r.mac, () => [
      { label: r.kind === "dynamic" ? __("Udělat statickou (rezervace)…") : __("Upravit…"), act: () => { c.edit = { mac: r.mac }; drawLeaseRows(); } },
      { sep: true },
      r.ip ? { label: __("Kopírovat IP"), act: () => copyText(r.ip, "IP") } : null,
      { label: __("Kopírovat MAC"), act: () => copyText(r.mac, "MAC") },
      r.hostname ? { label: __("Kopírovat hostname"), act: () => copyText(r.hostname, __("Hostname")) } : null,
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
  if (!r) { mac = field(__("MAC adresa"), el("input")); mac.placeholder = "AA:BB:CC:DD:EE:FF"; mac.size = 18; mac.value = (c.edit && c.edit.presetMac) || ""; }
  const ipBox = el("span", null, "ipbox");
  const ip = el("input"); ip.size = 13; ip.value = (r ? (r.reserved_ip || r.ip) : c.edit.ip) || "";
  const sug = el("button", __("volná mimo pool"), "small secondary"); sug.type = "button";
  sug.addEventListener("click", () => { if (v.suggest) ip.value = v.suggest; });
  ipBox.append(ip, sug);
  field(r && r.kind === "dynamic" ? __("Statická IP (rezervace)") : __("IP adresa"), ipBox);
  const comment = field(__("Popis na MikroTiku"), el("input")); comment.maxLength = 100; comment.value = r ? r.comment : "";
  comment.placeholder = __("jen ASCII, uloží se do leasu");
  const note = field(__("Poznámka (jen controller)"), el("textarea")); note.maxLength = 500; note.rows = 2; note.value = r ? r.note : "";
  const optBox = el("div", null, "pe-tags");
  const optChecks = [];
  for (const o of v.options) {
    const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.value = o.name;
    cb.checked = !!r && r.options.includes(o.name); optChecks.push(cb);
    l.title = __`kód ${o.code} = ${o.value}`; l.append(cb, document.createTextNode(` ${o.name} (${o.code})`)); optBox.appendChild(l);
  }
  if (!v.options.length) optBox.appendChild(el("span", __("na zařízení zatím nejsou žádné"), "muted"));
  field("DHCP options", optBox);
  // nová option
  const nb = el("div", null, "inline newopt");
  const preset = el("select");
  const o0 = el("option", __("+ nová option…")); o0.value = ""; preset.appendChild(o0);
  for (const p of v.presets) { const o = el("option", `${p.code} – ${p.label}`); o.value = String(p.code); preset.appendChild(o); }
  const oc = el("option", __("vlastní kód")); oc.value = "custom"; preset.appendChild(oc);
  const oName = el("input"); oName.placeholder = __("jméno"); oName.size = 10;
  const oCode = el("input"); oCode.type = "number"; oCode.min = 1; oCode.max = 254; oCode.placeholder = __("kód"); oCode.size = 4;
  const oType = el("select");
  for (const [t, l] of [["ip", "IP"], ["text", "text"], ["hex", "hex"]]) { const o = el("option", l); o.value = t; oType.appendChild(o); }
  const oVal = el("input"); oVal.placeholder = __("hodnota"); oVal.size = 18;
  for (const x of [oName, oCode, oType, oVal]) x.hidden = true;
  preset.addEventListener("change", () => {
    const p = v.presets.find((x) => String(x.code) === preset.value);
    for (const x of [oName, oCode, oType, oVal]) x.hidden = !preset.value;
    if (p) { oCode.value = p.code; oType.value = p.type; oName.value = `opt${p.code}`; }
    else if (preset.value === "custom") { oCode.value = ""; oName.value = ""; }
  });
  nb.append(preset, oName, oCode, oType, oVal);
  field(__("Přidat DHCP option"), nb);
  const al = field(__("Address-listy (firewall)"), el("input")); al.value = r ? r.address_lists : ""; al.placeholder = __("např. tiskarny,iot");
  const blk = el("input"); blk.type = "checkbox"; blk.checked = !!r && r.block_access;
  field(__("Zablokovat přístup"), blk);
  td.appendChild(f);
  const res = el("div", null, "muted");
  const bar = el("div", null, "pe-actions");
  const isStatic = r && r.kind === "static";
  const save = el("button", (isStatic ? __("Uložit") : __("Vytvořit rezervaci")) + (canManage() ? __(" a použít") : __(" jako plán")), "small");
  save.type = "button";
  const cancel = el("button", __("Zavřít"), "small secondary"); cancel.type = "button";
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
      msg((out.command_id ? __("Změna odeslána – aplikuje se s automatickým návratem") : __("Plán uložen ke schválení")) +
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
    const nb2 = el("button", __("Uložit jen poznámku"), "small secondary"); nb2.type = "button";
    nb2.addEventListener("click", async () => { if (await saveNote(r.mac)) { msg(__("Poznámka uložena")); c.edit = null; loadLeases(c.devId, c.server); } });
    bar.appendChild(nb2);
  }
  bar.prepend(save);
  bar.appendChild(cancel);
  if (r && r.kind === "static") {
    const rm = el("button", __("Zrušit rezervaci"), "small danger"); rm.type = "button";
    rm.addEventListener("click", async () => { if ((await ask(__`Zrušit rezervaci ${r.ip} pro ${r.mac}?`))) send({ remove: true }, r.mac); });
    bar.appendChild(rm);
  }
  td.append(bar, res);
  return tr;
}

const MOD_LABEL = { ntp: "NTP", ports: __("porty"), dhcp_snooping: "DHCP snooping", dhcp: "DHCP leases" };

const DG_STATUS = { plan: [__("změna"), "warn"], ok: [__("nastaveno"), "ok"], blocked: [__("nelze bezpečně"), "bad"], skip: [__("přeskočeno"), "muted"] };

function renderSnooping(sn) {
  const rg = $("dg-rogue"); rg.textContent = "";
  for (const r of sn.rogue) rg.appendChild(el("li", __`${r.device} ${r.interface}: cizí DHCP server ${r.servers}`, "sev-error"));
  const src = $("dg-sources"); src.textContent = "";
  const names = {};
  for (const d of sn.devices) names[d.device_id] = d.device;
  for (const x of sn.sources) {
    const li = el("li", x.label);
    src.appendChild(li);
  }
  for (const [k, m] of (sn.policy.sources || []).entries()) {
    const li = el("li", __`ručně: ${m.label} – ${names[m.device_id] || "?"} ${m.port}${m.ip ? ` (${m.ip})` : ""} `, "muted");
    const rm = el("button", __("Odebrat"), "small secondary"); rm.type = "button";
    rm.addEventListener("click", () => saveDhcpPolicy((sn.policy.sources || []).filter((_, i) => i !== k)));
    li.appendChild(rm); src.appendChild(li);
  }
  const mi = $("dg-missing"); mi.textContent = "";
  for (const m of sn.missing) {
    mi.appendChild(el("p", __`${m.label}: nevím, kde v síti je (není mezi zařízeními ani klienty). Zadej port, za kterým leží.`, "banner warn"));
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
    const notes = [...d.reasons, ...d.warnings, ...(d.alerts.length ? [__`DHCP alert na ${d.alerts.join(", ")}`] : [])];
    tr.append(el("td", d.device), el("td", stxt, "dg-" + scls), tp, el("td", d.keep.join(", ") || "–", d.keep.length ? "" : "muted"),
              el("td", d.bridges.map((b) => b.bridge + (b.now ? __(" (už zapnuto)") : "")).join(", ") || "–"),
              el("td", notes.join(" · "), d.status === "blocked" ? "sev-error" : "muted"),
              dgModeCell(d));
    tb.appendChild(tr);
  }
  $("dg-generate").hidden = !sn.devices.some((d) => d.status === "plan");
  $("dg-result").textContent = "";
}

function dgModeCell(d) {
  const td = el("td", d.module_mode === "manage" ? __("spravuje") : __("jen sleduje "), d.module_mode === "manage" ? "" : "muted");
  if (d.module_mode !== "manage" && d.status === "plan" && canManage()) {
    const b = el("button", __("Povolit správu"), "small secondary"); b.type = "button";
    b.addEventListener("click", async () => {
      if (!(await ask(__`Povolit controlleru měnit DHCP snooping na ${d.device}? Každá změna jde přes schválení plánu s automatickým návratem.`))) return;
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
    label: $("dg-m-label").value.trim() || (ip ? `DHCP server ${ip}` : __("ruční zdroj")), ip: ip || null }]);
});

$("dg-generate").addEventListener("click", async () => {
  try {
    const r = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/plans/dhcp_snooping`);
    const n = await approveAll((r.devices || []).filter((d) => d.plan_id));
    $("dg-result").textContent = n ? "" : __("nic k nasazení (zařízení musí mít povolenou správu DHCP snoopingu)");
  } catch (e) { msg(e.message, true); }
});

async function loadStp() {
  try { renderStp(await api("GET", `/tenants/${tenantId}/sites/${siteId()}/stp`)); } catch (e) { msg(e.message, true); }
}

function renderStp(s) {
  $("stp-roots").textContent = s.roots.length
    ? __("Root bridge: ") + s.roots.map((r) => (r.device || __("nespravované zařízení")) + ` (${r.root_id})`).join(", ")
    : __("Žádný bridge se zapnutým STP");
  const ul = $("stp-warn"); ul.textContent = "";
  for (const x of s.warnings) ul.appendChild(el("li", x.text, "sev-" + (x.severity === "info" ? "info" : "warning")));
  const tb = $("stp-bridges"); tb.textContent = "";
  for (const b of s.bridges) {
    const tr = el("tr");
    tr.append(el("td", b.device || "?"), el("td", b.bridge), el("td", b.protocol, b.protocol === "none" ? "muted" : ""),
              el("td", b.root ? __("ano") : (b.root_device || b.root_id || "–")), el("td", b.root_port || "–"),
              el("td", b.root_path_cost != null ? String(b.root_path_cost) : "–"), el("td", b.managed_links.join(", ")));
    tb.appendChild(tr);
  }
  if (!s.bridges.length) { const tr = el("tr"); const td = el("td", __("Žádné bridge (inventura běží každých 5 min)"), "muted"); td.colSpan = 7; tr.appendChild(td); tb.appendChild(tr); }
}

// --- detail zařízení -----------------------------------------------------------------

let detailId = null;
let detailData = null;

// zařízení s Wi-Fi rádii (nebo s rolí AP) má v panelu záložku Wi-Fi
// izolace řízeného zařízení: samo jen na controller a do privátních sítí; výjimka s auditem
function isoCell(d) {
  const box = el("span", d.isolation.internet ? __("povolen (výjimka) ") : __("jen controller a privátní sítě "));
  if (canManage()) {
    const allow = !d.isolation.internet;
    box.appendChild(actBtn(allow ? __("Povolit přístup do internetu") : __("Zrušit výjimku"), async () => {
      if (!(await ask(allow ? __("Povolit tomuto zařízení přístup do internetu? (např. dočasně kvůli diagnostice – zapíše se do auditu)")
                            : __("Zrušit výjimku? Zařízení bude zase smět jen na controller a do privátních sítí.")))) return;
      try { const r = await api("PUT", `/tenants/${tenantId}/devices/${d.device.id}/internet`, { allow }); showApply(r.apply); openDetail(d.device.id); }
      catch (e) { msg(e.message, true); }
    }));
  }
  return box;
}

function hasWifi(d) {
  return (d.interfaces || []).some((i) => /^(wifi|wlan|wireless)/.test(String(i.type || "")) || /^(wifi|wlan)\d/.test(String(i.name || "")))
    || (d.device && d.device.role === "ap");
}

// záložka Wi-Fi panelu zařízení: rádia tohoto AP, SSID, kanály, skupina AP
async function loadDevWifi(id) {
  const box = $("d-wifi"); box.textContent = "";
  const dev = detailData && detailData.device;
  if (!dev || !dev.site_id) { box.appendChild(el("p", __("Zařízení není v lokalitě."), "muted")); return; }
  let w, it;
  try {
    [w, it] = await Promise.all([api("GET", `/tenants/${tenantId}/sites/${dev.site_id}/wifi`),
                                 api("GET", `/tenants/${tenantId}/sites/${dev.site_id}/intent`).catch(() => null)]);
  } catch (e) { box.appendChild(el("p", e.message, "sev-error")); return; }
  if (id !== detailId) return;
  const mine = w.radios.filter((r) => r.device_id === id);
  const groups = ((it && it.intent && it.intent.wifi) || {}).groups || [];
  const di = it && (it.devices || []).find((x) => x.id === id);
  const grp = di && di.ap_group ? (groups.find((g) => g.id === di.ap_group) || {}).name : null;
  const info = el("dl", null, "dq-info");
  const row = (k, v) => { info.append(el("dt", k), el("dd", v)); };
  row(__("Skupina AP"), grp || (groups.length ? __("bez skupiny (vysílá SSID pro všechna AP)") : "–"));
  row("SSID", [...new Set(mine.map((r) => r.ssid).filter(Boolean))].join(", ") || "–");
  row(__("Klienti"), String(mine.reduce((a, r) => a + (r.clients || 0), 0)));
  box.appendChild(info);
  const t = el("table", null, "ptable");
  const hr = el("tr"); for (const h of [__("Rádio"), "SSID", __("Pásmo"), __("Kanál"), __("Šířka"), __("Výkon"), __("Klienti"), __("Signál prům./min")]) hr.appendChild(el("th", h));
  const th = el("thead"); th.appendChild(hr); t.appendChild(th);
  const tb = el("tbody");
  for (const r of mine) {
    const tr = el("tr", null, r.disabled ? "muted" : "");
    tr.append(el("td", r.interface + (r.master ? __` (virt. na ${r.master})` : "") + (r.capsman ? " · CAPsMAN" : "") + (r.disabled ? __(" · vypnuté") : !r.running ? __(" · neběží") : "")),
              el("td", r.ssid || ""), el("td", r.band || ""),
              el("td", r.freq_now ? `${chanNum(r.freq_now)} · ${r.freq_now} MHz` : r.frequency || ""),
              el("td", r.width_now ? `${r.width_now} MHz` : r.width || ""), el("td", r.tx_power != null ? `${r.tx_power} dBm` : "–"),
              el("td", String(r.clients)), el("td", r.signal_avg != null ? `${r.signal_avg} / ${r.signal_min} dBm` : ""));
    tb.appendChild(tr);
  }
  if (!mine.length) { const tr = el("tr"); const td = el("td", __("Rádia zatím bez dat (inventura běží každých 5 min)"), "muted"); td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); }
  t.appendChild(tb);
  box.appendChild(t);
  const go = el("button", __("Nastavení Wi-Fi lokality"), "small secondary"); go.type = "button";
  go.onclick = () => { closeDetail(); openSub("settings", "wifi"); };
  box.appendChild(go);
}

function kv(tbody, rows) {
  tbody.textContent = "";
  for (const row of rows) {
    if (!row) continue;
    const [k, v] = row;
    if (v === undefined || v === null || v === "") continue;
    const td = el("td"); if (typeof v === "object") td.appendChild(v); else td.textContent = String(v);
    const tr = el("tr"); tr.append(el("th", k), td); tbody.appendChild(tr);
  }
}

async function openDetail(id) {
  detailId = id;
  let d;
  try { d = await api("GET", `/tenants/${tenantId}/devices/${id}`); } catch (e) { return msg(e.message, true); }
  show("detail");
  detailData = d;
  $("dt-wifi").hidden = !hasWifi(d);
  openDetailTab(prefs.detailTab || "over");
  renderDevices();
  if (!watch || watch.id !== id) { $("d-live").hidden = true; watchDevice(id, null, true); }
  const dev = d.device, sm = d.summary;
  $("d-title").textContent = dev.identity || dev.serial;
  {
    // podtitul: model · role · stav; nejdůležitější upozornění rovnou jako pruhy nad záložkami
    const sub = $("d-sub"); sub.textContent = "";
    const on = dev.state === "adopted" && !(devOf(id) && devOf(id).online === false);
    const stName = dev.state === "adopted" ? (on ? "online" : "offline") : dev.state;
    sub.append(document.createTextNode([dev.model, dev.role && dev.role !== "unknown" ? dev.role : null].filter(Boolean).join(" · ") + " · "), el("i", "", "dot " + (on ? "ok" : "bad")), el("span", stName, on ? "st-ok" : "st-bad"));
    const bn = $("d-banners"); bn.textContent = "";
    for (const x of (d.warnings || []).slice(0, 2)) bn.appendChild(el("div", x.text, "dd-banner " + (x.severity === "critical" ? "bad" : "warn")));
  }
  renderQuickInfo($("d-quick"), devOf(id).id === id && devOf(id).serial ? devOf(id) : { ...dev, id }, d);
  const w = $("d-warn"); w.textContent = "";
  for (const x of d.warnings) w.appendChild(el("p", (x.severity === "critical" ? "⚠ " : "ⓘ ") + x.text, "sev-" + (x.severity === "critical" ? "error" : "warning")));
  if (dev.inventory_error) w.appendChild(el("p", __("Inventura: ") + dev.inventory_error, "sev-warning"));
  const pk = d.packages;
  if (pk && pk.wifi && !pk.wifi.ok) {
    const p = el("p", __`⚠ Wi-Fi ovladač: ${pk.wifi.problem}`, "sev-warning");
    if (pk.wifi.fix && dev.state === "adopted") {
      const b = el("button", __`Vyměnit na ${pk.wifi.fix.install}`, "small"); b.type = "button";
      b.addEventListener("click", () => sendCommand(dev, "wifi_package",
        __`Stáhnout ${pk.wifi.fix.install}` + (pk.wifi.fix.remove.length ? __`, odinstalovat ${pk.wifi.fix.remove.join(", ")}` : "") +
        __(" a restartovat (Wi-Fi konfigurace rádií se změní, AP pak převezme CAPsMAN)")));
      p.append(" ", b);
    }
    w.appendChild(p);
  }
  if (pk && pk.mismatch && pk.mismatch.length)
    w.appendChild(el("p", __`⚠ Balíčky s jinou verzí než RouterOS (po restartu budou vypnuté): ${pk.mismatch.join(", ")}`, "sev-warning"));
  const listed = (devicesCache || []).find((x) => x.id === id);
  const fl = $("d-flow"); fl.textContent = "";
  if (listed && flowActive(listed)) fl.appendChild(flowBanner(listed));
  const up = $("d-upgrade");
  up.hidden = !(listed && listed.update_available && dev.state === "adopted");
  if (!up.hidden) { up.textContent = __`Aktualizovat → ${listed.latest_version}`; up.onclick = () => upgradeDevice(listed); }
  $("d-notes").hidden = true;
  if (!up.hidden) loadReleaseNotes(id);
  const fw = $("d-firmware");
  fw.hidden = !(dev.state === "adopted" && sm.firmware && sm.firmware_upgrade && sm.firmware_upgrade !== sm.firmware);
  if (!fw.hidden) {
    fw.textContent = `RouterBOOT → ${sm.firmware_upgrade}`;
    fw.onclick = async () => {
      if (!(await ask(__`Aktualizovat RouterBOOT ${sm.firmware} → ${sm.firmware_upgrade}? Zařízení se restartuje.`))) return;
      api("POST", `/tenants/${tenantId}/devices/${id}/commands`, { kind: "firmware" })
        .then(() => msg(__("Aktualizace RouterBOOT naplánována"))).catch((e) => msg(e.message, true));
    };
  }
  const es = $("d-edit-site"); es.textContent = "";
  for (const st of sites) { const o = el("option", st.name); o.value = st.id; es.appendChild(o); }
  es.value = dev.site_id || "";
  const er = $("d-edit-role"); er.textContent = "";
  for (const r of ["unknown", "gateway", "switch", "ap", "ptp"]) { const o = el("option", ROLE_NAME[r]); o.value = r; er.appendChild(o); }
  er.value = dev.role || "unknown";
  $("d-edit-note").value = dev.note || "";
  $("d-edit-name").value = dev.identity_pending || dev.identity || "";
  $("d-edit-name").placeholder = dev.serial;
  $("d-edit-name").title = dev.identity_pending && dev.identity_pending !== dev.identity
    ? __`Nové jméno ${dev.identity_pending} čeká na potvrzení zařízením` : __("Jméno v RouterOS (/system identity)");
  const sec = $("d-security"); sec.textContent = "";
  if (!d.security) sec.appendChild(el("li", __("Zatím bez inventury"), "muted"));
  else if (!d.security.length) sec.appendChild(el("li", __("Bez nálezů ✓"), "muted"));
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
  // upozornění sbalená do jednoho řádku – obsah panelu zůstává nahoře
  const nIss = w.children.length + da.children.length;
  const crit = (d.warnings || []).some((x) => x.severity === "critical") || (d.alerts || []).some((a) => a.severity === "critical");
  $("d-issues").hidden = !nIss;
  $("d-issues-sum").textContent = nIss ? __`⚠ ${nIss} upozornění – zobrazit` : __("Co přinese aktualizace – zobrazit");
  $("d-issues-sum").className = crit ? "sev-error" : "sev-warning";
  kv($("d-summary"), [
    [__("Sériové číslo"), dev.serial], ["RouterOS", dev.ros_version], [__("Kanál / dostupná"), sm.update_channel ? `${sm.update_channel} / ${sm.latest_version || "?"}` : null],
    ["RouterBOOT", sm.firmware ? `${sm.firmware}${sm.firmware_upgrade && sm.firmware_upgrade !== sm.firmware ? " → " + sm.firmware_upgrade : ""}` : null],
    [__("Uptime"), sm.uptime], ["CPU", sm.cpu_load != null ? `${sm.cpu_load} %${sm.cpu ? ` (${sm.cpu})` : ""}` : null],
    [__("Paměť"), sm.memory_used_pct != null ? `${sm.memory_used_pct} %` : null], [__("Úložiště"), sm.hdd_used_pct != null ? `${sm.hdd_used_pct} %` : null],
    [__("Role"), ROLE_NAME[dev.role] || dev.role],
    [__("Režim"), (() => { const x = el("a", dev.managed_at ? __("Řízené – controller nastavuje porty, VLAN, NTP a Wi-Fi") : __("Sledované – controller jen čte"));
      x.href = "#"; x.title = __("Otevřít záložku Řízení"); x.addEventListener("click", (e) => { e.preventDefault(); openDetailTab("manage"); }); return x; })()],
    d.isolation ? [__("Internet"), isoCell(d)] : null,
    [__("Lokalita"), dev.site], [__("Poznámka"), dev.note], isLocal() ? [__("Adresa správy"), dev.tunnel_ip || "–"] : [__("Tunel"), __`${dev.tunnel_ip || "–"} přes ${dev.concentrator || "–"}`],
    [__("Check-in"), fmtTime(dev.last_checkin_at)], [__("Inventura"), fmtTime(dev.inventory_at)], [__("Záloha"), fmtTime(dev.backup_at)],
  ]);
  // moduly
  const mt = $("d-modules"); mt.textContent = "";
  for (const [mod, mode] of Object.entries(d.modules)) {
    const tr = el("tr"); tr.appendChild(el("th", MOD_LABEL[mod] || mod));
    const td = el("td", mode === "manage" ? __("spravuje ") : __("jen sleduje "));
    if (MOD_LABEL[mod]) {
      const b = el("button", mode === "manage" ? __("Přepnout na sledování") : __("Povolit správu"), "small secondary");
      b.onclick = async () => {
        const to = mode === "manage" ? "observe" : "manage";
        if (to === "manage" && !(await ask(__`Povolit controlleru měnit ${MOD_LABEL[mod]} na tomto zařízení (vždy až po schválení plánu)?`))) return;
        try { await api("POST", `/tenants/${tenantId}/devices/${id}/modules`, { module: mod, mode: to }); openDetail(id); }
        catch (e) { msg(e.message, true); }
      };
      td.appendChild(b);
    }
    tr.appendChild(td); mt.appendChild(tr);
  }
  if (d.ntp) kv($("d-ntp"), [[__("Klient"), d.ntp.client_enabled ? __("zapnutý") : __("vypnutý")], [__("Servery"), d.ntp.servers.join(", ") || "–"],
                             [__("Stav"), d.ntp.status], ["NTP server", d.ntp.server_enabled ? __("zapnutý") : __("vypnutý")], [__("Časová zóna"), d.ntp.timezone]]);
  // VLAN
  const vs = $("d-vlan-status"), vl = $("d-vlan"); vl.textContent = "";
  $("d-vlan-preview").hidden = true;
  if (d.vlan) {
    const label = { conforming: __("odpovídá modelu"), simple: __("bez VLAN"), legacy: __("starý způsob – doporučena migrace"), unknown: __("bez dat") };
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
    tr.title = __("Graf provozu");
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
    const stp = p.stp_role ? p.stp_role.replace("-port", "") + (p.stp_forwarding === false ? __(" (blokuje)") : "") + (p.edge ? " · edge" : "") : "";
    tr.append(el("td", i.name), el("td", i.type), el("td", i.disabled ? __("vypnuté") : i.running ? __("běží") : __("neběží"), i.running ? "" : "sev-warning"),
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
    tr.append(el("td", new Date(b.taken_at).toLocaleString(LOCALE)), el("td", b.reason), el("td", `${Math.round(b.size_bytes / 1024)} kB`),
              el("td", b.changed ? __("ano") : "–"));
    const act = el("td");
    if (b.changed) {
      const df = el("button", __("Rozdíl"), "small secondary");
      df.onclick = async () => {
        try { const r = await api("GET", `/tenants/${tenantId}/devices/${id}/backups/${b.id}/diff`);
              const pre = $("d-diff"); pre.textContent = r.diff || __("Bez rozdílu."); pre.hidden = false; }
        catch (e) { msg(e.message, true); }
      };
      act.appendChild(df);
    }
    const a = el("a", __(" Stáhnout")); a.href = `/api/admin/tenants/${tenantId}/devices/${id}/backups/${b.id}/download`;
    a.title = __("Celý export včetně hesel (jen admin tenanta, auditováno)");
    act.appendChild(a);
    tr.appendChild(act); bt.appendChild(tr);
  }
  if (!d.backups.length) { const tr = el("tr"); const td = el("td", __("Zatím žádná záloha"), "muted"); td.colSpan = 5; tr.appendChild(td); bt.appendChild(tr); }
  // historie
  const ht = $("d-history"); ht.textContent = "";
  const PLAN_ST = { draft: __("návrh"), queued: __("ve frontě"), confirming: __("čeká na potvrzení"), applied: __("provedeno"),
                    rolled_back: __("vráceno zařízením"), failed: __("selhalo"), superseded: __("zastaralý"), discarded: __("zahozen") };
  const hist = d.plans.map((p) => [p.created_at, __`plán ${p.module}: ${p.summary}`, PLAN_ST[p.status] || p.status])
    .concat(d.commands.filter((c) => c.kind !== "confirm_plan").map((c) => [c.created_at, __`příkaz ${c.kind}`, c.status]))
    .sort((a, b) => (a[0] < b[0] ? 1 : -1)).slice(0, 25);
  for (const [t, what, st] of hist) { const tr = el("tr"); tr.append(el("td", new Date(t).toLocaleString(LOCALE)), el("td", what), el("td", st)); ht.appendChild(tr); }
}

function closeDetail() {
  $("detail").hidden = true; $("port-edit").hidden = true; detailId = null; detailData = null;
  stopWatch();
  renderDevices();
}
$("d-close").addEventListener("click", closeDetail);
$("d-backup").addEventListener("click", async () => {
  try { const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/backup`); msg(r.detail); } catch (e) { msg(e.message, true); }
});
$("d-vlan-plan").addEventListener("click", async () => {
  try {
    const r = await api("GET", `/tenants/${tenantId}/devices/${detailId}/vlan-migration`);
    const p = r.plan;
    const steps = $("d-vlan-steps"), notes = $("d-vlan-notes");
    steps.textContent = ""; notes.textContent = "";
    if (!p.ok) for (const b of p.blockers) notes.appendChild(el("li", __("Nelze automaticky: ") + b, "sev-error"));
    for (const s of p.steps) steps.appendChild(el("li", s));
    for (const w of p.warnings) notes.appendChild(el("li", w));
    $("d-vlan-script").textContent = p.script || "";
    $("d-vlan-preview").hidden = false;
  } catch (e) { msg(e.message, true); }
});
setInterval(() => {
  if (!$("app").hidden && tenantId) Promise.all([loadDevices(), loadAlerts()]).then(renderOverview).catch(() => {});
}, 15000);

// --- detail: logy ze zařízení (syslog přes tunel) ----------------------------------------------
const LOG_LEVELS = [["off", __("Vypnuto")], ["errors", __("Chyby a varování")], ["info", __("Vše (info)")]];
const LOG_HINT = {
  off: __("Zařízení logy neposílá."),
  errors: __("Jen kritické, chyby a varování – obvykle jednotky kB denně."),
  info: __("I informační zprávy (přihlášení, DHCP, Wi-Fi připojení, linky) – obvykle desítky kB až jednotky MB denně, ") +
        __("podle provozu. Nad 600 řádků za minutu se zbytek zahodí."),
};
const SEV_NAME = ["emerg", "alert", __("kritické"), __("chyba"), __("varování"), "notice", "info", "debug"];
const logState = { id: null, q: "", sev: 7, live: false, timer: null, data: null };

function fmtBytes(n) {
  if (n == null) return "–";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return Math.round(n / 1e3) + " kB";
  return n + " B";
}

async function loadLogs(id, more) {
  if (!id) return;
  let qs = `q=${encodeURIComponent(logState.q)}&sev=${logState.sev}`;
  if (more && logState.data && logState.data.logs.length) qs += `&before=${logState.data.logs[logState.data.logs.length - 1].id}`;
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/devices/${id}/logs?${qs}`); } catch (e) { return msg(e.message, true); }
  if (detailId !== id) return;
  if (more && logState.data) v.logs = logState.data.logs.concat(v.logs);
  logState.id = id; logState.data = v;
  renderLogs();
}

function renderLogs() {
  const v = logState.data, id = logState.id;
  segmented($("lg-level"), LOG_LEVELS.map(([k, l]) => [k, l, !canManage()]), v.level, async (lvl) => {
    if (!(await ask(__`${LOG_HINT[lvl]}\n\nZměnit logy zařízení na „${LOG_LEVELS.find((x) => x[0] === lvl)[1]}“?`, __("Změnit")))) return;
    try { await api("POST", `/tenants/${tenantId}/devices/${id}/syslog`, { level: lvl }); msg(__("Nastavení logů posláno na zařízení")); loadLogs(id); }
    catch (e) { msg(e.message, true); }
  });
  const vol = v.volume;
  $("lg-volume").textContent = vol.days.length
    ? __`≈ ${fmtBytes(vol.avg_bytes_per_day)} denně · ${vol.avg_lines_per_day} řádků/den${vol.dropped ? ` · zahozeno ${vol.dropped}` : ""} · uchovává se ${v.retention_days} dní`
    : __`uchovává se ${v.retention_days} dní`;
  $("lg-hint").textContent = LOG_HINT[v.level];
  segmented($("lg-sev"), [[7, __("vše")], [4, __("varování+")], [3, __("chyby+")]], logState.sev, (s) => { logState.sev = s; loadLogs(id); });
  drawLogVolume(vol.days);
  const tb = $("lg-rows"); tb.textContent = "";
  for (const r of v.logs) {
    const tr = el("tr", null, "lg-" + (r.severity <= 3 ? "err" : r.severity === 4 ? "warn" : "info"));
    const t = new Date(r.at);
    tr.append(el("td", t.toLocaleString(LOCALE, { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }), "mono small"),
              el("td", SEV_NAME[r.severity] || r.severity, "lg-sev"), el("td", r.topics, "muted small"), el("td", r.message, "wrap"));
    tb.appendChild(tr);
  }
  if (!v.logs.length) {
    const tr = el("tr"); const td = el("td", v.level === "off" ? __("Logy jsou vypnuté – zapni je nahoře.") : __("Zatím žádné logy."), "muted");
    td.colSpan = 4; tr.appendChild(td); tb.appendChild(tr);
  }
  $("lg-more").hidden = v.logs.length < 300;
}

// objem logů po dnech – sloupce (bajty), řádky v popisku
function drawLogVolume(days) {
  const box = $("lg-chart"); box.textContent = "";
  if (!days.length) return;
  const W = 280, H = 56, max = Math.max(...days.map((d) => d.bytes), 1), bw = W / days.length;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H + 14}`, width: W, height: H + 14, class: "lg-bars" });
  days.forEach((d, i) => {
    const h = Math.max(2, (d.bytes / max) * H);
    const r = svgEl("rect", { x: i * bw + 3, y: H - h, width: bw - 6, height: h, rx: 2 });
    r.appendChild(svgEl("title", {}, __`${d.day}: ${fmtBytes(d.bytes)}, ${d.lines} řádků${d.dropped ? `, zahozeno ${d.dropped}` : ""}`));
    svg.appendChild(r);
    svg.appendChild(svgEl("text", { x: i * bw + bw / 2, y: H + 12 }, d.day.slice(8) + "."));
  });
  box.appendChild(svg);
}

$("lg-q").addEventListener("input", () => {
  clearTimeout(logState.qt);
  logState.qt = setTimeout(() => { logState.q = $("lg-q").value.trim(); loadLogs(detailId); }, 300);
});
$("lg-refresh").addEventListener("click", () => loadLogs(detailId));
$("lg-more").addEventListener("click", () => loadLogs(detailId, true));
$("lg-live").addEventListener("change", () => {
  clearInterval(logState.timer);
  if ($("lg-live").checked) logState.timer = setInterval(() => {
    if (detailId && !$("dp-logs").hidden && !$("detail").hidden) loadLogs(detailId); else { clearInterval(logState.timer); $("lg-live").checked = false; }
  }, 5000);
});

// --- detail: test rychlosti mezi zařízeními ----------------------------------------------------
const bwState = { id: null, v: null, dur: 10, dir: "both", proto: "tcp", poll: null };
const BW_DIR = [["both", __("oba směry")], ["transmit", __("odesílání")], ["receive", __("příjem")]];

async function loadBw(id) {
  if (!id) return;
  let v;
  try { v = await api("GET", `/tenants/${tenantId}/devices/${id}/bwtest`); } catch (e) { return msg(e.message, true); }
  if (detailId !== id) return;
  bwState.id = id; bwState.v = v;
  renderBw();
  const run = v.tests.find((t) => t.status === "running");
  if (run) bwPoll(run.id);
}

function renderBw() {
  const v = bwState.v;
  const to = $("bw-to"), prev = to.value; to.textContent = "";
  const sorted = [...v.targets].sort((a, b) => (b.same_site - a.same_site) || (a.site || "").localeCompare(b.site || ""));
  for (const t of sorted) { const o = el("option", `${t.name}${t.same_site ? "" : " · " + (t.site || __("jiná lokalita"))}`); o.value = t.id; to.appendChild(o); }
  to.value = sorted.some((t) => t.id === prev) ? prev : (sorted[0] ? sorted[0].id : "");
  if (!sorted.length) { const o = el("option", __("— žádné jiné online zařízení —")); o.value = ""; to.appendChild(o); }
  bwAddrs();
  segmented($("bw-dur"), v.durations.map((d) => [d, `${d} s`]), bwState.dur, (d) => { bwState.dur = d; renderBw(); });
  segmented($("bw-dir"), BW_DIR, bwState.dir, (d) => { bwState.dir = d; renderBw(); });
  segmented($("bw-proto"), [["tcp", "TCP"], ["udp", "UDP"]], bwState.proto, (p) => { bwState.proto = p; renderBw(); });
  // odhad dat: na každých 100 Mb/s rychlosti
  const mb = bwState.dur * 100e6 / 8 / 1e6 * (bwState.dir === "both" ? 2 : 1);
  $("bw-est").textContent = __`Spotřeba dat ≈ ${Math.round(mb)} MB na každých 100 Mb/s rychlosti linky (pozor na LTE a měřené linky).`;
  $("bw-start").disabled = !canOperate() || !sorted.length;
  const h = $("bw-hist"); h.textContent = "";
  for (const t of v.tests) h.appendChild(bwCard(t));
  if (!v.tests.length) h.appendChild(el("p", __("Zatím žádný test."), "muted"));
}
function bwAddrs() {
  const t = bwState.v.targets.find((x) => x.id === $("bw-to").value);
  const a = $("bw-addr"); a.textContent = "";
  for (const x of (t ? t.addresses : [])) { const o = el("option", `${x.address} (${x.interface})`); o.value = x.address; a.appendChild(o); }
  a.value = t && t.addresses.length ? t.addresses[0].address : "";
}
$("bw-to").addEventListener("change", bwAddrs);

$("bw-start").addEventListener("click", async () => {
  const t = bwState.v.targets.find((x) => x.id === $("bw-to").value);
  if (!t) return;
  if (!(await ask(__`Test rychlosti ${bwState.dur} s k ${t.name} (${$("bw-addr").value})?\n\n${$("bw-est").textContent}`, __("Spustit")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${bwState.id}/bwtest`,
      { to: t.id, address: $("bw-addr").value, duration: bwState.dur, direction: bwState.dir, protocol: bwState.proto });
    bwPoll(r.id, Date.now());
  } catch (e) { msg(e.message, true); }
});

function bwPoll(testId, started) {
  clearInterval(bwState.poll);
  const live = $("bw-live"); live.hidden = false;
  const t0 = started || Date.now();
  const tick = async () => {
    const s = Math.round((Date.now() - t0) / 1000);
    live.textContent = "";
    const bar = el("div", null, "bw-bar"); const fill = el("i"); fill.style.width = Math.min(100, (s / (bwState.dur + 6)) * 100) + "%";
    bar.appendChild(fill);
    live.append(el("b", __`Test běží… ${s} s`), bar);
    let v;
    try { v = await api("GET", `/tenants/${tenantId}/devices/${bwState.id}/bwtest`); } catch (_e) { return; }
    const t = v.tests.find((x) => x.id === testId);
    if (t && t.status !== "running") {
      clearInterval(bwState.poll); live.hidden = true; bwState.v = v; renderBw();
      msg(t.status === "done" ? __`Test hotový: ↑ ${fmtBps(t.result.tx_avg)} / ↓ ${fmtBps(t.result.rx_avg)}` : __`Test selhal: ${t.error}`, t.status !== "done");
    }
  };
  bwState.poll = setInterval(tick, 2000);
  tick();
}

function bwCard(t) {
  const c = el("div", null, "bw-card " + t.status);
  const head = el("div", null, "bw-head");
  head.append(el("b", `${t.from_name || "?"} → ${t.to_name || "?"}`), el("span", `${t.address} · ${t.params.duration} s · ${t.params.protocol.toUpperCase()}`, "muted small"),
              el("span", fmtTime(t.started_at), "muted small"));
  c.appendChild(head);
  if (t.status === "running") { c.appendChild(el("div", __("běží…"), "muted")); return c; }
  if (t.status === "failed") { c.appendChild(el("div", t.error || __("selhalo"), "sev-error")); return c; }
  const r = t.result;
  const nums = el("div", null, "bw-nums");
  const num = (label, val, sub, cls) => { const d = el("div", null, "bw-num " + (cls || "")); d.append(el("span", label), el("b", val), el("small", sub || "")); nums.appendChild(d); };
  num(__("↑ odesílání"), fmtBps(r.tx_avg), r.tx_max ? `max ${fmtBps(r.tx_max)}` : "", "tx");
  num(__("↓ příjem"), fmtBps(r.rx_avg), r.rx_max ? `max ${fmtBps(r.rx_max)}` : "", "rx");
  num(__("CPU zdroj / cíl"), `${r.cpu_local ?? "–"} % / ${r.cpu_remote ?? "–"} %`, (r.cpu_local >= 90 || r.cpu_remote >= 90) ? __("limitem je CPU") : "", (r.cpu_local >= 90 || r.cpu_remote >= 90) ? "warn" : "");
  if (r.lost && r.lost !== "0") num(__("ztracené pakety"), r.lost, "", "warn");
  c.append(nums, bwChart(r.samples));
  return c;
}

// průběh testu: odesílání a příjem po sekundách
function bwChart(samples) {
  const W = 560, H = 120, P = 28;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "bw-chart" });
  const max = Math.max(1, ...samples.map((s) => Math.max(s.tx || 0, s.rx || 0)));
  const n = Math.max(1, samples.length - 1);
  const x = (i) => P + (i / n) * (W - P - 6), y = (v) => H - 16 - (v / max) * (H - 26);
  for (const f of [0.5, 1]) {
    svg.appendChild(svgEl("line", { x1: P, x2: W - 6, y1: y(max * f), y2: y(max * f), class: "grid" }));
    svg.appendChild(svgEl("text", { x: 2, y: y(max * f) + 4, class: "ax" }, fmtBps(max * f).replace(" ", "")));
  }
  for (const [k, cls] of [["tx", "tx"], ["rx", "rx"]]) {
    const pts = samples.map((s, i) => (s[k] == null ? null : `${x(i).toFixed(1)},${y(s[k]).toFixed(1)}`)).filter(Boolean);
    if (pts.length > 1) svg.appendChild(svgEl("polyline", { points: pts.join(" "), class: "ln " + cls }));
  }
  svg.appendChild(svgEl("text", { x: W - 6, y: H - 2, class: "ax end" }, `${samples.length} s`));
  return svg;
}

// --- Wi-Fi: roaming a časová osa klienta ----------------------------------------------------------
const roamState = { hours: 24, data: null };
const RANGES = [[1, "1 h"], [6, "6 h"], [24, "24 h"], [72, __("3 dny")], [168, __("7 dní")]];
const FIND_ICON = { warning: "⚠", info: "ⓘ", ok: "✓" };
const ROAM_HOW = { ft: __("rychlý přechod (FT)"), roam: __("přechod"), reconnect: __("přechod s plným přihlášením"), inferred: __("přechod (odvozený z inventury)") };

async function loadRoaming() {
  let r;
  try { r = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi/roaming?hours=${roamState.hours}`); } catch (e) { return msg(e.message, true); }
  roamState.data = r;
  renderRoaming();
}

const laneName = (lanes, key) => { const l = lanes.find((x) => x.key === key); return l ? `${l.ap} ${l.radio}${l.band ? " · " + l.band + " GHz" : ""}` : key; };

function renderRoaming() {
  const r = roamState.data;
  segmented($("roam-range"), RANGES, roamState.hours, (h) => { roamState.hours = h; loadRoaming(); });
  const lg = r.logging || { on_: 0, n: 0 };
  $("roam-log").textContent = __`Wi-Fi události: ${lg.on_} z ${lg.n} zařízení`;
  const bn = $("roam-banner"); bn.textContent = "";
  if (!lg.on_) {
    const b = el("div", null, "banner info");
    b.appendChild(document.createTextNode(__("Přesné časy roamingu a důvody odpojení přijdou z logů AP. Bez nich je časová osa jen podle inventury (po 5 minutách). ")));
    if (canManage()) {
      const on = el("button", __("Zapnout Wi-Fi události na lokalitě"), "small"); on.type = "button";
      on.onclick = async () => {
        if (!(await ask(__("Zapnout na všech zařízeních s Wi-Fi posílání událostí klientů (připojení, odpojení, roaming)?\n\nData: obvykle desítky kB až jednotky MB denně na AP podle počtu klientů."), __("Zapnout")))) return;
        try { const x = await api("POST", `/tenants/${tenantId}/sites/${siteId()}/syslog`, { wifi: true }); msg(__`Wi-Fi události zapnuté na ${x.devices} zařízeních`); loadRoaming(); }
        catch (e) { msg(e.message, true); }
      };
      b.appendChild(on);
    }
    bn.appendChild(b);
  }
  const box = $("roam-clients"); box.textContent = "";
  for (const c of r.clients.filter((x) => x.score > 0 || x.roams).slice(0, 40)) {
    const row = el("div", null, "roam-row clickable");
    const nm = el("div", null, "roam-nm");
    const txt = el("div", null, "roam-txt"); txt.append(el("b", clientName(c)), el("span", c.mac, "mono muted small"));
    nm.append(clientAvatar(c, "sm"), txt);
    const st = el("div", null, "roam-st");
    st.append(el("span", `↔ ${c.roams}`, "wpill"), el("span", `✕ ${c.disconnects}`, "wpill" + (c.disconnects >= 3 ? " bad" : "")),
              el("span", c.min_signal != null ? `${c.min_signal} dBm` : "–", "wpill" + (c.min_signal != null && c.min_signal <= -75 ? " bad" : "")));
    const fs = el("div", null, "roam-f");
    for (const f of c.findings.slice(0, 3)) fs.appendChild(el("span", `${FIND_ICON[f.severity] || ""} ${f.text}`, "rf " + f.severity));
    row.append(nm, st, fs);
    row.addEventListener("click", () => openJourney(c.mac));
    withCtx(row, clientName(c), () => [{ label: __("Wi-Fi časová osa"), act: () => openJourney(c.mac) },
      { label: __("Upravit klienta…"), act: () => openClient(c) }, { label: __("Kopírovat MAC"), act: () => copyText(c.mac, "MAC") }]);
    box.appendChild(row);
  }
  if (!box.children.length) box.appendChild(el("p", r.clients.length ? __("Žádný klient s problémem v tomto období ✓") : __("Zatím žádná data o Wi-Fi klientech."), "muted"));
  // matice přechodů: odkud → kam, počet
  const mx = $("roam-matrix"); mx.textContent = "";
  if (!r.matrix.length) { mx.appendChild(el("p", __("Žádné přechody."), "muted")); return; }
  const max = Math.max(...r.matrix.map((x) => x.n));
  const t = el("table", null, "roam-mx");
  for (const x of r.matrix.slice(0, 20)) {
    const tr = el("tr");
    const bar = el("td", null, "mxcell"); const i = el("i", null, "mxbar"); i.style.width = Math.max(6, (x.n / max) * 100) + "%"; bar.appendChild(i);
    tr.append(el("td", laneName(r.lanes, x.from), "small"), el("td", "→", "muted"), el("td", laneName(r.lanes, x.to), "small"),
              el("td", String(x.n), "num"), bar);
    t.appendChild(tr);
  }
  mx.appendChild(t);
}

// --- časová osa klienta ---
const jrState = { mac: null, hours: 6, data: null };
async function openJourney(mac, hours) {
  jrState.mac = mac; if (hours) jrState.hours = hours;
  let j;
  try { j = await api("GET", `/tenants/${tenantId}/sites/${siteId()}/wifi/journey?mac=${encodeURIComponent(mac)}&hours=${jrState.hours}`); }
  catch (e) { return msg(e.message, true); }
  jrState.data = j;
  // časová osa je záložka panelu klienta
  if ($("cl-dlg").hidden || !clEdit || clEdit.c.mac !== mac) openClient({ ...j.client, mac });
  $("clt-jr").hidden = false;
  clTab("jr");
  renderJourney();
}

function renderJourney() {
  const j = jrState.data, c = j.client;
  segmented($("jr-range"), RANGES, jrState.hours, (h) => openJourney(jrState.mac, h));
  const ul = $("jr-findings"); ul.textContent = "";
  for (const f of j.findings) {
    const li = el("li", null, "jf " + f.severity);
    li.append(el("b", `${FIND_ICON[f.severity] || ""} ${f.text}`));
    if (f.fix) li.appendChild(el("div", f.fix, "muted small"));
    ul.appendChild(li);
  }
  const lg = $("jr-legend"); lg.textContent = "";
  for (const [cls, t] of [["sig-good", __("signál ≥ −60")], ["sig-mid", __("−60 až −72")], ["sig-bad", __("slabý < −72")], ["mk-ft", __("FT přechod")],
                          ["mk-re", __("plné přihlášení")], ["mk-inf", __("odvozený")], ["mk-dis", __("odpojení")], ["mk-fail", __("chyba přihlášení")]]) {
    const s = el("span"); s.append(el("i", null, "lg " + cls), document.createTextNode(t)); lg.appendChild(s);
  }
  drawJourney(j);
  const tb = $("jr-events"); tb.textContent = "";
  const KIND = { connected: __("připojení"), disconnected: __("odpojení"), roamed: __("přechod"), reassoc: __("reasociace"), auth_fail: __("chyba přihlášení"), other: __("jiné") };
  for (const e of [...j.events].reverse()) {
    const tr = el("tr", null, e.kind === "disconnected" || e.kind === "auth_fail" ? "lg-err" : "");
    tr.append(el("td", new Date(e.at).toLocaleString(LOCALE), "mono small"), el("td", KIND[e.kind] + (e.ft ? " (FT)" : "")),
              el("td", laneName(j.lanes, e.lane), "small"), el("td", e.signal != null ? `${e.signal} dBm` : "–"), el("td", e.reason || "", "muted"));
    tb.appendChild(tr);
  }
  $("jr-evn").textContent = `(${j.events.length})`;
}

// plavecké dráhy: řádek = AP/rádio, pruh = připojení (barva podle signálu), značky = přechody a odpojení;
// pod tím průběh signálu s hranicí −75 dBm
function drawJourney(j) {
  const svg = $("jr-svg"); svg.textContent = "";
  const t0 = new Date(j.from).getTime(), t1 = new Date(j.until).getTime();
  const lanes = j.lanes, LW = 170, W = 980, RH = 30, top = 22, sigH = 90;
  const H = top + lanes.length * RH + 24 + sigH + 22;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", W); svg.setAttribute("height", H);
  const x = (t) => LW + ((new Date(t).getTime() - t0) / Math.max(1, t1 - t0)) * (W - LW - 10);
  const yl = (key) => top + lanes.findIndex((l) => l.key === key) * RH;
  // časová osa
  const ticks = 6;
  for (let i = 0; i <= ticks; i++) {
    const t = t0 + ((t1 - t0) * i) / ticks, xx = x(t);
    svg.appendChild(svgEl("line", { x1: xx, x2: xx, y1: top - 4, y2: H - 18, class: "jr-grid" }));
    const d = new Date(t);
    svg.appendChild(svgEl("text", { x: xx, y: 12, class: "jr-ax" },
      (t1 - t0 > 36e5 * 30 ? d.toLocaleDateString(LOCALE, { day: "numeric", month: "numeric" }) + " " : "") +
      d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" })));
  }
  lanes.forEach((l, i) => {
    const y = top + i * RH;
    svg.appendChild(svgEl("rect", { x: 0, y, width: W, height: RH, class: i % 2 ? "jr-row alt" : "jr-row" }));
    svg.appendChild(svgEl("text", { x: 8, y: y + 13, class: "jr-lane" }, shortName(l.ap, 18)));
    svg.appendChild(svgEl("text", { x: 8, y: y + 25, class: "jr-lane2" }, `${l.radio}${l.band ? " · " + l.band + " GHz" : ""}`));
  });
  const sigCls = (s) => (s == null ? "sig-none" : s >= -60 ? "sig-good" : s >= -72 ? "sig-mid" : "sig-bad");
  for (const s of j.segments) {
    const y = yl(s.lane) + 7, x1 = x(s.start), x2 = Math.max(x1 + 3, x(s.end));
    const sig = s.signals.map((p) => p.signal).filter((v) => v != null);
    const avg = sig.length ? Math.round(sig.reduce((a, b) => a + b, 0) / sig.length) : null;
    const r = svgEl("rect", { x: x1, y, width: x2 - x1, height: RH - 14, rx: 4, class: "jr-seg " + sigCls(avg) });
    r.appendChild(svgEl("title", {}, `${laneName(lanes, s.lane)}\n${new Date(s.start).toLocaleTimeString(LOCALE)} – ${new Date(s.end).toLocaleTimeString(LOCALE)}` +
      (avg != null ? __`\nprůměrný signál ${avg} dBm, nejhorší ${Math.min(...sig)} dBm` : "")));
    svg.appendChild(r);
  }
  // přechody mezi AP: svislá spojnice
  for (const r of j.roams) {
    const xx = x(r.at), y1 = yl(r.from) + RH / 2, y2 = yl(r.to) + RH / 2;
    const cls = r.how === "ft" ? "mk-ft" : r.how === "inferred" ? "mk-inf" : "mk-re";
    const g = svgEl("g", { class: "jr-roam " + cls });
    g.appendChild(svgEl("line", { x1: xx, x2: xx, y1, y2 }));
    g.appendChild(svgEl("circle", { cx: xx, cy: y2, r: 4 }));
    g.appendChild(svgEl("title", {}, `${new Date(r.at).toLocaleTimeString(LOCALE)} ${ROAM_HOW[r.how]}\n${laneName(lanes, r.from)} → ${laneName(lanes, r.to)}`));
    svg.appendChild(g);
  }
  for (const e of j.events.filter((e) => e.kind === "disconnected" || e.kind === "auth_fail")) {
    const xx = x(e.at), yy = yl(e.lane) + RH / 2;
    const g = svgEl("g", { class: "jr-mark " + (e.kind === "auth_fail" ? "mk-fail" : "mk-dis") });
    g.appendChild(svgEl("text", { x: xx, y: yy + 5 }, e.kind === "auth_fail" ? "!" : "✕"));
    g.appendChild(svgEl("title", {}, `${new Date(e.at).toLocaleTimeString(LOCALE)} ${e.kind === "auth_fail" ? __("chyba přihlášení") : __("odpojení")}` +
      `${e.reason ? ": " + e.reason : ""}${e.signal != null ? __`\nsignál ${e.signal} dBm` : ""}`));
    svg.appendChild(g);
  }
  // průběh signálu (vzorky + události se signálem)
  const sy0 = top + lanes.length * RH + 24;
  const sy = (s) => sy0 + ((-30 - s) / 60) * sigH;   // −30 nahoře, −90 dole
  svg.appendChild(svgEl("text", { x: 8, y: sy0 + 10, class: "jr-lane" }, __("Signál")));
  for (const v of [-50, -75]) {
    svg.appendChild(svgEl("line", { x1: LW, x2: W - 10, y1: sy(v), y2: sy(v), class: v === -75 ? "jr-thr" : "jr-grid" }));
    svg.appendChild(svgEl("text", { x: LW - 6, y: sy(v) + 4, class: "jr-ax end" }, `${v}`));
  }
  const pts = [...j.samples.map((s) => ({ at: s.at, s: s.signal })), ...j.events.map((e) => ({ at: e.at, s: e.signal }))]
    .filter((p) => p.s != null).sort((a, b) => new Date(a.at) - new Date(b.at));
  if (pts.length > 1) svg.appendChild(svgEl("polyline", { points: pts.map((p) => `${x(p.at).toFixed(1)},${sy(Math.max(-90, Math.min(-30, p.s))).toFixed(1)}`).join(" "), class: "jr-sig" }));
  for (const p of pts) {
    const c = svgEl("circle", { cx: x(p.at), cy: sy(Math.max(-90, Math.min(-30, p.s))), r: 2.5, class: "jr-pt " + sigCls(p.s) });
    c.appendChild(svgEl("title", {}, `${new Date(p.at).toLocaleTimeString(LOCALE)} ${p.s} dBm`));
    svg.appendChild(c);
  }
  if (!lanes.length) svg.appendChild(svgEl("text", { x: 20, y: 40, class: "jr-lane" }, __("V tomto období klient na Wi-Fi nebyl.")));
}

// --- přehled zařízení: volitelné sloupce, třídění, filtr ---------------------------------------------
const pctCell = (v, warn = 80, bad = 90) => {
  const td = el("td", null, "num");
  if (v == null) { td.textContent = "–"; td.className += " muted"; return td; }
  const bar = el("span", null, "pbar " + (v >= bad ? "bad" : v >= warn ? "warn" : "ok"));
  const i = el("i"); i.style.width = Math.min(100, v) + "%"; bar.appendChild(i);
  td.append(el("span", `${v} %`), bar);
  return td;
};
// klíč, popisek, výchozí viditelnost, hodnota pro třídění/filtr, vykreslení buňky
const DEV_COLS = [
  ["state", __("Stav"), true, (d) => d.state, (d) => el("td", STATE_NAME[d.state] || d.state, "state " + d.state)],
  ["name", __("Zařízení"), true, (d) => (d.identity || d.serial || "").toLowerCase(), devNameCell],
  ["note", __("Poznámka"), true, (d) => (d.note || "").toLowerCase(), devNoteCell],
  ["model", __("Model"), true, (d) => d.model || "", (d) => el("td", d.model || "–")],
  ["role", __("Role"), false, (d) => d.role || "", (d) => el("td", (d.role || "–") + (d.managed ? __(" · řízené") : ""), d.managed ? "" : "muted")],
  ["ros", "RouterOS", true, (d) => d.ros_version || "", devVersionCell],
  ["site", __("Lokalita"), true, (d) => d.site || "", (d) => el("td", d.site || "–")],
  ["uptime", __("Uptime"), true, (d) => d.uptime_s ?? -1, (d) => el("td", fmtUptime(d.uptime_s), d.uptime_s != null && d.uptime_s < 3600 ? "warn" : "")],
  ["cpu", "CPU", true, (d) => d.cpu ?? -1, (d) => pctCell(d.cpu)],
  ["mem", "RAM", true, (d) => d.mem_pct ?? -1, (d) => pctCell(d.mem_pct)],
  ["uplink", __("Uplink"), true, (d) => rateNum((uplinksCache[d.id] || {}).rate) || -1, devUplinkCell],
  ["parent", __("Připojeno k"), true, (d) => ((uplinksCache[d.id] || {}).parent_label || "").toLowerCase(), devParentCell],
  ["clients", __("Klienti"), true, (d) => Number(d.clients) || 0, (d) => el("td", d.clients ? String(d.clients) : "–", "num")],
  ["alerts", __("Alerty"), false, (d) => Number(d.alerts) || 0, (d) => el("td", d.alerts ? `⚠ ${d.alerts}` : "–", d.alerts ? "num warn" : "num muted")],
  ["security", __("Bezpečnost"), false, (d) => Number(d.security_issues) || 0,
    (d) => el("td", d.security_issues ? String(d.security_issues) : "✓", d.security_issues ? "num warn" : "num ok")],
  ["arch", __("Architektura"), false, (d) => d.arch || "", (d) => el("td", d.arch || "–", "muted")],
  ["tunnel", __("IP správy"), false, (d) => ipSortKey(d.tunnel_ip), (d) => el("td", d.tunnel_ip || "–", "mono")],
  ["sstp", "SSTP", false, (d) => (d.last_status && d.last_status.sstp) || "", (d) => el("td", (d.last_status && d.last_status.sstp) || "–")],
  ["checkin", __("Check-in"), true, (d) => (d.last_checkin_at ? new Date(d.last_checkin_at).getTime() : 0), (d) => el("td", fmtTime(d.last_checkin_at))],
  ["adopted", __("Adoptováno"), false, (d) => (d.adopted_at ? new Date(d.adopted_at).getTime() : 0), (d) => el("td", d.adopted_at ? new Date(d.adopted_at).toLocaleDateString(LOCALE) : "–")],
];
const DEV_STATES = [["all", __("Vše")], ["adopted", __("Online")], ["offline", __("Offline")], ["pending", __("Čekající")], ["problem", __("S problémem")]];
const devView = Object.assign({ cols: null, sort: "name", dir: 1, state: "all", f: {} }, prefs.devView || {});
let uplinksCache = {};
// --- skupiny filtrů v levém panelu (jako UniFi): zaškrtávátka s počty, skupinu jde sbalit ------------------
// defs: [klíč, popisek, fn(položka) → hodnota | pole hodnot | null, jméno hodnoty?, vždy ukázat?]
// Počty u skupiny respektují ostatní skupiny (a `pre`), takže ukazují, kolik řádků výběr opravdu nechá.
const facetVals = (fn, item) => { const v = fn(item); return v === null || v === undefined || v === "" ? [] : Array.isArray(v) ? v : [v]; };
function facetOk(defs, sel, item, skip) {
  for (const [k, , fn] of defs) {
    const want = (sel || {})[k];
    if (k !== skip && want && want.length && !facetVals(fn, item).some((v) => want.includes(v))) return false;
  }
  return true;
}
function facetGroups(box, scope, items, defs, sel, onChange, pre, limit) {
  box.textContent = "";
  prefs.facetClosed = prefs.facetClosed || {};
  for (const [k, label, fn, nameOf, always] of defs) {
    const counts = new Map();
    for (const it of items) if ((!pre || pre(it)) && facetOk(defs, sel, it, k)) for (const v of facetVals(fn, it)) counts.set(v, (counts.get(v) || 0) + 1);
    const cur = sel[k] || [];
    for (const v of cur) if (!counts.has(v)) counts.set(v, 0);
    if (!counts.size || (counts.size < 2 && !cur.length && !always)) continue;
    const id = scope + "." + k, sec = el("div", null, "facet" + (prefs.facetClosed[id] ? " closed" : ""));
    const h = el("button", null, "facet-h"); h.type = "button"; h.setAttribute("aria-expanded", prefs.facetClosed[id] ? "false" : "true");
    h.append(el("span", label), el("span", "", "facet-chev"));
    h.addEventListener("click", () => { prefs.facetClosed[id] = !prefs.facetClosed[id]; savePrefs(); onChange(); });
    sec.appendChild(h);
    const rows = [...counts].sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0]), LOCALE, { numeric: true }));
    const open = (prefs.facetMore || {})[id], max = limit || 8;
    for (const [v, n] of open ? rows : rows.filter((r, i) => i < max || cur.includes(r[0]))) {
      const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.checked = cur.includes(v);
      cb.addEventListener("change", () => {
        const now = new Set(sel[k] || []);
        if (cb.checked) now.add(v); else now.delete(v);
        sel[k] = [...now]; onChange();
      });
      l.append(cb, el("span", `${nameOf ? nameOf(v) : v} (${n})`));
      sec.appendChild(l);
    }
    if (rows.length > max) {
      const more = el("button", open ? __("méně") : __`dalších ${rows.length - max}…`, "ux-link facet-more"); more.type = "button";
      more.addEventListener("click", () => { prefs.facetMore = prefs.facetMore || {}; prefs.facetMore[id] = !open; savePrefs(); onChange(); });
      sec.appendChild(more);
    }
    box.appendChild(sec);
  }
  box.hidden = !box.children.length;
}
const facetAny = (sel) => Object.values(sel || {}).some((v) => v && v.length);

// filtry seznamu zařízení
const ROLE_NAME = { gateway: __("Brána"), switch: "Switch", ap: "Access point", ptp: __("Spoj PtP"), unknown: __("bez role") };
const STATE_NAME = { adopted: "online", offline: "offline", pending: __("čeká na adopci"), provisioning: __("nastavuje se"), removed: __("odebráno") };
const TODO_NAME = { alert: __("Upozornění"), update: __("Dostupná aktualizace"), security: __("Bezpečnostní nález") };
const DEV_FACETS = [
  ["role", __("Typ zařízení"), (d) => d.role || "unknown", (v) => ROLE_NAME[v] || v, true],
  ["manage", __("Správa"), (d) => d.state === "pending" ? null : d.managed_at ? "full" : "watch", (v) => v === "full" ? __("Plná správa") : __("Jen sledování")],
  ["todo", __("K řešení"), (d) => [Number(d.alerts) > 0 && "alert", d.update_available && "update", Number(d.security_issues) > 0 && "security"].filter(Boolean),
    (v) => TODO_NAME[v] || v],
  ["ros", "RouterOS", (d) => d.ros_version ? String(d.ros_version).split(" ")[0] : null],
  ["model", __("Model"), (d) => d.model || __("neznámý model")],
  ["site", __("Lokalita"), (d) => d.site || __("bez lokality")]];
const devFacetOk = (d, skip) => facetOk(DEV_FACETS, devView.f, d, skip);
function renderDevFacets() {
  const box = $("dev-facets"); if (!box) return;
  devView.f = devView.f || {};
  facetGroups(box, "dev", siteDevices(), DEV_FACETS, devView.f, () => { saveDevView(); renderDevices(); }, (d) => devMatchesState(d, devView.state));
  $("dev-clear").disabled = !facetAny(devView.f) && devView.state === "all" && !$("dev-q").value;
  $("dev-topo").checked = !!devView.topo;
}
// řazení podle topologie: rodič, pod ním jeho potomci (odsazení podle hloubky); mimo strom na konec
function devTopoOrder(list) {
  const ids = new Set(list.map((d) => d.id)), kids = new Map(), depth = new Map(), out = [];
  const name = (d) => String(d.identity || d.serial || "").toLowerCase();
  for (const d of list) {
    const par = (uplinksCache[d.id] || {}).parent_id;
    const key = par && ids.has(par) && par !== d.id ? par : "";
    if (!kids.has(key)) kids.set(key, []);
    kids.get(key).push(d);
  }
  const walk = (key, lvl) => {
    for (const d of (kids.get(key) || []).sort((a, b) => name(a).localeCompare(name(b), LOCALE, { numeric: true }))) {
      if (depth.has(d.id)) continue;
      depth.set(d.id, lvl); out.push(d); walk(d.id, lvl + 1);
    }
  };
  walk("", 0);
  for (const d of list) if (!depth.has(d.id)) { depth.set(d.id, 0); out.push(d); }    // kruh v datech: nic neztratit
  return [out, depth];
}
function devUplinkCell(d) {
  const u = uplinksCache[d.id];
  if (!u || (!u.rate && !u.uplink_port)) return el("td", u && u.wan ? "WAN" : "–", "muted");
  const td = el("td", u.rate ? u.rate.replace("bps", "") : "", rateNum(u.rate) >= 1000 ? "gbe" : u.rate ? "fe" : "");
  if (u.uplink_port) td.appendChild(el("span", ` ${u.uplink_port}`, "muted small"));
  return td;
}
function devParentCell(d) {
  const u = uplinksCache[d.id];
  if (!u || !u.parent_label) return el("td", u && u.wan ? __("internet (WAN)") : "–", "muted");
  const td = el("td");
  const a = el("a", u.parent_label + (u.parent_port ? ` · port ${portNum(u.parent_port)}` : ""));
  a.href = "#"; a.title = u.parent_port ? `${u.parent_label}, ${u.parent_port}` : u.parent_label;
  a.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation && ev.stopPropagation();
    const par = devicesCache.find((x) => x.id === u.parent_id); if (par) openQuick(par); });
  td.appendChild(a);
  return td;
}
const visibleCols = () => DEV_COLS.filter(([k, , def]) => (devView.cols ? devView.cols.includes(k) : def));
function saveDevView() { prefs.devView = devView; savePrefs(); }

function ipSortKey(ip) { return ip ? ip.split(".").map((x) => x.padStart(3, "0")).join(".") : ""; }
function fmtUptime(s) {
  if (s == null) return "–";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}
function devNameCell(d) {
  const who = el("td", null, "dev-who");
  who.appendChild(el("b", d.identity || d.serial));
  if (d.identity_pending && d.identity_pending !== d.identity) {
    const pend = el("span", ` → ${d.identity_pending}`, "id-pending"); pend.title = __("nové jméno čeká na potvrzení zařízením");
    who.appendChild(pend);
  }
  if (d.identity) who.appendChild(el("div", d.serial, "muted small mono"));
  if (flowActive(d)) who.appendChild(flowChip(d));
  if (d.state !== "pending" && d.state !== "removed") {
    who.title = __("Dvojklik = přejmenovat");
    who.addEventListener("dblclick", (ev) => { ev.stopPropagation && ev.stopPropagation(); renameDevice(d); });
  }
  return who;
}
function devNoteCell(d) {
  const nt = el("td", d.note || "", "dev-note" + (d.note ? "" : " muted"));
  if (d.note) nt.title = d.note;
  if (d.state !== "pending" && d.state !== "removed") {
    nt.title = (d.note ? d.note + "\n\n" : "") + __("Dvojklik = upravit poznámku");
    nt.addEventListener("dblclick", (ev) => { ev.stopPropagation && ev.stopPropagation(); editNote(d); });
  }
  return nt;
}
function devVersionCell(d) {
  const td = el("td", d.ros_version);
  if (d.update_available) {
    const u = el("span", ` ↑ ${d.latest_version}`, "warn");
    u.title = __`Dostupná aktualizace v kanálu ${d.update_channel || "?"}`;
    td.appendChild(u);
  }
  if (d.warnings && d.warnings.length) {
    const w = el("span", d.warnings.some((x) => x.severity === "critical") ? " ⚠" : " ⓘ", "warn");
    w.title = d.warnings.map((x) => x.text).join("\n");
    td.appendChild(w);
  }
  return td;
}

async function loadDevices() {
  const list = await api("GET", `/tenants/${tenantId}/devices`);
  devicesCache = list; devicesLoaded = true;
  setBadge("devices", list.filter((d) => d.state === "pending").length, "warn");
  renderDevices();
  if (!$("view-devices").hidden && $("add-dlg").hidden) loadFound();
  // uplinky (strom lokalit) dopočítat na pozadí – seznam se ukáže hned
  api("GET", `/tenants/${tenantId}/uplinks`).then((u) => { uplinksCache = u || {}; renderDevices(); }).catch(() => {});
  if (ovDrill) renderOverview();  // otevřený proklik z přehledu ukazuje aktuální stav
}

function renderDevices() {
  const cols = visibleCols();
  // hlavička s tříděním
  const head = $("devices-head"); head.textContent = "";
  for (const [k, label] of cols) {
    const th = el("th", null, "sortable" + (devView.sort === k ? " sorted" : ""));
    th.append(document.createTextNode(label), el("span", devView.sort === k ? (devView.dir > 0 ? " ▲" : " ▼") : "", "sort-ind"));
    th.title = __("Seřadit");
    th.addEventListener("click", () => { devView.dir = devView.sort === k ? -devView.dir : 1; devView.sort = k; saveDevView(); renderDevices(); });
    head.appendChild(th);
  }
  head.appendChild(el("th", ""));
  segmented($("dev-state"), DEV_STATES.map(([k, l]) => [k, `${l}${k === "all" ? "" : " " + devCount(k)}`]), devView.state,
            (st) => { devView.state = st; saveDevView(); renderDevices(); });
  // filtr a třídění
  const q = ($("dev-q").value || "").trim().toLowerCase();
  const col = DEV_COLS.find((c) => c[0] === devView.sort) || DEV_COLS[1];
  renderDevFacets();
  const base = siteDevices();
  const list = base.filter((d) => devMatchesState(d, devView.state) && devFacetOk(d) && (!q ||
      [d.identity, d.serial, d.note, d.model, d.site, d.tunnel_ip, d.ros_version, d.role, d.identity_pending]
        .some((v) => v && String(v).toLowerCase().includes(q))))
    .sort((a, b) => { const x = col[3](a), y = col[3](b); return (x < y ? -1 : x > y ? 1 : 0) * devView.dir; });
  let depth = null;
  if (devView.topo) { const [ordered, dep] = devTopoOrder(list); list.length = 0; list.push(...ordered); depth = dep; }
  $("dev-count").textContent = list.length === base.length ? __`${list.length} zařízení` : `${list.length} z ${base.length}`;
  const tb = $("devices"); tb.textContent = "";
  for (const d of list) {
    const tr = el("tr");
    if (d.state !== "pending") {
      tr.className = "clickable";
      // klik = panel zařízení vpravo (jako UniFi)
      tr.addEventListener("click", (ev) => { if (!["BUTTON", "SELECT", "INPUT", "A"].includes(ev.target.tagName)) openDetail(d.id); });
      if (detailId === d.id && !$("detail").hidden) tr.className += " selected";
      tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && ev.target === tr) openDetail(d.id); });
    }
    if (depth && depth.get(d.id)) tr.setAttribute("data-depth", String(Math.min(12, depth.get(d.id))));
    withCtx(tr, d.identity || d.serial, () => deviceMenu(d));
    for (const c of cols) tr.appendChild(c[4](d));
    // akce: schválení u čekajících, jinak aktualizace a ⋯ (vše ostatní v menu – pravé tlačítko)
    const act = el("td", null, "dev-act");
    if (d.state === "pending") act.append(approveControls(d), " ", forgetButton(d));
    else {
      if (d.update_available && d.state === "adopted") {
        const b = el("button", `↑ ${d.latest_version}`, "small"); b.title = __`Aktualizovat na ${d.latest_version}`;
        b.onclick = () => upgradeDevice(d);
        act.append(b, " ");
      }
      // rychlé akce při najetí na řádek (jako Locate / Restart v UniFi)
      const hov = el("span", null, "row-hover");
      if (d.state === "adopted" && canOperate()) {
        const rb = el("button", __("Restart"), "small secondary"); rb.type = "button"; rb.onclick = () => sendCommand(d, "reboot", __("Restart"));
        hov.appendChild(rb);
      }
      act.append(hov, moreButton(d.identity || d.serial, () => deviceMenu(d)));
    }
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  if (!list.length) {
    const tr = el("tr"); const td = el("td", base.length ? __("Filtru nic neodpovídá") : __("Žádná zařízení v lokalitě"), "muted"); td.colSpan = cols.length + 1;
    tr.appendChild(td); tb.appendChild(tr);
  }
}
// --- přehled zařízení (záložka Přehled v panelu zařízení): porty, připojení, stav ------------
// klik na zařízení kdekoli (topologie, mapa, rodič) otevře panel zařízení
function openQuick(d) { openDetail(d.id); }

function renderQuickInfo(body, d, r) {
  body.textContent = "";
  // porty: stejný přehled jako na záložce Porty, klik otevře port
  const phys = (r.interfaces || []).filter((i) => i.type === "ether" && PORT_RE.test(i.name));
  if (phys.length) {
    const cu = phys.filter((i) => !/^(sfp|qsfp)/.test(i.name)).sort((a, b) => portNum(a.name) - portNum(b.name));
    const sfp = phys.filter((i) => /^(sfp|qsfp)/.test(i.name)).sort((a, b) => portNum(a.name) - portNum(b.name));
    const strip = el("div", null, "dq-ports");
    const tile = (i) => {
      const tl = portTile(i, "link"); tl.type = "button";
      tl.appendChild(el("span", String(portNum(i.name)), "num"));
      tl.title = portTooltip(i);
      tl.addEventListener("click", () => { openDetailTab("ports"); selectPortTraffic(i.name); });
      return tl;
    };
    for (const g of [cu, sfp]) if (g.length) { const row = el("div", null, "dq-prow"); for (const i of g) row.appendChild(tile(i)); strip.appendChild(row); }
    body.appendChild(strip);
    const bad = phys.filter((i) => portLevel(i));
    const w = phys.reduce((a, i) => a + (Number((i.port || {}).poe_power) || 0), 0);
    body.appendChild(el("p", [__`${phys.filter((i) => i.running).length}/${phys.length} portů s linkou`, w ? `PoE ${w.toFixed(1)} W` : "",
      bad.length ? __`⚠ ${bad.length} ${bad.length === 1 ? __("port potřebuje") : __("porty potřebují")} pozornost` : ""].filter(Boolean).join(" · "),
      bad.length ? "small sev-warning" : "small muted"));
  }
  const acts = el("div", null, "dq-acts");
  if (d.state === "adopted" && canOperate()) {
    const rb = el("button", __("Restart"), "small secondary"); rb.type = "button"; rb.onclick = () => sendCommand(d, "reboot", __("Restart"));
    const tl = el("button", __("Nástroje (ping, test rychlosti)"), "small secondary"); tl.type = "button"; tl.onclick = () => openDetailTab("tools");
    acts.append(rb, tl);
  }
  // sledované zařízení: převzetí do řízení stejným řetězcem jako při adopci
  if (d.state === "adopted" && !(r.device && r.device.managed_at) && !flowActive(d) && canManage()
      && d.role !== "gateway" && suggestMode(d, true) !== "monitor") {
    if (hasWifi(r)) acts.append(actBtn(__("Převzít do řízení (AP + switch)"), () => startManage(d, "ap")));
    acts.append(actBtn(hasWifi(r) ? __("…jen jako switch") : __("Převzít do řízení"), () => startManage(d, "switch")));
  }
  acts.appendChild(moreButton(d.identity || d.serial, () => deviceMenu(d)));
  body.appendChild(acts);
  const grid = el("div", null, "dq-grid");
  body.appendChild(grid);
  const left = el("div"), right = el("div");
  grid.append(left, right);
  const u = uplinksCache[d.id] || {};
  const info = el("dl", null, "dq-info");
  const row = (k, v) => { if (v == null || v === "") return; info.append(el("dt", k)); const dd = el("dd"); if (typeof v === "object") dd.appendChild(v); else dd.textContent = v; info.appendChild(dd); };
  if (u.parent_label) {
    const a = el("a", u.parent_label + (u.parent_port ? ` · port ${portNum(u.parent_port)}` : "")); a.href = "#";
    a.addEventListener("click", (ev) => { ev.preventDefault(); const par = devicesCache.find((x) => x.id === u.parent_id); if (par) openQuick(par); });
    row(__("Připojeno k"), a);
  } else if (u.wan) row(__("Připojeno k"), __("internet (WAN)"));
  if (u.uplink_port) row(__("Přes port"), `${u.uplink_port}${u.rate ? " · " + u.rate : ""} (uplink)`);
  row(isLocal() ? __("IP správy") : __("IP v tunelu"), d.tunnel_ip);
  left.appendChild(info);
  const conn = phys.filter((i) => i.connected).sort((a, b) => portNum(a.name) - portNum(b.name));
  if (conn.length) {
    right.appendChild(el("h4", __("Na portech")));
    const t2 = el("table", null, "dq-conn");
    for (const i of conn) {
      const n = i.connected, tr = el("tr");
      const nm = el("td"); nm.append(devIcon(n.kind, "sm"), el("span", " " + n.name, n.managed ? "managed" : ""));
      tr.append(nm, el("td", n.ip || (n.count > 1 ? `${n.count} MAC` : ""), "muted"), el("td", String(portNum(i.name)), "num"));
      t2.appendChild(tr);
    }
    right.appendChild(t2);
  }
}

if (typeof document.addEventListener === "function") {
  document.addEventListener("keydown", (e) => {
    const m = $("ctx");
    if (e.key !== "Escape" || (m && !m.hidden) || askDone || !$("ask").hidden) return;
    if (!$("port-edit").hidden) return;  // editor portu má vlastní zavírání
    if (!$("cl-dlg").hidden) clClose();
    else if (!$("detail").hidden) closeDetail();
  });
}

function devMatchesState(d, st) {
  if (st === "all") return true;
  if (st === "problem") return d.state === "offline" || Number(d.alerts) > 0 || d.update_available || Number(d.security_issues) > 0 ||
    (d.warnings || []).some((w) => w.severity === "critical");
  return d.state === st;
}
function devCount(st) { return siteDevices().filter((d) => devMatchesState(d, st)).length; }

$("dev-q").addEventListener("input", () => renderDevices());
$("dev-topo").addEventListener("change", () => { devView.topo = $("dev-topo").checked; saveDevView(); renderDevices(); });
$("dev-clear").addEventListener("click", () => { devView.f = {}; devView.state = "all"; $("dev-q").value = ""; saveDevView(); renderDevices(); });
$("dev-cols").addEventListener("click", (ev) => {
  if (ev.stopPropagation) ev.stopPropagation();
  const pop = $("dev-cols-pop");
  if (!pop.hidden) { pop.hidden = true; return; }
  pop.textContent = "";
  const vis = new Set(visibleCols().map((c) => c[0]));
  for (const [k, label] of DEV_COLS) {
    const l = el("label", null, "check"); const cb = el("input"); cb.type = "checkbox"; cb.checked = vis.has(k);
    cb.disabled = k === "name";
    cb.addEventListener("change", () => {
      const cur = new Set(visibleCols().map((c) => c[0]));
      if (cb.checked) cur.add(k); else cur.delete(k);
      devView.cols = DEV_COLS.map((c) => c[0]).filter((x) => cur.has(x));
      saveDevView(); renderDevices();
    });
    l.append(cb, document.createTextNode(" " + label)); pop.appendChild(l);
  }
  const rs = el("button", __("Výchozí sloupce"), "small secondary"); rs.type = "button";
  rs.addEventListener("click", () => { devView.cols = null; saveDevView(); pop.hidden = true; renderDevices(); });
  pop.appendChild(rs);
  pop.hidden = false;
});
if (typeof document.addEventListener === "function") {
  document.addEventListener("click", (e) => { const p = $("dev-cols-pop"); if (p && !p.hidden && !p.contains(e.target) && e.target !== $("dev-cols")) p.hidden = true; });
}

boot();

// --- firewall jako zóny (UniFi zone-based firewall), zatím jen čtení ----------------------------

const FW_VIEWS = [["matrix", __("Matice zón")], ["filter", __("Pravidla filtru")], ["nat", "NAT"]];
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
    box.appendChild(el("p", __("Zatím bez inventury firewallu"), "muted"));
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
  td.appendChild(el("span", c.decision === "allow" ? __("Povoleno") : __("Blokováno"), "fw-dec"));
  if (c.exceptions.length) td.appendChild(el("small", __` +${c.exceptions.length} výj.`));
  td.title = c.note || (c.rule != null ? __`pravidlo #${c.rule}` : "");
  td.addEventListener("click", () => fwShow(fw, src, dst, c));
  return td;
}

function fwMatrix(box, fw) {
  if (!fw.zones.length) { box.appendChild(el("p", __("Žádné zóny (seznamy rozhraní)"), "muted")); return; }
  const t = el("table", "", "fw-matrix");
  const head = el("tr"); head.appendChild(el("th", "z \\ do", "muted"));
  for (const z of fw.zones) head.appendChild(el("th", z));
  head.appendChild(el("th", __("Router")));
  t.appendChild(head);
  for (const src of fw.zones) {
    const tr = el("tr");
    const th = el("th", src);
    th.title = (fw.members[src] || []).join(", ") || __("bez členů");
    tr.appendChild(th);
    for (const dst of fw.zones) tr.appendChild(fwCell(fw.matrix[src][dst], src, dst, fw));
    tr.appendChild(fwCell(fw.router[src], src, null, fw));
    t.appendChild(tr);
  }
  box.appendChild(t);
  const rt = fw.return_traffic || {};
  box.appendChild(el("p", __`Návratový provoz (established/related): forward ${rt.forward ? __("povolen ✓") : __("chybí")}, ` +
    __`vstup do routeru ${rt.input ? __("povolen ✓") : __("chybí")}. Invalid: ${fw.drop_invalid && fw.drop_invalid.forward ? __("zahazuje se ✓") : __("nezahazuje se")}.`, "muted"));
  box.appendChild(el("p", __("Úpravy pravidel budou následovat s automatickým návratem (commit-confirm). Klikněte na buňku pro pravidla."), "muted"));
}

function fwShow(fw, src, dst, c) {
  $("d-fw-detail").hidden = false;
  $("d-fw-detail-h").textContent = `${src} → ${dst || __("Router")}: ${c.decision === "allow" ? __("povoleno") : __("blokováno")}` +
    (c.note ? ` (${c.note})` : "");
  const ol = $("d-fw-detail-list"); ol.textContent = "";
  const byIdx = {};
  for (const r of fw.rules) byIdx[r.index] = r;
  for (const i of c.exceptions) {
    const r = byIdx[i];
    if (r) ol.appendChild(el("li", __`výjimka #${i}: ${r.action} – ${r.summary}${r.comment ? " (" + r.comment + ")" : ""}`));
  }
  const r = c.rule != null ? byIdx[c.rule] : null;
  ol.appendChild(el("li", r ? __`rozhoduje #${r.index}: ${r.action} – ${r.summary}${r.comment ? " (" + r.comment + ")" : ""}`
    : __("rozhoduje výchozí accept RouterOS (žádné pravidlo)"), "fw-" + c.decision));
}

function fwRules(box, rows, nat) {
  if (!rows || !rows.length) { box.appendChild(el("p", __("Žádná pravidla"), "muted")); return; }
  const t = el("table", "", "fw-rules");
  const head = el("tr");
  for (const h of ["#", __("Chain"), __("Akce"), __("Podmínky"), __("Paketů"), __("Komentář")]) head.appendChild(el("th", h));
  t.appendChild(head);
  for (const r of rows) {
    const tr = el("tr", "", r.disabled ? "fw-disabled" : "");
    tr.appendChild(el("td", String(r.index) + (r.dynamic ? " D" : "")));
    tr.appendChild(el("td", r.chain || ""));
    const act = String(r.action || "");
    const cls = /^(drop|reject|tarpit)$/.test(act) ? "block" : /^(accept|fasttrack-connection)$/.test(act) ? "allow" : "other";
    const td = el("td"); td.appendChild(el("span", act, "pill fw-" + cls)); tr.appendChild(td);
    tr.appendChild(el("td", r.summary + (r.invalid ? __(" (neplatné)") : "")));
    tr.appendChild(el("td", fmtCount(r.packets), "num"));
    tr.appendChild(el("td", r.comment || "", "muted"));
    t.appendChild(tr);
  }
  box.appendChild(t);
  if (!nat) box.appendChild(el("p", __("D = dynamické pravidlo (vytvořené RouterOS), přeškrtnuté = vypnuté."), "muted"));
}

// --- historie portů (Port Time Machine) -------------------------------------------------------

const phState = { days: 7, port: "" };
const PH_CLS = { link: (e) => (e.new === "up" ? "ph-up" : "ph-down"), flaps: () => "ph-down", disabled: () => "ph-cfg",
                 neighbor: (e) => (e.new ? "ph-up" : "ph-down"), loop: (e) => (e.new === "ano" ? "ph-down" : "ph-up") };

function phItem(e, withPort) {
  const li = el("li", null, (PH_CLS[e.field] ? PH_CLS[e.field](e) : "ph-cfg"));
  const t = new Date(e.at);
  li.appendChild(el("span", t.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" }), "ph-time"));
  if (withPort) li.appendChild(el("b", e.port + " "));
  li.appendChild(el("span", e.text));
  li.title = t.toLocaleString(LOCALE);
  return li;
}

async function loadPortHistory(id) {
  const q = `days=${phState.days}` + (phState.port ? `&port=${encodeURIComponent(phState.port)}` : "");
  let h;
  try { h = await api("GET", `/tenants/${tenantId}/devices/${id}/port-history?${q}`); } catch (e) { return; }
  if (detailId !== id) return;
  segmented($("d-ph-range"), [[1, "24 h"], [7, __("7 dní")], [30, __("30 dní")], [90, __("90 dní")]], phState.days,
            (v) => { phState.days = v; loadPortHistory(id); });
  const sel = $("d-ph-port");
  if (!phState.port) {
    sel.textContent = "";
    const o0 = el("option", __("všechny porty")); o0.value = ""; sel.appendChild(o0);
    const ports = [...new Set(h.events.map((e) => e.port))].sort((a, b) => portNum(a) - portNum(b));
    for (const p of ports) { const o = el("option", p); o.value = p; sel.appendChild(o); }
    sel.onchange = () => { phState.port = sel.value; loadPortHistory(id); };
  }
  sel.value = phState.port;
  const n = h.events.length;
  $("d-ph-n").textContent = n ? `(${n} ${n === 1 ? __("změna") : n < 5 ? __("změny") : __("změn")})` : "";
  const box = $("d-ph"); box.textContent = "";
  if (!h.events.length) { box.appendChild(el("p", __("Za zvolené období beze změn (historie se plní s každou inventurou)."), "muted")); return; }
  let day = null, ul = null;
  for (const e of h.events) {
    const dd = new Date(e.at).toLocaleDateString(LOCALE, { weekday: "short", day: "numeric", month: "numeric" });
    if (dd !== day) { day = dd; box.appendChild(el("h4", dd, "ph-day")); ul = el("ul", null, "ph-list"); box.appendChild(ul); }
    ul.appendChild(phItem(e, !phState.port));
  }
}



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
  $("intent-state").textContent = cur ? __`– ${cur.networks.length} sítí` : __("– zatím nenastaven");
  renderIntent(cur ? cur.access_vlan : null);
  const ul = $("intent-devs"); ul.textContent = "";
  for (const d of intentData.devices) {
    const li = el("li", `${d.name} · ${d.managed ? __("řízené") : __("sledované")}`, d.managed ? "managed" : "");
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
const CHIP_LABEL = { ax: "ax", ac: "ac", legacy: __("starý") };
const sitePath = () => `/tenants/${tenantId}/sites/${siteId()}`;

function wifiPolicy() {
  const cur = intentData.intent;
  return (cur && cur.wifi) || { capsman_device_id: null, max_width: 80, dfs: true, country: (me && me.defaults && me.defaults.country) || "Czech Republic", groups: [] };
}
const netName = (vid) => { const n = intentNets.find((x) => String(x.vlan_id) === String(vid)); return n ? n.name : `VLAN ${vid}`; };
const groupName = (gid) => { const g = wifiGroups.find((x) => x.id === gid); return g ? g.name : "?"; };
const siteAps = () => intentData.devices.filter((d) => d.role === "ap" || (d.wifi && d.role !== "gateway"));

function renderIntentWifi() {
  const w = wifiPolicy();
  wifiGroups = (w.groups || []).map((g) => ({ ...g }));
  const host = $("wifi-host"); host.textContent = "";
  const none = el("option", __("— nevybráno —")); none.value = ""; host.appendChild(none);
  for (const d of intentData.devices.filter((x) => x.managed)) {
    const o = el("option", `${d.name}${d.model ? " (" + d.model + ")" : ""}`); o.value = d.id; host.appendChild(o);
  }
  if (!intentData.devices.some((x) => x.managed)) none.textContent = __("— nejdřív převezmi zařízení do řízení —");
  host.value = w.capsman_device_id || "";
  $("wifi-width").value = String(w.max_width || 80);
  $("wifi-dfs").checked = w.dfs !== false;
  $("wifi-country").value = w.country || (me && me.defaults && me.defaults.country) || "Czech Republic";
  $("wifi-tx").value = w.tx_power || "balanced";
  $("wifi-steer").checked = w.steering !== false;
  $("wifi-rogue").checked = w.rogue_scan === "night";
  $("wifi-result").textContent = "";
  renderSsidCards();
  renderApBoard();
  $("apg-add").disabled = !canManage();
  $("wifi-deploy").disabled = !canManage() || !w.capsman_device_id || !(intentData.ssids || []).length;
  $("wifi-deploy").title = !w.capsman_device_id ? __("nejdřív vyber a ulož zařízení s CAPsMANem") : "";
}

// kde se SSID vysílá – text pro kartu
function ssidWhere(x) {
  return (x.groups || []).length ? x.groups.map(groupName).join(", ") : __("všechna AP");
}

function ssidMenu(x) {
  const can = canManage();
  return [
    { label: __("Upravit…"), act: () => openSsid(x), key: "Enter" },
    { label: x.enabled ? __("Vypnout") : __("Zapnout"), disabled: !can, act: () => patchSsid(x, { enabled: !x.enabled }) },
    { label: __("Roaming (FT)"), checked: x.ft !== false, disabled: !can, act: () => patchSsid(x, { ft: x.ft === false }) },
    { label: __("Izolace klientů"), checked: !!x.isolation, disabled: !can, act: () => patchSsid(x, { isolation: !x.isolation }) },
    { sep: true },
    { head: __("Vysílat na") },
    { label: __("všech AP"), checked: !(x.groups || []).length, disabled: !can, act: () => patchSsid(x, { groups: [] }) },
    ...wifiGroups.map((g) => ({ label: g.name, checked: (x.groups || []).includes(g.id), disabled: !can,
                                act: () => patchSsid(x, { groups: toggled(x.groups || [], g.id) }) })),
    { sep: true },
    (x.ppsk || []).length ? { label: __("Zobrazit hesla více hesel"), disabled: !can, act: () => revealPpsk(x) } : null,
    { label: __("Duplikovat…"), disabled: !can, act: () => openSsid({ ...x, id: null, ssid: x.ssid + "-2", ppsk: [] }) },
    { label: __("Smazat"), danger: true, disabled: !can, act: () => deleteSsid(x) },
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
    if (x.isolation) meta.appendChild(el("span", __("izolace"), "wpill"));
    if (x.enabled === false) meta.appendChild(el("span", __("vypnuto"), "wpill bad"));
    card.appendChild(meta);
    if ((x.ppsk || []).length) {
      const pp = el("div", null, "sc-ppsk");
      pp.append(el("span", __`${x.ppsk.length}× heslo`, "badge-ax"), el("span", " → " + [...new Set(x.ppsk.map((e) => netName(e.vlan_id)))].join(", "), "muted"));
      pp.title = __("Více hesel s VLAN – vysílá se jen na cAP ax");
      card.appendChild(pp);
      // AP s 802.11ac tam, kde by se SSID mělo vysílat: tam ho nebude
      const ac = siteAps().filter((d) => d.chip !== "ax" && (!(x.groups || []).length || x.groups.includes(d.ap_group)));
      if (ac.length) {
        const wn = el("div", __`⚠ na ${ac.length} AP bez ax se nevysílá`, "sc-warn");
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
  add.append(el("span", "+", "sc-plus"), el("span", __("Nová Wi-Fi síť")));
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
    msg(__`Wi-Fi ${x.ssid} upravena`); showApply(r.apply); loadIntent();
  } catch (e) { msg(e.message, true); }
}

async function deleteSsid(x) {
  if (!(await ask(__`Smazat Wi-Fi ${x.ssid}? Z AP hned zmizí.`, __("Smazat")))) return false;
  try { const r = await api("DELETE", `${sitePath()}/ssids/${x.id}`); msg(__`Wi-Fi ${x.ssid} smazána`); showApply(r.apply); loadIntent(); return true; }
  catch (e) { msg(e.message, true); return false; }
}

async function revealPpsk(x) {
  try {
    const r = await api("POST", `${sitePath()}/ssids/${x.id}/ppsk/reveal`, {});
    const lines = (r.ppsk || []).map((e) => `${e.name} (${netName(e.vlan_id)}): ${e.psk}`);
    await ask(__`${x.ssid} – hesla (zobrazení je v auditu):\n\n${lines.join("\n")}`, __("Zavřít"));
    return r.ppsk || [];
  } catch (e) { msg(e.message, true); return null; }
}

// --- editor SSID ---
let sd = null;  // {x, bands, groups, ppsk:[{name, vlan_id, psk, saved}], ppskDirty}
function openSsid(x) {
  sd = { x, bands: x.bands || "2.4,5", groups: [...(x.groups || [])], ppskDirty: false,
         ppsk: (x.ppsk || []).map((e) => ({ name: e.name, vlan_id: e.vlan_id, psk: "", saved: true })) };
  $("sd-title").textContent = x.id ? __`Wi-Fi ${x.ssid}` : __("Nová Wi-Fi síť");
  $("sd-ssid").value = x.ssid || "";
  const v = $("sd-vlan"); v.textContent = "";
  for (const n of intentNets) { const o = el("option", `${n.name} (VLAN ${n.vlan_id})`); o.value = String(n.vlan_id); v.appendChild(o); }
  if (x.vlan_id != null) v.value = String(x.vlan_id);
  $("sd-psk").value = ""; $("sd-psk").type = "password";
  $("sd-psk").placeholder = x.id ? __("uloženo (prázdné = beze změny)") : __("8–63 znaků");
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
  const all = el("button", __("všechna AP"), "gchip" + (sd.groups.length ? "" : " on")); all.type = "button";
  all.addEventListener("click", () => { sd.groups = []; sdGroups(); });
  box.appendChild(all);
  for (const g of wifiGroups) {
    const b = el("button", g.name, "gchip" + (sd.groups.includes(g.id) ? " on" : "")); b.type = "button";
    const n = siteAps().filter((d) => d.ap_group === g.id).length;
    b.title = __`${n} AP ve skupině`;
    b.addEventListener("click", () => { sd.groups = toggled(sd.groups, g.id); sdGroups(); });
    box.appendChild(b);
  }
  if (!wifiGroups.length) box.appendChild(el("span", __("skupiny AP založíš na tabuli pod kartami"), "muted small"));
}
function sdPpsk() {
  const tb = $("sd-ppsk"); tb.textContent = "";
  for (const e of sd.ppsk) {
    const tr = el("tr");
    const nm = el("input"); nm.value = e.name; nm.maxLength = 40; nm.placeholder = __("např. Účetní");
    nm.addEventListener("input", () => { e.name = nm.value; sd.ppskDirty = true; });
    const vl = el("select");
    for (const n of intentNets) { const o = el("option", `${n.name} (${n.vlan_id})`); o.value = String(n.vlan_id); vl.appendChild(o); }
    vl.value = String(e.vlan_id); vl.addEventListener("change", () => { e.vlan_id = Number(vl.value); sd.ppskDirty = true; });
    const pk = el("input"); pk.type = "password"; pk.autocomplete = "new-password"; pk.maxLength = 63; pk.value = e.psk;
    pk.placeholder = e.saved ? __("uloženo") : __("8–63 znaků");
    pk.addEventListener("input", () => { e.psk = pk.value; sd.ppskDirty = true; });
    const rm = el("button", "×", "icon-btn"); rm.type = "button"; rm.title = __("Odebrat heslo");
    rm.addEventListener("click", () => { sd.ppsk.splice(sd.ppsk.indexOf(e), 1); sd.ppskDirty = true; sdPpsk(); });
    const td = (...xs) => { const c = el("td"); c.append(...xs); return c; };
    tr.append(td(nm), td(vl), td(pk), td(rm));
    tb.appendChild(tr);
  }
  if (!sd.ppsk.length) {
    const tr = el("tr"); const c = el("td", __("Jedno heslo pro všechny (hlavní heslo a VLAN SSID)."), "muted"); c.colSpan = 4;
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
  msg(__`Načteno ${n} hesel` + (bad.length ? __` – ${bad.length} řádků nejde přečíst (zůstaly v poli)` : ""), !!bad.length);
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
    msg(__`Wi-Fi ${body.ssid} uložena`); showApply(r.apply);
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
    { label: __("Otevřít detail"), act: () => openDetail(d.id) },
    { sep: true },
    { head: ids.length > 1 ? __`Přesunout ${ids.length} AP do` : __("Přesunout do") },
    { label: __("bez skupiny"), checked: ids.length === 1 && !d.ap_group, disabled: !can, act: () => moveAps(ids, null) },
    ...wifiGroups.map((g) => ({ label: g.name, checked: ids.length === 1 && d.ap_group === g.id, disabled: !can,
                                act: () => moveAps(ids, g.id) })),
    { label: __("Nová skupina…"), disabled: !canManage(), act: async () => { const g = await addGroup(); if (g) moveAps(ids, g.id); } },
    { sep: true },
    { label: apSel.has(d.id) ? __("Zrušit výběr") : __("Vybrat"), act: () => { apSel.has(d.id) ? apSel.delete(d.id) : apSel.add(d.id); renderApBoard(); } },
  ];
}
function groupMenu(g) {
  const can = canManage();
  const members = siteAps().filter((d) => (d.ap_group || null) === (g ? g.id : null));
  return [
    { label: __("Vybrat všechna AP"), disabled: !members.length, act: () => { for (const d of members) apSel.add(d.id); renderApBoard(); } },
    g ? { label: __("Přejmenovat…"), disabled: !can, act: () => renameGroup(g) } : null,
    g ? { label: __("Nová Wi-Fi jen pro tuto skupinu…"), disabled: !can,
          act: () => openSsid({ vlan_id: intentData.intent ? intentData.intent.access_vlan : null, groups: [g.id] }) } : null,
    g ? { sep: true } : null,
    g ? { label: __("Smazat skupinu"), danger: true, disabled: !can, act: () => deleteGroup(g) } : null,
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
    head.append(el("b", g ? g.name : __("Bez skupiny")), el("span", String(members.length), "apg-n"), moreButton(g ? g.name : __("Bez skupiny"), () => groupMenu(g)));
    if (g) head.addEventListener("dblclick", () => renameGroup(g));
    col.appendChild(head);
    // SSID, která se v této skupině vysílají
    const ss = el("div", null, "apg-ssids");
    const here = (intentData.ssids || []).filter((x) => x.enabled !== false && (g ? !(x.groups || []).length || x.groups.includes(g.id) : !(x.groups || []).length));
    for (const x of here) {
      const p = el("span", x.ssid, "wpill" + ((x.groups || []).length ? " vlan" : ""));
      p.title = (x.groups || []).length ? __("jen ve vybraných skupinách") : __("na všech AP");
      p.addEventListener("click", () => openSsid(x));
      withCtx(p, x.ssid, () => ssidMenu(x));
      ss.appendChild(p);
    }
    if (!here.length) ss.appendChild(el("span", __("žádné SSID"), "muted small"));
    col.appendChild(ss);
    const list = el("div", null, "apg-list");
    for (const d of members) list.appendChild(apChip(d));
    if (!members.length) list.appendChild(el("div", g ? __("přetáhni sem AP") : "—", "apg-empty"));
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
    withCtx(col, g ? g.name : __("Bez skupiny"), () => groupMenu(g));
    board.appendChild(col);
  }
  if (!aps.length) board.appendChild(el("p", __("Na lokalitě zatím není žádné AP s Wi-Fi."), "muted"));
}

function apChip(d) {
  const c = el("div", null, "ap-chip" + (apSel.has(d.id) ? " sel" : "") + (d.state === "offline" ? " offline" : ""));
  c.draggable = canOperate();
  c.append(el("span", "", "ap-dot"), el("span", d.name, "ap-name"));
  if (d.chip) {
    const b = el("span", CHIP_LABEL[d.chip] || d.chip, "ap-std " + d.chip);
    b.title = d.chip === "ax" ? __("802.11ax (wifi-qcom) – umí i více hesel s VLAN")
      : d.chip === "ac" ? __("802.11ac (wifi-qcom-ac) – SSID s více hesly se tu nevysílá") : __("starý ovladač wireless – CAPsMAN v7 nepodporuje");
    c.appendChild(b);
  }
  c.title = `${d.model || ""}${d.managed ? "" : __(" · zatím neřízené")}`.trim();
  c.addEventListener("dragstart", (ev) => { if (ev.dataTransfer) { ev.dataTransfer.setData("text/plain", d.id); ev.dataTransfer.effectAllowed = "move"; } });
  c.addEventListener("click", (ev) => {
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { apSel.has(d.id) ? apSel.delete(d.id) : apSel.add(d.id); renderApBoard(); }
    else openDetail(d.id);
  });
  withCtx(c, () => (apSel.has(d.id) && apSel.size > 1 ? __`${apSel.size} vybraných AP` : d.name), () => apMenu(d));
  return c;
}

async function moveAps(ids, group) {
  try {
    const r = await api("POST", `${sitePath()}/ap-groups`, { devices: ids, group });
    msg(`${r.moved || ids.length} AP → ${group ? groupName(group) : __("bez skupiny")}`); showApply(r.apply);
    apSel.clear(); loadIntent();
  } catch (e) { msg(e.message, true); }
}

// skupiny jsou součást záměru: uloží se celé nastavení Wi-Fi
async function saveGroups(groups, done) {
  const w = wifiPolicy();
  const body = { ...intentData.intent, wifi: { ...w, groups } };
  try { const r = await api("PUT", `${sitePath()}/intent`, body); msg(done); showApply(r.apply); loadIntent(); return r; }
  catch (e) { msg(e.message, true); return null; }
}
async function addGroup() {
  if (!intentData.intent) { msg(__("Nejdřív ulož záměr lokality (sítě)"), true); return null; }
  const name = await askText(__("Jméno nové skupiny AP (např. Sklad, 1. patro):"), "", __("Založit"));
  if (!name) return null;
  const r = await saveGroups([...wifiGroups, { name }], __`Skupina ${name} založena`);
  return r && r.intent ? (r.intent.wifi.groups || []).find((g) => g.name === name) || null : null;
}
async function renameGroup(g) {
  const name = await askText(__("Nové jméno skupiny:"), g.name);
  if (!name || name === g.name) return;
  saveGroups(wifiGroups.map((x) => (x.id === g.id ? { ...x, name } : x)), __`Skupina přejmenována na ${name}`);
}
async function deleteGroup(g) {
  const n = siteAps().filter((d) => d.ap_group === g.id).length;
  const s = (intentData.ssids || []).filter((x) => (x.groups || []).includes(g.id)).length;
  if (!(await ask(__`Smazat skupinu ${g.name}?\n\n${n} AP se vrátí mezi AP bez skupiny, ${s} SSID ji přestane uvádět ` +
                  __("(SSID bez jiné skupiny se pak vysílá na všech AP)."), __("Smazat")))) return;
  saveGroups(wifiGroups.filter((x) => x.id !== g.id), __`Skupina ${g.name} smazána`);
}
$("apg-add").addEventListener("click", () => addGroup());

$("ssid-add").addEventListener("click", () => openSsid({ vlan_id: intentData.intent ? intentData.intent.access_vlan : null }));
$("wifi-deploy").addEventListener("click", async () => {
  let w;
  try { w = await api("GET", `${sitePath()}/wifi/preview`); } catch (e) { return msg(e.message, true); }
  const ul = $("wifi-result"); ul.textContent = "";
  for (const b of w.blockers || []) ul.appendChild(el("li", b, "sev-error"));
  for (const r of w.radios || []) {
    const li = el("li", __`${r.device} ${r.interface}: ${r.band} GHz, kanál ${r.channel ?? "?"} / ${r.width} MHz` +
                        __`, výkon ${r.tx_power != null ? r.tx_power + " dBm" : __("max.")} – ${r.ssids.join(", ")}`);
    if (r.tx_reason) li.title = r.tx_reason;
    if (r.group) li.appendChild(el("span", groupName(r.group), "wpill"));
    if (r.chip) li.appendChild(el("span", CHIP_LABEL[r.chip] || r.chip, "ap-std " + r.chip));
    ul.appendChild(li);
  }
  for (const n of w.notes || []) ul.appendChild(el("li", n, "muted"));
  for (const t of w.tables || []) {
    if (t.table.length && t.sha !== t.installed) ul.appendChild(el("li", __`${t.device}: VLAN rádií se na AP doladí samy (${t.table.length} rozhraní)`, "muted"));
  }
  if ((w.blockers || []).length) return;
  if (!(await ask(__`${w.summary}\n\nVytvořit plán pro CAPsMAN? Po schválení se Wi-Fi na AP krátce přenastaví (klienti se znovu připojí).`, __("Vytvořit plán")))) return;
  try {
    const r = await api("POST", `${sitePath()}/wifi/plan`, {});
    msg(__`Plán Wi-Fi #${r.plan_id} čeká na schválení v Aktivitě → Změny.`); loadPlans();
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
    const rm = el("button", "×", "small secondary"); rm.type = "button"; rm.title = __("Odebrat síť");
    rm.addEventListener("click", () => { intentNets.splice(intentNets.indexOf(n), 1); renderIntent($("intent-access").value); });
    const tdr = el("td"); tdr.appendChild(rm);
    tr.append(intentInput(n, "name", "", __("např. Kancelář")), intentInput(n, "vlan_id", "vid", "10"), tdp,
              intentInput(n, "subnet", "", "192.168.10.0/24"), intentInput(n, "gateway", "", "192.168.10.1"),
              intentInput(n, "dns", "", __("volitelné")), tdr);
    tb.appendChild(tr);
  }
  if (!intentNets.length) {
    const tr = el("tr"); const td = el("td", __("Žádné sítě – přidej je nebo je načti ze stávající sítě."), "muted"); td.colSpan = 7;
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
  msg(__("Návrh načten ze sítí na zařízeních – zkontroluj jména a účely a ulož."));
});
$("intent-save").addEventListener("click", () => saveIntent());
$("wifi-save").addEventListener("click", () => saveIntent());
async function saveIntent() {
  const body = {
    networks: intentNets.map((n) => ({ ...n, dns: typeof n.dns === "string" ? n.dns : (n.dns || []).join(",") })),
    access_vlan: $("intent-access").value, mgmt_native: $("intent-native").checked, auto_trunk: $("intent-auto").checked,
    wifi: { capsman_device_id: $("wifi-host").value || null, max_width: Number($("wifi-width").value),
            dfs: $("wifi-dfs").checked, country: $("wifi-country").value.trim(), groups: wifiGroups,
            tx_power: $("wifi-tx").value, steering: $("wifi-steer").checked, rogue_scan: $("wifi-rogue").checked ? "night" : "off" },
  };
  try { const r = await api("PUT", `/tenants/${tenantId}/sites/${siteId()}/intent`, body); msg(__("Sítě lokality uloženy")); showApply(r.apply); loadIntent(); }
  catch (e) { msg(e.message, true); }
}
const SYNC_RESULT = { ok: __("beze změn"), plan: __("plán čeká na schválení"), auto: __("jen přidání VLAN – aplikuje se samo"),
  skipped: __("přeskočeno"), blocked: __("nelze") };
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
  if (!r.devices.length) ul.appendChild(el("li", __("Na lokalitě zatím není žádné řízené zařízení."), "muted"));
  if (r.devices.some((d) => d.result === "plan")) loadPlans();
});

// --- řízený režim: převzetí a výjimky zařízení -------------------------------------------------------

let ctlData = null;
let ctlOver = {};
const CTL_CLASS = { factory: __("tovární / prázdná konfigurace"), simple: __("jednoduchá (bridge bez VLAN)"),
  conforming: __("bridge s VLAN filtrováním"), custom: __("vlastní konfigurace (router, firewall, tunely…)") };
const MODE_LABEL = { trunk: "trunk", access: __("přístup"), off: __("vypnuto") };

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
  b.textContent = c.managed ? __("řízené") : __("sledované"); b.className = "badge " + (c.managed ? "managed" : "observed");
  const intro = c.managed
    ? __`Konfiguraci zařízení nastavuje controller podle záměru lokality (od ${fmtTime(c.managed_at)}). Změny sítí nebo výjimek se projeví sladěním.`
    : __("Převzetí smaže současnou konfiguraci a nasadí ji načisto podle záměru lokality. Předtím se udělá záloha; když se zařízení po převzetí do 12 minut neozve přes tunel, samo se z ní vrátí.");
  $("ctl-intro").textContent = intro;
  listInto($("ctl-blockers"), c.blockers, "sev-error");
  listInto($("ctl-warnings"), c.warnings, "sev-warning");
  const cls = c.classification;
  $("ctl-removed-d").hidden = c.managed || !cls || !cls.removed.length;
  if (cls) {
    listInto($("ctl-removed"), cls.removed);
    $("ctl-removed-n").textContent = __`(${cls.removed.length}) – konfigurace: ${CTL_CLASS[cls.class] || cls.class}`;
  }
  $("ctl-body").hidden = !c.model;
  if (!c.model) return;
  const isGw = !!c.model.gw;
  $("ctl-gw").hidden = !isGw;
  $("ctl-role-row").hidden = isGw;
  $("ctl-mgmt-row").hidden = isGw;
  if (isGw) {
    if (!c.managed) $("ctl-intro").textContent = __("Převzetí smaže současnou konfiguraci routeru a nasadí ji načisto: WAN, sítě lokality s DHCP, NAT a firewall podle zón. Předtím se udělá záloha; když se brána po převzetí do 12 minut neozve controlleru, sama se z ní vrátí.");
    loadGateway(detailId);
  }
  renderCtlPorts();
  const vt = $("ctl-vlans"); vt.textContent = "";
  const name = (p) => { if (p === "bridge") return __("správa zařízení"); const x = c.model.ports.find((q) => q.port === p); return x ? x.name : p; };
  for (const v of c.model.vlans) {
    const tr = el("tr");
    tr.append(el("td", String(v.vid) + (v.vid === c.model.mgmt.vid ? " (mgmt)" : "")), el("td", v.name),
              el("td", v.tagged.map(name).join(", ") || "–", v.tagged.length ? "" : "muted"),
              el("td", v.untagged.map(name).join(", ") || "–", v.untagged.length ? "" : "muted"));
    vt.appendChild(tr);
  }
  const role = $("ctl-role");
  role.textContent = "";
  // role: switch / switch + CAPsMAN (toto zařízení řídí Wi-Fi lokality) / AP (CAP – rádia řídí CAPsMAN)
  for (const [v, l] of [["switch", "switch"], ["capsman", __("switch + CAPsMAN (řídí Wi-Fi lokality)")],
                        ["ap", __("AP + switch (CAP – Wi-Fi z CAPsMANu)")]]) { const o = el("option", l); o.value = v; role.appendChild(o); }
  const curRole = (c.device_intent && c.device_intent.role) || c.model.role || "switch";
  role.value = c.capsman_here && curRole !== "ap" ? "capsman" : curRole;
  role.dataset0 = role.value;
  const apOk = c.wifi === true && !!c.capsman && !c.capsman_here;
  role.children[1].disabled = !c.managed;
  role.children[2].disabled = !apOk && role.value !== "ap";
  role.onchange = async () => {
    if (role.value === "capsman" && role.dataset0 !== "capsman") {
      await setCapsmanHost(detailId, c.site_id);
      loadControl(detailId);
    } else if (role.dataset0 === "capsman" && role.value !== "capsman") {
      await setCapsmanHost(null, c.site_id);
      loadControl(detailId);
    }
  };
  const why = $("ctl-role-why"); why.textContent = "";
  why.textContent = c.wifi === false ? __("rádia jen se starým balíkem wireless – AP nejde řídit")
    : role.value === "capsman" ? __("tady běží CAPsMAN lokality – SSID a nasazení v Nastavení lokality → Wi-Fi")
    : !c.managed ? __("CAPsMAN lze nastavit po převzetí do řízení")
    : c.wifi == null ? __("zařízení nemá Wi-Fi – může být jen CAPsMAN, ne AP")
    : !c.capsman ? __("AP (CAP) jde vybrat, až lokalita má CAPsMAN – nastav ho tady (switch + CAPsMAN) nebo na jiném řízeném zařízení")
    : role.value === "ap" ? __`rádia řídí CAPsMAN na ${c.capsman}` : __`CAPsMAN lokality je na ${c.capsman}`;
  const dm = (c.device_intent && c.device_intent.mgmt) || {};
  $("ctl-mgmt-mode").value = dm.mode || "";
  $("ctl-mgmt-addr").value = dm.address || "";
  $("ctl-mgmt-addr").hidden = dm.mode !== "static";
  const m = c.model.mgmt, path = c.model.path || {};
  $("ctl-mgmt-now").textContent = `→ ${m.mode === "static" ? __("statická ") + m.address : "DHCP"} ve VLAN ${m.vid}${m.native ? __(" (na trunkách bez tagu)") : __(" (tagovaně)")}` +
    (path.address ? __`; dnes ${path.address} přes ${path.l3}` : "");
  const rc = $("ctl-reconcile"); rc.textContent = "";
  if (c.reconcile) {
    if (!c.reconcile.changes.length) rc.appendChild(el("li", __("Zařízení odpovídá záměru."), "muted"));
    for (const x of c.reconcile.changes) rc.appendChild(el("li", __("rozdíl: ") + x));
  }
  const custom = cls && cls.class === "custom";
  $("ctl-custom-l").hidden = !custom || c.managed;
  $("ctl-custom").checked = false;
  const go = $("ctl-go");
  go.hidden = c.managed;
  go.disabled = !canManage() || (c.blockers || []).length > 0 || !!(c.active_plan && c.active_plan.module === "takeover");
  go.title = c.active_plan ? __`plán #${c.active_plan.id} je ve stavu ${c.active_plan.status}` : "";
}

function renderCtlPorts() {
  const c = ctlData;
  const tb = $("ctl-ports"); tb.textContent = "";
  const nets = c.model.vlans;
  for (const p of c.model.ports) {
    const tr = el("tr");
    const sel = el("select"); sel.setAttribute("aria-label", __("Nastavení portu ") + p.name);
    const opts = [["", __("automaticky")], ...nets.map((v) => [`access:${v.vid}`, __`přístup – ${v.name} (${v.vid})`]), ["trunk", __("trunk (všechny sítě)")], ["off", __("vypnuto")]];
    for (const [v, label] of opts) { const o = el("option", label); o.value = v; sel.appendChild(o); }
    const o = ctlOver[p.name];
    sel.value = !o ? "" : o.mode === "access" ? `access:${o.vlan || ""}` : o.mode;
    // bond (LACP): VLAN se nastavuje na bondu v Portech, ne výjimkou portu
    sel.disabled = c.model.uplink === p.port || !!p.bond;
    sel.addEventListener("change", () => {
      if (!sel.value) delete ctlOver[p.name];
      else if (sel.value.startsWith("access:")) ctlOver[p.name] = { mode: "access", vlan: Number(sel.value.slice(7)) };
      else ctlOver[p.name] = { mode: sel.value };
    });
    const tds = el("td"); tds.appendChild(sel);
    const net = nets.find((v) => v.vid === p.pvid);
    const res = p.mode === "access" ? __`přístup – ${net ? net.name : "VLAN " + p.pvid}` : p.mode === "trunk"
      ? (c.model.mgmt.native ? __("trunk, mgmt bez tagu") : __("trunk, vše tagované"))
      : p.mode === "hybrid" ? __`vlastní – ${net ? net.name : "VLAN " + p.pvid} + tagované ${(p.tagged || []).join(", ")}` : __("vypnuto");
    tr.append(el("td", p.bond ? `${p.name} (LACP: ${p.bond.slaves.join(" + ")})` : p.name + (p.name !== p.port ? ` (${p.port})` : "")), tds, el("td", res, "mode-" + p.mode),
              el("td", p.reason + (p.trusted ? " · DHCP trusted" : ""), "muted"));
    tb.appendChild(tr);
  }
}

$("ctl-mgmt-mode").addEventListener("change", () => { $("ctl-mgmt-addr").hidden = $("ctl-mgmt-mode").value !== "static"; });
$("ctl-save").addEventListener("click", async () => {
  const mode = $("ctl-mgmt-mode").value;
  const body = { ports: ctlOver, mgmt: mode ? { mode, address: $("ctl-mgmt-addr").value.trim() } : {},
                 role: $("ctl-role").value === "capsman" ? "switch" : $("ctl-role").value };
  try { const r = await api("PUT", `/tenants/${tenantId}/devices/${detailId}/intent`, body); msg(__("Výjimky uloženy")); showApply(r.apply); loadControl(detailId); }
  catch (e) { msg(e.message, true); }
});
$("ctl-go").addEventListener("click", async () => {
  const c = ctlData;
  const custom = c.classification && c.classification.class === "custom";
  if (custom && !$("ctl-custom").checked) return msg(__("Zařízení má vlastní konfiguraci – nejdřív potvrď, že bude smazána."), true);
  const n = (c.classification ? c.classification.removed.length : 0);
  if (!(await ask(__`Vytvořit plán převzetí? Po schválení se zařízení zazálohuje, resetuje a nastaví podle záměru (výpadek ~2–3 min).` +
                  (n ? __`\n\nZanikne: ${n} položek současné konfigurace.` : ""), __("Vytvořit plán")))) return;
  try {
    const r = await api("POST", `/tenants/${tenantId}/devices/${detailId}/takeover`, { confirm_custom: custom && $("ctl-custom").checked });
    msg(__`Plán převzetí #${r.plan_id} čeká na schválení v Aktivitě → Změny.`);
    loadPlans();
    loadControl(detailId);
  } catch (e) { msg(e.message, true); }
});


// --- řízená brána: WAN, DHCP po sítích, náhled zón -------------------------------------------------------

let gwData = null;
const ZONE_LABEL = { WAN: __("internet"), MGMT: __("správa"), LAN: "LAN", GUEST: __("hosté"), IOT: "IoT", CAM: __("kamery"), VOIP: "VoIP", OTHER: __("jiné") };

async function loadGateway(id) {
  try { gwData = await api("GET", `/tenants/${tenantId}/devices/${id}/gateway`); }
  catch (e) { return msg(e.message, true); }
  if (id !== detailId) return;
  renderGateway();
}

function gwModeFields() {
  const m = $("gw-mode").value;
  $("gw-static").hidden = m !== "static";
  $("gw-pppoe").hidden = m !== "pppoe";
}

function renderGateway() {
  const g = gwData, w = g.wan || {};
  const lock = g.managed && g.configured;   // WAN řízené brány se za provozu zatím nemění
  $("gw-note").textContent = g.other_gateway ? __`Lokalita už má nastavenou bránu ${g.other_gateway}.`
    : lock ? __("WAN řízené brány zatím za provozu změnit nejde (jen při převzetí). DHCP a sítě se mění sladěním.")
    : !g.configured ? __("Zkontroluj, kudy jde internet, a ulož. Bez uložení se použije port dnešní výchozí routy a DHCP.") : "";
  const ps = $("gw-port"); ps.textContent = "";
  for (const p of g.ports) { const o = el("option", p.name + (p.name !== p.port ? ` (${p.port})` : "")); o.value = p.port; ps.appendChild(o); }
  ps.value = w.port || g.wan_guess || (g.ports[0] && g.ports[0].port) || "";
  $("gw-mode").value = w.mode || "dhcp";
  $("gw-vlan").value = w.vlan || "";
  $("gw-addr").value = w.address || ""; $("gw-gate").value = w.gateway || ""; $("gw-dns").value = (w.dns || []).join(", ");
  $("gw-user").value = w.user || ""; $("gw-pass").value = "";
  $("gw-pass").placeholder = w.has_password ? __("uloženo – vyplň jen pro změnu") : "";
  for (const id of ["gw-port", "gw-mode", "gw-vlan", "gw-addr", "gw-gate", "gw-dns", "gw-user", "gw-pass"]) $(id).disabled = lock || !canManage();
  gwModeFields();
  const tb = $("gw-nets"); tb.textContent = "";
  for (const n of g.networks) {
    const tr = el("tr"); tr.dataset.vid = n.vid;
    const pl = n.plan, o = g.dhcp[String(n.vid)] || {};
    tr.append(el("td", `${n.name} (${n.vid})`));
    if (!pl) {
      const td = el("td", n.subnet ? __("podsíť nejde použít") : __("bez podsítě – brána ji nesměruje (doplň v Nastavení → Sítě)"), "muted");
      td.colSpan = 6; tr.appendChild(td); tb.appendChild(tr); continue;
    }
    const on = el("input"); on.type = "checkbox"; on.className = "gw-on"; on.checked = o.enabled !== false; on.setAttribute("aria-label", __("DHCP server"));
    const rng = (pl.dhcp ? pl.dhcp.ranges : "").split(",");
    const from = el("input"), to = el("input"), lease = el("input");
    from.className = "gw-from"; to.className = "gw-to"; lease.className = "gw-lease";
    from.size = to.size = 13; lease.size = 5;
    from.value = o.start || ""; to.value = o.end || ""; lease.value = o.lease || "";
    from.placeholder = rng[0] ? rng[0].split("-")[0] : ""; to.placeholder = rng.length ? rng[rng.length - 1].split("-").pop() : "";
    lease.placeholder = pl.dhcp ? pl.dhcp.lease : "12h";
    from.setAttribute("aria-label", __("Rozsah od")); to.setAttribute("aria-label", __("Rozsah do")); lease.setAttribute("aria-label", __("Zápůjčka"));
    const cell = (x) => { const td = el("td"); td.appendChild(x); return td; };
    tr.append(el("td", ZONE_LABEL[pl.zone] || pl.zone), el("td", pl.address), cell(on), cell(from), cell(to), cell(lease));
    for (const x of [on, from, to, lease]) x.disabled = !canManage();
    tb.appendChild(tr);
  }
  // matice: řádek = odkud, sloupec = kam
  const box = $("gw-matrix"); box.textContent = "";
  const zones = Object.keys(g.zones || {});
  if (zones.length) {
    const allow = new Set((g.policies || []).filter((p) => p.action === "accept").map((p) => p.src + ">" + p.dst));
    const t = el("table", "", "fw-matrix"), hr = el("tr");
    hr.appendChild(el("th", __("odkud ↓ / kam →")));
    for (const z of zones) hr.appendChild(el("th", ZONE_LABEL[z] || z));
    t.appendChild(hr);
    for (const s of zones) {
      const tr = el("tr"); tr.appendChild(el("th", ZONE_LABEL[s] || s));
      for (const d of zones) {
        const ok = allow.has(s + ">" + d) || (s === d && s !== "WAN");
        const td = el("td", s === d && s === "WAN" ? "–" : ok ? "✓" : "✕", s === d && s === "WAN" ? "muted" : ok ? "fw-allow" : "fw-block");
        td.title = (ZONE_LABEL[s] || s) + " → " + (ZONE_LABEL[d] || d) + ": " + (ok ? __("povoleno") : __("zakázáno"));
        tr.appendChild(td);
      }
      t.appendChild(tr);
    }
    box.appendChild(t);
  }
  $("gw-save").disabled = !canManage() || !!g.other_gateway;
}

$("gw-mode").addEventListener("change", gwModeFields);
$("gw-save").addEventListener("click", async () => {
  const wan = { port: $("gw-port").value, mode: $("gw-mode").value, vlan: $("gw-vlan").value.trim() || null };
  if (wan.mode === "static") Object.assign(wan, { address: $("gw-addr").value.trim(), gateway: $("gw-gate").value.trim(),
    dns: $("gw-dns").value.split(/[\s,;]+/).filter(Boolean) });
  if (wan.mode === "pppoe") { wan.user = $("gw-user").value.trim(); if ($("gw-pass").value) wan.password = $("gw-pass").value; }
  const dhcp = {};
  for (const tr of $("gw-nets").querySelectorAll("tr")) {
    const on = tr.querySelector(".gw-on");
    if (!on) continue;
    const o = { enabled: on.checked }, f = tr.querySelector(".gw-from").value.trim(), t = tr.querySelector(".gw-to").value.trim(),
      l = tr.querySelector(".gw-lease").value.trim();
    if (f || t) { o.start = f; o.end = t; }
    if (l) o.lease = l;
    dhcp[tr.dataset.vid] = o;
  }
  try {
    const r = await api("PUT", `/tenants/${tenantId}/devices/${detailId}/gateway`, { wan, dhcp });
    msg(__("Nastavení brány uloženo")); showApply(r.apply); loadControl(detailId);
  } catch (e) { msg(e.message, true); }
});


// --- průběh změny na zařízení (doručení → provedení → potvrzení → nová inventura) ----------------------

const PLAN_STATE = { queued: __("čeká na doručení"), confirming: __("provedeno, čeká na potvrzení spojení"), applied: __("hotovo"),
  failed: __("selhalo"), rolled_back: __("vráceno zpět") };
const CMD_STATE = { queued: __("čeká na doručení"), sent: __("zařízení provádí"), done: __("hotovo"), failed: __("selhalo") };
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
      const what = p ? `${(PLAN_MODULE[p.module] || p.module)}: ${sent ? __("zařízení provádí změnu") : PLAN_STATE[p.status] || p.status}`
        : c ? `${c.kind}: ${CMD_STATE[c.status] || c.status}` : __("probíhá změna");
      setBar(`${what} (${Math.round((Date.now() - w.t0) / 1000)} s)`);
    } else if (!w.seenBusy) {
      if (quiet) { stopWatch(); return; }
      w.seenBusy = true;  // změna proběhla dřív, než se stav poprvé načetl
    } else if (!w.doneAt) {
      w.doneAt = Date.now(); w.invAt = a.inventory_at;
      const last = a.plans[0];
      const bad = last && (last.status === "failed" || last.status === "rolled_back") ||
        (!last && a.commands[0] && a.commands[0].status === "failed");
      setBar(bad ? __`Změna ${PLAN_STATE[last ? last.status : "failed"]} – podrobnosti v historii zařízení` : __("Změna provedena – načítám nový stav zařízení…"),
             bad ? "fail" : "done");
      msg(bad ? __`${label || __("Zařízení")}: změna ${PLAN_STATE[last ? last.status : "failed"]}` : __`${label || __("Zařízení")}: změna provedena`, !!bad);
      loadPlans();
      if (bar()) openDetail(id);
    } else if ((a.inventory_at && a.inventory_at !== w.invAt) || Date.now() - w.doneAt > 60000) {
      stopWatch();
      if (detailId === id && !$("detail").hidden) { await openDetail(id); setBar(__("Stav zařízení aktuální"), "done"); setTimeout(() => { const b = bar(); if (b) b.hidden = true; }, 4000); }
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
    const tr = el("tr"); const td = el("td", __("Výchozí konfiguraci (obsahuje klíč lokality) vidí jen správce tenanta."), "muted");
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
    const cp = el("button", __("Kopírovat"), "small secondary"); cp.type = "button";
    cp.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(r.one_liner); msg(__("Příkaz zkopírován")); } catch (_e) { msg(__("Kopírování nejde – označ text ručně"), true); }
    });
    const td2 = el("td"); td2.append(pre, cp);
    const role = el("td"); role.append(el("strong", r.label), el("div", __`v controlleru: ${r.device_role}`, "muted"));
    tr.append(role, td1, td2);
    tb.appendChild(tr);
  }
}

$("deploy-rotate").addEventListener("click", async () => {
  if (!(await ask(__("Vyměnit klíč lokality? Všechny dříve stažené výchozí konfigurace a příkazy přestanou fungovat ") +
                  __("(zařízení, která se ještě neohlásila, se neohlásí). Adoptovaných zařízení se to netýká."), __("Vyměnit")))) return;
  try { renderDeploy(await api("POST", `/tenants/${tenantId}/sites/${siteId()}/defaults`, {})); msg(__("Klíč lokality vyměněn")); }
  catch (e) { msg(e.message, true); }
});

async function loadFactoryPasswords() {
  let rows;
  try { rows = await api("GET", `/tenants/${tenantId}/factory-passwords`); } catch (e) { return msg(e.message, true); }
  const tb = $("fpw-rows"); tb.textContent = "";
  for (const r of rows) {
    const tr = el("tr");
    const dev = r.device_id ? `${r.identity || "?"} (${r.state})` : __("zatím neadoptované");
    const pw = el("td");
    const show = el("button", __("Zobrazit"), "small secondary"); show.type = "button"; show.disabled = !canManage();
    show.addEventListener("click", async () => {
      try {
        const x = await api("POST", `/tenants/${tenantId}/factory-passwords/${encodeURIComponent(r.serial)}/reveal`, {});
        pw.textContent = ""; pw.appendChild(el("span", x.password, "pw-shown"));
        setTimeout(() => { pw.textContent = ""; pw.appendChild(show); }, 20000);
      } catch (e) { msg(e.message, true); }
    });
    pw.appendChild(show);
    const del = el("button", "×", "small secondary"); del.type = "button"; del.title = __("Smazat"); del.disabled = !canManage();
    del.addEventListener("click", async () => {
      if (!(await ask(__`Smazat uložené heslo pro ${r.serial}?`, __("Smazat")))) return;
      try { await api("DELETE", `/tenants/${tenantId}/factory-passwords/${encodeURIComponent(r.serial)}`); loadFactoryPasswords(); }
      catch (e) { msg(e.message, true); }
    });
    const tdd = el("td"); tdd.appendChild(del);
    tr.append(el("td", r.serial), el("td", r.note || "–", r.note ? "" : "muted"), el("td", dev, r.device_id ? "" : "muted"), pw, tdd);
    tb.appendChild(tr);
  }
  if (!rows.length) { const tr = el("tr"); const td = el("td", __("Zatím žádná hesla."), "muted"); td.colSpan = 5; tr.appendChild(td); tb.appendChild(tr); }
  for (const id of ["fpw-import"]) $(id).disabled = !canManage();
}

$("fpw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", `/tenants/${tenantId}/factory-passwords`,
              { serial: $("fpw-serial").value, password: $("fpw-pass").value, note: $("fpw-note").value });
    $("fpw-serial").value = ""; $("fpw-pass").value = ""; $("fpw-note").value = "";
    msg(__("Heslo uloženo (šifrovaně)")); loadFactoryPasswords();
  } catch (err) { msg(err.message, true); }
});
$("fpw-import").addEventListener("click", async () => {
  try {
    const r = await api("POST", `/tenants/${tenantId}/factory-passwords/import`, { csv: $("fpw-csv").value });
    $("fpw-import-res").textContent = __`importováno ${r.imported}` + (r.errors.length ? __`, chyby: ${r.errors.join("; ")}` : "");
    if (!r.errors.length) $("fpw-csv").value = "";
    loadFactoryPasswords();
  } catch (e) { msg(e.message, true); }
});
