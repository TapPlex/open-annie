import { SeenIds, checkCommand } from "./actions.js";

// Browser side of the optional Rosemate integration (docs/rosemate.md). Talks only to the
// open-annie broker, never to Rosemate: the broker holds the Rosemate MCP credential,
// subscribes to Rosemate's command stream and forwards sanitized commands here.
//
// Flow: authorize(accessKey) -> POST {broker}/rosemate/session -> short-lived integration
// token (kept in memory only) -> WS {broker}/rosemate/events, token sent as the first
// message (never in the URL). Commands are de-duplicated by id and checked for age again
// here, then handed to onCommand. Nothing received is ever sent back upstream: toyAction()
// is only called from a user's click.
//
// Status (onStatus): {state: "off"|"authorizing"|"connecting"|"connected"|"reconnecting"|
// "unauthorized"|"error"|"closed", detail, upstream?} with upstream the broker's view of the
// Rosemate stream (idle|connecting|connected|reconnecting|closed).

const BACKOFF_S = [1, 2, 5, 10, 30];
const REQUEST_TIMEOUT_S = 15;

export class RosemateBridge {
  constructor({ brokerUrl, onCommand, onStatus, onRejected, fetch = globalThis.fetch?.bind(globalThis), WebSocket = globalThis.WebSocket, timers = globalThis, now = () => Date.now() / 1000 }) {
    Object.assign(this, { onCommand, onStatus, onRejected, fetch, WebSocket, timers, now });
    this.base = String(brokerUrl).replace(/\/+$/, "");
    this.token = null;
    this.expiresAt = 0;
    this.features = { events: false, toy_actions: false, poll: false };
    this.toyConsent = false;
    this.ws = null;
    this.retry = 0;
    this.retryTimer = null;
    this.disposed = false;
    this.seen = new SeenIds({ now, ttlS: 600, max: 512 });
    this.state = "off";
  }

