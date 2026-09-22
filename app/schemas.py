"""Pydantic request/response models."""
from __future__ import annotations

import re
from typing_extensions import Annotated

from pydantic import BaseModel, Field, StringConstraints

_ID_PATTERN = re.compile(r"^[\w.:@\- ]+$")

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, pattern=_ID_PATTERN),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, pattern=_ID_PATTERN),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class ReleaseRequest(BaseModel):
    dataset_id: Identifier = Field(..., description="Dataset whose privacy budget is consumed")
    query_id: Identifier = Field(..., description="Stable identifier of the query/mechanism")
    epsilon: FiniteFloat = Field(..., gt=0, description="Privacy loss charged for this release")
    sensitivity: FiniteFloat = Field(..., gt=0, description="L1 sensitivity of true_value")
    true_value: FiniteFloat = Field(..., description="Real aggregate before noise is added")
    idempotency_key: IdempotencyKey = Field(
        ..., description="Client-generated key; retries with the same key replay the release"
    )

    model_config = {"extra": "forbid"}


class ReleaseResponse(BaseModel):
    id: str
    idempotency_key: str
    dataset_id: str
    query_id: str
    epsilon: float
    sensitivity: float
    released_value: float
    noise: float
    budget_used: float
    budget_remaining: float


class BudgetResponse(BaseModel):
    dataset_id: str
    used_budget: float
    remaining_budget: float


class HealthResponse(BaseModel):
    status: str
