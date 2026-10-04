<script setup>
import { computed } from 'vue';
import CaptureComposer from '../components/CaptureComposer.vue';
import EventRow from '../components/EventRow.vue';
import TaskRow from '../components/TaskRow.vue';

const props = defineProps({
  runtimeNow: { type: Date, default: () => new Date() },
  eventFeedback: { type: String, default: '' },
  nextEvent: { type: Object, default: null },
  upcomingEvents: { type: Array, default: () => [] },
  todayEventCount: { type: Number, default: 0 },
  todayTasks: { type: Array, default: () => [] },
  overdueTasks: { type: Array, default: () => [] },
  taskBusy: { type: Boolean, default: false },
  taskError: { type: String, default: '' },
  dashboardLoaded: { type: Boolean, default: false },
  dashboardLoading: { type: Boolean, default: false },
  dashboardError: { type: String, default: '' },
  overviewCalendarError: { type: String, default: '' },
  form: { type: Object, default: () => ({ body: '', attachments: [] }) },
  capturePolicy: { type: Object, default: () => ({}) },
  captureBusy: { type: Boolean, default: false },
  captureError: { type: String, default: '' },
  guestMode: { type: Boolean, default: false },
});
const emit = defineEmits([
  'go-calendar',
  'go-lists',
  'toggle-task',
  'event-info',
  'capture-update',
  'capture-files',
  'capture-remove',
  'capture-submit',
]);

const todayDateLabel = computed(() => `${props.runtimeNow.getMonth() + 1} 月 ${props.runtimeNow.getDate()} 日`);
const todayWeekdayLabel = computed(() => new Intl.DateTimeFormat('zh-Hant-TW', { weekday: 'long' }).format(props.runtimeNow));
const upcomingEvents = computed(() => props.upcomingEvents.length ? props.upcomingEvents : (props.nextEvent ? [props.nextEvent] : []));
const todayTasks = computed(() => [...props.overdueTasks, ...props.todayTasks]);
</script>

<template>
  <div class="workspace-route route-today route-overview omt-page">
    <header class="today-header omt-page-header">
      <div class="today-heading-copy">
        <h1><time>{{ todayDateLabel }}</time></h1>
        <p class="omt-date-line">{{ todayWeekdayLabel }}</p>
      </div>
    </header>

    <p v-if="props.eventFeedback" class="event-feedback" role="status">{{ props.eventFeedback }}</p>

    <section class="today-capture omt-capture-panel" aria-labelledby="today-capture-title">
      <h2 id="today-capture-title">有什麼要記的？</h2>
      <CaptureComposer
        :text="props.form.body"
        :attachments="props.form.attachments"
        :policy="props.capturePolicy"
        :busy="props.captureBusy"
        :submit-disabled="false"
        :error="props.captureError"
        :disabled="props.guestMode"
        :disabled-reason="props.guestMode ? '登入後即可使用 AI 整理文字、圖片與 PDF。展示帳號僅供參考。' : ''"
        inline-submit
        @update:text="emit('capture-update', $event)"
        @add-files="emit('capture-files', $event)"
        @remove-attachment="emit('capture-remove', $event)"
        @submit="emit('capture-submit')"
      />
    </section>

    <div class="today-content-grid omt-home-columns" :class="{ 'has-no-primary-content': !todayTasks.length && !props.taskError }">
      <section class="today-section today-agenda omt-list-section" aria-labelledby="upcoming-heading" :aria-busy="props.dashboardLoading">
        <div class="today-section-heading omt-section-heading">
          <h2 id="upcoming-heading">接下來</h2>
          <md-button color="text" size="small" class="omt-section-heading-link" @click="emit('go-calendar')">查看行事曆<AppIcon slot="icon" name="arrow_forward" /></md-button>
        </div>

        <p v-if="props.dashboardLoading && !props.dashboardLoaded" class="omt-inline-state" role="status">載入行程中…</p>
        <div v-else-if="props.overviewCalendarError" class="omt-inline-error" role="alert">
          <span>{{ props.overviewCalendarError }}</span>
          <md-button color="text" size="small" @click="emit('go-calendar')">開啟行事曆</md-button>
        </div>
        <div v-else-if="upcomingEvents.length" class="omt-row-list">
          <EventRow v-for="event in upcomingEvents" :key="event.id" :event="event" />
        </div>
        <p v-else-if="props.dashboardLoaded" class="omt-inline-state">目前沒有接下來的行程</p>
      </section>

      <section class="today-section today-tasks omt-list-section" aria-labelledby="today-tasks-title">
        <div class="today-section-heading omt-section-heading">
          <h2 id="today-tasks-title">待辦</h2>
          <md-button color="text" size="small" class="omt-section-heading-link" @click="emit('go-lists')">查看全部<AppIcon slot="icon" name="arrow_forward" /></md-button>
        </div>
        <p v-if="props.taskError" class="omt-inline-error" role="alert">{{ props.taskError }}</p>
        <p v-if="props.dashboardLoading && !props.dashboardLoaded && !todayTasks.length" class="omt-inline-state" role="status">載入待辦中…</p>
        <div v-else-if="todayTasks.length" class="omt-row-list">
          <TaskRow v-for="task in todayTasks" :key="task.id || task.title" :task="task" :busy="props.taskBusy" @toggle="emit('toggle-task', $event)" />
        </div>
        <p v-else-if="props.dashboardLoaded && !props.taskError" class="omt-inline-state">今天目前沒有待辦</p>
      </section>
    </div>

  </div>
</template>
