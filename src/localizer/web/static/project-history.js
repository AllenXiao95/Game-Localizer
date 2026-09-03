/* Project-level Review change history.
 *
 * This is a presentation over the existing append-only ReviewDecisionLog.  It does
 * not create a second history store and deliberately does not claim to be a full TM
 * lifecycle/event-sourcing view.
 */
(() => {
  const historyState = {
    view: "tm",
    action: "all",
    status: "all",
    runId: "",
    query: "",
    payload: null,
    rows: [],
    selected: 0,
  };

  const ACTION_LABELS = {
    all: "全部操作",
    draft: "draft · 草稿",
    commit: "commit · 人工落表",
    unify: "unify · 同源统一",
    glossary: "glossary · 术语调整",
    accept_debt: "accept_debt · 接受债务",
    retire: "retire · 退休记录",
    revert: "revert · 撤销",
    skip: "skip · 跳过",
    defer: "defer · 待议",
  };

  const STATUS_LABELS = {
    all: "全部状态",
    current: "当前仍生效",
    superseded: "已被后续修改",
    reverted: "已撤销",
    mixed: "混合状态",
    recorded: "仅记录",
  };

  function statusChip(status) {
    if (status === "current") return `<span class="chip ok">当前仍生效</span>`;
    if (status === "reverted") return `<span class="chip">已撤销</span>`;
    if (status === "superseded") return `<span class="chip warn">已被后续修改</span>`;
    if (status === "mixed") return `<span class="chip warn">混合状态</span>`;
    return `<span class="chip">仅记录</span>`;
  }

  function installProjectState() {
    const tmPanel = $("tmPanel");
    if (!tmPanel || $("projectStateTabs")) return;
    const section = tmPanel.parentElement;
    if (!section) return;
    section.id = "projectStateSection";
    const heading = section.querySelector("h2");
    if (heading) heading.textContent = "项目状态";

    const tabs = document.createElement("div");
    tabs.className = "tabs";
    tabs.id = "projectStateTabs";
    tabs.innerHTML = `
      <button type="button" data-project-state="tm" aria-selected="true">翻译记忆库</button>
      <button type="button" data-project-state="history">变更历史</button>`;
    section.insertBefore(tabs, tmPanel);

    const historyPanel = document.createElement("div");
    historyPanel.id = "projectHistoryPanel";
    historyPanel.hidden = true;
    section.appendChild(historyPanel);

    tabs.querySelectorAll("[data-project-state]").forEach((button) => {
      button.addEventListener("click", () => switchProjectState(button.dataset.projectState));
    });
    renderProjectHistoryShell();
  }

  async function switchProjectState(view) {
    historyState.view = view === "history" ? "history" : "tm";
    const tmPanel = $("tmPanel");
    const historyPanel = $("projectHistoryPanel");
    if (!tmPanel || !historyPanel) return;
    tmPanel.hidden = historyState.view !== "tm";
    historyPanel.hidden = historyState.view !== "history";
    document.querySelectorAll("[data-project-state]").forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.projectState === historyState.view));
    });
    if (historyState.view === "history") {
      await loadProjectHistory();
    }
  }

  function renderProjectHistoryShell() {
    const panel = $("projectHistoryPanel");
    if (!panel) return;
    panel.innerHTML = `
      <div class="rowflex" style="margin-bottom:10px;align-items:end">
        <label class="muted">操作<br>
          <select id="historyActionFilter">
            ${Object.entries(ACTION_LABELS).map(([value, label]) =>
              `<option value="${esc(value)}">${esc(label)}</option>`).join("")}
          </select>
        </label>
        <label class="muted">状态<br>
          <select id="historyStatusFilter">
            ${Object.entries(STATUS_LABELS).map(([value, label]) =>
              `<option value="${esc(value)}">${esc(label)}</option>`).join("")}
          </select>
        </label>
        <label class="muted">运行<br>
          <select id="historyRunFilter"><option value="">全部 run</option></select>
        </label>
        <label class="muted" style="flex:1 1 260px">搜索 source / path / key / reason<br>
          <input id="historyQuery" style="width:100%" placeholder="例如 Affirmative! / lc_messages / g2">
        </label>
        <button class="action secondary" id="historySearch" type="button">查询</button>
        <button class="action secondary" id="historyRefresh" type="button">刷新</button>
      </div>
      <div class="note">
        项目级 <b>Review Change History</b>：跨 run 展示 append-only ReviewDecisionLog 中的人工审查/操作记录。
        它不是 Provider、Planner、tm_source_history 等 TM 全生命周期事件流。
      </div>
      <div class="reviewgrid" style="margin-top:12px">
        <div>
          <div id="historySummary" class="rowflex" style="margin-bottom:8px"></div>
          <div class="queue" id="historyQueue"><div class="empty">切换到“变更历史”后载入</div></div>
        </div>
        <div id="historyDetail"><div class="empty">左侧选择一次变更</div></div>
      </div>`;

    $("historyActionFilter").value = historyState.action;
    $("historyStatusFilter").value = historyState.status;
    $("historyQuery").value = historyState.query;
    $("historySearch").addEventListener("click", () => loadProjectHistory(true));
    $("historyRefresh").addEventListener("click", () => loadProjectHistory(true));
    $("historyActionFilter").addEventListener("change", () => loadProjectHistory(true));
    $("historyStatusFilter").addEventListener("change", () => loadProjectHistory(true));
    $("historyRunFilter").addEventListener("change", () => loadProjectHistory(true));
    $("historyQuery").addEventListener("keydown", (event) => {
      if (event.key === "Enter") loadProjectHistory(true);
    });
  }

  function readHistoryFilters() {
    historyState.action = $("historyActionFilter")?.value || "all";
    historyState.status = $("historyStatusFilter")?.value || "all";
    historyState.runId = $("historyRunFilter")?.value || "";
    historyState.query = $("historyQuery")?.value.trim() || "";
  }

  function updateRunFilter(runIds) {
    const select = $("historyRunFilter");
    if (!select) return;
    const selected = historyState.runId;
    select.innerHTML = `<option value="">全部 run</option>` + (runIds || []).map((runId) =>
      `<option value="${esc(runId)}">${esc(runId)}</option>`).join("");
    if (selected && (runIds || []).includes(selected)) select.value = selected;
    else if (!selected) select.value = "";
  }

  async function loadProjectHistory(fromControls = false) {
    const queue = $("historyQueue");
    if (!queue) return;
    if (fromControls) readHistoryFilters();
    queue.innerHTML = `<div class="empty">载入项目变更历史…</div>`;
    $("historyDetail").innerHTML = `<div class="empty">载入中…</div>`;
    const params = new URLSearchParams({
      action: historyState.action,
      status: historyState.status,
      limit: "100",
      offset: "0",
    });
    if (historyState.runId) params.set("run_id", historyState.runId);
    if (historyState.query) params.set("q", historyState.query);
    try {
      const payload = await api(`/api/review/history?${params.toString()}`);
      historyState.payload = payload;
      historyState.rows = payload.operations || [];
      historyState.selected = 0;
      updateRunFilter(payload.run_ids || []);
      $("historySummary").innerHTML = `
        <span class="chip">匹配操作 <b>${num(payload.total)}</b></span>
        <span class="chip">当前页 <b>${num(historyState.rows.length)}</b></span>
        <span class="chip">日志 revision <b class="mono">${esc(payload.log_revision)}</b></span>`;
      queue.innerHTML = historyState.rows.length ? historyState.rows.map((op, index) => `
        <div class="qitem" data-history-index="${index}" ${index === 0 ? 'aria-selected="true"' : ""}>
          <div class="mono">${esc(op.decided_at)} · ${esc(op.action)}</div>
          <div class="var">${statusChip(op.status)} <span class="mono">${esc(op.run_id || "—")}</span></div>
          <div class="src">
            ${num(op.coordinate_count)} 个坐标
            ${op.current_count ? ` · 当前 ${num(op.current_count)}` : ""}
            ${op.reverted_count ? ` · 已撤销 ${num(op.reverted_count)}` : ""}
            ${op.superseded_count ? ` · 后续修改 ${num(op.superseded_count)}` : ""}
            ${op.revertible_count ? ` · <span class="chip ok">可安全撤销 ${num(op.revertible_count)}</span>` : ""}
          </div>
          <div class="src">${esc(op.reason || "未填写理由")}</div>
        </div>`).join("") : `<div class="empty">没有匹配的 Review 变更</div>`;
      queue.querySelectorAll("[data-history-index]").forEach((node) => {
        node.addEventListener("click", () => selectProjectHistory(Number(node.dataset.historyIndex)));
      });
      if (historyState.rows.length) selectProjectHistory(0);
      else $("historyDetail").innerHTML = `<div class="empty">没有可展示的变更</div>`;
    } catch (err) {
      queue.innerHTML = `<div class="empty">读取项目变更历史失败：${esc(err.message)}</div>`;
      $("historyDetail").innerHTML = `<div class="empty">请刷新后重试</div>`;
    }
  }

  function historyText(value, emptyLabel) {
    return value === null || value === undefined
      ? `<span class="muted">${esc(emptyLabel)}</span>`
      : `<span class="visible-text">${visibleBreaks(String(value))}</span>`;
  }

  function selectProjectHistory(index) {
    historyState.selected = index;
    document.querySelectorAll("[data-history-index]").forEach((node) =>
      node.setAttribute("aria-selected", String(Number(node.dataset.historyIndex) === index)));
    const operation = historyState.rows[index];
    if (operation) renderProjectHistoryDetail(operation);
  }

  function renderProjectHistoryDetail(operation) {
    const detail = $("historyDetail");
    const coordinates = operation.coordinates || [];
    const safe = coordinates.filter((item) => item.revertible).length;
    const actor = Object.entries(operation.actor || {}).map(([key, value]) =>
      `${esc(key)}=${esc(value)}`).join(" · ") || "—";
    const details = operation.details && Object.keys(operation.details).length
      ? `<details style="margin-top:10px"><summary>原始 details</summary><pre>${esc(JSON.stringify(operation.details, null, 2))}</pre></details>`
      : "";
    detail.innerHTML = `
      <h3 style="margin:0 0 10px;font-size:14px">项目变更 · ${esc(operation.action)}</h3>
      <dl class="kv">
        <dt>run_id</dt><dd class="mono">${esc(operation.run_id || "—")}</dd>
        <dt>audit_id</dt><dd class="mono">${esc(operation.audit_id)}</dd>
        <dt>时间</dt><dd class="mono">${esc(operation.decided_at)}</dd>
        <dt>状态</dt><dd>${statusChip(operation.status)}</dd>
        <dt>actor</dt><dd>${actor}</dd>
        <dt>理由</dt><dd>${esc(operation.reason || "—")}</dd>
        <dt>写入译文</dt><dd>${historyText(operation.translation, "多译法 / 无译文")}</dd>
        <dt>坐标</dt><dd>${num(operation.coordinate_count)} 条；可安全撤销 ${num(safe)} 条</dd>
      </dl>
      <div class="rowflex" style="margin:10px 0">
        <button class="secondary" id="historyOnlyRun" type="button">仅看此 run</button>
        ${safe ? '<button class="secondary" id="historySelectSafe" type="button">全选可安全撤销</button><button class="secondary" id="historyClear" type="button">清空选择</button><span class="chip" id="historySelected">已选 0 条</span>' : ""}
      </div>
      ${coordinates.length ? `<div class="scroll tall"><table><thead><tr>
        <th>选择</th><th>状态</th><th>资源 / 键</th><th>源文</th><th>修改前</th><th>本次写入</th><th>当前 TM</th>
      </tr></thead><tbody>
        ${coordinates.map((item) => `<tr>
          <td><input type="checkbox" data-history-decision="${esc(item.decision_id)}" ${item.revertible ? "" : "disabled"}></td>
          <td>${statusChip(item.status)}</td>
          <td class="mono">${esc(item.relative_path || "—")}<br>${esc(item.logical_key || item.stable_identity)}</td>
          <td>${historyText(item.source_text, "无源文")}</td>
          <td>${historyText(item.before_translation, "原无 TM 行")}</td>
          <td>${historyText(item.after_translation, "无译文")}</td>
          <td>${historyText(item.current_translation, "当前无 TM 行")}
            <div class="muted">${esc(item.current_origin || "—")} · ${esc(item.current_review_state || "—")}</div>
            ${item.revertible ? '<span class="chip ok">可撤销</span>' : ""}
            ${item.conflict_reason ? `<div class="muted">${esc(item.conflict_reason)}</div>` : ""}
          </td>
        </tr>`).join("")}
      </tbody></table></div>` : `<div class="note">这次 Review 决策没有 coordinate target；请查看 reason / details。</div>`}
      ${safe ? `<div class="rowflex" style="margin-top:10px">
        <input id="historyRevertReason" placeholder="撤销理由（必填）" style="flex:1 1 260px">
        <button class="action" id="historyApplyRevert" type="button">撤销所选坐标</button>
      </div>
      <div class="note">项目视图不新增撤销语义：仍调用同一个 safe_revert，并在提交时重新校验日志 revision、latest mutation 与当前 TM；任一 stale 坐标会使整批 409 fail-closed。</div>` : ""}
      ${details}`;

    $("historyOnlyRun").addEventListener("click", async () => {
      historyState.runId = operation.run_id || "";
      const select = $("historyRunFilter");
      if (select && Array.from(select.options).some((option) => option.value === historyState.runId)) {
        select.value = historyState.runId;
      }
      await loadProjectHistory();
    });

    if (!safe) return;
    const checkboxes = Array.from(document.querySelectorAll("[data-history-decision]"));
    const refreshSelected = () => {
      const count = checkboxes.filter((node) => node.checked).length;
      $("historySelected").textContent = `已选 ${count} 条`;
    };
    checkboxes.forEach((node) => node.addEventListener("change", refreshSelected));
    $("historySelectSafe").addEventListener("click", () => {
      checkboxes.forEach((node) => { if (!node.disabled) node.checked = true; });
      refreshSelected();
    });
    $("historyClear").addEventListener("click", () => {
      checkboxes.forEach((node) => { node.checked = false; });
      refreshSelected();
    });
    $("historyApplyRevert").addEventListener("click", async () => {
      const selected = checkboxes.filter((node) => node.checked).map((node) => node.dataset.historyDecision);
      const reason = $("historyRevertReason").value.trim();
      if (!selected.length) {
        window.alert("请先勾选要恢复的坐标。");
        return;
      }
      if (!reason) {
        window.alert("请填写撤销理由，它会进入 append-only Review 决策日志。");
        return;
      }
      if (!window.confirm(`确认恢复所选 ${selected.length} 个坐标的 before-image？`)) return;
      const button = $("historyApplyRevert");
      button.disabled = true;
      try {
        await postApi("/api/review/revert", {
          run_id: operation.run_id,
          decision_ids: selected,
          reason,
          expected_log_revision: historyState.payload.log_revision,
        });
        await loadProjectHistory();
      } catch (err) {
        button.disabled = false;
        window.alert(`撤销失败：${err.message}`);
      }
    });
  }

  // Keep the run-scoped Recovery view as a contextual shortcut, but make the
  // project-wide authority discoverable from there.
  const runScopedLoadReviewQueue = loadReviewQueue;
  loadReviewQueue = async function loadReviewQueueWithProjectHistoryLink() {
    const result = await runScopedLoadReviewQueue();
    if (review.view !== "recovery") return result;
    const keys = $("reviewKeys");
    if (keys && !$("openProjectHistory")) {
      keys.insertAdjacentHTML("beforeend", `
        <br><button class="secondary" id="openProjectHistory" type="button" style="margin-top:8px">查看项目全部变更</button>`);
      $("openProjectHistory").addEventListener("click", async () => {
        historyState.runId = "";
        const runFilter = $("historyRunFilter");
        if (runFilter) runFilter.value = "";
        await switchProjectState("history");
        $("projectStateSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
    return result;
  };

  // Variant switches change the Review log/TM pair behind the page.  Never leave a
  // project-history panel showing data from the previous variant.
  const baseSwitchVariant = switchVariant;
  switchVariant = async function switchVariantWithHistoryReset(...args) {
    const result = await baseSwitchVariant(...args);
    historyState.payload = null;
    historyState.rows = [];
    historyState.runId = "";
    if (historyState.view === "history") await loadProjectHistory();
    return result;
  };

  installProjectState();
})();
