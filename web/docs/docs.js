// Renders an OpenAPI 3.1 document (web/docs/openapi-*.json) as a readable page: topics, each
// endpoint with who may call it, its parameters, request fields and responses. No dependencies.
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var DOCS = {}, current = null;
  var WHO = { userToken: function (sc) { return sc && sc.indexOf("admin") >= 0 ? "admin" : "logged in"; }, runToken: function (sc) { return "run token: " + (sc || []).join(", "); },
              backendSignature: function () { return "signed by the GPU backend"; }, prepSignature: function () { return "signed by the preparation service"; },
              controlPlaneSignature: function () { return "signed by the control plane"; } };
  function who(sec) {
    if (!sec || !sec.length) return "open";
    return sec.map(function (req) { return Object.keys(req).map(function (k) { return (WHO[k] || function () { return k; })(req[k]); }).join(" + "); }).join(" or ");
  }
  function slug(s) { return s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""); }
  function render() {
    var d = DOCS[current], q = $("q").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!d) return;
    var byTag = {};
    Object.keys(d.paths).forEach(function (path) {
      Object.keys(d.paths[path]).forEach(function (m) {
        var o = d.paths[path][m], hay = (m + " " + path + " " + o.summary + " " + (o.description || "") + " " + o.tags.join(" ")).toLowerCase();
        if (!q.every(function (w) { return hay.indexOf(w) >= 0; })) return;
        (byTag[o.tags[0]] = byTag[o.tags[0]] || []).push({ m: m, path: path, o: o });
      });
    });
    var tags = (d.tags || []).map(function (t) { return t.name; }).filter(function (t) { return byTag[t]; });
    var n = tags.reduce(function (a, t) { return a + byTag[t].length; }, 0);
    $("toc").innerHTML = tags.map(function (t) { return '<a href="#' + slug(t) + '">' + esc(t) + ' <span class="none">(' + byTag[t].length + ')</span></a>'; }).join("");
    $("doc").innerHTML = '<div class="intro"><b>' + esc(d.info.title) + '</b> · version ' + esc(d.info.version) + '<br>' + esc(d.info.description) +
      '<br><span class="none">Base URL: <code>' + esc((d.servers[0] || {}).url) + '</code> · ' + n + ' endpoint' + (n === 1 ? "" : "s") + (q.length ? " match" : "") + '</span></div>' +
      (tags.length ? tags.map(function (t) {
        return '<h2 id="' + slug(t) + '">' + esc(t) + '</h2>' + byTag[t].map(function (x) {
          var o = x.o, params = o.parameters || [], body = o.requestBody && o.requestBody.content["application/json"].schema.properties;
          return '<details class="op" id="' + esc(o.operationId) + '"><summary><span class="m ' + x.m + '">' + x.m + '</span><span class="path">' + esc(x.path) + '</span><span class="sum">' + esc(o.summary) +
            '</span><span class="who">' + esc(who(o.security)) + '</span></summary><div class="body">' +
            (o.description ? '<p>' + esc(o.description) + '</p>' : "") +
            (params.length ? '<div class="wrap"><table><thead><tr><th>Parameter</th><th>In</th><th>Type</th><th>Description</th></tr></thead><tbody>' + params.map(function (p) {
              return '<tr><td><code>' + esc(p.name) + '</code>' + (p.required ? " *" : "") + '</td><td>' + esc(p.in) + '</td><td>' + esc((p.schema || {}).type) + '</td><td>' + esc(p.description) + '</td></tr>';
            }).join("") + '</tbody></table></div>' : "") +
            (body ? '<div class="wrap"><table><thead><tr><th>Request field</th><th>Type</th><th>Notes</th></tr></thead><tbody>' + Object.keys(body).map(function (k) {
              return '<tr><td><code>' + esc(k) + '</code></td><td>' + esc(body[k].type + (body[k].format ? " (" + body[k].format + ")" : "")) + '</td><td>' + esc(body[k].description || "") + '</td></tr>';
            }).join("") + '</tbody></table></div>' : "") +
            '<div class="wrap"><table><thead><tr><th>Response</th><th>Meaning</th></tr></thead><tbody>' + Object.keys(o.responses).map(function (c) {
              return '<tr><td><code>' + esc(c) + '</code></td><td>' + esc(o.responses[c].description) + '</td></tr>';
            }).join("") + '</tbody></table></div></div></details>';
        }).join("");
      }).join("") : '<p class="none">No endpoint matches.</p>');
    if (location.hash.length > 1) { var t = document.getElementById(location.hash.slice(1)); if (t && t.tagName === "DETAILS") t.open = true; }
  }
  function show(name) {
    current = name;
    document.querySelectorAll(".tab").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-doc") === name)); });
    if (DOCS[name]) { render(); return; }
    fetch(name, { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) { DOCS[name] = d; if (current === name) render(); },
      function () { $("doc").innerHTML = '<p class="none">Could not load ' + esc(name) + '.</p>'; });
  }
  document.querySelectorAll(".tab").forEach(function (b) { b.addEventListener("click", function () { show(b.getAttribute("data-doc")); }); });
  $("q").addEventListener("input", render);
  show(new URLSearchParams(location.search).get("doc") || "openapi-control-plane.json");
})();
