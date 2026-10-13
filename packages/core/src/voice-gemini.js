import { LiveAnalyser, LiveMouth } from "./lipsync.js";

const TURN_QUIET_S = 1.5;
const BARGEIN_HOLD_S = 0.35;
const MIC_GATE = 0.15;
const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

export function downsamplePcm16(samples, sampleRate) {
  const ratio = sampleRate / INPUT_RATE;
  const length = Math.floor(samples.length / ratio);
  const pcm = new Int16Array(length);
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(samples.length, Math.floor((i + 1) * ratio));
    let total = 0;
    for (let j = start; j < end; j++) total += samples[j];
    const sample = total / Math.max(1, end - start);
    pcm[i] = Math.round(Math.max(-1, Math.min(1, sample)) * (sample < 0 ? 32768 : 32767));
  }
  return pcm;
}

function encodeBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000)
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function decodePcm16(data) {
  const raw = atob(data);
  const samples = new Float32Array(Math.floor(raw.length / 2));
  for (let i = 0; i < samples.length; i++) {
    const value = raw.charCodeAt(i * 2) | (raw.charCodeAt(i * 2 + 1) << 8);
    samples[i] = (value & 0x8000 ? value - 0x10000 : value) / 32768;
  }
  return samples;
}

function brokerSocketUrl(brokerUrl) {
  const url = new URL(`${brokerUrl.replace(/\/$/, "")}/live`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

export class GeminiLiveVoice {
  constructor({ bus, brokerUrl, instructions = "", clock, mic, onRaw }) {
    Object.assign(this, { bus, brokerUrl, instructions, clock, mic, onRaw });
    this.user = { text: "", last: -Infinity, open: false };
    this.asst = { text: "", last: -Infinity, open: false };
    this.lastAnnieActive = -Infinity;
    this.userVoice = { since: null, quietSince: null, overAnnie: false, barged: false };
    this.playbackAt = 0;
    this.audioPending = 0;
    this.sources = new Set();
  }

  async connect() {
    this.mic ??= await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.audioCtx = new AudioContext();
    await this.audioCtx.resume();
    const micSource = this.audioCtx.createMediaStreamSource(this.mic);
    this.micAnalyser = new LiveAnalyser(this.audioCtx, micSource);
    this.input = this.audioCtx.createScriptProcessor(4096, 1, 1);
    const mute = this.audioCtx.createGain();
    mute.gain.value = 0;
    this.input.connect(mute).connect(this.audioCtx.destination);
    this.input.onaudioprocess = (event) => {
      if (this.socket?.readyState !== WebSocket.OPEN || !this.ready) return;
      const pcm = downsamplePcm16(event.inputBuffer.getChannelData(0), this.audioCtx.sampleRate);
      this.socket.send(JSON.stringify({
        realtimeInput: { audio: { mimeType: `audio/pcm;rate=${INPUT_RATE}`, data: encodeBase64(pcm) } },
      }));
    };
    micSource.connect(this.input);

    this.output = this.audioCtx.createGain();
    this.output.connect(this.audioCtx.destination);
    this.recording = this.audioCtx.createMediaStreamDestination();
    this.output.connect(this.recording);
    this.remote = this.recording.stream;
    this.analyser = new LiveMouth(this.audioCtx, this.output);
    this.socket = new WebSocket(brokerSocketUrl(this.brokerUrl));
    await new Promise((resolve, reject) => {
      let settled = false;
      const timeout = setTimeout(() => {
        if (!settled) settled = true, reject(new Error("voice connection timed out"));
      }, 15000);
      this.socket.onerror = () => {
        clearTimeout(timeout);
        if (!settled) settled = true, reject(new Error("voice connection failed"));
      };
      this.socket.onclose = () => {
        this.ready = false;
        clearTimeout(timeout);
        if (!settled) settled = true, reject(new Error("voice connection closed"));
        if (!this.closed) this.bus.emit("session.closed", { reason: "connection_closed" });
      };
      this.socket.onopen = () => this.socket.send(JSON.stringify({
        type: "annie.setup", instructions: this.instructions,
      }));
      this.socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data);
          this.onRaw?.(event);
          if (event.type === "annie.error") throw new Error(event.error);
          if (event.type === "annie.session") {
            this.sessionId = event.session_id;
            this.token = event.token;
            this.expiresAt = event.expires_at;
            settled = true;
            clearTimeout(timeout);
            resolve(event);
          } else this.handleEvent(event);
        } catch (error) {
          clearTimeout(timeout);
          reject(error);
        }
      };
    });
    return { session_id: this.sessionId, token: this.token, expires_at: this.expiresAt };
  }

  handleEvent(event) {
    const now = this.clock.now();
    if (event.type === "annie.closed") {
      this.closed = true;
      this.bus.emit("session.closed", { reason: event.reason });
      return;
    }
    const content = event.serverContent;
    if (event.setupComplete) this.ready = true;
    if (!content) {
      if (event.error) this.bus.emit("voice.error", { code: event.error.code, message: event.error.message });
      return;
    }
    if (content.interrupted) {
      this.bus.emit("bargein", {});
      this.#stopPlayback();
      if (this.asst.open) this.#finishAssistant();
    }
    const inputText = content.inputTranscription?.text;
    if (inputText) {
      if (!this.user.open) this.user.open = true, this.user.text = "";
      this.user.text += inputText;
      this.user.last = now;
      this.bus.emit("user.partial", { text: this.user.text.trim() });
    }
    const outputText = content.outputTranscription?.text;
    if (outputText) {
      if (!this.asst.open) {
        this.asst.open = true;
        this.asst.text = "";
        this.bus.emit("assistant.audio.start", {});
      }
      this.asst.text += outputText;
      this.asst.last = now;
      this.lastAnnieActive = now;
      this.bus.emit("assistant.partial", { text: this.asst.text.trim() });
    }
    for (const part of content.modelTurn?.parts ?? []) {
      const inline = part.inlineData ?? part.inline_data;
      if (inline?.data && (!inline.mimeType || inline.mimeType.includes("audio/pcm")))
        this.#playAudio(inline.data);
    }
    if (content.turnComplete) {
      if (this.user.open) {
        this.user.open = false;
        this.bus.emit("user.final", { text: this.user.text.trim() });
      }
      if (this.asst.open) {
        this.bus.emit("assistant.final", { text: this.asst.text.trim() });
        this.asst.open = false;
      }
      this.turnComplete = true;
      this.#finishAudioIfDrained();
    }
  }

  #playAudio(data) {
    const samples = decodePcm16(data);
    if (!samples.length) return;
    if (!this.asst.open) {
      this.asst.open = true;
      this.asst.text = "";
      this.bus.emit("assistant.audio.start", {});
    }
    const buffer = this.audioCtx.createBuffer(1, samples.length, OUTPUT_RATE);
    buffer.copyToChannel(samples, 0);
    const source = this.audioCtx.createBufferSource();
    source.buffer = buffer;
    source.connect(this.output);
    const start = Math.max(this.audioCtx.currentTime, this.playbackAt);
    this.playbackAt = start + buffer.duration;
    this.audioPending++;
    this.sources.add(source);
    source.onended = () => {
      if (this.sources.delete(source)) this.audioPending = Math.max(0, this.audioPending - 1);
      this.#finishAudioIfDrained();
    };
    source.start(start);
    this.lastAnnieActive = this.clock.now();
  }

  #finishAudioIfDrained() {
    if (this.audioPending || (!this.turnComplete && !this.endAfterPlayback)) return;
    this.turnComplete = false;
    this.endAfterPlayback = false;
    this.bus.emit("assistant.audio.end", {});
  }

  #stopPlayback() {
    this.playbackAt = this.audioCtx?.currentTime ?? 0;
    for (const source of this.sources) source.stop();
    this.audioPending = 0;
    this.turnComplete = false;
    if (!this.audioCtx) return;
    this.output?.disconnect();
    this.output = this.audioCtx.createGain();
    this.output.connect(this.audioCtx.destination);
    this.output.connect(this.recording);
    this.analyser = new LiveMouth(this.audioCtx, this.output);
  }

  tick(now, mouthSpeaking) {
    this.speaking = mouthSpeaking;
    if (mouthSpeaking) this.lastAnnieActive = now;
    const u = this.user, a = this.asst;
    if (u.open && now - u.last > TURN_QUIET_S) {
      u.open = false;
      this.bus.emit("user.final", { text: u.text.trim() });
    }
    if (a.open && now - this.lastAnnieActive > TURN_QUIET_S) this.#finishAssistant();
    const m = this.micAnalyser?.sample();
    this.micLevel = m?.amp ?? 0;
    const v = this.userVoice;
    if (m && m.amp > MIC_GATE) {
      v.quietSince = null;
      if (v.since == null) {
        v.since = now;
        v.overAnnie = mouthSpeaking || now - this.lastAnnieActive < 0.2;
      }
      if (v.overAnnie && !v.barged && now - v.since >= BARGEIN_HOLD_S) {
        v.barged = true;
        this.bus.emit("bargein", {});
        if (this.asst.open) this.#finishAssistant();
      }
    } else if (v.since != null) {
      v.quietSince ??= now;
      if (now - v.quietSince > 0.6) (v.since = null), (v.barged = false);
    }
    this.userVoiced = v.since != null;
    return this.analyser?.sample() ?? null;
  }

  #finishAssistant() {
    if (!this.asst.open) return;
    this.asst.open = false;
    this.bus.emit("assistant.final", { text: this.asst.text.trim() });
    this.endAfterPlayback = true;
    this.#finishAudioIfDrained();
  }

  close() {
    this.ready = false;
    this.socket?.close();
    this.input?.disconnect();
    this.audioCtx?.close();
    this.mic?.getTracks().forEach((track) => track.stop());
  }
}
