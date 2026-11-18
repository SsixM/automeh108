from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

DATA_FILES = [
    "students.json",
    "schedule.json",
    "grades.json",
    "tasks.json",
    "duties.json",
    "replacements.json",
    "reviews.json",
    "manifest.json",
]

EXPECTED_STUDENTS = [
    ("s01", "Аглиуллина Самира", "2к207"),
    ("s02", "Алборов Михаил", "2к207"),
    ("s03", "Бадреев Айдар", "2к207"),
    ("s04", "Гордеева Милана", "2к207"),
    ("s05", "Гуцу Вадим", "2к207"),
    ("s06", "Долгошеев Максим", "2к207"),
    ("s07", "Звизило Илья", "2к207"),
    ("s08", "Зданович Тихон", "2к207"),
    ("s09", "Камалова Ландыш", "2к207"),
    ("s10", "Колбасов Тимур", "2к207"),
    ("s11", "Кольцов Вячеслав", "2к207"),
    ("s12", "Маматбаев Кирилл", "2к207"),
    ("s13", "Маннанов Камиль", "2к207"),
    ("s14", "Мухаметшин Ильяс", "2к207"),
    ("s15", "Нуретдинова Ильвина", "2к207"),
    ("s16", "Осадчий Максим", "2к207"),
    ("s17", "Паймушев Макар", "2к207"),
    ("s18", "Плотников Вячеслав", "2к202"),
    ("s19", "Сафин Равиль", "2к202"),
    ("s20", "Соловьев Илья", "2к202"),
    ("s21", "Сыромятников Данислав", "2к202"),
    ("s22", "Тюляндин Александр", "2к202"),
    ("s23", "Хафизов Юсуф", "2к202"),
    ("s24", "Шакиров Имиль", "2к202"),
    ("s25", "Юнусов Артур", "2к202"),
    ("s26", "Абакаев Артем", "2к202"),
    ("s27", "Гайфутдинов Владислав", "2к202"),
    ("s28", "Галиев Нияз", "2к202"),
    ("s29", "Ефремова Валерия", "2к202"),
    ("s30", "Кузнецов Леонид", "2к202"),
    ("s31", "Кузьмин Егор", "2к202"),
    ("s32", "Шакирзянов Карим", "2к202"),
    ("s33", "Шарипов Булат", "2к202"),
]


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", DATA_FILES)
def test_data_matches_schema(data_dir: Path, schema_dir: Path, name: str) -> None:
    schema_name = name.removesuffix(".json") + ".schema.json"
    schema = read(schema_dir / schema_name)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(read(data_dir / name)))
    assert not errors, "\n".join(error.message for error in errors[:10])


def test_students_are_exactly_the_plan_list(data_dir: Path) -> None:
    students = read(data_dir / "students.json")["students"]
    assert [(s["studentId"], s["name"], s["englishGroup"]) for s in students] == EXPECTED_STUDENTS


def test_all_files_share_one_revision(data_dir: Path) -> None:
    manifest = read(data_dir / "manifest.json")
    revision = manifest["revision"]
    assert set(manifest["files"].values()) == {revision}
    for name in DATA_FILES:
        assert read(data_dir / name)["revision"] == revision


def test_schedule_has_all_pairs_and_english_subgroups(data_dir: Path) -> None:
    schedule = read(data_dir / "schedule.json")
    assert [item["pairNumber"] for item in schedule["times"]] == list(range(1, 8))
    assert schedule["times"][1]["end"] == "11:40"
    assert schedule["times"][2]["start"] == "12:10"

    english = [lesson for lesson in schedule["lessons"] if lesson["subjectId"] == "english"]
    assert len(english) == 2
    for lesson in english:
        assert lesson["subgroup"] == "split"
        assert {option["id"] for option in lesson["subgroupOptions"]} == {"2к207", "2к202"}

    service = {lesson["lessonId"] for lesson in schedule["lessons"] if lesson["service"]}
    assert service == {"mon-1-important", "thu-1-horizons"}


def test_denominator_rules_are_data_driven(data_dir: Path) -> None:
    rules = read(data_dir / "schedule.json")["replacementRules"]
    by_id = {rule["ruleId"]: rule for rule in rules}
    assert by_id["thu-denominator-history-to-informatics"]["newSubjectId"] == "informatics"
    assert by_id["fri-denominator-pe-to-math-1"]["newRoom"] == "2к302"
    assert by_id["fri-denominator-pe-to-math-2"]["newSubjectId"] == "math"
    assert all(rule["weekType"] == "denominator" for rule in rules)


def test_duties_initial_state_has_all_students_at_zero(data_dir: Path) -> None:
    duties = read(data_dir / "duties.json")
    students = read(data_dir / "students.json")["students"]
    assert set(duties["students"]) == {student["studentId"] for student in students}
    assert all(record["count"] == 0 for record in duties["students"].values())
    assert duties["days"] == []
    assert duties["pairPreferences"] == []


def test_no_invented_grades_tasks_or_replacements(data_dir: Path) -> None:
    assert read(data_dir / "grades.json")["grades"] == []
    assert read(data_dir / "tasks.json")["tasks"] == []
    assert read(data_dir / "replacements.json")["items"] == []
    assert read(data_dir / "reviews.json")["items"] == []


def test_gemini_fixtures_match_response_schema(schema_dir: Path, fixtures_dir: Path) -> None:
    schema = read(schema_dir / "gemini-journal-response.schema.json")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for name in ["gemini-normal.json", "gemini-low-confidence.json"]:
        assert not list(validator.iter_errors(read(fixtures_dir / name))), name
    invalid = read(fixtures_dir / "gemini-invalid-value.json")
    assert list(validator.iter_errors(invalid))
