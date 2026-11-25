/* Native browser actions use the PNG prepared at generation time, not after a click. */
(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  document.querySelectorAll('[data-edit-ctv]').forEach((button) => {
    button.addEventListener('click', () => {
      const details = byId(button.dataset.editCtv);
      details.open = true;
      details.querySelector('.row-edit').open = true;
      details.querySelector('input').focus();
    });
  });
  document.querySelectorAll('[data-delete-code]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(`Xóa CTV ${form.dataset.deleteCode}? Dữ liệu bài nộp cũ vẫn được giữ.`)) event.preventDefault();
    });
  });

  const form = byId('ctv-link-form');
  const output = byId('ctv-link-output');
  const status = byId('ctv-link-status');
  const preview = byId('ctv-qr-preview');
  const linkInput = byId('ctv-generated-link');
  const copyLink = byId('ctv-copy-link');
  const shareLink = byId('ctv-share-link');
  const copyImage = byId('ctv-copy-image');
  const shareImage = byId('ctv-share-image');
  const downloadImage = byId('ctv-download-image');
  const buttons = [copyLink, shareLink, copyImage, shareImage, downloadImage];
  let generation = 0;
  let current = null;
  function invalidate() {
    generation += 1;
    if (current) URL.revokeObjectURL(current.objectUrl);
    current = null;
    buttons.forEach((button) => { button.disabled = true; });
    output.hidden = true;
    linkInput.value = '';
    preview.replaceChildren();
    status.textContent = '';
  }
  form.addEventListener('change', invalidate);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    invalidate();
    const code = byId('ctv-link-code').value;
    const formId = byId('ctv-link-form-id').value;
    if (!code || !formId) return;
    const version = generation;
    const url = new URL(`/forms/${encodeURIComponent(formId)}`, window.location.origin);
    url.search = `?ref=${encodeURIComponent(code)}`;
    const link = url.href;
    output.hidden = false;
    linkInput.value = link;
    status.textContent = 'Đang chuẩn bị ảnh QR…';
    try {
      if (typeof QRCode === 'undefined') throw new Error('QR unavailable');
      const source = document.createElement('div');
      new QRCode(source, {
        text: link, width: 256, height: 256, colorDark: '#000000',
        colorLight: '#ffffff', correctLevel: QRCode.CorrectLevel.M,
      });
      const qrCanvas = source.querySelector('canvas');
      if (!qrCanvas) throw new Error('QR canvas unavailable');
      const canvas = document.createElement('canvas');
      // A 64px white edge exceeds four modules at the minimum 21-module size.
      canvas.width = canvas.height = 384;
      const context = canvas.getContext('2d');
      context.fillStyle = '#ffffff';
      context.fillRect(0, 0, 384, 384);
      context.drawImage(qrCanvas, 64, 64, 256, 256);
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', `Mã QR liên kết khảo sát cho CTV ${code}`);
      preview.replaceChildren(canvas);
      canvas.toBlob((blob) => {
        if (version !== generation) return;
        if (!blob) {
          status.textContent = 'Không tạo được ảnh PNG. Vui lòng thử lại.';
          return;
        }
        const file = new File([blob], 'ctv-qr.png', {type: 'image/png'});
        current = {link, blob, file, objectUrl: URL.createObjectURL(blob), version};
        copyLink.disabled = false;
        downloadImage.disabled = false;
        const supportsImageCopy = Boolean(window.isSecureContext && window.ClipboardItem && navigator.clipboard?.write
          && (!ClipboardItem.supports || ClipboardItem.supports('image/png')));
        copyImage.disabled = !supportsImageCopy;
        shareLink.disabled = !navigator.share;
        let supportsImageShare = false;
        try { supportsImageShare = Boolean(navigator.share && navigator.canShare?.({files: [file]})); } catch (_) { /* unsupported */ }
        shareImage.disabled = !supportsImageShare;
        const unavailable = [];
        if (!supportsImageCopy) unavailable.push('sao chép ảnh');
        if (!navigator.share) unavailable.push('chia sẻ liên kết');
        if (!supportsImageShare) unavailable.push('chia sẻ ảnh');
        status.textContent = 'Liên kết và ảnh PNG đã sẵn sàng.' + (unavailable.length
          ? ` Trình duyệt không hỗ trợ ${unavailable.join(', ')}; bạn có thể sao chép liên kết hoặc tải PNG.` : '');
      }, 'image/png');
    } catch (_) {
      status.textContent = 'Không tạo được mã QR. Vui lòng tải lại trang và thử lại.';
    }
  });

  async function perform(action, success) {
    const selected = current;
    if (!selected) return;
    try {
      await action(selected);
      if (current === selected) status.textContent = success;
    } catch (error) {
      if (current === selected) status.textContent = error.name === 'AbortError'
        ? 'Đã hủy chia sẻ; chưa gửi nội dung.'
        : 'Thao tác không thành công. Bạn có thể chọn liên kết để sao chép thủ công hoặc tải PNG.';
    }
  }
  copyLink.addEventListener('click', () => perform((selected) => {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(selected.link);
    linkInput.focus();
    linkInput.select();
    if (!document.execCommand('copy')) throw new Error('Copy failed');
  }, 'Đã sao chép liên kết.'));
  shareLink.addEventListener('click', () => perform((selected) => navigator.share({url: selected.link}), 'Đã chia sẻ liên kết.'));
  copyImage.addEventListener('click', () => perform((selected) => navigator.clipboard.write([
    new ClipboardItem({'image/png': selected.blob}),
  ]), 'Đã sao chép ảnh QR PNG.'));
  shareImage.addEventListener('click', () => perform((selected) => navigator.share({files: [selected.file]}), 'Đã chia sẻ ảnh QR.'));
  downloadImage.addEventListener('click', () => {
    if (!current) return;
    const anchor = document.createElement('a');
    anchor.href = current.objectUrl;
    anchor.download = current.file.name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    status.textContent = 'Đã yêu cầu tải ảnh PNG.';
  });
})();
