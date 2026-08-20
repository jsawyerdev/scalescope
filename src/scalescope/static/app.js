const POLL_INTERVAL_MS = 3000;
const DEFAULT_MODEL = "auto_ets";

// The multi-model forecast overlay is not urgent evidence like observations
// or the selected model's band, so it refetches on a slower cadence to avoid
// firing 6 forecast requests every 3s poll.
const MULTI_MODEL_POLL_MS = 12000;

// Freshness thresholds, in seconds since the latest observation's ts.
const FRESH_MAX_S = 6;
const AGING_MAX_S = 20;

// Raw log panel: cap rows kept in the DOM.
const LOG_MAX_ROWS = 40;

// Muted blue-grey shades for the non-selected model overlay lines, in the
// same family as --accent. Kept out of style.css since Chart.js needs raw
// hex strings rather than CSS custom properties.
const MODEL_COLORS = {
  naive: "#9aa7ba",
  seasonal_naive: "#6f83a0",
  ewma: "#4a6690",
  linear_trend: "#8493ab",
  auto_ets: "#1a3e7c",
  lightgbm_quantile: "#334a6b",
};
const FALLBACK_MODEL_COLOR = "#7d8fa6";

let currentWorkload = null;
let selectedModel = DEFAULT_MODEL;
let chart = null;
let pollTimer = null;
let allModelForecasts = {};
let lastMultiModelFetchAt = 0;

