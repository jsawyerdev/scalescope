const POLL_INTERVAL_MS = 3000;
// Minute history changes once a minute, so the learning panel polls slower.
const LEARNING_POLL_INTERVAL_MS = 60000;
const LOG_MAX_ROWS = 40;
// The chart shows this many forecast horizons of history, so the forecast
// takes a readable share of the x-axis instead of a sliver at the end.
const HISTORY_HORIZONS_SHOWN = 3;

// What the forecast is fitted on, per the API's demand_signal.
const DEMAND_SIGNALS = {
  request_rate: { field: "request_rate", unit: "req/s", label: "requests/s" },
  cpu_millicores: { field: "cpu_usage_millicores", unit: "mCPU", label: "total CPU" },
};

const HOLD_REASONS = {
  diagnosis: (rec) => `Adding pods would not help. ${rec.explanation}`,
  capacity_unknown: () =>
    "How much one pod can handle is not known yet, so no pod count is guessed. " +
    "Set SCALESCOPE_CAPACITY_PER_POD_RPS, give the Deployment a CPU request, " +
    "or wait for more history.",
  low_confidence: () => "The forecast range is too wide to act on yet.",
};

const MODEL_DESCRIPTIONS = {
  naive: "Repeats the last value. The baseline every model must beat.",
  seasonal_naive: "Repeats the last detected cycle; naive until a cycle is found.",
  ewma: "Smoothed average of the history, extended flat.",
  linear_trend: "Straight line through the last 60 points.",
  auto_ets: "Statistical exponential smoothing (AutoETS). The model the autoscaler uses.",
  lightgbm_quantile: "Gradient-boosted trees (LightGBM) over recent values.",
};

const TRIGGER_LABELS = {
  cpu: "CPU spike",
  memory: "Memory leak",
  traffic: "Traffic spike",
  stress: "CPU stress",
};

let currentWorkload = null;
let selectedModel = null;
let actuationModel = null;
let sourceMode = null;
let workloadLabels = {};
let triggerButtonsBusy = false;
let demandChart = null;
let podsChart = null;
let dayChart = null;
let lastSource = null;

const workloadSelect = document.getElementById("workload-select");
const triggerDurationSelect = document.getElementById("trigger-duration-select");
const fetchError = document.getElementById("fetch-error");

// ---------- helpers ----------

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

function ageSeconds(ts) {
  return Math.max(0, (Date.now() - parseTs(ts).getTime()) / 1000);
}

