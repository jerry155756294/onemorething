export function calendarDayEventCount(dateKey, eventCounts = {}, eventLabels = {}) {
  if (Object.prototype.hasOwnProperty.call(eventCounts, dateKey)) {
    const count = Number(eventCounts[dateKey]);
    return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
  }
  return eventLabels[dateKey] ? 1 : 0;
}

export function calendarEventCountMark(count) {
  if (count <= 1) return '';
  return count > 9 ? '+9' : `+${count}`;
}

export function calendarAgendaRangeLabel(hasSelectedDate) {
  return hasSelectedDate ? '當日完整行程' : '選擇日期查看行程';
}

export function shouldShowCalendarAgendaEmpty(eventCount, loading, error) {
  return eventCount === 0 && !loading && !error;
}
