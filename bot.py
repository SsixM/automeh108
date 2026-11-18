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
DUTY_ALGORITHM_VERSION = "sha256-layered-v1"

DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash",
)
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
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
    root = _root(document, "duties.json", {"group", "students", "days"})
    _schema(root.get("group") == GROUP_NAME, "duties.json.group: ожидалась группа 108М")
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
        return {**base, "group": GROUP_NAME, "students": {}, "days": []}
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
        if not event_id or not re.fullmatch(r"[A-Za-z0-9_.:-]{3,120}", event_id):
            raise ValueError("event_id должен быть безопасной короткой строкой")
        unknown = set(mutators) - set(DATA_FILES)
        if unknown:
            raise ValueError(f"Неизвестные файлы для записи: {', '.join(sorted(unknown))}")
        commit_text = CONTROL_RE.sub(" ", str(message or "update")).strip()[:60] or "update"
        suffix = event_id[:12]
        with self.lock:
            last_error: Exception | None = None
            for attempt in range(self.retries):
                try:
                    bundle = self.read_bundle(strict=False)
                    documents = clone_json(bundle.documents)
                    for name, mutator in mutators.items():
                        mutator(documents[name])
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
        return stable_hash(f"{self.subject_id}|{self.assigned_date.isoformat()}")[:24]

    @property
    def next_lesson_date(self) -> str:
        return str(self.next_lesson["date"])

    @property
    def next_lesson_id(self) -> str:
        return str(self.next_lesson["lessonId"])


class HomeworkResult:
    def __init__(self, status: str, draft: HomeworkDraft, existing_text: str | None = None):
        self.status = status  # added | updated | duplicate
        self.draft = draft
        self.existing_text = existing_text


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
    ) -> HomeworkDraft:
        if len(text) > 1000:
            raise UserFacingError("Сообщение слишком длинное для домашнего задания.")
        subject_id, subject, remaining = self._find_subject(text.strip(), schedule_doc)
        assigned_date, homework = self._extract_date(remaining, assigned)
        homework = SPACE_RE.sub(" ", homework).strip()
        if not homework:
            raise UserFacingError("После предмета напиши текст задания. Например: «Математика выучить логарифмы 10.09».")
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
    ) -> HomeworkResult:
        draft = self.parse(text, assigned, documents["schedule.json"], documents["replacements.json"])
        result_holder: dict[str, Any] = {}

        def mutate(tasks_doc: dict[str, Any]) -> None:
            tasks = tasks_doc.setdefault("tasks", [])
            existing = next((task for task in tasks if task.get("taskId") == draft.task_id), None)
            if existing is None:
                existing = next(
                    (
                        task for task in tasks
                        if task.get("subjectId") == draft.subject_id
                        and task.get("assignedDate") == draft.assigned_date.isoformat()
                    ),
                    None,
                )
            now = iso_now()
            if existing is not None:
                same_update = update_id is not None and existing.get("sourceUpdateId") == update_id
                same_text = existing.get("text") == draft.text
                same_lesson = existing.get("nextLessonId") == draft.next_lesson_id
                if same_update or (same_text and same_lesson and existing.get("assignedDate") == draft.assigned_date.isoformat()):
                    result_holder["status"] = "duplicate"
                    result_holder["existing_text"] = existing.get("text")
                    return
                existing.update({
                    "subjectId": draft.subject_id,
                    "subject": draft.subject,
                    "text": draft.text,
                    "assignedDate": draft.assigned_date.isoformat(),
                    "nextLessonDate": draft.next_lesson_date,
                    "nextLessonId": draft.next_lesson_id,
                    "movedByReplacement": draft.moved,
                    "updatedAt": now,
                    "sourceUserId": user_id,
                })
                if update_id is not None:
                    existing["sourceUpdateId"] = update_id
                if draft.moved:
                    existing.setdefault("originalNextLessonDate", (draft.naive_lesson or {}).get("date", draft.next_lesson_date))
                if existing.get("status") != "выполнено":
                    existing["status"] = self._status_for(draft)
                result_holder["status"] = "updated"
                result_holder["existing_text"] = existing.get("text")
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
            tasks.append(task)
            result_holder["status"] = "added"
            result_holder["existing_text"] = None

        self.storage.publish({"tasks.json": mutate}, f"task-{draft.task_id}", "add or update homework")
        return HomeworkResult(
            result_holder.get("status", "duplicate"),
            draft,
            result_holder.get("existing_text"),
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


class Backend:
    def __init__(
        self,
        config: Config,
        storage: GitHubStorage | None = None,
        local: LocalState | None = None,
        quota: Quota | None = None,
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
        self.update_guard = UpdateGuard(self.local)
        self.started_at = now_moscow()
        self.batches: dict[int, Any] = {}
        self.batch_lock = asyncio.Lock()

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
        "• Дежурство: «Дежурили Абакаев, Бадреев, Осадчий».\n"
        "• Журнал: отправь фото или альбом.\n"
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
        "Замены и чётность недели учитываются. Повторное сообщение с тем же предметом и датой обновит текст.\n\n"
        "ДЕЖУРСТВА\n"
        "Сообщение: «Дежурили Абакаев, Бадреев, Осадчий».\n"
        "Можно фамилию, имя или ФИО. Бот засчитает только однозначно найденных людей. "
        "Если имя неоднозначное — попросит написать точнее и ничего не засчитает.\n\n"
        "ЖУРНАЛ\n"
        "Отправь фото страницы или Telegram-альбом. Бот сам определит момент окончания загрузки. "
        "Максимум 20 изображений в одной загрузке; повторное фото по hash не отправляется в Gemini. "
        "PDF и другие документы как журнал не принимаются.\n\n"
        "ЗАМЕНЫ\n"
        "Перешли PDF замен документом. Бот обработает только строки группы 108М. "
        "PDF не публикуется и не исполняется.\n\n"
        "КОМАНДЫ\n"
        "/today — расписание, замены и ДЗ на сегодня\n"
        "/grades — как смотреть оценки и сколько записей опубликовано\n"
        "/duties — последние дежурства\n"
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


def build_application(config: Config) -> Any:
    global backend
    if Application is None:
        raise ConfigError("Не установлены зависимости Telegram. Выполните: python -m pip install -r requirements.txt")
    backend = Backend(config)
    application = Application.builder().token(config.telegram_token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("grades", grades_command))
    application.add_handler(CommandHandler("status", status_command))
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


# __PART_4__

if __name__ == "__main__":
    main()


