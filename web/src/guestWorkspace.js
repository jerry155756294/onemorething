export const GUEST_SESSION_KEY = 'omt:guest-workspace:v2';

function pad(value) {
  return String(value).padStart(2, '0');
}

function dateKey(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function isoAt(date, hour, minute) {
  const value = new Date(date.getFullYear(), date.getMonth(), date.getDate(), hour, minute, 0, 0);
  const offset = -value.getTimezoneOffset();
  const sign = offset >= 0 ? '+' : '-';
  const hours = pad(Math.floor(Math.abs(offset) / 60));
  const minutes = pad(Math.abs(offset) % 60);
  return `${dateKey(value)}T${pad(value.getHours())}:${pad(value.getMinutes())}:00${sign}${hours}:${minutes}`;
}

function event(date, id, title, startHour, startMinute, endHour, endMinute, location = '') {
  const start_at = isoAt(date, startHour, startMinute);
  const end_at = isoAt(date, endHour, endMinute);
  return {
    id,
    title,
    date_label: dateKey(date),
    time_label: `${pad(startHour)}:${pad(startMinute)}～${pad(endHour)}:${pad(endMinute)}`,
    start_at,
    end_at,
    all_day: false,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei',
    detail: '',
    location,
    source_id: 'guest-demo',
    source_type: 'manual',
    event_source: 'local',
    created_source: 'user_created',
    lifecycle_status: 'created',
    sync_status: 'local',
  };
}

function allDayEvent(date, id, title) {
  const day = dateKey(date);
  const next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1);
  return {
    id,
    title,
    date_label: day,
    time_label: '全天',
    start_at: `${day}T00:00:00`,
    end_at: `${dateKey(next)}T00:00:00`,
    all_day: true,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei',
    detail: '',
    location: '',
    source_id: 'guest-demo',
    source_type: 'manual',
    event_source: 'local',
    created_source: 'user_created',
    lifecycle_status: 'created',
    sync_status: 'local',
  };
}

function localWeekdayAliases(value) {
  const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  if (Number.isNaN(date.getTime())) return [];
  const zh = ['日', '一', '二', '三', '四', '五', '六'][date.getDay()];
  const en = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'][date.getDay()];
  return [`星期${zh}`, `週${zh}`, `周${zh}`, `禮拜${zh}`, en];
}

function eventSearchText(item, now = new Date()) {
  const raw = String(item.start_at || item.date_label || '').slice(0, 10);
  const parts = [item.title, item.location, item.detail, raw];
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (match) {
    const month = Number(match[2]);
    const day = Number(match[3]);
    parts.push(`${month}/${day}`, `${month}月${day}日`, `${month}月${day}號`, ...localWeekdayAliases(raw));
    const current = dateKey(now);
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    const afterTomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 2);
    if (raw === current) parts.push('今天', '今日');
    if (raw === dateKey(tomorrow)) parts.push('明天', '明日');
    if (raw === dateKey(afterTomorrow)) parts.push('後天');
  }
  return parts.filter(Boolean).join(' ').toLocaleLowerCase();
}

