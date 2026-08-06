/* MolMind Agent — history drawer + context tips (export / rename / delete) */
(function (global) {
  const LONG_PRESS_MS = 500;
  const MOVE_TOLERANCE = 20;
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

  function apiErrorMessage(body, fallback) {
    const detail = body && body.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    return fallback;
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

  function toLocalIso(value) {
    const d = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(d.getTime())) return "";
    const pad = (n, width = 2) => String(n).padStart(width, "0");
    const offsetMinutes = -d.getTimezoneOffset();
    const sign = offsetMinutes >= 0 ? "+" : "-";
    const absoluteOffset = Math.abs(offsetMinutes);
    const offset = `${sign}${pad(Math.floor(absoluteOffset / 60))}:${pad(absoluteOffset % 60)}`;
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
      `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.` +
      `${pad(d.getMilliseconds(), 3)}${offset}`
    );
  }

  const LOG_TIMESTAMP_KEYS = new Set([
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
    "expires_at",
    "used_at",
  ]);

  function localizeLogTimestamps(value) {
    if (Array.isArray(value)) return value.map(localizeLogTimestamps);
    if (!value || typeof value !== "object") return value;
    const localized = {};
    Object.entries(value).forEach(([key, item]) => {
      if (LOG_TIMESTAMP_KEYS.has(key) && typeof item === "string") {
        const localValue = toLocalIso(item);
        if (localValue) {
          localized[key] = localValue;
          localized[`${key}_utc`] = item;
          return;
        }
      }
      localized[key] = localizeLogTimestamps(item);
    });
    return localized;
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
    if (["queued", "running", "cancel_requested"].includes(String(session.run_status || ""))) {
      throw new Error("当前会话仍在执行，完成后才能删除");
    }
    const name = session.title || session.preview || "未命名对话";
    const ok = await openDeleteConfirm(name);
    if (!ok) return;
    const resp = await fetch(`/api/agent/sessions/${session.session_id}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(apiErrorMessage(err, "删除失败"));
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

  function buildCapabilityAudit(detail, events, settings) {
    const skillStates = Array.isArray(detail.installed_scp_skills)
      ? detail.installed_scp_skills
      : [];
    const toolOwners = new Map();
    const serverIndex = new Map();
    const registeredTools = [];
    for (const skill of settings && Array.isArray(settings.skills) ? settings.skills : []) {
      if (!skill || !skill.installed) continue;
      for (const toolId of Array.isArray(skill.tools) ? skill.tools : []) {
        if (!toolOwners.has(String(toolId))) {
          toolOwners.set(String(toolId), {
            plugin_id: skill.plugin_id || "",
            skill_id: skill.skill_id || skill.id || "",
            server_id: "",
          });
        }
      }
    }
    const installedSkills = skillStates.map((state) => {
      const servers = Array.isArray(state.servers) ? state.servers : [];
      for (const server of servers) {
        const serverId = String(server.server_id || "");
        if (serverId) {
          serverIndex.set(serverId, {
            server_id: serverId,
            title: server.title || serverId,
            endpoint: server.endpoint || "",
            skill_id: state.skill_id || "",
          });
        }
      }
      const descriptors = Array.isArray(state.tool_descriptors)
        ? state.tool_descriptors
        : [];
      for (const descriptor of descriptors) {
        const toolId = String(descriptor.tool_id || "");
        if (!toolId) continue;
        const owner = {
          plugin_id: descriptor.plugin_id || "scp-hub",
          skill_id: state.skill_id || "",
          server_id: descriptor.server_id || "",
        };
        toolOwners.set(toolId, owner);
        registeredTools.push({
          tool_id: toolId,
          ...owner,
          wire_tool_name: descriptor.wire_tool_name || "",
          descriptor_hash: descriptor.descriptor_hash || "",
          writes_selection: Boolean(descriptor.writes_selection),
        });
      }
      return {
        skill_id: state.skill_id || "",
        plugin_id: "scp-hub",
        enabled: Boolean(state.enabled),
        capability_ids: state.capability_ids || [],
        credential_status: state.credential_status || "unknown",
        tools: state.tools || [],
        servers: servers.map((server) => server.server_id).filter(Boolean),
      };
    });

    const plans = new Map();
    const calls = [];
    const openCalls = new Map();
    for (const event of Array.isArray(events) ? events : []) {
      if (!event || typeof event !== "object") continue;
      if (event.type === "agent_plan") {
        const diagnostics = Array.isArray(event.diagnostics) ? event.diagnostics : [];
        plans.set(String(event.run_id || ""), {
          capability_id: diagnostics[0] === "task_router" ? diagnostics[1] || "" : "",
          planner_status: diagnostics[0] === "task_router" ? diagnostics[2] || "" : "",
        });
        continue;
      }
      if (event.type === "tool_start") {
        const toolId = String(event.tool || "");
        const owner = toolOwners.get(toolId) || {};
        const plan = plans.get(String(event.run_id || "")) || {};
        const record = {
          call_id: event.call_id || "",
          run_id: event.run_id || "",
          seq_start: event.seq || null,
          seq_end: null,
          capability_id: event.capability_id || plan.capability_id || "",
          planner_status: plan.planner_status || "",
          plugin_id: event.plugin || owner.plugin_id || "molmind-core",
          skill_id: owner.skill_id || "",
          tool_id: toolId,
          source: event.source || "core",
          mcp_server_id: owner.server_id || "",
          arguments: event.args || {},
          status: "running",
          cache_status: "",
          response_hash: "",
          writes_selection: Boolean(event.writes_selection),
          ranking_changed: false,
          job_id: "",
          relevance_status: "",
          relevance_score: null,
          missing_concepts: [],
          excluded_concepts_present: [],
          protocol_validation: null,
          evidence_role: event.evidence_role || "primary_evidence",
          recovery_stage: event.recovery_stage || "",
          claim_scopes: event.claim_scopes || [],
        };
        calls.push(record);
        const key = String(event.call_id || `${event.run_id || ""}:${toolId}`);
        openCalls.set(key, record);
        continue;
      }
      if (event.type === "tool_end") {
        const toolId = String(event.tool || "");
        const key = String(event.call_id || `${event.run_id || ""}:${toolId}`);
        let record = openCalls.get(key);
        if (!record) {
          record = [...calls]
            .reverse()
            .find((item) => item.run_id === (event.run_id || "") && item.tool_id === toolId && !item.seq_end);
        }
        if (!record) continue;
        const digest = event.digest || (event.observation && event.observation.digest) || {};
        record.seq_end = event.seq || null;
        record.status = event.status || (event.ok ? "succeeded" : "failed");
        record.cache_status =
          digest.cache_status || (digest.status === "cache_hit" ? "cache_hit" : "unknown");
        record.response_hash = digest.response_hash || event.response_hash || "";
        record.writes_selection = Boolean(event.writes_selection || digest.writes_selection);
        record.ranking_changed = Boolean(event.ranking_changed);
        record.job_id = event.job_id || "";
        if (record.status !== "queued") openCalls.delete(key);
        continue;
      }
      if (event.type === "job_end") {
        const record = [...calls]
          .reverse()
          .find((item) => item.job_id && item.job_id === event.job_id);
        if (record) {
          record.seq_end = event.seq || record.seq_end;
          record.status = event.ok ? "succeeded" : "failed";
          record.cache_status =
            event.cache_status || (event.digest && event.digest.cache_status) || record.cache_status;
          record.response_hash = event.response_hash || record.response_hash;
        }
        continue;
      }
      if (event.type === "observation_validation") {
        const record = [...calls]
          .reverse()
          .find((item) => item.run_id === (event.run_id || "") && !item.relevance_status);
        if (record) {
          record.relevance_status = event.status || "";
          record.relevance_score = event.score == null ? null : event.score;
          record.missing_concepts = event.missing_concepts || [];
          record.excluded_concepts_present = event.excluded_concepts_present || [];
          record.protocol_validation = event.protocol_validation || null;
          record.claim_scopes = event.claim_scopes || record.claim_scopes;
        }
      }
    }

    const unique = (values) => [...new Set(values.filter(Boolean))].sort();
    const installedPluginIds = unique([
      ...(detail.installed_catalog || []),
      ...installedSkills.map((item) => item.plugin_id),
      ...((settings && Array.isArray(settings.plugins) ? settings.plugins : [])
        .filter((item) => item && item.installed)
        .map((item) => item.plugin_id || item.id || "")),
    ]);
    return {
      inventory: {
        installed_plugin_ids: installedPluginIds,
        installed_plugins: (settings && Array.isArray(settings.plugins)
          ? settings.plugins.filter((item) => item && item.installed)
          : []
        ).map((item) => ({
          plugin_id: item.plugin_id || item.id || "",
          title: item.title || "",
          builtin: Boolean(item.builtin),
          catalog: Boolean(item.catalog),
          network_policy: item.network_policy || {},
        })),
        available_skills: (settings && Array.isArray(settings.skills)
          ? settings.skills.filter((item) => item && item.installed)
          : []
        ).map((item) => ({
          skill_id: item.skill_id || item.id || "",
          plugin_id: item.plugin_id || "",
          capability_ids: item.capability_ids || [],
          tools: item.tools || [],
          builtin: Boolean(item.builtin),
        })),
        installed_scp_skills: installedSkills,
        registered_scp_tools: registeredTools,
        mcp_servers: [...serverIndex.values()],
      },
      usage_summary: {
        plugin_ids: unique(calls.map((item) => item.plugin_id)),
        skill_ids: unique(calls.map((item) => item.skill_id)),
        capability_ids: unique(calls.map((item) => item.capability_id)),
        tool_ids: unique(calls.map((item) => item.tool_id)),
        mcp_server_ids: unique(calls.map((item) => item.mcp_server_id)),
      },
      calls,
    };
  }

  async function exportSessionLog(session) {
    const sessionId = session.session_id;
    const [detailResp, eventsResp, settingsResp] = await Promise.all([
      fetch(`/api/agent/sessions/${sessionId}`),
      fetch(`/api/agent/sessions/${sessionId}/events`),
      fetch(`/api/agent/settings?session_id=${encodeURIComponent(sessionId)}`),
    ]);
    if (!detailResp.ok || !eventsResp.ok || !settingsResp.ok) {
      const failedResp = !detailResp.ok
        ? detailResp
        : !eventsResp.ok
          ? eventsResp
          : settingsResp;
      const err = await failedResp.json().catch(() => ({}));
      throw new Error(err.detail || "导出日志失败");
    }

    const [detail, eventData, settings] = await Promise.all([
      detailResp.json(),
      eventsResp.json(),
      settingsResp.json(),
    ]);
    const exportedAt = new Date();
    const exportedAtLocal = toLocalIso(exportedAt);
    const stamp = exportedAtLocal.replace(/[:.]/g, "-");
    const title = detail.title || session.title || session.preview || "未命名对话";
    const latestUserMessage = [...(detail.messages || [])]
      .reverse()
      .find((message) => message && message.role === "user" && message.text);
    const freshSummary = {
      ...session,
      session_id: detail.session_id,
      title: detail.title || session.title || null,
      preview: latestUserMessage ? String(latestUserMessage.text).slice(0, 120) : session.preview,
      created_at: detail.created_at || session.created_at,
      updated_at: detail.updated_at || session.updated_at,
      sdf_filename: detail.sdf_filename,
      has_sdf: Boolean(detail.has_sdf),
      profile_id: detail.profile_id,
      artifact_count: (detail.artifact_ids || []).length,
      event_seq: eventData.event_seq,
    };
    downloadJson(`MolMind_${safeFilename(title)}_${stamp}.json`, {
      format: "molmind-agent-session-log",
      format_version: 3,
      exported_at: exportedAtLocal,
      exported_at_utc: exportedAt.toISOString(),
      time_context: {
        display_timezone:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "local-browser-timezone",
        utc_offset_minutes: -exportedAt.getTimezoneOffset(),
        storage_timezone: "UTC",
      },
      session_summary: localizeLogTimestamps(freshSummary),
      capability_audit: localizeLogTimestamps(
        buildCapabilityAudit(detail, eventData.events || [], settings),
      ),
      conversation: localizeLogTimestamps(detail),
      execution: {
        event_seq: eventData.event_seq,
        events: localizeLogTimestamps(eventData.events || []),
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
      const resp = await fetch("/api/agent/sessions?limit=50");
      if (!resp.ok) throw new Error("无法加载会话历史");
      const data = await resp.json();
      return (data.sessions || [])
        .sort((a, b) => Date.parse(b.updated_at || b.created_at || "") - Date.parse(a.updated_at || a.created_at || ""));
    },

    registerSession() {
      // Sessions are listed from the server on demand; no local index is needed.
    },

    forgetSession() {
      // The next on-demand request is the source of truth.
    },

    async clearHistory() {
      const ok = await openClearHistoryConfirm();
      if (!ok) return false;
      const resp = await fetch("/api/agent/sessions", { method: "DELETE" });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(apiErrorMessage(err, "清空对话历史失败"));
      }
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
              <span class="mm-history-item-title">${escapeHtml(s.title || "未命名对话")}${["queued", "running", "cancel_requested"].includes(String(s.run_status || "")) ? " · 运行中" : ""}</span>
              <span class="mm-history-item-preview">${escapeHtml(s.preview || s.sdf_filename || s.session_id.slice(0, 8))}</span>
              <span class="mm-history-item-time">${escapeHtml(formatSessionTime(s.updated_at || s.created_at))}</span>
            </button>
            <button type="button" class="mm-history-item-more" title="更多" aria-label="更多操作">
              <span class="mm-icon mm-icon--dots mm-icon--md" aria-hidden="true"></span>
            </button>
          `;
          const main = row.querySelector(".mm-history-item-main");
          const more = row.querySelector(".mm-history-item-more");
          if (["queued", "running", "cancel_requested"].includes(String(s.run_status || ""))) {
            more.title = "运行中的会话不可删除";
          }
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
          <div class="mm-confirm-content">确定清空当前用户的全部对话历史吗？此操作不会影响其他用户 ID，且无法恢复。</div>
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
