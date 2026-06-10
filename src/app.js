// ── State ──────────────────────────────────────────────────────────────────
const state = {
  galleryFilter: "",
  queue: [], history: [],
  stats: { pending: 0, confirmed: 0, total: 0 },
  pollerStatus: { running: false, last_poll: null, last_error: "", next_poll_in: 0 },
  unclaimed_threshold: 30,
  recentScans: [],
  selectedOrder: null,
  samplesFolder: "",
  samplesFiles: [],
  samplesSelected: new Set(),
  logVisible: true,
  updateInfo: null,
  jobs: [],
  queueFilter: "pending",
  queueSortAsc: false,
};

// ── API helpers ────────────────────────────────────────────────────────────
async function apiGet(path, params = {}) {
  const url = new URL("/api/" + path, location.origin);
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") url.searchParams.set(k, v); });
  const r = await fetch(url);
  return r.json();
}
async function apiPost(path, body = {}) {
  const r = await fetch("/api/" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
}

// ── SSE ────────────────────────────────────────────────────────────────────
function initSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    try {
      const { event, data } = JSON.parse(e.data);
      if (event === "new_orders") { toast(`📦 ${data.count} new order(s)`, "info"); refreshAll(); }
      else if (event === "poll_complete") updatePollerStatus();
      else if (event === "download_done") refreshQueue();
      else if (event === "poll_error") { setApiStatus(false, data.error); updatePollerStatus(); }
      else if (event === "activity") appendLogLine(data.message, data.level);
      else if (event === "update_available") showUpdateAvailable(data);
      else if (event === "update_progress") handleUpdateProgress(data);
      else if (event === "update_error") { toast(`Update failed: ${data.error}`, "error"); hideUpdateProgress(); }
      else if (event === "order_confirmed" || event === "order_fulfilled") { refreshAll(); if (state.selectedOrder?.order_num === data.order_num) openDetail(data.order_num); }
      else if (event === "jobs_updated") { refreshJobs(); }
      else if (event === "job_history_done") {
        if (data.gallery === state.galleryFilter) {
          document.getElementById("selected-job-label").textContent = data.gallery;
          if (data.ok && data.count > 0) {
            toast(`📂 ${data.count} new order(s) synced for '${data.gallery}'`, "info");
            refreshAll();
          }
        }
      }
    } catch(e) { console.warn("[SSE]", e); }
  };
  es.onerror = () => console.warn("[SSE] reconnecting…");
}

// ── Navigation ─────────────────────────────────────────────────────────────
function showPanel(name, el) {
  document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
  const panel = document.getElementById(`panel-${name}`);
  if (!panel) return;
  panel.classList.add("active");
  if (el) el.classList.add("active");
  if (name === "scan") setTimeout(() => document.getElementById("scan-input").focus(), 80);
  if (name === "settings") setTimeout(() => loadSettings().catch(console.error), 40);
  if (name === "history") renderHistory();
  if (name === "samples") loadSamples();
}

// ── Jobs searchable dropdown ───────────────────────────────────────────────
function onJobSearch() {
  const q = document.getElementById("job-search-input").value.toLowerCase();
  renderJobDropdown(q);
  showJobDropdown();
}

function showJobDropdown() {
  const dd = document.getElementById("job-dropdown");
  renderJobDropdown(document.getElementById("job-search-input").value.toLowerCase());
  dd.style.display = "block";
  setTimeout(() => {
    document.addEventListener("click", closeJobDropdown, { once: true });
  }, 0);
}

function closeJobDropdown(e) {
  const wrap = document.getElementById("gallery-filter-wrap");
  if (wrap && wrap.contains(e.target)) return;
  document.getElementById("job-dropdown").style.display = "none";
}

function renderJobDropdown(query = "") {
  const dd = document.getElementById("job-dropdown");
  const jobs = state.jobs || [];
  const filtered = query
    ? jobs.filter(j => j.gallery.toLowerCase().includes(query))
    : jobs;

  const allJobsHtml = `<div class="job-option${!state.galleryFilter ? " selected" : ""}" onclick="selectJob('')">
    All Jobs <span style="color:var(--text3);font-size:10px">(${state.stats.total||0} orders)</span>
  </div>`;

  const jobsHtml = filtered.map(j => `
    <div class="job-option${state.galleryFilter === j.gallery ? " selected" : ""}" onclick="selectJob(${JSON.stringify(j.gallery)})">
      ${esc(j.gallery)}
      <span style="color:var(--text3);font-size:10px"> (${j.order_count})</span>
    </div>`).join("");

  dd.innerHTML = allJobsHtml + (jobsHtml || '<div class="job-option" style="color:var(--text3)">No jobs found</div>');
}

async function selectJob(gallery) {
  document.getElementById("job-dropdown").style.display = "none";
  document.getElementById("job-search-input").value = gallery;
  document.getElementById("selected-job-label").textContent = gallery || "All Jobs";
  state.galleryFilter = gallery;

  try { localStorage.setItem("pdx_job_filter", gallery); } catch(e) {}

  if (gallery) {
    state.queueFilter = "all";
    document.getElementById("queue-filter").value = "all";
    document.getElementById("selected-job-label").textContent = `⏳ Loading ${gallery}…`;

    try {
      await apiPost("fetch_job_history", { gallery });
    } catch(e) {}
    await refreshAll();
    document.getElementById("selected-job-label").textContent = gallery;
  } else {
    state.queueFilter = "pending";
    document.getElementById("queue-filter").value = "pending";
    await refreshAll();
  }
}

