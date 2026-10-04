import assert from 'node:assert/strict';
import test from 'node:test';
import { acceptedProposals, confirmableProposals, isPreviewableProposal, buildApplyResults, captureDraftAfterInterpret, groupPendingProposalsBySource, sourceHeading, sourceTypeLabel } from './proposalWorkflow.js';

const proposals = [
  { id: 'pending-1', status: 'pending', patch: { title: '待確認' } },
  { id: 'edited-1', status: 'edited', patch: { title: '已修改' } },
  { id: 'accepted-1', status: 'accepted', operation: 'create', target_type: 'event', patch: { title: '已接受', date: '2026-09-25', all_day: true } },
  { id: 'rejected-1', status: 'rejected', patch: { title: '已忽略' } },
];

test('Apply eligibility includes accepted proposals that satisfy backend preconditions only', () => {
  const eligible = [
    ...proposals,
    { id: 'accepted-update', status: 'accepted', operation: 'update', target_type: 'task', target_id: 'task-1', patch: { title: '更新' } },
    { id: 'accepted-ignore', status: 'accepted', operation: 'ignore', target_type: 'task', patch: {} },
    { id: 'needs-review', status: 'accepted', operation: 'create', target_type: 'task', needs_review: true, patch: { title: '尚需確認' } },
    { id: 'missing-target', status: 'accepted', operation: 'update', target_type: 'event', patch: { title: '未選目標' } },
    { id: 'empty-update', status: 'accepted', operation: 'update', target_type: 'task', target_id: 'task-2', patch: {} },
    { id: 'invalid-create', status: 'accepted', operation: 'create', target_type: 'event', patch: { title: '缺少日期' } },
  ];
  assert.deepEqual(acceptedProposals(eligible).map((proposal) => proposal.id), ['accepted-1', 'accepted-update', 'accepted-ignore']);
});

test('one-step confirmation accepts complete, start-only, and all-day Events but leaves missing schedules unresolved', () => {
  const items = [
    { id: 'timed', status: 'pending', operation: 'create', target_type: 'event', patch: { title: '課程', date: '2026-09-28', time: '09:00～10:00' } },
    { id: 'start-only', status: 'pending', operation: 'create', target_type: 'event', patch: { title: '集合', date: '2026-09-28', time: '09:00' } },
    { id: 'all-day', status: 'edited', operation: 'create', target_type: 'event', patch: { title: '校慶', date: '2026-09-29', all_day: true } },
    { id: 'missing-schedule', status: 'pending', operation: 'create', target_type: 'event', patch: { title: '待補時間', date: '2026-09-30' } },
    { id: 'ambiguous', status: 'pending', operation: 'create', target_type: 'task', needs_review: true, patch: { title: '待確認待辦' } },
  ];
  assert.deepEqual(confirmableProposals(items).map((proposal) => proposal.id), ['timed', 'start-only', 'all-day']);
});

test('Task proposals remain confirmable when optional due and list metadata are omitted', () => {
  const items = [
    { id: 'task-no-meta', status: 'pending', operation: 'create', target_type: 'task', patch: { title: '買西瓜' } },
    { id: 'task-with-due', status: 'pending', operation: 'create', target_type: 'task', patch: { title: '買哈密瓜', due: '2026-09-30' } },
  ];
  assert.deepEqual(confirmableProposals(items).map(({ id }) => id), ['task-no-meta', 'task-with-due']);
});

test('accepted start-only Event proposals remain eligible for Apply', () => {
  assert.deepEqual(acceptedProposals([{
    id: 'accepted-start-only', status: 'accepted', operation: 'create', target_type: 'event',
    patch: { title: '集合', date: '2026-09-28', time: '09:00' },
  }]).map(({ id }) => id), ['accepted-start-only']);
});

