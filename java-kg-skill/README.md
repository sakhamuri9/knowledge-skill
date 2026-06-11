# java-knowledge-graph — a code knowledge graph skill for Java repos

## The 30-second pitch

Every question you ask an AI agent about a codebase today triggers the same
wasteful ritual: grep, read, read, read. A 1M-token Java repo costs ~1M input
tokens *per question* — and the agent forgets everything when the context
window rolls over.

**This skill replaces re-reading with remembering.** One `analyze` pass
(0.2s on Spring PetClinic) parses every `.java` file into a **knowledge
graph** — classes, methods, call edges with confidence scores, inheritance,
Spring entry points, execution flows. From then on:

- *"What calls `placeOrder`?"* → **one ~500-token graph query**, not a repo scan
- *"Is this edit safe?"* → risk-scored blast radius, including dynamic
  dispatch through interfaces that grep can never see
- *"Show me the architecture"* → **interactive graph UI** in the browser
- `ask "what breaks if I change Owner.addPet?"` → **natural-language
  questions**, routed to the right graph operation automatically
- a **token odometer** that counts every question answered from the graph
  and totals the tokens & dollars saved — live, in the CLI and the UI badge
- `mcp` mode: the graph becomes **MCP tools for any AI client** — Claude
  Desktop, Cursor, anything. Not a skill for one agent; infrastructure for all
- `report` → an **architecture health one-pager** (god classes, dead code,
  package cycles, cohesion) — on Spring PetClinic it instantly finds the
  project's real, well-known `model ⇄ owner` package cycle

On PetClinic that's **~59× cheaper per question**; on a 10M-token enterprise
monolith it's **~5000×**. The graph is **plain JSON — no database, no
server, no native bindings**, just one zero-dependency Python file. Inspired
by [GitNexus](https://github.com/abhigyanpatwari/GitNexus), distilled into
something you can drop into any repo in 10 seconds.

## The graph UI

`python3 jkg.py viz --open` generates `.jkg/graph.html` — a **fully
self-contained, offline** interactive visualization (vanilla JS canvas,
zero external assets):

- force-directed layout, **types colored by auto-detected functional area**
- node size = how called the type is; dashed ring = interface
- **execution-flow dropdown** — pick a flow and watch the call path light up
  red through the graph, with the step-by-step trace in the side panel
- click any type → callers, callees, methods, annotations, file:line
- live search, pan/zoom, draggable nodes, per-area show/hide legend
- a header badge that shows the judges the money shot: *"one query ≈ 500
  tokens vs 30k to read the repo — 59× cheaper"*

## Why this wins

| | Grep-driven agent | With this skill |
|---|---|---|
| "What calls `placeOrder`?" | Scans every file, misses dynamic dispatch | One graph lookup, includes interface/override dispatch edges |
| "Is this change safe?" | Vibes | `impact` → risk level (LOW→CRITICAL), depth-tagged blast radius, affected flows |
| "How does checkout work?" | Reads 30 files | `flow P2` → the exact 4-step call chain with file:line |
| Pre-commit safety | None | `diff` → changed symbols, their callers, touched execution flows |
| Tokens spent | Entire repo, every question | One JSON query per question |
| Setup | — | `python3 jkg.py analyze` (0.2 s on Spring PetClinic), incremental afterwards |

**Honest about uncertainty:** every edge carries a confidence score and a
resolution reason, and impact results behind interface seams are explicitly
marked *lower bound* — the agent knows what the graph doesn't know.

## Install

Drop the `java-knowledge-graph/` folder into your project's skills directory:

```bash
cp -r java-knowledge-graph /path/to/your-java-repo/.claude/skills/
```

(or `~/.claude/skills/` to enable it for every project). Then in Claude Code:

```
> how does order cancellation work in this repo?
```

The skill auto-triggers on Java codebase questions, builds `.jkg/graph.json`
on first use, and answers from the graph thereafter. Run
`python3 .claude/skills/java-knowledge-graph/scripts/jkg.py init` once to
also install Always/Never rules into the repo's CLAUDE.md (impact-before-edit,
diff-before-commit).

## What gets built

```
.jkg/
├── graph.json        # nodes, edges, clusters, execution flows — plain JSON
└── parse-cache.json  # SHA-1 per file → incremental re-analyze
```

- **Nodes:** Class / Interface / Enum / Record / Method / Constructor / Field
- **Edges:** CALLS (with confidence), EXTENDS, IMPLEMENTS, OVERRIDES,
  INSTANTIATES, HAS_METHOD, HAS_FIELD
- **Execution flows:** traced from `main`, Spring/Jakarta endpoints,
  schedulers, message listeners
- **Functional areas:** deterministic call-graph clustering with
  auto-generated labels and cohesion scores
- **Architecture checks:** package dependency cycle detection

## Demo script (2 minutes)

```bash
git clone --depth 1 https://github.com/spring-projects/spring-petclinic
cd spring-petclinic
JKG=.claude/skills/java-knowledge-graph/scripts/jkg.py

python3 $JKG analyze              # 42 files → graph in 0.2s
python3 $JKG flows                # 10 execution flows, Spring endpoints auto-detected
python3 $JKG flow P2              # step-by-step trace with file:line
python3 $JKG impact OwnerRepository.findById
#   → RISK: HIGH (15 direct callers), epistemic: lower-bound (interface),
#     affected flows listed
python3 $JKG diff                 # after an edit: changed symbols + affected flows
python3 $JKG ask "how does finding an owner work?"   # NL answer: flow trace + files to read
python3 $JKG report               # health report: finds petclinic's real model⇄owner cycle
python3 $JKG stats                # odometer: "10 questions · ~294k tokens saved (≈ $0.88)"
python3 $JKG viz --open           # the wow moment: interactive graph in the browser
```

**MCP setup (works in Cursor / Claude Desktop too):**

```json
{ "mcpServers": { "jkg": {
    "command": "python3",
    "args": ["/abs/path/to/jkg.py", "--root", "/abs/path/to/java-repo", "mcp"]
} } }
```

**Suggested demo arc:** open with the problem (paste a screenshot of an agent
burning 200k tokens grepping), run `analyze` live (sub-second), ask Claude a
question and show it answered from one query, run `impact` on an interface
method to show the HIGH-risk warning + lower-bound honesty, then finish on
`viz --open` with a flow highlighted.

## Design lineage (GitNexus → jkg)

| GitNexus concept | jkg equivalent |
|---|---|
| LadybugDB graph database | plain JSON in `.jkg/` |
| Tree-sitter parsers (20+ languages) | purpose-built Java parser (stdlib regex + brace tracking) |
| Leiden community detection | deterministic label propagation |
| Process detection (depth 10, branching 4, max 75, min 3 steps) | same thresholds |
| Impact risk thresholds (5/15/30 direct, 3/5 flows/areas, 100/200 total) | same thresholds |
| Edge confidence + epistemic lower-bound on interface boundaries | same model |
| `detect_changes()` pre-commit check | `diff` command |
| File-hash incremental indexing | same (SHA-1 parse cache) |
