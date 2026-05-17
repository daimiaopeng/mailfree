/**
 * 管理员页面
 * @module admin
 */

import { api, getUsers, createUser, updateUser, deleteUser, getUserMailboxes, getSystemHealth, getAnalytics, assignMailbox, unassignMailbox } from './modules/admin/api.js';
import { formatTime, renderUserRow, renderUserList, generateSkeletonRows, renderPagination } from './modules/admin/user-list.js';
import { fillEditForm, collectEditFormData, validateEditForm, resetEditState } from './modules/admin/user-edit.js';

// showToast 由 toast-utils.js 全局提供
const showToast = window.showToast || ((msg, type) => console.log(`[${type}] ${msg}`));

// 分页状态
let currentPage = 1, pageSize = 20, totalUsers = 0;
let currentViewingUser = null;
let mailboxPage = 1, mailboxPageSize = 20, totalMailboxes = 0;
let analyticsRange = '30d';
let analyticsLoaded = false;

// DOM 元素
const els = {
  back: document.getElementById('back'),
  logout: document.getElementById('logout'),
  demoBanner: document.getElementById('demo-banner'),
  usersTbody: document.getElementById('users-tbody'),
  usersRefresh: document.getElementById('users-refresh'),
  usersLoading: document.getElementById('users-loading'),
  usersCount: document.getElementById('users-count'),
  usersPagination: document.getElementById('users-pagination'),
  pageInfo: document.getElementById('page-info'),
  paginationText: document.getElementById('pagination-text'),
  prevPage: document.getElementById('prev-page'),
  nextPage: document.getElementById('next-page'),
  adminMain: document.querySelector('.admin-main'),
  analyticsPanel: document.getElementById('analytics-panel'),
  navUsers: document.getElementById('nav-users'),
  navAnalytics: document.getElementById('nav-analytics'),
  healthD1: document.getElementById('health-d1'),
  healthR2: document.getElementById('health-r2'),
  healthResend: document.getElementById('health-resend'),
  healthRouting: document.getElementById('health-routing'),
  healthLatest: document.getElementById('health-latest'),
  healthCheckedAt: document.getElementById('health-checked-at'),
  healthRefresh: document.getElementById('health-refresh'),
  analyticsLoading: document.getElementById('analytics-loading'),
  analyticsRange: document.getElementById('analytics-range'),
  metricUsers: document.getElementById('metric-users'),
  metricMailboxes: document.getElementById('metric-mailboxes'),
  metricMessages: document.getElementById('metric-messages'),
  metricSent: document.getElementById('metric-sent'),
  metricExpired: document.getElementById('metric-expired'),
  chartTrend: document.getElementById('chart-trend'),
  chartTrendSummary: document.getElementById('chart-trend-summary'),
  chartGrowth: document.getElementById('chart-growth'),
  chartSentStatus: document.getElementById('chart-sent-status'),
  chartDomains: document.getElementById('chart-domains'),
  chartTopUsers: document.getElementById('chart-top-users'),
  
  uOpen: document.getElementById('u-open'),
  uModal: document.getElementById('u-modal'),
  uClose: document.getElementById('u-close'),
  uCancel: document.getElementById('u-cancel'),
  uCreate: document.getElementById('u-create'),
  uName: document.getElementById('u-name'),
  uPass: document.getElementById('u-pass'),
  uRole: document.getElementById('u-role'),
  
  aOpen: document.getElementById('a-open'),
  aModal: document.getElementById('a-modal'),
  aClose: document.getElementById('a-close'),
  aCancel: document.getElementById('a-cancel'),
  aAssign: document.getElementById('a-assign'),
  aName: document.getElementById('a-name'),
  aMail: document.getElementById('a-mail'),
  
  // 取消分配模态框
  unassignOpen: document.getElementById('unassign-open'),
  unassignModal: document.getElementById('unassign-modal'),
  unassignClose: document.getElementById('unassign-close'),
  unassignCancel: document.getElementById('unassign-cancel'),
  unassignSubmit: document.getElementById('unassign-submit'),
  unassignName: document.getElementById('unassign-name'),
  unassignMail: document.getElementById('unassign-mail'),
  
  editModal: document.getElementById('edit-modal'),
  editClose: document.getElementById('edit-close'),
  editCancel: document.getElementById('edit-cancel'),
  editSave: document.getElementById('edit-save'),
  editName: document.getElementById('edit-name'),
  editUserDisplay: document.getElementById('edit-user-display'),
  editNewName: document.getElementById('edit-new-name'),
  editRoleCheck: document.getElementById('edit-role-check'),
  editLimit: document.getElementById('edit-limit'),
  editSendCheck: document.getElementById('edit-send-check'),
  editPass: document.getElementById('edit-pass'),
  editDelete: document.getElementById('edit-delete'),
  
  userMailboxes: document.getElementById('user-mailboxes'),
  userMailboxesLoading: document.getElementById('user-mailboxes-loading'),
  mailboxesCount: document.getElementById('mailboxes-count'),
  mailboxesPagination: document.getElementById('mailboxes-pagination'),
  mailboxesPageInfo: document.getElementById('mailboxes-page-info'),
  mailboxesPrevPage: document.getElementById('mailboxes-prev-page'),
  mailboxesNextPage: document.getElementById('mailboxes-next-page'),
  
  // 确认模态框
  confirmModal: document.getElementById('admin-confirm-modal'),
  confirmMessage: document.getElementById('admin-confirm-message'),
  confirmClose: document.getElementById('admin-confirm-close'),
  confirmCancel: document.getElementById('admin-confirm-cancel'),
  confirmOk: document.getElementById('admin-confirm-ok')
};