function restoreJobFilter() {
  try {
    const saved = localStorage.getItem("pdx_job_filter");
    if (saved) {
      state.galleryFilter = saved;
      state.queueFilter = "all";
      document.getElementById("job-search-input").value = saved;
      document.getElementById("selected-job-label").textContent = saved;
      setTimeout(() => {
        const qf = document.getElementById("queue-filter");
        if (qf) qf.value = "all";
      }, 0);
    }
  } catch(e) {}
}

async function refreshJobs() {
  state.jobs = await apiGet("get_jobs");
  renderJobDropdown();
}

// ── Search ─────────────────────────────────────────────────────────────────
let searchDebounce = null;
function onSearch() {
  clearTimeout(searchDebounce);
  const q = document.getElementById("search-input").value.trim();
  if (!q) { clearSearch(); return; }
  searchDebounce = setTimeout(() => doSearch(q), 280);
}

async function doSearch(q) {
  const results = await apiGet("search", { q, gallery: state.galleryFilter });
  const overlay = document.getElementById("search-overlay");
  const list = document.getElementById("search-results-list");
  const countEl = document.getElementById("search-results-count");

  overlay.style.display = "block";
  countEl.textContent = `${results.length} result${results.length !== 1 ? "s" : ""} for "${q}"`;

  if (results.length === 0) {
    list.innerHTML = `<div class="empty-state"><div class="icon">🔍</div><div class="title">No results</div><div class="sub">No orders match "${esc(q)}"</div></div>`;
    return;
  }

  list.innerHTML = results.map(o => {
    const filenameMatch = (JSON.parse(o.images_json || "[]")).find(img => img.filename && img.filename.toLowerCase().includes(q.toLowerCase()));
    return `<div class="search-result-card" onclick="openDetail('${esc(o.order_num)}')">
      <div class="sr-header">
        <span class="sr-num">${esc(o.order_num)}</span>
        <span class="sr-status ${o.status === 'fulfilled' ? 'fulfilled' : 'received'}">${o.status === 'fulfilled' ? 'Completed' : 'Pending'}</span>
      </div>
      <div class="sr-customer">${esc(o.customer_name)}</div>
      <div class="sr-gallery">${esc(o.gallery || "—")}</div>
      ${filenameMatch ? `<div class="sr-match">📎 Matched: ${esc(filenameMatch.filename)}</div>` : ""}
    </div>`;
  }).join("");
}

function clearSearch() {
  document.getElementById("search-input").value = "";
  document.getElementById("search-overlay").style.display = "none";
  document.getElementById("search-results-count").textContent = "";
}

// ── Queue ──────────────────────────────────────────────────────────────────
async function refreshQueue() {
  state.queue = await apiGet("get_queue", { gallery: state.galleryFilter });
  renderQueue();
}

function onQueueFilterChange() {
  state.queueFilter = document.getElementById("queue-filter").value;
  renderQueue();
}

function toggleQueueSort() {
  state.queueSortAsc = !state.queueSortAsc;
  document.getElementById("queue-sort-th").textContent = `Received ${state.queueSortAsc ? "↑" : "↓"}`;
  renderQueue();
}

