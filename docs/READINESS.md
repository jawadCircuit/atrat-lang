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
| Safe-code memory safety | **Partial** | Ownership, borrows, returned-borrow tracking, exact drops on normal control flow, init analysis, bounds checks; panic cleanup for live owners exists but is not unwinding; no formal proof or large-scale fuzzing campaign. |
| Defined arithmetic | **Yes** | Division/modulo/shift trap; `-fwrapv` gives defined wrapping. |
| Language stability | **No** | No 1.0 spec, no semver promise; breaking changes expected. |
| Type system completeness | **Partial** | HM + ADTs + generics + generic impls + traits + defaults + supertraits + returned views + `&dyn`/`Box<dyn>`; no explicit lifetime parameters or associated types. |
| Standard library | **Minimal** | Prelude, `Option`/`Result` methods, files, env, argv, time, `Box`. No maps, IO streams, JSON, threading, networking. |
| Modules / packages | **Partial** | Multi-file imports with cycle detection, enforced cross-file `pub`, module aliases/qualified names, stable project-relative module IDs, deterministic JSON interfaces and a dependency graph; no per-field `pub`, object cache, separate compilation or package manager. |
| Tooling | **Minimal** | CLI, per-file diagnostics, `emit-c`. No formatter, LSP, debugger, REPL, docs generator. |
| Test suite | **Moderate** | 180 language cases incl. 74 compile-fail, 10 panic-path and 12 multi-file tests, plus a module-interface/dependency-graph suite; 1,000-case mutation fuzz smoke, ASan/UBSan locally and in CI, optimization/compiler matrix; no large-scale differential campaign. |
| CI | **Yes** | Linux + Windows, Python 3.10/3.12, warning-free C job. |
| Performance | **Good** | 1.00x–1.21x of hand-written C on three micro-benchmarks. |
| Packaging | **Yes** | `pip install .`, `atrc` console script, wheel-ready. |
| Security process | **Minimal** | `SECURITY.md`; no audit, no threat model beyond safe/unsafe boundary. |
| Documentation | **Good for alpha** | Language reference, STDLIB, diagnostics, regression reproductions, design notes, readiness audit, examples. |
| Community / governance | **No** | No maintainer policy or deprecation process; prereleases only. |

## What 0.8.14 added

- Make `&&` and `||` lazy: right-hand calls, temporaries and drops run only
  when the left operand does not decide the result.
- Fix inclusive ranges at the element type's maximum (`u8` `0..=255`,
  `i8` `-128..=127`) so they no longer wrap and loop forever.
- Allow casts on bare numeric literals (`0 as u8`, `1.5 as i64`).
- Reject out-of-range integer and non-finite float literals at lex time.
- Reject infinitely sized recursive types, and duplicate struct fields, enum
  variants, generic parameters, trait parameters and impl generics, at
  type-check time.
- Attribute lexer diagnostics to the file being parsed rather than the prelude,
  accept UTF-8 BOM-prefixed sources, and reject non-UTF-8 files cleanly.
- **180/180 language tests** (84 run, 74 compile-fail, 10 panic-path, 12
  multi-file), plus interface, validation-matrix, fuzz and sanitizer suites.

## What 0.8.13 added

- Fix ownership at operator boundaries: concatenation/comparison borrow their
  operands, and `box`/`rc` consume their payload. Reuse-after-concatenation is
  accepted and use-after-`box` is rejected.
- Correct `Result.ok()`/`err()` payload tracking on borrowed receivers, closing
  a heap-corruption hole when a non-`Copy` payload was moved out of a reference.
- Add shared-borrow checks to concrete, trait, `dyn`, enum, string and list
  method receivers, shared `match` and shared `for` iteration.
- Reject borrows and assignments rooted in temporary values instead of emitting
  dangling references.
- Fix receiver adaptation for `&T`/`&mut T` method calls and `Rc` methods reached
  through a reference.
- Fix assignment evaluation order for fields and indexed elements, including
  self-assignment and compound assignment; materialise an index exactly once.
