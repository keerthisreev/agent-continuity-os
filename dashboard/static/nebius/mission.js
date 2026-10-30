// Mission Control UI: copy-receipt button + run-button busy state.
// No data is fetched or fabricated here; everything rendered server-side.
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) return;
    var target = document.getElementById(
      button.getAttribute("data-copy-target"));
    if (!target) return;
    var text = target.textContent || "";
    var note = document.getElementById("copy-note");
    function done() {
      if (note) note.textContent = "Copied.";
      button.textContent = "Copied";
      setTimeout(function () { button.textContent = "Copy receipt"; }, 2500);
    }
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); done(); }
      catch (err) { if (note) note.textContent = "Copy failed — select the text manually."; }
      document.body.removeChild(area);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.classList || !form.classList.contains("run-form")) return;
    var button = form.querySelector("button[type=submit]");
    if (button) {
      button.disabled = true;
      button.textContent = "Running…";
    }
  });
})();
