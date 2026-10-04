<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, toRaw, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { calendarCells, dateKey, monthLabel, normalizeEvent, normalizeTask, parseDateLabel } from './dashboardData';
import { createLatestRequestGate, fetchCalendarPages, monthKey, monthStart, shiftMonth, withoutDeletedLocalEventSeries } from './calendarData.js';
import { WORKSPACE_PATHS, consumeWorkspaceReloadRoute, logoutAfterServerConfirmation, rememberWorkspaceRoute } from './authWorkflow.js';
import { sourceDetailsFromPayload } from './inboxData.js';
import { captureRequestFailureMessage } from './captureWorkflow.js';
import ProposalReviewDialog from './components/ProposalReviewDialog.vue';
import WorkspaceShell from './components/WorkspaceShell.vue';
import PublicEntry from './views/PublicEntry.vue';
import { acceptedProposals, confirmableProposals, isPreviewableProposal, buildApplyResults, captureDraftAfterInterpret } from './proposalWorkflow.js';
import { clearGuestWorkspace, createGuestWorkspace, guestApiRequest, guestSessionExists, loadGuestWorkspace, saveGuestWorkspace } from './guestWorkspace.js';

import { canPresentCollectionConclusion, countCalendarEventsByDate, countRemainingEventsToday, selectCalendarAgendaEvents, selectNextOverviewEvent, selectOverviewTasks, selectUpcomingOverviewEvents } from './overviewData.js';

const API = import.meta.env.VITE_API_URL || '/api';
const authBootstrap = globalThis.__OMT_AUTH_BOOTSTRAP__ || null;
const guestMode = ref(guestSessionExists(window.sessionStorage));
const guestWorkspace = ref(guestMode.value ? loadGuestWorkspace(window.sessionStorage, new Date()) : null);
const signedIn = ref(Boolean(guestMode.value && guestWorkspace.value) || (authBootstrap?.status === 'signed-in' && Boolean(authBootstrap?.user)));
const currentUser = ref(guestMode.value ? (guestWorkspace.value?.user || null) : (authBootstrap?.user || null));
const authModal = ref(false);
const proposalModal = ref(false);
const proposalReturnToOverview = ref(false);
const authStatus = ref('正在檢查可用的登入方式…');
const authNotice = ref(null);
const authSessionState = ref(guestMode.value ? 'signed-in' : (['signed-in', 'signed-out'].includes(authBootstrap?.status) ? authBootstrap.status : 'checking'));
const authProvidersLoading = ref(false);
const authLoading = ref(false);
const signOutBusy = ref(false);
const signOutError = ref('');
const sourceLoadError = ref('');
const sourceLoadErrorId = ref('');
const sourceLoadingId = ref('');
const activeFilter = ref('all');
const mobileNavOpen = ref(false);
const fileStatus = ref('');
const firstAuthButtonRef = ref(null);
const googleAuthButtonRef = ref(null);
const guestAuthButtonRef = ref(null);
const authCloseButtonRef = ref(null);
const authFeedbackRef = ref(null);
const lastFocusedElement = ref(null);
let authScrollRestore = null;
let authLoadGeneration = 0;
const preview = ref(null);
const newCaptureSourceId = ref('');
const cancelingCaptureReview = ref(false);
const captureStatus = ref('idle');
const requestError = ref('');
const requestErrorStatus = ref(0);
const requestErrorCode = ref('');
const noticeBusy = ref(false);
const captureError = ref('');
const proposalError = ref('');
const proposalApplyResults = ref([]);
const eventEditorOpen = ref(false);
const selectedEvent = ref(null);
const editingDraft = ref(null);
// Compatibility alias for the route prop while the draft remains a separate object.
const eventEditor = editingDraft;

function cloneEvent(value) {
  const seen = new WeakMap();
  const unwrap = (input) => {
    const raw = toRaw(input);
    if (raw === null || typeof raw !== 'object') return raw;
    if (raw instanceof Date) return new Date(raw.getTime());
    if (seen.has(raw)) return seen.get(raw);
    const output = Array.isArray(raw) ? [] : {};
    seen.set(raw, output);
    Object.entries(raw).forEach(([key, child]) => {
      output[key] = unwrap(child);
    });
    return output;
  };
  return structuredClone(unwrap(value));
}
const eventDeleteDialogOpen = ref(false);
const eventDeleteError = ref('');
const eventDeleteReturnFocus = ref(null);
const eventDeleteSucceeded = ref(false);
const eventBusy = ref(false);
const eventError = ref('');
const eventFeedback = ref('');
const taskBusy = ref(false);
const taskDeleteDialogOpen = ref(false);
const taskDeleteError = ref('');
const taskDeleteReturnFocus = ref(null);
const taskPendingDelete = ref(null);
const taskError = ref('');
const taskEventSearchResults = ref([]);
const taskEventSearchLoading = ref(false);
const taskEventSearchQuery = ref('');
const lists = ref([]);
const listsLoading = ref(false);
const listsLoaded = ref(false);
const selectedListId = ref('');
const listBusy = ref(false);
const listError = ref('');
const listPendingDelete = ref(null);
const listDeleteDialogOpen = ref(false);
const listDeleteError = ref('');
const listDeleteReturnFocus = ref(null);
const listsLoadError = ref('');
const listTaskRecords = ref([]);
const listTasksLoaded = ref(false);
const taskListLoading = ref(false);
const taskListError = ref('');
const taskListRequests = createLatestRequestGate();
const inboxProposals = ref([]);
const inboxProposalsLoaded = ref(false);
const inboxSource = ref(null);
const inboxSourceRequests = createLatestRequestGate();
const inboxLoading = ref(false);
const inboxError = ref('');
const calendarModal = ref(false);
const calendarBusy = ref(false);
const calendarSourcesLoading = ref(false);
const calendarStatus = ref('');
const calendarLoading = ref(false);
const calendarError = ref('');
const calendarSources = ref([]);
const calendarSyncingId = ref('');
const calendarFlow = ref(null);
const calendarPollTimer = ref(null);
const calendarServerRef = ref(null);
const calendarUrl = ref('');
const CALENDAR_AUTO_SYNC_INTERVAL_MS = 5 * 60 * 1000;
let automaticCalendarSyncTimer = null;
let lastAutomaticCalendarSyncAt = 0;
let automaticCalendarRefreshTimer = null;
const integrations = ref(null);
const integrationsLoading = ref(false);
const integrationsError = ref('');
const integrationActionMessage = ref('');
const integrationBusy = ref('');
const accountBusy = ref('');
const accountMessage = ref('');
const accountError = ref('');
const accountErrorAction = ref('');
const dashboardLoading = ref(false);
const dashboardError = ref('');
const dashboardLoaded = ref(false);
const dashboardBootstrapLoading = computed(() => dashboardLoading.value && !dashboardLoaded.value);
const inboxPageLoading = computed(() => dashboardBootstrapLoading.value || inboxLoading.value || (!inboxProposalsLoaded.value && !inboxError.value));
const inboxCanPresentConclusion = computed(() => canPresentCollectionConclusion({ loaded: inboxProposalsLoaded.value, loading: inboxPageLoading.value, error: inboxError.value }));
const listDataUnresolved = computed(() => !canPresentCollectionConclusion({
  loaded: listsLoaded.value && listTasksLoaded.value,
  loading: dashboardBootstrapLoading.value || listsLoading.value || taskListLoading.value,
  error: listsLoadError.value || taskListError.value,
}));
const overviewCalendarError = ref('');
const overviewCalendarEvents = ref([]);
const calendarEventsByMonth = reactive({});
const calendarRequests = createLatestRequestGate();
const externalCalendarEvents = computed(() => {
  const unique = new Map();
  Object.values(calendarEventsByMonth).flat().forEach((event) => unique.set(event.id, event));
  return [...unique.values()];
});
const runtimeNow = ref(new Date());
let runtimeClock = null;
const route = useRoute();
const router = useRouter();

const state = reactive({ profile: { name: '我的空間', email: null }, stats: { open_tasks: 0, today_events: 0, unread: 0, completion: 0 }, notices: [], tasks: [], events: [], proposals: [] });
const form = reactive({ title: '', body: '', source_id: null, attachments: [] });
const ingestionPolicy = reactive({ max_upload_bytes: 10 * 1024 * 1024, max_attachments_per_capture: 5, max_total_capture_bytes: 25 * 1024 * 1024, preview_char_limit: 2000 });
const providers = reactive({ github: false, google: false });
const canSubmitNotice = computed(() => Boolean(form.body.trim() || form.attachments.length));
const captureBusy = computed(() => captureStatus.value === 'processing');
const openTaskCount = computed(() => state.tasks.filter((task) => task.status !== 'done').length);
const normalizedOverviewEvents = computed(() => {
  const unique = new Map();
  [...state.events, ...externalCalendarEvents.value, ...overviewCalendarEvents.value].forEach((event, index) => unique.set(event.id || `event-${index}`, event));
  return [...unique.values()].map((event) => normalizeEvent(event, runtimeNow.value));
});
const normalizedTasks = computed(() => state.tasks.map((task) => normalizeTask(task, runtimeNow.value)));
const normalizedListTasks = computed(() => listTaskRecords.value.map((task) => normalizeTask(task, runtimeNow.value)));
const visibleTasks = computed(() => normalizedListTasks.value.filter((task) => activeFilter.value === 'all' || (activeFilter.value === 'done' && task.status === 'done') || (activeFilter.value === 'today' && task.status !== 'done' && task.dueStatus === 'today')));
const currentMonth = ref(monthStart(new Date()));
const selectedCalendarDateKey = ref(dateKey(new Date()));
const calendarDays = computed(() => calendarCells(currentMonth.value));
const calendarMonthLabel = computed(() => monthLabel(currentMonth.value));
const todayEventCount = computed(() => countRemainingEventsToday(normalizedOverviewEvents.value, runtimeNow.value));
const calendarEventCounts = computed(() => countCalendarEventsByDate(normalizedOverviewEvents.value));
const overviewTasks = computed(() => selectOverviewTasks(state.tasks, runtimeNow.value));
const overdueTasks = computed(() => overviewTasks.value.overdue);
const todayTasks = computed(() => overviewTasks.value.today);
const nextEvent = computed(() => selectNextOverviewEvent(normalizedOverviewEvents.value, runtimeNow.value));
const upcomingOverviewEvents = computed(() => selectUpcomingOverviewEvents(normalizedOverviewEvents.value, runtimeNow.value));
const calendarAgendaEvents = computed(() => selectCalendarAgendaEvents(normalizedOverviewEvents.value, selectedCalendarDateKey.value, runtimeNow.value));
const calendarEventLabels = computed(() => normalizedOverviewEvents.value.reduce((result, event) => {
  if (event.dateKey) result[event.dateKey] = result[event.dateKey] ? `${result[event.dateKey]}、${event.title}` : event.title;
  return result;
}, {}));

