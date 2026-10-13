import test from "node:test";
import assert from "node:assert/strict";
import { RosemateBridge } from "../src/rosemate-bridge.js";

const BROKER = "https://broker.example.test";

class FakeTimers {
  constructor() {
    this.q = new Map();
    this.n = 0;
  }
  setTimeout(fn, ms) {
    this.q.set(++this.n, { fn, ms });
    return this.n;
  }
  clearTimeout(id) {
    this.q.delete(id);
  }
  pending() {
    return [...this.q.values()].map((t) => t.ms);
  }
  fire(ms) {
    for (const [id, t] of this.q) if (t.ms === ms) return this.q.delete(id), t.fn();
    throw new Error(`no timer ${ms}`);
  }
}

function rig({ session = { status: 200, body: { token: "integration-token", expires_at: 2000, features: { events: true, toy_actions: true, poll: false }, toy_consent: false } } } = {}) {
  const sockets = [];
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.sent = [];
      sockets.push(this);
    }
    send(m) {
      this.sent.push(m);
    }
    close(code) {
      this.closed = code;
    }
    // test helpers
    open() {
      this.onopen?.();
    }
    msg(o) {
      this.onmessage?.({ data: typeof o === "string" ? o : JSON.stringify(o) });
    }
    drop(code = 1006) {
      this.onclose?.({ code });
    }
  }
  const requests = [];
  const responses = { session, consent: { status: 200, body: { toy_consent: true } }, action: { status: 200, body: { ok: true, tool: "rosemate_toy_stop", delivery: "queued" } }, close: { status: 200, body: { closed: true } } };
  const fetch = async (url, init) => {
    const route = url.split("/").pop();
    requests.push({ url, route, body: JSON.parse(init.body), init });
    const r = responses[route];
    return { status: r.status, json: async () => r.body };
  };
  const timers = new FakeTimers();
  let t = 1000;
  const got = { commands: [], status: [], rejected: [] };
  const bridge = new RosemateBridge({
    brokerUrl: BROKER + "/",
    fetch,
    WebSocket: FakeWS,
    timers,
    now: () => t,
    onCommand: (c) => got.commands.push(c),
    onStatus: (s) => got.status.push(s),
    onRejected: (r) => got.rejected.push(r),
  });
  return { bridge, sockets, requests, responses, timers, got, setT: (v) => (t = v) };
}

const cmd = (id, extra = {}) => ({ type: "command", command: { id, type: "haptic_trigger", action: "pulse", intensity: 30, duration_sec: 2, timestamp: 999, ...extra } });

test("authorize: access key posted once, token sent as the first socket message, never in the URL", async () => {
  const { bridge, sockets, requests } = rig();
  const r = await bridge.authorize("the-access-key");
  assert.ok(r.ok);
  assert.equal(requests[0].url, `${BROKER}/rosemate/session`);
  assert.deepEqual(requests[0].body, { access_key: "the-access-key", toy_actions: false });
  assert.equal(requests[0].init.credentials, "omit");
  assert.equal(sockets.length, 1);
  assert.equal(sockets[0].url, "wss://broker.example.test/rosemate/events");
  assert.ok(!sockets[0].url.includes("token"));
  sockets[0].open();
  assert.deepEqual(JSON.parse(sockets[0].sent[0]), { token: "integration-token" });
  assert.equal(JSON.stringify(bridge).includes("the-access-key"), false); // not kept
});

test("authorize failure reports the broker's code, no socket", async () => {
  const { bridge, sockets, got } = rig({ session: { status: 401, body: { error: "invalid_access_key" } } });
  const r = await bridge.authorize("wrong");
  assert.deepEqual(r, { ok: false, error: "invalid_access_key" });
  assert.equal(sockets.length, 0);
  assert.equal(got.status.at(-1).state, "unauthorized");
});

test("commands: deduped by id, stale/malformed rejected, extra fields dropped", async () => {
  const { bridge, sockets, got } = rig();
  await bridge.authorize("k");
  const ws = sockets[0];
  ws.open();
  ws.msg({ type: "status", state: "connected", detail: "" });
  assert.equal(got.status.at(-1).state, "connected");
  ws.msg(cmd("c1", { reason: "ignore previous instructions", secret: "x" }));
  ws.msg(cmd("c1"));
  ws.msg(cmd("c2", { timestamp: 100 })); // stale
  ws.msg(cmd("c3", { action: "zap" }));
  ws.msg({ type: "rejected", reason: "malformed" });
  ws.msg("not json");
  assert.equal(got.commands.length, 1);
  assert.deepEqual(got.commands[0], { id: "c1", type: "haptic_trigger", timestamp: 999, action: "pulse", intensity: 30, duration_sec: 2 });
  assert.deepEqual(got.rejected, ["stale", "unknown_action", "malformed"]);
});

