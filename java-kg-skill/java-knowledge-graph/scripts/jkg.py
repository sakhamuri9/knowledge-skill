#!/usr/bin/env python3
"""
jkg.py — Java Knowledge Graph
A zero-dependency code-graph indexer & query engine for Java codebases.

Builds a GitNexus-style knowledge graph (classes, interfaces, methods, calls,
inheritance, execution flows, functional clusters) and stores it as plain JSON
in <repo>/.jkg/ — no database required.

Commands:
  analyze   Build / incrementally refresh the graph
  query     Search the graph by concept or name
  context   Full 360° view of one symbol (callers, callees, flows, cluster)
  impact    Blast-radius analysis with risk level (run BEFORE editing!)
  callers   Direct callers of a symbol
  callees   Direct callees of a symbol
  flows     List execution flows (entry point -> terminal)
  flow      Print one execution flow step by step
  clusters  List functional areas
  hierarchy Type hierarchy (supertypes & subtypes)
  cycles    Package dependency cycles
  diff      What changed since last analyze (run BEFORE committing!)
  stats     Graph statistics
  init      Analyze + install agent rules into CLAUDE.md / AGENTS.md

Python 3.8+, standard library only.
"""

import sys
import os
import re
import json
import hashlib
import argparse
import time
from collections import defaultdict, Counter

SCHEMA_VERSION = 2  # v2: field annotations + Lombok synthesis
JKG_DIR = ".jkg"
GRAPH_FILE = "graph.json"
CACHE_FILE = "parse-cache.json"

SKIP_DIRS = {".git", ".jkg", "target", "build", "out", "bin", "node_modules",
             ".gradle", ".idea", ".settings", "generated", "generated-sources"}

JAVA_KEYWORDS = {
    "if", "else", "for", "while", "do", "switch", "case", "default", "try",
    "catch", "finally", "return", "throw", "throws", "new", "break", "continue",
    "assert", "synchronized", "yield", "instanceof", "this", "super", "class",
    "interface", "enum", "record", "extends", "implements", "package", "import",
    "void", "int", "long", "short", "byte", "char", "boolean", "float", "double",
    "public", "private", "protected", "static", "final", "abstract", "native",
    "strictfp", "transient", "volatile", "sealed", "permits", "var", "null",
    "true", "false",
}

MODIFIER_WORDS = {"public", "private", "protected", "static", "final", "abstract",
                  "synchronized", "native", "strictfp", "default", "sealed", "non-sealed"}

# Entry-point annotations (Spring / Jakarta / messaging / lifecycle)
ENTRY_METHOD_ANNOS = {
    "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
    "RequestMapping", "Scheduled", "KafkaListener", "RabbitListener",
    "JmsListener", "EventListener", "PostConstruct", "SqsListener",
    "MessageMapping", "SubscribeMapping", "GrpcMethod", "Path", "GET", "POST",
    "PUT", "DELETE",
}
ENTRY_CLASS_ANNOS = {"RestController", "Controller", "RestControllerAdvice",
                     "ControllerAdvice", "WebServlet", "SpringBootApplication"}
TEST_ANNOS = {"Test", "ParameterizedTest", "RepeatedTest", "BeforeEach",
              "AfterEach", "BeforeAll", "AfterAll"}

# Process detection config (mirrors GitNexus defaults)
MAX_TRACE_DEPTH = 10
MAX_BRANCHING = 4
MAX_PROCESSES = 75
MIN_STEPS = 3

# ---------------------------------------------------------------------------
# Source cleaning
# ---------------------------------------------------------------------------

def strip_noise(src):
    """Blank out comments and string/char literal contents, preserving
    offsets and newlines so positions/line numbers stay valid."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            if j == -1:
                j = n
            for k in range(i, j):
                out[k] = ' '
            i = j
        elif c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
        elif c == '"':
            if src.startswith('"""', i):  # text block
                j = src.find('"""', i + 3)
                j = n if j == -1 else j + 3
            else:
                j = i + 1
                while j < n and src[j] != '"' and src[j] != '\n':
                    if src[j] == '\\':
                        j += 1
                    j += 1
                j = min(j + 1, n)
            for k in range(i + 1, j - 1):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
        elif c == "'":
            j = i + 1
            while j < n and src[j] != "'" and src[j] != '\n':
                if src[j] == '\\':
                    j += 1
                j += 1
            j = min(j + 1, n)
            for k in range(i + 1, j - 1):
                out[k] = ' '
            i = j
        else:
            i += 1
    return ''.join(out)


def strip_generics(s):
    out, d = [], 0
    for c in s:
        if c == '<':
            d += 1
        elif c == '>':
            d = max(0, d - 1)
        elif d == 0:
            out.append(c)
    return ''.join(out)


def match_brace(text, open_idx):
    d = 0
    for i in range(open_idx, len(text)):
        if text[i] == '{':
            d += 1
        elif text[i] == '}':
            d -= 1
            if d == 0:
                return i
    return len(text) - 1


def line_of(text, pos):
    return text.count('\n', 0, pos) + 1

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

TYPE_RE = re.compile(r'(?<![.\w$])(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)')
PKG_RE = re.compile(r'^\s*package\s+([\w.]+)\s*;', re.M)
IMPORT_RE = re.compile(r'^\s*import\s+(static\s+)?([\w.]+)(\.\*)?\s*;', re.M)
ANNO_RE = re.compile(r'@([A-Za-z_$][\w$]*)')
MEMBER_RE = re.compile(r'([\w$>\]?])\s+([A-Za-z_$][\w$]*)\s*\(')
FIELD_RE = re.compile(r'([A-Za-z_$][\w$]*(?:\s*<[^<>;={]*>)?(?:\s*\[\s*\])*)\s+'
                      r'([a-z_$][\w$]*)\s*[=;]')
CALL_RE = re.compile(
    r'(?:\bnew\s+([A-Z][\w$.]*)\s*(?:<[^<>(]*>)?\s*\('       # 1: new Type(
    r'|([\w$]+)\s*\.\s*([A-Za-z_$][\w$]*)\s*\('               # 2.3: recv.method(
    r'|\)\s*\.\s*([A-Za-z_$][\w$]*)\s*\('                     # 4: ).chained(
    r'|(?<![\w$.])([A-Za-z_$][\w$]*)\s*\()'                   # 5: bare(
)
LOCAL_RE = re.compile(r'(?<![\w$.])([A-Z][\w$]*)(?:\s*<[^<>(]*>)?(?:\s*\[\s*\])*'
                      r'\s+([a-z_$][\w$]*)\s*[=;,)]')


