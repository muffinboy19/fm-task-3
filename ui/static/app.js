const POLL_MS = 1000;
let lastUpdated = "";
let lastPayload = null;
let clientElapsedBase = 0;
let clientElapsedAt = 0;
let elapsedTicker = null;
let lastJob = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function renderSteps(steps) {
  const el = $("#steps");
  if (!steps?.length) {
    el.innerHTML = '<p class="empty">Waiting for pipeline…</p>';
    return;
  }
  el.innerHTML = steps
    .map(
      (s) => {
        const total = steps.length || 7;
        const spin =
          s.status === "running"
            ? '<span class="step-spinner" aria-hidden="true"></span>'
            : "";
        return `
    <div class="step-card ${esc(s.status)}">
      <div class="id">Step ${esc(s.id)}/${total}</div>
      <div class="title">${spin}${esc(s.title)}</div>
      <div class="detail">${esc(s.detail || s.description || "")}</div>
      <span class="badge ${esc(s.status)}">${esc(s.status)}</span>
    </div>`;
      }
    )
    .join("");
}

function setHeartbeat(ok) {
  const el = $("#heartbeat");
  if (!el) return;
  el.classList.toggle("ok", ok);
  el.classList.toggle("err", !ok);
}

function startElapsedTicker() {
  if (elapsedTicker) return;
  elapsedTicker = setInterval(() => {
    if (!clientElapsedAt) return;
    const sec = clientElapsedBase + Math.floor((Date.now() - clientElapsedAt) / 1000);
    $("#elapsed").textContent = `${sec}s`;
  }, 1000);
}

function updateLogTail(text) {
  const logEl = $("#log-tail");
  if (!logEl) return;
  const prev = logEl.textContent;
  if (text === prev) return;
  logEl.textContent = text || "(no log yet)";
  logEl.scrollTop = logEl.scrollHeight;
}

function emptyPayload() {
  return {
    updated_at: new Date().toISOString(),
    elapsed_sec: 0,
    any_running: false,
    success: null,
    steps: [],
    events: [],
    artifacts: [],
    log_tail: "",
    files: {},
  };
}

function clearDisplay() {
  lastUpdated = "";
  applyPayload(emptyPayload());
}

function applyPayload(data) {
  lastPayload = data;
  $("#clock").textContent = new Date(data.updated_at).toLocaleTimeString();
  clientElapsedBase = data.elapsed_sec ?? 0;
  clientElapsedAt = Date.now();
  $("#elapsed").textContent = `${clientElapsedBase}s`;
  startElapsedTicker();

  const running = data.any_running || (data.steps || []).some((s) => s.status === "running");
  $("#poll-status").textContent = running ? "running" : "live";
  $("#poll-status").className = `pill ${running ? "run" : "ok"}`;

  renderSteps(data.steps);
  renderEvents(data.events);
  renderArtifacts(data.artifacts);

  const f = data.files || {};
  renderIssue(f, data.steps);
  renderContext(f, data.steps);
  renderPlan(f.plan_md, data.steps);
  renderPr(f.pr_summary_md, data.steps);
  renderDiff(f.patch_files, f.patch_stats, f.patch_raw, data.steps);
  renderValidation(f, data.steps);

  updateLogTail(data.log_tail);
  if (data.job) updateJobUi(data.job);
}

function renderEvents(events) {
  const el = $("#events");
  if (!events?.length) {
    el.innerHTML = "<li class='empty'>No events yet</li>";
    return;
  }
  el.innerHTML = [...events]
    .reverse()
    .map((e) => `<li>${e}</li>`)
    .join("");
}

function renderArtifacts(artifacts) {
  const el = $("#artifacts");
  if (!artifacts?.length) {
    el.innerHTML = "<li class='empty'>No artifacts yet</li>";
    return;
  }
  el.innerHTML = artifacts
    .map(([label, path]) => {
      const isUrl = path.startsWith("http");
      const inner = isUrl
        ? `<a href="${esc(path)}" target="_blank">${esc(path)}</a>`
        : `<span title="${esc(path)}">${esc(label)}</span>`;
      return `<li><strong>${esc(label)}</strong><br/>${inner}</li>`;
    })
    .join("");
}

