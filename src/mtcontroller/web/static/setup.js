"use strict";
// Průvodce prvního spuštění: kód → nová instalace / obnova → … → nastavení (root) → záchranné údaje → přepnutí.
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const S = { code: "", info: null, flow: "install", uploaded: false, step: "code", polling: false, kitText: "" };

const FLOWS = {
  install: ["code", "start", "mode", "network", "region", "admin", "summary", "progress", "kit", "done"],
  restore: ["code", "start", "backup", "network", "summary", "progress", "kit", "done"],
};
const LOCKED = new Set(["progress", "kit", "done"]);  // odsud zpět nejde
const PROGRESS = {
  install: ["Certifikáty a konfigurace", "Webový server a služby", "První záloha", "Záchranné údaje"],
  restore: ["Obnova databáze a konfigurace ze zálohy", "Certifikáty", "Webový server a služby", "Záchranné údaje"],
};
// krok ze stavu služby → index v PROGRESS
const STEP_IDX = { install: { 1: 0, 3: 1, 4: 2, 5: 3 }, restore: { 1: 0, 2: 1, 3: 2, 5: 3 } };

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}

function toast(text, info) {
  const t = $("toast");
  t.textContent = text || "";
  t.className = "toast" + (info ? " info" : "");
  t.hidden = !text;
  clearTimeout(toast.t);
  if (text) toast.t = setTimeout(() => { t.hidden = true; }, 7000);
}

async function call(method, path, body, raw) {
  const opts = { method, headers: { "X-Setup-Code": S.code } };
  if (raw) opts.body = raw;
  else if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* prázdná odpověď */ }
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}

const val = (name) => {
  const e = document.querySelector(`[name="${name}"]:checked`) || document.querySelector(`[name="${name}"]`);
  return e ? e.value.trim() : "";
};
const list = (s) => s.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean);
const flowSteps = () => FLOWS[S.flow];

// --- navigace ---------------------------------------------------------------------------------

function renderRail() {
  const ol = $("rail"); ol.textContent = "";
  const steps = flowSteps().filter((s) => s !== "done");
  const cur = steps.indexOf(S.step);
  steps.forEach((s, i) => {
    const sec = document.querySelector(`[data-step="${s}"]`);
    const li = el("li", null, i < cur || S.step === "done" ? "done" : i === cur ? "current" : "");
    const n = el("span", null, "n"); n.appendChild(el("span", String(i + 1)));
    li.append(n, el("span", sec.dataset.title, "t"));
    ol.appendChild(li);
  });
}

function show(step) {
  S.step = step;
  for (const s of $$(".step")) s.hidden = s.dataset.step !== step;
  const nav = $("nav");
  nav.hidden = step === "code" || LOCKED.has(step);
  $("back").style.visibility = step === "start" ? "hidden" : "visible";
  const next = $("next");
  next.textContent = "";
  next.append(step === "summary" ? (S.flow === "restore" ? "Obnovit controller" : "Nastavit controller") : "Pokračovat", el("i", null, "arr"));
  if (step === "kit") nav.hidden = false;
  if (step === "kit") { next.textContent = ""; next.append("Dokončit a přejít do controlleru", el("i", null, "arr")); $("back").style.visibility = "hidden"; }
  syncKit();
  renderRail();
  toast("");
  const first = document.querySelector(`[data-step="${step}"] input:not([type=radio]):not([type=checkbox]):not([type=file])`);
  if (first && !first.value) first.focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function move(delta) {
  const steps = flowSteps();
  const i = steps.indexOf(S.step) + delta;
  if (i >= 0 && i < steps.length) show(steps[i]);
}

$("back").addEventListener("click", () => move(-1));
$("next").addEventListener("click", async () => {
  try {
    if (S.step === "kit") return await finish();
    validate(S.step);
    if (S.step === "summary") return await submit();
    if (S.step === "network") fillSummary();
    if (S.step === "region" || S.step === "admin") fillSummary();
    move(1);
  } catch (e) { toast(e.message); }
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !$("nav").hidden && ev.target.tagName === "INPUT" && ev.target.type !== "file") {
    ev.preventDefault();
    $("next").click();
  }
});

