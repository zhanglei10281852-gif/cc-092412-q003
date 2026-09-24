from __future__ import annotations

import sqlite3

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import Principal, request_fingerprint
from app.repositories.business import AffairControlRepository, AffairRepository
from app.services.access import DataScope
from app.services.audit import AuditContext, AuditService

# 受控类别：受理 -> 经办 -> 复核 -> 办结/退回
CONTROLLED_TRANSITIONS = {
    ("待受理", "办理中"): "accept",
    ("已退回", "办理中"): "reopen",
    ("办理中", "待复核"): "submit",
    ("办理中", "已退回"): "return",
    ("待复核", "已办结"): "approve",
    ("待复核", "已退回"): "return",
}

# 非受控类别：沿用原有流转，办理中可直接办结
PLAIN_TRANSITIONS = {
    ("待受理", "办理中"): "accept",
    ("已退回", "待受理"): "reopen",
    ("办理中", "已办结"): "complete",
    ("办理中", "已退回"): "return",
}


class AffairWorkflowService:
    """事务办理与可配置职责分离规则。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.affairs = AffairRepository(connection)
        self.controls = AffairControlRepository(connection)
        self.audit = AuditService(connection, self.clock)

    # ------------------------------------------------------------------ 规则配置

    def list_rules(self, principal: Principal) -> list[dict]:
        principal.require("affairs.read")
        return self.controls.list_rules()

    def configure_rule(self, principal: Principal, category: str, is_enabled: bool) -> dict:
        principal.require("affairs.configure")
        valid = {"户籍", "社保", "医保", "低保", "建房", "计生", "其他"}
        if category not in valid:
            raise ValidationError(f"不支持的事务类别：{category}")
        now = to_storage(self.clock.now())
        rule = self.controls.upsert_rule(category, is_enabled, principal.user_id, now)
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action="affair.rule.configure",
            resource_type="affair_control_rule",
            resource_id=category,
            after={"category": category, "is_enabled": is_enabled},
        )
        return rule

    # ------------------------------------------------------------------ 详情视图

    def detail(self, principal: Principal, affair_id: int) -> dict:
        principal.require("affairs.read")
        affair = self.affairs.detail(affair_id)
        if affair is None:
            raise NotFoundError("政务事务不存在")
        scope = DataScope.from_principal(principal, "affairs.read")
        if affair["department_id"] is not None:
            scope.require_owned_department(affair["department_id"])
        return self.build_detail(affair)

    def build_detail(self, affair: dict) -> dict:
        controlled = self.controls.is_controlled(affair["category"])
        rounds = self.controls.rounds(affair["id"]) if controlled else []
        decisions = self.controls.decisions(affair["id"])
        open_round = next((item for item in reversed(rounds) if item["status"] == "待复核"), None)
        result = dict(affair)
        result["controlled"] = controlled
        result["review_rounds"] = rounds
        result["decisions"] = decisions
        result["current_responsibility"], result["pending_reason"] = self._responsibility(
            affair, rounds, open_round, controlled, decisions
        )
        return result

    def _responsibility(
        self,
        affair: dict,
        rounds: list[dict],
        open_round: dict | None,
        controlled: bool,
        decisions: list[dict] | None = None,
    ) -> tuple[dict, str]:
        status = affair["status"]
        department = {"department_id": affair["department_id"], "department_name": affair["department_name"]}
        if status == "待受理":
            return {"stage": "待受理", **department}, "等待受理并分派承办部门"
        if status == "已办结":
            last_round = rounds[-1] if rounds else None
            return (
                {"stage": "已办结", **department,
                 "reviewer_name": last_round["reviewer_name"] if last_round else affair["handler"]},
                "流程已完结",
            )
        if status == "已退回":
            latest = rounds[-1] if rounds else None
            if latest and latest["status"] == "复核退回":
                reason = latest["review_opinion"] or "复核未通过"
                next_round = latest["round_no"] + 1
                return (
                    {"stage": "退回重办", **department, "next_round_no": next_round},
                    f"已退回重办：{reason}；重新经办提交后将开启第 {next_round} 轮复核，历史轮次证据保留",
                )
            fallback = next((d for d in reversed(decisions or []) if d["to_status"] == "已退回"), None)
            reason = (fallback["summary"] if fallback else None) or "已退回"
            return (
                {"stage": "退回重办", **department, "next_round_no": (latest["round_no"] + 1) if latest else 1},
                f"已退回重办：{reason}；重新受理后继续办理",
            )
        if status == "办理中":
            if controlled:
                return (
                    {"stage": "经办处理", **department, "handler_name": affair["handler"]},
                    f"经办人 {affair['handler']} 提交办理结果后进入复核，办结前须经不同人员复核",
                )
            return (
                {"stage": "经办处理", **department, "handler_name": affair["handler"]},
                "非受控类别，经办完成后可直接办结",
            )
        if status == "待复核" and open_round is not None:
            return (
                {"stage": "等待复核", **department, "round_no": open_round["round_no"],
                 "handler_name": open_round["handler_name"], "handler_user_id": open_round["handler_user_id"]},
                f"等待与经办人 {open_round['handler_name']} 不同、且在本部门任期与权限均有效的人员完成复核",
            )
        return {"stage": status, **department}, ""

    # ------------------------------------------------------------------ 状态流转

    def transition(
        self,
        principal: Principal,
        affair_id: int,
        target_status: str,
        *,
        department_id: int | None = None,
        result: str | None = None,
        opinion: str | None = None,
        idempotency_key: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        affair = self.affairs.detail(affair_id)
        if affair is None:
            raise NotFoundError("政务事务不存在")
        principal.require("affairs.write")
        now = to_storage(self.clock.now())
        fingerprint = request_fingerprint(payload or {})

        if idempotency_key:
            existing = self.controls.decision_by_key(idempotency_key)
            if existing is not None:
                if existing["affair_id"] != affair_id or existing["request_hash"] != fingerprint:
                    raise ConflictError("同一幂等键不能用于不同请求")
                refreshed = self.affairs.detail(affair_id)
                assert refreshed is not None
                return {**self.build_detail(refreshed), "replayed": True, "decision_id": existing["id"]}

        controlled = self.controls.is_controlled(affair["category"])
        table = CONTROLLED_TRANSITIONS if controlled else PLAIN_TRANSITIONS
        action = table.get((affair["status"], target_status))
        if action is None:
            if controlled and (affair["status"], target_status) == ("办理中", "已办结"):
                raise ConflictError("受控类别在办结前必须先提交复核，不能由经办人直接办结")
            raise ConflictError(f"状态不允许从 {affair['status']} 转到 {target_status}")

        effective_department_id = affair["department_id"]
        round_no: int | None = None

        if action in {"accept", "reopen"}:
            effective_department_id = self._accept_or_reopen(principal, affair, target_status, department_id, now)
        elif action == "submit":
            effective_department_id = self._submit(principal, affair, result, now)
            open_round = self.controls.open_round(affair_id)
            round_no = open_round["round_no"] if open_round else None
        elif action in {"approve"}:
            round_no = self._review(principal, affair, approve=True, opinion=opinion, now=now)
        elif action == "return":
            if affair["status"] == "待复核":
                round_no = self._review(principal, affair, approve=False, opinion=opinion, now=now)
            else:
                effective_department_id = self._handler_return(principal, affair, opinion, now)
        elif action == "complete":
            effective_department_id = self._direct_complete(principal, affair, result, now)

        updates = ["status=?", "updated_at=?"]
        params: list = [target_status, now]
        if result is not None and action in {"submit"}:
            updates.append("result=?")
            params.append(result)
        elif result is not None and action == "complete":
            updates.append("result=?")
            params.append(result)
        if effective_department_id is not None:
            updates.append("department_id=?")
            params.append(effective_department_id)
        params.append(affair_id)
        self.connection.execute(f"UPDATE affairs SET {','.join(updates)} WHERE id=?", tuple(params))

        summary = opinion or result
        decision_id = self.controls.insert_decision(
            affair_id=affair_id,
            round_no=round_no,
            action=action,
            actor_user_id=principal.user_id,
            actor_name=principal.display_name,
            from_status=affair["status"],
            to_status=target_status,
            summary=summary,
            request_hash=fingerprint,
            idempotency_key=idempotency_key,
            created_at=now,
        )
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=f"affair.{action}",
            resource_type="affair",
            resource_id=affair_id,
            before={"status": affair["status"]},
            after={"status": target_status, "round_no": round_no, "controlled": controlled},
            metadata={"idempotency_key": idempotency_key},
        )
        after = self.affairs.detail(affair_id)
        assert after is not None
        return {**self.build_detail(after), "replayed": False, "decision_id": decision_id}

    # ------------------------------------------------------------------ 各动作规则

    def _resolve_department(self, department_id: int) -> None:
        row = self.connection.execute(
            "SELECT id FROM departments WHERE id=? AND is_active=1", (department_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("承办部门不存在或已停用")

    def _require_live_scope(self, principal: Principal, department_id: int | None, now: str) -> None:
        """决定生效瞬间重新校验账号状态、权限与部门任期，防止授权事后失效。"""
        user = self.connection.execute(
            "SELECT status FROM users WHERE id=?", (principal.user_id,)
        ).fetchone()
        if user is None or user["status"] != "active":
            raise PermissionDeniedError("账号已失效，不能继续审批")
        if department_id is None:
            raise PermissionDeniedError("事务尚未分派承办部门")
        permissions = self.controls.live_permissions(principal.user_id)
        if "*" in permissions:
            return
        if "affairs.write" not in permissions:
            raise PermissionDeniedError("事务办理权限已失效，审批被阻断")
        membership = self.controls.active_membership(principal.user_id, department_id, now)
        if membership is None:
            raise PermissionDeniedError("在承办部门的数据范围或任期已失效，审批被阻断")

    def _accept_or_reopen(
        self, principal: Principal, affair: dict, target_status: str,
        department_id: int | None, now: str,
    ) -> int:
        if target_status == "办理中" and affair["status"] == "待受理" and department_id is None:
            raise ValidationError("受理时必须指定承办部门")
        effective_department_id = department_id or affair["department_id"]
        if effective_department_id is None:
            raise ValidationError("必须指定承办部门")
        self._resolve_department(effective_department_id)
        self._require_live_scope(principal, effective_department_id, now)
        self.connection.execute(
            "UPDATE affairs SET handler=? WHERE id=?", (principal.display_name, affair["id"])
        )
        return effective_department_id

    def _submit(self, principal: Principal, affair: dict, result: str | None, now: str) -> int:
        if not result or not result.strip():
            raise ValidationError("提交复核时必须填写办理结果")
        department_id = affair["department_id"]
        self._require_live_scope(principal, department_id, now)
        round_no = self.controls.next_round_no(affair["id"])
        handler = {"id": principal.user_id, "display_name": principal.display_name}
        self.controls.insert_round(
            affair["id"], round_no, handler, department_id, result.strip(), now, now
        )
        self.connection.execute(
            "UPDATE affairs SET handler=? WHERE id=?", (principal.display_name, affair["id"])
        )
        return department_id

    def _review(
        self, principal: Principal, affair: dict, *, approve: bool, opinion: str | None, now: str
    ) -> int:
        if not opinion or not opinion.strip():
            raise ValidationError("复核意见不能为空")
        open_round = self.controls.open_round(affair["id"])
        if open_round is None:
            raise ConflictError("当前没有待复核的轮次")
        if principal.user_id == open_round["handler_user_id"]:
            raise PermissionDeniedError("经办与复核必须由不同人员完成，不能自办自审")
        self._require_live_scope(principal, open_round["handler_department_id"], now)
        if approve:
            # 办结是最终决定：经办人一方权限或任期在复核期间失效同样阻断办结
            self._require_party_active(open_round["handler_user_id"], open_round["handler_department_id"], now)
        self.controls.finish_round(
            open_round["id"],
            "复核通过" if approve else "复核退回",
            {"id": principal.user_id, "display_name": principal.display_name},
            opinion.strip(),
            now,
        )
        return int(open_round["round_no"])

    def _require_party_active(self, user_id: int, department_id: int, now: str) -> None:
        user = self.connection.execute("SELECT status FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None or user["status"] != "active":
            raise PermissionDeniedError("原经办人账号已失效，不能办结，请退回重办")
        permissions = self.controls.live_permissions(user_id)
        if "*" not in permissions and "affairs.write" not in permissions:
            raise PermissionDeniedError("原经办人的办理权限已失效，不能办结，请退回重办")
        if self.controls.active_membership(user_id, department_id, now) is None:
            raise PermissionDeniedError("原经办人在承办部门的任期已失效，不能办结，请退回重办")

    def _handler_return(
        self, principal: Principal, affair: dict, opinion: str | None, now: str
    ) -> int | None:
        if not opinion or not opinion.strip():
            raise ValidationError("退回原因不能为空")
        department_id = affair["department_id"]
        self._require_live_scope(principal, department_id, now)
        return department_id

    def _direct_complete(
        self, principal: Principal, affair: dict, result: str | None, now: str
    ) -> int | None:
        department_id = affair["department_id"]
        self._require_live_scope(principal, department_id, now)
        if result is not None and result.strip():
            self.connection.execute(
                "UPDATE affairs SET handler=? WHERE id=?", (principal.display_name, affair["id"])
            )
        return department_id