  #status(state, detail = "", upstream) {
    this.state = state;
    this.onStatus?.({ state, detail, ...(upstream ? { upstream } : {}) });
  }

  async #post(route, body) {
    const ctl = new AbortController();
    const timer = this.timers.setTimeout(() => ctl.abort(), REQUEST_TIMEOUT_S * 1000);
    try {
      const r = await this.fetch(`${this.base}/rosemate/${route}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: ctl.signal,
        credentials: "omit",
      });
      let data = {};
      try {
        data = await r.json();
      } catch {}
      return { status: r.status, data: data && typeof data === "object" ? data : {} };
    } catch (e) {
      return { status: 0, data: { error: e?.name === "AbortError" ? "timeout" : "network_error" } };
    } finally {
      this.timers.clearTimeout(timer);
    }
  }

  // The operator's access key, typed by the user; it is sent once and not stored.
  async authorize(accessKey, { toyActions = false } = {}) {
    if (this.disposed) return { ok: false, error: "disposed" };
    this.#status("authorizing");
    const { status, data } = await this.#post("session", { access_key: String(accessKey ?? ""), toy_actions: !!toyActions });
    if (status !== 200 || typeof data.token !== "string") {
      const error = typeof data.error === "string" ? data.error : `http_${status}`;
      this.#status(status === 401 || status === 403 ? "unauthorized" : "error", error);
      return { ok: false, error };
    }
    this.token = data.token;
    this.expiresAt = Number(data.expires_at) || 0;
    this.features = { ...this.features, ...(data.features ?? {}) };
    this.toyConsent = !!data.toy_consent;
    this.connect();
    return { ok: true, features: this.features, toyConsent: this.toyConsent, expiresAt: this.expiresAt };
  }

  connect() {
    if (this.disposed || !this.token || this.ws) return;
    if (this.expiresAt && this.now() >= this.expiresAt) return this.#expired();
    this.#status(this.retry ? "reconnecting" : "connecting");
    const url = this.base.replace(/^http/, "ws") + "/rosemate/events";
    let ws;
    try {
      ws = this.ws = new this.WebSocket(url);
    } catch {
      this.ws = null;
      return this.#schedule("socket_failed");
    }
    ws.onopen = () => ws.send(JSON.stringify({ token: this.token }));
    ws.onmessage = (e) => {
      if (this.ws !== ws || typeof e.data !== "string" || e.data.length > 16384) return;
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      this.#message(msg);
    };
    ws.onclose = (e) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.disposed) return;
      if (e?.code === 4401 || e?.code === 1008) {
        this.token = null;
        return this.#status("unauthorized", e.code === 1008 ? "origin_or_disabled" : "invalid_token");
      }
      // fal and proxies cut long sockets; the broker's stream also restarts. Reconnect.
      this.#schedule(e?.code === 4429 ? "at_capacity" : "disconnected");
    };
    ws.onerror = () => {};
  }

  #lost(status) {
    if (status !== 401) return;
    this.token = null;
    this.timers.clearTimeout(this.retryTimer);
    try {
      this.ws?.close(1000);
    } catch {}
    this.ws = null;
    this.#status("unauthorized", "invalid_token");
  }

  #expired() {
    this.token = null;
    this.#status("closed", "session_expired");
  }

  #schedule(detail) {
    if (this.disposed || !this.token) return;
    const delay = BACKOFF_S[Math.min(this.retry, BACKOFF_S.length - 1)];
    this.retry++;
    this.#status("reconnecting", detail);
    this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay * 1000);
  }

  #message(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "status") {
      if (msg.state === "closed" && msg.detail === "session_ended") {
        this.token = null;
        this.#status("closed", "session_ended");
        return;
      }
      if (msg.state === "connected") this.retry = 0;
      this.#status("connected", "", typeof msg.state === "string" ? msg.state : undefined);
    } else if (msg.type === "command") {
      const cmd = msg.command;
      const c = checkCommand(cmd, this.now());
      if (!c.ok) return this.onRejected?.(c.error);
      if (!this.seen.add(cmd.id)) return; // duplicate (reconnect, transport switch)
      const clean = { id: cmd.id, type: cmd.type, timestamp: cmd.timestamp, action: cmd.type === "stop" ? "stop" : cmd.action };
      for (const k of ["intensity", "duration_sec", "pattern"]) if (cmd[k] != null) clean[k] = cmd[k];
      this.onCommand?.(clean);
    } else if (msg.type === "rejected") {
      this.onRejected?.(typeof msg.reason === "string" ? msg.reason.slice(0, 40) : "rejected");
    } else if (msg.type === "error") {
      this.#status("error", typeof msg.error === "string" ? msg.error.slice(0, 40) : "error");
    }
  }

  async setConsent(toyActions) {
    if (!this.token) return { ok: false, error: "not_authorized" };
    const { status, data } = await this.#post("consent", { token: this.token, toy_actions: !!toyActions });
    this.#lost(status);
    if (status !== 200) return { ok: false, error: data.error ?? `http_${status}` };
    this.toyConsent = !!data.toy_consent;
    return { ok: true, toyConsent: this.toyConsent };
  }

  // Outbound only, from a user's action. Success means the Rosemate MCP queue accepted
  // it ("queued"), not that the toy moved: the Rosemate PWA does the hardware work.
  async toyAction(tool, args = {}) {
    if (!this.token) return { ok: false, error: "not_authorized" };
    const { status, data } = await this.#post("action", { token: this.token, tool, arguments: args });
    this.#lost(status);
    if (status !== 200) return { ok: false, error: data.error ?? `http_${status}`, ...(data.outcome ? { outcome: data.outcome } : {}) };
    return { ok: true, tool: data.tool, delivery: data.delivery, ...(data.text ? { text: data.text } : {}) };
  }

  stopToy() {
    return this.toyAction("rosemate_toy_stop", {});
  }

  // Closes the socket and revokes the session (best effort). Safe to call twice.
  dispose({ keepalive = false } = {}) {
    if (this.disposed) return;
    this.disposed = true;
    this.timers.clearTimeout(this.retryTimer);
    const ws = this.ws;
    this.ws = null;
    try {
      ws?.close(1000);
    } catch {}
    if (this.token) {
      const body = JSON.stringify({ token: this.token });
      this.token = null;
      try {
        this.fetch(`${this.base}/rosemate/close`, { method: "POST", headers: { "content-type": "application/json" }, body, keepalive, credentials: "omit" })?.catch?.(() => {});
      } catch {}
    }
    this.#status("off");
  }
}
