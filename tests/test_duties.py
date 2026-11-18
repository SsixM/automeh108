from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

import pytest

import bot


def load_seed(data_dir: Path) -> tuple[dict[str, dict], dict]:
    docs = {name: json.loads((data_dir / name).read_text(encoding="utf-8")) for name in bot.DATA_FILES}
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    return docs, manifest


class MemoryStorage:
    def __init__(self, docs):
        self.docs = copy.deepcopy(docs)
        self.revision = 0
        self.publish_count = 0

    def publish(self, mutators, event_id, message):
        self.publish_count += 1
        self.revision += 1
        for name, mutator in mutators.items():
            mutator(self.docs[name])
        return bot.PublishResult(f"memory-{self.revision}", True)

    def read_documents(self):
        return copy.deepcopy(self.docs), {}


@pytest.fixture()
def service(data_dir: Path):
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    return bot.DutyService(storage), storage, docs


def all_zero(docs) -> None:
    for record in docs["duties.json"]["students"].values():
        record["count"] = 0


def test_selection_is_deterministic_and_reload_safe(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    first = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    second = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert first == second
    assert len(first) == 2
    assert len(set(first)) == 2
    seed = duties.seed_for(date(2026, 11, 18), docs["students.json"])
    restored = bot.DutyService(storage)
    assert restored.seed_for(date(2026, 11, 18), docs["students.json"]) == seed


def test_selection_picks_unique_minimum_then_next_layer(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    docs["duties.json"]["students"]["s01"]["count"] = 0
    for student_id in docs["duties.json"]["students"]:
        if student_id != "s01":
            docs["duties.json"]["students"][student_id]["count"] = 1
    selected = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert "s01" in selected
    assert len(selected) == 2


def test_selection_picks_two_when_only_two_minimum(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    for student_id in docs["duties.json"]["students"]:
        docs["duties.json"]["students"][student_id]["count"] = 2
    docs["duties.json"]["students"]["s04"]["count"] = 0
    docs["duties.json"]["students"]["s09"]["count"] = 0
    selected = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert set(selected) == {"s04", "s09"}


def test_ensure_day_persists_selection_and_survives_student_list_change(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    day = duties.ensure_day(date(2026, 11, 18), docs)
    assert len(day["selected"]) == 2
    selected_before = list(day["selected"])
    stored = storage.docs["duties.json"]["days"][0]
    assert stored["selected"] == selected_before
    assert stored["closed"] is False

    docs["students.json"]["students"].append({
        "studentId": "s34",
        "name": "Новый Ученик",
        "englishGroup": "2к202",
        "aliases": [],
    })
    same = duties.ensure_day(date(2026, 11, 18), docs)
    assert same["selected"] == selected_before
    assert len(storage.docs["duties.json"]["days"]) == 1


def test_parse_exact_surname_given_typo_and_ambiguous(service) -> None:
    duties, _, docs = service
    students = docs["students.json"]
    exact = duties.parse_report("Дежурили Аглиуллина Самира, Бадреев, Осадчий", students)
    assert exact is not None
    assert exact.student_ids == ["s01", "s03", "s16"]
    assert exact.ambiguous == []

    typo = duties.parse_report("Дежурили Аббакаев, Бадреев", students)
    assert typo is not None
    assert "s26" in typo.student_ids

    unique_name = duties.parse_report("Дежурили Айдар", students)
    assert unique_name is not None
    assert unique_name.student_ids == ["s03"]

    ambiguous = duties.parse_report("Дежурили Максим", students)
    assert ambiguous is not None
    assert ambiguous.student_ids == []
    assert ambiguous.ambiguous

    one_letter = duties.parse_report("Дежурили А", students)
    assert one_letter is not None
    assert one_letter.student_ids == []
    assert one_letter.ignored

    comma_less = duties.parse_report("Дежурили Абакаев Бадреев Осадчий", students)
    assert comma_less is not None
    assert set(comma_less.student_ids) == {"s26", "s03", "s16"}


def test_five_people_are_all_resolved(service) -> None:
    duties, _, docs = service
    parsed = duties.parse_report(
        "Дежурили Абакаев, Бадреев, Осадчий, Сафин, Кузьмин",
        docs["students.json"],
    )
    assert parsed is not None
    assert len(parsed.student_ids) == 5
    assert not parsed.ambiguous


def test_close_credits_only_reported_people(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    day = duties.ensure_day(date(2026, 11, 18), docs)
    selected = list(day["selected"])
    report = duties.add_report(
        date(2026, 11, 18),
        "Дежурили Абакаев, Бадреев, Осадчий",
        1001,
        1,
        docs,
    )
    assert report.status == "confirmed"
    result = duties.close_day(date(2026, 11, 18), docs)
    assert result.status == "closed"
    for student_id, record in storage.docs["duties.json"]["students"].items():
        expected = 1 if student_id in {"s26", "s03", "s16"} else 0
        assert record["count"] == expected
    for student_id in selected:
        if student_id not in {"s26", "s03", "s16"}:
            assert storage.docs["duties.json"]["students"][student_id]["count"] == 0
    stored_day = storage.docs["duties.json"]["days"][0]
    assert stored_day["closed"] is True
    assert stored_day["actual"] == ["s26", "s03", "s16"]


def test_close_without_report_credits_nobody(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    result = duties.close_day(date(2026, 11, 18), docs)
    assert result.status == "closed-without-confirmation"
    assert all(record["count"] == 0 for record in storage.docs["duties.json"]["students"].values())


def test_pending_report_credits_nobody(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    report = duties.add_report(date(2026, 11, 18), "Дежурили Максим", 1001, 2, docs)
    assert report.status == "pending"
    result = duties.close_day(date(2026, 11, 18), docs)
    assert result.status == "closed-without-confirmation"
    assert all(record["count"] == 0 for record in storage.docs["duties.json"]["students"].values())


def test_repeated_close_is_idempotent(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    duties.add_report(date(2026, 11, 18), "Дежурили Абакаев, Бадреев", 1001, 3, docs)
    duties.close_day(date(2026, 11, 18), docs)
    counts_after_first = {sid: record["count"] for sid, record in storage.docs["duties.json"]["students"].items()}
    second = duties.close_day(date(2026, 11, 18), docs)
    assert second.status == "already-closed"
    counts_after_second = {sid: record["count"] for sid, record in storage.docs["duties.json"]["students"].items()}
    assert counts_after_first == counts_after_second


def test_duplicate_report_is_not_duplicated(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    duties.add_report(date(2026, 11, 18), "Дежурили Абакаев, Бадреев", 1001, 7, docs)
    duties.add_report(date(2026, 11, 18), "Дежурили Абакаев, Бадреев", 1001, 7, docs)
    day = storage.docs["duties.json"]["days"][0]
    assert len(day["reports"]) == 1


def test_report_after_close_is_rejected(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    duties.close_day(date(2026, 11, 18), docs)
    with pytest.raises(bot.UserFacingError, match="уже закрыт"):
        duties.add_report(date(2026, 11, 18), "Дежурили Абакаев", 1001, 9, docs)


def test_weekend_selection_is_rejected(service) -> None:
    duties, _, docs = service
    with pytest.raises(bot.UserFacingError, match="выходные"):
        duties.select_for(date(2026, 11, 21), docs["students.json"], docs["duties.json"])


def test_selection_picks_preferred_pair_inside_minimum_layer(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    docs["duties.json"]["pairPreferences"] = [["s04", "s09"]]
    selected = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert set(selected) == {"s04", "s09"}


def test_selection_allows_partner_one_layer_above(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    for student_id in docs["duties.json"]["students"]:
        docs["duties.json"]["students"][student_id]["count"] = 1
    docs["duties.json"]["students"]["s04"]["count"] = 0
    docs["duties.json"]["pairPreferences"] = [["s04", "s09"]]
    selected = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert set(selected) == {"s04", "s09"}


def test_selection_does_not_force_partner_far_above_minimum(service) -> None:
    duties, _, docs = service
    all_zero(docs)
    docs["duties.json"]["students"]["s04"]["count"] = 0
    docs["duties.json"]["students"]["s09"]["count"] = 3
    for student_id in docs["duties.json"]["students"]:
        if student_id not in {"s04", "s09"}:
            docs["duties.json"]["students"][student_id]["count"] = 1
    docs["duties.json"]["pairPreferences"] = [["s04", "s09"]]
    selected = duties.select_for(date(2026, 11, 18), docs["students.json"], docs["duties.json"])
    assert "s04" in selected
    assert "s09" not in selected


def test_pair_command_persists_and_removes(service) -> None:
    duties, storage, docs = service
    names, changed = duties.update_pair(["Абакаев", "Бадреев"], docs["students.json"], docs)
    assert changed is True
    assert names == ["Абакаев Артем", "Бадреев Айдар"]
    assert storage.docs["duties.json"]["pairPreferences"] == [["s26", "s03"]]
    names, changed = duties.update_pair(["Абакаев", "Бадреев"], docs["students.json"], docs)
    assert changed is False
    names, changed = duties.update_pair(["Абакаев", "Бадреев"], docs["students.json"], docs, remove=True)
    assert changed is True
    assert storage.docs["duties.json"]["pairPreferences"] == []


def test_pair_command_rejects_ambiguous_and_same_student(service) -> None:
    duties, _, docs = service
    with pytest.raises(bot.UserFacingError, match="однозначно"):
        duties.update_pair(["Максим", "Бадреев"], docs["students.json"], docs)
    with pytest.raises(bot.UserFacingError, match="два разных"):
        duties.update_pair(["Бадреев", "Бадреев"], docs["students.json"], docs)


def test_close_unions_two_reports(service) -> None:
    duties, storage, docs = service
    all_zero(docs)
    duties.ensure_day(date(2026, 11, 18), docs)
    duties.add_report(date(2026, 11, 18), "Дежурили Абакаев, Бадреев", 1001, 20, docs)
    second = duties.add_report(date(2026, 11, 18), "Дежурили Осадчий", 1001, 21, docs)
    assert second.all_names == ["Абакаев Артем", "Бадреев Айдар", "Осадчий Максим"]
    assert second.added_names == ["Осадчий Максим"]
    result = duties.close_day(date(2026, 11, 18), docs)
    assert result.status == "closed"
    assert set(result.names) == {"Абакаев Артем", "Бадреев Айдар", "Осадчий Максим"}
    counts = {sid: record["count"] for sid, record in storage.docs["duties.json"]["students"].items()}
    assert counts["s26"] == 1
    assert counts["s03"] == 1
    assert counts["s16"] == 1
    assert sum(counts.values()) == 3


def test_scheduler_closes_past_and_creates_today(service, tmp_path: Path) -> None:
    duties, storage, docs = service
    all_zero(docs)
    past = {
        "date": "2026-11-17",
        "selected": ["s01", "s02"],
        "selectedNames": ["Аглиуллина Самира", "Алборов Михаил"],
        "actual": [],
        "actualNames": [],
        "reports": [{
            "reportId": "report-past-01",
            "userId": 1001,
            "receivedAt": "2026-11-17T18:00:00+03:00",
            "status": "confirmed",
            "studentIds": ["s01"],
            "studentNames": ["Аглиуллина Самира"],
            "ambiguous": [],
            "messageHash": "b" * 64,
        }],
        "status": "reported",
        "seed": "a" * 64,
        "algorithmVersion": bot.DUTY_ALGORITHM_VERSION,
        "closed": False,
    }
    storage.docs["duties.json"]["days"] = [past]
    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        bot_state_file=tmp_path / "state.json",
    )
    current = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    scheduler = bot.BotScheduler(current)
    import asyncio
    asyncio.run(scheduler.run_maintenance(date(2026, 11, 18)))
    asyncio.run(scheduler.run_maintenance(date(2026, 11, 18)))
    days = {item["date"]: item for item in storage.docs["duties.json"]["days"]}
    assert days["2026-11-17"]["closed"] is True
    assert days["2026-11-17"]["status"] == "closed"
    assert days["2026-11-17"]["actual"] == ["s01"]
    assert storage.docs["duties.json"]["students"]["s01"]["count"] == 1
    assert "2026-11-18" in days
    assert len(days["2026-11-18"]["selected"]) == 2
