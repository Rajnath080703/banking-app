// Copy the shared remarks box into whichever action form is submitted.
(function () {
  var box = document.getElementById("remarks");
  if (!box) return;
  document.querySelectorAll("[data-copy-remarks]").forEach(function (hidden) {
    hidden.form.addEventListener("submit", function () { if (!hidden.value) hidden.value = box.value; }, true);  // a reason typed in the dialog wins
  });
})();
