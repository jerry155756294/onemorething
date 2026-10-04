<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import CalendarEventDetail from '../components/CalendarEventDetail.vue';
import EventRow from '../components/EventRow.vue';
import { calendarDayEventCount, calendarEventCountMark, shouldShowCalendarAgendaEmpty } from './calendarPresentation.js';

const props = defineProps({
  calendarDays: { type: Array, default: () => [] },
  calendarEventLabels: { type: Object, default: () => ({}) },
  calendarEventCounts: { type: Object, default: () => ({}) },
  calendarMonthLabel: { type: String, default: '' },
  calendarAgendaEvents: { type: Array, default: () => [] },
  calendarTasks: { type: Array, default: () => [] },
  calendarLoading: { type: Boolean, default: false },
  calendarError: { type: String, default: '' },
  calendarRetry: { type: Function, default: null },
  selectedCalendarDateKey: { type: String, default: '' },
  eventFeedback: { type: String, default: '' },
  eventEditorOpen: { type: Boolean, default: false },
  eventEditor: { type: Object, default: null },
  editingDraft: { type: Object, default: null },
  eventBusy: { type: Boolean, default: false },
  eventError: { type: String, default: '' },
});
const emit = defineEmits([
  'next-month',
  'prev-month',
  'scroll-today',
  'select-date',
  'edit-event',
  'cancel-event-edit',
  'save-event',
  'delete-event',
]);

const selectedEventId = ref('');
const compactViewport = ref(false);
const detailDialog = ref(null);
const returnFocusTarget = ref(null);
const mobileSheetExpanded = ref(false);
const mobileSheetDragStartY = ref(null);
const mobileSheetDragOffset = ref(0);
const monthMotion = ref('next');
const monthMotionSpeed = ref('normal');
let mobileSheetLastMoveY = 0;
let mobileSheetLastMoveAt = 0;
let mobileSheetVelocity = 0;
let viewportQuery = null;
const monthDays = computed(() => {
  const days = props.calendarDays;
  const firstMonthDay = days.findIndex((day) => day.isCurrentMonth);
  const lastMonthDay = days.map((day) => Boolean(day.isCurrentMonth)).lastIndexOf(true);
  if (firstMonthDay < 0 || lastMonthDay < 0) return [];
  const firstWeek = Math.floor(firstMonthDay / 7) * 7;
  const lastWeek = Math.ceil((lastMonthDay + 1) / 7) * 7;
  return days.slice(firstWeek, lastWeek);
});
const monthDisplayLabel = computed(() => {
  const label = String(props.calendarMonthLabel || '').trim();
  const match = label.match(/^(\d{4})\s*年\s*(\d{1,2})\s*月$/);
  return match ? `${Number(match[1])} 年 ${Number(match[2])} 月` : label;
});
const selectedDayLabel = computed(() => {
  const day = props.calendarDays.find((item) => item.dateKey === props.selectedCalendarDateKey);
  if (!day) return '';
  const date = day.date;
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
});
const agendaEvents = computed(() => props.selectedCalendarDateKey ? props.calendarAgendaEvents : []);
const selectedEvent = computed(() => agendaEvents.value.find((event) => String(event.id) === selectedEventId.value) || null);
const selectedEventTasks = computed(() => selectedEvent.value ? props.calendarTasks.filter((task) => String(task.related_event_id || '') === String(selectedEvent.value.id)) : []);
const showAgendaEmptyState = computed(() => shouldShowCalendarAgendaEmpty(agendaEvents.value.length, props.calendarLoading, props.calendarError));

function dayEventCount(dateKey) {
  return calendarDayEventCount(dateKey, props.calendarEventCounts, props.calendarEventLabels);
}

function changeMonth(direction) {
  monthMotion.value = direction;
  monthMotionSpeed.value = 'normal';
  emit(direction === 'next' ? 'next-month' : 'prev-month');
}

function selectCalendarDate(day) {
  if (!day?.dateKey) return;
  selectedEventId.value = '';
  if (!day.isCurrentMonth) {
    const currentDay = props.calendarDays.find((item) => item.isCurrentMonth);
    if (currentDay?.date instanceof Date && day.date instanceof Date) {
      monthMotion.value = day.date < currentDay.date ? 'prev' : 'next';
      monthMotionSpeed.value = 'fast';
    }
  }
  emit('select-date', day.dateKey);
}

function selectEvent(event) {
  if (props.eventEditorOpen && String(event.id) !== String(props.eventEditor?.id)) emit('cancel-event-edit');
  returnFocusTarget.value = document.activeElement;
  selectedEventId.value = String(event.id);
}

function closeEventDetail() {
  if (props.eventEditorOpen) emit('cancel-event-edit');
  mobileSheetExpanded.value = false;
  mobileSheetDragStartY.value = null;
  mobileSheetDragOffset.value = 0;
  selectedEventId.value = '';
  nextTick(() => {
    if (returnFocusTarget.value?.isConnected) returnFocusTarget.value.focus();
    returnFocusTarget.value = null;
  });
}

function updateCompactViewport() {
  compactViewport.value = Boolean(viewportQuery?.matches);
}

