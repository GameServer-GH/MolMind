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
  const agentWelcome = document.getElementById("agentWelcome");
  const agentStreamBeam = document.getElementById("agentStreamBeam");
  const agentNewChatBtn = document.getElementById("agentNewChatBtn");
  const agentDemoSdfBtn = document.getElementById("agentDemoSdfBtn");
  const agentHistoryBtn = document.getElementById("agentHistoryBtn");
  const agentSettingsBtn = document.getElementById("agentSettingsBtn");
  const isMacPlatform =
    /Mac|iPhone|iPad|iPod/i.test(navigator.platform || "") ||
    (navigator.userAgentData && navigator.userAgentData.platform === "macOS");

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
  const agentHistoryCloseBtn = document.getElementById("agentHistoryCloseBtn");
  const agentSettingsPanel = document.getElementById("agentSettingsPanel");
  const agentSettingsBody = document.getElementById("agentSettingsBody");
  const agentSettingsCloseBtn = document.getElementById("agentSettingsCloseBtn");
  const agentDrawerScrim = document.getElementById("agentDrawerScrim");
  const agentTurnRailEl = document.getElementById("agentTurnRail");

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

  const AGENT_SESSION_KEY = "molmind:agent_active_session_v1";
  let agentSessionId = null;
  let agentBusy = false;
  let activeTurn = null;
  /**
   * Per-session in-flight NDJSON streams.
   * Switching chats detaches the UI but keeps the HTTP stream alive so the server
   * can finish and persist; coming back reloads (and refreshes again on complete).
   * @type {Map<string, { id: number, controller: AbortController, running: boolean, onComplete: null | (() => void | Promise<void>) }>}
   */
  const agentStreams = new Map();
  let agentStreamSeq = 0;

  function readCachedAgentSessionId() {
    try {
      return localStorage.getItem(AGENT_SESSION_KEY) || null;
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
    agentSessionId = sid || null;
    writeCachedAgentSessionId(agentSessionId);
  }

  function syncAgentBusyUi() {
    const cur = agentSessionId ? agentStreams.get(agentSessionId) : null;
    const busy = !!(cur && cur.running);
    agentBusy = busy;
    setStreaming(busy);
    if (agentSendBtn) agentSendBtn.disabled = busy;
    if (RunStatus) {
      if (busy) RunStatus.setVisible(true);
      else RunStatus.setVisible(false);
    }
  }

  function isStreamEntryActive(sid, entry) {
    return !!entry && agentStreams.get(sid) === entry && entry.running;
  }

  function canPaintStream(sid, entry, turn) {
    return (
      isStreamEntryActive(sid, entry) &&
      agentSessionId === sid &&
      turn &&
      turn.root &&
      turn.root.isConnected
    );
  }

  /** Stop painting into the current DOM without cancelling background generation. */
  function detachAgentUi() {
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

  function agentScrollBottom() {
    if (agentChatScroll) agentChatScroll.scrollTop = agentChatScroll.scrollHeight;
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

  function setAgentAttachment(filename, { pending } = {}) {
    if (!agentAttachRail) return;
    agentAttachRail.innerHTML = "";
    if (agentFileLabel) agentFileLabel.textContent = "上传附件";
    // pending===false means session still has SDF but UI already moved it into a turn
    if (pending === false) return;
    if (!filename) return;

    const chip = document.createElement("div");
    chip.className = "mm-attach-chip";
    chip.title = filename;

    const iconEl = document.createElement("span");
    iconEl.className = "mm-icon mm-icon--file-txt mm-icon--md mm-attach-chip-icon";
    iconEl.setAttribute("aria-hidden", "true");
    chip.appendChild(iconEl);

    const meta = document.createElement("div");
    meta.className = "mm-attach-chip-meta";
    meta.innerHTML = `
      <span class="mm-attach-chip-kind">SDF</span>
      <span class="mm-attach-chip-name"></span>
    `;
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
    const resp = await fetch(`/api/agent/sessions/${agentSessionId}/upload`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "移除失败");
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

  async function renderAgentSessionTranscript(sessionId) {
    const resp = await fetch(`/api/agent/sessions/${sessionId}`);
    if (!resp.ok) throw new Error("无法打开会话");
    const detail = await resp.json();
    const evResp = await fetch(`/api/agent/sessions/${sessionId}/events`);
    if (!evResp.ok) throw new Error("无法加载事件");
    const evData = await evResp.json();

    if (Render && agentMessages) Render.clearMessages(agentMessages);
    const pendingSdf =
      detail.sdf_ui_pending && detail.has_sdf
        ? detail.sdf_filename || "library.sdf"
        : null;
    const hasContent =
      (detail.messages || []).length > 0 ||
      (detail.artifacts || []).length > 0 ||
      !!pendingSdf;
    setAgentEmpty(!hasContent);
    setAgentAttachment(pendingSdf);
    updateSessionMeta(sessionId, detail.title || "");

    if (!(Render && agentMessages)) return detail;

    const userMsgs = (detail.messages || []).filter((m) => m.role === "user");
    const events = evData.events || [];

    let u = 0;
    let needUser = true;
    let batch = [];
    let turn = null;
    const flush = () => {
      if (!batch.length) return;
      if (!turn) turn = Render.beginTurn(agentMessages, {});
      Render.replayEventsIntoTurn(turn, batch);
      turn = null;
      batch = [];
    };
    events.forEach((ev) => {
      if (needUser && (ev.type === "thinking" || ev.type === "plan") && u < userMsgs.length) {
        flush();
        const um = userMsgs[u++];
        turn = Render.beginTurn(agentMessages, {
          text: um.text || "",
          attachments: um.attachments || [],
        });
        needUser = false;
      }
      batch.push(ev);
      if (ev.type === "done") {
        flush();
        needUser = true;
      }
    });
    flush();
    while (u < userMsgs.length) {
      const um = userMsgs[u++];
      Render.beginTurn(agentMessages, {
        text: um.text || "",
        attachments: um.attachments || [],
      });
    }
    const turns = agentMessages.querySelectorAll(".mm-turn");
    const lastTurn = turns.length ? turns[turns.length - 1] : null;
    const artifactsEl = lastTurn && lastTurn.querySelector(".mm-turn-artifacts");
    (detail.artifacts || []).forEach((card) => {
      const exists = agentMessages.querySelector(`a[href="${card.download_url}"]`);
      if (exists) return;
      Render.appendArtifactCard(artifactsEl || agentMessages, card);
    });
    if (TurnRail) TurnRail.rebuild();
    agentScrollBottom();
    return detail;
  }

  async function loadAgentSession(sessionId) {
    // Detach UI only — do not abort the previous session's HTTP stream.
    detachAgentUi();
    if (Render && agentMessages) Render.clearMessages(agentMessages);

    setAgentSessionId(sessionId);
    const detail = await renderAgentSessionTranscript(sessionId);
    await applyCatalogPrefs(sessionId, (detail && detail.installed_catalog) || []);

    const live = agentStreams.get(sessionId);
    if (live && live.running) {
      if (RunStatus) {
        RunStatus.reset();
        RunStatus.applyEvent({ type: "thinking", text: "后台生成中" });
        RunStatus.applyEvent({ type: "plan", steps: ["继续处理当前任务"] });
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
    }
    syncAgentBusyUi();
    if (MentionUI) MentionUI.refresh(sessionId).catch(() => {});
  }

  async function uploadAgentSdf(file) {
    const sid = await ensureAgentSession();
    const fd = new FormData();
    fd.append("file", file);
    const resp = await fetch(`/api/agent/sessions/${sid}/upload`, {
      method: "POST",
      body: fd,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "上传失败");
    }
    const data = await resp.json();
    const name = data.sdf_filename || file.name;
    afterAgentSdfAttached(name);
    if (agentFileInput) agentFileInput.value = "";
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
      const Tour = window.MolMindAgentTour;
      if (Tour) {
        Tour.showPromptSuggestions(agentMessages, {
          onPick: (text) => {
            if (agentInput) {
              agentInput.value = text;
              agentInput.focus();
              if (typeof resizeAgentInput === "function") resizeAgentInput();
            }
          },
        });
      }
      agentScrollBottom();
    }
  }

  async function attachDemoSdfToSession() {
    showAgentToast("正在加载试用库…");
    const sid = await ensureAgentSession();
    const resp = await fetch(`/api/agent/sessions/${sid}/demo-sdf`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || "试用库加载失败");
    }
    const data = await resp.json();
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
    const name = demoSdfName || DEMO_SDF_FALLBACK_NAME;
    const mask = document.createElement("div");
    mask.className = "mm-demo-pop-mask";
    mask.setAttribute("role", "presentation");
    mask.innerHTML = `
      <div class="mm-demo-pop" role="dialog" aria-modal="true" aria-label="试用样例库">
        <div class="mm-demo-pop-head">
          <h3 class="mm-demo-pop-title">试用样例库</h3>
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
      chips.push({ kind, filename });
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
    agentMessages.querySelectorAll(".mm-prompt-suggest--clarify").forEach((el) => el.remove());

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
    agentScrollBottom();
    if (agentInput) agentInput.focus();
  }

  async function sendAgentMessage(text) {
    if (!Render) return;
    // Only block if *this* visible session is already streaming.
    if (agentSessionId && agentStreams.get(agentSessionId)?.running) return;

    const pendingChips = getPendingAgentAttachments();

    let ownedTurn = null;
    let streamSid = null;
    let entry = null;
    try {
      streamSid = await ensureAgentSession();
      entry = startSessionStream(streamSid);
      const signal = entry.controller.signal;
      if (RunStatus) {
        RunStatus.reset();
        RunStatus.setVisible(true);
      }
      syncAgentBusyUi();
      setAgentEmpty(false);
      finishTurn();

      ownedTurn = Render.beginTurn(agentMessages, {
        text,
        attachments: pendingChips,
        live: true,
        onScroll: agentScrollBottom,
      });
      activeTurn = ownedTurn;
      if (pendingChips.length) setAgentAttachment(null, { pending: false });
      if (TurnRail) TurnRail.rebuild();
      agentScrollBottom();

      const resp = await fetch(`/api/agent/sessions/${streamSid}/message/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
        signal,
      });
      if (!isStreamEntryActive(streamSid, entry)) return;
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `请求失败 (${resp.status})`);
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
          if (agentSessionId === streamSid && isStreamEntryActive(streamSid, entry) && RunStatus) {
            RunStatus.applyEvent(ev);
          }
          if (canPaintStream(streamSid, entry, ownedTurn)) {
            ownedTurn.applyEvent(ev);
            if (ev.type === "done" || ev.type === "error") {
              sawTerminal = true;
              await finishTurnAfterStream(ownedTurn);
            }
            agentScrollBottom();
          } else if (ev.type === "done" || ev.type === "error") {
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
      if (aborted || !isStreamEntryActive(streamSid, entry)) return;
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
        syncAgentBusyUi();
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
      }
      if (TurnRail) TurnRail.rebuild();
      agentScrollBottom();
    }
  }

  async function openAgentHistory() {
    if (!HistoryUI) return;
    if (SettingsUI) SettingsUI.close(agentChatRoot, agentSettingsPanel);
    HistoryUI.open(agentChatRoot, agentHistoryPanel);

    async function refreshHistoryList() {
      const sessions = await HistoryUI.fetchSessions();
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
    }

    try {
      await refreshHistoryList();
    } catch (err) {
      if (agentHistoryList) {
        agentHistoryList.innerHTML = `<p class="mm-history-empty">${err.message || err}</p>`;
      }
    }
  }

  async function refreshAgentSettings() {
    if (!SettingsUI || !agentSettingsBody) return;
    const settings = await SettingsUI.fetchSettings(agentSessionId);
    SettingsUI.render(agentSettingsBody, settings, {
      sessionId: agentSessionId,
      onChanged: async () => {
        await refreshAgentSettings();
        if (MentionUI && agentSessionId) MentionUI.refresh(agentSessionId).catch(() => {});
      },
    });
  }

  async function openAgentSettings() {
    if (!SettingsUI) return;
    if (HistoryUI) HistoryUI.close(agentChatRoot, agentHistoryPanel);
    SettingsUI.open(agentChatRoot, agentSettingsPanel);
    try {
      if (!agentSessionId) await ensureAgentSession();
      await refreshAgentSettings();
    } catch (err) {
      if (agentSettingsBody) {
        agentSettingsBody.innerHTML = `<p class="mm-history-empty">${err.message || err}</p>`;
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
      if (!file) return;
      try {
        await uploadAgentSdf(file);
      } catch (err) {
        setAgentEmpty(false);
        if (Render) {
          Render.appendAssistantBubble(agentMessages, `上传失败：${err.message || err}`, {
            error: true,
          });
        }
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
      if (agentInput) {
        agentInput.value = "";
        resizeAgentInput();
      }
      await sendAgentMessage(text);
    });
  }
  if (agentInput) {
    agentInput.addEventListener("input", resizeAgentInput);
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
  if (MentionUI && agentInput) {
    const sendWrap = agentInput.closest(".mm-send-wrapper") || agentInput.parentElement;
    MentionUI.attach({
      input: agentInput,
      anchor: sendWrap,
      getSessionId: () => agentSessionId,
    });
  }
  if (agentNewChatBtn) agentNewChatBtn.addEventListener("click", () => startNewAgentChat());
  if (agentDemoSdfBtn) {
    agentDemoSdfBtn.addEventListener("click", () => {
      if (demoPopMask) closeDemoSdfPop();
      else openDemoSdfPop();
    });
  }
  if (agentHistoryBtn) agentHistoryBtn.addEventListener("click", () => openAgentHistory());
  if (agentHistoryClearBtn) {
    agentHistoryClearBtn.addEventListener("click", async () => {
      try {
        await HistoryUI.clearLocalHistory();
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
    }
    if (window.MolMindAgentTour) {
      window.MolMindAgentTour.maybeStart();
    }
  })();
})();
