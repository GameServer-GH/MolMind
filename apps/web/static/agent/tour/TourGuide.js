/**
 * Framework-agnostic Tour Guide (ported from GameGhost shared/tour).
 * 4-panel overlay so the spotlight hole can optionally pass clicks.
 */
(function (global) {
  const OVERLAY_Z = 9000;

  class TourGuide {
    constructor(steps, callbacks) {
      this.steps = steps || [];
      this.callbacks = callbacks || {};
      this.currentIndex = -1;
      this.active = false;
      this._panels = {};
      this._guardEl = null;
      this._retryTimer = null;
      this._resizeHandler = null;
      this._mutationObserver = null;
    }

    start(startIndex) {
      if (!this.steps.length) return;
      this._createPanels();
      this._bindEvents();
      this._goTo(Math.min(startIndex || 0, this.steps.length - 1));
      this.active = true;
      if (this.callbacks.onStart) this.callbacks.onStart();
    }

    next() {
      if (this.currentIndex < this.steps.length - 1) {
        this._clearRetry();
        this._goTo(this.currentIndex + 1);
      } else {
        this.skip();
      }
    }

    prev() {
      if (this.currentIndex > 0) {
        this._clearRetry();
        this._goTo(this.currentIndex - 1);
      }
    }

    skip() {
      this._finish();
    }

    destroy() {
      this._unbindEvents();
      this._clearRetry();
      Object.keys(this._panels).forEach((k) => {
        const p = this._panels[k];
        if (p && p.parentNode) p.parentNode.removeChild(p);
      });
      if (this._guardEl && this._guardEl.parentNode) {
        this._guardEl.parentNode.removeChild(this._guardEl);
      }
      this._guardEl = null;
      this._panels = {};
      this.active = false;
    }

    getCurrentStep() {
      return this.steps[this.currentIndex] || null;
    }

    isFirst() {
      return this.currentIndex === 0;
    }

    isLast() {
      return this.currentIndex >= this.steps.length - 1;
    }

    _goTo(index) {
      this.currentIndex = index;
      const step = this.steps[index];
      if (!step) return;
      if (typeof step.stepEnterAction === "function") {
        try {
          step.stepEnterAction();
        } catch (_) {
          /* ignore */
        }
      }
      if (this.callbacks.onStepChange) this.callbacks.onStepChange(step, index);
      const targetEl = this._resolveTarget(step);
      if (targetEl) {
        this._showSpotlight(targetEl, step.spotlightPadding != null ? step.spotlightPadding : 8);
      } else {
        this._retryFindTarget(step, index);
      }
    }

    _retryFindTarget(step, index, attempt) {
      this._clearRetry();
      const n = attempt || 0;
      if (n >= 30 || !this.active || this.currentIndex !== index) return;
      this._retryTimer = setTimeout(() => {
        if (!this.active || this.currentIndex !== index) return;
        const el = this._resolveTarget(step);
        if (el) {
          this._showSpotlight(el, step.spotlightPadding != null ? step.spotlightPadding : 8);
        } else {
          this._retryFindTarget(step, index, n + 1);
        }
      }, 300);
    }

    _clearRetry() {
      if (this._retryTimer) {
        clearTimeout(this._retryTimer);
        this._retryTimer = null;
      }
    }

    _resolveTarget(step) {
      if (!step.targetSelector) return null;
      if (typeof step.targetSelector === "string") {
        return document.querySelector(step.targetSelector);
      }
      if (typeof step.targetSelector === "function") return step.targetSelector();
      if (step.targetSelector instanceof Element) return step.targetSelector;
      return null;
    }

    _createPanels() {
      if (Object.keys(this._panels).length) return;
      const base =
        "position:fixed;z-index:" +
        OVERLAY_Z +
        ";background:rgba(0,0,0,0.55);pointer-events:all;transition:all 0.35s cubic-bezier(0.22,1,0.36,1);";
      ["top", "right", "bottom", "left"].forEach((pos) => {
        const panel = document.createElement("div");
        panel.className = "tour-overlay-panel tour-overlay-" + pos;
        panel.style.cssText = base + "top:0;left:0;width:0;height:0;";
        document.body.appendChild(panel);
        this._panels[pos] = panel;
      });
      const guard = document.createElement("div");
      guard.className = "tour-spotlight-guard";
      guard.style.cssText =
        "position:fixed;z-index:" +
        (OVERLAY_Z + 1) +
        ";pointer-events:all;background:rgba(255,255,255,0.3);transition:all 0.35s cubic-bezier(0.22,1,0.36,1);top:0;left:0;width:0;height:0;border-radius:12px;";
      document.body.appendChild(guard);
      this._guardEl = guard;
    }

    _showSpotlight(el, padding) {
      const pad = padding != null ? padding : 8;
      const rect = el.getBoundingClientRect();
      const x = rect.left - pad;
      const y = rect.top - pad;
      const w = rect.width + pad * 2;
      const h = rect.height + pad * 2;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const pfx =
        "position:fixed;z-index:" +
        OVERLAY_Z +
        ";background:rgba(0,0,0,0.55);pointer-events:all;transition:all 0.35s cubic-bezier(0.22,1,0.36,1);";

      if (this._panels.top) {
        this._panels.top.style.cssText =
          pfx + ";top:0;left:0;width:100vw;height:" + Math.max(0, y) + "px;";
      }
      if (this._panels.bottom) {
        this._panels.bottom.style.cssText =
          pfx +
          ";top:" +
          (y + h) +
          "px;left:0;width:100vw;height:" +
          Math.max(0, vh - y - h) +
          "px;";
      }
      if (this._panels.left) {
        this._panels.left.style.cssText =
          pfx +
          ";top:" +
          y +
          "px;left:0;width:" +
          Math.max(0, x) +
          "px;height:" +
          h +
          "px;";
      }
      if (this._panels.right) {
        this._panels.right.style.cssText =
          pfx +
          ";top:" +
          y +
          "px;left:" +
          (x + w) +
          "px;width:" +
          Math.max(0, vw - x - w) +
          "px;height:" +
          h +
          "px;";
      }
      if (this._guardEl) {
        this._guardEl.style.cssText =
          "position:fixed;z-index:" +
          (OVERLAY_Z + 1) +
          ";pointer-events:all;background:rgba(255,255,255,0.28);transition:all 0.35s cubic-bezier(0.22,1,0.36,1);top:" +
          y +
          "px;left:" +
          x +
          "px;width:" +
          w +
          "px;height:" +
          h +
          "px;border-radius:14px;box-shadow:0 0 0 2px rgba(0,90,255,0.35);";
      }
    }

    _updateSpotlight() {
      const step = this.steps[this.currentIndex];
      if (!step) return;
      const targetEl = this._resolveTarget(step);
      if (targetEl) {
        this._showSpotlight(targetEl, step.spotlightPadding != null ? step.spotlightPadding : 8);
      }
    }

    _bindEvents() {
      this._resizeHandler = () => {
        if (this.active) this._updateSpotlight();
      };
      window.addEventListener("resize", this._resizeHandler);
      this._mutationObserver = new MutationObserver((mutations) => {
        if (!this.active) return;
        const isOwn = mutations.some((m) =>
          m.target.classList && m.target.classList.contains("tour-overlay-panel")
        );
        if (isOwn) return;
        this._updateSpotlight();
      });
      this._mutationObserver.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["style", "class"],
      });
    }

    _unbindEvents() {
      if (this._resizeHandler) {
        window.removeEventListener("resize", this._resizeHandler);
        this._resizeHandler = null;
      }
      if (this._mutationObserver) {
        this._mutationObserver.disconnect();
        this._mutationObserver = null;
      }
    }

    _finish() {
      this._clearRetry();
      if (this.callbacks.onComplete) this.callbacks.onComplete();
      this.destroy();
    }
  }

  global.MolMindTourGuide = TourGuide;
})(window);
