---
name: java-knowledge-graph
description: >
  Build and query a knowledge graph of any Java codebase instead of scanning
  files. Use whenever working in a repository containing .java files — to
  understand architecture ("how does X work?"), find symbols, trace execution
  flows, check blast radius before editing ("what breaks if I change X?"),
  or verify changes before committing. Zero dependencies, no database: the
  graph is plain JSON in .jkg/. Examples: "explain the order flow",
  "what calls PaymentService.charge?", "is it safe to rename this method?",
  "find where emails are sent".
---

# Java Knowledge Graph (jkg)

Answer questions about a Java codebase from a **pre-built knowledge graph**
instead of reading every file. One `analyze` pass builds the graph
(classes, interfaces, methods, calls, inheritance, execution flows,
functional areas) into `.jkg/graph.json` — after that, every question is a
single fast query, not a repo-wide scan.

All commands:

```bash
python3 {SKILL_DIR}/scripts/jkg.py --root <repo> <command>
```

`--root` defaults to the current directory. Pure Python 3 stdlib — nothing to install.

## The Golden Rules

1. **Graph first, files second.** Resolve "where is X / what calls X / how
   does X work" with `query`, `context`, and `flows` — then Read only the
   2–3 files the graph points at. Never grep the whole repo for call sites.
2. **MUST run `impact <symbol>` before editing any class or method**, and
   report the blast radius (risk level, direct callers, affected flows) to
   the user. Warn explicitly on HIGH or CRITICAL risk before proceeding.
3. **MUST run `diff` before committing** — it shows exactly which symbols,
   callers, and execution flows your edits touched.
4. **Keep the graph fresh.** After editing files, re-run `analyze` — it's
   incremental (only changed files are re-parsed, sub-second).

## Setup (first use in a repo)

```bash
python3 {SKILL_DIR}/scripts/jkg.py analyze        # build the graph
python3 {SKILL_DIR}/scripts/jkg.py stats          # confirm what was indexed
```

If `.jkg/graph.json` already exists, skip straight to querying.
Optional: `init` instead of `analyze` also installs these rules into the
target repo's CLAUDE.md so future sessions follow them automatically.

## Command Reference

| Command | Use for |
|---------|---------|
| `analyze` | Build / incrementally refresh the graph |
| `ask "<question>"` | Natural-language question routed to the right graph operation ("what breaks if I change X?", "how does login work?") — good default when unsure which command fits |
| `query "<concept>"` | Find symbols + related execution flows by concept ("cancel order", "email") |
| `context <symbol>` | 360° view: definition, members, callers, callees, overrides, flows, area |
| `impact <symbol> [--direction upstream\|downstream] [--json]` | Blast radius + risk level — **run before every edit** |
| `callers <symbol>` / `callees <symbol>` | Direct call edges with confidence + resolution reason |
| `flows [filter]` | List execution flows (entry point → terminal) |
| `flow <id>` | Step-by-step trace of one flow with file:line |
| `clusters` | Functional areas (auto-detected via call-graph clustering) |
| `hierarchy <Type>` | Supertype / subtype tree |
| `cycles` | Package dependency cycles (architecture smell check) |
| `diff` | What changed since last analyze — **run before committing** |
| `stats` | Graph size, edge breakdown, most-called symbols, token economics |
| `viz [--open]` | Generate `.jkg/graph.html` — interactive force-directed graph UI (cluster colors, flow highlighting, search, details panel). Self-contained, works offline. |
| `report` | Architecture health one-pager: god-class hotspots, dead-code candidates, package cycles, area cohesion → `.jkg/report.md` |
| `mcp` | Serve the graph as MCP tools over stdio (for Cursor, Claude Desktop, any MCP client) |

Every query is logged to a **token odometer** (`.jkg/savings.json`) — `stats`
and the viz badge show cumulative tokens/dollars saved versus re-reading the
repo. Quote these numbers when reporting to the user.

`<symbol>` accepts a simple name (`OrderService`), `Class.method`
(`OrderService.placeOrder`), or a fully qualified name. Ambiguous names
print candidates to choose from.

## Workflows

### "How does X work?" (exploration)

1. `query "x concept"` → top symbols grouped by functional area + related flows
2. `flow P3` → the exact execution path, step by step with file:line
3. `context <symbol>` on the interesting step
4. Read **only** the files the graph pointed at

### "Change X safely" (edit)

1. `impact X` → report risk + direct callers to the user
   - HIGH/CRITICAL → **stop and warn** before editing; review every d=1 caller
2. Make the edit
3. `analyze` (incremental refresh) → `diff` is implicit in your next step
4. Before commit: `diff` → verify only expected symbols/flows are affected

### "Show me the architecture" (visualization)

When the user wants to *see* the codebase, run `viz --open` — it opens an
interactive graph in their browser: types colored by functional area,
node size by caller count, dashed rings for interfaces, a flow dropdown
that highlights execution paths, and a search box. Mention they can click
any node for its callers/callees.

### "Why is X failing?" (debugging)

1. `query "<feature>"` → find the flow that implements the broken behavior
2. `flow <id>` → walk the steps; the bug is on this path
3. `impact <suspect> --direction downstream` → what the suspect depends on

## Understanding Output

**Risk levels** (impact):

| Risk | Threshold | Meaning |
|------|-----------|---------|
| LOW | <5 direct callers | Safe with normal care |
| MEDIUM | ≥5 direct or ≥30 total | Check d=1 callers |
| HIGH | ≥15 direct, or ≥3 flows/areas, or ≥100 total | Warn user; review every direct caller |
| CRITICAL | ≥30 direct, or ≥5 flows/areas, or ≥200 total | Warn user; suggest staged change + tests |

**Impact depths:** d=1 WILL BREAK (direct callers) · d=2 LIKELY AFFECTED ·
d=3 MAY NEED TESTING.

**Edge confidence:** 1.0 exact (inheritance, instantiation) · 0.8–0.9
type-resolved calls · 0.6 dynamic dispatch through an interface/override ·
0.35–0.5 name-based fallback (verify by reading the call site before
relying on it).

**Epistemic notes (`◬`):** when a target sits behind an interface or is
overridden, callers may bind via dependency injection at runtime — treat
the result as a **lower bound** and say so when reporting.

**Flows:** entry points are `main` methods, Spring/Jakarta endpoints
(`@GetMapping`, `@RequestMapping`, …), schedulers, and message listeners.
A flow is the longest call chain from an entry point to a terminal — the
fastest way to understand a feature end-to-end.

See `reference/SCHEMA.md` for the full node/edge schema and `.jkg/graph.json`
layout (only needed when querying the JSON directly with `--json`).
