(function () {
  "use strict";
  if (!window.Chart) return;
  var charts = [];

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function palette() {
    return {
      c1: css("--chart-1"), c2: css("--chart-2"), c3: css("--chart-3"), c4: css("--chart-4"),
      c5: css("--chart-5"), c6: css("--chart-6"), grid: css("--chart-grid"), text: css("--muted"),
      surface: css("--surface"), fg: css("--text")
    };
  }
  function alpha(hex, a) {
    var h = hex.replace("#", "");
    if (h.length === 3) h = h.split("").map(function (x) { return x + x; }).join("");
    var n = parseInt(h, 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  function money(v) {
    var a = Math.abs(v);
    if (a >= 1e7) return "₹" + (v / 1e7).toFixed(1) + " Cr";
    if (a >= 1e5) return "₹" + (v / 1e5).toFixed(1) + " L";
    if (a >= 1e3) return "₹" + (v / 1e3).toFixed(0) + "K";
    return "₹" + v;
  }
  function fullMoney(v) { return "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 0 }); }

  function baseOptions(p, opts) {
    opts = opts || {};
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 400 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { position: "bottom", labels: { color: p.text, boxWidth: 10, boxHeight: 10, usePointStyle: true, padding: 14 } },
        tooltip: {
          backgroundColor: p.surface, titleColor: p.fg, bodyColor: p.fg, borderColor: p.grid, borderWidth: 1, padding: 10,
          callbacks: opts.money ? { label: function (c) { return " " + c.dataset.label + ": " + fullMoney(c.parsed.y); } } :
                     opts.pct ? { label: function (c) { return " " + c.dataset.label + ": " + c.parsed.y + "%"; } } : {}
        }
      },
      scales: opts.noScales ? {} : {
        x: { grid: { display: false }, ticks: { color: p.text, maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, border: { color: p.grid } },
        y: { grid: { color: p.grid }, border: { display: false }, beginAtZero: true,
             ticks: { color: p.text, callback: opts.money ? money : opts.pct ? function (v) { return v + "%"; } : undefined } }
      }
    };
  }

  function build() {
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    var p = palette();
    document.querySelectorAll("canvas[data-chart]").forEach(function (cv) {
      var d = JSON.parse(document.getElementById(cv.getAttribute("data-source")).textContent);
      var kind = cv.getAttribute("data-chart"), cfg;
      if (kind === "flow") {
        cfg = { type: "bar", data: { labels: d.labels, datasets: [
          { label: "Income", data: d.income, backgroundColor: css("--income"), borderRadius: 3, maxBarThickness: 9, categoryPercentage: 0.7, barPercentage: 0.9 },
          { label: "Outgoing", data: d.outgoing, backgroundColor: css("--outgoing"), borderRadius: 3, maxBarThickness: 9, categoryPercentage: 0.7, barPercentage: 0.9 }
        ] }, options: baseOptions(p, { money: true }) };
        cfg.options.plugins.legend.display = false;
      } else if (kind === "volume") {
        cfg = { type: "line", data: { labels: d.labels, datasets: [
          { label: "Transactions", data: d.values, borderColor: p.c1, backgroundColor: alpha(p.c1, 0.12), fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 }
        ] }, options: baseOptions(p) };
        cfg.options.plugins.legend.display = false;
      } else if (kind === "trends") {
        cfg = { type: "line", data: { labels: d.labels, datasets: [
          { label: "Deposits", data: d.deposits, borderColor: p.c1, backgroundColor: alpha(p.c1, 0.1), fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2 },
          { label: "Loan disbursements", data: d.loans, borderColor: p.c3, backgroundColor: alpha(p.c3, 0.08), fill: true, tension: 0.35, pointRadius: 2, borderWidth: 2 }
        ] }, options: baseOptions(p, { money: true }) };
      } else if (kind === "targets") {
        cfg = { type: "bar", data: { labels: d.labels, datasets: [
          { label: "Deposit target achieved", data: d.deposit_pct, backgroundColor: p.c2, borderRadius: 4, maxBarThickness: 28 },
          { label: "Loan target achieved", data: d.loan_pct, backgroundColor: p.c1, borderRadius: 4, maxBarThickness: 28 }
        ] }, options: baseOptions(p, { pct: true }) };
      } else if (kind === "doughnut") {
        // data-colors holds CSS custom property names (e.g. "acc-active,acc-inactive")
        var colors = (cv.getAttribute("data-colors") || "chart-2,chart-3,chart-6,chart-4").split(",")
          .map(function (k) { return p[k] || css("--" + k); });
        cfg = { type: "doughnut", data: { labels: d.labels, datasets: [
          { data: d.values, backgroundColor: colors, borderColor: p.surface, borderWidth: 2, hoverOffset: 4 }
        ] }, options: baseOptions(p, { noScales: true }) };
        cfg.options.cutout = "70%";
        cfg.options.interaction = { mode: "nearest", intersect: true };
        cfg.options.plugins.legend.display = false;
      }
      if (cfg) charts.push(new Chart(cv, cfg));
    });
  }
  build();
  document.addEventListener("themechange", function () { setTimeout(build, 30); });
})();
