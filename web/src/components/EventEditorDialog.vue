<script setup>
import { computed, reactive, watch } from 'vue';

const props = defineProps({
  open: { type: Boolean, default: false },
  embedded: { type: Boolean, default: false },
  event: { type: Object, default: null },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
});

const emit = defineEmits(['close', 'save', 'delete']);
const draft = reactive({
  title: '',
  date_label: '',
  start_time: '',
  end_time: '',
  location: '',
  description: '',
  allDay: false,
  timezone: '',
});

function clockFromIso(value) {
  const text = String(value || '');
  const match = text.match(/T(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : '';
}

function clocksFromLabel(value) {
  return [...String(value || '').matchAll(/(?<!\d)(\d{1,2}):(\d{2})(?!\d)/g)]
    .slice(0, 2)
    .map((match) => `${String(Number(match[1])).padStart(2, '0')}:${match[2]}`);
}

function eventDate(event) {
  const start = String(event?.start_at || '');
  if (/^\d{4}-\d{2}-\d{2}/.test(start)) return start.slice(0, 10);
  const candidate = String(event?.date_iso || event?.date_label || '');
  const match = candidate.match(/(\d{4})[-/](\d{2})[-/](\d{2})/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : '';
}

function syncDraft(event) {
  const allDay = event?.all_day === true || event?.allDay === true || String(event?.timeLabel || event?.time_label || '').trim() === '全天';
  const labelClocks = clocksFromLabel(event?.timeLabel || event?.time_label);
  Object.assign(draft, {
    title: event?.title || '',
    date_label: eventDate(event),
    start_time: allDay ? '' : (clockFromIso(event?.start_at) || labelClocks[0] || ''),
    end_time: allDay ? '' : (clockFromIso(event?.end_at) || labelClocks[1] || ''),
    location: event?.location || '',
    description: event?.description || event?.detail || '',
    allDay,
    timezone: event?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Taipei',
  });
}

watch(() => props.event, syncDraft, { immediate: true });
watch(() => draft.allDay, (allDay) => {
  if (allDay) {
    draft.start_time = '';
    draft.end_time = '';
  }
});

const timeError = computed(() => {
  if (draft.allDay || !draft.start_time || !draft.end_time) return '';
  return draft.end_time <= draft.start_time ? '結束時間必須晚於開始時間。' : '';
});
const canSave = computed(() => Boolean(
  !props.busy
  && draft.title.trim()
  && draft.date_label
  && (draft.allDay || draft.start_time)
  && !timeError.value
));

function close() {
  if (!props.busy) emit('close');
}

function save() {
  if (!canSave.value) return;
  const timeLabel = draft.allDay
    ? '全天'
    : draft.end_time
      ? `${draft.start_time}～${draft.end_time}`
      : draft.start_time;
  emit('save', {
    title: draft.title.trim(),
    date_label: draft.date_label,
    time_label: timeLabel,
    all_day: draft.allDay,
    timezone: draft.timezone || null,
    location: draft.location.trim() || null,
    detail: draft.description.trim(),
  });
}
</script>

<template>
  <component :is="props.embedded ? 'section' : 'md-dialog'" v-if="props.open" :class="{ 'event-editor-dialog': !props.embedded, 'event-editor-embedded': props.embedded }" :open="props.open" quick @cancel="close" @closed="close">
    <div slot="headline" class="event-editor-heading">
      <h2>編輯行程</h2>
      <md-icon-button class="event-editor-close" aria-label="關閉" @click="close"><AppIcon name="close" /></md-icon-button>
    </div>
    <div slot="content" class="event-editor-content">
      <p v-if="props.error" class="event-editor-error" role="alert">{{ props.error }}</p>
      <p v-if="props.event?.recurrence || props.event?.recurrence_rule" class="event-series-notice">這是重複行程；目前編輯會保留原本的重複規則。</p>
      <div class="event-editor-fields">
        <label class="event-editor-field event-editor-field--wide event-editor-field--name">
          <span>名稱</span>
          <input v-model="draft.title" class="event-editor-control" type="text" maxlength="500" autocomplete="off" :disabled="props.busy" />
        </label>

        <div class="event-editor-schedule" :class="{ 'event-editor-schedule--all-day': draft.allDay }">
          <label class="event-editor-field event-editor-field--date">
            <span>日期</span>
            <input v-model="draft.date_label" class="event-editor-control" type="date" :disabled="props.busy" />
          </label>

          <label v-if="!draft.allDay" class="event-editor-field event-editor-field--start-time">
            <span>開始時間</span>
            <input v-model="draft.start_time" class="event-editor-control" type="time" step="60" required :disabled="props.busy" />
          </label>
          <label v-if="!draft.allDay" class="event-editor-field event-editor-field--end-time">
            <span>結束時間（選擇性）</span>
            <input v-model="draft.end_time" class="event-editor-control" type="time" step="60" :disabled="props.busy" />
          </label>
          <p v-if="timeError" class="event-editor-validation" role="alert">{{ timeError }}</p>

          <label class="event-series-toggle"><md-checkbox touch-target="wrapper" :checked="draft.allDay" :disabled="props.busy" aria-label="全天行程" @change="draft.allDay = $event.target.checked"></md-checkbox><span>全天行程</span></label>
        </div>

        <label class="event-editor-field event-editor-field--wide">
          <span>地點</span>
          <input v-model="draft.location" class="event-editor-control" type="text" maxlength="2000" autocomplete="off" :disabled="props.busy" />
        </label>

        <label class="event-editor-field event-editor-field--wide">
          <span>備註</span>
          <textarea v-model="draft.description" class="event-editor-control event-editor-textarea" rows="4" maxlength="10000" :disabled="props.busy"></textarea>
        </label>
      </div>
    </div>
    <div slot="actions" class="event-editor-actions">
      <md-button color="text" size="medium" :disabled="props.busy" @click="close">取消</md-button>
      <md-button color="text" size="medium" class="event-delete-action" :disabled="props.busy" @click="emit('delete')">刪除</md-button>
      <md-button color="filled" size="medium" class="event-editor-save" :disabled="!canSave" @click="save">{{ props.busy ? '儲存中…' : '儲存' }}</md-button>
    </div>
  </component>
</template>

<style scoped>
.event-series-notice { margin: 0; color: var(--omt-on-surface-variant, #4d4650); font-size: 0.8125rem; line-height: 1.55; }
.event-series-toggle { display: flex !important; align-items: center; gap: 8px !important; }
.event-series-toggle md-checkbox { flex: 0 0 auto; }
.event-editor-embedded { display: grid; gap: 8px; min-width: 0; padding-top: 4px; border-top: 0; }
.event-editor-embedded .event-editor-heading { padding: 0 0 8px; }
.event-editor-embedded .event-editor-heading h2 { font-size: 1.125rem; }
.event-editor-embedded .event-editor-content { max-height: none; overflow: visible; padding: 0; }
.event-editor-embedded .event-editor-actions { flex-wrap: wrap; justify-content: flex-end; padding: 10px 0 0; }
.event-editor-schedule { grid-column: 1 / -1; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 12px; align-items: start; min-width: 0; }
.event-editor-schedule--all-day { grid-template-columns: minmax(0, 1fr) max-content; align-items: end; }
.event-editor-schedule--all-day .event-series-toggle { align-self: end; min-height: 48px; padding: 2px 4px 2px 0; }
.event-editor-schedule > .event-series-toggle { grid-column: 1 / -1; }
.event-editor-schedule--all-day > .event-series-toggle { grid-column: 2; }
.event-editor-validation { grid-column: 1 / -1; margin: -4px 0 0; font-size: .76rem; line-height: 1.45; }
.event-editor-validation { color: var(--omt-error, #ba1a1a); }
.event-editor-embedded .event-editor-schedule:not(.event-editor-schedule--all-day) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.event-editor-embedded .event-editor-field--date { grid-column: 1 / -1; }
.event-editor-embedded .event-editor-schedule > .event-series-toggle { grid-column: 1 / -1; }
@media (max-width: 600px) {
  .event-editor-embedded { padding-top: 18px; }
  .event-editor-embedded .event-editor-heading { padding: 4px 0 14px; }
}
@media (max-width: 520px) {
  .event-editor-schedule, .event-editor-schedule--all-day { grid-template-columns: minmax(0, 1fr); }
  .event-editor-field--date, .event-editor-field--start-time, .event-editor-field--end-time,
  .event-editor-embedded .event-editor-field--date { grid-column: 1; }
  .event-editor-schedule--all-day > .event-series-toggle, .event-editor-schedule > .event-series-toggle { grid-column: 1; }
}
</style>
