# The Atrat Language Reference

This document describes Atrat as implemented by `atrc` in this repository.
Anything marked *not yet* is rejected by the compiler rather than silently
accepted.

## 1. Lexical structure

- Source is UTF-8.
- Comments run from `#` to the end of the line.
- Statements end at a newline. **Indentation defines blocks** (4 spaces per
  level; tabs are rejected). Inside `(`, `[`, `{` newlines are ignored.
- Identifiers: `[A-Za-z_][A-Za-z0-9_]*`. `_` alone is the wildcard pattern.
- Keywords: `fn let var const struct enum impl trait match if elif else while
  for in return break continue true false unsafe asm extern import as mut self
  pub type where loop defer`.
- Integer literals: decimal, `0x`, `0b`, `0o`, with `_` separators.
- Float literals: `1.5`, `1e9`, `2.5e-3`.
- Character literals: `'a'`, `'\n'`, `'\x41'`, `'\u00e9'`.
- Strings: `"..."` with `\n \t \r \0 \\ \" \' \xNN \uNNNN`, `{{` and `}}` for
  literal braces. `{expr}` interpolates any expression.

```atrat
let who = "world"
print("hello {who}, 1 + 2 = {1 + 2}, braces = {{ok}}")
```

## 2. Types

| Type | Meaning |
| --- | --- |
| `i8 i16 i32 i64 isize` | signed integers |
| `u8 u16 u32 u64 usize` | unsigned integers |
| `f32 f64` | IEEE floats |
| `bool` | `true` / `false` |
| `char` | Unicode scalar value (stored as `uint32_t`) |
| `str` | borrowed immutable UTF-8 bytes (`{ptr, len}`, `Copy`) |
| `String` | owned UTF-8 buffer (`{ptr, len, cap}`, move-only, drops) |
| `()` / `unit` | the unit type |
| `[T]` / `List<T>` | growable heap list, move-only, drops |
| `Box<T>` | owned heap value, deref with `*b`, drops |
| `Rc<T>` | reference-counted shared value (read-only), `.clone()` |
| `Option<T>` | `Some(T)` / `None` (prelude) |
| `Result<T, E>` | `Ok(T)` / `Err(E)` (prelude) |
| `struct` / `enum` | nominal aggregates |
| `&T`, `&mut T` | non-escaping shared / mutable references (returned borrows are tied to their single reference parameter) |
| `*T`, `*mut T` | raw pointers (unsafe to use) |
| `fn(T, ...) -> U` | function pointer type |

Integer and float literals get a fresh constrained type variable and default to
`i64` / `f64` when unconstrained. A literal integer may flow into any numeric
type, including a float type (`let x: f64 = 1`).

`Copy` types: all primitives, `str`, references, pointers, function pointers,
and structs/enums whose fields are all `Copy`. Everything else is move-only:
`String`, `List<T>`, `Box<T>`, `Rc<T>`, and aggregates containing them.

## 3. Declarations

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

fn add(a: i64, b: i64) -> i64:
    a + b

extern fn getpid() -> i32
```

- A function's last expression is its return value; an explicit `return` also
  works. Every path of a non-unit function must return.
- `impl Type:` may only contain methods whose first parameter is `self`,
  `&self` or `&mut self`. Dispatch is static (monomorphised), not virtual.
- Enum variants carry positional fields. `Option`/`Result` are ordinary generic
  enums defined in `atrat/prelude.atr`.
- `@repr(C)` is accepted before a struct/enum; output C layout is already
  `repr(C)`-style.
- `import "path"` (or `import a.b`) loads another file relative to the current
  file. Modules share one flat namespace; duplicate imports are deduplicated and
  cycles are rejected. `pub` is accepted but visibility is not enforced yet.
- Traits declare method signatures; `impl Trait for Type:` provides them.
  Dispatch is static, and generic functions can require bounds (see below).

## 4. Generics

Generic parameters are explicit and monomorphised:

```atrat
fn identity<T>(x: T) -> T:
    x

struct Node<T>:
    value: T
    children: List<Node<T>>

fn first_or<T>(xs: &List<T>, fallback: T) -> T:
    if xs.len() > 0:
        return xs[0]
    fallback
```

- Type arguments are inferred at the call site.
- Each concrete instantiation becomes a distinct C function/struct/enum.
- Recursion through `List<T>` is fine (heap indirection); a struct containing
  itself by value would be an infinite type and fails in C.
- Generic `impl` blocks are not supported yet; put type parameters on methods.

Trait bounds constrain a generic argument:

```atrat
trait Show:
    fn show(&self) -> String

impl Show for i64:
    fn show(&self) -> String:
        return "i64({*self})"

fn show_all<T: Show>(x: &T) -> String:
    return x.show()
