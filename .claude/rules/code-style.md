# Code Style Rules

## Python
- Python 3.11+
- Type hints on every function signature
- Docstring on every function: what it does, inputs, outputs, side effects
- async/await throughout backend and inference layer
- f-strings only (no .format(), no % formatting)
- Pydantic v2 for all schemas

## Naming
- Files: snake_case
- Classes: PascalCase
- Functions/variables: snake_case
- Constants: UPPER_SNAKE_CASE
- DB tables: snake_case plural (stock_movements, agent_actions)
- Enums: PascalCase class, UPPER_SNAKE_CASE values

## Imports Order
1. Standard library
2. Third-party (fastapi, sqlalchemy, xgboost...)
3. Internal (ml., backend.)
Blank line between each group.

## Error Handling
Every async function that touches DB or S3: wrap in try/except
Log the error with logger.error(f"[{depot_id}] {context}: {e}")
Never raise a bare Exception — always include context

## Notebooks (.ipynb)
- First cell: purpose, dataset used, expected output
- One section per logical step: Load → Clean → Features → Train → Evaluate → Save
- Final cell: print the metric (MAPE, AUC) and confirm pass/fail vs target
- Always save model via ml/registry/model_store.py, never joblib.dump() directly

## Logging
logger = logging.getLogger("flowsync.{module}")
Format: [depot_id] context: message
Always log counts: "Demand predictions written: 47 products"