class ApiError extends Error {
  constructor(status, payload) {
    super(payload?.message || payload?.detail || `API error ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function requestJson(path, options = {}) {
  if (guestMode.value && guestWorkspace.value) {
    try {
      const result = guestApiRequest(guestWorkspace.value, path, options);
      saveGuestWorkspace(guestWorkspace.value, window.sessionStorage);
      return cloneEvent(result);
    } catch (error) {
      throw new ApiError(error?.status || 403, error?.payload || { detail: error?.message || '展示模式不支援這項操作' });
    }
  }
  const response = await fetch(`${API}${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }, ...options });
  if (response.status === 204) return {};
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

const workspacePaths = new Set(WORKSPACE_PATHS);
function syncAuthRoute() {
  // Never destroy a deep link while auth is unresolved. Only a definitive
  // signed-out state may redirect a protected route back to the entry page.
  if (authSessionState.value === 'checking' || authSessionState.value === 'unknown') return;
  if (authSessionState.value === 'signed-in' && signedIn.value && route.path === '/') {
    let reloadRoute = '';
    try { reloadRoute = consumeWorkspaceReloadRoute(window.sessionStorage); } catch {}
    return router.replace(reloadRoute || '/today');
  }
  if (authSessionState.value === 'signed-out' && workspacePaths.has(route.path)) router.replace('/');
}
function clearAuthState() {
  if (guestMode.value) {
    clearGuestWorkspace(window.sessionStorage);
    guestWorkspace.value = null;
    guestMode.value = false;
  }
  currentUser.value = null;
  signedIn.value = false;
  authSessionState.value = 'signed-out';
  syncAuthRoute();
}
async function request(path, options = {}) {
  try {
    requestError.value = '';
    requestErrorStatus.value = 0;
    requestErrorCode.value = '';
    return await requestJson(path, options);
  } catch (error) {
    requestError.value = error instanceof ApiError ? error.message : '目前無法連線到 backend。';
    requestErrorStatus.value = error instanceof ApiError ? error.status : 0;
    requestErrorCode.value = error instanceof ApiError ? String(error.payload?.error?.code || '') : '';
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    return null;
  }
}

function normalizeProviders(payload) {
  const providerData = payload?.providers ?? payload ?? {};
  const read = (name) => {
    if (Array.isArray(providerData)) {
      const item = providerData.find((entry) => entry?.id === name || entry?.name === name);
      return Boolean(item?.enabled ?? item?.available ?? item);
    }
    const value = providerData[name];
    return Boolean(value?.enabled ?? value?.available ?? value);
  };
  return { github: read('github'), google: read('google') };
}
const enabledProviderCount = computed(() => Object.values(providers).filter(Boolean).length);
const authProviderHint = computed(() => authProvidersLoading.value ? '正在從服務端確認可用的登入方式…' : '');
const authCallbackMessages = { access_denied: '你取消了登入；沒有任何資料被變更。', consent_denied: '你取消了授權；可以重新選擇登入方式。', provider_not_configured: '這個登入方式尚未在服務端設定完成。', invalid_state: '登入驗證已過期，請重新開始登入。', state_mismatch: '登入驗證未通過，請重新開始登入。', callback_failed: '登入服務回傳失敗，請稍後再試。', session_unavailable: '登入成功但工作階段尚未建立，請重新登入。' };

function readAuthCallback() {
  const url = new URL(window.location.href);
  const code = url.searchParams.get('auth_error') || url.searchParams.get('error');
  if (!code && !url.searchParams.has('auth')) return null;
  const description = url.searchParams.get('error_description');
  ['auth', 'auth_error', 'error', 'error_description'].forEach((key) => url.searchParams.delete(key));
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
  return code ? { kind: 'error', code, description } : { kind: 'success' };
}
function callbackMessage(callback) { return callback?.kind === 'error' ? authCallbackMessages[callback.code] || '登入沒有完成，請再試一次。' : null; }

async function loadAuth() {
  const generation = ++authLoadGeneration;
  if (guestMode.value && guestWorkspace.value) {
    currentUser.value = guestWorkspace.value.user;
    signedIn.value = true;
    authSessionState.value = 'signed-in';
    dashboardLoading.value = true;
    dashboardError.value = '';
    requestError.value = '';
    await loadIngestionPolicy();
    await loadDashboard();
    syncAuthRoute();
    return;
  }
  try {
    const bootstrap = globalThis.__OMT_AUTH_BOOTSTRAP__;
    const hasDefinitiveBootstrap = ['signed-in', 'signed-out'].includes(bootstrap?.status);
    const payload = hasDefinitiveBootstrap ? { user: bootstrap?.user || null } : await requestJson('/auth/me');
    if (generation !== authLoadGeneration || guestMode.value) return;
    globalThis.__OMT_AUTH_BOOTSTRAP__ = null;
    currentUser.value = payload?.user || null;
    signedIn.value = Boolean(currentUser.value);
    authSessionState.value = signedIn.value ? 'signed-in' : 'signed-out';
    if (signedIn.value) {
      dashboardLoading.value = true;
      syncAuthRoute();
      await loadIngestionPolicy();
      await loadDashboard();
      await loadCalendarSources();
    }
  } catch (error) {
    if (generation !== authLoadGeneration || guestMode.value) return;
    globalThis.__OMT_AUTH_BOOTSTRAP__ = null;
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    else authSessionState.value = 'unknown';
  }
  const callback = readAuthCallback();
  if (callback?.kind === 'error') {
    await openAuthModal();
    authNotice.value = { kind: 'error', message: callbackMessage(callback), retryable: true };
    authStatus.value = '';
    await nextTick();
    authFeedbackRef.value?.focus();
  }
  syncAuthRoute();
}

async function loadIngestionPolicy() {
  try {
    const result = await requestJson('/ingestion/policy');
    Object.assign(ingestionPolicy, result?.policy || {});
  } catch {
    // Safe defaults keep Capture usable when the hint endpoint is unavailable.
  }
}

async function loadDashboard() {
  dashboardLoading.value = true;
  dashboardError.value = '';
  overviewCalendarError.value = '';
  const data = await request('/dashboard?include_calendar=false');
  if (!data) { dashboardError.value = '目前無法載入工作區，請稍後再試。'; dashboardLoading.value = false; return false; }
  Object.assign(state, { ...data, notices: Array.isArray(data.notices) ? data.notices : [], tasks: Array.isArray(data.tasks) ? data.tasks : [], events: Array.isArray(data.events) ? data.events : [], proposals: Array.isArray(data.proposals) ? data.proposals : [], stats: { ...state.stats, ...(data.stats || {}) } });
  await loadCalendarEvents(currentMonth.value);
  await loadOverviewCalendarEvents();
  dashboardLoaded.value = true;
  dashboardLoading.value = false;
  return true;
}

async function loadLists() {
  listsLoading.value = true;
  listsLoadError.value = '';
  const result = await request('/lists');
  if (result) {
    lists.value = Array.isArray(result.lists) ? result.lists : [];
    listsLoaded.value = true;
    if (selectedListId.value && !lists.value.some((item) => item.id === selectedListId.value)) selectedListId.value = '';
  } else {
    listsLoadError.value = requestError.value || '無法載入個人清單，請稍後重試。';
  }
  listsLoading.value = false;
  return Boolean(result);
}

async function loadListTasks(listId = selectedListId.value) {
  const requestId = taskListRequests.begin();
  taskListLoading.value = true;
  listTasksLoaded.value = false;
  listTaskRecords.value = [];
  const query = listId ? `?list_id=${encodeURIComponent(listId)}` : '';
  taskListError.value = '';
  const result = await request(`/tasks${query}`);
  if (!taskListRequests.isLatest(requestId)) return;
  if (!result) {
    taskListError.value = requestError.value || '無法載入待辦，請稍後重試。';
    taskListLoading.value = false;
    return false;
  }
  listTaskRecords.value = Array.isArray(result.tasks) ? result.tasks : [];
  listTasksLoaded.value = true;
  taskListLoading.value = false;
  return true;
}

async function retryListData() {
  if (!listsLoaded.value && !(await loadLists())) return;
  await loadListTasks(selectedListId.value);
}

async function selectList(listId) {
  selectedListId.value = listId || '';
  await loadListTasks(selectedListId.value);
}

async function createList(name) {
  if (!String(name || '').trim() || listBusy.value) return;
  listBusy.value = true;
  listError.value = '';
  const result = await request('/lists', { method: 'POST', body: JSON.stringify({ name: String(name).trim() }) });
  listBusy.value = false;
  if (result?.list) {
    lists.value = [...lists.value, result.list];
    listsLoaded.value = true;
    selectedListId.value = result.list.id;
    await loadListTasks(result.list.id);
  } else {
    listError.value = requestError.value || '清單尚未建立，請稍後重試。';
  }
}

function requestListDeleteConfirmation(list) {
  if (!list?.id || listBusy.value) return;
  listPendingDelete.value = { id: list.id, name: list.name };
  listDeleteReturnFocus.value = document.activeElement;
  listDeleteError.value = '';
  listDeleteDialogOpen.value = true;
}

function restoreConfirmationFocus(focusTargetRef) {
  const focusTarget = focusTargetRef.value;
  if (!focusTarget?.isConnected) return;
  nextTick(() => {
    if (focusTarget.isConnected) focusTarget.focus?.();
  });
}

function prepareConfirmationDialog(event) {
  const dialog = event.currentTarget;
  if (!dialog) return;
  dialog.isAtScrollTop = true;
  dialog.isAtScrollBottom = true;
}

function syncConfirmationDialog(event) {
  const dialog = event.currentTarget;
  const scroller = dialog?.shadowRoot?.querySelector('.scroller');
  if (!dialog || !scroller) return;
  const maxScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  dialog.isAtScrollTop = scroller.scrollTop <= 1;
  dialog.isAtScrollBottom = scroller.scrollTop >= maxScrollTop - 1;
  dialog.requestUpdate?.();
}

function closeListDeleteConfirmation() {
  if (listBusy.value) return;
  listDeleteDialogOpen.value = false;
  listPendingDelete.value = null;
  restoreConfirmationFocus(listDeleteReturnFocus);
}

async function deleteList() {
  const list = listPendingDelete.value;
  if (!list?.id || listBusy.value) return;
  listBusy.value = true;
  listDeleteError.value = '';
  const result = await request(`/lists/${encodeURIComponent(list.id)}`, { method: 'DELETE' });
  if (!result) {
    listBusy.value = false;
    listDeleteError.value = requestError.value || '清單尚未刪除，請稍後重試。';
    return;
  }
  lists.value = lists.value.filter((item) => item.id !== list.id);
  state.tasks = state.tasks.map((task) => task.list_id === list.id ? { ...task, list_id: null } : task);
  listsLoaded.value = true;
  if (selectedListId.value === list.id) selectedListId.value = '';
  listPendingDelete.value = null;
  listDeleteDialogOpen.value = false;
  const refreshed = await loadListTasks(selectedListId.value);
  listBusy.value = false;
  listDeleteError.value = refreshed ? '' : (taskListError.value || '清單已刪除，但目前無法重新載入待辦。');
}

async function assignTaskList(payload = {}) {
  const { task, list_id: listId } = payload;
  const target = task?.raw || task;
  if (!target?.id || taskBusy.value) return;
  taskBusy.value = true;
  taskError.value = '';
  const result = await request(`/tasks/${target.id}`, { method: 'PATCH', body: JSON.stringify({ list_id: listId || null }) });
  taskBusy.value = false;
  if (!result) {
    taskError.value = requestError.value || '清單變更尚未儲存，請重試。';
    return;
  }
  state.tasks = state.tasks.map((item) => item.id === result.id ? result : item);
  await loadListTasks(selectedListId.value);
}

async function linkTaskEvent(payload = {}) {
  const { task, event_id: eventId } = payload;
  const target = task?.raw || task;
  if (!target?.id || taskBusy.value) return;
  taskBusy.value = true;
  taskError.value = '';
  const result = await request(`/tasks/${target.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ related_event_id: eventId || null }),
  });
  taskBusy.value = false;
  if (!result) {
    taskError.value = requestError.value || '行程連結尚未儲存，請重試。';
    return;
  }
  state.tasks = state.tasks.map((item) => item.id === result.id ? result : item);
  listTaskRecords.value = listTaskRecords.value.map((item) => item.id === result.id ? result : item);
}

async function updateTaskSchedule(payload = {}) {
  const { task, schedule = {} } = payload;
  const target = task?.raw || task;
  if (!target?.id || taskBusy.value) return;
  taskBusy.value = true;
  taskError.value = '';
  const result = await request(`/tasks/${target.id}`, { method: 'PATCH', body: JSON.stringify(schedule) });
  taskBusy.value = false;
  if (!result) {
    taskError.value = requestError.value || '待辦時間尚未儲存，請重試。';
    return;
  }
  state.tasks = state.tasks.map((item) => item.id === result.id ? result : item);
  listTaskRecords.value = listTaskRecords.value.map((item) => item.id === result.id ? result : item);
}

async function searchTaskEvents(payload = {}) {
  const query = String(payload?.query || '').trim();
  const task = payload?.task?.raw || payload?.task || {};
  const around = String(task.start_at || task.due_iso || '').slice(0, 10);
  taskEventSearchLoading.value = true;
  taskEventSearchQuery.value = query;
  const params = new URLSearchParams({ limit: query ? '12' : '6', today: dateKey(runtimeNow.value) });
  if (query) params.set('q', query);
  if (around) params.set('around', around);
  const result = await request(`/events/search?${params.toString()}`);
  taskEventSearchLoading.value = false;
  if (query !== taskEventSearchQuery.value) return;
  taskEventSearchResults.value = Array.isArray(result?.events) ? result.events.map((event) => normalizeEvent(event, runtimeNow.value)) : [];
}

async function loadInboxProposals() {
  inboxLoading.value = true;
  inboxError.value = '';
  const result = await request('/proposals');
  if (result) {
    inboxProposals.value = Array.isArray(result.proposals) ? result.proposals : [];
    inboxProposalsLoaded.value = true;
  }
  else inboxError.value = requestError.value || '無法載入收件匣，請稍後重試。';
  inboxLoading.value = false;
}

async function loadInboxSource(sourceId) {
  if (!sourceId || sourceId === 'manual') return;
  const requestId = inboxSourceRequests.begin();
  inboxSource.value = null;
  sourceLoadError.value = '';
  sourceLoadErrorId.value = '';
  sourceLoadingId.value = String(sourceId);
  const result = await request(`/notices/${encodeURIComponent(sourceId)}`);
  if (!inboxSourceRequests.isLatest(requestId)) return;
  sourceLoadingId.value = '';
  const source = sourceDetailsFromPayload(result, sourceId);
  if (!source) {
    inboxSource.value = null;
    sourceLoadErrorId.value = String(sourceId);
    sourceLoadError.value = requestError.value || '無法載入原始資訊，請稍後重試。';
    return;
  }
  inboxSource.value = source;
}

async function loadOverviewCalendarEvents() {
  try {
    const result = await fetchCalendarPages((offset) => request(`/calendar/events?scope=upcoming&limit=100&offset=${offset}`));
    if (!result) {
      overviewCalendarError.value = requestError.value || '無法載入最近的未來行程。';
      return;
    }
    overviewCalendarEvents.value = result.events;
  } catch (error) {
    overviewCalendarError.value = error?.message || '無法載入最近的未來行程。';
  }
}

async function loadCalendarSources() {
  const result = await request('/calendar/sources');
  if (result) calendarSources.value = result.sources || [];
  return result;
}

async function loadCalendarEvents(monthDate = currentMonth.value) {
  const normalizedMonth = monthStart(monthDate);
  if (!normalizedMonth) return null;
  const month = monthKey(normalizedMonth);
  const requestSequence = calendarRequests.begin();
  calendarLoading.value = true;
  calendarError.value = '';
  try {
    const result = await fetchCalendarPages((offset) => request(`/calendar/events?scope=all&month=${encodeURIComponent(month)}&limit=100&offset=${offset}`));
    if (!calendarRequests.isLatest(requestSequence)) return null;
    if (!result) {
      calendarError.value = requestError.value || '行事曆載入失敗，請重試。';
      return null;
    }
    calendarEventsByMonth[month] = result.events;
    return result;
  } catch (error) {
    if (calendarRequests.isLatest(requestSequence)) calendarError.value = error?.message || '行事曆載入失敗，請重試。';
    return null;
  } finally {
    if (calendarRequests.isLatest(requestSequence)) calendarLoading.value = false;
  }
}

function clearCalendarCaches() {
  Object.keys(calendarEventsByMonth).forEach((month) => { delete calendarEventsByMonth[month]; });
  overviewCalendarEvents.value = [];
}

function retryCalendarEvents() {
  return loadCalendarEvents(currentMonth.value);
}

function retrySource(sourceId) {
  return route.path === '/inbox' ? loadInboxSource(sourceId) : viewSource(sourceId);
}

async function loadIntegrations() {
  if (!signedIn.value || integrationsLoading.value) return null;
  if (guestMode.value && guestWorkspace.value) {
    integrationsError.value = '';
    integrationActionMessage.value = '';
    integrations.value = guestApiRequest(guestWorkspace.value, '/integrations');
    return integrations.value;
  }
  integrationsLoading.value = true;
  integrationsError.value = '';
  integrationActionMessage.value = '';
  try {
    integrations.value = await requestJson('/integrations');
    return integrations.value;
  } catch (error) {
    integrations.value = null;
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    integrationsError.value = '目前無法讀取整合狀態，請稍後再試。';
    return null;
  } finally {
    integrationsLoading.value = false;
  }
}

async function openAuthModal() {
  lastFocusedElement.value = document.activeElement;
  authModal.value = true; authLoading.value = true; authProvidersLoading.value = true; authNotice.value = null; authStatus.value = '正在檢查可用的登入方式…';
  try {
    const available = await requestJson('/auth/providers');
    Object.assign(providers, normalizeProviders(available));
    authStatus.value = enabledProviderCount.value ? '' : '目前沒有可用的登入方式。';
  } catch (error) {
    Object.assign(providers, { github: false, google: false });
    authStatus.value = '登入服務目前無法使用。';
    authNotice.value = { kind: 'error', message: error instanceof ApiError && error.status >= 500 ? '服務暫時忙碌，請稍後重試。' : '找不到登入服務；請確認 backend 已啟動。', retryable: true };
  } finally { authProvidersLoading.value = false; authLoading.value = false; }
  await nextTick();
  if (providers.google) googleAuthButtonRef.value?.focus();
  else if (providers.github) firstAuthButtonRef.value?.focus();
  else guestAuthButtonRef.value?.focus();
}
async function startGuestSession() {
  // Invalidate an /auth/me request that may still be in flight. Guest mode must
  // never be overwritten by a late real-account bootstrap response.
  authLoadGeneration += 1;
  const workspace = createGuestWorkspace(new Date());
  requestError.value = '';
  requestErrorStatus.value = 0;
  requestErrorCode.value = '';
  dashboardError.value = '';
  calendarError.value = '';
  overviewCalendarError.value = '';
  integrationsError.value = '';
  guestWorkspace.value = workspace;
  guestMode.value = true;
  saveGuestWorkspace(workspace, window.sessionStorage);
  currentUser.value = workspace.user;
  signedIn.value = true;
  authSessionState.value = 'signed-in';
  authModal.value = false;
  Object.assign(form, { title: '', body: '', source_id: null, attachments: [] });
  clearCalendarCaches();
  listsLoaded.value = false;
  listTasksLoaded.value = false;
  await loadDashboard();
  await loadLists();
  await loadListTasks('');
  if (route.path !== '/today') await router.replace('/today');
}
function closeAuthModal() { authModal.value = false; lastFocusedElement.value?.focus?.(); }
function startOAuth(provider) {
  if (!providers[provider]) { authNotice.value = { kind: 'error', message: '這個登入方式目前尚未開放。', retryable: false }; return; }
  authLoading.value = true; authNotice.value = null; authStatus.value = `正在前往 ${provider === 'github' ? 'GitHub' : 'Google'} 登入…`; window.location.href = `${API}/auth/${provider}/start`;
}
function linkLoginProvider(provider) {
  window.location.assign(`${API}/auth/${provider}/start?mode=link`);
}
async function signOut() {
  if (signOutBusy.value) return;
  signOutBusy.value = true;
  signOutError.value = '';
  try {
    if (guestMode.value) {
      clearAuthState();
      await router.replace('/');
      return;
    }
    await logoutAfterServerConfirmation(requestJson, clearAuthState);
  } catch {
    signOutError.value = '登出沒有完成；目前仍保留登入狀態，請檢查連線後重試。';
  } finally {
    signOutBusy.value = false;
  }
}

const AVATAR_MAX_BYTES = 2 * 1024 * 1024;
const AVATAR_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('無法讀取這張圖片。'));
    reader.onload = () => resolve(String(reader.result || ''));
    reader.readAsDataURL(file);
  });
}

async function updateAvatar(file) {
  if (!file || accountBusy.value) return;
  accountBusy.value = 'avatar';
  accountMessage.value = '';
  accountError.value = '';
  accountErrorAction.value = 'avatar';
  try {
    if (!AVATAR_TYPES.has(file.type)) throw new Error('請選擇 PNG、JPEG 或 WebP 圖片。');
    if (file.size > AVATAR_MAX_BYTES) throw new Error('頭像圖片請控制在 2 MB 以內。');
    const dataUrl = await readFileAsDataUrl(file);
    if (guestMode.value && guestWorkspace.value) {
      guestWorkspace.value.user.avatar_url = dataUrl;
      currentUser.value = { ...guestWorkspace.value.user };
      saveGuestWorkspace(guestWorkspace.value, window.sessionStorage);
      accountMessage.value = '頭像已更新。';
      return;
    }
    const payload = await requestJson('/account/avatar', { method: 'PATCH', body: JSON.stringify({ data_url: dataUrl }) });
    currentUser.value = payload?.user || currentUser.value;
    accountMessage.value = '頭像已更新。';
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    accountError.value = error instanceof Error && error.message !== 'Failed to fetch' ? error.message : '目前無法更新頭像，請稍後再試。';
  } finally {
    accountBusy.value = '';
  }
}

async function deleteAccount() {
  if (accountBusy.value) return;
  accountBusy.value = 'delete';
  accountMessage.value = '';
  accountError.value = '';
  accountErrorAction.value = 'delete';
  try {
    if (guestMode.value) {
      clearAuthState();
      await router.replace('/');
      return;
    }
    await requestJson('/account', { method: 'DELETE' });
    accountBusy.value = '';
    try { window.sessionStorage.removeItem('omt:last-workspace-route'); window.sessionStorage.removeItem('omt:workspace-reload'); } catch {}
    clearAuthState();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    else accountError.value = error instanceof Error && error.message !== 'Failed to fetch' ? error.message : '刪除帳戶失敗，請稍後再試。';
  } finally {
    accountBusy.value = '';
  }
}

async function openCalendarModal() {
  lastFocusedElement.value = document.activeElement; calendarModal.value = true; calendarSourcesLoading.value = true; calendarStatus.value = '正在讀取已連接的行事曆…';
  const result = await request('/calendar/sources');
  calendarSources.value = result?.sources || []; calendarSourcesLoading.value = false; calendarStatus.value = result ? '' : '目前無法讀取同步狀態；仍可檢查 URL，稍後再試。';
  await nextTick(); calendarServerRef.value?.focus();
}
function closeCalendarModal() { calendarModal.value = false; calendarBusy.value = false; clearCalendarPoll(); calendarFlow.value = null; lastFocusedElement.value?.focus?.(); }
function normalizeCalendarUrl(value) { const cleaned = value.trim().replace(/^`|`$/g, '').replace(/^\\+|\\+$/g, ''); if (!cleaned) return ''; if (/^https?:\/\//i.test(cleaned)) return cleaned.replace(/\/+$/, ''); return `https://${cleaned.replace(/^\/+/, '').replace(/\/+$/, '')}`; }
function clearCalendarPoll() { if (calendarPollTimer.value) window.clearTimeout(calendarPollTimer.value); calendarPollTimer.value = null; }
async function pollNextcloudLogin(flowId) {
  if (!calendarModal.value) return;
  const result = await request(`/calendar/nextcloud/login/${flowId}/poll`, { method: 'POST' });
  if (!calendarModal.value) return;
  if (!result) { calendarBusy.value = false; calendarStatus.value = '登入流程輪詢失敗，請確認 Nextcloud URL 與 backend 連線。'; return; }
  if (result.status === 'pending') { calendarStatus.value = '等待 Nextcloud 授權完成…這個視窗可以保留在背景。'; calendarPollTimer.value = window.setTimeout(() => pollNextcloudLogin(flowId), (result.poll_interval || 2) * 1000); return; }
  clearCalendarPoll(); calendarBusy.value = false;
  await loadIntegrations();
  if (result.status === 'connected') { calendarFlow.value = null; calendarSources.value = [...calendarSources.value.filter((item) => item.source.id !== result.source.id), { source: result.source, calendars: result.calendars }]; calendarStatus.value = `已連線，找到 ${result.calendars.length} 個唯讀行事曆。`; lastAutomaticCalendarSyncAt = 0; onCalendarAppVisible(); return; }
  calendarStatus.value = result.message || 'Nextcloud 登入未完成。';
}
async function startNextcloudLogin() {
  const normalizedUrl = normalizeCalendarUrl(calendarUrl.value);
  if (!normalizedUrl) { calendarStatus.value = '請先輸入 Nextcloud URL。'; calendarServerRef.value?.focus(); return; }
  calendarUrl.value = normalizedUrl;
  const loginWindow = window.open('about:blank', '_blank'); if (loginWindow) loginWindow.opener = null;
  calendarBusy.value = true; calendarStatus.value = '正在建立 Nextcloud Login Flow…';
  const connectionResult = await request('/integrations/calendar/connections/nextcloud_calendar/connect', { method: 'POST', body: JSON.stringify({ base_url: normalizedUrl, sync_mode: 'manual' }) });
  const result = connectionResult?.flow || connectionResult;
  if (!result) { calendarBusy.value = false; loginWindow?.close?.(); calendarStatus.value = '無法建立登入流程，請確認 Nextcloud URL 與 backend 連線。'; return; }
  calendarFlow.value = result; calendarStatus.value = '已準備好 Nextcloud 登入頁，正在新分頁開啟…';
  if (loginWindow && result.login_url) loginWindow.location.href = result.login_url;
  if (!result.login_url) { loginWindow?.close?.(); calendarBusy.value = false; calendarStatus.value = 'Nextcloud 沒有回傳可用的登入頁，請稍後再試。'; return; }
  if (!loginWindow) calendarStatus.value = '瀏覽器阻擋了新分頁，請點下方「開啟登入頁」。';
  pollNextcloudLogin(result.flow_id);
}
async function disconnectCalendar(sourceId) {
  const result = await request(`/calendar/sources/${sourceId}`, { method: 'DELETE' });
  if (result) {
    calendarSources.value = calendarSources.value.filter((item) => item.source.id !== sourceId);
    clearCalendarCaches();
    await loadDashboard();
    calendarStatus.value = '已移除同步來源。';
  }
  else calendarStatus.value = '移除同步來源失敗，請稍後再試。';
}

async function syncCalendar(sourceId) {
  if (!sourceId || calendarSyncingId.value) return;
  calendarSyncingId.value = sourceId;
  const result = await request(`/calendar/sources/${sourceId}/sync`, { method: 'POST' });
  calendarSyncingId.value = '';
  if (result) {
    const sources = await request('/calendar/sources');
    if (sources) calendarSources.value = sources.sources || [];
    await loadDashboard();
  }
}

function onCalendarAppVisible() {
  if (guestMode.value || !signedIn.value || document.visibilityState === 'hidden') return;
  if (automaticCalendarSyncTimer) window.clearTimeout(automaticCalendarSyncTimer);
  if (Date.now() - lastAutomaticCalendarSyncAt < CALENDAR_AUTO_SYNC_INTERVAL_MS) return;
  automaticCalendarSyncTimer = window.setTimeout(async () => {
    automaticCalendarSyncTimer = null;
    if (!signedIn.value || document.visibilityState === 'hidden') return;
    lastAutomaticCalendarSyncAt = Date.now();
    const result = await request('/calendar/auto-sync', { method: 'POST' });
    if (!result) return;
    if (automaticCalendarRefreshTimer) window.clearTimeout(automaticCalendarRefreshTimer);
    automaticCalendarRefreshTimer = window.setTimeout(() => {
      automaticCalendarRefreshTimer = null;
      if (signedIn.value && document.visibilityState !== 'hidden') {
        void Promise.all([loadCalendarEvents(currentMonth.value), loadOverviewCalendarEvents()]);
      }
    }, 1000);
  }, 250);
}

async function connectGoogleCalendar() {
  integrationBusy.value = 'google_calendar:connect';
  integrationActionMessage.value = '';
  try {
    const result = await requestJson('/integrations/calendar/connections/google_calendar/connect', {
      method: 'POST',
      body: JSON.stringify({ sync_mode: 'manual' }),
    });
    if (!result?.authorization_url) throw new Error('Google Calendar 沒有回傳可用的連結頁。');
    window.location.assign(result.authorization_url);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    integrationActionMessage.value = error instanceof Error && error.message !== 'Failed to fetch'
      ? error.message
      : '目前無法開始 Google Calendar 連結，請稍後再試。';
    integrationBusy.value = '';
  }
}

async function disconnectIntegration(connectionId) {
  if (!connectionId || integrationBusy.value) return;
  integrationBusy.value = `${connectionId}:disconnect`;
  integrationActionMessage.value = '';
  try {
    await requestJson(`/integrations/calendar/connections/${connectionId}`, { method: 'DELETE' });
    await loadIntegrations();
    await loadCalendarSources();
    clearCalendarCaches();
    await loadDashboard();
    integrationActionMessage.value = '已解除連結。';
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    integrationActionMessage.value = '解除連結失敗，請稍後再試。';
  } finally {
    integrationBusy.value = '';
  }
}

async function syncIntegration(connectionId, provider) {
  if (!connectionId || integrationBusy.value) return;
  integrationBusy.value = `${connectionId}:sync`;
  integrationActionMessage.value = '';
  try {
    const result = await requestJson(`/integrations/calendar/connections/${connectionId}/sync`, { method: 'POST' });
    await loadIntegrations();
    await loadCalendarSources();
    await loadDashboard();
    integrationActionMessage.value = result?.sync?.status === 'error'
      ? `同步失敗：${result.sync.error || '外部日曆沒有回傳詳細原因。'}`
      : `同步完成${result?.sync?.last_synced_at ? `：${new Intl.DateTimeFormat('zh-TW', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(result.sync.last_synced_at))}` : '。'}`;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearAuthState();
    } else if (error instanceof ApiError && error.status === 409 && provider === 'google_calendar') {
      integrationActionMessage.value = 'Google Calendar 授權已失效，請解除連線後重新授權。';
      await loadIntegrations();
    } else {
      integrationActionMessage.value = error instanceof Error && error.message !== 'Failed to fetch'
        ? `同步失敗：${error.message}`
        : '同步失敗，請稍後再試。';
    }
  } finally {
    integrationBusy.value = '';
  }
}

function attachmentKind(file) {
  const filename = file.name.toLowerCase();
  if (file.type === 'application/pdf' || filename.endsWith('.pdf')) return 'pdf';
  if (file.type.startsWith('image/') || /\.(heic|heif)$/.test(filename)) return 'image';
  return 'file';
}
async function uploadCaptureAttachment(attachment) {
  if (!attachment?.file || attachment.upload_id) return attachment;
  attachment.upload_error = '';
  try {
    const response = await fetch(`${API}/capture/uploads?filename=${encodeURIComponent(attachment.name)}`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': attachment.media_type || 'application/octet-stream' },
      body: attachment.file,
    });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (!response.ok) throw new ApiError(response.status, payload);
    const uploaded = payload?.upload;
    if (!uploaded?.id) throw new Error('上傳服務沒有回傳檔案識別碼。');
    attachment.upload_id = uploaded.id;
    attachment.media_type = uploaded.media_type || attachment.media_type;
    attachment.kind = uploaded.kind || attachment.kind;
    attachment.size = Number(uploaded.size || attachment.size || 0);
    return attachment;
  } catch (error) {
    attachment.upload_error = error instanceof ApiError ? error.message : '檔案上傳失敗';
    if (error instanceof ApiError && error.status === 401) clearAuthState();
    throw new Error(`「${attachment.name}」上傳失敗：${attachment.upload_error}`);
  }
}

async function prepareCaptureAttachments() {
  const selected = [...form.attachments];
  await Promise.all(selected.map((attachment) => uploadCaptureAttachment(attachment)));
  const incomplete = selected.find((attachment) => attachment?.file && !attachment?.upload_id);
  if (incomplete) throw new Error(`「${incomplete.name}」上傳沒有完成，請重新選取。`);
  return selected.map(({ file, upload_error, data_url, ...metadata }) => metadata);
}
function addFiles(files) {
  if (!files.length) return;
  captureError.value = '';
  let totalBytes = form.attachments.reduce((sum, attachment) => sum + Number(attachment.size || 0), 0);
  for (const file of files) {
    if (form.attachments.length >= ingestionPolicy.max_attachments_per_capture) {
      captureError.value = `一次最多加入 ${ingestionPolicy.max_attachments_per_capture} 個檔案。`;
      break;
    }
    if (file.size > ingestionPolicy.max_upload_bytes) {
      captureError.value = `檔案太大，單一檔案上限為 ${Math.round(ingestionPolicy.max_upload_bytes / 1024 / 1024)} MB。`;
      continue;
    }
    if (totalBytes + file.size > ingestionPolicy.max_total_capture_bytes) {
      captureError.value = `這次上傳的總量太大，上限為 ${Math.round(ingestionPolicy.max_total_capture_bytes / 1024 / 1024)} MB。`;
      continue;
    }
    const kind = attachmentKind(file);
    const filename = file.name.toLowerCase();
    const fallbackMediaType = kind === 'pdf'
      ? 'application/pdf'
      : filename.endsWith('.docx')
        ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        : filename.endsWith('.odt')
          ? 'application/vnd.oasis.opendocument.text'
          : filename.endsWith('.ods')
            ? 'application/vnd.oasis.opendocument.spreadsheet'
            : filename.endsWith('.odp')
              ? 'application/vnd.oasis.opendocument.presentation'
              : filename.endsWith('.heic')
                ? 'image/heic'
                : filename.endsWith('.heif')
                  ? 'image/heif'
                  : 'application/octet-stream';
    const attachment = { id: `${file.name}-${file.lastModified}-${Math.random().toString(36).slice(2)}`, file, name: file.name, media_type: file.type || fallbackMediaType, size: file.size, kind, upload_id: '', upload_error: '' };
    form.attachments.push(attachment);
    totalBytes += file.size;
  }
  captureStatus.value = 'pending';
}
function removeAttachment(id) {
  form.attachments = form.attachments.filter((attachment) => attachment.id !== id);
}
function sourceUrlFromText(text) {
  return text.match(/https?:\/\/[^\s]+/i)?.[0]?.replace(/[),.;，。]+$/, '') || null;
}
function decorateProposals(proposals) {
  return proposals.map((proposal) => {
    const collection = proposal.target_type === 'event' ? state.events : state.tasks;
    const target = collection.find((item) => item.id === proposal.target_id);
    return {
      ...proposal,
      target_title: target?.title || proposal.target_title,
      target_location: target?.location || proposal.target_location,
      target_date: target?.date_label || target?.due_label || proposal.target_date,
      target_time: target?.time_label || proposal.target_time,
    };
  });
}
async function submitCapture() {
  if (guestMode.value) {
    captureError.value = '展示帳號不會送出 AI 請求；使用 Google 或 GitHub 登入後即可整理資訊。';
    return;
  }
  if (!canSubmitNotice.value) return;
  if (captureBusy.value) return;
  let isNewSource = !form.source_id;
  captureError.value = '';
  captureStatus.value = 'processing';
  let attachments = [];
  try {
    if (form.attachments.some((attachment) => attachment?.file)) {
      attachments = await prepareCaptureAttachments();
    } else {
      attachments = form.attachments.map(({ file, upload_error, ...metadata }) => metadata);
    }
  } catch (error) {
    captureStatus.value = 'failed';
    captureError.value = error instanceof Error ? error.message : '檔案讀取失敗，請移除後重新選取。';
    return;
  }
  // Snapshot text after local file transfer so edits made while the request starts
  // are still included in the same Capture action. The UI exposes only one
  // user-facing state: organizing.
  const sourceBody = form.body.trim();
  const sourceUrl = sourceUrlFromText(sourceBody);
  const buildInterpretOptions = (sourceId) => ({
    method: 'POST',
    body: JSON.stringify({
      title: '',
      body: sourceBody,
      source_id: sourceId || null,
      audience: '我的課程',
      source_type: sourceUrl && sourceBody === sourceUrl ? 'url' : 'manual',
      source_url: sourceUrl,
      attachments,
    }),
  });
  let result = await request('/interpret', buildInterpretOptions(form.source_id));
  // A draft source may have been discarded or expired while the user kept editing.
  // Treat that specific 404 as a new Capture instead of surfacing a misleading Not Found.
  if (!result && requestErrorStatus.value === 404 && form.source_id) {
    form.source_id = null;
    isNewSource = true;
    requestError.value = '';
    requestErrorStatus.value = 0;
    requestErrorCode.value = '';
    result = await request('/interpret', buildInterpretOptions(null));
  }
  if (!result) { captureStatus.value = 'failed'; captureError.value = requestErrorCode.value === 'malformed_output' ? 'AI 有回應，但整理結果格式不完整；請再試一次。' : (requestError.value || captureRequestFailureMessage(sourceUrl, requestErrorStatus.value)); return; }
  if (attachments.some((attachment) => attachment?.upload_id) && result.release !== 'json-object-v26') {
    captureStatus.value = 'failed';
    captureError.value = '目前檔案整理服務尚未更新，請稍後再試。';
    return;
  }
  const authoritativeSourceId = String(result.source?.id || '');
  const proposals = (result.proposals || []).filter((proposal) => ['pending', 'edited'].includes(proposal.status) && isPreviewableProposal(proposal));
  const sourceBindingValid = authoritativeSourceId
    && proposals.every((proposal) => String(proposal.source_id || '') === authoritativeSourceId);
  if (!sourceBindingValid) {
    captureStatus.value = 'failed';
    captureError.value = '這批整理結果與原始資訊無法對應，尚未顯示或套用；請重新整理後再試。';
    return;
  }
  newCaptureSourceId.value = isNewSource ? String(result.source?.id || '') : '';
  let sourceDetails = null;
  if (result.source?.id) {
    try { sourceDetails = await requestJson(`/notices/${encodeURIComponent(result.source.id)}`); } catch { sourceDetails = null; }
  }
  if (sourceDetails?.id && String(sourceDetails.id) !== authoritativeSourceId) {
    captureStatus.value = 'failed';
    captureError.value = '這批整理結果與原始資訊無法對應，尚未顯示或套用；請重新整理後再試。';
    return;
  }
  const reviewPreview = { ...result, source: sourceDetails || result.source || null, proposals: decorateProposals(proposals), unsupported_attachments: result.unsupported_attachments || [] };
  if (!newCaptureSourceId.value) Object.assign(form, captureDraftAfterInterpret(form, result.source));
  captureStatus.value = 'completed';
  preview.value = reviewPreview;
  proposalReturnToOverview.value = false;
  proposalError.value = reviewPreview.source?.processing?.ai?.status === 'too_large'
    ? '這段原始資訊太長，無法安全整理完整內容。請縮短文字或只提供相關段落後重試。'
    : '';
  proposalApplyResults.value = [];
  proposalModal.value = true;
}
async function acceptProposal(proposal) {
  if (!proposal?.id || !['pending', 'edited'].includes(proposal.status)) return;
  noticeBusy.value = true;
  const result = await request(`/proposals/${proposal.id}/accept`, { method: 'POST' });
  noticeBusy.value = false;
  if (result) Object.assign(proposal, result); else proposalError.value = '接受整理結果失敗，資料沒有變更。';
  proposalApplyResults.value = [];
}
async function rejectProposal(proposal) {
  if (!proposal?.id || !['pending', 'edited'].includes(proposal.status)) return;
  noticeBusy.value = true;
  const result = await request(`/proposals/${proposal.id}/reject`, { method: 'POST' });
  noticeBusy.value = false;
  if (result) Object.assign(proposal, result); else proposalError.value = '目前無法忽略這項整理結果，請稍後再試。';
}
async function editProposal(proposal, targetId = proposal?.target_id, patch = proposal?.patch || {}, targetType = proposal?.target_type) {
  if (!proposal?.id) return;
  noticeBusy.value = true;
  const result = await request(`/proposals/${proposal.id}`, { method: 'PATCH', body: JSON.stringify({ target_id: targetId, target_type: targetType, patch }) });
  noticeBusy.value = false;
  if (result) Object.assign(proposal, result); else proposalError.value = '目前無法儲存修改，請稍後再試。';
}
async function saveProposalEdit({ proposal, patch, targetType }) {
  await editProposal(proposal, proposal.target_id, patch, targetType);
}
async function selectProposalTarget({ proposal, targetId }) {
  await editProposal(proposal, targetId, proposal.patch || {});
}
async function viewProposal(proposal) {
  if (!proposal?.id) return;
  if (!proposalModal.value) lastFocusedElement.value = document.activeElement;
  proposalError.value = '';
  noticeBusy.value = true;
  const result = await request(`/proposals/${proposal.id}`);
  if (!result) {
    noticeBusy.value = false;
    proposalError.value = '目前無法載入這項變更，請稍後再試。';
    return;
  }
  const [sourceResult, proposalResult] = await Promise.all([
    result.source_id ? request(`/notices/${result.source_id}`) : Promise.resolve(null),
    result.source_id ? request('/proposals') : Promise.resolve(null),
  ]);
  noticeBusy.value = false;
  const source = result.source_id ? sourceResult : null;
  const hasCompleteHistory = !result.source_id || Array.isArray(proposalResult?.proposals);
  const historyError = result.source_id && (!sourceResult || !hasCompleteHistory)
    ? '無法載入原始內容的完整整理紀錄；目前不顯示不完整清單，請重新載入。'
    : '';
  const history = result.source_id
    ? (hasCompleteHistory ? proposalResult.proposals.filter((item) => String(item.source_id) === String(result.source_id)) : [])
    : [result];
  preview.value = {
    source,
    proposals: historyError ? [] : decorateProposals(history),
    unsupported_attachments: [],
    sourceHistoryError: historyError,
    sourceHistoryProposal: proposal,
  };
  proposalApplyResults.value = [];
  proposalReturnToOverview.value = true;
  proposalModal.value = true;
}
async function showSourceLoadError(sourceId, message, focusTarget) {
  sourceLoadErrorId.value = String(sourceId);
  sourceLoadError.value = message;
  await nextTick();
  if (focusTarget?.isConnected) focusTarget.focus();
}

