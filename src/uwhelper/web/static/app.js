// Minimal progressive enhancement; the app works without JavaScript.
document.addEventListener("submit", function (ev) {
  var form = ev.target;
  var msg = form.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { ev.preventDefault(); }
});
document.addEventListener("change", function (ev) {
  var el = ev.target;
  if (el.matches && el.matches("input[data-check-all]")) {
    document.querySelectorAll("input[name='" + el.getAttribute("data-check-all") + "']").forEach(function (c) { c.checked = el.checked; });
  }
});
