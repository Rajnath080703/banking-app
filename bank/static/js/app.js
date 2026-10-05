(function () {
  "use strict";
  var root = document.documentElement;

  // ---------------------------------------------------------------- theme
  function setTheme(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem("bank-theme", theme); } catch (e) {}
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === theme));
    });
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }
  document.querySelectorAll("[data-theme-set]").forEach(function (b) {
    b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === root.getAttribute("data-theme")));
    b.addEventListener("click", function () { setTheme(b.getAttribute("data-theme-set")); });
  });

  // ---------------------------------------------------------------- sidebar / dropdowns
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-toggle-nav]");
    if (t) { document.body.classList.toggle("nav-open"); return; }
    if (e.target.classList.contains("overlay")) { document.body.classList.remove("nav-open"); }

    var dd = e.target.closest("[data-dropdown]");
    document.querySelectorAll(".dropdown").forEach(function (m) {
      if (!dd || m !== document.getElementById(dd.getAttribute("data-dropdown"))) m.classList.add("hidden");
    });
    if (dd) {
      var menu = document.getElementById(dd.getAttribute("data-dropdown"));
      if (menu) menu.classList.toggle("hidden");
    }

    var x = e.target.closest(".flash .x");
    if (x) x.parentElement.remove();

    var pw = e.target.closest("[data-pw-toggle]");
    if (pw) {
      var input = document.getElementById(pw.getAttribute("data-pw-toggle"));
      input.type = input.type === "password" ? "text" : "password";
      pw.setAttribute("aria-label", input.type === "password" ? "Show password" : "Hide password");
    }
  });

  // ---------------------------------------------------------------- confirmation dialogs
  var pendingForm = null, pendingSubmitter = null;
  var modal = document.getElementById("confirm-modal");
  function closeModal() { if (modal) modal.classList.add("hidden"); pendingForm = null; pendingSubmitter = null; }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("data-confirm") && !form.dataset.confirmed && modal) {
      e.preventDefault();
      pendingForm = form;
      pendingSubmitter = e.submitter || null;
      modal.querySelector("[data-confirm-title]").textContent = form.getAttribute("data-confirm-title") || "Please confirm";
      modal.querySelector("[data-confirm-text]").textContent = form.getAttribute("data-confirm");
      var ok = modal.querySelector("[data-confirm-ok]");
      ok.className = "btn " + (form.getAttribute("data-confirm-tone") || "primary");
      ok.textContent = form.getAttribute("data-confirm-ok") || "Confirm";
      modal.classList.remove("hidden");
      ok.focus();
    }
  }, true);
  if (modal) {
    modal.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      if (pendingForm) {
        var f = pendingForm, s = pendingSubmitter;
        f.dataset.confirmed = "1";
        closeModal();
        if (!f.requestSubmit) f.submit();
        else if (s) f.requestSubmit(s);
        else f.requestSubmit();
      }
    });
    modal.querySelectorAll("[data-confirm-cancel]").forEach(function (b) { b.addEventListener("click", closeModal); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeModal(); });
  }

  // ---------------------------------------------------------------- misc behaviours
  document.querySelectorAll("[data-width]").forEach(function (el) {
    var w = Math.max(0, Math.min(100, parseFloat(el.getAttribute("data-width")) || 0));
    requestAnimationFrame(function () { el.style.width = w + "%"; });
  });
  document.querySelectorAll("select[data-autosubmit]").forEach(function (s) {
    s.addEventListener("change", function () { s.form.submit(); });
  });
  document.querySelectorAll("[data-range-select]").forEach(function (sel) {
    var custom = document.querySelectorAll("[data-custom-range]");
    function sync() { custom.forEach(function (c) { c.classList.toggle("hidden", sel.value !== "custom"); }); }
    sel.addEventListener("change", function () { sync(); if (sel.value !== "custom") sel.form.submit(); });
    sync();
  });
  document.querySelectorAll("[data-check-col]").forEach(function (box) {
    box.addEventListener("change", function () {
      document.querySelectorAll('input[data-col="' + box.getAttribute("data-check-col") + '"]:not(:disabled)')
        .forEach(function (c) { c.checked = box.checked; });
    });
  });
  document.querySelectorAll("[data-check-row]").forEach(function (box) {
    box.addEventListener("change", function () {
      document.querySelectorAll('input[data-row="' + box.getAttribute("data-check-row") + '"]:not(:disabled)')
        .forEach(function (c) { c.checked = box.checked; });
    });
  });

  // Role select pre-fills the permission grid (Add Employee & Permissions)
  document.querySelectorAll("select[data-perm-source]").forEach(function (sel) {
    var map = JSON.parse(document.getElementById(sel.getAttribute("data-perm-source")).textContent);
    sel.addEventListener("change", function () {
      var grants = map[sel.value] || {};
      document.querySelectorAll(".perm-card input[type=checkbox]").forEach(function (box) {
        var parts = box.name.split(":");
        box.checked = (grants[parts[0]] || []).indexOf(parts[1]) !== -1;
      });
    });
  });

  // EMI preview on loan form
  var emiForm = document.querySelector("[data-emi-form]");
  if (emiForm) {
    var calc = function () {
      var p = parseFloat(emiForm.amount.value) || 0, r = (parseFloat(emiForm.interest_rate.value) || 0) / 1200,
          n = parseInt(emiForm.tenure_months.value, 10) || 0, out = document.getElementById("emi-preview");
      if (!p || !n) { out.textContent = "—"; return; }
      var emi = r ? p * r * Math.pow(1 + r, n) / (Math.pow(1 + r, n) - 1) : p / n;
      out.textContent = "₹" + emi.toLocaleString("en-IN", { maximumFractionDigits: 0 }) + " / month";
    };
    emiForm.addEventListener("input", calc);
    calc();
    var typeSel = emiForm.loan_type, goldBox = document.getElementById("gold-fields");
    var syncGold = function () { if (goldBox) goldBox.classList.toggle("hidden", typeSel.value !== "gold"); };
    typeSel.addEventListener("change", syncGold);
    syncGold();
  }

  // ---------------------------------------------------------------- idle session timeout
  var timeoutMin = parseInt(document.body.getAttribute("data-session-timeout") || "0", 10);
  if (timeoutMin > 0) {
    var last = Date.now();
    ["click", "keydown", "mousemove", "scroll", "touchstart"].forEach(function (ev) {
      document.addEventListener(ev, function () { last = Date.now(); }, { passive: true });
    });
    setInterval(function () {
      if (Date.now() - last > timeoutMin * 60000) window.location.reload();
    }, 30000);
  }
})();
