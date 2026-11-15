"""Batch application service: validation, idempotency, atomic commit.

The invariant this module exists to guarantee: **a batch and its summary land
in the same SQLite transaction.**  Either the ledger entries, the source-row
changes, the naive "carry the previous summary forward" result, the full
recomputation of the same snapshot, and the item-by-item comparison all
commit, or none of them do.

Idempotency: a batch is identified by a hash of its normalised payload.  Re-
submitting an already committed payload returns the stored result and writes
nothing, so replaying a batch can never double-count.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from . import aggregation, db, sample_data

FORMAT_VERSION = 1


class BatchError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail or {}


class SimulatedFailure(Exception):
    """Raised on purpose to prove the transaction rolls back mid-batch."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_hash(payload: dict) -> str:
    body = {"format": FORMAT_VERSION, "strict": bool(payload.get("strict", False)),
            "ops": payload.get("ops") or []}
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _value_text(measure: str, value) -> str | None:
    if value is None:
        return None
    if measure in {"total_cents", "unit_price_cents"}:
        return db.format_cents(int(value))
    return str(value)


class Service:
    """One SQLite file, one connection per call, serialised by a process-wide lock.

    sqlite3 connections are not safe to share across the server's request
    threads, so every public method holds the lock for its whole duration and
    threads share the same file (WAL) rather than the same handle.  That also
    makes the "read state while another request commits" race impossible: a
    read either sees the batch and its snapshot, or neither.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or db.DEFAULT_DB_PATH
        self._lock = threading.RLock()
        conn = db.connect(self.db_path)
        try:
            db.init_schema(conn)
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    @property
    def lock(self) -> threading.RLock:
        """Exposed so the reset endpoint can finish in-flight requests first."""
        return self._lock

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    def ensure_seed(self) -> None:
        with self._lock:
            conn = self._conn()
            try:
                if conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]:
                    return
                zone_count = conn.execute("SELECT COUNT(*) AS n FROM zones").fetchone()["n"]
                if zone_count == 0:
                    with self._transaction(conn):
                        conn.executemany(
                            "INSERT INTO zones(zone_id, name, ordinal) VALUES (?,?,?)",
                            [(z["zone_id"], z["name"], z["ordinal"]) for z in sample_data.ZONES],
                        )
                        conn.executemany(
                            """INSERT INTO stores(store_id, name, zone_id, version, created_batch, updated_batch)
                               VALUES (?,?,?,?,?,?)""",
                            [
                                (s["store_id"], s["name"], s["zone_id"], 1, "seed", "seed")
                                for s in sample_data.STORES
                            ],
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO meta(key, value) VALUES ('seed_ts', ?)",
                            (now_iso(),),
                        )
            finally:
                conn.close()
            payload = sample_data.seed_payload()
            payload["label"] = "初始样例批次"
            self.submit_batch(payload)

    # ------------------------------------------------------------------
    # Transaction helper
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _transaction(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _read(self, conn: sqlite3.Connection | None = None):
        """Yield a connection, opening and closing one when not supplied."""
        if conn is not None:
            yield conn
            return
        with self._lock:
            fresh = self._conn()
            try:
                yield fresh
            finally:
                fresh.close()

    def zones(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read(conn) as c:
            return _rows(c, "SELECT zone_id, name, ordinal FROM zones ORDER BY ordinal, zone_id")

    def stores(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read(conn) as c:
            return _rows(
                c,
                "SELECT store_id, name, zone_id, version FROM stores ORDER BY store_id",
            )

    def sales(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read(conn) as c:
            rows = _rows(
                c,
                """SELECT sale_id, store_id, amount_cents, status, version, created_batch,
                          updated_batch, created_at, updated_at
                   FROM sales ORDER BY sale_id""",
            )
        for row in rows:
            row["amount"] = db.format_cents(row["amount_cents"])
        return rows

    def batches(self, limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read(conn) as c:
            rows = _rows(
                c,
                """SELECT batch_id, seq, label, payload_hash, strict, base_version,
                          snapshot_version, applied_at, op_count, accepted_count,
                          rejected_count, entry_count, status
                   FROM batches ORDER BY seq DESC LIMIT ?""",
                (limit,),
            )
        for row in rows:
            row["strict"] = bool(row["strict"])
        return rows

    def attempts(self, limit: int = 50, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._read(conn) as c:
            return _rows(
                c,
                """SELECT id, label, payload_hash, status, batch_id, error_code, error,
                          fail_at, applied_ops, created_at
                   FROM attempts ORDER BY id DESC LIMIT ?""",
                (limit,),
            )

    def batch_detail(self, batch_id: str, conn: sqlite3.Connection | None = None) -> dict:
        with self._read(conn) as c:
            batch = c.execute(
                "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise BatchError("NOT_FOUND", f"批次不存在: {batch_id}", 404)
            batch = dict(batch)
            batch["strict"] = bool(batch["strict"])
            ops = _rows(c, "SELECT * FROM batch_ops WHERE batch_id = ? ORDER BY idx", (batch_id,))
            deltas = _rows(
                c, "SELECT * FROM group_deltas WHERE batch_id = ? ORDER BY zone_key", (batch_id,)
            )
            summary = _rows(
                c,
                "SELECT * FROM summary_groups WHERE version = ? ORDER BY zone_key",
                (batch["snapshot_version"],),
            )
        for op in ops:
            op["payload"] = json.loads(op["payload"])
            op["warnings"] = json.loads(op["warnings"] or "[]")
        return {"batch": batch, "ops": ops, "group_deltas": deltas, "summary": summary}

    def latest_snapshot(self, conn: sqlite3.Connection | None = None) -> dict:
        with self._read(conn) as c:
            version = c.execute("SELECT MAX(version) AS v FROM snapshots").fetchone()["v"]
            if version is None:
                return {"version": None, "summary": [], "full": [], "reconciliation": []}
            return self.snapshot(version, c)

    def snapshot(self, version: int, conn: sqlite3.Connection | None = None) -> dict:
        with self._read(conn) as c:
            row = c.execute("SELECT * FROM snapshots WHERE version = ?", (version,)).fetchone()
            if row is None:
                raise BatchError("NOT_FOUND", f"快照版本不存在: {version}", 404)
            return {
                "version": version,
                "batch": dict(row),
                "summary": _rows(
                    c,
                    "SELECT * FROM summary_groups WHERE version = ? ORDER BY zone_key",
                    (version,),
                ),
                "full": _rows(
                    c,
                    "SELECT * FROM full_groups WHERE version = ? ORDER BY zone_key",
                    (version,),
                ),
                "reconciliation": _rows(
                    c,
                    """SELECT * FROM reconciliation WHERE version = ?
                       ORDER BY zone_key, measure""",
                    (version,),
                ),
            }

    def impact(self, batch_id: str, op_index: int) -> dict:
        with self._read() as c:
            detail = self.batch_detail(batch_id, c)
            op = next((o for o in detail["ops"] if o["idx"] == op_index), None)
            if op is None:
                raise BatchError("NOT_FOUND", f"批次 {batch_id} 没有第 {op_index} 条操作", 404)
            batch = detail["batch"]
            entries = _rows(
                c,
                "SELECT * FROM ledger WHERE batch_id = ? AND op_index = ? ORDER BY id",
                (batch_id, op_index),
            )
            previous = (
                _rows(
                    c,
                    "SELECT * FROM summary_groups WHERE version = ? ORDER BY zone_key",
                    (batch["base_version"],),
                )
                if batch["base_version"] is not None
                else []
            )
        current = detail["summary"]
        prev_by_zone = {r["zone_key"]: r for r in previous}
        cur_by_zone = {r["zone_key"]: r for r in current}

        affected_zone_keys = sorted({e["zone_key"] for e in entries})
        zones = []
        for zone_key in affected_zone_keys:
            in_entries = [e for e in entries if e["zone_key"] == zone_key]
            before = prev_by_zone.get(zone_key)
            after = cur_by_zone.get(zone_key)
            zones.append(
                {
                    "zone_key": zone_key,
                    "zone_name": (before or after or {}).get("zone_name", zone_key),
                    "entry_retract_orders": sum(
                        e["order_delta"] for e in in_entries if e["phase"] == "retract"
                    ),
                    "entry_add_orders": sum(
                        e["order_delta"] for e in in_entries if e["phase"] == "add"
                    ),
                    "entry_retract_total_cents": sum(
                        e["total_delta_cents"] for e in in_entries if e["phase"] == "retract"
                    ),
                    "entry_add_total_cents": sum(
                        e["total_delta_cents"] for e in in_entries if e["phase"] == "add"
                    ),
                    "total_delta_cents": sum(e["total_delta_cents"] for e in in_entries),
                    "before": before,
                    "after": after,
                    "before_exists": before is not None,
                    "after_exists": after is not None,
                }
            )
        return {
            "batch_id": batch_id,
            "base_version": batch["base_version"],
            "snapshot_version": batch["snapshot_version"],
            "op": op,
            "entries": entries,
            "zones": zones,
            "before_exists": batch["base_version"] is not None,
        }

    def state(self) -> dict:
        with self._read() as conn:
            latest = self.latest_snapshot(conn)
            zones = self.zones(conn)
            stores = self.stores(conn)
            sales = self.sales(conn)
            batches = self.batches(conn=conn)
            attempts = self.attempts(conn=conn)
        zone_by_id = {z["zone_id"]: z for z in zones}
        store_by_id = {s["store_id"]: s for s in stores}
        for store in stores:
            zone = zone_by_id.get(store.get("zone_id")) if store.get("zone_id") else None
            store["zone_name"] = zone["name"] if zone else None
            store["active_sales"] = sum(
                1
                for s in sales
                if s["status"] == "active" and s.get("store_id") == store["store_id"]
            )
        for sale in sales:
            store = store_by_id.get(sale.get("store_id")) if sale.get("store_id") else None
            sale["store_name"] = store["name"] if store else None
            zone = zone_by_id.get(store.get("zone_id")) if store and store.get("zone_id") else None
            sale["zone_id"] = zone["zone_id"] if zone else None
            sale["zone_name"] = zone["name"] if zone else None
            sale["matched"] = zone is not None
        return {
            "zones": zones,
            "stores": stores,
            "sales": sales,
            "summary": latest["summary"],
            "full": latest["full"],
            "reconciliation": latest["reconciliation"],
            "snapshot_version": latest["version"],
            "snapshot_batch": latest["batch"],
            "batches": batches,
            "attempts": attempts,
        }

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def submit_batch(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise BatchError("INVALID_PAYLOAD", "请求体必须是 JSON 对象")
        ops = payload.get("ops")
        if not isinstance(ops, list) or not ops:
            raise BatchError("INVALID_PAYLOAD", "ops 必须是非空数组")
        if len(ops) > 500:
            raise BatchError("INVALID_PAYLOAD", "单个批次最多 500 条操作")
        strict = bool(payload.get("strict", False))
        label = (payload.get("label") or "").strip() or "未命名批次"
        fail_after = payload.get("fail_after")
        if fail_after is not None:
            if not isinstance(fail_after, int) or fail_after < 0 or fail_after > len(ops):
                raise BatchError("INVALID_FAILURE", "fail_after 必须是 0..操作数 之间的整数")

        payload_hash = canonical_hash({**payload, "strict": strict})

        with self._lock:
            conn = self._conn()
            try:
                return self._submit_locked(
                    conn, payload, ops, strict, label, fail_after, payload_hash
                )
            finally:
                conn.close()

    def _submit_locked(self, conn, payload, ops, strict, label, fail_after,
                       payload_hash) -> dict:
        existing = conn.execute(
            "SELECT * FROM batches WHERE payload_hash = ?", (payload_hash,)
        ).fetchone()
        if existing is not None:
            return {
                "status": "replayed",
                "message": "相同批次内容已提交过，本次为幂等重放，未再次累计",
                "batch": dict(existing),
                "snapshot": self.snapshot(existing["snapshot_version"], conn),
            }

        zones = self.zones(conn)
        sales = self.sales(conn)
        stores = self.stores(conn)

        # Run the pure application first so a validation failure can never
        # touch the database at all.
        result = aggregation.apply_ops(ops, sales, stores, zones)

        if strict and result.rejected:
            self._record_attempt(
                conn, label, payload_hash, payload, "rejected_strict", None,
                "BATCH_REJECTED", self._rejected_summary(result), fail_after, 0,
            )
            return {
                "status": "rejected",
                "message": "strict 模式下批次中存在被拒收的操作，整批未提交",
                "batch": None,
                "rejected": result.rejected,
                "snapshot": self.latest_snapshot(conn),
            }

        if not result.accepted:
            # Nothing survived validation: no batch row, no snapshot, no
            # ledger - only the audit attempt.  A zero-effect batch must not
            # advance the snapshot version.
            message = "所有操作均被拒收，未生成批次，汇总保持不变"
            self._record_attempt(
                conn, label, payload_hash, payload, "rejected", None,
                "ALL_OPS_REJECTED", self._rejected_summary(result), fail_after, 0,
            )
            return {
                "status": "rejected",
                "message": message,
                "batch": None,
                "rejected": result.rejected,
                "snapshot": self.latest_snapshot(conn),
            }

        batch_seq = (
            conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM batches").fetchone()["s"]
        )
        batch_id = f"B{batch_seq:04d}"
        base_version = conn.execute(
            "SELECT MAX(version) AS v FROM snapshots"
        ).fetchone()["v"]
        snapshot_version = (base_version or 0) + 1
        applied_at = now_iso()
        entries = [entry for plan in result.accepted for entry in plan.entries]
        group_deltas = aggregation.net_group_deltas(entries)

        previous_summary = (
            {
                r["zone_key"]: dict(r)
                for r in _rows(
                    conn,
                    "SELECT * FROM summary_groups WHERE version = ?",
                    (base_version,),
                )
            }
            if base_version is not None
            else {}
        )
        incremental = aggregation.carry_summary(previous_summary, group_deltas, zones)
        full = aggregation.full_aggregate(result.sales_table, result.stores_table, zones)
        comparison = aggregation.compare_summaries(incremental, full)
        all_match = all(item["is_match"] for item in comparison)
        batch_status = "mismatch" if not all_match else (
            "partial" if result.rejected else "committed"
        )

        applied_ops = 0
        try:
            with self._transaction(conn):
                conn.execute(
                    """INSERT INTO batches(batch_id, seq, label, payload_hash, payload, strict,
                                           base_version, snapshot_version, applied_at, op_count,
                                           accepted_count, rejected_count, entry_count, status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        batch_id, batch_seq, label, payload_hash,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        int(strict), base_version, snapshot_version, applied_at,
                        len(ops), len(result.accepted), len(result.rejected),
                        len(entries), batch_status,
                    ),
                )

                accepted_by_index = {p.op_index: p for p in result.accepted}
                for idx, raw_op in enumerate(ops):
                    plan = accepted_by_index.get(idx)
                    result_kind = "applied" if plan else "rejected"
                    code = message = warnings = None
                    if plan is None:
                        rej = next(r for r in result.rejected if r["op_index"] == idx)
                        code, message = rej["code"], rej["message"]
                        warnings = []
                    else:
                        warnings = plan.warnings
                    conn.execute(
                        """INSERT INTO batch_ops(batch_id, idx, op_type, payload, result,
                                                 error_code, error_message, warnings)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            batch_id, idx, (raw_op or {}).get("type", "invalid"),
                            json.dumps(raw_op, ensure_ascii=False, sort_keys=True),
                            result_kind, code, message,
                            json.dumps(warnings, ensure_ascii=False),
                        ),
                    )

                # Write ledger rows op by op so an injected failure lands
                # mid-stream and proves the rollback.
                if fail_after == 0:
                    raise SimulatedFailure("注入的故障：在本批次写入任何明细之前中断")
                for plan in result.accepted:
                    for entry in plan.entries:
                        conn.execute(
                            """INSERT INTO ledger(batch_id, op_index, sale_id, store_id, phase,
                                                  reason, zone_key, order_delta,
                                                  amount_count_delta, null_count_delta,
                                                  zero_count_delta, total_delta_cents,
                                                  old_amount_cents, new_amount_cents,
                                                  old_zone_key, new_zone_key)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                batch_id, plan.op_index, entry.sale_id, entry.store_id,
                                entry.phase, entry.reason, entry.zone_key,
                                entry.order_count, entry.amount_count, entry.null_count,
                                entry.zero_count, entry.total_cents,
                                entry.old_amount_cents, entry.new_amount_cents,
                                entry.old_zone_key, entry.new_zone_key,
                            ),
                        )
                    applied_ops += 1
                    if fail_after is not None and applied_ops >= fail_after:
                        raise SimulatedFailure(
                            f"注入的故障：已写入 {applied_ops} 条操作的增量明细后中断"
                        )

                for delta in group_deltas.values():
                    conn.execute(
                        """INSERT INTO group_deltas(batch_id, zone_key, retract_orders,
                                                    add_orders, retract_amount_count,
                                                    add_amount_count, retract_null_count,
                                                    add_null_count, retract_zero_count,
                                                    add_zero_count, retract_total_cents,
                                                    add_total_cents, net_orders,
                                                    net_amount_count, net_null_count,
                                                    net_zero_count, net_total_cents)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            batch_id, delta["zone_key"], delta["retract_orders"],
                            delta["add_orders"], delta["retract_amount_count"],
                            delta["add_amount_count"], delta["retract_null_count"],
                            delta["add_null_count"], delta["retract_zero_count"],
                            delta["add_zero_count"], delta["retract_total_cents"],
                            delta["add_total_cents"], delta["net_orders"],
                            delta["net_amount_count"], delta["net_null_count"],
                            delta["net_zero_count"], delta["net_total_cents"],
                        ),
                    )

                touched_sales = {r["sale_id"]: r for r in result.sales_table}
                prior_sales = {r["sale_id"]: r for r in sales}
                # Any sale row that existed before but is absent from the
                # resulting table was removed by a key rename.
                for prior_id in prior_sales:
                    if prior_id not in touched_sales:
                        conn.execute(
                            "DELETE FROM sales WHERE sale_id = ?", (prior_id,)
                        )
                for sale_id, row in touched_sales.items():
                    prior = prior_sales.get(sale_id)
                    version = (prior["version"] + 1) if prior else 1
                    created_batch = prior["created_batch"] if prior else batch_id
                    created_at = prior["created_at"] if prior else applied_at
                    if prior is not None and all(
                        prior.get(key) == row.get(key)
                        for key in ("store_id", "amount_cents", "status")
                    ):
                        continue
                    conn.execute(
                        """INSERT INTO sales(sale_id, store_id, amount_cents, status, version,
                                             created_batch, updated_batch, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(sale_id) DO UPDATE SET
                             store_id=excluded.store_id,
                             amount_cents=excluded.amount_cents,
                             status=excluded.status,
                             version=excluded.version,
                             updated_batch=excluded.updated_batch,
                             updated_at=excluded.updated_at""",
                        (
                            sale_id, row.get("store_id"), row.get("amount_cents"),
                            row["status"], version, created_batch, batch_id,
                            created_at, applied_at,
                        ),
                    )

                prior_stores = {r["store_id"]: r for r in stores}
                for store in result.stores_table:
                    prior = prior_stores.get(store["store_id"])
                    if prior is None:
                        conn.execute(
                            """INSERT INTO stores(store_id, name, zone_id, version,
                                                  created_batch, updated_batch)
                               VALUES (?,?,?,?,?,?)""",
                            (store["store_id"], store["name"], store.get("zone_id"),
                             1, batch_id, batch_id),
                        )
                    elif prior.get("zone_id") != store.get("zone_id"):
                        conn.execute(
                            """UPDATE stores SET zone_id = ?, version = version + 1,
                                                 updated_batch = ? WHERE store_id = ?""",
                            (store.get("zone_id"), batch_id, store["store_id"]),
                        )

                self._write_snapshot(
                    conn, snapshot_version, batch_id, label, applied_at,
                    incremental, full, comparison,
                    result.sales_table, result.stores_table,
                )

                conn.execute(
                    """INSERT INTO attempts(label, payload_hash, payload, status, batch_id,
                                            error_code, error, fail_at, applied_ops, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (label, payload_hash, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     batch_status, batch_id, None, None, None, len(result.accepted),
                     applied_at),
                )
        except SimulatedFailure as exc:
            self._record_attempt(
                conn, label, payload_hash, payload, "rolled_back", None,
                "SIMULATED_FAILURE", str(exc), fail_after, applied_ops,
            )
            return {
                "status": "rolled_back",
                "message": str(exc),
                "detail": {
                    "applied_ops_before_failure": applied_ops,
                    "committed": False,
                    "note": "故障发生在本批次事务内，批次、明细、汇总与快照全部回滚；"
                            "重新提交同一批次即可恢复",
                },
                "snapshot": self.latest_snapshot(conn),
            }

        return {
            "status": batch_status,
            "message": "批次与汇总结果已在同一事务中提交"
            if all_match else "批次已提交，但增量汇总与全量重算不一致，请检查",
            "batch": self.batch_detail(batch_id, conn)["batch"],
            "snapshot": self.snapshot(snapshot_version, conn),
            "group_deltas": list(group_deltas.values()),
            "rejected": result.rejected,
            "warnings": [
                {"op_index": p.op_index, "warnings": p.warnings}
                for p in result.accepted
                if p.warnings
            ],
            "all_match": all_match,
        }

    def _rejected_summary(self, result: aggregation.Result) -> str:
        return json.dumps(result.rejected, ensure_ascii=False)

    def _record_attempt(self, conn, label, payload_hash, payload, status, batch_id,
                        error_code, error, fail_at, applied_ops) -> None:
        """Write the failure record in its own transaction, after the rollback.

        This row is the audit trail that survives a rolled-back batch; the
        batch itself does not exist.
        """
        with self._transaction(conn):
            conn.execute(
                """INSERT INTO attempts(label, payload_hash, payload, status, batch_id,
                                        error_code, error, fail_at, applied_ops, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    label, payload_hash,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    status, batch_id, error_code, error,
                    None if fail_at is None else str(fail_at),
                    applied_ops, now_iso(),
                ),
            )

    def _write_snapshot(self, conn, version, batch_id, label, created_at, incremental, full,
                        comparison, sales_table, stores_table) -> None:
        conn.execute(
            "INSERT INTO snapshots(version, batch_id, label, created_at) VALUES (?,?,?,?)",
            (version, batch_id, label, created_at),
        )
        for zone_key, row in incremental.items():
            conn.execute(
                """INSERT INTO summary_groups(version, zone_key, zone_name, order_count,
                                              amount_count, null_count, zero_count,
                                              total_cents, unit_price_cents)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (version, zone_key, row["zone_name"], row["order_count"], row["amount_count"],
                 row["null_count"], row["zero_count"], row["total_cents"],
                 row["unit_price_cents"]),
            )
        for zone_key, row in full.items():
            conn.execute(
                """INSERT INTO full_groups(version, zone_key, zone_name, order_count,
                                           amount_count, null_count, zero_count,
                                           total_cents, unit_price_cents)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (version, zone_key, row["zone_name"], row["order_count"], row["amount_count"],
                 row["null_count"], row["zero_count"], row["total_cents"],
                 row["unit_price_cents"]),
            )
        for item in comparison:
            conn.execute(
                """INSERT INTO reconciliation(version, zone_key, zone_name, measure,
                                              incremental_value, full_value, is_match)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    version, item["zone_key"], item["zone_name"], item["measure"],
                    _value_text(item["measure"], item["incremental_value"]),
                    _value_text(item["measure"], item["full_value"]),
                    int(item["is_match"]),
                ),
            )
        for sale in sales_table:
            conn.execute(
                """INSERT INTO source_sales_snapshot(version, sale_id, store_id,
                                                     amount_cents, status)
                   VALUES (?,?,?,?,?)""",
                (version, sale["sale_id"], sale.get("store_id"),
                 sale.get("amount_cents"), sale.get("status", "active")),
            )
        for store in stores_table:
            conn.execute(
                """INSERT INTO source_stores_snapshot(version, store_id, name, zone_id)
                   VALUES (?,?,?,?)""",
                (version, store["store_id"], store["name"], store.get("zone_id")),
            )

    # ------------------------------------------------------------------
    # Verification helpers (used by tests and the "same snapshot" button)
    # ------------------------------------------------------------------
    def recompute_from_snapshot(self, version: int) -> dict:
        """Recompute the summary from the persisted source snapshot at `version`."""
        with self._read() as conn:
            sales = _rows(
                conn,
                """SELECT sale_id, store_id, amount_cents, status
                   FROM source_sales_snapshot WHERE version = ?""",
                (version,),
            )
            stores = _rows(
                conn,
                "SELECT store_id, name, zone_id FROM source_stores_snapshot WHERE version = ?",
                (version,),
            )
            zones = self.zones(conn)
            stored_incremental = {
                r["zone_key"]: dict(r)
                for r in _rows(
                    conn,
                    "SELECT * FROM summary_groups WHERE version = ?",
                    (version,),
                )
            }
            stored_full = {
                r["zone_key"]: dict(r)
                for r in _rows(
                    conn,
                    "SELECT * FROM full_groups WHERE version = ?",
                    (version,),
                )
            }
        full = aggregation.full_aggregate(sales, stores, zones)
        fresh_comparison = aggregation.compare_summaries(stored_incremental, full)
        full_matches_stored = aggregation.compare_summaries(stored_full, full)
        return {
            "version": version,
            "recomputed": full,
            "incremental": stored_incremental,
            "comparison": fresh_comparison,
            "stored_full_matches": full_matches_stored,
            "all_match": all(i["is_match"] for i in fresh_comparison)
            and all(i["is_match"] for i in full_matches_stored),
        }
