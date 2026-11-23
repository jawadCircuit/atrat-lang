"""Name resolution, Hindley-Milner style inference, exhaustiveness and borrow checking."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import os

from . import ast
from .typesys import (
    BOOL, FLOAT_TYPES, I64, INT_TYPES, NUM_TYPES, PRIMS, STR, STRING, TCon, TFn,
    TPtr, TRef, TVar, UNIT, USIZE, Scheme, Unifier, UnifyError, free_vars,
    is_float, is_int, is_numeric, subst,
)
from .lexer import AtratError


class TypeError_(AtratError):
    kind = "type error"


class BorrowError(AtratError):
    kind = "borrow error"


TYPE_ALIASES = {"int": "i64", "uint": "u64", "float": "f64", "byte": "u8"}

BUILTIN_FNS = {"print", "println", "write", "panic", "assert", "len", "to_str",
               "min", "max", "abs", "sqrt", "exit", "drop",
               "read_file", "write_file", "env", "args", "now_ms", "box", "rc"}

LIST_METHODS = {"len", "is_empty", "push", "pop", "get", "set", "clear"}


@dataclass
class StructInfo:
    name: str
    generics: list
    fields: list            # list[(str, Type)]
    decl: ast.StructDecl
    repr_c: bool = False
    source: str = ""
    public: bool = False


@dataclass
class EnumInfo:
    name: str
    generics: list
    variants: list          # list[(str, list[Type])]
    decl: ast.EnumDecl
    source: str = ""
    public: bool = False


@dataclass
class ClosureInfo:
    name: str
    params: list
    ret: object
    captures: list          # list[(str, Type)]
    decl: object


@dataclass
class TraitInfo:
    name: str
    generics: list
    methods: dict           # method name -> ast.FnDecl signature
    supertraits: list = field(default_factory=list)
    impls: dict = field(default_factory=dict)   # type name -> {method: FnInfo}
    source: str = ""
    public: bool = False


@dataclass
class FnInfo:
    decl: object
    name: str
    generics: list          # names
    generic_vars: dict      # name -> TVar
    params: list            # list[Type]
    ret: Type
    scheme: Scheme
    is_method: bool = False
    self_mode: str | None = None
    owner: str | None = None
    borrow_param: str | None = None
    borrow_mut: bool = False
    source: str = ""
    public: bool = False


@dataclass
class VariantRef:
    enum: str
    index: int


class Module:
    """All declarations plus the unifier. Shared by the checker and code generator."""

    def __init__(self):
        self.u = Unifier()
        self.structs: dict[str, StructInfo] = {}
        self.enums: dict[str, EnumInfo] = {}
        self.fns: dict[str, FnInfo] = {}
        self.methods: dict[tuple[str, str], FnInfo] = {}
        self.traits: dict[str, TraitInfo] = {}
        self.closures: dict[str, ClosureInfo] = {}
        self.default_methods: list[FnInfo] = []
        self.variants: dict[str, list[VariantRef]] = {}
        self.program: ast.Program | None = None
        self.warnings: list[str] = []
        self.display: dict[str, str] = {}

    def display_name(self, name: str) -> str:
        return self.display.get(name, name)

    def require_visible(self, info, node, name: str | None = None):
        """Reject a cross-file use of a declaration that is not `pub`.

        Single-file compiles and the prelude have no cross-module boundary.
        This is visibility only; there is no module namespace or separate
        compilation yet.
        """
        source = getattr(node, "source", "")
        target = getattr(info, "source", "")
        if not source or not target or source == target or target == "<prelude>":
            return
        if getattr(info, "public", False):
            return
        label = name or getattr(info, "name", "declaration")
        label = self.display_name(label)
        module = os.path.basename(target)
        raise TypeError_(
            f"'{label}' is private to module '{module}'",
            getattr(node, "line", 0), getattr(node, "col", 0),
            f"mark '{label}' as `pub` in {module} to use it from another file")

    # -- type helpers -----------------------------------------------------
    def is_copy(self, t, _seen=None) -> bool:
        t = self.u.resolve_deep(t)
        if isinstance(t, TVar):
            return True
        if isinstance(t, TCon):
            if t.name in PRIMS:
                return t.name not in ("String",)
            info = self.structs.get(t.name)
            if info is not None:
                seen = _seen or set()
                key = (t.name, tuple(a.show() for a in t.args))
                if key in seen:
                    return True
                seen.add(key)
                mp = dict(zip(info.generics, t.args))
                return all(self.is_copy(subst(ft, mp), seen) for _, ft in info.fields)
            info = self.enums.get(t.name)
            if info is not None:
                seen = _seen or set()
                key = (t.name, tuple(a.show() for a in t.args))
                if key in seen:
                    return True
                seen.add(key)
                mp = dict(zip(info.generics, t.args))
                return all(self.is_copy(subst(ft, mp), seen)
                           for _, tys in info.variants for ft in tys)
            ci = self.closures.get(t.name)
            if ci is not None:
                return all(self.is_copy(ct) for _, ct in ci.captures)
            return False
        if isinstance(t, (TRef, TPtr, TFn)):
            return True
        return True

    def needs_drop(self, t) -> bool:
        return not self.is_copy(t)

    def variant_ref(self, enum_name: str, variant: str) -> VariantRef | None:
        info = self.enums.get(enum_name)
        if not info:
            return None
        for i, (vn, _) in enumerate(info.variants):
            if vn == variant:
                return VariantRef(enum_name, i)
        return None

    def trait_lineage(self, name: str, _seen=None):
        """Yield `name` followed by its supertraits, child-first, once each."""
        seen = _seen if _seen is not None else set()
        if name in seen or name not in self.traits:
            return
        seen.add(name)
        yield name
        for parent in self.traits[name].supertraits:
            yield from self.trait_lineage(parent, seen)

    def trait_methods(self, name: str) -> dict:
        """All methods visible on a trait, with child declarations overriding parents."""
        out = {}
        for owner in self.trait_lineage(name):
            for mname, sig in self.traits[owner].methods.items():
                if mname not in out:
                    out[mname] = (owner, sig)
        return out

    def trait_method(self, name: str, method: str):
        for owner in self.trait_lineage(name):
            sig = self.traits[owner].methods.get(method)
            if sig is not None:
                return owner, sig
        return None

    def find_unqualified_variant(self, name: str) -> VariantRef | None:
        refs = self.variants.get(name, [])
        if len(refs) == 1:
            return refs[0]
        return None

    def struct_field(self, t: TCon, field: str):
        info = self.structs.get(t.name)
        if not info:
            return None
        for fn, ft in info.fields:
            if fn == field:
                mp = dict(zip(info.generics, t.args))
                return subst(ft, mp)
        return None


def collect(prog: ast.Program) -> Module:
    m = Module()
    m.program = prog
    m.display = dict(getattr(prog, "display_names", {}) or {})
    u = m.u

    def ty(node, generics: dict[str, TVar], ctx: str):
        if isinstance(node, ast.TyDyn):
            if node.trait not in m.traits:
                raise TypeError_(f"unknown trait '{node.trait}' in dyn type",
                                 node.line, node.col)
            m.require_visible(m.traits[node.trait], node, node.trait)
            return TCon("dyn", [TCon(node.trait)])
        if isinstance(node, ast.TyName):
            name = TYPE_ALIASES.get(node.name, node.name)
            if name in generics:
                if node.args:
                    raise TypeError_(f"type parameter '{name}' cannot take arguments",
                                     node.line, node.col)
                return generics[name]
            if name in PRIMS:
                if node.args:
                    raise TypeError_(f"primitive type '{name}' takes no arguments",
                                     node.line, node.col)
                return TCon(name)
            if name in ("List", "Box", "Rc"):
                if len(node.args) != 1:
                    raise TypeError_(f"{name}<T> expects exactly one type argument",
                                     node.line, node.col)
                return TCon(name, [ty(node.args[0], generics, ctx)])
            if name in m.structs:
                info = m.structs[name]
                m.require_visible(info, node, name)
                if len(node.args) != len(info.generics):
                    raise TypeError_(
                        f"type '{name}' expects {len(info.generics)} argument(s), got {len(node.args)}",
                        node.line, node.col)
                return TCon(name, [ty(a, generics, ctx) for a in node.args])
            if name in m.enums:
                info = m.enums[name]
                m.require_visible(info, node, name)
                if len(node.args) != len(info.generics):
                    raise TypeError_(
                        f"type '{name}' expects {len(info.generics)} argument(s), got {len(node.args)}",
                        node.line, node.col)
                return TCon(name, [ty(a, generics, ctx) for a in node.args])
            raise TypeError_(f"unknown type '{name}'", node.line, node.col)
        if isinstance(node, ast.TyRef):
            return TRef(node.mut, ty(node.inner, generics, ctx))
        if isinstance(node, ast.TyPtr):
            return TPtr(node.mut, ty(node.inner, generics, ctx))
        if isinstance(node, ast.TySlice):
            return TCon("List", [ty(node.inner, generics, ctx)])
        if isinstance(node, ast.TyFn):
            return TFn([ty(p, generics, ctx) for p in node.params],
                       ty(node.ret, generics, ctx))
        if isinstance(node, ast.TyUnit):
            return UNIT
        raise TypeError_(f"invalid type in {ctx}", getattr(node, "line", 0), getattr(node, "col", 0))

    # Pass 1: type names must exist before fields refer to them.
    for d in prog.decls:
        if isinstance(d, ast.StructDecl):
            if d.name in m.structs or d.name in m.enums:
                raise TypeError_(f"type '{d.name}' is defined twice", d.line, d.col)
            m.structs[d.name] = StructInfo(
                d.name, list(d.generics), [], d, d.repr_c,
                source=getattr(d, "source", ""),
                public=getattr(d, "is_public", False))
        elif isinstance(d, ast.EnumDecl):
            if d.name in m.structs or d.name in m.enums:
                raise TypeError_(f"type '{d.name}' is defined twice", d.line, d.col)
            m.enums[d.name] = EnumInfo(
                d.name, list(d.generics), [], d,
                source=getattr(d, "source", ""),
                public=getattr(d, "is_public", False))
        elif isinstance(d, ast.TraitDecl):
            if d.name in m.traits:
                raise TypeError_(f"trait '{d.name}' is defined twice", d.line, d.col)
            m.traits[d.name] = TraitInfo(
                d.name, list(d.generics), {},
                list(getattr(d, "supertraits", [])),
                source=getattr(d, "source", ""),
                public=getattr(d, "is_public", False))

    # Pass 2: fields, variants.
    for d in prog.decls:
        if isinstance(d, ast.StructDecl):
            gvars = {g: u.fresh(hint=g) for g in d.generics}
            fields = []
            for fname, fty in d.fields:
                # References are allowed in structs, but conservatively: a value
                # holding one keeps its source borrowed for the whole block and
                # cannot be returned or rebound.
                t = ty(fty, gvars, f"struct {d.name}")
                if _dyn_by_value(m, t):
                    raise TypeError_(
                        f"field '{d.name}.{fname}' stores a trait object by value",
                        d.line, d.col, "use `&dyn Trait`, `&mut dyn Trait` or `Box<dyn Trait>`")
                fields.append((fname, t))
            m.structs[d.name].fields = fields
        elif isinstance(d, ast.EnumDecl):
            gvars = {g: u.fresh(hint=g) for g in d.generics}
            variants = []
            for vname, vtys in d.variants:
                types = []
                for vt in vtys:
                    t = ty(vt, gvars, f"enum {d.name}")
                    if _dyn_by_value(m, t):
                        raise TypeError_(
                            f"variant '{d.name}.{vname}' stores a trait object by value",
                            d.line, d.col,
                            "use `&dyn Trait`, `&mut dyn Trait` or `Box<dyn Trait>`")
                    if _contains_ref(t):
                        raise TypeError_(
                            f"variant '{d.name}.{vname}' stores a reference, which is not "
                            f"supported yet (no lifetimes)", d.line, d.col)
                    types.append(t)
                variants.append((vname, types))
            m.enums[d.name].variants = variants
            for i, (vname, _) in enumerate(variants):
                m.variants.setdefault(vname, []).append(VariantRef(d.name, i))

    for d in prog.decls:
        if isinstance(d, ast.TraitDecl):
            for parent in getattr(d, "supertraits", []):
                if parent not in m.traits:
                    raise TypeError_(f"trait '{d.name}' has unknown supertrait '{parent}'",
                                     d.line, d.col)
                if parent == d.name:
                    raise TypeError_(f"trait '{d.name}' cannot extend itself",
                                     d.line, d.col)
                m.require_visible(m.traits[parent], d, parent)

    trait_lines = {d.name: (d.line, d.col) for d in prog.decls
                   if isinstance(d, ast.TraitDecl)}

    def _check_trait_cycles(node, path):
        if node in path:
            chain = " -> ".join(path[path.index(node):] + [node])
            line, col = trait_lines.get(node, (0, 0))
            raise TypeError_(f"cyclic supertrait relationship: {chain}", line, col)
        for parent in m.traits[node].supertraits:
            _check_trait_cycles(parent, path + [node])

    for name in m.traits:
        _check_trait_cycles(name, [])

    for d in prog.decls:
        if isinstance(d, ast.TraitDecl):
            gvars = {g: u.fresh(hint=g) for g in d.generics}
            for fn in d.methods:
                if fn.name in m.traits[d.name].methods:
                    raise TypeError_(f"trait '{d.name}' declares method '{fn.name}' twice",
                                     fn.line, fn.col)
                if not fn.params or fn.params[0].name != "self":
                    raise TypeError_(f"trait method '{fn.name}' must take self first",
                                     fn.line, fn.col)
                self_var = u.fresh(hint="Self")
                trait_gvars = dict(gvars)
                trait_gvars["Self"] = self_var
                params = []
                for i, p in enumerate(fn.params):
                    if p.name == "self":
                        pty = self_var if p.mode is None else TRef(p.mode == "&mut", self_var)
                    else:
                        if p.ty is None:
                            raise TypeError_(f"trait method parameter '{p.name}' needs a type",
                                             p.line, p.col)
                        pty = ty(p.ty, trait_gvars, f"trait {d.name}")
                        if p.mode:
                            pty = TRef(p.mode == "&mut", pty)
                    params.append(pty)
                ret = ty(fn.ret, trait_gvars, f"trait {d.name}") if fn.ret is not None else UNIT
                scheme_vars = [self_var.id] + [v.id for v in gvars.values()]
                m.traits[d.name].methods[fn.name] = FnInfo(
                    decl=fn, name=fn.name, generics=list(fn.generics),
                    generic_vars=gvars, params=params, ret=ret,
                    scheme=Scheme(scheme_vars, TFn(params, ret)),
                    is_method=True, self_mode=fn.params[0].mode, owner=None,
                    source=getattr(d, "source", ""),
                    public=getattr(d, "is_public", False))

    # Pass 3: functions.
    for d in prog.decls:
        if isinstance(d, ast.FnDecl):
            if d.name in m.fns:
                raise TypeError_(f"function '{d.name}' is defined twice", d.line, d.col)
            m.fns[d.name] = _fn_info(m, d, ty, owner=None, self_mode=None)

    for d in prog.decls:
        if isinstance(d, ast.ImplBlock):
            owner = d.type_name
            if owner not in m.structs and owner not in m.enums                 and owner not in PRIMS:
                raise TypeError_(f"cannot implement methods on unknown type '{owner}'",
                                 d.line, d.col)
            impl_generics = list(d.generics)
            if impl_generics:
                sinfo = m.structs.get(owner) or m.enums.get(owner)
                if sinfo is None or len(sinfo.generics) != len(impl_generics):
                    raise TypeError_(
                        f"impl generics {impl_generics} do not match type '{owner}'",
                        d.line, d.col,
                        f"'{owner}' expects {len(getattr(sinfo, 'generics', []))} "
                        f"type parameter(s)")
            trait = None
            if d.trait_name is not None:
                trait = m.traits.get(d.trait_name)
                if trait is None:
                    raise TypeError_(f"unknown trait '{d.trait_name}'", d.line, d.col)
                m.require_visible(trait, d, d.trait_name)
                if owner in trait.impls:
                    raise TypeError_(f"'{owner}' already implements '{trait.name}'",
                                     d.line, d.col)
                trait.impls.setdefault(owner, {})
                provided = {fn.name for fn in d.methods}
                for required, rsig in trait.methods.items():
                    if required not in provided and not rsig.decl.body:
                        raise TypeError_(
                            f"'{owner}' is missing trait method '{required}' "
                            f"required by '{trait.name}'", d.line, d.col)
                for fn in d.methods:
                    if fn.name not in trait.methods:
                        raise TypeError_(
                            f"'{trait.name}' has no method '{fn.name}'", fn.line, fn.col)
                    sig = trait.methods[fn.name]
                    if len(sig.params) != len(fn.params):
                        raise TypeError_(
                            f"trait method '{fn.name}' takes {len(sig.params)} "
                            f"parameter(s), impl has {len(fn.params)}", fn.line, fn.col)
                    if (sig.self_mode or None) != (fn.params[0].mode or None):
                        raise TypeError_(
                            f"trait method '{fn.name}' receiver does not match the impl",
                            fn.line, fn.col)
            for fn in d.methods:
                key = (owner, fn.name)
                if key in m.methods:
                    raise TypeError_(f"method '{owner}.{fn.name}' is defined twice",
                                     fn.line, fn.col)
                if not fn.params or fn.params[0].name != "self":
                    raise TypeError_(f"method '{fn.name}' must take self as its first parameter",
                                     fn.line, fn.col)
                info = _fn_info(m, fn, ty, owner=owner,
                                self_mode=fn.params[0].mode,
                                impl_generics=impl_generics)
                if trait is not None:
                    sig = trait.methods[fn.name]
                    own_ty = TCon(owner)
                    if impl_generics:
                        own_ty = TCon(owner, [info.generic_vars[g] for g in impl_generics])
                    mp = {"Self": own_ty}
                    for got, want in zip(info.params, sig.params):
                        if not m.u.unify_ok(got, subst(want, mp)):
                            raise TypeError_(
                                f"trait method '{fn.name}' parameter types do not match",
                                fn.line, fn.col)
                    if not m.u.unify_ok(info.ret, subst(sig.ret, mp)):
                        raise TypeError_(
                            f"trait method '{fn.name}' return type does not match "
                            f"`{subst(sig.ret, mp).show()}`", fn.line, fn.col)
                m.methods[key] = info
                if trait is not None:
                    trait.impls.setdefault(owner, {})[fn.name] = info
            if trait is not None:
                for mname, sig in trait.methods.items():
                    if not sig.decl.body:
                        continue
                    if mname in provided:
                        # The impl supplied this method explicitly.
                        trait.impls[owner][mname] = m.methods[(owner, mname)]
                        continue
                    dinfo = _fn_info(m, copy.deepcopy(sig.decl), ty, owner=owner,
                                     self_mode=sig.self_mode,
                                     impl_generics=impl_generics)
                    # Different traits may each provide a default with the same
                    # method name for the same type. Give each default a unique
                    # internal function name so their C bodies do not collide.
                    dinfo.name = f"{mname}__{trait.name}"
                    trait.impls[owner][mname] = dinfo
                    if (owner, mname) not in m.methods:
                        m.methods[(owner, mname)] = dinfo
                    else:
                        m.default_methods.append(dinfo)

    # A child trait that inherits the same method name from two different
    # parents is ambiguous unless the child declares its own override.
    for name, tri in m.traits.items():
        own = set(tri.methods)
        seen = {}
        for parent in tri.supertraits:
            for owner in m.trait_lineage(parent):
                for mname in m.traits[owner].methods:
                    if mname in own:
                        continue
                    prev = seen.get(mname)
                    if prev is not None and prev != owner:
                        line, col = trait_lines.get(name, (0, 0))
                        raise TypeError_(
                            f"trait '{name}' inherits ambiguous method "
                            f"'{mname}' from '{prev}' and '{owner}'",
                            line, col,
                            f"declare '{mname}' in '{name}' to disambiguate")
                    seen[mname] = owner
    # A public declaration may not expose a private type through its
    # signature or fields. This is the same rule that makes `pub` meaningful:
    # callers can use the public surface without naming hidden implementation
    # types.
    def named_types(t, out):
        t = m.u.resolve_deep(t)
        if isinstance(t, TCon):
            if t.name == "dyn" and t.args:
                inner = m.u.resolve_deep(t.args[0])
                if isinstance(inner, TCon):
                    out.append(inner.name)
                return
            out.append(t.name)
            for a in t.args:
                named_types(a, out)
        elif isinstance(t, (TRef, TPtr)):
            named_types(t.inner, out)
        elif isinstance(t, TFn):
            for p in t.params:
                named_types(p, out)
            named_types(t.ret, out)

    checked = set()

    def check_names(owner_name, info, types):
        if not getattr(info, "public", False):
            return
        source = getattr(info, "source", "")
        if not source or source == "<input>":
            return
        names = []
        for t in types:
            named_types(t, names)
        for name in names:
            target = m.structs.get(name) or m.enums.get(name) or m.traits.get(name)
            if target is None:
                continue
            if getattr(target, "source", "") == "<prelude>":
                continue
            if getattr(target, "public", False):
                continue
            key = (id(info), name)
            if key in checked:
                continue
            checked.add(key)
            raise TypeError_(
                f"public {owner_name} exposes private type '{m.display_name(name)}'",
                getattr(info.decl, "line", 0),
                getattr(info.decl, "col", 0),
                f"declare type '{m.display_name(name)}' as `pub` or remove it from the public interface")

    def check_bound_names(owner_name, info):
        if not getattr(info, "public", False):
            return
        source = getattr(info, "source", "")
        if not source or source == "<input>":
            return
        for g, bounds in getattr(getattr(info, "decl", None), "bounds", {}).items():
            for name in bounds:
                target = m.traits.get(name)
                if target is None or getattr(target, "source", "") == "<prelude>":
                    continue
                if getattr(target, "public", False):
                    continue
                raise TypeError_(
                    f"public {owner_name} exposes private trait '{m.display_name(name)}'",
                    getattr(info.decl, "line", 0),
                    getattr(info.decl, "col", 0),
                    f"declare trait '{m.display_name(name)}' as `pub` or remove it from the public interface")

    for info in list(m.fns.values()) + list(m.methods.values()):
        check_names(f"function '{info.name}'", info, list(info.params) + [info.ret])
        check_bound_names(f"function '{info.name}'", info)
    for tri in m.traits.values():
        for sig in tri.methods.values():
            check_names(f"trait method '{tri.name}.{sig.name}'", sig,
                        list(sig.params) + [sig.ret])
    for sinfo in m.structs.values():
        if not sinfo.public or sinfo.source == "<input>":
            continue
        for fname, fty in sinfo.fields:
            check_names(f"field '{sinfo.name}.{fname}'", sinfo, [fty])
    for einfo in m.enums.values():
        if not einfo.public or einfo.source == "<input>":
            continue
        for vname, vtys in einfo.variants:
            check_names(f"variant '{einfo.name}.{vname}'", einfo, vtys)

    return m


def _contains_ref(t) -> bool:
    if isinstance(t, TRef):
        return True
    if isinstance(t, TCon):
        return any(_contains_ref(a) for a in t.args)
    if isinstance(t, TFn):
        return any(_contains_ref(p) for p in t.params) or _contains_ref(t.ret)
    return False


def _contains_tvar(t) -> bool:
    if isinstance(t, TVar):
        return True
    if isinstance(t, TCon):
        return any(_contains_tvar(a) for a in t.args)
    if isinstance(t, TFn):
        return any(_contains_tvar(p) for p in t.params) or _contains_tvar(t.ret)
    return False


def _has_ref(m: Module, t, _seen=None) -> bool:
    """Like _contains_ref, but also looks inside struct/enum definitions."""
    t = m.u.resolve_deep(t)
    if isinstance(t, TRef):
        return True
    if isinstance(t, TFn):
        return any(_has_ref(m, p, _seen) for p in t.params) or _has_ref(m, t.ret, _seen)
    if isinstance(t, TCon):
        if any(_has_ref(m, a, _seen) for a in t.args):
            return True
        info = m.structs.get(t.name)
        if info is not None:
            seen = _seen or set()
            key = (t.name, tuple(a.show() for a in t.args))
            if key in seen:
                return False
            seen.add(key)
            mp = dict(zip(info.generics, t.args))
            return any(_has_ref(m, subst(ft, mp), seen) for _, ft in info.fields)
        info = m.enums.get(t.name)
        if info is not None:
            seen = _seen or set()
            key = (t.name, tuple(a.show() for a in t.args))
            if key in seen:
                return False
            seen.add(key)
            mp = dict(zip(info.generics, t.args))
            return any(_has_ref(m, subst(ft, mp), seen)
                       for _, tys in info.variants for ft in tys)
    return False


def _has_mut_ref(m: Module, t, _seen=None) -> bool:
    t = m.u.resolve_deep(t)
    if isinstance(t, TRef):
        return bool(t.mut) or _has_mut_ref(m, t.inner, _seen)
    if isinstance(t, TFn):
        return (any(_has_mut_ref(m, p, _seen) for p in t.params)
                or _has_mut_ref(m, t.ret, _seen))
    if isinstance(t, TCon):
        info = m.structs.get(t.name)
        if info is not None:
            seen = _seen or set()
            key = (t.name, tuple(a.show() for a in t.args))
            if key in seen:
                return False
            seen.add(key)
            mp = dict(zip(info.generics, t.args))
            return any(_has_mut_ref(m, subst(ft, mp), seen) for _, ft in info.fields)
        info = m.enums.get(t.name)
        if info is not None:
            seen = _seen or set()
            key = (t.name, tuple(a.show() for a in t.args))
            if key in seen:
                return False
            seen.add(key)
            mp = dict(zip(info.generics, t.args))
            return any(_has_mut_ref(m, subst(ft, mp), seen)
                       for _, tys in info.variants for ft in tys)
        return any(_has_mut_ref(m, a, _seen) for a in t.args)
    return False


def _dyn_by_value(m: Module, t) -> bool:
    """True if `dyn Trait` appears by value (not refs or Box<dyn>)."""
    t = m.u.resolve_deep(t)
    if isinstance(t, TCon):
        if t.name == "dyn":
            return True
        if t.name == "Box" and len(t.args) == 1:
            return False
        return any(_dyn_by_value(m, a) for a in t.args)
    if isinstance(t, (TRef, TPtr)):
        return False
    if isinstance(t, TFn):
        return any(_dyn_by_value(m, p) for p in t.params) or _dyn_by_value(m, t.ret)
    return False


def _fn_info(m: Module, d: ast.FnDecl, ty, owner, self_mode,
             impl_generics=None) -> FnInfo:
    u = m.u
    for g, bounds in getattr(d, "bounds", {}).items():
        for trait in bounds:
            if trait not in m.traits:
                raise TypeError_(f"unknown trait '{trait}' in bound for '{g}'",
                                 d.line, d.col)
            m.require_visible(m.traits[trait], d, trait)
    impl_generics = list(impl_generics or [])
    if len(set(impl_generics) & set(d.generics)):
        raise TypeError_("impl and method type parameters must have different names",
                         d.line, d.col)
    gvars = {g: u.fresh(hint=g) for g in impl_generics + list(d.generics)}
    body_gvars = dict(gvars)
    own_ty = TCon(owner) if owner is not None else None
    if owner is not None and impl_generics:
        own_ty = TCon(owner, [gvars[g] for g in impl_generics])
    if owner is not None:
        body_gvars["Self"] = own_ty
    params = []
    for i, p in enumerate(d.params):
        if p.name == "self":
            if owner is None:
                raise TypeError_("'self' is only valid inside an impl block", p.line, p.col)
            base = own_ty
            pty = base if self_mode is None else TRef(self_mode == "&mut", base)
            if p.ty is not None:
                raise TypeError_("self may not have a type annotation", p.line, p.col)
        else:
            if p.ty is None:
                raise TypeError_(f"parameter '{p.name}' needs a type annotation", p.line, p.col)
            pty = ty(p.ty, body_gvars, f"function {d.name}")
        if p.mode and p.name != "self":
            pty = TRef(p.mode == "&mut", pty)
        params.append(pty)
    if any(_dyn_by_value(m, p) for p in params):
        raise TypeError_(
            f"function '{d.name}' takes a trait object by value", d.line, d.col,
            "use `&dyn Trait`, `&mut dyn Trait` or `Box<dyn Trait>`")
    ret = ty(d.ret, body_gvars, f"function {d.name}") if d.ret is not None else UNIT
    if _dyn_by_value(m, ret):
        raise TypeError_(
            f"function '{d.name}' returns a trait object by value", d.line, d.col,
            "use `Box<dyn Trait>` or a reference")
    borrow_param = None
    borrow_mut = False
    if _has_ref(m, ret):
        ref_params = [(p.name, t) for p, t in zip(d.params, params)
                      if isinstance(t, TRef)]
        if len(ref_params) != 1:
            raise TypeError_(
                f"function '{d.name}' returns a borrow but has {len(ref_params)} "
                f"reference parameters", d.line, d.col,
                "a returned borrow needs exactly one `&T`/`&mut T` parameter")
        borrow_param = ref_params[0][0]
        borrow_mut = _has_mut_ref(m, ret)
    scheme_vars = [v.id for v in gvars.values()]
    info = FnInfo(decl=d, name=d.name, generics=impl_generics + list(d.generics),
                  generic_vars=gvars, params=params, ret=ret,
                  scheme=Scheme(scheme_vars, TFn(params, ret)),
                  is_method=owner is not None, self_mode=self_mode, owner=owner,
                  borrow_param=borrow_param, borrow_mut=borrow_mut,
                  source=getattr(d, "source", ""),
                  public=getattr(d, "is_public", False))
    # Impl methods inherit the visibility of the type they are defined on;
    # method-level `pub` is not part of the surface syntax yet.
    if owner is not None:
        oi = m.structs.get(owner) or m.enums.get(owner)
        if oi is not None and oi.public:
            info.public = True
    return info


# --------------------------------------------------------------------------
# Expression / statement inference
# --------------------------------------------------------------------------
class FnChecker:
    def __init__(self, m: Module, info: FnInfo):
        self.m = m
        self.u = m.u
        self.info = info
        self.scopes: list[dict[str, TVar]] = [{}]
        self.loop_depth = 0
        self.unsafe = 0
        self.lambda_base = None
        self.lambda_captures: list[set] = []

    def require_visible(self, info, node, name=None):
        self.m.require_visible(info, node, name)

    # -- helpers ----------------------------------------------------------
    def err(self, msg, node, hint=""):
        raise TypeError_(msg, getattr(node, "line", 0), getattr(node, "col", 0), hint)

    def push(self):
        self.scopes.append({})

    def pop(self):
        self.scopes.pop()

    def bind(self, name, t):
        self.scopes[-1][name] = t

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        return None

    def lookup_index(self, name):
        for i in range(len(self.scopes) - 1, -1, -1):
            if name in self.scopes[i]:
                return i
        return None

    def fresh(self, hint="", cons=frozenset()):
        return self.u.fresh(hint, cons)

    def assignable(self, expr, got, want, node, ctx=""):
        """Unification plus safe literal coercions (str -> String copies)."""
        if self.try_coerce(expr, got, want):
            return
        self.unify(got, want, node, ctx)

    def try_coerce(self, expr, got, want, depth=0) -> bool:
        if depth > 8:
            return False
        g = self.u.resolve_deep(got)
        w = self.u.resolve_deep(want)
        from . import ast as _ast
        if isinstance(expr, _ast.Match):
            if not isinstance(w, TCon):
                return False
            ok = True
            for arm in expr.arms:
                if not self.try_coerce(arm.body, arm.body.ty, w, depth + 1):
                    if not self.u.unify_ok(arm.body.ty, w):
                        ok = False
            if ok:
                expr.ty = w
            return ok
        if isinstance(w, TRef) and isinstance(w.inner, TCon)                 and w.inner.name == "dyn":
            if not isinstance(g, TRef):
                return False
            if w.mut and not g.mut:
                return False
            base = self.u.resolve_deep(g.inner)
            if isinstance(base, TVar):
                d = self.u.default(base)
                if not (isinstance(d, TVar) and d.id == base.id):
                    self.u.unify(base, d)
                    base = self.u.resolve_deep(base)
            if not (isinstance(base, TCon) and base.name != "dyn"):
                return False
            tri = self.m.traits.get(w.inner.args[0].name)
            if tri is None:
                return False
            # Every supertrait needs its own impl; methods may live on any
            # trait in the hierarchy.
            for owner in self.m.trait_lineage(tri.name):
                if base.name not in self.m.traits[owner].impls:
                    return False
            methods = self.m.trait_methods(tri.name)
            if len(methods) == 0:
                return False
            for mname, (owner, sig) in methods.items():
                impl = self.m.traits[owner].impls.get(base.name) or {}
                info = impl.get(mname)
                if info is None or info.generics:
                    return False
                if sig.self_mode is None:
                    return False
                if _contains_ref(sig.ret) or _contains_tvar(sig.ret):
                    return False
                if any(_contains_tvar(p) for p in sig.params):
                    return False
            expr.coerce_dyn = (tri.name, base)
            expr.ty = w
            return True
        if (isinstance(w, TCon) and w.name == "Box" and len(w.args) == 1
                and isinstance(w.args[0], TCon) and w.args[0].name == "dyn"
                and isinstance(g, TCon) and g.name == "Box" and len(g.args) == 1):
            dyn_t = w.args[0]
            base = self.u.resolve_deep(g.args[0])
            if isinstance(base, TVar):
                d = self.u.default(base)
                if not (isinstance(d, TVar) and d.id == base.id):
                    self.u.unify(base, d)
                    base = self.u.resolve_deep(base)
            if not (isinstance(base, TCon) and base.name != "dyn"):
                return False
            tri = self.m.traits.get(dyn_t.args[0].name)
            if tri is None:
                return False
            # `box(value)`, a local `Box<T>`, and calls returning Box<T>
            # transfer the allocation into the trait object.
            if not (isinstance(expr, _ast.Name) or isinstance(expr, _ast.Call)):
                return False
            for owner in self.m.trait_lineage(tri.name):
                if base.name not in self.m.traits[owner].impls:
                    return False
            methods = self.m.trait_methods(tri.name)
            if len(methods) == 0:
                return False
            for mname, (owner, sig) in methods.items():
                impl = self.m.traits[owner].impls.get(base.name) or {}
                info = impl.get(mname)
                if info is None or info.generics:
                    return False
                if sig.self_mode is None:
                    return False
                if _contains_ref(sig.ret) or _contains_tvar(sig.ret):
                    return False
                if any(_contains_tvar(p) for p in sig.params):
                    return False
            expr.coerce_box_dyn = (tri.name, base)
            expr.ty = w
            return True
        if isinstance(w, TCon) and w.name == "String"                 and isinstance(g, TCon) and g.name == "str":
            expr.coerce_str_to_string = True
            expr.ty = w
            return True
        if not (isinstance(g, TCon) and isinstance(w, TCon)) or g.name != w.name                 or len(g.args) != len(w.args):
            return False
        if isinstance(expr, _ast.Call) and getattr(expr, "solved", None):
            params = getattr(expr, "param_types", None)
            if params is None:
                ref = expr.solved[1]
                if expr.solved[0] == "variant" and isinstance(ref, VariantRef):
                    info = self.m.enums[ref.enum]
                    mp = dict(zip(info.generics, w.args))
                    params = [subst(t, mp) for t in info.variants[ref.index][1]]
                else:
                    return False
            if len(params) != len(expr.args):
                return False
            ok = True
            for a, p in zip(expr.args, params):
                if not self.try_coerce(a, a.ty, p, depth + 1):
                    if not self.u.unify_ok(a.ty, p):
                        ok = False
            if not ok:
                return False
            expr.ty = w
            if hasattr(expr, "result_type"):
                expr.result_type = w
            return True
        if isinstance(expr, _ast.ListLit) and len(g.args) == 1:
            for item in expr.items:
                if not self.try_coerce(item, item.ty, w.args[0], depth + 1):
                    if not self.u.unify_ok(item.ty, w.args[0]):
                        return False
            expr.ty = w
            return True
        if isinstance(expr, _ast.StructLit):
            info = self.m.structs.get(w.name)
            if not info:
                return False
            mp = dict(zip(info.generics, w.args))
            by_name = dict(expr.fields)
            ok = True
            for fname, fty in info.fields:
                val = by_name.get(fname)
                if val is None:
                    continue
                if not self.try_coerce(val, val.ty, subst(fty, mp), depth + 1):
                    if not self.u.unify_ok(val.ty, subst(fty, mp)):
                        ok = False
            if ok:
                expr.ty = w
            return ok
        return False

    def unify(self, a, b, node, ctx=""):
        try:
            self.u.unify(a, b)
        except UnifyError as e:
            ra, rb = self.u.resolve_deep(a), self.u.resolve_deep(b)
            where = f" in {ctx}" if ctx else ""
            self.err(f"{e.msg}{where}: expected `{rb.show()}`, found `{ra.show()}`", node)

    # -- entry ------------------------------------------------------------
    def check(self):
        info = self.info
        for g, v in info.generic_vars.items():
            self.bind(g, v)
        for p, t in zip(info.decl.params, info.params):
            if p.name in self.scopes[0]:
                self.err(f"duplicate parameter '{p.name}'", p)
            self.bind(p.name, t)
        self.check_block(info.decl.body, new_scope=False)
        body = info.decl.body
        if body and isinstance(body[-1], ast.ExprStmt) and not _is_unit(info.ret):
            self.assignable(body[-1].expr, body[-1].expr.ty, info.ret,
                            body[-1], "function result")
        if not _is_unit(info.ret) and not always_returns(body, info.ret):
            self.err(
                f"function '{info.name}' can finish without returning a `{info.ret.show()}`",
                info.decl,
                "add a `return` on every path")
        return self

    def check_block(self, stmts, new_scope=True):
        if new_scope:
            self.push()
        for s in stmts:
            self.check_stmt(s)
        if new_scope:
            self.pop()

    def check_stmt(self, s):
        if isinstance(s, ast.Pass):
            return
        if isinstance(s, ast.Let):
            if s.ty is not None:
                declared = type_from_ast_checked(self.m, s.ty, self.info.generic_vars,
                                                 self.err, "binding")
                if s.init is None:
                    s.ty = self.u.resolve_deep(declared)
                    self.bind(s.name, s.ty)
                    return
                init_ty = self.infer(s.init)
                self.assignable(s.init, init_ty, declared, s, "let binding")
                s.ty = self.u.resolve_deep(declared)
            else:
                if s.init is None:
                    self.err(f"binding '{s.name}' needs a type annotation or an initializer", s)
                init_ty = self.infer(s.init)
                s.ty = self.u.resolve_deep(s.init.ty if s.init.ty is not None else init_ty)
            self.bind(s.name, s.ty)

        elif isinstance(s, ast.Assign):
            target_ty = self.infer_place(s.target)
            s.target.ty = self.u.resolve_deep(target_ty)
            if s.op == "=":
                val_ty = self.infer(s.value)
                self.assignable(s.value, val_ty, target_ty, s, "assignment")
            else:
                val_ty = self.infer(s.value)
                base_op = s.op[:-1]
                res = self.binop_type(base_op, target_ty, val_ty, s)
                self.unify(res, target_ty, s, "compound assignment")

        elif isinstance(s, ast.ExprStmt):
            self.infer(s.expr)

        elif isinstance(s, ast.If):
            self.infer(s.cond, BOOL)
            self.check_block(s.then_body)
            for c, body in s.elifs:
                self.infer(c, BOOL)
                self.check_block(body)
            if s.else_body is not None:
                self.check_block(s.else_body)

        elif isinstance(s, ast.While):
            self.infer(s.cond, BOOL)
            self.loop_depth += 1
            self.check_block(s.body)
            self.loop_depth -= 1

        elif isinstance(s, ast.For):
            iter_ty = self.infer(s.iter)
            elem = self.for_element_type(iter_ty, s)
            s.elem_ty = elem
            self.push()
            self.bind(s.name, elem)
            self.loop_depth += 1
            self.check_block(s.body, new_scope=False)
            self.loop_depth -= 1
            self.pop()

        elif isinstance(s, ast.Return):
            want = self.info.ret
            if _is_unit(want):
                if s.value is not None:
                    got = self.infer(s.value)
                    if not _is_unit(got):
                        self.err(
                            f"function '{self.info.name}' returns no value, but this "
                            f"returns `{got.show()}`", s)
            else:
                if s.value is None:
                    self.err(f"expected a `{want.show()}` return value", s)
                got = self.infer(s.value)
                self.assignable(s.value, got, want, s, "return")

        elif isinstance(s, ast.Break):
            if self.loop_depth == 0:
                self.err("'break' outside of a loop", s)
        elif isinstance(s, ast.Continue):
            if self.loop_depth == 0:
                self.err("'continue' outside of a loop", s)

        elif isinstance(s, ast.UnsafeStmt):
            self.unsafe += 1
            self.check_block(s.body)
            self.unsafe -= 1
        else:
            self.err("unsupported statement", s)

    def for_element_type(self, t, node):
        t = self.u.resolve_deep(t)
        if isinstance(t, TCon) and t.name == "Range":
            return t.args[0]
        if isinstance(t, TRef):
            inner = self.u.resolve_deep(t.inner)
            if isinstance(inner, TCon) and inner.name == "List":
                return TRef(t.mut, inner.args[0])
            if isinstance(inner, TCon) and inner.name == "str":
                return TCon("char")
            self.err(f"cannot iterate over `{t.show()}`", node,
                     "use a range (`0..n`) or a reference to a List")
        if isinstance(t, TCon) and t.name == "List":
            elem = t.args[0]
            if not self.m.is_copy(elem):
                self.err(f"cannot iterate over `{t.show()}` by value because it owns "
                         f"`{elem.show()}`", node,
                         "iterate over a reference instead: `for x in &list`")
            return elem
        if isinstance(t, TCon) and t.name == "str":
            return TCon("char")
        self.err(f"cannot iterate over `{t.show()}`", node,
                 "use a range (`0..n`) or a reference to a List")

    # -- identifier / place type -----------------------------------------
    def infer_place(self, e):
        if isinstance(e, ast.Name):
            t = self.lookup(e.name)
            if t is None:
                self.err(f"unknown variable '{e.name}'", e)
            e.ty = self.u.resolve_deep(t)
            return e.ty
        if isinstance(e, ast.Field):
            base = self.infer_place(e.base) if _is_place(e.base) else self.infer(e.base)
            e.ty = self.field_type(base, e)
            return e.ty
        if isinstance(e, ast.Index):
            base = self.infer(e.base)
            self.infer(e.index, USIZE)
            e.ty = self.index_type(base, e)
            return e.ty
        if isinstance(e, ast.Deref):
            base = self.infer(e.base)
            e.ty = self.deref_type(base, e)
            return e.ty
        self.err("invalid place expression", e)

    def _owned_place_root(self, e):
        while isinstance(e, (ast.Field, ast.Index, ast.Deref)):
            e = e.base
        return e

    def require_owned_place(self, e, t, what):
        """Reject field/method access on a temporary Box/Rc receiver.

        A temporary `box(...)`/`rc(...)` has no binding that can carry a drop
        flag. Allowing `.field`, `.method()` or `*temp` would leak the allocation
        (and any owned payload). Requiring a local makes destruction exact.
        """
        if not (isinstance(t, TCon) and t.name in ("Box", "Rc")):
            return
        # Owned trait objects have dedicated temporary cleanup in codegen
        # (emit_dyn_method drops the temporary Box after the call).
        if (t.name == "Box" and len(t.args) == 1
                and isinstance(t.args[0], TCon) and t.args[0].name == "dyn"):
            return
        if not isinstance(self._owned_place_root(e), ast.Name):
            self.err(f"temporary `{t.show()}` used for {what}", e,
                     "bind the Box/Rc to a local so it is dropped exactly once")

    def field_type(self, base, e):
        t = self.u.resolve_deep(base)
        if isinstance(t, TRef):
            t = self.u.resolve_deep(t.inner)
        if isinstance(t, TCon) and t.name in ("Box", "Rc"):
            self.require_owned_place(e.base, t, "field access")
            t = self.u.resolve_deep(t.args[0])
        if isinstance(t, TCon):
            ft = self.m.struct_field(t, e.field)
            if ft is not None:
                return ft
            if t.name in ("List", "Option", "Result", "str", "String"):
                self.err(f"`{t.show()}` has no field '{e.field}'", e,
                         "use a method call instead, e.g. `.len()`")
            self.err(f"unknown field '{e.field}' on `{t.show()}`", e)
        self.err(f"cannot access field '{e.field}' on `{t.show()}`", e)

    def index_type(self, base, e):
        t = self.u.resolve_deep(base)
        if isinstance(t, TRef):
            t = self.u.resolve_deep(t.inner)
        if isinstance(t, TCon) and t.name == "List":
            return t.args[0]
        self.err(f"cannot index `{t.show()}`", e)

    def deref_type(self, base, e):
        t = self.u.resolve_deep(base)
        if isinstance(t, TCon) and t.name in ("Box", "Rc"):
            self.require_owned_place(e.base, t, "dereference")
            return t.args[0]
        if isinstance(t, TRef):
            return t.inner
        if isinstance(t, TPtr):
            if self.unsafe == 0:
                self.err("dereferencing a raw pointer requires an `unsafe:` block", e)
            return t.inner
        self.err(f"cannot dereference `{t.show()}`", e)

    # -- expressions ------------------------------------------------------
    def infer(self, e, expected=None):
        t = self.infer_expr(e)
        if expected is not None:
            self.unify(t, expected, e)
        e.ty = self.u.resolve(t)
        return e.ty

    def lambda_free_vars(self, body, params):
        found = []

        def add(name, bound):
            if name in bound or name in found:
                return
            if self.lookup(name) is not None:
                found.append(name)

        def pattern_names(p, bound):
            if isinstance(p, ast.PatBind):
                bound.add(p.name)
            elif isinstance(p, ast.PatVariant):
                for a in p.args:
                    pattern_names(a, bound)

        def expr(e, bound):
            if isinstance(e, ast.Name):
                add(e.name, bound)
            elif isinstance(e, ast.Field):
                expr(e.base, bound)
            elif isinstance(e, ast.Index):
                expr(e.base, bound)
                expr(e.index, bound)
            elif isinstance(e, (ast.Deref, ast.Ref, ast.Cast, ast.Try)):
                expr(e.base, bound)
            elif isinstance(e, ast.Unary):
                expr(e.operand, bound)
            elif isinstance(e, ast.Binary):
                expr(e.left, bound)
                expr(e.right, bound)
            elif isinstance(e, ast.Call):
                expr(e.callee, bound)
                for a in e.args:
                    expr(a, bound)
            elif isinstance(e, ast.StructLit):
                for _, v in e.fields:
                    expr(v, bound)
            elif isinstance(e, ast.ListLit):
                for v in e.items:
                    expr(v, bound)
            elif isinstance(e, ast.Range):
                if e.start is not None:
                    expr(e.start, bound)
                if e.end is not None:
                    expr(e.end, bound)
            elif isinstance(e, ast.StrLit):
                for seg in e.segs:
                    if seg.kind == "expr":
                        expr(seg.parts[0], bound)
            elif isinstance(e, ast.Match):
                expr(e.scrutinee, bound)
                for arm in e.arms:
                    b = set(bound)
                    pattern_names(arm.pattern, b)
                    expr(arm.body, b)
            elif isinstance(e, ast.BlockExpr):
                stmts(e.stmts, bound)
            elif isinstance(e, ast.UnsafeExpr):
                stmts(e.body, bound)
            elif isinstance(e, ast.Lambda):
                b = set(bound)
                for p in e.params:
                    b.add(p.name)
                expr(e.body, b)
            elif isinstance(e, ast.AsmExpr):
                for v in e.operands:
                    expr(v, bound)

        def stmts(items, bound):
            for st in items:
                if isinstance(st, ast.Let):
                    if st.init is not None:
                        expr(st.init, bound)
                    bound.add(st.name)
                elif isinstance(st, ast.Assign):
                    expr(st.target, bound)
                    expr(st.value, bound)
                elif isinstance(st, ast.ExprStmt):
                    expr(st.expr, bound)
                elif isinstance(st, ast.If):
                    expr(st.cond, bound)
                    stmts(st.then_body, set(bound))
                    for c, body in st.elifs:
                        expr(c, bound)
                        stmts(body, set(bound))
                    if st.else_body is not None:
                        stmts(st.else_body, set(bound))
                elif isinstance(st, ast.While):
                    expr(st.cond, bound)
                    stmts(st.body, set(bound))
                elif isinstance(st, ast.For):
                    expr(st.iter, bound)
                    b = set(bound)
                    b.add(st.name)
                    stmts(st.body, b)
                elif isinstance(st, ast.Return):
                    if st.value is not None:
                        expr(st.value, bound)
                elif isinstance(st, ast.UnsafeStmt):
                    stmts(st.body, set(bound))
                elif isinstance(st, ast.Defer):
                    expr(st.expr, bound)

        expr(body, set(params))
        return found

    def infer_expr(self, e) -> object:
        if isinstance(e, ast.IntLit):
            return self.fresh(cons=frozenset({"int"}))
        if isinstance(e, ast.FloatLit):
            return self.fresh(cons=frozenset({"float"}))
        if isinstance(e, ast.BoolLit):
            return BOOL
        if isinstance(e, ast.UnitLit):
            return UNIT
        if isinstance(e, ast.CharLit):
            return TCon("char")
        if isinstance(e, ast.StrLit):
            any_expr = False
            for seg in e.segs:
                if seg.kind == "expr":
                    any_expr = True
                    seg.expr_ty = self.infer(seg.parts[0])
            return STRING if any_expr else STR
        if isinstance(e, ast.Name):
            return self.infer_name(e)
        if isinstance(e, ast.Field):
            if isinstance(e.base, ast.Name) and e.base.name not in self.scopes[-1] \
                    and self.lookup(e.base.name) is None and e.base.name in self.m.enums:
                enum = self.m.enums[e.base.name]
                self.require_visible(enum, e, enum.name)
                ref = self.m.variant_ref(enum.name, e.field)
                if ref is None:
                    self.err(f"enum '{enum.name}' has no variant '{e.field}'", e)
                t, payload = self.variant_value_type(ref, e, [])
                if payload:
                    self.err(f"variant '{enum.name}.{e.field}' takes "
                             f"{len(payload)} argument(s)", e)
                return t
            base = self.infer(e.base)
            return self.field_type(base, e)
        if isinstance(e, ast.Index):
            base = self.infer(e.base)
            self.infer(e.index, USIZE)
            res = self.index_type(base, e)
            if not self.m.is_copy(res):
                self.err(
                    f"indexing `{base.show()}` copies `{res.show()}`, which is not Copy",
                    e, "borrow the element with `&list[i]` or use `.pop()`")
            return res
        if isinstance(e, ast.Deref):
            base = self.infer(e.base)
            return self.deref_type(base, e)
        if isinstance(e, ast.Ref):
            inner = self.infer_place(e.base) if _is_place(e.base) else self.infer(e.base)
            if not _is_place(e.base):
                self.err("cannot borrow a temporary", e,
                         "bind it to a variable first")
            return TRef(e.mut, inner)
        if isinstance(e, ast.Unary):
            operand = self.infer(e.operand)
            if e.op == "-":
                if not _numeric_like(self.u.resolve_deep(operand)):
                    self.err(f"unary '-' needs a number, found `{operand.show()}`", e)
                return operand
            if e.op == "!":
                self.unify(operand, BOOL, e)
                return BOOL
            if e.op == "~":
                if not _int_like(self.u.resolve_deep(operand)):
                    self.err(f"unary '~' needs an integer, found `{operand.show()}`", e)
                return operand
            self.err(f"unknown unary operator '{e.op}'", e)
        if isinstance(e, ast.Binary):
            left = self.infer(e.left)
            right = self.infer(e.right)
            return self.binop_type(e.op, left, right, e)
        if isinstance(e, ast.Lambda):
            ptypes = []
            for p in e.params:
                ptypes.append(type_from_ast_checked(
                    self.m, p.ty, self.info.generic_vars, self.err,
                    "closure parameter"))
            free = self.lambda_free_vars(e.body, {p.name for p in e.params})
            captures = []
            for cname in free:
                ct = self.u.resolve_deep(self.lookup(cname))
                if isinstance(ct, TPtr):
                    self.err(f"cannot capture raw pointer `{cname}` in a closure", e)
                if isinstance(ct, TRef) and ct.mut:
                    self.err(f"cannot capture mutable reference `{cname}` in a closure",
                             e, "capture a shared reference instead")
                captures.append((cname, ct))
            self.push()
            base = len(self.scopes) - 1
            for p, pt in zip(e.params, ptypes):
                self.bind(p.name, pt)
            for cname, ct in captures:
                self.bind(cname, ct if self.m.is_copy(ct) else TRef(False, ct))
            old = self.lambda_base
            self.lambda_base = base
            body_ty = self.infer(e.body)
            self.lambda_base = old
            self.pop()
            if isinstance(self.u.resolve_deep(body_ty), TRef):
                self.err("closures cannot return borrows yet", e)
            e.param_types = [self.u.resolve_deep(t) for t in ptypes]
            e.result_type = self.u.resolve_deep(body_ty)
            if not captures:
                return TFn(e.param_types, e.result_type)
            if old is not None:
                self.err("nested capturing closures are not supported yet", e)
            cname = f"__closure_{len(self.m.closures)}"
            self.m.closures[cname] = ClosureInfo(
                name=cname, params=list(e.param_types),
                ret=self.u.resolve_deep(e.result_type), captures=captures,
                decl=e)
            e.captures = captures
            e.closure_name = cname
            return TCon(cname, [])

        if isinstance(e, ast.Try):
            if self.lambda_base is not None:
                self.err("`?` cannot be used inside a closure yet", e)
            base_t = self.u.resolve_deep(self.infer(e.base))
            fn_ret = self.u.resolve_deep(self.info.ret)
            if isinstance(base_t, TCon) and base_t.name == "Result":
                if not (isinstance(fn_ret, TCon) and fn_ret.name == "Result"):
                    self.err("`?` on a Result needs the enclosing function to return Result",
                             e, f"this function returns `{fn_ret.show()}`")
                err_t = base_t.args[1]
                self.unify(fn_ret.args[1], err_t, e, "`?` error type")
                e.try_kind = "Result"
                e.result_type = base_t.args[0]
                e.solved = ("try", "Result")
            elif isinstance(base_t, TCon) and base_t.name == "Option":
                if not (isinstance(fn_ret, TCon) and fn_ret.name == "Option"):
                    self.err("`?` on an Option needs the enclosing function to return Option",
                             e, f"this function returns `{fn_ret.show()}`")
                e.try_kind = "Option"
                e.result_type = base_t.args[0]
                e.solved = ("try", "Option")
            else:
                self.err(f"`?` needs a Result or Option, found `{base_t.show()}`", e)
            return e.result_type

        if isinstance(e, ast.Cast):
            src = self.infer(e.base)
            target = type_from_ast_checked(self.m, e.target, self.info.generic_vars,
                                           self.err, "cast")
            self.check_cast(src, target, e)
            return target
        if isinstance(e, ast.Call):
            return self.infer_call(e)
        if isinstance(e, ast.StructLit):
            return self.infer_struct_lit(e)
        if isinstance(e, ast.ListLit):
            elem = self.fresh(hint="element")
            for item in e.items:
                it = self.infer(item, elem)
                self.unify(it, elem, item)
            return TCon("List", [elem])
        if isinstance(e, ast.Range):
            elem = self.fresh(cons=frozenset({"int"}))
            if e.start is not None:
                self.infer(e.start, elem)
            if e.end is not None:
                self.infer(e.end, elem)
            return TCon("Range", [elem])
        if isinstance(e, ast.Match):
            return self.infer_match(e)
        if isinstance(e, ast.AsmExpr):
            if self.unsafe == 0:
                self.err("inline assembly requires an `unsafe:` block", e)
            for opnd in e.operands:
                self.infer(opnd)
            return UNIT
        if isinstance(e, ast.BlockExpr):
            for s in e.stmts:
                self.infer(s)
            return UNIT
        if isinstance(e, ast.UnsafeExpr):
            self.unsafe += 1
            self.check_block(e.body)
            self.unsafe -= 1
            return UNIT
        self.err(f"unsupported expression {type(e).__name__}", e)

    def infer_name(self, e):
        t = self.lookup(e.name)
        if t is not None:
            return self.u.resolve_deep(t)
        ref = self.m.find_unqualified_variant(e.name)
        if ref is not None:
            enum = self.m.enums[ref.enum]
            self.require_visible(enum, e, enum.name)
            _, tys = enum.variants[ref.index]
            if tys:
                self.err(f"variant '{ref.enum}.{e.name}' needs {len(tys)} argument(s)", e)
            return TCon(ref.enum, [self.fresh(hint=g) for g in enum.generics])
        if e.name in self.m.fns:
            info = self.m.fns[e.name]
            self.require_visible(info, e, e.name)
            if info.generics:
                self.err(f"generic function '{e.name}' needs a monomorphic context "
                         f"to be used as a value", e)
            e.fn_ref = info
            return info.scheme.type
        self.err(f"unknown name '{e.name}'", e)

    def variant_value_type(self, ref: VariantRef, node, arg_types):
        enum = self.m.enums[ref.enum]
        _, payload = enum.variants[ref.index]
        args = [self.fresh(hint=g) for g in enum.generics]
        mp = dict(zip(enum.generics, args))
        return TCon(ref.enum, args), [subst(p, mp) for p in payload]

    def infer_struct_lit(self, e):
        info = self.m.structs.get(e.name)
        if info is None:
            self.err(f"unknown struct '{e.name}'", e)
        self.require_visible(info, e, e.name)
        args = [self.fresh(hint=g) for g in info.generics]
        t = TCon(e.name, args)
        mp = dict(zip(info.generics, args))
        given = {}
        for fname, val in e.fields:
            if fname in given:
                self.err(f"field '{fname}' is given twice", e)
            ft = self.m.struct_field(t, fname)
            if ft is None:
                self.err(f"struct '{e.name}' has no field '{fname}'", e)
            given[fname] = val
            vt = self.infer(val)
            self.assignable(val, vt, ft, val, f"field '{fname}'")
        for fname, _ in info.fields:
            if fname not in given:
                self.err(f"missing field '{fname}' in `{e.name}` literal", e)
        return t

    def check_cast(self, src, target, e):
        s = self.u.resolve_deep(src)
        t = self.u.resolve_deep(target)
        if is_numeric(s) and is_numeric(t):
            return
        if isinstance(s, TCon) and s.name == "bool" and is_numeric(t):
            return
        if isinstance(s, TCon) and s.name == "char" and (is_numeric(t)):
            return
        if isinstance(t, TPtr) and (isinstance(s, TPtr) or is_int(s) or isinstance(s, TRef)):
            if self.unsafe == 0:
                self.err("pointer casts require an `unsafe:` block", e)
            return
        if isinstance(s, TPtr) and is_int(t):
            if self.unsafe == 0:
                self.err("pointer casts require an `unsafe:` block", e)
            return
        self.err(f"cannot cast `{s.show()}` to `{t.show()}`", e)

    # -- operators --------------------------------------------------------
    def binop_type(self, op, left, right, node):
        l = self.u.resolve_deep(left)
        r = self.u.resolve_deep(right)

        if op == "+" and (_is_stringy(l) or _is_stringy(r)):
            if _is_stringy(l) and _is_stringy(r):
                return STRING
            self.err(f"cannot concatenate `{l.show()}` and `{r.show()}`", node)

        if op in ("&&", "||"):
            self.unify(l, BOOL, node)
            self.unify(r, BOOL, node)
            return BOOL

        if op in ("==", "!="):
            if _is_stringy(l) and _is_stringy(r):
                return BOOL
            self.unify(l, r, node, f"operator '{op}'")
            lt2 = self.u.resolve_deep(l)
            if isinstance(lt2, TCon) and lt2.name == "unit":
                self.err("cannot compare unit values", node)
            if not (_numeric_like(lt2) or _is_stringy(lt2) or isinstance(lt2, (TCon, TRef, TPtr))):
                self.err(f"cannot compare `{l.show()}` values", node)
            return BOOL

        if op in ("<", "<=", ">", ">="):
            if _is_stringy(l) and _is_stringy(r):
                return BOOL
            self.unify(l, r, node, f"operator '{op}'")
            if not (_numeric_like(self.u.resolve_deep(l)) or _is_stringy(self.u.resolve_deep(l))
                    or (isinstance(self.u.resolve_deep(l), TCon)
                        and self.u.resolve_deep(l).name == "char")):
                self.err(f"cannot order `{l.show()}` values", node)
            return BOOL

        if op in ("&", "|", "^", "<<", ">>"):
            if not _int_like(l):
                self.err(f"operator '{op}' needs integers, found `{l.show()}`", node)
            self.unify(l, r, node, f"operator '{op}'")
            return l

        if op in ("+", "-", "*", "/", "%", "**"):
            if not _numeric_like(l):
                self.err(f"operator '{op}' needs numbers, found `{l.show()}`", node)
            self.unify(l, r, node, f"operator '{op}'")
            res = self.u.resolve_deep(l)
            if op == "**" and _int_like(res):
                return res
            return res

        self.err(f"unknown operator '{op}'", node)

    # -- calls ------------------------------------------------------------
    def infer_call(self, e):
        callee = e.callee

        if isinstance(callee, ast.Field):
            base = callee.base
            # Enum variant constructor: Shape.Circle(...)
            if isinstance(base, ast.Name) and self.lookup(base.name) is None \
                    and base.name in self.m.enums:
                self.require_visible(self.m.enums[base.name], e, base.name)
                ref = self.m.variant_ref(base.name, callee.field)
                if ref is None:
                    self.err(f"enum '{base.name}' has no variant '{callee.field}'", e)
                t, payload = self.variant_value_type(ref, e, None)
                if len(payload) != len(e.args):
                    self.err(f"variant '{base.name}.{callee.field}' takes {len(payload)} "
                             f"argument(s), got {len(e.args)}", e)
                for a, want in zip(e.args, payload):
                    got = self.infer(a)
                    self.assignable(a, got, want, a, "variant argument")
                e.solved = ("variant", ref)
                e.result_type = t
                return t

            recv_ty = self.infer(base)
            return self.infer_method_call(e, recv_ty)

        if isinstance(callee, ast.Name):
            name = callee.name
            if name in BUILTIN_FNS:
                return self.infer_builtin(e, name)
            if name in self.m.fns:
                info = self.m.fns[name]
                self.require_visible(info, e, name)
                inst, mapping = self.u.instantiate(info.scheme)
                params = inst.params
                if len(params) != len(e.args):
                    self.err(f"function '{name}' takes {len(params)} argument(s), "
                             f"got {len(e.args)}", e)
                for a, want in zip(e.args, params):
                    got = self.infer(a)
                    self.assignable(a, got, want, a, f"argument of '{name}'")
                self.check_bounds(info, mapping, e)
                e.solved = ("fn", info)
                e.inst = mapping
                e.param_types = [self.u.resolve_deep(p) for p in params]
                e.result_type = inst.ret
                return inst.ret
            ref = self.m.find_unqualified_variant(name)
            if ref is not None:
                enum = self.m.enums[ref.enum]
                self.require_visible(enum, e, enum.name)
                _, payload = enum.variants[ref.index]
                if len(payload) != len(e.args):
                    self.err(f"variant '{ref.enum}.{name}' takes {len(payload)} "
                             f"argument(s), got {len(e.args)}", e)
                t, wants = self.variant_value_type(ref, e, None)
                for a, want in zip(e.args, wants):
                    got = self.infer(a)
                    self.assignable(a, got, want, a, "variant argument")
                e.solved = ("variant", ref)
                e.result_type = t
                return t
            var_ty = self.lookup(name)
            if var_ty is not None:
                ft = self.u.resolve_deep(var_ty)
                if isinstance(ft, TCon) and ft.name in self.m.closures:
                    ci = self.m.closures[ft.name]
                    if len(ci.params) != len(e.args):
                        self.err(f"closure takes {len(ci.params)} argument(s), "
                                 f"got {len(e.args)}", e)
                    for a, want in zip(e.args, ci.params):
                        got = self.infer(a)
                        self.assignable(a, got, want, a, "closure argument")
                    e.solved = ("closure", name)
                    e.param_types = [self.u.resolve_deep(p) for p in ci.params]
                    e.result_type = ci.ret
                    return ci.ret
                if isinstance(ft, TFn):
                    if len(ft.params) != len(e.args):
                        self.err(f"expected {len(ft.params)} argument(s), got {len(e.args)}", e)
                    for a, want in zip(e.args, ft.params):
                        got = self.infer(a)
                        self.assignable(a, got, want, a, "function argument")
                    if isinstance(self.u.resolve_deep(ft.ret), TRef):
                        self.err("function pointers cannot return borrows yet", e)
                    e.solved = ("fnptr", name)
                    e.result_type = ft.ret
                    return ft.ret
                self.err(f"'{name}' is not callable (`{ft.show()}`)", e)
            self.err(f"unknown function '{name}'", e)

        self.err("unsupported call target", e)

    def infer_builtin(self, e, name):
        if name in ("print", "println", "write"):
            for a in e.args:
                self.infer(a)
            e.solved = ("builtin", name)
            return UNIT
        if name == "panic":
            if len(e.args) != 1:
                self.err("panic() takes exactly one message argument", e)
            self.infer(e.args[0])
            e.solved = ("builtin", name)
            return UNIT
        if name == "assert":
            if not 1 <= len(e.args) <= 2:
                self.err("assert() takes a condition and an optional message", e)
            self.infer(e.args[0], BOOL)
            for a in e.args[1:]:
                self.infer(a)
            e.solved = ("builtin", name)
            return UNIT
        if name == "exit":
            if len(e.args) != 1:
                self.err("exit() takes one integer code", e)
            self.infer(e.args[0], I64)
            e.solved = ("builtin", name)
            return UNIT
        if name == "len":
            if len(e.args) != 1:
                self.err("len() takes exactly one argument", e)
            t = self.u.resolve_deep(self.infer(e.args[0]))
            if isinstance(t, TRef):
                t = self.u.resolve_deep(t.inner)
            if isinstance(t, TCon) and t.name in ("List", "str", "String"):
                e.solved = ("builtin", name)
                return USIZE
            self.err(f"len() is not defined for `{t.show()}`", e)
        if name == "to_str":
            if len(e.args) != 1:
                self.err("to_str() takes exactly one argument", e)
            self.infer(e.args[0])
            e.solved = ("builtin", name)
            return STRING
        if name in ("min", "max"):
            if len(e.args) != 2:
                self.err(f"{name}() takes two arguments", e)
            t = self.infer(e.args[0])
            got = self.infer(e.args[1], t)
            self.unify(got, t, e)
            if not _numeric_like(self.u.resolve_deep(t)):
                self.err(f"{name}() needs numbers, found `{t.show()}`", e)
            e.solved = ("builtin", name)
            return t
        if name == "abs":
            if len(e.args) != 1:
                self.err("abs() takes one argument", e)
            t = self.infer(e.args[0])
            if not _numeric_like(self.u.resolve_deep(t)):
                self.err(f"abs() needs a number, found `{t.show()}`", e)
            e.solved = ("builtin", name)
            return t
        if name == "sqrt":
            if len(e.args) != 1:
                self.err("sqrt() takes one argument", e)
            t = self.infer(e.args[0])
            self.unify(t, TCon("f64"), e)
            e.solved = ("builtin", name)
            return TCon("f64")
        if name == "drop":
            if len(e.args) != 1:
                self.err("drop() takes one argument", e)
            t = self.infer(e.args[0])
            e.solved = ("builtin", name)
            return UNIT
        if name == "read_file":
            if len(e.args) != 1:
                self.err("read_file() takes a path", e)
            t = self.infer(e.args[0])
            if not _is_stringy(self.u.resolve_deep(t)):
                self.err(f"read_file() needs a path string, found `{t.show()}`", e)
            e.solved = ("builtin", name)
            return TCon("Result", [STRING, STRING])
        if name == "write_file":
            if len(e.args) != 2:
                self.err("write_file() takes a path and contents", e)
            for a in e.args:
                t = self.infer(a)
                if not _is_stringy(self.u.resolve_deep(t)):
                    self.err(f"write_file() needs strings, found `{t.show()}`", e)
            e.solved = ("builtin", name)
            return TCon("Result", [UNIT, STRING])
        if name == "env":
            if len(e.args) != 1:
                self.err("env() takes a variable name", e)
            t = self.infer(e.args[0])
            if not _is_stringy(self.u.resolve_deep(t)):
                self.err(f"env() needs a string, found `{t.show()}`", e)
            e.solved = ("builtin", name)
            return TCon("Option", [STRING])
        if name == "args":
            if e.args:
                self.err("args() takes no arguments", e)
            e.solved = ("builtin", name)
            return TCon("List", [STRING])
        if name == "now_ms":
            if e.args:
                self.err("now_ms() takes no arguments", e)
            e.solved = ("builtin", name)
            return TCon("u64")
        if name == "box":
            if len(e.args) != 1:
                self.err("box() takes exactly one value", e)
            inner = self.infer(e.args[0])
            e.solved = ("builtin", name)
            return TCon("Box", [inner])
        if name == "rc":
            if len(e.args) != 1:
                self.err("rc() takes exactly one value", e)
            inner = self.infer(e.args[0])
            e.solved = ("builtin", name)
            return TCon("Rc", [inner])
        self.err(f"unknown builtin '{name}'", e)

    def infer_method_call(self, e, recv_ty):
        name = e.callee.field
        t = self.u.resolve_deep(recv_ty)
        inner = t.inner if isinstance(t, TRef) else t
        if isinstance(inner, TVar) and inner.cons:
            d = self.u.default(inner)
            if not (isinstance(d, TVar) and d.id == inner.id):
                self.u.unify(inner, d)
                t = self.u.resolve_deep(t)
                inner = t.inner if isinstance(t, TRef) else t

        if isinstance(t, TCon) and t.name in ("Box", "Rc"):
            self.require_owned_place(e.callee.base, t, "method access")

        boxed_ref = isinstance(t, TRef)
        boxed_dyn = (isinstance(inner, TCon) and inner.name == "Box"
                     and len(inner.args) == 1
                     and isinstance(inner.args[0], TCon)
                     and inner.args[0].name == "dyn")
        if boxed_dyn:
            boxed_inner = inner.args[0]
            if not boxed_ref:
                t = boxed_inner
            inner = boxed_inner

        # Field access already auto-dereferences Box/Rc. Method calls should be
        # just as usable: `boxed_string.len()` means `(*boxed_string).len()`.
        # A consuming method is rejected later by consume_place, so this only
        # changes the receiver path, not ownership rules.
        if isinstance(inner, TCon) and inner.name == "Box":
            elem = self.u.resolve_deep(inner.args[0])
            e.callee.base = ast.Deref(
                base=e.callee.base,
                line=getattr(e.callee, "line", 0),
                col=getattr(e.callee, "col", 0),
                ty=elem,
            )
            return self.infer_method_call(e, elem)

        if isinstance(inner, TCon) and inner.name == "dyn":
            tri = self.m.traits.get(inner.args[0].name)
            if tri is None:
                self.err(f"unknown trait '{inner.args[0].name}'", e)
            found = self.m.trait_method(tri.name, name)
            if found is None:
                self.err(f"trait '{tri.name}' has no method '{name}'", e)
            sig = found[1]
            if sig.self_mode is None:
                self.err(f"`dyn {tri.name}` cannot call '{name}' (takes self by value)",
                         e)
            if sig.self_mode == "&mut":
                if boxed_ref:
                    if not t.mut:
                        self.err(f"method '{name}' needs `&mut dyn {tri.name}`", e)
                elif not boxed_dyn and not (isinstance(t, TRef) and t.mut):
                    self.err(f"method '{name}' needs `&mut dyn {tri.name}`", e)
            params = list(sig.params[1:])
            if len(params) != len(e.args):
                self.err(f"trait method '{tri.name}.{name}' takes {len(params)} "
                         f"argument(s), got {len(e.args)}", e)
            mp = {"Self": inner if boxed_dyn else t}
            for a, want in zip(e.args, params):
                got = self.infer(a)
                self.assignable(a, got, subst(want, mp), a, "dyn method argument")
            e.solved = ("dyn_method", tri.name, name)
            e.param_types = [self.u.resolve_deep(subst(p, mp)) for p in params]
            e.result_type = self.u.resolve_deep(subst(sig.ret, mp))
            return e.result_type

        if isinstance(inner, TCon) and inner.name == "Rc":
            if name == "clone":
                self.expect_arity(e, 0)
                e.solved = ("rc_method", name)
                return inner
            if name == "strong_count":
                self.expect_arity(e, 0)
                e.solved = ("rc_method", name)
                return USIZE
            self.err(f"`Rc` has no method '{name}'", e,
                     "Rc only supports clone() and strong_count()")

        if isinstance(inner, TCon) and inner.name == "Option":
            elem = inner.args[0]
            if name in ("unwrap", "expect", "unwrap_or", "ok_or")                     and isinstance(t, TRef) and not self.m.is_copy(elem):
                self.err(f"cannot move a non-Copy value out of `{t.show()}`", e,
                         "borrow the payload with `match` or clone it first")
            if name in ("is_some", "is_none"):
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = elem
                return BOOL
            if name == "unwrap":
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = elem
                return elem
            if name == "expect":
                self.expect_arity(e, 1)
                msg = self.infer(e.args[0])
                if not _is_stringy(self.u.resolve_deep(msg)):
                    self.err("expect() needs a message string", e)
                e.solved = ("enum_method", name)
                e.enum_elem = elem
                return elem
            if name == "unwrap_or":
                self.expect_arity(e, 1)
                self.infer(e.args[0], elem)
                e.solved = ("enum_method", name)
                e.enum_elem = elem
                return elem
            if name == "ok_or":
                self.expect_arity(e, 1)
                err_t = self.infer(e.args[0])
                e.solved = ("enum_method", name)
                e.enum_elem = elem
                e.enum_err = err_t
                return TCon("Result", [elem, err_t])
            self.err(f"`Option` has no method '{name}'", e)

        if isinstance(inner, TCon) and inner.name == "Result":
            ok_t, err_t = inner.args
            if name in ("unwrap", "expect", "unwrap_or", "ok", "err", "unwrap_err")                     and isinstance(t, TRef):
                moved = err_t if name in ("ok", "unwrap_err") else ok_t
                if not self.m.is_copy(moved):
                    self.err(f"cannot move a non-Copy value out of `{t.show()}`", e,
                             "borrow the payload with `match` or clone it first")
            if name in ("is_ok", "is_err"):
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return BOOL
            if name in ("unwrap", "expect"):
                if name == "expect":
                    self.expect_arity(e, 1)
                    msg = self.infer(e.args[0])
                    if not _is_stringy(self.u.resolve_deep(msg)):
                        self.err("expect() needs a message string", e)
                else:
                    self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return ok_t
            if name == "unwrap_or":
                self.expect_arity(e, 1)
                self.infer(e.args[0], ok_t)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return ok_t
            if name == "unwrap_err":
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return err_t
            if name == "ok":
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return TCon("Option", [ok_t])
            if name == "err":
                self.expect_arity(e, 0)
                e.solved = ("enum_method", name)
                e.enum_elem = ok_t
                e.enum_err = err_t
                return TCon("Option", [err_t])
            self.err(f"`Result` has no method '{name}'", e)

        if isinstance(inner, TCon) and inner.name in ("str", "String"):
            if name == "len":
                self.expect_arity(e, 0)
                e.solved = ("strmeth", "len")
                return USIZE
            if name == "is_empty":
                self.expect_arity(e, 0)
                e.solved = ("strmeth", "is_empty")
                return BOOL
            if name == "clone":
                self.expect_arity(e, 0)
                e.solved = ("strmeth", "clone")
                return STRING
            self.err(f"`{inner.show()}` has no method '{name}'", e)

        if isinstance(inner, TCon) and inner.name == "List":
            elem = inner.args[0]

            def shared_receiver():
                if isinstance(t, TRef):
                    if t.mut:
                        e.self_auto = "none"
                    else:
                        e.self_auto = "none"
                else:
                    e.self_auto = "ref"

            def mutable_receiver(method):
                if isinstance(t, TRef):
                    if not t.mut:
                        self.err(f"method '{method}' needs a mutable reference",
                                 e, "declare the list with `var` and use `&mut`")
                    e.self_auto = "none"
                else:
                    e.self_auto = "mutref"

            if name == "len":
                self.expect_arity(e, 0)
                shared_receiver()
                e.solved = ("list", "len")
                e.param_types = []
                return USIZE
            if name == "is_empty":
                self.expect_arity(e, 0)
                shared_receiver()
                e.solved = ("list", "is_empty")
                return BOOL
            if name == "push":
                self.expect_arity(e, 1)
                got = self.infer(e.args[0])
                self.assignable(e.args[0], got, elem, e.args[0], "list element")
                mutable_receiver("push")
                e.solved = ("list", "push")
                e.param_types = [elem]
                return UNIT
            if name == "pop":
                self.expect_arity(e, 0)
                mutable_receiver("pop")
                e.solved = ("list", "pop")
                return TCon("Option", [elem])
            if name == "get":
                self.expect_arity(e, 1)
                self.infer(e.args[0], USIZE)
                if not self.m.is_copy(elem):
                    self.err(f"get() copies `{elem.show()}`, which is not Copy", e,
                             "use `for x in &list` or `.pop()` for owning iteration")
                shared_receiver()
                e.solved = ("list", "get")
                e.param_types = [USIZE]
                return TCon("Option", [elem])
            if name == "set":
                self.expect_arity(e, 2)
                self.infer(e.args[0], USIZE)
                got = self.infer(e.args[1])
                self.assignable(e.args[1], got, elem, e.args[1], "list element")
                mutable_receiver("set")
                e.solved = ("list", "set")
                e.param_types = [USIZE, elem]
                return UNIT
            if name == "clear":
                self.expect_arity(e, 0)
                mutable_receiver("clear")
                e.solved = ("list", "clear")
                return UNIT
            self.err(f"`List` has no method '{name}'", e)

        if isinstance(inner, TVar):
            for tname in getattr(self.info.decl, "bounds", {}).get(inner.hint, []):
                tri = self.m.traits.get(tname)
                if tri is None:
                    continue
                found = self.m.trait_method(tname, name)
                if found is None:
                    continue
                owner_name, sig = found
                mapping = {}
                mp = {"Self": inner}
                sig_params = [subst(p, mp) for p in sig.params]
                params = list(sig_params)
                self_ty = params.pop(0)
                if isinstance(self_ty, TRef):
                    if isinstance(t, TRef):
                        if self_ty.mut and not t.mut:
                            self.err(f"method '{name}' needs a mutable reference", e)
                        auto = "none" if self_ty.mut == t.mut else "deref"
                        base_inner = t.inner
                    else:
                        auto = "mutref" if self_ty.mut else "ref"
                        base_inner = inner
                    self.unify(base_inner, self_ty.inner, e, "trait receiver")
                else:
                    if isinstance(t, TRef):
                        self.err(f"trait method '{name}' takes self by value", e)
                    auto = "none"
                    self.unify(inner, self_ty, e, "trait receiver")
                if len(params) != len(e.args):
                    self.err(f"trait method '{tname}.{name}' takes {len(params)} "
                             f"argument(s), got {len(e.args)}", e)
                for a, want in zip(e.args, params):
                    got = self.infer(a)
                    self.assignable(a, got, want, a, "trait method argument")
                e.solved = ("trait_method", owner_name, name)
                e.self_auto = auto
                e.trait_self_mode = sig.self_mode
                e.inst = mapping
                e.param_types = [self.u.resolve_deep(p) for p in params]
                e.result_type = subst(sig.ret, mp)
                return e.result_type

        if isinstance(inner, TCon):
            info = self.m.methods.get((inner.name, name))
            if info is not None:
                inst, mapping = self.u.instantiate(info.scheme)
                inst = self.u.resolve_deep(inst)
                params = list(inst.params)
                self_ty = params.pop(0)
                # Receiver adaptation: value, shared borrow or mutable borrow.
                if isinstance(self_ty, TRef):
                    if isinstance(t, TRef):
                        if self_ty.mut and not t.mut:
                            self.err(f"method '{name}' needs a mutable reference to "
                                     f"`{inner.show()}`", e,
                                     "declare the value with `var` and borrow it with `&mut`")
                        auto = "none" if self_ty.mut == t.mut else "deref"
                    else:
                        if self_ty.mut and not _is_mutable_place(e.callee.base):
                            pass  # reported by the borrow checker with a better message
                        auto = "mutref" if self_ty.mut else "ref"
                    base_inner = t.inner if isinstance(t, TRef) else t
                    self.unify(base_inner, self_ty.inner, e, "method receiver")
                else:
                    if isinstance(t, TRef):
                        if not self.m.is_copy(self_ty):
                            self.err(f"method '{name}' takes `self` by value, but the "
                                     f"receiver is borrowed", e)
                        auto = "deref"
                    else:
                        auto = "none"
                    self.unify(inner, self_ty, e, "method receiver")
                if len(params) != len(e.args):
                    self.err(f"method '{inner.name}.{name}' takes {len(params)} "
                             f"argument(s), got {len(e.args)}", e)
                for a, want in zip(e.args, params):
                    got = self.infer(a)
                    self.assignable(a, got, want, a, "method argument")
                self.check_bounds(info, mapping, e)
                e.solved = ("method", info)
                e.self_auto = auto
                e.inst = mapping
                e.param_types = [self.u.resolve_deep(p) for p in params]
                e.result_type = inst.ret
                return inst.ret
        self.err(f"`{inner.show()}` has no method '{name}'", e)

    def check_bounds(self, info, mapping, node):
        for g, bounds in getattr(info.decl, "bounds", {}).items():
            ov = info.generic_vars.get(g)
            if ov is None:
                continue
            t = self.u.resolve_deep(mapping.get(ov.id, ov))
            if not isinstance(t, TCon):
                continue
            for b in bounds:
                if b not in self.m.traits:
                    continue
                for owner in self.m.trait_lineage(b):
                    tri = self.m.traits[owner]
                    if t.name not in tri.impls:
                        self.err(f"`{t.show()}` does not implement trait `{owner}`",
                                 node, f"add `impl {owner} for {t.show()}`")

    def expect_arity(self, e, n):
        if len(e.args) != n:
            self.err(f"expected {n} argument(s), got {len(e.args)}", e)

    def require_mut_receiver(self, t, e, name):
        if not isinstance(t, TRef) or not t.mut:
            self.err(f"method '{name}' needs a mutable reference to `{t.show()}`", e,
                     f"call it on a `var` binding as `x.{name}(...)`")

    # -- match ------------------------------------------------------------
    def infer_match(self, e):
        scrut = self.infer(e.scrutinee)
        st = self.u.resolve_deep(scrut)
        if isinstance(st, TRef):
            if not self.m.is_copy(st.inner):
                self.err(f"cannot match through `{st.show()}` because `{st.inner.show()}` "
                         f"is not Copy", e, "match on the value itself, or borrow fields")
            match_ty = self.u.resolve_deep(st.inner)
            e.match_by_ref = True
        else:
            match_ty = st
            e.match_by_ref = False

        result_ty = None
        for arm in e.arms:
            self.push()
            self.check_pattern(arm.pattern, match_ty, arm)
            body_ty = self.infer(arm.body)
            if result_ty is None:
                result_ty = self.u.resolve_deep(body_ty)
            else:
                self.assignable(arm.body, body_ty, result_ty, arm.body)
            arm.bindings = getattr(arm, "bindings", [])
            self.pop()
        self.check_exhaustive(e, match_ty)
        e.match_ty = match_ty
        return result_ty if result_ty is not None else UNIT

    def check_pattern(self, pat, ty, arm):
        ty = self.u.resolve_deep(ty)
        if isinstance(pat, ast.PatWild):
            return
        if isinstance(pat, ast.PatBind):
            pat.ty = ty
            if not hasattr(arm, "bindings"):
                arm.bindings = []
            arm.bindings.append((pat.name, ty))
            self.bind(pat.name, ty)
            return
        if isinstance(pat, ast.PatLit):
            v = pat.value
            if isinstance(v, bool):
                self.unify(ty, BOOL, pat)
            elif isinstance(v, int):
                lit = self.fresh(cons=frozenset({"int"}))
                self.unify(ty, lit, pat)
            elif isinstance(v, float):
                lit = self.fresh(cons=frozenset({"float"}))
                self.unify(ty, lit, pat)
            elif isinstance(v, str):
                ty2 = self.u.resolve_deep(ty)
                if not (isinstance(ty2, TCon) and ty2.name in ("str", "String")):
                    self.unify(ty, STR, pat)
            else:
                self.unify(ty, TCon("char"), pat)
            return
        if isinstance(pat, ast.PatVariant):
            t = ty
            if not isinstance(t, TCon) or t.name not in self.m.enums:
                self.err(f"pattern '{pat.variant}' needs an enum value, found "
                         f"`{t.show()}`", pat)
            enum = self.m.enums[t.name]
            if pat.enum and pat.enum != t.name:
                self.err(f"pattern for enum '{pat.enum}' cannot match `{t.show()}`", pat)
            ref = self.m.variant_ref(t.name, pat.variant)
            if ref is None:
                names = ", ".join(v for v, _ in enum.variants)
                self.err(f"enum '{t.name}' has no variant '{pat.variant}'"
                         f" (variants: {names})", pat)
            pat.ref = ref
            mp = dict(zip(enum.generics, t.args))
            payload = enum.variants[ref.index][1]
            if len(payload) != len(pat.args):
                self.err(f"variant '{t.name}.{pat.variant}' has {len(payload)} field(s), "
                         f"pattern has {len(pat.args)}", pat)
            for sub, want in zip(pat.args, payload):
                self.check_pattern(sub, subst(want, mp), arm)
            return
        self.err("unknown pattern", pat)

    def check_exhaustive(self, e, ty):
        ty = self.u.resolve_deep(ty)
        covered = set()
        has_catchall = False
        for arm in e.arms:
            p = arm.pattern
            if isinstance(p, (ast.PatWild, ast.PatBind)):
                has_catchall = True
            elif isinstance(p, ast.PatLit):
                covered.add(p.value)
            elif isinstance(p, ast.PatVariant):
                covered.add(p.ref.index)
        if has_catchall:
            return
        if isinstance(ty, TCon) and ty.name in self.m.enums:
            enum = self.m.enums[ty.name]
            missing = [v for i, (v, _) in enumerate(enum.variants) if i not in covered]
            if missing:
                self.err(
                    f"match is not exhaustive; missing: {', '.join(missing)}", e,
                    "add the missing arm(s) or a `_` arm")
            return
        if isinstance(ty, TCon) and ty.name == "bool":
            if {True, False} - covered:
                self.err("match on bool is not exhaustive; add `_` or both `true`/`false`", e)
            return
        self.err(f"match on `{ty.show()}` needs a `_` arm", e)


def _is_place(e) -> bool:
    return isinstance(e, (ast.Name, ast.Field, ast.Index, ast.Deref))


def _is_mutable_place(e) -> bool:
    return _is_place(e)


def _numeric_like(t) -> bool:
    if isinstance(t, TCon):
        return t.name in NUM_TYPES
    if isinstance(t, TVar):
        return bool(t.cons & {"int", "float"})
    return False


def _int_like(t) -> bool:
    if isinstance(t, TCon):
        return t.name in INT_TYPES
    if isinstance(t, TVar):
        return "int" in t.cons
    return False


def _float_like(t) -> bool:
    if isinstance(t, TCon):
        return t.name in FLOAT_TYPES
    if isinstance(t, TVar):
        return "float" in t.cons
    return False


def _is_stringy(t) -> bool:
    if isinstance(t, TRef):
        t = t.inner
    return isinstance(t, TCon) and t.name in ("str", "String")


def _is_unit(t) -> bool:
    t = t
    return isinstance(t, TCon) and t.name == "unit"


def type_from_ast_checked(m: Module, node, generic_vars, err, ctx):
    def go(n):
        if isinstance(n, ast.TyDyn):
            if n.trait not in m.traits:
                err(f"unknown trait '{n.trait}' in dyn type", n)
            m.require_visible(m.traits[n.trait], n, n.trait)
            return TCon("dyn", [TCon(n.trait)])
        if isinstance(n, ast.TyName):
            name = TYPE_ALIASES.get(n.name, n.name)
            if name in generic_vars:
                return generic_vars[name]
            if name in PRIMS:
                return TCon(name)
            if name in ("List", "Box", "Rc"):
                if len(n.args) != 1:
                    err(f"{name}<T> expects exactly one type argument", n)
                return TCon(name, [go(n.args[0])])
            if name in m.structs:
                info = m.structs[name]
                m.require_visible(info, n, name)
                return TCon(name, [go(a) for a in n.args])
            if name in m.enums:
                info = m.enums[name]
                m.require_visible(info, n, name)
                return TCon(name, [go(a) for a in n.args])
            err(f"unknown type '{name}'", n)
        if isinstance(n, ast.TyRef):
            return TRef(n.mut, go(n.inner))
        if isinstance(n, ast.TyPtr):
            return TPtr(n.mut, go(n.inner))
        if isinstance(n, ast.TySlice):
            return TCon("List", [go(n.inner)])
        if isinstance(n, ast.TyFn):
            return TFn([go(p) for p in n.params], go(n.ret))
        if isinstance(n, ast.TyUnit):
            return UNIT
        err(f"invalid type in {ctx}", n)
    return go(node)


def always_returns(stmts, ret=None) -> bool:
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, ast.Return):
        return True
    if isinstance(last, ast.ExprStmt) and ret is not None and not _is_unit(ret):
        return True
    if isinstance(last, ast.If):
        if last.else_body is None:
            return False
        bodies = [last.then_body] + [b for _, b in last.elifs] + [last.else_body]
        return all(always_returns(b, ret) for b in bodies)
    if isinstance(last, ast.UnsafeStmt):
        return always_returns(last.body, ret)
    return False


def check_program(prog: ast.Program) -> Module:
    m = collect(prog)
    checker_infos = [i for i in (list(m.fns.values()) + list(m.methods.values())
                                 + list(m.default_methods))
                     if not i.decl.is_extern]
    for info in checker_infos:
        FnChecker(m, info).check()
    # Default every remaining constrained type variable.
    for info in checker_infos:
        protect = {v.id for v in info.generic_vars.values()}
        _default_fn_types(m, info, protect)
    # Borrow checking on the now-concrete tree.
    for info in checker_infos:
        BorrowChecker(m, info).check()
    return m


def _walk_exprs(node, fn):
    if isinstance(node, list):
        for x in node:
            _walk_exprs(x, fn)
        return
    if isinstance(node, ast.Expr):
        fn(node)
        for field_name in ("base", "operand", "left", "right", "callee", "scrutinee",
                           "index", "target", "value", "init", "cond", "iter", "body"):
            child = getattr(node, field_name, None)
            if isinstance(child, ast.Expr):
                _walk_exprs(child, fn)
        for attr in ("args", "items", "operands"):
            for child in getattr(node, attr, []) or []:
                _walk_exprs(child, fn)
        if isinstance(node, ast.StrLit):
            for seg in node.segs:
                if seg.kind == "expr":
                    for p in seg.parts:
                        _walk_exprs(p, fn)
        if isinstance(node, ast.Match):
            for arm in node.arms:
                _walk_exprs(arm.body, fn)
        if isinstance(node, ast.StructLit):
            for _, v in node.fields:
                _walk_exprs(v, fn)
        return
    if isinstance(node, ast.Stmt):
        for field_name in ("init", "value", "cond", "iter", "expr", "target"):
            child = getattr(node, field_name, None)
            if isinstance(child, ast.Expr):
                _walk_exprs(child, fn)
        for attr in ("then_body", "body", "else_body"):
            _walk_exprs(getattr(node, attr, None), fn)
        for c, b in getattr(node, "elifs", []) or []:
            _walk_exprs(c, fn)
            _walk_exprs(b, fn)
        return


def _walk_stmts(node, fn):
    if isinstance(node, list):
        for x in node:
            _walk_stmts(x, fn)
        return
    if isinstance(node, ast.Stmt):
        fn(node)
        for attr in ("then_body", "body", "else_body"):
            _walk_stmts(getattr(node, attr, None), fn)
        for _, body in getattr(node, "elifs", []) or []:
            _walk_stmts(body, fn)
        for attr in ("init", "value", "cond", "iter", "expr"):
            child = getattr(node, attr, None)
            if isinstance(child, ast.Match):
                for arm in child.arms:
                    _walk_stmts(arm.body, fn)
        return
    if isinstance(node, ast.Match):
        for arm in node.arms:
            _walk_stmts(arm.body, fn)


def _default_fn_types(m: Module, info: FnInfo, protect: set):
    def fix_stmt(s):
        if isinstance(s, ast.Let) and s.ty is not None:
            s.ty = m.u.default(s.ty, protect)
        if isinstance(s, ast.For) and getattr(s, "elem_ty", None) is not None:
            s.elem_ty = m.u.default(s.elem_ty, protect)

    def fix(e):
        if isinstance(e, ast.StrLit):
            for seg in e.segs:
                if seg.kind == "expr":
                    seg.expr_ty = m.u.default(seg.expr_ty, protect)
        if getattr(e, "ty", None) is not None:
            e.ty = m.u.default(e.ty, protect)
        if isinstance(e, ast.Call) and getattr(e, "param_types", None):
            e.param_types = [m.u.default(p, protect) for p in e.param_types]
        if isinstance(e, ast.Call) and getattr(e, "result_type", None) is not None:
            e.result_type = m.u.default(e.result_type, protect)

    _walk_exprs(info.decl.body, fix)
    _walk_stmts(info.decl.body, fix_stmt)
    info.params = [m.u.default(p, protect) for p in info.params]
    info.ret = m.u.default(info.ret, protect)
    info.scheme = Scheme(info.scheme.vars, TFn(info.params, info.ret))


# --------------------------------------------------------------------------
# Borrow checker
# --------------------------------------------------------------------------
@dataclass
class BVar:
    name: str
    ty: object
    mutable: bool
    line: int
    moved: bool = False
    uninit: bool = False
    moved_paths: set = field(default_factory=set)
    borrow_src: set | None = None
    borrow_mut: bool = False
    ref_borrow: object = None


@dataclass
class Borrow:
    roots: set
    mut: bool
    block: int
    line: int
    temp: bool = False


class BorrowChecker:
    def __init__(self, m: Module, info: FnInfo):
        self.m = m
        self.info = info
        self.scopes: list[dict[str, BVar]] = [{}]
        self.blocks: list[int] = [0]
        self.next_block = 1
        self.borrows: list[Borrow] = []
        self.loop_depth = 0
        self.unsafe = 0

    def err(self, msg, node, hint="", notes=None):
        raise BorrowError(msg, getattr(node, "line", 0), getattr(node, "col", 0),
                          hint, notes=notes)

    # -- scope ------------------------------------------------------------
    def push(self):
        self.scopes.append({})
        self.blocks.append(self.next_block)
        self.next_block += 1

    def pop(self):
        block = self.blocks.pop()
        self.borrows = [b for b in self.borrows if b.block != block]
        self.scopes.pop()

    def bind(self, name, ty, mutable, node, uninit=False,
             borrow_src=None, borrow_mut=False, ref_borrow=None):
        self.scopes[-1][name] = BVar(name, ty, mutable, getattr(node, "line", 0),
                                     uninit=uninit, borrow_src=borrow_src,
                                     borrow_mut=borrow_mut,
                                     ref_borrow=ref_borrow)

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        return None

    def check(self):
        info = self.info
        for p, t in zip(info.decl.params, info.params):
            src = {p.name} if isinstance(self.m.u.resolve_deep(t), TRef) else None
            if p.name == "self":
                self.bind("self", t, p.mode == "&mut", p, borrow_src=src,
                          borrow_mut=p.mode == "&mut")
            else:
                self.bind(p.name, t, False, p, borrow_src=src,
                          borrow_mut=isinstance(self.m.u.resolve_deep(t), TRef)
                          and self.m.u.resolve_deep(t).mut)
        self.check_block(info.decl.body, new_scope=False)

    def snapshot(self):
        return {id(bv): (bv.moved, bv.uninit, frozenset(bv.moved_paths), bv.name)
                for sc in self.scopes for bv in sc.values()}

    def restore(self, snap):
        for sc in self.scopes:
            for bv in sc.values():
                if id(bv) in snap:
                    bv.moved, bv.uninit, paths, _ = snap[id(bv)]
                    bv.moved_paths = set(paths)

    def merge(self, states):
        for sc in self.scopes:
            for bv in sc.values():
                entries = [st[id(bv)] for st in states if id(bv) in st]
                if entries:
                    bv.moved = any(e[0] for e in entries)
                    bv.uninit = any(e[1] for e in entries)
                    paths = set()
                    for e in entries:
                        paths.update(e[2])
                    bv.moved_paths = paths

    def check_block(self, stmts, new_scope=True):
        if new_scope:
            self.push()
        for s in stmts:
            mark = len(self.borrows)
            self.check_stmt(s)
            self.borrows = [b for b in self.borrows[:mark] if not b.temp] + \
                           [b for b in self.borrows[mark:] if not b.temp]
        if new_scope:
            self.pop()

    # -- borrow bookkeeping ----------------------------------------------
    def active_on(self, name):
        return [b for b in self.borrows if name in b.roots]

    def check_no_borrows(self, roots, node, action, ignore=None):
        for root in roots:
            act = [b for b in self.active_on(root) if b is not ignore]
            if act:
                b = act[0]
                self.err(f"cannot {action} `{root}` while it is borrowed", node,
                         "the borrow is still alive here",
                         notes=[(b.line, f"borrow of `{root}` starts here")])

    def check_mut_receiver(self, recv, roots, node):
        rt = self.m.u.resolve_deep(getattr(recv, "ty", None) or UNIT)
        if isinstance(rt, TRef) and rt.mut:
            self.check_no_borrows(roots, node, "mutate through")
            return
        self.check_mut_borrow(roots, node)

    def check_mut_borrow(self, roots, node, ignore=None):
        for root in roots:
            bv = self.lookup(root)
            if bv is None:
                continue
            rt = self.m.u.resolve_deep(bv.ty)
            if isinstance(rt, TCon) and rt.name == "Rc":
                self.err(f"cannot mutably borrow `{root}` through `Rc`", node,
                         "Rc provides shared access only")
            if isinstance(rt, TRef):
                if not rt.mut:
                    self.err(f"cannot mutate through shared reference `{root}`", node)
            elif not bv.mutable:
                self.err(f"cannot mutably borrow `{root}` because it was declared with "
                         f"`let`", node, "declare it with `var` to allow mutation")
            act = [b for b in self.active_on(root) if b is not ignore]
            if act:
                self.err(f"cannot borrow `{root}` as mutable while it is already "
                         f"borrowed", node, "wait until the first borrow ends",
                         notes=[(act[0].line, f"first borrow of `{root}` starts here")])

    def clear_temps(self, mark: int):
        self.borrows = self.borrows[:mark] + [b for b in self.borrows[mark:]
                                              if not b.temp]

    def add_borrow(self, roots, mut, node, temp=True):
        b = Borrow(set(roots), mut, self.blocks[-1],
                   getattr(node, "line", 0), temp)
        self.borrows.append(b)
        return b

    def move_var(self, bv, node, root_name=None):
        if bv.uninit:
            self.err(f"`{bv.name}` is moved before it is initialized", node)
        if bv.moved:
            self.err(f"use of moved value `{root_name or bv.name}`", node)
        if bv.moved_paths:
            what = self.field_label(bv.name, next(iter(bv.moved_paths)))
            self.err(f"cannot move `{root_name or bv.name}` because "
                     f"`{what}` was moved out of it", node,
                     "move the remaining fields individually or reassign "
                     "the whole value")
        act = self.active_on(bv.name)
        if act:
            self.err(f"cannot move `{bv.name}` while it is borrowed", node,
                     "wait until the borrow ends, or clone the value",
                     notes=[(act[0].line, f"borrow of `{bv.name}` starts here")])
        bv.moved = True

    # -- partial moves ----------------------------------------------------
    def place_path(self, e):
        """Return (root_name, field-path) for a plain aggregate place.

        Only Name/Field chains are tracked. Deref/Index and Rc payloads are
        deliberately excluded; a direct field chain through an owned Box is
        tracked because the Box remains the drop owner.
        """
        if isinstance(e, ast.Name):
            return (e.name, ())
        if isinstance(e, ast.Field):
            base = self.place_path(e.base)
            if base is None:
                return None
            return (base[0], base[1] + (e.field,))
        return None

    def field_label(self, root, path):
        return ".".join((root,) + tuple(path)) if path else root

    def path_related(self, a, b):
        """True if one field path is a prefix of the other."""
        return a[:len(b)] == b or b[:len(a)] == a

    def path_moved(self, bv, path):
        if bv.moved:
            return True
        return any(self.path_related(p, path) for p in bv.moved_paths)

    def check_partial_read(self, e):
        path = self.place_path(e)
        if path is None:
            return
        bv = self.lookup(path[0])
        if bv is None:
            return
        if path[1] and self.path_moved(bv, path[1]):
            self.err(f"use of moved field `{self.field_label(*path)}`", e,
                     "the field was moved earlier; clone it or reassign it")
        if not path[1] and bv.moved_paths:
            what = self.field_label(path[0], next(iter(bv.moved_paths)))
            self.err(f"use of partially moved value `{path[0]}`", e,
                     f"`{what}` was moved out; use the remaining fields or "
                     "reassign the whole value")

    def move_path(self, bv, path, node, root):
        if bv.uninit:
            self.err(f"`{bv.name}` is moved before it is initialized", node)
        if bv.moved:
            self.err(f"cannot move a field of moved value `{root}`", node)
        if self.path_moved(bv, path):
            self.err(f"use of moved field `{self.field_label(root, path)}`",
                     node, "the field was moved earlier; clone it or reassign it")
        act = self.active_on(bv.name)
        if act:
            self.err(f"cannot move a field of `{root}` while it is borrowed",
                     node, "wait until the borrow ends, or clone the field",
                     notes=[(act[0].line, f"borrow of `{root}` starts here")])
        bv.moved_paths.add(path)

    def field_move_blocked(self, e):
        """Why a field move has no movable owner to zero/drop, if any."""
        node = e.base
        while isinstance(node, ast.Field):
            node = node.base
        if isinstance(node, ast.Index):
            return "an indexed element"
        if isinstance(node, ast.Deref):
            return "a borrowed or boxed value"
        bt = self.m.u.resolve_deep(getattr(node, "ty", None) or UNIT)
        if isinstance(bt, TCon) and bt.name == "Rc":
            return "an `Rc` payload"
        if isinstance(bt, (TRef, TPtr)):
            return "a borrowed value"
        return ""

    # -- places -----------------------------------------------------------
    def place_roots(self, e, node, read=True):
        if isinstance(e, ast.Name):
            bv = self.lookup(e.name)
            if bv is None:
                return set()
            if read and bv.uninit:
                self.err(f"`{e.name}` is used before it is initialized", node)
            if read and bv.moved:
                self.err(f"use of moved value `{e.name}`", node)
            return {e.name}
        if isinstance(e, ast.Field):
            return self.place_roots(e.base, node, read)
        if isinstance(e, ast.Index):
            roots = self.place_roots(e.base, node, read)
            self.use(e.index)
            return roots
        if isinstance(e, ast.Deref):
            bt = self.m.u.resolve_deep(e.base.ty)
            if isinstance(bt, TCon) and bt.name in ("Box", "Rc"):
                # The owner of the pointee is the box/Rc binding itself.
                return self.place_roots(e.base, node, read)
            if isinstance(bt, TRef):
                # A reference's pointee is tied to the reference's source.
                roots, _mut = self.borrow_sources(e.base, _seen=None)
                if roots:
                    return roots
                return self.place_roots(e.base, node, read)
            self.use(e.base, consume=False)
            return set()
        if isinstance(e, ast.Ref):
            return self.place_roots(e.base, node, read)
        self.use(e)
        return set()

    def consume_place(self, e, node):
        if isinstance(e, ast.Deref):
            bt = self.m.u.resolve_deep(e.base.ty)
            if isinstance(bt, TCon) and bt.name in ("Box", "Rc")                     and not self.m.is_copy(e.ty):
                self.err(f"cannot move a non-Copy value out of a {bt.name}", e,
                         "borrow it, clone the payload, or consume the "
                         f"{bt.name} explicitly")
        if isinstance(e, ast.Field):
            roots = self.place_roots(e, node)
            self.use(e, consume=True)
            return roots
        roots = self.place_roots(e, node)
        if not self.m.is_copy(e.ty):
            for root in roots:
                bv = self.lookup(root)
                if bv is not None:
                    self.move_var(bv, node, root)
        return roots

    def check_read_borrows(self, roots, node, ignore=None):
        for root in roots:
            for b in self.active_on(root):
                if b.mut and b is not ignore:
                    self.err(f"cannot read `{root}` while it is mutably borrowed",
                             node, "wait until the mutable borrow ends",
                             notes=[(b.line, f"mutable borrow of `{root}` starts here")])

    def ref_borrow_of(self, e):
        """The borrow object that a reference expression itself carries."""
        if isinstance(e, ast.Name):
            bv = self.lookup(e.name)
            return bv.ref_borrow if bv is not None else None
        if isinstance(e, ast.Field):
            return self.ref_borrow_of(e.base)
        if isinstance(e, ast.Deref):
            return self.ref_borrow_of(e.base)
        return None

    # -- expressions ------------------------------------------------------
    def use(self, e, consume=True):
        if isinstance(e, (ast.IntLit, ast.FloatLit, ast.BoolLit, ast.CharLit, ast.UnitLit)):
            return
        if isinstance(e, ast.Name):
            bv = self.lookup(e.name)
            if bv is None:
                return
            if bv.uninit:
                self.err(f"`{e.name}` is used before it is initialized", e)
            if bv.moved:
                self.err(f"use of moved value `{e.name}`", e,
                         "the value was moved earlier; clone it or borrow instead")
            if bv.moved_paths:
                what = self.field_label(e.name, next(iter(bv.moved_paths)))
                self.err(f"use of partially moved value `{e.name}`", e,
                         f"`{what}` was moved out; use the remaining fields or "
                         "reassign the whole value")
            self.check_read_borrows({e.name}, e)
            if consume and not self.m.is_copy(e.ty):
                self.move_var(bv, e)
            return
        if isinstance(e, ast.Field):
            roots = self.place_roots(e, e)
            self.check_read_borrows(roots, e)
            self.check_partial_read(e)
            if consume and not self.m.is_copy(e.ty):
                path = self.place_path(e)
                if not roots:
                    why = self.field_move_blocked(e)
                    if why:
                        self.err(f"cannot move a field out of {why}", e,
                                 "bind an owned value first or clone the field")
                    return
                if path is None:
                    self.err("cannot partially move a non-Copy field out of a "
                             "borrowed or indexed value", e,
                             "bind an owned aggregate first or clone the field")
                for root in roots:
                    bv = self.lookup(root)
                    if bv is None:
                        continue
                    rt = self.m.u.resolve_deep(bv.ty)
                    if isinstance(rt, TCon) and rt.name == "Rc":
                        self.err("cannot move a non-Copy field out of an "
                                 "`Rc` payload", e,
                                 "Rc payloads are shared; clone the field")
                    if isinstance(rt, (TRef, TPtr)):
                        self.err(f"cannot move a non-Copy field out of `{root}` "
                                 f"because it is borrowed", e,
                                 "clone it or borrow the field instead")
                    self.move_path(bv, path[1], e, root)
            return
        if isinstance(e, ast.Index):
            roots = self.place_roots(e, e)
            self.check_read_borrows(roots, e)
            self.check_partial_read(e.base)
            if consume and not self.m.is_copy(e.ty):
                for root in self.place_roots(e, e):
                    bv = self.lookup(root)
                    if bv is not None:
                        self.move_var(bv, e, root)
            return
        if isinstance(e, ast.Deref):
            roots = self.place_roots(e, e)
            self.check_read_borrows(roots, e, ignore=self.ref_borrow_of(e.base))
            self.check_partial_read(e.base)
            base_t = self.m.u.resolve_deep(e.base.ty)
            if isinstance(base_t, TCon) and base_t.name in ("Box", "Rc"):
                if consume and not self.m.is_copy(e.ty):
                    self.err(f"cannot move a non-Copy value out of a {base_t.name}", e,
                             "borrow it with `&` or copy it explicitly")
                return
            if consume and not self.m.is_copy(e.ty):
                pass  # raw/ref deref: ownership stays with the pointee
            return
        if isinstance(e, ast.Ref):
            if e.mut and isinstance(e.base, ast.Deref):
                bt = self.m.u.resolve_deep(e.base.base.ty)
                if isinstance(bt, TCon) and bt.name == "Rc":
                    self.err("cannot take `&mut` through `Rc`", e,
                             "Rc provides shared access only")
            self.check_partial_read(e.base)
            roots = self.place_roots(e.base, e)
            ignore = self.ref_borrow_of(e.base)
            if e.mut:
                self.check_mut_borrow(roots, e, ignore=ignore)
            else:
                self.check_read_borrows(roots, e, ignore=ignore)
                for root in roots:
                    bv = self.lookup(root)
                    if bv is not None and bv.moved:
                        self.err(f"cannot borrow moved value `{root}`", e)
            self.add_borrow(roots, e.mut, e)
            return
        if isinstance(e, ast.Unary):
            self.use(e.operand)
            return
        if isinstance(e, ast.Cast):
            self.use(e.base)
            return
        if isinstance(e, ast.Try):
            self.use(e.base)
            return
        if isinstance(e, ast.Lambda):
            for cname, ct in getattr(e, "captures", []):
                if not self.m.is_copy(ct):
                    bv = self.lookup(cname)
                    if bv is not None:
                        self.move_var(bv, e, cname)
            self.push()
            for p, pt in zip(e.params, getattr(e, "param_types", [])):
                self.bind(p.name, pt, False, p)
            for cname, ct in getattr(e, "captures", []):
                self.bind(cname, ct if self.m.is_copy(ct) else TRef(False, ct),
                          False, e)
            if isinstance(e.body, ast.BlockExpr):
                self.check_block(e.body.stmts, new_scope=False)
            else:
                self.use(e.body)
            self.pop()
            return
        if isinstance(e, ast.Binary):
            self.use(e.left)
            self.use(e.right)
            return
        if isinstance(e, ast.StrLit):
            for seg in e.segs:
                if seg.kind == "expr":
                    self.use(seg.parts[0], consume=False)
            return
        if isinstance(e, ast.ListLit):
            for item in e.items:
                self.use(item)
            return
        if isinstance(e, ast.StructLit):
            for _, v in e.fields:
                if isinstance(v, ast.Ref):
                    if v.mut and isinstance(v.base, ast.Deref):
                        bt = self.m.u.resolve_deep(v.base.base.ty)
                        if isinstance(bt, TCon) and bt.name == "Rc":
                            self.err("cannot take `&mut` through `Rc`", v,
                                     "Rc provides shared access only")
                    roots = self.place_roots(v.base, v)
                    if v.mut:
                        self.check_mut_borrow(roots, v)
                    else:
                        self.check_read_borrows(roots, v)
                    b = self.add_borrow(roots, v.mut, e, temp=False)
                    if getattr(e, "ref_borrow", None) is None:
                        e.ref_borrow = b
                else:
                    self.use(v)
                    roots, mut = self.borrow_sources(v)
                    if roots:
                        self.add_borrow(roots, mut, e, temp=False)
            return
        if isinstance(e, ast.Range):
            if e.start is not None:
                self.use(e.start)
            if e.end is not None:
                self.use(e.end)
            return
        if isinstance(e, ast.AsmExpr):
            for opnd in e.operands:
                self.use(opnd)
            return
        if isinstance(e, ast.BlockExpr):
            for s in e.stmts:
                self.use(s)
            return
        if isinstance(e, ast.UnsafeExpr):
            self.unsafe += 1
            self.check_block(e.body)
            self.unsafe -= 1
            return
        if isinstance(e, ast.Call):
            self.check_call(e)
            return
        if isinstance(e, ast.Match):
            self.check_match(e)
            return
        self.err("unsupported expression in borrow checker", e)

    def note_returned_borrow(self, e, roots):
        rt = self.m.u.resolve_deep(getattr(e, "result_type", None) or e.ty)
        if not _has_ref(self.m, rt):
            return
        if not roots:
            self.err("cannot return a borrow of a temporary", e,
                     "bind the owner to a local first")
        mut = _has_mut_ref(self.m, rt)
        e.returned_src = set(roots)
        e.returned_mut = mut
        e.returned_borrow = self.add_borrow(set(roots), mut, e, temp=False)

    def check_call(self, e):
        solved = getattr(e, "solved", None)
        if solved is None:
            self.err("internal: unresolved call", e)
        kind = solved[0]

        if kind == "builtin":
            name = solved[1]
            for a in e.args:
                # `drop(x.field)` destroys that field in place; it does not
                # move the whole owner. Other builtins keep their normal
                # move semantics.
                if name == "drop" and isinstance(a, (ast.Field, ast.Index)):
                    self.use(a, consume=False)
                    node = a.base
                    while True:
                        owner = self.m.u.resolve_deep(
                            getattr(node, "ty", None) or UNIT)
                        if isinstance(node, ast.Deref):
                            owner = self.m.u.resolve_deep(
                                getattr(node.base, "ty", None) or UNIT)
                        if isinstance(owner, TCon) and owner.name == "Rc":
                            self.err("cannot drop a field through `Rc`", a,
                                     "Rc payloads are shared; clone the value "
                                     "and drop the clone instead")
                        if isinstance(owner, TRef) and not owner.mut:
                            self.err("cannot drop a field through a shared "
                                     "reference", a,
                                     "borrow the owner mutably or drop an "
                                     "owned copy")
                        if isinstance(owner, (TRef, TPtr)):
                            self.err("cannot drop a field through a borrowed "
                                     "value", a,
                                     "drop the owned value or an owned copy")
                        if isinstance(node, ast.Deref):
                            self.err("cannot drop a field through a "
                                     "dereference", a,
                                     "drop a named field on the owner or drop "
                                     "an owned copy")
                        if isinstance(node, (ast.Field, ast.Index, ast.Deref)):
                            node = node.base
                            continue
                        break
                    if isinstance(a, ast.Field) and not self.m.is_copy(a.ty):
                        path = self.place_path(a)
                        if path is not None and path[1]:
                            bv = self.lookup(path[0])
                            if bv is not None:
                                roots = self.place_roots(a, a, read=False)
                                for root in roots:
                                    act = self.active_on(root)
                                    if act:
                                        self.err(f"cannot drop a field of "
                                                 f"`{root}` while it is borrowed", a,
                                                 "wait until the borrow ends")
                                bv.moved_paths.add(path[1])
                else:
                    self.use(a, consume=(name == "drop"))
            return
        if kind == "dyn_method":
            recv = e.callee.base
            self.check_partial_read(recv)
            sm = "&"
            tri = self.m.traits.get(solved[1])
            if tri is not None:
                sig = tri.methods.get(solved[2])
                sm = sig.self_mode if sig is not None else "&"
            roots = self.place_roots(recv, e)
            if sm == "&mut":
                self.check_mut_borrow(roots, e)
                self.add_borrow(roots, True, e)
            else:
                self.add_borrow(roots, False, e)
            for a in e.args:
                self.use(a)
            return
        if kind == "closure":
            self.use(e.callee, consume=False)
            for a in e.args:
                self.use(a)
            return
        if kind == "rc_method":
            self.check_partial_read(e.callee.base)
            roots = self.place_roots(e.callee.base, e)
            self.add_borrow(roots, False, e)
            return
        if kind == "trait_method":
            recv = e.callee.base
            self.check_partial_read(recv)
            sm = getattr(e, "trait_self_mode", "&")
            if sm is None:
                self.consume_place(recv, e)
            elif sm == "&mut":
                roots = self.place_roots(recv, e)
                self.check_mut_receiver(recv, roots, e)
                self.add_borrow(roots, True, e)
            else:
                roots = self.place_roots(recv, e)
                self.add_borrow(roots, False, e)
            for a in e.args:
                self.use(a)
            self.note_returned_borrow(e, self.place_roots(recv, e))
            return
        if kind == "enum_method":
            method = solved[1]
            recv = e.callee.base
            self.check_partial_read(recv)
            if method in ("is_some", "is_none", "is_ok", "is_err"):
                roots = self.place_roots(recv, e)
                self.add_borrow(roots, False, e)
            else:
                self.consume_place(recv, e)
            for a in e.args:
                self.use(a)
            return
        if kind == "strmeth":
            self.check_partial_read(e.callee.base)
            roots = self.place_roots(e.callee.base, e)
            self.add_borrow(roots, False, e)
            return
        if kind == "list":
            method = solved[1]
            self.check_partial_read(e.callee.base)
            self.list_receiver_borrow(e, method)
            for a in e.args:
                self.use(a)
            return
        if kind == "method":
            info = solved[1]
            recv = e.callee.base
            self.check_partial_read(recv)
            auto = getattr(e, "self_auto", "none")
            if info.self_mode is None:
                self.consume_place(recv, e)
            elif auto == "ref" or (auto == "none" and info.self_mode == "&"):
                roots = self.place_roots(recv, e)
                self.add_borrow(roots, False, e)
            elif auto == "mutref" or (auto == "none" and info.self_mode == "&mut"):
                if isinstance(recv, ast.Deref):
                    bt = self.m.u.resolve_deep(recv.base.ty)
                    if isinstance(bt, TCon) and bt.name == "Rc":
                        self.err("cannot call a `&mut self` method through `Rc`", e,
                                 "Rc provides shared access only")
                roots = self.place_roots(recv, e)
                self.check_mut_receiver(recv, roots, e)
                self.add_borrow(roots, True, e)
            for a in e.args:
                self.use(a)
            self.note_returned_borrow(e, self.place_roots(recv, e))
            return
        if kind == "fn":
            info = solved[1]
            for a in e.args:
                self.use(a)
            if info.borrow_param is not None:
                names = [p.name for p in info.decl.params]
                try:
                    idx = names.index(info.borrow_param)
                except ValueError:
                    idx = -1
                roots = self.place_roots(e.args[idx], e) if idx >= 0 else set()
                self.note_returned_borrow(e, roots)
            return
        if kind in ("variant", "fnptr"):
            for a in e.args:
                self.use(a)
            return
        if kind == "list" and solved[1] == "clear":
            return
        self.err("internal: unknown call kind", e)

    def list_receiver_borrow(self, e, method):
        recv = e.callee.base
        roots = self.place_roots(recv, e)
        mut = method in ("push", "pop", "set", "clear")
        if mut:
            self.check_mut_borrow(roots, e)
            self.add_borrow(roots, True, e)
        else:
            self.add_borrow(roots, False, e)

    def check_match(self, e):
        scrut = e.scrutinee
        if getattr(e, "match_by_ref", False):
            roots = self.place_roots(scrut, e)
            self.add_borrow(roots, False, e, temp=False)
        else:
            self.use(scrut)
        snap = self.snapshot()
        ends = []
        for arm in e.arms:
            self.restore(snap)
            self.push()
            for name, ty in getattr(arm, "bindings", []):
                self.bind(name, ty, False, arm)
            self.use(arm.body)
            self.pop()
            ends.append(self.snapshot())
        self.restore(snap)
        self.merge(ends)

    def borrow_sources(self, e, _seen=None):
        """Roots whose borrows a value carries (for structs with reference fields)."""
        if isinstance(e, ast.Ref):
            return set(self.place_roots(e.base, e)), bool(e.mut)
        if isinstance(e, ast.Name):
            bv = self.lookup(e.name)
            if bv is not None and bv.borrow_src is not None:
                return set(bv.borrow_src), bv.borrow_mut
            return set(), False
        if isinstance(e, ast.StructLit):
            roots, mut = set(), False
            for _, v in e.fields:
                r, m = self.borrow_sources(v, _seen)
                roots |= r
                mut = mut or m
            return roots, mut
        if isinstance(e, ast.ListLit):
            roots, mut = set(), False
            for v in e.items:
                r, m = self.borrow_sources(v, _seen)
                roots |= r
                mut = mut or m
            return roots, mut
        if isinstance(e, ast.Field):
            # A field of a struct that carries references inherits the struct's
            # source. This is conservative and keeps mutations through &mut
            # fields tied to the original owner.
            return self.borrow_sources(e.base, _seen)
        if isinstance(e, ast.Index):
            return self.borrow_sources(e.base, _seen)
        if isinstance(e, ast.Call):
            src = getattr(e, "returned_src", None)
            if src:
                return set(src), bool(getattr(e, "returned_mut", False))
            return set(), False
        if isinstance(e, ast.Cast):
            return self.borrow_sources(e.base, _seen)
        return set(), False

    # -- statements -------------------------------------------------------
    def check_stmt(self, s):
        if isinstance(s, ast.Pass):
            return
        if isinstance(s, ast.Let):
            init = s.init
            ref_borrow = None
            if init is not None:
                if isinstance(init, ast.Ref):
                    roots = self.place_roots(init.base, s)
                    ignore = self.ref_borrow_of(init.base)
                    if init.mut:
                        self.check_mut_borrow(roots, s, ignore=ignore)
                    else:
                        self.check_read_borrows(roots, s, ignore=ignore)
                    ref_borrow = self.add_borrow(roots, init.mut, s, temp=False)
                elif isinstance(init, ast.Cast) and isinstance(init.base, ast.Ref):
                    self.use(init)
                    ref_borrow = getattr(init, "returned_borrow", None)
                else:
                    self.use(init)
                    ref_borrow = (getattr(init, "returned_borrow", None)
                                  or getattr(init, "ref_borrow", None))
            src, smut = (None, False)
            if init is not None:
                roots, mut = self.borrow_sources(init)
                if roots:
                    src, smut = roots, mut
            self.bind(s.name, s.ty, s.mutable, s, uninit=(s.init is None),
                      borrow_src=src, borrow_mut=smut, ref_borrow=ref_borrow)

        elif isinstance(s, ast.Assign):
            if _has_ref(self.m, getattr(s.target, "ty", None) or UNIT):
                self.err("cannot assign to a place that stores a reference yet",
                         s, "bind a new value instead")
            if isinstance(s.target, ast.Name):
                bv = self.lookup(s.target.name)
                if bv is None:
                    self.err(f"unknown variable '{s.target.name}'", s)
                if not bv.mutable:
                    self.err(f"cannot assign to immutable binding '{s.target.name}'", s,
                             "declare it with `var` instead of `let`")
                self.check_no_borrows({s.target.name}, s, "assign to")
                self.use(s.value)
                bv.moved = False
                bv.uninit = False
                bv.moved_paths.clear()
            elif isinstance(s.target, ast.Field):
                roots = self.place_roots(s.target.base, s)
                self.require_mutable_roots(roots, s, "assign to")
                self.check_no_borrows(roots, s, "assign through")
                self.use(s.value)
                path = self.place_path(s.target)
                if path is not None and path[1]:
                    bv = self.lookup(path[0])
                    if bv is not None:
                        parent = [p for p in bv.moved_paths
                                  if len(p) < len(path[1])
                                  and path[1][:len(p)] == p]
                        if parent:
                            self.err(f"cannot assign through partially moved "
                                     f"value `{path[0]}`", s,
                                     "reassign the whole value first")
                        bv.moved_paths = {
                            p for p in bv.moved_paths
                            if not (len(p) >= len(path[1])
                                    and p[:len(path[1])] == path[1])
                        }
            elif isinstance(s.target, ast.Index):
                roots = self.place_roots(s.target.base, s)
                self.require_mutable_roots(roots, s, "assign to")
                self.check_no_borrows(roots, s, "assign through")
                self.use(s.target.index)
                self.use(s.value)
            elif isinstance(s.target, ast.Deref):
                t = self.m.u.resolve_deep(s.target.base.ty)
                if isinstance(t, TCon) and t.name == "Rc":
                    self.err("cannot mutate through `Rc`; use `Box` or `&mut`", s)
                if isinstance(t, TPtr) and self.unsafe == 0:
                    self.err("dereferencing a raw pointer requires an `unsafe:` block", s)
                if isinstance(t, TRef) and not t.mut:
                    self.err("cannot assign through a shared reference", s)
                roots = self.place_roots(s.target, s)
                if isinstance(t, TCon) and t.name == "Box":
                    self.require_mutable_roots(roots, s, "assign through")
                ignore = self.ref_borrow_of(s.target.base)
                self.check_no_borrows(roots, s, "assign through", ignore=ignore)
                self.use(s.target.base, consume=False)
                self.use(s.value)
            elif isinstance(s.target, ast.Ref):
                self.err("cannot assign to a reference expression", s)
            else:
                self.err("invalid assignment target", s)

        elif isinstance(s, ast.ExprStmt):
            self.use(s.expr)

        elif isinstance(s, ast.If):
            mark = len(self.borrows)
            self.use(s.cond)
            self.clear_temps(mark)
            snap = self.snapshot()
            ends = []

            self.restore(snap)
            self.check_block(s.then_body)
            ends.append(self.snapshot())
            for c, body in s.elifs:
                self.restore(snap)
                mark = len(self.borrows)
                self.use(c)
                self.clear_temps(mark)
                self.check_block(body)
                ends.append(self.snapshot())
            if s.else_body is not None:
                self.restore(snap)
                self.check_block(s.else_body)
                ends.append(self.snapshot())
            else:
                ends.append(snap)
            self.restore(snap)
            self.merge(ends)

        elif isinstance(s, ast.While):
            mark = len(self.borrows)
            self.use(s.cond)
            self.clear_temps(mark)
            snap = self.snapshot()
            self.restore(snap)
            self.loop_guard(lambda: self.check_block(s.body))
            end = self.snapshot()
            self.restore(snap)
            self.merge([snap, end])

        elif isinstance(s, ast.For):
            self.check_for(s)

        elif isinstance(s, ast.Return):
            if s.value is not None:
                self.use(s.value)
            ret = self.m.u.resolve_deep(self.info.ret)
            if _has_ref(self.m, ret):
                roots, _mut = self.borrow_sources(s.value)
                want = self.info.borrow_param
                if not roots or want is None or not roots <= {want}:
                    self.err(
                        f"returned borrow must be derived from `{want}`", s,
                        "borrows of locals or temporaries cannot escape")

        elif isinstance(s, ast.UnsafeStmt):
            self.unsafe += 1
            self.check_block(s.body)
            self.unsafe -= 1

    def require_mutable_roots(self, roots, node, action):
        for root in roots:
            bv = self.lookup(root)
            if bv is not None:
                rt = self.m.u.resolve_deep(bv.ty)
                if isinstance(rt, TCon) and rt.name == "Rc":
                    self.err("cannot mutate through `Rc`", node,
                             "Rc provides shared access only; use Box or a local")
                if isinstance(rt, TRef):
                    if not rt.mut:
                        self.err(f"cannot mutate through shared reference `{root}`", node)
                    continue
            if bv is not None and not bv.mutable:
                self.err(f"cannot {action} `{root}` because it was declared with `let`",
                         node, "declare it with `var`")

    def loop_guard(self, body_fn):
        before = {name: (bv.moved, frozenset(bv.moved_paths))
                  for sc in self.scopes for name, bv in sc.items()}
        self.loop_depth += 1
        body_fn()
        self.loop_depth -= 1
        for sc in self.scopes:
            for name, bv in sc.items():
                old_moved, old_paths = before.get(name, (False, frozenset()))
                if name not in before:
                    continue
                new_paths = bv.moved_paths - old_paths
                if (bv.moved and not old_moved) or new_paths:
                    what = (self.field_label(name, next(iter(new_paths)))
                            if new_paths else f"`{name}`")
                    self.err(f"`{what}` is moved inside a loop", bv,
                             "each iteration would use it again; move it before the loop")

    def check_for(self, s):
        it = s.iter
        if isinstance(it, ast.Range):
            self.use(it)
            snap = self.snapshot()
            self.push()
            self.bind(s.name, s.elem_ty, False, s)
            self.restore(snap)
            self.loop_guard(lambda: self.check_block(s.body, new_scope=False))
            self.pop()
            end = self.snapshot()
            self.restore(snap)
            self.merge([snap, end])
            return
        if isinstance(it, ast.Ref):
            roots = self.place_roots(it.base, s)
            t = self.m.u.resolve_deep(it.base.ty)
            if isinstance(t, TRef):
                t = t.inner
            mut = it.mut
            if mut:
                self.check_mut_borrow(roots, s)
            borrow = Borrow(set(roots), mut, self.blocks[-1],
                            getattr(s, "line", 0), True)
            self.borrows.append(borrow)
            snap = self.snapshot()
            self.push()
            self.bind(s.name, s.elem_ty, False, s)
            self.restore(snap)
            self.loop_guard(lambda: self.check_block(s.body, new_scope=False))
            self.pop()
            end = self.snapshot()
            self.restore(snap)
            self.merge([snap, end])
            try:
                self.borrows.remove(borrow)
            except ValueError:
                pass
            return
        # by value: Copy element, iterate and copy
        self.use(it, consume=False)
        snap = self.snapshot()
        self.push()
        self.bind(s.name, s.elem_ty, False, s)
        self.loop_depth += 1
        self.restore(snap)
        self.check_block(s.body, new_scope=False)
        self.loop_depth -= 1
        self.pop()
        end = self.snapshot()
        self.restore(snap)
        self.merge([snap, end])
