"""FastAPI application: differential-privacy budget release service."""
from __future__ import annotations

import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import connect, immediate_transaction, init_db
from .privacy import derive_laplace_noise
from .schemas import (
    BudgetResponse,
    HealthResponse,
    ReleaseRequest,
    ReleaseResponse,
    TransferRequest,
    TransferResponse,
)
from .security import resolve_server_key

app = FastAPI(
    title="Differential Privacy Budget Release API",
    version="1.0.0",
    description="Charges epsilon budgets transactionally, transfers unused "
    "budget between datasets, and releases Laplace-noised aggregates with "
    "idempotent retries.",
)

# Resolved once at startup: env-supplied key, or a persisted random key.
_server_key: bytes = b""

# Fields that must be identical when an idempotency key is reused.
_REPLAY_FIELDS = (
    "dataset_id",
    "query_id",
    "epsilon",
    "sensitivity",
    "true_value",
)

# Fields that must be identical when a transfer id is reused.
_TRANSFER_REPLAY_FIELDS = (
    "source_dataset",
    "target_dataset",
    "epsilon",
)

# Float tolerance when comparing a retry against the stored release.
_EPSILON_COMPARE_TOL = 1e-12
# Tolerance covers binary float rounding when a transfer exactly exhausts
# the remaining budget.
_BALANCE_TOL = 1e-9


class _ReplayRelease(Exception):
    """Raised inside a transaction when a same-key race must be replayed."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row


class _ReplayTransfer(Exception):
    """Raised inside a transaction when a same-id transfer race replays."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row


@app.on_event("startup")
def _startup() -> None:
    global _server_key
    init_db()
    _server_key = resolve_server_key()


@app.exception_handler(RequestValidationError)
def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The default handler echoes rejected values in "ctx"/"input", which can
    # contain NaN/Infinity and crash strict JSON rendering; drop ctx and
    # replace non-finite floats with their textual token.
    def _sanitize(value):  # type: ignore[no-untyped-def]
        if isinstance(value, float):
            if value != value:
                return "NaN"
            if value == float("inf"):
                return "Infinity"
            if value == float("-inf"):
                return "-Infinity"
            return value
        if isinstance(value, dict):
            return {k: _sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sanitize(v) for v in value]
        return value

    safe_errors = []
    for error in exc.errors():
        error = {k: v for k, v in error.items() if k != "ctx"}
        safe_errors.append(_sanitize(error))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": safe_errors},
    )


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _row_to_release(row: sqlite3.Row) -> ReleaseResponse:
    return ReleaseResponse(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        dataset_id=row["dataset_id"],
        query_id=row["query_id"],
        epsilon=row["epsilon"],
        sensitivity=row["sensitivity"],
        released_value=row["released_value"],
        noise=row["noise"],
        budget_used=row["budget_used"],
        budget_remaining=row["budget_remaining"],
    )


def _row_to_transfer(row: sqlite3.Row) -> TransferResponse:
    return TransferResponse(
        id=row["id"],
        source_dataset=row["source_dataset"],
        target_dataset=row["target_dataset"],
        epsilon=row["epsilon"],
        source_remaining_before=row["source_remaining_before"],
        source_remaining_after=row["source_remaining_after"],
        target_remaining_before=row["target_remaining_before"],
        target_remaining_after=row["target_remaining_after"],
    )


def _stored_matches(
    row: sqlite3.Row, fields: tuple[str, ...], values: dict[str, object]
) -> bool:
    for field in fields:
        stored = row[field]
        incoming = values[field]
        if isinstance(stored, float):
            if abs(stored - incoming) > _EPSILON_COMPARE_TOL * max(
                1.0, abs(stored), abs(incoming)
            ):
                return False
        elif stored != incoming:
            return False
    return True


def _params_match(row: sqlite3.Row, req: ReleaseRequest) -> bool:
    return _stored_matches(row, _REPLAY_FIELDS, vars(req))


