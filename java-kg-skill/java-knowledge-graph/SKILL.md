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

Answer questions about a Java codebase from a pre-built knowledge graph.

```bash
python3 {SKILL_DIR}/scripts/jkg.py --root <repo> <command>
```

`--root` defaults to cwd. Pure Python 3 stdlib. If `.jkg/graph.json` is
missing or files changed since, run `analyze` first (incremental, fast).

## Cost discipline (read this first)

Every command is a tool round-trip, and each round-trip re-sends the whole
conversation — **chained commands cost more than the graph saves**. Rules:

1. **One question → one command.** Pick the single command that answers it;
   never run query → context → flows → flow as a pipeline.
2. **"Explain this codebase / the flow"** → `overview` (one call: areas,
   flows, main flow traced step-by-step, hotspots). Answer from it directly.
3. **A specific question** → `ask "<question>"` (one call — routes to
   impact/flow/context internally and prints the answer).
4. **Small repo (≲30 files) and no edit planned?** Skip the graph; read the
   files. The graph pays off on large repos and for impact/diff safety.
5. **Read line ranges, not files.** Graph output prints methods as
   `File.java:178-245` — Read with that offset/limit. Enterprise files are
   often 2000+ lines; never read one whole to see one method.
6. `report` and `viz` only when the user explicitly asks for a health
   report or visualization — `overview` already covers architecture
   questions.

## Commands

| Command | Use for |
|---------|---------|
| `analyze` | Build/refresh the graph (incremental; multi-core on big repos) |
| `overview` | **Default first call** — areas, flows, main flow steps, hotspots |
| `ask "<question>"` | NL question → routed answer ("what breaks if…", "how does X work?") |
| `impact <symbol> [--json]` | Blast radius + risk — **required before editing a symbol** |
| `diff` | Changed symbols/flows since last analyze — **run before committing** |
| `flow <id\|name>` / `flows [filter]` | Step-by-step trace / list of execution flows |
| `query "<concept>"` · `context <symbol>` · `callers/callees <symbol>` | Targeted lookups |
| `hierarchy <Type>` · `clusters` · `cycles` · `stats` · `report` | Structure & health |
| `viz [--open]` | Self-contained interactive graph UI (`.jkg/graph.html`) |
| `mcp` | Serve the graph as MCP tools over stdio |

`<symbol>` = simple name, `Class.method`, or FQN. Ambiguity prints candidates.

## Editing workflow

1. `impact X` → report risk + direct callers; **warn the user and pause on
   HIGH/CRITICAL** before editing.
2. Edit, then `analyze` (incremental refresh).
3. Before commit: `diff` → confirm only expected symbols/flows changed.

## Reading the output

- **Risk:** LOW <5 direct callers · MEDIUM ≥5 · HIGH ≥15 (or ≥3 flows/areas)
  · CRITICAL ≥30 (or ≥5 flows/areas). d=1 = will break, d=2 likely, d=3 test.
- **Confidence:** 1.0 inheritance/instantiation · 0.8–0.9 type-resolved ·
  0.75 single-impl dispatch · 0.5–0.7 multi-impl dispatch / async topic ·
  ≤0.5 name-fallback (verify before relying on it).
- **`◬` epistemic note:** target behind an interface — results are a lower
  bound; say so when reporting.

## What the graph covers (big-repo accuracy)

Flows survive the boundaries that break naive call tracing: interface →
implementation dispatch (`DISPATCHES_TO`, transitive through abstract
classes), Lombok and Spring Data generated methods (synthesized so
`order.getId()` / `repo.save()` resolve), Kafka/JMS/Rabbit/SQS async hops
(`PUBLISHES_TO`, matched by topic), external library calls (`USES_EXTERNAL`
marks where execution leaves the repo), and duplicate class names across
modules (package-proximity resolution). See `reference/SCHEMA.md` for the
full schema — read it only if querying `graph.json` directly.