// --- kód --------------------------------------------------------------------------------------

$("f-code").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  S.code = ev.target.elements.code.value.trim().toUpperCase();
  try {
    const i = await call("GET", "/setup/api/info");
    S.info = i;
    if (i.configured) {
      // nastavení už proběhlo – navázat (např. po obnovení stránky na záchranných údajích)
      const st = await call("GET", "/setup/api/status");
      if (st.state === "ready") { S.flow = st.kit && st.kit.restored ? "restore" : "install"; return showKit(st); }
      if (st.state === "done") return showDone(st.url);
      toast("Controller je už nastavený.", true);
      return;
    }
    prefill(i);
    const st = await call("GET", "/setup/api/status");
    if (st.state === "queued" || st.state === "applying") { S.flow = st.restore ? "restore" : S.flow; show("progress"); poll(); return; }
    show("start");
  } catch (e) { toast(e.message); }
});

function prefill(i) {
  const f = (n) => document.querySelector(`[name="${n}"]`);
  f("controller_ip").value = i.ip || "";
  f("host").value = i.ip || "";
  f("admin_nets").value = (i.nets || []).join(", ");
  $("top-hint").textContent = i.hostname ? `Server ${i.hostname}` : "";
  const sel = $("country"); sel.textContent = "";
  for (const c of i.countries || []) {
    const o = el("option", `${c.label} (${c.ros})`); o.value = c.ros; o.dataset.ntp = c.ntp.join(", ");
    sel.appendChild(o);
  }
  sel.value = "Czech Republic";
  f("ntp").value = (sel.selectedOptions[0] && sel.selectedOptions[0].dataset.ntp) || "tik.cesnet.cz, tak.cesnet.cz";
  const dl = $("tz-list"); dl.textContent = "";
  for (const z of i.timezones || []) { const o = el("option"); o.value = z; dl.appendChild(o); }
  const tz = i.timezone || "";
  f("timezone").value = !tz || tz === "UTC" || tz.startsWith("Etc/") ? "Europe/Prague" : tz;
}

$("country").addEventListener("change", (ev) => {
  const o = ev.target.selectedOptions[0];
  if (o) document.querySelector('[name="ntp"]').value = o.dataset.ntp;
});

// --- volby ------------------------------------------------------------------------------------

function syncChoices() {
  S.flow = val("flow") || "install";
  const mode = val("mode");
  for (const e of $$("#net-fields [data-mode]")) e.hidden = S.flow === "restore" || e.dataset.mode !== mode;
  $("net-fields").querySelector('[name="controller_ip"]').closest(".field").hidden = S.flow === "restore";
  $("cert-box").hidden = S.flow === "restore";
  $("net-lead").textContent = S.flow === "restore"
    ? "Adresy a certifikát se převezmou ze zálohy. Tady jen potvrď, odkud smí do administrace (pro zálohy ze starší instalace)."
    : "Adresy, na kterých bude controller dostupný.";
  const cert = val("certs");
  for (const e of $$("[data-cert]")) e.hidden = e.dataset.cert !== cert;
  renderRail();
}
document.addEventListener("change", (ev) => { if (["flow", "mode", "certs"].includes(ev.target.name)) syncChoices(); });

// --- záloha (nahrání) ------------------------------------------------------------------------

const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", (ev) => { ev.preventDefault(); if (ev.dataTransfer.files[0]) upload(ev.dataTransfer.files[0]); });
$("bk-file").addEventListener("change", (ev) => { if (ev.target.files[0]) upload(ev.target.files[0]); });

