# Auto-Typer

A CLI tool that takes a text or code payload and types it out on your machine as real keystrokes, character by character, as though a person were at the keyboard.

I wrote it to test a keystroke-history tracker I had built. The tracker captures input events and has no idea where they came from, so feeding it automated typing was the only practical way to exercise it at volume without sitting there typing by hand.

## The problem worth solving

Symbols. Replaying prose is easy. Replaying *code* is not, because a single mangled bracket, quote, or backslash makes the output useless, and those are exactly the characters that keyboard-layout and Unicode handling get wrong.

So the design centers on symbol fidelity:

- Unicode normalization before anything is sent, so visually identical characters don't arrive as different codepoints
- A **symbol torture-test payload** covering the characters most likely to break
- A `--dry-run` mode that reports exactly what would be typed without sending it
- A verify mode that checks what arrived against what was intended
- A JSONL send log, so a run that goes wrong leaves a record of what actually happened

## Payload format

Payloads are versioned, with a header line, a `---` separator, and the verbatim body:

```
#!AUTOTYPE v1
---
<everything below this line is typed exactly as written>
```

Versioning the format means an old payload can never be silently misread by a newer tool.

## Stack

Python, `argparse`, `pynput` for keystroke emission, `unicodedata` for normalization, `threading` for timing control. Around 950 lines, with its own test file.

## Caution

This types into whatever window has focus. Run it deliberately.
