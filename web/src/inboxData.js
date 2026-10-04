export const inboxLabels = Object.freeze({
  page: '收到的資訊',
  capture: '新增資訊',
  proposals: '待你確認',
  source: '原始資訊',
});

export function reviewableProposals(proposals = []) {
  return proposals.filter((proposal) => ['pending', 'edited', 'accepted'].includes(proposal?.status));
}

export function proposalsForSource(proposals = [], source) {
  if (!source?.id) return [];
  return proposals.filter((proposal) => String(proposal.source_id) === String(source.id));
}

export function sourceOriginalText(source) {
  return source?.body?.trim() || source?.excerpt?.trim() || '';
}

export function sourceDetailsFromPayload(payload, requestedSourceId) {
  if (!payload || requestedSourceId == null) return null;
  const source = payload.source || payload.notice || payload;
  const sourceId = source?.id || payload.id;
  if (!source || String(sourceId || '') !== String(requestedSourceId)) return null;
  return {
    ...source,
    id: String(sourceId),
    body: source.body ?? payload.body ?? '',
  };
}

export function sourceIdForRetry(source, requestedSourceId) {
  const sourceId = source?.id || requestedSourceId;
  return sourceId == null ? '' : String(sourceId);
}

export function canViewSource(sourceId) {
  return Boolean(String(sourceId ?? '').trim()) && String(sourceId).trim() !== 'manual';
}

export function sourceAttachmentUrl(source, attachment, apiBase = '/api') {
  if (attachment?.url) return attachment.url;
  if (!source?.id || !attachment?.id || attachment.raw_available === false) return '';
  return `${apiBase.replace(/\/$/, '')}/sources/${encodeURIComponent(source.id)}/attachments/${encodeURIComponent(attachment.id)}`;
}
