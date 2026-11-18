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
        self.last_event = ""

    def read_documents(self):
        return copy.deepcopy(self.docs), {}

    def publish(self, mutators, event_id, message):
        self.publish_count += 1
        self.last_event = event_id
        for name, mutator in mutators.items():
            mutator(self.docs[name])
        return bot.PublishResult("memory-revision", True)

    def publish_all(self, mutator, event_id, message):
        self.publish_count += 1
        self.last_event = event_id
        mutator(self.docs)
        return bot.PublishResult("memory-revision", True)


@pytest.fixture()
def schedule(data_dir: Path):
    docs, _ = load_seed(data_dir)
    return docs["schedule.json"]


def read_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def test_parse_only_108m_rows(schedule) -> None:
    result = bot.ReplacementParser.parse(
        read_fixture("replacements-108m.txt"),
        schedule,
        "Telegram PDF",
        "a" * 64,
    )
    assert result.status == "parsed"
    assert result.target_date == date(2026, 11, 21)
    assert result.week_type == "denominator"
    assert len(result.rows) == 2
    by_pair = {row["pairNumber"]: row for row in result.rows}
    assert by_pair[1]["newSubjectId"] == "math"
    assert by_pair[1]["newRoom"] == "2к302"
    assert by_pair[4]["newSubjectId"] == "informatics"
    assert all(row["group"] == bot.GROUP_NAME for row in result.rows)
    assert all(row["weekType"] == "denominator" for row in result.rows)


def test_parse_multiple_groups_ignores_others(schedule) -> None:
    result = bot.ReplacementParser.parse(
        read_fixture("replacements-multiple-groups.txt"),
        schedule,
        "VK",
        "b" * 64,
    )
    assert [row["pairNumber"] for row in result.rows] == [2]
    assert result.rows[0]["newSubjectId"] == "chemistry"
    assert result.week_type == "numerator"


def test_parse_without_108m_creates_nothing(schedule) -> None:
    result = bot.ReplacementParser.parse(
        read_fixture("replacements-no-108m.txt"),
        schedule,
        "VK",
        "c" * 64,
    )
    assert result.status == "no-108m"
    assert result.rows == []


def test_parse_requires_date(schedule) -> None:
    with pytest.raises(bot.UserFacingError, match="дату"):
        bot.ReplacementParser.parse("108М\n1 пара: Математика", schedule, "VK", "d" * 64)


def test_parse_date_fallback_and_cancellation(schedule) -> None:
    text = "108М\n2 пара: Физика отмена\n3 пара: История — каб. 2к201"
    result = bot.ReplacementParser.parse(
        text,
        schedule,
        "Telegram PDF",
        "e" * 64,
        fallback_date=date(2026, 11, 19),
    )
    assert result.target_date == date(2026, 11, 19)
    by_pair = {row["pairNumber"]: row for row in result.rows}
    assert by_pair[2]["cancelled"] is True
    assert by_pair[3]["newRoom"] == "2к201"


def test_reimport_is_idempotent_and_does_not_touch_other_rows(data_dir: Path, schedule, monkeypatch) -> None:
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        bot_state_file=Path("state.json"),
    )
    current = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    text = read_fixture("replacements-108m.txt")
    monkeypatch.setattr(bot.ReplacementParser, "extract_text", staticmethod(lambda data: text))
    first = current.import_replacements(b"%PDF-1.4 test", "Telegram PDF")
    assert first.changed is True
    assert len(storage.docs["replacements.json"]["items"]) == 2
    stored = copy.deepcopy(storage.docs["replacements.json"]["items"])
    second = current.import_replacements(b"%PDF-1.4 test", "Telegram PDF")
    assert second.changed is False
    assert storage.docs["replacements.json"]["items"] == stored


def test_no_108m_pdf_does_not_publish(data_dir: Path, schedule, monkeypatch) -> None:
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        bot_state_file=Path("state.json"),
    )
    current = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    monkeypatch.setattr(
        bot.ReplacementParser,
        "extract_text",
        staticmethod(lambda data: read_fixture("replacements-no-108m.txt")),
    )
    result = current.import_replacements(b"%PDF-1.4 test", "VK")
    assert result.status == "no-108m"
    assert storage.publish_count == 0
    assert storage.docs["replacements.json"]["items"] == []


def test_vk_finds_dated_post_with_pdf() -> None:
    calls: list[str] = []

    def http(method, url, headers, body, timeout):
        calls.append(url)
        if "wall.get" in url:
            payload = {
                "response": {
                    "items": [
                        {"date": 1700000000, "text": "Просто новость", "attachments": []},
                        {
                            "date": 1700000100,
                            "text": "Замены на 21.11.2026 для 108М",
                            "attachments": [{
                                "type": "doc",
                                "doc": {"ext": "pdf", "title": "zameni.pdf", "url": "https://sun9-1.vkuser.net/zameni.pdf"},
                            }],
                        },
                    ],
                },
            }
            return 200, {}, json.dumps(payload).encode("utf-8")
        raise AssertionError(url)

    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        vk_group_id="123",
        vk_access_token="vk-secret",
    )
    client = bot.VKClient(config, http=http)
    found = client.find_post(date(2026, 11, 21))
    assert found is not None
    assert found[0] == "https://sun9-1.vkuser.net/zameni.pdf"
    assert all("access_token=" in url for url in calls)


def test_vk_skips_wrong_date_and_non_vk_host() -> None:
    def http(method, url, headers, body, timeout):
        if "wall.get" in url:
            payload = {
                "response": {
                    "items": [{
                        "date": 1700000100,
                        "text": "Замены на 20.11.2026",
                        "attachments": [{
                            "type": "doc",
                            "doc": {"ext": "pdf", "title": "x.pdf", "url": "https://evil.example/x.pdf"},
                        }],
                    }],
                },
            }
            return 200, {}, json.dumps(payload).encode("utf-8")
        return 200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 test"

    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        vk_group_id="123",
        vk_access_token="vk-secret",
    )
    client = bot.VKClient(config, http=http)
    assert client.find_post(date(2026, 11, 21)) is None
    with pytest.raises(bot.ExternalServiceError):
        client.download_pdf("https://evil.example/x.pdf")


def test_vk_disabled_returns_manual_instruction(data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        bot_state_file=Path("state.json"),
    )
    current = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    result = current.import_replacements_from_vk(date(2026, 11, 19))
    assert result.status == "vk-disabled"
    assert "вручную" in result.message
    assert storage.publish_count == 0


def test_extract_text_rejects_non_pdf() -> None:
    with pytest.raises(bot.UserFacingError, match="PDF"):
        bot.ReplacementParser.extract_text(b"not a pdf")


def test_scheduler_vk_target_skips_weekend(monkeypatch, data_dir: Path) -> None:
    docs, _ = load_seed(data_dir)
    storage = MemoryStorage(docs)
    config = bot.Config(
        telegram_token="t",
        allowed_telegram_ids=frozenset({1}),
        github_token="g",
        gemini_api_key="x",
        bot_state_file=Path("state.json"),
    )
    current = bot.Backend(config, storage=storage)  # type: ignore[arg-type]
    scheduler = bot.BotScheduler(current)
    seen: list[date] = []

    def fake_import(target):
        seen.append(target)
        return bot.ReplacementImportResult("vk-disabled", target_date=target, message="manual")

    monkeypatch.setattr(current, "import_replacements_from_vk", fake_import)
    import asyncio
    asyncio.run(scheduler.run_vk_import(date(2026, 11, 20)))
    assert seen == [date(2026, 11, 23)]