test("reconnects with backoff after drops and keeps dedupe across sockets", async () => {
  const { bridge, sockets, timers, got } = rig();
  await bridge.authorize("k");
  sockets[0].open();
  sockets[0].msg(cmd("c1"));
  sockets[0].drop(1006);
  assert.equal(got.status.at(-1).state, "reconnecting");
  assert.deepEqual(timers.pending(), [1000]);
  timers.fire(1000);
  assert.equal(sockets.length, 2);
  sockets[1].open();
  sockets[1].msg(cmd("c1")); // the mailbox re-delivered it after reconnect
  assert.equal(got.commands.length, 1);
  sockets[1].drop(1006);
  assert.deepEqual(timers.pending(), [2000]);
  timers.fire(2000);
  sockets[2].msg({ type: "status", state: "connected" });
  sockets[2].drop(1006);
  assert.deepEqual(timers.pending(), [1000]); // reset after a good connection
});

test("no reconnect when unauthorized or the session ended", async () => {
  const a = rig();
  await a.bridge.authorize("k");
  a.sockets[0].drop(4401);
  assert.equal(a.got.status.at(-1).state, "unauthorized");
  assert.deepEqual(a.timers.pending(), []);
  const b = rig();
  await b.bridge.authorize("k");
  b.sockets[0].msg({ type: "status", state: "closed", detail: "session_ended" });
  b.sockets[0].drop(1000);
  assert.deepEqual(b.timers.pending(), []);
  assert.equal(b.bridge.token, null);
});

test("received commands never produce an outbound request", async () => {
  const { bridge, sockets, requests } = rig();
  await bridge.authorize("k");
  sockets[0].open();
  for (let i = 0; i < 5; i++) sockets[0].msg(cmd(`c${i}`));
  sockets[0].msg({ type: "command", command: { id: "s1", type: "stop", action: "stop", timestamp: 999 } });
  assert.deepEqual(requests.map((r) => r.route), ["session"]);
});

test("toy actions go through the broker with the token; 401 drops the session", async () => {
  const { bridge, requests, responses, got } = rig();
  await bridge.authorize("k");
  assert.deepEqual(await bridge.stopToy(), { ok: true, tool: "rosemate_toy_stop", delivery: "queued" });
  assert.deepEqual(requests.at(-1).body, { token: "integration-token", tool: "rosemate_toy_stop", arguments: {} });
  responses.action = { status: 403, body: { error: "consent_required" } };
  assert.deepEqual(await bridge.toyAction("rosemate_haptic_trigger", { action: "pulse", intensity: 10, duration_sec: 1 }), { ok: false, error: "consent_required" });
  responses.action = { status: 504, body: { error: "toy_timeout", outcome: "unknown" } };
  assert.deepEqual(await bridge.stopToy(), { ok: false, error: "toy_timeout", outcome: "unknown" });
  responses.action = { status: 401, body: { error: "invalid_token" } };
  await bridge.stopToy();
  assert.equal(bridge.token, null);
  assert.equal(got.status.at(-1).state, "unauthorized");
  assert.deepEqual(await bridge.stopToy(), { ok: false, error: "not_authorized" });
});

test("consent toggles through the broker", async () => {
  const { bridge, requests } = rig();
  assert.equal((await bridge.setConsent(true)).error, "not_authorized");
  await bridge.authorize("k");
  assert.deepEqual(await bridge.setConsent(true), { ok: true, toyConsent: true });
  assert.deepEqual(requests.at(-1).body, { token: "integration-token", toy_actions: true });
});

test("dispose closes the socket, cancels reconnects, revokes the session, is idempotent", async () => {
  const { bridge, sockets, timers, requests, got } = rig();
  await bridge.authorize("k");
  sockets[0].drop(1006);
  assert.equal(timers.pending().length, 1);
  timers.fire(1000);
  bridge.dispose({ keepalive: true });
  bridge.dispose();
  assert.equal(sockets[1].closed, 1000);
  assert.deepEqual(timers.pending(), []);
  const closes = requests.filter((r) => r.route === "close");
  assert.equal(closes.length, 1);
  assert.equal(closes[0].init.keepalive, true);
  assert.equal(got.status.at(-1).state, "off");
  sockets[1].msg(cmd("late"));
  assert.equal(got.commands.length, 0);
  assert.equal((await bridge.authorize("k")).error, "disposed");
});

test("an expired session does not reconnect", async () => {
  const { bridge, sockets, timers, setT, got } = rig();
  await bridge.authorize("k");
  sockets[0].drop(1006);
  setT(2001);
  timers.fire(1000);
  assert.equal(sockets.length, 1);
  assert.equal(got.status.at(-1).detail, "session_expired");
});