function themeColor(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function formatDuration(seconds) {
  const total = Math.max(1, Math.round(Number(seconds)));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const remaining = total % 60;
  return remaining ? `${minutes}m ${remaining}s` : `${minutes}m`;
}

function formatNumber(value) {
  if (!Number.isFinite(value)) return "-";
  return Math.round(value).toLocaleString();
}

function formatPercent(fraction) {
  return fraction === null || fraction === undefined ? "-" : `${(fraction * 100).toFixed(0)}%`;
}

function pods(count) {
  return `${count} pod${count === 1 ? "" : "s"}`;
}

function signalOf(forecast) {
  return DEMAND_SIGNALS[forecast.demand_signal] || DEMAND_SIGNALS.request_rate;
}

function tickSecondsOf(source) {
  const value = Number(source.tick_seconds);
  return Number.isFinite(value) && value > 0 ? value : 1;
}

function horizonLabel(index, tickSeconds) {
  return `+${formatDuration((index + 1) * tickSeconds)}`;
}

function targetLabel(workload) {
  return workloadLabels[workload] || workload;
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function appendCell(row, text, className = "") {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
  return cell;
}

function deltaSpan(value) {
  const span = document.createElement("span");
  span.className = `delta ${value > 0 ? "positive" : value < 0 ? "negative" : "zero"}`;
  span.textContent = `${value > 0 ? "+" : ""}${value}`;
  return span;
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

// ---------- status and decision ----------

// Data is fresh within 3 ticks and aging within 10; slower than that means
// collection has stalled.
function freshness(ageS, tickSeconds) {
  if (ageS <= Math.max(6, tickSeconds * 3)) return "good";
  if (ageS <= Math.max(20, tickSeconds * 10)) return "warn";
  return "bad";
}

function overallStatus(source, rec, latest, horizonText) {
  if (source.mode === "observe" && !source.connected) {
    return {
      cls: "bad",
      headline: "Not receiving data from the cluster",
      detail: source.last_error || "Collection is disconnected.",
    };
  }
  if (!latest) {
    return { cls: "warn", headline: "Waiting for the first observations", detail: "" };
  }
  const age = ageSeconds(latest.ts);
  const dataState = freshness(age, tickSecondsOf(source));
  if (dataState === "bad") {
    return {
      cls: "bad",
      headline: "Data is stale",
      detail: `The newest observation is ${formatDuration(age)} old.`,
    };
  }
  if (!rec.scaling_will_help) {
    return {
      cls: "bad",
      headline: "Scaling will not fix this",
      detail: rec.explanation,
    };
  }
  const change = rec.recommended_replicas - rec.current_replicas;
  if (rec.hold_reason) {
    return {
      cls: "warn",
      headline: "No recommendation yet",
      detail: HOLD_REASONS[rec.hold_reason](rec),
    };
  }
  if (change !== 0) {
    return {
      cls: "warn",
      headline: change > 0 ? "More pods needed soon" : "Pods can be released",
      detail:
        change > 0
          ? "Forecast demand will outgrow the current pods within the time new pods take to start."
          : "Forecast demand stays below what fewer pods can handle.",
    };
  }
  if (rec.diagnosis !== "healthy") {
    return { cls: "warn", headline: "Worth a look", detail: rec.explanation };
  }
  if (dataState === "warn") {
    return {
      cls: "warn",
      headline: "Data is arriving slowly",
      detail: `The newest observation is ${formatDuration(age)} old.`,
    };
  }
  const later = firstStepNeedingMore(rec, rec.current_replicas) >= 0;
  return {
    cls: "good",
    headline: later ? "All good for now" : "All good",
    detail: later
      ? "More pods will be needed later in the forecast; none are needed yet."
      : `${pods(rec.current_replicas)} cover the busy-case forecast for the next ${horizonText}.`,
  };
}

function renderStatus(status) {
  document.getElementById("status").className = `status ${status.cls}`;
  setText("status-headline", status.headline);
  setText("status-detail", status.detail);
}

// First forecast step at which more pods than `current` are needed, or -1.
function firstStepNeedingMore(rec, current) {
  return rec.pods_needed ? rec.pods_needed.findIndex((n) => n > current) : -1;
}

function renderDecision(rec, forecast, latest, source, horizonText) {
  const signal = signalOf(forecast);
  const tickSeconds = tickSecondsOf(source);
  const leadText = formatDuration(rec.startup_lead_steps * tickSeconds);
  const current = rec.current_replicas;
  const target = rec.recommended_replicas;
  const change = target - current;
  const neededAtPeak = rec.pods_needed ? Math.max(...rec.pods_needed) : null;
  const action = document.getElementById("decision-action");

  let reason;
  if (rec.hold_reason) {
    action.textContent = `Hold at ${pods(current)}`;
    action.className = "decision-action hold";
    reason = HOLD_REASONS[rec.hold_reason](rec);
  } else if (change > 0) {
    action.textContent = `Add ${pods(change)} now: ${current} → ${target}`;
    action.className = "decision-action up";
    reason = `Within the next ${leadText}, busy-case demand needs more than ${pods(current)} can handle. Pods take about that long to start, so they are added now.`;
    if (neededAtPeak !== null && neededAtPeak > target) {
      reason += ` ${neededAtPeak} are needed at the peak; ScaleScope adds at most 4 per step.`;
    }
  } else if (change < 0) {
    action.textContent = `Remove ${pods(-change)}: ${current} → ${target}`;
    action.className = "decision-action";
    reason = `Demand stays low for the whole next ${horizonText}, so pods can go.`;
    if (neededAtPeak !== null && neededAtPeak < target) {
      reason += ` ${pods(neededAtPeak)} would be enough; ScaleScope removes at most 2 per step so it can react if demand returns.`;
    }
  } else {
    action.textContent = `Keep ${pods(current)}`;
    action.className = "decision-action";
    const step = firstStepNeedingMore(rec, current);
    reason =
      step < 0
        ? `${pods(current)} cover the busy-case forecast for the next ${horizonText}.`
        : `${pods(current)} are enough for now. ${pods(neededAtPeak)} will be needed in about ${formatDuration((step + 1) * tickSeconds)}; they are added once that is within the pod start-up time (${leadText}).`;
  }
  if (rec.anticipated) {
    reason +=
      change > 0
        ? " This workload's learned pattern says demand usually rises within the pod start-up time, so pods are added before it arrives."
        : " This workload's learned pattern expects demand back soon, so pods are kept rather than removed and re-added.";
  }
  setText("decision-reason", reason);

  const latestValue = latest ? latest[signal.field] : Number.NaN;
  setText("fact-now", `${formatNumber(latestValue)} ${signal.unit}`);
  setText("fact-peak-label", `Busy-case peak, next ${horizonText}`);
  setText("fact-peak", `${formatNumber(rec.peak_forecast_p90)} ${signal.unit}`);
  setText(
    "fact-capacity",
    rec.capacity_per_pod === null
      ? "unknown"
      : `${formatNumber(rec.capacity_per_pod * rec.target_utilization)} ${signal.unit}`
  );
  setText("fact-needed", neededAtPeak === null ? "-" : pods(neededAtPeak));

  const modelNote =
    rec.model === actuationModel
      ? `Forecast by ${rec.model}, the model the autoscaler uses.`
      : `Viewing ${rec.model}; the autoscaler uses ${actuationModel}.`;
  const latency = rec.latency_model;
  const capacityNote = latency
    ? ` Pod capacity comes from this workload's latency curve: p95 stays under ${formatNumber(latency.latency_target_ms)} ms up to ${formatNumber(rec.capacity_per_pod * rec.target_utilization)} ${signal.unit} per pod.`
    : "";
  const demandNote =
    forecast.demand_signal === "cpu_millicores"
      ? " Demand is measured as total CPU because this workload reports no request rate."
      : "";
  const autoscaling =
    source.mode === "observe" && source.actuate
      ? " Autoscaling is on: ScaleScope applies this automatically."
      : " Advisory only: nothing in the cluster is changed.";
  setText("decision-footnote", `${modelNote}${capacityNote}${demandNote}${autoscaling}`);
}

// ---------- charts ----------

function baseChartOptions(yTitle, integerTicks) {
  return {
    animation: false,
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    scales: {
      x: {
        ticks: {
          color: themeColor("--muted"),
          maxTicksLimit: window.innerWidth < 640 ? 4 : 10,
          maxRotation: 0,
          autoSkipPadding: 12,
        },
        grid: { color: themeColor("--line") },
        border: { color: themeColor("--line-strong") },
      },
      y: {
        beginAtZero: true,
        title: { display: true, text: yTitle, color: themeColor("--muted") },
        ticks: { color: themeColor("--muted"), precision: integerTicks ? 0 : undefined },
        grid: { color: themeColor("--line") },
        border: { color: themeColor("--line-strong") },
      },
    },
    plugins: {
      legend: {
        labels: {
          color: themeColor("--text"),
          boxWidth: 14,
          boxHeight: 2,
          // Hide the band's lower edge; the band is labelled once.
          filter: (item) => !item.text.startsWith("_"),
        },
      },
    },
  };
}

function upsertChart(existing, canvasId, labels, datasets, options) {
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets = datasets;
    existing.update("none");
    return existing;
  }
  const ctx = document.getElementById(canvasId).getContext("2d");
  return new Chart(ctx, { type: "line", data: { labels, datasets }, options });
}

function line(label, data, color, extra = {}) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: "transparent",
    borderWidth: 2,
    pointRadius: 0,
    tension: 0,
    ...extra,
  };
}

