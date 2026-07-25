"""Evidence packet normalization and citation matching."""
from __future__ import annotations

import hashlib
from typing import Any

from .contracts import EvidencePacket


def normalize_evidence_packets(
    search_results: list[dict[str, Any]],
    retrieval_query: str = "",
    retrieval_reason: str = "thesis search",
) -> list[dict[str, Any]]:
    """Normalize existing search/workspace results into EvidencePacket dicts."""
    packets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, result in enumerate(search_results, start=1):
        packet = _packet_from_result(result, rank, retrieval_query, retrieval_reason)
        packet_dict = packet.to_dict()
        evidence_id = packet_dict["evidence_id"]
        if evidence_id in seen:
            evidence_id = f"{evidence_id}_{rank}"
            packet_dict["evidence_id"] = evidence_id
        seen.add(evidence_id)
        packets.append(packet_dict)
    return packets


def resolve_citation_evidence_ids(citations: list[Any], evidence_packets: list[dict[str, Any]]) -> list[str]:
    """Resolve memo citations to normalized evidence IDs."""
    resolved: list[str] = []
    for citation in citations or []:
        for packet in evidence_packets:
            if citation_matches_packet(citation, packet):
                evidence_id = packet["evidence_id"]
                if evidence_id not in resolved:
                    resolved.append(evidence_id)
    return resolved


def citation_matches_packet(citation: Any, packet: dict[str, Any]) -> bool:
    """Return True when a citation points to a normalized evidence packet."""
    locator = packet.get("locator", {})
    source_uri = str(packet.get("source_uri") or "")
    source_path = str(locator.get("source_path") or "")
    chunk_id = str(locator.get("chunk_id") or "")

    if isinstance(citation, dict):
        citation_uri = str(citation.get("url") or citation.get("source_uri") or "")
        citation_path = str(citation.get("source_path") or "")
        citation_chunk = str(citation.get("chunk_id") or "")
        if citation_path and citation_chunk:
            return citation_path == source_path and citation_chunk == chunk_id
        if citation_uri:
            return citation_uri == source_uri
        return False

    if not isinstance(citation, str):
        return False

    citation = citation.strip()
    if not citation:
        return False
    if citation.startswith("http://") or citation.startswith("https://") or citation.startswith("local://"):
        return citation == source_uri
    return False


def evidence_packet_by_id(evidence_packets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {packet["evidence_id"]: packet for packet in evidence_packets}


def _packet_from_result(
    result: dict[str, Any],
    rank: int,
    retrieval_query: str,
    retrieval_reason: str,
) -> EvidencePacket:
    source_uri = str(result.get("url") or result.get("source_uri") or result.get("source_path") or f"result:{rank}")
    source_path = str(result.get("source_path") or "")
    chunk_id = str(result.get("chunk_id") or "")
    title = str(result.get("title") or result.get("section") or source_path or f"Evidence {rank}")
    excerpt = _compact_excerpt(result.get("snippet") or result.get("excerpt") or result.get("text") or "")
    source_type = _source_type(result)
    evidence_class = _evidence_class(result, source_type)
    evidence_id = _stable_evidence_id(source_uri, source_path, chunk_id, excerpt, rank)
    locator = {
        "url": source_uri,
        "source_path": source_path,
        "section": str(result.get("section") or ""),
        "chunk_id": chunk_id,
        "rank": rank,
    }
    pageindex_doc_id = str(result.get("pageindex_doc_id") or source_path or "") or None
    pageindex_chunk_id = str(result.get("pageindex_chunk_id") or chunk_id or "") or None

    return EvidencePacket(
        evidence_id=evidence_id,
        source_uri=source_uri,
        title=title,
        excerpt=excerpt,
        source_type=source_type,
        evidence_class=evidence_class,
        locator=locator,
        retrieval_reason=str(result.get("retrieval_reason") or retrieval_reason),
        confidence=_confidence_for_result(result, source_type),
        query_metadata={
            "query": retrieval_query,
            "rank": rank,
            "source_query": result.get("query") or result.get("source_query"),
        },
        source_metadata={
            "source": result.get("source") or source_type,
            "raw_title": result.get("title"),
            "source_path": source_path,
            "evidence_class": evidence_class,
            "provider": result.get("provider"),
            "import_source_type": result.get("import_source_type"),
            "regulator": result.get("regulator"),
            "row": result.get("row"),
        },
        pageindex_doc_id=pageindex_doc_id,
        pageindex_chunk_id=pageindex_chunk_id,
    )


def _source_type(result: dict[str, Any]) -> str:
    source = str(result.get("source") or "").lower()
    if source == "workspace":
        return "workspace"
    if source in {"exa", "firecrawl"}:
        return "web"
    if source == "mock":
        return "mock_web"
    if source in {
        "private_document",
        "imported_market_database",
        "public_web",
        "regulator_ground_truth",
        "manual_fund_memory",
        "external_ai_memo",
    }:
        return source
    return source or "unknown"


def _evidence_class(result: dict[str, Any], source_type: str) -> str:
    explicit = str(result.get("evidence_class") or "").strip()
    if explicit:
        return explicit
    if source_type == "workspace":
        return "private_document"
    if source_type in {"web", "mock_web"}:
        return "public_web"
    if source_type in {
        "private_document",
        "imported_market_database",
        "public_web",
        "regulator_ground_truth",
        "manual_fund_memory",
        "external_ai_memo",
    }:
        return source_type
    return "public_web"


def _confidence_for_result(result: dict[str, Any], source_type: str) -> float:
    if result.get("confidence") is not None:
        try:
            return max(0.0, min(1.0, float(result["confidence"])))
        except (TypeError, ValueError):
            pass
    if source_type == "workspace":
        return 0.85
    if source_type == "web":
        return 0.70
    if source_type == "mock_web":
        return 0.65
    return 0.50


def _stable_evidence_id(source_uri: str, source_path: str, chunk_id: str, excerpt: str, rank: int) -> str:
    seed = "|".join([source_uri, source_path, chunk_id, excerpt[:180], str(rank)])
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"ev_{digest}"


def _compact_excerpt(text: Any, limit: int = 1400) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."
