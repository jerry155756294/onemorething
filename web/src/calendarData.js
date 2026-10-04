export function monthStart(value = new Date()) {
  const date = value instanceof Date ? new Date(value) : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

export function shiftMonth(value, offset) {
  const first = monthStart(value);
  if (!first || !Number.isInteger(offset)) return null;
  return new Date(first.getFullYear(), first.getMonth() + offset, 1);
}

export function monthKey(value) {
  const first = monthStart(value);
  return first
    ? `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, '0')}`
    : '';
}

export function createLatestRequestGate() {
  let latest = 0;
  return {
    begin: () => ++latest,
    isLatest: (requestId) => requestId === latest,
  };
}

function localEventSeriesId(eventId) {
  const id = String(eventId ?? '');
  const match = id.match(/^(event-[a-f0-9]+)(?:@\d{4}-\d{2}-\d{2})?$/);
  return match ? match[1] : id;
}

export function withoutDeletedLocalEventSeries(events, eventId) {
  if (!Array.isArray(events)) return [];
  const deletedSeriesId = localEventSeriesId(eventId);
  return events.filter((event) => localEventSeriesId(event?.id) !== deletedSeriesId);
}

export async function fetchCalendarPages(fetchPage) {
  const events = [];
  let offset = 0;
  let meta = null;
  while (true) {
    const page = await fetchPage(offset);
    if (!page || !Array.isArray(page.events)) return null;
    events.push(...page.events);
    meta = page.meta || meta;
    const totalCount = page.meta?.total_count;
    const nextOffset = page.meta?.next_offset;
    if (!Number.isInteger(totalCount) || totalCount < 0 || events.length > totalCount) {
      throw new Error('行事曆分頁回應缺少有效的總筆數。');
    }
    if (nextOffset === null) {
      if (events.length < totalCount) throw new Error('行事曆分頁未完整，請重試。');
      break;
    }
    if (!Number.isInteger(nextOffset) || nextOffset <= offset || nextOffset !== offset + page.events.length || events.length >= totalCount) {
      throw new Error('行事曆分頁位置無效，請重試。');
    }
    offset = nextOffset;
  }
  return { events, meta };
}

function checklistText(item) {
  return String(typeof item === 'string' ? item : item?.text || item?.title || '').trim();
}

export function preserveChecklistItems(lines, existingItems = []) {
  const existing = Array.isArray(existingItems) ? existingItems : [];
  const normalizedLines = lines
    .map((line) => String(line || '').trim())
    .filter(Boolean);
  const used = new Set();
  const matches = new Array(normalizedLines.length).fill(-1);

  normalizedLines.forEach((text, lineIndex) => {
    const matchIndex = existing.findIndex((item, candidate) => !used.has(candidate) && checklistText(item) === text);
    if (matchIndex >= 0) {
      matches[lineIndex] = matchIndex;
      used.add(matchIndex);
    }
  });

  if (normalizedLines.length === existing.length) {
    normalizedLines.forEach((_text, lineIndex) => {
      if (matches[lineIndex] >= 0 || used.has(lineIndex)) return;
      matches[lineIndex] = lineIndex;
      used.add(lineIndex);
    });
  }

  return normalizedLines.map((text, lineIndex) => {
    const matchIndex = matches[lineIndex];
    if (matchIndex < 0) return { text, done: false };
    const previous = existing[matchIndex];
    return typeof previous === 'object' && previous !== null
      ? { ...previous, text }
      : { text, done: false };
  });
}