// 自定义确认对话框
let confirmResolver = null;
function showConfirm(message) {
  return new Promise(resolve => {
    confirmResolver = resolve;
    if (els.confirmMessage) els.confirmMessage.textContent = message;
    els.confirmModal?.classList.add('show');
  });
}

function initConfirmEvents() {
  if (els._confirmInitialized) return;
  els._confirmInitialized = true;
  
  const closeConfirm = (result) => {
    els.confirmModal?.classList.remove('show');
    if (confirmResolver) {
      confirmResolver(result);
      confirmResolver = null;
    }
  };
  
  els.confirmOk?.addEventListener('click', () => closeConfirm(true));
  els.confirmCancel?.addEventListener('click', () => closeConfirm(false));
  els.confirmClose?.addEventListener('click', () => closeConfirm(false));
  els.confirmModal?.addEventListener('click', (e) => {
    if (e.target === els.confirmModal) closeConfirm(false);
  });
}
initConfirmEvents();

// 加载用户列表
async function loadUsers() {
  if (els.usersLoading) els.usersLoading.style.display = 'flex';
  if (els.usersTbody) els.usersTbody.innerHTML = generateSkeletonRows(5);

  try {
    const data = await getUsers({ page: currentPage, size: pageSize });
    const users = Array.isArray(data) ? data : (data.list || []);
    totalUsers = data.total || users.length;

    renderUserList(users, els.usersTbody);
    updatePagination();

    // 更新统计卡片
    updateStats(users);

    if (els.usersCount) els.usersCount.textContent = `${totalUsers} 人`;

    bindUserEvents();
  } catch (e) {
    console.error('加载用户失败:', e);
    showToast('加载失败', 'error');
  } finally {
    if (els.usersLoading) els.usersLoading.style.display = 'none';
  }
}

