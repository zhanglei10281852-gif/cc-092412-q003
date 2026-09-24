from __future__ import annotations

import sqlite3

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal
from app.repositories.business import AffairRepository
from app.repositories.identity import UserRepository
from app.services.audit import AuditContext, AuditService

# 受控事务的状态机：办理中 -> 待复核 -> 已办结/已退回
CONTROLLED_TRANSITIONS = {
    ("办理中", "待复核"): "经办提交",
    ("待复核", "已办结"): "复核通过",
    ("待复核", "已退回"): "复核退回",
}

RESPONSIBILITY_BY_STATUS = {
    "待受理": ("受理岗", "等待受理并分派承办部门"),
    "办理中": ("经办岗", "等待经办人提交办理结果"),
    "待复核": ("复核岗", "等待与经办人不同的复核人作出复核结论"),
    "已退回": ("经办岗", "复核退回重办，重新提交后将开启新的复核轮次"),
    "已办结": (None, "事务已办结"),
}


class AffairWorkflowService:
    """事务办理工作流，为受控类别提供经办/复核职责分离。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.affairs = AffairRepository(connection)
        self.users = UserRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ---------- 策略配置 ----------

    def list_policies(self, principal: Principal) -> list[dict]:
        principal.require("affairs.read")
        return self.affairs.list_policies()

    def set_policy(self, principal: Principal, category: str, is_active: bool, note: str) -> dict:
        principal.require("affairs.policy.write")
        policy = self.affairs.upsert_policy(category, is_active, note.strip(), to_storage(self.clock.now()))
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="affair.policy.update",
            resource_type="affair_review_policy",
            resource_id=category,
            after={"category": category, "is_active": bool(is_active)},
        )
        return policy

    # ---------- 经办 / 复核 ----------

    def submit(
        self,
        principal: Principal,
        affair_id: int,
        result: str,
        department_id: int | None,
        idempotency_key: str | None,
    ) -> dict:
        affair = self.affairs.require(affair_id)
        self._require_controlled(affair)
        if (affair["status"], "待复核") not in CONTROLLED_TRANSITIONS:
            raise ConflictError(f"当前状态“{affair['status']}”不允许提交复核")
        target_department_id = department_id or affair["department_id"]
        if target_department_id is None:
            raise ValidationError("提交前必须先指定承办部门")
        handler = self._qualified_actor(principal, target_department_id, "affairs.write")
        now = to_storage(self.clock.now())
        try:
            round_no = self.affairs.open_round(affair_id, handler, result, now)
            self._add_decision(
                affair_id=affair_id,
                round_no=round_no,
                stage="handle",
                decision="submitted",
                actor=handler,
                opinion=result,
                idempotency_key=idempotency_key,
                now=now,
            )
            self.connection.execute(
                "UPDATE affairs SET status='待复核',department_id=?,handler=?,result=?,updated_at=? WHERE id=?",
                (target_department_id, handler["name"], result, now, affair_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该事务已存在有效的经办提交决定，重复点击不会再次生效") from exc
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="affair.submit",
            resource_type="affair",
            resource_id=affair_id,
            before={"status": affair["status"], "round": None},
            after={"status": "待复核", "round": round_no},
            metadata={"department_id": target_department_id, "idempotency_key": idempotency_key},
        )
        return self.detail(affair_id)

    def review(
        self,
        principal: Principal,
        affair_id: int,
        approved: bool,
        opinion: str,
        idempotency_key: str | None,
    ) -> dict:
        affair = self.affairs.require(affair_id)
        target_status = "已办结" if approved else "已退回"
        if (affair["status"], target_status) not in CONTROLLED_TRANSITIONS:
            raise ConflictError(f"当前状态“{affair['status']}”不允许复核结论")
        # 策略可能在事务进入待复核后被关闭：在途复核仍须走完职责分离流程，避免事务卡死
        round_row = self.affairs.pending_round(affair_id)
        if round_row is None:
            raise ConflictError("没有等待复核的轮次")
        department_id = affair["department_id"]
        if department_id is None:
            raise ValidationError("事务缺少承办部门，无法复核")

        # 复核人资格：与经办人不同、具备复核权限、且在本部门有有效数据范围
        reviewer = self._qualified_actor(principal, department_id, "affairs.review")
        if round_row["handler_user_id"] is not None and reviewer["user_id"] == round_row["handler_user_id"]:
            raise PermissionDeniedError("经办人与复核人必须为不同人员，不能自行办结")

        # 经办人资格在决定时刻实时复检：权限、账号或部门任期失效都会阻断办结
        handler_blockers = self._eligibility_failures(
            round_row["handler_user_id"], department_id, "affairs.write"
        )
        if handler_blockers:
            raise PermissionDeniedError("经办一方资格已失效，审批被阻断：" + "；".join(handler_blockers))

        decision = "approved" if approved else "returned"
        now = to_storage(self.clock.now())
        try:
            self._add_decision(
                affair_id=affair_id,
                round_no=round_row["round_no"],
                stage="review",
                decision=decision,
                actor=reviewer,
                opinion=opinion,
                idempotency_key=idempotency_key,
                now=now,
            )
            self.affairs.close_round(
                round_row["id"], reviewer, opinion, "approved" if approved else "returned", now
            )
            self.connection.execute(
                "UPDATE affairs SET status=?,updated_at=? WHERE id=?",
                (target_status, now, affair_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该复核轮次已存在有效决定，重复点击不会再次生效") from exc
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="affair.review",
            resource_type="affair",
            resource_id=affair_id,
            before={"status": "待复核", "round": round_row["round_no"]},
            after={"status": target_status, "round": round_row["round_no"]},
            metadata={"decision": decision, "idempotency_key": idempotency_key},
        )
        return self.detail(affair_id)

    # ---------- 详情视图：当前责任、待办原因、完整决定历史 ----------

    def detail(self, affair_id: int) -> dict:
        affair = self.affairs.detail(affair_id)
        if affair is None:
            raise NotFoundError("事务不存在")
        controlled = self.affairs.is_controlled(affair["category"])
        rounds = self.affairs.rounds(affair_id)
        decisions = self.affairs.decisions(affair_id)
        pending = next((item for item in rounds if item["status"] == "pending_review"), None)
        # 存在复核轮次（例如策略在途被关闭）也按受控视图展示历史与当前责任
        show_review_view = controlled or bool(rounds)

        responsibility_role, reason = RESPONSIBILITY_BY_STATUS.get(affair["status"], (None, ""))
        current_responsibility: dict = {
            "stage": affair["status"],
            "role": responsibility_role,
            "reason": reason,
        }
        if pending is not None:
            current_responsibility["round_no"] = pending["round_no"]
            current_responsibility["handler_name"] = pending["handler_name"]

        pending_reasons: list[str] = []
        if show_review_view:
            if controlled:
                pending_reasons.append("该类别受控：办结前必须由不同人员分别完成经办与复核")
            if pending is not None:
                if not controlled:
                    pending_reasons.append("该事务已进入复核流程，策略虽被关闭，仍须完成本轮复核才能办结")
                pending_reasons.append(
                    f"当前为第 {pending['round_no']} 轮复核，等待复核人；经办人不得复核本人提交的事务"
                )
                # 任一方资格失效都直接体现在待办原因中
                if pending["handler_user_id"] is not None:
                    pending_reasons.extend(
                        "经办一方" + item for item in
                        self._eligibility_failures(pending["handler_user_id"], affair["department_id"], "affairs.write")
                    )
            elif affair["status"] == "办理中" and controlled:
                pending_reasons.append("经办人提交办理结果后进入待复核")
        if not pending_reasons:
            pending_reasons.append("普通类别，沿用原有流转")

        affair["controlled"] = controlled
        affair["current_responsibility"] = current_responsibility
        affair["pending_reasons"] = pending_reasons
        affair["review_rounds"] = rounds if show_review_view else []
        affair["decisions"] = decisions if show_review_view else []
        return affair

    # ---------- 内部规则 ----------

    def _require_controlled(self, affair: dict) -> None:
        if not self.affairs.is_controlled(affair["category"]):
            raise ConflictError(f"类别“{affair['category']}”未启用职责分离，沿用原有办理流程")

    def _qualified_actor(self, principal: Principal, department_id: int, permission: str) -> dict:
        if not principal.can(permission):
            raise PermissionDeniedError(f"缺少权限：{permission}")
        # 全局数据范围（系统管理员）无需具体部门任期
        if "*" in principal.permissions:
            return {
                "user_id": principal.user_id,
                "name": principal.display_name,
                "department_id": department_id,
            }
        failures = self._eligibility_failures(principal.user_id, department_id, permission)
        if failures:
            raise PermissionDeniedError("；".join(failures))
        return {
            "user_id": principal.user_id,
            "name": principal.display_name,
            "department_id": department_id,
        }

    def _eligibility_failures(self, user_id: int | None, department_id: int | None, permission: str) -> list[str]:
        if user_id is None:
            return ["缺少经办人记录"]
        user = self.users.get(user_id)
        if user is None:
            return ["账号不存在"]
        failures: list[str] = []
        if user["status"] != "active":
            failures.append("账号已停用或锁定")
        if permission not in self.users.permissions(user_id):
            failures.append(f"缺少权限：{permission}")
        now = to_storage(self.clock.now())
        department = self.connection.execute(
            "SELECT id,is_active FROM departments WHERE id=?", (department_id,)
        ).fetchone()
        if department is None:
            failures.append("承办部门不存在")
        elif not department["is_active"]:
            failures.append("承办部门已停用")
        membership = self.connection.execute(
            "SELECT 1 FROM department_memberships WHERE user_id=? AND department_id=? "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>?) LIMIT 1",
            (user_id, department_id, now, now),
        ).fetchone()
        if membership is None:
            failures.append("在承办部门的任期已失效")
        return failures

    def _add_decision(
        self,
        *,
        affair_id: int,
        round_no: int,
        stage: str,
        decision: str,
        actor: dict,
        opinion: str | None,
        idempotency_key: str | None,
        now: str,
    ) -> None:
        self.affairs.add_decision(
            affair_id=affair_id,
            round_no=round_no,
            stage=stage,
            decision=decision,
            actor=actor,
            opinion=opinion,
            idempotency_key=idempotency_key,
            now=now,
        )
