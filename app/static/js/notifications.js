// 重新计算渠道统计
async function recalculateStats() {
    if (!confirm('确定要重新计算所有渠道的发送统计吗？\n\n这将从发送记录表重新统计，修复统计数据与记录不一致的问题。')) return;
    
    try {
        const res = await fetch('/api/notifications/channels/recalculate-stats', {method: 'POST'});
        const data = await res.json();
        
        if (res.ok) {
            // 显示变化详情
            let msg = data.message + '\n\n';
            for (const [id, ch] of Object.entries(data.channels)) {
                if (ch.changed) {
                    msg += `${ch.name}: ${ch.old.send_count}→${ch.new.send_count} 次, 失败 ${ch.old.error_count}→${ch.new.error_count}\n`;
                }
            }
            alert(msg);
            loadChannels();  // 刷新列表
        } else {
            alert('重新计算失败: ' + (data.detail?.message || data.detail));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    }
}

// 加载通知人员列表
async function loadRecipients() {
    try {
        const res = await fetch('/api/notifications/recipients');
        const recipients = await res.json();
        
        const tbody = document.getElementById('recipients-tbody');
        const emptyState = document.getElementById('recipients-empty');
        
        if (!tbody || !emptyState) return;

        if (recipients.length === 0) {
            tbody.innerHTML = '';
            emptyState.style.display = 'block';
            return;
        }
        
        emptyState.style.display = 'none';
        tbody.innerHTML = recipients.map(r => `
            <tr>
                <td><strong>${r.name}</strong></td>
                <td>${r.email}</td>
                <td>
                    <span class="badge badge-${r.enabled ? 'success' : 'secondary'}">
                        ${r.enabled ? '已启用' : '已禁用'}
                    </span>
                </td>
                <td>${formatTime(r.created_at)}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="editRecipient(${r.id})">编辑</button>
                    <button class="btn btn-sm btn-${r.enabled ? 'warning' : 'success'}" onclick="toggleRecipient(${r.id}, ${!r.enabled})">
                        ${r.enabled ? '禁用' : '启用'}
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="deleteRecipient(${r.id}, '${r.name}')">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Failed to load recipients:', e);
    }
}

// 显示添加人员模态框
function showAddRecipientModal() {
    document.getElementById('recipientModalTitle').textContent = '添加通知人员';
    document.getElementById('recipientId').value = '';
    document.getElementById('recipientForm').reset();
    document.getElementById('recipientEnabled').checked = true;
    document.getElementById('recipientModal').style.display = 'flex';
}

// 隐藏人员模态框
function hideRecipientModal() {
    document.getElementById('recipientModal').style.display = 'none';
}

// 编辑人员
async function editRecipient(id) {
    try {
        const res = await fetch(`/api/notifications/recipients/${id}`);
        const r = await res.json();
        
        document.getElementById('recipientModalTitle').textContent = '编辑通知人员';
        document.getElementById('recipientId').value = r.id;
        document.getElementById('recipientName').value = r.name;
        document.getElementById('recipientEmail').value = r.email;
        document.getElementById('recipientEnabled').checked = r.enabled;
        
        document.getElementById('recipientModal').style.display = 'flex';
    } catch (e) {
        alert('加载人员信息失败: ' + e.message);
    }
}

// 保存人员
const recipientForm = document.getElementById('recipientForm');
if (recipientForm) {
    recipientForm.onsubmit = async (e) => {
        e.preventDefault();
        
        const id = document.getElementById('recipientId').value;
        const isEdit = !!id;
        
        const data = {
            name: document.getElementById('recipientName').value,
            email: document.getElementById('recipientEmail').value,
            enabled: document.getElementById('recipientEnabled').checked,
        };
        
        try {
            const url = isEdit ? `/api/notifications/recipients/${id}` : '/api/notifications/recipients';
            const method = isEdit ? 'PUT' : 'POST';
            
            const res = await fetch(url, {
                method,
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            
            if (res.ok) {
                hideRecipientModal();
                loadRecipients();
                alert(isEdit ? '人员信息已更新' : '人员已添加到名单');
            } else {
                const err = await res.json();
                alert('保存失败: ' + (err.detail?.message || err.detail));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        }
    };
}

// 删除人员
async function deleteRecipient(id, name) {
    if (!confirm(`确定要将 "${name}" 从通知名单中移除吗？`)) return;
    
    try {
        const res = await fetch(`/api/notifications/recipients/${id}`, {method: 'DELETE'});
        if (res.ok) {
            loadRecipients();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

// 切换人员状态
async function toggleRecipient(id, enabled) {
    try {
        const res = await fetch(`/api/notifications/recipients/${id}/toggle?enabled=${enabled}`, {method: 'POST'});
        if (res.ok) {
            loadRecipients();
        } else {
            const err = await res.json();
            alert('操作失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
    }
}

// 加载通知渠道列表
async function loadChannels() {
    try {
        const res = await fetch('/api/notifications/channels');
        const channels = await res.json();
        
        const tbody = document.getElementById('channels-tbody');
        const emptyState = document.getElementById('empty-state');
        
        if (!tbody || !emptyState) return;

        if (channels.length === 0) {
            tbody.innerHTML = '';
            emptyState.style.display = 'block';
            return;
        }
        
        emptyState.style.display = 'none';
        tbody.innerHTML = channels.map(ch => `
            <tr data-id="${ch.id}">
                <td><strong>${ch.name}</strong></td>
                <td>
                    <span class="badge badge-${ch.channel_type === 'email' ? 'info' : 'secondary'}">
                        ${ch.channel_type === 'email' ? '邮箱' : 'Webhook'}
                    </span>
                </td>
                <td class="url-cell" title="${ch.channel_type === 'email' ? ch.email_recipients : ch.webhook_url}">
                    ${ch.channel_type === 'email' ? (ch.email_recipients || '-').substring(0, 30) : (ch.webhook_url || '-').substring(0, 30)}...
                </td>
                <td>
                    ${ch.notify_on_down ? '<span class="badge badge-danger" style="margin-right:3px;">离线</span>' : ''}
                    ${ch.notify_on_recovery ? '<span class="badge badge-success" style="margin-right:3px;">恢复</span>' : ''}
                    ${ch.notify_on_latency ? '<span class="badge badge-warning">延迟</span>' : ''}
                </td>
                <td>
                    <span title="成功: ${ch.send_count - ch.error_count}, 失败: ${ch.error_count}">
                        ${ch.send_count} 次
                        ${ch.error_count > 0 ? `<span style="color:red">(${ch.error_count} 失败)</span>` : ''}
                    </span>
                </td>
                <td>
                    <span class="badge badge-${ch.enabled ? 'success' : 'secondary'}">
                        ${ch.enabled ? '已启用' : '已禁用'}
                    </span>
                </td>
                <td>
                    <button class="btn btn-sm btn-info" onclick="testChannelById(${ch.id})">测试</button>
                    <button class="btn btn-sm btn-primary" onclick="editChannel(${ch.id})">编辑</button>
                    <button class="btn btn-sm btn-${ch.enabled ? 'warning' : 'success'}" onclick="toggleChannel(${ch.id}, ${!ch.enabled})">
                        ${ch.enabled ? '禁用' : '启用'}
                    </button>
                    <button class="btn btn-sm btn-danger" onclick="deleteChannel(${ch.id}, '${ch.name}')">删除</button>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('Failed to load channels:', e);
    }
}

// 切换渠道类型字段显示
function toggleChannelTypeFields() {
    const type = document.getElementById('channelType').value;
    document.getElementById('emailFields').style.display = type === 'email' ? 'block' : 'none';
    document.getElementById('webhookFields').style.display = type === 'webhook' ? 'block' : 'none';
}

// 显示添加渠道模态框
function showAddChannelModal() {
    document.getElementById('modalTitle').textContent = '添加通知渠道';
    document.getElementById('channelId').value = '';
    document.getElementById('channelForm').reset();
    document.getElementById('testBtn').style.display = 'none';
    toggleChannelTypeFields();
    document.getElementById('channelModal').style.display = 'flex';
}

// 隐藏模态框
function hideChannelModal() {
    document.getElementById('channelModal').style.display = 'none';
}

// 编辑渠道
async function editChannel(id) {
    try {
        const res = await fetch(`/api/notifications/channels/${id}`);
        const ch = await res.json();
        
        document.getElementById('modalTitle').textContent = '编辑通知渠道';
        document.getElementById('channelId').value = ch.id;
        document.getElementById('channelName').value = ch.name;
        document.getElementById('channelType').value = ch.channel_type;
        document.getElementById('smtpHost').value = ch.smtp_host || '';
        document.getElementById('smtpPort').value = ch.smtp_port || 465;
        document.getElementById('smtpSsl').value = ch.smtp_ssl ? 'true' : 'false';
        document.getElementById('smtpUser').value = ch.smtp_user || '';
        document.getElementById('smtpPassword').value = '';  // 不回显密码
        document.getElementById('emailRecipients').value = ch.email_recipients || '';
        document.getElementById('webhookUrl').value = ch.webhook_url || '';
        document.getElementById('webhookSecret').value = '';  // 不回显密钥
        document.getElementById('notifyOnDown').checked = ch.notify_on_down;
        document.getElementById('notifyOnRecovery').checked = ch.notify_on_recovery;
        document.getElementById('notifyOnLatency').checked = ch.notify_on_latency;
        document.getElementById('channelEnabled').checked = ch.enabled;
        document.getElementById('testBtn').style.display = 'inline-block';
        
        toggleChannelTypeFields();
        document.getElementById('channelModal').style.display = 'flex';
    } catch (e) {
        alert('加载渠道信息失败: ' + e.message);
    }
}

// 保存渠道
const channelForm = document.getElementById('channelForm');
if (channelForm) {
    channelForm.onsubmit = async (e) => {
        e.preventDefault();
        
        const id = document.getElementById('channelId').value;
        const isEdit = !!id;
        
        const data = {
            name: document.getElementById('channelName').value,
            channel_type: document.getElementById('channelType').value,
            enabled: document.getElementById('channelEnabled').checked,
            smtp_host: document.getElementById('smtpHost').value || null,
            smtp_port: parseInt(document.getElementById('smtpPort').value) || null,
            smtp_ssl: document.getElementById('smtpSsl').value === 'true',
            smtp_user: document.getElementById('smtpUser').value || null,
            email_recipients: document.getElementById('emailRecipients').value || null,
            webhook_url: document.getElementById('webhookUrl').value || null,
            notify_on_down: document.getElementById('notifyOnDown').checked,
            notify_on_recovery: document.getElementById('notifyOnRecovery').checked,
            notify_on_latency: document.getElementById('notifyOnLatency').checked,
        };
        
        // 只有填写了密码才更新
        const password = document.getElementById('smtpPassword').value;
        if (password) {
            data.smtp_password = password;
        }
        const secret = document.getElementById('webhookSecret').value;
        if (secret) {
            data.webhook_secret = secret;
        }
        
        try {
            const url = isEdit ? `/api/notifications/channels/${id}` : '/api/notifications/channels';
            const method = isEdit ? 'PUT' : 'POST';
            
            const res = await fetch(url, {
                method,
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            
            if (res.ok) {
                hideChannelModal();
                loadChannels();
                alert(isEdit ? '渠道更新成功' : '渠道添加成功');
            } else {
                const err = await res.json();
                alert('保存失败: ' + (err.detail?.message || err.detail));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        }
    };
}

// 删除渠道
async function deleteChannel(id, name) {
    if (!confirm(`确定要删除通知渠道 "${name}" 吗？`)) return;
    
    try {
        const res = await fetch(`/api/notifications/channels/${id}`, {method: 'DELETE'});
        if (res.ok) {
            loadChannels();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

// 启用/禁用渠道
async function toggleChannel(id, enabled) {
    try {
        const res = await fetch(`/api/notifications/channels/${id}/toggle?enabled=${enabled}`, {method: 'POST'});
        if (res.ok) {
            loadChannels();
        } else {
            const err = await res.json();
            alert('操作失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
    }
}

// 测试渠道（按ID）
async function testChannelById(id) {
    if (!confirm('发送测试通知?')) return;
    
    try {
        const res = await fetch('/api/notifications/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({channel_id: id})
        });
        
        if (res.ok) {
            const data = await res.json();
            alert(`测试通知发送成功!\n渠道: ${data.channel_name}`);
            loadChannels();  // 刷新发送次数
        } else {
            const err = await res.json();
            alert('测试发送失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('测试发送失败: ' + e.message);
    }
}

// 测试当前编辑的渠道
async function testChannel() {
    const id = document.getElementById('channelId').value;
    if (!id) {
        alert('请先保存渠道配置');
        return;
    }
    await testChannelById(parseInt(id));
}

// Tab 切换
function initTabs() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            
            // 更新按钮状态
            tabBtns.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            // 显示对应内容
            document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
            const targetTab = document.getElementById('tab-' + tabName);
            if (targetTab) targetTab.style.display = 'block';
            
            // 加载对应数据
            if (tabName === 'channels') {
                loadChannels();
            } else if (tabName === 'recipients') {
                loadRecipients();
            } else if (tabName === 'logs') {
                loadLogsStats();
                loadLogs();
                loadChannelsForFilter();
            } else if (tabName === 'settings') {
                loadSettings();
            }
        });
    });
}

// ===================== 发送记录 =====================
let logsOffset = 0;
let logsTotal = 0;

async function loadLogsStats() {
    try {
        const res = await fetch('/api/notifications/logs/stats');
        const data = await res.json();
        
        const todayTotal = document.getElementById('today-total');
        const todaySuccess = document.getElementById('today-success');
        const todayFailed = document.getElementById('today-failed');
        
        if (todayTotal) todayTotal.textContent = data.today.total;
        if (todaySuccess) todaySuccess.textContent = data.today.success;
        if (todayFailed) todayFailed.textContent = data.today.failed;
    } catch (e) {
        console.error('Failed to load logs stats:', e);
    }
}

async function loadChannelsForFilter() {
    try {
        const res = await fetch('/api/notifications/channels');
        const channels = await res.json();
        
        const select = document.getElementById('log-filter-channel');
        if (!select) return;
        // 保留第一个选项
        select.innerHTML = '<option value="">全部渠道</option>' +
            channels.map(ch => `<option value="${ch.id}">${ch.name}</option>`).join('');
    } catch (e) {
        console.error('Failed to load channels for filter:', e);
    }
}

async function loadLogs(offset = 0) {
    logsOffset = Math.max(0, offset);
    
    const channelId = document.getElementById('log-filter-channel')?.value || '';
    const alertType = document.getElementById('log-filter-type')?.value || '';
    const success = document.getElementById('log-filter-status')?.value || '';
    
    let url = `/api/notifications/logs?limit=50&offset=${logsOffset}`;
    if (channelId) url += `&channel_id=${channelId}`;
    if (alertType) url += `&alert_type=${alertType}`;
    if (success) url += `&success=${success}`;
    
    try {
        const res = await fetch(url);
        const data = await res.json();
        
        logsTotal = data.total;
        
        const tbody = document.getElementById('logs-tbody');
        const emptyState = document.getElementById('logs-empty');
        
        if (!tbody || !emptyState) return;

        if (data.logs.length === 0) {
            tbody.innerHTML = '';
            emptyState.style.display = 'block';
        } else {
            emptyState.style.display = 'none';
            
            const alertTypeNames = {
                'node_down': '节点离线',
                'node_recovery': '节点恢复',
                'latency_high': '延迟告警',
                'test': '测试通知',
                'manual': '手动发送'
            };
            
            tbody.innerHTML = data.logs.map(log => `
                <tr>
                    <td><input type="checkbox" class="log-checkbox" value="${log.id}" onchange="updateLogsSelectedCount()"></td>
                    <td>${formatTime(log.sent_at)}</td>
                    <td>${log.channel_name}</td>
                    <td>
                        <span class="badge badge-${getAlertTypeBadge(log.alert_type)}">
                            ${alertTypeNames[log.alert_type] || log.alert_type}
                        </span>
                    </td>
                    <td>${log.node_name || '-'}</td>
                    <td class="url-cell" title="${log.subject || ''}">${(log.subject || '-').substring(0, 30)}</td>
                    <td class="url-cell" title="${log.recipients || ''}">${log.recipients || '-'}</td>
                    <td>
                        <span class="badge badge-${log.success ? 'success' : 'danger'}">
                            ${log.success ? '成功' : '失败'}
                        </span>
                        ${!log.success && log.error_message ? `<small style="color:red;" title="${log.error_message}">🛑</small>` : ''}
                    </td>
                    <td>${log.send_duration_ms ? log.send_duration_ms.toFixed(0) + 'ms' : '-'}</td>
                    <td>
                        <button class="btn btn-sm btn-danger" onclick="deleteLog(${log.id})" title="删除">×</button>
                    </td>
                </tr>
            `).join('');
        }
        
        // 更新分页信息
        const logsInfo = document.getElementById('logs-info');
        const logsPrev = document.getElementById('logs-prev');
        const logsNext = document.getElementById('logs-next');
        
        if (logsInfo) logsInfo.textContent = `共 ${logsTotal} 条记录，当前显示 ${logsOffset + 1}-${Math.min(logsOffset + 50, logsTotal)}`;
        if (logsPrev) logsPrev.disabled = logsOffset === 0;
        if (logsNext) logsNext.disabled = logsOffset + 50 >= logsTotal;
    } catch (e) {
        console.error('Failed to load logs:', e);
    }
}

function resetLogFilters() {
    const filterChannel = document.getElementById('log-filter-channel');
    const filterType = document.getElementById('log-filter-type');
    const filterStatus = document.getElementById('log-filter-status');
    
    if (filterChannel) filterChannel.value = '';
    if (filterType) filterType.value = '';
    if (filterStatus) filterStatus.value = '';
    loadLogs(0);
}

// 全选/取消全选
function toggleSelectAllLogs() {
    const selectAll = document.getElementById('log-select-all');
    if (!selectAll) return;
    const isChecked = selectAll.checked;
    const checkboxes = document.querySelectorAll('.log-checkbox');
    checkboxes.forEach(cb => cb.checked = isChecked);
    updateLogsSelectedCount();
}

// 更新选中数量显示
function updateLogsSelectedCount() {
    const checkboxes = document.querySelectorAll('.log-checkbox:checked');
    const count = checkboxes.length;
    const countSpan = document.getElementById('logs-selected-count');
    const deleteBtn = document.getElementById('batch-delete-btn');
    
    if (countSpan) {
        if (count > 0) {
            countSpan.textContent = `已选择 ${count} 条`;
            if (deleteBtn) deleteBtn.style.display = 'inline-block';
        } else {
            countSpan.textContent = '';
            if (deleteBtn) deleteBtn.style.display = 'none';
        }
    }
    
    // 更新全选框状态
    const allCheckboxes = document.querySelectorAll('.log-checkbox');
    const selectAll = document.getElementById('log-select-all');
    if (selectAll) {
        if (allCheckboxes.length > 0 && count === allCheckboxes.length) {
            selectAll.checked = true;
            selectAll.indeterminate = false;
        } else if (count > 0) {
            selectAll.checked = false;
            selectAll.indeterminate = true;
        } else {
            selectAll.checked = false;
            selectAll.indeterminate = false;
        }
    }
}

// 删除单条记录
async function deleteLog(id) {
    if (!confirm('确定要删除这条发送记录吗？')) return;
    
    try {
        const res = await fetch(`/api/notifications/logs/${id}`, {method: 'DELETE'});
        if (res.ok) {
            loadLogs(logsOffset);
            loadLogsStats();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

// 批量删除记录
async function batchDeleteLogs() {
    const checkboxes = document.querySelectorAll('.log-checkbox:checked');
    const ids = Array.from(checkboxes).map(cb => parseInt(cb.value));
    
    if (ids.length === 0) {
        alert('请选择要删除的记录');
        return;
    }
    
    if (!confirm(`确定要删除选中的 ${ids.length} 条发送记录吗？`)) {
        return;
    }
    
    try {
        const res = await fetch('/api/notifications/logs/batch-delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ids: ids})
        });
        if (res.ok) {
            const result = await res.json();
            alert(result.message);
            const selectAll = document.getElementById('log-select-all');
            if (selectAll) selectAll.checked = false;
            updateLogsSelectedCount();
            loadLogs(logsOffset);
            loadLogsStats();
        } else {
            const err = await res.json();
            alert('删除失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

function formatTime(isoStr) {
    if (!isoStr) return '-';
    const d = new Date(isoStr);
    return d.toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function getAlertTypeBadge(type) {
    switch(type) {
        case 'node_down': return 'danger';
        case 'node_recovery': return 'success';
        case 'latency_high': return 'warning';
        case 'test': return 'info';
        default: return 'secondary';
    }
}

// ===================== 告警设置 =====================
async function loadSettings() {
    // 加载告警触发条件配置
    await loadAlertConfig();

    // 加载重复提醒节奏
    await loadAlertCadence();

    // 加载渠道发送规则
    await loadChannelSettings();
}

async function loadAlertConfig() {
    try {
        const res = await fetch('/api/notifications/alerts/config');
        const config = await res.json();

        const alertEnabled = document.getElementById('alert-enabled');
        const alertEnabledText = document.getElementById('alert-enabled-text');
        const alertFailures = document.getElementById('alert-failures');
        const alertLatency = document.getElementById('alert-latency');
        const alertSuppress = document.getElementById('alert-suppress');
        const alertRecoveryNotify = document.getElementById('alert-recovery-notify');

        if (alertEnabled) alertEnabled.checked = config.enabled;
        if (alertEnabledText) alertEnabledText.textContent = config.enabled ? '已启用' : '已禁用';
        if (alertFailures) alertFailures.value = config.consecutive_failures || 3;
        if (alertLatency) alertLatency.value = config.latency_p95_ms || 2000;
        if (alertSuppress) alertSuppress.value = config.suppress_minutes || 240;
        if (alertRecoveryNotify) alertRecoveryNotify.checked = config.notify_on_recovery !== false;
    } catch (e) {
        console.error('Failed to load alert config:', e);
    }
}

// ===================== 提醒节奏配置 =====================

/** 把分钟数格式化为易读的时长 */
function formatMinutes(m) {
    if (m === 0) return '立即';
    if (m < 60) return m + '分';
    if (m % 1440 === 0) return (m / 1440) + '天';
    const h = Math.floor(m / 60);
    const r = m % 60;
    return r ? `${h}小时${r}分` : `${h}小时`;
}

/** 实时预览阶梯对应的实际提醒时刻 */
function updateIntervalsPreview() {
    const input = document.getElementById('alert-reminder-intervals');
    const preview = document.getElementById('alert-intervals-preview');
    if (!input || !preview) return;

    const parts = input.value.split(',').map(s => s.trim()).filter(s => s !== '');
    const nums = parts.map(s => parseInt(s, 10)).filter(n => !isNaN(n) && n >= 0);

    if (!nums.length) {
        preview.textContent = '';
        preview.style.color = '#dc3545';
        preview.textContent = '格式无效，应为逗号分隔的数字';
        return;
    }

    // 计算相邻间隔，便于用户理解"隔多久提醒一次"
    const gaps = nums.map((v, i) => (i === 0 ? v : v - nums[i - 1]));
    preview.style.color = '#17a2b8';
    preview.textContent = '间隔: ' + gaps.map(formatMinutes).join(' → ');
}

async function loadAlertCadence() {
    try {
        const res = await fetch('/api/notifications/alerts/config');
        const config = await res.json();

        const intervalsEl = document.getElementById('alert-reminder-intervals');
        const loopEl = document.getElementById('alert-loop-minutes');
        const quietEnabled = document.getElementById('alert-quiet-enabled');
        const quietStart = document.getElementById('alert-quiet-start');
        const quietEnd = document.getElementById('alert-quiet-end');

        const intervals = config.reminder_intervals_minutes || [];
        if (intervalsEl) intervalsEl.value = intervals.join(',');
        if (loopEl) loopEl.value = config.intervals_loop_minutes || 720;

        const escalateEl = document.getElementById('alert-escalate-minutes');
        if (escalateEl) escalateEl.value = config.escalate_after_minutes || 0;

        const q = config.quiet_hours || {};
        if (quietEnabled) quietEnabled.checked = !!q.enabled;
        if (quietStart && q.start) quietStart.value = q.start;
        if (quietEnd && q.end) quietEnd.value = q.end;

        updateIntervalsPreview();
    } catch (e) {
        console.error('Failed to load alert cadence:', e);
    }
}

async function saveAlertCadence() {
    const intervalsEl = document.getElementById('alert-reminder-intervals');
    const loopEl = document.getElementById('alert-loop-minutes');
    const quietEnabled = document.getElementById('alert-quiet-enabled');
    const quietStart = document.getElementById('alert-quiet-start');
    const quietEnd = document.getElementById('alert-quiet-end');
    const status = document.getElementById('alert-cadence-status');

    // 解析提醒时刻
    const raw = (intervalsEl?.value || '').trim();
    const parts = raw.split(',').map(s => s.trim()).filter(s => s !== '');
    const intervals = [];
    for (const p of parts) {
        const n = parseInt(p, 10);
        if (isNaN(n) || n < 0) {
            alert(`提醒时刻格式错误: "${p}"，应为非负整数`);
            return;
        }
        intervals.push(n);
    }
    if (!intervals.length) {
        alert('提醒时刻不能为空。默认节奏为 0,60,240,480,720');
        return;
    }
    if (intervals.some((v, i) => i > 0 && v < intervals[i - 1])) {
        alert('提醒时刻必须按升序填写，例如 0,60,240,480,720');
        return;
    }

    const quietOn = quietEnabled ? quietEnabled.checked : false;
    if (quietOn && quietStart.value === quietEnd.value) {
        alert('静默时段的开始与结束时间不能相同');
        return;
    }

    const data = {
        reminder_intervals_minutes: intervals,
        intervals_loop_minutes: parseInt(loopEl?.value) || 720,
        escalate_after_minutes: parseInt(document.getElementById('alert-escalate-minutes')?.value) || 0,
        quiet_hours_enabled: quietOn,
        quiet_hours_start: quietStart?.value || '23:00',
        quiet_hours_end: quietEnd?.value || '07:00',
    };

    try {
        const res = await fetch('/api/notifications/alerts/config', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });

        const result = await res.json();
        if (res.ok) {
            if (status) {
                status.textContent = result.persisted === false
                    ? '⚠️ 已生效，但未能写回配置文件'
                    : '✅ 节奏已保存';
                setTimeout(() => { status.textContent = ''; }, 4000);
            }
        } else {
            alert('保存失败: ' + (result.detail?.message || result.detail));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

async function saveAlertConfig() {
    const alertEnabled = document.getElementById('alert-enabled');
    const alertFailures = document.getElementById('alert-failures');
    const alertLatency = document.getElementById('alert-latency');
    const alertSuppress = document.getElementById('alert-suppress');
    const alertRecoveryNotify = document.getElementById('alert-recovery-notify');

    if (!alertEnabled) return;

    const enabled = alertEnabled.checked;
    const failures = parseInt(alertFailures?.value) || 3;
    const latency = parseInt(alertLatency?.value) || 2000;
    const suppress = parseInt(alertSuppress?.value) || 240;
    const notifyOnRecovery = alertRecoveryNotify ? alertRecoveryNotify.checked : true;

    // 更新启用状态文本
    const alertEnabledText = document.getElementById('alert-enabled-text');
    if (alertEnabledText) alertEnabledText.textContent = enabled ? '已启用' : '已禁用';

    const data = {
        enabled: enabled,
        consecutive_failures: failures,
        latency_p95_ms: latency,
        suppress_minutes: suppress,
        notify_on_recovery: notifyOnRecovery,
    };

    try {
        const res = await fetch('/api/notifications/alerts/config', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });

        if (res.ok) {
            const result = await res.json();
            const status = document.getElementById('alert-config-status');
            if (status) {
                status.textContent = result.persisted === false
                    ? '⚠️ 已生效，但未能写回配置文件'
                    : '✅ 配置已保存';
                setTimeout(() => {
                    status.textContent = '';
                }, 4000);
            }
        } else {
            const err = await res.json();
            alert('保存失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

async function loadChannelSettings() {
    try {
        const res = await fetch('/api/notifications/channels');
        const channels = await res.json();
        
        const container = document.getElementById('settings-list');
        if (!container) return;
        
        if (channels.length === 0) {
            container.innerHTML = '<p style="color:#666;">暂无通知渠道，请先添加渠道。</p>';
            return;
        }
        
        container.innerHTML = channels.map(ch => `
            <div class="settings-card" data-id="${ch.id}">
                <div class="settings-header">
                    <h4>${ch.name}</h4>
                    <span class="badge badge-${ch.enabled ? 'success' : 'secondary'}">
                        ${ch.enabled ? '已启用' : '已禁用'}
                    </span>
                </div>
                <div class="settings-body">
                    <div class="setting-row">
                        <label>单个节点每日发送上限</label>
                        <input type="number" class="setting-input" id="limit-${ch.id}" 
                            value="${ch.daily_limit_per_node || 10}" min="0" max="1000">
                        <span class="setting-unit">次/天</span>
                    </div>
                    <div class="setting-row">
                        <label>最小发送间隔</label>
                        <input type="number" class="setting-input" id="interval-${ch.id}" 
                            value="${ch.min_interval_seconds || 10}" min="0" max="3600">
                        <span class="setting-unit">秒</span>
                    </div>
                    <div class="setting-row">
                        <label>可发送时间范围</label>
                        <input type="time" class="setting-input time-input" id="time-start-${ch.id}" 
                            value="${ch.send_time_start || ''}" placeholder="开始">
                        <span class="setting-unit">-</span>
                        <input type="time" class="setting-input time-input" id="time-end-${ch.id}" 
                            value="${ch.send_time_end || ''}" placeholder="结束">
                        <small style="color:#666;margin-left:10px;">留空表示不限制</small>
                    </div>
                </div>
                <div class="settings-footer">
                    <button class="btn btn-sm btn-primary" onclick="saveChannelSettings(${ch.id})">保存设置</button>
                </div>
            </div>
        `).join('');
    } catch (e) {
        console.error('Failed to load channel settings:', e);
    }
}

async function saveChannelSettings(channelId) {
    const dailyLimit = parseInt(document.getElementById(`limit-${channelId}`)?.value) || 10;
    const minInterval = parseInt(document.getElementById(`interval-${channelId}`)?.value) || 10;
    const timeStart = document.getElementById(`time-start-${channelId}`)?.value || null;
    const timeEnd = document.getElementById(`time-end-${channelId}`)?.value || null;
    
    const data = {
        daily_limit_per_node: dailyLimit,
        min_interval_seconds: minInterval,
        send_time_start: timeStart,
        send_time_end: timeEnd,
    };
    
    try {
        const res = await fetch(`/api/notifications/channels/${channelId}/settings`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        
        if (res.ok) {
            alert('设置保存成功');
        } else {
            const err = await res.json();
            alert('保存失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('保存失败: ' + e.message);
    }
}

// 页面加载
document.addEventListener('DOMContentLoaded', () => {
    loadRecipients();  // 默认加载通知人员
    initTabs();

    // 提醒时刻输入框实时预览
    const intervalsInput = document.getElementById('alert-reminder-intervals');
    if (intervalsInput) {
        intervalsInput.addEventListener('input', updateIntervalsPreview);
    }
});