function beginMobileSheetDrag(event) {
  if (props.eventEditorOpen) return;
  mobileSheetDragStartY.value = event.clientY;
  mobileSheetDragOffset.value = 0;
  mobileSheetLastMoveY = event.clientY;
  mobileSheetLastMoveAt = performance.now();
  mobileSheetVelocity = 0;
  event.currentTarget?.setPointerCapture?.(event.pointerId);
}

function moveMobileSheetDrag(event) {
  const startY = mobileSheetDragStartY.value;
  if (startY == null || props.eventEditorOpen) return;
  const now = performance.now();
  const elapsed = Math.max(8, now - mobileSheetLastMoveAt);
  mobileSheetVelocity = (event.clientY - mobileSheetLastMoveY) / elapsed;
  mobileSheetLastMoveY = event.clientY;
  mobileSheetLastMoveAt = now;

  const rawDelta = event.clientY - startY;
  // Expanded is the one and only upper snap point. Once there, another upward
  // pull must not translate the dialog even temporarily; only downward motion
  // is allowed so the sheet cannot "walk" up the viewport gesture by gesture.
  if (mobileSheetExpanded.value && rawDelta <= 0) {
    mobileSheetDragOffset.value = 0;
    return;
  }
  const minOffset = mobileSheetExpanded.value ? 0 : -170;
  mobileSheetDragOffset.value = Math.max(minOffset, Math.min(220, rawDelta));
}

function cancelMobileSheetDrag() {
  mobileSheetDragStartY.value = null;
  mobileSheetDragOffset.value = 0;
  mobileSheetVelocity = 0;
}

function endMobileSheetDrag(event) {
  const startY = mobileSheetDragStartY.value;
  const delta = startY == null ? 0 : event.clientY - startY;
  const velocity = mobileSheetVelocity;
  mobileSheetDragStartY.value = null;
  mobileSheetDragOffset.value = 0;
  mobileSheetVelocity = 0;
  if (startY == null || props.eventEditorOpen) return;

  if (mobileSheetExpanded.value) {
    if (delta > 42 || velocity > 0.35) mobileSheetExpanded.value = false;
    return;
  }
  if (delta < -32 || velocity < -0.35) mobileSheetExpanded.value = true;
  else if (delta > 72 || velocity > 0.55) closeEventDetail();
}

watch(() => props.selectedCalendarDateKey, () => {
  if (props.eventEditorOpen) emit('cancel-event-edit');
  mobileSheetExpanded.value = false;
  mobileSheetDragOffset.value = 0;
  selectedEventId.value = '';
});
watch([selectedEvent, compactViewport], async ([event, compact]) => {
  if (!event || !compact) return;
  await nextTick();
  detailDialog.value?.show?.();
});

onMounted(() => {
  viewportQuery = window.matchMedia('(max-width: 900px)');
  updateCompactViewport();
  if (viewportQuery.addEventListener) viewportQuery.addEventListener('change', updateCompactViewport);
  else viewportQuery.addListener?.(updateCompactViewport);
});
onBeforeUnmount(() => {
  if (viewportQuery?.removeEventListener) viewportQuery.removeEventListener('change', updateCompactViewport);
  else viewportQuery?.removeListener?.(updateCompactViewport);
});
</script>

