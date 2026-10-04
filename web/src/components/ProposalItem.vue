<script setup>
import { computed, ref } from 'vue';

const props = defineProps({
  proposal: { type: Object, required: true },
  busy: { type: Boolean, default: false },
  showView: { type: Boolean, default: false },
  showApply: { type: Boolean, default: false },
  reviewMode: { type: Boolean, default: false },
});
const emit = defineEmits(['view', 'accept', 'reject', 'save', 'select-target', 'apply']);
const editing = ref(false);
const expanded = ref(false);
const draft = ref({});
const unknownValue = '尚未確認';

const isEvent = computed(() => props.proposal.target_type === 'event');
const isUpdate = computed(() => props.proposal.operation === 'update');
const actionLabel = computed(() => {
  if (props.reviewMode) {
    return isEvent.value
      ? (isUpdate.value ? '建議修改行程' : '建議新增行程')
      : (isUpdate.value ? '建議修改待辦' : '建議新增待辦');
  }
  return isEvent.value
    ? (isUpdate.value ? '修改行程' : '新增行程')
    : (isUpdate.value ? '修改待辦' : '新增待辦');
});
const fallbackTitle = computed(() => isEvent.value ? '未命名行程' : '未命名待辦');
const displayTitle = computed(() => props.proposal.patch?.title || props.proposal.target_title || (props.reviewMode ? unknownValue : fallbackTitle.value));
const location = computed(() => props.proposal.patch?.location || props.proposal.target_location || unknownValue);
const eventDate = computed(() => props.proposal.patch?.date || props.proposal.patch?.start_at?.slice?.(0, 10) || props.proposal.target_date || unknownValue);
const eventTime = computed(() => {
  const patch = props.proposal.patch || {};
  if (patch.all_day === true) return '全天';
  if (patch.time) return patch.time;
  if (patch.start_at && patch.end_at) return `${patch.start_at.slice(11, 16)}～${patch.end_at.slice(11, 16)}`;
  if (patch.start_at) return patch.start_at.slice(11, 16);
  return props.proposal.target_time || unknownValue;
});
const taskDueExplicitlyUncertain = computed(() => !isEvent.value
  && props.proposal.needs_review
  && /(期限|截止日期|日期)/.test(props.proposal.review_reason || ''));
