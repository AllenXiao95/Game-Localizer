(() => {
  "use strict";

  const api = window.LocalizerI18n;
  if (!api) return;

  function localized(text) {
    return api.translateText(String(text || ""));
  }

  function syncScopeDescriptions() {
    document.querySelectorAll("[data-ux-scope]").forEach((node) => {
      node.setAttribute("aria-description", localized(`影响对象：${node.dataset.uxScope}`));
    });
  }

  if (!window.__localizerWorkflowConfirmWrapped) {
    const nativeConfirm = window.confirm.bind(window);
    window.confirm = (message) => nativeConfirm(localized(message));
    window.__localizerWorkflowConfirmWrapped = true;
  }

  syncScopeDescriptions();
  document.addEventListener("localizer:locale-changed", () =>
    setTimeout(syncScopeDescriptions, 0));

  window.LocalizerWorkflowLocaleBridge = { sync: syncScopeDescriptions };
})();