```

Every concrete type used at a call site must implement the bound, checked during
monomorphisation. Implementations are allowed on structs, enums and primitives.
Generic impl blocks are supported:

```atrat
struct Wrap<T>:
    value: T

impl<T> Wrap<T>:
    fn get(&self) -> T:
        return self.value
```

Trait methods may have default bodies; an impl may omit them:

```atrat
trait Named:
    fn name(&self) -> String
    fn greeting(&self) -> String:
        return "hello {self.name()}"
```

Traits can extend other traits. The child inherits all parent methods and an
implementation must also implement every parent:

```atrat
trait Named:
    fn name(&self) -> String

trait Greet: Named:
    fn greet(&self) -> String:
        return "hello, " + self.name()

impl Named for P:
    fn name(&self) -> String:
        return self.n.clone()

impl Greet for P:
    pass                         # all methods have defaults
```

`Self` is available in trait signatures and impl methods. Trait objects are
supported through `&dyn Trait` / `&mut dyn Trait` and owned `Box<dyn Trait>`:

```atrat
fn render(x: &dyn Show) -> String:
    return x.show()

let item = P { name: "p" }
let view: &dyn Show = &item
print(view.show())

let owned: Box<dyn Show> = box(P { name: "q" })
print(owned.show())
```

The compiler generates a vtable per `(trait, type)` pair, including inherited
methods and a destructor for owned trait objects. Trait methods used in a `dyn`
vtable must take `&self` or `&mut self`, must not be generic, and must not
return references or `Self`. A `Box<dyn Trait>` can be created from
`box(value)`, an existing `Box<T>` local (moved into the trait object), or a
call returning `Box<T>`. There are no associated types.

## 5. Expressions and operators

Precedence, loosest to tightest:

```
range  ..  ..=
||
&&
== !=
< <= > >=
|
^
&
<< >>
+ -
* / %
**            (right associative)
unary - ! ~ & &mut *   and postfix calls/index/field
as            (binds tighter than binary operators, looser than unary)
```

- Integer `/` truncates and `%` is remainder. Division/modulo by zero and
  `INT_MIN / -1` panic; shifts wider than the type panic. Signed overflow is
  defined two's-complement wrapping (the driver compiles with `-fwrapv`).
- Float `**` uses `pow`; integer `**` uses exponentiation by squaring.
- `+` concatenates when either side is a string, producing `String`.
- `==`, `!=`, `<`, ... work on numbers, `char`, strings, and aggregates
  (aggregates get generated structural equality).
- `&&` and `||` short-circuit.

String interpolation produces an owned `String`:

```atrat
let s: String = "x = {x}, y = {y}"
let t = s + "!"          # String + str -> String
```

`str` → `String` coercion is implicit in initialisers, arguments, returns,
struct fields, list literals and match arms; it copies the bytes.

### Propagating errors

`expr?` unwraps `Ok`/`Some` or returns `Err`/`None` from the enclosing function:

```atrat
fn load(path: str) -> Result<String, String>:
    let text = read_file(path)?
    return Ok(text)
```

The enclosing function must return the same `Result` error type, or `Option`
when propagating an `Option`. The early return runs the normal scope drops.

### Closures

Non-capturing closures are function values:

```atrat
let double: fn(i64) -> i64 = |x: i64| x * 2
let add_one: fn(i64) -> i64 = |x: i64| x + 1
```

Parameters need type annotations. Closures capture **Copy values and shared
references by value** at creation:

```atrat
let n = 10
let add_n = |x: i64| x + n
print(add_n(5))       # 15

var m = 1
let snapshot = || m
m = 9
print(snapshot())     # 1
```

The environment is a generated struct. Copy captures are copied; non-Copy
captures are **moved** into the closure and accessed through a shared reference
inside it:

```atrat
let text = "hello".clone()
let measure = || text.len()
print(measure())    # 5
# `text` is moved here; using it now is an error.
```

Inside the closure a non-Copy capture is read-only: it can be formatted, cloned
or passed to `&self` methods, but cannot be moved out or mutated. Nested
capturing closures, capture by `&mut`, and returning closures are not supported
yet.

### Box and Rc

`box(x)` allocates a `Box<T>`; `rc(x)` allocates a reference-counted `Rc<T>`.
Dereference with `*b`, access fields with `b.field`, and drop is recursive and
deterministic. `Box` enables recursive types:

```atrat
struct Node:
    value: i64
    next: Option<Box<Node>>
```

`Rc<T>` is read-only shared ownership:

```atrat
let a = rc(P { name: "shared", n: 7 })
let b = a.clone()
print(a.strong_count())   # 2
print(b.n)                # field access through the shared value
```

Mutating through `Rc`, taking `&mut` through it, or calling a `&mut self`
method through it is rejected. Use `Box` for unique ownership.

### Returned borrows

A method or function may return a borrow derived from its single reference
parameter:

```atrat
impl P:
    fn get(&self) -> &i64:
        return &self.x
    fn get_mut(&mut self) -> &mut i64:
        return &mut self.y
