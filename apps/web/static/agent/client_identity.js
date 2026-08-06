/* MolMind Agent — persistent per-browser identity and API isolation */
(function (global) {
  const STORAGE_KEY = "molmind_agent_client_id_v1";
  const HEADER_NAME = "X-MolMind-Client-ID";
  const VALID_ID = /^[A-Za-z0-9_-]{16,128}$/;

  function generateId() {
    if (global.crypto && typeof global.crypto.randomUUID === "function") {
      return `browser_${global.crypto.randomUUID().replace(/-/g, "")}`;
    }
    const bytes = new Uint8Array(24);
    if (global.crypto && typeof global.crypto.getRandomValues === "function") {
      global.crypto.getRandomValues(bytes);
      return `browser_${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
    }
    return `browser_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}_${Math.random().toString(36).slice(2)}`;
  }

  function loadOrCreateId() {
    try {
      const stored = global.localStorage.getItem(STORAGE_KEY);
      if (VALID_ID.test(stored || "")) return stored;
      const created = generateId();
      global.localStorage.setItem(STORAGE_KEY, created);
      return created;
    } catch (_error) {
      // Privacy modes may disable localStorage. Keep this tab isolated for the
      // current page lifetime instead of falling back to a shared server id.
      return generateId();
    }
  }

  const clientId = loadOrCreateId();
  const nativeFetch = global.fetch.bind(global);

  function isAgentApi(input) {
    try {
      const raw = typeof input === "string" || input instanceof URL ? input : input.url;
      const url = new URL(raw, global.location.href);
      return url.origin === global.location.origin && url.pathname.startsWith("/api/agent/");
    } catch (_error) {
      return false;
    }
  }

  global.fetch = function molMindOwnedFetch(input, init) {
    if (!isAgentApi(input)) return nativeFetch(input, init);
    const options = { ...(init || {}) };
    const sourceHeaders =
      input instanceof Request && !options.headers ? input.headers : options.headers;
    const headers = new Headers(sourceHeaders || {});
    headers.set(HEADER_NAME, clientId);
    options.headers = headers;
    return nativeFetch(input, options);
  };

  function decorateDownloadUrl(value) {
    if (!value) return value;
    try {
      const url = new URL(value, global.location.href);
      if (url.origin !== global.location.origin || !url.pathname.startsWith("/api/agent/")) {
        return value;
      }
      url.searchParams.set("client_id", clientId);
      return `${url.pathname}${url.search}${url.hash}`;
    } catch (_error) {
      return value;
    }
  }

  function headers() {
    return { [HEADER_NAME]: clientId };
  }

  function switchClientId(value, latestSessionId) {
    const target = String(value || "").trim();
    if (!VALID_ID.test(target)) throw new Error("用户 ID 格式无效");
    global.localStorage.setItem(STORAGE_KEY, target);
    global.localStorage.removeItem("molmind:agent_active_session_v1");
    global.localStorage.removeItem("molmind:agent_installed_catalog_v1");
    if (latestSessionId) {
      global.localStorage.setItem(
        `molmind:agent_active_session_v1:${target}`,
        String(latestSessionId)
      );
    }
  }

  global.MolMindClientIdentity = Object.freeze({
    storageKey: STORAGE_KEY,
    clientId,
    headers,
    decorateDownloadUrl,
    switchClientId,
  });

  // Register the installation independently of conversations so its ID can
  // be restored in another browser even before the first chat is created.
  global.fetch("/api/agent/clients/register", { method: "POST" }).catch(() => {
    /* A temporary network failure must not block the page. */
  });
})(window);
