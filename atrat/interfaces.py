"""Stable module interfaces and the module dependency build graph.

This is the first separate-compilation layer: the compiler still builds one C
translation unit, but each module now has a deterministic public interface and
a dependency record that can be cached and compared.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from .check import Module
from .typesys import TCon, TFn, TPtr, TRef, TVar
from .version import __version__

SCHEMA = "atrat-module-interface-v1"
GRAPH_SCHEMA = "atrat-build-graph-v1"


def _json_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _type_name(module: Module, t, ids: dict[str, str], local_source: str) -> str:
    t = module.u.resolve_deep(t)
    if isinstance(t, TCon):
        if t.name == "unit":
            return "()"
        if t.name == "dyn" and len(t.args) == 1:
            return "dyn " + _type_name(module, t.args[0], ids, local_source)
        name = module.display_name(t.name)
        info = (module.structs.get(t.name) or module.enums.get(t.name)
                or module.traits.get(t.name))
        if info is not None and getattr(info, "source", "") not in ("", "<prelude>"):
            if info.source != local_source:
                mid = ids.get(info.source)
                if mid:
                    name = f"{mid}::{name}"
        if t.args:
            args = ", ".join(_type_name(module, a, ids, local_source)
                             for a in t.args)
            return f"{name}<{args}>"
        return name
    if isinstance(t, TRef):
        return (("&mut " if t.mut else "&")
                + _type_name(module, t.inner, ids, local_source))
    if isinstance(t, TPtr):
        return (("*mut " if t.mut else "*")
                + _type_name(module, t.inner, ids, local_source))
    if isinstance(t, TFn):
        params = ", ".join(_type_name(module, p, ids, local_source)
                           for p in t.params)
        return "fn(" + params + ") -> " + _type_name(module, t.ret, ids, local_source)
    if isinstance(t, TVar):
        return t.hint or f"T{t.id}"
    return t.show()


def _named(module: Module, name: str, ids: dict[str, str],
           local_source: str) -> str:
    label = module.display_name(name)
    info = (module.structs.get(name) or module.enums.get(name)
            or module.traits.get(name))
    if info is not None and getattr(info, "source", "") not in ("", "<prelude>"):
        if info.source != local_source:
            mid = ids.get(info.source)
            if mid:
                return f"{mid}::{label}"
    return label


def _fn_sig(module: Module, info, ids: dict[str, str], local_source: str) -> dict:
    bounds = {}
    for g, names in getattr(info.decl, "bounds", {}).items():
        bounds[g] = [_named(module, n, ids, local_source) for n in names]
    return {
        "generics": list(info.generics),
        "bounds": bounds,
        "params": [_type_name(module, p, ids, local_source) for p in info.params],
        "ret": _type_name(module, info.ret, ids, local_source),
        "self": info.self_mode,
        "extern": bool(getattr(info.decl, "is_extern", False)),
    }


def _public_type(module: Module, name: str) -> bool:
    info = module.structs.get(name) or module.enums.get(name)
    return bool(info is not None and getattr(info, "public", False))


def _interface(module: Module, unit, dependencies: list[dict],
               ids: dict[str, str]) -> dict:
    source = unit.source
    functions = {}
    structs = {}
    enums = {}
    traits = {}
    impls = []

    for name, info in module.fns.items():
        if getattr(info, "source", "") != source or not getattr(info, "public", False):
            continue
        functions[module.display_name(name)] = _fn_sig(module, info, ids, source)

    for name, info in module.structs.items():
        if getattr(info, "source", "") != source or not getattr(info, "public", False):
            continue
        structs[module.display_name(name)] = {
            "generics": list(info.generics),
            "fields": [{"name": fname, "type": _type_name(module, fty, ids, source)}
                       for fname, fty in info.fields],
            "repr_c": bool(info.repr_c),
        }

    for name, info in module.enums.items():
        if getattr(info, "source", "") != source or not getattr(info, "public", False):
            continue
        enums[module.display_name(name)] = {
            "generics": list(info.generics),
            "variants": [
                {"name": vname,
                 "fields": [_type_name(module, fty, ids, source) for fty in tys]}
                for vname, tys in info.variants
            ],
        }

    for name, info in module.traits.items():
        if getattr(info, "source", "") != source or not getattr(info, "public", False):
            continue
        traits[module.display_name(name)] = {
            "generics": list(info.generics),
            "supertraits": [_named(module, s, ids, source) for s in info.supertraits],
            "methods": {mname: _fn_sig(module, sig, ids, source)
                        for mname, sig in info.methods.items()},
        }

    trait_of = {}
    for tname, tri in module.traits.items():
        for owner, methods in tri.impls.items():
            for mname in methods:
                trait_of[(owner, mname)] = tname
    for (owner, mname), info in module.methods.items():
        if getattr(info, "source", "") != source:
            continue
        if not _public_type(module, owner):
            continue
        tri = trait_of.get((owner, mname))
        if tri is not None and not getattr(module.traits.get(tri), "public", False):
            tri = None
        impls.append({
            "type": _named(module, owner, ids, source),
            "trait": _named(module, tri, ids, source) if tri else None,
            "name": mname,
            "signature": _fn_sig(module, info, ids, source),
        })
    impls.sort(key=lambda x: (x["type"], x["trait"] or "", x["name"]))

    exports = {
        "functions": functions,
        "structs": structs,
        "enums": enums,
        "traits": traits,
        "impls": impls,
    }
    return {
        "schema": SCHEMA,
        "compiler": __version__,
        "module": unit.module_id,
        "source_hash": unit.source_hash,
        "dependencies": dependencies,
        "exports": exports,
    }


def _seal(interface: dict) -> dict:
    payload = {k: v for k, v in interface.items()
               if k not in ("source_hash", "interface_hash")}
    interface["interface_hash"] = _json_hash(payload)
    return interface


def build_graph(module: Module) -> tuple[dict, dict]:
    """Return (graph, interfaces_by_module_id) in dependency-first order."""
    units = [u for u in (module.program.units if module.program else [])
             if not u.is_prelude]
    ids = {u.source: u.module_id for u in units}
    node_hashes: dict[str, str] = {}
    nodes = []
    interfaces = {}
    for unit in units:
        deps = []
        seen = set()
        for node, target in unit.imports:
            dep_id = ids.get(target)
            if dep_id is None or dep_id in seen:
                continue
            seen.add(dep_id)
            deps.append({"id": dep_id,
                         "interface_hash": node_hashes.get(dep_id, "")})
        deps.sort(key=lambda d: d["id"])
        interface = _seal(_interface(module, unit, deps, ids))
        interfaces[unit.module_id] = interface
        node_hashes[unit.module_id] = interface["interface_hash"]
        nodes.append({
            "module": unit.module_id,
            "source_hash": unit.source_hash,
            "interface_hash": interface["interface_hash"],
            "dependencies": [d["id"] for d in deps],
        })
    graph = {
        "schema": GRAPH_SCHEMA,
        "compiler": __version__,
        "order": [n["module"] for n in nodes],
        "nodes": nodes,
    }
    return graph, interfaces


def _file_name(module_id: str) -> str:
    raw = module_id.replace("\\", "/").replace("/", "__").replace("..", "__")
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", raw)
    if safe != raw:
        safe += "_" + hashlib.sha1(module_id.encode("utf-8")).hexdigest()[:8]
    return safe + ".json"


def write_interfaces(module: Module, outdir: str) -> dict:
    graph, interfaces = build_graph(module)
    os.makedirs(outdir, exist_ok=True)
    for module_id, interface in interfaces.items():
        path = os.path.join(outdir, _file_name(module_id))
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(interface, f, sort_keys=True, indent=2, ensure_ascii=True)
            f.write("\n")
    path = os.path.join(outdir, "graph.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(graph, f, sort_keys=True, indent=2, ensure_ascii=True)
        f.write("\n")
    return graph
