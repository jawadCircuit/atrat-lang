#!/usr/bin/env python3
"""Run the end-to-end suite under ASan/UBSan when the host toolchain supports it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")
CC = os.environ.get("ATRAT_CC") or shutil.which("gcc") or shutil.which("clang")
FLAGS = "-fsanitize=address,undefined -fno-omit-frame-pointer -g"


def sanitizer_supported(cc: str) -> bool:
    if not cc:
        return False
    with tempfile.TemporaryDirectory(prefix="atrat-san-") as d:
        c = os.path.join(d, "probe.c")
        exe = os.path.join(d, "probe" + (".exe" if os.name == "nt" else ""))
        with open(c, "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n")
        p = subprocess.run([cc, "-fsanitize=address,undefined", c, "-o", exe],
                           capture_output=True, text=True)
        return p.returncode == 0


def main() -> int:
    if not sanitizer_supported(CC):
        print(f"SKIP: {CC or 'no C compiler'} does not support ASan/UBSan")
        return 0
    env = os.environ.copy()
    env["ATRAT_CC"] = CC
    env["ATRAT_CFLAGS"] = (env.get("ATRAT_CFLAGS", "") + " " + FLAGS).strip()
    env.setdefault("ASAN_OPTIONS", "detect_leaks=1:abort_on_error=1")
    env.setdefault("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "run_tests.py")],
                       cwd=ROOT, env=env)
    if p.returncode != 0:
        return p.returncode
    # Run the lifecycle examples explicitly too, so their strong-count output is
    # checked under the sanitizers rather than only in the normal test suite.
    with tempfile.TemporaryDirectory(prefix="atrat-san-ex-") as d:
        for name in ("lifecycle.atr", "ownership.atr", "result.atr"):
            exe = os.path.join(d, name.replace(".atr", ""))
            built = subprocess.run([sys.executable, ATRC, "build",
                                    os.path.join(ROOT, "examples", name),
                                    "-o", exe], cwd=ROOT, env=env,
                                   capture_output=True, text=True)
            ran = subprocess.run([exe], capture_output=True, text=True) if built.returncode == 0 else None
            if built.returncode != 0 or ran.returncode != 0:
                print(f"FAIL sanitizer example {name}")
                print((built.stderr or "")[:2000])
                if ran is not None:
                    print((ran.stderr or "")[:2000])
                return 1
    print("sanitizer lifecycle examples: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
