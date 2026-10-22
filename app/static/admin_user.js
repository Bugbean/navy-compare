// Select all / Clear all for each module group on the admin user page.
document.querySelectorAll(".layer[data-layer]").forEach(function (layer) {
  var btn = layer.querySelector(".toggle-all");
  var boxes = layer.querySelectorAll('input[name="modules"]');
  function sync() {
    var all = Array.prototype.every.call(boxes, function (b) { return b.checked; });
    btn.textContent = all ? "Clear all" : "Select all";
  }
  btn.addEventListener("click", function () {
    var all = Array.prototype.every.call(boxes, function (b) { return b.checked; });
    boxes.forEach(function (b) { b.checked = !all; });
    sync();
  });
  boxes.forEach(function (b) { b.addEventListener("change", sync); });
  sync();
});
