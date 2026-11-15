"""Pure aggregation and batch-application rules.

The rules are intentionally small and explicit because the whole point of the
page is to make them auditable:

* an **order** is every active sale record, with or without an amount;
* ``amount_count`` counts only sales whose amount is non-null (zero is present);
* ``null_count`` counts sales with a missing amount;
* ``zero_count`` counts sales whose amount is exactly 0;
* ``total_cents`` sums non-null amounts only;
* ``unit_price_cents`` = total / amount_count and is ``None`` when there is no
  non-null amount.  A zero unit price and an undefined unit price are therefore
  different values.
* a sale whose store is unknown, or whose store has no region, lands in the
  ``__unmatched__`` bucket.  It is shown, but it is not part of any region's
  numbers.
* a group exists **iff** its order count is non-zero.  A region with only
  null-amount sales is a group whose total is zero; a region whose last sale
  was retracted has no group at all.

Batch operations are applied through signed ledger entries.  An update is
always a retraction of the old contribution followed by an addition of the
new one, even when both land in the same region.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

UNMATCHED = "__unmatched__"
UNMATCHED_NAME = "未匹配门店 / 未分配区域"

MEASURES = (
    "order_count",
    "amount_count",
    "null_count",
    "zero_count",
    "total_cents",
    "unit_price_cents",
)

MEASURE_LABELS = {
    "order_count": "订单数",
    "amount_count": "非空金额笔数",
    "null_count": "空金额笔数",
    "zero_count": "零金额笔数",
    "total_cents": "金额合计",
    "unit_price_cents": "客单价",
}


class OpError(Exception):
    """A rejected operation with a stable machine-readable code."""

    def __init__(self, code: str, message: str, warning: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.warning = warning


def normalize_amount(raw) -> int | None:
    """Convert an amount to integer cents, ``None`` meaning *missing*.

    ``None``, the empty string, and the literal markers ``null`` / ``none`` /
    ``~`` all mean missing.  A numeric zero is a *present* amount worth zero.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise OpError("INVALID_AMOUNT", "布尔值不能作为金额")
    if isinstance(raw, str):
        text = raw.strip()
        if text == "" or text.lower() in {"null", "none", "~"}:
            return None
        raw = text
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise OpError("INVALID_AMOUNT", f"金额格式无法解析: {raw!r}")
    if not value.is_finite():
        raise OpError("INVALID_AMOUNT", "金额必须是有限数字")
    scaled = value * 100
    cents = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if cents != scaled:
        raise OpError("INVALID_AMOUNT", "金额最多两位小数（分）")
    return int(cents)


@dataclass
class Contribution:
    """Additive footprint of one active sale in its region bucket."""

    zone_key: str
    order_count: int = 0
    amount_count: int = 0
    null_count: int = 0
    zero_count: int = 0
    total_cents: int = 0

    def as_dict(self) -> dict:
        return {
            "order_count": self.order_count,
            "amount_count": self.amount_count,
            "null_count": self.null_count,
            "zero_count": self.zero_count,
            "total_cents": self.total_cents,
        }

    def __add__(self, other: "Contribution") -> "Contribution":
        if self.zone_key != other.zone_key:
            raise ValueError("cannot add contributions from different regions")
        return Contribution(
            zone_key=self.zone_key,
            order_count=self.order_count + other.order_count,
            amount_count=self.amount_count + other.amount_count,
            null_count=self.null_count + other.null_count,
            zero_count=self.zero_count + other.zero_count,
            total_cents=self.total_cents + other.total_cents,
        )

    def negated(self) -> "Contribution":
        return Contribution(
            zone_key=self.zone_key,
            order_count=-self.order_count,
            amount_count=-self.amount_count,
            null_count=-self.null_count,
            zero_count=-self.zero_count,
            total_cents=-self.total_cents,
        )

    def is_empty(self) -> bool:
        return (
            self.order_count == 0
            and self.amount_count == 0
            and self.null_count == 0
            and self.zero_count == 0
            and self.total_cents == 0
        )