- Rework `if`/`elif`/`while` condition emission: one evaluation per test,
  condition temporaries cleaned before the branch, `elif` temps in scope, and
  `while` side effects re-evaluated every iteration.
- Evaluate `min`/`max`/integer `abs` operands once.
- Harden the lexer against malformed numeric separators, malformed exponents,
  surrogate `\u` escapes and a decimal zero at end-of-file.
- Add `starts_with`, `ends_with` and `contains` to `str`/`String`.
- **169/169 language tests** (80 run, 67 compile-fail, 10 panic-path, 12
  multi-file), plus interface, validation-matrix, fuzz and sanitizer suites.

## What 0.8.12 added

- Added stable **project-relative module IDs** derived from paths, not file-stem
  load order, so interface names do not depend on import order.
- Added deterministic JSON `ModuleInterface` output: public functions and
  signatures, struct layouts, enum variants, traits/methods/supertraits, impls,
  compiler version, source hash and dependency interface hashes. Imported types
  are fully qualified as `module::Type`.
- Added a dependency-first build graph and `atrc interfaces` / `atrc graph`.
- Public interface changes invalidate importing modules through dependency
  hashes; private body changes alter only the module's own source hash.
- Added `tests/interfaces_test.py` covering ordering, deterministic output,
  imported-type qualification, private-body invalidation and public-change
  propagation.
- **154 language tests + interface suite**; separate compilation, object caches,
  per-field `pub`, package boundaries and a package manager are still absent.

## What 0.8.11 added

- Added real module aliases and qualified names. `import "util" as u` supports
  `u.fn(...)`, `u.Type`, `u.Enum.Variant`, `impl u.Trait for u.Type`,
  generic bounds and `&dyn u.Trait` / `Box<dyn u.Trait>`.
- Each non-root file gets an internal declaration namespace. Duplicate public
  names in different modules no longer collide, and using the wrong module's
  name is a normal unknown-name error, not a silent collision.
- Imports without `as` retain the legacy unqualified behavior and also get a
  default alias from the file stem. Ambiguous unqualified names from multiple
  imports are rejected with a hint to use an alias.
- Cross-file `pub` remains enforced for qualified and unqualified access, and
  public surfaces still cannot expose private types or traits.
- Added multi-file coverage for qualified functions, structs, enums, traits,
  generic trait bounds, `dyn` trait objects, duplicate names and default aliases.
- **154/154 tests** (72 run, 60 compile-fail, 10 panic-path, 12 multi-file).
- Still no per-field `pub`, separate compilation, package manager or conditional
  imports.

## What 0.8.10 added

- Added **partial moves** for non-`Copy` fields. The checker tracks a path per
  local (`p.inner.name`), allows sibling fields to stay usable, rejects reads of
  moved fields, duplicate moves, whole-value moves after a partial move, and
  moves through `Rc`, shared/`&mut` references, raw pointers or indexed
  elements. Assignment to a moved field restores it.
- Codegen implements partial moves by copying the field out and zeroing the
  source place. Every generated destructor is null/zero safe, so the owner
  later destroys exactly the remaining fields without a second owner. This is
  equivalent to per-field runtime drop flags for the current type system.
- Partial moves work through nested fields and owned `Box` payloads, and from
  materialised temporary aggregates.
- Materialised expression temporaries now join the current drop scope; early
  `return`, `?`, `break`, `continue` and panic paths use the same exactly-once
  cleanup as normal statement end.
- Fixed `break`/`continue` cleanup in `for` loops: the loop-body drop scope is
  now included, so loop-local owners and temporary pair operands are dropped.
- `tests/sanitizers.py` now auto-detects a sanitizer-capable compiler, including
  LLVM MinGW clang. Locally, all 146 tests plus lifecycle examples pass under
  ASan+UBSan (Windows ASan has no LeakSanitizer); Linux CI still runs leak
  detection.