<template>
  <div class="workspace-route route-calendar omt-page">
    <p v-if="props.eventFeedback" class="event-feedback" role="status">{{ props.eventFeedback }}</p>

    <header class="calendar-head omt-page-header">
      <h1>行事曆</h1>
    </header>

    <div v-if="props.calendarError" class="calendar-load-error omt-inline-error" role="alert">
      <span>{{ props.calendarError }}</span>
      <md-button color="text" size="small" :disabled="props.calendarLoading" @click="props.calendarRetry?.()">重試</md-button>
    </div>

    <section class="calendar-layout route-calendar-layout omt-calendar-layout" :class="{ 'has-selected-event': selectedEvent }">
      <section class="calendar-panel md3-calendar-card omt-calendar-panel" :aria-busy="props.calendarLoading" aria-label="月曆">
        <div class="omt-calendar-month-row">
          <strong class="omt-calendar-month-label">{{ monthDisplayLabel }}</strong>
          <div class="omt-calendar-month-controls" aria-label="切換月份">
            <md-button color="text" size="small" class="calendar-today-action" @click="emit('scroll-today')">今天</md-button>
            <md-icon-button class="md3-month-button" aria-label="上一個月" @click="changeMonth('prev')"><AppIcon name="chevron_left" /></md-icon-button>
            <md-icon-button class="md3-month-button" aria-label="下一個月" @click="changeMonth('next')"><AppIcon name="chevron_right" /></md-icon-button>
          </div>
        </div>
        <div class="week-labels" aria-hidden="true"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>
        <div class="calendar-grid-stage">
          <Transition :name="`calendar-month-${monthMotion}${monthMotionSpeed === 'fast' ? '-fast' : ''}`">
            <div :key="props.calendarMonthLabel" class="calendar-grid" role="group" :aria-label="`${props.calendarMonthLabel}日期`">
          <button
            v-for="day in monthDays"
            :key="day.key"
            type="button"
            class="calendar-day"
            :class="{ muted: !day.isCurrentMonth, today: day.isToday, selected: props.selectedCalendarDateKey === day.dateKey, 'has-event': dayEventCount(day.dateKey) > 0, 'many-events': dayEventCount(day.dateKey) > 1 }"
            :aria-current="day.isToday ? 'date' : undefined"
            :aria-pressed="props.selectedCalendarDateKey === day.dateKey"
            :aria-label="`${day.date.getFullYear()}年${day.date.getMonth() + 1}月${day.day}日${dayEventCount(day.dateKey) ? `，${dayEventCount(day.dateKey)} 個行程` : '，沒有行程'}，點擊查看`"
            @click="selectCalendarDate(day)"
          >
            <span>{{ day.day }}</span>
            <b v-if="dayEventCount(day.dateKey)" :class="dayEventCount(day.dateKey) > 1 ? 'calendar-event-count' : 'calendar-event-dot'" aria-hidden="true">{{ calendarEventCountMark(dayEventCount(day.dateKey)) }}</b>
          </button>
            </div>
          </Transition>
        </div>
        <md-divider class="md3-calendar-divider" aria-hidden="true"></md-divider>
      </section>

      <section class="calendar-agenda" :class="{ 'omt-list-section': true }" aria-live="polite" aria-labelledby="calendar-agenda-title">
        <header class="section-heading timeline-heading omt-section-heading">
          <h2 id="calendar-agenda-title">{{ selectedDayLabel || monthDisplayLabel }}</h2>
          <span v-if="props.selectedCalendarDateKey && !props.calendarLoading && !props.calendarError" class="calendar-agenda-count">{{ agendaEvents.length }} 項</span>
        </header>

        <p v-if="props.calendarLoading && !agendaEvents.length" class="omt-inline-state" role="status">載入行程中…</p>
        <div v-else-if="agendaEvents.length" class="timeline calendar-agenda-list omt-row-list">
          <EventRow
            v-for="event in agendaEvents"
            :key="event.id"
            :event="event"
            :interactive="true"
            :selected="String(event.id) === selectedEventId"
            @select="selectEvent"
          />
        </div>
        <div v-else-if="showAgendaEmptyState" class="empty-state compact omt-inline-state">
          <strong>{{ selectedDayLabel ? `${selectedDayLabel} 沒有行程` : '目前沒有行程' }}</strong>
          <span>可以切換日期查看其他行程。</span>
        </div>
      </section>

      <aside v-if="selectedEvent && !compactViewport" class="calendar-event-detail omt-detail-panel" aria-label="行程詳細資料">
        <CalendarEventDetail
          :event="selectedEvent"
          :related-tasks="selectedEventTasks"
          :editor-open="props.eventEditorOpen"
          :editing-draft="props.editingDraft || props.eventEditor"
          :editor-busy="props.eventBusy"
          :editor-error="props.eventError"
          @close="closeEventDetail"
          @edit="emit('edit-event', $event)"
          @cancel-event-edit="emit('cancel-event-edit')"
          @save-event="emit('save-event', $event)"
          @delete="emit('delete-event', $event)"
        />
      </aside>
    </section>

    <md-dialog
      v-if="selectedEvent && compactViewport"
      ref="detailDialog"
      class="omt-mobile-detail-dialog"
      :class="{ 'omt-mobile-detail-dialog--editor': props.eventEditorOpen, 'omt-mobile-detail-dialog--expanded': mobileSheetExpanded, 'omt-mobile-detail-dialog--dragging': mobileSheetDragStartY !== null }"
      :style="{ '--omt-sheet-drag-offset': `${mobileSheetDragOffset}px` }"
      :open="true"
      aria-label="行程詳細資料"
      @cancel="closeEventDetail"
      @closed="closeEventDetail"
    >
      <div slot="headline"><span class="sr-only">行程詳細資料</span></div>
      <div slot="content" class="omt-mobile-sheet-content">
        <button
          v-if="!props.eventEditorOpen"
          class="omt-mobile-sheet-handle"
          type="button"
          :aria-label="mobileSheetExpanded ? '向下滑可縮小行程面板' : '向上滑可展開行程面板'"
          @pointerdown="beginMobileSheetDrag"
          @pointermove="moveMobileSheetDrag"
          @pointerup="endMobileSheetDrag"
          @pointercancel="cancelMobileSheetDrag"
        ><span aria-hidden="true"></span></button>
        <CalendarEventDetail
          :event="selectedEvent"
          :related-tasks="selectedEventTasks"
          :editor-open="props.eventEditorOpen"
          :editing-draft="props.editingDraft || props.eventEditor"
          :editor-busy="props.eventBusy"
          :editor-error="props.eventError"
          @close="closeEventDetail"
          @edit="emit('edit-event', $event)"
          @cancel-event-edit="emit('cancel-event-edit')"
          @save-event="emit('save-event', $event)"
          @delete="emit('delete-event', $event)"
        />
      </div>
    </md-dialog>
  </div>
</template>