function upload(file) {
  S.uploaded = false;
  drop.classList.remove("ok");
  $("drop-title").textContent = file.name;
  $("drop-sub").textContent = "Nahrávám…";
  const bar = drop.querySelector(".bar"); bar.hidden = false; $("bk-bar").style.width = "0";
  const x = new XMLHttpRequest();
  x.open("PUT", "/setup/api/upload");
  x.setRequestHeader("X-Setup-Code", S.code);
  x.upload.onprogress = (e) => { if (e.lengthComputable) $("bk-bar").style.width = (100 * e.loaded / e.total).toFixed(1) + "%"; };
  x.onload = () => {
    let d = {}; try { d = JSON.parse(x.responseText); } catch (e) { /* */ }
    if (x.status === 200) {
      S.uploaded = true; drop.classList.add("ok");
      $("drop-sub").textContent = `Nahráno · ${(file.size / 1048576).toFixed(1)} MB`;
      S.backupName = file.name;
    } else { $("drop-sub").textContent = d.detail || "Nahrání selhalo"; toast(d.detail || "Nahrání selhalo"); bar.hidden = true; }
  };
  x.onerror = () => { $("drop-sub").textContent = "Nahrání selhalo"; bar.hidden = true; };
  x.send(file);
}

// --- heslo ------------------------------------------------------------------------------------

document.querySelector('[name="password"]').addEventListener("input", (ev) => {
  const p = ev.target.value;
  const kinds = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((r) => r.test(p)).length;
  const score = p.length < 12 ? Math.min(p.length / 12, 1) * 0.35 : Math.min(1, 0.45 + (p.length - 12) * 0.04 + kinds * 0.1);
  const m = $("pw-meter");
  m.style.width = Math.round(score * 100) + "%";
  m.style.background = score < 0.45 ? "var(--bad)" : score < 0.75 ? "var(--warn)" : "var(--ok)";
  $("pw-hint").textContent = p.length < 12 ? `Ještě ${12 - p.length} znaků.` : score < 0.75 ? "Použitelné – delší je lepší." : "Silné heslo.";
});

// --- kontrola a souhrn ----------------------------------------------------------------------

const IP4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
const HOST = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/i;
const CIDR = /^[0-9a-f.:]+(\/\d{1,3})?$/i;

function need(ok, msg, name) {
  if (ok) return;
  const e = name && document.querySelector(`[name="${name}"]`);
  if (e) { e.classList.add("bad"); e.focus(); e.addEventListener("input", () => e.classList.remove("bad"), { once: true }); }
  throw new Error(msg);
}

function validate(step) {
  if (step === "backup") {
    need(S.uploaded, "Nahraj soubor zálohy.");
    need($("bk-pass").value.trim().length >= 12, "Zadej heslo záloh.");
  }
  if (step === "network") {
    const nets = list(val("admin_nets"));
    need(nets.length && nets.every((n) => CIDR.test(n)), "Zadej sítě pro správu, např. 192.168.88.0/24.", "admin_nets");
    if (S.flow === "restore") return;
    need(IP4.test(val("controller_ip")), "IP adresa serveru není platná IPv4.", "controller_ip");
    if (val("mode") === "local") need(HOST.test(val("host") || val("controller_ip")), "Neplatná adresa controlleru.", "host");
    else {
      need(HOST.test(val("ui_host")) && val("ui_host").includes("."), "Zadej hostname administrace.", "ui_host");
      need(HOST.test(val("checkin_host")) && val("checkin_host").includes("."), "Zadej hostname check-inu.", "checkin_host");
      need(val("ui_host") !== val("checkin_host"), "Administrace a check-in musí mít různá jména.", "checkin_host");
    }
    if (val("certs") === "letsencrypt") {
      need(/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val("acme_email")), "Zadej e-mail pro Let's Encrypt.", "acme_email");
      const hosts = val("mode") === "local" ? [val("host")] : [val("ui_host"), val("checkin_host")];
      need(!hosts.some((h) => IP4.test(h)), "Let's Encrypt potřebuje DNS jména, ne IP adresu.", "certs");
    }
  }
  if (step === "region") {
    need(/^[A-Za-z_]+(\/[A-Za-z0-9_+-]+){0,2}$/.test(val("timezone")), "Vyber časovou zónu ze seznamu.", "timezone");
    const n = list(val("ntp"));
    need(n.length >= 1 && n.length <= 4 && n.every((h) => HOST.test(h)), "NTP: 1–4 servery.", "ntp");
  }
  if (step === "admin") {
    need(/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(val("email")), "Zadej e-mail správce.", "email");
    need(val("password").length >= 12, "Heslo musí mít alespoň 12 znaků.", "password");
    need(val("password") === val("password2"), "Hesla se neshodují.", "password2");
  }
}

