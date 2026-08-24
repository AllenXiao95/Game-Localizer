(() => {
  "use strict";

  function selectedPublishTask() {
    if (!selectedRun) return null;
    return tasks.find((item) => item.kind === "publish" && item.run_id === selectedRun
      && ["queued", "running"].includes(item.status)) || null;
  }

  function targetResult(target) {
    const objects = target.objects || [];
    const skipped = objects.filter((item) => item.skipped).length;
    const uploaded = objects.length - skipped;
    const cls = target.status === "ok" || target.status === "succeeded" ? "ok" : "err";
    const reason = target.error_message
      ? `<div class="muted">${esc(target.error_message)}</div>` : "";
    const counts = objects.length
      ? `<span class="muted">上传 ${num(uploaded)} · 跳过 ${num(skipped)}</span>` : "";
    return `<div class="worker-card">
      <div class="title"><b>${esc(target.target || "target")}</b><span class="chip ${cls}">${esc(target.status || "unknown")}</span></div>
      <div>${esc(publishSummary(target))}</div>${counts}${reason}
    </div>`;
  }

  function receiptPanel() {
    const receipt = detail?.publish;
    if (!receipt?.available) return "";
    const passed = receipt.status === "completed" && receipt.passed === true;
    const running = ["queued", "running"].includes(receipt.status);
    const stateClass = passed ? "ok" : running ? "warn" : "err";
    const stateLabel = passed ? "发布完成" : running ? "发布进行中" : "发布未完全成功";
    const error = receipt.error?.message
      ? `<div class="note" style="border-color:var(--err)"><b>任务错误</b>：${esc(receipt.error.message)}</div>` : "";
    const targets = (receipt.targets || []).length
      ? `<div class="workergrid">${receipt.targets.map(targetResult).join("")}</div>`
      : `<div class="empty">本次回执没有 target 结果${receipt.error?.message ? "；请根据任务错误重试。" : "。"}</div>`;
    return `<div id="publishReceiptPanel" class="note" style="margin-top:12px;border-color:var(--${stateClass === "ok" ? "ok" : stateClass === "err" ? "err" : "warn"})">
      <div class="rowflex" style="align-items:center;margin-bottom:8px">
        <b>发布回执</b><span class="chip ${stateClass}">${stateLabel}</span>
        ${receipt.task_id ? `<span class="chip mono">${esc(receipt.task_id)}</span>` : ""}
      </div>
      ${targets}${error}
      <dl class="kv" style="margin-top:8px">
        <dt>开始</dt><dd class="mono">${esc(receipt.started_at || "—")}</dd>
        <dt>完成</dt><dd class="mono">${esc(receipt.finished_at || "—")}</dd>
        <dt>持久化回执</dt><dd class="mono">${esc(receipt.source || "—")}</dd>
      </dl>
    </div>`;
  }

  function decoratePublish() {
    if (activeTab !== "artifact" || !detail) return;
    const button = $("publishRelease"), message = $("publishMessage");
    if (!button || !message) return;

    const previous = $("publishReceiptPanel");
    if (previous) previous.remove();
    const html = receiptPanel();
    if (html) button.closest(".rowflex")?.insertAdjacentHTML("afterend", html);

    const active = selectedPublishTask();
    const receipt = detail.publish;
    if (active) {
      button.disabled = true;
      button.classList.remove("secondary");
      button.textContent = "发布任务执行中";
      button.title = "当前 run 已有发布任务 queued/running，禁止重复提交。";
      message.className = "chip warn";
      message.textContent = `发布任务 ${active.task_id.slice(0, 8)} 正在执行；等待 run 级回执。`;
      return;
    }

    if (receipt?.available && receipt.status === "completed" && receipt.passed === true) {
      button.disabled = false;
      button.classList.add("secondary");
      button.textContent = "重新发布（幂等）";
      button.title = "当前 run 已发布成功；重复发布仍会执行目标级幂等检查。";
      message.className = "chip ok";
      message.textContent = "主工作流已完成；如需补发，可显式重新发布。";
      return;
    }

    if (receipt?.available && !(receipt.status === "completed" && receipt.passed === true)) {
      button.disabled = !(detail.artifact?.quality_gate_passed && detail.artifact?.exists);
      button.classList.remove("secondary");
      button.textContent = "重试发布到已配置目标";
      button.title = "上次发布未完全成功；重试会生成新的 run 级回执。";
      message.className = "chip err";
      message.textContent = "上次发布未完全成功；查看下方 target 原因后再重试。";
    }
  }

  const previousRenderTab = renderTab;
  renderTab = async function() {
    const result = await previousRenderTab();
    decoratePublish();
    return result;
  };

  const previousTaskStatus = renderTaskStatus;
  renderTaskStatus = function() {
    previousTaskStatus();
    decoratePublish();
  };

  document.addEventListener("localizer:locale-changed", () => setTimeout(decoratePublish, 0));
  decoratePublish();
  window.LocalizerWorkflowPublishUX = { decorate: decoratePublish, receiptPanel };
})();
