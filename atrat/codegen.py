"""Atrat -> C99 (GCC/Clang) code generator.

Generated programs need only libc: no GC, no tracing, no VM.  Generics are
monomorphised so structs stay unboxed and calls stay direct.  Statement
expressions (`({ ... })`) are used to keep ownership-building expressions
composable.
"""
from __future__ import annotations

from . import ast
from .check import FnInfo, Module, VariantRef
from .lexer import AtratError
from .runtime import RUNTIME
from .typesys import INT_TYPES, TCon, TFn, TPtr, TRef, TVar, subst


class CodegenError(AtratError):
    kind = "codegen error"


C_PRIMS = {
    "i8": "int8_t", "i16": "int16_t", "i32": "int32_t", "i64": "int64_t",
    "u8": "uint8_t", "u16": "uint16_t", "u32": "uint32_t", "u64": "uint64_t",
    "isize": "intptr_t", "usize": "size_t",
    "f32": "float", "f64": "double", "bool": "bool",
    "char": "uint32_t", "str": "AtrStr", "String": "AtrString", "unit": "AtrUnit",
}


def _zero(ct: str) -> str:
    return f"({ct}){{0}}"


def _stringy(t) -> bool:
    if isinstance(t, TRef):
        t = t.inner
    return isinstance(t, TCon) and t.name in ("str", "String")


def _float_t(t) -> bool:
    return isinstance(t, TCon) and t.name in ("f32", "f64")


def _push_suffix(t) -> str:
    if isinstance(t, TCon):
        if t.name in ("f32", "f64"):
            return "f64"
        if t.name == "bool":
            return "bool"
        if t.name == "char":
            return "char"
        if t.name.startswith("u"):
            return "u64"
    return "i64"


def _c_bytes(b: bytes) -> str:
    out = ['"']
    for byte in b:
        if 32 <= byte < 127 and byte not in (34, 92):
            out.append(chr(byte))
        else:
            out.append("\\%03o" % byte)
    out.append('"')
    return "".join(out)


def _asm_template(template: str) -> str:
    out = []
    idx = 0
    for ch in template:
        if ch == "{":
            out.append(f"%{idx}")
            idx += 1
        elif ch == "}":
            continue
        else:
            out.append(ch)
    raw = "".join(out)
    escaped = _c_bytes(raw.encode("utf-8"))
    return escaped[1:-1]


