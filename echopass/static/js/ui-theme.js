(function () {
  const THEME_STORAGE_KEY = "echopass_ui_theme";
  const THEME_IDS = ["aurora", "midnight", "ember", "forest", "paper"];

  let onThemeChanged = null;

  function apply(id) {
    let next = id;
    if (!THEME_IDS.includes(next)) next = "aurora";
    if (next === "aurora") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", next);
    }
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch (_) {}
    document.querySelectorAll(".theme-pill").forEach((btn) => {
      const bid = btn.getAttribute("data-theme-id");
      btn.setAttribute("aria-pressed", bid === next ? "true" : "false");
    });
    if (typeof onThemeChanged === "function") onThemeChanged(next);
  }

  function setPopoverOpen(open) {
    const pop = document.getElementById("themePopover");
    const btn = document.getElementById("btnThemeUi");
    if (!pop || !btn) return;
    if (open) pop.removeAttribute("hidden");
    else pop.setAttribute("hidden", "");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.classList.toggle("active", !!open);
  }

  function init(opts) {
    onThemeChanged = opts && typeof opts.onThemeChanged === "function"
      ? opts.onThemeChanged
      : null;

    let saved = "aurora";
    try {
      saved = localStorage.getItem(THEME_STORAGE_KEY) || "aurora";
    } catch (_) {}
    if (!THEME_IDS.includes(saved)) saved = "aurora";
    apply(saved);

    const wrap = document.querySelector(".theme-popover-wrap");
    const btnTheme = document.getElementById("btnThemeUi");
    if (btnTheme) {
      btnTheme.addEventListener("click", (e) => {
        e.stopPropagation();
        const pop = document.getElementById("themePopover");
        if (!pop) return;
        setPopoverOpen(pop.hasAttribute("hidden"));
      });
    }
    document.addEventListener("click", (e) => {
      if (!wrap || wrap.contains(e.target)) return;
      const pop = document.getElementById("themePopover");
      if (pop && !pop.hasAttribute("hidden")) setPopoverOpen(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") setPopoverOpen(false);
    });
    document.querySelectorAll(".theme-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        apply(btn.getAttribute("data-theme-id") || "aurora");
        setPopoverOpen(false);
      });
    });
  }

  window.EchoPassTheme = {
    init,
    apply,
    setPopoverOpen,
  };
})();
