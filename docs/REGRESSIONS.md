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


## Running them

```bash
python tests/run_tests.py
python tests/fuzz_smoke.py
python tests/validation_matrix.py
python tests/sanitizers.py       # SKIP locally when the C toolchain lacks ASan
```

The Linux CI job runs the sanitizer pass with GCC. Local MinGW does not ship
`libasan`/`libubsan`, so that pass is skipped there and is not claimed locally.