function renderCharts(observations, forecast, rec, source) {
  const signal = signalOf(forecast);
  const tickSeconds = tickSecondsOf(source);
  const horizon = forecast.p50.length;
  const history = observations.slice(-horizon * HISTORY_HORIZONS_SHOWN);
  const labels = [
    ...history.map((o) => parseTs(o.ts).toLocaleTimeString()),
    ...forecast.p50.map((_, i) => horizonLabel(i, tickSeconds)),
  ];
  const pad = (n) => new Array(Math.max(n, 0)).fill(null);
  // Forecast series start at the last observed point so the lines connect.
  const futureOf = (values, last) => [...pad(history.length - 1), last, ...values];
  const lastValue = history.length ? history[history.length - 1][signal.field] : null;

  setText(
    "chart-note",
    `${signal.label}: last ${formatDuration(history.length * tickSeconds)} and the next ${formatDuration(horizon * tickSeconds)}`
  );

  const blue = themeColor("--blue");
  const orange = themeColor("--orange");
  const band = "rgba(106, 174, 224, 0.22)";
  demandChart = upsertChart(
    demandChart,
    "demand-chart",
    labels,
    [
      line("observed", [...history.map((o) => o[signal.field]), ...pad(horizon)], blue),
      line("likely range", futureOf(forecast.p90, lastValue), "transparent", {
        backgroundColor: band,
        fill: "+1",
        borderWidth: 0,
      }),
      line("_low", futureOf(forecast.p10, lastValue), "transparent", { borderWidth: 0 }),
      line("expected", futureOf(forecast.p50, lastValue), blue, { borderDash: [5, 4] }),
      line("busy case (pods sized for this)", futureOf(forecast.p90, lastValue), orange),
    ],
    baseChartOptions(signal.unit, false)
  );

  const running = history.map((o) => o.replicas);
  const lastRunning = running.length ? running[running.length - 1] : null;
  const podsDatasets = [
    line("running", [...running, ...pad(horizon)], blue, { stepped: true }),
  ];
  if (rec.pods_needed) {
    podsDatasets.push(
      line("needed for busy case", futureOf(rec.pods_needed, lastRunning), orange, {
        stepped: true,
        borderDash: [5, 4],
      })
    );
  }
  podsChart = upsertChart(podsChart, "pods-chart", labels, podsDatasets, baseChartOptions("pods", true));
}

