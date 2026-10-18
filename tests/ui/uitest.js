// Minimální DOM + fetch pro spuštění app.js v Node (jen pro test vykreslení).
"use strict";
const fs = require("fs");
const vm = require("vm");
const [appPath, htmlPath, apiPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const api = JSON.parse(fs.readFileSync(apiPath, "utf8"));
const errors = [];
const requests = [];

class El {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ""; this.children = []; this._text = "";
    this.hidden = false; this.className = ""; this.attrs = {}; this.listeners = {}; this.value = ""; this.style = {};
    this.colSpan = 1; this.title = ""; this.href = ""; this.onclick = null;
  }
  set textContent(v) { this._text = v === null || v === undefined ? "" : String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => (c instanceof El ? c.textContent : String(c))).join(""); }
  set innerHTML(v) { throw new Error("innerHTML nesmí být použito (XSS): " + this.id); }
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  append(...cs) { for (const c of cs) this.children.push(typeof c === "string" ? c : c); }
  remove() {}
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
  async fire(ev, extra = {}) {
    const e = { preventDefault() {}, target: this, ...extra };
    for (const fn of this.listeners[ev] || []) await fn(e);
    if (ev === "click" && this.onclick) await this.onclick(e);
  }
  reset() {}
}

const byId = {};
for (const m of html.matchAll(/<(\w+)[^>]*\sid="([^"]+)"/g)) byId[m[2]] = new El(m[1], m[2]);
// pole formulářů jako vlastnosti formuláře (f.upstream.value)
for (const m of html.matchAll(/<form[^>]*id="([^"]+)"[\s\S]*?<\/form>/g)) {
  const form = byId[m[1]];
  for (const f of m[0].matchAll(/name="([^"]+)"/g)) form[f[1]] = new El("input");
}

const document = {
  getElementById: (id) => byId[id] || null,
  createElement: (t) => new El(t),
  createElementNS: (_ns, t) => new El(t),
  createTextNode: (t) => String(t),
};

function route(method, url, body) {
  requests.push(method + " " + url);
  const p = url.replace(/^\/api\/admin/, "/api/admin");
  if (method !== "GET") return { ok: true, status: 200, json: async () => ({ ok: true, detail: "ok", command_id: 1 }) };
  const r = api.responses[p];
  if (r === undefined) { errors.push("chybí zachycená odpověď pro " + p); return { ok: false, status: 404, json: async () => ({ detail: "x" }) }; }
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(r)) };
}

const ctx = {
  document, console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, Promise, Date, Math, JSON, Object, Array, String, Number,
  confirm: () => true, navigator: { clipboard: { writeText: async () => {} } }, location: { reload() {} },
  FormData: class { constructor(form) { this.form = form; } get(n) { return this.form[n] ? this.form[n].value : ""; } },
  fetch: async (url, opt) => route((opt && opt.method) || "GET", url, opt && opt.body),
};
ctx.window = ctx;
vm.createContext(ctx);
process.on("unhandledRejection", (e) => errors.push("unhandledRejection: " + (e && e.stack || e)));

