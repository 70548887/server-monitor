// 图表实例
let latencyTrendChart = null;
let availabilityTrendChart = null;

// 颜色配置
const chartColors = [
    'rgb(54, 162, 235)',   // 蓝色
    'rgb(255, 99, 132)',   // 红色
    'rgb(75, 192, 192)',   // 青色
    'rgb(255, 206, 86)',   // 黄色
    'rgb(153, 102, 255)',  // 紫色
    'rgb(255, 159, 64)',   // 橙色
    'rgb(46, 204, 113)',   // 绿色
    'rgb(231, 76, 60)',    // 深红
    'rgb(52, 152, 219)',   // 天蓝
    'rgb(155, 89, 182)',   // 紫罗兰
];

// 节点选择器交互
const selectedNodesEl = document.getElementById('selectedNodes');
if (selectedNodesEl) {
    selectedNodesEl.addEventListener('click', function(e) {
        const dropdown = document.getElementById('nodeDropdown');
        if (dropdown) {
            dropdown.style.display = dropdown.style.display === 'none' ? 'block' : 'none';
        }
        e.stopPropagation();
    });
}

document.addEventListener('click', function(e) {
    const selector = document.getElementById('nodeSelector');
    const dropdown = document.getElementById('nodeDropdown');
    if (selector && dropdown && !selector.contains(e.target)) {
        dropdown.style.display = 'none';
    }
});

function selectAllNodes() {
    document.querySelectorAll('#nodeList input[type="checkbox"]').forEach(cb => cb.checked = true);
    updateSelectedNodes();
}

function clearAllNodes() {
    document.querySelectorAll('#nodeList input[type="checkbox"]').forEach(cb => cb.checked = false);
    updateSelectedNodes();
}

function updateSelectedNodes() {
    const selectedNodesEl = document.getElementById('selectedNodes');
    if (!selectedNodesEl) return;
    const checkboxes = document.querySelectorAll('#nodeList input[type="checkbox"]:checked');
    
    if (checkboxes.length === 0) {
        selectedNodesEl.innerHTML = '<span class="placeholder">点击选择节点...</span>';
    } else {
        const names = Array.from(checkboxes).map(cb => cb.value);
        selectedNodesEl.innerHTML = names.map(name => 
            `<span class="selected-tag">${escapeHtml(name)}</span>`
        ).join('');
    }
}

function getSelectedNodes() {
    const checkboxes = document.querySelectorAll('#nodeList input[type="checkbox"]:checked');
    return Array.from(checkboxes).map(cb => cb.value);
}

// 加载趋势数据
async function loadTrendsData() {
    const selectedNodes = getSelectedNodes();
    const timeRangeEl = document.getElementById('timeRange');
    if (!timeRangeEl) return;
    const hours = timeRangeEl.value;
    
    if (selectedNodes.length === 0) {
        alert('请至少选择一个节点');
        return;
    }
    
    try {
        const res = await fetch(`/api/nodes/stats/latency-trend?hours=${hours}`);
        const data = await res.json();
        
        // 过滤选中的节点数据
        const filteredNodes = {};
        selectedNodes.forEach(name => {
            if (data.nodes[name]) {
                filteredNodes[name] = data.nodes[name];
            }
        });
        
        renderLatencyStats(filteredNodes);
        renderLatencyTrendChart(filteredNodes);
        renderAvailabilityTrendChart(filteredNodes);
        
    } catch (e) {
        console.error('加载趋势数据失败:', e);
        alert('加载数据失败: ' + e.message);
    }
}

