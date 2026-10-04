import { compareEventsByStart, dateKey, eventStartTimestamp, isEventActiveOrUpcoming, normalizeTask } from './dashboardData.js';

export function canPresentCollectionConclusion({ loaded, loading, error }) {
  return Boolean(loaded) && !loading && !error;
}

export function selectNextOverviewEvent(events, now = new Date()) {
  const current = now.getTime();
  return [...events]
    .filter((event) => isEventActiveOrUpcoming(event, now))
    .sort((first, second) => eventStartTimestamp(first) - eventStartTimestamp(second))
    .find((event) => eventStartTimestamp(event) >= current) || null;
}

export function selectUpcomingOverviewEvents(events, now = new Date(), limit = 4) {
  const upcoming = [...events]
    .filter((event) => isEventActiveOrUpcoming(event, now))
    .sort(compareEventsByStart);
  const todayKey = dateKey(now);
  const focusDateKey = upcoming.some((event) => event.dateKey === todayKey)
    ? todayKey
    : upcoming[0]?.dateKey;
  return upcoming
    .filter((event) => event.dateKey === focusDateKey)
    .slice(0, Math.max(0, limit));
}

export function selectCalendarAgendaEvents(events, selectedDateKey = '') {
  const ordered = [...events].sort(compareEventsByStart);
  if (selectedDateKey) return ordered.filter((event) => event.dateKey === selectedDateKey);
  return [];
}

export function countCalendarEventsByDate(events) {
  return events.reduce((counts, event) => {
    if (event.dateKey) counts[event.dateKey] = (counts[event.dateKey] || 0) + 1;
    return counts;
  }, {});
}

export function selectOverviewTasks(tasks, now = new Date()) {
  const remaining = tasks
    .map((task) => normalizeTask(task, now))
    .filter((task) => task.status !== 'done');
  return {
    overdue: remaining.filter((task) => task.dueStatus === 'overdue'),
    today: remaining.filter((task) => task.dueStatus === 'today'),
  };
}

export function countRemainingEventsToday(events, now = new Date()) {
  const today = dateKey(now);
  return events.filter((event) => event.dateKey === today && isEventActiveOrUpcoming(event, now)).length;
}
