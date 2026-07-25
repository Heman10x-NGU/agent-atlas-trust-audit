"""Claim extraction and deterministic claim-to-evidence support judgment."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .contracts import ClaimRecord, SupportAssessment
from .evidence import evidence_packet_by_id, resolve_citation_evidence_ids


STATUS_WEIGHTS = {
    "verified_by_regulator": 1.0,
    "verified": 0.92,
    "supported": 1.0,
    "weak_support": 0.55,
    "weak": 0.55,
    "needs_manual_check": 0.25,
    "regulator_unavailable": 0.15,
    "data_stale": 0.20,
    "unsupported": 0.0,
    "contradicted": 0.0,
    "contradicted_by_regulator": 0.0,
    "contradicted_by_imported_database": 0.0,
}

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "based", "be", "by", "for", "from",
    "has", "have", "in", "into", "is", "it", "of", "on", "or", "stage", "the",
    "their", "this", "to", "with", "within",
}


def extract_claims_from_memo(
    memo: dict[str, Any],
    evidence_packets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Extract deterministic company and market claims from the current memo schema."""
    claims: list[dict[str, Any]] = []
    claim_index = 1

    for company in memo.get("companies", []) or []:
        subject = str(company.get("name") or "Unknown company").strip() or "Unknown company"
        cited_ids = resolve_citation_evidence_ids(company.get("citations", []), evidence_packets)
        claim_index = _append_claim(
            claims,
            claim_index,
            text=str(company.get("why_matches_thesis") or ""),
            subject=subject,
            claim_type="thesis_fit",
            memo_section="companies.why_matches_thesis",
            cited_evidence_ids=cited_ids,
            metadata={"company": subject},
        )
        funding_stage = str(company.get("funding_stage") or "").strip()
        if funding_stage:
            claim_index = _append_claim(
                claims,
                claim_index,
                text=f"{subject} funding stage is {funding_stage}.",
                subject=subject,
                claim_type="funding_stage",
                memo_section="companies.funding_stage",
                cited_evidence_ids=cited_ids,
                metadata={"company": subject, "raw_value": funding_stage},
            )
        traction = str(company.get("traction_signal") or "").strip()
        if traction:
            claim_index = _append_claim(
                claims,
                claim_index,
                text=f"{subject} traction signal: {traction}",
                subject=subject,
                claim_type="traction",
                memo_section="companies.traction_signal",
                cited_evidence_ids=cited_ids,
                metadata={"company": subject, "raw_value": traction},
            )
        risk = str(company.get("risk") or "").strip()
        if risk:
            claim_index = _append_claim(
                claims,
                claim_index,
                text=f"{subject} risk: {risk}",
                subject=subject,
                claim_type="risk",
                memo_section="companies.risk",
                cited_evidence_ids=cited_ids,
                metadata={"company": subject, "raw_value": risk},
            )

    for theme in memo.get("market_themes", []) or []:
        claim_index = _append_claim(
            claims,
            claim_index,
            text=str(theme),
            subject="Market",
            claim_type="market_theme",
            memo_section="market_themes",
            cited_evidence_ids=[],
            metadata={},
        )

    return claims


def assess_claim_support(
    claims: list[dict[str, Any]],
    evidence_packets: list[dict[str, Any]],
    trust_mode: bool = False,
) -> list[dict[str, Any]]:
    """Assess every claim against available evidence packets."""
    packets_by_id = evidence_packet_by_id(evidence_packets)
    return [_assess_claim(claim, evidence_packets, packets_by_id, trust_mode=trust_mode).to_dict() for claim in claims]


