#!/usr/bin/env python3
"""
auto_typer.py - types a payload file into whatever window has focus,
character by character, with human-like timing and ZERO typing errors.

Built for testing keystroke/input-history trackers.

--------------------------------------------------------------------------
INSTALL (Windows, PowerShell or cmd):

    pip install pynput

RUN:

    python auto_typer.py payload.txt

    Focus the window you want typed into, then press F8 to start.
    F9  = pause / resume
    ESC = abort immediately

DRY RUN (no typing, just proves the payload round-trips exactly):

    python auto_typer.py payload.txt --dry-run --verify

SPEED:

    --wpm 120                 override the payload's speed
    --target-time 90          solve for the wpm that takes about 90 seconds
                              (pair with --dry-run to see the answer first)

--------------------------------------------------------------------------
PAYLOAD FORMAT
--------------------------------------------------------------------------
A payload is a UTF-8 text file. Everything after the `---` separator is
typed VERBATIM - byte for byte, including blank lines, trailing spaces and
indentation. Nothing in the body is interpreted unless `#!tokens on`.

    #!AUTOTYPE v1
    #!wpm 85
    #!jitter 0.35
    #!indent literal
    #!tabs key
    #!newline-pause 0.35
    #!thinking 0.02
    #!tokens off
    #!enter-at-end false
    ---
    def hello():
        print("hi")

If the first line is not `#!AUTOTYPE v1`, the ENTIRE file is treated as the
body and all settings use their defaults. So a plain .txt file just works.

Header directives:
    wpm            words per minute, 5 chars/word          (default 85)
    jitter         0.0-1.0 timing randomness               (default 0.35)
    indent         literal | strip | guard                 (default literal)
                     literal - type leading whitespace as-is
                     strip   - skip it, let the editor auto-indent
                     guard   - after Enter, clear the editor's auto-indent
                               (shift+home, backspace) then type it literally
    tabs           key | spaces:N                          (default key)
    newline-pause  extra seconds after each Enter          (default 0.35)
    thinking       chance (0-1) of a longer pause per char (default 0.02)
    tokens         off | on - enable {{PAUSE:n}} / {{KEY:...}} in the body
    enter-at-end   true | false - press Enter after the last char
                                                           (default false)

Tokens (only when `#!tokens on`):
    {{PAUSE:2.5}}        wait 2.5 seconds
    {{KEY:enter}}        press a named key
    {{KEY:ctrl+s}}       press a chord
Leave tokens off unless you need them - off guarantees the body is typed
literally even if it contains {{ }}.
--------------------------------------------------------------------------
"""

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone

VERSION = "1.0"

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

DEFAULTS = {
    "wpm": 85.0,
    "jitter": 0.35,
    "indent": "literal",
    "tabs": "key",
    "newline-pause": 0.35,
    "thinking": 0.02,
    "tokens": "off",
    "enter-at-end": "false",
    "dismiss": "none",
}

HEADER_RE = re.compile(r"^#!([A-Za-z][A-Za-z0-9-]*)\s*(.*)$")
TOKEN_RE = re.compile(r"\{\{(PAUSE|KEY):([^}]*)\}\}")

# Characters that get an extra beat after them, and how much (seconds, before jitter)
PUNCT_PAUSE = {
    ",": 0.08, ";": 0.09, ":": 0.09,
    ".": 0.14, "!": 0.14, "?": 0.14,
    ")": 0.05, "]": 0.05, "}": 0.06,
    " ": 0.015,
}


# --------------------------------------------------------------------------
# Payload parsing
# --------------------------------------------------------------------------

class Payload:
    def __init__(self, body, settings, source):
        self.body = body
        self.settings = settings
        self.source = source


def parse_payload(text, source="<payload>"):
    """Split a payload into settings + verbatim body."""
    # Strip a UTF-8 BOM if the file has one; it must never be typed.
    if text.startswith("﻿"):
        text = text[1:]

    settings = dict(DEFAULTS)

    lines = text.split("\n")
    if not lines or lines[0].strip() != "#!AUTOTYPE v1":
        # Plain text file: the whole thing is the body.
        return Payload(normalize_newlines(text), coerce_settings(settings), source)

    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            i += 1
            break
        m = HEADER_RE.match(line.strip())
        if m:
            key, value = m.group(1).lower(), m.group(2).strip()
            if key not in DEFAULTS:
                raise ValueError("unknown header directive: #!%s" % key)
            settings[key] = value
            i += 1
            continue
        if line.strip() == "":
            i += 1
            continue
        raise ValueError(
            "payload line %d is neither a #! directive nor the --- separator: %r"
            % (i + 1, line)
        )
    else:
        raise ValueError("payload has an #!AUTOTYPE header but no --- separator")

    body = "\n".join(lines[i:])
    return Payload(normalize_newlines(body), coerce_settings(settings), source)