function renderQueue() {
  const tbody = document.getElementById("queue-tbody");
  const threshold = state.unclaimed_threshold;
  const now = Date.now();
  const filter = state.queueFilter;

  let rows = [];
  if (filter === "all") {
    rows = [
      ...state.queue.map(o => ({ ...o, _type: "pending" })),
      ...state.history.map(o => ({ ...o, _type: "fulfilled" }))
    ];
  } else if (filter === "pending") {
    rows = state.queue.map(o => ({ ...o, _type: "pending" }));
  } else if (filter === "completed") {
    rows = state.history.map(o => ({ ...o, _type: "fulfilled" }));
  } else if (filter === "needs_attention") {
    rows = state.queue
      .filter(o => (now - new Date(o.received_at).getTime()) / 60000 >= threshold)
      .map(o => ({ ...o, _type: "pending" }));
  }

  rows.sort((a, b) => {
    const ta = new Date(a.received_at).getTime();
    const tb = new Date(b.received_at).getTime();
    return state.queueSortAsc ? ta - tb : tb - ta;
  });

  const pendingRows = state.queue;
  if (pendingRows.length) {
    const avgMin = pendingRows.reduce((sum, o) => sum + (now - new Date(o.received_at).getTime()) / 60000, 0) / pendingRows.length;
    document.getElementById("avg-wait-display").textContent = `Avg wait: ${formatAge(avgMin)}`;
  } else {
    document.getElementById("avg-wait-display").textContent = "";
  }

  const pendingCount = state.queue.length;
  const unclaimed = state.queue.filter(o => (now - new Date(o.received_at).getTime()) / 60000 >= threshold).length;
  updateBadge("badge-queue", pendingCount, unclaimed > 0);
  document.getElementById("queue-sub").textContent = `${rows.length} order(s)`;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:40px;color:var(--text3)">No orders to display</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(o => {
    const isPending = o._type === "pending";
    const ageMin = (now - new Date(o.received_at).getTime()) / 60000;
    const isAlert = isPending && ageMin >= threshold;
    const isWarn  = isPending && !isAlert && ageMin >= threshold * 0.5;
    const rowClass = isAlert ? "queue-row-alert" : isWarn ? "queue-row-warn" : !isPending ? "queue-row-confirmed" : "";

    const items = parseItems(o.items_json);
    const itemStr = items.map(it => `${it.files||it.qty}× ${esc(it.desc||it.sku)}`).join(", ") || "—";

    let statusHtml = "";
    if (o._type === "fulfilled") {
      statusHtml = `<span class="status-pill confirmed">✓ Completed</span>`;
    } else if (o.fulfill_status === "fulfilled") {
      statusHtml = `<span class="status-pill fulfilled">🖨 Printed</span>`;
    } else if (isAlert) {
      statusHtml = `<span class="status-pill pending" style="color:var(--red);border-color:rgba(239,68,68,.3)">⚠ Needs Attention</span>`;
    } else {
      statusHtml = `<span class="status-pill pending">⏳ Pending</span>`;
    }

    const dl = o.download_status || "pending";
    const dlIcon = dl === "ok" ? "📁" : dl === "failed" ? "⚠" : "⏳";

    const waitStr = isPending
      ? `<span style="color:${isAlert?'var(--red)':isWarn?'var(--amber)':'var(--text2)'};font-weight:${isAlert?'700':'400'}">${formatAge(ageMin)}</span>`
      : (o.confirmed_at ? formatAge((new Date(o.confirmed_at)-new Date(o.received_at))/60000) : "—");

    const actions = isPending
      ? `<div class="queue-actions">
           <button class="btn-xs btn-xs-blue" onclick="event.stopPropagation();openDetail('${esc(o.order_num)}')">View</button>
         </div>`
      : `<div class="queue-actions">
           <button class="btn-xs btn-xs-ghost" onclick="event.stopPropagation();openDetail('${esc(o.order_num)}')">View</button>
         </div>`;

    return `<tr class="${rowClass}" onclick="openDetail('${esc(o.order_num)}')" style="cursor:pointer">
      <td class="td-mono" style="font-size:10px">${esc(o.order_num)}</td>
      <td class="td-bold" style="font-size:12px">${esc(o.customer_name)}</td>
      <td style="font-size:11px;color:var(--text3);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(o.gallery||"—")}</td>
      <td style="font-size:11px">${esc(itemStr)} ${dlIcon}</td>
      <td class="td-mono" style="font-size:10px;color:var(--text3)">${formatTime(o.received_at)}</td>
      <td style="font-size:11px">${waitStr}</td>
      <td>${statusHtml}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
}

// ── Order Detail Slide-out ─────────────────────────────────────────────────
async function openDetail(orderNum) {
  const order = await apiGet("get_order", { order_num: orderNum });
  if (order.error) { toast("Order not found", "error"); return; }
  state.selectedOrder = order;

  document.getElementById("detail-order-num").textContent = order.order_num;
  document.getElementById("detail-customer").textContent = order.customer_name;
  document.getElementById("detail-gallery").textContent = order.gallery || "—";
  document.getElementById("detail-placed").textContent = formatTime(order.placed_at);
  document.getElementById("detail-received").textContent = formatTime(order.received_at);
  document.getElementById("detail-status-text").textContent = order.status === "fulfilled" ? "✅ Completed" : "⏳ Pending";
  document.getElementById("detail-fulfilled-text").textContent = order.fulfill_status === "fulfilled" ? formatTime(order.fulfilled_at) : "—";

  const badge = document.getElementById("detail-status-badge");
  if (order.status === "fulfilled") {
    badge.className = "detail-status-badge confirmed"; badge.textContent = "✅ Completed";
  } else if (order.fulfill_status === "fulfilled") {
    badge.className = "detail-status-badge fulfilled"; badge.textContent = "🖨 Printed — Awaiting Scan";
  } else {
    badge.className = "detail-status-badge pending"; badge.textContent = "⏳ Awaiting Fulfill";
  }

  const isFulfilled = order.fulfill_status === "fulfilled";
  const isConfirmed = order.status === "fulfilled";
  document.getElementById("btn-detail-fulfill").disabled = isFulfilled || isConfirmed;
  document.getElementById("btn-detail-reprint-img").disabled = !isFulfilled && !isConfirmed;

  const items = parseItems(order.items_json);
  document.getElementById("detail-items").innerHTML = items.map(it =>
    `<div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">
      <span style="color:var(--accent);font-weight:700">${it.files||it.qty}×</span>
      <span style="color:var(--text)">${esc(it.desc||it.sku)}</span>
    </div>`
  ).join("") || "<div style='color:var(--text3);font-size:12px'>No items</div>";

  const images = JSON.parse(order.images_json || "[]");
  document.getElementById("detail-images").innerHTML = images.map(img =>
    `<div>
      <div class="detail-img-wrap">
        <img src="/api/image/${encodeURIComponent(order.order_num)}/${encodeURIComponent(img.filename)}"
             alt="${esc(img.filename)}"
             data-fallback="${esc(img.assetUrl || '')}"
             onerror="if(!this.dataset.tried&&this.dataset.fallback){this.dataset.tried=1;this.src=this.dataset.fallback}else{this.parentElement.innerHTML='<div class=img-loading style=font-size:32px>🖼</div>'}">
      </div>
      <div class="detail-img-name" title="${esc(img.filename)}">${esc(img.filename)}</div>
    </div>`
  ).join("") || "<div style='color:var(--text3);font-size:12px'>No images</div>";

  document.getElementById("order-detail").classList.add("open");
  document.getElementById("detail-overlay").classList.add("show");
  renderQueue();
}

function closeDetail() {
  document.getElementById("order-detail").classList.remove("open");
  document.getElementById("detail-overlay").classList.remove("show");
  state.selectedOrder = null;
  renderQueue();
}

async function detailFulfill() {
  if (!state.selectedOrder) return;
  const btn = document.getElementById("btn-detail-fulfill");
  btn.disabled = true; btn.textContent = "⏳ Fulfilling…";
  const result = await apiPost("fulfill_order", { order_num: state.selectedOrder.order_num });
  if (result.ok) {
    toast(`🖨 Sent to printer: ${state.selectedOrder.order_num}`, "success");
    await openDetail(state.selectedOrder.order_num);
  } else {
    toast(`Fulfill failed: ${result.error}`, "error");
    btn.disabled = false; btn.textContent = "🖨 Send to Printer";
  }
}

async function detailReprintImages() {
  if (!state.selectedOrder) return;
  const btn = document.getElementById("btn-detail-reprint-img");
  btn.disabled = true; btn.textContent = "⏳ Reprinting…";
  const result = await apiPost("reprint_images", { order_num: state.selectedOrder.order_num });
  if (result.ok) {
    toast(`🔁 Images requeued: ${state.selectedOrder.order_num}`, "success");
  } else {
    toast(`Reprint failed: ${result.error}`, "error");
  }
  btn.disabled = false; btn.textContent = "🔁 Reprint Images";
}

async function detailReprintReceipt() {
  if (!state.selectedOrder) return;
  const btn = document.querySelector(".btn-reprint-rcpt");
  btn.disabled = true; btn.textContent = "⏳ Printing…";
  const result = await apiPost("reprint_receipt", { order_num: state.selectedOrder.order_num });
  if (result.ok) { toast("🧾 Receipt reprinted", "info"); }
  else { toast(`Receipt failed: ${result.error}`, "error"); }
  btn.disabled = false; btn.textContent = "🧾 Reprint Receipt";
}

// ── History ────────────────────────────────────────────────────────────────
async function refreshHistory() {
  state.history = await apiGet("get_history", { gallery: state.galleryFilter });
}

function renderHistory() {
  const tbody = document.getElementById("history-tbody");
  const sub = document.getElementById("history-sub");
  if (!tbody) return;
  if (!state.history.length) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:36px;color:var(--text3)">No confirmed orders yet</td></tr>`;
    if (sub) sub.textContent = "No orders confirmed"; return;
  }
  if (sub) sub.textContent = `${state.history.length} order(s) confirmed`;
  tbody.innerHTML = state.history.map(o => {
    const items = parseItems(o.items_json);
    const itemStr = items.map(i => `${i.files||i.qty}× ${i.desc||i.sku}`).join(", ") || "—";
    const wait = o.confirmed_at && o.received_at ? formatAge((new Date(o.confirmed_at)-new Date(o.received_at))/60000) : "—";
    return `<tr onclick="openDetail('${esc(o.order_num)}')">
      <td class="td-mono">${esc(o.order_num)}</td>
      <td class="td-bold">${esc(o.customer_name)}</td>
      <td style="font-size:11px;color:var(--text3)">${esc(o.gallery||"—")}</td>
      <td style="font-size:11px">${esc(itemStr)}</td>
      <td class="td-mono" style="font-size:10px;color:var(--text3)">${formatTime(o.received_at)}</td>
      <td class="td-mono" style="font-size:10px;color:var(--text3)">${formatTime(o.confirmed_at)}</td>
      <td><span class="td-badge">✓ ${esc(wait)}</span></td>
    </tr>`;
  }).join("");
}

