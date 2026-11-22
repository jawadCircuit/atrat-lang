# Regression reproductions

These are the smallest programs that exposed real Atrat bugs. They stay in the
repository so a future change cannot quietly reintroduce the same failure. The
full suite is in `tests/run_tests.py`; these snippets are the original shapes.

## 1. Borrowing through `Box` did not protect the box (fixed 0.8.1)

```atrat
struct P:
    x: i64

fn main() -> i32:
    let p = box(P { x: 1 })
    let r = &(*p).x
    drop(p)
    print(*r)
    return 0
```

Before the fix this compiled and left `r` pointing into freed memory. It now
fails with `cannot move `p` while it is borrowed` / `E0302`.

Test: `box-ref-owner-drop` in `tests/run_tests.py`.

## 2. Consuming enum methods leaked non-`Copy` payloads (fixed 0.8.2)

```atrat
fn main() -> i32:
    let r = rc(7)
    let e: Result<i64, Rc<i64>> = Err(r.clone())
    print(r.strong_count())      # 2
    print(e.unwrap_or(0))        # 0
    print(r.strong_count())      # 1; was 2 before 0.8.2
    return 0
```

`unwrap_or` marked the `Result` moved but never dropped the unused `Err`
payload. The ASan/UBSan CI job found it. `ok()` and `err()` had the same bug.

Test: `enum-method-payload-drop`, `unwrap-err` and `destruction-matrix`.

## 3. Nested match patterns emitted invalid C (fixed 0.8.3)

```atrat
enum Inner:
    A(i64)
    B(i64)

enum Outer:
    S(Inner)
    N

fn main() -> i32:
    let x = Outer.S(Inner.A(3))
    let y = match x:
        Outer.S(Inner.A(n)) -> n
        Outer.S(Inner.B(n)) -> -n
        Outer.N -> 0
    print(y)                     # 3
    return 0
```

The generated condition referenced a pattern temporary (`v_pat_N`) before its
declaration, so C compilation failed. Nested patterns now read the parent union
path directly.

Test: `nested-match-codegen`.

## 4. Address-taking through a dereference produced invalid C (fixed 0.8.3)

```atrat
fn main() -> i32:
    let b: Box<String> = box("owned".clone())
    print((*b).len())            # 5
    print(len(*b))               # 5
    return 0
```

The address helper returned the dereferenced value instead of its address, so
the generated C used `value->len` on a struct value. It now emits
`(&(*b.ptr))->len`.

Test: `box-string-methods`.

## 5. `Rc` was never dropped (fixed 0.8.0)

```atrat
fn main() -> i32:
    let r = rc(7)
    let a = r.clone()
    print(r.strong_count())      # 2
    drop(a)
    print(r.strong_count())      # 1
    return 0
```

The old `Rc` destructor did nothing. It now decrements the strong count, frees
the payload at zero, and releases the count allocation.

Test: `rc-clone-count`, `rc-drop-count`.

## 6. Temporary Box/Rc receivers leaked (fixed 0.8.6)

```atrat
fn main() -> i32:
    let r = rc(1)
    print(box(Some(r.clone())).is_some())
    print(r.strong_count())      # stayed 2: the temporary Box never dropped
    return 0
```

A temporary `box(...)`/`rc(...)` had no local drop flag. Field, method and
dereference access on such a temporary is now rejected; bind it first.

Tests: `temporary-box-method`, `temporary-box-field`, `temporary-box-deref`.

## 7. Owned temporaries in `drop` and `len` emitted invalid C (fixed 0.8.6)

```atrat
fn main() -> i32:
    drop(rc(1))
    drop(box(2))
    drop("x".clone())
    print(len("x".clone()))
    print(len(to_str(12345)))
    return 0
```

`drop` and `len` took the address of a statement-expression value, so GCC
reported `lvalue required as unary '&' operand`. Owned temporaries are now
materialised once and dropped exactly once.

Tests: `drop-temporary`, `len-owned-temporary`.

## 8. Temporary struct field reads were evaluated twice (fixed 0.8.6)

```atrat
struct P:
    s: String

fn main() -> i32:
    print(P { s: "x".clone() }.s)
    print(P { s: "y".clone() }.s.len())
    return 0
```

The field read emitted the aggregate once for the value and again for its
address, leaking/double-evaluating the temporary. Field access now materialises
the aggregate once and drops it at statement end.

Test: `temporary-aggregate-field`.


## 9. Panics leaked every live owner (fixed 0.8.7)

```atrat
struct Holder:
    name: String
    boxed: Box<String>

fn main() -> i32:
    let a = "alpha".clone()
    let b = box("beta".clone())
    let h = Holder { name: "holder".clone(), boxed: box("payload".clone()) }
    let l = ["one".clone(), "two".clone()]
    panic("cleanup boom")
    return 0
```

The old runtime called `abort()` immediately, so none of those owning locals was
destroyed. Atrat now registers live owning locals and parameters, moves and
normal scope exits unregister them, and a panic destroys the remaining entries
in reverse order before exiting with status 101.

Test: `panic-cleanup`.

## 10. A moved match binding stayed registered during a callee panic (fixed 0.8.7)

```atrat
fn consume(s: String) -> i32:
    panic("match boom")
    return 0

fn main() -> i32:
    let x = Some("payload".clone())
    let n = match x:
        Some(v) -> consume(v)
        None -> 0
    return n
```

The match arm moved `v` into `consume` but the arm's deferred code was emitted
after the call, leaving `v` registered while the callee's parameter was also
registered. A panic in the callee would then destroy the same `String` twice.
Moves now unregister before the call, and late arm declarations are emitted
before the arm expression.

Test: `panic-match-move`.

## 11. `pub` was parsed but ignored across files (fixed 0.8.8)

```atrat
# helper.atr
fn hidden() -> i64:
    return 9

# main.atr
import "helper"

fn main() -> i32:
    print(hidden())   # used to compile from another file
    return 0
```

The loader merged every imported declaration into one flat AST and the checker
had no module identity, so `pub` had no effect. Every AST node now carries its
source file; a cross-file use of a non-`pub` function/type/trait is rejected
with `E0402`, and public interfaces cannot expose private types.

Tests: `private-function`, `private-struct`, `exposed-private-type`.

## Running them

```bash
python tests/run_tests.py
python tests/fuzz_smoke.py
python tests/validation_matrix.py
python tests/sanitizers.py       # SKIP locally when the C toolchain lacks ASan
```

The Linux CI job runs the sanitizer pass with GCC. Local MinGW does not ship
`libasan`/`libubsan`, so that pass is skipped there and is not claimed locally.
