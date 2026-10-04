import assert from 'node:assert/strict';
import test from 'node:test';
import { createLatestRequestGate, fetchCalendarPages, monthKey, monthStart, preserveChecklistItems, shiftMonth, withoutDeletedLocalEventSeries } from './calendarData.js';

test('confirmed recurring event deletion removes the series from every cached calendar view', () => {
  const events = [
    { id: 'event-a12bc3' },
    { id: 'event-a12bc3@2026-09-28' },
    { id: 'event-a12bc3@2026-10-05' },
    { id: 'event-f98de7@2026-09-28' },
    { id: 'external-event-a12bc3' },
  ];

  assert.deepEqual(withoutDeletedLocalEventSeries(events, 'event-a12bc3@2026-10-05'), [
    { id: 'event-f98de7@2026-09-28' },
    { id: 'external-event-a12bc3' },
  ]);
});

test('month navigation normalizes month-end dates before shifting', () => {
  assert.equal(monthKey(monthStart(new Date(2026, 0, 31))), '2026-01');
  assert.equal(monthKey(shiftMonth(new Date(2026, 0, 31), 1)), '2026-02');
  assert.equal(monthKey(shiftMonth(new Date(2026, 2, 31), -1)), '2026-02');
  assert.equal(monthKey(shiftMonth(new Date(2024, 1, 29), 1)), '2024-03');
});

test('loads every calendar page using the server next_offset contract', async () => {
  const allEvents = Array.from({ length: 205 }, (_, index) => ({ id: `event-${index}` }));
  const offsets = [];
  const result = await fetchCalendarPages(async (offset) => {
    offsets.push(offset);
    const events = allEvents.slice(offset, offset + 100);
    return { events, meta: { total_count: allEvents.length, next_offset: offset + events.length < allEvents.length ? offset + events.length : null } };
  });
  assert.deepEqual(offsets, [0, 100, 200]);
  assert.equal(result.events.length, 205);
  assert.equal(result.events.at(-1).id, 'event-204');
});

test('only the latest month request can publish results when Sep and Oct finish after Nov', async () => {
  const gate = createLatestRequestGate();
  const published = [];
  const pending = new Map();
  const start = (month) => {
    const id = gate.begin();
    return new Promise((resolve) => pending.set(month, () => {
      if (gate.isLatest(id)) published.push(month);
      resolve();
    }));
  };
  const sep = start('2026-09');
  const oct = start('2026-10');
  const nov = start('2026-11');
  pending.get('2026-11')();
  pending.get('2026-10')();
  pending.get('2026-09')();
  await Promise.all([sep, oct, nov]);
  assert.deepEqual(published, ['2026-11']);
});

test('a late response for a previously selected task list cannot replace the current list', async () => {
  const gate = createLatestRequestGate();
  const published = [];
  const pending = new Map();
  const start = (listId) => {
    const id = gate.begin();
    return new Promise((resolve) => pending.set(listId, () => {
      if (gate.isLatest(id)) published.push(listId);
      resolve();
    }));
  };
  const previous = start('list-previous');
  const selected = start('list-selected');
  pending.get('list-selected')();
  pending.get('list-previous')();
  await Promise.all([previous, selected]);
  assert.deepEqual(published, ['list-selected']);
});

test('a late source response cannot replace the latest source or its loading and error state', async () => {
  const gate = createLatestRequestGate();
  const state = { source: null, loadingId: '', errorId: '', error: '' };
  const pending = new Map();
  const start = (sourceId) => {
    const requestId = gate.begin();
    state.source = null;
    state.loadingId = sourceId;
    state.errorId = '';
    state.error = '';
    return new Promise((resolve) => pending.set(sourceId, (source) => {
      if (gate.isLatest(requestId)) {
        state.source = source;
        state.loadingId = '';
        state.errorId = source ? '' : sourceId;
        state.error = source ? '' : 'failed';
      }
      resolve();
    }));
  };

  const sourceA = start('source-A');
  const sourceB = start('source-B');
  pending.get('source-B')({ id: 'source-B' });
  pending.get('source-A')({ id: 'source-A' });
  await Promise.all([sourceA, sourceB]);

  assert.deepEqual(state, { source: { id: 'source-B' }, loadingId: '', errorId: '', error: '' });
});

test('rejects incomplete or non-advancing pagination metadata', async () => {
  let calls = 0;
  await assert.rejects(fetchCalendarPages(async () => {
    calls += 1;
    return { events: [{ id: 'one' }], meta: { total_count: 2, next_offset: null } };
  }), /未完整/);
  assert.equal(calls, 1);
  await assert.rejects(fetchCalendarPages(async () => ({
    events: [{ id: 'one' }], meta: { total_count: 2, next_offset: 0 },
  })), /分頁位置無效/);
  await assert.rejects(fetchCalendarPages(async () => ({
    events: [{ id: 'one' }], meta: { total_count: 2, next_offset: '100' },
  })), /分頁位置無效/);
});

test('preserves exact checklist matches before attempting same-position text edits', () => {
  const items = [
    { id: 'a', text: 'A', done: true },
    { id: 'b', text: 'B', done: false },
  ];
  assert.deepEqual(preserveChecklistItems(['A', 'B'], items), items);
  assert.deepEqual(preserveChecklistItems(['A edited', 'B'], items), [
    { id: 'a', text: 'A edited', done: true },
    { id: 'b', text: 'B', done: false },
  ]);
  assert.deepEqual(preserveChecklistItems(['B', 'A'], items), [items[1], items[0]]);
});

test('new items inserted before checked items default false without stealing their identity', () => {
  const items = [
    { id: 'a', text: 'A', done: true },
    { id: 'b', text: 'B', done: false },
  ];
  assert.deepEqual(preserveChecklistItems(['New', 'A', 'B'], items), [
    { text: 'New', done: false },
    items[0],
    items[1],
  ]);
});

test('deleting checklist lines keeps the remaining exact items and their state', () => {
  const items = [
    { id: 'a', text: 'A', done: true },
    { id: 'b', text: 'B', done: false },
  ];
  assert.deepEqual(preserveChecklistItems(['B'], items), [items[1]]);
});
