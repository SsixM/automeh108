'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const app = require('../../app.js');

const META = { weekAnchor: '2026-08-31', firstWeekType: 'numerator' };

function day(iso) {
  return app.parseISODate(iso);
}

test('escapeHtml escapes every dangerous character', () => {
  assert.equal(
    app.escapeHtml('<img src=x onerror="alert(1)">&\''),
    '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;',
  );
  assert.equal(app.escapeHtml(null), '');
});

test('dates round-trip as YYYY-MM-DD without UTC shifts', () => {
  for (const iso of ['2026-01-01', '2026-03-29', '2026-11-18', '2026-12-31']) {
    assert.equal(app.toISODate(app.parseISODate(iso)), iso);
  }
  assert.equal(app.parseISODate('2026-02-30'), null);
  assert.equal(app.parseISODate('18.11.2026'), null);
});

test('week starts on Monday and has five school days', () => {
  const monday = app.weekDates(0, day('2026-11-18'));
  assert.deepEqual(monday.map(app.toISODate), [
    '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20',
  ]);
  const previous = app.weekDates(-1, day('2026-11-18'));
  assert.equal(app.toISODate(previous[0]), '2026-11-09');
});

test('week type alternates from the configured anchor', () => {
  assert.equal(app.weekTypeForDate(day('2026-09-01'), META), 'numerator');
  assert.equal(app.weekTypeForDate(day('2026-09-07'), META), 'denominator');
  assert.equal(app.weekTypeForDate(day('2026-09-14'), META), 'numerator');
  assert.equal(app.weekTypeForDate(day('2026-09-21'), META), 'denominator');
  assert.equal(app.weekTypeForDate(day('2026-11-16'), META), 'denominator');
  assert.equal(app.weekTypeForDate(day('2026-11-09'), META), 'numerator');
  assert.equal(
    app.weekTypeForDate(day('2026-09-01'), { ...META, firstWeekType: 'denominator' }),
    'denominator',
  );
});

test('effectiveLessons applies denominator rules and individual replacements', () => {
  const schedule = {
    meta: META,
    times: [],
    lessons: [
      {
        lessonId: 'thu-4-history', weekday: 4, pairNumber: 4, subjectId: 'history',
        subject: 'История', teacher: 'Батыкова', room: '227', subgroup: 'all',
        weekType: 'all', service: false,
      },
      {
        lessonId: 'fri-1-pe', weekday: 5, pairNumber: 1, subjectId: 'pe',
        subject: 'Физическая культура', teacher: '', room: '', subgroup: 'all',
        weekType: 'all', service: false,
      },
    ],
    replacementRules: [
      {
        ruleId: 'thu-rule', weekday: 4, pairNumber: 4, weekType: 'denominator',
        originalLessonId: 'thu-4-history', newSubjectId: 'informatics',
        newSubject: 'Информатика', newRoom: '406', newTeacher: 'Филенкова',
      },
      {
        ruleId: 'fri-rule', weekday: 5, pairNumber: 1, weekType: 'denominator',
        originalLessonId: 'fri-1-pe', newSubjectId: 'math',
        newSubject: 'Математика', newRoom: '2к302',
      },
    ],
  };
  const data = {
    'schedule.json': schedule,
    'replacements.json': {
      items: [
        {
          replacementId: 'r1', date: '2026-11-12', weekType: 'numerator',
          pairNumber: 4, originalLessonId: 'thu-4-history', newSubjectId: 'chemistry',
          newSubject: 'Химия', newRoom: '2к105', cancelled: false,
          source: 'VK post 1', sourceHash: 'a'.repeat(64), receivedAt: '2026-11-18T20:01:00+03:00',
          group: '108М',
        },
      ],
    },
  };

  const denominatorThursday = app.effectiveLessons(day('2026-11-19'), data);
  assert.equal(denominatorThursday.weekType, 'denominator');
  assert.equal(denominatorThursday.lessons[0].subjectId, 'informatics');
  assert.equal(denominatorThursday.lessons[0].originalSubject, 'История');
  assert.equal(denominatorThursday.lessons[0].replacementKind, 'rule');

  const numeratorThursday = app.effectiveLessons(day('2026-11-12'), data);
  assert.equal(numeratorThursday.weekType, 'numerator');
  assert.equal(numeratorThursday.lessons[0].subjectId, 'chemistry');
  assert.equal(numeratorThursday.lessons[0].replacementKind, 'individual');

  const friday = app.effectiveLessons(day('2026-11-20'), data);
  assert.equal(friday.weekType, 'denominator');
  assert.equal(friday.lessons[0].subjectId, 'math');
  assert.equal(friday.lessons[0].room, '2к302');
});

