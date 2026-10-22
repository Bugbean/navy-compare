// Password / magic-link tabs on the sign-in page.
document.querySelectorAll(".tabs button").forEach(function (b) {
  b.addEventListener("click", function () {
    document.querySelectorAll(".tabs button").forEach(function (x) { x.classList.toggle("on", x === b); });
    document.getElementById("pane-pw").hidden = b.dataset.pane !== "pw";
    document.getElementById("pane-magic").hidden = b.dataset.pane !== "magic";
  });
});
if (document.querySelector(".tabs").dataset.start === "magic") {
  document.querySelector('[data-pane="magic"]').click();
}
