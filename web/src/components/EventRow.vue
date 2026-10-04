<script setup>
const props = defineProps({
  event: { type: Object, required: true },
  interactive: { type: Boolean, default: false },
  selected: { type: Boolean, default: false },
});
const emit = defineEmits(['select']);
</script>

<template>
  <button v-if="props.interactive" type="button" class="omt-event-row" :class="{ selected: props.selected }" :aria-pressed="props.selected" @click="emit('select', props.event)">
    <time class="omt-event-row__time">{{ props.event.timeLabel || '時間待確認' }}</time>
    <span class="omt-event-row__body">
      <strong class="omt-event-row__title">{{ props.event.title || '未命名行程' }}</strong>
      <span v-if="props.event.location" class="omt-event-row__location">{{ props.event.location }}</span>
    </span>
  </button>
  <article v-else class="omt-event-row">
    <time class="omt-event-row__time">{{ props.event.timeLabel || '時間待確認' }}</time>
    <span class="omt-event-row__body">
      <strong class="omt-event-row__title">{{ props.event.title || '未命名行程' }}</strong>
      <span v-if="props.event.location" class="omt-event-row__location">{{ props.event.location }}</span>
    </span>
  </article>
</template>
