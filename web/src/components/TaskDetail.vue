<script setup>
import { computed, ref, watch } from 'vue';
import { taskDueLongLabel } from '../taskPresentation.js';

const props = defineProps({
  task: { type: Object, required: true },
  lists: { type: Array, default: () => [] },
  calendarEvents: { type: Array, default: () => [] },
  eventSearchResults: { type: Array, default: () => [] },
  eventSearchLoading: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
});
const emit = defineEmits(['close', 'toggle', 'delete', 'assign-list', 'link-event', 'update-time', 'search-events']);

const linkedEventId = computed(() => String(props.task?.related_event_id || ''));
const linkedEvent = computed(() => [...props.calendarEvents, ...props.eventSearchResults]
  .find((event) => String(event?.id || '') === linkedEventId.value) || null);
const eventQuery = ref('');
const eventPickerOpen = ref(false);
let searchTimer = null;

const scheduleDate = ref('');
const scheduleStart = ref('');
const scheduleEnd = ref('');
const scheduleAllDay = ref(false);

function hydrateSchedule() {
  const start = String(props.task?.start_at || '');
  const end = String(props.task?.end_at || '');
  scheduleDate.value = start.slice(0, 10) || '';
  scheduleStart.value = props.task?.all_day ? '' : start.slice(11, 16);
  scheduleEnd.value = props.task?.all_day ? '' : end.slice(11, 16);
  scheduleAllDay.value = Boolean(props.task?.all_day);
}
watch(() => [props.task?.id, props.task?.start_at, props.task?.end_at, props.task?.all_day], hydrateSchedule, { immediate: true });

const scheduleCanSave = computed(() => {
  if (linkedEventId.value) return false;
  if (!scheduleDate.value) return false;
  return scheduleAllDay.value || Boolean(scheduleStart.value);
});
const hasStandaloneSchedule = computed(() => Boolean(props.task?.start_at));

function localDateTime(date, clock) {
  return date && clock ? `${date}T${clock}:00` : null;
}
function saveSchedule() {
  if (!scheduleCanSave.value || props.busy) return;
  emit('update-time', {
    task: props.task,
    schedule: scheduleAllDay.value
      ? { start_at: scheduleDate.value, end_at: null, all_day: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei' }
      : {
          start_at: localDateTime(scheduleDate.value, scheduleStart.value),
          end_at: scheduleEnd.value ? localDateTime(scheduleDate.value, scheduleEnd.value) : null,
          all_day: false,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei',
        },
  });
}
function clearSchedule() {
  if (props.busy) return;
  emit('update-time', { task: props.task, schedule: { start_at: null, end_at: null, all_day: false, timezone: null } });
}

function eventOptionLabel(event) {
  const when = [event?.dateLabel || event?.date_label, event?.timeLabel || event?.time_label].filter(Boolean).join(' · ');
  return when ? `${event.title} · ${when}` : event?.title || '未命名行程';
}
function searchEvents(query = eventQuery.value) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => emit('search-events', { task: props.task, query }), 180);
}
function openEventPicker() {
  eventPickerOpen.value = true;
  searchEvents('');
}
function chooseEvent(event) {
  emit('link-event', { task: props.task, event_id: event?.id || null });
  eventPickerOpen.value = false;
  eventQuery.value = '';
}
</script>

