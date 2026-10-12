let alertsOffset = 0;
let alertsTotal = 0;
let incidentsOffset = 0;
let incidentsTotal = 0;
let currentView = 'incidents';

// 页面加载：默认展示事件视图
document.addEventListener('DOMContentLoaded', () => {
    loadAlertsStats();
    loadIncidents(0);
});

/** 切换视图 */
function switchView(view) {
    currentView = view;

    const incView = document.getElementById('incidents-view');
    const logView = document.getElementById('logs-view');
    const btnInc = document.getElementById('view-incidents');
    const btnLog = document.getElementById('view-logs');
    const hint = document.getElementById('view-hint');

    const showInc = view === 'incidents';
    incView.style.display = showInc ? 'block' : 'none';
    logView.style.display = showInc ? 'none' : 'block';

    btnInc.className = showInc ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-secondary';
    btnLog.className = showInc ? 'btn btn-sm btn-secondary' : 'btn btn-sm btn-primary';

    if (hint) {
        hint.textContent = showInc
            ? '按故障聚合：一次故障 = 一个事件，可展开查看每次提醒'
            : '逐条告警记录（同一故障的多次提醒都会出现）';
    }

    if (showInc) {
        loadIncidents(incidentsOffset);
    } else {
        loadAlerts(alertsOffset);
    }
}

/** 把秒数格式化为易读的时长 */
function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) return '-';
    seconds = Math.max(0, Math.floor(seconds));
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const mins = Math.floor((seconds % 3600) / 60);

    if (days > 0) return `${days}天${hours}小时`;
    if (hours > 0) return `${hours}小时${mins}分`;
    return `${mins}分钟`;
}

/** 统计卡片：加载事件维度的数据 */
async function loadIncidentStats() {
    try {
        const res = await fetch('/api/notifications/incidents/stats');
        if (!res.ok) return;
        const data = await res.json();

        const activeEl = document.getElementById('alert-active-count');
        const totalEl = document.getElementById('alert-total-incidents');
        const notifiedEl = document.getElementById('alert-total-notified');

        if (activeEl) activeEl.textContent = data.open_incidents || 0;
        if (totalEl) {
            totalEl.textContent = (data.open_incidents || 0) + (data.resolved_incidents || 0);
        }
        if (notifiedEl) notifiedEl.textContent = data.open_notifications || 0;
    } catch (e) {
        console.error('Failed to load incident stats:', e);
    }
}

