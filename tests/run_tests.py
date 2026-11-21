#!/usr/bin/env python3
"""End-to-end tests for the Atrat toolchain.

Each case is compiled and executed natively; failures check that the front end
rejects the program with the expected diagnostic.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")

RUN_CASES = [
    (
        "interpolation",
        """
fn main() -> i32:
    let name = "world"
    let n = 6 * 7
    print("hello {name}, n = {n}")
    print(1 + 2, true, 2.5)
    return 0
""",
        "hello world, n = 42\n3 true 2.5\n",
    ),
    (
        "adt-match",
        """
enum Tree:
    Leaf(i64)
    Node(i64, i64)

fn total(t: &Tree) -> i64:
    match t:
        Tree.Leaf(v) -> v
        Tree.Node(a, b) -> a + b

fn kind(t: &Tree) -> String:
    match t:
        Leaf(_) -> "leaf"
        Node(_, _) -> "node"

fn main() -> i32:
    let ts = [Tree.Leaf(3), Tree.Node(4, 5)]
    var sum = 0
    for t in &ts:
        sum += total(t)
    print("sum = {sum}")
    print("{kind(&ts[0])} {kind(&ts[1])}")
    return 0
""",
        "sum = 12\nleaf node\n",
    ),
    (
        "string-ops",
        """
fn main() -> i32:
    let a = "A"
    let b = "B"
    print("{a}" + "{b}")
    print(("x" + to_str(1)) == "x1")
    print("z" < "zz")
    var s: String = "q"
    s += "{a}{b}"
    print(s)
    return 0
""",
        "AB\ntrue\ntrue\nqAB\n",
    ),
    (
        "returned-borrows",
        """
struct P:
    x: i64
    y: i64

impl P:
    fn get(&self) -> &i64:
        return &self.x
    fn get_mut(&mut self) -> &mut i64:
        return &mut self.y

fn id(x: &i64) -> &i64:
    return x

fn main() -> i32:
    let p = P { x: 7, y: 3 }
    print(*p.get())
    let r = p.get()
    print(*r)
    let z = id(&p.y)
    print(*z)

    var q = P { x: 0, y: 0 }
    let m = q.get_mut()
    *m = 9
    print(*m)
    return 0
""",
        "7\n7\n3\n9\n",
    ),
    (
        "try-methods",
        """
fn safe_div(a: i64, b: i64) -> Result<i64, String>:
    if b == 0:
        return Err("division by zero")
    return Ok(a / b)

fn chain(a: i64, b: i64) -> Result<i64, String>:
    let q = safe_div(a, b)?
    return Ok(q + 1)

fn checked(x: i64) -> Option<i64>:
    if x < 0:
        return None
    return Some(x)

fn twice(x: i64) -> Option<i64>:
    let v = checked(x)?
    return Some(v * 2)

fn unit_result(ok: bool) -> Result<(), String>:
    if ok:
        return Ok(())
    return Err("not ok")

fn main() -> i32:
    print(chain(8, 2).unwrap())
    match chain(1, 0):
        Ok(v) -> print("unexpected {v}")
        Err(e) -> print("caught: {e}")

    print(twice(21).unwrap())
    print(twice(-1).is_none())

    let okv: Result<i64, String> = Ok(7)
    let errv: Result<i64, String> = Err("bad")
    print(okv.is_ok())
    print(errv.is_err())
    print(errv.unwrap_or(99))
    print(okv.ok().is_some())
    let o: Option<i64> = None
    print(o.unwrap_or(5))
    print(o.ok_or("missing").is_err())

    match unit_result(true):
        Ok(_) -> print("unit ok")
        Err(e) -> print("unit err: {e}")
    return 0
""",
        "5\ncaught: division by zero\n42\ntrue\ntrue\ntrue\n99\ntrue\n5\ntrue\nunit ok\n",
    ),
    (
        "traits",
        """
trait Show:
    fn show(&self) -> String

