const weekdays = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница'];
const schedule = [
  [
    { name: 'Математика', teacher: 'Капаева', room: '2к302' },
    { name: 'Иностранный язык', teacher: 'Шарипова, Шипанова', room: '2к207, 2к202' },
    { name: 'ОБиЗР', teacher: 'Жукова', room: '2к407' },
    { name: 'Русский язык', teacher: 'Маршинкина', room: '2к307' }
  ],
  [
    { name: 'Введение в специальность', teacher: 'Мингажева' },
    { name: 'Математика', teacher: 'Капаева', room: '2к302' },
    { name: 'Химия', teacher: 'Мусина', room: '2к105' },
    { name: 'Русский язык', teacher: 'Маршинкина', room: '2к307' }
  ],
  [
    { name: 'Русский язык', teacher: 'Маршинкина', room: '2к307' },
    { name: 'Русский язык', teacher: 'Маршинкина', room: '2к307' },
    { name: 'Иностранный язык', teacher: 'Шарипова, Шипанова', room: '2к207, 2к202' },
    { name: 'Родная литература', teacher: 'Садыкова', room: '2к301' }
  ],
  [
    { name: 'Биология', teacher: 'Кипрова', room: 'Л307' },
    { name: 'Информатика', teacher: 'Филенкова', room: '406' },
    { name: 'История', teacher: 'Бятикова', room: '227', split: true },
    { name: 'Информатика', teacher: 'Филенкова', room: '406', split: true }
  ],
  [
    { name: 'Физическая культура', room: 'Спортзал', split: true },
    { name: 'Математика', teacher: 'Капаева', room: '2к302', split: true },
    { name: 'Физическая культура', teacher: 'Федосеев', room: 'Спортзал' },
    { name: 'История', teacher: 'Бятикова', room: '227' }
  ]
];
const state = { weekShift: 0, tasks: [] };
const $ = selector => document.querySelector(selector);
async function loadTasks() { try { const response = await fetch('tasks.json', { cache: 'no-store' }); return response.ok ? await response.json() : []; } catch { try { return JSON.parse(localStorage.getItem('planner-tasks') || '[]'); } catch { return []; } } }
function getDates() {
  const now = new Date();
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - (now.getDay() + 6) % 7 + state.weekShift * 7);
  return weekdays.map((_, i) => new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + i));
}
function formatDate(date, options = { day: 'numeric', month: 'short' }) { return date.toLocaleDateString('ru-RU', options).replace('.', ''); }
function update() {
  const dates = getDates();
  if (!state.weekShift) $('#weekTitle').textContent = 'Эта неделя';
  else $('#weekTitle').textContent = `${formatDate(dates[0])} — ${formatDate(dates[4])}`;
  $('#dayHeaders').innerHTML = '<th class="number-head">№</th>' + dates.map((date, i) => {
    const today = state.weekShift === 0 && date.toDateString() === new Date().toDateString();
    return `<th class="${today ? 'today' : ''}">${weekdays[i]}<small>${formatDate(date)}</small>${today ? '<i>сегодня</i>' : ''}</th>`;
  }).join('');
  const maxLessons = Math.max(...schedule.map(day => day.length));
  $('#scheduleBody').innerHTML = Array.from({ length: maxLessons }, (_, row) => `<tr><th class="lesson-no">${row + 1}</th>${schedule.map(day => {
    const lesson = day[row];
    if (!lesson) return '<td class="empty-cell"></td>';
    return `<td><div class="subject">${escapeHtml(lesson.name)}</div>${lesson.teacher ? `<div class="teacher">${escapeHtml(lesson.teacher)}</div>` : ''}${lesson.room ? `<div class="room">${escapeHtml(lesson.room)}</div>` : ''}${lesson.split ? '<span class="split" title="Разделённая пара">½</span>' : ''}</td>`;
  }).join('')}</tr>`).join('');
  renderTasks(dates);
}
function renderTasks(dates) {
  const keys = dates.map(date => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`);
  const tasks = state.tasks.filter(task => keys.includes(task.dueDay));
  $('#taskBadge').hidden = tasks.length === 0;
  $('#taskBadge').textContent = tasks.length || '';
  $('#taskContent').innerHTML = tasks.length ? tasks.map(task => `<p><b>${escapeHtml(task.subject)}</b> — ${escapeHtml(task.text)} <small>${escapeHtml(task.dueLabel)}</small></p>`).join('') : '<p class="empty-tasks">Заданий пока нет. Они появятся здесь после добавления через Telegram-бота.</p>';
}
function escapeHtml(value) { return String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]); }
$('#prevWeek').addEventListener('click', () => { state.weekShift--; update(); });
$('#nextWeek').addEventListener('click', () => { state.weekShift++; update(); });
$('#weekTitle').addEventListener('click', () => { state.weekShift = 0; update(); });
function toggleTasks() {
  const panel = $('#tasksPanel'); const open = panel.hidden;
  panel.hidden = !open;
  $('#showTasks').setAttribute('aria-expanded', String(open));
  $('#tasksToggle').setAttribute('aria-expanded', String(open));
}
$('#showTasks').addEventListener('click', toggleTasks);
$('#tasksToggle').addEventListener('click', toggleTasks);
loadTasks().then(tasks => { state.tasks = Array.isArray(tasks) ? tasks : []; update(); });
update();
