<script setup>
import { taskDueCompactLabel } from '../taskPresentation.js';
const props = defineProps({
  task: { type: Object, required: true },
  busy: { type: Boolean, default: false },
  selectable: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
});
const emit = defineEmits(['toggle', 'select']);

const taskHasDue = (task) => Boolean(taskDueCompactLabel(task));
const updateTaskStatus = (event) => {
  emit('toggle', {
    task: props.task,
    status: event.target.checked ? 'done' : 'open',
  });
};
</script>

<template>
  <article
    class="omt-task-row task-item"
    :class="{ done: props.task.status === 'done', overdue: props.task.dueStatus === 'overdue', selected: props.selected }"
  >
    <md-checkbox
      class="md3-task-checkbox"
      touch-target="wrapper"
      :checked="props.task.status === 'done'"
      :disabled="props.busy"
      :aria-label="`${props.task.status === 'done' ? '重新開啟' : '完成'}：${props.task.title}`"
      @change="updateTaskStatus"
    ></md-checkbox>
    <button
      v-if="props.selectable"
      type="button"
      class="omt-task-row__main"
      :aria-pressed="props.selected"
      @click="emit('select', props.task)"
    >
      <strong class="omt-task-row__title">{{ props.task.title }}</strong>
      <span v-if="taskHasDue(props.task)" class="omt-task-row__due">{{ taskDueCompactLabel(props.task) }}</span>
    </button>
    <div v-else class="omt-task-row__main">
      <strong class="omt-task-row__title">{{ props.task.title }}</strong>
      <span v-if="taskHasDue(props.task)" class="omt-task-row__due">{{ taskDueCompactLabel(props.task) }}</span>
    </div>
  </article>
</template>