function plan() {
  const mode = val("mode");
  const host = mode === "local" ? (val("host") || val("controller_ip")) : "";
  return {
    mode, controller_ip: val("controller_ip"),
    ui_host: mode === "local" ? host : val("ui_host"),
    checkin_host: mode === "local" ? host : val("checkin_host"),
    admin_nets: list(val("admin_nets")), certs: val("certs"), acme_email: val("acme_email"),
    timezone: val("timezone"), country: val("country"), ntp: list(val("ntp")),
  };
}

function dl(target, rows) {
  target.textContent = "";
  for (const [k, v] of rows) { if (v === null || v === undefined || v === "") continue; target.append(el("dt", k), el("dd", v)); }
}

function fillSummary() {
  const p = plan();
  if (S.flow === "restore") {
    dl($("summary"), [["Akce", "Obnova ze zálohy"], ["Soubor", S.backupName || ""],
      ["Síť a certifikát", "ze zálohy"], ["Správa povolena z", p.admin_nets.join(", ")]]);
    return;
  }
  dl($("summary"), [
    ["Režim", p.mode === "local" ? "Lokální controller" : "Centrální vzdálená správa"],
    ["Adresa", "https://" + p.ui_host + "/"], ...(p.mode === "remote" ? [["Check-in", p.checkin_host]] : []),
    ["IP serveru", p.controller_ip], ["Správa povolena z", p.admin_nets.join(", ")],
    ["Certifikát", p.certs === "own_ca" ? "Vlastní CA controlleru" : "Let's Encrypt (" + p.acme_email + ")"],
    ["Země / Wi-Fi", p.country], ["Časová zóna", p.timezone], ["NTP", p.ntp.join(", ")],
    ["Správce", val("email")], ["Firma / lokalita", [val("tenant"), val("site")].filter(Boolean).join(" / ") || "–"],
  ]);
}

// --- odeslání a průběh -----------------------------------------------------------------------

async function submit() {
  $("next").disabled = true;
  try {
    if (S.flow === "restore") {
      await call("POST", "/setup/api/apply", { action: "restore", restore: { passphrase: $("bk-pass").value.trim() },
                                               form: { admin_nets: list(val("admin_nets")), acme_email: val("acme_email") } });
    } else {
      await call("POST", "/setup/api/apply", { action: "install", plan: plan(),
        admin: { email: val("email"), password: val("password"), password2: val("password2") },
        tenant: val("tenant"), site: val("site") });
    }
    show("progress");
    poll();
  } finally { $("next").disabled = false; }
}

function renderChecklist(idx, failed) {
  const ul = $("checklist"); ul.textContent = "";
  PROGRESS[S.flow].forEach((t, i) => {
    const li = el("li", null, i < idx ? "ok" : i === idx ? (failed ? "err" : "run") : "");
    li.append(el("i"), el("span", t));
    ul.appendChild(li);
  });
}

