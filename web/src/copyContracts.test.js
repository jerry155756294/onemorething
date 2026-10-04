import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const readSource = (path) => readFile(new URL(path, import.meta.url), 'utf8');

test('Today labels its always-visible Capture field for text, URL, and attachment intake', async () => {
  const source = await readSource('./views/TodayView.vue');
  assert.match(source, /<h2 id="today-capture-title">有什麼要記的？<\/h2>/);
  assert.match(source, /<CaptureComposer[^>]*inline-submit/);
  const composer = await readSource('./components/CaptureComposer.vue');
  assert.match(composer, /placeholder="貼上文字或網址，或加入圖片／PDF"/);
  assert.match(composer, /: '整理'/);
});

test('Source details stay in Inbox while Proposal Review omits original-content affordances', async () => {
  const [review, detail] = await Promise.all([
    readSource('./components/ProposalReviewDialog.vue'),
    readSource('./components/SourceDetailPanel.vue'),
  ]);
  assert.doesNotMatch(review, /展開查看原始內容|原始內容|SourceDetailPanel|showOriginal/);
  assert.match(detail, /complete: '整理完成'/);
  assert.match(detail, /complete: '已讀取網頁內容'/);
  assert.match(detail, /網頁內容較長，部分文字未納入整理/);
  const detailTemplate = detail.slice(detail.indexOf('<template>'));
  assert.doesNotMatch(detailTemplate, /proposal|source_id|processing\.status|worker|debug|provenance/i);
  assert.doesNotMatch(detail, /處理完成|網頁內容已擷取|擷取內容已依 AI 處理上限截短/);
});

test('source records keep the 原始資訊 label', async () => {
  const source = await readSource('./views/InboxView.vue');
  assert.match(source, /原始資訊/);
});
