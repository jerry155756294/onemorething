<script setup>
import { computed, ref, watch } from 'vue';
import ProposalItem from './ProposalItem.vue';
import { confirmableProposals, isPreviewableProposal } from '../proposalWorkflow.js';
import { captureEmptyResult } from '../captureWorkflow.js';

defineOptions({ inheritAttrs: false });

const props = defineProps({
  open: { type: Boolean, default: false },
  source: { type: Object, default: null },
  proposals: { type: Array, default: () => [] },
  unsupportedAttachments: { type: Array, default: () => [] },
  overviewMode: { type: Boolean, default: false },
  sourceHistoryError: { type: String, default: '' },
  sourceHistoryLoading: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  error: { type: String, default: '' },
});
const emit = defineEmits(['close', 'back', 'accept', 'reject', 'save', 'select-target', 'apply', 'apply-all', 'retry-source-history']);

const expandedId = ref('');
const selectedIds = ref(new Set());
const visibleProposals = computed(() => props.proposals.filter(isPreviewableProposal));
watch(visibleProposals, (items) => {
  selectedIds.value = new Set(items.filter((item) => item.status !== 'rejected').map((item) => String(item.id)));
}, { immediate: true });

const textEncodingErrorMessage = computed(() => {
  const names = [...new Set(props.unsupportedAttachments
    .filter((attachment) => attachment.extraction_error === 'unsupported_encoding')
    .map((attachment) => attachment.name || attachment.filename)
    .filter(Boolean))];
  return names.length ? `無法讀取文字附件「${names.join('、')}」的編碼。請另存為 UTF-8 後重新加入。` : '';
});
const selectedProposals = computed(() => visibleProposals.value.filter((proposal) => selectedIds.value.has(String(proposal.id))));
const readyProposals = computed(() => props.sourceHistoryError ? [] : confirmableProposals(selectedProposals.value));
const unresolvedCount = computed(() => selectedProposals.value.filter((proposal) => !readyProposals.value.some((ready) => ready.id === proposal.id)).length);
const emptyResult = computed(() => captureEmptyResult(props.source));
const proposalCountLabel = computed(() => {
  const active = visibleProposals.value.filter((proposal) => proposal.status !== 'rejected');
  const eventCount = active.filter((proposal) => proposal.target_type === 'event').length;
  const taskCount = active.filter((proposal) => proposal.target_type === 'task').length;
  return [eventCount ? `${eventCount} 個行程` : '', taskCount ? `${taskCount} 個待辦` : ''].filter(Boolean).join(' · ') || '沒有可加入的項目';
});
const applyLabel = computed(() => readyProposals.value.length ? `加入 ${readyProposals.value.length} 個項目` : unresolvedCount.value ? '先補充標示的資訊' : '沒有可加入的項目');

function proposalTitle(proposal) {
  return proposal.patch?.title || proposal.target_title || (proposal.target_type === 'event' ? '未命名行程' : '未命名待辦');
}
function proposalMeta(proposal) {
  const patch = proposal.patch || {};
  if (proposal.target_type === 'event') {
    if (patch.all_day) return [patch.date || patch.start_at?.slice?.(0, 10), '全天'].filter(Boolean).join(' · ');
    const date = patch.date || patch.start_at?.slice?.(0, 10);
    const start = patch.start_at?.slice?.(11, 16) || (String(patch.time || '').match(/\d{1,2}:\d{2}/)?.[0]);
    const end = patch.end_at?.slice?.(11, 16) || (String(patch.time || '').match(/\d{1,2}:\d{2}/g)?.[1]);
    return [date, start && end ? `${start}–${end}` : start, patch.location].filter(Boolean).join(' · ');
  }
  return [patch.due_label || patch.due, patch.start_at ? `${patch.start_at.slice(0, 10)} ${patch.start_at.slice(11, 16)}` : ''].filter(Boolean).join(' · ');
}
function isSelected(proposal) { return selectedIds.value.has(String(proposal.id)); }
function toggleSelected(proposal, checked) {
  const next = new Set(selectedIds.value);
  if (checked) next.add(String(proposal.id)); else next.delete(String(proposal.id));
  selectedIds.value = next;
}
function ignoreProposal(proposal) {
  toggleSelected(proposal, false);
  if (['pending', 'edited'].includes(proposal.status)) emit('reject', proposal);
}
function changeProposalType(proposal, event) {
  const targetType = event.target.value;
  if (!['event', 'task'].includes(targetType) || targetType === proposal.target_type) return;
  emit('save', { proposal, patch: proposal.patch || {}, targetType });
}
function requestApply() {
  if (props.busy || !readyProposals.value.length) return;
  emit('apply-all', selectedProposals.value);
}
</script>