// ── Scan ───────────────────────────────────────────────────────────────────
function onScanInput() {
  const val = document.getElementById("scan-input").value.trim();
  document.getElementById("scan-submit-btn").disabled = !val;
  if (val) setScanStatus("info", "⌖", `Order: ${val}`);
  else setScanStatus("", "⌖", "Ready — waiting for scan");
}

async function submitScan() {
  const input = document.getElementById("scan-input");
  const orderNum = input.value.trim().toUpperCase();
  if (!orderNum) return;

  const done = (delay = 0) => {
    input.disabled = false;
    if (delay > 0) {
      setTimeout(() => {
        input.value = "";
        setScanStatus("", "⌖", "Ready — waiting for scan");
        document.getElementById("scan-submit-btn").disabled = true;
        input.focus();
      }, delay);
    } else {
      input.focus();
    }
  };

  setScanStatus("info", "⏳", "Looking up order…");
  input.disabled = true;

  const inQueue   = state.queue.find(o => o.order_num === orderNum);
  const inHistory = state.history.find(o => o.order_num === orderNum);

  if (!inQueue) {
    if (inHistory) {
      setScanStatus("warn", "⚠", `${orderNum} already completed at ${formatTime(inHistory.confirmed_at || inHistory.received_at)}`);
      db_log_activity(`Already scanned: ${orderNum} — previously confirmed at ${formatTime(inHistory.confirmed_at)}`);
    } else {
      setScanStatus("error", "✗", `Order ${orderNum} not found in queue`);
    }
    done();
    return;
  }

  try {
    const result = await apiPost("confirm_order", { order_num: orderNum });
    if (result.ok) {
      setScanStatus("success", "✓", `${orderNum} confirmed! ${inQueue.customer_name} — completed`);
      addRecentScan(orderNum, inQueue.customer_name);
      refreshAll();
      done(3000);
    } else {
      setScanStatus("error", "✗", `Failed: ${result.error}`);
      done();
    }
  } catch(e) {
    setScanStatus("error", "✗", `Error: ${e.message}`);
    done();
  }
}

