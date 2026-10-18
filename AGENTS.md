# RenAlly project conventions

## Keep responsibilities separated

- Add shared dependency construction to `src/runtime.py`; do not rebuild the graph, retriever, constraint engine, and candidate generator separately in each entry point.
- Keep request planning, application orchestration, and HTTP transport in `src/web/request_planner.py`, `src/web/service.py`, and `src/web/http_server.py` respectively.
- Put shared nutrient names, aliases, and units in `src/nutrition.py`. Put user-input validation in the appropriate `src/constraints` module.
- Extend the existing domain modules when adding behavior. Avoid parallel one-off implementations in `main.py`, `web_app.py`, or `evaluate.py`.

## Preserve data integrity

- Neo4j is the runtime source of truth for recipes, Foundation Foods, nutrients, and graph relationships. JSON recipe and ingredient metadata files are importer inputs only; do not restore the old in-memory graph as a runtime fallback.
- Keep nutrient arithmetic and explicit constraint checks deterministic.
- Keep units, food state, source IDs, and source URLs with imported nutrient records. Missing values stay missing; do not convert them to zero.
- Daily clinical references are context, not meal limits. Apply a limit only when the user supplies an explicit numeric budget.
- Treat model output as a request plan or wording only. Validate catalog IDs and use retrieved records as the source of factual values.
- Keep illustrative recipe compositions and substitution compatibility clearly labeled as prototype data.

## Bound growth and optional dependencies

- Keep retrieval and candidate generation bounded as catalog size grows; candidate variants currently have a 256-item cap.
- Keep PyTorch and Transformers optional. Import and initialize the local model only when selected, and retain deterministic behavior when it is unavailable.
- Avoid adding a dependency for functionality already covered by the Python standard library.
