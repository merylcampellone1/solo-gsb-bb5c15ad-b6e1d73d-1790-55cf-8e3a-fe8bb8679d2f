"""接口与规则版本化行为测试。"""


def make_payload(**kw):
    base = {
        "batch_no": "B20261005-001",
        "region": "default",
        "weight_kg": 100,
        "materials": [{"category": "PET", "contamination_kg": 3}],
        "operator": "tester",
    }
    base.update(kw)
    return base


def fields(resp):
    return {e["field"] for e in resp.json()["errors"]}


# ------------------------------------------------------------------ 正常判定
def test_create_assessment_ok(client):
    r = client.post("/api/assessments", json=make_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["rule_version"] == "v1.0"
    assert d["level"]["code"] == "B"            # 3% 污染率
    assert d["conclusion"] == "降级接收"
    assert d["total_fee"] == 3.6                # 3kg * 1.2 * 1.0
    assert d["currency"] == "CNY"
    assert len(d["fee_lines"]) == 1
    assert any("规则版本" in e for e in d["evidence"])


def test_clean_batch_no_fee(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": 1}]))
    assert r.status_code == 201
    d = r.json()
    assert d["level"]["code"] == "A"
    assert d["conclusion"] == "合格接收"
    assert d["total_fee"] == 0


def test_reject_batch(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "其他", "contamination_kg": 50}]))
    assert r.status_code == 201
    assert r.json()["conclusion"] == "拒收"


# ------------------------------------------------------------------ 字段校验
def test_negative_weight_rejected(client):
    r = client.post("/api/assessments", json=make_payload(weight_kg=-5))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)


def test_zero_weight_rejected(client):
    r = client.post("/api/assessments", json=make_payload(weight_kg=0))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)


def test_negative_contamination_rejected(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": -1}]))
    assert r.status_code == 422
    assert "materials[0].contamination_kg" in fields(r)


def test_single_contamination_exceeds_weight(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "PET", "contamination_kg": 150}]))
    assert r.status_code == 422
    assert "materials[0].contamination_kg" in fields(r)


def test_total_contamination_exceeds_weight(client):
    r = client.post("/api/assessments", json=make_payload(materials=[
        {"category": "PET", "contamination_kg": 60},
        {"category": "纸类", "contamination_kg": 60},
    ]))
    assert r.status_code == 422
    assert "materials" in fields(r)


def test_unknown_category_rejected(client):
    r = client.post("/api/assessments", json=make_payload(
        materials=[{"category": "锂电池", "contamination_kg": 1}]))
    assert r.status_code == 422
    assert "materials[0].category" in fields(r)
    assert "未知物料类别" in r.json()["errors"][0]["message"]


def test_unknown_region_rejected(client):
    r = client.post("/api/assessments", json=make_payload(region="不存在区"))
    assert r.status_code == 422
    assert "region" in fields(r)


def test_empty_materials_rejected(client):
    r = client.post("/api/assessments", json=make_payload(materials=[]))
    assert r.status_code == 422


# -------------------------------------------------------------- 规则版本固定
def test_history_pinned_after_rule_switch(client):
    # 切换前出具：v1.0，PET 3kg/100kg → B 级，扣费 3.6
    r1 = client.post("/api/assessments", json=make_payload())
    assert r1.status_code == 201
    aid = r1.json()["id"]
    assert r1.json()["rule_version"] == "v1.0"
    assert r1.json()["total_fee"] == 3.6

    # 切换到 v1.1（费率更高、阈值更严）
    r = client.post("/api/rules/default/activate", json={"version": "v1.1"})
    assert r.status_code == 200

    # 新判定使用 v1.1：3kg * 1.5 = 4.5
    r2 = client.post("/api/assessments", json=make_payload(batch_no="B002"))
    assert r2.json()["rule_version"] == "v1.1"
    assert r2.json()["total_fee"] == 4.5

    # 历史判定单保持出具时的版本与结论，不被改写
    old = client.get(f"/api/assessments/{aid}")
    assert old.status_code == 200
    assert old.json()["rule_version"] == "v1.0"
    assert old.json()["total_fee"] == 3.6
    assert old.json()["conclusion"] == "降级接收"


