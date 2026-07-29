/* MolMind Agent — settings / catalog drawer (Codex-inspired) */
(function (global) {
  /** Browser-local preferred Catalog installs (cross-session). null = never seeded. */
  const LEGACY_CATALOG_PREF_KEY = "molmind:agent_installed_catalog_v1";
  const CLIENT_ID =
    global.MolMindClientIdentity && global.MolMindClientIdentity.clientId
      ? global.MolMindClientIdentity.clientId
      : "anonymous";
  const CATALOG_PREF_KEY = `${LEGACY_CATALOG_PREF_KEY}:${CLIENT_ID}`;

  const TABS = [
    { id: "plugins", label: "插件" },
    { id: "tools", label: "工具" },
    { id: "skills", label: "技能" },
  ];

  /* Semantic icons (GameGhost Tabler set) — prefer exact id match */
  const ICON_BY_ID = {
    // plugins
    "molmind-core": "cpu",
    "origene-mcp": "connection",
    "aurobind": "link",
    "vcworld": "grain",
    "eva-rna": "comet",
    "enzyme-cage": "filter",
    // tools
    parse_sdf: "upload",
    score_and_rank: "sort-ascending",
    export_nomination: "download",
    query_evidence: "input-search",
    start_mechanism_report: "file",
    get_mechanism_job: "progress-help",
    get_run_audit: "shield-check",
    build_evidence_card: "cards",
    export_submission_bundle: "stack",
    draft_nomination_review: "file-pencil",
    apply_review: "circle-check",
    eval_goldset: "scale",
    predict_pl_fitness: "chart-infographic",
    mcp_query_opentargets: "world-search",
    mcp_query_chembl: "databricks",
    mcp_query_uniprot: "article",
    // skills
    masld_nominate: "list-numbers",
    masld_mechanism: "strategy",
    masld_explain: "message-cog",
    masld_export_bundle: "cloud-down",
    masld_full_submission: "checklist",
    enrich_mechanism_with_mcp: "sparkles",
    enrich_topn_with_aurobind: "bolt",
  };

  const ICON_POOL = [
    "puzzle",
    "cpu",
    "ai",
    "sparkles",
    "activity",
    "tool",
    "list",
    "list-numbers",
    "file",
    "settings",
    "bolt",
    "comet",
    "flame",
    "grain",
    "filter",
    "connection",
    "link",
    "code",
    "stack",
    "layout",
    "cards",
    "article",
    "badges",
    "checklist",
    "strategy",
    "tournament",
    "scale",
    "sort-ascending",
    "chart-circle",
    "chart-dots",
    "chart-infographic",
    "shield-check",
    "shield-chevron",
    "cloud-down",
    "share",
    "progress-help",
    "history",
    "clock",
    "rotate",
    "message-cog",
    "text-recognition",
    "info-hexagon",
    "databricks",
    "world-search",
    "input-search",
    "file-pencil",
    "file-text-shield",
    "square-plus",
  ];

  const state = {
    tab: "plugins",
    query: "",
    settings: null,
    sessionId: null,
    onChanged: null,
    tipId: null,
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hashIcon(id) {
    let h = 0;
    const str = String(id || "");
    for (let i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) >>> 0;
    return ICON_POOL[h % ICON_POOL.length];
  }

  function resolveIcon(id) {
    const key = String(id || "");
    if (ICON_BY_ID[key]) return ICON_BY_ID[key];
    // soft match by prefix / keyword
    const low = key.toLowerCase();
    if (low.includes("mcp") || low.includes("query")) return "world-search";
    if (low.includes("export") || low.includes("bundle")) return "cloud-down";
    if (low.includes("rank") || low.includes("score") || low.includes("sort")) return "sort-ascending";
    if (low.includes("pdf") || low.includes("report") || low.includes("mechanism")) return "file";
    if (low.includes("review")) return "file-pencil";
    if (low.includes("evidence") || low.includes("card")) return "cards";
    if (low.includes("enrich")) return "sparkles";
    if (low.includes("skill") || low.includes("masld")) return "strategy";
    return hashIcon(key);
  }

  function iconClass(id, size) {
    const name = resolveIcon(id);
    return `mm-icon mm-icon--${name}${size ? ` mm-icon--${size}` : ""}`;
  }

  function itemsForTab(settings, tab) {
    if (!settings) return [];
    if (tab === "plugins") return settings.plugins || [];
    if (tab === "tools") return settings.tools || [];
    if (tab === "skills") return settings.skills || [];
    return [];
  }

  function matchesQuery(item, q) {
    if (!q) return true;
    const hay = [item.title, item.id, item.plugin_id, item.description, item.tool_id, item.skill_id]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  }

  function normalizeCatalogIds(ids) {
    if (!Array.isArray(ids)) return [];
    const out = [];
    const seen = new Set();
    ids.forEach((id) => {
      const s = String(id || "").trim();
      if (!s || seen.has(s)) return;
      seen.add(s);
      out.push(s);
    });
    return out;
  }

  /** @returns {string[]|null} null when preference has never been written */
  function readPreferredCatalog() {
    try {
      let raw = localStorage.getItem(CATALOG_PREF_KEY);
      if (raw === null) {
        raw = localStorage.getItem(LEGACY_CATALOG_PREF_KEY);
        if (raw !== null) {
          localStorage.setItem(CATALOG_PREF_KEY, raw);
          localStorage.removeItem(LEGACY_CATALOG_PREF_KEY);
        }
      }
      if (raw === null) return null;
      return normalizeCatalogIds(JSON.parse(raw));
    } catch {
      return null;
    }
  }

  function writePreferredCatalog(ids) {
    try {
      localStorage.setItem(CATALOG_PREF_KEY, JSON.stringify(normalizeCatalogIds(ids)));
    } catch {
      /* ignore quota / private mode */
    }
  }

  function catalogIdsFromSettings(settings) {
    if (!settings) return [];
    return normalizeCatalogIds(
      (settings.plugins || [])
        .filter((p) => p && p.catalog && p.installed)
        .map((p) => p.install_target || p.plugin_id || p.id)
    );
  }

  function rememberInstall(pluginId) {
    const id = String(pluginId || "").trim();
    if (!id) return;
    let cur = readPreferredCatalog();
    if (cur === null) cur = catalogIdsFromSettings(state.settings);
    if (!cur.includes(id)) writePreferredCatalog([...cur, id]);
    else writePreferredCatalog(cur);
  }

  function rememberUninstall(pluginId) {
    const id = String(pluginId || "").trim();
    let cur = readPreferredCatalog();
    if (cur === null) cur = catalogIdsFromSettings(state.settings);
    writePreferredCatalog(cur.filter((x) => x !== id));
  }

  function closeTip() {
    state.tipId = null;
    document.querySelectorAll(".mm-cat-tip").forEach((el) => el.remove());
  }

  function placeTip(anchor, tip) {
    const panel = anchor.closest(".mm-history-drawer") || document.body;
    panel.appendChild(tip);
    const a = anchor.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    const tipW = tip.offsetWidth || 260;
    let left = a.left - p.left + a.width / 2 - tipW / 2;
    left = Math.max(12, Math.min(left, p.width - tipW - 12));
    let top = a.bottom - p.top + 10;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    requestAnimationFrame(() => {
      const tRect = tip.getBoundingClientRect();
      if (tRect.bottom > p.bottom - 8) {
        tip.style.top = `${Math.max(12, a.top - p.top - tRect.height - 10)}px`;
      }
    });
  }

  async function doInstall(targetId) {
    if (!state.sessionId || !targetId) return;
    await MolMindAgentSettings.install(state.sessionId, targetId);
    rememberInstall(targetId);
    if (state.onChanged) await state.onChanged();
  }

  async function doUninstall(targetId) {
    if (!state.sessionId || !targetId) return;
    await MolMindAgentSettings.uninstall(state.sessionId, targetId);
    rememberUninstall(targetId);
    closeTip();
    if (state.onChanged) await state.onChanged();
  }

  function openTip(anchor, item) {
    closeTip();
    const id = item.id || item.plugin_id;
    state.tipId = id;
    const tip = document.createElement("div");
    tip.className = "mm-cat-tip";
    tip.setAttribute("role", "dialog");
    tip.innerHTML = `
      <div class="mm-cat-tip-head">
        <span class="${iconClass(id, "lg")}" aria-hidden="true"></span>
        <div class="mm-cat-tip-titles">
          <div class="mm-cat-tip-title">${escapeHtml(item.title || id)}</div>
          <div class="mm-cat-tip-id">${escapeHtml(id)}</div>
        </div>
      </div>
      <p class="mm-cat-tip-desc">${escapeHtml(item.description || "暂无简介")}</p>
      <div class="mm-cat-tip-meta">
        ${item.plugin_id && item.plugin_id !== id ? `<span>插件 · ${escapeHtml(item.plugin_id)}</span>` : ""}
        ${item.builtin ? "<span>内置</span>" : ""}
        ${item.risk ? `<span>${escapeHtml(item.risk)}</span>` : ""}
      </div>
      <div class="mm-cat-tip-actions"></div>
    `;
    const actions = tip.querySelector(".mm-cat-tip-actions");
    if (item.can_uninstall && item.install_target) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mm-cat-btn mm-cat-btn--danger";
      btn.textContent = "卸载";
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        try {
          await doUninstall(item.install_target);
        } catch (err) {
          alert(err.message || err);
          btn.disabled = false;
        }
      });
      actions.appendChild(btn);
    } else if (item.builtin) {
      const note = document.createElement("span");
      note.className = "mm-cat-tip-note";
      note.textContent = "内置项，不可卸载";
      actions.appendChild(note);
    }
    placeTip(anchor, tip);
  }

  function renderInstalledGrid(items, rootEl) {
    const grid = document.createElement("div");
    grid.className = "mm-cat-installed";
    if (!items.length) {
      grid.innerHTML = '<p class="mm-cat-empty">暂无已安装项</p>';
      return grid;
    }
    items.forEach((item) => {
      const id = item.id || item.plugin_id;
      const tile = document.createElement("button");
      tile.type = "button";
      tile.className = "mm-cat-tile";
      tile.title = item.title || id;
      tile.setAttribute("aria-label", item.title || id);
      tile.innerHTML = `
        <span class="mm-cat-tile-face">
          <span class="${iconClass(id, "lg")}" aria-hidden="true"></span>
        </span>
        <span class="mm-cat-tile-name">${escapeHtml(item.title || id)}</span>
      `;
      tile.addEventListener("click", (e) => {
        e.stopPropagation();
        if (state.tipId === id) closeTip();
        else openTip(tile, item);
      });
      grid.appendChild(tile);
    });
    return grid;
  }

  function renderAvailableGrid(items, rootEl) {
    const grid = document.createElement("div");
    grid.className = "mm-cat-available";
    if (!items.length) {
      grid.innerHTML = '<p class="mm-cat-empty">没有更多可添加项</p>';
      return grid;
    }
    items.forEach((item) => {
      const id = item.id || item.plugin_id;
      const card = document.createElement("div");
      card.className = "mm-cat-card";
      card.innerHTML = `
        <div class="mm-cat-card-icon">
          <span class="${iconClass(id, "lg")}" aria-hidden="true"></span>
        </div>
        <div class="mm-cat-card-body">
          <div class="mm-cat-card-title">${escapeHtml(item.title || id)}</div>
          <div class="mm-cat-card-desc">${escapeHtml(item.description || "")}</div>
        </div>
        <div class="mm-cat-card-action"></div>
      `;
      const action = card.querySelector(".mm-cat-card-action");
      if (item.installed) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "mm-glass-btn mm-cat-more";
        more.setAttribute("aria-label", "更多");
        more.innerHTML = '<span class="mm-icon mm-icon--dots mm-icon--md" aria-hidden="true"></span>';
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          if (state.tipId === id) closeTip();
          else openTip(more, item);
        });
        action.appendChild(more);
      } else {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "mm-cat-btn mm-cat-btn--primary";
        btn.textContent = "安装";
        btn.disabled = !state.sessionId || !item.install_target;
        btn.title = state.sessionId
          ? item.install_target
            ? `安装 ${item.install_target}`
            : "不可安装"
          : "请先开始一次对话会话";
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!item.install_target) return;
          btn.disabled = true;
          try {
            await doInstall(item.install_target);
          } catch (err) {
            alert(err.message || err);
            btn.disabled = false;
          }
        });
        action.appendChild(btn);
      }
      grid.appendChild(card);
    });
    return grid;
  }

  function filteredItems() {
    const q = (state.query || "").trim().toLowerCase();
    const all = itemsForTab(state.settings, state.tab).filter((it) => matchesQuery(it, q));
    return {
      installed: all.filter((it) => it.installed),
      available: all.filter((it) => !it.installed),
    };
  }

  function paintLists(rootEl) {
    const { installed, available } = filteredItems();
    let lists = rootEl.querySelector(".mm-cat-lists");
    if (!lists) {
      lists = document.createElement("div");
      lists.className = "mm-cat-lists";
      rootEl.appendChild(lists);
    }
    lists.innerHTML = "";

    const secInstalled = document.createElement("section");
    secInstalled.className = "mm-cat-section";
    secInstalled.innerHTML = `<h4 class="mm-cat-section-title">已安装 <span>${installed.length}</span></h4>`;
    secInstalled.appendChild(renderInstalledGrid(installed, rootEl));
    lists.appendChild(secInstalled);

    const secAvail = document.createElement("section");
    secAvail.className = "mm-cat-section";
    secAvail.innerHTML = `<h4 class="mm-cat-section-title">可添加 <span>${available.length}</span></h4>`;
    secAvail.appendChild(renderAvailableGrid(available, rootEl));
    lists.appendChild(secAvail);
  }

  function syncTabs(rootEl) {
    rootEl.querySelectorAll(".mm-cat-tab").forEach((btn) => {
      const id = btn.dataset.tab;
      const on = id === state.tab;
      btn.classList.toggle("mm-cat-tab--active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    const input = rootEl.querySelector(".mm-cat-search-input");
    if (input) {
      const label = TABS.find((t) => t.id === state.tab)?.label || "";
      input.placeholder = `搜索${label}…`;
    }
  }

  function renderBody(rootEl) {
    if (!rootEl || !state.settings) return;
    rootEl.classList.add("mm-catalog-body");

    let toolbar = rootEl.querySelector(".mm-cat-toolbar");
    if (!toolbar) {
      rootEl.innerHTML = "";
      toolbar = document.createElement("div");
      toolbar.className = "mm-cat-toolbar";

      const tabs = document.createElement("div");
      tabs.className = "mm-cat-tabs";
      tabs.setAttribute("role", "tablist");
      TABS.forEach((t) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "mm-cat-tab";
        btn.dataset.tab = t.id;
        btn.setAttribute("role", "tab");
        btn.textContent = t.label;
        btn.addEventListener("click", () => {
          state.tab = t.id;
          closeTip();
          syncTabs(rootEl);
          paintLists(rootEl);
        });
        tabs.appendChild(btn);
      });

      const searchWrap = document.createElement("label");
      searchWrap.className = "mm-cat-search";
      searchWrap.innerHTML = `
        <span class="mm-icon mm-icon--search mm-icon--sm" aria-hidden="true"></span>
        <input type="search" class="mm-cat-search-input" placeholder="搜索…" autocomplete="off" />
      `;
      const input = searchWrap.querySelector("input");
      input.value = state.query || "";
      input.addEventListener("input", () => {
        state.query = input.value;
        closeTip();
        paintLists(rootEl);
      });

      toolbar.appendChild(tabs);
      toolbar.appendChild(searchWrap);
      rootEl.appendChild(toolbar);
    } else {
      const input = toolbar.querySelector(".mm-cat-search-input");
      if (input && input.value !== (state.query || "")) input.value = state.query || "";
    }

    syncTabs(rootEl);
    paintLists(rootEl);
  }

  const MolMindAgentSettings = {
    getPreferredCatalog() {
      return readPreferredCatalog();
    },

    /**
     * Apply browser-local Catalog prefs onto a session so plugins stay callable
     * after switching / creating chats. Seeds prefs once from sessionInstalled
     * when localStorage has never been written.
     */
    async syncPreferredToSession(sessionId, sessionInstalled) {
      if (!sessionId) return [];
      let preferred = readPreferredCatalog();
      if (preferred === null) {
        preferred = normalizeCatalogIds(sessionInstalled);
        writePreferredCatalog(preferred);
      }
      const prefSet = new Set(preferred);
      const cur = normalizeCatalogIds(sessionInstalled);
      const curSet = new Set(cur);

      for (const id of preferred) {
        if (curSet.has(id)) continue;
        try {
          await MolMindAgentSettings.install(sessionId, id);
          curSet.add(id);
        } catch {
          // Catalog entry may have been removed — drop from prefs.
          preferred = preferred.filter((x) => x !== id);
          writePreferredCatalog(preferred);
          prefSet.delete(id);
        }
      }
      for (const id of cur) {
        if (prefSet.has(id)) continue;
        try {
          await MolMindAgentSettings.uninstall(sessionId, id);
          curSet.delete(id);
        } catch {
          /* ignore */
        }
      }
      return preferred;
    },

    async fetchSettings(sessionId) {
      const q = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
      const resp = await fetch(`/api/agent/settings${q}`);
      if (!resp.ok) throw new Error("无法加载设置");
      return resp.json();
    },

    async install(sessionId, pluginId) {
      const resp = await fetch(`/api/agent/sessions/${sessionId}/catalog/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plugin_id: pluginId }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || "添加失败");
      }
      return resp.json();
    },

    async uninstall(sessionId, pluginId) {
      const resp = await fetch(
        `/api/agent/sessions/${sessionId}/catalog/${encodeURIComponent(pluginId)}`,
        { method: "DELETE" }
      );
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || "移除失败");
      }
      return resp.json();
    },

    render(rootEl, settings, { sessionId, onChanged } = {}) {
      if (!rootEl) return;
      state.settings = settings;
      state.sessionId = sessionId || null;
      state.onChanged = onChanged || null;
      closeTip();
      renderBody(rootEl);
    },

    open(root, panel) {
      if (root) {
        root.classList.add("mm-chat-root--drawer-open");
        root.classList.add("mm-chat-root--settings-drawer");
      }
      if (panel) panel.classList.add("mm-history-drawer--open");
    },

    close(root, panel) {
      closeTip();
      if (panel) panel.classList.remove("mm-history-drawer--open");
      if (root) {
        root.classList.remove("mm-chat-root--settings-drawer");
        const open = root.querySelector(".mm-history-drawer--open");
        if (!open) root.classList.remove("mm-chat-root--drawer-open");
      }
    },
  };

  document.addEventListener(
    "click",
    (e) => {
      if (!state.tipId) return;
      if (e.target.closest(".mm-cat-tip") || e.target.closest(".mm-cat-tile") || e.target.closest(".mm-cat-more")) {
        return;
      }
      closeTip();
    },
    true
  );

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeTip();
  });

  global.MolMindAgentSettings = MolMindAgentSettings;
})(window);
