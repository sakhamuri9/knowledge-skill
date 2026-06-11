# jkg Graph Schema (v3)

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
  "schema": 3,
  "indexedAt": "2026-06-11T10:04:15",
  "root": "/abs/path/to/repo",
  "stats": { "files": 42, "types": 47, "methods": 173, "nodes": 286,
             "edges": 576, "clusters": 8, "processes": 10, "repoBytes": 91000 },
  "nodes":     [ /* see Node */ ],
  "edges":     [ /* see Edge */ ],
  "clusters":  [ /* see Cluster */ ],
  "processes": [ /* see Process */ ]
}
```

## Node

| Field | Notes |
|-------|-------|
| `id` | Types: FQN (`com.shop.service.OrderService`). Methods: `FQN#name/arity` (`…OrderService#placeOrder/1`). Constructors use `<init>`. Fields: `FQN.fieldName`. External library types: `ext:<FQN>`. |
| `kind` | `Class` · `Interface` · `Enum` · `Record` · `Method` · `Constructor` · `Field` · `External` |
| `name` | Simple name |
| `file`, `line` | Location (file is repo-relative; empty for `External`) |
| `owner` | Methods/fields only: FQN of the declaring type |
| `pkg` | Types only: package |
| `annotations` | e.g. `["RestController"]` — drives entry-point detection |
| `public`, `static`, `abstract`, `arity` | Methods only. Types also carry `abstract` (abstract classes and all interfaces). |
| `synthetic` | `"lombok"` (generated getters/setters/builders/ctors) or `"spring-data"` (CRUD methods inherited from `JpaRepository`/`CrudRepository`/… base interfaces) |
| `topics` | Listener methods only: topic/queue/destination keys from `@KafkaListener`, `@JmsListener`, `@RabbitListener`, `@SqsListener` |

## Edge

`{ "src", "dst", "type", "conf", "reason" }`

| Type | Meaning | Confidence |
|------|---------|------------|
| `CALLS` | method → method (incl. constructor calls) | 0.9 this/same-class/static, 0.8 var-typed receiver, 0.75 single-impl dispatch, 0.5 multi-impl dispatch, 0.5 unique-name fallback, 0.35 name-candidate |
| `DISPATCHES_TO` | interface/abstract method → overriding implementation (runtime dispatch) | 0.9 single implementation, 0.6 multiple |
| `PUBLISHES_TO` | producer method (`kafkaTemplate.send`, `jmsTemplate.convertAndSend`, …) → listener method, matched by topic/queue key | 0.7, `reason: "topic:<key>"` |
| `INSTANTIATES` | method → type (`new T()` where T has no declared ctor) | 0.95 |
| `EXTENDS` / `IMPLEMENTS` | type → type | 1.0 |
| `OVERRIDES` | method → supertype method | 0.85 |
| `HAS_METHOD` / `HAS_FIELD` | type → member | 1.0 |
| `USES_EXTERNAL` | method → `ext:` library type (the call leaves the repo here; JDK `java.*` excluded) | 0.9 |

**Dynamic dispatch (big-repo correctness):** every overridden method gets
`DISPATCHES_TO` edges to its implementations, transitively through abstract
bases (interface → abstract class → concrete). Execution-flow tracing and
impact analysis follow these edges, so a layered call chain
(`Controller → ServiceInterface → ServiceImpl → AbstractProcessor →
ConcreteProcessor`) is traced end-to-end instead of dead-ending at the
interface. Callers additionally get direct `CALLS` edges to implementations
when there are ≤8 (0.75 conf for exactly one impl).

**Framework synthesis:** methods that exist only at compile/run time are
materialized so calls to them resolve — Lombok (`@Data`, `@Getter`,
`@Builder`, constructors) and Spring Data repository CRUD
(`save`, `findById`, `findAll`, `deleteById`, … on interfaces extending
`JpaRepository`/`CrudRepository`/`MongoRepository`/reactive variants).
Declared query methods (`findByStatus`) are parsed normally.

**Async messaging:** producer `send`/`convertAndSend`/`publish` calls are
matched to `@KafkaListener`/`@JmsListener`/`@RabbitListener`/`@SqsListener`
methods by topic key (string literal or `CONSTANTS.REFERENCE` text), emitting
`PUBLISHES_TO` edges so flows continue across Kafka/JMS boundaries.

**Name resolution in large repos:** ambiguous simple names (several `Order`
classes across modules) are resolved by package proximity — the candidate
sharing the longest package prefix with the caller wins; genuinely ambiguous
references are dropped rather than guessed.

## Cluster (functional area)

Deterministic label propagation over the type-level call/inheritance graph;
labels from the most frequent camelCase tokens of member names.

`{ "id": "C0", "label": "OrderService", "members": [fqns], "keywords": [],
   "cohesion": 0.6 }`

## Process (execution flow)

Longest call chain from an entry point following `CALLS`, `DISPATCHES_TO`,
and `PUBLISHES_TO` edges. Depth cap 14, branching 4, minimum 3 steps,
deduplicated by (entry, terminal). The process count scales with repo size:
`max(75, methods/20)` capped at 400.

`{ "id": "P1", "label": "OrderController.cancel -> Notifier.send",
   "entry": id, "terminal": id, "steps": [node ids in order],
   "stepCount": 4, "type": "cross_cluster"|"intra_cluster",
   "reason": "@DeleteMapping"|"main"|"entry", "clusters": ["C0","C1"] }`

Entry-point detection: `static main` · method annotations (`@GetMapping`,
`@PostMapping`, `@RequestMapping`, `@Scheduled`, `@KafkaListener`,
`@JmsListener`, `@RabbitListener`, `@SqsListener`, `@EventListener`,
`@PostConstruct`, JAX-RS `@GET/@POST/@Path`, …) · public methods of
`@RestController`/`@Controller` classes · public zero-caller non-abstract
methods with fan-out (lowest priority). Test files and `@Test` methods are
excluded.

## Performance on large repos

`analyze` is incremental (SHA-1 per file). When ≥64 files need re-parsing,
parsing fans out across CPU cores via `multiprocessing` (stdlib only),
falling back to serial on any failure.

## Machine output

`query --json` and `impact --json` emit structured JSON for programmatic
use. The impact payload includes `risk`, `directCount`, `totalAffected`,
`byDepth`, `affectedProcesses`, `affectedAreas`, and
`epistemic: "complete" | "lower-bound"`.