struct Point:
    x: i64
    y: i64

impl Show for Point:
    fn show(&self) -> String:
        return "({self.x}, {self.y})"

struct User:
    name: String

impl Show for User:
    fn show(&self) -> String:
        return "user:{self.name}"

struct Plain:
    n: i64

impl Plain:
    fn show(&self) -> String:
        return "plain:{self.n}"

fn main() -> i32:
    let p = Point { x: 3, y: 4 }
    let u = User { name: "ada" }
    let q = Plain { n: 9 }
    print(p.show())
    print(u.show())
    print(q.show())
    return 0
""",
        "(3, 4)\nuser:ada\nplain:9\n",
    ),
    (
        "trait-bounds",
        """
trait Show:
    fn show(&self) -> String

impl Show for i64:
    fn show(&self) -> String:
        return "i64({*self})"

struct Point:
    x: i64
    y: i64

impl Show for Point:
    fn show(&self) -> String:
        return "Point({self.x}, {self.y})"

fn show_all<T: Show>(a: &T) -> String:
    return a.show()

fn main() -> i32:
    let n = 5
    let p = Point { x: 1, y: 2 }
    print(show_all(&n))
    print(show_all(&p))
    print((7).show())
    return 0
""",
        "i64(5)\nPoint(1, 2)\ni64(7)\n",
    ),
    (
        "closures",
        """
fn apply(f: fn(i64) -> i64, x: i64) -> i64:
    return f(x)

fn main() -> i32:
    let double: fn(i64) -> i64 = |x: i64| x * 2
    print(double(21))
    print(apply(|x: i64| x + 1, 41))
    return 0
""",
        "42\n42\n",
    ),
    (
        "rc",
        """
struct P:
    name: String
    n: i64

fn main() -> i32:
    let a = rc(P { name: "shared", n: 7 })
    let b = a.clone()
    print(b.n)
    print(a.strong_count())
    print(b)
    print((*a).name)
    return 0
""",
        "7\n2\nrc({ name: shared, n: 7 })\nshared\n",
    ),
    (
        "trait-default",
        """
trait Named:
    fn name(&self) -> String
    fn greeting(&self) -> String:
        return "hello {self.name()}"

struct P:
    n: String

impl Named for P:
    fn name(&self) -> String:
        return self.n.clone()

fn main() -> i32:
    let p = P { n: "ada" }
    print(p.greeting())
    return 0
""",
        "hello ada\n",
    ),
    (
        "box",
        """
struct P:
    x: i64
    name: String

fn main() -> i32:
    let b = box(P { x: 41, name: "answer" })
    print(b.x + 1)
    print((*b).name)
    print(b)
    let n = box(7)
    print(*n * 6)
    return 0
""",
        "42\nanswer\nbox({ x: 41, name: answer })\n42\n",
    ),
    (
        "borrow-scoped",
        """
fn main() -> i32:
    var xs = [1, 2, 3]
    while xs.len() > 0:
        xs.pop()
    if xs.is_empty():
        print("empty")
    for i in 0..3:
        xs.push(i)
    print(xs.len())
    return 0
""",
        "empty\n3\n",
    ),
    (
        "generics",
        """
fn identity<T>(x: T) -> T:
    x

fn second<T>(xs: &List<T>) -> T:
    xs[1]

fn main() -> i32:
    print(identity(41))
    print(identity("typed"))
    let nums = [10, 20, 30]
    print(second(&nums))
    return 0
""",
        "41\ntyped\n20\n",
    ),
    (
        "lists-options",
        """
struct Point:
    x: i64
    y: i64

impl Point:
    fn norm1(&self) -> i64:
        abs(self.x) + abs(self.y)

fn main() -> i32:
    var xs = [1, 2, 3]
    xs.push(4)
    print(xs)
    print(xs.len(), xs.is_empty())
    let last = xs.pop()
    print(last)
    match last:
        Some(v) -> print("popped {v}")
        None -> print("empty")
    let p = Point { x: -3, y: 4 }
    print("norm = {p.norm1()}")
    print(xs.get(10))
    return 0