- **146/146 tests** (72 run, 60 compile-fail, 10 panic-path, 4 multi-file).
- Remaining panic limit: no unwinding/catch; values under construction and a
  panic raised by a destructor are still outside the cleanup guarantee.

## What 0.8.9 added

- Fixed a silent leak when matching a non-`Copy` value against a literal
  pattern: an owned `String` scrutinee is now dropped at the end of a literal
  arm instead of being left behind.
- Fixed `unwrap_or`/`ok_or` defaults. Defaults are evaluated eagerly into a
  registered temporary and dropped when the success variant wins; moving a
  local into the default no longer leaves it unregistered and leaked.
- Rejected moving a non-`Copy` field out of an aggregate. The old conservative
  path marked the whole owner moved without dropping its siblings. `let s =
  p.name` now fails with E0302; clone the field or move the whole value.
- Changed `drop(p.field)` to destroy the field in place instead of marking the
  whole owner moved, so the owner and its other fields remain usable.
- Rejected `drop(r.field)` through `Rc` or a shared reference, which would
  otherwise destroy a field visible through another owner.
- Materialise non-`Copy` aggregate operands of `==`/`!=` before comparing them,
  so a struct/enum literal's owned fields are dropped afterwards instead of
  leaking.
- Drop inspected non-`Copy` fields when a variant pattern combines a literal
  subpattern with a binding in another field, instead of leaving those
  fields behind.
- Track a non-`Copy` match scrutinee as a cleanup owner. A literal arm, an
  early `?` return, `break`/`continue` or a panic now drops it exactly once;
  moved source locals are marked dead before the arms so an early return cannot
  double-drop them.
- Added regressions for eager defaults, literal-pattern matching, field drops,
  rejected field moves and rejected destructive drops through shared owners.
- **132/132 tests** (64 run, 55 compile-fail, 9 panic-path, 4 multi-file).
- Historical limitation (superseded by 0.8.10): no per-field partial moves yet.

## What 0.8.8 added

- Enforced `pub` across files. Every AST node now carries its originating
  module, and the checker rejects cross-module use of non-public functions,
  structs, enums, traits, impl methods, supertraits and bounds.
- Rejected public declarations that expose a private type or trait through a
  parameter, return type, field, enum variant or generic bound.
- Kept same-file private helpers usable by public wrappers.
- Added multi-file positive coverage for public structs and private helpers, plus
  negative coverage for a private function, a private struct and an exposed
  private type.
- **119/119 tests** (56 run, 51 compile-fail, 8 panic-path, 4 multi-file).
- At 0.8.8, imports still merged into one flat translation unit and public names could collide; 0.8.11 added aliases, qualified names and per-module namespaces. Separate compilation and per-field `pub` remain absent.

## What 0.8.7 added

- Added a panic cleanup registry. Every live owning local and function
  parameter is registered, moves and normal scope exits unregister it, and a
  panic destroys the remaining owners in reverse order before terminating with
  status 101.
- Cleanup crosses nested calls: caller arguments are transferred before a call,
  callee parameters are registered on entry, and a panic inside a callee drops
  the callee's locals/parameters exactly once.
- Materialised statement temporaries are registered and unregistered with their
  normal statement-end drop, covering field access, `len` and enum receivers.
- Explicit `drop(local)` and consuming operations unregister before ownership
  leaves the binding, so panic cleanup cannot double-free a moved value.
- Normal `main` return disposes the registry array; panic cleanup already frees
  it.
- Added panic-cleanup regressions for String/Box/Rc/List/struct locals, nested
  calls, match bindings, loop locals, consuming `Result::unwrap` on `Err`, plus
  sanitizer-output checks in the panic test loop.
- **116/116 tests** (56 run, 51 compile-fail, 8 panic-path, 1 multi-file).
- Still not unwinding/catchable: expression temporaries that never become locals
  can leak, and a panic inside a destructor exits without remaining cleanup.

## What 0.8.6 added

- Rejected temporary `Box`/`Rc` receivers for field, method and dereference
  access; they previously leaked because no binding carried a drop flag.
