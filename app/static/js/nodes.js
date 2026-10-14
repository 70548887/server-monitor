function showAddNodeModal() {
    document.getElementById('addNodeModal').style.display = 'flex';
}

function hideAddNodeModal() {
    document.getElementById('addNodeModal').style.display = 'none';
}

document.getElementById('addNodeForm').onsubmit = async (e) => {
    e.preventDefault();
    const form = e.target;
    
    // 解析标签（逗号分隔）
    const tagsInput = form.tags.value.trim();
    const tags = tagsInput ? tagsInput.split(',').map(t => t.trim()).filter(t => t) : [];
    
    const data = {
        name: form.name.value,
        url: form.url.value,
        region: form.region.value || 'default',
        timeout_ms: parseInt(form.timeout_ms.value) || 2000,
        enabled: true,
        tags: tags,
        expect: {
            status: parseInt(form.expect_status.value) || 200,
            body_contains: form.expect_body_contains.value || null
        }
    };
    
    try {
        const res = await fetch('/api/nodes', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        if (res.ok) {
            location.reload();
        } else {
            const err = await res.json();
            alert('添加失败: ' + (err.detail?.message || err.detail));
        }
    } catch (e) {
        alert('请求失败: ' + e.message);
    }
};

async function pingNode(name) {
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(name)}/ping`, {method: 'POST'});
        const data = await res.json();
        alert(`Ping 结果:\n状态码: ${data.status_code}\n延迟: ${data.latency_ms}ms\n成功: ${data.success}`);
    } catch (e) {
        alert('Ping 失败: ' + e.message);
    }
}

async function toggleNode(name, enabled) {
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(name)}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled})
        });
        if (res.ok) {
            location.reload();
        }
    } catch (e) {
        alert('操作失败: ' + e.message);
    }
}

async function deleteNode(name) {
    if (!confirm(`确定要删除节点 ${name} 吗？`)) return;
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(name)}?hard=true`, {method: 'DELETE'});
        if (res.ok) {
            location.reload();
        }
    } catch (e) {
        alert('删除失败: ' + e.message);
    }
}

// ============= 编辑节点功能 =============
let editNodeLoadController = null;
function setEditNodeBusy(busy) {
    document.querySelector('#editNodeForm button[type="submit"]').disabled = busy;
}

function showEditNodeModal(nodeName) {
    // 显示弹窗
    document.getElementById('editNodeModal').style.display = 'flex';
    document.getElementById('editNodeName').textContent = nodeName;
    document.getElementById('editOriginalName').value = nodeName;
    document.getElementById('editName').value = nodeName;
    setEditNodeBusy(true);
    
    // 加载节点数据
    loadNodeForEdit(nodeName);
}

function hideEditNodeModal() {
    if (editNodeLoadController) editNodeLoadController.abort();
    document.getElementById('editNodeModal').style.display = 'none';
}

