let nodeLatencyChart = null;

async function pingNode(nodeName) {
    const resultBox = document.getElementById('latestResult');
    if (!resultBox) return;
    resultBox.innerHTML = '<p>检测中...</p>';
    
    try {
        const res = await fetch(`/api/nodes/${encodeURIComponent(nodeName)}/ping`, {method: 'POST'});
        const data = await res.json();
        
        resultBox.innerHTML = `
            <div class="result-item">
                <label>时间</label>
                <span>${data.timestamp}</span>
            </div>
            <div class="result-item">
                <label>状态码</label>
                <span>${data.status_code}</span>
            </div>
            <div class="result-item">
                <label>延迟</label>
                <span>${data.latency_ms}ms</span>
            </div>
            <div class="result-item">
                <label>成功</label>
                <span class="badge ${data.success ? 'badge-success' : 'badge-danger'}">${data.success ? '是' : '否'}</span>
            </div>
            ${data.error_type ? `<div class="result-item"><label>错误类型</label><span>${data.error_type}</span></div>` : ''}
            ${data.error_message ? `<div class="result-item"><label>错误信息</label><span>${data.error_message}</span></div>` : ''}
        `;
    } catch (e) {
        resultBox.innerHTML = `<p class="error">检测失败: ${e.message}</p>`;
    }
}

// 加载延迟趋势图
async function loadNodeLatencyChart(nodeName) {
    const canvas = document.getElementById('nodeLatencyChart');
    if (!canvas) return;
    
    try {
        const res = await fetch(`/api/nodes/stats/latency-trend?hours=24`);
        const data = await res.json();
        
        // 获取当前节点的数据
        const nodeData = data.nodes[nodeName];
        
        if (!nodeData || nodeData.length === 0) {
            canvas.parentElement.innerHTML = 
                '<p style="text-align:center;color:#666;padding:40px;">暂无延迟数据</p>';
            return;
        }
        
        // 准备图表数据
        const labels = nodeData.map(item => {
            const date = new Date(item.hour);
            return date.getHours() + ':00';
        });
        
        const avgLatency = nodeData.map(item => item.avg_latency);
        const maxLatency = nodeData.map(item => item.max_latency);
        const availability = nodeData.map(item => item.availability);
        
        const ctx = canvas.getContext('2d');
        
        // 销毁旧图表
        if (nodeLatencyChart) {
            nodeLatencyChart.destroy();
        }
        
        // 创建新图表
        nodeLatencyChart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: '平均延迟 (ms)',
                        data: avgLatency,
                        borderColor: 'rgb(54, 162, 235)',
                        backgroundColor: 'rgba(54, 162, 235, 0.1)',
                        fill: true,
                        tension: 0.3,
                        yAxisID: 'y',
                    },
                    {
                        label: '最大延迟 (ms)',
                        data: maxLatency,
                        borderColor: 'rgba(255, 99, 132, 0.5)',
                        borderDash: [5, 5],
                        fill: false,
                        tension: 0.3,
                        pointRadius: 1,
                        yAxisID: 'y',
                    },
                    {
                        label: '可用性 (%)',
                        data: availability,
                        borderColor: 'rgb(75, 192, 192)',
                        backgroundColor: 'rgba(75, 192, 192, 0.1)',
                        fill: false,
                        tension: 0.3,
                        pointRadius: 2,
                        yAxisID: 'y1',
                    }
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: {
                    mode: 'index',
                    intersect: false,
                },
                plugins: {
                    legend: {
                        position: 'top',
                        labels: { color: '#e0e0e0' }
                    },
                    title: {
                        display: true,
                        text: '近24小时延迟趋势',
                        color: '#e0e0e0',
                    }
                },
                scales: {
                    x: {
                        grid: { color: 'rgba(255,255,255,0.1)' },
                        ticks: { color: '#a0a0a0' }
                    },
                    y: {
                        type: 'linear',
                        display: true,
                        position: 'left',
                        beginAtZero: true,
                        grid: { color: 'rgba(255,255,255,0.1)' },
                        ticks: {
                            color: '#a0a0a0',
                            callback: value => value + 'ms'
                        },
                        title: {
                            display: true,
                            text: '延迟 (ms)',
                            color: '#a0a0a0',
                        }
                    },
                    y1: {
                        type: 'linear',
                        display: true,
                        position: 'right',
                        min: 0,
                        max: 100,
                        grid: { drawOnChartArea: false },
                        ticks: {
                            color: '#a0a0a0',
                            callback: value => value + '%'
                        },
                        title: {
                            display: true,
                            text: '可用性 (%)',
                            color: '#a0a0a0',
                        }
                    },
                },
            },
        });
    } catch (e) {
        console.error('Failed to load latency chart:', e);
        canvas.parentElement.innerHTML = 
            `<p style="text-align:center;color:#f00;padding:40px;">加载图表失败: ${e.message}</p>`;
    }
}
