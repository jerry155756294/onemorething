import assert from 'node:assert/strict';
import test from 'node:test';
import { taskDueCompactLabel, taskDueLongLabel } from './taskPresentation.js';

test('task due presentation never leaks raw ISO timestamps', () => {
  const task = { due_iso: '2026-09-28T08:00:00+08:00' };
  assert.equal(taskDueCompactLabel(task), '9/28');
  assert.equal(taskDueLongLabel(task), '9 月 28 日');
});

test('date-only due values are formatted without timezone rollover', () => {
  const task = { due_date: '2026-01-02' };
  assert.equal(taskDueCompactLabel(task), '1/2');
  assert.equal(taskDueLongLabel(task), '1 月 2 日');
});

test('friendly due labels stay intact', () => {
  assert.equal(taskDueCompactLabel({ due_label: '明天' }), '明天');
  assert.equal(taskDueLongLabel({ due_label: '今天' }), '今天');
});

test('missing due stays empty', () => {
  assert.equal(taskDueCompactLabel({}), '');
  assert.equal(taskDueLongLabel(null), '');
});
