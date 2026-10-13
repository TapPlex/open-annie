// node --test packages/core/test/ : pure-module tests, mocks only (no network, audio or three.js).
import test from "node:test";
import assert from "node:assert/strict";
import { validateAction, validateTriggerMap, checkCommand, SeenIds, ActionDispatcher, PositionMover, leaseFaces, DEFAULT_TRIGGER_MAP, TALK_MAX } from "../src/actions.js";

test("talk: type, trimming, length", () => {
  assert.deepEqual(validateAction({ type: "talk", message: "  hi\nthere " }), { ok: true, action: { type: "talk", message: "hi there" } });
  assert.equal(validateAction({ type: "talk", message: 5 }).error, "message_type");
  assert.equal(validateAction({ type: "talk", message: "   " }).error, "message_length");
  assert.equal(validateAction({ type: "talk", message: "x".repeat(TALK_MAX + 1) }).error, "message_length");
  assert.equal(validateAction({ type: "talk", message: "hi", run: "alert(1)" }).error, "unknown_field");
});

test("emotion: face allowlist and bounds", () => {
  assert.deepEqual(validateAction({ type: "emotion", name: "happy" }).action, { type: "emotion", name: "happy", intensity: 0.8, hold_s: 3 });
  assert.equal(validateAction({ type: "emotion", name: "toString" }).error, "unknown_emotion");
  assert.equal(validateAction({ type: "emotion", name: "ecstatic" }).error, "unknown_emotion");
  assert.equal(validateAction({ type: "emotion", name: "sad", intensity: 1.5 }).error, "intensity");
  assert.equal(validateAction({ type: "emotion", name: "sad", hold_s: 60 }).error, "hold_s");
});

test("position: finite and inside scene bounds", () => {
  assert.ok(validateAction({ type: "position", x: 0.1, y: 0, z: -0.2 }).ok);
  for (const bad of [NaN, Infinity, "0.1", 2, -1]) assert.equal(validateAction({ type: "position", x: bad, y: 0, z: 0 }).ok, false);
  assert.equal(validateAction({ type: "position", x: 0, y: 0.5, z: 0 }).error, "y_out_of_bounds");
  assert.equal(validateAction({ type: "position", x: 0, y: 0, z: 0, duration_s: 99 }).error, "duration_s");
  assert.equal(validateAction({ type: "position", x: 0, y: 0, z: 0, hold_s: -1 }).error, "hold_s");
});

test("toy_action: strict tool allowlist", () => {
  assert.ok(validateAction({ type: "toy_action", tool: "rosemate_toy_stop" }).ok);
  assert.ok(validateAction({ type: "toy_action", tool: "rosemate_haptic_trigger", arguments: { action: "pulse", intensity: 20, duration_sec: 2 } }).ok);
  assert.equal(validateAction({ type: "toy_action", tool: "talk" }).error, "unknown_tool");
  assert.equal(validateAction({ type: "toy_action", tool: "rosemate_haptic_trigger", arguments: { action: "zap", intensity: 1, duration_sec: 1 } }).error, "toy_action");
  assert.equal(validateAction({ type: "toy_action", tool: "rosemate_haptic_trigger", arguments: { action: "pulse", intensity: 20.5, duration_sec: 2 } }).error, "intensity");
  assert.equal(validateAction({ type: "nope" }).error, "unknown_type");
  assert.equal(validateAction(null).error, "not_an_object");
});

test("trigger maps: default is valid, toy_action is refused", () => {
  assert.ok(validateTriggerMap(DEFAULT_TRIGGER_MAP).ok);
  const echo = { haptic_trigger: { vibrate: [{ type: "toy_action", tool: "rosemate_haptic_trigger", arguments: { action: "vibrate", intensity: 10, duration_sec: 1 } }] } };
  assert.match(validateTriggerMap(echo).error, /toy_action not allowed/);
  assert.equal(validateTriggerMap({ haptic_trigger: { zap: [] } }).ok, false);
  assert.equal(validateTriggerMap({ haptic_trigger: { vibrate: [{ type: "talk", message: 1 }] } }).ok, false);
  assert.equal(validateTriggerMap([]).ok, false);
  assert.throws(() => new ActionDispatcher({ map: echo }));
});

test("checkCommand: malformed, unknown and stale commands", () => {
  const now = 1000;
  assert.ok(checkCommand({ id: "a1", type: "haptic_trigger", action: "pulse", timestamp: 999 }, now).ok);
  assert.ok(checkCommand({ id: "a2", type: "stop", timestamp: 999 }, now).ok);
  assert.equal(checkCommand({ id: "a3", type: "haptic_trigger", action: "pulse", timestamp: 900 }, now).error, "stale");
  assert.equal(checkCommand({ id: "a4", type: "haptic_trigger", action: "zap", timestamp: 999 }, now).error, "unknown_action");
  assert.equal(checkCommand({ id: "a5", type: "eval", timestamp: 999 }, now).error, "unknown_type");
  assert.equal(checkCommand({ id: "<x>", type: "stop", timestamp: 999 }, now).error, "malformed");
  assert.equal(checkCommand({ id: "a6", type: "stop", timestamp: 5000 }, now).error, "malformed");
});

test("SeenIds: dedupe with TTL and size bound", () => {
  let t = 0;
  const s = new SeenIds({ now: () => t, ttlS: 10, max: 3 });
  assert.ok(s.add("a"));
  assert.equal(s.add("a"), false);
  s.add("b"), s.add("c"), s.add("d");
  assert.ok(s.ids.size <= 3);
  t = 20;
  assert.ok(s.add("a")); // expired
});

