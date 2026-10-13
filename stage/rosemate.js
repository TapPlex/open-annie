import { ActionDispatcher, PositionMover, leaseFaces, validateTriggerMap, TRIGGER_ACTIONS } from "../packages/core/src/actions.js";
import { BrowserSpeech, SPEECH_LABEL } from "../packages/core/src/voice-speech.js";
import { RosemateBridge } from "../packages/core/src/rosemate-bridge.js";

// Optional Rosemate integration for the stage (?rosemate, docs/rosemate.md). Loaded only
// on demand, never in capture or replay. Inbound toy commands drive avatar actions only;
// toy actions go out solely from this panel's buttons, after the user opts in.
//
//   ?rosemate                    avatar-only demo: local mock triggers, no broker, no toy
//   ?rosemate&broker=URL         plus the broker's Rosemate events (access key required)
//   ?rosemate&rosemateMap=PATH   a same-origin JSON trigger map (validateTriggerMap)
//
// Console demo interface: window.annieActions.dispatch({type: "talk", message: "Hi"}),
// .trigger("vibrate"), .stop(), .setMap(map), .map.

export async function startRosemate({ bus, character, brokerUrl, now, getMode, isLive, mapUrl }) {
  const root = character.vrm.scene;
  const speech = new BrowserSpeech({ bus, now });
  const lease = leaseFaces(character, now);
  const mover = new PositionMover(root.position, { home: { x: root.position.x, y: root.position.y, z: root.position.z }, onMove: () => root.updateMatrixWorld() });
  const ui = panel();
  let bridge = null;
  let mockId = 0;

  const dispatcher = new ActionDispatcher({
    handlers: {
      talk: (a) => {
        if (getMode() === "script") return { ok: false, error: "busy_script" };
        if (isLive()) return { ok: false, error: "live_voice_active" }; // the open mic would hear it; GPT-Live owns the voice
        return speech.speak(a.message);
      },
      emotion: (a) => {
        const applied = lease.claim(a.name, a.intensity, a.hold_s);
        bus.emit("cue.face", { name: a.name, intensity: a.intensity, source: "rosemate", why: applied ? "rosemate" : "rosemate · queued" });
        return { ok: true, applied };
      },
      position: (a) => (getMode() === "script" ? { ok: false, error: "busy_script" } : mover.moveTo(a)),
      toy_action: (a) => (bridge ? bridge.toyAction(a.tool, a.arguments) : { ok: false, error: "not_authorized" }),
      cancel: () => {
        speech.cancel();
        mover.cancel();
        lease.release();
      },
    },
    onResult: ({ action, source, result }) => ui.log(`${source} ${action?.type ?? "?"}: ${result.ok ? "ok" : result.error}`),
  });

  if (mapUrl) {
    const u = new URL(mapUrl, location.href);
    if (u.origin !== location.origin) ui.log("trigger map: same-origin only, using defaults");
    else {
      const r = await fetch(u).then((x) => x.json()).then((m) => dispatcher.setMap(m)).catch(() => ({ ok: false, error: "unreadable" }));
      ui.log(r.ok ? "trigger map loaded" : `trigger map rejected (${r.error}), using defaults`);
    }
  }

  // An inbound (or local mock) command: avatar actions only, never echoed upstream.
  const onCommand = (cmd) => {
    if (getMode() === "script") return ui.log(`trigger ${cmd.action} ignored: demo playing`);
    ui.log(`trigger ${cmd.action}${cmd.intensity != null ? ` ${cmd.intensity}` : ""}`);
    dispatcher.trigger(cmd);
  };
  const offBarge = bus.on("bargein", () => speech.cancel());

  // ---- panel wiring ----------------------------------------------------------
  ui.label.textContent = `speech: ${SPEECH_LABEL}`;
  ui.voice.onclick = () => {
    const ok = speech.activate();
    ui.voice.textContent = ok ? (speech.remoteVoice ? "Voice on (network voice)" : "Voice on") : "Speech unavailable";
    ui.voice.disabled = true;
  };
  for (const action of [...TRIGGER_ACTIONS, "stop"]) {
    const b = button(action === "stop" ? "mock stop" : `mock ${action}`);
    b.onclick = () => onCommand({ id: `local-${++mockId}`, type: action === "stop" ? "stop" : "haptic_trigger", action, timestamp: Date.now() / 1000 });
    ui.mocks.append(b);
  }
  ui.state.textContent = brokerUrl ? "not connected" : "avatar-only (no broker: local mock triggers)";
  if (brokerUrl) {
    ui.connectRow.hidden = false;
    ui.connect.onclick = async () => {
      const key = ui.key.value;
      ui.key.value = ""; // used once, never stored
      bridge?.dispose();
      bridge = new RosemateBridge({
        brokerUrl,
        onCommand,
        onRejected: (why) => ui.log(`command rejected: ${why}`),
        onStatus: ({ state, detail, upstream }) => {
          ui.state.textContent = `broker ${state}${upstream ? ` · Rosemate stream ${upstream}` : ""}${detail ? ` · ${detail}` : ""}`;
          ui.toyRow.hidden = !(bridge?.token && state !== "unauthorized");
        },
      });
      const r = await bridge.authorize(key, { toyActions: false });
      if (!r.ok) return;
      ui.consentRow.hidden = !r.features.toy_actions;
      ui.consent.checked = r.toyConsent;
      ui.toyRow.hidden = false;
    };
    ui.consent.onchange = async () => {
      const r = bridge ? await bridge.setConsent(ui.consent.checked) : { ok: false, error: "not_authorized" };
      ui.consent.checked = r.ok ? r.toyConsent : false;
      ui.log(r.ok ? `toy actions ${r.toyConsent ? "allowed" : "off"}` : `consent: ${r.error}`);
    };
    ui.send.onclick = () =>
      dispatcher.dispatch({ type: "toy_action", tool: "rosemate_haptic_trigger", arguments: { action: "pulse", intensity: 20, duration_sec: 2, pattern: "pulse", reason: "open-annie panel" } });
    ui.stopToy.onclick = () => dispatcher.dispatch({ type: "toy_action", tool: "rosemate_toy_stop", arguments: {} });
    ui.status.onclick = async () => {
      const r = bridge ? await bridge.toyAction("rosemate_get_device_status", {}) : { ok: false, error: "not_authorized" };
      ui.log(r.ok ? `device status: ${(r.text ?? "").slice(0, 120)}` : `device status: ${r.error}`);
    };
  }

  window.annieActions = {
    dispatch: (a) => dispatcher.dispatch(a, { source: "user" }),
    trigger: (action = "vibrate") => onCommand({ id: `local-${++mockId}`, type: action === "stop" ? "stop" : "haptic_trigger", action, timestamp: Date.now() / 1000 }),
    stop: () => onCommand({ id: `local-${++mockId}`, type: "stop", action: "stop", timestamp: Date.now() / 1000 }),
    setMap: (m) => dispatcher.setMap(m),
    get map() {
      return structuredClone(dispatcher.map);
    },
    validateTriggerMap,
  };

  let disposed = false;
  return {
    // Approximate mouth frame while the browser voice speaks (idle mode only).
    frame: (t) => speech.frame(t),
    update: (t) => mover.update(t),
    // The scripted demo is about to play: drop everything and put her back.
    suspend() {
      dispatcher.cancel();
      mover.reset();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      offBarge();
      bridge?.dispose({ keepalive: true });
      dispatcher.cancel();
      mover.reset();
      lease.dispose();
      ui.el.remove();
      delete window.annieActions;
    },
  };
}

