(() => {
  if (window.AdminI18n) return;
  const messages = {
    vi: {
      nav_forms: 'Biểu mẫu', nav_submissions: 'Dữ liệu nộp', nav_audit: 'Audit',
      nav_media: 'Media', nav_ctvs: 'CTV', nav_image_objects: 'Ảnh/Object',
      nav_modules: 'Module', nav_exports: 'Exports', nav_account: 'Tài khoản của tôi',
      nav_accounts: 'Quản lý tài khoản', nav_trash: 'Thùng rác', nav_recovery_email: 'Email khôi phục',
      accounts_title: 'Quản lý tài khoản quản trị', account_title: 'Tài khoản của tôi',
      logged_in: 'Đang đăng nhập:',
      account_create_heading: 'Tạo tài khoản quản trị',
      username: 'Tên đăng nhập', role: 'Vai trò', password: 'Mật khẩu',
      password_hint: 'Mật khẩu (12–1024 ký tự ASCII in được)',
      confirm_password: 'Nhập lại mật khẩu',
      create_account: 'Tạo tài khoản',
      role_section_heading: 'Phân quyền tài khoản',
      role_section_hint: 'Chọn tài khoản và vai trò mới. Không thể tự hạ vai trò hoặc xóa superadmin cuối cùng.',
      account: 'Tài khoản', new_role: 'Vai trò mới', choose_role: 'Chọn vai trò',
      update_role: 'Cập nhật vai trò',
      accounts_table_heading: 'Tài khoản quản trị',
      th_username: 'Tên đăng nhập', th_role: 'Vai trò', th_status: 'Trạng thái',
      th_created: 'Tạo lúc', th_actions: '',
      status_on: 'Hoạt động', status_off: 'Đã tắt',
      manage: 'Quản lý', update_role_short: 'Cập nhật vai trò', empty_accounts: 'Không có tài khoản.',
      deactivate_account: 'Tắt tài khoản', activate_account: 'Bật tài khoản',
      delete_account: 'Xóa tài khoản', restore: 'Khôi phục',
      open_my_account: 'Mở trang tài khoản của tôi',
      trash_heading: 'Thùng rác',
      trash_hint: 'Có thể khôi phục tài khoản trong vòng 30 ngày sau khi xóa.',
      th_deleted_at: 'Xóa lúc', th_deleted_by: 'Người xóa',
      account_self_heading: 'Tài khoản của tôi',
      change_password_heading: 'Đổi mật khẩu',
      current_password: 'Mật khẩu hiện tại',
      new_password_hint: 'Mật khẩu mới (12–1024 ký tự ASCII in được)',
      confirm_new_password: 'Nhập lại mật khẩu mới',
      change_password: 'Đổi mật khẩu',
      role_superadmin: 'Quản trị tối cao', role_manager: 'Quản lý',
      role_reviewer: 'Kiểm duyệt', role_viewer: 'Chỉ xem',
      msg_password_changed: 'Đã đổi mật khẩu. Dùng mật khẩu mới cho lần đăng nhập sau.',
      msg_account_created: 'Đã tạo tài khoản quản trị.',
      msg_account_deactivated: 'Đã tắt tài khoản quản trị.',
      msg_account_activated: 'Đã bật tài khoản quản trị.',
      msg_account_role_changed: 'Đã đổi vai trò tài khoản quản trị.',
      msg_account_deleted: 'Đã chuyển tài khoản vào thùng rác.',
      msg_account_restored: 'Đã khôi phục và bật tài khoản quản trị.',
      err_current_password: 'Mật khẩu hiện tại không đúng.',
      err_password_policy: 'Mật khẩu mới phải dài từ 12 đến 1024 ký tự ASCII in được.',
      err_password_mismatch: 'Xác nhận mật khẩu không khớp.',
      err_same_password: 'Mật khẩu mới phải khác mật khẩu hiện tại.',
      err_account_exists: 'Tên đăng nhập đã tồn tại.',
      err_invalid_username: 'Tên đăng nhập phải dài 3-32 ký tự, chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch ngang.',
      err_invalid_role: 'Vai trò không hợp lệ.',
      err_last_admin: 'Phải giữ ít nhất một superadmin đang hoạt động.',
      err_self_change: 'Không thể tự tắt, xóa hoặc hạ vai trò của mình.',
      err_not_found: 'Không tìm thấy tài khoản.',
      err_db_unavailable: 'Cơ sở dữ liệu không khả dụng.'
    },
    en: {
      nav_forms: 'Forms', nav_submissions: 'Submissions', nav_audit: 'Audit',
      nav_media: 'Media', nav_ctvs: 'CTVs', nav_image_objects: 'Images/Objects',
      nav_modules: 'Modules', nav_exports: 'Exports', nav_account: 'My account',
      nav_accounts: 'Manage accounts', nav_trash: 'Trash', nav_recovery_email: 'Recovery email',
      accounts_title: 'Admin account management', account_title: 'My account',
      logged_in: 'Signed in as:',
      account_create_heading: 'Create admin account',
      username: 'Username', role: 'Role', password: 'Password',
      password_hint: 'Password (12–1024 printable ASCII characters)',
      confirm_password: 'Confirm password',
      create_account: 'Create account',
      role_section_heading: 'Account roles',
      role_section_hint: 'Choose an account and a new role. You cannot demote yourself or remove the last superadmin.',
      account: 'Account', new_role: 'New role', choose_role: 'Choose a role',
      update_role: 'Update role',
      accounts_table_heading: 'Admin accounts',
      th_username: 'Username', th_role: 'Role', th_status: 'Status',
      th_created: 'Created', th_actions: '',
      status_on: 'Active', status_off: 'Disabled',
      manage: 'Manage', update_role_short: 'Update role', empty_accounts: 'No accounts.',
      deactivate_account: 'Disable account', activate_account: 'Enable account',
      delete_account: 'Delete account', restore: 'Restore',
      open_my_account: 'Open my account page',
      trash_heading: 'Trash',
      trash_hint: 'Deleted accounts can be restored for 30 days.',
      th_deleted_at: 'Deleted', th_deleted_by: 'Deleted by',
      account_self_heading: 'My account',
      change_password_heading: 'Change password',
      current_password: 'Current password',
      new_password_hint: 'New password (12–1024 printable ASCII characters)',
      confirm_new_password: 'Confirm new password',
      change_password: 'Change password',
      role_superadmin: 'Superadmin', role_manager: 'Manager',
      role_reviewer: 'Reviewer', role_viewer: 'Viewer',
      msg_password_changed: 'Password changed. Use the new password next time.',
      msg_account_created: 'Admin account created.',
      msg_account_deactivated: 'Admin account disabled.',
      msg_account_activated: 'Admin account enabled.',
      msg_account_role_changed: 'Admin account role changed.',
      msg_account_deleted: 'Admin account moved to trash.',
      msg_account_restored: 'Admin account restored and enabled.',
      err_current_password: 'Current password is incorrect.',
      err_password_policy: 'The new password must be 12–1024 printable ASCII characters.',
      err_password_mismatch: 'Password confirmation does not match.',
      err_same_password: 'The new password must differ from the current password.',
      err_account_exists: 'That username already exists.',
      err_invalid_username: 'Username must be 3-32 characters: letters, numbers, dot, underscore, or hyphen.',
      err_invalid_role: 'Invalid role.',
      err_last_admin: 'At least one active superadmin must remain.',
      err_self_change: 'You cannot disable, delete, or demote your own account.',
      err_not_found: 'Account not found.',
      err_db_unavailable: 'Database unavailable.'
    }
  };
  const getLang = () => localStorage.getItem('tb_lang') === 'en' ? 'en' : 'vi';
  const t = (key) => (messages[getLang()] && messages[getLang()][key]) || messages.vi[key] || key;
  function apply(root = document) {
    root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    root.querySelectorAll('[data-role-label]').forEach(el => {
      const role = el.dataset.roleLabel || '';
      el.textContent = t('role_' + role);
    });
    root.querySelectorAll('[data-account-option]').forEach(el => {
      const username = el.dataset.accountUsername || '';
      const role = el.dataset.accountRole || '';
      el.textContent = username + ' (' + t('role_' + role) + ')';
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
  }
  function setLang(next) {
    localStorage.setItem('tb_lang', next === 'en' ? 'en' : 'vi');
    document.documentElement.lang = getLang();
    apply();
    document.dispatchEvent(new CustomEvent('admin-language-change', { detail: { lang: getLang() } }));
  }
  window.AdminI18n = {
    t, apply, getLang, setLang,
    toggle: () => setLang(getLang() === 'vi' ? 'en' : 'vi')
  };
  document.addEventListener('DOMContentLoaded', () => {
    document.documentElement.lang = getLang();
    apply();
  });
})();
