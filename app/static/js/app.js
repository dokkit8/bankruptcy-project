console.log("APP.JS LOADED v=20260118-6");

const isSafari = (() => {
  const ua = navigator.userAgent;
  const isAppleWebKit = /AppleWebKit/.test(ua);
  const isChrome = /Chrome|CriOS|Chromium/.test(ua);
  const isFirefox = /Firefox|FxiOS/.test(ua);
  return isAppleWebKit && !isChrome && !isFirefox;
})();
if (isSafari) {
  document.documentElement.classList.add("is-safari");
}

document.addEventListener("DOMContentLoaded", () => {
  console.log("validation script loaded");
  document.body.classList.add("js-enabled");
  document.documentElement.classList.add("js-ready");
  document.documentElement.classList.add("page-enter");
  requestAnimationFrame(() => {
    document.documentElement.classList.add("page-entered");
    document.documentElement.classList.remove("page-enter");
  });

document.addEventListener("click", (e) => {
  const a = e.target.closest("a[href]");
  if (!a) return;
  const href = a.getAttribute("href") || "";
  const isInternal = href.startsWith("/") && !href.startsWith("//");
  if (!isInternal) return;
  if (document.documentElement.classList.contains("is-safari")) {
    document.documentElement.classList.add("no-blur");
    return;
  }
  document.documentElement.classList.add("page-leave");
}, true);

  function initPasswordToggles() {
    const toggles = document.querySelectorAll(".password-toggle");
    toggles.forEach((btn) => {
      const targetId = btn.dataset.target;
      const input = document.getElementById(targetId);
      if (!input) return;
      btn.addEventListener("click", () => {
        if (input.type === "password") {
          input.type = "text";
          btn.textContent = "🙈";
          btn.setAttribute("aria-label", "Скрыть пароль");
        } else {
          input.type = "password";
          btn.textContent = "👁";
          btn.setAttribute("aria-label", "Показать пароль");
        }
      });
    });
  }

  function initNumericForm(formId) {
    const form = document.getElementById(formId);
    if (!form) { console.warn(`${formId} not found`); return; }

    const fields = Array.from(form.querySelectorAll("[data-numeric='true']"));
    if (!fields.length) { console.warn(`no numeric fields found in ${formId}`); return; }

    const banner = form.querySelector(".form-error-banner");
    const lang = document.documentElement.lang === "en" ? "en" : "ru";
    const fieldMsg = lang === "en"
      ? (form.dataset.fieldEn || "Enter a number, e.g. 2 or 3.5")
      : (form.dataset.fieldRu || "Введите число, например 2 или 3.5");
    const re = /^-?\d+([.,]\d+)?$/;

    function ensureErrorEl(input) {
      let el = input.parentElement.querySelector(".field-error-text");
      if (!el) {
        el = document.createElement("div");
        el.className = "field-error-text";
        el.style.display = "none";
        input.parentElement.appendChild(el);
      }
      return el;
    }

    function setFieldState(input, ok) {
      const err = ensureErrorEl(input);
      if (ok) {
        input.classList.remove("input-error");
        err.textContent = "";
        err.style.display = "none";
      } else {
        input.classList.add("input-error");
        err.textContent = fieldMsg;
        err.style.display = "block";
      }
    }

    function validateOne(input) {
      const val = String(input.value ?? "").trim();
      const required = input.hasAttribute("required");
      if (!val) return !required;           // пусто ок, если не required
      return re.test(val);
    }

    form.addEventListener("submit", (e) => {
      let hasInvalid = false;
      let firstInvalid = null;

      fields.forEach((input) => {
        const ok = validateOne(input);
        setFieldState(input, ok);
        if (!ok) { hasInvalid = true; if (!firstInvalid) firstInvalid = input; }
        else {
          const trimmed = String(input.value ?? "").trim();
          if (trimmed.includes(",")) input.value = trimmed.replace(",", ".");
        }
      });

      if (hasInvalid) {
        e.preventDefault();
        if (banner) banner.hidden = false;
        firstInvalid?.focus();
      } else if (banner) {
        banner.hidden = true;
      }
    });

    form.addEventListener("input", (e) => {
      const input = e.target;
      if (!input.matches("[data-numeric='true']")) return;
      const ok = validateOne(input);
      setFieldState(input, ok);
      if (ok && banner) {
        const stillInvalid = fields.some((inp) => inp.classList.contains("input-error"));
        if (!stillInvalid) banner.hidden = true;
      }
    });

    console.log("validation attached", formId, fields.length);
  }

  function initModeSwitch() {
    const switcher = document.querySelector(".mode-switch");
    const panels = {
      bankruptcy: document.getElementById("panel-bankruptcy"),
      breakeven: document.getElementById("panel-breakeven"),
    };
    if (!switcher || !panels.bankruptcy || !panels.breakeven) return;
    const buttons = switcher.querySelectorAll(".mode-btn");
    const stored = localStorage.getItem("formMode");
    const initial = switcher.dataset.initialMode || "bankruptcy";
    let mode = stored || initial;

    function applyMode(next) {
      mode = next;
      localStorage.setItem("formMode", next);
      buttons.forEach((btn) => {
        const isActive = btn.dataset.mode === next;
        btn.classList.toggle("is-active", isActive);
      });
      panels.bankruptcy.classList.toggle("is-hidden", next !== "bankruptcy");
      panels.breakeven.classList.toggle("is-hidden", next !== "breakeven");
    }

    buttons.forEach((btn) => {
      btn.addEventListener("click", () => applyMode(btn.dataset.mode));
    });
    applyMode(mode);
  }

  initNumericForm("predict-form");
  initNumericForm("breakeven-form");
  initModeSwitch();
  initWhatIf();
  initReveal();
});

