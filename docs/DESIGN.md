# Why Atrat looks like this

The brief was six requirements that usually pull in opposite directions:

1. Rust memory safety
2. Python ease and reliability
3. C extreme performance
4. Lua lightweight
5. Haskell type system
6. Assembly raw control

Most languages pick two and pay for them with the others. Atrat's bet is that
**the front end can be strict and the back end can be dumb**: do the hard work
in a type/ownership checker, then emit simple C that a C compiler already
optimises perfectly.

```
source (.atr)
   -> lexer (indentation, interpolation)
   -> parser (AST)
   -> HM inference + exhaustiveness + borrow checker
   -> monomorphising C99 emitter
   -> gcc/clang -O2
   -> native binary
```

## Requirement → mechanism

### Rust-like memory safety

Three separate mechanisms, all compile-time:

- **Ownership and moves.** Non-`Copy` values are moved, not copied. The checker
  tracks each binding as live/moved/uninitialised and rejects use-after-move and
  use-before-init.
- **Borrow checker.** A per-function pass tracks active borrows, their roots,
  mutability and lifetime (block-scoped for `let` bindings, statement-scoped for
  temporaries). It rejects aliasing mutable borrows, moves while borrowed and
  mutation through `let` bindings.
- **RAII + exact drops.** Every non-`Copy` type has a generated drop function
  (`String`, `List<T>`, structs, enums). Codegen emits `if (alive) drop(&value)`
  at scope exit and suppresses it after moves. This is why matching an
  `Option<String>` neither leaks nor double-frees.

Bounds checks on list indexing and exhaustive `match` are part of the same
safety story: the common memory-safety failure modes are rejected or trapped
rather than left as undefined behaviour.

`unsafe:` exists because requirement 6 needs it. It is a syntactic block, so the
unsafe region is visible during review, and the checker still enforces ownership
of safe values around it.

### Python ease

- Indentation blocks and no semicolons.
- Local inference: `let x = 41` and `let shapes = [Shape.Circle(1.0)]`.
- String interpolation is a core feature, not a library macro:
  `"area = {total}"`.
- `print` accepts any value and any number of values; `to_str` gives the same
  formatting as an owned `String`.