async function viewSource(sourceId) {
  if (!sourceId || sourceId === 'manual') return;
  const activeElement = document.activeElement;
  const focusTarget = activeElement?.closest?.('.task-source-actions')?.querySelector?.('.task-source-action') || activeElement;
  lastFocusedElement.value = focusTarget;
  noticeBusy.value = true;
  proposalError.value = '';
  sourceLoadError.value = '';
  sourceLoadErrorId.value = '';
  sourceLoadingId.value = String(sourceId);
  const [sourcePayload, proposalPayload] = await Promise.all([
    request(`/notices/${encodeURIComponent(sourceId)}`),
    request('/proposals'),
  ]);
  noticeBusy.value = false;
  sourceLoadingId.value = '';
  if (!sourcePayload) {
    await showSourceLoadError(sourceId, '無法載入原始內容，請稍後重試。', focusTarget);
    return;
  }

  const source = sourceDetailsFromPayload(sourcePayload, sourceId);
  if (!source) {
    await showSourceLoadError(sourceId, '無法確認這筆原始內容，請重試。', focusTarget);
    return;
  }
  const proposals = Array.isArray(proposalPayload)
    ? proposalPayload
    : Array.isArray(proposalPayload?.proposals)
      ? proposalPayload.proposals
      : null;
  if (!proposals) {
    await showSourceLoadError(sourceId, '無法載入這筆原始內容的變更紀錄，請稍後重試。', focusTarget);
    return;
  }
  const sourceProposals = proposals.filter((proposal) => String(proposal.source_id) === String(sourceId));
  preview.value = {
    source,
    proposals: decorateProposals(sourceProposals),
    unsupported_attachments: [],
  };
  proposalReturnToOverview.value = true;
  proposalModal.value = true;
}

