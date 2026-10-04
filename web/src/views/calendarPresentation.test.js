import test from 'node:test';
import assert from 'node:assert/strict';
import { calendarAgendaRangeLabel, calendarDayEventCount, calendarEventCountMark, shouldShowCalendarAgendaEmpty } from './calendarPresentation.js';

test('calendar day counts distinguish empty, one, and multiple event days', () => {
  const counts = { empty: 0, one: 1, many: 4 };
  assert.equal(calendarDayEventCount('empty', counts), 0);
  assert.equal(calendarDayEventCount('one', counts), 1);
  assert.equal(calendarDayEventCount('many', counts), 4);
});

test('calendar day counts fall back to presence when the count contract is not supplied', () => {
  assert.equal(calendarDayEventCount('busy', {}, { busy: 'Class, club' }), 1);
  assert.equal(calendarDayEventCount('free', {}, {}), 0);
});

test('calendar markers show a compact count and cap crowded days', () => {
  assert.equal(calendarEventCountMark(1), '');
  assert.equal(calendarEventCountMark(2), '+2');
  assert.equal(calendarEventCountMark(9), '+9');
  assert.equal(calendarEventCountMark(10), '+9');
});

test('agenda copy describes date selection without exposing implementation limits', () => {
  assert.equal(calendarAgendaRangeLabel(false), '選擇日期查看行程');
  assert.equal(calendarAgendaRangeLabel(true), '當日完整行程');
});

test('agenda empty state appears only after a successful empty load', () => {
  assert.equal(shouldShowCalendarAgendaEmpty(0, false, ''), true);
  assert.equal(shouldShowCalendarAgendaEmpty(0, true, ''), false);
  assert.equal(shouldShowCalendarAgendaEmpty(0, false, '載入失敗'), false);
  assert.equal(shouldShowCalendarAgendaEmpty(1, false, ''), false);
});
