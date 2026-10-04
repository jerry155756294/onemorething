<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import AttachmentItem from './AttachmentItem.vue';
import './CaptureTextField.js';

const props = defineProps({
  text: { type: String, default: '' },
  attachments: { type: Array, default: () => [] },
  policy: { type: Object, default: () => ({}) },
  busy: { type: Boolean, default: false },
  submitDisabled: { type: Boolean, default: false },
  error: { type: String, default: '' },
  inlineSubmit: { type: Boolean, default: false },
  disabled: { type: Boolean, default: false },
  disabledReason: { type: String, default: '' },
});
const emit = defineEmits(['update:text', 'add-files', 'remove-attachment', 'submit']);
const fileInput = ref(null);
const textField = ref(null);
let textFieldRoot = null;

function handleComposerInput(event) {
  emit('update:text', event.target.value);
}

function handleComposerKeydown(event) {
  if (event.key !== 'Enter' || !event.ctrlKey || event.isComposing) return;
  event.preventDefault();
  emit('submit');
}

onMounted(async () => {
  await nextTick();
  await textField.value?.updateComplete;
  textFieldRoot = textField.value?.shadowRoot || null;
  textFieldRoot?.addEventListener('input', handleComposerInput);
});

onBeforeUnmount(() => {
  textFieldRoot?.removeEventListener('input', handleComposerInput);
});

function chooseFiles() {
  fileInput.value?.click();
}

function handleFiles(event) {
  const files = Array.from(event.target.files || []);
  if (files.length) emit('add-files', files);
  event.target.value = '';
}
</script>

<template>
  <div class="capture-composer">
    <div class="capture-input-surface">
      <capture-text-field
        ref="textField"
        id="capture-composer-text"
        class="capture-composer-textarea"
        color="outlined"
        type="textarea"
        :value="props.text"
        rows="2"
        aria-label="貼上文字或輸入網址，也可以加入圖片或 PDF"
        placeholder="貼上文字或網址，或加入圖片／PDF"
        :disabled="props.disabled"
        @keydown="handleComposerKeydown"
      ></capture-text-field>
      <div class="capture-composer-actions">
        <md-button type="button" color="text" size="small" class="capture-add-file" :disabled="props.busy || props.disabled" @click="chooseFiles"><AppIcon slot="icon" name="add" />加入圖片或檔案</md-button>
        <md-button v-if="props.inlineSubmit" type="button" color="filled" size="small" class="capture-inline-submit" :disabled="props.busy || props.submitDisabled || props.disabled || (!props.text.trim() && !props.attachments.length)" @click="emit('submit')">{{ props.busy ? '整理中…' : '整理' }}</md-button>
      </div>
    </div>

    <md-list v-if="props.attachments.length" class="capture-attachment-list" aria-label="已加入的圖片或檔案">
      <AttachmentItem v-for="attachment in props.attachments" :key="attachment.id" :attachment="attachment" :disabled="props.busy" @remove="emit('remove-attachment', attachment.id)" />
    </md-list>

    <input ref="fileInput" class="capture-file-input" type="file" accept=".png,.jpg,.jpeg,.gif,.webp,.bmp,.heic,.heif,.pdf,.docx,.odt,.ods,.odp,.txt,.md,.markdown,.csv,.tsv" multiple @change="handleFiles" />

    <div v-if="props.inlineSubmit && props.busy" class="capture-inline-progress" role="status" aria-live="polite">
      <span class="capture-inline-spinner" aria-hidden="true"></span>
      <span>正在整理資訊…</span>
    </div>
    <p v-if="props.disabledReason" class="capture-disabled-note" role="note"><AppIcon name="verified_user" />{{ props.disabledReason }}</p>
    <p v-if="props.error" class="capture-inline-error" role="alert">{{ props.error }}</p>
    <p class="capture-budget-hint">單檔 {{ Math.round((props.policy.max_upload_bytes || 10485760) / 1024 / 1024) }} MB · 最多 {{ props.policy.max_attachments_per_capture || 5 }} 個 · 合計 {{ Math.round((props.policy.max_total_capture_bytes || 26214400) / 1024 / 1024) }} MB</p>
  </div>
</template>
