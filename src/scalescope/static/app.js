const POLL_INTERVAL_MS = 3000;
const DEFAULT_MODEL = "ewma";
const MIN_CHART_OVERLAY_CONFIDENCE = 0.10;

// The multi-model forecast overlay is not urgent evidence like observations
// or the selected model's band, so it refetches on a slower cadence to avoid
// firing 6 forecast requests every 3s poll.
const MULTI_MODEL_POLL_MS = 12000;

// Freshness thresholds, in seconds since the latest observation's ts.
const FRESH_MAX_S = 6;
const AGING_MAX_S = 20;

// Raw log panel: cap rows kept in the DOM.
const LOG_MAX_ROWS = 40;

const MODEL_COLOR_VARS = {
  naive: "--chart-naive",
  seasonal_naive: "--chart-seasonal-naive",
  ewma: "--chart-ewma",
  linear_trend: "--chart-linear-trend",
  auto_ets: "--chart-auto-ets",
  lightgbm_quantile: "--chart-lightgbm",
};
const FALLBACK_MODEL_COLOR_VAR = "--chart-muted";

// Short fit description per model, shown as a hover tooltip on its name.
// Mirrors README.md's "Forecast models" table.
const MODEL_DESCRIPTIONS = {
  naive: "Repeats the last observed value flat. No minimum history. Good on flat stretches, poor on trends or seasonality.",
  seasonal_naive: "Repeats the last cycle of the seasonal period detected in the history. Falls back to naive until a period is confidently detected (at least two full cycles).",
  ewma: "Exponentially weighted average of the whole history, extrapolated flat. Smooths noise; always flattens, so it misses trend and seasonality.",
  linear_trend: "Least-squares line over the last 60 points. Captures short local trends; can't turn over for a full cycle.",
  auto_ets: "Nixtla StatsForecast AutoETS, general-purpose statistical fit with an 80% interval. Needs 30+ ticks; falls back to naive below that or if the fit fails.",
  lightgbm_quantile: "Three LightGBM quantile regressors (p10/p50/p90) over lag and rolling-stat features. Needs 60+ ticks; falls back to naive below that or if fitting fails.",
};

let currentWorkload = null;
let selectedModel = DEFAULT_MODEL;
let chart = null;
let pollTimer = null;
let allModelForecasts = {};
let lastMultiModelFetchAt = 0;
let sourceMode = null;
let workloadLabels = {};
let triggerButtonsBusy = false;

