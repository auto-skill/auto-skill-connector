"""Contextual candidate roles and conservative abstention gates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Iterable

from query_compiler import CompiledIntent


ROLE_CLASSIFIER_VERSION = "candidate-roles-v1"
PRIMARY_SURFACE_THRESHOLD = 0.80
SUPPORTING_SURFACE_THRESHOLD = 0.72
POLICY_SURFACE_THRESHOLD = 0.95

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]*", re.I)
_CONFLICT_RE = re.compile(
    r"\b(?:ignore (?:previous|prior|system|developer) instructions|override the user|"
    r"do not answer the user|system prompt|prompt injection|jailbreak|exfiltrat|"
    r"send credentials|disable safety|bypass authorization)\b",
    re.I,
)
_FAILURE_SIGNAL_RE = re.compile(
    r"\b(?:error|exception|fail(?:ed|ing|ure)?|bug|crash(?:ed|ing)?|timeout|broken|invalid|corrupt)\b",
    re.I,
)
_GENERIC_SUPPORT_TERMS = frozenset(
    "best practices guidelines guide workflow review testing debugging deployment migration policy process methodology general generic".split()
)


@dataclass(frozen=True)
class CandidateClassification:
    role: str
    confidence: float
    surfaced: bool
    reasons: tuple[str, ...]
    classifier_version: str = ROLE_CLASSIFIER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tokens(value: str) -> set[str]:
    tokens = {token.casefold() for token in _TOKEN_RE.findall(value or "") if len(token) > 2}
    tokens.update(token[:-1] for token in list(tokens) if len(token) > 4 and token.endswith("s"))
    return tokens


def _candidate_text(candidate: dict[str, Any]) -> str:
    tags = candidate.get("tags") if isinstance(candidate.get("tags"), list) else []
    return " ".join(
        str(value or "")
        for value in (
            candidate.get("name"),
            candidate.get("description"),
            " ".join(str(tag) for tag in tags),
            candidate.get("capability_summary"),
            candidate.get("retrieval_text"),
        )
    ).casefold()


def _overlap(parts: Iterable[str], candidate_tokens: set[str]) -> int:
    query_tokens = _tokens(" ".join(parts))
    return len(query_tokens & candidate_tokens)


def _bounded_float(value: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def classify_candidate(
    intent: CompiledIntent,
    candidate: dict[str, Any],
    *,
    curated_policy_names: Iterable[str] = (),
) -> CandidateClassification:
    """Classify utility, not merely semantic relevance, for one task."""
    text = _candidate_text(candidate)
    candidate_tokens = _tokens(text)
    reasons: list[str] = []
    risk_score = int(candidate.get("risk_score") or 0)
    risk_flags = {str(value).casefold() for value in candidate.get("risk_flags") or []}
    name = str(candidate.get("name") or "").strip().casefold()

    if risk_score >= 3 or _CONFLICT_RE.search(text) or risk_flags & {
        "prompt-injection", "credential-access", "destructive", "malware"
    }:
        return CandidateClassification(
            role="harmful/conflicting",
            confidence=1.0,
            surfaced=False,
            reasons=("safety-conflict",),
        )

    policy_names = {str(value).strip().casefold() for value in curated_policy_names}
    is_policy = name in policy_names or str(candidate.get("category") or "").casefold() == "policy"
    if is_policy:
        trusted = risk_score == 0 and bool(candidate.get("content_hash"))
        confidence = 0.98 if trusted else 0.60
        return CandidateClassification(
            role="policy",
            confidence=confidence,
            surfaced=trusted and confidence >= POLICY_SURFACE_THRESHOLD,
            reasons=("curated-policy" if trusted else "unverified-policy",),
        )

    technology_overlap = _overlap(intent.technology, candidate_tokens)
    operation_overlap = _overlap(intent.operation, candidate_tokens)
    artifact_overlap = _overlap(intent.artifact, candidate_tokens)
    failure_overlap = int(bool(intent.failure_mode and _FAILURE_SIGNAL_RE.search(text)))
    constraint_overlap = _overlap(intent.constraints, candidate_tokens)
    direct_overlap = len(_tokens(intent.compressed_query) & candidate_tokens)
    if technology_overlap:
        reasons.append("technology-match")
    if operation_overlap:
        reasons.append("operation-match")
    if artifact_overlap:
        reasons.append("artifact-match")
    if failure_overlap:
        reasons.append("failure-mode-match")

    similarity = candidate.get("similarity")
    similarity_component = 0.0
    if similarity is not None:
        similarity_component = max(0.0, min(1.0, (float(similarity) - 0.72) / 0.28))
    lexical_component = min(1.0, direct_overlap / 5.0)
    quality_component = _bounded_float((candidate.get("quality_score") or 0) / 100.0)
    provenance_component = _bounded_float(candidate.get("provenance_score"), 0.25)
    specificity_component = min(
        1.0,
        technology_overlap * 0.35
        + operation_overlap * 0.30
        + artifact_overlap * 0.30
        + failure_overlap * 0.20
        + constraint_overlap * 0.10,
    )
    confidence = (
        similarity_component * 0.32
        + lexical_component * 0.18
        + specificity_component * 0.32
        + quality_component * 0.10
        + provenance_component * 0.08
    )
    generic_share = (
        len(candidate_tokens & _GENERIC_SUPPORT_TERMS) / max(1, len(candidate_tokens))
    )
    has_primary_contract = bool(
        (operation_overlap and (artifact_overlap or technology_overlap))
        or (failure_overlap and technology_overlap)
    )
    if has_primary_contract:
        confidence += 0.23
    confidence = round(max(0.0, min(1.0, confidence)), 6)
    if has_primary_contract and confidence >= PRIMARY_SURFACE_THRESHOLD:
        return CandidateClassification("primary", confidence, True, tuple(reasons or ["specific-match"]))
    if (
        direct_overlap >= 2
        and (generic_share > 0 or operation_overlap or technology_overlap or artifact_overlap)
        and confidence >= SUPPORTING_SURFACE_THRESHOLD
    ):
        return CandidateClassification("supporting", confidence, True, tuple(reasons or ["adjacent-match"]))
    return CandidateClassification(
        "irrelevant",
        confidence,
        False,
        tuple(reasons or ["insufficient-task-contract"]),
    )


def partition_candidates(
    intent: CompiledIntent,
    candidates: list[dict[str, Any]],
    *,
    curated_policy_names: Iterable[str] = (),
    supporting_limit: int = 2,
) -> dict[str, Any]:
    """Select one primary independently; supporting content never takes its slot."""
    annotated: list[dict[str, Any]] = []
    for rank, candidate in enumerate(candidates):
        classification = classify_candidate(
            intent,
            candidate,
            curated_policy_names=curated_policy_names,
        )
        item = dict(candidate)
        item["candidate_role"] = classification.role
        item["role_confidence"] = classification.confidence
        item["role_reasons"] = list(classification.reasons)
        item["retrieval_rank"] = rank
        annotated.append(item)
    primary_items = [item for item in annotated if item["candidate_role"] == "primary"]

    # Role confidence is a gate, not a replacement for retrieval quality.  A
    # lower-ranked candidate can contain more task vocabulary and therefore
    # receive a slightly higher role score even when the hybrid retriever found
    # a materially better primary match.  Preserve the retrieval order first,
    # then use role confidence and semantic similarity only as deterministic
    # tie-breakers.  This prevents the classifier from silently undoing the
    # evidence-backed retrieval improvement.
    primary_items.sort(
        key=lambda item: (
            -float(item.get("route_score") or 0.0),
            -float(item.get("similarity") or 0.0),
            -float(item["role_confidence"]),
            int(item["retrieval_rank"]),
        )
    )
    supporting = [item for item in annotated if item["candidate_role"] == "supporting"]
    supporting.sort(key=lambda item: (-float(item["role_confidence"]), int(item["retrieval_rank"])))
    return {
        "primary": primary_items[0] if primary_items else None,
        "primary_candidates": primary_items,
        "supporting": supporting[: max(0, supporting_limit)],
        "annotated": annotated,
        "harmful_count": sum(item["candidate_role"] == "harmful/conflicting" for item in annotated),
        "abstained": not primary_items,
        "thresholds": {
            "primary": PRIMARY_SURFACE_THRESHOLD,
            "supporting": SUPPORTING_SURFACE_THRESHOLD,
            "policy": POLICY_SURFACE_THRESHOLD,
        },
    }