<template>
  <div v-if="props.open" class="proposal-review-layer" @keydown.esc.stop.prevent="emit('back')">
    <div class="proposal-review-scrim" aria-hidden="true" @click="emit('back')"></div>
    <section class="proposal-review-card" role="dialog" aria-modal="true" aria-labelledby="proposal-review-dialog-title">
      <header class="proposal-review-head">
        <div>
          <h2 id="proposal-review-dialog-title">確認加入內容</h2>
          <p>{{ proposalCountLabel }}</p>
        </div>
        <md-icon-button class="proposal-dialog-close" aria-label="關閉" @click="emit('back')"><AppIcon name="close" /></md-icon-button>
      </header>

      <div class="proposal-review-body">
        <div v-if="props.sourceHistoryError" class="proposal-error proposal-history-error" role="alert">
          <span>{{ props.sourceHistoryError }}</span>
          <md-button color="text" size="small" :disabled="props.sourceHistoryLoading || props.busy" @click="emit('retry-source-history')">{{ props.sourceHistoryLoading ? '正在重試…' : '重新載入' }}</md-button>
        </div>
        <p v-if="props.error" class="proposal-error" role="alert">{{ props.error }}</p>
        <p v-if="textEncodingErrorMessage" class="proposal-error proposal-attachment-encoding-error" role="alert">{{ textEncodingErrorMessage }}</p>

        <div v-if="visibleProposals.length && !props.sourceHistoryError" class="proposal-review-list" role="list">
          <article v-for="proposal in visibleProposals" :key="proposal.id" class="proposal-review-row" :class="{ 'is-ignored': proposal.status === 'rejected' }" role="listitem">
            <div class="proposal-review-row-main">
              <md-checkbox
                touch-target="wrapper"
                :checked="isSelected(proposal)"
                :disabled="props.busy || proposal.status === 'rejected'"
                :aria-label="`加入 ${proposalTitle(proposal)}`"
                @change="toggleSelected(proposal, $event.target.checked)"
              ></md-checkbox>
              <div class="proposal-review-row-copy">
                <span>{{ proposal.target_type === 'event' ? '行程' : '待辦' }}<template v-if="proposal.needs_review"> · 待補充</template></span>
                <strong>{{ proposalTitle(proposal) }}</strong>
                <small v-if="proposalMeta(proposal)">{{ proposalMeta(proposal) }}</small>
              </div>
              <div class="proposal-review-row-actions">
                <md-button color="text" size="small" @click="expandedId = expandedId === proposal.id ? '' : proposal.id">{{ expandedId === proposal.id ? '收起' : '詳細資訊' }}</md-button>
                <md-button v-if="proposal.status !== 'rejected'" color="text" size="small" :disabled="props.busy" @click="ignoreProposal(proposal)">忽略</md-button>
                <span v-else class="proposal-review-ignored">已忽略</span>
              </div>
            </div>
            <md-select
              v-if="proposal.needs_review && proposal.operation === 'create' && ['event', 'task'].includes(proposal.target_type)"
              class="proposal-review-type-select"
              color="outlined"
              label="加入為"
              :value="proposal.target_type"
              :disabled="props.busy || proposal.status === 'rejected'"
              @change="changeProposalType(proposal, $event)"
            >
              <md-select-option value="event"><span slot="headline">行程（可設為全天）</span></md-select-option>
              <md-select-option value="task"><span slot="headline">待辦（可設為全天期限）</span></md-select-option>
            </md-select>
            <div v-if="expandedId === proposal.id" class="proposal-review-expanded">
              <ProposalItem
                :proposal="proposal"
                :busy="props.busy"
                review-mode
                @accept="emit('accept', $event)"
                @reject="emit('reject', $event)"
                @save="emit('save', $event)"
                @select-target="emit('select-target', $event)"
                @apply="emit('apply', $event)"
              />
            </div>
          </article>
        </div>
        <section v-else class="proposal-empty" aria-live="polite">
          <h3>{{ props.sourceHistoryError ? '完整整理紀錄尚未載入' : props.overviewMode && props.source ? '這筆內容目前沒有整理提案' : emptyResult.title }}</h3>
          <p v-if="!props.sourceHistoryError && (!props.overviewMode || !props.source)">{{ emptyResult.message }}</p>
        </section>
      </div>

      <footer class="proposal-review-actions">
        <md-button color="text" size="medium" :disabled="props.busy" @click="emit('back')">取消</md-button>
        <md-button color="filled" size="medium" :disabled="props.busy || !readyProposals.length" @click="requestApply">{{ props.busy ? '加入中…' : applyLabel }}</md-button>
      </footer>
    </section>
  </div>
</template>
