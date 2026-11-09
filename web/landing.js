/* AgentMeter landing page behaviour (web/index.html, the site's main page; loaded from <head>, no inline
   scripts, so the CSP needs no hash).
   0. Old app links: the app moved from index.html to app.html, so a URL with an app route in its hash
      (/#/new, /#/session/<id>, links in old e-mails or bookmarks) is forwarded there before anything renders.
   1. Theme before first paint: the same "agentmeter.theme" key as the app (system | light | dark).
   2. Theme toggle button.
   3. Scroll reveals: elements with .rv fade up once; .grow bars inside them scale in; [data-count] counts up.
   4. Hero chart playhead + Replay button.  5. Scroll progress, header shadow, pipeline dot.
   6. Magnetic big buttons (fine pointers only).
   Reduced motion: everything is shown in its final state, nothing moves. */
(function () {
  "use strict";
  if (/^#\//.test(location.hash)) { location.replace("app.html" + location.hash); return; }
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
      if (meta) meta.setAttribute("content", dark ? "#12275A" : "#102A5C");
    }
    sync();
    if (btn) btn.addEventListener("click", function () {
      var next = isDark() ? "light" : "dark";
      apply(next);
      try { localStorage.setItem(KEY, next); } catch (e) { /* storage blocked: theme lasts for this page only */ }
      sync();
    });

    var reduce = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    var items = document.querySelectorAll(".rv");
    var hasIO = "IntersectionObserver" in window;

    // 4. Hero chart: a playhead sweeps one flow (10.79 s compressed into ~2.6 s); bars fill as it passes.
    var hero = setupHeroFlow(reduce);

    if (!hasIO || reduce) {
      items.forEach(function (el) { el.classList.add("in"); });
      if (hero) hero.finish();
    } else {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (!en.isIntersecting) return;
          en.target.classList.add("in");
          io.unobserve(en.target);
          if (hero && en.target.contains(hero.el)) setTimeout(hero.play, 350);
          countUp(en.target);
        });
      }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });
      items.forEach(function (el) { io.observe(el); });
    }

    // 5. Scroll-linked: reading progress, header shadow, the pipeline dot.
    setupScroll(reduce);
    // 6. Big primary buttons lean a few px toward the pointer.
    if (!reduce) setupMagnets();
  }

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function easeInOut(t) { return t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }

  function setupHeroFlow(reduce) {
    var wrap = document.getElementById("gwrap");
    if (!wrap) return null;
    var rows = Array.prototype.slice.call(wrap.querySelectorAll(".gantt li"));
    var lat = document.getElementById("hero-lat");
    var btn = document.getElementById("replay");
    var TOTAL = 10.79, DURATION = 2600, raf = 0, running = false;

    function render(sec) {
      wrap.style.setProperty("--ph", String(sec / TOTAL));
      rows.forEach(function (li) {
        var a = parseFloat(li.getAttribute("data-from")), b = parseFloat(li.getAttribute("data-to"));
        var r = clamp((sec - a) / (b - a), 0, 1);
        var bar = li.querySelector(".bar");
        if (bar) bar.style.setProperty("--r", String(r));
        li.classList.toggle("on", sec >= a && sec < b);
      });
      if (lat) lat.textContent = sec.toFixed(2) + " s";
    }
    function finish() {
      cancelAnimationFrame(raf);
      running = false;
      render(TOTAL);
      rows.forEach(function (li) { li.classList.remove("on"); });
      wrap.classList.remove("playing");
      if (lat) lat.textContent = "10.80 s";            // the measured mean, as reported
      if (btn) btn.disabled = false;
    }
    function play() {
      if (running) return;
      if (reduce) { finish(); return; }
      running = true;
      if (btn) btn.disabled = true;
      wrap.classList.add("playing");
      var t0 = performance.now();
      (function step(now) {
        var t = clamp((now - t0) / DURATION, 0, 1);
        render(easeInOut(t) * TOTAL);
        if (t < 1) raf = requestAnimationFrame(step);
        else setTimeout(finish, 250);
      })(t0);
    }
    render(0);
    if (btn) btn.addEventListener("click", function () { render(0); play(); });
    return { el: wrap, play: play, finish: finish };
  }

  function countUp(scope) {
    var els = scope.querySelectorAll("[data-count]");
    els.forEach(function (el) {
      var to = parseFloat(el.getAttribute("data-count")), t0 = performance.now(), D = 1100;
      var fmt = new Intl.NumberFormat("en-US");
      (function step(now) {
        var t = clamp((now - t0) / D, 0, 1);
        el.textContent = fmt.format(Math.round(to * (1 - Math.pow(1 - t, 3))));
        if (t < 1) requestAnimationFrame(step);
      })(t0);
    });
  }

  function setupScroll(reduce) {
    var root = document.documentElement;
    var top = document.querySelector(".top");
    var pipe = document.getElementById("pipe");
    var stages = pipe ? Array.prototype.slice.call(pipe.querySelectorAll(".stage")) : [];
    var ticking = false;

    function update() {
      ticking = false;
      var max = root.scrollHeight - window.innerHeight;
      var y = window.scrollY || window.pageYOffset;
      root.style.setProperty("--p", String(max > 0 ? clamp(y / max, 0, 1) : 0));
      if (top) top.classList.toggle("scrolled", y > 8);
      if (pipe && !reduce) {
        var vh = window.innerHeight, r = pipe.getBoundingClientRect();
        // The dot runs as the pipeline rises from 80% to 30% of the viewport height.
        var p = clamp((vh * 0.8 - r.top) / (vh * 0.5), 0, 1);
        pipe.style.setProperty("--pp", String(p));
        var idx = p <= 0 ? -1 : Math.min(stages.length - 1, Math.floor(p * stages.length));
        stages.forEach(function (s, i) { s.classList.toggle("active", i === idx); });
      }
    }
    function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    update();
  }

  function setupMagnets() {
    if (!(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches)) return;
    document.querySelectorAll(".magnet").forEach(function (b) {
      b.addEventListener("pointermove", function (e) {
        var r = b.getBoundingClientRect();
        var dx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
        var dy = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
        b.classList.add("pulling");
        b.style.setProperty("--mx", (dx * 6).toFixed(1) + "px");
        b.style.setProperty("--my", (dy * 4).toFixed(1) + "px");
      });
      b.addEventListener("pointerleave", function () {
        b.classList.remove("pulling");
        b.style.setProperty("--mx", "0px");
        b.style.setProperty("--my", "0px");
      });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
