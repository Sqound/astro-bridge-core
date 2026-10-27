/** Fetch-based client for native apps. Tokens stay in memory unless the app stores them. */
export class AstroBridgeError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.name = "AstroBridgeError";
    this.code = code;
    this.status = status;
  }
}

export class AstroBridgeClient {
  #token;
  constructor(baseURL, { token = "", timeoutMs = 60000, fetch: transport = globalThis.fetch } = {}) {
    const url = new URL(baseURL);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || !["", "/"].includes(url.pathname)) {
      throw new TypeError("Pass a bridge origin without a path, credentials, query or fragment.");
    }
    this.baseURL = url.origin;
    this.timeoutMs = timeoutMs;
    this.transport = transport;
    this.#token = token;
  }

  async request(method, path, data, { revision, authenticated = true } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers = { Accept: "application/json" };
    if (authenticated && this.#token) headers.Authorization = "Bearer " + this.#token;
    if (revision) headers["If-Match"] = '"' + revision + '"';
    if (data !== undefined) headers["Content-Type"] = "application/json";
    try {
      const response = await this.transport(this.baseURL + "/api/v1" + path, {
        method, headers, signal: controller.signal,
        credentials: "omit", redirect: "error", body: data === undefined ? undefined : JSON.stringify(data),
      });
      const body = await response.json();
      if (!response.ok) {
        if (response.status === 401 && authenticated) this.#token = "";
        throw new AstroBridgeError(body.error?.code || "REQUEST_FAILED", body.error?.message || "Request failed.", response.status);
      }
      return body;
    } catch (error) {
      if (error instanceof AstroBridgeError) throw error;
      throw new AstroBridgeError(error.name === "AbortError" ? "TIMEOUT" : "NETWORK_ERROR", "Bridge request failed. Check connectivity and reload status before retrying a write.");
    } finally {
      clearTimeout(timer);
    }
  }

  info() { return this.request("GET", "/info", undefined, { authenticated: false }); }
  async login(password) {
    const result = await this.request("POST", "/auth/token", { password }, { authenticated: false });
    this.#token = result.access_token;
    return result;
  }
  async logout() {
    try { return await this.request("DELETE", "/auth/token"); }
    finally { this.#token = ""; }
  }
  // status.management separates home/AP reachability from telescope association.
  // Preserve endpoint.management_endpoints: choose a reachable path explicitly;
  // endpoint.proxy is only the preferred current proxy, never a stable profile port.
  status() { return this.request("GET", "/status"); }
  config() { return this.request("GET", "/config"); }
  interfaces() { return this.request("GET", "/interfaces"); }
  networks(name) { return this.request("GET", "/wifi/networks?interface=" + encodeURIComponent(name)); }
  discover(mode = "wifi") { return this.request("POST", "/discovery", { mode }); }
  binding(id) { return this.request("GET", "/telescopes/" + encodeURIComponent(id) + "/binding"); }
  bind(id, observation, revision) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/binding", observation, { revision }); }
  unlink(id, revision) { return this.request("DELETE", "/telescopes/" + encodeURIComponent(id) + "/binding", {}, { revision }); }
  telescopes() { return this.request("GET", "/telescopes"); }
  telescope(id) { return this.request("GET", "/telescopes/" + encodeURIComponent(id)); }
  addTelescope(profile, revision) { return this.request("POST", "/telescopes", profile, { revision }); }
  editTelescope(id, patch, revision) { return this.request("PATCH", "/telescopes/" + encodeURIComponent(id), patch, { revision }); }
  deleteTelescope(id, revision) { return this.request("DELETE", "/telescopes/" + encodeURIComponent(id), undefined, { revision }); }
  connect(id, revision, assignment = {}) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/connect", assignment, { revision }); }
  ensure(id, revision) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/ensure", {}, { revision }); }
  protection() { return this.request("GET", "/protection"); }
  activity() { return this.request("GET", "/activity"); }
  reserve(id, revision, ttlSeconds = 120) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/leases", { ttl_seconds: ttlSeconds }, { revision }); }
  renewLease(id, version, ttlSeconds = 120) { return this.request("PATCH", "/leases/" + encodeURIComponent(id), { version, ttl_seconds: ttlSeconds }); }
  releaseLease(id, version) { return this.request("DELETE", "/leases/" + encodeURIComponent(id), { version }); }
  ownerReleaseLease(id, version, currentPassword, revision) { return this.request("POST", "/leases/" + encodeURIComponent(id) + "/release", { version, current_password: currentPassword, confirm_release: true }, { revision }); }
  ownerLock(id, locked, currentPassword, revision) { return this.request("PATCH", "/telescopes/" + encodeURIComponent(id) + "/owner-lock", { locked, current_password: currentPassword }, { revision }); }
  recoverConnectionPauses(currentPassword, revision) { return this.request("POST", "/system/connection-pauses/recover", { current_password: currentPassword, confirm_recovery: true }, { revision }); }
  operation(id) { return this.request("GET", "/operations/" + encodeURIComponent(id)); }
  disconnect(id, revision) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/disconnect", {}, { revision }); }
  home() { return this.request("GET", "/home"); }
  editHome(patch, revision) { return this.request("PATCH", "/home", patch, { revision }); }
  hotspot() { return this.request("GET", "/hotspot"); }
  editHotspot(enabled, revision) { return this.request("PATCH", "/hotspot", { enabled }, { revision }); }
  settings() { return this.request("GET", "/settings"); }
  editSettings(patch, revision) { return this.request("PATCH", "/settings", patch, { revision }); }
  async changePassword(target, currentPassword, password) {
    const result = await this.request("POST", "/credentials", { target, current_password: currentPassword, password });
    if (result.sessions_invalidated) this.#token = "";
    return result;
  }
  async revokeAll() {
    try { return await this.request("POST", "/auth/revoke-all", {}); }
    finally { this.#token = ""; }
  }
  reboot() { return this.request("POST", "/system/reboot", {}); }
}
