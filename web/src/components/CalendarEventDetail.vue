<script setup>
import EventEditorDialog from './EventEditorDialog.vue';

const props = defineProps({
  event: { type: Object, required: true },
  relatedTasks: { type: Array, default: () => [] },
  editorOpen: { type: Boolean, default: false },
  editorEvent: { type: Object, default: null },
  editingDraft: { type: Object, default: null },
  editorBusy: { type: Boolean, default: false },
  editorError: { type: String, default: '' },
});
const emit = defineEmits(['close', 'edit', 'delete', 'cancel-event-edit', 'save-event']);
</script>

<template>
  <div v-if="!props.editorOpen" class="omt-detail-content">
    <header class="omt-detail-heading">
      <div class="omt-detail-heading-copy">
        <span class="omt-detail-eyebrow">選取的行程</span>
        <h2>{{ props.event.title || '未命名行程' }}</h2>
      </div>
      <md-icon-button class="omt-detail-close" aria-label="關閉行程詳細資料" @click="emit('close')"><AppIcon name="close" /></md-icon-button>
    </header>

    <dl class="omt-detail-facts">
      <div><dt>日期與時間</dt><dd>{{ props.event.dateLabel || '日期待確認' }}<span v-if="props.event.timeLabel"> · {{ props.event.timeLabel }}</span></dd></div>
      <div v-if="props.event.location"><dt>地點</dt><dd>{{ props.event.location }}</dd></div>
    </dl>

    <section v-if="props.event.description" class="omt-detail-note" aria-label="行程備註">
      <p>{{ props.event.description }}</p>
    </section>

    <section v-if="props.relatedTasks.length" class="omt-detail-related-tasks" aria-labelledby="related-tasks-heading">
      <h3 id="related-tasks-heading">相關待辦</h3>
      <ul>
        <li v-for="task in props.relatedTasks" :key="task.id">
          <span>{{ task.title }}</span>
          <small>{{ task.status === 'done' ? '已完成' : '未完成' }}</small>
        </li>
      </ul>
    </section>

    <footer class="omt-detail-actions">
      <md-button color="tonal" size="medium" class="omt-detail-primary-action" @click="emit('edit', props.event)">編輯行程</md-button>
      <md-button color="text" size="medium" class="omt-detail-danger-action" @click="emit('delete', props.event)">刪除行程</md-button>
    </footer>
  </div>
  <div v-else class="omt-detail-content omt-detail-editing">
    <EventEditorDialog
      :open="props.editorOpen"
      :embedded="true"
      :event="props.editingDraft || props.editorEvent || props.event"
      :busy="props.editorBusy"
      :error="props.editorError"
      @close="emit('cancel-event-edit')"
      @save="emit('save-event', $event)"
      @delete="emit('delete', props.editingDraft || props.editorEvent || props.event)"
    />
  </div>
</template>
