from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.models import AffairCategory
from app.schemas.business import AffairReviewDecisionRequest, AffairReviewPolicyRequest, AffairSubmitRequest
from app.services.affairs import AffairWorkflowService
from app.services.idempotency import IdempotencyService

router = APIRouter(prefix="/api/affairs", tags=["事务职责分离"])


def _run_idempotent(scope: str, key: str | None, payload: dict, operation) -> dict:
    with transaction(immediate=True) as connection:
        idempotency = IdempotencyService(connection)
        if key:
            stored = idempotency.lookup(scope, key, payload)
            if stored is not None:
                return {**stored.body, "idempotent_replayed": True}
        body = operation(connection)
        if key:
            idempotency.save(scope, key, payload, body, 200)
        return body


@router.get("/review-policies")
def list_policies(principal: Principal = Depends(current_principal)) -> list[dict]:
    return AffairWorkflowService(get_connection()).list_policies(principal)


@router.put("/review-policies/{category}")
def set_policy(
    category: AffairCategory,
    data: AffairReviewPolicyRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AffairWorkflowService(connection).set_policy(
            principal, category.value, data.is_active, data.note
        )


@router.get("/{affair_id}")
def affair_detail(affair_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("affairs.read")
    return AffairWorkflowService(get_connection()).detail(affair_id)


@router.post("/{affair_id}/submit")
def submit_for_review(
    affair_id: int,
    data: AffairSubmitRequest,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, max_length=200),
) -> dict:
    payload = {
        "user_id": principal.user_id,
        "result": data.result,
        "department_id": data.department_id,
    }
    return _run_idempotent(
        f"affair.submit:{affair_id}",
        idempotency_key,
        payload,
        lambda connection: AffairWorkflowService(connection).submit(
            principal, affair_id, data.result, data.department_id, idempotency_key
        ),
    )


@router.post("/{affair_id}/review")
def review_affair(
    affair_id: int,
    data: AffairReviewDecisionRequest,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, max_length=200),
) -> dict:
    payload = {"user_id": principal.user_id, "approved": data.approved, "opinion": data.opinion}
    return _run_idempotent(
        f"affair.review:{affair_id}",
        idempotency_key,
        payload,
        lambda connection: AffairWorkflowService(connection).review(
            principal, affair_id, data.approved, data.opinion, idempotency_key
        ),
    )