def split_top_commas(s):
    parts, d, cur = [], 0, []
    for c in s:
        if c in '<([':
            d += 1
        elif c in '>)]':
            d -= 1
        if c == ',' and d == 0:
            parts.append(''.join(cur))
            cur = []
        else:
            cur.append(c)
    if cur:
        parts.append(''.join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_params(param_text):
    """Return [{'type': simpleType, 'name': varName}] from a parameter list."""
    params = []
    for p in split_top_commas(param_text):
        p = re.sub(r'@[\w$.]+(\([^)]*\))?', '', p)  # drop param annotations
        p = p.replace('final ', ' ').replace('...', '[]')
        p = strip_generics(p).strip()
        toks = p.split()
        if len(toks) >= 2:
            ptype = toks[-2].split('.')[-1].replace('[]', '').strip()
            params.append({"type": ptype, "name": toks[-1]})
        elif len(toks) == 1 and toks[0]:
            params.append({"type": toks[0].replace('[]', ''), "name": "_"})
    return params


def annotations_before(text, region_start, pos):
    """Annotations between the previous member boundary and pos."""
    lo = max(region_start, pos - 400)
    seg = text[lo:pos]
    cut = max(seg.rfind(';'), seg.rfind('}'))
    if cut != -1:
        seg = seg[cut + 1:]
    return ANNO_RE.findall(seg)


def modifiers_before(text, pos):
    seg = text[max(0, pos - 200):pos]
    cut = max(seg.rfind(';'), seg.rfind('}'), seg.rfind('{'))
    if cut != -1:
        seg = seg[cut + 1:]
    seg = re.sub(r'@[\w$.]+(\([^)]*\))?', ' ', seg)
    return set(w for w in seg.split() if w in MODIFIER_WORDS)


def extract_calls(body, line_base_text, body_start):
    calls = []
    for m in CALL_RE.finditer(body):
        pos = body_start + m.start()
        ln = line_of(line_base_text, pos)
        if m.group(1):  # new Type(
            calls.append({"k": "new", "name": m.group(1).split('.')[-1], "line": ln})
        elif m.group(2):  # recv.method(
            recv, name = m.group(2), m.group(3)
            if name in JAVA_KEYWORDS:
                continue
            calls.append({"k": "recv", "recv": recv, "name": name, "line": ln})
        elif m.group(4):  # ).chained(
            name = m.group(4)
            if name not in JAVA_KEYWORDS:
                calls.append({"k": "chain", "name": name, "line": ln})
        else:  # bare(
            name = m.group(5)
            if name in JAVA_KEYWORDS:
                continue
            calls.append({"k": "bare", "name": name, "line": ln})
    return calls


def extract_locals(body):
    locs = {}
    for m in LOCAL_RE.finditer(body):
        t, v = m.group(1), m.group(2)
        if t not in JAVA_KEYWORDS and v not in JAVA_KEYWORDS:
            locs[v] = t
    # var x = new Foo(...)
    for m in re.finditer(r'\bvar\s+([a-z_$][\w$]*)\s*=\s*new\s+([A-Z][\w$]*)', body):
        locs[m.group(1)] = m.group(2)
    return locs


def parse_java(src, rel_path):
    """Parse one Java source file into a declaration record (JSON-friendly)."""
    text = strip_noise(src)
    pkg_m = PKG_RE.search(text)
    package = pkg_m.group(1) if pkg_m else ""

    imports, wildcards, static_imports = {}, [], []
    for m in IMPORT_RE.finditer(text):
        is_static, path, star = m.group(1), m.group(2), m.group(3)
        if star:
            (static_imports if is_static else wildcards).append(path)
        elif is_static:
            static_imports.append(path)
        else:
            imports[path.split('.')[-1]] = path

    # --- locate type declarations -----------------------------------------
    raw_types = []
    for m in TYPE_RE.finditer(text):
        kind, name = m.group(1), m.group(2)
        body_open = text.find('{', m.end())
        if body_open == -1:
            continue
        header = text[m.end():body_open]
        # `record X(...)` header includes the component list
        rec_params = []
        if kind == 'record':
            pm = re.match(r'\s*(?:<[^<>]*>)?\s*\(([^)]*)\)', header)
            if pm:
                rec_params = parse_params(pm.group(1))
                header = header[pm.end():]
        header = strip_generics(header)
        ext, impl = [], []
        em = re.search(r'\bextends\s+([\w$.,\s]+?)(?=\bimplements\b|\bpermits\b|$)', header)
        if em:
            ext = split_top_commas(em.group(1))
        im = re.search(r'\bimplements\s+([\w$.,\s]+?)(?=\bpermits\b|$)', header)
        if im:
            impl = split_top_commas(im.group(1))
        body_close = match_brace(text, body_open)
        raw_types.append({
            "kind": kind, "name": name, "start": m.start(), "open": body_open,
            "close": body_close, "extends": ext, "implements": impl,
            "annotations": annotations_before(text, 0, m.start()),
            "line": line_of(text, m.start()), "rec_params": rec_params,
        })

    # nesting: parent = smallest strictly-enclosing type body
    for t in raw_types:
        parent = None
        for o in raw_types:
            if o is t:
                continue
            if o["open"] < t["start"] and t["close"] <= o["close"]:
                if parent is None or o["close"] - o["open"] < parent["close"] - parent["open"]:
                    parent = o
        t["parent"] = parent

    def qname_of(t):
        parts = [t["name"]]
        p = t["parent"]
        while p:
            parts.append(p["name"])
            p = p["parent"]
        parts.reverse()
        return (package + "." if package else "") + ".".join(parts)

    # depth array for member-level detection
    depths = [0] * (len(text) + 1)
    d = 0
    for i, c in enumerate(text):
        depths[i] = d
        if c == '{':
            d += 1
        elif c == '}':
            d = max(0, d - 1)
    depths[len(text)] = d

    nested_ranges = [(t["open"], t["close"]) for t in raw_types]

    def in_nested_type(pos, own):
        for (o, c) in nested_ranges:
            if (o, c) != (own["open"], own["close"]) and o < pos <= c \
                    and own["open"] < o:  # only ranges nested inside own
                return True
        return False

    types = []
    for t in raw_types:
        body_lo, body_hi = t["open"] + 1, t["close"]
        member_depth = depths[t["open"]] + 1
        methods, fields = [], []
        seen_spans = []

        # methods
        for m in MEMBER_RE.finditer(text, body_lo, body_hi):
            name_pos = m.start(2)
            if depths[name_pos] != member_depth or in_nested_type(name_pos, t):
                continue
            name = m.group(2)
            if name in JAVA_KEYWORDS or name == t["name"]:
                continue  # constructors are handled by the ctor pass below
            prev = text[max(0, m.start()):m.start(2)].split()
            prev_word_m = re.search(r'([\w$]+)\s*$', text[max(0, name_pos - 50):name_pos])
            prev_word = prev_word_m.group(1) if prev_word_m else ""
            if prev_word in {"return", "new", "throw", "else", "case", "yield",
                             "break", "continue", "assert", ".", "instanceof"}:
                continue
            # match the parameter list
            paren_open = text.find('(', m.end() - 1)
            pd, paren_close = 0, -1
            for i in range(paren_open, min(body_hi, paren_open + 4000)):
                if text[i] == '(':
                    pd += 1
                elif text[i] == ')':
                    pd -= 1
                    if pd == 0:
                        paren_close = i
                        break
            if paren_close == -1:
                continue
            after = text[paren_close + 1:paren_close + 200]
            after_ws = after.lstrip()
            if after_ws.startswith('throws'):
                brace = text.find('{', paren_close)
                semi = text.find(';', paren_close)
                if brace != -1 and (semi == -1 or brace < semi):
                    after_ws = '{'
                    body_open_pos = brace
                else:
                    after_ws = ';'
                    body_open_pos = -1
            elif after_ws.startswith('{'):
                body_open_pos = text.find('{', paren_close)
            elif after_ws.startswith(';'):
                body_open_pos = -1
            else:
                continue  # not a method (e.g. a call statement)

            mods = modifiers_before(text, m.start(2) if not m.group(1) else m.start())
            # the return-type token must exist before the name (not "if (...")
            ret_seg = text[max(0, name_pos - 60):name_pos].strip()
            if not ret_seg:
                continue
            params = parse_params(text[paren_open + 1:paren_close])
            annos = annotations_before(text, body_lo, m.start(2))
            body_span = None
            calls, locs = [], {}
            if after_ws.startswith('{') and body_open_pos != -1:
                bclose = match_brace(text, body_open_pos)
                body_span = (body_open_pos, bclose)
                body = text[body_open_pos:bclose + 1]
                calls = extract_calls(body, text, body_open_pos)
                locs = extract_locals(body)
            if any(s[0] <= name_pos < s[1] for s in seen_spans):
                continue
            if body_span:
                seen_spans.append(body_span)
            methods.append({
                "name": name, "arity": len(params), "params": params,
                "line": line_of(text, name_pos), "annotations": annos,
                "static": "static" in mods, "abstract": after_ws.startswith(';'),
                "public": "public" in mods or t["kind"] == "interface",
                "ctor": False, "calls": calls, "locals": locs,
            })

        # constructors: Name(
        ctor_re = re.compile(r'(?<![\w$.])(' + re.escape(t["name"]) + r')\s*\(')
        for m in ctor_re.finditer(text, body_lo, body_hi):
            name_pos = m.start(1)
            if depths[name_pos] != member_depth or in_nested_type(name_pos, t):
                continue
            pw = re.search(r'([\w$]+)\s*$', text[max(0, name_pos - 50):name_pos])
            if pw and pw.group(1) not in MODIFIER_WORDS and pw.group(1) != "":
                continue  # `new Name(` or `Foo Name(` — not a ctor decl
            paren_open = text.find('(', name_pos)
            pd, paren_close = 0, -1
            for i in range(paren_open, min(body_hi, paren_open + 4000)):
                if text[i] == '(':
                    pd += 1
                elif text[i] == ')':
                    pd -= 1
                    if pd == 0:
                        paren_close = i
                        break
            if paren_close == -1:
                continue
            after_ws = text[paren_close + 1:paren_close + 100].lstrip()
            if not (after_ws.startswith('{') or after_ws.startswith('throws')):
                continue
            body_open_pos = text.find('{', paren_close)
            if body_open_pos == -1:
                continue
            bclose = match_brace(text, body_open_pos)
            if any(s[0] <= name_pos < s[1] for s in seen_spans):
                continue
            seen_spans.append((body_open_pos, bclose))
            params = parse_params(text[paren_open + 1:paren_close])
            body = text[body_open_pos:bclose + 1]
            mods = modifiers_before(text, name_pos)
            methods.append({
                "name": "<init>", "arity": len(params), "params": params,
                "line": line_of(text, name_pos),
                "annotations": annotations_before(text, body_lo, name_pos),
                "static": False, "abstract": False, "public": "public" in mods,
                "ctor": True, "calls": extract_calls(body, text, body_open_pos),
                "locals": extract_locals(body),
            })

        # fields (member depth, outside method bodies)
        for m in FIELD_RE.finditer(text, body_lo, body_hi):
            pos = m.start(2)
            if depths[pos] != member_depth or in_nested_type(pos, t):
                continue
            if any(s[0] <= pos < s[1] for s in seen_spans):
                continue
            ftype = strip_generics(m.group(1)).replace('[]', '').strip()
            fname = m.group(2)
            if ftype in JAVA_KEYWORDS and ftype not in {"int", "long", "boolean",
                                                        "double", "float", "char",
                                                        "byte", "short"}:
                continue
            if fname in JAVA_KEYWORDS:
                continue
            fields.append({"name": fname, "type": ftype.split('.')[-1],
                           "line": line_of(text, pos),
                           "annotations": annotations_before(text, body_lo, m.start())})

        for rp in t["rec_params"]:
            fields.append({"name": rp["name"], "type": rp["type"], "line": t["line"]})

        types.append({
            "name": t["name"], "qname": qname_of(t), "kind": t["kind"],
            "line": t["line"], "extends": t["extends"], "implements": t["implements"],
            "annotations": t["annotations"], "methods": methods, "fields": fields,
        })

    return {"package": package, "imports": imports, "wildcards": wildcards,
            "types": types}

# ---------------------------------------------------------------------------
# Lombok synthesis — materialize the methods Lombok generates at compile time
# so calls like order.getId() resolve on @Data/@Getter/@Builder classes.
# (GitNexus does not do this; tree-sitter only sees declared methods.)
# ---------------------------------------------------------------------------

LOMBOK_GETTER = {"Getter", "Data", "Value"}
LOMBOK_SETTER = {"Setter", "Data"}
LOMBOK_ALL_ARGS = {"AllArgsConstructor", "Value", "Builder"}


def synthesize_lombok(td):
    """Append synthetic method records for Lombok-generated members."""
    td["methods"] = [m for m in td["methods"] if not m.get("synthetic")]
    annos = set(td["annotations"])
    existing = {(m["name"], m["arity"]) for m in td["methods"]}
    synth = []

    def add(name, params, static=False, ctor=False):
        if (name, len(params)) in existing:
            return
        existing.add((name, len(params)))
        synth.append({"name": name, "arity": len(params), "params": params,
                      "line": td["line"], "annotations": [],
                      "static": static, "abstract": False, "public": True,
                      "ctor": ctor, "calls": [], "locals": {},
                      "synthetic": "lombok"})

    for f in td["fields"]:
        fannos = set(f.get("annotations", []))
        cap = f["name"][0].upper() + f["name"][1:]
        if annos & LOMBOK_GETTER or "Getter" in fannos:
            gname = ("is" + cap) if f["type"] == "boolean" else ("get" + cap)
            add(gname, [])
        if annos & LOMBOK_SETTER or "Setter" in fannos:
            add("set" + cap, [{"type": f["type"], "name": f["name"]}])
        if "Builder" in annos:
            # fluent setter on the builder is resolved via the chain
            # fallback; expose it on the type itself as an approximation
            add(f["name"], [{"type": f["type"], "name": f["name"]}])
    if "Builder" in annos:
        add("builder", [], static=True)
        add("build", [])
    if annos & LOMBOK_ALL_ARGS:
        add("<init>", [{"type": f["type"], "name": f["name"]}
                       for f in td["fields"]], ctor=True)
    if "NoArgsConstructor" in annos:
        add("<init>", [], ctor=True)
    if "RequiredArgsConstructor" in annos or "Data" in annos:
        add("<init>", [], ctor=True)  # arity approximated
    td["methods"].extend(synth)
    return bool(synth)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def is_test_file(path):
    p = path.replace('\\', '/')
    return ('/test/' in p or '/tests/' in p or p.endswith('Test.java')
            or p.endswith('Tests.java') or p.endswith('IT.java'))


def camel_tokens(name):
    return [t.lower() for t in re.findall(r'[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])', name) if t]


class GraphBuilder:
    def __init__(self, root, cache):
        self.root = root
        self.cache = cache          # {rel_path: {"hash":..., "parsed":...}}
        self.nodes = {}             # id -> node dict
        self.edges = []             # {src,dst,type,conf,reason}
        self.type_by_qname = {}
        self.simple_index = defaultdict(list)   # simpleName -> [qname]
        self.method_name_index = defaultdict(list)  # name -> [method ids]

    # -- node helpers -------------------------------------------------------
    def add_type_node(self, file, td):
        nid = td["qname"]
        kind = {"class": "Class", "interface": "Interface",
                "enum": "Enum", "record": "Record"}[td["kind"]]
        self.nodes[nid] = {
            "id": nid, "kind": kind, "name": td["name"], "qname": td["qname"],
            "file": file, "line": td["line"], "pkg": td["qname"].rsplit('.', 1)[0]
            if '.' in td["qname"] else "",
            "annotations": td["annotations"],
        }
        self.type_by_qname[td["qname"]] = (file, td)
        self.simple_index[td["name"]].append(td["qname"])

    def method_id(self, qname, m):
        return "%s#%s/%d" % (qname, m["name"], m["arity"])

    def add_method_node(self, file, td, m):
        mid = self.method_id(td["qname"], m)
        disp = td["name"] + "." + (td["name"] if m["ctor"] else m["name"])
        self.nodes[mid] = {
            "id": mid, "kind": "Constructor" if m["ctor"] else "Method",
            "name": m["name"] if not m["ctor"] else td["name"],
            "display": disp, "qname": mid, "file": file, "line": m["line"],
            "owner": td["qname"], "arity": m["arity"],
            "annotations": m["annotations"], "public": m["public"],
            "static": m["static"], "abstract": m["abstract"],
        }
        if m.get("synthetic"):
            self.nodes[mid]["synthetic"] = m["synthetic"]
        self.method_name_index[m["name"]].append(mid)
        return mid

    def edge(self, src, dst, etype, conf=1.0, reason=""):
        self.edges.append({"src": src, "dst": dst, "type": etype,
                           "conf": round(conf, 2), "reason": reason})

    # -- type resolution ------------------------------------------------------
    def resolve_type(self, name, file_rec, owner_qname):
        if not name or name in JAVA_KEYWORDS:
            return None
        name = strip_generics(name).strip().replace('[]', '')
        if not name:
            return None
        pkg = file_rec["package"]
        if '.' in name:
            if name in self.type_by_qname:
                return name
            cand = (pkg + "." + name) if pkg else name
            if cand in self.type_by_qname:
                return cand
            first = name.split('.')[0]
            if first in file_rec["imports"]:
                cand = file_rec["imports"][first] + name[len(first):]
                if cand in self.type_by_qname:
                    return cand
            last = name.split('.')[-1]
            hits = self.simple_index.get(last, [])
            return hits[0] if len(hits) == 1 else None
        # nested in owner chain
        oq = owner_qname
        while oq:
            cand = oq + "." + name
            if cand in self.type_by_qname:
                return cand
            oq = oq.rsplit('.', 1)[0] if '.' in oq else ""
        if name in file_rec["imports"]:
            imp = file_rec["imports"][name]
            if imp in self.type_by_qname:
                return imp
            return None  # imported from an external library
        cand = (pkg + "." + name) if pkg else name
        if cand in self.type_by_qname:
            return cand
        for w in file_rec["wildcards"]:
            cand = w + "." + name
            if cand in self.type_by_qname:
                return cand
        hits = self.simple_index.get(name, [])
        return hits[0] if len(hits) == 1 else None

    def supertypes(self, qname):
        """Internal supertype qnames (extends + implements), one level."""
        out = []
        rec = self.type_by_qname.get(qname)
        if not rec:
            return out
        file, td = rec
        frec = self.cache[file]["parsed"]
        for s in td["extends"] + td["implements"]:
            r = self.resolve_type(s, frec, qname)
            if r:
                out.append(r)
        return out

    def find_method(self, qname, name, arity, _seen=None):
        """Find method `name` on type or its internal supertype chain.
        Returns (method_id, depth) or None."""
        if _seen is None:
            _seen = set()
        if qname in _seen or qname not in self.type_by_qname:
            return None
        _seen.add(qname)
        file, td = self.type_by_qname[qname]
        exact, loose = None, None
        for m in td["methods"]:
            if m["name"] == name:
                if m["arity"] == arity:
                    exact = self.method_id(qname, m)
                    break
                loose = self.method_id(qname, m)
        if exact:
            return (exact, 0)
        if loose:
            return (loose, 0)
        for sup in self.supertypes(qname):
            r = self.find_method(sup, name, arity, _seen)
            if r:
                return (r[0], r[1] + 1)
        return None

    # -- main build -----------------------------------------------------------
    def build(self):
        # pass 0: materialize Lombok-generated members (idempotent)
        self.lombok_types = 0
        for file in sorted(self.cache):
            for td in self.cache[file]["parsed"]["types"]:
                if synthesize_lombok(td):
                    self.lombok_types += 1
        # pass 1: nodes
        for file in sorted(self.cache):
            parsed = self.cache[file]["parsed"]
            for td in parsed["types"]:
                self.add_type_node(file, td)
        for file in sorted(self.cache):
            parsed = self.cache[file]["parsed"]
            for td in parsed["types"]:
                for m in td["methods"]:
                    mid = self.add_method_node(file, td, m)
                    self.edge(td["qname"], mid, "HAS_METHOD")
                for f in td["fields"]:
                    fid = td["qname"] + "." + f["name"]
                    self.nodes[fid] = {"id": fid, "kind": "Field",
                                       "name": f["name"], "qname": fid,
                                       "file": file, "line": f["line"],
                                       "owner": td["qname"], "ftype": f["type"]}
                    self.edge(td["qname"], fid, "HAS_FIELD")

        # subtype map (for dynamic dispatch)
        self.subtypes = defaultdict(list)

        # pass 2: inheritance edges
        for file in sorted(self.cache):
            parsed = self.cache[file]["parsed"]
            for td in parsed["types"]:
                for s in td["extends"]:
                    r = self.resolve_type(s, parsed, td["qname"])
                    if r:
                        self.edge(td["qname"], r, "EXTENDS", 1.0)
                        self.subtypes[r].append(td["qname"])
                for s in td["implements"]:
                    r = self.resolve_type(s, parsed, td["qname"])
                    if r:
                        self.edge(td["qname"], r, "IMPLEMENTS", 1.0)
                        self.subtypes[r].append(td["qname"])

        # pass 3: OVERRIDES edges
        for file in sorted(self.cache):
            parsed = self.cache[file]["parsed"]
            for td in parsed["types"]:
                for m in td["methods"]:
                    if m["ctor"]:
                        continue
                    for sup in self.supertypes(td["qname"]):
                        r = self.find_method(sup, m["name"], m["arity"])
                        if r:
                            self.edge(self.method_id(td["qname"], m), r[0],
                                      "OVERRIDES", 0.85)
                            break

        # pass 4: CALLS edges
        for file in sorted(self.cache):
            parsed = self.cache[file]["parsed"]
            for td in parsed["types"]:
                field_types = {f["name"]: f["type"] for f in td["fields"]}
                for m in td["methods"]:
                    src = self.method_id(td["qname"], m)
                    env = dict(field_types)
                    env.update({p["name"]: p["type"] for p in m["params"]})
                    env.update(m["locals"])
                    self._resolve_calls(src, td, m, env, parsed)

        return self

    def _link_method(self, src, owner_q, name, conf, reason, dispatch=True):
        r = self.find_method(owner_q, name, -1)  # arity unknown: loose match
        linked = False
        if r:
            mid, depth = r
            self.edge(src, mid, "CALLS", conf if depth == 0 else conf * 0.95,
                      reason)
            linked = True
            # dynamic dispatch: an interface/abstract method may execute any
            # override in a subtype — link those too (GitNexus: METHOD_IMPLEMENTS)
            if dispatch:
                owner_of_found = mid.split('#')[0]
                impls = self.subtypes.get(owner_of_found, [])
                if impls and len(impls) <= 6:
                    for st in sorted(impls):
                        ri = self.find_method(st, name, -1, _seen={owner_of_found})
                        if ri and ri[0] != mid:
                            self.edge(src, ri[0], "CALLS", 0.6,
                                      "dynamic-dispatch via " +
                                      owner_of_found.split('.')[-1])
        return linked

    def _resolve_calls(self, src, td, m, env, file_rec):
        own_q = td["qname"]
        for c in m["calls"]:
            k = c["k"]
            if k == "new":
                tq = self.resolve_type(c["name"], file_rec, own_q)
                if tq:
                    r = self.find_method(tq, "<init>", -1)
                    if r:
                        self.edge(src, r[0], "CALLS", 0.95, "instantiation")
                    else:
                        self.edge(src, tq, "INSTANTIATES", 0.95)
            elif k == "bare":
                if c["name"] == td["name"]:
                    continue
                self._link_method(src, own_q, c["name"], 0.9, "same-class",
                                  dispatch=False)
            elif k == "recv":
                recv, name = c["recv"], c["name"]
                if recv in ("this",):
                    self._link_method(src, own_q, name, 0.9, "this",
                                      dispatch=False)
                elif recv == "super":
                    for sup in self.supertypes(own_q):
                        if self._link_method(src, sup, name, 0.9, "super",
                                             dispatch=False):
                            break
                elif recv in env:
                    tq = self.resolve_type(env[recv], file_rec, own_q)
                    if tq:
                        self._link_method(src, tq, name, 0.8,
                                          "var:" + recv)
                    # else: receiver type is external (JDK/library) —
                    # the call leaves the repo, don't guess by name
                elif recv[0:1].isupper():
                    tq = self.resolve_type(recv, file_rec, own_q)
                    if tq:
                        self._link_method(src, tq, name, 0.9, "static")
                    elif recv in file_rec["imports"] or any(
                            recv == w.split('.')[-1]
                            for w in file_rec["wildcards"]):
                        pass  # imported external type — call leaves the repo
                    else:
                        self._fallback(src, name)
                else:
                    self._fallback(src, name)
            elif k == "chain":
                self._fallback(src, name=c["name"])

    def _fallback(self, src, name):
        """Receiver type unknown: link by globally-unique method name."""
        cands = self.method_name_index.get(name, [])
        owners = sorted({c.split('#')[0] for c in cands})
        if len(owners) == 1 and cands:
            self.edge(src, cands[0], "CALLS", 0.5, "unique-name")
        elif 1 < len(owners) <= 3:
            done = set()
            for c in sorted(cands):
                o = c.split('#')[0]
                if o not in done:
                    done.add(o)
                    self.edge(src, c, "CALLS", 0.35, "name-candidate")

# ---------------------------------------------------------------------------
# Clusters (functional areas) — deterministic label propagation
# ---------------------------------------------------------------------------

CLUSTER_STOP = {"impl", "abstract", "base", "default", "java", "util", "common"}


def detect_clusters(nodes, edges):
    type_ids = sorted(n for n, v in nodes.items()
                      if v["kind"] in ("Class", "Interface", "Enum", "Record"))
    owner = {n: v.get("owner") for n, v in nodes.items()}
    weight = defaultdict(int)
    for e in edges:
        if e["type"] not in ("CALLS", "EXTENDS", "IMPLEMENTS", "OVERRIDES"):
            continue
        a = owner.get(e["src"]) or e["src"]
        b = owner.get(e["dst"]) or e["dst"]
        if a in nodes and b in nodes and a != b \
                and nodes.get(a, {}).get("kind") != "Method" \
                and nodes.get(b, {}).get("kind") != "Method":
            weight[(a, b)] += 1
            weight[(b, a)] += 1
    nbrs = defaultdict(dict)
    for (a, b), w in weight.items():
        nbrs[a][b] = w
    labels = {t: i for i, t in enumerate(type_ids)}
    for _ in range(10):
        changed = False
        for t in type_ids:
            if not nbrs[t]:
                continue
            score = defaultdict(int)
            for nb, w in nbrs[t].items():
                if nb in labels:
                    score[labels[nb]] += w
            if not score:
                continue
            best = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            if best != labels[t]:
                labels[t] = best
                changed = True
        if not changed:
            break
    groups = defaultdict(list)
    for t in type_ids:
        groups[labels[t]].append(t)
    # fold singleton clusters into a package-level bucket
    clusters = []
    misc = []
    for lid, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(members) < 2:
            misc.extend(members)
            continue
        toks = Counter()
        for t in members:
            for tok in camel_tokens(nodes[t]["name"]):
                if tok not in CLUSTER_STOP and len(tok) > 2:
                    toks[tok] += 1
        top = [w for w, _ in toks.most_common(3)]
        label = "".join(w.capitalize() for w in top[:2]) or \
                nodes[members[0]]["name"]
        internal = sum(1 for (a, b) in weight
                       if labels.get(a) == lid and labels.get(b) == lid) / 2
        external = sum(1 for (a, b) in weight
                       if (labels.get(a) == lid) != (labels.get(b) == lid)) / 2
        cohesion = internal / (internal + external) if (internal + external) else 1.0
        clusters.append({"id": "C%d" % len(clusters), "label": label,
                         "members": sorted(members),
                         "keywords": top, "cohesion": round(cohesion, 2)})
    if misc:
        bypkg = defaultdict(list)
        for t in misc:
            bypkg[nodes[t].get("pkg", "")].append(t)
        for pkg, members in sorted(bypkg.items()):
            label = (pkg.split('.')[-1].capitalize() if pkg else "Misc")
            clusters.append({"id": "C%d" % len(clusters), "label": label,
                             "members": sorted(members), "keywords": [],
                             "cohesion": 0.0})
    return clusters

# ---------------------------------------------------------------------------
# Processes (execution flows)
# ---------------------------------------------------------------------------

def detect_processes(nodes, edges, member_cluster):
    calls_out = defaultdict(list)
    calls_in = defaultdict(set)
    for e in edges:
        if e["type"] == "CALLS":
            calls_out[e["src"]].append((e["dst"], e["conf"]))
            calls_in[e["dst"]].add(e["src"])

    def is_test(nid):
        n = nodes[nid]
        return is_test_file(n.get("file", "")) or \
            any(a in TEST_ANNOS for a in n.get("annotations", []))

    entries = []
    for nid in sorted(nodes):
        n = nodes[nid]
        if n["kind"] not in ("Method", "Constructor") or is_test(nid):
            continue
        annos = set(n.get("annotations", []))
        owner = nodes.get(n.get("owner"), {})
        owner_annos = set(owner.get("annotations", []))
        reason = None
        if n["name"] == "main" and n.get("static"):
            reason = "main"
        elif annos & ENTRY_METHOD_ANNOS:
            reason = "@" + sorted(annos & ENTRY_METHOD_ANNOS)[0]
        elif owner_annos & ENTRY_CLASS_ANNOS and n.get("public"):
            reason = "@" + sorted(owner_annos & ENTRY_CLASS_ANNOS)[0]
        if reason:
            entries.append((nid, reason))
    # secondary: public zero-caller methods with real fan-out
    if len(entries) < MAX_PROCESSES:
        extra = []
        seeded = {e for e, _ in entries}
        for nid in sorted(nodes):
            n = nodes[nid]
            if (n["kind"] == "Method" and n.get("public") and nid not in seeded
                    and not calls_in.get(nid) and calls_out.get(nid)
                    and not is_test(nid)):
                extra.append((nid, len(calls_out[nid])))
        extra.sort(key=lambda kv: (-kv[1], kv[0]))
        for nid, _ in extra[:MAX_PROCESSES - len(entries)]:
            entries.append((nid, "entry"))

    processes = {}
    for entry, reason in entries:
        # iterative DFS for the longest path (cycle-safe, capped)
        best = [entry]
        stack = [(entry, [entry])]
        steps = 0
        while stack and steps < 4000:
            steps += 1
            cur, path = stack.pop()
            if len(path) > len(best):
                best = path
            if len(path) >= MAX_TRACE_DEPTH:
                continue
            outs = sorted(calls_out.get(cur, []), key=lambda x: (-x[1], x[0]))
            picked = 0
            for nxt, conf in outs:
                if nxt in path or nodes.get(nxt, {}).get("kind") not in \
                        ("Method", "Constructor"):
                    continue
                stack.append((nxt, path + [nxt]))
                picked += 1
                if picked >= MAX_BRANCHING:
                    break
        if len(best) < MIN_STEPS:
            continue
        terminal = best[-1]
        key = (entry, terminal)
        if key not in processes or len(best) > len(processes[key]["steps"]):
            cl = {member_cluster.get(nodes[s].get("owner") or s) for s in best}
            cl.discard(None)
            ptype = "cross_cluster" if len(cl) > 1 else "intra_cluster"
            def disp(nid):
                n = nodes[nid]
                return n.get("display") or n["name"]
            processes[key] = {
                "label": "%s -> %s" % (disp(entry), disp(terminal)),
                "entry": entry, "terminal": terminal, "steps": best,
                "stepCount": len(best), "type": ptype, "reason": reason,
                "clusters": sorted(cl),
            }
    plist = sorted(processes.values(), key=lambda p: (-p["stepCount"], p["label"]))
    plist = plist[:MAX_PROCESSES]
    for i, p in enumerate(plist):
        p["id"] = "P%d" % (i + 1)
    return plist

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def jkg_path(root):
    return os.path.join(root, JKG_DIR)


def load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def file_hash(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def find_java_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                       and not d.startswith('.')]
        for fn in filenames:
            if fn.endswith('.java') and fn not in ('package-info.java',
                                                   'module-info.java'):
                out.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(out)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_analyze(root, quiet=False):
    t0 = time.time()
    cache_path = os.path.join(jkg_path(root), CACHE_FILE)
    old = load_json(cache_path) or {"files": {}}
    if old.get("schema") != SCHEMA_VERSION:
        old = {"files": {}}
    files = find_java_files(root)
    cache, parsed_new, kept, repo_bytes = {}, 0, 0, 0
    for rel in files:
        full = os.path.join(root, rel)
        try:
            h = file_hash(full)
            repo_bytes += os.path.getsize(full)
        except OSError:
            continue
        prev = old["files"].get(rel)
        if prev and prev["hash"] == h:
            cache[rel] = prev
            kept += 1
        else:
            try:
                with open(full, 'r', encoding='utf-8', errors='replace') as f:
                    src = f.read()
                cache[rel] = {"hash": h, "parsed": parse_java(src, rel)}
                parsed_new += 1
            except Exception as ex:
                sys.stderr.write("warn: failed to parse %s: %s\n" % (rel, ex))
    removed = len(set(old["files"]) - set(cache))

    gb = GraphBuilder(root, cache).build()
    clusters = detect_clusters(gb.nodes, gb.edges)
    member_cluster = {}
    for c in clusters:
        for m in c["members"]:
            member_cluster[m] = c["id"]
    processes = detect_processes(gb.nodes, gb.edges, member_cluster)

    graph = {
        "schema": SCHEMA_VERSION,
        "indexedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": os.path.abspath(root),
        "stats": {
            "files": len(cache),
            "types": sum(1 for n in gb.nodes.values()
                         if n["kind"] in ("Class", "Interface", "Enum", "Record")),
            "methods": sum(1 for n in gb.nodes.values()
                           if n["kind"] in ("Method", "Constructor")),
            "nodes": len(gb.nodes), "edges": len(gb.edges),
            "clusters": len(clusters), "processes": len(processes),
            "repoBytes": repo_bytes,
        },
        "nodes": sorted(gb.nodes.values(), key=lambda n: n["id"]),
        "edges": gb.edges,
        "clusters": clusters,
        "processes": processes,
    }
    save_json(os.path.join(jkg_path(root), GRAPH_FILE), graph)
    save_json(cache_path, {"schema": SCHEMA_VERSION, "files": cache})

    if not quiet:
        s = graph["stats"]
        print("✓ Knowledge graph built in %.1fs  (%d parsed, %d cached, %d removed)"
              % (time.time() - t0, parsed_new, kept, removed))
        print("  %d files · %d types · %d methods · %d edges · "
              "%d clusters · %d flows" % (s["files"], s["types"], s["methods"],
                                          s["edges"], s["clusters"],
                                          s["processes"]))
        print("  stored in %s/ (plain JSON, no database)" % JKG_DIR)
        if gb.lombok_types:
            print("  ◆ Lombok detected on %d type(s) — generated "
                  "getters/setters/builders synthesized into the graph"
                  % gb.lombok_types)
        print("  " + token_economics(repo_bytes))
    return graph


QUERY_TOKENS = 500  # typical tokens for one graph-query answer
PRICE_PER_MTOK = 3.0  # USD per 1M input tokens (Sonnet-class)


def record_saving(root, command, repo_bytes):
    """Token odometer: every graph query avoids one repo scan."""
    if not repo_bytes:
        return
    path = os.path.join(jkg_path(root), "savings.json")
    data = load_json(path) or {"questions": 0, "tokensSaved": 0, "events": []}
    saved = max(0, repo_bytes // 4 - QUERY_TOKENS)
    data["questions"] += 1
    data["tokensSaved"] += saved
    data["events"] = (data["events"] + [{"cmd": command, "saved": saved,
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}])[-200:]
    save_json(path, data)


def load_savings(root):
    return load_json(os.path.join(jkg_path(root), "savings.json")) or \
        {"questions": 0, "tokensSaved": 0, "events": []}


def fmt_tokens(t):
    return "%.1fM" % (t / 1e6) if t >= 1e6 else "%.0fk" % (t / 1e3) \
        if t >= 1000 else str(int(t))


def odometer_line(root):
    s = load_savings(root)
    if not s["questions"]:
        return None
    usd = s["tokensSaved"] / 1e6 * PRICE_PER_MTOK
    return ("odometer: %d questions answered from the graph · ~%s tokens "
            "saved (≈ $%.2f at $%.0f/M)" % (s["questions"],
                                            fmt_tokens(s["tokensSaved"]),
                                            usd, PRICE_PER_MTOK))


def token_economics(repo_bytes):
    repo_tokens = max(1, repo_bytes // 4)  # ~4 chars/token
    ratio = repo_tokens // QUERY_TOKENS
    def fmt(t):
        return "%.1fM" % (t / 1e6) if t >= 1e6 else "%.0fk" % (t / 1e3) \
            if t >= 1000 else str(t)
    return ("token economics: reading the repo ≈ %s tokens; one graph query "
            "≈ %s tokens (~%dx cheaper)"
            % (fmt(repo_tokens), fmt(QUERY_TOKENS), max(1, ratio)))


class Graph:
    """Query-side wrapper around graph.json."""

    def __init__(self, root):
        path = os.path.join(jkg_path(root), GRAPH_FILE)
        data = load_json(path)
        if not data:
            sys.exit("No knowledge graph found. Run:  jkg.py analyze [path]")
        self.data = data
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.out_edges = defaultdict(list)
        self.in_edges = defaultdict(list)
        for e in data["edges"]:
            self.out_edges[e["src"]].append(e)
            self.in_edges[e["dst"]].append(e)
        self.clusters = data["clusters"]
        self.member_cluster = {}
        for c in self.clusters:
            for m in c["members"]:
                self.member_cluster[m] = c
        self.processes = data["processes"]
        self.proc_by_step = defaultdict(list)
        for p in self.processes:
            for s in p["steps"]:
                self.proc_by_step[s].append(p)

    def display(self, nid):
        n = self.nodes.get(nid)
        if not n:
            return nid
        if n["kind"] in ("Method", "Constructor"):
            return "%s.%s" % (n["owner"].split('.')[-1], n["name"])
        return n["name"]

    def loc(self, nid):
        n = self.nodes.get(nid, {})
        return "%s:%s" % (n.get("file", "?"), n.get("line", "?"))

    # symbol lookup: accepts Name, Class.method, fqn, fqn#m/2
    def find_symbol(self, ref):
        if ref in self.nodes:
            return [ref]
        hits = []
        if '.' in ref and '#' not in ref:
            cls, meth = ref.rsplit('.', 1)
            for nid, n in self.nodes.items():
                if n["kind"] in ("Method", "Constructor") and n["name"] == meth:
                    owner = n["owner"]
                    if owner.endswith('.' + cls) or owner == cls or \
                            owner.split('.')[-1] == cls:
                        hits.append(nid)
            if hits:
                return sorted(hits)
            # maybe it's a fqn type
            for nid, n in self.nodes.items():
                if n["qname"] == ref:
                    hits.append(nid)
            if hits:
                return sorted(hits)
        for nid, n in self.nodes.items():
            if n["name"] == ref:
                hits.append(nid)
        if hits:
            order = {"Class": 0, "Interface": 1, "Enum": 2, "Record": 3,
                     "Method": 4, "Constructor": 5, "Field": 6}
            return sorted(hits, key=lambda h: (order.get(self.nodes[h]["kind"], 9), h))
        low = ref.lower()
        return sorted(nid for nid, n in self.nodes.items()
                      if low in n["name"].lower())


def resolve_one(g, ref):
    hits = g.find_symbol(ref)
    if not hits:
        sys.exit("Symbol not found: %r. Try:  jkg.py query \"%s\"" % (ref, ref))
    if len(hits) > 1:
        same_name = [h for h in hits if g.nodes[h]["name"] == ref or
                     g.nodes[h]["id"] == ref]
        if len(same_name) == 1:
            return same_name[0]
        kinds = {g.nodes[h]["kind"] for h in hits}
        if len(hits) > 1 and not (len(kinds) > 1 and len(same_name) >= 1):
            pass
        if len(hits) > 8 or (len(same_name) != 1 and len({g.nodes[h]["owner"]
                             if "owner" in g.nodes[h] else h for h in hits}) > 1):
            print("Ambiguous symbol %r — candidates:" % ref)
            for h in hits[:10]:
                n = g.nodes[h]
                print("  %-12s %-40s %s" % (n["kind"], h, g.loc(h)))
            sys.exit(1)
    return hits[0]


def cmd_query(root, text, top=12, as_json=False):
    g = Graph(root)
    record_saving(root, "query", g.data["stats"].get("repoBytes", 0))
    qtoks = [t for t in re.split(r'[^a-zA-Z0-9]+', text.lower()) if t]
    scored = []
    for nid, n in g.nodes.items():
        name = n["name"]
        ntoks = camel_tokens(name)
        s = 0.0
        nl = name.lower()
        joined = ''.join(qtoks)
        if nl == joined or nl == text.lower():
            s += 100
        if nl.startswith(joined):
            s += 30
        for qt in qtoks:
            if qt in ntoks:
                s += 50
            elif any(t.startswith(qt) for t in ntoks):
                s += 25
            elif qt in nl:
                s += 15
            if qt in n.get("qname", "").lower() and qt not in nl:
                s += 8
        for a in n.get("annotations", []):
            if a.lower() in qtoks:
                s += 20
        if s <= 0:
            continue
        s += {"Class": 6, "Interface": 5, "Enum": 4, "Record": 4,
              "Method": 3, "Constructor": 2, "Field": 0}.get(n["kind"], 0)
        scored.append((s, nid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top_hits = scored[:top]

    if as_json:
        print(json.dumps([{"id": nid, "score": s, "kind": g.nodes[nid]["kind"],
                           "file": g.nodes[nid].get("file"),
                           "line": g.nodes[nid].get("line")}
                          for s, nid in top_hits], indent=1))
        return
    if not top_hits:
        print("No matches for %r. The graph indexes names & annotations — "
              "try a different term or jkg.py flows." % text)
        return
    print("Results for %r:\n" % text)
    procs_seen = {}
    for s, nid in top_hits:
        n = g.nodes[nid]
        cl = g.member_cluster.get(nid if "owner" not in n else n["owner"])
        cname = (" [%s]" % cl["label"]) if cl else ""
        print("  %-11s %-44s %s%s" % (n["kind"], g.display(nid), g.loc(nid), cname))
        for p in g.proc_by_step.get(nid, [])[:2]:
            procs_seen[p["id"]] = p
    if procs_seen:
        print("\nRelated execution flows (jkg.py flow <id>):")
        for p in list(procs_seen.values())[:5]:
            print("  %-4s %s  (%d steps)" % (p["id"], p["label"], p["stepCount"]))


def cmd_context(root, ref):
    g = Graph(root)
    record_saving(root, "context", g.data["stats"].get("repoBytes", 0))
    nid = resolve_one(g, ref)
    n = g.nodes[nid]
    print("═" * 70)
    print("%s %s" % (n["kind"].upper(), g.display(nid)))
    print("  id:    %s" % nid)
    print("  at:    %s" % g.loc(nid))
    if n.get("annotations"):
        print("  annos: %s" % " ".join("@" + a for a in n["annotations"]))
    cl = g.member_cluster.get(n.get("owner", nid)) or g.member_cluster.get(nid)
    if cl:
        print("  area:  %s (%s)" % (cl["label"], cl["id"]))
    print("═" * 70)

    seeds = [nid]
    if n["kind"] in ("Class", "Interface", "Enum", "Record"):
        members = [e["dst"] for e in g.out_edges[nid]
                   if e["type"] in ("HAS_METHOD", "HAS_FIELD")]
        meths = [m for m in members if g.nodes[m]["kind"] in ("Method", "Constructor")]
        print("\nMembers (%d):" % len(members))
        for m in meths[:20]:
            mn = g.nodes[m]
            print("  %-12s %s/%s  line %s" % (mn["kind"], mn["name"],
                                              mn.get("arity", "?"), mn["line"]))
        sups = [e["dst"] for e in g.out_edges[nid]
                if e["type"] in ("EXTENDS", "IMPLEMENTS")]
        subs = [e["src"] for e in g.in_edges[nid]
                if e["type"] in ("EXTENDS", "IMPLEMENTS")]
        if sups:
            print("\nExtends/implements: " + ", ".join(g.display(s) for s in sups))
        if subs:
            print("Subtypes:           " + ", ".join(g.display(s) for s in subs))
        seeds = [nid] + meths

    callers, callees = [], []
    for s in seeds:
        for e in g.in_edges[s]:
            if e["type"] == "CALLS":
                callers.append((e["src"], s, e["conf"]))
        for e in g.out_edges[s]:
            if e["type"] in ("CALLS", "INSTANTIATES"):
                callees.append((s, e["dst"], e["conf"]))
    if callers:
        uniq = sorted({(s, d) for s, d, _ in callers})
        print("\nCallers (%d):" % len(uniq))
        for src, dst in uniq[:15]:
            print("  %-40s -> %-30s %s" % (g.display(src),
                                           g.display(dst), g.loc(src)))
    if callees:
        best = {}
        for _, dst, conf in callees:
            best[dst] = max(best.get(dst, 0), conf)
        print("\nCallees (%d):" % len(best))
        for dst in sorted(best)[:15]:
            print("  -> %-40s conf=%.2f  %s" % (g.display(dst), best[dst],
                                                g.loc(dst)))

    ov_in = [e["src"] for s in seeds for e in g.in_edges[s] if e["type"] == "OVERRIDES"]
    if ov_in:
        print("\nOverridden by: " + ", ".join(sorted({g.display(o) for o in ov_in})))

    procs = {}
    for s in seeds:
        for p in g.proc_by_step.get(s, []):
            procs[p["id"]] = p
    if procs:
        print("\nExecution flows through this symbol:")
        for p in list(procs.values())[:6]:
            print("  %-4s %s (%d steps)" % (p["id"], p["label"], p["stepCount"]))
    print()


def _bfs_impact(g, seeds, direction, max_depth, conf_floor=0.3):
    visited = {s: 0 for s in seeds}
    frontier = list(seeds)
    by_depth = defaultdict(list)
    rel = ("CALLS", "INSTANTIATES", "OVERRIDES", "EXTENDS", "IMPLEMENTS")
    for d in range(1, max_depth + 1):
        nxt = []
        for cur in frontier:
            edges = g.in_edges[cur] if direction == "upstream" else g.out_edges[cur]
            for e in edges:
                if e["type"] not in rel or e["conf"] < conf_floor:
                    continue
                other = e["src"] if direction == "upstream" else e["dst"]
                if other in visited:
                    continue
                visited[other] = d
                by_depth[d].append((other, e))
                nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return visited, by_depth


def risk_level(direct, total, procs, areas):
    if direct >= 30 or procs >= 5 or areas >= 5 or total >= 200:
        return "CRITICAL"
    if direct >= 15 or procs >= 3 or areas >= 3 or total >= 100:
        return "HIGH"
    if direct >= 5 or total >= 30:
        return "MEDIUM"
    return "LOW"


def cmd_impact(root, ref, direction="upstream", max_depth=3, as_json=False):
    g = Graph(root)
    record_saving(root, "impact", g.data["stats"].get("repoBytes", 0))
    nid = resolve_one(g, ref)
    n = g.nodes[nid]
    seeds = {nid}
    if n["kind"] in ("Class", "Interface", "Enum", "Record"):
        for e in g.out_edges[nid]:
            if e["type"] in ("HAS_METHOD", "HAS_FIELD"):
                seeds.add(e["dst"])
    visited, by_depth = _bfs_impact(g, seeds, direction, max_depth)
    impacted = [v for v in visited if v not in seeds]
    direct = len(by_depth.get(1, []))
    procs = {p["id"]: p for s in list(seeds) + impacted
             for p in g.proc_by_step.get(s, [])}
    areas = set()
    for m in impacted:
        key = g.nodes[m].get("owner") or m
        if key in g.member_cluster:
            areas.add(g.member_cluster[key]["id"])
    risk = risk_level(direct, len(impacted), len(procs), len(areas))

    # epistemic boundary: target is an interface method or has overrides
    boundary = None
    if n["kind"] == "Interface" or (n.get("owner") and
            g.nodes.get(n["owner"], {}).get("kind") == "Interface"):
        boundary = ("target sits behind an interface — callers bound via "
                    "dependency injection / dynamic dispatch may not all be "
                    "traced. Treat results as a LOWER BOUND.")
    elif any(e["type"] == "OVERRIDES" for e in g.in_edges.get(nid, [])):
        boundary = ("target is overridden in subtypes — runtime dispatch may "
                    "reach overrides instead. Treat results as a lower bound.")

    if as_json:
        print(json.dumps({
            "target": nid, "direction": direction, "risk": risk,
            "directCount": direct, "totalAffected": len(impacted),
            "affectedProcesses": [p["label"] for p in procs.values()],
            "affectedAreas": sorted(areas),
            "byDepth": {str(d): [x[0] for x in xs] for d, xs in by_depth.items()},
            "epistemic": "lower-bound" if boundary else "complete",
        }, indent=1))
        return

    arrow = "what depends on this (breaks if you change it)" \
        if direction == "upstream" else "what this depends on"
    print("═" * 70)
    print("IMPACT: %s %s   [%s]" % (n["kind"], g.display(nid), direction))
    print("  %s" % arrow)
    print("═" * 70)
    print("\n  RISK: %s   (%d direct, %d total affected, %d flows, %d areas)"
          % (risk, direct, len(impacted), len(procs), len(areas)))
    if risk in ("HIGH", "CRITICAL"):
        print("  ⚠ %s risk — review every depth-1 caller before editing, and"
              % risk)
        print("    run the affected flows below after the change.")
    if boundary:
        print("  ◬ epistemic: %s" % boundary)

    labels = {1: "WILL BREAK (direct)", 2: "LIKELY AFFECTED (indirect)",
              3: "MAY NEED TESTING (transitive)"}
    for d in sorted(by_depth):
        items = by_depth[d]
        print("\n  d=%d %s — %d symbol(s):" % (d, labels.get(d, "transitive"),
                                               len(items)))
        for other, e in sorted(items, key=lambda x: x[0])[:25]:
            print("    %-44s via %-11s conf=%.2f  %s"
                  % (g.display(other), e["type"], e["conf"], g.loc(other)))
        if len(items) > 25:
            print("    … and %d more" % (len(items) - 25))
    if procs:
        print("\n  Affected execution flows:")
        for p in list(procs.values())[:8]:
            print("    %-4s %s" % (p["id"], p["label"]))
    if areas:
        names = [c["label"] for c in g.clusters if c["id"] in areas]
        print("\n  Affected functional areas: " + ", ".join(sorted(names)))
    print()


def cmd_edge_list(root, ref, direction):
    g = Graph(root)
    record_saving(root, "callers", g.data["stats"].get("repoBytes", 0))
    nid = resolve_one(g, ref)
    edges = g.in_edges[nid] if direction == "in" else g.out_edges[nid]
    rels = [e for e in edges if e["type"] in ("CALLS", "INSTANTIATES")]
    label = "Callers of" if direction == "in" else "Callees of"
    print("%s %s (%d):" % (label, g.display(nid), len(rels)))
    for e in sorted(rels, key=lambda e: (e["src"], e["dst"])):
        other = e["src"] if direction == "in" else e["dst"]
        print("  %-44s conf=%.2f %-18s %s" % (g.display(other), e["conf"],
                                              e.get("reason", ""), g.loc(other)))


def cmd_flows(root, filt=None):
    g = Graph(root)
    print("Execution flows (%d):  [jkg.py flow <id> for the full trace]\n"
          % len(g.processes))
    for p in g.processes:
        if filt and filt.lower() not in p["label"].lower():
            continue
        print("  %-4s %-58s %d steps  %s" % (p["id"], p["label"][:58],
                                             p["stepCount"], p["reason"]))


def cmd_flow(root, pid):
    g = Graph(root)
    record_saving(root, "flow", g.data["stats"].get("repoBytes", 0))
    proc = next((p for p in g.processes
                 if p["id"].lower() == pid.lower()
                 or pid.lower() in p["label"].lower()), None)
    if not proc:
        sys.exit("Flow not found: %s  (list with: jkg.py flows)" % pid)
    print("FLOW %s: %s  (%s, entry via %s)\n" % (proc["id"], proc["label"],
                                                 proc["type"], proc["reason"]))
    for i, s in enumerate(proc["steps"]):
        print("  %2d. %-44s %s" % (i + 1, g.display(s), g.loc(s)))


def cmd_clusters(root):
    g = Graph(root)
    print("Functional areas (%d):\n" % len(g.clusters))
    for c in g.clusters:
        print("  %-4s %-28s %3d types  cohesion=%.2f  %s"
              % (c["id"], c["label"], len(c["members"]), c["cohesion"],
                 ", ".join(g.nodes[m]["name"] for m in c["members"][:5])
                 + ("…" if len(c["members"]) > 5 else "")))


def cmd_hierarchy(root, ref):
    g = Graph(root)
    nid = resolve_one(g, ref)

    def ups(x, depth=0, seen=None):
        seen = seen or set()
        if x in seen:
            return
        seen.add(x)
        for e in g.out_edges[x]:
            if e["type"] in ("EXTENDS", "IMPLEMENTS"):
                print("  " * (depth + 1) + "▲ %s (%s)"
                      % (g.display(e["dst"]), e["type"].lower()))
                ups(e["dst"], depth + 1, seen)

    def downs(x, depth=0, seen=None):
        seen = seen or set()
        if x in seen:
            return
        seen.add(x)
        for e in sorted(g.in_edges[x], key=lambda e: e["src"]):
            if e["type"] in ("EXTENDS", "IMPLEMENTS"):
                print("  " * (depth + 1) + "▼ %s (%s)"
                      % (g.display(e["src"]), e["type"].lower()))
                downs(e["src"], depth + 1, seen)

    print("%s %s  %s" % (g.nodes[nid]["kind"], g.display(nid), g.loc(nid)))
    print("Supertypes:")
    ups(nid)
    print("Subtypes:")
    downs(nid)


def cmd_cycles(root):
    g = Graph(root)
    pkg_edges = defaultdict(set)
    for e in g.data["edges"]:
        if e["type"] not in ("CALLS", "EXTENDS", "IMPLEMENTS", "INSTANTIATES"):
            continue
        a = g.nodes.get(e["src"], {})
        b = g.nodes.get(e["dst"], {})
        pa = a.get("pkg") or (g.nodes.get(a.get("owner"), {}).get("pkg") if a.get("owner") else None)
        pb = b.get("pkg") or (g.nodes.get(b.get("owner"), {}).get("pkg") if b.get("owner") else None)
        if pa and pb and pa != pb:
            pkg_edges[pa].add(pb)
    # Tarjan SCC
    idx, low, onstk, stack, sccs = {}, {}, set(), [], []
    counter = [0]

    def strong(v):
        idx[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        onstk.add(v)
        for w in sorted(pkg_edges.get(v, ())):
            if w not in idx:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in onstk:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                onstk.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    sys.setrecursionlimit(10000)
    for v in sorted(set(pkg_edges) | {x for s in pkg_edges.values() for x in s}):
        if v not in idx:
            strong(v)
    if not sccs:
        print("No package dependency cycles. ✓")
    else:
        print("Package dependency cycles (%d):" % len(sccs))
        for s in sccs:
            print("  ⟳ " + "  ⇄  ".join(s))


def cmd_diff(root):
    """Like GitNexus detect_changes(): what changed since the last analyze."""
    cache_path = os.path.join(jkg_path(root), CACHE_FILE)
    old = load_json(cache_path)
    if not old:
        sys.exit("No knowledge graph found. Run:  jkg.py analyze")
    g = Graph(root)
    changed, deleted = [], []
    files = set(find_java_files(root))
    for rel in sorted(files):
        full = os.path.join(root, rel)
        prev = old["files"].get(rel)
        try:
            h = file_hash(full)
        except OSError:
            continue
        if not prev:
            changed.append((rel, "added"))
        elif prev["hash"] != h:
            changed.append((rel, "modified"))
    for rel in sorted(set(old["files"]) - files):
        deleted.append(rel)
    if not changed and not deleted:
        print("✓ No changes since last analyze (%s)."
              % g.data.get("indexedAt", "?"))
        return
    print("Changes since last analyze (%s):\n" % g.data.get("indexedAt", "?"))
    affected_syms, affected_procs = set(), {}
    for rel, status in changed:
        print("  %-9s %s" % (status, rel))
        if status == "modified":
            try:
                with open(os.path.join(root, rel), 'r', encoding='utf-8',
                          errors='replace') as f:
                    new_parsed = parse_java(f.read(), rel)
            except Exception:
                continue
            old_syms = _file_symbols(old["files"][rel]["parsed"])
            new_syms = _file_symbols(new_parsed)
            for s in sorted(set(old_syms) - set(new_syms)):
                print("            - removed: %s" % s)
                affected_syms.add(s)
            for s in sorted(set(new_syms) - set(old_syms)):
                print("            + added:   %s" % s)
            for s in sorted(set(old_syms) & set(new_syms)):
                if old_syms[s] != new_syms[s]:
                    print("            ~ changed: %s" % s)
                    affected_syms.add(s)
    for rel in deleted:
        print("  %-9s %s" % ("deleted", rel))
        affected_syms |= set(_file_symbols(old["files"][rel]["parsed"]))
    for s in affected_syms:
        for p in g.proc_by_step.get(s, []):
            affected_procs[p["id"]] = p
        for e in g.in_edges.get(s, []):
            if e["type"] == "CALLS":
                for p in g.proc_by_step.get(e["src"], []):
                    affected_procs[p["id"]] = p
    if affected_procs:
        print("\n  Execution flows touched by these changes:")
        for p in affected_procs.values():
            print("    %-4s %s" % (p["id"], p["label"]))
    callers = set()
    for s in affected_syms:
        for e in g.in_edges.get(s, []):
            if e["type"] in ("CALLS", "OVERRIDES"):
                callers.add(e["src"])
    if callers:
        print("\n  Callers of changed/removed symbols (verify these):")
        for c in sorted(callers)[:20]:
            print("    %-44s %s" % (g.display(c), g.loc(c)))
    print("\nRun `jkg.py analyze` to refresh the graph.")


def _file_symbols(parsed):
    """{symbol_id: signature_hash} for one parsed file."""
    out = {}
    for td in parsed["types"]:
        out[td["qname"]] = hash((tuple(td["extends"]), tuple(td["implements"]),
                                 td["kind"]))
        for m in td["methods"]:
            if m.get("synthetic"):
                continue  # Lombok-synthesized: not present in a fresh parse
            mid = "%s#%s/%d" % (td["qname"], m["name"], m["arity"])
            sig_calls = tuple(json.dumps({k: v for k, v in c.items()
                                          if k != "line"}, sort_keys=True)
                              for c in m["calls"])
            out[mid] = hash((sig_calls, tuple(p["type"] for p in m["params"])))
    return out


def cmd_stats(root):
    g = Graph(root)
    s = g.data["stats"]
    print("Knowledge graph for %s" % g.data["root"])
    print("  indexed:  %s  (schema v%d)" % (g.data["indexedAt"], g.data["schema"]))
    print("  files:    %d" % s["files"])
    print("  types:    %d   methods: %d   nodes: %d" % (s["types"], s["methods"],
                                                        s["nodes"]))
    print("  edges:    %d" % s["edges"])
    by_type = Counter(e["type"] for e in g.data["edges"])
    for t, c in by_type.most_common():
        print("            %-14s %d" % (t, c))
    print("  clusters: %d   flows: %d" % (s["clusters"], s["processes"]))
    top = sorted(g.nodes.values(),
                 key=lambda n: -len([e for e in g.in_edges[n["id"]]
                                     if e["type"] == "CALLS"]))[:5]
    print("  most-called symbols:")
    for n in top:
        c = len([e for e in g.in_edges[n["id"]] if e["type"] == "CALLS"])
        if c:
            print("    %-44s %d callers" % (g.display(n["id"]), c))
    if s.get("repoBytes"):
        print("  " + token_economics(s["repoBytes"]))
    odo = odometer_line(root)
    if odo:
        print("  " + odo)


# ---------------------------------------------------------------------------
# ask — natural-language questions answered from the graph
# ---------------------------------------------------------------------------

def _extract_symbols(g, question):
    """Symbols mentioned in the question, best matches first."""
    refs = re.findall(r'[A-Z][\w$]*(?:\.[a-zA-Z_$][\w$]*)?|[a-z_$][\w$]*\(\)',
                      question)
    stop = {"I", "If", "The", "What", "Who", "How", "When", "Where", "Why",
            "Is", "It", "Do", "Does", "Can", "Should", "Will", "And", "Or"}
    # prefer qualified Class.method refs, then longer names
    refs = sorted((r.rstrip('()') for r in refs if r.rstrip('()') not in stop
                   and len(r.rstrip('()')) > 2),
                  key=lambda r: ('.' not in r, -len(r)))
    hits = []
    for r in refs:
        found = g.find_symbol(r)
        # only accept exact-name or exact-id matches here; fuzzy comes later
        exact = [f for f in found
                 if g.nodes[f]["name"] == r or f == r or
                 ('.' in r and f.endswith('#' + r.split('.')[-1] + '/' +
                                          str(g.nodes[f].get("arity", ''))))
                 or ('.' in r and g.nodes[f]["name"] == r.split('.')[-1])]
        if exact:
            hits.append((r, exact[0]))
    # fall back to fuzzy: nouns in the question matched against node names
    if not hits:
        toks = [t for t in re.split(r'[^a-zA-Z0-9]+', question.lower())
                if len(t) > 3]
        best, score = None, 0
        for nid, n in g.nodes.items():
            nt = set(camel_tokens(n["name"]))
            s = len(nt & set(toks))
            if s > score:
                best, score = nid, s
        if best:
            hits.append((g.nodes[best]["name"], best))
    return hits


def cmd_ask(root, question):
    g = Graph(root)
    record_saving(root, "ask", g.data["stats"].get("repoBytes", 0))
    q = question.lower()
    syms = _extract_symbols(g, question)
    print("Q: %s\n" % question)

    def files_to_read(ids):
        seen, out = set(), []
        for i in ids:
            f = g.nodes.get(i, {}).get("file")
            if f and f not in seen:
                seen.add(f)
                out.append("%s:%s" % (f, g.nodes[i].get("line", "")))
        return out[:5]

    # intent: impact / safety
    if any(k in q for k in ("what calls", "who calls", "what breaks", "break",
                            "safe to", "impact", "depends on", "blast",
                            "callers of", "remove", "rename", "delete")):
        if not syms:
            print("I couldn't find a symbol in that question. Try: "
                  "ask \"what breaks if I change OrderService.placeOrder?\"")
            return
        ref, nid = syms[0]
        print("Answer (impact analysis on %s):\n" % g.display(nid))
        cmd_impact(root, nid)
        return

    # intent: how does X work / what happens when
    if any(k in q for k in ("how does", "how do", "what happens", "walk me",
                            "explain", "trace", "flow of", "work")):
        toks = [t for t in re.split(r'[^a-zA-Z0-9]+', q) if len(t) > 3 and
                t not in ("does", "what", "happens", "when", "work", "works",
                          "flow", "explain", "trace", "walk")]
        best, score = None, 0
        for p in g.processes:
            s = sum(1 for t in toks if t in p["label"].lower())
            for sid in p["steps"]:
                s += sum(0.5 for t in toks
                         if t in g.nodes[sid]["name"].lower())
            if s > score:
                best, score = p, s
        if best:
            print("Answer — this is implemented by execution flow %s:\n"
                  % best["id"])
            for i, s in enumerate(best["steps"]):
                n = g.nodes[s]
                annos = " ".join("@" + a for a in n.get("annotations", []))
                print("  %2d. %-44s %s %s" % (i + 1, g.display(s),
                                              g.loc(s), annos))
            areas = {g.member_cluster[g.nodes[s].get("owner") or s]["label"]
                     for s in best["steps"]
                     if (g.nodes[s].get("owner") or s) in g.member_cluster}
            if areas:
                print("\n  Functional areas involved: " + ", ".join(sorted(areas)))
            print("\n  To go deeper, read only: " +
                  ", ".join(files_to_read(best["steps"])))
            return
        # no flow matched — fall through to search

    # intent: where is / find  (and default)
    if syms and not any(k in q for k in ("where", "find", "search", "list")):
        ref, nid = syms[0]
        print("Answer (context for %s):\n" % g.display(nid))
        cmd_context(root, nid)
        return
    print("Answer (closest matches in the graph):\n")
    cmd_query(root, " ".join(t for t in re.split(r'[^a-zA-Z0-9]+', question)
                             if len(t) > 2), top=8)


# ---------------------------------------------------------------------------
# report — architecture health one-pager
# ---------------------------------------------------------------------------

def _pkg_sccs(g):
    pkg_edges = defaultdict(set)
    for e in g.data["edges"]:
        if e["type"] not in ("CALLS", "EXTENDS", "IMPLEMENTS", "INSTANTIATES"):
            continue
        a, b = g.nodes.get(e["src"], {}), g.nodes.get(e["dst"], {})
        pa = a.get("pkg") or g.nodes.get(a.get("owner"), {}).get("pkg")
        pb = b.get("pkg") or g.nodes.get(b.get("owner"), {}).get("pkg")
        if pa and pb and pa != pb:
            pkg_edges[pa].add(pb)
    idx, low, onstk, stack, sccs = {}, {}, set(), [], []
    counter = [0]

    def strong(v):
        idx[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        onstk.add(v)
        for w in sorted(pkg_edges.get(v, ())):
            if w not in idx:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in onstk:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                onstk.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    sys.setrecursionlimit(10000)
    for v in sorted(set(pkg_edges) | {x for s in pkg_edges.values() for x in s}):
        if v not in idx:
            strong(v)
    return sccs


def cmd_report(root):
    g = Graph(root)
    record_saving(root, "report", g.data["stats"].get("repoBytes", 0))
    s = g.data["stats"]
    lines = []
    w = lines.append
    w("# Architecture Health Report — %s" % os.path.basename(g.data["root"]))
    w("")
    w("_Generated by jkg from the knowledge graph (%s) — no files were "
      "scanned._" % g.data["indexedAt"])
    w("")
    w("**%d** types · **%d** methods · **%d** call edges · **%d** functional "
      "areas · **%d** execution flows" % (s["types"], s["methods"],
                                          sum(1 for e in g.data["edges"]
                                              if e["type"] == "CALLS"),
                                          s["clusters"], s["processes"]))

    # god classes: fan-in to a type's members
    fan_in = Counter()
    for e in g.data["edges"]:
        if e["type"] != "CALLS":
            continue
        owner = g.nodes.get(e["dst"], {}).get("owner")
        src_owner = g.nodes.get(e["src"], {}).get("owner")
        if owner and src_owner and owner != src_owner:
            fan_in[owner] += 1
    w("")
    w("## Hotspots (god-class candidates)")
    w("")
    w("Types with the highest external fan-in — changes here ripple furthest:")
    w("")
    w("| Type | external calls in | methods | risk if changed |")
    w("|------|------------------:|--------:|-----------------|")
    for tid, cnt in fan_in.most_common(6):
        n = g.nodes.get(tid)
        if not n:
            continue
        mcount = sum(1 for e in g.out_edges[tid] if e["type"] == "HAS_METHOD")
        risk = risk_level(cnt, cnt, 0, 0)
        w("| `%s` | %d | %d | %s |" % (n["name"], cnt, mcount, risk))

    # dead-code candidates
    has_caller = {e["dst"] for e in g.data["edges"]
                  if e["type"] in ("CALLS", "OVERRIDES")}
    orphans = []
    for nid, n in g.nodes.items():
        if n["kind"] != "Method" or not n.get("public"):
            continue
        if nid in has_caller or is_test_file(n.get("file", "")):
            continue
        if n.get("annotations") or n["name"] == "main" or n.get("synthetic"):
            continue  # entry points / framework-invoked / Lombok-generated
        if any(e["type"] == "OVERRIDES" for e in g.out_edges[nid]):
            continue  # implements an interface — called via dispatch
        orphans.append(nid)
    w("")
    w("## Dead-code candidates")
    w("")
    if orphans:
        w("Public methods with no internal callers, no framework annotations, "
          "and no interface contract (%d found):" % len(orphans))
        w("")
        for nid in sorted(orphans)[:12]:
            w("- `%s`  — %s" % (g.display(nid), g.loc(nid)))
        if len(orphans) > 12:
            w("- … and %d more" % (len(orphans) - 12))
    else:
        w("None found. ✓")

    # cycles
    sccs = _pkg_sccs(g)
    w("")
    w("## Package dependency cycles")
    w("")
    if sccs:
        for scc in sccs:
            w("- ⟳ " + " ⇄ ".join("`%s`" % p for p in scc))
    else:
        w("None. ✓")

    # cohesion
    w("")
    w("## Functional-area cohesion")
    w("")
    w("| Area | types | cohesion | note |")
    w("|------|------:|---------:|------|")
    for c in g.clusters:
        if len(c["members"]) < 2:
            continue
        note = "tightly focused" if c["cohesion"] >= 0.6 else \
            "moderately coupled" if c["cohesion"] >= 0.3 else \
            "highly entangled with other areas"
        w("| %s | %d | %.2f | %s |" % (c["label"], len(c["members"]),
                                       c["cohesion"], note))

    # longest flows
    w("")
    w("## Deepest execution flows")
    w("")
    for p in g.processes[:5]:
        w("- **%s** — %d steps (`jkg.py flow %s`)" % (p["label"],
                                                      p["stepCount"], p["id"]))
    odo = odometer_line(root)
    if odo:
        w("")
        w("---")
        w("_%s_" % odo)
    report = "\n".join(lines) + "\n"
    out = os.path.join(jkg_path(root), "report.md")
    with open(out, 'w', encoding='utf-8') as f:
        f.write(report)
    print(report)
    print("(saved to %s)" % out)


# ---------------------------------------------------------------------------
# mcp — serve the graph as MCP tools over stdio (works with any MCP client)
# ---------------------------------------------------------------------------

MCP_TOOLS = [
    {"name": "ask", "description": "Ask a natural-language question about the "
     "Java codebase, answered from the knowledge graph (no file scanning).",
     "args": {"question": "the question"}},
    {"name": "query", "description": "Search the code knowledge graph for "
     "symbols and execution flows matching a concept.",
     "args": {"text": "search terms"}},
    {"name": "context", "description": "Full 360-degree view of one symbol: "
     "definition, callers, callees, overrides, execution flows.",
     "args": {"symbol": "Class, Class.method, or fully-qualified name"}},
    {"name": "impact", "description": "Blast-radius analysis with risk level "
     "(LOW/MEDIUM/HIGH/CRITICAL). MUST be called before editing any symbol.",
     "args": {"symbol": "target symbol"}},
    {"name": "flows", "description": "List detected execution flows (entry "
     "point to terminal call chains).", "args": {}},
    {"name": "flow", "description": "Step-by-step trace of one execution flow.",
     "args": {"id": "flow id, e.g. P1"}},
    {"name": "diff", "description": "Show which symbols and execution flows "
     "changed since the last analyze. Call before committing.", "args": {}},
    {"name": "report", "description": "Architecture health report: hotspots, "
     "dead code, dependency cycles, cohesion.", "args": {}},
    {"name": "analyze", "description": "Build or incrementally refresh the "
     "knowledge graph.", "args": {}},
]


def _mcp_tool_defs():
    defs = []
    for t in MCP_TOOLS:
        props = {k: {"type": "string", "description": v}
                 for k, v in t["args"].items()}
        defs.append({"name": "jkg_" + t["name"],
                     "description": t["description"],
                     "inputSchema": {"type": "object", "properties": props,
                                     "required": list(props)}})
    return defs


def _mcp_call(root, name, args):
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if name == "jkg_ask":
                cmd_ask(root, args.get("question", ""))
            elif name == "jkg_query":
                cmd_query(root, args.get("text", ""))
            elif name == "jkg_context":
                cmd_context(root, args.get("symbol", ""))
            elif name == "jkg_impact":
                cmd_impact(root, args.get("symbol", ""))
            elif name == "jkg_flows":
                cmd_flows(root)
            elif name == "jkg_flow":
                cmd_flow(root, args.get("id", "P1"))
            elif name == "jkg_diff":
                cmd_diff(root)
            elif name == "jkg_report":
                cmd_report(root)
            elif name == "jkg_analyze":
                cmd_analyze(root)
            else:
                print("unknown tool: %s" % name)
    except SystemExit as e:
        if e.code and isinstance(e.code, str):
            buf.write(e.code + "\n")
    except Exception as ex:
        buf.write("error: %s\n" % ex)
    return buf.getvalue()


def cmd_mcp(root):
    """Newline-delimited JSON-RPC 2.0 over stdio (MCP transport)."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid, method = req.get("id"), req.get("method", "")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": req.get("params", {}).get(
                    "protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jkg", "version": "1.0.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid,
                  "result": {"tools": _mcp_tool_defs()}})
        elif method == "tools/call":
            p = req.get("params", {})
            text = _mcp_call(root, p.get("name", ""),
                             p.get("arguments", {}) or {})
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text or "(no output)"}]}})
        elif rid is not None:  # unknown request → empty result
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        # notifications (no id) are ignored


# ---------------------------------------------------------------------------
# Interactive graph UI (single self-contained HTML, no external assets)
# ---------------------------------------------------------------------------

def _viz_payload(g):
    """Type-level view of the graph for the UI."""
    owner = {nid: n.get("owner") for nid, n in g.nodes.items()}

    def to_type(nid):
        return owner.get(nid) or nid

    type_ids = [nid for nid, n in g.nodes.items()
                if n["kind"] in ("Class", "Interface", "Enum", "Record")]
    callers_of_type = Counter()
    agg = {}
    for e in g.data["edges"]:
        if e["type"] in ("HAS_METHOD", "HAS_FIELD"):
            continue
        a, b = to_type(e["src"]), to_type(e["dst"])
        if a == b or a not in g.nodes or b not in g.nodes:
            continue
        if g.nodes[a]["kind"] not in ("Class", "Interface", "Enum", "Record"):
            continue
        if g.nodes[b]["kind"] not in ("Class", "Interface", "Enum", "Record"):
            continue
        et = "CALLS" if e["type"] in ("CALLS", "INSTANTIATES", "OVERRIDES") \
            else e["type"]
        key = (a, b, et)
        agg[key] = agg.get(key, 0) + 1
        if et == "CALLS":
            callers_of_type[b] += 1

    def top_links(nid, incoming):
        out, seen = [], set()
        members = {nid} | {e["dst"] for e in g.out_edges[nid]
                           if e["type"] in ("HAS_METHOD", "HAS_FIELD")}
        for m in members:
            edges = g.in_edges[m] if incoming else g.out_edges[m]
            for e in edges:
                if e["type"] not in ("CALLS", "INSTANTIATES"):
                    continue
                o = e["src"] if incoming else e["dst"]
                lab = "%s → %s" % (g.display(e["src"]), g.display(e["dst"]))
                if lab not in seen and to_type(o) != nid:
                    seen.add(lab)
                    out.append(lab)
        return out[:8]

    vnodes = []
    for nid in sorted(type_ids):
        n = g.nodes[nid]
        cl = g.member_cluster.get(nid)
        methods = [g.nodes[e["dst"]] for e in g.out_edges[nid]
                   if e["type"] == "HAS_METHOD"]
        vnodes.append({
            "id": nid, "name": n["name"], "kind": n["kind"],
            "file": n.get("file", ""), "line": n.get("line", 0),
            "cluster": cl["id"] if cl else "",
            "annotations": n.get("annotations", []),
            "callers": callers_of_type.get(nid, 0),
            "methods": sorted(m["name"] for m in methods),
            "inTop": top_links(nid, True), "outTop": top_links(nid, False),
        })
    vedges = [{"src": a, "dst": b, "type": t, "w": w}
              for (a, b, t), w in sorted(agg.items())]
    vflows = []
    for p in g.processes:
        tsteps, last = [], None
        for s in p["steps"]:
            t = to_type(s)
            if t != last:
                tsteps.append(t)
                last = t
        vflows.append({"id": p["id"], "label": p["label"],
                       "typeSteps": tsteps,
                       "steps": [g.display(s) + "  (" + g.loc(s) + ")"
                                 for s in p["steps"]]})
    s = g.data["stats"]
    repo_tokens = max(1, s.get("repoBytes", 0) // 4)
    sav = load_savings(g.data["root"])
    return {"project": os.path.basename(g.data["root"]), "stats": s,
            "tokens": {"repo": repo_tokens, "query": QUERY_TOKENS,
                       "ratio": max(1, repo_tokens // QUERY_TOKENS),
                       "saved": sav["tokensSaved"],
                       "questions": sav["questions"]},
            "nodes": vnodes, "edges": vedges,
            "clusters": [{"id": c["id"], "label": c["label"],
                          "n": len(c["members"])} for c in g.clusters],
            "flows": vflows}


VIZ_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>jkg — Java Knowledge Graph</title>
<style>
  :root { --bg:#0d1117; --panel:#161b22; --line:#30363d; --fg:#e6edf3;
          --dim:#8b949e; --accent:#58a6ff; --hot:#ff5252; }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--fg);
         font:13px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         overflow:hidden; }
  #hdr { position:fixed; top:0; left:0; right:0; height:52px; z-index:10;
         display:flex; align-items:center; gap:14px; padding:0 16px;
         background:var(--panel); border-bottom:1px solid var(--line); }
  #hdr h1 { font-size:15px; font-weight:600; white-space:nowrap; }
  #hdr h1 span { color:var(--accent); }
  #stats { color:var(--dim); white-space:nowrap; }
  #badge { background:#1f6feb22; border:1px solid #1f6feb; color:#79c0ff;
           border-radius:14px; padding:3px 12px; white-space:nowrap; }
  #search { background:var(--bg); color:var(--fg); border:1px solid var(--line);
            border-radius:6px; padding:5px 10px; width:200px; }
  select { background:var(--bg); color:var(--fg); border:1px solid var(--line);
           border-radius:6px; padding:5px 8px; max-width:260px; }
  label.tgl { color:var(--dim); user-select:none; white-space:nowrap; }
  canvas { position:fixed; top:52px; left:0; }
  #panel { position:fixed; top:52px; right:0; bottom:0; width:320px;
           background:var(--panel); border-left:1px solid var(--line);
           padding:14px; overflow-y:auto; display:none; }
  #panel h2 { font-size:14px; color:var(--accent); word-break:break-all; }
  #panel .kind { color:var(--dim); font-size:11px; text-transform:uppercase; }
  #panel .sec { margin-top:12px; color:var(--dim); font-size:11px;
                text-transform:uppercase; letter-spacing:.5px; }
  #panel ul { list-style:none; margin-top:4px; }
  #panel li { padding:2px 0; color:var(--fg); font-size:12px;
              word-break:break-all; }
  #panel .file { color:var(--dim); font-size:11px; word-break:break-all; }
  #panel .anno { color:#d2a8ff; }
  #legend { position:fixed; left:12px; bottom:12px; background:var(--panel);
            border:1px solid var(--line); border-radius:8px; padding:10px 12px;
            max-height:40vh; overflow-y:auto; }
  #legend div { display:flex; align-items:center; gap:7px; padding:2px 0;
                cursor:pointer; color:var(--dim); }
  #legend div.on { color:var(--fg); }
  #legend i { width:10px; height:10px; border-radius:50%; display:inline-block; }
  #hint { position:fixed; right:332px; bottom:12px; color:var(--dim);
          font-size:11px; }
</style></head><body>
<div id="hdr">
  <h1>⬡ <span>jkg</span> · __PROJECT__</h1>
  <span id="stats"></span>
  <span id="badge"></span>
  <input id="search" placeholder="search types… (Enter to focus)">
  <select id="flowSel"><option value="">— highlight a flow —</option></select>
  <label class="tgl"><input type="checkbox" id="tCalls" checked> calls</label>
  <label class="tgl"><input type="checkbox" id="tInh" checked> inheritance</label>
</div>
<canvas id="cv"></canvas>
<div id="panel"></div>
<div id="legend"></div>
<div id="hint">drag to pan · wheel to zoom · drag nodes · click for details</div>
<script>
const DATA = __JKG_DATA__;
const PALETTE = ["#58a6ff","#3fb950","#d29922","#f778ba","#a371f7","#ff7b72",
                 "#56d4dd","#9e6a03","#7ee787","#ffa657","#79c0ff","#d2a8ff"];
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
let W, H, dpr = window.devicePixelRatio || 1;
function resize(){ W = innerWidth - 320; H = innerHeight - 52;
  cv.width = W*dpr; cv.height = H*dpr; cv.style.width = W+'px';
  cv.style.height = H+'px'; ctx.setTransform(dpr,0,0,dpr,0,0); }
resize(); addEventListener('resize', ()=>{resize();});

function mulberry32(a){return function(){a|=0;a=a+0x6D2B79F5|0;
  let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;
  return ((t^t>>>14)>>>0)/4294967296;}}
const clusterIdx = {}; DATA.clusters.forEach((c,i)=>clusterIdx[c.id]=i);
const clusterColor = id => PALETTE[(clusterIdx[id]||0) % PALETTE.length];

const nodes = DATA.nodes.map((n,i)=>{
  const r = mulberry32(i+7), ci = clusterIdx[n.cluster]||0;
  const ang = (ci / Math.max(1,DATA.clusters.length)) * 2*Math.PI;
  const cx = Math.cos(ang)*Math.min(W,H)*0.28, cy = Math.sin(ang)*Math.min(W,H)*0.28;
  return Object.assign({}, n, {
    x: cx + (r()-0.5)*160, y: cy + (r()-0.5)*160, vx:0, vy:0,
    r: 5 + Math.min(14, Math.sqrt(n.callers||0)*2.2 + n.methods.length*0.25) });
});
const byId = {}; nodes.forEach(n=>byId[n.id]=n);
const edges = DATA.edges.filter(e=>byId[e.src]&&byId[e.dst]);
const adj = {}; edges.forEach(e=>{
  (adj[e.src]=adj[e.src]||[]).push(e); (adj[e.dst]=adj[e.dst]||[]).push(e);});

// --- simulation ----------------------------------------------------------
let alpha = 1;
function tick(){
  for(let i=0;i<nodes.length;i++){
    const a=nodes[i];
    for(let j=i+1;j<nodes.length;j++){
      const b=nodes[j];
      let dx=a.x-b.x, dy=a.y-b.y, d2=dx*dx+dy*dy;
      if(d2<1)d2=1; if(d2>90000)continue;
      const f=900/d2*alpha;
      const d=Math.sqrt(d2); dx/=d; dy/=d;
      a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f;
    }
    a.vx-=a.x*0.004*alpha; a.vy-=a.y*0.004*alpha;  // gravity to center
  }
  edges.forEach(e=>{
    const a=byId[e.src], b=byId[e.dst];
    let dx=b.x-a.x, dy=b.y-a.y, d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(d-90)*0.004*alpha*Math.min(3,Math.sqrt(e.w));
    dx/=d; dy/=d;
    a.vx+=dx*f; a.vy+=dy*f; b.vx-=dx*f; b.vy-=dy*f;
  });
  const MAXV=25;
  nodes.forEach(n=>{
    if(n===dragNode)return;
    n.vx=Math.max(-MAXV,Math.min(MAXV,n.vx));
    n.vy=Math.max(-MAXV,Math.min(MAXV,n.vy));
    n.x+=n.vx; n.y+=n.vy; n.vx*=0.6; n.vy*=0.6;
    if(!Number.isFinite(n.x)||!Number.isFinite(n.y)){n.x=0;n.y=0;n.vx=0;n.vy=0;}
  });
  if(alpha>0.02) alpha*=0.985;
}

// --- view transform ------------------------------------------------------
let view = {k:1, tx:0, ty:0};
const toScreen = (x,y)=>[(x*view.k)+W/2+view.tx, (y*view.k)+H/2+view.ty];
const toWorld  = (sx,sy)=>[(sx-W/2-view.tx)/view.k, (sy-H/2-view.ty)/view.k];

// --- interaction ---------------------------------------------------------
let dragNode=null, panning=false, lastM=null, hover=null, selected=null;
let searchTerm='', flowPath=null, flowSet=null, hiddenClusters=new Set();
cv.addEventListener('mousedown', ev=>{
  const n=hit(ev.offsetX, ev.offsetY);
  if(n){dragNode=n; alpha=Math.max(alpha,0.25);} else {panning=true;}
  lastM=[ev.offsetX,ev.offsetY];
});
addEventListener('mouseup', ev=>{
  if(dragNode && lastM && Math.abs(ev.clientX-startClick[0])<4 &&
     Math.abs(ev.clientY-startClick[1])<4){ select(dragNode); }
  else if(panning && startClick && Math.abs(ev.clientX-startClick[0])<4 &&
     Math.abs(ev.clientY-startClick[1])<4){ select(null); }
  dragNode=null; panning=false;
});
let startClick=[0,0];
cv.addEventListener('mousedown', ev=>{startClick=[ev.clientX,ev.clientY];});
cv.addEventListener('mousemove', ev=>{
  const mx=ev.offsetX,my=ev.offsetY;
  if(dragNode){ const [wx,wy]=toWorld(mx,my); dragNode.x=wx; dragNode.y=wy;
    alpha=Math.max(alpha,0.2); }
  else if(panning){ view.tx+=mx-lastM[0]; view.ty+=my-lastM[1]; }
  else { hover=hit(mx,my); cv.style.cursor=hover?'pointer':'default'; }
  lastM=[mx,my];
});
cv.addEventListener('wheel', ev=>{
  ev.preventDefault();
  const f = ev.deltaY<0?1.12:0.89;
  const [wx,wy]=toWorld(ev.offsetX,ev.offsetY);
  view.k*=f;
  const [sx,sy]=toScreen(wx,wy);
  view.tx+=ev.offsetX-sx; view.ty+=ev.offsetY-sy;
},{passive:false});
function hit(sx,sy){
  for(let i=nodes.length-1;i>=0;i--){
    const n=nodes[i]; if(hiddenClusters.has(n.cluster))continue;
    const [x,y]=toScreen(n.x,n.y);
    const r=n.r*view.k+3;
    if((sx-x)**2+(sy-y)**2<r*r) return n;
  } return null;
}

// --- panel ---------------------------------------------------------------
const panel=document.getElementById('panel');
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function select(n){
  selected=n;
  if(!n){panel.style.display='none';return;}
  let h='<div class="kind">'+n.kind+(n.annotations.length?
    ' · <span class="anno">@'+n.annotations.join(' @')+'</span>':'')+'</div>'
    +'<h2>'+esc(n.name)+'</h2>'
    +'<div class="file">'+esc(n.file)+':'+n.line+'</div>'
    +'<div class="sec">'+n.callers+' incoming calls · '
    +n.methods.length+' methods</div>';
  if(n.methods.length) h+='<div class="sec">methods</div><ul>'+
    n.methods.slice(0,18).map(m=>'<li>'+esc(m)+'()</li>').join('')+'</ul>';
  if(n.inTop.length) h+='<div class="sec">called by</div><ul>'+
    n.inTop.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
  if(n.outTop.length) h+='<div class="sec">calls</div><ul>'+
    n.outTop.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>';
  panel.innerHTML=h; panel.style.display='block';
}

// --- header / controls ---------------------------------------------------
const S=DATA.stats;
document.getElementById('stats').textContent =
  S.types+' types · '+S.methods+' methods · '+S.edges+' edges · '
  +S.processes+' flows';
const T=DATA.tokens;
const fmt=t=>t>=1e6?(t/1e6).toFixed(1)+'M':t>=1e3?Math.round(t/1e3)+'k':t;
document.getElementById('badge').textContent =
  '⚡ one query ≈ '+fmt(T.query)+' tokens vs '+fmt(T.repo)
  +' to read the repo — '+T.ratio+'× cheaper'
  +(T.questions?' · saved so far: '+fmt(T.saved)+' tokens ('
    +T.questions+' questions)':'');
const flowSel=document.getElementById('flowSel');
DATA.flows.forEach(f=>{
  const o=document.createElement('option'); o.value=f.id;
  o.textContent=f.id+'  '+f.label; flowSel.appendChild(o);});
flowSel.addEventListener('change',()=>{
  const f=DATA.flows.find(x=>x.id===flowSel.value);
  if(!f){flowPath=null;flowSet=null;select(null);return;}
  flowPath=f.typeSteps; flowSet=new Set();
  for(let i=0;i<f.typeSteps.length-1;i++)
    flowSet.add(f.typeSteps[i]+'>'+f.typeSteps[i+1]);
  panel.innerHTML='<div class="kind">execution flow</div><h2>'+esc(f.label)
    +'</h2><div class="sec">steps</div><ul>'+
    f.steps.map((s,i)=>'<li>'+(i+1)+'. '+esc(s)+'</li>').join('')+'</ul>';
  panel.style.display='block';
});
document.getElementById('search').addEventListener('input',ev=>{
  searchTerm=ev.target.value.toLowerCase();});
document.getElementById('search').addEventListener('keydown',ev=>{
  if(ev.key==='Enter'&&searchTerm){
    const n=nodes.find(n=>n.name.toLowerCase().includes(searchTerm));
    if(n){view.tx=-n.x*view.k; view.ty=-n.y*view.k; select(n);}
  }});
const legend=document.getElementById('legend');
DATA.clusters.forEach(c=>{
  const d=document.createElement('div'); d.className='on';
  d.innerHTML='<i style="background:'+clusterColor(c.id)+'"></i>'
    +esc(c.label)+' ('+c.n+')';
  d.addEventListener('click',()=>{
    if(hiddenClusters.has(c.id)){hiddenClusters.delete(c.id);
      d.className='on';}
    else {hiddenClusters.add(c.id); d.className='';}
  });
  legend.appendChild(d);
});

// --- render --------------------------------------------------------------
const EDGE_COLOR={CALLS:'#3d444d',EXTENDS:'#bb8009',IMPLEMENTS:'#8957e5'};
function neighborsOf(id){
  const s=new Set([id]);
  (adj[id]||[]).forEach(e=>{s.add(e.src);s.add(e.dst);});
  return s;
}
function frame(){
  tick();
  ctx.clearRect(0,0,W,H);
  const showCalls=document.getElementById('tCalls').checked;
  const showInh=document.getElementById('tInh').checked;
  const focus = selected?neighborsOf(selected.id):null;
  // edges
  edges.forEach(e=>{
    if(e.type==='CALLS'&&!showCalls)return;
    if(e.type!=='CALLS'&&!showInh)return;
    const a=byId[e.src], b=byId[e.dst];
    if(hiddenClusters.has(a.cluster)||hiddenClusters.has(b.cluster))return;
    const [x1,y1]=toScreen(a.x,a.y), [x2,y2]=toScreen(b.x,b.y);
    const onFlow=flowSet&&(flowSet.has(e.src+'>'+e.dst)||flowSet.has(e.dst+'>'+e.src));
    let alpha_=0.55, color=EDGE_COLOR[e.type]||'#3d444d',
        w=Math.min(4,0.6+Math.sqrt(e.w)*0.5);
    if(flowSet){alpha_=onFlow?0.95:0.08; if(onFlow){color='#ff5252';w=3;}}
    else if(focus){alpha_=(focus.has(e.src)&&focus.has(e.dst))?0.9:0.07;}
    ctx.globalAlpha=alpha_; ctx.strokeStyle=color; ctx.lineWidth=w;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
  });
  // nodes
  ctx.globalAlpha=1;
  nodes.forEach(n=>{
    if(hiddenClusters.has(n.cluster))return;
    const [x,y]=toScreen(n.x,n.y);
    if(x<-40||y<-40||x>W+40||y>H+40)return;
    const r=Math.max(2.5,n.r*view.k);
    const onFlow=flowPath&&flowPath.includes(n.id);
    const matched=searchTerm&&n.name.toLowerCase().includes(searchTerm);
    let dim = (flowSet&&!onFlow) || (focus&&!focus.has(n.id));
    if(matched)dim=false;
    ctx.globalAlpha=dim?0.15:1;
    ctx.beginPath(); ctx.arc(x,y,r,0,7);
    ctx.fillStyle=clusterColor(n.cluster); ctx.fill();
    if(n.kind==='Interface'){ctx.strokeStyle='#fff';ctx.lineWidth=1.4;
      ctx.setLineDash([3,2]);ctx.stroke();ctx.setLineDash([]);}
    if(matched||n===selected||onFlow){
      ctx.strokeStyle=matched?'#f0d50c':(onFlow?'#ff5252':'#fff');
      ctx.lineWidth=2.2; ctx.beginPath(); ctx.arc(x,y,r+2.5,0,7); ctx.stroke();}
    const showLabel = n===hover||n===selected||matched||onFlow
      ||view.k>1.1||n.callers>=8;
    if(showLabel&&!dim){
      ctx.fillStyle='#e6edf3'; ctx.font='11px sans-serif';
      ctx.fillText(n.name, x+r+4, y+4);}
  });
  ctx.globalAlpha=1;
  requestAnimationFrame(frame);
}
frame();
</script></body></html>
"""


def cmd_viz(root, open_browser=False):
    g = Graph(root)
    payload = _viz_payload(g)
    html = VIZ_HTML.replace("__PROJECT__", payload["project"]) \
                   .replace("__JKG_DATA__", json.dumps(payload,
                                                       separators=(',', ':')))
    out = os.path.join(jkg_path(root), "graph.html")
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print("✓ Interactive graph UI written to %s" % out)
    print("  %d types · %d edges · %d flows · %d areas — open it in any browser"
          % (len(payload["nodes"]), len(payload["edges"]),
             len(payload["flows"]), len(payload["clusters"])))
    if open_browser:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out))


RULES_BLOCK = """<!-- jkg:start -->
# Java Knowledge Graph (jkg)

This project is indexed by **jkg** — a file-based code knowledge graph in
`.jkg/` (plain JSON, no database). Query the graph instead of scanning files.

## Always Do
- **MUST run `python3 {script} impact <symbol>` before editing any class or
  method** and report the blast radius (direct callers, flows, risk) to the user.
- **MUST run `python3 {script} diff` before committing** to verify only
  expected symbols and execution flows are affected.
- **MUST warn the user** on HIGH or CRITICAL risk before proceeding.
- Use `query`/`context`/`flows` to navigate instead of grepping the repo.

## Never Do
- NEVER edit a method without running `impact` on it first.
- NEVER assume the graph is fresh after edits — re-run `analyze` (incremental, fast).
<!-- jkg:end -->
"""


def cmd_init(root):
    cmd_analyze(root)
    script = os.path.abspath(__file__)
    block = RULES_BLOCK.format(script=script)
    for fname in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(root, fname)
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            if '<!-- jkg:start -->' in content:
                content = re.sub(r'<!-- jkg:start -->.*?<!-- jkg:end -->\n?',
                                 block, content, flags=re.S)
            else:
                content = content.rstrip() + "\n\n" + block
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            print("✓ Installed agent rules into %s" % fname)
            return
    with open(os.path.join(root, "CLAUDE.md"), 'w', encoding='utf-8') as f:
        f.write(block)
    print("✓ Created CLAUDE.md with agent rules")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="jkg.py",
                                 description="Java Knowledge Graph — index & "
                                             "query Java code without a database")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("analyze", help="build/refresh the graph (incremental)")
    q = sub.add_parser("query", help="search the graph")
    q.add_argument("text")
    q.add_argument("--top", type=int, default=12)
    q.add_argument("--json", action="store_true")
    c = sub.add_parser("context", help="360° view of a symbol")
    c.add_argument("symbol")
    i = sub.add_parser("impact", help="blast radius + risk (run before editing)")
    i.add_argument("symbol")
    i.add_argument("--direction", choices=["upstream", "downstream"],
                   default="upstream")
    i.add_argument("--depth", type=int, default=3)
    i.add_argument("--json", action="store_true")
    for name in ("callers", "callees"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
    f = sub.add_parser("flows", help="list execution flows")
    f.add_argument("filter", nargs="?")
    fl = sub.add_parser("flow", help="print one flow trace")
    fl.add_argument("id")
    sub.add_parser("clusters", help="functional areas")
    h = sub.add_parser("hierarchy", help="type hierarchy")
    h.add_argument("symbol")
    sub.add_parser("cycles", help="package dependency cycles")
    sub.add_parser("diff", help="what changed since last analyze")
    sub.add_parser("stats", help="graph statistics")
    sub.add_parser("init", help="analyze + install CLAUDE.md rules")
    v = sub.add_parser("viz", help="generate interactive graph UI (.jkg/graph.html)")
    v.add_argument("--open", action="store_true", help="open in browser")
    a = sub.add_parser("ask", help="natural-language question answered from the graph")
    a.add_argument("question")
    sub.add_parser("report", help="architecture health report (.jkg/report.md)")
    sub.add_parser("mcp", help="serve the graph as MCP tools over stdio")

    args = ap.parse_args()
    root = os.path.abspath(args.root)
    if not args.cmd:
        ap.print_help()
        return
    if args.cmd == "analyze":
        cmd_analyze(root)
    elif args.cmd == "query":
        cmd_query(root, args.text, args.top, args.json)
    elif args.cmd == "context":
        cmd_context(root, args.symbol)
    elif args.cmd == "impact":
        cmd_impact(root, args.symbol, args.direction, args.depth, args.json)
    elif args.cmd == "callers":
        cmd_edge_list(root, args.symbol, "in")
    elif args.cmd == "callees":
        cmd_edge_list(root, args.symbol, "out")
    elif args.cmd == "flows":
        cmd_flows(root, args.filter)
    elif args.cmd == "flow":
        cmd_flow(root, args.id)
    elif args.cmd == "clusters":
        cmd_clusters(root)
    elif args.cmd == "hierarchy":
        cmd_hierarchy(root, args.symbol)
    elif args.cmd == "cycles":
        cmd_cycles(root)
    elif args.cmd == "diff":
        cmd_diff(root)
    elif args.cmd == "stats":
        cmd_stats(root)
    elif args.cmd == "init":
        cmd_init(root)
    elif args.cmd == "viz":
        cmd_viz(root, args.open)
    elif args.cmd == "ask":
        cmd_ask(root, args.question)
    elif args.cmd == "report":
        cmd_report(root)
    elif args.cmd == "mcp":
        cmd_mcp(root)


if __name__ == "__main__":
    main()