(async () => {
  try {
    vm.runInContext(fs.readFileSync(appPath, "utf8") + "\n;globalThis.__t = { openDetail, drawTopology, renderClients };", ctx);
    await new Promise((r) => setImmediate(r));
    for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
    const visible = ["login", "mfa", "app", "detail"].filter((s) => !byId[s].hidden);
    if (visible.join() !== "app") errors.push("po bootu je vidět " + visible.join());
    const devRows = byId["devices"].children.length;
    if (byId["system-card"].hidden || byId["system-steps"].children.length !== 3) errors.push("stav systému");
    if (byId["users-card"].hidden || byId["users"].children.length !== 2) errors.push("uživatelé: čekány 2 řádky");
    const tiles = byId["overview"].children.length;
    if (tiles < 7) errors.push("přehled: čekáno aspoň 7 dlaždic, je " + tiles);
    if (byId["overview"].children[0].textContent !== "2Online") errors.push("přehled: " + byId["overview"].children[0].textContent);
    const alertRows = byId["alerts"].children.length;
    const plansN = byId["plans"].children.length;
    // lokalita: topologie a klienti
    await byId["topo-btn"].fire("click");
    await byId["clients-btn"].fire("click");
    await byId["wifi-btn"].fire("click");
    await byId["stp-btn"].fire("click");
    await byId["upg-btn"].fire("click");
    await byId["infra-btn"].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["infra-list"].children.length !== 4) errors.push("infrastruktura: seznam zařízení " + byId["infra-list"].children.length);
    const ig = byId["infra-svg"].children;
    if (ig.length !== 2 || ig[1].children.length < 6) errors.push("infrastruktura: strom se nevykreslil");
    byId["infra-ext"].checked = true;
    await byId["infra-ext"].fire("change");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const topoEls = byId["topo"].children.length;
    const clientRows = byId["clients"].children.length;
    const wifiRows = byId["wifi-radios"].children.length;
    if (wifiRows !== 5) errors.push("wifi: čekáno 5 rádií, je " + wifiRows);
    if (byId["wifi-plan-wrap"].hidden || byId["wifi-plan"].children.length !== 3) errors.push("wifi: plán kanálů");
    if (!byId["wifi-plan-sum"].textContent.includes("2 změny") || !byId["wifi-plan"].textContent.includes("52 / 20 MHz · DFS"))
      errors.push("wifi: plán kanálů obsah " + byId["wifi-plan-sum"].textContent);
    if (!byId["wifi-radios"].textContent.includes("36 · 5180 MHz")) errors.push("wifi: skutečný kanál v tabulce rádií");
    if (byId["wifi-caps-t"].hidden) errors.push("wifi: tabulka CAPsMAN skrytá");
    if (byId["upg-wrap"].hidden || byId["upg-jobs"].children.length !== 2) errors.push("aktualizace lokality: úlohy");
    if (!byId["upg-start"].hidden) errors.push("aktualizace lokality: spuštění musí být skryté, když úloha běží");
    const stpRows = byId["stp-bridges"].children.length;
    if (stpRows !== 3) errors.push("stp: čekány 3 bridge, je " + stpRows);
    if (!byId["stp-roots"].textContent.includes("core")) errors.push("stp: chybí root");
    // detail gw a sw (legacy) + náhled migrace + diff zálohy
    await ctx.__t.openDetail(api.gw);
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["d-notes"].hidden || byId["d-notes-list"].children.length !== 2) errors.push("release notes se nezobrazily");
    if (byId["d-upgrade"].hidden) errors.push("tlačítko aktualizace chybí");
    if (byId["d-security"].children.length < 5) errors.push("bezpečnostní nálezy se nezobrazily");
    // historie portů: dva dny, výběr portu filtruje
    const ph = byId["d-ph"].children;
    if (ph.length !== 4 || ph[0].tagName !== "H4" || ph[1].children.length !== 2) errors.push("historie portů: " + ph.length);
    if (byId["d-ph-port"].children.length !== 3) errors.push("historie portů: výběr portu");
    byId["d-ph-port"].value = "ether2"; await byId["d-ph-port"].onchange();
    for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
    if (byId["d-ph"].children.length !== 4 || byId["d-ph-n"].textContent !== "(2 změny)") errors.push("historie portů: filtr portu");
    // firewall: matice zón (WAN, LAN, GUEST + Router), klik na buňku, přepnutí na pravidla
    const fwT = byId["d-fw"].children[0];
    if (!fwT || fwT.className !== "fw-matrix" || fwT.children.length !== 4) errors.push("firewall: matice zón");
    else {
      const wanLan = fwT.children[1].children[2];
      if (!/fw-block/.test(wanLan.className)) errors.push("firewall: WAN→LAN musí být blokováno");
      await wanLan.fire("click");
      if (byId["d-fw-detail"].hidden || !byId["d-fw-detail-h"].textContent.includes("dstnat")) errors.push("firewall: detail buňky");
    }
    await byId["d-fw-views"].children[1].fire("click");
    if (byId["d-fw"].children[0].className !== "fw-rules" || byId["d-fw"].children[0].children.length !== 14)
      errors.push("firewall: tabulka pravidel");
    await byId["d-fw-views"].children[0].fire("click");
    const prow = byId["d-ports"].children[0];
    const pg = prow && prow.children[0];
    if (!pg || !/^port-group/.test(pg.className) || !pg.children.some((c) => /^port( |$)/.test(c.className)))
      errors.push("panel portů se nevykreslil");
    if (byId["d-traffic"].hidden) errors.push("graf celého zařízení se po otevření detailu nezobrazil");
    // přepínání pohledů portů
    const views = byId["d-ports-views"].children;
    if (views.length !== 6) errors.push("přepínač pohledů portů: " + views.length);
    // připojená zařízení: ikona + jméno nad portem 1 (kamera), tooltip s IP
    const lbl = pg.children.find((c) => /^port-label above/.test(c.className));
    const dev = lbl && lbl.children.find((c) => /^dev/.test(c.className || ""));
    if (!dev || !/camera/.test(dev.children[0].attrs.class || "") || !lbl.title.includes("192.168.10.50"))
      errors.push("panel portů: chybí ikona připojeného zařízení");
    // tabulka portů: 2 fyzické porty, řazení podle anomálie, profil, jméno VLAN
    if (byId["d-ptable-wrap"].hidden || byId["d-ptable-b"].children.length !== 2) errors.push("tabulka portů");
    const ptxt = byId["d-ptable-b"].textContent;
    if (!ptxt.includes("AP trunk") || !ptxt.includes("vlan10-lan (10)") || !ptxt.includes("GW-centrala")) errors.push("tabulka portů: obsah " + ptxt);
    await byId["d-ptable-h"].children[2].fire("click");
    if (!byId["d-ptable-b"].children[0].textContent.includes("45")) errors.push("tabulka portů: řazení podle anomálie");
    await byId["d-ports-opts"].children[1].fire("click");
    if (!byId["d-ptable-wrap"].hidden) errors.push("přepínač tabulky portů");
    await byId["d-ports-opts"].children[1].fire("click");
    await views[5].fire("click");
    if (!byId["d-ports-legend"].textContent.includes("nejhorší port 45")) errors.push("pohled anomálií");
    await views[2].fire("click");
    if (!byId["d-ports"].children.some((c) => c.className === "vlan-row")) errors.push("pohled VLAN: chybí matice");
    if (byId["d-vlan-filter"].hidden) errors.push("pohled VLAN: chybí filtr");
    if (!byId["d-ports"].textContent.includes("192.168.10.0/24")) errors.push("pohled VLAN: chybí podsíť");
    await byId["d-ports-views"].children[3].fire("click");
    if (byId["d-ports"].children[0].className !== "port-row") errors.push("pohled PoE");
    await byId["d-ports-views"].children[0].fire("click");
    // přepínače grafu: metrika Chyby, jen přijato
    await byId["tr-metric"].children[1].fire("click");
    await byId["tr-dir"].children[1].fire("click");
    const pl = byId["d-traffic-svg"].children.filter((c) => c.tagName === "POLYLINE").length;
    if (pl !== 1) errors.push("graf: po volbě RX čekána 1 křivka, je " + pl);
    await byId["tr-dir"].children[0].fire("click");
    await byId["tr-metric"].children[0].fire("click");
    await byId["d-ifaces"].children[0].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const lines = byId["d-traffic-svg"].children.filter((c) => c.tagName === "POLYLINE").length;
    if (byId["d-traffic"].hidden || lines !== 2) errors.push("graf provozu: čekány 2 křivky, je " + lines);
    if (byId["tr-scope"].children[1].disabled) errors.push("graf: po kliknutí na port má jít přepnout na port");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["port-edit"].hidden) errors.push("editor portu se neotevřel");
    const pe = JSON.stringify(byId["port-edit"].textContent);
    if (!pe.includes("Povolit správu portů") || !pe.includes("Použít")) errors.push("editor portu: chybí akce " + pe.slice(0, 120));
    // profil portu: výběr „AP trunk“ zaškrtne VLAN 10 a 40 a doplní 50
    const find = (n, pred) => pred(n) ? n : (n.children || []).reduce((a, c) => a || (c instanceof El ? find(c, pred) : null), null);
    const prof = find(byId["port-edit"], (n) => n.className === "pe-prof");
    if (!prof || byId["port-edit"].children.indexOf(prof) > byId["port-edit"].children.findIndex((c) => c.className === "pe-grid"))
      errors.push("editor portu: výběr profilu chybí nebo je pod formulářem");
    else {
      const sel = prof.children[1];
      if (sel.children.length !== 3) errors.push("profil: čekány 3 volby");
      sel.value = "2"; await sel.fire("change");
      const checks = [];
      find(byId["port-edit"], (n) => { if (n.tagName === "INPUT" && n.type === "checkbox" && n.value) checks.push(n); return false; });
      const on = checks.filter((c) => c.checked).map((c) => c.value).join(",");
      const extra = find(byId["port-edit"], (n) => n.tagName === "INPUT" && n.placeholder && n.placeholder.startsWith("další VLAN"));
      if (on !== "10,40" || !extra || extra.value !== "50") errors.push(`profil: VLAN ${on} / ${extra && extra.value}`);
      if (prof.children[2].hidden) errors.push("profil: chybí tlačítko smazat");
    }
    if (!pe.includes("Uložit jako profil")) errors.push("editor portu: chybí uložení profilu");
    if (!pe.includes("Historie portu") || !pe.includes("linka dole")) errors.push("editor portu: chybí historie");
    await byId["tr-range"].children[2].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const l7 = byId["d-traffic-svg"].children.filter((c) => c.tagName === "POLYLINE").length;
    if (l7 !== 4) errors.push("graf 7 dní: čekány 2 křivky + 2 špičky, je " + l7);
    await byId["tr-range"].children[1].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const diffBtn = byId["d-backups"].children[0].children[4].children[0];
    if (diffBtn && diffBtn.onclick) await diffBtn.onclick();
    const diffShown = !byId["d-diff"].hidden;
    await ctx.__t.openDetail(api.sw);
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (!byId["d-traffic-info"].textContent.includes("málo dat")) errors.push("prázdný graf: chybí hláška");
    await byId["d-vlan-plan"].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const steps = byId["d-vlan-steps"].children.length;
    console.log(JSON.stringify({ devRows, alertRows, plansN, topoEls, clientRows, wifiRows, diffShown, steps,
                                 title: byId["d-title"].textContent, vlan: byId["d-vlan-status"].textContent,
                                 requests: requests.length }));
  } catch (e) { errors.push(e.stack || String(e)); }
  if (errors.length) { console.error("CHYBY:\n" + errors.join("\n")); process.exit(1); }
  console.log("UI OK");
})();