def normalize_newlines(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")


def coerce_settings(raw):
    s = dict(raw)
    s["wpm"] = float(s["wpm"])
    s["jitter"] = max(0.0, min(1.0, float(s["jitter"])))
    s["newline-pause"] = float(s["newline-pause"])
    s["thinking"] = max(0.0, min(1.0, float(s["thinking"])))
    if s["indent"] not in ("literal", "strip", "guard"):
        raise ValueError("#!indent must be literal, strip or guard")
    if s["tabs"] != "key" and not re.match(r"^spaces:\d+$", str(s["tabs"])):
        raise ValueError("#!tabs must be 'key' or 'spaces:N'")
    if s["dismiss"] not in ("none", "esc"):
        raise ValueError("#!dismiss must be none or esc")
    if s["tokens"] not in ("on", "off"):
        raise ValueError("#!tokens must be on or off")
    if str(s["enter-at-end"]).lower() not in ("true", "false"):
        raise ValueError("#!enter-at-end must be true or false")
    s["enter-at-end"] = str(s["enter-at-end"]).lower() == "true"
    if s["wpm"] <= 0:
        raise ValueError("#!wpm must be greater than 0")
    return s


# --------------------------------------------------------------------------
# Pre-flight validation - fail loudly rather than mistype
# --------------------------------------------------------------------------

def validate(body):
    """Return (fatal_errors, warnings)."""
    fatal, warn = [], []

    for idx, ch in enumerate(body):
        cp = ord(ch)
        if ch in ("\n", "\t"):
            continue
        if cp < 0x20 or cp == 0x7F:
            fatal.append(
                "control character U+%04X at offset %d cannot be typed reliably"
                % (cp, idx)
            )
        elif cp > 0xFFFF:
            fatal.append(
                "character %r (U+%05X) at offset %d is outside the Basic "
                "Multilingual Plane and may be split by Windows unicode "
                "injection" % (ch, cp, idx)
            )
        elif unicodedata.combining(ch):
            warn.append(
                "combining mark U+%04X at offset %d - some editors merge it "
                "into the previous character" % (cp, idx)
            )

    for n, line in enumerate(body.split("\n"), 1):
        if line != line.rstrip() and line.strip():
            warn.append(
                "line %d has trailing whitespace - editors with trim-on-save "
                "or trim-on-type will remove it" % n
            )

    # de-dupe warnings but keep order
    seen, uniq = set(), []
    for w in warn:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return fatal, uniq[:20]


# --------------------------------------------------------------------------
# Turning the body into a flat list of actions
# --------------------------------------------------------------------------
# Each action is a tuple:
#   ("char", "a")        type a literal character
#   ("key", "enter")     press a named key or chord, e.g. "ctrl+s"
#   ("pause", 2.5)       sleep, in addition to normal timing

def build_actions(payload):
    s = payload.settings
    actions = []

    def emit_line(line, is_first_line):
        text = line
        if not is_first_line and s["indent"] == "strip":
            text = text.lstrip(" \t")
        for ch in text:
            if ch == "\t":
                if s["tabs"] == "key":
                    actions.append(("key", "tab"))
                else:
                    n = int(str(s["tabs"]).split(":")[1])
                    for _ in range(n):
                        actions.append(("char", " "))
            else:
                actions.append(("char", ch))

    def emit_text(text, first_line_offset):
        parts = text.split("\n")
        for j, line in enumerate(parts):
            if j > 0:
                if s["dismiss"] == "esc":
                    actions.append(("key", "esc"))
                actions.append(("key", "enter"))
                if s["indent"] == "guard":
                    actions.append(("key", "shift+home"))
                    actions.append(("key", "backspace"))
            emit_line(line, is_first_line=(j == 0 and first_line_offset == 0))

    if s["tokens"] == "on":
        pos = 0
        chunk_index = 0
        for m in TOKEN_RE.finditer(payload.body):
            if m.start() > pos:
                emit_text(payload.body[pos:m.start()], chunk_index)
                chunk_index += 1
            kind, value = m.group(1), m.group(2).strip()
            if kind == "PAUSE":
                actions.append(("pause", float(value)))
            else:
                actions.append(("key", value.lower()))
            pos = m.end()
        if pos < len(payload.body):
            emit_text(payload.body[pos:], chunk_index)
    else:
        emit_text(payload.body, 0)

    if s["enter-at-end"]:
        if s["dismiss"] == "esc":
            actions.append(("key", "esc"))
        actions.append(("key", "enter"))

    return actions


def reconstruct(actions, settings):
    """Rebuild the text these actions would produce. Used by --verify."""
    out = []
    for kind, value in actions:
        if kind == "char":
            out.append(value)
        elif kind == "key":
            if value == "enter":
                out.append("\n")
            elif value == "tab":
                out.append("\t")
            elif value in ("shift+home", "backspace", "esc"):
                pass  # indent guard / popup dismissal; no net text
    return "".join(out)


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------

def estimate_seconds(actions, settings, seed=12345):
    """Simulate the real timing model to predict how long the run will take.

    This walks the same delay function the typing loop uses, so it accounts
    for newline pauses, punctuation beats and thinking pauses - not just the
    raw character rate. PER_EVENT_OVERHEAD covers the cost of pushing each
    key through the OS, measured on Windows.
    """
    PER_EVENT_OVERHEAD = 0.0055
    rng = random.Random(seed)
    tm = Timing(settings, rng)
    total, prev = 0.0, None
    for kind, value in actions:
        if kind == "pause":
            total += float(value)
        else:
            total += tm.delay_for(kind, value, prev) + PER_EVENT_OVERHEAD
        prev = (kind, value)
    return total


def solve_wpm(actions, settings, target_seconds):
    """Find the #!wpm that makes this payload take about target_seconds."""
    lo, hi = 1.0, 4000.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        trial = dict(settings)
        trial["wpm"] = mid
        if estimate_seconds(actions, trial) > target_seconds:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


class Timing:
    def __init__(self, settings, rng):
        self.s = settings
        self.rng = rng
        cps = settings["wpm"] * 5.0 / 60.0
        self.base = 1.0 / cps

    def delay_for(self, kind, value, prev):
        if kind == "pause":
            return 0.0  # handled separately
        d = self.base

        # Shifted characters take a touch longer.
        if kind == "char" and (value.isupper() or value in "~!@#$%^&*()_+{}|:\"<>?"):
            d *= 1.25

        # Multiplicative jitter, clamped so it never goes silly.
        j = self.s["jitter"]
        if j > 0:
            d *= max(0.35, min(2.6, self.rng.lognormvariate(0.0, j)))

        # Extra beat after punctuation / spaces.
        if prev and prev[0] == "char":
            d += PUNCT_PAUSE.get(prev[1], 0.0) * (1.0 + self.rng.uniform(-0.3, 0.5))

        # Extra beat after a line break.
        if prev and prev == ("key", "enter"):
            d += self.s["newline-pause"] * (1.0 + self.rng.uniform(-0.3, 0.6))

        # Occasional "thinking" pause.
        if self.rng.random() < self.s["thinking"]:
            d += self.rng.uniform(0.25, 1.1)

        return max(0.004, d)


# --------------------------------------------------------------------------
# Typing backend (pynput)
# --------------------------------------------------------------------------

NAMED_KEYS = {
    "enter": "enter", "return": "enter", "tab": "tab", "esc": "esc",
    "escape": "esc", "space": "space", "backspace": "backspace",
    "delete": "delete", "home": "home", "end": "end", "up": "up",
    "down": "down", "left": "left", "right": "right",
    "pageup": "page_up", "pagedown": "page_down", "insert": "insert",
}
MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt",
             "shift": "shift", "win": "cmd", "cmd": "cmd", "super": "cmd"}


