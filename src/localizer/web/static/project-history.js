/* Project-level Review change history with paged coordinate inspection. */
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
    coordinate: {
      query: "",
      status: "all",
      recovery: "all",
      offset: 0,
      limit: 100,
      payload: null,
      selectedIds: new Set(),
    },
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
  const COORDINATE_STATUS_LABELS = {
    all: "全部 coordinate 状态",
    current: "当前仍生效",
    superseded: "已被后续修改",
    reverted: "已撤销",
    recorded: "仅记录",
  };

  function statusChip(status) {
    if (status === "current") return `<span class="chip ok">当前仍生效</span>`;
    if (status === "reverted") return `<span class="chip">已撤销</span>`;
    if (status === "superseded") return `<span class="chip warn">已被后续修改</span>`;
    if (status === "mixed") return `<span class="chip warn">混合状态</span>`;
    return `<span class="chip">仅记录</span>`;
  }
  function historyText(value, emptyLabel) {
    return value === null || value === undefined
      ? `<span class="muted">${esc(emptyLabel)}</span>`
      : `<span class="visible-text">${visibleBreaks(String(value))}</span>`;
  }
  function recoveryProofLabel(value) {
    if (value === "after_image") return "after-image";
    if (value === "before_image") return "before-image";
    if (value === "review_index") return "ReviewIndex";
    if (value === "missing_evidence") return "证据不足";
    return value || "未知证据";
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
    const tmPanel = $("tmPanel"), historyPanel = $("projectHistoryPanel");
    if (!tmPanel || !historyPanel) return;
    tmPanel.hidden = historyState.view !== "tm";
    historyPanel.hidden = historyState.view !== "history";
    document.querySelectorAll("[data-project-state]").forEach((button) =>
      button.setAttribute("aria-selected", String(button.dataset.projectState === historyState.view)));
    if (historyState.view === "history") await loadProjectHistory();
  }

  function renderProjectHistoryShell() {
    const panel = $("projectHistoryPanel");
    if (!panel) return;
    panel.innerHTML = `
      <div class="rowflex" style="margin-bottom:10px;align-items:end">
        <label class="muted">操作<br><select id="historyActionFilter">
          ${Object.entries(ACTION_LABELS).map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}
        </select></label>
        <label class="muted">状态<br><select id="historyStatusFilter">
          ${Object.entries(STATUS_LABELS).map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}
        </select></label>
        <label class="muted">运行<br><select id="historyRunFilter"><option value="">全部 run</option></select></label>
        <label class="muted" style="flex:1 1 260px">搜索操作上下文<br>
          <input id="historyQuery" style="width:100%" placeholder="source / path / key / reason"></label>
        <button class="action secondary" id="historySearch" type="button">查询</button>
        <button class="action secondary" id="historyRefresh" type="button">刷新</button>
      </div>
      <div class="note">项目级 <b>Review Change History</b>：跨 run 展示 append-only ReviewDecisionLog。
        大型 audit 点开后使用 coordinate 级二次搜索与分页；这里不是完整 TM 生命周期事件流。</div>
      <div class="reviewgrid" style="margin-top:12px">
        <div><div id="historySummary" class="rowflex" style="margin-bottom:8px"></div>
          <div class="queue" id="historyQueue"><div class="empty">切换到“变更历史”后载入</div></div></div>
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
    $("historyQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") loadProjectHistory(true); });
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
    const params = new URLSearchParams({ action: historyState.action, status: historyState.status, limit: "100", offset: "0" });
    if (historyState.runId) params.set("run_id", historyState.runId);
    if (historyState.query) params.set("q", historyState.query);
    try {
      const payload = await api(`/api/review/history?${params.toString()}`);
      historyState.payload = payload;
      historyState.rows = payload.operations || [];
      historyState.selected = 0;
      historyState.coordinate.selectedIds.clear();
      updateRunFilter(payload.run_ids || []);
      $("historySummary").innerHTML = `<span class="chip">匹配操作 <b>${num(payload.total)}</b></span>
        <span class="chip">当前页 <b>${num(historyState.rows.length)}</b></span>
        <span class="chip">日志 revision <b class="mono">${esc(payload.log_revision)}</b></span>`;
      queue.innerHTML = historyState.rows.length ? historyState.rows.map((op, index) => `
        <div class="qitem" data-history-index="${index}" ${index === 0 ? 'aria-selected="true"' : ""}>
          <div class="mono">${esc(op.decided_at)} · ${esc(op.action)}</div>
          <div class="var">${statusChip(op.status)} <span class="mono">${esc(op.run_id || "—")}</span></div>
          <div class="src">${num(op.coordinate_count)} 个坐标
            ${op.current_count ? ` · 当前 ${num(op.current_count)}` : ""}
            ${op.reverted_count ? ` · 已撤销 ${num(op.reverted_count)}` : ""}
            ${op.superseded_count ? ` · 后续修改 ${num(op.superseded_count)}` : ""}</div>
          <div class="src">${esc(op.reason || "未填写理由")}</div>
        </div>`).join("") : `<div class="empty">没有匹配的 Review 变更</div>`;
      queue.querySelectorAll("[data-history-index]").forEach((node) =>
        node.addEventListener("click", () => selectProjectHistory(Number(node.dataset.historyIndex))));
      if (historyState.rows.length) selectProjectHistory(0);
      else $("historyDetail").innerHTML = `<div class="empty">没有可展示的变更</div>`;
    } catch (err) {
      queue.innerHTML = `<div class="empty">读取项目变更历史失败：${esc(err.message)}</div>`;
      $("historyDetail").innerHTML = `<div class="empty">请刷新后重试</div>`;
    }
  }

  function selectProjectHistory(index) {
    historyState.selected = index;
    historyState.coordinate.query = "";
    historyState.coordinate.status = "all";
    historyState.coordinate.recovery = "all";
    historyState.coordinate.offset = 0;
    historyState.coordinate.selectedIds.clear();
    document.querySelectorAll("[data-history-index]").forEach((node) =>
      node.setAttribute("aria-selected", String(Number(node.dataset.historyIndex) === index)));
    const operation = historyState.rows[index];
    if (operation) renderProjectHistoryDetail(operation);
  }

  function renderProjectHistoryDetail(operation) {
    const detail = $("historyDetail");
    const actor = Object.entries(operation.actor || {}).map(([key, value]) => `${esc(key)}=${esc(value)}`).join(" · ") || "—";
    const details = operation.details && Object.keys(operation.details).length
      ? `<details style="margin-top:10px"><summary>原始 details</summary><pre>${esc(JSON.stringify(operation.details, null, 2))}</pre></details>` : "";
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
        <dt>坐标</dt><dd>${num(operation.coordinate_count)} 条；请在下方按 coordinate 筛选定位</dd>
      </dl>
      <div class="rowflex" style="margin:10px 0"><button class="secondary" id="historyOnlyRun" type="button">仅看此 run</button></div>
      <div class="note">大型 audit 不再一次性渲染全部 coordinate。搜索会匹配 path / key / source / stable_identity / 修改前 / 本次写入 / 当前译文 / 冲突原因。</div>
      <div class="rowflex" style="margin:10px 0;align-items:end">
        <label class="muted" style="flex:1 1 300px">coordinate 搜索<br><input id="historyCoordQuery" style="width:100%" placeholder="例如 radio / lc_messages / 收到！"></label>
        <label class="muted">状态<br><select id="historyCoordStatus">
          ${Object.entries(COORDINATE_STATUS_LABELS).map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}
        </select></label>
        <label class="muted">可恢复性<br><select id="historyCoordRecovery">
          <option value="all">全部</option><option value="revertible">仅可安全撤销</option><option value="blocked">仅不可撤销/冲突</option>
        </select></label>
        <label class="muted">每页<br><select id="historyCoordLimit"><option>50</option><option selected>100</option><option>200</option></select></label>
        <button class="action secondary" id="historyCoordSearch" type="button">筛选</button>
      </div>
      <div id="historyCoordSummary" class="rowflex" style="margin-bottom:8px"></div>
      <div class="rowflex" style="margin-bottom:8px">
        <button class="secondary" id="historyCoordPrev" type="button">上一页</button>
        <button class="secondary" id="historyCoordNext" type="button">下一页</button>
        <button class="secondary" id="historySelectPageSafe" type="button">全选本页可安全撤销</button>
        <button class="secondary" id="historyClearSelected" type="button">清空累计选择</button>
        <span class="chip" id="historySelected">累计已选 0 条 / 最多 500</span>
      </div>
      <div class="scroll tall"><table><thead><tr>
        <th>选择</th><th>状态</th><th>资源 / 键</th><th>源文</th><th>修改前</th><th>本次写入</th><th>当前 TM / 可恢复性</th>
      </tr></thead><tbody id="historyCoordRows"><tr><td colspan="7" class="empty">载入 coordinate…</td></tr></tbody></table></div>
      <div class="rowflex" style="margin-top:10px">
        <input id="historyRevertReason" placeholder="撤销理由（必填）" style="flex:1 1 260px">
        <button class="action" id="historyApplyRevert" type="button" disabled>撤销累计所选坐标</button>
      </div>
      <div id="historyRecoveryNote" class="note">撤销始终 fail-closed；新事件优先直接校验记录的 after-image，旧事件才回退到 ReviewIndex / before-image compatibility proof。</div>
      ${details}`;

    $("historyOnlyRun").addEventListener("click", async () => {
      historyState.runId = operation.run_id || "";
      const select = $("historyRunFilter");
      if (select && Array.from(select.options).some((option) => option.value === historyState.runId)) select.value = historyState.runId;
      await loadProjectHistory();
    });
    $("historyCoordQuery").value = historyState.coordinate.query;
    $("historyCoordStatus").value = historyState.coordinate.status;
    $("historyCoordRecovery").value = historyState.coordinate.recovery;
    $("historyCoordLimit").value = String(historyState.coordinate.limit);
    $("historyCoordSearch").addEventListener("click", () => {
      historyState.coordinate.query = $("historyCoordQuery").value.trim();
      historyState.coordinate.status = $("historyCoordStatus").value;
      historyState.coordinate.recovery = $("historyCoordRecovery").value;
      historyState.coordinate.limit = Number($("historyCoordLimit").value) || 100;
      historyState.coordinate.offset = 0;
      loadOperationCoordinates(operation);
    });
    $("historyCoordQuery").addEventListener("keydown", (event) => { if (event.key === "Enter") $("historyCoordSearch").click(); });
    $("historyCoordPrev").addEventListener("click", () => {
      historyState.coordinate.offset = Math.max(0, historyState.coordinate.offset - historyState.coordinate.limit);
      loadOperationCoordinates(operation);
    });
    $("historyCoordNext").addEventListener("click", () => {
      historyState.coordinate.offset += historyState.coordinate.limit;
      loadOperationCoordinates(operation);
    });
    $("historyClearSelected").addEventListener("click", () => {
      historyState.coordinate.selectedIds.clear();
      refreshCoordinateSelection();
      document.querySelectorAll("[data-history-decision]").forEach((node) => { node.checked = false; });
    });
    $("historySelectPageSafe").addEventListener("click", () => {
      document.querySelectorAll("[data-history-decision]").forEach((node) => {
        if (!node.disabled && historyState.coordinate.selectedIds.size < 500) {
          node.checked = true;
          historyState.coordinate.selectedIds.add(node.dataset.historyDecision);
        }
      });
      refreshCoordinateSelection();
    });
    $("historyApplyRevert").addEventListener("click", async () => {
      const selected = Array.from(historyState.coordinate.selectedIds);
      const reason = $("historyRevertReason").value.trim();
      if (!selected.length) return window.alert("请先勾选要恢复的坐标。");
      if (!reason) return window.alert("请填写撤销理由，它会进入 append-only Review 决策日志。");
      if (!window.confirm(`确认恢复累计选择的 ${selected.length} 个坐标 before-image？`)) return;
      const button = $("historyApplyRevert"); button.disabled = true;
      try {
        await postApi("/api/review/revert", {
          run_id: operation.run_id,
          decision_ids: selected,
          reason,
          expected_log_revision: historyState.coordinate.payload?.log_revision || historyState.payload?.log_revision,
        });
        historyState.coordinate.selectedIds.clear();
        await loadProjectHistory();
      } catch (err) {
        button.disabled = false;
        window.alert(`撤销失败：${err.message}`);
      }
    });
    loadOperationCoordinates(operation);
  }

  async function loadOperationCoordinates(operation) {
    const body = $("historyCoordRows");
    if (!body) return;
    body.innerHTML = `<tr><td colspan="7" class="empty">载入 coordinate…</td></tr>`;
    const c = historyState.coordinate;
    const params = new URLSearchParams({
      run_id: operation.run_id,
      action: operation.action,
      audit_id: operation.audit_id,
      status: c.status,
      recovery: c.recovery,
      limit: String(c.limit),
      offset: String(c.offset),
    });
    if (c.query) params.set("q", c.query);
    try {
      const payload = await api(`/api/review/history/coordinates?${params.toString()}`);
      c.payload = payload;
      if (c.offset >= payload.total && c.offset > 0) {
        c.offset = Math.max(0, payload.total - (payload.total % c.limit || c.limit));
        return loadOperationCoordinates(operation);
      }
      const counts = payload.status_counts || {}, proofs = payload.proof_counts || {};
      $("historyCoordSummary").innerHTML = `
        <span class="chip">筛选命中 <b>${num(payload.total)}</b></span>
        <span class="chip">当前 ${num(counts.current || 0)}</span>
        <span class="chip">后续修改 ${num(counts.superseded || 0)}</span>
        <span class="chip">已撤销 ${num(counts.reverted || 0)}</span>
        <span class="chip ok">整次操作可安全撤销 ${num(payload.operation_revertible_total || 0)}</span>
        <span class="chip">页 ${num(Math.floor(c.offset / c.limit) + 1)} / ${num(Math.max(1, Math.ceil(payload.total / c.limit)))}</span>`;
      $("historyCoordPrev").disabled = c.offset <= 0;
      $("historyCoordNext").disabled = c.offset + c.limit >= payload.total;
      const afterProof = proofs.after_image || 0,
        beforeProof = proofs.before_image || 0,
        indexProof = proofs.review_index || 0,
        missing = proofs.missing_evidence || 0;
      $("historyRecoveryNote").innerHTML = `恢复证据：after-image <b>${num(afterProof)}</b> · ReviewIndex <b>${num(indexProof)}</b> · before-image fallback <b>${num(beforeProof)}</b> · 证据不足 <b>${num(missing)}</b>。<br>
        ${afterProof ? "新事件直接按 append-only 日志记录的 after-image 校验 freshness；旧事件继续使用兼容证据链。" : (payload.run_index_available ? "旧事件的历史 ReviewIndex 仍可用；按 run sidecar 校验。" : "历史 ReviewIndex 已不可用；旧事件仅 source/coordinate 元数据与完整 before-image 一致时允许撤销。")}`;
      const rows = payload.coordinates || [];
      body.innerHTML = rows.length ? rows.map((item) => `<tr>
        <td><input type="checkbox" data-history-decision="${esc(item.decision_id)}" ${item.revertible ? "" : "disabled"}
          ${historyState.coordinate.selectedIds.has(item.decision_id) ? "checked" : ""}></td>
        <td>${statusChip(item.status)}</td>
        <td class="mono">${esc(item.relative_path || "—")}<br>${esc(item.logical_key || item.stable_identity)}</td>
        <td>${historyText(item.source_text, "无源文")}</td>
        <td>${historyText(item.before_translation, "原无 TM 行")}</td>
        <td>${historyText(item.after_translation, "无译文")}</td>
        <td>${historyText(item.current_translation, "当前无 TM 行")}
          <div class="muted">${esc(item.current_origin || "—")} · ${esc(item.current_review_state || "—")}</div>
          ${item.revertible ? `<span class="chip ok">可撤销 · ${esc(recoveryProofLabel(item.recovery_proof))}</span>` : '<span class="chip warn">不可撤销</span>'}
          ${item.conflict_reason ? `<div class="muted">${esc(item.conflict_reason)}</div>` : ""}</td>
      </tr>`).join("") : `<tr><td colspan="7" class="empty">当前筛选没有 coordinate</td></tr>`;
      document.querySelectorAll("[data-history-decision]").forEach((node) => node.addEventListener("change", () => {
        const id = node.dataset.historyDecision;
        if (node.checked) {
          if (c.selectedIds.size >= 500) { node.checked = false; return window.alert("一次最多选择 500 条；请先撤销当前选择。 "); }
          c.selectedIds.add(id);
        } else c.selectedIds.delete(id);
        refreshCoordinateSelection();
      }));
      refreshCoordinateSelection();
    } catch (err) {
      body.innerHTML = `<tr><td colspan="7" class="empty">读取 coordinate 失败：${esc(err.message)}</td></tr>`;
    }
  }

  function refreshCoordinateSelection() {
    const count = historyState.coordinate.selectedIds.size;
    if ($("historySelected")) $("historySelected").textContent = `累计已选 ${count} 条 / 最多 500`;
    if ($("historyApplyRevert")) $("historyApplyRevert").disabled = count === 0;
  }

  const runScopedLoadReviewQueue = loadReviewQueue;
  loadReviewQueue = async function loadReviewQueueWithProjectHistoryLink() {
    const result = await runScopedLoadReviewQueue();
    if (review.view !== "recovery") return result;
    const keys = $("reviewKeys");
    if (keys && !$("openProjectHistory")) {
      keys.insertAdjacentHTML("beforeend", `<br><button class="secondary" id="openProjectHistory" type="button" style="margin-top:8px">查看项目全部变更</button>`);
      $("openProjectHistory").addEventListener("click", async () => {
        historyState.runId = "";
        const runFilter = $("historyRunFilter"); if (runFilter) runFilter.value = "";
        await switchProjectState("history");
        $("projectStateSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    }
    return result;
  };

  const baseSwitchVariant = switchVariant;
  switchVariant = async function switchVariantWithHistoryReset(...args) {
    const result = await baseSwitchVariant(...args);
    historyState.payload = null; historyState.rows = []; historyState.runId = "";
    historyState.coordinate.payload = null; historyState.coordinate.selectedIds.clear();
    if (historyState.view === "history") await loadProjectHistory();
    return result;
  };

  installProjectState();
})();
