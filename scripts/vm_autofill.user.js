// ==UserScript==
// @name         TrustBridge Form Autofill
// @namespace    https://survey.trustbridgevn.com/
// @version      1.0.0
// @description  Floating panel that refills the dynamic survey form from the newest admin submission.
// @match        https://survey.trustbridgevn.com/forms/*
// @match        http://127.0.0.1:8010/forms/*
// @match        http://localhost:8010/forms/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * Install:
 *   1. Install Violentmonkey (or Tampermonkey).
 *   2. Extension dashboard -> create a new userscript -> paste this whole file -> save.
 *   3. Reload any /forms/<form_id> page.
 *   4. Enter the admin username/password in the bottom-right panel and press
 *      "Fill latest submission". Credentials are kept in this browser's
 *      localStorage (tb_autofill_user / tb_autofill_pass) and are only sent as
 *      HTTP Basic auth to /api/v1/submissions.
 */

(function () {
    'use strict';

    var USER_KEY = 'tb_autofill_user';
    var PASS_KEY = 'tb_autofill_pass';

    function readPref(key) {
        try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
    }
    function writePref(key, value) {
        try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
    }
    function basicAuth(user, pass) {
        var raw = user + ':' + pass;
        try { return btoa(raw); } catch (e) { return btoa(unescape(encodeURIComponent(raw))); }
    }
    function pageFormId() {
        // SCHEMA is a top-level lexical binding on the form page; with @grant none
        // this userscript shares that scope. Fall back to the URL segment otherwise.
        try {
            if (typeof SCHEMA !== 'undefined' && SCHEMA && SCHEMA.form_id) {
                return String(SCHEMA.form_id);
            }
        } catch (e) { /* not in page scope */ }
        var match = location.pathname.match(/\/forms\/([^/?#]+)/);
        return match ? decodeURIComponent(match[1]) : null;
    }
    function submissionTime(s) {
        var value = s && (s.submitted_at || s.created_at || s.createdAt || s.timestamp);
        var parsed = Date.parse(value);
        return Number.isFinite(parsed) ? parsed : 0;
    }

    // ---- Panel -----------------------------------------------------------
    var panel = document.createElement('div');
    panel.id = 'tb-autofill-panel';
    panel.style.cssText = [
        'position: fixed', 'right: 14px', 'bottom: 14px', 'z-index: 2147483647',
        'width: 236px', 'background: #0f172a', 'color: #e2e8f0',
        'border: 1px solid #334155', 'border-radius: 10px',
        'box-shadow: 0 8px 24px rgba(0,0,0,.45)',
        'font: 12px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
        'text-align: left', 'overflow: hidden'
    ].join(';');

    panel.innerHTML = [
        '<div id="tbaf-head" style="display:flex;justify-content:space-between;align-items:center;',
        'padding:8px 10px;background:#1e293b;cursor:pointer;font-weight:700;font-size:12px;color:#f8fafc;">',
        '<span>TrustBridge Autofill</span><span id="tbaf-toggle" style="color:#94a3b8;">&minus;</span></div>',
        '<div id="tbaf-body" style="padding:10px;display:flex;flex-direction:column;gap:7px;">',
        '<label style="display:flex;flex-direction:column;gap:3px;font-size:11px;color:#94a3b8;">',
        'Admin username<input id="tbaf-user" type="text" autocomplete="username" spellcheck="false" ',
        'style="background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;padding:5px 7px;font-size:12px;outline:none;width:100%;box-sizing:border-box;"></label>',
        '<label style="display:flex;flex-direction:column;gap:3px;font-size:11px;color:#94a3b8;">',
        'Password<input id="tbaf-pass" type="password" autocomplete="current-password" ',
        'style="background:#1e293b;border:1px solid #475569;border-radius:6px;color:#f8fafc;padding:5px 7px;font-size:12px;outline:none;width:100%;box-sizing:border-box;"></label>',
        '<button id="tbaf-fill" type="button" style="background:#2563eb;border:0;border-radius:6px;color:#fff;',
        'padding:7px 9px;font-size:12px;font-weight:700;cursor:pointer;">Fill latest submission</button>',
        '<div id="tbaf-status" style="font-size:11px;color:#94a3b8;min-height:15px;word-break:break-word;">Ready</div>',
        '</div>'
    ].join('');

    var head = panel.querySelector('#tbaf-head');
    var body = panel.querySelector('#tbaf-body');
    var toggle = panel.querySelector('#tbaf-toggle');
    var userInput = panel.querySelector('#tbaf-user');
    var passInput = panel.querySelector('#tbaf-pass');
    var fillButton = panel.querySelector('#tbaf-fill');
    var statusLine = panel.querySelector('#tbaf-status');

    userInput.value = readPref(USER_KEY);
    passInput.value = readPref(PASS_KEY);

    head.addEventListener('click', function () {
        var hidden = body.style.display === 'none';
        body.style.display = hidden ? 'flex' : 'none';
        toggle.innerHTML = hidden ? '&minus;' : '+';
    });
    userInput.addEventListener('input', function () { writePref(USER_KEY, userInput.value); });
    passInput.addEventListener('input', function () { writePref(PASS_KEY, passInput.value); });

    function setStatus(text, isError) {
        statusLine.textContent = text;
        statusLine.style.color = isError ? '#fca5a5' : '#86efac';
    }

    // ---- Page-context filler --------------------------------------------
    // Runs inside an injected <script> so it can reach the page's top-level
    // `answers`, `SCHEMA`, `evaluateConditionals` and `scheduleDraftSave`.
    function injectFiller(payload) {
        var jsonText = JSON.stringify(payload || {}).replace(/</g, '\\u003c');
        var code = '(function(){\n' +
            'var out;\n' +
            'try {\n' +
            '  var payload = JSON.parse(' + JSON.stringify(jsonText) + ');\n' +
            '  var fd = payload.form_data || {};\n' +
            '  var schema = (typeof SCHEMA !== "undefined" && SCHEMA) ? SCHEMA : null;\n' +
            '  var questions = (schema && Array.isArray(schema.questions)) ? schema.questions : [];\n' +
            '  if (!questions.length) { out = { error: "page SCHEMA.questions not found" }; }\n' +
            '  else {\n' +
            '    var A = null;\n' +
            '    try { A = answers; } catch (e) { A = null; }\n' +
            '    if (!A || typeof A !== "object") throw new Error("page answers state not found");\n' +
            '    var norm = function (v) {\n' +
            '      return String(v == null ? "" : v).replace(/\\u00a0/g, " ").replace(/\\s+/g, " ").trim().toLowerCase();\n' +
            '    };\n' +
            '    var blank = function (v) {\n' +
            '      return v === undefined || v === null ||\n' +
            '        (typeof v === "string" && v.trim() === "") ||\n' +
            '        (Array.isArray(v) && v.length === 0);\n' +
            '    };\n' +
            '    var fire = function (el, type) {\n' +
            '      try { el.dispatchEvent(new Event(type, { bubbles: true })); } catch (e) {}\n' +
            '    };\n' +
            '    var filled = 0, skipped = 0;\n' +
            '    questions.forEach(function (q) {\n' +
            '      var val = fd[q.id];\n' +
            '      if (q.type === "camera_webrtc") { skipped++; return; }\n' +
            '      var card = document.getElementById("card_" + q.id);\n' +
            '      if (q.type === "signature") {\n' +
            '        var sig = null;\n' +
            '        [fd[q.id], fd.driver_signature, fd.signature_png, fd.signature].some(function (cand) {\n' +
            '          if (typeof cand === "string" && /^data:image\\//.test(cand)) { sig = cand; return true; }\n' +
            '          return false;\n' +
            '        });\n' +
            '        if (!sig) { if (!blank(val)) skipped++; return; }\n' +
            '        A[q.id] = sig;\n' +
            '        var img = document.getElementById("sigImg_" + q.id);\n' +
            '        if (img) { img.src = sig; img.style.display = "block"; }\n' +
            '        var lbl = document.getElementById("sigBtnLabel_" + q.id);\n' +
            '        if (lbl) {\n' +
            '          try { lbl.textContent = I18N[currentLang].sig_done; } catch (e) { lbl.textContent = "Signature captured"; }\n' +
            '        }\n' +
            '        filled++;\n' +
            '        return;\n' +
            '      }\n' +
            '      if (blank(val)) return;\n' +
            '      if (!card) { skipped++; return; }\n' +
            '      if (q.type === "text" || q.type === "number") {\n' +
            '        var inp = card.querySelector("input.q-input") || card.querySelector("input");\n' +
            '        if (!inp) { skipped++; return; }\n' +
            '        var str = String(val);\n' +
            '        if (q.type === "number" && str.trim() !== "" && isNaN(Number(str))) { skipped++; return; }\n' +
            '        A[q.id] = val;\n' +
            '        inp.value = str;\n' +
            '        fire(inp, "input");\n' +
            '        fire(inp, "change");\n' +
            '        filled++;\n' +
            '        return;\n' +
            '      }\n' +
            '      if (q.type === "dropdown") {\n' +
            '        var sel = card.querySelector("select.q-input") || card.querySelector("select");\n' +
            '        if (!sel || sel.disabled) { skipped++; return; }\n' +
            '        var wanted = norm(val);\n' +
            '        var opt = null;\n' +
            '        Array.prototype.forEach.call(sel.options, function (o) {\n' +
            '          if (!opt && (norm(o.textContent) === wanted || norm(o.value) === wanted)) opt = o;\n' +
            '        });\n' +
            '        if (!opt) { skipped++; return; }\n' +
            '        sel.value = opt.value;\n' +
            '        A[q.id] = opt.value;\n' +
            '        fire(sel, "change");\n' +
            '        filled++;\n' +
            '        return;\n' +
            '      }\n' +
            '      var items = Array.prototype.slice.call(card.querySelectorAll(".choice-item"));\n' +
            '      if (!items.length) { skipped++; return; }\n' +
            '      if (q.type === "single_choice") {\n' +
            '        var wantedOne = norm(val);\n' +
            '        var hit = null;\n' +
            '        items.forEach(function (item) { if (!hit && norm(item.textContent) === wantedOne) hit = item; });\n' +
            '        if (!hit) { skipped++; return; }\n' +
            '        if (!hit.classList.contains("selected")) { try { hit.click(); } catch (e) {} }\n' +
            '        A[q.id] = hit.textContent.trim();\n' +
            '        filled++;\n' +
            '        return;\n' +
            '      }\n' +
            '      if (q.type === "multiple_choice") {\n' +
            '        var cands = [];\n' +
            '        if (Array.isArray(val)) cands = val;\n' +
            '        else if (typeof val === "string") {\n' +
            '          cands = [val];\n' +
            '          if (val.indexOf(",") !== -1) cands = cands.concat(val.split(/\\s*,\\s*/));\n' +
            '        } else { skipped++; return; }\n' +
            '        var wantedSet = {};\n' +
            '        cands.forEach(function (c) { var n = norm(c); if (n) wantedSet[n] = true; });\n' +
            '        var touched = false;\n' +
            '        items.forEach(function (item) {\n' +
            '          if (!wantedSet[norm(item.textContent)]) return;\n' +
            '          touched = true;\n' +
            '          if (!item.classList.contains("selected")) { try { item.click(); } catch (e) {} }\n' +
            '        });\n' +
            '        if (!touched) { skipped++; return; }\n' +
            '        A[q.id] = items.filter(function (i) { return i.classList.contains("selected"); })\n' +
            '          .map(function (i) { return i.textContent.trim(); });\n' +
            '        filled++;\n' +
            '        return;\n' +
            '      }\n' +
            '      skipped++;\n' +
            '    });\n' +
            '    try { if (typeof evaluateConditionals === "function") evaluateConditionals(); } catch (e) {}\n' +
            '    try { if (typeof scheduleDraftSave === "function") scheduleDraftSave(); } catch (e) {}\n' +
            '    out = { filled: filled, skipped: skipped };\n' +
            '  }\n' +
            '} catch (err) {\n' +
            '  out = { error: String((err && err.message) || err) };\n' +
            '}\n' +
            'try { document.documentElement.setAttribute("data-tb-autofill-result", JSON.stringify(out)); } catch (e) {}\n' +
            '})();';

        var root = document.documentElement;
        root.removeAttribute('data-tb-autofill-result');
        var script = document.createElement('script');
        script.textContent = code;
        (document.head || root).appendChild(script);
        script.remove();
        var raw = root.getAttribute('data-tb-autofill-result');
        root.removeAttribute('data-tb-autofill-result');
        if (!raw) return null;
        try { return JSON.parse(raw); } catch (e) { return null; }
    }

    // ---- Fill flow -------------------------------------------------------
    async function fillLatest() {
        var user = userInput.value.trim();
        var pass = passInput.value;
        writePref(USER_KEY, user);
        writePref(PASS_KEY, pass);

        var formId = pageFormId();
        if (!formId) {
            setStatus('Could not detect the current form id.', true);
            return;
        }

        fillButton.disabled = true;
        setStatus('Fetching submissions\u2026');
        try {
            var response;
            try {
                response = await fetch('/api/v1/submissions?limit=100', {
                    headers: { Authorization: 'Basic ' + basicAuth(user, pass) },
                    credentials: 'include'
                });
            } catch (err) {
                setStatus('Network error: ' + (err && err.message ? err.message : err), true);
                return;
            }
            if (response.status === 401) {
                setStatus('401 Unauthorized \u2014 check admin username/password.', true);
                return;
            }
            if (!response.ok) {
                setStatus('HTTP ' + response.status + ' while fetching submissions.', true);
                return;
            }
            var data;
            try { data = await response.json(); } catch (err) { data = null; }
            if (!data) {
                setStatus('Submissions API returned invalid JSON.', true);
                return;
            }
            var subs = Array.isArray(data) ? data : (Array.isArray(data.submissions) ? data.submissions : []);
            var forForm = subs.filter(function (s) {
                return String((s && s.form_id) || 'income_survey') === formId;
            });
            if (!forForm.length) {
                setStatus('No submissions found for form "' + formId + '".', true);
                return;
            }
            forForm.sort(function (a, b) { return submissionTime(b) - submissionTime(a); });
            var latest = forForm[0];

            var formData = latest.form_data;
            if (typeof formData === 'string') {
                try { formData = JSON.parse(formData); } catch (err) { formData = {}; }
            }
            if (!formData || typeof formData !== 'object' || Array.isArray(formData)) formData = {};

            var result = injectFiller({ form_data: formData });
            if (!result) {
                setStatus('Could not run the fill script (injection blocked by the page?).', true);
                return;
            }
            if (result.error) {
                setStatus('Fill failed: ' + result.error, true);
                return;
            }

            var subId = latest.submission_id == null ? '' : String(latest.submission_id);
            var idText = subId ? (subId.charAt(0) === '#' ? subId : '#' + subId) : '(unknown submission)';
            var msg = 'Filled ' + result.filled + ' fields from ' + idText + ' (images not restored)';
            if (result.skipped) msg += ', ' + result.skipped + ' skipped';
            setStatus(msg);
        } finally {
            fillButton.disabled = false;
        }
    }

    fillButton.addEventListener('click', fillLatest);

    (document.body || document.documentElement).appendChild(panel);
})();