async function poll() {
  if (S.polling) return;
  S.polling = true;
  renderChecklist(-1);
  let misses = 0;
  while (S.polling) {
    try {
      const st = await call("GET", "/setup/api/status");
      misses = 0;
      const idx = STEP_IDX[S.flow][st.step] ?? -1;
      renderChecklist(idx === -1 ? 0 : idx);
      $("prog-detail").textContent = st.detail || "";
      if (st.state === "ready") { S.polling = false; renderChecklist(99); setTimeout(() => showKit(st), 500); return; }
      if (st.state === "error") {
        S.polling = false;
        renderChecklist(idx === -1 ? 0 : idx, true);
        $("prog-title").textContent = "Nastavení se nepovedlo";
        await new Promise((r) => setTimeout(r, 1500));
        $("prog-title").textContent = "Nastavuji controller…";
        show("summary");  // oprav a odešli znovu
        toast("Nastavení se nepovedlo: " + (st.detail || "chyba"));
        return;
      }
    } catch (e) {
      if (++misses > 20) { S.polling = false; toast("Služba nastavení neodpovídá: " + e.message); return; }
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
}

// --- záchranné údaje a konec ---------------------------------------------------------------

function showKit(st) {
  const k = st.kit || {};
  S.kitText = st.kit_text || "";
  $("kit-pass").textContent = k.backup_passphrase || "";
  const w = $("kit-warning"); w.textContent = k.warning || ""; w.hidden = !k.warning;
  const rows = [["Adresa", k.url], ["Správce", k.admin], ["Zálohy", k.backup_dir ? k.backup_dir + " (šifrované; plán a kopie do cloudu: Systém → Zálohy)" : ""],
    ["Databáze", k.db ? `${k.db.name} – ${k.db.roles}` : ""], ["Přihlášení do DB", k.db ? k.db.auth : ""],
    ["Hlavní klíč", k.files ? k.files.master_key + " (je i v každé záloze)" : ""]];
  if (k.restored) rows.unshift(["Obnoveno ze zálohy", `${(k.restored.from || "").slice(0, 16).replace("T", " ")} · verze ${k.restored.version || "?"}` +
                               (k.restored.migrated && k.restored.migrated.length ? ` · převedeno na novou verzi (${k.restored.migrated.length} migrací)` : "")]);
  dl($("kit-list"), rows);
  S.url = k.url;
  show("kit");
}

function syncKit() { if (S.step === "kit") $("next").disabled = !$("kit-ok").checked; }
$("kit-ok").addEventListener("change", syncKit);
$("kit-copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("kit-pass").textContent); toast("Heslo zkopírováno", true); }
  catch (e) { toast("Kopírování nejde – označ heslo a zkopíruj ručně."); }
});
$("kit-download").addEventListener("click", () => {
  const a = el("a");
  a.href = URL.createObjectURL(new Blob([S.kitText], { type: "text/plain;charset=utf-8" }));
  a.download = "mtcontroller-zachranne-udaje.txt";
  document.body.appendChild(a); a.click(); a.remove();
});
$("kit-print").addEventListener("click", () => { $("print-area").textContent = S.kitText; window.print(); });

async function finish() {
  $("next").disabled = true;
  try {
    await call("POST", "/setup/api/apply", { action: "finish" });
  } catch (e) { $("next").disabled = false; throw e; }
  // po přepnutí průvodce zmizí – další dotazy už neprojdou, to je v pořádku
  for (let i = 0; i < 8; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try { const st = await call("GET", "/setup/api/status"); if (st.state === "done") break; } catch (e) { break; }
  }
  showDone(S.url);
}

function showDone(url) {
  url = url || "/";
  $("done-link").href = url;
  show("done");
  let n = 6;
  const tick = () => {
    $("done-text").textContent = `Za ${n} s tě přesměruji na přihlášení (${url}). Prohlížeč možná upozorní na certifikát vlastní CA.`;
    if (n-- <= 0) location.href = url; else setTimeout(tick, 1000);
  };
  tick();
}

syncChoices();
renderRail();
