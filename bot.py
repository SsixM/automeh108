"""Backend для группы 108М: Telegram-бот, расписание, ДЗ, дежурства, замены.

Запуск: ``python bot.py``. Секреты читаются только из переменных окружения;
см. ``.env.example``. Файл является единственной точкой входа backend.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import functools
import hashlib
import json
import logging
import os
import random
import re
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:  # Telegram is optional for storage/parser tests.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError:  # pragma: no cover - exercised only without dependencies
    Application = None
    Update = Any
    ContextTypes = Any
    filters = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    Image = None
    ImageOps = None

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None


# =============================================================================
# Константы
# =============================================================================

TZ_NAME = "Europe/Moscow"
TZ = ZoneInfo(TZ_NAME)
UTC = timezone.utc
LOG = logging.getLogger("108m")

DATA_FILES: tuple[str, ...] = (
    "students.json",
    "schedule.json",
    "grades.json",
    "tasks.json",
    "duties.json",
    "replacements.json",
    "reviews.json",
)
MANIFEST_FILE = "manifest.json"
GROUP_NAME = "108М"
VALID_GRADE_VALUES = ("2", "3", "4", "5", "н/б")
ENTRY_TYPES = ("daily", "attendance", "attestation")
GRADE_STATUSES = ("confirmed", "automatic", "pending", "conflict")
TASK_STATUSES = ("актуальное", "выполнено", "просрочено", "перенесено")
DUTY_STATUSES = (
    "selected",
    "awaiting-report",
    "reported",
    "pending",
    "closed",
    "closed-without-confirmation",
)
PROMPT_VERSION = "journal-recognition-v1"
RECOGNITION_SCHEMA_VERSION = "journal-recognition-v1"
DUTY_ALGORITHM_VERSION = "sha256-layered-v2-pairs"
MEDIA_DIR = "media/homework"
MAX_ATTACHMENT_BYTES = 8_388_608
MAX_HOMEWORK_IMAGE_SIDE = 1_600
MAX_PDF_BYTES = 10 * 1024 * 1024
VK_API_VERSION = "5.199"
VK_ALLOWED_HOST_SUFFIXES = (
    "vk.com", "vkuser.net", "userapi.com", "vk-cdn.net", "mycdn.me",
)

DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_BATCH_FILES = 20
BATCH_IDLE_SECONDS = 8.0
BATCH_MAX_AGE_SECONDS = 180.0
GITHUB_RETRIES = 5
GITHUB_TIMEOUT = 30
DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
GROUP_RE = re.compile(r"\b(\d{3})\s*[МM]\b", re.IGNORECASE)
PAIR_RE = re.compile(r"^\s*(?:пара\s*)?([1-7])(?:\s*(?:пара|урок|слот))?\s*[.:)\-—–]?\s*", re.IGNORECASE)
ROOM_RE = re.compile(
    r"(?:каб(?:инет)?\.?\s*)?((?:2\s*к\s*)?\d{2,3}\s*[А-Яа-яA-Za-z]?|[А-Яа-яA-Za-z]\s*\d{2,3})\b",
    re.IGNORECASE,
)
SPACE_RE = re.compile(r"\s+")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# =============================================================================
# Ошибки и конфигурация
# =============================================================================


class ConfigError(Exception):
    """Неверная или неполная конфигурация; сообщение безопасно показать."""


class UserFacingError(Exception):
    """Ошибка, которую можно показать пользователю Telegram."""


class ExternalServiceError(Exception):
    """Внешний сервис недоступен; сообщение без секретов и тела ответа."""


class SchemaError(Exception):
    """Данные не соответствуют схеме."""


class StorageConflict(Exception):
    """CAS-конфликт GitHub; операцию нужно перечитать и повторить."""


class StorageBusyError(Exception):
    """Публикация не завершена: файлы имеют разные revision."""


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_telegram_ids: frozenset[int]
    github_token: str
    gemini_api_key: str
    github_repository: str = "SsixM/automeh108"
    github_branch: str = "main"
    data_dir: str = "data"
    vk_group_id: str | None = None
    vk_access_token: str | None = None
    gemini_models: tuple[str, ...] = DEFAULT_GEMINI_MODELS
    gemini_rpm_limit: int = 5
    gemini_rpd_limit: int = 20
    gemini_tpm_limit: int = 250_000
    bot_state_file: Path = Path("bot_state.local.json")
    media_release_tag: str = "homework-media"
    log_level: str = "INFO"

    @property
    def vk_enabled(self) -> bool:
        return bool(self.vk_group_id and self.vk_access_token)


def _env_text(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = str(env.get(name, default) or "").strip()
    return value


def _env_int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = _env_text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Переменная {name} должна быть целым числом.") from exc
    if value < minimum:
        raise ConfigError(f"Переменная {name} должна быть не меньше {minimum}.")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Прочитать и проверить конфигурацию. Значения секретов не логируются."""
    source = env if env is not None else os.environ
    missing: list[str] = []
    telegram_token = _env_text(source, "TELEGRAM_BOT_TOKEN")
    github_token = _env_text(source, "GITHUB_TOKEN")
    gemini_key = _env_text(source, "GEMINI_API_KEY")
    raw_allowed = _env_text(source, "ALLOWED_TELEGRAM_IDS")
    if not telegram_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not github_token:
        missing.append("GITHUB_TOKEN")
    if not gemini_key:
        missing.append("GEMINI_API_KEY")
    if not raw_allowed:
        missing.append("ALLOWED_TELEGRAM_IDS")
    if missing:
        raise ConfigError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
            + ". Скопируйте .env.example, заполните значения и перезапустите бота."
        )

    allowed: set[int] = set()
    for token in re.split(r"[\s,;]+", raw_allowed):
        if not token:
            continue
        try:
            user_id = int(token)
        except ValueError as exc:
            raise ConfigError("ALLOWED_TELEGRAM_IDS должен содержать только числовые Telegram ID.") from exc
        if user_id <= 0:
            raise ConfigError("ALLOWED_TELEGRAM_IDS: Telegram ID должен быть положительным.")
        allowed.add(user_id)
    if not allowed:
        raise ConfigError("ALLOWED_TELEGRAM_IDS пуст: укажите хотя бы один ID.")

    models_raw = _env_text(source, "GEMINI_MODELS")
    models = tuple(model.strip() for model in models_raw.split(",") if model.strip()) if models_raw else DEFAULT_GEMINI_MODELS
    if not models:
        raise ConfigError("GEMINI_MODELS пуст.")

    vk_group = _env_text(source, "VK_GROUP_ID").lstrip("-")
    if vk_group and not vk_group.isdigit():
        raise ConfigError("VK_GROUP_ID должен быть числовым id сообщества без минуса.")
    vk_token = _env_text(source, "VK_ACCESS_TOKEN")

    return Config(
        telegram_token=telegram_token,
        allowed_telegram_ids=frozenset(allowed),
        github_token=github_token,
        gemini_api_key=gemini_key,
        github_repository=_env_text(source, "GITHUB_REPOSITORY", "SsixM/automeh108"),
        github_branch=_env_text(source, "GITHUB_BRANCH", "main"),
        data_dir=_env_text(source, "DATA_DIR", "data").strip("/") or "data",
        vk_group_id=vk_group or None,
        vk_access_token=vk_token or None,
        gemini_models=models,
        gemini_rpm_limit=_env_int(source, "GEMINI_RPM_LIMIT", 5),
        gemini_rpd_limit=_env_int(source, "GEMINI_RPD_LIMIT", 20),
        gemini_tpm_limit=_env_int(source, "GEMINI_TPM_LIMIT", 250_000, minimum=1000),
        bot_state_file=Path(_env_text(source, "BOT_STATE_FILE", "bot_state.local.json")),
        media_release_tag=_env_text(source, "MEDIA_RELEASE_TAG", "homework-media"),
        log_level=_env_text(source, "LOG_LEVEL", "INFO").upper(),
    )


# =============================================================================
# Общие помощники
# =============================================================================


def now_moscow() -> datetime:
    return datetime.now(TZ)


def today_moscow() -> date:
    return now_moscow().date()


def iso_now() -> str:
    return now_moscow().isoformat(timespec="seconds")


