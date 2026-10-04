import test from 'node:test';
import assert from 'node:assert/strict';
import { createGuestWorkspace, guestApiRequest } from './guestWorkspace.js';

test('guest workspace seeds editable examples on the browser local day', () => {
  const now = new Date(2026, 9, 1, 12, 0, 0);
  const workspace = createGuestWorkspace(now);
  assert.equal(workspace.user.is_guest, true);
  assert.ok(workspace.events.length >= 3);
  assert.ok(workspace.events.every((event) => String(event.start_at).startsWith('2026-10-01T')));
  assert.ok(workspace.tasks.some((task) => task.start_at && String(task.start_at).startsWith('2026-10-01T')));
});



test('guest workspace keeps a current-day example visible even late at night', () => {
  const workspace = createGuestWorkspace(new Date(2026, 9, 1, 23, 50, 0));
  const allDay = workspace.events.find((event) => event.all_day);
  assert.ok(allDay);
  assert.ok(String(allDay.start_at).startsWith('2026-10-01T'));
  assert.equal(allDay.time_label, '全天');
});

test('guest workspace stays local, supports edits, and blocks AI/network-only actions', () => {
  const workspace = createGuestWorkspace(new Date(2026, 9, 1, 12, 0, 0));
  const task = workspace.tasks[0];
  const updated = guestApiRequest(workspace, `/tasks/${task.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ title: '改過的展示待辦', status: 'done' }),
  });
  assert.equal(updated.title, '改過的展示待辦');
  assert.equal(updated.status, 'done');
  assert.throws(
    () => guestApiRequest(workspace, '/interpret', { method: 'POST', body: '{}' }),
    (error) => error?.status === 403,
  );
});

test('guest event linking search is bounded instead of dumping every event', () => {
  const workspace = createGuestWorkspace(new Date(2026, 9, 1, 12, 0, 0));
  const result = guestApiRequest(workspace, '/events/search?limit=2');
  assert.equal(result.events.length, 2);
  assert.equal(result.meta.limit, 2);
});


test('guest event search understands local dates and weekday keywords', () => {
  const workspace = createGuestWorkspace(new Date(2026, 9, 1, 12, 0, 0));
  const byDate = guestApiRequest(workspace, '/events/search?q=10%E6%9C%881%E6%97%A5&limit=12');
  assert.ok(byDate.events.length >= 1);
  assert.ok(byDate.events.every((event) => String(event.start_at).startsWith('2026-10-01T')));
  const byWeekday = guestApiRequest(workspace, '/events/search?q=%E6%98%9F%E6%9C%9F%E5%9B%9B&limit=12');
  assert.ok(byWeekday.events.length >= 1);
});

test('guest identity is generic and never borrows the signed-in account name', () => {
  const workspace = createGuestWorkspace(new Date(2026, 9, 1, 12, 0, 0));
  assert.equal(workspace.user.name, '展示帳號');
  assert.equal(workspace.profile.name, '展示帳號');
});