class Typist:
    """Wraps pynput. Imported lazily so --dry-run works with no GUI."""

    def __init__(self):
        from pynput.keyboard import Controller, Key  # noqa
        self.Key = Key
        self.kb = Controller()

    def _resolve(self, name):
        if name in NAMED_KEYS:
            return getattr(self.Key, NAMED_KEYS[name])
        if len(name) == 1:
            return name
        if re.match(r"^f([1-9]|1[0-9]|2[0-4])$", name):
            return getattr(self.Key, name)
        raise ValueError("unknown key name: %r" % name)

    def press_key(self, spec):
        parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
        mods = [MODIFIERS[p] for p in parts if p in MODIFIERS]
        rest = [p for p in parts if p not in MODIFIERS]
        if len(rest) != 1:
            raise ValueError("key spec %r must name exactly one non-modifier key" % spec)
        target = self._resolve(rest[0])
        mod_keys = [getattr(self.Key, m) for m in mods]
        for mk in mod_keys:
            self.kb.press(mk)
        try:
            self.kb.press(target)
            self.kb.release(target)
        finally:
            for mk in reversed(mod_keys):
                self.kb.release(mk)

    def type_char(self, ch):
        self.kb.press(ch)
        self.kb.release(ch)



# --------------------------------------------------------------------------
# VirtualBox backend - injects scancodes into the VM's emulated keyboard
# --------------------------------------------------------------------------
# The guest cannot distinguish these from physical keypresses, because the
# emulated keyboard controller is the only keyboard it has. This bypasses
# VirtualBox's host-side input capture entirely.
#
# Set-1 scancodes, US layout. Break code = make code | 0x80.

