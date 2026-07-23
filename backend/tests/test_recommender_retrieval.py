from recommender import _rank_retrieval_lanes


def _row(name: str, *, similarity=None, rank: float = 0.05) -> dict:
    return {
        "id": name,
        "name": name,
        "description": f"{name} specialist guidance",
        "quality_status": "active",
        "quality_score": 90,
        "risk_score": 0,
        "content_hash": name,
        "rank": rank,
        "similarity": similarity,
    }


def test_pending_embedding_candidate_is_reserved_as_last_hint_lane() -> None:
    pending = _row("exact-lexical-pending", rank=1.0)
    semantic_a = _row("semantic-a", similarity=0.94, rank=0.06)
    semantic_b = _row("semantic-b", similarity=0.91, rank=0.05)

    ranked = _rank_retrieval_lanes(
        "exact lexical pending specialist guidance",
        [pending, semantic_a, semantic_b],
        3,
        vector_available=True,
    )

    assert [row["id"] for row in ranked[:2]] == ["semantic-a", "semantic-b"]
    assert ranked[-1]["id"] == "exact-lexical-pending"
    assert ranked[-1]["similarity"] is None


def test_pending_lane_cannot_displace_only_semantic_result_slot() -> None:
    ranked = _rank_retrieval_lanes(
        "exact lexical pending",
        [_row("pending", rank=1.0), _row("semantic", similarity=0.9, rank=0.05)],
        1,
        vector_available=True,
    )

    assert [row["id"] for row in ranked] == ["semantic"]


def test_fts_fallback_reranks_all_rows_when_embedding_is_unavailable() -> None:
    ranked = _rank_retrieval_lanes(
        "exact spreadsheet formula guidance",
        [
            _row("calendar", rank=0.2),
            {
                **_row("spreadsheet", rank=0.1),
                "description": "Exact spreadsheet formula guidance and charts.",
            },
        ],
        2,
        vector_available=False,
    )

    assert {row["id"] for row in ranked} == {"calendar", "spreadsheet"}
