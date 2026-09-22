# Differential Privacy Budget Release API

A FastAPI + SQLite service that releases **Laplace-mechanism** noised query
answers while charging a per-dataset ε (epsilon) privacy budget.

- Budget is checked **and deducted inside a single SQLite transaction**
  (`BEGIN IMMEDIATE`), so concurrent requests racing for the last slice of a
  budget serialize — exactly one succeeds, the rest get `402`.
- Noise is **deterministic and reproducible**: derived from an
  HMAC-SHA256 keyed by a server-side secret over
  `(dataset, query, epsilon, sensitivity, idempotency key)`.
- **Idempotent** releases: retrying with the same idempotency key returns the
  *exact same* release (same `id`, same noise, same value) and never charges
  the budget twice. Reusing a key with different parameters returns `409`.
- **Budget transfers:** unused ε quota can be moved from one dataset to another
  with `POST /budgets/transfer`. Both balances are locked and updated in one
  SQLite transaction; transfers are idempotent on `transfer_id`.

## Endpoints

| Method | Path                    | Description                                             |
|--------|-------------------------|---------------------------------------------------------|
| POST   | `/releases`             | Validate budget, deduct ε, return a noised release      |
| POST   | `/budgets/transfer`     | Move unused ε quota from one dataset to another         |
| GET    | `/budgets/{dataset_id}` | Used and remaining budget only                          |
| GET    | `/health`               | Health check                                            |
| GET    | `/docs`                 | Interactive Swagger UI                                  |

### `POST /releases`

Request body:

```json
{
  "dataset_id": "sales",
  "query_id": "daily-revenue-2026-09-22",
  "epsilon": 0.1,
  "sensitivity": 1.0,
  "true_value": 12345.67,
  "idempotency_key": "client-uuid-7f3a..."
}
```

You may also pass the key via the `Idempotency-Key` header; if both are sent
they must match (otherwise `409`).

`200` response:

```json
{
  "id": "d6e2db1798ca4ff78c293ae2e00ded34",
  "idempotency_key": "client-uuid-7f3a...",
  "dataset_id": "sales",
  "query_id": "daily-revenue-2026-09-22",
  "epsilon": 0.1,
  "sensitivity": 1.0,
  "released_value": 12340.2175,
  "noise": -5.4525,
  "budget_used": 0.3,
  "budget_remaining": 0.7
}
```

Status codes:

- `200` — release created, or identical idempotent retry replayed
- `409` — idempotency key already exists with different parameters
- `402` — remaining budget is smaller than the requested ε
- `422` — invalid request (ε/sensitivity ≤ 0, NaN/Infinity, bad id, …)

The released value is `true_value + noise` with
`noise ~ Laplace(0, sensitivity / epsilon)`.

### `GET /budgets/{dataset_id}`

Returns only used and remaining amounts (never released values):

```json
{
  "dataset_id": "sales",
  "used_budget": 0.3,
  "remaining_budget": 0.7
}
```

Unknown datasets report `used_budget = 0` and the configured total as
remaining. After a transfer, `remaining_budget` reflects the quota moved in
or out (only the *unused* part of a budget can ever move — already-spent ε
stays with its dataset).

### `POST /budgets/transfer`

Move unused ε quota from one dataset to another. The source's total drops and
the target's total rises by `epsilon`; used amounts on both sides are
untouched.

Request body:

```json
{
  "transfer_id": "client-uuid-9c1b...",
  "source_dataset": "sales",
  "target_dataset": "marketing",
  "epsilon": 0.2
}
```

`200` response:

```json
{
  "transfer_id": "client-uuid-9c1b...",
  "source_dataset": "sales",
  "target_dataset": "marketing",
  "epsilon": 0.2,
  "source": {
    "dataset_id": "sales",
    "used_budget": 0.3,
    "remaining_budget": 0.5
  },
  "target": {
    "dataset_id": "marketing",
    "used_budget": 0.0,
    "remaining_budget": 1.2
  }
}
```

Status codes:

- `200` — transfer applied, or identical idempotent retry replayed
- `409` — `transfer_id` already exists with different parameters
  (`source_dataset`, `target_dataset` or `epsilon` changed)
- `402` — source has less than `epsilon` unused quota remaining; the body
  identifies the source, the available remainder and the requested amount
- `422` — invalid request: same source and target, `epsilon ≤ 0`,
  NaN/Infinity, bad identifier, extra fields, …

**Idempotency.** Retrying with the same `transfer_id` *and the exact same
parameters* returns the original transfer result (balances frozen at the time
of the first transfer are echoed back) without moving any additional quota.
Reusing a `transfer_id` with changed parameters returns `409` and moves
nothing.

