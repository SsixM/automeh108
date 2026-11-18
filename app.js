'use strict';

/**
 * 108М — статический учебный дневник.
 *
 * Классический скрипт (без сборки) для GitHub Pages. Читает только data/*.json,
 * ничего не записывает в GitHub и не содержит секретов. Чистые функции внизу
 * экспортируются для Node-тестов; в браузере приложение запускается само.
 */
(function () {
  const VIEW_FILES = {
    schedule: ['schedule.json'],
    grades: ['students.json', 'schedule.json', 'grades.json', 'reviews.json'],
    tasks: ['tasks.json', 'schedule.json', 'replacements.json'],
    duties: ['students.json', 'duties.json'],
    replacements: ['replacements.json', 'schedule.json'],
  };

  const REQUIRED_FILES = [
    'students.json',
    'schedule.json',
    'grades.json',
    'tasks.json',
    'duties.json',
    'replacements.json',
    'reviews.json',
  ];

  const VIEW_META = {
    schedule: {
      title: 'Расписание',
      intro: 'Пары, кабинеты и замены на неделю.',
    },
    grades: {
      title: 'Оценки',
      intro: 'Отметки по датам, посещаемость и итог аттестации.',
    },
    tasks: {
      title: 'Домашние задания',
      intro: 'Что задано и к какому уроку это относится.',
    },
    duties: {
      title: 'Дежурства',
      intro: 'Кто дежурит сегодня и сколько раз дежурил каждый.',
    },
    replacements: {
      title: 'Замены',
      intro: 'Изменения в расписании на выбранную дату.',
    },
  };

  const WEEKDAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье'];
  const WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  const MONTHS_GENITIVE = [
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
  ];
  const STALE_AFTER_HOURS = 36;
  const STALE_NOTE = 'Данные давно не обновлялись. Проверьте, работает ли Telegram-бот.';
  const EMPTY_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 7.5A2.5 2.5 0 0 1 6.5 5H11l2 2h4.5A2.5 2.5 0 0 1 20 9.5v7A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5v-9Z"/><path d="M8 13h8M8 16h5"/></svg>';
  const ERROR_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.7 2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.7a2 2 0 0 0-3.4 0Z"/></svg>';

  const state = {
    view: 'schedule',
    weekShift: 0,
    gradeGroup: '2к207',
    studentId: null,
    period: 'current',
    subjectId: 'all',
    taskFilter: 'active',
    replacementDate: moscowTodayISO(),
    data: {},
    manifest: null,
    status: 'loading',
    refreshing: false,
    error: '',
    lastLoadedAt: null,
    loadToken: 0,
    toastTimer: null,
  };

  /* ------------------------------------------------------------------ */
  /* Безопасность и базовые помощники                                    */
  /* ------------------------------------------------------------------ */

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, (character) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[character]));
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function parseISODate(value) {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const [year, month, day] = value.split('-').map(Number);
    const result = new Date(year, month - 1, day, 12, 0, 0, 0);
    if (result.getFullYear() !== year || result.getMonth() !== month - 1 || result.getDate() !== day) return null;
    return result;
  }

  function toISODate(date) {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  }

  function moscowTodayISO() {
    try {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Europe/Moscow',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }).format(new Date());
    } catch (error) {
      return toISODate(new Date());
    }
  }

  function todayDate() {
    return parseISODate(moscowTodayISO()) || new Date();
  }

  function addDays(date, days) {
    const result = new Date(date.getFullYear(), date.getMonth(), date.getDate() + days, 12, 0, 0, 0);
    return result;
  }

  function startOfWeek(date) {
    const offset = (date.getDay() + 6) % 7;
    return addDays(date, -offset);
  }

  function weekDates(shift, baseDate) {
    const base = baseDate || todayDate();
    const monday = addDays(startOfWeek(base), shift * 7);
    return [0, 1, 2, 3, 4].map((offset) => addDays(monday, offset));
  }

  function dayNumber(date) {
    return Math.floor(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()) / 86400000);
  }

  function weekTypeForDate(date, meta) {
    const safeMeta = meta || {};
    const anchor = parseISODate(safeMeta.weekAnchor) || parseISODate('2026-09-01');
    const first = safeMeta.firstWeekType === 'denominator' ? 'denominator' : 'numerator';
    const difference = Math.floor((dayNumber(date) - dayNumber(anchor)) / 7);
    const sameParity = ((difference % 2) + 2) % 2 === 0;
    if (sameParity) return first;
    return first === 'numerator' ? 'denominator' : 'numerator';
  }

  function weekTypeLabel(type) {
    return type === 'denominator' ? 'знаменатель' : 'числитель';
  }

  function formatDateLong(date) {
    return `${date.getDate()} ${MONTHS_GENITIVE[date.getMonth()]}`;
  }

  function formatDateFull(date) {
    return `${date.getDate()} ${MONTHS_GENITIVE[date.getMonth()]} ${date.getFullYear()}`;
  }

  function formatWeekday(date) {
    return WEEKDAYS[(date.getDay() + 6) % 7];
  }

  function formatWeekRange(days) {
    if (!days.length) return '';
    const first = days[0];
    const last = days[days.length - 1];
    if (first.getMonth() === last.getMonth()) {
      return `${first.getDate()}–${last.getDate()} ${MONTHS_GENITIVE[first.getMonth()]} ${first.getFullYear()}`;
    }
    return `${formatDateLong(first)} — ${formatDateLong(last)} ${last.getFullYear()}`;
  }

  function formatMoscowDateTime(value) {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    try {
      return new Intl.DateTimeFormat('ru-RU', {
        timeZone: 'Europe/Moscow',
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(parsed).replace('.', '');
    } catch (error) {
      return '';
    }
  }

  function formatMoscowTime(value) {
    if (!value) return '';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return '';
    try {
      return new Intl.DateTimeFormat('ru-RU', {
        timeZone: 'Europe/Moscow',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).format(parsed);
    } catch (error) {
      return '';
    }
  }

  function hoursSince(value) {
    if (!value) return 0;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return 0;
    return (Date.now() - parsed.getTime()) / 3600000;
  }

  function plural(number, one, few, many) {
    const absolute = Math.abs(number) % 100;
    const last = absolute % 10;
    if (absolute > 10 && absolute < 20) return many;
    if (last === 1) return one;
    if (last >= 2 && last <= 4) return few;
    return many;
  }

  function shortRevision(value) {
    const text = String(value || '');
    return text.length > 16 ? `${text.slice(0, 16)}…` : text;
  }

  function normalizeText(value) {
    return String(value || '').toLocaleLowerCase('ru-RU').replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
  }

  function isWeekend(date) {
    return date.getDay() === 0 || date.getDay() === 6;
  }

  /* ------------------------------------------------------------------ */
  /* Данные: расписание, замены, задания                                */
  /* ------------------------------------------------------------------ */

  function dataFile(name) {
    return state.data[name] || null;
  }

  function schedule() {
    return dataFile('schedule.json');
  }

  function timesByPair() {
    const map = new Map();
    const documentValue = schedule();
    for (const item of (documentValue && documentValue.times) || []) map.set(item.pairNumber, item);
    return map;
  }

  function students() {
    const documentValue = dataFile('students.json');
    return (documentValue && documentValue.students) || [];
  }

  function studentById(studentId) {
    return students().find((student) => student.studentId === studentId) || null;
  }

  function studentName(studentId) {
    const student = studentById(studentId);
    return student ? student.name : studentId;
  }

  function subjectCatalog() {
    const catalog = new Map();
    const documentValue = schedule();
    for (const lesson of (documentValue && documentValue.lessons) || []) {
      if (!lesson.service && !catalog.has(lesson.subjectId)) {
        catalog.set(lesson.subjectId, lesson.subject);
      }
    }
    return catalog;
  }

  function validOn(item, isoDate) {
    return (!item.validFrom || item.validFrom <= isoDate) && (!item.validTo || item.validTo >= isoDate);
  }

  function replacementDisplaySource(source) {
    const text = String(source || '');
    if (/schedule\.json/i.test(text)) return 'правило недели';
    if (/vk/i.test(text)) return 'VK';
    if (/telegram/i.test(text)) return 'PDF из Telegram';
    return text || 'источник не указан';
  }

  function effectiveLessons(date, availableData) {
    const data = availableData || state.data;
    const scheduleDoc = data['schedule.json'];
    const iso = toISODate(date);
    const weekday = date.getDay() === 0 ? 7 : date.getDay();
    if (!scheduleDoc) {
      return { date: iso, weekday, weekType: 'numerator', lessons: [], replacements: [] };
    }
    const weekType = weekTypeForDate(date, scheduleDoc.meta);

    let lessons = (scheduleDoc.lessons || [])
      .filter((lesson) => lesson.weekday === weekday)
      .filter((lesson) => validOn(lesson, iso))
      .filter((lesson) => lesson.weekType === 'all' || lesson.weekType === weekType)
      .map((lesson) => ({ ...lesson, weekTypeActual: weekType }));

    for (const rule of scheduleDoc.replacementRules || []) {
      if (rule.weekday !== weekday) continue;
      if (rule.weekType !== 'all' && rule.weekType !== weekType) continue;
      if (!validOn(rule, iso)) continue;
      const index = lessons.findIndex((lesson) => lesson.lessonId === rule.originalLessonId);
      if (index < 0) continue;
      const original = lessons[index];
      lessons[index] = {
        ...original,
        subjectId: rule.newSubjectId,
        subject: rule.newSubject,
        teacher: rule.newTeacher != null ? rule.newTeacher : original.teacher,
        room: rule.newRoom != null ? rule.newRoom : original.room,
        replacement: true,
        replacementKind: 'rule',
        replacementRuleId: rule.ruleId,
        originalSubject: original.subject,
        originalTeacher: original.teacher,
        originalRoom: original.room,
        source: 'schedule.json',
      };
    }

    const replacementItems = (data['replacements.json'] && data['replacements.json'].items) || [];
    const replacements = replacementItems.filter((item) => item.date === iso);
    for (const replacement of replacements) {
      if (replacement.weekType && replacement.weekType !== weekType) continue;
      let index = replacement.originalLessonId
        ? lessons.findIndex((lesson) => lesson.lessonId === replacement.originalLessonId)
        : -1;
      if (index < 0) index = lessons.findIndex((lesson) => lesson.pairNumber === replacement.pairNumber);
      if (replacement.cancelled) {
        if (index >= 0) lessons.splice(index, 1);
        continue;
      }
      if (index >= 0) {
        const original = lessons[index];
        lessons[index] = {
          ...original,
          subjectId: replacement.newSubjectId,
          subject: replacement.newSubject,
          teacher: replacement.newTeacher != null ? replacement.newTeacher : original.teacher,
          room: replacement.newRoom != null ? replacement.newRoom : original.room,
          replacement: true,
          replacementKind: 'individual',
          replacementId: replacement.replacementId,
          originalSubject: original.originalSubject || original.subject,
          originalTeacher: original.originalTeacher || original.teacher,
          originalRoom: original.originalRoom || original.room,
          source: replacement.source,
          sourceHash: replacement.sourceHash,
          receivedAt: replacement.receivedAt,
          weekTypeActual: weekType,
        };
      } else {
        lessons.push({
          lessonId: replacement.originalLessonId || `replacement-${replacement.pairNumber}`,
          weekday,
          pairNumber: replacement.pairNumber,
          subjectId: replacement.newSubjectId,
          subject: replacement.newSubject,
          teacher: replacement.newTeacher || '',
          room: replacement.newRoom || '',
          subgroup: 'all',
          weekType: weekType,
          weekTypeActual: weekType,
          service: false,
          replacement: true,
          replacementKind: 'individual',
          replacementId: replacement.replacementId,
          originalSubject: replacement.originalSubject || '',
          originalRoom: '',
          source: replacement.source,
          receivedAt: replacement.receivedAt,
        });
      }
    }

    lessons.sort((left, right) => {
      if (left.pairNumber !== right.pairNumber) return left.pairNumber - right.pairNumber;
      return String(left.subject).localeCompare(String(right.subject), 'ru');
    });
    return { date: iso, weekday, weekType, lessons, replacements };
  }

  function findLessonOnDate(isoDate, lessonId, subjectId) {
    const date = parseISODate(isoDate);
    if (!date) return null;
    const result = effectiveLessons(date, state.data);
    return result.lessons.find((lesson) => lesson.lessonId === lessonId)
      || result.lessons.find((lesson) => lesson.subjectId === subjectId)
      || null;
  }

  function taskDisplayStatus(task, todayIso) {
    if (!task) return 'актуальное';
    if (task.status === 'выполнено') return 'выполнено';
    if (task.nextLessonDate && task.nextLessonDate < todayIso) return 'просрочено';
    if (task.movedByReplacement) return 'перенесено';
    return 'актуальное';
  }

  function pairNumberFor(isoDate, lessonId) {
    const lesson = findLessonOnDate(isoDate, lessonId, null);
    return lesson ? lesson.pairNumber : 0;
  }

  function safeHttpsUrl(value) {
    try {
      const url = new URL(String(value));
      return url.protocol === 'https:' ? url.href : '';
    } catch {
      return '';
    }
  }

  function renderAttachments(attachments) {
    if (!Array.isArray(attachments) || !attachments.length) return '';
    const items = attachments.map((attachment) => {
      const url = safeHttpsUrl(attachment && attachment.url);
      if (!url) return '';
      const width = Number(attachment.width) > 0 ? Number(attachment.width) : 1200;
      const height = Number(attachment.height) > 0 ? Number(attachment.height) : 900;
      const label = `Фото к заданию${attachment.fileName ? `: ${attachment.fileName}` : ''}`;
      return `<a class="task-attachment" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" aria-label="${escapeHtml(label)}">
        <img src="${escapeHtml(url)}" alt="${escapeHtml(label)}" width="${width}" height="${height}" loading="lazy" decoding="async">
      </a>`;
    }).filter(Boolean);
    if (!items.length) return '';
    return `<div class="task-attachments">${items.join('')}</div>`;
  }

  function dutyReason(day) {
    if (!day) return 'выбор ещё не опубликован';
    if (day.status === 'closed' || day.status === 'closed-without-confirmation') {
      return (day.actual || []).length ? 'подтверждён сообщением' : 'не подтверждён';
    }
    if (day.status === 'reported') return 'ожидает закрытия дня';
    if (day.status === 'pending') return 'ожидает проверки';
    return 'выбран';
  }

  /* ------------------------------------------------------------------ */
  /* Загрузка данных                                                     */
  /* ------------------------------------------------------------------ */

  class DataError extends Error {}
  class RevisionMismatch extends DataError {}

  function friendlyError(error) {
    if (error instanceof DataError) return error.message;
    if (error && error.name === 'TypeError') {
      return 'Не удалось связаться с опубликованными данными. Проверьте подключение к интернету.';
    }
    return 'Не удалось загрузить опубликованные данные. Попробуйте ещё раз.';
  }

  async function fetchJson(url) {
    let response;
    try {
      response = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' } });
    } catch (error) {
      throw new DataError('Нет связи с GitHub Pages. Проверьте подключение к интернету.');
    }
    if (!response.ok) {
      const name = url.split('/').pop().split('?')[0];
      throw new DataError(`Файл ${name} не найден или недоступен (HTTP ${response.status}).`);
    }
    try {
      return await response.json();
    } catch (error) {
      throw new DataError('Опубликованный файл повреждён: это не корректный JSON.');
    }
  }

  function validateManifest(manifest) {
    if (!manifest || typeof manifest !== 'object' || manifest.schemaVersion !== 1) {
      throw new DataError('manifest.json имеет неизвестный формат.');
    }
    if (typeof manifest.revision !== 'string' || !manifest.revision) {
      throw new DataError('В manifest.json отсутствует revision.');
    }
    if (!manifest.files || typeof manifest.files !== 'object') {
      throw new DataError('В manifest.json отсутствует список файлов.');
    }
    for (const name of REQUIRED_FILES) {
      if (manifest.files[name] !== manifest.revision) {
        throw new RevisionMismatch('Опубликованные файлы ещё не согласованы по revision. Обновляем…');
      }
    }
    return manifest;
  }

  async function fetchManifest() {
    const manifest = await fetchJson(`data/manifest.json?ts=${Date.now()}`);
    return validateManifest(manifest);
  }

  function viewDataReady(view) {
    if (!state.manifest) return false;
    return (VIEW_FILES[view] || []).every((name) => {
      const documentValue = state.data[name];
      return documentValue && documentValue.revision === state.manifest.files[name];
    });
  }

  function sleep(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  async function loadView(view, options) {
    const opts = options || {};
    const token = ++state.loadToken;
    const hadData = viewDataReady(view);
    if (hadData) {
      state.refreshing = true;
    } else {
      state.status = 'loading';
      state.error = '';
    }
    render();

    try {
      const manifest = await fetchManifest();
      if (token !== state.loadToken) return;
      const needed = VIEW_FILES[view] || [];
      const staged = Object.assign({}, state.data);
      for (const name of needed) {
        const expectedRevision = manifest.files[name];
        const cached = staged[name];
        if (!opts.force && cached && cached.revision === expectedRevision) continue;
        const documentValue = await fetchJson(`data/${name}?v=${encodeURIComponent(expectedRevision)}`);
        if (token !== state.loadToken) return;
        if (!documentValue || typeof documentValue !== 'object' || documentValue.schemaVersion !== 1) {
          throw new DataError(`Файл ${name} имеет неизвестный формат.`);
        }
        if (documentValue.revision !== expectedRevision) {
          throw new RevisionMismatch('Файлы обновились во время загрузки. Пробуем ещё раз…');
        }
        staged[name] = documentValue;
      }
      for (const name of needed) {
        if (!staged[name] || staged[name].revision !== manifest.files[name]) {
          throw new RevisionMismatch('Набор файлов не согласован по revision. Пробуем ещё раз…');
        }
      }
      if (token !== state.loadToken) return;
      state.data = staged;
      state.manifest = manifest;
      state.status = 'ready';
      state.error = '';
      state.lastLoadedAt = Date.now();
      ensureSelections();
      if (opts.announce) announce('Данные обновлены.');
    } catch (error) {
      if (token !== state.loadToken) return;
      if (error instanceof RevisionMismatch && !opts.retried) {
        await sleep(450);
        if (token !== state.loadToken) return;
        return loadView(view, { force: true, retried: true, announce: opts.announce });
      }
      const message = error instanceof RevisionMismatch && opts.retried
        ? 'Опубликованные файлы не согласованы по revision. Это временное состояние публикации — повторите загрузку через минуту.'
        : friendlyError(error);
      state.error = message;
      if (hadData) {
        state.status = 'stale';
      } else {
        state.status = 'error';
      }
      announce(message);
    } finally {
      if (token === state.loadToken) {
        state.refreshing = false;
        render();
      }
    }
  }

  function ensureSelections() {
    const allStudents = students();
    if (!allStudents.length) return;
    const group = state.gradeGroup;
    const inGroup = group === 'all' ? allStudents : allStudents.filter((student) => student.englishGroup === group);
    if (!inGroup.length) {
      state.gradeGroup = 'all';
      state.studentId = allStudents[0].studentId;
      return;
    }
    if (!state.studentId || !inGroup.some((student) => student.studentId === state.studentId)) {
      state.studentId = inGroup[0].studentId;
    }
    const catalog = subjectCatalog();
    if (state.subjectId !== 'all' && !catalog.has(state.subjectId)) state.subjectId = 'all';
  }

  /* ------------------------------------------------------------------ */
  /* Общие фрагменты разметки                                            */
  /* ------------------------------------------------------------------ */

  function announce(message) {
    const region = document.getElementById('liveRegion');
    if (region) region.textContent = message;
  }

  function chip(text, tone, extraClass) {
    const classes = ['chip'];
    if (tone) classes.push(`chip-${tone}`);
    if (extraClass) classes.push(extraClass);
    return `<span class="${classes.join(' ')}">${escapeHtml(text)}</span>`;
  }

  function emptyState(title, text, actionHtml) {
    return `<div class="state-card state-card-empty">
      <span class="state-icon">${EMPTY_ICON}</span>
      <h2>${escapeHtml(title)}</h2>
      <p>${escapeHtml(text)}</p>
      ${actionHtml || ''}
    </div>`;
  }

  function errorState(message, withRetry) {
    return `<div class="state-card state-card-error" role="alert">
      <span class="state-icon">${ERROR_ICON}</span>
      <h2>Не удалось загрузить данные</h2>
      <p>${escapeHtml(message || 'Попробуйте обновить страницу чуть позже.')}</p>
      ${withRetry === false ? '' : '<button type="button" class="button button-primary" data-action="retry">Повторить загрузку</button>'}
    </div>`;
  }

  function loadingState() {
    return `<div class="skeleton-grid" role="status" aria-label="Загрузка данных">
      <div class="card skeleton-card"><span class="skeleton skeleton-line w-40"></span><span class="skeleton skeleton-line w-80"></span><span class="skeleton skeleton-line w-60"></span></div>
      <div class="card skeleton-card"><span class="skeleton skeleton-line w-30"></span><span class="skeleton skeleton-line w-70"></span><span class="skeleton skeleton-line w-50"></span></div>
      <div class="card skeleton-card"><span class="skeleton skeleton-line w-35"></span><span class="skeleton skeleton-line w-75"></span><span class="skeleton skeleton-line w-55"></span></div>
    </div>`;
  }

  function staleBanner() {
    const publication = state.manifest ? formatMoscowDateTime(state.manifest.generatedAt) : '';
    if (state.status === 'stale') {
      return `<div class="banner banner-warn" role="status">
        <strong>Показаны устаревшие данные.</strong>
        <span>${escapeHtml(state.error || 'Не удалось проверить новую публикацию.')}</span>
        ${publication ? `<span class="banner-meta">Последняя подтверждённая публикация: ${escapeHtml(publication)}</span>` : ''}
      </div>`;
    }
    if (state.status === 'ready' && state.manifest && hoursSince(state.manifest.generatedAt) > STALE_AFTER_HOURS) {
      return `<div class="banner banner-warn" role="status">
        <strong>Данные давно не обновлялись.</strong>
        <span>${escapeHtml(STALE_NOTE)}</span>
        ${publication ? `<span class="banner-meta">Последняя публикация: ${escapeHtml(publication)}</span>` : ''}
      </div>`;
    }
    return '';
  }

  function sectionHeading(title, subtitle, extra, titleId) {
    return `<div class="section-heading">
      <div><h2${titleId ? ` id="${escapeHtml(titleId)}"` : ''}>${escapeHtml(title)}</h2>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ''}</div>
      ${extra || ''}
    </div>`;
  }

  function subjectTone(subjectId) {
    const tones = ['lime', 'sky', 'amber', 'rose', 'violet', 'teal'];
    let hash = 0;
    for (const character of String(subjectId)) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
    return tones[hash % tones.length];
  }

  /* ------------------------------------------------------------------ */
  /* Экран расписания                                                    */
  /* ------------------------------------------------------------------ */

  function subgroupOptions(lesson) {
    if (!Array.isArray(lesson.subgroupOptions) || !lesson.subgroupOptions.length) return '';
    return `<div class="subgroups" aria-label="Подгруппы английского языка">
      ${lesson.subgroupOptions.map((option) => `<span class="subgroup">
        <span class="subgroup-id">${escapeHtml(option.id)}</span>
        ${option.room ? `<span class="subgroup-room">каб. ${escapeHtml(option.room)}</span>` : ''}
        ${option.teacher ? `<span class="subgroup-teacher">${escapeHtml(option.teacher)}</span>` : ''}
      </span>`).join('')}
    </div>`;
  }

  function lessonMetaLine(lesson) {
    const parts = [];
    if (lesson.teacher) parts.push(escapeHtml(lesson.teacher));
    if (lesson.room) parts.push(`каб. ${escapeHtml(lesson.room)}`);
    return parts.length ? `<p class="lesson-meta">${parts.join(' · ')}</p>` : '';
  }

  function lessonCard(lesson) {
    const time = timesByPair().get(lesson.pairNumber);
    const classes = ['lesson'];
    if (lesson.service) classes.push('lesson-service');
    if (lesson.replacement) classes.push('lesson-replaced');
    let original = '';
    if (lesson.replacement) {
      const before = [lesson.originalSubject, lesson.originalRoom ? `каб. ${lesson.originalRoom}` : '']
        .filter(Boolean).join(' · ');
      original = before
        ? `<p class="lesson-original">Было: <s>${escapeHtml(before)}</s></p>`
        : '';
    }
    const tags = [];
    if (lesson.replacement) tags.push(chip('замена', 'warn'));
    else if (lesson.service) tags.push(chip('событие', 'neutral'));
    if (lesson.replacementKind === 'rule') tags.push(chip('правило недели', 'ghost'));
    if (lesson.weekType && lesson.weekType !== 'all') tags.push(chip(weekTypeLabel(lesson.weekType), 'ghost'));
    if (lesson.subgroup === 'split' && !(lesson.subgroupOptions || []).length) tags.push(chip('2к207 / 2к202', 'lime'));
    else if (lesson.subgroup === '2к207' || lesson.subgroup === '2к202') tags.push(chip(lesson.subgroup, 'lime'));

    return `<article class="${classes.join(' ')}" data-subject-tone="${subjectTone(lesson.subjectId)}">
      <div class="lesson-time" aria-label="Пара ${escapeHtml(lesson.pairNumber)}${time ? `, ${escapeHtml(time.start)}–${escapeHtml(time.end)}` : ''}">
        <span class="lesson-pair">${escapeHtml(lesson.pairNumber)}</span>
        <span class="lesson-clock">${time ? `${escapeHtml(time.start)}<br>${escapeHtml(time.end)}` : '—'}</span>
      </div>
      <div class="lesson-body">
        <h3 class="lesson-subject">${escapeHtml(lesson.subject)}</h3>
        ${lessonMetaLine(lesson)}
        ${original}
        ${subgroupOptions(lesson)}
        ${tags.length ? `<div class="lesson-tags">${tags.join('')}</div>` : ''}
      </div>
    </article>`;
  }

  function renderSchedule() {
    const scheduleDoc = schedule();
    if (!scheduleDoc) return emptyState('Расписание ещё не опубликовано', 'Данные появятся после публикации через Telegram-бота.');
    const days = weekDates(state.weekShift);
    const todayIso = moscowTodayISO();
    const weekType = weekTypeForDate(days[0], scheduleDoc.meta);
    const dayResults = days.map((date) => ({ date, result: effectiveLessons(date, state.data) }));
    const hasLessons = dayResults.some((item) => item.result.lessons.length > 0);
    const weekLabel = state.weekShift === 0
      ? 'Эта неделя'
      : state.weekShift === -1 ? 'Прошлая неделя' : state.weekShift === 1 ? 'Следующая неделя' : `Неделя ${state.weekShift > 0 ? '+' : ''}${state.weekShift}`;

    const dayCards = dayResults.map(({ date, result }) => {
      const iso = toISODate(date);
      const isToday = iso === todayIso;
      return `<article class="day-card${isToday ? ' is-today' : ''}" aria-labelledby="day-${iso}">
        <header class="day-head">
          <div>
            <h3 id="day-${iso}">${escapeHtml(formatWeekday(date))}</h3>
            <time datetime="${iso}">${escapeHtml(formatDateLong(date))}</time>
          </div>
          ${isToday ? chip('сегодня', 'accent') : ''}
        </header>
        <div class="day-lessons">
          ${result.lessons.length
            ? result.lessons.map(lessonCard).join('')
            : '<p class="day-empty">Пар нет</p>'}
        </div>
      </article>`;
    }).join('');

    return `<section class="card week-toolbar-card" aria-label="Навигация по неделям">
      <div class="week-toolbar">
        <button type="button" class="icon-button" data-action="week-prev" aria-label="Предыдущая неделя">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m15 5-7 7 7 7"/></svg>
        </button>
        <div class="week-title">
          <p class="eyebrow">${escapeHtml(weekLabel)}</p>
          <h2>${escapeHtml(formatWeekRange(days))}</h2>
          <div class="week-chips">
            ${chip(weekTypeLabel(weekType), 'accent')}
            ${state.weekShift !== 0 ? '<button type="button" class="text-button" data-action="week-today">Вернуться к текущей</button>' : ''}
          </div>
        </div>
        <button type="button" class="icon-button" data-action="week-next" aria-label="Следующая неделя">
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 5 7 7-7 7"/></svg>
        </button>
      </div>
    </section>
    ${hasLessons
      ? `<div class="week-grid">${dayCards}</div>`
      : emptyState('На этой неделе пар нет', 'Проверьте другую неделю: возможно, расписание опубликовано с учётом чётности.')}`;
  }

  /* ------------------------------------------------------------------ */
  /* Экран оценок                                                        */
  /* ------------------------------------------------------------------ */

  function periodRange(period) {
    const today = todayDate();
    if (period === 'all') return { start: null, end: null };
    const monthOffset = period === 'previous' ? -1 : 0;
    const start = new Date(today.getFullYear(), today.getMonth() + monthOffset, 1, 12);
    const end = new Date(today.getFullYear(), today.getMonth() + monthOffset + 1, 0, 12);
    return { start: toISODate(start), end: toISODate(end) };
  }

  function inPeriod(isoDate, range) {
    if (!range.start || !range.end) return true;
    return isoDate >= range.start && isoDate <= range.end;
  }

  function gradeBadge(value, extraClass) {
    const normalized = String(value);
    const safeValue = ['2', '3', '4', '5', 'н/б'].includes(normalized) ? normalized : 'неизвестно';
    const className = safeValue === 'н/б' ? 'grade-badge grade-nb' : `grade-badge grade-${safeValue}`;
    return `<span class="${className}${extraClass ? ` ${extraClass}` : ''}" aria-label="Оценка ${escapeHtml(normalized)}">${escapeHtml(normalized)}</span>`;
  }

  function gradeEntry(entry) {
    const date = parseISODate(entry.lessonDate);
    const badges = [];
    if (entry.entryType === 'attendance') badges.push(chip('посещаемость', 'neutral'));
    if (entry.entryType === 'attestation') badges.push(chip('итог аттестации', 'violet'));
    if (entry.status === 'pending') badges.push(chip('на проверке', 'warn'));
    if (entry.status === 'conflict') badges.push(chip('конфликт', 'danger'));
    if (Number(entry.confidence) < 0.75) badges.push(chip(`уверенность ${Math.round(Number(entry.confidence) * 100)}%`, 'warn'));
    const rawDiffers = entry.raw && normalizeText(entry.raw) !== normalizeText(entry.value);
    return `<li class="grade-entry">
      <time class="grade-date" datetime="${escapeHtml(entry.lessonDate)}">${date ? escapeHtml(formatDateLong(date)) : escapeHtml(entry.lessonDate)}</time>
      ${gradeBadge(entry.value)}
      ${badges.length ? `<span class="grade-flags">${badges.join('')}</span>` : ''}
      ${rawDiffers ? `<span class="grade-raw">распознано как «${escapeHtml(entry.raw)}»</span>` : ''}
    </li>`;
  }

  function renderGrades() {
    const studentsDoc = dataFile('students.json');
    const gradesDoc = dataFile('grades.json');
    const reviewsDoc = dataFile('reviews.json');
    if (!studentsDoc || !gradesDoc) return emptyState('Оценки ещё не опубликованы', 'Выберите ученика после первой публикации журнала.');
    const allStudents = studentsDoc.students || [];
    const groupStudents = state.gradeGroup === 'all'
      ? allStudents
      : allStudents.filter((student) => student.englishGroup === state.gradeGroup);
    const selected = groupStudents.find((student) => student.studentId === state.studentId) || groupStudents[0] || allStudents[0];
    if (!selected) return emptyState('Список учеников пуст', 'Проверьте публикацию students.json.');
    state.studentId = selected.studentId;

    const range = periodRange(state.period);
    const studentGrades = (gradesDoc.grades || []).filter((grade) => grade.studentId === selected.studentId);
    const periodGrades = studentGrades.filter((grade) => inPeriod(grade.lessonDate, range));
    const visibleGrades = state.subjectId === 'all'
      ? periodGrades
      : periodGrades.filter((grade) => grade.subjectId === state.subjectId);
    const regularGrades = visibleGrades.filter((grade) => grade.entryType !== 'attestation');
    const attestations = studentGrades
      .filter((grade) => grade.entryType === 'attestation')
      .filter((grade) => state.subjectId === 'all' || grade.subjectId === state.subjectId)
      .sort((left, right) => right.lessonDate.localeCompare(left.lessonDate));
    const latestAttestations = [];
    const seenSubjects = new Set();
    for (const grade of attestations) {
      if (!seenSubjects.has(grade.subjectId)) {
        seenSubjects.add(grade.subjectId);
        latestAttestations.push(grade);
      }
    }

    const numeric = regularGrades.filter((grade) => /^[2-5]$/.test(grade.value)).map((grade) => Number(grade.value));
    const average = numeric.length ? (numeric.reduce((sum, value) => sum + value, 0) / numeric.length) : null;
    const attendance = regularGrades.filter((grade) => grade.value === 'н/б').length;
    const alertCount = regularGrades.filter((grade) => grade.status === 'pending' || grade.status === 'conflict' || Number(grade.confidence) < 0.75).length;
    const pendingReviews = ((reviewsDoc && reviewsDoc.items) || [])
      .filter((review) => review.studentId === selected.studentId && review.status === 'pending')
      .filter((review) => state.subjectId === 'all' || review.subjectId === state.subjectId);

    const groupButtons = [['all', 'Все'], ['2к207', '2к207'], ['2к202', '2к202']].map(([value, label]) =>
      `<button type="button" class="segmented-button${state.gradeGroup === value ? ' is-active' : ''}" data-action="grade-group" data-group="${value}" aria-pressed="${state.gradeGroup === value}">${escapeHtml(label)}</button>`
    ).join('');

    const studentOptions = groupStudents.map((student) =>
      `<option value="${escapeHtml(student.studentId)}"${student.studentId === selected.studentId ? ' selected' : ''}>${escapeHtml(student.name)}</option>`
    ).join('');

    const catalog = subjectCatalog();
    for (const grade of studentGrades) if (!catalog.has(grade.subjectId)) catalog.set(grade.subjectId, grade.subject || grade.subjectId);
    const subjectOptions = [...catalog.entries()].sort((left, right) => left[1].localeCompare(right[1], 'ru'));
    if (state.subjectId !== 'all' && !catalog.has(state.subjectId)) state.subjectId = 'all';

    const controls = `<div class="card controls-card">
      <div class="segmented" role="group" aria-label="Подгруппа английского">${groupButtons}</div>
      <div class="controls-grid">
        <label class="field">
          <span>Ученик</span>
          <select id="studentSelect">${studentOptions}</select>
        </label>
        <label class="field">
          <span>Период</span>
          <select id="periodSelect">
            <option value="current"${state.period === 'current' ? ' selected' : ''}>Текущая аттестация</option>
            <option value="previous"${state.period === 'previous' ? ' selected' : ''}>Предыдущая аттестация</option>
            <option value="all"${state.period === 'all' ? ' selected' : ''}>Все периоды</option>
          </select>
        </label>
        <label class="field">
          <span>Предмет</span>
          <select id="subjectSelect">
            <option value="all"${state.subjectId === 'all' ? ' selected' : ''}>Все предметы</option>
            ${subjectOptions.map(([subjectId, subject]) => `<option value="${escapeHtml(subjectId)}"${subjectId === state.subjectId ? ' selected' : ''}>${escapeHtml(subject)}</option>`).join('')}
          </select>
        </label>
      </div>
      <p class="hint">Журнал опубликован: ${escapeHtml(state.manifest ? formatMoscowDateTime(state.manifest.generatedAt) : 'дата неизвестна')} (Москва).</p>
    </div>`;

    const summary = `<section class="card student-card" aria-labelledby="studentName">
      <div class="student-head">
        <div class="avatar" aria-hidden="true">${escapeHtml(selected.name.split(/\s+/).map((part) => part[0]).slice(0, 2).join(''))}</div>
        <div class="student-title">
          <h2 id="studentName">${escapeHtml(selected.name)}</h2>
          <p>${escapeHtml(selected.englishGroup)} · группа 108М</p>
        </div>
      </div>
      <div class="stats-grid">
        <div class="stat"><span class="stat-label">Средний балл</span><strong class="stat-value">${average == null ? '—' : average.toFixed(2).replace('.', ',')}</strong><span class="stat-note">по текущим отметкам</span></div>
        <div class="stat"><span class="stat-label">Оценок</span><strong class="stat-value">${numeric.length}</strong><span class="stat-note">в выбранном периоде</span></div>
        <div class="stat"><span class="stat-label">Н/Б</span><strong class="stat-value">${attendance}</strong><span class="stat-note">отметок посещаемости</span></div>
        <div class="stat"><span class="stat-label">На проверке</span><strong class="stat-value">${alertCount}</strong><span class="stat-note">низкая уверенность или конфликт</span></div>
      </div>
    </section>`;

    const attestationBlock = latestAttestations.length ? `<section class="card attestation-card">
      <div class="section-heading">
        <div><h2>Итог аттестации</h2><p>Отдельно от текущих отметок. Маркер «А» в журнале не является оценкой.</p></div>
      </div>
      <div class="attestation-list">
        ${latestAttestations.map((entry) => {
          const date = parseISODate(entry.lessonDate);
          return `<div class="attestation-item">
            <div><strong>${escapeHtml(entry.subject)}</strong><span class="attestation-date">${date ? escapeHtml(formatDateLong(date)) : escapeHtml(entry.lessonDate)}</span></div>
            ${gradeBadge(entry.value, 'grade-attestation')}
          </div>`;
        }).join('')}
      </div>
    </section>` : '';

    const reviewBlock = pendingReviews.length ? `<section class="card reviews-card">
      ${sectionHeading('Нужно подтверждение', 'Такие записи не заменяют подтверждённую оценку автоматически.')}
      <div class="review-list">
        ${pendingReviews.map((review) => {
          const date = parseISODate(review.lessonDate);
          return `<article class="review-item">
            <div class="review-values">${gradeBadge(review.oldValue)}<span class="review-arrow" aria-hidden="true">→</span>${gradeBadge(review.newValue)}</div>
            <div class="review-text">
              <strong>${escapeHtml(review.subject)}</strong>
              <span>${date ? escapeHtml(formatDateLong(date)) : escapeHtml(review.lessonDate)}</span>
              <p>${escapeHtml(review.reason || 'Значения отличаются. Нужна проверка.')}</p>
            </div>
          </article>`;
        }).join('')}
      </div>
      <p class="hint">Подтвердить или отклонить запись можно командой /review в Telegram-боте.</p>
    </section>` : '';

    const groups = new Map();
    for (const grade of regularGrades) {
      if (!groups.has(grade.subjectId)) groups.set(grade.subjectId, { subject: grade.subject, items: [] });
      groups.get(grade.subjectId).items.push(grade);
    }
    const subjectCards = [...groups.entries()]
      .sort((left, right) => left[1].subject.localeCompare(right[1].subject, 'ru'))
      .map(([subjectId, group]) => {
        const values = group.items.filter((grade) => /^[2-5]$/.test(grade.value)).map((grade) => Number(grade.value));
        const avg = values.length ? (values.reduce((sum, value) => sum + value, 0) / values.length) : null;
        const sorted = group.items.slice().sort((left, right) => left.lessonDate.localeCompare(right.lessonDate));
        return `<section class="card subject-card">
          <div class="subject-head">
            <div class="subject-title"><span class="subject-dot" data-tone="${subjectTone(subjectId)}" aria-hidden="true"></span><h2>${escapeHtml(group.subject)}</h2></div>
            ${avg == null ? '' : `<span class="subject-average">средний ${avg.toFixed(2).replace('.', ',')}</span>`}
          </div>
          <ul class="grade-list">${sorted.map(gradeEntry).join('')}</ul>
        </section>`;
      }).join('');

    const noGrades = !regularGrades.length ? emptyState(
      'Оценок за этот период нет',
      state.period === 'current'
        ? 'Возможно, журнал ещё не обновлён. Можно посмотреть предыдущую аттестацию или все периоды.'
        : 'Для выбранного ученика и предмета пока нет записей.',
      state.period !== 'all' ? '<button type="button" class="button button-primary" data-action="grades-all-periods">Показать все периоды</button>' : ''
    ) : '';

    return `${controls}${summary}${attestationBlock}${reviewBlock}${noGrades || `<div class="subject-grid">${subjectCards}</div>`}`;
  }

  /* ------------------------------------------------------------------ */
  /* Экран домашних заданий                                              */
  /* ------------------------------------------------------------------ */

  function taskStatusChip(status) {
    const tones = {
      'актуальное': 'accent',
      'выполнено': 'success',
      'просрочено': 'danger',
      'перенесено': 'warn',
    };
    return chip(status, tones[status] || 'neutral');
  }

  function renderTasks() {
    const tasksDoc = dataFile('tasks.json');
    if (!tasksDoc) return emptyState('Домашние задания ещё не опубликованы', 'Отправьте задание в Telegram-бот: «Математика выучить логарифмы 10.09».');
    const todayIso = moscowTodayISO();
    const allTasks = (tasksDoc.tasks || []).map((task) => ({ ...task, displayStatus: taskDisplayStatus(task, todayIso) }));
    const active = allTasks.filter((task) => task.displayStatus !== 'выполнено');
    const visible = state.taskFilter === 'all' ? allTasks : active;
    const overdue = active.filter((task) => task.displayStatus === 'просрочено').length;
    const completed = allTasks.filter((task) => task.displayStatus === 'выполнено').length;

    const filterButtons = `<div class="segmented" role="group" aria-label="Фильтр заданий">
      <button type="button" class="segmented-button${state.taskFilter === 'active' ? ' is-active' : ''}" data-action="task-filter" data-filter="active" aria-pressed="${state.taskFilter === 'active'}">Актуальные</button>
      <button type="button" class="segmented-button${state.taskFilter === 'all' ? ' is-active' : ''}" data-action="task-filter" data-filter="all" aria-pressed="${state.taskFilter === 'all'}">Все</button>
    </div>`;

    if (!allTasks.length) {
      return emptyState('Заданий пока нет', 'Когда учитель задаст домашнее задание, бот привяжет его к следующему уроку предмета.');
    }

    const groups = new Map();
    for (const task of visible) {
      const key = `${task.nextLessonDate}|${task.nextLessonId}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(task);
    }
    const sortedGroups = [...groups.entries()].sort((left, right) => {
      const [leftDate, leftLesson] = left[0].split('|');
      const [rightDate, rightLesson] = right[0].split('|');
      if (leftDate !== rightDate) return leftDate.localeCompare(rightDate);
      return pairNumberFor(leftDate, leftLesson) - pairNumberFor(rightDate, rightLesson);
    });
    const groupCards = sortedGroups.map(([key, groupTasks]) => {
      const [isoDate, lessonId] = key.split('|');
      const date = parseISODate(isoDate);
      const lesson = findLessonOnDate(isoDate, lessonId, groupTasks[0].subjectId);
      const time = lesson ? timesByPair().get(lesson.pairNumber) : null;
      const when = date ? `${escapeHtml(formatWeekday(date))}, ${escapeHtml(formatDateLong(date))}` : escapeHtml(isoDate);
      return `<section class="task-group" aria-labelledby="task-${escapeHtml(key.replace('|', '-'))}">
        <header class="task-date-head">
          <div>
            <p class="eyebrow">Следующий урок</p>
            <h2 id="task-${escapeHtml(key.replace('|', '-'))}">${escapeHtml(groupTasks[0].subject)}</h2>
            <p class="task-date-line">${when}${lesson ? ` · ${escapeHtml(lesson.pairNumber)} пара${time ? ` · ${escapeHtml(time.start)}–${escapeHtml(time.end)}` : ''}${lesson.room ? ` · каб. ${escapeHtml(lesson.room)}` : ''}` : ''}</p>
          </div>
          ${groupTasks.length > 1 ? `<span class="count-chip">${escapeHtml(groupTasks.length)} ${escapeHtml(plural(groupTasks.length, 'задание', 'задания', 'заданий'))}</span>` : ''}
        </header>
        <div class="task-list">
          ${groupTasks.map((task) => `<article class="card task-card">
            <div class="task-card-head">
              <div class="subject-title"><span class="subject-dot" data-tone="${subjectTone(task.subjectId)}" aria-hidden="true"></span><h3>${escapeHtml(task.subject)}</h3></div>
              ${taskStatusChip(task.displayStatus)}
            </div>
            <p class="task-text">${escapeHtml(task.text)}</p>
            ${renderAttachments(task.attachments)}
            <div class="task-meta">
              <span>Выдано: ${escapeHtml(formatTaskDate(task.assignedDate))}</span>
              ${task.movedByReplacement ? '<span class="task-moved">перенесено из-за замены</span>' : ''}
            </div>
          </article>`).join('')}
        </div>
      </section>`;
    }).join('');

    const summary = `<div class="card tasks-summary">
      <div class="stat"><span class="stat-label">Активных</span><strong class="stat-value">${active.length}</strong></div>
      <div class="stat"><span class="stat-label">Просрочено</span><strong class="stat-value">${overdue}</strong></div>
      <div class="stat"><span class="stat-label">Выполнено</span><strong class="stat-value">${completed}</strong></div>
      ${filterButtons}
    </div>`;

    if (!visible.length) {
      return `${summary}${emptyState(
        state.taskFilter === 'active' ? 'Активных заданий нет' : 'Заданий нет',
        state.taskFilter === 'active' ? 'Все опубликованные задания отмечены выполненными.' : 'Бот ещё не публиковал домашние задания.',
        state.taskFilter === 'active' ? '<button type="button" class="button" data-action="task-filter" data-filter="all">Показать все</button>' : ''
      )}`;
    }
    return `${summary}${groupCards}`;
  }

  function formatTaskDate(value) {
    const date = parseISODate(value);
    return date ? formatDateLong(date) : String(value || '');
  }

  /* ------------------------------------------------------------------ */
  /* Экран дежурств                                                      */
  /* ------------------------------------------------------------------ */

  function resolvedNames(ids, storedNames, dutiesDoc) {
    const map = new Map();
    for (const [studentId, record] of Object.entries((dutiesDoc && dutiesDoc.students) || {})) {
      map.set(studentId, record.name);
    }
    for (const student of students()) map.set(student.studentId, student.name);
    return (ids || []).map((studentId, index) => (storedNames && storedNames[index]) || map.get(studentId) || studentId);
  }

  function dutyDayCard(day, dutiesDoc, title) {
    if (!day) return '';
    const selected = resolvedNames(day.selected, day.selectedNames, dutiesDoc);
    const actual = resolvedNames(day.actual, day.actualNames, dutiesDoc);
    const date = parseISODate(day.date);
    const reason = dutyReason(day);
    const tone = day.status === 'closed' && actual.length ? 'success'
      : day.status === 'closed-without-confirmation' || (day.status === 'closed' && !actual.length) ? 'neutral'
        : day.status === 'pending' ? 'warn' : 'accent';
    return `<section class="card duty-today" aria-labelledby="duty-today-title">
      <div class="duty-today-head">
        <div>
          <p class="eyebrow">${escapeHtml(title)}</p>
          <h2 id="duty-today-title">${date ? `${escapeHtml(formatWeekday(date))}, ${escapeHtml(formatDateLong(date))}` : escapeHtml(day.date)}</h2>
        </div>
        ${chip(reason, tone)}
      </div>
      <div class="duty-columns">
        <div class="duty-column">
          <span class="duty-label">Выбраны</span>
          <ul class="person-list">${selected.length ? selected.map((name) => `<li>${escapeHtml(name)}</li>`).join('') : '<li class="muted">Никто не выбран</li>'}</ul>
        </div>
        <div class="duty-column">
          <span class="duty-label">Засчитаны</span>
          <ul class="person-list">${actual.length ? actual.map((name) => `<li>${escapeHtml(name)}</li>`).join('') : '<li class="muted">Пока никто</li>'}</ul>
          <p class="hint">${actual.length ? 'Начисление произойдёт при закрытии дня.' : 'Засчитываются только люди из отчёта «Дежурили …».'}</p>
        </div>
      </div>
    </section>`;
  }

  function renderDuties() {
    const dutiesDoc = dataFile('duties.json');
    if (!dutiesDoc) return emptyState('Дежурства ещё не опубликованы', 'Бот выберет дежурных в 00:00 по московскому времени.');
    const todayIso = moscowTodayISO();
    const today = parseISODate(todayIso);
    const days = (dutiesDoc.days || []).slice().sort((left, right) => right.date.localeCompare(left.date));
    const todayDay = days.find((day) => day.date === todayIso) || null;
    const upcoming = days.slice().sort((left, right) => left.date.localeCompare(right.date)).find((day) => day.date > todayIso) || null;
    const focusDay = todayDay || upcoming;
    const focusTitle = todayDay ? 'Сегодня дежурят' : upcoming ? 'Ближайшие дежурства' : 'Сегодня дежурят';

    let todayBlock = '';
    if (focusDay) {
      todayBlock = dutyDayCard(focusDay, dutiesDoc, focusTitle);
    } else if (isWeekend(today)) {
      todayBlock = `<div class="card duty-today"><div class="duty-today-head"><div><p class="eyebrow">Сегодня</p><h2>Выходной</h2></div>${chip('дежурства не назначаются', 'neutral')}</div><p class="hint">Выбор появится в ближайший учебный день в 00:00 по Москве.</p></div>`;
    } else {
      todayBlock = `<div class="card duty-today"><div class="duty-today-head"><div><p class="eyebrow">Сегодня</p><h2>${escapeHtml(formatWeekday(today))}, ${escapeHtml(formatDateLong(today))}</h2></div>${chip('ожидает выбора', 'warn')}</div><p class="hint">Выбор на сегодня ещё не опубликован. Бот делает его в 00:00 по Москве.</p></div>`;
    }

    const records = Object.entries(dutiesDoc.students || {}).map(([studentId, record]) => ({
      studentId,
      name: record.name || studentName(studentId),
      count: Number(record.count) || 0,
    }));
    const minimum = records.length ? Math.min(...records.map((record) => record.count)) : 0;
    const maximum = records.length ? Math.max(...records.map((record) => record.count), 1) : 1;
    records.sort((left, right) => left.count - right.count || left.name.localeCompare(right.name, 'ru'));

    const counterCards = records.map((record) => {
      const percent = maximum ? Math.round((record.count / maximum) * 100) : 0;
      return `<article class="counter-card">
        <div class="counter-top">
          <span class="counter-name">${escapeHtml(record.name)}</span>
          <span class="counter-value" aria-label="${record.count} ${plural(record.count, 'дежурство', 'дежурства', 'дежурств')}">${record.count}</span>
        </div>
        <span class="count-bar" aria-hidden="true"><i style="width:${percent}%"></i></span>
        ${record.count === minimum ? '<span class="counter-note">реже всех</span>' : ''}
      </article>`;
    }).join('');

    const historyItems = days.map((day) => {
      const date = parseISODate(day.date);
      const selected = resolvedNames(day.selected, day.selectedNames, dutiesDoc);
      const actual = resolvedNames(day.actual, day.actualNames, dutiesDoc);
      return `<article class="history-item">
        <div class="history-date">
          <strong>${date ? escapeHtml(formatDateLong(date)) : escapeHtml(day.date)}</strong>
          <span>${date ? escapeHtml(formatWeekday(date)) : ''}</span>
        </div>
        <div class="history-body">
          <p><span class="history-label">Выбраны:</span> ${selected.length ? escapeHtml(selected.join(', ')) : '—'}</p>
          <p><span class="history-label">Засчитаны:</span> ${actual.length ? escapeHtml(actual.join(', ')) : 'никто'}</p>
        </div>
        ${chip(dutyReason(day), day.status === 'closed' && actual.length ? 'success' : day.status === 'closed-without-confirmation' ? 'neutral' : 'warn')}
      </article>`;
    }).join('');

    return `${todayBlock}
      <section class="card counters-card" aria-labelledby="counters-title">
        ${sectionHeading('Счётчик дежурств', 'Обновляется только после закрытия дня и подтверждённого отчёта.', `<span class="chip chip-ghost">${records.length} учеников</span>`, 'counters-title')}
        <div class="counter-grid">${counterCards || '<p class="muted">Список учеников пуст.</p>'}</div>
      </section>
      <section class="card history-card" aria-labelledby="history-title">
        ${sectionHeading('История', 'Даты, выбранные люди и фактические дежурные.', '', 'history-title')}
        <div class="history-list">${historyItems || '<p class="muted">История появится после первого выбора дежурных.</p>'}</div>
      </section>`;
  }

  /* ------------------------------------------------------------------ */
  /* Экран замен                                                         */
  /* ------------------------------------------------------------------ */

  function shiftReplacementDate(days) {
    const date = parseISODate(state.replacementDate) || todayDate();
    state.replacementDate = toISODate(addDays(date, days));
  }

  function renderReplacements() {
    const scheduleDoc = schedule();
    const replacementsDoc = dataFile('replacements.json');
    if (!scheduleDoc) return emptyState('Замены ещё не опубликованы', 'Расписание и замены появятся после публикации через Telegram-бота.');
    const date = parseISODate(state.replacementDate) || parseISODate(moscowTodayISO());
    state.replacementDate = toISODate(date);
    const result = effectiveLessons(date, state.data);
    const iso = toISODate(date);
    const isToday = iso === moscowTodayISO();
    const ruleLessons = result.lessons.filter((lesson) => lesson.replacementKind === 'rule');
    const individualLessons = result.lessons.filter((lesson) => lesson.replacementKind === 'individual');
    const totalReplacements = ruleLessons.length + individualLessons.length;
    const sources = [...new Set((replacementsDoc && replacementsDoc.items || [])
      .filter((item) => item.date === iso)
      .map((item) => replacementDisplaySource(item.source)))];
    const received = (replacementsDoc && replacementsDoc.items || [])
      .filter((item) => item.date === iso && item.receivedAt)
      .map((item) => item.receivedAt)
      .sort();
    const receivedAt = received.length ? received[received.length - 1] : null;
    const dateLabel = `${formatWeekday(date)}, ${formatDateFull(date)}`;
    const dateInput = `<label class="field date-field"><span>Дата</span><input type="date" id="replacementDateInput" value="${escapeHtml(iso)}" aria-label="Дата замен"></label>`;

    const nav = `<section class="card week-toolbar-card" aria-label="Выбор даты">
      <div class="week-toolbar">
        <button type="button" class="icon-button" data-action="replacement-prev" aria-label="Предыдущий день"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m15 5-7 7 7 7"/></svg></button>
        <div class="week-title">
          <p class="eyebrow">${isToday ? 'Сегодня' : 'Выбранная дата'}</p>
          <h2>${escapeHtml(dateLabel)}</h2>
          <div class="week-chips">
            ${chip(weekTypeLabel(result.weekType), 'accent')}
            ${totalReplacements ? chip(`${totalReplacements} ${plural(totalReplacements, 'замена', 'замены', 'замен')}`, 'warn') : chip('замен нет', 'neutral')}
            ${!isToday ? '<button type="button" class="text-button" data-action="replacement-today">Вернуться к сегодня</button>' : ''}
          </div>
        </div>
        <button type="button" class="icon-button" data-action="replacement-next" aria-label="Следующий день"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m9 5 7 7-7 7"/></svg></button>
      </div>
      <div class="date-row">${dateInput}<span class="hint">Числитель/знаменатель определяется по дате из schedule.json.</span></div>
    </section>`;

    if (isWeekend(date)) {
      return `${nav}${emptyState('В выходной занятий нет', 'Выберите будний день, чтобы посмотреть обычное расписание и замены.', '<button type="button" class="button" data-action="replacement-next-weekday">Ближайший будний день</button>')}`;
    }

    const lessonsHtml = result.lessons.length
      ? `<div class="lesson-list">${result.lessons.map(lessonCard).join('')}</div>`
      : emptyState('Занятий на эту дату нет', 'В базовом расписании на этот день нет пар.');

    const noChanges = totalReplacements === 0
      ? `<div class="banner banner-soft" role="status"><strong>Замен для 108М на эту дату нет.</strong><span>Ниже показано обычное расписание — это не пустая таблица.</span></div>`
      : '';

    const sourceLines = [];
    if (sources.length) sourceLines.push(`Источник: ${sources.join(', ')}`);
    if (receivedAt) sourceLines.push(`Замены получены: ${formatMoscowDateTime(receivedAt)}`);
    if (state.manifest) sourceLines.push(`Публикация: ${formatMoscowDateTime(state.manifest.generatedAt)}`);
    const sourceBlock = sourceLines.length
      ? `<p class="source-note">${sourceLines.map(escapeHtml).join(' · ')}</p>`
      : '';

    return `${nav}${noChanges}
      <section class="card replacements-card" aria-labelledby="replacements-title">
        ${sectionHeading('Занятия на день', 'Замена действует только на выбранную дату и не меняет базовое расписание.', `<span class="chip chip-soft">${escapeHtml(formatWeekday(date))}</span>`, 'replacements-title')}
        <div>${lessonsHtml}</div>
        ${sourceBlock}
      </section>`;
  }

  /* ------------------------------------------------------------------ */
  /* Рендер и события                                                    */
  /* ------------------------------------------------------------------ */

  function currentSyncInfo() {
    if (state.refreshing) return { tone: 'loading', text: 'обновляем данные…' };
    if (state.status === 'loading') return { tone: 'loading', text: 'загружаем данные…' };
    if (state.status === 'error') return { tone: 'error', text: 'ошибка загрузки' };
    if (state.status === 'stale') return { tone: 'stale', text: 'нет связи · показаны старые данные' };
    if (state.manifest) {
      const publication = formatMoscowDateTime(state.manifest.generatedAt);
      if (hoursSince(state.manifest.generatedAt) > STALE_AFTER_HOURS) {
        return { tone: 'stale', text: `данные от ${publication || 'неизвестной даты'}` };
      }
      return { tone: 'ready', text: `обновлено ${publication || 'недавно'}` };
    }
    return { tone: 'loading', text: 'проверяем данные…' };
  }

  function renderHeader() {
    const sync = currentSyncInfo();
    const status = document.getElementById('syncStatus');
    const text = document.getElementById('syncText');
    if (status) status.dataset.state = sync.tone;
    if (text) text.textContent = sync.text;
    const refresh = document.getElementById('refreshButton');
    if (refresh) {
      refresh.disabled = state.status === 'loading' || state.refreshing;
      refresh.classList.toggle('is-busy', state.status === 'loading' || state.refreshing);
    }
    const footer = document.getElementById('footerStamp');
    if (footer) {
      const publication = state.manifest ? formatMoscowDateTime(state.manifest.generatedAt) : '';
      footer.textContent = publication
        ? `Последняя публикация: ${publication} (Москва)${state.manifest && state.manifest.revision ? ` · rev ${shortRevision(state.manifest.revision)}` : ''}`
        : 'Данные ещё не загружены.';
    }
    const badges = document.getElementById('pageBadges');
    if (badges) {
      const items = [chip('108М', 'soft')];
      if (state.status === 'stale') items.push(chip('устаревшие данные', 'warn'));
      if (state.manifest) items.push(chip(`rev ${shortRevision(state.manifest.revision)}`, 'ghost'));
      badges.innerHTML = items.join('');
    }
  }

  function renderNav() {
    for (const button of document.querySelectorAll('[data-view]')) {
      const active = button.dataset.view === state.view;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
  }

  function renderPageMeta() {
    const meta = VIEW_META[state.view] || VIEW_META.schedule;
    const title = document.getElementById('pageTitle');
    const intro = document.getElementById('pageIntro');
    if (title) title.textContent = meta.title;
    if (intro) intro.textContent = meta.intro;
  }

  function renderViewContent() {
    const container = document.getElementById('view');
    if (!container) return;
    if (state.status === 'loading' && !viewDataReady(state.view)) {
      container.innerHTML = loadingState();
      return;
    }
    if (state.status === 'error' && !viewDataReady(state.view)) {
      container.innerHTML = errorState(state.error, true);
      return;
    }
    if (!viewDataReady(state.view)) {
      container.innerHTML = errorState(state.error || 'Данные для этого раздела ещё не загружены.', true);
      return;
    }
    const renderers = {
      schedule: renderSchedule,
      grades: renderGrades,
      tasks: renderTasks,
      duties: renderDuties,
      replacements: renderReplacements,
    };
    const renderer = renderers[state.view] || renderSchedule;
    let content = '';
    try {
      content = renderer();
    } catch (error) {
      content = errorState('Не удалось отобразить раздел. Обновите страницу.', true);
    }
    container.innerHTML = `${staleBanner()}${content}`;
  }

  function render() {
    renderHeader();
    renderNav();
    renderPageMeta();
    renderViewContent();
  }

  function setView(view) {
    if (!VIEW_META[view]) return;
    const changed = state.view !== view;
    state.view = view;
    if (viewDataReady(view) && state.status !== 'stale' && state.status !== 'loading') {
      state.status = 'ready';
      state.error = '';
    }
    if (changed) {
      state.weekShift = 0;
      try {
        history.pushState(null, '', `#${view}`);
      } catch (error) {
        /* file:// или ограниченный режим — состояние всё равно обновлено */
      }
    }
    render();
    if (state.status !== 'loading' && !viewDataReady(view)) {
      loadView(view, {});
    } else {
      announce(VIEW_META[view].title);
    }
    if (changed) window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function renderWithFocus(focusId) {
    render();
    if (focusId) {
      const element = document.getElementById(focusId);
      if (element) element.focus({ preventScroll: true });
    }
  }

  function handleAction(action, element) {
    if (!action) return;
    switch (action) {
      case 'week-prev':
        state.weekShift -= 1;
        render();
        break;
      case 'week-next':
        state.weekShift += 1;
        render();
        break;
      case 'week-today':
        state.weekShift = 0;
        render();
        break;
      case 'retry':
        loadView(state.view, { force: true, announce: true });
        break;
      case 'task-filter':
        state.taskFilter = element.dataset.filter === 'all' ? 'all' : 'active';
        render();
        break;
      case 'grade-group':
        state.gradeGroup = element.dataset.group || 'all';
        ensureSelections();
        renderWithFocus('studentSelect');
        break;
      case 'grades-all-periods':
        state.period = 'all';
        renderWithFocus('periodSelect');
        break;
      case 'replacement-prev':
        shiftReplacementDate(-1);
        renderWithFocus('replacementDateInput');
        break;
      case 'replacement-next':
        shiftReplacementDate(1);
        renderWithFocus('replacementDateInput');
        break;
      case 'replacement-today':
        state.replacementDate = moscowTodayISO();
        renderWithFocus('replacementDateInput');
        break;
      case 'replacement-next-weekday': {
        let date = parseISODate(state.replacementDate) || todayDate();
        do { date = addDays(date, 1); } while (isWeekend(date));
        state.replacementDate = toISODate(date);
        renderWithFocus('replacementDateInput');
        break;
      }
      default:
        break;
    }
  }

  function handleChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.id === 'studentSelect') {
      state.studentId = target.value;
      renderWithFocus('studentSelect');
      return;
    }
    if (target.id === 'periodSelect') {
      state.period = ['current', 'previous', 'all'].includes(target.value) ? target.value : 'current';
      renderWithFocus('periodSelect');
      return;
    }
    if (target.id === 'subjectSelect') {
      state.subjectId = target.value || 'all';
      renderWithFocus('subjectSelect');
      return;
    }
    if (target.id === 'replacementDateInput') {
      const parsed = parseISODate(target.value);
      if (parsed) {
        state.replacementDate = target.value;
        renderWithFocus('replacementDateInput');
      }
    }
  }

  function handleClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const navButton = target.closest('[data-view]');
    if (navButton && navButton.dataset.view) {
      event.preventDefault();
      setView(navButton.dataset.view);
      return;
    }
    const actionButton = target.closest('[data-action]');
    if (actionButton) {
      event.preventDefault();
      handleAction(actionButton.dataset.action, actionButton);
    }
  }

  function handleHash() {
    const raw = (window.location.hash || '').replace(/^#/, '').split('/')[0];
    const view = VIEW_META[raw] ? raw : 'schedule';
    if (view !== state.view) setView(view);
  }

  function init() {
    document.addEventListener('click', handleClick);
    document.addEventListener('change', handleChange);
    const refresh = document.getElementById('refreshButton');
    if (refresh) refresh.addEventListener('click', () => loadView(state.view, { force: true, announce: true }));
    window.addEventListener('hashchange', handleHash);
    window.addEventListener('popstate', handleHash);
    window.addEventListener('online', () => {
      if (state.status === 'stale' || state.status === 'error') loadView(state.view, { force: true, announce: true });
    });
    const raw = (window.location.hash || '').replace(/^#/, '').split('/')[0];
    state.view = VIEW_META[raw] ? raw : 'schedule';
    render();
    loadView(state.view, {});
    window.setInterval(() => {
      if (document.hidden || state.status !== 'ready' || state.refreshing) return;
      fetchManifest()
        .then((manifest) => {
          if (state.manifest && manifest.revision !== state.manifest.revision) {
            return loadView(state.view, { force: true, announce: true });
          }
          return undefined;
        })
        .catch(() => {
          /* тихая проверка не должна менять экран; следующая загрузка покажет ошибку */
        });
    }, 5 * 60 * 1000);
  }

  /* ------------------------------------------------------------------ */
  /* Экспорт для Node-тестов                                             */
  /* ------------------------------------------------------------------ */

  const api = {
    escapeHtml,
    parseISODate,
    toISODate,
    moscowTodayISO,
    addDays,
    startOfWeek,
    weekDates,
    weekTypeForDate,
    weekTypeLabel,
    formatDateLong,
    effectiveLessons,
    taskDisplayStatus,
    dutyReason,
    periodRange,
    plural,
    normalizeText,
    VIEW_FILES,
    REQUIRED_FILES,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
  if (typeof window !== 'undefined') {
    window.__108m = api;
  }
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }
})();