async function loadNodeForEdit(nodeName) {
    if (editNodeLoadController) editNodeLoadController.abort();
    const controller = new AbortController();
    editNodeLoadController = controller;
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}`, {signal: controller.signal});
        if (!res.ok) throw new Error('获取节点信息失败');
        const node = await res.json();
        if (controller.signal.aborted || editNodeLoadController !== controller) return;
        document.getElementById('editName').value = node.name || nodeName;
        document.getElementById('editUrl').value = node.url || '';
        document.getElementById('editRegion').value = node.region || 'default';
        document.getElementById('editTags').value = (node.tags || []).join(', ');
        document.getElementById('editTimeout').value = node.timeout_ms || 2000;
        document.getElementById('editExpectStatus').value = node.expect?.status || 200;
        document.getElementById('editExpectBody').value = node.expect?.body_contains || '';
        document.getElementById('editEnabled').value = node.enabled ? 'true' : 'false';
        updateTagsPreview();
        setEditNodeBusy(false);
    } catch (error) {
        if (error.name === 'AbortError') return;
        alert('加载节点信息失败: ' + error.message);
        hideEditNodeModal();
    }
}

// 标签预览功能
const editTagsInput = document.getElementById('editTags');
if (editTagsInput) {
    editTagsInput.addEventListener('input', updateTagsPreview);
}

function updateTagsPreview() {
    const inputEl = document.getElementById('editTags');
    if (!inputEl) return;
    const input = inputEl.value;
    const preview = document.getElementById('editTagsPreview');
    const tags = input.split(',').map(t => t.trim()).filter(t => t);
    
    if (tags.length > 0) {
        preview.innerHTML = tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
    } else {
        preview.innerHTML = '';
    }
}

// 编辑表单提交
const editNodeForm = document.getElementById('editNodeForm');
if (editNodeForm) {
    editNodeForm.onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target;
        const nodeName = document.getElementById('editOriginalName').value;
        const newName = document.getElementById('editName').value;
        if (!newName.trim()) { alert('节点名称不能为空'); return; }
        
        // 解析标签
        const tagsInput = form.tags.value.trim();
        const tags = tagsInput ? tagsInput.split(',').map(t => t.trim()).filter(t => t) : [];
        
        const data = {
            url: form.url.value,
            region: form.region.value || 'default',
            tags: tags,
            timeout_ms: parseInt(form.timeout_ms.value) || 2000,
            enabled: form.enabled.value === 'true',
            expect: {
                status: parseInt(form.expect_status.value) || 200,
                body_contains: form.expect_body_contains.value || null
            }
        };
        
        if (newName !== nodeName) data.name = newName;
        setEditNodeBusy(true);
        try {
            const res = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });
            
            if (res.ok) {
                alert('保存成功');
                location.reload();
            } else {
                const err = await res.json();
                alert('保存失败: ' + (err.detail?.message || (Array.isArray(err.detail) ? err.detail.map(item => item.msg).join('；') : err.detail) || '未知错误'));
            }
        } catch (e) {
            alert('请求失败: ' + e.message);
        } finally {
            setEditNodeBusy(false);
        }
    };
}

// ============= 可用性统计加载 =============

// 页面加载后获取可用性数据
document.addEventListener('DOMContentLoaded', () => {
    loadAllAvailability();
    // 每30秒刷新一次
    setInterval(loadAllAvailability, 30000);
});

async function loadAllAvailability() {
    try {
        const res = await fetch('/api/nodes/stats/availability');
        const data = await res.json();
        
        data.nodes.forEach(node => {
            updateNodeUptime(node.node_name, node.uptime_24h, node.current_status);
        });
        
        // 加载每个节点的趋势数据
        const nodeRows = document.querySelectorAll('#nodes-table tbody tr');
        nodeRows.forEach(row => {
            const nodeName = row.getAttribute('data-node');
            if (nodeName) {
                loadNodeTrend(nodeName);
            }
        });
    } catch (e) {
        console.error('Failed to load availability:', e);
    }
}

function updateNodeUptime(nodeName, uptime, status) {
    const uptimeCell = document.querySelector(`.uptime-cell[data-node="${CSS.escape(nodeName)}"] .uptime-value`);
    const statusCell = document.querySelector(`.status-cell[data-node="${CSS.escape(nodeName)}"] .badge`);
    
    if (uptimeCell) {
        uptimeCell.textContent = uptime.toFixed(2) + '%';
        // 根据可用性设置颜色
        uptimeCell.className = 'uptime-value';
        if (uptime >= 99) {
            uptimeCell.classList.add('uptime-good');
        } else if (uptime >= 95) {
            uptimeCell.classList.add('uptime-warn');
        } else {
            uptimeCell.classList.add('uptime-bad');
        }
    }
    
    if (statusCell) {
        statusCell.className = 'badge';
        if (status === 'up') {
            statusCell.classList.add('badge-success');
            statusCell.textContent = '在线';
        } else if (status === 'down') {
            statusCell.classList.add('badge-danger');
            statusCell.textContent = '离线';
        } else {
            statusCell.classList.add('badge-secondary');
            statusCell.textContent = '未知';
        }
    }
}

async function loadNodeTrend(nodeName) {
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}/availability/trend?hours=24`);
        const data = await res.json();
        renderTrendBar(nodeName, data.data);
    } catch (e) {
        console.error(`Failed to load trend for ${nodeName}:`, e);
    }
}

function renderTrendBar(nodeName, trendData) {
    const trendCell = document.querySelector(`.trend-cell[data-node="${CSS.escape(nodeName)}"] .trend-bar`);
    if (!trendCell) return;
    
    // 清空现有内容
    trendCell.innerHTML = '';
    
    // 取最后24个小时的数据
    const recentData = trendData.slice(-24);
    
    recentData.forEach(item => {
        const bar = document.createElement('span');
        bar.className = 'trend-segment';
        // 将 UTC 时间转换为本地时间显示
        const utcDate = new Date(item.hour);
        const localHour = utcDate.getHours().toString().padStart(2, '0');
        const localMinute = utcDate.getMinutes().toString().padStart(2, '0');
        bar.title = `${localHour}:${localMinute} - ${item.availability}%`;
        
        if (item.total === 0) {
            bar.classList.add('trend-unknown');
        } else if (item.availability >= 99) {
            bar.classList.add('trend-up');
        } else if (item.availability >= 90) {
            bar.classList.add('trend-degraded');
        } else {
            bar.classList.add('trend-down');
        }
        
        trendCell.appendChild(bar);
    });
}
