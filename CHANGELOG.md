# Changelog

## 0.8.2 — enum payload cleanup

- Fixed a memory leak in consuming `Option`/`Result` methods. `unwrap_or`,
  `ok` and `err` now drop the payload of the variant they do not return;
  `unwrap`/`expect` drop the failing payload before panicking.
- Added a `Rc` strong-count regression test for `Result<i64, Rc<i64>>`.
- ASan/UBSan CI found the leak. 92/92 end-to-end tests pass.

## 0.8.1 — borrow provenance, diagnostics and validation

### Borrow checker

- References into `Box<T>`/`Rc<T>` payloads now borrow the box/Rc binding;
  dropping or moving it while the reference is alive is rejected.
- Assignment through `*box` checks live borrows and requires a mutable binding.
- Reads through `Box`/`Rc` respect live mutable borrows.
- A `&mut` can use its own borrow without being blocked by itself, while other
  live borrows still conflict.
- Reference provenance is propagated through struct fields and `&mut` fields.
- New negative tests for box/Rc owner drop/move, dereference assignment while
  borrowed, field-reference mutation conflicts and all previous safety cases.

### Diagnostics

- Stable diagnostic categories and Rust-style headers:
  `error[E0301]: borrow error: ...`, source arrow, caret, and `help:`.
- Added `docs/DIAGNOSTICS.md` with the code table.
- Tests assert codes for returned-view, move-while-borrowed, mutable/shared
  conflict, bare-dyn and trait-conflict errors.

### Validation

- `tests/sanitizers.py` runs the suite under ASan/UBSan where supported.
- `tests/fuzz_smoke.py` performs deterministic mutation fuzzing of the front end
  and C emitter and rejects compiler tracebacks.
- `tests/validation_matrix.py` compares generated behavior across `-O0/-O2/-O3`
  and available GCC/Clang, plus an 80-instantiation generic program.
- CI jobs added for sanitizers, fuzzing and the optimization/compiler matrix.
- Memory lifecycle coverage: Box/Rc counts, early returns, nested scopes,
  conditional branches, owned struct fields and trait-object destruction.
- 91 end-to-end tests (45 run, 45 compile-fail, 1 multi-file).

## 0.8.0 — safety and trait-system hardening

### Safety fixes

- A shared `let` borrow now conflicts with a live mutable borrow:
  `let a = &mut p.x; let b = &p.x` is rejected.
- Moving or dropping a non-`Copy` owner while a returned view is alive is
  rejected (`cannot move ... while it is borrowed`).
- Added adversarial coverage for nested scopes, conditional branches, loops,
  owner destruction, multiple reference sources and returned-view validation.

### Trait-system fixes

- **`Rc<T>` now drops correctly**: strong count is decremented and the
  allocation is freed at zero. This also fixes owned trait-object destruction
  of values containing `Rc`.
- Default methods from different traits no longer share one generated C
  function; `&dyn`/`Box<dyn>` dispatch to the correct default, including
  parent/child overrides.
- Ambiguous methods inherited from multiple supertraits are rejected unless the
  child declares an override.
- Bare `dyn Trait` values (fields, parameters, returns) are rejected; use
  `&dyn Trait`, `&mut dyn Trait` or `Box<dyn Trait>`.

### Tests

- 81 end-to-end tests: 40 run cases, 40 compile-fail cases and one multi-file
  case. The negative suite covers borrow conflicts, move/use-after-borrow,
  owner destruction, branches, loops, nested scopes, returned views,
  trait conflicts, invalid trait-object coercion and trait-object ownership.

## 0.7.0 — borrowed views, supertraits, owned trait objects

### Language

- **Returnable borrowed views**: a struct containing references can be returned
  when every reference derives from the function's single reference parameter.
  The caller keeps the source borrowed for the view's lifetime; views built from
  locals or temporaries are rejected.
- **Supertraits**: `trait Child: Parent` inherits methods; implementations must
  provide the parent impl as well. Works through static bounds, `&dyn Child` and
  `Box<dyn Child>`.
- **`Box<dyn Trait>`**: owned trait objects with generated vtables and a
  destructor. Supported from `box(value)`, an existing `Box<T>` local, or a
  function returning `Box<T>` / `Box<dyn Trait>`.
- **`pass`**: a no-op statement and a way to write an impl that only uses
  default trait methods.
- Coercion of owned boxes into `Box<dyn Trait>` in list elements, struct fields,
  returns and function arguments.

### Bug fixes

- A trait with only default methods and an impl with no functions no longer
  crashes collection with `KeyError`.
- `List<Box<dyn Trait>>` no longer emits inconsistent C struct names.
- `List.push` / `List.set` now use normal assignability, so coercions apply.
- Temporary `Box<dyn Trait>` method receivers are dropped after the call.
- Cyclic supertraits, unknown supertraits and missing parent impls are rejected.

### Tests

- 60 end-to-end tests, including returned views, supertrait dispatch, default-only
  impls, `Box<dyn>` moves/lists/fields/returns, temporary receivers and new
  negative cases.

## 0.6.0 — non-Copy closure captures

- Closures now capture **non-Copy values by moving them into the closure
  environment**. Inside the closure body the value is accessed through a shared
  reference, so it can be read, formatted, cloned or passed to `&self` methods,
  but it cannot be moved out or mutated.
- The closure owns and drops its captured values. Capturing moves the original
  binding, so using it afterwards is a compile error.
- The borrow checker now checks closure bodies, including moves out of captured
  non-Copy values.

### Tests

- 50 end-to-end tests, including moved captures, repeated closure calls,
  captured `String`/`List` reads, closure-environment drops and rejection of
  using a moved capture.

