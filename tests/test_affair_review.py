from __future__ import annotations

from datetime import UTC, datetime, timedelta

_RESIDENT_SEQ = 0


def _auth(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password, "client_label": "tests"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _setup_org(client, admin):
    department = client.post(
        "/api/departments",
        headers=admin["headers"],
        json={"name": "民政办", "manager": "主任", "phone": "010-88888888"},
    )
    assert department.status_code == 201, department.text
    department_id = department.json()["id"]

    handler_role = client.post(
        "/api/roles",
        headers=admin["headers"],
        # 经办人同时具备经办与复核权限，用以验证“同人阻断”而非权限不足
        json={"code": "affair.handler", "name": "事务经办员", "permission_codes": ["affairs.read", "affairs.write", "affairs.review"]},
    ).json()
    reviewer_role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": "affair.reviewer", "name": "事务复核员", "permission_codes": ["affairs.read", "affairs.review"]},
    ).json()

    def make_user(username, display_name, role_code):
        created = client.post(
            "/api/users",
            headers=admin["headers"],
            json={
                "username": username,
                "password": "Clerk!23456",
                "display_name": display_name,
                "department_id": department_id,
                "role_codes": [role_code],
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]
        starts = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
        membership = client.post(
            f"/api/departments/users/{user_id}/memberships",
            headers=admin["headers"],
            json={"department_id": department_id, "is_primary": True, "starts_at": starts},
        )
        assert membership.status_code == 201, membership.text
        return user_id

    handler_id = make_user("handler.a", "经办员甲", "affair.handler")
    reviewer_id = make_user("reviewer.b", "复核员乙", "affair.reviewer")
    return {
        "department_id": department_id,
        "handler": _auth(client, "handler.a", "Clerk!23456"),
        "reviewer": _auth(client, "reviewer.b", "Clerk!23456"),
        "handler_id": handler_id,
        "reviewer_id": reviewer_id,
    }


def _create_affair(client, category: str = "低保") -> int:
    global _RESIDENT_SEQ
    _RESIDENT_SEQ += 1
    id_card = f"11010119850101{_RESIDENT_SEQ:04d}"
    resident = client.post(
        "/residents",
        json={
            "name": "李四", "id_card": id_card, "gender": "男",
            "birth_date": "1985-05-05", "address": "民政路一号", "village": "民政村",
        },
    )
    assert resident.status_code == 201, resident.text
    affair = client.post(
        "/affairs",
        json={"title": f"{category}补助申请", "category": category, "applicant_id": resident.json()["id"]},
    )
    assert affair.status_code == 201, affair.text
    return affair.json()["id"]


def _move_to_processing(client, affair_id: int, department_id: int, handler_name: str = "经办员甲"):
    response = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "办理中", "department_id": department_id, "handler": handler_name},
    )
    assert response.status_code == 200, response.text


def test_controlled_category_requires_separation(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])

    # 旧接口直接办结受控类别被阻断
    legacy = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "已办结", "department_id": org["department_id"], "handler": "经办员甲", "result": "自行办结"},
    )
    assert legacy.status_code == 409

    # 经办人提交，进入待复核
    submitted = client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"],
        json={"result": "低保材料齐全，建议发放"},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "待复核"

    # 经办人不能复核本人提交的事务
    self_review = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["handler"],
        json={"approved": True, "opinion": "自己复核"},
    )
    assert self_review.status_code == 403

    # 不同复核人通过后办结
    approved = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"],
        json={"approved": True, "opinion": "情况属实，同意发放"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "已办结"

    detail = client.get(f"/api/affairs/{affair_id}", headers=org["reviewer"]).json()
    assert detail["controlled"] is True
    assert detail["review_rounds"][0]["status"] == "approved"
    assert detail["review_rounds"][0]["handler_name"] == "经办员甲"
    assert detail["review_rounds"][0]["reviewer_name"] == "复核员乙"
    assert [item["decision"] for item in detail["decisions"]] == ["submitted", "approved"]
    assert detail["current_responsibility"]["stage"] == "已办结"


def test_reviewer_without_department_scope_is_blocked(client, admin):
    org = _setup_org(client, admin)
    # 第三个复核员，有复核权限但没有任何部门任期
    outsider = client.post(
        "/api/users",
        headers=admin["headers"],
        json={
            "username": "reviewer.outsider", "password": "Clerk!23456",
            "display_name": "外来复核员", "role_codes": ["affair.reviewer"],
        },
    )
    assert outsider.status_code == 201, outsider.text
    outsider_headers = _auth(client, "reviewer.outsider", "Clerk!23456")

    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )
    response = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=outsider_headers, json={"approved": True, "opinion": "越权复核"},
    )
    assert response.status_code == 403
    assert "任期" in response.json()["error"]["message"]


