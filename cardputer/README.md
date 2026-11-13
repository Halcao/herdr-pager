# cardputer-pager

A Linux/keyboard client for herdr on the M5Stack CardputerZero (Debian 13,
aarch64, 320×170). It talks to herdr-pager's bridge on the Mac:

```sh
ssh -T mbp '~/.local/bin/pager-bridge stdio'
```

so there is no bridge SSH server, no USB pairing and no port 2277.

## Launch

- On the device: launcher → **Herdr Pager** (the desktop file is
  `/usr/share/APPLaunch/applications/cardputer-pager.desktop`). The launcher
  scans that directory at startup; reboot once if the icon is not there yet.
- From a Mac terminal (UI in the terminal, microphone still on the device):

  ```sh
  ssh -tt capu '~/.local/bin/cardputer-pager'
  ```

## Screen

The top band shows the target and screen (`herdr@mbp · agents` or
`· detail`) plus the clock. `LINK`, `OFF`, `HERDR OFFLINE`, `REC` and
`transcribing` replace it only when they matter.

The list is navigation only. It is sorted **blocked > done > working > idle**;
within a group the most recently active agent comes first (transcript mtime,
falling back to the observed state-change time). The title is the pane's
terminal title, full width; the old trailing folder column is gone.

```
                          12:34
◐ 新 Linux 小节点 SSH、tailscale…
○ 多模态模型训练负载均衡…
f/x select  Enter open  q quit
```

Press `Enter` on an agent to open its detail screen. The detail screen shows
the last assistant message (no thinking/tool calls), the permission/question
card if herdr has one, and is the only place that can record voice, send a
prompt, or approve/deny:

```
                          12:34
RESULT [transcript] gemini
最后一条 assistant 文本…
STATUS gemini
i int  j jump  Esc
```

Agents with a Claude session open in transcript mode; agents without one open
in terminal mode automatically. `t` switches between transcript and terminal;
`f/x` (or Up/Down) and `[` / `]` scroll the result. The transcript view is read
from Claude Code's session JSONL on the Mac by
[`herdr_final.py`](herdr_final.py), and the list's mtime sort uses
[`herdr_agents.py`](herdr_agents.py). Both run on the Mac; installing them is
one command each:

```sh
install -m 755 cardputer/herdr_final.py ~/.local/bin/herdr-final
install -m 755 cardputer/herdr_agents.py ~/.local/bin/herdr-agents
```

`herdr-agents` is polled once per five seconds in place of the previous
`herdr --machine … agent list`, so it adds no extra request. If it is missing,
the client falls back to the state-change time.

| glyph | meaning |
|---|---|
| `×` | blocked: needs your approval |
| `✓` | done since you last looked |
| `◐` | working |
| `○` | idle |
| `?` | unknown |

Older builds showed herdr's `workspace.tab` number (`7.1`, `8.1`); that was not
a name. The title comes from the pane's terminal.

## Keys

**List**

| key | action |
|---|---|
| `f` / `x` (or Up / Down) | move in the agent list |
| Enter | open the selected agent's detail |
| `q` | quit |

**Detail**

| key | action |
|---|---|
| `f` / `x` (or Up / Down), `[` / `]` | scroll the result |
| `t` | toggle the result: transcript ↔ terminal |
| `v` | start recording; press `v` again to stop |
| `p` | type a prompt (ASCII); Enter sends |
| `1`–`6` | arm a quick reply (继续 / 可以 / 不行 / commit and push / retry / 怎么样了); Enter sends, Esc cancels |
| `a` | approve (press twice), when the card offers it |
| `d` | deny |
| `i` | stop the current turn (interrupt the agent; press twice) |
| `j` | focus the agent's pane in herdr on the Mac |
| `r` | refresh the snapshot and the card |
| Esc | close the detail / cancel a quick reply or prompt |

## Voice

Config: `~/.config/herdr-pager-client/voice.json` (mode 0600). Current default
is `chat-audio` with `gemini-3.1-flash-lite` on `https://cnapi.kksj.org/v1`;
each dictation costs about $0.0001. The recording is converted to MP3 with
`ffmpeg` before upload (about 1/5 the bytes), then cleaned of spaces between
Chinese characters. Transient network resets are retried once, with a 45 s cap.
The built-in prompt follows OpenTypeless' correction rules (fillers, hotwords,
numbers, self-corrections, spacing); see [`voice.md`](voice.md).

After transcription the text stays in the prompt line: Enter sends it to the
selected agent, Esc cancels it. Pressing Esc while `transcribing` does not
close the detail; the prompt is kept.

The microphone is the ES8389 card:

```sh
arecord -D plughw:CARD=ES8389Audio,DEV=0 -f S16_LE -r 16000 -c 1 -d 3 /tmp/a.wav
```

## Test without the UI

```sh
cardputer-pager --probe                 # one herdr snapshot
cardputer-pager --voice-test            # record, transcribe, print
cardputer-pager --voice-test --wav x.wav
```

## Current scope

Implemented: status list with terminal titles; a detail screen per agent with
the last assistant text (transcript or terminal), the permission/question card,
voice-to-prompt, a typed prompt, quick replies, and approve/deny/interrupt/jump.

The list never sends anything; all message and decision actions live in the
detail screen. The result is fetched only while a detail screen is open.
