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
  document, console, setTimeout: () => 0, setInterval: () => 0, Promise, Date, Math, JSON, Object, Array, String, Number,
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
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const topoEls = byId["topo"].children.length;
    const clientRows = byId["clients"].children.length;
    const wifiRows = byId["wifi-radios"].children.length;
    if (wifiRows !== 5) errors.push("wifi: čekáno 5 rádií, je " + wifiRows);
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
    await byId["d-ifaces"].children[0].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    const lines = byId["d-traffic-svg"].children.filter((c) => c.tagName === "POLYLINE").length;
    if (byId["d-traffic"].hidden || lines !== 2) errors.push("graf provozu: čekány 2 křivky, je " + lines);
    const diffBtn = byId["d-backups"].children[0].children[4].children[0];
    if (diffBtn && diffBtn.onclick) await diffBtn.onclick();
    const diffShown = !byId["d-diff"].hidden;
    await ctx.__t.openDetail(api.sw);
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
