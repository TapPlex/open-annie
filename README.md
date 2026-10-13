<p align="center">
  <img src="docs/media/banner.png" alt="open-annie: the model that talks is not the model that moves" width="100%">
</p>

https://github.com/user-attachments/assets/a54b7354-3331-408f-9fdb-aa5be35c8392

<p align="center"><sub>Recorded live, with sound (1:37) · <a href="docs/media/open-annie-demo.mp4">download the mp4</a></sub></p>

**open-annie** is an open-source 3D character you talk to in the browser. Gemini 3.8 Live
handles realtime speech through a server-side Cloudflare AI Gateway relay. A second model moves her:
[Jev](https://typesafe.ai) reads the live transcript and picks Annie's face, gestures and body
language in about 120 ms. Her mouth is lip-synced from the audio you hear. Everything visual runs
in the browser, and the only server is a thin broker on [fal serverless](https://fal.ai/serverless).

The bundled recorded demo is one historical session, replayed frame for frame:

- **Annie** answered with GPT-Live-1 in that recording. Her words are her own.
- **You** are six scripted lines, performed by an AI voice (OpenAI `gpt-audio-1.5`, voice `cedar`)
  and spoken into a synthetic microphone.
- **Every face, gesture and dance** is a live Jev decision: 51 of them, p50 118 ms, $0.0026 of
  Jev for the whole conversation.

## Quick start

No keys needed to look around. From a clone of this repo:

```bash
python3 -m http.server 8080
```

| open | what you get |
|---|---|
| <http://localhost:8080/stage/> | the scripted preview: Annie's lines from Kokoro-82M, decisions from the local rule floor |
| <http://localhost:8080/stage/?replay=demo/live/session.json&decisions=demo/live/decisions.json> | the recorded live session from the video, with its real Jev decisions |

To talk to her, you need a Gemini API key and a [TypeSafe](https://typesafe.ai) key:

```bash
cp .env.example .env          # set the Gemini key, TypeSafe key and current audio-cost estimate
cd broker && uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python local.py     # the broker, on http://127.0.0.1:8787
```

Then open <http://localhost:8080/stage/?live=1&broker=http://127.0.0.1:8787> and allow the microphone.

| parameter | effect |
|---|---|
| `?avatar=vivi`, `?avatar=annie` | another avatar (see [assets/](assets/README.md)); the default is Annie without her jacket |
| `?outfit=full` | Annie with her jacket |
| `?lipsync=energy` | the energy-only mouth, without HeadAudio visemes |

## How it works

<p align="center">
  <img src="docs/media/architecture.png" alt="How open-annie works. Gemini Live audio is relayed through the broker and Cloudflare AI Gateway; the broker keeps the Gemini key server-side. The browser sends transcripts to the broker, which asks Jev typed questions and returns decisions. A local rule floor answers whenever Jev misses." width="100%">
</p>

1. **Voice.** Gemini Live audio is streamed over a WebSocket from the browser through the broker to
   Cloudflare AI Gateway. The Gemini API key is injected only into the broker; it is never sent to the browser.
2. **Instinct.** While *you* talk, Annie asks Jev one question: which face would a listening friend
   make? Her face changes before she answers. While *she* talks, transcript fragments (at most 4 asks
   a turn) ask five questions in one request: is she committing to an action, which clip, which
   reaction, which face, how much energy. The questions are in
   [`packages/core/src/instinct.js`](packages/core/src/instinct.js).
3. **Gates, not vibes.** Code keeps control. Jev only picks from clips that are installed, and every
   answer passes a confidence gate before it can act. Actions such as dancing need a spoken
   commitment, and a refusal ("no backflips, sorry") vetoes motion. Any Jev miss (timeout, 429,
   breaker, no key) falls back to a local rule floor, so she never waits.
4. **Lipsync from the audio.** Gemini Live audio has no visemes. HeadAudio (vendored, MIT) classifies 15
   visemes from the decoded audio every 16 ms, and `mouth.js` pools them into the avatar's mouth
   shapes with loudness from an energy follower. It is measured against phoneme ground truth in
   [`lipsync-eval/`](lipsync-eval/README.md).
5. **A body animated by hand.** Every clip is keyed on an IK control rig in headless Blender: idles,
   listening, Jev's clips (wave, dance, shrug, …), talk-rest loops and 32 co-speech gesture phrases.
   Each is collision-checked against Annie's deformed mesh and baked into a CC0 motion pack
   ([`motion/`](motion/README.md)). At runtime every switch is inertialized, gesture strokes land on
   the stressed syllables of her own voice, a per-frame collision guard keeps her hands out of her
   body, and her hair and skirt swing on substepped VRM spring bones.
6. **Faces with an envelope.** Expressions ease in and out per family (alert, warm, down, hot, calm),
   so her face reacts quickly without flickering.

### Deploy the broker on fal

```bash
fal secrets set ANNIE_GEMINI_API_KEY="$GEMINI_API_KEY" ANNIE_TYPESAFE_API_KEY="$TYPESAFE_API_KEY" \
  ANNIE_GEMINI_BASE_URL=https://gateway.ai.cloudflare.com/v1/ac4054842a60a05296897e5f7042b4cd/winbestsai/compat \
  ANNIE_VOICE_USD_PER_SECOND="$GEMINI_VOICE_USD_PER_SECOND" \
  ANNIE_HMAC_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
fal secrets set ANNIE_ALLOWED_ORIGINS=https://your-stage.example.com
fal deploy broker/fal_app.py::AnnieBroker --app-name annie-broker --auth public
curl https://fal.run/<you>/annie-broker/health
```

It runs on one always-warm CPU runner (`S`, `min_concurrency=1`, `keep_alive=300`,
`max_multiplexing=32`). It caps sessions at 180 s, closes after 30 s without speech, allows
2 sessions per IP per day and admits sessions against the configured estimated daily budget, and never forwards upstream error
bodies. Secrets are `ANNIE_`-prefixed because fal shares secrets across an account's apps. See
[broker/README.md](broker/README.md) and the verified upstream contracts in
[broker/UPSTREAM.md](broker/UPSTREAM.md).
Inject credentials from a secret manager rather than saving them in source or shell history. Rotate any
Gemini credential disclosed in chat, an issue or a log.

## Numbers

| | measured |
|---|---|
| The recorded session in the video (97 s, historical GPT-Live-1 + Jev take) | 51 decisions, all live Jev; p50 118 ms, p90 151 ms; $0.0026 of Jev |
| Jev upstream, 20 warm sequential requests | p50 108 ms, p90 149 ms; the broker adds ~1 ms |
| Gemini Live voice | Billing depends on audio-token usage; configure `ANNIE_VOICE_USD_PER_SECOND` as an admission-budget estimate |
| Body, over all 2,912 frames of the replay (`demo/bodyprobe.mjs`) | no hand or forearm more than 10 mm inside her body (worst 5.4 mm, one frame); 92% of gesture strokes within 100 ms of a voice onset, p50 26 ms |

## Repo

```
stage/              the page (no build step, CDN import map): scene, HUD, live mode, replay
packages/core/src/  character: motion, gesture timing, collision guard, cloth · faces · lipsync + mouth
                    instinct: Jev questions, pacer, gates · floor: rules · voices: Gemini Live, scripted, replay
broker/             the only server: Python core, fal App, local runner, tests, upstream notes
assets/             avatars and the motion pack, each with a licence manifest (scripts/check_assets.py)
motion/blender/     the Blender pipeline that authors the motion pack
persona/            character.md + voice.md: swap these to make a different character
demo/               the keyless preview's script and voice lines, the recorded session, QA probes
lipsync-eval/       offline lipsync measurement against phoneme ground truth
docs/               contracts.md (the interfaces) and README media (with their source pages)
```

## Privacy, safety and honesty

- No analytics. The broker never logs or retains transcripts. Mic audio goes through the broker to
  Cloudflare AI Gateway and Gemini; transcript text goes to TypeSafe.
- Annie says she is an AI in her first reply, keeps things suitable for general audiences, and
  points to 988 if someone mentions self-harm ([`persona/character.md`](persona/character.md)).
- An optional, off-by-default Rosemate toy integration lets Annie react to toy triggers. When configured, it adds
  Rosemate as a third service; see [`docs/rosemate.md`](docs/rosemate.md).
- Both voices in the demo video are AI-generated. The session itself is not edited: it is replayed
  frame by frame from the recording.

## Licenses

Code is Apache-2.0 ([LICENSE](LICENSE)). Assets carry their own licences, recorded per file with
hashes and checked in CI (see [NOTICE](NOTICE) and [assets/README.md](assets/README.md)):

- **Avatars:** pixiv's official VRoid sample models. AvatarSample_A and AvatarSample_B use pixiv's
  sample-model terms (free use, modification and redistribution; not CC0). Vivi is CC0.
- **Motion:** the `annie-blender` pack is authored in this repository and released CC0.
- **Audio:** the demo's audio is AI-generated: historical GPT-Live-1 and `gpt-audio-1.5` (OpenAI) for the recorded
  take and the user lines, Kokoro-82M (Apache-2.0) for the preview.

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Please report
security issues privately, as described in [SECURITY.md](SECURITY.md).

## Roadmap

- **An open voice on a fal GPU:** NVIDIA PersonaPlex-7B, full duplex with a text stream, behind the
  same voice interface. That gives Annie with no OpenAI at all.
- A hosted demo, foot locking, a Gemini Live voice provider, and per-voice lipsync calibration.

## Thanks

[three-vrm](https://github.com/pixiv/three-vrm), [VRoid](https://vroid.com) sample models by pixiv,
[TalkingHead / HeadAudio](https://github.com/met4citizen/TalkingHead), [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M),
and prior art in [ChatVRM](https://github.com/pixiv/ChatVRM), [AIRI](https://github.com/moeru-ai/airi)
and [jev-stage](https://github.com/GY19A/jev-stage).
