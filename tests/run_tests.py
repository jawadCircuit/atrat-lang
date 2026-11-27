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
        "eq-temp-break-cleanup",
        """
struct P:
    r: Rc<i64>

fn main() -> i32:
    let r = rc(5)
    for i in 0..1:
        if P { r: r.clone() } == P { r: r.clone() }:
            break
    print(r.strong_count())
    return 0
""",
        "1" + chr(10),
    ),
    (
        "eq-temp-struct-drop",
        """
struct P:
    r: Rc<i64>

fn main() -> i32:
    let r = rc(5)
    let a = r.clone()
    let b = r.clone()
    let same = P { r: a } == P { r: b }
    print(same)
    print(r.strong_count())
    return 0
""",
        "true" + chr(10) + "1" + chr(10),
    ),
    (
        "eager-default-drop",
        """
fn main() -> i32:
    let r = rc(5)
    let o: Option<Rc<i64>> = Some(r.clone())
    let d = rc(9)
    let dc = d.clone()
    let x = o.unwrap_or(d)
    print(r.strong_count())
    print(dc.strong_count())
    print(*x)
    drop(x)
    print(r.strong_count())
    drop(dc)

    let p: Option<i64> = Some(1)
    let e = rc(7)
    let ec = e.clone()
    let res = p.ok_or(e)
    print(res.is_ok())
    print(ec.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10) + "5" + chr(10) + "1" + chr(10)
        + "true" + chr(10) + "1" + chr(10),
    ),
    (
        "partial-move-branch",
        """
struct P:
    a: String
    b: String

fn take(flag: bool) -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    if flag:
        let s = p.a
        print(s)
    print(p.b)
    return 0

fn main() -> i32:
    take(true)
    take(false)
    return 0
""",
        "a" + chr(10) + "b" + chr(10) + "b" + chr(10),
    ),
    (
        "partial-move-fields",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    let s = p.a
    print(s)
    print(p.b)
    return 0
""",
        "a" + chr(10) + "b" + chr(10),
    ),
    (
        "partial-move-nested",
        """
struct Inner:
    a: String
    b: String

struct P:
    inner: Inner
    c: String

fn main() -> i32:
    let p = P { inner: Inner { a: "a".clone(), b: "b".clone() }, c: "c".clone() }
    let s = p.inner.a
    print(s)
    print(p.inner.b)
    print(p.c)
    return 0
""",
        "a" + chr(10) + "b" + chr(10) + "c" + chr(10),
    ),
    (
        "partial-move-temporary",
        """
struct P:
    a: String
    b: String

fn make() -> P:
    return P { a: "made".clone(), b: "rest".clone() }

fn main() -> i32:
    let s = P { a: "a".clone(), b: "b".clone() }.a
    print(s)
    let t = make().a
    print(t)
    return 0
""",
        "a" + chr(10) + "made" + chr(10),
    ),
    (
        "partial-move-box-field",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = box(P { a: "a".clone(), b: "b".clone() })
    let s = p.a
    print(s)
    print(p.b)
    return 0
""",
        "a" + chr(10) + "b" + chr(10),
    ),
    (
        "partial-move-enum-field",
        """
struct Holder:
    maybe: Option<String>
    other: String

fn main() -> i32:
    let h = Holder { maybe: Some("hello".clone()), other: "other".clone() }
    let s = h.maybe.unwrap()
    print(s)
    print(h.other)
    return 0
""",
        "hello" + chr(10) + "other" + chr(10),
    ),
    (
        "partial-move-assign-restore",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    var p = P { a: "a".clone(), b: "b".clone() }
    let s = p.a
    p.a = "new".clone()
    print(s)
    print(p.a)
    print(p.b)
    return 0
""",
        "a" + chr(10) + "new" + chr(10) + "b" + chr(10),
    ),
    (
        "drop-field-in-place",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    drop(p.a)
    print(p.b)
    return 0
""",
        "b" + chr(10),
    ),
    (
        "match-try-early-return",
        """
fn f() -> Option<i64>:
    let s = "hello".clone()
    let n = match s:
        "hello" -> Some(1)?
        _ -> 2
    return Some(n)

fn main() -> i32:
    print(f().unwrap())
    return 0
""",
        "1" + chr(10),
    ),
    (
        "match-all-literal-fields",
        """
enum E:
    A(String, String)
    B

fn main() -> i32:
    let e = E.A("x".clone(), "y".clone())
    let n = match e:
        E.A("x", "y") -> 1
        E.A(_, _) -> 2
        E.B -> 3
    print(n)
    return 0
""",
        "1" + chr(10),
    ),
    (
        "match-partial-literal-field",
        """
enum E:
    A(String, String)
    B

fn main() -> i32:
    let e = E.A("x".clone(), "y".clone())
    let n = match e:
        E.A(x, "y") -> len(x)
        E.A(_, _) -> 0
        E.B -> 3
    print(n)
    return 0
""",
        "1" + chr(10),
    ),
    (
        "match-string-literal",
        """
fn main() -> i32:
    let s = "hello".clone()
    let n = match s:
        "hello" -> 1
        _ -> 2
    print(n)
    return 0
""",
        "1" + chr(10),
    ),
    (
        "match-string-literal-unit",
        """
fn main() -> i32:
    let s = "hello".clone()
    match s:
        "hello" -> print("hit")
        _ -> print("miss")
    print("done")
    return 0
""",
        "hit" + chr(10) + "done" + chr(10),
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
    (
        "returnable-views",
        """
struct View:
    r: &i64
    n: i64

struct P:
    x: i64

impl P:
    fn view(&self) -> View:
        return View { r: &self.x, n: self.x }

fn make(x: &i64) -> View:
    let v = View { r: x, n: 7 }
    return v

fn main() -> i32:
    let p = P { x: 41 }
    let a = p.view()
    print(*a.r)
    print(a.n)
    let b = make(&p.x)
    print(*b.r)
    return 0
""",
        "41" + chr(10) + "41" + chr(10) + "41" + chr(10),
    ),
    (
        "supertrait-dispatch",
        """
trait Named:
    fn name(&self) -> String

trait Greet: Named:
    fn greet(&self) -> String:
        return "hello, " + self.name()

struct P:
    n: String

impl Named for P:
    fn name(&self) -> String:
        return self.n.clone()

impl Greet for P:
    pass

fn describe<T: Greet>(x: &T) -> String:
    return x.greet() + " / " + x.name()

fn main() -> i32:
    let p = P { n: "atrat" }
    print(describe(&p))
    let d: &dyn Greet = &p
    print(d.greet())
    let b: Box<dyn Greet> = box(P { n: "box" })
    print(b.name())
    return 0
""",
        "hello, atrat / atrat" + chr(10) + "hello, atrat" + chr(10)
        + "box" + chr(10),
    ),
    (
        "box-dyn-ownership",
        """
trait Counter:
    fn bump(&mut self) -> i64
    fn value(&self) -> i64

struct C:
    n: i64
    tag: String

impl Counter for C:
    fn bump(&mut self) -> i64:
        self.n += 1
        return self.n
    fn value(&self) -> i64:
        return self.n

fn main() -> i32:
    var c: Box<dyn Counter> = box(C { n: 0, tag: "c" })
    print(c.bump())
    print(c.bump())
    let moved = c
    print(moved.value())
    var list: List<Box<dyn Counter>> = [box(C { n: 10, tag: "x" })]
    list.push(box(C { n: 20, tag: "y" }))
    print((&list[0]).value())
    print((&list[1]).value())
    let p = &mut list[0]
    print(p.bump())
    return 0
""",
        "1" + chr(10) + "2" + chr(10) + "2" + chr(10) + "10" + chr(10)
        + "20" + chr(10) + "11" + chr(10),
    ),
    (
        "box-dyn-from-local",
        """
trait Show:
    fn show(&self) -> String

struct P:
    n: String

impl Show for P:
    fn show(&self) -> String:
        return self.n.clone()

fn make() -> Box<P>:
    return box(P { n: "made" })

fn make_dyn() -> Box<dyn Show>:
    return box(P { n: "dyn" })

fn main() -> i32:
    let b = box(P { n: "local" })
    let d: Box<dyn Show> = b
    print(d.show())
    let e: Box<dyn Show> = make()
    print(e.show())
    print(make_dyn().show())
    return 0
""",
        "local" + chr(10) + "made" + chr(10) + "dyn" + chr(10),
    ),
    (
        "pass-statement",
        """
fn main() -> i32:
    pass
    let x = 1
    if x == 1:
        pass
    else:
        print("no")
    print(x)
    return 0
""",
        "1" + chr(10),
    ),
    (
        "borrow-nested-scopes",
        """
struct P:
    x: i64
    s: String

fn main() -> i32:
    let p = P { x: 1, s: "p" }
    if true:
        let r = &p.x
        print(*r)
    for i in 0..1:
        let r = &p.x
        print(*r)
    let q = p
    print(q.x)
    return 0
""",
        "1" + chr(10) + "1" + chr(10) + "1" + chr(10),
    ),
    (
        "borrow-branches-and-loops",
        """
struct P:
    x: i64

fn main() -> i32:
    let p = P { x: 3 }
    let r = &p.x
    if true:
        print(*r)
    else:
        print(*r)
    for i in 0..2:
        print(*r)
    return 0
""",
        "3" + chr(10) + "3" + chr(10) + "3" + chr(10),
    ),
    (
        "supertrait-multi-bounds",
        """
trait A:
    fn a(&self) -> String
trait B:
    fn b(&self) -> String
trait C: A, B:
    fn c(&self) -> String:
        return self.a() + self.b()

struct P:
    n: String
impl A for P:
    fn a(&self) -> String:
        return "A" + self.n.clone()
impl B for P:
    fn b(&self) -> String:
        return "B" + self.n.clone()
impl C for P:
    pass

fn both<T: A + B>(x: &T) -> String:
    return x.a() + x.b()

fn main() -> i32:
    let p = P { n: "!" }
    print(both(&p))
    let d: &dyn C = &p
    print(d.c())
    let boxed: Box<dyn C> = box(P { n: "?" })
    print(boxed.c())
    return 0
""",
        "A!B!" + chr(10) + "A!B!" + chr(10) + "A?B?" + chr(10),
    ),
    (
        "trait-default-override",
        """
trait A:
    fn who(&self) -> String:
        return "A"
trait B: A:
    fn who(&self) -> String:
        return "B"
struct P:
    x: i64
impl A for P:
    pass
impl B for P:
    pass
fn main() -> i32:
    let p = P { x: 1 }
    let a: &dyn A = &p
    let b: &dyn B = &p
    let c: Box<dyn B> = box(P { x: 2 })
    print(a.who())
    print(b.who())
    print(c.who())
    return 0
""",
        "A" + chr(10) + "B" + chr(10) + "B" + chr(10),
    ),
    (
        "trait-object-destruction",
        """
trait Show:
    fn show(&self) -> String
struct H:
    r: Rc<i64>
impl Show for H:
    fn show(&self) -> String:
        return "h"
fn main() -> i32:
    let r = rc(7)
    let d: Box<dyn Show> = box(H { r: r.clone() })
    print(r.strong_count())
    drop(d)
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10),
    ),
    (
        "trait-nested-object",
        """
trait Show:
    fn show(&self) -> String
struct P:
    n: String
impl Show for P:
    fn show(&self) -> String:
        return self.n.clone()
fn main() -> i32:
    let d: Box<dyn Show> = box(P { n: "nested" })
    let r = &d
    print(r.show())
    let rr = &r
    print((*rr).show())
    return 0
""",
        "nested" + chr(10) + "nested" + chr(10),
    ),
    (
        'destruction-matrix',
        'struct Owned:\n    name: String\n    r: Rc<i64>\n    b: Box<i64>\n    extra: Option<Rc<i64>>\n\nfn early(flag: bool, r: Rc<i64>) -> i64:\n    let a: Option<Rc<i64>> = Some(r.clone())\n    let b: Option<Box<Rc<i64>>> = Some(box(r.clone()))\n    let c: Option<Result<Box<i64>, Rc<i64>>> = Some(Ok(box(1)))\n    if flag:\n        drop(a)\n        return 1\n    drop(b)\n    drop(c)\n    return 2\n\nfn make_nested(r: Rc<i64>) -> Option<Result<Box<i64>, Rc<i64>>>:\n    return Some(Ok(box(7)))\n\nfn use_nested(x: Option<Result<Box<i64>, Rc<i64>>>) -> i64:\n    let out = match x:\n        Some(Ok(b)) -> *b\n        Some(Err(e)) -> *e\n        None -> -1\n    return out\n\nfn main() -> i32:\n    let r = rc(5)\n    let h = Owned { name: "h".clone(), r: r.clone(), b: box(3), extra: Some(r.clone()) }\n    print(r.strong_count())\n    drop(h)\n    print(r.strong_count())\n\n    print(early(true, r.clone()))\n    print(r.strong_count())\n    print(early(false, r.clone()))\n    print(r.strong_count())\n\n    let n = make_nested(r.clone())\n    print(r.strong_count())\n    print(use_nested(n))\n    print(r.strong_count())\n\n    var i = 0\n    while i < 3:\n        let t: Option<Rc<i64>> = Some(r.clone())\n        if i == 1:\n            drop(t)\n        i += 1\n    print(r.strong_count())\n\n    let e: Result<i64, Rc<i64>> = Err(r.clone())\n    print(r.strong_count())\n    print(e.unwrap_or(0))\n    print(r.strong_count())\n\n    let eok: Result<Rc<i64>, String> = Ok(r.clone())\n    print(r.strong_count())\n    let oe = eok.err()\n    print(oe.is_none())\n    print(r.strong_count())\n    return 0\n',
        '3\n1\n1\n1\n2\n1\n1\n7\n1\n1\n2\n0\n1\n2\ntrue\n1\n',
    ),
    (
        'nested-match-codegen',
        'enum Inner:\n    A(i64)\n    B(i64)\n\nenum Outer:\n    S(Inner)\n    N\n\nfn main() -> i32:\n    let x = Outer.S(Inner.A(3))\n    let y = match x:\n        Outer.S(Inner.A(n)) -> n\n        Outer.S(Inner.B(n)) -> -n\n        Outer.N -> 0\n    print(y)\n\n    let z = Outer.S(Inner.B(4))\n    let w = match z:\n        Outer.S(Inner.A(n)) -> n\n        Outer.S(Inner.B(n)) -> -n\n        Outer.N -> 0\n    print(w)\n    return 0\n',
        '3\n-4\n',
    ),
    (
        'ownership-enum-transfer',
        'struct Holder:\n    name: String\n    r: Rc<i64>\n    b: Box<i64>\n    maybe: Option<Rc<i64>>\n\nfn make_opt_box(v: i64) -> Option<Box<i64>>:\n    return Some(box(v))\n\nfn consume_opt_box(o: Option<Box<i64>>) -> i64:\n    let out = match o:\n        Some(b) -> *b\n        None -> -1\n    return out\n\nfn pass_opt_box(o: Option<Box<i64>>) -> Option<Box<i64>>:\n    return o\n\nfn make_res_box(v: i64) -> Result<Box<i64>, String>:\n    if v < 0:\n        return Err("negative".clone())\n    return Ok(box(v))\n\nfn consume_res_box(r: Result<Box<i64>, String>) -> i64:\n    let out = match r:\n        Ok(b) -> *b\n        Err(e) -> -1\n    return out\n\nfn make_opt_rc(v: i64) -> Option<Rc<i64>>:\n    return Some(rc(v))\n\nfn main() -> i32:\n    let o = make_opt_box(11)\n    print(consume_opt_box(o))\n    let p = pass_opt_box(make_opt_box(12))\n    print(consume_opt_box(p))\n    print(consume_opt_box(None))\n\n    let r = make_res_box(13)\n    print(consume_res_box(r))\n    let bad = make_res_box(-1)\n    print(consume_res_box(bad))\n\n    let shared = rc(99)\n    let a = make_opt_rc(1)\n    let b: Option<Rc<i64>> = Some(shared.clone())\n    print(shared.strong_count())\n    drop(a)\n    print(shared.strong_count())\n    drop(b)\n    print(shared.strong_count())\n\n    let h = Holder { name: "h".clone(), r: shared.clone(), b: box(5), maybe: Some(shared.clone()) }\n    print(shared.strong_count())\n    drop(h)\n    print(shared.strong_count())\n    return 0\n',
        '11\n12\n-1\n13\n-1\n2\n2\n1\n3\n1\n',
    ),
    (
        'unwrap-err',
        'fn main() -> i32:\n    let r = rc(4)\n    let e: Result<i64, Rc<i64>> = Err(r.clone())\n    print(r.strong_count())\n    let got = e.unwrap_err()\n    print(r.strong_count())\n    print(*got)\n    drop(got)\n    print(r.strong_count())\n\n    let ok: Result<i64, String> = Ok(7)\n    print(ok.unwrap())\n    return 0\n',
        '2\n2\n4\n1\n7\n',
    ),
    (
        'box-string-methods',
        'fn main() -> i32:\n    let b: Box<String> = box("owned".clone())\n    print((*b).len())\n    print(len(*b))\n    print((*b).clone())\n    return 0\n',
        '5\n5\nowned\n',
    ),
    (
        'destruction-assignment',
        'struct Holder:\n    r: Rc<i64>\n    b: Box<i64>\n    maybe: Option<Rc<i64>>\n\nfn main() -> i32:\n    let base = rc(1)\n    let other = rc(2)\n    var h = Holder { r: base.clone(), b: box(9), maybe: Some(other.clone()) }\n    print(base.strong_count())\n    print(other.strong_count())\n    h.r = rc(3)\n    print(base.strong_count())\n    h.maybe = None\n    print(other.strong_count())\n    h.b = box(10)\n    print(*h.b)\n    print(*h.r)\n    drop(h)\n\n    var o: Option<Rc<i64>> = Some(other.clone())\n    print(other.strong_count())\n    o = None\n    print(other.strong_count())\n    o = Some(other.clone())\n    print(other.strong_count())\n    o = Some(rc(4))\n    print(other.strong_count())\n    print("done")\n    return 0\n',
        '2\n2\n1\n1\n10\n3\n2\n1\n2\n1\ndone\n',
    ),
    (
        'box-method-autoderef',
        'struct P:\n    n: i64\n    name: String\n\nimpl P:\n    fn get(&self) -> i64:\n        return self.n\n    fn bump(&mut self) -> i64:\n        self.n += 1\n        return self.n\n    fn name_of(&self) -> String:\n        return self.name.clone()\n\nfn main() -> i32:\n    var b: Box<P> = box(P { n: 7, name: "boxed".clone() })\n    print(b.get())\n    print(b.bump())\n    print(b.get())\n    print(b.name_of())\n    print(b.name)\n    print(b.name.len())\n\n    var l: Box<List<i64>> = box([1, 2])\n    print(l.len())\n    l.push(3)\n    print(l.pop().unwrap())\n\n    let s: Box<String> = box("hi".clone())\n    print(s.len())\n    print(s.clone())\n    return 0\n',
        '7\n8\n8\nboxed\nboxed\n5\n2\n3\n2\nhi\n',
    ),
    (
        'drop-temporary',
        'fn main() -> i32:\n    let r = rc(1)\n    drop(rc(2))\n    drop(box(3))\n    drop("temporary".clone())\n    print(r.strong_count())\n    print("ok")\n    return 0\n',
        '1\nok\n',
    ),
    (
        'len-owned-temporary',
        'fn main() -> i32:\n    print(len(to_str(12345)))\n    print(len("x".clone()))\n    return 0\n',
        '5\n1\n',
    ),
    (
        'temporary-aggregate-field',
        'struct P:\n    s: String\n\nfn main() -> i32:\n    print(P { s: "x".clone() }.s)\n    print(P { s: "y".clone() }.s.len())\n    print(P { s: "z".clone() }.s.clone())\n    return 0\n',
        'x\n1\nz\n',
    ),
    (
        "enum-method-payload-drop",
        """
fn main() -> i32:
    let r = rc(7)
    let e: Result<i64, Rc<i64>> = Err(r.clone())
    print(r.strong_count())
    print(e.unwrap_or(0))
    print(r.strong_count())

    let e2: Result<i64, Rc<i64>> = Err(r.clone())
    print(r.strong_count())
    let o = e2.ok()
    print(o.is_none())
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "0" + chr(10) + "1" + chr(10)
        + "2" + chr(10) + "true" + chr(10) + "1" + chr(10),
    ),
    (
        "rc-clone-count",
        """
fn main() -> i32:
    let r = rc(7)
    let a = r.clone()
    let b = r.clone()
    print(r.strong_count())
    drop(a)
    print(r.strong_count())
    drop(b)
    print(r.strong_count())
    return 0
""",
        "3" + chr(10) + "2" + chr(10) + "1" + chr(10),
    ),
    (
        "box-owns-rc",
        """
struct H:
    r: Rc<i64>
fn main() -> i32:
    let r = rc(7)
    let b = box(H { r: r.clone() })
    print(r.strong_count())
    drop(b)
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10),
    ),
    (
        "cleanup-early-return",
        """
struct H:
    r: Rc<i64>
fn early(r: Rc<i64>) -> i32:
    let h = H { r: r }
    if true:
        return 0
    return 1
fn main() -> i32:
    let r = rc(7)
    print(r.strong_count())
    early(r.clone())
    print(r.strong_count())
    return 0
""",
        "1" + chr(10) + "1" + chr(10),
    ),
    (
        "cleanup-nested-scopes",
        """
struct H:
    r: Rc<i64>
fn main() -> i32:
    let r = rc(7)
    if true:
        let h = H { r: r.clone() }
        print(r.strong_count())
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10),
    ),
    (
        "cleanup-conditional-branches",
        """
struct H:
    r: Rc<i64>
fn main() -> i32:
    let r = rc(7)
    if true:
        let h = H { r: r.clone() }
        print(r.strong_count())
    else:
        let h = H { r: r.clone() }
        print(r.strong_count())
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10),
    ),
    (
        "rc-drop-count",
        """
struct H:
    r: Rc<i64>
fn main() -> i32:
    let r = rc(7)
    let h = H { r: r.clone() }
    print(r.strong_count())
    drop(h)
    print(r.strong_count())
    return 0
""",
        "2" + chr(10) + "1" + chr(10),
    ),
    (
        "binary-borrow-operands",
        """
fn main() -> i32:
    let a = "hi".clone()
    let b = a + "!"
    print(a)
    print(b)
    return 0
""",
        "hi" + chr(10) + "hi!" + chr(10),
    ),
    (
        "field-assign-self-reference",
        """
struct P:
    s: String
fn main() -> i32:
    var p = P { s: "hi".clone() }
    p.s = p.s + "!"
    print(p.s)
    return 0
""",
        "hi!" + chr(10),
    ),
    (
        "while-condition-effects",
        """
fn check(i: i64) -> i64:
    print("check {i}")
    return i
fn main() -> i32:
    var i = 0
    while to_str(check(i)).len() >= 0:
        i += 1
        if i == 3:
            break
    return 0
""",
        "check 0" + chr(10) + "check 1" + chr(10) + "check 2" + chr(10),
    ),
    (
        "elif-condition-temp",
        """
fn main() -> i32:
    let x = 1
    if false:
        print("a")
    elif to_str(x).len() > 0:
        print("b")
    return 0
""",
        "b" + chr(10),
    ),
    (
        "min-max-abs-single-eval",
        """
fn side() -> i64:
    print("s")
    return 1
fn main() -> i32:
    print(max(side(), side()))
    print(abs(side()))
    return 0
""",
        "s" + chr(10) + "s" + chr(10) + "1" + chr(10) + "s" + chr(10) + "1" + chr(10),
    ),
    (
        "rc-borrow-method",
        """
fn main() -> i32:
    let a = rc("x".clone())
    let b = (&a).clone()
    print(a.strong_count())
    print(b.strong_count())
    return 0
""",
        "2" + chr(10) + "2" + chr(10),
    ),
    (
        "string-search-methods",
        """
fn main() -> i32:
    let s = "hello world".clone()
    print(s.starts_with("hello"))
    print(s.ends_with("world"))
    print(s.contains("lo w"))
    print(s.contains("xyz"))
    let needle = "world".clone()
    print(s.ends_with(needle))
    print(needle)
    return 0
""",
        "true" + chr(10) + "true" + chr(10) + "true" + chr(10) +
        "false" + chr(10) + "true" + chr(10) + "world" + chr(10),
    ),
    (
        "compound-index-once",
        """
fn idx() -> usize:
    print("i")
    return 1
fn main() -> i32:
    var xs = [1, 2, 3]
    xs[idx()] += 10
    print(xs[1])
    return 0
""",
        "i" + chr(10) + "12" + chr(10),
    ),
    (
        "short-circuit-lazy",
        """
struct P:
    n: i64

fn make() -> P:
    print("BAD")
    return P { n: 1 }

fn main() -> i32:
    print(false && make().n > 0)
    print(true || make().n > 0)
    if false && make().n > 0:
        print("unreachable")
    var i = 0
    while i < 1 && make().n > 0:
        i += 1
    print("DONE")
    return 0
""",
        "false" + chr(10) + "true" + chr(10) + "BAD" + chr(10) + "DONE" + chr(10),
    ),
    (
        "inclusive-range-max",
        """
fn main() -> i32:
    let lo: u8 = 0
    let hi: u8 = 255
    var n = 0
    for i in lo..=hi:
        n += 1
        if n > 300:
            break
    print(n)
    var odd = 0
    for i in lo..=hi:
        if i % 2 == 0:
            continue
        odd += 1
    print(odd)
    let ilo: i8 = -128
    let ihi: i8 = 127
    var signed = 0
    for i in ilo..=ihi:
        signed += 1
    print(signed)
    return 0
""",
        "256" + chr(10) + "128" + chr(10) + "256" + chr(10),
    ),
    (
        "cast-numeric-literals",
        """
fn main() -> i32:
    let a = 0 as u8
    let b = 1.5 as i64
    let c = 65 as u8
    print(a)
    print(b)
    print(c)
    return 0
""",
        "0" + chr(10) + "1" + chr(10) + "65" + chr(10),
    ),
    (
        "utf8-bom",
        "\ufefffn main() -> i32:\n    print(\"ok\")\n    return 0\n",
        "ok" + chr(10),
    ),
]

