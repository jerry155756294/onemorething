function rawTaskDue(task) {
  if (!task) return '';
  const friendlyLabel = String(task.due_label || '').trim();
  if (friendlyLabel && !/^\d{4}-\d{2}-\d{2}(?:[T\s]|$)/.test(friendlyLabel)) return friendlyLabel;
  return String(task.due_iso || task.due_at || task.due_date || friendlyLabel || '').trim();
}

function numericDateParts(value) {
  const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})(?:[T\s]|$)/);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  return { year, month, day };
}

export function taskDueCompactLabel(task) {
  const raw = rawTaskDue(task);
  if (!raw) return '';
  const parts = numericDateParts(raw);
  return parts ? `${parts.month}/${parts.day}` : raw;
}

export function taskDueLongLabel(task) {
  const raw = rawTaskDue(task);
  if (!raw) return '';
  const parts = numericDateParts(raw);
  return parts ? `${parts.month} 月 ${parts.day} 日` : raw;
}
