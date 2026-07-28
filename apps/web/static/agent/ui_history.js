/* MolMind Agent — history drawer + context tips (export / rename / delete) */
(function (global) {
  const LONG_PRESS_MS = 500;
  const MOVE_TOLERANCE = 20;
  const LOCAL_HISTORY_KEY = "molmind_agent_history_local_v1";
  const LOCAL_HISTORY_CACHE_KEY = "molmind_agent_history_cache_v1";

  function readLocalIds() {
    try {
      const value = JSON.parse(localStorage.getItem(LOCAL_HISTORY_KEY) || "[]");
      return Array.isArray(value) ? value.filter(Boolean) : [];
    } catch {
      return [];
    }
  }

  function writeLocalIds(ids) {
    try {
      localStorage.setItem(LOCAL_HISTORY_KEY, JSON.stringify([...new Set(ids)]));
    } catch {
      /* local storage may be unavailable */
    }
  }

  function readCachedSessions() {
    try {
      const value = JSON.parse(localStorage.getItem(LOCAL_HISTORY_CACHE_KEY) || "[]");
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  }

  function writeCachedSessions(sessions) {
    try {
      localStorage.setItem(LOCAL_HISTORY_CACHE_KEY, JSON.stringify(sessions.slice(0, 50)));
    } catch {
      /* local storage may be unavailable */
    }
  }

  function groupByDay(sessions) {
    const groups = { 今天: [], 昨天: [], 更早: [] };
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const startYesterday = startToday - 86400000;
    (sessions || []).forEach((s) => {
      const t = Date.parse(s.updated_at || s.created_at || "") || 0;
      if (t >= startToday) groups["今天"].push(s);
      else if (t >= startYesterday) groups["昨天"].push(s);
      else groups["更早"].push(s);
    });
    return groups;
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatSessionTime(iso) {
    const t = Date.parse(iso || "");
    if (!Number.isFinite(t) || !t) return "";
    const d = new Date(t);
    const pad = (n) => String(n).padStart(2, "0");
    const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const startYesterday = startToday - 86400000;
    if (t >= startYesterday) return hhmm;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hhmm}`;
  }

  let menuEl = null;
  let menuOverlay = null;
  let menuOpenedAt = 0;
  let longPressTimer = null;
  let longPressJustFired = false;
  let touchStartPos = { x: 0, y: 0 };
  let longPressTargetEl = null;
  let activeMenuSession = null;
  let refreshCallback = null;
  let onDeletedActive = null;

  function clearLongPress() {
    if (longPressTimer) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
    longPressTargetEl = null;
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onEnd);
  }

  function onMove(e) {
    const point = e.touches && e.touches[0] ? e.touches[0] : e;
    const dx = Math.abs((point.clientX || 0) - touchStartPos.x);
    const dy = Math.abs((point.clientY || 0) - touchStartPos.y);
    if (dx > MOVE_TOLERANCE || dy > MOVE_TOLERANCE) clearLongPress();
  }

  function onEnd() {
    clearLongPress();
  }

  function closeMenu() {
    if (menuOverlay && menuOverlay.parentNode) menuOverlay.parentNode.removeChild(menuOverlay);
    if (menuEl && menuEl.parentNode) menuEl.parentNode.removeChild(menuEl);
    menuOverlay = null;
    menuEl = null;
    activeMenuSession = null;
    menuOpenedAt = 0;
  }

  function placeMenu(x, y) {
    const menuEstH = 158;
    const menuHalfW = 80;
    let posX = x;
    let posY = y;
    if (posX - menuHalfW < 8) posX = menuHalfW + 8;
    if (posX + menuHalfW > window.innerWidth - 8) {
      posX = window.innerWidth - menuHalfW - 8;
    }
    if (posY + menuEstH > window.innerHeight - 8) {
      posY = Math.max(8, posY - menuEstH - 8);
    }
    return { x: posX, y: posY };
  }

  async function renameSession(session) {
    const cur = session.title || session.preview || "未命名对话";
    const next = await openRenameDialog(cur);
    if (next == null) return;
    const title = next.trim();
    if (!title || title === cur.trim()) return;
    const resp = await fetch(`/api/agent/sessions/${session.session_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "重命名失败");
    }
    if (refreshCallback) await refreshCallback();
  }

  async function deleteSession(session) {
    const name = session.title || session.preview || "未命名对话";
    const ok = await openDeleteConfirm(name);
    if (!ok) return;
    const resp = await fetch(`/api/agent/sessions/${session.session_id}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "删除失败");
    }
    if (onDeletedActive) onDeletedActive(session.session_id);
    MolMindAgentHistory.forgetSession(session.session_id);
    if (refreshCallback) await refreshCallback();
  }

  function safeFilename(value) {
    return (
      String(value || "未命名对话")
        .trim()
        .replace(/[\\/:*?"<>|\u0000-\u001f]/g, "_")
        .replace(/\s+/g, "_")
        .slice(0, 60) || "未命名对话"
    );
  }

  function downloadJson(filename, data) {
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function exportSessionLog(session) {
    const sessionId = session.session_id;
    const [detailResp, eventsResp] = await Promise.all([
      fetch(`/api/agent/sessions/${sessionId}`),
      fetch(`/api/agent/sessions/${sessionId}/events`),
    ]);
    if (!detailResp.ok || !eventsResp.ok) {
      const failedResp = !detailResp.ok ? detailResp : eventsResp;
      const err = await failedResp.json().catch(() => ({}));
      throw new Error(err.detail || "导出日志失败");
    }

    const [detail, eventData] = await Promise.all([detailResp.json(), eventsResp.json()]);
    const exportedAt = new Date();
    const stamp = exportedAt.toISOString().replace(/[:.]/g, "-");
    const title = detail.title || session.title || session.preview || "未命名对话";
    downloadJson(`MolMind_${safeFilename(title)}_${stamp}.json`, {
      format: "molmind-agent-session-log",
      format_version: 1,
      exported_at: exportedAt.toISOString(),
      session_summary: session,
      conversation: detail,
      execution: {
        event_seq: eventData.event_seq,
        events: eventData.events || [],
      },
    });
  }

  function mountConfirmMask(id, html, onReady) {
    return new Promise((resolve) => {
      const existing = document.getElementById(id);
      if (existing) existing.remove();

      const mask = document.createElement("div");
      mask.id = id;
      mask.className = "mm-confirm-mask";
      mask.setAttribute("role", "dialog");
      mask.setAttribute("aria-modal", "true");
      mask.innerHTML = html;

      let settled = false;
      const finish = (val) => {
        if (settled) return;
        settled = true;
        window.removeEventListener("keydown", onKey);
        mask.classList.remove("mm-confirm-mask--open");
        setTimeout(() => {
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        }, 220);
        resolve(val);
      };

      function onKey(e) {
        if (e.key === "Escape") {
          e.preventDefault();
          finish(false);
        }
      }

      mask.querySelector(".mm-confirm-cancel").addEventListener("click", (e) => {
        e.stopPropagation();
        finish(false);
      });
      mask.addEventListener("click", (e) => {
        if (e.target === mask) finish(false);
      });
      mask.querySelector(".mm-confirm-dialog").addEventListener("click", (e) => {
        e.stopPropagation();
      });
      window.addEventListener("keydown", onKey);

      document.body.appendChild(mask);
      requestAnimationFrame(() => mask.classList.add("mm-confirm-mask--open"));
      if (onReady) onReady(mask, finish);
    });
  }

  function openRenameDialog(currentTitle) {
    return mountConfirmMask(
      "agentRenameDialog",
      `
        <div class="mm-confirm-dialog">
          <div class="mm-confirm-header">
            <span class="mm-icon mm-icon--pencil mm-confirm-icon mm-confirm-icon--neutral" aria-hidden="true"></span>
            <h3 id="agentRenameDialogTitle">重命名对话</h3>
          </div>
          <div class="mm-confirm-content">
            <label class="mm-confirm-label" for="agentRenameInput">对话名称</label>
            <input
              id="agentRenameInput"
              class="mm-confirm-input"
              type="text"
              maxlength="80"
              autocomplete="off"
              spellcheck="false"
            />
          </div>
          <div class="mm-confirm-footer">
            <button type="button" class="mm-confirm-cancel">取消</button>
            <button type="button" class="mm-confirm-ok mm-confirm-ok--primary">保存</button>
          </div>
        </div>
      `,
      (mask, finish) => {
        mask.setAttribute("aria-labelledby", "agentRenameDialogTitle");
        const input = mask.querySelector("#agentRenameInput");
        const okBtn = mask.querySelector(".mm-confirm-ok");
        input.value = currentTitle || "";
        const submit = () => {
          const title = (input.value || "").trim();
          if (!title) {
            input.focus();
            input.classList.add("mm-confirm-input--invalid");
            return;
          }
          finish(title);
        };
        okBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          submit();
        });
        input.addEventListener("input", () => {
          input.classList.remove("mm-confirm-input--invalid");
        });
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        });
        requestAnimationFrame(() => {
          input.focus();
          input.select();
        });
      }
    ).then((val) => (val === false ? null : val));
  }

  function openDeleteConfirm(name) {
    return mountConfirmMask(
      "agentDeleteConfirm",
      `
        <div class="mm-confirm-dialog">
          <div class="mm-confirm-header">
            <span class="mm-icon mm-icon--circle-dashed-x mm-confirm-icon" aria-hidden="true"></span>
            <h3 id="agentDeleteConfirmTitle">删除会话</h3>
          </div>
          <div class="mm-confirm-content">
            确定删除「<strong></strong>」？此操作不可恢复。
          </div>
          <div class="mm-confirm-footer">
            <button type="button" class="mm-confirm-cancel">取消</button>
            <button type="button" class="mm-confirm-ok">确认删除</button>
          </div>
        </div>
      `,
      (mask, finish) => {
        mask.setAttribute("aria-labelledby", "agentDeleteConfirmTitle");
        const nameEl = mask.querySelector(".mm-confirm-content strong");
        if (nameEl) nameEl.textContent = name;
        mask.querySelector(".mm-confirm-ok").addEventListener("click", (e) => {
          e.stopPropagation();
          finish(true);
        });
      }
    );
  }

  function openMenu(session, clientX, clientY) {
    closeMenu();
    activeMenuSession = session;
    menuOpenedAt = Date.now();
    longPressJustFired = true;

    const pos = placeMenu(clientX, clientY + 4);
    menuOverlay = document.createElement("div");
    menuOverlay.className = "mm-context-menu-overlay";
    menuOverlay.addEventListener("click", (e) => {
      e.stopPropagation();
      if (Date.now() - menuOpenedAt < 350) return;
      closeMenu();
      setTimeout(() => {
        longPressJustFired = false;
      }, 50);
    });

    menuEl = document.createElement("div");
    menuEl.className = "mm-context-menu";
    menuEl.style.left = pos.x + "px";
    menuEl.style.top = pos.y + "px";
    menuEl.innerHTML = `
      <button type="button" class="mm-context-menu-item" data-act="export">
        <span class="mm-icon mm-icon--download mm-icon--md" aria-hidden="true"></span>
        <span>导出日志</span>
      </button>
      <button type="button" class="mm-context-menu-item" data-act="rename">
        <span class="mm-icon mm-icon--pencil mm-icon--md" aria-hidden="true"></span>
        <span>重命名</span>
      </button>
      <button type="button" class="mm-context-menu-item mm-context-menu-item--danger" data-act="delete">
        <span class="mm-icon mm-icon--trash mm-icon--md" aria-hidden="true"></span>
        <span>删除</span>
      </button>
    `;
    menuEl.addEventListener("click", async (e) => {
      e.stopPropagation();
      const btn = e.target.closest("[data-act]");
      if (!btn || !activeMenuSession) return;
      const act = btn.getAttribute("data-act");
      const target = activeMenuSession;
      closeMenu();
      longPressJustFired = true;
      try {
        if (act === "export") await exportSessionLog(target);
        if (act === "rename") await renameSession(target);
        if (act === "delete") await deleteSession(target);
      } catch (err) {
        alert(err.message || err);
      } finally {
        setTimeout(() => {
          longPressJustFired = false;
        }, 80);
      }
    });

    document.body.appendChild(menuOverlay);
    document.body.appendChild(menuEl);
    if (navigator.vibrate) navigator.vibrate(10);
  }

  function bindLongPress(itemEl, session) {
    const start = (e) => {
      clearLongPress();
      longPressJustFired = false;
      if (!e.touches) {
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup", onEnd);
      }
      const touch = (e.touches && e.touches[0]) || e;
      touchStartPos = { x: touch.clientX || 0, y: touch.clientY || 0 };
      longPressTargetEl = itemEl;
      longPressTimer = setTimeout(() => {
        const rect = (longPressTargetEl || itemEl).getBoundingClientRect();
        openMenu(session, touchStartPos.x || rect.left + rect.width / 2, rect.bottom);
      }, LONG_PRESS_MS);
    };
    itemEl.addEventListener("touchstart", start, { passive: true });
    itemEl.addEventListener("mousedown", start);
    itemEl.addEventListener("touchmove", onMove, { passive: true });
    itemEl.addEventListener("touchend", onEnd);
    itemEl.addEventListener("touchcancel", onEnd);
  }

  const MolMindAgentHistory = {
    async fetchSessions() {
      const localIds = new Set(readLocalIds());
      return readCachedSessions().filter((session) => localIds.has(session.session_id));
    },

    async syncSessions() {
      const resp = await fetch("/api/agent/sessions?limit=50");
      if (!resp.ok) throw new Error("无法加载会话历史");
      const data = await resp.json();
      const localIds = new Set(readLocalIds());
      const remote = (data.sessions || []).filter((session) => localIds.has(session.session_id));
      const merged = new Map(readCachedSessions().map((session) => [session.session_id, session]));
      remote.forEach((session) => merged.set(session.session_id, session));
      const sessions = [...merged.values()]
        .filter((session) => localIds.has(session.session_id))
        .sort((a, b) => Date.parse(b.updated_at || b.created_at || "") - Date.parse(a.updated_at || a.created_at || ""));
      writeCachedSessions(sessions);
      return sessions;
    },

    registerSession(sessionId) {
      if (!sessionId) return;
      writeLocalIds([...readLocalIds(), sessionId]);
    },

    forgetSession(sessionId) {
      writeLocalIds(readLocalIds().filter((id) => id !== sessionId));
      writeCachedSessions(readCachedSessions().filter((session) => session.session_id !== sessionId));
    },

    async clearLocalHistory() {
      const ok = await openClearHistoryConfirm();
      if (!ok) return false;
      writeLocalIds([]);
      writeCachedSessions([]);
      if (refreshCallback) await refreshCallback();
      return true;
    },

    renderList(listEl, sessions, onSelect, { activeId, countEl, onRefresh, onDeleted } = {}) {
      if (!listEl) return;
      refreshCallback = onRefresh || null;
      onDeletedActive = onDeleted || null;
      closeMenu();
      listEl.innerHTML = "";
      if (countEl) countEl.textContent = sessions.length ? `${sessions.length}` : "";
      if (!sessions.length) {
        listEl.innerHTML = '<p class="mm-history-empty">暂无对话历史</p>';
        return;
      }
      const groups = groupByDay(sessions);
      Object.keys(groups).forEach((label) => {
        const items = groups[label];
        if (!items.length) return;
        const h = document.createElement("div");
        h.className = "mm-history-date-header";
        h.textContent = label;
        listEl.appendChild(h);
        items.forEach((s) => {
          const row = document.createElement("div");
          row.className =
            "mm-history-item" +
            (activeId && s.session_id === activeId ? " mm-history-item--active" : "");
          row.innerHTML = `
            <button type="button" class="mm-history-item-main">
              <span class="mm-history-item-title">${escapeHtml(s.title || "未命名对话")}</span>
              <span class="mm-history-item-preview">${escapeHtml(s.preview || s.sdf_filename || s.session_id.slice(0, 8))}</span>
              <span class="mm-history-item-time">${escapeHtml(formatSessionTime(s.updated_at || s.created_at))}</span>
            </button>
            <button type="button" class="mm-history-item-more" title="更多" aria-label="更多操作">
              <span class="mm-icon mm-icon--dots mm-icon--md" aria-hidden="true"></span>
            </button>
          `;
          const main = row.querySelector(".mm-history-item-main");
          const more = row.querySelector(".mm-history-item-more");
          main.addEventListener("click", () => {
            if (longPressJustFired) {
              longPressJustFired = false;
              return;
            }
            if (onSelect) onSelect(s);
          });
          more.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            const rect = more.getBoundingClientRect();
            openMenu(s, rect.left + rect.width / 2, rect.bottom);
          });
          bindLongPress(row, s);
          listEl.appendChild(row);
        });
      });
    },

    open(root, panel) {
      if (root) {
        root.classList.add("mm-chat-root--drawer-open");
        root.classList.remove("mm-chat-root--settings-drawer");
      }
      if (panel) panel.classList.add("mm-history-drawer--open");
    },

    close(root, panel) {
      closeMenu();
      if (panel) panel.classList.remove("mm-history-drawer--open");
      if (root) {
        const open = root.querySelector(".mm-history-drawer--open");
        if (!open) {
          root.classList.remove("mm-chat-root--drawer-open");
          root.classList.remove("mm-chat-root--settings-drawer");
        }
      }
    },

    closeAll(root) {
      closeMenu();
      if (!root) return;
      root.querySelectorAll(".mm-history-drawer--open").forEach((p) => {
        p.classList.remove("mm-history-drawer--open");
      });
      root.classList.remove("mm-chat-root--drawer-open");
      root.classList.remove("mm-chat-root--settings-drawer");
    },
  };

  function openClearHistoryConfirm() {
    return mountConfirmMask(
      "agentClearHistoryConfirm",
      `
        <div class="mm-confirm-dialog">
          <div class="mm-confirm-header">
            <span class="mm-icon mm-icon--trash mm-confirm-icon" aria-hidden="true"></span>
            <h3 id="agentClearHistoryConfirmTitle">清空对话历史</h3>
          </div>
          <div class="mm-confirm-content">确定清空本机浏览器中的全部对话历史吗？云端会话不会被删除。</div>
          <div class="mm-confirm-footer">
            <button type="button" class="mm-confirm-cancel">取消</button>
            <button type="button" class="mm-confirm-ok">确认清空</button>
          </div>
        </div>
      `,
      (mask, finish) => {
        mask.setAttribute("aria-labelledby", "agentClearHistoryConfirmTitle");
        mask.querySelector(".mm-confirm-ok").addEventListener("click", (e) => {
          e.stopPropagation();
          finish(true);
        });
      }
    );
  }

  global.MolMindAgentHistory = MolMindAgentHistory;
})(window);
