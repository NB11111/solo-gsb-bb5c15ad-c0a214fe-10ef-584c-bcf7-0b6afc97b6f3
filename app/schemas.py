"""Pydantic request/response models."""
from __future__ import annotations

import re
from typing_extensions import Annotated

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError

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


class TransferRequest(BaseModel):
    transfer_id: IdempotencyKey = Field(
        ..., description="Client-generated key; retries replay the same transfer"
    )
    source_dataset: Identifier = Field(..., description="Dataset whose unused quota is moved out")
    target_dataset: Identifier = Field(..., description="Dataset receiving the quota")
    epsilon: FiniteFloat = Field(..., gt=0, description="Unused epsilon quota moved between datasets")

    @field_validator("target_dataset")
    @classmethod
    def _datasets_must_differ(cls, value: str, info: ValidationInfo) -> str:
        source = info.data.get("source_dataset")
        if source is not None and value == source:
            raise PydanticCustomError(
                "same_source_and_target",
                "target_dataset must differ from source_dataset",
            )
        return value

    model_config = {"extra": "forbid"}


class TransferResponse(BaseModel):
    transfer_id: str
    source_dataset: str
    target_dataset: str
    epsilon: float
    source: BudgetResponse
    target: BudgetResponse


class HealthResponse(BaseModel):
    status: str
