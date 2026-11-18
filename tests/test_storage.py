from __future__ import annotations

import base64
import copy
import json
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest

import bot


def make_config(tmp_path: Path, **overrides) -> bot.Config:
    values = dict(
        telegram_token="test-telegram-token",
        allowed_telegram_ids=frozenset({1001}),
        github_token="test-github-token",
        gemini_api_key="test-gemini-key",
        bot_state_file=tmp_path / "state.json",
    )
    values.update(overrides)
    return bot.Config(**values)


def load_seed(data_dir: Path) -> tuple[dict[str, dict], dict]:
    documents = {
        name: json.loads((data_dir / name).read_text(encoding="utf-8"))
        for name in bot.DATA_FILES
    }
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    return documents, manifest


class FakeGitHub:
    """Мини-реализация Contents API с CAS и журналом операций."""

    def __init__(self, documents: dict[str, dict], manifest: dict):
        self.files: dict[str, dict] = {f"data/{name}": copy.deepcopy(value) for name, value in documents.items()}
        self.files["data/manifest.json"] = copy.deepcopy(manifest)
        self.shas: dict[str, str] = {key: self._sha(key) for key in self.files}
        self.put_log: list[str] = []
        self.get_log: list[str] = []
        self.conflict_file: str | None = None
        self.conflict_used = False
        self.fail_after_writes: int | None = None
        self.writes = 0

    @staticmethod
    def _sha(name: str) -> str:
        return "sha-" + bot.stable_hash(name)[:12]

    def _new_sha(self, name: str) -> str:
        self.writes += 1
        return f"sha-{bot.stable_hash(name + str(self.writes))[:20]}"

    def __call__(self, method, url, headers, body, timeout):
        path = unquote(urlparse(url).path)
        name = path.split("/contents/", 1)[-1]
        if method == "GET":
            self.get_log.append(name)
            if name not in self.files:
                return 404, {}, b'{"message":"Not Found"}'
            payload = {
                "content": base64.b64encode(bot.json_dumps_stable(self.files[name]).encode()).decode(),
                "encoding": "base64",
                "sha": self.shas[name],
            }
            return 200, {}, json.dumps(payload).encode()
        if method == "PUT":
            self.put_log.append(name)
            if self.conflict_file == name and not self.conflict_used:
                self.conflict_used = True
                return 409, {}, b'{"message":"sha does not match"}'
            if self.fail_after_writes is not None and self.writes >= self.fail_after_writes:
                return 503, {}, b"{}"
            data = json.loads(body.decode())
            expected = data.get("sha")
            if expected and self.shas.get(name) != expected:
                return 409, {}, b'{"message":"sha does not match"}'
            if name not in self.files:
                return 404, {}, b'{"message":"Not Found"}'
            self.files[name] = json.loads(base64.b64decode(data["content"]).decode())
            self.shas[name] = self._new_sha(name)
            return 200, {}, json.dumps({"content": {"sha": self.shas[name]}}).encode()
        return 405, {}, b"{}"


def make_storage(tmp_path: Path, fake: FakeGitHub, retries: int = 3) -> bot.GitHubStorage:
    return bot.GitHubStorage(
        make_config(tmp_path),
        http=fake,
        api_root="https://api.test/repos/x/contents",
        retries=retries,
        sleeper=lambda delay: None,
    )


def valid_task() -> dict:
    return {
        "taskId": "task-math-000001",
        "subjectId": "math",
        "subject": "Математика",
        "text": "Выучить логарифмы",
        "assignedDate": "2026-11-18",
        "nextLessonDate": "2026-11-19",
        "nextLessonId": "thu-4-history",
        "status": "актуальное",
        "createdAt": "2026-11-18T10:00:00+03:00",
        "updatedAt": "2026-11-18T10:00:00+03:00",
        "sourceUserId": 1001,
    }