async function applyProposal(proposal) {
  if (!acceptedProposals([proposal]).length) return;
  noticeBusy.value = true;
  proposalError.value = '';
  const result = await request(`/proposals/${proposal.id}/apply`, { method: 'POST' });
  noticeBusy.value = false;
  if (!result) {
    proposalError.value = '目前無法套用這項變更；它仍維持已接受狀態，請稍後再試。';
    proposalApplyResults.value = buildApplyResults([proposal], new Map([[proposal.id, { status: 'failed', message: proposalError.value }]]));
    return;
  }
  Object.assign(proposal, result.proposal || { status: 'applied' });
  if (String(proposal.source_id || '') === newCaptureSourceId.value) newCaptureSourceId.value = '';
  proposalApplyResults.value = buildApplyResults([proposal], new Map([[proposal.id, { status: 'applied', message: '已套用' }]]));
  await refreshProposalDomainState();
}
async function applyAcceptedProposals(proposals = []) {
  if (noticeBusy.value) return;
  const accepted = acceptedProposals(proposals);
  if (!accepted.length) return;
  noticeBusy.value = true;
  proposalError.value = '';
  const outcomes = new Map();
  proposalApplyResults.value = buildApplyResults(proposals, outcomes);
  const batch = await request('/proposals/apply-batch', {
    method: 'POST',
    body: JSON.stringify({ proposal_ids: accepted.map((proposal) => proposal.id), confirm: false }),
  });
  const results = new Map((batch?.results || []).map((result) => [result.id, result]));
  for (const proposal of accepted) {
    const result = results.get(proposal.id);
    if (result?.status === 'applied') {
      Object.assign(proposal, result.proposal || { status: 'applied' });
      if (String(proposal.source_id || '') === newCaptureSourceId.value) newCaptureSourceId.value = '';
      outcomes.set(proposal.id, { status: 'applied', message: '已套用' });
    } else {
      outcomes.set(proposal.id, { status: 'failed', message: result?.message || requestError.value || '套用失敗；這項變更仍維持已接受狀態。' });
    }
  }
  proposalApplyResults.value = buildApplyResults(proposals, outcomes);
  noticeBusy.value = false;
  const failures = [...outcomes.values()].filter((outcome) => outcome.status === 'failed').length;
  proposalError.value = failures ? `${failures} 項變更套用失敗；其他結果已逐項更新，失敗項目仍可重試。` : '';
  await refreshProposalDomainState();
}
async function confirmAndApplyProposals(proposals = preview.value?.proposals || []) {
  if (noticeBusy.value) return;
  const confirmable = confirmableProposals(proposals);
  if (!confirmable.length) return;
  noticeBusy.value = true;
  proposalError.value = '';
  const outcomes = new Map();
  proposalApplyResults.value = buildApplyResults(proposals, outcomes);
  const batch = await request('/proposals/apply-batch', {
    method: 'POST',
    body: JSON.stringify({ proposal_ids: confirmable.map((proposal) => proposal.id), confirm: true }),
  });
  const results = new Map((batch?.results || []).map((result) => [result.id, result]));
  for (const proposal of confirmable) {
    const result = results.get(proposal.id);
    if (result?.status === 'applied') {
      Object.assign(proposal, result.proposal || { status: 'applied' });
      if (String(proposal.source_id || '') === newCaptureSourceId.value) newCaptureSourceId.value = '';
      outcomes.set(proposal.id, { status: 'applied', message: '已套用' });
    } else {
      outcomes.set(proposal.id, { status: 'failed', message: result?.message || requestError.value || '確認失敗，尚未寫入資料。' });
    }
  }
  proposalApplyResults.value = buildApplyResults(proposals, outcomes);
  noticeBusy.value = false;
  const failures = [...outcomes.values()].filter((outcome) => outcome.status === 'failed').length;
  proposalError.value = failures ? `${failures} 項變更未完成；其餘結果已逐項更新，失敗項目仍可重試。` : '';
  await refreshProposalDomainState();
  if (!failures) closeProposalModal();
}
async function refreshProposalDomainState() {
  const data = await request('/dashboard?include_calendar=false');
  if (!data) return;
  if (Array.isArray(data.events)) state.events = data.events;
  if (Array.isArray(data.tasks)) state.tasks = data.tasks;
  if (Array.isArray(data.proposals)) state.proposals = data.proposals;
  if (Array.isArray(data.notices)) state.notices = data.notices;
}