def _transfer_params_match(row: sqlite3.Row, req: TransferRequest) -> bool:
    return _stored_matches(row, _TRANSFER_REPLAY_FIELDS, vars(req))


def _idempotency_key(request: Request, payload: ReleaseRequest) -> str:
    """Prefer the Idempotency-Key header, fall back to the request body."""
    header = request.headers.get("Idempotency-Key", "").strip()
    if header:
        if header != payload.idempotency_key:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key header does not match idempotency_key in the body",
            )
        return header
    return payload.idempotency_key


@app.post(
    "/releases",
    response_model=ReleaseResponse,
    status_code=status.HTTP_200_OK,
    tags=["releases"],
)
def create_release(
    payload: ReleaseRequest, request: Request
) -> ReleaseResponse:
    idem_key = _idempotency_key(request, payload)
    total_budget = settings.budget_for(payload.dataset_id)

    try:
        with immediate_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM releases WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
            if existing is not None:
                if not _params_match(existing, payload):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key was already used with different parameters",
                    )
                # Identical retry: replay the stored release, no extra charge.
                # (A read-only transaction commits harmlessly on context exit.)
                return _row_to_release(existing)

            # Ensure a budget row exists and lock/freeze it for this transaction.
            # ON CONFLICT DO NOTHING keeps any regranted total (transferred in
            # via /budgets/transfer) intact.
            conn.execute(
                "INSERT INTO budgets (dataset_id, total, used) VALUES (?, ?, 0) "
                "ON CONFLICT(dataset_id) DO NOTHING",
                (payload.dataset_id, total_budget),
            )
            budget = conn.execute(
                "SELECT total, used FROM budgets WHERE dataset_id = ?",
                (payload.dataset_id,),
            ).fetchone()
            used = float(budget["used"])
            total = float(budget["total"])
            new_used = used + payload.epsilon
            # Tolerance covers binary float rounding when the request exactly
            # exhausts the remaining budget.
            if new_used > total + 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "error": "insufficient_privacy_budget",
                        "dataset_id": payload.dataset_id,
                        "total_budget": total,
                        "used_budget": used,
                        "remaining_budget": total - used,
                        "requested_epsilon": payload.epsilon,
                    },
                )

            noise = derive_laplace_noise(
                key=_server_key,
                dataset_id=payload.dataset_id,
                query_id=payload.query_id,
                epsilon=payload.epsilon,
                sensitivity=payload.sensitivity,
                idempotency_key=idem_key,
            )
            released_value = payload.true_value + noise
            remaining = total - new_used
            release_id = uuid.uuid4().hex

            conn.execute(
                "UPDATE budgets SET used = ? WHERE dataset_id = ?",
                (new_used, payload.dataset_id),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO releases (
                        id, idempotency_key, dataset_id, query_id, epsilon,
                        sensitivity, true_value, noise, released_value,
                        budget_used, budget_remaining
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release_id,
                        idem_key,
                        payload.dataset_id,
                        payload.query_id,
                        payload.epsilon,
                        payload.sensitivity,
                        payload.true_value,
                        noise,
                        released_value,
                        new_used,
                        remaining,
                    ),
                )
            except sqlite3.IntegrityError:
                # Lost a race against another transaction with the same key.
                winner = conn.execute(
                    "SELECT * FROM releases WHERE idempotency_key = ?",
                    (idem_key,),
                ).fetchone()
                if winner is not None and _params_match(winner, payload):
                    # Same request won: roll back our duplicate deduction
                    # (context exit rolls back) and replay the winner.
                    raise _ReplayRelease(winner)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key was already used with different parameters",
                )

            stored = conn.execute(
                "SELECT * FROM releases WHERE id = ?", (release_id,)
            ).fetchone()
            return _row_to_release(stored)
    except _ReplayRelease as replay:
        return _row_to_release(replay.row)


