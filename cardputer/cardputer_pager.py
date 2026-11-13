#!/usr/bin/env python3
"""cardputer-pager — a keyboard client for herdr-pager's bridge, for CardputerZero.

This is a Linux port of the interaction model of MaverickHQ/herdr-pager, not a
port of its ESP32 firmware: the bridge (Go, on the Mac) keeps all the herdr and
safety logic, and this client speaks the same pager protocol over

    ssh -T <host> '~/.local/bin/pager-bridge stdio'

Using `stdio` means no bridge SSH server, no USB pairing and no port 2277: the
existing SSH key between the CardputerZero and the Mac is the only credential.

Screen: 40x21 at the device's default 8x8 font (320x170). The layout adapts to
any terminal at least 40x10; the vendor launcher's terminal page (45x10) works.

Keys
  Up/Down     move in the agent list
  Enter       open the selected agent's card
  a           approve (press twice: arm, then confirm)
  d           deny
  i           interrupt (press twice)
  j           jump to the pane in herdr
  r           ask for a fresh snapshot
  v           record + transcribe, then edit/send as a prompt
  Esc         close the card / cancel an armed key / cancel the prompt
  q           quit
"""

import argparse
import base64
import errno
import json
import os
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata

# ---- protocol ---------------------------------------------------------------

PROTO_VERSION = 1
CLIENT_FW = "1.0.1-cardputer"
MAX_LINE = 8192

GLYPH = {
    "blocked": "×",
    "done": "✓",
    "working": "◐",
    "unknown": "?",
    "idle": "○",
}

QUICK_REPLIES = ["继续", "可以", "不行", "commit and push", "retry", "怎么样了"]
QUICK_SHORT = ["继续", "可以", "不行", "commit", "retry", "怎样"]
STATUS_RANK = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4}
HOTWORD_MAP = {
    "cloud code": "Claude Code",
    "扣的code": "Claude Code",
    "code x": "Codex",
    "herdr pager": "herdr-pager",
}

MENU_RE = re.compile(r"^\s*([>❯])?\s*(\d+)[.)]\s+(.*)$")
HINT_RE = re.compile(
    r"(?i)enter to select|↑/↓|esc to cancel|shift\+tab|ctrl\+g|to navigate|to amend"
)


def parse_form(text):
    """Extract the last numbered menu from herdr's detection snapshot."""
    lines = (text or "").splitlines()
    groups = []
    cur = []
    for i, raw in enumerate(lines):
        m = MENU_RE.match(raw)
        if m:
            cur.append({
                "idx": i,
                "marker": bool(m.group(1)),
                "n": int(m.group(2)),
                "label": m.group(3).strip(),
                "desc": [],
            })
        elif raw.strip().startswith(("─", "╌")):
            continue
        elif cur and raw.strip() and not HINT_RE.search(raw):
            if raw.startswith((" ", "\t")):
                cur[-1]["desc"].append(raw.strip())
            else:
                groups.append(cur)
                cur = []
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    for g in reversed(groups):
        if len(g) >= 2:
            selected = next((k for k, o in enumerate(g) if o["marker"]), 0)
            title = ""
            for j in range(g[0]["idx"] - 1, max(-1, g[0]["idx"] - 8), -1):
                t = lines[j].strip()
                if t and not t.startswith(("─", "╌")) and not MENU_RE.match(lines[j]):
                    title = t
                    break
            options = [{
                "n": o["n"],
                "label": o["label"],
                "desc": " ".join(o["desc"]),
            } for o in g]
            return options, title, selected
    return [], "", 0
STATUS_COLOR = {
    "blocked": "red",
    "done": "cyan",
    "working": "yellow",
    "unknown": "grey",
    "idle": "grey",
}
ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "rev": "\x1b[7m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
    "white": "\x1b[37m",
    "grey": "\x1b[90m",
}


def cfg_path():
    return os.path.expanduser("~/.config/herdr-pager-client/config.json")


def load_config():
    defaults = {
        "host": "mbp",
        "bridge": "~/.local/bin/pager-bridge stdio",
        "name": "cardputer0",
        "herdr_machine": "mbp",
    }
    try:
        with open(cfg_path()) as f:
            defaults.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"config: {e}", file=sys.stderr)
    return defaults


# ---- voice ------------------------------------------------------------------

VOICE_PATH = "~/.config/herdr-pager-client/voice.json"
VOICE_DEFAULTS = {
    "enabled": False,
    "mode": "openai",              # openai | chat-audio | command
    "base_url": "http://127.0.0.1:8836/v1",
    "api_key": "",
    "model": "doubao-asr-official-flash",
    "language": "zh",
    "record_seconds": 6,
    "record_mode": "toggle",       # toggle | fixed
    "max_record_seconds": 15,
    "audio_format": "mp3",         # mp3 (ffmpeg) | wav
    "record_device": "plughw:CARD=ES8389Audio,DEV=0",
    "command": "",
    "prompt": "",
    "prompt_mode": "chat",         # chat | terminal
    "keep_audio": False,
    "proxy": "",
}

DEFAULT_PROMPTS = {
    "chat": (
        "你是中文语音听写器。把音频转写成简体中文，只输出转写文本，不要回答、解释或执行内容。"
        "保留英文技术词、代码标识符、文件路径、命令、参数和 URL 的原写法，不要翻译或改写。"
        "不要补全、总结或改写用户没说过的内容。如果音频里没有可识别语音，输出空字符串。"
    ),
    "terminal": (
        "你是终端命令听写器。把音频转成一条可直接执行的 shell 命令，只输出命令本身。\n"
        "保留路径、参数、引号、管道、重定向和英文标识符原样；删除语气词；"
        "不要加句号、解释、markdown 或代码围栏。\n"
        "如果音频里没有可识别语音，输出空字符串。"
    ),
}


def build_prompt(cfg):
    mode = cfg.get("prompt_mode", "chat")
    prompt = cfg.get("prompt") or DEFAULT_PROMPTS.get(mode, DEFAULT_PROMPTS["chat"])
    if cfg.get("language"):
        prompt += f"\n输出语言：{cfg['language']}（中文用简体，英文技术词保留英文）。"
    return prompt