def test_handler_losing_eligibility_blocks_pending_approval(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )

    # 经办权限被收回（换成只有复核权限、不含经办权限的角色），复核时实时复检阻断
    replaced = client.put(
        f"/api/users/{org['handler_id']}/roles",
        headers=admin["headers"], json={"role_codes": ["affair.reviewer"]},
    )
    assert replaced.status_code == 200, replaced.text

    blocked = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": True, "opinion": "尝试办结"},
    )
    assert blocked.status_code == 403
    assert "经办一方" in blocked.json()["error"]["message"]
    assert "affairs.write" in blocked.json()["error"]["message"]

    # 事务仍是待复核，待办原因中可见阻断因素
    detail = client.get(f"/api/affairs/{affair_id}", headers=org["reviewer"]).json()
    assert detail["status"] == "待复核"
    assert any("经办一方" in reason for reason in detail["pending_reasons"])
    assert detail["review_rounds"][0]["status"] == "pending_review"


def test_expired_membership_blocks_pending_approval(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    submit = client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )
    assert submit.status_code == 200

    # 经办人在承办部门的任期在复核前结束
    memberships = client.get(
        f"/api/departments/{org['department_id']}/members", headers=admin["headers"]
    ).json()
    handler_membership = next(item for item in memberships if item["user_id"] == org["handler_id"])
    ended = client.post(
        f"/api/departments/memberships/{handler_membership['id']}/end",
        headers=admin["headers"],
        json={"ends_at": datetime.now(UTC).isoformat(timespec="seconds")},
    )
    assert ended.status_code == 200, ended.text

    blocked = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": True, "opinion": "尝试办结"},
    )
    assert blocked.status_code == 403
    assert "任期已失效" in blocked.json()["error"]["message"]
    assert client.get(f"/api/affairs/{affair_id}", headers=org["reviewer"]).json()["status"] == "待复核"


def test_duplicate_clicks_form_single_decision(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])

    # 正常提交一次，进入待复核
    first = client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )
    assert first.status_code == 200
    # 无幂等键再次点击：状态机直接拒绝，不会形成第二条决定
    second = client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "再次提交"},
    )
    assert second.status_code == 409

    key = "review-click-1"
    first = client.post(
        f"/api/affairs/{affair_id}/review",
        headers={**org["reviewer"], "Idempotency-Key": key},
        json={"approved": True, "opinion": "同意"},
    )
    assert first.status_code == 200
    replay = client.post(
        f"/api/affairs/{affair_id}/review",
        headers={**org["reviewer"], "Idempotency-Key": key},
        json={"approved": True, "opinion": "同意"},
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replayed"] is True

    # 已办结后再点复核被状态机拒绝
    extra = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": True, "opinion": "再点一次"},
    )
    assert extra.status_code == 409

    detail = client.get(f"/api/affairs/{affair_id}", headers=org["reviewer"]).json()
    assert len(detail["review_rounds"]) == 1
    assert len([d for d in detail["decisions"] if d["stage"] == "review"]) == 1


