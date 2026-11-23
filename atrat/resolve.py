"""Module identity and qualified-name resolution.

The loader parses every file separately, then this pass gives every
non-root declaration a module-local internal name and rewrites references
that cross a module boundary. The checker still sees one flat declaration
list, but names can no longer collide across modules and `import ... as`
aliases are real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
import re

from . import ast
from .check import TypeError_


@dataclass
class Unit:
    source: str
    stem: str
    decls: list
    imports: list = field(default_factory=list)  # list[(ast.Import, target source)]
    prefix: str = ""
    module_id: str = ""
    source_hash: str = ""
    is_root: bool = False
    is_prelude: bool = False


@dataclass
class Symbol:
    kind: str        # "fn", "type", or "trait"
    name: str        # original name
    internal: str
    decl: object
    source: str
    public: bool

    @property
    def module(self) -> str:
        return os.path.basename(self.source)


def _default_alias(spec: str) -> str:
    s = spec.rstrip("/\\")
    if s.endswith(".atr"):
        s = s[:-4]
    s = s.replace("\\", "/").rstrip("/")
    tail = s.split("/")[-1]
    if "." in tail:
        tail = tail.split(".")[-1]
    return tail or "module"


def _safe_stem(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not name or name[0].isdigit():
        name = "m_" + name
    return name


class Resolver:
    def __init__(self, units: list[Unit], root_source: str):
        self.units = units
        self.root_source = root_source
        self.by_source = {u.source: u for u in units}
        self.symbols: dict[str, dict[str, list[Symbol]]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        self.unqualified: dict[str, dict[str, list[Symbol]]] = {}
        self.display: dict[str, str] = {}

    # -- setup ------------------------------------------------------------
    def _module_id(self, source: str) -> str:
        if source == "<prelude>":
            return "<prelude>"
        project_root = os.path.dirname(os.path.abspath(self.root_source))
        rel = os.path.relpath(source, project_root).replace(os.sep, "/")
        return os.path.splitext(rel)[0]

    def _allocate_prefixes(self):
        originals = {d.name for u in self.units for d in u.decls
                     if isinstance(d, (ast.FnDecl, ast.StructDecl,
                                       ast.EnumDecl, ast.TraitDecl))}
        used = set()
        for u in self.units:
            u.module_id = self._module_id(u.source)
            if u.is_root or u.is_prelude:
                u.prefix = ""
                continue
            base = _safe_stem(u.module_id).strip("_") or "module"
            digest = hashlib.sha1(u.module_id.encode("utf-8")).hexdigest()[:8]

            def free(candidate):
                return (candidate not in used
                        and not any((candidate + name) in originals
                                    for name in originals))

            prefix = base + "__"
            if not free(prefix):
                # Sanitised IDs can collide (sub/math vs sub_math). Fall back to
                # a stable hash segment, then numbered suffixes if even that
                # collides with a user declaration.
                prefix = f"{base}_{digest}__"
                n = 2
                while not free(prefix):
                    prefix = f"{base}_{digest}_{n}__"
                    n += 1
            used.add(prefix)
            u.prefix = prefix

    def _collect_symbols(self):
        for u in self.units:
            table: dict[str, list[Symbol]] = {}
            seen: dict[tuple[str, str], object] = {}
            for d in u.decls:
                if isinstance(d, ast.FnDecl):
                    kind = "fn"
                elif isinstance(d, (ast.StructDecl, ast.EnumDecl)):
                    kind = "type"
                elif isinstance(d, ast.TraitDecl):
                    kind = "trait"
                else:
                    continue
                key = (kind, d.name)
                if key in seen:
                    if kind == "type":
                        msg = f"type '{d.name}' is defined twice"
                    elif kind == "trait":
                        msg = f"trait '{d.name}' is defined twice"
                    else:
                        msg = f"function '{d.name}' is defined twice"
                    raise TypeError_(msg, d.line, d.col)
                seen[key] = d
                original = d.name
                internal = original
                if not u.is_root and not u.is_prelude:
                    if not (isinstance(d, ast.FnDecl) and d.is_extern):
                        internal = u.prefix + original
                d.internal_name = internal
                d.display_name = original
                d.name = internal
                d.module_source = u.source
                sym = Symbol(kind, original, internal, d, u.source,
                             bool(getattr(d, "is_public", False)))
                table.setdefault(original, []).append(sym)
                self.display[internal] = original
            self.symbols[u.source] = table
            self.aliases[u.source] = {}
            self.unqualified[u.source] = {}

    def _build_imports(self):
        for u in self.units:
            for node, target in u.imports:
                target_unit = self.by_source.get(target)
                if target_unit is None:
                    continue
                alias = node.alias or _default_alias(node.path)
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", alias):
                    raise TypeError_(
                        f"invalid module alias '{alias}'", node.line, node.col,
                        "aliases must be identifiers")
                prev = self.aliases[u.source].get(alias)
                if prev is not None and prev != target:
                    raise TypeError_(
                        f"module alias '{alias}' refers to two modules",
                        node.line, node.col,
                        "give one of the imports a different `as` name")
                self.aliases[u.source][alias] = target
                if node.alias is None:
                    for name, syms in self.symbols[target].items():
                        self.unqualified[u.source].setdefault(name, []).extend(syms)

    # -- lookup -----------------------------------------------------------
    def _visible(self, sym: Symbol, node, label: str | None = None):
        source = getattr(node, "source", "")
        if not source or sym.source == source or sym.source == "<prelude>":
            return
        if sym.public:
            return
        name = label or sym.name
        raise TypeError_(
            f"'{name}' is private to module '{sym.module}'",
            getattr(node, "line", 0), getattr(node, "col", 0),
            f"mark '{name}' as `pub` in {sym.module} to use it from another file")

    def _matching(self, syms, kind):
        return [s for s in syms if kind is None or s.kind == kind]

    def _pick(self, candidates, name, node, context=""):
        real = [s for s in candidates if s is not None]
        if not real:
            return None
        unique = {}
        for s in real:
            unique.setdefault(s.internal, s)
        if len(unique) == 1:
            return next(iter(unique.values()))
        mods = ", ".join(sorted({s.module for s in real}))
        what = f"{context} " if context else ""
        raise TypeError_(
            f"{what}name '{name}' is ambiguous between {mods}",
            getattr(node, "line", 0), getattr(node, "col", 0),
            "import one module `as` an alias and qualify the use")

    def _lookup(self, unit: Unit, name: str, kind: str, node):
        own = self._matching(self.symbols[unit.source].get(name, []), kind)
        if own:
            sym = self._pick(own, name, node)
            self._visible(sym, node)
            return sym
        imported = self._matching(self.unqualified[unit.source].get(name, []), kind)
        if imported:
            sym = self._pick(imported, name, node)
            self._visible(sym, node)
            return sym
        return None

    def _member(self, unit: Unit, alias: str, name: str, kind: str, node,
                required=False):
        target = self.aliases[unit.source].get(alias)
        if target is None:
            return None
        syms = self._matching(self.symbols[target].get(name, []), kind)
        if not syms:
            if required:
                label = "type" if kind == "type" else kind
                raise TypeError_(
                    f"module '{alias}' has no {label} '{name}'",
                    getattr(node, "line", 0), getattr(node, "col", 0),
                    "check the imported module's `pub` declarations")
            return None
        sym = self._pick(syms, name, node, f"module '{alias}' has ambiguous")
        self._visible(sym, node)
        return sym

    def _path(self, unit: Unit, name: str, kind: str, node, required=False):
        if "." in name:
            alias, rest = name.split(".", 1)
            if alias in self.aliases[unit.source]:
                sym = self._member(unit, alias, rest, kind, node, required=True)
                return sym
            if required:
                raise TypeError_(
                    f"unknown module alias '{alias}' in '{name}'",
                    getattr(node, "line", 0), getattr(node, "col", 0),
                    "add `import \"...\" as %s`" % alias)
            return None
        return self._lookup(unit, name, kind, node)

    # -- rewriting --------------------------------------------------------
    def _type_name(self, unit: Unit, name: str, node):
        sym = self._path(unit, name, "type", node)
        return sym.internal if sym is not None else name

    def _trait_name(self, unit: Unit, name: str, node):
        sym = self._path(unit, name, "trait", node)
        return sym.internal if sym is not None else name

    def _fn_name(self, unit: Unit, name: str, node):
        sym = self._path(unit, name, "fn", node)
        return sym.internal if sym is not None else name

    def _variant_owner(self, unit: Unit, variant: str, node):
        """Find the enum that owns an unqualified variant name."""
        found = []
        for syms in self.symbols[unit.source].values():
            for s in syms:
                if s.kind == "type" and isinstance(s.decl, ast.EnumDecl) \
                        and any(v == variant for v, _ in s.decl.variants):
                    found.append(s)
        for syms in self.unqualified[unit.source].values():
            for s in syms:
                if s.kind == "type" and isinstance(s.decl, ast.EnumDecl) \
                        and any(v == variant for v, _ in s.decl.variants):
                    found.append(s)
        if not found:
            return None
        owner = self._pick(found, variant, node, "variant")
        self._visible(owner, node)
        return owner

    def _enum_has_variant(self, sym: Symbol, variant: str) -> bool:
        return (sym is not None and isinstance(sym.decl, ast.EnumDecl)
                and any(v == variant for v, _ in sym.decl.variants))

    def _resolve_type(self, unit: Unit, t):
        if t is None:
            return
        if isinstance(t, ast.TyName):
            t.name = self._type_name(unit, t.name, t)
            for a in t.args:
                self._resolve_type(unit, a)
            return
        if isinstance(t, ast.TyDyn):
            t.trait = self._trait_name(unit, t.trait, t)
            return
        if isinstance(t, (ast.TyRef, ast.TyPtr, ast.TySlice)):
            self._resolve_type(unit, t.inner)
            return
        if isinstance(t, ast.TyFn):
            for p in t.params:
                self._resolve_type(unit, p)
            self._resolve_type(unit, t.ret)
            return
        if isinstance(t, ast.TyUnit):
            return

    def _resolve_bounds(self, unit: Unit, fn):
        for g, bounds in list(getattr(fn, "bounds", {}).items()):
            fn.bounds[g] = [self._trait_name(unit, b, fn) for b in bounds]

    def _resolve_fn(self, unit: Unit, fn: ast.FnDecl):
        self._resolve_bounds(unit, fn)
        for p in fn.params:
            self._resolve_type(unit, p.ty)
        self._resolve_type(unit, fn.ret)
        for s in fn.body:
            self._resolve_stmt(unit, s)

    def _resolve_decl(self, unit: Unit, d):
        if isinstance(d, ast.StructDecl):
            for _, fty in d.fields:
                self._resolve_type(unit, fty)
        elif isinstance(d, ast.EnumDecl):
            for _, tys in d.variants:
                for vty in tys:
                    self._resolve_type(unit, vty)
        elif isinstance(d, ast.TraitDecl):
            d.supertraits = [self._trait_name(unit, s, d) for s in d.supertraits]
            for fn in d.methods:
                self._resolve_fn(unit, fn)
        elif isinstance(d, ast.FnDecl):
            self._resolve_fn(unit, d)
        elif isinstance(d, ast.ImplBlock):
            if d.trait_name is not None:
                d.trait_name = self._trait_name(unit, d.trait_name, d)
            d.type_name = self._type_name(unit, d.type_name, d)
            for fn in d.methods:
                self._resolve_fn(unit, fn)

    # -- expressions ------------------------------------------------------
    def _resolve_pattern(self, unit: Unit, p):
        if isinstance(p, ast.PatVariant):
            if p.enum:
                p.enum = self._type_name(unit, p.enum, p)
            else:
                owner = self._variant_owner(unit, p.variant, p)
                if owner is not None:
                    p.enum = owner.internal
            for a in p.args:
                self._resolve_pattern(unit, a)

    def _resolve_expr(self, unit: Unit, e):
        if e is None:
            return
        if isinstance(e, ast.Call):
            callee = e.callee
            if isinstance(callee, ast.Name):
                callee.name = self._fn_name(unit, callee.name, e)
            elif isinstance(callee, ast.Field):
                # `module.fn(...)`
                if isinstance(callee.base, ast.Name) and \
                        callee.base.name in self.aliases[unit.source]:
                    sym = self._member(unit, callee.base.name, callee.field,
                                       "fn", e)
                    if sym is not None:
                        e.callee = ast.Name(name=sym.internal, line=callee.line,
                                            col=callee.col)
                        callee = e.callee
                # `module.Enum.Variant(...)`
                elif isinstance(callee.base, ast.Field) and \
                        isinstance(callee.base.base, ast.Name) and \
                        callee.base.base.name in self.aliases[unit.source]:
                    sym = self._member(unit, callee.base.base.name,
                                       callee.base.field, "type", e)
                    if sym is not None and self._enum_has_variant(sym, callee.field):
                        e.callee = ast.Field(
                            base=ast.Name(name=sym.internal, line=callee.base.line,
                                          col=callee.base.col),
                            field=callee.field, line=callee.line, col=callee.col)
                        callee = e.callee
                # `Enum.Variant(...)` in this module or an unqualified import
                elif isinstance(callee.base, ast.Name):
                    sym = self._lookup(unit, callee.base.name, "type", e)
                    if sym is not None and self._enum_has_variant(sym, callee.field):
                        callee.base.name = sym.internal
                if isinstance(callee, ast.Field):
                    self._resolve_expr(unit, callee.base)
            else:
                self._resolve_expr(unit, callee)
            for a in e.args:
                self._resolve_expr(unit, a)
            return
        if isinstance(e, ast.Field):
            if isinstance(e.base, ast.Name) and e.base.name in self.aliases[unit.source]:
                # `module.Type.Variant` or `module.Type` as a value.
                sym = self._member(unit, e.base.name, e.field, "type", e)
                if sym is not None and self._enum_has_variant(sym, e.field):
                    e.base.name = sym.internal
            elif isinstance(e.base, ast.Field) and \
                    isinstance(e.base.base, ast.Name) and \
                    e.base.base.name in self.aliases[unit.source]:
                sym = self._member(unit, e.base.base.name, e.base.field,
                                   "type", e)
                if sym is not None and self._enum_has_variant(sym, e.field):
                    e.base = ast.Name(name=sym.internal, line=e.base.line,
                                      col=e.base.col)
            elif isinstance(e.base, ast.Name):
                sym = self._lookup(unit, e.base.name, "type", e)
                if sym is not None and self._enum_has_variant(sym, e.field):
                    e.base.name = sym.internal
            self._resolve_expr(unit, e.base)
            return
        if isinstance(e, ast.StructLit):
            e.name = self._type_name(unit, e.name, e)
            for _, v in e.fields:
                self._resolve_expr(unit, v)
            return
        if isinstance(e, ast.Cast):
            self._resolve_type(unit, e.target)
            self._resolve_expr(unit, e.base)
            return
        if isinstance(e, ast.Lambda):
            for p in e.params:
                self._resolve_type(unit, p.ty)
            self._resolve_expr(unit, e.body)
            return
        if isinstance(e, ast.Match):
            self._resolve_expr(unit, e.scrutinee)
            for arm in e.arms:
                self._resolve_pattern(unit, arm.pattern)
                self._resolve_expr(unit, arm.body)
            return
        if isinstance(e, ast.StrLit):
            for seg in e.segs:
                if seg.kind == "expr":
                    for p in seg.parts:
                        self._resolve_expr(unit, p)
            return
        if isinstance(e, ast.UnsafeExpr):
            for s in e.body:
                self._resolve_stmt(unit, s)
            return
        if isinstance(e, ast.BlockExpr):
            for s in e.stmts:
                self._resolve_stmt(unit, s)
            return
        if isinstance(e, ast.AsmExpr):
            for a in e.operands:
                self._resolve_expr(unit, a)
            return
        for attr in ("base", "operand", "left", "right", "index", "start", "end"):
            child = getattr(e, attr, None)
            if isinstance(child, ast.Expr):
                self._resolve_expr(unit, child)
        for attr in ("args", "items"):
            for child in getattr(e, attr, []) or []:
                self._resolve_expr(unit, child)

    def _resolve_stmt(self, unit: Unit, s):
        if isinstance(s, ast.Let):
            self._resolve_type(unit, s.ty)
            self._resolve_expr(unit, s.init)
        elif isinstance(s, ast.Assign):
            self._resolve_expr(unit, s.target)
            self._resolve_expr(unit, s.value)
        elif isinstance(s, ast.ExprStmt):
            self._resolve_expr(unit, s.expr)
        elif isinstance(s, ast.If):
            self._resolve_expr(unit, s.cond)
            for x in s.then_body:
                self._resolve_stmt(unit, x)
            for c, body in s.elifs:
                self._resolve_expr(unit, c)
                for x in body:
                    self._resolve_stmt(unit, x)
            for x in s.else_body or []:
                self._resolve_stmt(unit, x)
        elif isinstance(s, ast.While):
            self._resolve_expr(unit, s.cond)
            for x in s.body:
                self._resolve_stmt(unit, x)
        elif isinstance(s, ast.For):
            self._resolve_expr(unit, s.iter)
            for x in s.body:
                self._resolve_stmt(unit, x)
        elif isinstance(s, ast.Return):
            self._resolve_expr(unit, s.value)
        elif isinstance(s, ast.UnsafeStmt):
            for x in s.body:
                self._resolve_stmt(unit, x)
        elif isinstance(s, ast.Defer):
            self._resolve_expr(unit, s.expr)

    def run(self) -> ast.Program:
        self._allocate_prefixes()
        self._collect_symbols()
        self._build_imports()
        for u in self.units:
            for d in u.decls:
                self._resolve_decl(u, d)
        decls = []
        for u in self.units:
            decls.extend(d for d in u.decls if not isinstance(d, ast.Import))
        prog = ast.Program(decls=decls)
        prog.display_names = dict(self.display)
        prog.modules = [u.source for u in self.units]
        prog.units = list(self.units)
        return prog


def resolve_program(units: list[Unit], root_source: str) -> ast.Program:
    return Resolver(units, root_source).run()
