(() => {
  if (document.getElementById('adminTopBar')) return;
  const path = (location.pathname.replace(/\/+$/, '') || '/');
  // Recovery is public; don't show a signed-in menu on its public pages.
  if (path.startsWith('/admin/recovery') && path !== '/admin/recovery/email') return;
  const getTheme = () => (localStorage.getItem('tb_theme') === 'light' ? 'light' : 'dark');
  const applyTheme = (theme) => {
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('tb_theme', next); } catch (_) { /* ignore storage errors */ }
    return next;
  };
  applyTheme(getTheme());
  const role = document.querySelector('meta[name="admin-role"]')?.content;
  const superadmin = role === 'superadmin';
  const operator = superadmin || role === 'manager';
  const links = [
    ...(operator ? [['/admin/forms', 'nav_forms']] : []),
    ['/admin/submissions', 'nav_submissions'],
    ...(operator ? [
      ['/admin/audit', 'nav_audit'],
      ['/admin/media', 'nav_media'],
      ['/admin/ctvs', 'nav_ctvs'],
      ['/admin/image-objects', 'nav_image_objects'],
      ['/admin/modules', 'nav_modules'],
      ['/admin/exports', 'nav_exports'],
    ] : []),
    ['/admin/account', 'nav_account'],
    ['/logout', 'nav_logout'],
    ...(superadmin ? [
      ['/admin/accounts', 'nav_accounts'],
      ['/admin/trash', 'nav_trash'],
      ['/admin/recovery/email', 'nav_recovery_email'],
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
    #adminTopBar .bar-right { margin-left: auto; display: inline-flex; align-items: center; gap: 8px; }
    #adminTopBar .icon-toggle { padding: 7px 10px; min-height: 34px; }
    #adminTopBar .icon-toggle svg { display: block; }
    @media (max-width: 640px) { #adminTopBar .inner { padding: 6px 10px; }
      #adminTopBar .brand { display: none; }
      body > .container { padding: 16px 12px !important; }
      #adminTopBar .menu { left: 10px; width: min(320px, calc(100vw - 20px)); } }
  `;
  document.head.appendChild(style);
  const sunIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
  const moonIcon = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  const nav = document.createElement('nav');
  nav.id = 'adminTopBar';
  nav.setAttribute('aria-label', 'Điều hướng quản trị');
  nav.innerHTML = '<div class="inner">'
    + '<button type="button" aria-expanded="false" aria-controls="adminNavMenu"'
    + ' aria-label="Menu điều hướng quản trị"><span aria-hidden="true">&#9776;</span> Menu</button>'
    + '<span class="brand">Trust Bridge Admin</span>'
    + '<div class="bar-right">'
    + '<button type="button" id="adminThemeToggle" class="icon-toggle" aria-label="Chuyển giao diện sáng / tối" title="Sáng / Tối"></button>'
    + '<button type="button" id="adminLangToggle" class="icon-toggle" aria-label="Đổi ngôn ngữ / Toggle language" title="VI / EN"><span id="adminLangCode">VI</span></button>'
    + '</div>'
    + '<div id="adminNavMenu" class="menu" hidden>'
    + links.map(([href, key]) =>
      `<a href="${href}" data-i18n="${key}"${href === '/logout' ? ' data-logout="1"' : ''}${isActive(href) ? ' class="active" aria-current="page"' : ''}>${window.AdminI18n ? window.AdminI18n.t(key) : key}</a>`
    ).join('')
    + '</div></div>';
  const button = nav.querySelector('button');
  const menu = nav.querySelector('#adminNavMenu');
  const setOpen = (open) => {
    button.setAttribute('aria-expanded', String(open));
    menu.hidden = !open;
  };
  const themeButton = nav.querySelector('#adminThemeToggle');
  const syncThemeButton = () => {
    const isLight = getTheme() === 'light';
    themeButton.innerHTML = isLight ? moonIcon : sunIcon;
    themeButton.setAttribute('aria-pressed', String(!isLight));
    themeButton.title = isLight ? 'Chuyển sang giao diện tối' : 'Chuyển sang giao diện sáng';
  };
  syncThemeButton();
  themeButton.addEventListener('click', () => {
    applyTheme(getTheme() === 'light' ? 'dark' : 'light');
    syncThemeButton();
  });
  const langButton = nav.querySelector('#adminLangToggle');
  const langCode = nav.querySelector('#adminLangCode');
  const updateLangCode = () => {
    if (langCode) langCode.textContent = window.AdminI18n ? window.AdminI18n.getLang().toUpperCase() : 'VI';
  };
  if (langButton && window.AdminI18n) {
    langButton.addEventListener('click', () => window.AdminI18n.toggle());
    document.addEventListener('admin-language-change', () => {
      updateLangCode();
      nav.querySelectorAll('#adminNavMenu a[data-i18n]').forEach((link) => {
        link.textContent = window.AdminI18n.t(link.dataset.i18n);
      });
    });
    updateLangCode();
    window.AdminI18n.apply(nav);
  }
  button.addEventListener('click', () => setOpen(menu.hidden));
  menu.addEventListener('click', (event) => {
    if (event.target.closest('a[data-logout]')) {
      event.preventDefault();
      const form = document.createElement('form');
      form.method = 'post';
      form.action = '/logout';
      document.body.appendChild(form);
      form.submit();
      return;
    }
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

  const tr = (text) => (window.AdminI18n?.translateText ? window.AdminI18n.translateText(text) : text);

  function confirmModal({ title, message, confirmLabel = 'Xác nhận', cancelLabel = 'Hủy', danger = false, onConfirm }) {
    let backdrop = document.getElementById('tbConfirmModal');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'tbConfirmModal';
      backdrop.className = 'tb-modal-backdrop';
      backdrop.setAttribute('role', 'dialog');
      backdrop.setAttribute('aria-modal', 'true');
      backdrop.innerHTML = '<div class="tb-modal-card"><h3 id="tbModalTitle"></h3><p id="tbModalMessage"></p>'
        + '<div class="tb-modal-actions"><button type="button" class="secondary" id="tbModalCancel"></button>'
        + '<button type="button" id="tbModalConfirm"></button></div></div>';
      document.body.appendChild(backdrop);
    }
    const titleEl = backdrop.querySelector('#tbModalTitle');
    const msgEl = backdrop.querySelector('#tbModalMessage');
    const cancelBtn = backdrop.querySelector('#tbModalCancel');
    const confirmBtn = backdrop.querySelector('#tbModalConfirm');
    titleEl.textContent = tr(title);
    msgEl.textContent = tr(message);
    cancelBtn.textContent = tr(cancelLabel);
    confirmBtn.textContent = tr(confirmLabel);
    confirmBtn.className = danger ? 'danger' : '';
    backdrop.hidden = false;
    const close = () => { backdrop.hidden = true; };
    cancelBtn.onclick = close;
    backdrop.onclick = (e) => { if (e.target === backdrop) close(); };
    confirmBtn.onclick = () => { close(); if (onConfirm) onConfirm(); };
    confirmBtn.focus();
  }

  function enhanceSelect(select) {
    if (!select || select.dataset.tbEnhanced || select.hasAttribute('data-native-select') || select.multiple) return;
    select.dataset.tbEnhanced = '1';
    const wrapper = document.createElement('div');
    wrapper.className = 'tb-combobox';
    const trigger = document.createElement('div');
    trigger.className = 'tb-combobox-trigger';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'tb-combobox-input';
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.autocomplete = 'off';
    const caret = document.createElement('span');
    caret.className = 'tb-combobox-caret';
    caret.setAttribute('aria-hidden', 'true');
    const panel = document.createElement('div');
    panel.className = 'tb-combobox-panel';
    panel.hidden = true;
    panel.setAttribute('role', 'listbox');
    trigger.append(input, caret);
    wrapper.append(trigger, panel);
    select.style.display = 'none';
    select.parentNode.insertBefore(wrapper, select.nextSibling);

    const syncLabel = () => {
      const opt = select.options[select.selectedIndex];
      input.value = opt ? opt.textContent : '';
      input.placeholder = opt ? opt.textContent : tr('Chọn...');
    };
    const renderOptions = (query = '') => {
      const q = query.trim().toLowerCase();
      panel.replaceChildren();
      let count = 0;
      Array.from(select.options).forEach((opt) => {
        if (opt.disabled && !opt.value) return;
        const label = opt.textContent || '';
        if (q && !label.toLowerCase().includes(q) && !String(opt.value || '').toLowerCase().includes(q)) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tb-combobox-option';
        btn.textContent = label;
        btn.setAttribute('role', 'option');
        btn.setAttribute('aria-selected', String(opt.value === select.value));
        btn.addEventListener('mousedown', (e) => e.preventDefault());
        btn.addEventListener('click', () => {
          select.value = opt.value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
          syncLabel();
          setOpenPanel(false);
        });
        panel.appendChild(btn);
        count += 1;
      });
      if (!count) {
        const empty = document.createElement('div');
        empty.className = 'muted';
        empty.style.padding = '8px 10px';
        empty.textContent = tr('Không tìm thấy kết quả');
        panel.appendChild(empty);
      }
    };
    const setOpenPanel = (open) => {
      wrapper.classList.toggle('open', open);
      panel.hidden = !open;
      input.setAttribute('aria-expanded', String(open));
      if (open) renderOptions('');
      else syncLabel();
    };
    input.addEventListener('focus', () => {
      input.select();
      setOpenPanel(true);
    });
    input.addEventListener('input', () => {
      if (panel.hidden) setOpenPanel(true);
      renderOptions(input.value);
    });
    caret.addEventListener('click', () => {
      if (panel.hidden) { input.focus(); setOpenPanel(true); }
      else setOpenPanel(false);
    });
    document.addEventListener('click', (e) => {
      if (!wrapper.contains(e.target) && !panel.hidden) setOpenPanel(false);
    });
    select.addEventListener('change', syncLabel);
    document.addEventListener('admin-language-change', () => {
      if (panel.hidden) syncLabel();
      else renderOptions(input.value);
    });
    syncLabel();
  }

  function initPagination(container) {
    if (!container || container.dataset.tbPaginated) return null;
    container.dataset.tbPaginated = '1';
    const itemSelector = container.dataset.tbPaginate || ':scope > *';
    let pageSize = parseInt(container.dataset.pageSize || '10', 10) || 10;
    let currentPage = 1;
    const bar = document.createElement('div');
    bar.className = 'tb-pagination';
    bar.innerHTML = '<div class="tb-page-summary"></div>'
      + '<div class="tb-pagination-controls">'
      + '<label style="margin:0;display:inline-flex;align-items:center;gap:6px;"><span>Hiển thị mỗi trang:</span>'
      + '<select data-native-select class="tb-page-size"><option value="10">10</option><option value="20">20</option><option value="50">50</option></select></label>'
      + '<button type="button" data-pg="first">« Đầu</button>'
      + '<button type="button" data-pg="prev">‹ Trước</button>'
      + '<span class="tb-page-current" style="padding:0 6px;font-weight:600;color:var(--text);"></span>'
      + '<button type="button" data-pg="next">Sau ›</button>'
      + '<button type="button" data-pg="last">Cuối »</button>'
      + '</div>';
    container.insertAdjacentElement('afterend', bar);
    const sizeSel = bar.querySelector('.tb-page-size');
    sizeSel.value = String(pageSize);
    const summaryEl = bar.querySelector('.tb-page-summary');
    const currentEl = bar.querySelector('.tb-page-current');
    const render = () => {
      const allItems = Array.from(container.querySelectorAll(itemSelector));
      const matching = allItems.filter((el) => el.dataset.filterHidden !== '1');
      const total = matching.length;
      const totalPages = Math.max(1, Math.ceil(total / pageSize));
      currentPage = Math.max(1, Math.min(currentPage, totalPages));
      const start = (currentPage - 1) * pageSize;
      const end = Math.min(start + pageSize, total);
      allItems.forEach((el) => { el.hidden = true; });
      matching.slice(start, end).forEach((el) => { el.hidden = false; });
      summaryEl.textContent = tr(`Hiển thị ${total ? start + 1 : 0} - ${end} trên tổng ${total} bản ghi`);
      currentEl.textContent = tr(`Trang ${currentPage} / ${totalPages}`);
      bar.querySelector('[data-pg="first"]').disabled = currentPage <= 1;
      bar.querySelector('[data-pg="prev"]').disabled = currentPage <= 1;
      bar.querySelector('[data-pg="next"]').disabled = currentPage >= totalPages;
      bar.querySelector('[data-pg="last"]').disabled = currentPage >= totalPages;
    };
    sizeSel.addEventListener('change', () => {
      pageSize = parseInt(sizeSel.value, 10) || 10;
      currentPage = 1;
      render();
    });
    bar.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-pg]');
      if (!btn || btn.disabled) return;
      const allMatching = Array.from(container.querySelectorAll(itemSelector)).filter((el) => el.dataset.filterHidden !== '1');
      const totalPages = Math.max(1, Math.ceil(allMatching.length / pageSize));
      const action = btn.dataset.pg;
      if (action === 'first') currentPage = 1;
      else if (action === 'prev') currentPage = Math.max(1, currentPage - 1);
      else if (action === 'next') currentPage = Math.min(totalPages, currentPage + 1);
      else if (action === 'last') currentPage = totalPages;
      render();
    });
    document.addEventListener('admin-language-change', render);
    container._tbPaginateRender = render;
    render();
    return render;
  }

  window.AdminUI = { confirmModal, enhanceSelect, initPagination };
  const bootstrapUI = () => {
    document.querySelectorAll('[data-tb-paginate]').forEach(initPagination);
    document.querySelectorAll('select[data-searchable-select]').forEach(enhanceSelect);
    document.querySelectorAll('[data-manage-account-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = document.getElementById(btn.dataset.manageAccountToggle);
        if (!row) return;
        row.hidden = !row.hidden;
        btn.setAttribute('aria-expanded', String(!row.hidden));
      });
    });
    document.querySelectorAll('[data-role-add-toggle]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const picker = document.getElementById(btn.dataset.roleAddToggle);
        if (!picker) return;
        picker.hidden = !picker.hidden;
      });
    });
    document.querySelectorAll('[data-confirm-logout]').forEach((btn) => {
      btn.addEventListener('click', () => {
        confirmModal({
          title: 'Xác nhận đăng xuất',
          message: 'Bạn có chắc muốn đăng xuất khỏi tài khoản quản trị trên thiết bị này?',
          confirmLabel: 'Đăng xuất',
          cancelLabel: 'Hủy',
          danger: true,
          onConfirm: () => {
            const form = document.createElement('form');
            form.method = 'post';
            form.action = '/logout';
            document.body.appendChild(form);
            form.submit();
          },
        });
      });
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootstrapUI);
  else bootstrapUI();
})();
