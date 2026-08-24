(() => {
  "use strict";

  const STORAGE_KEY = "localizer.dashboard.locale";
  const SUPPORTED = new Set(["zh-CN", "en-US"]);
  const textOriginal = new WeakMap();
  const attrOriginal = new WeakMap();

  const PHRASES = [
    ["Localizer 观测面板", "Localizer Dashboard"],
    ["启动本地任务", "Start local task"],
    ["任务预设", "Task preset"],
    ["临时配置（不套用预设）", "Temporary configuration (no preset)"],
    ["资源环境", "Resource environment"],
    ["单一资源目录", "Single resource directory"],
    ["游戏版本", "Game version"],
    ["源文件或源目录（运行机器上的绝对/相对路径）", "Source file or directory (absolute/relative path on runner)"],
    ["构建模式", "Build mode"],
    ["预设名称（保存时使用）", "Preset name (used when saving)"],
    ["run_id（留空自动生成）", "run_id (leave blank to generate)"],
    [".env 文件（可选，留空使用项目自动发现）", ".env file (optional; leave blank for project auto-discovery)"],
    ["保存 / 更新预设", "Save / update preset"],
    ["分析待翻译内容", "Analyze pending translations"],
    ["确认并启动", "Confirm and start"],
    ["流水线", "Pipeline"],
    ["运行详情", "Run details"],
    ["翻译记忆库", "Translation memory"],
    ["概览", "Overview"],
    ["实时翻译", "Live translation"],
    ["QA 问题", "QA issues"],
    ["审查修复", "Review & repair"],
    ["批次", "Batches"],
    ["制品", "Artifact"],
    ["文件与日志", "Files & logs"],
    ["载入中…", "Loading…"],
    ["自动刷新 5s", "Auto refresh 5s"],
    ["左侧选择一次运行", "Select a run on the left"],
    ["启动前请先分析待翻译内容；预检不会调用模型或写入 TM。", "Analyze pending translations before starting. Preflight does not call the model or write to TM."],
    ["参数已变化，请重新分析待翻译内容。", "Parameters changed. Analyze pending translations again."],
    ["扫描文件", "Scanned files"],
    ["待翻译文件", "Files to translate"],
    ["提取词条", "Extracted units"],
    ["待翻译词条", "Units to translate"],
    ["命中来源", "Match source"],
    ["计划指纹", "Plan fingerprint"],
    ["TM 命中来源", "TM match sources"],
    ["所有词条均已有可用译文；启动后不会调用模型。", "All units already have usable translations; starting will not call the model."],
    ["需要人工确认", "Human confirmation required"],
    ["资源 / 键", "Resource / key"],
    ["旧源文", "Previous source"],
    ["新源文", "New source"],
    ["旧译文 / 来源", "Previous translation / origin"],
    ["备份并退休旧记录", "Back up and retire stale entries"],
    ["确认后需重新预检，防止计划在 TM 变更后失效。", "Run preflight again after confirmation so the plan cannot go stale after the TM change."],
    ["正在备份并重新校验候选…", "Backing up and revalidating candidates…"],
    ["请重新点击“分析待翻译内容”，确认更新后的计划。", "Run “Analyze pending translations” again to confirm the updated plan."],
    ["预设已保存", "Preset saved"],
    ["正在只读扫描资源与 TM…", "Scanning resources and TM in read-only mode…"],
    ["预检完成", "Preflight complete"],
    ["预检失败", "Preflight failed"],
    ["请先分析待翻译内容", "Analyze pending translations first"],
    ["任务已入队", "Task queued"],
    ["当前无任务记录", "No task history for the current scope"],
    ["最近任务", "Latest task"],
    ["全部发布目标成功", "All publish targets succeeded"],
    ["存在发布目标失败", "Some publish targets failed"],
    ["QA 通过", "QA passed"],
    ["QA 未通过", "QA failed"],
    ["QA 尚未完成", "QA not finished"],
    ["等待执行", "Waiting to run"],
    ["资源已更新，等待确认", "Resources changed; waiting for confirmation of"],
    ["增量重建自", "Incremental rebuild from"],
    ["复用", "Reused"],
    ["重试", "Retried"],
    ["人工解决", "Human-resolved"],
    ["工作区与输出目录下暂无运行记录", "No runs found in the workspace or output directory"],
    ["闸门通过", "Gate passed"],
    ["闸门阻断", "Gate blocked"],
    ["成功", "Succeeded"],
    ["失败/待重试", "Failed / retryable"],
    ["完成度", "Completion"],
    ["按状态", "By state"],
    ["总词条", "Total units"],
    ["失败词条", "Failed units"],
    ["问题总数", "Total issues"],
    ["按类别", "By category"],
    ["闸门", "Gate"],
    ["通过", "Passed"],
    ["阻断", "Blocked"],
    ["翻译进度", "Translation progress"],
    ["QA 与闸门", "QA & gate"],
    ["模型运行", "Model runtime"],
    ["当前文件", "Current file"],
    ["文件进度", "File progress"],
    ["Provider 请求", "Provider requests"],
    ["输入 Token", "Input tokens"],
    ["输出 Token", "Output tokens"],
    ["总 Token", "Total tokens"],
    ["尚无 worker 状态", "No worker state yet"],
    ["执行已暂停，等待确认", "Execution paused; waiting for confirmation of"],
    ["备份、退休并继续原任务", "Back up, retire, and resume original task"],
    ["不会删除 checkpoint。", "The checkpoint will be preserved."],
    ["正在备份、复核并恢复 checkpoint…", "Backing up, revalidating, and restoring the checkpoint…"],
    ["原任务已重新入队。", "The original task has been queued again."],
    ["路径", "Path"],
    ["等待文件", "Waiting for file"],
    ["当前无批次", "No active batch"],
    ["worker 尚未启动", "Workers have not started"],
    ["旧 checkpoint 未记录词条级文件进度", "Legacy checkpoint has no unit-level file progress"],
    ["没有待模型翻译的文件", "No files require model translation"],
    ["checkpoint 落盘降级中", "Checkpoint persistence is degraded"],
    ["运行继续", "Execution continues"],
    ["但断点恢复可能会重译最近这段窗口内的词条。", "Recovery may retranslate units from the most recent persistence window."],
    ["最近一次错误", "Latest error"],
    ["活跃", "active"],
    ["文件", "Files"],
    ["运行中", "Running"],
    ["排队", "Queued"],
    ["异常文件", "Problem files"],
    ["请求", "Requests"],
    ["Token 入/出", "Tokens in/out"],
    ["文件级实时进度", "Live file progress"],
    ["状态 / worker", "State / worker"],
    ["词条进度", "Unit progress"],
    ["失败 / 成功", "Failed / succeeded"],
    ["批次 / 请求", "Batches / requests"],
    ["当前批次", "Current batch"],
    ["最近批次事件", "Recent batch events"],
    ["时间", "Time"],
    ["状态", "State"],
    ["原因", "Reason"],
    ["尚无批次事件", "No batch events yet"],
    ["本视图每 5 秒刷新", "This view refreshes every 5 seconds"],
    ["本次运行没有批次记录", "This run has no batch records"],
    ["终态", "Final state"],
    ["词条", "Units"],
    ["最近事件", "Recent events"],
    ["文件缺失", "File missing"],
    ["大小", "Size"],
    ["字节", "bytes"],
    ["创建于", "Created at"],
    ["远端发布已拦截", "Remote publishing blocked"],
    ["远端发布可执行", "Remote publishing ready"],
    ["仅本地发布", "Local publishing only"],
    ["目标", "Targets"],
    ["发布到全部已配置目标", "Publish to all configured targets"],
    ["显式操作；不会在 release 构建后自动上传", "Explicit action; release builds are never uploaded automatically"],
    ["Manifest 元数据", "Manifest metadata"],
    ["发布任务入队中…", "Queueing publish task…"],
    ["已入队；将依次尝试全部 target", "queued; all targets will be attempted in sequence"],
    ["本次运行没有可查看的文本产物", "This run has no viewable text artifacts"],
    ["修改时间", "Modified"],
    ["读取失败", "Read failed"],
    ["已截断，仅显示末尾 256 KB", "Truncated; showing only the final 256 KB"],
    ["全部严重度", "All severities"],
    ["全部类别", "All categories"],
    ["搜索路径 / 词条 / 说明", "Search path / unit / description"],
    ["这一页没有内容，回到第一页看看", "This page is empty. Return to the first page."],
    ["没有匹配的问题", "No matching issues"],
    ["严重度", "Severity"],
    ["类别", "Category"],
    ["来源", "Origin"],
    ["资源", "Resource"],
    ["说明", "Description"],
    ["本次新译", "New in this run"],
    ["人工定稿", "Human-reviewed"],
    ["存量", "Existing debt"],
    ["上一页", "Previous"],
    ["下一页", "Next"],
    ["审查视图不可用", "Review view unavailable"],
    ["单人定点修复", "single-operator targeted repair"],
    ["批量人工翻译与校对统一在 ParaTranz（M5）完成。", "Bulk human translation and proofreading remain in ParaTranz (M5)."],
    ["应用修复并重新构建", "Apply repairs and rebuild"],
    ["子运行 run_id（留空自动生成）", "Child run_id (leave blank to generate)"],
    ["目标游戏版本（发布前确认未占用）", "Target game version (confirm it is unused before publishing)"],
    ["子运行", "Child run"],
    ["已入队，目标版本", "queued; target version"],
    ["术语", "Glossary"],
    ["个决策", "decisions"],
    ["同源多译", "Source inconsistencies"],
    ["空译文", "Empty translations"],
    ["占位符不符", "Placeholder mismatch"],
    ["已定", "Committed"],
    ["草稿", "Draft"],
    ["待议", "Deferred"],
    ["跳过", "Skipped"],
    ["没有术语违规", "No glossary violations"],
    ["这是唯一能解发布阻断的队列。", "This is the only queue that can clear the publish blocker."],
    ["人工定稿术语", "Human-reviewed term"],
    ["已统一为", "Unified to"],
    ["还有", "plus"],
    ["种译法", "variants"],
    ["只落表", "Only committed"],
    ["没有同源多译", "No source inconsistencies"],
    ["一键按多数派统一落表", "Commit unique-majority variants"],
    ["批量同步理由（必填）", "Bulk sync reason (required)"],
    ["已修复", "Repaired"],
    ["这个队列是空的", "This queue is empty"],
    ["正在同步…", "Synchronizing…"],
    ["多数派同步", "Majority sync"],
    ["存在受保护或校验失败的组，请逐组查看。", "Some groups are protected or failed validation; review them individually."],
    ["匹配方式", "Match mode"],
    ["当前 scope", "Current scope"],
    ["已排除", "Excluded"],
    ["违规", "Violations"],
    ["逐条修改违规译文", "Edit violating translations individually"],
    ["操作", "Action"],
    ["译文", "Translation"],
    ["源文", "Source"],
    ["已人工落表", "Human-committed"],
    ["本地重判", "Local recheck"],
    ["修改理由（必填）", "Change reason (required)"],
    ["落表已修改译文", "Commit changed translations"],
    ["用表内当前译文重判全部", "Recheck all using current table values"],
    ["按语境排除该术语", "Exclude this term by context"],
    ["排除理由（必填）", "Exclusion reason (required)"],
    ["排除指定路径", "Exclude selected path"],
    ["重判", "Rechecked"],
    ["仍违规", "still violating"],
    ["此结果不构成发布结论（authoritative=false）。", "This result is not an authoritative publish verdict (authoritative=false)."],
    ["已消解", "Resolved"],
    ["仍有", "Remaining"],
    ["新增", "Introduced"],
    ["没有检测到译文修改。", "No translation changes detected."],
    ["一次最多落表 100 条，请分批修改提交。", "At most 100 items can be committed at once; submit changes in batches."],
    ["请填写排除路径与理由。", "Provide both an exclusion path and a reason."],
    ["译法", "Variant"],
    ["条数", "Count"],
    ["采纳", "Choose"],
    ["统一为此", "Use this"],
    ["自定义译文", "Custom translation"],
    ["也可以写一个都不在列表里的译法", "You may enter a translation not listed above"],
    ["定稿理由（必填）", "Decision reason (required)"],
    ["用自定义译文统一", "Unify with custom translation"],
    ["再次落表", "Commit again"],
    ["落表", "Commit"],
    ["存草稿", "Save draft"],
    ["本地重新校验", "Local recheck"],
    ["换行数量", "Line breaks"],
    ["占位符已高亮", "placeholders highlighted"],
    ["表示显式换行", "marks an explicit line break"],
    ["问题", "Issues"],
    ["新引入", "Introduced"],
    ["未评估", "Not evaluated"],
    ["这不是发布结论。", "This is not a publish verdict."],
    ["已记录。", "Recorded."],
    ["已落表", "Committed"],
    ["只写入", "Only wrote"],
    ["未完成", "incomplete"],
    ["权威源", "Authority"],
    ["已切换为权威", "Authoritative"],
    ["影子库（非权威）", "Shadow TM (non-authoritative)"],
    ["条目总数", "Total entries"],
    ["正式", "formal"],
    ["审核状态", "Review state"],
    ["质量分类", "Quality class"],
    ["数据库", "Database"],
    ["影子同步记录", "Shadow sync history"],
    ["源文件", "Source file"],
    ["哈希", "Hash"],
    ["导入", "Imported"],
    ["M0 存量分类基线", "M0 legacy classification baseline"],
    ["总计", "Total"],
    ["TM 已是权威源", "TM is authoritative"],
    ["TM 影子库（非权威）", "TM shadow store (non-authoritative)"],
    ["TM 未创建", "TM not created"],
    ["发布", "Publish"],
    ["更新于", "Updated"],
    ["刷新失败", "Refresh failed"],
    ["初始化失败", "Initialization failed"],
    ["自动", "Auto"],
    ["未自动发现；可填写 .env 文件路径", "Not auto-discovered; enter an .env path if needed"],
    ["当前绑定地址不是回环地址，任务写接口已自动关闭；此面板仅展示运行状态。", "The dashboard is not bound to a loopback address, so task write APIs are disabled. This page is read-only."],
    ["RU 正式服", "RU live"],
    ["PT 测试服", "PT test"],
    ["已切换到", "Switched to"],
    ["请重新分析待翻译内容。", "Analyze pending translations again."],
    ["发布任务正在执行", "Publish task is running"],
    ["发布完成", "Publish complete"],
    ["发布未完全成功", "Publish partially failed"],
    ["发布任务执行失败", "Publish task failed"],
    ["发布状态异常", "Unexpected publish state"],
    ["已产出正式制品与 Manifest", "Release artifact and Manifest created"],
    ["QualityGate 未通过", "QualityGate failed"],
    ["QualityGate 通过", "QualityGate passed"],
    ["批次进行中", "Batch processing in progress"],
    ["工作区已创建，尚无批次记录", "Workspace created; no batch records yet"],
    ["无可用证据", "No available evidence"],
    ["资源扫描", "Resource scan"],
    ["只读遍历游戏资源，产出扫描清单", "Read-only resource traversal and scan manifest"],
    ["词条提取", "Unit extraction"],
    ["Adapter 把资源解析为标准词条", "Adapters parse resources into normalized units"],
    ["坐标精确命中 + 已审核全局命中", "Coordinate exact matches + reviewed global matches"],
    ["模型翻译", "Model translation"],
    ["未命中词条分批送 Provider，含缩批与断点", "Unmatched units are batched to the provider with split/recovery support"],
    ["QA 校验", "QA validation"],
    ["占位符、术语、源语言残留、同源异译", "Placeholders, glossary, source-language residue, and source inconsistencies"],
    ["release 零容忍；preview 只记录不晋升", "Zero tolerance for release; preview records without promotion"],
    ["构建制品", "Build artifact"],
    ["回编译资源 + ZIP + Manifest", "Recompile resources + ZIP + Manifest"],
    ["Local / GitHub Release / R2", "Local / GitHub Release / R2"],
    ["占位符集合与源文不一致", "Placeholder set differs from source"],
    ["译文与源文完全相同", "Translation is identical to source"],
    ["含 NUL 控制字符", "Contains NUL control character"],
    ["缺少已审核术语的标准译名", "Missing reviewed glossary translation"],
    ["同一源文在本次运行内有多个译法", "One source has multiple translations in this run"],
    ["未获规则允许的源语言残留", "Source-language residue not allowed by rules"],
    ["占位符 token 以变体形式残留在译文里", "Placeholder token remains in a variant form"],
    ["译文为空", "Translation is empty"]
  ].sort((a, b) => b[0].length - a[0].length);

  const PATTERNS = [
    [/第\s*([\d,.]+)–([\d,.]+)\s*条，共\s*([\d,.]+)\s*条/g, "Items $1–$2 of $3"],
    [/([\d,.]+)\s*次/g, "$1 runs"],
    [/([\d,.]+)\s*个文件/g, "$1 files"],
    [/([\d,.]+)\s*个 target/g, "$1 targets"],
    [/([\d,.]+)\s*个成员/g, "$1 members"],
    [/([\d,.]+)\s*个决策/g, "$1 decisions"],
    [/([\d,.]+)\s*组/g, "$1 groups"],
    [/([\d,.]+)\s*条/g, "$1 items"],
    [/([\d,.]+)\s*种译法/g, "$1 variants"],
    [/连续失败\s*([\d,.]+)\s*次/g, "$1 consecutive failures"],
    [/已处理/g, "processed"],
    [/失败/g, "failed"]
  ];

  function normalizeLocale(value) {
    const raw = String(value || "").toLowerCase();
    if (raw.startsWith("zh")) return "zh-CN";
    return "en-US";
  }

  function initialLocale() {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (SUPPORTED.has(stored)) return stored;
    } catch (_err) {}
    const preferred = (navigator.languages && navigator.languages[0]) || navigator.language || "zh-CN";
    return normalizeLocale(preferred);
  }

  let currentLocale = initialLocale();

  function locale() {
    return currentLocale;
  }

  function translateText(source) {
    if (currentLocale !== "en-US" || !source || !source.trim()) return source;
    let value = source;
    for (const [zh, en] of PHRASES) value = value.split(zh).join(en);
    for (const [pattern, replacement] of PATTERNS) value = value.replace(pattern, replacement);
    return value;
  }

  function skipTextNode(node) {
    const parent = node.parentElement;
    if (!parent) return true;
    return Boolean(parent.closest("script,style,pre,code,textarea,.mono,[data-i18n-raw]"));
  }

  function desiredText(source) {
    return currentLocale === "zh-CN" ? source : translateText(source);
  }

  function processTextNode(node, allowSourceRefresh) {
    if (skipTextNode(node)) return;
    const current = node.nodeValue || "";
    if (!current.trim()) return;
    let source = textOriginal.get(node);
    if (source === undefined) {
      source = current;
      textOriginal.set(node, source);
    } else if (allowSourceRefresh && current !== desiredText(source)) {
      source = current;
      textOriginal.set(node, source);
    }
    const desired = desiredText(source);
    if (node.nodeValue !== desired) node.nodeValue = desired;
  }

  function processAttributes(element, allowSourceRefresh) {
    if (!(element instanceof Element)) return;
    let saved = attrOriginal.get(element);
    if (!saved) {
      saved = new Map();
      attrOriginal.set(element, saved);
    }
    for (const attr of ["placeholder", "title", "aria-label"]) {
      if (!element.hasAttribute(attr)) continue;
      const current = element.getAttribute(attr) || "";
      let source = saved.get(attr);
      if (source === undefined) {
        source = current;
        saved.set(attr, source);
      } else if (allowSourceRefresh && current !== desiredText(source)) {
        source = current;
        saved.set(attr, source);
      }
      const desired = desiredText(source);
      if (current !== desired) element.setAttribute(attr, desired);
    }
  }

  function translateTree(root, allowSourceRefresh = false) {
    if (!root) return;
    if (root.nodeType === Node.TEXT_NODE) {
      processTextNode(root, allowSourceRefresh);
      return;
    }
    if (!(root instanceof Element) && root !== document) return;
    if (root instanceof Element) processAttributes(root, allowSourceRefresh);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) processTextNode(node, allowSourceRefresh);
    if (root.querySelectorAll) {
      root.querySelectorAll("[placeholder],[title],[aria-label]").forEach((element) =>
        processAttributes(element, allowSourceRefresh)
      );
    }
  }

  function updateChrome() {
    document.documentElement.lang = currentLocale === "zh-CN" ? "zh-Hans" : "en";
    document.title = currentLocale === "zh-CN" ? "Localizer 观测面板" : "Localizer Dashboard";
    const button = document.getElementById("localeToggle");
    if (button) {
      button.textContent = currentLocale === "zh-CN" ? "EN" : "中文";
      button.title = currentLocale === "zh-CN" ? "Switch to English" : "切换到中文";
      button.setAttribute("aria-label", button.title);
    }
  }

  function setLocale(next) {
    currentLocale = normalizeLocale(next);
    try { localStorage.setItem(STORAGE_KEY, currentLocale); } catch (_err) {}
    updateChrome();
    translateTree(document, false);
    document.dispatchEvent(new CustomEvent("localizer:locale-changed", { detail: { locale: currentLocale } }));
  }

  function installToggle() {
    if (document.getElementById("localeToggle")) return;
    const grow = document.querySelector("header .grow");
    if (!grow) return;
    const button = document.createElement("button");
    button.type = "button";
    button.id = "localeToggle";
    button.className = "secondary";
    button.style.padding = "3px 9px";
    button.style.borderRadius = "999px";
    button.addEventListener("click", () => setLocale(currentLocale === "zh-CN" ? "en-US" : "zh-CN"));
    grow.insertAdjacentElement("afterend", button);
  }

  window.LocalizerI18n = { locale, setLocale, translateText, translateTree };
  installToggle();
  updateChrome();
  translateTree(document, false);

  const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      if (mutation.type === "characterData") {
        processTextNode(mutation.target, true);
      } else if (mutation.type === "attributes") {
        processAttributes(mutation.target, true);
      } else {
        mutation.addedNodes.forEach((node) => translateTree(node, true));
      }
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
