// AgentMeter web app — wizard.js: New benchmark: data (upload or link; with a control plane it is prepared on the GPU machine, never alongside a benchmark), models, settings, Run, live telemetry.
// One of the app's scripts (web/app.html loads them in order: core, home, wizard, session, analysis,
// compare, settings, boot); they share the page's global scope, so a function defined in one is used by the others.
"use strict";
// ---------- NEW BENCHMARK (wizard) ----------
var WIZ = { prep: null, set: null, summary: null, src: "file" };
// With a control plane every upload goes to the preparation service (New Benchmark steps 2-3). The
// end-to-end tests may still force the GPU server ("agentmeter.prepWhere" = server), as before.
function viaService() {
  if (!CP) return false;
  try { return localStorage.getItem("agentmeter.prepWhere") !== "server"; } catch (e) { return true; }
}
function wizStep(step) {
  var panel = step === "review" ? "prep" : step;            // steps 2 and 3 share one panel (analyse.js)
  ["data", "prep", "models", "run"].forEach(function (s) { $("wiz-step-" + s).hidden = s !== panel; });
  var order = ["data", "prep", "review", "models", "run"], idx = order.indexOf(step), n = 0;
  document.querySelectorAll("#wiz-steps li").forEach(function (li, i) {
    // steps 2-3 (the preparation service) exist with a control plane only; the others are renumbered
    li.hidden = !CP && (li.getAttribute("data-step") === "prep" || li.getAttribute("data-step") === "review");
    if (!li.hidden) li.querySelector("b").textContent = String(++n);
    li.classList.toggle("done", i < idx);
    if (i === idx) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
  });
}
function routeWizard(p, q) {
  var off = !ONLINE;
  $("wiz-offline").hidden = !off;
  // with a control plane, uploading and preparing need no GPU (the preparation service does it)
  // (via the preparation service, Continue starts the service itself when it is not running)
  if ($("svc-upload-btn")) $("svc-upload-btn").disabled = viaService() ? !anCloud() : off;
  if ($("svc-import-btn")) $("svc-import-btn").disabled = off;
  if (p[1] === "models") { wizStep("models"); wizModels(q); }
  else if (p[1] === "run" && p[2]) { wizStep("run"); watchRun(p[2]); }
  else if (p[1] === "prep" && p[2] && CP) { wizStep("prep"); anDetail(p[2]); }
  else { wizStep("data"); wizData(q.src || "file", q); }
}
var srcTabs = Array.prototype.slice.call(document.querySelectorAll('#wiz-step-data [role="tab"]'));
function setSrc(name) {
  WIZ.src = name;
  selectTab(srcTabs, $("tab-src-" + name));
  $("src-file").hidden = name !== "file";
  $("src-url").hidden = name !== "url";
  $("svc-upload-error").hidden = true;
}
// the chosen source lives in the URL (#/new?src=…): a re-render (e.g. the GPU going offline)
// keeps it instead of falling back to "Upload a file"; replaceState fires no hashchange
tablist(document.querySelector("#wiz-step-data .tabs"), function (t) {
  var src = t.id.replace("tab-src-", "");
  try { history.replaceState(null, "", "#/new" + (src === "file" ? "" : "?src=" + src)); } catch (e) { /* URL unchanged */ }
  setSrc(src);
});
function wizData(src) {
  setSrc(src === "url" ? "url" : "file");
  if (viaService()) { anServiceNotice(); anUploads(); } else { $("an-unavailable").hidden = true; $("svc-prep-where").hidden = true; $("an-uploads").hidden = true; }
}

$("svc-upload-form").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var err = $("svc-upload-error"), lim = (CONFIG && CONFIG.limits) || null, fd = new FormData();
  err.hidden = true;
  if (WIZ.src === "url") {
    var url = $("svc-url").value.trim();
    if (!/^https:\/\//i.test(url)) { showError(err, { error: "Give a direct https:// link to the .pcap/.pcapng/.csv file." }); return; }
    fd.append("url", url);
  } else {
    var file = $("svc-file").files[0];
    if (!file) { showError(err, { error: "Choose a .csv, .pcap or .pcapng file first, or use Import from a link." }); return; }
    if (lim && file.size > lim.max_upload_bytes) {   // checked here too, so a huge file is never sent
      showError(err, { error: "This file is too large to upload. The limit is " + lim.max_upload_label + " (this file is " +
        fmtSize(file.size) + "). Use Import from a link instead. The server downloads files up to " +
        lim.max_url_download_label + " from a direct https link.", code: "too_large" });
      return;
    }
    fd.append("file", file);
  }
  var src = WIZ.src === "url" ? { url: fd.get("url") } : { file: file };
  if (viaService()) { startAnalysis(src); return; }   // the preparation service: no GPU needed (analyse.js)
  prepareOnServer(fd);
});
function prepareOnServer(fd) {
  var err = $("svc-upload-error");
  fd.append("max_flows", $("svc-max-flows").value || "50");
  $("svc-upload-btn").disabled = true; $("svc-upload-busy").hidden = false;
  $("svc-upload-busy").textContent = WIZ.src === "url" ? "Checking the link…" : "Uploading…";
  api("/api/prepare", { method: "POST", body: fd }).then(function (r) {
    $("svc-upload-btn").disabled = false; $("svc-upload-busy").hidden = true;
    if (!r.ok) { showError(err, r.data); return; }
    location.hash = "#/new/models?prep=" + enc(r.data.job_id);      // models now; data prepares meanwhile
  });
}
// Without a control plane (one GPU server, one user) the GPU server prepares the data as a job in
// its own queue, so it never runs alongside a benchmark.
// ---------- the preparation service's files go to the GPU server (hash-checked there) ----------
// Imported once: a set already on the GPU server is used as it is, so a page reload never creates a
// second copy (each upload starts one benchmark, checked by the control plane on the set's name).
function cloudImport(j) {
  return api("/api/prepared/" + setPath(j.set_name)).then(function (have) {
    if (have.ok) return { ok: true, data: { prepared_set: j.set_name } };
    return cloudImportFiles(j);
  });
}
function cloudImportFiles(j) {
  return Promise.all(j.files.map(function (n) {
    if (n === "ids_flows_identity.csv") return Promise.resolve(null);
    return cpFetch("/api/prep/jobs/" + enc(j.job_id) + "/files/" + n, { raw: true }).then(function (r) { return r && r.ok ? r.blob() : null; });
  })).then(function (blobs) {
    var fd = new FormData(), field = { "manifest.json": "manifest", "features.csv": "features", "labels.csv": "labels",
                                       "ids_scores.csv": "ids_scores", "ids_pool.json": "ids_pool" };   // the addresses never go
    for (var i = 0; i < j.files.length; i++) {
      if (!field[j.files[i]]) continue;
      if (!blobs[i]) return { ok: false, data: { error: "Could not read " + j.files[i] + " from the cloud." } };
      fd.append(field[j.files[i]], blobs[i], j.files[i]);
    }
    return api("/api/prepared/import", { method: "POST", body: fd });
  });
}
var drop = $("svc-drop");
["dragover", "dragenter"].forEach(function (e) { drop.addEventListener(e, function (ev) { ev.preventDefault(); drop.classList.add("over"); }); });
["dragleave", "drop"].forEach(function (e) { drop.addEventListener(e, function () { drop.classList.remove("over"); }); });
drop.addEventListener("drop", function (ev) {
  ev.preventDefault();
  if (ev.dataTransfer && ev.dataTransfer.files.length) { $("svc-file").files = ev.dataTransfer.files; showChosen(); }
});
// the chosen file, by name and size, in place of the browser's "No file chosen"
function showChosen() {
  var f = $("svc-file").files[0], el = $("svc-file-name");
  if (!f) { el.hidden = true; return; }
  var mb = f.size / 1048576;
  el.textContent = f.name + " · " + (mb >= 1 ? mb.toFixed(1) + " MB" : Math.max(1, Math.round(f.size / 1024)) + " KB");
  el.hidden = false;
}
$("svc-file").addEventListener("change", showChosen);
// step 2
var PHASES = { queued: "Queued", downloading: "Downloading", downloaded: "Downloaded", validating: "Validating",
  sampling: "Sampling rows across the file", indexing: "Indexing the capture", windowing: "Copying time windows",
  extracting: "Extracting flows", selecting: "Selecting flows", writing: "Writing the prepared set", ids: "Scoring with the IDS", done: "Done" };
