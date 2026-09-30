"""封存人交接（handover）规则测试。

覆盖：
- 发起邀请校验：仅封存人、资料未开封、受让人已注册且非封存人/当前见证人、
  截止时刻带时区且在未来、每份资料同时只有一份待确认且未过期邀请；
- 撤回：仅封存人、只能撤回待确认邀请；撤回/逾期后可重新邀请；
- 确认：仅受让人凭自己的 X-API-Key、已撤回/逾期/重复确认拒绝；
- 确认结算：资料已开封（含临界区到点开封）拒绝交接；进行中请求置
  superseded、旧同意失效；过截止请求先 expired；再转移 owner；
- 交接后原封存人失去查看/操作权限，新封存人可重新申请开封；
- 开封前任何响应不泄露正文；成功交接与请求失效均留审计。
"""
import datetime as dt

from conftest import auth, digest_of, register, seal

BODY = "交接绝密正文：HANDOVER-SECRET-2026"


def _in(minutes: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _setup(client, names=("alice", "bob", "carol", "dave", "erin")):
    return {n: register(client, n) for n in names}


def _invite(client, owner, doc_id, invitee, minutes=60):
    return client.post(
        f"/documents/{doc_id}/handovers",
        json={"invitee": invitee, "deadline": _in(minutes)},
        headers=auth(owner),
    )


def _open_request(client, owner, doc_id, minutes=60, **extra):
    payload = {"reason": "交接测试", "deadline": _in(minutes), **extra}
    return client.post(f"/documents/{doc_id}/open-requests",
                       json=payload, headers=auth(owner))


# ---------------------------------------------------------------------------
# 发起邀请
# ---------------------------------------------------------------------------


def test_create_invitation_validation(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]

    # 非封存人（见证人）不能发起
    assert _invite(client, keys["bob"], doc_id, "erin").status_code == 403
    # 陌生人无权且不泄露资料
    ghost = register(client, "ghost")
    assert _invite(client, ghost, doc_id, "erin").status_code == 403
    # 不存在的资料
    assert _invite(client, keys["alice"], 9999, "erin").status_code == 403

    # 受让人未注册
    r = _invite(client, keys["alice"], doc_id, "nobody")
    assert r.status_code == 400
    # 受让人是封存人本人
    assert _invite(client, keys["alice"], doc_id, "alice").status_code == 400
    # 受让人是当前见证人
    assert _invite(client, keys["alice"], doc_id, "bob").status_code == 400

    # 截止时刻在过去
    r = client.post(f"/documents/{doc_id}/handovers",
                    json={"invitee": "erin", "deadline": _in(-1)}, headers=auth(keys["alice"]))
    assert r.status_code == 400
    # 不带时区
    r = client.post(f"/documents/{doc_id}/handovers",
                    json={"invitee": "erin", "deadline": "2026-12-31T12:00:00"},
                    headers=auth(keys["alice"]))
    assert r.status_code == 422


def test_only_one_pending_invitation_per_document(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]

    r = _invite(client, keys["alice"], doc_id, "erin")
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["status"] == "pending"
    assert inv["inviter"] == "alice" and inv["invitee"] == "erin"
    assert "body" not in r.text and BODY not in r.text
    # 响应不含摘要
    assert d["digest"] not in r.text

    # 第二份待确认邀请被拒（即便受让人不同）
    frank = register(client, "frank")
    assert _invite(client, keys["alice"], doc_id, "frank").status_code == 409


def test_invitation_visible_in_me_handovers_and_document(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]
    inv = _invite(client, keys["alice"], doc_id, "erin").json()

    # 受让人能看到发给自己的邀请
    got = client.get("/me/handovers", headers=auth(keys["erin"])).json()["invitations"]
    assert len(got) == 1
    assert got[0]["invitation_id"] == inv["invitation_id"]
    assert got[0]["role"] == "invitee" and got[0]["inviter"] == "alice"

    # 发起人也能看到
    got = client.get("/me/handovers", headers=auth(keys["alice"])).json()["invitations"]
    assert got[0]["role"] == "inviter" and got[0]["invitee"] == "erin"

    # 无关人看不到
    frank = register(client, "frank")
    assert client.get("/me/handovers", headers=auth(frank)).json() == {"invitations": []}

    # 资料详情（参与方）含最新邀请元数据，无正文
    detail = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert detail["latest_handover"]["invitation_id"] == inv["invitation_id"]
    assert "body" not in detail


# ---------------------------------------------------------------------------
# 撤回
# ---------------------------------------------------------------------------


def test_inviter_can_revoke_pending_invitation(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]

    # 受让人不能撤回
    assert client.post(f"/handovers/{inv_id}/revoke", json={"reason": "x"},
                       headers=auth(keys["erin"])).status_code == 403
    # 陌生人无权，不泄露
    assert client.post(f"/handovers/{inv_id}/revoke", json={},
                       headers=auth(register(client, "x1"))).status_code == 403

    r = client.post(f"/handovers/{inv_id}/revoke", json={"reason": "改主意"},
                    headers=auth(keys["alice"]))
    assert r.status_code == 200 and r.json()["status"] == "revoked"

    # 撤回后受让人不能确认
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"])).status_code == 409
    # 重复撤回
    assert client.post(f"/handovers/{inv_id}/revoke", json={},
                       headers=auth(keys["alice"])).status_code == 409

    # 撤回后可重新邀请
    r = _invite(client, keys["alice"], doc_id, "erin")
    assert r.status_code == 201 and r.json()["status"] == "pending"


# ---------------------------------------------------------------------------
# 确认：基础交接
# ---------------------------------------------------------------------------


def test_confirm_transfers_ownership_and_revokes_old_owner(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]

    # 非受让人不能确认（封存人本人 / 见证人 / 陌生人）
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["alice"])).status_code == 403
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["bob"])).status_code == 403
    assert client.post(f"/handovers/{inv_id}/confirm",
                       headers=auth(register(client, "zzz"))).status_code == 403

    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["invitation"]["status"] == "confirmed"
    assert body["current_owner"] == "erin"
    assert body["old_open_request"] is None
    assert "body" not in r.text and BODY not in r.text and dg not in r.text

    # 原封存人立即失去查看/操作权限
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).status_code == 403
    assert client.get(f"/documents/{doc_id}/audit", headers=auth(keys["alice"])).status_code == 403
    # 原封存人不能再发邀请 / 开封请求 / 撤回
    assert _invite(client, keys["alice"], doc_id, "frank").status_code == 403
    assert _open_request(client, keys["alice"], doc_id).status_code == 403

    # 新封存人有权，开封前仍无正文
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).json()
    assert got["status"] == "sealed" and "body" not in got
    # 见证人名单、摘要不变
    assert got["witnesses"] == ["bob", "carol", "dave"]

    # 新封存人可重新申请开封并走两票流程
    req = _open_request(client, keys["erin"], doc_id, minutes=60).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(keys["bob"]))
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(keys["carol"]))
    assert r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).json()
    assert got["body"] == BODY

    # 重复确认已完成邀请拒绝
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"])).status_code == 409