// ---------- learning ----------

function formatDays(days) {
  if (days < 1) return formatDuration(days * 86400);
  const rounded = days < 10 ? days.toFixed(1) : Math.round(days).toString();
  return `${rounded} day${rounded === "1.0" ? "" : "s"}`;
}

function learningStage(learning) {
  const days = learning.history_days;
  if (!learning.knows_daily_pattern) {
    return {
      headline: `Learning this workload's daily pattern: ${formatDays(days)} of history so far, a day is needed.`,
      progress: Math.min(1, days),
    };
  }
  if (!learning.knows_weekly_pattern) {
    return {
      headline: `Knows the daily pattern from ${formatDays(days)} of history. The weekly pattern needs 7 days.`,
      progress: Math.min(1, days / 7),
    };
  }
  return {
    headline: `Knows the daily and weekly pattern from ${formatDays(days)} of history.`,
    // Accuracy keeps improving until about three weeks of history.
    progress: days < 21 ? days / 21 : null,
  };
}

function learningAccuracy(learning) {
  const accuracy = learning.accuracy;
  if (!learning.knows_daily_pattern) {
    return "Until then, scaling uses the last few minutes of demand only.";
  }
  if (!accuracy.scored_minutes) {
    return "Its accuracy appears here once its first forecasts can be checked against what happened, within the hour.";
  }
  const pct = (value) => `${(value * 100).toFixed(1)}%`;
  const checked =
    accuracy.scored_minutes < 1440
      ? `Over the ${formatDuration(accuracy.scored_minutes * 60)} checked so far`
      : "Over the last week";
  let text = `${checked}, its forecasts have been off by ${pct(accuracy.model_error)} on average, against ${pct(accuracy.baseline_error)} for assuming nothing changes. The busy-case line covered ${pct(accuracy.p90_coverage)} of what happened.`;
  const days = accuracy.daily_model_error;
  if (days.length >= 2) {
    text += ` Daily error: ${pct(days[0].error)} on ${days[0].day}, ${pct(days[days.length - 1].error)} on ${days[days.length - 1].day}.`;
  }
  return text;
}

