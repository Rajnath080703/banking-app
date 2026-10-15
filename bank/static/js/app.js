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
  // Forms with data-confirm ask first. data-confirm-reason adds a required reason box whose
  // text is copied into the form's hidden "reason" field. Focus is trapped in the dialog and
  // returned to the triggering button when it closes.
  var pendingForm = null, pendingSubmitter = null, lastFocus = null;
  var modal = document.getElementById("confirm-modal");
  var reasonWrap = modal && modal.querySelector("[data-confirm-reason-wrap]");
  var reasonBox = modal && modal.querySelector("#confirm-reason");
  var reasonErr = modal && modal.querySelector("[data-confirm-reason-error]");
  function closeModal() {
    if (modal) modal.classList.add("hidden");
    pendingForm = null; pendingSubmitter = null;
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  // "{field}" in confirm text is replaced with what the employee entered, so the dialog states
  // exactly what will happen ("Deposit of ₹5,000.00 to 5012…"). Selects show the chosen label;
  // fields marked data-money are shown as rupees.
  function fillIn(text, form) {
    return (text || "").replace(/\{(\w+)\}/g, function (m, name) {
      var el = form.elements[name];
      if (!el) return m;
      if (el.tagName === "SELECT") return el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : "";
      var v = (el.value || "").trim();
      if (el.hasAttribute("data-money")) {
        var n = Number(v);
        return isFinite(n) && v !== "" ? "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "₹—";
      }
      return v || "—";
    });
  }
  // A submit button may carry its own wording (data-confirm, -title, -ok, -tone, -reason),
  // e.g. "Verify" and "Reject" on the same form.
  function attr(form, submitter, name) {
    return (submitter && submitter.hasAttribute(name)) ? submitter.getAttribute(name) : form.getAttribute(name);
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    var s = e.submitter || null;
    var asks = form.hasAttribute("data-confirm") || (s && s.hasAttribute("data-confirm"));
    if (asks && !form.dataset.confirmed && modal) {
      e.preventDefault();
      pendingForm = form;
      pendingSubmitter = s;
      lastFocus = s || document.activeElement;
      modal.querySelector("[data-confirm-title]").textContent = attr(form, s, "data-confirm-title") || "Please confirm";
      modal.querySelector("[data-confirm-text]").textContent = fillIn(attr(form, s, "data-confirm"), form);
      var reasonLabel = attr(form, s, "data-confirm-reason");
      var needsReason = !!reasonLabel;
      form.dataset.needsReason = needsReason ? "1" : "";
      reasonWrap.classList.toggle("hidden", !needsReason);
      reasonErr.classList.add("hidden");
      var remarksBox = document.getElementById("remarks");  // start from remarks already typed on the page
      reasonBox.value = needsReason && remarksBox ? remarksBox.value : "";
      if (needsReason) modal.querySelector("[data-confirm-reason-label]").textContent = reasonLabel;
      var ok = modal.querySelector("[data-confirm-ok]");
      ok.className = "btn " + (attr(form, s, "data-confirm-tone") || "primary");
      ok.textContent = attr(form, s, "data-confirm-ok") || "Confirm";
      modal.classList.remove("hidden");
      (needsReason ? reasonBox : ok).focus();
    }
  }, true);
  if (modal) {
    modal.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      if (!pendingForm) return;
      var f = pendingForm, s = pendingSubmitter;
      if (f.dataset.needsReason) {
        var text = reasonBox.value.trim();
        if (!text) { reasonErr.classList.remove("hidden"); reasonBox.focus(); return; }
        var hidden = f.querySelector('input[name="reason"]') || f.querySelector('[name="remarks"]');
        if (hidden) hidden.value = text;
      }
      f.dataset.confirmed = "1";
      modal.classList.add("hidden");
      pendingForm = null; pendingSubmitter = null;
      if (!f.requestSubmit) f.submit();
      else if (s) f.requestSubmit(s);
      else f.requestSubmit();
    });
    modal.querySelectorAll("[data-confirm-cancel]").forEach(function (b) { b.addEventListener("click", closeModal); });
    document.addEventListener("keydown", function (e) {
      if (modal.classList.contains("hidden")) return;
      if (e.key === "Escape") { closeModal(); return; }
      if (e.key === "Tab") {  // keep keyboard focus inside the dialog
        var items = Array.prototype.filter.call(modal.querySelectorAll("button, textarea"), function (el) {
          return el.offsetParent !== null; });
        var first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
  }

  // ---------------------------------------------------------------- busy state on submit
  // After a form is really submitted, disable its buttons and show progress so it cannot be sent
  // twice from the page (the server also rejects duplicates with one-time form tokens).
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (e.defaultPrevented || (form.hasAttribute("data-confirm") && !form.dataset.confirmed)) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    setTimeout(function () {
      form.querySelectorAll('button[type="submit"], button:not([type])').forEach(function (btn) {
        if (e.submitter && btn === e.submitter && btn.name) {  // keep the clicked button's value
          var h = document.createElement("input"); h.type = "hidden"; h.name = btn.name; h.value = btn.value; form.appendChild(h);
        }
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        if (btn.hasAttribute("data-busy")) btn.textContent = btn.getAttribute("data-busy");
      });
    }, 0);
  });
  document.querySelectorAll("[data-select-all]").forEach(function (el) {
    el.addEventListener("focus", function () { el.select(); });
  });

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