def test_publish_writes_common_revision_and_manifest_last(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    fake = FakeGitHub(documents, manifest)
    storage = make_storage(tmp_path, fake)

    def add_task(doc: dict) -> None:
        doc["tasks"].append(valid_task())

    result = storage.publish({"tasks.json": add_task}, "event-task-1", "add homework")
    assert result.changed is True
    assert fake.put_log[-1] == "data/manifest.json"
    assert set(fake.put_log) == {f"data/{name}" for name in bot.DATA_FILES} | {"data/manifest.json"}

    bundle = storage.read_bundle()
    assert bundle.manifest["revision"] == result.revision
    assert all(document["revision"] == result.revision for document in bundle.documents.values())
    assert bundle.documents["tasks.json"]["tasks"][0]["text"] == "Выучить логарифмы"
    assert len(bundle.documents["grades.json"]["grades"]) == 0


def test_noop_publish_does_not_write(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    fake = FakeGitHub(documents, manifest)
    storage = make_storage(tmp_path, fake)

    result = storage.publish({"tasks.json": lambda doc: None}, "event-noop-1", "noop")
    assert result.changed is False
    assert fake.put_log == []
    assert result.revision == manifest["revision"]


def test_publish_recovers_from_cas_conflict(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    fake = FakeGitHub(documents, manifest)
    fake.conflict_file = "data/grades.json"
    storage = make_storage(tmp_path, fake, retries=4)

    def add_grade(doc: dict) -> None:
        doc["grades"].append({
            "gradeId": "grade-math-000001",
            "studentId": "s01",
            "subjectId": "math",
            "subject": "Математика",
            "lessonDate": "2026-11-10",
            "entryType": "daily",
            "value": "5",
            "raw": "5",
            "confidence": 0.96,
            "source": "gemini",
            "sourceHash": "a" * 64,
            "model": "gemini-3.8-flash",
            "promptVersion": bot.PROMPT_VERSION,
            "recognitionSchemaVersion": bot.RECOGNITION_SCHEMA_VERSION,
            "recognisedAt": "2026-11-18T10:00:00+03:00",
            "status": "automatic",
            "evidence": ["a" * 64],
        })

    result = storage.publish({"grades.json": add_grade}, "event-grade-1", "merge grades")
    assert result.changed is True
    assert fake.conflict_used is True
    bundle = storage.read_bundle()
    assert len(bundle.documents["grades.json"]["grades"]) == 1
    assert all(document["revision"] == result.revision for document in bundle.documents.values())


def test_strict_read_detects_partial_publication(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    fake = FakeGitHub(documents, manifest)
    fake.files["data/grades.json"]["revision"] = "newer-revision"
    storage = make_storage(tmp_path, fake)
    with pytest.raises(bot.StorageBusyError):
        storage.read_bundle(strict=True)
    bundle = storage.read_bundle(strict=False)
    assert bundle.consistent is False


def test_missing_optional_file_is_recovered(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    fake = FakeGitHub(documents, manifest)
    del fake.files["data/grades.json"]
    fake.shas.pop("data/grades.json")
    storage = make_storage(tmp_path, fake)
    bundle = storage.read_bundle(strict=True)
    assert bundle.documents["grades.json"]["grades"] == []
    assert bundle.documents["grades.json"]["revision"] == manifest["revision"]


def test_publish_rejects_unknown_file(tmp_path: Path, data_dir: Path) -> None:
    documents, manifest = load_seed(data_dir)
    storage = make_storage(tmp_path, FakeGitHub(documents, manifest))
    with pytest.raises(ValueError):
        storage.publish({"secrets.json": lambda doc: None}, "event-x-1", "bad")


def test_local_state_persists_updates_events_and_images(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = bot.LocalState(path)
    assert state.update_seen(10) is False
    assert state.update_seen(10) is True
    assert state.event_done("evt-1") is False
    state.mark_event("evt-1", "rev-1")
    assert state.event_done("evt-1") is True
    assert state.once("midnight-2026-11-18") is True
    assert state.once("midnight-2026-11-18") is False
    state.mark_image("a" * 64)
    assert state.image_done("a" * 64) is True

    restored = bot.LocalState(path)
    assert restored.update_seen(10) is True
    assert restored.event_done("evt-1") is True
    assert restored.image_done("a" * 64) is True


def test_quota_enforces_rpm_rpd_and_tpm(tmp_path: Path) -> None:
    now = [1_800_000_000.0]
    local = bot.LocalState(tmp_path / "quota.json")
    quota = bot.Quota(local, rpm_limit=5, rpd_limit=20, tpm_limit=100_000, clock=lambda: now[0])
    for _ in range(5):
        quota.reserve(1000)
    with pytest.raises(bot.QuotaExceeded, match="минуту"):
        quota.reserve(1000)

    now[0] += 61
    for _ in range(3):
        for _ in range(5):
            quota.reserve(1000)
        now[0] += 61
    with pytest.raises(bot.QuotaExceeded, match="Дневной"):
        quota.reserve(1000)

    token_local = bot.LocalState(tmp_path / "tokens.json")
    token_quota = bot.Quota(token_local, rpm_limit=50, rpd_limit=50, tpm_limit=1000, clock=lambda: now[0])
    token_quota.reserve(900)
    with pytest.raises(bot.QuotaExceeded, match="размера"):
        token_quota.reserve(200)


def test_config_reports_missing_variables_without_values(tmp_path: Path) -> None:
    with pytest.raises(bot.ConfigError) as error:
        bot.load_config({})
    message = str(error.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "GITHUB_TOKEN" in message
    assert "GEMINI_API_KEY" in message
    assert "ALLOWED_TELEGRAM_IDS" in message


def test_config_parses_allowlist_and_optional_settings(tmp_path: Path) -> None:
    config = bot.load_config({
        "TELEGRAM_BOT_TOKEN": "token",
        "ALLOWED_TELEGRAM_IDS": "1, 2 3",
        "GITHUB_TOKEN": "github",
        "GEMINI_API_KEY": "gemini",
        "VK_GROUP_ID": "-231528643",
        "GEMINI_MODELS": "model-a, model-b",
    })
    assert config.allowed_telegram_ids == frozenset({1, 2, 3})
    assert config.vk_group_id == "231528643"
    assert config.gemini_models == ("model-a", "model-b")


def test_config_rejects_bad_allowlist() -> None:
    with pytest.raises(bot.ConfigError):
        bot.load_config({
            "TELEGRAM_BOT_TOKEN": "token",
            "ALLOWED_TELEGRAM_IDS": "alice",
            "GITHUB_TOKEN": "github",
            "GEMINI_API_KEY": "gemini",
        })


def test_date_label_and_normalization() -> None:
    assert bot.parse_date("2026-11-18") == date(2026, 11, 18)
    assert bot.normalize_text("  Соловьёв   Илья  ") == "соловьев илья"
    assert bot.date_label(date(2026, 9, 5)) == "5.09"
    assert bot.plural_ru(2, "замена", "замены", "замен") == "замены"