""",
        "[1, 2, 3, 4]\n4 false\nSome(4)\npopped 4\nnorm = 7\nNone\n",
    ),
    (
        "result",
        """
fn div(a: i64, b: i64) -> Result<i64, String>:
    if b == 0:
        return Err("by zero")
    return Ok(a / b)

fn main() -> i32:
    let r = div(8, 2)
    match r:
        Ok(v) -> print("ok {v}")
        Err(e) -> print("err {e}")
    let bad = div(1, 0)
    print(bad)
    return 0
""",
        "ok 4\nErr(by zero)\n",
    ),
    (
        "control-flow",
        """
fn fib(n: i64) -> i64:
    match n:
        0 -> 0
        1 -> 1
        _ -> fib(n - 1) + fib(n - 2)

fn main() -> i32:
    print(fib(10))
    var total = 0
    for i in 0..=10:
        if i % 2 == 0:
            continue
        if i > 7:
            break
        total += i
    print(total)
    var n = 0
    while n < 3:
        n += 1
    print(n)
    return 0
""",
        "55\n16\n3\n",
    ),
    (
        "rvalue-methods",
        """
struct P:
    x: i64

impl P:
    fn get(&self) -> i64:
        self.x

fn make_p(x: i64) -> P:
    P { x: x }

fn main() -> i32:
    print([1, 2, 3].len())
    print([10, 20, 30][1])
    print(to_str(12345).len())
    print(make_p(7).get())
    print("z" < "zz")
    print(now_ms() > 0)
    return 0
""",
        "3\n20\n5\n7\ntrue\ntrue\n",
    ),
    (
        "unsafe",
        """
extern fn getpid() -> i32

fn main() -> i32:
    var x: i64 = 41
    unsafe:
        let p = &mut x as *mut i64
        *p = *p + 1
        asm("nop")
    print(x)
    print(getpid() > 0)
    return 0
""",
        "42\ntrue\n",
    ),
    (
        "closure-capture",
        """
fn main() -> i32:
    let n = 5
    let f = |x: i64| x + n
    print(f(1))

    var m = 1
    let snapshot = || m
    m = 9
    print(snapshot())

    let s = "hello".clone()
    let r = &s
    let h = || r.len()
    print(h())
    return 0
""",
        "6" + chr(10) + "1" + chr(10) + "5" + chr(10),
    ),
    (
        "struct-references",
        """
struct View:
    r: &i64
    label: str

struct MutView:
    r: &mut i64

struct Outer:
    v: View

fn main() -> i32:
    let x = 41
    let v = View { r: &x, label: "answer" }
    print(*v.r)
    print(v.label)
    let o = Outer { v: View { r: &x, label: "nested" } }
    print(*o.v.r)

    var y = 1
    let m = MutView { r: &mut y }
    *m.r = 7
    print(*m.r)
    return 0
""",
        "41" + chr(10) + "answer" + chr(10) + "41" + chr(10) + "7" + chr(10),
    ),
    (
        "generic-impl",
        """
struct Wrap<T>:
    value: T

impl<T> Wrap<T>:
    fn get(&self) -> T:
        return self.value
    fn id<U>(&self, other: U) -> U:
        return other

trait Label:
    fn label(&self) -> String

impl<T> Label for Wrap<T>:
    fn label(&self) -> String:
        return "wrap"

fn main() -> i32:
    let a = Wrap { value: 41 }
    print(a.get() + 1)
    print(a.id("hi"))
    print(a.label())
    let b = Wrap { value: "text" }
    print(b.label())
    return 0
""",
        "42" + chr(10) + "hi" + chr(10) + "wrap" + chr(10) + "wrap" + chr(10),
    ),
    (
        "dyn-trait",
        """
