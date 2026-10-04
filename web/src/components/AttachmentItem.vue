<script setup>
import { computed, onBeforeUnmount, ref } from 'vue';

const props = defineProps({
  attachment: { type: Object, required: true },
  disabled: { type: Boolean, default: false },
});
const emit = defineEmits(['remove']);
const previewUrl = ref('');

if (props.attachment.file?.type?.startsWith('image/')) {
  previewUrl.value = URL.createObjectURL(props.attachment.file);
}

onBeforeUnmount(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value);
});

const sizeLabel = computed(() => {
  const size = Number(props.attachment.size || props.attachment.file?.size || 0);
  if (!size) return '';
  if (size < 1024 * 1024) return `${Math.max(1, Math.round(size / 1024))} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
});
</script>

<template>
  <md-list-item class="capture-attachment-row" type="text">
    <img v-if="previewUrl" slot="start" class="capture-attachment-thumb" :src="previewUrl" alt="" />
    <AppIcon v-else slot="start" class="capture-attachment-icon" :name="attachment.kind === 'pdf' ? 'picture_as_pdf' : 'upload_file'" />
    <span slot="headline" class="capture-attachment-name">{{ attachment.name }}</span>
    <span slot="supporting-text" class="capture-attachment-meta">{{ attachment.kind === 'pdf' ? 'PDF' : attachment.kind === 'image' ? '圖片' : '檔案' }}<template v-if="sizeLabel"> · {{ sizeLabel }}</template><template v-if="attachment.upload_error"> · 上傳失敗</template></span>
    <md-icon-button slot="end" class="capture-attachment-remove" :aria-label="`移除 ${attachment.name}`" :disabled="props.disabled" @click="emit('remove')"><AppIcon name="close" /></md-icon-button>
  </md-list-item>
</template>