function openEventEditor(event) {
  eventFeedback.value = '';
  eventError.value = '';
  selectedEvent.value = normalizeEvent(cloneEvent(event));
  editingDraft.value = cloneEvent(selectedEvent.value);
  eventEditorOpen.value = true;
}

const eventDeleteIsSeries = computed(() => selectedEvent.value?.recurrence?.frequency === 'weekly');
const eventDeleteTitle = computed(() => eventDeleteIsSeries.value ? '刪除整個行程系列？' : '刪除這個行程？');
const eventDeleteDescription = computed(() => {
  const event = selectedEvent.value;
  if (!event) return '';
  if (!eventDeleteIsSeries.value) return `「${event.title}」將從行事曆移除。`;
  const recurrence = event.recurrence || {};
  const dateRange = recurrence.start_date && recurrence.end_date
    ? `（${recurrence.start_date} 至 ${recurrence.end_date}）`
    : '（整個學期範圍）';
  return `「${event.title}」的每週系列${dateRange}中的所有行程都會移除。`;
});

function requestEventDeleteConfirmation(event = selectedEvent.value) {
  if (!event || eventBusy.value) return;
  selectedEvent.value = normalizeEvent(cloneEvent(event));
  editingDraft.value = cloneEvent(selectedEvent.value);
  eventDeleteReturnFocus.value = document.activeElement;
  eventDeleteError.value = '';
  eventDeleteSucceeded.value = false;
  eventDeleteDialogOpen.value = true;
}

