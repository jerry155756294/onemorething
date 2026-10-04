<script setup>
import { computed } from 'vue';
import { sourceHeading, sourceTypeLabel } from '../proposalWorkflow.js';
import { sourceAttachmentUrl, sourceOriginalText } from '../inboxData.js';

const props = defineProps({
  source: { type: Object, default: null },
  unsupportedAttachments: { type: Array, default: () => [] },
  apiBase: { type: String, default: '/api' },
});

const title = computed(() => sourceHeading(props.source));
const sourceType = computed(() => sourceTypeLabel(props.source));
const originalText = computed(() => sourceOriginalText(props.source));
const attachments = computed(() => [...(props.source?.attachments || []), ...props.unsupportedAttachments]);

function capturedAtLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('zh-Hant-TW', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function processingLabel(source) {
  const status = source?.processing?.ai?.status || source?.processing?.status;
  return ({ complete: '整理完成', not_requested: '未使用 AI 整理', failed: '整理失敗', timeout: '整理逾時', too_large: '內容超出整理範圍' })[status] || '';
}

function webpageLabel(source) {
  const status = source?.processing?.webpage?.status;
  return ({ complete: '已讀取網頁內容', unavailable: '網頁讀取服務未連線', failed: '讀取網頁內容失敗' })[status] || '';
}

function attachmentUrl(attachment) {
  return sourceAttachmentUrl(props.source, attachment, props.apiBase);
}
</script>

<template>
  <section class="source-detail-panel" aria-label="原始資訊詳情">
    <header class="source-detail-heading">
      <strong>{{ title }}</strong>
      <time v-if="props.source.created_at || props.source.captured_at">{{ capturedAtLabel(props.source.created_at || props.source.captured_at) }}</time>
    </header>
    <p class="source-detail-meta">
      <span>{{ sourceType }}</span>
      <span v-if="processingLabel(source)">{{ processingLabel(source) }}</span>
      <span v-if="webpageLabel(source)">{{ webpageLabel(source) }}</span>
      <span v-if="props.source.processing?.webpage?.truncated">網頁內容較長，部分文字未納入整理</span>
    </p>

    <section class="source-detail-section">
      <h3>原始內容</h3>
      <div class="source-detail-original">{{ originalText || '這筆原始資訊沒有文字內容。' }}</div>
    </section>

    <section v-if="attachments.length" class="source-detail-section" aria-label="原始附件">
      <h3>附件</h3>
      <article v-for="attachment in attachments" :key="attachment.id || attachment.name || attachment.filename" class="source-detail-attachment">
        <strong>{{ attachment.name || attachment.filename || '未命名附件' }}</strong>
        <p v-if="attachment.extracted_text || attachment.preview_text">{{ attachment.extracted_text || attachment.preview_text }}</p>
        <a v-if="attachmentUrl(attachment)" :href="attachmentUrl(attachment)" target="_blank" rel="noopener noreferrer">開啟原始附件 ↗</a>
        <small v-else-if="attachment.raw_available === false">原始附件目前無法下載。</small>
      </article>
    </section>
  </section>
</template>

<style scoped>
.source-detail-panel { --source-ink: var(--route-ink, var(--capture-ink, #202522)); --source-muted: var(--route-muted, var(--capture-muted, #77796f)); --source-accent: var(--route-accent, var(--capture-accent, #355d47)); min-width: 0; display: grid; gap: 14px; }
.source-detail-heading { display: grid; gap: 4px; }
.source-detail-heading strong { color: var(--source-ink); font: 600 1.15rem/1.3 var(--display); overflow-wrap: anywhere; }
.source-detail-heading time, .source-detail-meta { color: var(--source-muted); font-size: .8125rem; line-height: 1.5; }
.source-detail-meta { display: flex; flex-wrap: wrap; gap: 4px 12px; margin: 0; }
.source-detail-section { min-width: 0; display: grid; gap: 7px; border-top: 1px solid var(--route-line); padding-top: 12px; }
.source-detail-section h3 { margin: 0; color: var(--source-ink); font-size: .9rem; }
.source-detail-original { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.65; }
.source-detail-attachment { min-width: 0; display: grid; gap: 6px; padding: 8px 0; overflow-wrap: anywhere; }
.source-detail-attachment p { margin: 0; color: var(--source-muted); font-size: .8125rem; line-height: 1.5; white-space: pre-wrap; }
.source-detail-attachment a, .source-detail-attachment small { color: var(--source-accent); font-size: .8125rem; }
</style>
