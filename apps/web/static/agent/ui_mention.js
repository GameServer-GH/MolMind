/* MolMind Agent — / @ suggestions for user-facing plugins and skills. */
(function (global) {
  const KIND_META = {
    skill: { label: "技能", order: 0, icon: "sparkles" },
    plugin: { label: "插件", order: 1, icon: "puzzle" },
  };

  const KIND_ALIASES = {
    skill: "skill",
    skills: "skill",
    技能: "skill",
    plugin: "plugin",
    plugins: "plugin",
    插件: "plugin",
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function flattenSettings(settings) {
    if (!settings) return [];
    const out = [];
    (settings.skills || []).forEach((it) => {
      out.push({
        kind: "skill",
        id: it.id || it.skill_id,
        title: it.title || it.id || it.skill_id,
        description: it.description || "",
        installed: !!it.installed,
        plugin_id: it.plugin_id || "",
      });
    });
    (settings.plugins || []).forEach((it) => {
      out.push({
        kind: "plugin",
        id: it.id || it.plugin_id,
        title: it.title || it.id || it.plugin_id,
        description: it.description || "",
        installed: !!it.installed,
        plugin_id: it.plugin_id || it.id || "",
      });
    });
    return out.filter((x) => x.id && x.installed);
  }

  function parseSearchQuery(raw) {
    let q = String(raw || "").trim().toLowerCase();
    let kindHint = null;
    const prefixed = q.match(/^(skill|plugin)[:：]\s*(.*)$/i);
    if (prefixed) {
      kindHint = prefixed[1].toLowerCase();
      q = (prefixed[2] || "").trim();
      return { q, kindHint };
    }
    // 「技能 提名」/「插件」等
    for (const [alias, kind] of Object.entries(KIND_ALIASES)) {
      if (q === alias) return { q: "", kindHint: kind };
      if (q.startsWith(alias + " ") || q.startsWith(alias + ":") || q.startsWith(alias + "：")) {
        return {
          kindHint: kind,
          q: q.slice(alias.length).replace(/^[\s:：]+/, "").trim(),
        };
      }
      // 中文类别可直接粘连：技能提名
      if (/[\u4e00-\u9fff]/.test(alias) && q.startsWith(alias) && q.length > alias.length) {
        return { kindHint: kind, q: q.slice(alias.length).trim() };
      }
    }
    return { q, kindHint };
  }

  function scoreItem(item, q, kindHint) {
    if (kindHint && item.kind !== kindHint) return -1;
    const title = String(item.title || "").toLowerCase();
    const id = String(item.id || "").toLowerCase();
    const desc = String(item.description || "").toLowerCase();
    const plugin = String(item.plugin_id || "").toLowerCase();
    const kindLabel = (KIND_META[item.kind] && KIND_META[item.kind].label) || "";
    const hay = `${title} ${id} ${desc} ${plugin} ${item.kind} ${kindLabel}`;

    if (!q) return item.installed ? 20 : 10;

    const tokens = q.split(/[\s/|]+/).filter(Boolean);
    for (const t of tokens) {
      if (!hay.includes(t)) return -1;
    }

    let score = 0;
    if (title === q || id === q) score += 200;
    else if (title.startsWith(q) || id.startsWith(q)) score += 120;
    else if (title.includes(q) || id.includes(q)) score += 60;
    else score += 20;
    if (item.installed) score += 5;
    score += Math.max(0, 10 - ((KIND_META[item.kind] && KIND_META[item.kind].order) || 9));
    return score;
  }

  /** 光标前最近的 / 或 @ 触发段：{ trigger, query, start, end } */
  function findActiveTrigger(value, caret) {
    const before = value.slice(0, caret);
    // 允许中文等任意非空白字符用于搜索（空格结束触发段）
    const m = before.match(/(^|[\s\n\t])([@/])([^\s]*)$/);
    if (!m) return null;
    const trigger = m[2];
    const query = m[3] || "";
    // 已写成完整 @kind:id（纯 ascii id）则收起，等待空格后继续输入
    if (/^(plugin|skill|tool):[A-Za-z0-9][\w.\-]*$/.test(query)) return null;
    const start = caret - trigger.length - query.length;
    return { trigger, query, start, end: caret };
  }

  function kindGlyph(kind) {
    const meta = KIND_META[kind] || KIND_META.plugin;
    return (
      `<span class="mm-mention-glyph mm-mention-glyph--${kind}" aria-hidden="true">` +
      `<span class="mm-icon mm-icon--${meta.icon} mm-icon--sm"></span>` +
      `</span>`
    );
  }

  const MolMindAgentMention = {
    _items: [],
    _sessionId: null,
    _menu: null,
    _active: null,
    _index: 0,
    _flat: [],
    _input: null,
    _anchor: null,
    _lastQuery: null,
    _getSessionId: null,

    async refresh(sessionId) {
      const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
      const resp = await fetch(`/api/agent/settings${q}`);
      if (!resp.ok) return;
      const data = await resp.json();
      this._items = flattenSettings(data);
      this._sessionId = sessionId || null;
    },

    attach({ input, anchor, getSessionId }) {
      if (!input || !anchor) return;
      this._input = input;
      this._anchor = anchor;
      this._getSessionId = getSessionId || (() => null);

      const onInput = () => this._syncFromInput();
      const onKeydown = (e) => this._onKeydown(e);
      const onBlur = () => {
        setTimeout(() => {
          if (!this._menu || !this._menu.contains(document.activeElement)) {
            this.close();
          }
        }, 180);
      };

      input.addEventListener("input", onInput);
      input.addEventListener("keydown", onKeydown, true);
      input.addEventListener("click", onInput);
      input.addEventListener("keyup", onInput);
      input.addEventListener("blur", onBlur);

      document.addEventListener(
        "click",
        (e) => {
          if (!this._menu) return;
          if (e.target === input || this._menu.contains(e.target)) return;
          this.close();
        },
        true
      );

      this.refresh(this._getSessionId()).catch(() => {});
    },

    isOpen() {
      return !!(this._menu && this._menu.classList.contains("is-open"));
    },

    hasChoices() {
      return this.isOpen() && this._flat.length > 0;
    },

    close() {
      if (this._menu) {
        this._menu.classList.remove("is-open");
        this._menu.setAttribute("aria-hidden", "true");
        this._menu.innerHTML = "";
      }
      this._active = null;
      this._flat = [];
      this._index = 0;
      this._lastQuery = null;
    },

    _ensureMenu() {
      if (this._menu) return this._menu;
      const menu = document.createElement("div");
      menu.id = "agentMentionMenu";
      menu.className = "mm-mention-menu";
      menu.setAttribute("role", "listbox");
      menu.setAttribute("aria-label", "选择插件或技能");
      menu.setAttribute("aria-hidden", "true");
      this._anchor.appendChild(menu);
      this._menu = menu;
      return menu;
    },

    async _syncFromInput() {
      const input = this._input;
      if (!input) return;
      const caret = input.selectionStart ?? input.value.length;
      const active = findActiveTrigger(input.value, caret);
      if (!active) {
        this.close();
        return;
      }
      const sid = this._getSessionId();
      if (sid !== this._sessionId || !this._items.length) {
        try {
          await this.refresh(sid);
        } catch (_) {
          /* ignore */
        }
      }
      this._active = active;
      this._render(active);
    },

    _filtered(query) {
      const { q, kindHint } = parseSearchQuery(query);
      const scored = [];
      this._items.forEach((it) => {
        const score = scoreItem(it, q, kindHint);
        if (score >= 0) scored.push({ it, score });
      });
      scored.sort((a, b) => {
        if (b.score !== a.score) return b.score - a.score;
        const ao = KIND_META[a.it.kind]?.order ?? 9;
        const bo = KIND_META[b.it.kind]?.order ?? 9;
        if (ao !== bo) return ao - bo;
        return String(a.it.title).localeCompare(String(b.it.title), "zh");
      });
      return scored.map((x) => x.it).slice(0, 40);
    },

    _render(active) {
      const menu = this._ensureMenu();
      const items = this._filtered(active.query);
      if (this._lastQuery !== active.query) {
        this._index = 0;
        this._lastQuery = active.query;
      }
      this._flat = items;
      this._index = items.length ? Math.min(this._index, items.length - 1) : 0;

      if (!items.length) {
        const hint = active.query
          ? `无匹配「${escapeHtml(active.query)}」`
          : "无匹配项";
        menu.innerHTML = `<div class="mm-mention-empty">${hint} · 仅显示已安装项，可在设置中添加</div>`;
        menu.classList.add("is-open");
        menu.setAttribute("aria-hidden", "false");
        return;
      }

      const groups = { skill: [], tool: [], plugin: [] };
      items.forEach((it) => {
        if (groups[it.kind]) groups[it.kind].push(it);
      });

      let html = "";
      if (active.query) {
        html += `<div class="mm-mention-search-hint">匹配「${escapeHtml(active.query)}」</div>`;
      }
      ["skill", "tool", "plugin"].forEach((kind) => {
        const list = groups[kind];
        if (!list.length) return;
        html += `<div class="mm-mention-group">`;
        html += `<div class="mm-mention-group-title">${KIND_META[kind].label}</div>`;
        list.forEach((it) => {
          const globalIdx = items.indexOf(it);
          const activeCls = globalIdx === this._index ? " mm-mention-item--active" : "";
          html += `
            <button type="button" class="mm-mention-item${activeCls}" role="option"
              data-idx="${globalIdx}" aria-selected="${globalIdx === this._index ? "true" : "false"}">
              ${kindGlyph(kind)}
              <span class="mm-mention-body">
                <span class="mm-mention-title">${escapeHtml(it.title)}</span>
                <span class="mm-mention-desc">${escapeHtml(it.description || it.id)}</span>
              </span>
              <span class="mm-mention-token">${escapeHtml(active.trigger + kind + ":" + it.id)}</span>
            </button>`;
        });
        html += `</div>`;
      });

      menu.innerHTML = html;
      menu.classList.add("is-open");
      menu.setAttribute("aria-hidden", "false");

      menu.querySelectorAll(".mm-mention-item").forEach((btn) => {
        btn.addEventListener("mousedown", (e) => {
          e.preventDefault();
          const idx = Number(btn.dataset.idx);
          this._index = idx;
          this._applySelection();
        });
        btn.addEventListener("mouseenter", () => {
          this._index = Number(btn.dataset.idx);
          this._paintActive();
        });
      });

      this._scrollActiveIntoView();
    },

    _paintActive() {
      if (!this._menu) return;
      this._menu.querySelectorAll(".mm-mention-item").forEach((el) => {
        const idx = Number(el.dataset.idx);
        const on = idx === this._index;
        el.classList.toggle("mm-mention-item--active", on);
        el.setAttribute("aria-selected", on ? "true" : "false");
      });
      this._scrollActiveIntoView();
    },

    _scrollActiveIntoView() {
      if (!this._menu) return;
      const el = this._menu.querySelector(".mm-mention-item--active");
      if (el && el.scrollIntoView) {
        el.scrollIntoView({ block: "nearest" });
      }
    },

    _onKeydown(e) {
      if (!this.isOpen()) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        if (!this._flat.length) return;
        this._index = (this._index + 1) % this._flat.length;
        this._paintActive();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        if (!this._flat.length) return;
        this._index = (this._index - 1 + this._flat.length) % this._flat.length;
        this._paintActive();
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        if (e.isComposing || e.keyCode === 229) return;
        if (!this._flat.length) {
          this.close();
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        this._applySelection();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        this.close();
        return;
      }
      if (e.key === "Tab") {
        if (this._flat.length) {
          e.preventDefault();
          e.stopPropagation();
          this._applySelection();
        }
      }
    },

    _applySelection() {
      const input = this._input;
      const active = this._active;
      const item = this._flat[this._index];
      if (!input || !active || !item) {
        this.close();
        return;
      }
      const token = `${active.trigger}${item.kind}:${item.id} `;
      const before = input.value.slice(0, active.start);
      const after = input.value.slice(active.end);
      input.value = before + token + after;
      const pos = before.length + token.length;
      input.focus();
      input.setSelectionRange(pos, pos);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      this.close();
    },
  };

  global.MolMindAgentMention = MolMindAgentMention;
})(window);