class Codegen:
    def __init__(self, m: Module):
        self.m = m
        self.u = m.u
        self.type_names: dict[str, str] = {}
        self.type_defs: list[str] = []
        self.type_in_progress: set = set()
        self.deferred_types: list[tuple] = []
        self.deferred_keys: set = set()
        self.helper_defs: list[str] = []
        self.show_names: dict[str, str] = {}
        self.show_defs: list[str] = []
        self.drop_names: dict[str, str] = {}
        self.drop_protos: list[str] = []
        self.drop_defs: list[str] = []
        self.list_pending: list = []
        self.forwarded: set = set()
        self.eq_names: dict[str, str] = {}
        self.eq_defs: list[str] = []
        self.fn_protos: dict[str, str] = {}
        self.fn_bodies: list[str] = []
        self.fn_queue: list[tuple] = []
        self.pending: list[str] = []
        self.pre: list[str] = []
        self.temp_n = 0
        self.var_scopes: list[dict[str, tuple[str, object, bool]]] = []
        self.drop_scopes: list[list[tuple[str, object]]] = []
        self.loop_stack: list[int] = []
        self.mapping: dict = {}
        self.current_fn: FnInfo | None = None
        self.dyn_vtables: dict = {}

    # ------------------------------------------------------------------
    # types
    # ------------------------------------------------------------------
    def mangle(self, t) -> str:
        t = self.concrete(t)
        if isinstance(t, TCon):
            if t.name in C_PRIMS:
                return t.name
            if not t.args:
                return t.name
            return t.name + "_" + "_".join(self.mangle(a) for a in t.args)
        if isinstance(t, TRef):
            return ("refmut_" if t.mut else "ref_") + self.mangle(t.inner)
        if isinstance(t, TPtr):
            return ("ptr_" if not t.mut else "mptr_") + self.mangle(t.inner)
        if isinstance(t, TFn):
            return ("fn_" + "_".join(self.mangle(p) for p in t.params)
                    + "_to_" + self.mangle(t.ret))
        if isinstance(t, TVar):
            if t.hint:
                return t.hint
            raise CodegenError(f"internal: unresolved type variable T{t.id}")
        raise CodegenError(f"internal: cannot mangle {t!r}")

    def concrete(self, t, mapping=None):
        mapping = self.mapping if mapping is None else mapping
        t = self.u.resolve_deep(t)
        if mapping:
            t = self.u.resolve_deep(subst(t, mapping))
        if isinstance(t, TVar):
            if t.hint in mapping:
                return mapping[t.hint]
            if "int" in t.cons:
                return self.u.default(t)
            if "float" in t.cons:
                return self.u.default(t)
            raise CodegenError(
                f"internal: type `{t.show()}` was not resolved before code generation")
        return t

    def forward_ctype(self, t) -> str:
        """Return the C name for a type, emitting only a forward declaration."""
        t = self.concrete(t)
        if isinstance(t, TCon):
            if t.name in C_PRIMS:
                return C_PRIMS[t.name]
            key = self.mangle(t)
            if key in self.type_names:
                return self.type_names[key]
            cname = "Atr_" + key
            if key not in self.forwarded:
                self.forwarded.add(key)
                self.type_defs.append(f"typedef struct {cname} {cname};\n")
            return cname
        if isinstance(t, TRef):
            return self.forward_ctype(t.inner) + "*"
        if isinstance(t, TPtr):
            return self.forward_ctype(t.inner) + "*"
        return self.ctype(t)

    def ctype(self, t) -> str:
        t = self.concrete(t)
        if isinstance(t, TCon):
            if t.name in C_PRIMS:
                return C_PRIMS[t.name]
            if t.name == "List":
                return self._emit_list(t)
            if t.name == "Box":
                return self._emit_box(t)
            if t.name == "Rc":
                return self._emit_rc(t)
            if t.name in self.m.closures:
                return self._emit_closure(t)
            if t.name in self.m.structs or t.name in self.m.enums:
                return self._emit_record(t)
            raise CodegenError(f"internal: no C type for `{t.show()}`")
        if isinstance(t, TRef):
            if isinstance(t.inner, TCon) and t.inner.name == "dyn":
                return self._emit_dyn(t.inner)
            return self.ctype(t.inner) + "*"
        if isinstance(t, TPtr):
            return self.ctype(t.inner) + "*"
        if isinstance(t, TFn):
            return self._emit_fnptr(t)
        raise CodegenError(f"internal: no C type for `{t.show()}`")

    def is_copy(self, t) -> bool:
        return self.m.is_copy(self.concrete(t))

    def _emit_closure(self, t: TCon) -> str:
        nl = chr(10)
        key = self.mangle(t)
        cname = "Atr_" + key
        if key in self.type_names:
            return self.type_names[key]
        self.type_names[key] = cname
        ci = self.m.closures[t.name]
        self.type_defs.append("typedef struct " + cname + " " + cname + ";" + nl)
        ret = self.ctype(ci.ret)
        params = ", ".join(self.ctype(p) for p in ci.params)
        args = f", {params}" if params else ""
        fields = [f"    {ret} (*__fn)(struct {cname}*{args});"]
        for cname_f, ct in ci.captures:
            fields.append(f"    {self.ctype(ct)} f_{cname_f};")
        self.type_defs.append(
            "struct " + cname + " {" + nl + nl.join(fields) + nl + "};" + nl)
        return cname

    def _emit_fnptr(self, t: TFn) -> str:
        key = self.mangle(t)
        cname = "AtrFn_" + key
        if key in self.type_names:
            return cname
        self.type_names[key] = cname
        ret = self.ctype(t.ret)
        params = ", ".join(self.ctype(p) for p in t.params) or "void"
        self.type_defs.append(f"typedef {ret} (*{cname})({params});\n")
        return cname

    def _dep_in_progress(self, t) -> bool:
        t = self.concrete(t)
        if isinstance(t, TCon):
            key = self.mangle(t)
            if key in self.type_in_progress or key in self.deferred_keys:
                return True
            return any(self._dep_in_progress(a) for a in t.args)
        if isinstance(t, TRef):
            return False
        if isinstance(t, TPtr):
            return False
        if isinstance(t, TFn):
            return any(self._dep_in_progress(p) for p in t.params)                 or self._dep_in_progress(t.ret)
        return False

    def _record_body_parts(self, t: TCon, cname: str):
        nl = chr(10)
        if t.name in self.m.structs:
            info = self.m.structs[t.name]
            mp = dict(zip(info.generics, t.args))
            fields = []
            for fname, fty in info.fields:
                ft = self.concrete(subst(fty, mp))
                fields.append(f"    {self.ctype(ft)} f_{fname};")
            body = nl.join(fields) if fields else "    char _empty;"
            return "struct " + cname + " {" + nl + body + nl + "};" + nl

        info = self.m.enums[t.name]
        mp = dict(zip(info.generics, t.args))
        members = []
        ctors = []
        for idx, (vname, vtys) in enumerate(info.variants):
            payload = [self.concrete(subst(vt, mp)) for vt in vtys]
            if payload:
                fs = nl.join(f"        {self.ctype(pt)} v{i};"
                             for i, pt in enumerate(payload))
                members.append("        struct {" + nl + fs + nl + "        } V_" + vname + ";")
            else:
                members.append("        struct { char _unused; } V_" + vname + ";")
            params = ", ".join(f"{self.ctype(pt)} _a{i}"
                               for i, pt in enumerate(payload)) or "void"
            assigns = "".join(nl + f"    r.u.V_{vname}.v{i} = _a{i};"
                              for i in range(len(payload)))
            ctors.append(
                f"static inline {cname} atr_mk_{cname}_{vname}({params}) {{" + nl
                + f"    {cname} r; memset(&r, 0, sizeof r); r.tag = {idx};"
                + assigns + nl + "    return r;" + nl + "}" + nl)
        union = nl.join(members)
        text = ("struct " + cname + " {" + nl + "    int32_t tag;" + nl
                + "    union {" + nl + union + nl + "    } u;" + nl + "};" + nl)
        return text + "".join(ctors)

    def _emit_record(self, t: TCon) -> str:
        key = self.mangle(t)
        if key in self.type_names:
            return self.type_names[key]
        cname = "Atr_" + key
        self.type_names[key] = cname
        self.type_defs.append("typedef struct " + cname + " " + cname + ";" + chr(10))
        self.type_in_progress.add(key)
        try:
            if t.name in self.m.structs:
                info = self.m.structs[t.name]
                mp = dict(zip(info.generics, t.args))
                deps = [self.concrete(subst(ft, mp)) for _, ft in info.fields]
            else:
                info = self.m.enums[t.name]
                mp = dict(zip(info.generics, t.args))
                deps = [self.concrete(subst(vt, mp))
                        for _, vtys in info.variants for vt in vtys]
            defer = any(self._dep_in_progress(d) for d in deps)
            text = self._record_body_parts(t, cname)
        finally:
            self.type_in_progress.discard(key)
        if defer:
            self.deferred_types.append((t, cname, deps))
            self.deferred_keys.add(key)
        else:
            self.type_defs.append(text)
        return cname

    def _flush_deferred_types(self):
        while self.deferred_types:
            ready = None
            for i, (t, cname, deps) in enumerate(self.deferred_types):
                if not any(self._dep_in_progress(d) for d in deps):
                    ready = i
                    break
            if ready is None:
                ready = 0
            t, cname, deps = self.deferred_types.pop(ready)
            key = self.mangle(t)
            self.deferred_keys.discard(key)
            self.type_defs.append(self._record_body_parts(t, cname))

    def _emit_dyn(self, t: TCon) -> str:
        nl = chr(10)
        tri = self.m.traits[t.args[0].name]
        key = self.mangle(t)
        cname = "AtrDyn_" + tri.name
        vt = "AtrVt_" + tri.name
        if key in self.type_names:
            return self.type_names[key]
        self.type_names[key] = cname
        fields = []
        for mname, sig in tri.methods.items():
            ret = self.ctype(sig.ret)
            params = ", ".join(self.ctype(p) for p in sig.params[1:])
            args = ", " + params if params else ""
            fields.append(f"    {ret} (*m_{mname})(void*{args});")
        self.type_defs.append(
            "typedef struct " + vt + " {" + nl + nl.join(fields) + nl
            + "} " + vt + ";" + nl)
        self.type_defs.append(
            "typedef struct " + cname + " {" + nl
            + "    void* data;" + nl
            + "    const " + vt + "* vtbl;" + nl
            + "} " + cname + ";" + nl)
        return cname

    def dyn_vtable(self, trait: str, base: TCon) -> str:
        nl = chr(10)
        tri = self.m.traits[trait]
        key = "vt_" + trait + "_" + self.mangle(base)
        vtname = "atr_vt_" + trait + "_" + self.mangle(base)
        if key in self.dyn_vtables:
            return self.dyn_vtables[key]
        self.dyn_vtables[key] = vtname
        vt = "AtrVt_" + trait
        owner_c = self.ctype(base)
        impl = tri.impls[base.name]
        entries = []
        for mname, sig in tri.methods.items():
            info = impl.get(mname)
            if info is None or info.generics:
                raise CodegenError(
                    f"cannot build a vtable for `{base.name}` as `dyn {trait}`")
            cname = self.ensure_fn(info, [])
            wname = vtname + "_" + mname
            params = [self.concrete(p) for p in info.params[1:]]
            pc = ", ".join(f"{self.ctype(p)} _a{i}" for i, p in enumerate(params))
            plist = "void* _p" + (", " + pc if pc else "")
            call_args = [f"(({owner_c}*)_p)"] + [f"_a{i}" for i in range(len(params))]
            ret = self.concrete(info.ret)
            rc = self.ctype(ret)
            proto = f"static {rc} {wname}({plist});"
            self.helper_defs.append(proto + nl)
            body = f"static {rc} {wname}({plist}) {{"
            if rc == "AtrUnit" or rc == "void":
                body += nl + f"    {cname}({', '.join(call_args)});" + nl + "}" + nl
            else:
                body += nl + f"    return {cname}({', '.join(call_args)});" + nl + "}" + nl
            self.fn_bodies.append(body)
            entries.append(f"    .m_{mname} = {wname}")
        self.helper_defs.append(
            f"static const {vt} {vtname} = {{{nl}" + ("," + nl).join(entries)
            + nl + "};" + nl)
        return vtname

    def emit_dyn_coercion(self, e) -> str:
        nl = chr(10)
        trait, base = e.coerce_dyn
        t = self.concrete(e.ty)
        c = self.ctype(t)
        vt = self.dyn_vtable(trait, self.concrete(base))
        if isinstance(e, ast.Ref):
            data = f"(&({self.emit_expr(e.base, consume=False)}))"
        else:
            saved = e.coerce_dyn
            e.coerce_dyn = None
            try:
                data = self.emit_expr(e, consume=False)
            finally:
                e.coerce_dyn = saved
        tmp = self.temp("dyn")
        return ("({ " + c + " " + tmp + "; " + tmp + ".data = (void*)(" + data
                + "); " + tmp + ".vtbl = &" + vt + "; " + tmp + "; })")

    def emit_dyn_method(self, e, trait: str, method: str) -> str:
        nl = chr(10)
        recv = self.emit_expr(e.callee.base, consume=False)
        cc = self.ctype(e.callee.base.ty)
        tmp = self.temp("dyn")
        args = []
        for a, pt in zip(e.args, getattr(e, "param_types", [])):
            consume = not isinstance(self.concrete(pt), TRef)
            args.append(self.emit_expr(a, consume=consume))
        tail = (", " + ", ".join(args)) if args else ""
        return ("({ " + cc + " " + tmp + " = " + recv + "; ((const AtrVt_" + trait
                + "*)" + tmp + ".vtbl)->m_" + method + "(" + tmp + ".data"
                + tail + "); })")

    def _emit_list(self, t: TCon) -> str:
        key = self.mangle(t)
        if key in self.type_names:
            return self.type_names[key]
        cname = "Atr_" + key
        self.type_names[key] = cname
        nl = chr(10)
        self.type_defs.append("typedef struct " + cname + " " + cname + ";" + nl)
        elem_t = self.concrete(t.args[0])
        elem_c = self.forward_ctype(elem_t)
        self.type_defs.append("struct " + cname + " {" + nl + "    " + elem_c
                              + "* data;" + nl + "    size_t len;" + nl
                              + "    size_t cap;" + nl + "};" + nl)
        self.ctype(elem_t)
        self.list_pending.append(t)
        return cname

    def _emit_box(self, t: TCon) -> str:
        key = self.mangle(t)
        if key in self.type_names:
            return self.type_names[key]
        cname = "Atr_" + key
        self.type_names[key] = cname
        elem = self.concrete(t.args[0])
        elem_c = self.forward_ctype(elem)
        self.type_defs.append(f"typedef struct {cname} {cname};\n")
        self.type_defs.append(f"struct {cname} {{\n    {elem_c}* ptr;\n}};\n")
        self.ctype(elem)
        return cname

    def _emit_rc(self, t: TCon) -> str:
        key = self.mangle(t)
        if key in self.type_names:
            return self.type_names[key]
        cname = "Atr_" + key
        self.type_names[key] = cname
        elem = self.concrete(t.args[0])
        elem_c = self.forward_ctype(elem)
        self.type_defs.append(f"""typedef struct {cname} {cname};
struct {cname} {{
    size_t* count;
    {elem_c}* ptr;
}};
""")
        self.ctype(elem)
        return cname

    def _gen_list_helpers(self, t: TCon):
        key = self.mangle(t)
        cname = "Atr_" + key
        elem_t = self.concrete(t.args[0])
        elem_c = self.ctype(elem_t)
        need_drop = not self.is_copy(elem_t)
        if need_drop:
            self.ensure_drop(elem_t)
        opt_c = self.ctype(TCon("Option", [elem_t]))
        some = f"atr_mk_{opt_c}_Some"
        none = f"atr_mk_{opt_c}_None"
        drop_elem = (f"        atr_drop_{self.mangle(elem_t)}(&self->data[i]);\n"
                     if need_drop else "")
        set_drop = (f"    atr_drop_{self.mangle(elem_t)}(&self->data[i]);\n"
                    if need_drop else "")
        self.helper_defs.append(f"""
static inline {cname} atr_list_{key}_new(void) {{
    {cname} l; l.data = NULL; l.len = 0; l.cap = 0; return l;
}}

static inline void atr_list_{key}_push({cname}* self, {elem_c} v) {{
    if (self->len == self->cap) {{
        size_t cap = self->cap ? self->cap * 2 : 8;
        self->data = ({elem_c}*)atr_realloc(self->data, cap * sizeof({elem_c}));
        self->cap = cap;
    }}
    self->data[self->len++] = v;
}}

static inline {elem_c}* atr_list_{key}_at({cname}* self, size_t i) {{
    if (i >= self->len) atr_panic("list index out of bounds");
    return &self->data[i];
}}

static inline {opt_c} atr_list_{key}_pop({cname}* self) {{
    if (self->len == 0) return {none}();
    return {some}(self->data[--self->len]);
}}

static inline {opt_c} atr_list_{key}_get({cname}* self, size_t i) {{
    if (i >= self->len) return {none}();
    return {some}(self->data[i]);
}}

static inline void atr_list_{key}_set({cname}* self, size_t i, {elem_c} v) {{
    if (i >= self->len) atr_panic("list index out of bounds");
{set_drop}    self->data[i] = v;
}}

static inline void atr_list_{key}_clear({cname}* self) {{
    for (size_t i = 0; i < self->len; i++) {{
{drop_elem}    }}
    self->len = 0;
}}

static inline void atr_list_{key}_free({cname}* self) {{
    for (size_t i = 0; i < self->len; i++) {{
{drop_elem}    }}
    free(self->data);
    self->data = NULL;
    self->len = 0;
    self->cap = 0;
}}
""")

    # ------------------------------------------------------------------
    # show / drop / eq
    # ------------------------------------------------------------------
    def ensure_show(self, t) -> str:
        t = self.concrete(t)
        if isinstance(t, TCon) and t.name in C_PRIMS:
            return ""
        key = self.mangle(t)
        if key in self.show_names:
            return self.show_names[key]
        name = f"atr_show_{key}"
        self.show_names[key] = name
        c = self.ctype(t)
        self.show_defs.append(f"static void {name}(AtrString* out, const {c}* v);\n")
        self._gen_show(name, t, c)
        return name

    def _show_call(self, t, addr: str) -> str:
        t = self.concrete(t)
        if isinstance(t, TRef):
            return self._show_call(t.inner, f"(*{addr})")
        if isinstance(t, TCon):
            n = t.name
            if n == "str":
                return f"atr_string_push_bytes(out, ({addr})->ptr, ({addr})->len);"
            if n == "String":
                return f"atr_string_push_bytes(out, ({addr})->ptr, ({addr})->len);"
            if n == "char":
                return f"atr_string_push_char(out, *({addr}));"
            if n in C_PRIMS:
                kind = "f64" if n in ("f32", "f64") else ("bool" if n == "bool" else "i64")
                return f"atr_string_push_{kind}(out, *({addr}));"
        show = self.ensure_show(t)
        return f"{show}(out, {addr});"

    def _gen_show(self, name: str, t, c: str):
        if isinstance(t, TCon) and t.name in ("Box", "Rc"):
            inner = self.concrete(t.args[0])
            show_one = self._show_call(inner, "v->ptr")
            label = "box(" if t.name == "Box" else "rc("
            self.show_defs.append(f"""static void {name}(AtrString* out, const {c}* v) {{
    atr_string_push_cstr(out, "{label}");
{show_one}    atr_string_push_cstr(out, ")");
}}
""")
            return
        if isinstance(t, TCon) and t.name == "List":
            elem = self.concrete(t.args[0])
            show_one = self._show_call(elem, "&v->data[i]")
            self.show_defs.append(f"""static void {name}(AtrString* out, const {c}* v) {{
    atr_string_push_cstr(out, "[");
    for (size_t i = 0; i < v->len; i++) {{
        if (i) atr_string_push_cstr(out, ", ");
{show_one}    }}
    atr_string_push_cstr(out, "]");
}}
""")
            return
        if isinstance(t, TCon) and t.name in self.m.structs:
            info = self.m.structs[t.name]
            mp = dict(zip(info.generics, t.args))
            body = []
            for i, (fname, fty) in enumerate(info.fields):
                ft = self.concrete(subst(fty, mp))
                prefix = "{ " if i == 0 else ", "
                body.append(f'    atr_string_push_cstr(out, "{prefix}{fname}: ");')
                body.append("    " + self._show_call(ft, f"&v->f_{fname}"))
            body.append('    atr_string_push_cstr(out, " }");')
            self.show_defs.append(f"""static void {name}(AtrString* out, const {c}* v) {{
{chr(10).join(body)}
}}
""")
            return
        if isinstance(t, TCon) and t.name in self.m.enums:
            info = self.m.enums[t.name]
            mp = dict(zip(info.generics, t.args))
            cases = []
            for idx, (vname, vtys) in enumerate(info.variants):
                payload = [self.concrete(subst(vt, mp)) for vt in vtys]
                stmts = [f'        atr_string_push_cstr(out, "{vname}");']
                if payload:
                    stmts.append('        atr_string_push_cstr(out, "(");')
                    for i, pt in enumerate(payload):
                        if i:
                            stmts.append('        atr_string_push_cstr(out, ", ");')
                        stmts.append("        " + self._show_call(
                            pt, f"&v->u.V_{vname}.v{i}"))
                    stmts.append('        atr_string_push_cstr(out, ")");')
                stmts.append("        break;")
                cases.append(f"    case {idx}:\n" + "\n".join(stmts))
            self.show_defs.append(f"""static void {name}(AtrString* out, const {c}* v) {{
    switch (v->tag) {{
{chr(10).join(cases)}
    default:
        atr_string_push_cstr(out, "<invalid>");
    }}
}}
""")
            return
        self.show_defs.append(f"""static void {name}(AtrString* out, const {c}* v) {{
    (void)v;
    atr_string_push_cstr(out, "<value>");
}}
""")

    def ensure_drop(self, t) -> str:
        t = self.concrete(t)
        if self.is_copy(t):
            return ""
        key = self.mangle(t)
        if key in self.drop_names:
            return self.drop_names[key]
        name = f"atr_drop_{key}"
        self.drop_names[key] = name
        c = self.ctype(t)
        self.drop_protos.append(f"static void {name}({c}* v);\n")
        self._gen_drop(name, t, c)
        return name

    def _gen_drop(self, name, t, c):
        if isinstance(t, TCon) and t.name == "Box":
            inner = self.concrete(t.args[0])
            d = self.ensure_drop(inner)
            body = f"    {d}(v->ptr);\n" if d else ""
            self.drop_defs.append(f"""static void {name}({c}* v) {{
    if (v->ptr) {{
{body}        free(v->ptr);
        v->ptr = NULL;
    }}
}}
""")
            return
        if isinstance(t, TCon) and t.name == "String":
            self.drop_defs.append(f"""static void {name}({c}* v) {{
    atr_string_free(v);
}}
""")
            return
        if isinstance(t, TCon) and t.name == "List":
            key = self.mangle(t)
            self.drop_defs.append(f"""static void {name}({c}* v) {{
    atr_list_{key}_free(v);
}}
""")
            return
        if isinstance(t, TCon) and t.name in self.m.structs:
            info = self.m.structs[t.name]
            mp = dict(zip(info.generics, t.args))
            stmts = []
            for fname, fty in info.fields:
                ft = self.concrete(subst(fty, mp))
                d = self.ensure_drop(ft)
                if d:
                    stmts.append(f"    {d}(&v->f_{fname});")
            self.drop_defs.append(f"""static void {name}({c}* v) {{
{chr(10).join(stmts) if stmts else "    (void)v;"}
}}
""")
            return
        if isinstance(t, TCon) and t.name in self.m.enums:
            info = self.m.enums[t.name]
            mp = dict(zip(info.generics, t.args))
            cases = []
            for idx, (vname, vtys) in enumerate(info.variants):
                drops = []
                for i, vt in enumerate(vtys):
                    d = self.ensure_drop(self.concrete(subst(vt, mp)))
                    if d:
                        drops.append(f"        {d}(&v->u.V_{vname}.v{i});")
                drops.append("        break;")
                cases.append(f"    case {idx}:\n" + "\n".join(drops))
            self.drop_defs.append(f"""static void {name}({c}* v) {{
    switch (v->tag) {{
{chr(10).join(cases)}
    default: break;
    }}
}}
""")
            return
        self.drop_defs.append(f"""static void {name}({c}* v) {{
    (void)v;
}}
""")

    def ensure_eq(self, t) -> str:
        t = self.concrete(t)
        if isinstance(t, TCon) and t.name not in self.m.structs \
                and t.name not in self.m.enums and t.name != "List":
            return ""
        key = self.mangle(t)
        if key in self.eq_names:
            return self.eq_names[key]
        name = f"atr_eq_{key}"
        self.eq_names[key] = name
        c = self.ctype(t)
        self.eq_defs.append(f"static int {name}(const {c}* a, const {c}* b);\n")
        if isinstance(t, TCon) and t.name == "List":
            elem = self.concrete(t.args[0])
            eq = self.ensure_eq(elem)
            cmp = (f"{eq}(&a->data[i], &b->data[i])" if eq
                   else "a->data[i] == b->data[i]")
            body = (f"    if (a->len != b->len) return 0;\n"
                    f"    for (size_t i = 0; i < a->len; i++) if (!({cmp})) return 0;\n"
                    f"    return 1;")
        elif isinstance(t, TCon) and t.name in self.m.structs:
            info = self.m.structs[t.name]
            mp = dict(zip(info.generics, t.args))
            conds = [self._eq_cond(self.concrete(subst(fty, mp)),
                                   f"a->f_{fname}", f"b->f_{fname}")
                     for fname, fty in info.fields]
            body = "    return " + (" && ".join(conds) if conds else "1") + ";"
        elif isinstance(t, TCon) and t.name in self.m.enums:
            info = self.m.enums[t.name]
            mp = dict(zip(info.generics, t.args))
            lines = ["    if (a->tag != b->tag) return 0;", "    switch (a->tag) {"]
            for idx, (vname, vtys) in enumerate(info.variants):
                conds = [self._eq_cond(self.concrete(subst(vt, mp)),
                                       f"a->u.V_{vname}.v{i}",
                                       f"b->u.V_{vname}.v{i}")
                         for i, vt in enumerate(vtys)]
                lines.append(f"    case {idx}: return " +
                             (" && ".join(conds) if conds else "1") + ";")
            lines.append("    default: return 0;")
            lines.append("    }")
            body = "\n".join(lines)
        else:
            body = "    (void)a; (void)b; return 1;"
        self.eq_defs.append(
            f"static int {name}(const {c}* a, const {c}* b) {{\n{body}\n}}\n")
        return name

    def _eq_cond(self, t, a: str, b: str) -> str:
        if isinstance(t, TCon):
            if t.name == "str":
                return f"atr_str_eq({a}, {b})"
            if t.name == "String":
                return (f"atr_str_eq(atr_string_view(&({a})), "
                        f"atr_string_view(&({b})))")
            if t.name in C_PRIMS:
                return f"({a}) == ({b})"
        eq = self.ensure_eq(t)
        if eq:
            return f"{eq}(&({a}), &({b}))"
        return f"memcmp(&({a}), &({b}), sizeof({a})) == 0"

    # ------------------------------------------------------------------
    # functions
    # ------------------------------------------------------------------
    def fn_cname(self, info: FnInfo, typeargs) -> str:
        if info.decl.is_extern:
            return info.name
        if info.decl.is_extern:
            return info.name
        base = (f"atr_m_{info.owner}_{info.name}" if info.owner
                else f"atr_fn_{info.name}")
        if typeargs:
            base += "_" + "_".join(self.mangle(t) for t in typeargs)
        return base

    def ensure_fn(self, info: FnInfo, typeargs) -> str:
        cname = self.fn_cname(info, typeargs)
        if cname in self.fn_protos:
            return cname
        mapping = dict(zip(info.generics, typeargs)) if info.generics else {}
        ret = self.concrete(info.ret, mapping)
        params = [self.concrete(p, mapping) for p in info.params]
        params_c = ", ".join(self.ctype(p) for p in params) or "void"
        if info.decl.is_extern:
            self.fn_protos[cname] = f"extern {self.ctype(ret)} {cname}({params_c});\n"
            return cname
        self.fn_protos[cname] = f"static {self.ctype(ret)} {cname}({params_c});\n"
        self.fn_queue.append((info, mapping, cname))
        return cname

    def run(self) -> str:
        for info in self.m.fns.values():
            if not info.generics:
                self.ensure_fn(info, [])
        i = 0
        while i < len(self.fn_queue):
            info, mapping, cname = self.fn_queue[i]
            i += 1
            self.emit_fn_body(info, mapping, cname)
        j = 0
        while j < len(self.list_pending):
            self._gen_list_helpers(self.list_pending[j])
            j += 1

        self._flush_deferred_types()
        main_info = self.m.fns.get("main")
        if main_info is not None and main_info.params:
            raise CodegenError("main() must not take parameters")

        out = [RUNTIME, "\n/* ---- types ---- */\n"]
        out.extend(self.type_defs)
        out.append("\n/* ---- drop prototypes ---- */\n")
        out.extend(self.drop_protos)
        out.append("\n/* ---- collections ---- */\n")
        out.extend(self.helper_defs)
        out.append("\n/* ---- prototypes ---- */\n")
        out.extend(self.fn_protos.values())
        out.append("\n/* ---- equality ---- */\n")
        out.extend(self.eq_defs)
        out.append("\n/* ---- show ---- */\n")
        out.extend(self.show_defs)
        out.append("\n/* ---- drop ---- */\n")
        out.extend(self.drop_defs)
        out.append("\n/* ---- functions ---- */\n")
        out.extend(self.fn_bodies)
        out.append("\n/* ---- entry point ---- */\n")
        if main_info is not None:
            ret = self.concrete(main_info.ret)
            if isinstance(ret, TCon) and ret.name == "unit":
                out.append("int main(int argc, char** argv) { "
                           "atr_argc = argc; atr_argv = argv; "
                           "atr_fn_main(); return 0; }\n")
            else:
                out.append("int main(int argc, char** argv) { "
                           "atr_argc = argc; atr_argv = argv; "
                           "return (int)atr_fn_main(); }\n")
        return "".join(out)

    def emit_fn_body(self, info: FnInfo, mapping: dict, cname: str):
        self.mapping = mapping
        self.current_fn = info
        self.pre = []
        self.pending = []
        self.var_scopes = [{}]
        self.drop_scopes = [[]]
        self.loop_stack = []
        ret = self.concrete(info.ret)
        ret_c = self.ctype(ret)
        params = [self.concrete(p) for p in info.params]

        plist = []
        for p, pt in zip(info.decl.params, params):
            pname = "v_" + p.name
            plist.append(f"{self.ctype(pt)} {pname}")
            self.var_scopes[-1][p.name] = (pname, pt, not self.is_copy(pt))
            if not self.is_copy(pt):
                self.drop_scopes[-1].append((pname, pt))

        lines = []
        for p, pt in zip(info.decl.params, params):
            if not self.is_copy(pt):
                lines.append(f"    bool a_v_{p.name} = true;")
        body_stmts = info.decl.body
        for idx, s in enumerate(body_stmts):
            implicit = (idx == len(body_stmts) - 1
                        and isinstance(s, ast.ExprStmt)
                        and ret_c != "AtrUnit")
            if not implicit:
                lines.extend(self.emit_stmt(s, 1))
                continue
            self.pre = []
            self.pending = []
            val = self.emit_expr(s.expr, consume=True)
            pre = self.pre
            tmp = self.temp("ret")
            lines.extend("    " + p for p in pre)
            lines.append(f"    {ret_c} {tmp} = {val};")
            lines.extend("    " + p for p in self.pending)
            for d in self.emit_all_drops(0):
                lines.append(f"    {d}")
            lines.append(f"    return {tmp};")
            self.pending = []
        for d in self.emit_all_drops(0):
            lines.append(f"    {d}")
        if ret_c == "AtrUnit":
            lines.append("    return;")
        else:
            lines.append(f"    return {_zero(ret_c)};")

        text = [f"static {ret_c} {cname}({', '.join(plist) or 'void'}) {{\n"]
        text.extend(line + "\n" for line in lines)
        text.append("}\n\n")
        self.fn_bodies.append("".join(text))

    # ------------------------------------------------------------------
    # scopes / locals
    # ------------------------------------------------------------------
    def ind(self, n):
        return "    " * n

    def temp(self, prefix="t") -> str:
        self.temp_n += 1
        return f"{prefix}_{self.temp_n}"

    def declare_local(self, name: str, t) -> str:
        cname = f"v_{name}_{self.temp_n}"
        self.temp_n += 1
        self.var_scopes[-1][name] = (cname, t, not self.is_copy(t))
        return cname

    def lookup_local(self, name: str):
        for sc in reversed(self.var_scopes):
            if name in sc:
                return sc[name]
        return None

    def alive_flag(self, cvar: str) -> str:
        return "a_" + cvar

    def root_has_flag(self, cvar: str) -> bool:
        for sc in self.var_scopes:
            for cv, t, has_flag in sc.values():
                if cv == cvar:
                    return has_flag
        return False

    def mark_moved(self, cvar: str):
        self.pending.append(f"{self.alive_flag(cvar)} = false;")

    def materialize(self, e, prefix="tmp"):
        t = self.concrete(e.ty)
        c = self.ctype(t)
        tmp = self.temp(prefix)
        val = self.emit_expr(e, consume=False)
        self.pre.append(f"{c} {tmp} = {val};")
        if not self.is_copy(t):
            d = self.ensure_drop(t)
            if d:
                self.pending.append(f"{d}(&{tmp});")
        return t, tmp

    def root_cvar(self, e):
        if isinstance(e, ast.Name):
            loc = self.lookup_local(e.name)
            return loc[0] if loc else None
        if isinstance(e, (ast.Field, ast.Index)):
            return self.root_cvar(e.base)
        return None

    def emit_all_drops(self, upto: int = 0) -> list[str]:
        lines = []
        for sc in reversed(self.drop_scopes[upto:]):
            for cvar, t in reversed(sc):
                d = self.ensure_drop(t)
                if d:
                    lines.append(f"if ({self.alive_flag(cvar)}) {{ {d}(&{cvar}); }}")
        return lines

    # ------------------------------------------------------------------
    # statements
    # ------------------------------------------------------------------
    def emit_stmt(self, s, indent: int) -> list[str]:
        pad = self.ind(indent)
        self.pre = []
        self.pending = []
        lines = self._emit_stmt(s, indent, pad)
        out = [pad + p for p in self.pre]
        out.extend(lines)
        out.extend(pad + p for p in self.pending)
        self.pre = []
        self.pending = []
        return out

    def _emit_stmt(self, s, indent, pad) -> list[str]:
        if isinstance(s, ast.Let):
            t = self.concrete(s.ty)
            c = self.ctype(t)
            cname = self.declare_local(s.name, t)
            if s.init is None:
                out = [f"{pad}{c} {cname} = {_zero(c)};"]
            else:
                val = self.emit_expr(s.init, consume=True)
                out = [f"{pad}{c} {cname} = {val};"]
            if not self.is_copy(t):
                out.append(f"{pad}bool {self.alive_flag(cname)} = "
                           f"{'true' if s.init is not None else 'false'};")
                self.drop_scopes[-1].append((cname, t))
            return out

        if isinstance(s, ast.Assign):
            return self.emit_assign(s, indent, pad)

        if isinstance(s, ast.ExprStmt):
            t = self.concrete(s.expr.ty)
            if isinstance(t, TCon) and t.name == "unit":
                val = self.emit_expr(s.expr, consume=True)
                return [f"{pad}{val};"] if val not in ("", "((void)0)") else []
            if self.is_copy(t):
                val = self.emit_expr(s.expr, consume=True)
                return [f"{pad}(void)({val});"]
            tmp = self.temp("discard")
            val = self.emit_expr(s.expr, consume=True)
            d = self.ensure_drop(t)
            return [f"{pad}{{ {self.ctype(t)} {tmp} = {val}; {d}(&{tmp}); }}"]

        if isinstance(s, ast.If):
            cond = self.emit_expr(s.cond, consume=False)
            out = [f"{pad}if ({cond}) {{"]
            out.extend(self.emit_block(s.then_body, indent + 1))
            for c, body in s.elifs:
                expr = self.emit_expr(c, consume=False)
                out.append(f"{pad}}} else if ({expr}) {{")
                out.extend(self.emit_block(body, indent + 1))
            if s.else_body is not None:
                out.append(f"{pad}}} else {{")
                out.extend(self.emit_block(s.else_body, indent + 1))
            out.append(f"{pad}}}")
            return out

        if isinstance(s, ast.While):
            cond = self.emit_expr(s.cond, consume=False)
            out = [f"{pad}while ({cond}) {{"]
            self.loop_stack.append(len(self.drop_scopes))
            out.extend(self.emit_block(s.body, indent + 1))
            self.loop_stack.pop()
            out.append(f"{pad}}}")
            return out

        if isinstance(s, ast.For):
            return self.emit_for(s, indent, pad)

        if isinstance(s, ast.Return):
            out = []
            if s.value is not None:
                val = self.emit_expr(s.value, consume=True)
                tmp = self.temp("ret")
                out.append(f"{pad}{self.ctype(self.concrete(s.value.ty))} {tmp} = {val};")
                out.extend(f"{pad}{p}" for p in self.pending)
                for d in self.emit_all_drops(0):
                    out.append(f"{pad}{d}")
                out.append(f"{pad}return {tmp};")
            else:
                for d in self.emit_all_drops(0):
                    out.append(f"{pad}{d}")
                out.extend(f"{pad}{p}" for p in self.pending)
                out.append(f"{pad}return;")
            self.pending = []
            return out

        if isinstance(s, ast.Break):
            depth = self.loop_stack[-1] if self.loop_stack else 0
            return ([f"{pad}{d}" for d in self.emit_all_drops(depth)]
                    + [f"{pad}break;"])

        if isinstance(s, ast.Continue):
            depth = self.loop_stack[-1] if self.loop_stack else 0
            return ([f"{pad}{d}" for d in self.emit_all_drops(depth)]
                    + [f"{pad}continue;"])

        if isinstance(s, ast.UnsafeStmt):
            return ["{"] + self.emit_block(s.body, indent + 1) + [f"{pad}}}"]

        raise CodegenError(f"cannot generate statement {type(s).__name__}")

    def emit_block(self, stmts, indent: int) -> list[str]:
        self.var_scopes.append({})
        self.drop_scopes.append([])
        out = []
        for s in stmts:
            out.extend(self.emit_stmt(s, indent))
        pad = self.ind(indent)
        for d in self.emit_all_drops(len(self.drop_scopes) - 1):
            out.append(f"{pad}{d}")
        self.drop_scopes.pop()
        self.var_scopes.pop()
        return out

    def emit_assign(self, s: ast.Assign, indent, pad) -> list[str]:
        target = s.target
        place = self.emit_expr(target, consume=False)
        t = self.concrete(target.ty)
        ct = self.ctype(t)
        if s.op == "=":
            if isinstance(target, ast.Name):
                loc = self.lookup_local(target.name)
                cvar, tt, has_flag = loc
                if isinstance(s.value, ast.Name) and s.value.name == target.name:
                    return []
                val = self.emit_expr(s.value, consume=True)
                out = []
                if not has_flag:
                    out.append(f"{pad}{cvar} = {val};")
                    return out
                tmp = self.temp("asg")
                out.append(f"{pad}{ct} {tmp} = {val};")
                moved_self = f"{self.alive_flag(cvar)} = false;"
                if moved_self in self.pending:
                    self.pending.remove(moved_self)
                else:
                    out.append(f"{pad}if ({self.alive_flag(cvar)}) "
                               f"{{ {self.ensure_drop(tt)}(&{cvar}); }}")
                out.append(f"{pad}{cvar} = {tmp};")
                out.append(f"{pad}{self.alive_flag(cvar)} = true;")
                return out
            val = self.emit_expr(s.value, consume=True)
            if self.is_copy(t):
                return [f"{pad}({place}) = {val};"]
            d = self.ensure_drop(t)
            ptr = self.temp("dst")
            return [f"{pad}{{ {ct}* {ptr} = &({place}); "
                    f"{d}({ptr}); *{ptr} = {val}; }}"]

        op = s.op[:-1]
        if self.is_copy(t):
            val = self.emit_expr(s.value, consume=True)
            return [f"{pad}({place}) = {self.arith(op, t, f'({place})', val)};"]
        # Non-Copy compound assignment: build the new value, then replace.
        rhs = self.emit_expr(s.value, consume=True)
        new_val = self.bin_expr(op, t, f"({place})", s.value, rhs)
        tmp = self.temp("nval")
        out = [f"{pad}{{ {ct} {tmp} = {new_val};"]
        if isinstance(target, ast.Name):
            loc = self.lookup_local(target.name)
            cvar = loc[0]
            out.append(f"{pad}    if ({self.alive_flag(cvar)}) "
                       f"{{ {self.ensure_drop(t)}(&{cvar}); }}")
            out.append(f"{pad}    {cvar} = {tmp};")
            out.append(f"{pad}    {self.alive_flag(cvar)} = true; }}")
        else:
            out.append(f"{pad}    {self.ensure_drop(t)}(&({place}));")
            out.append(f"{pad}    ({place}) = {tmp}; }}")
        return out

    def simple_bin(self, op, a, b):
        return f"(({a}) {op} ({b}))"

    def arith(self, op, t, a, b):
        t = self.concrete(t)
        if isinstance(t, TCon) and t.name in INT_TYPES and op in ("/", "%", "<<", ">>"):
            fn = {"/": "div", "%": "mod", "<<": "shl", ">>": "shr"}[op]
            c = self.ctype(t)
            return f"atr_{fn}_{t.name}(({c})({a}), ({c})({b}))"
        return self.simple_bin(op, a, b)

    def bin_expr(self, op, t, left_c, right_ast, right_c) -> str:
        if op == "+" and (_stringy(t) or _stringy(self.concrete(right_ast.ty))):
            return self.view_expr(
                None, right_ast,
                lambda a, b: f"atr_string_concat({a}, {b})", "AtrString",
                left_code=left_c)
        return self.arith(op, t, left_c, right_c)

    def view_expr(self, left_e, right_e, build, result_c, left_code=None) -> str:
        decls = []
        cleanups = []

        def view(e, code):
            c = code if code is not None else self.emit_expr(e, consume=False)
            t = self.concrete(e.ty)
            if isinstance(t, TRef):
                inner = self.concrete(t.inner)
                if isinstance(inner, TCon) and inner.name == "String":
                    return f"atr_string_view({c})"
                if isinstance(inner, TCon) and inner.name == "str":
                    return f"(*({c}))"
            if isinstance(t, TCon) and t.name == "String":
                if code is None and isinstance(e, (ast.Name, ast.Field, ast.Index, ast.Deref)):
                    return f"atr_string_view(&({c}))"
                tmp = self.temp("sv")
                decls.append(f"AtrString {tmp} = {c};")
                cleanups.append(f"atr_string_free(&{tmp});")
                return f"atr_string_view(&{tmp})"
            return c

        lv = (view(left_e, left_code) if left_e is not None
              else f"atr_string_view(&({left_code}))")
        rv = view(right_e, None)
        call = build(lv, rv)
        if not decls:
            return call
        res = self.temp("res")
        body = decls + [f"{result_c} {res} = {call};"] + cleanups + [f"{res};"]
        return "({ " + " ".join(body) + " })"

    def str_view_of_c(self, c, t):
        if _stringy(t):
            return f"atr_string_view(&({c}))"
        return c

    def emit_for(self, s: ast.For, indent, pad) -> list[str]:
        it = s.iter
        elem_t = self.concrete(s.elem_ty)
        elem_c = self.ctype(elem_t)
        out = []
        if isinstance(it, ast.Range):
            start = self.emit_expr(it.start, consume=False) if it.start else "0"
            end = self.emit_expr(it.end, consume=False) if it.end else "0"
            startv = self.temp("start")
            endv = self.temp("end")
            cvar = f"v_{s.name}_{self.temp_n}"
            self.temp_n += 1
            op = "<=" if it.inclusive else "<"
            out.append(f"{pad}{{")
            out.append(f"{pad}    {elem_c} {startv} = {start};")
            out.append(f"{pad}    {elem_c} {endv} = {end};")
            out.append(f"{pad}    for ({elem_c} {cvar} = {startv}; "
                       f"{cvar} {op} {endv}; {cvar}++) {{")
            self.var_scopes.append({})
            self.drop_scopes.append([])
            self.var_scopes[-1][s.name] = (cvar, elem_t, False)
            self.loop_stack.append(len(self.drop_scopes))
            for st in s.body:
                out.extend(self.emit_stmt(st, indent + 2))
            self.loop_stack.pop()
            for d in self.emit_all_drops(len(self.drop_scopes) - 1):
                out.append(f"{pad}        {d}")
            self.drop_scopes.pop()
            self.var_scopes.pop()
            out.append(f"{pad}    }}")
            out.append(f"{pad}}}")
            return out

        lp = self.list_ptr(it)
        loop_pre = self.pre[:]
        del self.pre[:]
        loop_cleanup = self.pending[:]
        del self.pending[:]
        out.extend(pad + p for p in loop_pre)
        list_c = self.ctype(self.list_type_of(it))
        idx = self.temp("i")
        lst = f"{idx}_list"
        cvar = f"v_{s.name}_{self.temp_n}"
        self.temp_n += 1
        out.append(f"{pad}{{")
        out.append(f"{pad}    {list_c}* {lst} = {lp};")
        out.append(f"{pad}    for (size_t {idx} = 0; {idx} < {lst}->len; {idx}++) {{")
        self.var_scopes.append({})
        self.drop_scopes.append([])
        self.var_scopes[-1][s.name] = (cvar, elem_t, False)
        if isinstance(elem_t, TRef):
            out.append(f"{pad}        {elem_c} {cvar} = &{lst}->data[{idx}];")
        else:
            out.append(f"{pad}        {elem_c} {cvar} = {lst}->data[{idx}];")
        self.loop_stack.append(len(self.drop_scopes))
        for st in s.body:
            out.extend(self.emit_stmt(st, indent + 2))
        self.loop_stack.pop()
        for d in self.emit_all_drops(len(self.drop_scopes) - 1):
            out.append(f"{pad}        {d}")
        self.drop_scopes.pop()
        self.var_scopes.pop()
        out.append(f"{pad}    }}")
        out.append(f"{pad}}}")
        out.extend(pad + p for p in loop_cleanup)
        return out

    def list_type_of(self, e):
        t = self.concrete(e.ty)
        while isinstance(t, TRef):
            t = self.concrete(t.inner)
        if isinstance(t, TCon) and t.name == "List":
            return t
        raise CodegenError("internal: not a List")

    def list_elem(self, e):
        return self.concrete(self.list_type_of(e).args[0])

    def list_ptr(self, base) -> str:
        t = self.concrete(base.ty)
        if isinstance(t, TRef):
            return self.emit_expr(base, consume=False)
        if not isinstance(base, (ast.Name, ast.Field, ast.Index, ast.Deref)):
            _, tmp = self.materialize(base, "list")
            return f"(&{tmp})"
        c = self.emit_expr(base, consume=False)
        return f"(&({c}))"

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------
    def emit_expr(self, e, consume=False) -> str:
        if getattr(e, "coerce_dyn", None) is not None:
            return self.emit_dyn_coercion(e)
        if getattr(e, "coerce_str_to_string", False):
            return f"atr_string_from_view({self.emit_expr_str_base(e, consume)})"
        if isinstance(e, ast.IntLit):
            return str(e.value)
        if isinstance(e, ast.FloatLit):
            return repr(float(e.value))
        if isinstance(e, ast.BoolLit):
            return "true" if e.value else "false"
        if isinstance(e, ast.UnitLit):
            return "((AtrUnit){0})"
        if isinstance(e, ast.CharLit):
            return f"((uint32_t){ord(e.value)})"
        if isinstance(e, ast.StrLit):
            return self.emit_string_lit(e)
        if isinstance(e, ast.Name):
            return self.emit_name(e, consume)
        if isinstance(e, ast.Field):
            return self.emit_field(e, consume)
        if isinstance(e, ast.Index):
            lp = self.list_ptr(e.base)
            idx = self.emit_expr(e.index, consume=False)
            key = self.mangle(self.list_type_of(e.base))
            return f"(*atr_list_{key}_at({lp}, {idx}))"
        if isinstance(e, ast.Deref):
            bt = self.concrete(e.base.ty)
            base = self.emit_expr(e.base, consume=False)
            if isinstance(bt, TCon) and bt.name in ("Box", "Rc"):
                return f"(*(({base}).ptr))"
            return f"(*({base}))"
        if isinstance(e, ast.Ref):
            return f"(&({self.emit_expr(e.base, consume=False)}))"
        if isinstance(e, ast.Unary):
            x = self.emit_expr(e.operand, consume=True)
            return f"({e.op}({x}))"
        if isinstance(e, ast.Binary):
            return self.emit_binary(e)
        if isinstance(e, ast.Cast):
            x = self.emit_expr(e.base, consume=True)
            return f"(({self.ctype(e.ty)})({x}))"
        if isinstance(e, ast.Try):
            return self.emit_try(e)
        if isinstance(e, ast.Lambda):
            return self.emit_lambda(e)
        if isinstance(e, ast.Call):
            return self.emit_call(e)
        if isinstance(e, ast.StructLit):
            t = self.concrete(e.ty)
            c = self.ctype(t)
            info = self.m.structs[t.name]
            mp = dict(zip(info.generics, t.args))
            by_name = dict(e.fields)
            field_types = dict(info.fields)
            parts = []
            for fname, _ in info.fields:
                ft = self.concrete(subst(field_types[fname], mp))
                val = self.emit_expr(by_name[fname], consume=True)
                parts.append(f".f_{fname} = {val}")
            return f"(({c}){{ {', '.join(parts)} }})"
        if isinstance(e, ast.ListLit):
            return self.emit_list_lit(e)
        if isinstance(e, ast.Range):
            raise CodegenError("a range is only valid as a for-loop iterable")
        if isinstance(e, ast.Match):
            return self.emit_match(e)
        if isinstance(e, ast.AsmExpr):
            inputs = ", ".join(f'"r"({self.emit_expr(o, consume=True)})'
                               for o in e.operands)
            tmpl = _asm_template(e.template)
            if inputs:
                return (f'({{ __asm__ __volatile__("{tmpl}" : : {inputs} : "memory"); '
                        f'((void)0); }})')
            return f'({{ __asm__ __volatile__("{tmpl}" : : : "memory"); ((void)0); }})'
        raise CodegenError(f"cannot generate expression {type(e).__name__}")

    def emit_name(self, e, consume) -> str:
        loc = self.lookup_local(e.name)
        if loc is None:
            ref = self.m.find_unqualified_variant(e.name)
            if ref is not None:
                return self.emit_variant_ctor(ref, [], e.ty)
            if e.name in self.m.fns:
                info = self.m.fns[e.name]
                if getattr(e, "fn_ref", None) is not None:
                    return self.ensure_fn(info, [])
                return self.fn_cname(info, [])
            raise CodegenError(f"internal: unresolved name '{e.name}'")
        cvar, t, has_flag = loc
        if consume and has_flag:
            self.mark_moved(cvar)
        return cvar

    def emit_field(self, e, consume=False) -> str:
        if isinstance(e.base, ast.Name) and self.lookup_local(e.base.name) is None                 and e.base.name in self.m.enums:
            ref = self.m.variant_ref(e.base.name, e.field)
            if ref is None:
                raise CodegenError(f"internal: unknown variant {e.base.name}.{e.field}")
            return self.emit_variant_ctor(ref, [], e.ty)
        bt = self.concrete(e.base.ty)
        base = self.emit_expr(e.base, consume=False)
        if consume and not self.is_copy(e.ty):
            root = self.root_cvar(e.base)
            if root and self.root_has_flag(root):
                self.mark_moved(root)
        if isinstance(bt, TCon) and bt.name in ("Box", "Rc"):
            return f"((*(({base}).ptr)).f_{e.field})"
        if isinstance(bt, (TRef, TPtr)):
            return f"((*({base})).f_{e.field})"
        return f"(({base}).f_{e.field})"

    def emit_list_lit(self, e) -> str:
        t = self.concrete(e.ty)
        c = self.ctype(t)
        key = self.mangle(t)
        tmp = self.temp("list")
        pieces = [f"{c} {tmp} = atr_list_{key}_new();"]
        for item in e.items:
            pieces.append(f"atr_list_{key}_push(&{tmp}, "
                          f"{self.emit_expr(item, consume=True)});")
        pieces.append(f"{tmp};")
        return "({ " + " ".join(pieces) + " })"

    def emit_expr_str_base(self, e, consume):
        saved = getattr(e, "coerce_str_to_string", False)
        e.coerce_str_to_string = False
        try:
            if isinstance(e, ast.StrLit):
                text = "".join(seg.text for seg in e.segs)
                b = text.encode("utf-8")
                return f"atr_str_lit({_c_bytes(b)}, {len(b)})"
            return self.emit_expr(e, consume)
        finally:
            e.coerce_str_to_string = saved

    def emit_string_lit(self, e) -> str:
        if not any(seg.kind == "expr" for seg in e.segs):
            text = "".join(seg.text for seg in e.segs)
            b = text.encode("utf-8")
            return f"atr_str_lit({_c_bytes(b)}, {len(b)})"
        tmp = self.temp("str")
        parts = []
        for seg in e.segs:
            if seg.kind == "lit":
                b = seg.text.encode("utf-8")
                if b:
                    parts.append(f"atr_string_push_bytes(&{tmp}, {_c_bytes(b)}, {len(b)});")
            else:
                parts.append(self.push_show(tmp, seg.parts[0]))
        return f"({{ AtrString {tmp} = atr_string_new(); {' '.join(parts)} {tmp}; }})"

    def push_show(self, buf: str, e) -> str:
        t = self.concrete(e.ty)
        if isinstance(t, (TRef, TPtr)):
            inner = self.concrete(t.inner)
            c = self.emit_expr(e, consume=False)
            if isinstance(inner, TCon):
                if inner.name in ("str", "String"):
                    return f"atr_string_push_bytes(&{buf}, {c}->ptr, {c}->len);"
                if inner.name == "char":
                    return f"atr_string_push_char(&{buf}, *{c});"
                if inner.name in C_PRIMS:
                    return f"atr_string_push_{_push_suffix(inner)}(&{buf}, *{c});"
            show = self.ensure_show(inner)
            return f"{show}(&{buf}, {c});"
        c = self.emit_expr(e, consume=False)
        if isinstance(t, TCon):
            if t.name == "str":
                return f"atr_string_push_bytes(&{buf}, ({c}).ptr, ({c}).len);"
            if t.name == "String":
                addr = self.addr_if_place(e)
                if addr:
                    return f"atr_string_push_bytes(&{buf}, ({addr})->ptr, ({addr})->len);"
                tmp = self.temp("sv")
                return (f"{{ AtrString {tmp} = {c}; "
                        f"atr_string_push_bytes(&{buf}, {tmp}.ptr, {tmp}.len); "
                        f"atr_string_free(&{tmp}); }}")
            if t.name == "char":
                return f"atr_string_push_char(&{buf}, {c});"
            if t.name in C_PRIMS:
                return f"atr_string_push_{_push_suffix(t)}(&{buf}, {c});"
        show = self.ensure_show(t)
        addr = self.addr_if_place(e)
        if addr:
            return f"{show}(&{buf}, {addr});"
        tmp = self.temp("v")
        ct = self.ctype(t)
        d = self.ensure_drop(t)
        cleanup = f"{d}(&{tmp});" if d else ""
        return f"{{ {ct} {tmp} = {c}; {show}(&{buf}, &{tmp}); {cleanup} }}"

    def addr_if_place(self, e):
        if isinstance(e, (ast.Name, ast.Field, ast.Index)):
            return f"(&({self.emit_expr(e, consume=False)}))"
        if isinstance(e, ast.Deref):
            return f"({self.emit_expr(e, consume=False)})"
        return None

    def emit_binary(self, e) -> str:
        op = e.op
        lt = self.concrete(e.left.ty)
        rt = self.concrete(e.right.ty)
        res = self.concrete(e.ty)

        if op == "+" and (_stringy(lt) or _stringy(rt)):
            return self.view_expr(
                e.left, e.right,
                lambda a, b: f"atr_string_concat({a}, {b})", "AtrString")

        if op in ("==", "!="):
            if _stringy(lt) or _stringy(rt):
                eq = self.view_expr(
                    e.left, e.right,
                    lambda a, b: f"atr_str_eq({a}, {b})", "bool")
                return eq if op == "==" else f"(!{eq})"
            eqf = self.ensure_eq(lt)
            a = self.emit_expr(e.left, consume=False)
            b = self.emit_expr(e.right, consume=False)
            if eqf:
                pa = self.addr_if_place(e.left) or f"(&({a}))"
                pb = self.addr_if_place(e.right) or f"(&({b}))"
                eq = f"{eqf}({pa}, {pb})"
                return eq if op == "==" else f"(!{eq})"
            return f"(({a}) {op} ({b}))"

        if op in ("<", "<=", ">", ">=") and _stringy(lt):
            return self.view_expr(
                e.left, e.right,
                lambda a, b: f"(atr_str_cmp({a}, {b}) {op} 0)", "bool")

        a = self.emit_expr(e.left, consume=True)
        b = self.emit_expr(e.right, consume=True)
        if op == "**":
            if _float_t(res):
                return f"pow({a}, {b})"
            return f"atr_ipow_i64((int64_t)({a}), (int64_t)({b}))"
        if op == "/" and _float_t(res):
            rc = self.ctype(res)
            return f"(({rc})({a}) / ({rc})({b}))"
        if op == "%" and _float_t(res):
            return f"fmod({a}, {b})"
        return self.arith(op, res, a, b)

    def str_view(self, e) -> str:
        t = self.concrete(e.ty)
        if isinstance(t, TRef):
            inner = self.concrete(t.inner)
            if isinstance(inner, TCon) and inner.name == "String":
                return f"atr_string_view({self.emit_expr(e, consume=False)})"
            if isinstance(inner, TCon) and inner.name == "str":
                return f"(*({self.emit_expr(e, consume=False)}))"
        if isinstance(t, TCon) and t.name == "str":
            return self.emit_expr(e, consume=False)
        if isinstance(t, TCon) and t.name == "String":
            addr = self.addr_if_place(e)
            if addr:
                return f"atr_string_view({addr})"
            _, tmp = self.materialize(e, "sv")
            return f"atr_string_view(&{tmp})"
        raise CodegenError(f"internal: `{t.show()}` is not a string")

    def mk_ctor(self, ty, variant: str, args: list) -> str:
        c = self.ctype(ty)
        return f"atr_mk_{c}_{variant}({', '.join(args)})"

    # ------------------------------------------------------------------
    # calls
    # ------------------------------------------------------------------
    def emit_call(self, e) -> str:
        solved = getattr(e, "solved", None)
        if solved is None:
            raise CodegenError("internal: unresolved call")
        kind = solved[0]
        if kind == "builtin":
            return self.emit_builtin(e, solved[1])
        if kind == "fn":
            info = solved[1]
            cname = self.ensure_fn(info, self.call_typeargs(e, info))
            args = []
            for a, pt in zip(e.args, getattr(e, "param_types", [])):
                consume = not isinstance(self.concrete(pt), TRef)
                args.append(self.emit_expr(a, consume=consume))
            return f"{cname}({', '.join(args)})"
        if kind == "dyn_method":
            return self.emit_dyn_method(e, solved[1], solved[2])
        if kind == "closure":
            loc = self.lookup_local(solved[1])
            if loc is None:
                raise CodegenError(f"internal: closure '{solved[1]}' is not a local")
            cvar = loc[0]
            args = [f"(&{cvar})"]
            for a, pt in zip(e.args, getattr(e, "param_types", [])):
                consume = not isinstance(self.concrete(pt), TRef)
                args.append(self.emit_expr(a, consume=consume))
            return f"({cvar}).__fn({', '.join(args)})"
        if kind == "fnptr":
            loc = self.lookup_local(solved[1])
            return f"{loc[0] if loc else solved[1]}(" \
                   f"{', '.join(self.emit_expr(a, consume=True) for a in e.args)})"
        if kind == "variant":
            args = [self.emit_expr(a, consume=True) for a in e.args]
            return self.emit_variant_ctor(solved[1], args, e.ty)
        if kind == "strmeth":
            return self.emit_str_method(e, solved[1])
        if kind == "method":
            return self.emit_method(e, solved[1])
        if kind == "list":
            return self.emit_list_method(e, solved[1])
        if kind == "enum_method":
            return self.emit_enum_method(e, solved[1])
        if kind == "trait_method":
            return self.emit_trait_method(e, solved[1], solved[2])
        if kind == "rc_method":
            return self.emit_rc_method(e, solved[1])
        raise CodegenError(f"internal: unknown call kind {kind}")

    def call_typeargs(self, e, info: FnInfo):
        if not info.generics:
            return []
        out = []
        for g in info.generics:
            ov = info.generic_vars[g]
            fresh = getattr(e, "inst", {}).get(ov.id, ov)
            out.append(self.concrete(fresh))
        return out

    def emit_variant_ctor(self, ref: VariantRef, args: list, result_ty) -> str:
        t = self.concrete(result_ty)
        c = self.ctype(t)
        variant = self.m.enums[ref.enum].variants[ref.index][0]
        return f"atr_mk_{c}_{variant}({', '.join(args)})"

    def emit_method(self, e, info: FnInfo) -> str:
        cname = self.ensure_fn(info, self.call_typeargs(e, info))
        recv_e = e.callee.base
        auto = getattr(e, "self_auto", "none")
        if auto in ("ref", "mutref"):
            if isinstance(recv_e, (ast.Name, ast.Field, ast.Index, ast.Deref)):
                recv = f"(&({self.emit_expr(recv_e, consume=False)}))"
            else:
                _, tmp = self.materialize(recv_e, "recv")
                recv = f"(&{tmp})"
        elif auto == "deref":
            recv = f"(*({self.emit_expr(recv_e, consume=False)}))"
        else:
            recv = self.emit_expr(recv_e, consume=(info.self_mode is None))
        args = [recv]
        for a, pt in zip(e.args, getattr(e, "param_types", [])):
            consume = not isinstance(self.concrete(pt), TRef)
            args.append(self.emit_expr(a, consume=consume))
        return f"{cname}({', '.join(args)})"

    def emit_lambda(self, e) -> str:
        if getattr(e, "captures", None):
            return self.emit_capturing_lambda(e)
        ret = self.concrete(e.result_type)
        rc = self.ctype(ret)
        name = f"atr_lambda_{self.temp_n}"
        self.temp_n += 1
        params = []
        for p, pt in zip(e.params, e.param_types):
            pt = self.concrete(pt)
            pc = self.ctype(pt)
            pn = f"p_{p.name}_{self.temp_n}"
            self.temp_n += 1
            params.append((p.name, pn, pt, pc))
        saved = (self.var_scopes, self.drop_scopes, self.pre, self.pending,
                 self.current_fn)
        self.var_scopes = [{}]
        self.drop_scopes = [[]]
        self.pre = []
        self.pending = []
        self.current_fn = None
        for pname, pn, pt, pc in params:
            self.var_scopes[-1][pname] = (pn, pt, not self.is_copy(pt))
            if not self.is_copy(pt):
                self.drop_scopes[-1].append((pn, pt))
        val = self.emit_expr(e.body, consume=True)
        pre = list(self.pre)
        pend = list(self.pending)
        drops = self.emit_all_drops(0)
        lines = [f"    bool {self.alive_flag(pn)} = true;"
                 for _, pn, pt, _ in params if not self.is_copy(pt)]
        lines.extend("    " + p for p in pre)
        lines.append(f"    {rc} _lambda_ret = {val};")
        lines.extend("    " + p for p in pend)
        lines.extend("    " + d for d in drops)
        lines.append("    return _lambda_ret;")
        plist = ", ".join(f"{pc} {pn}" for _, pn, _, pc in params) or "void"
        self.fn_bodies.append(
            f"static {rc} {name}({plist}) {{\n" + "\n".join(lines) + "\n}\n\n")
        (self.var_scopes, self.drop_scopes, self.pre, self.pending,
         self.current_fn) = saved
        return name

    def emit_capturing_lambda(self, e) -> str:
        ret = self.concrete(e.result_type)
        rc = self.ctype(ret)
        ct = TCon(e.closure_name, [])
        env_c = self.ctype(ct)
        wname = f"atr_closure_{e.closure_name.split('_')[-1]}"
        params = []
        for p, pt in zip(e.params, e.param_types):
            pt = self.concrete(pt)
            pc = self.ctype(pt)
            pn = f"p_{p.name}_{self.temp_n}"
            self.temp_n += 1
            params.append((p.name, pn, pt, pc))
        plist = [f"{env_c}* _env"] + [f"{pc} {pn}" for _, pn, _, pc in params]
        self.fn_protos[wname] = ("static " + rc + " " + wname + "("
                                 + ", ".join(plist) + ");" + chr(10))
        saved = (self.var_scopes, self.drop_scopes, self.pre, self.pending,
                 self.current_fn)
        self.var_scopes = [{}]
        self.drop_scopes = [[]]
        self.pre = []
        self.pending = []
        self.current_fn = None
        for cname, cty in e.captures:
            cty = self.concrete(cty)
            cvar = f"c_{cname}"
            self.pre.append(f"{self.ctype(cty)} {cvar} = (*_env).f_{cname};")
            self.var_scopes[-1][cname] = (cvar, cty, False)
        for pname, pn, pt, pc in params:
            self.var_scopes[-1][pname] = (pn, pt, not self.is_copy(pt))
            if not self.is_copy(pt):
                self.drop_scopes[-1].append((pn, pt))
        val = self.emit_expr(e.body, consume=True)
        pre = list(self.pre)
        pend = list(self.pending)
        drops = self.emit_all_drops(0)
        lines = [f"    bool {self.alive_flag(pn)} = true;"
                 for _, pn, pt, _ in params if not self.is_copy(pt)]
        lines.extend("    " + p for p in pre)
        lines.append(f"    {rc} _lambda_ret = {val};")
        lines.extend("    " + p for p in pend)
        lines.extend("    " + d for d in drops)
        lines.append("    return _lambda_ret;")
        self.fn_bodies.append(
            "static " + rc + " " + wname + "(" + ", ".join(plist) + ") {"
            + chr(10) + chr(10).join(lines) + chr(10) + "}" + chr(10) + chr(10))
        (self.var_scopes, self.drop_scopes, self.pre, self.pending,
         self.current_fn) = saved
        tmp = self.temp("clo")
        init = [f"{env_c} {tmp};", f"{tmp}.__fn = {wname};"]
        for cname, _ in e.captures:
            loc = self.lookup_local(cname)
            if loc is None:
                raise CodegenError(
                    f"internal: captured name '{cname}' is not a local")
            init.append(f"{tmp}.f_{cname} = {loc[0]};")
        init.append(tmp + ";")
        return "({ " + " ".join(init) + " })"

    def emit_try(self, e) -> str:
        if self.current_fn is None:
            raise CodegenError("internal: `?` outside a function")
        op_t = self.concrete(e.base.ty)
        if isinstance(op_t, TRef):
            op_t = self.concrete(op_t.inner)
        c = self.ctype(op_t)
        val = self.emit_expr(e.base, consume=True)
        pend = " ".join(self.pending)
        self.pending = []
        tmp = self.temp("q")
        ret = self.concrete(self.current_fn.ret)
        info = self.m.enums[op_t.name]
        names = [v[0] for v in info.variants]
        ok_i = names.index("Ok" if op_t.name == "Result" else "Some")
        drops = " ".join(self.emit_all_drops(0))
        if op_t.name == "Result":
            mk = self.mk_ctor(ret, "Err", [f"{tmp}.u.V_Err.v0"])
        else:
            mk = self.mk_ctor(ret, "None", [])
        return (f"({{ {c} {tmp} = {val}; {pend} "
                f"if ({tmp}.tag != {ok_i}) {{ {drops} return {mk}; }} "
                f"{tmp}.u.V_{'Ok' if op_t.name == 'Result' else 'Some'}.v0; }})")

    def enum_receiver_addr(self, base, consumes: bool) -> str:
        place = self.addr_if_place(base)
        if place is not None:
            return f"(&({self.emit_expr(base, consume=consumes)}))"
        t, tmp = self.materialize(base, "en")
        if consumes and not self.is_copy(t):
            d = self.ensure_drop(t)
            line = f"{d}(&{tmp});"
            if line in self.pending:
                self.pending.remove(line)
        return f"(&{tmp})"

    def emit_enum_method(self, e, method: str) -> str:
        base = e.callee.base
        rt = self.concrete(base.ty)
        is_ref = isinstance(rt, TRef)
        et = self.concrete(rt.inner) if is_ref else rt
        c = self.ctype(et)
        info = self.m.enums[et.name]
        names = [v[0] for v in info.variants]
        consumes = method in ("unwrap", "expect", "unwrap_or", "ok", "err", "ok_or")
        if is_ref:
            addr = self.emit_expr(base, consume=False)
        else:
            addr = self.enum_receiver_addr(base, consumes)
        ok_name = "Ok" if et.name == "Result" else "Some"
        bad_name = "Err" if et.name == "Result" else "None"
        ok_i = names.index(ok_name)
        bad_i = names.index(bad_name)
        if method in ("is_some", "is_ok"):
            return f"(({addr})->tag == {ok_i})"
        if method in ("is_none", "is_err"):
            return f"(({addr})->tag == {bad_i})"
        if method == "unwrap":
            msg = ("called unwrap on an Err value" if et.name == "Result"
                   else "called unwrap on None")
            return (f"({{ {c}* _p = {addr}; if (_p->tag != {ok_i}) "
                    f'atr_panic("{msg}"); _p->u.V_{ok_name}.v0; }})')
        if method == "expect":
            msg = self.str_view(e.args[0])
            return (f"({{ {c}* _p = {addr}; if (_p->tag != {ok_i}) "
                    f"atr_panic_str({msg}); _p->u.V_{ok_name}.v0; }})")
        if method == "unwrap_or":
            tmp = self.temp("en")
            base_pre = list(self.pre)
            self.pre = []
            dflt = self.emit_expr(e.args[0], consume=True)
            dpre = list(self.pre)
            self.pre = base_pre
            return (f"({{ {' '.join(base_pre)} {c}* _p = {addr}; "
                    f"{self.ctype(self.concrete(e.ty))} {tmp}; "
                    f"if (_p->tag == {ok_i}) {{ {tmp} = _p->u.V_{ok_name}.v0; }} "
                    f"else {{ {' '.join(dpre)} {tmp} = {dflt}; }} {tmp}; }})")
        if method == "ok_or":
            tmp = self.temp("en")
            base_pre = list(self.pre)
            self.pre = []
            errv = self.emit_expr(e.args[0], consume=True)
            dpre = list(self.pre)
            self.pre = base_pre
            rc = self.ctype(self.concrete(e.ty))
            mk_ok = self.mk_ctor(self.concrete(e.ty), "Ok",
                                 [f"_p->u.V_{ok_name}.v0"])
            mk_err = self.mk_ctor(self.concrete(e.ty), "Err", [errv])
            return (f"({{ {' '.join(base_pre)} {c}* _p = {addr}; {rc} {tmp}; "
                    f"if (_p->tag == {ok_i}) {{ {tmp} = {mk_ok}; }} "
                    f"else {{ {' '.join(dpre)} {tmp} = {mk_err}; }} {tmp}; }})")
        if method in ("ok", "err"):
            src_variant = ok_name if method == "ok" else bad_name
            src_i = ok_i if method == "ok" else bad_i
            rc = self.ctype(self.concrete(e.ty))
            tmp = self.temp("en")
            some = self.mk_ctor(self.concrete(e.ty), "Some",
                                [f"_p->u.V_{src_variant}.v0"])
            none = self.mk_ctor(self.concrete(e.ty), "None", [])
            return (f"({{ {c}* _p = {addr}; {rc} {tmp}; "
                    f"if (_p->tag == {src_i}) {{ {tmp} = {some}; }} "
                    f"else {{ {tmp} = {none}; }} {tmp}; }})")
        raise CodegenError(f"internal: unknown enum method {method}")

    def emit_trait_method(self, e, trait: str, method: str) -> str:
        recv = self.concrete(e.callee.base.ty)
        inner = self.concrete(recv.inner) if isinstance(recv, TRef) else recv
        if not isinstance(inner, TCon):
            raise CodegenError(f"internal: trait dispatch on `{inner.show()}`")
        tri = self.m.traits.get(trait)
        impl = tri.impls.get(inner.name) if tri else None
        info = impl.get(method) if impl else None
        if info is None:
            raise CodegenError(f"`{inner.show()}` does not implement `{trait}.{method}`")
        return self.emit_method(e, info)

    def emit_rc_method(self, e, method: str) -> str:
        base = e.callee.base
        rt = self.concrete(base.ty)
        et = self.concrete(rt.inner) if isinstance(rt, TRef) else rt
        c = self.ctype(et)
        val = self.emit_expr(base, consume=False)
        tmp = self.temp("rc")
        if method == "clone":
            return (f"({{ {c} {tmp} = {val}; if ({tmp}.count) (*{tmp}.count)++; "
                    f"{tmp}; }})")
        if method == "strong_count":
            return (f"({{ {c} {tmp} = {val}; "
                    f"{tmp}.count ? *{tmp}.count : (size_t)0; }})")
        raise CodegenError(f"internal: unknown Rc method {method}")

    def emit_str_method(self, e, method: str) -> str:
        t = self.concrete(e.callee.base.ty)
        inner = self.concrete(t.inner) if isinstance(t, TRef) else t
        base = e.callee.base
        if isinstance(inner, TCon) and inner.name == "String":
            if isinstance(t, TRef):
                addr = self.emit_expr(base, consume=False)
            else:
                addr = self.addr_if_place(base)
                if addr is None:
                    _, tmp = self.materialize(base, "str")
                    addr = f"(&{tmp})"
            size = f"(({addr})->len)"
        elif isinstance(t, TRef) and isinstance(inner, TCon) and inner.name == "str":
            size = f"((*{self.emit_expr(base, consume=False)}).len)"
        else:
            size = f"(({self.emit_expr(base, consume=False)}).len)"
        if method == "clone":
            return f"atr_string_from_view({self.str_view(base)})"
        return size if method == "len" else f"({size} == 0)"

    def emit_list_method(self, e, method: str) -> str:
        base = e.callee.base
        lp = self.list_ptr(base)
        key = self.mangle(self.list_type_of(base))
        if method == "len":
            return f"(({lp})->len)"
        if method == "is_empty":
            return f"(({lp})->len == 0)"
        if method == "push":
            v = self.emit_expr(e.args[0], consume=True)
            return f"({{ atr_list_{key}_push({lp}, {v}); ((void)0); }})"
        if method == "pop":
            return f"atr_list_{key}_pop({lp})"
        if method == "get":
            return f"atr_list_{key}_get({lp}, {self.emit_expr(e.args[0])})"
        if method == "set":
            i = self.emit_expr(e.args[0], consume=False)
            v = self.emit_expr(e.args[1], consume=True)
            return f"({{ atr_list_{key}_set({lp}, {i}, {v}); ((void)0); }})"
        if method == "clear":
            return f"({{ atr_list_{key}_clear({lp}); ((void)0); }})"
        raise CodegenError(f"internal: unknown list method {method}")

    def emit_builtin(self, e, name: str) -> str:
        if name in ("print", "println", "write"):
            tmp = self.temp("p")
            pushes = []
            for i, a in enumerate(e.args):
                if i:
                    pushes.append(f'atr_string_push_cstr(&{tmp}, " ");')
                pushes.append(self.push_show(tmp, a))
            nl = (f"atr_print_str(atr_string_view(&{tmp}));" if name == "write"
                  else f"atr_println_str(atr_string_view(&{tmp}));")
            body = " ".join(pushes + [nl, f"atr_string_free(&{tmp});", "((void)0);"])
            return f"({{ AtrString {tmp} = atr_string_new(); {body} }})"
        if name == "panic":
            return f"({{ atr_panic_str({self.str_view(e.args[0])}); ((void)0); }})"
        if name == "assert":
            cond = self.emit_expr(e.args[0], consume=False)
            msg = (self.str_view(e.args[1]) if len(e.args) > 1
                   else 'atr_str_lit("assertion failed", 17)')
            return f"({{ if (!({cond})) atr_panic_str({msg}); ((void)0); }})"
        if name == "exit":
            return f"({{ exit((int)({self.emit_expr(e.args[0], consume=True)})); ((void)0); }})"
        if name == "len":
            t = self.concrete(e.args[0].ty)
            if isinstance(t, TRef):
                t = self.concrete(t.inner)
            if isinstance(t, TCon) and t.name == "String":
                orig = self.concrete(e.args[0].ty)
                if isinstance(orig, TRef):
                    addr = self.emit_expr(e.args[0], consume=False)
                else:
                    addr = self.addr_if_place(e.args[0]) or \
                        f"(&({self.emit_expr(e.args[0], consume=False)}))"
                return f"(({addr})->len)"
            if isinstance(t, TCon) and t.name == "str":
                orig = self.concrete(e.args[0].ty)
                if isinstance(orig, TRef):
                    return f"((*{self.emit_expr(e.args[0], consume=False)}).len)"
                return f"(({self.emit_expr(e.args[0], consume=False)}).len)"
            return f"(({self.list_ptr(e.args[0])})->len)"
        if name == "to_str":
            tmp = self.temp("s")
            push = self.push_show(tmp, e.args[0])
            return f"({{ AtrString {tmp} = atr_string_new(); {push} {tmp}; }})"
        if name == "abs":
            a = self.emit_expr(e.args[0], consume=True)
            if _float_t(self.concrete(e.args[0].ty)):
                return f"fabs({a})"
            return f"(({a}) < 0 ? -({a}) : ({a}))"
        if name in ("min", "max"):
            a = self.emit_expr(e.args[0], consume=True)
            b = self.emit_expr(e.args[1], consume=True)
            op = "<" if name == "min" else ">"
            return f"(({a}) {op} ({b}) ? ({a}) : ({b}))"
        if name == "sqrt":
            return f"sqrt({self.emit_expr(e.args[0], consume=True)})"
        if name == "drop":
            t = self.concrete(e.args[0].ty)
            d = self.ensure_drop(t)
            if not d:
                return "((void)0)"
            return f"({{ {d}(&({self.emit_expr(e.args[0], consume=False)})); ((void)0); }})"
        if name == "box":
            t = self.concrete(e.ty)
            c = self.ctype(t)
            elem = self.ctype(self.concrete(t.args[0]))
            val = self.emit_expr(e.args[0], consume=True)
            return (f"({{ {c} _box; _box.ptr = ({elem}*)atr_alloc(sizeof({elem})); "
                    f"*_box.ptr = {val}; _box; }})")
        if name == "rc":
            t = self.concrete(e.ty)
            c = self.ctype(t)
            elem = self.ctype(self.concrete(t.args[0]))
            val = self.emit_expr(e.args[0], consume=True)
            return (f"({{ {c} _rc; _rc.count = (size_t*)atr_alloc(sizeof(size_t)); "
                    f"*_rc.count = 1; _rc.ptr = ({elem}*)atr_alloc(sizeof({elem})); "
                    f"*_rc.ptr = {val}; _rc; }})")
        if name in ("read_file", "write_file", "env", "args", "now_ms"):
            return self.emit_host_builtin(e, name)
        raise CodegenError(f"internal: unknown builtin {name}")

    def emit_host_builtin(self, e, name: str) -> str:
        rt = self.concrete(e.ty)
        if name == "read_file":
            res_c = self.ctype(rt)
            ok = self.mk_ctor(rt, "Ok", ["_o"])
            err = self.mk_ctor(rt, "Err", ["_e"])
            path = self.str_view(e.args[0])
            return (f"({{ AtrString _o = atr_string_new(); AtrString _e = atr_string_new(); "
                    f"{res_c} _r; if (atr_read_file({path}, &_o, &_e)) {{ "
                    f"atr_string_free(&_e); _r = {ok}; }} else {{ "
                    f"atr_string_free(&_o); _r = {err}; }} _r; }})")
        if name == "write_file":
            res_c = self.ctype(rt)
            ok = self.mk_ctor(rt, "Ok", ["(AtrUnit){0}"])
            err = self.mk_ctor(rt, "Err", ["_e"])
            path = self.str_view(e.args[0])
            data = self.str_view(e.args[1])
            return (f"({{ AtrString _e = atr_string_new(); {res_c} _r; "
                    f"if (atr_write_file({path}, {data}, &_e)) {{ "
                    f"atr_string_free(&_e); _r = {ok}; }} else {{ _r = {err}; }} _r; }})")
        if name == "env":
            opt_c = self.ctype(rt)
            some = self.mk_ctor(rt, "Some", ["_o"])
            none = self.mk_ctor(rt, "None", [])
            var = self.str_view(e.args[0])
            return (f"({{ AtrString _o = atr_string_new(); {opt_c} _r; "
                    f"if (atr_env({var}, &_o)) {{ _r = {some}; }} else {{ "
                    f"atr_string_free(&_o); _r = {none}; }} _r; }})")
        if name == "args":
            list_c = self.ctype(rt)
            key = self.mangle(rt)
            return (f"({{ {list_c} _l = atr_list_{key}_new(); "
                    f"for (size_t _i = 0; _i < atr_args_count(); _i++) {{ "
                    f"AtrString _a = atr_string_new(); "
                    f"if (atr_arg_at(_i, &_a)) atr_list_{key}_push(&_l, _a); "
                    f"else atr_string_free(&_a); }} _l; }})")
        if name == "now_ms":
            return "((uint64_t)atr_now_ms())"
        raise CodegenError(f"internal: unknown host builtin {name}")

    # ------------------------------------------------------------------
    # match
    # ------------------------------------------------------------------
    def emit_match(self, e) -> str:
        st = self.concrete(e.scrutinee.ty)
        by_ref = getattr(e, "match_by_ref", False)
        tmp = self.temp("m")
        if by_ref:
            src = self.emit_expr(e.scrutinee, consume=False)
            head = [f"{self.ctype(st)} {tmp} = {src};"]
            access = f"(*({tmp}))"
            match_t = self.concrete(st.inner)
        else:
            src = self.emit_expr(e.scrutinee, consume=True)
            head = [f"{self.ctype(st)} {tmp} = {src};"]
            access = tmp
            match_t = st

        result_t = self.concrete(e.ty)
        is_unit = isinstance(result_t, TCon) and result_t.name == "unit"
        result_c = self.ctype(result_t)
        result_var = None
        if not is_unit:
            result_var = self.temp("r")
            head.append(f"{result_c} {result_var};")

        post_moves = list(self.pending)
        self.pending = []
        body = []
        base_pre = len(self.pre)
        if by_ref:
            pass
        seen_catchall = False
        for i, arm in enumerate(e.arms):
            self.var_scopes.append({})
            self.drop_scopes.append([])
            conds = []
            decls = []
            self.emit_pattern(arm.pattern, access, match_t, conds, decls, not by_ref)
            cond = " && ".join(conds) if conds else "1"
            is_catchall = cond == "1"
            if i == 0:
                body.append("if (" + cond + ") {")
            elif is_catchall and seen_catchall:
                pass
            elif is_catchall:
                body.append("else {")
            else:
                body.append("else if (" + cond + ") {")
            if is_catchall:
                seen_catchall = True
            arm_pre = self.pre[base_pre:]
            del self.pre[base_pre:]
            self.pending = []
            for p in arm_pre:
                body.append("    " + p)
            for d in decls:
                body.append("    " + d)
            val = self.emit_expr(arm.body, consume=True)
            if is_unit:
                body.append(f"    {val};")
            else:
                body.append(f"    {result_var} = {val};")
            for p in self.pending:
                body.append("    " + p)
            for d in self.emit_all_drops(len(self.drop_scopes) - 1):
                body.append("    " + d)
            self.drop_scopes.pop()
            self.var_scopes.pop()
            body.append("}")
            if is_catchall:
                break
        if not seen_catchall:
            body.append('else { atr_panic("non-exhaustive match"); }')
        body.extend(post_moves)
        body.append(f"{result_var};" if not is_unit else "((void)0);")
        return "({ " + " ".join(head + body) + " })"

    def emit_pattern(self, pat, vex, ty, conds, decls, owns):
        ty = self.concrete(ty)
        if isinstance(pat, ast.PatWild):
            if owns and not self.is_copy(ty):
                d = self.ensure_drop(ty)
                if d:
                    decls.append(f"{d}(&({vex}));")
            return
        if isinstance(pat, ast.PatBind):
            cname = self.declare_local(pat.name, ty)
            decls.append(f"{self.ctype(ty)} {cname} = {vex};")
            if not self.is_copy(ty):
                decls.append(f"bool {self.alive_flag(cname)} = true;")
                self.drop_scopes[-1].append((cname, ty))
            return
        if isinstance(pat, ast.PatLit):
            v = pat.value
            if isinstance(v, str):
                b = v.encode("utf-8")
                conds.append(f"atr_str_eq(atr_string_view(&({vex})), "
                             f"atr_str_lit({_c_bytes(b)}, {len(b)}))")
            elif isinstance(v, bool):
                conds.append(f"({vex}) == {'true' if v else 'false'}")
            elif isinstance(v, float):
                conds.append(f"({vex}) == {repr(v)}")
            else:
                conds.append(f"({vex}) == {v}")
            return
        if isinstance(pat, ast.PatVariant):
            info = self.m.enums[pat.ref.enum]
            _, vtys = info.variants[pat.ref.index]
            mp = dict(zip(info.generics, ty.args))
            conds.append(f"({vex}).tag == {pat.ref.index}")
            for i, (sub, vt) in enumerate(zip(pat.args, vtys)):
                pt = self.concrete(subst(vt, mp))
                pex = f"({vex}).u.V_{pat.variant}.v{i}"
                want_owns = owns and not self.is_copy(pt)
                if isinstance(sub, ast.PatVariant) and want_owns:
                    ntmp = f"v_pat_{self.temp_n}"
                    self.temp_n += 1
                    decls.append(f"{self.ctype(pt)} {ntmp} = {pex};")
                    self.emit_pattern(sub, ntmp, pt, conds, decls, True)
                else:
                    self.emit_pattern(sub, pex, pt, conds, decls, want_owns)
            return
        raise CodegenError("internal: unknown pattern")


def generate(m: Module) -> str:
    return Codegen(m).run()