def apply_support_to_memo(
    memo: dict[str, Any],
    claims: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach aggregate claim support labels back onto company findings."""
    assessment_by_claim = {a["claim_id"]: a for a in assessments}
    claim_status_by_company: dict[str, list[str]] = defaultdict(list)
    claim_ids_by_company: dict[str, list[str]] = defaultdict(list)

    for claim in claims:
        company = claim.get("metadata", {}).get("company")
        if not company:
            continue
        assessment = assessment_by_claim.get(claim["claim_id"])
        if not assessment:
            continue
        claim_status_by_company[company].append(assessment["status"])
        claim_ids_by_company[company].append(claim["claim_id"])

    for company in memo.get("companies", []) or []:
        name = str(company.get("name") or "")
        statuses = claim_status_by_company.get(name, [])
        if not statuses:
            continue
        status_counts = Counter(statuses)
        company["support_status"] = _company_status(statuses)
        company["claim_support_distribution"] = dict(status_counts)
        company["claim_ids"] = claim_ids_by_company.get(name, [])
        company["evidence_score"] = round(sum(STATUS_WEIGHTS.get(s, 0.0) for s in statuses) / len(statuses), 4)
    return memo


def claim_quality_score(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute final quality from claim-level support labels."""
    if not assessments:
        return {
            "overall_score": 0.0,
            "support_distribution": {},
            "unsupported_ratio": 1.0,
            "contradiction_count": 0,
            "hard_stop_count": 0,
        }

    counts = Counter(a.get("status", "unsupported") for a in assessments)
    weighted = sum(STATUS_WEIGHTS.get(a.get("status", "unsupported"), 0.0) for a in assessments)
    total = len(assessments)
    bad = (
        counts.get("unsupported", 0)
        + counts.get("needs_manual_check", 0)
        + counts.get("regulator_unavailable", 0)
        + counts.get("data_stale", 0)
    )
    hard_stop_count = counts.get("contradicted_by_regulator", 0) + counts.get("contradicted_by_imported_database", 0)
    contradiction_count = counts.get("contradicted", 0) + hard_stop_count
    return {
        "overall_score": round(weighted / total, 4),
        "support_distribution": dict(counts),
        "unsupported_ratio": round(bad / total, 4),
        "contradiction_count": contradiction_count,
        "hard_stop_count": hard_stop_count,
    }


def _append_claim(
    claims: list[dict[str, Any]],
    claim_index: int,
    text: str,
    subject: str,
    claim_type: str,
    memo_section: str,
    cited_evidence_ids: list[str],
    metadata: dict[str, Any],
) -> int:
    clean_text = " ".join(str(text or "").split())
    if not clean_text:
        return claim_index
    claim = ClaimRecord(
        claim_id=f"claim_{claim_index:03d}",
        text=clean_text,
        subject=subject,
        claim_type=claim_type,
        memo_section=memo_section,
        cited_evidence_ids=cited_evidence_ids,
        metadata=metadata,
    )
    claims.append(claim.to_dict())
    return claim_index + 1


def _assess_claim(
    claim: dict[str, Any],
    evidence_packets: list[dict[str, Any]],
    packets_by_id: dict[str, dict[str, Any]],
    trust_mode: bool = False,
) -> SupportAssessment:
    text = claim.get("text", "")
    claim_id = claim.get("claim_id", "")
    claim_type = claim.get("claim_type", "")
    cited_ids = [eid for eid in claim.get("cited_evidence_ids", []) if eid in packets_by_id]
    direct_packets = [packets_by_id[eid] for eid in cited_ids]
    candidate_packets = direct_packets or _best_candidate_packets(claim, evidence_packets)

    if _explicit_manual_check(text):
        return SupportAssessment(
            claim_id=claim_id,
            status="needs_manual_check",
            reasoning="The claim itself is explicitly marked for manual checking.",
            best_evidence_ids=cited_ids[:3],
            missing_evidence=[_missing_for_claim_type(claim_type)],
        )

    contradiction = _find_contradiction(claim, candidate_packets)
    if contradiction:
        return SupportAssessment(
            claim_id=claim_id,
            status=_contradiction_status(contradiction.get("evidence_class", ""), trust_mode),
            reasoning=contradiction["reasoning"],
            best_evidence_ids=[contradiction["evidence_id"]],
            missing_evidence=[],
        )

    if not candidate_packets:
        return SupportAssessment(
            claim_id=claim_id,
            status="unsupported",
            reasoning="No retrieved evidence mentions the claim subject or key terms.",
            best_evidence_ids=[],
            missing_evidence=[_missing_for_claim_type(claim_type)],
        )

    best_packet, best_overlap = _best_supporting_packet(text, candidate_packets)
    best_ids = [best_packet["evidence_id"]] if best_packet else cited_ids[:1]
    has_direct_citation = bool(cited_ids)

    if has_direct_citation and _strongly_supported(text, best_packet, best_overlap):
        return SupportAssessment(
            claim_id=claim_id,
            status=_verified_status(best_packet, trust_mode),
            reasoning="A cited evidence packet directly supports the claim.",
            best_evidence_ids=best_ids,
            missing_evidence=[],
        )

    if has_direct_citation:
        return SupportAssessment(
            claim_id=claim_id,
            status="weak_support" if trust_mode else "weak",
            reasoning="A cited evidence packet exists, but only partially supports the claim.",
            best_evidence_ids=best_ids,
            missing_evidence=[_missing_for_claim_type(claim_type)],
        )

    if _strongly_supported(text, best_packet, best_overlap):
        if trust_mode:
            return SupportAssessment(
                claim_id=claim_id,
                status=_verified_status(best_packet, trust_mode),
                reasoning="Retrieved trust-layer evidence directly supports the claim.",
                best_evidence_ids=best_ids,
                missing_evidence=[],
            )
        return SupportAssessment(
            claim_id=claim_id,
            status="weak_support" if trust_mode else "weak",
            reasoning="Relevant retrieved evidence exists, but the memo did not cite it directly.",
            best_evidence_ids=best_ids,
            missing_evidence=["Direct citation from memo claim to evidence packet."],
        )

    return SupportAssessment(
        claim_id=claim_id,
        status="unsupported",
        reasoning="Retrieved evidence is too weak or generic for this claim.",
        best_evidence_ids=best_ids,
        missing_evidence=[_missing_for_claim_type(claim_type)],
    )


def _best_candidate_packets(claim: dict[str, Any], evidence_packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    subject_tokens = _tokens(claim.get("subject", ""))
    claim_tokens = _tokens(claim.get("text", ""))
    ranked: list[tuple[float, dict[str, Any]]] = []
    for packet in evidence_packets:
        packet_text = f"{packet.get('title', '')} {packet.get('excerpt', '')}"
        packet_tokens = _tokens(packet_text)
        if not packet_tokens:
            continue
        subject_hit = bool(subject_tokens & packet_tokens) or claim.get("subject") == "Market"
        overlap = len(claim_tokens & packet_tokens) / max(1, len(claim_tokens))
        if subject_hit or overlap >= 0.18:
            ranked.append((overlap + (0.25 if subject_hit else 0), packet))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [packet for _, packet in ranked[:4]]


def _best_overlap(text: str, packets: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    claim_tokens = _tokens(text)
    best_packet = None
    best_score = 0.0
    for packet in packets:
        packet_tokens = _tokens(f"{packet.get('title', '')} {packet.get('excerpt', '')}")
        score = len(claim_tokens & packet_tokens) / max(1, len(claim_tokens))
        if score > best_score:
            best_packet = packet
            best_score = score
    return best_packet, best_score


def _best_supporting_packet(text: str, packets: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    best_packet, best_overlap = _best_overlap(text, packets)
    if _strongly_supported(text, best_packet, best_overlap):
        return best_packet, best_overlap
    for packet in packets:
        packet_tokens = _tokens(f"{packet.get('title', '')} {packet.get('excerpt', '')}")
        claim_tokens = _tokens(text)
        overlap = len(claim_tokens & packet_tokens) / max(1, len(claim_tokens))
        if _strongly_supported(text, packet, overlap):
            return packet, overlap
    return best_packet, best_overlap


def _strongly_supported(text: str, packet: dict[str, Any] | None, overlap: float) -> bool:
    if not packet:
        return False
    evidence_text = f"{packet.get('title', '')} {packet.get('excerpt', '')}".lower()
    text_lower = text.lower()
    claim_metrics = _metrics(text_lower)
    evidence_metrics = _metrics(evidence_text)
    has_metric = bool(claim_metrics & evidence_metrics)
    has_stage = bool(_stage_terms(text_lower) & _stage_terms(evidence_text))
    if _requires_metric_match(text_lower) and claim_metrics and not has_metric:
        return False
    return overlap >= 0.28 or has_metric or has_stage


def _find_contradiction(claim: dict[str, Any], packets: list[dict[str, Any]]) -> dict[str, str] | None:
    text = str(claim.get("text", ""))
    claim_type = str(claim.get("claim_type", ""))
    text_lower = text.lower()
    claim_stages = _stage_terms(text_lower)
    for packet in packets:
        evidence_text = f"{packet.get('title', '')} {packet.get('excerpt', '')}".lower()
        evidence_class = packet.get("evidence_class") or packet.get("source_metadata", {}).get("evidence_class") or ""
        evidence_stages = _stage_terms(evidence_text)
        if claim_stages and evidence_stages and claim_stages.isdisjoint(evidence_stages):
            return {
                "evidence_id": packet["evidence_id"],
                "evidence_class": evidence_class,
                "reasoning": "Funding stage in evidence conflicts with the memo claim.",
            }
        numeric_conflict = _numeric_contradiction(text_lower, evidence_text, claim_type)
        if numeric_conflict:
            return {
                "evidence_id": packet["evidence_id"],
                "evidence_class": evidence_class,
                "reasoning": numeric_conflict,
            }
        categorical_conflict = _categorical_contradiction(text_lower, evidence_text, claim_type)
        if categorical_conflict:
            return {
                "evidence_id": packet["evidence_id"],
                "evidence_class": evidence_class,
                "reasoning": categorical_conflict,
            }
        if "indian" in text_lower and re.search(r"\b(not indian|us hq|california|european hq|not india)\b", evidence_text):
            return {
                "evidence_id": packet["evidence_id"],
                "evidence_class": evidence_class,
                "reasoning": "Geography in evidence conflicts with the memo claim.",
            }
        if "llm" in text_lower and re.search(r"\bnot llm|not llm-specific|not ai-specific\b", evidence_text):
            return {
                "evidence_id": packet["evidence_id"],
                "evidence_class": evidence_class,
                "reasoning": "Category fit in evidence conflicts with the memo claim.",
            }
    return None


def _company_status(statuses: list[str]) -> str:
    counts = Counter(statuses)
    if counts.get("contradicted_by_regulator"):
        return "contradicted_by_regulator"
    if counts.get("contradicted_by_imported_database"):
        return "contradicted_by_imported_database"
    if counts.get("contradicted"):
        return "contradicted"
    hard_fail = counts.get("unsupported", 0) + counts.get("needs_manual_check", 0)
    if hard_fail == len(statuses):
        return "unsupported" if counts.get("unsupported", 0) >= counts.get("needs_manual_check", 0) else "needs_manual_check"
    if hard_fail / len(statuses) > 0.5:
        return "needs_manual_check"
    if counts.get("weak_support") and not counts.get("verified"):
        return "weak_support"
    if counts.get("weak") and not counts.get("supported"):
        return "weak"
    if counts.get("verified") or counts.get("verified_by_regulator"):
        return "verified_by_regulator" if counts.get("verified_by_regulator") else "verified"
    return "supported"


def _explicit_manual_check(text: str) -> bool:
    lower = text.lower()
    return "needs_manual_check" in lower or "manual verification" in lower or "manual check" in lower


def _missing_for_claim_type(claim_type: str) -> str:
    labels = {
        "thesis_fit": "Direct evidence that the company matches the thesis filters.",
        "funding_stage": "Authoritative funding stage or financing source.",
        "funding": "Authoritative funding amount, date, round, or investor source.",
        "revenue": "Audited financials, export row, or company document supporting revenue.",
        "employee_count": "Credible employee count export or primary headcount evidence.",
        "incorporation_year": "MCA/company registry incorporation date or year.",
        "signatory_director": "MCA/director/signatory record or board document.",
        "dpiit_startup_recognition": "DPIIT/startup recognition certificate or registry row.",
        "charges": "MCA charge register, loan document, or charges export.",
        "traction": "Primary traction metric, customer evidence, or credible secondary source.",
        "risk": "Evidence-backed risk, not just generic analyst opinion.",
        "market_theme": "Multiple sources or cited examples supporting the market theme.",
    }
    return labels.get(claim_type, "Direct supporting source.")


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9+.-]*", str(text).lower())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _metrics(text: str) -> set[str]:
    metrics = set()
    for match in re.finditer(r"\$?(\d+(?:\.\d+)?)\s*(k|m|b|%|\+|customers|employees|stars|users)?", text.lower()):
        number, unit = match.groups()
        if not number:
            continue
        normalized = number.rstrip("0").rstrip(".") if "." in number else number
        metrics.add(normalized)
        if unit:
            metrics.add(f"{normalized}:{unit}")
    return metrics


def _requires_metric_match(text: str) -> bool:
    metric_sensitive_terms = (
        "revenue",
        "arr",
        "employee",
        "employees",
        "headcount",
        "customers",
        "users",
        "stars",
        "traction",
    )
    return any(term in text for term in metric_sensitive_terms)


def _stage_terms(text: str) -> set[str]:
    stages: list[tuple[str, str]] = [
        ("pre-seed", "pre-seed"),
        ("pre seed", "pre-seed"),
        ("series a", "series a"),
        ("series b", "series b"),
        ("series c", "series c"),
        ("seed", "seed"),
        ("growth", "growth"),
        ("ipo", "ipo"),
    ]
    found: set[str] = set()
    masked = text
    for phrase, label in stages:
        if phrase in masked:
            found.add(label)
            masked = masked.replace(phrase, " ")
    return found


def _verified_status(packet: dict[str, Any] | None, trust_mode: bool) -> str:
    if not trust_mode:
        return "supported"
    evidence_class = (packet or {}).get("evidence_class") or (packet or {}).get("source_metadata", {}).get("evidence_class")
    if evidence_class == "regulator_ground_truth":
        return "verified_by_regulator"
    return "verified"


def _contradiction_status(evidence_class: str, trust_mode: bool) -> str:
    if not trust_mode:
        return "contradicted"
    if evidence_class == "regulator_ground_truth":
        return "contradicted_by_regulator"
    if evidence_class == "imported_market_database":
        return "contradicted_by_imported_database"
    return "contradicted"


def _numeric_contradiction(claim_text: str, evidence_text: str, claim_type: str) -> str | None:
    if claim_type in {"employee_count", "headcount"}:
        claim_values = _nearby_numbers(claim_text, ("employee", "employees", "headcount", "team"))
        evidence_values = _nearby_numbers(evidence_text, ("employee", "employees", "headcount", "team"))
        if claim_values and evidence_values and claim_values.isdisjoint(evidence_values):
            return "Employee/headcount value in evidence conflicts with the memo claim."
    if claim_type in {"incorporation_year", "incorporation_date"}:
        claim_years = _years(claim_text)
        evidence_years = _years(evidence_text)
        if claim_years and evidence_years and claim_years.isdisjoint(evidence_years):
            return "Incorporation year in evidence conflicts with the memo claim."
    if claim_type in {"revenue", "funding", "charges"}:
        claim_amounts = _amounts(claim_text)
        evidence_amounts = _amounts(evidence_text)
        if claim_amounts and evidence_amounts and claim_amounts.isdisjoint(evidence_amounts):
            label = claim_type.replace("_", " ")
            return f"{label.title()} amount in evidence conflicts with the memo claim."
    return None


def _categorical_contradiction(claim_text: str, evidence_text: str, claim_type: str) -> str | None:
    if claim_type == "dpiit_startup_recognition":
        claim_positive = bool(re.search(r"\b(dpiit|startup recognition|recognized|recognised)\b", claim_text))
        evidence_negative = bool(re.search(r"\b(not recognized|not recognised|not dpiit|no dpiit|unrecognized|unrecognised)\b", evidence_text))
        if claim_positive and evidence_negative:
            return "DPIIT/startup recognition evidence conflicts with the memo claim."
    if claim_type == "charges":
        claim_no_charge = bool(re.search(r"\b(no charges|zero charges|no outstanding charge|no outstanding charges)\b", claim_text))
        evidence_charge = bool(re.search(r"\b(charge|charges|hypothecation|outstanding)\b", evidence_text)) and bool(_amounts(evidence_text) or re.search(r"\b(open|active|outstanding)\b", evidence_text))
        if claim_no_charge and evidence_charge:
            return "Charge register evidence conflicts with the memo claim."
    if claim_type == "signatory_director":
        claim_names = _person_names_after_terms(claim_text)
        evidence_names = _person_names_after_terms(evidence_text)
        if claim_names and evidence_names and claim_names.isdisjoint(evidence_names):
            return "Director/signatory name in evidence conflicts with the memo claim."
    return None


def _nearby_numbers(text: str, terms: tuple[str, ...]) -> set[str]:
    values = set()
    for term in terms:
        for match in re.finditer(rf"(?:\b(\d{{1,7}})\s+{re.escape(term)}\b|\b{re.escape(term)}\D{{0,20}}(\d{{1,7}})\b)", text):
            value = match.group(1) or match.group(2)
            if value:
                values.add(value.lstrip("0") or "0")
    return values


def _years(text: str) -> set[str]:
    return set(re.findall(r"\b(?:19|20)\d{2}\b", text))


def _amounts(text: str) -> set[str]:
    amounts = set()
    for match in re.finditer(r"(rs\.?|inr|₹|\$)?\s*(\d+(?:\.\d+)?)\s*(crore|cr|lakh|lac|m|million|b|billion|k)?", text, flags=re.I):
        currency, number, unit = match.groups()
        if not number:
            continue
        if not currency and not unit:
            continue
        token = f"{number.lower().rstrip('0').rstrip('.') if '.' in number else number.lower()}:{(unit or '').lower()}"
        amounts.add(token)
    return amounts


def _person_names_after_terms(text: str) -> set[str]:
    names = set()
    for match in re.finditer(r"\b(?:director|signatory|authorised signatory|authorized signatory)\b[:\s-]+([a-z][a-z ]{2,40})", text, flags=re.I):
        name = re.sub(r"\s+", " ", match.group(1)).strip(" .;,")
        if name:
            names.add(name.lower())
    return names
