from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import bot


def load_seed(data_dir: Path) -> dict[str, dict]:
    return {
        name: json.loads((data_dir / name).read_text(encoding="utf-8"))
        for name in bot.DATA_FILES
    }


def seed_manifest(data_dir: Path) -> dict:
    return json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))


def test_seed_bundle_is_valid(data_dir: Path) -> None:
    bot.validate_bundle(load_seed(data_dir), seed_manifest(data_dir))


def test_grade_rejects_impossible_date(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    grade = {
        "gradeId": "grade-bad-date-01",
        "studentId": "s01",
        "subjectId": "math",
        "subject": "Математика",
        "lessonDate": "2026-13-01",
        "entryType": "daily",
        "value": "5",
        "raw": "5",
        "confidence": 0.9,
        "source": "gemini",
        "status": "automatic",
        "evidence": ["a" * 64],
    }
    documents["grades.json"]["grades"].append(grade)
    with pytest.raises(bot.SchemaError):
        bot.validate_grades_document(documents["grades.json"])


def test_grade_rejects_unknown_fields_and_values(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    grade = {
        "gradeId": "grade-bad-value-1",
        "studentId": "s01",
        "subjectId": "math",
        "subject": "Математика",
        "lessonDate": "2026-11-10",
        "entryType": "daily",
        "value": "6",
        "raw": "6",
        "confidence": 0.9,
        "source": "gemini",
        "status": "automatic",
        "evidence": ["a" * 64],
        "extra": "forbidden",
    }
    documents["grades.json"]["grades"].append(grade)
    with pytest.raises(bot.SchemaError):
        bot.validate_grades_document(documents["grades.json"])


def test_duplicate_grade_key_is_rejected(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    grade = {
        "gradeId": "grade-one-0001",
        "studentId": "s01",
        "subjectId": "math",
        "subject": "Математика",
        "lessonDate": "2026-11-10",
        "entryType": "daily",
        "value": "5",
        "raw": "5",
        "confidence": 0.9,
        "source": "gemini",
        "status": "automatic",
        "evidence": ["a" * 64],
    }
    duplicate = copy.deepcopy(grade)
    duplicate["gradeId"] = "grade-two-0002"
    documents["grades.json"]["grades"] = [grade, duplicate]
    with pytest.raises(bot.SchemaError, match="ключ"):
        bot.validate_grades_document(documents["grades.json"])


def test_manifest_must_share_one_revision(data_dir: Path) -> None:
    manifest = seed_manifest(data_dir)
    manifest["files"]["tasks.json"] = "older-revision"
    with pytest.raises(bot.SchemaError):
        bot.validate_manifest_document(manifest)


def test_duties_require_two_selected_students(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    day = {
        "date": "2026-11-16",
        "selected": ["s01"],
        "selectedNames": ["Аглиуллина Самира"],
        "actual": [],
        "actualNames": [],
        "reports": [],
        "status": "selected",
        "seed": "a" * 64,
        "algorithmVersion": bot.DUTY_ALGORITHM_VERSION,
        "closed": False,
    }
    documents["duties.json"]["days"] = [day]
    with pytest.raises(bot.SchemaError):
        bot.validate_duties_document(documents["duties.json"])


def test_split_english_requires_both_subgroups(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    lesson = next(item for item in documents["schedule.json"]["lessons"] if item["subgroup"] == "split")
    lesson["subgroupOptions"] = [{"id": "2к207", "teacher": "", "room": "2к207"}]
    with pytest.raises(bot.SchemaError):
        bot.validate_schedule_document(documents["schedule.json"])


def test_bundle_rejects_unknown_student_reference(data_dir: Path) -> None:
    documents = load_seed(data_dir)
    documents["tasks.json"]["tasks"].append({
        "taskId": "task-unknown-01",
        "subjectId": "math",
        "subject": "Математика",
        "text": "Проверка",
        "assignedDate": "2026-11-18",
        "nextLessonDate": "2026-11-19",
        "nextLessonId": "thu-4-history",
        "status": "актуальное",
        "createdAt": "2026-11-18T10:00:00+03:00",
        "updatedAt": "2026-11-18T10:00:00+03:00",
        "sourceUserId": 1,
    })
    # tasks intentionally have no studentId; break a grade reference instead.
    documents["grades.json"]["grades"].append({
        "gradeId": "grade-unknown-1",
        "studentId": "s99",
        "subjectId": "math",
        "subject": "Математика",
        "lessonDate": "2026-11-10",
        "entryType": "daily",
        "value": "5",
        "raw": "5",
        "confidence": 0.9,
        "source": "gemini",
        "status": "automatic",
        "evidence": ["a" * 64],
    })
    with pytest.raises(bot.SchemaError):
        bot.validate_bundle(documents, seed_manifest(data_dir))


def test_empty_optional_documents_are_valid() -> None:
    for name in ("grades.json", "tasks.json", "duties.json", "replacements.json", "reviews.json"):
        bot.validate_document(name, bot.empty_document(name, "seed"))