function renderLearning(learning, source) {
  const stage = learningStage(learning);
  setText("learning-headline", stage.headline);
  const progress = document.getElementById("learning-progress");
  progress.hidden = stage.progress === null;
  document.getElementById("learning-progress-fill").style.width = `${Math.round((stage.progress || 0) * 100)}%`;
  setText(
    "learning-note",
    learning.trained_at
      ? `trained ${formatDuration(ageSeconds(learning.trained_at))} ago, retrains every ${formatDuration(learning.retrain_minutes * 60)}`
      : `retrains every ${formatDuration(learning.retrain_minutes * 60)}`
  );
  setText("learning-accuracy", learningAccuracy(learning));
  setText(
    "learning-footnote",
    source && source.demo_history_days
      ? `Demo: the first ${source.demo_history_days} days of this history were generated when the demo started, so the model has weeks to learn from; everything since is live.`
      : ""
  );
  renderDayChart(learning);
}

function renderDayChart(learning) {
  const signal = DEMAND_SIGNALS[learning.demand_signal] || DEMAND_SIGNALS.request_rate;
  const history = learning.history;
  const forecast = learning.forecast;
  const horizon = forecast ? forecast.p50.length : 0;
  const clock = (date) => date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const labels = history.minutes.map((m) => clock(parseTs(m)));
  if (forecast) {
    const start = parseTs(forecast.start).getTime();
    for (let i = 0; i < horizon; i++) labels.push(clock(new Date(start + i * 60000)));
  }
  const pad = (n) => new Array(Math.max(n, 0)).fill(null);
  const last = history.values.length ? history.values[history.values.length - 1] : null;
  const future = (values) => [...pad(history.values.length - 1), last, ...values];
  const blue = themeColor("--blue");
  const orange = themeColor("--orange");
  const datasets = [line("observed", [...history.values, ...pad(horizon)], blue)];
  if (forecast) {
    datasets.push(
      line("likely range", future(forecast.p90), "transparent", {
        backgroundColor: "rgba(106, 174, 224, 0.22)",
        fill: "+1",
        borderWidth: 0,
      }),
      line("_low", future(forecast.p10), "transparent", { borderWidth: 0 }),
      line("expected", future(forecast.p50), blue, { borderDash: [5, 4] }),
      line("busy case", future(forecast.p90), orange)
    );
  }
  const options = baseChartOptions(signal.unit, false);
  options.scales.x.ticks.maxTicksLimit = window.innerWidth < 640 ? 4 : 12;
  dayChart = upsertChart(dayChart, "day-chart", labels, datasets, options);
}

async function refreshLearning() {
  if (!currentWorkload) return;
  const workload = currentWorkload;
  try {
    const learning = await fetchJson(`/api/workloads/${encodeURIComponent(workload)}/learning`);
    if (workload !== currentWorkload) return;
    renderLearning(learning, lastSource);
  } catch (err) {
    if (workload !== currentWorkload) return;
    setText("learning-headline", `Could not load what it has learned: ${err.message}`);
  }
}

// ---------- engineering details ----------

function renderMetrics(latest) {
  if (!latest) return;
  setText("stat-replicas", `${latest.replicas}`);
  setText("stat-request-rate", latest.request_rate.toFixed(0));
  setText("stat-cpu", `${latest.cpu_usage_pct.toFixed(1)}%`);
  setText("stat-cpu-throttled", `${latest.cpu_throttled_pct.toFixed(1)}%`);
  setText("stat-memory", `${latest.memory_usage_mb.toFixed(0)} MB`);
  setText("stat-latency", `${latest.latency_p95_ms.toFixed(0)} ms`);
  setText("stat-error-rate", `${(latest.error_rate * 100).toFixed(2)}%`);
  setText("stat-pending", `${latest.pending_pods}`);
  setText("stat-restarts", `${latest.restarts}`);
}

