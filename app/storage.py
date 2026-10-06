"""SQLite 持久化：判定单、规则版本与审计日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .rules import SEED_RULES, _money as round_money

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    region       TEXT NOT NULL,
    version      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'inactive',
    created_at   TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE(region, version)
);
CREATE TABLE IF NOT EXISTS assessments (
    id                     TEXT PRIMARY KEY,
    batch_no               TEXT NOT NULL,
    region                 TEXT NOT NULL,
    weight_kg              REAL NOT NULL,
    total_contamination_kg REAL NOT NULL,
    contamination_ratio    REAL NOT NULL,
    level_code             TEXT NOT NULL,
    level_name             TEXT NOT NULL,
    conclusion             TEXT NOT NULL,
    total_fee              REAL NOT NULL,
    currency               TEXT NOT NULL,
    rule_version           TEXT NOT NULL,
    rule_snapshot          TEXT NOT NULL,
    request_json           TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    operator               TEXT,
    root_id                TEXT,
    parent_id              TEXT,
    version                INTEGER NOT NULL DEFAULT 1,
    revision_reason        TEXT,
    created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assessments_batch  ON assessments(batch_no);
CREATE INDEX IF NOT EXISTS idx_assessments_region ON assessments(region);
CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS settlements (
    id           TEXT PRIMARY KEY,
    request_no   TEXT NOT NULL UNIQUE,
    content_json TEXT NOT NULL,
    region       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    total_delta  REAL NOT NULL,
    operator     TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id      TEXT NOT NULL REFERENCES settlements(id),
    line_no            INTEGER NOT NULL,
    root_id            TEXT NOT NULL,
    assessment_id      TEXT NOT NULL,
    version            INTEGER NOT NULL,
    batch_no           TEXT NOT NULL,
    source_fee         REAL NOT NULL,
    previous_settled_id TEXT,
    previous_fee       REAL NOT NULL DEFAULT 0,
    fee_delta          REAL NOT NULL,
    UNIQUE(root_id, version)
);
CREATE INDEX IF NOT EXISTS idx_settlement_items_bill
    ON settlement_items(settlement_id, line_no);
CREATE INDEX IF NOT EXISTS idx_settlement_items_root
    ON settlement_items(root_id, version);
CREATE INDEX IF NOT EXISTS idx_settlements_region
    ON settlements(region, created_at, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SettlementError(Exception):
    """结算事务内的可预期失败：code 决定 HTTP 状态码与字段名。"""

    def __init__(self, code: str, field: str, message: str):
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message


class Store:
    """对 SQLite 的轻量封装；每次操作独立连接，线程安全。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("APP_DB", "data/app.db")
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self._locked_conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_assessments_root"
                         " ON assessments(root_id)")
        self._seed_rules()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧版数据库补齐修订链相关列（新列对历史单据取默认值）。"""
        cols = {r[1] for r in conn.execute("PRAGMA table_info(assessments)")}
        for col, ddl in (
            ("root_id", "ALTER TABLE assessments ADD COLUMN root_id TEXT"),
            ("parent_id", "ALTER TABLE assessments ADD COLUMN parent_id TEXT"),
            ("version", "ALTER TABLE assessments ADD COLUMN version"
                        " INTEGER NOT NULL DEFAULT 1"),
            ("revision_reason",
             "ALTER TABLE assessments ADD COLUMN revision_reason TEXT"),
        ):
            if col not in cols:
                conn.execute(ddl)

    # ------------------------------------------------------------------ 基础
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _locked_conn(self):
        """上下文管理器：持锁打开连接，退出时提交并关闭。"""
        store = self

        class _Ctx:
            def __enter__(self) -> sqlite3.Connection:
                store._lock.acquire()
                self.conn = store._conn()
                return self.conn

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self.conn.commit()
                    else:
                        self.conn.rollback()
                finally:
                    self.conn.close()
                    store._lock.release()
                return False

        return _Ctx()

    # ------------------------------------------------------------------ 规则
    def _seed_rules(self) -> None:
        with self._locked_conn() as conn:
            for rule in SEED_RULES:
                payload = {k: v for k, v in rule.items() if k != "status"}
                conn.execute(
                    "INSERT OR IGNORE INTO rule_versions"
                    " (region, version, payload, status, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (rule["region"], rule["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            # 保证每个区域恰好一个启用版本；已被用户切换过的区域不受影响。
            regions = [r[0] for r in conn.execute(
                "SELECT DISTINCT region FROM rule_versions")]
            for region in regions:
                active = conn.execute(
                    "SELECT COUNT(*) FROM rule_versions"
                    " WHERE region=? AND status='active'", (region,),
                ).fetchone()[0]
                if active:
                    continue
                preferred = next(
                    (r["version"] for r in SEED_RULES
                     if r["region"] == region and r.get("status") == "active"),
                    None,
                )
                if preferred is None:
                    preferred = conn.execute(
                        "SELECT version FROM rule_versions"
                        " WHERE region=? ORDER BY id LIMIT 1", (region,),
                    ).fetchone()[0]
                conn.execute(
                    "UPDATE rule_versions SET status='active', activated_at=?"
                    " WHERE region=? AND version=?",
                    (_now(), region, preferred),
                )

    def list_rules(self, region: str | None = None) -> list[dict]:
        sql = ("SELECT region, version, payload, status, created_at, activated_at"
               " FROM rule_versions")
        params: tuple = ()
        if region:
            sql += " WHERE region=?"
            params = (region,)
        sql += " ORDER BY region, id"
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "region": r["region"],
            "version": r["version"],
            "status": r["status"],
            "created_at": r["created_at"],
            "activated_at": r["activated_at"],
            "rule": json.loads(r["payload"]),
        } for r in rows]

    def get_rule(self, region: str, version: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_active_rule(self, region: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT payload FROM rule_versions"
                " WHERE region=? AND status='active'", (region,),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def create_rule(self, body: dict) -> dict:
        payload = {k: body[k] for k in
                   ("region", "version", "currency", "categories", "levels")}
        with self._locked_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO rule_versions (region, version, payload, status,"
                    " created_at) VALUES (?,?,?,?,?)",
                    (body["region"], body["version"],
                     json.dumps(payload, ensure_ascii=False), "inactive", _now()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"区域 {body['region']} 已存在规则版本 {body['version']}"
                ) from exc
            if body.get("activate"):
                self._activate(conn, body["region"], body["version"])
        return payload

    def _activate(self, conn: sqlite3.Connection, region: str, version: str) -> None:
        conn.execute("UPDATE rule_versions SET status='inactive' WHERE region=?",
                     (region,))
        conn.execute(
            "UPDATE rule_versions SET status='active', activated_at=?"
            " WHERE region=? AND version=?",
            (_now(), region, version),
        )

    def activate_rule(self, region: str, version: str) -> bool:
        with self._locked_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM rule_versions WHERE region=? AND version=?",
                (region, version),
            ).fetchone()
            if not exists:
                return False
            self._activate(conn, region, version)
        return True

    # ---------------------------------------------------------------- 判定单
    def create_assessment(self, payload: dict, rule: dict, result: dict) -> dict:
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            conn.execute(
                "INSERT INTO assessments (id, batch_no, region, weight_kg,"
                " total_contamination_kg, contamination_ratio, level_code,"
                " level_name, conclusion, total_fee, currency, rule_version,"
                " rule_snapshot, request_json, result_json, operator,"
                " root_id, version, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, payload["batch_no"], payload["region"], payload["weight_kg"],
                    result["total_contamination_kg"], result["contamination_ratio"],
                    result["level"]["code"], result["level"]["name"],
                    result["conclusion"], result["total_fee"], result["currency"],
                    rule["version"], json.dumps(rule, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    payload.get("operator"), aid, 1, _now(),
                ),
            )
        return self.get_assessment(aid)

    def get_chain(self, aid: str) -> list[dict] | None:
        """按链内任意单号解析整条修订链（含旧版无 root_id 的原单），
        按版本号升序返回原始行；单号不存在时返回 None。"""
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            root_id = row["root_id"] or row["id"]
            rows = conn.execute(
                "SELECT * FROM assessments WHERE root_id=? OR id=?"
                " ORDER BY version, created_at, id",
                (root_id, root_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_revision(self, root_id: str, base_id: str, payload: dict,
                        rule: dict, result: dict) -> tuple[dict | None, str | None]:
        """在单个持锁事务内复核 base_id 仍为链尾并插入新版本，
        保证争用同一尾版的并发请求只有一个成功。
        返回 (新单, None)；失败返回 (None, 'not_found' | 'conflict')。"""
        aid = "A" + uuid.uuid4().hex[:10].upper()
        with self._locked_conn() as conn:
            tail = conn.execute(
                "SELECT id, version FROM assessments"
                " WHERE root_id=? OR id=? ORDER BY version DESC LIMIT 1",
                (root_id, root_id),
            ).fetchone()
            if not tail:
                return None, "not_found"
            if tail["id"] != base_id:
                return None, "conflict"
            conn.execute(
                "INSERT INTO assessments (id, batch_no, region, weight_kg,"
                " total_contamination_kg, contamination_ratio, level_code,"
                " level_name, conclusion, total_fee, currency, rule_version,"
                " rule_snapshot, request_json, result_json, operator,"
                " root_id, parent_id, version, revision_reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid, payload["batch_no"], payload["region"], payload["weight_kg"],
                    result["total_contamination_kg"], result["contamination_ratio"],
                    result["level"]["code"], result["level"]["name"],
                    result["conclusion"], result["total_fee"], result["currency"],
                    rule["version"], json.dumps(rule, ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                    payload.get("operator"),
                    root_id, base_id, tail["version"] + 1,
                    payload.get("reason"), _now(),
                ),
            )
        return self.get_assessment(aid), None

    def get_assessment(self, aid: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
            if not row:
                return None
            # 链尾判定：没有任何版本以本单为前版
            is_current = conn.execute(
                "SELECT 1 FROM assessments WHERE parent_id=? LIMIT 1",
                (aid,),
            ).fetchone() is None
        request = json.loads(row["request_json"])
        result = json.loads(row["result_json"])
        return {
            "id": row["id"],
            "batch_no": row["batch_no"],
            "region": row["region"],
            "weight_kg": row["weight_kg"],
            "materials": request.get("materials"),
            "operator": row["operator"],
            "created_at": row["created_at"],
            "rule_version": row["rule_version"],
            "rule_snapshot": json.loads(row["rule_snapshot"]),
            "total_contamination_kg": row["total_contamination_kg"],
            "contamination_ratio": row["contamination_ratio"],
            "level": {"code": row["level_code"], "name": row["level_name"]},
            "conclusion": row["conclusion"],
            "fee_lines": result["fee_lines"],
            "total_fee": row["total_fee"],
            "currency": row["currency"],
            "evidence": result["evidence"],
            "root_id": row["root_id"] or row["id"],
            "parent_id": row["parent_id"],
            "version": row["version"],
            "is_current": is_current,
            "revision_reason": row["revision_reason"],
        }

    def list_assessments(self, region: str | None = None, level: str | None = None,
                         batch_no: str | None = None, limit: int = 50,
                         offset: int = 0, current: bool = False) -> list[dict]:
        sql = ("SELECT id, batch_no, region, weight_kg, total_contamination_kg,"
               " contamination_ratio, level_code, level_name, conclusion, total_fee,"
               " currency, rule_version, operator, created_at,"
               " root_id, parent_id, version, revision_reason,"
               " NOT EXISTS(SELECT 1 FROM assessments c"
               "            WHERE c.parent_id = assessments.id) AS is_current"
               " FROM assessments")
        conds, params = [], []
        if region:
            conds.append("region=?")
            params.append(region)
        if level:
            conds.append("level_code=?")
            params.append(level)
        if batch_no:
            conds.append("batch_no LIKE ?")
            params.append(f"%{batch_no}%")
        if current:
            conds.append("NOT EXISTS(SELECT 1 FROM assessments c"
                         " WHERE c.parent_id = assessments.id)")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{**dict(r),
                 "root_id": r["root_id"] or r["id"],
                 "is_current": bool(r["is_current"])} for r in rows]

    # ---------------------------------------------------------------- 结算单
    def get_settlement_by_request(self, request_no: str) -> dict | None:
        with self._locked_conn() as conn:
            row = conn.execute(
                "SELECT id FROM settlements WHERE request_no=?", (request_no,),
            ).fetchone()
        return self.get_settlement(row["id"]) if row else None

    def create_settlement(self, request_no: str, content: dict,
                          assessment_ids: list[str], operator: str | None
                          ) -> tuple[dict, bool]:
        """在单个持锁事务内核对链状态并生成不可修改的结算单。

        幂等：相同 request_no 与相同 content 返回原单（不写审计）；
        content 不同抛 idempotency_conflict。
        每条链本次只冻结链尾版本；与该链上次已结算版本的扣费差额入账，
        (root_id, version) 唯一约束保证同一版本只能结算一次。
        返回 (结算单, 是否重放)。
        """
        sid = "S" + uuid.uuid4().hex[:10].upper()
        content_json = json.dumps(content, ensure_ascii=False,
                                  sort_keys=True, separators=(",", ":"))
        with self._locked_conn() as conn:
            dup = conn.execute(
                "SELECT id, content_json FROM settlements WHERE request_no=?",
                (request_no,),
            ).fetchone()
            if dup:
                if dup["content_json"] == content_json:
                    bill = self._load_settlement(conn, dup["id"])
                    return bill, True
                raise SettlementError(
                    "idempotency_conflict", "request_no",
                    f"幂等请求号 {request_no} 已被使用但提交内容不同，"
                    f"原结算单 {dup['id']} 不允许修改；请更换请求号后重试")

            items: list[dict] = []
            seen_roots: set[str] = set()
            total_delta = 0.0
            region = currency = None
            for line_no, aid in enumerate(assessment_ids, start=1):
                row = conn.execute(
                    "SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
                if not row:
                    raise SettlementError(
                        "invalid_assessment", f"assessment_ids[{line_no - 1}]",
                        f"单号不存在：{aid}")
                root_id = row["root_id"] or row["id"]
                if root_id in seen_roots:
                    raise SettlementError(
                        "duplicate_chain", f"assessment_ids[{line_no - 1}]",
                        f"单号 {aid} 与列表中的其他单号属于同一条修订链，"
                        f"同一结算单内每条链只能选择当前链尾一次")
                seen_roots.add(root_id)
                # 链尾判定：没有任何版本以本单为前版
                is_tail = conn.execute(
                    "SELECT 1 FROM assessments WHERE parent_id=? LIMIT 1",
                    (aid,),
                ).fetchone() is None
                if not is_tail:
                    tail = conn.execute(
                        "SELECT id FROM assessments"
                        " WHERE root_id=? OR id=?"
                        " ORDER BY version DESC LIMIT 1",
                        (root_id, root_id),
                    ).fetchone()
                    raise SettlementError(
                        "not_tail", f"assessment_ids[{line_no - 1}]",
                        f"单号 {aid} 不是修订链尾（当前链尾为 {tail['id']}），"
                        f"仅允许结算当前有效版本")
                if region is None:
                    region, currency = row["region"], row["currency"]
                elif row["region"] != region:
                    raise SettlementError(
                        "region_mismatch", f"assessment_ids[{line_no - 1}]",
                        f"跨区域结算：单号 {aid} 属于区域 {row['region']}，"
                        f"与请求区域 {region} 不一致")
                elif row["currency"] != currency:
                    raise SettlementError(
                        "currency_mismatch", f"assessment_ids[{line_no - 1}]",
                        f"币种不一致：单号 {aid} 为 {row['currency']}，"
                        f"与本次结算币种 {currency} 不一致")
                prev = conn.execute(
                    "SELECT assessment_id, source_fee FROM settlement_items"
                    " WHERE root_id=? ORDER BY line_no DESC, id DESC LIMIT 1",
                    (root_id,),
                ).fetchone()
                # 同一链的同一版本只能结算一次（正常由上一次结算记录拦截，
                # 唯一约束为并发争用兜底）
                if prev and prev["assessment_id"] == aid:
                    raise SettlementError(
                        "already_settled", f"assessment_ids[{line_no - 1}]",
                        f"单号 {aid} 所在修订链的该版本已结算，"
                        f"不能重复入账；请修订链尾后再结算差额")
                previous_fee = prev["source_fee"] if prev else 0.0
                delta = round_money(row["total_fee"] - previous_fee)
                total_delta = round_money(total_delta + delta)
                items.append({
                    "line_no": line_no, "root_id": root_id,
                    "assessment_id": aid, "version": row["version"],
                    "batch_no": row["batch_no"], "source_fee": row["total_fee"],
                    "previous_settled_id":
                        prev["assessment_id"] if prev else None,
                    "previous_fee": previous_fee, "fee_delta": delta,
                })
            try:
                conn.execute(
                    "INSERT INTO settlements (id, request_no, content_json,"
                    " region, currency, total_delta, operator, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (sid, request_no, content_json, region, currency,
                     total_delta, operator, _now()),
                )
                conn.executemany(
                    "INSERT INTO settlement_items (settlement_id, line_no,"
                    " root_id, assessment_id, version, batch_no, source_fee,"
                    " previous_settled_id, previous_fee, fee_delta)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(sid, it["line_no"], it["root_id"], it["assessment_id"],
                      it["version"], it["batch_no"], it["source_fee"],
                      it["previous_settled_id"], it["previous_fee"],
                      it["fee_delta"]) for it in items],
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc)
                if "settlements.request_no" in msg:
                    # 并发提交同一请求号：交由事务顶层的幂等复核判定
                    dup = conn.execute(
                        "SELECT id, content_json FROM settlements"
                        " WHERE request_no=?", (request_no,),
                    ).fetchone()
                    if dup and dup["content_json"] == content_json:
                        raise SettlementError(
                            "idempotent_replay", "request_no",
                            f"请求号 {request_no} 正在并发提交相同内容") from exc
                    raise SettlementError(
                        "idempotency_conflict", "request_no",
                        f"幂等请求号 {request_no} 已被使用但提交内容不同，"
                        f"原结算单不允许修改；请更换请求号后重试") from exc
                # 并发争用同一链同一版本：只允许一个请求成功
                raise SettlementError(
                    "version_settled", "assessment_ids",
                    "并发结算冲突：所选修订链版本已被其他请求结算，"
                    "请重新获取链尾后再试") from exc
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, entity, entity_id,"
                " detail) VALUES (?,?,?,?,?,?)",
                (_now(), operator, "settlement.created", "settlement", sid,
                 json.dumps({"request_no": request_no, "region": region,
                             "currency": currency, "total_delta": total_delta,
                             "assessment_ids": assessment_ids,
                             "items": items}, ensure_ascii=False)),
            )
            bill = self._load_settlement(conn, sid)
        return bill, False

    @staticmethod
    def _load_settlement(conn: sqlite3.Connection, sid: str) -> dict:
        row = conn.execute(
            "SELECT * FROM settlements WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        item_rows = conn.execute(
            "SELECT * FROM settlement_items WHERE settlement_id=?"
            " ORDER BY line_no, id", (sid,),
        ).fetchall()
        return {
            "id": row["id"],
            "request_no": row["request_no"],
            "region": row["region"],
            "currency": row["currency"],
            "total_delta": row["total_delta"],
            "operator": row["operator"],
            "created_at": row["created_at"],
            "items": [dict(r) for r in item_rows],
        }

    def get_settlement(self, sid: str) -> dict | None:
        with self._locked_conn() as conn:
            return self._load_settlement(conn, sid)

    def list_settlements(self, region: str | None = None,
                         request_no: str | None = None,
                         limit: int = 50, offset: int = 0) -> list[dict]:
        sql = "SELECT id FROM settlements"
        conds, params = [], []
        if region:
            conds.append("region=?")
            params.append(region)
        if request_no:
            conds.append("request_no=?")
            params.append(request_no)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            bills = [self._load_settlement(conn, r["id"]) for r in rows]
        return bills

    # ---------------------------------------------------------------- 审计
    def add_audit(self, actor: str | None, action: str, entity: str,
                  entity_id: str | None, detail: dict | None = None) -> None:
        with self._locked_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, entity, entity_id, detail)"
                " VALUES (?,?,?,?,?,?)",
                (_now(), actor, action, entity, entity_id,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )

    def list_audit(self, entity: str | None = None, limit: int = 100,
                   offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if entity:
            sql += " WHERE entity=?"
            params.append(entity)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._locked_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
