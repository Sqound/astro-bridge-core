/** Fetch-based client for native apps. Tokens stay in memory unless the app stores them. */
// Frozen v1.0: consumers tolerate additive response fields, enum values and event types.
// Compare this major version with info().contract_version after discovery.
export const NATIVE_CONTRACT_VERSION = 1;

export class AstroBridgeError extends Error {
  constructor(code, message, status = 0, details = {}) {
    super(message);
    this.name = "AstroBridgeError";
    this.code = code;
    this.status = status;
    this.details = details;
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

  async request(method, path, data, { revision, authenticated = true, stream = false } = {}) {
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
      if (stream && response.ok) return response;
      const body = await response.json();
      if (!response.ok) {
        if (response.status === 401 && authenticated) this.#token = "";
        throw new AstroBridgeError(body.error?.code || "REQUEST_FAILED", body.error?.message || "Request failed.", response.status, body.error || {});
      }
      return body;
    } catch (error) {
      if (error instanceof AstroBridgeError) throw error;
      throw new AstroBridgeError(error.name === "AbortError" ? "TIMEOUT" : "NETWORK_ERROR", "Bridge request failed. Check connectivity and reload status before retrying a write.");
    } finally {
      clearTimeout(timer);
    }
  }

  // Public identity: compare device_id with DNS-SD id before pairing.
  // capabilities is the currently enabled native set; legacy flags use legacy_capabilities.
  // request_path "home" covers owned Ethernet and private Wi-Fi; use returned endpoints.
  info() { return this.request("GET", "/info", undefined, { authenticated: false }); }
  async login(password) {
    const result = await this.request("POST", "/auth/token", { password }, { authenticated: false });
    this.#token = result.access_token;
    return result;
  }
  clients() { return this.request("GET", "/auth/clients"); }
  revokeClient(clientId, revision) {
    return this.request("DELETE", "/auth/clients/" + encodeURIComponent(clientId), undefined, { revision });
  }
  // Pairing never retains the administrator credential or token after registration.
  async pair(password, name, scopes = ["read", "connect", "telemetry", "camera"]) {
    try {
      await this.login(password);
      return await this.request("POST", "/auth/clients", { name, scopes });
    } finally { this.#token = ""; }
  }
  async loginClient(clientId, clientSecret) {
    this.#token = "";
    const result = await this.request("POST", "/auth/token", { client_id: clientId, client_secret: clientSecret }, { authenticated: false });
    this.#token = result.access_token;
    return result;
  }
  instruments({ after } = {}) {
    return this.request("GET", "/instruments" + (after === undefined ? "" : "?" + new URLSearchParams({ after })));
  }
  // Membership changes restart the traversal; at most 3 attempts and 2 pages each.
  async allInstruments() {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        let result = await this.instruments();
        const instruments = [...result.instruments];
        if (result.has_more) {
          if (typeof result.next_cursor !== "string") throw new Error("Invalid instrument page");
          const next = await this.instruments({ after: result.next_cursor });
          if (next.has_more || next.boot_id !== result.boot_id) throw new AstroBridgeError("CATALOG_CHANGED", "Instrument catalog changed. Restart listing instruments.", 409);
          instruments.push(...next.instruments);
          result = next;
        }
        if (instruments.length > 32 || new Set(instruments.map(row => row.id)).size !== instruments.length) throw new Error("Invalid instrument page");
        return { ...result, instruments, next_cursor: null, has_more: false };
      } catch (error) {
        if (error.code !== "CATALOG_CHANGED" || attempt === 2) throw error;
      }
    }
  }
  instrument(id) { return this.request("GET", "/instruments/" + encodeURIComponent(id)); }
  connect(id, revision, options = {}) {
    return this.request("POST", "/instruments/" + encodeURIComponent(id) + "/connect", options, { revision });
  }
  // Cameras return a bound Alpaca endpoint. Native leases reserve bridge access;
  // Alpaca ClientID/source ownership stays independent. Disconnect releases this
  // native reservation only and never aborts an exposure or switches Wi-Fi.
  releaseEndpointOwner(id, revision) {
    return this.request("POST", "/endpoints/" + encodeURIComponent(id) + "/release-owner", {}, { revision });
  }
  endpoint(id) { return this.request("GET", "/instruments/" + encodeURIComponent(id) + "/endpoint"); }
  // Current inventory; singular endpoint() above belongs to the instrument facade.
  // Isolated UDP paths repeat the shared profile refused_clients datagram count; do not sum paths.
  endpoints(id) { return this.request("GET", "/endpoints" + (id === undefined ? "" : "/" + encodeURIComponent(id))); }
  // Native reservations belong to the paired client, survive token refresh and last 60–3600 seconds.
  // Contract 1.1: reserveSeconds (60–50400) sets a night reservation horizon; omitted, a renewal never shortens one.
  renewLease(id, leaseId, leaseSeconds = 900, reserveSeconds) {
    const body = { lease_id: leaseId, lease_seconds: leaseSeconds };
    if (reserveSeconds !== undefined) body.reserve_seconds = reserveSeconds;
    return this.request("POST", "/instruments/" + encodeURIComponent(id) + "/lease", body);
  }
  release(id, leaseId) {
    return this.request("POST", "/instruments/" + encodeURIComponent(id) + "/release", { lease_id: leaseId });
  }
  disconnect(id, leaseId) {
    return this.request("POST", "/instruments/" + encodeURIComponent(id) + "/disconnect", leaseId === undefined ? {} : { lease_id: leaseId });
  }
  // Current short polling includes profile_id="hotspot" management events.
  // hotspot_restored confirms AP access only, independently of instrument readiness.
  events({ after, bootId, limit = 100 } = {}) {
    const query = new URLSearchParams();
    if (after !== undefined) query.set("after", after);
    if (bootId !== undefined) query.set("boot_id", bootId);
    query.set("limit", limit);
    return this.request("GET", "/events?" + query);
  }
  // Empty timeout pages are normal; reset_reason requires a fresh instrument catalog.
  // service_changed.outcome includes ready, not_answering and unknown.
  waitEvents({ after, bootId, limit = 100, wait = 25 } = {}) {
    const query = new URLSearchParams();
    if (after !== undefined) query.set("after", after);
    if (bootId !== undefined) query.set("boot_id", bootId);
    query.set("limit", limit); query.set("wait", wait);
    return this.request("GET", "/events?" + query);
  }
  // adapter_status contains sanitized adapter lifecycle/counters, never unreviewed event names.
  // card_data contains per-metric source ages, independent of journal batching.
  telemetry() { return this.request("GET", "/telemetry"); }
  // Preserve other adapters when toggling seestar_events_v1; a supplied key enables
  // only profiles without explicit selections. adapter_yielded/refused are events.
  // Optional profiles[id].alpaca_interval_seconds is 10–120 seconds (default 15).
  updateTelemetrySettings(settings, revision) { return this.request("PATCH", "/telemetry/settings", settings, { revision }); }
  sessions({ profileId, after, limit = 100 } = {}) {
    const query = new URLSearchParams({ limit });
    if (profileId !== undefined) query.set("profile_id", profileId);
    if (after !== undefined) query.set("after", after);
    return this.request("GET", "/telemetry/sessions?" + query);
  }
  // Link byte metrics are interval deltas; preserve normalized boolean/text values.
  samples({ sessionId, after, bootId, limit = 100 } = {}) {
    const query = new URLSearchParams({ session_id: sessionId, limit });
    if (after !== undefined) query.set("after", after);
    if (bootId !== undefined) query.set("boot_id", bootId);
    return this.request("GET", "/telemetry/samples?" + query);
  }
  // Full retained-session buckets; derived reads never imply raw-sample fetches.
  // Honor limited/history_available and reader_coverage.unknown_before_seq.
  sessionAnalysis(id) { return this.request("GET", "/telemetry/sessions/" + encodeURIComponent(id) + "/analysis"); }
  // The caller consumes response.body incrementally; exports are not buffered here.
  exportSession(id, format = "json") {
    return this.request("GET", "/telemetry/sessions/" + encodeURIComponent(id) + "/export?" + new URLSearchParams({ format }), undefined, { stream: true });
  }
  async logout() {
    try { return await this.request("DELETE", "/auth/token"); }
    finally { this.#token = ""; }
  }
  // status.management separates home/AP reachability from telescope association.
  // Optional path.kind is wifi or ethernet; preserve absence for older bridges.
  // Preserve endpoint.management_endpoints: choose a reachable path explicitly;
  // endpoint.proxy is only the preferred current proxy, never a stable profile port.
  // connection.service reports a bounded service check independently of relay_ready.
  // Check service.state before claiming that the instrument answers; status() never probes.
  status() { return this.request("GET", "/status"); }
  // Manifest observation defaults describe capabilities; they do not enable a profile.
  plugins() { return this.request("GET", "/plugins"); }
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
  connectTelescope(id, revision, assignment = {}) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/connect", assignment, { revision }); }
  ensure(id, revision) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/ensure", {}, { revision }); }
  protection() { return this.request("GET", "/protection"); }
  activity() { return this.request("GET", "/activity"); }
  reserve(id, revision, ttlSeconds = 120) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/leases", { ttl_seconds: ttlSeconds }, { revision }); }
  renewReservation(id, version, ttlSeconds = 120) { return this.request("PATCH", "/leases/" + encodeURIComponent(id), { version, ttl_seconds: ttlSeconds }); }
  releaseLease(id, version) { return this.request("DELETE", "/leases/" + encodeURIComponent(id), { version }); }
  ownerReleaseLease(id, version, currentPassword, revision) { return this.request("POST", "/leases/" + encodeURIComponent(id) + "/release", { version, current_password: currentPassword, confirm_release: true }, { revision }); }
  ownerLock(id, locked, currentPassword, revision) { return this.request("PATCH", "/telescopes/" + encodeURIComponent(id) + "/owner-lock", { locked, current_password: currentPassword }, { revision }); }
  recoverConnectionPauses(currentPassword, revision) { return this.request("POST", "/system/connection-pauses/recover", { current_password: currentPassword, confirm_recovery: true }, { revision }); }
  operation(id) { return this.request("GET", "/operations/" + encodeURIComponent(id)); }
  disconnectTelescope(id, revision) { return this.request("POST", "/telescopes/" + encodeURIComponent(id) + "/disconnect", {}, { revision }); }
  home() { return this.request("GET", "/home"); }
  editHome(patch, revision) { return this.request("PATCH", "/home", patch, { revision }); }
  hotspot() { return this.request("GET", "/hotspot"); }
  editHotspot(enabled, revision) { return this.request("PATCH", "/hotspot", { enabled }, { revision }); }
  settings() { return this.request("GET", "/settings"); }
  // auto_policy.protect_running_operations is opt-in; enabling requires acknowledge_interruption.
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
  makerKeys() { return this.request("GET", "/maker-keys"); }
  fetchMakerKey(family) { return this.request("POST", "/maker-keys/" + encodeURIComponent(family) + "/fetch", {}); }
  importMakerKey(family, pem) { return this.request("PUT", "/maker-keys/" + encodeURIComponent(family), { pem }); }
  removeMakerKey(family, revision) { return this.request("DELETE", "/maker-keys/" + encodeURIComponent(family), undefined, { revision }); }
  makerKeyOperation(id) { return this.request("GET", "/maker-keys/operations/" + encodeURIComponent(id)); }
  cameras() { return this.request("GET", "/cameras"); }
  releaseCameraOwner(id, ownerGeneration, revision) { return this.request("POST", "/cameras/" + encodeURIComponent(id) + "/release-owner", {confirm_release:true,owner_generation:ownerGeneration}, {revision}); }
  plugDemoCamera(plugged, revision) { return this.request("POST", "/cameras/demo", {plugged}, {revision}); }
  cameraSettings() { return this.request("GET", "/cameras/settings"); }
  // Optional indi selects the packaged service for eligible attached cameras.
  editCameraSettings(patch, revision) { return this.request("PATCH", "/cameras/settings", patch, { revision }); }
  systemMetrics() { return this.request("GET", "/system/metrics"); }
  systemTime() { return this.request("GET", "/system/time"); }
  timeHint(utcIso, uncertaintyMs) { return this.request("POST", "/system/time-hint", { utc_iso: utcIso, uncertainty_ms: uncertaintyMs }); }
}
