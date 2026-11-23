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

## 12. Destruction leaks at ownership edges (fixed 0.8.9)

Several destruction leaks shared one cause: an owning temporary lost its drop
owner, or a value was moved without transferring its cleanup ownership.

```atrat
let s = "hello".clone()
let n = match s:
    "hello" -> 1
    _ -> 2
```

A literal pattern does not consume the scrutinee, so the arm-end drop was
missing and `s` leaked. Matching now drops a non-`Copy` scrutinee after a
literal arm, including unit-result matches.

```atrat
let d = rc(9)
let x = o.unwrap_or(d)
```

The default was emitted only in the `None`/`Err` branch. The checker still
marked `d` moved, so when the success branch won the `Rc` was neither used nor
dropped. Defaults are now evaluated eagerly into a registered temporary and
dropped when unused.

```atrat
let s = p.name
```

Moving a non-`Copy` field marked the whole `p` moved, so `p`'s remaining owned
fields were never dropped. The checker now rejects the move with `E0302`;
clone the field or move the whole value. `drop(p.name)` destroys just that field
in place and leaves the owner usable.

A field drop also must not run through a shared owner: `drop(r.name)` where `r`
is an `Rc` or `&` alias would free a field another owner can still reach. Both
are rejected with `E0302`.

```atrat
let same = P { r: a } == P { r: b }
```

Struct equality compared the addresses of two compound literals. Nothing owned
those literals, so their `Rc`/`String` fields leaked. Non-`Copy` aggregate
operands are now materialised once and dropped after the comparison.

```atrat
match e:
    E.A(x, "y") -> len(x)
```

`x` is bound (and dropped through its binding), so the variant is not dropped
as a whole. The literal `"y"` subpattern only inspects its field, which
therefore had no drop owner. Inspected non-`Copy` fields now get their own drop
when a sibling field is bound.

An all-literal variant arm still drops the scrutinee as a whole, so the
per-field path is only used when another field is bound. The match scrutinee is
now itself a cleanup owner: a literal arm that panics, returns early with `?`,
breaks, or falls off the end without an arm drops it exactly once. If the
scrutinee is a local, its alive flag is cleared before the arms so an early
return cannot drop both the source and the match temporary.

Tests: `eager-default-drop`, `match-all-literal-fields`,
`match-partial-literal-field`, `match-string-literal`, `match-try-early-return`,
`panic-match-literal`,
`match-string-literal-unit`, `move-field-rejected`, `drop-field-in-place`,
`drop-rc-field-rejected`, `drop-shared-field-rejected`,
`drop-deref-shared-field-rejected`, `eq-temp-struct-drop`.

## 13. Partial field moves and loop/temporary cleanup (fixed 0.8.10)

```atrat
struct Person:
    name: String
    city: String

let p = Person { name: "Ada".clone(), city: "London".clone() }
let name = p.name
print(p.city)
```

Moving a field used to mark the whole owner moved, so either sibling fields
became unusable or the owner's remaining fields leaked. Field moves now record
a path in the checker and zero the source field in codegen. The owner's normal
destructor drops only the untouched fields. Reads of `p.name`, duplicate moves,
and moving the whole of `p` are rejected; `p.city` remains usable and
`p.name = ...` restores the field.

The same zeroing rule makes partial moves sound through nested fields and
`Box` payloads. `Rc`, shared/`&mut` references, raw pointers and indexed elements
remain rejected because a shared or non-addressable owner cannot be zeroed
safely.

Two temporaries-at-scope bugs were found while testing this:

- Materialised expression temporaries were only in statement-end cleanup, so an
  early `?`, `return`, `break` or panic leaked them. They now enter the current
  drop scope and are marked dead after their normal drop.
- `for` loops recorded the loop-body drop scope one past its actual index, so
  `break`/`continue` skipped loop-local destructors and temporary pair operands.
  The loop now stores the body scope index.

Tests: `partial-move-fields`, `partial-move-nested`, `partial-move-temporary`,
`partial-move-box-field`, `partial-move-enum-field`,
`partial-move-assign-restore`, `partial-move-branch`, `move-moved-field`,
`move-whole-partial`, `move-rc-field`, `move-shared-field`,
`partial-move-loop`, `partial-move-method-read`, `panic-partial-move`,
`eq-temp-break-cleanup`.

## 14. Modules were a flat namespace (fixed 0.8.11)

```atrat
import "util" as u

fn main() -> i32:
    print(u.greet("atrat"))
    let p: u.Pair = u.Pair { a: 20, b: 22 }
    return 0
```

`import ... as alias` was parsed but the alias was discarded, and the loader
merged every file into one flat declaration list. Two modules could not both
export `Pair`, and `u.Pair` was parsed as field access on an unknown variable.

The loader now keeps each file as a `Unit`, assigns every non-root declaration
an internal module namespace, and a resolver rewrites qualified references
before the checker runs. Functions, structs, enums/variants, traits, generic
bounds, `dyn` types and struct literals all resolve through aliases. Duplicate
public names across modules are safe; ambiguous unqualified imports are rejected
with a hint to qualify. Cross-file `pub` still applies to both access forms.

Tests: `module-qualified`, `module-duplicate-names`,
`module-qualified-enum`, `module-qualified-trait`, `module-default-alias`,
`qualified-private`, `alias-only`, `ambiguous-import`.


## Running them

```bash
python tests/run_tests.py
python tests/fuzz_smoke.py
python tests/validation_matrix.py
python tests/sanitizers.py       # auto-detects clang or gcc with ASan+UBSan
```

The Linux CI job runs the sanitizer pass with GCC and leak detection. Locally,
LLVM MinGW clang runs the same suite under ASan+UBSan; Windows ASan has no
LeakSanitizer, so leak coverage is CI-only.