function kvTable(obj, keys) {
  if (!obj) return '<p class="empty">Not available yet</p>';
  const rows = keys
    .filter(([k]) => obj[k] !== undefined && obj[k] !== null && obj[k] !== "")
    .map(([k, label]) => {
      let v = obj[k];
      if (Array.isArray(v)) v = v.join(", ");
      if (typeof v === "object") v = JSON.stringify(v, null, 2);
      return `<tr><th>${esc(label)}</th><td><pre class="json-block">${esc(String(v))}</pre></td></tr>`;
    })
    .join("");
  return rows
    ? `<table class="kv-table"><tbody>${rows}</tbody></table>`
    : '<p class="empty">Not available yet</p>';
}

function renderIssue(files, steps) {
  const st = stepStatus(steps, "1");
  if (st === "running") {
    $("#tab-issue").innerHTML =
      '<p class="empty">Loading issue…</p>';
    return;
  }
  const u = files.issue_understanding;
  const raw = files.issue_raw;
  const html = [];
  if (u) {
    html.push("<h3>Structured intake</h3>");
    html.push(
      kvTable(u, [
        ["type", "Type"],
        ["confidence", "Confidence"],
        ["symptom", "Symptom"],
        ["expected", "Expected"],
        ["actual", "Actual"],
        ["repro", "Repro"],
        ["open_questions", "Open questions"],
        ["source", "Source"],
      ])
    );
    if (u.anchors) {
      html.push("<h3>Anchors</h3>");
      html.push(`<pre class="json-block">${esc(JSON.stringify(u.anchors, null, 2))}</pre>`);
    }
  }
  if (raw) {
    html.push("<h3>Raw issue</h3>");
    html.push(
      kvTable(raw, [
        ["title", "Title"],
        ["url", "URL"],
        ["labels", "Labels"],
      ])
    );
    if (raw.body) {
      html.push("<h3>Body</h3>");
      html.push(`<pre class="md-body">${esc(raw.body.slice(0, 8000))}</pre>`);
    }
  }
  $("#tab-issue").innerHTML =
    html.join("") || '<p class="empty">Run step 1 to load issue data</p>';
}

function renderContext(files, steps) {
  const st = stepStatus(steps, "2");
  if (st === "running") {
    $("#tab-context").innerHTML =
      '<p class="empty">Building context…</p>';
    return;
  }
  const ctx = files.context;
  $("#tab-context").innerHTML = ctx
    ? `<pre class="json-block">${esc(JSON.stringify(ctx, null, 2))}</pre>`
    : '<p class="empty">Run step 2 to load context</p>';
}

function stepStatus(steps, id) {
  const s = (steps || []).find((x) => String(x.id) === String(id));
  return s?.status || "pending";
}

function renderPlan(planMd, steps) {
  const st = stepStatus(steps, "3");
  let text = "";
  if (st === "running") text = "Generating plan…";
  else if (planMd?.trim()) text = planMd;
  else text = "Plan not generated yet — step 3";
  $("#plan-body").textContent = text;
}

function renderPr(prMd, steps) {
  const st = stepStatus(steps, "7");
  let text = "";
  if (st === "running") text = "Writing PR summary…";
  else if (prMd?.trim()) text = prMd;
  else text = "PR summary not generated yet — step 7";
  $("#pr-body").textContent = text;
}

