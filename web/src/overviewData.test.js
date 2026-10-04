import assert from 'node:assert/strict';
import test from 'node:test';
import { canPresentCollectionConclusion, countCalendarEventsByDate, countRemainingEventsToday, selectCalendarAgendaEvents, selectNextOverviewEvent, selectOverviewTasks, selectUpcomingOverviewEvents } from './overviewData.js';

const now = new Date('2026-09-24T16:42:00+08:00');

test('a delayed empty collection is not presented as a conclusion before its response succeeds', async () => {
  const state = { loaded: false, loading: true, error: '' };
  let finishRequest;
  const pending = new Promise((resolve) => { finishRequest = resolve; });
  assert.equal(canPresentCollectionConclusion(state), false);

  finishRequest({ items: [] });
  const response = await pending;
  state.loaded = Array.isArray(response.items);
  state.loading = false;
  assert.equal(canPresentCollectionConclusion(state), true);
  assert.equal(canPresentCollectionConclusion({ loaded: false, loading: false, error: 'failed' }), false);
});

test('groups incomplete overdue tasks before today and omits completed tasks', () => {
  const result = selectOverviewTasks([
    { id: 'overdue', title: '逾期', due_label: '9/23', status: 'open' },
    { id: 'today', title: '今天', due_label: '今天', status: 'needs_review' },
    { id: 'done-overdue', title: '已完成', due_label: '9/23', status: 'done' },
    { id: 'future', title: '未來', due_label: '9/25', status: 'open' },
  ], now);
  assert.deepEqual(result.overdue.map((task) => task.id), ['overdue']);
  assert.deepEqual(result.today.map((task) => task.id), ['today']);
});

test('counts only ongoing and future events remaining today', () => {
  const events = [
    { id: 'ended-today', dateKey: '2026-09-24', start_at: '2026-09-24T15:00:00+08:00', end_at: '2026-09-24T15:30:00+08:00' },
    { id: 'ongoing-today', dateKey: '2026-09-24', start_at: '2026-09-24T16:30:00+08:00', end_at: '2026-09-24T17:00:00+08:00' },
    { id: 'future-today', dateKey: '2026-09-24', start_at: '2026-09-24T18:00:00+08:00', end_at: '2026-09-24T19:00:00+08:00' },
    { id: 'tomorrow', dateKey: '2026-09-25', start_at: '2026-09-25T08:15:00+08:00', end_at: '2026-09-25T09:00:00+08:00' },
  ];
  assert.equal(countRemainingEventsToday(events, now), 2);
});

test('selects the nearest upcoming event by full datetime across month boundaries', () => {
  const events = [
    { id: 'later-day', start_at: '2026-10-02T08:00:00+08:00', end_at: '2026-10-02T09:00:00+08:00' },
    { id: 'earlier-month-boundary', start_at: '2026-09-25T08:00:00+08:00', end_at: '2026-09-25T09:00:00+08:00' },
    { id: 'earlier-time', start_at: '2026-09-25T07:00:00+08:00', end_at: '2026-09-25T08:00:00+08:00' },
    { id: 'already-ended', start_at: '2026-09-24T15:00:00+08:00', end_at: '2026-09-24T15:30:00+08:00' },
  ];
  assert.equal(selectNextOverviewEvent(events, now)?.id, 'earlier-time');
});

test('Today lists remaining events on the nearest active date in chronological order', () => {
  const events = [
    { id: 'tomorrow', dateKey: '2026-09-25', start_at: '2026-09-25T08:00:00+08:00', end_at: '2026-09-25T09:00:00+08:00' },
    { id: 'later-today', dateKey: '2026-09-24', start_at: '2026-09-24T18:00:00+08:00', end_at: '2026-09-24T19:00:00+08:00' },
    { id: 'ongoing-today', dateKey: '2026-09-24', start_at: '2026-09-24T16:30:00+08:00', end_at: '2026-09-24T17:00:00+08:00' },
    { id: 'ended-today', dateKey: '2026-09-24', start_at: '2026-09-24T15:00:00+08:00', end_at: '2026-09-24T15:30:00+08:00' },
  ];
  assert.deepEqual(selectUpcomingOverviewEvents(events, now).map((event) => event.id), ['ongoing-today', 'later-today']);
});

test('calendar agenda stays empty until a date is selected and shows that full date', () => {
  const dates = ['2026-09-25', '2026-09-26', '2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02'];
  const events = dates.map((day, index) => ({ id: `event-${index}`, dateKey: day, start_at: `${day}T08:00:00+08:00`, end_at: `${day}T09:00:00+08:00` }));
  assert.deepEqual(selectCalendarAgendaEvents(events), []);
  assert.deepEqual(selectCalendarAgendaEvents(events, '2026-09-27').map((event) => event.id), ['event-2']);
});

test('selected calendar date keeps every event and event counts include every loaded event', () => {
  const events = [
    { id: 'one', dateKey: '2026-09-25', start_at: '2026-09-25T08:00:00+08:00' },
    { id: 'two', dateKey: '2026-09-25', start_at: '2026-09-25T09:00:00+08:00' },
    { id: 'other-day', dateKey: '2026-10-01', start_at: '2026-10-01T09:00:00+08:00' },
  ];
  assert.deepEqual(selectCalendarAgendaEvents(events, '2026-09-25', now).map((event) => event.id), ['one', 'two']);
  assert.deepEqual(countCalendarEventsByDate(events), { '2026-09-25': 2, '2026-10-01': 1 });
});
