(() => {
  /* UI build lineage: yluo / LJR — do not strip */
  const TOP_N_MIN = window.MOLMIND_TOP_N_MIN || 1;
  const TOP_N_MAX = window.MOLMIND_TOP_N_MAX || 50;
  const HISTORY_KEY = "molmind_run_history_v1";
  const HISTORY_LIMIT = 30;
  const UI_BUILD_MARK = "mm.yluo.ui";

  const useSnapshotInput = document.getElementById("useSnapshot");
  const allowLiveInput = document.getElementById("allowLive");
  const nominationReviewInput = document.getElementById("nominationReview");
  const runtimeHint = document.getElementById("runtimeHint");
  const liveHint = document.getElementById("liveHint");
  const snapshotHint = document.getElementById("snapshotHint");
  const reviewHint = document.getElementById("reviewHint");
  const snapshotSwitch = document.getElementById("snapshotSwitch");
  const liveSwitch = document.getElementById("liveSwitch");
  const reviewSwitch = document.getElementById("reviewSwitch");

  let selectedFile = null;
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
  const agentSection = document.getElementById("agentSection");
  const noteBanner = document.getElementById("noteBanner");
  let workMode = "agent";
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
  const reviewModal = document.getElementById("reviewModal");
  const reviewModalBackdrop = document.getElementById("reviewModalBackdrop");
  const reviewModalHint = document.getElementById("reviewModalHint");
  const reviewProposalList = document.getElementById("reviewProposalList");
  const reviewApplyBtn = document.getElementById("reviewApplyBtn");
  const navModeBadge = document.getElementById("navModeBadge");
  const runningModeBadge = document.getElementById("runningModeBadge");

  let lastCsv = null;
  let lastLogs = [];
  let lastDownloadName = "nomination_top10.csv";
  let lastPdfBase64 = null;
  let lastPdfName = "mechanism_hypothesis.pdf";
  let lastMechanismJobId = null;
  let lastHistoryId = null;
  let lastResultPayload = null;
  let pendingReviewMeta = null;
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
        } else if (["cancel_requested", "cancelled"].includes(data.status)) {
          stopMechanismPoll();
          setPdfButtonState("idle");
          appendLog("INFO", "机制 PDF 任务已取消。", "Mechanism PDF job cancelled.");
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

  function allowLiveEnabled() {
    return !!(allowLiveInput && allowLiveInput.checked);
  }

  function nominationReviewEnabled() {
    // Default ON when DOM missing (older cached HTML).
    if (!nominationReviewInput) return true;
    return !!nominationReviewInput.checked;
  }

  function updateSnapshotHint() {
    if (!snapshotHint) return;
    snapshotHint.textContent = useSnapshotEnabled()
      ? "开启：优先读取本地 evidence snapshot，可复现且更快。"
      : "关闭：不读取本地快照（仅规则/本地表/联网路径，可复现路径不建议）。";
  }

  function updateLiveHint() {
    if (!liveHint) return;
    liveHint.textContent = allowLiveEnabled()
      ? "开启：候选短名单尝试 ChEMBL/PubChem live 补洞；定稿前请烘焙快照并关闭此开关复跑。"
      : "关闭（默认）：不访问外网证据 API，仅用本地快照/规则路径（可复现）。";
  }

  function updateReviewHint() {
    if (!reviewHint) return;
    reviewHint.textContent = nominationReviewEnabled()
      ? "开启：算法榜后先出 LLM/规则草案，再弹窗人工确认，确认后导出最终结果。"
      : "关闭：跳过 LLM 草案与人工复核弹窗，筛选结束后直接出结果。";
  }

  function runtimeBadgeText() {
    const parts = ["Quality-Max", snapshotLabel(useSnapshotEnabled())];
    if (allowLiveEnabled()) parts.push("联网开");
    parts.push(nominationReviewEnabled() ? "复核开" : "复核关");
    return parts.join(" · ");
  }

  function updateRuntimeUI() {
    updateSnapshotHint();
    updateLiveHint();
    updateReviewHint();
    if (runtimeHint) {
      const bits = [];
      bits.push(
        allowLiveEnabled()
          ? "已开启联网补证据：结果可能随外网波动；正式定稿前请烘焙快照并关闭联网复跑。"
          : "默认路径：读取本地证据快照、不联网，保证可复现运行。"
      );
      bits.push(
        nominationReviewEnabled()
          ? "LLM+人工复核已开启：结果前需确认弹窗。"
          : "LLM+人工复核已关闭：直接出结果。"
      );
      runtimeHint.textContent = bits.join(" ");
    }
    const badge = runtimeBadgeText();
    navModeBadge.textContent = badge;
    runningModeBadge.textContent = badge;
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

  function modeLabel() {
    return "Quality-Max";
  }

  function snapshotLabel(enabled) {
    return enabled ? "使用快照" : "未使用快照";
  }

  function clampTopN(raw) {
    const n = parseInt(raw, 10);
    if (Number.isNaN(n)) return 10;
    return Math.min(TOP_N_MAX, Math.max(TOP_N_MIN, n));
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
    const agentOn = workMode === "agent";
    document.body.classList.toggle("mm-body-agent", agentOn);
    if (agentOn) {
      if (agentSection) agentSection.classList.remove("hidden");
      uploadSection.classList.add("hidden");
    } else {
      if (agentSection) agentSection.classList.add("hidden");
      uploadSection.classList.remove("hidden");
    }
    runningSection.classList.add("hidden");
    runningSection.classList.remove("flex");
    resultsSection.classList.add("hidden");
    resultsSection.classList.remove("flex");
    setReuploadVisible(false);
  }

  function showRunning() {
    document.body.classList.remove("mm-body-agent");
    if (agentSection) agentSection.classList.add("hidden");
    uploadSection.classList.add("hidden");
    runningSection.classList.remove("hidden");
    runningSection.classList.add("flex");
    resultsSection.classList.add("hidden");
    resultsSection.classList.remove("flex");
    setReuploadVisible(false);
  }

  function showResults() {
    document.body.classList.remove("mm-body-agent");
    if (agentSection) agentSection.classList.add("hidden");
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
            <span class="glass-status-tag ${item.allowLive ? "status-pill-stopped" : "status-pill-success"}">${escapeHtml(item.allowLive ? "联网开" : "联网关")}</span>
            <span class="glass-status-tag ${item.nominationReview === false ? "status-pill-stopped" : "status-pill-success"}">${escapeHtml(item.nominationReview === false ? "复核关" : "复核开")}</span>
            <span class="glass-status-tag ${item.useSnapshot === false ? "status-pill-stopped" : "status-pill-success"}">${escapeHtml(snapshotLabel(item.useSnapshot !== false))}</span>
            <span class="glass-status-tag ${statusClass}">${statusText}</span>
          </div>
        </div>
        <div class="text-xs text-on-surface-variant flex flex-wrap gap-x-3 gap-y-1 items-center">
          <span>Top ${escapeHtml(String(item.topN ?? "—"))}</span>
          <span>${escapeHtml(modeLabel())}</span>
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

  function severityClass(sev) {
    if (sev === "high") return "text-red-700 bg-red-50 border-red-200";
    if (sev === "medium") return "text-amber-800 bg-amber-50 border-amber-200";
    return "text-on-surface-variant bg-white/70 border-white/80";
  }

  function actionLabel(action) {
    if (action === "drop_from_primary") return "建议移出主榜";
    if (action === "annotate") return "建议脚注";
    if (action === "keep") return "建议保留";
    return action || "—";
  }

  function closeReviewModal() {
    if (!reviewModal) return;
    reviewModal.classList.add("hidden");
    reviewModal.classList.remove("flex");
    reviewModal.setAttribute("aria-hidden", "true");
  }

  function decisionBadge(decision) {
    const d = String(decision || "").toUpperCase();
    if (d === "DROP") return "建议移出主榜";
    if (d.includes("NOTE")) return "KEEP+NOTE";
    if (d === "KEEP") return "KEEP";
    return actionLabel(decision);
  }

  function actionableReviewProposals(review) {
    const proposals = Array.isArray(review.proposals) ? review.proposals : [];
    const seats = Array.isArray(review.seat_decisions) ? review.seat_decisions : [];
    if (seats.length > 0) {
      // 席位表已展示 KEEP；勾选区保留 DROP / KEEP+NOTE（annotate）与高优先级项。
      return proposals.filter(
        (p) =>
          p.suggested_action === "drop_from_primary" ||
          p.suggested_action === "annotate" ||
          String(p.severity || "") === "high"
      );
    }
    return proposals;
  }

  function openReviewModal(data) {
    if (!reviewModal || !reviewProposalList) return false;
    const review = (data && data.interactive_review) || {};
    const enabled =
      Boolean(review.enabled) ||
      Boolean(data && data.summary && data.summary.nomination_review);
    const seats = Array.isArray(review.seat_decisions) ? review.seat_decisions : [];
    const proposals = Array.isArray(review.proposals) ? review.proposals : [];
    const actionable = actionableReviewProposals(review);
    const hasReviewContent =
      seats.length > 0 ||
      proposals.length > 0 ||
      Boolean(review.conclusion) ||
      Boolean(review.intro);
    // 复核开启且有草案内容即弹窗；即使无可勾选动作也需人工确认后导出。
    if (!enabled || !hasReviewContent) {
      closeReviewModal();
      return false;
    }
    if (reviewModalHint) {
      const engine = review.draft_engine || "rules";
      const llm = review.llm_used ? "LLM 逐席草案" : "规则草案（无 LLM）";
      reviewModalHint.textContent = `${llm} · ${engine}。勾选要执行的项后点确认，生成最终 CSV 与机制假说 PDF。`;
    }
    reviewProposalList.innerHTML = "";

    if (review.conclusion || review.intro || seats.length) {
      const narrative = document.createElement("div");
      narrative.className =
        "rounded-2xl border border-primary/20 bg-white/70 p-4 flex flex-col gap-2 shrink-0";
      const counts = review.summary_counts || {};
      const countLine =
        counts.keep != null
          ? `<div class="text-xs text-on-surface-variant">汇总：KEEP ${escapeHtml(String(counts.keep))} · KEEP+NOTE ${escapeHtml(String(counts.keep_note ?? 0))} · DROP ${escapeHtml(String(counts.drop ?? 0))}</div>`
          : "";
      const extraNotes = Array.isArray(counts.extra_notes)
        ? counts.extra_notes
            .slice(0, 3)
            .map(
              (n) =>
                `<li class="text-xs text-on-surface-variant leading-snug">${escapeHtml(String(n))}</li>`
            )
            .join("")
        : "";
      let seatsHtml = "";
      if (seats.length) {
        seatsHtml = `
          <div class="overflow-x-auto rounded-xl border border-white/80">
            <table class="w-full text-left text-xs min-w-[480px]">
              <thead class="bg-white/90 text-on-surface-variant sticky top-0">
                <tr>
                  <th class="p-2 font-medium w-10">#</th>
                  <th class="p-2 font-medium">ID</th>
                  <th class="p-2 font-medium">识别</th>
                  <th class="p-2 font-medium">决定</th>
                  <th class="p-2 font-medium">要点</th>
                </tr>
              </thead>
              <tbody>
                ${seats
                  .map((s) => {
                    const dec = String(s.decision || "");
                    const rationale = String(s.rationale || "");
                    const rowCls =
                      dec.toUpperCase() === "DROP"
                        ? "bg-red-50/70"
                        : String(dec).includes("NOTE")
                          ? "bg-amber-50/40"
                          : "";
                    return `<tr class="border-t border-white/70 ${rowCls}">
                      <td class="p-2 font-mono tabular-nums align-top">${escapeHtml(String(s.rank ?? ""))}</td>
                      <td class="p-2 font-mono font-semibold text-primary align-top whitespace-nowrap">${escapeHtml(String(s.molecule_id || ""))}</td>
                      <td class="p-2 align-top max-w-[7rem]">${escapeHtml(String(s.identity_label || "—"))}</td>
                      <td class="p-2 whitespace-nowrap font-semibold align-top">${escapeHtml(decisionBadge(dec))}</td>
                      <td class="p-2 leading-snug text-on-surface-variant align-top" title="${escapeHtml(rationale)}"><span class="line-clamp-2">${escapeHtml(rationale)}</span></td>
                    </tr>`;
                  })
                  .join("")}
              </tbody>
            </table>
          </div>`;
      }
      narrative.innerHTML = `
        ${review.conclusion ? `<p class="text-sm font-semibold text-on-background leading-snug">复核结论：${escapeHtml(String(review.conclusion))}</p>` : ""}
        ${review.intro ? `<p class="text-xs text-on-surface-variant leading-snug line-clamp-2" title="${escapeHtml(String(review.intro))}">${escapeHtml(String(review.intro))}</p>` : ""}
        ${countLine}
        ${seatsHtml}
        ${extraNotes ? `<ul class="list-disc pl-5 flex flex-col gap-0.5">${extraNotes}</ul>` : ""}
      `;
      reviewProposalList.appendChild(narrative);
    }

    const compactMode = seats.length > 0;
    const actionTitle = document.createElement("div");
    actionTitle.className =
      "text-xs font-semibold tracking-wide text-on-surface-variant uppercase mt-1 shrink-0";
    actionTitle.textContent = compactMode
      ? "勾选要执行的动作（DROP / KEEP+NOTE 脚注）"
      : "勾选要应用的复核动作";
    reviewProposalList.appendChild(actionTitle);

    if (!actionable.length) {
      const empty = document.createElement("div");
      empty.className =
        "rounded-2xl border border-white/80 bg-white/60 px-3 py-2 text-sm text-on-surface-variant shrink-0";
      empty.textContent =
        "当前草案无可勾选动作；确认后按算法榜导出并启动机制假说 PDF。";
      reviewProposalList.appendChild(empty);
    }

    for (const p of actionable) {
      const id = String(p.proposal_id || "");
      const checked = p.default_selected ? "checked" : "";
      const repl = p.replacement_molecule_id
        ? ` · 补位 <span class="font-mono">${escapeHtml(String(p.replacement_molecule_id))}</span>`
        : "";
      const card = document.createElement("label");
      card.className =
        "flex gap-3 items-center rounded-2xl border px-3 py-2 cursor-pointer hover:bg-white/50 transition-colors shrink-0 " +
        severityClass(p.severity);
      card.innerHTML = `
        <input type="checkbox" class="review-proposal-cb shrink-0" data-proposal-id="${escapeHtml(id)}" ${checked} />
        <div class="min-w-0 flex-1 flex flex-wrap items-center gap-2 text-sm">
          <span class="font-mono font-semibold">${escapeHtml(String(p.molecule_id || "—"))}</span>
          <span class="text-xs rounded-full px-2 py-0.5 bg-white/70 border border-white/80">${escapeHtml(actionLabel(p.suggested_action))}</span>
          <span class="text-xs text-on-surface-variant truncate">${escapeHtml(String(p.issue_type || ""))}${repl}</span>
        </div>
      `;
      reviewProposalList.appendChild(card);
    }

    reviewModal.classList.remove("hidden");
    reviewModal.classList.add("flex");
    reviewModal.setAttribute("aria-hidden", "false");
    return true;
  }

  function selectedReviewProposalIds() {
    if (!reviewProposalList) return [];
    return Array.from(reviewProposalList.querySelectorAll(".review-proposal-cb:checked"))
      .map((el) => el.getAttribute("data-proposal-id") || "")
      .filter(Boolean);
  }

  async function applyInteractiveReview(selectedIds) {
    if (!lastResultPayload || !lastResultPayload.summary) return;
    const runId = lastResultPayload.summary.run_id;
    if (!runId) {
      setError("缺少 run_id，无法应用复核");
      return;
    }
    const meta = pendingReviewMeta || {};
    if (reviewApplyBtn) reviewApplyBtn.disabled = true;
    setProgress(96, "正在确认复核并启动机制 PDF…");
    try {
      const resp = await fetch(window.MOLMIND_APPLY_REVIEW || "/api/screen/apply-review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          run_id: runId,
          selected_proposal_ids: selectedIds,
        }),
      });
      if (!resp.ok) {
        let detail = "应用复核失败";
        try {
          const errBody = await resp.json();
          detail = errBody.detail || detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }
      const next = await resp.json();
      next.logs = next.logs && next.logs.length ? next.logs : lastResultPayload.logs || [];
      if (!next.hepg2_ffa_resources || !Object.keys(next.hepg2_ffa_resources).length) {
        next.hepg2_ffa_resources = lastResultPayload.hepg2_ffa_resources || {};
      }
      lastResultPayload = next;
      lastCsv = next.csv;
      lastLogs = next.logs || lastLogs;
      renderSummary(next);
      renderTable(next);
      downloadBtn.disabled = false;
      downloadLogBtn.disabled = false;
      setProgress(100, "筛选完成（机制 PDF 后台生成中）");
      showResults();

      const filename = meta.filename || (next.summary && next.summary.source) || "nomination.sdf";
      const top = meta.topN != null ? meta.topN : next.summary && next.summary.requested_top_n;
      lastDownloadName =
        meta.downloadName ||
        String(filename).replace(/\.sdf$/i, "") + `_nomination_top${top || 10}.csv`;
      lastPdfName =
        meta.pdfName ||
        String(filename).replace(/\.sdf$/i, "") + "_mechanism_hypothesis.pdf";

      const jobId =
        next.mechanism_job_id ||
        (next.summary && next.summary.mechanism_job_id) ||
        "";
      const historyId = `${(meta.startedAt || new Date()).getTime()}_${Math.random()
        .toString(36)
        .slice(2, 8)}`;
      lastHistoryId = historyId;
      pushHistory({
        id: historyId,
        status: "success",
        filename,
        useSnapshot: meta.useSnapshot,
        allowLive: meta.allowLive,
        nominationReview: meta.nominationReview !== false,
        topN: top,
        uiBuild: UI_BUILD_MARK,
        time: (meta.startedAt || new Date()).toLocaleString("zh-CN", { hour12: false }),
        csv: lastCsv,
        logs: lastLogs,
        downloadName: lastDownloadName,
        mechanismJobId: jobId || null,
        mechanismPdfBase64: null,
        mechanismPdfName: lastPdfName,
        reviewed: true,
        appliedProposalIds: selectedIds,
      });

      appendLog(
        "INFO",
        selectedIds.length
          ? `人工复核已确认并应用 ${selectedIds.length} 项提案；已启动最终机制 PDF`
          : "人工复核已确认：保留算法榜；已启动最终机制 PDF",
        selectedIds.length
          ? `Interactive review confirmed with ${selectedIds.length} proposal(s); mechanism PDF started`
          : "Interactive review confirmed with algorithmic board; mechanism PDF started"
      );
      closeReviewModal();
      pendingReviewMeta = null;
      setRunBtnRunning(false);
      abortController = null;

      if (jobId) {
        pollMechanismJob(jobId);
      } else {
        setPdfButtonState("error");
      }
    } catch (err) {
      setError(err && err.message ? err.message : String(err));
      setProgress(92, "复核失败，可重试");
    } finally {
      if (reviewApplyBtn) reviewApplyBtn.disabled = false;
    }
  }

  if (reviewApplyBtn) {
    reviewApplyBtn.addEventListener("click", () => {
      applyInteractiveReview(selectedReviewProposalIds());
    });
  }
  if (reviewModalBackdrop) {
    reviewModalBackdrop.addEventListener("click", (e) => {
      e.stopPropagation();
    });
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
    const liveTag = s.allow_live ? "联网开" : "联网关";
    const reviewTag = s.nomination_review ? "复核开" : "复核关";
    summaryBadge.textContent = `${modeLabel()} · ${snapshotLabel(s.use_snapshot !== false)} · ${liveTag} · ${reviewTag} · 输出 ${s.output_count} / Top ${s.requested_top_n}`;

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

    const liveTag = data.summary.allow_live ? "联网开" : "联网关";
    const reviewTag = data.summary.nomination_review ? "复核开" : "复核关";
    toolbarMeta.textContent = `来源：${data.summary.source} · ${modeLabel()} · ${snapshotLabel(data.summary.use_snapshot !== false)} · ${liveTag} · ${reviewTag} · Top ${data.summary.requested_top_n}`;
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

  if (useSnapshotInput) {
    useSnapshotInput.addEventListener("change", updateRuntimeUI);
  }
  if (allowLiveInput) {
    allowLiveInput.addEventListener("change", updateRuntimeUI);
  }
  if (nominationReviewInput) {
    nominationReviewInput.addEventListener("change", updateRuntimeUI);
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
    lastResultPayload = null;
    pendingReviewMeta = null;
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
    closeReviewModal();
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
    const useSnapshot = useSnapshotEnabled();
    const allowLive = allowLiveEnabled();
    const nominationReview = nominationReviewEnabled();
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
    updateRuntimeUI();
    showRunning();
    setProgress(0, "正在解析、多维打分与 Critic…");
    const liveTag = allowLive ? "联网开" : "联网关";
    const reviewTag = nominationReview ? "复核开" : "复核关";
    appendLog(
      "INFO",
      `开始筛选 · ${filename} · Top ${top} · ${modeLabel()} · ${snapshotLabel(useSnapshot)} · ${liveTag} · ${reviewTag}`,
      `Start screen · ${filename} · Top ${top} · ${modeLabel()} · ${useSnapshot ? "use snapshot" : "no snapshot"} · allow_live=${allowLive} · nomination_review=${nominationReview}`
    );

    abortController = new AbortController();
    const form = new FormData();
    form.append("file", selectedFile);

    let resultPayload = null;
    let reviewPendingPayload = null;
    let streamError = null;
    let awaitingReview = false;

    try {
      const resp = await fetch(
        window.MOLMIND_SCREEN_STREAM(top, useSnapshot, allowLive, nominationReview),
        {
        method: "POST",
        body: form,
        signal: abortController.signal,
      }
      );

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
        } else if (evt.type === "review_pending") {
          reviewPendingPayload = evt;
          if (Array.isArray(evt.logs) && evt.logs.length && lastLogs.length === 0) {
            for (const e of evt.logs) {
              ingestServerLog(e);
            }
          }
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

      if (reviewPendingPayload) {
        awaitingReview = true;
        lastResultPayload = reviewPendingPayload;
        lastCsv = reviewPendingPayload.csv || null;
        lastLogs = reviewPendingPayload.logs || lastLogs;
        lastDownloadName = filename.replace(/\.sdf$/i, "") + `_nomination_top${top}.csv`;
        lastPdfBase64 = null;
        lastPdfName = filename.replace(/\.sdf$/i, "") + "_mechanism_hypothesis.pdf";
        lastHistoryId = null;
        pendingReviewMeta = {
          filename,
          topN: top,
          useSnapshot,
          allowLive,
          nominationReview,
          startedAt,
          downloadName: lastDownloadName,
          pdfName: lastPdfName,
        };
        renderSummary(reviewPendingPayload);
        renderTable(reviewPendingPayload);
        downloadBtn.disabled = true;
        downloadLogBtn.disabled = false;
        showResults();
        const opened = openReviewModal(reviewPendingPayload);
        if (opened) {
          setProgress(92, "算法榜就绪，等待人工复核…");
          appendLog(
            "INFO",
            "算法主榜已就绪，请在弹窗中确认复核后再导出最终结果",
            "Algorithmic shortlist ready; confirm interactive review to finalize deliverables"
          );
        } else {
          appendLog(
            "INFO",
            "无可应用复核提案，自动确认算法榜并导出最终结果",
            "No actionable review proposals; auto-confirm algorithmic board and finalize"
          );
          await applyInteractiveReview([]);
          awaitingReview = false;
        }
      } else {
        if (!resultPayload) throw new Error("未收到筛选结果");

        lastCsv = resultPayload.csv;
        lastLogs = resultPayload.logs || lastLogs;
        lastResultPayload = resultPayload;
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
          useSnapshot,
          allowLive,
          nominationReview,
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
      }
    } catch (err) {
      awaitingReview = false;
      pendingReviewMeta = null;
      if (userStopped || (err && err.name === "AbortError")) {
        appendLog("WARN", "用户停止筛选", "Screening stopped by user");
        setProgress(0, "已停止");
        pushHistory({
          id: `${startedAt.getTime()}_${Math.random().toString(36).slice(2, 8)}`,
          status: "stopped",
          filename,
          useSnapshot,
          allowLive,
          nominationReview,
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
          useSnapshot,
          allowLive,
          nominationReview,
          topN: top,
          time: startedAt.toLocaleString("zh-CN", { hour12: false }),
          csv: null,
          logs: lastLogs,
          downloadName: null,
        });
        showUploadOnly();
      }
    } finally {
      if (!awaitingReview) {
        abortController = null;
        setRunBtnRunning(false);
      }
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
  updateRuntimeUI();

  /* ——— Agent chat (GameGhost-inspired shell) ——— */
  const modeClassicBtn = document.getElementById("modeClassicBtn");
  const modeAgentBtn = document.getElementById("modeAgentBtn");
  const agentUploadBtn = document.getElementById("agentUploadBtn");
  const agentFileInput = document.getElementById("agentFileInput");
  const agentFileLabel = document.getElementById("agentFileLabel");
  const agentAttachRail = document.getElementById("agentAttachRail");
  const agentSessionMeta = document.getElementById("agentSessionMeta");
  const agentMessages = document.getElementById("agentMessages");
  const agentChatScroll = document.getElementById("agentChatScroll");
  const agentChatRoot = document.getElementById("agentChatRoot");
  const agentChatMain = document.getElementById("agentChatMain");
  const agentChatForm = document.getElementById("agentChatForm");
  const agentInput = document.getElementById("agentInput");
  const agentSendBtn = document.getElementById("agentSendBtn");
  const agentSendShortcut = document.getElementById("agentSendShortcut");
  const agentStopBtn = document.getElementById("agentStopBtn");
  const agentGuideBtn = document.getElementById("agentGuideBtn");
  const agentQueueRail = document.getElementById("agentQueueRail");
  const agentWelcome = document.getElementById("agentWelcome");
  const agentStreamBeam = document.getElementById("agentStreamBeam");
  const agentNewChatBtn = document.getElementById("agentNewChatBtn");
  const agentDemoSdfBtn = document.getElementById("agentDemoSdfBtn");
  const agentHistoryBtn = document.getElementById("agentHistoryBtn");
  const agentSettingsBtn = document.getElementById("agentSettingsBtn");
  const isMacPlatform =
    /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ||
    (navigator.userAgentData && navigator.userAgentData.platform === "macOS");

  // Hoist busy flag above the shortcut-label helper so the immediate init call
  // below does not hit the let TDZ.
  let agentBusy = false;

  function applyAgentSendShortcutLabel() {
    if (!agentSendShortcut) return;
    // Mac: RUN ⌘ + ↵ · Windows/Linux: RUN Ctrl + Enter
    const label = isMacPlatform ? "RUN ⌘ + ↵" : "RUN Ctrl + Enter";
    agentSendShortcut.textContent = label;
    if (agentSendBtn) {
      agentSendBtn.setAttribute("aria-label", `发送（${label}）`);
      agentSendBtn.title = label;
    }
  }
  applyAgentSendShortcutLabel();
  const agentHistoryPanel = document.getElementById("agentHistoryPanel");
  const agentHistoryList = document.getElementById("agentHistoryList");
  const agentHistoryCount = document.getElementById("agentHistoryCount");
  const agentHistoryClearBtn = document.getElementById("agentHistoryClearBtn");
  const mmProfileBanner = document.getElementById("mmProfileBanner");
  const profileInfoModal = document.getElementById("profileInfoModal");
  const profileInfoVersion = document.getElementById("profileInfoVersion");
  const profileInfoBuild = document.getElementById("profileInfoBuild");
  const profileInfoClientId = document.getElementById("profileInfoClientId");
  const profileInfoClientEdit = document.getElementById("profileInfoClientEdit");
  const profileClassicModeBtn = document.getElementById("profileClassicModeBtn");
  const agentHistoryCloseBtn = document.getElementById("agentHistoryCloseBtn");
  const agentSettingsPanel = document.getElementById("agentSettingsPanel");
  const agentSettingsBody = document.getElementById("agentSettingsBody");
  const agentSettingsCloseBtn = document.getElementById("agentSettingsCloseBtn");
  const agentDrawerScrim = document.getElementById("agentDrawerScrim");
  const agentTurnRailEl = document.getElementById("agentTurnRail");
  let agentHistoryLoadSeq = 0;
  let agentSettingsLoadSeq = 0;

  const Render = window.MolMindAgentRender;
  const HistoryUI = window.MolMindAgentHistory;
  const SettingsUI = window.MolMindAgentSettings;
  const MentionUI = window.MolMindAgentMention;
  const RunStatus = window.MolMindAgentRunStatus;
  const TurnRail =
    window.MolMindAgentTurnRail &&
    new window.MolMindAgentTurnRail().mount({
      scrollEl: agentChatScroll,
      messagesEl: agentMessages,
      railEl: agentTurnRailEl,
    });

  if (RunStatus) {
    RunStatus.mount({
      chatMain: agentChatMain,
      bottomSend: document.querySelector("#agentChatMain .mm-bottom-send"),
    });
  }

  const LEGACY_AGENT_SESSION_KEY = "molmind:agent_active_session_v1";
  const AGENT_CLIENT_ID =
    window.MolMindClientIdentity && window.MolMindClientIdentity.clientId
      ? window.MolMindClientIdentity.clientId
      : "anonymous";
  const AGENT_SESSION_KEY = `${LEGACY_AGENT_SESSION_KEY}:${AGENT_CLIENT_ID}`;
  const AGENT_DRAFT_KEY = `molmind:agent_drafts:v1:${AGENT_CLIENT_ID}`;
  const AGENT_DRAFT_LIMIT = 50;
  let agentSessionId = null;
  let activeTurn = null;
  // Streaming may grow the transcript without taking control away from the
  // reader. This flag is intentionally driven by the user's scroll position.
  let agentShouldFollow = true;
  let agentProgrammaticScroll = false;
  let agentUploadInProgress = false;
  const ACTIVE_AGENT_RUN_STATUSES = new Set(["queued", "running", "cancel_requested"]);
  const agentRunStateBySession = new Map();
  /**
   * Per-session in-flight NDJSON streams.
   * Switching chats detaches the UI but keeps the HTTP stream alive so the server
   * can finish and persist; coming back reloads (and refreshes again on complete).
   * @type {Map<string, { id: number, controller: AbortController, running: boolean, onComplete: null | (() => void | Promise<void>) }>}
   */
  const agentStreams = new Map();
  let agentStreamSeq = 0;
  let agentQueueCount = 0;
  let agentPendingTurns = [];
  /** @type {{ key: string, kind: string, text: string, attachment_ids: string[], attachments?: object[] }[]} */
  let agentOptimisticTurns = [];
  /** @type {Map<string, { filename: string, kind: string }>} */
  const agentAttachmentMetaById = new Map();
  let agentDraftTimer = null;

  function rememberAttachmentMeta(meta) {
    if (!meta || typeof meta !== "object") return;
    const id = String(meta.attachment_id || "");
    if (!id) return;
    agentAttachmentMetaById.set(id, {
      filename: String(meta.filename || "attachment"),
      kind: String(meta.kind || attachmentKindLabel(meta.filename || "")).toLowerCase(),
    });
  }

  function resolveQueueAttachments(card) {
    if (Array.isArray(card.attachments) && card.attachments.length) {
      return card.attachments
        .map((item) => ({
          attachment_id: String((item && item.attachment_id) || ""),
          filename: String((item && item.filename) || ""),
          kind: String((item && item.kind) || ""),
        }))
        .filter((item) => item.filename || item.attachment_id);
    }
    return (card.attachment_ids || [])
      .map((id) => {
        const key = String(id || "");
        const meta = agentAttachmentMetaById.get(key);
        return {
          attachment_id: key,
          filename: (meta && meta.filename) || key,
          kind: (meta && meta.kind) || "",
        };
      })
      .filter((item) => item.attachment_id);
  }

  /** Chips for a live ask painted from an already-active / promoted Run. */
  function resolveRunAskAttachments(run) {
    if (!run || typeof run !== "object") return [];
    if (Array.isArray(run.attachment_summaries) && run.attachment_summaries.length) {
      return run.attachment_summaries
        .map((item) => ({
          attachment_id: String((item && item.attachment_id) || ""),
          filename: String((item && item.filename) || ""),
          kind: String((item && item.kind) || ""),
        }))
        .filter((item) => item.filename || item.attachment_id);
    }
    const ids = (run.input && run.input.attachment_ids) || [];
    return resolveQueueAttachments({ attachment_ids: ids });
  }

  function readAgentDrafts() {
    try {
      const parsed = JSON.parse(localStorage.getItem(AGENT_DRAFT_KEY) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  function writeAgentDrafts(drafts) {
    try {
      const entries = Object.entries(drafts || {})
        .sort((a, b) => Number((b[1] || {}).updatedAt || 0) - Number((a[1] || {}).updatedAt || 0))
        .slice(0, AGENT_DRAFT_LIMIT);
      localStorage.setItem(AGENT_DRAFT_KEY, JSON.stringify(Object.fromEntries(entries)));
    } catch {
      /* private browsing / quota: the textarea remains the in-memory fallback */
    }
  }

  function persistAgentDraft(sessionId = agentSessionId || "__new__") {
    if (!sessionId || !agentInput) return;
    const drafts = readAgentDrafts();
    const text = String(agentInput.value || "");
    if (!text) {
      delete drafts[sessionId];
    } else {
      drafts[sessionId] = {
        text: text.slice(0, 50_000),
        selectionStart: agentInput.selectionStart || 0,
        selectionEnd: agentInput.selectionEnd || 0,
        updatedAt: Date.now(),
      };
    }
    writeAgentDrafts(drafts);
  }

  function scheduleAgentDraftSave() {
    if (agentDraftTimer) clearTimeout(agentDraftTimer);
    agentDraftTimer = setTimeout(() => {
      agentDraftTimer = null;
      persistAgentDraft();
    }, 200);
  }

  function restoreAgentDraft(sessionId) {
    if (!agentInput) return;
    const draft = readAgentDrafts()[sessionId || "__new__"] || null;
    agentInput.value = draft && typeof draft.text === "string" ? draft.text : "";
    resizeAgentInput();
    if (draft) {
      const start = Math.min(Number(draft.selectionStart || 0), agentInput.value.length);
      const end = Math.min(Number(draft.selectionEnd || start), agentInput.value.length);
      requestAnimationFrame(() => agentInput.setSelectionRange(start, end));
    }
  }

  function migrateNewAgentDraft(sessionId) {
    if (!sessionId) return;
    persistAgentDraft("__new__");
    const drafts = readAgentDrafts();
    if (drafts.__new__ && !drafts[sessionId]) drafts[sessionId] = drafts.__new__;
    delete drafts.__new__;
    writeAgentDrafts(drafts);
  }

  function consumeAgentDraft(submittedText) {
    if (!agentInput) return;
    if (
      submittedText != null &&
      String(agentInput.value || "").trim() !== String(submittedText || "").trim()
    ) {
      return;
    }
    clearAgentInput();
  }

  function clearAgentInput() {
    if (!agentInput) return;
    if (agentDraftTimer) {
      clearTimeout(agentDraftTimer);
      agentDraftTimer = null;
    }
    agentInput.value = "";
    persistAgentDraft();
    resizeAgentInput();
  }

  function restoreAgentInputText(text) {
    if (!agentInput) return;
    if (String(agentInput.value || "").trim()) return;
    agentInput.value = String(text || "");
    persistAgentDraft();
    resizeAgentInput();
  }

  function apiErrorMessage(body, fallback) {
    const detail = body && body.detail;
    if (typeof detail === "string" && detail) return detail;
    if (detail && typeof detail === "object" && detail.message) return detail.message;
    if (body && typeof body.message === "string" && body.message) return body.message;
    return fallback;
  }

  function isAgentRunActive(run) {
    return !!run && ACTIVE_AGENT_RUN_STATUSES.has(String(run.status || ""));
  }

  function setAgentRunSnapshot(sessionId, run) {
    if (!sessionId) return;
    if (run) agentRunStateBySession.set(sessionId, run);
    else agentRunStateBySession.delete(sessionId);
  }

  function currentAgentRun(sessionId = agentSessionId) {
    return sessionId ? agentRunStateBySession.get(sessionId) || null : null;
  }

  function isCurrentAgentSessionBusy() {
    if (isAgentStopGateActive()) return true;
    const local = agentSessionId ? agentStreams.get(agentSessionId) : null;
    if (local && local.running) return true;
    if (isAgentRunActive(currentAgentRun())) return true;
    // Backend may already be terminal while the previous turn's typewriter is
    // still draining — keep treating that as busy so new prompts enqueue.
    if (
      activeTurn &&
      typeof activeTurn.isStreamPending === "function" &&
      activeTurn.isStreamPending()
    ) {
      return true;
    }
    return false;
  }

  function assertAgentSessionMutable(message) {
    if (!isCurrentAgentSessionBusy()) return true;
    showAgentToast(message || "当前回复完成后再操作");
    return false;
  }

  function readCachedAgentSessionId() {
    try {
      const scoped = localStorage.getItem(AGENT_SESSION_KEY);
      if (scoped) return scoped;
      const legacy = localStorage.getItem(LEGACY_AGENT_SESSION_KEY);
      if (legacy) {
        localStorage.setItem(AGENT_SESSION_KEY, legacy);
        localStorage.removeItem(LEGACY_AGENT_SESSION_KEY);
      }
      return legacy || null;
    } catch {
      return null;
    }
  }

  function writeCachedAgentSessionId(sid) {
    try {
      if (sid) localStorage.setItem(AGENT_SESSION_KEY, sid);
      else localStorage.removeItem(AGENT_SESSION_KEY);
    } catch {
      /* ignore quota / private mode */
    }
  }

  /** Keep in-memory session id and localStorage cache in sync. */
  function setAgentSessionId(sid) {
    const previous = agentSessionId;
    if (previous && previous !== sid) persistAgentDraft(previous);
    agentSessionId = sid || null;
    writeCachedAgentSessionId(agentSessionId);
    if (previous !== agentSessionId) {
      agentOptimisticTurns = [];
      recomputeAgentQueueCount();
      restoreAgentDraft(agentSessionId);
    }
  }

  function syncAgentBusyUi() {
    const cur = agentSessionId ? agentStreams.get(agentSessionId) : null;
    const stopping = isAgentStopGateActive();
    const busy =
      stopping ||
      !!(cur && cur.running) ||
      isAgentRunActive(currentAgentRun()) ||
      !!(
        activeTurn &&
        typeof activeTurn.isStreamPending === "function" &&
        activeTurn.isStreamPending()
      );
    agentBusy = busy;
    setStreaming(busy && !stopping);
    if (agentChatRoot) {
      agentChatRoot.classList.toggle("mm-chat-root--stopping", stopping);
      agentChatRoot.setAttribute("aria-busy", busy ? "true" : "false");
    }
    if (agentInput) {
      if (!agentInput.dataset.defaultPlaceholder) {
        agentInput.dataset.defaultPlaceholder =
          agentInput.getAttribute("placeholder") || "";
      }
      agentInput.disabled = stopping;
      agentInput.setAttribute(
        "placeholder",
        stopping
          ? "正在停止，请稍候…"
          : agentInput.dataset.defaultPlaceholder || ""
      );
    }
    if (agentSendBtn) {
      // During stop: hard-ban send. Otherwise allow queueing unless full.
      agentSendBtn.disabled = stopping || (busy && agentQueueCount >= 3);
      agentSendBtn.type = "submit";
      applyAgentSendShortcutLabel();
    }
    if (agentSendShortcut) {
      agentSendShortcut.classList.remove("hidden");
    }
    if (agentStopBtn) {
      const showStop = busy || stopping;
      agentStopBtn.classList.toggle("hidden", !showStop);
      agentStopBtn.hidden = !showStop;
      agentStopBtn.disabled = stopping;
      agentStopBtn.title = stopping ? "正在停止…" : "停止当前任务";
      agentStopBtn.setAttribute("aria-label", stopping ? "正在停止" : "停止");
      agentStopBtn.classList.toggle("mm-stop-btn--stopping", stopping);
    }
    if (agentGuideBtn) {
      agentGuideBtn.classList.add("hidden");
      agentGuideBtn.hidden = true;
    }
    if (agentFileInput) agentFileInput.disabled = stopping || agentUploadInProgress;
    if (agentUploadBtn) {
      agentUploadBtn.setAttribute(
        "aria-disabled",
        String(stopping || agentUploadInProgress)
      );
      agentUploadBtn.title = stopping
        ? "正在停止"
        : agentUploadInProgress
          ? "附件上传中"
          : "上传附件";
    }
    if (agentNewChatBtn) agentNewChatBtn.disabled = stopping;
    if (agentHistoryClearBtn) agentHistoryClearBtn.disabled = busy || stopping;
    if (agentAttachRail) {
      agentAttachRail.querySelectorAll(".mm-attach-chip-remove").forEach((button) => {
        button.disabled = stopping;
        button.title = stopping ? "正在停止" : "移除附件";
      });
    }
    if (RunStatus) {
      if (busy || stopping) RunStatus.setVisible(true);
      else RunStatus.setVisible(false);
    }
    paintAgentQueueRail();
  }

  function shortQueueText(s, max = 36) {
    const t = String(s || "").replace(/\s+/g, " ").trim();
    if (!t) return "";
    if (t.length <= max) return t;
    return t.slice(0, Math.max(0, max - 1)) + "…";
  }

  function recomputeAgentQueueCount() {
    const serverNormal = agentPendingTurns.filter((item) => item.kind !== "guidance").length;
    const optimisticNormal = agentOptimisticTurns.filter((item) => item.kind !== "guidance").length;
    agentQueueCount = serverNormal + optimisticNormal;
  }

  function pushOptimisticQueueTurn({
    kind = "queue",
    text = "",
    attachment_ids = [],
    attachments = [],
  } = {}) {
    const key = `opt-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    agentOptimisticTurns.push({
      key,
      kind: kind === "guidance" ? "guidance" : "queue",
      text: String(text || ""),
      attachment_ids: Array.isArray(attachment_ids) ? attachment_ids.slice() : [],
      attachments: Array.isArray(attachments) ? attachments.slice() : [],
    });
    recomputeAgentQueueCount();
    syncAgentBusyUi();
    return key;
  }

  function removeOptimisticQueueTurn(key, { paint = true } = {}) {
    if (!key) return;
    const before = agentOptimisticTurns.length;
    agentOptimisticTurns = agentOptimisticTurns.filter((item) => item.key !== key);
    if (agentOptimisticTurns.length === before) return;
    recomputeAgentQueueCount();
    if (paint) syncAgentBusyUi();
  }

  function renderAgentQueue(turns, limit = 3) {
    agentPendingTurns = Array.isArray(turns) ? turns.slice() : [];
    recomputeAgentQueueCount();
    void limit;
    syncAgentBusyUi();
  }

  function paintAgentQueueRail() {
    if (!agentQueueRail) return;
    // Only show server-queued prompts (max 3 normal + optional guidance), never live input draft.
    // Optimistic placeholders cover the network gap after send clears the input.
    const turns = agentPendingTurns.slice();
    const guidance = turns.filter((item) => item.kind === "guidance");
    const normal = turns
      .filter((item) => item.kind !== "guidance")
      .slice(0, 3);
    const cards = [];
    guidance.forEach((turn, index) => {
      cards.push({
        key: String(turn.turn_id || `guide-${index}`),
        kind: "guidance",
        text: String(turn.text || ""),
        turn_id: String(turn.turn_id || ""),
        attachment_ids: turn.attachment_ids || [],
        attachments: turn.attachments || [],
      });
    });
    normal.forEach((turn, index) => {
      cards.push({
        key: String(turn.turn_id || `turn-${index}`),
        kind: "queue",
        text: String(turn.text || ""),
        turn_id: String(turn.turn_id || ""),
        attachment_ids: turn.attachment_ids || [],
        attachments: turn.attachments || [],
      });
    });
    agentOptimisticTurns.forEach((turn) => {
      cards.push({
        key: turn.key,
        kind: turn.kind === "guidance" ? "guidance" : "queue",
        text: String(turn.text || ""),
        turn_id: "",
        attachment_ids: turn.attachment_ids || [],
        attachments: turn.attachments || [],
        optimistic: true,
      });
    });

    agentQueueRail.innerHTML = "";
    if (!cards.length) {
      agentQueueRail.classList.add("hidden");
      agentQueueRail.classList.remove("is-visible");
      agentQueueRail.setAttribute("aria-hidden", "true");
      return;
    }

    agentQueueRail.classList.remove("hidden");
    agentQueueRail.classList.add("is-visible");
    agentQueueRail.setAttribute("aria-hidden", "false");
    cards.forEach((card) => {
      const row = document.createElement("div");
      if (card.optimistic) {
        row.className =
          "mm-run-inv-item mm-run-inv-item--pending mm-queue-card mm-queue-card--loading";
        row.dataset.kind = card.kind;
        row.dataset.optimistic = "1";
        row.setAttribute("aria-busy", "true");
        row.setAttribute("aria-label", card.kind === "guidance" ? "指引提交中" : "排队提交中");

        const iconWrap = document.createElement("span");
        iconWrap.className = "mm-run-inv-icon-wrap mm-queue-skel-icon";
        iconWrap.setAttribute("aria-hidden", "true");

        const meta = document.createElement("span");
        meta.className = "mm-run-inv-meta mm-queue-skel-meta";
        meta.innerHTML =
          '<span class="mm-queue-skel-line mm-queue-skel-line--sm" aria-hidden="true"></span>' +
          '<span class="mm-queue-skel-line mm-queue-skel-line--lg" aria-hidden="true"></span>';

        row.append(iconWrap, meta);
        agentQueueRail.appendChild(row);
        return;
      }

      const statusClass = card.kind === "guidance" ? "active" : "pending";
      row.className = `mm-run-inv-item mm-run-inv-item--${statusClass} mm-queue-card`;
      row.dataset.kind = card.kind;
      if (card.turn_id) row.dataset.turnId = card.turn_id;

      const main = document.createElement("div");
      main.className = "mm-queue-card-main";

      const iconWrap = document.createElement("span");
      iconWrap.className = "mm-run-inv-icon-wrap";
      const iconName = card.kind === "guidance" ? "sparkles" : "list-plain";
      iconWrap.innerHTML = `<span class="mm-icon mm-icon--${iconName} mm-icon--md" aria-hidden="true"></span>`;

      const meta = document.createElement("span");
      meta.className = "mm-run-inv-meta";
      const kind = document.createElement("span");
      kind.className = "mm-run-inv-kind";
      kind.textContent = card.kind === "guidance" ? "指引中" : "排队";
      const name = document.createElement("span");
      name.className = "mm-run-inv-name";
      const full = String(card.text || "");
      const label = shortQueueText(full, 32);
      name.textContent = label;
      if (full) {
        // Always expose the full prompt on hover — CSS ellipsis may clip even
        // when shortQueueText did not truncate.
        name.title = full;
        meta.title = full;
        row.title = full;
      }
      meta.append(kind, name);

      const side = document.createElement("span");
      side.className = "mm-run-inv-side";
      if (card.kind === "guidance") {
        const badge = document.createElement("span");
        badge.className = "mm-run-inv-badge mm-run-inv-badge--done";
        badge.textContent = "处理中";
        side.appendChild(badge);
      } else {
        const actions = document.createElement("span");
        actions.className = "mm-queue-actions";

        const setActionsDisabled = (disabled) => {
          sendBtn.disabled = disabled;
          editBtn.disabled = disabled;
          deleteBtn.disabled = disabled;
        };

        const sendBtn = document.createElement("button");
        sendBtn.type = "button";
        sendBtn.className = "mm-queue-icon-btn";
        sendBtn.setAttribute("aria-label", "发送指引");
        sendBtn.title = "打断当前任务，并按这条提示词重新规划";
        sendBtn.innerHTML = '<span class="mm-icon mm-icon--send mm-icon--sm" aria-hidden="true"></span>';
        sendBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          if (isAgentStopGateActive()) {
            showAgentToast("正在停止，请稍候");
            return;
          }
          setActionsDisabled(true);
          try {
            await promotePromptAsGuidance(card);
          } catch (error) {
            setActionsDisabled(false);
            showAgentToast(error.message || "发送失败");
          }
        });

        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "mm-queue-icon-btn";
        editBtn.setAttribute("aria-label", "修改排队");
        editBtn.title = "取回输入框修改（会移出排队）";
        editBtn.innerHTML = '<span class="mm-icon mm-icon--pencil mm-icon--sm" aria-hidden="true"></span>';
        editBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          setActionsDisabled(true);
          try {
            await editQueuedPrompt(card);
          } catch (error) {
            setActionsDisabled(false);
            showAgentToast(error.message || "取回失败");
          }
        });

        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "mm-queue-icon-btn mm-queue-icon-btn--danger";
        deleteBtn.setAttribute("aria-label", "删除排队");
        deleteBtn.title = "从排队中删除这条提示词";
        deleteBtn.innerHTML = '<span class="mm-icon mm-icon--trash mm-icon--sm" aria-hidden="true"></span>';
        deleteBtn.addEventListener("click", async (e) => {
          e.preventDefault();
          e.stopPropagation();
          setActionsDisabled(true);
          try {
            await removeQueuedPrompt(card);
          } catch (error) {
            setActionsDisabled(false);
            showAgentToast(error.message || "删除失败");
          }
        });

        actions.append(sendBtn, editBtn, deleteBtn);
        if (isAgentStopGateActive()) {
          setActionsDisabled(true);
        }
        side.appendChild(actions);
      }

      main.append(iconWrap, meta, side);
      row.appendChild(main);

      const attachments = resolveQueueAttachments(card);
      if (attachments.length) {
        const attachRow = document.createElement("div");
        attachRow.className = "mm-queue-attach-row";
        attachments.forEach((att) => {
          const chip = document.createElement("span");
          chip.className = "mm-queue-attach-chip";
          const filename = String(att.filename || att.attachment_id || "附件");
          chip.title = filename;
          const kindLabel = attachmentKindLabel(filename);
          chip.innerHTML =
            '<span class="mm-icon mm-icon--file-txt mm-icon--sm" aria-hidden="true"></span>' +
            `<span class="mm-queue-attach-kind">${kindLabel}</span>` +
            `<span class="mm-queue-attach-name"></span>`;
          chip.querySelector(".mm-queue-attach-name").textContent = filename;
          attachRow.appendChild(chip);
        });
        row.appendChild(attachRow);
      }

      agentQueueRail.appendChild(row);
    });
  }

  async function promotePromptAsGuidance(card) {
    const text = String((card && card.text) || "").trim();
    if (!text) {
      showAgentToast("请先输入补充指引");
      return;
    }
    const turnId = String((card && card.turn_id) || "");
    const sid = agentSessionId || (await ensureAgentSession());
    const expectedInterrupt = isCurrentAgentSessionBusy();

    // Take the item out of the visible queue immediately — do not paint a new
    // optimistic "排队" loading card (that reads as a normal enqueue flash).
    if (card && card.kind === "queue" && turnId) {
      agentPendingTurns = agentPendingTurns.filter(
        (item) => String(item.turn_id || "") !== turnId
      );
      recomputeAgentQueueCount();
      syncAgentBusyUi();
      const resp = await fetch(
        `/api/agent/sessions/${sid}/turns/${encodeURIComponent(turnId)}`,
        { method: "DELETE" }
      );
      if (!resp.ok && resp.status !== 409 && resp.status !== 404) {
        await refreshAgentQueue(sid);
        throw new Error(
          apiErrorMessage(await resp.json().catch(() => ({})), "无法提升为指引")
        );
      }
    }

    const accepted = await submitBusyAgentTurn(text, "guidance", {
      optimistic: false,
    });
    const disposition = String((accepted && accepted.disposition) || "");
    const active =
      (await refreshAgentQueue(sid))?.active_run ||
      currentAgentRun(sid) ||
      (disposition === "started" ? accepted : null);

    if (disposition === "started" || (active && isAgentRunActive(active))) {
      // Server had no active Run (common when only the typewriter is still
      // draining). Treat as a direct send and paint the live ask/answer box.
      if (active && isAgentRunActive(active)) {
        setAgentRunSnapshot(sid, active);
        if (!agentStreams.get(sid)?.running) {
          await followActiveRunWithLiveTurn(sid, active);
        }
      }
      showAgentToast(
        expectedInterrupt
          ? "当前步骤已结束，已直接发送该提示词"
          : "已发送该提示词"
      );
      return;
    }

    showAgentToast("指引已收到，正在停止当前步骤并重新规划");
  }

  async function removeQueuedPrompt(card) {
    const turnId = String((card && card.turn_id) || "");
    if (!turnId || !agentSessionId) {
      showAgentToast("无法删除该排队项");
      return;
    }
    const resp = await fetch(
      `/api/agent/sessions/${agentSessionId}/turns/${encodeURIComponent(turnId)}`,
      { method: "DELETE" }
    );
    if (!resp.ok) {
      throw new Error(
        apiErrorMessage(await resp.json().catch(() => ({})), "删除失败")
      );
    }
    await refreshAgentQueue(agentSessionId);
  }

  async function editQueuedPrompt(card) {
    const text = String((card && card.text) || "");
    const turnId = String((card && card.turn_id) || "");
    if (!turnId || !agentSessionId) {
      showAgentToast("无法取回该排队项");
      return;
    }
    const resp = await fetch(
      `/api/agent/sessions/${agentSessionId}/turns/${encodeURIComponent(turnId)}`,
      { method: "DELETE" }
    );
    if (!resp.ok) {
      throw new Error(
        apiErrorMessage(await resp.json().catch(() => ({})), "取回失败")
      );
    }
    if (agentInput) {
      if (agentDraftTimer) {
        clearTimeout(agentDraftTimer);
        agentDraftTimer = null;
      }
      agentInput.value = text;
      persistAgentDraft();
      resizeAgentInput();
      agentInput.focus();
      const cursor = agentInput.value.length;
      try {
        agentInput.setSelectionRange(cursor, cursor);
      } catch {
        /* ignore */
      }
    }
    await refreshAgentQueue(agentSessionId);
  }

  async function refreshAgentQueue(sessionId = agentSessionId) {
    if (!sessionId) {
      renderAgentQueue([]);
      return null;
    }
    try {
      const resp = await fetch(`/api/agent/sessions/${sessionId}/turns`, { cache: "no-store" });
      if (!resp.ok) return null;
      const data = await resp.json();
      if (agentSessionId === sessionId) {
        setAgentRunSnapshot(sessionId, data.active_run || null);
        (data.turns || []).forEach((turn) => {
          (turn.attachments || []).forEach((att) => rememberAttachmentMeta(att));
        });
        const active = data.active_run;
        if (active) {
          (active.attachment_summaries || []).forEach((att) => rememberAttachmentMeta(att));
        }
        renderAgentQueue(data.turns || [], data.queue_limit || 3);
      }
      return data;
    } catch {
      return null;
    }
  }

  document.addEventListener("molmind:retry-agent-run", async (event) => {
    const runId = String((event.detail && event.detail.runId) || "");
    const button = event.detail && event.detail.button;
    const turn = event.detail && event.detail.turn;
    const sessionId = agentSessionId;
    if (!runId || !sessionId) return;
    // Belt-and-suspenders: clear answer body even if the click handler already did.
    if (turn && typeof turn.resetAnswer === "function") {
      turn.resetAnswer();
    } else if (button && button.closest) {
      const body = button.closest(".mm-turn")?.querySelector(".mm-turn-answer-body");
      if (body) body.innerHTML = "";
    }
    try {
      const response = await fetch(
        `/api/agent/sessions/${sessionId}/runs/${encodeURIComponent(runId)}/retry`,
        { method: "POST" }
      );
      if (!response.ok) {
        throw new Error(
          apiErrorMessage(await response.json().catch(() => ({})), "重试失败")
        );
      }
      const retry = await response.json();
      showAgentToast("已创建检查点重试 Run");
      setAgentRunSnapshot(sessionId, retry);
      if (turn && turn.root && turn.root.isConnected) {
        await followActiveRunWithLiveTurn(sessionId, retry, { existingTurn: turn });
      } else {
        await loadAgentSession(sessionId);
      }
    } catch (error) {
      if (button) button.disabled = false;
      showAgentToast(error.message || "重试失败");
    }
  });

  function isStreamEntryActive(sid, entry) {
    return !!entry && agentStreams.get(sid) === entry && entry.running;
  }

  function canPaintStream(sid, entry, turn, ev = null) {
    const attached =
      isStreamEntryActive(sid, entry) &&
      agentSessionId === sid &&
      turn &&
      turn.root &&
      turn.root.isConnected;
    if (!attached) return false;
    const frozen =
      !!(entry && entry.paintFrozen) || isAgentStopGateActive(sid);
    if (!frozen) return true;
    // During stop freeze, only terminal / interrupt chrome may still paint.
    const type = ev && ev.type;
    return (
      type === "done" ||
      type === "error" ||
      type === "run_interrupted"
    );
  }

  function noteAgentStreamTerminal(sessionId, ev) {
    const status = String((ev && ev.status) || "").trim();
    const type = String((ev && ev.type) || "");
    const wasStopping = isAgentStopGateActive(sessionId);
    const terminalStop =
      type === "run_interrupted" ||
      status === "interrupted" ||
      status === "cancelled" ||
      status === "failed";
    if (terminalStop || type === "done" || type === "error") {
      clearAgentStopGate(sessionId);
      if (wasStopping) {
        showAgentToast(
          terminalStop || status === "interrupted" ? "已停止当前任务" : "任务已结束"
        );
      }
      syncAgentBusyUi();
    }
  }

  /** Stop painting into the current DOM without cancelling background generation. */
  function detachAgentUi() {
    dismissInstallRequestFloat();
    finishTurn();
    syncAgentBusyUi();
  }

  function abortSessionStream(sid) {
    const entry = agentStreams.get(sid);
    if (!entry) return;
    entry.running = false;
    entry.onComplete = null;
    try {
      entry.controller.abort();
    } catch {
      /* ignore */
    }
    agentStreams.delete(sid);
  }

  function startSessionStream(sid) {
    abortSessionStream(sid);
    const entry = {
      id: ++agentStreamSeq,
      controller: new AbortController(),
      running: true,
      onComplete: null,
    };
    agentStreams.set(sid, entry);
    return entry;
  }

  function setModeTabStyles() {
    // Agent 全屏：经典入口在顶栏玻璃按钮；经典页：Agent 入口在 nav
    if (modeAgentBtn) {
      const agentOn = workMode === "agent";
      modeAgentBtn.setAttribute("aria-selected", agentOn ? "true" : "false");
      modeAgentBtn.className = agentOn
        ? "px-3 py-1.5 rounded-full text-label-md text-white bg-gradient-to-br from-[#005aff] to-[#50d1ff] shadow-sm"
        : "px-3 py-1.5 rounded-full text-label-md text-on-surface-variant hover:text-primary transition-colors";
    }
  }

  function setWorkMode(mode) {
    workMode = mode === "classic" ? "classic" : "agent";
    setModeTabStyles();
    if (HistoryUI) HistoryUI.closeAll(agentChatRoot);
    showUploadOnly();
    if (workMode === "agent" && window.MolMindAgentTour) {
      window.MolMindAgentTour.maybeStart();
    }
  }

  function agentIsNearBottom() {
    if (!agentChatScroll) return true;
    return agentChatScroll.scrollTop + agentChatScroll.clientHeight >= agentChatScroll.scrollHeight - 36;
  }

  if (agentChatScroll) {
    const releaseAgentProgrammaticScroll = () => { agentProgrammaticScroll = false; };
    agentChatScroll.addEventListener("wheel", releaseAgentProgrammaticScroll, { passive: true });
    agentChatScroll.addEventListener("touchstart", releaseAgentProgrammaticScroll, { passive: true });
    agentChatScroll.addEventListener("scroll", () => {
      if (!agentProgrammaticScroll) agentShouldFollow = agentIsNearBottom();
    }, { passive: true });
  }

  function agentScrollBottom({ force = false } = {}) {
    if (force) agentShouldFollow = true;
    if (agentChatScroll && agentShouldFollow) {
      agentProgrammaticScroll = true;
      agentChatScroll.scrollTop = agentChatScroll.scrollHeight;
      requestAnimationFrame(() => { agentProgrammaticScroll = false; });
    }
    if (TurnRail) TurnRail.syncActive();
  }

  function setAgentEmpty(isEmpty) {
    if (!agentChatScroll) return;
    agentChatScroll.classList.toggle("mm-messages-area--empty", !!isEmpty);
    if (agentWelcome) agentWelcome.classList.toggle("hidden", !isEmpty);
    syncNewChatBtnTitle();
  }

  function isAgentNewConversation() {
    if (!agentMessages) return true;
    return agentMessages.querySelectorAll(".mm-turn").length === 0;
  }

  function syncNewChatBtnTitle() {
    if (!agentNewChatBtn) return;
    const empty = isAgentNewConversation();
    const tip = empty ? "已经在新对话中" : "新对话";
    agentNewChatBtn.title = tip;
    agentNewChatBtn.setAttribute("aria-label", tip);
  }

  let agentToastTimer = null;
  function showAgentToast(msg) {
    const el = document.getElementById("agentToast");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.add("is-visible");
    el.setAttribute("aria-hidden", "false");
    if (agentToastTimer) clearTimeout(agentToastTimer);
    agentToastTimer = setTimeout(() => {
      el.classList.remove("is-visible");
      el.setAttribute("aria-hidden", "true");
      agentToastTimer = null;
    }, 2000);
  }

  window.addEventListener("molmind:agent-toast", (event) => {
    showAgentToast(event.detail && event.detail.message);
  });

  let installFloatEl = null;
  let installFloatBusy = false;
  /** Session id waiting for install/UI settle before auto-promoting queued turns. */
  let agentQueueFollowDeferred = null;
  /** True while continueAgentQueueFollow is actively trying to promote. */
  let agentQueueFollowActive = false;
  /** Hold queue while we auto-send「继续」to resume pending_install after install. */
  let agentInstallResumeHold = false;
  /** Highest-priority stop gate: freeze UI paint and ban user ops until terminal. */
  let agentStopGate = null;
  /** After user stop, do not auto-drain the queue (avoid surprise follow-up runs). */
  let agentSuppressQueueFollow = false;

  function isAgentStopGateActive(sessionId = agentSessionId) {
    return !!(
      agentStopGate &&
      agentStopGate.active &&
      (!sessionId || agentStopGate.sessionId === sessionId)
    );
  }

  function beginAgentStopGate(sessionId, runId) {
    agentStopGate = {
      active: true,
      sessionId: sessionId || agentSessionId,
      runId: String(runId || ""),
      startedAt: Date.now(),
    };
    const stream = sessionId ? agentStreams.get(sessionId) : null;
    if (stream) stream.paintFrozen = true;
    if (activeTurn && typeof activeTurn.haltForStop === "function") {
      activeTurn.haltForStop("已请求停止，正在中断当前任务…");
    }
    if (RunStatus && typeof RunStatus.applyEvent === "function") {
      RunStatus.applyEvent({
        type: "thinking",
        text: "正在停止当前任务…",
      });
    }
    // Safety valve: never leave the UI locked forever if the terminal event is lost.
    if (agentStopGate.timer) clearTimeout(agentStopGate.timer);
    agentStopGate.timer = setTimeout(() => {
      if (!isAgentStopGateActive(sessionId)) return;
      clearAgentStopGate(sessionId);
      showAgentToast("停止已超时解除；若任务仍在跑可再次点击停止");
      syncAgentBusyUi();
    }, 45000);
    syncAgentBusyUi();
  }

  function clearAgentStopGate(sessionId = null) {
    if (
      sessionId &&
      agentStopGate &&
      agentStopGate.sessionId &&
      agentStopGate.sessionId !== sessionId
    ) {
      return;
    }
    if (agentStopGate && agentStopGate.timer) {
      clearTimeout(agentStopGate.timer);
    }
    if (agentStopGate) agentStopGate.active = false;
    agentStopGate = null;
    const sid = sessionId || agentSessionId;
    const stream = sid ? agentStreams.get(sid) : null;
    if (stream) stream.paintFrozen = false;
  }

  function isInstallRequestOpen() {
    return !!(installFloatEl && installFloatEl.isConnected);
  }

  function isAgentStreamUiPending() {
    return !!(
      activeTurn &&
      typeof activeTurn.isStreamPending === "function" &&
      activeTurn.isStreamPending()
    );
  }

  function hasAgentQueueWaiting() {
    return agentPendingTurns.length > 0 || agentOptimisticTurns.length > 0;
  }

  /**
   * Direct idle send is only safe when nothing else owns the turn lifecycle.
   * Otherwise always enqueue via mode=queue and let /turns/next promote in order.
   */
  function shouldEnqueueAgentSend() {
    return (
      isAgentStopGateActive() ||
      isCurrentAgentSessionBusy() ||
      hasAgentQueueWaiting() ||
      isInstallRequestOpen() ||
      agentQueueFollowActive ||
      !!agentQueueFollowDeferred ||
      agentInstallResumeHold
    );
  }

  function holdAgentQueueForInstallResume(sessionId) {
    const sid = sessionId || agentSessionId;
    if (!sid) return;
    agentQueueFollowDeferred = sid;
    agentInstallResumeHold = true;
  }

  function releaseAgentInstallResumeHold() {
    agentInstallResumeHold = false;
  }

  function dismissInstallRequestFloat({ resumeQueue = true } = {}) {
    if (installFloatEl && installFloatEl.parentNode) {
      installFloatEl.classList.remove("mm-install-float--open");
      const node = installFloatEl;
      installFloatEl = null;
      setTimeout(() => node.remove(), 220);
    }
    installFloatBusy = false;
    if (resumeQueue) {
      resumeAgentQueueFollowAfterGate();
    }
  }

  function resumeAgentQueueFollowAfterGate() {
    const sid = agentQueueFollowDeferred || agentSessionId;
    if (!sid || isInstallRequestOpen()) return;
    agentQueueFollowDeferred = null;
    continueAgentQueueFollow(sid).catch(() => {});
  }

  function installRequestSkills(detail) {
    if (Array.isArray(detail && detail.skills) && detail.skills.length) {
      return detail.skills
        .map((item) => ({
          skill_id: String((item && item.skill_id) || "").trim(),
          title: String((item && (item.title || item.skill_id)) || "").trim(),
          description: String((item && item.description) || "").trim(),
        }))
        .filter((item) => item.skill_id);
    }
    const skillId = String((detail && detail.skill_id) || "").trim();
    if (!skillId) return [];
    return [
      {
        skill_id: skillId,
        title: String((detail && (detail.title || detail.label)) || skillId).trim(),
        description: String((detail && detail.description) || "").trim(),
      },
    ];
  }

  function showInstallRequestFloat(detail) {
    const skills = installRequestSkills(detail);
    if (!skills.length) return;
    const host = document.getElementById("agentChatMain") || document.body;
    // Replace any prior float without releasing the queue hold.
    dismissInstallRequestFloat({ resumeQueue: false });
    if (agentSessionId && hasAgentQueueWaiting()) {
      agentQueueFollowDeferred = agentSessionId;
    }

    const titles = skills.map((s) => s.title || s.skill_id).join("、");
    const mask = document.createElement("div");
    mask.className = "mm-install-float";
    mask.setAttribute("role", "dialog");
    mask.setAttribute("aria-modal", "true");
    mask.setAttribute("aria-label", "安装请求");
    mask.innerHTML = `
      <div class="mm-install-float-card">
        <div class="mm-install-float-head">
          <span class="mm-icon mm-icon--puzzle mm-icon--md" aria-hidden="true"></span>
          <div class="mm-install-float-titles">
            <div class="mm-install-float-kicker">安装请求</div>
            <div class="mm-install-float-title"></div>
          </div>
        </div>
        <p class="mm-install-float-summary"></p>
        <ul class="mm-install-float-list"></ul>
        <p class="mm-install-float-error" aria-live="polite" hidden></p>
        <div class="mm-install-float-actions">
          <button type="button" class="mm-install-float-cancel">暂不安装</button>
          <button type="button" class="mm-install-float-ok">确认安装</button>
        </div>
      </div>
    `;
    mask.querySelector(".mm-install-float-title").textContent = titles;
    mask.querySelector(".mm-install-float-summary").textContent =
      (detail && detail.summary) ||
      `确认后将安装「${titles}」，即可在当前对话继续使用。`;
    const list = mask.querySelector(".mm-install-float-list");
    skills.forEach((skill) => {
      const li = document.createElement("li");
      const name = document.createElement("strong");
      name.textContent = skill.title || skill.skill_id;
      li.appendChild(name);
      if (skill.description) {
        const desc = document.createElement("span");
        desc.textContent = skill.description;
        li.appendChild(desc);
      } else {
        const id = document.createElement("span");
        id.textContent = skill.skill_id;
        li.appendChild(id);
      }
      list.appendChild(li);
    });

    const cancelBtn = mask.querySelector(".mm-install-float-cancel");
    const okBtn = mask.querySelector(".mm-install-float-ok");
    const errorEl = mask.querySelector(".mm-install-float-error");
    const close = () => dismissInstallRequestFloat();
    cancelBtn.addEventListener("click", close);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) close();
    });

    okBtn.addEventListener("click", async () => {
      if (installFloatBusy) return;
      installFloatBusy = true;
      okBtn.disabled = true;
      cancelBtn.disabled = true;
      okBtn.textContent = "安装中…";
      errorEl.hidden = true;
      errorEl.textContent = "";
      try {
        for (let i = 0; i < 50 && isCurrentAgentSessionBusy(); i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 40));
        }
        const sessionId = await ensureAgentSession();
        if (!SettingsUI || typeof SettingsUI.scpInstall !== "function") {
          throw new Error("安装能力不可用");
        }
        for (const skill of skills) {
          await SettingsUI.scpInstall(sessionId, skill.skill_id);
        }
        try {
          await refreshAgentSettings();
        } catch {
          /* settings drawer may be closed */
        }
        if (MentionUI && sessionId) {
          MentionUI.refresh(sessionId).catch(() => {});
        }
        // Close the float without draining the queue. Queued test prompts must
        // not race ahead of pending_install resume (that wiped the scientific
        // retry and jumped to the next unrelated ask).
        dismissInstallRequestFloat({ resumeQueue: false });
        holdAgentQueueForInstallResume(sessionId);
        showAgentToast(`已安装「${titles}」，正在按原请求继续…`);
        try {
          // Backend pending_install treats「继续」as the affirm to restore retry_text.
          await sendAgentMessage("继续", { forceDirect: true });
        } catch (continueError) {
          showAgentToast(
            ((continueError && continueError.message) || "自动继续失败") +
              "；请手动回复「继续」以执行原请求"
          );
        } finally {
          releaseAgentInstallResumeHold();
          resumeAgentQueueFollowAfterGate();
        }
        if (agentInput) agentInput.focus();
      } catch (error) {
        installFloatBusy = false;
        okBtn.disabled = false;
        cancelBtn.disabled = false;
        okBtn.textContent = "确认安装";
        errorEl.hidden = false;
        errorEl.textContent = (error && error.message) || "安装失败，请重试";
      }
    });

    host.appendChild(mask);
    installFloatEl = mask;
    requestAnimationFrame(() => mask.classList.add("mm-install-float--open"));
  }

  document.addEventListener("molmind:install-request", (event) => {
    showInstallRequestFloat(event.detail || {});
  });

  function renderAgentDrawerLoading(rootEl, kind) {
    if (!rootEl) return;
    const catalog = kind === "catalog";
    rootEl.setAttribute("aria-busy", "true");
    rootEl.innerHTML = catalog
      ? `
        <div class="mm-drawer-loading mm-drawer-loading--catalog" role="status" aria-label="正在加载工具与插件">
          <div class="mm-drawer-loading-label">正在加载工具与插件…</div>
          <div class="mm-drawer-loading-toolbar" aria-hidden="true">
            <span class="mm-drawer-loading-pill"></span>
            <span class="mm-drawer-loading-pill"></span>
            <span class="mm-drawer-loading-pill"></span>
          </div>
          <div class="mm-drawer-loading-grid" aria-hidden="true">
            ${'<span class="mm-drawer-loading-card"></span>'.repeat(6)}
          </div>
        </div>`
      : `
        <div class="mm-drawer-loading mm-drawer-loading--history" role="status" aria-label="正在加载对话历史">
          <div class="mm-drawer-loading-label">正在加载对话历史…</div>
          <div aria-hidden="true">
            ${'<div class="mm-drawer-loading-row"></div>'.repeat(6)}
          </div>
        </div>`;
  }

  function renderAgentDrawerError(rootEl, message) {
    if (!rootEl) return;
    rootEl.setAttribute("aria-busy", "false");
    rootEl.innerHTML = "";
    const error = document.createElement("p");
    error.className = "mm-history-empty";
    error.textContent = message || "加载失败，请稍后重试";
    rootEl.appendChild(error);
  }

  function setStreaming(on) {
    if (agentStreamBeam) {
      agentStreamBeam.classList.toggle("hidden", !on);
      agentStreamBeam.setAttribute("aria-hidden", on ? "false" : "true");
    }
  }

  function finishTurn() {
    if (activeTurn) {
      if (typeof activeTurn.abortStream === "function") activeTurn.abortStream();
      activeTurn.finalize();
      activeTurn = null;
    }
  }

  /** Let the browser paint between token deltas so batched NDJSON still looks live. */
  function yieldAgentPaintFrame() {
    return new Promise((resolve) => {
      if (typeof requestAnimationFrame === "function") {
        requestAnimationFrame(() => resolve());
      } else {
        setTimeout(resolve, 16);
      }
    });
  }

  async function finishTurnAfterStream(turn) {
    if (!turn) return;
    if (typeof turn.waitForStream === "function") {
      await turn.waitForStream();
    }
    turn.finalize();
    if (activeTurn === turn) activeTurn = null;
  }

  function ensureActiveTurn() {
    if (!activeTurn && Render && agentMessages) {
      activeTurn = Render.beginTurn(agentMessages, {});
    }
    return activeTurn;
  }

  function updateSessionMeta(sid, extra) {
    if (!agentSessionMeta) return;
    agentSessionMeta.textContent = sid
      ? `${sid.slice(0, 8)}…${extra ? " · " + extra : ""}`
      : "";
  }

  function attachmentKindLabel(filename) {
    const name = String(filename || "").toLowerCase();
    if (name.endsWith(".sdf")) return "SDF";
    if (name.endsWith(".pdf")) return "PDF";
    if (/\.(png|jpe?g|webp|gif)$/.test(name)) return "IMG";
    if (/\.(txt|md|csv|tsv|json|docx?)$/.test(name)) return "DOC";
    return "FILE";
  }

  function setAgentAttachment(filename, { pending, attachmentId } = {}) {
    if (!agentAttachRail) return;
    agentAttachRail.innerHTML = "";
    if (agentFileLabel && !agentUploadInProgress) agentFileLabel.textContent = "上传附件";
    // pending===false means session still has SDF but UI already moved it into a turn
    if (pending === false) return;
    if (!filename) return;

    const chip = document.createElement("div");
    chip.className = "mm-attach-chip";
    chip.title = filename;
    if (attachmentId) chip.dataset.attachmentId = attachmentId;

    const iconEl = document.createElement("span");
    iconEl.className = "mm-icon mm-icon--file-txt mm-icon--md mm-attach-chip-icon";
    iconEl.setAttribute("aria-hidden", "true");
    chip.appendChild(iconEl);

    const meta = document.createElement("div");
    meta.className = "mm-attach-chip-meta";
    meta.innerHTML = `
      <span class="mm-attach-chip-kind"></span>
      <span class="mm-attach-chip-name"></span>
    `;
    meta.querySelector(".mm-attach-chip-kind").textContent = attachmentKindLabel(filename);
    meta.querySelector(".mm-attach-chip-name").textContent = filename;
    chip.appendChild(meta);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "mm-attach-chip-remove";
    removeBtn.title = "移除附件";
    removeBtn.setAttribute("aria-label", "移除附件");
    removeBtn.innerHTML = '<span class="mm-icon mm-icon--x mm-icon--sm" aria-hidden="true"></span>';
    removeBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      try {
        await removeAgentAttachment();
      } catch (err) {
        showAgentToast(err.message || "移除失败");
      }
    });
    chip.appendChild(removeBtn);
    agentAttachRail.appendChild(chip);
  }

  async function removeAgentAttachment() {
    if (!agentSessionId) {
      setAgentAttachment(null);
      return;
    }
    const stagedChip = agentAttachRail && agentAttachRail.querySelector(".mm-attach-chip[data-attachment-id]");
    const attachmentId = stagedChip && stagedChip.dataset.attachmentId;
    const endpoint = attachmentId
      ? `/api/agent/sessions/${agentSessionId}/turn-attachments/${attachmentId}`
      : `/api/agent/sessions/${agentSessionId}/upload`;
    const resp = await fetch(endpoint, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(apiErrorMessage(err, "移除失败"));
    }
    setAgentAttachment(null);
    if (agentFileInput) agentFileInput.value = "";
    showAgentToast("已移除附件");
  }

  async function applyCatalogPrefs(sessionId, sessionInstalled) {
    if (!SettingsUI || !sessionId || typeof SettingsUI.syncPreferredToSession !== "function") {
      return;
    }
    try {
      await SettingsUI.syncPreferredToSession(sessionId, sessionInstalled || []);
    } catch {
      /* ignore sync errors — tools still work for builtins */
    }
  }

  async function ensureAgentSession() {
    if (agentSessionId) return agentSessionId;
    const resp = await fetch("/api/agent/sessions", { method: "POST" });
    if (!resp.ok) throw new Error("无法创建 Agent 会话");
    const data = await resp.json();
    migrateNewAgentDraft(data.session_id);
    setAgentSessionId(data.session_id);
    if (HistoryUI) HistoryUI.registerSession(data.session_id);
    updateSessionMeta(agentSessionId);
    await applyCatalogPrefs(agentSessionId, []);
    if (MentionUI) MentionUI.refresh(agentSessionId).catch(() => {});
    return agentSessionId;
  }

  function resetAgentChatUi({ keepWelcome } = {}) {
    detachAgentUi();
    if (Render && agentMessages) Render.clearMessages(agentMessages);
    setAgentEmpty(!!keepWelcome);
    setAgentAttachment(null);
    if (agentFileInput) agentFileInput.value = "";
    if (TurnRail) TurnRail.rebuild();
  }

  async function startNewAgentChat() {
    if (HistoryUI) HistoryUI.closeAll(agentChatRoot);
    if (isAgentNewConversation()) {
      showAgentToast("已经在新对话中");
      return;
    }
    setAgentSessionId(null);
    resetAgentChatUi({ keepWelcome: true });
    updateSessionMeta(null);
    syncAgentBusyUi();
    await ensureAgentSession();
  }

  function sessionHasLiveAsk(sessionId) {
    return (
      !!sessionId &&
      agentSessionId === sessionId &&
      activeTurn &&
      activeTurn.root &&
      activeTurn.root.isConnected
    );
  }

  async function renderAgentSessionTranscript(sessionId, { preserveLive = false } = {}) {
    const resp = await fetch(`/api/agent/sessions/${sessionId}`);
    if (!resp.ok) throw new Error("无法打开会话");
    const detail = await resp.json();
    const evResp = await fetch(`/api/agent/sessions/${sessionId}/events`);
    if (!evResp.ok) throw new Error("无法加载事件");
    const evData = await evResp.json();
    detail.active_run = evData.active_run || detail.active_run || null;
    detail._execution_events = evData.events || [];
    setAgentRunSnapshot(sessionId, detail.active_run);

    // Live send/follow already owns the DOM. Boot-time loadAgentSession can finish
    // *after* the user already painted a first ask — wiping here made the chat
    // look empty until a hard refresh rebuilt from durable messages.
    if (
      sessionHasLiveAsk(sessionId) ||
      (preserveLive && agentStreams.get(sessionId)?.running)
    ) {
      return detail;
    }

    if (Render && agentMessages) Render.clearMessages(agentMessages);
    const pendingSdf =
      detail.sdf_ui_pending && detail.has_sdf
        ? detail.sdf_filename || "library.sdf"
        : null;
    const stagedDraft = (detail.staged_attachments || [])
      .filter((item) => item && item.state === "draft")
      .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))[0];
    const hasContent =
      (detail.messages || []).length > 0 ||
      (detail.artifacts || []).length > 0 ||
      !!pendingSdf ||
      !!stagedDraft;
    setAgentEmpty(!hasContent);
    if (stagedDraft) {
      setAgentAttachment(stagedDraft.filename || "attachment.sdf", {
        attachmentId: stagedDraft.attachment_id,
      });
    } else {
      setAgentAttachment(pendingSdf);
    }
    updateSessionMeta(sessionId, detail.title || "");

    if (!(Render && agentMessages)) return detail;

    const parseMs =
      (Render.parseTimeMs && Render.parseTimeMs.bind(Render)) ||
      ((value) => {
        const t = Date.parse(String(value || ""));
        return Number.isFinite(t) ? t : null;
      });

    // Older interrupted streams could persist the same user message twice
    // (once at reservation and once at dispatch). Collapse only consecutive
    // exact duplicates during replay so historical transcripts do not create
    // shifted/empty answer cards; the durable event log remains untouched.
    const userMsgs = [];
    (detail.messages || [])
      .filter((m) => m.role === "user")
      .forEach((message) => {
        const previous = userMsgs[userMsgs.length - 1];
        if (
          previous &&
          String(previous.text || "") === String(message.text || "") &&
          JSON.stringify(previous.attachments || []) ===
            JSON.stringify(message.attachments || []) &&
          String(previous.run_id || "") === String(message.run_id || "")
        ) {
          return;
        }
        userMsgs.push(message);
      });

    const events = evData.events || [];
    const isTurnStart = (event) =>
      ["thinking", "plan", "assistant", "error", "run_interrupted"].includes(
        event && event.type
      );
    const eventTime = (event) => parseMs(event && event.occurred_at);

    // Group durable events by run_id (first-seen order), then sort each turn
    // by the earliest known timestamp so queue auto-drain cannot scramble DOM order.
    // Legacy events without run_id keep the old done-boundary batching.
    const runOrder = [];
    const eventsByRun = new Map();
    const anonymousBatches = [];
    let anonymousBatch = [];
    const flushAnonymous = () => {
      if (!anonymousBatch.length) return;
      anonymousBatches.push(anonymousBatch);
      anonymousBatch = [];
    };
    events.forEach((ev) => {
      const rid = String((ev && ev.run_id) || "");
      if (!rid) {
        anonymousBatch.push(ev);
        if (ev && ev.type === "done") flushAnonymous();
        return;
      }
      flushAnonymous();
      if (!eventsByRun.has(rid)) {
        eventsByRun.set(rid, []);
        runOrder.push(rid);
      }
      eventsByRun.get(rid).push(ev);
    });
    flushAnonymous();

    const usedUserIdx = new Set();
    const claimUserMessage = (runId, fallbackText) => {
      const rid = String(runId || "");
      if (rid) {
        const byId = userMsgs.findIndex(
          (msg, idx) => !usedUserIdx.has(idx) && String(msg.run_id || "") === rid
        );
        if (byId >= 0) {
          usedUserIdx.add(byId);
          return userMsgs[byId];
        }
      }
      const want = String(fallbackText || "").trim();
      if (want) {
        const byText = userMsgs.findIndex(
          (msg, idx) => !usedUserIdx.has(idx) && String(msg.text || "").trim() === want
        );
        if (byText >= 0) {
          usedUserIdx.add(byText);
          return userMsgs[byText];
        }
      }
      const next = userMsgs.findIndex((_, idx) => !usedUserIdx.has(idx));
      if (next >= 0) {
        usedUserIdx.add(next);
        return userMsgs[next];
      }
      return null;
    };

    const turnPlans = [];
    const pushPlanFromBatch = (runId, batch) => {
      if (!batch || !batch.length) return;
      if (!batch.some(isTurnStart)) return;
      const um = claimUserMessage(runId, "");
      if (!um) return;
      const times = [parseMs(um.created_at), ...batch.map(eventTime)].filter(
        (t) => t != null
      );
      const startedAt = times.length ? Math.min(...times) : Date.now();
      turnPlans.push({
        startedAt,
        runId: String(runId || ""),
        text: um.text || "",
        attachments: um.attachments || [],
        events: batch,
      });
    };

    runOrder.forEach((runId) => pushPlanFromBatch(runId, eventsByRun.get(runId) || []));
    anonymousBatches.forEach((batch) => pushPlanFromBatch("", batch));

    // User messages that never received execution events (e.g. still starting).
    const maxTimed = turnPlans.reduce(
      (max, plan) => Math.max(max, Number(plan.startedAt) || 0),
      0
    );
    let orphanOffset = 0;
    userMsgs.forEach((um, idx) => {
      if (usedUserIdx.has(idx)) return;
      const created = parseMs(um.created_at);
      turnPlans.push({
        startedAt: created != null ? created : maxTimed + ++orphanOffset,
        runId: String(um.run_id || ""),
        text: um.text || "",
        attachments: um.attachments || [],
        events: [],
      });
    });

    // Active run may exist before `_prepare_turn` persists the user message.
    const active = detail.active_run;
    if (active && isAgentRunActive(active)) {
      const activeRunId = String(active.run_id || "");
      const already = turnPlans.some(
        (plan) => activeRunId && plan.runId === activeRunId
      );
      if (!already) {
        const askText =
          String(active.display_text || "").trim() ||
          String((active.input && active.input.text) || "").trim();
        if (askText) {
          turnPlans.push({
            startedAt: parseMs(active.started_at) ?? Date.now(),
            runId: activeRunId,
            text: askText,
            attachments: resolveRunAskAttachments(active),
            events: [],
          });
        }
      }
    }

    turnPlans.forEach((plan, index) => {
      plan.order = index;
    });
    turnPlans.sort((a, b) => {
      const dt = a.startedAt - b.startedAt;
      if (dt) return dt;
      // Second-precision server timestamps can collide under fast queue drain;
      // keep plan construction order (run/event discovery order) as tie-break.
      return (a.order || 0) - (b.order || 0);
    });

    turnPlans.forEach((plan) => {
      const turn = Render.beginTurn(agentMessages, {
        text: plan.text,
        attachments: plan.attachments,
        startedAt: plan.startedAt,
        runId: plan.runId,
      });
      if (plan.events && plan.events.length) {
        Render.replayEventsIntoTurn(turn, plan.events);
      }
    });

    if (typeof Render.sortTurnsByTime === "function") {
      Render.sortTurnsByTime(agentMessages);
    }

    const turns = agentMessages.querySelectorAll(".mm-turn");
    const lastTurn = turns.length ? turns[turns.length - 1] : null;
    const artifactsEl = lastTurn && lastTurn.querySelector(".mm-turn-artifacts");
    (detail.artifacts || []).forEach((card) => {
      const cardHref = window.MolMindClientIdentity
        ? window.MolMindClientIdentity.decorateDownloadUrl(card.download_url)
        : card.download_url;
      const exists = agentMessages.querySelector(`a[href="${cardHref}"]`);
      if (exists) return;
      Render.appendArtifactCard(artifactsEl || agentMessages, card);
    });
    if (TurnRail) TurnRail.rebuild();
    agentScrollBottom({ force: true });
    return detail;
  }

  async function loadAgentSession(sessionId) {
    // Same-session live send already owns the transcript — only refresh metadata.
    if (sessionHasLiveAsk(sessionId) || agentStreams.get(sessionId)?.running) {
      try {
        const detail = await renderAgentSessionTranscript(sessionId, {
          preserveLive: true,
        });
        renderAgentQueue(detail.pending_turns || [], detail.queue_limit || 3);
        await refreshAgentQueue(sessionId);
        syncAgentBusyUi();
        return detail;
      } catch {
        /* fall through to a full load */
      }
    }

    // Detach UI only — do not abort the previous session's HTTP stream.
    detachAgentUi();
    if (Render && agentMessages) Render.clearMessages(agentMessages);

    setAgentSessionId(sessionId);
    const detail = await renderAgentSessionTranscript(sessionId);
    renderAgentQueue(detail.pending_turns || [], detail.queue_limit || 3);
    if (!isAgentRunActive(detail && detail.active_run)) {
      await applyCatalogPrefs(sessionId, (detail && detail.installed_catalog) || []);
    }

    const live = agentStreams.get(sessionId);
    if (live && live.running) {
      if (RunStatus) {
        const liveRunId = String(
          live.runId || (detail.active_run && detail.active_run.run_id) || ""
        );
        const liveEvents = (detail._execution_events || []).filter(
          (event) => !liveRunId || String(event.run_id || "") === liveRunId
        );
        RunStatus.restore(liveEvents, detail.active_run || { status: "running" });
      }
      // Background generation still going: refresh transcript when it finishes.
      live.onComplete = async () => {
        if (agentSessionId !== sessionId) return;
        try {
          await renderAgentSessionTranscript(sessionId);
        } catch {
          /* ignore refresh errors */
        }
        syncAgentBusyUi();
      };
    } else if (isAgentRunActive(detail && detail.active_run)) {
      startRecoveredAgentRunFollow(sessionId, detail);
    }
    syncAgentBusyUi();
    await refreshAgentQueue(sessionId);
    if (
      agentSessionId === sessionId &&
      !agentStreams.get(sessionId)?.running &&
      !isAgentRunActive(currentAgentRun(sessionId)) &&
      agentPendingTurns.length
    ) {
      // Opening a session with durable queued turns — drain after transcript paint.
      await continueAgentQueueFollow(sessionId);
    }
    if (MentionUI) MentionUI.refresh(sessionId).catch(() => {});
  }

  function waitForAgentPoll(ms, signal) {
    return new Promise((resolve, reject) => {
      if (signal && signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      const timer = setTimeout(resolve, ms);
      if (signal) {
        signal.addEventListener(
          "abort",
          () => {
            clearTimeout(timer);
            reject(new DOMException("Aborted", "AbortError"));
          },
          { once: true }
        );
      }
    });
  }

  function startRecoveredAgentRunFollow(sessionId, detail) {
    const run = detail && detail.active_run;
    if (!sessionId || !isAgentRunActive(run)) return;
    const existing = agentStreams.get(sessionId);
    if (existing && existing.running) return;

    const entry = startSessionStream(sessionId);
    entry.recovered = true;
    entry.runId = String(run.run_id || "");
    let afterSeq = (detail._execution_events || []).reduce(
      (max, event) => Math.max(max, Number(event.seq || 0)),
      0
    );
    const initialRunEvents = (detail._execution_events || []).filter(
      (event) => !entry.runId || String(event.run_id || "") === entry.runId
    );
    if (agentSessionId === sessionId && RunStatus) {
      RunStatus.restore(initialRunEvents, run);
      RunStatus.setReconnectState(false);
    }
    syncAgentBusyUi();

    (async () => {
      try {
        while (isStreamEntryActive(sessionId, entry)) {
          await waitForAgentPoll(800, entry.controller.signal);
          let resp;
          try {
            resp = await fetch(
              `/api/agent/sessions/${sessionId}/events?after_seq=${afterSeq}`,
              { signal: entry.controller.signal, cache: "no-store" }
            );
          } catch (error) {
            if (error && error.name === "AbortError") throw error;
            if (agentSessionId === sessionId && RunStatus) {
              RunStatus.setReconnectState(true);
            }
            await waitForAgentPoll(1600, entry.controller.signal);
            continue;
          }
          if (resp.status === 404) {
            setAgentRunSnapshot(sessionId, null);
            break;
          }
          if (!resp.ok) {
            if (agentSessionId === sessionId && RunStatus) {
              RunStatus.setReconnectState(true);
            }
            await waitForAgentPoll(1600, entry.controller.signal);
            continue;
          }
          const data = await resp.json();
          const events = data.events || [];
          const latestRunId = String((data.active_run && data.active_run.run_id) || "");
          if (latestRunId && latestRunId !== entry.runId) {
            entry.runId = latestRunId;
            if (agentSessionId === sessionId && RunStatus) {
              RunStatus.reset();
              RunStatus.restore([], data.active_run);
            }
          }
          events.forEach((event) => {
            afterSeq = Math.max(afterSeq, Number(event.seq || 0));
            if (
              agentSessionId === sessionId &&
              RunStatus &&
              (!entry.runId || String(event.run_id || "") === entry.runId)
            ) {
              RunStatus.applyEvent(event);
            }
          });
          setAgentRunSnapshot(sessionId, data.active_run || null);
          if (agentSessionId === sessionId) refreshAgentQueue(sessionId);
          if (events.length && agentSessionId === sessionId) {
            const refreshed = await renderAgentSessionTranscript(sessionId, {
              preserveLive: true,
            });
            setAgentRunSnapshot(sessionId, refreshed.active_run || null);
          }
          syncAgentBusyUi();
          if (!isAgentRunActive(currentAgentRun(sessionId))) break;
        }
      } catch (error) {
        if (!(error && error.name === "AbortError") && agentSessionId === sessionId) {
          if (RunStatus) RunStatus.setReconnectState(true);
          showAgentToast("执行仍在后台，连接恢复后将继续同步");
        }
      } finally {
        if (agentStreams.get(sessionId) === entry) {
          entry.running = false;
          agentStreams.delete(sessionId);
        }
        if (agentSessionId === sessionId) {
          try {
            const refreshed = await renderAgentSessionTranscript(sessionId);
            setAgentRunSnapshot(sessionId, refreshed.active_run || null);
            await refreshAgentQueue(sessionId);
            if (isAgentRunActive(refreshed.active_run)) {
              // Prefer a live ask/answer box for the newly activated Run so the
              // queue drain feels like a normal user send (not a silent re-fetch).
              await followActiveRunWithLiveTurn(sessionId, refreshed.active_run);
            } else {
              // Current run ended; wait briefly then pick up the next queued turn.
              await continueAgentQueueFollow(sessionId);
              if (RunStatus && !isCurrentAgentSessionBusy()) {
                RunStatus.finalize();
              }
            }
          } catch {
            /* keep the last durable snapshot visible */
          }
        }
        syncAgentBusyUi();
      }
    })();
  }

  async function promoteNextQueuedTurn(sessionId) {
    const resp = await fetch(`/api/agent/sessions/${sessionId}/turns/next`, {
      method: "POST",
    });
    if (!resp.ok) {
      throw new Error(
        apiErrorMessage(await resp.json().catch(() => ({})), "无法启动排队任务")
      );
    }
    return resp.json();
  }

  function activeRunAskText(run) {
    if (!run || typeof run !== "object") return "";
    const display = String(run.display_text || "").trim();
    if (display) return display;
    return String((run.input && run.input.text) || "").trim();
  }

  function lastTurnAskText() {
    if (!agentMessages) return "";
    const turns = agentMessages.querySelectorAll(".mm-turn");
    const last = turns.length ? turns[turns.length - 1] : null;
    return last ? String(last.getAttribute("data-turn-text") || "") : "";
  }

  function findTurnApiByRunId(runId) {
    const rid = String(runId || "");
    if (!rid || !agentMessages) return null;
    if (
      activeTurn &&
      activeTurn.root &&
      activeTurn.root.isConnected &&
      String(activeTurn.runId || activeTurn.root.getAttribute("data-run-id") || "") === rid
    ) {
      return activeTurn;
    }
    return null;
  }

  function runStartedAtMs(run) {
    if (!run) return Date.now();
    const parseMs =
      (Render && Render.parseTimeMs) ||
      ((value) => {
        const t = Date.parse(String(value || ""));
        return Number.isFinite(t) ? t : null;
      });
    return (
      parseMs(run.started_at) ??
      parseMs(run.created_at) ??
      Date.now()
    );
  }

  /**
   * Follow an already-active Run by painting a live ask/answer box immediately,
   * then applying durable events into that turn (same UX as a normal send).
   * Pass existingTurn to reuse a cleared turn (e.g. checkpoint retry).
   */
  async function followActiveRunWithLiveTurn(sessionId, run, { existingTurn = null } = {}) {
    if (!sessionId || !run || !Render || !agentMessages) return;
    if (agentSessionId !== sessionId) return;
    if (agentStreams.get(sessionId)?.running) return;

    const runId = String(run.run_id || "");
    let ownedTurn = existingTurn && existingTurn.root && existingTurn.root.isConnected
      ? existingTurn
      : null;
    if (!ownedTurn) {
      ownedTurn = findTurnApiByRunId(runId);
    }
    if (!ownedTurn) {
      const askText = activeRunAskText(run);
      // Transcript recovery may have already painted this ask; reuse that node.
      const existingRoot = runId
        ? Array.from(agentMessages.querySelectorAll(".mm-turn")).find(
            (el) => el.getAttribute("data-run-id") === runId
          )
        : null;
      if (existingRoot) {
        // Prefer recovered follow into the durable transcript rather than a
        // second live box that would land out of chronological order.
        startRecoveredAgentRunFollow(sessionId, {
          active_run: run,
          _execution_events: [],
        });
        return;
      }
      if (askText && lastTurnAskText() === askText) {
        startRecoveredAgentRunFollow(sessionId, {
          active_run: run,
          _execution_events: [],
        });
        return;
      }

      setAgentEmpty(false);
      finishTurn();
      ownedTurn = Render.beginTurn(agentMessages, {
        text: askText,
        attachments: resolveRunAskAttachments(run),
        live: true,
        onScroll: agentScrollBottom,
        startedAt: runStartedAtMs(run),
        runId,
      });
      if (typeof Render.sortTurnsByTime === "function") {
        Render.sortTurnsByTime(agentMessages);
      }
    } else if (typeof ownedTurn.resetAnswer === "function") {
      // Ensure interrupted error / retry chrome is gone before new events arrive.
      ownedTurn.resetAnswer();
    }
    activeTurn = ownedTurn;
    if (TurnRail) TurnRail.rebuild();
    agentScrollBottom({ force: true });

    const entry = startSessionStream(sessionId);
    entry.recovered = true;
    entry.runId = String(run.run_id || "");
    let afterSeq = Math.max(0, Number(run.last_event_seq || 0));
    setAgentRunSnapshot(sessionId, run);
    if (RunStatus) {
      RunStatus.reset();
      RunStatus.restore([], run);
      RunStatus.setReconnectState(false);
    }
    syncAgentBusyUi();

    try {
      while (isStreamEntryActive(sessionId, entry)) {
        await waitForAgentPoll(500, entry.controller.signal);
        let resp;
        try {
          resp = await fetch(
            `/api/agent/sessions/${sessionId}/events?after_seq=${afterSeq}`,
            { signal: entry.controller.signal, cache: "no-store" }
          );
        } catch (error) {
          if (error && error.name === "AbortError") throw error;
          if (agentSessionId === sessionId && RunStatus) {
            RunStatus.setReconnectState(true);
          }
          await waitForAgentPoll(1200, entry.controller.signal);
          continue;
        }
        if (resp.status === 404) {
          setAgentRunSnapshot(sessionId, null);
          break;
        }
        if (!resp.ok) {
          if (agentSessionId === sessionId && RunStatus) {
            RunStatus.setReconnectState(true);
          }
          await waitForAgentPoll(1200, entry.controller.signal);
          continue;
        }
        const data = await resp.json();
        const events = data.events || [];
        const latestRunId = String((data.active_run && data.active_run.run_id) || "");
        if (latestRunId && latestRunId !== entry.runId) {
          // A newer Run replaced this one (guidance / recovery). Stop painting here.
          entry.runId = latestRunId;
          setAgentRunSnapshot(sessionId, data.active_run || null);
          break;
        }
        let sawTerminal = false;
        for (const event of events) {
          afterSeq = Math.max(afterSeq, Number(event.seq || 0));
          if (
            agentSessionId === sessionId &&
            RunStatus &&
            (!entry.runId || String(event.run_id || "") === entry.runId)
          ) {
            RunStatus.applyEvent(event);
          }
          if (
            canPaintStream(sessionId, entry, ownedTurn, event) &&
            (!entry.runId || !event.run_id || String(event.run_id) === entry.runId)
          ) {
            ownedTurn.applyEvent(event);
            if (event.type === "done" || event.type === "error" || event.type === "run_interrupted") {
              noteAgentStreamTerminal(sessionId, event);
              sawTerminal = true;
              await finishTurnAfterStream(ownedTurn);
            }
            agentScrollBottom();
          } else if (
            event.type === "done" ||
            event.type === "error" ||
            event.type === "run_interrupted"
          ) {
            noteAgentStreamTerminal(sessionId, event);
            sawTerminal = true;
          }
        }
        setAgentRunSnapshot(sessionId, data.active_run || null);
        if (agentSessionId === sessionId) refreshAgentQueue(sessionId);
        syncAgentBusyUi();
        if (sawTerminal || !isAgentRunActive(currentAgentRun(sessionId))) break;
      }
    } catch (error) {
      if (!(error && error.name === "AbortError") && agentSessionId === sessionId) {
        if (RunStatus) RunStatus.setReconnectState(true);
        showAgentToast("执行仍在后台，连接恢复后将继续同步");
      }
    } finally {
      if (agentStreams.get(sessionId) === entry) {
        entry.running = false;
        agentStreams.delete(sessionId);
      }
      if (agentSessionId === sessionId) {
        try {
          await refreshAgentQueue(sessionId);
        } catch {
          /* ignore */
        }
        if (isAgentRunActive(currentAgentRun(sessionId))) {
          // Still running (e.g. switched run id) — recover via durable transcript.
          try {
            const refreshed = await renderAgentSessionTranscript(sessionId);
            if (isAgentRunActive(refreshed.active_run)) {
              startRecoveredAgentRunFollow(sessionId, refreshed);
            }
          } catch {
            /* keep live turn visible */
          }
        } else {
          await continueAgentQueueFollow(sessionId);
          if (RunStatus && !isCurrentAgentSessionBusy()) {
            RunStatus.finalize();
          }
        }
      }
      syncAgentBusyUi();
      if (TurnRail) TurnRail.rebuild();
      agentScrollBottom();
    }
  }

  async function continueAgentQueueFollow(sessionId) {
    if (!sessionId) return;
    if (agentSuppressQueueFollow) {
      agentSuppressQueueFollow = false;
      agentQueueFollowDeferred = null;
      return;
    }
    // Install confirmation owns the conversation gate: do not auto-promote
    // queued turns underneath the modal (that scrambled ask/answer order).
    if (isInstallRequestOpen() || agentInstallResumeHold || isAgentStopGateActive(sessionId)) {
      agentQueueFollowDeferred = sessionId;
      return;
    }
    if (agentQueueFollowActive) {
      // Another drain loop is already responsible; remember if we need a
      // follow-up pass after install/UI settle.
      if (hasAgentQueueWaiting()) agentQueueFollowDeferred = sessionId;
      return;
    }
    agentQueueFollowActive = true;
    try {
      // Wait for the previous turn's streaming UI to settle, then explicitly
      // promote the next queued Turn and paint ask/answer like a normal send.
      for (let attempt = 0; attempt < 12; ) {
        if (agentSessionId !== sessionId) return;
        if (isInstallRequestOpen() || agentInstallResumeHold) {
          agentQueueFollowDeferred = sessionId;
          return;
        }
        if (agentStreams.get(sessionId)?.running) return;
        if (isAgentStreamUiPending()) {
          // Do not burn promote retries while the typewriter is still draining.
          if (activeTurn && typeof activeTurn.waitForStream === "function") {
            try {
              await Promise.race([
                activeTurn.waitForStream(),
                waitForAgentPoll(20000),
              ]);
            } catch {
              await waitForAgentPoll(350);
            }
          } else {
            await waitForAgentPoll(350);
          }
          continue;
        }
        attempt += 1;
        await waitForAgentPoll(attempt === 1 ? 200 : 350);
        if (agentSessionId !== sessionId) return;
        if (isInstallRequestOpen() || agentInstallResumeHold) {
          agentQueueFollowDeferred = sessionId;
          return;
        }
        if (agentStreams.get(sessionId)?.running) return;
        if (isAgentStreamUiPending()) continue;

        try {
          await refreshAgentQueue(sessionId);
        } catch {
          continue;
        }
        if (agentSessionId !== sessionId) return;
        if (isInstallRequestOpen() || agentInstallResumeHold) {
          agentQueueFollowDeferred = sessionId;
          return;
        }

        if (isAgentRunActive(currentAgentRun(sessionId))) {
          await followActiveRunWithLiveTurn(sessionId, currentAgentRun(sessionId));
          return;
        }
        if (!agentPendingTurns.length) return;

        let started = null;
        try {
          started = await promoteNextQueuedTurn(sessionId);
        } catch (error) {
          if (attempt >= 12) {
            showAgentToast(error.message || "排队任务启动失败");
            return;
          }
          continue;
        }
        if (agentSessionId !== sessionId) return;
        try {
          await refreshAgentQueue(sessionId);
        } catch {
          /* queue rail is best-effort */
        }
        if (started && started.started && started.active_run) {
          await followActiveRunWithLiveTurn(sessionId, started.active_run);
          return;
        }
        if (isAgentRunActive(currentAgentRun(sessionId))) {
          await followActiveRunWithLiveTurn(sessionId, currentAgentRun(sessionId));
          return;
        }
        if (!agentPendingTurns.length) return;
      }
    } finally {
      agentQueueFollowActive = false;
      if (
        agentQueueFollowDeferred &&
        !isInstallRequestOpen() &&
        !agentInstallResumeHold &&
        agentSessionId === agentQueueFollowDeferred &&
        hasAgentQueueWaiting()
      ) {
        const again = agentQueueFollowDeferred;
        agentQueueFollowDeferred = null;
        continueAgentQueueFollow(again).catch(() => {});
      }
    }
  }

  const AGENT_ATTACHMENT_EXTS = [
    ".sdf",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".doc",
    ".docx",
  ];

  function isAllowedAgentAttachment(file) {
    const name = String((file && file.name) || "").toLowerCase();
    return AGENT_ATTACHMENT_EXTS.some((ext) => name.endsWith(ext));
  }

  async function uploadAgentSdf(file) {
    if (!isAllowedAgentAttachment(file)) {
      throw new Error("支持的附件：.sdf / .pdf / 图片 / 文本与文档");
    }
    const sid = await ensureAgentSession();
    const fd = new FormData();
    fd.append("file", file);
    const staged = isCurrentAgentSessionBusy();
    const isSdf = /\.sdf$/i.test(file.name || "");
    if (staged && agentAttachRail) {
      const previous = agentAttachRail.querySelector(".mm-attach-chip[data-attachment-id]");
      if (previous && previous.dataset.attachmentId) {
        await fetch(
          `/api/agent/sessions/${sid}/turn-attachments/${previous.dataset.attachmentId}`,
          { method: "DELETE" }
        ).catch(() => {});
      }
    }
    // Idle SDF binds the session library; other types (and busy uploads) are turn-scoped.
    const endpoint =
      staged || !isSdf
        ? `/api/agent/sessions/${sid}/turn-attachments`
        : `/api/agent/sessions/${sid}/upload`;
    const resp = await fetch(endpoint, {
      method: "POST",
      body: fd,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(apiErrorMessage(err, "上传失败"));
    }
    const data = await resp.json();
    const name = data.sdf_filename || (data.attachment && data.attachment.filename) || file.name;
    if (data.attachment) {
      rememberAttachmentMeta(data.attachment);
      setAgentAttachment(name, { attachmentId: data.attachment.attachment_id });
      setAgentEmpty(false);
      showAgentToast(
        staged ? "附件已暂存，将随下一条消息进入排队一并发送" : "附件已添加，发送消息时一并提交"
      );
    } else {
      afterAgentSdfAttached(name);
    }
    if (agentFileInput) agentFileInput.value = "";
  }

  function setAgentUploadState(uploading) {
    agentUploadInProgress = uploading;
    if (agentUploadBtn) {
      agentUploadBtn.classList.toggle("mm-upload-btn--loading", uploading);
      agentUploadBtn.setAttribute("aria-busy", String(uploading));
      agentUploadBtn.setAttribute("aria-disabled", String(uploading));
      agentUploadBtn.title = uploading ? "附件上传中" : "上传附件";
    }
    if (agentFileInput) agentFileInput.disabled = uploading;
    if (agentFileLabel) agentFileLabel.textContent = uploading ? "上传中…" : "上传附件";
  }

  const DEMO_SDF_FALLBACK_NAME = "T001 TargetMol现货产品22966.sdf";
  let demoSdfName = DEMO_SDF_FALLBACK_NAME;
  let demoSdfSource = "data/T001 TargetMol现货产品22966.sdf";
  let demoPopMask = null;

  async function refreshDemoSdfInfo() {
    try {
      const resp = await fetch("/api/agent/demo/sdf/info");
      if (!resp.ok) return;
      const data = await resp.json();
      if (data && data.filename) demoSdfName = data.filename;
      if (data && data.source) demoSdfSource = data.source;
    } catch {
      /* keep fallback */
    }
  }

  function closeDemoSdfPop() {
    if (!demoPopMask) return;
    if (agentDemoSdfBtn) agentDemoSdfBtn.setAttribute("aria-expanded", "false");
    if (agentUploadBtn) agentUploadBtn.setAttribute("aria-expanded", "false");
    if (typeof demoPopMask._cleanupKey === "function") {
      try {
        demoPopMask._cleanupKey();
      } catch {
        /* ignore */
      }
    }
    demoPopMask.classList.remove("mm-demo-pop-mask--open");
    const mask = demoPopMask;
    demoPopMask = null;
    setTimeout(() => {
      if (mask.parentNode) mask.parentNode.removeChild(mask);
    }, 220);
  }

  function afterAgentSdfAttached(name) {
    setAgentAttachment(name);
    setAgentEmpty(false);
    if (Render && agentMessages) {
      // An attached SDF is actionable immediately. Show the deterministic
      // choice card here rather than relying on a model reply or waiting for
      // an empty submit; it applies equally to uploads and the demo library.
      showAttachmentClarify([{ kind: "sdf", filename: name }]);
      agentScrollBottom();
    }
  }

  async function attachDemoSdfToSession() {
    showAgentToast("正在加载试用库…");
    const sid = await ensureAgentSession();
    // While the current turn UI is still settling, stage as a turn attachment so
    // the file rides the queue with the next prompt.
    const stage = isCurrentAgentSessionBusy();
    const resp = await fetch(
      `/api/agent/sessions/${sid}/demo-sdf${stage ? "?stage=1" : ""}`,
      { method: "POST" }
    );
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(apiErrorMessage(err, "试用库加载失败"));
    }
    const data = await resp.json();
    if (data.attachment) {
      rememberAttachmentMeta(data.attachment);
      setAgentAttachment(data.attachment.filename || demoSdfName, {
        attachmentId: data.attachment.attachment_id,
      });
      setAgentEmpty(false);
      showAgentToast(
        stage
          ? "附件已暂存，将随下一条消息进入排队一并发送"
          : "附件已添加，发送消息时一并提交"
      );
      showAttachmentClarify([
        {
          kind: "sdf",
          filename: data.attachment.filename || demoSdfName,
          attachment_id: data.attachment.attachment_id,
        },
      ]);
      agentScrollBottom();
      return;
    }
    afterAgentSdfAttached(data.sdf_filename || demoSdfName);
    showAgentToast("已作为附件加入当前对话");
  }

  function downloadDemoSdf() {
    const a = document.createElement("a");
    a.href = "/api/agent/demo/sdf";
    a.download = demoSdfName || DEMO_SDF_FALLBACK_NAME;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    showAgentToast("开始下载试用库");
  }

  async function openDemoSdfPop() {
    closeDemoSdfPop();
    await refreshDemoSdfInfo();
    if (agentDemoSdfBtn) agentDemoSdfBtn.setAttribute("aria-expanded", "true");
    if (agentUploadBtn) agentUploadBtn.setAttribute("aria-expanded", "true");
    const name = demoSdfName || DEMO_SDF_FALLBACK_NAME;
    const mask = document.createElement("div");
    mask.className = "mm-demo-pop-mask";
    mask.setAttribute("role", "presentation");
    mask.innerHTML = `
      <div class="mm-demo-pop" role="dialog" aria-modal="true" aria-label="可选试用样例库">
        <div class="mm-demo-pop-head">
          <h3 class="mm-demo-pop-title">可选试用样例库</h3>
          <p class="mm-demo-pop-sub">${
            demoSdfSource.startsWith("data/")
              ? "TargetMol 现货产品参考全库（约 2.3 万条），可直接试用或下载"
              : `当前挂载：${demoSdfSource}`
          }</p>
        </div>
        <div class="mm-demo-pop-actions">
          <button type="button" class="mm-demo-pop-item" data-action="try">
            <span class="mm-icon mm-icon--file-txt mm-icon--md mm-demo-pop-item-icon" aria-hidden="true"></span>
            <span class="mm-demo-pop-item-body">
              <span class="mm-demo-pop-item-label">试用${name}</span>
              <span class="mm-demo-pop-item-hint">直接作为当前对话附件</span>
            </span>
          </button>
          <button type="button" class="mm-demo-pop-item" data-action="download">
            <span class="mm-icon mm-icon--download mm-icon--md mm-demo-pop-item-icon" aria-hidden="true"></span>
            <span class="mm-demo-pop-item-body">
              <span class="mm-demo-pop-item-label">下载${name}</span>
              <span class="mm-demo-pop-item-hint">保存到本地</span>
            </span>
          </button>
          <button type="button" class="mm-demo-pop-item" data-action="upload">
            <span class="mm-icon mm-icon--upload mm-icon--md mm-demo-pop-item-icon" aria-hidden="true"></span>
            <span class="mm-demo-pop-item-body">
              <span class="mm-demo-pop-item-label">上传其他附件</span>
              <span class="mm-demo-pop-item-hint">.sdf / PDF / 图片 / 文档</span>
            </span>
          </button>
        </div>
      </div>
    `;
    const dialog = mask.querySelector(".mm-demo-pop");
    dialog.addEventListener("click", (e) => e.stopPropagation());
    mask.addEventListener("click", () => closeDemoSdfPop());
    mask.querySelector('[data-action="try"]').addEventListener("click", async (e) => {
      e.preventDefault();
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        await attachDemoSdfToSession();
        closeDemoSdfPop();
      } catch (err) {
        btn.disabled = false;
        showAgentToast(err.message || "试用失败");
      }
    });
    mask.querySelector('[data-action="download"]').addEventListener("click", (e) => {
      e.preventDefault();
      downloadDemoSdf();
      closeDemoSdfPop();
    });
    mask.querySelector('[data-action="upload"]').addEventListener("click", (e) => {
      e.preventDefault();
      closeDemoSdfPop();
      if (agentFileInput) agentFileInput.click();
    });
    const onKey = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeDemoSdfPop();
      }
    };
    mask._cleanupKey = () => document.removeEventListener("keydown", onKey);
    document.addEventListener("keydown", onKey);
    document.body.appendChild(mask);
    demoPopMask = mask;
    requestAnimationFrame(() => mask.classList.add("mm-demo-pop-mask--open"));
  }

  function getPendingAgentAttachments() {
    const chips = [];
    if (!agentAttachRail) return chips;
    agentAttachRail.querySelectorAll(".mm-attach-chip").forEach((chip) => {
      const nameEl = chip.querySelector(".mm-attach-chip-name");
      const kindEl = chip.querySelector(".mm-attach-chip-kind");
      const filename = (nameEl && nameEl.textContent) || "";
      if (!filename) return;
      const kind = ((kindEl && kindEl.textContent) || "sdf").trim().toLowerCase() || "sdf";
      chips.push({
        kind,
        filename,
        attachment_id: chip.dataset.attachmentId || "",
      });
    });
    return chips;
  }

  function clarifyChoicesForAttachment(att) {
    const kind = (att && att.kind) || "sdf";
    const name = (att && att.filename) || "附件";
    if (kind === "sdf" || /\.sdf$/i.test(name)) {
      return {
        title: `已收到化合物库「${name}」，你想用它做什么？`,
        choices: [
          "生成 top10 候选清单 csv",
          "生成 top10 候选，并给出机制与验证方案 pdf",
          "只要机制与验证方案 pdf",
          "先介绍这份化合物库能做什么",
        ],
      };
    }
    return {
      title: `已收到附件「${name}」，你想用它做什么？`,
      choices: [
        "请根据这个附件帮我分析一下",
        "介绍这个附件可以怎么用",
      ],
    };
  }

  function showAttachmentClarify(attachments) {
    if (!Render || !agentMessages || !attachments || !attachments.length) return;
    const primary = attachments[0];
    const { title, choices } = clarifyChoicesForAttachment(primary);
    setAgentEmpty(false);
    // Remove previous pending clarify cards so we don't stack duplicates.
    dismissAttachmentClarify();

    const box = document.createElement("div");
    box.className = "mm-prompt-suggest mm-prompt-suggest--clarify";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "附件用途确认");
    box.innerHTML = `<div class="mm-prompt-suggest-title"></div>`;
    box.querySelector(".mm-prompt-suggest-title").textContent = title;
    const list = document.createElement("div");
    list.className = "mm-prompt-suggest-list";
    choices.forEach((text) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mm-prompt-chip";
      btn.textContent = text;
      btn.addEventListener("click", async () => {
        box.remove();
        if (agentInput) {
          agentInput.value = "";
          resizeAgentInput();
        }
        await sendAgentMessage(text);
      });
      list.appendChild(btn);
    });
    box.appendChild(list);
    agentMessages.appendChild(box);
    agentShouldFollow = true;
    agentScrollBottom({ force: true });
    if (agentInput) agentInput.focus();
  }

  function dismissAttachmentClarify() {
    if (!agentMessages) return;
    agentMessages
      .querySelectorAll(".mm-prompt-suggest--clarify")
      .forEach((el) => el.remove());
  }

  function shouldWarnSnapshotFallback(attachments, text) {
    const hasSdf = (attachments || []).some(
      (attachment) => attachment && (attachment.kind === "sdf" || /\.sdf$/i.test(attachment.filename || ""))
    );
    return hasSdf && /csv|候选清单|提名清单/i.test(String(text || ""));
  }

  async function sendAgentMessage(text, opts = {}) {
    if (!Render) return;
    if (isAgentStopGateActive() && !opts.forceDirect) {
      showAgentToast("正在停止，请稍候再发送");
      return;
    }
    // User chose to send instead of picking a quick prompt — drop the clarify card.
    dismissAttachmentClarify();
    // Do not dismiss the install float here: it gates queue auto-drain. Closing it
    // on every send let queued turns race under the install decision.
    const submitted = String(text || "").trim();
    if (!submitted) return;
    const forceDirect = !!opts.forceDirect;

    if (!forceDirect && shouldEnqueueAgentSend()) {
      if (agentQueueCount >= 3) {
        showAgentToast("排队已满（最多 3 条），请等待或将某条提升为指引");
        return;
      }
      const holdInstall = isInstallRequestOpen() || agentInstallResumeHold;
      const holdQueueOrSettle =
        agentPendingTurns.length > 0 ||
        agentOptimisticTurns.length > 0 ||
        isAgentStreamUiPending() ||
        agentQueueFollowActive ||
        !!agentQueueFollowDeferred;
      try {
        // Kick off submit first so the optimistic queue card paints before the input clears.
        // Always mode=queue so the server never starts ahead of durable pending turns
        // while the previous answer is still rendering or install is open.
        const pending = submitBusyAgentTurn(submitted, "queue");
        clearAgentInput();
        if (holdInstall) {
          showAgentToast("已加入排队；安装完成后将先续接原请求，再按顺序发送");
        } else if (holdQueueOrSettle) {
          showAgentToast("已加入排队，将按顺序发送");
        }
        return await pending;
      } catch (error) {
        restoreAgentInputText(submitted);
        throw error;
      }
    }

    const pendingChips = getPendingAgentAttachments();

    let ownedTurn = null;
    let streamSid = null;
    let entry = null;
    clearAgentInput();
    // Paint the ask immediately — before create-session / catalog sync — so the
    // first message never waits on the network, and a racing boot-time session
    // load cannot leave the user staring at an empty welcome screen.
    setAgentEmpty(false);
    finishTurn();
    agentShouldFollow = true;
    ownedTurn = Render.beginTurn(agentMessages, {
      text: submitted,
      attachments: pendingChips,
      live: true,
      onScroll: agentScrollBottom,
      startedAt: Date.now(),
    });
    activeTurn = ownedTurn;
    if (shouldWarnSnapshotFallback(pendingChips, submitted)) {
      ownedTurn.appendAssistant(
        "提示：若附件中的分子未命中本地证据快照，本轮会转入本地补洞与结构计算；生成 CSV 的耗时可能明显增加，请耐心等待。",
        { instant: true }
      );
    }
    if (pendingChips.length) setAgentAttachment(null, { pending: false });
    if (TurnRail) TurnRail.rebuild();
    agentScrollBottom({ force: true });

    try {
      streamSid = await ensureAgentSession();
      entry = startSessionStream(streamSid);
      const signal = entry.controller.signal;
      if (RunStatus) {
        RunStatus.reset();
        RunStatus.setVisible(true);
      }
      syncAgentBusyUi();

      const attachmentIds = pendingChips
        .map((item) => item.attachment_id)
        .filter(Boolean);
      const resp = await fetch(`/api/agent/sessions/${streamSid}/message/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: submitted,
          attachment_ids: attachmentIds,
        }),
        signal,
      });
      if (!isStreamEntryActive(streamSid, entry)) return;
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(apiErrorMessage(err, `请求失败 (${resp.status})`));
      }
      // Input was cleared at send start; keep draft store in sync.
      persistAgentDraft();
      entry.runId = resp.headers.get("X-MolMind-Run-ID") || "";
      if (entry.runId) {
        setAgentRunSnapshot(streamSid, {
          run_id: entry.runId,
          status: "running",
        });
        if (ownedTurn && ownedTurn.root) {
          ownedTurn.runId = entry.runId;
          ownedTurn.root.setAttribute("data-run-id", entry.runId);
        }
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let sawTerminal = false;

      // Always drain to completion so the server finishes & persists — even if UI detached.
      while (true) {
        if (!isStreamEntryActive(streamSid, entry)) {
          try {
            await reader.cancel();
          } catch {
            /* ignore */
          }
          break;
        }
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line) continue;
          if (!isStreamEntryActive(streamSid, entry)) {
            buf = "";
            break;
          }
          let ev;
          try {
            ev = JSON.parse(line);
          } catch {
            continue;
          }
          if (ev.run_id && !entry.runId) entry.runId = String(ev.run_id);
          if (ev.type === "done") {
            const prior = currentAgentRun(streamSid) || {};
            const doneStatus = String(ev.status || "").trim();
            setAgentRunSnapshot(streamSid, {
              ...prior,
              run_id: String(ev.run_id || entry.runId || prior.run_id || ""),
              // Prefer explicit backend status; do not invent succeeded over a
              // prior failed/interrupted snapshot when the event omits status.
              status: doneStatus || String(prior.status || "succeeded"),
            });
          }
          if (agentSessionId === streamSid && isStreamEntryActive(streamSid, entry) && RunStatus) {
            // Keep status strip updates during stop freeze (shows cancel_requested).
            if (
              !entry.paintFrozen ||
              ev.type === "done" ||
              ev.type === "error" ||
              ev.type === "run_interrupted" ||
              ev.type === "thinking"
            ) {
              RunStatus.applyEvent(ev);
            }
          }
          if (canPaintStream(streamSid, entry, ownedTurn, ev)) {
            ownedTurn.applyEvent(ev);
            if (ev.type === "assistant_delta") {
              agentScrollBottom();
              // One frame per delta so a burst of tokens still reveals progressively.
              await yieldAgentPaintFrame();
              continue;
            }
            if (
              ev.type === "done" ||
              ev.type === "error" ||
              ev.type === "run_interrupted"
            ) {
              noteAgentStreamTerminal(streamSid, ev);
              sawTerminal = true;
              await finishTurnAfterStream(ownedTurn);
            }
            agentScrollBottom();
          } else if (
            ev.type === "done" ||
            ev.type === "error" ||
            ev.type === "run_interrupted"
          ) {
            noteAgentStreamTerminal(streamSid, ev);
            sawTerminal = true;
          }
        }
      }
      if (
        !sawTerminal &&
        canPaintStream(streamSid, entry, ownedTurn)
      ) {
        await finishTurnAfterStream(ownedTurn);
      }
    } catch (err) {
      const aborted =
        (entry && entry.controller.signal.aborted) ||
        (err && err.name === "AbortError");
      const accepted = !!(entry && entry.runId);
      if (!aborted && !accepted) {
        restoreAgentInputText(submitted);
        // Optimistic ask was painted before the network; remove it on hard fail.
        if (ownedTurn && ownedTurn.root && ownedTurn.root.isConnected) {
          ownedTurn.root.remove();
        }
        if (activeTurn === ownedTurn) activeTurn = null;
        setAgentEmpty(isAgentNewConversation());
        if (TurnRail) TurnRail.rebuild();
        syncAgentBusyUi();
      }
      if (aborted || !entry || !isStreamEntryActive(streamSid, entry)) return;
      if (canPaintStream(streamSid, entry, ownedTurn)) {
        if (typeof ownedTurn.abortStream === "function") ownedTurn.abortStream();
        ownedTurn.appendAssistant(`错误：${err.message || err}`, { error: true });
        ownedTurn.finalize();
        if (activeTurn === ownedTurn) activeTurn = null;
      }
    } finally {
      if (entry && agentStreams.get(streamSid) === entry) {
        const onComplete = entry.onComplete;
        entry.running = false;
        entry.onComplete = null;
        agentStreams.delete(streamSid);
        if (agentSessionId === streamSid && (!ownedTurn || !ownedTurn.root.isConnected)) {
          try {
            const refreshed = await renderAgentSessionTranscript(streamSid);
            setAgentRunSnapshot(streamSid, refreshed.active_run || null);
          } catch {
            /* the event stream remains the fallback transcript */
          }
        }
        syncAgentBusyUi();
        await refreshAgentQueue(streamSid);
        if (typeof onComplete === "function") {
          try {
            await onComplete();
          } catch {
            /* ignore */
          }
        } else if (
          agentSessionId === streamSid &&
          ownedTurn &&
          !ownedTurn.root.isConnected
        ) {
          // Detached mid-flight without a reload hook — pull final transcript.
          try {
            await renderAgentSessionTranscript(streamSid);
          } catch {
            /* ignore */
          }
        }
        // Drain queued Turns as normal user sends after the current run settles.
        if (agentSessionId === streamSid) {
          await continueAgentQueueFollow(streamSid);
        }
      }
      if (TurnRail) TurnRail.rebuild();
      agentScrollBottom();
    }
  }

  async function submitBusyAgentTurn(text, mode, { optimistic = true } = {}) {
    const pending = getPendingAgentAttachments();
    const attachmentIds = pending.map((item) => item.attachment_id).filter(Boolean);
    const attachments = pending.map((item) => ({
      attachment_id: item.attachment_id || "",
      filename: item.filename || "",
      kind: item.kind || "",
    }));
    // Paint an empty loading card immediately so clearing the input does not feel like a failed send.
    // Callers that already own a queue-rail transition (e.g. promote-to-guidance) can skip this.
    const optimisticKey = optimistic
      ? pushOptimisticQueueTurn({
          kind: mode === "guidance" ? "guidance" : "queue",
          text,
          attachment_ids: attachmentIds,
          attachments,
        })
      : null;
    try {
      const sid = await ensureAgentSession();
      const idempotencyKey =
        (window.crypto && typeof window.crypto.randomUUID === "function")
          ? window.crypto.randomUUID()
          : `turn-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const resp = await fetch(`/api/agent/sessions/${sid}/turns`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text,
          mode: mode === "guidance" ? "guidance" : "queue",
          attachment_ids: attachmentIds,
          idempotency_key: idempotencyKey,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(apiErrorMessage(err, `请求失败 (${resp.status})`));
      }
      const accepted = await resp.json();
      consumeAgentDraft(text);
      if (pending.length) setAgentAttachment(null, { pending: false });
      // Drop placeholder before refresh paint to avoid a duplicate flash.
      if (optimisticKey) removeOptimisticQueueTurn(optimisticKey, { paint: false });
      await refreshAgentQueue(sid);
      return accepted;
    } catch (error) {
      if (optimisticKey) removeOptimisticQueueTurn(optimisticKey);
      throw error;
    }
  }

  async function interruptCurrentAgentRun() {
    const sid = agentSessionId;
    if (!sid) return;
    if (isAgentStopGateActive(sid)) {
      showAgentToast("正在停止，请稍候");
      return;
    }
    if (!isCurrentAgentSessionBusy()) return;
    const run = currentAgentRun(sid);
    const stream = agentStreams.get(sid);
    const runId = String((run && run.run_id) || (stream && stream.runId) || "");
    if (!runId) {
      showAgentToast("没有可停止的任务");
      return;
    }
    // Freeze UI + ban ops BEFORE the network round-trip (highest priority).
    beginAgentStopGate(sid, runId);
    setAgentRunSnapshot(sid, {
      ...(run || {}),
      run_id: runId,
      status: "cancel_requested",
    });
    showAgentToast("正在停止当前任务");
    // Hold queue drain until the interrupted terminal event arrives,
    // then suppress auto-promote so stop does not immediately start the next turn.
    agentQueueFollowDeferred = sid;
    agentSuppressQueueFollow = true;
    try {
      const resp = await fetch(
        `/api/agent/sessions/${sid}/runs/${encodeURIComponent(runId)}/interrupt`,
        { method: "POST" }
      );
      if (!resp.ok) {
        throw new Error(
          apiErrorMessage(await resp.json().catch(() => ({})), "停止失败")
        );
      }
    } catch (error) {
      clearAgentStopGate(sid);
      showAgentToast(error.message || "停止失败");
      syncAgentBusyUi();
    } finally {
      syncAgentBusyUi();
    }
  }

  async function openAgentHistory() {
    if (!HistoryUI) return;
    if (SettingsUI) SettingsUI.close(agentChatRoot, agentSettingsPanel);
    HistoryUI.open(agentChatRoot, agentHistoryPanel);

    async function refreshHistoryList() {
      const loadSeq = ++agentHistoryLoadSeq;
      if (agentHistoryCount) agentHistoryCount.textContent = "";
      renderAgentDrawerLoading(agentHistoryList, "history");
      try {
        const sessions = await HistoryUI.fetchSessions();
        if (loadSeq !== agentHistoryLoadSeq) return;
        agentHistoryList.setAttribute("aria-busy", "false");
        HistoryUI.renderList(agentHistoryList, sessions, async (s) => {
          HistoryUI.close(agentChatRoot, agentHistoryPanel);
          try {
            await loadAgentSession(s.session_id);
          } catch (err) {
            alert(err.message || err);
          }
        }, {
          activeId: agentSessionId,
          countEl: agentHistoryCount,
          onRefresh: refreshHistoryList,
          onDeleted: (sid) => {
            abortSessionStream(sid);
            if (agentSessionId === sid) {
              setAgentSessionId(null);
              resetAgentChatUi({ keepWelcome: true });
              updateSessionMeta(null);
            }
            syncAgentBusyUi();
          },
        });
      } catch (error) {
        if (loadSeq !== agentHistoryLoadSeq) return;
        renderAgentDrawerError(agentHistoryList, error.message || "无法加载会话历史");
        throw error;
      }
    }

    try {
      await refreshHistoryList();
    } catch {
      /* error state is rendered inside the history drawer */
    }
  }

  async function refreshAgentSettings({ loading = false } = {}) {
    if (!SettingsUI || !agentSettingsBody) return;
    const loadSeq = ++agentSettingsLoadSeq;
    if (loading) renderAgentDrawerLoading(agentSettingsBody, "catalog");
    try {
      const settings = await SettingsUI.fetchSettings(agentSessionId);
      if (loadSeq !== agentSettingsLoadSeq) return;
      agentSettingsBody.setAttribute("aria-busy", "false");
      SettingsUI.render(agentSettingsBody, settings, {
        sessionId: agentSessionId,
        onChanged: async () => {
          await refreshAgentSettings();
          if (MentionUI && agentSessionId) MentionUI.refresh(agentSessionId).catch(() => {});
        },
      });
    } catch (error) {
      if (loadSeq === agentSettingsLoadSeq) {
        renderAgentDrawerError(agentSettingsBody, error.message || "无法加载工具与插件");
      }
      throw error;
    }
  }

  async function openAgentSettings() {
    if (!SettingsUI) return;
    if (HistoryUI) HistoryUI.close(agentChatRoot, agentHistoryPanel);
    SettingsUI.open(agentChatRoot, agentSettingsPanel);
    renderAgentDrawerLoading(agentSettingsBody, "catalog");
    try {
      if (!agentSessionId) await ensureAgentSession();
      await refreshAgentSettings({ loading: true });
    } catch (error) {
      if (agentSettingsBody && agentSettingsBody.getAttribute("aria-busy") === "true") {
        renderAgentDrawerError(
          agentSettingsBody,
          error.message || "无法加载工具与插件"
        );
      }
    }
  }

  if (modeClassicBtn) modeClassicBtn.addEventListener("click", () => setWorkMode("classic"));
  if (modeAgentBtn) modeAgentBtn.addEventListener("click", () => setWorkMode("agent"));
  const modeAgentFab = document.getElementById("modeAgentFab");
  if (modeAgentFab) modeAgentFab.addEventListener("click", () => setWorkMode("agent"));
  if (agentFileInput) {
    agentFileInput.addEventListener("change", async () => {
      const file = agentFileInput.files && agentFileInput.files[0];
      if (!file || agentUploadInProgress) return;
      setAgentUploadState(true);
      try {
        await uploadAgentSdf(file);
      } catch (err) {
        setAgentEmpty(false);
        if (Render) {
          Render.appendAssistantBubble(agentMessages, `上传失败：${err.message || err}`, {
            error: true,
          });
        }
      } finally {
        // Let the user select the same file again after a failed upload.
        if (agentFileInput) agentFileInput.value = "";
        setAgentUploadState(false);
      }
    });
  }
  function resizeAgentInput() {
    if (!agentInput) return;
    const INPUT_MIN_H = 24;
    const INPUT_MAX_H = 200;
    agentInput.classList.remove("mm-send-input--scroll");
    agentInput.style.height = "auto";
    const next = Math.min(
      Math.max(agentInput.scrollHeight, INPUT_MIN_H),
      INPUT_MAX_H
    );
    agentInput.style.height = `${next}px`;
    if (agentInput.scrollHeight > INPUT_MAX_H) {
      agentInput.classList.add("mm-send-input--scroll");
    }
  }

  if (agentChatForm) {
    agentChatForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = ((agentInput && agentInput.value) || "").trim();
      const pending = getPendingAgentAttachments();
      if (!text) {
        if (pending.length) {
          showAttachmentClarify(pending);
        } else {
          showAgentToast("请先输入内容");
        }
        return;
      }
      try {
        await sendAgentMessage(text);
      } catch (error) {
        showAgentToast(error.message || "发送失败，草稿已保留");
      }
    });
  }
  if (agentStopBtn) {
    agentStopBtn.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      await interruptCurrentAgentRun();
    });
  }
  if (agentInput) {
    agentInput.addEventListener("input", () => {
      resizeAgentInput();
      scheduleAgentDraftSave();
    });
    agentInput.addEventListener("keydown", (e) => {
      const composing = e.isComposing || e.keyCode === 229;
      if (MentionUI && MentionUI.isOpen()) {
        // ↑↓ Esc Tab 与有候选项时的 Enter 由 MentionUI 处理
        if (
          e.key === "ArrowUp" ||
          e.key === "ArrowDown" ||
          e.key === "Escape" ||
          e.key === "Tab" ||
          (e.key === "Enter" && !e.shiftKey && !composing && MentionUI.hasChoices())
        ) {
          return;
        }
      }
      // 打字中回车只换行，不发送；Ctrl/Cmd+Enter 发送
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !composing) {
        e.preventDefault();
        if (agentChatForm) agentChatForm.requestSubmit();
        return;
      }
      if (e.key === "Enter") {
        requestAnimationFrame(resizeAgentInput);
      }
    });
    resizeAgentInput();
  }
  window.addEventListener("pagehide", () => persistAgentDraft());
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    persistAgentDraft();
    // Background tabs throttle setTimeout; finish typewriters immediately so
    // waitForStream / queue drain can continue without the window focused.
    if (activeTurn && typeof activeTurn.abortStream === "function") {
      activeTurn.abortStream();
    }
    if (Render && typeof Render.flushLiveReveals === "function") {
      Render.flushLiveReveals();
    }
    syncAgentBusyUi();
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== AGENT_DRAFT_KEY || !agentSessionId || !agentInput) return;
    const incoming = readAgentDrafts()[agentSessionId];
    if (!agentInput.value && incoming && incoming.text) restoreAgentDraft(agentSessionId);
  });
  if (MentionUI && agentInput) {
    const sendWrap = agentInput.closest(".mm-send-wrapper") || agentInput.parentElement;
    MentionUI.attach({
      input: agentInput,
      anchor: sendWrap,
      getSessionId: () => agentSessionId,
    });
  }
  if (agentNewChatBtn) agentNewChatBtn.addEventListener("click", () => startNewAgentChat());
  if (agentUploadBtn) {
    agentUploadBtn.addEventListener("click", () => {
      if (demoPopMask) closeDemoSdfPop();
      else openDemoSdfPop();
    });
  }
  if (agentDemoSdfBtn) {
    agentDemoSdfBtn.addEventListener("click", () => {
      if (demoPopMask) closeDemoSdfPop();
      else openDemoSdfPop();
    });
  }
  if (agentHistoryBtn) agentHistoryBtn.addEventListener("click", () => openAgentHistory());
  if (mmProfileBanner && profileInfoModal) {
    const ClientIdentity = window.MolMindClientIdentity;
    const closeProfileInfo = () => {
      profileInfoModal.classList.remove("mm-profile-info-modal--open");
      profileInfoModal.setAttribute("aria-hidden", "true");
    };
    mmProfileBanner.addEventListener("click", () => {
      if (profileInfoClientId) {
        profileInfoClientId.textContent = ClientIdentity ? ClientIdentity.clientId : "不可用";
      }
      profileInfoModal.classList.add("mm-profile-info-modal--open");
      profileInfoModal.setAttribute("aria-hidden", "false");
      if (profileInfoVersion) {
        fetch("/health", { cache: "no-store" })
          .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error("health request failed"))))
          .then((data) => {
            profileInfoVersion.textContent = data.version || "未知";
            if (profileInfoBuild) profileInfoBuild.textContent = data.build || "未知";
          })
          .catch(() => {
            profileInfoVersion.textContent = "未知";
            if (profileInfoBuild) profileInfoBuild.textContent = "未知";
          });
      }
    });
    if (profileInfoClientEdit && ClientIdentity) {
      profileInfoClientEdit.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        closeProfileInfo();

        const oldMask = document.getElementById("agentClientIdDialog");
        if (oldMask) oldMask.remove();
        const mask = document.createElement("div");
        mask.id = "agentClientIdDialog";
        mask.className = "mm-confirm-mask";
        mask.setAttribute("role", "dialog");
        mask.setAttribute("aria-modal", "true");
        mask.setAttribute("aria-labelledby", "agentClientIdDialogTitle");
        mask.innerHTML = `
          <div class="mm-confirm-dialog">
            <div class="mm-confirm-header">
              <span class="mm-icon mm-icon--rotate mm-confirm-icon mm-confirm-icon--neutral" aria-hidden="true"></span>
              <h3 id="agentClientIdDialogTitle">切换用户 ID</h3>
            </div>
            <div class="mm-confirm-content">
              <label class="mm-confirm-label" for="agentClientIdInput">用户 ID</label>
              <input id="agentClientIdInput" class="mm-confirm-input" type="text" maxlength="128" autocomplete="off" spellcheck="false" />
              <p class="mm-confirm-error" aria-live="polite"></p>
            </div>
            <div class="mm-confirm-footer">
              <button type="button" class="mm-confirm-cancel">取消</button>
              <button type="button" class="mm-confirm-ok mm-confirm-ok--primary">切换</button>
            </div>
          </div>
        `;

        const input = mask.querySelector("#agentClientIdInput");
        const cancelBtn = mask.querySelector(".mm-confirm-cancel");
        const submitBtn = mask.querySelector(".mm-confirm-ok");
        const errorEl = mask.querySelector(".mm-confirm-error");
        input.value = ClientIdentity.clientId;

        const close = () => {
          window.removeEventListener("keydown", onDialogKeydown);
          mask.classList.remove("mm-confirm-mask--open");
          setTimeout(() => mask.remove(), 220);
        };
        const submit = async () => {
          const target = String(input.value || "").trim();
          input.classList.remove("mm-confirm-input--invalid");
          errorEl.textContent = "";
          if (!target) {
            input.classList.add("mm-confirm-input--invalid");
            errorEl.textContent = "请输入用户 ID";
            input.focus();
            return;
          }
          if (target === ClientIdentity.clientId) {
            close();
            return;
          }
          submitBtn.disabled = true;
          submitBtn.textContent = "验证中…";
          try {
            const resp = await fetch("/api/agent/clients/validate", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ client_id: target }),
            });
            if (!resp.ok) {
              const detail = await resp.json().catch(() => ({}));
              throw new Error(detail.detail || "未找到该用户记录");
            }
            const validated = await resp.json();
            ClientIdentity.switchClientId(target, validated.latest_session_id);
            window.location.reload();
          } catch (error) {
            input.classList.add("mm-confirm-input--invalid");
            errorEl.textContent = error.message || "未找到该用户记录";
            input.focus();
            submitBtn.disabled = false;
            submitBtn.textContent = "切换";
          }
        };
        function onDialogKeydown(keyEvent) {
          if (keyEvent.key === "Escape") {
            keyEvent.preventDefault();
            close();
          } else if (keyEvent.key === "Enter") {
            keyEvent.preventDefault();
            submit();
          }
        }
        cancelBtn.addEventListener("click", close);
        submitBtn.addEventListener("click", submit);
        input.addEventListener("input", () => {
          input.classList.remove("mm-confirm-input--invalid");
          errorEl.textContent = "";
        });
        mask.addEventListener("click", (maskEvent) => {
          if (maskEvent.target === mask) close();
        });
        window.addEventListener("keydown", onDialogKeydown);
        document.body.appendChild(mask);
        requestAnimationFrame(() => {
          mask.classList.add("mm-confirm-mask--open");
          input.focus();
          input.select();
        });
      });
    }
    if (profileClassicModeBtn) {
      profileClassicModeBtn.addEventListener("click", (event) => {
        event.preventDefault();
        closeProfileInfo();
        setWorkMode("classic");
      });
    }
    profileInfoModal.querySelectorAll("[data-profile-close]").forEach((el) => {
      el.addEventListener("click", closeProfileInfo);
    });
    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && profileInfoModal.classList.contains("mm-profile-info-modal--open")) {
        closeProfileInfo();
      }
    });
  }
  if (agentHistoryClearBtn) {
    agentHistoryClearBtn.addEventListener("click", async () => {
      try {
        const cleared = await HistoryUI.clearHistory();
        if (cleared && agentSessionId) {
          abortSessionStream(agentSessionId);
          setAgentSessionId(null);
          resetAgentChatUi({ keepWelcome: true });
          updateSessionMeta(null);
          syncAgentBusyUi();
        }
      } catch (err) {
        alert(err.message || err);
      }
    });
  }
  if (agentSettingsBtn) agentSettingsBtn.addEventListener("click", () => openAgentSettings());
  if (agentHistoryCloseBtn) {
    agentHistoryCloseBtn.addEventListener("click", () =>
      HistoryUI && HistoryUI.close(agentChatRoot, agentHistoryPanel)
    );
  }
  if (agentSettingsCloseBtn) {
    agentSettingsCloseBtn.addEventListener("click", () =>
      SettingsUI && SettingsUI.close(agentChatRoot, agentSettingsPanel)
    );
  }
  if (agentDrawerScrim) {
    agentDrawerScrim.addEventListener("click", () => {
      if (HistoryUI) HistoryUI.closeAll(agentChatRoot);
    });
  }

  setModeTabStyles();
  showUploadOnly();
  syncNewChatBtnTitle();

  (async () => {
    const cachedSid = readCachedAgentSessionId();
    if (cachedSid) {
      try {
        await loadAgentSession(cachedSid);
      } catch {
        // Session may have been deleted or expired — fall back to welcome.
        setAgentSessionId(null);
      }
    } else {
      restoreAgentDraft(null);
    }
    if (window.MolMindAgentTour) {
      window.MolMindAgentTour.maybeStart();
    }
  })();
})();