export function createGuestWorkspace(now = new Date()) {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = dateKey(today);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei';
  return {
    version: 2,
    base_date: day,
    user: { id: 'guest-local', name: '展示帳號', display_name: '展示帳號', email: null, avatar_url: null, is_guest: true },
    profile: { name: '展示帳號', email: null },
    lists: [
      { id: 'guest-list-school', name: '學習' },
      { id: 'guest-list-life', name: '生活' },
    ],
    tasks: [
      {
        id: 'guest-task-plan', title: '帶企劃書', due_label: '今天', due_iso: day,
        start_at: isoAt(today, 18, 30), end_at: null, all_day: false, timezone,
        status: 'open', source_id: 'manual', owner: 'student', list_id: 'guest-list-school', related_event_id: null,
      },
      {
        id: 'guest-task-notes', title: '整理討論筆記', due_label: '今天', due_iso: day,
        start_at: null, end_at: null, all_day: false, timezone,
        status: 'open', source_id: 'manual', owner: 'student', list_id: 'guest-list-school', related_event_id: null,
      },
      {
        id: 'guest-task-buy', title: '買牛奶', due_label: '', due_iso: null,
        start_at: isoAt(today, 20, 30), end_at: isoAt(today, 21, 0), all_day: false, timezone,
        status: 'open', source_id: 'manual', owner: 'student', list_id: 'guest-list-life', related_event_id: null,
      },
    ],
    events: [
      allDayEvent(today, 'guest-event-birthday', '朋友生日'),
      event(today, 'guest-event-group', '小組討論', 9, 30, 10, 20, '圖書館討論區'),
      event(today, 'guest-event-lunch', '和同學吃午餐', 12, 10, 13, 0, '學餐'),
      event(today, 'guest-event-club', '社團練習', 15, 30, 16, 40, '活動教室'),
      event(today, 'guest-event-review', '複習今天的筆記', 19, 30, 20, 30, '家裡'),
    ],
  };
}

export function guestSessionExists(storage = globalThis.sessionStorage) {
  try { return Boolean(storage?.getItem(GUEST_SESSION_KEY)); } catch { return false; }
}

export function loadGuestWorkspace(storage = globalThis.sessionStorage, now = new Date()) {
  try {
    const raw = storage?.getItem(GUEST_SESSION_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed?.version === 2 && parsed?.base_date === dateKey(now) && Array.isArray(parsed.tasks) && Array.isArray(parsed.events)) return parsed;
    }
  } catch {}
  const fresh = createGuestWorkspace(now);
  saveGuestWorkspace(fresh, storage);
  return fresh;
}

export function saveGuestWorkspace(workspace, storage = globalThis.sessionStorage) {
  try { storage?.setItem(GUEST_SESSION_KEY, JSON.stringify(workspace)); } catch {}
}

export function clearGuestWorkspace(storage = globalThis.sessionStorage) {
  try { storage?.removeItem(GUEST_SESSION_KEY); } catch {}
}

function dashboardPayload(workspace) {
  const openTasks = workspace.tasks.filter((task) => task.status !== 'done');
  return {
    profile: workspace.profile,
    stats: { open_tasks: openTasks.length, today_events: workspace.events.length, unread: 0, completion: workspace.tasks.length ? Math.round((workspace.tasks.length - openTasks.length) / workspace.tasks.length * 100) : 0 },
    notices: [],
    tasks: workspace.tasks,
    events: workspace.events,
    proposals: [],
  };
}

function parseBody(options) {
  if (!options?.body) return {};
  if (typeof options.body === 'string') {
    try { return JSON.parse(options.body); } catch { return {}; }
  }
  return options.body;
}

function updateEventLabels(item) {
  if (item.all_day) {
    item.date_label = String(item.start_at || item.date_label || '').slice(0, 10);
    item.time_label = '全天';
    return;
  }
  if (item.start_at) {
    item.date_label = String(item.start_at).slice(0, 10);
    const start = String(item.start_at).slice(11, 16);
    const end = item.end_at ? String(item.end_at).slice(11, 16) : '';
    item.time_label = end ? `${start}～${end}` : start;
  }
}

function calendarPage(workspace, url) {
  let events = [...workspace.events];
  const month = url.searchParams.get('month');
  const scope = url.searchParams.get('scope') || 'upcoming';
  const now = new Date();
  if (month) events = events.filter((item) => String(item.start_at || item.date_label || '').startsWith(month));
  if (!month && scope === 'upcoming') events = events.filter((item) => new Date(item.end_at || item.start_at || 0).getTime() >= now.getTime());
  const offset = Math.max(0, Number(url.searchParams.get('offset') || 0));
  const limit = Math.max(1, Number(url.searchParams.get('limit') || 100));
  const page = events.slice(offset, offset + limit);
  return { events: page, meta: { total_count: events.length, count: page.length, limit, offset, next_offset: offset + page.length < events.length ? offset + page.length : null, status: 'ok', sources: [] } };
}

