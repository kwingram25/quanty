// Tabs that survive the page's auto-refresh without flicker: the selected tab lives on <html>
// (data-tab-<set>), which htmx never swaps, and CSS shows the matching panel. It is remembered
// in sessionStorage across reloads.
(function () {
  const root = document.documentElement;
  const store = (k, v) => { try { v === undefined ? (v = sessionStorage.getItem(k)) : sessionStorage.setItem(k, v); } catch (e) {} return v; };

  function select(set, tab) {
    root.dataset[`tab${set[0].toUpperCase()}${set.slice(1)}`] = tab;
    store(`tab:${set}`, tab);
    document.querySelectorAll(`[data-tabs="${set}"] [role=tab]`).forEach((t) => {
      t.setAttribute("aria-selected", t.dataset.tab === tab);
    });
  }

  function scan() {
    document.querySelectorAll("[data-tabs]").forEach((box) => {
      const set = box.dataset.tabs;
      const names = [...box.querySelectorAll("[role=tab]")].map((t) => t.dataset.tab);
      const want = store(`tab:${set}`);
      select(set, names.includes(want) ? want : names[0]);
    });
  }

  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-tabs] [role=tab]");
    if (t) select(t.closest("[data-tabs]").dataset.tabs, t.dataset.tab);
  });
  document.addEventListener("DOMContentLoaded", scan);
  document.addEventListener("htmx:afterSettle", scan);
})();
