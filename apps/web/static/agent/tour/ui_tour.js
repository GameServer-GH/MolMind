/* MolMind Agent first-run tour (localStorage, no URL param) */
(function (global) {
  // Bump the tour version whenever its steps change so existing users see new controls.
  const STORAGE_KEY = "molmind:agent_tour_v3";
  const PROMPT_TIP_KEY = "molmind:agent_upload_tip_v1";

  const PROMPT_EXAMPLES = [
    "生成 top10 候选清单 csv",
    "生成 top10 候选，并给出机制与验证方案 pdf",
    "只要机制与验证方案 pdf",
  ];

  function hasCompletedTour() {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function markTourDone() {
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch (_) {
      /* ignore */
    }
  }

  function hasShownUploadTip() {
    try {
      return sessionStorage.getItem(PROMPT_TIP_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function markUploadTipShown() {
    try {
      sessionStorage.setItem(PROMPT_TIP_KEY, "1");
    } catch (_) {
      /* ignore */
    }
  }

  function isTourTargetVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    if (el.hasAttribute("hidden") || el.classList.contains("hidden")) return false;
    if (el.getAttribute("aria-hidden") === "true") return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  /**
   * Agent-first path. Classic mode & demo-library top buttons are hidden;
   * classic lives under Profile, demo library under the upload popover.
   */
  function getSteps() {
    const steps = [
      {
        targetSelector: "#agentNewChatBtn",
        spotlightPadding: 8,
        cardPlacement: "bottom",
        title: "新对话",
        content: "清空当前对话并开启新一轮会话。不会删除历史记录，可随时从右侧历史找回。",
      },
      {
        targetSelector: "#mmProfileBanner",
        spotlightPadding: 8,
        cardPlacement: "bottom",
        title: "当前 Profile",
        content:
          "查看应用、研究方向与用户 ID。若需要表单式上传与结果页，可在弹窗里切换到「经典筛选」；日常产出更推荐留在 Agent 对话。",
      },
      {
        targetSelector: "#agentSettingsBtn",
        spotlightPadding: 8,
        cardPlacement: "bottom",
        title: "工具与插件",
        content: "查看内置 molmind-core，并从 Catalog 主动添加 OrigeneMCP / AuroBind 等旁证插件（默认关闭）。",
      },
      {
        targetSelector: "#agentHistoryBtn",
        spotlightPadding: 8,
        cardPlacement: "bottom",
        title: "对话历史",
        content: "打开历史抽屉，按时间回看会话、恢复消息与产物卡片。",
      },
      {
        targetSelector: "#agentUploadBtn",
        spotlightPadding: 10,
        cardPlacement: "top",
        title: "上传附件",
        content:
          "上传化合物库 .sdf，或附带 PDF / 图片 / 文档作参考。点这里也会打开样例库，可将内置 SDF 直接附加到会话。没有附件也能先对话问用法。",
      },
      {
        targetSelector: "#agentChatForm",
        spotlightPadding: 10,
        cardPlacement: "top",
        title: "输入与发送",
        content:
          "用自然语言描述目标即可，例如：\n· 生成 top10 候选清单 csv\n· 生成 top10 候选，并给出机制与验证方案 pdf\n· 只要机制与验证方案 pdf\n⌘/Ctrl+Enter 发送 · Enter 换行。",
      },
    ];
    return steps.filter((step) => {
      const el =
        typeof step.targetSelector === "string"
          ? document.querySelector(step.targetSelector)
          : null;
      return isTourTargetVisible(el);
    });
  }

  function placeCard(card, step, pad) {
    const target =
      typeof step.targetSelector === "string"
        ? document.querySelector(step.targetSelector)
        : null;
    if (!target) {
      card.style.top = "50%";
      card.style.left = "50%";
      card.style.transform = "translate(-50%, -50%)";
      return;
    }
    card.style.transform = "";
    const targetRect = target.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    const offset = step.cardOffset != null ? step.cardOffset : 16;
    const padding = pad != null ? pad : 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let placement = step.cardPlacement || "auto";
    if (placement === "auto") placement = vw < 768 ? "bottom" : "bottom";

    let top;
    let left;
    switch (placement) {
      case "top":
        top = targetRect.top - padding - offset - cardRect.height;
        left = targetRect.left + targetRect.width / 2 - cardRect.width / 2;
        break;
      case "right":
        top = targetRect.top + targetRect.height / 2 - cardRect.height / 2;
        left = targetRect.right + padding + offset;
        break;
      case "left":
        top = targetRect.top + targetRect.height / 2 - cardRect.height / 2;
        left = targetRect.left - padding - offset - cardRect.width;
        break;
      case "bottom":
      default:
        top = targetRect.bottom + padding + offset;
        left = targetRect.left + targetRect.width / 2 - cardRect.width / 2;
        break;
    }
    left = Math.max(12, Math.min(vw - cardRect.width - 12, left));
    top = Math.max(12, Math.min(vh - cardRect.height - 12, top));
    card.style.top = top + "px";
    card.style.left = left + "px";
  }

  function createCard() {
    const card = document.createElement("div");
    card.className = "tour-glass-card";
    card.style.position = "fixed";
    card.innerHTML = `
      <div class="tour-step-badge"></div>
      <h3 class="tour-card-title"></h3>
      <p class="tour-card-content"></p>
      <div class="tour-card-actions">
        <button type="button" class="tour-btn tour-btn-prev" title="上一步" aria-label="上一步">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <button type="button" class="tour-btn tour-btn-skip" title="跳过">跳过</button>
        <button type="button" class="tour-btn tour-btn-next" title="下一步" aria-label="下一步">
          <svg class="tour-next-arrow" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
          <svg class="tour-next-check hidden" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </button>
      </div>
    `;
    document.body.appendChild(card);
    return card;
  }

  const MolMindAgentTour = {
    STORAGE_KEY,
    PROMPT_EXAMPLES,
    hasCompletedTour,
    markTourDone,

    maybeStart() {
      if (hasCompletedTour()) return null;
      if (!global.MolMindTourGuide) return null;
      if (document.body.classList.contains("mm-body-agent") === false) return null;
      return this.start();
    },

    start() {
      const TourGuide = global.MolMindTourGuide;
      const steps = getSteps();
      if (!steps.length) {
        markTourDone();
        return null;
      }
      const card = createCard();
      const badge = card.querySelector(".tour-step-badge");
      const title = card.querySelector(".tour-card-title");
      const content = card.querySelector(".tour-card-content");
      const prevBtn = card.querySelector(".tour-btn-prev");
      const skipBtn = card.querySelector(".tour-btn-skip");
      const nextBtn = card.querySelector(".tour-btn-next");
      const nextArrow = card.querySelector(".tour-next-arrow");
      const nextCheck = card.querySelector(".tour-next-check");

      let tour = null;

      const reposition = () => {
        const step = tour && tour.getCurrentStep();
        if (!step) return;
        placeCard(card, step, step.spotlightPadding);
      };

      const renderStep = (step, index) => {
        badge.textContent = index + 1 + " / " + steps.length;
        title.textContent = step.title || "";
        content.textContent = step.content || "";
        prevBtn.style.visibility = index === 0 ? "hidden" : "visible";
        const last = index >= steps.length - 1;
        nextArrow.classList.toggle("hidden", last);
        nextCheck.classList.toggle("hidden", !last);
        requestAnimationFrame(() => {
          placeCard(card, step, step.spotlightPadding);
          // second pass after layout
          requestAnimationFrame(() => placeCard(card, step, step.spotlightPadding));
        });
      };

      const cleanupCard = () => {
        window.removeEventListener("resize", reposition);
        if (card.parentNode) card.parentNode.removeChild(card);
      };

      tour = new TourGuide(steps, {
        onStepChange(step, index) {
          renderStep(step, index);
        },
        onComplete() {
          markTourDone();
          cleanupCard();
        },
      });

      prevBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        tour.prev();
      });
      skipBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        tour.skip();
      });
      nextBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        tour.next();
      });
      window.addEventListener("resize", reposition);

      setTimeout(() => tour.start(0), 450);
      return tour;
    },

    /** After SDF upload: suggest prompt chips once per session. */
    showPromptSuggestions(container, { onPick } = {}) {
      if (!container || hasShownUploadTip()) return null;
      markUploadTipShown();
      const box = document.createElement("div");
      box.className = "mm-prompt-suggest";
      box.innerHTML = `<div class="mm-prompt-suggest-title">附件已就绪，可以这样说</div>`;
      const list = document.createElement("div");
      list.className = "mm-prompt-suggest-list";
      PROMPT_EXAMPLES.forEach((text) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "mm-prompt-chip";
        btn.textContent = text;
        btn.addEventListener("click", () => {
          if (onPick) onPick(text);
        });
        list.appendChild(btn);
      });
      box.appendChild(list);
      container.appendChild(box);
      return box;
    },
  };

  global.MolMindAgentTour = MolMindAgentTour;
})(window);
