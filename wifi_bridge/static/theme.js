/* Runs before the stylesheet so a saved night theme never flashes white. */
(() => {
  "use strict";
  const root = document.documentElement;
  const key = "smart-telescope.theme";
  const modes = ["auto", "light", "dark"];
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let preference = "auto";
  try {
    const saved = localStorage.getItem(key);
    if (modes.includes(saved)) preference = saved;
  } catch (_) { /* Still works when browser storage is unavailable. */ }
  const icons = {
    auto: '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4 19 5"/>',
    dark: '<path d="M20.8 13.1A9 9 0 0 1 10.9 3.2 9 9 0 1 0 20.8 13.1Z"/>'
  };
  const labels = { auto: "Automatic", light: "Light", dark: "Dark" };
  function apply() {
    const effective = preference === "auto" ? (system.matches ? "dark" : "light") : preference;
    root.dataset.theme = effective;
    root.dataset.themeMode = preference;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = effective === "dark" ? "#0a0d0b" : "#f7f4ee";
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    const next = modes[(modes.indexOf(preference) + 1) % modes.length];
    document.getElementById("theme-icon").innerHTML = icons[preference];
    document.getElementById("theme-label").textContent = BridgeI18n.t(labels[preference]);
    const description = BridgeI18n.t("Theme: {current}. Switch to {next}.", {current:BridgeI18n.t(labels[preference]), next:BridgeI18n.t(labels[next])});
    button.setAttribute("aria-label", description);
    button.title = description;
  }
  apply();
  document.addEventListener("bridge:localechange", apply);
  system.addEventListener("change", () => { if (preference === "auto") apply(); });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) {
      preference = modes.includes(event.newValue) ? event.newValue : "auto";
      apply();
    }
  });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle").addEventListener("click", () => {
      preference = modes[(modes.indexOf(preference) + 1) % modes.length];
      try { localStorage.setItem(key, preference); } catch (_) { /* Session-only choice. */ }
      apply();
    });
  });
})();
