#!/usr/bin/env python3
"""Small deterministic mutation fuzzer for the Atrat front end and C backend.

It does not try to prove correctness. It checks that malformed or unusual input
never produces an unhandled Python traceback or an unexpected process exit.
"""
from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")

SEEDS = [
r'''struct P:
    x: i64
    s: String
impl P:
    fn get(&self) -> &i64:
        return &self.x
fn main() -> i32:
    let p = P { x: 1, s: "p" }
    print(*p.get())
    return 0
''',
r'''trait Show:
    fn show(&self) -> String
struct H:
    n: String
impl Show for H:
    fn show(&self) -> String:
        # Repeated seed insertion can grow a mutated program without bound. Keep
    # cases small so a pathological parse cannot masquerade as a hang.
    return s[:16000]elf.n.clone()
fn main() -> i32:
    let d: Box<dyn Show> = box(H { n: "x" })
    print(d.show())
    return 0
''',
r'''enum E:
    A(i64)
    B(String)
fn main() -> i32:
    let e = E.B("x")
    match e:
        E.A(n) -> print(n)
        E.B(s) -> print(s)
    return 0
''',
r'''fn main() -> i32:
    var xs = [1, 2, 3]
    for x in &xs:
        print(*x)
    return 0
''',
r'''enum Inner:
    A(String)
    B(i64)
enum Outer:
    S(Inner)
    N
fn main() -> i32:
    let x = Outer.S(Inner.A("x".clone()))
    let y = match x:
        Outer.S(Inner.A(s)) -> s.len()
        Outer.S(Inner.B(n)) -> n
        Outer.N -> 0
    print(y)
    return 0
''',
r'''struct Bag:
    r: Rc<i64>
    b: Box<i64>
fn main() -> i32:
    let shared = rc(1)
    let x = Bag { r: shared.clone(), b: box(2) }
    print(shared.strong_count())
    drop(x)
    print(shared.strong_count())
    return 0
''',
r'''fn main() -> i32:
    let r = rc(3)
    let e: Result<i64, Rc<i64>> = Err(r.clone())
    print(e.unwrap_or(0))
    print(r.strong_count())
    return 0
''',
]

ALPHABET = "abcxyz0123:(){}[],.&*+-=<>!?@_ \n\t\"'#"


def mutate(text: str, rng: random.Random) -> str:
    s = text
    for _ in range(rng.randint(1, 5)):
        op = rng.randrange(5)
        if op == 0 and s:
            i = rng.randrange(len(s)); s = s[:i] + s[i + 1:]
        elif op == 1:
            i = rng.randrange(len(s) + 1); s = s[:i] + rng.choice(ALPHABET) + s[i:]
        elif op == 2:
            i = rng.randrange(len(s) + 1); s = s[:i] + rng.choice(SEEDS) + s[i:]
        elif op == 3 and s:
            i = rng.randrange(len(s)); s = s[:i] + rng.choice(ALPHABET) + s[i + 1:]
        else:
            lines = s.splitlines(True)
            if lines:
                k = rng.randrange(len(lines)); lines.insert(k, lines[k])
                s = "".join(lines)
    return s


def main() -> int:
    rng = random.Random(0xA7A7)
    failures = 0
    tmp = tempfile.mkdtemp(prefix="atrat-fuzz-")
    cases = int(os.environ.get("ATRAT_FUZZ_CASES", "500"))
    for i in range(cases):
        src = mutate(rng.choice(SEEDS), rng)
        path = os.path.join(tmp, f"case_{i}.atr")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        for command in ("check", "emit-c"):
            out = os.path.join(tmp, f"case_{i}.c")
            args = [sys.executable, ATRC, command, path]
            if command == "emit-c":
                args += ["-o", out]
            try:
                p = subprocess.run(args, cwd=ROOT, capture_output=True,
                                   text=True, timeout=30)
            except subprocess.TimeoutExpired:
                print(f"FAIL timeout: case_{i} {command}")
                failures += 1
                continue
            text = (p.stdout or "") + (p.stderr or "")
            if "Traceback (most recent call last)" in text:
                print(f"FAIL traceback: case_{i} {command}")
                print(text[:2000])
                failures += 1
            elif p.returncode not in (0, 1):
                print(f"FAIL exit {p.returncode}: case_{i} {command}")
                print(text[:1000])
                failures += 1
    print(f"fuzz cases: {cases * 2}, failures: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
