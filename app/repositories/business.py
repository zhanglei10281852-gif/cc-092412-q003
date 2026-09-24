from __future__ import annotations

import sqlite3
from typing import Any

from app.repositories.base import Repository, row_dict, rows_dict


class DepartmentRepository(Repository):
    table = "departments"
    entity_name = "部门"

    def by_name(self, name: str) -> dict[str, Any] | None:
        return row_dict(self.connection.execute("SELECT * FROM departments WHERE name=?", (name,)).fetchone())

    def list(self, *, active_only: bool, limit: int, offset: int) -> list[dict]:
        where = " WHERE d.is_active=1" if active_only else ""
        return rows_dict(self.connection.execute(
            "SELECT d.*,COUNT(DISTINCT u.id) AS user_count,COUNT(DISTINCT p.id) AS petition_count "
            "FROM departments d LEFT JOIN users u ON u.department_id=d.id "
            "LEFT JOIN petitions p ON p.department_id=d.id" + where +
            " GROUP BY d.id ORDER BY d.name LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall())

    def active_memberships(self, department_id: int, moment: str) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT m.*,u.username,u.display_name,u.status FROM department_memberships m "
            "JOIN users u ON u.id=m.user_id WHERE m.department_id=? "
            "AND m.starts_at<=? AND (m.ends_at IS NULL OR m.ends_at>?) ORDER BY m.is_primary DESC,u.display_name",
            (department_id, moment, moment),
        ).fetchall())

    def membership(self, membership_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT m.*,u.username,u.display_name,d.name AS department_name FROM department_memberships m "
            "JOIN users u ON u.id=m.user_id JOIN departments d ON d.id=m.department_id WHERE m.id=?",
            (membership_id,),
        ).fetchone())

    def user_memberships(self, user_id: int, moment: str) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT m.*,d.name AS department_name FROM department_memberships m "
            "JOIN departments d ON d.id=m.department_id WHERE m.user_id=? "
            "AND m.starts_at<=? AND (m.ends_at IS NULL OR m.ends_at>?) ORDER BY m.is_primary DESC,d.name",
            (user_id, moment, moment),
        ).fetchall())


