/* MolMind Agent — live run status floats (inventory + step strip) */
(function (global) {
  const KIND_META = {
    skill: { label: "技能", icon: "sparkles" },
    tool: { label: "工具", icon: "tool" },
    plugin: { label: "插件", icon: "puzzle" },
    step: { label: "步骤", icon: "bolt" },
  };

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function shortText(s, max) {
    const t = String(s || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    if (t.length <= max) return t;
    return t.slice(0, Math.max(0, max - 1)) + "…";
  }

  function parsePlanStep(raw) {
    const text = String(raw || "").trim();
    const m = text.match(/^(Skill|Tool|Plugin)\s+([^\s：:]+)[：:：]?\s*(.*)$/i);
    if (m) {
      const kind = m[1].toLowerCase();
      return {
        kind: kind === "skill" || kind === "tool" || kind === "plugin" ? kind : "step",
        id: m[2],
        desc: (m[3] || "").trim() || text,
        raw: text,
      };
    }
    return { kind: "step", id: text, desc: text, raw: text };
  }

  /** Derive a short phase label from thinking text — never false-positive on “不调用筛选”. */
  function phaseFromThinking(text) {
    const t = String(text || "").trim();
    if (!t) return "处理中";
    if (/不调用筛选|一般问答|对话模型|纯对话|闲聊/.test(t)) return "理解问题";
    if (/理解你的需求|将调用技能|准备/.test(t) && !/不调用/.test(t)) return "规划中";
    if (/正在解析|解析 SDF/.test(t)) return "解析数据";
    if (/机制报告状态/.test(t)) return "生成机制报告";
    if (/enrichment|Catalog/.test(t)) return "插件增强";
    // Require positive screening intent; exclude “不…筛选”
    if (/(?:正在|开始|执行).*(?:筛选|排名)|score_and_rank|候选清单/.test(t)) {
      return "筛选排名";
    }
    if (/判断用户|确认/.test(t)) return "确认需求";
    return shortText(t, 18) || "处理中";
  }

  function humanToolLabel(tool) {
    const map = {
      score_and_rank: "筛选排名",
      export_nomination: "导出 CSV",
      start_mechanism_report: "机制报告",
      query_evidence: "证据查询",
      catalog_enrich: "插件增强",
    };
    return map[tool] || tool || "工具";
  }

  function createApi() {
    let inventoryEl = null;
    let inventoryListEl = null;
    let stepEl = null;
    let stepLabelEl = null;
    let tipEl = null;
    let tipHideTimer = null;
    let visible = false;
    let tipOpen = false;
    /** @type {{ key: string, kind: string, id: string, label: string, status: 'pending'|'active'|'done'|'error' }[]} */
    let items = [];
    /** @type {{ raw: string, kind: string, id: string, desc: string, status: 'pending'|'active'|'done'|'error' }[]} */
    let steps = [];
    let stepIndex = 0;
    let phase = "准备中";
    let hasPlan = false;
    let seenSeq = new Set();

    function ensureDom(chatMain, bottomSend) {
      if (!inventoryEl && chatMain) {
        inventoryEl = document.createElement("aside");
        inventoryEl.id = "agentRunInventory";
        inventoryEl.className = "mm-run-inventory";
        inventoryEl.setAttribute("aria-live", "polite");
        inventoryEl.setAttribute("aria-hidden", "true");
        inventoryEl.innerHTML = `
          <div class="mm-run-inventory-head">
            <span class="mm-run-inventory-pulse" aria-hidden="true"></span>
            <span class="mm-run-inventory-title">本次调用</span>
          </div>
          <ul class="mm-run-inventory-list"></ul>
        `;
        inventoryListEl = inventoryEl.querySelector(".mm-run-inventory-list");
        chatMain.appendChild(inventoryEl);
      }
      if (!stepEl && bottomSend) {
        stepEl = document.createElement("div");
        stepEl.id = "agentRunStep";
        stepEl.className = "mm-run-step";
        stepEl.setAttribute("aria-live", "polite");
        stepEl.setAttribute("aria-hidden", "true");
        stepEl.setAttribute("tabindex", "0");
        stepEl.innerHTML = `
          <span class="mm-run-step-dot" aria-hidden="true"></span>
          <span class="mm-run-step-label"></span>
          <div class="mm-run-step-tip" role="tooltip" aria-hidden="true">
            <div class="mm-run-step-tip-title">全部步骤</div>
            <ol class="mm-run-step-tip-list"></ol>
          </div>
        `;
        stepLabelEl = stepEl.querySelector(".mm-run-step-label");
        tipEl = stepEl.querySelector(".mm-run-step-tip");
        const toolbar = bottomSend.querySelector(".mm-bottom-send-toolbar");
        if (toolbar) bottomSend.insertBefore(stepEl, toolbar);
        else bottomSend.prepend(stepEl);
        bindStepTip();
      }
    }

    function bindStepTip() {
      if (!stepEl || !tipEl) return;
      const show = () => {
        if (tipHideTimer) {
          clearTimeout(tipHideTimer);
          tipHideTimer = null;
        }
        tipOpen = true;
        renderStepTip();
        tipEl.classList.add("is-open");
        tipEl.setAttribute("aria-hidden", "false");
      };
      const hideSoon = () => {
        if (tipHideTimer) clearTimeout(tipHideTimer);
        tipHideTimer = setTimeout(() => {
          tipOpen = false;
          tipEl.classList.remove("is-open");
          tipEl.setAttribute("aria-hidden", "true");
          tipHideTimer = null;
        }, 120);
      };
      stepEl.addEventListener("mouseenter", show);
      stepEl.addEventListener("mouseleave", hideSoon);
      tipEl.addEventListener("mouseenter", show);
      tipEl.addEventListener("mouseleave", hideSoon);
      stepEl.addEventListener("focus", show);
      stepEl.addEventListener("blur", hideSoon);
    }

    function itemKey(kind, id) {
      return `${kind}:${id}`;
    }

    function upsertItem({ kind, id, label, status }) {
      const k = itemKey(kind, id);
      let row = items.find((x) => x.key === k);
      if (!row) {
        row = {
          key: k,
          kind: kind || "tool",
          id: id || "",
          label: label || id || kind,
          status: status || "pending",
        };
        items.push(row);
      } else {
        if (label) row.label = label;
        if (status) {
          if (!(row.status === "done" && status === "pending")) {
            row.status = status;
          }
        }
      }
      return row;
    }

    function markOthersInactive(exceptKey) {
      items.forEach((it) => {
        if (it.key === exceptKey) return;
        if (it.status === "active") it.status = "done";
      });
    }

    function seedFromPlan(planSteps) {
      hasPlan = true;
      steps = (planSteps || []).map((s) => {
        const parsed = parsePlanStep(s);
        return {
          raw: parsed.raw,
          kind: parsed.kind,
          id: parsed.id,
          desc: parsed.desc,
          status: "pending",
        };
      });
      stepIndex = 0;
      (planSteps || []).forEach((s) => {
        const parsed = parsePlanStep(s);
        if (parsed.kind === "skill" || parsed.kind === "tool" || parsed.kind === "plugin") {
          upsertItem({
            kind: parsed.kind,
            id: parsed.id,
            label: parsed.id,
            status: "pending",
          });
        }
      });
      if (steps.length) {
        steps[0].status = "active";
        phase = shortText(steps[0].desc, 16) || "规划中";
      }
    }

    /** If backend emitted no plan yet, invent steps from thinking so strip stays truthful. */
    function ensureStepsFromThinking(text) {
      if (hasPlan || steps.length) return;
      const t = String(text || "");
      if (/一般问答|对话模型|不调用筛选|闲聊/.test(t)) {
        seedFromPlan(["理解问题", "生成对话回复"]);
        return;
      }
      if (/将调用技能|候选清单|机制/.test(t) && !/不调用/.test(t)) {
        // Wait for real plan event; show a soft placeholder only.
        steps = [
          {
            raw: "规划中",
            kind: "step",
            id: "规划中",
            desc: "规划中",
            status: "active",
          },
        ];
        stepIndex = 0;
        phase = "规划中";
        return;
      }
      steps = [
        {
          raw: shortText(t, 24) || "处理中",
          kind: "step",
          id: "thinking",
          desc: phaseFromThinking(t),
          status: "active",
        },
      ];
      stepIndex = 0;
    }

    function syncActiveStepFromThinking(text) {
      if (!steps.length) return;
      const t = String(text || "");
      const label = phaseFromThinking(t);
      // Chat: first step stays “理解问题” while identifying; second starts on reply.
      if (hasPlan && steps.length >= 2 && /一般问答|对话模型|不调用筛选/.test(t)) {
        steps[0].status = "active";
        steps[0].desc = label || "理解问题";
        for (let i = 1; i < steps.length; i++) {
          if (steps[i].status === "active") steps[i].status = "pending";
        }
        stepIndex = 0;
        phase = steps[0].desc;
        return;
      }
      // Prefer matching an existing pending/active step by keyword overlap.
      const idx = steps.findIndex((s, i) => {
        if (s.status === "done" || s.status === "error") return false;
        const blob = `${s.desc} ${s.raw} ${s.id}`;
        if (label && blob.includes(label)) return true;
        if (/规划/.test(label) && /规划|理解|Skill|技能/.test(blob)) return true;
        if (/解析/.test(label) && /解析|SDF/.test(blob)) return true;
        if (/机制/.test(label) && /机制|PDF|mechanism/i.test(blob)) return true;
        if (/筛选|排名|候选/.test(label) && /筛选|排名|候选|nominate/i.test(blob)) return true;
        if (/对话|回复|问答/.test(label) && /对话|回复|问答|理解/.test(blob)) return i === 0;
        return false;
      });
      if (idx >= 0) {
        for (let i = 0; i < idx; i++) {
          if (steps[i].status !== "error") steps[i].status = "done";
        }
        steps[idx].status = "active";
        stepIndex = idx;
        phase = shortText(steps[idx].desc, 16) || label;
      } else {
        phase = label;
        if (steps[stepIndex] && steps[stepIndex].status === "active") {
          // Keep strip label aligned with thinking without rewriting plan desc permanently
          phase = label || shortText(steps[stepIndex].desc, 16);
        }
      }
    }

    function advanceStepByTool(toolName) {
      if (!steps.length) return;
      const tool = String(toolName || "");
      let idx = steps.findIndex(
        (s) =>
          s.status !== "done" &&
          s.status !== "error" &&
          (s.id === tool ||
            s.raw.includes(tool) ||
            (tool === "score_and_rank" && /nominate|筛选|候选/i.test(s.raw)) ||
            (tool === "export_nomination" && /export|CSV|csv/i.test(s.raw)) ||
            (tool === "start_mechanism_report" && /mechanism|机制|PDF|pdf/i.test(s.raw)) ||
            (/enrich|catalog/i.test(tool) && /enrich|插件|Catalog/i.test(s.raw)))
      );
      if (idx < 0) {
        idx = steps.findIndex((s) => s.status === "pending" || s.status === "active");
      }
      if (idx < 0) return;
      for (let i = 0; i < idx; i++) {
        if (steps[i].status !== "error") steps[i].status = "done";
      }
      steps[idx].status = "active";
      stepIndex = idx;
      phase = shortText(steps[idx].desc, 16) || humanToolLabel(tool);
    }

    function completeActiveStep() {
      if (!steps.length) return;
      const cur = steps[stepIndex];
      if (cur && cur.status === "active") cur.status = "done";
      const next = steps.findIndex((s, i) => i > stepIndex && s.status === "pending");
      if (next >= 0) {
        steps[next].status = "active";
        stepIndex = next;
        phase = shortText(steps[next].desc, 16) || phase;
      }
    }

    function renderInventory() {
      if (!inventoryListEl) return;
      if (!items.length) {
        inventoryListEl.innerHTML =
          '<li class="mm-run-inventory-empty">等待调用清单…</li>';
        return;
      }
      inventoryListEl.innerHTML = items
        .map((it) => {
          const meta = KIND_META[it.kind] || KIND_META.step;
          let sideHtml = "";
          if (it.status === "done") {
            sideHtml = '<span class="mm-run-inv-badge mm-run-inv-badge--done">已完成</span>';
          } else if (it.status === "error") {
            sideHtml = '<span class="mm-run-inv-badge mm-run-inv-badge--error">失败</span>';
          }
          return `
            <li class="mm-run-inv-item mm-run-inv-item--${it.status}" data-status="${it.status}">
              <span class="mm-run-inv-icon-wrap">
                <span class="mm-icon mm-icon--${meta.icon} mm-icon--md" aria-hidden="true"></span>
              </span>
              <span class="mm-run-inv-meta">
                <span class="mm-run-inv-kind">${escapeHtml(meta.label)}</span>
                <span class="mm-run-inv-name">${escapeHtml(it.label)}</span>
              </span>
              <span class="mm-run-inv-side">${sideHtml}</span>
            </li>
          `;
        })
        .join("");
    }

    function currentStepDesc() {
      if (steps.length && steps[stepIndex]) {
        return phase || shortText(steps[stepIndex].desc, 16) || "处理中";
      }
      return phase || "处理中";
    }

    function renderStep() {
      if (!stepLabelEl) return;
      const total = Math.max(steps.length, 1);
      const current = steps.length ? Math.min(stepIndex + 1, total) : 1;
      stepLabelEl.textContent = `第${current}/${total}步 · ${currentStepDesc()}`;
      if (tipOpen) renderStepTip();
    }

    function renderStepTip() {
      if (!tipEl) return;
      const list = tipEl.querySelector(".mm-run-step-tip-list");
      if (!list) return;
      const rows = steps.length
        ? steps
        : [
            {
              desc: phase || "处理中",
              status: "active",
            },
          ];
      list.innerHTML = rows
        .map((s, i) => {
          const st = s.status || "pending";
          const label = shortText(s.desc || s.raw || `步骤 ${i + 1}`, 42) || `步骤 ${i + 1}`;
          return `
            <li class="mm-run-step-tip-item mm-run-step-tip-item--${st}">
              <span class="mm-run-step-tip-idx">${i + 1}</span>
              <span class="mm-run-step-tip-text">${escapeHtml(label)}</span>
              <span class="mm-run-step-tip-beam" aria-hidden="true"></span>
            </li>
          `;
        })
        .join("");
    }

    function paint() {
      renderInventory();
      renderStep();
    }

    function setVisible(on) {
      visible = !!on;
      if (inventoryEl) {
        inventoryEl.classList.toggle("is-visible", visible);
        inventoryEl.setAttribute("aria-hidden", visible ? "false" : "true");
      }
      if (stepEl) {
        stepEl.classList.toggle("is-visible", visible);
        stepEl.setAttribute("aria-hidden", visible ? "false" : "true");
        if (!visible && tipEl) {
          tipEl.classList.remove("is-open");
          tipEl.setAttribute("aria-hidden", "true");
          tipOpen = false;
        }
      }
      if (typeof document !== "undefined") {
        const root = document.getElementById("agentChatRoot");
        if (root) root.classList.toggle("mm-chat-root--run-active", visible);
      }
    }

    function reset() {
      items = [];
      steps = [];
      stepIndex = 0;
      phase = "准备中";
      hasPlan = false;
      seenSeq = new Set();
      paint();
    }

    function applyEvent(ev) {
      if (!ev || !ev.type) return;
      const seq = Number(ev.seq || 0);
      if (seq > 0) {
        if (seenSeq.has(seq)) return;
        seenSeq.add(seq);
      }
      const type = ev.type;

      if (type === "thinking" && ev.text) {
        ensureStepsFromThinking(ev.text);
        syncActiveStepFromThinking(ev.text);
        const skillMatch = String(ev.text).match(/将调用技能\s*\[([^\]]*)\]/);
        if (skillMatch) {
          const raw = skillMatch[1];
          raw.split(",").forEach((part) => {
            const id = part.replace(/['"\s]/g, "");
            if (id) upsertItem({ kind: "skill", id, label: id, status: "pending" });
          });
        }
        paint();
        return;
      }

      if (type === "plan") {
        seedFromPlan(ev.steps || []);
        phase = steps.length ? shortText(steps[0].desc, 16) || "规划中" : "规划中";
        paint();
        return;
      }

      if (type === "tool_start") {
        const tool = ev.tool || "tool";
        const plugin = ev.plugin || "";
        if (plugin) {
          upsertItem({
            kind: "plugin",
            id: plugin,
            label: plugin,
            status: "active",
          });
        }
        const row = upsertItem({
          kind: "tool",
          id: tool,
          label: tool,
          status: "active",
        });
        markOthersInactive(row.key);
        if (tool === "score_and_rank" || tool === "export_nomination") {
          const sk = items.find((x) => x.kind === "skill" && /nominate/i.test(x.id));
          if (sk && sk.status !== "done") sk.status = "active";
        }
        if (tool === "start_mechanism_report") {
          const sk = items.find((x) => x.kind === "skill" && /mechanism/i.test(x.id));
          if (sk && sk.status !== "done") sk.status = "active";
        }
        if (!steps.length) {
          seedFromPlan([`Tool ${tool}：${humanToolLabel(tool)}`]);
        }
        advanceStepByTool(tool);
        phase = humanToolLabel(tool);
        paint();
        return;
      }

      if (type === "tool_end") {
        const tool = ev.tool || "tool";
        const plugin = ev.plugin || "";
        const st = ev.ok === false ? "error" : "done";
        upsertItem({ kind: "tool", id: tool, label: tool, status: st });
        if (plugin) {
          const stillActive = items.some(
            (x) => x.kind === "tool" && x.status === "active" && x.id !== tool
          );
          if (!stillActive) {
            upsertItem({ kind: "plugin", id: plugin, label: plugin, status: st });
          }
        }
        if (tool === "export_nomination" || (tool === "score_and_rank" && ev.ok === false)) {
          const sk = items.find((x) => x.kind === "skill" && /nominate/i.test(x.id));
          if (sk) sk.status = st === "error" ? "error" : "done";
        }
        if (tool === "start_mechanism_report") {
          const sk = items.find((x) => x.kind === "skill" && /mechanism/i.test(x.id));
          if (sk) sk.status = st === "error" ? "error" : "done";
        }
        if (st === "error") {
          if (steps[stepIndex]) steps[stepIndex].status = "error";
          phase = `${humanToolLabel(tool)}失败`;
        } else {
          completeActiveStep();
          phase =
            steps[stepIndex] && steps[stepIndex].status === "active"
              ? shortText(steps[stepIndex].desc, 16)
              : "收尾中";
        }
        paint();
        return;
      }

      if (type === "governance_denied") {
        const tool = ev.tool || "tool";
        upsertItem({
          kind: "tool",
          id: tool,
          label: tool,
          status: "error",
        });
        if (steps[stepIndex]) steps[stepIndex].status = "error";
        phase = `${humanToolLabel(tool)}已拦截`;
        paint();
        return;
      }

      if (type === "query_plan" || type === "remote_start") {
        phase = "证据查询";
        if (!steps.length) seedFromPlan(["证据查询"]);
        upsertItem({
          kind: "tool",
          id: "query_evidence",
          label: "query_evidence",
          status: "active",
        });
        paint();
        return;
      }

      if (type === "query_summary") {
        upsertItem({
          kind: "tool",
          id: "query_evidence",
          label: "query_evidence",
          status: "done",
        });
        paint();
        return;
      }

      if (type === "loop_decision") {
        const decision = ev.decision || "final";
        phase =
          decision === "continue"
            ? "继续推理"
            : decision === "clarify"
              ? "等待补充信息"
              : decision === "abort"
                ? "已停止"
                : "整理最终答复";
        paint();
        return;
      }

      if (type === "log" && ev.message) {
        if (typeof ev.progress === "number") {
          phase = `筛选 ${Math.round(ev.progress)}%`;
        } else {
          phase = shortText(ev.message, 18) || phase;
        }
        paint();
        return;
      }

      if (type === "card") {
        phase = "产物就绪";
        const last = steps[steps.length - 1];
        if (last && /产物|卡片|返回/.test(last.raw)) {
          steps.forEach((s, i) => {
            if (i < steps.length - 1 && s.status !== "error") s.status = "done";
          });
          last.status = "active";
          stepIndex = steps.length - 1;
        }
        paint();
        return;
      }

      if (type === "assistant") {
        // Chat: move to last step (“生成对话回复”) then complete.
        if (steps.length >= 2 && /对话|回复|问答|理解问题/.test(steps.map((s) => s.desc).join(" "))) {
          for (let i = 0; i < steps.length - 1; i++) {
            if (steps[i].status !== "error") steps[i].status = "done";
          }
          stepIndex = steps.length - 1;
          steps[stepIndex].status = "active";
          phase = shortText(steps[stepIndex].desc, 16) || "生成回复";
        } else {
          phase = "生成回复";
        }
        // Finalize shortly — mark remaining done for strip consistency
        steps.forEach((s) => {
          if (s.status === "pending" || s.status === "active") s.status = "done";
        });
        items.forEach((it) => {
          if (it.status === "pending" || it.status === "active") it.status = "done";
        });
        if (steps.length) stepIndex = steps.length - 1;
        paint();
      }
    }

    return {
      mount({ chatMain, bottomSend } = {}) {
        ensureDom(chatMain, bottomSend);
        setVisible(false);
        reset();
        return this;
      },
      setVisible,
      reset,
      applyEvent,
      restore(events, runSnapshot) {
        reset();
        (events || []).forEach((event) => applyEvent(event));
        const status = String((runSnapshot && runSnapshot.status) || "");
        if (status === "queued" && !(events || []).length) {
          applyEvent({ type: "thinking", text: "任务排队中" });
        } else if (status === "running" && !(events || []).length) {
          applyEvent({ type: "thinking", text: "任务执行中" });
        }
        setVisible(["queued", "running", "cancel_requested"].includes(status));
      },
      setReconnectState(reconnecting) {
        if (reconnecting) {
          phase = "正在恢复连接";
          paint();
          setVisible(true);
        }
      },
      finalize() {
        setVisible(false);
      },
      isVisible() {
        return visible;
      },
    };
  }

  global.MolMindAgentRunStatus = createApi();
})(window);
