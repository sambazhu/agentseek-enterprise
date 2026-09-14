"""Bounded, content-free report diagnostics using the existing event sink."""

from collections.abc import Sequence


def record_source_diagnostic(*, work_id: str, question_id: str, chunk_id: str, rank: int, reason: str) -> None:
    from agentseek_enterprise.observability import get_event_writer

    try:
        writer = get_event_writer()
        writer.emit(
            "report.source_admission", stage="source_admission", reason_code=reason,
            protocol_version="securities-body-v1",
            evidence_use_version="securities-evidence-use-v1",
            work_fingerprint=writer.identity_key(work_id),
            question_fingerprint=writer.identity_key(question_id),
            chunk_fingerprint=writer.identity_key(chunk_id), rank=rank,
        )
    except Exception:
        return


def record_draft_diagnostic(
    *, reason: str, work_id: str, outline_version: int, brief_version: int,
    proposals: Sequence[object], rejected: object | None = None,
) -> None:
    """Never persist claim text, excerpts, raw tool arguments or exception strings."""
    from agentseek_enterprise.observability import get_event_writer

    try:
        writer = get_event_writer()
        fields = {
            "stage": "draft_claim_validation", "reason_code": reason,
            "protocol_version": "securities-body-v1",
            "evidence_use_version": "securities-evidence-use-v1",
            "work_fingerprint": writer.identity_key(work_id),
            "outline_version": outline_version, "brief_version": brief_version,
            "proposal_count": len(proposals),
            "fact_count": sum(getattr(p, "claim_type", None) == "fact" for p in proposals),
            "inference_count": sum(getattr(p, "claim_type", None) == "inference" for p in proposals),
        }
        if rejected is not None:
            fields.update({
                "claim_type": str(getattr(rejected, "claim_type", "unknown")),
                "section_fingerprint": writer.identity_key(getattr(rejected, "section_id", "")),
                "claim_fingerprint": writer.identity_key(getattr(rejected, "statement", "")),
                "evidence_fingerprints": [writer.identity_key(key) for key in getattr(rejected, "evidence_ids", ())[:20]],
                "evidence_count": len(getattr(rejected, "evidence_ids", ())),
            })
        writer.emit("report.draft_validation", **fields)
    except Exception:
        # Observability failure must never turn a rejected batch into a write.
        return