```

The caller's borrow lasts for the rest of the block, so mutating the owner while
the result is alive is rejected. Returning a borrow of a local or temporary is
rejected.

References can also be stored in structs:

```atrat
struct View:
    r: &i64

let x = 41
let v = View { r: &x }
print(*v.r)
```

A struct containing references can be returned when every reference in it
derives from the function's single reference parameter:

```atrat
struct View:
    r: &i64

impl P:
    fn view(&self) -> View:
        return View { r: &self.x }

fn make(x: &i64) -> View:
    return View { r: x }
```

The caller keeps the source borrowed for as long as the returned view lives.
A view cannot be built from a local that does not come from that parameter, and
returning a view of a temporary is rejected.

These are still conservative, block-scoped lifetimes: a value containing a
reference cannot be rebound, escaped through a nested closure, or stored beyond
its source's block. There are no explicit lifetime parameters yet.

## 6. Statements and control flow

```atrat
let a = 1              # immutable
var b: i64 = 2         # mutable
b += 3

if b > 3:
    print("big")
elif b == 3:
    print("three")
else:
    print("small")

for i in 0..10:        # exclusive
    print(i)

for i in 0..=10:       # inclusive
    print(i)

for x in &list:        # x: &T
    print(x)

for x in list:         # requires Copy elements
    print(x)

while b > 0:
    b -= 1
    if b == 2:
        continue
    if b == 1:
        break
```

`match` is an expression and may appear anywhere:

```atrat
let label = match n % 3:
    0 -> "fizz"
    1 -> "one"
    _ -> "other"
```

Patterns: `_`, a binding name, literals, `Variant(p, ...)` and
`Enum.Variant(p, ...)`. Matching must be exhaustive; an enum without a wildcard
must list every variant. Matching through `&enum` is allowed only when the enum
is `Copy`.

## 7. Ownership and borrows

### Moves and drops

- Passing or assigning a non-`Copy` value moves it.
- Moves are checked; a moved-from binding cannot be read, borrowed or moved again.
- `String` and `List<T>` are dropped at the end of their scope, in reverse
  declaration order. Moved values are not dropped.
- `match` on an owned non-`Copy` value moves it. Bound payloads become owned by
  the binding; unmatched payloads are dropped by the arm.
- `list[i]` copies the element and therefore requires `Copy`. Use
  `for x in &list` for borrowing iteration, or `list.pop()` / `list.get(i)`.

### Borrows

```atrat
var v = [1, 2, 3]
let a = &v        # shared borrow, lives until end of `a`'s scope
print(a.len())
v.push(4)         # ERROR while `a` is alive
```

Rules enforced:

1. Any number of shared borrows *or* exactly one mutable borrow at a time.
2. Cannot move or assign a value while it is borrowed.
3. `&mut x` requires `x` to be declared with `var`.
4. A borrow's lifetime is the enclosing block (when bound with `let`) or the
   enclosing statement (temporary borrows, e.g. `f(&x)`).
5. Borrows cannot escape: storing a reference in a struct or returning one is
   rejected until explicit lifetimes exist.

`unsafe:` blocks do not disable these rules for safe values; they add raw
pointer operations that the borrow checker deliberately does not track.

## 8. Unsafe and assembly

```atrat
var x: i64 = 41
unsafe:
    let p = &mut x as *mut i64
    *p = *p + 1
    asm("nop")
    asm("add {0}, {1}", *p, 1)
