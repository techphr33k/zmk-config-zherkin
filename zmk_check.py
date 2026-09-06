#!/usr/bin/env python3
"""
zmk-check - a fast static pre-flight checker for ZMK keymaps.

Catches the mistakes that cost you a 5-minute GitHub Actions round trip:
unclosed bindings blocks, undefined keycodes, wrong binding-cell counts,
layer counts that don't match the matrix, references to layers that don't
exist, and out-of-range combo positions.

It works by running the real C preprocessor over your keymap with ZMK's
own headers on the include path, then parsing the expanded devicetree.
That means keycode and behavior validation is driven by ZMK's actual
source, not a hand-maintained list that goes stale.

Usage:
    zmk_check.py                       # auto-discover keymaps in cwd
    zmk_check.py path/to/repo
    zmk_check.py boards/shields/foo/foo.keymap
    zmk_check.py --zmk ~/src/zmk       # use an existing ZMK checkout
    zmk_check.py --keys 34             # override key count detection

Exit code is 0 if clean, 1 for keymap errors, 2 for setup/usage errors.
This is a static checker, not a firmware build or full devicetree validator.

Requires: Python 3.10+, GCC/Clang/Zig, ZMK and Zephyr headers.
--syntax-only requires just Python. --fetch downloads ZMK headers only;
Zephyr's include directory is discovered beside ZMK or supplied with -I.
"""

from __future__ import annotations

import argparse
import ast
import operator
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ZMK_REPO = "https://github.com/zmkfirmware/zmk.git"
CACHE_DIR = Path.home() / ".cache" / "zmk-check" / "zmk"


class SetupError(Exception):
    """A required tool or source tree is unavailable."""


class PreprocessError(Exception):
    """The compiler could not expand a keymap."""

# Behaviors whose first cell is a layer index.
LAYER_ARG_BEHAVIORS = {"mo": 0, "to": 0, "tog": 0, "sl": 0, "lt": 0}


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------

@dataclass
class Diag:
    level: str  # "error" | "warn"
    file: str
    line: int
    code: str
    msg: str

    def render(self, color: bool) -> str:
        tag = "error" if self.level == "error" else "warning"
        if color:
            hue = "\033[31m" if self.level == "error" else "\033[33m"
            tag = f"{hue}{tag}\033[0m"
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"{loc}: {tag}: [{self.code}] {self.msg}"


class Report:
    def __init__(self) -> None:
        self.diags: list[Diag] = []

    def error(self, file: str, line: int, code: str, msg: str) -> None:
        self.diags.append(Diag("error", file, line, code, msg))

    def warn(self, file: str, line: int, code: str, msg: str) -> None:
        self.diags.append(Diag("warn", file, line, code, msg))

    @property
    def errors(self) -> int:
        return sum(1 for d in self.diags if d.level == "error")

    @property
    def warnings(self) -> int:
        return sum(1 for d in self.diags if d.level == "warn")


# --------------------------------------------------------------------------
# ZMK source discovery
# --------------------------------------------------------------------------

def find_zmk(explicit: str | None, fetch: bool) -> Path:
    candidates = []
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not (candidate / "app" / "dts" / "behaviors.dtsi").is_file():
            raise SetupError(f"Not a ZMK source tree: {candidate}")
        return candidate
    if os.environ.get("ZMK_SRC"):
        candidates.append(Path(os.environ["ZMK_SRC"]).expanduser())
    candidates += [CACHE_DIR, Path("zmk"), Path("../zmk"), Path("../../zmk")]

    for c in candidates:
        if (c / "app" / "dts" / "behaviors.dtsi").exists():
            return c.resolve()

    if not fetch:
        raise SetupError(
            "Could not find a ZMK source tree.\n"
            "Point at one with --zmk PATH, or run with --fetch to download a\n"
            "small sparse checkout into ~/.cache/zmk-check/zmk."
        )

    print(f"Fetching ZMK headers into {CACHE_DIR} ...", file=sys.stderr)
    CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    if CACHE_DIR.exists():
        raise SetupError(f"Incomplete cache at {CACHE_DIR}; choose a checkout with --zmk. "
                         "The existing directory was left intact.")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
         ZMK_REPO, str(CACHE_DIR)],
        check=True,
    )
    subprocess.run(
        ["git", "sparse-checkout", "set", "app/dts", "app/include"],
        cwd=CACHE_DIR, check=True,
    )
    return CACHE_DIR


