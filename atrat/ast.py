"""AST node definitions for Atrat."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------- types ----
@dataclass
class TyDyn:
    trait: str
    line: int = 0
    col: int = 0


@dataclass
class TyName:
    name: str
    args: list = field(default_factory=list)
    line: int = 0
    col: int = 0


@dataclass
class TyRef:
    mut: bool
    inner: object
    line: int = 0
    col: int = 0


@dataclass
class TyPtr:
    mut: bool
    inner: object
    line: int = 0
    col: int = 0


@dataclass
class TySlice:
    inner: object
    line: int = 0
    col: int = 0


@dataclass
class TyFn:
    params: list
    ret: object
    line: int = 0
    col: int = 0


@dataclass
class TyUnit:
    line: int = 0
    col: int = 0


# ------------------------------------------------------------ patterns ----
@dataclass
class PatWild:
    line: int = 0
    col: int = 0


@dataclass
class PatBind:
    name: str
    line: int = 0
    col: int = 0


@dataclass
class PatLit:
    value: object
    line: int = 0
    col: int = 0


@dataclass
class PatVariant:
    enum: Optional[str]
    variant: str
    args: list
    line: int = 0
    col: int = 0


# ------------------------------------------------------------ expressions --
@dataclass
class Expr:
    line: int = 0
    col: int = 0
    ty: object = None          # filled by the checker
    lvalue: bool = False


@dataclass
class IntLit(Expr):
    value: int = 0


@dataclass
class FloatLit(Expr):
    value: float = 0.0


@dataclass
class BoolLit(Expr):
    value: bool = False


@dataclass
class UnitLit(Expr):
    pass


@dataclass
class CharLit(Expr):
    value: str = ""


@dataclass
class StrLit(Expr):
    segs: list = field(default_factory=list)   # list[StrSeg]


@dataclass
class Name(Expr):
    name: str = ""


@dataclass
class Field(Expr):
    base: Expr = None
    field: str = ""


@dataclass
class Index(Expr):
    base: Expr = None
    index: Expr = None


@dataclass
class Deref(Expr):
    base: Expr = None


@dataclass
class Ref(Expr):
    mut: bool = False
    base: Expr = None


@dataclass
class Unary(Expr):
    op: str = ""
    operand: Expr = None


@dataclass
class Binary(Expr):
    op: str = ""
    left: Expr = None
    right: Expr = None


@dataclass
class Cast(Expr):
    base: Expr = None
    target: object = None


@dataclass
class Try(Expr):
    """`expr?` — propagate Err/None to the enclosing function."""
    base: Expr = None


@dataclass
class Lambda(Expr):
    params: list = field(default_factory=list)   # list[Param]
    body: Expr = None
    # filled by the checker for capturing closures
    captures: list = field(default_factory=list)  # list[(name, Type)]
    closure_name: str = ""
    param_types: list = field(default_factory=list)
    result_type: object = None


@dataclass
class Call(Expr):
    callee: Expr = None
    args: list = field(default_factory=list)
    # filled by checker for method calls
    method_self: Optional[Expr] = None


@dataclass
class StructLit(Expr):
    name: str = ""
    fields: list = field(default_factory=list)     # list[(str, Expr)]


@dataclass
class ListLit(Expr):
    items: list = field(default_factory=list)


@dataclass
class Range(Expr):
    start: Optional[Expr] = None
    end: Optional[Expr] = None
    inclusive: bool = False


@dataclass
class MatchArm:
    pattern: object = None
    body: Expr = None
    line: int = 0
    col: int = 0


@dataclass
class Match(Expr):
    scrutinee: Expr = None
    arms: list = field(default_factory=list)


@dataclass
class UnsafeExpr(Expr):
    body: list = field(default_factory=list)     # list[Stmt]
    is_expr: bool = False


@dataclass
class AsmExpr(Expr):
    template: str = ""
    operands: list = field(default_factory=list)


@dataclass
class BlockExpr(Expr):
    """`if` used in expression position (statement blocks produce unit)."""
    stmts: list = field(default_factory=list)


# ------------------------------------------------------------- statements --
@dataclass
class Stmt:
    line: int = 0
    col: int = 0


@dataclass
class Let(Stmt):
    name: str = ""
    mutable: bool = False
    ty: object = None
    init: Optional[Expr] = None


@dataclass
class Assign(Stmt):
    target: Expr = None
    op: str = "="
    value: Expr = None


@dataclass
class ExprStmt(Stmt):
    expr: Expr = None


@dataclass
class If(Stmt):
    cond: Expr = None
    then_body: list = field(default_factory=list)
    elifs: list = field(default_factory=list)      # list[(cond, body)]
    else_body: Optional[list] = None


@dataclass
class While(Stmt):
    cond: Expr = None
    body: list = field(default_factory=list)


@dataclass
class For(Stmt):
    name: str = ""
    iter: Expr = None
    body: list = field(default_factory=list)


@dataclass
class Return(Stmt):
    value: Optional[Expr] = None


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Pass(Stmt):
    pass


@dataclass
class UnsafeStmt(Stmt):
    body: list = field(default_factory=list)


@dataclass
class Defer(Stmt):
    expr: Expr = None


# ------------------------------------------------------------ declarations --
@dataclass
class Param:
    name: str
    ty: object
    mode: Optional[str]        # None (by value), "&", "&mut"
    line: int = 0
    col: int = 0


@dataclass
class FnDecl:
    name: str = ""
    generics: list = field(default_factory=list)
    bounds: dict = field(default_factory=dict)
    params: list = field(default_factory=list)
    ret: object = None
    body: list = field(default_factory=list)
    is_public: bool = False
    is_extern: bool = False
    line: int = 0
    col: int = 0
    # filled by checker
    scheme: object = None


@dataclass
class StructDecl:
    name: str = ""
    generics: list = field(default_factory=list)
    fields: list = field(default_factory=list)     # list[(str, ty)]
    line: int = 0
    col: int = 0
    repr_c: bool = False
    is_public: bool = False


@dataclass
class EnumDecl:
    name: str = ""
    generics: list = field(default_factory=list)
    variants: list = field(default_factory=list)   # list[(str, list[ty])]
    line: int = 0
    col: int = 0
    is_public: bool = False


@dataclass
class ImplMethod:
    decl: FnDecl = None


@dataclass
class TraitDecl:
    name: str = ""
    generics: list = field(default_factory=list)
    supertraits: list = field(default_factory=list)
    methods: list = field(default_factory=list)   # list[FnDecl] signatures
    line: int = 0
    col: int = 0
    is_public: bool = False


@dataclass
class ImplBlock:
    type_name: str = ""
    trait_name: Optional[str] = None
    generics: list = field(default_factory=list)
    methods: list = field(default_factory=list)
    line: int = 0
    col: int = 0
    is_public: bool = False


@dataclass
class Import:
    path: str = ""
    alias: Optional[str] = None
    line: int = 0
    col: int = 0


@dataclass
class Program:
    decls: list = field(default_factory=list)
    display_names: dict = field(default_factory=dict)
    modules: list = field(default_factory=list)
    units: list = field(default_factory=list)


def stamp_sources(node, source: str):
    """Attach the originating file to every AST node with a source position.

    The loader merges files into one AST, so a node's source is needed for
    module-visibility checks. `ty`/`scheme` are filled later by the checker and
    deliberately skipped.
    """
    from dataclasses import fields as _fields
    seen = set()

    def walk(x):
        if x is None or isinstance(x, (str, bytes, int, float, bool)):
            return
        if id(x) in seen:
            return
        if isinstance(x, (list, tuple)):
            for item in x:
                walk(item)
            return
        if isinstance(x, dict):
            for item in x.values():
                walk(item)
            return
        if hasattr(x, "__dataclass_fields__"):
            seen.add(id(x))
            if hasattr(x, "line"):
                try:
                    x.source = source
                except Exception:
                    pass
            for f in _fields(x):
                if f.name in ("ty", "scheme", "inst", "solved", "fn_ref",
                              "source", "param_types"):
                    continue
                walk(getattr(x, f.name, None))

    walk(node)
