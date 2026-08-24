(() => {
  "use strict";

  const api = window.LocalizerI18n;
  if (!api) return;

  // Long-form operator copy is kept separate from the core locale runtime.  This makes the
  // translation layer reviewable: i18n.js owns locale/state mechanics, while this file covers
  // prose-heavy workflow guidance and repair explanations that would otherwise leave mixed CJK
  // text after switching to English.
  const PHRASES = [
    ["任务之间串行执行；单个任务会按 provider.concurrency 并行翻译不同资源文件，正式 TM 仍由主线程统一提交。预设不保存 API Token 或压缩密码；release 的 AES 密码从 project.yaml 指定的环境变量读取。release 构建通过后，可在“制品”页显式发布到全部已配置 target。",
      "Tasks run serially. Within one task, provider.concurrency may translate different resource files in parallel, while formal TM writes remain serialized on the main thread. Presets never store API tokens or archive passwords; release AES passwords come from the environment variable referenced by project.yaml. After a release build passes, publish explicitly from the Artifact tab."],
    ["面板提供受控的本地任务启动，以及针对 QA 报告已识别问题的", "The dashboard provides controlled local task execution and"],
    ["（编辑落本地 TM 并留审计）。它不组织人工审核流程：没有审核队列、任务分发或多人审批；",
      "(edits are committed to the local TM with an audit trail). It does not orchestrate a human review workflow: there is no review queue, task assignment, or multi-person approval;"],
    ["批量人工翻译与校对统一在 ParaTranz（M5）完成。此处不提供", "Bulk human translation and proofreading remain in ParaTranz (M5). This dashboard does not provide a"],
    ["stage 变更入口；人工翻译与校对统一在 ParaTranz（M5）完成。", "stage mutation entry point; human translation and proofreading remain in ParaTranz (M5)."],
    ["阶段状态由磁盘产物反推，不是运行态权威状态。", "Stage status is inferred from persisted artifacts and is not the runtime source of truth."],
    ["继续前会备份 SQLite TM，再退休这些旧记录；新译文仍由本次资源和 checkpoint 接管。",
      "Before continuing, the SQLite TM is backed up and these stale entries are retired; new translations remain owned by the current resources and checkpoint."],
    ["正式启动时会重新扫描并校验该指纹。", "The fingerprint is rescanned and validated again when the task actually starts."],
    ["无需备份", "No backup required"],
    ["暂无任务记录", "No task history"],
    ["上传", "Uploaded"],
    ["暂无数据", "No data yet"],
    ["父运行", "Parent run"],
    ["这是游戏资源更新后的源文漂移，不是 QA 仍在执行。确认后会先备份 TM、退休旧记录，",
      "This is source drift caused by a game resource update, not QA still running. After confirmation the TM is backed up and stale entries are retired,"],
    ["再恢复同一 run_id；checkpoint 中已成功的译文不会重新请求 Provider。",
      "then the same run_id is resumed; translations already successful in the checkpoint are not requested from the provider again."],
    ["无法生成 formal 确认清单", "Unable to build the formal-entry confirmation list"],
    ["已退休", "Retired"],
    ["面板提供 QA 缺陷的", "The dashboard provides"],
    ["：编辑落本地 TM 并留完整审计。", ": edits are committed to the local TM with a complete audit trail."],
    ["它不组织人工审核流程 —— 没有审核队列、任务分发或多人审批；",
      "It does not orchestrate a human review workflow — there is no review queue, task assignment, or multi-person approval;"],
    ["：创建新的不可变子运行；父运行没有 checkpoint 时自动沿谱系", ": creates a new immutable child run; if the parent has no checkpoint, it follows lineage automatically"],
    ["回溯最近的成功机器结果，", "to the most recent successful machine result,"],
    ["只重试仍未解决或源文已变化的词条，并重新执行完整 QualityGate。",
      "retries only unresolved units or units whose source changed, then runs the complete QualityGate again."],
    ["例如 1.44.0.2，不要填写前导 v", "e.g. 1.44.0.2; do not add a leading v"],
    ["目标版本默认继承父运行但可在本次重建中修改；源路径与 .env 仍从父运行快照继承。版本变化只影响制品、Manifest、tag 和上传目录，不会让已安全复用的词条重新调用 Provider。",
      "The target version defaults to the parent run but may be changed for this rebuild. Source path and .env still inherit from the parent snapshot. A version change affects only the artifact, Manifest, tag, and upload path; safely reused units do not call the provider again."],
    ["从左侧选一条", "Select an item on the left"],
    ["正在创建子运行…", "Creating child run…"],
    ["本队列把", "This queue condenses"],
    ["条违规压成", "violations into"],
    ["组：有唯一最高频译法", "groups: unique top-frequency translation"],
    ["归一化可坍缩", "normalization collapses"],
    ["其余", "remaining"],
    ["组需逐条判断。", "groups require case-by-case review."],
    ["处理全部未解决组：非空译文中出现次数唯一最高者胜出，不设比例门槛；空译文一并补齐，并列第一的组跳过。",
      "Processes all unresolved groups: the uniquely most frequent non-empty translation wins with no ratio threshold; empty translations are filled too; tied leaders are skipped."],
    ["上下", "up/down"],
    ["选译法", "choose variant"],
    ["没有这类问题", "No issues of this type"],
    ["已修复的排在列表底部。", "Repaired items are sorted to the bottom."],
    ["本地重新校验不调用模型，也不产生费用。", "Local revalidation does not call the model and incurs no model cost."],
    ["请填写批量同步理由 —— 每个组的决策都会进入审计日志。", "Provide a bulk-sync reason — every group decision is written to the audit log."],
    ["个可判定组", "eligible groups"],
    ["写入", "wrote"],
    ["个坐标", "coordinates"],
    ["并列跳过", "tied groups skipped"],
    ["全部", "All"],
    ["无", "None"],
    ["条，分布在", "items across"],
    ["这是 human + reviewed 的术语，受 G01 绝对保护；下方排除操作会自动走备份、diff 与审计维护路径。",
      "This is a human + reviewed glossary term protected by G01. Exclusion below automatically follows the backup, diff, and audit-maintenance path."],
    ["改术语会改变全部违规的判据基准，改完只对本术语覆盖的词条重判。",
      "Changing the term changes the criterion for all violations; after editing, only units covered by this term are rechecked."],
    ["多义词误报的正解是给术语加", "For polysemy false positives, add"],
    ["不是", "Do not"],
    ["把正确的译文改错 —— 实测这类误报占术语违规的 23%。", "change a correct translation to satisfy the rule — measured false positives of this type account for 23% of glossary violations."],
    ["：下方回显的是当前有效译文（已人工落表的优先于本次运行原值）。",
      ": the table below shows the currently effective translation (human-committed values take precedence over the original value from this run)."],
    ["“本地重判”只检查；“落表”才会把编辑写入 TM 并进入决策日志。",
      "“Local recheck” only validates; “Commit” writes edits to the TM and decision log."],
    ["单次最多落表 100 条；提交前服务端会重新校验，并保留远端锁定译文。",
      "At most 100 items can be committed at once. The server revalidates before commit and preserves remotely locked translations."],
    ["填写相对于源目录的文件路径或 glob。建议先用精确文件名；", "Enter a file path or glob relative to the source directory. Prefer an exact filename first;"],
    ["面板拒绝", "The dashboard rejects"],
    ["等全局排除。", "and other global exclusions."],
    ["例如 comp7.mo 或 ui/ranks/**", "e.g. comp7.mo or ui/ranks/**"],
    ["只修改当前术语的 exclude_scope；旧 QA 报告保持不变，需增量重建后才有权威结论。",
      "Only this term's exclude_scope is changed. The old QA report stays unchanged; an incremental rebuild is required for an authoritative result."],
    ["请填写修改理由 —— 它会进入决策日志。", "Provide a change reason — it will be written to the decision log."],
    ["这一组已统一为", "This group has been unified to"],
    ["个成员全部落表。再次统一会覆盖它。", "members are fully committed. Unifying again will overwrite it."],
    ["只有", "Only"],
    ["个成员落了表 ——", "members were committed —"],
    ["剩下的下一次运行会被重译出新的译法。请重新统一。", "the remaining members may be translated differently in the next run. Unify the group again."],
    ["统一会为组内", "Unifying writes one row for all"],
    ["各写一行", "members in the group"],
    ["（含空译文成员 —— 它不在 QA 记录里，漏掉就只统一一半）", "(including empty-translation members, which are not present in the QA record; omitting them would only unify part of the group)"],
    ["只写一条指望同源传播在真机上不成立。", "Writing one row and relying on same-source propagation does not work on real data."],
    ["请填写定稿理由 —— 它会进决策日志。", "Provide a decision reason — it will be written to the decision log."],
    ["源文（占位符已高亮；", "Source (placeholders highlighted;"],
    ["这条已经落表。下面回显的是", "This item is already committed. The field below shows"],
    ["你定稿的译文", "your committed translation"],
    ["，不是运行时的原值。", ", not the original runtime value."],
    ["注意：规则改写了译文，实际入库的是", "Note: rules rewrote the translation; the value actually stored is"],
    ["quarantined 不参与命中、不进 Prompt 参考、不参与全局收敛（§12.4）。",
      "quarantined entries do not participate in matching, Prompt references, or global convergence (§12.4)."],
    ["例如 1.44.0.0", "e.g. 1.44.0.0"],
    ["例如 WOT RU 正式包", "e.g. WOT RU release package"],
    ["自动生成", "Generated automatically"],
    ["或单个 .mo", "or a single .mo"],
    ["正在备份并重新校验候选", "Backing up and revalidating candidates"],
    ["正式启动", "Start task"],
    ["发布目标", "publish targets"]
  ].sort((a, b) => b[0].length - a[0].length);

  const PATTERNS = [
    [/当前\s+(.+?)\s+暂无任务记录。/g, "No task history for $1."],
    [/已退休\s*([\d,.]+)\s*条；备份：/g, "Retired $1 items; backup:"],
    [/完成\s*([\d,.]+)\/([\d,.]+)\s*个可判定组/g, "completed $1/$2 eligible groups"],
    [/写入\s*([\d,.]+)\/([\d,.]+)\s*个坐标/g, "wrote $1/$2 coordinates"],
    [/并列跳过\s*([\d,.]+)\s*组/g, "$1 tied groups skipped"],
    [/已修复\s*([\d,.]+)\/([\d,.]+)\s*条/g, "Repaired $1/$2 items"],
    [/违规\s*([\d,.]+)\s*条，分布在\s*([\d,.]+)\s*个文件/g, "$1 violations across $2 files"],
    [/只有\s*([\d,.]+)\/([\d,.]+)\s*个成员落了表/g, "Only $1/$2 members were committed"],
    [/已统一为\s*(.+?)，\s*([\d,.]+)\s*个成员全部落表/g, "unified to $1; all $2 members committed"],
    [/重判\s*([\d,.]+)\s*条，仍违规\s*([\d,.]+)\s*条/g, "Rechecked $1 items; $2 still violate"],
    [/已落表\s*([\d,.]+)\s*条/g, "Committed $1 items"],
    [/只写入\s*([\d,.]+)\/([\d,.]+)\s*条/g, "Only wrote $1/$2 items"],
    [/换行数量：源文\s*([\d,.]+)\s*·\s*译文\s*([\d,.]+)/g, "Line breaks: source $1 · translation $2"]
  ];

  function translateText(text) {
    let value = String(text || "");
    for (const [zh, en] of PHRASES) value = value.split(zh).join(en);
    for (const [pattern, replacement] of PATTERNS) value = value.replace(pattern, replacement);
    return value;
  }

  function skip(node) {
    const parent = node.parentElement;
    if (!parent) return true;
    return Boolean(parent.closest("#localeToggle,script,style,pre,code,textarea,.mono,[data-i18n-raw]"));
  }

  function translateNode(node) {
    if (api.locale() !== "en-US" || skip(node)) return;
    const current = node.nodeValue || "";
    if (!current.trim()) return;
    const translated = translateText(current);
    if (translated !== current) node.nodeValue = translated;
  }

  function translateAttributes(element) {
    if (api.locale() !== "en-US" || !(element instanceof Element) || element.id === "localeToggle") return;
    for (const attr of ["placeholder", "title", "aria-label"]) {
      if (!element.hasAttribute(attr)) continue;
      const current = element.getAttribute(attr) || "";
      const translated = translateText(current);
      if (translated !== current) element.setAttribute(attr, translated);
    }
  }

  function translateTree(root) {
    if (api.locale() !== "en-US" || !root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      translateNode(root);
      return;
    }
    if (!(root instanceof Element) && root !== document) return;
    if (root instanceof Element) translateAttributes(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) translateNode(node);
    if (root.querySelectorAll) {
      root.querySelectorAll("[placeholder],[title],[aria-label]").forEach(translateAttributes);
    }
  }

  translateTree(document);
  document.addEventListener("localizer:locale-changed", () => translateTree(document));

  const observer = new MutationObserver((mutations) => {
    if (api.locale() !== "en-US") return;
    for (const mutation of mutations) {
      if (mutation.type === "characterData") translateNode(mutation.target);
      else if (mutation.type === "attributes") translateAttributes(mutation.target);
      else mutation.addedNodes.forEach(translateTree);
    }
  });
  observer.observe(document.body, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["placeholder", "title", "aria-label"]
  });
})();
