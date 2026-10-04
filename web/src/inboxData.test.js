import test from 'node:test';
import assert from 'node:assert/strict';
import { canViewSource, inboxLabels, proposalsForSource, reviewableProposals, sourceAttachmentUrl, sourceDetailsFromPayload, sourceIdForRetry, sourceOriginalText } from './inboxData.js';

test('Inbox eyebrow and section labels use concise Traditional Chinese copy', () => {
  assert.deepEqual(inboxLabels, { page: '收到的資訊', capture: '新增資訊', proposals: '待你確認', source: '原始資訊' });
});

test('Inbox keeps every pending, edited, and accepted proposal for the full review list', () => {
  const items = Array.from({ length: 8 }, (_, index) => ({ id: String(index), status: index === 7 ? 'applied' : ['pending', 'edited', 'accepted'][index % 3] }));
  assert.equal(reviewableProposals(items).length, 7);
  assert.deepEqual(reviewableProposals(items).map(({ id }) => id), ['0', '1', '2', '3', '4', '5', '6']);
});

test('source detail relation is exact and safely handles a missing Source', () => {
  const proposals = [{ source_id: 'a', id: 'one' }, { source_id: 'b', id: 'two' }];
  assert.deepEqual(proposalsForSource(proposals, { id: 'a' }).map(({ id }) => id), ['one']);
  assert.deepEqual(proposalsForSource(proposals, null), []);
  assert.equal(sourceOriginalText(null), '');
  assert.equal(sourceOriginalText({ body: '  原文  ' }), '原文');
  assert.equal(sourceOriginalText({ excerpt: '  節錄  ' }), '節錄');
});

test('source details restore the id and full body from the notice response envelope', () => {
  const details = sourceDetailsFromPayload({
    id: 'notice-42',
    body: '完整的原始通知內容，超過摘要長度也必須保留。',
    source: { name: '課程通知', excerpt: '完整的原始通知內容' },
  }, 'notice-42');
  assert.equal(details.id, 'notice-42');
  assert.equal(details.body, '完整的原始通知內容，超過摘要長度也必須保留。');
  assert.equal(sourceOriginalText(details), '完整的原始通知內容，超過摘要長度也必須保留。');
  assert.equal(sourceDetailsFromPayload({ id: 'different-source', source: {} }, 'notice-42'), null);
});

test('source retry keeps the requested source id while its detail is unavailable', () => {
  assert.equal(sourceIdForRetry(null, 'selected-source'), 'selected-source');
  assert.equal(sourceIdForRetry({ id: 'loaded-source' }, 'selected-source'), 'loaded-source');
  assert.equal(sourceIdForRetry(null, ''), '');
});

test('source action is hidden for missing or manual source ids but remains available for real sources', () => {
  assert.equal(canViewSource(null), false);
  assert.equal(canViewSource(''), false);
  assert.equal(canViewSource('   '), false);
  assert.equal(canViewSource('manual'), false);
  assert.equal(canViewSource('notice-123'), true);
});

test('source attachment links use the real attachment endpoint and respect expiry', () => {
  const source = { id: 'notice 1' };
  assert.equal(sourceAttachmentUrl(source, { id: 'raw/1' }), '/api/sources/notice%201/attachments/raw%2F1');
  assert.equal(sourceAttachmentUrl(source, { id: 'old', raw_available: false }), '');
  assert.equal(sourceAttachmentUrl(null, { id: 'raw' }), '');
  assert.equal(sourceAttachmentUrl(source, { url: '/files/a.pdf' }), '/files/a.pdf');
});
