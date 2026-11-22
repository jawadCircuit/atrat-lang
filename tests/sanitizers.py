#!/usr/bin/env python3
"""Run the end-to-end suite under ASan/UBSan when the host toolchain supports it."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")
FLAGS = "-fsanitize=address,undefined -fno-omit-frame-pointer -g"


def compiler_candidates() -> list[str]:
    """Prefer an explicitly configured compiler, then a sanitizer-capable one."""
    out = []
    explicit = os.environ.get("ATRAT_CC")
    if explicit:
        out.append(explicit)
    for name in ("clang", "gcc"):
        found = shutil.which(name)
        if found:
            out.append(found)
    local = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(local, "Microsoft", "WinGet", "Packages", "*LLVM*",
                     "llvm-*", "bin", "clang.exe"),
        os.path.join(local, "Programs", "LLVM", "bin", "clang.exe"),
        r"C:\Program Files\LLVM\bin\clang.exe",
        "/usr/bin/clang",
    ]
    for pat in patterns:
        out.extend(glob.glob(pat))
    seen = set()
    uniq = []
    for c in out:
        key = os.path.normcase(os.path.abspath(c)) if os.path.exists(c) else c
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


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


def pick_compiler():
    tried = []
    for cc in compiler_candidates():
        tried.append(cc)
        if sanitizer_supported(cc):
            return cc
    print("SKIP: no sanitizer-capable C compiler; tried " + ", ".join(tried or ["<none>"]))
    return None


def main() -> int:
    cc = pick_compiler()
    if cc is None:
        return 0
    env = os.environ.copy()
    env["ATRAT_CC"] = cc
    env["ATRAT_CFLAGS"] = (env.get("ATRAT_CFLAGS", "") + " " + FLAGS).strip()
    # Windows ASan uses a runtime DLL shipped next to clang.exe.
    env["PATH"] = os.path.dirname(os.path.abspath(cc)) + os.pathsep + env.get("PATH", "")
    if os.name == "nt":
        # Windows ASan supports memory errors but not LeakSanitizer.
        env.setdefault("ASAN_OPTIONS", "abort_on_error=1")
    else:
        env.setdefault("ASAN_OPTIONS", "detect_leaks=1:abort_on_error=1")
    env.setdefault("UBSAN_OPTIONS", "halt_on_error=1:print_stacktrace=1")
    print(f"sanitizer compiler: {cc}")
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "run_tests.py")],
                       cwd=ROOT, env=env)
    if p.returncode != 0:
        return p.returncode
    # Run the lifecycle examples explicitly too, so their strong-count output is
    # checked under the sanitizers rather than only in the normal test suite.
    runtime_dir = os.path.dirname(os.path.abspath(cc))
    runtime_dlls = ("libclang_rt.asan_dynamic-x86_64.dll", "libc++.dll",
                    "libunwind.dll")
    with tempfile.TemporaryDirectory(prefix="atrat-san-ex-") as d:
        for name in ("lifecycle.atr", "ownership.atr", "result.atr"):
            exe = os.path.join(d, name.replace(".atr", "")
                               + (".exe" if os.name == "nt" else ""))
            built = subprocess.run([sys.executable, ATRC, "build",
                                    os.path.join(ROOT, "examples", name),
                                    "-o", exe], cwd=ROOT, env=env,
                                   capture_output=True, text=True)
            if built.returncode == 0 and os.name == "nt":
                for dll in runtime_dlls:
                    src = os.path.join(runtime_dir, dll)
                    if os.path.exists(src):
                        shutil.copy2(src, d)
            ran = subprocess.run([exe], capture_output=True, text=True,
                                 env=env) if built.returncode == 0 else None
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