def load_voice_config():
    cfg = dict(VOICE_DEFAULTS)
    try:
        with open(os.path.expanduser(VOICE_PATH)) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"voice config: {e}", file=sys.stderr)
    return cfg


def record_wav(path, cfg):
    secs = int(cfg.get("record_seconds", 6))
    cmd = [
        "arecord", "-q",
        "-D", cfg.get("record_device", VOICE_DEFAULTS["record_device"]),
        "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(secs), path,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=secs + 15)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "arecord failed").strip()[:200])
    return path


def tidy_transcript(text, cfg=None):
    """Gemini sometimes puts a space between every Chinese character."""
    text = re.sub(r"\s+", " ", text or "").strip()
    for wrong, right in HOTWORD_MAP.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text)
    text = re.sub(r"\s+(?=[，。！？；：、）】》])", "", text)
    text = re.sub(r"(?<=[（【《])\s+", "", text)
    if (cfg or {}).get("prompt_mode", "chat") == "chat":
        # reference style: one half-width space between Chinese and Latin/digits
        text = re.sub(r"(?<=[A-Za-z0-9])(?=[一-鿿])", " ", text)
        text = re.sub(r"(?<=[一-鿿])(?=[A-Za-z0-9])", " ", text)
        text = re.sub(r"\s{2,}", " ", text)
        text = re.sub(r"\s+(?=[，。！？；：、）】》])", "", text)
    return text.strip()


def prepare_audio(path, cfg):
    """Convert the recording to MP3 when ffmpeg is available: 1/5 the upload."""
    if cfg.get("audio_format", "mp3") != "mp3" or not shutil.which("ffmpeg"):
        return path, "wav", None
    out = path + ".mp3"
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-codec:a", "libmp3lame", "-q:a", "4", out],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return out, "mp3", out
    except Exception:
        pass
    return path, "wav", None


def transcribe_wav(path, cfg):
    mode = cfg.get("mode", "openai")
    if mode == "command":
        cmd = (cfg.get("command") or "").strip()
        if not cmd:
            raise RuntimeError("voice.command is empty")
        quoted = shlex.quote(path)
        cmd = cmd.replace("{wav}", quoted) if "{wav}" in cmd else f"{cmd} {quoted}"
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "command failed").strip()[:200])
        return tidy_transcript(p.stdout, cfg)

    audio, fmt, tmp = prepare_audio(path, cfg)
    try:
        if mode == "chat-audio":
            base = (cfg.get("base_url") or "").rstrip("/")
            if not base:
                raise RuntimeError("voice.base_url is empty")
            url = base + "/chat/completions"
            with open(audio, "rb") as f:
                payload = base64.b64encode(f.read()).decode()
            prompt = build_prompt(cfg)
            body = {
                "model": cfg.get("model") or "gemini-3.1-flash-lite",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "input_audio", "input_audio": {"data": payload, "format": fmt}},
                    ],
                }],
            }
            cmd = [
                "curl", "-sS", "--http1.1",
                "--connect-timeout", "8", "--retry", "1",
                "--retry-all-errors", "--retry-delay", "1", "-m", "45",
                "-H", "Content-Type: application/json",
            ]
            if cfg.get("proxy"):
                cmd += ["-x", cfg["proxy"]]
            if cfg.get("api_key"):
                cmd += ["-H", f"Authorization: Bearer {cfg['api_key']}"]
            cmd += ["-d", "@-", url]
            p = subprocess.run(cmd, input=json.dumps(body), capture_output=True, text=True, timeout=100)
            out = (p.stdout or "").strip()
            if p.returncode != 0:
                msg = (p.stderr or out or "curl failed").strip()
                if p.returncode in (28, 56):
                    msg = "network timeout/reset — press v to retry"
                raise RuntimeError(msg[:200])
            try:
                d = json.loads(out)
            except json.JSONDecodeError:
                raise RuntimeError(out[:200])
            if isinstance(d, dict) and d.get("error"):
                raise RuntimeError(str(d["error"])[:200])
            try:
                return tidy_transcript(d["choices"][0]["message"]["content"] or "", cfg)
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(str(d)[:200])

        base = (cfg.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("voice.base_url is empty")
        url = base + "/audio/transcriptions"
        cmd = [
            "curl", "-sS", "--http1.1",
            "--connect-timeout", "8", "--retry", "1",
            "--retry-all-errors", "--retry-delay", "1", "-m", "45",
            "-F", f"file=@{audio}",
            "-F", f"model={cfg.get('model') or 'whisper-1'}",
        ]
        if cfg.get("proxy"):
            cmd += ["-x", cfg["proxy"]]
        if cfg.get("language"):
            cmd += ["-F", f"language={cfg['language']}"]
        if cfg.get("api_key"):
            cmd += ["-H", f"Authorization: Bearer {cfg['api_key']}"]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=100)
        out = (p.stdout or "").strip()
        if p.returncode != 0:
            msg = (p.stderr or out or "curl failed").strip()
            if p.returncode in (28, 56):
                msg = "network timeout/reset — press v to retry"
            raise RuntimeError(msg[:200])
        try:
            d = json.loads(out)
            if isinstance(d, dict) and d.get("error"):
                raise RuntimeError(str(d["error"])[:200])
            return tidy_transcript(d.get("text") or "", cfg)
        except json.JSONDecodeError:
            return tidy_transcript(out, cfg)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def voice_test(cfg, wav=None):
    if not cfg.get("enabled") and not wav:
        print(f"voice disabled; edit {VOICE_PATH}", file=sys.stderr)
        return 1
    recorded = wav is None
    path = wav or f"/tmp/cardputer-voice-{os.getpid()}.wav"
    try:
        if recorded:
            record_wav(path, cfg)
            if cfg.get("keep_audio"):
                dest = os.path.expanduser("~/.cache/cardputer-voice/last.wav")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(path, dest)
        print(transcribe_wav(path, cfg))
        return 0
    except Exception as e:
        print(f"voice: {e}", file=sys.stderr)
        return 1
    finally:
        if recorded:
            try:
                os.remove(path)
            except OSError:
                pass


# ---- rendering --------------------------------------------------------------

