/* AgentMeter landing page behaviour (loaded from <head>; no inline scripts, so the CSP needs no hash).
   1. Theme before first paint: the same "agentmeter.theme" key as the app (system | light | dark).
   2. Theme toggle button.
   3. Scroll reveals: elements with .rv fade up once; .grow bars inside them scale in.
   Reduced motion: CSS shows everything at once; this script only adds classes. */
(function () {
  "use strict";
  var KEY = "agentmeter.theme";
  var root = document.documentElement;

  function stored() {
    try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; }
  }
  function apply(t) {
    if (t === "dark" || t === "light") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
  }
  apply(stored());
  root.classList.add("js");

  function isDark() {
    var t = root.getAttribute("data-theme");
    if (t) return t === "dark";
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function init() {
    var btn = document.getElementById("theme-toggle");
    var meta = document.querySelector('meta[name="theme-color"]');
    function sync() {
      var dark = isDark();
      if (btn) btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
      if (meta) meta.setAttribute("content", dark ? "#101418" : "#F4F2EC");
    }
    sync();
    if (btn) btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage blocked: theme lasts for this page only */ }
      sync();
    });

    var items = document.querySelectorAll(".rv");
    if (!("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
    items.forEach(function (el) { io.observe(el); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
