import test from 'node:test';
import assert from 'node:assert/strict';
import { captureEmptyResult, captureRequestFailureMessage } from './captureWorkflow.js';

test('Capture request errors give a safe, actionable URL-specific message', () => {
  assert.equal(
    captureRequestFailureMessage('https://example.test', 422),
    '無法讀取這個網址。請確認網址完整且可公開存取，或直接貼上頁面文字。',
  );
  assert.match(captureRequestFailureMessage('https://example.test', 503), /稍後重試.*直接貼上頁面文字/);
  assert.doesNotMatch(captureRequestFailureMessage('https://example.test', 503), /503|backend|provider/i);
});

test('Capture request errors explain text and service recovery without backend details', () => {
  assert.match(captureRequestFailureMessage('', 502), /稍後重試.*檢查文字與附件/);
  assert.match(captureRequestFailureMessage('', 0), /連不到整理服務.*稍後重試/);
  assert.match(captureRequestFailureMessage('', 413), /縮短文字或減少附件/);
});

test('empty Capture results explain URL failure, crawler availability, and uninterpretable text', () => {
  assert.equal(captureEmptyResult({ source_type: 'url', processing: { webpage: { status: 'failed' } } }).title, '無法讀取這個網址');
  assert.match(captureEmptyResult({ type: 'url', processing: { webpage: { status: 'unavailable' } } }).message, /稍後重試.*貼上頁面/);
  assert.match(captureEmptyResult({ source_type: 'url', processing: { webpage: { status: 'complete' } } }).message, /課程時間或待辦資訊/);
  assert.match(captureEmptyResult({ source_type: 'manual' }).message, /明確行程或待辦/);
});


test('empty Capture results distinguish attachment extraction failure from a valid but proposal-free read', () => {
  assert.match(captureEmptyResult({ processing: { attachments: [{ status: 'extraction_failed', processed_chars: 0 }] } }).title, /沒有成功讀取/);
  assert.match(captureEmptyResult({ processing: { attachments: [{ status: 'empty', processed_chars: 0 }] } }).message, /沒有辨識到足夠文字/);
  assert.match(captureEmptyResult({ processing: { attachments: [{ status: 'complete', processed_chars: 42 }] } }).title, /內容已讀取/);
});
