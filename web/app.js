/* LearnDeck front end: library, upload, and the drill-down slide viewer. No build step. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const KIND = { pdf: "PDF", pptx: "Slides", docx: "Word document", xlsx: "Spreadsheet", csv: "Table", code: "Code",
    repo: "Code repository", notebook: "Notebook", markdown: "Notes", text: "Text", web: "Web page", image: "Image", collection: "Several files" };
  const PROVIDER = { anthropic: "Claude", openai: "OpenAI", ollama: "Ollama", fake: "Test model" };
  const SKIP_DIRS = new Set(["node_modules", ".git", "__pycache__", ".venv", "venv", "env", "dist", "build", ".next", "target", ".idea", ".vscode", ".mypy_cache", ".pytest_cache", "Pods", "DerivedData"]);

  const S = { deck: null, deckId: null, node: null, poll: null, token: 0, health: null, settings: null, zoomClone: null, zoomStart: 0 };

  // ------------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const json = opts.body !== undefined && !(opts.body instanceof FormData);
    const res = await fetch(path, {
      method: opts.method || "GET",
      headers: json ? { "Content-Type": "application/json" } : {},
      body: json ? JSON.stringify(opts.body) : opts.body,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (!res.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : `Request failed (${res.status}).`);
    }
    return data;
  }

  function uploadFiles(items, level, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      for (const it of items) fd.append("files", it.file, it.path || it.file.name);
      fd.append("level", level);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/decks/upload");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total, false);
      xhr.upload.onload = () => onProgress(1, true);
      xhr.onload = () => {
        let d = null;
        try { d = JSON.parse(xhr.responseText); } catch { /* not json */ }
        if (xhr.status < 300) resolve(d);
        else reject(new Error((d && typeof d.detail === "string" && d.detail) || `Upload failed (${xhr.status}).`));
      };
      xhr.onerror = () => reject(new Error("Upload failed. Check that LearnDeck is still running."));
      xhr.send(fd);
    });
  }

  // ------------------------------------------------------------- markdown
  function inlineFallback(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w*])\*([^*\n]+)\*(?=[^\w*]|$)/g, "$1<em>$2</em>");
  }

  function fallbackMd(src) {
    const out = [];
    const lines = src.split("\n");
    let para = [], list = null;
    const flush = () => {
      if (para.length) { out.push(`<p>${inlineFallback(para.join(" "))}</p>`); para = []; }
      if (list) { out.push(`<${list.t}>${list.items.map((x) => `<li>${inlineFallback(x)}</li>`).join("")}</${list.t}>`); list = null; }
    };
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const fence = ln.match(/^\s*```\s*([\w+-]*)/);
      if (fence) {
        flush();
        const buf = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) buf.push(lines[i]);
        out.push(`<pre><code class="language-${esc(fence[1] || "plaintext")}">${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      let m;
      if ((m = ln.match(/^(#{1,4})\s+(.*)/))) { flush(); out.push(`<h${m[1].length}>${inlineFallback(m[2])}</h${m[1].length}>`); }
      else if ((m = ln.match(/^\s*([-*•]|\d+[.)])\s+(.*)/))) {
        const t = /\d/.test(m[1]) ? "ol" : "ul";
        if (para.length) flush();
        if (!list || list.t !== t) { flush(); list = { t, items: [] }; }
        list.items.push(m[2]);
      } else if (!ln.trim()) flush();
      else if (ln.startsWith(">")) { flush(); out.push(`<blockquote>${inlineFallback(ln.replace(/^>\s?/, ""))}</blockquote>`); }
      else { if (list) flush(); para.push(ln.trim()); }
    }
    flush();
    return out.join("\n");
  }

  function md(source) {
    let src = String(source || "");
    const codes = [];
    const hold = (m) => { codes.push(m); return `@@C${codes.length - 1}@@`; };
    src = src.replace(/```[\s\S]*?(?:```|$)/g, hold).replace(/`[^`\n]+`/g, hold);
    const maths = [];
    const keep = (tex, display) => { maths.push({ tex, display }); return `@@M${maths.length - 1}@@`; };
    src = src
      .replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => keep(t, true))
      .replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => keep(t, true))
      .replace(/\\\(([\s\S]+?)\\\)/g, (_, t) => keep(t, false))
      .replace(/(^|[^\\\w$])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])/g, (_, pre, t) => pre + keep(t, false));
    src = src.replace(/@@C(\d+)@@/g, (_, i) => codes[+i]);
    let html;
    if (window.marked && window.DOMPurify) {
      html = window.DOMPurify.sanitize(window.marked.parse(src, { gfm: true, breaks: false }));
    } else {
      html = fallbackMd(src);
    }
    return html.replace(/@@M(\d+)@@/g, (_, i) => {
      const m = maths[+i];
      if (window.katex) {
        try { return window.katex.renderToString(m.tex, { displayMode: m.display, throwOnError: false }); } catch { /* fall through */ }
      }
      return esc(m.display ? `$$${m.tex}$$` : `$${m.tex}$`);
    });
  }
  const mdInline = (s) => md(s).replace(/^\s*<p>([\s\S]*)<\/p>\s*$/, "$1");

  function enhance(el) {
    if (!window.hljs) return;
    el.querySelectorAll("pre code").forEach((c) => {
      if (c.dataset.highlighted) return;
      const lang = (c.className.match(/language-([\w+-]+)/) || [])[1];
      if (lang && !window.hljs.getLanguage(lang)) c.className = "language-plaintext";
      try { window.hljs.highlightElement(c); } catch { /* ignore */ }
    });
  }

  // --------------------------------------------------------------- toasts
  function toast(msg, isError = false) {
    const t = document.createElement("div");
    t.className = "toast" + (isError ? " error" : "");
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), isError ? 8000 : 4500);
  }

  // ---------------------------------------------------------------- theme
  let theme = null;
  try { theme = localStorage.getItem("ld-theme"); } catch { /* storage blocked */ }
  function applyTheme(t) {
    if (t) document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
    const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    $("#hljs-light").disabled = dark;
    $("#hljs-dark").disabled = !dark;
  }
  function toggleTheme() {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    theme = cur === "dark" ? "light" : "dark";
    try { localStorage.setItem("ld-theme", theme); } catch { /* ignore */ }
    applyTheme(theme);
  }
  applyTheme(theme);

  // --------------------------------------------------------------- router
  function route() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    closeDrawers();
    hideDivePop();
    if (parts[0] === "d" && parts[1]) showDeck(decodeURIComponent(parts[1]), decodeURIComponent(parts[2] || "root"));
    else showHome();
  }
  const go = (deckId, nodeId) => { location.hash = `#/d/${encodeURIComponent(deckId)}/${encodeURIComponent(nodeId)}`; };
  window.addEventListener("hashchange", route);

  // ----------------------------------------------------------------- home
  async function showHome() {
    stopPoll();
    S.deck = null; S.deckId = null; S.node = null;
    $("#deck").hidden = true;
    $("#home").hidden = false;
    document.title = "LearnDeck";
    loadHealth();
    loadLibrary();
  }

  async function loadHealth() {
    try { S.health = await api("/api/health"); updateModePills(); } catch { /* server down: other calls will say so */ }
  }

  function updateModePills() {
    const llm = (S.health && S.health.llm) || {};
    const simple = !llm.provider || llm.provider === "none";
    for (const el of [$("#home-mode"), $("#deck-mode")]) {
      el.hidden = false;
      el.classList.toggle("warn", simple);
      el.textContent = simple ? "Simple mode: add an AI key" : `AI: ${PROVIDER[llm.provider] || llm.provider}`;
      el.title = simple ? "Without an AI provider, LearnDeck shows the material's own structure. Click to add a key."
        : `Model: ${llm.model}. Click to change.`;
    }
  }

  async function loadLibrary() {
    const list = $("#library-list");
    try {
      const decks = await api("/api/decks");
      list.innerHTML = decks.map(libItem).join("");
      $("#library-empty").hidden = decks.length > 0;
    } catch (e) {
      toast(e.message, true);
    }
  }

  function libItem(d) {
    const p = d.progress || { visited: 0, total: 0 };
    const pct = p.total ? Math.round((100 * p.visited) / p.total) : 0;
    const when = new Date(d.created * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    return `<li>
      <a class="lib-open" href="#/d/${esc(d.id)}/root"><span class="lib-title">${esc(d.title)}</span>
        <span class="lib-meta">${esc(KIND[d.kind] || d.kind)}, added ${esc(when)}</span></a>
      <span class="lib-progress"><span class="bar"><span style="width:${pct}%"></span></span>${p.visited} of ${p.total} opened</span>
      <button class="icon-btn" type="button" data-del="${esc(d.id)}" data-title="${esc(d.title)}" aria-label="Delete ${esc(d.title)}" title="Delete deck">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg></button>
    </li>`;
  }

  $("#library-list").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-del]");
    if (!b) return;
    if (!confirm(`Delete “${b.dataset.title}”? Its slides and progress will be removed.`)) return;
    try { await api(`/api/decks/${b.dataset.del}`, { method: "DELETE" }); loadLibrary(); toast("Deck deleted."); }
    catch (err) { toast(err.message, true); }
  });

  const level = () => ($("input[name=level]:checked") || {}).value || "intermediate";
  try {
    const saved = localStorage.getItem("ld-level");
    if (saved) { const r = $(`input[name=level][value="${saved}"]`); if (r) r.checked = true; }
  } catch { /* ignore */ }
  $$("input[name=level]").forEach((r) => r.addEventListener("change", () => { try { localStorage.setItem("ld-level", r.value); } catch { /* ignore */ } }));

  // upload: drop zone, file picker, folders
  const dz = $("#dropzone"), fileInput = $("#file-input");
  function setBusy(on, msg = "", p = null) {
    dz.classList.toggle("busy", on);
    $(".drop-inner", dz).hidden = on;
    const pr = $(".drop-progress", dz);
    pr.hidden = !on;
    $$("#link-form button, #text-form button").forEach((b) => { b.disabled = on; });
    if (!on) return;
    $(".drop-status", dz).textContent = msg;
    const bar = $(".bar", pr);
    bar.classList.toggle("indeterminate", p == null);
    $("span", bar).style.width = p == null ? "" : `${Math.round(p * 100)}%`;
  }

  function afterCreate(d) {
    if (d.notes && d.notes.length) toast(d.notes.join(" "));
    go(d.id, "root");
  }

  async function startUpload(items) {
    if (!items.length || dz.classList.contains("busy")) return;
    setBusy(true, "Uploading…", 0);
    try {
      const d = await uploadFiles(items, level(), (p, done) =>
        setBusy(true, done ? `Reading ${items.length > 1 ? items.length + " files" : items[0].file.name}…` : `Uploading… ${Math.round(p * 100)}%`, done ? null : p));
      afterCreate(d);
    } catch (e) {
      toast(e.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function filesFromDrop(dt) {
    const files = [...(dt.files || [])];
    const entries = [...(dt.items || [])].filter((i) => i.kind === "file").map((i) => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    if (!entries.length || entries.every((e) => e.isFile)) return files.map((f) => ({ file: f, path: f.name }));
    const out = [];
    const readDir = (dir) => new Promise((resolve) => {
      const reader = dir.createReader(), all = [];
      const next = () => reader.readEntries((batch) => { if (!batch.length) return resolve(all); all.push(...batch); next(); }, () => resolve(all));
      next();
    });
    const walk = async (entry, prefix) => {
      if (out.length >= 3000) return;
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej)).catch(() => null);
        if (file && !entry.name.startsWith(".")) out.push({ file, path: prefix + entry.name });
      } else if (entry.isDirectory && !SKIP_DIRS.has(entry.name) && !entry.name.startsWith(".")) {
        for (const child of await readDir(entry)) await walk(child, prefix + entry.name + "/");
      }
    };
    for (const e of entries) await walk(e, "");
    return out;
  }

  dz.addEventListener("click", () => { if (!dz.classList.contains("busy")) fileInput.click(); });
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener("change", () => {
    const items = [...fileInput.files].map((f) => ({ file: f, path: f.name }));
    fileInput.value = "";
    startUpload(items);
  });
  document.addEventListener("dragover", (e) => {
    if ($("#home").hidden) return;
    e.preventDefault();
    dz.classList.add("over");
  });
  document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) dz.classList.remove("over"); });
  document.addEventListener("drop", async (e) => {
    if ($("#home").hidden) return;
    e.preventDefault();
    dz.classList.remove("over");
    startUpload(await filesFromDrop(e.dataTransfer));
  });

  $("#link-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = $("#link-input").value.trim();
    if (!url) return;
    setBusy(true, "Downloading and reading the link…");
    try {
      afterCreate(await api("/api/decks/link", { method: "POST", body: { url, level: level() } }));
      $("#link-input").value = "";
    } catch (err) { toast(err.message, true); }
    finally { setBusy(false); }
  });

  $("#text-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = $("#text-input").value;
    if (!text.trim()) return;
    setBusy(true, "Reading your text…");
    try {
      afterCreate(await api("/api/decks/text", { method: "POST", body: { text, title: $("#text-title").value.trim() || "Pasted text", level: level() } }));
      $("#text-input").value = ""; $("#text-title").value = "";
    } catch (err) { toast(err.message, true); }
    finally { setBusy(false); }
  });

  // ----------------------------------------------------------------- deck
  async function showDeck(deckId, nodeId) {
    $("#home").hidden = true;
    $("#deck").hidden = false;
    if (S.deckId !== deckId || !S.deck) {
      S.deckId = deckId; S.node = null;
      try { await refreshDeck(); }
      catch (e) { toast(e.message, true); location.hash = "#/"; return; }
    }
    loadNode(nodeId);
  }

  async function refreshDeck() {
    const d = await api(`/api/decks/${S.deckId}`);
    S.deck = d;
    if (d.llm) { S.health = { ...(S.health || {}), llm: d.llm }; updateModePills(); }
    $("#deck-name").textContent = d.title;
    updateProgress();
    if (!$("#map-drawer").hidden) renderMap();
  }

  function updateProgress() {
    const p = S.deck.progress;
    $("#progress .progress-bar span").style.width = `${p.total ? (100 * p.visited) / p.total : 0}%`;
    $("#progress .progress-text").textContent = `${p.visited} of ${p.total} opened`;
  }

  function stopPoll() { clearTimeout(S.poll); S.poll = null; }

  async function loadNode(nodeId, opts = {}) {
    stopPoll();
    const token = ++S.token;
    let node;
    try { node = await api(`/api/decks/${S.deckId}/nodes/${encodeURIComponent(nodeId)}${opts.retry ? "?retry=true" : ""}`); }
    catch (e) { finishZoom(); toast(e.message, true); return; }
    if (token !== S.token) return;
    const prev = S.node;
    const isNew = !prev || prev.id !== node.id;
    const direction = isNew && prev ? (node.depth > prev.depth ? "down" : node.depth < prev.depth ? "up" : "side") : isNew ? "down" : null;
    S.node = node;
    renderNode(node, direction);
    if (node.status === "ready") onReady(node);
    else if (node.status !== "error") S.poll = setTimeout(() => pollNode(token), 1000);
  }

  async function pollNode(token) {
    if (token !== S.token || !S.node) return;
    let node;
    try { node = await api(`/api/decks/${S.deckId}/nodes/${S.node.id}`); }
    catch { S.poll = setTimeout(() => pollNode(token), 2500); return; }
    if (token !== S.token) return;
    S.node = node;
    if (node.status === "ready" || node.status === "error") {
      renderNode(node, null);
      if (node.status === "ready") onReady(node);
      return;
    }
    const msg = $("#progress-msg");
    if (msg && node.progress) msg.textContent = node.progress;
    S.poll = setTimeout(() => pollNode(token), 1200);
  }

  async function onReady(n) {
    if (!n.visited) {
      try { await api(`/api/decks/${S.deckId}/nodes/${n.id}/visited`, { method: "POST" }); n.visited = true; } catch { /* ignore */ }
    }
    try { await refreshDeck(); } catch { /* ignore */ }
    if (S.node && S.node.id === n.id) renderNav(n);
  }

  function renderNode(n, direction) {
    document.title = `${n.title} | LearnDeck`;
    renderLadder(n);
    renderSlide(n);
    renderRail(n);
    renderNav(n);
    const slide = $("#slide");
    if (direction) {
      window.scrollTo({ top: 0 });
      slide.focus({ preventScroll: true });
      if (!reduceMotion) {
        slide.classList.remove("enter", "leave-up");
        void slide.offsetWidth;
        slide.classList.add(direction === "up" ? "leave-up" : "enter");
      }
    }
    finishZoom();
  }

  const nodeHref = (id) => `#/d/${encodeURIComponent(S.deckId)}/${encodeURIComponent(id)}`;

  function renderLadder(n) {
    $("#ladder").innerHTML = n.path.map((p, i) => {
      const indent = `style="margin-left:${Math.min(i, 6) * 10}px"`;
      return i === n.path.length - 1
        ? `<span class="here" ${indent} aria-current="page">${esc(p.title)}</span>`
        : `<a href="${nodeHref(p.id)}" ${indent} title="${esc(p.title)}">${esc(p.title)}</a>`;
    }).join("");
  }

  function sourceText(n) {
    const shown = n.source_labels.slice(0, 3);
    const more = n.source_count - shown.length;
    return shown.join(", ") + (more > 0 ? `, and ${more} more` : "");
  }

  function renderSlide(n) {
    const c = n.content || {};
    const crumbs = n.path.length > 1
      ? `<nav class="crumbs" aria-label="Path">${n.path.slice(0, -1).map((p) => `<a href="${nodeHref(p.id)}">${esc(p.title)}</a><span class="sep" aria-hidden="true">/</span>`).join("")}</nav>` : "";
    const lede = c.headline || (n.status !== "ready" ? n.summary : "");
    const from = n.kind !== "root" && n.source_labels && n.source_labels.length
      ? `<p class="slide-from">From ${esc(sourceText(n))}. <button type="button" data-act="sources">Show the original</button></p>` : "";
    const body = n.status === "ready" ? readyBody(n) : n.status === "error" ? errorBody(n) : loadingBody(n);
    $("#slide").innerHTML = `${crumbs}
      <header class="slide-head">
        ${n.icon && n.kind !== "root" ? `<span class="slide-icon" aria-hidden="true">${esc(n.icon)}</span>` : ""}
        <h1 class="slide-title">${esc(n.title)}</h1>
        ${lede ? `<p class="slide-headline">${esc(lede)}</p>` : ""}
        ${from}
        ${n.status === "ready" && n.children.length ? `<button class="jump-parts" type="button" data-act="jump">${n.children.length === 1 ? "1 part" : n.children.length + " parts"} to explore below</button>` : ""}
      </header>${body}`;
    enhance($("#slide"));
  }

  function selectionBlock(n) {
    return n.selection ? `<section class="block"><h2 class="block-title">You highlighted</h2><p class="analogy">${esc(n.selection)}</p></section>` : "";
  }

  function readyBody(n) {
    const c = n.content || {};
    let h = selectionBlock(n);
    h += `<div class="prose">${md(c.explanation)}</div>`;
    if (c.key_points && c.key_points.length) {
      h += `<section class="block"><h2 class="block-title">Key points</h2><ul class="points">${c.key_points.map((k) =>
        `<li><button type="button" data-point="${esc(k)}" title="Open a slide about this point"><span>${mdInline(k)}</span><span class="dive-hint">Go deeper</span></button></li>`).join("")}</ul></section>`;
    }
    if (c.analogy) h += `<section class="block"><h2 class="block-title">Think of it like this</h2><p class="analogy">${mdInline(c.analogy)}</p></section>`;
    if (c.example) h += `<section class="block"><details class="example"><summary>Worked example</summary><div class="prose">${md(c.example)}</div></details></section>`;
    if (c.check && c.check.question) {
      h += `<section class="block"><details class="check"><summary><span>Check yourself: <span class="q">${mdInline(c.check.question)}</span></span></summary><div class="answer">${md(c.check.answer)}</div></details></section>`;
    }
    h += `<section class="qa" aria-label="Questions about this slide"><h2 class="block-title">Ask about this slide</h2>
      <div id="qa-list">${(n.qa || []).map(qaItem).join("")}</div>
      <form class="ask-row" id="ask-form"><label class="sr-only" for="ask-input">Your question</label>
        <input id="ask-input" type="text" placeholder="What's unclear here?" autocomplete="off" required>
        <button class="btn" type="submit">Ask</button></form></section>`;
    const by = n.provider && n.provider !== "simple" ? `<span>Written by ${esc(providerLabel(n.provider))}</span>` : "";
    h += `<div class="slide-tools">${by}<button type="button" data-act="regen">Rewrite this slide</button></div>`;
    return h;
  }

  const qaItem = (x) => `<div class="qa-item"><p class="qa-q">${esc(x.q)}</p><div class="qa-a prose">${md(x.a)}</div></div>`;
  function providerLabel(p) {
    const [name, ...model] = String(p).split(":");
    return `${PROVIDER[name] || name}${model.length ? ` (${model.join(":")})` : ""}`;
  }

  function loadingBody(n) {
    const msg = n.progress || (n.kind === "root" ? "Reading the material…" : "Reading this part…");
    return `${selectionBlock(n)}<div class="reading"><p class="reading-status"><span class="spinner" aria-hidden="true"></span><span id="progress-msg">${esc(msg)}</span></p>
      ${[92, 100, 86, 97, 58].map((w) => `<div class="ghost-line" style="width:${w}%"></div>`).join("")}</div>`;
  }

  function errorBody(n) {
    const keyIssue = /key|provider|settings|model|ollama/i.test(n.error || "");
    return `<div class="error-box" role="alert"><p>${esc(n.error || "This slide couldn't be written.")}</p>
      <div class="actions"><button class="btn primary" type="button" data-act="retry">Try again</button>
      ${keyIssue ? `<button class="btn" type="button" data-act="settings">Open settings</button>` : ""}</div></div>`;
  }

  function renderRail(n) {
    const kids = n.children.filter((k) => k.kind !== "dive");
    const dives = n.children.filter((k) => k.kind === "dive");
    let h;
    if (n.status !== "ready") {
      h = `<h2>Go deeper</h2><p class="rail-note">The parts of this slide appear here once it's written.</p>`;
    } else if (kids.length) {
      h = `<h2>${n.kind === "root" ? "Parts, in a good order to learn them" : "Go deeper"}</h2>
        <p class="rail-note">${n.kind === "root" ? "Open any part. Each one leads further in." : "Open a part to zoom in on it."}</p>
        <ol class="regions">${kids.map((k, i) => regionItem(k, i, false)).join("")}</ol>`;
    } else {
      h = `<div class="leaf-note"><p>${n.depth >= n.max_depth ? "This is the deepest level for this lesson." : "Nothing smaller to open here: this slide covers the whole part."}</p>
        <p>To dig further, highlight words on the slide and choose “Go deeper on this”, or ask a question.</p></div>`;
    }
    if (dives.length) h += `<h2>Your deep dives</h2><ol class="regions">${dives.map((k, i) => regionItem(k, i, true)).join("")}</ol>`;
    $("#rail").innerHTML = h;
  }

  function regionItem(k, i, isDive) {
    const state = k.visited ? "Opened" : k.status === "ready" ? "Ready to open" : k.status === "working" ? "Being written" : k.status === "error" ? "Failed, open to retry" : "Not opened yet";
    const cls = ["region", k.status, k.visited ? "visited" : ""].join(" ").trim();
    return `<li><button class="${cls}" type="button" data-node="${esc(k.id)}" aria-label="${esc(`${isDive ? "" : i + 1 + ". "}${k.title}. ${state}.`)}">
      <span class="r-num" aria-hidden="true">${isDive ? "🔍" : i + 1}</span>
      <span class="r-body"><span class="r-title">${k.icon && !isDive ? `<span class="r-icon" aria-hidden="true">${esc(k.icon)}</span>` : ""}<span>${esc(k.title)}</span></span>
        ${k.summary ? `<span class="r-sum">${esc(k.summary)}</span>` : ""}</span>
      <span class="r-state" title="${state}" aria-hidden="true"></span></button></li>`;
  }

  // navigation
  function dfs() {
    const out = [], nodes = (S.deck && S.deck.nodes) || {};
    const walk = (id) => { const x = nodes[id]; if (!x) return; out.push(x); x.children.forEach(walk); };
    if (S.deck) walk(S.deck.root);
    return out;
  }
  function nextUnexplored() {
    const order = dfs();
    const i = order.findIndex((x) => S.node && x.id === S.node.id);
    const rest = [...order.slice(i + 1), ...order.slice(0, Math.max(i, 0))];
    return rest.find((x) => !x.visited && (!S.node || x.id !== S.node.id));
  }
  function renderNav(n) {
    $("#btn-up").disabled = !n.parent;
    const sibs = n.siblings || [];
    const i = sibs.findIndex((s) => s.id === n.id);
    $("#btn-prev").disabled = i <= 0;
    $("#btn-next").disabled = i < 0 || i >= sibs.length - 1;
    $("#sib-pos").textContent = n.parent && sibs.length > 1 ? `${i + 1} of ${sibs.length}` : "";
    const nx = nextUnexplored();
    const b = $("#btn-unexplored");
    b.disabled = !nx;
    b.title = nx ? `Open “${nx.title}” (N)` : "You've opened every slide generated so far";
  }
  const goUp = () => { if (S.node && S.node.parent) go(S.deckId, S.node.parent); };
  function goSib(d) {
    if (!S.node) return;
    const s = S.node.siblings || [];
    const t = s[s.findIndex((x) => x.id === S.node.id) + d];
    if (t) go(S.deckId, t.id);
  }
  const goUnexplored = () => { const nx = nextUnexplored(); if (nx) go(S.deckId, nx.id); };
  function openRegion(id, el) { if (el) zoomFrom(el); go(S.deckId, id); }

  // the zoom: a clicked region grows into the next slide
  function zoomFrom(el) {
    if (reduceMotion) return;
    finishZoom(true);
    const r = el.getBoundingClientRect();
    const t = $("#slide").getBoundingClientRect();
    const clone = document.createElement("div");
    clone.className = "zoom-clone";
    clone.innerHTML = `<span class="r-title">${($(".r-title", el) || el).innerHTML}</span>`;
    Object.assign(clone.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
    document.body.appendChild(clone);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      clone.classList.add("grow");
      Object.assign(clone.style, {
        left: `${t.left}px`, top: `${Math.max(t.top, 72)}px`, width: `${t.width}px`,
        height: `${Math.min(innerHeight * 0.5, 360)}px`, borderColor: "transparent", background: "var(--paper)",
      });
    }));
    S.zoomClone = clone;
    S.zoomStart = performance.now();
  }
  function finishZoom(now = false) {
    const c = S.zoomClone;
    if (!c) return;
    S.zoomClone = null;
    const wait = now ? 0 : Math.max(0, 340 - (performance.now() - S.zoomStart));
    setTimeout(() => { c.style.opacity = "0"; setTimeout(() => c.remove(), 220); }, wait);
  }

  // slide actions
  $("#deck").addEventListener("click", (e) => {
    const nodeBtn = e.target.closest("[data-node]");
    if (nodeBtn) { openRegion(nodeBtn.dataset.node, nodeBtn); return; }
    const point = e.target.closest("[data-point]");
    if (point) { diveInto(point.dataset.point); return; }
    const act = (e.target.closest("[data-act]") || {}).dataset;
    if (!act) return;
    if (act.act === "sources") toggleDrawer("sources", true);
    else if (act.act === "retry") loadNode(S.node.id, { retry: true });
    else if (act.act === "settings") openSettings();
    else if (act.act === "regen") regenerate();
    else if (act.act === "jump") $("#rail").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" });
  });
  $("#btn-up").addEventListener("click", goUp);
  $("#btn-prev").addEventListener("click", () => goSib(-1));
  $("#btn-next").addEventListener("click", () => goSib(1));
  $("#btn-unexplored").addEventListener("click", goUnexplored);

  async function regenerate() {
    if (!S.node) return;
    if (S.node.children.length && !confirm("Rewrite this slide? The deeper slides under it will be rewritten too.")) return;
    try {
      await api(`/api/decks/${S.deckId}/nodes/${S.node.id}/regenerate`, { method: "POST" });
      await refreshDeck();
      loadNode(S.node.id);
    } catch (e) { toast(e.message, true); }
  }

  async function diveInto(text) {
    hideDivePop();
    if (!S.node || S.node.status !== "ready" || !text) return;
    try {
      const r = await api(`/api/decks/${S.deckId}/nodes/${S.node.id}/dive`, { method: "POST", body: { text } });
      const sel = window.getSelection();
      if (sel) sel.removeAllRanges();
      go(S.deckId, r.id);
    } catch (e) { toast(e.message, true); }
  }

  document.addEventListener("submit", async (e) => {
    if (e.target.id !== "ask-form") return;
    e.preventDefault();
    const input = $("#ask-input"), btn = $("button", e.target);
    const q = input.value.trim();
    if (!q || !S.node) return;
    const nodeId = S.node.id;
    btn.disabled = true; input.disabled = true;
    const list = $("#qa-list");
    const pending = document.createElement("div");
    pending.className = "qa-item";
    pending.innerHTML = `<p class="qa-q">${esc(q)}</p><div class="qa-a"><p class="reading-status"><span class="spinner" aria-hidden="true"></span><span>Thinking…</span></p></div>`;
    list.appendChild(pending);
    try {
      const r = await api(`/api/decks/${S.deckId}/nodes/${nodeId}/ask`, { method: "POST", body: { question: q } });
      if (S.node && S.node.id === nodeId) {
        S.node.qa = [...(S.node.qa || []), { q, a: r.answer }];
        pending.outerHTML = qaItem({ q, a: r.answer });
        enhance(list);
        input.value = "";
      }
    } catch (err) { pending.remove(); toast(err.message, true); }
    finally { btn.disabled = false; input.disabled = false; input.focus(); }
  });

  // highlight any text on a slide -> "Go deeper on this"
  const pop = $("#dive-pop");
  function hideDivePop() { pop.hidden = true; }
  function checkSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount || $("#deck").hidden) return hideDivePop();
    const range = sel.getRangeAt(0);
    const text = selectionText(range);
    if (text.length < 3 || !$("#slide").contains(range.commonAncestorContainer) || !S.node || S.node.status !== "ready") return hideDivePop();
    if (range.commonAncestorContainer.parentElement && range.commonAncestorContainer.parentElement.closest("form, .qa-q")) return hideDivePop();
    const rect = range.getBoundingClientRect();
    if (rect.bottom < 60 || rect.top > innerHeight - 70) return hideDivePop();
    pop.style.left = `${Math.min(Math.max(rect.left + rect.width / 2, 100), innerWidth - 100)}px`;
    pop.style.top = `${Math.min(rect.bottom, innerHeight - 130)}px`;
    pop.dataset.text = text.slice(0, 2000);
    pop.hidden = false;
  }
  // Read selected text, turning rendered math back into its LaTeX source.
  function selectionText(range) {
    const box = document.createElement("div");
    box.appendChild(range.cloneContents());
    box.querySelectorAll(".katex").forEach((k) => {
      const tex = k.querySelector('annotation[encoding="application/x-tex"]');
      k.replaceWith(tex ? `$${tex.textContent}$` : (k.querySelector(".katex-html") || k).textContent);
    });
    box.querySelectorAll(".katex-mathml").forEach((m) => m.remove());
    return box.textContent.replace(/\s+/g, " ").trim();
  }
  let selTimer = null;
  document.addEventListener("mouseup", () => setTimeout(checkSelection, 10));
  document.addEventListener("keyup", (e) => { if (e.shiftKey || e.key === "Shift") checkSelection(); });
  document.addEventListener("selectionchange", () => {
    if (!matchMedia("(pointer: coarse)").matches) return;
    clearTimeout(selTimer);
    selTimer = setTimeout(checkSelection, 350);
  });
  window.addEventListener("scroll", hideDivePop, { passive: true });
  pop.addEventListener("mousedown", (e) => e.preventDefault());
  pop.addEventListener("click", () => diveInto(pop.dataset.text));

  // drawers
  function toggleDrawer(which, forceOpen = false) {
    const el = which === "map" ? $("#map-drawer") : $("#source-drawer");
    if (!el.hidden && !forceOpen) return closeDrawers();
    closeDrawers();
    if (!S.deck || !S.node) return;
    el.hidden = false;
    $("#scrim").hidden = false;
    if (which === "map") renderMap(); else renderSources();
    const btn = $(`[data-action="${which}"]`);
    if (btn) btn.setAttribute("aria-pressed", "true");
    $("[data-close]", el).focus();
  }
  function closeDrawers() {
    $("#map-drawer").hidden = true;
    $("#source-drawer").hidden = true;
    $("#scrim").hidden = true;
    $$('[data-action="map"], [data-action="sources"]').forEach((b) => b.setAttribute("aria-pressed", "false"));
  }
  $("#scrim").addEventListener("click", closeDrawers);
  $$("[data-close]").forEach((b) => b.addEventListener("click", closeDrawers));

  function renderMap() {
    const nodes = S.deck.nodes, cur = S.node && S.node.id;
    const walk = (id) => {
      const n = nodes[id];
      if (!n) return "";
      const kids = n.children.map(walk).join("");
      const dot = n.visited ? "visited" : n.status !== "ready" ? "pending" : "";
      return `<li><a href="${nodeHref(id)}" class="${id === cur ? "current" : ""}"${id === cur ? ' aria-current="page"' : ""}>
        <span class="dot ${dot}" aria-hidden="true"></span><span>${n.icon && n.kind !== "root" ? esc(n.icon) + " " : ""}${esc(n.title)}</span></a>${kids ? `<ul>${kids}</ul>` : ""}</li>`;
    };
    $("#map-tree").innerHTML = `<ul>${walk(S.deck.root)}</ul>`;
  }

  async function renderSources() {
    const list = $("#source-list"), note = $("#source-note");
    list.innerHTML = `<p class="reading-status"><span class="spinner" aria-hidden="true"></span><span>Loading…</span></p>`;
    note.textContent = "";
    try {
      const r = await api(`/api/decks/${S.deckId}/nodes/${S.node.id}/sources`);
      note.textContent = r.total > r.items.length
        ? `The first ${r.items.length} of ${r.total} parts of the original this slide draws on.`
        : `The ${r.items.length === 1 ? "part" : r.items.length + " parts"} of the original this slide draws on.`;
      list.innerHTML = r.items.map((s) => {
        const text = s.text ? (s.lang ? `<pre><code class="language-${esc(s.lang)}">${esc(s.text)}</code></pre>` : `<p class="prose-src">${esc(s.text)}</p>`) : "";
        const img = s.page_url || s.image_url;
        return `<section class="src"><h3>${esc(s.loc || s.title)}</h3>
          ${img ? `<img loading="lazy" src="${esc(img)}" alt="${esc(s.loc || s.title)} from the original">` : ""}
          ${img && text ? `<details><summary>Text found on this page</summary>${text}</details>` : text}
          ${s.truncated ? `<p class="drawer-note">This part is long, so it's shortened here.</p>` : ""}</section>`;
      }).join("") || "<p>No original text is linked to this slide.</p>";
      enhance(list);
    } catch (e) { list.innerHTML = `<p>${esc(e.message)}</p>`; }
  }

  // settings
  async function openSettings() {
    try { S.settings = await api("/api/settings"); } catch (e) { toast(e.message, true); return; }
    const s = S.settings;
    $("#set-provider").value = s.provider;
    $("#set-key").value = "";
    $("#set-ollama").value = s.ollama_url || "";
    syncSettingsForm();
    const a = s.active || {};
    $("#settings-active").textContent = a.provider && a.provider !== "none"
      ? `Now using ${PROVIDER[a.provider] || a.provider} with ${a.model}.`
      : "No AI provider is active, so LearnDeck is in simple mode and shows the material's own structure.";
    $("#settings-dialog").showModal();
  }
  function syncSettingsForm() {
    const p = $("#set-provider").value, s = S.settings || {};
    const keyFor = p === "openai" ? "openai" : p === "anthropic" ? "anthropic" : null;
    $("#set-key-row").hidden = p === "ollama" || p === "none";
    $("#set-model-row").hidden = !["anthropic", "openai", "ollama"].includes(p);
    $("#set-ollama-row").hidden = p !== "ollama";
    const saved = keyFor === "openai" ? s.openai_key_set : keyFor === "anthropic" ? s.anthropic_key_set : (s.anthropic_key_set || s.openai_key_set);
    $("#set-key-hint").textContent = saved ? "A key is already saved. Leave this blank to keep it."
      : keyFor === "openai" ? "Create a key at platform.openai.com." : "Create a key at console.anthropic.com (keys start with sk-ant-).";
    const model = { anthropic: s.anthropic_model, openai: s.openai_model, ollama: s.ollama_model }[p] || "";
    $("#set-model").value = model;
  }
  $("#set-provider").addEventListener("change", syncSettingsForm);
  $("#settings-dialog").addEventListener("close", async () => {
    if ($("#settings-dialog").returnValue !== "save") return;
    try {
      const s = await api("/api/settings", { method: "POST", body: {
        provider: $("#set-provider").value, api_key: $("#set-key").value || null,
        model: $("#set-model").value || null, ollama_url: $("#set-ollama").value || null } });
      const a = s.active || {};
      S.health = { ...(S.health || {}), llm: a };
      updateModePills();
      toast(a.provider && a.provider !== "none" ? `Settings saved. Using ${PROVIDER[a.provider] || a.provider} with ${a.model}.` : "Settings saved. Simple mode is on.");
    } catch (e) { toast(e.message, true); }
  });

  // global buttons + keyboard
  document.addEventListener("click", (e) => {
    const a = e.target.closest("[data-action]");
    if (!a) return;
    const act = a.dataset.action;
    if (act === "theme") toggleTheme();
    else if (act === "settings") openSettings();
    else if (act === "map") toggleDrawer("map");
    else if (act === "sources") toggleDrawer("sources");
    else if (act === "help") $("#help-dialog").showModal();
  });
  $("#home-mode").addEventListener("click", openSettings);
  $("#deck-mode").addEventListener("click", openSettings);

  document.addEventListener("keydown", (e) => {
    if ($("#deck").hidden || document.querySelector("dialog[open]")) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) { if (e.key === "Escape") e.target.blur(); return; }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    const drawerOpen = !$("#map-drawer").hidden || !$("#source-drawer").hidden;
    if (k === "Escape") {
      e.preventDefault();
      if (drawerOpen) closeDrawers(); else if (!pop.hidden) hideDivePop(); else goUp();
    } else if (k === "Backspace") { e.preventDefault(); goUp(); }
    else if (k === "ArrowLeft") { e.preventDefault(); goSib(-1); }
    else if (k === "ArrowRight") { e.preventDefault(); goSib(1); }
    else if (/^[1-9]$/.test(k)) {
      const kids = ((S.node && S.node.children) || []).filter((c) => c.kind !== "dive");
      const t = kids[+k - 1];
      if (t) openRegion(t.id, $(`[data-node="${t.id}"]`));
    } else if (k === "n" || k === "N") goUnexplored();
    else if (k === "m" || k === "M") toggleDrawer("map");
    else if (k === "s" || k === "S") toggleDrawer("sources");
    else if (k === "/") {
      const a = $("#ask-input");
      if (a) { e.preventDefault(); a.scrollIntoView({ block: "center", behavior: reduceMotion ? "auto" : "smooth" }); a.focus({ preventScroll: true }); }
    } else if (k === "?") $("#help-dialog").showModal();
  });

  route();
})();
