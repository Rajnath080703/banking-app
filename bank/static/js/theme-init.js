// Runs in <head> before first paint so there is no flash of the wrong theme.
(function () {
  var theme = null;
  try { theme = localStorage.getItem("bank-theme"); } catch (e) {}
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.setAttribute("data-theme", theme);
})();
