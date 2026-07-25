"""Structured Phase 1.6 contracts for evidence judgment and convergence."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


LEGACY_SUPPORT_STATUSES = {
    "supported",
    "weak",
    "needs_manual_check",
    "unsupported",
    "contradicted",
}

TRUST_SUPPORT_STATUSES = {
    "verified",
    "weak_support",
    "unsupported",
    "contradicted",
    "verified_by_regulator",
    "contradicted_by_regulator",
    "contradicted_by_imported_database",
    "regulator_unavailable",
    "data_stale",
    "needs_manual_check",
}

SUPPORT_STATUSES = LEGACY_SUPPORT_STATUSES | TRUST_SUPPORT_STATUSES

HARD_STOP_STATUSES = {
    "contradicted_by_regulator",
    "contradicted_by_imported_database",
}

EVIDENCE_CLASSES = {
    "private_document",
    "imported_market_database",
    "public_web",
    "regulator_ground_truth",
    "manual_fund_memory",
    "external_ai_memo",
}

LENS_NAMES = ("vc", "market", "competition", "product", "traction", "risk")


@dataclass(frozen=True)
class EvidencePacket:
    """Normalized evidence unit from web, mock, workspace, or future PageIndex."""

    evidence_id: str
    source_uri: str
    title: str
    excerpt: str
    source_type: str
    evidence_class: str = "public_web"
    locator: dict[str, Any] = field(default_factory=dict)
    retrieval_reason: str = ""
    confidence: float = 0.5
    query_metadata: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)
    pageindex_doc_id: str | None = None
    pageindex_chunk_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClaimRecord:
    """Deterministic claim extracted from the memo schema."""

    claim_id: str
    text: str
    subject: str
    claim_type: str
    memo_section: str
    cited_evidence_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentRecord:
    """Replayable source record for a locally indexed workspace document."""

    source_id: str
    source_path: str
    source_uri: str
    title: str
    extension: str
    content_hash: str
    converted_hash: str
    byte_size: int
    modified_time: float
    converter: str
    indexed_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceManifest:
    """Exact document set used by a workspace-backed run."""

    workspace: str
    index_path: str
    generated_at: str
    documents: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunManifest:
    """Replay metadata for a research or external memo audit run."""

    run_id: str
    mode: str
    created_at: str
    workspace_path: str | None = None
    thesis: str | None = None
    memo_path: str | None = None
    source_manifest_hash: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SupportAssessment:
    """Claim-to-evidence support judgment."""

    claim_id: str
    status: str
    reasoning: str
    best_evidence_ids: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        if self.status not in SUPPORT_STATUSES:
            raise ValueError(f"Unknown support status: {self.status}")
        return asdict(self)


@dataclass(frozen=True)
class ConvergenceReport:
    """Iteration-level convergence status."""

    score: float
    iteration_delta: float
    contradiction_count: int
    unsupported_ratio: float
    stop_reason: str
    hard_stop_count: int = 0
    next_research_actions: list[str] = field(default_factory=list)
    support_distribution: dict[str, int] = field(default_factory=dict)
    council_verdict: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LensRecord:
    """Evidence-backed view of one investor review lens."""

    lens: str
    support_status: str
    rationale: str
    key_evidence_ids: list[str] = field(default_factory=list)
    representative_claim_ids: list[str] = field(default_factory=list)
    support_distribution: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        if self.lens not in LENS_NAMES:
            raise ValueError(f"Unknown lens: {self.lens}")
        if self.support_status not in SUPPORT_STATUSES:
            raise ValueError(f"Unknown support status: {self.support_status}")
        return asdict(self)


@dataclass(frozen=True)
class LensBundle:
    """Six-lens bundle for council and memo consumption."""

    records: list[LensRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        by_lens = {record.lens: record.to_dict() for record in self.records}
        missing = [lens for lens in LENS_NAMES if lens not in by_lens]
        if missing:
            raise ValueError(f"Missing lens records: {', '.join(missing)}")
        return {lens: by_lens[lens] for lens in LENS_NAMES}


@dataclass(frozen=True)
class ClaimCluster:
    """Deterministic cluster of repeated or near-duplicate memo claims."""

    cluster_id: str
    normalized_subject: str
    claim_type: str
    representative_claim_id: str
    representative_claim: str
    member_claim_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    support_summary: dict[str, int] = field(default_factory=dict)
    cluster_support_status: str = "needs_manual_check"
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.cluster_support_status not in SUPPORT_STATUSES:
            raise ValueError(f"Unknown support status: {self.cluster_support_status}")
        return asdict(self)


@dataclass(frozen=True)
class AdapterCapability:
    """Availability descriptor for optional third-party adapter layers."""

    name: str
    available: bool
    reason: str
    package: str | None = None
    config_keys: list[str] = field(default_factory=list)
    fallback: str = "current SQLite/FTS plus web/mock path"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
