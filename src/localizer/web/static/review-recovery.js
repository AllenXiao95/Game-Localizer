/* Review recovery extension.
 *
 * The main dashboard intentionally stays dependency-free and self-contained.  The
 * HTTP server inlines this repo-owned extension after index.html so the existing
 * Review globals/functions remain the single UI shell instead of creating a second
 * dashboard page.
 */
(() => {
  const originalReviewShell = reviewShell;
  reviewShell = function reviewShellWithRecovery() {
    const html = originalReviewShell();
    const marker = '<span class="grow"></span>';
    const selected = review.view === "recovery" ? ' aria-selected="true"' : "";
    const button = `<button class="secondary" data-rview="recovery"${selected}>变更历史 / Recovery</button>`;
    return html.includes(marker) ? html.replace(marker, `${button}${marker}`) : html;
  };

  const originalLoadReviewQueue = loadReviewQueue;
  loadReviewQueue = async function loadReviewQueueWithRecovery() {
    if (review.view !== "recovery") return originalLoadReviewQueue();
    return loadRecoveryQueue();
  };

  const originalSelectReviewRow = selectReviewRow;
  selectReviewRow = async function selectReviewRowWithRecovery(index) {
    if (review.view !== "recovery") return originalSelectReviewRow(index);
    review.selected = index;
    document.querySelectorAll("#reviewQueue .qitem").forEach((el) =>
      el.setAttribute("aria-selected", String(Number(el.dataset.idx) === index)));
    const operation = review.rows[index];
    if (operation) renderRecoveryDetail(operation);
  };

  async function loadRecoveryQueue() {
    const box = $("reviewQueue");
    if (!box) return;
    box.innerHTML = `<div class="empty">载入变更历史…</div>`;
    try {
      const payload = await reviewApi("recovery", { action: "unify", limit: 100 });
      review.rows = payload.operations || [];
      review.recoveryRevision = payload.log_revision;
      box.innerHTML = review.rows.length ? review.rows.map((op, i) => `
        <div class="qitem" data-idx="${i}" ${i === 0 ? 'aria-selected="true"' : ""}>
          <div class="mono">${esc(op.action)} · ${esc(op.decided_at)}</div>
          <div class="var">${esc(op.translation || "（多译法/无译文）")}</div>
          <div class="src">${num(op.coordinate_count)} 个坐标 · 可安全撤销 ${num(op.revertible_count)}
            ${op.conflict_count ? ` · <span class="chip err">冲突 ${num(op.conflict_count)}</span>` : ""}</div>
          <div class="src">${esc(op.reason || "未填写理由")}</div>
        </div>`).join("") : `<div class="empty">这个 run 没有可恢复的同源统一操作</div>`;
      $("reviewKeys").innerHTML = `
        仅展示本 run 的 <b>unify</b> TM 写入，并按一次 audit 聚合。<br>
        展开后按坐标勾选错误成员；任何已被后续人工修改的坐标都会标成冲突并禁止普通撤销。`;
      box.querySelectorAll(".qitem").forEach((el) =>
        el.addEventListener("click", () => selectReviewRow(Number(el.dataset.idx))));
      if (review.rows.length) await selectReviewRow(0);
      else $("reviewDetail").innerHTML = `<div class="empty">没有可恢复操作</div>`;
    } catch (err) {
      box.innerHTML = `<div class="empty">读取变更历史失败：${esc(err.message)}</div>`;
      $("reviewDetail").innerHTML = `<div class="empty">请刷新后重试</div>`;
    }
  }

  function recoveryText(value, emptyLabel) {
    return value === null || value === undefined
      ? `<span class="muted">${esc(emptyLabel)}</span>`
      : `<span class="visible-text">${visibleBreaks(String(value))}</span>`;
  }

  function renderRecoveryDetail(operation) {
    const safe = operation.coordinates.filter((item) => item.revertible).length;
    $("reviewDetail").innerHTML = `
      <h3 style="margin:0 0 10px;font-size:14px">变更恢复 · ${esc(operation.action)}</h3>
      <dl class="kv">
        <dt>audit_id</dt><dd class="mono">${esc(operation.audit_id)}</dd>
        <dt>时间</dt><dd class="mono">${esc(operation.decided_at)}</dd>
        <dt>理由</dt><dd>${esc(operation.reason || "—")}</dd>
        <dt>写入译文</dt><dd>${esc(operation.translation || "—")}</dd>
        <dt>坐标</dt><dd>${num(operation.coordinate_count)} 条；可安全撤销 ${num(safe)} 条</dd>
      </dl>
      <div class="note">
        这里恢复的是 <b>Review 决策的 before-image</b>，不是修改旧 run/QA 报告。
        撤销完成后仍应修正相关 glossary scope（如需要），再创建一个新的 run 做权威重判。
      </div>
      <div class="rowflex" style="margin:10px 0">
        <button class="secondary" id="recoverySelectSafe">全选可安全撤销</button>
        <button class="secondary" id="recoveryClear">清空选择</button>
        <span class="chip" id="recoverySelected">已选 0 条</span>
      </div>
      <div class="scroll tall"><table><thead><tr>
        <th>选择</th><th>资源 / 键</th><th>源文</th><th>修改前</th><th>本次写入</th><th>当前 TM</th>
      </tr></thead><tbody>
        ${operation.coordinates.map((item) => `<tr>
          <td><input type="checkbox" data-recovery-decision="${esc(item.decision_id)}"
            ${item.revertible ? "" : "disabled"}></td>
          <td class="mono">${esc(item.relative_path || "—")}<br>${esc(item.logical_key || item.stable_identity)}</td>
          <td>${recoveryText(item.source_text, "无源文")}</td>
          <td>${recoveryText(item.before_translation, "原无 TM 行")}</td>
          <td>${recoveryText(item.after_translation, "无译文")}</td>
          <td>${recoveryText(item.current_translation, "当前无 TM 行")}
            <div class="muted">${esc(item.current_origin || "—")} · ${esc(item.current_review_state || "—")}</div>
            ${item.revertible ? '<span class="chip ok">可撤销</span>'
              : `<span class="chip err" title="${esc(item.conflict_reason)}">已变化 / 冲突</span>`}
            ${item.conflict_reason ? `<div class="muted">${esc(item.conflict_reason)}</div>` : ""}
          </td>
        </tr>`).join("")}
      </tbody></table></div>
      <div class="rowflex" style="margin-top:10px">
        <input id="recoveryReason" placeholder="撤销理由（必填）" style="flex:1 1 260px">
        <button class="action" id="recoveryApply" ${safe ? "" : "disabled"}>撤销所选坐标</button>
      </div>
      <div class="note">批量撤销 fail-closed：提交时服务端会重新校验 freshness；只要所选中有一条后来又被人工/TM 修改过，整批返回 409，不会先撤一半。</div>`;

    const checkboxes = Array.from(document.querySelectorAll("[data-recovery-decision]"));
    const refreshSelected = () => {
      const count = checkboxes.filter((node) => node.checked).length;
      $("recoverySelected").textContent = `已选 ${count} 条`;
    };
    checkboxes.forEach((node) => node.addEventListener("change", refreshSelected));
    $("recoverySelectSafe").addEventListener("click", () => {
      checkboxes.forEach((node) => { if (!node.disabled) node.checked = true; });
      refreshSelected();
    });
    $("recoveryClear").addEventListener("click", () => {
      checkboxes.forEach((node) => { node.checked = false; });
      refreshSelected();
    });
    $("recoveryApply").addEventListener("click", async () => {
      const selected = checkboxes.filter((node) => node.checked).map((node) => node.dataset.recoveryDecision);
      const reason = $("recoveryReason").value.trim();
      if (!selected.length) {
        reviewNote("请先勾选要恢复的坐标。", "warn");
        return;
      }
      if (!reason) {
        reviewNote("请填写撤销理由 —— 它会进入 append-only Review 决策日志。", "warn");
        return;
      }
      if (!window.confirm(`确认恢复所选 ${selected.length} 个坐标的 before-image？`)) return;
      await runReviewAction(() => postApi("/api/review/revert", {
        run_id: selectedRun,
        decision_ids: selected,
        reason,
        expected_log_revision: review.session.log_revision,
      }));
    });
  }
})();
