<script setup>
import { computed, ref } from 'vue';
import ProposalItem from '../components/ProposalItem.vue';
import SourceDetailPanel from '../components/SourceDetailPanel.vue';
import { canViewSource, inboxLabels, reviewableProposals, sourceIdForRetry } from '../inboxData.js';

const props = defineProps({
  proposals: { type: Array, default: () => [] },
  source: { type: Object, default: null },
  apiBase: { type: String, default: '/api' },
  busy: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  error: { type: String, default: '' },
  sourceLoading: { type: Boolean, default: false },
  sourceError: { type: String, default: '' },
});

const emit = defineEmits([
  'view-source', 'retry-proposals', 'retry-source', 'accept-proposal',
  'reject-proposal', 'save-proposal', 'select-proposal-target', 'apply-proposal',
]);

const visibleProposals = computed(() => reviewableProposals(props.proposals));
const showSourcePanel = computed(() => Boolean(props.source || props.sourceLoading || props.sourceError));
const labels = inboxLabels;
const lastRequestedSourceId = ref('');
const retrySourceId = computed(() => sourceIdForRetry(props.source, lastRequestedSourceId.value));
function hasViewableSource(sourceId) { return canViewSource(sourceId); }
function openSource(sourceId) {
  if (!hasViewableSource(sourceId)) return;
  lastRequestedSourceId.value = String(sourceId);
  emit('view-source', lastRequestedSourceId.value);
}
</script>

<template>
  <div class="workspace-route route-inbox">
    <header class="calendar-head inbox-head">
      <div><span class="md3-eyebrow">{{ labels.page }}</span><h1>收件匣</h1><p>回看原始資訊，逐項確認它能否成為你的行動。</p></div>
    </header>

    <p v-if="props.loading" class="inbox-feedback" role="status" aria-live="polite">正在載入收件匣…</p>

    <div class="inbox-workspace" :class="{ 'has-source-panel': showSourcePanel }">
      <section class="inbox-proposals" aria-labelledby="inbox-proposals-title" :aria-busy="props.loading">
        <div class="inbox-section-heading"><div><span class="section-kicker">{{ labels.proposals }}</span><h2 id="inbox-proposals-title">待確認的整理結果</h2><p>接受只記錄你的決定；套用才會建立或更新行程與待辦。</p></div><span class="inbox-count">{{ visibleProposals.length }} 項</span></div>
        <div v-if="props.error" class="inbox-recovery" role="alert"><span>{{ props.error }}</span><button type="button" class="inbox-retry" :disabled="props.loading" @click="emit('retry-proposals')">{{ props.loading ? '正在重試…' : '重試載入提案' }}</button></div>
        <div v-else-if="visibleProposals.length" class="inbox-proposal-list" aria-label="完整待確認提案清單">
          <article v-for="proposal in visibleProposals" :key="proposal.id" class="inbox-proposal-row">
            <ProposalItem
              :proposal="proposal"
              :busy="props.busy"
              :show-apply="true"
              @accept="emit('accept-proposal', $event)"
              @reject="emit('reject-proposal', $event)"
              @save="emit('save-proposal', $event)"
              @select-target="emit('select-proposal-target', $event)"
              @apply="emit('apply-proposal', $event)"
            />
            <button v-if="hasViewableSource(proposal.source_id)" type="button" class="inbox-source-link" :disabled="props.sourceLoading" @click="openSource(proposal.source_id)">回看原始資訊</button>
            <span v-else class="inbox-source-missing">此提案沒有關聯的原始資訊</span>
          </article>
        </div>
        <div v-else-if="!props.loading && !props.error" class="inbox-empty"><strong>目前沒有待確認的整理結果</strong><span>新的整理結果會顯示在這裡，套用前都能先檢查或修改。</span></div>
      </section>

      <aside v-if="showSourcePanel" class="inbox-source-panel" aria-labelledby="inbox-source-title" :aria-busy="props.sourceLoading">
        <div class="inbox-section-heading"><div><span class="section-kicker">{{ labels.source }}</span><h2 id="inbox-source-title">原始資訊</h2></div></div>
        <div v-if="props.sourceError" class="inbox-recovery" role="alert"><span>{{ props.sourceError }}</span><button type="button" class="inbox-retry" :disabled="props.sourceLoading || !retrySourceId" @click="emit('retry-source', retrySourceId)">{{ props.sourceLoading ? '正在重試…' : '重試載入原始資訊' }}</button></div>
        <p v-else-if="props.sourceLoading" class="inbox-feedback" role="status">正在載入原始資訊…</p>
        <SourceDetailPanel v-else-if='props.source' :source='props.source' :api-base='props.apiBase' />
        <div v-else class="inbox-empty"><strong>尚未選取原始資訊</strong><span>從提案旁的「回看原始資訊」開啟完整內容、附件與處理關聯。</span></div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.inbox-workspace { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(280px, .8fr); gap: 28px; padding-top: 24px; align-items: start; }
.inbox-workspace:not(.has-source-panel) { grid-template-columns: minmax(0, 1fr); }
.inbox-proposals, .inbox-source-panel { min-width: 0; }
.inbox-section-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 18px; }
.inbox-section-heading h2 { margin: 6px 0; color: var(--route-ink); font: 600 1.5rem/1.15 var(--display); }
.inbox-section-heading p { margin: 0; color: var(--route-muted); font-size: .875rem; line-height: 1.55; }
.inbox-count { flex: none; color: var(--route-muted); font: 500 .8rem var(--mono); }
.inbox-proposal-list { display: grid; gap: 14px; }
.inbox-proposal-row, .inbox-source-panel { border: 1px solid var(--route-line); border-radius: 14px; background: var(--route-surface); padding: 16px; }
.inbox-source-link { min-height: 44px; margin-top: 12px; border: 0; background: transparent; color: var(--route-accent); font: 600 .875rem var(--body); text-decoration: underline; text-underline-offset: .2em; cursor: pointer; }
.inbox-source-link:focus-visible { outline: 3px solid currentColor; outline-offset: 2px; }
.inbox-source-link:disabled { cursor: wait; opacity: .65; }
.inbox-source-missing, .inbox-source-meta, .inbox-processing { color: var(--route-muted); font-size: .8125rem; line-height: 1.55; }
.inbox-empty { display: grid; gap: 6px; border: 0; border-radius: 0; padding: 18px 0; background: transparent; color: var(--route-muted); font-size: .875rem; line-height: 1.55; }
.inbox-empty strong { color: var(--route-ink); font: 600 1.1rem var(--display); }
.inbox-feedback { color: var(--route-muted); line-height: 1.5; }
.inbox-recovery { display: grid; justify-items: start; gap: 8px; color: var(--route-muted); font-size: .875rem; line-height: 1.5; }
.inbox-retry { min-height: 44px; padding: 8px 12px; border: 1px solid var(--route-line); border-radius: 999px; background: transparent; color: var(--route-accent); font: 600 .875rem var(--body); cursor: pointer; }
.inbox-retry:focus-visible { outline: 3px solid currentColor; outline-offset: 2px; }
.inbox-retry:disabled { cursor: wait; opacity: .65; }
@media (max-width: 900px) { .inbox-workspace { grid-template-columns: 1fr; } }
</style>