test('cancelled replacement removes the lesson without touching other pairs', () => {
  const data = {
    'schedule.json': {
      meta: META,
      lessons: [
        { lessonId: 'a', weekday: 1, pairNumber: 1, subjectId: 'math', subject: 'Математика', teacher: '', room: '', subgroup: 'all', weekType: 'all', service: false },
        { lessonId: 'b', weekday: 1, pairNumber: 2, subjectId: 'russian', subject: 'Русский язык', teacher: '', room: '', subgroup: 'all', weekType: 'all', service: false },
      ],
      replacementRules: [],
    },
    'replacements.json': {
      items: [{
        replacementId: 'cancel', date: '2026-11-09', weekType: 'numerator',
        pairNumber: 1, cancelled: true, newSubjectId: 'math', newSubject: 'Математика',
        newRoom: '', source: 'telegram', sourceHash: 'b'.repeat(64), receivedAt: '2026-11-09T09:00:00+03:00', group: '108М',
      }],
    },
  };
  const result = app.effectiveLessons(day('2026-11-09'), data);
  assert.deepEqual(result.lessons.map((lesson) => lesson.lessonId), ['b']);
});

test('task status is derived from the next lesson date and manual completion', () => {
  const today = '2026-11-18';
  assert.equal(app.taskDisplayStatus({ status: 'актуальное', nextLessonDate: '2026-11-20' }, today), 'актуальное');
  assert.equal(app.taskDisplayStatus({ status: 'актуальное', nextLessonDate: '2026-11-17' }, today), 'просрочено');
  assert.equal(app.taskDisplayStatus({ status: 'актуальное', nextLessonDate: '2026-11-20', movedByReplacement: true }, today), 'перенесено');
  assert.equal(app.taskDisplayStatus({ status: 'выполнено', nextLessonDate: '2026-11-10' }, today), 'выполнено');
});

test('duty reason never claims a credit without a confirmed report', () => {
  assert.equal(app.dutyReason({ status: 'selected', actual: [] }), 'выбран');
  assert.equal(app.dutyReason({ status: 'closed-without-confirmation', actual: [] }), 'не подтверждён');
  assert.equal(app.dutyReason({ status: 'closed', actual: ['s01'] }), 'подтверждён сообщением');
  assert.equal(app.dutyReason({ status: 'pending', actual: [] }), 'ожидает проверки');
});

test('periodRange uses calendar months for attestation filters', () => {
  const current = app.periodRange('current');
  const previous = app.periodRange('previous');
  assert.match(current.start, /^\d{4}-\d{2}-01$/);
  assert.match(current.end, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(previous.end < current.start);
  assert.deepEqual(app.periodRange('all'), { start: null, end: null });
});

test('plural picks Russian forms', () => {
  assert.equal(app.plural(1, 'замена', 'замены', 'замен'), 'замена');
  assert.equal(app.plural(2, 'замена', 'замены', 'замен'), 'замены');
  assert.equal(app.plural(5, 'замена', 'замены', 'замен'), 'замен');
  assert.equal(app.plural(11, 'замена', 'замены', 'замен'), 'замен');
  assert.equal(app.plural(21, 'замена', 'замены', 'замен'), 'замена');
});
