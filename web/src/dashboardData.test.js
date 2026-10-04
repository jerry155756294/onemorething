import assert from 'node:assert/strict';
import test from 'node:test';
import {
  compareEventsByStart,
  dateKey,
  isEventActiveOrUpcoming,
  parseDateLabel,
  normalizeEvent,
  selectNextEvent,
  sortEventsByStart,
} from './dashboardData.js';

process.env.TZ = 'Asia/Taipei';

const now = new Date('2026-09-24T10:00:00+08:00');
test('dashboard fixtures pin the local clock to Asia/Taipei', () => {
  const local = new Date(2026, 8, 24, 11, 15, 0);
  assert.equal(local.getTimezoneOffset(), -480);
  assert.equal(local.toISOString(), '2026-09-24T03:15:00.000Z');
});

const fixture = [
  { id: 'nextcloud-0815', title: 'Nextcloud 08:15', start_at: '2026-09-24T08:15:00+08:00', end_at: '2026-09-24T09:00:00+08:00', source_type: 'calendar', source: { name: '課表' } },
  { id: 'omt-0915', title: 'OMT 09:15', start_at: '2026-09-24T09:15:00+08:00', end_at: '2026-09-24T10:45:00+08:00', created_source: 'ai_generated' },
  { id: 'user-1115', title: '使用者 11:15', date_iso: '2026-09-24', time_label: '11:15-12:00' },
  { id: 'ai-1320', title: 'AI 13:20', start_iso: '2026-09-24T13:20:00+08:00', end_at: '2026-09-24T14:00:00+08:00', created_source: 'ai_generated' },
  { id: 'tomorrow-0800', title: '明天 08:00', start_at: '2026-09-25T08:00:00+08:00', end_at: '2026-09-25T09:00:00+08:00' },
];

test('normalizes and sorts every provider by the same start instant', () => {
  const events = fixture.map((event) => normalizeEvent(event, now));
  assert.deepEqual(sortEventsByStart(events).map((event) => event.id), [
    'nextcloud-0815',
    'omt-0915',
    'user-1115',
    'ai-1320',
    'tomorrow-0800',
  ]);
  assert.equal(compareEventsByStart(events[2], events[3]) < 0, true);
});

test('selects the nearest event that has not started yet, skipping ongoing events', () => {
  const events = fixture.map((event) => normalizeEvent(event, now));
  const next = selectNextEvent(events, now);
  assert.equal(next.id, 'user-1115');
  assert.equal(next.isOngoing, undefined);
});

test('skips ended events and falls through to the nearest future day', () => {
  const events = fixture.map((event) => normalizeEvent(event, now));
  const futureToday = selectNextEvent(events.filter((event) => event.id !== 'omt-0915'), now);
  assert.equal(futureToday.id, 'user-1115');

  const tomorrow = selectNextEvent(events.filter((event) => !['nextcloud-0815', 'omt-0915', 'user-1115', 'ai-1320'].includes(event.id)), now);
  assert.equal(tomorrow.id, 'tomorrow-0800');
  assert.equal(isEventActiveOrUpcoming(events[0], now), false);
});

test('at 16:42 skips earlier and ongoing same-day events for tomorrow at 08:15', () => {
  const lateNow = new Date('2026-09-24T16:42:00+08:00');
  const events = [
    { id: 'today-0815', title: '今天 08:15', start_at: '2026-09-24T08:15:00+08:00', end_at: '2026-09-24T09:00:00+08:00' },
    { id: 'today-ongoing', title: '今天 16:00', start_at: '2026-09-24T16:00:00+08:00', end_at: '2026-09-24T17:00:00+08:00' },
    { id: 'tomorrow-0815', title: '明天 08:15', start_at: '2026-09-25T08:15:00+08:00', end_at: '2026-09-25T09:00:00+08:00' },
  ].map((event) => normalizeEvent(event, lateNow));
  assert.equal(selectNextEvent(events, lateNow).id, 'tomorrow-0815');
});

test('does not invent metadata when location, description, or source name is absent', () => {
  const event = normalizeEvent({
    id: 'empty-fields',
    title: '沒有附加欄位',
    start_at: '2026-09-24T13:20:00+08:00',
    detail: '來自行事曆',
    description: '請回看原始內容確認細節。',
  }, now);
  assert.equal(event.location, '');
  assert.equal(event.description, '');
  assert.equal(Object.hasOwn(event, 'sourceLabel'), false);
  assert.equal(Object.hasOwn(event, 'isExternal'), false);
});

test('normalizes local, AI, and provider events into the same Calendar presentation fields', () => {
  const samples = [
    { id: 'local', title: 'Local', start_at: '2026-09-24T13:20:00+08:00', event_source: 'local' },
    { id: 'ai', title: 'AI', start_at: '2026-09-24T13:20:00+08:00', created_source: 'ai_generated', external_calendar_ref: 'remote-1' },
    { id: 'provider', title: 'Provider', start_at: '2026-09-24T13:20:00+08:00', event_source: 'external_calendar', source_type: 'calendar' },
  ].map((event) => normalizeEvent(event, now));

  for (const event of samples) {
    assert.equal(event.timeLabel, '13:20');
    assert.equal(Object.hasOwn(event, 'sourceLabel'), false);
    assert.equal(Object.hasOwn(event, 'syncLabel'), false);
    assert.equal(Object.hasOwn(event, 'isExternal'), false);
  }
});

test('rejects invalid numeric dates instead of rolling them into another month', () => {
  assert.equal(parseDateLabel('2026-09-31', now), null);
  assert.equal(parseDateLabel('9/31', now), null);
  assert.equal(dateKey('2026-09-31'), '');
  assert.equal(normalizeEvent({ id: 'invalid-date', start_at: '2026-09-31T10:00:00+08:00' }, now).date, null);
  assert.equal(dateKey(parseDateLabel('2026-09-30', now)), '2026-09-30');
});

test('normalized event date labels use the compact OMT v2 Traditional Chinese presentation', () => {
  const event = normalizeEvent({
    id: 'date-label',
    title: '日期格式',
    start_at: '2026-09-29T14:20:00+08:00',
  }, new Date('2026-09-29T08:00:00+08:00'));
  assert.equal(event.dateLabel, '9 月 29 日');
});

test('legacy weekday labels stay anchored to the event timestamp and expire after that day', () => {
  const saturday = new Date('2026-10-03T11:30:00+08:00');
  const event = normalizeEvent({
    id: 'legacy-thursday-duty',
    title: '職勤',
    date_label: '星期四',
    time_label: '全天',
    updated_at: '2026-10-01T09:00:00+08:00',
  }, saturday);
  assert.equal(event.dateKey, '2026-10-01');
  assert.equal(event.status, 'overdue');
  assert.equal(isEventActiveOrUpcoming(event, saturday), false);
});

test('start-only events automatically leave upcoming after their start time', () => {
  const event = normalizeEvent({
    id: 'start-only',
    title: '短行程',
    start_at: '2026-10-03T10:00:00+08:00',
  }, new Date('2026-10-03T10:00:01+08:00'));
  assert.equal(isEventActiveOrUpcoming(event, new Date('2026-10-03T10:00:01+08:00')), false);
});
