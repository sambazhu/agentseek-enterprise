"""Synthetic fixtures only; no private review-sheet excerpts."""

import pytest
from enterprise_wecom_digital_employee.evidence_relevance import content_use, is_evidence_sentence, relevant_content
from enterprise_wecom_digital_employee.report_outline import OutlineQuestion, OutlineSection

FIVE = "business-line-benchmark.five-lines"
DIGITAL = "industry-overview.digital-transformation"
CORE = "executive-summary.core-trends"
ROE = "operating-benchmark.roe-and-structure"
TITLE = "证券行业数字化转型研究报告"


@pytest.mark.parametrize("statement", ["证券行业数字化转型的目标是服务客户。", "证券行业数字化转型的目标是服务客户"])
def test_matching_source_punctuation_not_model_punctuation_drives_relevance(statement):
    source = "证券行业数字化转型的目标是服务客户。"
    assert is_evidence_sentence(statement, [source])
    assert content_use(source, DIGITAL, TITLE, statement=statement) == "direct"


@pytest.mark.parametrize("statement", ["证券行业数字化转型的目标不是服务客户。", "转型的目标是服务客户。", "证券行业数字化转型的目标是盈利。"])
def test_quote_changes_never_acquire_source_context(statement):
    source = "证券行业数字化转型的目标是服务客户。"
    assert not is_evidence_sentence(statement, [source])
    assert content_use(source, DIGITAL, TITLE, statement=statement) is None


@pytest.mark.parametrize("source", [
    "本报告从业务、监管两个维度展开，提供证券投行分析框架。",
    "tail123.shtml)，[来源](https://example.invalid/ref)。",
    "# 证券行业财富管理转型\n自营配置收益增长。",
    "证券行业财富管理转型。\n这种转型推动自营配置变化。",
    "证券行业财富管理转型。银行自营配置收益增长。",
    "证券行业财富管理转型。信息技术部数据安全策略需要调整。",
])
def test_noise_or_cross_boundary_inheritance_is_not_a_claim(source):
    last = source.split("\n")[-1].split("。")[-2] + "。" if source.endswith("。") else source
    assert content_use(source, FIVE, TITLE, statement=last) is None


def test_local_explicit_subject_supports_background_without_closing_question():
    source = "证券公司讨论了财富管理业务。这种转型推动自营配置变化。"
    target = "这种转型推动自营配置变化。"
    assert content_use(source, FIVE, TITLE, statement=target) == "background"
    assert content_use(target, FIVE, TITLE) is None


@pytest.mark.parametrize("source,question,use", [
    ("券商杠杆乘数提高至3倍。", ROE, "direct"),
    ("券商杠杆乘数提高至3倍。", FIVE, "background"),
    ("券商与国际投行的杠杆存在差异。", FIVE, "background"),
    ("本公司为适应证券市场数字化转型趋势，完善公司信息技术治理。", DIGITAL, "company_case"),
    ("证券公司数字化转型需要完善治理责任。", DIGITAL, "direct"),
    ("数据安全策略需要调整。", FIVE, None),
    ("本报告发现证券公司投行业务收入增长。", FIVE, "direct"),
    ("证券公司投行业务收入增长。[来源](https://example.invalid/ref)。", FIVE, "direct"),
])
def test_usage_is_question_and_scope_specific(source, question, use):
    assert content_use(source, question, TITLE) == use
    assert relevant_content(source, question, TITLE) == (use == "direct")


def test_background_outline_round_trip_retains_gap_and_old_payload_parses():
    question = OutlineQuestion(FIVE, "业务对标", background_source_ids=("background-1",))
    restored = OutlineQuestion.from_payload(question.to_payload())
    section = OutlineSection("business", "业务", (restored,))
    assert section.source_ids == ("background-1",)
    assert section.unresolved_question_ids == (FIVE,)
    legacy = {"question_id": FIVE, "prompt": "业务对标", "source_ids": [], "evidence_status": "unresolved"}
    assert OutlineQuestion.from_payload(legacy).background_source_ids == ()
