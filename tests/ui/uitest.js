// Minimální DOM + fetch pro spuštění app.js v Node (jen pro test vykreslení).
"use strict";
const fs = require("fs");
const vm = require("vm");
const [appPath, htmlPath, apiPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const api = JSON.parse(fs.readFileSync(apiPath, "utf8"));
const errors = [];
const requests = [];
const byId = {};

class El {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ""; this.children = []; this._text = "";
    this.hidden = false; this.className = ""; this.attrs = {}; this.listeners = {}; this.value = ""; this.style = {};
    this.colSpan = 1; this.title = ""; this.href = ""; this.onclick = null;
  }
  set textContent(v) { this._text = v === null || v === undefined ? "" : String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => (c instanceof El ? c.textContent : String(c))).join(""); }
  set innerHTML(v) { throw new Error("innerHTML nesmí být použito (XSS): " + this.id); }
  // prvky vytvořené skriptem s id jsou dostupné přes getElementById (navigace)
  set id(v) { this._id = v; if (v) byId[v] = this; }
  get id() { return this._id || ""; }
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  append(...cs) { for (const c of cs) this.children.push(typeof c === "string" ? c : c); }
  prepend(...cs) { this.children.unshift(...cs); }
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
  document, console, setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {}, Promise, Date, Math, JSON, Object, Array, String, Number,
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
    {
      const r0 = byId["devices"].children[0];
      if (!r0.children[1].textContent.includes("GW1") || r0.children[2].textContent !== "rozvaděč 2. patro") errors.push("přehled: identita/poznámka " + r0.children[1].textContent + "|" + r0.children[2].textContent);
      const n0 = requests.length;
      const pn = r0.children[2].fire("dblclick", {});
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
      if (byId["ask"].hidden || byId["ask-input"].value !== "rozvaděč 2. patro") errors.push("přehled: úprava poznámky");
      else { byId["ask-input"].value = "nová"; byId["ask-ok"].onclick(); await pn; for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); }
      if (!requests.slice(n0).some((r) => r.startsWith("PATCH") && /\/devices\/[^/]+$/.test(r))) errors.push("přehled: uložení poznámky");
    }
    // levá navigace: sekce, odznaky, přepínání
    if (byId["nav"].children.length !== 7) errors.push("navigace: čekáno 7 sekcí, je " + byId["nav"].children.length);
    if (byId["view-overview"].hidden || !byId["view-alerts"].hidden) errors.push("navigace: výchozí pohled");
    await byId["nav-alerts"].fire("click");
    if (byId["view-alerts"].hidden || !byId["view-overview"].hidden || byId["nav-alerts"].className !== "navbtn active") errors.push("navigace: přepnutí na alerty");
    if (byId["nav-alerts"].children[1].textContent !== "4" || byId["nav-plans"].children[1].textContent !== "2") errors.push("navigace: odznaky " + byId["nav-alerts"].children[1].textContent);
    await byId["nav-site"].fire("click");
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
    // sítě a DHCP snooping
    await byId["net-btn"].fire("click");
    if (byId["net-wrap"].hidden || byId["net-rows"].children.length !== 3) errors.push("sítě: tabulka");
    if (!byId["net-rows"].textContent.includes("Relay → 192.168.30.10") || !byId["net-rows"].textContent.includes("1 / 100"))
      errors.push("sítě: DHCP sloupce " + byId["net-rows"].textContent.slice(0, 200));
    if (byId["net-bridges"].children.length !== 4) errors.push("sítě: bridge");
    // záměr lokality (řízený režim)
    for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
    if (byId["intent-rows"].children.length !== 3) errors.push("záměr: řádky " + byId["intent-rows"].children.length);
    if (byId["intent-access"].value !== "10" || !byId["intent-native"].checked) errors.push("záměr: volby");
    if (byId["intent-devs"].children.length !== 4 || !byId["intent-devs"].textContent.includes("řízené")) errors.push("záměr: zařízení");
    {
      const n0 = requests.length;
      await byId["intent-add"].fire("click");
      if (byId["intent-rows"].children.length !== 4) errors.push("záměr: přidání sítě");
      await byId["intent-save"].fire("click");
      await byId["intent-sync"].fire("click");
      const rq = requests.slice(n0);
      if (!rq.some((r) => r.startsWith("PUT") && r.endsWith("/intent"))) errors.push("záměr: uložení");
      if (!rq.some((r) => r.startsWith("POST") && r.endsWith("/intent/reconcile"))) errors.push("záměr: sladění");
    }
    // Wi-Fi: CAPsMAN jen z řízených zařízení, karty SSID, editor, skupiny AP, pravé tlačítko, nasazení přes dialog
    {
      const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      const ctxItem = (label) => find(byId["ctx"], (n) => n.tagName === "BUTTON" && n.textContent.includes(label));
      await tick();
      if (byId["wifi-host"].children.length !== 4 || byId["wifi-host"].value !== api.sw) errors.push("wifi: CAPsMAN výběr " + byId["wifi-host"].children.length);
      const cards = byId["ssid-cards"].children;
      if (cards.length !== 3) errors.push("wifi: karty SSID " + cards.length);
      if (!cards[1].textContent.includes("2× heslo") || !cards[1].textContent.includes("Sklad") || !cards[0].textContent.includes("všechna AP"))
        errors.push("wifi: karta " + cards[1].textContent);
      if (byId["wifi-deploy"].disabled) errors.push("wifi: nasazení zakázané");
      // tabule skupin: bez skupiny + 2 skupiny, AP ve správných sloupcích
      const cols = byId["apg-board"].children;
      if (cols.length !== 3 || !cols[0].textContent.includes("AP-chodba") || !cols[1].textContent.includes("AP-sklad"))
        errors.push("wifi: tabule skupin " + cols.length + " " + (cols[1] || {}).textContent);
      if (!cols[1].textContent.includes("Hoste") || cols[0].textContent.includes("Hoste")) errors.push("wifi: SSID ve skupině");
      let n0 = requests.length;
      // pravé tlačítko na kartě SSID
      await cards[1].fire("contextmenu", { clientX: 50, clientY: 60 });
      if (byId["ctx"].hidden || !ctxItem("Zobrazit hesla") || !ctxItem("Kancelář")) errors.push("ctx: menu SSID " + byId["ctx"].textContent);
      else {
        await ctxItem("Vypnout").fire("click");
        if (!byId["ctx"].hidden) errors.push("ctx: menu se po volbě nezavřelo");
        if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.includes("/ssids/0b9a3c1e-0000-4000-8000-000000000002"))) errors.push("ctx: vypnutí SSID");
      }
      // editor SSID: skupiny, více hesel, uložení
      await tick();
      await byId["ssid-cards"].children[0].fire("click", { target: byId["ssid-cards"].children[0] });
      if (byId["ssid-dlg"].hidden || byId["sd-ssid"].value !== "Firma") errors.push("wifi: editor SSID");
      if (byId["sd-groups"].children.length !== 3 || byId["sd-bands"].children.length !== 3) errors.push("wifi: editor skupiny/pásma");
      await byId["sd-groups"].children[1].fire("click");
      await byId["sd-ppsk-add"].fire("click");
      if (byId["sd-ppsk"].children.length !== 1) errors.push("wifi: více hesel řádek");
      byId["sd-csv"].value = "Vyroba;Hosté;vyroba-heslo-1\nnesmysl";
      await byId["sd-csv-ok"].fire("click");
      if (byId["sd-ppsk"].children.length !== 2 || byId["sd-csv"].value !== "nesmysl") errors.push("wifi: import CSV " + byId["sd-ppsk"].children.length);
      n0 = requests.length;
      await byId["sd-save"].fire("click");
      if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.includes("/ssids/"))) errors.push("wifi: uložení SSID");
      if (!byId["ssid-dlg"].hidden) errors.push("wifi: editor po uložení zůstal");
      await tick();
      // AP: pravé tlačítko → přesun do skupiny; přetažení do sloupce
      const chip = find(byId["apg-board"], (n) => /^ap-chip/.test(n.className) && n.textContent.includes("AP-chodba"));
      n0 = requests.length;
      await chip.fire("contextmenu", {});
      if (!ctxItem("Kancelář")) errors.push("ctx: menu AP " + byId["ctx"].textContent);
      else await ctxItem("Kancelář").fire("click");
      await tick();
      const col = byId["apg-board"].children[2];
      await col.fire("drop", { dataTransfer: { getData: () => "d1" } });
      if (requests.slice(n0).filter((r) => r.startsWith("POST") && r.endsWith("/ap-groups")).length !== 2) errors.push("wifi: přesun AP");
      // nová skupina přes dialog
      n0 = requests.length;
      const pg = byId["apg-add"].fire("click");
      await tick();
      if (byId["ask"].hidden || byId["ask-input"].hidden) errors.push("wifi: dialog nové skupiny");
      else { byId["ask-input"].value = "Venku"; byId["ask-ok"].onclick(); await pg; }
      if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.endsWith("/intent"))) errors.push("wifi: uložení skupiny");
      await tick();
      // pravé tlačítko na řádku zařízení
      await byId["devices"].children[0].fire("contextmenu", {});
      if (!ctxItem("Restart") || !ctxItem("Otevřít detail")) errors.push("ctx: menu zařízení " + byId["ctx"].textContent);
      byId["ctx"].hidden = true;
      n0 = requests.length;
      const p = byId["wifi-deploy"].fire("click");
      await tick();
      if (byId["ask"].hidden || !byId["ask-text"].textContent.includes("Wi-Fi: 2 SSID")) errors.push("wifi: dialog " + byId["ask-text"].textContent.slice(0, 60));
      else { byId["ask-ok"].onclick(); await p; }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/wifi/plan"))) errors.push("wifi: plán");
      if (byId["wifi-result"].children.length < 5 || !byId["wifi-result"].textContent.includes("doladí")) errors.push("wifi: přehled rádií");
    }
    if (!byId["dg-rogue"].textContent.includes("E4:5F:01:00:00:09")) errors.push("DHCP: cizí server");
    if (!byId["dg-missing"].textContent.includes("192.168.30.10")) errors.push("DHCP: nedohledaný server");
    const dgRows = byId["dg-devices"].children;
    if (dgRows.length !== 4 || !dgRows[1].textContent.includes("ether1ether2")) errors.push("DHCP: plán po zařízeních " + (dgRows[1] || {}).textContent);
    if (byId["dg-generate"].hidden) errors.push("DHCP: tlačítko plánů");
    if (byId["dg-m-ip"].value !== "192.168.30.10") errors.push("DHCP: předvyplněná IP");
    byId["dg-m-port"].value = "ether5";
    const before = requests.length;
    await byId["dg-manual"].fire("submit");
    if (!requests.slice(before).some((r) => r.startsWith("POST") && r.endsWith("/policy/dhcp_snooping"))) errors.push("DHCP: uložení ručního zdroje");
    await byId["dg-generate"].fire("click");
    // leases: klik na počet → přehled, mapa, filtr, editace
    const leaseBtn = byId["net-rows"].children[0].children[5].children[0];
    if (!leaseBtn || leaseBtn.className !== "linkbtn") errors.push("sítě: leases nejsou klikací");
    else {
      await leaseBtn.fire("click");
      if (byId["lease-wrap"].hidden) errors.push("leases: panel");
      const lr = byId["lease-rows"].children.filter((r) => /^lease /.test(r.className));
      if (lr.length !== 6) errors.push("leases: řádky " + lr.length);
      if (!byId["lease-rows"].textContent.includes("Synology Incorporated") || !byId["lease-rows"].textContent.includes("zálohy"))
        errors.push("leases: výrobce/poznámka");
      if (byId["lease-map"].children.length !== 254) errors.push("leases: mapa " + byId["lease-map"].children.length);
      if (!byId["lease-sum"].textContent.includes("1 konflikt")) errors.push("leases: souhrn " + byId["lease-sum"].textContent);
      await byId["lease-filter"].children[3].fire("click");
      if (byId["lease-rows"].children.filter((r) => /^lease /.test(r.className)).length !== 3) errors.push("leases: filtr ruční IP");
      await byId["lease-filter"].children[0].fire("click");
      // úprava dynamického klienta → rezervace
      const dyn = byId["lease-rows"].children.find((r) => r.className.startsWith("lease dynamic"));
      await dyn.children[8].children[0].fire("click");
      const ed = byId["lease-rows"].children.find((r) => r.className === "lease-edit-row");
      if (!ed || !ed.textContent.includes("Vytvořit rezervaci a použít") || !ed.textContent.includes("Uložit jen poznámku"))
        errors.push("leases: editor " + (ed ? ed.textContent.slice(0, 120) : "chybí"));
      else {
        const n0 = requests.length;
        const findB = (n, t) => n.tagName === "BUTTON" && n.textContent === t ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? findB(x, t) : null), null);
        await findB(ed, "Vytvořit rezervaci a použít").fire("click");
        if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/dhcp/dhcp10/lease"))) errors.push("leases: odeslání plánu");
      }
      // klik na volnou adresu v mapě = nová rezervace s předvyplněnou IP
      await byId["lease-map"].children[200].fire("click");
      if (!byId["lease-rows"].children[0] || byId["lease-rows"].children[0].className !== "lease-edit-row") errors.push("leases: nová rezervace z mapy");
    }
    if (!requests.some((r) => r.startsWith("POST") && r.endsWith("/plans/dhcp_snooping"))) errors.push("DHCP: generování plánů");
    // nasazení: výchozí konfigurace podle role, výchozí hesla
    {
      const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
      await byId["deploy-btn"].fire("click"); await tick();
      if (byId["deploy-wrap"].hidden || byId["deploy-roles"].children.length !== 5) errors.push("nasazení: role " + byId["deploy-roles"].children.length);
      if (!byId["deploy-roles"].textContent.includes("reset-configuration no-defaults=yes")) errors.push("nasazení: příkaz");
      if (byId["fpw-rows"].children.length !== 2 || !byId["fpw-rows"].textContent.includes("zatím neadoptované")) errors.push("nasazení: hesla");
      const n0 = requests.length;
      byId["fpw-csv"].value = "AB12CD;Heslo1234";
      await byId["fpw-import"].fire("click");
      await byId["fpw-rows"].children[0].children[3].children[0].fire("click");
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/factory-passwords/import"))) errors.push("nasazení: import");
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/reveal"))) errors.push("nasazení: zobrazení hesla");
    }
    await byId["upg-btn"].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["upg-wrap"].hidden || byId["upg-jobs"].children.length !== 2) errors.push("aktualizace lokality: úlohy");
    if (!byId["upg-start"].hidden) errors.push("aktualizace lokality: spuštění musí být skryté, když úloha běží");
    if (!byId["net-wrap"].hidden) errors.push("záložky lokality: po přepnutí má být předchozí panel skrytý");
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
    const stpRows = byId["stp-bridges"].children.length;
    if (stpRows !== 3) errors.push("stp: čekány 3 bridge, je " + stpRows);
    if (!byId["stp-roots"].textContent.includes("core")) errors.push("stp: chybí root");
    // detail gw a sw (legacy) + náhled migrace + diff zálohy
    await ctx.__t.openDetail(api.gw);
    {
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
      // detail: poznámka v poli a uložení formuláře (PATCH, ne odeslání stránky)
      if (byId["d-edit-note"].value !== "rozvaděč 2. patro") errors.push("detail: poznámka v poli " + byId["d-edit-note"].value);
      const n0 = requests.length;
      byId["d-edit-note"].value = "jinde";
      await byId["d-edit"].fire("submit");
      if (!requests.slice(n0).some((r) => r.startsWith("PATCH") && r.includes(api.gw))) errors.push("detail: uložení úprav");
    }
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["d-notes"].hidden || byId["d-notes-list"].children.length !== 2) errors.push("release notes se nezobrazily");
    if (byId["d-upgrade"].hidden) errors.push("tlačítko aktualizace chybí");
    if (byId["d-security"].children.length < 5) errors.push("bezpečnostní nálezy se nezobrazily");
    // povolení správy modulu: vlastní dialog (ne confirm), Zrušit nic nepošle, Potvrdit pošle POST
    {
      const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
      const findB = (n, re) => n.tagName === "BUTTON" && re.test(n.textContent) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? findB(x, re) : null), null);
      const mb = findB(byId["d-modules"], /^Povolit správu$/);
      if (!mb) errors.push("moduly: chybí tlačítko Povolit správu");
      else {
        ctx.confirm = () => { errors.push("použit systémový confirm()"); return false; };
        const n0 = requests.length;
        let p = mb.onclick({});
        await tick();
        if (byId["ask"].hidden || !byId["ask-text"].textContent.includes("Povolit controlleru")) errors.push("dialog: neotevřel se");
        byId["ask-cancel"].onclick(); await p;
        if (!byId["ask"].hidden || requests.slice(n0).some((r) => r.startsWith("POST"))) errors.push("dialog: zrušení");
        p = mb.onclick({}); await tick();
        byId["ask-ok"].onclick(); await p;
        if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/modules"))) errors.push("dialog: potvrzení neposlalo POST");
        await tick();
      }
    }
    // řízení: náhled převzetí, výjimka portu, plán přes vlastní dialog
    {
      const tick = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
      await byId["dt-ctl"].fire("click"); await tick();
      if (byId["dp-ctl"].hidden || byId["ctl-ports"].children.length !== 6) errors.push("řízení: porty " + byId["ctl-ports"].children.length);
      if (!byId["ctl-ports"].textContent.includes("ether5-tiskarna (ether5)") || !byId["ctl-ports"].textContent.includes("uplink"))
        errors.push("řízení: jména/důvody portů");
      if (byId["ctl-vlans"].children.length !== 3 || byId["ctl-badge"].textContent !== "sledované") errors.push("řízení: VLAN/stav");
      if (byId["ctl-go"].disabled) errors.push("řízení: tlačítko převzetí");
      if (byId["ctl-role"].children.length !== 2 || byId["ctl-role"].children[1].disabled) errors.push("řízení: role AP");
      const sel = byId["ctl-ports"].children[1].children[1].children[0];
      sel.value = "access:20"; await sel.fire("change");
      const n0 = requests.length;
      await byId["ctl-save"].fire("click");
      if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.endsWith("/intent"))) errors.push("řízení: uložení výjimek");
      const p = byId["ctl-go"].onclick ? byId["ctl-go"].onclick({}) : byId["ctl-go"].fire("click");
      await tick();
      if (byId["ask"].hidden) errors.push("řízení: dialog převzetí");
      else { byId["ask-ok"].onclick(); await p; }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/takeover"))) errors.push("řízení: plán převzetí");
      await byId["dt-ports"].fire("click");
    }
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
