from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.database import get_connection, transaction
from app.schemas.business import AffairControlRuleRequest, AffairDecisionRequest
from app.services.affairs import AffairWorkflowService

router = APIRouter(prefix="/api/affair-workflow", tags=["事务职责分离"])


@router.get("/rules")
def list_rules(principal: Principal = Depends(current_principal)) -> list[dict]:
    return AffairWorkflowService(get_connection()).list_rules(principal)


@router.put("/rules/{category}")
def configure_rule(
    category: str,
    data: AffairControlRuleRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    with transaction(immediate=True) as connection:
        return AffairWorkflowService(connection).configure_rule(principal, category, data.is_enabled)


@router.get("/{affair_id}")
def get_affair(affair_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return AffairWorkflowService(get_connection()).detail(principal, affair_id)


@router.post("/{affair_id}/decisions")
def create_decision(
    affair_id: int,
    data: AffairDecisionRequest,
    principal: Principal = Depends(current_principal),
    idempotency_key: str | None = Header(default=None, max_length=200),
) -> JSONResponse:
    payload = data.model_dump()
    with transaction(immediate=True) as connection:
        result = AffairWorkflowService(connection).transition(
            principal,
            affair_id,
            data.target_status,
            department_id=data.department_id,
            result=data.result,
            opinion=data.opinion,
            idempotency_key=(idempotency_key or "").strip() or None,
            payload=payload,
        )
    # 重复点击重放同一决定：返回 200 且不产生新决定
    return JSONResponse(content=result, status_code=200 if result.get("replayed") else 201)
