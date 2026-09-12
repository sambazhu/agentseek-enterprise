"""Conservative content admission for the bundled securities research template.

Retrieval scores and the department owning a document are not evidence of relevance.
These rules are necessary lexical checks, not a claim of semantic entailment.
Unknown question types remain gaps until a reviewed rule is supplied.
"""

import re

RELEVANCE_VERSION = "securities-content-v1"
DOMAIN_TERMS = ("证券", "券商", "资本市场", "投行业务", "经纪业务", "两融", "保荐")
QUESTION_TERMS = {
    "executive-summary.core-trends": ("趋势", "变化", "转型", "不确定", "增长", "下降"),
    "industry-overview.digital-transformation": ("数字化", "转型"),
    "operating-benchmark.roe-and-structure": ("收入", "利润", "roe", "资本效率", "业务结构"),
    "business-line-benchmark.five-lines": ("财富", "投行", "自营", "资管", "资产管理"),
    "action-recommendations.priorities": ("优先级", "适用条件", "依赖", "验证指标", "行动建议"),
}


def relevant_content(content: str, question_id: str, report_title: str) -> bool:
    text = content.casefold()
    if not any(term in text for term in DOMAIN_TERMS):
        return False
    if question_id == "executive-summary.topic-specific-evidence":
        # Topic search needs the specific topic, not just a generic securities label.
        topic = re.sub(r"证券行业|证券公司|研究报告|报告|研究|分析|[\W_]", "", report_title.casefold())
        return len(topic) >= 3 and any(
            topic in sentence and any(term in sentence for term in DOMAIN_TERMS)
            for sentence in re.split(r"[。！？!?\n]", text)
        )
    terms = QUESTION_TERMS.get(question_id)
    if not terms:
        return False
    # A domain label in a header must not qualify unrelated sentences below it.
    return any(
        any(term in sentence for term in DOMAIN_TERMS) and any(term in sentence for term in terms)
        for sentence in re.split(r"[。！？!?\n]", text)
    )


def is_evidence_sentence(statement: str, excerpts: list[str]) -> bool:
    """Require a complete source sentence; substring extraction can invert meaning."""
    normalize = lambda value: re.sub(r"\s+", "", value).strip("。！？!?；;\"“”")
    wanted = normalize(statement)
    return bool(wanted) and any(
        wanted == normalize(sentence)
        for excerpt in excerpts
        for sentence in re.split(r"[。！？!?\n]", excerpt)
    )
