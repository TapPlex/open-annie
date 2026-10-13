import test from "node:test";
import assert from "node:assert/strict";
import { GeminiLiveVoice, downsamplePcm16 } from "../src/voice-gemini.js";

test("downsamples float audio to signed little-endian PCM16 input frames", () => {
  assert.deepEqual(
    [...downsamplePcm16(Float32Array.from([0.5, 0.5, 0.5, -1, -1, -1]), 48000)],
    [16384, -32768],
  );
});

test("maps Gemini transcription, turn completion and interruption to the voice bus", () => {
  const events = [];
  const bus = { emit: (type, payload) => events.push([type, payload]) };
  const voice = new GeminiLiveVoice({ bus, brokerUrl: "http://localhost:8787", clock: { now: () => 1 } });
  voice.handleEvent({ setupComplete: {} });
  voice.handleEvent({
    serverContent: {
      inputTranscription: { text: "Hi" },
      outputTranscription: { text: "Hello" },
    },
  });
  voice.handleEvent({ serverContent: { turnComplete: true } });
  voice.handleEvent({ serverContent: { interrupted: true } });
  assert.ok(voice.ready);
  assert.deepEqual(events, [
    ["user.partial", { text: "Hi" }],
    ["assistant.audio.start", {}],
    ["assistant.partial", { text: "Hello" }],
    ["user.final", { text: "Hi" }],
    ["assistant.final", { text: "Hello" }],
    ["assistant.audio.end", {}],
    ["bargein", {}],
  ]);
});