**Atomicity.** Both budget rows are seeded (for unknown datasets) and then
read and updated inside a single `BEGIN IMMEDIATE` transaction. If the source
cannot cover the transfer, the whole request rolls back; concurrent
transfers serialize on SQLite's write lock, so quota is never double-spent.

## Quick start with Docker Compose

Requirements: Docker with the Compose plugin.

```bash
# optional: set a persistent server key (otherwise one is generated
# automatically and stored in the data volume)
export DP_SERVER_KEY=$(openssl rand -hex 32)

docker compose up --build -d
```

The API is then served at:

- **Base URL:** `http://localhost:8000`
- **Health check:** `http://localhost:8000/health`
- **Swagger UI:** `http://localhost:8000/docs`
- **Port:** `8000` (host and container)

Logs and lifecycle:

```bash
docker compose logs -f
docker compose down            # keep data
docker compose down -v         # also delete the database volume
```

The SQLite database (`dp.db`) and the auto-generated key (`server.key`,
mode `0600`) live in the named volume `dp-data` mounted at `/data`.

## Configuration

All configuration is via environment variables:

| Variable             | Default      | Description                                                            |
|----------------------|--------------|------------------------------------------------------------------------|
| `DP_SERVER_KEY`      | *(generated)*| HMAC key for deterministic noise. Set it for stable noise across rebuilds. Generate: `openssl rand -hex 32`. |
| `DP_TOTAL_BUDGET`    | `1.0`        | Total ε budget every dataset receives by default.                      |
| `DP_DATASET_BUDGETS`| *(empty)*    | Per-dataset overrides, e.g. `sales:0.5,users:2.0`; others fall back to `DP_TOTAL_BUDGET`. |
| `DATABASE_PATH`      | `/data/dp.db`| SQLite database file path.                                             |
| `APP_HOST`           | `0.0.0.0`    | Bind address.                                                          |
| `APP_PORT`           | `8000`       | Listen port.                                                           |

Copy `.env.example` to `.env` and adjust — Compose picks it up automatically.

> **Key management note:** if `DP_SERVER_KEY` is unset, a random 32-byte key
> is generated once and persisted next to the database. Deleting the volume
> (or running without one) rotates the key and changes future noise, so set
> `DP_SERVER_KEY` explicitly for production.

## Local development (without Docker)

Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_PATH=./data/dp.db DP_TOTAL_BUDGET=1.0 DP_SERVER_KEY=dev-key \
  .venv/bin/uvicorn app.main:app --reload --port 8000
```

Run the test suite (starts a real uvicorn server on port 8765 against a
temporary database, including concurrency races):

```bash
.venv/bin/python -m unittest tests.test_api -v
```

## Try it

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/releases \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"sales","query_id":"q1","epsilon":0.3,
       "sensitivity":1.0,"true_value":42.0,"idempotency_key":"abc-123"}'

# repeat the exact request -> byte-identical response, budget charged once

curl -s http://localhost:8000/budgets/sales

# move 0.2 of sales' unused quota to marketing
curl -s -X POST http://localhost:8000/budgets/transfer \
  -H 'Content-Type: application/json' \
  -d '{"transfer_id":"tr-001","source_dataset":"sales",
       "target_dataset":"marketing","epsilon":0.2}'

# retrying with the same transfer_id returns the original result, moves nothing

curl -s http://localhost:8000/budgets/marketing
```

## Design notes

- **Concurrency:** each release runs in `BEGIN IMMEDIATE`, which takes
  SQLite's reserved lock at transaction start. Concurrent writers queue on
  `busy_timeout` and each reads the committed balance, so the total deducted
  never exceeds the configured budget.
- **Deterministic noise:** `u ∈ (0,1)` is derived from
  `HMAC-SHA256(key, "dp-release-v1" ␟ dataset ␟ query ␟ ε ␟ sensitivity ␟ key)`
  (56 bits), then inverted through the Laplace CDF. Same inputs ⇒ identical
  float noise, independent of the database or request timing.
- **Idempotency:** `idempotency_key` has a `UNIQUE` constraint. Retries replay
  the stored row; the lost side of a same-key insert race rolls back and
  replays the winner instead of double-charging. Transfers follow the same
  pattern on `transfers.id` (`transfer_id`): identical retries replay the
  stored balances, changed parameters get `409`.
- **Budget transfers:** unused quota is moved by adjusting the two rows'
  `total` columns inside one `BEGIN IMMEDIATE` transaction (`used` never
  changes, so ε already spent cannot be moved out). Budget rows are seeded in
  a fixed alphabetical order so crossing transfers always acquire locks in the
  same order and cannot deadlock. A release never overwrites a budget row
  created by a transfer — the configured total only seeds a brand-new dataset.
- **Privacy:** budget accounting is basic ε-sum bookkeeping with no
  composition niceties; release values are returned to the caller (this is a
  release service), while the budget endpoint exposes amounts only.