// 更新统计卡片
function updateStats(users) {
  const totalUsers = users.length;
  const adminCount = users.filter(u => u.role === 'admin').length;
  const mailboxCount = users.reduce((sum, u) => sum + (u.mailbox_count || 0), 0);
  const activeUsers = users.filter(u => u.can_send).length;

  const statTotal = document.getElementById('stat-total-users');
  const statAdmin = document.getElementById('stat-admin-count');
  const statMailbox = document.getElementById('stat-mailbox-count');
  const statActive = document.getElementById('stat-active-users');

  if (statTotal) statTotal.textContent = totalUsers;
  if (statAdmin) statAdmin.textContent = adminCount;
  if (statMailbox) statMailbox.textContent = mailboxCount;
  if (statActive) statActive.textContent = activeUsers;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  }[ch]));
}

function compactNumber(value) {
  const n = Number(value || 0);
  if (n >= 10000) return `${(n / 10000).toFixed(1)}万`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function setHealthPill(el, label, state = 'unknown') {
  if (!el) return;
  el.className = `health-pill status-${state}`;
  el.textContent = label;
}

function deriveRoutingState(health) {
  const domains = Array.isArray(health?.domains) ? health.domains.filter(Boolean) : [];
  if (!domains.length) return { label: 'Email Routing 未配置域名', state: 'danger' };
  if (health?.latest_message_at) return { label: 'Email Routing 收信正常', state: 'ok' };
  return { label: 'Email Routing 待验证', state: 'warn' };
}

async function loadSystemHealth() {
  setHealthPill(els.healthD1, 'D1 检查中', 'unknown');
  setHealthPill(els.healthR2, 'R2 检查中', 'unknown');
  setHealthPill(els.healthResend, 'Resend 检查中', 'unknown');
  setHealthPill(els.healthRouting, 'Email Routing 检查中', 'unknown');
  if (els.healthRefresh) els.healthRefresh.disabled = true;
  try {
    const health = await getSystemHealth();
    setHealthPill(els.healthD1, health.db_bound ? 'D1 正常' : 'D1 异常', health.db_bound ? 'ok' : 'danger');
    setHealthPill(els.healthR2, health.r2_bound ? 'R2 已绑定' : 'R2 未绑定', health.r2_bound ? 'ok' : 'warn');
    setHealthPill(els.healthResend, health.resend_configured ? 'Resend 已配置' : 'Resend 未配置', health.resend_configured ? 'ok' : 'warn');
    const routing = deriveRoutingState(health);
    setHealthPill(els.healthRouting, routing.label, routing.state);
    setHealthPill(els.healthLatest, health.latest_message_at ? `最近收信 ${formatTime(health.latest_message_at)}` : '最近收信 --', 'muted');
    if (els.healthCheckedAt) els.healthCheckedAt.textContent = health.checked_at ? `检查 ${formatTime(health.checked_at)}` : '刚刚检查';
  } catch (e) {
    setHealthPill(els.healthD1, '健康检查不可用', 'danger');
    setHealthPill(els.healthR2, 'R2 未知', 'unknown');
    setHealthPill(els.healthResend, 'Resend 未知', 'unknown');
    setHealthPill(els.healthRouting, 'Email Routing 未知', 'unknown');
    setHealthPill(els.healthLatest, '最近收信 --', 'muted');
    if (els.healthCheckedAt) els.healthCheckedAt.textContent = '检查失败';
  } finally {
    if (els.healthRefresh) els.healthRefresh.disabled = false;
  }
}

function setAdminView(view) {
  const isAnalytics = view === 'analytics';
  if (els.adminMain) els.adminMain.style.display = isAnalytics ? 'none' : 'grid';
  if (els.analyticsPanel) els.analyticsPanel.style.display = isAnalytics ? 'flex' : 'none';
  els.navUsers?.classList.toggle('active', !isAnalytics);
  els.navUsers?.classList.toggle('action-btn-primary', !isAnalytics);
  els.navUsers?.classList.toggle('action-btn-outline', isAnalytics);
  els.navAnalytics?.classList.toggle('active', isAnalytics);
  els.navAnalytics?.classList.toggle('action-btn-primary', isAnalytics);
  els.navAnalytics?.classList.toggle('action-btn-outline', !isAnalytics);
  if (isAnalytics && !analyticsLoaded) loadAnalytics();
}

function buildSeriesPath(values, width, height, padding) {
  const max = Math.max(1, ...values);
  const innerW = width - padding * 2;
  const innerH = height - padding * 2;
  return values.map((value, index) => {
    const x = padding + (values.length === 1 ? innerW / 2 : (innerW * index) / (values.length - 1));
    const y = padding + innerH - (value / max) * innerH;
    return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
}

function renderTrendChart(data) {
  if (!els.chartTrend) return;
  const rows = data.trend || [];
  if (!rows.length) {
    els.chartTrend.innerHTML = '<div class="empty-chart">暂无趋势数据</div>';
    return;
  }
  const width = 720, height = 240, padding = 28;
  const messages = rows.map(row => Number(row.messages || 0));
  const sent = rows.map(row => Number(row.sent_emails || 0));
  const msgPath = buildSeriesPath(messages, width, height, padding);
  const sentPath = buildSeriesPath(sent, width, height, padding);
  const totalMessages = messages.reduce((sum, n) => sum + n, 0);
  const totalSent = sent.reduce((sum, n) => sum + n, 0);
  if (els.chartTrendSummary) els.chartTrendSummary.textContent = `收信 ${totalMessages} / 发信 ${totalSent}`;
  els.chartTrend.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" class="line-chart" role="img" aria-label="收信和发信趋势">
      <path class="grid-line" d="M${padding},${height - padding} H${width - padding}" />
      <path class="grid-line" d="M${padding},${padding} V${height - padding}" />
      <path class="trend-area messages" d="${msgPath} L${width - padding},${height - padding} L${padding},${height - padding} Z" />
      <path class="trend-line messages" d="${msgPath}" />
      <path class="trend-line sent" d="${sentPath}" />
    </svg>
    <div class="chart-legend"><span class="legend-dot messages"></span>收信 <span class="legend-dot sent"></span>发信</div>
  `;
}

function renderGrowthChart(data) {
  if (!els.chartGrowth) return;
  const rows = (data.trend || []).slice(-14);
  if (!rows.length) {
    els.chartGrowth.innerHTML = '<div class="empty-chart">暂无新增数据</div>';
    return;
  }
  const max = Math.max(1, ...rows.map(row => Math.max(Number(row.users || 0), Number(row.mailboxes || 0))));
  els.chartGrowth.innerHTML = `
    <div class="bar-chart">
      ${rows.map(row => {
        const usersH = Math.max(4, (Number(row.users || 0) / max) * 100);
        const mailboxesH = Math.max(4, (Number(row.mailboxes || 0) / max) * 100);
        return `<div class="bar-day" title="${escapeHtml(row.date)}">
          <span class="bar users" style="height:${usersH}%"></span>
          <span class="bar mailboxes" style="height:${mailboxesH}%"></span>
        </div>`;
      }).join('')}
    </div>
    <div class="chart-legend"><span class="legend-dot users"></span>用户 <span class="legend-dot mailboxes"></span>邮箱</div>
  `;
}

function renderDonutChart(data) {
  if (!els.chartSentStatus) return;
  const rows = data.sent_status || [];
  const total = rows.reduce((sum, row) => sum + Number(row.total || 0), 0);
  if (!total) {
    els.chartSentStatus.innerHTML = '<div class="empty-chart">暂无发信记录</div>';
    return;
  }
  const colors = ['#10b981', '#7c3aed', '#f59e0b', '#ef4444', '#64748b'];
  let offset = 25;
  const circles = rows.map((row, index) => {
    const value = Number(row.total || 0);
    const dash = (value / total) * 100;
    const circle = `<circle r="36" cx="50" cy="50" pathLength="100" stroke="${colors[index % colors.length]}" stroke-dasharray="${dash} ${100 - dash}" stroke-dashoffset="${offset}" />`;
    offset -= dash;
    return circle;
  }).join('');
  els.chartSentStatus.innerHTML = `
    <div class="donut-wrap">
      <svg viewBox="0 0 100 100" class="donut-chart" role="img" aria-label="发信状态分布">
        <circle r="36" cx="50" cy="50" class="donut-bg" />
        ${circles}
      </svg>
      <div class="donut-center"><strong>${total}</strong><span>封</span></div>
    </div>
    <div class="status-list">
      ${rows.map((row, index) => `<span><i style="background:${colors[index % colors.length]}"></i>${escapeHtml(row.status)} ${row.total}</span>`).join('')}
    </div>
  `;
}

function renderBarList(container, rows, labelKey, valueKey, emptyText) {
  if (!container) return;
  const max = Math.max(1, ...rows.map(row => Number(row[valueKey] || 0)));
  if (!rows.length) {
    container.innerHTML = `<div class="empty-chart">${emptyText}</div>`;
    return;
  }
  container.innerHTML = rows.map(row => {
    const value = Number(row[valueKey] || 0);
    const percent = Math.max(4, (value / max) * 100);
    return `<div class="hbar-row">
      <div class="hbar-label"><span>${escapeHtml(row[labelKey])}</span><strong>${compactNumber(value)}</strong></div>
      <div class="hbar-track"><span style="width:${percent}%"></span></div>
    </div>`;
  }).join('');
}

function renderAnalytics(data) {
  const totals = data.totals || {};
  if (els.metricUsers) els.metricUsers.textContent = compactNumber(totals.users);
  if (els.metricMailboxes) els.metricMailboxes.textContent = compactNumber(totals.mailboxes);
  if (els.metricMessages) els.metricMessages.textContent = compactNumber(totals.messages);
  if (els.metricSent) els.metricSent.textContent = compactNumber(totals.sent_emails);
  if (els.metricExpired) els.metricExpired.textContent = compactNumber(totals.expired_mailboxes);
  renderTrendChart(data);
  renderGrowthChart(data);
  renderDonutChart(data);
  renderBarList(els.chartDomains, data.domain_distribution || [], 'domain', 'total', '暂无域名数据');
  renderBarList(els.chartTopUsers, data.top_users || [], 'username', 'mailbox_count', '暂无用户邮箱数据');
}

async function loadAnalytics() {
  if (els.analyticsLoading) els.analyticsLoading.style.display = 'flex';
  try {
    const data = await getAnalytics(analyticsRange);
    analyticsLoaded = true;
    renderAnalytics(data);
  } catch (e) {
    analyticsLoaded = false;
    const message = '<div class="empty-chart chart-error">分析数据加载失败</div>';
    [els.chartTrend, els.chartGrowth, els.chartSentStatus, els.chartDomains, els.chartTopUsers]
      .forEach(el => { if (el) el.innerHTML = message; });
    showToast('分析数据加载失败', 'error');
  } finally {
    if (els.analyticsLoading) els.analyticsLoading.style.display = 'none';
  }
}

// 更新分页
function updatePagination() {
  const totalPages = Math.max(1, Math.ceil(totalUsers / pageSize));
  const start = (currentPage - 1) * pageSize + 1;
  const end = Math.min(currentPage * pageSize, totalUsers);

  if (els.pageInfo) els.pageInfo.textContent = `${currentPage} / ${totalPages}`;
  if (els.paginationText) els.paginationText.textContent = `显示 ${start}-${end} 条，共 ${totalUsers} 条`;
  if (els.prevPage) els.prevPage.disabled = currentPage <= 1;
  if (els.nextPage) els.nextPage.disabled = currentPage >= totalPages;
}

// 绑定用户操作事件
function bindUserEvents() {
  // 点击整行加载邮箱列表
  els.usersTbody?.querySelectorAll('.user-row.clickable').forEach(row => {
    row.onclick = async (e) => {
      // 如果点击的是按钮，不触发行点击
      if (e.target.closest('[data-action]')) return;
      
      const userId = row.dataset.userId;
      if (userId) {
        // 移除其他行的选中状态
        els.usersTbody.querySelectorAll('.user-row').forEach(r => r.classList.remove('active'));
        row.classList.add('active');
        await openMailboxesPanel(userId);
      }
    };
  });
  
  // 编辑按钮事件
  els.usersTbody?.querySelectorAll('[data-action="edit"]').forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const userId = btn.dataset.userId;
      await openEditModal(userId);
    };
  });
}

// 打开编辑模态框
async function openEditModal(userId) {
  try {
    const data = await getUsers({ page: 1, size: 100 });
    const users = Array.isArray(data) ? data : (data.list || []);
    const user = users.find(u => u.id == userId);
    if (!user) { showToast('用户不存在', 'error'); return; }
    
    currentViewingUser = user;
    fillEditForm(els, user);
    els.editModal?.classList.add('show');
  } catch(e) {
    showToast('加载用户信息失败', 'error');
  }
}

// 保存用户编辑
async function saveEdit() {
  if (!currentViewingUser) return;
  
  const formData = collectEditFormData(els);
  const validation = validateEditForm(formData, false);
  if (!validation.valid) {
    showToast(validation.error, 'error');
    return;
  }
  
  try {
    await updateUser(currentViewingUser.id, formData);
    showToast('保存成功', 'success');
    els.editModal?.classList.remove('show');
    loadUsers();
  } catch(e) {
    showToast('保存失败', 'error');
  }
}

// 打开邮箱面板
async function openMailboxesPanel(userId) {
  try {
    const data = await getUsers({ page: 1, size: 100 });
    const users = Array.isArray(data) ? data : (data.list || []);
    const user = users.find(u => u.id == userId);
    if (!user) { showToast('用户不存在', 'error'); return; }
    
    currentViewingUser = user;
    mailboxPage = 1;
    await loadUserMailboxes();
    
    // 显示邮箱面板
    if (els.userMailboxes) els.userMailboxes.style.display = 'block';
    if (els.aName) els.aName.value = user.username;
  } catch(e) {
    showToast('加载失败', 'error');
  }
}

// 加载用户邮箱
async function loadUserMailboxes() {
  if (!currentViewingUser) return;
  if (els.userMailboxesLoading) els.userMailboxesLoading.style.display = 'flex';

  try {
    const data = await getUserMailboxes(currentViewingUser.id, { page: mailboxPage, size: mailboxPageSize });
    const list = Array.isArray(data) ? data : (data.list || []);
    totalMailboxes = data.total || list.length;

    if (els.mailboxesCount) els.mailboxesCount.textContent = `${totalMailboxes} 个`;

    // 渲染邮箱列表
    const container = document.getElementById('mailbox-list');
    const emptyState = document.getElementById('empty-mailbox-list');

    if (container) {
      if (list.length > 0) {
        container.innerHTML = list.map(m => `
          <div class="mailbox-item" data-address="${m.address}" data-href="/?mailbox=${encodeURIComponent(m.address)}">
            <span class="address">${m.address}</span>
            <button class="btn danger" data-action="unassign">取消分配</button>
          </div>
        `).join('');
        if (emptyState) emptyState.classList.add('hidden');

        // 绑定取消分配事件
        container.querySelectorAll('[data-action="unassign"]').forEach(btn => {
          btn.onclick = async (e) => {
            e.stopPropagation();
            const address = btn.closest('[data-address]')?.dataset.address;
            if (!address) return;

            const confirmed = await showConfirm(`确定取消分配邮箱 ${address}？`);
            if (!confirmed) return;

            try {
              await unassignMailbox(currentViewingUser.username, address);
              showToast('已取消分配', 'success');
              loadUserMailboxes();
            } catch(e) { showToast('取消分配失败', 'error'); }
          };
        });
      } else {
        container.innerHTML = '';
        if (emptyState) emptyState.classList.remove('hidden');
      }
    }

    // 更新分页
    const totalPages = Math.max(1, Math.ceil(totalMailboxes / mailboxPageSize));
    if (els.mailboxesPageInfo) els.mailboxesPageInfo.textContent = `${mailboxPage} / ${totalPages}`;
    if (els.mailboxesPrevPage) els.mailboxesPrevPage.disabled = mailboxPage <= 1;
    if (els.mailboxesNextPage) els.mailboxesNextPage.disabled = mailboxPage >= totalPages;

    // 更新选中用户显示
    const selectedUserInfo = document.getElementById('selected-user-info');
    if (selectedUserInfo) {
      selectedUserInfo.innerHTML = `<span class="selected-user-name">${currentViewingUser.username}</span>`;
    }
  } catch(e) {
    showToast('加载邮箱失败', 'error');
  } finally {
    if (els.userMailboxesLoading) els.userMailboxesLoading.style.display = 'none';
  }
}

// 创建用户
async function handleCreateUser() {
  const username = els.uName?.value.trim();
  const password = els.uPass?.value.trim();
  const role = els.uRole?.value || 'user';
  
  if (!username || !password) {
    showToast('用户名和密码不能为空', 'error');
    return;
  }
  
  try {
    await createUser({ username, password, role });
    showToast('用户创建成功', 'success');
    els.uModal?.classList.remove('show');
    els.uName.value = '';
    els.uPass.value = '';
    loadUsers();
  } catch(e) {
    showToast('创建失败', 'error');
  }
}

// 分配邮箱
async function handleAssignMailbox() {
  const username = els.aName?.value.trim();
  const addressText = els.aMail?.value.trim();
  
  if (!username) {
    showToast('请输入用户名', 'error');
    return;
  }
  
  if (!addressText) {
    showToast('请输入邮箱地址', 'error');
    return;
  }
  
  // 支持批量分配（每行一个地址）
  const addresses = addressText.split('\n').map(a => a.trim()).filter(a => a);
  if (addresses.length === 0) {
    showToast('请输入有效的邮箱地址', 'error');
    return;
  }
  
  try {
    let successCount = 0;
    let failCount = 0;
    for (const address of addresses) {
      try {
        await assignMailbox(username, address);
        successCount++;
      } catch(e) {
        failCount++;
      }
    }
    
    if (successCount > 0 && failCount === 0) {
      showToast(`成功分配 ${successCount} 个邮箱`, 'success');
    } else if (successCount > 0 && failCount > 0) {
      showToast(`成功 ${successCount} 个，失败 ${failCount} 个`, 'warning');
    } else {
      showToast('分配失败', 'error');
    }
    
    els.aModal?.classList.remove('show');
    els.aMail.value = '';
    els.aName.value = '';
    
    // 如果当前有查看的用户且用户名匹配，刷新邮箱列表
    if (currentViewingUser && currentViewingUser.username === username) {
      loadUserMailboxes();
    }
  } catch(e) {
    showToast('分配失败', 'error');
  }
}

// 取消分配邮箱
async function handleUnassignMailbox() {
  const username = els.unassignName?.value.trim();
  const addressText = els.unassignMail?.value.trim();
  
  if (!username) {
    showToast('请输入用户名', 'error');
    return;
  }
  
  if (!addressText) {
    showToast('请输入邮箱地址', 'error');
    return;
  }
  
  // 支持批量取消分配（每行一个地址）
  const addresses = addressText.split('\n').map(a => a.trim()).filter(a => a);
  if (addresses.length === 0) {
    showToast('请输入有效的邮箱地址', 'error');
    return;
  }
  
  try {
    let successCount = 0;
    let failCount = 0;
    for (const address of addresses) {
      try {
        await unassignMailbox(username, address);
        successCount++;
      } catch(e) {
        failCount++;
      }
    }
    
    if (successCount > 0 && failCount === 0) {
      showToast(`成功取消分配 ${successCount} 个邮箱`, 'success');
    } else if (successCount > 0 && failCount > 0) {
      showToast(`成功 ${successCount} 个，失败 ${failCount} 个`, 'warning');
    } else {
      showToast('取消分配失败', 'error');
    }
    
    els.unassignModal?.classList.remove('show');
    els.unassignMail.value = '';
    els.unassignName.value = '';
    
    // 如果当前有查看的用户且用户名匹配，刷新邮箱列表
    if (currentViewingUser && currentViewingUser.username === username) {
      loadUserMailboxes();
    }
  } catch(e) {
    showToast('取消分配失败', 'error');
  }
}

// 事件绑定
els.back?.addEventListener('click', () => history.back());
els.logout?.addEventListener('click', async () => { try { await api('/api/logout', { method: 'POST' }); } catch(_) {} location.replace('/html/login.html'); });
els.usersRefresh?.addEventListener('click', loadUsers);
els.healthRefresh?.addEventListener('click', loadSystemHealth);
els.navUsers?.addEventListener('click', () => setAdminView('users'));
els.navAnalytics?.addEventListener('click', () => setAdminView('analytics'));
els.analyticsRange?.querySelectorAll('[data-range]').forEach(btn => {
  btn.addEventListener('click', () => {
    analyticsRange = btn.dataset.range || '30d';
    els.analyticsRange.querySelectorAll('[data-range]').forEach(item => item.classList.toggle('active', item === btn));
    loadAnalytics();
  });
});
els.prevPage?.addEventListener('click', () => { if (currentPage > 1) { currentPage--; loadUsers(); }});
els.nextPage?.addEventListener('click', () => { const totalPages = Math.ceil(totalUsers / pageSize); if (currentPage < totalPages) { currentPage++; loadUsers(); }});

// 创建用户模态框
els.uOpen?.addEventListener('click', () => els.uModal?.classList.add('show'));
els.uClose?.addEventListener('click', () => els.uModal?.classList.remove('show'));
els.uCancel?.addEventListener('click', () => els.uModal?.classList.remove('show'));
els.uCreate?.addEventListener('click', handleCreateUser);

// 分配邮箱模态框
els.aOpen?.addEventListener('click', () => els.aModal?.classList.add('show'));
els.aClose?.addEventListener('click', () => els.aModal?.classList.remove('show'));
els.aCancel?.addEventListener('click', () => els.aModal?.classList.remove('show'));
els.aAssign?.addEventListener('click', handleAssignMailbox);

// 取消分配模态框
els.unassignOpen?.addEventListener('click', () => els.unassignModal?.classList.add('show'));
els.unassignClose?.addEventListener('click', () => els.unassignModal?.classList.remove('show'));
els.unassignCancel?.addEventListener('click', () => els.unassignModal?.classList.remove('show'));
els.unassignSubmit?.addEventListener('click', handleUnassignMailbox);

// 编辑模态框
els.editClose?.addEventListener('click', () => els.editModal?.classList.remove('show'));
els.editCancel?.addEventListener('click', () => els.editModal?.classList.remove('show'));
els.editSave?.addEventListener('click', saveEdit);
els.editDelete?.addEventListener('click', async () => {
  if (!currentViewingUser) return;
  
  const confirmed = await showConfirm(`确定删除用户 "${currentViewingUser.username}" 吗？此操作不可恢复。`);
  if (!confirmed) return;
  
  try {
    await deleteUser(currentViewingUser.id);
    showToast('用户已删除', 'success');
    els.editModal?.classList.remove('show');
    loadUsers();
  } catch(e) { showToast('删除失败', 'error'); }
});

// 邮箱分页
els.mailboxesPrevPage?.addEventListener('click', () => { if (mailboxPage > 1) { mailboxPage--; loadUserMailboxes(); }});
els.mailboxesNextPage?.addEventListener('click', () => { const totalPages = Math.ceil(totalMailboxes / mailboxPageSize); if (mailboxPage < totalPages) { mailboxPage++; loadUserMailboxes(); }});

// 初始化
loadUsers();
loadSystemHealth();
