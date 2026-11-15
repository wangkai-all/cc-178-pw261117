"""Fictional sample dataset for the local incremental roll-up page.

All names, stores, and sales are invented.  The dataset is deliberately shaped
to exercise the rules the page must survive:

* one zero-amount sale (a present amount of 0, not a missing one);
* one null-amount sale (counted as an order, not as an amount);
* one sale whose store is not in the store master (unmatched group);
* one store with no region (unmatched group);
* every region is seeded so the page can be edited from a realistic start.
"""

from __future__ import annotations

ZONES = [
    {"zone_id": "Z-NORTH", "name": "北区", "ordinal": 1},
    {"zone_id": "Z-EAST", "name": "东区", "ordinal": 2},
    {"zone_id": "Z-SOUTH", "name": "南区", "ordinal": 3},
    {"zone_id": "Z-WEST", "name": "西区", "ordinal": 4},
]

STORES = [
    {"store_id": "S-101", "name": "晨光路店", "zone_id": "Z-NORTH"},
    {"store_id": "S-102", "name": "松林街店", "zone_id": "Z-NORTH"},
    {"store_id": "S-201", "name": "东岸广场店", "zone_id": "Z-EAST"},
    {"store_id": "S-202", "name": "梧桐里店", "zone_id": "Z-EAST"},
    {"store_id": "S-301", "name": "南湾店", "zone_id": "Z-SOUTH"},
    {"store_id": "S-302", "name": "稻香路店", "zone_id": "Z-SOUTH"},
    {"store_id": "S-401", "name": "西城集市店", "zone_id": "Z-WEST"},
    {"store_id": "S-501", "name": "临江新店", "zone_id": None},
]

# amount is intentionally a JSON-ish scalar: None means missing, 0 means zero.
SALES = [
    {"sale_id": "SO-1001", "store_id": "S-101", "amount": "128.50"},
    {"sale_id": "SO-1002", "store_id": "S-101", "amount": "64.00"},
    {"sale_id": "SO-1003", "store_id": "S-102", "amount": None},
    {"sale_id": "SO-1004", "store_id": "S-102", "amount": "0"},
    {"sale_id": "SO-1005", "store_id": "S-201", "amount": "299.90"},
    {"sale_id": "SO-1006", "store_id": "S-201", "amount": "41.20"},
    {"sale_id": "SO-1007", "store_id": "S-202", "amount": "88.80"},
    {"sale_id": "SO-1008", "store_id": "S-301", "amount": "152.00"},
    {"sale_id": "SO-1009", "store_id": "S-302", "amount": "23.45"},
    {"sale_id": "SO-1010", "store_id": "S-401", "amount": "310.00"},
    {"sale_id": "SO-1011", "store_id": "S-401", "amount": "77.70"},
    {"sale_id": "SO-1012", "store_id": "S-599", "amount": "55.00"},  # unknown store
    {"sale_id": "SO-1013", "store_id": "S-501", "amount": "19.90"},  # no region
]


def seed_payload() -> dict:
    """The batch payload that seeds a fresh database."""
    return {
        "label": "初始样例批次",
        "ops": [
            {
                "type": "insert",
                "sale_id": sale["sale_id"],
                "store_id": sale["store_id"],
                "amount": sale["amount"],
            }
            for sale in SALES
        ],
    }
