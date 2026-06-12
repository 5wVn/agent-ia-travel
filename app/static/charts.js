/* Agent IA Travel — Chart.js setup for route price sparklines.
   Reads data from data-* attributes on each <canvas data-prices>:
     data-labels  : JSON array of ISO day strings ("2026-09-12")
     data-prices  : JSON array of numbers (EUR)
     data-median  : number | "" (30-day median, dashed reference line)
     data-snipe   : number | "" (armed snipe threshold, dashed line)
   French tooltips ("54 € — 12 sept."), euro Y axis, no heavy animation. */
(function () {
  "use strict";

  var FR_MONTHS = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc."
  ];
  var NBSP = " "; // narrow no-break space, matches the Python formatter

  function eur(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return Math.round(v) + NBSP + "€";
  }

  function frDay(iso) {
    if (!iso) return "";
    var p = String(iso).slice(0, 10).split("-");
    if (p.length !== 3) return iso;
    return parseInt(p[2], 10) + " " + (FR_MONTHS[parseInt(p[1], 10) - 1] || "");
  }

  function cssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }

  function gradient(ctx, area, accent) {
    var g = ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, accent.replace("ACCENT", "0.28"));
    g.addColorStop(1, accent.replace("ACCENT", "0.01"));
    return g;
  }

  function refLine(label, value, color, dash) {
    return {
      label: label,
      data: [],          // filled per-render below
      borderColor: color,
      borderDash: dash,
      borderWidth: 1.5,
      pointRadius: 0,
      fill: false,
      tension: 0,
      _ref: value
    };
  }

  function prefersReducedMotion() {
    return window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function num(v) {
    if (v === "" || v === null || v === undefined) return null;
    var n = parseFloat(v);
    return isNaN(n) ? null : n;
  }

  function render(cv) {
    var labels = JSON.parse(cv.dataset.labels || "[]");
    var prices = JSON.parse(cv.dataset.prices || "[]");
    var median = num(cv.dataset.median);
    var snipe = num(cv.dataset.snipe);
    if (!labels.length) return;

    var accentHex = cssVar("--accent", "#4c9aff").trim();
    // build rgba template "rgba(r,g,b,ACCENT)" from a #rrggbb accent
    var r = parseInt(accentHex.slice(1, 3), 16);
    var g = parseInt(accentHex.slice(3, 5), 16);
    var b = parseInt(accentHex.slice(5, 7), 16);
    var accentTpl = "rgba(" + r + "," + g + "," + b + ",ACCENT)";
    var muted = cssVar("--muted", "#8b97a5").trim();
    var warn = cssVar("--warn", "#d29922").trim();
    var border = cssVar("--border", "#2c3744").trim();

    var datasets = [{
      label: "Prix",
      data: prices,
      borderColor: accentHex,
      borderWidth: 2,
      backgroundColor: function (c) {
        var chart = c.chart;
        if (!chart.chartArea) return accentTpl.replace("ACCENT", "0.12");
        return gradient(chart.ctx, chart.chartArea, accentTpl);
      },
      fill: true,
      tension: 0.3,
      pointRadius: 2,
      pointHoverRadius: 4
    }];

    if (median !== null) {
      var dm = refLine("Médiane 30 j", median, muted, [5, 4]);
      dm.data = labels.map(function () { return median; });
      datasets.push(dm);
    }
    if (snipe !== null) {
      var ds = refLine("Seuil snipe", snipe, warn, [3, 3]);
      ds.data = labels.map(function () { return snipe; });
      datasets.push(ds);
    }

    new Chart(cv.getContext("2d"), {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: prefersReducedMotion()
          ? false
          : { duration: 600, easing: "easeOutCubic" },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: datasets.length > 1,
            position: "bottom",
            labels: { boxWidth: 14, boxHeight: 2, color: muted, font: { size: 11 }, filter: function (it) { return it.text !== "Prix"; } }
          },
          tooltip: {
            callbacks: {
              title: function (items) { return items.length ? frDay(items[0].label) : ""; },
              label: function (ctx) {
                var name = ctx.dataset.label;
                return (name === "Prix" ? "" : name + " : ") + eur(ctx.parsed.y);
              }
            }
          }
        },
        scales: {
          x: { ticks: { maxTicksLimit: 6, color: muted, callback: function (v) { return frDay(this.getLabelForValue(v)); } }, grid: { color: border } },
          y: {
            beginAtZero: false,
            ticks: { color: muted, callback: function (v) { return eur(v); } },
            grid: { color: border }
          }
        }
      }
    });
  }

  function init() {
    if (typeof Chart === "undefined") return;
    document.querySelectorAll("canvas[data-prices]").forEach(render);
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
