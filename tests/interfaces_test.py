#!/usr/bin/env python3
"""Check stable module interfaces and the dependency graph."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATRC = os.path.join(ROOT, "atrc")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def run(*args):
    return subprocess.run([sys.executable, ATRC, *args],
                          capture_output=True, text=True)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def snapshot(directory):
    out = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                out[name] = f.read()
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="atrat-ifaces-")
    write(os.path.join(tmp, "sub", "math.atr"), """
pub struct Item:
    value: i64

pub fn add(a: i64, b: i64) -> i64:
    return a + b
""")
    util_path = os.path.join(tmp, "util.atr")
    write(util_path, """
import "sub/math"

pub struct Pair:
    a: i64
    b: i64

pub fn greet(name: str) -> String:
    return "hello, {name}"

fn secret() -> i64:
    return 1

pub fn answer() -> i64:
    return add(40, 2)

pub fn make(v: i64) -> math.Item:
    return math.Item { value: v }
""")
    write(os.path.join(tmp, "main.atr"), """
import "util" as u
import "sub/math" as m

fn main() -> i32:
    print(u.greet("atrat"))
    print(u.answer())
    let p = u.Pair { a: 20, b: 22 }
    print(p.a + p.b)
    print(m.add(1, 2))
    return 0
""")

    out = os.path.join(tmp, "interfaces")
    proc = run("interfaces", os.path.join(tmp, "main.atr"), "-o", out)
    if proc.returncode != 0:
        print("FAIL interfaces: command failed")
        print(proc.stdout)
        print(proc.stderr)
        return 1
    if "wrote 3 module interfaces" not in proc.stdout:
        print("FAIL interfaces: unexpected output")
        print(proc.stdout)
        return 1

    graph = read_json(os.path.join(out, "graph.json"))
    expected = ["sub/math", "util", "main"]
    if graph["order"] != expected:
        print(f"FAIL interfaces: wrong order {graph['order']}")
        return 1
    main_node = graph["nodes"][-1]
    if main_node["dependencies"] != ["sub/math", "util"]:
        print(f"FAIL interfaces: wrong main deps {main_node['dependencies']}")
        return 1
    if not os.path.isfile(os.path.join(out, "sub__math.json")):
        print("FAIL interfaces: missing sub/math interface file")
        return 1

    util = read_json(os.path.join(out, "util.json"))
    main_initial = read_json(os.path.join(out, "main.json"))
    if util["module"] != "util":
        print("FAIL interfaces: wrong util module")
        return 1
    if util["exports"]["functions"].keys() != {"greet", "answer", "make"}:
        print(f"FAIL interfaces: wrong util exports {util['exports']['functions']}")
        return 1
    if util["exports"]["structs"]["Pair"]["fields"][0]["type"] != "i64":
        print("FAIL interfaces: wrong Pair fields")
        return 1
    if util["exports"]["functions"]["make"]["ret"] != "sub/math::Item":
        print("FAIL interfaces: imported type lost its module identity")
        print(util["exports"]["functions"]["make"])
        return 1
    if util["dependencies"] != [{"id": "sub/math",
                                 "interface_hash": read_json(
                                     os.path.join(out, "sub__math.json")
                                 )["interface_hash"]}]:
        print("FAIL interfaces: wrong util dependencies")
        return 1

    first = snapshot(out)
    proc = run("interfaces", os.path.join(tmp, "main.atr"), "-o", out)
    if proc.returncode != 0 or snapshot(out) != first:
        print("FAIL interfaces: output is not deterministic")
        return 1

    # A private body change affects source_hash, not the public interface.
    util_src = open(util_path, "r", encoding="utf-8").read()
    write(util_path, util_src.replace("return 1\n", "return 2\n", 1))
    proc = run("interfaces", os.path.join(tmp, "main.atr"), "-o", out)
    if proc.returncode != 0:
        print("FAIL interfaces: private change build failed")
        print(proc.stderr)
        return 1
    changed = read_json(os.path.join(out, "util.json"))
    if changed["interface_hash"] != util["interface_hash"]:
        print("FAIL interfaces: private change altered interface hash")
        return 1
    if changed["source_hash"] == util["source_hash"]:
        print("FAIL interfaces: private change did not alter source hash")
        return 1
    main_after_private = read_json(os.path.join(out, "main.json"))
    if main_after_private["interface_hash"] != main_initial["interface_hash"]:
        print("FAIL interfaces: private change altered dependent interface")
        return 1

    # A public change must invalidate both the module and its dependents.
    write(util_path, util_src + """
pub fn extra() -> i64:
    return 99
""")
    proc = run("interfaces", os.path.join(tmp, "main.atr"), "-o", out)
    if proc.returncode != 0:
        print("FAIL interfaces: public change build failed")
        print(proc.stderr)
        return 1
    util2 = read_json(os.path.join(out, "util.json"))
    main2 = read_json(os.path.join(out, "main.json"))
    if util2["interface_hash"] == util["interface_hash"]:
        print("FAIL interfaces: public change did not alter util interface")
        return 1
    if main2["interface_hash"] == main_initial["interface_hash"]:
        print("FAIL interfaces: public change did not alter dependent interface")
        return 1
    if main2["dependencies"][1]["interface_hash"] != util2["interface_hash"]:
        print("FAIL interfaces: main did not inherit util interface hash")
        return 1

    proc = run("graph", os.path.join(tmp, "main.atr"))
    if proc.returncode != 0 or "main <- sub/math, util" not in proc.stdout:
        print("FAIL interfaces: graph command output")
        print(proc.stdout)
        print(proc.stderr)
        return 1
    proc = run("graph", os.path.join(tmp, "main.atr"), "--json")
    if proc.returncode != 0 or json.loads(proc.stdout)["order"] != expected:
        print("FAIL interfaces: graph --json output")
        print(proc.stdout)
        print(proc.stderr)
        return 1

    print(f"module interfaces: {len(first)} files, 1 graph, 3 modules")
    print("module interfaces: deterministic and dependency-aware")
    return 0


if __name__ == "__main__":
    sys.exit(main())