class ResidentRepository(Repository):
    table = "residents"
    entity_name = "居民"

    def search(self, *, village: str | None, name: str | None, limit: int, offset: int) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if village:
            conditions.append("village=?")
            params.append(village)
        if name:
            conditions.append("name LIKE ?")
            params.append(f"%{name}%")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        return rows_dict(self.connection.execute(
            "SELECT * FROM residents" + where + " ORDER BY id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())

    def dependency_counts(self, resident_id: int) -> dict[str, int]:
        affairs = int(self.connection.execute("SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)).fetchone()[0])
        return {"affairs": affairs}


class PetitionRepository(Repository):
    table = "petitions"
    entity_name = "信访件"

    def detail(self, petition_id: int) -> dict[str, Any] | None:
        petition = row_dict(self.connection.execute(
            "SELECT p.*,d.name AS department_name FROM petitions p LEFT JOIN departments d ON d.id=p.department_id WHERE p.id=?",
            (petition_id,),
        ).fetchone())
        if petition is None:
            return None
        petition["flow_records"] = rows_dict(self.connection.execute(
            "SELECT * FROM petition_flow_records WHERE petition_id=? ORDER BY id", (petition_id,)
        ).fetchall())
        petition["urge_records"] = rows_dict(self.connection.execute(
            "SELECT * FROM petition_urges WHERE petition_id=? ORDER BY id DESC", (petition_id,)
        ).fetchall())
        return petition

    def list_for_scope(
        self,
        *,
        department_id: int | None,
        statuses: list[str] | None,
        deadline_before: str | None,
        limit: int,
        offset: int,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if department_id is not None:
            conditions.append("p.department_id=?")
            params.append(department_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"p.status IN ({placeholders})")
            params.extend(statuses)
        if deadline_before:
            conditions.append("p.deadline<?")
            params.append(deadline_before)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        return rows_dict(self.connection.execute(
            "SELECT p.*,d.name AS department_name FROM petitions p LEFT JOIN departments d ON d.id=p.department_id"
            + where + " ORDER BY p.id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())

    def append_flow(self, petition_id: int, action: str, operator: str, remark: str | None, created_at: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO petition_flow_records(petition_id,action,operator,remark,created_at) VALUES(?,?,?,?,?)",
            (petition_id, action, operator, remark, created_at),
        )
        return int(cursor.lastrowid)


class AffairRepository(Repository):
    table = "affairs"
    entity_name = "政务事务"

    POLICY_DEFAULT_ACTIVE = {"低保"}

    def detail(self, affair_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT a.*,r.name AS applicant_name,r.phone AS applicant_phone,"
            "d.name AS department_name,d.manager AS department_manager,d.phone AS department_phone "
            "FROM affairs a "
            "JOIN residents r ON r.id=a.applicant_id LEFT JOIN departments d ON d.id=a.department_id WHERE a.id=?",
            (affair_id,),
        ).fetchone())

    def list_for_scope(self, department_id: int | None, status: str | None, limit: int, offset: int) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if department_id is not None:
            conditions.append("a.department_id=?")
            params.append(department_id)
        if status:
            conditions.append("a.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])
        return rows_dict(self.connection.execute(
            "SELECT a.*,r.name AS applicant_name,d.name AS department_name FROM affairs a "
            "JOIN residents r ON r.id=a.applicant_id LEFT JOIN departments d ON d.id=a.department_id"
            + where + " ORDER BY a.id DESC LIMIT ? OFFSET ?", tuple(params)
        ).fetchall())

    # ---- 职责分离策略 ----

    def policy_map(self) -> dict[str, dict[str, Any]]:
        return {
            row["category"]: row_dict(row)
            for row in self.connection.execute("SELECT * FROM affair_review_policies").fetchall()
        }

    def list_policies(self) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM affair_review_policies ORDER BY category"
        ).fetchall())

    def upsert_policy(self, category: str, is_active: bool, note: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO affair_review_policies(category,is_active,note,created_at,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(category) DO UPDATE SET is_active=excluded.is_active,note=excluded.note,updated_at=excluded.updated_at",
            (category, 1 if is_active else 0, note, now, now),
        )
        policy = row_dict(self.connection.execute(
            "SELECT * FROM affair_review_policies WHERE category=?", (category,)
        ).fetchone())
        assert policy is not None
        return policy

    def is_controlled(self, category: str) -> bool:
        row = self.connection.execute(
            "SELECT is_active FROM affair_review_policies WHERE category=?", (category,)
        ).fetchone()
        if row is not None:
            return bool(row[0])
        return category in self.POLICY_DEFAULT_ACTIVE

    # ---- 复核轮次与决定证据 ----

    def rounds(self, affair_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM affair_review_rounds WHERE affair_id=? ORDER BY round_no", (affair_id,)
        ).fetchall())

    def pending_round(self, affair_id: int) -> dict[str, Any] | None:
        return row_dict(self.connection.execute(
            "SELECT * FROM affair_review_rounds WHERE affair_id=? AND status='pending_review' "
            "ORDER BY round_no DESC LIMIT 1",
            (affair_id,),
        ).fetchone())

    def open_round(self, affair_id: int, handler: dict, result: str | None, now: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(round_no),0)+1 AS next_no FROM affair_review_rounds WHERE affair_id=?",
            (affair_id,),
        ).fetchone()
        round_no = int(row["next_no"])
        self.connection.execute(
            "INSERT INTO affair_review_rounds(affair_id,round_no,status,handler_user_id,handler_name,"
            "handler_department_id,result,created_at,submitted_at) "
            "VALUES(?,?,'pending_review',?,?,?,?,?,?)",
            (affair_id, round_no, handler["user_id"], handler["name"], handler["department_id"], result, now, now),
        )
        return round_no

    def close_round(self, round_id: int, reviewer: dict, opinion: str, status: str, now: str) -> None:
        self.connection.execute(
            "UPDATE affair_review_rounds SET status=?,reviewer_user_id=?,reviewer_name=?,"
            "review_opinion=?,decided_at=?,closed_at=? WHERE id=?",
            (status, reviewer["user_id"], reviewer["name"], opinion, now, now, round_id),
        )

    def add_decision(
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
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO affair_decisions(affair_id,round_no,stage,decision,actor_user_id,actor_name,"
            "department_id,opinion,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (affair_id, round_no, stage, decision, actor["user_id"], actor["name"],
             actor.get("department_id"), opinion, idempotency_key, now),
        )
        return int(cursor.lastrowid)

    def decisions(self, affair_id: int) -> list[dict]:
        return rows_dict(self.connection.execute(
            "SELECT * FROM affair_decisions WHERE affair_id=? ORDER BY id", (affair_id,)
        ).fetchall())