def div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division with half-up rounding, correct for negative numerators."""
    if denominator == 0:
        raise ZeroDivisionError("denominator must be non-zero")
    if denominator < 0:
        numerator, denominator = -numerator, -denominator
    sign = -1 if numerator < 0 else 1
    quotient, remainder = divmod(abs(numerator), denominator)
    if remainder * 2 >= denominator:
        quotient += 1
    return sign * quotient


def finalize(bucket: Contribution) -> dict:
    """Turn an additive bucket into a display row, deriving the unit price."""
    amount_count = bucket.amount_count
    return {
        "zone_key": bucket.zone_key,
        "order_count": bucket.order_count,
        "amount_count": amount_count,
        "null_count": bucket.null_count,
        "zero_count": bucket.zero_count,
        "total_cents": bucket.total_cents,
        "unit_price_cents": (
            div_round_half_up(bucket.total_cents, amount_count) if amount_count else None
        ),
    }


def bucket_contribution(zone_key: str, row: dict) -> Contribution:
    """Rebuild the additive bucket from a stored/finished summary row."""
    return Contribution(
        zone_key=zone_key,
        order_count=row.get("order_count", 0),
        amount_count=row.get("amount_count", 0),
        null_count=row.get("null_count", 0),
        zero_count=row.get("zero_count", 0),
        total_cents=row.get("total_cents", 0),
    )


def zone_lookup(zones: list[dict]) -> dict[str, dict]:
    return {z["zone_id"]: z for z in zones}


def store_zone_map(stores: list[dict], zones: list[dict]) -> dict[str, str | None]:
    """Map every store to a real zone, or ``None`` when it is unassigned."""
    zone_ids = {z["zone_id"] for z in zones}
    result: dict[str, str | None] = {}
    for store in stores:
        zone_id = store.get("zone_id")
        result[store["store_id"]] = zone_id if zone_id in zone_ids else None
    return result


def zone_key_for(store_id: str | None, store_zone: dict[str, str | None]) -> str:
    """Region bucket for a sale, falling back to the unmatched bucket."""
    if not store_id:
        return UNMATCHED
    zone_id = store_zone.get(store_id)
    return zone_id or UNMATCHED


def contribution_for(sale: dict, store_zone: dict[str, str | None]) -> Contribution:
    zone_key = zone_key_for(sale.get("store_id"), store_zone)
    amount = sale.get("amount_cents")
    if amount is None:
        return Contribution(zone_key=zone_key, order_count=1, null_count=1)
    return Contribution(
        zone_key=zone_key,
        order_count=1,
        amount_count=1,
        zero_count=1 if amount == 0 else 0,
        total_cents=amount,
    )


def full_aggregate(
    sales: list[dict], stores: list[dict], zones: list[dict]
) -> dict[str, dict]:
    """Recompute every region bucket from scratch against one consistent input."""
    store_zone = store_zone_map(stores, zones)
    zone_by_id = zone_lookup(zones)
    buckets: dict[str, Contribution] = {}
    for sale in sales:
        if sale.get("status") != "active":
            continue
        c = contribution_for(sale, store_zone)
        buckets[c.zone_key] = buckets.get(c.zone_key, Contribution(c.zone_key)) + c

    result: dict[str, dict] = {}
    for zone_key, bucket in buckets.items():
        if bucket.order_count == 0:
            continue
        row = finalize(bucket)
        row["zone_name"] = (
            UNMATCHED_NAME if zone_key == UNMATCHED else zone_by_id[zone_key]["name"]
        )
        result[zone_key] = row
    return result


def fold_ledger(entries: list[dict], zones: list[dict]) -> dict[str, dict]:
    """Fold every signed ledger entry into region buckets (full ledger replay)."""
    zone_by_id = zone_lookup(zones)
    buckets: dict[str, Contribution] = {}
    for entry in entries:
        zone_key = entry["zone_key"]
        sign = 1 if entry["phase"] == "add" else -1
        signed = Contribution(
            zone_key=zone_key,
            order_count=sign * entry["order_count"],
            amount_count=sign * entry["amount_count"],
            null_count=sign * entry["null_count"],
            zero_count=sign * entry["zero_count"],
            total_cents=sign * entry["total_cents"],
        )
        buckets[zone_key] = buckets.get(zone_key, Contribution(zone_key)) + signed

    result: dict[str, dict] = {}
    for zone_key, bucket in buckets.items():
        if bucket.order_count == 0:
            continue
        row = finalize(bucket)
        row["zone_name"] = (
            UNMATCHED_NAME if zone_key == UNMATCHED else zone_by_id[zone_key]["name"]
        )
        result[zone_key] = row
    return result


def carry_summary(
    previous: dict[str, dict],
    group_deltas: dict[str, dict],
    zones: list[dict],
) -> dict[str, dict]:
    """Advance the running summary by one batch's net group deltas.

    This is the incremental path: the new summary is the previous snapshot plus
    the batch's signed deltas.  Groups whose order count reaches zero are
    removed, which is what makes "no group" different from "a group totalling
    zero".
    """
    zone_by_id = zone_lookup(zones)
    keys = set(previous) | set(group_deltas)
    result: dict[str, dict] = {}
    for zone_key in keys:
        bucket = Contribution(zone_key)
        if zone_key in previous:
            bucket = bucket + bucket_contribution(zone_key, previous[zone_key])
        if zone_key in group_deltas:
            delta = group_deltas[zone_key]
            bucket = bucket + Contribution(
                zone_key=zone_key,
                order_count=delta.get("net_orders", 0),
                amount_count=delta.get("net_amount_count", 0),
                null_count=delta.get("net_null_count", 0),
                zero_count=delta.get("net_zero_count", 0),
                total_cents=delta.get("net_total_cents", 0),
            )
        if bucket.order_count == 0:
            continue
        if bucket.order_count < 0 or bucket.amount_count < 0 or bucket.null_count < 0:
            raise ValueError(f"summary went negative for {zone_key}: {bucket.as_dict()}")
        row = finalize(bucket)
        row["zone_name"] = (
            UNMATCHED_NAME if zone_key == UNMATCHED else zone_by_id[zone_key]["name"]
        )
        result[zone_key] = row
    return result


def compare_summaries(incremental: dict[str, dict], full: dict[str, dict]) -> list[dict]:
    """Item-by-item comparison of the incremental summary and full recompute."""
    keys = sorted(set(incremental) | set(full), key=lambda k: (k == UNMATCHED, k))
    findings: list[dict] = []
    for zone_key in keys:
        inc = incremental.get(zone_key)
        complete = full.get(zone_key)
        zone_name = (inc or complete or {}).get("zone_name", zone_key)
        for measure in MEASURES:
            inc_value = inc.get(measure) if inc else None
            full_value = complete.get(measure) if complete else None
            findings.append(
                {
                    "zone_key": zone_key,
                    "zone_name": zone_name,
                    "measure": measure,
                    "incremental_value": inc_value,
                    "full_value": full_value,
                    "is_match": inc_value == full_value,
                }
            )
    return findings


# --------------------------------------------------------------------------
# Batch application
# --------------------------------------------------------------------------


@dataclass
class Entry:
    sale_id: str
    store_id: str | None
    phase: str  # 'retract' | 'add'
    reason: str
    zone_key: str
    order_count: int
    amount_count: int
    null_count: int
    zero_count: int
    total_cents: int
    old_amount_cents: int | None = None
    new_amount_cents: int | None = None
    old_zone_key: str | None = None
    new_zone_key: str | None = None

    def as_dict(self) -> dict:
        return {
            "sale_id": self.sale_id,
            "store_id": self.store_id,
            "phase": self.phase,
            "reason": self.reason,
            "zone_key": self.zone_key,
            "order_count": self.order_count,
            "amount_count": self.amount_count,
            "null_count": self.null_count,
            "zero_count": self.zero_count,
            "total_cents": self.total_cents,
            "old_amount_cents": self.old_amount_cents,
            "new_amount_cents": self.new_amount_cents,
            "old_zone_key": self.old_zone_key,
            "new_zone_key": self.new_zone_key,
        }


@dataclass
class AcceptedOp:
    op_index: int
    op_type: str
    sale_id: str | None
    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    affected_sale_ids: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "op_index": self.op_index,
            "op_type": self.op_type,
            "sale_id": self.sale_id,
            "warnings": self.warnings,
            "affected_sale_ids": self.affected_sale_ids,
            "entries": [e.as_dict() for e in self.entries],
        }


@dataclass
class Result:
    accepted: list[AcceptedOp]
    rejected: list[dict]
    sales_table: list[dict]  # the full working source table after the batch
    stores_table: list[dict]  # the full working store master after the batch

    @property
    def has_rejections(self) -> bool:
        return bool(self.rejected)


def apply_ops(
    ops: list[dict],
    sales: list[dict],
    stores: list[dict],
    zones: list[dict],
) -> Result:
    """Validate and apply a batch in memory, op by op.

    Rejected operations leave no trace: they contribute no ledger entries and
    do not mutate the working source state.  Validation happens before any
    mutation of the current op, so a rejection can never half-apply.
    """
    zone_ids = {z["zone_id"] for z in zones}
    sales_by_id: dict[str, dict] = {s["sale_id"]: dict(s) for s in sales}
    store_by_id: dict[str, dict] = {s["store_id"]: dict(s) for s in stores}
    store_zone = store_zone_map(stores, zones)

    accepted: list[AcceptedOp] = []
    rejected: list[dict] = []
    seen_keys: dict[str, int] = {}
    seen_store_moves: dict[str, int] = {}

    def reject(index: int, op: dict, exc: OpError) -> None:
        rejected.append(
            {
                "op_index": index,
                "op_type": op.get("type"),
                "sale_id": op.get("sale_id") or op.get("store_id"),
                "code": exc.code,
                "message": exc.message,
                "raw": op,
            }
        )

    for index, op in enumerate(ops):
        op_type = op.get("type")
        try:
            if not isinstance(op, dict):
                raise OpError("INVALID_OP", "操作必须是对象")
            if op_type not in {"insert", "update", "delete", "move_store"}:
                raise OpError("INVALID_OP", f"未知操作类型 {op_type!r}")

            if op_type == "move_store":
                store_id = (op.get("store_id") or "").strip()
                if not store_id:
                    raise OpError("INVALID_KEY", "缺少门店主键 store_id")
                if store_id in seen_store_moves:
                    raise OpError(
                        "DUPLICATE_KEY",
                        f"批次内门店迁移重复: {store_id}（已在第 {seen_store_moves[store_id] + 1} 条出现）",
                    )
                store = store_by_id.get(store_id)
                if store is None:
                    raise OpError("NOT_FOUND", f"门店不存在: {store_id}")
                new_zone = (op.get("zone_id") or "").strip() or None
                if new_zone is not None and new_zone not in zone_ids:
                    raise OpError("UNKNOWN_ZONE", f"区域不存在: {new_zone}")
                old_zone = store_zone.get(store_id)
                if (old_zone or None) == (new_zone or None):
                    raise OpError("NO_CHANGE", f"门店 {store_id} 的区域未发生变化")
                seen_store_moves[store_id] = index

                updated_store = dict(store, zone_id=new_zone, version=store["version"] + 1)
                store_by_id[store_id] = updated_store
                store_zone[store_id] = new_zone
                plan = AcceptedOp(index, op_type, None, raw=op)
                for sale_id in sorted(sales_by_id):
                    sale = sales_by_id[sale_id]
                    if sale.get("status") != "active" or sale.get("store_id") != store_id:
                        continue
                    amount = sale.get("amount_cents")
                    if amount is None:
                        old_c = Contribution(old_zone or UNMATCHED, order_count=1, null_count=1)
                        new_c = Contribution(new_zone or UNMATCHED, order_count=1, null_count=1)
                    else:
                        old_c = Contribution(
                            old_zone or UNMATCHED, order_count=1, amount_count=1,
                            zero_count=1 if amount == 0 else 0, total_cents=amount,
                        )
                        new_c = Contribution(
                            new_zone or UNMATCHED, order_count=1, amount_count=1,
                            zero_count=1 if amount == 0 else 0, total_cents=amount,
                        )
                    plan.affected_sale_ids.append(sale_id)
                    plan.entries.append(
                        Entry(sale_id, store_id, "retract", "store-move-retract",
                              old_c.zone_key, -1, -old_c.amount_count, -old_c.null_count,
                              -old_c.zero_count, -old_c.total_cents,
                              old_amount_cents=amount, new_amount_cents=amount,
                              old_zone_key=old_zone or UNMATCHED,
                              new_zone_key=new_zone or UNMATCHED)
                    )
                    plan.entries.append(
                        Entry(sale_id, store_id, "add", "store-move-add",
                              new_c.zone_key, 1, new_c.amount_count, new_c.null_count,
                              new_c.zero_count, new_c.total_cents,
                              old_amount_cents=amount, new_amount_cents=amount,
                              old_zone_key=old_zone or UNMATCHED,
                              new_zone_key=new_zone or UNMATCHED)
                    )
                if not plan.affected_sale_ids:
                    plan.warnings.append("该门店没有有效销售，区域已更新但不会产生任何分组的增减")
                accepted.append(plan)
                continue

            sale_id = (op.get("sale_id") or "").strip()
            if not sale_id:
                raise OpError("INVALID_KEY", "缺少业务主键 sale_id")
            new_sale_id = (op.get("new_sale_id") or "").strip() or None
            target_key = new_sale_id or sale_id
            existing = sales_by_id.get(sale_id)

            if op_type in {"insert", "update"}:
                if target_key in seen_keys:
                    raise OpError(
                        "DUPLICATE_KEY",
                        f"批次内主键重复: {target_key}（已在第 {seen_keys[target_key] + 1} 条出现）",
                    )
                if op_type == "update" and new_sale_id and new_sale_id in sales_by_id:
                    raise OpError("DUPLICATE_KEY", f"主键已存在于源表: {new_sale_id}")

            if op_type == "insert":
                if existing is not None:
                    raise OpError(
                        "DUPLICATE_KEY",
                        f"主键已存在于源表: {sale_id}"
                        + ("（该销售已被删除，仍占用主键）" if existing["status"] != "active" else ""),
                    )
                store_id = (op.get("store_id") or "").strip() or None
                amount = normalize_amount(op.get("amount"))
                plan = AcceptedOp(index, op_type, sale_id, raw=op)
                if amount is None:
                    plan.warnings.append("金额为空：计入订单数与空金额笔数，不计入金额合计与客单价")
                elif amount == 0:
                    plan.warnings.append("金额为零：属于非空金额，计入非空笔数与零金额笔数")
                if store_id and store_id not in store_by_id:
                    plan.warnings.append(f"门店 {store_id} 不在门店资料中，计入「未匹配」分组")
                elif store_id and store_zone.get(store_id) is None:
                    plan.warnings.append("门店未分配区域，计入「未匹配」分组")
                seen_keys[target_key] = index
                new_sale = {
                    "sale_id": sale_id,
                    "store_id": store_id,
                    "amount_cents": amount,
                    "status": "active",
                }
                sales_by_id[sale_id] = new_sale
                c = contribution_for(new_sale, store_zone)
                plan.entries.append(
                    Entry(sale_id, store_id, "add", "insert", c.zone_key, 1,
                          c.amount_count, c.null_count, c.zero_count, c.total_cents,
                          new_amount_cents=amount, new_zone_key=c.zone_key)
                )
                accepted.append(plan)

            elif op_type == "update":
                if existing is None:
                    raise OpError("NOT_FOUND", f"要更新的销售不存在: {sale_id}")
                if existing["status"] != "active":
                    raise OpError("NOT_FOUND", f"销售 {sale_id} 已被删除，不能更新")
                store_id = (
                    (op.get("store_id") or "").strip() or None
                    if "store_id" in op
                    else existing.get("store_id")
                )
                amount = (
                    normalize_amount(op.get("amount"))
                    if "amount" in op
                    else existing["amount_cents"]
                )
                plan = AcceptedOp(index, op_type, target_key, raw=op)
                if store_id and store_id not in store_by_id:
                    plan.warnings.append(f"门店 {store_id} 不在门店资料中，更新后计入「未匹配」分组")
                old_c = contribution_for(existing, store_zone)
                new_sale = dict(existing)
                new_sale.update(
                    {
                        "sale_id": target_key,
                        "store_id": store_id,
                        "amount_cents": amount,
                        "status": "active",
                    }
                )
                new_c = contribution_for(new_sale, store_zone)
                if target_key != sale_id:
                    del sales_by_id[sale_id]
                seen_keys[target_key] = index
                sales_by_id[target_key] = new_sale
                plan.affected_sale_ids = sorted({sale_id, target_key})
                if target_key != sale_id and new_sale_id:
                    plan.warnings.append(f"主键已重命名：{sale_id} → {target_key}")
                # Retract before add: old contribution leaves its region first.
                plan.entries.append(
                    Entry(sale_id, existing.get("store_id"), "retract", "update-retract",
                          old_c.zone_key, -1, -old_c.amount_count, -old_c.null_count,
                          -old_c.zero_count, -old_c.total_cents,
                          old_amount_cents=existing["amount_cents"], new_amount_cents=amount,
                          old_zone_key=old_c.zone_key, new_zone_key=new_c.zone_key)
                )
                plan.entries.append(
                    Entry(target_key, store_id, "add", "update-add",
                          new_c.zone_key, 1, new_c.amount_count, new_c.null_count,
                          new_c.zero_count, new_c.total_cents,
                          old_amount_cents=existing["amount_cents"], new_amount_cents=amount,
                          old_zone_key=old_c.zone_key, new_zone_key=new_c.zone_key)
                )
                accepted.append(plan)

            else:  # delete
                if existing is None:
                    raise OpError("NOT_FOUND", f"要删除的销售不存在: {sale_id}")
                if existing["status"] != "active":
                    raise OpError("ALREADY_DELETED", f"销售 {sale_id} 已被删除")
                old_c = contribution_for(existing, store_zone)
                plan = AcceptedOp(index, op_type, sale_id, raw=op)
                plan.affected_sale_ids = [sale_id]
                plan.entries.append(
                    Entry(sale_id, existing.get("store_id"), "retract", "delete",
                          old_c.zone_key, -1, -old_c.amount_count, -old_c.null_count,
                          -old_c.zero_count, -old_c.total_cents,
                          old_amount_cents=existing["amount_cents"],
                          old_zone_key=old_c.zone_key)
                )
                deleted = dict(existing, status="deleted")
                sales_by_id[sale_id] = deleted
                accepted.append(plan)
        except OpError as exc:
            reject(index, op if isinstance(op, dict) else {}, exc)

    return Result(
        accepted,
        rejected,
        [sales_by_id[k] for k in sorted(sales_by_id)],
        [store_by_id[k] for k in sorted(store_by_id)],
    )


def net_group_deltas(entries: list[Entry]) -> dict[str, dict]:
    """Aggregate signed entries into per-region retract/add/net totals."""
    groups: dict[str, dict] = {}

    def bucket(zone_key: str) -> dict:
        return groups.setdefault(
            zone_key,
            {
                "zone_key": zone_key,
                "retract_orders": 0,
                "add_orders": 0,
                "retract_amount_count": 0,
                "add_amount_count": 0,
                "retract_null_count": 0,
                "add_null_count": 0,
                "retract_zero_count": 0,
                "add_zero_count": 0,
                "retract_total_cents": 0,
                "add_total_cents": 0,
            },
        )

    for entry in entries:
        row = bucket(entry.zone_key)
        if entry.phase == "retract":
            row["retract_orders"] += entry.order_count
            row["retract_amount_count"] += entry.amount_count
            row["retract_null_count"] += entry.null_count
            row["retract_zero_count"] += entry.zero_count
            row["retract_total_cents"] += entry.total_cents
        else:
            row["add_orders"] += entry.order_count
            row["add_amount_count"] += entry.amount_count
            row["add_null_count"] += entry.null_count
            row["add_zero_count"] += entry.zero_count
            row["add_total_cents"] += entry.total_cents

    for row in groups.values():
        row["net_orders"] = row["add_orders"] + row["retract_orders"]
        row["net_amount_count"] = row["add_amount_count"] + row["retract_amount_count"]
        row["net_null_count"] = row["add_null_count"] + row["retract_null_count"]
        row["net_zero_count"] = row["add_zero_count"] + row["retract_zero_count"]
        row["net_total_cents"] = row["add_total_cents"] + row["retract_total_cents"]
    return groups
