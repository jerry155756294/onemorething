const WEEKDAY_INDEX = { 日: 0, 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6 };

export function startOfDay(value = new Date()) {
  const date = new Date(value);
  date.setHours(0, 0, 0, 0);
  return date;
}

function strictDate(year, month, day) {
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  return date;
}

export function dateKey(value) {
  if (!value) return '';
  const text = typeof value === 'string' ? value.trim() : '';
  const dateOnly = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  const date = dateOnly
    ? strictDate(Number(dateOnly[1]), Number(dateOnly[2]), Number(dateOnly[3]))
    : new Date(value);
  if (!date || Number.isNaN(date.getTime())) return '';
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
}

function mondayOfWeek(value) {
  const date = startOfDay(value);
  const offset = (date.getDay() + 6) % 7;
  date.setDate(date.getDate() - offset);
  return date;
}

function relativeWeekday(value, now) {
  const match = String(value || '').match(/(?:(本週|這週|下週)\s*)?週?([日一二三四五六])/);
  if (!match) return null;
  const week = mondayOfWeek(now);
  const weekdayOffset = WEEKDAY_INDEX[match[2]] === 0 ? 6 : WEEKDAY_INDEX[match[2]] - 1;
  week.setDate(week.getDate() + weekdayOffset + (match[1] === '下週' ? 7 : 0));
  if (!match[1] && week < startOfDay(now)) week.setDate(week.getDate() + 7);
  return week;
}

export function parseDateLabel(value, now = new Date()) {
  const label = String(value || '').trim();
  if (!label) return null;
  const explicit = label.match(/(?:(\d{4})[\/-])?(\d{1,2})[\/-](\d{1,2})/) || label.match(/(?:(\d{4})年)?\s*(\d{1,2})月\s*(\d{1,2})日/);
  if (explicit) {
    const year = Number(explicit[1] || now.getFullYear());
    return strictDate(year, Number(explicit[2]), Number(explicit[3]));
  }
  if (/今天|今日/.test(label)) return startOfDay(now);
  if (/明天|明日/.test(label)) {
    const date = startOfDay(now);
    date.setDate(date.getDate() + 1);
    return date;
  }
  if (/昨天|昨日/.test(label)) {
    const date = startOfDay(now);
    date.setDate(date.getDate() - 1);
    return date;
  }
  return relativeWeekday(label, now);
}

function statusForDate(date, now) {
  if (!date) return 'unknown';
  const day = startOfDay(date).getTime();
  const today = startOfDay(now).getTime();
  if (day < today) return 'overdue';
  if (day === today) return 'today';
  return 'upcoming';
}

function clockLabel(value) {
  const match = String(value || '').match(/(?:T|\s)(\d{1,2}):(\d{2})/);
  if (!match) return '';
  return `${String(Number(match[1])).padStart(2, '0')}:${match[2]}`;
}

const EMPTY_EVENT_COPY = new Set(['請回看原始內容確認細節。', '來自行事曆']);

function cleanEventText(value) {
  const text = String(value ?? '').trim();
  return EMPTY_EVENT_COPY.has(text) ? '' : text;
}

function firstEventText(...values) {
  return values.map(cleanEventText).find(Boolean) || '';
}

function addClockToDate(dateValue, clock) {
  return dateValue && clock ? `${dateValue}T${clock}:00` : '';
}

function parseEventDateTime(value) {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : new Date(value);
  const text = cleanEventText(value);
  if (!text) return null;
  const isoDate = text.match(/^(\d{4})-(\d{2})-(\d{2})(?=T|$)/);
  if (isoDate && !strictDate(Number(isoDate[1]), Number(isoDate[2]), Number(isoDate[3]))) return null;
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? null : date;
}

function eventDateTimeValue(event, field) {
  const value = field === 'end'
    ? event?.endDateTime || event?.end_at || event?.end_iso || event?.end_datetime || event?.end
    : event?.startDateTime || event?.start_at || event?.start_iso || event?.start_datetime || event?.start;
  return parseEventDateTime(value);
}

export function eventStartTimestamp(event) {
  return eventDateTimeValue(event, 'start')?.getTime() ?? Number.POSITIVE_INFINITY;
}

export function eventEndTimestamp(event) {
  return eventDateTimeValue(event, 'end')?.getTime() ?? Number.NaN;
}

export function compareEventsByStart(first, second) {
  const startDelta = eventStartTimestamp(first) - eventStartTimestamp(second);
  if (startDelta) return startDelta;
  return String(first?.id ?? '').localeCompare(String(second?.id ?? ''));
}

export function sortEventsByStart(events) {
  return [...events].sort(compareEventsByStart);
}

export function isEventOngoing(event, now = new Date()) {
  const start = eventStartTimestamp(event);
  const end = eventEndTimestamp(event);
  const current = now.getTime();
  return Number.isFinite(start) && Number.isFinite(end) && start <= current && end > current;
}

export function isEventActiveOrUpcoming(event, now = new Date()) {
  const start = eventStartTimestamp(event);
  const end = eventEndTimestamp(event);
  const current = now.getTime();
  if (!Number.isFinite(start)) return false;
  if (Number.isFinite(end) && end > current && start <= current) return true;
  return start >= current;
}

export function selectNextEvent(events, now = new Date()) {
  const ordered = sortEventsByStart(events);
  return ordered.find((event) => eventStartTimestamp(event) > now.getTime()) || null;
}