async function db_log_activity(msg) {
  try { await apiPost("activity_log_write", { message: msg, level: "warn" }); } catch(e) {}
}

function setScanStatus(type, icon, text) {
  const el = document.getElementById("scan-status");
  el.className = type;
  document.getElementById("scan-status-icon").textContent = icon;
  document.getElementById("scan-status-text").textContent = text;
}

function addRecentScan(num, name) {
  state.recentScans.unshift({ orderNum: num, customerName: name || num, time: new Date() });
  if (state.recentScans.length > 5) state.recentScans.pop();
  const wrap = document.getElementById("recent-scans-wrap");
  const list = document.getElementById("recent-scans-list");
  wrap.style.display = state.recentScans.length ? "block" : "none";
  list.innerHTML = state.recentScans.map(s => `<div class="recent-item"><span class="r-num">${esc(s.orderNum)}</span><span class="r-name">${esc(s.customerName)}</span><span class="r-time">${s.time.toLocaleTimeString()}</span></div>`).join("");
}

// ── Samples ────────────────────────────────────────────────────────────────
async function loadSamples() {
  const folder = state.samplesFolder;
  if (!folder) return;
  const data = await apiGet("samples/list", { folder });
  state.samplesFiles = data.files || [];
  state.samplesSelected = new Set();
  renderSamples();
}