def parse_date(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    return parsed


def safe_date(value: Any) -> date | None:
    try:
        return parse_date(value)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("datetime must be ISO 8601")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return parsed


def date_label(day: date) -> str:
    return f"{day.day}.{day.month:02d}"


def date_label_full(day: date) -> str:
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    return f"{day.day} {months[day.month - 1]}"


def normalize_text(value: Any) -> str:
    text = str(value or "").casefold().replace("ё", "е")
    text = re.sub(r"[^а-яa-z0-9]+", " ", text)
    return SPACE_RE.sub(" ", text).strip()


def normalize_name(value: Any) -> str:
    return normalize_text(value).replace(" ", " ")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def short_hash(value: str, length: int = 16) -> str:
    return stable_hash(value)[:length]


def new_revision() -> str:
    return f"{now_moscow().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"


def json_dumps_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def redact(text: Any, secrets_to_hide: Iterable[str] = ()) -> str:
    result = str(text or "")
    for secret in secrets_to_hide:
        if secret and len(secret) >= 6:
            result = result.replace(secret, "[секрет]")
    result = re.sub(r"(?i)(token|key|authorization|bearer)\s*[:=]\s*\S+", r"\1=[скрыто]", result)
    result = CONTROL_RE.sub(" ", result)
    return result[:300]


def safe_exception(exc: BaseException, secrets_to_hide: Iterable[str] = ()) -> str:
    return redact(f"{type(exc).__name__}: {exc}", secrets_to_hide)


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    absolute = abs(number) % 100
    last = absolute % 10
    if 10 < absolute < 20:
        return many
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def chunk_lines(lines: list[str], limit: int = 3800) -> list[str]:
    """Разбить длинный текст на сообщения Telegram без потери строк."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# =============================================================================
# Строгая валидация опубликованных JSON
# =============================================================================

ROOT_FIELDS = {"schemaVersion", "revision", "generatedAt"}


def _schema(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)


def _object(value: Any, where: str, required: set[str], allowed: set[str] | None = None) -> dict[str, Any]:
    _schema(isinstance(value, dict), f"{where}: ожидался объект")
    result = value  # type: ignore[assignment]
    missing = required - set(result)
    _schema(not missing, f"{where}: отсутствуют поля {', '.join(sorted(missing))}")
    if allowed is not None:
        unknown = set(result) - allowed
        _schema(not unknown, f"{where}: неизвестные поля {', '.join(sorted(unknown))}")
    return result


def _list(value: Any, where: str, min_items: int = 0, max_items: int | None = None) -> list[Any]:
    _schema(isinstance(value, list), f"{where}: ожидался массив")
    items = value  # type: ignore[assignment]
    _schema(len(items) >= min_items, f"{where}: слишком мало элементов")
    if max_items is not None:
        _schema(len(items) <= max_items, f"{where}: слишком много элементов")
    return items


def _string(value: Any, where: str, min_length: int = 0, allow_empty: bool = False) -> str:
    _schema(isinstance(value, str), f"{where}: ожидалась строка")
    text = value  # type: ignore[assignment]
    if not allow_empty:
        _schema(len(text) >= max(min_length, 1), f"{where}: пустая строка")
    else:
        _schema(len(text) >= min_length, f"{where}: слишком короткая строка")
    return text


def _integer(value: Any, where: str, minimum: int | None = None, maximum: int | None = None) -> int:
    _schema(isinstance(value, int) and not isinstance(value, bool), f"{where}: ожидалось целое число")
    if minimum is not None:
        _schema(value >= minimum, f"{where}: значение меньше {minimum}")
    if maximum is not None:
        _schema(value <= maximum, f"{where}: значение больше {maximum}")
    return value


def _number(value: Any, where: str, minimum: float | None = None, maximum: float | None = None) -> float:
    _schema(isinstance(value, (int, float)) and not isinstance(value, bool), f"{where}: ожидалось число")
    number = float(value)
    if minimum is not None:
        _schema(number >= minimum, f"{where}: значение меньше {minimum}")
    if maximum is not None:
        _schema(number <= maximum, f"{where}: значение больше {maximum}")
    return number


def _boolean(value: Any, where: str) -> bool:
    _schema(isinstance(value, bool), f"{where}: ожидалось true/false")
    return value


def _enum(value: Any, where: str, allowed: Iterable[str]) -> str:
    allowed_tuple = tuple(allowed)
    _schema(isinstance(value, str) and value in allowed_tuple, f"{where}: недопустимое значение")
    return value


def _iso_date(value: Any, where: str) -> str:
    _schema(isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), f"{where}: дата должна быть YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError(f"{where}: некорректная дата") from exc
    return value


def _iso_datetime(value: Any, where: str) -> str:
    _schema(isinstance(value, str), f"{where}: ожидалась ISO-дата со временем")
    try:
        parse_datetime(value)
    except ValueError as exc:
        raise SchemaError(f"{where}: некорректная дата-время с часовым поясом") from exc
    return value


def _root(document: Any, where: str, extra_allowed: Iterable[str] = ()) -> dict[str, Any]:
    allowed = set(ROOT_FIELDS) | set(extra_allowed)
    root = _object(document, where, ROOT_FIELDS, allowed)
    _schema(root["schemaVersion"] == 1, f"{where}: schemaVersion должен быть 1")
    _string(root["revision"], f"{where}.revision", min_length=1)
    _iso_datetime(root["generatedAt"], f"{where}.generatedAt")
    return root


def validate_students_document(document: Any) -> None:
    root = _root(document, "students.json", {"group", "students"})
    _schema(root.get("group") == GROUP_NAME, "students.json.group: ожидалась группа 108М")
    students = _list(root.get("students"), "students.json.students", 1, 100)
    ids: set[str] = set()
    names: set[str] = set()
    for index, item in enumerate(students):
        where = f"students.json.students[{index}]"
        student = _object(
            item,
            where,
            {"studentId", "name", "englishGroup", "aliases"},
            {"studentId", "name", "englishGroup", "aliases"},
        )
        student_id = _string(student["studentId"], f"{where}.studentId")
        _schema(bool(re.fullmatch(r"s\d{2}", student_id)), f"{where}.studentId: ожидался sNN")
        _schema(student_id not in ids, f"{where}.studentId: повторяется")
        ids.add(student_id)
        name = _string(student["name"], f"{where}.name", min_length=3)
        _schema(normalize_text(name) not in names, f"{where}.name: повторяется")
        names.add(normalize_text(name))
        _enum(student["englishGroup"], f"{where}.englishGroup", ("2к207", "2к202"))
        aliases = _list(student["aliases"], f"{where}.aliases", 0, 30)
        for alias_index, alias in enumerate(aliases):
            _string(alias, f"{where}.aliases[{alias_index}]", min_length=3)


def validate_schedule_document(document: Any) -> None:
    root = _root(document, "schedule.json", {"meta", "times", "lessons", "replacementRules"})
    meta = _object(
        root.get("meta"),
        "schedule.json.meta",
        {"timezone", "weekAnchor", "firstWeekType", "group"},
        {"timezone", "weekAnchor", "firstWeekType", "group"},
    )
    _schema(meta["timezone"] == TZ_NAME, "schedule.json.meta.timezone: ожидался Europe/Moscow")
    _iso_date(meta["weekAnchor"], "schedule.json.meta.weekAnchor")
    _enum(meta["firstWeekType"], "schedule.json.meta.firstWeekType", ("numerator", "denominator"))
    _schema(meta["group"] == GROUP_NAME, "schedule.json.meta.group: ожидалась группа 108М")

    times = _list(root.get("times"), "schedule.json.times", 1, 7)
    pairs: set[int] = set()
    for index, item in enumerate(times):
        where = f"schedule.json.times[{index}]"
        entry = _object(item, where, {"pairNumber", "start", "end"}, {"pairNumber", "start", "end"})
        pair = _integer(entry["pairNumber"], f"{where}.pairNumber", 1, 7)
        _schema(pair not in pairs, f"{where}.pairNumber: повторяется")
        pairs.add(pair)
        for key in ("start", "end"):
            clock = _string(entry[key], f"{where}.{key}")
            _schema(bool(re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)), f"{where}.{key}: ожидалось HH:MM")

    lessons = _list(root.get("lessons"), "schedule.json.lessons", 1, 500)
    lesson_ids: set[str] = set()
    lesson_keys: set[tuple[int, int, str, str]] = set()
    for index, item in enumerate(lessons):
        where = f"schedule.json.lessons[{index}]"
        lesson = _object(
            item,
            where,
            {"lessonId", "weekday", "pairNumber", "subjectId", "subject", "teacher", "room", "subgroup", "weekType", "service"},
            {
                "lessonId", "weekday", "pairNumber", "subjectId", "subject", "teacher", "room",
                "subgroup", "subgroupOptions", "weekType", "service", "validFrom", "validTo",
            },
        )
        lesson_id = _string(lesson["lessonId"], f"{where}.lessonId", min_length=3)
        _schema(lesson_id not in lesson_ids, f"{where}.lessonId: повторяется")
        lesson_ids.add(lesson_id)
        weekday = _integer(lesson["weekday"], f"{where}.weekday", 1, 5)
        pair = _integer(lesson["pairNumber"], f"{where}.pairNumber", 1, 7)
        subject_id = _string(lesson["subjectId"], f"{where}.subjectId", min_length=2)
        _string(lesson["subject"], f"{where}.subject", min_length=2)
        _string(lesson["teacher"], f"{where}.teacher", allow_empty=True)
        _string(lesson["room"], f"{where}.room", allow_empty=True)
        subgroup = _enum(lesson["subgroup"], f"{where}.subgroup", ("all", "split", "2к207", "2к202"))
        week_type = _enum(lesson["weekType"], f"{where}.weekType", ("all", "numerator", "denominator"))
        _boolean(lesson["service"], f"{where}.service")
        key = (weekday, pair, subject_id, subgroup)
        _schema(key not in lesson_keys, f"{where}: урок повторяется для той же пары")
        lesson_keys.add(key)
        if subgroup == "split":
            options = _list(lesson.get("subgroupOptions"), f"{where}.subgroupOptions", 2, 2)
            option_ids: set[str] = set()
            for option_index, option in enumerate(options):
                option_where = f"{where}.subgroupOptions[{option_index}]"
                value = _object(option, option_where, {"id", "teacher", "room"}, {"id", "teacher", "room"})
                option_id = _enum(value["id"], f"{option_where}.id", ("2к207", "2к202"))
                _schema(option_id not in option_ids, f"{option_where}.id: повторяется")
                option_ids.add(option_id)
                _string(value["teacher"], f"{option_where}.teacher", allow_empty=True)
                _string(value["room"], f"{option_where}.room", allow_empty=True)
            _schema(option_ids == {"2к207", "2к202"}, f"{where}.subgroupOptions: нужны обе подгруппы")
        for key_name in ("validFrom", "validTo"):
            if key_name in lesson:
                _iso_date(lesson[key_name], f"{where}.{key_name}")

    rules = _list(root.get("replacementRules"), "schedule.json.replacementRules", 0, 100)
    rule_ids: set[str] = set()
    for index, item in enumerate(rules):
        where = f"schedule.json.replacementRules[{index}]"
        rule = _object(
            item,
            where,
            {"ruleId", "weekday", "pairNumber", "weekType", "originalLessonId", "newSubjectId", "newSubject", "newRoom"},
            {
                "ruleId", "weekday", "pairNumber", "weekType", "originalLessonId", "newSubjectId",
                "newSubject", "newTeacher", "newRoom", "validFrom", "validTo",
            },
        )
        rule_id = _string(rule["ruleId"], f"{where}.ruleId", min_length=3)
        _schema(rule_id not in rule_ids, f"{where}.ruleId: повторяется")
        rule_ids.add(rule_id)
        _integer(rule["weekday"], f"{where}.weekday", 1, 5)
        _integer(rule["pairNumber"], f"{where}.pairNumber", 1, 7)
        _enum(rule["weekType"], f"{where}.weekType", ("all", "numerator", "denominator"))
        _schema(rule["originalLessonId"] in lesson_ids, f"{where}.originalLessonId: урок не найден")
        _string(rule["newSubjectId"], f"{where}.newSubjectId", min_length=2)
        _string(rule["newSubject"], f"{where}.newSubject", min_length=2)
        if "newTeacher" in rule:
            _string(rule["newTeacher"], f"{where}.newTeacher", allow_empty=True)
        _string(rule["newRoom"], f"{where}.newRoom", allow_empty=True)
        for key_name in ("validFrom", "validTo"):
            if key_name in rule:
                _iso_date(rule[key_name], f"{where}.{key_name}")


def validate_grades_document(document: Any) -> None:
    root = _root(document, "grades.json", {"grades"})
    grades = _list(root.get("grades"), "grades.json.grades", 0, 50_000)
    ids: set[str] = set()
    keys: set[tuple[str, str, str, str]] = set()
    for index, item in enumerate(grades):
        where = f"grades.json.grades[{index}]"
        grade = _object(
            item,
            where,
            {
                "gradeId", "studentId", "subjectId", "subject", "lessonDate", "entryType",
                "value", "raw", "confidence", "source", "status", "evidence",
            },
            {
                "gradeId", "studentId", "subjectId", "subject", "lessonDate", "entryType",
                "value", "raw", "confidence", "source", "sourceHash", "model", "promptVersion",
                "recognitionSchemaVersion", "recognisedAt", "status", "evidence", "confirmedBy",
                "confirmedAt", "note",
            },
        )
        grade_id = _string(grade["gradeId"], f"{where}.gradeId", min_length=8)
        _schema(grade_id not in ids, f"{where}.gradeId: повторяется")
        ids.add(grade_id)
        student_id = _string(grade["studentId"], f"{where}.studentId")
        _schema(bool(re.fullmatch(r"s\d{2}", student_id)), f"{where}.studentId: ожидался sNN")
        subject_id = _string(grade["subjectId"], f"{where}.subjectId", min_length=2)
        _string(grade["subject"], f"{where}.subject", min_length=2)
        lesson_date = _iso_date(grade["lessonDate"], f"{where}.lessonDate")
        entry_type = _enum(grade["entryType"], f"{where}.entryType", ENTRY_TYPES)
        _enum(grade["value"], f"{where}.value", VALID_GRADE_VALUES)
        _string(grade["raw"], f"{where}.raw", allow_empty=True)
        _number(grade["confidence"], f"{where}.confidence", 0, 1)
        _string(grade["source"], f"{where}.source", min_length=2)
        _enum(grade["status"], f"{where}.status", GRADE_STATUSES)
        evidence = _list(grade["evidence"], f"{where}.evidence", 0, 100)
        for evidence_index, evidence_item in enumerate(evidence):
            _string(evidence_item, f"{where}.evidence[{evidence_index}]", min_length=8)
        for optional in ("sourceHash", "model", "promptVersion", "recognitionSchemaVersion", "note"):
            if optional in grade:
                _string(grade[optional], f"{where}.{optional}", allow_empty=True)
        if "recognisedAt" in grade:
            _iso_datetime(grade["recognisedAt"], f"{where}.recognisedAt")
        if "confirmedAt" in grade:
            _iso_datetime(grade["confirmedAt"], f"{where}.confirmedAt")
        if "confirmedBy" in grade:
            _integer(grade["confirmedBy"], f"{where}.confirmedBy", 1)
        key = (student_id, subject_id, lesson_date, entry_type)
        _schema(key not in keys, f"{where}: оценка с таким ключом уже есть")
        keys.add(key)


def validate_tasks_document(document: Any) -> None:
    root = _root(document, "tasks.json", {"tasks"})
    tasks = _list(root.get("tasks"), "tasks.json.tasks", 0, 5000)
    ids: set[str] = set()
    for index, item in enumerate(tasks):
        where = f"tasks.json.tasks[{index}]"
        task = _object(
            item,
            where,
            {
                "taskId", "subjectId", "subject", "text", "assignedDate", "nextLessonDate",
                "nextLessonId", "status", "createdAt", "updatedAt", "sourceUserId",
            },
            {
                "taskId", "subjectId", "subject", "text", "assignedDate", "nextLessonDate",
                "nextLessonId", "status", "createdAt", "updatedAt", "sourceUserId",
                "sourceUpdateId", "originalNextLessonDate", "movedByReplacement", "completedAt",
                "attachments",
            },
        )
        task_id = _string(task["taskId"], f"{where}.taskId", min_length=8)
        _schema(task_id not in ids, f"{where}.taskId: повторяется")
        ids.add(task_id)
        _string(task["subjectId"], f"{where}.subjectId", min_length=2)
        _string(task["subject"], f"{where}.subject", min_length=2)
        _string(task["text"], f"{where}.text", min_length=1)
        _iso_date(task["assignedDate"], f"{where}.assignedDate")
        _iso_date(task["nextLessonDate"], f"{where}.nextLessonDate")
        _string(task["nextLessonId"], f"{where}.nextLessonId", min_length=3)
        _enum(task["status"], f"{where}.status", TASK_STATUSES)
        _iso_datetime(task["createdAt"], f"{where}.createdAt")
        _iso_datetime(task["updatedAt"], f"{where}.updatedAt")
        _integer(task["sourceUserId"], f"{where}.sourceUserId", 1)
        if "sourceUpdateId" in task:
            _integer(task["sourceUpdateId"], f"{where}.sourceUpdateId", 0)
        if "originalNextLessonDate" in task:
            _iso_date(task["originalNextLessonDate"], f"{where}.originalNextLessonDate")
        if "movedByReplacement" in task:
            _boolean(task["movedByReplacement"], f"{where}.movedByReplacement")
        if "completedAt" in task:
            _iso_datetime(task["completedAt"], f"{where}.completedAt")
        if "attachments" in task:
            attachments = _list(task["attachments"], f"{where}.attachments", 0, 6)
            attachment_ids: set[str] = set()
            for attachment_index, item in enumerate(attachments):
                attachment_where = f"{where}.attachments[{attachment_index}]"
                attachment = _object(
                    item,
                    attachment_where,
                    {"attachmentId", "url", "fileName", "mimeType", "size", "sha256", "createdAt"},
                    {
                        "attachmentId", "url", "fileName", "mimeType", "size", "sha256",
                        "createdAt", "width", "height",
                    },
                )
                attachment_id = _string(attachment["attachmentId"], f"{attachment_where}.attachmentId", min_length=8)
                _schema(attachment_id not in attachment_ids, f"{attachment_where}.attachmentId: повторяется")
                attachment_ids.add(attachment_id)
                url = _string(attachment["url"], f"{attachment_where}.url", min_length=12)
                _schema(url.startswith("https://"), f"{attachment_where}.url: нужен https")
                _string(attachment["fileName"], f"{attachment_where}.fileName", min_length=1)
                _schema(len(attachment["fileName"]) <= 120, f"{attachment_where}.fileName: слишком длинное")
                _enum(attachment["mimeType"], f"{attachment_where}.mimeType", ("image/jpeg", "image/png", "image/webp"))
                _integer(attachment["size"], f"{attachment_where}.size", 1, 8_388_608)
                sha = _string(attachment["sha256"], f"{attachment_where}.sha256", min_length=64)
                _schema(bool(re.fullmatch(r"[a-f0-9]{64}", sha)), f"{attachment_where}.sha256: ожидался hex")
                _iso_datetime(attachment["createdAt"], f"{attachment_where}.createdAt")
                if "width" in attachment:
                    _integer(attachment["width"], f"{attachment_where}.width", 1, 20_000)
                if "height" in attachment:
                    _integer(attachment["height"], f"{attachment_where}.height", 1, 20_000)


def _validate_student_id_array(value: Any, where: str, min_items: int = 0) -> list[str]:
    items = _list(value, where, min_items, 50)
    result: list[str] = []
    for index, item in enumerate(items):
        student_id = _string(item, f"{where}[{index}]")
        _schema(bool(re.fullmatch(r"s\d{2}", student_id)), f"{where}[{index}]: ожидался sNN")
        _schema(student_id not in result, f"{where}: повторяется {student_id}")
        result.append(student_id)
    return result


def validate_duties_document(document: Any) -> None:
    root = _root(document, "duties.json", {"group", "students", "days", "pairPreferences"})
    _schema(root.get("group") == GROUP_NAME, "duties.json.group: ожидалась группа 108М")
    preferences = _list(root.get("pairPreferences", []), "duties.json.pairPreferences", 0, 200)
    seen_pairs: set[tuple[str, str]] = set()
    for index, pair in enumerate(preferences):
        where = f"duties.json.pairPreferences[{index}]"
        items = _list(pair, where, 2, 2)
        student_ids = [_string(item, f"{where}[{item_index}]", min_length=3) for item_index, item in enumerate(items)]
        for student_id in student_ids:
            _schema(bool(re.fullmatch(r"s\d{2}", student_id)), f"{where}: ожидался studentId вида sNN")
        if len(set(student_ids)) != 2:
            raise SchemaError(f"{where}: нужны два разных ученика")
        key = tuple(sorted(student_ids))
        if key in seen_pairs:
            raise SchemaError(f"{where}: пара повторяется")
        seen_pairs.add(key)
    records = _object(root.get("students"), "duties.json.students", set(), None)
    for student_id, record in records.items():
        _schema(bool(re.fullmatch(r"s\d{2}", str(student_id))), "duties.json.students: ключ должен быть sNN")
        where = f"duties.json.students.{student_id}"
        value = _object(record, where, {"name", "count"}, {"name", "count"})
        _string(value["name"], f"{where}.name", min_length=3)
        _integer(value["count"], f"{where}.count", 0)
    days = _list(root.get("days"), "duties.json.days", 0, 5000)
    dates: set[str] = set()
    for index, item in enumerate(days):
        where = f"duties.json.days[{index}]"
        day = _object(
            item,
            where,
            {
                "date", "selected", "selectedNames", "actual", "actualNames", "reports",
                "status", "seed", "algorithmVersion", "closed",
            },
            {
                "date", "selected", "selectedNames", "actual", "actualNames", "reports",
                "status", "seed", "algorithmVersion", "closed", "closedAt",
            },
        )
        day_date = _iso_date(day["date"], f"{where}.date")
        _schema(day_date not in dates, f"{where}.date: повторяется")
        dates.add(day_date)
        selected = _validate_student_id_array(day["selected"], f"{where}.selected", 2)
        _schema(len(selected) == 2, f"{where}.selected: ожидалось два дежурных")
        selected_names = _list(day["selectedNames"], f"{where}.selectedNames", 2, 2)
        for name_index, name in enumerate(selected_names):
            _string(name, f"{where}.selectedNames[{name_index}]", min_length=3)
        _validate_student_id_array(day["actual"], f"{where}.actual", 0)
        actual_names = _list(day["actualNames"], f"{where}.actualNames", 0, 50)
        for name_index, name in enumerate(actual_names):
            _string(name, f"{where}.actualNames[{name_index}]", min_length=3)
        reports = _list(day["reports"], f"{where}.reports", 0, 200)
        for report_index, report_item in enumerate(reports):
            report_where = f"{where}.reports[{report_index}]"
            report = _object(
                report_item,
                report_where,
                {"reportId", "userId", "receivedAt", "status", "studentIds", "studentNames", "ambiguous", "messageHash"},
                {"reportId", "userId", "receivedAt", "status", "studentIds", "studentNames", "ambiguous", "messageHash"},
            )
            _string(report["reportId"], f"{report_where}.reportId", min_length=8)
            _integer(report["userId"], f"{report_where}.userId", 1)
            _iso_datetime(report["receivedAt"], f"{report_where}.receivedAt")
            _enum(report["status"], f"{report_where}.status", ("confirmed", "pending"))
            _validate_student_id_array(report["studentIds"], f"{report_where}.studentIds", 0)
            report_names = _list(report["studentNames"], f"{report_where}.studentNames", 0, 50)
            for name_index, name in enumerate(report_names):
                _string(name, f"{report_where}.studentNames[{name_index}]", min_length=3)
            ambiguous = _list(report["ambiguous"], f"{report_where}.ambiguous", 0, 50)
            for ambiguous_index, name in enumerate(ambiguous):
                _string(name, f"{report_where}.ambiguous[{ambiguous_index}]", min_length=1)
            _string(report["messageHash"], f"{report_where}.messageHash", min_length=16)
        _enum(day["status"], f"{where}.status", DUTY_STATUSES)
        _string(day["seed"], f"{where}.seed", min_length=16)
        _string(day["algorithmVersion"], f"{where}.algorithmVersion", min_length=3)
        _boolean(day["closed"], f"{where}.closed")
        if "closedAt" in day:
            _iso_datetime(day["closedAt"], f"{where}.closedAt")


def validate_replacements_document(document: Any) -> None:
    root = _root(document, "replacements.json", {"items"})
    items = _list(root.get("items"), "replacements.json.items", 0, 5000)
    ids: set[str] = set()
    for index, item in enumerate(items):
        where = f"replacements.json.items[{index}]"
        replacement = _object(
            item,
            where,
            {
                "replacementId", "date", "weekType", "pairNumber", "newSubjectId", "newSubject",
                "newRoom", "cancelled", "source", "sourceHash", "receivedAt", "group",
            },
            {
                "replacementId", "date", "weekType", "pairNumber", "originalLessonId",
                "originalSubject", "newSubjectId", "newSubject", "newTeacher", "newRoom",
                "cancelled", "source", "sourceUrl", "sourceHash", "receivedAt", "group",
            },
        )
        replacement_id = _string(replacement["replacementId"], f"{where}.replacementId", min_length=8)
        _schema(replacement_id not in ids, f"{where}.replacementId: повторяется")
        ids.add(replacement_id)
        _iso_date(replacement["date"], f"{where}.date")
        _enum(replacement["weekType"], f"{where}.weekType", ("numerator", "denominator"))
        _integer(replacement["pairNumber"], f"{where}.pairNumber", 1, 7)
        _string(replacement["newSubjectId"], f"{where}.newSubjectId", min_length=2)
        _string(replacement["newSubject"], f"{where}.newSubject", min_length=2)
        _string(replacement["newRoom"], f"{where}.newRoom", allow_empty=True)
        _boolean(replacement["cancelled"], f"{where}.cancelled")
        _string(replacement["source"], f"{where}.source", min_length=2)
        _string(replacement["sourceHash"], f"{where}.sourceHash", min_length=16)
        _iso_datetime(replacement["receivedAt"], f"{where}.receivedAt")
        _schema(replacement["group"] == GROUP_NAME, f"{where}.group: ожидалась группа 108М")
        for optional in ("originalLessonId", "originalSubject", "newTeacher", "sourceUrl"):
            if optional in replacement:
                _string(replacement[optional], f"{where}.{optional}", allow_empty=True)


def _validate_candidate(candidate: Any, where: str) -> None:
    value = _object(
        candidate,
        where,
        {
            "gradeId", "studentId", "subjectId", "subject", "lessonDate", "entryType",
            "value", "raw", "confidence", "source", "status", "evidence",
        },
        {
            "gradeId", "studentId", "subjectId", "subject", "lessonDate", "entryType",
            "value", "raw", "confidence", "source", "sourceHash", "model", "promptVersion",
            "recognitionSchemaVersion", "recognisedAt", "status", "evidence",
        },
    )
    _string(value["gradeId"], f"{where}.gradeId", min_length=8)
    _schema(bool(re.fullmatch(r"s\d{2}", _string(value["studentId"], f"{where}.studentId"))), f"{where}.studentId: ожидался sNN")
    _string(value["subjectId"], f"{where}.subjectId", min_length=2)
    _string(value["subject"], f"{where}.subject", min_length=2)
    _iso_date(value["lessonDate"], f"{where}.lessonDate")
    _enum(value["entryType"], f"{where}.entryType", ENTRY_TYPES)
    _enum(value["value"], f"{where}.value", VALID_GRADE_VALUES)
    _string(value["raw"], f"{where}.raw", allow_empty=True)
    _number(value["confidence"], f"{where}.confidence", 0, 1)
    _string(value["source"], f"{where}.source", min_length=2)
    _enum(value["status"], f"{where}.status", GRADE_STATUSES)
    evidence = _list(value["evidence"], f"{where}.evidence", 0, 100)
    for index, item in enumerate(evidence):
        _string(item, f"{where}.evidence[{index}]", min_length=8)


def validate_reviews_document(document: Any) -> None:
    root = _root(document, "reviews.json", {"items"})
    items = _list(root.get("items"), "reviews.json.items", 0, 5000)
    ids: set[str] = set()
    for index, item in enumerate(items):
        where = f"reviews.json.items[{index}]"
        review = _object(
            item,
            where,
            {
                "reviewId", "gradeId", "studentId", "subjectId", "subject", "lessonDate",
                "entryType", "oldValue", "newValue", "oldConfidence", "newConfidence",
                "reason", "status", "createdAt", "candidate",
            },
            {
                "reviewId", "gradeId", "studentId", "subjectId", "subject", "lessonDate",
                "entryType", "oldValue", "newValue", "oldConfidence", "newConfidence",
                "reason", "status", "createdAt", "resolvedAt", "resolvedBy", "candidate",
            },
        )
        review_id = _string(review["reviewId"], f"{where}.reviewId", min_length=8)
        _schema(review_id not in ids, f"{where}.reviewId: повторяется")
        ids.add(review_id)
        _string(review["gradeId"], f"{where}.gradeId", min_length=8)
        _schema(bool(re.fullmatch(r"s\d{2}", _string(review["studentId"], f"{where}.studentId"))), f"{where}.studentId: ожидался sNN")
        _string(review["subjectId"], f"{where}.subjectId", min_length=2)
        _string(review["subject"], f"{where}.subject", min_length=2)
        _iso_date(review["lessonDate"], f"{where}.lessonDate")
        _enum(review["entryType"], f"{where}.entryType", ENTRY_TYPES)
        _enum(review["oldValue"], f"{where}.oldValue", VALID_GRADE_VALUES)
        _enum(review["newValue"], f"{where}.newValue", VALID_GRADE_VALUES)
        _number(review["oldConfidence"], f"{where}.oldConfidence", 0, 1)
        _number(review["newConfidence"], f"{where}.newConfidence", 0, 1)
        _string(review["reason"], f"{where}.reason", min_length=1)
        _enum(review["status"], f"{where}.status", ("pending", "confirmed", "rejected"))
        _iso_datetime(review["createdAt"], f"{where}.createdAt")
        if "resolvedAt" in review:
            _iso_datetime(review["resolvedAt"], f"{where}.resolvedAt")
        if "resolvedBy" in review:
            _integer(review["resolvedBy"], f"{where}.resolvedBy", 1)
        _validate_candidate(review["candidate"], f"{where}.candidate")


def validate_manifest_document(document: Any) -> None:
    root = _object(document, MANIFEST_FILE, ROOT_FIELDS | {"files"}, ROOT_FIELDS | {"files"})
    _schema(root["schemaVersion"] == 1, "manifest.json: schemaVersion должен быть 1")
    revision = _string(root["revision"], "manifest.json.revision", min_length=1)
    _iso_datetime(root["generatedAt"], "manifest.json.generatedAt")
    files = _object(root["files"], "manifest.json.files", set(DATA_FILES), set(DATA_FILES))
    for name in DATA_FILES:
        _schema(files[name] == revision, f"manifest.json.files.{name}: revision не совпадает с manifest.revision")


VALIDATORS: dict[str, Callable[[Any], None]] = {
    "students.json": validate_students_document,
    "schedule.json": validate_schedule_document,
    "grades.json": validate_grades_document,
    "tasks.json": validate_tasks_document,
    "duties.json": validate_duties_document,
    "replacements.json": validate_replacements_document,
    "reviews.json": validate_reviews_document,
    MANIFEST_FILE: validate_manifest_document,
}


def validate_document(name: str, document: Any) -> None:
    validator = VALIDATORS.get(name)
    if validator is None:
        raise SchemaError(f"Неизвестный файл данных: {name}")
    validator(document)


def empty_document(name: str, revision: str = "bootstrap") -> dict[str, Any]:
    """Пустой документ для отсутствующего необязательного файла."""
    now = iso_now()
    base = {"schemaVersion": 1, "revision": revision, "generatedAt": now}
    if name == "grades.json":
        return {**base, "grades": []}
    if name == "tasks.json":
        return {**base, "tasks": []}
    if name == "duties.json":
        return {**base, "group": GROUP_NAME, "students": {}, "days": [], "pairPreferences": []}
    if name == "replacements.json":
        return {**base, "items": []}
    if name == "reviews.json":
        return {**base, "items": []}
    raise SchemaError(f"Нельзя создать пустой обязательный файл {name}")


def validate_bundle(documents: Mapping[str, dict[str, Any]], manifest: dict[str, Any]) -> None:
    """Проверить набор файлов целиком, включая ссылки на учеников."""
    validate_manifest_document(manifest)
    revision = manifest["revision"]
    for name in DATA_FILES:
        if name not in documents:
            raise SchemaError(f"В публикации нет {name}")
        validate_document(name, documents[name])
        if documents[name]["revision"] != manifest["files"][name]:
            raise StorageBusyError(f"{name}: revision не совпадает с manifest.json")
        if documents[name]["revision"] != revision:
            raise StorageBusyError("Файлы опубликованы с разными revision")
    student_ids = {student["studentId"] for student in documents["students.json"]["students"]}
    for name in ("grades.json", "reviews.json"):
        for item in documents[name].get("grades", documents[name].get("items", [])):
            if item["studentId"] not in student_ids:
                raise SchemaError(f"{name}: неизвестный studentId")
    for pair in documents["duties.json"].get("pairPreferences", []):
        for student_id in pair:
            if student_id not in student_ids:
                raise SchemaError("duties.json: неизвестный studentId в паре дежурств")
    for day in documents["duties.json"]["days"]:
        for student_id in list(day["selected"]) + list(day["actual"]):
            if student_id not in student_ids:
                raise SchemaError("duties.json: неизвестный studentId")
        for report in day["reports"]:
            for student_id in report["studentIds"]:
                if student_id not in student_ids:
                    raise SchemaError("duties.json: неизвестный studentId в отчёте")


# =============================================================================
# Локальное состояние и квоты
# =============================================================================


class StorageError(Exception):
    """Хранилище недоступно или содержит повреждённые данные."""


class LocalState:
    """Небольшое локальное состояние процесса; не публикуется в Git."""

    MAX_UPDATES = 5000
    MAX_EVENTS = 3000
    MAX_IMAGES = 3000

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            value = json.loads(raw)
            self.data = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.data = {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temporary.write_text(json_dumps_stable(self.data), encoding="utf-8")
        os.replace(temporary, self.path)

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return clone_json(self.data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self.lock:
            self.data[key] = clone_json(value)
            self._save_locked()

    def update(self, key: str, function: Callable[[Any], Any], default: Any = None) -> Any:
        with self.lock:
            value = function(self.data.get(key, default))
            self.data[key] = value
            self._save_locked()
            return clone_json(value)

    @staticmethod
    def _int_list(value: Any) -> list[int]:
        result: list[int] = []
        for item in value if isinstance(value, list) else []:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed not in result:
                result.append(parsed)
        return result

    def update_seen(self, update_id: int | None) -> bool:
        if update_id is None:
            return False
        with self.lock:
            seen = self._int_list(self.data.get("processedUpdates", []))
            if update_id in seen:
                return True
            seen.append(update_id)
            self.data["processedUpdates"] = seen[-self.MAX_UPDATES:]
            self._save_locked()
            return False

    def has_update(self, update_id: int | None) -> bool:
        if update_id is None:
            return False
        with self.lock:
            return update_id in self._int_list(self.data.get("processedUpdates", []))

    def mark_update(self, update_id: int | None) -> None:
        if update_id is None:
            return
        with self.lock:
            seen = self._int_list(self.data.get("processedUpdates", []))
            if update_id not in seen:
                seen.append(update_id)
                self.data["processedUpdates"] = seen[-self.MAX_UPDATES:]
                self._save_locked()

    def event_done(self, event_id: str) -> bool:
        with self.lock:
            events = self.data.get("processedEvents", {})
            return bool(isinstance(events, dict) and event_id in events)

    def mark_event(self, event_id: str, revision: str = "") -> None:
        with self.lock:
            events = self.data.get("processedEvents", {})
            if not isinstance(events, dict):
                events = {}
            events[event_id] = {"at": iso_now(), "revision": revision}
            if len(events) > self.MAX_EVENTS:
                ordered = sorted(events.items(), key=lambda item: str(item[1].get("at", "")))
                events = dict(ordered[-self.MAX_EVENTS:])
            self.data["processedEvents"] = events
            self._save_locked()

    def once(self, key: str) -> bool:
        """Вернуть True только при первом вызове с данным ключом."""
        with self.lock:
            keys = self.data.get("onceKeys", {})
            if not isinstance(keys, dict):
                keys = {}
            if key in keys:
                return False
            keys[key] = iso_now()
            if len(keys) > 2000:
                ordered = sorted(keys.items(), key=lambda item: str(item[1]))
                keys = dict(ordered[-2000:])
            self.data["onceKeys"] = keys
            self._save_locked()
            return True

    def image_done(self, image_hash: str) -> bool:
        with self.lock:
            images = self.data.get("processedImages", [])
            return isinstance(images, list) and image_hash in images

    def mark_image(self, image_hash: str) -> None:
        with self.lock:
            images = self.data.get("processedImages", [])
            if not isinstance(images, list):
                images = []
            if image_hash not in images:
                images.append(image_hash)
            self.data["processedImages"] = images[-self.MAX_IMAGES:]
            self._save_locked()


class QuotaExceeded(UserFacingError):
    pass


class Quota:
    """Локальный предохранитель для лимитов Gemini."""

    def __init__(
        self,
        local: LocalState,
        rpm_limit: int = 5,
        rpd_limit: int = 20,
        tpm_limit: int = 250_000,
        clock: Callable[[], float] = time.time,
    ):
        self.local = local
        self.rpm_limit = rpm_limit
        self.rpd_limit = rpd_limit
        self.tpm_limit = tpm_limit
        self.clock = clock
        self.lock = threading.RLock()

    def _day_key(self, timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, TZ).date().isoformat()

    def _prune_locked(self, timestamp: float) -> tuple[list[float], list[dict[str, Any]], int]:
        minute = [
            float(item)
            for item in self.local.data.get("geminiMinute", [])
            if isinstance(item, (int, float)) and timestamp - float(item) < 60
        ]
        tokens = [
            {"time": float(item.get("time", 0)), "tokens": int(item.get("tokens", 0))}
            for item in self.local.data.get("geminiTokenEvents", [])
            if isinstance(item, dict) and timestamp - float(item.get("time", 0)) < 60
        ]
        day = self._day_key(timestamp)
        if self.local.data.get("geminiDay") != day:
            self.local.data["geminiDay"] = day
            self.local.data["geminiDayCount"] = 0
        count = int(self.local.data.get("geminiDayCount", 0) or 0)
        return minute, tokens, count

    def reserve(self, estimated_tokens: int = 20_000) -> None:
        estimated = max(1, int(estimated_tokens))
        with self.lock:
            timestamp = float(self.clock())
            with self.local.lock:
                minute, tokens, count = self._prune_locked(timestamp)
                used_tokens = sum(item["tokens"] for item in tokens)
                if len(minute) >= self.rpm_limit:
                    raise QuotaExceeded("Лимит Gemini: 5 запросов в минуту исчерпан. Фото не потеряно — повторите через минуту.")
                if count >= self.rpd_limit:
                    raise QuotaExceeded("Дневной лимит Gemini исчерпан. Обработка возобновится после полуночи по Москве.")
                if used_tokens + estimated > self.tpm_limit:
                    raise QuotaExceeded("Минутный лимит размера запросов Gemini исчерпан. Повторите через минуту.")
                minute.append(timestamp)
                tokens.append({"time": timestamp, "tokens": estimated})
                self.local.data["geminiMinute"] = minute
                self.local.data["geminiTokenEvents"] = tokens
                self.local.data["geminiDayCount"] = count + 1
            self.local.save()

    def status(self) -> dict[str, Any]:
        with self.lock:
            timestamp = float(self.clock())
            with self.local.lock:
                minute, tokens, count = self._prune_locked(timestamp)
                self.local.save()
                return {
                    "rpm_used": len(minute),
                    "rpm_limit": self.rpm_limit,
                    "rpd_used": count,
                    "rpd_limit": self.rpd_limit,
                    "tpm_used": sum(item["tokens"] for item in tokens),
                    "tpm_limit": self.tpm_limit,
                    "day": self._day_key(timestamp),
                }


# =============================================================================
# GitHub Contents API с CAS и общей revision
# =============================================================================


@dataclass
class Bundle:
    documents: dict[str, dict[str, Any]]
    shas: dict[str, str | None]
    manifest: dict[str, Any]
    manifest_sha: str | None
    consistent: bool


@dataclass
class PublishResult:
    revision: str
    changed: bool
    written: tuple[str, ...] = ()


def _request_with_urllib(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int,
) -> tuple[int, dict[str, str], bytes]:
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), dict(response.headers.items()), response.read()
    except HTTPError as exc:
        try:
            payload = exc.read(65536)
        except Exception:
            payload = b""
        return int(exc.code), dict(exc.headers.items()) if exc.headers else {}, payload
    except (URLError, TimeoutError, OSError) as exc:
        raise ExternalServiceError("Сетевой запрос к GitHub не выполнен.") from exc


class GitHubStorage:
    """Единственный слой записи в GitHub.

    Публикация пишет все файлы с одной revision, manifest — последним.
    При CAS-конфликте файлы перечитываются, изменения применяются заново.
    """

    def __init__(
        self,
        config: Config,
        http: Callable[[str, str, dict[str, str], bytes | None, int], tuple[int, dict[str, str], bytes]] | None = None,
        api_root: str | None = None,
        retries: int = GITHUB_RETRIES,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.http = http or _request_with_urllib
        self.api_root = (api_root or f"https://api.github.com/repos/{config.github_repository}/contents").rstrip("/")
        self.retries = max(1, retries)
        self.sleeper = sleeper
        self.lock = threading.RLock()
        self._secrets = (config.github_token,)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "108m-backend/1.0",
        }

    def _url(self, name: str, with_ref: bool = True) -> str:
        path = quote(f"{self.config.data_dir}/{name}", safe="/")
        url = f"{self.api_root}/{path}"
        if with_ref:
            url += "?" + urlencode({"ref": self.config.github_branch})
        return url

    def _call(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> tuple[int, dict[str, Any] | None]:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            status, headers, payload = self.http(method, url, self._headers(), raw_body, GITHUB_TIMEOUT)
        except ExternalServiceError:
            raise
        except Exception as exc:  # defensive: do not leak transport internals
            raise ExternalServiceError("Сетевой запрос к GitHub не выполнен.") from exc
        if status == 404 and allow_not_found:
            return status, None
        if status in (409, 422) and method in {"PUT", "DELETE"}:
            raise StorageConflict(f"GitHub CAS-конфликт (HTTP {status}).")
        if status in (401, 403):
            remaining = str(headers.get("X-RateLimit-Remaining", "")).strip()
            if remaining == "0":
                raise ExternalServiceError("GitHub API: исчерпан лимит запросов. Попробуйте позже.")
            raise ExternalServiceError("GitHub отклонил токен или не выдал нужные права.")
        if status in (500, 502, 503, 504):
            raise ExternalServiceError(f"GitHub временно недоступен (HTTP {status}).")
        if status >= 400:
            raise ExternalServiceError(f"GitHub вернул HTTP {status}.")
        if not payload:
            return status, {}
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExternalServiceError("GitHub вернул некорректный JSON.") from exc
        if not isinstance(parsed, dict):
            raise ExternalServiceError("GitHub вернул неожиданный ответ.")
        return status, parsed

    def _read_json(self, name: str) -> tuple[dict[str, Any] | None, str | None]:
        status, response = self._call("GET", self._url(name), allow_not_found=True)
        if status == 404 or response is None:
            return None, None
        content = response.get("content")
        encoding = response.get("encoding")
        sha = response.get("sha")
        if not isinstance(content, str) or encoding != "base64":
            raise ExternalServiceError(f"GitHub не вернул содержимое {name}.")
        try:
            raw = base64.b64decode("".join(content.split()), validate=False).decode("utf-8")
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise StorageError(f"{name}: повреждён JSON в репозитории.") from exc
        if not isinstance(parsed, dict):
            raise StorageError(f"{name}: ожидался JSON-объект.")
        return parsed, str(sha) if sha else None

    def _write_json(self, name: str, document: dict[str, Any], sha: str | None, message: str) -> str:
        encoded = base64.b64encode(json_dumps_stable(document).encode("utf-8")).decode("ascii")
        body: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": self.config.github_branch,
        }
        if sha:
            body["sha"] = sha
        _, response = self._call("PUT", self._url(name, with_ref=False), body=body)
        if not response:
            return ""
        content = response.get("content") if isinstance(response, dict) else None
        return str(content.get("sha", "")) if isinstance(content, dict) else ""

    def _media_url(self, path: str) -> str:
        repo = self.config.github_repository.strip("/")
        return f"https://raw.githubusercontent.com/{repo}/{self.config.github_branch}/{path}"

    def _media_contents_url(self, path: str) -> str:
        repo_api = self.api_root.rsplit("/contents", 1)[0]
        return f"{repo_api}/contents/{quote(path, safe='/')}"

    def upload_media(self, data: bytes, extension: str, sha256: str) -> str:
        """Опубликовать изображение ДЗ отдельным файлом и вернуть публичный URL.

        Журнальные фотографии сюда не попадают: метод вызывается только для
        вложений домашних заданий с явной подписью.
        """
        if not data:
            raise UserFacingError("Пустое изображение.")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise UserFacingError("Вложение слишком большое (максимум 8 МБ).")
        ext = "webp" if extension.lower() == "webp" else "jpg"
        path = f"{MEDIA_DIR}/{sha256[:32]}.{ext}"
        url = self._media_url(path)
        api_url = self._media_contents_url(path)
        status, response = self._call("GET", api_url + "?" + urlencode({"ref": self.config.github_branch}), allow_not_found=True)
        if status != 404 and response is not None:
            return url
        body = {
            "message": f"108M: homework attachment {sha256[:12]}",
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.config.github_branch,
        }
        try:
            self._call("PUT", api_url, body=body)
        except StorageConflict:
            # Параллельная загрузка того же файла: имя детерминировано по hash.
            return url
        return url

    def read_bundle(self, strict: bool = True) -> Bundle:
        with self.lock:
            manifest_raw, manifest_sha = self._read_json(MANIFEST_FILE)
            manifest: dict[str, Any] | None = None
            if manifest_raw is not None:
                try:
                    validate_manifest_document(manifest_raw)
                    manifest = manifest_raw
                except SchemaError as exc:
                    raise StorageError(f"manifest.json повреждён: {exc}") from exc
            raw_documents: dict[str, dict[str, Any] | None] = {}
            shas: dict[str, str | None] = {}
            for name in DATA_FILES:
                raw, sha = self._read_json(name)
                raw_documents[name] = raw
                shas[name] = sha
            revisions = [
                raw["revision"]
                for raw in raw_documents.values()
                if isinstance(raw, dict) and isinstance(raw.get("revision"), str)
            ]
            if manifest is not None:
                revision_hint = manifest["revision"]
            elif revisions:
                revision_hint = max(set(revisions), key=revisions.count)
            else:
                revision_hint = "bootstrap"
            documents: dict[str, dict[str, Any]] = {}
            for name in DATA_FILES:
                raw = raw_documents[name]
                if raw is None:
                    if name in ("students.json", "schedule.json"):
                        raise StorageError(f"В репозитории отсутствует обязательный {name}.")
                    raw = empty_document(name, revision_hint)
                try:
                    validate_document(name, raw)
                except SchemaError as exc:
                    raise StorageError(f"{name} не соответствует схеме: {exc}") from exc
                documents[name] = raw
            revisions_set = {document["revision"] for document in documents.values()}
            consistent = manifest is not None and len(revisions_set) == 1 and revisions_set == {manifest["revision"]}
            if manifest is None:
                common = next(iter(revisions_set)) if len(revisions_set) == 1 else revision_hint
                manifest = {
                    "schemaVersion": 1,
                    "revision": common,
                    "generatedAt": iso_now(),
                    "files": {name: documents[name]["revision"] for name in DATA_FILES},
                }
                consistent = len(revisions_set) == 1
            if strict:
                if not consistent:
                    raise StorageBusyError("Публикация ещё не завершена: файлы имеют разные revision.")
                try:
                    validate_bundle(documents, manifest)
                except (SchemaError, StorageBusyError) as exc:
                    raise StorageError(f"Опубликованный набор данных повреждён: {exc}") from exc
            return Bundle(documents=documents, shas=shas, manifest=manifest, manifest_sha=manifest_sha, consistent=consistent)

    def read_file(self, name: str) -> dict[str, Any]:
        return clone_json(self.read_bundle(strict=True).documents[name])

    def read_documents(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        bundle = self.read_bundle(strict=True)
        return clone_json(bundle.documents), clone_json(bundle.manifest)

    def publish(
        self,
        mutators: Mapping[str, Callable[[dict[str, Any]], None]],
        event_id: str,
        message: str,
    ) -> PublishResult:
        unknown = set(mutators) - set(DATA_FILES)
        if unknown:
            raise ValueError(f"Неизвестные файлы для записи: {', '.join(sorted(unknown))}")

        def apply_all(documents: dict[str, dict[str, Any]]) -> None:
            for name, mutator in mutators.items():
                mutator(documents[name])

        return self._publish_internal(apply_all, event_id, message)

    def publish_all(
        self,
        mutator: Callable[[dict[str, dict[str, Any]]], None],
        event_id: str,
        message: str,
    ) -> PublishResult:
        """Атомарно (в рамках CAS-повтора) изменить несколько файлов сразу."""
        return self._publish_internal(mutator, event_id, message)

    def _publish_internal(
        self,
        apply_all: Callable[[dict[str, dict[str, Any]]], None],
        event_id: str,
        message: str,
    ) -> PublishResult:
        if not event_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,120}", event_id):
            raise ValueError("event_id должен быть безопасной короткой строкой")
        commit_text = CONTROL_RE.sub(" ", str(message or "update")).strip()[:60] or "update"
        suffix = event_id[:12]
        with self.lock:
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    bundle = self.read_bundle(strict=False)
                    documents = clone_json(bundle.documents)
                    apply_all(documents)
                    for name in DATA_FILES:
                        validate_document(name, documents[name])
                    unchanged = documents == bundle.documents
                    if unchanged and bundle.consistent:
                        return PublishResult(bundle.manifest["revision"], False)
                    revision = new_revision()
                    generated_at = iso_now()
                    for name in DATA_FILES:
                        documents[name]["schemaVersion"] = 1
                        documents[name]["revision"] = revision
                        documents[name]["generatedAt"] = generated_at
                    manifest = {
                        "schemaVersion": 1,
                        "revision": revision,
                        "generatedAt": generated_at,
                        "files": {name: revision for name in DATA_FILES},
                    }
                    try:
                        validate_bundle(documents, manifest)
                    except (SchemaError, StorageBusyError) as exc:
                        raise StorageError(f"Данные не прошли проверку перед публикацией: {exc}") from exc
                    written: list[str] = []
                    for name in DATA_FILES:
                        self._write_json(
                            name,
                            documents[name],
                            bundle.shas.get(name),
                            f"108M: {commit_text} [{suffix}]",
                        )
                        written.append(name)
                    self._write_json(
                        MANIFEST_FILE,
                        manifest,
                        bundle.manifest_sha,
                        f"108M: publish manifest [{suffix}]",
                    )
                    written.append(MANIFEST_FILE)
                    LOG.info("published revision %s (%s files) event=%s", revision, len(written), event_id[:24])
                    return PublishResult(revision, True, tuple(written))
                except (StorageConflict, ExternalServiceError) as exc:
                    last_error = exc
                    if attempt == self.retries - 1:
                        break
                    delay = 0.5 * (2 ** attempt)
                    LOG.warning(
                        "publish retry %s/%s after %s",
                        attempt + 1,
                        self.retries,
                        safe_exception(exc, self._secrets),
                    )
                    self.sleeper(delay)
            raise ExternalServiceError("Не удалось опубликовать данные после нескольких попыток.") from last_error


# =============================================================================
# Расписание
# =============================================================================


class ScheduleService:
    """Чистые расчёты по schedule.json и replacements.json."""

    @staticmethod
    def week_type(day: date, schedule_doc: dict[str, Any], replacements_doc: dict[str, Any] | None = None) -> str:
        meta = schedule_doc.get("meta", {})
        anchor = safe_date(meta.get("weekAnchor")) or date(2026, 8, 31)
        weeks = (day - anchor).days // 7
        first = "denominator" if meta.get("firstWeekType") == "denominator" else "numerator"
        computed = first if weeks % 2 == 0 else ("denominator" if first == "numerator" else "numerator")
        if replacements_doc:
            source_types = {
                item.get("weekType")
                for item in replacements_doc.get("items", [])
                if item.get("date") == day.isoformat() and item.get("weekType") in ("numerator", "denominator")
            }
            if len(source_types) == 1:
                return next(iter(source_types))
        return computed

    @staticmethod
    def _valid_on(item: dict[str, Any], iso: str) -> bool:
        valid_from = item.get("validFrom")
        valid_to = item.get("validTo")
        return (not valid_from or valid_from <= iso) and (not valid_to or iso <= valid_to)

    def effective_lessons(
        self,
        day: date,
        schedule_doc: dict[str, Any],
        replacements_doc: dict[str, Any] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        iso = day.isoformat()
        weekday = day.isoweekday()
        week_type = self.week_type(day, schedule_doc, replacements_doc)
        lessons: list[dict[str, Any]] = []
        for item in schedule_doc.get("lessons", []):
            if int(item.get("weekday", 0)) != weekday:
                continue
            if not self._valid_on(item, iso):
                continue
            if item.get("weekType", "all") not in ("all", week_type):
                continue
            lesson = clone_json(item)
            lesson["weekTypeActual"] = week_type
            lessons.append(lesson)

        for rule in schedule_doc.get("replacementRules", []):
            if int(rule.get("weekday", 0)) != weekday:
                continue
            if rule.get("weekType") not in ("all", week_type):
                continue
            if not self._valid_on(rule, iso):
                continue
            index = next(
                (i for i, lesson in enumerate(lessons) if lesson.get("lessonId") == rule.get("originalLessonId")),
                None,
            )
            if index is None:
                continue
            original = lessons[index]
            lessons[index] = {
                **original,
                "subjectId": rule.get("newSubjectId", original.get("subjectId", "")),
                "subject": rule.get("newSubject", original.get("subject", "")),
                "teacher": rule.get("newTeacher", original.get("teacher", "")),
                "room": rule.get("newRoom", original.get("room", "")),
                "replacement": True,
                "replacementKind": "rule",
                "replacementRuleId": rule.get("ruleId", ""),
                "originalSubject": original.get("subject", ""),
                "originalTeacher": original.get("teacher", ""),
                "originalRoom": original.get("room", ""),
                "source": "schedule.json",
            }

        if replacements_doc:
            for replacement in replacements_doc.get("items", []):
                if replacement.get("date") != iso:
                    continue
                if replacement.get("weekType") and replacement.get("weekType") != week_type:
                    continue
                original_id = replacement.get("originalLessonId")
                index = next(
                    (i for i, lesson in enumerate(lessons) if original_id and lesson.get("lessonId") == original_id),
                    None,
                )
                if index is None:
                    pair = int(replacement.get("pairNumber", 0) or 0)
                    index = next((i for i, lesson in enumerate(lessons) if int(lesson.get("pairNumber", 0)) == pair), None)
                if replacement.get("cancelled"):
                    if index is not None:
                        lessons.pop(index)
                    continue
                if index is not None:
                    original = lessons[index]
                    lessons[index] = {
                        **original,
                        "subjectId": replacement.get("newSubjectId", original.get("subjectId", "")),
                        "subject": replacement.get("newSubject", original.get("subject", "")),
                        "teacher": replacement.get("newTeacher", original.get("teacher", "")),
                        "room": replacement.get("newRoom", original.get("room", "")),
                        "replacement": True,
                        "replacementKind": "individual",
                        "replacementId": replacement.get("replacementId", ""),
                        "originalSubject": original.get("originalSubject") or original.get("subject", ""),
                        "originalTeacher": original.get("originalTeacher") or original.get("teacher", ""),
                        "originalRoom": original.get("originalRoom") or original.get("room", ""),
                        "source": replacement.get("source", ""),
                        "sourceHash": replacement.get("sourceHash", ""),
                        "receivedAt": replacement.get("receivedAt", ""),
                        "weekTypeActual": week_type,
                    }
                else:
                    lessons.append({
                        "lessonId": original_id or f"replacement-{replacement.get('pairNumber', 0)}",
                        "weekday": weekday,
                        "pairNumber": int(replacement.get("pairNumber", 0) or 0),
                        "subjectId": replacement.get("newSubjectId", ""),
                        "subject": replacement.get("newSubject", ""),
                        "teacher": replacement.get("newTeacher", ""),
                        "room": replacement.get("newRoom", ""),
                        "subgroup": "all",
                        "weekType": week_type,
                        "weekTypeActual": week_type,
                        "service": False,
                        "replacement": True,
                        "replacementKind": "individual",
                        "replacementId": replacement.get("replacementId", ""),
                        "originalSubject": replacement.get("originalSubject", ""),
                        "originalRoom": "",
                        "source": replacement.get("source", ""),
                        "receivedAt": replacement.get("receivedAt", ""),
                    })

        lessons.sort(key=lambda item: (int(item.get("pairNumber", 0)), str(item.get("subject", ""))))
        return week_type, lessons

    def next_lesson(
        self,
        subject_id: str,
        after: date,
        schedule_doc: dict[str, Any],
        replacements_doc: dict[str, Any] | None = None,
        limit: int = 370,
    ) -> dict[str, Any] | None:
        for offset in range(1, limit + 1):
            day = after + timedelta(days=offset)
            _, lessons = self.effective_lessons(day, schedule_doc, replacements_doc)
            for lesson in lessons:
                if lesson.get("subjectId") == subject_id and not lesson.get("service"):
                    return {**lesson, "date": day.isoformat()}
        return None

    @staticmethod
    def subject_catalog(schedule_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        catalog: dict[str, dict[str, Any]] = {}
        for lesson in schedule_doc.get("lessons", []):
            subject_id = str(lesson.get("subjectId", ""))
            if not subject_id or lesson.get("service"):
                continue
            if subject_id not in catalog:
                catalog[subject_id] = {
                    "subjectId": subject_id,
                    "subject": str(lesson.get("subject", subject_id)),
                    "key": normalize_text(str(lesson.get("subject", subject_id))),
                }
        return catalog

    @staticmethod
    def times_by_pair(schedule_doc: dict[str, Any]) -> dict[int, dict[str, str]]:
        return {
            int(item.get("pairNumber", 0)): {"start": str(item.get("start", "")), "end": str(item.get("end", ""))}
            for item in schedule_doc.get("times", [])
        }


# =============================================================================
# Домашние задания
# =============================================================================

SUBJECT_ALIASES: dict[str, str] = {
    "математика": "math",
    "матем": "math",
    "математике": "math",
    "русский язык": "russian",
    "русский": "russian",
    "рус": "russian",
    "иностранный язык": "english",
    "иностранный": "english",
    "английский язык": "english",
    "английский": "english",
    "англ": "english",
    "англ яз": "english",
    "ин яз": "english",
    "обизр": "obizr",
    "обж": "obizr",
    "основы безопасности": "obizr",
    "введение в специальность": "intro",
    "введение": "intro",
    "введ": "intro",
    "химия": "chemistry",
    "химии": "chemistry",
    "биология": "biology",
    "биологии": "biology",
    "био": "biology",
    "информатика": "informatics",
    "информатике": "informatics",
    "инфа": "informatics",
    "история": "history",
    "истории": "history",
    "родная литература": "literature",
    "литература": "literature",
    "литературе": "literature",
    "физическая культура": "pe",
    "физкультура": "pe",
    "физкультуре": "pe",
    "физра": "pe",
    "физ ра": "pe",
    "физ к": "pe",
}

SERVICE_SUBJECT_ALIASES = {
    "разговоры о важном",
    "классный час",
    "россия мои горизонты",
    "горизонты",
}

END_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*$")


class HomeworkDraft:
    def __init__(
        self,
        subject_id: str,
        subject: str,
        text: str,
        assigned_date: date,
        next_lesson: dict[str, Any],
        naive_lesson: dict[str, Any] | None,
        moved: bool,
    ):
        self.subject_id = subject_id
        self.subject = subject
        self.text = text
        self.assigned_date = assigned_date
        self.next_lesson = next_lesson
        self.naive_lesson = naive_lesson
        self.moved = moved

    @property
    def task_id(self) -> str:
        return stable_hash(
            f"{self.subject_id}|{self.assigned_date.isoformat()}|{normalize_text(self.text)}"
        )[:24]

    @property
    def next_lesson_date(self) -> str:
        return str(self.next_lesson["date"])

    @property
    def next_lesson_id(self) -> str:
        return str(self.next_lesson["lessonId"])


class HomeworkResult:
    def __init__(
        self,
        status: str,
        draft: HomeworkDraft,
        existing_text: str | None = None,
        siblings: int = 0,
        attachment_added: bool = False,
    ):
        self.status = status  # added | attached | duplicate
        self.draft = draft
        self.existing_text = existing_text
        self.siblings = siblings
        self.attachment_added = attachment_added


class HomeworkService:
    def __init__(self, storage: GitHubStorage, schedule: ScheduleService):
        self.storage = storage
        self.schedule = schedule

    @staticmethod
    def _catalog_with_aliases(schedule_doc: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        catalog = ScheduleService.subject_catalog(schedule_doc)
        aliases: dict[str, str] = {}
        for subject_id, item in catalog.items():
            aliases[item["key"]] = subject_id
        for alias, subject_id in SUBJECT_ALIASES.items():
            if subject_id in catalog:
                aliases.setdefault(normalize_text(alias), subject_id)
        return catalog, aliases

    def _find_subject(self, text: str, schedule_doc: dict[str, Any]) -> tuple[str, str, str]:
        catalog, aliases = self._catalog_with_aliases(schedule_doc)
        words = text.split()
        if not words:
            raise UserFacingError("Пустое сообщение. Начни с названия предмета.")
        normalized_text = normalize_text(text)
        for service_alias in SERVICE_SUBJECT_ALIASES:
            if normalized_text == service_alias or normalized_text.startswith(service_alias + " "):
                raise UserFacingError("Это служебное событие, а не учебный предмет. ДЗ к нему не создаётся.")
        exact: list[tuple[int, int, str]] = []
        for count in range(1, min(5, len(words)) + 1):
            candidate = normalize_text(" ".join(words[:count]))
            if candidate in aliases:
                exact.append((count, len(candidate), aliases[candidate]))
        if exact:
            exact.sort(key=lambda item: (-item[1], item[0]))
            best_count, best_length, subject_id = exact[0]
            same_length = {item[2] for item in exact if item[1] == best_length}
            if len(same_length) > 1:
                raise UserFacingError("Не понял предмет: подходят несколько вариантов. Напиши полное название.")
            remaining = " ".join(words[best_count:]).strip()
            self._reject_second_subject(remaining, aliases)
            return subject_id, catalog[subject_id]["subject"], remaining

        first = normalize_text(words[0])
        scores: list[tuple[float, str]] = []
        for alias, subject_id in aliases.items():
            first_alias_word = alias.split()[0]
            score = max(
                difflib.SequenceMatcher(None, first, alias).ratio(),
                difflib.SequenceMatcher(None, first, first_alias_word).ratio(),
            )
            scores.append((score, subject_id))
        scores.sort(reverse=True, key=lambda item: item[0])
        if scores and scores[0][0] >= 0.78:
            if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.08 and scores[0][1] != scores[1][1]:
                raise UserFacingError("Не понял предмет: похожие варианты. Напиши точное название предмета.")
            subject_id = scores[0][1]
            remaining = " ".join(words[1:]).strip()
            self._reject_second_subject(remaining, aliases)
            return subject_id, catalog[subject_id]["subject"], remaining
        raise UserFacingError(
            "Не распознал предмет. Начни сообщение с предмета из расписания, например: "
            "«Математика выучить логарифмы 10.09»."
        )

    @staticmethod
    def _reject_second_subject(remaining: str, aliases: Mapping[str, str]) -> None:
        words = remaining.split()
        if not words:
            return
        for count in range(1, min(3, len(words)) + 1):
            if normalize_text(" ".join(words[:count])) in aliases:
                raise UserFacingError("В сообщении несколько предметов. Отправь отдельное сообщение для каждого.")

    @staticmethod
    def _extract_date(text: str, assigned: date) -> tuple[date, str]:
        match = END_DATE_RE.search(text)
        if not match:
            return assigned, text.strip(" .,:;—-")
        day, month, year = match.groups()
        year_value = int(year) if year else assigned.year
        if year_value < 100:
            year_value += 2000
        try:
            parsed = date(year_value, int(month), int(day))
        except ValueError as exc:
            raise UserFacingError("Дата задания некорректна.") from exc
        if not year and parsed > assigned + timedelta(days=31):
            try:
                parsed = parsed.replace(year=parsed.year - 1)
            except ValueError as exc:
                raise UserFacingError("Дата задания некорректна.") from exc
        if parsed > assigned + timedelta(days=31):
            raise UserFacingError("Дата задания слишком далеко в будущем. Проверь день и месяц.")
        cleaned = (text[:match.start()] + " " + text[match.end():]).strip(" .,:;—-")
        return parsed, cleaned

    def parse(
        self,
        text: str,
        assigned: date,
        schedule_doc: dict[str, Any],
        replacements_doc: dict[str, Any] | None,
        allow_empty_text: bool = False,
    ) -> HomeworkDraft:
        if len(text) > 1000:
            raise UserFacingError("Сообщение слишком длинное для домашнего задания.")
        subject_id, subject, remaining = self._find_subject(text.strip(), schedule_doc)
        assigned_date, homework = self._extract_date(remaining, assigned)
        homework = SPACE_RE.sub(" ", homework).strip()
        if not homework:
            if not allow_empty_text:
                raise UserFacingError("После предмета напиши текст задания. Например: «Математика выучить логарифмы 10.09».")
            homework = "Задание на фото"
        next_lesson = self.schedule.next_lesson(subject_id, assigned_date, schedule_doc, replacements_doc)
        if next_lesson is None:
            raise UserFacingError("Не нашёл следующий урок этого предмета в расписании. Сообщение не сохранено.")
        naive_lesson = self.schedule.next_lesson(subject_id, assigned_date, schedule_doc, None)
        moved = bool(naive_lesson and (naive_lesson.get("date") != next_lesson.get("date") or naive_lesson.get("lessonId") != next_lesson.get("lessonId")))
        return HomeworkDraft(subject_id, subject, homework, assigned_date, next_lesson, naive_lesson, moved)

    @staticmethod
    def _status_for(draft: HomeworkDraft) -> str:
        if draft.next_lesson_date < today_moscow().isoformat():
            return "просрочено"
        if draft.moved:
            return "перенесено"
        return "актуальное"

    def add(
        self,
        text: str,
        user_id: int,
        update_id: int | None,
        assigned: date,
        documents: dict[str, dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
        allow_empty_text: bool = False,
    ) -> HomeworkResult:
        draft = self.parse(
            text,
            assigned,
            documents["schedule.json"],
            documents["replacements.json"],
            allow_empty_text=allow_empty_text,
        )
        result_holder: dict[str, Any] = {}

        def mutate(tasks_doc: dict[str, Any]) -> None:
            tasks = tasks_doc.setdefault("tasks", [])
            existing = next(
                (
                    task for task in tasks
                    if task.get("subjectId") == draft.subject_id
                    and task.get("assignedDate") == draft.assigned_date.isoformat()
                    and task.get("nextLessonId") == draft.next_lesson_id
                    and normalize_text(task.get("text", "")) == normalize_text(draft.text)
                ),
                None,
            )
            now = iso_now()
            if existing is not None:
                current_attachments = existing.get("attachments", [])
                if attachments:
                    known = {item.get("sha256") for item in current_attachments}
                    fresh = [item for item in attachments if item.get("sha256") not in known]
                    if fresh:
                        if len(current_attachments) + len(fresh) > 6:
                            raise UserFacingError("К одному заданию можно приложить не больше 6 изображений.")
                        existing.setdefault("attachments", []).extend(fresh)
                        existing["updatedAt"] = now
                        if update_id is not None:
                            existing["sourceUpdateId"] = update_id
                        result_holder["status"] = "attached"
                        result_holder["attachment_added"] = True
                    else:
                        result_holder["status"] = "duplicate"
                        result_holder["attachment_added"] = False
                else:
                    result_holder["status"] = "duplicate"
                result_holder["existing_text"] = existing.get("text")
                result_holder["siblings"] = self._siblings(tasks, existing)
                return
            task = {
                "taskId": draft.task_id,
                "subjectId": draft.subject_id,
                "subject": draft.subject,
                "text": draft.text,
                "assignedDate": draft.assigned_date.isoformat(),
                "nextLessonDate": draft.next_lesson_date,
                "nextLessonId": draft.next_lesson_id,
                "movedByReplacement": draft.moved,
                "status": self._status_for(draft),
                "createdAt": now,
                "updatedAt": now,
                "sourceUserId": user_id,
            }
            if draft.moved:
                task["originalNextLessonDate"] = (draft.naive_lesson or {}).get("date", draft.next_lesson_date)
            if update_id is not None:
                task["sourceUpdateId"] = update_id
            if attachments:
                task["attachments"] = list(attachments[:6])
            if any(item.get("taskId") == task["taskId"] for item in tasks):
                task["taskId"] = stable_hash(task["taskId"] + "|" + now)[:24]
            tasks.append(task)
            result_holder["status"] = "added"
            result_holder["existing_text"] = None
            result_holder["siblings"] = self._siblings(tasks, task) - 1

        self.storage.publish({"tasks.json": mutate}, f"task-{draft.task_id}", "add homework")
        return HomeworkResult(
            result_holder.get("status", "duplicate"),
            draft,
            result_holder.get("existing_text"),
            int(result_holder.get("siblings", 0)),
            bool(result_holder.get("attachment_added", False)),
        )

    @staticmethod
    def _siblings(tasks: list[dict[str, Any]], task: dict[str, Any]) -> int:
        return sum(
            1 for item in tasks
            if item.get("subjectId") == task.get("subjectId")
            and item.get("nextLessonId") == task.get("nextLessonId")
        )

    def recalculate(self, documents: dict[str, dict[str, Any]], today: date | None = None) -> int:
        """Пересчитать следующие уроки и статусы после замен/полуночи."""
        current_day = today or today_moscow()
        changed = 0
        schedule_doc = documents["schedule.json"]
        replacements_doc = documents["replacements.json"]
        for task in documents["tasks.json"].get("tasks", []):
            if task.get("status") == "выполнено":
                continue
            task_changed = False
            assigned = safe_date(task.get("assignedDate"))
            if assigned is None:
                continue
            lesson = self.schedule.next_lesson(task.get("subjectId", ""), assigned, schedule_doc, replacements_doc)
            if lesson is None:
                continue
            naive = self.schedule.next_lesson(task.get("subjectId", ""), assigned, schedule_doc, None)
            moved = bool(naive and (naive.get("date") != lesson.get("date") or naive.get("lessonId") != lesson.get("lessonId")))
            new_status = "просрочено" if str(lesson["date"]) < current_day.isoformat() else ("перенесено" if moved else "актуальное")
            if task.get("nextLessonDate") != lesson["date"] or task.get("nextLessonId") != lesson["lessonId"]:
                task["nextLessonDate"] = lesson["date"]
                task["nextLessonId"] = lesson["lessonId"]
                task_changed = True
            if moved and not task.get("originalNextLessonDate"):
                task["originalNextLessonDate"] = (naive or {}).get("date", lesson["date"])
                task_changed = True
            if bool(task.get("movedByReplacement")) != moved:
                task["movedByReplacement"] = moved
                task_changed = True
            if task.get("status") != new_status:
                task["status"] = new_status
                task_changed = True
            if task_changed:
                task["updatedAt"] = iso_now()
                changed += 1
        return changed

    @staticmethod
    def describe(draft: HomeworkDraft, schedule_doc: dict[str, Any]) -> str:
        times = ScheduleService.times_by_pair(schedule_doc)
        pair = int(draft.next_lesson.get("pairNumber", 0))
        clock = times.get(pair, {})
        next_date = safe_date(draft.next_lesson_date)
        date_text = date_label_full(next_date) if next_date else draft.next_lesson_date
        parts = [f"{date_text}, {pair} пара"]
        if clock:
            parts.append(f"{clock.get('start', '')}–{clock.get('end', '')}")
        if draft.next_lesson.get("room"):
            parts.append(f"каб. {draft.next_lesson['room']}")
        return " · ".join(part for part in parts if part)


# =============================================================================
# Замены из PDF и VK
# =============================================================================

PDF_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
PDF_GROUP_RE = re.compile(r"(?:^|\s)(?:группа\s*)?(\d{3})\s*[МM]\b", re.IGNORECASE)
PDF_PAIR_RE = re.compile(r"^\s*(\d)\s*(?:пара|урок|ур\.?)\b\s*[:.)—–-]?\s*(.*)$", re.IGNORECASE)
PDF_PAIR_PLAIN_RE = re.compile(r"^\s*([1-7])\s*[.)]\s+(.+)$")
PDF_ROOM_RE = re.compile(r"каб(?:инет)?\.?\s*([0-9A-Za-zА-Яа-яЁё/_-]+)|\b(\d[кk]\d{3})\b", re.IGNORECASE)
PDF_CANCEL_RE = re.compile(r"\bотмен|\bнет\s+пары|\bснят", re.IGNORECASE)


class ReplacementImportResult:
    def __init__(
        self,
        status: str,
        rows: list[dict[str, Any]] | None = None,
        target_date: date | None = None,
        week_type: str = "",
        source: str = "",
        changed: bool = False,
        recalculated: int = 0,
        message: str = "",
    ):
        self.status = status
        self.rows = rows or []
        self.target_date = target_date
        self.week_type = week_type
        self.source = source
        self.changed = changed
        self.recalculated = recalculated
        self.message = message


class ReplacementParser:
    """Разбор текстового слоя PDF замен. Только строки группы 108М."""

    @staticmethod
    def extract_text(data: bytes) -> str:
        if PdfReader is None:
            raise UserFacingError("Для PDF установите pypdf: python -m pip install -r requirements.txt")
        if not data.startswith(b"%PDF-"):
            raise UserFacingError("Файл не похож на PDF. Перешлите PDF замен документом.")
        if len(data) > MAX_PDF_BYTES:
            raise UserFacingError("PDF слишком большой (максимум 10 МБ).")
        try:
            reader = PdfReader(BytesIO(data))
            pages: list[str] = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
        except Exception as exc:
            raise UserFacingError("Не удалось прочитать PDF. Возможно, это скан без текстового слоя.") from exc
        text = "\n".join(pages).strip()
        if not text:
            raise UserFacingError(
                "В PDF нет текстового слоя: это скан. Сделайте скриншот/текстовый PDF или пришлите замены текстом."
            )
        return text

    @staticmethod
    def extract_date(text: str, fallback: date | None = None) -> date | None:
        lines = [line for line in str(text).splitlines() if line.strip()]
        ordered = [line for line in lines if "замен" in normalize_text(line)] + lines
        for line in ordered:
            for match in PDF_DATE_RE.finditer(line):
                day_value, month_value, year_value = match.groups()
                base_year = (fallback or today_moscow()).year
                year = int(year_value) if year_value else base_year
                if year < 100:
                    year += 2000
                try:
                    parsed = date(year, int(month_value), int(day_value))
                except ValueError:
                    continue
                if not year_value and parsed < (fallback or today_moscow()) - timedelta(days=180):
                    try:
                        parsed = parsed.replace(year=year + 1)
                    except ValueError:
                        continue
                return parsed
        return fallback

    @staticmethod
    def extract_week_type(text: str) -> str:
        lowered = normalize_text(text)
        if "знаменатель" in lowered:
            return "denominator"
        if "числитель" in lowered:
            return "numerator"
        return ""

    @staticmethod
    def _subject_id(subject: str, schedule_doc: dict[str, Any]) -> tuple[str, str]:
        catalog = ScheduleService.subject_catalog(schedule_doc)
        cleaned = normalize_text(subject)
        if not cleaned:
            return "cancelled", "Отмена"
        best: tuple[float, str] | None = None
        for subject_id, item in catalog.items():
            key = normalize_text(item.get("key", ""))
            name = normalize_text(item.get("subject", ""))
            score = max(
                difflib.SequenceMatcher(None, cleaned, key).ratio(),
                difflib.SequenceMatcher(None, cleaned, name).ratio(),
            )
            if cleaned == key or cleaned == name:
                return subject_id, item.get("subject", subject)
            if best is None or score > best[0]:
                best = (score, subject_id)
        if best and best[0] >= 0.78:
            return best[1], catalog[best[1]].get("subject", subject)
        return "other-" + stable_hash(cleaned)[:10], subject

    @classmethod
    def parse(
        cls,
        text: str,
        schedule_doc: dict[str, Any],
        source: str,
        source_hash: str,
        source_url: str = "",
        fallback_date: date | None = None,
    ) -> ReplacementImportResult:
        target = cls.extract_date(text, fallback_date)
        if target is None:
            raise UserFacingError("В PDF не нашёл дату замен. Добавьте строку «Замены на ДД.ММ.ГГГГ».")
        week_type = cls.extract_week_type(text) or ScheduleService.week_type(target, schedule_doc, None)
        current_group = ""
        rows_by_pair: dict[int, dict[str, Any]] = {}
        for raw_line in str(text).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            group_matches = list(PDF_GROUP_RE.finditer(line))
            if group_matches:
                matched = group_matches[0]
                current_group = f"{matched.group(1)}М"
                line = (line[:matched.start()] + " " + line[matched.end():]).strip(" .,:;-—–")
                if not line:
                    continue
            if current_group != GROUP_NAME:
                continue
            pair_match = PDF_PAIR_RE.match(line) or PDF_PAIR_PLAIN_RE.match(line)
            if not pair_match:
                continue
            pair_number = int(pair_match.group(1))
            body = pair_match.group(2).strip(" .,:;—–-")
            if not body:
                continue
            cancelled = bool(PDF_CANCEL_RE.search(body))
            room_match = PDF_ROOM_RE.search(body)
            room = ""
            if room_match:
                room = (room_match.group(1) or room_match.group(2) or "").strip(" .,;")
            without_room = PDF_ROOM_RE.sub(" ", body)
            subject_part = re.split(r"\s+[—–-]\s+|\s{2,}", without_room)[0]
            subject = re.sub(r"\([^)]*\)", " ", subject_part)
            subject = SPACE_RE.sub(" ", subject).strip(" .,:;—–-")
            if cancelled and not subject:
                subject = "Отмена"
            if not subject:
                continue
            subject_id, canonical = cls._subject_id(subject, schedule_doc)
            replacement_id = "rep-" + stable_hash(f"{target.isoformat()}|{pair_number}|{GROUP_NAME}")[:20]
            row = {
                "replacementId": replacement_id,
                "date": target.isoformat(),
                "weekType": week_type,
                "pairNumber": pair_number,
                "newSubjectId": subject_id,
                "newSubject": canonical if not cancelled else (subject or "Отмена"),
                "newRoom": room,
                "cancelled": cancelled,
                "source": source,
                "sourceHash": source_hash,
                "receivedAt": iso_now(),
                "group": GROUP_NAME,
            }
            if cancelled:
                row["newSubject"] = subject
            if source_url:
                row["sourceUrl"] = source_url
            rows_by_pair[pair_number] = row
        rows = [rows_by_pair[pair] for pair in sorted(rows_by_pair)]
        if not rows:
            return ReplacementImportResult("no-108m", target_date=target, week_type=week_type, source=source)
        return ReplacementImportResult(
            "parsed",
            rows=rows,
            target_date=target,
            week_type=week_type,
            source=source,
        )


class VKClient:
    """Поиск вечернего поста замен в открытой группе VK."""

    def __init__(
        self,
        config: Config,
        http: Callable[[str, str, dict[str, str], bytes | None, int], tuple[int, dict[str, str], bytes]] | None = None,
    ):
        self.config = config
        self.http = http or _request_with_urllib

    def _api(self, method: str, params: dict[str, str]) -> dict[str, Any]:
        query = dict(params)
        query["access_token"] = self.config.vk_access_token or ""
        query["v"] = VK_API_VERSION
        url = f"https://api.vk.com/method/{method}?" + urlencode(query)
        status, _, payload = self.http("GET", url, {"User-Agent": "108m-backend/1.0"}, None, 30)
        if status != 200:
            raise ExternalServiceError(f"VK недоступен (HTTP {status}).")
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ExternalServiceError("VK вернул некорректный ответ.") from exc
        if not isinstance(data, dict) or "response" not in data:
            raise ExternalServiceError("VK вернул ошибку. Проверьте VK_GROUP_ID и VK_ACCESS_TOKEN.")
        return data["response"]

    def find_post(self, target: date) -> tuple[str, str] | None:
        response = self._api("wall.get", {
            "owner_id": f"-{self.config.vk_group_id}",
            "count": "30",
        })
        items = response.get("items", []) if isinstance(response, dict) else []
        best: tuple[int, int, str, str] | None = None
        for post in items:
            if not isinstance(post, dict):
                continue
            text = str(post.get("text", ""))
            title_date = ReplacementParser.extract_date(text)
            dates = [title_date] if title_date else []
            for match in PDF_DATE_RE.finditer(text):
                day_value, month_value, year_value = match.groups()
                try:
                    parsed = date(int(year_value) + (2000 if year_value and int(year_value) < 100 else 0) if year_value else target.year, int(month_value), int(day_value))
                except ValueError:
                    continue
                if parsed not in dates:
                    dates.append(parsed)
            if dates and target not in dates:
                continue
            lowered = normalize_text(text)
            score = 0
            if "замен" in lowered:
                score += 3
            if target in dates:
                score += 5
            pdf_url = ""
            pdf_title = ""
            for attachment in post.get("attachments", []) if isinstance(post.get("attachments"), list) else []:
                doc = attachment.get("doc") if isinstance(attachment, dict) else None
                if not isinstance(doc, dict):
                    continue
                extension = str(doc.get("ext", "")).lower()
                url = str(doc.get("url", ""))
                if extension == "pdf" and url.startswith("https://"):
                    pdf_url = url
                    pdf_title = str(doc.get("title", "zameni.pdf"))
                    break
            if not pdf_url:
                continue
            score += 4
            if not dates and "замен" not in lowered:
                continue
            timestamp = int(post.get("date", 0) or 0)
            candidate = (score, timestamp, pdf_url, pdf_title)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
        if best is None or best[0] < 5:
            return None
        return best[2], best[3]

    def download_pdf(self, url: str) -> bytes:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(
            host == suffix or host.endswith("." + suffix) for suffix in VK_ALLOWED_HOST_SUFFIXES
        ):
            raise ExternalServiceError("VK вернул недопустимый адрес PDF.")
        status, headers, payload = self.http("GET", url, {"User-Agent": "108m-backend/1.0"}, None, 60)
        if status != 200:
            raise ExternalServiceError(f"Не удалось скачать PDF из VK (HTTP {status}).")
        if len(payload) > MAX_PDF_BYTES:
            raise UserFacingError("PDF замен слишком большой (максимум 10 МБ).")
        if not payload.startswith(b"%PDF-"):
            raise UserFacingError("Вложение VK не является PDF.")
        content_type = str(headers.get("Content-Type", headers.get("content-type", ""))).lower()
        if content_type and "pdf" not in content_type and "octet-stream" not in content_type:
            raise UserFacingError("VK вернул файл с неожиданным MIME-типом.")
        return payload


# =============================================================================
# Дежурства
# =============================================================================

DUTY_PREFIX_RE = re.compile(
    r"^\s*(?:дежурили|дежурил[аи]?|дежурные|отч[её]т\s+о\s+дежурств[еа])\b[:\s—–-]*",
    re.IGNORECASE,
)
DUTY_COMMON_WORDS = {
    "сегодня", "дежурили", "дежурил", "дежурила", "дежурные", "дежурство",
    "человек", "человека", "ученик", "ученика", "учеников", "смена", "пара", "и",
}
HOMEWORK_CAPTION_RE = re.compile(
    r"^\s*(?:дз|д/з|домашк[аи]|домашнее\s+задание|hw)\b[:\s—–-]*",
    re.IGNORECASE,
)


class DutyParseResult:
    def __init__(self, student_ids: list[str], names: list[str], ambiguous: list[str], ignored: list[str]):
        self.student_ids = student_ids
        self.names = names
        self.ambiguous = ambiguous
        self.ignored = ignored


class DutyReportResult:
    def __init__(
        self,
        status: str,
        names: list[str],
        ambiguous: list[str],
        day_status: str,
        all_names: list[str] | None = None,
        added_names: list[str] | None = None,
    ):
        self.status = status
        self.names = names
        self.ambiguous = ambiguous
        self.day_status = day_status
        self.all_names = all_names if all_names is not None else names
        self.added_names = added_names if added_names is not None else names


class DutyCloseResult:
    def __init__(self, status: str, names: list[str], already_closed: bool = False):
        self.status = status
        self.names = names
        self.already_closed = already_closed


class DutyService:
    ALGORITHM_VERSION = DUTY_ALGORITHM_VERSION

    def __init__(self, storage: GitHubStorage):
        self.storage = storage

    @staticmethod
    def _students(students_doc: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(students_doc.get("students", []), key=lambda item: str(item.get("studentId", "")))

    @classmethod
    def seed_for(cls, day: date, students_doc: dict[str, Any]) -> str:
        students = cls._students(students_doc)
        version = stable_hash(json_dumps_stable(students))
        return stable_hash(f"{day.isoformat()}|{GROUP_NAME}|{version}|{cls.ALGORITHM_VERSION}")

    @staticmethod
    def _ensure_student_records(duties_doc: dict[str, Any], students_doc: dict[str, Any]) -> None:
        records = duties_doc.setdefault("students", {})
        for student in students_doc.get("students", []):
            student_id = str(student["studentId"])
            record = records.setdefault(student_id, {"name": student.get("name", ""), "count": 0})
            record["name"] = student.get("name", record.get("name", ""))
            record["count"] = int(record.get("count", 0) or 0)

    @staticmethod
    def _preference_pairs(duties_doc: dict[str, Any]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in duties_doc.get("pairPreferences", []) or []:
            if not isinstance(raw, (list, tuple)) or len(raw) != 2:
                continue
            left, right = str(raw[0]), str(raw[1])
            if left == right:
                continue
            key = tuple(sorted((left, right)))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((left, right))
        return sorted(pairs)

    @classmethod
    def select_for(
        cls,
        day: date,
        students_doc: dict[str, Any],
        duties_doc: dict[str, Any],
    ) -> list[str]:
        if day.isoweekday() > 5:
            raise UserFacingError("В выходные дежурства не назначаются без отдельного исключения.")
        cls._ensure_student_records(duties_doc, students_doc)
        records = duties_doc.get("students", {})
        if not records:
            raise UserFacingError("Список учеников пуст: дежурных выбрать нельзя.")
        counts = {student_id: int(record.get("count", 0) or 0) for student_id, record in records.items()}
        minimum = min(counts.values())
        layer = sorted(student_id for student_id, count in counts.items() if count == minimum)
        seed = cls.seed_for(day, students_doc)
        rng = random.Random(int(seed[:16], 16))

        # Пары пожеланий учитываются только среди учеников с минимальным числом дежурств.
        preferred_pairs = [
            pair for pair in cls._preference_pairs(duties_doc)
            if pair[0] in layer and pair[1] in layer
        ]
        if preferred_pairs:
            chosen = rng.choice(preferred_pairs)
            return [chosen[0], chosen[1]]

        if len(layer) >= 2:
            return rng.sample(layer, 2)
        selected = layer[:1]
        first = selected[0]
        partners = sorted({
            right if left == first else left
            for left, right in cls._preference_pairs(duties_doc)
            if first in (left, right)
        } - set(selected))
        if partners:
            best_count = min(counts[student_id] for student_id in partners)
            # Небольшой перевес в пользу пожелания: партнёр может быть на один дежурный слой выше.
            if best_count <= minimum + 1:
                best_layer = [student_id for student_id in partners if counts[student_id] == best_count]
                selected.append(rng.choice(best_layer))
                return selected
        next_counts = sorted({count for count in counts.values() if count > minimum})
        if next_counts:
            second_layer = sorted(
                student_id for student_id, count in counts.items()
                if count == next_counts[0] and student_id not in selected
            )
        else:
            second_layer = sorted(student_id for student_id in counts if student_id not in selected)
        if not second_layer:
            raise UserFacingError("Не удалось выбрать второго дежурного: список учеников слишком мал.")
        selected.append(rng.choice(second_layer))
        return selected

    @classmethod
    def resolve_pair(
        cls,
        tokens: list[str],
        students_doc: dict[str, Any],
    ) -> tuple[str, str]:
        """Разобрать пару пожеланий из фамилий/имён. Нужно ровно два разных ученика."""
        cleaned: list[str] = []
        for token in tokens:
            for part in re.split(r"[,;+/]|\s+и\s+|\s+", str(token or "")):
                part = part.strip(" .,:;—-")
                if part:
                    cleaned.append(part)
        if len(cleaned) != 2:
            raise UserFacingError("Укажи ровно две фамилии: /pair Абакаев Бадреев")
        resolved: list[str] = []
        unresolved: list[str] = []
        for token in cleaned:
            query = normalize_text(token)
            if len(query) < 3:
                unresolved.append(token)
                continue
            student, ambiguous = cls._resolve_name_token(query, students_doc)
            if ambiguous or student is None:
                unresolved.append(token)
                continue
            student_id = str(student["studentId"])
            if student_id not in resolved:
                resolved.append(student_id)
        if unresolved:
            raise UserFacingError(
                "Не смог однозначно найти: " + ", ".join(unresolved)
                + ". Напиши фамилию точнее."
            )
        if len(resolved) != 2:
            raise UserFacingError("Нужны два разных ученика. Проверь фамилии.")
        return resolved[0], resolved[1]

    def update_pair(
        self,
        tokens: list[str],
        students_doc: dict[str, Any],
        documents: dict[str, dict[str, Any]],
        remove: bool = False,
    ) -> tuple[list[str], bool]:
        left, right = self.resolve_pair(tokens, students_doc)
        names = {
            str(student["studentId"]): str(student.get("name", student["studentId"]))
            for student in students_doc.get("students", [])
        }
        key = tuple(sorted((left, right)))
        changed = False

        def mutate(duties_doc: dict[str, Any]) -> None:
            nonlocal changed
            current = [
                tuple(sorted((str(pair[0]), str(pair[1]))))
                for pair in duties_doc.get("pairPreferences", []) or []
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
            if remove:
                if key in current:
                    duties_doc["pairPreferences"] = [
                        pair for pair in duties_doc.get("pairPreferences", [])
                        if tuple(sorted((str(pair[0]), str(pair[1])))) != key
                    ]
                    changed = True
                return
            if key not in current:
                duties_doc.setdefault("pairPreferences", []).append([left, right])
                duties_doc["pairPreferences"].sort(key=lambda pair: tuple(sorted((str(pair[0]), str(pair[1])))))
                changed = True

        self.storage.publish(
            {"duties.json": mutate},
            f"duty-pair-{'remove' if remove else 'add'}-{short_hash('|'.join(key), 16)}",
            "update duty pair preference",
        )
        return [names.get(left, left), names.get(right, right)], changed

    @classmethod
    def _ensure_day_in_doc(
        cls,
        day: date,
        duties_doc: dict[str, Any],
        students_doc: dict[str, Any],
    ) -> dict[str, Any]:
        cls._ensure_student_records(duties_doc, students_doc)
        days = duties_doc.setdefault("days", [])
        existing = next((item for item in days if item.get("date") == day.isoformat()), None)
        if existing is not None:
            return existing
        if day.isoweekday() > 5:
            raise UserFacingError("В выходные дежурства не назначаются.")
        selected = cls.select_for(day, students_doc, duties_doc)
        names = {
            str(student["studentId"]): str(student.get("name", ""))
            for student in students_doc.get("students", [])
        }
        item = {
            "date": day.isoformat(),
            "selected": selected,
            "selectedNames": [names.get(student_id, student_id) for student_id in selected],
            "actual": [],
            "actualNames": [],
            "reports": [],
            "status": "selected",
            "seed": cls.seed_for(day, students_doc),
            "algorithmVersion": cls.ALGORITHM_VERSION,
            "closed": False,
        }
        days.append(item)
        days.sort(key=lambda entry: str(entry.get("date", "")))
        return item

    def ensure_day(self, day: date, documents: dict[str, dict[str, Any]]) -> dict[str, Any]:
        result_holder: dict[str, Any] = {}

        def mutate(duties_doc: dict[str, Any]) -> None:
            result_holder["day"] = clone_json(
                self._ensure_day_in_doc(day, duties_doc, documents["students.json"])
            )

        self.storage.publish({"duties.json": mutate}, f"duty-select-{day.isoformat()}", "select duty")
        return result_holder.get("day", {})

    @classmethod
    def _resolve_name_token(
        cls,
        query: str,
        students_doc: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        students = cls._students(students_doc)
        scored: list[tuple[float, dict[str, Any]]] = []
        for student in students:
            full = normalize_text(student.get("name", ""))
            parts = full.split()
            surname = parts[0] if parts else ""
            given = parts[1] if len(parts) > 1 else ""
            terms = {full, surname, given}
            for alias in student.get("aliases", []):
                terms.add(normalize_text(alias))
            best = 0.0
            for term in terms:
                if not term:
                    continue
                if query == term:
                    score = 1.0
                elif len(query) >= 3 and (query in term or (len(term) >= 3 and term in query)):
                    score = 0.97
                else:
                    score = difflib.SequenceMatcher(None, query, term).ratio()
                best = max(best, score)
            scored.append((best, student))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored or scored[0][0] < 0.80:
            return None, True
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.08:
            return None, True
        return scored[0][1], False

    @classmethod
    def parse_report(cls, text: str, students_doc: dict[str, Any]) -> DutyParseResult | None:
        match = DUTY_PREFIX_RE.match(text or "")
        if not match:
            return None
        body = (text or "")[match.end():].strip()
        body = re.sub(r"^\s*(?:сегодня\s+)?\d+\s*(?:человек[а]?|ученик[а]?|учеников)?\s*[:\s—–-]*", "", body, flags=re.I)
        body = body.strip(" .,:;—-")
        if not body:
            return DutyParseResult([], [], [], [])
        parts = [part for part in re.split(r"[,;\n]+|\s+и\s+|\s*/\s*", body) if part.strip()]
        if len(parts) == 1 and len(parts[0].split()) > 1:
            parts = parts[0].split()
        ids: list[str] = []
        names: list[str] = []
        ambiguous: list[str] = []
        ignored: list[str] = []
        for raw in parts:
            cleaned = re.sub(r"^\s*\d+\s*[.)]?\s*", "", raw).strip(" .,:;—-")
            query = normalize_text(cleaned)
            if not query or query in DUTY_COMMON_WORDS:
                continue
            if len(query) < 3:
                ignored.append(cleaned)
                continue
            student, is_ambiguous = cls._resolve_name_token(query, students_doc)
            if is_ambiguous or student is None:
                ambiguous.append(cleaned)
                continue
            student_id = str(student["studentId"])
            if student_id not in ids:
                ids.append(student_id)
                names.append(str(student.get("name", student_id)))
        return DutyParseResult(ids, names, ambiguous, ignored)

    def add_report(
        self,
        day: date,
        text: str,
        user_id: int,
        update_id: int | None,
        documents: dict[str, dict[str, Any]],
    ) -> DutyReportResult:
        students_doc = documents["students.json"]
        parsed = self.parse_report(text, students_doc)
        if parsed is None:
            raise UserFacingError("Не понял отчёт. Формат: «Дежурили Абакаев, Бадреев, Осадчий».")
        if not parsed.student_ids and not parsed.ambiguous:
            raise UserFacingError("Не нашёл имён в отчёте. Ничего не засчитано; напиши ФИО или фамилии точнее.")
        status = "pending" if parsed.ambiguous or not parsed.student_ids else "confirmed"
        report_id = stable_hash(f"{day.isoformat()}|{user_id}|{update_id}|{normalize_text(text)}")[:24]
        result_holder: dict[str, Any] = {}

        def mutate(duties_doc: dict[str, Any]) -> None:
            day_entry = self._ensure_day_in_doc(day, duties_doc, students_doc)
            if day_entry.get("closed"):
                raise UserFacingError("Этот день уже закрыт. Отчёт не изменяет счётчики.")
            reports = day_entry.setdefault("reports", [])
            existing = next((report for report in reports if report.get("reportId") == report_id), None)
            if existing is not None:
                result_holder["status"] = existing.get("status")
                result_holder["names"] = list(existing.get("studentNames", []))
                result_holder["ambiguous"] = list(existing.get("ambiguous", []))
                result_holder["day_status"] = day_entry.get("status")
                result_holder["all_names"] = self._confirmed_names(day_entry)
                result_holder["added_names"] = []
                return
            known_ids = {
                student_id
                for report in reports
                if report.get("status") == "confirmed"
                for student_id in report.get("studentIds", [])
            }
            reports.append({
                "reportId": report_id,
                "userId": user_id,
                "receivedAt": iso_now(),
                "status": status,
                "studentIds": parsed.student_ids,
                "studentNames": parsed.names,
                "ambiguous": parsed.ambiguous,
                "messageHash": stable_hash(normalize_text(text)),
            })
            day_entry["status"] = "reported" if any(
                report.get("status") == "confirmed" for report in reports
            ) else "pending"
            result_holder["status"] = status
            result_holder["names"] = parsed.names
            result_holder["ambiguous"] = parsed.ambiguous
            result_holder["day_status"] = day_entry["status"]
            result_holder["all_names"] = self._confirmed_names(day_entry)
            result_holder["added_names"] = [
                name for student_id, name in zip(parsed.student_ids, parsed.names)
                if student_id not in known_ids
            ]

        self.storage.publish({"duties.json": mutate}, f"duty-report-{report_id}", "duty report")
        return DutyReportResult(
            result_holder.get("status", status),
            result_holder.get("names", parsed.names),
            result_holder.get("ambiguous", parsed.ambiguous),
            result_holder.get("day_status", "pending"),
            result_holder.get("all_names", parsed.names),
            result_holder.get("added_names", parsed.names),
        )

    @staticmethod
    def _confirmed_names(day_entry: dict[str, Any]) -> list[str]:
        names: list[str] = []
        for report in day_entry.get("reports", []):
            if report.get("status") != "confirmed":
                continue
            for name in report.get("studentNames", []):
                if name not in names:
                    names.append(name)
        return names

    @classmethod
    def _close_day_in_doc(
        cls,
        day: date,
        duties_doc: dict[str, Any],
        students_doc: dict[str, Any],
    ) -> DutyCloseResult:
        cls._ensure_student_records(duties_doc, students_doc)
        day_entry = next((item for item in duties_doc.setdefault("days", []) if item.get("date") == day.isoformat()), None)
        if day_entry is None:
            return DutyCloseResult("no-selection", [])
        if day_entry.get("closed"):
            names = duties_doc.get("students", {})
            actual_names = [
                names.get(student_id, {}).get("name", student_id)
                for student_id in day_entry.get("actual", [])
            ]
            return DutyCloseResult("already-closed", actual_names, already_closed=True)
        confirmed = [report for report in day_entry.get("reports", []) if report.get("status") == "confirmed"]
        day_entry["closed"] = True
        day_entry["closedAt"] = iso_now()
        if not confirmed:
            day_entry["actual"] = []
            day_entry["actualNames"] = []
            day_entry["status"] = "closed-without-confirmation"
            return DutyCloseResult("closed-without-confirmation", [])
        # Несколько отчётов объединяются: «Дежурили …» дважды означает всех упомянутых.
        actual: list[str] = []
        for report in confirmed:
            for student_id in report.get("studentIds", []):
                if student_id not in actual:
                    actual.append(student_id)
        actual_names = [
            duties_doc.get("students", {}).get(student_id, {}).get("name")
            or next((student.get("name", "") for student in students_doc.get("students", []) if student.get("studentId") == student_id), student_id)
            for student_id in actual
        ]
        day_entry["actual"] = actual
        day_entry["actualNames"] = actual_names
        day_entry["status"] = "closed"
        for student_id in actual:
            record = duties_doc.setdefault("students", {}).setdefault(
                student_id,
                {"name": next((student.get("name", "") for student in students_doc.get("students", []) if student.get("studentId") == student_id), student_id), "count": 0},
            )
            record["count"] = int(record.get("count", 0) or 0) + 1
        return DutyCloseResult("closed", actual_names)

    def close_day(self, day: date, documents: dict[str, dict[str, Any]]) -> DutyCloseResult:
        result_holder: dict[str, Any] = {}

        def mutate(duties_doc: dict[str, Any]) -> None:
            result_holder["result"] = self._close_day_in_doc(day, duties_doc, documents["students.json"])

        self.storage.publish({"duties.json": mutate}, f"duty-close-{day.isoformat()}", "close duty day")
        return result_holder.get("result", DutyCloseResult("no-selection", []))

    @staticmethod
    def reason(day_entry: dict[str, Any] | None) -> str:
        if not day_entry:
            return "выбор ещё не опубликован"
        if day_entry.get("status") in ("closed", "closed-without-confirmation"):
            return "подтверждён сообщением" if day_entry.get("actual") else "не подтверждён"
        if day_entry.get("status") == "reported":
            return "ожидает закрытия дня"
        if day_entry.get("status") == "pending":
            return "ожидает проверки"
        return "выбран"

    @classmethod
    def describe_pair_preferences(cls, documents: dict[str, dict[str, Any]]) -> str:
        names = {
            str(student["studentId"]): str(student.get("name", student["studentId"]))
            for student in documents["students.json"].get("students", [])
        }
        pairs = cls._preference_pairs(documents["duties.json"])
        if not pairs:
            return (
                "Пожелания по парам дежурных не заданы.\n"
                "Добавить: /pair Абакаев Бадреев\n"
                "Удалить: /unpair Абакаев Бадреев"
            )
        lines = ["Пожелания по парам дежурных:"]
        for left, right in pairs:
            lines.append(f"— {names.get(left, left)} + {names.get(right, right)}")
        lines.append("")
        lines.append("Бот выберет такую пару, если оба ученика входят в минимальный слой дежурств.")
        return "\n".join(lines)

    @classmethod
    def describe_recent(cls, documents: dict[str, dict[str, Any]], limit: int = 7) -> str:
        days = sorted(documents["duties.json"].get("days", []), key=lambda item: str(item.get("date", "")), reverse=True)
        if not days:
            return "Дежурства ещё не выбирались. Бот сделает выбор в ближайший учебный день в 00:00 по Москве."
        lines = ["Последние дежурства:"]
        for day_entry in days[:limit]:
            parsed_date = safe_date(day_entry.get("date"))
            date_text = date_label_full(parsed_date) if parsed_date else str(day_entry.get("date", ""))
            selected = ", ".join(day_entry.get("selectedNames", [])) or "—"
            actual = ", ".join(day_entry.get("actualNames", [])) or "никто"
            lines.append(f"{date_text}: выбраны {selected}; засчитаны {actual} · {cls.reason(day_entry)}")
        records = documents["duties.json"].get("students", {})
        if records:
            lowest = min(int(record.get("count", 0) or 0) for record in records.values())
            leaders = sorted(
                record.get("name", student_id)
                for student_id, record in records.items()
                if int(record.get("count", 0) or 0) == lowest
            )
            if leaders:
                lines.append("")
                lines.append(f"Реже всех дежурили ({lowest}): " + ", ".join(leaders[:8]) + ("…" if len(leaders) > 8 else ""))
        return "\n".join(lines)


# =============================================================================
# Ночная обработка
# =============================================================================


class BotScheduler:
    """Идемпотентные задачи: закрытие дня, выбор на сегодня, пересчёт ДЗ."""

    def __init__(self, current: "Backend"):
        self.backend = current
        self.task: asyncio.Task | None = None
        self.catch_up_done = False
        self.bot: Any = None

    async def start(self, app: Any = None) -> None:
        self.bot = getattr(app, "bot", None) if app is not None else None
        self.task = asyncio.create_task(self.loop(), name="108m-scheduler")

    async def stop(self, app: Any = None) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("scheduler tick failed: %s", safe_exception(exc, self.backend.secrets))
            await asyncio.sleep(30)

    async def tick(self) -> None:
        now = now_moscow()
        today = now.date()
        if not self.catch_up_done:
            await self.run_maintenance(today)
            self.catch_up_done = True
        if now.hour == 0:
            marker = f"midnightDone:{today.isoformat()}"
            if not self.backend.local.get(marker):
                await self.run_maintenance(today)
                self.backend.local.set(marker, iso_now())
        if now.hour == 20:
            marker = f"vkImportDone:{today.isoformat()}"
            if not self.backend.local.get(marker):
                self.backend.local.set(marker, iso_now())
                await self.run_vk_import(today)

    async def run_vk_import(self, today: date) -> None:
        target = today + timedelta(days=1)
        while target.isoweekday() > 5:
            target += timedelta(days=1)
        try:
            result = await asyncio.to_thread(self.backend.import_replacements_from_vk, target)
        except UserFacingError as exc:
            await self._notify_all(f"Замены из VK не загружены: {exc}\nПерешлите PDF замен в бота вручную.")
            return
        except (ExternalServiceError, StorageBusyError, StorageError) as exc:
            LOG.warning("vk import failed: %s", safe_exception(exc, self.backend.secrets))
            await self._notify_all("Не удалось получить замены из VK. Перешлите PDF замен в бота вручную.")
            return
        except Exception as exc:
            LOG.error("vk import crashed: %s", safe_exception(exc, self.backend.secrets))
            await self._notify_all("Не удалось получить замены из VK. Перешлите PDF замен в бота вручную.")
            return
        if result.status in ("vk-disabled", "vk-not-found", "no-108m"):
            await self._notify_all(result.message)
        elif result.changed:
            await self._notify_all(result.message)

    async def _notify_all(self, text: str) -> None:
        if self.bot is None:
            return
        for user_id in sorted(self.backend.config.allowed_telegram_ids):
            try:
                await self.bot.send_message(user_id, text, disable_web_page_preview=True)
            except Exception as exc:
                LOG.warning("notification failed user=%s: %s", user_id, safe_exception(exc, self.backend.secrets))

    async def run_maintenance(self, today: date) -> None:
        documents, _ = await self.backend.read_documents()
        students_doc = documents["students.json"]
        closed_days: list[str] = []
        ensured_today = False

        def mutate_duties(duties_doc: dict[str, Any]) -> None:
            nonlocal ensured_today
            for day_entry in sorted(duties_doc.get("days", []), key=lambda item: str(item.get("date", ""))):
                day_value = safe_date(day_entry.get("date"))
                if day_value and day_value < today and not day_entry.get("closed"):
                    result = DutyService._close_day_in_doc(day_value, duties_doc, students_doc)
                    if result.status in ("closed", "closed-without-confirmation"):
                        closed_days.append(day_value.isoformat())
            if today.isoweekday() <= 5:
                DutyService._ensure_day_in_doc(today, duties_doc, students_doc)
                ensured_today = True

        def mutate_tasks(tasks_doc: dict[str, Any]) -> None:
            wrapper = {
                "tasks.json": tasks_doc,
                "schedule.json": documents["schedule.json"],
                "replacements.json": documents["replacements.json"],
            }
            self.backend.homework.recalculate(wrapper, today)

        result = await asyncio.to_thread(
            self.backend.storage.publish,
            {"duties.json": mutate_duties, "tasks.json": mutate_tasks},
            f"maintenance-{today.isoformat()}-{secrets.token_hex(4)}",
            "midnight duty and homework maintenance",
        )
        if result.changed:
            LOG.info(
                "maintenance revision=%s closed=%s today_selected=%s",
                result.revision,
                len(closed_days),
                ensured_today,
            )


# =============================================================================
# Gemini и слияние оценок
# =============================================================================

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}


@dataclass(frozen=True)
class PhotoInput:
    data: bytes
    mime_type: str
    file_name: str
    sha256: str


@dataclass
class GeminiResult:
    response: dict[str, Any]
    model: str
    rotation: int


@dataclass
class MergeStats:
    added: int = 0
    same: int = 0
    low: int = 0
    conflicts: int = 0
    unresolved: int = 0
    changed: bool = False


@dataclass
class RecognisedPage:
    photo: PhotoInput
    result: GeminiResult


class GeminiClient:
    def __init__(
        self,
        config: Config,
        quota: Quota,
        http: Callable[[str, str, dict[str, str], bytes | None, int], tuple[int, dict[str, str], bytes]] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.quota = quota
        self.http = http or _request_with_urllib
        self.sleeper = sleeper

    @staticmethod
    def _prompt(students_doc: dict[str, Any], schedule_doc: dict[str, Any]) -> str:
        students = [
            {
                "studentId": student.get("studentId"),
                "name": student.get("name"),
                "aliases": student.get("aliases", []),
                "englishGroup": student.get("englishGroup"),
            }
            for student in students_doc.get("students", [])
        ]
        subjects = [
            {"subjectId": item["subjectId"], "subject": item["subject"]}
            for item in ScheduleService.subject_catalog(schedule_doc).values()
        ]
        students_json = json.dumps(students, ensure_ascii=False, separators=(",", ":"))
        subjects_json = json.dumps(subjects, ensure_ascii=False, separators=(",", ":"))
        return (
            "Ты распознаёшь фотографию страницы журнала группы 108М колледжа КГАМТ.\n"
            "Верни ТОЛЬКО JSON без Markdown и без пояснений.\n"
            f"Версия схемы: {PROMPT_VERSION}.\n"
            f"Ученики с постоянными ID: {students_json}\n"
            f"Предметы: {subjects_json}\n"
            "Правила:\n"
            "1. Допустимые значения оценки: 2, 3, 4, 5, н/б. Точка, пустая клетка и маркер А не являются оценкой.\n"
            "2. Не додумывай отсутствующие клетки и даты. Если клетка не видна или не читается — не добавляй её; опиши в warnings.\n"
            "3. Рукописную Н нельзя превращать в 4. Если символ похож на Н и 4 одновременно — укажи value 4, raw Н, confidence не выше 0.5 и объясни в reason.\n"
            "4. entryType: daily — обычная отметка, attendance — посещаемость (например, н/б), attestation — итог аттестации.\n"
            "5. studentId бери только из списка. Если не уверен — оставь пустым, а studentHint заполни видимым текстом.\n"
            "6. date — только YYYY-MM-DD, если дата действительно видна. Не выдумывай дату.\n"
            "7. confidence — число от 0 до 1. alternatives — другие возможные значения.\n"
            "8. document.group всегда 108М; document.subject — название предмета; document.orientation — 0, 90, 180 или 270.\n"
            "Схема ответа:\n"
            '{"document":{"group":"108М","subject":"","subjectId":"","orientation":0},'
            '"cells":[{"studentHint":"","studentId":"","date":"YYYY-MM-DD","raw":"","value":"5",'
            '"entryType":"daily","confidence":0.95,"alternatives":[],"reason":""}],"warnings":[]}'
        )

    @staticmethod
    def _image_bytes(photo: PhotoInput, rotation: int = 0) -> bytes:
        if Image is None:
            raise UserFacingError("Для фотографий установите Pillow: python -m pip install -r requirements.txt")
        if len(photo.data) > MAX_IMAGE_BYTES:
            raise UserFacingError("Изображение слишком большое (максимум 12 МБ).")
        if photo.mime_type and photo.mime_type not in ALLOWED_IMAGE_MIME:
            raise UserFacingError("Поддерживаются только JPEG, PNG и WebP.")
        try:
            image = Image.open(BytesIO(photo.data))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
            if rotation:
                image = image.rotate(rotation, expand=True)
            output = BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
        except Exception as exc:
            raise UserFacingError("Не удалось открыть изображение. Отправьте JPEG, PNG или WebP.") from exc
        result = output.getvalue()
        if len(result) > MAX_IMAGE_BYTES:
            raise UserFacingError("Изображение слишком большое после сжатия.")
        return result

    @staticmethod
    def homework_image(photo: PhotoInput) -> tuple[bytes, str, str, int, int]:
        """Сжать вложение ДЗ, убрать EXIF и вернуть (bytes, mime, extension, width, height)."""
        if Image is None:
            raise UserFacingError("Для изображений установите Pillow: python -m pip install -r requirements.txt")
        if len(photo.data) > MAX_IMAGE_BYTES:
            raise UserFacingError("Изображение слишком большое (максимум 12 МБ).")
        if photo.mime_type and photo.mime_type not in ALLOWED_IMAGE_MIME:
            raise UserFacingError("Поддерживаются только JPEG, PNG и WebP.")
        try:
            image = Image.open(BytesIO(photo.data))
            image.load()
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((MAX_HOMEWORK_IMAGE_SIDE, MAX_HOMEWORK_IMAGE_SIDE), Image.Resampling.LANCZOS)
            width, height = image.size
            output = BytesIO()
            try:
                image.save(output, format="WEBP", quality=82, method=4)
                mime, ext = "image/webp", "webp"
            except Exception:
                output = BytesIO()
                image.save(output, format="JPEG", quality=82, optimize=True)
                mime, ext = "image/jpeg", "jpg"
        except UserFacingError:
            raise
        except Exception as exc:
            raise UserFacingError("Не удалось открыть изображение. Отправьте JPEG, PNG или WebP.") from exc
        result = output.getvalue()
        if len(result) > MAX_ATTACHMENT_BYTES:
            raise UserFacingError("Вложение слишком большое после сжатия.")
        return result, mime, ext, width, height

    @staticmethod
    def extract_json(text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", value, flags=re.IGNORECASE | re.DOTALL)
        if fence:
            value = fence.group(1).strip()
        start = value.find("{")
        if start < 0:
            raise ValueError("Gemini не вернул JSON")
        try:
            parsed, _ = json.JSONDecoder().raw_decode(value[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Gemini вернул некорректный JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Gemini вернул не объект")
        return parsed

    @staticmethod
    def validate_response(value: Any) -> dict[str, Any]:
        try:
            root = _object(value, "gemini", {"document", "cells", "warnings"}, {"document", "cells", "warnings", "metadata"})
            document = _object(
                root["document"],
                "gemini.document",
                {"group", "subject", "orientation"},
                {"group", "subject", "subjectId", "orientation"},
            )
            group = normalize_text(document["group"]).upper().replace("M", "М")
            if group not in {"108М"}:
                raise SchemaError("gemini.document.group: ожидалась группа 108М")
            subject = _string(document["subject"], "gemini.document.subject", allow_empty=True)
            if "subjectId" in document:
                _string(document["subjectId"], "gemini.document.subjectId", allow_empty=True)
            orientation = _integer(document["orientation"], "gemini.document.orientation")
            if orientation not in (0, 90, 180, 270):
                raise SchemaError("gemini.document.orientation: допустимо 0, 90, 180, 270")
            cells = _list(root["cells"], "gemini.cells", 0, 5000)
            for index, item in enumerate(cells):
                where = f"gemini.cells[{index}]"
                cell = _object(
                    item,
                    where,
                    {"studentHint", "studentId", "date", "raw", "value", "entryType", "confidence", "alternatives", "reason"},
                    {"studentHint", "studentId", "date", "raw", "value", "entryType", "confidence", "alternatives", "reason"},
                )
                _string(cell["studentHint"], f"{where}.studentHint", allow_empty=True)
                _string(cell["studentId"], f"{where}.studentId", allow_empty=True)
                _iso_date(cell["date"], f"{where}.date")
                _string(cell["raw"], f"{where}.raw", allow_empty=True)
                _enum(cell["value"], f"{where}.value", VALID_GRADE_VALUES)
                _enum(cell["entryType"], f"{where}.entryType", ENTRY_TYPES)
                _number(cell["confidence"], f"{where}.confidence", 0, 1)
                alternatives = _list(cell["alternatives"], f"{where}.alternatives", 0, 10)
                for alt_index, alternative in enumerate(alternatives):
                    _enum(alternative, f"{where}.alternatives[{alt_index}]", VALID_GRADE_VALUES)
                _string(cell["reason"], f"{where}.reason", allow_empty=True)
            warnings = _list(root["warnings"], "gemini.warnings", 0, 200)
            for index, warning in enumerate(warnings):
                _string(warning, f"gemini.warnings[{index}]", allow_empty=True)
            if "metadata" in root:
                _object(root["metadata"], "gemini.metadata", set(), None)
        except SchemaError as exc:
            raise ValueError(str(exc)) from exc
        normalized = clone_json(value)
        normalized["document"]["group"] = "108М"
        normalized["document"]["subject"] = subject
        return normalized

    def _call_model(self, model: str, image_bytes: bytes, prompt: str) -> dict[str, Any]:
        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_bytes).decode("ascii")}},
                ],
            }],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.config.gemini_api_key,
            "User-Agent": "108m-backend/1.0",
        }
        url = f"{GEMINI_API_BASE}/{quote(model, safe='')}:generateContent"
        estimated = min(50_000, 4_000 + len(image_bytes) // 1024 * 60)
        last_error: Exception | None = None
        for attempt in range(3):
            self.quota.reserve(estimated)
            try:
                status, response_headers, payload = self.http("POST", url, headers, json.dumps(body).encode(), 90)
            except ExternalServiceError:
                raise
            if status == 200:
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                    candidates = parsed.get("candidates", [])
                    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
                    text = "".join(str(part.get("text", "")) for part in parts)
                except (ValueError, UnicodeDecodeError, KeyError, IndexError, TypeError) as exc:
                    raise ValueError("Gemini вернул неожиданный ответ") from exc
                return self.extract_json(text)
            if status in (429, 503):
                last_error = ExternalServiceError(f"Gemini HTTP {status}")
                if attempt == 2:
                    break
                delay = 5.0
                retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = max(1.0, (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds())
                        except (TypeError, ValueError):
                            delay = 5.0
                self.sleeper(max(1.0, min(30.0, delay)))
                continue
            if status in (400, 404, 422):
                raise UserFacingError("Модель Gemini недоступна. Бот попробует следующую модель.")
            if status in (401, 403):
                raise UserFacingError("Gemini отклонил API-ключ. Проверьте GEMINI_API_KEY.")
            raise ExternalServiceError(f"Gemini вернул HTTP {status}.")
        raise ExternalServiceError(str(last_error or "Gemini недоступен"))

    def recognise(
        self,
        photo: PhotoInput,
        students_doc: dict[str, Any],
        schedule_doc: dict[str, Any],
        rotation: int = 0,
    ) -> GeminiResult:
        image_bytes = self._image_bytes(photo, rotation)
        prompt = self._prompt(students_doc, schedule_doc)
        last_error: Exception | None = None
        for model in self.config.gemini_models:
            try:
                response = self._call_model(model, image_bytes, prompt)
                validated = self.validate_response(response)
                return GeminiResult(validated, model, rotation)
            except UserFacingError as exc:
                message = str(exc)
                if "API-ключ" in message:
                    raise
                last_error = exc
                continue
            except (ValueError, ExternalServiceError) as exc:
                last_error = exc
                continue
        if isinstance(last_error, UserFacingError):
            raise last_error
        raise UserFacingError("Gemini не смог распознать страницу. Проверьте фото и повторите позже.")

    def recognise_with_fallback(
        self,
        photo: PhotoInput,
        students_doc: dict[str, Any],
        schedule_doc: dict[str, Any],
    ) -> GeminiResult:
        attempts = [0, 90, 270]
        best: GeminiResult | None = None
        last_error: Exception | None = None
        for rotation in attempts:
            try:
                result = self.recognise(photo, students_doc, schedule_doc, rotation)
            except QuotaExceeded:
                raise
            except UserFacingError as exc:
                last_error = exc
                continue
            cells = result.response.get("cells", [])
            if cells:
                confidences = [float(cell.get("confidence", 0) or 0) for cell in cells]
                average = sum(confidences) / len(confidences)
                if best is None or len(cells) > len(best.response.get("cells", [])) or average > 0.6:
                    best = result
                if average >= 0.55:
                    return result
            elif best is None:
                best = result
        if best is not None:
            return best
        if last_error is not None:
            raise last_error
        raise UserFacingError("Gemini не смог распознать страницу.")


class GradeService:
    @staticmethod
    def _subject(document: dict[str, Any], schedule_doc: dict[str, Any]) -> tuple[str, str]:
        catalog, aliases = HomeworkService._catalog_with_aliases(schedule_doc)
        subject_id = str(document.get("subjectId", "") or "")
        if subject_id in catalog:
            return subject_id, catalog[subject_id]["subject"]
        key = normalize_text(document.get("subject", ""))
        if key in aliases:
            resolved = aliases[key]
            return resolved, catalog[resolved]["subject"]
        if key:
            for catalog_id, item in catalog.items():
                if difflib.SequenceMatcher(None, key, item["key"]).ratio() >= 0.85:
                    return catalog_id, item["subject"]
        raise UserFacingError(
            f"Предмет «{document.get('subject', '')}» не найден в расписании. Оценки этой страницы не сохранены."
        )

    @staticmethod
    def _student(student_id: Any, hint: Any, students_doc: dict[str, Any]) -> dict[str, Any] | None:
        students = students_doc.get("students", [])
        by_id = {str(student.get("studentId")): student for student in students}
        if str(student_id or "") in by_id:
            return by_id[str(student_id)]
        key = normalize_text(hint or "")
        if not key:
            return None
        exact: list[dict[str, Any]] = []
        for student in students:
            name = normalize_text(student.get("name", ""))
            aliases = [normalize_text(alias) for alias in student.get("aliases", [])]
            if key == name or key in aliases or (len(name) >= 3 and name in key):
                exact.append(student)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
        scored = [
            (difflib.SequenceMatcher(None, key, normalize_text(student.get("name", ""))).ratio(), student)
            for student in students
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and scored[0][0] >= 0.88 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
            return scored[0][1]
        return None

    def merge_into(
        self,
        grades_doc: dict[str, Any],
        result: dict[str, Any],
        model: str,
        photo_hash: str,
        students_doc: dict[str, Any],
        schedule_doc: dict[str, Any],
        review_sink: list[dict[str, Any]],
        review_ids: set[str],
        recognised_at: str | None = None,
    ) -> MergeStats:
        stats = MergeStats()
        subject_id, subject = self._subject(result.get("document", {}), schedule_doc)
        now = recognised_at or iso_now()
        today = today_moscow()
        grades = grades_doc.setdefault("grades", [])
        for cell in result.get("cells", []):
            student = self._student(cell.get("studentId"), cell.get("studentHint"), students_doc)
            lesson_date = safe_date(cell.get("date"))
            if student is None or lesson_date is None:
                stats.unresolved += 1
                continue
            if lesson_date > today + timedelta(days=7) or lesson_date < today - timedelta(days=370):
                stats.unresolved += 1
                continue
            value = str(cell.get("value", ""))
            entry_type = str(cell.get("entryType", "daily"))
            if value not in VALID_GRADE_VALUES or entry_type not in ENTRY_TYPES:
                stats.unresolved += 1
                continue
            try:
                confidence = max(0.0, min(1.0, float(cell.get("confidence", 0) or 0)))
            except (TypeError, ValueError):
                stats.unresolved += 1
                continue
            raw = str(cell.get("raw", "") or "")
            ambiguous_handwriting = normalize_text(raw) in {"н", "h"} and value == "4"
            if ambiguous_handwriting:
                confidence = min(confidence, 0.5)
            if confidence < 0.75:
                stats.low += 1
            status = "automatic" if confidence >= 0.9 and not ambiguous_handwriting else "pending"
            student_id = str(student["studentId"])
            grade_id = stable_hash(f"{student_id}|{subject_id}|{lesson_date.isoformat()}|{entry_type}")[:24]
            candidate = {
                "gradeId": grade_id,
                "studentId": student_id,
                "subjectId": subject_id,
                "subject": subject,
                "lessonDate": lesson_date.isoformat(),
                "entryType": entry_type,
                "value": value,
                "raw": raw or value,
                "confidence": confidence,
                "source": "gemini",
                "sourceHash": photo_hash,
                "model": model,
                "promptVersion": PROMPT_VERSION,
                "recognitionSchemaVersion": RECOGNITION_SCHEMA_VERSION,
                "recognisedAt": now,
                "status": status,
                "evidence": [photo_hash],
            }
            existing = next((grade for grade in grades if grade.get("gradeId") == grade_id), None)
            if existing is None:
                grades.append(candidate)
                stats.added += 1
                stats.changed = True
                continue
            if existing.get("value") == value:
                evidence = existing.setdefault("evidence", [])
                if photo_hash not in evidence:
                    evidence.append(photo_hash)
                    stats.changed = True
                if float(existing.get("confidence", 0) or 0) < confidence:
                    existing["confidence"] = confidence
                    stats.changed = True
                if existing.get("status") == "pending" and status == "automatic":
                    existing["status"] = "automatic"
                    stats.changed = True
                stats.same += 1
                continue
            review_id = stable_hash(f"{grade_id}|{existing.get('value')}|{value}")[:24]
            old_status = existing.get("status")
            if review_id not in review_ids:
                review = {
                    "reviewId": review_id,
                    "gradeId": grade_id,
                    "studentId": student_id,
                    "subjectId": subject_id,
                    "subject": subject,
                    "lessonDate": lesson_date.isoformat(),
                    "entryType": entry_type,
                    "oldValue": existing.get("value"),
                    "newValue": value,
                    "oldConfidence": float(existing.get("confidence", 0) or 0),
                    "newConfidence": confidence,
                    "reason": (
                        "Рукописный символ похож на Н и 4; нужно подтверждение."
                        if ambiguous_handwriting
                        else "На новом фото другое значение. Старое значение сохранено до подтверждения."
                    ),
                    "status": "pending",
                    "createdAt": now,
                    "candidate": candidate,
                }
                review_sink.append(review)
                review_ids.add(review_id)
                stats.changed = True
            if old_status != "confirmed" and old_status != "conflict":
                existing["status"] = "conflict"
                stats.changed = True
            stats.conflicts += 1
        return stats

    @staticmethod
    def pending_reviews(documents: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            item for item in documents["reviews.json"].get("items", [])
            if item.get("status") == "pending"
        ]

    @staticmethod
    def resolve_review_in_docs(
        grades_doc: dict[str, Any],
        reviews_doc: dict[str, Any],
        review_id: str,
        accept: bool,
        user_id: int,
        now: str | None = None,
    ) -> bool:
        review = next(
            (item for item in reviews_doc.get("items", []) if item.get("reviewId") == review_id and item.get("status") == "pending"),
            None,
        )
        if review is None:
            return False
        resolved_at = now or iso_now()
        review["status"] = "confirmed" if accept else "rejected"
        review["resolvedAt"] = resolved_at
        review["resolvedBy"] = user_id
        grade = next((item for item in grades_doc.get("grades", []) if item.get("gradeId") == review.get("gradeId")), None)
        if accept:
            candidate = clone_json(review["candidate"])
            candidate.update({
                "status": "confirmed",
                "confirmedBy": user_id,
                "confirmedAt": resolved_at,
            })
            if grade is None:
                grades_doc.setdefault("grades", []).append(candidate)
            else:
                grade.clear()
                grade.update(candidate)
        elif grade is not None:
            grade["status"] = "confirmed"
            grade["confirmedBy"] = user_id
            grade["confirmedAt"] = resolved_at
        return True

    def resolve_review(self, storage: GitHubStorage, review_id: str, accept: bool, user_id: int) -> bool:
        result_holder: dict[str, bool] = {}

        def mutate(documents: dict[str, dict[str, Any]]) -> None:
            result_holder["ok"] = self.resolve_review_in_docs(
                documents["grades.json"],
                documents["reviews.json"],
                review_id,
                accept,
                user_id,
            )

        storage.publish_all(
            mutate,
            f"review-{review_id}-{'yes' if accept else 'no'}",
            "resolve journal review",
        )
        return result_holder.get("ok", False)


# =============================================================================
# Telegram-инфраструктура
# =============================================================================


class UpdateGuard:
    """Не даёт обработать один Telegram update дважды, включая параллельную доставку."""

    def __init__(self, local: LocalState):
        self.local = local
        self.lock = asyncio.Lock()
        self.inflight: set[int] = set()

    async def claim(self, update_id: int | None) -> bool:
        if update_id is None:
            return True
        async with self.lock:
            if update_id in self.inflight or self.local.has_update(update_id):
                return False
            self.inflight.add(update_id)
            return True

    def complete(self, update_id: int | None) -> None:
        if update_id is None:
            return
        self.inflight.discard(update_id)
        self.local.mark_update(update_id)

    def release(self, update_id: int | None) -> None:
        if update_id is not None:
            self.inflight.discard(update_id)


@dataclass
class PhotoBatch:
    user_id: int
    chat_id: int
    photos: list[PhotoInput] = field(default_factory=list)
    mode: str = "journal"
    caption: str = ""
    timer: asyncio.Task | None = None
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    ack_sent: bool = False


@dataclass
class PhotoOutcome:
    pages: int = 0
    added: int = 0
    same: int = 0
    low: int = 0
    conflicts: int = 0
    unresolved: int = 0
    failed: int = 0
    skipped: int = 0
    quota_message: str = ""
    models: tuple[str, ...] = ()

    def summary(self) -> str:
        lines = [
            f"Обработано страниц: {self.pages}",
            f"Новых оценок: {self.added}",
            f"Совпало с существующими: {self.same}",
            f"Низкая уверенность: {self.low}",
            f"Конфликтов: {self.conflicts}",
            f"Не распознано: {self.failed}",
        ]
        if self.unresolved:
            lines.append(f"Клеток без ученика или даты: {self.unresolved}")
        if self.skipped:
            lines.append(f"Пропущено уже обработанных фото: {self.skipped}")
        if self.quota_message:
            lines.append(self.quota_message)
        if self.pages == 0 and self.failed == 0:
            lines.append("Страницы не содержали оценок; опубликованные данные не изменены.")
        return "\n".join(lines)


class Backend:
    def __init__(
        self,
        config: Config,
        storage: GitHubStorage | None = None,
        local: LocalState | None = None,
        quota: Quota | None = None,
        gemini: GeminiClient | None = None,
    ):
        self.config = config
        self.local = local or LocalState(config.bot_state_file)
        self.quota = quota or Quota(
            self.local,
            config.gemini_rpm_limit,
            config.gemini_rpd_limit,
            config.gemini_tpm_limit,
        )
        self.storage = storage or GitHubStorage(config)
        self.schedule = ScheduleService()
        self.homework = HomeworkService(self.storage, self.schedule)
        self.duties = DutyService(self.storage)
        self.grades = GradeService()
        self.gemini = gemini or GeminiClient(config, self.quota)
        self.update_guard = UpdateGuard(self.local)
        self.started_at = now_moscow()
        self.batches: dict[tuple[int, str], PhotoBatch] = {}
        self.batch_lock = asyncio.Lock()
        self.scheduler: BotScheduler | None = None

    @property
    def secrets(self) -> tuple[str, ...]:
        return (
            self.config.telegram_token,
            self.config.github_token,
            self.config.gemini_api_key,
            self.config.vk_access_token or "",
        )

    def is_allowed(self, update: Any) -> bool:
        user = getattr(update, "effective_user", None)
        if user is None:
            return False
        try:
            return int(user.id) in self.config.allowed_telegram_ids
        except (TypeError, ValueError):
            return False

    async def read_documents(self) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        return await asyncio.to_thread(self.storage.read_documents)

    async def read_bundle(self) -> Bundle:
        return await asyncio.to_thread(self.storage.read_bundle)

    def queue_size(self) -> int:
        return len(self.batches)

    def process_photos(self, photos: list[PhotoInput]) -> PhotoOutcome:
        unique: list[PhotoInput] = []
        seen: set[str] = set()
        for photo in photos:
            if photo.sha256 not in seen:
                seen.add(photo.sha256)
                unique.append(photo)
        outcome = PhotoOutcome()
        if not unique:
            return outcome
        documents, _ = self.storage.read_documents()
        recognised: list[RecognisedPage] = []
        models: set[str] = set()
        for photo in unique:
            if self.local.image_done(photo.sha256):
                outcome.skipped += 1
                continue
            try:
                result = self.gemini.recognise_with_fallback(
                    photo,
                    documents["students.json"],
                    documents["schedule.json"],
                )
            except QuotaExceeded as exc:
                outcome.quota_message = str(exc)
                break
            except UserFacingError as exc:
                outcome.failed += 1
                LOG.warning("recognition failed hash=%s: %s", photo.sha256[:12], safe_exception(exc, self.secrets))
                continue
            except ExternalServiceError as exc:
                outcome.failed += 1
                LOG.warning("recognition service failed hash=%s: %s", photo.sha256[:12], safe_exception(exc, self.secrets))
                continue
            except Exception as exc:
                outcome.failed += 1
                LOG.error("recognition crashed hash=%s: %s", photo.sha256[:12], safe_exception(exc, self.secrets))
                continue
            recognised.append(RecognisedPage(photo, result))
            models.add(result.model)

        if recognised:
            recognised_at = iso_now()
            stats_holder: dict[str, MergeStats] = {"stats": MergeStats()}

            def mutate(all_documents: dict[str, dict[str, Any]]) -> None:
                review_sink: list[dict[str, Any]] = []
                review_ids = {
                    str(item.get("reviewId"))
                    for item in all_documents["reviews.json"].get("items", [])
                }
                aggregate = MergeStats()
                for page in recognised:
                    page_stats = self.grades.merge_into(
                        all_documents["grades.json"],
                        page.result.response,
                        page.result.model,
                        page.photo.sha256,
                        documents["students.json"],
                        documents["schedule.json"],
                        review_sink,
                        review_ids,
                        recognised_at=recognised_at,
                    )
                    aggregate.added += page_stats.added
                    aggregate.same += page_stats.same
                    aggregate.low += page_stats.low
                    aggregate.conflicts += page_stats.conflicts
                    aggregate.unresolved += page_stats.unresolved
                    aggregate.changed = aggregate.changed or page_stats.changed
                existing_reviews = {
                    str(item.get("reviewId"))
                    for item in all_documents["reviews.json"].get("items", [])
                }
                for review in review_sink:
                    if review["reviewId"] not in existing_reviews:
                        all_documents["reviews.json"].setdefault("items", []).append(review)
                        existing_reviews.add(review["reviewId"])
                        aggregate.changed = True
                stats_holder["stats"] = aggregate

            event_id = "batch-" + stable_hash("|".join(sorted(page.photo.sha256 for page in recognised)))[:24]
            self.storage.publish_all(mutate, event_id, "merge journal grades")
            stats = stats_holder["stats"]
            outcome.pages = len(recognised)
            outcome.added = stats.added
            outcome.same = stats.same
            outcome.low = stats.low
            outcome.conflicts = stats.conflicts
            outcome.unresolved = stats.unresolved
            for page in recognised:
                self.local.mark_image(page.photo.sha256)
        outcome.models = tuple(sorted(models))
        return outcome

    def save_homework_photos(
        self,
        caption: str,
        user_id: int,
        update_id: int | None,
        assigned: date,
        photos: list[PhotoInput],
        documents: dict[str, dict[str, Any]],
    ) -> HomeworkResult:
        if not photos:
            raise UserFacingError("Не получил изображение.")
        unique: list[PhotoInput] = []
        seen: set[str] = set()
        for photo in photos:
            if photo.sha256 not in seen:
                seen.add(photo.sha256)
                unique.append(photo)
        if len(unique) > 6:
            raise UserFacingError("К одному заданию можно приложить не больше 6 изображений.")
        attachments: list[dict[str, Any]] = []
        for photo in unique:
            data, mime, ext, width, height = GeminiClient.homework_image(photo)
            digest = hashlib.sha256(data).hexdigest()
            url = self.storage.upload_media(data, ext, digest)
            attachments.append({
                "attachmentId": "att-" + digest[:20],
                "url": url,
                "fileName": f"homework-{digest[:12]}.{ext}",
                "mimeType": mime,
                "size": len(data),
                "width": width,
                "height": height,
                "sha256": digest,
                "createdAt": iso_now(),
            })
        return self.homework.add(
            caption,
            user_id,
            update_id,
            assigned,
            documents,
            attachments=attachments,
            allow_empty_text=True,
        )

    def import_replacements(
        self,
        pdf_bytes: bytes,
        source: str,
        source_url: str = "",
        fallback_date: date | None = None,
    ) -> ReplacementImportResult:
        source_hash = hashlib.sha256(pdf_bytes).hexdigest()
        text = ReplacementParser.extract_text(pdf_bytes)
        documents, _ = self.storage.read_documents()
        parsed = ReplacementParser.parse(
            text,
            documents["schedule.json"],
            source,
            source_hash,
            source_url=source_url,
            fallback_date=fallback_date,
        )
        if parsed.status == "no-108m":
            parsed.message = "В PDF нет строк для группы 108М. Замены не изменены."
            return parsed
        holder: dict[str, Any] = {"changed": False, "recalculated": 0}

        def mutate(all_documents: dict[str, dict[str, Any]]) -> None:
            items = all_documents["replacements.json"].setdefault("items", [])
            before = json_dumps_stable(items)
            existing_by_id = {str(item.get("replacementId")): item for item in items}
            for row in parsed.rows:
                previous = existing_by_id.get(row["replacementId"])
                if previous and all(
                    previous.get(field) == row.get(field)
                    for field in ("date", "weekType", "pairNumber", "newSubjectId", "newSubject", "newRoom", "cancelled", "sourceHash")
                ):
                    row["receivedAt"] = previous.get("receivedAt", row["receivedAt"])
            fresh_ids = {row["replacementId"] for row in parsed.rows}
            kept = [item for item in items if item.get("replacementId") not in fresh_ids]
            items[:] = kept + parsed.rows
            items.sort(key=lambda item: (str(item.get("date", "")), int(item.get("pairNumber", 0))))
            holder["changed"] = json_dumps_stable(items) != before
            holder["recalculated"] = self.homework.recalculate(all_documents, today_moscow())
            if holder["recalculated"]:
                holder["changed"] = True

        event_id = "replacements-" + source_hash[:24]
        result = self.storage.publish_all(mutate, event_id, "import replacements PDF")
        parsed.changed = bool(holder["changed"])
        parsed.recalculated = int(holder["recalculated"])
        parsed.message = (
            f"Замены на {date_label_full(parsed.target_date)} опубликованы: {len(parsed.rows)}."
            if parsed.changed
            else f"Замены на {date_label_full(parsed.target_date)} уже актуальны."
        )
        return parsed

    def import_replacements_from_vk(self, target: date) -> ReplacementImportResult:
        if not self.config.vk_enabled:
            return ReplacementImportResult(
                "vk-disabled",
                target_date=target,
                message="VK не настроен: перешлите PDF замен в бота вручную.",
            )
        client = VKClient(self.config)
        found = client.find_post(target)
        if found is None:
            return ReplacementImportResult(
                "vk-not-found",
                target_date=target,
                message="В VK не нашёл пост с PDF замен на нужную дату.",
            )
        url, title = found
        pdf_bytes = client.download_pdf(url)
        return self.import_replacements(
            pdf_bytes,
            source=f"VK: {title}"[:120],
            source_url=url if url.startswith("https://") else "",
            fallback_date=target,
        )

    async def add_photo(
        self,
        user_id: int,
        chat_id: int,
        photo: PhotoInput,
        bot: Any,
        caption: str = "",
        homework: bool = False,
    ) -> tuple[str, int, str]:
        mode = "homework" if homework else "journal"
        async with self.batch_lock:
            now = time.time()
            if not homework and not caption:
                # Фото без подписи сразу после ДЗ-фото — продолжение задания.
                homework_batch = self.batches.get((user_id, "homework"))
                if homework_batch is not None and now - homework_batch.last_update <= BATCH_MAX_AGE_SECONDS:
                    mode = "homework"
                    caption = homework_batch.caption
            key = (user_id, mode)
            batch = self.batches.get(key)
            if batch is not None and now - batch.last_update > BATCH_MAX_AGE_SECONDS:
                if batch.timer is not None:
                    batch.timer.cancel()
                batch = None
            if batch is None:
                batch = PhotoBatch(user_id=user_id, chat_id=chat_id, mode=mode, caption=caption)
                self.batches[key] = batch
            if mode == "homework":
                batch.caption = caption or batch.caption
            if len(batch.photos) >= MAX_BATCH_FILES:
                return "limit", len(batch.photos), batch.mode
            if any(item.sha256 == photo.sha256 for item in batch.photos):
                return "duplicate", len(batch.photos), batch.mode
            batch.photos.append(photo)
            batch.last_update = now
            if batch.timer is not None:
                batch.timer.cancel()
            batch.timer = asyncio.create_task(self._finish_batch(user_id, bot), name=f"batch-{user_id}-{mode}")
            return "added", len(batch.photos), batch.mode

    async def _finish_batch(self, user_id: int, bot: Any) -> None:
        try:
            await asyncio.sleep(BATCH_IDLE_SECONDS)
        except asyncio.CancelledError:
            return
        async with self.batch_lock:
            candidates = [
                (key, batch)
                for key, batch in self.batches.items()
                if key[0] == user_id
            ]
            if not candidates:
                return
            # Таймер один на пользователя; завершаем только «созревшие» батчи.
            ready: list[tuple[tuple[int, str], PhotoBatch]] = []
            for key, batch in candidates:
                if time.time() - batch.last_update < BATCH_IDLE_SECONDS:
                    if batch.timer is not None:
                        batch.timer.cancel()
                    batch.timer = asyncio.create_task(self._finish_batch(user_id, bot), name=f"batch-{user_id}-{key[1]}")
                    continue
                ready.append((key, batch))
                self.batches.pop(key, None)
            if not ready:
                return
        for _, batch in ready:
            await self._finish_one_batch(batch, bot)

    async def _finish_one_batch(self, batch: PhotoBatch, bot: Any) -> None:
        if batch.mode == "homework":
            try:
                documents, _ = await asyncio.to_thread(self.storage.read_documents)
                result = await asyncio.to_thread(
                    self.save_homework_photos,
                    batch.caption,
                    batch.user_id,
                    None,
                    today_moscow(),
                    batch.photos,
                    documents,
                )
                await bot.send_message(
                    batch.chat_id,
                    _homework_reply(result, documents["schedule.json"]),
                    disable_web_page_preview=True,
                )
            except UserFacingError as exc:
                await bot.send_message(batch.chat_id, str(exc))
            except StorageBusyError:
                await bot.send_message(batch.chat_id, "Публикация данных не завершена. Фото не изменены; отправьте позже.")
            except StorageError:
                await bot.send_message(batch.chat_id, "Опубликованные данные повреждены. Фото не изменены.")
            except ExternalServiceError:
                await bot.send_message(batch.chat_id, "Не удалось загрузить вложение в GitHub. Фото не изменены; повторите позже.")
            except Exception as exc:
                LOG.error("homework photo batch failed: %s", safe_exception(exc, self.secrets))
                await bot.send_message(batch.chat_id, "Не удалось сохранить фото задания. Данные не изменены.")
            return
        try:
            outcome = await asyncio.to_thread(self.process_photos, batch.photos)
            await bot.send_message(batch.chat_id, outcome.summary(), disable_web_page_preview=True)
        except UserFacingError as exc:
            await bot.send_message(batch.chat_id, str(exc))
        except StorageBusyError:
            await bot.send_message(batch.chat_id, "Публикация данных не завершена. Фото не изменены; отправьте позже.")
        except StorageError:
            await bot.send_message(batch.chat_id, "Опубликованные данные повреждены. Фото не изменены.")
        except ExternalServiceError:
            await bot.send_message(batch.chat_id, "Внешний сервис недоступен. Фото не изменены; повторите позже.")
        except Exception as exc:
            LOG.error("photo batch failed: %s", safe_exception(exc, self.secrets))
            await bot.send_message(batch.chat_id, "Не удалось обработать фотографии. Опубликованные данные не изменены.")

    async def cancel_batch(self, user_id: int) -> int:
        async with self.batch_lock:
            keys = [key for key in self.batches if key[0] == user_id]
            count = 0
            for key in keys:
                batch = self.batches.pop(key, None)
                if batch is None:
                    continue
                if batch.timer is not None:
                    batch.timer.cancel()
                count += len(batch.photos)
            return count


backend: Backend | None = None


def require_backend() -> Backend:
    if backend is None:
        raise RuntimeError("backend не инициализирован")
    return backend


def _message_of(update: Any) -> Any:
    return getattr(update, "effective_message", None)


async def reply_text(update: Any, text: str) -> None:
    message = _message_of(update)
    if message is None:
        return
    for chunk in chunk_lines(str(text).splitlines()):
        try:
            await message.reply_text(chunk, disable_web_page_preview=True)
        except Exception as exc:  # sending must not crash the update loop
            LOG.warning("telegram reply failed: %s", safe_exception(exc, require_backend().secrets))


def authorized(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Allowlist + update_id guard + безопасные ответы на ошибки."""

    @functools.wraps(handler)
    async def wrapper(update: Any, context: Any) -> None:
        current = require_backend()
        if not current.is_allowed(update):
            LOG.warning("rejected telegram user outside allowlist")
            await reply_text(update, "Этот бот доступен только участникам группы 108М.")
            return
        update_id = getattr(update, "update_id", None)
        if not await current.update_guard.claim(update_id):
            return
        try:
            await handler(update, context)
        except UserFacingError as exc:
            await reply_text(update, str(exc))
        except StorageBusyError:
            await reply_text(update, "Публикация данных ещё не завершена. Повторите через минуту.")
        except StorageError as exc:
            LOG.error("storage error: %s", safe_exception(exc, current.secrets))
            await reply_text(update, "Опубликованные данные повреждены или недоступны. Запись остановлена, повторите позже.")
        except ExternalServiceError as exc:
            LOG.warning("external service error: %s", safe_exception(exc, current.secrets))
            await reply_text(update, "Внешний сервис недоступен. Данные не изменены; повторите позже.")
        except Exception as exc:  # no traceback with possible secrets
            LOG.error("handler failed: %s", safe_exception(exc, current.secrets))
            await reply_text(update, "Внутренняя ошибка. Данные не изменены; повторите позже.")
        finally:
            current.update_guard.complete(update_id)

    return wrapper


def _week_type_ru(value: str) -> str:
    return "знаменатель" if value == "denominator" else "числитель"


def _format_lesson(lesson: dict[str, Any], times: dict[int, dict[str, str]]) -> str:
    pair = int(lesson.get("pairNumber", 0))
    clock = times.get(pair, {})
    prefix = f"{pair}. {clock.get('start', '--:--')}–{clock.get('end', '--:--')} · {lesson.get('subject', '')}"
    parts = [prefix]
    if lesson.get("teacher"):
        parts.append(str(lesson["teacher"]))
    if lesson.get("room"):
        parts.append(f"каб. {lesson['room']}")
    if lesson.get("replacement"):
        source = "правило недели" if lesson.get("replacementKind") == "rule" else "замена"
        original = lesson.get("originalSubject")
        parts.append(f"{source}" + (f" вместо «{original}»" if original else ""))
    options = lesson.get("subgroupOptions") or []
    if options:
        parts.append(" / ".join(f"{item.get('id')}: каб. {item.get('room') or '—'}" for item in options))
    return " · ".join(str(part) for part in parts if part)


# =============================================================================
# Команды
# =============================================================================


@authorized
async def start_command(update: Any, context: Any) -> None:
    await reply_text(
        update,
        "Привет! Это дневник группы 108М.\n\n"
        "• Расписание, оценки, ДЗ, дежурства и замены — на сайте.\n"
        "• ДЗ: «Математика выучить логарифмы 10.09».\n"
        "• Фото к ДЗ: подпись «ДЗ Математика выучить логарифмы».\n"
        "• Дежурство: «Дежурили Абакаев, Бадреев, Осадчий».\n"
        "• Журнал: отправь фото или альбом без подписи.\n"
        "• Замены: перешли PDF из VK.\n\n"
        "/help — подробная инструкция.",
    )


@authorized
async def help_command(update: Any, context: Any) -> None:
    await reply_text(
        update,
        "Как пользоваться ботом 108М\n\n"
        "ДОМАШНЕЕ ЗАДАНИЕ\n"
        "Формат: «Предмет текст задания [дата]».\n"
        "Пример: «Математика выучить логарифмы 10.09».\n"
        "Дата может быть в конце: ДД.ММ, ДД.ММ.ГГ или ДД.ММ.ГГГГ. Без даты берётся дата сообщения. "
        "Задание появится на сайте под ближайшим уроком этого предмета строго после даты выдачи. "
        "Замены и чётность недели учитываются. Второе сообщение по тому же предмету добавляет ещё одно "
        "задание — оно показывается ниже первого. Одинаковый текст дважды не дублируется.\n\n"
        "ФОТО К ДОМАШНЕМУ ЗАДАНИЮ\n"
        "Отправь фото с подписью «ДЗ Математика выучить логарифмы». "
        "Можно приложить до 6 фото к одному заданию. Фото без подписи считаются фотографиями журнала.\n\n"
        "ДЕЖУРСТВА\n"
        "Сообщение: «Дежурили Абакаев, Бадреев, Осадчий».\n"
        "Можно фамилию, имя или ФИО. Бот засчитает только однозначно найденных людей. "
        "Если имя неоднозначное — попросит написать точнее и ничего не засчитает. "
        "Два отчёта за день объединяются: засчитаны будут все, кого называли.\n\n"
        "ПОЖЕЛАНИЯ ПО ПАРАМ\n"
        "/pair Абакаев Бадреев — сохранить пожелание дежурить вместе.\n"
        "/unpair Абакаев Бадреев — удалить пожелание.\n"
        "/pairs — показать сохранённые пары. Достаточно фамилий.\n\n"
        "ЖУРНАЛ\n"
        "Отправь фото страницы или Telegram-альбом без подписи. Бот сам определит момент окончания загрузки. "
        "Максимум 20 изображений в одной загрузке; повторное фото по hash не отправляется в Gemini. "
        "PDF и другие документы как журнал не принимаются.\n\n"
        "ЗАМЕНЫ\n"
        "Перешли PDF замен документом. Бот обработает только строки группы 108М. "
        "PDF не публикуется и не исполняется.\n\n"
        "КОМАНДЫ\n"
        "/today — расписание, замены и ДЗ на сегодня\n"
        "/grades — как смотреть оценки и сколько записей опубликовано\n"
        "/duties — последние дежурства\n"
        "/pair — добавить пожелание по паре дежурных\n"
        "/unpair — удалить пожелание\n"
        "/pairs — показать пожелания\n"
        "/review — спорные оценки\n"
        "/status — очередь, ревизия и квоты Gemini\n"
        "/cancel — отменить незавершённую загрузку фото\n"
        "/help — эта инструкция",
    )


@authorized
async def today_command(update: Any, context: Any) -> None:
    current = require_backend()
    documents, _ = await current.read_documents()
    day = today_moscow()
    schedule_doc = documents["schedule.json"]
    replacements_doc = documents["replacements.json"]
    week_type, lessons = current.schedule.effective_lessons(day, schedule_doc, replacements_doc)
    times = current.schedule.times_by_pair(schedule_doc)
    lines = [f"Сегодня, {date_label_full(day)} · {_week_type_ru(week_type)}"]
    if lessons:
        lines.append("")
        lines.extend(_format_lesson(lesson, times) for lesson in lessons)
    else:
        lines.append("Занятий нет.")
    replacements = [
        item for item in replacements_doc.get("items", [])
        if item.get("date") == day.isoformat() and not item.get("cancelled")
    ]
    if replacements:
        lines.append("")
        lines.append(f"Замены для 108М: {len(replacements)}")
        for item in replacements:
            lines.append(
                f"— {item.get('pairNumber')} пара: {item.get('newSubject')}"
                + (f", каб. {item['newRoom']}" if item.get("newRoom") else "")
                + f" (источник: {item.get('source', 'PDF')})"
            )
    tasks = [
        task for task in documents["tasks.json"].get("tasks", [])
        if task.get("nextLessonDate") == day.isoformat() and task.get("status") != "выполнено"
    ]
    if tasks:
        lines.append("")
        lines.append("Домашние задания к сегодняшним урокам:")
        for task in tasks:
            lines.append(f"— {task.get('subject')}: {task.get('text')}")
    await reply_text(update, "\n".join(lines))


@authorized
async def grades_command(update: Any, context: Any) -> None:
    current = require_backend()
    documents, manifest = await current.read_documents()
    grades = documents["grades.json"].get("grades", [])
    pending = len([item for item in grades if item.get("status") in ("pending", "conflict")])
    await reply_text(
        update,
        "Оценки смотри на сайте: раздел «Оценки» → выбери подгруппу и ученика.\n\n"
        f"Записей в журнале: {len(grades)}\n"
        f"Из них на проверке: {pending}\n"
        f"Ревизия данных: {manifest.get('revision', '—')}\n\n"
        "Низкоуверенные и конфликтные значения не заменяют подтверждённую оценку автоматически. "
        "Спорные записи можно посмотреть и подтвердить командой /review.",
    )


@authorized
async def status_command(update: Any, context: Any) -> None:
    current = require_backend()
    quota = current.quota.status()
    revision = "—"
    generated = "—"
    try:
        _, manifest = await current.read_documents()
        revision = str(manifest.get("revision", "—"))
        generated = str(manifest.get("generatedAt", "—"))
    except (StorageError, StorageBusyError, ExternalServiceError) as exc:
        LOG.warning("status read failed: %s", safe_exception(exc, current.secrets))
    lines = [
        f"Очередь фото: {current.queue_size()}",
        f"Ревизия: {revision}",
        f"Публикация: {generated}",
        f"Gemini: {quota['rpm_used']}/{quota['rpm_limit']} за минуту, "
        f"{quota['rpd_used']}/{quota['rpd_limit']} за день, "
        f"{quota['tpm_used']}/{quota['tpm_limit']} токенов за минуту",
        f"Модели: {', '.join(current.config.gemini_models)}",
        f"Время Москвы: {iso_now()}",
    ]
    await reply_text(update, "\n".join(lines))


@authorized
async def cancel_command(update: Any, context: Any) -> None:
    current = require_backend()
    user_id = int(update.effective_user.id)
    count = await current.cancel_batch(user_id)
    if count:
        await reply_text(update, f"Незавершённая загрузка отменена: {count} фото не обработано.")
    else:
        await reply_text(update, "Незавершённой загрузки фото нет.")


@authorized
async def review_command(update: Any, context: Any) -> None:
    current = require_backend()
    documents, _ = await current.read_documents()
    reviews = GradeService.pending_reviews(documents)
    if not reviews:
        await reply_text(update, "Спорных оценок нет. Все записи подтверждены или однозначны.")
        return
    students = {student["studentId"]: student.get("name", student["studentId"]) for student in documents["students.json"].get("students", [])}
    lines = ["Спорные оценки. Подтвердите новое значение или оставьте старое."]
    keyboard = []
    for review in reviews[:6]:
        lines.append(
            f"\n{review.get('lessonDate')} · {review.get('subject')} · {students.get(review.get('studentId'), '')}"
            f"\n{review.get('oldValue')} → {review.get('newValue')} · {review.get('reason')}"
        )
        keyboard.append([
            InlineKeyboardButton(
                f"Подтвердить {review.get('newValue')}",
                callback_data=f"review:yes:{review.get('reviewId')}",
            ),
            InlineKeyboardButton(
                f"Оставить {review.get('oldValue')}",
                callback_data=f"review:no:{review.get('reviewId')}",
            ),
        ])
    if len(reviews) > 6:
        lines.append(f"\nПоказаны первые 6 из {len(reviews)}. Остальные появятся после решения этих.")
    message = _message_of(update)
    if message is not None:
        await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)