function nextDateValue(dateValue) {
  if (!dateValue) return '';
  const date = new Date(`${dateValue}T00:00:00`);
  if (Number.isNaN(date.getTime())) return '';
  date.setDate(date.getDate() + 1);
  return dateKey(date);
}


function canonicalEventBounds(event, date) {
  const dateValue = dateKey(date);
  const rawTime = cleanEventText(event?.time_label);
  const rawTimes = rawTime.match(/\d{1,2}:\d{2}/g) || [];
  const startClock = rawTimes[0] || clockLabel(event?.start_at) || clockLabel(event?.start_iso);
  const endClock = rawTimes[1] || clockLabel(event?.end_at);
  const allDay = Boolean(event?.all_day || /^all[- ]?day$/i.test(rawTime) || /全天|整天/.test(rawTime));
  const start_at = firstEventText(event?.start_at, event?.start_iso, event?.start_datetime, event?.start) || addClockToDate(dateValue, startClock) || (allDay && dateValue ? `${dateValue}T00:00:00` : '');
  const end_at = firstEventText(event?.end_at, event?.end_iso, event?.end_datetime, event?.end) || addClockToDate(dateValue, endClock) || (allDay && dateValue ? `${nextDateValue(dateValue)}T00:00:00` : '');
  return { start_at, end_at, allDay, startClock, endClock, rawTime };
}

export function normalizeEventTime(event) {
  const raw = cleanEventText(event?.time_label);
  if (event?.all_day || /^all[- ]?day$/i.test(raw) || /全天|整天/.test(raw)) return '全天';
  const rawTimes = raw.match(/\d{1,2}:\d{2}/g) || [];
  const start = rawTimes[0] ? `${String(Number(rawTimes[0].split(':')[0])).padStart(2, '0')}:${rawTimes[0].split(':')[1]}` : clockLabel(event?.start_at);
  const end = rawTimes[1] ? `${String(Number(rawTimes[1].split(':')[0])).padStart(2, '0')}:${rawTimes[1].split(':')[1]}` : clockLabel(event?.end_at);
  if (start && end) return `${start}-${end}`;
  return start || raw || '';
}

export function normalizeEvent(event, now = new Date()) {
  const rawStart = firstEventText(event?.start_at, event?.start_iso, event?.start_datetime, event?.start);
  const parsedStart = parseEventDateTime(rawStart);
  // Legacy rows may still carry relative labels such as「星期四」or「今天」.
  // Resolve those labels against the row's own timestamp rather than the
  // current viewing day; otherwise a past weekday rolls forward every week
  // and incorrectly stays in「接下來」forever.
  const labelReference = parseEventDateTime(event?.updated_at || event?.created_at) || now;
  const date = parseDateLabel(rawStart || event?.date_iso || event?.date_label, labelReference);
  const bounds = canonicalEventBounds(event, date);
  const timeLabel = normalizeEventTime({ ...event, start_at: bounds.start_at, end_at: bounds.end_at, all_day: bounds.allDay });
  const allDay = bounds.allDay || (!bounds.startClock && !bounds.endClock && Boolean(date));
  const rawStatus = cleanEventText(event?.status);
  const marker = firstEventText(event?.marker, event?.category, rawStatus);
  const startDateTime = parseEventDateTime(bounds.start_at) || parsedStart;
  const endDateTime = parseEventDateTime(bounds.end_at);
  const displayDate = startDateTime || date;
  const status = statusForDate(displayDate, now);
  return {
    ...event,
    title: cleanEventText(event?.title),
    start_at: bounds.start_at,
    end_at: bounds.end_at,
    location: cleanEventText(event?.location),
    description: firstEventText(event?.description, event?.detail),
    marker,
    startDateTime,
    endDateTime,
    date: displayDate,
    dateKey: dateKey(displayDate),
    dateLabel: displayDate ? `${displayDate.getMonth() + 1} 月 ${displayDate.getDate()} 日` : '',
    timeLabel,
    allDay,
    status: status || (displayDate ? statusForDate(displayDate, now) : 'unknown'),
    statusLabel: displayDate ? { overdue: '已過期', today: '今天', upcoming: '即將到來' }[status] || '' : '',
  };
}

export function normalizeTask(task, now = new Date()) {
  const date = parseDateLabel(task.due_iso || task.due_label, now);
  const status = statusForDate(date, now);
  return {
    ...task,
    raw: task,
    dueDate: date,
    dueDateKey: dateKey(date),
    dueStatus: status,
    dueStatusLabel: { overdue: '已過期', today: '今天', upcoming: '', unknown: '日期待確認' }[status],
  };
}

export function calendarCells(monthDate = new Date()) {
  const first = new Date(monthDate.getFullYear(), monthDate.getMonth(), 1);
  const mondayOffset = (first.getDay() + 6) % 7;
  const start = new Date(first);
  start.setDate(first.getDate() - mondayOffset);
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start);
    date.setDate(start.getDate() + index);
    return {
      key: dateKey(date),
      date,
      day: date.getDate(),
      dateKey: dateKey(date),
      isCurrentMonth: date.getMonth() === first.getMonth(),
      isToday: dateKey(date) === dateKey(new Date()),
    };
  });
}

export function monthLabel(value = new Date()) {
  return new Intl.DateTimeFormat('zh-Hant-TW', { year: 'numeric', month: 'long' }).format(value);
}
