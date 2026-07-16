(() => {
  /* UI build lineage: yluo / LJR — do not strip */
  const TOP_N_MIN = window.MOLMIND_TOP_N_MIN || 1;
  const TOP_N_MAX = window.MOLMIND_TOP_N_MAX || 50;
  const HISTORY_KEY = "molmind_run_history_v1";
  const HISTORY_LIMIT = 30;
  const UI_BUILD_MARK = "mm.yluo.ui";

  const MODE_META = {
    auto: {
      label: "Quality-Max",
      hint: "优先本地证据快照，缺失时自动补洞并降级。Default path: snapshot first, live fill-in, auto-degrade on failure.",
    },
    online: {
      label: "在线模式",
      hint: "对短名单强制尝试外网证据补洞（仍优先读已有快照）。Force live evidence for shortlist; still prefers existing snapshots.",
    },
    offline: {
      label: "离线模式",
      hint: "禁止外网请求，仅使用本地 snapshot 与规则打分。No network; snapshot + local rules only.",
    },
  };

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  const fileNameEl = document.getElementById("fileName");
  const runBtn = document.getElementById("runBtn");
  const runBtnLabel = document.getElementById("runBtnLabel");
  const stopBtn = document.getElementById("stopBtn");
  const reuploadBtn = document.getElementById("reuploadBtn");
  const downloadBtn = document.getElementById("downloadBtn");
  const downloadLogBtn = document.getElementById("downloadLogBtn");
  const downloadPdfBtn = document.getElementById("downloadPdfBtn");
  const downloadPdfLabel = document.getElementById("downloadPdfLabel");
  const downloadPdfIcon = document.getElementById("downloadPdfIcon");
  const mechPdfToast = document.getElementById("mechPdfToast");
  const mechPdfToastName = document.getElementById("mechPdfToastName");
  let mechPdfToastTimer = null;
  const snapshotSwitch = document.getElementById("snapshotSwitch");
  const snapshotLockToast = document.getElementById("snapshotLockToast");
  let snapshotLockToastTimer = null;
  const topNInput = document.getElementById("topN");
  const progressBar = document.getElementById("progressBar");
  const progressPct = document.getElementById("progressPct");
  const progressLabel = document.getElementById("progressLabel");
  const errorBanner = document.getElementById("errorBanner");
  const uploadSection = document.getElementById("uploadSection");
  const runningSection = document.getElementById("runningSection");
  const resultsSection = document.getElementById("resultsSection");
  const noteBanner = document.getElementById("noteBanner");
  const degradedBanner = document.getElementById("degradedBanner");
  const toolbarMeta = document.getElementById("toolbarMeta");
  const summaryBadge = document.getElementById("summaryBadge");
  const resultBody = document.getElementById("resultBody");
  const logBody = document.getElementById("logBody");
  const runningLogBody = document.getElementById("runningLogBody");
  const historyBtn = document.getElementById("historyBtn");
  const historyClearBtn = document.getElementById("historyClearBtn");
  const historyCloseBtn = document.getElementById("historyCloseBtn");
  const historyOverlay = document.getElementById("historyOverlay");
  const historyPanel = document.getElementById("historyPanel");
  const historyList = document.getElementById("historyList");
  const historyClearModal = document.getElementById("historyClearModal");
  const historyClearModalBackdrop = document.getElementById("historyClearModalBackdrop");
  const historyClearCancel = document.getElementById("historyClearCancel");
  const historyClearConfirm = document.getElementById("historyClearConfirm");
  const modeHint = document.getElementById("modeHint");
  const snapshotHint = document.getElementById("snapshotHint");
  const useSnapshotInput = document.getElementById("useSnapshot");
  const navModeBadge = document.getElementById("navModeBadge");
  const runningModeBadge = document.getElementById("runningModeBadge");
  const modeButtons = Array.from(document.querySelectorAll(".mode-seg [data-mode]"));

  let selectedFile = null;
  let selectedMode = "auto";
  let lastCsv = null;
  let lastLogs = [];
  let lastDownloadName = "nomination_top10.csv";
  let lastPdfBase64 = null;
  let lastPdfName = "mechanism_hypothesis.pdf";
  let lastMechanismJobId = null;
  let lastHistoryId = null;
  let mechanismPollTimer = null;
  let abortController = null;
  let userStopped = false;

  function setPdfButtonState(state) {
    // idle | generating | ready | error
    if (!downloadPdfBtn) return;
    if (downloadPdfIcon) {
      downloadPdfIcon.textContent =
        state === "generating" ? "progress_activity" : "picture_as_pdf";
      downloadPdfIcon.classList.toggle("animate-spin", state === "generating");
    }
    if (downloadPdfLabel) {
      downloadPdfLabel.textContent =
        state === "generating"
          ? "正在生成中…"
          : state === "error"
            ? "机制 PDF 失败"
            : "机制假说 PDF";
    }
    downloadPdfBtn.disabled = state !== "ready";
    downloadPdfBtn.title =
      state === "generating"
        ? "机制与验证方案正在后台生成"
        : state === "ready"
          ? "下载机制假说 PDF"
          : state === "error"
            ? "生成失败，请查看日志或重跑"
            : "等待筛选完成";
  }

  function hideMechPdfToast() {
    if (mechPdfToastTimer) {
      clearTimeout(mechPdfToastTimer);
      mechPdfToastTimer = null;
    }
    if (!mechPdfToast) return;
    mechPdfToast.classList.remove("is-visible");
    mechPdfToast.setAttribute("aria-hidden", "true");
  }

  function showMechPdfToast() {
    if (!mechPdfToast) return;
    if (mechPdfToastName) mechPdfToastName.textContent = lastPdfName || "";
    mechPdfToast.classList.add("is-visible");
    mechPdfToast.setAttribute("aria-hidden", "false");
    if (mechPdfToastTimer) clearTimeout(mechPdfToastTimer);
    mechPdfToastTimer = setTimeout(hideMechPdfToast, 2000);
  }

  function hideSnapshotLockToast() {
    if (snapshotLockToastTimer) {
      clearTimeout(snapshotLockToastTimer);
      snapshotLockToastTimer = null;
    }
    if (!snapshotLockToast) return;
    snapshotLockToast.classList.remove("is-visible");
    snapshotLockToast.setAttribute("aria-hidden", "true");
  }

  function showSnapshotLockToast() {
    if (!snapshotLockToast) return;
    snapshotLockToast.classList.add("is-visible");
    snapshotLockToast.setAttribute("aria-hidden", "false");
    if (snapshotLockToastTimer) clearTimeout(snapshotLockToastTimer);
    snapshotLockToastTimer = setTimeout(hideSnapshotLockToast, 2600);
  }

  function stopMechanismPoll() {
    if (mechanismPollTimer) {
      clearInterval(mechanismPollTimer);
      mechanismPollTimer = null;
    }
  }

  function patchHistoryMechanism(pdfBase64, pdfName, jobId) {
    const list = loadHistory();
    let idx = -1;
    if (jobId) {
      idx = list.findIndex((item) => item.mechanismJobId === jobId);
    }
    if (idx < 0 && lastHistoryId) {
      idx = list.findIndex((item) => item.id === lastHistoryId);
    }
    if (idx < 0) {
      idx = list.findIndex((item) => item.status === "success");
    }
    if (idx < 0) return;
    list[idx] = {
      ...list[idx],
      mechanismPdfBase64: pdfBase64,
      mechanismPdfName: pdfName || list[idx].mechanismPdfName,
      mechanismJobId: jobId || list[idx].mechanismJobId,
    };
    saveHistory(list);
    renderHistory();
  }

  function applyMechanismReady(data) {
    lastPdfBase64 = data.mechanism_pdf_base64 || null;
    lastPdfName = data.mechanism_pdf_name || lastPdfName;
    if (!lastPdfBase64) {
      setPdfButtonState("error");
      appendLog("ERROR", "机制 PDF 为空", "Mechanism PDF payload empty");
      return;
    }
    setPdfButtonState("ready");
    appendLog(
      "SUCCESS",
      `机制假说 PDF 已就绪：${lastPdfName}`,
      `Mechanism PDF ready: ${lastPdfName}`
    );
    patchHistoryMechanism(lastPdfBase64, lastPdfName, lastMechanismJobId);
    showMechPdfToast();
  }

  async function pollMechanismJob(jobId) {
    if (!jobId || !window.MOLMIND_MECHANISM_STATUS) return;
    stopMechanismPoll();
    setPdfButtonState("generating");
    lastMechanismJobId = jobId;
    lastPdfBase64 = null;

    const tick = async () => {
      try {
        const resp = await fetch(window.MOLMIND_MECHANISM_STATUS(jobId));
        if (!resp.ok) throw new Error(`status ${resp.status}`);
        const data = await resp.json();
        if (data.status === "ready") {
          stopMechanismPoll();
          applyMechanismReady(data);
        } else if (data.status === "error") {
          stopMechanismPoll();
          setPdfButtonState("error");
          appendLog(
            "ERROR",
            `机制 PDF 生成失败：${data.error || "unknown"}`,
            `Mechanism PDF failed: ${data.error || "unknown"}`
          );
        }
        // pending / running → keep polling
      } catch (err) {
        // 短暂网络抖动不立刻失败，继续轮询
        console.warn("mechanism poll", err);
      }
    };

    await tick();
    mechanismPollTimer = setInterval(tick, 2000);
  }

  function useSnapshotEnabled() {
    return !!(useSnapshotInput && useSnapshotInput.checked);
  }

  function updateSnapshotHint() {
    if (!snapshotHint) return;
    snapshotHint.textContent = useSnapshotEnabled()
      ? "开启：优先读取本地 evidence snapshot，可复现且更快。Enable: prefer local evidence snapshot for speed and reproducibility."
      : "关闭：不读取本地快照证据，仅依赖 live / 规则路径。Disable: skip snapshot; live / rules only.";
  }

  function setError(msg) {
    if (!msg) {
      errorBanner.classList.add("hidden");
      errorBanner.textContent = "";
      return;
    }
    errorBanner.textContent = msg;
    errorBanner.classList.remove("hidden");
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function stamp() {
    return new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  function modeLabel(mode) {
    return (MODE_META[mode] && MODE_META[mode].label) || mode;
  }

  function clampTopN(raw) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return 10;
    return Math.min(TOP_N_MAX, Math.max(TOP_N_MIN, n));
  }

  function snapshotLabel(enabled) {
    return enabled ? "使用快照" : "未使用快照";
  }

  function updateModeUI() {
    modeButtons.forEach((btn) => {
      const on = btn.dataset.mode === selectedMode;
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const meta = MODE_META[selectedMode];
    modeHint.textContent = meta ? meta.hint : "";
    if (useSnapshotInput) {
      if (selectedMode === "auto") {
        useSnapshotInput.checked = true;
        useSnapshotInput.disabled = true;
      } else {
        useSnapshotInput.disabled = false;
      }
    }
    if (snapshotSwitch) {
      const locked = selectedMode === "auto";
      snapshotSwitch.classList.toggle("is-locked", locked);
      snapshotSwitch.title = locked
        ? "Quality-Max 必须使用快照"
        : "使用证据快照";
    }
    const snapTag = snapshotLabel(useSnapshotEnabled());
    navModeBadge.textContent = `${modeLabel(selectedMode)} · ${snapTag}`;
    runningModeBadge.textContent = `${modeLabel(selectedMode)} · ${snapTag}`;
    updateSnapshotHint();
  }

  function appendLogTo(container, level, message, lang) {
    const levelClass =
      level === "SUCCESS"
        ? "text-emerald-300"
        : level === "WARN"
          ? "text-amber-300"
          : level === "ERROR"
            ? "text-red-300"
            : "text-sky-300";
    const langClass = lang === "en" ? "text-violet-300" : "text-cyan-300";
    const line = document.createElement("div");
    line.className = "text-white/70";
    line.innerHTML =
      `[${stamp()}] <span class="${levelClass}">${level}</span> ` +
      `<span class="${langClass}">[${lang === "en" ? "EN" : "ZH"}]</span>: ${escapeHtml(message || "")}`;
    container.appendChild(line);
    container.scrollTop = container.scrollHeight;
  }

  function appendLogLine(level, message, lang) {
    const resolvedLang = lang === "en" ? "en" : "zh";
    appendLogTo(runningLogBody, level, message, resolvedLang);
    appendLogTo(logBody, level, message, resolvedLang);
    lastLogs.push({
      level,
      message: message || "",
      lang: resolvedLang,
      ts: new Date().toISOString(),
    });
  }

  /** 同时追加中英两条独立日志（兼容前端本地事件）。 */
  function appendLog(level, zh, en) {
    if (zh) appendLogLine(level, zh, "zh");
    if (en) appendLogLine(level, en, "en");
  }

  function ingestServerLog(evt) {
    const level = evt.level || "INFO";
    if (evt.message != null && evt.lang) {
      appendLogLine(level, evt.message, evt.lang);
    } else if (evt.zh || evt.en) {
      // 兼容旧格式
      appendLog(level, evt.zh || "", evt.en || "");
    } else if (evt.message) {
      appendLogLine(level, evt.message, "zh");
    }
    if (typeof evt.progress === "number" && (evt.lang === "zh" || !evt.lang)) {
      setProgress(evt.progress, evt.message || evt.zh || progressLabel.textContent);
    }
  }

  function setProgress(pct, label) {
    const value = Math.max(0, Math.min(100, Math.round(pct)));
    progressBar.style.width = `${value}%`;
    progressPct.textContent = `${value}%`;
    if (label) {
      progressLabel.textContent = label;
      progressLabel.title = label;
    }
  }

  function setReuploadVisible(visible) {
    if (visible) {
      reuploadBtn.classList.remove("hidden");
    } else {
      reuploadBtn.classList.add("hidden");
    }
  }

  function showUploadOnly() {
    uploadSection.classList.remove("hidden");
    runningSection.classList.add("hidden");
    runningSection.classList.remove("flex");
    resultsSection.classList.add("hidden");
    resultsSection.classList.remove("flex");
    setReuploadVisible(false);
  }

  function showRunning() {
    uploadSection.classList.add("hidden");
    runningSection.classList.remove("hidden");
    runningSection.classList.add("flex");
    resultsSection.classList.add("hidden");
    resultsSection.classList.remove("flex");
    setReuploadVisible(false);
  }

  function showResults() {
    uploadSection.classList.add("hidden");
    runningSection.classList.add("hidden");
    runningSection.classList.remove("flex");
    resultsSection.classList.remove("hidden");
    resultsSection.classList.add("flex");
    setReuploadVisible(true);
  }

  function setRunBtnRunning(running) {
    if (running) {
      runBtn.classList.add("is-running");
      runBtn.disabled = true;
      runBtnLabel.textContent = "筛选进行中…";
    } else {
      runBtn.classList.remove("is-running");
      runBtnLabel.textContent = "开始筛选";
      runBtn.disabled = !selectedFile;
    }
  }

  function setFile(file) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".sdf")) {
      setError("仅支持 .sdf 文件");
      return;
    }
    selectedFile = file;
    fileNameEl.textContent = file.name;
    fileNameEl.classList.remove("hidden");
    dropzone.classList.add("border-primary/60", "bg-primary/5");
    runBtn.disabled = false;
    setError("");
  }

  function clearFile() {
    selectedFile = null;
    fileInput.value = "";
    fileNameEl.textContent = "";
    fileNameEl.classList.add("hidden");
    dropzone.classList.remove("border-primary/60", "bg-primary/5");
    runBtn.disabled = true;
  }

  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }

  function saveHistory(list) {
    const clipped = list.slice(0, HISTORY_LIMIT);
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(clipped));
      return;
    } catch (err) {
      // 配额不足：先丢掉旧记录的 PDF，保留最新一条 PDF
      const slimPdf = clipped.map((item, i) =>
        i === 0
          ? item
          : { ...item, mechanismPdfBase64: null }
      );
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(slimPdf));
        return;
      } catch {
        /* continue */
      }
      // 再砍大 CSV
      const slim = slimPdf.map((item) => ({
        ...item,
        csv: item.csv && item.csv.length > 120000 ? null : item.csv,
        mechanismPdfBase64: item.mechanismPdfBase64,
      }));
      try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(slim));
      } catch {
        console.warn("history save failed", err);
      }
    }
  }

  function snapshotLogs(logs) {
    return (logs || []).map((e) => {
      if (e.message != null) {
        return {
          level: e.level || "INFO",
          message: e.message || "",
          lang: e.lang === "en" ? "en" : "zh",
          ts: e.ts || "",
          progress: e.progress,
        };
      }
      // 旧格式 {zh,en} → 拆成两条
      const out = [];
      if (e.zh) {
        out.push({
          level: e.level || "INFO",
          message: e.zh,
          lang: "zh",
          ts: e.ts || "",
        });
      }
      if (e.en) {
        out.push({
          level: e.level || "INFO",
          message: e.en,
          lang: "en",
          ts: e.ts || "",
        });
      }
      return out;
    }).flat();
  }

  function pushHistory(record) {
    const list = loadHistory();
    list.unshift({
      ...record,
      logs: snapshotLogs(record.logs),
    });
    saveHistory(list);
  }

  function clearHistory() {
    try {
      localStorage.removeItem(HISTORY_KEY);
    } catch {
      /* ignore */
    }
    lastHistoryId = null;
    renderHistory();
  }

  function openHistoryClearModal() {
    if (!historyClearModal) return;
    historyClearModal.classList.remove("hidden");
    historyClearModal.classList.add("flex");
    historyClearModal.setAttribute("aria-hidden", "false");
  }

  function closeHistoryClearModal() {
    if (!historyClearModal) return;
    historyClearModal.classList.add("hidden");
    historyClearModal.classList.remove("flex");
    historyClearModal.setAttribute("aria-hidden", "true");
  }

  function downloadText(filename, text, mime) {
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  function downloadBase64Pdf(filename, b64) {
    if (!b64) return;
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    const blob = new Blob([bytes], { type: "application/pdf" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "mechanism_hypothesis.pdf";
    a.click();
    URL.revokeObjectURL(url);
  }

  function logsToText(logs) {
    return (logs || [])
      .map((e) => {
        const t = e.ts || "";
        const level = e.level || "INFO";
        if (e.message != null) {
          const lang = (e.lang || "zh").toUpperCase();
          return `[${t}] ${level} [${lang}]: ${e.message}`;
        }
        const zh = e.zh || "";
        const en = e.en || "";
        const lines = [];
        if (zh) lines.push(`[${t}] ${level} [ZH]: ${zh}`);
        if (en) lines.push(`[${t}] ${level} [EN]: ${en}`);
        return lines.join("\n");
      })
      .filter(Boolean)
      .join("\n");
  }

  function renderHistory() {
    const list = loadHistory();
    historyList.innerHTML = "";
    if (!list.length) {
      historyList.innerHTML =
        '<p class="text-sm text-on-surface-variant px-2 py-6 text-center">暂无执行记录</p>';
      return;
    }
    for (const item of list) {
      const statusClass =
        item.status === "success"
          ? "status-pill-success"
          : item.status === "error"
            ? "status-pill-error"
            : "status-pill-stopped";
      const statusText =
        item.status === "success" ? "成功" : item.status === "error" ? "失败" : "停止";
      const card = document.createElement("div");
      card.className = "glass-panel rounded-2xl p-4 flex flex-col gap-3";
      card.innerHTML = `
        <div class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <div class="font-mono text-sm text-primary truncate">${escapeHtml(item.filename || "—")}</div>
            <div class="text-xs text-on-surface-variant mt-1">${escapeHtml(item.time || "")}</div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <span class="glass-status-tag ${item.useSnapshot === false ? "status-pill-stopped" : "status-pill-success"}">${escapeHtml(snapshotLabel(item.useSnapshot !== false))}</span>
            <span class="glass-status-tag ${statusClass}">${statusText}</span>
          </div>
        </div>
        <div class="text-xs text-on-surface-variant flex flex-wrap gap-x-3 gap-y-1 items-center">
          <span>Top ${escapeHtml(String(item.topN ?? "—"))}</span>
          <span>${escapeHtml(modeLabel(item.mode || "auto"))}</span>
        </div>
        <div class="flex gap-2 flex-wrap"></div>
      `;
      const actions = card.lastElementChild;
      // 成功 / 失败 / 停止 均可下载运行日志
      const logBtn = document.createElement("button");
      logBtn.type = "button";
      logBtn.className =
        "glass-btn px-3 py-2 rounded-full text-xs inline-flex items-center gap-1";
      logBtn.innerHTML =
        '<span class="material-symbols-outlined text-[16px]">description</span>下载运行日志';
      const hasLogs = Array.isArray(item.logs) && item.logs.length > 0;
      logBtn.disabled = !hasLogs;
      if (!hasLogs) logBtn.title = "无可用日志";
      logBtn.addEventListener("click", () => {
        if (!hasLogs) return;
        const base = (item.filename || "run").replace(/\.sdf$/i, "");
        const suffix =
          item.status === "success"
            ? "success"
            : item.status === "error"
              ? "failed"
              : "stopped";
        downloadText(
          `${base}_${suffix}_runlog.txt`,
          logsToText(item.logs),
          "text/plain;charset=utf-8"
        );
      });
      actions.appendChild(logBtn);

      // CSV / 机制 PDF 仅成功记录显示
      if (item.status === "success" && item.csv) {
        const dl = document.createElement("button");
        dl.type = "button";
        dl.className =
          "glass-btn glass-btn-primary px-3 py-2 rounded-full text-xs inline-flex items-center gap-1";
        dl.innerHTML =
          '<span class="material-symbols-outlined text-[16px]">download</span>下载 CSV';
        dl.addEventListener("click", () => {
          downloadText(
            item.downloadName || "nomination.csv",
            "\ufeff" + item.csv,
            "text/csv;charset=utf-8"
          );
        });
        actions.appendChild(dl);
      }
      if (item.status === "success" && item.mechanismPdfBase64) {
        const pdfBtn = document.createElement("button");
        pdfBtn.type = "button";
        pdfBtn.className =
          "glass-btn px-3 py-2 rounded-full text-xs inline-flex items-center gap-1";
        pdfBtn.innerHTML =
          '<span class="material-symbols-outlined text-[16px]">picture_as_pdf</span>机制假说 PDF';
        pdfBtn.addEventListener("click", () => {
          downloadBase64Pdf(
            item.mechanismPdfName || "mechanism_hypothesis.pdf",
            item.mechanismPdfBase64
          );
        });
        actions.appendChild(pdfBtn);
      } else if (item.status === "success" && item.mechanismJobId && !item.mechanismPdfBase64) {
        const pendingBtn = document.createElement("button");
        pendingBtn.type = "button";
        pendingBtn.className =
          "glass-btn px-3 py-2 rounded-full text-xs inline-flex items-center gap-1 opacity-70";
        pendingBtn.innerHTML =
          '<span class="material-symbols-outlined text-[16px]">hourglass_top</span>机制 PDF 生成中';
        pendingBtn.title = "后台生成完成后将可下载；也可点此刷新状态";
        pendingBtn.addEventListener("click", async () => {
          try {
            const resp = await fetch(
              window.MOLMIND_MECHANISM_STATUS(item.mechanismJobId)
            );
            if (!resp.ok) throw new Error("job missing");
            const data = await resp.json();
            if (data.status === "ready" && data.mechanism_pdf_base64) {
              lastMechanismJobId = item.mechanismJobId;
              lastHistoryId = item.id;
              applyMechanismReady(data);
            } else if (data.status === "error") {
              pendingBtn.textContent = "机制 PDF 失败";
            } else {
              pendingBtn.innerHTML =
                '<span class="material-symbols-outlined text-[16px]">hourglass_top</span>仍在生成…';
            }
          } catch {
            pendingBtn.innerHTML =
              '<span class="material-symbols-outlined text-[16px]">wifi_off</span>任务已失效';
            pendingBtn.disabled = true;
          }
        });
        actions.appendChild(pendingBtn);
      }
      historyList.appendChild(card);
    }
  }

  function openHistory() {
    renderHistory();
    historyOverlay.classList.remove("hidden");
    historyPanel.classList.remove("translate-x-full");
    historyOverlay.setAttribute("aria-hidden", "false");
  }

  function closeHistory() {
    historyOverlay.classList.add("hidden");
    historyPanel.classList.add("translate-x-full");
    historyOverlay.setAttribute("aria-hidden", "true");
  }

  function renderSummary(data) {
    const s = data.summary;
    const d = s.diagnostics || {};
    document.getElementById("statRaw").textContent =
      s.raw_count != null ? s.raw_count : s.input_count;
    document.getElementById("statSkipped").textContent =
      s.parse_skipped != null ? s.parse_skipped : 0;
    document.getElementById("statInput").textContent = s.input_count;
    document.getElementById("statFiltered").textContent = s.filtered_out;
    document.getElementById("statEligible").textContent = s.eligible_count;
    document.getElementById("statOutput").textContent = s.output_count;
    document.getElementById("statStdTox").textContent =
      d.std_tox != null ? Number(d.std_tox).toFixed(4) : "—";
    document.getElementById("statScaffold").textContent =
      d.scaffold_diversity_top10 != null ? d.scaffold_diversity_top10 : "—";

    const qualityEl = document.getElementById("statQuality");
    if (d.engineering_pass === true) {
      const scienceReady = d.scientific_validation_status === "validated";
      qualityEl.textContent = scienceReady ? "VALIDATED" : "工程 PASS · 科学未验证";
      qualityEl.className = scienceReady
        ? "text-lg font-semibold tabular-nums text-primary"
        : "text-sm font-semibold tabular-nums text-amber-600";
    } else if (d.quality_pass === false) {
      qualityEl.textContent = "FAIL";
      qualityEl.className = "text-lg font-semibold tabular-nums text-red-600";
    } else {
      qualityEl.textContent = "—";
      qualityEl.className = "text-lg font-semibold tabular-nums text-on-surface";
    }

    document.getElementById("statHash").textContent = s.config_hash || "—";
    summaryBadge.textContent = `${modeLabel(s.mode)} · ${snapshotLabel(s.use_snapshot !== false)} · 输出 ${s.output_count} / Top ${s.requested_top_n}`;

    if (s.note) {
      noteBanner.textContent = s.note;
      noteBanner.classList.remove("hidden");
    } else {
      noteBanner.classList.add("hidden");
    }

    const degraded = s.degraded_channels || [];
    if (degraded.length) {
      degradedBanner.textContent = "degraded: " + degraded.join(" | ");
      degradedBanner.classList.remove("hidden");
    } else {
      degradedBanner.classList.add("hidden");
    }
  }

  function cellNum(v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (Number.isNaN(n)) return escapeHtml(String(v));
    return Number.isInteger(n) ? String(n) : n.toFixed(4);
  }

  function cellText(v, wide) {
    const s = v == null || v === "" ? "—" : String(v);
    const cls = wide
      ? "p-3 text-sm text-on-surface-variant max-w-[220px] whitespace-normal break-words"
      : "p-3 font-mono text-xs text-on-surface-variant max-w-[160px] truncate";
    return `<td class="${cls}" title="${escapeHtml(s)}">${escapeHtml(s)}</td>`;
  }

  function renderTable(data) {
    resultBody.innerHTML = "";
    for (const row of data.rows) {
      const tr = document.createElement("tr");
      tr.className = "hover:bg-white/60 transition-colors duration-150";
      tr.innerHTML = `
        <td class="p-3 font-mono text-sm tabular-nums">${row.rank}</td>
        <td class="p-3 font-mono text-sm text-primary font-medium whitespace-nowrap">${escapeHtml(String(row.molecule_id ?? "—"))}</td>
        <td class="p-3 font-mono text-xs tabular-nums whitespace-nowrap">${escapeHtml(String(row.cas || "—"))}</td>
        <td class="p-3 font-mono text-xs max-w-[140px] truncate" title="${escapeHtml(String(row.inchikey || ""))}">${escapeHtml(String(row.inchikey || "—"))}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.lipid_score)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_risk)}</td>
        <td class="p-3 font-mono text-xs whitespace-nowrap">${escapeHtml(String(row.eligibility_status || "—"))}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.toxicity_confidence)}</td>
        <td class="p-3 font-mono text-sm tabular-nums font-bold text-primary">${cellNum(row.final_score)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.novelty_score)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.conf_e)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_alert)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_physchem)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_dili)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_admet)}</td>
        <td class="p-3 font-mono text-sm tabular-nums">${cellNum(row.tox_evidence)}</td>
        ${cellText(row.scaffold, false)}
        ${cellText(row.lipid_rationale, true)}
        ${cellText(row.tox_rationale, true)}
        ${cellText(row.overall_reason, true)}
        <td class="p-3 font-mono text-xs whitespace-nowrap">${escapeHtml(String(row.run_mode || "—"))}</td>
        <td class="p-3 font-mono text-xs max-w-[120px] truncate" title="${escapeHtml(String(row.config_hash || ""))}">${escapeHtml(String(row.config_hash || "—"))}</td>
        <td class="p-3 font-mono text-xs max-w-[140px] truncate" title="${escapeHtml(String(row.degraded_channels || ""))}">${escapeHtml(String(row.degraded_channels || "—"))}</td>
      `;
      resultBody.appendChild(tr);
    }

    toolbarMeta.textContent = `来源：${data.summary.source} · ${modeLabel(data.summary.mode)} · ${snapshotLabel(data.summary.use_snapshot !== false)} · Top ${data.summary.requested_top_n}`;
  }

  async function readNdjsonStream(resp, onEvent) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        onEvent(JSON.parse(trimmed));
      }
    }
    if (buffer.trim()) onEvent(JSON.parse(buffer.trim()));
  }

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) setFile(fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("border-primary/50", "bg-white/40");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("border-primary/50", "bg-white/40");
    });
  });
  dropzone.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) setFile(file);
  });

  modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      selectedMode = btn.dataset.mode;
      updateModeUI();
    });
  });

  if (useSnapshotInput) {
    useSnapshotInput.addEventListener("change", updateModeUI);
  }

  if (snapshotSwitch) {
    snapshotSwitch.addEventListener("click", (e) => {
      if (selectedMode !== "auto") return;
      e.preventDefault();
      if (useSnapshotInput) useSnapshotInput.checked = true;
      showSnapshotLockToast();
    });
  }

  topNInput.addEventListener("change", () => {
    topNInput.value = String(clampTopN(topNInput.value));
  });

  historyBtn.addEventListener("click", openHistory);
  historyCloseBtn.addEventListener("click", closeHistory);
  historyOverlay.addEventListener("click", closeHistory);
  if (historyClearBtn) {
    historyClearBtn.addEventListener("click", () => {
      if (!loadHistory().length) return;
      openHistoryClearModal();
    });
  }
  if (historyClearCancel) {
    historyClearCancel.addEventListener("click", closeHistoryClearModal);
  }
  if (historyClearModalBackdrop) {
    historyClearModalBackdrop.addEventListener("click", closeHistoryClearModal);
  }
  if (historyClearConfirm) {
    historyClearConfirm.addEventListener("click", () => {
      clearHistory();
      closeHistoryClearModal();
    });
  }

  reuploadBtn.addEventListener("click", () => {
    clearFile();
    lastCsv = null;
    lastPdfBase64 = null;
    lastMechanismJobId = null;
    stopMechanismPoll();
    setPdfButtonState("idle");
    hideMechPdfToast();
    lastLogs = [];
    logBody.innerHTML = "";
    runningLogBody.innerHTML = "";
    downloadBtn.disabled = true;
    downloadLogBtn.disabled = true;
    setError("");
    setRunBtnRunning(false);
    showUploadOnly();
  });

  stopBtn.addEventListener("click", () => {
    userStopped = true;
    if (abortController) abortController.abort();
  });

  runBtn.addEventListener("click", async () => {
    if (!selectedFile) return;

    const top = clampTopN(topNInput.value);
    topNInput.value = String(top);
    const mode = selectedMode;
    const useSnapshot = useSnapshotEnabled();
    const filename = selectedFile.name;
    const startedAt = new Date();

    setError("");
    userStopped = false;
    lastCsv = null;
    lastPdfBase64 = null;
    lastMechanismJobId = null;
    stopMechanismPoll();
    setPdfButtonState("idle");
    hideMechPdfToast();
    lastLogs = [];
    logBody.innerHTML = "";
    runningLogBody.innerHTML = "";
    downloadBtn.disabled = true;
    downloadLogBtn.disabled = true;
    setRunBtnRunning(true);
    updateModeUI();
    showRunning();
    setProgress(0, "正在解析、多维打分与 Critic…");
    appendLog(
      "INFO",
      `开始筛选 · ${filename} · Top ${top} · ${modeLabel(mode)} · ${snapshotLabel(useSnapshot)}`,
      `Start screen · ${filename} · Top ${top} · ${modeLabel(mode)} · ${useSnapshot ? "use snapshot" : "no snapshot"}`
    );

    abortController = new AbortController();
    const form = new FormData();
    form.append("file", selectedFile);

    let resultPayload = null;
    let streamError = null;

    try {
      const resp = await fetch(window.MOLMIND_SCREEN_STREAM(top, mode, useSnapshot), {
        method: "POST",
        body: form,
        signal: abortController.signal,
      });

      if (!resp.ok) {
        let detail = "筛选失败";
        try {
          const errBody = await resp.json();
          detail = errBody.detail || detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }

      await readNdjsonStream(resp, (evt) => {
        if (evt.type === "log") {
          ingestServerLog(evt);
        } else if (evt.type === "result") {
          resultPayload = evt;
          if (Array.isArray(evt.logs) && evt.logs.length && lastLogs.length === 0) {
            for (const e of evt.logs) {
              ingestServerLog(e);
            }
          }
        } else if (evt.type === "error") {
          streamError = evt.detail || "筛选失败";
        }
      });

      if (streamError) throw new Error(streamError);
      if (!resultPayload) throw new Error("未收到筛选结果");

      lastCsv = resultPayload.csv;
      lastLogs = resultPayload.logs || lastLogs;
      lastDownloadName = filename.replace(/\.sdf$/i, "") + `_nomination_top${top}.csv`;
      lastPdfBase64 = null;
      lastPdfName =
        filename.replace(/\.sdf$/i, "") + "_mechanism_hypothesis.pdf";

      setProgress(100, "筛选完成（机制 PDF 后台生成中）");
      renderSummary(resultPayload);
      renderTable(resultPayload);
      downloadBtn.disabled = false;
      downloadLogBtn.disabled = false;
      showResults();

      const jobId = resultPayload.mechanism_job_id || (resultPayload.summary && resultPayload.summary.mechanism_job_id);
      const historyId = `${startedAt.getTime()}_${Math.random().toString(36).slice(2, 8)}`;
      lastHistoryId = historyId;
      lastPdfName =
        filename.replace(/\.sdf$/i, "") + "_mechanism_hypothesis.pdf";
      pushHistory({
        id: historyId,
        status: "success",
        filename,
        mode,
        useSnapshot,
        topN: top,
        uiBuild: UI_BUILD_MARK,
        time: startedAt.toLocaleString("zh-CN", { hour12: false }),
        csv: lastCsv,
        logs: lastLogs,
        downloadName: lastDownloadName,
        mechanismJobId: jobId || null,
        mechanismPdfBase64: null,
        mechanismPdfName: lastPdfName,
      });

      if (jobId) {
        pollMechanismJob(jobId);
      } else if (resultPayload.mechanism_pdf_base64) {
        applyMechanismReady(resultPayload);
      } else {
        setPdfButtonState("error");
      }
    } catch (err) {
      if (userStopped || (err && err.name === "AbortError")) {
        appendLog("WARN", "用户停止筛选", "Screening stopped by user");
        setProgress(0, "已停止");
        pushHistory({
          id: `${startedAt.getTime()}_${Math.random().toString(36).slice(2, 8)}`,
          status: "stopped",
          filename,
          mode,
          useSnapshot,
          topN: top,
          time: startedAt.toLocaleString("zh-CN", { hour12: false }),
          csv: null,
          logs: lastLogs,
          downloadName: null,
        });
        showUploadOnly();
      } else {
        const msg = (err && err.message) || "请求失败，请确认服务已启动";
        setError(msg);
        appendLog("ERROR", msg, msg);
        pushHistory({
          id: `${startedAt.getTime()}_${Math.random().toString(36).slice(2, 8)}`,
          status: "error",
          filename,
          mode,
          useSnapshot,
          topN: top,
          time: startedAt.toLocaleString("zh-CN", { hour12: false }),
          csv: null,
          logs: lastLogs,
          downloadName: null,
        });
        showUploadOnly();
      }
    } finally {
      abortController = null;
      setRunBtnRunning(false);
    }
  });

  downloadBtn.addEventListener("click", () => {
    if (!lastCsv) return;
    downloadText(lastDownloadName, "\ufeff" + lastCsv, "text/csv;charset=utf-8");
    appendLog("INFO", `已下载 ${lastDownloadName}`, `Downloaded ${lastDownloadName}`);
  });

  downloadLogBtn.addEventListener("click", () => {
    if (!lastLogs.length) return;
    const name =
      (selectedFile && selectedFile.name
        ? selectedFile.name.replace(/\.sdf$/i, "")
        : "molmind") + "_runlog.txt";
    downloadText(name, logsToText(lastLogs), "text/plain;charset=utf-8");
  });

  if (downloadPdfBtn) {
    downloadPdfBtn.addEventListener("click", () => {
      if (!lastPdfBase64) return;
      downloadBase64Pdf(lastPdfName, lastPdfBase64);
      appendLog("INFO", `已下载 ${lastPdfName}`, `Downloaded ${lastPdfName}`);
    });
  }

  setPdfButtonState("idle");
  updateModeUI();
  showUploadOnly();
})();
