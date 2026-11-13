"""Telegram homework bot. Configure the constants below, then run with Python."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# ── CONFIG: edit these values ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")  # Fine-grained PAT: Contents read/write on this repo
GITHUB_REPOSITORY = "SsixM/automeh108"
TASKS_FILE = "tasks.json"
# Subject names here must match the website schedule; weekday numbers: Mon=0.
LESSONS = {
    "математика": {"days": [0, 1, 4]},
    "иностранный язык": {"days": [0, 2]},
    "обизр": {"days": [0]},
    "обж": {"days": [0]},
    "русский язык": {"days": [0, 1, 2]},
    "введение в специальность": {"days": [1]},
    "химия": {"days": [1]},
    "родная литература": {"days": [2]},
    "биология": {"days": [3]},
    "информатика": {"days": [3]},
    "история": {"days": [3, 4]},
    "физическая культура": {"days": [4]},
}
# ──────────────────────────────────────────────────────────────────────────

DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")


def parse_assignment(text: str) -> tuple[str, str, date]:
    match = DATE_RE.search(text)
    if not match:
        raise ValueError("Добавь дату в конце, например: Математика выучить степени 9.11")
    day, month, year = match.groups()
    today = date.today()
    year_num = int(year) if year else today.year
    if year_num < 100:
        year_num += 2000
    assigned = date(year_num, int(month), int(day))
    if not year and assigned > today + timedelta(days=31):
        assigned = assigned.replace(year=assigned.year - 1)
    task_part = (text[:match.start()] + " " + text[match.end():]).strip(" .,:;—-\t")
    normalized = task_part.casefold()
    subject_key = next((key for key in sorted(LESSONS, key=len, reverse=True) if normalized.startswith(key)), None)
    if not subject_key:
        raise ValueError("Не распознал предмет. Начни сообщение с названия предмета из расписания.")
    subject = task_part[:len(subject_key)].strip().capitalize()
    homework = task_part[len(subject_key):].strip(" .,:;—-\t")
    if not homework:
        raise ValueError("После предмета напиши задание.")
    days = LESSONS[subject_key]["days"]
    due = assigned + timedelta(days=1)
    while due.weekday() not in days:
        due += timedelta(days=1)
    return subject, homework, due


def github_get() -> tuple[list[dict], str | None]:
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{TASKS_FILE}"
    req = Request(url, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urlopen(req, timeout=20) as response:
            obj = json.load(response)
        import base64
        return json.loads(base64.b64decode(obj["content"]).decode("utf-8")), obj["sha"]
    except HTTPError as exc:
        if exc.code == 404:
            return [], None
        raise


def github_put(tasks: list[dict], sha: str | None) -> None:
    import base64
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/contents/{TASKS_FILE}"
    payload = {"message": "Add homework via Telegram bot", "content": base64.b64encode((json.dumps(tasks, ensure_ascii=False, indent=2) + "\n").encode()).decode()}
    if sha:
        payload["sha"] = sha
    req = Request(url, data=json.dumps(payload).encode(), method="PUT", headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    with urlopen(req, timeout=20):
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Пришли задание так: Математика выучить степени 9.11\nДата — когда задали; задание появится под следующим уроком этого предмета.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    try:
        subject, homework, due = parse_assignment(update.message.text)
        tasks, sha = github_get()
        tasks.append({"subject": subject, "text": homework, "assignedDate": date.today().isoformat(), "dueDay": due.isoformat(), "dueLabel": due.strftime("%-d.%m")})
        github_put(tasks, sha)
        await update.message.reply_text(f"Добавил: {subject} — {homework}\nПокажу под уроком {due.strftime('%-d.%m')}.")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
    except (HTTPError, URLError, OSError, KeyError, json.JSONDecodeError) as exc:
        await update.message.reply_text(f"Не удалось сохранить в GitHub: {exc}")


def main() -> None:
    if not TELEGRAM_TOKEN or not GITHUB_TOKEN:
        raise SystemExit("Установи TELEGRAM_BOT_TOKEN и GITHUB_TOKEN как переменные окружения.")
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()


if __name__ == "__main__":
    main()