def test_explicit_rule_version(client):
    r = client.post("/api/assessments",
                    json=make_payload(rule_version="v1.1"))
    assert r.status_code == 201
    assert r.json()["rule_version"] == "v1.1"


def test_activate_unknown_version_404(client):
    r = client.post("/api/rules/default/activate", json={"version": "v9.9"})
    assert r.status_code == 404


def test_create_and_activate_new_rule(client):
    new_rule = {
        "region": "华南",
        "version": "v1.0",
        "activate": True,
        "categories": {"PET": {"fee_per_kg": 1.0, "desc": "PET"}},
        "levels": [
            {"code": "A", "name": "合格", "max_ratio": 0.05,
             "conclusion": "合格接收", "fee_multiplier": 0},
            {"code": "B", "name": "超标", "max_ratio": 1.0,
             "conclusion": "拒收", "fee_multiplier": 1.0},
        ],
    }
    r = client.post("/api/rules", json=new_rule)
    assert r.status_code == 201, r.text
    r = client.post("/api/assessments", json=make_payload(
        region="华南", materials=[{"category": "PET", "contamination_kg": 10}]))
    assert r.status_code == 201
    assert r.json()["conclusion"] == "拒收"


def test_duplicate_rule_version_conflict(client):
    rule = {
        "region": "default", "version": "v1.0",
        "categories": {"PET": {"fee_per_kg": 1.0}},
        "levels": [{"code": "A", "name": "合格", "max_ratio": 1.0,
                    "conclusion": "合格接收", "fee_multiplier": 0}],
    }
    r = client.post("/api/rules", json=rule)
    assert r.status_code == 409