// 渲染延迟统计表格
function renderLatencyStats(nodesData) {
    const tbody = document.getElementById('latencyStatsBody');
    if (!tbody) return;
    
    // 计算各节点统计数据
    const stats = [];
    for (const [nodeName, hourlyData] of Object.entries(nodesData)) {
        const latencies = hourlyData.filter(h => h.total > 0).map(h => h.avg_latency);
        const maxLatencies = hourlyData.filter(h => h.total > 0).map(h => h.max_latency);
        const availabilities = hourlyData.filter(h => h.total > 0).map(h => h.availability);
        
        if (latencies.length > 0) {
            const avgLatency = latencies.reduce((a, b) => a + b, 0) / latencies.length;
            const maxLatency = Math.max(...maxLatencies);
            const minLatency = Math.min(...latencies.filter(l => l > 0));
            const avgAvailability = availabilities.reduce((a, b) => a + b, 0) / availabilities.length;
            
            stats.push({
                name: nodeName,
                avgLatency: avgLatency,
                maxLatency: maxLatency,
                minLatency: minLatency || 0,
                availability: avgAvailability,
            });
        }
    }
    
    // 按平均延迟排序（从高到低）
    stats.sort((a, b) => b.avgLatency - a.avgLatency);
    
    // 渲染表格
    if (stats.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center">无数据</td></tr>';
        return;
    }
    
    tbody.innerHTML = stats.map((s, idx) => {
        // 延迟等级
        let level, levelClass;
        if (s.avgLatency < 100) {
            level = '优秀'; levelClass = 'badge-success';
        } else if (s.avgLatency < 300) {
            level = '良好'; levelClass = 'badge-success';
        } else if (s.avgLatency < 500) {
            level = '一般'; levelClass = 'badge-warning';
        } else if (s.avgLatency < 1000) {
            level = '较慢'; levelClass = 'badge-warning';
        } else {
            level = '很慢'; levelClass = 'badge-danger';
        }
        
        return `
            <tr class="${idx === 0 ? 'highlight-row' : ''}">
                <td><strong>${escapeHtml(s.name)}</strong>${idx === 0 ? ' <span class="badge badge-danger">延迟最高</span>' : ''}</td>
                <td>${s.avgLatency.toFixed(2)}ms</td>
                <td>${s.maxLatency.toFixed(2)}ms</td>
                <td>${s.minLatency.toFixed(2)}ms</td>
                <td>${s.availability.toFixed(2)}%</td>
                <td><span class="badge ${levelClass}">${level}</span></td>
            </tr>
        `;
    }).join('');
}

// 渲染延迟趋势图表
function renderLatencyTrendChart(nodesData) {
    const canvas = document.getElementById('latencyTrendChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    const datasets = [];
    const nodeNames = Object.keys(nodesData);
    
    // 获取时间标签
    let labels = [];
    if (nodeNames.length > 0 && nodesData[nodeNames[0]].length > 0) {
        labels = nodesData[nodeNames[0]].map(item => {
            const date = new Date(item.hour);
            return date.getHours() + ':00';
        });
    }
    
    // 为每个节点创建数据集
    nodeNames.forEach((name, index) => {
        const nodeData = nodesData[name];
        datasets.push({
            label: name,
            data: nodeData.map(item => item.avg_latency),
            borderColor: chartColors[index % chartColors.length],
            backgroundColor: chartColors[index % chartColors.length].replace('rgb', 'rgba').replace(')', ', 0.1)'),
            fill: false,
            tension: 0.3,
            pointRadius: 3,
            borderWidth: 2,
        });
    });
    
    if (latencyTrendChart) {
        latencyTrendChart.destroy();
    }
    
    latencyTrendChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { color: '#e0e0e0', padding: 15 }
                },
                title: {
                    display: true,
                    text: '各节点延迟趋势对比',
                    color: '#e0e0e0',
                    font: { size: 16 }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#a0a0a0' }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: {
                        color: '#a0a0a0',
                        callback: value => value + 'ms'
                    },
                    title: {
                        display: true,
                        text: '延迟 (ms)',
                        color: '#a0a0a0'
                    }
                }
            },
            interaction: {
                intersect: false,
                mode: 'index',
            },
        }
    });
}

// 渲染可用性趋势图表
function renderAvailabilityTrendChart(nodesData) {
    const canvas = document.getElementById('availabilityTrendChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    const datasets = [];
    const nodeNames = Object.keys(nodesData);
    
    let labels = [];
    if (nodeNames.length > 0 && nodesData[nodeNames[0]].length > 0) {
        labels = nodesData[nodeNames[0]].map(item => {
            const date = new Date(item.hour);
            return date.getHours() + ':00';
        });
    }
    
    nodeNames.forEach((name, index) => {
        const nodeData = nodesData[name];
        datasets.push({
            label: name,
            data: nodeData.map(item => item.availability),
            borderColor: chartColors[index % chartColors.length],
            backgroundColor: chartColors[index % chartColors.length].replace('rgb', 'rgba').replace(')', ', 0.1)'),
            fill: false,
            tension: 0.3,
            pointRadius: 3,
            borderWidth: 2,
        });
    });
    
    if (availabilityTrendChart) {
        availabilityTrendChart.destroy();
    }
    
    availabilityTrendChart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { color: '#e0e0e0', padding: 15 }
                },
                title: {
                    display: true,
                    text: '各节点可用性趋势对比',
                    color: '#e0e0e0',
                    font: { size: 16 }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: { color: '#a0a0a0' }
                },
                y: {
                    min: 0,
                    max: 100,
                    grid: { color: 'rgba(255,255,255,0.1)' },
                    ticks: {
                        color: '#a0a0a0',
                        callback: value => value + '%'
                    },
                    title: {
                        display: true,
                        text: '可用性 (%)',
                        color: '#a0a0a0'
                    }
                }
            },
            interaction: {
                intersect: false,
                mode: 'index',
            },
        }
    });
}
