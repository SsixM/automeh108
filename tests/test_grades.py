from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import bot


FIXTURES = Path(__file__).parent / "fixtures"


def read_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_seed(data_dir: Path) -> tuple[dict[str, dict], dict]:
    docs = {name: json.loads((data_dir / name).read_text(encoding="utf-8")) for name in bot.DATA_FILES}
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    return docs, manifest


def merge(grades, response, docs, reviews, review_ids, photo_hash="a" * 64):
    service = bot.GradeService()
    return service.merge_into(
        grades,
        response,
        "gemini-test",
        photo_hash,
        docs["students.json"],
        docs["schedule.json"],
        reviews,
        review_ids,
        recognised_at="2026-11-18T12:00:00+03:00",
    )


def test_extract_json_from_markdown_fence() -> None:
    text = (FIXTURES / "gemini-markdown.txt").read_text(encoding="utf-8")
    parsed = bot.GeminiClient.extract_json(text)
    assert parsed["document"]["group"] == "108М"
    assert parsed["cells"][0]["value"] == "5"


def test_validate_normal_and_reject_invalid_value() -> None:
    normal = bot.GeminiClient.validate_response(read_json("gemini-normal.json"))
    assert normal["document"]["group"] == "108М"
    with pytest.raises(ValueError):
        bot.GeminiClient.validate_response(read_json("gemini-invalid-value.json"))


def test_merge_adds_grades_and_accumulates_evidence(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    review_ids: set[str] = set()
    stats = merge(grades, read_json("gemini-normal.json"), docs, reviews, review_ids)
    assert stats.added == 3
    assert len(grades["grades"]) == 3
    assert {item["status"] for item in grades["grades"]} == {"automatic"}

    second_hash = "b" * 64
    stats = merge(grades, read_json("gemini-normal.json"), docs, reviews, review_ids, second_hash)
    assert stats.added == 0
    assert stats.same == 3
    assert len(grades["grades"]) == 3
    for grade in grades["grades"]:
        assert grade["evidence"] == ["a" * 64, second_hash]


def test_merge_low_confidence_is_pending(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    stats = merge(grades, read_json("gemini-low-confidence.json"), docs, reviews, set())
    assert stats.low == 1
    assert grades["grades"][0]["status"] == "pending"
    assert grades["grades"][0]["confidence"] == 0.5


def test_conflict_keeps_confirmed_value_and_creates_review(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    review_ids: set[str] = set()
    merge(grades, read_json("gemini-normal.json"), docs, reviews, review_ids)
    target = next(item for item in grades["grades"] if item["studentId"] == "s01")
    target["status"] = "confirmed"
    target["value"] = "4"

    conflicting = copy.deepcopy(read_json("gemini-normal.json"))
    conflicting["cells"][0]["value"] = "5"
    stats = merge(grades, conflicting, docs, reviews, review_ids, "c" * 64)
    assert stats.conflicts == 1
    updated = next(item for item in grades["grades"] if item["studentId"] == "s01")
    assert updated["value"] == "4"
    assert updated["status"] == "confirmed"
    assert len(reviews) == 1
    assert reviews[0]["oldValue"] == "4"
    assert reviews[0]["newValue"] == "5"


def test_handwritten_h_and_4_requires_review(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    stats = merge(grades, read_json("gemini-low-confidence.json"), docs, reviews, set())
    assert stats.low == 1
    assert grades["grades"][0]["status"] == "pending"
    # Значение 4 с raw "Н" не становится подтверждённым автоматически.
    assert "Н" in grades["grades"][0]["raw"]


def test_resolve_review_accept_and_reject(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    review_ids: set[str] = set()
    merge(grades, read_json("gemini-normal.json"), docs, reviews, review_ids)
    target = next(item for item in grades["grades"] if item["studentId"] == "s01")
    target["status"] = "confirmed"
    target["value"] = "4"
    conflicting = copy.deepcopy(read_json("gemini-normal.json"))
    conflicting["cells"][0]["value"] = "5"
    merge(grades, conflicting, docs, reviews, review_ids, "d" * 64)
    review = reviews[0]
    reviews_doc = {"items": [review]}

    accepted = bot.GradeService.resolve_review_in_docs(
        grades, reviews_doc, review["reviewId"], True, 1001, "2026-11-18T13:00:00+03:00"
    )
    assert accepted is True
    updated = next(item for item in grades["grades"] if item["studentId"] == "s01")
    assert updated["value"] == "5"
    assert updated["status"] == "confirmed"
    assert reviews_doc["items"][0]["status"] == "confirmed"

    repeated = bot.GradeService.resolve_review_in_docs(
        grades, reviews_doc, review["reviewId"], True, 1001
    )
    assert repeated is False


def test_incomplete_journal_does_not_delete_existing_grades(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    grades = {"grades": []}
    reviews: list[dict] = []
    review_ids: set[str] = set()
    merge(grades, read_json("gemini-normal.json"), docs, reviews, review_ids)
    assert len(grades["grades"]) == 3

    partial = copy.deepcopy(read_json("gemini-normal.json"))
    partial["cells"] = partial["cells"][:1]
    stats = merge(grades, partial, docs, reviews, review_ids, "f" * 64)
    assert stats.added == 0
    assert len(grades["grades"]) == 3
    values = {item["studentId"]: item["value"] for item in grades["grades"]}
    assert values["s02"] == "4"
    assert values["s03"] == "н/б"