# ------------------------------------------------------------------ 查询导出
def test_query_and_export(client):
    client.post("/api/assessments", json=make_payload())
    client.post("/api/assessments", json=make_payload(
        batch_no="B-HD-01", region="华东",
        materials=[{"category": "厨余", "contamination_kg": 10}]))

    r = client.get("/api/assessments", params={"region": "华东"})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["batch_no"] == "B-HD-01"

    r = client.get("/api/assessments", params={"batch_no": "B20261005"})
    assert len(r.json()["items"]) == 1

    r = client.get("/api/export/assessments.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "B20261005-001" in r.text and "B-HD-01" in r.text


def test_audit_log_and_export(client):
    client.post("/api/assessments", json=make_payload())
    client.post("/api/rules/default/activate",
                json={"version": "v1.1", "actor": "admin"})

    r = client.get("/api/audit")
    assert r.status_code == 200
    actions = [i["action"] for i in r.json()["items"]]
    assert "assessment.created" in actions
    assert "rule.activated" in actions

    r = client.get("/api/audit", params={"entity": "rule"})
    assert all(i["entity"] == "rule" for i in r.json()["items"])

    r = client.get("/api/export/audit.csv")
    assert r.status_code == 200
    assert "rule.activated" in r.text


def test_assessment_not_found(client):
    r = client.get("/api/assessments/NOPE")
    assert r.status_code == 404


# ------------------------------------------------------------------ 判定修订
def revise_payload(base_id, **kw):
    base = {
        "base_id": base_id,
        "weight_kg": 120,
        "materials": [{"category": "PET", "contamination_kg": 2}],
        "reason": "称重录入有误，按复检重量重算",
        "operator": "复检员甲",
    }
    base.update(kw)
    return base


def create_one(client, **kw):
    r = client.post("/api/assessments", json=make_payload(**kw))
    assert r.status_code == 201, r.text
    return r.json()


def test_revision_ok(client):
    a1 = create_one(client)
    assert a1["version"] == 1 and a1["is_current"] and a1["root_id"] == a1["id"]

    r = client.post(f"/api/assessments/{a1['id']}/revisions",
                    json=revise_payload(a1["id"]))
    assert r.status_code == 201, r.text
    a2 = r.json()
    # 新单关联前版、沿用原单批次/区域/规则快照复算
    assert a2["id"] != a1["id"]
    assert a2["version"] == 2
    assert a2["root_id"] == a1["id"]
    assert a2["parent_id"] == a1["id"]
    assert a2["is_current"]
    assert a2["batch_no"] == a1["batch_no"]
    assert a2["region"] == a1["region"]
    assert a2["rule_version"] == a1["rule_version"]
    assert a2["weight_kg"] == 120
    assert a2["revision_reason"] == "称重录入有误，按复检重量重算"
    assert a2["operator"] == "复检员甲"
    # 2kg / 120kg ≈ 1.67% → A 级合格，免扣费
    assert a2["level"]["code"] == "A"
    assert a2["conclusion"] == "合格接收"
    assert a2["total_fee"] == 0
    assert any("修订版 v2" in e and a1["id"] in e for e in a2["evidence"])
    assert any("修订原因" in e for e in a2["evidence"])

    # 原单保持只读、内容不变，仅标记为已被修订
    old = client.get(f"/api/assessments/{a1['id']}").json()
    assert old["version"] == 1
    assert not old["is_current"]
    assert old["weight_kg"] == a1["weight_kg"]
    assert old["conclusion"] == a1["conclusion"]
    assert old["total_fee"] == a1["total_fee"]


def test_revision_pinned_to_original_snapshot(client):
    a1 = create_one(client)  # v1.0：PET 3kg/100kg → B 级，扣费 3.6
    assert a1["rule_version"] == "v1.0"

    # 切换区域启用版本后再修订：复算仍用原单 v1.0 快照
    r = client.post("/api/rules/default/activate", json={"version": "v1.1"})
    assert r.status_code == 200

    r = client.post(f"/api/assessments/{a1['id']}/revisions",
                    json=revise_payload(a1["id"], weight_kg=100,
                                        materials=[{"category": "PET",
                                                    "contamination_kg": 3}]))
    assert r.status_code == 201
    a2 = r.json()
    assert a2["rule_version"] == "v1.0"      # 不随区域规则切换改变
    assert a2["total_fee"] == 3.6            # v1.0 费率 1.2；v1.1 应为 4.5
    assert a2["conclusion"] == "降级接收"


def test_revision_chain_tail_only(client):
    a1 = create_one(client)
    a2 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"])).json()

    # 旧版本（非链尾）不可再被修订
    r = client.post(f"/api/assessments/{a1['id']}/revisions",
                    json=revise_payload(a1["id"]))
    assert r.status_code == 409
    assert "base_id" in fields(r)

    # 链尾可以继续修订，版本号递增
    r = client.post(f"/api/assessments/{a1['id']}/revisions",
                    json=revise_payload(a2["id"], weight_kg=80))
    assert r.status_code == 201
    a3 = r.json()
    assert a3["version"] == 3
    assert a3["parent_id"] == a2["id"]
    assert a3["root_id"] == a1["id"]


def test_revision_contention_single_winner(client):
    # 两个请求争用同一尾版：只接受一个，其余返回明确冲突
    a1 = create_one(client)
    r1 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"], weight_kg=90))
    r2 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"], weight_kg=110))
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert "链尾" in r2.json()["errors"][0]["message"]

    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 2
    assert chain["current_id"] == r1.json()["id"]


def test_revision_concurrent_threads(client):
    import concurrent.futures

    a1 = create_one(client)

    def revise(w):
        return client.post(f"/api/assessments/{a1['id']}/revisions",
                           json=revise_payload(a1["id"], weight_kg=w))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = [f.result().status_code for f in
                 [pool.submit(revise, 100 + i) for i in range(4)]]
    assert codes.count(201) == 1
    assert codes.count(409) == 3
    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 2


def test_revision_not_found(client):
    r = client.post("/api/assessments/NOPE/revisions",
                    json=revise_payload("NOPE"))
    assert r.status_code == 404
    # 不产生审计记录
    actions = [i["action"] for i in client.get("/api/audit").json()["items"]]
    assert "assessment.revised" not in actions


def test_revision_validation_failure_no_side_effects(client):
    a1 = create_one(client)
    before = len(client.get("/api/audit").json()["items"])

    r = client.post(f"/api/assessments/{a1['id']}/revisions",
                    json=revise_payload(a1["id"], weight_kg=-1,
                                        materials=[{"category": "锂电池",
                                                    "contamination_kg": 1}]))
    assert r.status_code == 422
    assert "weight_kg" in fields(r)
    assert "materials[0].category" in fields(r)

    # 校验失败：不产生新单据、不产生审计记录
    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 1
    after = client.get("/api/audit").json()["items"]
    assert len(after) == before
    assert all(i["action"] != "assessment.revised" for i in after)


def test_revision_missing_reason_or_operator(client):
    a1 = create_one(client)
    for kw in ({"reason": ""}, {"operator": ""}):
        r = client.post(f"/api/assessments/{a1['id']}/revisions",
                        json=revise_payload(a1["id"], **kw))
        assert r.status_code == 422
    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 1


def test_revision_audit_records_reason_and_operator(client):
    a1 = create_one(client)
    client.post(f"/api/assessments/{a1['id']}/revisions",
                json=revise_payload(a1["id"], reason="杂质类别录入错误",
                                    operator="李四"))
    items = client.get("/api/audit", params={"entity": "assessment"}).json()["items"]
    revised = [i for i in items if i["action"] == "assessment.revised"]
    assert len(revised) == 1
    rec = revised[0]
    assert rec["actor"] == "李四"
    import json as _json
    detail = _json.loads(rec["detail"])
    assert detail["reason"] == "杂质类别录入错误"
    assert detail["root_id"] == a1["id"]
    assert detail["version"] == 2


def test_revision_chain_query(client):
    a1 = create_one(client)
    a2 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"])).json()
    a3 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a2["id"], weight_kg=60,
                                         materials=[{"category": "其他",
                                                    "contamination_kg": 30}])).json()

    # 按原单号查询：当前有效结论 + 完整修订链
    r = client.get(f"/api/assessments/{a1['id']}/revisions")
    assert r.status_code == 200
    chain = r.json()
    assert chain["root_id"] == a1["id"]
    assert chain["current_id"] == a3["id"]
    assert [i["version"] for i in chain["items"]] == [1, 2, 3]
    assert [i["id"] for i in chain["items"]] == [a1["id"], a2["id"], a3["id"]]
    assert chain["items"][0]["is_current"] is False
    assert chain["items"][-1]["is_current"] is True
    # 当前有效结论为链尾结论（30kg/60kg → 拒收）
    assert chain["current"]["conclusion"] == "拒收"
    assert chain["current"]["id"] == a3["id"]
    assert chain["items"][1]["revision_reason"]

    # 链内任意单号均可追溯同一条链
    for aid in (a2["id"], a3["id"]):
        r = client.get(f"/api/assessments/{aid}/revisions")
        assert r.status_code == 200
        assert r.json()["root_id"] == a1["id"]
        assert len(r.json()["items"]) == 3

    r = client.get("/api/assessments/NOPE/revisions")
    assert r.status_code == 404


