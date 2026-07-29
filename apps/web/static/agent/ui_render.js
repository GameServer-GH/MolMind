/* MolMind Agent UI — GameGhost-inspired render (hint / thinking / cards) */
(function (global) {
  function icon(name, extraClass) {
    const span = document.createElement("span");
    span.className = "mm-icon mm-icon--" + name + (extraClass ? " " + extraClass : "");
    span.setAttribute("aria-hidden", "true");
    return span;
  }

  function truncateHint(text, maxChars) {
    const t = String(text || "").replace(/\s+/g, " ").trim();
    if (t.length <= maxChars) return t;
    return t.slice(0, maxChars - 1) + "…";
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function safeDisplayText(value, maxChars) {
    let text = String(value == null ? "" : value);
    text = text.replace(/\bBearer\s+[A-Za-z0-9._~+\-/=]+/gi, "Bearer [REDACTED]");
    text = text.replace(
      /\b(api[_-]?key|access[_-]?token|authorization|secret|password)\s*[:=]\s*[^\s,;&]+/gi,
      "$1=[REDACTED]"
    );
    const limit = maxChars || 1000;
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }

  function safeToolArgs(args) {
    const blocked = /authorization|api[_-]?key|access[_-]?token|secret|password|credential|headers?/i;
    const copy = {};
    Object.entries(args || {}).forEach(([key, value]) => {
      if (blocked.test(key)) return;
      if (Array.isArray(value)) {
        copy[key] = value.slice(0, 30).map((item) => safeDisplayText(item, 160));
      } else if (value && typeof value === "object") {
        const nested = {};
        Object.entries(value).forEach(([nestedKey, nestedValue]) => {
          if (!blocked.test(nestedKey)) nested[nestedKey] = safeDisplayText(nestedValue, 160);
        });
        copy[key] = nested;
      } else if (typeof value === "string") {
        copy[key] = safeDisplayText(value, 240);
      } else {
        copy[key] = value;
      }
    });
    return copy;
  }

  function isMdTableSep(line) {
    return /^\s*\|?[\s:\-|]+\|[\s:\-|]*\|?\s*$/.test(line) && /\|/.test(line) && /-/.test(line);
  }

  function splitMdRow(line) {
    let s = String(line || "").trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }

  function inlineHtml(text) {
    // Escape first, then light inline marks.
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return s;
  }

  /** Lightweight markdown: paragraphs + pipe tables (no external deps). */
  function renderAssistantHtml(text) {
    const raw = String(text || "").replace(/\r\n/g, "\n").trim();
    if (!raw) return "";
    const lines = raw.split("\n");
    const chunks = [];
    let i = 0;
    let para = [];

    const flushPara = () => {
      if (!para.length) return;
      const body = para.join("\n").trim();
      if (body) {
        chunks.push(`<p class="mm-md-p">${inlineHtml(body).replace(/\n/g, "<br>")}</p>`);
      }
      para = [];
    };

    while (i < lines.length) {
      const line = lines[i];
      const next = lines[i + 1] || "";
      const looksTable =
        line.includes("|") && isMdTableSep(next) && splitMdRow(line).length >= 2;

      if (looksTable) {
        flushPara();
        const header = splitMdRow(line);
        i += 2; // skip header + separator
        const bodyRows = [];
        while (i < lines.length && lines[i].includes("|") && !isMdTableSep(lines[i])) {
          const cells = splitMdRow(lines[i]);
          if (cells.some((c) => c.length)) bodyRows.push(cells);
          i += 1;
        }
        const align = splitMdRow(next).map((cell) => {
          const t = cell.trim();
          if (/^:?-+:$/.test(t)) return "center";
          if (/^-+:$/.test(t)) return "right";
          return "left";
        });
        let html = '<div class="mm-md-table-wrap"><table class="mm-md-table"><thead><tr>';
        header.forEach((h, idx) => {
          html += `<th style="text-align:${align[idx] || "left"}">${inlineHtml(h)}</th>`;
        });
        html += "</tr></thead><tbody>";
        bodyRows.forEach((row) => {
          html += "<tr>";
          for (let c = 0; c < header.length; c += 1) {
            html += `<td style="text-align:${align[c] || "left"}">${inlineHtml(row[c] || "")}</td>`;
          }
          html += "</tr>";
        });
        html += "</tbody></table></div>";
        chunks.push(html);
        continue;
      }

      if (!line.trim()) {
        flushPara();
        i += 1;
        continue;
      }
      para.push(line);
      i += 1;
    }
    flushPara();
    return chunks.join("");
  }

  function buildUserAsk(text, attachments) {
    const ask = document.createElement("div");
    ask.className = "mm-turn-ask";

    const el = document.createElement("div");
    el.className = "mm-msg-user-hint";
    el.title = text || "";
    el.setAttribute("data-turn-text", text || "");
    el.appendChild(icon("activity", "mm-icon--sm"));
    const span = document.createElement("span");
    span.className = "mm-msg-user-text";
    span.textContent = truncateHint(text, 48);
    el.appendChild(span);
    ask.appendChild(el);

    const atts = Array.isArray(attachments) ? attachments.filter(Boolean) : [];
    if (atts.length) {
      const rail = document.createElement("div");
      rail.className = "mm-msg-user-attach";
      atts.forEach((att) => {
        const name = att.filename || att.name || "附件";
        const chip = document.createElement("div");
        chip.className = "mm-msg-user-attach-chip";
        chip.title = name;
        chip.innerHTML = `
          <span class="mm-icon mm-icon--file-txt mm-icon--sm" aria-hidden="true"></span>
          <span class="mm-msg-user-attach-kind">${escapeHtml(att.kind || "SDF")}</span>
          <span class="mm-msg-user-attach-name">${escapeHtml(name)}</span>
        `;
        rail.appendChild(chip);
      });
      ask.appendChild(rail);
    }
    return ask;
  }

  function buildAssistantBubble(text, { error } = {}) {
    const block = document.createElement("div");
    block.className =
      "mm-msg-assistant-block" + (error ? " mm-msg-assistant-block--error" : "");
    const bubble = document.createElement("div");
    bubble.className = error ? "mm-msg-bubble mm-msg-bubble--error" : "mm-msg-bubble";
    if (error) {
      const head = document.createElement("div");
      head.className = "mm-msg-error-head";
      head.appendChild(icon("file-info", "mm-icon--md"));
      const label = document.createElement("span");
      label.className = "mm-msg-error-label";
      label.textContent = "出错了";
      head.appendChild(label);
      const body = document.createElement("div");
      body.className = "mm-msg-error-body";
      const detail = String(text || "").replace(/^错误[：:]\s*/, "");
      body.textContent = detail || "未知错误";
      bubble.appendChild(head);
      bubble.appendChild(body);
    } else {
      const html = renderAssistantHtml(text);
      if (html.includes("mm-md-table")) {
        block.classList.add("mm-msg-assistant-block--wide");
      }
      bubble.innerHTML = html || escapeHtml(text || "");
    }
    block.appendChild(bubble);
    return block;
  }

  function buildArtifactCard(card) {
    const fullTitle = card.title || card.filename || "附件";
    const kind = card.kind === "pdf" ? "pdf" : card.kind === "bundle" ? "bundle" : "csv";

    const row = document.createElement("div");
    row.className = "mm-artifact-attach";

    const a = document.createElement("a");
    a.href = global.MolMindClientIdentity
      ? global.MolMindClientIdentity.decorateDownloadUrl(card.download_url)
      : card.download_url;
    a.download = card.filename || "";
    a.className = "mm-artifact-chip";
    a.setAttribute("aria-label", `下载 ${fullTitle}`);
    a.title = fullTitle;

    const fileIcon = kind === "pdf" ? "file" : kind === "bundle" ? "stack" : "file-txt";
    a.appendChild(icon(fileIcon, "mm-icon--md"));

    const title = document.createElement("span");
    title.className = "mm-artifact-chip-title";
    title.textContent = fullTitle;
    a.appendChild(title);

    a.appendChild(icon("download", "mm-icon--sm"));
    row.appendChild(a);
    return row;
  }

  function appendEvidenceFact(parent, label, value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return;
    const row = document.createElement("div");
    row.className = "mm-evidence-fact";
    const key = document.createElement("span");
    key.className = "mm-evidence-fact-key";
    key.textContent = label;
    const val = document.createElement("span");
    val.className = "mm-evidence-fact-value";
    val.textContent = safeDisplayText(Array.isArray(value) ? value.join("、") : value, 500);
    row.appendChild(key);
    row.appendChild(val);
    parent.appendChild(row);
  }

  function evidenceStatusRows(card) {
    const raw =
      card.provider_statuses || card.sources || card.source_statuses || card.channels || card.providers;
    if (!raw) return [];
    if (Array.isArray(raw)) {
      return raw.map((item) => {
        if (item && typeof item === "object") return item;
        return { provider: item, status: "not_queried" };
      });
    }
    if (typeof raw === "object") {
      return Object.entries(raw).map(([provider, value]) => {
        if (value && typeof value === "object") return { provider, ...value };
        return { provider, status: value };
      });
    }
    return [];
  }

  function buildEvidenceCard(card) {
    const wrap = document.createElement("section");
    wrap.className = "mm-evidence-card";
    wrap.setAttribute(
      "aria-label",
      safeDisplayText(card.title || "候选分子证据卡", 120)
    );

    const head = document.createElement("div");
    head.className = "mm-evidence-card-head";
    const title = document.createElement("div");
    title.className = "mm-evidence-card-title";
    title.appendChild(icon("cards", "mm-icon--md"));
    const titleText = document.createElement("span");
    titleText.textContent = safeDisplayText(card.title || "候选分子证据卡", 120);
    title.appendChild(titleText);
    const status = document.createElement("span");
    status.className = "mm-evidence-status";
    status.dataset.status = safeDisplayText(card.status || "not_queried", 80);
    status.textContent = safeDisplayText(card.status || "not_queried", 80);
    head.appendChild(title);
    head.appendChild(status);
    wrap.appendChild(head);

    const identity = card.identity && typeof card.identity === "object" ? card.identity : {};
    const facts = document.createElement("div");
    facts.className = "mm-evidence-facts";
    appendEvidenceFact(facts, "候选", identity.molecule_id || card.molecule_id);
    appendEvidenceFact(facts, "查询身份", identity.lookup_field || card.lookup_field);
    appendEvidenceFact(facts, "采用值", identity.lookup_value || card.lookup_value);
    appendEvidenceFact(facts, "匹配方式", identity.match_type || card.match_type);
    appendEvidenceFact(facts, "InChIKey", identity.inchikey || identity.standardized_inchikey);
    appendEvidenceFact(facts, "CAS", identity.cas);
    if (facts.childElementCount) wrap.appendChild(facts);

    const badges = document.createElement("div");
    badges.className = "mm-evidence-badges";
    [
      card.allow_live ? "allow_live=true" : "allow_live=false",
      card.writes_selection === false ? "不改主榜" : "",
      card.selection_sha256_unchanged === true ? "排名哈希未变" : "",
    ]
      .filter(Boolean)
      .forEach((label) => {
        const badge = document.createElement("span");
        badge.className = "mm-evidence-badge";
        badge.textContent = label;
        badges.appendChild(badge);
      });
    wrap.appendChild(badges);

    if (card.summary || card.message) {
      const summary = document.createElement("p");
      summary.className = "mm-evidence-summary";
      summary.textContent = safeDisplayText(card.summary || card.message, 1200);
      wrap.appendChild(summary);
    }

    const providers = evidenceStatusRows(card);
    if (providers.length) {
      const sourceBlock = document.createElement("div");
      sourceBlock.className = "mm-evidence-section";
      const sourceTitle = document.createElement("div");
      sourceTitle.className = "mm-evidence-section-title";
      sourceTitle.textContent = "来源状态";
      sourceBlock.appendChild(sourceTitle);
      const list = document.createElement("div");
      list.className = "mm-evidence-source-list";
      providers.slice(0, 20).forEach((item) => {
        const row = document.createElement("div");
        row.className = "mm-evidence-source-row";
        const provider = document.createElement("span");
        provider.className = "mm-evidence-source-name";
        provider.textContent = safeDisplayText(item.provider || item.adapter_id || "source", 100);
        const sourceStatus = document.createElement("span");
        sourceStatus.className = "mm-evidence-source-status";
        const statusList = Array.isArray(item.statuses)
          ? item.statuses.map((value) => safeDisplayText(value, 80)).filter(Boolean)
          : [];
        sourceStatus.textContent = safeDisplayText(
          statusList.length
            ? statusList.join(" / ")
            : item.status || item.query_status || item.result || "not_queried",
          180
        );
        row.appendChild(provider);
        row.appendChild(sourceStatus);
        list.appendChild(row);
      });
      sourceBlock.appendChild(list);
      wrap.appendChild(sourceBlock);
    }

    const hitsRaw = card.evidence_items || card.evidence || card.hits || card.items;
    const hits = Array.isArray(hitsRaw) ? hitsRaw.filter((item) => item && typeof item === "object") : [];
    if (hits.length) {
      const hitBlock = document.createElement("div");
      hitBlock.className = "mm-evidence-section";
      const hitTitle = document.createElement("div");
      hitTitle.className = "mm-evidence-section-title";
      hitTitle.textContent = `证据条目（${hits.length}）`;
      hitBlock.appendChild(hitTitle);
      const hitList = document.createElement("div");
      hitList.className = "mm-evidence-hit-list";
      hits.slice(0, 20).forEach((item) => {
        const row = document.createElement("div");
        row.className = "mm-evidence-hit-row";
        const main = document.createElement("div");
        main.className = "mm-evidence-hit-main";
        main.textContent = [
          item.provider || item.adapter_id,
          item.query_type,
          item.query_status || item.status,
        ]
          .filter(Boolean)
          .map((x) => safeDisplayText(x, 100))
          .join(" · ");
        const meta = document.createElement("div");
        meta.className = "mm-evidence-hit-meta";
        const explicitParticipation =
          typeof item.participates_in_ranking === "boolean"
            ? item.participates_in_ranking
            : typeof item.participated_in_ranking === "boolean"
              ? item.participated_in_ranking
              : null;
        const participates =
          explicitParticipation === true
            ? "参与当前排名"
            : item.evidence_role === "task_evidence"
              ? "候选任务证据/未参与当前排名"
              : "仅注释/审计";
        const auditReason =
          item.audit_detail && typeof item.audit_detail === "object"
            ? item.audit_detail.reason
            : "";
        meta.textContent = [item.evidence_id, item.evidence_role, participates, auditReason]
          .filter(Boolean)
          .map((x) => safeDisplayText(x, 140))
          .join(" · ");
        row.appendChild(main);
        row.appendChild(meta);
        hitList.appendChild(row);
      });
      hitBlock.appendChild(hitList);
      wrap.appendChild(hitBlock);
    }

    const degraded = Array.isArray(card.degraded_channels) ? card.degraded_channels : [];
    if (degraded.length) {
      const note = document.createElement("div");
      note.className = "mm-evidence-warning";
      note.textContent = `降级通道：${degraded.map((x) => safeDisplayText(x, 120)).join("、")}`;
      wrap.appendChild(note);
    }

    if (card.identity_conflict || card.identity_review_required) {
      const note = document.createElement("div");
      note.className = "mm-evidence-warning";
      note.textContent = safeDisplayText(
        card.identity_conflict || card.identity_review_required,
        600
      );
      wrap.appendChild(note);
    }

    const conclusion =
      card.scientific_conclusion || card.allowed_conclusion || card.claim_ceiling || card.conclusion;
    if (conclusion) {
      const note = document.createElement("div");
      note.className = "mm-evidence-conclusion";
      note.textContent = `当前结论边界：${safeDisplayText(conclusion, 800)}`;
      wrap.appendChild(note);
    }
    if (card.recommendation || card.live_recommendation) {
      const note = document.createElement("div");
      note.className = "mm-evidence-recommendation";
      note.textContent = safeDisplayText(card.recommendation || card.live_recommendation, 800);
      wrap.appendChild(note);
    }
    return wrap;
  }

  function createRevealQueue() {
    let chain = Promise.resolve();
    let forceInstant = false;
    const waiters = new Set();

    function delay(ms) {
      return new Promise((resolve) => {
        if (forceInstant) {
          resolve();
          return;
        }
        const entry = { resolve, id: null };
        entry.id = setTimeout(() => {
          waiters.delete(entry);
          resolve();
        }, ms);
        waiters.add(entry);
      });
    }

    async function typeInto(el, fullText, onTick) {
      const text = String(fullText || "");
      if (!el) return;
      if (forceInstant || !text) {
        el.textContent = text;
        if (onTick) onTick();
        return;
      }
      const len = text.length;
      let chunk = 1;
      let cps = 46;
      if (len > 900) {
        chunk = 5;
        cps = 110;
      } else if (len > 400) {
        chunk = 3;
        cps = 72;
      } else if (len > 120) {
        chunk = 2;
        cps = 56;
      }
      let i = 0;
      el.textContent = "";
      while (i < len) {
        if (forceInstant) {
          el.textContent = text;
          if (onTick) onTick();
          return;
        }
        i = Math.min(len, i + chunk);
        el.textContent = text.slice(0, i);
        if (onTick) onTick();
        await delay(Math.max(10, Math.round((1000 / cps) * chunk)));
      }
    }

    return {
      enqueue(task) {
        chain = chain.then(() => task()).catch(() => {});
        return chain;
      },
      whenIdle() {
        return chain.then(() => undefined);
      },
      /** Jump remaining animations to full text (e.g. user navigates away). */
      flush() {
        forceInstant = true;
        waiters.forEach((entry) => {
          clearTimeout(entry.id);
          entry.resolve();
        });
        waiters.clear();
      },
      typeInto,
      get instant() {
        return forceInstant;
      },
    };
  }

  function finishAssistantBubble(block, full) {
    const bubble = block.querySelector(".mm-msg-bubble");
    if (!bubble) return;
    bubble.classList.remove("mm-msg-bubble--streaming");
    const html = renderAssistantHtml(full);
    if (html.includes("mm-md-table")) {
      block.classList.add("mm-msg-assistant-block--wide");
    }
    bubble.innerHTML = html || escapeHtml(full || "");
  }

  function buildStreamingAssistantShell() {
    const block = document.createElement("div");
    block.className = "mm-msg-assistant-block";
    const bubble = document.createElement("div");
    bubble.className = "mm-msg-bubble mm-msg-bubble--streaming";
    const streamEl = document.createElement("span");
    streamEl.className = "mm-msg-stream-text";
    const cursor = document.createElement("span");
    cursor.className = "mm-msg-stream-cursor";
    cursor.setAttribute("aria-hidden", "true");
    bubble.appendChild(streamEl);
    bubble.appendChild(cursor);
    block.appendChild(bubble);
    return { block, streamEl };
  }

  function queryEventLine(ev) {
    const provider = safeDisplayText(ev.provider || ev.adapter_id || ev.source || "", 120);
    const status = safeDisplayText(ev.status || ev.query_status || "", 120);
    const message = safeDisplayText(ev.message || "", 500);
    const count = ev.count != null ? ` · ${ev.count} 条` : ev.hit_count != null ? ` · ${ev.hit_count} 条` : "";
    if (ev.type === "query_plan") {
      const providers = Array.isArray(ev.providers)
        ? ev.providers.map((x) => safeDisplayText(x, 80)).filter(Boolean).join("、")
        : "";
      const queryTypes = Array.isArray(ev.query_types)
        ? ev.query_types.map((x) => safeDisplayText(x, 80)).filter(Boolean).join("、")
        : "";
      return [
        `查询计划 · allow_live=${ev.allow_live === true}`,
        providers ? `来源=${providers}` : "",
        queryTypes ? `类型=${queryTypes}` : "",
        message,
      ]
        .filter(Boolean)
        .join(" · ");
    }
    if (ev.type === "local_hit") {
      return `本地命中${provider ? " · " + provider : ""}${count}${status ? " · " + status : ""}`;
    }
    if (ev.type === "remote_start") {
      return `远端查询开始${provider ? " · " + provider : ""}`;
    }
    if (ev.type === "remote_end") {
      return `远端查询结束${provider ? " · " + provider : ""}${status ? " · " + status : ""}${count}`;
    }
    if (ev.type === "degraded") {
      const channels = Array.isArray(ev.degraded_channels)
        ? ev.degraded_channels.map((x) => safeDisplayText(x, 100)).join("、")
        : "";
      return `查询降级${provider ? " · " + provider : ""}${channels ? " · " + channels : ""}${message ? " · " + message : ""}`;
    }
    if (ev.type === "identity_conflict") {
      return `身份冲突${status ? " · " + status : ""}${message ? " · " + message : ""}`;
    }
    return `证据摘要${status ? " · " + status : ""}${message ? " · " + message : ""}`;
  }

  function createTraceBlock(reveal, { live, onTick, startedAt, completedAt } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "mm-thinking mm-thinking--active";
    wrap.dataset.open = "1";
    wrap.dataset.steps = "0";

    const head = document.createElement("button");
    head.type = "button";
    head.className = "mm-thinking-toggle";

    const chev = icon("chevron", "mm-thinking-chevron mm-thinking-chevron--open");
    const label = document.createElement("span");
    label.className = "mm-thinking-label";
    label.textContent = "思考与执行过程";
    const pulse = document.createElement("span");
    pulse.className = "mm-thinking-pulse";
    pulse.setAttribute("aria-hidden", "true");
    const duration = document.createElement("span");
    duration.className = "mm-thinking-duration";
    const count = document.createElement("span");
    count.className = "mm-thinking-count";

    head.appendChild(chev);
    head.appendChild(label);
    head.appendChild(pulse);
    head.appendChild(duration);
    head.appendChild(count);

    const body = document.createElement("div");
    body.className = "mm-thinking-body";

    const setOpen = (open) => {
      wrap.dataset.open = open ? "1" : "0";
      body.classList.toggle("hidden", !open);
      chev.classList.toggle("mm-thinking-chevron--open", open);
    };

    head.addEventListener("click", () => {
      setOpen(wrap.dataset.open !== "1");
    });

    wrap.appendChild(head);
    wrap.appendChild(body);

    const traceStartedAt = Number.isFinite(startedAt) ? startedAt : Date.now();
    let traceCompletedAt = Number.isFinite(completedAt) ? completedAt : null;
    const formatDuration = (referenceTime = Date.now()) => {
      const elapsedSeconds = Math.max(0, Math.floor((referenceTime - traceStartedAt) / 1000));
      const minutes = Math.floor(elapsedSeconds / 60);
      const seconds = String(elapsedSeconds % 60).padStart(2, "0");
      return `${String(minutes).padStart(2, "0")}:${seconds}`;
    };
    const updateDuration = (completed) => {
      duration.textContent = `${completed ? "对话用时" : "已对话"} ${formatDuration(
        completed && traceCompletedAt ? traceCompletedAt : Date.now()
      )}`;
    };
    updateDuration(false);
    const durationTimer = live ? setInterval(() => updateDuration(false), 1000) : null;

    const markLatest = () => {
      body.querySelectorAll(".mm-thinking-step--latest").forEach((n) => {
        n.classList.remove("mm-thinking-step--latest");
      });
      const last = body.lastElementChild;
      if (last) last.classList.add("mm-thinking-step--latest");
      body.scrollTop = body.scrollHeight;
    };

    const paintStep = (el, text, { stream } = {}) => {
      const full = String(text || "");
      el.dataset.fullText = full;
      if (!live || !stream || !reveal) {
        el.textContent = full;
        markLatest();
        if (onTick) onTick();
        return;
      }
      el.textContent = "";
      markLatest();
      reveal.enqueue(async () => {
        await reveal.typeInto(el, full, () => {
          markLatest();
          if (onTick) onTick();
        });
      });
    };

    return {
      root: wrap,
      body,
      bump(nextLabel) {
        const n = Number(wrap.dataset.steps || "0") + 1;
        wrap.dataset.steps = String(n);
        if (nextLabel) label.textContent = nextLabel;
        count.textContent = `${n} 步`;
      },
      finalize({ completedAt: nextCompletedAt } = {}) {
        if (Number.isFinite(nextCompletedAt)) traceCompletedAt = nextCompletedAt;
        if (durationTimer) clearInterval(durationTimer);
        updateDuration(true);
        const n = Number(wrap.dataset.steps || "0");
        label.textContent = "思考与执行过程";
        count.textContent = n ? `${n} 步` : "";
        if (pulse.parentNode) pulse.remove();
        wrap.classList.remove("mm-thinking--active");
        setOpen(false);
      },
      appendThinking(text) {
        const el = document.createElement("div");
        el.className = "mm-thinking-step";
        body.appendChild(el);
        this.bump();
        paintStep(el, text, { stream: true });
      },
      appendPlan(steps) {
        const el = document.createElement("div");
        el.className = "mm-thinking-step";
        body.appendChild(el);
        this.bump("计划已生成");
        paintStep(el, "计划：" + (steps || []).join(" → "), { stream: true });
      },
      appendTool(kind, tool, extra) {
        const el = document.createElement("div");
        el.className = "mm-thinking-step mm-thinking-step--tool";
        const status =
          kind === "start"
            ? "正在执行"
            : kind === "end"
              ? "已完成"
              : kind === "error"
                ? "执行失败"
                : "执行";
        body.appendChild(el);
        this.bump();
        // Tool lines stay snappy — short fade via CSS, no typewriter.
        paintStep(el, `${status}：${tool}${extra ? `（${extra}）` : ""}`, { stream: false });
      },
      appendQuery(ev) {
        const el = document.createElement("div");
        el.className = `mm-thinking-step mm-thinking-step--query mm-thinking-step--${ev.type}`;
        body.appendChild(el);
        this.bump(ev.type === "query_summary" ? "证据查询完成" : "正在查询证据");
        paintStep(el, queryEventLine(ev), { stream: false });
      },
    };
  }

  function friendlyTraceLog(message) {
    const text = String(message || "");
    if (/开始规则 Critic/.test(text)) return "正在核对候选是否符合关键筛选要求…";
    const criticDone = text.match(/规则 Critic 完成：.*?keep=(\d+).*?当前短名单大小=(\d+)/);
    if (criticDone) return `候选核对完成：保留 ${criticDone[2]} 个符合要求的候选。`;
    if (/开始证据约束 LLM Critic/.test(text)) return "正在确认本轮是否有可用于补充判断的证据…";
    if (/LLM Critic 未改动短名单/.test(text)) return "本轮没有额外证据需要调整候选顺序。";
    if (/诊断备注：/.test(text)) {
      return "筛选结果可用；候选的结构多样性仍有提升空间，建议后续扩大候选来源。";
    }
    const complete = text.match(/筛选完成：正式主榜=(\d+)\/(\d+)，候补=(\d+)\/(\d+)/);
    if (complete) return `筛选完成：已得到 ${complete[1]} 个优先候选和 ${complete[3]} 个备选。`;
    return text;
  }

  function friendlyToolLine(kind, tool, event) {
    const topN = event && event.args && event.args.top_n;
    if (tool === "score_and_rank") {
      if (kind === "start") return `开始筛选${topN ? ` Top${topN}` : ""}候选`;
      return event && event.ok === false
        ? "候选筛选未完成"
        : `候选筛选完成${event && event.elapsed_s != null ? `（用时 ${event.elapsed_s} 秒）` : ""}`;
    }
    if (tool === "export_nomination") {
      return kind === "start" ? "正在生成 CSV 文件" : event && event.ok === false ? "CSV 文件生成失败" : "CSV 文件已准备好";
    }
    if (tool === "start_mechanism_report") {
      if (kind === "start") return "正在生成机制与验证方案 PDF";
      return event && event.ok === false ? "机制 PDF 生成失败" : "机制 PDF 已准备好";
    }
    return tool;
  }

  function applyEventToTurn(turn, ev) {
    if (!turn || !ev) return;
    const type = ev.type;
    if (
      type === "thinking" ||
      type === "plan" ||
      type === "tool_start" ||
      type === "tool_end" ||
      type === "log" ||
      type === "query_plan" ||
      type === "local_hit" ||
      type === "remote_start" ||
      type === "remote_end" ||
      type === "degraded" ||
      type === "identity_conflict" ||
      type === "query_summary" ||
      type === "loop_decision" ||
      type === "governance_denied"
    ) {
      const trace = turn.ensureTrace();
      if (type === "thinking") trace.appendThinking(ev.text || "");
      else if (type === "plan") trace.appendPlan(ev.steps || []);
      else if (type === "tool_start") {
        trace.appendTool("start", friendlyToolLine("start", ev.tool, ev));
      } else if (type === "tool_end") {
        const terminalKind = ev.ok === false ? "error" : "end";
        trace.appendTool(terminalKind, friendlyToolLine(terminalKind, ev.tool, ev));
      } else if (type === "log" && ev.message) {
        trace.appendTool("log", "进度", friendlyTraceLog(ev.message));
      } else if (
        type === "query_plan" ||
        type === "local_hit" ||
        type === "remote_start" ||
        type === "remote_end" ||
        type === "degraded" ||
        type === "identity_conflict" ||
        type === "query_summary"
      ) {
        trace.appendQuery(ev);
      } else if (type === "loop_decision") {
        const labels = {
          continue: "继续下一轮",
          final: "输出结果",
          clarify: "请求补充信息",
          abort: "停止执行",
        };
        const label = labels[ev.decision] || ev.decision || "输出结果";
        trace.appendThinking(
          `Loop 第 ${ev.iteration || 1} 轮决策：${label}${ev.reason ? `（${ev.reason}）` : ""}`,
        );
      } else if (type === "governance_denied") {
        trace.appendTool(
          "log",
          "治理拦截",
          `${ev.tool || "tool"}：${ev.detail || ev.code || "调用未获准"}`,
        );
      }
    } else if (type === "card" && ev.card) {
      // 附件始终挂在本轮回答末尾，不结束思考块
      turn.appendCard(ev.card);
    } else if (type === "assistant") {
      turn.appendAssistant(ev.text || "");
    } else if (type === "error") {
      turn.appendAssistant(`错误：${ev.detail || "unknown"}`, { error: true });
    } else if (type === "run_interrupted") {
      turn.appendAssistant(
        ev.detail || "服务重启导致本轮中断，请重新发送本轮请求。",
        { error: true }
      );
    }
    // done：由调用方 waitForStream 后再 finalize（直播打字机完成后收起思考）
  }

  const MolMindAgentRender = {
    /**
     * 一轮对话盒子：问(ask) + 答(answer)。
     * answer 内顺序固定：思考过程 → 正文 → 附件（末尾）。
     * live=true 时对思考/回答做打字机渐进揭示；历史回放保持瞬间展示。
     */
    beginTurn(container, { text, attachments, live, onScroll, startedAt, completedAt } = {}) {
      const turnId =
        "turn-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
      const root = document.createElement("div");
      root.className = "mm-turn";
      root.setAttribute("data-turn-id", turnId);
      root.setAttribute("data-turn-text", text || "");

      const hasAsk = text != null && String(text).length > 0;
      if (hasAsk) {
        root.appendChild(buildUserAsk(text, attachments));
      }

      const answer = document.createElement("div");
      answer.className = "mm-turn-answer";

      const body = document.createElement("div");
      body.className = "mm-turn-answer-body";

      const artifacts = document.createElement("div");
      artifacts.className = "mm-turn-artifacts";

      answer.appendChild(body);
      answer.appendChild(artifacts);
      root.appendChild(answer);
      container.appendChild(root);

      const reveal = live ? createRevealQueue() : null;
      const turnStartedAt = Number.isFinite(startedAt) ? startedAt : Date.now();
      const tick = () => {
        if (typeof onScroll === "function") onScroll();
      };
      let trace = null;
      let finalized = false;

      const api = {
        id: turnId,
        root,
        answer,
        body,
        artifacts,
        ensureTrace() {
          if (!trace) {
            trace = createTraceBlock(reveal, {
              live: !!live,
              onTick: tick,
              startedAt: turnStartedAt,
              completedAt,
            });
            answer.insertBefore(trace.root, body);
          }
          return trace;
        },
        appendAssistant(text, opts) {
          const error = !!(opts && opts.error);
          const instant = !live || !!(opts && opts.instant) || error;
          if (error) {
            body.appendChild(buildAssistantBubble(text, opts));
            tick();
            return;
          }
          const full = String(text || "");
          if (instant || !reveal) {
            body.appendChild(buildAssistantBubble(full, opts));
            tick();
            return;
          }
          const { block, streamEl } = buildStreamingAssistantShell();
          block.dataset.fullText = full;
          body.appendChild(block);
          tick();
          reveal.enqueue(async () => {
            await reveal.typeInto(streamEl, full, tick);
            finishAssistantBubble(block, full);
            tick();
          });
        },
        appendCard(card) {
          if (!card) return;
          const href = global.MolMindClientIdentity
            ? global.MolMindClientIdentity.decorateDownloadUrl(card.download_url)
            : card.download_url;
          if (href && artifacts.querySelector(`a[href="${href}"]`)) return;
          if (card.kind === "evidence") {
            artifacts.appendChild(buildEvidenceCard(card));
          } else {
            artifacts.appendChild(buildArtifactCard(card));
          }
          tick();
        },
        applyEvent(ev) {
          applyEventToTurn(api, ev);
        },
        waitForStream() {
          return reveal ? reveal.whenIdle() : Promise.resolve();
        },
        finalize({ completedAt: nextCompletedAt } = {}) {
          if (finalized) return;
          finalized = true;
          if (trace) trace.finalize({ completedAt: nextCompletedAt });
        },
        abortStream() {
          if (reveal) reveal.flush();
          body.querySelectorAll("[data-full-text]").forEach((el) => {
            const full = el.dataset.fullText || "";
            if (el.classList.contains("mm-msg-assistant-block")) {
              finishAssistantBubble(el, full);
            } else if (!el.textContent || el.textContent.length < full.length) {
              el.textContent = full;
            }
          });
        },
      };
      return api;
    },

    /** @deprecated 兼容：优先使用 beginTurn */
    appendUserBubble(container, text, opts) {
      const turn = this.beginTurn(container, { text, ...(opts || {}) });
      return turn.root.querySelector(".mm-msg-user-hint");
    },

    appendAssistantBubble(container, text, opts) {
      const turn = this.beginTurn(container, {});
      turn.appendAssistant(text, opts);
      turn.finalize();
      return turn.body.lastElementChild;
    },

    createTraceBlock(container) {
      const trace = createTraceBlock(null, { live: false });
      if (container) container.appendChild(trace.root);
      return trace;
    },

    appendArtifactCard(container, card) {
      const row = buildArtifactCard(card);
      if (container) container.appendChild(row);
      return row.querySelector("a");
    },

    clearMessages(container) {
      if (container) container.innerHTML = "";
    },

    /** 将一批事件回放到一个已创建的 turn（一轮只一个思考块） */
    replayEventsIntoTurn(turn, events) {
      (events || []).forEach((ev) => applyEventToTurn(turn, ev));
      const eventTimes = (events || [])
        .map((event) => Date.parse(event && event.occurred_at ? event.occurred_at : ""))
        .filter(Number.isFinite);
      turn.finalize({ completedAt: eventTimes.length ? eventTimes[eventTimes.length - 1] : undefined });
    },

    replayEvents(container, events, onScroll) {
      const turn = this.beginTurn(container, { onScroll });
      this.replayEventsIntoTurn(turn, events);
      if (onScroll) onScroll();
      return turn;
    },
  };

  global.MolMindAgentRender = MolMindAgentRender;
})(window);