<template>
  <div class="omt-detail-content">
    <header class="omt-detail-heading">
      <div class="omt-detail-heading-copy">
        <span class="omt-detail-eyebrow">待辦</span>
        <h2>{{ props.task.title }}</h2>
      </div>
      <md-icon-button class="omt-detail-close" aria-label="關閉待辦詳細資料" @click="emit('close')"><AppIcon name="close" /></md-icon-button>
    </header>

    <dl class="omt-detail-facts">
      <div v-if="taskDueLongLabel(props.task)"><dt>期限</dt><dd>{{ taskDueLongLabel(props.task) }}</dd></div>
    </dl>
    <p v-if="props.task.description" class="omt-detail-note">{{ props.task.description }}</p>

    <md-select
      class="omt-detail-select"
      color="outlined"
      label="所屬清單"
      :value="String(props.task.list_id || '__none__')"
      :disabled="props.busy"
      @change="emit('assign-list', { task: props.task, list_id: $event.target.value === '__none__' ? null : ($event.target.value || null) })"
    >
      <md-select-option value="__none__"><span slot="headline">未分組</span></md-select-option>
      <md-select-option v-for="list in props.lists" :key="list.id" :value="String(list.id)"><span slot="headline">{{ list.name }}</span></md-select-option>
    </md-select>

    <section class="omt-task-schedule" aria-labelledby="task-schedule-title">
      <div class="omt-task-schedule-head">
        <div><span class="omt-detail-field-label" id="task-schedule-title">待辦時間</span><small>不連結行程時，可單獨安排全天或一段時間。</small></div>
        <md-button v-if="hasStandaloneSchedule && !linkedEventId" color="text" size="small" :disabled="props.busy" @click="clearSchedule">清除</md-button>
      </div>
      <p v-if="linkedEventId" class="omt-task-schedule-linked"><AppIcon name="link" />已連結行程；時間以行程為主。解除連結後可編輯待辦自己的時間。</p>
      <div class="omt-task-schedule-grid" :class="{ 'is-disabled': Boolean(linkedEventId) }">
        <label class="omt-task-time-field omt-task-time-date">日期<input v-model="scheduleDate" type="date" :disabled="props.busy || Boolean(linkedEventId)" /></label>
        <label class="omt-task-all-day"><md-checkbox touch-target="wrapper" :checked="scheduleAllDay" :disabled="props.busy || Boolean(linkedEventId)" @change="scheduleAllDay = $event.target.checked"></md-checkbox><span>全天</span></label>
        <template v-if="!scheduleAllDay">
          <label class="omt-task-time-field">開始時間<input v-model="scheduleStart" type="time" :disabled="props.busy || Boolean(linkedEventId)" /></label>
          <label class="omt-task-time-field">結束時間（選擇性）<input v-model="scheduleEnd" type="time" :disabled="props.busy || Boolean(linkedEventId)" /></label>
        </template>
      </div>
      <md-button v-if="!linkedEventId" color="tonal" size="small" class="omt-task-schedule-save" :disabled="props.busy || !scheduleCanSave" @click="saveSchedule">儲存時間</md-button>
    </section>

    <section class="omt-event-link-picker" :class="{ open: eventPickerOpen }">
      <span class="omt-detail-field-label">連結行程</span>
      <button type="button" class="omt-event-link-current" :disabled="props.busy" aria-haspopup="listbox" :aria-expanded="eventPickerOpen" @click="eventPickerOpen ? eventPickerOpen = false : openEventPicker()">
        <span>{{ linkedEvent ? eventOptionLabel(linkedEvent) : linkedEventId ? '已連結行程' : '不連結行程' }}</span>
        <AppIcon name="expand_more" />
      </button>
      <div v-if="eventPickerOpen" class="omt-event-link-popover">
        <div class="omt-event-link-search"><AppIcon name="search" /><input v-model="eventQuery" type="search" placeholder="搜尋名稱、日期或星期幾" autocomplete="off" @input="searchEvents()" /></div>
        <div class="omt-event-link-results" role="listbox" aria-label="可連結的近期行程">
          <button type="button" role="option" :aria-selected="!linkedEventId" @click="chooseEvent(null)"><strong>不連結行程</strong><small>使用待辦自己的時間</small></button>
          <p v-if="props.eventSearchLoading" class="omt-event-link-state" role="status">搜尋中…</p>
          <button v-for="event in props.eventSearchResults" :key="event.id" type="button" role="option" :aria-selected="String(event.id) === linkedEventId" @click="chooseEvent(event)">
            <strong>{{ event.title }}</strong><small>{{ [event.dateLabel || event.date_label, event.timeLabel || event.time_label].filter(Boolean).join(' · ') }}</small>
          </button>
          <p v-if="!props.eventSearchLoading && !props.eventSearchResults.length" class="omt-event-link-state">沒有符合的近期行程</p>
        </div>
      </div>
    </section>

    <p v-if="props.error" class="omt-detail-error" role="alert">{{ props.error }}</p>

    <footer class="omt-detail-actions">
      <md-button color="tonal" size="medium" class="omt-detail-primary-action" :disabled="props.busy" @click="emit('toggle', props.task)">{{ props.task.status === 'done' ? '標記為未完成' : '標記為完成' }}</md-button>
      <md-button color="text" size="medium" class="omt-detail-danger-action" :disabled="props.busy" @click="emit('delete', props.task)">刪除待辦</md-button>
    </footer>
  </div>
</template>