def test_confirm_supersedes_in_progress_open_request(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    req_id = _open_request(client, keys["alice"], doc_id, minutes=60).json()["request_id"]
    # 已有一票
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["bob"]))

    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]
    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 200
    old = r.json()["old_open_request"]
    assert old == {"request_id": req_id, "status": "superseded", "approval_count": 0}

    # 旧请求终态 superseded；旧同意失效——新封存人重新申请从 0 票开始
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).json()
    assert got["latest_open_request"]["status"] == "superseded"

    # 旧请求不能再被同意；新封存人也无法撤销一个已失效请求
    assert client.post(f"/open-requests/{req_id}/approve", json={"digest": dg},
                       headers=auth(keys["carol"])).status_code == 409
    assert client.post(f"/open-requests/{req_id}/revoke", json={},
                       headers=auth(keys["erin"])).status_code == 409
    # 原封存人对旧请求同样无权撤销（已不是 owner）
    assert client.post(f"/open-requests/{req_id}/revoke", json={},
                       headers=auth(keys["alice"])).status_code == 403

    new_req = _open_request(client, keys["erin"], doc_id, minutes=60).json()["request_id"]
    r = client.post(f"/open-requests/{new_req}/approve", json={"digest": dg},
                    headers=auth(keys["bob"]))
    assert r.json()["approval_count"] == 1  # 不是续票

    # 审计：成功交接 + 请求失效均留痕
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["erin"])).json()["records"]
    actions = [r["action"] for r in records]
    assert "request_superseded" in actions and "handover_confirmed" in actions
    assert actions.index("request_superseded") < actions.index("handover_confirmed")


def test_confirm_with_waiting_request_supersedes(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    # waiting：两票集齐但最早开封时刻在未来
    req_id = _open_request(client, keys["alice"], doc_id, minutes=120,
                           earliest_open_at=_in(60)).json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["bob"]))
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["carol"]))

    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]
    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 200
    assert r.json()["old_open_request"]["status"] == "superseded"
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).json()
    assert got["status"] == "sealed" and got["latest_open_request"]["status"] == "superseded"


# ---------------------------------------------------------------------------
# 已开封拒绝交接
# ---------------------------------------------------------------------------


def test_cannot_handover_opened_document(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    req_id = _open_request(client, keys["alice"], doc_id, minutes=60).json()["request_id"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]

    # 两票开封
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["bob"]))
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["carol"]))

    # 已开封：邀请确认被拒，owner 不变
    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 409
    assert BODY not in r.text
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).status_code == 200
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).status_code == 403

    # 已开封资料也不能再发起邀请
    assert _invite(client, keys["alice"], doc_id, "erin").status_code == 409


def test_handover_invitee_is_when_replaced_into_witness_list_rejected(client):
    """邀请发出后受让人被替换进见证名单：确认时按当前名单复核并拒绝。"""
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]
    # 把 dave 换成 erin —— erin 成为当前见证人
    r = client.post(f"/documents/{doc_id}/witness-replacements",
                    json={"old_witness": "dave", "new_witness": "erin"}, headers=auth(keys["alice"]))
    assert r.status_code == 200
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"])).status_code == 409