@authorized
async def review_callback(update: Any, context: Any) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    try:
        await query.answer()
    except Exception:
        pass
    current = require_backend()
    try:
        _, action, review_id = str(query.data or "").split(":", 2)
        if action not in ("yes", "no") or not re.fullmatch(r"[A-Za-z0-9]{8,64}", review_id):
            raise ValueError("bad callback")
        ok = await asyncio.to_thread(
            current.grades.resolve_review,
            current.storage,
            review_id,
            action == "yes",
            int(update.effective_user.id),
        )
        text = "Решение сохранено." if ok else "Эта запись уже решена."
        try:
            await query.edit_message_text(text)
        except Exception:
            await reply_text(update, text)
    except UserFacingError as exc:
        try:
            await query.edit_message_text(str(exc))
        except Exception:
            await reply_text(update, str(exc))
    except Exception as exc:
        LOG.error("review callback failed: %s", safe_exception(exc, current.secrets))
        try:
            await query.edit_message_text("Не удалось сохранить решение. Повторите /review.")
        except Exception:
            pass


@authorized
async def handle_image(update: Any, context: Any) -> None:
    current = require_backend()
    message = _message_of(update)
    if message is None:
        return
    caption = str(getattr(message, "caption", "") or "").strip()
    homework_match = HOMEWORK_CAPTION_RE.match(caption) if caption else None
    is_homework = homework_match is not None
    homework_caption = caption[homework_match.end():].strip() if homework_match else ""
    if is_homework and not homework_caption:
        await reply_text(
            update,
            "Укажи в подписи предмет и задание, например: "
            "«ДЗ Математика выучить логарифмы». Так фото привяжется к ДЗ.",
        )
        return
    document = getattr(message, "document", None)
    if document is not None:
        mime = (getattr(document, "mime_type", "") or "").lower()
        if not mime.startswith("image/"):
            await reply_text(update, "Для журнала принимаются только изображения, не PDF и не другие документы.")
            return
        if mime == "image/jpg":
            mime = "image/jpeg"
        file_id = document.file_id
        file_name = document.file_name or "journal-image"
        file_size = int(document.file_size or 0)
    else:
        photos = getattr(message, "photo", None) or []
        if not photos:
            return
        telegram_photo = photos[-1]
        file_id = telegram_photo.file_id
        file_name = "telegram-photo.jpg"
        file_size = int(telegram_photo.file_size or 0)
        mime = "image/jpeg"
    if file_size > MAX_IMAGE_BYTES:
        await reply_text(update, "Изображение слишком большое (максимум 12 МБ).")
        return
    try:
        telegram_file = await context.bot.get_file(file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
    except Exception as exc:
        LOG.warning("telegram file download failed: %s", safe_exception(exc, current.secrets))
        await reply_text(update, "Не удалось скачать изображение из Telegram. Попробуйте ещё раз.")
        return
    if len(raw) > MAX_IMAGE_BYTES:
        await reply_text(update, "Изображение слишком большое (максимум 12 МБ).")
        return
    photo = PhotoInput(raw, mime, file_name, hashlib.sha256(raw).hexdigest())
    if not is_homework and current.local.image_done(photo.sha256):
        await reply_text(update, "Это фото уже было обработано; повторно в Gemini не отправляю.")
        return
    status, count, mode = await current.add_photo(
        int(update.effective_user.id),
        int(message.chat_id),
        photo,
        context.bot,
        caption=homework_caption,
        homework=is_homework,
    )
    if status == "limit":
        await reply_text(update, f"В одной загрузке максимум {MAX_BATCH_FILES} изображений.")
    elif status == "duplicate":
        await reply_text(update, "Это фото уже в текущей очереди — второй раз не добавляю.")
    elif count == 1:
        if mode == "homework":
            await reply_text(update, "Фото принято. Сохраню его как вложение к домашнему заданию после короткой паузы.")
        else:
            await reply_text(update, "Фото принято. Обработаю после короткой паузы, чтобы дождаться остальных фото альбома.")


@authorized
async def handle_document(update: Any, context: Any) -> None:
    current = require_backend()
    message = _message_of(update)
    document = getattr(message, "document", None)
    if document is None:
        return
    mime = str(getattr(document, "mime_type", "") or "").lower()
    file_name = str(getattr(document, "file_name", "") or "").lower()
    is_pdf = mime == "application/pdf" or file_name.endswith(".pdf")
    if not is_pdf:
        await reply_text(
            update,
            "Документ не похож на PDF замен. Пришлите PDF файлом или фото журнала как изображение.",
        )
        return
    file_size = int(getattr(document, "file_size", 0) or 0)
    if file_size > MAX_PDF_BYTES:
        await reply_text(update, "PDF слишком большой (максимум 10 МБ).")
        return
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        raw = bytes(await telegram_file.download_as_bytearray())
    except Exception as exc:
        LOG.warning("telegram pdf download failed: %s", safe_exception(exc, current.secrets))
        await reply_text(update, "Не удалось скачать PDF из Telegram. Попробуйте ещё раз.")
        return
    if len(raw) > MAX_PDF_BYTES:
        await reply_text(update, "PDF слишком большой (максимум 10 МБ).")
        return
    await reply_text(update, "PDF получен. Разбираю замены только для группы 108М…")
    try:
        result = await asyncio.to_thread(
            current.import_replacements,
            raw,
            "Telegram forwarded PDF",
            "",
            _message_day(update),
        )
    except UserFacingError as exc:
        await reply_text(update, str(exc))
        return
    if result.status == "no-108m" or not result.rows:
        await reply_text(
            update,
            f"В этом PDF нет строк для 108М на {date_label_full(result.target_date)}. "
            "Расписание не изменено.",
        )
        return
    lines = [result.message]
    for row in result.rows:
        subject = row.get("newSubject") or "—"
        room = f", каб. {row['newRoom']}" if row.get("newRoom") else ""
        mark = " (отмена)" if row.get("cancelled") else ""
        lines.append(f"— {row['pairNumber']} пара: {subject}{room}{mark}")
    if result.recalculated:
        lines.append(f"Пересчитаны домашние задания: {result.recalculated}.")
    await reply_text(update, "\n".join(lines))


@authorized
async def duties_command(update: Any, context: Any) -> None:
    current = require_backend()
    documents, _ = await current.read_documents()
    await reply_text(update, DutyService.describe_recent(documents, limit=10))


@authorized
async def pairs_command(update: Any, context: Any) -> None:
    current = require_backend()
    documents, _ = await current.read_documents()
    await reply_text(update, DutyService.describe_pair_preferences(documents))


async def _change_pair(update: Any, context: Any, remove: bool) -> None:
    current = require_backend()
    tokens = list(getattr(context, "args", None) or [])
    if not tokens:
        await reply_text(update, "Укажи две фамилии: /pair Абакаев Бадреев")
        return
    documents, _ = await current.read_documents()
    try:
        names, changed = await asyncio.to_thread(
            current.duties.update_pair,
            tokens,
            documents["students.json"],
            documents,
            remove,
        )
    except UserFacingError as exc:
        await reply_text(update, str(exc))
        return
    pair_text = f"{names[0]} + {names[1]}"
    if not changed:
        await reply_text(update, f"Такая пара уже {'отсутствует' if remove else 'сохранена'}: {pair_text}.")
        return
    await reply_text(
        update,
        f"Пара {'удалена' if remove else 'сохранена'}: {pair_text}.\n"
        "Бот учтёт пожелание при следующем выборе дежурных."
    )


@authorized
async def pair_command(update: Any, context: Any) -> None:
    await _change_pair(update, context, remove=False)


@authorized
async def unpair_command(update: Any, context: Any) -> None:
    await _change_pair(update, context, remove=True)


def _message_day(update: Any) -> date:
    message = _message_of(update)
    value = getattr(message, "date", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(TZ).date()
    return today_moscow()


def _homework_reply(result: HomeworkResult, schedule_doc: dict[str, Any]) -> str:
    draft = result.draft
    if result.status == "attached":
        lines = [f"Добавил фото к ДЗ: {draft.subject} — {draft.text}"]
    elif result.status == "duplicate":
        if result.attachment_added:
            lines = [f"Такое ДЗ уже есть: {draft.subject} — {draft.text}"]
        else:
            lines = [f"Такое ДЗ уже есть: {draft.subject} — {draft.text}"]
    else:
        lines = [f"Добавил ДЗ: {draft.subject} — {draft.text}"]
    lines.append("Следующий урок: " + HomeworkService.describe(draft, schedule_doc) + ".")
    if result.status == "added" and result.siblings:
        lines.append(
            f"По этому предмету на урок уже {result.siblings + 1} "
            f"{plural_ru(result.siblings + 1, 'задание', 'задания', 'заданий')} — новое показано ниже."
        )
    if draft.moved:
        lines.append("Урок перенесён из-за замены — задание привязано к следующему действующему уроку.")
    return "\n".join(lines)


@authorized
async def handle_text(update: Any, context: Any) -> None:
    current = require_backend()
    message = _message_of(update)
    text = str(getattr(message, "text", "") or "").strip()
    if not text:
        return
    documents, _ = await current.read_documents()
    user_id = int(update.effective_user.id)
    update_id = getattr(update, "update_id", None)
    day = _message_day(update)

    if DUTY_PREFIX_RE.match(text):
        result = await asyncio.to_thread(
            current.duties.add_report,
            day,
            text,
            user_id,
            update_id,
            documents,
        )
        if result.status == "confirmed":
            lines = ["Отчёт принят: " + ", ".join(result.added_names or result.names) + "."]
            if result.all_names and set(result.all_names) != set(result.names):
                lines.append("Итого за сегодня: " + ", ".join(result.all_names) + ".")
            lines.append("Засчитаю всех упомянутых при закрытии дня. Счётчики обновятся после 00:00.")
            await reply_text(update, "\n".join(lines))
        else:
            ambiguous = ", ".join(result.ambiguous) if result.ambiguous else "не найдено"
            await reply_text(
                update,
                f"Не смог однозначно подтвердить: {ambiguous}.\n"
                "Ничего не засчитано. Напиши ФИО или фамилии точнее — отчёт останется на проверке.",
            )
        return

    result = await asyncio.to_thread(
        current.homework.add,
        text,
        user_id,
        update_id,
        day,
        documents,
    )
    await reply_text(update, _homework_reply(result, documents["schedule.json"]))


async def error_handler(update: object, context: Any) -> None:
    error = getattr(context, "error", None)
    current = backend
    secrets_value = current.secrets if current else ()
    LOG.error("telegram update failed: %s", safe_exception(error or Exception("unknown"), secrets_value))
    if isinstance(update, Update) and current and current.is_allowed(update):
        try:
            await reply_text(update, "Произошла ошибка. Данные не изменены; повторите позже.")
        except Exception:
            pass


async def post_init(application: Any) -> None:
    current = require_backend()
    current.scheduler = BotScheduler(current)
    await current.scheduler.start(application)


async def post_shutdown(application: Any) -> None:
    current = require_backend()
    if current.scheduler is not None:
        await current.scheduler.stop(application)


def build_application(config: Config) -> Any:
    global backend
    if Application is None:
        raise ConfigError("Не установлены зависимости Telegram. Выполните: python -m pip install -r requirements.txt")
    backend = Backend(config)
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("grades", grades_command))
    application.add_handler(CommandHandler("duties", duties_command))
    application.add_handler(CommandHandler("pair", pair_command))
    application.add_handler(CommandHandler("unpair", unpair_command))
    application.add_handler(CommandHandler("pairs", pairs_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(review_callback, pattern=r"^review:(yes|no):"))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_image))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    try:
        application = build_application(config)
    except ConfigError as exc:
        print(f"Ошибка запуска: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    LOG.info("108M backend started; repository=%s branch=%s", config.github_repository, config.github_branch)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)



if __name__ == "__main__":
    main()


