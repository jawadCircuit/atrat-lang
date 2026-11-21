# Production-readiness audit

**Verdict: not production-ready.** Atrat is a working alpha compiler that is
suitable for experiments, teaching, prototypes and demos. It is not suitable for
production systems, untrusted input, or code you cannot afford to rewrite.

This document exists so the gap is explicit rather than marketing. "Make it
production ready" is not a one-commit change for a programming language; it is a
multi-year program. Releases 0.2–0.3 closed the most dangerous soundness holes,
added traits/modules/`?`/`Box`/closures, and added professional scaffolding. The
remaining blockers are listed with severity and an honest estimate.

## Scorecard

| Area | Status | Notes |
| --- | --- | --- |
| Safe-code memory safety | **Partial** | Ownership, borrows, returned-borrow tracking, exact drops, init analysis, bounds checks; no formal proof, no fuzzing campaign. |
| Defined arithmetic | **Yes** | Division/modulo/shift trap; `-fwrapv` gives defined wrapping. |
| Language stability | **No** | No 1.0 spec, no semver promise; breaking changes expected. |
| Type system completeness | **Partial** | HM + ADTs + generics + generic impls + traits + defaults + supertraits + returned views + `&dyn`/`Box<dyn>`; no explicit lifetime parameters or associated types. |
| Standard library | **Minimal** | Prelude, `Option`/`Result` methods, files, env, argv, time, `Box`. No maps, IO streams, JSON, threading, networking. |
| Modules / packages | **Partial** | Multi-file imports with cycle detection; flat namespace, no visibility, no package manager. |
| Tooling | **Minimal** | CLI, per-file diagnostics, `emit-c`. No formatter, LSP, debugger, REPL, docs generator. |
| Test suite | **Moderate** | 60 end-to-end cases incl. negative tests; no fuzzing, no differential testing at scale. |
| CI | **Yes** | Linux + Windows, Python 3.10/3.12, warning-free C job. |
| Performance | **Good** | 1.00x–1.21x of hand-written C on three micro-benchmarks. |
| Packaging | **Yes** | `pip install .`, `atrc` console script, wheel-ready. |
| Security process | **Minimal** | `SECURITY.md`; no audit, no threat model beyond safe/unsafe boundary. |
| Documentation | **Good for alpha** | Reference, design notes, readiness audit, examples. |
| Community / governance | **No** | No maintainer policy or deprecation process; prereleases only. |

## What 0.7.0 added

- **Returnable borrowed views**: structs containing references can be returned
  when every reference derives from the single reference parameter; callers
  keep the source borrowed for the view's lifetime.
- **Supertraits**: `trait Child: Parent` with inherited methods, static
  dispatch, `&dyn Child` and `Box<dyn Child>` support.
- **Owned trait objects**: `Box<dyn Trait>` with generated vtables and a
  destructor; creation is supported directly from `box(value)`.
- **Empty/default-only impls** via `pass`, plus `pass` as a no-op statement.
- **Fixed** a crash when a trait had only default methods and an impl provided
  no functions (`KeyError` during collection).
- **Fixed** inconsistent C struct naming for `List<Box<dyn Trait>>`.
- Trait element coercion in `List.push` / `List.set`.
- 60/60 end-to-end tests.

## What 0.6.0 added

- Non-Copy closure captures: moved into the closure environment, read-only
  inside the body, and dropped with the closure.
- Borrow checking of closure bodies, including rejection of moving fields out
  of captured values.

## What 0.5.0 added

- References in structs with conservative block-scoped lifetime tracking.
- Capturing closures (Copy values and shared references, by value).
- `&dyn Trait` / `&mut dyn Trait` with generated vtables.
- Generic impl blocks, including generic trait impls.
- Recursive/generic recursive type emission fixed.
- Fixed a double free in `Option`/`Result` consuming methods on temporaries.
- Fixed trait default methods sharing one AST across impls.
- Fixed `&String`/`&str`/`&List` len, clone, concat and indexing codegen.

## What 0.4.0 added

- Non-escaping returned borrows (`fn get(&self) -> &T: &self.field`) with
  caller-side borrow tracking.
- `Rc<T>` read-only shared ownership.
- Trait default methods and stricter impl signature checks.
- Three soundness fixes: escape-through-local borrows, moving non-`Copy` fields
  through references, and reading through a live `&mut` borrow.

## What 0.3.0 added

- `?` propagation with early-return cleanup.
- `Option`/`Result` methods.
- Traits, trait bounds and static dispatch, including impls on primitives.
- Multi-file imports with source-mapped diagnostics.
- `Box<T>` with recursive drop.
- Non-capturing closures and function values.
- Unit `()` so `Result<(), E>` is usable.

## What 0.2.0 hardened

1. **Definite initialisation across control flow.** `if`/`elif`/`else`, `while`,
   `for` and `match` now merge "may be uninitialised" states. This closed a real
   unsoundness where a value assigned in only one branch could be read.
2. **Branch-local move merging.** A value moved on one path is treated as moved
   after the join, so it cannot be read on another path.
