import { FACES } from "./faces.js";
import { clamp, easeInOutCubic } from "./bus.js";

// Local avatar actions with explicit schemas: talk, emotion, position, toy_action.
// A trigger map turns an inbound toy command (already sanitized by the broker) into
// avatar actions only. toy_action is outbound (broker -> Rosemate MCP) and is accepted
// from the user alone, never from a trigger, so a received command can't echo back to
// the hardware. Nothing here evaluates text: messages are spoken and shown, not run.

export const ACTION_TYPES = ["talk", "emotion", "position", "toy_action"];
export const TALK_MAX = 200;
// Scene world coordinates (metres) the avatar root may occupy; home is the origin.
export const POSITION_BOUNDS = { x: [-0.6, 0.6], y: [0, 0.15], z: [-0.6, 0.6] };
export const MOVE_MAX_S = 5;
export const HOLD_MAX_S = 30;
export const TOY_TOOLS = ["rosemate_haptic_trigger", "rosemate_toy_stop", "rosemate_get_device_status"];
export const TOY_ACTIONS = ["vibrate", "pulse", "wave", "tease", "escalate", "stop"];
export const TRIGGER_ACTIONS = ["vibrate", "pulse", "wave", "tease", "escalate"];
const MAX_PER_TRIGGER = 4;

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const fail = (error) => ({ ok: false, error });
const extra = (a, keys) => Object.keys(a).find((k) => !keys.includes(k));

// -> {ok: true, action} (normalized, defaults filled) or {ok: false, error}.
export function validateAction(a) {
  if (!isObj(a)) return fail("not_an_object");
  switch (a.type) {
    case "talk": {
      if (extra(a, ["type", "message"])) return fail("unknown_field");
      if (typeof a.message !== "string") return fail("message_type");
      const message = a.message.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
      if (!message || message.length > TALK_MAX) return fail("message_length");
      return { ok: true, action: { type: "talk", message } };
    }
    case "emotion": {
      if (extra(a, ["type", "name", "intensity", "hold_s"])) return fail("unknown_field");
      if (typeof a.name !== "string" || !Object.hasOwn(FACES, a.name)) return fail("unknown_emotion");
      const intensity = a.intensity ?? 0.8;
      const hold_s = a.hold_s ?? 3;
      if (!isNum(intensity) || intensity < 0 || intensity > 1) return fail("intensity");
      if (!isNum(hold_s) || hold_s < 0.5 || hold_s > 10) return fail("hold_s");
      return { ok: true, action: { type: "emotion", name: a.name, intensity, hold_s } };
    }
    case "position": {
      if (extra(a, ["type", "x", "y", "z", "duration_s", "hold_s"])) return fail("unknown_field");
      for (const k of ["x", "y", "z"]) {
        const [lo, hi] = POSITION_BOUNDS[k];
        if (!isNum(a[k]) || a[k] < lo || a[k] > hi) return fail(`${k}_out_of_bounds`);
      }
      const duration_s = a.duration_s ?? 0.8;
      const hold_s = a.hold_s ?? 0;
      if (!isNum(duration_s) || duration_s < 0 || duration_s > MOVE_MAX_S) return fail("duration_s");
      if (!isNum(hold_s) || hold_s < 0 || hold_s > HOLD_MAX_S) return fail("hold_s");
      return { ok: true, action: { type: "position", x: a.x, y: a.y, z: a.z, duration_s, hold_s } };
    }
    case "toy_action": {
      if (extra(a, ["type", "tool", "arguments"])) return fail("unknown_field");
      if (!TOY_TOOLS.includes(a.tool)) return fail("unknown_tool");
      const args = a.arguments ?? {};
      if (!isObj(args)) return fail("arguments");
      if (a.tool === "rosemate_haptic_trigger") {
        // The broker re-validates against the operator's caps; this is the shape check.
        if (!TOY_ACTIONS.includes(args.action)) return fail("toy_action");
        if (args.action !== "stop") {
          if (!Number.isInteger(args.intensity) || args.intensity < 0 || args.intensity > 100) return fail("intensity");
          if (!isNum(args.duration_sec) || args.duration_sec < 1 || args.duration_sec > 120) return fail("duration_sec");
        }
      }
      return { ok: true, action: { type: "toy_action", tool: a.tool, arguments: { ...args } } };
    }
    default:
      return fail("unknown_type");
  }
}

