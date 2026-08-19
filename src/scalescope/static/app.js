const POLL_INTERVAL_MS = 3000;

let currentWorkload = null;
let chart = null;

const modelSelect = document.getElementById("model-select");
const banner = document.getElementById("diagnosis-banner");

function selectedModel() {
  return modelSelect.value;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url} -> ${response.status}`);
  }
  return response.json();
}

function updateStats(recommendation, latestObservation) {
  document.getElementById("stat-current-replicas").textContent = recommendation.current_replicas;
  document.getElementById("stat-recommended-replicas").textContent = recommendation.recommended_replicas;
  document.getElementById("stat-confidence").textContent = `${Math.round(recommendation.confidence * 100)}%`;
  if (latestObservation) {
    document.getElementById("stat-cpu").textContent = `${latestObservation.cpu_usage_pct.toFixed(1)}%`;
    document.getElementById("stat-memory").textContent = `${latestObservation.memory_usage_mb.toFixed(0)} MB`;
    document.getElementById("stat-latency").textContent = `${latestObservation.latency_p95_ms.toFixed(0)} ms`;
  }
}

function updateBanner(recommendation) {
  banner.textContent = recommendation.explanation;
  banner.classList.remove("healthy", "caution", "blocked");
  if (!recommendation.scaling_will_help) {
    banner.classList.add("blocked");
  } else if (recommendation.diagnosis !== "healthy") {
    banner.classList.add("caution");
  } else {
    banner.classList.add("healthy");
  }
}

function updateChart(observations, forecast) {
  const historyLabels = observations.map((o) => new Date(o.ts).toLocaleTimeString());
  const historyValues = observations.map((o) => o.request_rate);

  const lastIndex = historyLabels.length - 1;
  const forecastLabels = forecast.p50.map((_, i) => `+${i + 1}`);
  const labels = [...historyLabels, ...forecastLabels];

  const pad = (arr) => new Array(historyValues.length).fill(null).concat(arr);
  // anchor forecast series at the last observed point so lines connect visually
  const anchorLast = (arr) => {
    const out = new Array(historyValues.length - 1).fill(null);
    out.push(historyValues[lastIndex]);
    return out.concat(arr);
  };

  const datasets = [
    {
      label: "request rate (observed)",
      data: [...historyValues, ...new Array(forecast.p50.length).fill(null)],
      borderColor: "#4fd1c5",
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    },
    {
      label: "forecast p50",
      data: anchorLast(forecast.p50),
      borderColor: "#8ab4f8",
      backgroundColor: "transparent",
      borderDash: [4, 4],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.15,
    },
    {
      label: "forecast p90",
      data: anchorLast(forecast.p90),
      borderColor: "transparent",
      backgroundColor: "rgba(138, 180, 248, 0.15)",
      pointRadius: 0,
      fill: "+1",
    },
    {
      label: "forecast p10",
      data: anchorLast(forecast.p10),
      borderColor: "transparent",
      backgroundColor: "rgba(138, 180, 248, 0.15)",
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
          x: { ticks: { color: "#7a8299", maxTicksLimit: 12 }, grid: { color: "#262e40" } },
          y: { ticks: { color: "#7a8299" }, grid: { color: "#262e40" }, beginAtZero: true },
        },
        plugins: {
          legend: { labels: { color: "#d8dee9" } },
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
  const model = selectedModel();
  try {
    const [observations, forecast, recommendation] = await Promise.all([
      fetchJson(`/api/workloads/${currentWorkload}/observations?limit=200`),
      fetchJson(`/api/workloads/${currentWorkload}/forecast?model=${model}`),
      fetchJson(`/api/workloads/${currentWorkload}/recommendation?model=${model}`),
    ]);
    updateChart(observations, forecast);
    updateStats(recommendation, observations[observations.length - 1]);
    updateBanner(recommendation);
  } catch (err) {
    banner.textContent = `error: ${err.message}`;
    banner.classList.remove("healthy", "caution");
    banner.classList.add("blocked");
  }
}

async function init() {
  const workloads = await fetchJson("/api/workloads");
  if (workloads.length === 0) {
    banner.textContent = "waiting for first observations...";
    setTimeout(init, POLL_INTERVAL_MS);
    return;
  }
  currentWorkload = workloads[0];
  modelSelect.addEventListener("change", refresh);
  await refresh();
  setInterval(refresh, POLL_INTERVAL_MS);
}

init();
