from __future__ import annotations

from datetime import UTC, datetime, timedelta


# --------------------------------------------------------------------- 测试夹具辅助


def _make_department(client, admin_headers, name: str) -> int:
    resp = client.post(
        "/api/departments",
        headers=admin_headers,
        json={"name": name, "manager": "主任", "phone": "010-6688"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_role(client, admin_headers, code: str, permissions: list[str]) -> None:
    resp = client.post(
        "/api/roles",
        headers=admin_headers,
        json={"code": code, "name": code, "permission_codes": permissions},
    )
    assert resp.status_code == 201, resp.text


def _make_user(client, admin_headers, username: str, display_name: str, role_codes: list[str]) -> dict:
    resp = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": username, "password": "Worker!23456", "display_name": display_name, "role_codes": role_codes},
    )
    assert resp.status_code == 201, resp.text
    user = resp.json()
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "Worker!23456", "client_label": "tests"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    return {"id": user["id"], "headers": {"Authorization": f"Bearer {token}"}}


def _add_membership(client, admin_headers, user_id: int, department_id: int) -> dict:
    resp = client.post(
        f"/api/departments/users/{user_id}/memberships",
        headers=admin_headers,
        json={
            "department_id": department_id,
            "title": "科员",
            "is_primary": True,
            "starts_at": "2026-01-01T00:00:00+00:00",
            "ends_at": None,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_affair(client, category: str = "低保") -> int:
    resident = client.post(
        "/residents",
        json={"name": "李四", "id_card": "110101198505051234", "gender": "男", "birth_date": "1985-05-05", "address": "民政街", "village": "幸福村"},
    )
    assert resident.status_code == 201, resident.text
    resp = client.post(
        "/affairs",
        json={"title": "低保申请", "category": category, "applicant_id": resident.json()["id"], "description": "申请补助"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _setup_workers(client, admin):
    h = admin["headers"]
    dept_a = _make_department(client, h, "民政办")
    dept_b = _make_department(client, h, "社保办")
    _make_role(client, h, "affair.worker", ["affairs.read", "affairs.write"])
    _make_role(client, h, "affair.reader", ["affairs.read"])
    handler = _make_user(client, h, "handler.zhao", "赵经办", ["affair.worker"])
    reviewer = _make_user(client, h, "reviewer.qian", "钱复核", ["affair.worker"])
    outsider = _make_user(client, h, "clerk.sun", "孙外部门", ["affair.worker"])
    _add_membership(client, h, handler["id"], dept_a)
    _add_membership(client, h, reviewer["id"], dept_a)
    _add_membership(client, h, outsider["id"], dept_b)
    return {"dept_a": dept_a, "dept_b": dept_b, "handler": handler, "reviewer": reviewer, "outsider": outsider}


def _enable_rule(client, admin_headers, category: str = "低保") -> None:
    resp = client.put(f"/api/affair-workflow/rules/{category}", headers=admin_headers, json={"is_enabled": True})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------- 规则配置


def test_rule_is_configurable_and_requires_permission(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])

    rules = client.get("/api/affair-workflow/rules", headers=admin["headers"])
    assert rules.status_code == 200
    rule = next(item for item in rules.json() if item["category"] == "低保")
    assert rule["is_enabled"] == 1

    # 仅具备办理权限的人员不能配置规则
    denied = client.put(
        "/api/affair-workflow/rules/医保",
        headers=env["handler"]["headers"],
        json={"is_enabled": True},
    )
    assert denied.status_code == 403


def test_disable_rule_returns_category_to_legacy_flow(client, admin):
    _enable_rule(client, admin["headers"])
    resp = client.put("/api/affair-workflow/rules/低保", headers=admin["headers"], json={"is_enabled": False})
    assert resp.status_code == 200
    assert resp.json()["is_enabled"] == 0
    # 旧接口恢复可用
    affair_id = _create_affair(client)
    legacy = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "办理中", "handler": "王经办"},
    )
    assert legacy.status_code == 200


# --------------------------------------------------------------------- 正常双人流程


def test_controlled_category_requires_two_distinct_people(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)

    accepted = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    assert accepted.status_code == 201, accepted.text

    submitted = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "材料齐全，建议发放补助"},
    )
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["status"] == "待复核"
    assert body["review_rounds"][0]["status"] == "待复核"
    assert body["current_responsibility"]["stage"] == "等待复核"
    assert env["handler"]["id"] == body["current_responsibility"]["handler_user_id"]
    assert "待办" in body["pending_reason"] or "复核" in body["pending_reason"]

    approved = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已办结", "opinion": "同意发放"},
    )
    assert approved.status_code == 201, approved.text
    final = approved.json()
    assert final["status"] == "已办结"
    assert final["review_rounds"][0]["status"] == "复核通过"
    assert final["review_rounds"][0]["reviewer_name"] == "钱复核"
    actions = [item["action"] for item in final["decisions"]]
    assert actions == ["accept", "submit", "approve"]
    assert final["current_responsibility"]["stage"] == "已办结"


