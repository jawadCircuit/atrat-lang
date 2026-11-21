# Contributing to Atrat

Atrat is an experimental compiler. Small, focused pull requests are welcome.

## Setup

```bash
python --version          # 3.10+
gcc --version             # or clang
python tests/run_tests.py
```

There are no Python dependencies.

## Before opening a PR

1. `python tests/run_tests.py` passes (24+ cases).
2. New language behaviour has a positive test, and new rejections have a
   compile-fail test with the expected diagnostic.
3. `python atrc emit-c examples/demo.atr -o /tmp/demo.c` compiles with
   `gcc -Wall -Wextra` without warnings.
4. Docs are updated (`docs/LANGUAGE.md`, `docs/DESIGN.md`, `CHANGELOG.md`).

## Code map

| File | Responsibility |
|---|---|
| `atrat/lexer.py` | indentation, tokens, literals, interpolation |
| `atrat/parser.py` | AST construction |
| `atrat/check.py` | declarations, HM inference, exhaustiveness, borrow checker |
| `atrat/codegen.py` | monomorphising C99 backend |
| `atrat/runtime.py` | embedded C runtime |
| `atrat/driver.py` | CLI, diagnostics, native compilation |

## Stability

The language is alpha: breaking changes are expected. The compiler itself aims
to always reject programs it cannot compile safely rather than emit unsound C.
A PR that turns a rejection into unsound acceptance will not be merged.

## Reporting bugs

Include the program, `atrc show` output if relevant, the expected result and the
actual result. For memory-safety bugs, say whether the generated C or the
compiled binary misbehaves, and include the `a`/`b`/`c` inputs.
