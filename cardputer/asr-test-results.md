# ASR test results — cnapi.kksj.org

Date: 2026-11-13
Endpoint: `https://cnapi.kksj.org/v1`
Key: stored only on capu at `~/.config/herdr-pager-client/voice.json` (0600); not in this file.
Audio: macOS `say -v Tingting`, converted to 16 kHz mono WAV.

## Clips

1. `你好，我是测试语音，请把这句话转成文字。` (148 KB, ~4.6 s)
2. `帮我把这个分支 rebase 到 main，然后跑一遍单元测试，如果失败就回滚。` (217 KB, ~6.7 s)
3. `今天下午三点开会，记得把上周的性能数据整理成表格发给我。` (198 KB, ~6.2 s)

## Model sweep, clip 1 (OpenAI-compatible chat + input_audio)

| model | raw result |
|---|---|
| gemini-3.5-flash | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-3.8-flash | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-3.7-flash | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-3.1-flash-lite | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-3.5-flash-lite | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-3-flash-preview | `你好，我是测试语音，请把这句话转成文字。` |
| gemini-4-argon | `你好我是测试语音请把这句话转成文字` (no punctuation) |
| gemini-3.6-flash | transient `SSL_ERROR_SYSCALL` |
| gemini-2.5-flash | `当前分组 gemini-cli 下对于模型 gemini-2.5-flash 无可用渠道` |
| gpt-4o-mini-transcribe | HTTP 429 `upstream_rate_limit` |
| gpt-4o-transcribe | HTTP 429 `upstream_rate_limit` |
| whisper-1 | HTTP 429 `upstream_rate_limit` |
| gpt-4o-mini-audio-preview | HTTP 429 / empty reply |
| advanced-voice | `{"error":{"message":"not support","type":"upstream_error"}}` |

## Three-clip sweep (Mac, via proxy)

| clip | model | raw transcript | tokens in/out | time |
|---|---|---|---|---|
| 1 | gemini-3.1-flash-lite | `curl: (52) Empty reply from server` | — | 9.80 s |
| 1 | gemini-3.5-flash-lite | `你好，我是测试语音，请把这句话转成文字。` | 127 / 13 | 9.58 s |
| 2 | gemini-3.1-flash-lite | `帮我把这个分支 rebase 到 main，然后跑一遍单元测试，如果失败就回滚。` | 183 / 22 | 12.00 s |
| 2 | gemini-3.5-flash-lite | `帮 我 把 这 个 分 支 rebaset 到 main ， 然 后 跑 一 遍 单 元 测 试 ， 如 果 失 败 就 回 滚 。` | 183 / 38 | 8.33 s |
| 3 | gemini-3.1-flash-lite | `今 天 下 午 3 点 开 会 ， 记 得 把 上 周 的 性 能 数 据 整 理 成 表 格 发 给 我 。` | 168 / 30 | 10.99 s |
| 3 | gemini-3.5-flash-lite | `curl: (52) Empty reply from server` | — | 13.52 s |

`rebaset` in the 3.5-flash-lite clip-2 output is a real transcription error.
The empty replies are network/proxy failures, not model refusals: the same
model succeeded on retry.

## capu direct (no proxy), gemini-3.1-flash-lite

| clip | raw transcript | time |
|---|---|---|
| 2 | `帮 我 把 这 个 分 支 rebase 到 main ， 然 后 跑 一 遍 单 元 测 试 ， 如 果 失 败 就 回 滚 。` | 8.65 s |
| 3 | `今天 下午 三点 开会 ， 记得 把 上周 的 性能 数据 整理 成 表格 发给 我 。` | 5.42 s |

## Billing shown by the gateway (user's console)

| model | price / 1M in / out | one request | model time |
|---|---|---|---|
| gemini-3.1-flash-lite | $0.5 / $3 | $0.000092 | 4–5 s |
| gemini-3.5-flash-lite | $0.6 / $5 | $0.000128 | 2–4 s |
| gemini-3-flash-preview | $1 / $6 | $0.000184 | 2 s |
| gemini-4-argon | $4 / $20 | $0.000944 | 6 s |
| gemini-3.7-flash | $1.5 / $7.5 | $0.001306 | 2–24 s |
| gemini-3.8-flash | $1.5 / $7.5 | $0.001488 | 2–3 s |
| gemini-3.5-flash | $3 / $18 | $0.003956 | 6 s |
| gpt-4o-mini-transcribe | $4.5 / $18 | unavailable (429) | — |
| gpt-4o-transcribe | $9 / $36 | unavailable (429) | — |
| whisper-1 | $27 / $27 | unavailable (429) | — |

## Caveats

- All clips are synthetic TTS, not real microphone/room speech. The capu mic
  itself was verified (`arecord`, 1 s WAV), but no human speech was recorded.
- The extra spaces between Chinese characters are a model formatting quirk;
  the client now strips them before filling the prompt.
- Empty replies / SSL errors are transport-level; retry usually succeeds.