```

- Dereferencing a raw pointer, casting to/from a pointer, and `asm` require
  `unsafe:`.
- `asm` templates use `{0}`, `{1}`, ... placeholders for the operand list.
  Operands are passed as `"r"(expr)` inputs; the template is emitted inside
  `__asm__ __volatile__(...)` with a `"memory"` clobber.
- `extern fn name(args) -> ret` declares a C-ABI function and emits an
  `extern` prototype with the mangled-free name.

## 9. Built-in functions

| Builtin | Behaviour |
| --- | --- |
| `print(x, ...)` | space-separated values, newline |
| `println(x, ...)` | alias for `print` |
| `write(x, ...)` | like `print`, without the newline |
| `panic(msg)` | print `atrat panic: msg`, clean up live owners, exit 101 |
| `assert(cond, msg?)` | abort if the condition is false |
| `len(x)` | length of `List`, `str`, `String` |
| `to_str(x)` | owned `String` using the value's `show` form |
| `min(a, b)`, `max(a, b)`, `abs(x)` | numeric helpers |
| `sqrt(x)` | `f64` square root |
| `exit(code)` | terminate immediately; bypasses Atrat destructors and panic cleanup |
| `drop(x)` | drop a value early |
| `read_file(path)` | `Result<String, String>` |
| `write_file(path, text)` | `Result<(), String>` |
| `env(name)` | `Option<String>` |
| `args()` | `List<String>` (process arguments, excluding argv[0]) |
| `now_ms()` | wall-clock milliseconds as `u64` |
| `box(x)` | allocate a `Box<T>` on the heap |

List methods: `len`, `is_empty`, `push`, `pop`, `get`, `set`, `clear`.
`str`/`String` methods: `len`, `is_empty`, `clone`.

`Option<T>` methods: `is_some`, `is_none`, `unwrap`, `expect(msg)`,
`unwrap_or(default)`, `ok_or(err)`.
`Result<T, E>` methods: `is_ok`, `is_err`, `unwrap`, `expect(msg)`,
`unwrap_err`, `unwrap_or(default)`, `ok`, `err`.

`pop()` and `get(i)` return `Option<T>`. `get` requires `Copy` elements.

## 10. Memory and code generation

- Programs are transpiled to a single C99 translation unit and compiled with
  `gcc -std=gnu11 -O<level>` (`-O2` by default).
- The runtime is embedded and consists of a small string buffer, panic,
  allocation and formatting helpers.
- Every value lives on the stack or inline in its owner unless it is explicitly
  a `List`/`String`/`Box`/`Rc` allocation.
- Struct fields cannot be initialized one at a time on an uninitialized local;
  use a complete literal or assign the whole value. This rules out partially
  initialized structs in safe code.
- Non-`Copy` locals carry a compile-time-known `alive` flag in C when their
  moves are conditional, so drops are exact and double-frees cannot occur.
- `atrc emit-c` prints the whole generated translation unit.

## 11. Diagnostics

Errors include a stable code, file, line, column, a caret, a hint and sometimes
a note pointing at an earlier borrow:

```
error[E0303]: borrow error: use of moved value `s`
  --> app.atr:4:11
   |
 4 |     print(s)
   |           ^
   = help: the value was moved earlier; clone it or borrow instead
```

Error kinds: `lex error`, `parse error`, `type error`, `borrow error`,
`codegen error`. See `docs/DIAGNOSTICS.md` for the code table.

Runtime traps print `atrat panic: ...`, clean up live owning locals and function
parameters in reverse registration order, and terminate with status **101**:
list index out of bounds, division/modulo by zero, integer overflow in
`INT_MIN / -1`, shift out of range, failed `assert`, explicit `panic`, and
allocation failure.

**Panic cleanup is not unwinding.** The process does not return to Atrat code,
there is no `catch`/`recover`, and a panic cannot be intercepted. Objects that
are only expression temporaries, or that are still under construction, are not
guaranteed to be destroyed. A panic raised by a destructor during cleanup exits
immediately without running the remaining destructors. Normal returns, branches,
loops and explicit `drop` run destructors exactly once; panic cleanup covers
registered live locals and parameters. This is documented rather than presented
as Rust-style unwinding.

## 12. Grammar sketch

```
program      := decl*
decl         := fn | struct | enum | impl | trait | extern | import | attr decl
fn           := "fn" NAME generics? params ("->" type)? (":" block | NEWLINE)
params       := "(" (param ("," param)*)? ")"
param        := ("&" "mut"?)? NAME (":" type)? | "&" "mut"? "self"
struct       := "struct" NAME generics? ":" NEWLINE INDENT (NAME ":" type)* DEDENT
enum         := "enum" NAME generics? ":" NEWLINE INDENT variant* DEDENT
variant      := NAME ("(" type ("," type)* ")")?
trait        := "trait" NAME generics? (":" supertrait ("," supertrait)*)? ":" NEWLINE INDENT fnsig* DEDENT
impl         := "impl" generics? (NAME "for")? NAME ":" NEWLINE INDENT (fn | "pass")* DEDENT
block        := NEWLINE INDENT stmt+ DEDENT
stmt         := let | assign | expr | if | while | for | return | break
              | continue | pass | unsafe
expr         := binary ((".." | "..=") binary?)?
binary       := unary (op binary)?
unary        := ("-"|"!"|"~"|"&" "mut"?|"*") unary | postfix ("as" type)*
postfix      := primary ("(" args ")" | "[" expr "]" | "." NAME | "?")*
primary      := INT | FLOAT | BOOL | CHAR | STRING | NAME structlit?
              | "(" expr? ")" | listlit | match | asm | lambda
lambda       := "|" (NAME ":" type ("," ...)*)? "|" expr
match        := "match" expr ":" NEWLINE INDENT (pattern "->" expr)+ DEDENT
```