# ---------------------------------------------------------------------------
# 逾期
# ---------------------------------------------------------------------------


def test_invitation_expiry_flow(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin", minutes=1).json()["invitation_id"]

    # 手工把截止时刻改到过去，模拟到期（避免真实等待）
    import sqlite3
    import os
    from app.config import settings
    db_path = settings.db_path
    conn = sqlite3.connect(db_path)
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    conn.execute("UPDATE handover_invitations SET deadline=? WHERE id=?", (past, inv_id))
    conn.commit(); conn.close()

    # 受让人确认：临界区确定性失效，拒绝
    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 409
    assert BODY not in r.text

    # 逾期后可重新邀请
    r = _invite(client, keys["alice"], doc_id, "erin")
    assert r.status_code == 201 and r.json()["status"] == "pending"
    new_id = r.json()["invitation_id"]
    assert client.post(f"/handovers/{new_id}/confirm", headers=auth(keys["erin"])).status_code == 200

    # 旧的过期邀请确认仍拒绝
    assert client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"])).status_code == 409


def test_sweeper_expires_due_invitations(client, sweep):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id = d["document_id"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin", minutes=1).json()["invitation_id"]

    import sqlite3
    from app.config import settings
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE handover_invitations SET deadline=? WHERE id=?", (past, inv_id))
    conn.commit(); conn.close()

    assert sweep() >= 0
    # 扫描后邀请为 expired，并留审计
    detail = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert detail["latest_handover"]["status"] == "expired"
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["alice"])).json()["records"]
    assert any(r["action"] == "handover_expired" for r in records)
    # 逾期后可重新邀请
    assert _invite(client, keys["alice"], doc_id, "erin").status_code == 201


def test_expired_open_request_settled_on_confirm(client):
    """确认时先做请求到期：过截止仍 pending 的请求 expired，并在 old_open_request 返回。"""
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    req_id = _open_request(client, keys["alice"], doc_id, minutes=1).json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["bob"]))
    inv_id = _invite(client, keys["alice"], doc_id, "erin", minutes=60).json()["invitation_id"]

    # 把开封请求截止时刻改到过去（邀请仍有效）
    import sqlite3
    from app.config import settings
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE open_requests SET deadline=? WHERE id=?", (past, req_id))
    conn.commit(); conn.close()

    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 200
    old = r.json()["old_open_request"]
    assert old == {"request_id": req_id, "status": "expired", "approval_count": 0}
    assert r.json()["current_owner"] == "erin"
    # 审计：请求失效（request_expired）+ 交接
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["erin"])).json()["records"]
    actions = [x["action"] for x in records]
    assert "request_expired" in actions and "handover_confirmed" in actions


def test_due_waiting_opens_before_confirm_and_rejects_handover(client):
    """确认结算时到点 waiting 先开封，交接拒绝；开封先落库。"""
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    # earliest 设在过去、deadline 在未来：两票此刻即可立即 opened；
    # 为制造 waiting->到点，先把 earliest 设未来再改到过去。
    req_id = _open_request(client, keys["alice"], doc_id, minutes=120,
                           earliest_open_at=_in(60)).json()["request_id"]
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["bob"]))
    client.post(f"/open-requests/{req_id}/approve", json={"digest": dg}, headers=auth(keys["carol"]))
    inv_id = _invite(client, keys["alice"], doc_id, "erin", minutes=90).json()["invitation_id"]

    # earliest 到点（改到过去）
    import sqlite3
    from app.config import settings
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE open_requests SET earliest_open_at=? WHERE id=?", (past, req_id))
    conn.commit(); conn.close()

    # 确认临界区先确定性开封，再拒绝交接
    r = client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))
    assert r.status_code == 409
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).json()
    assert got["status"] == "opened" and got["body"] == BODY  # 开封已落库
    # owner 未变
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["erin"])).status_code == 403
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(keys["alice"])).json()["records"]
    assert [x["action"] for x in records].count("opened") == 1
    assert "handover_confirmed" not in [x["action"] for x in records]


# ---------------------------------------------------------------------------
# 交接后见证名单与摘要不变；见证人仍可访问
# ---------------------------------------------------------------------------


def test_witnesses_and_digest_unchanged_after_handover(client):
    keys = _setup(client)
    d = seal(client, keys["alice"], BODY, ["bob", "carol", "dave"])
    doc_id, dg = d["document_id"], d["digest"]
    inv_id = _invite(client, keys["alice"], doc_id, "erin").json()["invitation_id"]
    client.post(f"/handovers/{inv_id}/confirm", headers=auth(keys["erin"]))

    # 见证人仍可查看（开封前无正文）
    got = client.get(f"/documents/{doc_id}", headers=auth(keys["bob"])).json()
    assert got["witnesses"] == ["bob", "carol", "dave"]
    assert "body" not in got
    # 摘要在开封前也不泄露
    assert dg not in client.get(f"/documents/{doc_id}", headers=auth(keys["bob"])).text
    # 原封存人无权
    assert client.get(f"/documents/{doc_id}", headers=auth(keys["alice"])).status_code == 403