function button(text) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = text;
  return b;
}

// Built with textContent only: nothing received is ever parsed as HTML.
function panel() {
  const el = document.createElement("section");
  el.className = "rosemate";
  el.setAttribute("aria-label", "Rosemate integration (optional)");
  const row = (cls) => Object.assign(document.createElement("div"), { className: cls });
  const title = Object.assign(document.createElement("h2"), { textContent: "Rosemate (optional)" });
  const state = row("rm-state");
  const label = row("rm-label");
  const voice = button("Enable voice");
  const mocks = row("rm-row");
  mocks.title = "Local mock triggers: avatar only, nothing is sent to a toy";
  const connectRow = row("rm-row");
  connectRow.hidden = true;
  const key = Object.assign(document.createElement("input"), { type: "password", placeholder: "access key", autocomplete: "off" });
  const connect = button("Connect");
  connectRow.append(key, connect);
  const consentRow = row("rm-row");
  consentRow.hidden = true;
  const consent = Object.assign(document.createElement("input"), { type: "checkbox", id: "rm-consent" });
  const consentLabel = Object.assign(document.createElement("label"), { htmlFor: "rm-consent", textContent: "I allow open-annie to send toy actions" });
  consentRow.append(consent, consentLabel);
  const toyRow = row("rm-row");
  toyRow.hidden = true;
  const send = button("Send pulse");
  const stopToy = button("Stop toy");
  stopToy.className = "rm-stop";
  const status = button("Device status");
  toyRow.append(stopToy, send, status);
  const logEl = row("rm-log");
  el.append(title, state, label, voice, mocks, connectRow, consentRow, toyRow, logEl);
  document.getElementById("app").append(el);
  const lines = [];
  return {
    el, state, label, voice, mocks, connectRow, key, connect, consentRow, consent, toyRow, send, stopToy, status,
    log(text) {
      lines.push(String(text).slice(0, 160));
      while (lines.length > 6) lines.shift();
      logEl.textContent = lines.join("\n");
    },
  };
}