function wizModels(q) {
  WIZ.prep = q.prep || null; WIZ.set = q.set || null; WIZ.summary = null;
  WIZ.want = q.models ? q.models.split(",").filter(Boolean) : null;          // a preset: these models
  fillPresets();
  $("wiz-again").hidden = true;
  $("wiz-back").setAttribute("href", "#/new?src=" + enc(WIZ.src || "file"));
  $("svc-summary").innerHTML = ""; $("wiz-data-error").hidden = true; $("svc-run-error").hidden = true;
  renderModels();
  renderSettings();
  loadHistory();
  setDataState("preparing", "Checking your data…", null);    // never show the previous data set's Ready/Failed
  $("svc-ids-tau").value = q.ids || "";                      // from Analyse traffic: the IDS gate at its threshold
  if (WIZ.set) dataReady(WIZ.set);
  else if (WIZ.prep) pollPrepare(WIZ.prep, ++pollToken);
  else location.replace("#/new");
}
function setDataState(kind, text, pct) {
  var chip = $("wiz-data-chip"), bar = $("wiz-data-bar");
  chip.className = "status " + (kind === "ready" ? "Done" : kind === "failed" ? "Failed" : "Running");
  chip.textContent = kind === "ready" ? "Ready" : kind === "failed" ? "Failed" : "Preparing";
  $("wiz-data-status").textContent = text || "";
  bar.hidden = kind !== "preparing";
  bar.classList.toggle("indet", pct == null);
  bar.querySelector("span").style.width = pct == null ? "" : pct + "%";
  syncRun();
}
function pollPrepare(id, token) {
  (function tick() {
    if (token !== pollToken) return;
    api("/api/jobs/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      if (!r.ok) { setDataState("failed", ""); showError($("wiz-data-error"), r.data); return; }
      var j = r.data, pp = j.progress || {};
      if (j.status === "done" && j.prepared) { WIZ.set = j.prepared; dataReady(j.prepared); return; }
      if (j.status === "failed" || j.status === "interrupted") {
        setDataState("failed", "Data preparation " + j.status + ".");
        showError($("wiz-data-error"), { error: (j.error || j.message) + ". Go back to step 1 to fix the input.", code: j.error_code || j.status });
        return;
      }
      var pct = pp.phase === "downloading" && pp.total ? Math.round(100 * pp.bytes / pp.total) : null;
      setDataState("preparing", (j.status === "queued" ? "Queued, " + (j.queue_position ? j.queue_position + " job(s) ahead" : "starting next") :
        (PHASES[pp.phase] || pp.phase || "Preparing") + (pct != null ? ", " + pct + "%" : "")) +
        " (" + (j.run_name || "your file") + "). You can pick the models now.", pct);
      setTimeout(tick, 1000);
    });
  })();
}
function dataReady(set) {
  api("/api/prepared/" + setPath(set)).then(function (r) {
    if (!r.ok) { setDataState("failed", ""); showError($("wiz-data-error"), r.data); return; }
    WIZ.summary = r.data;
    setDataState("ready", fmtNum(r.data.rows_selected) + " flows ready, prepared set " + r.data.prepared_set);
    renderSummary(r.data);
    renderSettings();
  });
}
function modeChip(acc) {
  return acc ? '<span class="chip ok">Labelled → efficiency + reference-label check</span>' : '<span class="chip warn">Unlabelled → efficiency + validity only</span>';
}
function distTable(s) {
  var cc = s.class_counts || {}, pool = cc.in_pool || s.class_distribution || {}, sel = s.label_distribution_selected || {};
  var file = cc.in_file || pool;
  var keys = (cc.class_set && cc.class_set.length) ? cc.class_set.slice() : Object.keys(pool).sort();
  if (!keys.length) return "";
  return '<div class="table-wrap"><table id="svc-class-table"><thead><tr><th>Class</th><th class="num">In file</th><th class="num">Usable in pool</th><th class="num">Selected</th></tr></thead><tbody>' +
    keys.map(function (k) {
      var lost = (file[k] || pool[k]) && !sel[k];
      return '<tr><td>' + esc(k) + (lost ? ' <span class="chip bad">absent</span>' : '') + '</td><td class="num">' + fmtNum(file[k] || 0) +
        '</td><td class="num">' + fmtNum(pool[k] || 0) + '</td><td class="num">' + fmtNum(sel[k] || 0) + '</td></tr>';
    }).join("") + '</tbody></table></div>';
}
function renderSummary(s) {
  var fm = s.feature_match || {}, csv = s.source_type === "csv", big = s.large_input || {}, samp = s.sampling || {};
  var counts = csv
    ? [["File", s.source_file], ["Rows in file", fmtNum(s.rows_read)], ["Candidate pool", fmtNum(s.rows_in_pool)], ["Usable rows (pool)", fmtNum(s.rows_usable)],
       ["Dropped (NaN/Inf)", fmtNum(s.rows_dropped)], ["Excluded (outside 5 classes)", fmtNum(s.rows_excluded_out_of_taxonomy)],
       ["Feature columns", (s.columns_present == null ? "—" : s.columns_present + " / 78")], ["Label column", s.label_column || "none"]]
    : [["File", s.source_file], ["Packets in capture", fmtNum(s.packets)], ["Packets parsed", fmtNum(s.packets_parsed)],
       ["Capture length", fmtNum(s.capture_duration_s, 1) + " s"], ["Flows extracted", fmtNum(s.flows_extracted)], ["Candidate pool", fmtNum(s.rows_total)]];
  var src = s.source || {};
  if (src.url) counts.splice(1, 0, ["Imported from", src.final_url || src.url]);
  if (s.file_size_bytes != null) counts.splice(1, 0, ["File size", fmtSize(s.file_size_bytes)]);
  if (src.sha256) counts.push(["SHA-256", src.sha256.slice(0, 16) + "…"]);
  counts.push(["Flows selected", fmtNum(s.rows_selected) + " of " + fmtNum(s.rows_total)]);
  var rules = (s.rules || []).map(function (r) {
    return '<tr><td class="mono">' + esc(r.id) + '</td><td>' + esc(r.kind === "constraint" ? "constraint" : r.type) +
      '</td><td>' + (r.fired ? "fired" : '<span class="muted">' + (r.enabled ? "did not fire" : "disabled") + '</span>') +
      '</td><td class="num">' + (r.kind === "constraint" ? "—" : fmtNum(r.admitted)) + '</td></tr>';
  }).join("");
  $("svc-summary").innerHTML = '<div id="svc-summary-card"><div class="chips">' + modeChip(s.accuracy_available) +
    '<span class="chip">' + esc((s.source_type || "").toUpperCase()) + '</span><span class="chip"' + (fm.note ? ' title="' + esc(fm.note) + '"' : "") + '>Features: ' + esc(fm.status || "—") + (fm.format_name ? ", " + esc(fm.format_name) : "") + '</span>' +
    (s.class_scheme ? '<span class="chip">' + esc(s.class_scheme.name) + '</span>' : "") + (big.capped ? '<span class="chip warn">large input, sampled</span>' : "") + '</div>' +
    (big.capped ? '<div class="notice warn" id="svc-large-input" role="status">' + esc(big.message) + '</div>' : "") +
    (samp.description ? '<p id="svc-sampling" class="small"><b>Sampling:</b> ' + esc(samp.description) + '.</p>' : "") +
    '<details><summary>Data summary</summary><div class="grid two" style="margin-top:10px"><dl class="kv" id="svc-counts">' +
    counts.map(function (kv) { return '<dt>' + esc(kv[0]) + '</dt><dd>' + esc(kv[1]) + '</dd>'; }).join("") + '</dl><div>' +
    (csv && s.accuracy_available ? distTable(s) : '<p class="muted small">There are no reference labels, so the run measures efficiency. Answers are still checked for validity.</p>') + '</div></div></details>' +
    '<details><summary>Flow-selection rules (' + (s.rules_fired || []).length + ' fired)</summary><div class="table-wrap"><table id="svc-rules-table"><thead><tr><th>Rule</th><th>Type</th><th>Result</th><th class="num">Flows admitted</th></tr></thead><tbody>' +
    rules + '</tbody></table></div></details>' +
    '<div class="actions" id="wiz-downloads"><span class="small muted" style="align-self:center">Prepared set <span class="mono" id="svc-set-id-value">' + esc(s.prepared_set) + '</span>:</span>' +
    (s.downloads || []).map(function (f) {
      return dlLink("/api/prepared/" + setPath(s.prepared_set) + "/" + enc(f), String(s.prepared_set).split("/").pop() + "_" + f, f).replace('<a ', '<a data-dl="' + esc(f) + '" ');
    }).join("") + '</div></div>';
}
function renderModels() {
  var box = $("svc-models"), models = (CONFIG && CONFIG.canonical_models && CONFIG.canonical_models.length) ? CONFIG.canonical_models : STUDY_MODELS;
  box.innerHTML = models.map(function (m, i) {
    return '<label class="model"><input type="checkbox" name="model" value="' + esc(m) + '" id="svc-model-' + i + '"><span class="mono">' + esc(m) + '</span></label>';
  }).join("");
  box.querySelectorAll("input").forEach(function (inp) { inp.addEventListener("change", syncRun); });
  var defs = WIZ.want || (SETTINGS && SETTINGS.default_models) || [];
  box.querySelectorAll("input").forEach(function (inp) { if (defs.indexOf(inp.value) >= 0) inp.checked = true; });
  syncRun();
  loadCatalog().then(function (cat) { renderStudyNotes(cat); renderExtras(cat); tickWanted(); });
}
// tick the wanted models (Run again, a preset) that are extras, once the catalogue is shown
function tickWanted() {
  if (!WIZ.want) return;
  var hit = false;
  modelInputs().forEach(function (inp) { if (WIZ.want.indexOf(inp.value) >= 0 && !inp.checked && !inp.dataset.locked) { inp.checked = true; hit = true; } });
  if (hit && $("svc-extra") && document.querySelector("#svc-extra-models input:checked")) $("svc-extra").open = true;
  syncRun();
}
// run presets: models + options under a name, kept in this browser
function runPresets() { try { return JSON.parse(localStorage.getItem("agentmeter.runPresets") || "[]") || []; } catch (e) { return []; } }
function saveRunPresets(list) { try { localStorage.setItem("agentmeter.runPresets", JSON.stringify(list)); return true; } catch (e) { return false; } }
function fillPresets(sel) {
  var list = runPresets();
  $("wiz-preset").innerHTML = '<option value="">' + (list.length ? "Choose a saved preset…" : "No presets yet") + '</option>' + list.map(function (p, i) {
    return '<option value="' + i + '"' + (sel === p.name ? " selected" : "") + '>' + esc(p.name + " — " + p.models.map(short).join(", ")) + '</option>';
  }).join("");
  $("wiz-preset-del").hidden = !$("wiz-preset").value;
}
$("wiz-preset").addEventListener("change", function () {
  var p = runPresets()[+this.value];
  $("wiz-preset-del").hidden = !p;
  if (!p) return;
  modelInputs().forEach(function (inp) { if (!inp.dataset.locked) inp.checked = p.models.indexOf(inp.value) >= 0; });
  WIZ.want = p.models.slice(); tickWanted();
  $("svc-ids-tau").value = p.ids && p.ids.threshold ? p.ids.threshold : "";
  syncRun();
});
$("wiz-preset-save").addEventListener("click", function () {
  var models = picked();
  if (!models.length) { alert("Tick at least one model first."); return; }
  var name = (prompt("Name this preset (models and options):", models.map(short).join(" + ")) || "").trim().slice(0, 60);
  if (!name) return;
  var list = runPresets().filter(function (p) { return p.name !== name; });
  list.unshift({ name: name, models: models, ids: idsBody() });
  if (!saveRunPresets(list.slice(0, 20))) { alert("This browser does not allow saving presets."); return; }
  fillPresets(name);
});
$("wiz-preset-del").addEventListener("click", function () {
  var i = +$("wiz-preset").value, list = runPresets();
  if (!list[i]) return;
  confirmAction({ title: 'Delete the preset \u201c' + list[i].name + '\u201d?', ok: "Delete preset", danger: true }).then(function (yes) {
    if (yes) { list.splice(i, 1); saveRunPresets(list); fillPresets(); }
  });
});
// study models: on this server already, or downloaded the first time a benchmark uses them
function renderStudyNotes(cat) {
  if (!cat || !(HEALTH && HEALTH.provider === "real")) return;
  (cat.study || []).forEach(function (x) {
    var inp = document.querySelector('#svc-models input[value="' + x.id + '"]');
    if (!inp || inp.parentNode.querySelector(".study-note")) return;
    var note = document.createElement("span");
    note.className = "small muted study-note";
    note.textContent = x.on_disk ? ", on this server ✓" : ", first use downloads " +
      (x.download_gb != null ? "~" + fmtNum(x.download_gb, 1) + " GB (~" + fmtNum(Math.max(1, x.download_min), 0) + " min)" : "the model");
    inp.parentNode.appendChild(note);
  });
}
// the extra models (GET /api/models/catalog): only ok / warning ones are offered
var CATALOG = null;
function loadCatalog() {
  if (CATALOG) return Promise.resolve(CATALOG);
  return api("/api/models/catalog").then(function (r) { if (r.ok) CATALOG = r.data; return r.ok ? r.data : null; });
}
function extraCard(x, i, real) {
  var locked = false;
  var line1 = [x.params_b != null ? fmtNum(x.params_b, 1) + " B params" : null,
               x.vram_4bit_gb != null ? "~" + fmtNum(x.vram_4bit_gb, 1) + " GB VRAM at 4-bit" : null,
               x.fits_gpu === true ? "✓ fits this GPU" : null].filter(Boolean).join(", ");
  var line2 = (x.original_format === "original weights" ? "original weights → 4-bit NF4" : "4-bit NF4") +
              (x.license ? ", " + x.license : "") + (x.downloads != null ? ", " + fmtNum(x.downloads) + " downloads" : "");
  var line3 = x.on_disk ? "On this server ✓" : x.download_gb != null ? "First use downloads ~" + fmtNum(x.download_gb, 1) + " GB (~" + fmtNum(Math.max(1, x.download_min), 0) + " min)" : "";
  // offered models are suitability "ok": what remains to show are notices (licence; the download is line3)
  var notes = (x.reasons || []).filter(function (r) { return r.outcome === "info" && r.rule !== "large_download" && r.rule !== "gated"; });
  var lic = x.licence_verification ? '<span class="small"><span class="chip warn">licence verification required</span> ' +
    (real && x.verification && x.verification.status === "ok"
      ? "Gated on Hugging Face. The licence has been accepted for this server, since the admin's 4-bit verification downloaded it."
      : "Gated on Hugging Face. The licence must be accepted with the server's Hugging Face account before it can be used.") + '</span>' : "";
  return '<label class="model card-model' + (locked ? " locked" : "") + '"><input type="checkbox" name="model" value="' + esc(x.id) + '" id="svc-extra-' + i + '"' +
    (locked ? ' data-locked="1" disabled' : "") + '><span class="mm"><span class="mono">' + esc(x.id) + '</span>' +
    (line1 ? '<span class="small">' + esc(line1) + '</span>' : "") + '<span class="small muted">' + esc(line2) + '</span>' +
    (line3 ? '<span class="small muted">' + esc(line3) + '</span>' : "") +
    (x.type ? '<span class="small muted">Architecture: ' + esc(x.type) + '</span>' : "") +
    (x.note ? '<span class="small muted">' + esc(x.note) + '</span>' : "") + lic +
    notes.map(function (r) { return '<span class="small"><span class="chip">' + esc(r.rule.replace(/_/g, " ")) + '</span> ' + esc(r.note) + '</span>'; }).join("") +
    '</span></label>';
}
function renderExtras(cat) {
  var det = $("svc-extra"), box = $("svc-extra-models");
  if (!det) return;
  if (!cat) { det.hidden = true; return; }
  var avail = (cat.extra || []).filter(function (x) { return x.available; });
  var others = (cat.extra || []).length - avail.length;
  det.hidden = false;
  $("svc-extra-count").textContent = "(" + avail.length + ")";
  box.innerHTML = avail.map(function (x, i) { return extraCard(x, i, cat.mode === "real"); }).join("") ||
    '<p class="muted small">No extra models are available on this server right now.</p>';
  // name each model that is not offered, with its reason (and the rule's note for a suitability problem)
  var why = (cat.extra || []).filter(function (x) { return !x.available; }).map(function (x) {
    var r = x.unavailable_reason || (x.status === "unknown" ? "model info unavailable" : "not suitable");
    var n = (x.reasons || []).filter(function (y) { return y.outcome === (x.status === "blocked" ? "block" : "warn"); })[0];
    return x.id.split("/").pop() + " (" + r + (n && (x.status === "warning" || x.status === "blocked") ? ", " + n.note.replace(/\.$/, "") : "") + ")";
  });
  $("svc-extra-note").textContent = others ? others + " more listed model" + (others === 1 ? " is" : "s are") + " not available here, " + (why.length < 2 ? why.join("") : why.slice(0, -1).join(", ") + " and " + why[why.length - 1]) + "." : "";
  var defs = (SETTINGS && SETTINGS.default_models) || [];
  box.querySelectorAll("input").forEach(function (inp) {
    if (defs.indexOf(inp.value) >= 0 && !inp.dataset.locked && picked().length < ((CONFIG && CONFIG.max_models) || 2)) { inp.checked = true; det.open = true; }
    inp.addEventListener("change", syncRun);
  });
  syncRun();
}
function modelInputs() { return document.querySelectorAll("#svc-models input, #svc-extra-models input"); }
function picked() { return Array.prototype.filter.call(modelInputs(), function (i) { return i.checked; }).map(function (i) { return i.value; }); }
// ---------- compare model sizes and number of agents (an experiment: one run per combination) ----------
var CMP_PIPES = { 1: "Triage", 2: "Analyse → Conclude", 3: "Perceive → Reason → Conclude", 4: "Perceive → Reason → Decide → Act",
  6: "Perceive → Enrich → Reason → Verify → Decide → Act", 8: "Perceive → Enrich → Hypothesise → Reason → Verify → Critique → Decide → Act",
  10: "Perceive → Enrich → Hypothesise → Reason → Compare → Verify → Critique → Decide → Justify → Act" };
