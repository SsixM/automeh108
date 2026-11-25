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
        self.publish_count = 0

    def publish(self, mutators, event_id, message):
        self.publish_count += 1
        for name, mutator in mutators.items():
            mutator(self.docs[name])
        return bot.PublishResult("memory-revision", True)

    def read_documents(self):
        return copy.deepcopy(self.docs), {}


@pytest.fixture()
def service(data_dir: Path):
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    return bot.HomeworkService(storage, bot.ScheduleService()), storage, docs


def test_homework_without_date_uses_message_date(service) -> None:
    homework, storage, docs = service
    draft = homework.parse(
        "Математика выучить логарифмы",
        date(2026, 11, 17),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert draft.subject_id == "math"
    assert draft.subject == "Математика"
    assert draft.text == "выучить логарифмы"
    assert draft.assigned_date == date(2026, 11, 17)
    assert draft.next_lesson_date == "2026-11-20"


def test_homework_date_at_end_and_year(service) -> None:
    homework, _, docs = service
    draft = homework.parse(
        "Математика выучить логарифмы 10.09",
        date(2026, 11, 17),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert draft.assigned_date == date(2026, 9, 10)
    assert draft.text == "выучить логарифмы"
    assert draft.next_lesson_date == "2026-09-11"

    explicit = homework.parse(
        "Химия повторить кислоты 12.11.2026",
        date(2026, 11, 17),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert explicit.assigned_date == date(2026, 11, 12)
    assert explicit.next_lesson_date == "2026-11-17"


def test_homework_typo_and_abbreviation(service) -> None:
    homework, _, docs = service
    typo = homework.parse(
        "Математик выучить формулы",
        date(2026, 11, 17),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert typo.subject_id == "math"
    abbreviation = homework.parse(
        "Введ подготовить доклад",
        date(2026, 11, 17),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert abbreviation.subject_id == "intro"
    assert abbreviation.subject == "Введение в специальность"


def test_homework_rejects_two_subjects_and_empty_text(service) -> None:
    homework, _, docs = service
    with pytest.raises(bot.UserFacingError, match="несколько предметов"):
        homework.parse(
            "Математика Русский язык выучить правила",
            date(2026, 11, 17),
            docs["schedule.json"],
            docs["replacements.json"],
        )
    with pytest.raises(bot.UserFacingError, match="текст задания"):
        homework.parse(
            "Математика 10.09",
            date(2026, 11, 17),
            docs["schedule.json"],
            docs["replacements.json"],
        )
    with pytest.raises(bot.UserFacingError, match="служебное"):
        homework.parse(
            "Разговоры о важном посмотреть видео",
            date(2026, 11, 17),
            docs["schedule.json"],
            docs["replacements.json"],
        )


def test_homework_duplicate_and_second_text_appends(service) -> None:
    homework, storage, docs = service
    first = homework.add(
        "Математика выучить логарифмы",
        1001,
        10,
        date(2026, 11, 17),
        docs,
    )
    assert first.status == "added"
    assert len(storage.docs["tasks.json"]["tasks"]) == 1

    duplicate = homework.add(
        "Математика выучить логарифмы",
        1001,
        11,
        date(2026, 11, 17),
        docs,
    )
    assert duplicate.status == "duplicate"
    assert len(storage.docs["tasks.json"]["tasks"]) == 1

    second = homework.add(
        "Математика выучить логарифмы и степени",
        1001,
        12,
        date(2026, 11, 17),
        docs,
    )
    assert second.status == "added"
    assert second.siblings == 1
    tasks = storage.docs["tasks.json"]["tasks"]
    assert len(tasks) == 2
    assert {task["text"] for task in tasks} == {"выучить логарифмы", "выучить логарифмы и степени"}


def test_repeated_telegram_update_does_not_change_task(service) -> None:
    homework, storage, docs = service
    homework.add("Математика выучить логарифмы", 1001, 42, date(2026, 11, 17), docs)
    before = copy.deepcopy(storage.docs["tasks.json"])
    repeated = homework.add("Математика выучить логарифмы", 1001, 42, date(2026, 11, 17), docs)
    assert repeated.status == "duplicate"
    assert storage.docs["tasks.json"] == before


def test_attachment_is_added_once_and_photo_only_task(service) -> None:
    homework, storage, docs = service
    attachment = {
        "attachmentId": "att-" + "a" * 20,
        "url": "https://raw.githubusercontent.com/SsixM/automeh108/main/media/homework/x.webp",
        "fileName": "homework.webp",
        "mimeType": "image/webp",
        "size": 1024,
        "width": 800,
        "height": 600,
        "sha256": "a" * 64,
        "createdAt": "2026-11-17T12:00:00+03:00",
    }
    first = homework.add(
        "Математика выучить логарифмы",
        1001,
        30,
        date(2026, 11, 17),
        docs,
        attachments=[attachment],
    )
    assert first.status == "added"
    task = storage.docs["tasks.json"]["tasks"][0]
    assert task["attachments"][0]["attachmentId"] == attachment["attachmentId"]

    repeated = homework.add(
        "Математика выучить логарифмы",
        1001,
        31,
        date(2026, 11, 17),
        docs,
        attachments=[attachment],
    )
    assert repeated.status == "duplicate"
    assert len(storage.docs["tasks.json"]["tasks"][0]["attachments"]) == 1

    second_attachment = {**attachment, "attachmentId": "att-" + "b" * 20, "sha256": "b" * 64}
    attached = homework.add(
        "Математика выучить логарифмы",
        1001,
        32,
        date(2026, 11, 17),
        docs,
        attachments=[second_attachment],
    )
    assert attached.status == "attached"
    assert len(storage.docs["tasks.json"]["tasks"][0]["attachments"]) == 2

    photo_only = homework.add(
        "Химия",
        1001,
        33,
        date(2026, 11, 17),
        docs,
        allow_empty_text=True,
    )
    assert photo_only.status == "added"
    assert photo_only.draft.text == "Задание на фото"


def test_homework_skips_cancelled_lesson(service) -> None:
    homework, _, docs = service
    docs["replacements.json"]["items"] = [
        {
            "replacementId": "cancel-fri-0001",
            "date": "2026-11-20",
            "weekType": "denominator",
            "pairNumber": 1,
            "originalLessonId": "fri-1-pe",
            "newSubjectId": "math",
            "newSubject": "Математика",
            "newRoom": "2к302",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "d" * 64,
            "receivedAt": "2026-11-19T20:00:00+03:00",
            "group": "108М",
        },
        {
            "replacementId": "cancel-fri-0002",
            "date": "2026-11-20",
            "weekType": "denominator",
            "pairNumber": 2,
            "originalLessonId": "fri-2-pe",
            "newSubjectId": "math",
            "newSubject": "Математика",
            "newRoom": "2к302",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "d" * 64,
            "receivedAt": "2026-11-19T20:00:00+03:00",
            "group": "108М",
        },
    ]
    draft = homework.parse(
        "Математика выучить логарифмы",
        date(2026, 11, 18),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    assert draft.next_lesson_date == "2026-11-23"
    assert draft.moved is True


def test_recalculate_moves_task_after_replacement(service) -> None:
    homework, storage, docs = service
    homework.add("Математика выучить логарифмы", 1001, 55, date(2026, 11, 18), docs)
    storage.docs["replacements.json"]["items"] = [
        {
            "replacementId": "cancel-fri-0003",
            "date": "2026-11-20",
            "weekType": "denominator",
            "pairNumber": 1,
            "originalLessonId": "fri-1-pe",
            "newSubjectId": "math",
            "newSubject": "Математика",
            "newRoom": "2к302",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "e" * 64,
            "receivedAt": "2026-11-19T20:00:00+03:00",
            "group": "108М",
        },
        {
            "replacementId": "cancel-fri-0004",
            "date": "2026-11-20",
            "weekType": "denominator",
            "pairNumber": 2,
            "originalLessonId": "fri-2-pe",
            "newSubjectId": "math",
            "newSubject": "Математика",
            "newRoom": "2к302",
            "cancelled": True,
            "source": "Telegram forwarded PDF",
            "sourceHash": "e" * 64,
            "receivedAt": "2026-11-19T20:00:00+03:00",
            "group": "108М",
        },
    ]
    changed = homework.recalculate(storage.docs, today=date(2026, 11, 18))
    assert changed >= 1
    task = storage.docs["tasks.json"]["tasks"][0]
    assert task["nextLessonDate"] == "2026-11-23"
    assert task["movedByReplacement"] is True
    assert task["status"] == "перенесено"


def test_describe_contains_date_pair_and_room(service) -> None:
    homework, _, docs = service
    draft = homework.parse(
        "Химия повторить кислоты",
        date(2026, 11, 12),
        docs["schedule.json"],
        docs["replacements.json"],
    )
    text = homework.describe(draft, docs["schedule.json"])
    assert "17 ноября" in text
    assert "3 пара" in text
    assert "каб. 2к105" in text