test('blank event creates are hidden from review while incomplete but substantive items stay reviewable', () => {
  const items = [
    { id: 'blank', operation: 'create', target_type: 'event', patch: { title: '  ', date: ' ', time: '' } },
    { id: 'title-only', operation: 'create', target_type: 'event', patch: { title: '段考' } },
    { id: 'date-only', operation: 'create', target_type: 'event', patch: { date: '2026-10-17' } },
    { id: 'task', operation: 'create', target_type: 'task', patch: { title: '繳交作業' } },
  ];
  assert.deepEqual(items.filter(isPreviewableProposal).map(({ id }) => id), ['title-only', 'date-only', 'task']);
});

test('Apply summary reports success, failure, and unprocessed review items by proposal', () => {
  const outcomes = new Map([
    ['accepted-1', { status: 'applied', message: '已套用' }],
  ]);
  const results = buildApplyResults(proposals, outcomes);
  assert.deepEqual(results.map(({ id, status }) => [id, status]), [
    ['pending-1', 'unprocessed'],
    ['edited-1', 'unprocessed'],
    ['accepted-1', 'applied'],
  ]);
  outcomes.set('accepted-1', { status: 'failed', message: '套用失敗' });
  assert.equal(buildApplyResults(proposals, outcomes)[2].message, '套用失敗');
});

test('returning to supplement Capture retains text, attachments, and the durable Source relation', () => {
  const attachment = { id: 'file-1', name: '通知.pdf', file: { marker: true } };
  const draft = { body: '補充資料', source_id: null, attachments: [attachment] };
  const restored = captureDraftAfterInterpret(draft, { id: 'notice-1' });
  assert.equal(restored.body, draft.body);
  assert.equal(restored.source_id, 'notice-1');
  assert.equal(restored.attachments[0], attachment);
  assert.notEqual(restored.attachments, draft.attachments);
});

test('source heading uses real source metadata and tolerates a missing Source', () => {
  assert.equal(sourceHeading({ title: '課程提醒' }), '課程提醒');
  assert.equal(sourceHeading({ name: '通知.pdf' }), '通知.pdf');
  assert.equal(sourceHeading(null), '原始內容');
});

test('source type labels use user-facing terms instead of internal enum values', () => {
  assert.equal(sourceTypeLabel({ source_type: 'manual' }), '貼上文字');
  assert.equal(sourceTypeLabel({ source_type: 'url' }), '網頁連結');
  assert.equal(sourceTypeLabel({ source_type: 'import' }), '附件');
  assert.equal(sourceTypeLabel({ source_type: 'unrecognized_internal_value' }), '原始資訊');
});

test('Today batches proposals from the same Source and keeps source-less proposals individually reachable', () => {
  const batches = groupPendingProposalsBySource([
    { id: 'event-1', source_id: 'notice-1', status: 'pending', target_type: 'event', patch: { title: '期中考' } },
    { id: 'task-1', source_id: 'notice-1', status: 'pending', target_type: 'task', needs_review: true, patch: { title: '繳交報告' } },
    { id: 'manual-1', source_id: 'manual', status: 'pending', target_type: 'task', patch: { title: '手動待辦' } },
    { id: 'no-source-1', status: 'pending', target_type: 'task', patch: { title: '來源缺漏' } },
  ]);

  assert.equal(batches.length, 3);
  assert.deepEqual(batches[0], {
    id: 'source:notice-1',
    sourceId: 'notice-1',
    proposals: [
      { id: 'event-1', source_id: 'notice-1', status: 'pending', target_type: 'event', patch: { title: '期中考' } },
      { id: 'task-1', source_id: 'notice-1', status: 'pending', target_type: 'task', needs_review: true, patch: { title: '繳交報告' } },
    ],
    count: 2,
    title: '期中考',
    eventCount: 1,
    taskCount: 1,
    unresolvedCount: 1,
  });
  assert.equal(batches[1].id, 'proposal:manual-1');
  assert.equal(batches[2].id, 'proposal:no-source-1');
});