function initWhatIf() {
  const panel = document.getElementById("whatif-panel");
  if (!panel) return;

  const endpoint = panel.dataset.endpoint;
  let baseline = {};
  let baselineResult = {};
  try {
    baseline = JSON.parse(panel.dataset.baseline || "{}");
    baselineResult = JSON.parse(panel.dataset.baselineResult || "{}");
  } catch (e) {
    console.warn("Invalid baseline data");
  }

  const recalcMsg = panel.dataset.recalcMsg || "…";
  const readyMsg = panel.dataset.readyMsg || "";
  const errorMsg = panel.dataset.errorMsg || "Error";

  const statusEl = document.getElementById("whatif-status");
  const riskValueEls = [
    document.getElementById("whatif-risk-value"),
    document.getElementById("result-probability"),
  ];
  const chipEls = [
    document.getElementById("whatif-chip"),
    document.getElementById("result-chip"),
  ];

  function applyRisk(prob, label, color) {
    const probText = `${prob}%`;
    riskValueEls.forEach((el) => {
      if (!el) return;
      el.textContent = probText;
      el.style.color = color || el.style.color;
    });
    chipEls.forEach((el) => {
      if (!el) return;
      el.textContent = label;
      if (color) {
        el.style.color = color;
        el.style.backgroundColor = `${color}22`;
      }
    });
  }

  applyRisk(
    baselineResult.probability ?? 0,
    baselineResult.risk_label ?? "",
    baselineResult.risk_color ?? "#36CFC9"
  );

  const rows = Array.from(panel.querySelectorAll(".whatif-row"));
  let currentValues = { ...baseline };
  let debounceTimer = null;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function clampValue(val) {
    const num = Number(val);
    if (Number.isNaN(num)) return null;
    return Math.min(10, Math.max(0, num));
  }

  function triggerPredict() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runPredict, 320);
  }

  rows.forEach((row) => {
    const key = row.dataset.key;
    const slider = row.querySelector(".whatif-slider");
    const input = row.querySelector(".whatif-input");
    const reset = row.querySelector(".whatif-reset");
    const err = row.querySelector(".whatif-error");
    const base = baseline[key] ?? 0;

    const setValue = (val) => {
      const clamped = clampValue(val);
      if (clamped === null) {
        if (err) {
          err.textContent = errorMsg;
          err.style.display = "block";
        }
        return;
      }
      if (err) err.style.display = "none";
      if (slider) slider.value = clamped;
      if (input) input.value = clamped;
      currentValues[key] = clamped;
      if (reset) reset.disabled = Math.abs(clamped - base) < 1e-6;
      triggerPredict();
    };

    if (slider) {
      slider.value = base;
      slider.addEventListener("input", (e) => setValue(e.target.value));
    }
    if (input) {
      input.value = base;
      input.addEventListener("input", (e) => setValue(e.target.value));
    }
    if (reset) {
      reset.disabled = true;
      reset.title = panel.dataset.resetOneTip || reset.title;
      reset.addEventListener("click", () => {
        setValue(base);
      });
    }
  });

  const resetAllBtn = document.getElementById("whatif-reset-all");
  if (resetAllBtn) {
    resetAllBtn.addEventListener("click", () => {
      rows.forEach((row) => {
        const key = row.dataset.key;
        const slider = row.querySelector(".whatif-slider");
        const input = row.querySelector(".whatif-input");
        const err = row.querySelector(".whatif-error");
        const reset = row.querySelector(".whatif-reset");
        const base = baseline[key] ?? 0;
        if (slider) slider.value = base;
        if (input) input.value = base;
        if (err) err.style.display = "none";
        currentValues[key] = base;
        if (reset) reset.disabled = true;
      });
      applyRisk(
        baselineResult.probability ?? 0,
        baselineResult.risk_label ?? "",
        baselineResult.risk_color ?? "#36CFC9"
      );
      setStatus(readyMsg);
    });
  }

  async function runPredict() {
    setStatus(recalcMsg);
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentValues),
      });
      if (!res.ok) {
        let msg = errorMsg;
        try {
          const data = await res.json();
          if (data && data.detail) msg = Array.isArray(data.detail) ? data.detail.join(", ") : data.detail;
        } catch (_) {}
        throw new Error(msg);
      }
      const data = await res.json();
      applyRisk(data.probability, data.risk_label, data.risk_color || "#36CFC9");
      setStatus(readyMsg);
    } catch (e) {
      setStatus(errorMsg);
    }
  }
}

function initReveal() {
  const prefersReduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (prefersReduce) return;
  if (window.__revealInitDone) return;
  window.__revealInitDone = true;
  try {
    if (!window.__revealObserver && "IntersectionObserver" in window) {
      window.__revealObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (entry.isIntersecting) {
              entry.target.classList.add("is-visible");
              window.__revealObserver.unobserve(entry.target);
            }
          });
        },
        { threshold: 0.15 }
      );
    }
    const elements = document.querySelectorAll(".reveal");
    if (window.__revealObserver) {
      elements.forEach((el) => {
        if (!el.classList.contains("is-visible")) {
          window.__revealObserver.observe(el);
        }
      });
    } else {
      elements.forEach((el) => el.classList.add("is-visible"));
    }
  } catch (_) {
    document.querySelectorAll(".reveal").forEach((el) => el.classList.add("is-visible"));
  }
}
