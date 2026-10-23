// Crosshair + tooltip for the server-rendered charts (quanty/ui/charts.py). Values are
// read from each chart's embedded JSON; the tooltip is built with textContent only.
(function () {
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  document.body.appendChild(tip);

  const fmt = (v, unit) =>
    unit === "ms" ? `${Math.round(v)} ms` : `${v < -0.005 ? "−" : ""}$${Math.abs(v).toFixed(2)}`;
  const hm = (ms) => new Date(ms).toISOString().slice(11, 16) + " UTC";

  function valueAt(pts, t) {  // step-after: the last point at or before t
    let v = null;
    for (const [pt, pv] of pts) { if (pt <= t) v = pv; else break; }
    return v;
  }

  function attach(svg, data) {
    if (svg.dataset.hover) return;
    svg.dataset.hover = "1";
    const ns = "http://www.w3.org/2000/svg";
    const cross = document.createElementNS(ns, "line");
    cross.setAttribute("class", "crosshair");
    cross.setAttribute("y1", data.top);
    cross.setAttribute("y2", data.top + data.ph);
    cross.style.display = "none";
    svg.appendChild(cross);
    const hit = document.createElementNS(ns, "rect");  // bigger than the marks
    hit.setAttribute("x", data.x0); hit.setAttribute("y", 0);
    hit.setAttribute("width", data.pw); hit.setAttribute("height", data.top + data.ph + 8);
    hit.setAttribute("fill", "transparent");
    svg.appendChild(hit);

    function show(ev) {
      const box = svg.getBoundingClientRect();
      const sx = (ev.clientX - box.left) * (data.w / box.width);
      const frac = Math.min(1, Math.max(0, (sx - data.x0) / data.pw));
      const t = data.t0 + frac * (data.t1 - data.t0);
      cross.setAttribute("x1", sx); cross.setAttribute("x2", sx);
      cross.style.display = "";
      tip.replaceChildren();
      const head = document.createElement("div");
      head.className = "tip-head";
      head.textContent = hm(t);
      tip.appendChild(head);
      for (const s of data.series) {
        const v = valueAt(s.pts, t);
        const row = document.createElement("div");
        row.className = "tip-row";
        const sw = document.createElement("span");
        sw.className = `swatch s${s.slot}`;
        const name = document.createElement("span");
        name.textContent = s.name;
        const val = document.createElement("span");
        val.className = "num";
        val.textContent = v === null ? "–" : fmt(v, data.unit);
        row.append(sw, name, val);
        tip.appendChild(row);
      }
      tip.hidden = false;
      const tx = Math.min(window.innerWidth - tip.offsetWidth - 8, ev.clientX + 14);
      tip.style.left = `${tx + window.scrollX}px`;
      tip.style.top = `${ev.clientY + window.scrollY + 14}px`;
    }
    function hide() { cross.style.display = "none"; tip.hidden = true; }
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hide);
  }

  function scan(root) {
    root.querySelectorAll("script.chart-data").forEach((el) => {
      const svg = el.previousElementSibling;
      if (svg && svg.tagName.toLowerCase() === "svg") attach(svg, JSON.parse(el.textContent));
    });
  }
  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.addEventListener("htmx:afterSettle", (e) => { tip.hidden = true; scan(e.target); });
})();
