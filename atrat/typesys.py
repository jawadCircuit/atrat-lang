"""Type representations, unification, and printing for Atrat."""
from __future__ import annotations
from dataclasses import dataclass, field

INT_TYPES = {"i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64",
             "isize", "usize", "int", "uint"}
FLOAT_TYPES = {"f32", "f64"}
NUM_TYPES = INT_TYPES | FLOAT_TYPES
SIGNED = {"i8", "i16", "i32", "i64", "isize"}
PRIMS = NUM_TYPES | {"bool", "char", "str", "String", "unit"}


class Type:
    def __repr__(self) -> str:
        return self.show()


@dataclass(frozen=False)
class TCon(Type):
    name: str
    args: list = field(default_factory=list)

    def show(self) -> str:
        if self.args:
            return f"{self.name}<{', '.join(a.show() for a in self.args)}>"
        return self.name


@dataclass(frozen=False)
class TVar(Type):
    id: int
    hint: str = ""
    cons: frozenset = frozenset()

    def show(self) -> str:
        if self.hint:
            return self.hint
        return f"T{self.id}"


@dataclass(frozen=False)
class TRef(Type):
    mut: bool
    inner: Type

    def show(self) -> str:
        return ("&mut " if self.mut else "&") + self.inner.show()


@dataclass(frozen=False)
class TPtr(Type):
    mut: bool
    inner: Type

    def show(self) -> str:
        return ("*mut " if self.mut else "*") + self.inner.show()


@dataclass(frozen=False)
class TFn(Type):
    params: list
    ret: Type

    def show(self) -> str:
        return "fn(" + ", ".join(p.show() for p in self.params) + ") -> " + self.ret.show()


@dataclass
class Scheme:
    vars: list          # list[int] of TVar ids
    type: Type


UNIT = TCon("unit")
BOOL = TCon("bool")
STR = TCon("str")
STRING = TCon("String")
I64 = TCon("i64")
USIZE = TCon("usize")


class UnifyError(Exception):
    def __init__(self, msg, a=None, b=None):
        super().__init__(msg)
        self.msg = msg
        self.a = a
        self.b = b


class Unifier:
    def __init__(self):
        self.subst: dict[int, Type] = {}
        self.next_var = 0

    def fresh(self, hint: str = "", cons: frozenset = frozenset()) -> TVar:
        v = TVar(self.next_var, hint, cons)
        self.next_var += 1
        return v

    def resolve(self, t: Type) -> Type:
        while isinstance(t, TVar) and t.id in self.subst:
            t = self.subst[t.id]
        return t

    def resolve_deep(self, t: Type) -> Type:
        t = self.resolve(t)
        if isinstance(t, TCon):
            return TCon(t.name, [self.resolve_deep(a) for a in t.args])
        if isinstance(t, TRef):
            return TRef(t.mut, self.resolve_deep(t.inner))
        if isinstance(t, TPtr):
            return TPtr(t.mut, self.resolve_deep(t.inner))
        if isinstance(t, TFn):
            return TFn([self.resolve_deep(p) for p in t.params], self.resolve_deep(t.ret))
        return t

    def occurs(self, var_id: int, t: Type) -> bool:
        t = self.resolve(t)
        if isinstance(t, TVar):
            return t.id == var_id
        if isinstance(t, TCon):
            return any(self.occurs(var_id, a) for a in t.args)
        if isinstance(t, (TRef, TPtr)):
            return self.occurs(var_id, t.inner)
        if isinstance(t, TFn):
            return any(self.occurs(var_id, p) for p in t.params) or self.occurs(var_id, t.ret)
        return False

    def unify_ok(self, a: Type, b: Type) -> bool:
        snapshot = dict(self.subst)
        try:
            self.unify(a, b)
            return True
        except UnifyError:
            self.subst = snapshot
            return False

    def unify(self, a: Type, b: Type) -> None:
        a = self.resolve(a)
        b = self.resolve(b)

        if isinstance(a, TVar) and isinstance(b, TVar) and a.id == b.id:
            return

        if isinstance(a, TVar):
            self._bind(a, b)
            return
        if isinstance(b, TVar):
            self._bind(b, a)
            return

        if isinstance(a, TCon) and isinstance(b, TCon):
            if a.name != b.name or len(a.args) != len(b.args):
                raise UnifyError("mismatched types", a, b)
            for x, y in zip(a.args, b.args):
                self.unify(x, y)
            return

        if isinstance(a, TRef) and isinstance(b, TRef):
            if a.mut != b.mut and not (a.mut and not b.mut):
                raise UnifyError("mismatched reference mutability", a, b)
            self.unify(a.inner, b.inner)
            return

        if isinstance(a, TPtr) and isinstance(b, TPtr):
            if a.mut != b.mut:
                raise UnifyError("mismatched pointer mutability", a, b)
            self.unify(a.inner, b.inner)
            return

        if isinstance(a, TFn) and isinstance(b, TFn):
            if len(a.params) != len(b.params):
                raise UnifyError("mismatched function arity", a, b)
            for x, y in zip(a.params, b.params):
                self.unify(x, y)
            self.unify(a.ret, b.ret)
            return

        raise UnifyError("mismatched types", a, b)

    def _bind(self, var: TVar, t: Type) -> None:
        t = self.resolve(t)
        if isinstance(t, TVar) and t.id == var.id:
            return
        if self.occurs(var.id, t):
            raise UnifyError(f"cannot construct infinite type: {var.show()} = {t.show()}", var, t)

        t = self.resolve(t)
        if var.cons and isinstance(t, TVar):
            merged = _merge_cons(var.cons, t.cons)
            if merged is None:
                raise UnifyError("incompatible numeric constraints", var, t)
            t.cons = merged
        elif var.cons and isinstance(t, TCon):
            if not _con_ok(var.cons, t.name):
                raise UnifyError("mismatched types", var, t)
        elif var.cons and isinstance(t, TRef):
            raise UnifyError("mismatched types", var, t)

        if isinstance(t, TVar) and t.cons and not var.cons and not isinstance(t, TCon):
            pass
        self.subst[var.id] = t

    def instantiate(self, scheme: Scheme) -> tuple[Type, dict[int, TVar]]:
        mapping = {}
        for vid in scheme.vars:
            mapping[vid] = self.fresh()
        return self._clone(scheme.type, mapping), mapping

    def _clone(self, t: Type, mapping: dict[int, TVar]) -> Type:
        t = self.resolve(t)
        if isinstance(t, TVar):
            if t.id in mapping:
                return mapping[t.id]
            return t
        if isinstance(t, TCon):
            return TCon(t.name, [self._clone(a, mapping) for a in t.args])
        if isinstance(t, TRef):
            return TRef(t.mut, self._clone(t.inner, mapping))
        if isinstance(t, TPtr):
            return TPtr(t.mut, self._clone(t.inner, mapping))
        if isinstance(t, TFn):
            return TFn([self._clone(p, mapping) for p in t.params],
                       self._clone(t.ret, mapping))
        return t

    def default(self, t: Type, protect: set[int] | None = None) -> Type:
        protect = protect or set()
        t = self.resolve_deep(t)
        if isinstance(t, TVar):
            if t.id in protect:
                return t
            if "unsigned" in t.cons:
                return TCon("u64")
            if "int" in t.cons:
                return I64
            if "float" in t.cons:
                return TCon("f64")
            return t
        if isinstance(t, TCon):
            return TCon(t.name, [self.default(a, protect) for a in t.args])
        if isinstance(t, TRef):
            return TRef(t.mut, self.default(t.inner, protect))
        if isinstance(t, TPtr):
            return TPtr(t.mut, self.default(t.inner, protect))
        if isinstance(t, TFn):
            return TFn([self.default(p, protect) for p in t.params],
                       self.default(t.ret, protect))
        return t


