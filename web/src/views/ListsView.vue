<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import TaskDetail from '../components/TaskDetail.vue';
import TaskRow from '../components/TaskRow.vue';

const props = defineProps({
  activeFilter: { type: String, default: 'all' },
  lists: { type: Array, default: () => [] },
  calendarEvents: { type: Array, default: () => [] },
  eventSearchResults: { type: Array, default: () => [] },
  eventSearchLoading: { type: Boolean, default: false },
  selectedListId: { type: String, default: '' },
  initialDataUnresolved: { type: Boolean, default: false },
  listsLoading: { type: Boolean, default: false },
  listsLoadError: { type: String, default: '' },
  taskListLoading: { type: Boolean, default: false },
  taskListError: { type: String, default: '' },
  listBusy: { type: Boolean, default: false },
  listError: { type: String, default: '' },
  openTaskCount: { type: Number, default: 0 },
  taskBusy: { type: Boolean, default: false },
  taskError: { type: String, default: '' },
  sourceError: { type: String, default: '' },
  sourceErrorId: { type: String, default: '' },
  sourceLoadingId: { type: String, default: '' },
  visibleTasks: { type: Array, default: () => [] },
});
const emit = defineEmits([
  'filter', 'select-list', 'create-list', 'delete-list', 'assign-task-list', 'link-task-event', 'search-task-events', 'update-task-schedule',
  'toggle-task', 'delete-task', 'view-source', 'retry-source', 'retry-lists', 'retry-task-list',
]);
const newListName = ref('');
const selectedTaskId = ref('');
const compactViewport = ref(false);
const detailDialog = ref(null);
const returnFocusTarget = ref(null);
let viewportQuery = null;
const filters = [
  { key: 'all', label: '全部' },
  { key: 'today', label: '今天' },
  { key: 'done', label: '已完成' },
];
const selectedList = computed(() => props.lists.find((list) => list.id === props.selectedListId));
const selectedTask = computed(() => props.visibleTasks.find((task) => String(task.id) === selectedTaskId.value) || null);
const openTasks = computed(() => props.visibleTasks.filter((task) => task.status !== 'done'));
const completedTasks = computed(() => props.visibleTasks.filter((task) => task.status === 'done'));

watch(() => props.listBusy, (busy, wasBusy) => {
  if (wasBusy && !busy && !props.listError) newListName.value = '';
});
watch(() => props.visibleTasks, (tasks) => {
  if (selectedTaskId.value && !tasks.some((task) => String(task.id) === selectedTaskId.value)) selectedTaskId.value = '';
}, { deep: false });
watch([selectedTask, compactViewport], async ([task, compact]) => {
  if (!task || !compact) return;
  await nextTick();
  detailDialog.value?.show?.();
});

function submitList() {
  const name = newListName.value.trim();
  if (!name || props.listBusy) return;
  emit('create-list', name);
}

function selectTask(task) {
  const nextId = String(task.id);
  if (selectedTaskId.value === nextId) return;
  returnFocusTarget.value = document.activeElement;
  selectedTaskId.value = nextId;
}

function closeTaskDetail() {
  selectedTaskId.value = '';
  nextTick(() => {
    if (returnFocusTarget.value?.isConnected) returnFocusTarget.value.focus();
    returnFocusTarget.value = null;
  });
}

