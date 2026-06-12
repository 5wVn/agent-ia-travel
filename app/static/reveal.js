/* Agent Travel — entrance animations, zero dependency.
   Adds a staggered fade-up+scale to every [data-reveal] element when it scrolls
   into view (once), and fills any quota gauge to its target width on reveal.
   Respects prefers-reduced-motion: everything shows instantly, no motion. */
(function () {
  "use strict";

  var REDUCED = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function fillGauge(el) {
    var fill = el.querySelector(".gauge .fill[data-target]");
    if (fill) {
      // double rAF so the 0 -> target transition actually plays
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { fill.style.width = fill.dataset.target + "%"; });
      });
    }
  }

  function reveal(el, delay) {
    el.style.setProperty("--reveal-delay", delay + "ms");
    el.classList.add("in");
    fillGauge(el);
  }

  function init() {
    var items = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    items.forEach(function (el) { el.classList.add("reveal"); });

    if (REDUCED || !("IntersectionObserver" in window)) {
      items.forEach(function (el) { el.classList.add("in"); fillGauge(el); });
      return;
    }

    // group reveals by animation frame so a batch entering together staggers
    var batch = [];
    var flushing = null;
    function flush() {
      batch.forEach(function (el, i) { reveal(el, Math.min(i, 8) * 70); });
      batch = [];
      flushing = null;
    }

    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        io.unobserve(e.target);
        batch.push(e.target);
        if (!flushing) flushing = requestAnimationFrame(flush);
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });

    items.forEach(function (el) { io.observe(el); });
  }

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
