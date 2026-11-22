# Atrat

**A working alpha inspired by Rust, Python, Haskell, C, Lua and assembly — not production-ready.**

> [!WARNING]
> **Atrat is not fully functional yet.** It is a real compiler that builds and
> runs real programs, but it is an early alpha. Several language features,
> standard-library APIs and production guarantees are missing. Expect breaking
> changes, compiler bugs and no backwards-compatibility promise. Do not use it
> for production systems, untrusted input or anything you cannot afford to
> rewrite. See [Honest boundaries](#honest-boundaries) and
> [docs/READINESS.md](docs/READINESS.md).

<p align="center">
  <a href="https://github.com/jawadCircuit/atrat-lang/releases/latest">
    <img alt="Latest Atrat release" src="https://img.shields.io/github/v/release/jawadCircuit/atrat-lang?include_prereleases&style=for-the-badge&label=release&color=2563eb">
  </a>
  <a href="https://github.com/jawadCircuit/atrat-lang/releases">
    <img alt="Alpha status" src="https://img.shields.io/badge/status-alpha-f59e0b?style=for-the-badge">
  </a>
</p>

[![CI](https://github.com/jawadCircuit/atrat-lang/actions/workflows/ci.yml/badge.svg)](https://github.com/jawadCircuit/atrat-lang/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**Atrat** (`.atr`) is a compiled systems language with ownership and
borrow checking, Hindley–Milner-style inference, algebraic data types, traits,
deterministic memory management, a tiny C runtime, inline assembly and a
C99 backend. The compiler is written in Python; programs compile to native
executables through `gcc`/`clang`.

Atrat is **not** a reimplementation of Rust, Python, Haskell, C, Lua or
assembly, and it does not claim to match all of them. It is a separate language
that borrows specific ideas from each:

| Inspiration | What Atrat takes from it |
| --- | --- |
| **Rust** | Ownership, moves, borrow checking, RAII drops, an explicit `unsafe:` boundary. |
| **Python** | Significant indentation, local type inference, string interpolation, readable collection syntax. |
| **Haskell** | Hindley–Milner-style inference, algebraic data types (`enum`), exhaustive pattern matching, `Option`/`Result`. |
| **C** | Compilation through C99 to native code, unboxed structs, a small libc-based runtime, `extern` C ABI declarations. |
| **Lua** | A deliberately small runtime with no VM, no GC and very little ceremony for small programs. |
| **Assembly** | Raw pointers, pointer arithmetic, casts and inline `asm("...")` under `unsafe:`. |

Atrat has its own syntax, semantics and tradeoffs. Some ideas inspired by these
languages are only partially implemented; the [honest boundaries](#honest-boundaries)
below are part of the project, not fine print.

Additional built-in conveniences include `read_file`, `write_file`, `env`,
`args`, `now_ms`, `expr?`, `Option`/`Result` methods, capturing closures,
`Box<T>`, `Rc<T>`, references in structs, `&dyn Trait`, generic impls,
multi-file imports, and panic cleanup for live owners (followed by exit 101).

This repository contains a working compiler, a borrow checker, a type checker, a
C backend, a standard prelude, examples and an end-to-end test suite. It is an
**alpha**: expect breaking changes, missing library APIs and compiler bugs. It
has not had a security audit and is not suitable for production workloads yet.

## Quick start

```bash
git clone https://github.com/jawadCircuit/atrat-lang.git
cd atrat-lang

./atrc run examples/shapes.atr     # compile + run   (python atrc ... on Windows)
./atrc run examples/lifecycle.atr  # Box/Rc/nested-enum destruction counts
./atrc build app.atr -o app        # native executable
./atrc check app.atr               # types + borrows only
./atrc emit-c app.atr              # inspect the generated C
```

Requires Python 3.10+ and `gcc` or `clang` (C99 + GCC statement expressions).
No Python packages are needed. Optionally install the `atrc` command:

```bash
python -m pip install .
atrc run examples/demo.atr
```

The compiler is intentionally small and hackable: `atrc` is a Python entry
point, and the generated C is available with `atrc emit-c`.

## Project status

| Area | Status |
| --- | --- |
| Lexer, parser, AST | **Working** |
| Type inference and checking | **Working**, with known omissions |
| Ownership / borrow checker | **Working**, conservative by design |
| Native C backend | **Working**, GCC/Clang |
| Recursive types, generics, traits, supertraits, `dyn` and `Box<dyn>` | **Working** for the documented subset |
| Panic semantics | **Cleanup, not unwinding**: live owners are destroyed, then exit 101; no catch/recover |
| Standard library | **Minimal** |
| Modules / packaging / tooling | **Partial**: cross-file `pub` visibility enforced; no namespacing or separate compilation |
| Production readiness | **No** |

There are **119 end-to-end tests** covering successful programs, exact stdout,
negative diagnostics and multi-file builds. The suite is useful, but it is not
a proof of correctness, a security audit or a fuzzing campaign.

## A taste

```atrat
struct Vec2:
    x: f64
    y: f64

enum Shape:
    Circle(f64)
    Rect(f64, f64)
    Point

impl Shape:
    fn area(&self) -> f64:
        match self:
            Circle(r) -> 3.141592653589793 * r * r
            Rect(w, h) -> w * h
            Point -> 0.0

fn main() -> i32:
    let name = "atrat"
    var total = 0.0
    let shapes = [Shape.Circle(1.0), Shape.Rect(2.0, 3.0), Shape.Point]
    for s in &shapes:
        total += s.area()
    print("{name}: total area = {total}")
    return 0
```

That program is type-inferred, exhaustively matched, borrow-checked, monomorphised
and compiled to native code.

## What is enforced today

- **Ownership.** Assigning or passing a non-`Copy` value moves it; using it
  afterwards is a compile error.
- **Borrow rules.** `&T` and `&mut T` follow the usual aliasing rules: many
  shared borrows *or* one mutable borrow, never both. Moving or assigning while
  borrowed is rejected.
- **RAII.** `String` and `List<T>` free their memory at scope exit; moved
  values are not double-freed; match payloads are moved or dropped exactly once.
- **Initialisation.** Reading a binding before initialisation is rejected.
- **Bounds.** `list[i]` checks the index and panics instead of corrupting memory.
- **Defined arithmetic.** Division/modulo by zero and out-of-range shifts trap;
  signed overflow is defined two's-complement wrapping (`-fwrapv`).
- **Definite initialisation.** A binding assigned in only one branch is not
  considered initialised after the join.
- **Exhaustiveness.** A `match` over an enum must cover every variant or use `_`.
- **Types.** HM unification with explicit generic parameters, numeric literal
  defaulting, and safe `str` → `String` materialisation.
- **Raw control is opt-in.** Pointer dereference, pointer casts and `asm` require
  an `unsafe:` block.

## Honest boundaries

Atrat is a real, working language implementation, but **not a finished
production compiler**. The important current gaps:

- Traits support static dispatch, generic impls, default methods, supertraits,
  `&dyn Trait` / `&mut dyn Trait` and owned `Box<dyn Trait>` with generated
  vtables and destructors. There are no associated types or trait-level
  generics beyond the implemented subset.
- Borrowed **views** work: a struct containing references can be returned when
  every reference derives from the function's single reference parameter, and
  callers keep the source borrowed for the view's lifetime. Lifetimes are still
  conservative and block-scoped: views cannot be rebound, stored beyond the
  block, or captured in ways that outlive their source. There are no explicit
  lifetime annotations yet.
- Modules are still a flat namespace: `import` merges declarations and `pub`
  is now enforced across files. A non-`pub` function/type cannot be used from
  another file, and a public declaration cannot expose a private type or trait.
  There is no module namespacing, per-field `pub`, separate compilation or
  package boundary yet.
- Closures capture Copy values by value and non-Copy values by move. Inside a
  closure, non-Copy captures are read-only shared references; they cannot be
  moved out or mutated. Nested capturing closures and returning closures are
  not supported yet.
- `Box<T>` and read-only `Rc<T>` exist; `Arc`, interior mutability and `Mutex` do not.
- Panics clean up live owning locals and function parameters, then terminate with
  status 101. They do not unwind, cannot be caught, and expression temporaries
  that never become a local may still leak. A panic inside a destructor exits
  without running the remaining cleanup. Normal returns and explicit `drop`
  remain the complete, exactly-once paths; panic cleanup is best-effort for
  registered locals and parameters.
- `match` has no guards, ranges or or-patterns yet.
- `unsafe:` is a block, not a function marker (`unsafe fn` is not implemented).
- The C backend relies on GCC/Clang statement expressions.
- No package registry, formatter, LSP or debugger source maps.
- No stable ABI, no 1.0 specification and no long-term compatibility promise.

See [docs/READINESS.md](docs/READINESS.md) for the full production-readiness
audit and the concrete gates that are still unmet.

These are design boundaries, not hidden bugs: the compiler rejects programs that
would need the missing features rather than accepting them unsafely.

## Layout

```
atrc                     CLI entry point
atrat/
  lexer.py               indentation-aware lexer, interpolation, literals
  parser.py              recursive-descent parser -> AST
  ast.py                 AST node definitions
  typesys.py             type representation, unification, substitutions
  check.py               declaration collection, HM inference, exhaustiveness,
                         borrow checker
  codegen.py             monomorphising C99 backend
  runtime.py             embedded ~360-line C runtime
  prelude.atr            Option<T> and Result<T, E>, written in Atrat
  driver.py              CLI, diagnostics, native compilation
examples/                runnable feature demos
tests/run_tests.py       end-to-end compile/run/compile-fail suite
docs/LANGUAGE.md         language reference
docs/STDLIB.md           prelude, built-ins, ownership and panic rules
docs/DIAGNOSTICS.md      error codes and diagnostic format
docs/REGRESSIONS.md      minimized reproductions of fixed bugs
docs/DESIGN.md           requirement-by-requirement design notes
```

## Tests

```bash
python tests/run_tests.py
```

The suite compiles and executes positive programs, compares exact stdout, and
asserts that unsafe/invalid programs are rejected with the right diagnostic.
Currently **119/119** pass, including trait objects, supertraits, owned
`Box<dyn Trait>`, returned views, generic impls, recursive drops, moved closure
captures, returned getters,
mutable-alias rejection, multi-file builds, branch moves and uninitialised
paths.

## Performance

`examples/fib.atr` computes `fib(30)` recursively and runs a 100,000,000
iteration loop. On the development machine (MinGW-w64 gcc 16, `-O2`):

| Benchmark | C | Atrat | Ratio |
| --- | --- | --- | --- |
| 100M-iteration `u64` loop | 11.2 ms | 11.2 ms | 1.00x |
| recursive `fib(32)` | 18.0 ms | 21.9 ms | 1.21x |
| 200k string build/format/free | 45.5 ms | 41.6 ms | 0.91x |

Run `python bench/bench.py`. These are micro-benchmarks only; the generated code
is plain C, and `atrc emit-c` shows exactly what the backend produced.

## Documentation

- [Language reference](docs/LANGUAGE.md)
- [Diagnostics and error codes](docs/DIAGNOSTICS.md)
- [Prelude and standard built-ins](docs/STDLIB.md)
- [Regression reproductions](docs/REGRESSIONS.md)
- [Ecosystem and package-manager plan](docs/ECOSYSTEM.md)
- [Design notes](docs/DESIGN.md)
- [Production-readiness audit](docs/READINESS.md)
- [Changelog](CHANGELOG.md)
- [Release checklist](RELEASING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## License

MIT. See [LICENSE](LICENSE).