## 0.5.0 — trait objects, generic impls, references in structs

### Language

- `&dyn Trait` / `&mut dyn Trait` with static vtables and dynamic dispatch.
- Generic impl blocks: `impl<T> Wrapper<T>` and `impl<T> Trait for Wrapper<T>`.
- References can be stored in structs (`View { r: &x }`), with conservative
  block-scoped lifetime tracking. Such values cannot be returned or rebound.
- Capturing closures: captures are snapshotted by value. Copy values and shared
  references are supported; non-Copy captures are rejected with a clear error.
- `&String`, `&str` and `&List` now support `len`, `clone`, concatenation,
  indexing and the free `len()` builtin.
- Mutable methods and field assignment work through `&mut` receivers and
  `&mut` fields held in immutable bindings.

### Bug fixes

- Fixed generated C ordering for recursive types such as
  `Node { next: Option<Box<Node>> }` and generic recursive types.
- Fixed a double free when consuming methods (`unwrap`, `expect`, `ok_or`) were
  called on a temporary `Option`/`Result` with a non-Copy payload.
- Fixed a shared-AST bug where a trait default method body used the last impl's
  method dispatch for every implementor.
- Rejected consuming a non-Copy payload through `&Option`/`&Result`.
- Fixed `&String`/`&str` method and free-function code generation.

### Tests

- 49 end-to-end tests, including dyn dispatch, generic impls, struct
  references, recursive drops, captured closures and negative move checks.

## 0.4.0 — borrows, Rc, trait defaults, soundness

### Language

- Non-escaping **returned borrows**: methods/functions may return `&T`/`&mut T`
  derived from their single reference parameter (`&self` included). Getter
  patterns now work, and the caller's borrow is tracked for the rest of the
  block. Returning a borrow of a local or temporary is rejected.
- `Rc<T>` shared ownership: `rc(x)`, `.clone()`, `.strong_count()`, read-only
  deref/field access, reference-counted drop. Mutation through `Rc` is rejected.
- Trait **default methods** and stricter impl signature checking (`Self`,
  receiver mode, parameter and return types).
- `String`/`str` `.clone()`.

### Soundness fixes

- Fixed a use-after-free: a borrow could escape through a local (`let r = &p.x;
  return r`). Such functions are now rejected up front.
- Fixed aliasing/double-drop when moving a non-`Copy` field out through `&self`
  or another reference.
- Rejected mutation and `&mut` access through `Rc`.
- Rejected reading a value directly while a `&mut` borrow of it is alive.

### Tests

- 39 end-to-end tests, including returned getters, borrow-while-getter and
  read-while-mut-borrow rejection, trait defaults, `Rc`, escape rejection and
  borrowed-field moves.

## 0.3.0 — language expressiveness

### Language

- `?` propagation for `Result` and `Option`, with automatic error-type checking
  and scope cleanup on the early-return path.
- `Option`/`Result` methods: `is_some`, `is_none`, `is_ok`, `is_err`, `unwrap`,
  `expect`, `unwrap_or`, `ok`, `err`, `ok_or`.
- Unit value `()` and `Result<(), E>` support.
- Traits: `trait` declarations, `impl Trait for Type`, static dispatch, and
  generic bounds (`<T: Show>`). Bounds are checked at each monomorphised call.
  Implementations are allowed on structs, enums and primitive types.
- Multi-file programs: `import "path"` / `import a.b`, recursive resolution,
  cycle detection, duplicate-import deduplication, and per-file diagnostics.
- `Box<T>`: heap allocation via `box(x)`, deref/field access, recursive types,
  and deterministic deep drop.
- Non-capturing closures: `|x: i64| x + 1`, first-class function values, and
  calls through function pointers. Capturing closures are rejected explicitly.

### Tooling

- Source map for merged modules: errors report the original file and line.
- 32 end-to-end tests (up from 24), including traits, bounds, closures, Box,
  `?`, enum methods and multi-file builds.
- Version bump and changelog.

### Still missing

- Lifetimes / escaping borrows, `Rc`/`Arc`, trait objects (`dyn`), capturing
  closures, package management, collections beyond `List`, and editor tooling.
  See `docs/READINESS.md`.

## 0.2.0 — hardening release

### Safety

- Definite-initialisation analysis now merges across `if`/`elif`/`else`,
  `while`, `for` and `match` arms. Reading a value that is uninitialised on any
  path is rejected.
- Moves are merged conservatively across branches and match arms, so a value
  moved on one path cannot be read on another.
- Integer division/modulo by zero now traps with a panic instead of undefined
  behaviour. `INT_MIN / -1` is checked.
- Shift counts are bounds-checked; out-of-range shifts panic instead of C UB.
- Programs are compiled with `-fwrapv`; signed arithmetic is defined as
  two's-complement wrapping.
- Method and list/string operations on rvalue receivers no longer emit invalid C.

### Standard library

- `read_file`, `write_file` (both `Result<String, String>` / `Result<(), String>`).
- `env` (`Option<String>`), `args` (`List<String>`, real argv), `now_ms` (`u64`).

### Tooling

- `atrc --version`, installable package (`pip install .`), console script.
- CI matrix (Linux + Windows, Python 3.10/3.12) and a warning-free generated-C job.
- 24 end-to-end tests, including control-flow initialisation and branch-move cases.
- Benchmark harness and readiness audit (`docs/READINESS.md`).

## 0.1.0 — first working compiler

- Lexer, parser, HM inference, ADTs, exhaustive matching, generics with
  monomorphisation, ownership and borrow checking, RAII drops.
- C99 backend, ~250-line runtime, inline `asm`, raw pointers, `extern fn`.
- 20 end-to-end tests.
