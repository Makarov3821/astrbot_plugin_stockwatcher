"""Exact references to facts actually supplied to the model, with auditable failures."""

from .domain import ValidationError

CITATION_ROOTS = ("metrics", "portfolio", "preference", "source_gaps")


class CitationError(ValidationError):
    def __init__(self, field, code, description, values=None):
        self.diagnostic = {"field": field, "code": code, "rejected_values": values or []}
        suffix = (
            "；实际引用：" + "、".join(self.diagnostic["rejected_values"][:3]) if values else ""
        )
        super().__init__(f"AI引用校验失败：{field} {description}。{suffix}")


def citation_scope(packet):
    """Enumerate real field paths. No prefix/fuzzy match or invented-ID correction."""
    known = {item["evidence_id"] for item in packet["evidence"]["items"]}

    def visit(path, value, depth=0):
        known.add(path)
        if depth >= 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.isidentifier():
                    visit(f"{path}.{key}", child, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(f"{path}.{index}", child, depth + 1)

    for root in CITATION_ROOTS:
        if root in packet:
            visit(root, packet[root])
    return known


def validate_ids(value, known, *, nonempty=False, field="evidence_ids"):
    if not isinstance(value, list):
        raise CitationError(field, "invalid_type", "引用格式错误，需要字符串数组")
    if len(value) > 10:
        raise CitationError(field, "too_many", "引用数量超过10条")
    if nonempty and not value:
        raise CitationError(field, "missing", "缺少必要引用")
    if any(not isinstance(item, str) for item in value):
        raise CitationError(field, "invalid_item_type", "引用格式错误，数组中含有非字符串")
    unknown = [item[:200] for item in value if item not in known]
    if unknown:
        raise CitationError(
            field, "unknown_reference", "含有未提供的证据ID或字段路径", unknown[:10]
        )
    return list(dict.fromkeys(value))