function renderSamples() {
  const grid = document.getElementById("samples-grid");
  const pathEl = document.getElementById("samples-path-display");
  pathEl.textContent = state.samplesFolder || "No folder selected";
  if (!state.samplesFiles.length) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><div class="icon">🖼</div><div class="title">No images found</div><div class="sub">No supported image files in this folder.</div></div>`;
    return;
  }
  grid.innerHTML = state.samplesFiles.map(f => `
    <div class="sample-card${state.samplesSelected.has(f.filename) ? ' selected' : ''}" onclick="toggleSample('${esc(f.filename)}')">
      <img class="sample-img" src="${esc(f.url)}" alt="${esc(f.filename)}" loading="lazy">
      <div class="sample-info"><div class="sample-name" title="${esc(f.filename)}">${esc(f.filename)}</div></div>
    </div>`).join("");
  updateSampleCount();
}

function toggleSample(filename) {
  if (state.samplesSelected.has(filename)) state.samplesSelected.delete(filename);
  else state.samplesSelected.add(filename);
  renderSamples();
}

function selectAllSamples() {
  state.samplesFiles.forEach(f => state.samplesSelected.add(f.filename));
  renderSamples();
}

function clearSampleSelection() {
  state.samplesSelected = new Set();
  renderSamples();
}

function updateSampleCount() {
  const n = state.samplesSelected.size;
  document.getElementById("samples-selected-count").textContent = `${n} selected`;
  document.getElementById("btn-print-samples").disabled = n === 0;
}

async function browseSamplesFolder() {
  const result = await apiGet("browse_folder");
  if (result.ok && result.path) {
    state.samplesFolder = result.path;
    await loadSamples();
  }
}

async function printSamples() {
  if (!state.samplesSelected.size) return;
  const result = await apiPost("samples/print", { folder: state.samplesFolder, filenames: [...state.samplesSelected] });
  if (result.ok) toast(`🖨 ${result.count} sample(s) sent to printer`, "success");
  else toast(`Print failed: ${result.error}`, "error");
}

// ── Stats & Poller ─────────────────────────────────────────────────────────
async function refreshStats() {
  state.stats = await apiGet("get_stats", { gallery: state.galleryFilter });
  document.getElementById("chip-pending").textContent = `${state.stats.pending} pending`;
  document.getElementById("chip-confirmed").textContent = `${state.stats.confirmed} confirmed`;
  document.getElementById("chip-total").textContent = `${state.stats.total} total`;
}

async function updatePollerStatus() {
  state.pollerStatus = await apiGet("get_poller_status");
  const hasErr = !!state.pollerStatus.last_error;
  setApiStatus(!hasErr && state.pollerStatus.running, state.pollerStatus.last_error);
  if (state.pollerStatus.last_poll) {
    const ago = Math.round((Date.now() - new Date(state.pollerStatus.last_poll)) / 1000);
    document.getElementById("last-poll-text").textContent = ago < 5 ? "just now" : `${ago}s ago`;
  }
  document.getElementById("next-poll-text").textContent = state.pollerStatus.next_poll_in > 0 ? `${state.pollerStatus.next_poll_in}s` : "—";
}

function setApiStatus(ok, errMsg) {
  const dot = document.getElementById("api-dot");
  const txt = document.getElementById("api-status-text");
  if (ok) { dot.className = "status-dot green"; txt.textContent = "Connected"; }
  else if (errMsg) { dot.className = "status-dot red"; txt.textContent = errMsg.length > 40 ? errMsg.substring(0,40)+"…" : errMsg; }
  else { dot.className = "status-dot amber"; txt.textContent = "No credentials"; }
}

function updateBadge(id, count, urgent=false) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = count;
  el.className = `nav-badge ${count===0?"zero":urgent?"urgent":""}`;
}

async function pollNow() {
  const btn = document.getElementById("btn-poll-now");
  btn.textContent = "↻ Polling…"; btn.disabled = true;
  await apiPost("trigger_poll");
  setTimeout(async () => { await refreshAll(); btn.textContent = "↻ Poll Now"; btn.disabled = false; }, 2000);
}

async function retryDownload(orderNum, btn) {
  if (btn) { btn.textContent = "…"; btn.disabled = true; }
  const result = await apiPost("retry_download", { order_num: orderNum });
  if (result.ok) { toast(`↻ Retry started`, "info"); await refreshQueue(); }
  else { toast(`Retry failed: ${result.error}`, "error"); if (btn) { btn.textContent = "Retry"; btn.disabled = false; } }
}

// ── Settings ───────────────────────────────────────────────────────────────
async function loadSettings() {
  try {
    const cfg = await apiGet("get_settings");
    document.getElementById("s-lab-id").value = cfg.lab_id || "";
    document.getElementById("s-api-key").value = cfg.api_key || "";
    document.getElementById("s-studio-name").value = cfg.studio_name || "";
    document.getElementById("s-fulfillment-mode").value = cfg.fulfillment_mode || "pickup";
    document.getElementById("s-print-mode").value = cfg.print_mode || "auto";
    document.getElementById("s-poll-interval").value = String(cfg.poll_interval || 60);
    document.getElementById("s-unclaimed-threshold").value = String(cfg.unclaimed_threshold || 30);
    document.getElementById("s-image-folder").value = cfg.image_output_folder || "";
    document.getElementById("s-samples-folder").value = cfg.samples_folder || "";
    state.unclaimed_threshold = parseInt(cfg.unclaimed_threshold) || 30;
    if (cfg.samples_folder) state.samplesFolder = cfg.samples_folder;
    loadPrinters(cfg.printer_name || "").catch(() => {
      document.getElementById("s-printer-name").innerHTML = '<option value="">Could not detect — enter manually</option>';
      document.getElementById("printer-manual-row").style.display = "block";
    });
  } catch(e) { toast("Could not load settings", "error"); }
}

async function loadPrinters(current = "") {
  const sel = document.getElementById("s-printer-name");
  const saved = current || sel.value;
  sel.innerHTML = '<option value="">Detecting…</option>';
  let printers = [];
  try { printers = await apiGet("get_printers"); } catch(e) { printers = []; }
  if (!printers?.length) {
    sel.innerHTML = '<option value="">None detected — enter manually</option>';
    document.getElementById("printer-manual-row").style.display = "block";
    return;
  }
  sel.innerHTML = '<option value="">Select printer…</option>' + printers.map(p => `<option value="${esc(p)}" ${p===saved?"selected":""}>${esc(p)}</option>`).join("");
  document.getElementById("printer-manual-row").style.display = "none";
}

async function saveSettings() {
  const printerSel    = document.getElementById("s-printer-name").value;
  const printerManual = document.getElementById("s-printer-manual")?.value.trim() || "";
  const cfg = {
    lab_id:               document.getElementById("s-lab-id").value.trim(),
    api_key:              document.getElementById("s-api-key").value.trim(),
    studio_name:          document.getElementById("s-studio-name").value.trim(),
    fulfillment_mode:     document.getElementById("s-fulfillment-mode").value,
    poll_interval:        parseInt(document.getElementById("s-poll-interval").value),
    unclaimed_threshold:  parseInt(document.getElementById("s-unclaimed-threshold").value),
    printer_name:         printerManual || printerSel,
    print_mode:           document.getElementById("s-print-mode").value,
    image_output_folder:  document.getElementById("s-image-folder").value.trim(),
    samples_folder:       document.getElementById("s-samples-folder").value.trim(),
    logo_path: "",
  };
  const result = await apiPost("save_settings", cfg);
  if (result.ok) {
    state.unclaimed_threshold = cfg.unclaimed_threshold;
    if (cfg.samples_folder) state.samplesFolder = cfg.samples_folder;
    updateHotFolderWarning(cfg.image_output_folder);
    const msg = document.getElementById("settings-save-msg");
    msg.classList.add("show"); setTimeout(() => msg.classList.remove("show"), 2200);
    toast("Settings saved", "success");
    if (cfg.lab_id && cfg.api_key) {
      await apiPost("trigger_poll");
      showPanel("queue", document.querySelector(".nav-item"));
      // Wait briefly for the poll to run, then refresh so orders appear immediately
      setTimeout(() => refreshAll(), 2500);
    } else {
      showPanel("queue", document.querySelector(".nav-item"));
    }
  } else toast(`Save failed: ${result.error}`, "error");
}

async function testConnection() {
  const el = document.getElementById("test-connection-result");
  el.textContent = "Testing…"; el.className = "";
  const result = await apiPost("test_connection", {
    lab_id:  document.getElementById("s-lab-id").value.trim(),
    api_key: document.getElementById("s-api-key").value.trim()
  });
  el.className = result.ok ? "result-ok" : "result-err";
  el.textContent = result.ok ? `✓ ${result.message}` : `✗ ${result.message}`;
}

async function browseImageFolder() {
  const result = await apiGet("browse_folder");
  if (result.ok && result.path) document.getElementById("s-image-folder").value = result.path;
}

async function browseSamplesFolderSettings() {
  const result = await apiGet("browse_folder");
  if (result.ok && result.path) document.getElementById("s-samples-folder").value = result.path;
}

// ── Hot folder warning ─────────────────────────────────────────────────────
function updateHotFolderWarning(folder) {
  const el = document.getElementById("hot-folder-warning");
  if (!el) return;
  el.style.display = (!folder || !folder.trim()) ? "flex" : "none";
}

// ── Logo upload ────────────────────────────────────────────────────────────
async function uploadLogo(input) {
  if (!input.files || !input.files[0]) return;
  const statusEl = document.getElementById("logo-upload-status");
  statusEl.textContent = "Uploading…";
  statusEl.style.color = "var(--text3)";
  const formData = new FormData();
  formData.append("file", input.files[0]);
  try {
    const r = await fetch("/api/upload_logo", { method: "POST", body: formData });
    const result = await r.json();
    if (result.ok) {
      statusEl.textContent = "✓ Logo saved";
      statusEl.style.color = "var(--green)";
      loadLogoPreview();
      toast("Logo uploaded — will appear on receipts", "success");
    } else {
      statusEl.textContent = `✗ ${result.error}`;
      statusEl.style.color = "var(--red)";
    }
  } catch(e) {
    statusEl.textContent = "✗ Upload failed";
    statusEl.style.color = "var(--red)";
  }
  input.value = "";
}

function loadLogoPreview() {
  const wrap = document.getElementById("logo-preview-wrap");
  const img  = document.getElementById("logo-preview");
  if (!wrap || !img) return;
  img.src = `/api/get_logo?t=${Date.now()}`;
  img.onload = () => { wrap.style.display = "block"; };
  img.onerror = () => { wrap.style.display = "none"; };
}

// ── Galleries ──────────────────────────────────────────────────────────────
async function refreshGalleries() {
  const galleries = await apiGet("get_galleries");
  const sel = document.getElementById("gallery-filter");
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '<option value="">All Jobs</option>' + galleries.map(g => `<option value="${esc(g)}" ${g===current?"selected":""}>${esc(g)}</option>`).join("");
}

// ── Activity Log ───────────────────────────────────────────────────────────
async function loadActivityLog() {
  const lines = await apiGet("activity_log", { limit: 60 });
  const el = document.getElementById("log-lines");
  el.innerHTML = "";
  lines.forEach(l => appendLogLine(l.message, l.level, l.ts, false));
  el.scrollTop = el.scrollHeight;
}

function appendLogLine(message, level="info", ts=null, scroll=true) {
  const el = document.getElementById("log-lines");
  const time = ts ? new Date(ts).toLocaleTimeString() : new Date().toLocaleTimeString();
  const div = document.createElement("div");
  div.className = "log-line";
  div.innerHTML = `<span class="log-time">${time}</span><span class="log-msg ${level==="error"?"error":level==="warn"?"warn":""}">${esc(message)}</span>`;
  el.appendChild(div);
  while (el.children.length > 100) el.removeChild(el.firstChild);
  if (scroll) { const log = document.getElementById("activity-log"); log.scrollTop = log.scrollHeight; }
}

function toggleLog() {
  state.logVisible = !state.logVisible;
  const log = document.getElementById("activity-log");
  const btn = document.getElementById("log-toggle");
  log.style.height = state.logVisible ? "var(--log-h)" : "32px";
  log.style.overflow = state.logVisible ? "auto" : "hidden";
  btn.textContent = state.logVisible ? "Hide" : "Show";
}

// ── Refresh All ────────────────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([refreshQueue(), refreshHistory(), refreshStats(), refreshGalleries(), refreshJobs()]);
  renderQueue();
}

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(message, type="info") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const icons = { success:"✓", error:"✗", info:"ℹ" };
  el.innerHTML = `<span>${icons[type]||"ℹ"}</span><span>${esc(message)}</span>`;
  document.getElementById("toast-container").appendChild(el);
  setTimeout(() => { el.style.animation = "toastOut .2s ease forwards"; setTimeout(() => el.remove(), 200); }, 3200);
}

// ── Helpers ────────────────────────────────────────────────────────────────
function esc(str) {
  if (str==null) return "";
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function parseItems(json) {
  try { return JSON.parse(json||"[]"); } catch { return []; }
}
function formatTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const h = d.getHours()%12||12;
    const m = String(d.getMinutes()).padStart(2,"0");
    return `${h}:${m} ${d.getHours()>=12?"pm":"am"}`;
  } catch { return "—"; }
}
function formatAge(min) {
  if (min < 1) return "just now";
  if (min < 60) return `${Math.floor(min)}m`;
  const h=Math.floor(min/60), m=Math.floor(min%60);
  return m>0?`${h}h ${m}m`:`${h}h`;
}

// ── Intervals ──────────────────────────────────────────────────────────────
setInterval(() => {
  renderQueue();
  updatePollerStatus();
}, 30000);

setInterval(() => {
  if (state.pollerStatus.next_poll_in > 0) {
    state.pollerStatus.next_poll_in = Math.max(0, state.pollerStatus.next_poll_in - 1);
    document.getElementById("next-poll-text").textContent = state.pollerStatus.next_poll_in > 0 ? `${state.pollerStatus.next_poll_in}s` : "—";
  }
}, 1000);

// ── OTA Updates ────────────────────────────────────────────────────────────
async function manualCheckUpdate() {
  const btn = document.getElementById("btn-check-update");
  const result_el = document.getElementById("settings-update-result");
  btn.disabled = true; btn.textContent = "Checking…";
  result_el.textContent = ""; result_el.style.color = "";
  try {
    const result = await apiGet("check_update");
    if (result.update_available) {
      result_el.textContent = `v${result.latest} available!`;
      result_el.style.color = "var(--green)";
      showUpdateAvailable(result);
    } else {
      result_el.textContent = `Up to date (v${result.current})`;
      result_el.style.color = "var(--text3)";
    }
  } catch(e) {
    result_el.textContent = "Check failed";
    result_el.style.color = "var(--red)";
  }
  btn.disabled = false; btn.textContent = "Check for Updates";
}

function showUpdateAvailable(info) {
  state.updateInfo = info;
  const btn = document.getElementById('update-btn');
  btn.classList.add('visible');
  btn.textContent = `🔄 v${info.latest} Available`;
  toast(`Update available: v${info.latest} — click to install`, 'info');
}

async function triggerUpdate() {
  if (!state.updateInfo) return;
  const confirmed = confirm(`Update to v${state.updateInfo.latest}?\n\n${state.updateInfo.release_notes}\n\nThe app will restart automatically.`);
  if (!confirmed) return;
  document.getElementById('update-btn').textContent = '⏳ Updating…';
  document.getElementById('update-btn').onclick = null;
  const result = await apiPost('install_update', { download_url: state.updateInfo.download_url });
  if (!result.ok) {
    toast(`Update failed: ${result.error}`, 'error');
    document.getElementById('update-btn').textContent = `🔄 v${state.updateInfo.latest} Available`;
    document.getElementById('update-btn').onclick = triggerUpdate;
  }
}

function handleUpdateProgress(data) {
  const bar = document.getElementById('update-progress-bar');
  bar.style.display = 'block';
  bar.style.width = data.pct + '%';
  document.getElementById('update-btn').textContent = `⏳ ${data.message}`;
  if (data.pct >= 100) setTimeout(hideUpdateProgress, 1500);
  appendLogLine(`🔄 ${data.message}`, 'info');
}

function hideUpdateProgress() {
  document.getElementById('update-progress-bar').style.display = 'none';
}

// ── Init ───────────────────────────────────────────────────────────────────
async function init() {
  restoreJobFilter();
  try { initSSE(); } catch(e) { console.warn(e); }
  try { await refreshAll(); } catch(e) { console.warn(e); }
  try { await updatePollerStatus(); } catch(e) { console.warn(e); }
  try { await loadActivityLog(); } catch(e) { console.warn(e); }

  // On first launch (no credentials), drop straight into Settings
  // Also check hot folder and show warning banner if missing
  try {
    const cfg = await apiGet("get_settings");
    if (!cfg.lab_id || !cfg.api_key) {
      showPanel("settings", document.querySelector(".nav-item[onclick*=\"'settings'\"]"));
    }
    updateHotFolderWarning(cfg.image_output_folder);
    if (cfg.logo_path) loadLogoPreview();
  } catch(e) {}
  try {
    const v = await apiGet("get_version");
    document.title = `PDX Onsite v${v.version} — Pickup Station`;
    document.querySelector(".status-brand").textContent = `PDX ONSITE v${v.version}`;
    const vdisplay = document.getElementById("settings-version-display");
    if (vdisplay) vdisplay.textContent = `Version: v${v.version}`;
    let _updatePollCount = 0;
    const _pollForUpdate = async () => {
      try {
        const upd = await apiGet("get_pending_update");
        if (upd.update_available) { showUpdateAvailable(upd); return; }
      } catch(e) {}
      _updatePollCount++;
      if (_updatePollCount < 6) setTimeout(_pollForUpdate, 3000);
    };
    setTimeout(_pollForUpdate, 2000);
  } catch(e) {}
}

document.addEventListener("DOMContentLoaded", () => init().catch(console.error));