var CMP_LABEL = { "4bit": "4-bit", "8bit": "8-bit", "16bit": "16-bit" };
var CMP_MAX = 12;
function cmpSel() {
  var vals = function (id) { return Array.prototype.filter.call(document.querySelectorAll("#" + id + " input"), function (i) { return i.checked; }).map(function (i) { return i.value; }); };
  var p = vals("cmp-prec"), a = vals("cmp-agents").map(Number);
  return { precisions: p.length ? p : ["4bit"], agents: a.length ? a : [4] };
}
function cmpRuns() { var c = cmpSel(); return picked().length * c.precisions.length * c.agents.length; }
function cmpIsStandard() { var c = cmpSel(); return c.precisions.join() === "4bit" && c.agents.join() === "4"; }
function cmpRender() {
  var c = cmpSel(), n = picked().length, runs = cmpRuns();
  $("cmp-pipes").innerHTML = c.agents.map(function (k) { return "<li><b>" + k + " agent" + (k === 1 ? "" : "s") + "</b>: " + esc(CMP_PIPES[k]) + "</li>"; }).join("");
  var line = $("cmp-line");
  if (cmpIsStandard()) { line.textContent = "A standard benchmark: 4-bit, 4 agents."; line.className = "small"; return; }
  line.className = "small" + (runs > CMP_MAX ? " warn-text" : "");
  line.textContent = runs + " run" + (runs === 1 ? "" : "s") + ": " + n + " model" + (n === 1 ? "" : "s") + " × " + c.precisions.length + " size" + (c.precisions.length === 1 ? "" : "s") +
    " × " + c.agents.length + " number" + (c.agents.length === 1 ? "" : "s") + " of agents" + (runs > CMP_MAX ? ". At most " + CMP_MAX + " runs per experiment, so untick a model, a size or a number of agents." : ". They run one after another, never at the same time.");
}
document.querySelectorAll("#cmp-prec input, #cmp-agents input").forEach(function (i) { i.addEventListener("change", function () { syncRun(); }); });
function syncRun() {
  var max = (CONFIG && CONFIG.max_models) || 2, n = picked().length;
  if ($("cmp-line")) cmpRender();
  modelInputs().forEach(function (inp) { inp.disabled = !!inp.dataset.locked || (!inp.checked && n >= max); });
  if ($("wiz-settings")) renderSettings();
  var failed = $("wiz-data-chip").textContent === "Failed", off = !ONLINE, noIds = !!HEALTH && !HEALTH.ids;
  var runs = cmpRuns(), exp = !cmpIsStandard();
  $("svc-run").disabled = n < 1 || n > max || failed || off || noIds || runs > CMP_MAX;
  $("svc-run").textContent = off ? "GPU offline" : noIds ? "No IDS model" : !(WIZ.set || failed) ? "Run when data is ready" : exp ? "Run " + runs + " benchmark" + (runs === 1 ? "" : "s") : "Run benchmark";
  $("svc-model-hint").textContent = n >= max ? "Maximum of " + max + " models selected. Deselect one to change."
    : n === 1 ? "1 model selected. Add another to compare, or run it alone." : n > 1 ? n + " models selected, up to " + max + ". Each runs on its own, one after another." : "Select 1 to " + max + " models.";
}
// picked models (study or extra) that are not on this server yet: downloaded before the benchmark starts
function downloadLine() {
  if (!CATALOG || !(HEALTH && HEALTH.provider === "real")) return [];
  var need = (CATALOG.study || []).concat(CATALOG.extra || []).filter(function (x) { return picked().indexOf(x.id) >= 0 && !x.on_disk; });
  if (!need.length) return [];
  var gb = need.reduce(function (a, x) { return a + (x.download_gb || 0); }, 0), min = need.reduce(function (a, x) { return a + (x.download_min || 0); }, 0);
  return [["First run", "downloads ~" + fmtNum(gb, 1) + " GB (~" + fmtNum(Math.max(1, min), 0) + " min) before the benchmark starts. Later runs on this rental reuse it. Download time is not measured."]];
}
// time and cost before Run, from earlier sessions of the same models on this GPU (INSIGHTS.runEstimate)
function fmtDur(sec) {
  if (sec == null) return "—";
  if (sec < 90) return Math.max(1, Math.round(sec)) + " s";
  if (sec < 5400) return Math.round(sec / 60) + " min";
  return Math.floor(sec / 3600) + " h " + Math.round((sec % 3600) / 60) + " min";
}
function estimateLine() {
  var models = picked(), n = WIZ.summary ? WIZ.summary.rows_selected : null;
  if (!window.INSIGHTS || !models.length || !n || !WIZ.history) return [];
  var real = HEALTH && HEALTH.provider === "real";
  var e = INSIGHTS.runEstimate(WIZ.history, { models: models, n_flows: n, demo: !real, gpu: real && HEALTH.gpu ? HEALTH.gpu.name : null });
  var where = real ? "on " + (e.gpu || "this GPU") : "in demo mode";
  if (!e.complete) return [["Estimate", "no earlier session of " + e.unknown.map(short).join(" or ") + " " + where + " yet, so no estimate"]];
  var src = Math.max.apply(null, e.models.map(function (m) { return m.sessions; }));
  if (!cmpIsStandard()) {                       // every combination: scaled by its number of agents (rough)
    var c = cmpSel(), k = c.agents.reduce(function (s, a) { return s + a / 4; }, 0) * c.precisions.length;
    return [["Estimate", "roughly " + fmtDur(e.seconds * k) + " of model time for all " + cmpRuns() + " runs" + (e.cost_usd != null ? ", about $" + fmtNum(e.cost_usd * k, 2) : "") +
      " (scaled from earlier 4-agent sessions " + where + " by the number of agents; 8-bit and 16-bit can differ, plus model loading)"]];
  }
  var out = [["Estimate", "about " + fmtDur(e.seconds) + " of model time" + (e.cost_usd != null ? ", about $" + fmtNum(e.cost_usd, 2) : "") +
    " (from earlier sessions " + where + ", plus model loading and first-use downloads)" + (src < 2 ? ", from one earlier session only" : "")]];
  var fg = models.length > 1 && INSIGHTS.flowGuidance ? INSIGHTS.flowGuidance(WIZ.history, { models: models, n_flows: n, demo: !real, gpu: real && HEALTH.gpu ? HEALTH.gpu.name : null }) : null;
  if (fg) out.push(["Precision", "with " + fmtNum(n) + " flows, a latency difference of about " + fmtNum(100 * fg.detectable, 0) + "% or more between these models can be told apart" +
    (fg.detectable > 0.10 ? ". For 10% use about " + fmtNum(fg.flows_for_10) + " flows" : "") + (fg.detectable > 0.05 ? (fg.detectable > 0.10 ? ", for 5% about " : ". For 5% use about ") + fmtNum(fg.flows_for_5) : "") +
    " (from the latency spread of earlier runs, at 5% significance and 80% power)"]);
  return out;
}
function loadHistory() {
  WIZ.history = null;
  allSessions().then(function (r) { if (r.ok) { WIZ.history = r.data.sessions || []; renderSettings(); } });
}
function renderSettings() {
  var s = WIZ.summary;
  $("wiz-settings").innerHTML = [
    ["Provider", HEALTH && HEALTH.provider === "real" ? "Real GPU, " + cmpSel().precisions.map(function (p) { return CMP_LABEL[p]; }).join(", ") + (HEALTH.engine === "vllm" ? ", vLLM engine" : "") : "Demo (no GPU)"],
    ["Flows", s ? fmtNum(s.rows_selected) : "from step 1 (" + ($("svc-max-flows").value || 50) + " max)"],
    ["Evaluation", s ? (s.accuracy_available ? "efficiency, with a reference-label validity check" : "efficiency (no reference labels)") : "known when the data is ready"],
    ["Agents", cmpSel().agents.length === 1 ? CMP_PIPES[cmpSel().agents[0]] : cmpSel().agents.join(", ") + " agents (one run each)"]
  ].concat(estimateLine()).concat(downloadLine()).map(function (kv) { return '<dt>' + esc(kv[0]) + '</dt><dd>' + esc(kv[1]) + '</dd>'; }).join("");
}

