(() => {
  const get = id => document.getElementById(id);
  const state = { records: [], lines: [], bytes: 0, cursor: 0, events: null,
    logs: null, eventsPaused: false, live: false, eventTimer: null, logTimer: null,
    since: 0, seen: new Set(), generation: 0, project: null, instance: null, logCap: Number(document.querySelector(".ops-layout").dataset.logCap) };
  const encoder = new TextEncoder();

  function renderHistory() {
    const source = get("source-filter").value;
    const records = state.records.filter(item => !source || item.source === source);
    get("timeline").replaceChildren(...records.slice().reverse().map(item => {
      const row = document.createElement("li");
      row.className = item.source === "alert" ? "event-alert" : "";
      const meta = document.createElement("div"); meta.className = "event-meta";
      const origin = document.createElement("span"); origin.className = "event-source";
      origin.textContent = { docker: "Docker", overseer: "Overseer", alert: "Alert" }[item.source] || item.source;
      const time = document.createElement("time"); time.dateTime = item.timestamp;
      time.textContent = new Date(item.timestamp).toLocaleTimeString();
      meta.append(origin, time);
      const text = document.createElement("div"); text.className = "event-message";
      const service = document.createElement("span"); service.className = "event-service";
      service.textContent = item.service;
      text.append(service, document.createTextNode(item.message || item.kind));
      row.append(meta, text); return row;
    }));
    if (!records.length) {
      const row = document.createElement("li"); row.className = "ops-empty";
      row.textContent = "No recent events for this selection."; get("timeline").append(row);
    }
  }

  function addRecord(item) {
    if (item.id <= state.cursor) return;
    state.cursor = item.id; state.records.push(item);
    state.records = state.records.slice(-200); renderHistory();
  }

  function connectEvents() {
    if (state.eventsPaused) return;
    state.events?.close();
    const source = state.events = new EventSource(`/api/events?after=${state.cursor}`);
    source.addEventListener("status", event => {
      const data = JSON.parse(event.data);
      if (state.instance && state.instance !== data.instance) {
        state.cursor = 0; state.records = []; renderHistory();
        state.instance = data.instance; source.close(); connectEvents(); return;
      }
      state.instance = data.instance;
      state.project = data.project;
      get("operations-status").textContent = `${data.project} • ${data.status}`;
    });
    source.addEventListener("record", event => addRecord(JSON.parse(event.data)));
    const reconnect = () => {
      source.close(); clearTimeout(state.eventTimer);
      if (!state.eventsPaused) state.eventTimer = setTimeout(connectEvents, 1500);
    };
    source.addEventListener("end", reconnect);
    source.onerror = () => { get("operations-status").textContent = "Event connection unavailable • retrying"; reconnect(); };
  }

  function renderLogs() {
    const query = get("log-search").value.toLowerCase();
    const lines = state.lines.filter(line => !query || line.toLowerCase().includes(query));
    const showTimestamps = get("log-timestamps").checked;
    get("log-output").textContent = lines.map(line => showTimestamps ? line : line.replace(/^\d{4}-\d{2}-\d{2}T\S+\s/, "")).join("\n") || "No matching log lines.";
  }

  function addLog(text) {
    // Timestamp + content suppresses inclusive-since replay after reconnects.
    if (state.seen.has(text)) return;
    state.seen.add(text);
    if (state.seen.size > 1000) state.seen.delete(state.seen.values().next().value);
    const timestamp = Date.parse(text.split(" ", 1)[0]);
    if (Number.isFinite(timestamp)) state.since = Math.max(state.since, Math.floor(timestamp / 1000));
    state.lines.push(text); state.bytes += encoder.encode(text).length + 1;
    while (state.bytes > state.logCap && state.lines.length) {
      state.bytes -= encoder.encode(state.lines.shift()).length + 1;
    }
    renderLogs();
  }

  function logsUrl(extra = {}) {
    const id = get("log-container").value;
    const params = new URLSearchParams({ tail: get("log-tail").value, timestamps: "1", ...extra });
    return `/api/service/${encodeURIComponent(id)}/logs?${params}`;
  }

  function pauseLogs() {
    state.logs?.close(); clearTimeout(state.logTimer); state.live = false;
    get("logs-live").textContent = "Go live"; get("logs-live").setAttribute("aria-pressed", "false");
  }

  function connectLogs() {
    if (!state.live) return;
    state.logs?.close();
    const generation = state.generation;
    const source = state.logs = new EventSource(logsUrl({ follow: "1", tail: "0", since: String(state.since) }));
    source.addEventListener("log", event => {
      if (generation !== state.generation) return;
      const line = JSON.parse(event.data); addLog(line.text);
      get("log-status").textContent = line.truncated ? "Live • a long line was truncated" : "Live • bounded output";
    });
    const reconnect = () => {
      source.close(); clearTimeout(state.logTimer);
      if (state.live && generation === state.generation) state.logTimer = setTimeout(connectLogs, 1500);
    };
    source.addEventListener("end", reconnect);
    source.addEventListener("unavailable", reconnect);
    source.onerror = () => { get("log-status").textContent = "Log connection unavailable • retrying"; reconnect(); };
  }

  async function loadLogs() {
    pauseLogs(); const generation = ++state.generation;
    state.lines = []; state.bytes = 0; state.since = 0; state.seen.clear();
    const selected = Boolean(get("log-container").value);
    get("logs-live").disabled = !selected; get("logs-download").disabled = !selected;
    if (!selected) { get("log-output").textContent = "Select a container to inspect recent logs."; return; }
    get("log-status").textContent = "Loading recent logs…";
    try {
      const response = await fetch(logsUrl());
      if (!response.ok) { const data = await response.json(); throw new Error(data.error || "Logs unavailable"); }
      const text = await response.text();
      if (generation !== state.generation) return;
      text.split("\n").filter(Boolean).forEach(addLog); renderLogs();
      get("log-status").textContent = "Recent logs loaded • output is bounded";
    } catch (error) { if (generation === state.generation) window.Overseer.showError(error.message, loadLogs); }
  }

  async function initialize() {
    try {
      const history = await window.Overseer.requestJson("/api/history");
      history.records.forEach(addRecord); state.project = history.project; state.instance = history.instance;
      const services = await window.Overseer.requestJson("/api/services");
      get("log-container").replaceChildren();
      const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Choose a container";
      get("log-container").append(placeholder);
      services.forEach(service => {
        const option = document.createElement("option"); option.value = service.id; option.textContent = service.name;
        get("log-container").append(option);
      });
      window.Overseer.clearMessage(); connectEvents();
    } catch (error) { get("operations-status").textContent = "Operational data unavailable"; window.Overseer.showError(error.message, initialize); }
  }
  get("source-filter").addEventListener("change", renderHistory);
  get("events-pause").addEventListener("click", () => {
    state.eventsPaused = !state.eventsPaused; state.events?.close(); clearTimeout(state.eventTimer);
    get("events-pause").textContent = state.eventsPaused ? "Resume" : "Pause";
    get("events-pause").setAttribute("aria-pressed", String(state.eventsPaused));
    if (!state.eventsPaused) connectEvents();
  });
  get("log-container").addEventListener("change", loadLogs);
  get("log-tail").addEventListener("change", loadLogs);
  get("log-search").addEventListener("input", renderLogs);
  get("log-timestamps").addEventListener("change", renderLogs);
  get("logs-live").addEventListener("click", () => {
    if (state.live) { pauseLogs(); get("log-status").textContent = "Paused"; }
    else { if (!state.since) state.since = Math.floor(Date.now() / 1000); state.live = true; get("logs-live").textContent = "Pause"; get("logs-live").setAttribute("aria-pressed", "true"); connectLogs(); }
  });
  get("logs-download").addEventListener("click", () => { window.location.href = logsUrl({ download: "1" }); });
  window.addEventListener("pagehide", () => { state.eventsPaused = true; state.events?.close(); clearTimeout(state.eventTimer); pauseLogs(); });
  initialize();
})();