trait Named:
    fn name(&self) -> String
    fn greet(&self) -> String:
        return "hi {self.name()}"

struct P:
    n: String

impl Named for P:
    fn name(&self) -> String:
        return self.n.clone()

impl Named for i64:
    fn name(&self) -> String:
        return "int"

fn render(x: &dyn Named) -> String:
    return x.greet()

trait Counter:
    fn bump(&mut self) -> i64

struct C:
    n: i64

impl Counter for C:
    fn bump(&mut self) -> i64:
        self.n += 1
        return self.n

fn main() -> i32:
    let p = P { n: "p" }
    let d: &dyn Named = &p
    print(d.greet())
    print(render(&p))

    let x = 7
    let e: &dyn Named = &x
    print(e.greet())

    var c = C { n: 0 }
    let m: &mut dyn Counter = &mut c
    print(m.bump())
    print(m.bump())
    return 0
""",
        "hi p" + chr(10) + "hi p" + chr(10) + "hi int" + chr(10) + "1" + chr(10) + "2" + chr(10),
    ),
    (
        "recursive-drop",
        """
struct Node:
    value: i64
    next: Option<Box<Node>>

fn build(n: i64) -> Option<Box<Node>>:
    if n == 0:
        return None
    return Some(box(Node { value: n, next: build(n - 1) }))

fn main() -> i32:
    let h = build(100).unwrap()
    print(h.value)
    let s = Some("payload".clone()).unwrap()
    print(s)
    let r: Result<String, String> = Ok("ok-value".clone())
    print(r.unwrap())
    return 0
""",
        "100" + chr(10) + "payload" + chr(10) + "ok-value" + chr(10),
    ),
    (
        "ref-methods",
        """
fn str_len(s: &str) -> usize:
    return s.len()

fn main() -> i32:
    let s = "hello".clone()
    let r = &s
    print(r.len())
    print(len(r))
    print(r.clone())
    print(r + "!")
    let t = "world"
    print(str_len(&t))

    let v = [10, 20, 30]
    let l = &v
    print(l.len())
    print(l[2])
    return 0
""",
        "5" + chr(10) + "5" + chr(10) + "hello" + chr(10) + "hello!" + chr(10)
        + "5" + chr(10) + "3" + chr(10) + "30" + chr(10),
    ),
    (
        "trait-default-multi",
        """
trait Named:
    fn name(&self) -> String
    fn twice(&self) -> String:
        return "{self.name()}+{self.name()}"

struct A:
    x: i64
struct B:
    x: i64

impl Named for A:
    fn name(&self) -> String:
        return "A"
impl Named for B:
    fn name(&self) -> String:
        return "B"

fn main() -> i32:
    let a = A { x: 1 }
    let b = B { x: 2 }
    print(a.twice())
    print(b.twice())
    return 0
""",
        "A+A" + chr(10) + "B+B" + chr(10),
    ),
    (
        "closure-non-copy-capture",
        """
fn main() -> i32:
    let s = "hello".clone()
    let f = || s.len()
    print(f())
    print(f())

    let t = "world".clone()
    let g = || t + "!"
    print(g())

    var v = [1, 2, 3]
    let h = || v.len()
    print(h())
    return 0
""",
        "5" + chr(10) + "5" + chr(10) + "world!" + chr(10) + "3" + chr(10),
    ),
    (
        "generic-recursive",
        """
struct Node<T>:
    value: T
    children: List<Node<T>>

fn main() -> i32:
    let a = Node { value: 1, children: [] }
    let b = Node { value: 2, children: [] }
    let tree = Node { value: 0, children: [a, b] }
    print(tree.value)
    print(tree.children.len())
    return 0
""",
        "0" + chr(10) + "2" + chr(10),
    ),
    (
        "method-through-mut-field",
        """
trait C:
    fn bump(&mut self) -> i64

struct S:
    n: i64

impl C for S:
    fn bump(&mut self) -> i64:
        self.n += 1
        return self.n