/** 加载故障事件列表 */
async function loadIncidents(offset = 0) {
    incidentsOffset = Math.max(0, offset);

    const statusEl = document.getElementById('incident-filter-status');
    const status = statusEl ? statusEl.value : 'open';

    let url = `/api/notifications/incidents?limit=20&offset=${incidentsOffset}`;
    if (status) url += `&status=${status}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        incidentsTotal = data.total || 0;

        const tbody = document.getElementById('incidents-tbody');
        const emptyState = document.getElementById('incidents-empty');
        if (!tbody) return;

        const list = data.incidents || [];

        if (list.length === 0) {
            tbody.innerHTML = '';
            if (emptyState) emptyState.style.display = 'block';
        } else {
            if (emptyState) emptyState.style.display = 'none';

            const typeNames = {
                'node_down': '节点离线',
                'latency_high': '延迟过高',
            };

            tbody.innerHTML = list.map(inc => {
                const isOpen = inc.status === 'open';
                // 进行中的事件实时计算持续时长；已关闭的用落库值
                const duration = isOpen
                    ? formatDuration((Date.now() - new Date(inc.started_at).getTime()) / 1000)
                    : formatDuration(inc.duration_seconds);

                // 下次提醒时间：已关闭的事件没有
                const nextCell = isOpen && inc.next_notify_at
                    ? `<span style="color:#17a2b8;">${formatTime(inc.next_notify_at)}</span>`
                    : '<span style="color:#aaa;">-</span>';

                return `
                <tr data-id="${inc.id}">
                    <td>
                        <button class="btn btn-sm btn-outline" onclick="toggleIncidentDetail(${inc.id})"
                                id="inc-toggle-${inc.id}" title="查看提醒历史">展开</button>
                    </td>
                    <td>${formatTime(inc.started_at)}</td>
                    <td><a href="/ui/nodes/${encodeURIComponent(inc.node_name)}"><strong>${escapeHtml(inc.node_name)}</strong></a></td>
                    <td>
                        <span class="badge ${inc.alert_type === 'node_down' ? 'badge-danger' : 'badge-warning'}">
                            ${escapeHtml(typeNames[inc.alert_type] || inc.alert_type)}
                        </span>
                    </td>
                    <td><strong>${duration}</strong></td>
                    <td>${inc.notify_count || 0} 次</td>
                    <td>${inc.total_failures || 0}</td>
                    <td>${nextCell}</td>
                    <td>
                        <span class="badge ${isOpen ? 'badge-danger' : 'badge-success'}">
                            ${isOpen ? '进行中' : '已恢复'}
                        </span>
                    </td>
                </tr>
                <tr id="inc-detail-${inc.id}" style="display:none;background:#f8f9fa;">
                    <td colspan="9">
                        <div id="inc-detail-body-${inc.id}" style="padding:10px;">
                            <span style="color:#888;">加载中...</span>
                        </div>
                    </td>
                </tr>`;
            }).join('');
        }

        // 分页信息
        const start = incidentsTotal > 0 ? incidentsOffset + 1 : 0;
        const end = Math.min(incidentsOffset + 20, incidentsTotal);
        const infoEl = document.getElementById('incidents-info');
        const prevEl = document.getElementById('incidents-prev');
        const nextEl = document.getElementById('incidents-next');

        if (infoEl) {
            infoEl.textContent = `共 ${incidentsTotal} 条事件${incidentsTotal > 0 ? `，当前显示 ${start}-${end}` : ''}`;
        }
        if (prevEl) prevEl.disabled = incidentsOffset === 0;
        if (nextEl) nextEl.disabled = incidentsOffset + 20 >= incidentsTotal;

        loadIncidentStats();
    } catch (e) {
        console.error('Failed to load incidents:', e);
    }
}

/** 展开/收起某个事件的提醒历史 */
async function toggleIncidentDetail(incidentId) {
    const row = document.getElementById(`inc-detail-${incidentId}`);
    const btn = document.getElementById(`inc-toggle-${incidentId}`);
    if (!row) return;

    // 已展开则收起
    if (row.style.display !== 'none') {
        row.style.display = 'none';
        if (btn) btn.textContent = '展开';
        return;
    }

    row.style.display = 'table-row';
    if (btn) btn.textContent = '收起';

    const body = document.getElementById(`inc-detail-body-${incidentId}`);
    if (!body) return;

    try {
        const res = await fetch(`/api/notifications/incidents/${incidentId}/notifications`);
        const data = await res.json();
        const logs = data.notifications || [];

        if (logs.length === 0) {
            body.innerHTML = '<span style="color:#888;">该事件尚无通知发送记录</span>';
            return;
        }

        const rows = logs.map(log => {
            const ok = log.success;
            return `
            <tr style="background:#fff;">
                <td style="padding:4px 8px;">${formatTime(log.sent_at)}</td>
                <td style="padding:4px 8px;">第 ${log.notify_seq ?? '-'} 次</td>
                <td style="padding:4px 8px;">${escapeHtml(log.channel_name)}</td>
                <td style="padding:4px 8px;">
                    <span class="badge ${ok ? 'badge-success' : 'badge-secondary'}">
                        ${ok ? '已送达' : '未送达'}
                    </span>
                </td>
                <td style="padding:4px 8px;color:#666;">${escapeHtml((log.subject || '').substring(0, 60))}</td>
                ${log.error_message ? `<td style="padding:4px 8px;color:#dc3545;font-size:12px;">${escapeHtml(log.error_message.substring(0, 50))}</td>` : '<td></td>'}
            </tr>`;
        }).join('');

        body.innerHTML = `
            <div style="font-size:13px;color:#666;margin-bottom:8px;">
                该故障共提醒 <strong>${data.count}</strong> 次（提醒节奏自动递减，避免刷屏）
            </div>
            <table class="table" style="font-size:13px;">
                <thead><tr><th>时间</th><th>第几次</th><th>渠道</th><th>结果</th><th>主题</th><th>错误</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>`;
    } catch (e) {
        body.innerHTML = `<span style="color:#dc3545;">加载失败: ${escapeHtml(e.message)}</span>`;
    }
}

async function loadAlertsStats() {
    try {
        const res = await fetch('/api/notifications/alerts/logs/stats');
        const data = await res.json();

        const todayTotalEl = document.getElementById('alert-today-total');
        const nodeRecoveryEl = document.getElementById('alert-node-recovery');
        const latencyHighEl = document.getElementById('alert-latency-high');

        if (todayTotalEl) todayTotalEl.textContent = data.today_total || 0;
        if (nodeRecoveryEl) nodeRecoveryEl.textContent = data.by_type?.node_recovery || 0;
        if (latencyHighEl) latencyHighEl.textContent = data.by_type?.latency_high || 0;
    } catch (e) {
        console.error('Failed to load alerts stats:', e);
    }
}

async function loadAlerts(offset = 0) {
    alertsOffset = Math.max(0, offset);

    const alertTypeEl = document.getElementById('alert-filter-type');
    const statusEl = document.getElementById('alert-filter-status');
    const startDateEl = document.getElementById('alert-filter-start');
    const endDateEl = document.getElementById('alert-filter-end');

    const alertType = alertTypeEl ? alertTypeEl.value : '';
    const status = statusEl ? statusEl.value : '';
    const startDate = startDateEl ? startDateEl.value : '';
    const endDate = endDateEl ? endDateEl.value : '';

    let url = `/api/notifications/alerts/logs?limit=50&offset=${alertsOffset}`;
    if (alertType) url += `&alert_type=${alertType}`;
    if (status) url += `&status=${status}`;
    if (startDate) url += `&start_date=${startDate}`;
    if (endDate) url += `&end_date=${endDate}`;

    try {
        const res = await fetch(url);
        const data = await res.json();

        alertsTotal = data.total;

        const tbody = document.getElementById('alerts-tbody');
        const emptyState = document.getElementById('alerts-empty');

        if (!tbody) return;

        if (data.alerts.length === 0) {
            tbody.innerHTML = '';
            if (emptyState) emptyState.style.display = 'block';
        } else {
            if (emptyState) emptyState.style.display = 'none';

            const alertTypeNames = {
                'node_down': '节点离线',
                'node_recovery': '节点恢复',
                'latency_high': '延迟告警',
            };

            const statusNames = {
                'active': '活跃',
                'acknowledged': '已确认',
                'resolved': '已解决',
            };

            const statusBadges = {
                'active': 'danger',
                'acknowledged': 'warning',
                'resolved': 'success',
            };

            const typeBadges = {
                'node_down': 'danger',
                'node_recovery': 'success',
                'latency_high': 'warning',
            };

            tbody.innerHTML = data.alerts.map(alert => `
                <tr data-id="${alert.id}">
                    <td><input type="checkbox" class="alert-checkbox" value="${alert.id}" onchange="updateSelectedCount()"></td>
                    <td>${formatTime(alert.triggered_at)}</td>
                    <td>
                        <span class="badge badge-${typeBadges[alert.alert_type] || 'secondary'}">
                            ${escapeHtml(alertTypeNames[alert.alert_type] || alert.alert_type)}
                        </span>
                        ${alert.notify_seq > 1
                            ? `<span class="badge badge-secondary" title="本次故障的第 ${alert.notify_seq} 次提醒${alert.incident_id ? '（事件 #' + alert.incident_id + '）' : ''}">
                                 第${alert.notify_seq}次
                               </span>`
                            : ''}
                    </td>
                    <td><a href="/ui/nodes/${encodeURIComponent(alert.node_name)}"><strong>${escapeHtml(alert.node_name)}</strong></a></td>
                    <td class="url-cell" title="${escapeHtml(alert.message || '')}">${escapeHtml((alert.message || '-').substring(0, 50))}...</td>
                    <td>
                        ${alert.notification_sent
                    ? `<span class="badge badge-success">已通知(${alert.notification_count})</span>`
                    : '<span class="badge badge-secondary">未通知</span>'}
                    </td>
                    <td>
                        <span class="badge badge-${statusBadges[alert.status] || 'secondary'}">
                            ${statusNames[alert.status] || alert.status}
                        </span>
                    </td>
                    <td>
                        ${alert.status === 'active'
                    ? `<button class="btn btn-sm btn-warning" onclick="acknowledgeAlert(${alert.id})">确认</button>`
                    : ''}
                        ${alert.status === 'acknowledged'
                    ? `<button class="btn btn-sm btn-success" onclick="resolveAlert(${alert.id})">解决</button>`
                    : ''}
                        <button class="btn btn-sm btn-outline-danger" onclick="deleteAlert(${alert.id})">删除</button>
                    </td>
                </tr>
            `).join('');
        }

        // 重置选择状态
        const selectAllEl = document.getElementById('select-all');
        if (selectAllEl) selectAllEl.checked = false;
        updateSelectedCount();

        // 更新分页信息
        const start = alertsTotal > 0 ? alertsOffset + 1 : 0;
        const end = Math.min(alertsOffset + 50, alertsTotal);
        const alertsInfoEl = document.getElementById('alerts-info');
        const alertsPrevEl = document.getElementById('alerts-prev');
        const alertsNextEl = document.getElementById('alerts-next');

        if (alertsInfoEl) alertsInfoEl.textContent = `共 ${alertsTotal} 条记录${alertsTotal > 0 ? `，当前显示 ${start}-${end}` : ''}`;
        if (alertsPrevEl) alertsPrevEl.disabled = alertsOffset === 0;
        if (alertsNextEl) alertsNextEl.disabled = alertsOffset + 50 >= alertsTotal;
    } catch (e) {
        console.error('Failed to load alerts:', e);
    }
}

function resetFilters() {
    const typeEl = document.getElementById('alert-filter-type');
    const statusEl = document.getElementById('alert-filter-status');
    const startDateEl = document.getElementById('alert-filter-start');
    const endDateEl = document.getElementById('alert-filter-end');
    if (typeEl) typeEl.value = '';
    if (statusEl) statusEl.value = '';
    if (startDateEl) startDateEl.value = '';
    if (endDateEl) endDateEl.value = '';
    loadAlerts(0);
}

// 快速日期选择
function setDateRange(range) {
    const startDateEl = document.getElementById('alert-filter-start');
    const endDateEl = document.getElementById('alert-filter-end');

    if (!startDateEl || !endDateEl) return;

    const today = new Date();
    const formatDate = (d) => d.toISOString().split('T')[0];

    endDateEl.value = formatDate(today);

    switch (range) {
        case 'today':
            startDateEl.value = formatDate(today);
            break;
        case 'week':
            const weekAgo = new Date(today);
            weekAgo.setDate(weekAgo.getDate() - 7);
            startDateEl.value = formatDate(weekAgo);
            break;
        case 'month':
            const monthAgo = new Date(today);
            monthAgo.setDate(monthAgo.getDate() - 30);
            startDateEl.value = formatDate(monthAgo);
            break;
    }

    loadAlerts(0);
}

async function acknowledgeAlert(alertId) {
    try {
        const res = await fetch(`/api/notifications/alerts/logs/${alertId}/acknowledge`, {
            method: 'POST'
        });
        if (res.ok) {
            loadAlerts(alertsOffset);
            loadAlertsStats();
        } else {
            const err = await res.json();
            alert('确认失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('确认失败: ' + e.message);
    }
}

async function resolveAlert(alertId) {
    try {
        const res = await fetch(`/api/notifications/alerts/logs/${alertId}/resolve`, {
            method: 'POST'
        });
        if (res.ok) {
            loadAlerts(alertsOffset);
            loadAlertsStats();
        } else {
            const err = await res.json();
            alert('解决失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('解决失败: ' + e.message);
    }
}

function formatTime(isoStr) {
    if (!isoStr) return '-';
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// 全选/取消全选
function toggleSelectAll() {
    const selectAllEl = document.getElementById('select-all');
    if (!selectAllEl) return;
    const selectAll = selectAllEl.checked;
    document.querySelectorAll('.alert-checkbox').forEach(cb => {
        cb.checked = selectAll;
    });
    updateSelectedCount();
}

// 更新选中数量
function updateSelectedCount() {
    const checkboxes = document.querySelectorAll('.alert-checkbox:checked');
    const count = checkboxes.length;
    const countEl = document.getElementById('selected-count');
    const batchBtn = document.getElementById('batch-delete-btn');
    if (countEl) countEl.textContent = count;
    if (batchBtn) batchBtn.style.display = count > 0 ? 'inline-block' : 'none';
}

// 删除单个告警
async function deleteAlert(alertId) {
    if (!confirm('确定要删除这条告警记录吗？')) {
        return;
    }

    try {
        const res = await fetch(`/api/notifications/alerts/logs/${alertId}`, {
            method: 'DELETE'
        });
        if (res.ok) {
            loadAlerts(alertsOffset);
            loadAlertsStats();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

// 批量删除告警
async function batchDeleteAlerts() {
    const checkboxes = document.querySelectorAll('.alert-checkbox:checked');
    const ids = Array.from(checkboxes).map(cb => parseInt(cb.value));

    if (ids.length === 0) {
        alert('请选择要删除的记录');
        return;
    }

    if (!confirm(`确定要删除选中的 ${ids.length} 条告警记录吗？`)) {
        return;
    }

    try {
        const res = await fetch('/api/notifications/alerts/logs/batch-delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ids: ids })
        });
        if (res.ok) {
            const result = await res.json();
            alert(result.message);
            loadAlerts(alertsOffset);
            loadAlertsStats();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}