function renderDiff(patchFiles, stats, raw, steps) {
  const statsEl = $("#patch-stats");
  const st = stepStatus(steps, "4");
  if (st === "running") {
    statsEl.innerHTML = "";
    $("#diff-viewer").innerHTML =
      '<p class="empty">Generating patch…</p>';
    return;
  }
  if (!stats?.file_count) {
    statsEl.innerHTML = "";
    $("#diff-viewer").innerHTML =
      '<p class="empty">No patch yet — step 4</p>';
    return;
  }
  statsEl.innerHTML = `
    <strong>Patch</strong> · ${stats.file_count} file(s) ·
    <span style="color:var(--add-text)">+${stats.additions}</span> /
    <span style="color:var(--del-text)">-${stats.deletions}</span> ·
    tests: ${stats.has_tests ? "yes" : "NO"}
  `;

  if (!patchFiles?.length && raw) {
    $("#diff-viewer").innerHTML = `<pre class="md-body">${esc(raw)}</pre>`;
    return;
  }

  $("#diff-viewer").innerHTML = patchFiles
    .map((file) => {
      const hunks = (file.hunks || [])
        .map((h) => {
          const lines = (h.lines || [])
            .map((ln) => {
              let cls = "ctx";
              if (ln.startsWith("+") && !ln.startsWith("+++")) cls = "add";
              else if (ln.startsWith("-") && !ln.startsWith("---")) cls = "del";
              else if (ln.startsWith("@@")) cls = "meta";
              return `<code class="diff-line ${cls}">${esc(ln)}</code>`;
            })
            .join("");
          return `
          <div class="diff-hunk">
            <div class="diff-hunk-header">${esc(h.header)}</div>
            ${lines}
          </div>`;
        })
        .join("");
      return `
      <div class="diff-file">
        <div class="diff-file-header">${esc(file.path)}</div>
        ${hunks}
      </div>`;
    })
    .join("");
}

function renderValidation(files, steps) {
  const el = $("#tab-validation");
  const stPlan = stepStatus(steps, "5");
  const stGo = stepStatus(steps, "6");
  if (stGo === "running") {
    el.innerHTML = '<p class="empty">Running git apply + go build + go test…</p>';
    return;
  }
  if (stPlan === "running") {
    el.innerHTML = '<p class="empty">Checking plan adherence…</p>';
    return;
  }

  let html = "";

  const planCheck = files.plan_check;
  if (planCheck) {
    const aligned = planCheck.aligned;
    const badge = aligned
      ? '<span class="badge ok">plan aligned</span>'
      : '<span class="badge fail">plan mismatch</span>';
    const devs = (planCheck.deviations || [])
      .map((d) => `<li>${esc(String(d))}</li>`)
      .join("");
    html += `
      <h3>Plan adherence (step 5)</h3>
      <div class="patch-stats">${badge} · confidence: ${esc(planCheck.confidence || "?")}</div>
      <p>${esc(planCheck.summary || "")}</p>
      ${devs ? `<ul class="events">${devs}</ul>` : ""}
    `;
  }

  const vr = files.validation;
  if (vr) {
    const passed = vr.overall_passed;
    const badge = passed
      ? '<span class="badge ok">tests pass</span>'
      : '<span class="badge fail">tests fail</span>';
    const cmds = (vr.test_commands || [])
      .map((c) => `<li><code>${esc(Array.isArray(c) ? c.join(" ") : c)}</code></li>`)
      .join("");
    html += `
      <h3>Go validation (step 6)</h3>
      <div class="patch-stats">${badge}</div>
      <table class="kv-table"><tbody>
        <tr><th>Apply</th><td>${vr.apply_passed ? "OK" : "FAIL"}</td></tr>
        <tr><th>Build</th><td>${vr.build_passed === null ? "skipped" : vr.build_passed ? "OK" : "FAIL"}</td></tr>
        <tr><th>Tests</th><td>${vr.tests_passed === null ? "skipped" : vr.tests_passed ? "PASS" : "FAIL"}</td></tr>
        <tr><th>Tiers</th><td>${esc((vr.tiers_run || []).join(" → "))}</td></tr>
        <tr><th>Packages</th><td>${esc((vr.packages || []).join(", "))}</td></tr>
      </tbody></table>
      ${cmds ? `<h4>Commands</h4><ul class="events">${cmds}</ul>` : ""}
      ${vr.test_output ? `<h4>Output</h4><pre class="log-tail">${esc(vr.test_output.slice(-8000))}</pre>` : ""}
      ${vr.error ? `<p class="err">${esc(vr.error)}</p>` : ""}
    `;
  }

  if (!html) {
    el.innerHTML = '<p class="empty">Run steps 5–6 to validate the patch</p>';
    return;
  }

  if (files.run_summary) {
    html += "<h3>Run summary</h3>";
    html += `<pre class="json-block">${esc(JSON.stringify(files.run_summary, null, 2))}</pre>`;
  }
  el.innerHTML = html;
}

