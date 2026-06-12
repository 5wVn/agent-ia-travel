/* Agent Travel — Chart.js setup for the overview route price chart.
   A single chart (#route-chart) renders the currently selected route. Each
   row of #routes-table carries the data on data-* attributes:
     data-labels  : JSON array of ISO day strings ("2026-09-12")
     data-prices  : JSON array of numbers (EUR)
     data-median  : number | "" (30-day median, dashed reference line)
     data-snipe   : number | "" (armed snipe threshold, dashed line)
   Clicking a row swaps the chart to that route. Sober brown/terracotta curve,
   dashed median + snipe lines, French tooltips, tabular euro axis. */
(function () {
  "use strict";

  var FR_MONTHS = [
    "janv.", "févr.", "mars", "avr.", "mai", "juin",
    "juil.", "août", "sept.", "oct.", "nov.", "déc."
  ];
  var NBSP = " "; // narrow no-break space, matches the Python formatter

  var chart = null;

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
    g.addColorStop(0, accent.replace("ACCENT", "0.22"));
    g.addColorStop(1, accent.replace("ACCENT", "0.01"));
    return g;
  }

  function refLine(label, value, color, dash, labels) {
    return {
      label: label,
      data: labels.map(function () { return value; }),
      borderColor: color,
      borderDash: dash,
      borderWidth: 1.25,
      pointRadius: 0,
      fill: false,
      tension: 0
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

  function show(row) {
    var canvas = document.getElementById("route-chart");
    var wrap = document.getElementById("chart-wrap");
    var empty = document.getElementById("chart-empty");
    var title = document.getElementById("chart-title");
    if (!canvas || !row) return;

    if (title) title.textContent = row.dataset.label || "";

    var labels = JSON.parse(row.dataset.labels || "[]");
    var prices = JSON.parse(row.dataset.prices || "[]");
    var median = num(row.dataset.median);
    var snipe = num(row.dataset.snipe);

    if (chart) { chart.destroy(); chart = null; }

    if (!labels.length) {
      if (wrap) wrap.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }
    if (wrap) wrap.hidden = false;
    if (empty) empty.hidden = true;

    var accentHex = cssVar("--accent", "#C2643C").trim();
    var r = parseInt(accentHex.slice(1, 3), 16);
    var g = parseInt(accentHex.slice(3, 5), 16);
    var b = parseInt(accentHex.slice(5, 7), 16);
    var accentTpl = "rgba(" + r + "," + g + "," + b + ",ACCENT)";
    var muted = cssVar("--muted", "#7A6E5C").trim();
    var border = cssVar("--border", "#DDD4C4").trim();
    var brique = cssVar("--bad", "#A8412F").trim();

    var datasets = [{
      label: "Prix",
      data: prices,
      borderColor: accentHex,
      borderWidth: 2,
      backgroundColor: function (c) {
        var ch = c.chart;
        if (!ch.chartArea) return accentTpl.replace("ACCENT", "0.10");
        return gradient(ch.ctx, ch.chartArea, accentTpl);
      },
      fill: true,
      tension: 0.25,
      pointRadius: 2,
      pointHoverRadius: 4,
      pointBackgroundColor: accentHex
    }];

    if (median !== null) datasets.push(refLine("Médiane 30 j", median, muted, [5, 4], labels));
    if (snipe !== null) datasets.push(refLine("Seuil snipe", snipe, brique, [3, 3], labels));

    chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: prefersReducedMotion()
          ? false
          : { duration: 350, easing: "easeOutCubic" },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: datasets.length > 1,
            position: "bottom",
            labels: {
              boxWidth: 14, boxHeight: 2, color: muted, font: { size: 11 },
              filter: function (it) { return it.text !== "Prix"; }
            }
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

  // Sélecteur commun aux deux rendus de la liste des routes : le tableau dense
  // desktop (#routes-table tr.route-row) et la liste mobile (.route-row dans
  // #routes-mlist). Les deux portent les mêmes data-* et un data-route-id, ce
  // qui permet de synchroniser la sélection d'un rendu à l'autre.
  function allRows() {
    return document.querySelectorAll(
      "#routes-table tr.route-row, #routes-mlist .route-row"
    );
  }

  function select(row) {
    var rid = row.dataset.routeId;
    allRows().forEach(function (el) {
      el.classList.toggle("selected", el.dataset.routeId === rid);
    });
    show(row);
  }

  function init() {
    if (typeof Chart === "undefined") return;
    var rows = allRows();
    if (!rows.length) return;
    rows.forEach(function (row) {
      row.addEventListener("click", function () { select(row); });
    });
    var selected =
      document.querySelector(
        "#routes-table tr.route-row.selected, #routes-mlist .route-row.selected"
      ) || rows[0];
    show(selected);
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