def test_revision_list_and_csv_compatibility(client):
    a1 = create_one(client)
    client.post(f"/api/assessments/{a1['id']}/revisions",
                json=revise_payload(a1["id"]))

    items = client.get("/api/assessments").json()["items"]
    assert len(items) == 2
    for it in items:
        # 既有字段保持，新增修订链字段
        for f in ("id", "batch_no", "region", "weight_kg", "conclusion",
                  "total_fee", "rule_version", "operator", "created_at"):
            assert f in it
        for f in ("version", "is_current", "root_id", "parent_id",
                  "revision_reason"):
            assert f in it
    # current 过滤只保留各链尾
    current = client.get("/api/assessments", params={"current": "true"}).json()
    assert len(current["items"]) == 1
    assert current["items"][0]["version"] == 2

    r = client.get("/api/export/assessments.csv")
    assert r.status_code == 200
    header = r.text.splitlines()[0]
    assert header.startswith("﻿id,batch_no,region,weight_kg")
    assert "version" in header and "is_current" in header
    assert "revision_reason" in header


# ------------------------------------------------------------------ 判定复核
def test_review_ok_with_fee_diff(client):
    # 原单：v1.0，PET 3kg/100kg → B 级，扣费 3.6
    a1 = create_one(client)
    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v1.1"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["assessment_id"] == a1["id"]
    assert d["batch_no"] == a1["batch_no"]
    assert d["region"] == "default"
    assert d["weight_kg"] == 100
    assert d["materials"] == [{"category": "PET", "contamination_kg": 3}]
    # 原结论与目标版本结果分别返回
    assert d["original"]["rule_version"] == "v1.0"
    assert d["original"]["level"]["code"] == "B"
    assert d["original"]["total_fee"] == 3.6
    assert d["review"]["rule_version"] == "v1.1"
    assert d["review"]["level"]["code"] == "B"        # 3% ≤ 5%，仍为 B
    assert d["review"]["total_fee"] == 4.5            # 3kg × 1.5 × 1.0
    assert any("default/v1.1" in e for e in d["review"]["evidence"])
    # 差异项
    diff = d["diff"]
    assert diff["level_changed"] is False
    assert diff["conclusion_changed"] is False
    assert diff["total_fee_delta"] == 0.9
    assert len(diff["fee_line_diffs"]) == 1
    line = diff["fee_line_diffs"][0]
    assert line["category"] == "PET"
    assert line["original_fee_per_kg"] == 1.2
    assert line["review_fee_per_kg"] == 1.5
    assert line["original_fee"] == 3.6
    assert line["review_fee"] == 4.5
    assert line["delta"] == 0.9
    assert any("扣费合计 3.60 → 4.50" in i for i in diff["items"])