// The IDS gate is always on: the IDS detects and the LLM agents triage only the flows it
// flags. A GPU server without an IDS model (GET /health "ids") cannot run a benchmark.
function idsOptionSync() {
  var ids = (HEALTH || {}).ids, tau = $("svc-ids-tau");
  if (!tau) return;
  tau.disabled = !ids;
  syncRun();
  if (!ids) { $("svc-ids-note").textContent = "This GPU server has no IDS model. Upload one in Admin, IDS model, before running a benchmark."; return; }
  var h = ids.holdout || {};
  $("svc-ids-note").innerHTML = (ids.demo ? '<span class="chip warn">Demo IDS</span> trained on the 300 bundled flows, for trying the flow only. ' : "") +
    'IDS <span class="mono">' + esc(ids.version || ids.name) + '</span>, threshold ' + fmtNum(ids.threshold, 3) +
    ' (' + esc(ids.threshold_rule || "") + '). On its hold-out flows it has recall ' + fmtPct(h.recall) + ', false-positive rate ' + fmtPct(h.false_positive_rate) + '.';
}
function idsBody() {
  var t = parseFloat($("svc-ids-tau").value);
  return isFinite(t) && t > 0 && t <= 1 ? { threshold: t } : true;
}
$("svc-run").addEventListener("click", function () {
  var err = $("svc-run-error"), body = { models: picked(), provider: CONFIG.provider, base_config: CONFIG.base_config };
  err.hidden = true;
  // The wizard's own preparation: always after_prepare (one session, counted once; the
  // server uses the prepared set directly when it is already done). Reused sets: run.
  if (WIZ.prep) body.after_prepare = WIZ.prep; else body.run = WIZ.set;
  body.ids = idsBody();
  if (!cmpIsStandard()) { var cs = cmpSel(); body.precisions = cs.precisions; body.agents = cs.agents; }
  $("svc-run").disabled = true;
  api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(function (r) {
    if (!r.ok) { showError(err, r.data); syncRun(); return; }
    watchForFinish(r.data.job_id, picked());             // always: a notification when it finishes
    location.hash = "#/new/run/" + enc(r.data.job_id);
  });
});
// "tell me when it finishes": a browser notification (or a message in the page) while the app is open
function watchedRuns() { try { return JSON.parse(localStorage.getItem("agentmeter.watch") || "[]") || []; } catch (e) { return []; } }
function setWatched(l) { try { localStorage.setItem("agentmeter.watch", JSON.stringify(l.slice(-10))); } catch (e) { /* this tab only */ } }
function watchForFinish(id, models) {
  setWatched(watchedRuns().filter(function (x) { return x.id !== id; }).concat([{ id: id, models: models }]));
  try { if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } catch (e) { /* not offered */ }
}
function toast(html) {
  var t = document.createElement("div");
  t.className = "toast"; t.setAttribute("role", "status"); t.innerHTML = html + '<button type="button" class="iconbtn" aria-label="Close">×</button>';
  t.querySelector("button").addEventListener("click", function () { t.remove(); });
  document.body.appendChild(t);
  setTimeout(function () { t.remove(); }, 20000);
}
setInterval(function () {
  var list = watchedRuns();
  if (!list.length || !ONLINE) return;
  list.forEach(function (w) {
    api("/api/jobs/" + enc(w.id)).then(function (r) {
      if (!r.ok) { if (r.status === 404) setWatched(watchedRuns().filter(function (x) { return x.id !== w.id; })); return; }
      var st = r.data.status;
      if (["done", "failed", "interrupted", "stopped"].indexOf(st) < 0) return;
      setWatched(watchedRuns().filter(function (x) { return x.id !== w.id; }));
      var ok = st === "done", what = (w.models || []).map(nick).join(", ");
      var title = ok ? "Benchmark finished" : "Benchmark " + st;
      try {
        if (window.Notification && Notification.permission === "granted" && document.hidden) {
          var n = new Notification(title, { body: what + (ok ? ". The results are ready." : ""), tag: w.id });
          n.onclick = function () { window.focus(); location.hash = ok ? "#/session/" + enc(w.id) : "#/new/run/" + enc(w.id); n.close(); };
          return;
        }
      } catch (e) { /* fall back to the page */ }
      toast('<b>' + esc(title) + '</b> ' + esc(what) + '. <a href="' + (ok ? "#/session/" : "#/new/run/") + enc(w.id) + '">' + (ok ? "Open the results" : "See what happened") + '</a>');
    });
  });
}, 15000);

