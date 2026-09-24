from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from app.database import get_connection
from app.models import AffairCreate, AffairProcess, AffairStatus
from app.repositories.business import AffairRepository
from app.services.affairs import AffairWorkflowService

router = APIRouter(prefix="/affairs", tags=["事务办理"])


@router.post("", status_code=201)
def create_affair(affair: AffairCreate):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM residents WHERE id = ?", (affair.applicant_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="申请人不存在")

    cursor.execute(
        """INSERT INTO affairs (title, category, applicant_id, description)
           VALUES (?, ?, ?, ?)""",
        (affair.title, affair.category.value, affair.applicant_id, affair.description)
    )
    conn.commit()
    return {"id": cursor.lastrowid, "message": "事务提交成功"}


@router.get("")
def list_affairs(
    status: Optional[AffairStatus] = None,
    category: Optional[str] = None,
    applicant_id: Optional[int] = None,
    department_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100)
):
    conn = get_connection()
    conditions = []
    params = []
    if status:
        conditions.append("a.status = ?")
        params.append(status.value)
    if category:
        conditions.append("a.category = ?")
        params.append(category)
    if applicant_id:
        conditions.append("a.applicant_id = ?")
        params.append(applicant_id)
    if department_id:
        conditions.append("a.department_id = ?")
        params.append(department_id)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    count_sql = f"SELECT COUNT(*) as total FROM affairs a{where_clause}"
    cursor = conn.cursor()
    cursor.execute(count_sql, params)
    total = cursor.fetchone()["total"]

    offset = (page - 1) * size
    query_sql = f"""SELECT a.*, r.name as applicant_name, d.name as department_name
                    FROM affairs a
                    LEFT JOIN residents r ON a.applicant_id = r.id
                    LEFT JOIN departments d ON a.department_id = d.id
                    {where_clause}
                    ORDER BY a.created_at DESC LIMIT ? OFFSET ?"""
    cursor.execute(query_sql, params + [size, offset])
    rows = cursor.fetchall()

    return {
        "total": total,
        "page": page,
        "size": size,
        "data": [dict(row) for row in rows]
    }


@router.get("/{affair_id}")
def get_affair(affair_id: int):
    # 复用职责分离工作流的详情视图：包含当前责任、待办原因、复核轮次与完整决定历史
    detail = AffairWorkflowService(get_connection()).detail(affair_id)
    return detail


@router.put("/{affair_id}/process")
def process_affair(affair_id: int, data: AffairProcess):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM affairs WHERE id = ?", (affair_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="事务不存在")

    current_status = row["status"]
    new_status = data.status.value

    valid_transitions = {
        "待受理": ["办理中", "已退回"],
        "办理中": ["已办结", "已退回"],
        "待复核": [],
        "已退回": ["待受理"],
        "已办结": []
    }

    if new_status not in valid_transitions.get(current_status, []):
        raise HTTPException(
            status_code=400,
            detail=f"状态不允许从'{current_status}'转换到'{new_status}'"
        )

    # 受控类别禁止通过旧接口绕过经办/复核职责分离直接办结
    if new_status == "已办结":
        cursor.execute("SELECT category FROM affairs WHERE id = ?", (affair_id,))
        category_row = cursor.fetchone()
        if category_row and AffairRepository(get_connection()).is_controlled(category_row["category"]):
            raise HTTPException(
                status_code=409,
                detail="该事务类别已启用职责分离，请通过经办提交与复核接口完成办结"
            )

    if data.department_id is not None:
        cursor.execute("SELECT id FROM departments WHERE id = ?", (data.department_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="承办部门不存在")

    cursor.execute(
        """UPDATE affairs SET status = ?, department_id = COALESCE(?, department_id),
           handler = ?, result = ?, updated_at = datetime('now') WHERE id = ?""",
        (new_status, data.department_id, data.handler, data.result, affair_id)
    )
    conn.commit()
    return {"message": "事务处理成功", "status": new_status}
