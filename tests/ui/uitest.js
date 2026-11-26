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
    vm.runInContext(fs.readFileSync(appPath, "utf8") + "\n;globalThis.__t = { openDetail, drawTopology, renderClients, setCapsmanHost, openAdopt, flowBanner };", ctx);
    await new Promise((r) => setImmediate(r));
    for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
    const visible = ["login", "mfa", "app", "detail"].filter((s) => !byId[s].hidden);
    if (visible.join() !== "app") errors.push("po bootu je vidět " + visible.join());
    const devRows = byId["devices"].children.length;
    // přehled zařízení: třídění, filtr, volba sloupců
    {
      const head = () => byId["devices-head"].children.map((th) => th.textContent);
      if (!head().some((h) => h.startsWith("Uptime")) || head().some((h) => h.startsWith("Alerty"))) errors.push("zařízení: výchozí sloupce " + head());
      const iUp = head().findIndex((h) => h.startsWith("Uptime"));
      await byId["devices-head"].children[iUp].fire("click");
      if (!byId["devices"].children[0].textContent.includes("SW1")) errors.push("zařízení: třídění podle uptime");
      if (!byId["devices"].children[0].children[iUp].textContent.includes("20 min")) errors.push("zařízení: uptime " + byId["devices"].children[0].children[iUp].textContent);
      await byId["devices-head"].children[iUp].fire("click");
      if (!byId["devices"].children[0].textContent.includes("GW1")) errors.push("zařízení: třídění sestupně");
      // levý panel: skupiny filtrů s počty; zaškrtnutí zúží seznam, „Zrušit filtry“ vrátí vše
      {
        const groups = byId["dev-facets"].children.map((x) => x.children[0].textContent);
        if (!groups.includes("Typ zařízení") || !groups.includes("RouterOS")) errors.push("zařízení: skupiny filtrů " + groups.join(","));
        const sec = byId["dev-facets"].children.find((x) => x.children[0].textContent === "RouterOS");
        const before = byId["devices"].children.length;
        const cb = sec.children[1].children[0]; cb.checked = true; await cb.fire("change");
        if (byId["devices"].children.length >= before || byId["dev-clear"].disabled) errors.push("zařízení: filtr RouterOS nezúžil seznam");
        await byId["dev-clear"].fire("click");
        if (byId["devices"].children.length !== before) errors.push("zařízení: Zrušit filtry");
        // řazení podle topologie: potomek (sw) je pod bránou a odsazený
        byId["dev-topo"].checked = true; await byId["dev-topo"].fire("change");
        const rows = byId["devices"].children;
        if (!rows[0].textContent.includes("gw") || rows[1].attrs["data-depth"] !== "1")
          errors.push("zařízení: řazení podle topologie");
        byId["dev-topo"].checked = false; await byId["dev-topo"].fire("change");
      }
      byId["dev-q"].value = "sw"; await byId["dev-q"].fire("input");
      if (byId["devices"].children.length !== 1 || byId["dev-count"].textContent !== "1 z 2") errors.push("zařízení: filtr " + byId["dev-count"].textContent);
      byId["dev-q"].value = ""; await byId["dev-q"].fire("input");
      await byId["dev-state"].children[2].fire("click");
      if (!byId["devices"].textContent.includes("Filtru nic neodpovídá")) errors.push("zařízení: filtr stavu");
      await byId["dev-state"].children[4].fire("click");
      if (byId["devices"].children.length !== 2) errors.push("zařízení: s problémem " + byId["devices"].children.length);
      await byId["dev-state"].children[0].fire("click");
      byId["dev-cols-pop"].hidden = true;  // falešný DOM nečte atribut hidden z HTML
      await byId["dev-cols"].fire("click");
      const cb = byId["dev-cols-pop"].children.find((l) => l.textContent.includes("Alerty")).children[0];
      cb.checked = true; await cb.fire("change");
      if (!head().some((h) => h.startsWith("Alerty"))) errors.push("zařízení: přidání sloupce");
      await byId["devices-head"].children[1].fire("click");   // zpět podle jména
    }
    {
      const r0 = byId["devices"].children[0];
      if (!r0.children[1].textContent.includes("GW1") || r0.children[2].textContent !== "rozvaděč 2. patro") errors.push("přehled: identita/poznámka " + r0.children[1].textContent + "|" + r0.children[2].textContent);
      const n0 = requests.length;
      const pn = r0.children[2].fire("dblclick", {});
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
      if (byId["ask"].hidden || byId["ask-input"].value !== "rozvaděč 2. patro") errors.push("přehled: úprava poznámky");
      else { byId["ask-input"].value = "nová"; byId["ask-ok"].onclick(); await pn; for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); }
      if (!requests.slice(n0).some((r) => r.startsWith("PATCH") && /\/devices\/[^/]+$/.test(r))) errors.push("přehled: uložení poznámky");
      const n1 = requests.length;
      const pr = r0.children[1].fire("dblclick", {});
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
      if (byId["ask"].hidden || byId["ask-input"].value !== "gw" || byId["ask-input"].maxLength !== 63) errors.push("přehled: přejmenování " + byId["ask-input"].value);
      else { byId["ask-input"].value = "GW-praha"; byId["ask-ok"].onclick(); await pr; for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); }
      if (!requests.slice(n1).some((r) => r.startsWith("POST") && r.endsWith("/identity"))) errors.push("přehled: odeslání jména");
    }
    // CSP (style-src 'self') blokuje atribut style – styly se smí nastavovat jen přes CSSOM nebo třídy
    {
      const src = require("fs").readFileSync(process.argv[2], "utf8");
      if (/setAttribute\(\s*["']style["']/.test(src) || /[{,]\s*style:\s*[`"']/.test(src) || /style\s*=\s*["'`][^"'`]*:/.test(src.replace(/\.style\.[a-zA-Z]+ = /g, ""))) errors.push("app.js nastavuje atribut style – v produkci ho zablokuje CSP");
      if (/\sstyle="/.test(require("fs").readFileSync(process.argv[3], "utf8"))) errors.push("index.html obsahuje atribut style – zablokuje ho CSP");
    }
    // levá navigace: 8 sekcí lokality + celý tenant (lokality, uživatelé, systém), odznaky, přepínání
    const navBtns = byId["nav-items"].children.filter((b) => b.tagName === "BUTTON");
    if (navBtns.length !== 8 || navBtns[0].textContent !== "Přehled" || navBtns[0].title !== "Přehled lokality" || navBtns[1].textContent !== "Mapa") errors.push("navigace: sekce " + navBtns.map((b) => b.textContent));
    // správa zákazníka je v nabídce v záhlaví (lokality, uživatelé, systém), zvonek nese počet k řešení, uživatel má vlastní nabídku
    {
      const items = byId["tmenu-list"].children.filter((b) => b.tagName === "BUTTON");
      if (items.length !== 4 || !items[2].textContent.includes("Zálohy") || !items[0].textContent.includes("Lokality") || !items[0].textContent.includes("všechny lokality zákazníka")) errors.push("záhlaví: nabídka správy " + items.map((b) => b.textContent));
      byId["tmenu-list"].hidden = true; byId["umenu-list"].hidden = true;  // falešný DOM nečte atribut hidden z HTML
      await byId["tmenu-btn"].fire("click", { stopPropagation() {} });
      if (byId["tmenu-list"].hidden) errors.push("záhlaví: nabídka se neotevřela");
      await byId["umenu-btn"].fire("click", { stopPropagation() {} });
      if (!byId["tmenu-list"].hidden || byId["umenu-list"].hidden) errors.push("záhlaví: otevřená má být jen jedna nabídka");
      if (byId["umenu"].hidden || !byId["umenu-btn"].textContent.includes("@") || !byId["who"].textContent.includes("Odhlásit") || !byId["who"].textContent.includes("Změnit heslo")) errors.push("záhlaví: nabídka uživatele " + byId["who"].textContent);
      await byId["nav-users"].fire("click");
      if (!byId["umenu-list"].hidden || byId["view-users"].hidden) errors.push("záhlaví: položka nabídky neotevřela sekci");
      // Zálohy: vlastní sekce ve správě – plán, stupňovaná retence, cíl Dropbox s vložením kódu
      await byId["nav-backups"].fire("click");
      for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
      if (byId["view-backups"].hidden || !byId["view-system"].hidden) errors.push("zálohy: sekce");
      if (byId["bk-keep"].value != 14 || byId["bk-monthly"].value != 12 || !byId["bk-ret-sum"].textContent.includes("nejvýš 26 záloh (14 posledních + 12 měsíčních)")) errors.push("zálohy: retence " + byId["bk-ret-sum"].textContent);
      if (!byId["backup-summary"].textContent.includes("14 posledních + 12 měsíčních")) errors.push("zálohy: souhrn " + byId["backup-summary"].textContent);
      byId["bk-keep"].value = "30"; byId["bk-weekly"].value = "8"; byId["bk-monthly"].value = "24"; await byId["bk-keep"].fire("input");
      if (!byId["bk-ret-sum"].textContent.includes("nejvýš 62 záloh") || !byId["bk-ret-sum"].textContent.includes("2 roky")) errors.push("zálohy: přepočet retence " + byId["bk-ret-sum"].textContent);
      const nb = requests.length;
      await byId["bk-plan"].fire("submit", { preventDefault() {} });
      for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
      if (!requests.slice(nb).some((r) => r.startsWith("PUT") && r.endsWith("/system/backup-plan"))) errors.push("zálohy: uložení plánu");
      const drow = byId["bt-list"].children[0];
      if (!drow.textContent.includes("Dropbox · 14 posledních + 12 měsíčních") || !drow.textContent.includes("Otevři Dropbox") || !drow.textContent.includes("Dokončit")) errors.push("zálohy: cíl Dropbox " + drow.textContent);
      // externí úložiště: ověření, že záloha v úložišti je; tlačítko „Ověřit zálohu“ volá kontrolu
      const nrow = byId["bt-list"].children[1];
      if (!nrow.textContent.includes("✓ záloha je v úložišti (12 celkem)") || !nrow.textContent.includes("Ověřit zálohu") || !nrow.textContent.includes("Test spojení")) errors.push("zálohy: ověření v úložišti " + nrow.textContent);
      if (!byId["bt-list"].children[2].textContent.includes("zatím neověřeno")) errors.push("zálohy: neověřené úložiště");
      const nv = requests.length, acts = nrow.children[4].children[0].children;
      await acts.filter((b) => b.textContent === "Ověřit zálohu")[0].fire("click");
      for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
      if (!requests.slice(nv).some((r) => r.startsWith("POST") && r.endsWith("/verify"))) errors.push("zálohy: ověření neodeslalo dotaz");
      await byId["nav-dash"].fire("click");
      if (byId["hdr-bell"].hidden) errors.push("záhlaví: zvonek");
    }
    if (byId["view-dash"].hidden || !byId["view-activity"].hidden) errors.push("navigace: výchozí pohled");
    await byId["nav-activity"].fire("click");
    if (byId["view-activity"].hidden || !byId["view-dash"].hidden || byId["nav-activity"].className !== "navbtn active") errors.push("navigace: přepnutí na aktivitu");
    if (byId["nav-activity"].children[1].textContent !== "6") errors.push("navigace: odznak aktivity " + byId["nav-activity"].children[1].textContent);
    // výchozí podzáložka aktivity je časová osa: graf, dny, řádky; hledání a přechod na alerty
    const tlTick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
    await tlTick();
    if (byId["tl-wrap"].hidden || !byId["alerts-wrap"].hidden) errors.push("aktivita: výchozí podzáložka");
    if (byId["tl-chart"].children.length !== 48) errors.push("časová osa: graf " + byId["tl-chart"].children.length);
    {
      const rows = byId["tl-list"].children.filter((x) => x.className.startsWith("tl-row"));
      const days = byId["tl-list"].children.filter((x) => x.tagName === "H4");
      if (rows.length !== 9 || days.length !== 2) errors.push(`časová osa: ${rows.length} řádků, ${days.length} dnů`);
      if (!rows[4].className.includes("tl-s-critical") || !rows[4].textContent.includes("neozývá")) errors.push("časová osa: alert");
      if (!rows[5].textContent.includes("tiskarna se odpojil")) errors.push("časová osa: klient " + rows[5].textContent);
      byId["tl-q"].value = "ether2"; await byId["tl-q"].fire("input");
      if (byId["tl-list"].children.filter((x) => x.className.startsWith("tl-row")).length !== 3) errors.push("časová osa: hledání");
      byId["tl-q"].value = ""; await byId["tl-q"].fire("input");
    }
    // Přehled: souhrn brány a linky, graf internetu, nej AP a klienti, co je pod dohledem
    {
      await byId["nav-dash"].fire("click"); await tlTick(); await tlTick(); await tlTick();
      const side = byId["dash-side"].textContent;
      if (!side.includes("gw-hala") || !side.includes("193.165.120.202") || !side.includes("99,65 %") || !side.includes("118.0 GB") || !side.includes("neodpovídá") || !side.includes("K řešení (3)") || !side.includes("ap-hala: zařízení se neozývá")) errors.push("přehled: panel " + side.slice(250));
      if (!side.includes("Linka lte1 není aktivní") || !side.includes("↓ 5.2 Mb/s")) errors.push("přehled: linka a provoz " + side.slice(0, 200));
      const svg = byId["dash-chart"].children[0];
      if (!svg || !svg.children.some((x) => x.attrs.class === "rx") || !svg.children.some((x) => x.attrs.class === "avail-down")) errors.push("přehled: graf");
      if (!byId["dash-aps"].textContent.includes("ap-kancelar") || !byId["dash-clients"].textContent.includes("pc-martina") || !byId["dash-watch"].textContent.includes("sw-hala · ether4")) errors.push("přehled: karty");
      if (!byId["dash-auth"].textContent.includes("97 %") || byId["dash-signal"].children.length !== 2) errors.push("přehled: Wi-Fi");
      if (byId["dash-range"].children.length !== 4 || byId["dash-layers"].children.length !== 3) errors.push("přehled: ovládání grafu");
      await byId["nav-map"].fire("click"); await tlTick();
      if (byId["view-map"].hidden || !byId["view-dash"].hidden) errors.push("navigace: mapa je samostatná sekce");
      // mapa: pohled Topologie ukáže klienty a filtry místo karet zařízení
      await byId["map-mode"].children[0].fire("click");
      const mf = byId["map-filters"];
      if (mf.hidden || !byId["infra-list"].hidden || !mf.textContent.includes("Drátoví (") || !mf.textContent.includes("Stav zařízení") || !mf.textContent.includes("Popisky")) errors.push("mapa: filtry topologie " + mf.textContent.slice(0, 200));
      const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      const leafs = () => all(byId["infra-svg"], (n) => n.attrs && n.attrs.class === "client").length;
      const before = leafs(), wired = all(mf, (n) => n.tagName === "INPUT")[2];
      wired.checked = false; await wired.fire("change");
      if (!before || leafs() >= before || !byId["map-filters"].textContent.includes("Zrušit filtry")) errors.push(`mapa: filtr drátových klientů ${before} → ${leafs()}`);
      await byId["map-mode"].children[1].fire("click");
      if (!byId["map-filters"].hidden || byId["infra-list"].hidden) errors.push("mapa: návrat na infrastrukturu");
    }
    // časová osa vedle mapy: značky událostí (shluknuté), souhrn
    {
      const evs = byId["itl-move"].children.filter((x) => x.className.startsWith("itl-ev"));
      if (!evs.length || !evs.some((x) => x.className.includes("tl-s-critical"))) errors.push("mapa: časová osa " + evs.length);
      if (!byId["itl-sum"].textContent.includes("9")) errors.push("mapa: souhrn osy " + byId["itl-sum"].textContent);
      // posun osy do minulosti: čas místo „TEĎ“, mapa dostane tehdejší stav a pruh s návratem
      await byId["itl-axis"].fire("wheel", { deltaY: 400 });
      await ctx.itlLoadState(); await tlTick();      // časovač v testu neběží – načtení stavu se zavolá přímo
      if (byId["itl-now"].textContent.includes("TEĎ") || !requests.some((r) => r.includes("/infra-at?at=2026-11-21T07%3A56%3A00.000Z"))) errors.push("mapa: posun osy " + byId["itl-now"].textContent + " " + requests.filter((r) => r.includes("infra-at")).join(","));
      if (!byId["infra-warn"].textContent.includes("Stav k") || !byId["infra-warn"].textContent.includes("1 zařízení offline")) errors.push("mapa: stav v minulosti " + byId["infra-warn"].textContent);
      {
        // cestování v čase: mapa kreslí tehdejší snímek (ap-hala tam ještě nebyl), po návratu zase živý stav
        const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
        const devs = () => all(byId["infra-svg"], (n) => n.attrs && n.attrs["data-dev"]).map((n) => n.attrs["data-dev"]).sort().join(",");
        if (!byId["infra-warn"].textContent.includes("jsou z té doby") || devs() !== "gw,lost,sw") errors.push("mapa: tehdejší topologie " + devs() + " " + byId["infra-warn"].textContent.slice(0, 160));
        if (!byId["infra-list"].textContent.includes("core-gw") || byId["infra-list"].textContent.includes("ap-hala")) errors.push("mapa: seznam zařízení v minulosti");
        await byId["itl-now"].fire("click"); await tlTick();
        if (devs() !== "ap,gw,lost,sw") errors.push("mapa: návrat živé topologie " + devs());
      }
      if (!byId["itl-now"].textContent.includes("TEĎ") || byId["infra-warn"].textContent.includes("Stav k")) errors.push("mapa: návrat na teď");
    }
    // dostupnost: řádek na zařízení, 30 sloupečků dnů, trvající výpadek
    await byId["avail-btn"].fire("click"); await tlTick();
    {
      const rows = byId["avail-rows"].children;
      if (byId["avail-wrap"].hidden || rows.length !== 2) errors.push("dostupnost: řádky " + rows.length);
      else {
        const strip = rows[1].children[1].children[0];
        if (strip.children.length !== 30 || strip.children[0].className !== "av-none" || strip.children[29].className !== "av-bad")
          errors.push("dostupnost: proužek dnů");
        if (!rows[1].children[2].textContent.includes("99.64") || !rows[1].textContent.includes("trvá")) errors.push("dostupnost: hodnoty " + rows[1].textContent);
        if (!rows[0].children[2].textContent.startsWith("100 ")) errors.push("dostupnost: 100 %");
      }
    }
    await byId["alerts-btn"].fire("click"); await tlTick();
    if (byId["alerts-wrap"].hidden || !byId["plans-wrap"].hidden) errors.push("aktivita: podzáložka alerty");
    await byId["plans-btn"].fire("click");
    if (byId["plans-wrap"].hidden || !byId["alerts-wrap"].hidden) errors.push("aktivita: přepnutí na změny");
    await byId["alerts-btn"].fire("click");
    if (byId["nav-site"].hidden) errors.push("navigace: výběr lokality u sekce lokality");
    await byId["nav-tenant"].fire("click");
    if (!byId["nav-site"].hidden || byId["view-tenant"].hidden) errors.push("navigace: celý tenant bez výběru lokality");
    await byId["nav-dash"].fire("click");
    if (byId["system-card"].hidden || byId["system-steps"].children.length !== 3) errors.push("stav systému");
    if (byId["users-card"].hidden || byId["users"].children.length !== 2) errors.push("uživatelé: čekány 2 řádky");
    const tiles = byId["overview"].children.length;
    if (tiles < 7) errors.push("přehled: čekáno aspoň 7 dlaždic, je " + tiles);
    if (byId["overview"].children[0].textContent !== "2Online") errors.push("přehled: " + byId["overview"].children[0].textContent);
    // proklik z dlaždic přehledu: seznam zařízení a problémů s akcemi
    {
      const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      const tile = (label) => byId["overview"].children.find((t) => t.textContent.includes(label));
      await tile("Varování").fire("click"); await tick();
      if (byId["ov-drill"].hidden || !byId["ov-drill"].textContent.includes("Potvrdit")) errors.push("přehled: proklik varování " + byId["ov-drill"].textContent.slice(0, 120));
      await tile("Dostupná aktualizace").fire("click"); await tick();
      if (!find(byId["ov-drill"], (n) => n.tagName === "BUTTON" && n.textContent.startsWith("Aktualizovat →"))) errors.push("přehled: proklik aktualizace");
      const n0 = requests.length;
      await tile("Bezpečnostní nálezy").fire("click"); await tick();
      const fixAll = find(byId["ov-drill"], (n) => n.tagName === "BUTTON" && n.textContent === "Opravit vše");
      if (!fixAll || !byId["ov-drill"].textContent.includes("Kopírovat příkaz")) errors.push("přehled: bezpečnostní nálezy " + byId["ov-drill"].textContent.slice(0, 160));
      else {
        const p = fixAll.fire("click"); await tick();
        if (byId["ask"].hidden) errors.push("přehled: dialog opravy");
        else { byId["ask-ok"].onclick(); await p; await tick(); }
        if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/hardening-fix"))) errors.push("přehled: oprava nálezů");
      }
      await tile("Bezpečnostní nálezy").fire("click"); await tick();
      if (!byId["ov-drill"].hidden) errors.push("přehled: druhý klik má proklik zavřít");
    }
    const alertRows = byId["alerts"].children.length;
    const plansN = byId["plans"].children.length;
    // lokalita: topologie a klienti
    await byId["topo-btn"].fire("click");
    if (byId["topo-wrap"].hidden || !byId["infra-wrap"].hidden) errors.push("přehled: podzáložka topologie");
    // bezdrátové spoje: seznam se stavem, karta spoje s pásy grafu, štítky odchylek a seznam úseků
    // Zařízení → Nespravované: nový nález se stavem, schválení, hlídání úseku pingem
    await byId["nav-devices"].fire("click");
    await byId["unm-btn"].fire("click");
    await tlTick();
    {
      const t = byId["unm-main"].textContent;
      if (byId["unm-wrap"].hidden || !t.includes("Nespravované switche") || !t.includes("sw-hala ether7") || !t.includes("3 klientů + spravované zařízení") || !t.includes("Nový") || !t.includes("Výchozí stav") || !t.includes("Schválit") || !t.includes("Hlídat pingem") || !t.includes("Zatím se nehlídá žádný úsek")) errors.push("nespravované: tabulka " + t.slice(-300));
      if (!byId["unm-side"].textContent.includes("Z toho nových1")) errors.push("nespravované: počet nových");
    }
    await byId["nav-links"].fire("click");
    await tlTick();
    {
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      const main = byId["wl-main"], row = find(main, (n) => n.tagName === "TR" && n.className === "clickable");
      if (byId["view-links"].hidden || !row) errors.push("spoje: seznam");
      else {
        const txt = row.textContent;
        if (!txt.includes("spoj-hala ↔ spoj-sklad") || !txt.includes("Zhoršený · 48") || !txt.includes("−71 / −69 dBm") || !txt.includes("4,0 %")) errors.push("spoje: řádek " + txt);
        if (!row.title.includes("signál −71 dBm, obvykle −58 dBm")) errors.push("spoje: důvody " + row.title);
        await row.fire("click"); await tlTick(); await tlTick();
        const side = byId["wl-side"].textContent, m = byId["wl-main"];
        if (!side.includes("Všechny spoje (1)") || !side.includes("obvykle −58 / −60") || !side.includes("ping 10× na 10.9.0.21") || !side.includes("Nehlásit upozornění")) errors.push("spoje: panel " + side);
        const chips = all(m, (n) => String(n.className).startsWith("wl-chip "));
        if (chips.length !== 5 || !chips.some((c) => c.textContent.includes("ztrátovost 4,0 %") && c.className.includes("warn"))) errors.push("spoje: štítky " + chips.map((c) => c.textContent));
        const svg = find(m, (n) => n.tagName === "SVG");
        const titles = svg ? all(svg, (n) => n.className === "wl-bt" || (n.attrs && n.attrs.class === "wl-bt")).map((n) => n.textContent) : [];
        if (!svg || !["Provoz", "Signál", "Rychlost spoje", "Ztrátovost a odezva", "CCQ", "Šum na kanálu"].every((t) => titles.includes(t))) errors.push("spoje: pásy grafu " + titles);
        if (svg && all(svg, (n) => String(n.attrs && n.attrs.class).startsWith("wl-ep-bg")).length !== 1) errors.push("spoje: podbarvení odchylky");
        const eps = all(m, (n) => n.className === "wl-ep");
        if (eps.length !== 1 || !eps[0].textContent.includes("propad signálu, pád rychlosti, ztrátovost") || !eps[0].textContent.includes("trvalo 20 min")) errors.push("spoje: úseky " + eps.map((e) => e.textContent));
        if (!requests.some((r) => r.includes("/traffic?hours=24&interface=wlan1"))) errors.push("spoje: dotaz na provoz");
        await tlTick();
        if (!byId["wl-side"].textContent.includes("↓ 84") || !find(byId["wl-side"], (n) => n.tagName === "BUTTON" && n.textContent === "Změřit propustnost")) errors.push("spoje: propustnost " + byId["wl-side"].textContent);
        const back = find(byId["wl-side"], (n) => n.tagName === "BUTTON" && n.textContent.includes("Všechny spoje"));
        await back.fire("click");
        if (!find(byId["wl-main"], (n) => n.tagName === "TABLE")) errors.push("spoje: návrat na seznam");
      }
    }
    // mapa: bezdrátový spoj je čára v barvě stavu a klik otevře kartu spoje
    await byId["nav-dash"].fire("click");
    await tlTick(); await tlTick();
    {
      const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      const cls = (n) => String((n.attrs && n.attrs.class) || "");
      const wless = all(byId["infra-svg"], (n) => cls(n).includes("wless")), hit = all(byId["infra-svg"], (n) => cls(n) === "link hit");
      if (wless.length !== 1 || !cls(wless[0]).includes("s-degraded") || hit.length !== 1) errors.push(`mapa: bezdrátový spoj ${wless.map(cls)} / ${hit.length}`);
      else {
        if (!hit[0].textContent.includes("spoj-hala ↔ spoj-sklad") || !hit[0].textContent.includes("Zhoršený · 48")) errors.push("mapa: popisek spoje " + hit[0].textContent);
        await hit[0].fire("click", { stopPropagation() {} }); await tlTick(); await tlTick();
        if (byId["view-links"].hidden || !byId["wl-side"].textContent.includes("Všechny spoje (1)")) errors.push("mapa: klik na spoj neotevřel kartu");
      }
    }
    // zálohy zařízení: pravidla zákazníka, výběr zařízení, verze a porovnání s aktuální konfigurací
    await byId["nav-settings"].fire("click");
    await byId["devbk-btn"].fire("click");
    await tlTick(); await tlTick();
    {
      const rows = byId["dbk-list"].children;
      if (byId["devbk-wrap"].hidden || rows.length !== 3) errors.push("zálohy zařízení: seznam " + rows.length);
      else {
        if (!byId["dbk-custom"].checked || byId["dbk-keep"].value != 14 || !byId["dbk-changes"].checked) errors.push("zálohy zařízení: pravidla");
        if (!byId["dbk-sum"].textContent.includes("nejvýš 26 verzí (14 posledních + 12 měsíčních)") || !byId["dbk-sum"].textContent.includes("seznam změn")) errors.push("zálohy zařízení: souhrn " + byId["dbk-sum"].textContent);
        if (byId["dbk-count"].textContent !== "2 z 3 se zálohuje" || byId["dbk-all"].checked) errors.push("zálohy zařízení: počet " + byId["dbk-count"].textContent);
        if (!rows[0].textContent.includes("řízené") || !rows[1].textContent.includes("sledované") || !rows[1].textContent.includes("chyba: export soubor") || !rows[2].textContent.includes("nezálohuje se")) errors.push("zálohy zařízení: řádky " + rows[1].textContent);
        const n0 = requests.length, cb = rows[2].children[0].children[0];
        cb.checked = true; await cb.fire("change"); await tlTick();
        if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/device-backups"))) errors.push("zálohy zařízení: zaškrtnutí neodeslalo výběr");
        byId["dbk-follow"].checked = true; byId["dbk-custom"].checked = false; await byId["dbk-follow"].fire("change");
        if (!byId["dbk-keep"].disabled || !byId["dbk-sum"].textContent.includes("jako záloha controlleru (02:30)")) errors.push("zálohy zařízení: režim podle controlleru " + byId["dbk-sum"].textContent);
        const btn = byId["dbk-list"].children[0].children[7].children[0].children[0];
        await btn.fire("click"); await tlTick();
        const vr = byId["dbv-list"].children;
        if (byId["dbv-dlg"].hidden || vr.length !== 3 || !vr[0].textContent.includes("aktuální") || vr[0].textContent.includes("Porovnat")) errors.push("zálohy zařízení: verze " + vr.length);
        else {
          await vr[1].children[3].children[0].children[0].fire("click"); await tlTick();
          if (byId["dbv-diff"].hidden || !byId["dbv-diff"].textContent.includes("+++ vybraná verze") || vr[1].className !== "selected") errors.push("zálohy zařízení: porovnání");
        }
        await byId["dbv-close"].fire("click");
      }
    }
    // sekce Porty: schéma, filtry, tabulka s připojeným zařízením/klientem, přehled VLAN
    await byId["nav-ports"].fire("click");
    await tlTick();
    {
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      const tilesOf = () => { const t = []; find(byId["pm-diagram"], (n) => { if (n.tagName === "BUTTON" && n.className.startsWith("pm-tile")) t.push(n); return false; }); return t; };
      const tiles = tilesOf(), rows = byId["pm-body"].children;
      if (byId["view-ports"].hidden || tiles.length !== 3 || rows.length !== 3) errors.push(`porty: schéma ${tiles.length}, řádků ${rows.length}`);
      {
        // popisek u portu: ikonka, celé jméno a IP po najetí, klik otevře zařízení / kartu klienta
        const who = []; find(byId["pm-diagram"], (n) => { if (n.tagName === "BUTTON" && n.className.includes("pm-who")) who.push(n); return false; });
        if (who.length !== 2 || !who[0].title.includes("AP-1 · ether1") || !who[1].title.includes("tiskarna · 10.0.10.21 · AA:BB:CC:00:00:01") || who[1].children.length !== 2) errors.push("porty: popisky s ikonkou " + who.map((w) => w.title));
        else { byId["cl-dlg"].hidden = true; await who[1].fire("click"); if (byId["cl-dlg"].hidden || !byId["cl-title"].textContent.includes("tiskarna")) errors.push("porty: proklik na klienta"); byId["cl-dlg"].hidden = true; }
      }
      // upozornění nad schématem (port s chybami) a časová osa jen u vybraného zařízení
      if (!byId["pm-banners"].textContent.includes("ether3: 1310 chyb za 24 h") || !byId["pm-tl"].hidden) errors.push("porty: upozornění / osa " + byId["pm-banners"].textContent);
      else {
        if (!tiles[1].className.includes("s-1g") || !tiles[2].className.includes("s-100")) errors.push("porty: barva podle rychlosti " + tiles.map((t) => t.className));
        if (!rows[1].children[4].textContent.includes("AP-1") || !rows[1].children[4].textContent.includes("ether1")) errors.push("porty: protější port " + rows[1].children[4].textContent);
        if (rows[2].children[4].textContent !== "tiskarna" || rows[2].children[8].textContent !== "AA:BB:CC:00:00:01") errors.push("porty: klient na portu");
        if (rows[1].children[11].textContent !== "Sprava (20)") errors.push("porty: nativní VLAN " + rows[1].children[11].textContent);
        // zdraví portu: značka na dlaždici, sloupec chyb, filtr, detail s důvody a hodinovým průběhem
        if (!tiles[2].className.includes("h-warning") || tiles[1].className.includes("h-")) errors.push("porty: značka chyb " + tiles.map((t) => t.className));
        if (!rows[2].children[7].textContent.includes("1310 · FCS") || rows[1].children[7].textContent !== "–") errors.push("porty: sloupec chyb " + rows[2].children[7].textContent);
        if (!tiles[2].title.includes("240 chyb za poslední hodinu") || !tiles[2].title.includes("kabel, konektor nebo rušení")) errors.push("porty: důvod " + tiles[2].title);
        const groups = byId["pm-facets"].children.map((x) => x.children[0].textContent);
        if (!groups.includes("Zdraví portu") || !groups.includes("Rychlost linky") || !groups.includes("PoE") || !groups.includes("Nativní VLAN")) errors.push("porty: filtry " + groups);
        await tiles[2].fire("click");
        if (byId["pm-body"].children.length !== 1) errors.push("porty: výběr portu");
        await tlTick();
        const det = byId["pm-port"];
        if (det.hidden || !det.textContent.includes("Varování") || !det.textContent.includes("1250 FCS")) errors.push("porty: detail chyb " + det.textContent);
        const bars = find(det, (n) => n.className === "pm-bars");
        if (!bars || bars.children.length !== 48 || bars.children.filter((b) => b.className === "h-warning").length !== 2) errors.push("porty: průběh chyb");
        if (!requests.some((r) => r.includes("/port-errors?hours=48&interface=ether3"))) errors.push("porty: dotaz na chyby portu");
        await tilesOf()[2].fire("click");
        byId["pm-o-vlan"].checked = true; await byId["pm-o-vlan"].fire("change");
        if (byId["pm-vlan"].hidden || !byId["pm-vlan"].textContent.includes("LAN (10)")) errors.push("porty: přehled VLAN");
        byId["pm-o-vlan"].checked = false; await byId["pm-o-vlan"].fire("change");
      }
    }
    await byId["nav-clients"].fire("click"); await byId["cll-btn"].fire("click");
    if (byId["view-clients"].hidden) errors.push("klienti: sekce");
    await byId["nav-settings"].fire("click");
    await byId["wifi-btn"].fire("click");
    if (byId["wifiset-wrap"].hidden || !byId["net-wrap"].hidden) errors.push("nastavení: podzáložka Wi-Fi");
    // Wi-Fi: roaming lokality a časová osa klienta
    {
      const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      await tick();
      if (!byId["capsman-card"].textContent.includes("CAPsMAN lokality: sw")) errors.push("wifi: karta CAPsMAN " + byId["capsman-card"].textContent.slice(0, 80));
      {
        const n0 = requests.length;
        const pc = ctx.__t.setCapsmanHost(api.sw); await tick();
        if (byId["ask"].hidden) errors.push("wifi: dialog CAPsMAN");
        else { byId["ask-ok"].onclick(); await pc; await tick(); }
        if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.endsWith("/intent"))) errors.push("wifi: nastavení CAPsMAN");
      }
      const rows = byId["roam-clients"].children;
      if (rows.length !== 2 || !rows[0].textContent.includes("ntb-novak") || !rows[0].textContent.includes("přeskakuje"))
        errors.push("roaming: klienti " + byId["roam-clients"].textContent.slice(0, 160));
      if (byId["roam-matrix"].children[0].children.length !== 2 || !byId["roam-matrix"].textContent.includes("AP-chodba wifi1"))
        errors.push("roaming: matice " + byId["roam-matrix"].textContent.slice(0, 120));
      if (!find(byId["roam-banner"], (n) => n.tagName === "BUTTON" && n.textContent.includes("Zapnout Wi-Fi události"))) errors.push("roaming: zapnutí událostí");
      await rows[0].fire("click"); await tick();
      if (byId["cl-dlg"].hidden || byId["clp-jr"].hidden || byId["cl-title"].textContent !== "ntb-novak") errors.push("časová osa: panel klienta " + byId["cl-title"].textContent);
      if (byId["jr-findings"].children.length < 3) errors.push("časová osa: nálezy " + byId["jr-findings"].textContent.slice(0, 120));
      const segs = []; find(byId["jr-svg"], (n) => { if (n.attrs && /^jr-seg/.test(n.attrs.class || "")) segs.push(n); return false; });
      if (segs.length < 4 || !find(byId["jr-svg"], (n) => n.attrs && /jr-roam mk-ft/.test(n.attrs.class || "")) ||
          !find(byId["jr-svg"], (n) => n.attrs && /mk-dis/.test(n.attrs.class || ""))) errors.push("časová osa: graf " + segs.length);
      if (byId["jr-events"].children.length < 5) errors.push("časová osa: události");
      await byId["clt-edit"].fire("click");
      if (byId["clp-edit"].hidden || !byId["clp-jr"].hidden) errors.push("panel klienta: záložka nastavení");
      byId["cl-dlg"].hidden = true;
    }
    await byId["nav-dash"].fire("click");
    await byId["stp-btn"].fire("click");
    if (byId["stp-wrap"].hidden) errors.push("přehled: podzáložka STP");
    // sítě a DHCP snooping
    await byId["nav-settings"].fire("click");
    await byId["setov-btn"].fire("click"); await tlTick(); await tlTick();
    {
      // nastavení: levé menu, Přehled s bloky Wi-Fi, Sítě a Internet; Internet má linky a porty brány
      const ov = byId["setov-body"].textContent;
      if (byId["setov-wrap"].hidden || byId["setov-btn"].className !== "active" || byId["setov-body"].children.length !== 3) errors.push("nastavení: přehled " + byId["setov-body"].children.length);
      if (!ov.includes("Firma") || !ov.includes("1 / 100") || !ov.includes("192.168.10.0/24") || !ov.includes("ether1 · O2") || !ov.includes("99,65 %")) errors.push("nastavení: obsah přehledu " + ov.slice(0, 300));
      await byId["inet-btn"].fire("click"); await tlTick(); await tlTick(); await tlTick();
      const it = byId["inet-body"].textContent;
      if (byId["inet-wrap"].hidden || !byId["setov-wrap"].hidden || !it.includes("193.165.120.1") || !it.includes("118.0 GB") || !it.includes("záloha / neaktivní") || !it.includes("Porty brány gw-hala")) errors.push("nastavení: internet " + it.slice(0, 300));
    }
    {
      // nastavení → firewall: zóny, matice a pravidla brány; klik na buňku vyfiltruje pravidla
      await byId["setfw-btn"].fire("click"); await tlTick(); await tlTick(); await tlTick();
      const fb = byId["setfw-body"], all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      const cells = all(fb, (n) => n.tagName === "BUTTON" && n.className.startsWith("fwz-c"));
      if (byId["setfw-wrap"].hidden || !fb.textContent.includes("Matice zón") || !fb.textContent.includes("Všechna pravidla (") || cells.length < 4) errors.push("nastavení: firewall " + cells.length + " " + fb.textContent.slice(0, 200));
      else {
        // obecná pravidla se do buněk nepočítají, vypnutá pravidla jsou označená a jdou odfiltrovat
        const txt = () => byId["setfw-body"].textContent;
        if (!txt().includes("Obecná pravidla (4)") || !txt().includes("bez adresy") || !txt().includes("vypnuté") || !txt().includes("Vypnutá (2)")) errors.push("nastavení: firewall – obecná / vypnutá " + txt().slice(0, 300));
        const offRows = () => all(byId["setfw-body"], (n) => n.tagName === "TR" && n.className === "fw-off").length;
        const segOn = all(byId["setfw-body"], (n) => n.tagName === "BUTTON" && n.textContent.startsWith("Zapnutá"))[0];
        if (offRows() !== 2 || !segOn) errors.push("nastavení: firewall – řádky vypnutých " + offRows());
        else { await segOn.fire("click"); if (offRows() !== 0) errors.push("nastavení: firewall – filtr zapnutých"); await all(byId["setfw-body"], (n) => n.tagName === "BUTTON" && n.textContent === "Vše")[0].fire("click"); }
        await all(byId["setfw-body"], (n) => n.tagName === "BUTTON" && n.className.startsWith("fwz-c"))[1].fire("click");
        if (!byId["setfw-body"].textContent.includes("Zobrazit všechna pravidla") || byId["setfw-body"].textContent.includes("Všechna pravidla (")) errors.push("nastavení: firewall – pravidla buňky");
      }
    }
    {
      // přehled → nejvytíženější klient: otevře kartu klienta na záložce Provoz s grafem portu
      const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      ctx.showView("dash"); await tlTick(); await tlTick();
      const b = all(byId["dash-clients"], (n) => n.tagName === "BUTTON")[0];
      if (!b) errors.push("přehled: klient není klikací");
      else {
        await b.fire("click"); await tlTick(); await tlTick(); await tlTick();
        const t = byId["cl-tr"].textContent;
        if (byId["cl-dlg"].hidden || byId["clp-tr"].hidden || !byId["clp-edit"].hidden || !t.includes("Port ether2") || !t.includes("Staženo")) errors.push("klient: provoz " + t.slice(0, 200));
        if (all(byId["cl-tr"], (n) => n.className === "fl-col").length !== 24) errors.push("klient: graf po hodinách " + all(byId["cl-tr"], (n) => n.className === "fl-col").length);
        byId["cl-dlg"].hidden = true;
      }
    }
    {
      // aktivita → provoz: součty, klienti, služby, protistrany; klik na klienta filtruje; vypnutí sběru se ptá
      const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      ctx.openSub("activity", "flow"); await tlTick(); await tlTick();
      const txt = () => byId["flow-body"].textContent;
      if (byId["flow-wrap"].hidden || !txt().includes("Nejaktivnější klienti") || !txt().includes("pc-martina") || !txt().includes("QUIC") || !txt().includes("gw-hala · sbírá")) errors.push("provoz: přehled " + txt().slice(0, 200));
      if (all(byId["flow-body"], (n) => n.className === "fl-col").length !== 24 || !txt().includes("Kam provoz směřuje") || !txt().includes("uchovávají se 30 dní")) errors.push("provoz: graf / protistrany");
      await all(byId["flow-body"], (n) => n.tagName === "BUTTON" && n.textContent === "pc-martina")[0].fire("click"); await tlTick(); await tlTick();
      if (!txt().includes("Klient 192.168.10.21") || txt().includes("Nejaktivnější klienti") || txt().includes("SSH")) errors.push("provoz: filtr klienta " + txt().slice(0, 200));
      await all(byId["flow-body"], (n) => n.tagName === "BUTTON" && n.textContent.startsWith("×"))[0].fire("click"); await tlTick(); await tlTick();
      if (!txt().includes("Nejaktivnější klienti")) errors.push("provoz: zrušení filtru");
      const n0 = requests.length;
      const pf = all(byId["flow-body"], (n) => n.tagName === "BUTTON" && n.textContent === "Vypnout")[0].fire("click"); await tlTick();
      if (byId["ask"].hidden) errors.push("provoz: dialog vypnutí"); else { byId["ask-ok"].onclick(); await pf; await tlTick(); }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/devices/gw/flow"))) errors.push("provoz: vypnutí sběru " + requests.slice(n0).join(" "));
      ctx.showView("settings"); await tlTick();
    }
    {
      // nastavení → zabezpečení: skóre lokality, oblasti, nálezy zařízení, filtr závažnosti a oprava jedním klikem
      await byId["setsec-btn"].fire("click"); await tlTick(); await tlTick();
      const sb = byId["setsec-body"], all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
      const txt = () => byId["setsec-body"].textContent;
      const score = all(sb, (n) => (n.className || "").startsWith("sec-score"))[0];
      if (byId["setsec-wrap"].hidden || !score || score.textContent !== "49" || score.className !== "sec-score critical") errors.push("zabezpečení: skóre " + txt().slice(0, 200));
      if (!txt().includes("Vážné nedostatky") || !txt().includes("1 kritických · 5 varování · 3 informací") || !txt().includes("1 zařízení bez inventury")) errors.push("zabezpečení: souhrn");
      if (all(sb, (n) => (n.className || "").startsWith("sec-cat ")).length !== 6 || !txt().includes("Vystavení do internetu")) errors.push("zabezpečení: oblasti");
      if (!txt().includes("jana@example.cz") || !txt().includes("Router je otevřený z internetu".slice(0, 0) + "input chain nic nezahazuje") || !txt().includes("1 zařízení bez nálezů")) errors.push("zabezpečení: nálezy");
      if (!txt().includes("Opravit vše automaticky (2)") || !txt().includes("není spravované přes API")) errors.push("zabezpečení: oprava / nezkontrolované");
      await all(byId["setsec-body"], (n) => n.tagName === "BUTTON" && n.textContent === "Kritické")[0].fire("click");
      if (txt().includes("UPnP") || !txt().includes("input chain") || txt().includes("jana@example.cz")) errors.push("zabezpečení: filtr kritických");
      await all(byId["setsec-body"], (n) => n.tagName === "BUTTON" && n.textContent === "Vše")[0].fire("click");
      const n0 = requests.length;
      const pf = all(byId["setsec-body"], (n) => n.tagName === "BUTTON" && n.textContent === "vypnout Telnet")[0].fire("click"); await tlTick();
      if (byId["ask"].hidden) errors.push("zabezpečení: dialog opravy"); else { byId["ask-ok"].onclick(); await pf; await tlTick(); }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/devices/gw/hardening-fix"))) errors.push("zabezpečení: oprava nálezu " + requests.slice(n0).join(" "));
    }
    await byId["net-btn"].fire("click"); await tlTick(); await tlTick();
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
    if (byId["maint-bar"].hidden || !byId["maint-bar"].textContent.includes("výměna switche")) errors.push("servisní okno: lišta");
    // optimalizace kanálů: karta s tlačítkem, dialog s návrhem
    if (!byId["rfopt-card"].textContent.includes("Optimalizovat kanály")) errors.push("rfopt: karta");
    else {
      const ob = byId["rfopt-card"].children[0].children[1];
      if (ob && ob.onclick) ob.onclick();
      if (byId["rfopt-dlg"].hidden || !byId["rfopt-rows"].textContent.includes("100 / 80 MHz · DFS") ||
          !byId["rfopt-sum"].textContent.includes("6.4 → 0.4")) errors.push("rfopt: dialog");
      byId["rfopt-dlg"].hidden = true;
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
      await byId["sys-btn"].fire("click"); await tick();
      if (byId["sys-wrap"].hidden) errors.push("nastavení: Systém a nasazení");
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
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["upg-jobs"].children.length !== 2) errors.push("aktualizace lokality: úlohy");
    if (!byId["upg-start"].hidden) errors.push("aktualizace lokality: spuštění musí být skryté, když úloha běží");
    if (!byId["net-wrap"].hidden) errors.push("záložky lokality: po přepnutí má být předchozí panel skrytý");
    await byId["nav-dash"].fire("click");
    await byId["infra-btn"].fire("click");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    if (byId["infra-list"].children.length !== 4) errors.push("infrastruktura: seznam zařízení " + byId["infra-list"].children.length);
    const ig = byId["infra-svg"].children;
    if (ig.length !== 2 || ig[1].children.length < 6) errors.push("infrastruktura: strom se nevykreslil");
    byId["infra-ext"].checked = true;
    await byId["infra-ext"].fire("change");
    for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r));
    // infrastruktura: klienti s MAC u zařízení (seznam i strom)
    {
      const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      const card = byId["infra-list"].children[0];
      if (!card.textContent.includes("👥 2 klienti") || !card.textContent.includes("B0:BE:76:12:34:56")) errors.push("infrastruktura: klienti v seznamu " + card.textContent.slice(0, 200));
      const before = JSON.stringify(byId["infra-svg"].children[1].children.length);
      byId["infra-clients"].checked = true;
      await byId["infra-clients"].fire("change"); await tick();
      const pill = find(byId["infra-svg"], (n) => n.attrs && n.attrs.class === "client");
      if (!pill || !pill.textContent.includes("NAS kancelář") || !find(byId["infra-svg"], (n) => n.tagName === "IMAGE"))
        errors.push("infrastruktura: klienti ve stromu " + before);
      // klik na klienta → editor klienta
      await pill.fire("click");
      if (byId["cl-dlg"].hidden || byId["cl-label"].value !== "NAS kancelář") errors.push("infrastruktura: editor klienta");
      byId["cl-dlg"].hidden = true;
    }
    // klienti: avatar, editor (popisek, ikona), statická IP a nastavení sítě DHCP
    {
      const tick = async () => { for (let i = 0; i < 10; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      await byId["nav-clients"].fire("click"); await tick(); await tick();
      {
        // klienti → rozbor připojení: výchozí filtr Online + Wi-Fi, první klient se vybere a načte se jeho rozbor
        const all = (n, pred, out = []) => { if (pred(n)) out.push(n); for (const x of n.children || []) if (x instanceof El) all(x, pred, out); return out; };
        await byId["clv-btn"].fire("click"); await tlTick(); await tlTick(); await tlTick(); await tlTick();
        const main = () => byId["cv-main"].textContent;
        if (byId["clv-wrap"].hidden || !byId["cl-ux"].hidden || byId["clv-btn"].className !== "active") errors.push("rozbor: výchozí pohled klientů");
        if (byId["cv-list"].children.length !== 1 || !byId["cv-list"].textContent.includes("A55-Martina") || !byId["cv-count"].textContent.startsWith("1 ")) errors.push("rozbor: seznam " + byId["cv-list"].textContent);
        if (!byId["cv-facets"].textContent.includes("Online (1)") || !byId["cv-facets"].textContent.includes("Wi-Fi (1)") || !byId["cv-facets"].textContent.includes("ap-kancelar (1)")) errors.push("rozbor: filtry " + byId["cv-facets"].textContent);
        if (!main().includes("ap-kancelar") || !main().includes("sw-hala") || !main().includes("Internet") || !main().includes("dostupnost 100 %")) errors.push("rozbor: cesta " + main().slice(0, 200));
        if (!main().includes("Kanál 36 · 5 GHz · 40 MHz") || !main().includes("Signál -56 dBm") || !main().includes("Přeneseno 24 h")) errors.push("rozbor: klient " + main().slice(0, 400));
        if (!main().includes("AP ap-kancelar") || !main().includes("Klientů 6") || !main().includes("Cizích sítí na kanálu 2") || !main().includes("ap-hala wifi1")) errors.push("rozbor: AP");
        if (!main().includes("Odezva 8 ms") || !main().includes("Ztrátovost 0 %")) errors.push("rozbor: lokalita");
        if (!main().includes("Přechod z ap-hala wifi1 (2,4 GHz) na ap-kancelar wifi2 (5 GHz)") || !main().includes("rychlý přechod (802.11r)") || !main().includes("Odpojeno od ap-hala wifi1") || !main().includes("connection lost")) errors.push("rozbor: záznam " + main().slice(-400));
        const charts = all(byId["cv-main"], (n) => n.tagName === "SVG" && (n.attrs.class || "") === "cv-chart");
        if (charts.length !== 3 || !all(charts[0], (n) => n.tagName === "CIRCLE").length) errors.push("rozbor: grafy a značky " + charts.length);
        if (!all(byId["cv-tl"], (n) => n.tagName === "CIRCLE").length || !byId["cv-tl"].textContent.includes("TEĎ")) errors.push("rozbor: časová osa");
        // kabel: jiný klient, panel s portem; zrušení filtrů ukáže všechny
        await byId["cv-clear"].fire("click"); await tlTick(); await tlTick();
        if (byId["cv-list"].children.length !== 3) errors.push("rozbor: zrušení filtrů " + byId["cv-list"].children.length);
        await all(byId["cv-main"], (n) => n.tagName === "BUTTON" && n.textContent === "Provoz (NetFlow)")[0].fire("click");
        if (!main().includes("Sběr provozu (NetFlow) zapneš")) errors.push("rozbor: záložka provoz");
        await byId["cll-btn"].fire("click"); await tick();
        if (byId["cl-ux"].hidden || !byId["clv-wrap"].hidden) errors.push("rozbor: přepnutí na seznam");
      }
      const rows = byId["clients"].children;
      if (rows.length !== 2 || rows[0].children[0].children[0].textContent !== "🗄️" || !rows[1].textContent.includes("TP-LINK"))
        errors.push("klienti: avatar " + rows.length + " " + (rows[0] && rows[0].children[0].textContent));
      if (!find(rows[1], (n) => n.tagName === "IMG")) errors.push("klienti: logo výrobce");
      // časová osa a filtry: posuvník na konci = teď, typ připojení filtruje řádky
      if (byId["cl-slider"].max !== "96" || !byId["cl-time-sum"].textContent.includes("nejvíc za období")) errors.push("klienti: časová osa " + byId["cl-time-sum"].textContent);
      if (!/^2 · \d Wi-Fi · \d kabel$/.test(byId["cl-count"].textContent)) errors.push("klienti: počty " + byId["cl-count"].textContent);
      const nWifi = Number(byId["cl-count"].textContent.split(" · ")[1].split(" ")[0]);
      await byId["cl-kind-wifi"].fire("click");
      if (byId["clients"].children.length !== Math.max(1, nWifi) || byId["cl-kind-wifi"].className !== "active") errors.push("klienti: filtr Wi-Fi");
      await byId["cl-kind-all"].fire("click");
      byId["cl-slider"].value = "40"; await byId["cl-slider"].fire("input");
      if (byId["cl-now"].hidden || !byId["cl-time-sum"].textContent.startsWith("připojeno")) errors.push("klienti: posun v čase " + byId["cl-time-sum"].textContent);
      await byId["cl-now"].fire("click");
      if (!byId["cl-now"].hidden || byId["cl-time-label"].textContent !== "Teď") errors.push("klienti: zpět na teď");
      await rows[1].fire("click", { target: rows[1] });
      if (byId["cl-dlg"].hidden || byId["cl-icons"].children.length < 20) errors.push("klienti: editor");
      byId["cl-label"].value = "Tiskárna";
      await byId["cl-icons"].children[5].fire("click");
      let n0 = requests.length;
      await byId["cl-save"].fire("click"); await tick();
      if (!requests.slice(n0).some((r) => r.startsWith("PUT") && r.includes("/client-notes/B0%3ABE"))) errors.push("klienti: uložení " + requests.slice(n0));
      n0 = requests.length;
      await rows[1].fire("contextmenu", {});
      const it = find(byId["ctx"], (n) => n.tagName === "BUTTON" && n.textContent.includes("Statická IP"));
      if (!it) errors.push("klienti: menu");
      else { await it.fire("click"); await tick(); }
      if (!requests.slice(n0).some((r) => r.endsWith("/clients/B0%3ABE%3A76%3A12%3A34%3A56/dhcp"))) errors.push("klienti: hledání DHCP serveru " + requests.slice(n0));
      if (byId["lease-wrap"].hidden || !byId["lease-rows"].children.some((r) => r.className === "lease-edit-row")) errors.push("klienti: editor rezervace");
      if (byId["lease-net"].hidden || !byId["lease-net-sum"].textContent.includes("192.168.10.1")) errors.push("DHCP síť: přehled");
      const save = find(byId["lease-net-body"], (n) => n.tagName === "BUTTON" && n.textContent.startsWith("Uložit nastavení sítě"));
      n0 = requests.length;
      if (save) { await save.fire("click"); await tick(); }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/dhcp/dhcp10/network"))) errors.push("DHCP síť: uložení");
    }
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
    // adopce na jeden krok: dialog (jméno, role podle modelu, skupina AP) a průběh u zařízení
    {
      const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
      await ctx.__t.openAdopt({ id: "p1", serial: "HGX1", model: "cAP ax", state: "pending" }); await tick();
      if (byId["adopt-dlg"].hidden || !byId["ad-m-ap"].checked || byId["ad-group-f"].hidden) errors.push("adopce: dialog / výchozí role AP");
      byId["ad-name"].value = "AP-sklad";
      const n0 = requests.length;
      await byId["ad-ok"].fire("click"); await tick();
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/devices/p1/approve"))) errors.push("adopce: schválení");
      await ctx.__t.openAdopt({ id: "p2", serial: "HGX2", model: "RB5009UG+S+", state: "pending" }); await tick();
      if (!byId["ad-m-monitor"].checked || !byId["ad-group-f"].hidden) errors.push("adopce: router jen sledovat");
      byId["adopt-dlg"].hidden = true;
      const fb = ctx.__t.flowBanner({ id: "x", serial: "S", adopt_flow: { step: "needs_confirm", msg: "vlastní konfigurace", removed: ["firewall: 3 pravidel"] } });
      if (!fb.textContent.includes("Přepsat načisto") || !fb.textContent.includes("firewall")) errors.push("adopce: banner rozhodnutí");
    }
    // přidání zařízení: jeden dialog s příkazem; zakládání tenanta je ve Všech lokalitách
    {
      const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
      const n0 = requests.length;
      await byId["add-dev"].fire("click"); await tick();
      if (byId["add-dlg"].hidden || !requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/adoption-tokens"))) errors.push("přidat zařízení: dialog / příkaz");
      await byId["add-close"].fire("click");
      if (!byId["add-dlg"].hidden) errors.push("přidat zařízení: zavření");
      if (byId["tenant-card"].hidden) errors.push("nový tenant: karta superadmina");
      if (byId["onboard"] && !byId["onboard"].hidden) errors.push("první kroky: jen v prázdné lokalitě");
    }
    // detail gw a sw (legacy) + náhled migrace + diff zálohy
    await ctx.__t.openDetail(api.gw);
    // panel zařízení: aplikace zůstává vidět, záložky Přehled · Porty · Wi-Fi · Diagnostika
    for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
    if (byId["detail"].hidden || byId["app"].hidden) errors.push("panel zařízení: viditelnost");
    if (byId["dp-over"].hidden || !byId["dp-ports"].hidden) errors.push("panel zařízení: výchozí záložka Přehled");
    if (!byId["d-quick"].textContent.includes("portů s linkou")) errors.push("panel zařízení: přehled portů " + byId["d-quick"].textContent.slice(0, 100));
    if (!byId["dt-wifi"].hidden) errors.push("panel zařízení: Wi-Fi záložka u brány bez rádií");
    // detail: logy a test rychlosti
    {
      const tick = async () => { for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r)); };
      const find = (n, pred) => (pred(n) ? n : (n.children || []).reduce((a, x) => a || (x instanceof El ? find(x, pred) : null), null));
      await byId["dt-diag"].fire("click"); await tick();
      if (byId["dp-diag"].hidden || byId["dp-sec"].hidden) errors.push("diagnostika: výchozí podzáložka");
      await byId["dt-logs"].fire("click"); await tick();
      if (byId["dp-diag"].hidden || byId["dp-logs"].hidden || byId["lg-rows"].children.length !== 2 || !byId["lg-rows"].textContent.includes("login failure"))
        errors.push("logy: tabulka " + byId["lg-rows"].children.length);
      if (!byId["lg-volume"].textContent.includes("5 kB denně") || byId["lg-level"].children.length !== 3) errors.push("logy: objem " + byId["lg-volume"].textContent);
      let n0 = requests.length;
      const pl = byId["lg-level"].children[2].fire("click"); await tick();
      if (byId["ask"].hidden || !byId["ask-text"].textContent.includes("MB")) errors.push("logy: dialog úrovně");
      else { byId["ask-ok"].onclick(); await pl; await tick(); }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/syslog"))) errors.push("logy: změna úrovně");
      await byId["dt-tools"].fire("click"); await tick();
      if (byId["dp-tools"].hidden || !byId["dp-logs"].hidden) errors.push("diagnostika: nástroje");
      if (byId["bw-to"].children.length !== 1 || byId["bw-addr"].children[0].value !== "192.168.77.2") errors.push("test rychlosti: cíle");
      if (byId["bw-hist"].children.length !== 2 || !byId["bw-hist"].textContent.includes("912 Mb/s") || !byId["bw-hist"].textContent.includes("limitem je CPU"))
        errors.push("test rychlosti: historie " + byId["bw-hist"].textContent.slice(0, 160));
      if (!find(byId["bw-hist"], (n) => n.tagName === "POLYLINE")) errors.push("test rychlosti: graf");
      if (!byId["bw-est"].textContent.includes("250 MB")) errors.push("test rychlosti: odhad dat " + byId["bw-est"].textContent);
      n0 = requests.length;
      const pb = byId["bw-start"].fire("click"); await tick();
      if (byId["ask"].hidden) errors.push("test rychlosti: dialog");
      else { byId["ask-ok"].onclick(); await pb; await tick(); }
      if (!requests.slice(n0).some((r) => r.startsWith("POST") && r.endsWith("/bwtest"))) errors.push("test rychlosti: spuštění");
      await byId["dt-ports"].fire("click");
    }
    {
      for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r));
      // detail: poznámka v poli a uložení formuláře (PATCH, ne odeslání stránky)
      if (byId["d-edit-note"].value !== "rozvaděč 2. patro") errors.push("detail: poznámka v poli " + byId["d-edit-note"].value);
      if (byId["d-edit-name"].value !== "gw") errors.push("detail: jméno v poli " + byId["d-edit-name"].value);
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
      await byId["dt-manage"].fire("click"); await tick();
      if (byId["dp-manage"].hidden || !byId["dp-over"].hidden || byId["ctl-ports"].children.length !== 6) errors.push("řízení: porty " + byId["ctl-ports"].children.length);
      if (!byId["ctl-ports"].textContent.includes("ether5-tiskarna (ether5)") || !byId["ctl-ports"].textContent.includes("uplink"))
        errors.push("řízení: jména/důvody portů");
      if (byId["ctl-vlans"].children.length !== 3 || byId["ctl-badge"].textContent !== "sledované") errors.push("řízení: VLAN/stav");
      if (byId["ctl-go"].disabled) errors.push("řízení: tlačítko převzetí");
      // role: switch / switch + CAPsMAN (jen řízené) / AP (lokalita má CAPsMAN jinde)
      if (byId["ctl-role"].children.length !== 3 || byId["ctl-role"].children[2].disabled || !byId["ctl-role"].children[1].disabled)
        errors.push("řízení: role AP / CAPsMAN");
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
      // rychlé hledání: sekce podle klíčového slova (bez diakritiky) i zařízení podle jména
      byId["gs-in"].value = "kanaly"; await byId["gs-in"].fire("input");
      if (byId["gs-list"].hidden || byId["gs-list"].children.length !== 1) errors.push("hledání: sekce Wi-Fi " + byId["gs-list"].children.length);
      byId["gs-in"].value = "gw"; await byId["gs-in"].fire("input");
      if (byId["gs-list"].children.length < 1) errors.push("hledání: zařízení gw");
      byId["gs-in"].value = "xyzxyz"; await byId["gs-in"].fire("input");
      if (byId["gs-list"].children[0].className !== "muted") errors.push("hledání: nic nenalezeno");
      byId["gs-in"].value = ""; await byId["gs-in"].fire("input");
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
    // tabulka portů: 4 fyzické porty (2 v LACP bondu), řazení podle anomálie, profil, jméno VLAN
    if (byId["d-ptable-wrap"].hidden || byId["d-ptable-b"].children.length !== 4) errors.push("tabulka portů");
    // LACP bond pod panelem portů (porty 5 a 7 – ne vedle sebe)
    if (!byId["d-ports"].textContent.includes("bond-mtc1") || !byId["d-ports"].textContent.includes("5, 7")) errors.push("chybí LACP bond pod porty");
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
