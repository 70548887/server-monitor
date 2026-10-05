let alertsOffset = 0;
let alertsTotal = 0;

// 页面加载
document.addEventListener('DOMContentLoaded', () => {
    loadAlertsStats();
    loadAlerts();
});

async function loadAlertsStats() {
    try {
        const res = await fetch('/api/notifications/alerts/logs/stats');
        const data = await res.json();

        const activeCountEl = document.getElementById('alert-active-count');
        const todayTotalEl = document.getElementById('alert-today-total');
        const nodeDownEl = document.getElementById('alert-node-down');
        const nodeRecoveryEl = document.getElementById('alert-node-recovery');
        const latencyHighEl = document.getElementById('alert-latency-high');

        if (activeCountEl) activeCountEl.textContent = data.active_count || 0;
        if (todayTotalEl) todayTotalEl.textContent = data.today_total || 0;
        if (nodeDownEl) nodeDownEl.textContent = data.by_type?.node_down || 0;
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
                            ${alertTypeNames[alert.alert_type] || alert.alert_type}
                        </span>
                        ${alert.notify_seq > 1
                            ? `<span class="badge badge-secondary" title="本次故障的第 ${alert.notify_seq} 次提醒${alert.incident_id ? '（事件 #' + alert.incident_id + '）' : ''}">
                                 第${alert.notify_seq}次
                               </span>`
                            : ''}
                    </td>
                    <td><a href="/ui/nodes/${encodeURIComponent(alert.node_name)}"><strong>${alert.node_name}</strong></a></td>
                    <td class="url-cell" title="${alert.message || ''}">${(alert.message || '-').substring(0, 50)}...</td>
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