def test_return_opens_new_round_but_keeps_old_evidence(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])

    client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "首轮材料"},
    )
    returned = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": False, "opinion": "缺收入证明，退回"},
    )
    assert returned.status_code == 200
    assert returned.json()["status"] == "已退回"

    # 沿用原退回流转回到办理中，然后重新经办
    back = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "待受理", "handler": "经办员甲"},
    )
    assert back.status_code == 200, back.text
    _move_to_processing(client, affair_id, org["department_id"])

    resubmitted = client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "补齐收入证明"},
    )
    assert resubmitted.status_code == 200
    assert resubmitted.json()["status"] == "待复核"

    approved = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": True, "opinion": "材料齐全，同意"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "已办结"

    detail = client.get(f"/api/affairs/{affair_id}", headers=org["reviewer"]).json()
    assert [r["round_no"] for r in detail["review_rounds"]] == [1, 2]
    assert detail["review_rounds"][0]["status"] == "returned"
    assert detail["review_rounds"][0]["review_opinion"] == "缺收入证明，退回"
    assert detail["review_rounds"][1]["status"] == "approved"
    # 旧轮次证据完整保留：两轮经办 + 退回 + 通过共四条决定
    assert [d["decision"] for d in detail["decisions"]] == [
        "submitted", "returned", "submitted", "approved"
    ]
    assert [d["round_no"] for d in detail["decisions"]] == [1, 1, 2, 2]


def test_policy_configuration_controls_category(client, admin):
    org = _setup_org(client, admin)

    policies = client.get("/api/affairs/review-policies", headers=admin["headers"])
    assert policies.status_code == 200
    categories = {item["category"]: item["is_active"] for item in policies.json()}
    assert categories.get("低保") == 1

    # 普通经办人无权配置策略
    forbidden = client.put(
        "/api/affairs/review-policies/低保",
        headers=org["handler"], json={"is_active": False},
    )
    assert forbidden.status_code == 403

    # 关闭低保后，低保沿用原流转，可直接办结
    disabled = client.put(
        "/api/affairs/review-policies/低保",
        headers=admin["headers"], json={"is_active": False, "note": "暂停控制"},
    )
    assert disabled.status_code == 200
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    completed = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "已办结", "department_id": org["department_id"], "handler": "经办员甲", "result": "直接办结"},
    )
    assert completed.status_code == 200, completed.text
    detail = client.get(f"/affairs/{affair_id}").json()
    assert detail["controlled"] is False
    assert detail["pending_reasons"] == ["普通类别，沿用原有流转"]

    # 把社保纳入受控后，社保必须走复核
    client.put(
        "/api/affairs/review-policies/社保",
        headers=admin["headers"], json={"is_active": True, "note": "新增受控"},
    )
    social_id = _create_affair(client, category="社保")
    _move_to_processing(client, social_id, department_id=org["department_id"])
    submitted = client.post(
        f"/api/affairs/{social_id}/submit",
        headers=org["handler"], json={"result": "社保业务待复核"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "待复核"


def test_policy_disabled_midflight_does_not_strand_pending_review(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )

    # 事务待复核期间关闭策略
    client.put(
        "/api/affairs/review-policies/低保",
        headers=admin["headers"], json={"is_active": False},
    )
    # 旧接口仍不能绕过在途复核
    bypass = client.put(
        f"/affairs/{affair_id}/process",
        json={"status": "已办结", "department_id": org["department_id"], "handler": "经办员甲", "result": "绕过"},
    )
    assert bypass.status_code == 400
    # 复核人仍须完成本轮复核，同人阻断依旧生效
    self_review = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["handler"], json={"approved": True, "opinion": "自审"},
    )
    assert self_review.status_code == 403
    approved = client.post(
        f"/api/affairs/{affair_id}/review",
        headers=org["reviewer"], json={"approved": True, "opinion": "在途复核完成"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "已办结"
    detail = approved.json()
    assert detail["review_rounds"][0]["status"] == "approved"


def test_detail_shows_responsibility_and_pending_reasons(client, admin):
    org = _setup_org(client, admin)
    affair_id = _create_affair(client)
    _move_to_processing(client, affair_id, org["department_id"])
    client.post(
        f"/api/affairs/{affair_id}/submit",
        headers=org["handler"], json={"result": "材料齐全"},
    )

    # 新旧两个详情入口都能看到当前责任与待办原因
    for path in (f"/api/affairs/{affair_id}", f"/affairs/{affair_id}"):
        headers = org["reviewer"] if path.startswith("/api") else {}
        detail = client.get(path, headers=headers).json()
        assert detail["current_responsibility"]["role"] == "复核岗"
        assert detail["current_responsibility"]["round_no"] == 1
        assert detail["current_responsibility"]["handler_name"] == "经办员甲"
        assert any("不同人员" in reason for reason in detail["pending_reasons"])
        assert detail["decisions"][0]["stage"] == "handle"
