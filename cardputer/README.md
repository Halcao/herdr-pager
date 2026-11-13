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

The list shows the agent's **terminal title** (from `herdr agent list`'s
`terminal_title_stripped`), its folder, and a status glyph:

```
OK                                12:34
×0 blocked ✓1 done ◐1 working ○6 idle
◐ 新 Linux 小节点 SSH、t   haocao
○ 多模态模型训练负载均衡  minicpm-lb
Up/Down  Enter open  v voice  q quit
```

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

| key | action |
|---|---|
| Up / Down | move in the agent list |
| Enter | open the selected agent's card |
| `v` | start recording; press `v` again to stop; then wait for the transcript |
| Enter | send the transcribed/typed prompt to the selected agent |
| Esc | cancel the prompt / close the card |
| `a` | approve (press twice) |
| `d` | deny |
| `i` | interrupt (press twice) |
| `j` | jump to the pane in herdr |
| `r` | request a fresh snapshot |
| `q` | quit |

## Voice

Config: `~/.config/herdr-pager-client/voice.json` (mode 0600). Current default
is `chat-audio` with `gemini-3.1-flash-lite` on `https://cnapi.kksj.org/v1`;
each dictation costs about $0.0001. The recording is converted to MP3 with
`ffmpeg` before upload (about 1/5 the bytes), then cleaned of spaces between
Chinese characters. Transient network resets are retried once, with a 45 s cap.
The built-in prompt follows OpenTypeless' correction rules (fillers, hotwords,
numbers, self-corrections, spacing); see [`voice.md`](voice.md).

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

Implemented: agent list/status, permission cards, approve/deny/interrupt/jump,
voice-to-prompt, prompt send via `herdr agent prompt`.

Not yet implemented: the "last assistant text" result view and the
transcript/terminal toggle. `Enter` on an agent still opens the pager card
(status/question/permission), not the final answer.