function setupTabs() {
  $$("#tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$("#tabs button").forEach((b) => b.classList.remove("active"));
      $$(".tab-panels .tab").forEach((t) => t.classList.remove("active"));
      btn.classList.add("active");
      const id = btn.dataset.tab;
      $(`#tab-${id}`).classList.add("active");
    });
  });
}

async function poll() {
  if (!$("#auto-refresh").checked) {
    setHeartbeat(false);
    $("#poll-ago").textContent = "paused";
    return;
  }
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    setHeartbeat(true);
    $("#poll-ago").textContent = "just now";

    const logChanged =
      !lastPayload || (data.log_tail || "") !== (lastPayload.log_tail || "");
    const structuralChange = data.updated_at !== lastUpdated;

    if (structuralChange || logChanged || !lastPayload) {
      lastUpdated = data.updated_at;
      applyPayload(data);
    } else if (lastPayload) {
      clientElapsedBase = data.elapsed_sec ?? clientElapsedBase;
      clientElapsedAt = Date.now();
      updateLogTail(data.log_tail);
    }
  } catch (e) {
    setHeartbeat(false);
    $("#poll-status").textContent = "offline";
    $("#poll-status").className = "pill err";
    $("#poll-ago").textContent = "retrying…";
  }
}

setInterval(() => {
  if (!lastUpdated || !$("#auto-refresh").checked) return;
  const ago = Math.floor((Date.now() - new Date(lastUpdated).getTime()) / 1000);
  if (ago < 3) $("#poll-ago").textContent = "just now";
  else if (ago < 60) $("#poll-ago").textContent = `${ago}s ago`;
  else $("#poll-ago").textContent = `${Math.floor(ago / 60)}m ago`;
}, 1000);

function updateJobUi(job) {
  lastJob = job;
  const el = $("#job-status");
  const retryBtn = $("#retry-run");
  const runBtn = $("#run-issue");
  if (!el) return;
  if (job.running) {
    el.textContent = `Running: ${job.current_issue || "…"}`;
    el.className = "job-status run";
    if (retryBtn) retryBtn.disabled = true;
    if (runBtn) runBtn.disabled = true;
  } else if (job.last_issue) {
    const exit = job.exit_code != null ? ` (exit ${job.exit_code})` : "";
    el.textContent = `Done · ${job.last_issue}${exit}`;
    el.className = "job-status muted";
    if (retryBtn) retryBtn.disabled = false;
    if (runBtn) runBtn.disabled = false;
  } else {
    el.textContent = "Paste an issue URL and click Run.";
    el.className = "job-status muted";
    if (retryBtn) retryBtn.disabled = true;
    if (runBtn) runBtn.disabled = false;
  }
}

async function postJson(url, body = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function runIssue() {
  const url = $("#issue-input")?.value?.trim();
  if (!url) {
    $("#job-status").textContent = "Enter an issue URL first";
    $("#job-status").className = "job-status err";
    return;
  }
  clearDisplay();
  try {
    const job = await postJson("/api/run", { issue_url: url });
    updateJobUi(job);
  } catch (e) {
    $("#job-status").textContent = String(e.message || e);
    $("#job-status").className = "job-status err";
  }
}

async function retryRun() {
  clearDisplay();
  try {
    const job = await postJson("/api/retry");
    updateJobUi(job);
  } catch (e) {
    $("#job-status").textContent = String(e.message || e);
    $("#job-status").className = "job-status err";
  }
}

async function resetForm() {
  const input = $("#issue-input");
  if (input) input.value = "";
  input?.focus();
  if (lastJob?.running) return;
  try {
    await postJson("/api/reset");
  } catch (_) {
    /* ignore if reset blocked */
  }
  clearDisplay();
  fetch("/api/job", { cache: "no-store" })
    .then((r) => r.json())
    .then(updateJobUi)
    .catch(() => {});
}

function setupLauncher() {
  $("#run-issue")?.addEventListener("click", runIssue);
  $("#issue-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runIssue();
  });
  $("#retry-run")?.addEventListener("click", retryRun);
  $("#new-issue")?.addEventListener("click", resetForm);
  clearDisplay();
  fetch("/api/job", { cache: "no-store" })
    .then((r) => r.json())
    .then(updateJobUi)
    .catch(() => {});
}

setupTabs();
setupLauncher();
poll();
setInterval(poll, POLL_MS);