def test_review_level_and_conclusion_change(client):
    # 1.8kg/100kg = 1.8%：v1.0 命中 A（≤2%），v1.1 命中 B（>1.5%）
    a1 = create_one(client,
                    materials=[{"category": "PET", "contamination_kg": 1.8}])
    assert a1["level"]["code"] == "A"
    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v1.1"})
    assert r.status_code == 200
    d = r.json()
    assert d["original"]["conclusion"] == "合格接收"
    assert d["review"]["level"]["code"] == "B"
    assert d["review"]["conclusion"] == "降级接收"
    diff = d["diff"]
    assert diff["level_changed"] is True
    assert diff["conclusion_changed"] is True
    assert diff["total_fee_delta"] == 2.7          # 1.8 × 1.5 × 1.0
    assert any("等级变化：A" in i and "→ B" in i for i in diff["items"])
    assert any("合格接收 → 降级接收" in i for i in diff["items"])


def test_review_same_version_no_diff(client):
    a1 = create_one(client)
    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v1.0"})
    assert r.status_code == 200
    diff = r.json()["diff"]
    assert diff["level_changed"] is False
    assert diff["conclusion_changed"] is False
    assert diff["total_fee_delta"] == 0
    assert diff["fee_line_diffs"] == []
    assert "复核结果与原判定一致。" in diff["items"]


def test_review_read_only_no_side_effects(client):
    a1 = create_one(client)
    before_audit = len(client.get("/api/audit").json()["items"])

    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v1.1"})
    assert r.status_code == 200

    # 原单内容不变、修订链不增长、不产生审计记录
    old = client.get(f"/api/assessments/{a1['id']}").json()
    assert old["rule_version"] == "v1.0"
    assert old["total_fee"] == 3.6
    assert old["is_current"] is True
    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 1
    after_audit = client.get("/api/audit").json()["items"]
    assert len(after_audit) == before_audit


def test_review_assessment_not_found(client):
    r = client.post("/api/assessments/NOPE/review",
                    json={"rule_version": "v1.1"})
    assert r.status_code == 404
    assert "id" in fields(r)


def test_review_rule_version_not_found(client):
    a1 = create_one(client)
    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v9.9"})
    assert r.status_code == 404
    assert "rule_version" in fields(r)


