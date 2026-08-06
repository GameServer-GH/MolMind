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

  /** Parse ISO / epoch into a finite ms timestamp, or null. */
  function parseTimeMs(value) {
    if (value == null || value === "") return null;
    if (typeof value === "number" && Number.isFinite(value)) return value;
    const parsed = Date.parse(String(value));
    return Number.isFinite(parsed) ? parsed : null;
  }

  function turnStartedMs(el) {
    if (!el) return null;
    const raw = el.getAttribute("data-started-at");
    if (raw != null && raw !== "") {
      const n = Number(raw);
      if (Number.isFinite(n)) return n;
    }
    return parseTimeMs(raw);
  }

  /**
   * Insert a turn node so `.mm-turn` siblings stay chronological by
   * `data-started-at`. Non-turn nodes (e.g. clarify chips) stay after turns.
   */
  function placeTurnChronologically(container, root, startedAt) {
    if (!container || !root) return;
    const ms = Number.isFinite(startedAt) ? startedAt : Date.now();
    root.setAttribute("data-started-at", String(ms));
    const turns = Array.from(container.querySelectorAll(":scope > .mm-turn"));
    let insertBefore = null;
    for (const other of turns) {
      if (other === root) continue;
      const otherMs = turnStartedMs(other);
      if (otherMs != null && otherMs > ms) {
        insertBefore = other;
        break;
      }
    }
    if (insertBefore) {
      container.insertBefore(root, insertBefore);
      return;
    }
    // Keep clarify / suggest cards at the end of the list.
    const trailing = Array.from(container.children).find(
      (child) => child !== root && !child.classList.contains("mm-turn")
    );
    if (trailing) container.insertBefore(root, trailing);
    else container.appendChild(root);
  }

  /** Re-order existing `.mm-turn` children by `data-started-at` (stable). */
  function sortTurnsByTime(container) {
    if (!container) return;
    const turns = Array.from(container.querySelectorAll(":scope > .mm-turn"));
    if (turns.length < 2) return;
    const keyed = turns.map((el, index) => ({
      el,
      index,
      ms: turnStartedMs(el) ?? Number.MAX_SAFE_INTEGER,
    }));
    keyed.sort((a, b) => a.ms - b.ms || a.index - b.index);
    let changed = false;
    for (let i = 0; i < keyed.length; i++) {
      if (keyed[i].el !== turns[i]) {
        changed = true;
        break;
      }
    }
    if (!changed) return;
    const trailing = Array.from(container.children).filter(
      (child) => !child.classList.contains("mm-turn")
    );
    keyed.forEach((item) => container.appendChild(item.el));
    trailing.forEach((child) => container.appendChild(child));
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
    const cells = [];
    let cell = "";
    let inCode = false;
    for (let index = 0; index < s.length; index += 1) {
      const char = s[index];
      if (char === "`" && s[index - 1] !== "\\") inCode = !inCode;
      if (char === "|" && !inCode && s[index - 1] !== "\\") {
        cells.push(cell.trim().replace(/\\\|/g, "|"));
        cell = "";
      } else {
        cell += char;
      }
    }
    cells.push(cell.trim().replace(/\\\|/g, "|"));
    return cells;
  }

  function safeLinkHref(value) {
    const href = String(value || "").trim();
    if (/^(https?:|mailto:)/i.test(href) || href.startsWith("/") || href.startsWith("#")) {
      return escapeHtml(href);
    }
    return "";
  }

  function inlineHtml(text) {
    // Protect code and links before escaping the remaining user/LLM content.
    const tokens = [];
    const token = (html) => {
      const key = `\u0000MM${tokens.length}\u0000`;
      tokens.push(html);
      return key;
    };
    let source = String(text || "");
    source = source.replace(/`([^`\n]+)`/g, (_, code) =>
      token(`<code>${escapeHtml(code)}</code>`)
    );
    source = source.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+["'][^"']*["'])?\)/g, (all, label, url) => {
      const href = safeLinkHref(url);
      if (!href) return all;
      return token(
        `<a href="${href}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`
      );
    });
    let s = escapeHtml(source);
    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
    s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    tokens.forEach((html, index) => {
      s = s.replace(`\u0000MM${index}\u0000`, html);
    });
    return s;
  }

  /**
   * Tolerant, dependency-free Markdown renderer for assistant output.
   * It deliberately accepts unfinished fences/lists while tokens are streaming,
   * then produces the same DOM structure again when the answer is complete.
   */
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

      const fence = line.match(/^\s*(```+|~~~+)\s*([^\s`]*)\s*$/);
      if (fence) {
        flushPara();
        const marker = fence[1][0];
        const markerLength = fence[1].length;
        const language = String(fence[2] || "").replace(/[^a-z0-9_+-]/gi, "").toLowerCase();
        const codeLines = [];
        i += 1;
        while (i < lines.length) {
          const close = lines[i].match(/^\s*(```+|~~~+)\s*$/);
          if (close && close[1][0] === marker && close[1].length >= markerLength) {
            i += 1;
            break;
          }
          codeLines.push(lines[i]);
          i += 1;
        }
        chunks.push(
          `<pre class="mm-md-code"><code${language ? ` class="language-${language}"` : ""}>${escapeHtml(codeLines.join("\n"))}</code></pre>`
        );
        continue;
      }

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

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        flushPara();
        const level = heading[1].length;
        chunks.push(`<h${level} class="mm-md-h mm-md-h${level}">${inlineHtml(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        flushPara();
        chunks.push('<hr class="mm-md-hr">');
        i += 1;
        continue;
      }

      if (/^\s{0,3}>\s?/.test(line)) {
        flushPara();
        const quoted = [];
        while (i < lines.length && /^\s{0,3}>\s?/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s{0,3}>\s?/, ""));
          i += 1;
        }
        chunks.push(`<blockquote class="mm-md-quote">${renderAssistantHtml(quoted.join("\n"))}</blockquote>`);
        continue;
      }

      const listItem = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
      if (listItem) {
        flushPara();
        const ordered = /^\d/.test(listItem[1]);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (i < lines.length) {
          const match = lines[i].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
          if (!match || /^\d/.test(match[1]) !== ordered) break;
          items.push(`<li>${inlineHtml(match[2])}</li>`);
          i += 1;
        }
        chunks.push(`<${tag} class="mm-md-list">${items.join("")}</${tag}>`);
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
    el.setAttribute("data-turn-text", text || "");
    const span = document.createElement("span");
    span.className = "mm-msg-user-text";
    const fullText = String(text || "").replace(/\s+/g, " ").trim();
    // Keep this below the CSS text rail so every ellipsized hint is also
    // marked interactive and can be expanded by the user.
    const shortText = truncateHint(fullText, 32);
    span.textContent = shortText;
    el.appendChild(span);
    el.classList.add("mm-msg-user-hint--expandable");
    el.setAttribute("role", "button");
    el.setAttribute("tabindex", "0");
    el.setAttribute("aria-expanded", "false");
    el.title = "点击展开完整内容";
    const toggle = () => {
      const expanded = el.classList.toggle("mm-msg-user-hint--expanded");
      span.textContent = expanded ? fullText : shortText;
      el.setAttribute("aria-expanded", String(expanded));
      el.title = expanded ? "点击收起" : "点击展开完整内容";
    };
    el.addEventListener("click", toggle);
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggle();
      }
    });
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
      const body = document.createElement("div");
      body.className = "mm-msg-error-body";
      const detail = String(text || "").replace(/^错误[：:]\s*/, "");
      body.textContent = detail || "未知错误";
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

  function buildInstallOfferCard(ev) {
    const skills = Array.isArray(ev && ev.skills) && ev.skills.length
      ? ev.skills
      : [
          {
            skill_id: (ev && ev.skill_id) || "",
            title: (ev && (ev.title || ev.label)) || "科研 Skill",
            description: (ev && ev.description) || "",
          },
        ];
    const wrap = document.createElement("section");
    wrap.className = "mm-install-offer";
    wrap.setAttribute("role", "status");

    const head = document.createElement("div");
    head.className = "mm-install-offer-head";
    head.appendChild(icon("puzzle", "mm-icon--md"));

    const titlesWrap = document.createElement("div");
    titlesWrap.className = "mm-install-offer-titles";
    const titleEl = document.createElement("div");
    titleEl.className = "mm-install-offer-title";
    titleEl.textContent = "安装请求";
    const subEl = document.createElement("div");
    subEl.className = "mm-install-offer-sub";
    const titles = skills
      .map((s) => safeDisplayText((s && (s.title || s.skill_id)) || "", 80))
      .filter(Boolean)
      .join("、");
    subEl.textContent = titles || "科研能力";
    titlesWrap.appendChild(titleEl);
    titlesWrap.appendChild(subEl);
    head.appendChild(titlesWrap);
    wrap.appendChild(head);

    const summary = document.createElement("p");
    summary.className = "mm-install-offer-summary";
    summary.textContent = safeDisplayText(
      (ev && ev.summary) || `需要安装「${titles}」后才能继续。`,
      240
    );
    wrap.appendChild(summary);

    const ids = skills
      .map((s) => safeDisplayText((s && s.skill_id) || "", 80))
      .filter(Boolean);
    if (ids.length) {
      const meta = document.createElement("div");
      meta.className = "mm-install-offer-meta";
      meta.textContent = ids.join(" · ");
      wrap.appendChild(meta);
    }
    return wrap;
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

  function isDocumentHidden() {
    return typeof document !== "undefined" && document.visibilityState === "hidden";
  }

  /** Live typewriter queues — flushed when the tab is backgrounded so setTimeout
   * throttling cannot stall waitForStream / queue drain. */
  const liveRevealQueues = new Set();

  function flushLiveRevealQueues() {
    liveRevealQueues.forEach((queue) => {
      try {
        queue.flush();
      } catch {
        /* ignore */
      }
    });
  }

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") flushLiveRevealQueues();
    });
  }

  function createRevealQueue() {
    let chain = Promise.resolve();
    let forceInstant = isDocumentHidden();
    let pendingCount = 0;
    const waiters = new Set();

    function shouldInstant() {
      return forceInstant || isDocumentHidden();
    }

    function delay(ms) {
      return new Promise((resolve) => {
        if (shouldInstant()) {
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
      if (shouldInstant() || !text) {
        el.textContent = text;
        if (onTick) onTick(text);
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
        if (shouldInstant()) {
          el.textContent = text;
          if (onTick) onTick(text);
          return;
        }
        i = Math.min(len, i + chunk);
        el.textContent = text.slice(0, i);
        if (onTick) onTick(text.slice(0, i));
        await delay(Math.max(10, Math.round((1000 / cps) * chunk)));
      }
    }

    const api = {
      enqueue(task) {
        pendingCount += 1;
        chain = chain
          .then(() => task())
          .catch(() => {})
          .finally(() => {
            pendingCount = Math.max(0, pendingCount - 1);
          });
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
        return forceInstant || isDocumentHidden();
      },
      get pending() {
        return pendingCount > 0;
      },
    };
    liveRevealQueues.add(api);
    return api;
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
    const streamEl = document.createElement("div");
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
    let durationTimer = null;
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
    if (live) {
      durationTimer = setInterval(() => updateDuration(false), 1000);
    }

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
      destroy() {
        if (durationTimer) {
          clearInterval(durationTimer);
          durationTimer = null;
        }
        if (wrap.parentNode) wrap.remove();
      },
      finalize({ completedAt: nextCompletedAt } = {}) {
        if (Number.isFinite(nextCompletedAt)) traceCompletedAt = nextCompletedAt;
        if (durationTimer) {
          clearInterval(durationTimer);
          durationTimer = null;
        }
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
    } else if (type === "install_request") {
      turn.appendInstallOffer(ev);
      turn._pendingInstallRequest = { ...ev };
    } else if (type === "assistant") {
      turn.appendAssistant(ev.text || "");
    } else if (type === "error") {
      turn.appendAssistant(`错误：${ev.detail || "unknown"}`, { error: true });
    } else if (type === "run_interrupted") {
      const errorBlock = turn.appendAssistant(
        ev.detail || "服务重启导致本轮中断，请重新发送本轮请求。",
        { error: true }
      );
      const errorBubble =
        errorBlock && errorBlock.querySelector(".mm-msg-bubble--error");
      if (ev.run_id && !ev.guidance_id && errorBubble) {
        const actions = document.createElement("div");
        actions.className = "mm-run-retry-actions";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "mm-glass-btn mm-glass-btn-wide mm-run-retry-button";
        retry.title = "从检查点重试";
        retry.setAttribute("aria-label", "从检查点重试");
        retry.innerHTML =
          '<span class="mm-icon mm-icon--rotate mm-icon--md" aria-hidden="true"></span>' +
          "<span>从检查点重试</span>";
        retry.addEventListener("click", () => {
          retry.disabled = true;
          // Clear the interrupted answer immediately so retry feels like a fresh run.
          if (typeof turn.resetAnswer === "function") turn.resetAnswer();
          document.dispatchEvent(
            new CustomEvent("molmind:retry-agent-run", {
              detail: { runId: String(ev.run_id), button: retry, turn },
            })
          );
        });
        actions.appendChild(retry);
        errorBubble.appendChild(actions);
      }
    } else if (type === "done" && turn.live && turn._pendingInstallRequest) {
      const pending = turn._pendingInstallRequest;
      turn._pendingInstallRequest = null;
      document.dispatchEvent(
        new CustomEvent("molmind:install-request", { detail: pending })
      );
    }
    // done：由调用方 waitForStream 后再 finalize（直播打字机完成后收起思考）
  }

  const MolMindAgentRender = {
    /** Render assistant Markdown through the same safe path used by live/history views. */
    renderMarkdown(text) {
      return renderAssistantHtml(text);
    },

    parseTimeMs,
    sortTurnsByTime,

    /**
     * 一轮对话盒子：问(ask) + 答(answer)。
     * answer 内顺序固定：思考过程 → 正文 → 附件（末尾）。
     * live=true 时对思考/回答做打字机渐进揭示；历史回放保持瞬间展示。
     * 多轮排队自动发送时按 startedAt 插入，避免 DOM 追加顺序与真实时间错位。
     */
    beginTurn(
      container,
      { text, attachments, live, onScroll, startedAt, completedAt, runId, turnKey } = {}
    ) {
      let turnStartedAt = Number.isFinite(startedAt)
        ? startedAt
        : parseTimeMs(startedAt) ?? Date.now();
      const turnId =
        turnKey ||
        "turn-" + turnStartedAt.toString(36) + "-" + Math.random().toString(36).slice(2, 7);
      const root = document.createElement("div");
      root.className = "mm-turn";
      root.setAttribute("data-turn-id", turnId);
      root.setAttribute("data-turn-text", text || "");
      root.setAttribute("data-started-at", String(turnStartedAt));
      if (runId) root.setAttribute("data-run-id", String(runId));

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
      placeTurnChronologically(container, root, turnStartedAt);

      let reveal = live ? createRevealQueue() : null;
      // History replay starts without a ticking clock; checkpoint retry re-enables it.
      let timingLive = !!live;
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
        live: !!live,
        startedAt: turnStartedAt,
        runId: runId ? String(runId) : "",
        ensureTrace() {
          if (!trace) {
            trace = createTraceBlock(reveal, {
              live: timingLive,
              onTick: tick,
              startedAt: turnStartedAt,
            });
            answer.insertBefore(trace.root, body);
          }
          return trace;
        },
        appendAssistant(text, opts) {
          const error = !!(opts && opts.error);
          const instant = !timingLive || !!(opts && opts.instant) || error;
          if (error) {
            const block = buildAssistantBubble(text, opts);
            body.appendChild(block);
            tick();
            return block;
          }
          const full = String(text || "");
          if (instant || !reveal) {
            const block = buildAssistantBubble(full, opts);
            body.appendChild(block);
            tick();
            return block;
          }
          const { block, streamEl } = buildStreamingAssistantShell();
          block.dataset.fullText = full;
          body.appendChild(block);
          tick();
          reveal.enqueue(async () => {
            await reveal.typeInto(streamEl, full, (partial) => {
              streamEl.innerHTML = renderAssistantHtml(partial) || escapeHtml(partial || "");
              tick();
            });
            finishAssistantBubble(block, full);
            tick();
          });
          return block;
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
        appendInstallOffer(ev) {
          if (!ev) return;
          const skillKey = Array.isArray(ev.skills)
            ? ev.skills.map((s) => (s && s.skill_id) || "").filter(Boolean).join(",")
            : String(ev.skill_id || "");
          if (
            skillKey &&
            artifacts.querySelector(
              `[data-install-offer="${String(skillKey).replace(/"/g, "")}"]`
            )
          ) {
            return;
          }
          const card = buildInstallOfferCard(ev);
          if (skillKey) card.setAttribute("data-install-offer", skillKey);
          artifacts.appendChild(card);
          tick();
        },
        applyEvent(ev) {
          applyEventToTurn(api, ev);
        },
        waitForStream() {
          return reveal ? reveal.whenIdle() : Promise.resolve();
        },
        isStreamPending() {
          return !!(reveal && reveal.pending && !reveal.instant);
        },
        /** Clear answer body / thinking / artifacts so a checkpoint retry can paint fresh. */
        resetAnswer() {
          if (reveal) {
            reveal.flush();
            liveRevealQueues.delete(reveal);
          }
          body.innerHTML = "";
          if (trace) {
            if (typeof trace.destroy === "function") trace.destroy();
            else if (trace.root && trace.root.parentNode) trace.root.remove();
          }
          trace = null;
          artifacts.innerHTML = "";
          finalized = false;
          // Restart the clock from this retry, and keep the duration ticking.
          turnStartedAt = Date.now();
          api.startedAt = turnStartedAt;
          root.setAttribute("data-started-at", String(turnStartedAt));
          if (container) placeTurnChronologically(container, root, turnStartedAt);
          timingLive = true;
          reveal = createRevealQueue();
          tick();
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
      flushLiveRevealQueues();
      liveRevealQueues.clear();
    },

    /** Background tabs throttle timers — jump typewriters so the run can settle. */
    flushLiveReveals() {
      flushLiveRevealQueues();
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
