// The hero: one example record, the mapping lines that apply to it, and what
// `open-entity-gateway print` wrote for it per adapter when the page was built
// (assets/from-tree/hero.json, written by site/build.py). Nothing is computed
// here; the page only lays out and marks what the gateway printed.
(function () {
  "use strict";

  document.querySelectorAll("button.copy").forEach(function (b) {
    b.addEventListener("click", function () {
      var text = document.getElementById(b.dataset.copy).textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () {
        b.textContent = "Copied"; setTimeout(function () { b.textContent = "Copy"; }, 1600);
      });
    });
  });

  var out = document.getElementById("out-static"), map = document.getElementById("map"), rec = document.getElementById("rec");
  var note = document.getElementById("note"), tabs = document.querySelectorAll("#tabs button");
  if (!out || !window.fetch) return;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }

  fetch("assets/from-tree/hero.json").then(function (r) { return r.json(); }).then(function (d) {
    var m = d.record.message;
    var values = [m.station_id, String(m.latitude), String(m.longitude), String(m.altitude)];
    function mark(text) {
      var h = esc(text);
      values.forEach(function (v) {
        h = h.split(esc(v)).join("\u0000" + esc(v) + "\u0001");
      });
      return h.replace(/\u0000/g, "<mark>").replace(/\u0001/g, "</mark>");
    }
    rec.innerHTML = mark(JSON.stringify(m).replace(/,"/g, ', "').replace(/":/g, '": '));

    // the mapping file's own lines, comments removed, grouped by the block they sit in
    var lines = d.mapping.split("\n").map(function (l) { return l.replace(/\s+#.*$/, ""); })
      .filter(function (l) { return l.trim() && l.trim()[0] !== "#"; });
    function block(name) {
      var res = [], inside = false;
      lines.forEach(function (l) {
        if (/^\S/.test(l)) inside = l.indexOf(name + ":") === 0;
        if (inside) res.push(l);
      });
      return res;
    }
    var common = ["uid", "position", "stale_minutes"].reduce(function (a, k) { return a.concat(block(k)); }, []);
    var mapFor = {
      lattice: common.slice(0, 1).concat(block("lattice").slice(0, 4)),
      cot: common.concat(block("type")),
      cat062: block("position").concat(block("asterix"))
    };

    var lattice = [];
    (function flat(o, path) {
      Object.keys(o).forEach(function (k) {
        var v = o[k], p = path ? path + "." + k : k;
        if (v && typeof v === "object" && !Array.isArray(v)) flat(v, p);
        else lattice.push(p + ": " + JSON.stringify(v));
      });
    })(d.lattice, "");
    var cot = d.cot.replace(/></g, ">\n<").split("\n").map(function (l, i, a) {
      return (i > 0 && i < a.length - 1 ? "  " : "") + l;
    });
    var payloads = {
      lattice: { lines: lattice, note: "The entity the Lattice bridge sends, one field path per line. expiryTime is the build time plus stale_minutes." },
      cot: { lines: cot, note: "A Cursor-on-Target event. This sender is moving to goat-transformers." },
      cat062: { groups: d.cat062, note: "One ASTERIX CAT062 record, " + d.cat062.reduce(function (n, g) { return n + g.hex.split(" ").length; }, 0) + " octets. Marked: position (I062/105) and altitude (I062/130). This sender is moving to goat-transformers." }
    };

    var order = ["lattice", "cot", "cat062"], cur = 0, timer = null, stepTimer = null, auto = !reduce;
    function show(k, animate) {
      tabs.forEach(function (b) { b.setAttribute("aria-selected", b.dataset.k === k ? "true" : "false"); });
      map.innerHTML = mapFor[k].map(function (l) { return "<span>" + esc(l) + "</span>"; }).join("");
      var spans = map.querySelectorAll("span");
      note.textContent = payloads[k].note;
      ["w1", "w2"].forEach(function (id) { var w = document.getElementById(id); w.classList.remove("go"); void w.offsetWidth; w.classList.add("go"); });
      var p = payloads[k], items;
      if (p.groups) {
        items = p.groups.map(function (g) {
          var hot = g.label === "I062/105" || g.label === "I062/130";
          return '<span class="grp' + (hot ? " hot" : "") + '"><b>' + esc(g.hex) + "</b><i>" + esc(g.label) + "</i></span>";
        });
      } else {
        items = p.lines.map(function (l) { return mark(l); });
      }
      var sep = p.groups ? "" : "\n";
      if (!animate) {
        out.innerHTML = items.join(sep);
        spans.forEach(function (s) { s.classList.add("on"); });
        return;
      }
      clearTimeout(stepTimer);
      out.innerHTML = "";
      var i = 0;
      (function step() {
        if (i < spans.length) spans[i].classList.add("on");
        out.innerHTML = items.slice(0, i + 1).join(sep);
        i++;
        if (i < Math.max(items.length, spans.length)) stepTimer = setTimeout(step, 160);
      })();
    }
    function cycle() {
      show(order[cur], true);
      timer = setTimeout(function () { cur = (cur + 1) % order.length; cycle(); }, 6500);
    }
    tabs.forEach(function (b) {
      b.addEventListener("click", function () {
        auto = false; clearTimeout(timer); clearTimeout(stepTimer); show(b.dataset.k, false);
      });
    });
    if (auto) cycle(); else show("lattice", false);
  }).catch(function () {});
})();