def test_review_region_mismatch_rejected(client):
    a1 = create_one(client)  # 属于 default 区域
    # 目标版本在华东确实存在，但区域与原单不一致
    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"region": "华东", "rule_version": "v1.0"})
    assert r.status_code == 422
    assert "region" in fields(r)
    assert "区域不一致" in r.json()["errors"][0]["message"]


def test_review_target_rule_validation_applies(client):
    # 新增只含 PET 的 default/v2.0；原单含纸类，按 v2.0 重算应被类别校验拒绝
    new_rule = {
        "region": "default", "version": "v2.0",
        "categories": {"PET": {"fee_per_kg": 2.0, "desc": "PET"}},
        "levels": [{"code": "A", "name": "合格", "max_ratio": 1.0,
                    "conclusion": "合格接收", "fee_multiplier": 0}],
    }
    assert client.post("/api/rules", json=new_rule).status_code == 201
    a1 = create_one(client, materials=[{"category": "纸类",
                                        "contamination_kg": 2}])
    before_audit = len(client.get("/api/audit").json()["items"])

    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v2.0"})
    assert r.status_code == 422
    assert "materials[0].category" in fields(r)
    assert "未知物料类别" in r.json()["errors"][0]["message"]
    # 校验失败同样只读：无新单据、无新审计
    chain = client.get(f"/api/assessments/{a1['id']}/revisions").json()
    assert len(chain["items"]) == 1
    assert len(client.get("/api/audit").json()["items"]) == before_audit


def test_review_missing_details_rejected(client, store):
    import json as _json
    import sqlite3

    a1 = create_one(client)
    # 模拟历史遗留数据：request_json 中缺少物料明细
    conn = sqlite3.connect(store.path)
    req = _json.loads(conn.execute(
        "SELECT request_json FROM assessments WHERE id=?",
        (a1["id"],)).fetchone()[0])
    req.pop("materials")
    conn.execute("UPDATE assessments SET request_json=? WHERE id=?",
                 (_json.dumps(req, ensure_ascii=False), a1["id"]))
    conn.commit()
    conn.close()

    r = client.post(f"/api/assessments/{a1['id']}/review",
                    json={"rule_version": "v1.1"})
    assert r.status_code == 422
    assert "缺少可重算明细" in r.json()["errors"][0]["message"]


# ------------------------------------------------------------------ 费用结算
def settle(client, ids, **kw):
    payload = {"region": "default", "assessment_ids": ids,
               "request_id": "REQ-0001", "operator": "结算员"}
    payload.update(kw)
    return client.post("/api/settlements", json=payload)


def test_settlement_ok_single_chain(client):
    a1 = create_one(client)  # PET 3kg/100kg → B 级，扣费 3.6
    r = settle(client, [a1["id"]])
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["region"] == "default"
    assert d["currency"] == "CNY"
    assert d["request_id"] == "REQ-0001"
    assert d["total_delta"] == 3.6
    assert len(d["items"]) == 1
    item = d["items"][0]
    assert item["assessment_id"] == a1["id"]
    assert item["root_id"] == a1["id"]
    assert item["version"] == 1
    assert item["original_fee"] == 3.6
    assert item["prev_settled_id"] is None
    assert item["prev_fee"] is None
    assert item["delta"] == 3.6


def test_settlement_query_detail(client):
    a1 = create_one(client)                                   # 扣费 3.6
    a2 = create_one(client, batch_no="B002",
                    materials=[{"category": "纸类",
                                "contamination_kg": 5}])      # 5kg × 0.6 = 3.0
    r = settle(client, [a1["id"], a2["id"]])
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    # 详情可见来源单号、原扣费、差额及合计
    d = client.get(f"/api/settlements/{sid}").json()
    assert d["id"] == sid
    assert d["currency"] == "CNY"
    by_aid = {it["assessment_id"]: it for it in d["items"]}
    assert by_aid[a1["id"]]["original_fee"] == 3.6
    assert by_aid[a1["id"]]["delta"] == 3.6
    assert by_aid[a2["id"]]["original_fee"] == 3.0
    assert by_aid[a2["id"]]["delta"] == 3.0
    assert d["total_delta"] == 6.6

    lst = client.get("/api/settlements").json()["items"]
    assert len(lst) == 1
    assert lst[0]["id"] == sid
    assert lst[0]["item_count"] == 2
    assert lst[0]["total_delta"] == 6.6

    r = client.get("/api/settlements", params={"region": "华东"})
    assert r.json()["items"] == []
    assert client.get("/api/settlements/NOPE").status_code == 404