function closeEventDeleteConfirmation() {
  if (eventBusy.value) return;
  eventDeleteDialogOpen.value = false;
}

function onEventDeleteDialogClosed() {
  if (eventDeleteSucceeded.value) {
    eventDeleteSucceeded.value = false;
    closeEventEditor();
    return;
  }
  nextTick(() => eventDeleteReturnFocus.value?.focus?.());
}

function closeEventEditor() {
  if (eventBusy.value) return;
  eventEditorOpen.value = false;
  editingDraft.value = null;
  eventError.value = '';
}

async function saveEvent(payload) {
  const event = selectedEvent.value;
  if (!event) return;
  eventBusy.value = true;
  eventError.value = '';
  const result = await request(`/events/${event.id}`, { method: 'PATCH', body: JSON.stringify(payload || {}) });
  eventBusy.value = false;
  if (!result) {
    eventError.value = requestError.value || '行程更新失敗，請稍後再試。';
    return;
  }
  const localIndex = state.events.findIndex((item) => item.id === event.id);
  if (localIndex >= 0) state.events.splice(localIndex, 1, result);
  for (const [month, events] of Object.entries(calendarEventsByMonth)) {
    const index = events.findIndex((item) => item.id === event.id);
    if (index >= 0) calendarEventsByMonth[month].splice(index, 1, result);
  }
  overviewCalendarEvents.value = overviewCalendarEvents.value.map((item) => item.id === event.id ? result : item);
  selectedEvent.value = structuredClone(normalizeEvent(result));
  eventFeedback.value = '行程已更新。';
  closeEventEditor();
}

async function deleteEvent(event = selectedEvent.value) {
  if (!event) return;
  if (!eventDeleteDialogOpen.value) return;
  eventBusy.value = true;
  eventDeleteError.value = '';
  eventError.value = '';
  const result = await request(`/events/${event.id}`, { method: 'DELETE' });
  eventBusy.value = false;
  if (!result) {
    eventDeleteError.value = requestError.value || '行程刪除失敗，請稍後再試。';
    eventError.value = requestError.value || '行程刪除失敗，請稍後再試。';
    return;
  }
  state.events = withoutDeletedLocalEventSeries(state.events, event.id);
  selectedEvent.value = null;
  overviewCalendarEvents.value = withoutDeletedLocalEventSeries(overviewCalendarEvents.value, event.id);
  for (const month of Object.keys(calendarEventsByMonth)) {
    calendarEventsByMonth[month] = withoutDeletedLocalEventSeries(calendarEventsByMonth[month], event.id);
  }
  // The local event cache is already updated above; a full dashboard reload only adds latency.
  eventDeleteSucceeded.value = true;
  eventDeleteDialogOpen.value = false;
}

async function toggleTask(change) {
  if (taskBusy.value) return;
  const task = change?.task || change;
  const target = task.raw || task;
  const requestedStatus = change?.status === 'done' || change?.status === 'open' ? change.status : null;
  const nextStatus = requestedStatus || (target.status === 'done' ? 'open' : 'done');
  const previousStatus = target.status;
  if (!target.id || nextStatus === previousStatus) return;
  taskError.value = ''; target.status = nextStatus;
  const stateTarget = state.tasks.find((item) => item.id === target.id);
  if (stateTarget) stateTarget.status = nextStatus;
  state.stats.completion = Math.round(state.tasks.filter((item) => item.status === 'done').length / state.tasks.length * 100); taskBusy.value = true;
  const result = await request(`/tasks/${target.id}`, { method: 'PATCH', body: JSON.stringify({ status: nextStatus }) }); taskBusy.value = false;
  if (result) {
    state.tasks = state.tasks.map((item) => item.id === result.id ? result : item);
    Object.assign(target, result);
    listTaskRecords.value = listTaskRecords.value.map((item) => item.id === result.id ? result : item);
    return;
  }
  state.tasks = state.tasks.map((item) => item.id === target.id ? { ...item, status: previousStatus } : item);
  target.status = previousStatus; state.stats.completion = Math.round(state.tasks.filter((item) => item.status === 'done').length / state.tasks.length * 100); taskError.value = '待辦尚未同步，已保留原本狀態。請確認連線後再試。';
}

function requestTaskDeleteConfirmation(task) {
  if (!task?.id || taskBusy.value) return;
  taskPendingDelete.value = task;
  taskDeleteReturnFocus.value = document.activeElement;
  taskDeleteError.value = '';
  taskDeleteDialogOpen.value = true;
}

function closeTaskDeleteConfirmation() {
  if (taskBusy.value) return;
  taskDeleteDialogOpen.value = false;
  restoreConfirmationFocus(taskDeleteReturnFocus);
}

function onTaskDeleteDialogClosed() {
  taskPendingDelete.value = null;
  taskDeleteError.value = '';
}

async function deleteTask() {
  const task = taskPendingDelete.value;
  if (!task?.id || taskBusy.value) return;
  taskBusy.value = true;
  taskDeleteError.value = '';
  const result = await request(`/tasks/${task.id}`, { method: 'DELETE' });
  if (!result) {
    taskBusy.value = false;
    taskDeleteError.value = requestError.value || '待辦刪除失敗，請重試。';
    return;
  }

  state.tasks = state.tasks.filter((item) => item.id !== task.id);
  listTaskRecords.value = listTaskRecords.value.filter((item) => item.id !== task.id);
  const remainingTasks = state.tasks;
  state.stats.completion = remainingTasks.length
    ? Math.round(remainingTasks.filter((item) => item.status === 'done').length / remainingTasks.length * 100)
    : 0;

  // The task and list caches are already updated above; keep the UI reactive without a reload.
  taskError.value = '';
  taskBusy.value = false;
  taskDeleteDialogOpen.value = false;
}

function navigateWorkspace(path) { mobileNavOpen.value = false; if (route.path !== path) router.push(path); }
function selectedDateInMonth(targetMonth) {
  const selected = parseDateLabel(selectedCalendarDateKey.value, runtimeNow.value) || runtimeNow.value;
  const lastDay = new Date(targetMonth.getFullYear(), targetMonth.getMonth() + 1, 0).getDate();
  return new Date(targetMonth.getFullYear(), targetMonth.getMonth(), Math.min(selected.getDate(), lastDay));
}
function moveCalendarMonth(offset) {
  const targetMonth = shiftMonth(currentMonth.value, offset);
  if (!targetMonth) return;
  const nextSelected = selectedDateInMonth(targetMonth);
  currentMonth.value = targetMonth;
  selectedCalendarDateKey.value = dateKey(nextSelected);
  loadCalendarEvents(targetMonth);
}
function previousMonth() { moveCalendarMonth(-1); }
function nextMonth() { moveCalendarMonth(1); }
function selectCalendarDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return;
  const date = parseDateLabel(value);
  if (!date || dateKey(date) !== value) return;
  currentMonth.value = monthStart(date);
  selectedCalendarDateKey.value = dateKey(date);
  loadCalendarEvents(currentMonth.value);
}
function scrollToToday() { currentMonth.value = monthStart(new Date()); selectedCalendarDateKey.value = dateKey(new Date()); loadCalendarEvents(currentMonth.value); nextTick(() => document.querySelector('.calendar-day.today')?.scrollIntoView({ behavior: 'smooth', block: 'center' })); }
function editSource(item) { if (item) { form.title = ''; form.body = item.body || ''; form.source_id = item.id || null; form.attachments = (item.attachments || []).map((attachment) => ({ ...attachment, id: attachment.id || attachment.name })); } }
function clearCaptureDraft() { form.title = ''; form.body = ''; form.source_id = null; form.attachments = []; fileStatus.value = ''; captureError.value = ''; proposalError.value = ''; }
function closeProposalModal() {
  clearCaptureDraft();
  newCaptureSourceId.value = '';
  captureStatus.value = 'idle';
  proposalModal.value = false;
  preview.value = null;
  proposalReturnToOverview.value = false;
  lastFocusedElement.value?.focus?.();
}
function captureHasAppliedProposal() {
  return proposalApplyResults.value.some((result) => result.status === 'applied')
    || Boolean(preview.value?.proposals?.some((proposal) => proposal.status === 'applied'));
}
async function returnToCapture() {
  if (cancelingCaptureReview.value) return;
  cancelingCaptureReview.value = true;
  try {
    if (proposalReturnToOverview.value) { closeProposalModal(); return; }
    if (newCaptureSourceId.value && !captureHasAppliedProposal()) {
      const discarded = await request(`/notices/${encodeURIComponent(newCaptureSourceId.value)}/discard`, { method: 'POST' });
      if (!discarded) {
        proposalError.value = requestError.value || '無法丟棄這次新增；已套用的項目與原始資料均已保留。';
        return;
      }
    }
    closeProposalModal();
  } finally {
    cancelingCaptureReview.value = false;
  }
}

