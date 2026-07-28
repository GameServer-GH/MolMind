/* MolMind Agent — left turn rail (scroll spy + hover preview) */
(function (global) {
  function truncate(text, maxChars) {
    const t = String(text || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    if (t.length <= maxChars) return t;
    return t.slice(0, maxChars - 1) + "…";
  }

  function MolMindAgentTurnRail() {
    this.scrollEl = null;
    this.messagesEl = null;
    this.railEl = null;
    this.ticksEl = null;
    this.previewEl = null;
    this._turns = [];
    this._activeId = null;
    this._hoverId = null;
    this._raf = 0;
    this._mo = null;
    this._onScroll = null;
    this._onResize = null;
  }

  MolMindAgentTurnRail.prototype.mount = function (opts) {
    this.scrollEl = opts.scrollEl || null;
    this.messagesEl = opts.messagesEl || null;
    this.railEl = opts.railEl || null;
    if (!this.scrollEl || !this.messagesEl || !this.railEl) return this;

    this.ticksEl = this.railEl.querySelector(".mm-turn-rail-ticks");
    this.previewEl = this.railEl.querySelector(".mm-turn-rail-preview");
    if (!this.ticksEl) {
      this.ticksEl = document.createElement("div");
      this.ticksEl.className = "mm-turn-rail-ticks";
      this.railEl.appendChild(this.ticksEl);
    }
    if (!this.previewEl) {
      this.previewEl = document.createElement("div");
      this.previewEl.className = "mm-turn-rail-preview";
      this.previewEl.setAttribute("aria-hidden", "true");
      this.railEl.appendChild(this.previewEl);
    }

    this._onScroll = () => this._scheduleSync();
    this._onResize = () => this._scheduleRebuild();
    this.scrollEl.addEventListener("scroll", this._onScroll, { passive: true });
    window.addEventListener("resize", this._onResize, { passive: true });

    if (typeof MutationObserver !== "undefined") {
      this._mo = new MutationObserver(() => {
        const n = this.messagesEl.querySelectorAll(".mm-turn").length;
        if (n !== this._turns.length) this._scheduleRebuild();
        else this._scheduleSync();
      });
      this._mo.observe(this.messagesEl, { childList: true, subtree: true });
    }

    this.rebuild();
    return this;
  };

  MolMindAgentTurnRail.prototype.destroy = function () {
    if (this.scrollEl && this._onScroll) {
      this.scrollEl.removeEventListener("scroll", this._onScroll);
    }
    if (this._onResize) window.removeEventListener("resize", this._onResize);
    if (this._mo) this._mo.disconnect();
    this._mo = null;
  };

  MolMindAgentTurnRail.prototype._scheduleRebuild = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this.rebuild();
    });
  };

  MolMindAgentTurnRail.prototype._scheduleSync = function () {
    if (this._raf) cancelAnimationFrame(this._raf);
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this.syncActive();
    });
  };

  MolMindAgentTurnRail.prototype.rebuild = function () {
    if (!this.messagesEl || !this.ticksEl || !this.railEl) return;
    const nodes = Array.from(this.messagesEl.querySelectorAll(".mm-turn"));
    this._turns = nodes.map((el, i) => {
      const full =
        el.getAttribute("data-turn-text") ||
        (el.querySelector(".mm-msg-user-hint") || {}).getAttribute?.("data-turn-text") ||
        (el.querySelector(".mm-msg-user-text") || {}).textContent ||
        "";
      const id = el.getAttribute("data-turn-id") || `turn-${i}`;
      if (!el.getAttribute("data-turn-id")) el.setAttribute("data-turn-id", id);
      if (!el.getAttribute("data-turn-text") && full) {
        el.setAttribute("data-turn-text", full);
      }
      return { id, el, text: String(full || "").trim() };
    });

    this.ticksEl.innerHTML = "";
    const n = this._turns.length;
    if (n === 0) {
      this.railEl.classList.add("mm-turn-rail--empty");
      this.railEl.setAttribute("aria-hidden", "true");
      this._hidePreview();
      return;
    }
    this.railEl.classList.remove("mm-turn-rail--empty");
    this.railEl.setAttribute("aria-hidden", "false");

    this._turns.forEach((turn, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mm-turn-rail-tick";
      btn.dataset.turnId = turn.id;
      btn.setAttribute("aria-label", `跳转到第 ${i + 1} 轮对话`);
      btn.title = truncate(turn.text, 80) || `对话 ${i + 1}`;

      const bar = document.createElement("span");
      bar.className = "mm-turn-rail-tick-bar";
      btn.appendChild(bar);

      btn.addEventListener("mouseenter", () => this._showPreview(turn, btn));
      btn.addEventListener("mouseleave", () => this._hidePreview());
      btn.addEventListener("focus", () => this._showPreview(turn, btn));
      btn.addEventListener("blur", () => this._hidePreview());
      btn.addEventListener("click", () => {
        turn.el.scrollIntoView({ behavior: "smooth", block: "start" });
        this.setActive(turn.id);
      });

      this.ticksEl.appendChild(btn);
    });

    this.syncActive();
  };

  MolMindAgentTurnRail.prototype._showPreview = function (turn, tickBtn) {
    if (!this.previewEl || !turn) return;
    this._hoverId = turn.id;
    const text = truncate(turn.text, 36) || "（无文字）";
    this.previewEl.textContent = text;
    this.previewEl.classList.add("is-visible");
    this.previewEl.setAttribute("aria-hidden", "false");

    // Align preview with hovered tick (right of the rail).
    const railBox = this.railEl.getBoundingClientRect();
    const tickBox = tickBtn.getBoundingClientRect();
    const top = tickBox.top - railBox.top + tickBox.height / 2;
    this.previewEl.style.top = `${Math.max(8, top)}px`;
  };

  MolMindAgentTurnRail.prototype._hidePreview = function () {
    this._hoverId = null;
    if (!this.previewEl) return;
    this.previewEl.classList.remove("is-visible");
    this.previewEl.setAttribute("aria-hidden", "true");
    this.previewEl.textContent = "";
  };

  MolMindAgentTurnRail.prototype.setActive = function (id) {
    this._activeId = id || null;
    if (!this.ticksEl) return;
    this.ticksEl.querySelectorAll(".mm-turn-rail-tick").forEach((btn) => {
      const on = btn.dataset.turnId === this._activeId;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-current", on ? "true" : "false");
    });
  };

  MolMindAgentTurnRail.prototype.syncActive = function () {
    if (!this.scrollEl || !this._turns.length) {
      this.setActive(null);
      return;
    }
    const scrollBox = this.scrollEl.getBoundingClientRect();
    // Prefer the last turn whose top has crossed ~28% of the viewport.
    const markerY = scrollBox.top + scrollBox.height * 0.28;
    let active = this._turns[0];
    for (let i = 0; i < this._turns.length; i += 1) {
      const box = this._turns[i].el.getBoundingClientRect();
      if (box.top <= markerY + 8) active = this._turns[i];
      else break;
    }
    // Near bottom: lock to last turn.
    const nearBottom =
      this.scrollEl.scrollTop + this.scrollEl.clientHeight >=
      this.scrollEl.scrollHeight - 24;
    if (nearBottom) active = this._turns[this._turns.length - 1];
    this.setActive(active ? active.id : null);
  };

  global.MolMindAgentTurnRail = MolMindAgentTurnRail;
})(window);
