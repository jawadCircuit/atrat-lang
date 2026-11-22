"""Command line driver: check, build, run, or dump C for an Atrat program."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import ast as _ast
from .check import BorrowError, TypeError_, check_program
from .codegen import CodegenError, generate
from .lexer import AtratError, diagnostic_code, tokenize
from .parser import Parser, parse
from .runtime import RUNTIME
from .version import __version__

HERE = os.path.dirname(os.path.abspath(__file__))
PRELUDE = os.path.join(HERE, "prelude.atr")


def read_source(path: str) -> tuple[str, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), path
    except OSError as e:
        raise SystemExit(f"atrc: cannot read {path}: {e.strerror}")


class SourceMap:
    """Maps global (merged) line numbers back to the original file and line."""

    def __init__(self):
        self.segments = []

    def add(self, path, text, start):
        lines = text.splitlines() or [""]
        self.segments.append((start, start + len(lines), path, list(lines)))
        return len(lines)

    def locate(self, line):
        for a, b, path, lines in self.segments:
            if a <= line < b:
                return path, line - a + 1, lines
        return None


_SOURCES = SourceMap()
_NEXT_LINE = 1


def _reset_sources():
    global _SOURCES, _NEXT_LINE
    _SOURCES = SourceMap()
    _NEXT_LINE = 1


def _register(path: str, text: str) -> int:
    global _NEXT_LINE
    start = _NEXT_LINE
    _NEXT_LINE += len(text.splitlines() or [""]) + 1
    _SOURCES.add(path, text, start)
    return start


def _parse_with_offset(path: str, text: str, start: int):
    toks = tokenize(text, path)
    for t in toks:
        t.line += start - 1
    prog = Parser(toks, path, text).parse_program()
    _ast.stamp_sources(prog, os.path.realpath(path) if path != "<prelude>" else path)
    return prog


def _resolve_import(spec: str, base_dir: str, node) -> str:
    rel = spec
    if not rel.endswith(".atr"):
        rel = rel.replace(".", os.sep) + ".atr"
    if not os.path.isabs(rel):
        rel = os.path.join(base_dir, rel)
    if not os.path.isfile(rel):
        raise AtratError(f"cannot find module '{spec}'", node.line, node.col,
                         f"looked for {os.path.normpath(rel)}")
    return rel


def _load_module(path: str, seen: set, stack: list, out: list, via=None):
    from . import ast as _ast
    canon = os.path.realpath(path)
    if canon in stack:
        raise AtratError(f"cyclic import of '{os.path.basename(path)}'",
                         getattr(via, "line", 0), getattr(via, "col", 0),
                         "break the cycle or move the shared declarations out")
    if canon in seen:
        return
    text, filename = read_source(path)
    start = _register(filename, text)
    prog = _parse_with_offset(filename, text, start)
    stack.append(canon)
    base = os.path.dirname(os.path.abspath(filename))
    for d in prog.decls:
        if isinstance(d, _ast.Import):
            dep = _resolve_import(d.path, base, d)
            _load_module(dep, seen, stack, out, via=d)
    stack.pop()
    seen.add(canon)
    out.extend(d for d in prog.decls if not isinstance(d, _ast.Import))


def load_program(path: str):
    from . import ast as _ast
    _reset_sources()
    prelude = ""
    if os.path.isfile(PRELUDE):
        prelude, _ = read_source(PRELUDE)
    decls = []
    seen = set()
    if prelude.strip():
        start = _register("<prelude>", prelude)
        pre_prog = _parse_with_offset("<prelude>", prelude, start)
        decls.extend(pre_prog.decls)
        seen.add(os.path.realpath(PRELUDE))
    rest = []
    _load_module(path, seen, [], rest)
    prog = _ast.Program(decls=list(decls) + rest)
    return prog


def render_error(err: AtratError, src: str = "", filename: str = "") -> str:
    loc = _SOURCES.locate(err.line) if getattr(err, "line", 0) else None
    if loc is not None:
        filename, local, lines = loc
        line_no = local
        text = lines[local - 1] if 1 <= local <= len(lines) else ""
    else:
        lines = src.splitlines()
        line_no = err.line
        text = lines[line_no - 1] if 1 <= line_no <= len(lines) else ""
    code = diagnostic_code(err)
    out = [f"error[{code}]: {err.kind}: {err.msg}",
           f"  --> {filename}:{line_no}:{err.col}"]
    if text:
        out.append("   |")
        out.append(f"{line_no:>3} | {text}")
        col = max(1, err.col)
        out.append("   | " + " " * (col - 1) + "^")
    if getattr(err, "hint", ""):
        out.append(f"   = help: {err.hint}")
    for note in getattr(err, "notes", []):
        if isinstance(note, tuple):
            nline, ntext = note
            loc = _SOURCES.locate(nline)
            where = f"{loc[0]}:{loc[1]}" if loc is not None else f"line {nline}"
            out.append(f"   = note: {ntext} ({where})")
        else:
            out.append(f"   = note: {note}")
    return "\n".join(out)


def compile_source(path: str):
    prog = load_program(path)
    module = check_program(prog)
    return module, "", path


def load_safe(path):
    try:
        return read_source(path)
    except SystemExit:
        return "", path


def write_c(module, c_path: str) -> str:
    c_src = generate(module)
    with open(c_path, "w", encoding="utf-8") as f:
        f.write(c_src)
    return c_src


def find_cc() -> str:
    configured = os.environ.get("ATRAT_CC")
    if configured:
        return configured
    for name in ("gcc", "clang", "cc"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("atrc: no C compiler found (install gcc or clang)")


def build_c(c_path: str, exe_path: str, opt_level: str):
    cc = find_cc()
    extra = shlex.split(os.environ.get("ATRAT_CFLAGS", ""))
    cmd = [cc, "-std=gnu11", f"-O{opt_level}", "-fwrapv", *extra,
           "-o", exe_path, c_path, "-lm"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit("atrc: C compilation failed")


def cmd_check(args) -> int:
    try:
        module, _, _ = compile_source(args.file)
    except AtratError as e:
        print(render_error(e), file=sys.stderr)
        return 1
    print(f"ok: {args.file} type-checks and passes the borrow checker")
    return 0


def cmd_emit_c(args) -> int:
    try:
        module, _, _ = compile_source(args.file)
    except AtratError as e:
        print(render_error(e), file=sys.stderr)
        return 1
    c_src = generate(module)
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(c_src)
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(c_src)
    return 0


def build_exe(args) -> tuple[str, str]:
    module, _, _ = compile_source(args.file)
    tmpdir = tempfile.mkdtemp(prefix="atrc-")
    base = os.path.splitext(os.path.basename(args.file))[0]
    c_path = os.path.join(tmpdir, base + ".c")
    exe = args.output or os.path.join(tmpdir, base + (".exe" if os.name == "nt" else ""))
    write_c(module, c_path)
    build_c(c_path, exe, args.opt)
    return exe, c_path


def cmd_build(args) -> int:
    try:
        exe, c_path = build_exe(args)
    except AtratError as e:
        print(render_error(e), file=sys.stderr)
        return 1
    if args.keep_c:
        shutil.copyfile(c_path, args.keep_c)
        print(f"generated C: {args.keep_c}")
    print(f"built {exe}")
    return 0


def cmd_run(args) -> int:
    try:
        exe, _ = build_exe(args)
    except AtratError as e:
        print(render_error(e), file=sys.stderr)
        return 1
    proc = subprocess.run([exe] + args.program_args)
    return proc.returncode


def cmd_runtime(args) -> int:
    sys.stdout.write(RUNTIME)
    return 0


def compile_source_text(text: str):
    with open(PRELUDE, "r", encoding="utf-8") as f:
        prelude = f.read()
    src = prelude + "\n" + text
    prog = parse(src, "<repl>")
    return check_program(prog), src, "<repl>"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="atrc",
        description="Atrat: one language with Rust safety, Python ergonomics, "
                    "C speed, Lua weight, Haskell types and assembly control.")
    p.add_argument("--version", action="version", version=f"atrc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, output=False):
        sp.add_argument("file", help="source file (.atr)")
        sp.add_argument("-O", "--opt", default="2",
                        choices=["0", "1", "2", "3", "s"],
                        help="C compiler optimisation level (default 2)")
        if output:
            sp.add_argument("-o", "--output", default=None, help="output executable path")

    sp = sub.add_parser("check", help="type-check and borrow-check only")
    common(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("build", help="compile to a native executable")
    common(sp, output=True)
    sp.add_argument("--keep-c", default=None, help="also write the generated C here")
    sp.set_defaults(func=cmd_build)

    sp = sub.add_parser("run", help="compile and run")
    common(sp, output=True)
    sp.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to the program")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("emit-c", help="print the generated C")
    common(sp)
    sp.add_argument("-o", "--output", default=None, help="write C to a file ('-' = stdout)")
    sp.set_defaults(func=cmd_emit_c)

    sp = sub.add_parser("runtime", help="print the embedded C runtime")
    sp.set_defaults(func=cmd_runtime)

    args = p.parse_args(argv)
    if hasattr(args, "args"):
        args.program_args = args.args
        del args.args
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
