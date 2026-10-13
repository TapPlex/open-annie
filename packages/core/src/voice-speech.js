import { clamp } from "./bus.js";

// Speech for avatar actions (talk) through the browser's Web Speech API.
//
// This is a labelled FALLBACK. GPT-Live, open-annie's voice, has no "say this text"
// command: its data channel accepts session.close, mute/unmute and the instruction,
// thinking and commentary appends (broker/UPSTREAM.md §1.4), none of which speak a given
// line, and GPT-Live is not the Realtime API (no response.create). So a talk action is
// spoken by the browser's own synthesizer instead.
//
// Limits, stated honestly:
// - The browser needs a user gesture first (activate() from a click), or speak() fails
//   with "needs_activation". Nothing is reported as spoken unless the synthesizer starts.
// - Its audio can't be tapped, so the mouth is approximated from word boundaries and a
//   syllable rhythm (frame()), not analysed like the GPT-Live or scripted audio.
// - Voice and quality depend on the OS/browser. Local voices are preferred; some
//   browsers only have network voices, which send the text to the browser vendor.
//
// Bus events: speech.start {text} when audio actually starts, speech.end {text, ok, error?}
// when it ends or is cut. Deliberately not assistant.*: those mean a GPT-Live/scripted
// model turn, feed Jev and the conversation history, and must not be faked.

export const SPEECH_LABEL = "browser speech synthesis (fallback: GPT-Live has no text-to-speech command)";
const START_TIMEOUT_S = 3;
const SYLLABLE_HZ = 4.2;

export class BrowserSpeech {
  constructor({ bus, now, synth = globalThis.speechSynthesis, Utterance = globalThis.SpeechSynthesisUtterance, maxQueue = 3, lang = "en-US", timers = globalThis } = {}) {
    Object.assign(this, { bus, now, synth, Utterance, maxQueue, lang, timers });
    this.activated = false;
    this.queue = [];
    this.current = null; // {text, resolve, started, utter, word, wordAt, timers}
    this.voice = null;
    this.remoteVoice = false;
  }

  get available() {
    return !!(this.synth && this.Utterance);
  }

  get speaking() {
    return !!this.current?.started;
  }

  // Call from a click/tap handler: unlocks audio and picks a voice.
  activate() {
    if (!this.available) return false;
    const voices = this.synth.getVoices?.() ?? [];
    const en = voices.filter((v) => /^en(-|_|$)/i.test(v.lang ?? ""));
    this.voice = en.find((v) => v.localService) ?? voices.find((v) => v.localService) ?? en[0] ?? null;
    this.remoteVoice = !!this.voice && !this.voice.localService;
    const u = new this.Utterance(" ");
    u.volume = 0;
    this.synth.speak(u);
    this.activated = true;
    return true;
  }

  // Resolves {ok: true} once the line has been spoken, or {ok: false, error}.
  speak(text) {
    if (!this.available) return Promise.resolve({ ok: false, error: "speech_unavailable" });
    if (!this.activated) return Promise.resolve({ ok: false, error: "needs_activation" });
    if (this.queue.length + (this.current ? 1 : 0) >= this.maxQueue) return Promise.resolve({ ok: false, error: "busy" });
    return new Promise((resolve) => {
      this.queue.push({ text, resolve });
      this.#next();
    });
  }

  // Stops the current line and drops the queue (stop command, barge-in, cleanup).
  cancel() {
    for (const job of this.queue.splice(0)) job.resolve({ ok: false, error: "cancelled" });
    if (this.current) {
      const job = this.current;
      try {
        this.synth.cancel();
      } catch {}
      this.#finish(job, { ok: false, error: "cancelled" });
    }
  }

  #next() {
    if (this.current || !this.queue.length) return;
    const job = (this.current = this.queue.shift());
    const u = (job.utter = new this.Utterance(job.text));
    u.lang = this.voice?.lang ?? this.lang;
    if (this.voice) u.voice = this.voice;
    job.word = "";
    job.wordAt = null;
    u.onstart = () => {
      if (this.current !== job) return;
      job.started = true;
      job.startedAt = this.now();
      job.wordAt = job.startedAt;
      job.word = job.text.split(/\s+/)[0] ?? "";
      this.timers.clearTimeout(job.startTimer);
      this.bus?.emit("speech.start", { text: job.text, source: "tts" });
    };
    u.onboundary = (e) => {
      if (this.current !== job || e.name === "sentence") return;
      job.word = /^\S*/.exec(job.text.slice(e.charIndex ?? 0))?.[0] ?? "";
      job.wordAt = this.now();
    };
    u.onend = () => this.#finish(job, { ok: true });
    u.onerror = (e) => {
      const code = e?.error;
      this.#finish(job, { ok: false, error: code === "not-allowed" ? "needs_activation" : code === "interrupted" || code === "canceled" ? "cancelled" : "speech_error" });
    };
    // No start (autoplay blocked, no voices) or no end (engine stalls on long lines): give up honestly.
    job.startTimer = this.timers.setTimeout(() => {
      if (this.current === job && !job.started) {
        try {
          this.synth.cancel();
        } catch {}
        this.#finish(job, { ok: false, error: "no_audio" });
      }
    }, START_TIMEOUT_S * 1000);
    job.endTimer = this.timers.setTimeout(() => {
      if (this.current !== job) return;
      try {
        this.synth.cancel();
      } catch {}
      this.#finish(job, { ok: false, error: "timeout" });
    }, (START_TIMEOUT_S + 2 + 0.12 * job.text.length) * 1000);
    this.synth.speak(u);
  }

  #finish(job, result) {
    if (job.done) return;
    job.done = true;
    this.timers.clearTimeout(job.startTimer);
    this.timers.clearTimeout(job.endTimer);
    if (job.utter) job.utter.onstart = job.utter.onend = job.utter.onerror = job.utter.onboundary = null;
    if (job.started) this.bus?.emit("speech.end", { text: job.text, source: "tts", ok: result.ok, ...(result.ok ? {} : { error: result.error }) });
    if (this.current === job) this.current = null;
    job.resolve(result);
    this.#next();
  }

  // Approximate lipsync feature frame {amp, db, vowels} while speaking, else null.
  // A syllable rhythm restarted on each word boundary, vowels from the word's letters.
  frame(now) {
    const job = this.current;
    if (!job?.started) return null;
    const t = Math.max(0, now - (job.wordAt ?? job.startedAt));
    const vowels = job.word.toLowerCase().match(/[aeiouy]/g) ?? ["a"];
    const dur = Math.max(0.18, vowels.length / SYLLABLE_HZ);
    // Between words (past the estimated word length) the mouth relaxes towards closed.
    const inWord = t < dur + 0.08 || job.wordAt === job.startedAt;
    const phase = Math.abs(Math.sin(Math.PI * SYLLABLE_HZ * t));
    const amp = clamp(inWord ? 0.18 + 0.62 * Math.pow(phase, 0.7) : 0.05);
    const v = vowels[Math.floor(t * SYLLABLE_HZ) % vowels.length];
    const pick = { a: "aa", e: "ee", i: "ih", y: "ih", o: "oh", u: "ou" }[v];
    const scores = { aa: 0.1, ih: 0.1, ou: 0.1, ee: 0.1, oh: 0.1 };
    scores[pick] = 1;
    return { amp, db: amp * 20 - 40, vowels: scores };
  }
}