function renderCluster(source) {
  if (source.mode !== "observe") {
    setText("cluster-server-text", "none (demo simulator)");
    setText("cluster-auth-text", "-");
    setText("cluster-scope-text", "demo workload");
    setText("cluster-target-count-text", "1");
    return;
  }
  const namespaces = source.k8s_namespaces || [];
  setText("cluster-server-text", source.cluster_server || "initializing");
  setText(
    "cluster-auth-text",
    `${source.cluster_auth_type || "unknown"} / ${source.cluster_auth_identity || "unknown"}`
  );
  setText(
    "cluster-scope-text",
    namespaces.length === 1 && namespaces[0] === "*" ? "all permitted" : namespaces.join(", ") || "-"
  );
  setText("cluster-target-count-text", `${(source.targets || []).length}`);
}

function renderForecastRamp(forecast, latest, source) {
  const signal = signalOf(forecast);
  const tickSeconds = tickSecondsOf(source);
  const latestValue = latest ? latest[signal.field] : Number.NaN;
  setText("forecast-ramp-model", `${forecast.model}, ${signal.unit}`);
  const length = forecast.p50.length;
  const indexes = [...new Set([0, 4, 9, 14, 19, length - 1])].filter((i) => i >= 0 && i < length);
  const rows = indexes.map((i) => {
    const row = document.createElement("tr");
    appendCell(row, horizonLabel(i, tickSeconds));
    appendCell(row, formatNumber(forecast.p10[i]), "num");
    appendCell(row, formatNumber(forecast.p50[i]), "num");
    appendCell(row, formatNumber(forecast.p90[i]), "num");
    appendCell(row, "", "num").appendChild(deltaSpan(Math.round(forecast.p90[i] - latestValue)));
    return row;
  });
  document.getElementById("forecast-ramp-body").replaceChildren(...rows);
}

function renderModelTable(models) {
  const rows = models.map((m) => {
    const row = document.createElement("tr");
    if (m.model === selectedModel) row.className = "selected";
    const name = appendCell(row, m.model);
    name.title = MODEL_DESCRIPTIONS[m.model] || "";
    appendCell(row, `${m.recommended_replicas}`, "num");
    appendCell(row, "", "num").appendChild(deltaSpan(m.recommended_replicas - m.current_replicas));
    const confidence = appendCell(row, "");
    const bar = document.createElement("span");
    bar.className = "bar";
    const fill = document.createElement("span");
    fill.className = "bar-fill";
    fill.style.display = "block";
    fill.style.width = `${Math.round(m.confidence * 100)}%`;
    bar.appendChild(fill);
    confidence.append(bar, `${Math.round(m.confidence * 100)}%`);
    appendCell(row, formatNumber(m.peak_forecast_p90), "num");
    appendCell(row, formatPercent(m.projected_utilization), "num");
    row.addEventListener("click", () => {
      selectedModel = m.model;
      refresh();
    });
    return row;
  });
  document.getElementById("model-table-body").replaceChildren(...rows);
}