# --------------------------------------------------------------------------
# Stage 1: structural check on the raw source
# --------------------------------------------------------------------------

def strip_comments(text: str) -> str:
    """Blank comments and neutralize delimiters in strings; preserve offsets."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif two == "//":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif text[i] == '"':
            # Keep the string's content (we match on `compatible` values) but
            # neutralise anything that would confuse bracket matching.
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            for k in range(i, j):
                if out[k] in "{}<>;":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


DIRECTIVE = re.compile(
    r'^\s*#\s*(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|pragma|'
    r'error|warning|line|\d+)\b')
CONDITIONAL = re.compile(r'^\s*#\s*(?:if|ifdef|ifndef)\b', re.M)


def mask_directives(text: str) -> str:
    """Ignore preprocessing syntax without hiding DTS #binding-cells."""
    lines = []
    continued = False
    for line in text.splitlines(keepends=True):
        directive = continued or bool(DIRECTIVE.match(line))
        continued = directive and line.rstrip().endswith('\\')
        lines.append(''.join('\n' if c == '\n' else ' ' for c in line)
                     if directive else line)
    return ''.join(lines)


def check_structure(path: Path, raw: str, rep: Report) -> bool:
    """Bracket balance. Returns True if the file is parseable enough to go on."""
    text = mask_directives(strip_comments(raw))
    line = 1
    stack: list[tuple[str, int]] = []
    paren_depth = 0
    ok = True

    for offset, ch in enumerate(text):
        if ch == "\n":
            line += 1
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "{":
            stack.append(("{", line))
        elif ch == "}":
            if stack and stack[-1][0] == "<":
                open_line = stack[-1][1]
                rep.error(
                    str(path), line, "unclosed-cells",
                    f"'}}' closes a node while a '<' value block opened at line "
                    f"{open_line} is still open - you are probably missing a '>;'",
                )
                ok = False
                stack.pop()
                if stack and stack[-1][0] == "{":
                    stack.pop()
            elif stack and stack[-1][0] == "{":
                stack.pop()
            else:
                rep.error(str(path), line, "stray-brace", "unmatched '}'")
                ok = False
        # '<' is only a bracket outside parenthesised expressions
        elif ch == "<" and paren_depth == 0:
            stack.append(("<", line))
        elif ch == ">" and paren_depth == 0:
            if stack and stack[-1][0] == "<":
                stack.pop()
                tail = text[offset + 1:].lstrip()
                if not tail.startswith((";", ",")):
                    rep.error(str(path), line, "missing-semicolon",
                              "expected ';' or ',' after '>'")
                    ok = False
            else:
                rep.error(str(path), line, "stray-angle", "unmatched '>'")
                ok = False

    for kind, open_line in stack:
        what = "node '{'" if kind == "{" else "value block '<'"
        rep.error(str(path), open_line, "unclosed", f"{what} is never closed")
        ok = False
    return ok


# --------------------------------------------------------------------------
# Stage 2: preprocess
# --------------------------------------------------------------------------

def compiler_command(explicit: str | None = None) -> list[str]:
    compiler = explicit or os.environ.get("ZMK_CPP")
    if not compiler:
        compiler = next((p for name in ("cpp", "clang-cpp", "clang", "gcc", "zig")
                         if (p := shutil.which(name))), None)
    if not compiler:
        raise SetupError("No C preprocessor found. Install GCC, Clang, or Zig; "
                         "use --cpp PATH. --syntax-only needs only Python.")
    return [compiler, "cc"] if Path(compiler).stem.lower() == "zig" else [compiler]


def preprocess(path: Path, zmk: Path, extra_includes: list[Path],
               cpp: str | None = None, defines: list[str] | None = None) -> str:
    """Use real headers; missing includes must never be silently stubbed."""
    cmd = compiler_command(cpp) + ["-E", "-undef", "-nostdinc", "-D__DTS__",
                                  "-x", "assembler-with-cpp"]
    includes = [path.parent, *extra_includes, zmk / "app" / "dts",
                zmk / "app" / "include", zmk.parent / "zephyr" / "include"]
    for inc in includes:
        cmd += ["-I", str(inc.resolve())]
    for define in defines or []:
        cmd += ["-D", define]
    cmd.append(str(path.resolve()))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode:
        raise PreprocessError(proc.stderr.strip() or "C preprocessing failed")
    return proc.stdout


