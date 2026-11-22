(() => {
  if (document.getElementById('adminTopBar')) return;
  const path = (location.pathname.replace(/\/+$/, '') || '/');
  // Recovery is public; don't show a signed-in menu on its public pages.
  if (path.startsWith('/admin/recovery') && path !== '/admin/recovery/email') return;
  const role = document.querySelector('meta[name="admin-role"]')?.content;
  const superadmin = role === 'superadmin';
  const operator = superadmin || role === 'manager';
  const links = [
    ...(operator ? [['/admin/forms', 'Biểu mẫu']] : []),
    ['/admin/submissions', 'Dữ liệu nộp'],
    ...(operator ? [
      ['/admin/audit', 'Audit'],
      ['/admin/media', 'Media'],
      ['/admin/ctvs', 'CTV'],
      ['/admin/image-objects', 'Ảnh/Object'],
      ['/admin/modules', 'Module'],
      ['/admin/exports', 'Exports'],
    ] : []),
    ['/admin/accounts/password', 'Đổi mật khẩu'],
    ...(superadmin ? [
      ['/admin/accounts', 'Tài khoản'],
      ['/admin/recovery/email', 'Email khôi phục'],
    ] : []),
  ];
  const isActive = (href) => {
    if (href === '/admin/forms') {
      return path === '/admin' || path === '/admin/forms' || path.startsWith('/admin/forms/');
    }
    if (href === '/admin/submissions') {
      return path === '/admin/submissions' || path.startsWith('/admin/submissions/')
        || path === '/admin/data' || path.startsWith('/admin/data/');
    }
    if (href === '/admin/accounts' && path === '/admin/accounts/password') return false;
    return path === href || path.startsWith(href + '/');
  };
  const style = document.createElement('style');
  style.textContent = `
    body { padding: 0 !important; }
    body > .container { width: 100% !important; padding: 20px 16px !important; }
    #adminTopBar { position: sticky; top: 0; z-index: 10000; background: #0b1220;
      border-bottom: 1px solid #23314f; font-family: -apple-system, BlinkMacSystemFont,
      "Segoe UI", Roboto, Arial, sans-serif; }
    #adminTopBar .inner { position: relative; max-width: 1440px; margin: 0 auto;
      display: flex; align-items: center; gap: 12px; padding: 8px 16px; }
    #adminTopBar .brand { color: #60a5fa; font-weight: 800; font-size: .82rem;
      white-space: nowrap; }
    #adminTopBar button { display: inline-flex; align-items: center; gap: 8px;
      border: 1px solid #475569; background: #0b1220; color: #f8fafc;
      border-radius: 7px; padding: 8px 10px; font: inherit; cursor: pointer; }
    #adminTopBar .menu { position: absolute; top: 100%; left: 16px;
      display: flex; flex-direction: column; width: min(320px, calc(100vw - 32px));
      max-height: 70vh; overflow-y: auto; box-sizing: border-box; padding: 8px;
      background: #0b1220; border: 1px solid #23314f; border-radius: 7px; }
    #adminTopBar .menu[hidden] { display: none; }
    #adminTopBar a { color: #94a3b8; text-decoration: none; padding: 10px;
      border-radius: 7px; font-size: .82rem; font-weight: 600; }
    #adminTopBar a:hover, #adminTopBar button:hover { background: #1a253e; color: #f8fafc; }
    #adminTopBar a:focus-visible, #adminTopBar button:focus-visible {
      outline: 2px solid #60a5fa; outline-offset: 2px; }
    #adminTopBar a.active { background: #2563eb; color: #fff; }
    @media (max-width: 640px) { #adminTopBar .inner { padding: 6px 10px; }
      #adminTopBar .brand { display: none; }
      body > .container { padding: 16px 12px !important; }
      #adminTopBar .menu { left: 10px; width: min(320px, calc(100vw - 20px)); } }
  `;
  document.head.appendChild(style);
  const nav = document.createElement('nav');
  nav.id = 'adminTopBar';
  nav.setAttribute('aria-label', 'Điều hướng quản trị');
  nav.innerHTML = '<div class="inner">'
    + '<button type="button" aria-expanded="false" aria-controls="adminNavMenu"'
    + ' aria-label="Menu điều hướng quản trị"><span aria-hidden="true">&#9776;</span> Menu</button>'
    + '<span class="brand">Trust Bridge Admin</span>'
    + '<div id="adminNavMenu" class="menu" hidden>'
    + links.map(([href, label]) =>
      `<a href="${href}"${isActive(href) ? ' class="active" aria-current="page"' : ''}>${label}</a>`
    ).join('')
    + '</div></div>';
  const button = nav.querySelector('button');
  const menu = nav.querySelector('#adminNavMenu');
  const setOpen = (open) => {
    button.setAttribute('aria-expanded', String(open));
    menu.hidden = !open;
  };
  button.addEventListener('click', () => setOpen(menu.hidden));
  menu.addEventListener('click', (event) => {
    if (event.target.closest('a')) setOpen(false);
  });
  document.addEventListener('click', (event) => {
    if (!nav.contains(event.target)) setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !menu.hidden) {
      event.preventDefault();
      setOpen(false);
      button.focus();
    }
  });
  nav.addEventListener('focusout', (event) => {
    if (!nav.contains(event.relatedTarget)) setOpen(false);
  });
  document.body.insertBefore(nav, document.body.firstChild);
})();