def test_handler_cannot_review_their_own_submission(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    self_review = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "已办结", "opinion": "自己复核"},
    )
    assert self_review.status_code == 403
    assert "不同人员" in self_review.json()["error"]["message"]


def test_controlled_category_cannot_skip_review(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    skipped = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "已办结", "result": "直接办结"},
    )
    assert skipped.status_code == 409

    # 旧接口同样被阻断，防止绕过
    legacy = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "已办结", "handler": "赵经办", "result": "绕过复核"},
    )
    assert legacy.status_code == 403


def test_reviewer_from_other_department_is_blocked(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    denied = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["outsider"]["headers"],
        json={"target_status": "已办结", "opinion": "外部门复核"},
    )
    assert denied.status_code == 403


# --------------------------------------------------------------------- 幂等


def test_duplicate_clicks_make_one_decision(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )

    headers = {**env["reviewer"]["headers"], "Idempotency-Key": "approve-001"}
    payload = {"target_status": "已办结", "opinion": "同意"}
    first = client.post(f"/api/affair-workflow/{affair_id}/decisions", headers=headers, json=payload)
    second = client.post(f"/api/affair-workflow/{affair_id}/decisions", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert first.json()["decision_id"] == second.json()["decision_id"]

    detail = client.get(f"/api/affair-workflow/{affair_id}", headers=env["reviewer"]["headers"])
    approve_decisions = [d for d in detail.json()["decisions"] if d["action"] == "approve"]
    assert len(approve_decisions) == 1

    # 不带幂等键的重复点击不会形成第二次决定
    again = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json=payload,
    )
    assert again.status_code == 409
    detail = client.get(f"/api/affair-workflow/{affair_id}", headers=env["reviewer"]["headers"])
    assert len([d for d in detail.json()["decisions"] if d["action"] == "approve"]) == 1


def test_same_idempotency_key_with_different_payload_is_rejected(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    headers = {**env["handler"]["headers"], "Idempotency-Key": "dedup-x"}
    first = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=headers,
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    assert first.status_code == 201
    clash = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=headers,
        json={"target_status": "办理中", "department_id": env["dept_b"]},
    )
    assert clash.status_code == 409


# --------------------------------------------------------------------- 退回重办


def test_return_starts_new_round_but_keeps_old_evidence(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)

    def decide(headers, payload):
        return client.post(f"/api/affair-workflow/{affair_id}/decisions", headers=headers, json=payload)

    decide(env["handler"]["headers"], {"target_status": "办理中", "department_id": env["dept_a"]})
    decide(env["handler"]["headers"], {"target_status": "待复核", "result": "首轮材料"})
    returned = decide(env["reviewer"]["headers"], {"target_status": "已退回", "opinion": "缺少公示记录"})
    assert returned.status_code == 201
    assert returned.json()["review_rounds"][0]["status"] == "复核退回"
    assert "退回重办" in returned.json()["current_responsibility"]["stage"]
    assert "第 2 轮" in returned.json()["pending_reason"]

    decide(env["handler"]["headers"], {"target_status": "办理中"})
    resubmitted = decide(env["handler"]["headers"], {"target_status": "待复核", "result": "补齐公示材料"})
    assert resubmitted.status_code == 201
    rounds = resubmitted.json()["review_rounds"]
    assert [r["round_no"] for r in rounds] == [1, 2]
    assert rounds[0]["status"] == "复核退回" and rounds[1]["status"] == "待复核"
    assert rounds[0]["result"] == "首轮材料"

    approved = decide(env["reviewer"]["headers"], {"target_status": "已办结", "opinion": "补齐后通过"})
    assert approved.status_code == 201
    final = approved.json()
    assert final["status"] == "已办结"
    assert [a["action"] for a in final["decisions"]] == [
        "accept", "submit", "return", "reopen", "submit", "approve"
    ]
    assert final["review_rounds"][1]["reviewer_name"] == "钱复核"