const workloadSelect = document.getElementById("workload-select");
const triggerDurationSelect = document.getElementById("trigger-duration-select");
const fetchError = document.getElementById("fetch-error");

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url} -> ${response.status}`);
  }
  return response.json();
}

async function postJson(url) {
  const response = await fetch(url, { method: "POST" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || `${url} -> ${response.status}`);
  }
  return body;
}

// Observation timestamps are UTC but serialized without an offset; append
// "Z" so the browser parses them as UTC instead of local time.
function parseTs(ts) {
  return new Date(ts.endsWith("Z") ? ts : `${ts}Z`);
}

function formatDiagnosis(diagnosis) {
  return diagnosis.replace(/_/g, " ");
}

function targetLabel(workload) {
  return workloadLabels[workload] || workload;
}

function sourceTickSeconds(source) {
  const value = Number(source.tick_seconds);
  return Number.isFinite(value) && value > 0 ? value : 1;
}

function formatDuration(seconds) {
  const total = Math.max(1, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  return remaining ? `${minutes}m ${remaining}s` : `${minutes}m`;
}

function formatReq(value) {
  if (!Number.isFinite(value)) return "-";
  return Math.round(value).toLocaleString();
}

function signedReqDelta(value) {
  if (!Number.isFinite(value)) return "-";
  const rounded = Math.round(value);
  const sign = rounded > 0 ? "+" : "";
  return `${sign}${rounded.toLocaleString()}`;
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
  return cell;
}

function replaceRows(body, rows) {
  body.replaceChildren(...rows);
}

function horizonLabel(index, tickSeconds) {
  return `+${formatDuration((index + 1) * tickSeconds)}`;
}

function updateWorkloadLabels(source) {
  workloadLabels = {};
  for (const target of source.targets || []) {
    workloadLabels[target.id] = `${target.namespace}/${target.deployment}`;
  }
}

function preferredWorkload(workloads, source) {
  if (currentWorkload && workloads.includes(currentWorkload)) return currentWorkload;

  const metricsTarget = (source.targets || []).find(
    (target) => target.metrics_url_configured && workloads.includes(target.id)
  );
  if (metricsTarget) return metricsTarget.id;

  const configuredTarget =
    source.k8s_namespace && source.k8s_deployment
      ? `${source.k8s_namespace}:${source.k8s_deployment}`
      : null;
  if (configuredTarget && workloads.includes(configuredTarget)) return configuredTarget;

  return workloads[0];
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
  document.getElementById("source-text").textContent = targetLabel(recommendations.workload);
}

function scopeLabel(source) {
  const namespaces = source.k8s_namespaces || [];
  if (!namespaces.length) return "-";
  if (namespaces.length === 1 && namespaces[0] === "*") return "all permitted namespaces";
  return namespaces.join(", ");
}

function renderCluster(source) {
  const statusNote = document.getElementById("cluster-status-note");
  const serverText = document.getElementById("cluster-server-text");
  const authText = document.getElementById("cluster-auth-text");
  const scopeText = document.getElementById("cluster-scope-text");
  const targetCountText = document.getElementById("cluster-target-count-text");
  const isObserve = source.mode === "observe";

  if (!isObserve) {
    statusNote.textContent = "demo simulator";
    serverText.textContent = "no cluster";
    authText.textContent = "none";
    scopeText.textContent = "demo workload";
    targetCountText.textContent = "1";
    return;
  }

  if (source.connected && source.last_success_ts) {
    const ageS = Math.max(0, (Date.now() - parseTs(source.last_success_ts).getTime()) / 1000);
    const { label } = classifyFreshness(ageS);
    statusNote.textContent = `OBSERVE ${label}`;
  } else {
    statusNote.textContent = source.connected ? "connected" : "disconnected";
  }

  const authType = source.cluster_auth_type || "unknown auth";
  const authIdentity = source.cluster_auth_identity || "identity unavailable";
  serverText.textContent = source.cluster_server || "cluster initializing";
  authText.textContent = `${authType} / ${authIdentity}`;
  scopeText.textContent = scopeLabel(source);
  targetCountText.textContent = `${(source.targets || []).length}`;
}

function themeColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function modelColor(model) {
  return themeColor(MODEL_COLOR_VARS[model] || FALLBACK_MODEL_COLOR_VAR);
}

function selectedForecastLabel(forecast) {
  if (forecast.model === selectedModel) return selectedModel;
  return `${selectedModel} via ${forecast.model}`;
}

// Shared freshness classification for observation age and source collection age.
function classifyFreshness(ageS) {
  const ageLabel = ageS <= FRESH_MAX_S ? `${ageS.toFixed(1)}s ago` : `${Math.round(ageS)}s ago`;
  if (ageS <= FRESH_MAX_S) return { cls: "good", label: `fresh - ${ageLabel}`, ageLabel };
  if (ageS <= AGING_MAX_S) return { cls: "warn", label: `aging - ${ageLabel}`, ageLabel };
  return { cls: "bad", label: `stale - ${ageLabel}`, ageLabel };
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

// Source/identity panel: live source identity and collection health.
function renderSource(source) {
  const modeBadge = document.getElementById("mode-badge");
  const targetText = document.getElementById("source-target-text");
  const connDot = document.getElementById("source-conn-dot");
  const connText = document.getElementById("source-conn-text");
  const errorBox = document.getElementById("source-error");
  document.getElementById("version-text").textContent = `ScaleScope v${source.version}`;

  const isObserve = source.mode === "observe";
  sourceMode = source.mode;
  updateTriggerButtons();
  modeBadge.textContent = isObserve ? "OBSERVE" : "DEMO";
  modeBadge.className = isObserve ? "badge mode" : "badge mode demo";

  const selectedTarget = (source.targets || []).find((target) => target.id === currentWorkload);
  const observingText = selectedTarget
    ? `${selectedTarget.namespace}/${selectedTarget.deployment}`
    : (source.k8s_namespaces || []).join(", ");
  targetText.textContent = isObserve
    ? `${observingText}\n${source.cluster_server}`
    : "demo simulator - no cluster";

  if (source.connected) {
    if (source.last_success_ts) {
      const ageS = Math.max(0, (Date.now() - parseTs(source.last_success_ts).getTime()) / 1000);
      const { cls, ageLabel } = classifyFreshness(ageS);
      connDot.className = `dot ${cls}`;
      connText.textContent = `connected - last success ${ageLabel}`;
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

  const actuationItem = document.getElementById("actuation-item");
  const actuationDot = document.getElementById("actuation-dot");
  const actuationText = document.getElementById("actuation-text");
  if (isObserve && source.actuate) {
    actuationItem.hidden = false;
    if (source.last_actuation_error) {
      actuationDot.className = "dot bad";
      actuationText.textContent = source.last_actuation_error;
    } else if (source.last_actuation_ts) {
      const ageS = Math.max(0, (Date.now() - parseTs(source.last_actuation_ts).getTime()) / 1000);
      const { cls } = classifyFreshness(ageS);
      actuationDot.className = `dot ${cls}`;
      actuationText.textContent = `set replicas=${source.last_actuation_replicas} - ${Math.round(ageS)}s ago`;
    } else {
      actuationDot.className = "dot";
      actuationText.textContent = "enabled - no action taken yet";
    }
  } else {
    actuationItem.hidden = true;
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

function rampIndexes(length) {
  const candidates = [0, 4, 9, 14, 19, length - 1];
  return [...new Set(candidates)].filter((index) => index >= 0 && index < length);
}

function renderForecastRamp(latest, forecast, recommendation, source) {
  const label = selectedForecastLabel(forecast);
  const tickSeconds = sourceTickSeconds(source);
  const length = forecast.p50.length;
  const latestRate = latest ? latest.request_rate : Number.NaN;
  const peakP50 = length ? Math.max(...forecast.p50) : Number.NaN;
  const peakP90 = length ? Math.max(...forecast.p90) : Number.NaN;

  document.getElementById("forecast-ramp-model").textContent = label;
  document.getElementById("forecast-ramp-window").textContent = length
    ? `${length} steps over ${formatDuration(length * tickSeconds)}`
    : "waiting for forecast";
  document.getElementById("forecast-now").textContent = `${formatReq(latestRate)} req/s`;
  document.getElementById("forecast-peak-p50").textContent = `${formatReq(peakP50)} req/s`;
  document.getElementById("forecast-peak-p90").textContent = `${formatReq(peakP90)} req/s`;

  if (recommendation) {
    const delta = recommendation.recommended_replicas - recommendation.current_replicas;
    const sign = delta > 0 ? "+" : "";
    document.getElementById(
      "forecast-replicas"
    ).textContent = `${recommendation.current_replicas} -> ${recommendation.recommended_replicas} (${sign}${delta})`;
    document.getElementById("forecast-confidence").textContent = `${Math.round(
      recommendation.confidence * 100
    )}%`;
    document.getElementById("forecast-pod-load").textContent = `${(
      recommendation.projected_utilization * 100
    ).toFixed(0)}%`;
  } else {
    document.getElementById("forecast-replicas").textContent = "-";
    document.getElementById("forecast-confidence").textContent = "-";
    document.getElementById("forecast-pod-load").textContent = "-";
  }

  const body = document.getElementById("forecast-ramp-body");
  if (!length) {
    const row = document.createElement("tr");
    const cell = appendCell(row, "waiting for forecast...");
    cell.colSpan = 5;
    replaceRows(body, [row]);
    return;
  }

  const rows = rampIndexes(length).map((index) => {
    const row = document.createElement("tr");
    const p90Delta = forecast.p90[index] - latestRate;
    appendCell(row, horizonLabel(index, tickSeconds));
    appendCell(row, formatReq(forecast.p10[index]), "num");
    appendCell(row, formatReq(forecast.p50[index]), "num");
    appendCell(row, formatReq(forecast.p90[index]), "num");
    const delta = appendCell(row, "", "num");
    delta.appendChild(deltaBadge(signedReqDelta(p90Delta), p90Delta));
    return row;
  });
  replaceRows(body, rows);
}

function deltaBadge(text, value) {
  const badge = document.createElement("span");
  badge.className = `delta ${value > 0 ? "positive" : value < 0 ? "negative" : "zero"}`;
  badge.textContent = text;
  return badge;
}

function confidenceCell(confidence) {
  const pct = Math.round(confidence * 100);
  const cell = document.createElement("div");
  cell.className = "confidence-cell hint";
  cell.title = "Derived from forecast band width relative to peak demand, not a statistical guarantee.";

  const bar = document.createElement("div");
  bar.className = "confidence-bar";
  const fill = document.createElement("div");
  fill.className = "confidence-bar-fill";
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);

  const label = document.createElement("span");
  label.textContent = `${pct}%`;

  cell.append(bar, label);
  return cell;
}

function modelNameCell(model) {
  const description = MODEL_DESCRIPTIONS[model] || "";
  const label = document.createElement("span");
  label.className = "hint";
  label.title = description;
  label.textContent = model;
  return label;
}

function renderTable(models) {
  const body = document.getElementById("model-table-body");
  const rows = models.map((m) => {
    const row = document.createElement("tr");
    if (m.model === selectedModel) row.className = "selected";
    row.dataset.model = m.model;

    const modelCell = appendCell(row, "", "model-name");
    modelCell.appendChild(modelNameCell(m.model));
    appendCell(row, `${m.recommended_replicas}`, "num");

    const deltaValue = m.recommended_replicas - m.current_replicas;
    const deltaCell = appendCell(row, "", "num");
    const sign = deltaValue > 0 ? "+" : "";
    deltaCell.appendChild(deltaBadge(`${sign}${deltaValue}`, deltaValue));

    const confidence = appendCell(row, "");
    confidence.appendChild(confidenceCell(m.confidence));
    appendCell(row, m.peak_forecast_p90.toFixed(0), "num");
    appendCell(row, `${(m.projected_utilization * 100).toFixed(0)}%`, "num");
    return row;
  });
  replaceRows(body, rows);
  body.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => {
      selectedModel = row.dataset.model || DEFAULT_MODEL;
      refresh();
    });
  });
}

function renderLog(observations) {
  const body = document.getElementById("log-body");
  const rows = observations.slice(-LOG_MAX_ROWS).reverse().map((o) => {
    const row = document.createElement("tr");
    appendCell(row, parseTs(o.ts).toLocaleTimeString());
    appendCell(row, `${o.replicas}`, "num");
    appendCell(row, o.request_rate.toFixed(1), "num");
    appendCell(row, `${o.cpu_usage_pct.toFixed(1)}%`, "num");
    appendCell(row, `${o.cpu_throttled_pct.toFixed(1)}%`, "num");
    appendCell(row, o.latency_p95_ms.toFixed(0), "num");
    appendCell(row, `${(o.error_rate * 100).toFixed(2)}%`, "num");
    appendCell(row, `${o.pending_pods}`, "num");
    appendCell(row, `${o.restarts}`, "num");
    return row;
  });
  replaceRows(body, rows);
}

function updateChart(observations, forecast, allForecasts, modelNames, confidenceByModel, source) {
  const selectedLabel = selectedForecastLabel(forecast);
  const tickSeconds = sourceTickSeconds(source);
  document.getElementById("chart-model-name").textContent = selectedLabel;

  const historyLabels = observations.map((o) => parseTs(o.ts).toLocaleTimeString());
  const historyValues = observations.map((o) => o.request_rate);
  const lastIndex = historyValues.length - 1;
  const forecastLabels = forecast.p50.map((_, i) => horizonLabel(i, tickSeconds));
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
      borderColor: themeColor("--chart-observed"),
      backgroundColor: "transparent",
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    },
    {
      label: `${selectedLabel} p50 (selected)`,
      data: anchorLast(forecast.p50),
      borderColor: themeColor("--chart-selected"),
      backgroundColor: "transparent",
      borderDash: [4, 4],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    },
    {
      label: `${selectedModel} p90`,
      data: anchorLast(forecast.p90),
      borderColor: "transparent",
      backgroundColor: themeColor("--chart-band"),
      pointRadius: 0,
      fill: "+1",
    },
    {
      label: `${selectedModel} p10`,
      data: anchorLast(forecast.p10),
      borderColor: "transparent",
      backgroundColor: themeColor("--chart-band"),
      pointRadius: 0,
      fill: false,
    },
  ];

  // Overlay every other registered model's p50 line, thin and unshaded, so
  // trajectories can be compared at a glance. The selected model keeps its
  // full p10-p90 band above; these are the "at a glance" comparison lines.
  for (const name of modelNames || []) {
    if (name === selectedModel) continue;
    if ((confidenceByModel[name] ?? 1) < MIN_CHART_OVERLAY_CONFIDENCE) continue;
    const other = allForecasts && allForecasts[name];
    if (!other) continue;
    datasets.push({
      label: `${name} p50`,
      data: anchorLast(other.p50),
      borderColor: modelColor(name),
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
          x: {
            ticks: { color: themeColor("--text-muted"), maxTicksLimit: 12 },
            grid: { color: themeColor("--border") },
          },
          y: {
            ticks: { color: themeColor("--text-muted") },
            grid: { color: themeColor("--border") },
            beginAtZero: true,
          },
        },
        plugins: {
          legend: { labels: { color: themeColor("--text") } },
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
  const workloadPath = encodeURIComponent(currentWorkload);
  try {
    const [observations, recommendations, forecast, source] = await Promise.all([
      fetchJson(`/api/workloads/${workloadPath}/observations?limit=200`),
      fetchJson(`/api/workloads/${workloadPath}/recommendations`),
      fetchJson(`/api/workloads/${workloadPath}/forecast?model=${selectedModel}`),
      fetchJson("/api/source"),
    ]);
    updateWorkloadLabels(source);
    if (!recommendations.models.some((m) => m.model === selectedModel)) {
      selectedModel = recommendations.models[0].model;
    }
    const modelNames = recommendations.models.map((m) => m.model);
    const confidenceByModel = Object.fromEntries(
      recommendations.models.map((m) => [m.model, m.confidence])
    );
    allModelForecasts[selectedModel] = forecast;

    // Refetch the other models' forecasts on a slower cadence than the main
    // poll (see MULTI_MODEL_POLL_MS) rather than on every 3s tick.
    if (Date.now() - lastMultiModelFetchAt >= MULTI_MODEL_POLL_MS) {
      const others = modelNames.filter((m) => m !== selectedModel);
      const otherForecasts = await Promise.all(
        others.map((m) => fetchJson(`/api/workloads/${workloadPath}/forecast?model=${m}`))
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
    renderCluster(source);
    renderEvidence(recommendations);
    renderFreshness(latest ? latest.ts : null);
    renderMetrics(latest);
    const selectedRecommendation = recommendations.models.find((m) => m.model === selectedModel);
    renderForecastRamp(latest, forecast, selectedRecommendation, source);
    renderTable(recommendations.models);
    renderLog(observations);
    updateChart(observations, forecast, allModelForecasts, modelNames, confidenceByModel, source);
    fetchError.hidden = true;
  } catch (err) {
    fetchError.hidden = false;
    fetchError.textContent = `connection error: ${err.message} - showing last known data`;
  }
}

const TRIGGER_LABELS = {
  cpu: "Spike CPU",
  memory: "Leak Memory",
  traffic: "Spike Traffic",
  stress: "Stress CPU",
};

function triggerButtonAvailable(btn) {
  return btn.dataset.kind !== "stress" || sourceMode === "observe";
}

function updateTriggerButtons() {
  document.querySelectorAll(".trigger-btn").forEach((btn) => {
    btn.disabled = triggerButtonsBusy || !triggerButtonAvailable(btn);
  });
}

function setTriggerButtonsBusy(busy) {
  triggerButtonsBusy = busy;
  updateTriggerButtons();
}

function triggerDurationSeconds() {
  return Number.parseInt(triggerDurationSelect.value, 10);
}

function wireTriggerButtons() {
  const buttons = document.querySelectorAll(".trigger-btn");
  const status = document.getElementById("trigger-status");
  let countdownTimer = null;

  function startCountdown(kind, endsAt) {
    clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
      const remaining = Math.ceil((endsAt - Date.now()) / 1000);
      if (remaining <= 0) {
        clearInterval(countdownTimer);
        status.textContent = "no trigger active";
        status.classList.remove("active");
        setTriggerButtonsBusy(false);
        return;
      }
      status.textContent = `${TRIGGER_LABELS[kind] || kind} active - ${remaining}s remaining`;
    }, 1000);
  }

  updateTriggerButtons();
  buttons.forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!currentWorkload) return;
      setTriggerButtonsBusy(true);
      status.classList.add("active");
      status.textContent = `triggering ${TRIGGER_LABELS[btn.dataset.kind] || btn.dataset.kind}...`;
      try {
        const workloadPath = encodeURIComponent(currentWorkload);
        const durationSeconds = triggerDurationSeconds();
        await postJson(
          `/api/workloads/${workloadPath}/trigger?kind=${btn.dataset.kind}&duration_seconds=${durationSeconds}`
        );
        startCountdown(btn.dataset.kind, Date.now() + durationSeconds * 1000);
        setTriggerButtonsBusy(false);
      } catch (err) {
        status.textContent = `trigger failed: ${err.message}`;
        setTriggerButtonsBusy(false);
      }
    });
  });
}

// Replay is a full model-retrain pass over recorded history (seconds, not
// milliseconds), so it runs on demand rather than on the main 3s poll cycle.
function renderReplay(result) {
  const body = document.getElementById("replay-table-body");
  const rows = result.scores.map((s, i) => {
    const row = document.createElement("tr");
    if (i === 0) row.className = "selected";
    const modelCell = appendCell(row, "", "model-name");
    modelCell.appendChild(modelNameCell(s.model));
    appendCell(row, `${s.n_anchors}`, "num");
    appendCell(row, s.mean_absolute_error.toFixed(2), "num");
    appendCell(row, `${s.mean_absolute_pct_error.toFixed(1)}%`, "num");
    return row;
  });
  replaceRows(body, rows);
}

function wireReplayButton() {
  const btn = document.getElementById("replay-run-btn");
  const status = document.getElementById("replay-status");
  btn.addEventListener("click", async () => {
    if (!currentWorkload) return;
    btn.disabled = true;
    status.textContent = "backtesting every model against recorded history...";
    try {
      const workloadPath = encodeURIComponent(currentWorkload);
      const result = await fetchJson(`/api/workloads/${workloadPath}/replay`);
      renderReplay(result);
      status.textContent = `${result.n_observations} observations, ${result.scores.length} models scored`;
    } catch (err) {
      status.textContent = `replay failed: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

async function loadWorkloads() {
  let workloads;
  let source;
  try {
    [workloads, source] = await Promise.all([
      fetchJson("/api/workloads"),
      fetchJson("/api/source"),
    ]);
  } catch (err) {
    fetchError.hidden = false;
    fetchError.textContent = `connection error: ${err.message} - retrying`;
    setTimeout(loadWorkloads, POLL_INTERVAL_MS);
    return;
  }
  updateWorkloadLabels(source);
  if (workloads.length === 0) {
    fetchError.hidden = false;
    fetchError.textContent = "waiting for first observations...";
    setTimeout(loadWorkloads, POLL_INTERVAL_MS);
    return;
  }
  const options = workloads.map((w) => {
    const option = document.createElement("option");
    option.value = w;
    option.textContent = targetLabel(w);
    return option;
  });
  workloadSelect.replaceChildren(...options);
  currentWorkload = preferredWorkload(workloads, source);
  workloadSelect.value = currentWorkload;
  workloadSelect.title = targetLabel(currentWorkload);
  workloadSelect.addEventListener("change", () => {
    currentWorkload = workloadSelect.value;
    workloadSelect.title = targetLabel(currentWorkload);
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

wireTriggerButtons();
wireReplayButton();
loadWorkloads();