const due = computed(() => {
  const patch = props.proposal.patch || {};
  if (patch.due) return patch.due;
  if (patch.due_label) return patch.due_label;
  return taskDueExplicitlyUncertain.value ? '尚未確認' : '';
});
function compactDateLabel(value) {
  const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return match ? `${Number(match[2])}/${Number(match[3])}` : value;
}
const compactEventDate = computed(() => compactDateLabel(eventDate.value));
const compactDue = computed(() => compactDateLabel(due.value));
const recurrenceLabel = computed(() => {
  const recurrence = props.proposal.patch?.recurrence;
  if (!recurrence) return '';
  const names = ['一', '二', '三', '四', '五', '六', '日'];
  const weekdays = (recurrence.weekdays || []).map((day) => `週${names[day - 1]}`).join('、');
  return `每週${weekdays} · ${recurrence.start_date} 至 ${recurrence.end_date}`;
});
const eventScheduleComplete = computed(() => {
  const patch = props.proposal.patch || {};
  const changesSchedule = ['date', 'time', 'start_at', 'end_at', 'all_day'].some((key) => Object.hasOwn(patch, key));
  if (props.proposal.operation === 'update' && !changesSchedule) return true;
  const times = String(patch.time || props.proposal.target_time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || [];
  const hasStart = Boolean(patch.start_at) || times.length >= 1;
  return eventDate.value !== unknownValue && (patch.all_day === true || hasStart);
});
const taskDueUnresolved = computed(() => {
  const patch = props.proposal.patch || {};
  return taskDueExplicitlyUncertain.value && !patch.due;
});
const typeSelectionNeeded = computed(() => props.reviewMode
  && props.proposal.operation === 'create'
  && props.proposal.needs_review
  && ['event', 'task'].includes(props.proposal.target_type));
const missingFields = computed(() => {
  if (!isEvent.value) return [];
  if (props.proposal.operation === 'update' && !['date', 'time', 'start_at', 'end_at', 'all_day'].some((key) => Object.hasOwn(props.proposal.patch || {}, key))) return [];
  return [
    ['活動名稱', displayTitle.value],
    ['日期', eventDate.value],
    ['開始時間或全天選項', eventScheduleComplete.value ? '已確認' : unknownValue],
  ].filter(([, value]) => value === unknownValue).map(([label]) => `${label}尚未確認`);
});
const hasUncertainty = computed(() => Boolean(props.proposal.needs_review || missingFields.value.length));
const canSupplement = computed(() => taskDueUnresolved.value || missingFields.value.length > 0);
const canAccept = computed(() => !props.proposal.needs_review && (!isEvent.value || eventScheduleComplete.value) && (!isUpdate.value || Boolean(props.proposal.target_id)) && ['pending', 'edited'].includes(props.proposal.status));
const draftReady = computed(() => Boolean(draft.value.title?.trim()) && (!taskDueUnresolved.value || Boolean(draft.value.due)) && (!isEvent.value || (draft.value.date && (draft.value.all_day || (String(draft.value.time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || []).length >= 1) && (!draft.value.repeat_weekly || (draft.value.recurrence_weekdays?.length && draft.value.recurrence_start && draft.value.recurrence_end)))));
const weekdayOptions = [{ day: 1, label: '一' }, { day: 2, label: '二' }, { day: 3, label: '三' }, { day: 4, label: '四' }, { day: 5, label: '五' }, { day: 6, label: '六' }, { day: 7, label: '日' }];

function startEdit() {
  const patch = props.proposal.patch || {};
  const recurrence = patch.recurrence || {};
  draft.value = {
    title: displayTitle.value === unknownValue ? '' : displayTitle.value,
    date: patch.date || patch.start_at?.slice?.(0, 10) || '',
    time: patch.time || (patch.start_at ? (patch.end_at ? `${patch.start_at.slice(11, 16)}～${patch.end_at.slice(11, 16)}` : patch.start_at.slice(11, 16)) : ''),
    location: patch.location || '',
    all_day: patch.all_day === true,
    repeat_weekly: Boolean(patch.recurrence),
    recurrence_weekdays: [...(recurrence.weekdays || [])],
    recurrence_start: recurrence.start_date || patch.date || patch.start_at?.slice?.(0, 10) || '',
    recurrence_end: recurrence.end_date || '',
    ...(isEvent.value ? {} : {
      ...patch,
      due: patch.due || (/^\d{4}-\d{2}-\d{2}$/.test(patch.due_label || '') ? patch.due_label : ''),
    }),
  };
  editing.value = true;
}

function saveEdit() {
  const patch = { ...draft.value };
  const repeatWeekly = patch.repeat_weekly;
  const recurrence = { frequency: 'weekly', weekdays: [...(patch.recurrence_weekdays || [])].sort(), start_date: patch.recurrence_start, end_date: patch.recurrence_end };
  delete patch.repeat_weekly;
  delete patch.recurrence_weekdays;
  delete patch.recurrence_start;
  delete patch.recurrence_end;
  if (isEvent.value) {
    delete patch.start_at;
    delete patch.end_at;
    patch.all_day = Boolean(patch.all_day);
    if (repeatWeekly) patch.recurrence = recurrence;
    else delete patch.recurrence;
  } else {
    patch.due = patch.due || null;
    delete patch.due_label;
  }
  Object.keys(patch).forEach((key) => { if (patch[key] === '') delete patch[key]; });
  if (!props.proposal.patch?.title && props.proposal.target_title && patch.title === props.proposal.target_title) delete patch.title;
  emit('save', { proposal: props.proposal, patch });
  editing.value = false;
}

function changeTargetType(event) {
  const targetType = event.target.value;
  if (!typeSelectionNeeded.value || !['event', 'task'].includes(targetType) || targetType === props.proposal.target_type) return;
  emit('save', { proposal: props.proposal, patch: props.proposal.patch || {}, targetType });
}

function cancelEdit() {
  editing.value = false;
}

function toggleDraftWeekday(day, event) {
  const selected = new Set(draft.value.recurrence_weekdays || []);
  if (event.target.checked) selected.add(day);
  else selected.delete(day);
  draft.value.recurrence_weekdays = [...selected].sort((a, b) => a - b);
}
</script>

<template>
  <article class="proposal-item" :class="`proposal-item-${proposal.status}`">
    <div v-if="!props.reviewMode || hasUncertainty || isUpdate || proposal.status !== 'pending'" class="proposal-item-topline"><span>{{ isEvent ? '行程' : '待辦' }}<template v-if="isUpdate"> · 修改</template></span><span v-if="proposal.status === 'pending'">待確認</span><span v-else-if="proposal.status === 'edited'">已修改</span><span v-else-if="proposal.status === 'accepted'">已接受，尚未套用</span><span v-else-if="proposal.status === 'rejected'">已忽略</span><span v-else-if="proposal.status === 'applied'">已套用</span></div>

    <div v-if="props.reviewMode && !expanded && !hasUncertainty" class="proposal-collapsed-summary">
      <div class="proposal-collapsed-main" :class="isEvent ? 'proposal-collapsed-main--event' : 'proposal-collapsed-main--task'">
        <span v-if="isEvent" class="proposal-summary-time">{{ eventTime }}</span>
        <span v-else class="proposal-summary-task-marker" aria-hidden="true"></span>
        <div class="proposal-summary-copy">
          <h3>{{ displayTitle }}</h3>
          <p v-if="isEvent"><span v-if="compactEventDate !== unknownValue">{{ compactEventDate }}</span><template v-if="compactEventDate !== unknownValue && location !== unknownValue"> · </template><span v-if="location !== unknownValue">{{ location }}</span></p>
          <p v-else-if="due">{{ due === unknownValue ? '期限待確認' : compactDue }}</p>
        </div>
      </div>
      <div class="proposal-collapsed-actions">
        <md-button color="text" size="small" class="proposal-text-action" :aria-expanded="expanded" @click="expanded = true">詳細資訊</md-button>
        <md-button v-if="['pending', 'edited'].includes(proposal.status)" color="text" size="small" class="proposal-text-action proposal-row-ignore" :disabled="busy" @click="emit('reject', proposal)">忽略</md-button>
      </div>
    </div>

    <template v-else-if="!editing">
      <h3>{{ displayTitle }}</h3>
      <dl class="proposal-facts">
        <template v-if="isEvent">
          <div><dt>時間</dt><dd>{{ eventDate }} · {{ eventTime }}<template v-if="location !== unknownValue"> · {{ location }}</template></dd></div>
          <div v-if="recurrenceLabel"><dt>重複</dt><dd>{{ recurrenceLabel }}</dd></div>
        </template>
        <div v-else-if="due"><dt>期限</dt><dd>{{ due === unknownValue ? '期限待確認' : due }}</dd></div>
      </dl>

      <div v-if="props.reviewMode && (props.proposal.needs_review || missingFields.length)" class="proposal-review-note">
        <strong>待確認</strong>
        <span v-if="missingFields.length">{{ missingFields.join('、') }}</span>
        <span v-else>{{ proposal.review_reason || '請確認這筆整理結果。' }}</span>
      </div>
      <p v-else-if="!props.reviewMode && proposal.needs_review" class="proposal-review-note">這筆內容需要你選擇要修改的行程。</p>

      <md-select
        v-if="typeSelectionNeeded"
        class="proposal-target-select"
        color="outlined"
        label="加入為"
        :value="proposal.target_type"
        :disabled="busy"
        @change="changeTargetType"
      >
        <md-select-option value="event"><span slot="headline">行程（可設為全天）</span></md-select-option>
        <md-select-option value="task"><span slot="headline">待辦（可設為全天期限）</span></md-select-option>
      </md-select>
      <md-select
        v-if="!props.reviewMode && proposal.needs_review && proposal.target_candidates?.length"
        class="proposal-target-select"
        color="outlined"
        label="要修改的行程"
        :value="String(proposal.target_id || '')"
        @change="emit('select-target', { proposal, targetId: $event.target.value || null })"
      >
        <md-select-option value=""><span slot="headline">請選擇</span></md-select-option>
        <md-select-option
          v-for="candidate in proposal.target_candidates"
          :key="candidate.id"
          :value="String(candidate.id)"
        ><span slot="headline">{{ `${candidate.title} · ${candidate.start || '時間待確認'}` }}</span></md-select-option>
      </md-select>
      <md-select
        v-if="props.reviewMode && proposal.needs_review && proposal.target_candidates?.length"
        class="proposal-target-select"
        color="outlined"
        label="請選擇要更新的行程"
        :value="String(proposal.target_id || '')"
        @change="emit('select-target', { proposal, targetId: $event.target.value || null })"
      >
        <md-select-option value=""><span slot="headline">尚未確認</span></md-select-option>
        <md-select-option
          v-for="candidate in proposal.target_candidates"
          :key="candidate.id"
          :value="String(candidate.id)"
        ><span slot="headline">{{ `${candidate.title} · ${candidate.start || unknownValue}` }}</span></md-select-option>
      </md-select>
    </template>

    <div v-else class="proposal-edit-fields">
      <label v-if="isEvent && missingFields.includes('活動名稱尚未確認')">活動名稱<capture-text-field color="outlined" type="text" :value="draft.title" aria-label="活動名稱" @input="draft.title = $event.target.value"></capture-text-field></label>
      <template v-if="isEvent">
        <label v-if="missingFields.includes('日期尚未確認')">日期<capture-text-field color="outlined" type="date" :value="draft.date" aria-label="日期" @input="draft.date = $event.target.value"></capture-text-field></label>
        <label v-if="missingFields.includes('開始時間或全天選項尚未確認') && !draft.all_day">時間<capture-text-field color="outlined" type="text" :value="draft.time" placeholder="例如 10:15 或 10:15～11:05" aria-label="時間，可只填開始時間" @input="draft.time = $event.target.value"></capture-text-field></label>
        <label v-if="missingFields.includes('開始時間或全天選項尚未確認')" class="proposal-all-day-option"><md-checkbox touch-target="wrapper" :checked="draft.all_day" aria-label="全天行程" @change="draft.all_day = $event.target.checked"></md-checkbox><span>全天行程</span></label>
        <label v-if="draft.repeat_weekly && missingFields.includes('開始時間或全天選項尚未確認')">學期開始<capture-text-field color="outlined" type="date" :value="draft.recurrence_start" aria-label="學期開始" @input="draft.recurrence_start = $event.target.value"></capture-text-field></label>
        <label v-if="draft.repeat_weekly && missingFields.includes('開始時間或全天選項尚未確認')">學期結束<capture-text-field color="outlined" type="date" :value="draft.recurrence_end" aria-label="學期結束" @input="draft.recurrence_end = $event.target.value"></capture-text-field></label>
        <fieldset v-if="draft.repeat_weekly && missingFields.includes('開始時間或全天選項尚未確認')" class="proposal-weekday-field">
          <legend>上課日</legend>
          <label v-for="option in weekdayOptions" :key="option.day"><md-checkbox touch-target="wrapper" :checked="draft.recurrence_weekdays.includes(option.day)" :aria-label="`週${option.label}`" @change="toggleDraftWeekday(option.day, $event)"></md-checkbox><span>{{ option.label }}</span></label>
        </fieldset>
      </template>
      <label v-else-if="taskDueUnresolved">截止日期
        <capture-text-field color="outlined" type="date" :value="draft.due" aria-label="截止日期" aria-describedby="proposal-due-hint" @input="draft.due = $event.target.value"></capture-text-field>
        <span id="proposal-due-hint" class="proposal-field-hint">
          {{ draft.due ? `已選擇 ${draft.due}` : proposal.needs_review ? '尚未確認；選擇日期後再儲存' : '目前沒有截止日期' }}
        </span>
      </label>
    </div>

    <div v-if="!props.reviewMode || expanded || hasUncertainty" class="proposal-item-actions">
      <md-button v-if="props.showView" color="text" size="small" class="proposal-text-action" :disabled="busy" @click="emit('view', proposal)">查看</md-button>
      <template v-if="props.reviewMode">
        <template v-if="editing">
          <md-button color="text" size="small" class="proposal-text-action" :disabled="busy" @click="cancelEdit">取消</md-button>
          <md-button color="filled" size="small" class="proposal-primary-action" :disabled="busy || !draftReady" @click="saveEdit">儲存修改</md-button>
        </template>
        <template v-else-if="['pending', 'edited'].includes(proposal.status)">
          <md-button v-if="canSupplement" color="text" size="small" class="proposal-text-action" :disabled="busy" @click="startEdit">補充資訊</md-button>
          <md-button color="text" size="small" class="proposal-text-action" :disabled="busy" @click="emit('reject', proposal)">忽略</md-button>
        </template>
        <md-button v-else-if="proposal.status === 'accepted' && proposal.operation === 'ignore'" color="text" size="small" class="proposal-text-action" :disabled="busy" @click="emit('apply', proposal)">完成忽略</md-button>
        <md-button v-else-if="props.showApply && proposal.status === 'accepted'" color="filled" size="small" class="proposal-primary-action" :disabled="busy" @click="emit('apply', proposal)">套用變更</md-button>
      </template>
      <template v-else>
        <template v-if="editing">
          <md-button color="text" size="small" class="proposal-text-action" :disabled="busy" @click="cancelEdit">取消</md-button>
          <md-button color="filled" size="small" class="proposal-primary-action" :disabled="busy || !draftReady" @click="saveEdit">儲存修改</md-button>
        </template>
        <template v-else-if="['pending', 'edited'].includes(proposal.status)">
          <md-button color="text" size="small" class="proposal-text-action" :disabled="busy" @click="emit('reject', proposal)">忽略</md-button>
          <md-button v-if="canSupplement" color="text" size="small" class="proposal-text-action" :disabled="busy" @click="startEdit">補充資訊</md-button>
          <md-button color="filled" size="small" class="proposal-primary-action" :disabled="busy || !canAccept" @click="emit('accept', proposal)">接受</md-button>
        </template>
        <md-button v-else-if="props.showApply && proposal.status === 'accepted'" color="filled" size="small" class="proposal-primary-action" :disabled="busy" @click="emit('apply', proposal)">套用變更</md-button>
      </template>
    </div>
  </article>
</template>
