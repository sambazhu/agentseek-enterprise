"""Conservative content admission for the bundled securities research template.

Retrieval scores and the department owning a document are not evidence of relevance.
These rules are necessary lexical checks, not a claim of semantic entailment.
Unknown question types remain gaps until a reviewed rule is supplied.
"""

import re

RELEVANCE_VERSION = "securities-content-v1"
BODY_ADMISSION_VERSION = "securities-body-v1"
EVIDENCE_USE_VERSION = "securities-evidence-use-v1"
DOMAIN_TERMS = ("证券", "券商", "资本市场", "投行业务", "经纪业务", "两融", "保荐")
QUESTION_TERMS = {
    "executive-summary.core-trends": ("趋势", "变化", "转型", "不确定", "增长", "下降", "行业周期", "高贝塔"),
    "industry-overview.digital-transformation": ("数字化", "转型"),
    "operating-benchmark.roe-and-structure": ("收入", "利润", "roe", "资本效率", "业务结构", "权益乘数", "杠杆乘数", "资本回报率"),
    "business-line-benchmark.five-lines": ("财富", "投行", "自营", "资管", "资产管理"),
    "action-recommendations.priorities": ("优先级", "适用条件", "依赖", "验证指标", "行动建议"),
}


def _noise_sentence(text: str) -> bool:
    # Remove only reference-only material for this check, not from the excerpt.
    remainder = re.sub(r"\[[^\]]*\]\([^)]*\)|https?://\S+|[\w/-]+\.(?:shtml|html|pdf)\)?", "", text)
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", remainder):
        return True
    return bool(re.search(r"^(?:本报告|本文|本研究)(?:将|从|围绕)", text.strip()) and
                re.search(r"展开|维度|章节|介绍|探讨", text) and
                not re.search(r"发现|统计|数据显示|增长|下降|达到|占比|\d\s*%", text))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.findall(r"[^。！？!?\n]+[。！？!?]?", text) if part.strip()]


def body_lines(content: str) -> tuple[str, ...]:
    """Ignore structural headings/metadata, not short prose or numeric table rows.

    This is a conservative structural heuristic, not a truth/entailment test.
    Original excerpts are never rewritten by this function.
    """
    result = []
    for line in content.splitlines():
        text = line.strip()
        if re.match(r"^#{1,6}\s|^[-=_]{3,}$", text):
            continue
        text = re.sub(r"\*\*|__", "", text).strip()
        if re.match(r"^(?:[-*]\s+)?(标题|作者|来源|发布日期|更新日期|数据截至|截至日期|文档类型|适用范围|密级|title|author|source|date)\s*[:：]", text, re.IGNORECASE):
            continue
        # A title without sentence structure must not qualify merely because it
        # includes both a securities label and a research keyword.
        if not re.search(r"[。！？!?；;]|\d\s*(?:%|％|亿元|万元|倍)|增长|下降|提高|降低|达到|占比|为目标|须落实|推动|应当|需要", text):
            continue
        content = "".join(sentence for sentence in _sentences(text) if not _noise_sentence(sentence))
        if content:
            result.append(content)
    return tuple(result)


def _direct_content(content: str, question_id: str, report_title: str) -> bool:
    text = "\n".join(body_lines(content)).casefold()
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


def _sentence_use(sentence: str, previous: str, question_id: str, report_title: str) -> str | None:
    if _noise_sentence(sentence) or not body_lines(sentence):
        return None
    has_domain = any(term in sentence for term in DOMAIN_TERMS)
    # One immediately preceding body sentence only; never a heading or another
    # paragraph. Explicit competing subjects cannot inherit a securities anchor.
    inherited = (
        not has_domain and any(term in previous for term in DOMAIN_TERMS)
        and not re.search(r"银行|保险|医院|学校|信息技术部|数据安全", sentence)
        and bool(re.search(r"^(?:这|该|其|上述|全行业|领先机构)|自营|FICC|财富管理", sentence))
    )
    scoped = f"{previous}{sentence}" if inherited else sentence
    if not has_domain and not inherited:
        return None
    if question_id in {"industry-overview.digital-transformation", "executive-summary.core-trends", "executive-summary.topic-specific-evidence"} and re.search(
        r"公司信息技术|本公司|本制度|完善公司.*治理|为适应.*数字化", scoped,
    ):
        if question_id == "executive-summary.topic-specific-evidence" and not _direct_content(sentence, question_id, report_title):
            return None
        return "company_case" if "数字化" in scoped or "转型" in scoped else None
    if question_id == "business-line-benchmark.five-lines" and "国际投行" in sentence and not re.search(
        r"投行业务|承销|并购|财富|自营|资管|资产管理", sentence,
    ):
        return "background"
    if _direct_content(sentence, question_id, report_title):
        return "direct"
    terms = QUESTION_TERMS.get(question_id, ())
    if inherited and any(term in sentence.casefold() for term in terms):
        return "background"
    background_terms = {
        "business-line-benchmark.five-lines": ("杠杆", "资本回报", "经营业绩", "周期", "公允价值", "机构交易", "衍生品", "通道盈利", "资产配置"),
        "executive-summary.core-trends": ("业绩", "周期", "流动性", "交投"),
    }
    if any(term in sentence for term in background_terms.get(question_id, ())):
        return "background"
    return None


def content_use(content: str, question_id: str, report_title: str, *, statement: str | None = None) -> str | None:
    """Classify original source sentences; never repair or synthesize a quote.

    Background and company cases are usable but cannot close a coverage gap.
    When statement is supplied, classify only full matching source sentences,
    preserving their punctuation and immediate same-paragraph context.
    """
    uses = []
    normalize = lambda value: re.sub(r"\s+", "", value).strip("。！？!?；;\"“”")
    for line in content.splitlines():
        if not body_lines(line):
            continue
        previous = ""
        for sentence in _sentences(line):
            if statement is None or normalize(statement) == normalize(sentence):
                use = _sentence_use(sentence, previous, question_id, report_title)
                if use:
                    uses.append(use)
            previous = sentence if body_lines(sentence) and not _noise_sentence(sentence) else ""
    return next((use for use in ("direct", "company_case", "background") if use in uses), None)


def relevant_content(content: str, question_id: str, report_title: str) -> bool:
    """Direct support only. Use content_use for bounded background admission."""
    return content_use(content, question_id, report_title) == "direct"


def is_evidence_sentence(statement: str, excerpts: list[str]) -> bool:
    """Require a complete source sentence; substring extraction can invert meaning."""
    normalize = lambda value: re.sub(r"\s+", "", value).strip("。！？!?；;\"“”")
    wanted = normalize(statement)
    return bool(wanted) and any(
        wanted == normalize(sentence)
        for excerpt in excerpts
        for sentence in re.split(r"[。！？!?\n]", excerpt)
    )
