"""HTTP API 与页面入口。"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import rules as engine
from .storage import Store

app = FastAPI(title="回收分拣来料污染判定", version="1.0.0")

_store = Store()


def get_store() -> Store:
    return _store


# ------------------------------------------------------------------ 请求模型
class MaterialItem(BaseModel):
    category: str = Field(min_length=1, max_length=32)
    contamination_kg: float


class AssessmentIn(BaseModel):
    batch_no: str = Field(min_length=1, max_length=64)
    region: str = Field(min_length=1, max_length=32)
    weight_kg: float
    materials: list[MaterialItem] = Field(min_length=1)
    rule_version: str | None = Field(default=None, max_length=32)
    operator: str | None = Field(default=None, max_length=32)


class RevisionIn(BaseModel):
    """修订提交：base_id 为提交方所见的最新单号（必须是链尾）。"""
    base_id: str = Field(min_length=1, max_length=64)
    weight_kg: float
    materials: list[MaterialItem] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=200)
    operator: str = Field(min_length=1, max_length=32)


class RuleIn(BaseModel):
    region: str = Field(min_length=1, max_length=32)
    version: str = Field(min_length=1, max_length=32)
    currency: str = Field(default="CNY", max_length=8)
    categories: dict[str, dict[str, Any]]
    levels: list[dict[str, Any]]
    activate: bool = False
    actor: str | None = Field(default=None, max_length=32)


class ActivateIn(BaseModel):
    version: str = Field(min_length=1, max_length=32)
    actor: str | None = Field(default=None, max_length=32)


class ReviewIn(BaseModel):
    """复核请求：目标规则版本；region 缺省取原单区域，提供时须与原单一致。"""
    rule_version: str = Field(min_length=1, max_length=32)
    region: str | None = Field(default=None, max_length=32)


class SettlementIn(BaseModel):
    """结算提交：区域、当前有效（链尾）单号列表与幂等请求号。"""
    region: str = Field(min_length=1, max_length=32)
    assessment_ids: list[str] = Field(min_length=1, max_length=500)
    request_id: str = Field(min_length=1, max_length=64)
    operator: str | None = Field(default=None, max_length=32)


# ------------------------------------------------------------------ 错误处理
def _err(errors: list[dict], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": errors})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError):
    errors = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"] if p != "body")
        errors.append({"field": loc or "body", "message": e["msg"]})
    return _err(errors)


# ------------------------------------------------------------------ 规则接口
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/rules")
def list_rules(region: str | None = None, store: Store = Depends(get_store)):
    return {"rules": store.list_rules(region)}


@app.get("/api/rules/{region}/active")
def active_rule(region: str, store: Store = Depends(get_store)):
    rule = store.get_active_rule(region)
    if not rule:
        return _err([{"field": "region",
                      "message": f"区域 {region} 没有启用中的规则版本"}], 404)
    return rule


@app.post("/api/rules", status_code=201)
def create_rule(payload: RuleIn, store: Store = Depends(get_store)):
    body = payload.model_dump()
    errors = engine.validate_rule(body)
    if errors:
        return _err(errors)
    try:
        store.create_rule(body)
    except ValueError as exc:
        return _err([{"field": "version", "message": str(exc)}], 409)
    store.add_audit(payload.actor, "rule.created", "rule",
                    f"{payload.region}/{payload.version}",
                    {"region": payload.region, "version": payload.version,
                     "activate": payload.activate})
    return {"region": payload.region, "version": payload.version,
            "status": "active" if payload.activate else "inactive"}


@app.post("/api/rules/{region}/activate")
def activate_rule(region: str, payload: ActivateIn,
                  store: Store = Depends(get_store)):
    if not store.activate_rule(region, payload.version):
        return _err([{"field": "version",
                      "message": f"区域 {region} 不存在规则版本 {payload.version}"}],
                    404)
    store.add_audit(payload.actor, "rule.activated", "rule",
                    f"{region}/{payload.version}",
                    {"region": region, "version": payload.version})
    return {"region": region, "active_version": payload.version}


# ---------------------------------------------------------------- 判定单接口
def _resolve_rule(store: Store, region: str, version: str | None):
    """解析本次判定使用的规则版本；失败时返回字段错误。"""
    if version:
        rule = store.get_rule(region, version)
        if not rule:
            return None, {"field": "rule_version",
                          "message": f"区域 {region} 不存在规则版本 {version}"}
        return rule, None
    rule = store.get_active_rule(region)
    if not rule:
        if store.list_rules(region):
            return None, {"field": "region",
                          "message": f"区域 {region} 没有启用中的规则版本"}
        return None, {"field": "region", "message": f"未知来源区域：{region}"}
    return rule, None


@app.post("/api/assessments", status_code=201)
def create_assessment(payload: AssessmentIn, store: Store = Depends(get_store)):
    rule, err = _resolve_rule(store, payload.region, payload.rule_version)
    if err:
        return _err([err])
    materials = [m.model_dump() for m in payload.materials]
    errors = engine.validate_submission(rule, payload.weight_kg, materials)
    if errors:
        return _err(errors)
    result = engine.evaluate(rule, payload.weight_kg, materials)
    rec = store.create_assessment(payload.model_dump(), rule, result)
    store.add_audit(payload.operator, "assessment.created", "assessment", rec["id"],
                    {"batch_no": payload.batch_no, "region": payload.region,
                     "rule_version": rule["version"],
                     "level": result["level"]["code"],
                     "conclusion": result["conclusion"],
                     "total_fee": result["total_fee"]})
    return rec


@app.get("/api/assessments")
def list_assessments(region: str | None = None, level: str | None = None,
                     batch_no: str | None = None, limit: int = 50, offset: int = 0,
                     current: bool = False,
                     store: Store = Depends(get_store)):
    limit = max(1, min(limit, 500))
    return {"items": store.list_assessments(region, level, batch_no, limit, offset,
                                            current)}


@app.get("/api/assessments/{aid}")
def get_assessment(aid: str, store: Store = Depends(get_store)):
    rec = store.get_assessment(aid)
    if not rec:
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    return rec


# ---------------------------------------------------------------- 修订接口
def _conflict_err(tail_id: str) -> JSONResponse:
    return _err([{"field": "base_id",
                  "message": f"修订冲突：所依据的单号已不是最新版本，"
                             f"当前链尾为 {tail_id}，请获取最新单号后重试"}], 409)


@app.post("/api/assessments/{aid}/revisions", status_code=201)
def create_revision(aid: str, payload: RevisionIn,
                    store: Store = Depends(get_store)):
    """修订已出具的判定单：沿用原单的批次、区域与规则快照重新校验、计算，
    生成关联前版的只读新版本。只有链尾可被修订。"""
    chain = store.get_chain(aid)
    if not chain:
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    root, tail = chain[0], chain[-1]
    if payload.base_id != tail["id"]:
        return _conflict_err(tail["id"])
    # 复算固定使用原单的规则快照，不受区域规则切换影响
    rule = json.loads(root["rule_snapshot"])
    materials = [m.model_dump() for m in payload.materials]
    errors = engine.validate_submission(rule, payload.weight_kg, materials)
    if errors:
        return _err(errors)
    result = engine.evaluate(rule, payload.weight_kg, materials)
    result["evidence"].insert(
        0, f"本单为修订版 v{tail['version'] + 1}，关联前版 {tail['id']}，"
           f"沿用原单 {root['id']} 的批次、区域与规则快照复算，"
           f"不受区域规则切换影响。")
    result["evidence"].append(
        f"修订原因：{payload.reason}；修订操作者：{payload.operator}。")
    stored = {
        "batch_no": root["batch_no"], "region": root["region"],
        "weight_kg": payload.weight_kg, "materials": materials,
        "operator": payload.operator, "reason": payload.reason,
        "base_id": payload.base_id,
    }
    rec, err = store.create_revision(root["root_id"] or root["id"],
                                     payload.base_id, stored, rule, result)
    if err == "not_found":
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    if err == "conflict":
        # 并发争用同一尾版：本请求未被接受，不产生单据与审计
        fresh = store.get_chain(aid)
        return _conflict_err(fresh[-1]["id"] if fresh else tail["id"])
    store.add_audit(payload.operator, "assessment.revised", "assessment", rec["id"],
                    {"root_id": rec["root_id"], "base_id": payload.base_id,
                     "version": rec["version"], "reason": payload.reason,
                     "batch_no": rec["batch_no"], "region": rec["region"],
                     "rule_version": rec["rule_version"],
                     "level": result["level"]["code"],
                     "conclusion": result["conclusion"],
                     "total_fee": result["total_fee"]})
    return rec


@app.get("/api/assessments/{aid}/revisions")
def list_revisions(aid: str, store: Store = Depends(get_store)):
    """按链内任意单号查询完整修订链与当前有效（链尾）结论。"""
    chain = store.get_chain(aid)
    if not chain:
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    tail_id = chain[-1]["id"]
    items = [{
        "id": r["id"],
        "version": r["version"],
        "parent_id": r["parent_id"],
        "is_current": r["id"] == tail_id,
        "batch_no": r["batch_no"],
        "region": r["region"],
        "weight_kg": r["weight_kg"],
        "total_contamination_kg": r["total_contamination_kg"],
        "contamination_ratio": r["contamination_ratio"],
        "level_code": r["level_code"],
        "level_name": r["level_name"],
        "conclusion": r["conclusion"],
        "total_fee": r["total_fee"],
        "currency": r["currency"],
        "rule_version": r["rule_version"],
        "operator": r["operator"],
        "revision_reason": r["revision_reason"],
        "created_at": r["created_at"],
    } for r in chain]
    return {"root_id": chain[0]["root_id"] or chain[0]["id"],
            "current_id": tail_id, "current": items[-1], "items": items}


# ---------------------------------------------------------------- 复核接口
@app.post("/api/assessments/{aid}/review")
def review_assessment(aid: str, payload: ReviewIn,
                      store: Store = Depends(get_store)):
    """复核已出具的判定单：以原单保存的批次重量与物料明细，按同区域
    目标规则版本重新校验、计算，返回原结论与目标等级、扣费明细及差异项。
    只读操作：不写入单据、不产生修订、不记录审计。"""
    rec = store.get_assessment(aid)
    if not rec:
        return _err([{"field": "id", "message": f"判定单不存在：{aid}"}], 404)
    region = payload.region or rec["region"]
    if region != rec["region"]:
        return _err([{"field": "region",
                      "message": f"区域不一致：判定单属于区域 {rec['region']}，"
                                 f"目标规则版本属于区域 {region}"}])
    rule = store.get_rule(region, payload.rule_version)
    if not rule:
        return _err([{"field": "rule_version",
                      "message": f"区域 {region} 不存在规则版本 "
                                 f"{payload.rule_version}"}], 404)
    weight = rec.get("weight_kg")
    materials = rec.get("materials")
    if weight is None or not materials:
        return _err([{"field": "materials",
                      "message": "原单缺少可重算明细（批次重量或物料明细缺失），"
                                 "无法复核"}])
    # 目标版本重算仍执行现有重量、类别与污染量校验
    errors = engine.validate_submission(rule, weight, materials)
    if errors:
        return _err(errors)
    result = engine.evaluate(rule, weight, materials)
    original = {
        "rule_version": rec["rule_version"],
        "level": rec["level"],
        "conclusion": rec["conclusion"],
        "total_contamination_kg": rec["total_contamination_kg"],
        "contamination_ratio": rec["contamination_ratio"],
        "fee_lines": rec["fee_lines"],
        "total_fee": rec["total_fee"],
        "currency": rec["currency"],
    }
    review = {
        "rule_version": rule["version"],
        "level": result["level"],
        "conclusion": result["conclusion"],
        "total_contamination_kg": result["total_contamination_kg"],
        "contamination_ratio": result["contamination_ratio"],
        "fee_lines": result["fee_lines"],
        "total_fee": result["total_fee"],
        "currency": result["currency"],
        "evidence": result["evidence"],
    }
    return {
        "assessment_id": rec["id"],
        "batch_no": rec["batch_no"],
        "region": rec["region"],
        "weight_kg": weight,
        "materials": materials,
        "original": original,
        "review": review,
        "diff": engine.diff_results(original, review),
    }


# ---------------------------------------------------------------- 结算接口
def _settlement_hash(region: str, assessment_ids: list[str],
                     operator: str | None) -> str:
    """结算请求内容指纹：同一幂等请求号重试时用于比对内容是否一致。"""
    canon = {"region": region, "assessment_ids": sorted(assessment_ids),
             "operator": operator}
    return hashlib.sha256(
        json.dumps(canon, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


@app.post("/api/settlements", status_code=201)
def create_settlement(payload: SettlementIn, store: Store = Depends(get_store)):
    """将所选修订链当前有效版本的扣费结成可追溯账单。

    核对单号区域、币种与链尾状态后，在同一事务内冻结每条链本次应结金额
    （与该链上次已结算版本的扣费差额，可为负）、来源版本与合计，生成
    不可修改的结算单并留审计。同一请求号与相同内容重试返回原单；
    内容不同报 409 冲突；同一链同一版本只会入账一次。"""
    ids = payload.assessment_ids
    if len(set(ids)) != len(ids):
        return _err([{"field": "assessment_ids",
                      "message": "单号列表存在重复，每条修订链每次结算 "
                                 "只能出现一次"}])
    rec, created, err = store.create_settlement(
        region=payload.region, assessment_ids=ids,
        request_id=payload.request_id, operator=payload.operator,
        content_hash=_settlement_hash(payload.region, ids, payload.operator))
    if err:
        return _err(err["errors"], err["status"])
    if not created:
        # 同一请求号 + 相同内容：返回原结算单，不重复入账、不重复审计
        return JSONResponse(status_code=200, content=rec)
    store.add_audit(payload.operator, "settlement.created", "settlement",
                    rec["id"],
                    {"request_id": payload.request_id,
                     "region": rec["region"],
                     "currency": rec["currency"],
                     "total_delta": rec["total_delta"],
                     "items": [{"assessment_id": it["assessment_id"],
                                "original_fee": it["original_fee"],
                                "delta": it["delta"]} for it in rec["items"]]})
    return rec


@app.get("/api/settlements")
def list_settlements(region: str | None = None, limit: int = 50, offset: int = 0,
                     store: Store = Depends(get_store)):
    limit = max(1, min(limit, 500))
    return {"items": store.list_settlements(region, limit, offset)}


@app.get("/api/settlements/{sid}")
def get_settlement(sid: str, store: Store = Depends(get_store)):
    """结算单详情：来源单号、来源版本原扣费、与上次结算的差额及合计。"""
    rec = store.get_settlement(sid)
    if not rec:
        return _err([{"field": "id", "message": f"结算单不存在：{sid}"}], 404)
    return rec


# ---------------------------------------------------------------- 审计与导出
@app.get("/api/audit")
def list_audit(entity: str | None = None, limit: int = 100, offset: int = 0,
               store: Store = Depends(get_store)):
    limit = max(1, min(limit, 1000))
    return {"items": store.list_audit(entity, limit, offset)}


def _csv_response(rows: list[dict], columns: list[str], filename: str):
    buf = io.StringIO()
    buf.write("﻿")  # BOM，便于 Excel 识别中文
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/export/assessments.csv")
def export_assessments(region: str | None = None, level: str | None = None,
                       batch_no: str | None = None,
                       store: Store = Depends(get_store)):
    rows = store.list_assessments(region, level, batch_no, limit=100000)
    return _csv_response(rows, [
        "id", "batch_no", "region", "weight_kg", "total_contamination_kg",
        "contamination_ratio", "level_code", "level_name", "conclusion",
        "total_fee", "currency", "rule_version", "operator", "created_at",
        "version", "is_current", "root_id", "parent_id", "revision_reason",
    ], "assessments.csv")


@app.get("/api/export/audit.csv")
def export_audit(store: Store = Depends(get_store)):
    rows = store.list_audit(limit=100000)
    return _csv_response(rows, ["id", "ts", "actor", "action", "entity",
                                "entity_id", "detail"], "audit.csv")


# ------------------------------------------------------------------ 页面
INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX)