const workspaceViewProps = computed(() => ({ guestMode: guestMode.value, activeFilter: activeFilter.value, calendarAgendaEvents: calendarAgendaEvents.value, calendarTasks: state.tasks, calendarDays: calendarDays.value, ...(route.path === '/calendar' ? { calendarError: calendarError.value, calendarLoading: calendarLoading.value || (dashboardLoading.value && !dashboardLoaded.value), calendarRetry: retryCalendarEvents, eventEditorOpen: eventEditorOpen.value, eventEditor: selectedEvent.value, editingDraft: editingDraft.value, eventBusy: eventBusy.value, eventError: eventError.value } : {}), calendarEventCounts: calendarEventCounts.value, calendarEventLabels: calendarEventLabels.value, calendarMonthLabel: calendarMonthLabel.value, calendarSources: calendarSources.value, calendarSourcesLoading: calendarSourcesLoading.value, calendarSyncingId: calendarSyncingId.value, currentUser: currentUser.value, eventFeedback: eventFeedback.value, fileStatus: fileStatus.value, form, capturePolicy: ingestionPolicy, captureBusy: captureBusy.value, captureError: captureError.value, integrations: integrations.value, integrationsError: integrationsError.value, integrationsLoading: integrationsLoading.value, integrationActionMessage: integrationActionMessage.value, integrationBusy: integrationBusy.value, accountBusy: accountBusy.value, accountMessage: accountMessage.value, accountError: accountError.value, accountErrorAction: accountErrorAction.value, nextEvent: nextEvent.value, upcomingEvents: upcomingOverviewEvents.value, overviewCalendarError: overviewCalendarError.value, openTaskCount: openTaskCount.value, proposalBusy: noticeBusy.value, proposalError: proposalError.value, runtimeNow: runtimeNow.value, selectedCalendarDateKey: selectedCalendarDateKey.value, taskBusy: taskBusy.value, taskError: taskError.value, todayEventCount: todayEventCount.value, todayTasks: todayTasks.value, visibleTasks: visibleTasks.value }));
const routeViewProps = computed(() => ({
  ...workspaceViewProps.value,
  dashboardLoaded: dashboardLoaded.value,
  dashboardLoading: dashboardLoading.value,
  dashboardError: dashboardError.value,
  ...(route.path === '/inbox' ? {
    proposals: inboxProposals.value,
    source: inboxSource.value,
    apiBase: API,
    busy: noticeBusy.value,
    loading: inboxPageLoading.value,
    error: inboxError.value,
    sourceLoading: Boolean(sourceLoadingId.value),
    sourceError: sourceLoadError.value,
  } : {}),
  ...(route.path === '/lists' ? {
    lists: lists.value,
    calendarEvents: normalizedOverviewEvents.value,
    eventSearchResults: taskEventSearchResults.value,
    eventSearchLoading: taskEventSearchLoading.value,
    selectedListId: selectedListId.value,
    listsLoading: listsLoading.value,
    listsLoadError: listsLoadError.value,
    listError: listError.value,
    listBusy: listBusy.value,
    taskListLoading: taskListLoading.value,
    taskListError: taskListError.value,
    initialDataUnresolved: listDataUnresolved.value,
    taskError: taskError.value,
    visibleTasks: visibleTasks.value,
    openTaskCount: listTaskRecords.value.filter((task) => task.status !== 'done').length,
  } : {}),
  overdueTasks: overdueTasks.value,
  sourceError: sourceLoadError.value,
  sourceErrorId: sourceLoadErrorId.value,
  sourceLoadingId: sourceLoadingId.value,
}));
watch(authModal, (open) => {
  if (open) {
    if (!authScrollRestore) {
      authScrollRestore = {
        html: document.documentElement.style.overflow,
        body: document.body.style.overflow,
      };
    }
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
  } else if (authScrollRestore) {
    document.documentElement.style.overflow = authScrollRestore.html;
    document.body.style.overflow = authScrollRestore.body;
    authScrollRestore = null;
  }
});
watch(() => [authSessionState.value, signedIn.value, route.path], syncAuthRoute);
watch(() => route.path, (path) => {
  if (!workspacePaths.has(path)) return;
  try { rememberWorkspaceRoute(window.sessionStorage, path); } catch {}
}, { immediate: true });
watch(() => [signedIn.value, route.path], ([isSignedIn, path]) => { if (isSignedIn && path === '/settings') loadIntegrations(); });
watch(() => [signedIn.value, route.path], ([isSignedIn, path]) => {
  if (!isSignedIn) return;
  if (path === '/lists') {
    void (async () => {
      if (!listsLoaded.value && !(await loadLists())) return;
      await loadListTasks(selectedListId.value);
    })();
  }
  if (path === '/inbox') {
    inboxSource.value = null;
    void loadInboxProposals();
  }
});
onMounted(loadAuth);
onMounted(() => {
  runtimeClock = window.setInterval(() => { runtimeNow.value = new Date(); }, 30000);
  document.addEventListener('visibilitychange', onCalendarAppVisible);
  onCalendarAppVisible();
});
watch(signedIn, (isSignedIn) => { if (isSignedIn) onCalendarAppVisible(); });
onBeforeUnmount(() => {
  if (runtimeClock) window.clearInterval(runtimeClock);
  if (automaticCalendarSyncTimer) window.clearTimeout(automaticCalendarSyncTimer);
  if (automaticCalendarRefreshTimer) window.clearTimeout(automaticCalendarRefreshTimer);
  document.removeEventListener('visibilitychange', onCalendarAppVisible);
  if (authScrollRestore) {
    document.documentElement.style.overflow = authScrollRestore.html;
    document.body.style.overflow = authScrollRestore.body;
    authScrollRestore = null;
  }
});
</script>