@app.post(
    "/budgets/transfer",
    response_model=TransferResponse,
    status_code=status.HTTP_200_OK,
    tags=["budgets"],
)
def transfer_budget(payload: TransferRequest) -> TransferResponse:
    if payload.source_dataset == payload.target_dataset:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "same_source_and_target",
                "source_dataset": payload.source_dataset,
                "target_dataset": payload.target_dataset,
            },
        )

    try:
        with immediate_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM transfers WHERE id = ?",
                (payload.transfer_id,),
            ).fetchone()
            if existing is not None:
                if not _transfer_params_match(existing, payload):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="transfer_id was already used with different parameters",
                    )
                # Identical retry: replay the stored transfer, balances untouched.
                return _row_to_transfer(existing)

            # Materialise both budget rows at their configured defaults so the
            # transfer is the single source of truth for balance changes.
            for dataset_id in (payload.source_dataset, payload.target_dataset):
                conn.execute(
                    "INSERT INTO budgets (dataset_id, total, used) VALUES (?, ?, 0) "
                    "ON CONFLICT(dataset_id) DO NOTHING",
                    (dataset_id, settings.budget_for(dataset_id)),
                )
            # Read both rows under the reserved write lock so no concurrent
            # transfer can change them mid-transaction (SQLite allows a single
            # writer, hence no cross-row deadlock).
            source = conn.execute(
                "SELECT total, used FROM budgets WHERE dataset_id = ?",
                (payload.source_dataset,),
            ).fetchone()
            target = conn.execute(
                "SELECT total, used FROM budgets WHERE dataset_id = ?",
                (payload.target_dataset,),
            ).fetchone()

            source_remaining = float(source["total"]) - float(source["used"])
            target_remaining = float(target["total"]) - float(target["used"])
            # Tolerance covers binary float rounding when the transfer exactly
            # exhausts the remaining budget.
            if payload.epsilon > source_remaining + _BALANCE_TOL:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "error": "insufficient_transferable_budget",
                        "source_dataset": payload.source_dataset,
                        "target_dataset": payload.target_dataset,
                        "source_remaining_budget": source_remaining,
                        "requested_epsilon": payload.epsilon,
                    },
                )

            source_after = source_remaining - payload.epsilon
            target_after = target_remaining + payload.epsilon
            conn.execute(
                "UPDATE budgets SET total = total - ? WHERE dataset_id = ?",
                (payload.epsilon, payload.source_dataset),
            )
            conn.execute(
                "UPDATE budgets SET total = total + ? WHERE dataset_id = ?",
                (payload.epsilon, payload.target_dataset),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO transfers (
                        id, source_dataset, target_dataset, epsilon,
                        source_remaining_before, source_remaining_after,
                        target_remaining_before, target_remaining_after
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.transfer_id,
                        payload.source_dataset,
                        payload.target_dataset,
                        payload.epsilon,
                        source_remaining,
                        source_after,
                        target_remaining,
                        target_after,
                    ),
                )
            except sqlite3.IntegrityError:
                # Lost a race against another transaction with the same id.
                winner = conn.execute(
                    "SELECT * FROM transfers WHERE id = ?",
                    (payload.transfer_id,),
                ).fetchone()
                if winner is not None and _transfer_params_match(winner, payload):
                    raise _ReplayTransfer(winner)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="transfer_id was already used with different parameters",
                )

            stored = conn.execute(
                "SELECT * FROM transfers WHERE id = ?",
                (payload.transfer_id,),
            ).fetchone()
            return _row_to_transfer(stored)
    except _ReplayTransfer as replay:
        return _row_to_transfer(replay.row)


@app.get(
    "/budgets/{dataset_id}",
    response_model=BudgetResponse,
    tags=["budgets"],
)
def get_budget(dataset_id: str) -> BudgetResponse:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT total, used FROM budgets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        # Never-seen dataset: configured default total, nothing spent or moved.
        total = settings.budget_for(dataset_id)
        used = 0.0
    else:
        total = float(row["total"])
        used = float(row["used"])
    return BudgetResponse(
        dataset_id=dataset_id,
        used_budget=used,
        remaining_budget=total - used,
    )