function harness(over = {}) {
  const calls = [];
  const h = {
    talk: async (a) => (calls.push(["talk", a.message]), { ok: true }),
    emotion: async (a) => (calls.push(["emotion", a.name]), { ok: true }),
    position: async (a) => (calls.push(["position", a.x, a.z]), { ok: true }),
    toy_action: async (a) => (calls.push(["toy", a.tool]), { ok: true }),
    cancel: () => calls.push(["cancel"]),
    ...over,
  };
  return { calls, d: new ActionDispatcher({ handlers: h }) };
}

test("dispatcher: trigger drives talk, emotion and position, never a toy action", async () => {
  const { calls, d } = harness();
  const r = await d.trigger({ id: "1", type: "haptic_trigger", action: "vibrate", timestamp: 0 });
  assert.ok(r.every((x) => x.ok));
  assert.deepEqual(calls.map((c) => c[0]).sort(), ["emotion", "position", "talk"]);
  assert.ok(!calls.some((c) => c[0] === "toy"));
  // A trigger-sourced toy_action is refused even if someone tries to dispatch one directly.
  assert.equal((await d.dispatch({ type: "toy_action", tool: "rosemate_toy_stop" }, { source: "trigger" })).error, "toy_action_requires_user");
  assert.ok(!calls.some((c) => c[0] === "toy"));
  assert.ok((await d.dispatch({ type: "toy_action", tool: "rosemate_toy_stop" })).ok);
});

test("dispatcher: stop cancels first, then settles her home", async () => {
  const { calls, d } = harness();
  await d.trigger({ id: "2", type: "stop", action: "stop", timestamp: 0 });
  assert.deepEqual(calls[0], ["cancel"]);
  assert.deepEqual(calls.slice(1).map((c) => c[0]).sort(), ["emotion", "position"]);
  assert.deepEqual(calls.find((c) => c[0] === "position").slice(1), [0, 0]);
});

test("dispatcher: invalid actions, missing and throwing handlers are reported, not thrown", async () => {
  const results = [];
  const d = new ActionDispatcher({ handlers: { talk: () => { throw new Error("boom"); } }, onResult: (r) => results.push(r) });
  assert.equal((await d.dispatch({ type: "talk", message: "hi" })).error, "boom");
  assert.equal((await d.dispatch({ type: "emotion", name: "happy" })).error, "emotion_unavailable");
  assert.equal((await d.dispatch({ type: "position", x: 9, y: 0, z: 0 })).error, "x_out_of_bounds");
  assert.equal(results.length, 3);
  assert.ok(d.setMap({ haptic_trigger: { default: [{ type: "talk", message: "ok" }] } }).ok);
  assert.equal(d.setMap({ haptic_trigger: { pulse: [{ type: "toy_action", tool: "rosemate_toy_stop" }] } }).ok, false);
  assert.ok(d.map.haptic_trigger.default); // a rejected map keeps the old one
});

test("PositionMover: interpolates the root, holds, returns home", async () => {
  const pos = { x: 0, y: 0, z: 0 };
  let moves = 0;
  const m = new PositionMover(pos, { onMove: () => moves++ });
  m.update(10);
  const done = m.moveTo({ x: 0.2, y: 0, z: 0.1, duration_s: 1, hold_s: 2 });
  m.update(10.5);
  assert.ok(pos.x > 0 && pos.x < 0.2);
  m.update(11);
  assert.deepEqual(await done, { ok: true, state: "arrived" });
  assert.equal(pos.x, 0.2);
  m.update(12.5); // holding
  assert.equal(pos.x, 0.2);
  m.update(13 + 1); // past hold + return
  assert.equal(pos.x, 0);
  assert.equal(pos.z, 0);
  assert.ok(moves > 0);
  assert.equal(m.seg, null);
});

test("PositionMover: supersede starts from where she is, cancel freezes, reset goes home", async () => {
  const pos = { x: 0, y: 0, z: 0 };
  const m = new PositionMover(pos);
  m.update(0);
  const first = m.moveTo({ x: 0.4, y: 0, z: 0, duration_s: 1 });
  m.update(0.5);
  const mid = pos.x;
  const second = m.moveTo({ x: -0.2, y: 0, z: 0, duration_s: 1 });
  assert.equal((await first).state, "superseded");
  m.update(0.5);
  assert.equal(pos.x, mid); // no jump
  m.update(0.75);
  m.cancel();
  assert.equal((await second).state, "cancelled");
  const frozen = pos.x;
  m.update(5);
  assert.equal(pos.x, frozen);
  m.reset();
  assert.deepEqual(pos, { x: 0, y: 0, z: 0 });
});

test("PositionMover: clamps to bounds even if called directly", async () => {
  const pos = { x: 0, y: 0, z: 0 };
  const m = new PositionMover(pos);
  await m.moveTo({ x: 5, y: -1, z: -5, duration_s: 0 });
  assert.deepEqual(pos, { x: 0.6, y: 0, z: -0.6 });
});

test("leaseFaces: an emotion holds the face against Jev for hold_s, then releases", () => {
  let t = 0;
  const set = [];
  const character = { setFace(name, i) { set.push([name, i]); return true; } };
  const lease = leaseFaces(character, () => t);
  assert.equal(lease.claim("happy", 0.7, 3), true);
  assert.equal(character.setFace("sad", 1), false); // Jev's pick declined during the lease
  t = 3.5;
  assert.equal(character.setFace("sad", 1), true);
  lease.claim("curious", 0.5, 3);
  lease.release();
  assert.equal(character.setFace("angry", 1), true);
  lease.dispose();
  assert.equal(Object.hasOwn(character, "setFace"), true); // own method restored
  assert.deepEqual(set.map((s) => s[0]), ["happy", "sad", "curious", "angry"]);
});