// Short, general-audience acknowledgements, a supported face, and a small move that
// returns home on its own. `stop` cancels what is queued, then settles her.
export const DEFAULT_TRIGGER_MAP = {
  haptic_trigger: {
    vibrate: [
      { type: "talk", message: "Oh! I felt a little buzz." },
      { type: "emotion", name: "surprised", intensity: 0.6 },
      { type: "position", x: 0.06, y: 0, z: 0.08, duration_s: 0.6, hold_s: 2.5 },
    ],
    pulse: [
      { type: "talk", message: "Ooh, pulses. Got it!" },
      { type: "emotion", name: "amused", intensity: 0.7 },
      { type: "position", x: -0.06, y: 0, z: 0.06, duration_s: 0.6, hold_s: 2.5 },
    ],
    wave: [
      { type: "talk", message: "Whoa, here comes a wave." },
      { type: "emotion", name: "happy", intensity: 0.7 },
      { type: "position", x: 0.08, y: 0, z: 0.04, duration_s: 0.9, hold_s: 2.5 },
    ],
    tease: [
      { type: "talk", message: "Hey, cheeky!" },
      { type: "emotion", name: "playful", intensity: 0.8 },
      { type: "position", x: -0.05, y: 0, z: 0.06, duration_s: 0.5, hold_s: 2 },
    ],
    escalate: [
      { type: "talk", message: "Okay, it's building up!" },
      { type: "emotion", name: "surprised", intensity: 0.8 },
      { type: "position", x: 0, y: 0, z: 0.1, duration_s: 0.8, hold_s: 2.5 },
    ],
  },
  stop: [
    { type: "emotion", name: "relieved", intensity: 0.6 },
    { type: "position", x: 0, y: 0, z: 0, duration_s: 1.0 },
  ],
};

// Validates a whole map (e.g. a ?rosemateMap= file). toy_action is rejected: triggers
// may only move the avatar.
export function validateTriggerMap(map) {
  if (!isObj(map) || extra(map, ["haptic_trigger", "stop"])) return fail("map_shape");
  const list = (arr, where) => {
    if (!Array.isArray(arr) || arr.length > MAX_PER_TRIGGER) return { error: `${where}: list` };
    const out = [];
    for (const a of arr) {
      if (isObj(a) && a.type === "toy_action") return { error: `${where}: toy_action not allowed in a trigger map` };
      const v = validateAction(a);
      if (!v.ok) return { error: `${where}: ${v.error}` };
      out.push(v.action);
    }
    return { out };
  };
  const clean = { haptic_trigger: {}, stop: [] };
  const ht = map.haptic_trigger ?? {};
  if (!isObj(ht)) return fail("map_shape");
  for (const [k, arr] of Object.entries(ht)) {
    if (!TRIGGER_ACTIONS.includes(k) && k !== "default") return fail(`haptic_trigger.${k}: unknown action`);
    const r = list(arr, `haptic_trigger.${k}`);
    if (r.error) return fail(r.error);
    clean.haptic_trigger[k] = r.out;
  }
  if (map.stop != null) {
    const r = list(map.stop, "stop");
    if (r.error) return fail(r.error);
    clean.stop = r.out;
  }
  return { ok: true, map: clean };
}

// Local check of an inbound command (the broker already rebuilt it): shape and age.
export function checkCommand(cmd, nowS, maxAgeS = 30) {
  if (!isObj(cmd) || typeof cmd.id !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(cmd.id)) return fail("malformed");
  if (cmd.type !== "haptic_trigger" && cmd.type !== "stop") return fail("unknown_type");
  if (!isNum(cmd.timestamp)) return fail("malformed");
  if (nowS - cmd.timestamp > maxAgeS) return fail("stale");
  if (cmd.timestamp - nowS > 60) return fail("malformed");
  if (cmd.type === "haptic_trigger" && !TOY_ACTIONS.includes(cmd.action)) return fail("unknown_action");
  return { ok: true };
}

// Bounded id memory with a TTL, so reconnects never replay a command onto the avatar.
export class SeenIds {
  constructor({ ttlS = 600, max = 512, now }) {
    Object.assign(this, { ttlS, max, now });
    this.ids = new Map();
  }
  add(id) {
    const t = this.now();
    for (const [k, at] of this.ids) {
      if (t - at <= this.ttlS && this.ids.size <= this.max) break;
      this.ids.delete(k);
    }
    if (this.ids.has(id)) return false;
    this.ids.set(id, t);
    while (this.ids.size > this.max) this.ids.delete(this.ids.keys().next().value);
    return true;
  }
}

// Dispatches validated actions to handlers {talk, emotion, position, toy_action, cancel}.
// Each handler resolves {ok, ...}; failures are returned, never thrown.
export class ActionDispatcher {
  constructor({ handlers, map = DEFAULT_TRIGGER_MAP, onResult } = {}) {
    this.handlers = handlers ?? {};
    this.onResult = onResult;
    const v = validateTriggerMap(map);
    if (!v.ok) throw new Error(`trigger map: ${v.error}`);
    this.map = v.map;
  }

