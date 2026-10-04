export function acceptedProposals(proposals = []) {
  return proposals.filter((proposal) => {
    if (!proposal?.id || proposal.status !== 'accepted' || proposal.needs_review) return false;
    if (!['event', 'task'].includes(proposal.target_type)) return false;

    const patch = proposal.patch || {};
    if (proposal.operation === 'ignore') return true;
    if (proposal.operation === 'create') {
      return proposal.target_type === 'event'
        ? Boolean(patch.title && (patch.date || patch.start_at) && (hasTimedRange(patch) || hasTimedStart(patch) || patch.all_day === true))
        : Boolean(patch.title);
    }
    if (proposal.operation !== 'update' || !proposal.target_id) return false;

    const applicableFields = proposal.target_type === 'event'
      ? ['title', 'location', 'date', 'time', 'start_at', 'end_at', 'all_day', 'recurrence', 'detail']
      : ['title', 'due', 'due_label', 'notes', 'list_id', 'date', 'time', 'start_at', 'end_at', 'all_day'];
    return applicableFields.some((field) => Object.hasOwn(patch, field));
  });
}

function hasTimedRange(patch) {
  if (patch.start_at && patch.end_at) return true;
  return (String(patch.time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || []).length >= 2;
}

function hasTimedStart(patch) {
  if (patch.end_at) return false;
  if (patch.start_at) return true;
  return (String(patch.time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || []).length === 1;
}

export function isPreviewableProposal(proposal) {
  if (proposal?.operation !== 'create' || proposal?.target_type !== 'event') return true;
  const patch = proposal.patch || {};
  return [patch.title, patch.date, patch.time, patch.start_at, patch.end_at]
    .some((value) => typeof value === 'string' && value.trim().length > 0);
}

export function confirmableProposals(proposals = []) {
  return proposals.filter((proposal) => {
    if (!proposal?.id || proposal.needs_review || !['event', 'task'].includes(proposal.target_type)) return false;
    if (!['pending', 'edited', 'accepted'].includes(proposal.status)) return false;
    if (proposal.operation === 'ignore') return false;
    if (proposal.operation === 'create') {
      const patch = proposal.patch || {};
      return proposal.target_type === 'event'
        ? Boolean(patch.title && (patch.date || patch.start_at) && (hasTimedRange(patch) || hasTimedStart(patch) || patch.all_day === true))
        : Boolean(patch.title);
    }
    if (proposal.operation !== 'update' || !proposal.target_id) return false;
    const patch = proposal.patch || {};
    const touchesSchedule = ['date', 'time', 'start_at', 'end_at', 'all_day'].some((key) => Object.hasOwn(patch, key));
    const hasTargetTime = (String(proposal.target_time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || []).length >= 2;
    const hasTargetStart = !proposal.target_end_at && (String(proposal.target_time || '').match(/(?<!\d)\d{1,2}:\d{2}(?!\d)/g) || []).length === 1;
    if (proposal.target_type === 'event' && touchesSchedule && !(hasTimedRange(patch) || hasTimedStart(patch) || hasTargetTime || hasTargetStart || patch.all_day === true)) return false;
    return acceptedProposals([{ ...proposal, status: 'accepted' }]).length === 1;
  });
}

export function buildApplyResults(proposals = [], outcomes = new Map()) {
  return proposals
    .filter((proposal) => outcomes.has(proposal?.id) || ['pending', 'edited', 'accepted'].includes(proposal?.status))
    .map((proposal) => {
      const outcome = outcomes.get(proposal.id);
      if (outcome) return { id: proposal.id, title: proposal.patch?.title || proposal.target_title || '未命名變更', ...outcome };
      return {
        id: proposal.id,
        title: proposal.patch?.title || proposal.target_title || '未命名變更',
        status: 'unprocessed',
        message: proposal.status === 'accepted' ? '尚未套用' : proposal.needs_review ? '需要先補充或確認，未套用' : '尚未確認加入',
      };
    });
}

export function captureDraftAfterInterpret(draft, source) {
  return {
    ...draft,
    source_id: source?.id || draft.source_id || null,
    attachments: [...(draft.attachments || [])],
  };
}

export function sourceHeading(source) {
  if (!source) return '原始內容';
  return source.title || source.name || source.source_name || source.filename || '原始內容';
}

export function sourceTypeLabel(source) {
  const type = String(source?.source_type || source?.type || source?.kind || '').toLowerCase();
  return ({ manual: '貼上文字', text: '貼上文字', message: '貼上文字', url: '網頁連結', webpage: '網頁連結', image: '圖片', pdf: 'PDF 文件', file: '附件', import: '附件', notice: '原始資訊' })[type] || '原始資訊';
}

export function groupPendingProposalsBySource(proposals = []) {
  const groups = new Map();
  proposals.forEach((proposal, index) => {
    const sourceId = proposal?.source_id && proposal.source_id !== 'manual' ? String(proposal.source_id) : null;
    const key = sourceId ? `source:${sourceId}` : `proposal:${proposal?.id || index}`;
    if (!groups.has(key)) groups.set(key, { id: key, sourceId, proposals: [] });
    groups.get(key).proposals.push(proposal);
  });

  return [...groups.values()].map((group) => {
    const firstProposal = group.proposals[0] || {};
    return {
      ...group,
      count: group.proposals.length,
      title: firstProposal.source_title || firstProposal.source_label || firstProposal.patch?.title || firstProposal.target_title || '一則資訊',
      eventCount: group.proposals.filter((proposal) => proposal.target_type === 'event').length,
      taskCount: group.proposals.filter((proposal) => proposal.target_type === 'task').length,
      unresolvedCount: group.proposals.filter((proposal) => proposal.needs_review).length,
    };
  });
}