// "19 flows × 3 models": the flows each model triages (only those the IDS flagged), not their product
function flowsLine(p) {
  var n = p.flows_per_model, m = p.n_models || 1;
  if (n == null) return (p.completed || 0) + " / " + (p.total || 0) + " flow answers";
  return fmtNum(n) + " flow" + (n === 1 ? "" : "s") + (m > 1 ? " × " + m + " models" : "");
}
function progressLine(p, pct) {
  if (p.all_flows_done) return "Every flow measured (" + flowsLine(p) + "). Writing the explanation, up to 20 minutes.";
  if (p.flows_per_model == null) return (p.completed || 0) + " / " + (p.total || 0) + " flow answers, " + pct + "%";
  var cur = p.current_model, done = cur && p.per_model ? (p.per_model[cur] || 0) : 0;
  return (p.n_models > 1 && p.model_index ? "Model " + p.model_index + " of " + p.n_models + ", " : "") +
    (cur ? short(cur) + ": " : "") + fmtNum(done) + " / " + fmtNum(p.flows_per_model) + " flows (" + pct + "% of the benchmark)";
}
// step 3 (also the "Watch" view of a running session)
function renderProgress(j, target) {
  var p = j.progress || {}, prep = j.prepare, st = target.status, bar = target.bar;
  var label = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", interrupted: "Failed", stopped: "Stopped" }[j.status] || j.status;
  st.textContent = label; st.className = "status " + (label === "Queued" ? "Running" : label);
  target.models.textContent = (j.models || []).join("  vs  ") + (j.run_name ? ", " + j.run_name : "");
  var pct = 0, text, phase;
  if (prep && prep.status !== "done") {
    phase = "Preparing your data";
    var pp = prep.progress || {};
    text = (PHASES[pp.phase] || pp.phase || prep.status) + (pp.phase === "downloading" && pp.total ? ", " + Math.round(100 * pp.bytes / pp.total) + "%" : "") +
      ". The benchmark starts as soon as the data is ready.";
    bar.classList.add("indet");
  } else {
    phase = j.status === "done" ? "Benchmark finished" : j.status === "queued" ? "Waiting for the GPU" : "Benchmarking";
    if (j.experiment) phase = "Run " + j.experiment.index + " of " + j.experiment.total + ": " + (CMP_LABEL[j.precision] || j.precision) + ", " + j.agents + " agent" + (j.agents === 1 ? "" : "s") + " · " + phase.toLowerCase();
    pct = p.total ? Math.round(100 * p.completed / p.total) : 0;
    var dl = j.model_download, downloading = j.status === "running" && !!dl && dl.phase === "downloading";
    if (downloading) {
      phase = "Downloading model";
      pct = dl.total_bytes ? Math.min(100, Math.round(100 * dl.done_bytes / dl.total_bytes)) : 0;
      text = "Downloading " + short(dl.model) + (dl.count > 1 ? " (" + dl.index + " of " + dl.count + ")" : "") + ", " +
        fmtNum((dl.done_bytes || 0) / 1073741824, 1) + (dl.total_bytes ? " / " + fmtNum(dl.total_bytes / 1073741824, 1) : "") + " GB" +
        (dl.total_bytes ? ", " + pct + "%" : "") + ". This is the first use on this server. The benchmark starts when it finishes, and the download is not measured.";
      bar.classList.toggle("indet", !dl.total_bytes);
    }
    if (downloading) { /* text and bar set above */ }
    else if (j.status === "queued") text = "Queued, " + (j.queue_position ? j.queue_position + " job(s) ahead" : "starting next") + ". One benchmark runs at a time." +
      (!CP ? "" : SETTINGS && SETTINGS.telegram_chat_id && SETTINGS.notify_on && SETTINGS.notify_on !== "none"
        ? " You'll get a Telegram message when it finishes, so you can close this page."
        : " To get a Telegram message when it finishes, set your Telegram chat in Settings.");
    else if (j.status === "running") text = progressLine(p, pct);
    else if (j.status === "done") text = flowsLine(p) + ", done";
    else if (j.status === "stopped") text = "Stopped" + (j.stopped_by ? " by " + j.stopped_by : "") + " after " + (p.completed || 0) + " / " + (p.total || 0) + " flows, not scored.";
    else text = (p.completed || 0) + " / " + (p.total || 0) + " flows completed before it stopped.";
    if (!downloading) bar.classList.toggle("indet", j.status === "running" && !p.completed);
  }
  if (j.status === "stopped") {
    phase = "Benchmark stopped";
    if (!target.text.parentNode.querySelector(".again-link")) {
      var a = document.createElement("div"); a.className = "actions again-link";
      a.innerHTML = '<a class="btn" href="#/new">New benchmark</a><span class="small muted">Upload the same file and pick the same number of flows to measure them again.</span>';
      target.text.parentNode.insertBefore(a, target.error);
    }
  }
  if (target.stop) wireStop(j, target);
  target.phase.textContent = phase;
  target.fill.style.width = pct + "%";
  bar.setAttribute("aria-valuenow", String(pct));
  target.text.textContent = text;
  if (j.status === "failed" || j.status === "interrupted") {
    showError(target.error, { error: j.error || j.message, code: j.status });
    target.resume.hidden = false;
  }
}
// ---------- LIVE TELEMETRY (run page and the watch view of a running session) ----------
// What the system is doing right now, from GET /api/jobs/<id>/telemetry: the steps (queue,
// data, Hugging Face, 4-bit load, flows, scoring), the GPU now (utilisation, memory, power,
// temperature; never simulated: a demo run says it has no GPU), each flow's agents, and the log.
var TEL_STEPS = [["queue", "Queue"], ["data", "Data"], ["hf", "Hugging Face"], ["load", "Load model (4-bit)"],
                 ["flows", "Run flows"], ["score", "Score & analyse"], ["explain", "Write explanation"], ["done", "Done"]];