function updateCompactViewport() {
  compactViewport.value = Boolean(viewportQuery?.matches);
}

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
  <div class="workspace-route route-lists omt-page">
    <header class="calendar-head omt-page-header todo-page-header">
      <div class="todo-page-heading">
        <h1>待辦</h1>
        <span v-if="!initialDataUnresolved && !listsLoading && !taskListLoading && !listsLoadError && !taskListError" class="todo-count">{{ openTaskCount }} 件未完成</span>
      </div>
    </header>

    <section class="route-list-panel omt-tasks-layout">
      <section class="todo-panel route-list-surface omt-task-list-panel" aria-labelledby="lists-heading">
        <div class="todo-heading" :class="{ 'todo-heading--default': !selectedList }">
          <h2 id="lists-heading" :class="{ 'sr-only': !selectedList }">{{ selectedList?.name || '全部待辦' }}</h2>
        </div>

        <details class="list-management">
          <summary><AppIcon class="list-management__icon" name="folder" /><span>清單</span><strong>{{ selectedList?.name || '全部待辦' }}</strong></summary>
          <div class="list-management__content">
            <div class="list-controls">
              <md-select
                v-if="lists.length"
                id="personal-list-picker"
                class="list-picker"
                color="outlined"
                aria-label="查看清單"
                :value="String(selectedListId || '')"
                :disabled="listsLoading"
                @change="emit('select-list', $event.target.value)"
              >
                <md-select-option value=""><span slot="headline">全部待辦</span></md-select-option>
                <md-select-option v-for="list in lists" :key="list.id" :value="String(list.id)"><span slot="headline">{{ list.name }}</span></md-select-option>
              </md-select>
              <span v-if="listsLoading" class="list-status" role="status">載入清單中…</span>
              <md-button v-if="selectedList" color="text" size="small" class="list-delete-button" :disabled="listBusy" @click="emit('delete-list', { id: selectedList.id, name: selectedList.name })"><AppIcon slot="icon" name="delete" />刪除清單</md-button>
            </div>

            <form class="list-form" @submit.prevent="submitList">
              <label for="new-list-name">新增個人清單</label>
              <div class="form-row">
                <input id="new-list-name" v-model="newListName" maxlength="80" autocomplete="off" placeholder="例如：期中考準備" :disabled="listBusy" />
                <md-button type="submit" color="tonal" size="small" :disabled="listBusy || !newListName.trim()"><AppIcon slot="icon" name="add" />{{ listBusy ? '建立中…' : '建立清單' }}</md-button>
              </div>
            </form>
          </div>
        </details>

        <p v-if="listsLoadError" class="task-feedback omt-inline-error" role="alert">
          <span>{{ listsLoadError }}</span>
          <md-button color="text" size="small" class="task-list-retry" @click="emit('retry-lists')">重試載入清單</md-button>
        </p>
        <p v-if="listError || taskError" class="task-feedback omt-inline-error" role="alert">{{ listError || taskError }}</p>

        <div class="todo-filter" role="toolbar" aria-label="待辦篩選">
          <md-button
            v-for="filter in filters"
            :key="filter.key"
            class="filter"
            :class="{ active: activeFilter === filter.key }"
            color="text"
            size="small"
            :aria-pressed="activeFilter === filter.key"
            @click="emit('filter', filter.key)"
          >{{ filter.label }}</md-button>
        </div>

        <div class="task-list omt-task-list" :aria-busy="taskListLoading">
          <p v-if="taskListLoading" class="list-status task-list-feedback" role="status">載入待辦中…</p>
          <p v-if="taskListError" class="task-feedback task-list-feedback omt-inline-error" role="alert">
            <span>{{ taskListError }}</span>
            <md-button color="text" size="small" class="task-list-retry" @click="emit('retry-task-list')">重試載入待辦</md-button>
          </p>

          <Transition name="todo-filter" mode="out-in">
            <div :key="activeFilter" class="todo-filter-content">
              <section v-if="openTasks.length" v-show="activeFilter !== 'all'" class="omt-task-group" aria-label="未完成的待辦">
                <TaskRow v-for="task in openTasks" :key="task.id" :task="task" :busy="taskBusy" selectable :selected="String(task.id) === selectedTaskId" @toggle="emit('toggle-task', $event)" @select="selectTask" />
              </section>
              <section v-if="activeFilter === 'all' && visibleTasks.length" class="omt-task-group" aria-label="Tasks">
                <TaskRow v-for="task in visibleTasks" :key="task.id" :task="task" :busy="taskBusy" selectable :selected="String(task.id) === selectedTaskId" @toggle="emit('toggle-task', $event)" @select="selectTask" />
              </section>
              <details v-if="activeFilter !== 'all' && completedTasks.length" class="omt-completed-group" :open="activeFilter === 'done'">
                <summary>已完成 <span>{{ completedTasks.length }}</span></summary>
                <section class="omt-task-group" aria-label="已完成的待辦">
                  <TaskRow v-for="task in completedTasks" :key="task.id" :task="task" :busy="taskBusy" selectable :selected="String(task.id) === selectedTaskId" @toggle="emit('toggle-task', $event)" @select="selectTask" />
                </section>
              </details>

              <div v-if="!visibleTasks.length && !initialDataUnresolved && !listsLoading && !listsLoadError && !taskListLoading && !taskListError" class="list-empty-state omt-empty-state">
                <strong v-if="activeFilter === 'today'">今天沒有待辦</strong>
                <strong v-else-if="activeFilter === 'done'">目前沒有已完成待辦</strong>
                <strong v-else>{{ selectedList ? `「${selectedList.name}」目前沒有符合條件的待辦` : lists.length ? '目前沒有符合條件的待辦' : '還沒有個人清單或待辦' }}</strong>
                <span v-if="activeFilter !== 'all'">切換到「全部」可查看其他待辦。</span>
                <span v-else>{{ selectedList ? '從今天頁面整理出的待辦會顯示在這裡。' : '從今天頁面的整理流程整理待辦，或建立個人清單後，內容會顯示在這裡。' }}</span>
                <md-button v-if="activeFilter !== 'all'" color="text" size="medium" @click="emit('filter', 'all')">查看全部待辦</md-button>
              </div>
            </div>
          </Transition>
        </div>
      </section>

      <aside v-if="selectedTask && !compactViewport" class="omt-detail-panel omt-task-detail-panel" aria-label="待辦詳細資料">
        <TaskDetail
          :task="selectedTask"
          :lists="props.lists"
          :calendar-events="props.calendarEvents"
          :event-search-results="props.eventSearchResults"
          :event-search-loading="props.eventSearchLoading"
          :busy="props.taskBusy"
          :error="props.taskError"
          @close="closeTaskDetail"
          @toggle="emit('toggle-task', $event)"
          @delete="emit('delete-task', $event)"
          @assign-list="emit('assign-task-list', $event)"
          @link-event="emit('link-task-event', $event)"
          @search-events="emit('search-task-events', $event)"
          @update-time="emit('update-task-schedule', $event)"
        />
      </aside>
    </section>

    <md-dialog v-if="selectedTask && compactViewport" ref="detailDialog" class="omt-mobile-detail-dialog" :open="true" quick aria-label="待辦詳細資料" @cancel="closeTaskDetail" @closed="closeTaskDetail">
      <div slot="headline"><span class="sr-only">待辦詳細資料</span></div>
      <div slot="content">
        <TaskDetail
          :task="selectedTask"
          :lists="props.lists"
          :calendar-events="props.calendarEvents"
          :event-search-results="props.eventSearchResults"
          :event-search-loading="props.eventSearchLoading"
          :busy="props.taskBusy"
          :error="props.taskError"
          @close="closeTaskDetail"
          @toggle="emit('toggle-task', $event)"
          @delete="emit('delete-task', $event)"
          @assign-list="emit('assign-task-list', $event)"
          @link-event="emit('link-task-event', $event)"
          @search-events="emit('search-task-events', $event)"
          @update-time="emit('update-task-schedule', $event)"
        />
      </div>
    </md-dialog>
  </div>
</template>