class LineMap:
    """Maps preprocessed line numbers back to (original file, line)."""

    def __init__(self, text: str, default_file: str) -> None:
        self.entries: list[tuple[int, str, int]] = [(1, default_file, 1)]
        marker = re.compile(r'^#\s+(\d+)\s+"((?:\\.|[^"\\])*)"')
        for i, ln in enumerate(text.split("\n"), start=1):
            m = marker.match(ln)
            if m:
                filename = re.sub(r'\\([\\"])', r'\1', m.group(2))
                self.entries.append((i + 1, filename, int(m.group(1))))

    def lookup(self, pp_line: int) -> tuple[str, int]:
        lo, hi = 0, len(self.entries) - 1
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.entries[mid][0] <= pp_line:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        start, fname, fline = self.entries[best]
        return fname, fline + (pp_line - start)


# --------------------------------------------------------------------------
# Stage 3: parse the expanded devicetree
# --------------------------------------------------------------------------

@dataclass
class Node:
    label: str | None
    name: str
    start: int          # offset of '{'
    end: int            # offset of matching '}'
    body: str
    body_start: int
    children: list["Node"] = field(default_factory=list)


NODE_RE = re.compile(r"(?:([A-Za-z_]\w*)\s*:\s*)?([\w,.+@-]+)\s*\{")


def match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def parse_nodes(text: str, base: int = 0) -> list[Node]:
    """Parse direct-child nodes of a devicetree body."""
    nodes: list[Node] = []
    i = 0
    while True:
        m = NODE_RE.search(text, i)
        if not m:
            break
        open_idx = m.end() - 1
        close_idx = match_brace(text, open_idx)
        if close_idx == -1:
            break
        body = text[open_idx + 1:close_idx]
        nodes.append(Node(m.group(1), m.group(2), base + m.start(),
                          base + close_idx, body, base + open_idx + 1))
        i = close_idx + 1
    return nodes


def property_span(body: str, name: str) -> tuple[str, int] | None:
    """Return property text and its offset inside this node's body."""
    depth = 0
    i = 0
    pat = re.compile(r"(?<![\w-])" + re.escape(name) + r"\s*=")
    while i < len(body):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            m = pat.match(body, i)
            if m:
                j = body.find(";", m.end())
                return body[m.end():j if j != -1 else len(body)], m.end()
        i += 1
    return None


def property_value(body: str, name: str) -> str | None:
    span = property_span(body, name)
    return span[0] if span else None