def _merge_cons(a: frozenset, b: frozenset) -> frozenset | None:
    if not a:
        return b
    if not b:
        return a
    if a == b:
        return a
    if "unsigned" in a or "unsigned" in b:
        # A literal above i64::MAX can only become u64/usize, never a float.
        if "float" in a or "float" in b:
            return None
        return frozenset({"int", "unsigned"})
    if {"int", "float"} <= (set(a) | set(b)):
        # int literal + float literal -> must be a float type
        return frozenset({"float"})
    return None


def _con_ok(cons: frozenset, name: str) -> bool:
    if "unsigned" in cons:
        return name in ("u64", "usize", "uint")
    if "int" in cons:
        return name in NUM_TYPES
    if "float" in cons:
        return name in FLOAT_TYPES
    return True


def is_numeric(t: Type) -> bool:
    return isinstance(t, TCon) and t.name in NUM_TYPES


def is_int(t: Type) -> bool:
    return isinstance(t, TCon) and t.name in INT_TYPES


def is_float(t: Type) -> bool:
    return isinstance(t, TCon) and t.name in FLOAT_TYPES


def tcon(name: str, *args) -> TCon:
    return TCon(name, list(args))


def subst(t: Type, mapping: dict) -> Type:
    """Substitute type-parameter names (str) with concrete types."""
    if isinstance(t, TVar):
        return mapping.get(t.hint, t) if t.hint else t
    if isinstance(t, TCon):
        return TCon(t.name, [subst(a, mapping) for a in t.args])
    if isinstance(t, TRef):
        return TRef(t.mut, subst(t.inner, mapping))
    if isinstance(t, TPtr):
        return TPtr(t.mut, subst(t.inner, mapping))
    if isinstance(t, TFn):
        return TFn([subst(p, mapping) for p in t.params], subst(t.ret, mapping))
    return t


def free_vars(t: Type, acc: set | None = None) -> set:
    acc = acc if acc is not None else set()
    if isinstance(t, TVar):
        acc.add(t.id)
    elif isinstance(t, TCon):
        for a in t.args:
            free_vars(a, acc)
    elif isinstance(t, (TRef, TPtr)):
        free_vars(t.inner, acc)
    elif isinstance(t, TFn):
        for p in t.params:
            free_vars(p, acc)
        free_vars(t.ret, acc)
    return acc