export function guestApiRequest(workspace, path, options = {}) {
  const url = new URL(path, 'https://guest.invalid');
  const method = String(options.method || 'GET').toUpperCase();
  const body = parseBody(options);

  if (url.pathname === '/dashboard') return dashboardPayload(workspace);
  if (url.pathname === '/ingestion/policy') return { policy: { max_upload_bytes: 10 * 1024 * 1024, max_attachments_per_capture: 5, max_total_capture_bytes: 25 * 1024 * 1024, preview_char_limit: 2000 } };
  if (url.pathname === '/calendar/sources') return { sources: [] };
  if (url.pathname === '/calendar/auto-sync') return { status: 'skipped', reason: 'guest' };
  if (url.pathname === '/integrations') return { calendar: [], authentication: [], available: { authentication: [], calendar: [] }, guest: true };
  if (url.pathname === '/calendar/events') return calendarPage(workspace, url);
  if (url.pathname === '/events/search') {
    const q = String(url.searchParams.get('q') || '').trim().toLocaleLowerCase();
    const limit = Math.max(1, Math.min(30, Number(url.searchParams.get('limit') || 6)));
    const now = new Date();
    let events = workspace.events.filter((item) => !q || eventSearchText(item, now).includes(q));
    events = events.sort((a, b) => new Date(a.start_at || 0) - new Date(b.start_at || 0)).slice(0, limit);
    return { events, meta: { count: events.length, limit, query: q } };
  }

  if (url.pathname === '/lists') {
    if (method === 'GET') return { lists: workspace.lists };
    if (method === 'POST') {
      const list = { id: `guest-list-${Date.now()}`, name: String(body.name || '').trim() || '新清單' };
      workspace.lists.push(list);
      return { list };
    }
  }
  const listMatch = url.pathname.match(/^\/lists\/([^/]+)$/);
  if (listMatch && method === 'DELETE') {
    const id = decodeURIComponent(listMatch[1]);
    workspace.lists = workspace.lists.filter((item) => String(item.id) !== id);
    workspace.tasks = workspace.tasks.map((task) => String(task.list_id) === id ? { ...task, list_id: null } : task);
    return {};
  }

  if (url.pathname === '/tasks' && method === 'GET') {
    const listId = url.searchParams.get('list_id');
    return { tasks: listId ? workspace.tasks.filter((task) => String(task.list_id || '') === listId) : workspace.tasks };
  }
  const taskMatch = url.pathname.match(/^\/tasks\/([^/]+)$/);
  if (taskMatch) {
    const id = decodeURIComponent(taskMatch[1]);
    const index = workspace.tasks.findIndex((task) => String(task.id) === id);
    if (index < 0) throw Object.assign(new Error('Task not found'), { status: 404 });
    if (method === 'DELETE') { workspace.tasks.splice(index, 1); return {}; }
    if (method === 'PATCH') {
      workspace.tasks[index] = { ...workspace.tasks[index], ...body };
      return workspace.tasks[index];
    }
  }

  const eventMatch = url.pathname.match(/^\/events\/([^/]+)$/);
  if (eventMatch) {
    const id = decodeURIComponent(eventMatch[1]);
    const index = workspace.events.findIndex((eventItem) => String(eventItem.id) === id);
    if (index < 0) throw Object.assign(new Error('Event not found'), { status: 404 });
    if (method === 'DELETE') { workspace.events.splice(index, 1); return {}; }
    if (method === 'PATCH') {
      workspace.events[index] = { ...workspace.events[index], ...body };
      updateEventLabels(workspace.events[index]);
      return workspace.events[index];
    }
  }

  throw Object.assign(new Error('This action is not available in the guest workspace'), { status: 403, payload: { detail: '展示模式不連接外部服務' } });
}