var TEL_STEP_OF = { queued: 0, start: 0, data: 1, hf: 2, download: 2, downloaded: 2, worker: 3, load: 3, loaded: 3,
                    measure: 4, flow: 4, extra: 4, idle: 4, worker_done: 4, scoring: 5, analysis: 5, results: 5,
                    explain: 6, explained: 6, done: 7 };
function startTelemetry(host, id) {
  // one panel at a time: a panel left in a hidden view (the run page) must not take this one's updates
  document.querySelectorAll("#tel-card").forEach(function (c) { if (!host.contains(c)) c.remove(); });
  var q = function (k) { return host.querySelector("#" + k); };
  var token = pollToken, since = 0, st = { step: 0, events: [], gpu: [], lastFlow: null, flows: [], dl: null, fail: null, model: null, of: null };
  host.innerHTML = '<div class="card" id="tel-card"><div class="row" style="margin-top:0;justify-content:space-between"><h2>Live telemetry</h2>' +
    '<span class="small muted" id="tel-now" aria-live="polite"></span></div>' +
    '<ol class="tel-steps" id="tel-steps" aria-label="Benchmark steps"></ol>' +
    '<div class="tel-grid"><div class="tel-box" id="tel-gpu"></div><div class="tel-box" id="tel-flow"></div></div>' +
    '<h3 style="margin:14px 0 6px">Event log</h3><div class="tel-log mono" id="tel-log" role="log" aria-label="Event log" tabindex="0"></div></div>';
  function hhmmss(t) { var d = new Date(t * 1000); return isNaN(d) ? "" : d.toTimeString().slice(0, 8); }
  function spark(vals, max, color) {
    var v = vals.filter(function (x) { return x != null; });
    if (v.length < 2) return "";
    var W = 120, H = 28, m = max || Math.max.apply(null, v) || 1;
    var pts = vals.map(function (x, i) { return x == null ? null : (i * W / (vals.length - 1)).toFixed(1) + "," + (H - 2 - (H - 4) * Math.min(1, x / m)).toFixed(1); }).filter(Boolean).join(" ");
    return '<svg class="tel-spark" viewBox="0 0 ' + W + ' ' + H + '" aria-hidden="true"><polyline points="' + pts + '" style="stroke:' + color + '"/></svg>';
  }
  function renderSteps() {
    q("tel-steps").innerHTML = TEL_STEPS.map(function (x, i) {
      var cls = st.fail && i === st.step ? "bad" : i < st.step || st.step === TEL_STEPS.length - 1 ? "done" : i === st.step ? "now" : "";
      var sub = i === 3 || i === 4 ? (st.model ? '<span class="small muted">' + esc(short(st.model)) + (st.of ? ", " + st.of : "") + '</span>' : "") : "";
      return '<li class="' + cls + '"' + (cls === "now" ? ' aria-current="step"' : "") + '><span class="dot" aria-hidden="true"></span><span>' + esc(x[1]) + sub + '</span></li>';
    }).join("");
  }
  function renderGpu(r) {
    var box = q("tel-gpu"), g = r.gpu;
    if (!g) {
      box.innerHTML = '<h3>GPU now</h3><p class="muted small" style="margin:0">' + (r.gpu_reason === "demo mode: no GPU"
        ? "Demo mode. This run has no GPU, so there are no live GPU readings, and nothing is simulated."
        : r.gpu_reason === "finished" ? "The run has finished." : "No live GPU reading from the backend.") + '</p>';
      return;
    }
    var h = st.gpu.slice(-60);
    var cell = function (label, val, sp) { return '<div class="tel-metric"><span class="small muted">' + label + '</span><b>' + val + '</b>' + (sp || "") + '</div>'; };
    box.innerHTML = '<h3>GPU now <span class="small muted">' + esc(g.name || "") + '</span></h3><div class="tel-metrics">' +
      cell("Utilisation", fmtNum(g.util_pct) + " %", spark(h.map(function (x) { return x.util_pct; }), 100, "var(--series-1)")) +
      cell("Memory", fmtNum(g.mem_used_mb / 1024, 1) + " / " + fmtNum(g.mem_total_mb / 1024, 1) + " GB", spark(h.map(function (x) { return x.mem_used_mb; }), g.mem_total_mb, "var(--series-2)")) +
      cell("Power", g.power_w == null ? "—" : fmtNum(g.power_w) + " W", spark(h.map(function (x) { return x.power_w; }), null, "var(--accent)")) +
      cell("Temperature", g.temp_c == null ? "—" : fmtNum(g.temp_c) + " °C") + '</div>';
  }
  function renderFlow() {
    var box = q("tel-flow");
    if (st.dl && st.step === 2) {
      var d = st.dl, pct = d.total_bytes ? Math.min(100, 100 * d.done_bytes / d.total_bytes) : null;
      box.innerHTML = '<h3>Downloading from Hugging Face</h3><p class="small" style="margin:0 0 6px">' + esc(short(d.model)) + ': ' + fmtNum((d.done_bytes || 0) / 1073741824, 2) + (d.total_bytes ? " / " + fmtNum(d.total_bytes / 1073741824, 2) : "") + ' GB' +
        (d.mb_per_s ? ', ' + fmtNum(d.mb_per_s) + ' MB/s' : "") + '</p><div class="bar' + (pct == null ? " indet" : "") + '"><span style="width:' + (pct || 0) + '%"></span></div>';
      return;
    }
    var f = st.lastFlow;
    if (!f) { box.innerHTML = '<h3>Pipeline</h3><p class="muted small" style="margin:0">Each flow passes through the agents one after another. The latest flow appears here.</p>'; return; }
    var mine = st.flows.filter(function (x) { return x.model === f.model; }).slice(-10);
    var secs = mine.reduce(function (a, x) { return a + (x.total_s || 0); }, 0);
    var outTok = mine.reduce(function (a, x) { return a + (x.agents || []).reduce(function (b, y) { return b + (y.out || 0); }, 0); }, 0);
    var max = Math.max.apply(null, (f.agents || []).map(function (a) { return a.s || 0; })) || 1;
    box.innerHTML = '<h3>Latest flow <span class="small muted">' + esc(short(f.model)) + ', ' + esc(f.scenario) + ', ' + f.n + ' of ' + f.of + '</span></h3>' +
      '<div class="tel-agents">' + (f.agents || []).map(function (a) {
        return '<div class="tel-agent"><span class="small">' + esc(agName(a.agent)) + '</span><span class="track" aria-hidden="true"><span class="fill" style="width:' + Math.max(2, 100 * (a.s || 0) / max) + '%;background:' + agColor(a.agent) + '"></span></span>' +
          '<span class="small mono">' + fmtTime(a.s) + ', ' + fmtNum((a.in || 0) + (a.out || 0)) + ' tok</span></div>';
      }).join("") + '</div><p class="small muted" style="margin:6px 0 0">Last ' + mine.length + ' flow' + (mine.length === 1 ? "" : "s") + ' of this model took on average ' +
      fmtTime(secs / (mine.length || 1)) + ' per flow' + (secs > 0 ? ', ' + fmtNum(outTok / secs, 1) + ' output tokens/s' : "") + '.</p>';
  }
  function renderLog(fresh) {
    var log = q("tel-log"), stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.insertAdjacentHTML("beforeend", fresh.map(function (e) {
      return '<div class="tel-line tel-' + esc(e.stage) + '"><span class="muted">' + hhmmss(e.t) + '</span> ' + esc(e.msg) + '</div>';
    }).join(""));
    while (log.childNodes.length > 400) log.removeChild(log.firstChild);
    if (stick) log.scrollTop = log.scrollHeight;
  }
  function apply(r) {
    r.events.forEach(function (e) {
      if (e.stage in TEL_STEP_OF) st.step = Math.max(e.stage === "worker" || e.stage === "load" || e.stage === "loaded" ? Math.min(st.step, 4) : st.step, TEL_STEP_OF[e.stage]);
      if (e.stage === "worker") { st.step = 3; st.model = e.model; st.of = e.count > 1 ? "model " + e.index + " of " + e.count : null; }
      if (e.stage === "download") st.dl = e;
      if (e.stage === "flow") { st.lastFlow = e; st.flows.push(e); if (st.flows.length > 200) st.flows.shift(); }
      if (e.stage === "failed" || e.stage === "stopped") st.fail = e;
    });
    if (r.gpu) st.gpu.push(r.gpu);
    renderSteps(); renderGpu(r); renderFlow(); renderLog(r.events);
    var p = r.progress || {};
    q("tel-now").textContent = r.status === "running" && p.total ? (p.all_flows_done ? "Writing the explanation" :
      p.flows_per_model != null && p.current_model ? short(p.current_model) + ": " + fmtNum((p.per_model || {})[p.current_model] || 0) + " / " + fmtNum(p.flows_per_model) + " flows"
      : (p.completed || 0) + " / " + p.total + " flow answers") : (r.message || "");
  }
  (function poll() {
    if (token !== pollToken || !q("tel-card")) return;
    api("/api/jobs/" + enc(id) + "/telemetry?since=" + since).then(function (r) {
      if (token !== pollToken || !q("tel-card")) return;
      if (!r.ok) { setTimeout(poll, 4000); return; }
      since = r.data.next;
      apply(r.data);
      if (r.data.status === "queued" || r.data.status === "running") setTimeout(poll, 1500);
    });
  })();
}