3. **No C UB from integer division/shift.** Division and modulo by zero panic;
   `INT_MIN / -1` panics; shifts wider than the type panic. Signed overflow is
   defined as wrapping via `-fwrapv`.
4. **Rvalue receivers.** Methods and indexing on temporaries now materialise a
   temporary instead of emitting `&({ ... })` (invalid C).
5. **Host services.** `read_file`, `write_file`, `env`, `args`, `now_ms` with
   `Result`/`Option` error handling.
6. **Professional scaffolding.** Versioning, packaging, CI, license, security
   policy, changelog, benchmark harness, this audit.

## Remaining blockers, by severity

### Critical (must fix before any production claim)

| # | Blocker | Why it blocks production | Rough effort |
| --- | --- | --- | --- |
| C1 | Traits are missing associated items | Defaults, bounds, supertraits, generic impls, `&dyn` and `Box<dyn>` work; no associated types or trait-level generics. | 2–4 weeks |
| C2 | Lifetimes are conservative | Returned views are tied to one reference parameter; no explicit lifetime parameters, and views cannot be rebound or escaped through nested closures. | 4–8 weeks |
| C3 | Module system is a flat include | No visibility, namespacing, separate compilation or package boundaries. | 3–5 weeks |
| C4 | No fuzzing / formal soundness evidence | The checker is hand-written; 60 tests cannot establish absence of unsoundness. | 4+ weeks, ongoing |
| C5 | Single maintainer, no release process | "Production" implies support, deprecation, security response. | ongoing |

### High

| # | Blocker | Impact |
| --- | --- | --- |
| H1 | Standard library is tiny | No maps, sets, iterators, IO streams, formatting control, paths, JSON, threads, sockets. |
| H2 | No debugger/source maps | Debugging shows generated C, not `.atr`. |
| H3 | No LSP/formatter | Unusable in an editor at team scale. |
| H4 | No package manager | Cannot depend on anything. |
| H5 | C backend + GNU extensions | MSVC unsupported; cross-compilation and ABI control are ad hoc. |
| H6 | Error recovery is limited | One error per run; no incremental checking. |
| H7 | Python compiler performance | Fine for small programs; slow for large codebases. |
| H8 | No capturing closures | Callbacks cannot hold state; iterator adapters are impossible. |
| H9 | Shared ownership is read-only | `Rc<T>` exists, but no `Arc`, `Mutex` or interior mutability. |

### Medium

- No pattern guards, or-patterns, range patterns, destructuring `let`.
- Closures cannot capture their environment yet.
- No slices; `List<T>` is the only sequence.
- No overflow-checked arithmetic mode (only wrapping + traps for div/shift).
- No sanitizer integration (`-fsanitize=address` can be added manually).
- No deterministic cross-platform behaviour for `char`/float formatting edge cases.
- No benchmarks for real workloads (only micro-benchmarks).
- `unsafe:` is a block, not an auditable function boundary.

## Definition of "production ready" (gates)

Atrat can call itself production-ready when all of these are true:

1. **Language spec 1.0** with a versioned grammar and semantics document.
2. **Semver policy** and a deprecation process.
3. **Soundness:** no known unsoundness in safe code; a fuzzer that generates
   well-typed programs and runs them under ASan/UBSan; a written safety argument
   for the borrow checker.
4. **Modules + packages:** multi-file builds, visibility, dependency resolution,
   lockfiles.
5. **Traits/type classes and lifetimes** (or an equivalent escape analysis)
   sufficient to write iterator/parser/serialisation libraries.
6. **Standard library:** collections, strings/formatting, filesystem, IO,
   processes, time, threads, networking, JSON/serialisation.
7. **Tooling:** formatter, LSP, debugger integration with source maps,
   `atrc test`, reproducible builds.
8. **Platforms:** Linux, macOS, Windows (MSVC and MinGW), documented ABIs.
9. **CI:** fuzzing, sanitizers, cross-platform matrix, benchmark regression gates.
10. **Governance:** security policy with response times, release notes,
    maintainer rotation, contribution guide, code of conduct.
11. **Adoption evidence:** at least a few non-trivial programs (a server, a CLI,
    a parser) maintained outside the compiler repo.

None of gates 3–6 and 8–11 are met today. Gates 1, 2, 7 and 9 are partially met.

## Recommended path

| Milestone | Scope | Outcome |
| --- | --- | --- |
| 0.3 | Modules + package layout, `atrc fmt` (basic), error recovery | Usable for multi-file toys |
| 0.4 | Traits + dictionary passing, `Iterator`, stdlib collections | Real generic libraries |
| 0.5 | Lifetimes/escape analysis, slices, closures | Real APIs |
| 0.6 | LSP, debug source maps, sanitizer CI, fuzzing | Team-usable |
| 1.0 | Spec freeze, semver, 3-platform support, security audit | Production candidate |

Until then: label releases **alpha**, pin versions, and do not run untrusted
Atrat programs or trust `unsafe:` code with anything important.
