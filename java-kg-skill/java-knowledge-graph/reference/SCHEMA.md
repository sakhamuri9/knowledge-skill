# jkg Graph Schema (v1)

Everything lives in `<repo>/.jkg/` as plain JSON — no database, no native
deps, trivially diffable and portable.

```
.jkg/
├── graph.json        # the knowledge graph (nodes, edges, clusters, processes)
└── parse-cache.json  # per-file parse results keyed by SHA-1 (enables incremental analyze)
```

## graph.json

```jsonc
{
  "schema": 1,
  "indexedAt": "2026-06-10T10:04:15",
  "root": "/abs/path/to/repo",
  "stats": { "files": 42, "types": 47, "methods": 173, "nodes": 286,
             "edges": 576, "clusters": 8, "processes": 10 },
  "nodes":     [ /* see Node */ ],
  "edges":     [ /* see Edge */ ],
  "clusters":  [ /* see Cluster */ ],
  "processes": [ /* see Process */ ]
}
```

## Node

| Field | Notes |
|-------|-------|
| `id` | Types: FQN (`com.shop.service.OrderService`). Methods: `FQN#name/arity` (`…OrderService#placeOrder/1`). Constructors use `<init>`. Fields: `FQN.fieldName`. |
| `kind` | `Class` · `Interface` · `Enum` · `Record` · `Method` · `Constructor` · `Field` |
| `name` | Simple name |
| `file`, `line` | Location (file is repo-relative) |
| `owner` | Methods/fields only: FQN of the declaring type |
| `pkg` | Types only: package |
| `annotations` | e.g. `["RestController"]` — drives entry-point detection |
| `public`, `static`, `abstract`, `arity` | Methods only |

## Edge

`{ "src", "dst", "type", "conf", "reason" }`

| Type | Meaning | Confidence |
|------|---------|------------|
| `CALLS` | method → method (incl. constructor calls) | 0.9 this/same-class, 0.8 var-typed receiver, 0.6 dynamic dispatch, 0.5 unique-name fallback, 0.35 name-candidate |
| `INSTANTIATES` | method → type (`new T()` where T has no declared ctor) | 0.95 |
| `EXTENDS` / `IMPLEMENTS` | type → type | 1.0 |
| `OVERRIDES` | method → supertype method | 0.85 |
| `HAS_METHOD` / `HAS_FIELD` | type → member | 1.0 |

**Dynamic dispatch:** a call to an interface/abstract method also emits
0.6-confidence `CALLS` edges to each override in subtypes (≤6 impls), with
`reason: "dynamic-dispatch via <Interface>"`. This is what makes impact
analysis work through dependency-injection seams.

## Cluster (functional area)

Deterministic label propagation over the type-level call/inheritance graph;
labels from the most frequent camelCase tokens of member names.

`{ "id": "C0", "label": "OrderService", "members": [fqns], "keywords": [],
   "cohesion": 0.6 }`

## Process (execution flow)

Longest call chain from an entry point, capped at depth 10 / branching 4 /
75 processes, minimum 3 steps, deduplicated by (entry, terminal).

`{ "id": "P1", "label": "OrderController.cancel -> Notifier.send",
   "entry": id, "terminal": id, "steps": [node ids in order],
   "stepCount": 4, "type": "cross_cluster"|"intra_cluster",
   "reason": "@DeleteMapping"|"main"|"entry", "clusters": ["C0","C1"] }`

Entry-point detection: `static main` · method annotations (`@GetMapping`,
`@PostMapping`, `@RequestMapping`, `@Scheduled`, `@KafkaListener`,
`@EventListener`, `@PostConstruct`, JAX-RS `@GET/@POST/@Path`, …) · public
methods of `@RestController`/`@Controller` classes · public zero-caller
methods with fan-out (lowest priority). Test files and `@Test` methods are
excluded.

## Machine output

`query --json` and `impact --json` emit structured JSON for programmatic
use. The impact payload includes `risk`, `directCount`, `totalAffected`,
`byDepth`, `affectedProcesses`, `affectedAreas`, and
`epistemic: "complete" | "lower-bound"`.
