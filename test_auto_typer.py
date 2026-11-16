import io, subprocess, sys, os, tempfile, textwrap
sys.path.insert(0, "/home/claude")
import auto_typer as at

FAILS = []
RUN = []

def check(name, cond, detail=""):
    RUN.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)

def roundtrip(name, text, expect_body=None):
    """parse -> build_actions -> reconstruct must equal the body verbatim."""
    p = at.parse_payload(text, name)
    actions = at.build_actions(p)
    rebuilt = at.reconstruct(actions, p.settings)
    expected = expect_body if expect_body is not None else p.body
    check(name, rebuilt == expected,
          "planned=%r expected=%r" % (rebuilt[:120], expected[:120]))
    return p, actions

# 1. plain file, no header -> whole file is body
plain = 'hello, world\nsecond line\n\n  indented\ttab\n'
p, a = roundtrip("plain-no-header", plain)
check("plain-defaults", p.settings["wpm"] == 85.0 and p.settings["indent"] == "literal")

# 2. header + real code, braces/quotes/blank lines/trailing space
code = (
    "#!AUTOTYPE v1\n"
    "#!wpm 120\n"
    "#!jitter 0.4\n"
    "---\n"
    'def f(x):\n'
    '    """doc {not a token} {{PAUSE:9}}"""\n'
    '    d = {"a": 1, "b": [2, 3]}\n'
    '\n'
    '    return d.get(x, None)   \n'
)
p, a = roundtrip("code-with-braces-and-tokens-off", code)
check("tokens-off-literal-braces", "{{PAUSE:9}}" in p.body and
      "".join(v for k, v in a if k == "char").count("{{PAUSE:9}}") == 1)
check("header-wpm-parsed", p.settings["wpm"] == 120.0)

# 3. CRLF and lone CR normalize to \n
crlf = "a\r\nb\rc\r\n"
p, a = roundtrip("crlf-normalized", crlf)
check("no-cr-remains", "\r" not in p.body)

# 4. BMP unicode is fine; astral is fatal
p = at.parse_payload("café naïve — 你好 ✓", "uni")
f, w = at.validate(p.body)
check("bmp-unicode-ok", f == [], str(f))
f2, _ = at.validate("wave \U0001F44B")
check("astral-is-fatal", len(f2) == 1 and "Multilingual" in f2[0], str(f2))
f3, _ = at.validate("null\x00byte")
check("control-char-is-fatal", len(f3) == 1, str(f3))
_, w4 = at.validate("trailing   \nok\n")
check("trailing-ws-warned", any("trailing" in x for x in w4), str(w4))

# 5. tabs:spaces and enter-at-end and indent:strip
s = ("#!AUTOTYPE v1\n#!tabs spaces:4\n#!indent strip\n#!enter-at-end true\n---\n"
     "line1\n\tline2\n        line3")
p = at.parse_payload(s, "modes")
a = at.build_actions(p)
rebuilt = at.reconstruct(a, p.settings)
check("tabs-to-spaces", "\t" not in rebuilt, repr(rebuilt))
check("indent-stripped", rebuilt == "line1\nline2\nline3\n", repr(rebuilt))
check("enter-at-end", rebuilt.endswith("\n"))

# 6. indent guard emits shift+home + backspace after each enter
g = "#!AUTOTYPE v1\n#!indent guard\n---\na\n    b\n    c"
p = at.parse_payload(g, "guard")
a = at.build_actions(p)
keys = [v for k, v in a if k == "key"]
check("guard-keys", keys == ["enter", "shift+home", "backspace",
                             "enter", "shift+home", "backspace"], str(keys))
check("guard-roundtrip", at.reconstruct(a, p.settings) == p.body,
      repr(at.reconstruct(a, p.settings)))

# 7. tokens on
t = ("#!AUTOTYPE v1\n#!tokens on\n---\n"
     "git status{{KEY:enter}}{{PAUSE:1.5}}git add -A{{KEY:enter}}")
p = at.parse_payload(t, "tok")
a = at.build_actions(p)
kinds = [(k, v) for k, v in a if k != "char"]
check("token-pause-parsed", ("pause", 1.5) in kinds, str(kinds))
check("token-key-parsed", kinds.count(("key", "enter")) == 2, str(kinds))
check("token-text-typed", "".join(v for k, v in a if k == "char")
      == "git statusgit add -A")

# 8. bad headers rejected
for bad, why in [("#!AUTOTYPE v1\n#!nope 1\n---\nx", "unknown directive"),
                 ("#!AUTOTYPE v1\n#!wpm 85\nx\n---\ny", "junk in header"),
                 ("#!AUTOTYPE v1\n#!wpm 85\n", "no separator"),
                 ("#!AUTOTYPE v1\n#!wpm 0\n---\nx", "wpm 0"),
                 ("#!AUTOTYPE v1\n#!indent sideways\n---\nx", "bad indent")]:
    try:
        at.parse_payload(bad, "bad")
        check("reject: " + why, False, "accepted a bad payload")
    except ValueError:
        check("reject: " + why, True)

# 9. BOM is stripped, never typed
p = at.parse_payload("﻿hello", "bom")
check("bom-stripped", p.body == "hello", repr(p.body))

# 10. timing sanity: mean delay tracks wpm
import random
p = at.parse_payload("#!AUTOTYPE v1\n#!wpm 60\n#!thinking 0\n---\n" + ("a" * 4000), "t")
tm = at.Timing(p.settings, random.Random(7))
ds = [tm.delay_for("char", "a", ("char", "a")) for _ in range(4000)]
mean = sum(ds) / len(ds)
target = 1.0 / (60 * 5 / 60.0)
check("timing-mean-near-target", abs(mean - target) / target < 0.20,
      "mean=%.4f target=%.4f" % (mean, target))
check("timing-varies", len(set(round(d, 5) for d in ds)) > 3000)
check("timing-never-zero", min(ds) > 0)

# 11. end-to-end CLI dry run + verify on a big code payload
big = "#!AUTOTYPE v1\n#!wpm 100\n---\n" + (
    'import os, sys\n\n'
    'class Thing:\n'
    '    def __init__(self, name: str = "x"):\n'
    '        self.name = name\n'
    '        self._map = {\'k\': [1, 2, 3], "j": (4, 5)}\n'
    '\n'
    '    def go(self):\n'
    '        for i in range(10):\n'
    '            print(f"{i}: {self.name!r}")  # <- symbols ~!@#$%^&*()_+|\\\n'
    '        return None\n'
) * 3
with open("/tmp/big.txt", "w", encoding="utf-8") as fh:
    fh.write(big)
r = subprocess.run([sys.executable, "/home/claude/auto_typer.py", "/tmp/big.txt",
                    "--dry-run", "--verify"], capture_output=True, text=True)
check("cli-dry-run-verify", r.returncode == 0 and "VERIFY: exact match" in r.stdout,
      r.stdout + r.stderr)
print(r.stdout.strip())

# 12. CLI refuses an astral payload without --force
with open("/tmp/bad.txt", "w", encoding="utf-8") as fh:
    fh.write("hi \U0001F600")
r = subprocess.run([sys.executable, "/home/claude/auto_typer.py", "/tmp/bad.txt",
                    "--dry-run"], capture_output=True, text=True)
check("cli-refuses-astral", r.returncode == 2 and "refusing to type" in r.stderr,
      r.stdout + r.stderr)

print("\n%d checks, %d failures" % (len(RUN), len(FAILS)))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASS")