def _cw(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _dw(s):
    return sum(_cw(ch) for ch in (s or ""))


def clip(s, n):
    s = s or ""
    out = []
    used = 0
    for ch in s:
        c = _cw(ch)
        if used + c > n:
            break
        out.append(ch)
        used += c
    return "".join(out)


def pad(s, n):
    s = clip(s, n)
    return s + " " * max(0, n - _dw(s))


def wrap_text(text, width):
    lines = []
    for para in (text or "").splitlines() or [""]:
        cur = ""
        used = 0
        for ch in para:
            c = _cw(ch)
            if used + c > width and cur:
                lines.append(cur)
                cur = ""
                used = 0
            cur += ch
            used += c
        lines.append(cur.rstrip())
    return lines


def color(text, name):
    if not name:
        return text
    return ANSI.get(name, "") + text + ANSI["reset"]


def line(parts, width):
    out = []
    used = 0
    for text, col in parts:
        text = clip(text, width - used)
        if not text:
            continue
        out.append(color(text, col))
        used += _dw(text)
    if used < width:
        out.append(" " * (width - used))
    return "".join(out)


class UI:
    def __init__(self, host):
        self.host = host
        self.text = []
        self.changed = True

    def draw(self, state):
        cols, rows = shutil.get_terminal_size((40, 21))
        w = max(40, min(cols, 48))
        h = max(10, rows)
        self.text = render(state, w, h)
        sys.stdout.write("\x1b[?25l\x1b[H")
        for i in range(h):
            row = self.text[i] if i < len(self.text) else ""
            sys.stdout.write(row + "\x1b[K")
            if i != h - 1:
                sys.stdout.write("\r\n")
        sys.stdout.flush()
        self.changed = False


def render(st, w, h):
    snap = st.snap or {}
    agents = snap.get("agents", [])
    herdr_off = snap.get("herdr") == "offline"

    # status band: only abnormal/transient states, no permanent OK
    left = []
    if st.link == "connecting":
        left.append(("LINK", "yellow"))
    elif st.link != "connected":
        left.append(("OFF", "red"))
    else:
        left.append((f"herdr@{st.host} · {'detail' if st.detail_pane else 'agents'}", "grey"))
    if herdr_off:
        left.append((" HERDR OFFLINE", "red"))
    if st.msg:
        left.append((" " + st.msg, "yellow"))
    if st.voice_state != "idle" or st.voice_msg:
        left.append((" " + st.voice_msg, "cyan"))
    clock = time.strftime("%H:%M", time.localtime(st.now or time.time()))
    band = line(left, w - 6) + color(pad(clock, 5), "grey") + " "

    out = [band]

    if st.detail_pane:
        pane = st.detail_pane
        sel_title = st.titles.get(pane) or st.agent_name(pane) or pane
        if st.form_options and st.form_pane == pane:
            title = st.form_title or ((st.card or {}).get("summary") or "")
            out.append(color(clip("FORM  " + title, w), "cyan"))
            form_h = max(1, h - 3)
            for i, o in enumerate(st.form_options[:form_h]):
                marker = "❯" if i == st.form_selected else " "
                text = f"{marker} {o['n']}. {o['label']}"
                if o.get("desc"):
                    text += "  " + o["desc"]
                out.append(line([(text, "rev" if i == st.form_selected else "white")], w))
            hint = "f/x move  Enter select  Esc back"
            out = out[:h - 1]
            while len(out) < h - 1:
                out.append("")
            out.append(color(pad(hint, w), "grey"))
            return out[:h]

        card_h = max(1, min(3, (h - 7) // 3)) if st.card else 0
        card_block = (1 + card_h) if st.card else 0
        prompt_lines = []
        prompt_h = 0
        if st.prompt_text is not None:
            prompt_lines = wrap_text(st.prompt_text, max(1, w - 2)) or [""]
            prompt_h = min(len(prompt_lines), max(1, h - 4 - card_block))
            result_h = max(1, h - 3 - card_block - prompt_h)
        else:
            result_h = max(1, h - 4 - card_block)

        if st.result_pane != pane:
            result = "loading…" if st.result_loading else "(loading…)"
        else:
            result = st.result_text or "(no result yet)"
        lines = wrap_text(result, w)
        max_scroll = max(0, len(lines) - result_h)
        st.result_scroll = max(0, min(st.result_scroll, max_scroll))
        pos = f" {st.result_scroll + 1}/{max_scroll + 1}" if max_scroll else ""
        out.append(color(clip(f"RESULT [{st.result_mode}{pos}] {sel_title}", w), "cyan"))
        for ln in lines[st.result_scroll:st.result_scroll + result_h]:
            out.append(line([(ln, "white")], w))

        card_hint = ""
        if st.card:
            card = st.card
            title = card.get("kind", "").upper()
            if card.get("risk") == "danger":
                title = "DANGER: " + (card.get("rl") or "RISKY")
                tcol = "red"
            elif card.get("kind") == "permission":
                title = "PERMISSION"
                tcol = "yellow"
            elif card.get("kind") == "question":
                title = "QUESTION"
                tcol = "cyan"
            else:
                title = "STATUS"
                tcol = "white"
            out.append(line([(title, tcol), ("  " + sel_title, "grey")], w))
            card_lines = []
            if card.get("summary"):
                card_lines.append(card["summary"])
            card_lines.extend(card.get("lines") or [])
            for ln in card_lines[:card_h]:
                out.append(line([(ln, "white")], w))
            can = card.get("can") or []
            hints = []
            if "approve" in can:
                hints.append("a" + (" APPROVE?" if st.armed == "approve" else " approve"))
            if "deny" in can:
                hints.append("d deny")
            if "interrupt" in can:
                hints.append("i" + (" STOP?" if st.armed == "interrupt" else " stop"))
            if "jump" in can:
                hints.append("j focus")
            card_hint = "  ".join(hints)

        if st.prompt_text is not None:
            start = max(0, len(prompt_lines) - prompt_h)
            for j, ln in enumerate(prompt_lines[start:start + prompt_h]):
                if start == 0:
                    prefix = "> " if j == 0 else "  "
                else:
                    prefix = "… " if j == 0 else "  "
                out.append(line([(prefix + ln, "rev")], w))
        else:
            quick_bar = " ".join(f"{i + 1}{x}" for i, x in enumerate(QUICK_SHORT))
            out.append(color(clip(quick_bar, w), "grey"))
    else:
        body_h = h - 2
        if st.link == "connecting":
            out.append(line([("connecting…", "yellow")], w))
        elif st.link != "connected":
            out.append(line([("disconnected — retrying…", "red")], w))
        elif herdr_off:
            out.append(line([("herdr is offline on " + st.host, "red")], w))
        elif not agents:
            out.append(line([("no agents", "grey")], w))
        else:
            vis = body_h
            first = st.first_visible(len(agents), vis)
            for i in range(first, min(len(agents), first + vis)):
                a = agents[i]
                stt = a.get("st", "unknown")
                g = GLYPH.get(stt, "?")
                title = st.titles.get(a.get("pane")) or a.get("name") or a.get("pane", "")
                row = color(pad(g, 1), STATUS_COLOR.get(stt, "white")) + " " + pad(title, w - 2)
                if i == st.selected:
                    row = color(row, "rev")
                out.append(row)

    # hint bar, pinned to the last row
    if st.voice_state == "recording":
        hint = "REC — v stop   Esc cancel"
    elif st.voice_state == "transcribing":
        hint = "transcribing..."
    elif st.prompt_text is not None:
        hint = "Enter send   Esc cancel"
    elif st.quick:
        hint = f'Enter send "{st.quick}"   Esc cancel'
    elif st.detail_pane:
        if card_hint:
            hint = "f/x scroll  " + card_hint + "  Esc"
        else:
            hint = "f/x scroll  t view  v/p 1-6  Esc back"
    else:
        hint = "f/x select  Enter open  q quit"
    if st.msg and time.time() - st.msg_at > 4:
        st.msg = ""
    out = out[: h - 1]
    while len(out) < h - 1:
        out.append("")
    out.append(color(pad(hint, w), "grey"))
    return out[:h]


# ---- state ------------------------------------------------------------------

class State:
    def __init__(self, host, name):
        self.host = host
        self.name = name
        self.link = "down"
        self.snap = None
        self.card = None
        self.selected = 0
        self.first = 0
        self.armed = None
        self.armed_at = 0.0
        self.msg = ""
        self.msg_at = 0.0
        self.now = 0
        self.seen_blocked = set()
        self.seen_done = set()
        self.pending = {}
        self.titles = {}
        self.sessions = {}
        self.state_times = {}
        self.last_state = {}
        self.detail_pane = None
        self.result_text = ""
        self.result_pane = None
        self.result_mode = "recent"
        self.result_at = 0.0
        self.result_scroll = 0
        self.result_loading = False
        self.form_title = ""
        self.form_options = []
        self.form_selected = 0
        self.form_pane = None
        self.form_at = 0.0
        self.form_key_at = 0.0
        self.form_miss = 0
        self.quick = None
        self.prompt_text = None
        self.prompt_pane = None
        self.voice_state = "idle"   # idle | recording | transcribing
        self.voice_msg = ""

    def say(self, text):
        self.msg = text
        self.msg_at = time.time()

    def update_times(self, agents, now):
        for a in agents:
            pane = a.get("pane")
            if not pane:
                continue
            sig = (a.get("st"), a.get("seq"))
            if pane not in self.state_times:
                self.state_times[pane] = a.get("since") or now
            elif self.last_state.get(pane) != sig:
                self.state_times[pane] = now
            self.last_state[pane] = sig

    def count(self, kind):
        if not self.snap:
            return 0
        return sum(1 for a in self.snap.get("agents", []) if a.get("st") == kind)

    def agent_name(self, pane):
        if pane in self.titles:
            return self.titles[pane]
        if not self.snap:
            return ""
        for a in self.snap.get("agents", []):
            if a.get("pane") == pane:
                return a.get("name", "")
        return ""

    def age(self, since):
        if not since:
            return ""
        d = int(self.now - since)
        if d < 0:
            d = 0
        if d < 60:
            return f"{d}s"
        if d < 3600:
            return f"{d // 60}m"
        return f"{d // 3600}h"

    def first_visible(self, n, vis):
        if self.selected < self.first:
            self.first = self.selected
        if self.selected >= self.first + vis:
            self.first = self.selected - vis + 1
        if self.first > max(0, n - vis):
            self.first = max(0, n - vis)
        return self.first


# ---- client -----------------------------------------------------------------

class Pager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.st = State(cfg["host"], cfg["name"])
        self.ui = UI(cfg["host"])
        self.proc = None
        self.buf = b""
        self.next_id = 1
        self.next_ping = 0
        self.retry_at = 0
        self.running = True
        self.old_term = None
        self.voice = load_voice_config()
        self.voice_proc = None
        self.voice_cancel = False
        self.key_q = queue.Queue()

    # -- ssh link --

    def _title_poller(self):
        herdr = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
        machine = self.cfg.get("herdr_machine", "mbp")
        while self.running:
            try:
                remote = (
                    "~/.local/bin/herdr agent list; echo __MTIMES__; "
                    "find \"$HOME/.claude/projects\" -name '*.jsonl' "
                    "-exec stat -f '%m %N' {} +"
                )
                p = subprocess.run(
                    ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
                     self.cfg["host"], remote],
                    capture_output=True, text=True, timeout=25,
                )
                agents = []
                mtimes = {}
                if p.returncode == 0 and p.stdout.strip():
                    head, _, tail = p.stdout.partition("__MTIMES__")
                    try:
                        d = json.loads(head.strip())
                        agents = d.get("result", {}).get("agents", [])
                    except Exception:
                        agents = []
                    for line in tail.splitlines():
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2 and parts[0].isdigit():
                            fname = os.path.basename(parts[1].strip())
                            if fname.endswith(".jsonl"):
                                mtimes[fname[:-6]] = int(parts[0])
                if not agents:
                    p = subprocess.run(
                        [herdr, "--machine", machine, "agent", "list"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if p.returncode == 0 and p.stdout.strip():
                        d = json.loads(p.stdout)
                        agents = d.get("result", {}).get("agents", [])
                for a in agents:
                    sess = (a.get("agent_session") or {}).get("value")
                    a["mtime"] = mtimes.get(sess, 0) if sess else 0
                if agents:
                    titles = {}
                    sessions = {}
                    for a in agents:
                        pane = a.get("pane_id")
                        title = (a.get("terminal_title_stripped") or "").strip()
                        if not title:
                            cwd = (a.get("cwd") or "").rstrip("/")
                            title = os.path.basename(cwd) or pane
                        if pane:
                            titles[pane] = title
                            sess_obj = a.get("agent_session") or {}
                            sessions[pane] = {
                                "session": sess_obj.get("value") if isinstance(sess_obj, dict) else None,
                                "agent": a.get("agent") or "",
                                "cwd": a.get("cwd") or "",
                                "mtime": a.get("mtime") or 0,
                            }
                    self.st.titles = titles
                    self.st.sessions = sessions
                    if self.st.snap and self.st.snap.get("agents"):
                        selected_pane = self._selected_pane()
                        self.st.snap["agents"] = sorted(
                            self.st.snap["agents"], key=self._sort_key)
                        if selected_pane:
                            for i, a in enumerate(self.st.snap["agents"]):
                                if a.get("pane") == selected_pane:
                                    self.st.selected = i
                                    break
            except Exception:
                pass
            for _ in range(50):
                if not self.running:
                    return
                time.sleep(0.1)

    def _selected_pane(self):
        agents = (self.st.snap or {}).get("agents", [])
        if agents and 0 <= self.st.selected < len(agents):
            return agents[self.st.selected].get("pane")
        return None

    def _sort_key(self, a):
        pane = a.get("pane")
        mt = (self.st.sessions.get(pane) or {}).get("mtime") or 0
        t = mt if mt else self.st.state_times.get(pane, 0)
        return (STATUS_RANK.get(a.get("st"), 4), -t)

    def _fetch_terminal(self, pane):
        herdr = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
        machine = self.cfg.get("herdr_machine", "mbp")
        status = "unknown"
        for a in (self.st.snap or {}).get("agents", []):
            if a.get("pane") == pane:
                status = a.get("st", "unknown")
                break
        recent = self.st.result_mode == "recent"
        note = ""
        if recent:
            if status in ("idle", "done", "unknown"):
                subcmd = ["agent", "read", pane]
            else:
                subcmd = ["pane", "read", pane]
                note = "(working — showing the pane's recent output)\n"
            source, lines = "recent-unwrapped", "200"
        else:
            subcmd = ["pane", "read", pane]
            source, lines = "visible", "40"
        p = subprocess.run(
            [herdr, "--machine", machine] + subcmd +
            ["--source", source, "--lines", lines, "--format", "text"],
            capture_output=True, text=True, timeout=20,
        )
        if p.returncode != 0 and source == "recent-unwrapped":
            subcmd = ["pane", "read", pane]
            source, lines = "visible", "40"
            note = "(recent unavailable — showing the visible screen)\n"
            p = subprocess.run(
                [herdr, "--machine", machine] + subcmd +
                ["--source", source, "--lines", lines, "--format", "text"],
                capture_output=True, text=True, timeout=20,
            )
        if p.returncode != 0:
            msg = (p.stderr or p.stdout or "read failed").strip().replace("\n", " ")
            return f"(terminal: {msg[:80]})"
        return note + (p.stdout.strip() or "(empty terminal)")

    def open_detail(self, pane):
        st = self.st
        st.detail_pane = pane
        st.card = None
        st.quick = None
        st.prompt_text = None
        st.prompt_pane = None
        st.result_pane = None
        st.result_text = ""
        st.result_scroll = 0
        st.result_at = 0
        st.result_loading = True
        st.voice_msg = ""
        st.form_title = ""
        st.form_options = []
        st.form_selected = 0
        st.form_pane = None
        st.form_key_at = 0
        st.form_miss = 0
        st.result_mode = "recent"
        self.show(pane, -1)

    def close_detail(self):
        st = self.st
        st.detail_pane = None
        st.card = None
        st.quick = None
        st.prompt_text = None
        st.prompt_pane = None
        st.result_pane = None
        st.result_text = ""
        st.result_loading = False
        st.voice_msg = ""
        st.form_title = ""
        st.form_options = []
        st.form_selected = 0
        st.form_pane = None
        st.form_key_at = 0
        st.form_miss = 0

    def _result_poller(self):
        while self.running:
            pane = self.st.detail_pane
            st = self.st
            if pane and (pane != st.result_pane or time.time() - st.result_at > 5):
                if pane != st.result_pane:
                    st.result_pane = None
                    st.result_text = ""
                st.result_loading = True
                try:
                    text = self._fetch_terminal(pane)
                except Exception as e:
                    text = f"(error: {e})"
                if self._selected_pane() == pane:
                    st.result_text = text
                    st.result_pane = pane
                    st.result_at = time.time()
                    st.result_loading = False
            for _ in range(10):
                if not self.running:
                    return
                time.sleep(0.1)

    def _key_worker(self):
        herdr = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
        machine = self.cfg.get("herdr_machine", "mbp")
        while self.running:
            try:
                pane, key = self.key_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                p = subprocess.run(
                    [herdr, "--machine", machine, "agent", "send-keys", pane, key],
                    capture_output=True, text=True, timeout=15,
                )
                if p.returncode != 0:
                    msg = (p.stdout or p.stderr or "").strip().replace("\n", " ")
                    self.st.say(f"key {key}: {msg[:70]}")
            except Exception as e:
                self.st.say(f"key {key}: {e}")

    def _form_key(self, key):
        st = self.st
        pane = st.form_pane or st.detail_pane
        if not pane:
            return
        st.form_key_at = time.time()
        if key == "up":
            st.form_selected = max(0, st.form_selected - 1)
        elif key == "down":
            st.form_selected = min(max(0, len(st.form_options) - 1), st.form_selected + 1)
        elif key == "enter":
            st.form_options = []
            st.form_title = ""
            st.form_pane = None
        self.key_q.put((pane, key))

    def _form_poller(self):
        herdr = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
        machine = self.cfg.get("herdr_machine", "mbp")
        while self.running:
            st = self.st
            pane = st.detail_pane
            kind = (st.card or {}).get("kind") if st.card else ""
            if pane and kind == "question":
                try:
                    p = subprocess.run(
                        [herdr, "--machine", machine, "agent", "read", pane,
                         "--source", "detection", "--format", "text"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if p.returncode == 0:
                        opts, title, sel = parse_form(p.stdout)
                        if opts:
                            st.form_pane = pane
                            st.form_title = title or ((st.card or {}).get("summary") or "")
                            st.form_options = opts
                            if time.time() - st.form_key_at > 1.2:
                                st.form_selected = sel
                            st.form_at = time.time()
                            st.form_miss = 0
                        else:
                            st.form_miss += 1
                            if st.form_miss >= 3:
                                st.form_pane = None
                                st.form_options = []
                                st.form_title = ""
                except Exception:
                    pass
            elif st.form_pane:
                st.form_pane = None
                st.form_options = []
                st.form_title = ""
            for _ in range(10):
                if not self.running:
                    return
                time.sleep(0.1)

    def connect(self):
        self.close()
        cmd = [
            "ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            self.cfg["host"],
            self.cfg["bridge"],
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
        except OSError as e:
            self.st.link = "down"
            self.st.say(f"ssh: {e}")
            self.retry_at = time.time() + 5
            return
        self.st.link = "connecting"
        self.st.say("connecting")
        self.buf = b""
        self.send_raw({
            "t": "hello", "v": PROTO_VERSION, "fw": CLIENT_FW,
            "name": self.st.name, "lines": 4,
        })
        self.send_list()
        self.next_ping = time.time() + 30

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self.buf = b""

    def send_raw(self, obj):
        if not self.proc or self.proc.poll() is not None:
            return False
        try:
            self.proc.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            self.link_lost()
            return False

    def send_list(self):
        self.send_raw({"t": "list", "id": self.new_id()})

    def new_id(self):
        i = self.next_id
        self.next_id += 1
        return i

    def link_lost(self):
        self.close()
        self.st.link = "down"
        self.st.say("link lost")
        self.retry_at = time.time() + 3

    # -- receive --

    def pump(self, timeout):
        fds = [sys.stdin.fileno()]
        if self.proc and self.proc.poll() is None:
            fds.append(self.proc.stdout.fileno())
        try:
            r, _, _ = select.select(fds, [], [], timeout)
        except InterruptedError:
            return
        if self.proc and self.proc.stdout.fileno() in r:
            try:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
            except OSError as e:
                if e.errno == errno.EIO:
                    chunk = b""
                else:
                    raise
            if not chunk:
                self.link_lost()
                return
            self.buf += chunk
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                if raw.strip():
                    self.on_line(raw.decode("utf-8", "replace"))
        if sys.stdin.fileno() in r:
            while True:
                key = read_key(sys.stdin.fileno())
                if not key:
                    break
                self.on_key(key)
                if not self.running:
                    break
                r2, _, _ = select.select([sys.stdin.fileno()], [], [], 0)
                if not r2:
                    break

    def on_line(self, line_text):
        if len(line_text) > MAX_LINE:
            self.st.say("line too long")
            return
        try:
            m = json.loads(line_text)
        except json.JSONDecodeError:
            self.st.say("bad json")
            return
        t = m.get("t")
        if t == "hello":
            self.st.link = "connected"
            self.st.now = m.get("now", 0) or self.st.now
        elif t == "snap":
            prev = self.st.snap
            selected_pane = self._selected_pane()
            agents = m.get("agents", [])
            now = time.time()
            self.st.update_times(agents, now)

            agents = sorted(agents, key=self._sort_key)
            m["agents"] = agents
            self.st.snap = m
            self.st.now = m.get("now", self.st.now)
            self.st.link = "connected"
            self.st.selected = 0
            if selected_pane:
                for i, a in enumerate(agents):
                    if a.get("pane") == selected_pane:
                        self.st.selected = i
                        break
            if agents:
                self.st.selected = min(self.st.selected, len(agents) - 1)
            else:
                self.st.selected = 0
            self.after_snapshot(prev, m)
            panes = {a.get("pane") for a in agents}
            if self.st.card and self.st.card.get("pane") not in panes:
                self.st.card = None
        elif t == "card":
            if self.st.detail_pane and m.get("pane") == self.st.detail_pane:
                self.st.card = m
                self.st.armed = None
                self.st.say("")
        elif t == "result":
            verb = self.st.pending.pop(m.get("id"), "")
            if m.get("ok"):
                self.st.say((verb + " ok").strip())
                if verb in ("approve", "deny"):
                    self.st.card = None
            else:
                self.st.say(f"{verb or 'error'}: {m.get('err', 'failed')}")
        elif t == "pong":
            self.st.now = m.get("now", self.st.now)

    def after_snapshot(self, prev, snap):
        blocked = {(a.get("pane"), a.get("seq")) for a in snap.get("agents", [])
                   if a.get("st") == "blocked"}
        done = {(a.get("pane"), a.get("dseq")) for a in snap.get("agents", [])
                if a.get("st") == "done" and a.get("dseq")}
        new_blocked = blocked - self.st.seen_blocked
        new_done = done - self.st.seen_done
        self.st.seen_blocked = blocked
        self.st.seen_done = done
        if new_blocked or new_done:
            sys.stdout.write("\a")
            sys.stdout.flush()

    # -- actions --

    def show(self, pane, pg=-1):
        i = self.new_id()
        self.st.pending[i] = "show"
        self.send_raw({"t": "show", "id": i, "pane": pane, "pg": pg})

    def approve(self):
        c = self.st.card
        if not c or "approve" not in (c.get("can") or []):
            self.st.say("this card can't be approved here")
            return
        i = self.new_id()
        self.st.pending[i] = "approve"
        self.send_raw({
            "t": "approve", "id": i, "pane": c["pane"],
            "seq": c["seq"], "hash": c["hash"],
        })

    def deny(self):
        c = self.st.card
        if not c or "deny" not in (c.get("can") or []):
            self.st.say("this card can't be denied here")
            return
        i = self.new_id()
        self.st.pending[i] = "deny"
        self.send_raw({
            "t": "deny", "id": i, "pane": c["pane"],
            "seq": c["seq"], "hash": c["hash"],
        })

    def interrupt(self):
        c = self.st.card
        if not c or "interrupt" not in (c.get("can") or []):
            self.st.say("nothing to interrupt")
            return
        i = self.new_id()
        self.st.pending[i] = "interrupt"
        self.send_raw({
            "t": "interrupt", "id": i, "pane": c["pane"], "seq": c["seq"],
        })

    def jump(self):
        c = self.st.card
        if not c or "jump" not in (c.get("can") or []):
            self.st.say("nothing to jump to")
            return
        i = self.new_id()
        self.st.pending[i] = "jump"
        self.send_raw({"t": "jump", "id": i, "pane": c["pane"]})
        self.st.say("jumped in herdr")

    # -- voice + prompt --

    def voice_start(self):
        st = self.st
        if not self.voice.get("enabled"):
            st.say(f"voice off: edit {VOICE_PATH}")
            return
        if st.voice_state == "recording":
            self.stop_recording()
            return
        if st.voice_state != "idle":
            return
        if not st.detail_pane:
            st.say("open an agent first (Enter)")
            return
        pane = st.detail_pane
        st.prompt_text = None
        st.prompt_pane = None
        self.voice_cancel = False
        st.voice_state = "recording"
        st.voice_msg = "REC — v stop"
        threading.Thread(target=self._voice_worker, args=(pane,), daemon=True).start()

    def stop_recording(self):
        if self.voice_proc and self.voice_proc.poll() is None:
            self.voice_proc.send_signal(signal.SIGINT)

    def cancel_voice(self):
        self.voice_cancel = True
        if self.voice_proc and self.voice_proc.poll() is None:
            self.voice_proc.kill()
        self.st.voice_state = "idle"
        self.st.voice_msg = "voice cancelled"

    def _voice_worker(self, pane):
        wav = f"/tmp/cardputer-voice-{os.getpid()}.wav"
        secs = int(self.voice.get("record_seconds", 6))
        mode = self.voice.get("record_mode", "toggle")
        dev = self.voice.get("record_device", VOICE_DEFAULTS["record_device"])
        try:
            cmd = ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", "16000", "-c", "1"]
            if mode == "fixed":
                cmd += ["-d", str(secs)]
            cmd.append(wav)
            self.voice_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if mode == "fixed":
                self.voice_proc.wait(timeout=secs + 15)
            else:
                end = time.time() + int(self.voice.get("max_record_seconds", 30))
                while self.voice_proc.poll() is None and time.time() < end:
                    time.sleep(0.1)
                if self.voice_proc.poll() is None:
                    self.voice_proc.send_signal(signal.SIGINT)
                try:
                    self.voice_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.voice_proc.kill()
            self.voice_proc = None
            if self.voice_cancel:
                return
            if not os.path.exists(wav) or os.path.getsize(wav) < 1000:
                raise RuntimeError("no audio recorded")
            if self.voice.get("keep_audio"):
                dest = os.path.expanduser("~/.cache/cardputer-voice/last.wav")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(wav, dest)
            self.st.voice_state = "transcribing"
            self.st.voice_msg = "transcribing…"
            text = transcribe_wav(wav, self.voice)
            if not text:
                raise RuntimeError("empty transcript")
            if self.st.detail_pane != pane:
                self.st.detail_pane = pane
                self.st.result_pane = None
            self.st.prompt_text = text
            self.st.prompt_pane = pane
            self.st.voice_msg = ""
        except Exception as e:
            self.st.voice_msg = f"voice: {e}"
        finally:
            self.voice_proc = None
            self.st.voice_state = "idle"
            try:
                os.remove(wav)
            except OSError:
                pass

    def send_prompt(self):
        st = self.st
        text = (st.prompt_text or "").strip()
        pane = st.prompt_pane
        if not text or not pane:
            st.say("no target agent — reopen detail")
            return
        st.prompt_text = None
        st.prompt_pane = None
        st.voice_msg = ""
        threading.Thread(target=self._send_prompt_worker, args=(pane, text), daemon=True).start()

    def _send_prompt_worker(self, pane, text):
        herdr = shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")
        machine = self.cfg.get("herdr_machine", "mbp")
        try:
            p = subprocess.run(
                [herdr, "--machine", machine, "agent", "prompt", pane, text],
                capture_output=True, text=True, timeout=40,
            )
            if p.returncode == 0:
                self.st.voice_msg = ""
                self.st.say(f"sent to {pane}")
            else:
                self.st.voice_msg = ""
                msg = (p.stdout or p.stderr or "").strip().replace("\n", " ")
                self.st.say(f"send failed: {msg[:80]}")
        except Exception as e:
            self.st.voice_msg = ""
            self.st.say(f"send: {e}")

    def on_key(self, key):
        st = self.st
        if key == "CTRL_C":
            self.running = False
            return
        if st.voice_state == "recording":
            if key == "ESC":
                self.cancel_voice()
                return
            if key.lower() == "v":
                self.stop_recording()
                return
        if st.prompt_text is not None:
            if key == "ESC":
                st.prompt_text = None
                st.prompt_pane = None
                st.voice_msg = ""
                st.say("prompt cancelled")
            elif key == "ENTER":
                self.send_prompt()
            elif key == "BACKSPACE":
                st.prompt_text = st.prompt_text[:-1]
            elif len(key) == 1 and key.isprintable():
                st.prompt_text += key
            return
        if st.voice_state == "transcribing":
            if key == "ESC":
                st.say("transcribing — wait")
                return
        if st.form_options and st.detail_pane:
            fk = key.lower()
            if fk == "q":
                self.running = False
                return
            if fk in ("f", "up"):
                self._form_key("up")
                return
            if fk in ("x", "down"):
                self._form_key("down")
                return
            if key == "ENTER":
                self._form_key("enter")
                return
            if key == "ESC":
                self.close_detail()
                return
            return

        k = key.lower()
        if k == "f":
            k = "up"
        elif k == "x":
            k = "down"
        if k == "q":
            self.running = False
            return
        if k == "esc":
            if st.quick:
                st.quick = None
                st.say("quick reply cancelled")
                return
            if st.detail_pane:
                self.close_detail()
                return
            st.card = None
            st.armed = None
            return
        if k in ("up", "down"):
            if st.detail_pane:
                st.result_scroll = max(0, st.result_scroll + (5 if k == "down" else -5))
                return
            n = len((st.snap or {}).get("agents", []))
            if n:
                st.selected = max(0, min(n - 1, st.selected + (1 if k == "down" else -1)))
            return
        if k == "enter":
            if st.quick:
                pane = st.detail_pane
                text = st.quick
                st.quick = None
                if pane and text:
                    threading.Thread(
                        target=self._send_prompt_worker, args=(pane, text), daemon=True
                    ).start()
                return
            if st.detail_pane:
                return
            agents = (st.snap or {}).get("agents", [])
            if agents:
                self.open_detail(agents[st.selected]["pane"])
            return
        if k == "p":
            if not st.detail_pane:
                st.say("open an agent first (Enter)")
                return
            st.prompt_text = ""
            st.prompt_pane = st.detail_pane
            st.quick = None
            return
        if k == "t":
            if not st.detail_pane:
                st.say("open an agent first (Enter)")
                return
            st.result_mode = "visible" if st.result_mode == "recent" else "recent"
            st.result_pane = None
            st.result_scroll = 0
            st.result_text = ""
            st.say("view: " + st.result_mode)
            return
        if k in ("[", "]"):
            if st.detail_pane:
                st.result_scroll = max(0, st.result_scroll + (1 if k == "]" else -1))
            return
        if k in "123456" and len(k) == 1:
            if not st.detail_pane:
                st.say("open an agent first (Enter)")
                return
            idx = int(k) - 1
            if idx < len(QUICK_REPLIES):
                st.quick = QUICK_REPLIES[idx]
                st.say(f'send "{st.quick}"? Enter yes / Esc no')
            return
        if k == "r":
            self.send_list()
            if st.detail_pane:
                self.show(st.detail_pane, -1)
            st.say("refresh")
            return
        if k == "v":
            self.voice_start()
            return
        if k == "a":
            if st.armed == "approve" and time.time() - st.armed_at < 5:
                st.armed = None
                self.approve()
            else:
                c = st.card
                if c and "approve" in (c.get("can") or []):
                    st.armed = "approve"
                    st.armed_at = time.time()
                    st.say("press a again to APPROVE")
                else:
                    st.say("no approvable card")
            return
        if k == "d":
            self.deny()
            return
        if k == "i":
            if st.armed == "interrupt" and time.time() - st.armed_at < 5:
                st.armed = None
                self.interrupt()
            else:
                c = st.card
                if c and "interrupt" in (c.get("can") or []):
                    st.armed = "interrupt"
                    st.armed_at = time.time()
                    st.say("press i again to STOP")
                else:
                    st.say("nothing to interrupt")
            return
        if k == "j":
            self.jump()

    # -- terminal --

    def enter_screen(self):
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[2J")
        sys.stdout.flush()
        self.old_term = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def leave_screen(self):
        try:
            if self.old_term is not None:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_term)
        except Exception:
            pass
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()

    def run(self):
        self.enter_screen()
        threading.Thread(target=self._title_poller, daemon=True).start()
        threading.Thread(target=self._result_poller, daemon=True).start()
        threading.Thread(target=self._form_poller, daemon=True).start()
        threading.Thread(target=self._key_worker, daemon=True).start()
        last_draw = 0.0
        try:
            while self.running:
                now = time.time()
                if self.st.armed and now - self.st.armed_at > 5:
                    self.st.armed = None
                if self.proc is None and now >= self.retry_at:
                    self.connect()
                self.pump(0.25)
                now = time.time()
                if self.proc and now >= self.next_ping:
                    self.next_ping = now + 30
                    self.send_raw({"t": "ping", "id": self.new_id()})
                if now - last_draw >= 0.25:
                    self.ui.draw(self.st)
                    last_draw = now
        finally:
            self.close()
            self.leave_screen()


# ---- input ------------------------------------------------------------------

def read_key(fd):
    b = os.read(fd, 1)
    if not b:
        return None
    if b == b"\x1b":
        r, _, _ = select.select([fd], [], [], 0.02)
        if not r:
            return "ESC"
        seq = os.read(fd, 2)
        if seq == b"[A":
            return "UP"
        if seq == b"[B":
            return "DOWN"
        if seq == b"[C":
            return "RIGHT"
        if seq == b"[D":
            return "LEFT"
        return "ESC"
    if b == b"\x03":
        return "CTRL_C"
    if b in (b"\x7f", b"\x08"):
        return "BACKSPACE"
    if b in (b"\r", b"\n"):
        return "ENTER"
    try:
        return b.decode("utf-8", "ignore")
    except Exception:
        return None


# ---- probe mode -------------------------------------------------------------

def probe(cfg, timeout=6.0):
    cmd = [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        cfg["host"], cfg["bridge"],
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, bufsize=0)
    p.stdin.write((json.dumps({
        "t": "hello", "v": PROTO_VERSION, "fw": CLIENT_FW,
        "name": cfg["name"], "lines": 4}) + "\n").encode())
    p.stdin.write((json.dumps({"t": "list", "id": 1}) + "\n").encode())
    p.stdin.flush()
    buf = b""
    end = time.time() + timeout
    try:
        while time.time() < end:
            r, _, _ = select.select([p.stdout.fileno()], [], [], 0.5)
            if not r:
                continue
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                m = json.loads(raw)
                if m.get("t") == "snap":
                    print(json.dumps(m, ensure_ascii=False, indent=1))
                    return 0
    finally:
        p.kill()
    print("no snapshot from the bridge", file=sys.stderr)
    return 1


# ---- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="SSH host of the Mac running pager-bridge")
    ap.add_argument("--bridge", help="remote command (default: ~/.local/bin/pager-bridge stdio)")
    ap.add_argument("--name", help="device name in the pager protocol")
    ap.add_argument("--probe", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--voice-test", action="store_true", help="record and print the transcript, then exit")
    ap.add_argument("--wav", help="with --voice-test: transcribe this file instead of recording")
    a = ap.parse_args()
    cfg = load_config()
    if a.host:
        cfg["host"] = a.host
    if a.bridge:
        cfg["bridge"] = a.bridge
    if a.name:
        cfg["name"] = a.name

    if a.probe:
        return probe(cfg)
    if a.voice_test:
        return voice_test(load_voice_config(), a.wav)

    pager = Pager(cfg)

    def stop(_sig, _frm):
        pager.running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        pager.run()
        return 0
    except Exception as e:
        pager.close()
        pager.leave_screen()
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