# --------------------------------------------------------------------- 失效阻断


def test_revoked_permission_blocks_pending_approval(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    # 经办后复核前撤掉复核人的办理权限
    replaced = client.put(
        f"/api/users/{env['reviewer']['id']}/roles",
        headers=admin["headers"],
        json={"role_codes": ["affair.reader"]},
    )
    assert replaced.status_code == 200

    blocked = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已办结", "opinion": "同意"},
    )
    assert blocked.status_code == 403
    assert "权限" in blocked.json()["error"]["message"]


def test_expired_department_tenure_blocks_pending_approval(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    listed = client.get(f"/api/departments/{env['dept_a']}/members", headers=admin["headers"])
    reviewer_membership = next(m for m in listed.json() if m["user_id"] == env["reviewer"]["id"])
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    ended = client.post(
        f"/api/departments/memberships/{reviewer_membership['id']}/end",
        headers=admin["headers"],
        json={"ends_at": past},
    )
    assert ended.status_code == 200, ended.text

    blocked = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已办结", "opinion": "同意"},
    )
    assert blocked.status_code == 403
    assert "任期" in blocked.json()["error"]["message"]


def test_handler_losing_scope_before_review_also_blocks_completion(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    # 复核前经办人失去部门任期；即使复核人资格齐全也不能办结，只能退回
    listed = client.get(f"/api/departments/{env['dept_a']}/members", headers=admin["headers"])
    handler_membership = next(m for m in listed.json() if m["user_id"] == env["handler"]["id"])
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    ended = client.post(
        f"/api/departments/memberships/{handler_membership['id']}/end",
        headers=admin["headers"],
        json={"ends_at": past},
    )
    assert ended.status_code == 200, ended.text

    blocked = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已办结", "opinion": "同意"},
    )
    assert blocked.status_code == 403
    assert "经办人" in blocked.json()["error"]["message"]

    # 仍然可以退回重办
    returned = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已退回", "opinion": "经办资格失效，重办"},
    )
    assert returned.status_code == 201


def test_disabled_account_is_blocked(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "待复核", "result": "经办完毕"},
    )
    client.patch(f"/api/users/{env['reviewer']['id']}", headers=admin["headers"], json={"status": "disabled"})
    blocked = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["reviewer"]["headers"],
        json={"target_status": "已办结", "opinion": "同意"},
    )
    assert blocked.status_code == 401


# --------------------------------------------------------------------- 非受控类别


def test_uncontrolled_category_keeps_original_flow(client, admin):
    env = _setup_workers(client, admin)
    affair_id = _create_affair(client, category="户籍")

    accepted = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "办理中", "department_id": env["dept_a"]},
    )
    assert accepted.status_code == 201
    completed = client.post(
        f"/api/affair-workflow/{affair_id}/decisions",
        headers=env["handler"]["headers"],
        json={"target_status": "已办结", "result": "当场办结"},
    )
    assert completed.status_code == 201
    body = completed.json()
    assert body["controlled"] is False
    assert body["review_rounds"] == []
    assert [d["action"] for d in body["decisions"]] == ["accept", "complete"]


def test_detail_requires_read_permission(client, admin):
    env = _setup_workers(client, admin)
    _enable_rule(client, admin["headers"])
    affair_id = _create_affair(client)
    # 未登录访问被拒
    assert client.get(f"/api/affair-workflow/{affair_id}").status_code == 401
    detail = client.get(f"/api/affair-workflow/{affair_id}", headers=env["reviewer"]["headers"])
    assert detail.status_code == 200
    assert detail.json()["pending_reason"]