- Errors are phrased in terms of the source ("cannot assign to immutable binding
  'x'; declare it with `var`") with a caret and a hint.

The important trade-off: "Python easy" here means **easy to read and write**,
not dynamically typed. Inference removes annotations from the common path while
the checker remains static.

### C performance

- No GC, no tracing, no boxing, no virtual dispatch, no interpreter.
- Generics are monomorphised: `List<i64>` is a concrete `{int64_t*, len, cap}`.
- Structs and enums are values; `Option<T>` lives inline.
- The emitted C is the C a performance-minded programmer would write: direct
  loops, direct calls, `restrict`-friendly pointers, `-O2`.
- Integer/float operations map directly to C operators; `pow`/`fmod` only for
  the operations that need them.

Measured on the development machine (MinGW-w64 gcc 16.2, `-O2`, best of 3):

| Workload | C | Atrat | Ratio |
| --- | --- | --- | --- |
| 100M-iteration `u64` accumulation | 11.2 ms | 11.2 ms | 1.00x |
| recursive `fib(32)` | 18.0 ms | 21.9 ms | 1.21x |
| 200k string build/format/free | 45.5 ms | 41.6 ms | 0.91x |

`atrc emit-c` makes the claim auditable and `python bench/bench.py` reproduces
the numbers.

### Lua lightweight

The runtime is a single ~360-line C header embedded in the compiler. Its jobs:
allocation, panic plus a small cleanup registry, a `{ptr,len,cap}` string
builder, printing and formatting.
Everything else is generated per program. There is no scheduler, no GC, no
module loader, no metadata, no reflection tables.

A `hello world` binary is about 61 KB with the default toolchain, mostly CRT
startup. Startup is C startup: there is nothing to initialise.

### Haskell-style types

Implemented:

- Hindley–Milner-style unification with occurs check and substitutions.
- Parametric polymorphism with explicit generic parameters, instantiated at use
  sites and monomorphised at codegen.
- Algebraic data types (`enum` with positional payloads), recursive types via
  `List`.
- Pattern matching, including nested patterns, with **exhaustiveness checking**.
- `Option<T>` and `Result<T, E>` as ordinary enums in the prelude.
- `match` as an expression, so `let x = match ...` and `return match ...` work.

Not implemented (and rejected clearly): type classes/traits, higher-kinded
types, existential types, GADTs, pattern guards. Trait/`impl`-based ad-hoc
polymorphism is the highest-value next addition because `impl` already provides
the syntactic hook and static dispatch.

### Assembly raw control

- `*T` and `*mut T` with dereference, address-of, casts and arithmetic.
- `extern fn` declarations for C ABI calls.
- `asm("template", operands...)` emitting GCC extended inline assembly with
  `{n}` operand placeholders and a memory clobber.
- All of this is gated behind `unsafe:`, so it does not weaken the default
  guarantees.

## Key engineering decisions

### Transpile to C instead of emitting machine code

Writing a competitive register allocator, instruction selector and optimizer is
a multi-year project; C compilers already do that extremely well. Emitting C99
gives portable native performance and makes the backend inspectable. The cost is
dependence on a C toolchain and a few GNU extensions (statement expressions).

### GCC statement expressions

Ownership-building expressions (interpolation, list literals, `to_str`, `match`)
need temporaries. Instead of introducing hidden temporary bindings and complex
drop tracking, the backend emits `({ ... value; })` expressions. This keeps the
C readable and lets values compose naturally. The driver compiles with
`-std=gnu11`.

### Monomorphisation instead of boxing

Generic structs/enums/functions are specialised on demand from a worklist keyed
by concrete type arguments. This is what makes `List<i64>` a plain struct and
generic calls direct calls. It costs compile time and code size for many
instantiations, which is the same trade-off Rust and C++ make.

### Drop flags, not a tracing GC

Each non-`Copy` local gets a C `bool alive` flag only when moves can happen
conditionally. Drops are exact, deterministic and zero-cost in the common case
where the flag is statically known; the flag remains correct inside branches and
matches. This is a simplified form of Rust's drop elaboration.

### Borrow-checking scope

The checker is deliberately conservative: borrows are block- or
statement-scoped. References may be stored in aggregates, and a view can be
returned when every reference in it derives from the function's single
reference parameter. Views cannot be rebound, escape through a nested closure,
or outlive their source's block. This avoids whole-program lifetime inference
while still catching the aliasing bugs that matter inside a function. Explicit
lifetimes are the planned path beyond this boundary.

## Known trade-offs

| Decision | Benefit | Cost |
| --- | --- | --- |
| C backend | native speed, portability, inspectability | needs gcc/clang; GNU statement expressions |
| Monomorphisation | unboxed values, direct calls | code size grows with instantiations |
| Exact drop flags | deterministic, no GC pauses | a little bookkeeping code per scope |
| Conservative borrows | simple, predictable checker | references cannot escape or live in structs |
| `str` vs `String` | no hidden copies for literals/views | one extra concept and a coercion rule |
| Python front end | fast to build and extend, no build step | compiler speed is Python-speed |
| Single file + prelude | simple, predictable | no module system |

## Production readiness

"Production ready" is a much larger bar than "works". The audited gap list,
scorecard and the concrete gates Atrat must pass are in
[READINESS.md](READINESS.md). In short: 0.2 hardened soundness and scaffolding;
modules, traits, lifetimes, tooling and a real standard library are still
required.

## Roadmap, in priority order

1. **Traits, part 2**: default methods, associated types, `dyn Trait` and
   dictionary passing for dynamic dispatch. (Static traits + bounds landed in 0.3.)
2. **Lifetimes or escape analysis**, enabling `&T` fields and returned borrows.
   This is the largest remaining language hole.
3. **Capturing closures** with environment structs/closure conversion, then
   iterator adapters (`map`/`filter`/`collect`).
4. **Real modules**: visibility, namespacing and separate compilation.
5. **Pattern ergonomics**: guards, or-patterns, range patterns, destructuring `let`.
6. **Slices** (`&[T]`) and string slicing without copying.
7. **Shared ownership**: `Rc`/`Arc`, interior mutability, `Mutex`, threads.
8. **Standard library**: maps, sets, iterators, paths/dirs, JSON, IO streams.
9. **Tooling**: formatter, LSP, debug source maps, package manager.
10. **Fuzzing and differential testing** against equivalent C programs, plus a
    memory-safety test corpus.