FAIL_CASES = [
    (
        "float-literal-out-of-range",
        'fn main() -> i32:\n    let x = 1e400\n    return 0\n',
        "lex error",
        "float literal is out of range",
    ),
    (
        "integer-literal-out-of-range",
        'fn main() -> i32:\n    let x = 18446744073709551616\n    return 0\n',
        "lex error",
        "integer literal is out of range for u64",
    ),
    (
        "recursive-type-infinite",
        'struct Node:\n    next: Option<Node>\n\nfn main() -> i32:\n    let n: Node\n    return 0\n',
        "type error",
        "infinite size",
    ),
    (
        "duplicate-field",
        'struct S:\n    x: i64\n    x: i64\n\nfn main() -> i32:\n    let s = S { x: 1 }\n    return 0\n',
        "type error",
        "duplicate field 'S.x'",
    ),
    (
        "duplicate-enum-variant",
        'enum E:\n    A\n    A\n\nfn main() -> i32:\n    let e = E.A\n    return 0\n',
        "type error",
        "duplicate variant 'E.A'",
    ),
    (
        "duplicate-generic-parameter",
        'struct S<T, T>:\n    x: T\n\nfn main() -> i32:\n    let s = S { x: 1 }\n    return 0\n',
        "type error",
        "duplicate generic parameter 'T'",
    ),
    (
        "duplicate-trait-parameter",
        'trait T:\n    fn f(&self, x: i64, x: i64) -> i64\n\nfn main() -> i32:\n    return 0\n',
        "type error",
        "duplicate parameter 'x'",
    ),
    (
        "partial-init-field",
        'struct P:\n    a: String\n    b: String\n\nfn main() -> i32:\n    var p: P\n    p.a = "x".clone()\n    p.b = "y".clone()\n    print(p.a)\n    return 0\n',
        "borrow error",
        "before it is initialized",
    ),
    (
        "partial-init-drop",
        'struct P:\n    s: String\n\nfn main() -> i32:\n    let p: P\n    drop(p)\n    return 0\n',
        "borrow error",
        "before it is initialized",
    ),
    (
        "box-unwrap-noncopy",
        'fn main() -> i32:\n    let b: Box<Option<String>> = box(Some("x".clone()))\n    print(b.unwrap())\n    return 0\n',
        "borrow error",
        "cannot move a non-Copy value out of a Box",
    ),
    (
        "temporary-box-method",
        'struct P:\n    x: i64\n\nimpl P:\n    fn get(&self) -> i64:\n        return self.x\n\nfn main() -> i32:\n    print(box(P { x: 1 }).get())\n    return 0\n',
        "type error",
        "temporary `Box<P>` used for method access",
    ),
    (
        "temporary-box-field",
        'struct P:\n    x: i64\n\nfn main() -> i32:\n    print(box(P { x: 1 }).x)\n    return 0\n',
        "type error",
        "temporary `Box<P>` used for field access",
    ),
    (
        "temporary-box-deref",
        'struct P:\n    x: i64\n\nfn main() -> i32:\n    print((*box(P { x: 5 })).x)\n    return 0\n',
        "type error",
        "temporary `Box<P>` used for dereference",
    ),
    (
        "drop-rc-field-rejected",
        """
struct P:
    s: String

fn main() -> i32:
    let p = rc(P { s: "hello".clone() })
    drop(p.s)
    return 0
""",
        "borrow error",
        "cannot drop a field through `Rc`",
    ),
    (
        "drop-deref-shared-field-rejected",
        """
struct P:
    s: String

fn main() -> i32:
    let p = P { s: "hello".clone() }
    let r = &p
    drop((*r).s)
    return 0
""",
        "borrow error",
        "cannot drop a field through a shared reference",
    ),
    (
        "drop-shared-field-rejected",
        """
struct P:
    s: String

fn main() -> i32:
    let p = P { s: "hello".clone() }
    let r = &p
    drop(r.s)
    return 0
""",
        "borrow error",
        "cannot drop a field through a shared reference",
    ),
    (
        "partial-move-loop",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    for i in 0..2:
        let s = p.a
        print(s)
    return 0
""",
        "borrow error",
        "moved inside a loop",
    ),
    (
        "partial-move-method-read",
        """
struct Holder:
    maybe: Option<String>
    other: String

fn main() -> i32:
    let h = Holder { maybe: Some("hello".clone()), other: "other".clone() }
    let s = h.maybe.unwrap()
    print(h.maybe.is_some())
    return 0
""",
        "borrow error",
        "use of moved field `h.maybe`",
    ),
    (
        "move-moved-field",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    let s = p.a
    print(p.a)
    return 0
""",
        "borrow error",
        "use of moved field `p.a`",
    ),
    (
        "move-whole-partial",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    let s = p.a
    let q = p
    print(q.b)
    return 0
""",
        "borrow error",
        "use of partially moved value `p`",
    ),
    (
        "move-rc-field",
        """
struct P:
    a: String

fn main() -> i32:
    let p = rc(P { a: "a".clone() })
    let s = p.a
    print(s)
    return 0
""",
        "borrow error",
        "cannot move a non-Copy field out of an `Rc` payload",
    ),
    (
        "move-shared-field",
        """
struct P:
    a: String

fn main() -> i32:
    let p = P { a: "a".clone() }
    let r = &p
    let s = r.a
    print(s)
    return 0
""",
        "borrow error",
        "because it is borrowed",
    ),
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
    (
        "view-local-escape",
        """
struct View:
    r: &i64

fn bad(x: &i64) -> View:
    let y = 1
    return View { r: &y }

fn main() -> i32:
    return 0
""",
        "error[E0301]",
        "returned borrow must be derived from",
    ),
    (
        "supertrait-missing-parent-impl",
        """
trait Named:
    fn name(&self) -> String

trait Greet: Named:
    fn greet(&self) -> String

struct P:
    n: String

impl Greet for P:
    fn greet(&self) -> String:
        return "hi"

fn describe<T: Greet>(x: &T) -> String:
    return x.greet()

fn main() -> i32:
    let p = P { n: "x" }
    print(describe(&p))
    return 0
""",
        "type error",
        "does not implement trait `Named`",
    ),
    (
        "unknown-supertrait",
        """
trait Child: Missing:
    fn f(&self) -> i64

fn main() -> i32:
    return 0
""",
        "type error",
        "unknown supertrait",
    ),
    (
        "box-dyn-coerced-move",
        """
trait Show:
    fn show(&self) -> String

struct P:
    n: String

impl Show for P:
    fn show(&self) -> String:
        return self.n.clone()

fn main() -> i32:
    let b = box(P { n: "x" })
    let d: Box<dyn Show> = b
    let c = b
    print(d.show())
    return 0
""",
        "borrow error",
        "use of moved value",
    ),
    (
        "box-dyn-use-after-move",
        """
trait Show:
    fn show(&self) -> String

struct P:
    n: String

impl Show for P:
    fn show(&self) -> String:
        return self.n.clone()

fn main() -> i32:
    let d: Box<dyn Show> = box(P { n: "x" })
    let e = d
    print(d.show())
    return 0
""",
        "borrow error",
        "use of moved value",
    ),
    (
        "field-ref-conflict",
        """
struct S:
    x: i64
struct H:
    v: &mut S
struct View:
    r: &i64
struct P:
    s: S
    t: String
impl P:
    fn view(&self) -> View:
        return View { r: &self.s.x }
fn main() -> i32:
    var p = P { s: S { x: 1 }, t: "p" }
    let h = H { v: &mut p.s }
    let w = p.view()
    *h.v = S { x: 2 }
    print(*w.r)
    return 0
""",
        "error[E0304]",
        "cannot read `p` while it is mutably borrowed",
    ),
    (
        "box-ref-owner-drop",
        """
struct P:
    x: i64
    s: String
fn main() -> i32:
    let b = box(P { x: 1, s: "p" })
    let r = &(*b).x
    drop(b)
    print(*r)
    return 0
""",
        "error[E0302]",
        "cannot move `b` while it is borrowed",
    ),
    (
        "box-ref-owner-move",
        """
struct P:
    x: i64
    s: String
fn main() -> i32:
    let b = box(P { x: 1, s: "p" })
    let r = &(*b).x
    let c = b
    print(*r)
    return 0
""",
        "borrow error",
        "cannot move `b` while it is borrowed",
    ),
    (
        "rc-ref-owner-drop",
        """
struct P:
    x: i64
    s: String
fn main() -> i32:
    let b = rc(P { x: 1, s: "p" })
    let r = &(*b).x
    drop(b)
    print(*r)
    return 0
""",
        "borrow error",
        "cannot move `b` while it is borrowed",
    ),
    (
        "box-deref-assign-borrowed",
        """
struct P:
    x: i64
    s: String
fn main() -> i32:
    var b = box(P { x: 1, s: "p" })
    let r = &(*b).x
    *b = P { x: 2, s: "q" }
    print(*r)
    return 0
""",
        "borrow error",
        "cannot assign through `b` while it is borrowed",
    ),
    (
        "view-multiple-sources",
        """
struct View:
    r: &i64
fn bad(a: &i64, b: &i64) -> View:
    return View { r: b }
fn main() -> i32:
    return 0
""",
        "type error",
        "reference parameters",
    ),
    (
        "view-temporary",
        """
struct P:
    x: i64
struct View:
    r: &i64
fn bad(x: &i64) -> View:
    return View { r: &P { x: 1 }.x }
fn main() -> i32:
    return 0
""",
        "type error",
        "cannot borrow a temporary",
    ),
    (
        "drop-owner-with-view",
        """
struct P:
    x: i64
    s: String
struct View:
    r: &i64
impl P:
    fn view(&self) -> View:
        return View { r: &self.x }
fn main() -> i32:
    let p = P { x: 1, s: "p" }
    let v = p.view()
    drop(p)
    print(*v.r)
    return 0
""",
        "borrow error",
        "cannot move `p` while it is borrowed",
    ),
    (
        "move-owner-with-view",
        """
struct P:
    x: i64
    s: String
struct View:
    r: &i64
impl P:
    fn view(&self) -> View:
        return View { r: &self.x }
fn main() -> i32:
    let p = P { x: 1, s: "p" }
    let v = p.view()
    let q = p
    print(*v.r)
    return 0
""",
        "borrow error",
        "cannot move `p` while it is borrowed",
    ),
    (
        "mutable-then-shared",
        """
struct P:
    x: i64
fn main() -> i32:
    var p = P { x: 1 }
    let a = &mut p.x
    let b = &p.x
    print(*b)
    return 0
""",
        "error[E0304]",
        "cannot read `p` while it is mutably borrowed",
    ),
    (
        "shared-then-mutable",
        """
struct P:
    x: i64
fn main() -> i32:
    var p = P { x: 1 }
    let a = &p.x
    let b = &mut p.x
    print(*b)
    return 0
""",
        "borrow error",
        "cannot borrow `p` as mutable while it is already borrowed",
    ),
    (
        "branch-mutate-view",
        """
struct P:
    x: i64
    s: String
struct View:
    r: &i64
impl P:
    fn view(&self) -> View:
        return View { r: &self.x }
fn main() -> i32:
    var p = P { x: 1, s: "p" }
    let v = p.view()
    if true:
        p.x = 2
    print(*v.r)
    return 0
""",
        "borrow error",
        "cannot assign through `p` while it is borrowed",
    ),
    (
        "loop-mutate-view",
        """
struct P:
    x: i64
    s: String
struct View:
    r: &i64
impl P:
    fn view(&self) -> View:
        return View { r: &self.x }
fn main() -> i32:
    var p = P { x: 1, s: "p" }
    let v = p.view()
    var i = 0
    while i < 1:
        p.x = 2
        i += 1
    print(*v.r)
    return 0
""",
        "borrow error",
        "cannot assign through `p` while it is borrowed",
    ),
    (
        "nested-view-escape",
        """
struct P:
    x: i64
    s: String
struct View:
    r: &i64
fn main() -> i32:
    var p = P { x: 1, s: "p" }
    var v = View { r: &p.x }
    if true:
        let q = P { x: 2, s: "q" }
        v = View { r: &q.x }
    print(*v.r)
    return 0
""",
        "borrow error",
        "cannot assign to a place that stores a reference yet",
    ),
    (
        "ambiguous-supertrait-method",
        """
trait A:
    fn f(&self) -> i64
trait B:
    fn f(&self) -> i64
trait C: A, B:
    fn c(&self) -> i64
fn main() -> i32:
    return 0
""",
        "type error",
        "inherits ambiguous method",
    ),
    (
        "trait-method-collision",
        """
trait A:
    fn f(&self) -> i64
trait B:
    fn f(&self) -> i64
struct P:
    x: i64
impl A for P:
    fn f(&self) -> i64:
        return 1
impl B for P:
    fn f(&self) -> i64:
        return 2
fn main() -> i32:
    return 0
""",
        "type error",
        "defined twice",
    ),
    (
        "duplicate-trait-impl",
        """
trait Show:
    fn show(&self) -> String
struct P:
    x: i64
impl Show for P:
    fn show(&self) -> String:
        return "a"
impl Show for P:
    fn show(&self) -> String:
        return "b"
fn main() -> i32:
    return 0
""",
        "error[E0401]",
        "already implements",
    ),
    (
        "dyn-by-value",
        """
trait Show:
    fn show(&self) -> String
struct Bad:
    d: dyn Show
fn main() -> i32:
    return 0
""",
        "error[E0308]",
        "stores a trait object by value",
    ),
    (
        "trait-object-wrong-mutability",
        """
trait Show:
    fn show(&self) -> String
struct P:
    x: i64
impl Show for P:
    fn show(&self) -> String:
        return "p"
fn main() -> i32:
    let p = P { x: 1 }
    let d: &mut dyn Show = &p
    return 0
""",
        "type error",
        "mismatched reference mutability",
    ),
    (
        "borrow-temp-field",
        """
struct P:
    x: i64
fn make() -> P:
    return P { x: 1 }
fn main() -> i32:
    let r = &make().x
    print(*r)
    return 0
""",
        "type error",
        "cannot borrow a temporary",
    ),
    (
        "borrow-temp-index",
        """
fn main() -> i32:
    let r = &[1, 2, 3][0]
    print(*r)
    return 0
""",
        "type error",
        "cannot borrow a temporary",
    ),
    (
        "box-payload-use-after-move",
        """
fn main() -> i32:
    let s = "x".clone()
    let b = box(s)
    print(s)
    return 0
""",
        "borrow error",
        "use of moved value",
    ),
    (
        "result-ok-from-ref",
        """
fn main() -> i32:
    let r: Result<String, i64> = Ok("x".clone())
    let o = (&r).ok()
    return 0
""",
        "type error",
        "cannot move a non-Copy value",
    ),
    (
        "while-condition-move",
        """
fn take(s: String) -> String:
    return s
fn main() -> i32:
    let s = "x".clone()
    var i = 0
    while take(s).len() > 0:
        i += 1
        if i > 1:
            break
    return 0
""",
        "borrow error",
        "moved inside a loop",
    ),
    (
        "number-separator-misplaced",
        """
fn main() -> i32:
    print(1__2)
    return 0
""",
        "lex error",
        "misplaced '_'",
    ),
    (
        "surrogate-escape",
        """
fn main() -> i32:
    print('\\uD800')
    return 0
""",
        "lex error",
        "surrogate code point",
    ),
]


PANIC_CASES = [
    (
        "panic-unwrap-none",
        """
fn main() -> i32:
    let o: Option<Box<String>> = None
    let b = o.unwrap()
    print("unreachable {b}")
    return 0
""",
        "called unwrap on None",
    ),
    (
        "panic-assert",
        """
fn main() -> i32:
    assert(false, "boom")
    print("unreachable")
    return 0
""",
        "boom",
    ),
    (
        "panic-div-zero",
        """
fn main() -> i32:
    let z = 0
    print(10 / z)
    print("unreachable")
    return 0
""",
        "integer division by zero",
    ),
    (
        "panic-cleanup",
        """
struct Holder:
    name: String
    boxed: Box<String>

fn main() -> i32:
    let a = "alpha".clone()
    let b = box("beta".clone())
    let c = rc("gamma".clone())
    let h = Holder { name: "holder".clone(), boxed: box("payload".clone()) }
    let l = ["one".clone(), "two".clone()]
    panic("cleanup boom")
    print("unreachable")
    return 0
""",
        "cleanup boom",
    ),
    (
        "panic-nested-cleanup",
        """
struct Holder:
    name: String
    boxed: Box<String>

fn inner(h: Holder, s: String) -> i32:
    let l = ["inner".clone()]
    panic("nested boom")
    return 0

fn main() -> i32:
    let h = Holder { name: "outer".clone(), boxed: box("payload".clone()) }
    let s = "argument".clone()
    inner(h, s)
    print("unreachable")
    return 0
""",
        "nested boom",
    ),
    (
        "panic-match-move",
        """
fn consume(s: String) -> i32:
    panic("match boom")
    return 0

fn main() -> i32:
    let x = Some("payload".clone())
    let n = match x:
        Some(v) -> consume(v)
        None -> 0
    print("unreachable {n}")
    return 0
""",
        "match boom",
    ),
    (
        "panic-loop-cleanup",
        """
fn main() -> i32:
    for i in 0..=2:
        let s = "loop".clone()
        if i == 1:
            panic("loop boom")
    print("unreachable")
    return 0
""",
        "loop boom",
    ),
    (
        "panic-partial-move",
        """
struct P:
    a: String
    b: String

fn main() -> i32:
    let p = P { a: "a".clone(), b: "b".clone() }
    let s = p.a
    panic("partial boom")
    print("unreachable {s}")
    return 0
""",
        "partial boom",
    ),
    (
        "panic-match-literal",
        """
fn main() -> i32:
    let s = "payload".clone()
    match s:
        "payload" -> panic("literal match boom")
        _ -> print("no")
    print("unreachable")
    return 0
""",
        "literal match boom",
    ),
    (
        "panic-result-err",
        """
fn main() -> i32:
    let r: Result<i32, String> = Err("bad".clone())
    let x = r.unwrap()
    print("unreachable {x}")
    return 0
""",
        "called unwrap on an Err value",
    ),
]

MULTI_CASES = [
    (
        "modules",
        {
            "sub/math.atr": """
pub fn add(a: i64, b: i64) -> i64:
    return a + b
""",
            "util.atr": """
import "sub/math"

pub struct Pair:
    a: i64
    b: i64

pub fn greet(name: str) -> String:
    return "hello, {name}"

pub fn answer() -> i64:
    return add(40, 2)
""",
            "main.atr": """
import "util"

fn main() -> i32:
    print(greet("atrat"))
    print(answer())
    let p = Pair { a: 20, b: 22 }
    print(p.a + p.b)
    return 0
""",
        },
        "main.atr",
        "hello, atrat\n42\n42\n",
    ),
    (
        "module-qualified",
        {
            "util.atr": """
pub struct Pair:
    a: i64
    b: i64

pub fn greet(name: str) -> String:
    return "hello, {name}"

fn hidden() -> i64:
    return 9
""",
            "main.atr": """
import "util" as u

fn main() -> i32:
    print(u.greet("atrat"))
    let p: u.Pair = u.Pair { a: 20, b: 22 }
    print(p.a + p.b)
    return 0
""",
        },
        "main.atr",
        "hello, atrat\n42\n",
    ),
    (
        "module-duplicate-names",
        {
            "a.atr": """
pub fn name() -> String:
    return "a"

pub struct Item:
    value: i64
""",
            "b.atr": """
pub fn name() -> String:
    return "b"

pub struct Item:
    value: i64
""",
            "main.atr": """
import "a" as a
import "b" as b

fn main() -> i32:
    print(a.name())
    print(b.name())
    let x: a.Item = a.Item { value: 1 }
    let y: b.Item = b.Item { value: 2 }
    print(x.value + y.value)
    return 0
""",
        },
        "main.atr",
        "a\nb\n3\n",
    ),
    (
        "module-qualified-enum",
        {
            "color.atr": """
pub enum Color:
    Red
    Green
    Custom(i64)
""",
            "main.atr": """
import "color" as c

fn label(x: c.Color) -> String:
    match x:
        c.Color.Red -> "red"
        c.Color.Green -> "green"
        c.Color.Custom(v) -> "custom"

fn main() -> i32:
    let a = c.Color.Red
    let b = c.Color.Custom(7)
    print(label(a))
    print(label(b))
    return 0
""",
        },
        "main.atr",
        "red\ncustom\n",
    ),
    (
        "module-qualified-trait",
        {
            "show.atr": """
pub trait Show:
    fn show(&self) -> String
""",
            "main.atr": """
import "show" as s

struct Point:
    x: i64

impl s.Show for Point:
    fn show(&self) -> String:
        return "point"

fn render<T: s.Show>(v: &T) -> String:
    return v.show()

fn dyn_render(v: &dyn s.Show) -> String:
    return v.show()

fn main() -> i32:
    let p = Point { x: 1 }
    print(render(&p))
    print(dyn_render(&p))
    let b: Box<dyn s.Show> = box(Point { x: 2 })
    print(b.show())
    return 0
""",
        },
        "main.atr",
        "point\npoint\npoint\n",
    ),
    (
        "module-default-alias",
        {
            "util.atr": """
pub fn greet() -> String:
    return "hi"
""",
            "main.atr": """
import "util"

fn main() -> i32:
    print(greet())
    print(util.greet())
    return 0
""",
        },
        "main.atr",
        "hi\nhi\n",
    ),
]

MULTI_FAIL_CASES = [
    (
        "private-function",
        {
            "helper.atr": """
fn hidden() -> i64:
    return 9
""",
            "main.atr": """
import "helper"

fn main() -> i32:
    print(hidden())
    return 0
""",
        },
        "main.atr",
        "type error",
        "private to module 'helper.atr'",
    ),
    (
        "private-struct",
        {
            "shapes.atr": """
struct Point:
    x: i64
""",
            "main.atr": """
import "shapes"

fn main() -> i32:
    let p = Point { x: 1 }
    print(p.x)
    return 0
""",
        },
        "main.atr",
        "type error",
        "private to module 'shapes.atr'",
    ),
    (
        "exposed-private-type",
        {
            "secret.atr": """
struct Hidden:
    x: i64

pub fn hidden() -> Hidden:
    return Hidden { x: 1 }
""",
            "main.atr": """
import "secret"

fn main() -> i32:
    let h = hidden()
    print(h.x)
    return 0
""",
        },
        "main.atr",
        "type error",
        "exposes private type 'Hidden'",
    ),
    (
        "qualified-private",
        {
            "util.atr": """
pub fn shown() -> i64:
    return 1

fn hidden() -> i64:
    return 2
""",
            "main.atr": """
import "util" as u

fn main() -> i32:
    print(u.shown())
    print(u.hidden())
    return 0
""",
        },
        "main.atr",
        "type error",
        "private to module 'util.atr'",
    ),
    (
        "alias-only",
        {
            "util.atr": """
pub fn shown() -> i64:
    return 1
""",
            "main.atr": """
import "util" as u

fn main() -> i32:
    print(shown())
    return 0
""",
        },
        "main.atr",
        "type error",
        "unknown function 'shown'",
    ),
    (
        "ambiguous-import",
        {
            "a.atr": """
pub fn name() -> String:
    return "a"
""",
            "b.atr": """
pub fn name() -> String:
    return "b"
""",
            "main.atr": """
import "a"
import "b"

fn main() -> i32:
    print(name())
    return 0
""",
        },
        "main.atr",
        "type error",
        "is ambiguous between a.atr, b.atr",
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

    for name, files, main_file, kind, needle in MULTI_FAIL_CASES:
        total += 1
        d = os.path.join(tmp, name)
        os.makedirs(d, exist_ok=True)
        for rel, src in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(src)
        proc = run([sys.executable, ATRC, "check",
                    os.path.join(d, main_file)])
        combined = proc.stdout + proc.stderr
        if proc.returncode == 0:
            print(f"FAIL {name}: expected visibility rejection, but it type-checked")
            failed += 1
        elif kind not in combined or needle not in combined:
            print(f"FAIL {name}: wrong visibility diagnostic")
            print(combined)
            failed += 1
        else:
            print(f"ok   {name} (private rejected)")

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
            print(f"FAIL {name}: wrong diagnostic")
            print(combined)
            failed += 1
        else:
            print(f"ok   {name} (rejected)")

    for name, src, needle in PANIC_CASES:
        total += 1
        path = os.path.join(tmp, "panic_" + name + ".atr")
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        exe = os.path.join(tmp, "panic_" + name + (".exe" if os.name == "nt" else ""))
        built = run([sys.executable, ATRC, "build", path, "-o", exe])
        if built.returncode != 0:
            print(f"FAIL {name}: build failed")
            print(built.stderr)
            failed += 1
            continue
        proc = run([exe])
        combined = proc.stdout + proc.stderr
        sanitizer_bad = any(marker in combined for marker in (
            "AddressSanitizer", "LeakSanitizer", "runtime error:",
            "SEGV", "heap corruption"))
        if (proc.returncode != 101 or needle not in combined
                or "unreachable" in proc.stdout or sanitizer_bad):
            print(f"FAIL {name}: expected clean panic cleanup, got exit "
                  f"{proc.returncode}")
            print(f"  stdout: {proc.stdout!r}")
            print(f"  stderr: {proc.stderr!r}")
            failed += 1
        else:
            print(f"ok   {name} (panic cleaned up, exit 101)")

    print()
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