function renderLog(observations) {
  const rows = observations
    .slice(-LOG_MAX_ROWS)
    .reverse()
    .map((o) => {
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
  document.getElementById("log-body").replaceChildren(...rows);
}

// ---------- source (top bar) ----------

function renderSource(source) {
  lastSource = source;
  sourceMode = source.mode;
  actuationModel = source.actuation_model;
  updateTriggerButtons();
  setText("version-text", `ScaleScope v${source.version}`);
  setText("mode-badge", source.mode === "observe" ? "OBSERVE" : "DEMO");

  const connDot = document.getElementById("source-conn-dot");
  const errorBox = document.getElementById("source-error");
  if (source.connected) {
    const age = source.last_success_ts ? ageSeconds(source.last_success_ts) : 0;
    connDot.className = `dot ${freshness(age, tickSecondsOf(source))}`;
    setText("source-conn-text", source.mode === "observe" ? "connected" : "simulated");
    errorBox.hidden = true;
  } else {
    connDot.className = "dot bad";
    setText("source-conn-text", "disconnected");
    errorBox.hidden = !source.last_error;
    errorBox.textContent = source.last_error ? `Last error: ${source.last_error}` : "";
  }

  const actuationItem = document.getElementById("actuation-item");
  actuationItem.hidden = !(source.mode === "observe" && source.actuate);
  if (!actuationItem.hidden) {
    const dot = document.getElementById("actuation-dot");
    if (source.last_actuation_error) {
      dot.className = "dot bad";
      setText("actuation-text", source.last_actuation_error);
    } else if (source.last_actuation_ts) {
      dot.className = "dot good";
      setText(
        "actuation-text",
        `set ${pods(source.last_actuation_replicas)} ${formatDuration(ageSeconds(source.last_actuation_ts))} ago`
      );
    } else {
      dot.className = "dot";
      setText("actuation-text", "on, no change needed yet");
    }
  }
}

// ---------- refresh loop ----------

async function refresh() {
  if (!currentWorkload) return;
  const workload = currentWorkload;
  const path = encodeURIComponent(workload);
  try {
    const [observations, recommendations, source] = await Promise.all([
      fetchJson(`/api/workloads/${path}/observations?limit=200`),
      fetchJson(`/api/workloads/${path}/recommendations`),
      fetchJson("/api/source"),
    ]);
    renderSource(source);
    updateWorkloadLabels(source);
    const names = recommendations.models.map((m) => m.model);
    if (!names.includes(selectedModel)) {
      selectedModel = names.includes(actuationModel) ? actuationModel : names[0];
    }
    const forecast = await fetchJson(`/api/workloads/${path}/forecast?model=${selectedModel}`);
    // The workload may have changed while these requests were in flight.
    if (workload !== currentWorkload) return;
    const rec = recommendations.models.find((m) => m.model === selectedModel);
    const latest = observations[observations.length - 1];
    const horizonText = formatDuration(forecast.p50.length * tickSecondsOf(source));

    renderStatus(overallStatus(source, rec, latest, horizonText));
    renderDecision(rec, forecast, latest, source, horizonText);
    renderCharts(observations, forecast, rec, source);
    renderMetrics(latest);
    renderCluster(source);
    renderForecastRamp(forecast, latest, source);
    renderModelTable(recommendations.models);
    renderLog(observations);
    fetchError.hidden = true;
  } catch (err) {
    fetchError.hidden = false;
    fetchError.textContent = `Connection problem: ${err.message}. Showing the last data received.`;
  }
}

// ---------- load triggers ----------

function updateTriggerButtons() {
  document.querySelectorAll(".trigger-btn").forEach((btn) => {
    btn.disabled = triggerButtonsBusy || (btn.dataset.kind === "stress" && sourceMode !== "observe");
  });
}

function setTriggerButtonsBusy(busy) {
  triggerButtonsBusy = busy;
  updateTriggerButtons();
}

function wireTriggerButtons() {
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
        return;
      }
      status.textContent = `${TRIGGER_LABELS[kind] || kind} running, ${remaining}s left`;
    }, 1000);
  }

  updateTriggerButtons();
  document.querySelectorAll(".trigger-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!currentWorkload) return;
      const kind = btn.dataset.kind;
      setTriggerButtonsBusy(true);
      status.classList.add("active");
      status.textContent = `starting ${TRIGGER_LABELS[kind] || kind}...`;
      try {
        const durationSeconds = Number.parseInt(triggerDurationSelect.value, 10);
        await postJson(
          `/api/workloads/${encodeURIComponent(currentWorkload)}/trigger?kind=${kind}&duration_seconds=${durationSeconds}`
        );
        startCountdown(kind, Date.now() + durationSeconds * 1000);
      } catch (err) {
        status.textContent = `trigger failed: ${err.message}`;
      } finally {
        setTriggerButtonsBusy(false);
      }
    });
  });
}

// ---------- replay ----------

