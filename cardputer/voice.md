# Voice input for cardputer-pager

The client records from the CardputerZero's ES8389 microphone with `arecord`,
then sends the WAV to whatever the config says. Nothing is recorded unless you
press `v`.

Config lives at `~/.config/herdr-pager-client/voice.json`. The file is created
once, with `enabled: false`; edit it on the device:

```sh
nano ~/.config/herdr-pager-client/voice.json
```

## Prompt

The default is a **concise verbatim transcriber**, deliberately short to avoid
the model "correcting" or rewriting speech:

- transcribe to Simplified Chinese; output only the transcript
- keep English terms, identifiers, paths, commands, flags and URLs as spoken
- do not answer, summarize, complete or rewrite what the user did not say
- no commentary, markdown or code fences

A few deterministic hotword fixes run after transcription (`cloud code` →
`Claude Code`, `code x` → `Codex`, `herdr pager` → `herdr-pager`). These are
post-processing, not prompt rules, so they don't bias the ASR.

`"prompt": ""` uses the built-in. Put the longer OpenTypeless/Ovis correction
rules in that field if you want them. `prompt_mode` is `chat` (a message to a
coding agent) or `terminal` (a shell command: no trailing period). Set
`keep_audio: true` to copy the last recording to
`~/.cache/cardputer-voice/last.wav` for debugging.

## OpenTypeless (OpenAI-compatible ASR)

[OpenTypeless](https://github.com/ckckck/OpenTypeless) wraps Doubao ASR as an
OpenAI-compatible server:

- `POST /v1/audio/transcriptions`
- default address `http://127.0.0.1:8836`, so the base_url is `.../v1`
- models: `doubao-asr`, `doubao-asr-official`, `doubao-asr-official-standard`,
  `doubao-asr-official-flash` (recommended)

If it runs on the Mac (Docker), capu must be able to reach it:

```yaml
# docker-compose.yml
ports:
  - "0.0.0.0:8836:8836"
```

then in `voice.json`:

```json
{
  "enabled": true,
  "mode": "openai",
  "base_url": "http://100.64.0.30:8836/v1",
  "api_key": "",
  "model": "doubao-asr-official-flash",
  "language": "zh",
  "record_seconds": 6,
  "record_device": "plughw:CARD=ES8389Audio,DEV=0"
}
```

Alternatively keep the server on `127.0.0.1:8836` on the Mac and run an SSH
tunnel on capu:

```sh
ssh -N -L 8836:127.0.0.1:8836 mbp
```

## OpenAI-compatible chat audio (`mode: chat-audio`)

Some gateways (including the KKSJ one) expose Gemini/omni models only through
`/v1/chat/completions` with base64 `input_audio`, not `/v1/audio/transcriptions`.
This mode sends the WAV that way and reads `choices[0].message.content`.

Tested on `https://cnapi.kksj.org/v1` with a Chinese TTS clip:

| model | result | billed tokens | cost / request |
|---|---|---|---|
| `gemini-3.1-flash-lite` | OK | 127 / 13 | $0.000092 |
| `gemini-3.5-flash-lite` | OK | 127 / 13 | $0.000128 |
| `gemini-3-flash-preview` | OK | 127 / 13 | $0.000184 |
| `gemini-3.7-flash` | OK | 127 / 168 | $0.001306 |
| `gemini-3.8-flash` | OK | 127 / 195 | $0.001488 |
| `gemini-4-argon` | OK | 192 / 14 | $0.000944 |
| `gemini-3.5-flash` | OK | 127 / 223 | $0.003956 |
| `gpt-4o-mini-transcribe` | 429 upstream_rate_limit | — | — |
| `gpt-4o-transcribe` | 429 upstream_rate_limit | — | — |
| `whisper-1` | 429 upstream_rate_limit | — | — |
| `advanced-voice` | `not support` on transcriptions | — | — |

Config:

```json
{
  "enabled": true,
  "mode": "chat-audio",
  "base_url": "https://cnapi.kksj.org/v1",
  "api_key": "sk-...",
  "model": "gemini-3.1-flash-lite",
  "language": "zh",
  "record_seconds": 6,
  "record_device": "plughw:CARD=ES8389Audio,DEV=0",
  "prompt": "Transcribe the audio verbatim. Output only the transcript."
}
```

The three `*-flash-lite` models are the cost-efficient choices; `gemini-3.5-flash`
is ~40× more expensive per dictation. The OpenAI/Whisper models are present in
the model list but their upstream pool is currently saturated (HTTP 429), so
they are not usable from this group right now.

## Any CLI (`mode: command`)

If you prefer a local `whisper.cpp`, a script, or a different cloud CLI:

```json
{
  "enabled": true,
  "mode": "command",
  "command": "whisper-cli -m /home/halcao/models/ggml-small.bin -l zh -otxt -of /tmp/out {wav} >/dev/null && cat /tmp/out.txt"
}
```

`{wav}` is replaced by the recorded file path (shell-quoted). If `command` has
no `{wav}`, the path is appended. The command's stdout is the transcript.

## Test without the UI

```sh
cardputer-pager --voice-test
```

It records for `record_seconds`, prints the transcript, and exits. The first
run may need `arecord` to pick the right card; the device is card 1
(`ES8389Audio`).
