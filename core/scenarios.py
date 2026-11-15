"""Editable sample batches for the demo page.

Each scenario is plain JSON the page drops into the batch editor.  They are
deliberately *not* auto-applied: the reader edits and submits them, and the
page records the batch exactly like any other.
"""

from __future__ import annotations


def _insert(sale_id: str, store_id: str, amount) -> dict:
    return {"type": "insert", "sale_id": sale_id, "store_id": store_id, "amount": amount}


SCENARIOS = [
    {
        "id": "duplicate-key",
        "title": "重复键拒收",
        "summary": "同一批次内重复主键、以及与源表冲突的主键都会被拒收；批次里其余合法操作仍然生效。",
        "why": "重复键不能静默覆盖，也不能被重复累计。",
        "payload": {
            "label": "场景：重复键拒收",
            "strict": False,
            "ops": [
                _insert("SO-1001", "S-101", "10.00"),
                _insert("SO-9001", "S-201", "20.00"),
                _insert("SO-9001", "S-202", "30.00"),
                _insert("SO-9002", "S-301", None),
            ],
        },
    },
    {
        "id": "null-amount",
        "title": "空金额、零金额与非空金额",
        "summary": "三条新增销售：一条金额为空、一条金额为零、一条金额正常。观察订单数、非空金额笔数、零金额笔数与客单价如何分别变化。",
        "why": "空值不是零：空金额不进合计、不进非空笔数；零金额进非空笔数但不改变合计。",
        "payload": {
            "label": "场景：空金额与零金额",
            "strict": True,
            "ops": [
                _insert("SO-9101", "S-301", None),
                _insert("SO-9102", "S-301", "0"),
                _insert("SO-9103", "S-301", "12.30"),
            ],
        },
    },
    {
        "id": "zone-migration",
        "title": "区域迁移（更新 + 门店换区）",
        "summary": "先更新一条销售的门店（北区 → 东区），再把一家南区门店整体迁到西区。每条更新都先撤回旧贡献、再加入新贡献。",
        "why": "区域迁移必须同时改动两个分组：旧区域减、新区域加，不能只加不减。",
        "payload": {
            "label": "场景：区域迁移",
            "strict": True,
            "ops": [
                {"type": "update", "sale_id": "SO-1005", "store_id": "S-201"},
                {"type": "move_store", "store_id": "S-301", "zone_id": "Z-WEST"},
            ],
        },
    },
    {
        "id": "delete-region",
        "title": "删除南区，同时留下一个合计为零的北区",
        "summary": "删除南区的两条销售（该区域随后完全没有分组）；北区则只删除有金额的销售，留下空金额与零金额两条。北区仍有分组，但合计为 0、客单价为空。",
        "why": "「没有分组」和「合计为零」是两种不同状态：前者在汇总里不存在，后者存在但金额为 0。",
        "payload": {
            "label": "场景：删除南区 + 北区只剩空/零金额",
            "strict": True,
            "ops": [
                {"type": "delete", "sale_id": "SO-1008"},
                {"type": "delete", "sale_id": "SO-1009"},
                {"type": "delete", "sale_id": "SO-1001"},
                {"type": "delete", "sale_id": "SO-1002"},
            ],
        },
        "expect": "南区不再出现在汇总中；北区仍在，订单数 2、非空金额笔数 0、合计 0.00、客单价为空。",
    },
    {
        "id": "crash-recovery",
        "title": "中途失败与恢复",
        "summary": "三条新增，在写入第 2 条后注入故障。整批回滚，源表与汇总保持不变；去掉故障注入后重新提交同一批次即可正确恢复。",
        "why": "批次明细和汇总必须在同一事务里提交，失败不能留下半批。",
        "payload": {
            "label": "场景：中途失败恢复",
            "strict": True,
            "fail_after": 2,
            "ops": [
                _insert("SO-9201", "S-101", "11.00"),
                _insert("SO-9202", "S-102", None),
                _insert("SO-9203", "S-401", "0"),
            ],
        },
        "retry_payload": {
            "label": "场景：中途失败恢复（重试）",
            "strict": True,
            "ops": [
                _insert("SO-9201", "S-101", "11.00"),
                _insert("SO-9202", "S-102", None),
                _insert("SO-9203", "S-401", "0"),
            ],
        },
    },
]


def scenarios() -> list[dict]:
    return SCENARIOS