  setMap(map) {
    const v = validateTriggerMap(map);
    if (v.ok) this.map = v.map;
    return v;
  }

  // source: "user" (UI / console demo interface) or "trigger" (an inbound command).
  async dispatch(action, { source = "user" } = {}) {
    const v = validateAction(action);
    let r;
    if (!v.ok) r = v;
    else if (v.action.type === "toy_action" && source !== "user") r = fail("toy_action_requires_user");
    else if (!this.handlers[v.action.type]) r = fail(`${v.action.type}_unavailable`);
    else {
      try {
        r = (await this.handlers[v.action.type](v.action)) ?? { ok: true };
      } catch (e) {
        r = fail(String(e?.message ?? e).slice(0, 80));
      }
    }
    this.onResult?.({ action: v.ok ? v.action : action, source, result: r });
    return r;
  }

  // An inbound command -> avatar actions, run together (speech doesn't hold up the face).
  async trigger(cmd) {
    let actions;
    if (cmd.type === "stop" || cmd.action === "stop") {
      this.cancel();
      actions = this.map.stop;
    } else actions = this.map.haptic_trigger[cmd.action] ?? this.map.haptic_trigger.default ?? [];
    return Promise.all(actions.map((a) => this.dispatch(a, { source: "trigger" })));
  }

  cancel() {
    this.handlers.cancel?.();
  }
}

// Interpolates the avatar root (any {x, y, z}, e.g. vrm.scene.position) inside
// POSITION_BOUNDS. A new move supersedes the old one from wherever she is now (no jump);
// cancel freezes her in place. Only the root moves: the skeleton and hips animation are
// never touched. `update(now)` runs once per frame on the stage clock.
export class PositionMover {
  constructor(target, { home = { x: target.x, y: target.y, z: target.z }, onMove } = {}) {
    this.target = target;
    this.home = { ...home };
    this.onMove = onMove;
    this.now = 0;
    this.seg = null; // {from, to, t0, dur, hold, resolve, returning}
  }

  #settle(state) {
    const s = this.seg;
    this.seg = null;
    if (s && !s.returning) s.resolve({ ok: true, state });
  }

  moveTo({ x, y, z, duration_s = 0.8, hold_s = 0 }) {
    const B = POSITION_BOUNDS;
    const to = { x: clamp(x, ...B.x), y: clamp(y, ...B.y), z: clamp(z, ...B.z) };
    this.#settle("superseded");
    return new Promise((resolve) => {
      this.seg = { from: this.#current(), to, t0: this.now, dur: Math.max(0, duration_s), hold: hold_s, resolve, returning: false };
      if (this.seg.dur === 0) this.update(this.now);
    });
  }

  cancel() {
    this.#settle("cancelled");
  }

  // Back home at once (cleanup).
  reset() {
    this.cancel();
    this.#write(this.home);
  }

  #current() {
    return { x: this.target.x, y: this.target.y, z: this.target.z };
  }

  #write(p) {
    this.target.x = p.x;
    this.target.y = p.y;
    this.target.z = p.z;
    this.onMove?.();
  }

  update(now) {
    this.now = now;
    const s = this.seg;
    if (!s) return;
    const k = s.dur > 0 ? clamp((now - s.t0) / s.dur) : 1;
    const e = easeInOutCubic(k);
    this.#write({ x: s.from.x + (s.to.x - s.from.x) * e, y: s.from.y + (s.to.y - s.from.y) * e, z: s.from.z + (s.to.z - s.from.z) * e });
    if (k < 1) return;
    if (s.returning) return void (this.seg = null);
    s.resolve({ ok: true, state: "arrived" });
    if (s.hold > 0) {
      // Drift back home after the hold, as its own segment (the caller already has its answer).
      this.seg = { from: { ...s.to }, to: { ...this.home }, t0: now + s.hold, dur: Math.max(0.8, s.dur), hold: 0, resolve: () => {}, returning: true };
    } else this.seg = null;
  }
}

// Lets an explicit emotion action hold the face for hold_s: other setFace callers
// (Jev's picks via annie.js) are declined meanwhile, so Annie's decision log reports them
// as not acted. Barge-in still wins (Character.bargeIn sets the face directly).
export function leaseFaces(character, now) {
  const own = Object.hasOwn(character, "setFace") ? character.setFace : null;
  const orig = character.setFace.bind(character);
  let until = -Infinity;
  character.setFace = (name, intensity) => (now() < until ? false : orig(name, intensity));
  return {
    claim(name, intensity, holdS) {
      until = -Infinity;
      const applied = orig(name, intensity); // false = queued behind the face dwell, applies shortly
      until = now() + holdS;
      return applied;
    },
    release() {
      until = -Infinity;
    },
    dispose() {
      if (own) character.setFace = own;
      else delete character.setFace;
    },
  };
}