<template>
  <div class="grain" :class="{ 'omt-v2-grain': ['/today', '/calendar', '/lists'].includes(route.path) }"></div>
  <div v-if="authSessionState === 'checking'" class="auth-boot-screen" aria-live="polite" aria-label="正在開啟工作區"></div>
  <div v-else-if="authSessionState === 'unknown'" class="auth-boot-screen auth-boot-screen--error" role="alert" aria-live="polite">
    <div class="auth-boot-message">
      <strong>目前無法確認登入狀態</strong>
      <span>保留目前頁面，重新連線後再確認工作階段。</span>
      <md-button color="filled" size="small" type="button" @click="loadAuth">重新確認</md-button>
    </div>
  </div>
  <PublicEntry v-else-if="authSessionState === 'signed-out'" :auth-session-state="authSessionState" :open-auth-modal="openAuthModal" />
  <WorkspaceShell v-else-if="signedIn" :dashboard-error="dashboardError" :mobile-nav-open="mobileNavOpen" :profile-name="state.profile.name" :profile-avatar-url="currentUser?.avatar_url || ''" @close-mobile-nav="mobileNavOpen = false" @navigate="navigateWorkspace" @reload="loadDashboard" @sign-out="signOut" @toggle-mobile-nav="mobileNavOpen = !mobileNavOpen">
    <div v-if="signOutError" class="status-banner error sign-out-feedback" role="alert"><span>{{ signOutError }}</span><md-button color="text" size="small" type="button" :disabled="signOutBusy" @click="signOut">{{ signOutBusy ? '登出中…' : '重試登出' }}</md-button></div>
    <RouterView v-slot="{ Component }"><Transition name="omt-page" mode="out-in"><component v-if="route.meta.requiresAuth" :is="Component" :class="{ 'route-data-unresolved': (route.path === '/inbox' && !inboxCanPresentConclusion) || (route.path === '/lists' && listDataUnresolved) }" v-bind="routeViewProps" @accept-proposal="acceptProposal" @apply-all="applyAcceptedProposals" @apply-proposal="applyProposal" @assign-task-list="assignTaskList" @connect-calendar="openCalendarModal" @connect-google-calendar="connectGoogleCalendar" @connect-login-provider="linkLoginProvider" @create-list="createList" @delete-list="requestListDeleteConfirmation" @capture-update="form.body = $event" @capture-files="addFiles" @capture-remove="removeAttachment" @capture-submit="submitCapture" @change-avatar="updateAvatar" @delete-account="deleteAccount" @delete-event="requestEventDeleteConfirmation" @delete-task="requestTaskDeleteConfirmation" @disconnect-calendar="disconnectCalendar" @disconnect-integration="disconnectIntegration" @edit-event="openEventEditor" @cancel-event-edit="closeEventEditor" @save-event="saveEvent" @edit-source="editSource" @event-info="eventFeedback = $event" @filter="activeFilter = $event" @go-calendar="navigateWorkspace('/calendar')" @go-lists="navigateWorkspace('/lists')" @link-task-event="linkTaskEvent" @search-task-events="searchTaskEvents" @update-task-schedule="updateTaskSchedule" @next-month="nextMonth" @open-calendar="openCalendarModal" @prev-month="previousMonth" @reject-proposal="rejectProposal" @retry-integrations="loadIntegrations" @retry-proposals="loadInboxProposals" @retry-source="retrySource" @retry-lists="retryListData" @retry-task-list="loadListTasks(selectedListId)" @save-proposal="saveProposalEdit" @scroll-today="scrollToToday" @select-date="selectCalendarDate" @select-list="selectList" @select-proposal-target="selectProposalTarget" @sync-integration="syncIntegration" @toggle-task="toggleTask" @update-body="form.body = $event" @view-proposal="viewProposal" @view-source="route.path === '/inbox' ? loadInboxSource($event) : viewSource($event)" /></Transition></RouterView>
  </WorkspaceShell>

  <div v-if="proposalModal" class="capture-modal-overlay" aria-hidden="true"></div>
  <ProposalReviewDialog v-if="preview" :open="proposalModal" :source="preview.source" :proposals="preview.proposals" :unsupported-attachments="preview.unsupported_attachments" :overview-mode="proposalReturnToOverview" :source-history-error="preview.sourceHistoryError" :source-history-loading="noticeBusy" :busy="noticeBusy" :error="proposalError" @back="returnToCapture" @accept="acceptProposal" @reject="rejectProposal" @save="saveProposalEdit" @select-target="selectProposalTarget" @apply="applyProposal" @apply-all="confirmAndApplyProposals" @retry-source-history="viewProposal(preview.sourceHistoryProposal)" />
  <md-dialog v-if="eventDeleteDialogOpen && eventEditor" class="omt-destructive-dialog event-delete-confirmation" :open="eventDeleteDialogOpen" quick aria-labelledby="event-delete-title" aria-describedby="event-delete-description" @cancel="closeEventDeleteConfirmation" @closed="onEventDeleteDialogClosed" @open="prepareConfirmationDialog" @opened="syncConfirmationDialog">
    <div slot="headline" class="event-delete-heading">
      <h2 id="event-delete-title">{{ eventDeleteTitle }}</h2>
    </div>
    <div slot="content" class="event-delete-content">
      <p id="event-delete-description">{{ eventDeleteDescription }}</p>
      <p v-if="eventDeleteError" class="event-editor-error" role="alert">{{ eventDeleteError }}</p>
    </div>
    <div slot="actions" class="event-delete-actions">
      <md-button color="text" size="medium" :disabled="eventBusy" @click="closeEventDeleteConfirmation">取消</md-button>
      <md-button color="filled" size="medium" class="event-delete-confirm-action" :disabled="eventBusy" @click="deleteEvent()">{{ eventBusy ? '刪除中…' : eventDeleteIsSeries ? '刪除系列' : '刪除行程' }}</md-button>
    </div>
  </md-dialog>

  <md-dialog v-if="taskPendingDelete" class="omt-destructive-dialog task-delete-confirmation" :open="taskDeleteDialogOpen" quick aria-labelledby="task-delete-title" aria-describedby="task-delete-description" @cancel="closeTaskDeleteConfirmation" @closed="onTaskDeleteDialogClosed" @open="prepareConfirmationDialog" @opened="syncConfirmationDialog">
    <div slot="headline"><h2 id="task-delete-title">刪除待辦？</h2></div>
    <div slot="content">
      <p id="task-delete-description">要刪除「{{ taskPendingDelete.title }}」嗎？刪除後無法還原。</p>
      <p v-if="taskDeleteError" class="task-delete-error" role="alert">{{ taskDeleteError }}</p>
    </div>
    <div slot="actions" class="task-delete-actions">
      <md-button color="text" size="medium" :disabled="taskBusy" @click="closeTaskDeleteConfirmation">取消</md-button>
      <md-button color="filled" size="medium" class="task-delete-confirm-action" :disabled="taskBusy" @click="deleteTask">{{ taskBusy ? '刪除中…' : '刪除待辦' }}</md-button>
    </div>
  </md-dialog>

  <md-dialog v-if="listPendingDelete" class="omt-destructive-dialog list-delete-confirmation" :open="listDeleteDialogOpen" quick aria-labelledby="list-delete-title" aria-describedby="list-delete-description" @cancel="closeListDeleteConfirmation" @closed="closeListDeleteConfirmation" @open="prepareConfirmationDialog" @opened="syncConfirmationDialog">
    <div slot="headline"><h2 id="list-delete-title">刪除個人清單？</h2></div>
    <div slot="content">
      <p id="list-delete-description">「{{ listPendingDelete.name }}」裡的待辦會變成未分組，待辦本身會保留。</p>
      <p v-if="listDeleteError" class="task-delete-error" role="alert">{{ listDeleteError }}</p>
    </div>
    <div slot="actions" class="task-delete-actions">
      <md-button color="text" size="medium" :disabled="listBusy" @click="closeListDeleteConfirmation">取消</md-button>
      <md-button color="filled" size="medium" class="task-delete-confirm-action" :disabled="listBusy" @click="deleteList">{{ listBusy ? '刪除中…' : '刪除清單' }}</md-button>
    </div>
  </md-dialog>

  <div v-if="authModal" class="auth-modal-layer" @keydown.esc.stop.prevent="closeAuthModal">
    <div class="auth-modal-scrim" aria-hidden="true" @click="closeAuthModal"></div>
    <section class="auth-modal-card" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
      <header class="auth-modal-heading">
        <h2 id="auth-modal-title">登入你的工作區</h2>
        <md-icon-button ref="authCloseButtonRef" aria-label="關閉登入視窗" @click="closeAuthModal"><AppIcon name="close" /></md-icon-button>
      </header>
      <div class="auth-modal-content">
        <p class="modal-intro">登入後可使用 AI 整理與同步；也可以先用展示帳號操作範例資料。</p>
        <div class="oauth-actions" aria-label="選擇登入方式">
          <md-button ref="googleAuthButtonRef" color="outlined" size="medium" class="oauth-md3-button" :disabled="authLoading || authProvidersLoading || !providers.google" :aria-describedby="!providers.google ? 'auth-provider-hint' : undefined" @click="startOAuth('google')">
            <span class="oauth-provider"><span class="provider-mark google-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path fill="#4285F4" d="M21.35 12.27c0-.79-.07-1.54-.21-2.27H12v4.3h5.24a4.48 4.48 0 0 1-1.94 2.94v2.45h3.14c1.84-1.69 2.91-4.18 2.91-7.42z"/><path fill="#34A853" d="M12 21.99c2.63 0 4.84-.87 6.45-2.36l-3.14 -2.45c-.87 .58-1.98 .93-3.31 .93-2.54 0-4.69-1.72-5.46-4.03H3.29v2.53A9.74 9.74 0 0 0 12 21.99z"/><path fill="#FBBC05" d="M6.54 14.08A5.84 5.84 0 0 1 6.23 12c0-.72.12-1.42.31-2.08V7.39H3.29A9.99 9.99 0 0 0 2.25 12c0 1.66.4 3.22 1.04 4.61l3.25-2.53z"/><path fill="#EA4335" d="M12 5.89c1.43 0 2.72 .49 3.73 1.45l2.79 -2.79C16.84 2.86 14.63 2 12 2a9.74 9.74 0 0 0-8.71 5.39l3.25 2.53C7.31 7.61 9.46 5.89 12 5.89z"/></svg></span><span>使用 Google 登入</span></span>
          </md-button>
          <md-button ref="firstAuthButtonRef" color="filled" size="medium" class="oauth-md3-button oauth-primary-button" :disabled="authLoading || authProvidersLoading || !providers.github" :aria-describedby="!providers.github ? 'auth-provider-hint' : undefined" @click="startOAuth('github')">
            <span class="oauth-provider"><span class="provider-mark github-mark" aria-hidden="true"><svg viewBox="0 0 240 240"><circle cx="120" cy="120" r="120" fill="#111111" /><path fill="#ffffff" transform="translate(0 240) scale(.1 -.1)" d="M970 2301 c-305 -68 -555 -237 -727 -493 -301 -451 -241 -1056 143 -1442 115 -116 290 -228 422 -271 49 -16 55 -16 77 -1 24 16 25 20 25 135 l0 118 -88 -5 c-103 -5 -183 13 -231 54 -17 14 -50 62 -73 106 -38 74 -66 108 -144 177 -26 23 -27 24 -9 37 43 32 130 1 185 -65 96 -117 133 -148 188 -160 49 -10 94 -6 162 14 9 3 21 24 27 48 6 23 22 58 35 77 l24 35 -81 16 c-170 35 -275 96 -344 200 -64 96 -85 179 -86 334 0 146 16 206 79 288 28 36 31 47 23 68 -15 36 -11 188 5 234 13 34 20 40 47 43 45 5 129 -24 214 -72 l73 -42 64 15 c91 21 364 20 446 0 l62 -16 58 35 c77 46 175 82 224 82 39 0 39 -1 55 -52 17 -59 20 -166 5 -217 -8 -30 -6 -39 16 -68 109 -144 121 -383 29 -579 -62 -129 -193 -219 -369 -252 l-84 -16 31 -55 32 -56 3 -223 4 -223 25 -16 c23 -15 28 -15 76 2 80 27 217 101 292 158 446 334 590 933 343 1431 -145 293 -419 518 -733 602 -137 36 -395 44 -525 15z" /></svg></span><span>使用 GitHub 登入</span></span>
          </md-button>
          <md-button ref="guestAuthButtonRef" color="tonal" size="medium" class="oauth-md3-button oauth-guest-button" @click="startGuestSession">
            <span class="oauth-provider"><AppIcon name="visibility" /><span>展示帳號</span></span>
          </md-button>
        </div>
        <p v-if="authProviderHint" id="auth-provider-hint" class="auth-provider-hint" aria-live="polite">{{ authProviderHint }}</p>
        <div v-if="authNotice" ref="authFeedbackRef" class="auth-feedback" :class="`auth-feedback-${authNotice.kind}`" role="alert" tabindex="-1">
          <strong>{{ authNotice.kind === 'error' ? '登入需要再試一次' : '登入狀態' }}</strong>
          <span>{{ authNotice.message }}</span>
          <md-button v-if="authNotice.retryable" color="text" size="small" @click="openAuthModal">重新檢查登入方式</md-button>
        </div>
      </div>
    </section>
  </div>

  <md-dialog v-if="calendarModal" class="md3-dialog calendar-auth-dialog" :open="calendarModal" quick @cancel="closeCalendarModal" @closed="closeCalendarModal">
    <div slot="headline" class="md3-dialog-heading"><span><small class="md3-dialog-kicker">CALENDAR SOURCE</small><strong id="calendar-auth-title">連接 Nextcloud</strong></span><md-icon-button aria-label="關閉行事曆連接視窗" @click="closeCalendarModal"><AppIcon name="close" /></md-icon-button></div>
    <div slot="content" class="md3-dialog-content"><p class="modal-intro">請輸入網址</p><md-text-field ref="calendarServerRef" label="Nextcloud 網址" placeholder="cloud.example.com" color="outlined" autocomplete="url" inputmode="url" :value="calendarUrl" @input="calendarUrl = $event.target.value" @blur="calendarUrl = normalizeCalendarUrl(calendarUrl)"></md-text-field><div v-if="calendarSourcesLoading" class="parse-preview loading-preview" role="status">正在讀取已連接的行事曆…</div><div v-else-if="calendarStatus" class="calendar-status-box" role="status" aria-live="polite">{{ calendarStatus }}</div><div v-if="calendarFlow?.login_url" class="calendar-login-link"><a :href="calendarFlow.login_url" target="_blank" rel="noreferrer">開啟 Nextcloud 登入頁 ↗</a><small>完成授權後請保留此視窗，系統會自動等待 CalDAV Calendar。</small></div><div v-if="!calendarSourcesLoading && !calendarSources.length" class="empty-state compact"><strong>尚未連接行事曆</strong><span>目前沒有已連接的行事曆。</span></div><div v-for="item in calendarSources" :key="item.source.id" class="calendar-source-result"><div class="calendar-source-heading"><strong>{{ item.source.name }}</strong><md-button color="text" size="small" @click="disconnectCalendar(item.source.id)">移除連接</md-button></div><small>{{ item.source.server_url }} · {{ item.source.username }}</small><ul><li v-for="calendar in item.calendars" :key="calendar.id || calendar.remote_url">{{ calendar.display_name }}<span v-if="calendar.timezone"> · {{ calendar.timezone }}</span></li></ul></div></div>
    <div slot="actions" class="md3-dialog-actions"><md-button color="text" @click="closeCalendarModal">取消</md-button><md-button color="filled" :disabled="calendarBusy || calendarSourcesLoading || !calendarUrl" @click="startNextcloudLogin"><AppIcon slot="icon" name="link" />{{ calendarBusy ? '等待登入…' : '連接 Nextcloud' }}</md-button></div>
  </md-dialog>
</template>

<style scoped>
.route-data-unresolved :deep(.inbox-count),
.route-data-unresolved :deep(.todo-count) {
  visibility: hidden;
}
</style>