function wireReplayButton() {
  const btn = document.getElementById("replay-run-btn");
  const status = document.getElementById("replay-status");
  btn.addEventListener("click", async () => {
    if (!currentWorkload) return;
    const workload = currentWorkload;
    btn.disabled = true;
    status.textContent = "re-forecasting past points with every model...";
    try {
      const result = await fetchJson(`/api/workloads/${encodeURIComponent(workload)}/replay`);
      if (workload !== currentWorkload) return;
      const rows = result.scores.map((s, i) => {
        const row = document.createElement("tr");
        if (i === 0) row.className = "selected";
        appendCell(row, s.model).title = MODEL_DESCRIPTIONS[s.model] || "";
        appendCell(row, `${s.n_anchors}`, "num");
        appendCell(row, s.mean_absolute_error.toFixed(2), "num");
        appendCell(row, `${s.mean_absolute_pct_error.toFixed(1)}%`, "num");
        appendCell(row, s.p90_pinball_loss.toFixed(2), "num");
        appendCell(row, `${(s.p90_coverage * 100).toFixed(0)}%`, "num");
        return row;
      });
      document.getElementById("replay-table-body").replaceChildren(...rows);
      status.textContent = `${result.n_observations} observations, ${result.scores.length} models scored`;
    } catch (err) {
      if (workload !== currentWorkload) return;
      status.textContent = `replay failed: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

function wireScalingReplayButton() {
  const btn = document.getElementById("scaling-replay-btn");
  const status = document.getElementById("scaling-replay-status");
  btn.addEventListener("click", async () => {
    if (!currentWorkload) return;
    const workload = currentWorkload;
    btn.disabled = true;
    status.textContent = "replaying recorded demand through both policies...";
    try {
      const result = await fetchJson(
        `/api/workloads/${encodeURIComponent(workload)}/scaling-replay`
      );
      if (workload !== currentWorkload) return;
      const rows = result.outcomes.map((o) => {
        const row = document.createElement("tr");
        appendCell(row, o.name);
        appendCell(row, `${o.under_provisioned_pct.toFixed(1)}%`, "num");
        appendCell(row, o.average_pods.toFixed(1), "num");
        appendCell(row, `${o.scale_changes}`, "num");
        return row;
      });
      document.getElementById("scaling-replay-body").replaceChildren(...rows);
      status.textContent = result.outcomes.length
        ? `${formatDuration(result.replayed_seconds)} replayed, a decision every ${formatDuration(result.decision_every_seconds)}`
        : `Cannot replay yet: ${result.reason}.`;
    } catch (err) {
      if (workload !== currentWorkload) return;
      status.textContent = `scaling replay failed: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

// Replay results belong to one workload; clear them when it changes.
function clearReplayResults() {
  for (const id of ["replay-table-body", "scaling-replay-body"]) {
    document.getElementById(id).replaceChildren();
  }
  setText("replay-status", "-");
  setText("scaling-replay-status", "-");
}

// ---------- startup ----------

async function loadWorkloads() {
  let workloads;
  let source;
  try {
    [workloads, source] = await Promise.all([fetchJson("/api/workloads"), fetchJson("/api/source")]);
  } catch (err) {
    fetchError.hidden = false;
    fetchError.textContent = `Connection problem: ${err.message}. Retrying.`;
    setTimeout(loadWorkloads, POLL_INTERVAL_MS);
    return;
  }
  renderSource(source);
  updateWorkloadLabels(source);
  if (workloads.length === 0) {
    fetchError.hidden = false;
    fetchError.textContent = "Waiting for the first observations...";
    setTimeout(loadWorkloads, POLL_INTERVAL_MS);
    return;
  }
  fetchError.hidden = true;
  workloadSelect.replaceChildren(
    ...workloads.map((w) => {
      const option = document.createElement("option");
      option.value = w;
      option.textContent = targetLabel(w);
      return option;
    })
  );
  currentWorkload = preferredWorkload(workloads, source);
  workloadSelect.value = currentWorkload;
  workloadSelect.addEventListener("change", () => {
    currentWorkload = workloadSelect.value;
    selectedModel = actuationModel;
    clearReplayResults();
    refresh();
    refreshLearning();
  });
  await refresh();
  refreshLearning();
  setInterval(refresh, POLL_INTERVAL_MS);
  setInterval(refreshLearning, LEARNING_POLL_INTERVAL_MS);
}

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
wireTriggerButtons();
wireReplayButton();
wireScalingReplayButton();
loadWorkloads();