- Fixed `drop` of owned temporaries (`drop(box(...))`, `drop(rc(...))`,
  `drop(make_string())`) emitting invalid C.
- Fixed `len` of owned string temporaries (`len(to_str(n))`,
  `len("x".clone())`) emitting invalid C.
- Fixed duplicate evaluation/drop for field reads from temporary structs.
- Mark explicit `drop(local)` as final so scope exit skips a second destructor.
- 111/111 tests.

## What 0.8.5 added

- `Box<T>` method calls auto-dereference (`boxed_string.len()`,
  `boxed_point.get()`, `boxed_list.push(x)`).
- Moving a non-`Copy` payload out through a `Box` method remains rejected.
- 105/105 tests at that release.

## What 0.8.4 added

- Fixed the sanitizer CI harness after it gained lifecycle examples: the
  `atrc` path constant was missing. No compiler behaviour changed.
- 103/103 tests still passed at that release.

## What 0.8.3 added

- Fixed nested enum `match` patterns emitting C that referenced a pattern
  temporary before its declaration.
- Fixed address-taking through a dereference (`(*boxed_string).len()` and
  `len(*boxed_string)`) producing invalid C.
- Added `Result::unwrap_err`, with the same drop-the-other-payload rule as the
  other consuming enum methods.
- Added destruction tests for nested `Option<Result<...>>` payloads, structs
  containing `Box` and `Rc`, early returns, loops, assignment, and exactly-once
  cleanup using `Rc::strong_count`.
- Added ownership-transfer tests for `Option<Box<T>>`, `Result<Box<T>, E>` and
  `Option<Rc<T>>` across argument passing and returns.
- Added diagnostic notes that point back to the first borrow location.
- Added three panic-path tests: `unwrap`, `assert` and division by zero abort
  before subsequent code. Panic still does not unwind, so local destructors do
  not run on a panic path.
- Added `docs/STDLIB.md`, `docs/REGRESSIONS.md` and `examples/lifecycle.atr`.
- Increased mutation fuzz smoke to 500 generated cases (1,000 check/emission
  invocations) and added lifecycle scenarios to the sanitizer and matrix jobs.
- 103/103 end-to-end tests at that release.

## What 0.8.2 added

- Fixed a leak in consuming enum methods: `unwrap_or`, `ok` and `err` now drop
  the non-returned non-`Copy` payload. `unwrap`/`expect` drop the failing
  payload before panicking.
- Added a strong-count regression test proving `Result<i64, Rc<i64>>` payloads
  are released by `unwrap_or` and `ok`.
- ASan/UBSan CI found this leak; the correction is included in this release.
- 92/92 end-to-end tests.

## What 0.8.1 added

- Borrow checking now tracks references into `Box<T>` and `Rc<T>` payloads:
  dropping or moving the box/Rc while such a reference is alive is rejected.
- Assignment through `*box` respects live borrows and requires a mutable
  binding; reads through `Box`/`Rc` respect mutable borrows.
- Reference provenance lets a `&mut` use its own borrow without being blocked
  by itself, while still rejecting other live borrows.
- Stable diagnostic codes (`E0301`, `E0302`, ...) and Rust-style `error[E...]`
  headers with source arrows and `help:` lines. See `docs/DIAGNOSTICS.md`.
- Added `tests/fuzz_smoke.py`, `tests/validation_matrix.py` and
  `tests/sanitizers.py`; CI now has ASan/UBSan, fuzz, optimization-level and
  GCC/Clang matrix jobs.
- Memory lifecycle tests for `Box`, `Rc`, early returns, nested scopes,
  conditional branches, owned struct fields and trait objects.
- 91/91 end-to-end tests, including 45 negative cases.

## What 0.8.0 added

- Safety hardening: live shared borrows now conflict with a mutable borrow in
  `let` bindings; owner destruction/moves while a returned view is alive is
  rejected; nested-scope, branch and loop borrow tests were added.