def collect_behaviors(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Return (behaviors, sensor_behaviors) as label -> cell count."""
    out: dict[str, int] = {}
    sensors: dict[str, int] = {}
    for m in re.finditer(r"([A-Za-z_]\w*)\s*:\s*[\w,.+@-]+\s*\{", text):
        close = match_brace(text, m.end() - 1)
        if close == -1:
            continue
        body = text[m.end():close]
        for prop, table in (("#binding-cells", out), ("#sensor-binding-cells", sensors)):
            value = property_value(body, prop)
            if value:
                count = eval_cell(value.strip().removeprefix('<').removesuffix('>'))
                if count is not None:
                    table[m.group(1)] = count
    return out, sensors


CELL_TOKEN = re.compile(r"&\w+|\(|\)|<|>|,|[^\s(),<>]+")


@dataclass
class Invocation:
    behavior: str
    cells: list[str]
    offset: int


def parse_bindings(value: str, base_offset: int) -> list[Invocation]:
    """Split a `bindings = < ... >` value into behavior invocations."""
    body = value
    invs: list[Invocation] = []
    cur: Invocation | None = None
    i = 0
    while i < len(body):
        m = CELL_TOKEN.search(body, i)
        if not m:
            break
        tok = m.group(0)
        i = m.end()
        if tok in ("<", ">", ","):
            continue
        if re.fullmatch(r"&\w+", tok):
            cur = Invocation(tok[1:], [], base_offset + m.start())
            invs.append(cur)
            continue
        if tok == "(":
            depth, j = 1, i
            while j < len(body) and depth:
                if body[j] == "(":
                    depth += 1
                elif body[j] == ")":
                    depth -= 1
                j += 1
            tok = "(" + body[i:j]
            i = j
        elif tok == ")":
            continue
        if cur is not None:
            cur.cells.append(tok)
    return invs


IDENT_RE = re.compile(r"(?<![\w.])(?!0[xX])[A-Za-z_]\w*")


def cell_is_resolved(cell: str) -> str | None:
    """Return the first unresolved identifier in a cell, or None."""
    m = IDENT_RE.search(cell)
    return m.group(0) if m else None


def eval_cell(cell: str) -> int | None:
    """Evaluate a bounded integer expression without executing Python code."""
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.BitOr: operator.or_, ast.BitAnd: operator.and_, ast.BitXor: operator.xor,
              ast.LShift: operator.lshift, ast.RShift: operator.rshift}
    unary = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Invert: operator.invert}

    def integer(node: ast.AST) -> int:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            value = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in unary:
            value = unary[type(node.op)](integer(node.operand))
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = integer(node.left), integer(node.right)
            if isinstance(node.op, (ast.LShift, ast.RShift)) and not 0 <= right <= 63:
                raise ValueError("shift out of range")
            value = binary[type(node.op)](left, right)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Mod)):
            left, right = integer(node.left), integer(node.right)
            quotient = abs(left) // abs(right) * (-1 if (left < 0) != (right < 0) else 1)
            value = quotient if isinstance(node.op, ast.Div) else left - quotient * right
        else:
            raise ValueError("unsupported expression")
        if value.bit_length() > 128:
            raise ValueError("integer out of range")
        return value

    try:
        if len(cell) > 1024:
            return None
        expression = ast.parse(cell.strip(), mode="eval")
        if sum(1 for _ in ast.walk(expression)) > 128:
            return None
        return integer(expression.body)
    except (ValueError, SyntaxError, ArithmeticError, RecursionError):
        return None


# --------------------------------------------------------------------------
# Key-count detection
# --------------------------------------------------------------------------

def _layout_candidates(keymap: Path) -> list[Path]:
    """Files that might carry this keymap's key layout, most likely first."""
    stem = keymap.stem
    files = sorted(keymap.parent.glob("*.overlay")) + \
        sorted(keymap.parent.glob("*.dtsi"))
    return sorted(files, key=lambda f: (not f.stem.startswith(stem), f.name))


def detect_key_count(keymap: Path, rep: Report) -> tuple[set[int], str]:
    """Find plausible key counts from the matrix transform, physical layout,
    or - failing both - the kscan definition."""
    for candidate in _layout_candidates(keymap):
        raw = strip_comments(candidate.read_text())

        counts: set[int] = set()
        for m in re.finditer(r'compatible\s*=\s*"zmk,matrix-transform"', raw):
            start = raw.rfind("{", 0, m.start())
            body = raw[start + 1:match_brace(raw, start)]
            mp = property_value(body, "map")
            if mp:
                n = len(re.findall(r"\bRC\s*\(", mp))
                if n:
                    counts.add(n)
                    continue
            cols, rows = property_value(body, "columns"), property_value(body, "rows")
            if cols and rows:
                c, r = eval_cell(cols.strip(" <>")), eval_cell(rows.strip(" <>"))
                if c and r:
                    counts.add(c * r)
        if counts:
            return counts, f"matrix-transform in {candidate.name}"

        for m in re.finditer(r'compatible\s*=\s*"zmk,physical-layout"', raw):
            start = raw.rfind("{", 0, m.start())
            body = raw[start + 1:match_brace(raw, start)]
            keys = property_value(body, "keys")
            if keys:
                n = len(re.findall(r"&key_physical_attrs", keys))
                if n:
                    counts.add(n)
        if counts:
            # Several layouts may describe the same board, or halves of it.
            if len(counts) > 1:
                counts.add(sum(counts))
            return counts, f"physical-layout in {candidate.name}"

        for m in re.finditer(r'compatible\s*=\s*"zmk,kscan-gpio-(matrix|direct)"', raw):
            start = raw.rfind("{", 0, m.start())
            body = raw[start + 1:match_brace(raw, start)]
            if m.group(1) == "direct":
                gp = property_value(body, "input-gpios")
                if gp:
                    n = gp.count("<")
                    if n:
                        return {n}, f"direct kscan in {candidate.name}"
            else:
                rg = property_value(body, "row-gpios")
                cg = property_value(body, "col-gpios")
                if rg and cg:
                    r, c = rg.count("<"), cg.count("<")
                    if r and c:
                        return {r * c}, f"{r}x{c} kscan in {candidate.name}"
    return set(), ""


# --------------------------------------------------------------------------
# Main check
# --------------------------------------------------------------------------

def check_keymap(keymap: Path, zmk: Path, rep: Report,
                 key_override: int | None, cpp: str | None = None,
                 includes: list[Path] | None = None,
                 defines: list[str] | None = None) -> None:
    raw = keymap.read_text(encoding="utf-8-sig")
    if not CONDITIONAL.search(strip_comments(raw)) and not check_structure(keymap, raw, rep):
        rep.warn(str(keymap), 0, "skipped",
                 "structural errors above - skipping semantic checks")
        return

    try:
        pp = preprocess(keymap, zmk, includes or [], cpp, defines)
    except PreprocessError as exc:
        rep.error(str(keymap), 0, "preprocess", str(exc))
        return
    lm = LineMap(pp, str(keymap))
    structure = Report()
    if not check_structure(keymap, pp, structure):
        for diag in structure.diags:
            diag.file, diag.line = lm.lookup(diag.line)
            rep.diags.append(diag)
        return
    clean = mask_directives(strip_comments(pp))

    def loc(offset: int) -> tuple[str, int]:
        return lm.lookup(clean.count("\n", 0, offset) + 1)

    behaviors, sensor_behaviors = collect_behaviors(clean)

    # --- locate the keymap node ---
    km = re.search(r'compatible\s*=\s*"zmk,keymap"', clean)
    if not km:
        rep.error(str(keymap), 0, "no-keymap", "no zmk,keymap node found")
        return
    km_start = clean.rfind("{", 0, km.start())
    km_end = match_brace(clean, km_start)
    layers = parse_nodes(clean[km_start + 1:km_end], km_start + 1)

    if not layers:
        rep.error(str(keymap), 0, "no-layers", "keymap node has no layers")
        return

    expected, source = ({key_override}, "--keys") if key_override else \
        detect_key_count(keymap, rep)
    if not expected:
        rep.warn(str(keymap), 0, "no-key-count",
                 "could not determine key count (no matrix transform or "
                 "physical layout found) - skipping layer size checks")

    warned_collision = False

    def check_binding_list(value: str, offset: int, ctx: str,
                           table: dict[str, int] | None = None
                           ) -> list[Invocation]:
        table = behaviors if table is None else table
        prefix = value.split('&', 1)[0]
        if prefix.strip(" \t\r\n<>,"):
            f, l = loc(offset)
            rep.error(f, l, "orphan-cell", f"value before the first &behavior ({ctx})")
        invs = parse_bindings(value, offset)
        for inv in invs:
            f, l = loc(inv.offset)
            if inv.behavior not in table:
                rep.error(f, l, "unknown-behavior",
                          f"&{inv.behavior} is not a defined behavior")
                continue
            want = table[inv.behavior]
            if len(inv.cells) != want:
                rep.error(f, l, "cell-count",
                          f"&{inv.behavior} takes {want} parameter"
                          f"{'' if want == 1 else 's'}, got {len(inv.cells)} "
                          f"({ctx})")
            for cell in inv.cells:
                bad = cell_is_resolved(cell)
                if bad:
                    rep.error(f, l, "undefined-keycode",
                              f"'{bad}' did not resolve to a value - not a "
                              f"valid ZMK keycode or macro")
            idx = LAYER_ARG_BEHAVIORS.get(inv.behavior)
            if idx is not None and len(inv.cells) > idx:
                n = eval_cell(inv.cells[idx])
                if n is not None:
                    if n < 0 or n >= len(layers):
                        rep.error(f, l, "bad-layer",
                                  f"&{inv.behavior} targets layer {n}, but only "
                                  f"{len(layers)} layers are defined (0-"
                                  f"{len(layers) - 1})")
        return invs

    # --- per-layer checks ---
    for i, layer in enumerate(layers):
        value = property_value(layer.body, "bindings")
        f, l = loc(layer.start)
        if layer.name.isdigit() and not warned_collision:
            warned_collision = True
            rep.warn(f, l, "name-macro-collision",
                     f"layer {i}'s node name expanded to '{layer.name}' - it "
                     f"collides with a #define. Legal, but confusing; rename "
                     f"the node or the macro")
        if value is None:
            rep.error(f, l, "no-bindings",
                      f"layer {i} '{layer.name}' has no bindings property")
            continue
        span = property_span(layer.body, "bindings")
        assert span is not None
        invs = check_binding_list(value, layer.body_start + span[1], f"layer {i} '{layer.name}'")
        sens = property_span(layer.body, "sensor-bindings")
        if sens:
            check_binding_list(sens[0], layer.body_start + sens[1],
                               f"layer {i} '{layer.name}' sensor-bindings",
                               sensor_behaviors)
        if expected and len(invs) not in expected:
            want = " or ".join(str(n) for n in sorted(expected))
            rep.error(f, l, "layer-size",
                      f"layer {i} '{layer.name}' has {len(invs)} bindings, "
                      f"expected {want} (from {source})")

    # --- combos ---
    cb = re.search(r'compatible\s*=\s*"zmk,combos"', clean)
    if cb:
        cb_start = clean.rfind("{", 0, cb.start())
        cb_end = match_brace(clean, cb_start)
        for combo in parse_nodes(clean[cb_start + 1:cb_end], cb_start + 1):
            f, l = loc(combo.start)
            pos = property_value(combo.body, "key-positions")
            if pos is None:
                rep.error(f, l, "combo-no-positions",
                          f"combo '{combo.name}' has no key-positions")
            elif expected:
                nums = [eval_cell(t) for t in
                        re.findall(r"[^\s<>,]+", pos)]
                nums = [n for n in nums if n is not None]
                if len(nums) < 2:
                    rep.warn(f, l, "combo-single",
                             f"combo '{combo.name}' has fewer than 2 positions")
                limit = max(expected)
                for n in nums:
                    if n < 0 or n >= limit:
                        rep.error(f, l, "combo-range",
                                  f"combo '{combo.name}' uses key position {n}, "
                                  f"but the board only has {limit} keys "
                                  f"(0-{limit - 1})")
            val = property_value(combo.body, "bindings")
            if val is None:
                rep.error(f, l, "combo-no-bindings",
                          f"combo '{combo.name}' has no bindings")
            else:
                span = property_span(combo.body, "bindings")
                assert span is not None
                check_binding_list(val, combo.body_start + span[1], f"combo '{combo.name}'")


# --------------------------------------------------------------------------

def discover(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for p in paths or ["."]:
        path = Path(p)
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found += sorted(path.rglob("*.keymap"))
        else:
            raise SetupError(f"No such file or directory: {p}")
    return list(dict.fromkeys(f.resolve() for f in found if ".git" not in f.parts))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Static pre-flight checker for ZMK keymaps.")
    ap.add_argument("paths", nargs="*", help="keymap files or a repo root")
    ap.add_argument("--zmk", help="path to a ZMK checkout")
    ap.add_argument("--fetch", action="store_true",
                    help="download ZMK headers if not found locally")
    ap.add_argument("--syntax-only", action="store_true",
                    help="check raw delimiter syntax without ZMK or a compiler")
    ap.add_argument("--cpp", help="path to cpp, GCC, Clang, or Zig executable")
    ap.add_argument("-I", "--include", action="append", default=[], type=Path,
                    help="additional include directory (repeatable, e.g. zephyr/include)")
    ap.add_argument("-D", "--define", action="append", default=[],
                    help="preprocessor definition, e.g. CONFIG_FOO=1 (repeatable)")
    ap.add_argument("--keys", type=int,
                    help="override the expected key count per layer")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()
    if args.keys is not None and args.keys <= 0:
        ap.error("--keys must be a positive integer")

    color = not args.no_color and sys.stdout.isatty()
    keymaps = discover(args.paths)
    if not keymaps:
        print("No .keymap files found.", file=sys.stderr)
        return 1

    rep = Report()
    pending = []
    for km in keymaps:
        raw = km.read_text(encoding="utf-8-sig")
        if CONDITIONAL.search(strip_comments(raw)):
            if args.syntax_only:
                rep.error(str(km), 0, "needs-preprocessing",
                          "conditional directives require a check without --syntax-only")
            else:
                pending.append(km)
        elif check_structure(km, raw, rep):
            pending.append(km)
    if pending and not args.syntax_only:
        zmk = find_zmk(args.zmk, args.fetch)
        for km in pending:
            check_keymap(km, zmk, rep, args.keys, args.cpp, args.include, args.define)

    for d in sorted(rep.diags, key=lambda d: (d.file, d.line or 10**9)):
        print(d.render(color))

    n = len(keymaps)
    print(f"\n{n} keymap{'' if n == 1 else 's'} checked, "
          f"{rep.errors} error{'' if rep.errors == 1 else 's'}, "
          f"{rep.warnings} warning{'' if rep.warnings == 1 else 's'}.")
    if args.syntax_only:
        print("Syntax-only: headers, bindings, and firmware compilation were not checked.")
    elif not rep.errors:
        print("Static checks passed; run the ZMK firmware build to verify compilation.")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SetupError, OSError, subprocess.CalledProcessError) as exc:
        print(f"zmk-check: {exc}", file=sys.stderr)
        sys.exit(2)