def test_settlement_audit_trail(client):
    a1 = create_one(client)
    sid = settle(client, [a1["id"]], request_id="REQ-AUD").json()["id"]
    items = client.get("/api/audit",
                       params={"entity": "settlement"}).json()["items"]
    assert len(items) == 1
    rec = items[0]
    assert rec["action"] == "settlement.created"
    assert rec["actor"] == "结算员"
    assert rec["entity_id"] == sid
    import json as _json
    detail = _json.loads(rec["detail"])
    assert detail["request_id"] == "REQ-AUD"
    assert detail["total_delta"] == 3.6
    assert detail["items"][0]["assessment_id"] == a1["id"]


def test_settlement_idempotent_replay_returns_original(client):
    a1 = create_one(client)
    r1 = settle(client, [a1["id"]], request_id="REQ-IDEM")
    assert r1.status_code == 201
    before_audit = len(client.get("/api/audit").json()["items"])

    # 同一请求号 + 相同内容：返回原单，不重复入账、不重复审计
    r2 = settle(client, [a1["id"]], request_id="REQ-IDEM")
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    assert r2.json()["total_delta"] == r1.json()["total_delta"]
    assert len(client.get("/api/settlements").json()["items"]) == 1
    assert len(client.get("/api/audit").json()["items"]) == before_audit


def test_settlement_request_id_content_conflict(client):
    a1 = create_one(client)
    a2 = create_one(client, batch_no="B002")
    assert settle(client, [a1["id"]], request_id="REQ-X").status_code == 201
    # 同一请求号但内容不同：冲突，不生成新单
    r = settle(client, [a2["id"]], request_id="REQ-X")
    assert r.status_code == 409
    assert "request_id" in fields(r)
    assert len(client.get("/api/settlements").json()["items"]) == 1


def test_settlement_empty_list_rejected(client):
    r = settle(client, [])
    assert r.status_code == 422
    assert "assessment_ids" in fields(r)
    assert client.get("/api/settlements").json()["items"] == []


def test_settlement_unknown_assessment_404(client):
    r = settle(client, ["NOPE"])
    assert r.status_code == 404
    assert "assessment_ids" in fields(r)


def test_settlement_non_tail_rejected(client):
    a1 = create_one(client)
    a2 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"])).json()
    # 旧版本不是链尾，不能结算
    r = settle(client, [a1["id"]])
    assert r.status_code == 422
    assert "链尾" in r.json()["errors"][0]["message"]
    # 新链尾可以正常结算
    assert settle(client, [a2["id"]]).status_code == 201


def test_settlement_cross_region_rejected(client):
    a1 = create_one(client)
    a2 = create_one(client, batch_no="B-HD", region="华东",
                    materials=[{"category": "厨余", "contamination_kg": 10}])
    r = settle(client, [a1["id"], a2["id"]])  # 请求区域 default
    assert r.status_code == 422
    assert "region" in fields(r)
    assert "区域" in r.json()["errors"][0]["message"]
    assert client.get("/api/settlements").json()["items"] == []


def test_settlement_currency_mismatch_rejected(client):
    # default 区域新增 USD 币种版本并启用，出具一张 USD 判定单
    usd_rule = {
        "region": "default", "version": "v9.9", "activate": True,
        "currency": "USD",
        "categories": {"PET": {"fee_per_kg": 1.0}},
        "levels": [{"code": "B", "name": "超标", "max_ratio": 1.0,
                    "conclusion": "拒收", "fee_multiplier": 1.0}],
    }
    assert client.post("/api/rules", json=usd_rule).status_code == 201
    a_cny = create_one(client, rule_version="v1.0")   # CNY
    a_usd = create_one(client, batch_no="B-USD")      # USD（v9.9 已启用）
    assert a_usd["currency"] == "USD"

    r = settle(client, [a_cny["id"], a_usd["id"]])
    assert r.status_code == 422
    assert "币种不一致" in r.json()["errors"][0]["message"]
    assert client.get("/api/settlements").json()["items"] == []


