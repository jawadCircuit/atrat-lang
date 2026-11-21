# Security policy

Atrat compiles to C and executes generated native code. It is an alpha compiler
and has not received a security audit.

## Supported versions

Only the latest tagged release (currently 0.2.x) receives fixes.

## What counts as a vulnerability

- A safe program (no `unsafe:` block) that produces memory unsafety, a
  double-free, use-after-free, or out-of-bounds access.
- A `safe` API in the prelude that can be used to violate memory safety.
- Compiler crashes on untrusted input (including non-termination or arbitrary
  code execution while *compiling*).
- A miscompilation that changes the meaning of a checked program.

## What does not count

- Anything inside `unsafe:` (documented as the escape hatch), including null
  dereferences, out-of-bounds raw pointer access and inline assembly mistakes.
- Resource exhaustion from untrusted source sizes.
- Bugs in `gcc`/`clang`.

## Reporting

Open a private security advisory on the repository, or email the maintainers.
Please include a minimal `.atr` reproducer and, if possible, the generated C.
