#!/usr/bin/env python3
"""Compile/run examples across optimisation levels and available C compilers."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")
EXAMPLES = ["demo.atr", "readme.atr", "shapes.atr", "generics.atr", "ownership.atr", "result.atr", "lifecycle.atr"]


def run_case(path: str, opt: str, cc: str, tmp: str) -> tuple[int, str, str]:
    exe = os.path.join(tmp, f"{os.path.basename(path)}.{opt}.{os.path.basename(cc)}.exe")
    env = os.environ.copy()
    env["ATRAT_CC"] = cc
    p = subprocess.run([sys.executable, ATRC, "build", path, "-O", opt,
                        "-o", exe], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return p.returncode, "", p.stderr
    r = subprocess.run([exe], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def generics_source(n: int = 80) -> str:
    lines = []
    for i in range(n):
        lines.append(f"struct S{i}:\n    x: i64\n")
    lines.append("fn id<T>(x: T) -> T:\n    return x\n")
    lines.append("fn main() -> i32:\n    var total = 0\n")
    for i in range(n):
        lines.append(f"    let v{i} = id(S{i} {{ x: {i} }})\n")
        lines.append(f"    total += v{i}.x\n")
    lines.append("    print(total)\n    return 0\n")
    return "".join(lines)


def main() -> int:
    compilers = []
    for name in ("gcc", "clang"):
        found = shutil.which(name)
        if found:
            compilers.append(found)
    if not compilers:
        print("SKIP: no gcc/clang")
        return 0
    failures = 0
    with tempfile.TemporaryDirectory(prefix="atrat-matrix-") as tmp:
        for ex in EXAMPLES:
            path = os.path.join(ROOT, "examples", ex)
            baseline = None
            for cc in compilers:
                for opt in ("0", "2", "3"):
                    rc, out, err = run_case(path, opt, cc, tmp)
                    if rc != 0:
                        print(f"FAIL {ex} O{opt} {cc}: {err[:300]}")
                        failures += 1
                        continue
                    if baseline is None:
                        baseline = out
                    elif out != baseline:
                        print(f"FAIL output mismatch {ex} O{opt} {cc}")
                        failures += 1
                    else:
                        print(f"ok   {ex} O{opt} {os.path.basename(cc)}")
        src = generics_source()
        path = os.path.join(tmp, "large_generics.atr")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        baseline = None
        for cc in compilers:
            for opt in ("0", "2"):
                rc, out, err = run_case(path, opt, cc, tmp)
                if rc != 0:
                    print(f"FAIL large-generics O{opt} {cc}: {err[:300]}")
                    failures += 1
                elif baseline is None:
                    baseline = out
                    print(f"ok   large-generics O{opt} {os.path.basename(cc)}")
                elif out != baseline:
                    print(f"FAIL large-generics output mismatch O{opt} {cc}")
                    failures += 1
                else:
                    print(f"ok   large-generics O{opt} {os.path.basename(cc)}")
    print(f"validation failures: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
