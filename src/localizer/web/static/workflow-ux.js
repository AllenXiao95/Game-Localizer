(() => {
  "use strict";

  const RUN_STAGE_KEYS = new Set(["scan", "extract", "tm_lookup", "translate"]);
  const VALIDATE_STAGE_KEYS = new Set(["qa", "gate"]);
  const MUTATION_SELECTOR = [
    "#saveProfile", "#preflightTask", "#launchTask", "#confirmPreflightStale",
    "#confirmRunStale", "#launchRebuild", "#publishRelease", "#syncMajorities",
    "#clusterCommitChanged", "#clusterExclude", "#groupCustomApply", "#groupSkip",
    "#groupDefer", "#unitCommit", "#unitDraft", "[data-unify]",
    "[data-cluster-check]", "#clusterRecheck", "#unitCheck"
  ].join(",");
  const CONTROL_SCOPE = {
    saveProfile: "本地任务预设",
    preflightTask: "只读任务计划",
    launchTask: "新的运行",
    confirmPreflightStale: "SQLite TM 中的过期 formal 记录",
    confirmRunStale: "当前运行与 SQLite TM",
    launchRebuild: "新的不可变子运行",
    publishRelease: "当前运行的全部已配置发布目标",
    syncMajorities: "同源多译组对应的 TM 坐标",
    clusterCommitChanged: "当前术语违规对应的 TM 坐标",
    clusterExclude: "当前术语的 exclude_scope 规则",
    groupCustomApply: "当前同源多译组的全部 TM 坐标",
    groupSkip: "当前审查决策",
    groupDefer: "当前审查决策",
    unitCommit: "当前词条的 TM 坐标",
    unitDraft: "当前审查草稿"
  };

  let reviewDirty = false;
  let busyControls = new Set();

  function workflowRecommendation(snapshot) {
    if (!snapshot.hasRun) {
      if (snapshot.staleCount > 0) {
        return {
          id: "resolve-stale", label: "处理过期 TM 记录",
          detail: `预检发现 ${snapshot.staleCount} 条 formal 记录源文已变化；先完成备份与退休。`,
          target: "preflight", kind: "focus"
        };
      }
      if (snapshot.hasPreflight) {
        return {
          id: "start", label: "启动翻译任务",
          detail: "预检计划有效，可以创建新的运行。", target: "prepare", kind: "launch"
        };
      }
      return {
        id: "preflight", label: "运行预检",
        detail: "先只读分析资源与 TM，确认待翻译量和 stale formal 风险。",
        target: "preflight", kind: "preflight"
      };
    }

    if (["queued", "running"].includes(snapshot.publishTaskStatus)) {
      return {
        id: "publish-running", label: "查看发布进度",
        detail: "发布任务正在执行；结果会按当前 run 持久化。",
        target: "publish", kind: "tab", tab: "artifact"
      };
    }

    if (["queued", "running", "waiting_confirmation"].includes(snapshot.taskStatus)
        || RUN_STAGE_KEYS.has(snapshot.stageKey)) {
      return {
        id: "live", label: "查看实时进度",
        detail: snapshot.taskStatus === "waiting_confirmation"
          ? "当前运行已暂停，等待处理过期 formal 记录后恢复。"
          : "当前运行仍在处理资源或模型翻译。",
        target: "run", kind: "tab", tab: "live"
      };
    }

    if ((snapshot.stageKey === "gate" && snapshot.stageState === "blocked")
        || (snapshot.qaAvailable && snapshot.qaPassed === false)) {
      if (snapshot.activeTab === "qa") {
        return {
          id: "repair", label: "打开定点修复",
          detail: "QualityGate 已阻断；查看问题后进入单人定点修复。",
          target: "repair", kind: "tab", tab: "review"
        };
      }
      return {
        id: "qa", label: "查看 QA 阻断项",
        detail: "这是确定性 QualityGate 阻断，不是普通运行失败。",
        target: "validate", kind: "tab", tab: "qa"
      };
    }

    if (snapshot.publishAvailable) {
      if (snapshot.publishStatus === "completed" && snapshot.publishPassed === true) {
        return {
          id: "complete", label: "发布完成",
          detail: "当前运行已完成构建与发布；无需额外的结束操作。",
          target: "publish", kind: "terminal"
        };
      }
      if (["queued", "running"].includes(snapshot.publishStatus)) {
        return {
          id: "publish-running", label: "查看发布进度",
          detail: "发布正在执行；结果会按当前 run 持久化。",
          target: "publish", kind: "tab", tab: "artifact"
        };
      }
      return {
        id: "publish-failed", label: "查看发布失败原因",
        detail: "发布未完全成功；查看 target 级结果后再决定是否重试。",
        target: "publish", kind: "tab", tab: "artifact"
      };
    }

    if (snapshot.artifactAvailable) {
      return {
        id: "publish-ready", label: "检查制品并发布",
        detail: "正式制品与 Manifest 已就绪；确认目标与治理状态后显式发布。",
        target: "build", kind: "tab", tab: "artifact"
      };
    }

    if (snapshot.qaAvailable && snapshot.qaPassed === true) {
      if (snapshot.mode === "preview") {
        return {
          id: "preview-complete", label: "查看验证结果",
          detail: "Preview 已通过 QA；如需正式发布，请创建 release 运行。",
          target: "validate", kind: "tab", tab: "summary"
        };
      }
      return {
        id: "build-pending", label: "查看构建状态",
        detail: "QualityGate 已通过，等待正式制品生成。",
        target: "build", kind: "tab", tab: "artifact"
      };
    }

    if (VALIDATE_STAGE_KEYS.has(snapshot.stageKey)) {
      return {
        id: "validate", label: "查看 QA 状态",
        detail: "翻译已进入确定性校验阶段。", target: "validate", kind: "tab", tab: "qa"
      };
    }

    return {
      id: "overview", label: "查看运行概览",
      detail: "查看当前运行状态和可用证据。", target: "run", kind: "tab", tab: "summary"
    };
  }

  function snapshot() {
    const liveTasks = selectedRun
      ? tasks.filter((item) => item.run_id === selectedRun
          && ["queued", "running", "waiting_confirmation"].includes(item.status))
      : [];
    const selectedPublishTask = liveTasks.find((item) => item.kind === "publish") || null;
    const selectedRunTask = liveTasks.find((item) => item.kind !== "publish") || null;
    return {
      hasRun: Boolean(detail && selectedRun),
      hasPreflight: Boolean(currentPreflight),
      staleCount: Number(currentPreflight?.stale_formal?.count || 0),
      runId: selectedRun || "",
      activeTab,
      taskStatus: selectedRunTask?.status || detail?.task?.status || "",
      publishTaskStatus: selectedPublishTask?.status || "",
      publishTaskId: selectedPublishTask?.task_id || "",
      stageKey: detail?.stage?.key || "",
      stageState: detail?.stage?.state || "",
      stageNote: detail?.stage?.note || "",
      qaAvailable: Boolean(detail?.qa?.available),
      qaPassed: detail?.qa?.available ? Boolean(detail.qa.passed) : null,
      qaIssueTotal: Number(detail?.qa?.issue_total || 0),
      artifactAvailable: Boolean(detail?.artifact?.available),
      publishAvailable: Boolean(detail?.publish?.available),
      publishStatus: detail?.publish?.status || "",
      publishPassed: detail?.publish?.passed,
      mode: detail?.task?.mode || "",
      parentRunId: detail?.task?.parent_run_id || ""
    };
  }

  function stepStates(state) {
    const stage = state.stageKey;
    const hasRun = state.hasRun;
    const validateDone = state.qaAvailable && state.qaPassed === true;
    const qaBlocked = state.qaAvailable && state.qaPassed === false;
    const artifact = state.artifactAvailable;
    const publishRunning = ["queued", "running"].includes(state.publishTaskStatus)
      || (state.publishAvailable && ["queued", "running"].includes(state.publishStatus));
    const published = state.publishAvailable && state.publishStatus === "completed" && state.publishPassed === true;
    const publishBlocked = state.publishAvailable && !published && !publishRunning;

    return [
      ["prepare", "准备", hasRun || state.hasPreflight ? "done" : "ready"],
      ["preflight", "预检", hasRun ? "done" : state.staleCount > 0 ? "blocked" : state.hasPreflight ? "done" : "ready"],
      ["run", "运行", !hasRun ? "idle" : RUN_STAGE_KEYS.has(stage) ? "running" : "done"],
      ["validate", "验证", !hasRun || RUN_STAGE_KEYS.has(stage) ? "idle" : qaBlocked ? "blocked" : validateDone ? "done" : "running"],
      ["repair", "修复", qaBlocked ? "ready" : validateDone ? "not-applicable" : "idle"],
      ["build", "构建", artifact ? "done" : validateDone && state.mode === "release" ? "running" : state.mode === "preview" && validateDone ? "not-applicable" : "idle"],
      ["publish", "发布", published ? "done" : publishBlocked ? "blocked" : publishRunning ? "running" : artifact ? "ready" : "idle"]
    ];
  }

  function installStyles() {
    if (document.getElementById("workflowUxStyle")) return;
    const style = document.createElement("style");
    style.id = "workflowUxStyle";
    style.textContent = `
      .workflow-context { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(260px,.8fr); gap:12px;
        padding:12px; margin-bottom:12px; border:1px solid var(--border); border-radius:9px; background:var(--bg); }
      .workflow-context .eyebrow { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
      .workflow-context .run-title { margin-top:3px; font-weight:650; }
      .workflow-context .run-note { margin-top:5px; color:var(--muted); font-size:12px; }
      .workflow-next { border-left:3px solid var(--accent); padding-left:11px; }
      .workflow-next[data-kind="terminal"] { border-left-color:var(--ok); }
      .workflow-next[data-kind="error"] { border-left-color:var(--err); }
      .workflow-next .next-label { font-weight:650; margin:2px 0; }
      .workflow-next .next-detail { color:var(--muted); font-size:12px; margin-bottom:8px; }
      .pipeline { align-items:stretch; }
      .pipeline .journey-stage { text-align:left; color:var(--fg); font:inherit; cursor:pointer; }
      .pipeline .journey-stage[data-state="ready"] { border-color:var(--accent); }
      .pipeline .journey-stage[data-state="idle"] { opacity:.65; }
      .pipeline .journey-stage[data-state="not-applicable"] { opacity:.45; border-style:dashed; }
      .pipeline .journey-stage:focus-visible, .run:focus-visible, .tabs button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
      .tabs .tab-group-label { color:var(--muted); font-size:11px; align-self:center; margin-right:3px; }
      .tabs .diagnostic-tab { opacity:.72; border-style:dashed; }
      .tabs .diagnostic-first { margin-left:8px; }
      .tabs .diagnostic-tab[aria-selected="true"] { opacity:1; border-style:solid; }
      .run .stage-line { display:flex; gap:6px; align-items:center; margin-top:4px; }
      .ux-disabled-reason { font-size:12px; color:var(--muted); }
      [data-ux-busy="true"] { cursor:progress !important; opacity:.7; }
      [data-ux-busy="true"]::after { content:" ↻"; }
      .tertiary-action { opacity:.72; border-style:dashed !important; }
      .workflow-scope { display:flex; flex-wrap:wrap; gap:6px; margin-top:5px; }
      @media (max-width:900px) { .workflow-context { grid-template-columns:1fr; } .workflow-next { border-left:0; border-top:3px solid var(--accent); padding:8px 0 0; } }
    `;
    document.head.appendChild(style);
  }

  function ensureContext() {
    const pipeline = $("pipeline");
    if (!pipeline) return null;
    let node = $("workflowContext");
    if (!node) {
      node = document.createElement("div");
      node.id = "workflowContext";
      node.className = "workflow-context";
      pipeline.insertAdjacentElement("beforebegin", node);
    }
    return node;
  }

  function renderWorkflowContext() {
    const node = ensureContext();
    if (!node) return;
    const state = snapshot();
    const next = workflowRecommendation(state);
    const mode = detail?.task?.mode || (selectedRun ? "—" : $("taskMode")?.value || "—");
    const identity = state.hasRun
      ? `<span class="mono">${esc(state.runId)}</span>`
      : `<span>新任务</span>`;
    const lineage = state.parentRunId
      ? `<span class="chip">父运行 <span class="mono">${esc(state.parentRunId)}</span></span>` : "";
    const stage = state.publishTaskStatus
      ? `<span class="chip warn">publish · ${esc(state.publishTaskStatus)}</span>`
      : state.stageKey
        ? `<span class="chip ${state.stageState === "blocked" ? "err" : state.stageState === "done" ? "ok" : "warn"}">${esc(state.stageKey)} · ${esc(state.stageState)}</span>`
        : `<span class="chip">尚未启动</span>`;
    const nextButton = next.kind === "terminal" ? "" :
      `<button type="button" class="action" id="workflowNextAction" data-next-kind="${esc(next.kind)}" data-next-target="${esc(next.target || "")}" data-next-tab="${esc(next.tab || "")}">${esc(next.label)}</button>`;
    node.innerHTML = `
      <div>
        <div class="eyebrow">当前工作对象</div>
        <div class="run-title">${identity}</div>
        <div class="workflow-scope"><span class="chip">模式 <b>${esc(mode)}</b></span>${stage}${lineage}</div>
        <div class="run-note">${esc(state.stageNote || (state.hasRun ? "选择下方工作流步骤查看对应证据。" : "配置任务参数后先运行只读预检。"))}</div>
      </div>
      <div class="workflow-next" data-kind="${next.kind === "terminal" ? "terminal" : next.id.includes("failed") ? "error" : "action"}">
        <div class="eyebrow">推荐下一步</div>
        <div class="next-label">${esc(next.label)}</div>
        <div class="next-detail">${esc(next.detail)}</div>
        ${nextButton}
      </div>`;
    $("workflowNextAction")?.addEventListener("click", () => executeRecommendation(next));
  }

  function renderJourney() {
    const pipeline = $("pipeline");
    if (!pipeline) return;
    const steps = stepStates(snapshot());
    pipeline.innerHTML = steps.map(([key, label, status], index) => {
      const badge = status === "done" ? "✓" : status === "running" ? "▶" : status === "blocked" ? "✕" : status === "not-applicable" ? "—" : "";
      const current = ["ready", "running", "blocked"].includes(status) ? ' aria-current="step"' : "";
      return `<button type="button" class="stage journey-stage" data-workflow-step="${key}" data-state="${status}"${current}
        aria-label="${esc(`${index + 1}. ${label} · ${status}`)}">
        <div class="badge">${badge}</div><div class="k">${index + 1}/7</div>
        <div class="n">${esc(label)}</div><div class="d">${esc(stepDescription(key, status))}</div>
      </button>`;
    }).join("");
  }

  function stepDescription(key, state) {
    const labels = {
      prepare: "参数与预设", preflight: "只读计划", run: "翻译执行",
      validate: "QA / QualityGate", repair: "定点修复", build: "制品 / Manifest", publish: "目标与回执"
    };
    if (state === "done") return `${labels[key]} · 已完成`;
    if (state === "blocked") return `${labels[key]} · 已阻断`;
    if (state === "not-applicable") return `${labels[key]} · 本次不适用`;
    if (state === "running") return `${labels[key]} · 进行中`;
    if (state === "ready") return `${labels[key]} · 可操作`;
    return labels[key];
  }

  function guardReviewNavigation() {
    if (activeTab !== "review" || !reviewDirty) return true;
    const ok = window.confirm("存在未提交的审查输入。离开当前修复视图会丢失这些输入，是否继续？");
    if (ok) reviewDirty = false;
    return ok;
  }

  function switchWorkflowTab(tab) {
    if (!tab || tab === activeTab) return;
    if (!guardReviewNavigation()) return;
    const safeTab = String(tab).replace(/[^a-z_-]/gi, "");
    const button = $("tabs")?.querySelector(`[data-tab="${safeTab}"]`);
    if (button) {
      button.click();
      $("detailPanel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  function focusPrepare(preflight = false) {
    if (!guardReviewNavigation()) return;
    const section = $("taskProfile")?.closest("section");
    section?.scrollIntoView({ behavior: "smooth", block: "start" });
    const target = preflight ? $("preflightTask") : $("taskProfile");
    setTimeout(() => target?.focus(), 250);
  }

  function executeRecommendation(next) {
    if (next.kind === "tab") { switchWorkflowTab(next.tab); return; }
    if (next.kind === "preflight") { focusPrepare(true); $("preflightTask")?.click(); return; }
    if (next.kind === "launch") { focusPrepare(false); $("launchTask")?.click(); return; }
    if (next.id === "resolve-stale") {
      focusPrepare(false);
      setTimeout(() => $("confirmPreflightStale")?.focus(), 250);
      return;
    }
    focusPrepare(next.target === "preflight");
  }

  function navigateStep(key) {
    const target = {
      prepare: () => focusPrepare(false), preflight: () => focusPrepare(true),
      run: () => switchWorkflowTab("live"), validate: () => switchWorkflowTab("qa"),
      repair: () => switchWorkflowTab("review"), build: () => switchWorkflowTab("artifact"),
      publish: () => switchWorkflowTab("artifact")
    }[key];
    target?.();
  }

  function decorateTabs() {
    const tabs = $("tabs");
    if (!tabs) return;
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "运行工作流视图");
    const buttons = [...tabs.querySelectorAll("button[data-tab]")];
    const byKey = new Map(buttons.map((button) => [button.dataset.tab, button]));
    let groupLabel = tabs.querySelector(".tab-group-label");
    if (!groupLabel) {
      groupLabel = document.createElement("span");
      groupLabel.className = "tab-group-label";
      groupLabel.textContent = "工作流";
      tabs.insertBefore(groupLabel, tabs.firstChild);
    }
    for (const key of ["summary", "live", "qa", "review", "artifact", "batches", "files"]) {
      const button = byKey.get(key);
      if (button) tabs.appendChild(button);
    }
    buttons.forEach((button) => {
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "tabBody");
      button.tabIndex = button.dataset.tab === activeTab ? 0 : -1;
      button.classList.toggle("diagnostic-tab", ["batches", "files"].includes(button.dataset.tab));
      button.classList.toggle("diagnostic-first", button.dataset.tab === "batches");
    });
  }

  function decorateRuns() {
    const list = $("runlist");
    if (!list) return;
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "运行记录");
    list.querySelectorAll(".run").forEach((run) => {
      run.setAttribute("role", "option");
      run.tabIndex = run.dataset.run === selectedRun ? 0 : -1;
      if (!run.querySelector(".stage-line")) {
        const meta = document.createElement("div");
        meta.className = "stage-line";
        const summary = runs.find((item) => item.run_id === run.dataset.run);
        const stage = summary?.stage;
        meta.innerHTML = stage
          ? `<span class="chip ${stage.state === "blocked" ? "err" : stage.state === "done" ? "ok" : "warn"}">${esc(stage.key)}</span><span class="muted">${esc(stage.note || "")}</span>`
          : `<span class="muted">暂无阶段证据</span>`;
        run.appendChild(meta);
      }
    });
  }

  function syncAutoRefresh() {
    const checkbox = $("autoRefresh");
    const label = checkbox?.closest("label");
    if (!checkbox || !label) return;
    let status = $("autoRefreshState");
    if (!status) {
      for (const node of [...label.childNodes]) {
        if (node.nodeType === Node.TEXT_NODE && node.nodeValue.trim()) node.nodeValue = " ";
      }
      status = document.createElement("span");
      status.id = "autoRefreshState";
      label.appendChild(status);
    }
    status.textContent = checkbox.checked ? "自动刷新 5s" : "自动刷新已暂停";
    label.classList.toggle("warn", !checkbox.checked);
    label.title = checkbox.checked
      ? "每 5 秒刷新运行状态；Review 输入不会被轮询重绘。"
      : "自动刷新已暂停；表单和 Review 输入保持不变。";
  }

  function localized(text) {
    return window.LocalizerI18n?.translateText(text) || text;
  }

  function applyControlScopes() {
    for (const [id, scope] of Object.entries(CONTROL_SCOPE)) {
      const button = $(id);
      if (!button) continue;
      button.dataset.uxScope = scope;
      button.setAttribute("aria-description", localized(`影响对象：${scope}`));
    }
    document.querySelectorAll("[data-unify]").forEach((button) => {
      button.dataset.uxScope = "当前同源多译组的全部 TM 坐标";
      button.setAttribute("aria-description", localized(`影响对象：${button.dataset.uxScope}`));
    });
  }

  function syncPrepareControls() {
    const save = $("saveProfile"), preflight = $("preflightTask"), launch = $("launchTask");
    if (save) save.textContent = "保存预设";
    if (preflight) preflight.textContent = currentPreflight ? "重新运行预检" : "运行预检";
    if (launch) launch.textContent = "启动翻译任务";

    let hint = $("launchHint");
    if (!hint && launch) {
      hint = document.createElement("span");
      hint.id = "launchHint";
      hint.className = "ux-disabled-reason";
      launch.insertAdjacentElement("afterend", hint);
    }
    if (launch && hint) {
      let reason = "";
      if (!currentPreflight) reason = "启动条件：先运行预检。";
      else if (currentPreflight.stale_formal?.count) reason = "启动条件：先处理过期 formal 记录并重新预检。";
      else reason = "预检有效：启动会创建新的 run。";
      hint.textContent = reason;
      launch.title = reason;
    }
    if (save) save.title = $("profileName")?.value.trim()
      ? "保存当前非敏感任务参数为本地预设。" : "填写预设名称后保存。";

    const stale = $("confirmPreflightStale");
    if (stale && currentPreflight?.stale_formal?.count) {
      stale.textContent = `备份 TM 并退休 ${currentPreflight.stale_formal.count} 条过期记录`;
    }
    applyControlScopes();
  }

  function decorateDynamicActions() {
    const confirmRun = $("confirmRunStale");
    const count = detail?.task?.confirmation?.count;
    if (confirmRun && count) confirmRun.textContent = `备份 TM、退休 ${count} 条并继续原任务`;
    if ($("launchRebuild")) $("launchRebuild").textContent = "创建重建子运行";
    if ($("publishRelease")) $("publishRelease").textContent = "发布到已配置目标";
    if ($("syncMajorities")) $("syncMajorities").textContent = "按多数派批量落表";
    if ($("unitDraft")) {
      $("unitDraft").textContent = "保存草稿（不改 TM 权威）";
      $("unitDraft").classList.add("tertiary-action");
    }
    $("groupSkip")?.classList.add("tertiary-action");
    $("groupDefer")?.classList.add("tertiary-action");
    if ($("clusterExclude")) $("clusterExclude").textContent = "按路径排除术语规则";

    const publish = $("publishRelease"), message = $("publishMessage");
    if (publish?.disabled && message) {
      const reason = detail?.artifact?.quality_gate_passed === false
        ? "QualityGate 未通过，不能发布。"
        : "正式制品不存在，不能发布。";
      publish.title = reason;
      message.className = "chip warn";
      message.textContent = reason;
    }
    applyControlScopes();
  }

  function refreshUx() {
    renderWorkflowContext();
    renderJourney();
    decorateTabs();
    decorateRuns();
    syncPrepareControls();
    decorateDynamicActions();
    syncAutoRefresh();
  }

  function installNavigationGuards() {
    $("tabs")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-tab]");
      if (!button || button.dataset.tab === activeTab) return;
      if (!guardReviewNavigation()) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }, true);
    $("runlist")?.addEventListener("click", (event) => {
      const run = event.target.closest(".run[data-run]");
      if (!run || run.dataset.run === selectedRun) return;
      if (!guardReviewNavigation()) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }, true);
    $("tabBody")?.addEventListener("click", (event) => {
      const row = event.target.closest("#reviewQueue .qitem[data-idx]");
      if (!row || Number(row.dataset.idx) === review.selected) return;
      if (!guardReviewNavigation()) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
    }, true);
    $("pipeline")?.addEventListener("click", (event) => {
      const step = event.target.closest("[data-workflow-step]");
      if (step) navigateStep(step.dataset.workflowStep);
    });

    $("tabs")?.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      const buttons = [...$("tabs").querySelectorAll("button[data-tab]")];
      const current = document.activeElement;
      let index = Math.max(0, buttons.indexOf(current));
      if (event.key === "ArrowRight") index = (index + 1) % buttons.length;
      if (event.key === "ArrowLeft") index = (index - 1 + buttons.length) % buttons.length;
      if (event.key === "Home") index = 0;
      if (event.key === "End") index = buttons.length - 1;
      event.preventDefault(); buttons[index]?.focus();
    });

    $("runlist")?.addEventListener("keydown", (event) => {
      const current = event.target.closest(".run[data-run]");
      if (!current) return;
      if (["Enter", " "].includes(event.key)) {
        event.preventDefault(); current.click(); return;
      }
      if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
      const rows = [...$("runlist").querySelectorAll(".run[data-run]")];
      let index = rows.indexOf(current);
      index += event.key === "ArrowDown" ? 1 : -1;
      index = Math.max(0, Math.min(rows.length - 1, index));
      event.preventDefault(); rows[index]?.focus();
    });
  }

  function installReviewDirtyTracking() {
    $("tabBody")?.addEventListener("input", (event) => {
      if (activeTab === "review" && event.target.matches("input,textarea,select")) reviewDirty = true;
    }, true);
    window.addEventListener("beforeunload", (event) => {
      if (!reviewDirty) return;
      event.preventDefault(); event.returnValue = "";
    });
  }

  function releaseBusyControl(button) {
    if (!button?.isConnected) return;
    button.dataset.uxBusy = "false";
    button.removeAttribute("aria-busy");
    button.removeAttribute("data-ux-request-started");
    busyControls.delete(button);
  }

  function installMutationContract() {
    document.addEventListener("click", (event) => {
      const button = event.target.closest(MUTATION_SELECTOR);
      if (!button) return;
      if (button.dataset.uxBusy === "true") {
        event.preventDefault(); event.stopImmediatePropagation(); return;
      }
      if (button.id === "saveProfile" && !$("profileName")?.value.trim()) {
        event.preventDefault(); event.stopImmediatePropagation();
        $("taskMessage").className = "chip warn";
        $("taskMessage").textContent = "请先填写预设名称。";
        $("profileName")?.focus();
        return;
      }
      button.dataset.uxBusy = "true";
      button.setAttribute("aria-busy", "true");
      busyControls.add(button);
      setTimeout(() => {
        if (button.dataset.uxRequestStarted !== "true") releaseBusyControl(button);
      }, 0);
    }, true);

    const legacyPostApi = postApi;
    postApi = async function(path, payload) {
      for (const button of busyControls) button.dataset.uxRequestStarted = "true";
      try {
        return await legacyPostApi(path, payload);
      } finally {
        setTimeout(() => {
          for (const button of [...busyControls]) releaseBusyControl(button);
          refreshUx();
        }, 0);
      }
    };
  }

  function wrapLegacyRenderers() {
    const legacyPipeline = renderPipeline;
    renderPipeline = function() { legacyPipeline(); refreshUx(); };

    const legacyTabs = renderTabs;
    renderTabs = function() { legacyTabs(); decorateTabs(); };

    const legacyRuns = renderRuns;
    renderRuns = function() { legacyRuns(); decorateRuns(); };

    const legacyTaskStatus = renderTaskStatus;
    renderTaskStatus = function() { legacyTaskStatus(); renderWorkflowContext(); syncPrepareControls(); };

    const legacyPreflight = renderPreflight;
    renderPreflight = function() { legacyPreflight(); refreshUx(); };

    const legacyInvalidate = invalidatePreflight;
    invalidatePreflight = function() { legacyInvalidate(); refreshUx(); };

    const legacyTab = renderTab;
    renderTab = async function() {
      const result = await legacyTab();
      decorateDynamicActions(); renderWorkflowContext(); decorateTabs();
      return result;
    };

    const legacyReview = renderReview;
    renderReview = async function() {
      const result = await legacyReview(); reviewDirty = false; decorateDynamicActions();
      return result;
    };

    const legacyQueue = loadReviewQueue;
    loadReviewQueue = async function() {
      const result = await legacyQueue(); reviewDirty = false; decorateDynamicActions();
      return result;
    };
  }

  installStyles();
  ensureContext();
  installNavigationGuards();
  installReviewDirtyTracking();
  installMutationContract();
  wrapLegacyRenderers();
  $("autoRefresh")?.addEventListener("change", syncAutoRefresh);
  refreshUx();

  document.addEventListener("localizer:locale-changed", () => setTimeout(refreshUx, 0));
  window.LocalizerWorkflowUX = { workflowRecommendation, stepStates, snapshot, refresh: refreshUx };
})();
