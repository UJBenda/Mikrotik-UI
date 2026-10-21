"use strict";
// Průvodce prvního spuštění: kód → formulář → aplikace (root) → přesměrování na controller.
const $ = (id) => document.getElementById(id);
let code = "";
let cfgUrl = "";

function msg(text, error) {
  const m = $("msg");
  m.textContent = "";
  if (!text) return;
  const d = document.createElement("div");
  d.textContent = text;
  if (error) d.className = "error";
  m.appendChild(d);
}

async function call(method, path, body) {
  const r = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-Setup-Code": code },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* prázdná odpověď */ }
  if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
  return data;
}

function syncMode() {
  const f = $("setup-form");
  const mode = f.elements.mode.value;
  for (const el of f.querySelectorAll("[data-mode]")) el.hidden = el.dataset.mode !== mode;
  const le = f.elements.certs.value === "letsencrypt";
  for (const el of f.querySelectorAll("[data-le]")) el.hidden = !le;
}

$("code-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  code = ev.target.elements.code.value.trim().toUpperCase();
  try {
    const i = await call("GET", "/setup/api/info");
    if (i.configured) { msg("Controller je už nastavený."); return; }
    const f = $("setup-form");
    f.elements.controller_ip.value = i.ip || "";
    f.elements.host.value = i.ip || "";
    f.elements.admin_nets.value = (i.nets || []).join(", ");
    $("s-code").hidden = true;
    f.hidden = false;
    msg("");
  } catch (e) { msg(e.message, true); }
});

$("setup-form").addEventListener("change", syncMode);

$("setup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target.elements;
  if (f.password.value !== f.password2.value) { msg("Hesla se neshodují.", true); return; }
  const mode = f.mode.value;
  const host = mode === "local" ? (f.host.value.trim() || f.controller_ip.value.trim()) : "";
  const plan = {
    mode, controller_ip: f.controller_ip.value.trim(),
    ui_host: mode === "local" ? host : f.ui_host.value.trim(),
    checkin_host: mode === "local" ? host : f.checkin_host.value.trim(),
    admin_nets: f.admin_nets.value.split(/[\s,]+/).filter(Boolean),
    certs: f.certs.value, acme_email: f.acme_email.value.trim(),
  };
  $("go").disabled = true;
  try {
    await call("POST", "/setup/api/apply", {
      plan, admin: { email: f.email.value.trim(), password: f.password.value },
      tenant: f.tenant.value.trim(), site: f.site.value.trim(),
    });
    $("setup-form").hidden = true;
    $("s-progress").hidden = false;
    cfgUrl = "https://" + plan.ui_host + "/";
    msg("");
    poll(0);
  } catch (e) { msg(e.message, true); $("go").disabled = false; }
});

async function poll(n) {
  try {
    const st = await call("GET", "/setup/api/status");
    $("progress").textContent = st.detail || st.state;
    if (st.state === "done") { finish(st.url || cfgUrl); return; }
    if (st.state === "error") {
      msg("Nastavení selhalo: " + st.detail, true);
      $("setup-form").hidden = false;
      $("s-progress").hidden = true;
      $("go").disabled = false;
      return;
    }
  } catch (e) {
    // průvodce po přepnutí zmizí – to je v pořádku
    if (n > 3) { finish(cfgUrl); return; }
  }
  setTimeout(() => poll(n + 1), 1500);
}

function finish(url) {
  $("progress").textContent = "Hotovo. Za chvíli tě přesměruji na přihlášení: " + url;
  setTimeout(() => { location.href = url; }, 6000);
}

syncMode();
