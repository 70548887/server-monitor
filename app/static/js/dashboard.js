// 定时刷新页面数据
setInterval(() => {
    fetch('/healthz')
        .then(r => r.json())
        .then(data => {
            const uptimeDisplay = document.getElementById('uptime-display');
            if (uptimeDisplay) {
                uptimeDisplay.textContent = Math.round(data.uptime_seconds) + 's';
            }
            updateComponentStatus(data.components);
        })
        .catch(console.error);
}, 5000);

// 检测存储
function pingStorage() {
    const btn = document.getElementById('ping-storage-btn');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '检测中...';
    
    fetch('/healthz/ping-storage', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            // 刷新页面获取最新状态
            return fetch('/healthz').then(r => r.json());
        })
        .then(data => {
            updateComponentStatus(data.components);
            btn.textContent = '检测完成';
            setTimeout(() => {
                btn.disabled = false;
                btn.textContent = '检测存储';
            }, 1500);
        })
        .catch(err => {
            console.error(err);
            btn.disabled = false;
            btn.textContent = '检测失败';
            setTimeout(() => { btn.textContent = '检测存储'; }, 2000);
        });
}

// 状态中文映射
function getStatusText(status) {
    const statusMap = {
        'up': '正常',
        'down': '异常',
        'unknown': '未知',
        'running': '运行中',
        'stopped': '已停止'
    };
    return statusMap[status] || status;
}

// 更新组件状态显示
function updateComponentStatus(components) {
    // 更新 MySQL
    const mysqlBadge = document.querySelector('#component-table tbody tr:nth-child(1) .badge');
    const mysqlLatency = document.querySelector('#component-table tbody tr:nth-child(1) td:nth-child(3)');
    if (mysqlBadge && components.mysql) {
        mysqlBadge.textContent = getStatusText(components.mysql.status);
        mysqlBadge.className = 'badge ' + (components.mysql.status === 'up' ? 'badge-success' : 'badge-danger');
        mysqlLatency.textContent = components.mysql.latency_ms + 'ms';
    }
    
    // 更新 Redis
    const redisBadge = document.querySelector('#component-table tbody tr:nth-child(2) .badge');
    const redisLatency = document.querySelector('#component-table tbody tr:nth-child(2) td:nth-child(3)');
    if (redisBadge && components.redis) {
        redisBadge.textContent = getStatusText(components.redis.status);
        redisBadge.className = 'badge ' + (components.redis.status === 'up' ? 'badge-success' : 
            components.redis.status === 'unknown' ? 'badge-warning' : 'badge-danger');
        redisLatency.textContent = components.redis.latency_ms + 'ms';
    }
}

// ============= 图表实现 =============

let latencyChart = null;
let availabilityChart = null;

// 颜色配置
const chartColors = [
    'rgb(75, 192, 192)',   // 青色
    'rgb(255, 99, 132)',   // 红色
    'rgb(54, 162, 235)',   // 蓝色
    'rgb(255, 206, 86)',   // 黄色
    'rgb(153, 102, 255)',  // 紫色
];

// 加载延迟趋势图
async function loadLatencyChart() {
    const chartHoursEl = document.getElementById('chart-hours');
    if (!chartHoursEl) return;
    const hours = chartHoursEl.value;
    
    try {
        const res = await fetch(`/api/nodes/stats/latency-trend?hours=${hours}`);
        const data = await res.json();
        
        renderLatencyChart(data);
        renderAvailabilityChart(data);
    } catch (e) {
        console.error('Failed to load chart data:', e);
    }
}

// 渲染延迟曲线图
function renderLatencyChart(data) {
    const canvas = document.getElementById('latencyChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    // 准备数据集
    const datasets = [];
    const nodeNames = Object.keys(data.nodes);
    
    // 获取时间标签（从第一个节点的数据中提取）
    let labels = [];
    if (nodeNames.length > 0 && data.nodes[nodeNames[0]].length > 0) {
        labels = data.nodes[nodeNames[0]].map(item => {
            const date = new Date(item.hour);
            return date.getHours() + ':00';
        });
    }
    
    // 为每个节点创建数据集
    nodeNames.forEach((name, index) => {
        const nodeData = data.nodes[name];
        datasets.push({
            label: name,
            data: nodeData.map(item => item.avg_latency),
            borderColor: chartColors[index % chartColors.length],
            backgroundColor: chartColors[index % chartColors.length].replace('rgb', 'rgba').replace(')', ', 0.1)'),
            fill: false,
            tension: 0.3,
            pointRadius: 2,
        });
    });
    
    // 销毁旧图表
    if (latencyChart) {
        latencyChart.destroy();
    }
    
    // 创建新图表
    latencyChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: datasets,
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { color: '#e0e0e0' }
                },
                title: {
                    display: false,
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

// 渲染可用性曲线图
function renderAvailabilityChart(data) {
    const canvas = document.getElementById('availabilityChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    const datasets = [];
    const nodeNames = Object.keys(data.nodes);
    
    let labels = [];
    if (nodeNames.length > 0 && data.nodes[nodeNames[0]].length > 0) {
        labels = data.nodes[nodeNames[0]].map(item => {
            const date = new Date(item.hour);
            return date.getHours() + ':00';
        });
    }
    
    nodeNames.forEach((name, index) => {
        const nodeData = data.nodes[name];
        datasets.push({
            label: name,
            data: nodeData.map(item => item.availability),
            borderColor: chartColors[index % chartColors.length],
            backgroundColor: chartColors[index % chartColors.length].replace('rgb', 'rgba').replace(')', ', 0.1)'),
            fill: false,
            tension: 0.3,
            pointRadius: 2,
        });
    });
    
    if (availabilityChart) {
        availabilityChart.destroy();
    }
    
    availabilityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: datasets,
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { color: '#e0e0e0' }
                },
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

// 页面加载时初始化图表
document.addEventListener('DOMContentLoaded', () => {
    loadLatencyChart();
    // 每60秒刷新图表
    setInterval(loadLatencyChart, 60000);
});