// Stop: the owner (or an admin) stops a queued or running benchmark. Final: never scored.
function canStop(j) {
  if (!CP) return true;
  var u = j.user && j.user.uname;
  return isAdmin() || (!!u && AUTH.user && u === AUTH.user.username);
}
function wireStop(j, t) {
  var b = t.stop, live = j.status === "queued" || j.status === "running";
  b.hidden = !(live && canStop(j)) || !!j.stop_requested;
  if (j.stop_requested && live) t.text.textContent = "Stopping…";
  if (b.getAttribute("data-wired") === t.id) return;
  b.setAttribute("data-wired", t.id);
  b.onclick = function () {
    confirmAction({ title: "Stop this benchmark?", ok: "Stop benchmark", cancel: "Keep running", danger: true,
      body: "Flows finished so far will not be scored. To measure them again later, start a new benchmark with the same file." }).then(function (yes) {
    if (!yes) return;
    b.disabled = true;
    api("/api/jobs/" + enc(t.id) + "/stop", { method: "POST" }).then(function (r) {
      b.disabled = false;
      if (!r.ok) { showError(t.error, r.data); return; }
      b.hidden = true; t.text.textContent = "Stopping…";
      if (t.onStopped) t.onStopped();
    });
    });
  };
}
// an experiment: after one run, watch the next one; after the last, open the comparison
function nextExperimentRun(job, token) {
  api("/api/experiments/" + enc(job.experiment.id)).then(function (r) {
    if (token !== pollToken) return;
    var jobs = r.ok ? r.data.jobs || [] : [], next = jobs.filter(function (j) { return j.status === "queued" || j.status === "running"; })[0];
    var first = jobs[0] || job;
    setTimeout(function () {
      if (token !== pollToken) return;
      location.hash = next ? "#/new/run/" + enc(next.job_id) : "#/session/" + enc(first.job_id) + "/variants";
    }, 1500);
  });
}
function watchRun(id) {
  var token = ++pollToken;
  var t = { status: $("svc-job-status"), bar: $("svc-progress"), fill: $("svc-progress-bar"), models: $("svc-job-models"),
            text: $("svc-progress-text"), phase: $("svc-job-phase"), error: $("svc-job-error"), resume: $("svc-resume"),
            stop: $("svc-stop"), id: id, onStopped: function () { watchRun(id); } };
  t.error.hidden = true; t.resume.hidden = true;
  startTelemetry($("tel-host"), id);
  $("svc-open-session").hidden = false;
  $("svc-open-session").setAttribute("href", "#/session/" + enc(id));
  t.resume.onclick = function () {
    t.resume.disabled = true;
    api("/api/jobs/" + enc(id) + "/resume", { method: "POST" }).then(function (r) {
      t.resume.disabled = false;
      if (!r.ok) { showError(t.error, r.data); return; }
      watchRun(id);
    });
  };
  (function tick() {
    if (token !== pollToken) return;
    api("/api/jobs/" + enc(id)).then(function (r) {
      if (token !== pollToken) return;
      if (!r.ok) { showError(t.error, r.data); return; }
      renderProgress(r.data, t);
      if (r.data.status === "done" && r.data.experiment) { nextExperimentRun(r.data, token); return; }
      if (r.data.status === "done") { setTimeout(function () { if (token === pollToken) location.hash = "#/session/" + enc(id); }, 2500); return; }
      if (r.data.status === "failed" || r.data.status === "interrupted" || r.data.status === "stopped") return;
      setTimeout(tick, r.data.prepare ? 1000 : 2000);
    });
  })();
}