_SC_BASE = {
    "`": 0x29, "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B, "-": 0x0C,
    "=": 0x0D,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
    "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "[": 0x1A, "]": 0x1B,
    "\\": 0x2B,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23,
    "j": 0x24, "k": 0x25, "l": 0x26, ";": 0x27, "'": 0x28,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30, "n": 0x31,
    "m": 0x32, ",": 0x33, ".": 0x34, "/": 0x35,
    " ": 0x39,
}
# characters reached with Shift, mapped to the unshifted key that produces them
_SC_SHIFTED = {
    "~": "`", "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0", "_": "-",
    "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'",
    "<": ",", ">": ".", "?": "/",
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _SC_SHIFTED[_c.upper()] = _c

_SC_NAMED = {
    "enter": [0x1C], "tab": [0x0F], "esc": [0x01], "backspace": [0x0E],
    "space": [0x39], "delete": [0xE0, 0x53], "home": [0xE0, 0x47],
    "end": [0xE0, 0x4F], "up": [0xE0, 0x48], "down": [0xE0, 0x50],
    "left": [0xE0, 0x4B], "right": [0xE0, 0x4D],
}
_SC_LSHIFT = 0x2A
_SC_CTRL = 0x1D
_SC_ALT = 0x38


def _make_break(codes):
    """Turn a make-code sequence into make+break hex strings."""
    out = []
    for c in codes:
        out.append("%02x" % c)
    for c in reversed(codes):
        out.append("%02x" % (0xE0 if c == 0xE0 else c | 0x80))
    # an E0 prefix must lead its break code too
    fixed, i = [], 0
    seq = [c for c in codes]
    for c in seq:
        fixed.append("%02x" % c)
    rel = []
    j = 0
    while j < len(seq):
        if seq[j] == 0xE0 and j + 1 < len(seq):
            rel = ["e0", "%02x" % (seq[j + 1] | 0x80)] + rel
            j += 2
        else:
            rel = ["%02x" % (seq[j] | 0x80)] + rel
            j += 1
    return fixed + rel


def scancodes_for_char(ch):
    """Hex scancode strings that type one character, or None if unmappable."""
    if ch in _SC_BASE:
        return _make_break([_SC_BASE[ch]])
    if ch in _SC_SHIFTED:
        base = _SC_BASE[_SC_SHIFTED[ch]]
        return (["%02x" % _SC_LSHIFT] + _make_break([base])
                + ["%02x" % (_SC_LSHIFT | 0x80)])
    return None


def scancodes_for_key(spec):
    """Hex scancodes for a named key or chord such as 'enter' or 'shift+home'."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods, rest = [], []
    for p in parts:
        if p in ("shift",):
            mods.append(_SC_LSHIFT)
        elif p in ("ctrl", "control"):
            mods.append(_SC_CTRL)
        elif p == "alt":
            mods.append(_SC_ALT)
        else:
            rest.append(p)
    if len(rest) != 1 or rest[0] not in _SC_NAMED:
        raise ValueError("vbox backend cannot send key %r" % spec)
    out = ["%02x" % m for m in mods]
    out += _make_break(_SC_NAMED[rest[0]])
    out += ["%02x" % (m | 0x80) for m in reversed(mods)]
    return out


class VBoxTypist:
    """Sends keystrokes to a running VirtualBox VM via VBoxManage."""

    def __init__(self, vm, vboxmanage=None, verify=True):
        import shutil
        self.vm = vm
        self.exe = vboxmanage or self._find_vboxmanage(shutil)
        if not self.exe:
            raise RuntimeError(
                "VBoxManage not found. Pass --vboxmanage with its full path "
                "(usually C:\\Program Files\\Oracle\\VirtualBox\\VBoxManage.exe)")
        if verify:
            self._check_running()

    @staticmethod
    def _find_vboxmanage(shutil):
        found = shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
        if found:
            return found
        for p in (r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
                  r"C:\Program Files\Oracle\VirtualBox\VBoxManage",
                  "/usr/bin/VBoxManage", "/usr/local/bin/VBoxManage"):
            if os.path.exists(p):
                return p
        return None

    def _run(self, args):
        import subprocess
        r = subprocess.run([self.exe] + args, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("VBoxManage failed: %s"
                               % (r.stderr.strip() or r.stdout.strip()))
        return r.stdout

    def _check_running(self):
        out = self._run(["showvminfo", self.vm, "--machinereadable"])
        state = ""
        for line in out.split("\n"):
            if line.startswith("VMState="):
                state = line.split("=", 1)[1].strip().strip('"')
        if state != "running":
            raise RuntimeError(
                "VM %r is in state %r - it must be running before typing."
                % (self.vm, state or "unknown"))

    def send_scancodes(self, codes):
        self._run(["controlvm", self.vm, "keyboardputscancode"] + codes)

    def type_char(self, ch):
        codes = scancodes_for_char(ch)
        if codes is None:
            raise ValueError("character %r has no US-layout scancode" % ch)
        self.send_scancodes(codes)

    def press_key(self, spec):
        self.send_scancodes(scancodes_for_key(spec))


# --------------------------------------------------------------------------
# Control: hotkeys + the run loop
# --------------------------------------------------------------------------

class Control:
    def __init__(self):
        self.start = threading.Event()
        self.abort = threading.Event()
        self.paused = threading.Event()


def install_hotkeys(ctl, esc_is_ours=False):
    """esc_is_ours: the payload sends Escape itself (#!dismiss esc), so the
    abort key moves to F10 - otherwise the script would abort on its own
    first synthetic Escape."""
    from pynput import keyboard

    def on_press(key):
        if key == keyboard.Key.f8:
            if not ctl.start.is_set():
                ctl.start.set()
        elif key == keyboard.Key.f10:
            ctl.abort.set()
            ctl.start.set()
        elif key == keyboard.Key.esc and esc_is_ours:
            return
        elif key == keyboard.Key.f9:
            if ctl.paused.is_set():
                ctl.paused.clear()
                print("\n[resumed]")
            else:
                ctl.paused.set()
                print("\n[paused - F9 to resume]")
        elif key == keyboard.Key.esc:
            ctl.abort.set()
            ctl.start.set()

    listener = keyboard.Listener(on_press=on_press)
    listener.daemon = True
    listener.start()
    return listener


def run(actions, settings, args):
    rng = random.Random(args.seed)
    timing = Timing(settings, rng)

    typist = None
    if not args.dry_run:
        if args.backend == "vbox":
            try:
                typist = VBoxTypist(args.vm, args.vboxmanage)
            except Exception as e:
                print("ERROR: %s" % e, file=sys.stderr)
                return 2
            print("backend: VirtualBox scancode injection -> VM %r" % args.vm)
        else:
            try:
                typist = Typist()
            except ImportError:
                print("ERROR: pynput is not installed.  Run:  pip install pynput",
                      file=sys.stderr)
                return 2

    ctl = Control()
    listener = None
    if not args.dry_run and args.backend == "vbox" and not args.no_hotkey:
        print("note: --backend vbox does not need host focus; hotkeys disabled.")
        args.no_hotkey = True
    if not args.dry_run and not args.no_hotkey:
        esc_is_ours = settings["dismiss"] == "esc"
        listener = install_hotkeys(ctl, esc_is_ours=esc_is_ours)
        print("Focus the target window, then press F8 to start.")
        if esc_is_ours:
            print("  F9 = pause/resume     F10 = abort")
            print("  (this payload sends Escape itself, so ESC will not abort)")
        else:
            print("  F9 = pause/resume     ESC or F10 = abort")
        ctl.start.wait()
        if ctl.abort.is_set():
            print("aborted before start")
            return 1
        for n in range(args.countdown, 0, -1):
            print("starting in %d..." % n)
            time.sleep(1.0)
    elif not args.dry_run:
        for n in range(args.countdown, 0, -1):
            print("starting in %d..." % n)
            time.sleep(1.0)

    log = open(args.log, "w", encoding="utf-8") if args.log else None
    t0 = time.perf_counter()
    prev = None
    sent = 0

    try:
        for i, (kind, value) in enumerate(actions):
            if ctl.abort.is_set():
                print("\n[aborted at action %d of %d]" % (i, len(actions)))
                break
            while ctl.paused.is_set():
                time.sleep(0.05)

            if kind == "pause":
                if not args.fast:
                    time.sleep(float(value))
                prev = (kind, value)
                continue

            d = timing.delay_for(kind, value, prev)
            if not args.fast:
                time.sleep(d)

            if typist:
                if kind == "char":
                    typist.type_char(value)
                else:
                    typist.press_key(value)
            sent += 1

            if log:
                log.write(json.dumps({
                    "i": i,
                    "elapsed": round(time.perf_counter() - t0, 6),
                    "wall": datetime.now(timezone.utc).isoformat(),
                    "type": kind,
                    "value": value,
                    "planned_delay": round(d, 6),
                }, ensure_ascii=False) + "\n")

            if args.progress and i % 50 == 0:
                pct = 100.0 * i / max(1, len(actions))
                print("\r  %5.1f%%  (%d/%d)" % (pct, i, len(actions)),
                      end="", flush=True)

            prev = (kind, value)
    finally:
        if log:
            log.close()
        if listener:
            listener.stop()

    elapsed = time.perf_counter() - t0
    print("\ndone: %d events in %.1fs" % (sent, elapsed))
    if args.log:
        print("log:  %s" % os.path.abspath(args.log))
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        description="Type a payload file as if a human were typing it.")
    p.add_argument("payload", help="path to the payload file (UTF-8)")
    p.add_argument("--dry-run", action="store_true",
                   help="do not type anything; just plan and report")
    p.add_argument("--verify", action="store_true",
                   help="check the planned keystrokes rebuild the body exactly")
    p.add_argument("--fast", action="store_true",
                   help="skip all delays (use with --dry-run)")
    p.add_argument("--wpm", type=float, default=None,
                   help="override the payload's #!wpm")
    p.add_argument("--jitter", type=float, default=None,
                   help="override the payload's #!jitter")
    p.add_argument("--target-time", type=float, default=None,
                   metavar="SECONDS",
                   help="solve for the wpm that makes the run take this long")
    p.add_argument("--countdown", type=int, default=3,
                   help="seconds between F8 and the first keystroke")
    p.add_argument("--log", default=None,
                   help="write a JSONL log of every event sent")
    p.add_argument("--seed", type=int, default=None,
                   help="fix the RNG seed for reproducible timing")
    p.add_argument("--no-hotkey", action="store_true",
                   help="start after the countdown instead of waiting for F8")
    p.add_argument("--progress", action="store_true",
                   help="show a running progress percentage")
    p.add_argument("--force", action="store_true",
                   help="type anyway despite pre-flight errors")
    p.add_argument("--backend", choices=("local", "vbox"), default="local",
                   help="local = type into this machine (pynput); "
                        "vbox = inject scancodes into a running VirtualBox VM")
    p.add_argument("--vm", default=None,
                   help="VirtualBox VM name or UUID (required with --backend vbox)")
    p.add_argument("--vboxmanage", default=None,
                   help="full path to VBoxManage.exe if it is not on PATH")
    p.add_argument("--vbox-benchmark", type=int, default=0, metavar="N",
                   help="send N harmless keystrokes to the VM and report the "
                        "per-call cost, then exit (use 20)")
    args = p.parse_args(argv)

    with open(args.payload, "r", encoding="utf-8", newline="") as fh:
        raw = fh.read()

    try:
        payload = parse_payload(raw, args.payload)
    except ValueError as e:
        print("payload error: %s" % e, file=sys.stderr)
        return 2

    if args.wpm is not None:
        payload.settings["wpm"] = args.wpm
    if args.jitter is not None:
        payload.settings["jitter"] = max(0.0, min(1.0, args.jitter))

    if args.vbox_benchmark:
        if not args.vm:
            print("ERROR: --vbox-benchmark requires --vm", file=sys.stderr)
            return 2
        try:
            t = VBoxTypist(args.vm, args.vboxmanage)
        except Exception as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 2
        n = args.vbox_benchmark
        print("sending %d Shift presses (harmless - types nothing) to %r..."
              % (n, args.vm))
        t0 = time.perf_counter()
        for _ in range(n):
            t.send_scancodes(["2a", "aa"])
        dt = (time.perf_counter() - t0) / n
        print("  per call: %.0f ms" % (dt * 1000))
        print("  that caps this backend at roughly %.0f wpm end to end"
              % ((1.0 / dt) * 60.0 / 5.0))
        print("  a 2000-keystroke file would take about %s"
              % fmt_duration(2000 * dt))
        return 0

    if args.backend == "vbox" and not args.vm:
        print("ERROR: --backend vbox requires --vm \"Your VM Name\"", file=sys.stderr)
        return 2

    fatal, warn = validate(payload.body)
    if args.backend == "vbox":
        bad = sorted({c for c in payload.body
                      if c not in ("\n", "\t") and scancodes_for_char(c) is None})
        if bad:
            fatal = list(fatal) + [
                "character %r cannot be sent as a US-layout scancode "
                "(vbox backend)" % c for c in bad]
    for w in warn:
        print("warning: %s" % w, file=sys.stderr)
    if fatal:
        for f in fatal:
            print("ERROR: %s" % f, file=sys.stderr)
        if not args.force:
            print("refusing to type - fix the payload or pass --force",
                  file=sys.stderr)
            return 2

    actions = build_actions(payload)

    if args.verify:
        rebuilt = reconstruct(actions, payload.settings)
        expected = payload.body
        if payload.settings["enter-at-end"]:
            expected = expected + "\n"
        if payload.settings["indent"] == "strip":
            expected = "\n".join(l.lstrip(" \t") for l in expected.split("\n"))
        if str(payload.settings["tabs"]).startswith("spaces:"):
            n = int(str(payload.settings["tabs"]).split(":")[1])
            expected = expected.replace("\t", " " * n)
        if rebuilt == expected:
            print("VERIFY: exact match (%d chars, %d key events)"
                  % (len(expected), len(actions)))
        else:
            print("VERIFY: MISMATCH", file=sys.stderr)
            for k in range(min(len(rebuilt), len(expected))):
                if rebuilt[k] != expected[k]:
                    print("  first difference at offset %d: planned %r, "
                          "expected %r" % (k, rebuilt[k], expected[k]),
                          file=sys.stderr)
                    break
            else:
                print("  length differs: planned %d, expected %d"
                      % (len(rebuilt), len(expected)), file=sys.stderr)
            return 3

    if args.target_time is not None:
        solved = solve_wpm(actions, payload.settings, args.target_time)
        payload.settings["wpm"] = solved
        print("solved #!wpm %.1f to hit a target of %s"
              % (solved, fmt_duration(args.target_time)))
        if solved > 400:
            floor = dict(payload.settings)
            floor["wpm"] = 100000.0
            print("  note: wpm is doing almost nothing at this point - the "
                  "fixed pauses dominate.\n  This payload cannot go below "
                  "about %s without lowering #!newline-pause (%.2fs) and "
                  "#!thinking (%.0f%%)."
                  % (fmt_duration(estimate_seconds(actions, floor)),
                     payload.settings["newline-pause"],
                     payload.settings["thinking"] * 100))

    chars = sum(1 for k, _ in actions if k == "char")
    est = estimate_seconds(actions, payload.settings)
    eff_wpm = (chars / 5.0) / (est / 60.0) if est > 0 else 0.0
    print("payload: %s" % payload.source)
    print("  %d characters, %d key events" % (chars, len(actions)))
    print("  settings: wpm=%g jitter=%g indent=%s tabs=%s tokens=%s"
          % (payload.settings["wpm"], payload.settings["jitter"],
             payload.settings["indent"], payload.settings["tabs"],
             payload.settings["tokens"]))
    print("  estimated typing time: %s  (effective %.0f wpm end to end)"
          % (fmt_duration(est), eff_wpm))

    if args.dry_run:
        print("dry run - nothing was typed")
        return 0

    return run(actions, payload.settings, args)


def fmt_duration(sec):
    if sec < 60:
        return "%.0fs" % sec
    m, s = divmod(int(sec), 60)
    if m < 60:
        return "%dm %ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh %dm" % (h, m)


if __name__ == "__main__":
    sys.exit(main())
