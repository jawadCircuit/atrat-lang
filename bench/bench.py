#!/usr/bin/env python3
"""Micro-benchmarks: Atrat versus the equivalent hand-written C.

Run: python bench/bench.py [--runs N]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")

PROGRAMS = {
    "loop-100M": (
        """
fn main() -> i32:
    var acc: u64 = 0
    for i in 0..100000000:
        acc += 1
    print(acc)
    return 0
""",
        """
#include <stdio.h>
#include <stdint.h>
int main(void) {
    uint64_t acc = 0;
    for (uint64_t i = 0; i < 100000000ULL; i++) acc += 1;
    printf("%llu\\n", (unsigned long long)acc);
    return 0;
}
""",
    ),
    "fib-32": (
        """
fn fib(n: i64) -> i64:
    match n:
        0 -> 0
        1 -> 1
        _ -> fib(n - 1) + fib(n - 2)

fn main() -> i32:
    print(fib(32))
    return 0
""",
        """
#include <stdio.h>
#include <stdint.h>
static int64_t fib(int64_t n) {
    if (n == 0) return 0;
    if (n == 1) return 1;
    return fib(n - 1) + fib(n - 2);
}
int main(void) { printf("%lld\\n", (long long)fib(32)); return 0; }
""",
    ),
    "string-churn": (
        """
fn main() -> i32:
    var total: u64 = 0
    var i: i64 = 0
    while i < 200000:
        let s: String = "item-{i}"
        total += s.len() as u64
        i += 1
    print(total)
    return 0
""",
        """
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
int main(void) {
    uint64_t total = 0;
    for (int64_t i = 0; i < 200000; i++) {
        char buf[64];
        int n = snprintf(buf, sizeof buf, "item-%lld", (long long)i);
        char* s = (char*)malloc((size_t)n + 1);
        memcpy(s, buf, (size_t)n + 1);
        total += strlen(s);
        free(s);
    }
    printf("%llu\\n", (unsigned long long)total);
    return 0;
}
""",
    ),
}


def best_time(cmd: list[str], runs: int) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, text=True)
        elapsed = time.perf_counter() - start
        if proc.returncode != 0:
            raise SystemExit(f"command failed: {' '.join(cmd)}\n{proc.stderr}")
        best = min(best, elapsed)
    return best


def main() -> int:
    runs = 3
    if "--runs" in sys.argv:
        runs = int(sys.argv[sys.argv.index("--runs") + 1])

    cc = shutil.which("gcc") or shutil.which("clang")
    if not cc:
        raise SystemExit("bench: no C compiler found")

    tmp = tempfile.mkdtemp(prefix="atrat-bench-")
    print(f"best of {runs} runs, gcc -O2\n")
    print(f"{'benchmark':<16}{'C':>9}{'Atrat':>9}{'Atrat/C':>9}")
    print("-" * 43)

    for name, (atr_src, c_src) in PROGRAMS.items():
        atr_path = os.path.join(tmp, name + ".atr")
        c_path = os.path.join(tmp, name + ".c")
        with open(atr_path, "w", encoding="utf-8") as f:
            f.write(atr_src)
        with open(c_path, "w", encoding="utf-8") as f:
            f.write(c_src)
        atr_exe = os.path.join(tmp, name + "-atr" + (".exe" if os.name == "nt" else ""))
        c_exe = os.path.join(tmp, name + "-c" + (".exe" if os.name == "nt" else ""))
        built = subprocess.run([sys.executable, ATRC, "build", atr_path,
                                "-o", atr_exe], capture_output=True, text=True)
        if built.returncode != 0:
            raise SystemExit(f"atrat build failed for {name}:\n{built.stderr}")
        subprocess.run([cc, "-std=gnu11", "-O2", "-fwrapv", "-o", c_exe, c_path],
                       check=True, capture_output=True)
        t_c = best_time([c_exe], runs)
        t_a = best_time([atr_exe], runs)
        ratio = t_a / t_c if t_c else 0.0
        print(f"{name:<16}{t_c * 1000:>8.1f}ms{t_a * 1000:>8.1f}ms{ratio:>9.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
