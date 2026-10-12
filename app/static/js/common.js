// 纯文本和 HTML 属性统一转义；JS 参数先 JSON 编码再做属性转义。
function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
function htmlJsArg(value) { return escapeHtml(JSON.stringify(String(value ?? ''))); }

/* 服务器监控面板 - 前端脚本 */

// 主题切换功能
function initThemeToggle() {
    const body = document.body;
    const themeToggle = document.getElementById('themeToggle');
    if (!themeToggle) return;

    const moonIcon = document.getElementById('moonIcon');
    const sunIcon = document.getElementById('sunIcon');

    function updateThemeUI(isDark) {
        if (isDark) {
            body.classList.add('dark-mode');
            if (moonIcon) moonIcon.style.display = 'block';
            if (sunIcon) sunIcon.style.display = 'none';
        } else {
            body.classList.remove('dark-mode');
            if (moonIcon) moonIcon.style.display = 'none';
            if (sunIcon) sunIcon.style.display = 'block';
        }
    }

    // 初始化主题（与登录页共用同一个 localStorage key）
    const savedTheme = localStorage.getItem('login-theme');
    if (savedTheme === 'dark') {
        updateThemeUI(true);
    } else {
        updateThemeUI(false); // 默认白天模式
    }

    // 点击切换
    themeToggle.addEventListener('click', () => {
        const isDark = body.classList.toggle('dark-mode');
        localStorage.setItem('login-theme', isDark ? 'dark' : 'light');
        updateThemeUI(isDark);
    });
}

// 格式化运行时长
function formatUptime(seconds) {
    if (seconds < 60) {
        return Math.round(seconds) + 's';
    } else if (seconds < 3600) {
        return Math.round(seconds / 60) + 'm';
    } else if (seconds < 86400) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        return h + 'h ' + m + 'm';
    } else {
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        return d + 'd ' + h + 'h';
    }
}

// 更新页脚运行时长
async function updateFooterUptime() {
    try {
        const res = await fetch('/healthz');
        const data = await res.json();
        const uptimeEl = document.getElementById('uptime');
        if (uptimeEl) {
            uptimeEl.textContent = 'Uptime: ' + formatUptime(data.uptime_seconds);
        }
    } catch (e) {
        console.error('Failed to fetch health status:', e);
    }
}

// 初始化
document.addEventListener('DOMContentLoaded', () => {
    // 初始化主题
    initThemeToggle();
    
    // 更新运行时长
    updateFooterUptime();
    setInterval(updateFooterUptime, 10000);
});

// 通用 API 请求函数
async function apiRequest(url, method = 'GET', body = null) {
    const options = {
        method,
        headers: {
            'Content-Type': 'application/json',
        },
    };
    
    if (body) {
        options.body = JSON.stringify(body);
    }
    
    const res = await fetch(url, options);
    
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail?.message || err.detail || 'Request failed');
    }
    
    return res.json();
}

// 显示通知
function showNotification(message, type = 'info') {
    // 简单的 alert，可以替换为更好的通知组件
    alert(message);
}

// 确认对话框
function confirmAction(message) {
    return confirm(message);
}

console.log('Server Monitor Dashboard loaded');
