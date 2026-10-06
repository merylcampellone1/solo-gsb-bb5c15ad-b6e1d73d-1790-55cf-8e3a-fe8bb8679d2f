"""SQLite 持久化：判定单、规则版本与审计日志。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .rules import SEED_RULES

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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
