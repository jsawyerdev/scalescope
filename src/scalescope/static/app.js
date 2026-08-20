const POLL_INTERVAL_MS = 3000;
const DEFAULT_MODEL = "auto_ets";

// Freshness thresholds, in seconds since the latest observation's ts.
const FRESH_MAX_S = 6;
const AGING_MAX_S = 20;

let currentWorkload = null;
let selectedModel = DEFAULT_MODEL;
let chart = null;
let pollTimer = null;

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
  document.getElementById("source-text").textContent =
    `${recommendations.workload} – simulator (demo mode)`;
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
  let cls = "bad";
  let label = `stale – ${Math.round(ageS)}s ago`;
  if (ageS <= FRESH_MAX_S) {
    cls = "good";
    label = `fresh – ${ageS.toFixed(1)}s ago`;
  } else if (ageS <= AGING_MAX_S) {
    cls = "warn";
    label = `aging – ${Math.round(ageS)}s ago`;
  }
  dot.className = `dot ${cls}`;
  text.textContent = label;
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

function updateChart(observations, forecast) {
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
      borderColor: "#1c5490",
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    },
    {
      label: "forecast p50",
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
      backgroundColor: "rgba(28, 84, 144, 0.1)",
      pointRadius: 0,
      fill: "+1",
    },
    {
      label: "forecast p10",
      data: anchorLast(forecast.p10),
      borderColor: "transparent",
      backgroundColor: "rgba(28, 84, 144, 0.1)",
      pointRadius: 0,
      fill: false,
    },
  ];

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
    const [observations, recommendations, forecast] = await Promise.all([
      fetchJson(`/api/workloads/${currentWorkload}/observations?limit=200`),
      fetchJson(`/api/workloads/${currentWorkload}/recommendations`),
      fetchJson(`/api/workloads/${currentWorkload}/forecast?model=${selectedModel}`),
    ]);
    if (!recommendations.models.some((m) => m.model === selectedModel)) {
      selectedModel = recommendations.models[0].model;
    }
    const latest = observations[observations.length - 1];
    renderEvidence(recommendations);
    renderFreshness(latest ? latest.ts : null);
    renderMetrics(latest);
    renderTable(recommendations.models);
    updateChart(observations, forecast);
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
    refresh();
  });
  await refresh();
  if (!pollTimer) {
    pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
  }
}

loadWorkloads();
