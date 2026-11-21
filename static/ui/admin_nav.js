(() => {
  if (document.getElementById('adminTopBar')) return;
  const links = [
    ['/admin/forms', 'Biểu mẫu'],
    ['/admin/submissions', 'Dữ liệu nộp'],
    ['/admin/audit', 'Audit'],
    ['/admin/media', 'Media'],
    ['/admin/ctvs', 'CTV'],
    ['/admin/image-objects', 'Ảnh/Object'],
    ['/admin/accounts', 'Tài khoản'],
  ];
  const path = (location.pathname.replace(/\/+$/, '') || '/');
  const isActive = (href) => {
    if (href === '/admin/forms') {
      return path === '/admin' || path === '/admin/forms' || path.startsWith('/admin/forms/');
    }
    if (href === '/admin/submissions') {
      return path === '/admin/submissions' || path.startsWith('/admin/submissions/')
        || path === '/admin/data' || path.startsWith('/admin/data/');
    }
    return path === href || path.startsWith(href + '/');
  };
  const style = document.createElement('style');
  style.textContent = `
    #adminTopBar { position: sticky; top: 0; z-index: 10000; background: #0b1220;
      border-bottom: 1px solid #23314f; font-family: -apple-system, BlinkMacSystemFont,
      "Segoe UI", Roboto, Arial, sans-serif; }
    #adminTopBar .inner { max-width: 1440px; margin: 0 auto; display: flex; align-items: center;
      gap: 4px; padding: 8px 16px; overflow-x: auto; }
    #adminTopBar .brand { color: #60a5fa; font-weight: 800; font-size: .82rem;
      margin-right: 12px; white-space: nowrap; }
    #adminTopBar a { color: #94a3b8; text-decoration: none; padding: 6px 10px;
      border-radius: 7px; font-size: .82rem; font-weight: 600; white-space: nowrap; }
    #adminTopBar a:hover { background: #1a253e; color: #f8fafc; }
    #adminTopBar a.active { background: #2563eb; color: #fff; }
    @media (max-width: 640px) { #adminTopBar .inner { padding: 6px 10px; }
      #adminTopBar .brand { display: none; } }
  `;
  document.head.appendChild(style);
  const nav = document.createElement('nav');
  nav.id = 'adminTopBar';
  nav.innerHTML = '<div class="inner"><span class="brand">Trust Bridge Admin</span>'
    + links.map(([href, label]) =>
      `<a href="${href}"${isActive(href) ? ' class="active"' : ''}>${label}</a>`
    ).join('')
    + '</div>';
  document.body.insertBefore(nav, document.body.firstChild);
})();
