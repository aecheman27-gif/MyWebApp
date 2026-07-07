// Subscribes to /printer/stream and updates each printer card's fields in
// place. No frameworks — just EventSource + DOM lookups by data-attributes.

(function () {
  const STREAM_URL = "/printer/stream";
  const reconnectDelayMs = 3000;

  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function timeOfDay(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function fmt(n, suffix) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    return Math.round(n) + (suffix || "");
  }

  function applyUpdate(payload) {
    const card = document.querySelector(
      '.printer-card[data-printer-slug="' + payload.slug + '"]'
    );
    if (!card) return;

    function set(field, value) {
      const el = card.querySelector('[data-field="' + field + '"]');
      if (el && el.textContent !== value) el.textContent = value;
    }

    // Status pill (also swap class for color).
    const status = payload.status || "OFFLINE";
    const statusEl = card.querySelector('[data-field="status"]');
    if (statusEl) {
      statusEl.textContent = status;
      statusEl.className = "printer-status status-pill status-" + status;
    }

    // Progress bar
    const pct = (payload.percent === null || payload.percent === undefined)
      ? 0 : payload.percent;
    const fill = card.querySelector('[data-field="percent-fill"]');
    if (fill) fill.style.width = pct.toFixed(1) + "%";
    set("percent", fmt(payload.percent, "%"));

    set("remaining",
      payload.remaining_minutes !== null && payload.remaining_minutes !== undefined
        ? payload.remaining_minutes + " min left"
        : "—"
    );
    set("current_file", payload.current_file || "—");

    set("layer-summary",
      (payload.layer !== null && payload.layer !== undefined && payload.total_layers)
        ? payload.layer + " / " + payload.total_layers
        : "—"
    );
    set("nozzle-summary",
      (payload.nozzle_temp !== null && payload.nozzle_temp !== undefined)
        ? Math.round(payload.nozzle_temp) + "° → " + Math.round(payload.nozzle_target || 0) + "°"
        : "—"
    );
    set("bed-summary",
      (payload.bed_temp !== null && payload.bed_temp !== undefined)
        ? Math.round(payload.bed_temp) + "° → " + Math.round(payload.bed_target || 0) + "°"
        : "—"
    );
    set("wifi_signal",
      (payload.wifi_signal !== null && payload.wifi_signal !== undefined)
        ? payload.wifi_signal + " dBm"
        : "—"
    );
    set("last_seen_at", timeOfDay(payload.last_seen_at));
  }

  let es;
  function connect() {
    es = new EventSource(STREAM_URL);
    es.onmessage = function (ev) {
      try {
        const payload = JSON.parse(ev.data);
        applyUpdate(payload);
      } catch (e) {
        console.warn("Telemetry parse error", e);
      }
    };
    es.onerror = function () {
      es.close();
      setTimeout(connect, reconnectDelayMs);
    };
  }

  if (typeof EventSource !== "undefined") {
    connect();
  }
})();
