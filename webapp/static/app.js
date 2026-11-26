/* Scraping Web App - front end */
(() => {
  "use strict";

  const ORDER = ["text", "hyperlinks", "buttons", "image", "pdf", "audio", "video", "documents"];
  const ICONS = {
    hyperlinks: "↗", buttons: "▣",
    text: "📄", image: "🖼️", pdf: "📕",
    audio: "🎵", video: "🎬", documents: "📊",
  };
  const LABELS = {
    hyperlinks: "Follow hyperlinks", buttons: "Explore interactive controls",
    text: "Text", image: "Images", pdf: "PDF",
    audio: "Audio", video: "Video", documents: "Other documents",
  };
  const DEFAULT_TEMPLATE = `{
  "prid": "prid",
  "title": "title",
  "text": "text",
  "date_time": "date_time",
  "source_url": "source_url",
  "website_name": "website_name",
  "image_url": [],
  "pdf_files": [],
  "language": "language",
  "collected_at": "collected_at"
}`;

  let CONFIG = null;
  let pollTimer = null;
  let openJobId = null;
  let STORAGE = {};
  // Job ids whose "Folder report" dropdown is open. The jobs list is rebuilt
  // every couple of seconds, so this survives the re-render instead of the
  // browser closing the <details> element each time.
  const openFolderReports = new Set();

  // ---------------- helpers ----------------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function fmtBytes(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(1)} ${u[i]}`;
  }

  function fmtTime(sec) {
    if (!sec) return "0s";
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
  }

  function toast(msg, isErr = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isErr ? " err" : "");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.add("hidden"), 4200);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* ignore */ }
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ---------------- UI builders ----------------
  function buildTypes(container, formats) {
    container.innerHTML = "";
    ORDER.forEach((kind) => {
      const card = document.createElement("div");
      card.className = "type-card on";
      card.dataset.kind = kind;

      const head = document.createElement("div");
      head.className = "type-head";

      const name = document.createElement("div");
      name.className = "type-name";
      name.innerHTML = `<span class="type-icon">${ICONS[kind]}</span>${LABELS[kind]}`;

      const sw = document.createElement("label");
      sw.className = "switch";
      sw.innerHTML = `<input type="checkbox" checked><span class="slider"></span>`;

      head.append(name, sw);
      card.append(head);

      if (formats[kind]) {
        const chips = document.createElement("div");
        chips.className = "chips";
        (formats[kind] || []).forEach((fmt) => {
          const chip = document.createElement("span");
          chip.className = "chip active";
          chip.dataset.fmt = fmt;
          chip.textContent = fmt;
          chip.title = `Include ${fmt.toUpperCase()} files (click to turn off)`;
          chip.onclick = () => {
            chip.classList.toggle("active");
            chip.title = chip.classList.contains("active")
              ? `Include ${fmt.toUpperCase()} files (click to turn off)`
              : `${fmt.toUpperCase()} files will not be downloaded`;
          };
          chips.append(chip);
        });
        card.append(chips);
      } else {
        const note = document.createElement("p");
        note.className = "hint";
        note.textContent = ({text: "Save page body text.", hyperlinks: "Follow links and pagination.",
          buttons: "Discover destinations and content behind controls."})[kind];
        card.append(note);
      }

      if (kind === "pdf") {
        const row = document.createElement("label");
        row.className = "pdf-text-row";
        row.innerHTML = `<input type="checkbox" class="pdf-text-toggle" checked>
          <span>Save extracted .txt file</span>`;
        card.append(row);
      }

      const input = $("input", sw);
      input.onchange = () => {
        card.classList.toggle("on", input.checked);
        const chips = $(".chips", card);
        if (chips) chips.classList.toggle("hidden", !input.checked);
        const extra = $(".pdf-text-row", card);
        if (extra) extra.classList.toggle("hidden", !input.checked);
      };
      container.append(card);
    });
  }

  function buildModes(container, modes, prefix) {
    container.innerHTML = "";
    modes.forEach((mode, i) => {
      const card = document.createElement("div");
      card.className = "mode-card" + (i === 0 ? " on" : "");
      card.dataset.mode = mode.key;

      const head = document.createElement("div");
      head.className = "type-head";
      head.innerHTML = `<div class="type-name">${mode.label}</div>
        <label class="switch"><input type="checkbox" ${i === 0 ? "checked" : ""}>
        <span class="slider"></span></label>`;
      card.append(head);

      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = mode.hint;
      card.append(hint);

      const input = $("input", head);
      input.onchange = () => {
        if (input.checked) {
          // One crawl scope at a time.
          $$(".mode-card", container).forEach((other) => {
            if (other === card) return;
            $("input", other).checked = false;
            other.classList.remove("on");
          });
        }
        card.classList.toggle("on", input.checked);
        syncLimits(prefix);
      };
      container.append(card);
    });
  }

  function selectedMode(prefix) {
    const card = $$(".mode-card", $(`#${prefix}-modes`))
      .find((c) => $("input", c).checked);
    return card ? card.dataset.mode : "internal";
  }

  // External depth / max external pages do nothing when the scope is internal.
  function syncLimits(prefix) {
    const externalOn = selectedMode(prefix) !== "internal";
    $$(`#${prefix}-limits [data-limit-group="external"]`).forEach((el) => {
      el.classList.toggle("hidden", !externalOn);
    });
  }

  function buildLimits(container, defaults) {
    container.innerHTML = "";
    const fields = [
      ["max_pages", "Max pages (0 = all)", defaults.max_pages],
      ["internal_depth", "Pages deep (0 = whole site)", defaults.internal_depth],
      ["max_depth", "External depth (0 = no limit)", defaults.max_depth, "external"],
      ["external_max_pages", "Max external pages (0 = no limit)", defaults.external_max_pages, "external"],
      ["delay", "Delay between requests (s)", defaults.delay],
      ["workers", "Parallel workers", defaults.workers],
      ["max_file_mb", "Max file size (MB)", defaults.max_file_mb],
    ];
    fields.forEach(([key, label, value, group]) => {
      const wrap = document.createElement("div");
      if (group) wrap.dataset.limitGroup = group;
      wrap.innerHTML = `<label class="field-label">${label}</label>
        <input type="number" data-limit="${key}" value="${value}" step="any" min="0">`;
      container.append(wrap);
    });

    // Category scheduling applies to all scopes.
    const catWrap = document.createElement("div");
    catWrap.innerHTML = `<label class="field-label">Category crawl mode</label>
      <label class="check">
        <input type="checkbox" data-limit="category_complete" ${defaults.category_complete ? "checked" : ""}>
        Complete each category fully before moving to the next
      </label>`;
    container.append(catWrap);

    const net = document.createElement("div");
    net.className = "proxy-job";
    net.innerHTML = `<label class="field-label">Proxy for this job (optional)</label>
      <textarea rows="2" data-proxy-list spellcheck="false"
        placeholder="Leave empty to use the global list"></textarea>
      <select data-proxy-mode>
        <option value="">Use global setting</option>
        <option value="real_ip">Real IP - ignore global list</option>
        <option value="rotate_on_block">Rotate when blocked</option>
        <option value="rotate_per_page">Rotate on every page</option>
        <option value="sticky">Sticky - one proxy for this job</option>
      </select>`;
    container.append(net);
  }

  function buildRefresh(container) {
    container.innerHTML = "";
    const fields = [
      ["after_days", "Only pages not checked in the last N days (0 = all)", 7],
      ["limit", "Max pages to re-check this run (0 = no cap)", 0],
    ];
    fields.forEach(([key, label, value]) => {
      const wrap = document.createElement("div");
      wrap.innerHTML = `<label class="field-label">${label}</label>
        <input type="number" data-refresh="${key}" value="${value}" step="1" min="0">`;
      container.append(wrap);
    });
    const media = document.createElement("div");
    media.innerHTML = `<label class="field-label">Files</label>
      <label class="check"><input type="checkbox" data-refresh="media"> Re-download changed files too</label>`;
    container.append(media);
  }

  function collectRefresh(prefix) {
    const on = $(`#${prefix}-refresh-on`).checked;
    if (!on) return { on: false };
    const out = { on: true };
    $$("[data-refresh]", $(`#${prefix}-refresh`)).forEach((inp) => {
      if (inp.type === "checkbox") out[inp.dataset.refresh] = inp.checked;
      else {
        const v = parseFloat(inp.value);
        out[inp.dataset.refresh] = Number.isFinite(v) ? v : 0;
      }
    });
    return out;
  }

  function collectTypes(container) {
    const out = {};
    $$(".type-card", container).forEach((card) => {
      const kind = card.dataset.kind;
      const on = $("input", card).checked;
      if (["text", "hyperlinks", "buttons"].includes(kind)) {
        out[kind] = on;
      } else {
        out[kind] = {
          on,
          formats: $$(".chip.active", card).map((c) => c.dataset.fmt),
        };
        if (kind === "pdf") {
          const toggle = $(".pdf-text-toggle", card);
          out[kind].pdf_text = toggle ? toggle.checked : true;
        }
      }
    });
    return out;
  }

  function collectModes(container) {
    const out = {};
    $$(".mode-card", container).forEach((card) => {
      out[card.dataset.mode] = $("input", card).checked;
    });
    return out;
  }

  function collectLimits(container) {
    const out = {};
    $$("[data-limit]", container).forEach((inp) => {
      if (inp.type === "checkbox") {
        out[inp.dataset.limit] = inp.checked;
      } else {
        const v = parseFloat(inp.value);
        out[inp.dataset.limit] = Number.isFinite(v) ? v : 0;
      }
    });
    const list = $("[data-proxy-list]", container);
    const mode = $("[data-proxy-mode]", container);
    const lines = list && list.value.trim()
      ? list.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean)
      : [];
    const chosen = mode ? mode.value : "";
    if (chosen === "real_ip") {
      out.proxies = [];
      out.proxy_mode = "rotate_on_block";
    } else {
      if (lines.length) out.proxies = lines;
      if (chosen) out.proxy_mode = chosen;
    }
    return out;
  }

  function collectConfig(prefix) {
    return {
      types: collectTypes($(`#${prefix}-types`)),
      modes: collectModes($(`#${prefix}-modes`)),
      limits: collectLimits($(`#${prefix}-limits`)),
      refresh: collectRefresh(prefix),
      json_template: $(`#${prefix}-template`).value,
    };
  }

  // ---------------- storage folder ----------------
  function siteNameFromUrl(raw) {
    try {
      let host = new URL(raw).hostname.toLowerCase().replace(/\.$/, "");
      if (host.startsWith("www.")) host = host.slice(4);
      return host.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "site";
    } catch (e) {
      return "";
    }
  }

  function storagePreview(prefix) {
    const input = $(`#${prefix}-storage-path`);
    const out = $(`#${prefix}-storage-preview`);
    if (!input || !out) return;
    const base = input.value.trim().replace(/[\\/]+$/, "");
    const site = siteNameFromUrl($(`#${prefix}-url`).value);
    out.textContent = base
      ? `Website folder: ${base}\\Scraped_Data\\${site || "<website_name>"}`
      : "";
  }

  function setStorageMessage(prefix, text, kind = "") {
    const msg = $(`#${prefix}-storage-msg`);
    const badge = $(`#${prefix}-storage-badge`);
    if (msg) {
      msg.textContent = text || "";
      msg.style.color = kind === "error" ? "#ff9d9d"
        : kind === "ok" ? "#7ee2a8" : "";
    }
    if (badge) {
      badge.className = "badge" + (kind === "ok" ? " done"
        : kind === "error" ? " error"
        : kind === "checking" ? " waiting" : "");
      badge.textContent = kind === "ok" ? "found"
        : kind === "error" ? "not found"
        : kind === "checking" ? "checking…" : "not checked";
    }
  }

  function applyStorage(info) {
    STORAGE = info || {};
    const root = STORAGE.output_root || "";
    ["scrape", "live"].forEach((prefix) => {
      const input = $(`#${prefix}-storage-path`);
      if (input) input.value = root;
      storagePreview(prefix);
    });
  }

  // ---------------- global proxy settings ----------------
  function setProxyMessage(text, kind) {
    const msg = $("#global-proxy-msg");
    if (msg) msg.textContent = text || "";
    const badge = $("#global-proxy-badge");
    if (badge) {
      badge.className = "badge" + (kind === "ok" ? " done"
        : kind === "error" ? " error" : kind === "checking" ? " waiting" : "");
      if (kind === "ok") badge.textContent = text.startsWith("No proxies")
        ? "direct connection" : "proxies saved";
      else if (kind === "error") badge.textContent = "failed";
      else if (kind === "checking") badge.textContent = "testing…";
      else badge.textContent = text || "direct connection";
    }
  }

  function applyProxySettings(settings) {
    const current = $("#global-proxy-current");
    const mode = $("#global-proxy-mode");
    if (mode && settings && settings.proxy_mode) mode.value = settings.proxy_mode;
    if (current) {
      const list = (settings && settings.proxies) || [];
      current.textContent = list.length
        ? "Current list (masked): " + list.join(" | ")
        : "No proxies saved yet - jobs use the real IP.";
    }
  }

  async function saveGlobalProxies(clear) {
    const text = $("#global-proxy-list") ? $("#global-proxy-list").value.trim() : "";
    const mode = $("#global-proxy-mode") ? $("#global-proxy-mode").value : "rotate_on_block";
    const body = { proxy_mode: mode };
    if (clear) body.proxies = [];
    else if (text) {
      body.proxies = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    }
    setProxyMessage("Saving…", "checking");
    try {
      const data = await api("/api/proxies", {
        method: "POST", body: JSON.stringify(body),
      });
      applyProxySettings(data);
      if ($("#global-proxy-list")) $("#global-proxy-list").value = "";
      setProxyMessage(data.has_proxies
        ? "Saved " + data.proxies.length + " proxy(ies). New jobs will use them."
        : "No proxies configured; jobs will use the real IP.", "ok");
    } catch (e) {
      setProxyMessage("Could not save: " + e.message, "error");
    }
  }

  async function testGlobalProxies() {
    const text = $("#global-proxy-list") ? $("#global-proxy-list").value.trim() : "";
    const body = text ? {
      proxies: text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
    } : {};
    setProxyMessage("Testing…", "checking");
    try {
      const data = await api("/api/proxies/test", {
        method: "POST", body: JSON.stringify(body),
      });
      if (!data.results || !data.results.length) {
        setProxyMessage(data.message || "No proxies to test.", "ok");
        return;
      }
      const ok = data.results.filter((r) => r.ok).length;
      const bad = data.results.length - ok;
      const detail = data.results.map((r) =>
        (r.ok ? "OK " : "FAIL ") + r.proxy +
        (r.status ? " (" + r.status + ", " + r.ms + "ms)" : "")
      ).join("\n");
      setProxyMessage(ok + " passed, " + bad + " failed\n" + detail, bad ? "error" : "ok");
    } catch (e) {
      setProxyMessage("Could not test: " + e.message, "error");
    }
  }

  async function checkStorage(prefix) {
    const input = $(`#${prefix}-storage-path`);
    const raw = input ? input.value.trim() : "";
    if (!raw) {
      setStorageMessage(prefix, "Enter a folder path first.", "error");
      return;
    }
    setStorageMessage(prefix, "Checking…", "checking");
    try {
      const data = await api("/api/storage/check", {
        method: "POST",
        body: JSON.stringify({ path: raw }),
      });
      if (!data.found) {
        setStorageMessage(prefix, data.message || "Folder not found.", "error");
        storagePreview(prefix);
        return;
      }
      if (!data.writable) {
        setStorageMessage(prefix, data.message || "Folder is not writable.", "error");
        storagePreview(prefix);
        return;
      }
      CONFIG.storage = data.storage;
      applyStorage(data.storage);
      ["scrape", "live"].forEach((p) => setStorageMessage(p, data.message, "ok"));
      toast("Storage folder found.");
    } catch (e) {
      setStorageMessage(prefix, e.message, "error");
    }
  }

  function initStorage() {
    applyStorage((CONFIG && CONFIG.storage) || {});
    ["scrape", "live"].forEach((prefix) => {
      const btn = $(`#${prefix}-storage-check`);
      const input = $(`#${prefix}-storage-path`);
      const url = $(`#${prefix}-url`);
      if (btn) btn.onclick = () => checkStorage(prefix);
      if (input) {
        input.addEventListener("input", () => {
          setStorageMessage(prefix, "", "");
          storagePreview(prefix);
        });
      }
      if (url) url.addEventListener("input", () => storagePreview(prefix));
    });
  }

  // ---------------- tabs ----------------
  function switchTab(name) {
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    if (name === "jobs") loadJobs();
    if (name === "dedupe") loadDedupe();
  }

  // ---------------- jobs ----------------
  async function loadJobs() {
    try {
      const data = await api("/api/jobs");
      renderJobs(data.jobs || []);
    } catch (e) {
      console.error(e);
    }
  }

  async function loadSites() {
    try {
      const data = await api("/api/sites");
      const sel = $("#scrape-existing");
      sel.innerHTML = `<option value="">Continue an already-scraped site…</option>`;
      (data.sites || []).forEach((s) => {
        if (!s.start_url) return;
        const opt = document.createElement("option");
        opt.value = s.start_url;
        opt.textContent = `${s.site} — ${s.pages} pages, ${s.files} files`;
        sel.append(opt);
      });
      sel.onchange = () => {
        if (sel.value) {
          $("#scrape-url").value = sel.value;
          toast("Loaded. Scraping resumes from the saved state for this site.");
        }
      };
    } catch (e) { /* picker is optional */ }
  }

  const STATUS_LABEL = {
    running: "Running", waiting: "Waiting", queued: "Queued",
    done: "Completed", stopped: "Stopped", error: "Failed",
  };
  const STATUS_ORDER = { running: 0, waiting: 1, queued: 2, stopped: 3, error: 4, done: 5 };

  function statusBadge(status) {
    const label = STATUS_LABEL[status] || status;
    return `<span class="badge ${status}">${label}</span>`;
  }

  function relTime(iso) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return "";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function metric(label, value) {
    return `<div class="metric"><span>${label}</span><b>${value}</b></div>`;
  }

  function renderJobsSummary(jobs) {
    const box = $("#jobs-summary");
    if (!box) return;
    if (!jobs.length) { box.innerHTML = ""; return; }
    const counts = {};
    jobs.forEach((j) => { counts[j.status] = (counts[j.status] || 0) + 1; });
    const chips = ["running", "waiting", "stopped", "error", "done"]
      .filter((s) => counts[s])
      .map((s) => `<span class="summary-chip ${s}">${counts[s]} ${STATUS_LABEL[s].toLowerCase()}</span>`)
      .join("");
    box.innerHTML = `<span class="summary-total">${jobs.length} job${jobs.length === 1 ? "" : "s"}</span>${chips}`;
  }

  function renderJobs(jobs) {
    const list = $("#jobs-list");
    $("#job-count").textContent = jobs.length;
    $("#jobs-empty").classList.toggle("hidden", jobs.length > 0);
    renderJobsSummary(jobs);

    if (!jobs.length) { list.innerHTML = ""; openFolderReports.clear(); return; }

    jobs = [...jobs].sort((a, b) =>
      (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
      String(b.created_at || "").localeCompare(String(a.created_at || "")));

    const currentIds = new Set(jobs.map((j) => j.id));
    openFolderReports.forEach((id) => {
      if (!currentIds.has(id)) openFolderReports.delete(id);
    });

    // Remember which folder reports are open before replacing the list.
    $$("details.job-folders", list).forEach((d) => {
      const id = d.dataset.job;
      if (!id) return;
      if (d.open) openFolderReports.add(id);
      else openFolderReports.delete(id);
    });

    list.innerHTML = "";
    jobs.forEach((job) => {
      const status = job.status || "queued";
      const stats = job.stats || {};
      const prog = stats.progress || {};
      const visited = prog.visited || 0;
      const queued = prog.queued || 0;
      const total = prog.total || (visited + queued);
      const finished = status === "done" && (prog.completed || !total);
      const percent = finished ? 100 : Math.min(100, prog.percent || 0);

      const kindLabel = job.kind === "live" ? "LIVE" : "SCRAPE";
      const live = job.live || {};
      const liveLine = job.kind === "live"
        ? `<div class="job-note">${esc(live.start || "?")} → ${esc(live.end || "no end date")} · every ${esc(String(live.interval_min || 30))} min</div>`
        : "";

      const metrics = [];
      if (total || visited) metrics.push(metric("Pages done", visited));
      if (queued) metrics.push(metric("Remaining", queued));
      if (stats.records != null) metrics.push(metric("Records", stats.records));
      if (stats.categories_scraped) {
        metrics.push(metric("Categories", stats.categories_scraped));
      }
      if (stats.translated_pages) {
        metrics.push(metric("Translated to English", stats.translated_pages));
      }
      if (stats.kept_original_language) {
        metrics.push(metric("Kept original language", stats.kept_original_language));
      }
      if (stats.empty_records) {
        metrics.push(metric("Empty pages", stats.empty_records));
      }
      if (stats.repair_progress && stats.repair_progress.total) {
        const rp = stats.repair_progress;
        metrics.push(metric(
          "Repairing empty pages",
          `${rp.done || 0}/${rp.total}${rp.repaired ? ` · ${rp.repaired} fixed` : ""}`
        ));
      }
      if (stats.media && stats.media.copied) metrics.push(metric("Media files", stats.media.copied));
      if (stats.json_bytes) metrics.push(metric("JSON size", fmtBytes(stats.json_bytes)));
      const links = stats.links || {};
      const linkParts = [];
      if (links.image) linkParts.push(`${links.image} img`);
      if (links.pdf) linkParts.push(`${links.pdf} pdf`);
      if (links.audio) linkParts.push(`${links.audio} audio`);
      if (links.video) linkParts.push(`${links.video} video`);
      if (links.documents) linkParts.push(`${links.documents} doc`);
      if (linkParts.length) metrics.push(metric("Links in JSON", linkParts.join(" · ")));
      if (status === "waiting" && stats.next_run_in) {
        metrics.push(metric("Next check", fmtTime(stats.next_run_in)));
      }
      if (stats.cycle) metrics.push(metric("Cycles", stats.cycle));

      const meta = [];
      if (job.started_at) meta.push(`<span>Started ${relTime(job.started_at)}</span>`);
      if (job.finished_at && status !== "running" && status !== "waiting") {
        meta.push(`<span>Finished ${relTime(job.finished_at)}</span>`);
      }
      if (prog.elapsed && status === "running") {
        meta.push(`<span>Elapsed ${fmtTime(prog.elapsed)}</span>`);
      }
      if (job.delivery) {
        meta.push(`<button class="link-btn" data-act="open" data-id="${job.id}" title="Open this folder">${esc(job.delivery)}</button>`);
      }

      const folders = stats.folders || [];
      const folderBytes = folders.reduce((n, f) => n + (f.bytes || 0), 0);
      const folderRows = folders.map((f) =>
        `<div class="folder-row"><span>${esc(f.path)}</span><span>${f.files} files · ${fmtBytes(f.bytes)}</span></div>`).join("");
      const folderBlock = folders.length
        ? `<details class="job-folders" data-job="${job.id}"${openFolderReports.has(job.id) ? " open" : ""}><summary>Folder report · ${folders.length} folders · ${fmtBytes(folderBytes)}</summary><div class="folder-list">${folderRows}</div></details>`
        : "";

      const actions = [];
      if (status === "running" || status === "waiting") {
        actions.push(`<button class="ghost danger" data-act="stop" data-id="${job.id}">Stop</button>`);
      } else if (status === "stopped" || status === "error") {
        actions.push(`<button class="primary" data-act="resume" data-id="${job.id}">Resume</button>`);
      } else if (status === "done") {
        actions.push(`<button class="ghost" data-act="resume" data-id="${job.id}" title="Continue from the saved state and fetch anything new">Run again</button>`);
      }
      if (status !== "running" && status !== "waiting" && job.kind !== "live") {
        actions.push(`<button class="ghost danger" data-act="restart" data-id="${job.id}" title="Move the saved pages to a backup and crawl from the beginning">Start over</button>`);
      }
      actions.push(`<button class="ghost" data-act="log" data-id="${job.id}">View log</button>`);
      if (job.delivery) {
        actions.push(`<button class="ghost" data-act="folders" data-id="${job.id}">Refresh folders</button>`);
      }
      if (folders.length) {
        actions.push(`<button class="ghost" data-act="dups" data-id="${job.id}">De-dupe info</button>`);
        actions.push(`<button class="ghost" data-act="rebuild" data-id="${job.id}">Create another JSON</button>`);
      }
      if (status !== "running" && status !== "waiting" && stats.empty_records) {
        if (job.repairing) {
          actions.push(`<button class="ghost danger" data-act="repair-stop" data-id="${job.id}">Stop repair</button>`);
        } else {
          actions.push(`<button class="primary" data-act="repair" data-id="${job.id}" title="Retry only the saved pages with no title or text (re-renders up to 50 by default)">Repair empty pages</button>`);
        }
      }
      if (job.delivery) {
        actions.push(`<button class="ghost" data-act="json" data-id="${job.id}">View JSON</button>`);
      }
      if (status !== "running" && status !== "waiting") {
        actions.push(`<button class="ghost danger" data-act="delete" data-id="${job.id}">Remove</button>`);
      }

      const el = document.createElement("article");
      el.className = `job job-${status}`;
      el.innerHTML = `
        <div class="job-head">
          <div class="job-id">
            <span class="kind-tag ${job.kind === "live" ? "live" : ""}">${kindLabel}</span>
            <div class="job-headings">
              <h3 class="job-title">${esc(job.display_name || job.site || "Website")}</h3>
              <a class="job-url" href="${esc(job.url || "#")}" target="_blank" rel="noopener noreferrer">${esc(job.url || "")}</a>
              ${liveLine}
            </div>
          </div>
          <div class="job-status">
            ${statusBadge(status)}
            <span class="job-site">${esc(job.site || "")}</span>
          </div>
        </div>
        <div class="job-progress">
          <div class="progress-top">
            <span>${total ? `${visited} of ${total} pages` : "No pages yet"}</span>
            <b>${percent}%</b>
          </div>
          <div class="bar ${status === "done" ? "done" : ""} ${status === "error" ? "error" : ""}">
            <div style="width:${percent}%"></div>
          </div>
        </div>
        ${metrics.length ? `<div class="job-metrics">${metrics.join("")}</div>` : ""}
        ${meta.length ? `<div class="job-meta">${meta.join("")}</div>` : ""}
        ${job.error ? `<div class="job-callout error">${esc(job.error)}</div>` : ""}
        ${stats.warning ? `<div class="job-callout warn">${esc(stats.warning)}</div>` : ""}
        ${stats.notice ? `<div class="job-callout">${esc(stats.notice)}</div>` : ""}
        ${status === "done" && !finished ? `<div class="job-callout">The page limit was reached, so more pages may remain. Use <b>Run again</b> to continue.</div>` : ""}
        ${folderBlock}
        <div class="job-actions">${actions.join("")}</div>`;
      list.append(el);
    });

    $$("[data-act]", list).forEach((btn) => {
      btn.onclick = () => handleJobAction(btn.dataset.act, btn.dataset.id);
    });
  }

  async function handleJobAction(act, id) {
    try {
      if (act === "stop") {
        await api(`/api/jobs/${id}/stop`, { method: "POST" });
        toast("Stopping — the crawler finishes the current request first.");
      } else if (act === "resume") {
        await api(`/api/jobs/${id}/resume`, { method: "POST" });
        toast("Resuming from the page where this job stopped.");
      } else if (act === "restart") {
        if (!window.confirm(
          "Start over?\n\nThe pages already saved for this job are moved to " +
          "webapp/data/backups, then the site is crawled again from the beginning."
        )) return;
        await api(`/api/jobs/${id}/restart`, { method: "POST" });
        toast("Starting over from the first page. The old folder was backed up.");
      } else if (act === "delete") {
        await api(`/api/jobs/${id}`, { method: "DELETE" });
        toast("Job removed from the list.");
      } else if (act === "open") {
        await api(`/api/jobs/${id}/open`, { method: "POST" });
        return;
      } else if (act === "log") {
        openJobId = id;
        const data = await api(`/api/jobs/${id}/log?lines=400`);
        openModal("Log", `<pre class="log"></pre>`);
        $("#modal-body pre").textContent = data.log || "(empty)";
        return;
      } else if (act === "repair") {
        if (!window.confirm(
          "Retry the saved pages that have no title or text?\n\n" +
          "This re-renders up to 50 pages with the same browser used by the " +
          "crawler. Only those records are updated; all other pages and media " +
          "stay untouched. It can take a few minutes."
        )) return;
        await api(`/api/jobs/${id}/repair-empty`, {
          method: "POST", body: JSON.stringify({ limit: 50 }),
        });
        toast("Repair started — watch this card for progress.");
      } else if (act === "repair-stop") {
        await api(`/api/jobs/${id}/repair-stop`, { method: "POST" });
        toast("Stopping the page repair…");
      } else if (act === "dups") {
        await showDuplicates(id);
        return;
      } else if (act === "rebuild") {
        showRebuild(id);
        return;
      } else if (act === "json") {
        await showJobJson(id);
        return;
      } else if (act === "folders") {
        await loadJobs();
        return;
      }
      loadJobs();
    } catch (e) {
      toast(e.message, true);
    }
  }

  // ---------------- modal helpers ----------------
  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function openModal(title, html) {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    $("#modal").classList.remove("hidden");
  }

  // ---------------- duplicates ----------------
  async function showDuplicates(id) {
    openModal("Duplicate files", `<p class="hint">Scanning every file by content…</p>`);
    try {
      const data = await api(`/api/jobs/${id}/duplicates`);
      renderDupReport(id, data.report, data.message);
    } catch (e) {
      openModal("Duplicate files",
        `<p class="hint" style="color:#ff9d9d">${esc(e.message)}</p>`);
    }
  }

  function renderDupReport(id, report, message) {
    const groups = report.groups || [];
    const head = message
      ? `<p class="hint">${esc(message)}</p>`
      : `<p class="hint">${report.scanned} files scanned · ` +
        `${report.duplicate_files} duplicate file(s) · ${fmtBytes(report.wasted_bytes)} can be freed</p>`;
    const list = groups.map((g) => `
      <div class="dup-group">
        <b>${esc(g.keep)}</b>
        <div>same as: ${g.duplicates.map(esc).join(", ")}</div>
        <div>${fmtBytes(g.size)} each</div>
      </div>`).join("");
    const removeBtn = report.duplicate_files
      ? `<button class="ghost danger" id="dup-remove">Remove ${report.duplicate_files} duplicate file(s)</button>`
      : "";
    openModal("Duplicate files",
      head + (list || `<p class="hint">No duplicate files. Nothing to clean.</p>`) +
      `<div class="row">${removeBtn}</div>`);

    const btn = $("#dup-remove");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      btn.textContent = "Removing…";
      try {
        const res = await api(`/api/jobs/${id}/duplicates/remove`, { method: "POST" });
        toast(`Removed ${res.result.removed} file(s), freed ${fmtBytes(res.result.freed)}.`);
        loadJobs();
        await showDuplicates(id);
      } catch (e) {
        toast(e.message, true);
        btn.disabled = false;
        btn.textContent = "Retry";
      }
    };
  }

  // ---------------- rebuild JSON from master ----------------
  function showRebuild(id) {
    openModal("Create another JSON", `
      <p class="hint">Reads this job's <code>master.json</code> and writes a new JSON
        file in the shape below — no crawling, no waiting.</p>
      <label class="field-label" for="rebuild-name">File name</label>
      <input type="text" id="rebuild-name" placeholder="e.g. only_pdfs">
      <label class="field-label" for="rebuild-template">Sample shape</label>
      <textarea id="rebuild-template" class="code" rows="12" spellcheck="false"></textarea>
      <div class="row"><button class="primary" id="rebuild-go">Create JSON</button></div>
      <p class="hint" id="rebuild-msg"></p>`);

    $("#rebuild-template").value = DEFAULT_TEMPLATE;
    $("#rebuild-go").onclick = async () => {
      const btn = $("#rebuild-go");
      const msg = $("#rebuild-msg");
      btn.disabled = true;
      btn.textContent = "Working…";
      try {
        const res = await api(`/api/jobs/${id}/rebuild`, {
          method: "POST",
          body: JSON.stringify({
            name: $("#rebuild-name").value || "custom",
            template: $("#rebuild-template").value,
          }),
        });
        msg.innerHTML = `Wrote <b>${esc(res.result.file)}</b> — ${res.result.records} records, ${fmtBytes(res.result.bytes)}. ` +
          `<button class="link-btn" id="rebuild-view">View JSON</button>`;
        $("#rebuild-view").onclick = () => {
          const name = String(res.result.file).split(/[\\/]/).pop();
          showJobJson(id, name);
        };
        toast("JSON created.");
      } catch (e) {
        msg.innerHTML = `<span style="color:#ff9d9d">${esc(e.message)}</span>`;
      } finally {
        btn.disabled = false;
        btn.textContent = "Create JSON";
      }
    };
  }

  // ---------------- formatted JSON viewer ----------------
  async function showJobJson(id, file) {
    try {
      const q = file ? `?file=${encodeURIComponent(file)}` : "";
      const data = await api(`/api/jobs/${id}/json${q}`);
      const files = data.files || [];
      const options = files.map((f) =>
        `<option value="${esc(f)}"${f === data.file ? " selected" : ""}>${esc(f)}</option>`).join("");
      const shown = data.truncated
        ? `Showing first ${data.shown} of ${data.records} records`
        : `${data.records} record${data.records === 1 ? "" : "s"}`;
      openModal("JSON data", `
        <div class="json-toolbar">
          <select id="json-file">${options}</select>
          <span class="hint">${shown} · ${fmtBytes(data.bytes)}</span>
        </div>
        <pre id="json-view" class="json-view"></pre>`);
      $("#json-view").textContent = data.text || "";
      const sel = $("#json-file");
      if (sel) sel.onchange = () => showJobJson(id, sel.value);
    } catch (e) {
      openModal("JSON data", `<p class="hint" style="color:#ff9d9d">${esc(e.message)}</p>`);
    }
  }

  // ---------------- de-dupe page ----------------
  const dedupePaths = new Map();

  function defaultDedupePath(target) {
    const parts = String(target.source || "").split(/[\\/]/).filter(Boolean);
    if (parts.length >= 2) {
      // source is <root>\Scraped_Data\<site>; use <root>\Deduped
      return `${parts.slice(0, -2).join("\\")}\\Deduped`;
    }
    return "";
  }

  async function loadDedupe() {
    try {
      const [targets, tasks] = await Promise.all([
        api("/api/dedupe/targets"),
        api("/api/dedupe/tasks"),
      ]);
      renderDedupe(targets.targets || [], tasks.tasks || []);
    } catch (e) {
      console.error(e);
    }
  }

  function renderDedupe(targets, tasks) {
    const list = $("#dedupe-list");
    if (!list) return;
    $("#dedupe-empty").classList.toggle("hidden", targets.length > 0);
    const counter = $("#dedupe-count");
    if (counter) counter.textContent = `${targets.length} website(s) ready to de-dupe`;

    // Remember paths typed by the user before the poll replaces the cards.
    $$("input[data-dedupe-site]", list).forEach((input) => {
      if (input.value) dedupePaths.set(input.dataset.dedupeSite, input.value);
    });

    list.innerHTML = "";
    targets.forEach((target) => {
      const site = target.site;
      const latest = tasks.find((t) => t.site === site) || null;
      const active = latest && ["running", "queued"].includes(latest.status);
      const path = dedupePaths.get(site) || defaultDedupePath(target);
      const legacy = target.legacy || [];

      const statsLine = [
        `${target.records || 0} pages`,
        `${target.categories || 0} categories`,
        fmtBytes(target.bytes),
      ].join(" · ");

      let taskBlock = "";
      if (latest) {
        const cls = latest.status === "error" ? "error"
          : latest.status === "done" ? "done" : "";
        taskBlock = `<div class="dedupe-task">
          <div class="progress-top"><span>${esc(latest.message || latest.status)}</span><b>${latest.percent || 0}%</b></div>
          <div class="bar ${cls}"><div style="width:${latest.percent || 0}%"></div></div>
          <div class="job-note">${latest.processed || 0}/${latest.total || 0} files · ${latest.unique || 0} unique · ${latest.duplicates || 0} duplicates</div>
        </div>`;
      }

      let resultBlock = "";
      const result = latest && latest.result;
      if (result && result.destination) {
        const isRestore = latest.type === "restore";
        resultBlock = `<div class="dedupe-result">
          <span>${isRestore ? "Restored" : "De-duped"}: <b>${esc(result.destination)}</b></span>
          <button class="link-btn" data-act="dedupe-open" data-path="${esc(result.destination)}">Open folder</button>
          ${result.manifest ? `<button class="link-btn" data-act="dedupe-manifest" data-id="${esc(latest.id)}">Download manifest</button>` : ""}
          ${result.bytes_saved ? `<span>saved ${fmtBytes(result.bytes_saved)}</span>` : ""}
        </div>`;
      }

      const actions = [];
      if (active) {
        actions.push(`<button class="ghost danger" data-act="dedupe-stop" data-id="${esc(latest.id)}">Stop task</button>`);
      } else {
        actions.push(`<button class="primary" data-act="dedupe-start" data-site="${esc(site)}">De-dupe</button>`);
        actions.push(`<button class="ghost" data-act="dedupe-restore" data-site="${esc(site)}" title="Rebuild the full, non-de-duplicated folder from the manifest">Restore full</button>`);
      }
      actions.push(`<button class="ghost" data-act="dedupe-open-source" data-path="${esc(target.source)}">Open original</button>`);
      if (legacy.length) {
        actions.push(`<button class="ghost danger" data-act="dedupe-cleanup" data-site="${esc(site)}" title="Remove ${legacy.join(", ")} after verifying the files exist in categories/">Clean old folders</button>`);
      }

      const el = document.createElement("article");
      el.className = "job dedupe-card";
      el.innerHTML = `
        <div class="job-head">
          <div class="job-id">
            <span class="kind-tag">SITE</span>
            <div class="job-headings">
              <h3 class="job-title">${esc(site)}</h3>
              <div class="job-note">${esc(statsLine)}</div>
              <div class="job-note">Original: ${esc(target.source)}</div>
            </div>
          </div>
          <div class="job-status">${statusBadge(target.status)}${legacy.length ? `<span class="badge warning">${legacy.length} old folder${legacy.length > 1 ? "s" : ""}</span>` : ""}</div>
        </div>
        <div class="dedupe-path-row">
          <label class="field-label" for="dedupe-path-${esc(site)}">Save de-duped copy in (parent folder)</label>
          <input id="dedupe-path-${esc(site)}" type="text" data-dedupe-site="${esc(site)}"
                 value="${esc(path)}" placeholder="C:\\Deduped" spellcheck="false">
        </div>
        ${taskBlock}
        ${resultBlock}
        ${legacy.length ? `<div class="job-callout warn">Old duplicate folders (${esc(legacy.join(", "))}) still exist. "Clean old folders" deletes them only after their files are verified in categories/.</div>` : ""}
        <div class="job-actions">${actions.join("")}</div>`;
      list.append(el);
    });

    $$("[data-act]", list).forEach((btn) => {
      btn.onclick = () => handleDedupeAction(btn.dataset.act, btn);
    });
  }

  function dedupeDestination(site) {
    const input = $$("input[data-dedupe-site]").find(
      (el) => el.dataset.dedupeSite === site
    );
    const value = input ? input.value.trim() : "";
    if (value) dedupePaths.set(site, value);
    return value;
  }

  async function handleDedupeAction(act, btn) {
    try {
      if (act === "dedupe-start" || act === "dedupe-restore") {
        const site = btn.dataset.site;
        const destination = dedupeDestination(site);
        if (!destination) { toast("Enter the folder where the copy should be saved.", true); return; }
        btn.disabled = true;
        await api("/api/dedupe/start", {
          method: "POST",
          body: JSON.stringify({
            site,
            destination,
            mode: act === "dedupe-restore" ? "restore" : "dedupe",
          }),
        });
        toast(act === "dedupe-restore"
          ? "Restore started — rebuilding the full folder."
          : "De-dupe started — the original folder stays unchanged.");
      } else if (act === "dedupe-stop") {
        await api(`/api/dedupe/tasks/${btn.dataset.id}/stop`, { method: "POST" });
        toast("Stopping this task…");
      } else if (act === "dedupe-open" || act === "dedupe-open-source") {
        await api("/api/dedupe/open", {
          method: "POST",
          body: JSON.stringify({ path: btn.dataset.path }),
        });
        return;
      } else if (act === "dedupe-manifest") {
        window.location.href = `/api/dedupe/tasks/${encodeURIComponent(btn.dataset.id)}/manifest`;
        return;
      } else if (act === "dedupe-cleanup") {
        const site = btn.dataset.site;
        if (!window.confirm(
          `Clean old duplicate folders for ${site}?\n\n` +
          "Files are deleted only after they are found in categories/. " +
          "The original categories/ folder is never touched."
        )) return;
        const res = await api("/api/dedupe/cleanup", {
          method: "POST",
          body: JSON.stringify({ site }),
        });
        const deleted = (res.result.results || []).filter((r) => r.deleted).map((r) => r.folder);
        const refused = (res.result.results || []).filter((r) => !r.deleted && r.unmatched);
        if (refused.length) {
          toast("Some folders were kept: files were missing from categories/.", true);
        } else if (deleted.length) {
          toast(`Removed: ${deleted.join(", ")}.`);
        } else {
          toast("No old folders to clean.");
        }
      }
      loadDedupe();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
      loadDedupe();
    }
  }

  // ---------------- start ----------------
  async function startJob(kind) {
    const prefix = kind === "live" ? "live" : "scrape";
    const url = $(`#${prefix}-url`).value.trim();

    if (!/^https?:\/\//i.test(url)) {
      toast("Enter a full URL starting with http:// or https://", true);
      return;
    }

    let config;
    try {
      config = collectConfig(prefix);
    } catch (e) {
      toast(e.message, true);
      return;
    }
    const mediaOn = ORDER.filter((k) => k !== "text")
      .some((k) => config.types[k] && config.types[k].on);
    if (!config.types.text && !config.types.buttons && !config.types.hyperlinks && !mediaOn) {
      toast("Turn on at least one What to Collect option.", true);
      return;
    }

    const storageInput = $(`#${prefix}-storage-path`);
    const outputRoot = storageInput ? storageInput.value.trim() : "";
    if (!outputRoot) {
      toast("Enter a storage folder and click Check.", true);
      return;
    }

    const payload = { url, kind, config, output_root: outputRoot };
    if (kind === "live") {
      const start = $("#live-start").value;
      if (!start) { toast("Pick a start date for live watching.", true); return; }
      payload.live = {
        start,
        end: $("#live-end").value || "",
        interval_min: parseFloat($("#live-interval").value) || 30,
      };
    }

    const btn = kind === "live" ? $("#live-start-btn") : $("#scrape-start");
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
      toast("Job started. It creates its own folder for this domain.");
      switchTab("jobs");
      loadJobs();
    } catch (e) {
      toast(e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = kind === "live" ? "Start live watching" : "Start scraping";
    }
  }

  // ---------------- preview ----------------
  async function previewTemplate(prefix) {
    const out = $(`#${prefix}-preview-out`);
    try {
      const data = await api("/api/template/preview", {
        method: "POST",
        body: JSON.stringify({ template: $(`#${prefix}-template`).value }),
      });
      out.textContent = JSON.stringify(data.preview, null, 2);
      out.classList.remove("hidden");
    } catch (e) {
      out.textContent = e.message;
      out.classList.remove("hidden");
    }
  }

  // ---------------- boot ----------------
  async function init() {
    $("#scrape-template").value = DEFAULT_TEMPLATE;
    $("#live-template").value = DEFAULT_TEMPLATE;

    try {
      CONFIG = await api("/api/config");
    } catch (e) {
      toast("Cannot reach the server: " + e.message, true);
      return;
    }

    buildTypes($("#scrape-types"), CONFIG.formats);
    buildTypes($("#live-types"), CONFIG.formats);
    buildModes($("#scrape-modes"), CONFIG.modes, "scrape");
    buildModes($("#live-modes"), CONFIG.modes, "live");
    buildLimits($("#scrape-limits"), CONFIG.defaults);
    buildLimits($("#live-limits"), CONFIG.defaults);
    syncLimits("scrape");
    syncLimits("live");
    buildRefresh($("#scrape-refresh"));
    buildRefresh($("#live-refresh"));
    initStorage();
    applyProxySettings(CONFIG.proxies || {});
    $("#global-proxy-save").onclick = () => saveGlobalProxies(false);
    $("#global-proxy-test").onclick = testGlobalProxies;
    $("#global-proxy-clear").onclick = () => saveGlobalProxies(true);

    ["scrape", "live"].forEach((prefix) => {
      const sw = $(`#${prefix}-refresh-on`);
      sw.onchange = () => $(`#${prefix}-refresh`).classList.toggle("hidden", !sw.checked);
    });

    loadSites();

    const disk = CONFIG.disk || {};
    $("#disk-info").textContent =
      `free ${fmtBytes(disk.free)} of ${fmtBytes(disk.total)}`;

    // today for live start
    $("#live-start").value = new Date().toISOString().slice(0, 10);

    $$(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
    $("#scrape-start").onclick = () => startJob("scrape");
    $("#live-start-btn").onclick = () => startJob("live");
    $("#scrape-preview").onclick = () => previewTemplate("scrape");
    $("#live-preview").onclick = () => previewTemplate("live");
    $("#refresh-jobs").onclick = loadJobs;
    $("#refresh-dedupe").onclick = loadDedupe;
    $("#modal-close").onclick = () => $("#modal").classList.add("hidden");

    loadJobs();
    pollTimer = setInterval(() => {
      if ($("#tab-jobs").classList.contains("active")) loadJobs();
      if ($("#tab-dedupe").classList.contains("active")) loadDedupe();
    }, 2000);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