const workloadSelect = document.getElementById("workload-select");
const fetchError = document.getElementById("fetch-error");

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url} -> ${response.status}`);
  }
  return response.json();
}

// Observation timestamps are UTC but serialized without an offset; append
// "Z" so the browser parses them as UTC instead of local time.
function parseTs(ts) {
  return new Date(ts.endsWith("Z") ? ts : `${ts}Z`);
}

function formatDiagnosis(diagnosis) {
  return diagnosis.replace(/_/g, " ");
}

function severityClass(diagnosis, scalingWillHelp) {
  if (!scalingWillHelp) return "bad";
  if (diagnosis !== "healthy") return "warn";
  return "good";
}

function renderEvidence(recommendations) {
  const badge = document.getElementById("diagnosis-badge");
  const text = document.getElementById("diagnosis-text");
  const cls = severityClass(recommendations.diagnosis, recommendations.scaling_will_help);
  badge.textContent = formatDiagnosis(recommendations.diagnosis);
  badge.className = `badge ${cls}`;
  text.textContent = recommendations.explanation;
  document.getElementById("source-text").textContent = recommendations.workload;
}

// Shared freshness classification: given an age in seconds, return the dot
// color class and a human label. Used both for observation freshness and
// for the source panel's last-successful-collection age.
function classifyFreshness(ageS) {
  if (ageS <= FRESH_MAX_S) return { cls: "good", label: `fresh – ${ageS.toFixed(1)}s ago` };
  if (ageS <= AGING_MAX_S) return { cls: "warn", label: `aging – ${Math.round(ageS)}s ago` };
  return { cls: "bad", label: `stale – ${Math.round(ageS)}s ago` };
}

function renderFreshness(latestTs) {
  const dot = document.getElementById("freshness-dot");
  const text = document.getElementById("freshness-text");
  if (!latestTs) {
    dot.className = "dot";
    text.textContent = "no data yet";
    return;
  }
  const ageS = Math.max(0, (Date.now() - parseTs(latestTs).getTime()) / 1000);
  const { cls, label } = classifyFreshness(ageS);
  dot.className = `dot ${cls}`;
  text.textContent = label;
}

// Source/identity panel: what ScaleScope is actually connected to (demo
// simulator vs a real Kubernetes cluster), plus collection health.
function renderSource(source) {
  const modeBadge = document.getElementById("mode-badge");
  const targetText = document.getElementById("source-target-text");
  const connDot = document.getElementById("source-conn-dot");
  const connText = document.getElementById("source-conn-text");
  const errorBox = document.getElementById("source-error");

  const isObserve = source.mode === "observe";
  modeBadge.textContent = isObserve ? "OBSERVE" : "DEMO";
  modeBadge.className = isObserve ? "badge mode" : "badge mode demo";

  targetText.textContent = isObserve
    ? `${source.cluster_server}\n${source.k8s_namespace} / ${source.k8s_deployment}`
    : "synthetic data — no cluster connection";

  if (source.connected) {
    if (source.last_success_ts) {
      const ageS = Math.max(0, (Date.now() - parseTs(source.last_success_ts).getTime()) / 1000);
      const { cls, label } = classifyFreshness(ageS);
      connDot.className = `dot ${cls}`;
      connText.textContent = `connected – last success ${label.split("– ")[1]}`;
    } else {
      connDot.className = "dot good";
      connText.textContent = "connected";
    }
    errorBox.hidden = true;
  } else {
    connDot.className = "dot bad";
    connText.textContent = "disconnected";
    if (source.last_error) {
      errorBox.hidden = false;
      errorBox.textContent = `last error: ${source.last_error}`;
    } else {
      errorBox.hidden = true;
    }
  }
}

function renderMetrics(latest) {
  if (!latest) return;
  document.getElementById("stat-replicas").textContent = latest.replicas;
  document.getElementById("stat-request-rate").textContent = latest.request_rate.toFixed(0);
  document.getElementById("stat-cpu").textContent = `${latest.cpu_usage_pct.toFixed(1)}%`;
  document.getElementById("stat-cpu-throttled").textContent = `${latest.cpu_throttled_pct.toFixed(1)}%`;
  document.getElementById("stat-memory").textContent = `${latest.memory_usage_mb.toFixed(0)} MB`;
  document.getElementById("stat-latency").textContent = `${latest.latency_p95_ms.toFixed(0)} ms`;
  document.getElementById("stat-error-rate").textContent = `${(latest.error_rate * 100).toFixed(2)}%`;
  document.getElementById("stat-pending").textContent = latest.pending_pods;
  document.getElementById("stat-restarts").textContent = latest.restarts;
}

function deltaCell(recommended, current) {
  const delta = recommended - current;
  const cls = delta > 0 ? "positive" : delta < 0 ? "negative" : "zero";
  const sign = delta > 0 ? "+" : "";
  return `<span class="delta ${cls}">${sign}${delta}</span>`;
}

function confidenceCell(confidence) {
  const pct = Math.round(confidence * 100);
  return `
    <div class="confidence-cell">
      <div class="confidence-bar"><div class="confidence-bar-fill" style="width:${pct}%"></div></div>
      <span>${pct}%</span>
    </div>`;
}

function renderTable(models) {
  const body = document.getElementById("model-table-body");
  body.innerHTML = models
    .map((m) => {
      const selected = m.model === selectedModel ? "selected" : "";
      return `
        <tr class="${selected}" data-model="${m.model}">
          <td class="model-name">${m.model}</td>
          <td class="num">${m.recommended_replicas}</td>
          <td class="num">${deltaCell(m.recommended_replicas, m.current_replicas)}</td>
          <td>${confidenceCell(m.confidence)}</td>
          <td class="num">${m.peak_forecast_p90.toFixed(0)}</td>
          <td class="num">${(m.projected_utilization * 100).toFixed(0)}%</td>
        </tr>`;
    })
    .join("");
  body.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => {
      selectedModel = row.dataset.model;
      refresh();
    });
  });
}

function renderLog(observations) {
  const body = document.getElementById("log-body");
  const rows = observations.slice(-LOG_MAX_ROWS).reverse();
  body.innerHTML = rows
    .map((o) => {
      const ts = parseTs(o.ts).toLocaleTimeString();
      return `
        <tr>
          <td>${ts}</td>
          <td class="num">${o.replicas}</td>
          <td class="num">${o.request_rate.toFixed(1)}</td>
          <td class="num">${o.cpu_usage_pct.toFixed(1)}%</td>
          <td class="num">${o.cpu_throttled_pct.toFixed(1)}%</td>
          <td class="num">${o.latency_p95_ms.toFixed(0)}</td>
          <td class="num">${(o.error_rate * 100).toFixed(2)}%</td>
          <td class="num">${o.pending_pods}</td>
          <td class="num">${o.restarts}</td>
        </tr>`;
    })
    .join("");
}

function updateChart(observations, forecast, allForecasts, modelNames) {
  document.getElementById("chart-model-name").textContent = forecast.model;

  const historyLabels = observations.map((o) => new Date(o.ts.endsWith("Z") ? o.ts : `${o.ts}Z`).toLocaleTimeString());
  const historyValues = observations.map((o) => o.request_rate);
  const lastIndex = historyValues.length - 1;
  const forecastLabels = forecast.p50.map((_, i) => `+${i + 1}`);
  const labels = [...historyLabels, ...forecastLabels];

  // Anchor forecast series at the last observed point so lines connect visually.
  const anchorLast = (arr) => {
    const out = new Array(Math.max(historyValues.length - 1, 0)).fill(null);
    out.push(historyValues[lastIndex]);
    return out.concat(arr);
  };

  const datasets = [
    {
      label: "request rate (observed)",
      data: [...historyValues, ...new Array(forecast.p50.length).fill(null)],
      borderColor: "#1a3e7c",
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    },
    {
      label: `${forecast.model} p50 (selected)`,
      data: anchorLast(forecast.p50),
      borderColor: "#5b8fc2",
      backgroundColor: "transparent",
      borderDash: [4, 4],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    },
    {
      label: "forecast p90",
      data: anchorLast(forecast.p90),
      borderColor: "transparent",
      backgroundColor: "rgba(26, 62, 124, 0.1)",
      pointRadius: 0,
      fill: "+1",
    },
    {
      label: "forecast p10",
      data: anchorLast(forecast.p10),
      borderColor: "transparent",
      backgroundColor: "rgba(26, 62, 124, 0.1)",
      pointRadius: 0,
      fill: false,
    },
  ];

  // Overlay every other registered model's p50 line, thin and unshaded, so
  // trajectories can be compared at a glance. The selected model keeps its
  // full p10-p90 band above; these are the "at a glance" comparison lines.
  for (const name of modelNames || []) {
    if (name === selectedModel) continue;
    const other = allForecasts && allForecasts[name];
    if (!other) continue;
    datasets.push({
      label: `${name} p50`,
      data: anchorLast(other.p50),
      borderColor: MODEL_COLORS[name] || FALLBACK_MODEL_COLOR,
      backgroundColor: "transparent",
      borderWidth: 1,
      pointRadius: 0,
      tension: 0.1,
    });
  }

  if (!chart) {
    const ctx = document.getElementById("request-rate-chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { color: "#52627a", maxTicksLimit: 12 }, grid: { color: "#ccd5e0" } },
          y: { ticks: { color: "#52627a" }, grid: { color: "#ccd5e0" }, beginAtZero: true },
        },
        plugins: {
          legend: { labels: { color: "#16202e" } },
        },
      },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.update("none");
  }
}

async function refresh() {
  if (!currentWorkload) return;
  try {
    const [observations, recommendations, forecast, source] = await Promise.all([
      fetchJson(`/api/workloads/${currentWorkload}/observations?limit=200`),
      fetchJson(`/api/workloads/${currentWorkload}/recommendations`),
      fetchJson(`/api/workloads/${currentWorkload}/forecast?model=${selectedModel}`),
      fetchJson("/api/source"),
    ]);
    if (!recommendations.models.some((m) => m.model === selectedModel)) {
      selectedModel = recommendations.models[0].model;
    }
    const modelNames = recommendations.models.map((m) => m.model);
    allModelForecasts[selectedModel] = forecast;

    // Refetch the other models' forecasts on a slower cadence than the main
    // poll (see MULTI_MODEL_POLL_MS) rather than on every 3s tick.
    if (Date.now() - lastMultiModelFetchAt >= MULTI_MODEL_POLL_MS) {
      const others = modelNames.filter((m) => m !== selectedModel);
      const otherForecasts = await Promise.all(
        others.map((m) => fetchJson(`/api/workloads/${currentWorkload}/forecast?model=${m}`))
      );
      others.forEach((m, i) => {
        allModelForecasts[m] = otherForecasts[i];
      });
      lastMultiModelFetchAt = Date.now();
    }
    for (const name of Object.keys(allModelForecasts)) {
      if (!modelNames.includes(name)) delete allModelForecasts[name];
    }

    const latest = observations[observations.length - 1];
    renderSource(source);
    renderEvidence(recommendations);
    renderFreshness(latest ? latest.ts : null);
    renderMetrics(latest);
    renderTable(recommendations.models);
    renderLog(observations);
    updateChart(observations, forecast, allModelForecasts, modelNames);
    fetchError.hidden = true;
  } catch (err) {
    fetchError.hidden = false;
    fetchError.textContent = `connection error: ${err.message} – showing last known data`;
  }
}

async function loadWorkloads() {
  const workloads = await fetchJson("/api/workloads");
  if (workloads.length === 0) {
    fetchError.hidden = false;
    fetchError.textContent = "waiting for first observations…";
    setTimeout(loadWorkloads, POLL_INTERVAL_MS);
    return;
  }
  workloadSelect.innerHTML = workloads.map((w) => `<option value="${w}">${w}</option>`).join("");
  currentWorkload = workloads[0];
  workloadSelect.value = currentWorkload;
  workloadSelect.addEventListener("change", () => {
    currentWorkload = workloadSelect.value;
    selectedModel = DEFAULT_MODEL;
    allModelForecasts = {};
    lastMultiModelFetchAt = 0;
    refresh();
  });
  await refresh();
  if (!pollTimer) {
    pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
  }
}

loadWorkloads();