def test_settlement_delta_after_revision_and_old_bill_unchanged(client):
    a1 = create_one(client)  # B 级，扣费 3.6
    s1 = settle(client, [a1["id"]], request_id="REQ-1").json()
    assert s1["total_delta"] == 3.6

    # 已结算链仍可按原有规则修订：复检后 A 级免扣费
    a2 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a1["id"])).json()
    assert a2["total_fee"] == 0

    # 再次选择新链尾：仅计入与上次已结算版本的差额（可为负）
    r = settle(client, [a2["id"]], request_id="REQ-2")
    assert r.status_code == 201
    s2 = r.json()
    item = s2["items"][0]
    assert item["assessment_id"] == a2["id"]
    assert item["version"] == 2
    assert item["original_fee"] == 0
    assert item["prev_settled_id"] == a1["id"]
    assert item["prev_fee"] == 3.6
    assert item["delta"] == -3.6
    assert s2["total_delta"] == -3.6

    # 旧结算单不变
    old = client.get(f"/api/settlements/{s1['id']}").json()
    assert old["total_delta"] == 3.6
    assert old["items"][0]["delta"] == 3.6

    # 再修订为更高扣费并第三次结算：差额基于上次已结算版本 v2
    a3 = client.post(f"/api/assessments/{a1['id']}/revisions",
                     json=revise_payload(a2["id"], weight_kg=100,
                                         materials=[{"category": "PET",
                                                     "contamination_kg": 6}]
                                         )).json()
    assert a3["total_fee"] == 7.2
    s3 = settle(client, [a3["id"]], request_id="REQ-3").json()
    assert s3["items"][0]["prev_settled_id"] == a2["id"]
    assert s3["items"][0]["prev_fee"] == 0
    assert s3["items"][0]["delta"] == 7.2
    assert s3["total_delta"] == 7.2


def test_settlement_same_version_conflict(client):
    a1 = create_one(client)
    assert settle(client, [a1["id"]], request_id="REQ-1").status_code == 201
    # 链尾未变：同一链同一版本不能重复入账
    r = settle(client, [a1["id"]], request_id="REQ-2")
    assert r.status_code == 409
    assert "已结算" in r.json()["errors"][0]["message"]
    assert len(client.get("/api/settlements").json()["items"]) == 1


def test_settlement_concurrent_single_winner(client):
    import concurrent.futures

    a1 = create_one(client)

    def do_settle(i):
        return settle(client, [a1["id"]], request_id=f"REQ-C{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = [f.result().status_code for f in
                 [pool.submit(do_settle, i) for i in range(4)]]
    assert codes.count(201) == 1
    assert codes.count(409) == 3
    items = client.get("/api/settlements").json()["items"]
    assert len(items) == 1
    assert items[0]["total_delta"] == 3.6


def test_settlement_failure_no_side_effects(client):
    a1 = create_one(client)
    before = len(client.get("/api/audit").json()["items"])

    assert settle(client, []).status_code == 422
    assert settle(client, ["NOPE"], request_id="REQ-F2").status_code == 404
    assert settle(client, [a1["id"], a1["id"]],
                  request_id="REQ-F3").status_code == 422
    # 各类失败均不产生结算单与审计
    assert client.get("/api/settlements").json()["items"] == []
    assert len(client.get("/api/audit").json()["items"]) == before

    # 失败不占用请求号：修正内容后可重新提交成功
    r = settle(client, [a1["id"]], request_id="REQ-F2")
    assert r.status_code == 201
    actions = [i["action"] for i in client.get("/api/audit").json()["items"]]
    assert actions.count("settlement.created") == 1
