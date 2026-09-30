#!/usr/bin/env python3
"""封存人交接（handover）调用示例（纯标准库，无需第三方依赖）。

覆盖：
1) 发起交接邀请：受让人须已注册、非封存人/当前见证人，截止时刻带时区；
   每份资料同时只允许一份待确认邀请；
2) 撤回未确认邀请后可重新邀请；
3) 受让人凭自己的 X-API-Key 在截止前确认：进行中的开封请求置 superseded、
   旧同意失效，封存人身份转移，见证名单/正文/摘要不变；
4) 交接后原封存人失去查看/操作权限，新封存人重新申请开封；
5) 常见拒绝：已开封不能交接、逾期/撤回不能确认、无权或无效邀请一律
   拒绝且不泄露正文。

用法: python3 examples/demo_handover.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# 每次运行使用唯一用户名，脚本可对同一服务重复执行
TAG = str(int(time.time()))


def call(method, path, key=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def in_hours(h):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=h)).isoformat()


def show(title, status, body):
    print(f"-- {title} -> {status}")
    print("   " + json.dumps(body, ensure_ascii=False))


def main():
    name = lambda role: f"{role}-{TAG}"
    users = {}
    for role in ["alice", "bob", "carol", "dave", "erin"]:
        _, body = call("POST", "/register", payload={"username": name(role)})
        users[role] = body["api_key"]
    alice, bob, carol, dave, erin = (users[r] for r in
                                     ["alice", "bob", "carol", "dave", "erin"])

    # ------------------------------------------------------------------
    # 场景 1：发起邀请 -> 撤回 -> 重新邀请 -> 确认交接
    # ------------------------------------------------------------------
    print("== 场景 1：发起 / 撤回 / 重新邀请 / 确认交接 ==")
    _, sealed = call("POST", "/documents", alice,
                     {"body": "交接示例正文：HANDOVER-DEMO-SECRET",
                      "witnesses": [name("bob"), name("carol"), name("dave")]})
    doc_id, digest = sealed["document_id"], sealed["digest"]

    invite = lambda invitee_key_role, who: call(
        "POST", f"/documents/{doc_id}/handovers", alice,
        {"invitee": who, "deadline": in_hours(24)})

    # 发起阶段的拒绝（每份资料此时尚无待确认邀请，先做这些校验）
    assert invite(alice, f"nobody-{TAG}")[0] == 400   # 未注册
    assert invite(alice, name("alice"))[0] == 400     # 受让人是封存人本人
    assert invite(alice, name("bob"))[0] == 400       # 受让人是当前见证人

    # 正式发起交接邀请给 erin，确认截止时刻 24 小时后（带时区）
    status, inv = invite(alice, name("erin"))
    show("alice 邀请 erin 接管", status, inv)
    assert status == 201 and inv["status"] == "pending"
    inv_id = inv["invitation_id"]
    assert "body" not in json.dumps(inv) and digest not in json.dumps(inv)

    # 每份资料同时只允许一份待确认邀请（受让人不同也不行）
    assert invite(alice, name("carol"))[0] == 409

    # 受让人可查询发给自己的邀请；见证人无权确认
    _, mine = call("GET", "/me/handovers", erin)
    assert mine["invitations"][0]["invitation_id"] == inv_id
    assert call("POST", f"/handovers/{inv_id}/confirm", bob)[0] == 403

    # 封存人撤回；撤回后不能确认，但可重新邀请
    status, rev = call("POST", f"/handovers/{inv_id}/revoke", alice, {"reason": "再想想"})
    show("alice 撤回邀请", status, rev)
    assert status == 200 and rev["status"] == "revoked"
    assert call("POST", f"/handovers/{inv_id}/confirm", erin)[0] == 409

    status, inv = invite(alice, name("erin"))
    assert status == 201
    inv_id = inv["invitation_id"]

    # ------------------------------------------------------------------
    # 场景 2：有进行中开封请求时确认 -> 请求 superseded、旧同意失效，再交接
    # ------------------------------------------------------------------
    print("== 场景 2：进行中开封请求随交接失效，身份转移 ==")
    _, req = call("POST", f"/documents/{doc_id}/open-requests", alice,
                  {"reason": "交接前申请开封", "deadline": in_hours(24)})
    req_id = req["request_id"]
    call("POST", f"/open-requests/{req_id}/approve", carol, {"digest": digest})  # 旧的一票

    status, confirmed = call("POST", f"/handovers/{inv_id}/confirm", erin)
    show("erin 在截止前确认交接", status, confirmed)
    assert status == 200
    assert confirmed["current_owner"] == name("erin")
    # 进行中请求及旧同意失效
    assert confirmed["old_open_request"] == {
        "request_id": req_id, "status": "superseded", "approval_count": 0}
    assert confirmed["invitation"]["status"] == "confirmed"
    assert "body" not in json.dumps(confirmed)  # 开封前不泄露正文

    # 交接后原封存人立即失去查看/操作权限（403，不区分资料是否存在）
    assert call("GET", f"/documents/{doc_id}", alice)[0] == 403
    assert call("POST", f"/documents/{doc_id}/handovers", alice,
                {"invitee": name("dave"), "deadline": in_hours(24)})[0] == 403
    # 见证人仍在名单中、仍可访问，但开封前无正文
    s, got = call("GET", f"/documents/{doc_id}", bob)
    assert s == 200 and got["witnesses"] == [name("bob"), name("carol"), name("dave")]
    assert "body" not in got

    # 新封存人 erin 重新申请开封，旧票不计，从 0 票开始
    _, req2 = call("POST", f"/documents/{doc_id}/open-requests", erin,
                   {"reason": "新封存人申请", "deadline": in_hours(24)})
    s, vote = call("POST", f"/open-requests/{req2['request_id']}/approve", bob, {"digest": digest})
    assert s == 200 and vote["approval_count"] == 1

    # ------------------------------------------------------------------
    # 场景 3：已开封资料不能交接
    # ------------------------------------------------------------------
    print("== 场景 3：已开封拒绝交接 ==")
    _, sealed2 = call("POST", "/documents", alice,
                      {"body": "交接示例正文 2",
                       "witnesses": [name("bob"), name("carol"), name("dave")]})
    doc2, digest2 = sealed2["document_id"], sealed2["digest"]

    # 开封前先发起一份邀请（此时可成功）
    _, inv2 = call("POST", f"/documents/{doc2}/handovers", alice,
                   {"invitee": name("erin"), "deadline": in_hours(24)})
    assert inv2["status"] == "pending"

    # 随后两票开封
    _, r2 = call("POST", f"/documents/{doc2}/open-requests", alice,
                 {"reason": "先开封", "deadline": in_hours(24)})
    call("POST", f"/open-requests/{r2['request_id']}/approve", bob, {"digest": digest2})
    call("POST", f"/open-requests/{r2['request_id']}/approve", carol, {"digest": digest2})

    # 已开封：受让人确认被拒、原封存人也不能再发起邀请（均不泄露正文）
    s, body = call("POST", f"/handovers/{inv2['invitation_id']}/confirm", erin)
    show("已开封后确认交接（应拒绝）", s, body)
    assert s == 409
    s, _ = call("POST", f"/documents/{doc2}/handovers", alice,
                {"invitee": name("dave"), "deadline": in_hours(24)})
    assert s == 409

    print("\n全部交接示例断言通过。审计可查：")
    _, audit = call("GET", f"/documents/{doc_id}/audit", erin)
    print("  actions:", [r["action"] for r in audit["records"]])


if __name__ == "__main__":
    main()
