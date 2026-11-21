"use strict";
// Zavaděč překladů. Zdrojové texty rozhraní jsou česky; pro jiný jazyk načte slovník
// (/i18n/<kód>.json), přeloží statické HTML a teprve potom spustí aplikaci, aby její
// texty vznikaly už přeložené. Jazyk: uložená volba (localStorage mtc-lang), jinak podle prohlížeče.
(function () {
  const me = document.currentScript;
  const SOURCE = "cs";
  let lang = null;
  try { lang = localStorage.getItem("mtc-lang"); } catch (e) { /* soukromý režim */ }
  if (!lang) lang = /^(cs|sk)\b/i.test(navigator.language || "") ? SOURCE : "en";
  window.MTC_LANG = lang;

  const start = () => {
    for (const src of (me.dataset.app || "").split(",").filter(Boolean)) {
      const s = document.createElement("script");
      s.src = src; s.async = false;
      document.head.appendChild(s);
    }
  };
  const norm = (s) => s.replace(/\s+/g, " ").trim();
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];

  // statické HTML: každý textový uzel a vybrané atributy jsou samostatný klíč
  function translateDom(root, dict) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    for (const n of nodes) {
      const p = n.parentNode && n.parentNode.nodeName;
      if (p === "SCRIPT" || p === "STYLE") continue;
      const key = norm(n.nodeValue);
      const tr = key && dict[key];
      if (tr) n.nodeValue = n.nodeValue.replace(/^(\s*)[\s\S]*?(\s*)$/, (m, a, b) => a + tr + b);
    }
    for (const e of root.querySelectorAll("[title],[placeholder],[aria-label],[alt]")) {
      for (const a of ATTRS) {
        const v = e.getAttribute(a);
        const tr = v && dict[norm(v)];
        if (tr) e.setAttribute(a, tr);
      }
    }
  }

  if (lang === SOURCE) { start(); return; }
  fetch(`/i18n/${encodeURIComponent(lang)}.json`, { credentials: "same-origin" })
    .then((r) => { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
    .then((j) => {
      const dict = j.strings || {};
      window.MTC_DICT = dict;
      window.MTC_LOCALE = j.locale || lang;
      document.documentElement.lang = j.code || lang;
      const go = () => { translateDom(document.documentElement, dict); start(); };
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go); else go();
    })
    .catch(() => {
      // slovník chybí nebo je poškozený → čeština
      window.MTC_LANG = SOURCE;
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
    });
})();