struct H:
    v: &mut S

fn main() -> i32:
    var s = S { n: 0 }
    let h = H { v: &mut s }
    print(h.v.bump())
    print(h.v.bump())
    return 0
""",
        "1" + chr(10) + "2" + chr(10),
    ),
]

FAIL_CASES = [
    (
        "use-after-move",
        """
fn main() -> i32:
    let s: String = "hi"
    let t = s
    print(s)
    return 0
""",
        "borrow error",
        "moved value",
    ),
    (
        "immutable-assign",
        """
fn main() -> i32:
    let x = 1
    x = 2
    return 0
""",
        "borrow error",
        "immutable",
    ),
    (
        "borrow-conflict",
        """
fn main() -> i32:
    var x = 1
    let r = &x
    x = 2
    print(r)
    return 0
""",
        "borrow error",
        "while it is borrowed",
    ),
    (
        "non-exhaustive",
        """
enum E:
    A
    B

fn main() -> i32:
    let e = E.A
    match e:
        A -> print("a")
    return 0
""",
        "type error",
        "not exhaustive",
    ),
    (
        "missing-return",
        """
fn f(x: i64) -> i64:
    if x > 0:
        return 1

fn main() -> i32:
    print(f(1))
    return 0
""",
        "type error",
        "without returning",
    ),
    (
        "type-mismatch",
        """
fn main() -> i32:
    let x: i64 = "text"
    return 0
""",
        "type error",
        "mismatched types",
    ),
    (
        "before-init",
        """
fn main() -> i32:
    let s: String
    print(s)
    return 0
""",
        "borrow error",
        "before it is initialized",
    ),
    (
        "move-while-borrowed",
        """
fn main() -> i32:
    let s: String = "x"
    let r = &s
    let t = s
    print(r)
    return 0
""",
        "borrow error",
        "while it is borrowed",
    ),
    (
        "wrong-arity",
        """
fn add(a: i64, b: i64) -> i64:
    a + b

fn main() -> i32:
    print(add(1))
    return 0
""",
        "type error",
        "argument(s)",
    ),
    (
        "unknown-field",
        """
struct P:
    x: i64

fn main() -> i32:
    let p = P { x: 1 }
    print(p.y)
    return 0
""",
        "type error",
        "unknown field",
    ),
    (
        "branch-move",
        """
fn main() -> i32:
    let s: String = "x"
    if true:
        drop(s)
    print(s)
    return 0
""",
        "borrow error",
        "moved value",
    ),
    (
        "uninit-branch",
        """
fn main() -> i32:
    var s: String
    if true:
        s = "x"
    print(s)
    return 0
""",
        "borrow error",
        "before it is initialized",
    ),
    (
        "uninit-loop",
        """
fn main() -> i32:
    var s: String
    while false:
        s = "x"
    print(s)
    return 0
""",
        "borrow error",
        "before it is initialized",
    ),
    (
        "uninitialized-borrow",
        """
fn main() -> i32:
    var x: i64
    let p = &x
    print(p)
    return 0
""",
        "borrow error",
        "before it is initialized",
    ),
    (
        "borrow-while-getter",
        """
struct P:
    x: i64

impl P:
    fn get(&self) -> &i64:
        return &self.x

fn main() -> i32:
    var p = P { x: 1 }
    let r = p.get()
    p.x = 2
    print(*r)
    return 0
""",
        "borrow error",
        "while it is borrowed",
    ),
    (
        "read-while-mut-borrow",
        """
fn main() -> i32:
    var x = 1
    let r = &mut x
    print(x)
    *r = 2
    return 0
""",
        "borrow error",
        "while it is mutably borrowed",
    ),
    (
        "trait-bound",
        """
trait Show:
    fn show(&self) -> String

struct Q:
    x: i64

fn show_all<T: Show>(a: &T) -> String:
    return a.show()

