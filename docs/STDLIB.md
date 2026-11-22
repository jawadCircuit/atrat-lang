# Atrat prelude and built-ins

Atrat currently has a small prelude (`atrat/prelude.atr`) plus compiler
built-ins. There is no `std::` namespace yet. This document describes what the
0.8.x compiler actually implements; it is not a promise that these names will
never change.

## Primitive types

| Type | Notes |
| --- | --- |
| `i8`, `i16`, `i32`, `i64`, `isize` | Signed integers |
| `u8`, `u16`, `u32`, `u64`, `usize` | Unsigned integers |
| `f32`, `f64` | Floating point |
| `bool`, `char` | Boolean and character |
| `str` | Borrowed string view |
| `String` | Owned, growable UTF-8 string |
| `unit` (`()`) | Empty value |

Integer overflow uses wrapping arithmetic (`-fwrapv` in generated C).
Division, modulo and out-of-range shifts panic instead of invoking undefined
behaviour.

## `String` and `str`

String literals are `str`. A `String` owns its bytes and can be moved; `str`
does not own its bytes.

```atrat
let owned: String = "hello"
let view: str = "world"
print(owned.len(), view.len())
print(owned + " " + view)
```

| Expression | Meaning |
| --- | --- |
| `s.len()` | Byte length |
| `s.is_empty()` | Length is zero |
| `s.clone()` | Owned copy |
| `a + b` | Concatenation |
| `a += b` | Append/concatenate into an owned binding |
| `a == b`, `a < b`, ... | String comparison |
| `len(s)` | Built-in length |
| `to_str(x)` | Format a printable value as `String` |

String interpolation is supported: `"value = {x}"`.

## `List<T>`

List literals infer `List<T>`:

```atrat
var numbers = [1, 2, 3]
numbers.push(4)
print(numbers.len())
```

| Method | Meaning |
| --- | --- |
| `len()` | Number of elements |
| `is_empty()` | Whether the list has no elements |
| `push(value)` | Append; needs mutable access |
| `pop()` | Remove and return `Option<T>`; needs mutable access |
| `get(index)` | Return `Option<T>` by copy; `T` must be `Copy` |
| `set(index, value)` | Replace an element; needs mutable access |
| `clear()` | Drop all elements; needs mutable access |

`for x in &list` borrows each element as `&T`. Moving a non-`Copy` value out of
a list by indexing is rejected; use `pop()`.

## `Option<T>`

```atrat
let value: Option<i64> = Some(4)
print(value.unwrap_or(0))
```

| Method | Meaning |
| --- | --- |
| `is_some()` / `is_none()` | Tag checks; borrow the receiver |
| `unwrap()` | Return `T`; panic on `None` |
| `expect(message)` | Return `T`; panic with `message` on `None` |
| `unwrap_or(default)` | Return `T` or the default |
| `ok_or(error)` | Convert to `Result<T, E>` |

`unwrap`, `expect`, `unwrap_or` and `ok_or` consume a non-`Copy` `Option` when
called by value. They cannot move a non-`Copy` payload out of a borrow.

## `Result<T, E>`

```atrat
fn divide(a: i64, b: i64) -> Result<i64, String>:
    if b == 0:
        return Err("division by zero")
    return Ok(a / b)
```

| Method | Meaning |
| --- | --- |
| `is_ok()` / `is_err()` | Tag checks; borrow the receiver |
| `unwrap()` | Return `T`; panic on `Err` |
| `expect(message)` | Return `T`; panic with `message` on `Err` |
| `unwrap_err()` | Return `E`; panic on `Ok` |
| `unwrap_or(default)` | Return `T` or the default |
| `ok()` | Convert to `Option<T>`, dropping any error payload |
| `err()` | Convert to `Option<E>`, dropping any success payload |

The postfix `?` operator returns early from a function returning `Result` (or
`Option`) when the value is the error/none variant.

Consuming methods drop the payload they do not return. For example,
`Result<T, E>.ok()` drops `E` when the value is `Err`; this behaviour is covered
by strong-count and sanitizer tests.

## `Box<T>`

`box(value)` allocates an owned value on the heap:

```atrat
let b: Box<String> = box("owned".clone())
print((*b).len())
drop(b)
```

`Box<T>` is moved, not copied. Field, method and dereference access on a
**bound** box auto-dereferences, so `boxed_string.len()` and
`boxed_point.get()` mean `(*boxed_string).len()` and `(*boxed_point).get()`.
Dereferencing a `Box` for a non-`Copy` value cannot move the payload out; borrow
it, clone the payload, or consume the box explicitly. A `Box<dyn Trait>` is the
owned trait-object form and its temporary method calls have dedicated cleanup.

A temporary `box(...)` or `rc(...)` cannot be used as a field/method/deref
receiver: there is no binding to carry its drop flag, so doing so would leak the
allocation. Bind it first:

```atrat
let boxed: Box<Point> = box(Point { x: 1 })
print(boxed.x)

# rejected: no owner to drop the temporary
# print(box(Point { x: 1 }).x)
```

Owned temporaries can still be passed to functions or explicitly dropped:
`drop(box(...))`, `drop(rc(...))` and `drop(make_string())` are supported.

## `Rc<T>`

`rc(value)` creates a non-atomic reference-counted allocation:

```atrat
let shared = rc(7)
let second = shared.clone()
print(shared.strong_count())
drop(second)
print(shared.strong_count())
```

| Method | Meaning |
| --- | --- |
| `clone()` | Increment the strong count and return another handle |
| `strong_count()` | Read the current strong count |

`Rc` provides shared access only. Mutating through an `Rc` is rejected; there is
no `Rc::get_mut` yet. `Arc`, threads and `Mutex` do not exist yet.

## Built-in functions

| Function | Meaning |
| --- | --- |
| `print(...)`, `println(...)`, `write(...)` | Formatted output; `write` omits the newline |
| `panic(message)` | Print to stderr and abort the process |
| `assert(condition, message?)` | Panic when the condition is false |
| `exit(code)` | Exit the process |
| `len(value)` | Length of a string or list |
| `to_str(value)` | Format a value as `String` |
| `min(a, b)`, `max(a, b)` | Numeric minimum/maximum |
| `abs(x)`, `sqrt(x)` | Numeric helpers |
| `drop(value)` | Run destruction now and mark the value moved |
| `box(value)` | Allocate `Box<T>` |
| `rc(value)` | Allocate `Rc<T>` |
| `read_file(path)` | `Result<String, String>` |
| `write_file(path, data)` | `Result<(), String>` |
| `env(name)` | `Option<String>` |
| `args()` | `List<String>` of process arguments |
| `now_ms()` | Milliseconds as `u64` |

## Ownership and destruction rules

- `let` is immutable; `var` is mutable.
- Non-`Copy` values move. A moved value cannot be used again.
- Locals are dropped in reverse declaration order at scope exit, including
  early `return`.
- Branch and loop cleanup uses per-binding drop flags, so a value is destroyed
  exactly once even when only one branch initializes it.
- `drop(value)` performs destruction immediately and marks the binding moved.
- A partially initialized local cannot be read or have its fields assigned.
  Use a complete struct literal or assign the whole value.
- `Box`/`Rc` payload destructors run recursively, including nested `Option`,
  `Result`, struct fields and trait objects.
- **Panics abort the process; Atrat does not unwind and does not run local
  destructors on a panic.** The OS reclaims process memory. Do not rely on
  destructors for cleanup after `panic`, `assert`, `unwrap`, division by zero
  or another fatal runtime check. This is a real limitation, not Rust-style
  unwinding.

## Trait objects

- `&dyn Trait`, `&mut dyn Trait` and `Box<dyn Trait>` are supported.
- A bare `dyn Trait` value is rejected.
- Trait objects use a generated vtable and a destructor entry for owned boxes.
- `Box<dyn Trait>` can be created from `box(value)`, a `Box<T>` local, or a call
  returning a `Box<T>`.

## Not implemented yet

- Explicit lifetime parameters
- Associated types and generic traits beyond the current method signatures
- Nested or returning closures
- `Arc`, threads, `Mutex`, interior mutability
- Module visibility enforcement and separate compilation
- A formatter, LSP, debugger or package registry
- A stable standard-library API