- **`Rc<T>` drop is implemented** (it previously never decremented the strong
  count or freed the allocation). Trait-object destruction now releases
  captured `Rc` values correctly.
- Default methods from different traits no longer share one generated C
  function; parent/child default overrides dispatch correctly through `&dyn`
  and `Box<dyn>`.
- Ambiguous inherited trait methods are rejected; bare `dyn Trait` values are
  rejected with a clear diagnostic.
- 81/81 end-to-end tests, including 40 negative cases.

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
| C3 | Module system is a single merged unit | Cross-file `pub`, aliases, qualified names, duplicate names, stable interfaces and a dependency graph work. Still no per-field visibility, object-file compilation, link cache, package boundaries or conditional imports. | 2–4 weeks |
| C4 | No formal soundness evidence | The checker is hand-written; 154 tests, mutation fuzzing and ASan/UBSan cannot establish absence of unsoundness. | 4+ weeks, ongoing |
| C5 | Single maintainer, no release process | "Production" implies support, deprecation, security response. | ongoing |
| C6 | Panic is not unwinding or catchable | Live owning locals, parameters and materialised temporaries are destroyed before exit 101, including across nested calls and early control flow. There is no catch/recover; values under construction and user destructor panics remain gaps. | 3–6 weeks for a full panic strategy, more for cross-platform/ABI guarantees |

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
| H8 | Nested/returning closures are unsupported | Capturing closures work, but nested and returning closures are rejected; iterator adapters are limited. |
| H9 | Shared ownership is read-only | `Rc<T>` exists, but no `Arc`, `Mutex` or interior mutability. |

### Medium

- No pattern guards, or-patterns, range patterns, destructuring `let`.
- Closures cannot be nested or returned yet; capturing closures themselves work.
- No slices; `List<T>` is the only sequence.
- No overflow-checked arithmetic mode (only wrapping + traps for div/shift).
- ASan/UBSan support exists via `ATRAT_CFLAGS`; `tests/sanitizers.py` auto-detects clang/gcc. The local LLVM MinGW clang and Linux CI both run it. Windows ASan lacks LeakSanitizer; Linux CI supplies leak detection.
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

## Realistic 1.0 estimate

This is an engineering estimate, not a promise. It assumes the current
single-maintainer project continues and that "production ready" means the gates
below, not merely "the demo compiles". The gates are listed above.

| Scope | Full-time effort | Calendar estimate from Nov 2026 |
| --- | --- | --- |
| Usable alpha for one team's small tools | 3–6 months | mid/late 2027 |
| Beta: lifetimes, modules, full panic semantics, core collections, formatter/LSP basics | 9–15 months | late 2027–early 2028 |
| Credible 1.0 candidate: stdlib, tooling, fuzzing, security review, 3 platforms, external users | 18–36 months | mid 2028–late 2029 |
| Comfortable production 1.0 with ecosystem and support | 30–48+ months | 2029–2030 |

At part-time effort (roughly 10–15 hours/week), multiply the calendar ranges by
2–3: a credible 1.0 is a 2029–2031 project, not a 2026–2027 one.

The largest uncertainty is not the parser or code generator. It is the work that
turns a compiler into a platform: lifetime/escape semantics, a real standard
library, module/package boundaries, full panic semantics, tooling, security response,
and enough external use to find the bugs the current 154 tests cannot.

## Recommended path

| Milestone | Scope | Outcome |
| --- | --- | --- |
| 0.8.x patch releases | Destruction, ownership-transfer, fuzz and diagnostics hardening; usability fixes only | Safer alpha |
| Pre-1.0 stabilization | Lifetimes/escape analysis, associated types, modules/visibility, full panic semantics, stdlib, formatter/LSP | Beta candidate |
| 1.0 | Spec freeze, semver, 3-platform support, security audit, external users | Production candidate |

Until then: label releases **alpha**, pin versions, and do not run untrusted
Atrat programs or trust `unsafe:` code with anything important.
