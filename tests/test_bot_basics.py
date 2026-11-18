from __future__ import annotations

import asyncio
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


def test_week_type_uses_monday_anchor(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    service = bot.ScheduleService()
    assert service.week_type(date(2026, 9, 1), docs["schedule.json"]) == "numerator"
    assert service.week_type(date(2026, 11, 9), docs["schedule.json"]) == "numerator"
    assert service.week_type(date(2026, 11, 16), docs["schedule.json"]) == "denominator"


def test_replacement_source_can_override_week_type(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    item = {
        "replacementId": "replace-override-01",
        "date": "2026-11-12",
        "weekType": "denominator",
        "pairNumber": 1,
        "newSubjectId": "math",
        "newSubject": "Математика",
        "newRoom": "2к302",
        "cancelled": False,
        "source": "VK post 1",
        "sourceHash": "a" * 64,
        "receivedAt": "2026-11-11T20:00:00+03:00",
        "group": "108М",
    }
    replacements = {"items": [item]}
    service = bot.ScheduleService()
    assert service.week_type(date(2026, 11, 12), docs["schedule.json"], replacements) == "denominator"


def test_denominator_rules_and_individual_replacement(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    docs["replacements.json"]["items"] = [{
        "replacementId": "replace-thu-0001",
        "date": "2026-11-19",
        "weekType": "denominator",
        "pairNumber": 4,
        "originalLessonId": "thu-4-history",
        "originalSubject": "История",
        "newSubjectId": "chemistry",
        "newSubject": "Химия",
        "newRoom": "2к105",
        "cancelled": False,
        "source": "Telegram forwarded PDF",
        "sourceHash": "b" * 64,
        "receivedAt": "2026-11-18T20:00:00+03:00",
        "group": "108М",
    }]
    service = bot.ScheduleService()
    week_type, lessons = service.effective_lessons(date(2026, 11, 19), docs["schedule.json"], docs["replacements.json"])
    assert week_type == "denominator"
    by_pair = {lesson["pairNumber"]: lesson for lesson in lessons}
    assert by_pair[4]["subjectId"] == "chemistry"
    assert by_pair[4]["replacementKind"] == "individual"
    assert by_pair[4]["originalSubject"] == "История"

    _, friday = service.effective_lessons(date(2026, 11, 20), docs["schedule.json"], {"items": []})
    friday_by_pair = {lesson["pairNumber"]: lesson for lesson in friday}
    assert friday_by_pair[1]["subjectId"] == "math"
    assert friday_by_pair[1]["replacementKind"] == "rule"
    assert friday_by_pair[3]["subjectId"] == "pe"


def test_next_lesson_skips_cancelled_lesson(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    docs["replacements.json"]["items"] = [
        {
            "replacementId": "cancel-thu-0001",
            "date": "2026-11-19",
            "weekType": "denominator",
            "pairNumber": 3,
            "originalLessonId": "thu-3-informatics",
            "newSubjectId": "informatics",
            "newSubject": "Информатика",
            "newRoom": "406",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "c" * 64,
            "receivedAt": "2026-11-18T20:00:00+03:00",
            "group": "108М",
        },
        {
            "replacementId": "cancel-thu-0002",
            "date": "2026-11-19",
            "weekType": "denominator",
            "pairNumber": 4,
            "originalLessonId": "thu-4-history",
            "newSubjectId": "informatics",
            "newSubject": "Информатика",
            "newRoom": "406",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "c" * 64,
            "receivedAt": "2026-11-18T20:00:00+03:00",
            "group": "108М",
        },
    ]
    service = bot.ScheduleService()
    lesson = service.next_lesson(
        "informatics",
        date(2026, 11, 18),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert lesson is not None
    assert lesson["date"] == "2026-11-26"


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, user_id: int, update_id: int):
        self.update_id = update_id
        self.effective_user = FakeUser(user_id)
        self.effective_message = FakeMessage()


class StubStorage:
    def __init__(self, docs, manifest):
        self.docs = docs
        self.manifest = manifest
        self.calls = 0

    def read_documents(self):
        self.calls += 1
        return copy.deepcopy(self.docs), copy.deepcopy(self.manifest)


def make_backend(tmp_path: Path, docs, manifest) -> tuple[bot.Backend, StubStorage]:
    config = bot.Config(
        telegram_token="telegram-secret",
        allowed_telegram_ids=frozenset({1001}),
        github_token="github-secret",
        gemini_api_key="gemini-secret",
        bot_state_file=tmp_path / "state.json",
    )
    storage = StubStorage(docs, manifest)
    backend = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    return backend, storage


def test_allowlist_blocks_storage_before_any_read(tmp_path: Path, data_dir: Path) -> None:
    docs, manifest = load_seed(data_dir)
    current, storage = make_backend(tmp_path, docs, manifest)
    old_backend = bot.backend
    bot.backend = current
    try:
        unauthorized = FakeUpdate(9999, 1)
        asyncio.run(bot.today_command(unauthorized, None))
        assert storage.calls == 0
        assert unauthorized.effective_message.replies
        assert "только участникам" in unauthorized.effective_message.replies[0]
    finally:
        bot.backend = old_backend


def test_today_command_replies_and_duplicate_update_is_ignored(tmp_path: Path, data_dir: Path) -> None:
    docs, manifest = load_seed(data_dir)
    docs["tasks.json"]["tasks"].append({
        "taskId": "task-today-0001",
        "subjectId": "russian",
        "subject": "Русский язык",
        "text": "Подготовить пересказ",
        "assignedDate": "2026-11-17",
        "nextLessonDate": bot.today_moscow().isoformat(),
        "nextLessonId": "wed-1-russian",
        "status": "актуальное",
        "createdAt": "2026-11-17T12:00:00+03:00",
        "updatedAt": "2026-11-17T12:00:00+03:00",
        "sourceUserId": 1001,
    })
    current, storage = make_backend(tmp_path, docs, manifest)
    old_backend = bot.backend
    bot.backend = current
    try:
        update = FakeUpdate(1001, 777)
        asyncio.run(bot.today_command(update, None))
        assert storage.calls == 1
        text = "\n".join(update.effective_message.replies)
        assert "Сегодня" in text
        assert "Русский язык" in text
        asyncio.run(bot.today_command(update, None))
        assert storage.calls == 1
        assert len(update.effective_message.replies) == 1
    finally:
        bot.backend = old_backend


def test_update_guard_claims_parallel_duplicates_once(tmp_path: Path) -> None:
    local = bot.LocalState(tmp_path / "state.json")
    guard = bot.UpdateGuard(local)

    async def scenario():
        first, second = await asyncio.gather(guard.claim(5), guard.claim(5))
        guard.complete(5)
        return first, second, await guard.claim(5)

    first, second, third = asyncio.run(scenario())
    assert first is True
    assert second is False
    assert third is False


def test_backend_status_contains_no_secret(tmp_path: Path, data_dir: Path) -> None:
    docs, manifest = load_seed(data_dir)
    current, _ = make_backend(tmp_path, docs, manifest)
    old_backend = bot.backend
    bot.backend = current
    try:
        update = FakeUpdate(1001, 888)
        asyncio.run(bot.status_command(update, None))
        text = "\n".join(update.effective_message.replies)
        assert manifest["revision"] in text
        assert "telegram-secret" not in text
        assert "github-secret" not in text
        assert "gemini-secret" not in text
    finally:
        bot.backend = old_backend
