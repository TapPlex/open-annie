import test from "node:test";
import assert from "node:assert/strict";
import { BrowserSpeech } from "../src/voice-speech.js";

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
  fireAll() {
    const due = [...this.q.values()];
    this.q.clear();
    for (const t of due) t.fn();
  }
}

class Utterance {
  constructor(text) {
    this.text = text;
  }
}

function rig({ withSynth = true } = {}) {
  const spoken = [];
  const synth = {
    cancelled: 0,
    speak: (u) => spoken.push(u),
    cancel() {
      this.cancelled++;
    },
    getVoices: () => [{ lang: "en-US", localService: true, name: "Local" }],
  };
  const events = [];
  const bus = { emit: (type, d) => events.push([type, d]) };
  let t = 0;
  const timers = new FakeTimers();
  const speech = new BrowserSpeech({ bus, now: () => t, synth: withSynth ? synth : undefined, Utterance, timers });
  return { speech, spoken, synth, events, timers, setT: (v) => (t = v) };
}

test("needs a user gesture first; unavailable without the API", async () => {
  const { speech } = rig();
  assert.deepEqual(await speech.speak("hi"), { ok: false, error: "needs_activation" });
  const none = rig({ withSynth: false }).speech;
  assert.equal(none.activate(), false);
  assert.deepEqual(await none.speak("hi"), { ok: false, error: "speech_unavailable" });
});

test("speaks: events only once audio starts, ok on end, mouth frames while speaking", async () => {
  const { speech, spoken, events, setT } = rig();
  assert.ok(speech.activate());
  assert.equal(spoken.length, 1); // silent unlock utterance
  const p = speech.speak("Hello there friend");
  const u = spoken.at(-1);
  assert.equal(u.text, "Hello there friend");
  assert.equal(u.voice.name, "Local");
  assert.equal(events.length, 0); // nothing claimed before the synthesizer starts
  assert.equal(speech.frame(0), null);
  u.onstart();
  assert.deepEqual(events[0], ["speech.start", { text: "Hello there friend", source: "tts" }]);
  setT(0.1);
  const f = speech.frame(0.1);
  assert.ok(f.amp > 0.04 && f.db === f.amp * 20 - 40 && f.vowels);
  u.onboundary({ name: "word", charIndex: 6 });
  u.onend();
  assert.deepEqual(await p, { ok: true });
  assert.deepEqual(events.at(-1), ["speech.end", { text: "Hello there friend", source: "tts", ok: true }]);
  assert.equal(speech.frame(1), null);
  assert.ok(!events.some(([type]) => type.startsWith("assistant."))); // never fakes a model turn
});

test("cancel stops the line and drops the queue", async () => {
  const { speech, spoken, synth, events } = rig();
  speech.activate();
  const a = speech.speak("one");
  const b = speech.speak("two");
  spoken.at(-1).onstart();
  speech.cancel();
  assert.deepEqual(await a, { ok: false, error: "cancelled" });
  assert.deepEqual(await b, { ok: false, error: "cancelled" });
  assert.ok(synth.cancelled >= 1);
  assert.deepEqual(events.at(-1)[1].ok, false);
  assert.equal(speech.current, null);
});

test("busy beyond the queue bound; no audio and errors are reported honestly", async () => {
  const { speech, spoken, timers, events } = rig();
  speech.activate();
  const ps = [speech.speak("a"), speech.speak("b"), speech.speak("c")];
  assert.deepEqual(await speech.speak("d"), { ok: false, error: "busy" });
  timers.fireAll(); // "a" never started: start watchdog
  assert.deepEqual(await ps[0], { ok: false, error: "no_audio" });
  spoken.at(-1).onerror({ error: "not-allowed" }); // "b"
  assert.deepEqual(await ps[1], { ok: false, error: "needs_activation" });
  spoken.at(-1).onstart(); // "c"
  timers.fireAll(); // end watchdog
  assert.deepEqual(await ps[2], { ok: false, error: "timeout" });
  assert.equal(events.filter(([t]) => t === "speech.start").length, 1);
});
