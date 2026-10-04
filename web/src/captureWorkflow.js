export function captureRequestFailureMessage(sourceUrl, status = 0) {
  if (status === 413) return '這次資料超出可整理大小。請縮短文字或減少附件後重試。';
  if (sourceUrl && (status === 400 || status === 422)) {
    return '無法讀取這個網址。請確認網址完整且可公開存取，或直接貼上頁面文字。';
  }
  if (sourceUrl && status >= 500) {
    return '目前無法讀取或整理這個網頁。請稍後重試，或直接貼上頁面文字。';
  }
  if (status >= 500) return '目前無法完成整理。請稍後重試，或檢查文字與附件後再試。';
  if (!status) return '目前連不到整理服務。請稍後重試。';
  return '這些資料目前無法整理。請確認文字及附件格式，再重試。';
}

export function captureEmptyResult(source) {
  const sourceType = source?.source_type || source?.type;
  const webpageStatus = source?.processing?.webpage?.status;
  const attachmentStages = Array.isArray(source?.processing?.attachments) ? source.processing.attachments : [];
  if (attachmentStages.length) {
    const processedChars = attachmentStages.reduce((sum, item) => sum + Number(item?.processed_chars || 0), 0);
    const statuses = new Set(attachmentStages.map((item) => String(item?.status || '').toLowerCase()));
    if (!processedChars && [...statuses].some((status) => ['timeout', 'extraction_failed', 'malformed', 'unsupported'].includes(status))) {
      return {
        title: '圖片或檔案沒有成功讀取',
        message: '檔案已上傳，但文字辨識或內容讀取沒有完成。請重新嘗試，或換一個較清晰、受支援的檔案。',
      };
    }
    if (!processedChars && statuses.has('empty')) {
      return {
        title: '沒有從圖片或檔案辨識到文字',
        message: '檔案已上傳完成，但沒有辨識到足夠文字。請確認內容清晰，並包含行程或待辦資訊。',
      };
    }
    if (processedChars > 0) {
      return {
        title: '內容已讀取，但沒有找到可整理的行程或待辦',
        message: '已成功讀取附件文字，但目前沒有辨識出明確的日期、時間或待辦事項。',
      };
    }
  }
  if (sourceType === 'url' && webpageStatus === 'failed') {
    return {
      title: '無法讀取這個網址',
      message: '請確認網址完整且可公開存取，或直接貼上頁面中需要整理的文字。',
    };
  }
  if (sourceType === 'url' && webpageStatus === 'unavailable') {
    return {
      title: '目前無法讀取這個網頁',
      message: '請稍後重試，或直接貼上頁面中需要整理的文字。',
    };
  }
  if (sourceType === 'url' && webpageStatus === 'complete') {
    return {
      title: '網頁已讀取，但沒有找到可整理的行程或待辦',
      message: '請確認頁面包含課程時間或待辦資訊，也可以直接貼上相關段落。',
    };
  }
  return {
    title: '目前找不到可整理的行程或待辦',
    message: '請貼上包含明確行程或待辦內容的原始資訊，再試一次。',
  };
}