fn main() -> i32:
    let q = Q { x: 1 }
    print(show_all(&q))
    return 0
""",
        "type error",
        "does not implement trait",
    ),
    (
        "return-ref-local",
        """
struct P:
    x: i64

fn dangle() -> &i64:
    let p = P { x: 7 }
    let r = &p.x
    return r

fn main() -> i32:
    let q = dangle()
    print(*q)
    return 0
""",
        "type error",
        "reference parameter",
    ),
    (
        "move-borrowed-field",
        """
struct P:
    n: String

impl P:
    fn take(&self) -> String:
        return self.n

fn main() -> i32:
    let p = P { n: "x" }
    print(p.take())
    return 0
""",
        "borrow error",
        "borrowed",
    ),
    (
        "ref-unwrap-move",
        """
struct Node:
    value: i64
fn main() -> i32:
    let o = Some(box(Node { value: 1 }))
    let r = &o
    let b = r.unwrap()
    print(b.value)
    return 0
""",
        "type error",
        "cannot move a non-Copy value out of",
    ),
    (
        "closure-move-capture",
        """
fn main() -> i32:
    let s = "hello".clone()
    let f = || s.len()
    print(s)
    return 0
""",
        "borrow error",
        "use of moved value",
    ),
]


MULTI_CASES = [
    (
        "modules",
        {
            "sub/math.atr": """
fn add(a: i64, b: i64) -> i64:
    return a + b
""",
            "util.atr": """
import "sub/math"

fn greet(name: str) -> String:
    return "hello, {name}"

fn answer() -> i64:
    return add(40, 2)
""",
            "main.atr": """
import "util"

fn main() -> i32:
    print(greet("atrat"))
    print(answer())
    return 0
""",
        },
        "main.atr",
        "hello, atrat\n42\n",
    ),
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="atrat-tests-")
    failed = 0
    total = 0

    for name, src, expected in RUN_CASES:
        total += 1
        path = os.path.join(tmp, name + ".atr")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        exe = os.path.join(tmp, name + (".exe" if os.name == "nt" else ""))
        built = run([sys.executable, ATRC, "build", path, "-o", exe])
        if built.returncode != 0:
            print(f"FAIL {name}: build failed\n{built.stderr}")
            failed += 1
            continue
        proc = run([exe])
        if proc.stdout != expected:
            print(f"FAIL {name}: output mismatch")
            print(f"  expected: {expected!r}")
            print(f"  actual:   {proc.stdout!r}")
            if proc.stderr:
                print(f"  stderr:   {proc.stderr}")
            failed += 1
        else:
            print(f"ok   {name}")

    for name, files, main_file, expected in MULTI_CASES:
        total += 1
        d = os.path.join(tmp, name)
        os.makedirs(d, exist_ok=True)
        for rel, src in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
        exe = os.path.join(d, "main" + (".exe" if os.name == "nt" else ""))
        built = run([sys.executable, ATRC, "build", os.path.join(d, main_file), "-o", exe])
        if built.returncode != 0:
            print(f"FAIL {name}: build failed\n{built.stderr}")
            failed += 1
            continue
        proc = run([exe])
        if proc.stdout != expected:
            print(f"FAIL {name}: output mismatch")
            print(f"  expected: {expected!r}")
            print(f"  actual:   {proc.stdout!r}")
            failed += 1
        else:
            print(f"ok   {name}")

    for name, src, kind, needle in FAIL_CASES:
        total += 1
        path = os.path.join(tmp, "fail_" + name + ".atr")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        proc = run([sys.executable, ATRC, "check", path])
        combined = proc.stdout + proc.stderr
        if proc.returncode == 0:
            print(f"FAIL {name}: expected rejection, but it type-checked")
            failed += 1
        elif kind not in combined or needle not in combined:
            print(f"FAIL {name}: wrong diagnostic\n{combined}")
            failed += 1
        else:
            print(f"ok   {name} (rejected)")

    print(f"\n{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
