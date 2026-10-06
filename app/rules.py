"""接收规则引擎：版本化规则定义、提交校验与污染判定计算。

规则按 (region, version) 唯一存储于数据库；判定单在出具时保存规则快照，
后续切换或新增版本不会改写历史结论。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# 首次启动时写入数据库的内置规则版本（种子数据）。
# 之后可通过 API 新增版本、切换启用版本，种子不会覆盖已有数据。
SEED_RULES: list[dict[str, Any]] = [
    {
        "region": "default",
        "version": "v1.0",
        "status": "active",
        "currency": "CNY",
        "categories": {
            "PET":  {"fee_per_kg": 1.20, "desc": "PET 瓶类"},
            "HDPE": {"fee_per_kg": 1.00, "desc": "高密度聚乙烯"},
            "PP":   {"fee_per_kg": 0.90, "desc": "聚丙烯"},
            "纸类": {"fee_per_kg": 0.60, "desc": "纸及纸板"},
            "金属": {"fee_per_kg": 0.40, "desc": "金属杂质"},
            "玻璃": {"fee_per_kg": 0.50, "desc": "玻璃杂质"},
            "织物": {"fee_per_kg": 0.80, "desc": "纺织物"},
            "其他": {"fee_per_kg": 1.50, "desc": "未列明杂物"},
        },
        "levels": [
            {"code": "A", "name": "合格",     "max_ratio": 0.02, "conclusion": "合格接收", "fee_multiplier": 0.0},
            {"code": "B", "name": "轻度污染", "max_ratio": 0.08, "conclusion": "降级接收", "fee_multiplier": 1.0},
            {"code": "C", "name": "重度污染", "max_ratio": 1.00, "conclusion": "拒收",     "fee_multiplier": 1.5},
        ],
    },
    {
        # 收紧后的版本：阈值更低、费率更高，用于演示规则切换与历史固定。
        "region": "default",
        "version": "v1.1",
        "status": "inactive",
        "currency": "CNY",
        "categories": {
            "PET":  {"fee_per_kg": 1.50, "desc": "PET 瓶类"},
            "HDPE": {"fee_per_kg": 1.20, "desc": "高密度聚乙烯"},
            "PP":   {"fee_per_kg": 1.10, "desc": "聚丙烯"},
            "纸类": {"fee_per_kg": 0.80, "desc": "纸及纸板"},
            "金属": {"fee_per_kg": 0.50, "desc": "金属杂质"},
            "玻璃": {"fee_per_kg": 0.60, "desc": "玻璃杂质"},
            "织物": {"fee_per_kg": 1.00, "desc": "纺织物"},
            "其他": {"fee_per_kg": 2.00, "desc": "未列明杂物"},
        },
        "levels": [
            {"code": "A", "name": "合格",     "max_ratio": 0.015, "conclusion": "合格接收", "fee_multiplier": 0.0},
            {"code": "B", "name": "轻度污染", "max_ratio": 0.05,  "conclusion": "降级接收", "fee_multiplier": 1.0},
            {"code": "C", "name": "重度污染", "max_ratio": 1.00,  "conclusion": "拒收",     "fee_multiplier": 2.0},
        ],
    },
    {
        "region": "华东",
        "version": "v1.0",
        "status": "active",
        "currency": "CNY",
        "categories": {
            "PET":  {"fee_per_kg": 1.30, "desc": "PET 瓶类"},
            "纸类": {"fee_per_kg": 0.70, "desc": "纸及纸板"},
            "金属": {"fee_per_kg": 0.45, "desc": "金属杂质"},
            "厨余": {"fee_per_kg": 1.80, "desc": "厨余及有机物"},
            "其他": {"fee_per_kg": 1.60, "desc": "未列明杂物"},
        },
        "levels": [
            {"code": "A", "name": "合格",     "max_ratio": 0.02, "conclusion": "合格接收", "fee_multiplier": 0.0},
            {"code": "B", "name": "轻度污染", "max_ratio": 0.06, "conclusion": "降级接收", "fee_multiplier": 1.0},
            {"code": "C", "name": "重度污染", "max_ratio": 1.00, "conclusion": "拒收",     "fee_multiplier": 1.8},
        ],
    },
]


def _money(value: float) -> float:
    """金额按分位四舍五入。"""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def validate_submission(rule: dict, weight_kg: float | None,
                        materials: list[dict]) -> list[dict]:
    """业务校验，返回字段错误列表（空列表表示通过）。"""
    errors: list[dict] = []
    weight_ok = isinstance(weight_kg, (int, float)) and weight_kg > 0
    if not weight_ok:
        errors.append({"field": "weight_kg", "message": "批次重量必须为正数"})

    total = 0.0
    for i, m in enumerate(materials):
        cat = (m.get("category") or "").strip()
        cont = m.get("contamination_kg")
        if cat not in rule["categories"]:
            errors.append({
                "field": f"materials[{i}].category",
                "message": f"未知物料类别：{cat or '(空)'}，当前规则版本支持的类别："
                           f"{', '.join(rule['categories'])}",
            })
        if cont is None:
            errors.append({"field": f"materials[{i}].contamination_kg",
                           "message": "污染量必填"})
            continue
        if not isinstance(cont, (int, float)) or cont < 0:
            errors.append({"field": f"materials[{i}].contamination_kg",
                           "message": "污染量不能为负数"})
            continue
        total += cont
        if weight_ok and cont > weight_kg:
            errors.append({"field": f"materials[{i}].contamination_kg",
                           "message": f"单项污染量 {cont:g}kg 超过批次重量 {weight_kg:g}kg"})
    if weight_ok and total > weight_kg:
        errors.append({"field": "materials",
                       "message": f"污染总量 {total:g}kg 超过批次重量 {weight_kg:g}kg"})
    return errors


def evaluate(rule: dict, weight_kg: float, materials: list[dict]) -> dict:
    """按规则版本计算污染等级、处置结论、扣费明细与证据说明。"""
    categories = rule["categories"]
    total_cont = round(sum(m["contamination_kg"] for m in materials), 4)
    ratio = total_cont / weight_kg

    levels = sorted(rule["levels"], key=lambda lv: lv["max_ratio"])
    level = next((lv for lv in levels if ratio <= lv["max_ratio"]), levels[-1])
    mult = level["fee_multiplier"]

    fee_lines: list[dict] = []
    evidence: list[str] = [
        f"依据规则版本 {rule['region']}/{rule['version']} 判定。"
    ]
    total_fee = 0.0
    for m in materials:
        rate = categories[m["category"]]["fee_per_kg"]
        fee = _money(m["contamination_kg"] * rate * mult)
        total_fee = _money(total_fee + fee)
        fee_lines.append({
            "category": m["category"],
            "contamination_kg": m["contamination_kg"],
            "fee_per_kg": rate,
            "multiplier": mult,
            "fee": fee,
        })
        evidence.append(
            f"{m['category']}：污染 {m['contamination_kg']:g}kg × 费率 {rate:g} 元/kg "
            f"× 等级系数 {mult:g} = {fee:.2f} 元"
        )
    evidence.append(
        f"批次重量 {weight_kg:g}kg，污染总量 {total_cont:g}kg，污染率 {ratio * 100:.2f}%。"
    )
    evidence.append(
        f"污染率命中等级 {level['code']}（{level['name']}，阈值 ≤ "
        f"{level['max_ratio'] * 100:.0f}%），处置结论：{level['conclusion']}。"
    )
    evidence.append("当前等级免扣费。" if mult == 0
                    else f"扣费合计 {total_fee:.2f} 元。")

    return {
        "total_contamination_kg": total_cont,
        "contamination_ratio": round(ratio, 6),
        "level": {"code": level["code"], "name": level["name"],
                  "max_ratio": level["max_ratio"]},
        "conclusion": level["conclusion"],
        "fee_lines": fee_lines,
        "total_fee": total_fee,
        "currency": rule.get("currency", "CNY"),
        "evidence": evidence,
    }


def _by_category(lines: list[dict]) -> dict:
    """扣费明细按类别聚合（同类别多行时合并污染量与扣费）。"""
    agg: dict[str, dict] = {}
    for line in lines:
        cur = agg.setdefault(line["category"], {
            "fee_per_kg": line["fee_per_kg"],
            "multiplier": line["multiplier"],
            "contamination_kg": 0.0,
            "fee": 0.0,
        })
        cur["contamination_kg"] += line["contamination_kg"]
        cur["fee"] = _money(cur["fee"] + line["fee"])
    return agg


def diff_results(original: dict, review: dict) -> dict:
    """对比原判定结论与复核重算结果，输出结构化差异项与中文说明。

    original / review 均需包含 level、conclusion、fee_lines、total_fee。
    """
    items: list[str] = []
    o_level, r_level = original["level"], review["level"]
    level_changed = o_level["code"] != r_level["code"]
    conclusion_changed = original["conclusion"] != review["conclusion"]
    if level_changed:
        items.append(f"等级变化：{o_level['code']}（{o_level['name']}）→ "
                     f"{r_level['code']}（{r_level['name']}）。")
    else:
        items.append(f"等级不变：{r_level['code']}（{r_level['name']}）。")
    if conclusion_changed:
        items.append(f"处置结论变化：{original['conclusion']} → "
                     f"{review['conclusion']}。")
    else:
        items.append(f"处置结论不变：{review['conclusion']}。")

    orig_lines = _by_category(original["fee_lines"])
    fee_line_diffs: list[dict] = []
    for cat, r in _by_category(review["fee_lines"]).items():
        o = orig_lines.get(cat)
        o_rate = o["fee_per_kg"] if o else None
        o_mult = o["multiplier"] if o else None
        o_fee = o["fee"] if o else 0.0
        delta = _money(r["fee"] - o_fee)
        if o and o_rate == r["fee_per_kg"] and o_mult == r["multiplier"] and not delta:
            continue
        fee_line_diffs.append({
            "category": cat,
            "original_fee_per_kg": o_rate,
            "review_fee_per_kg": r["fee_per_kg"],
            "original_multiplier": o_mult,
            "review_multiplier": r["multiplier"],
            "original_fee": o_fee,
            "review_fee": r["fee"],
            "delta": delta,
        })
        items.append(f"{cat}：费率 {o_rate} → {r['fee_per_kg']} 元/kg，"
                     f"扣费 {o_fee:.2f} → {r['fee']:.2f} 元（{delta:+.2f}）。")

    total_delta = _money(review["total_fee"] - original["total_fee"])
    items.append(f"扣费合计 {original['total_fee']:.2f} → "
                 f"{review['total_fee']:.2f} 元（{total_delta:+.2f}）。")
    if (not level_changed and not conclusion_changed
            and not fee_line_diffs and not total_delta):
        items.append("复核结果与原判定一致。")
    return {
        "level_changed": level_changed,
        "conclusion_changed": conclusion_changed,
        "total_fee_delta": total_delta,
        "fee_line_diffs": fee_line_diffs,
        "items": items,
    }


def validate_rule(body: dict) -> list[dict]:
    """校验通过 API 提交的规则版本定义。"""
    errors: list[dict] = []
    categories = body.get("categories") or {}
    if not categories:
        errors.append({"field": "categories", "message": "至少定义一个物料类别"})
    for name, cfg in categories.items():
        fee = (cfg or {}).get("fee_per_kg")
        if not isinstance(fee, (int, float)) or fee < 0:
            errors.append({"field": f"categories.{name}.fee_per_kg",
                           "message": "费率必须为不小于 0 的数字"})

    levels = body.get("levels") or []
    if not levels:
        errors.append({"field": "levels", "message": "至少定义一个污染等级"})
    codes: set[str] = set()
    for i, lv in enumerate(levels):
        code = (lv.get("code") or "").strip()
        if not code:
            errors.append({"field": f"levels[{i}].code", "message": "等级编码必填"})
        elif code in codes:
            errors.append({"field": f"levels[{i}].code", "message": f"等级编码重复：{code}"})
        codes.add(code)
        ratio = lv.get("max_ratio")
        if not isinstance(ratio, (int, float)) or not 0 < ratio <= 1:
            errors.append({"field": f"levels[{i}].max_ratio",
                           "message": "阈值须在 (0, 1] 区间"})
        mult = lv.get("fee_multiplier")
        if not isinstance(mult, (int, float)) or mult < 0:
            errors.append({"field": f"levels[{i}].fee_multiplier",
                           "message": "扣费系数必须为不小于 0 的数字"})
        if not (lv.get("conclusion") or "").strip():
            errors.append({"field": f"levels[{i}].conclusion", "message": "处置结论必填"})
    if levels and max(lv.get("max_ratio") or 0 for lv in levels) < 1:
        errors.append({"field": "levels",
                       "message": "最高等级阈值必须为 1（覆盖全部污染率区间）"})
    return errors
