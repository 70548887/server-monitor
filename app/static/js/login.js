// 昼夜模式切换逻辑
const body = document.body;
const themeToggle = document.getElementById('themeToggle');
if (themeToggle) {
    const moonPath = document.getElementById('moonPath');
    const sunElements = [
        'sunCircle', 'sunLine1', 'sunLine2', 'sunLine3', 'sunLine4', 
        'sunLine5', 'sunLine6', 'sunLine7', 'sunLine8'
    ].map(id => document.getElementById(id));

    function updateThemeUI(isDark) {
        if (isDark) {
            body.classList.add('dark-mode');
            if (moonPath) moonPath.style.display = 'block';
            sunElements.forEach(el => { if (el) el.style.display = 'none'; });
        } else {
            body.classList.remove('dark-mode');
            if (moonPath) moonPath.style.display = 'none';
            sunElements.forEach(el => { if (el) el.style.display = 'block'; });
        }
    }

    // 初始化主题
    const savedTheme = localStorage.getItem('login-theme');
    if (savedTheme === 'dark') {
        updateThemeUI(true);
    } else {
        updateThemeUI(false); // 默认白天模式
    }

    themeToggle.addEventListener('click', () => {
        const isDark = body.classList.toggle('dark-mode');
        localStorage.setItem('login-theme', isDark ? 'dark' : 'light');
        updateThemeUI(isDark);
    });
}

// 滑动验证码逻辑
function initSliderCaptcha() {
    const container = document.getElementById('captcha-container');
    const loginBtn = document.getElementById('loginBtn');
    const captchaIdInput = document.getElementById('captcha_id');
    const captchaXInput = document.getElementById('captcha_x');
    const captchaTrailInput = document.getElementById('captcha_trail');
    
    if (!container) return;

    let isVerified = false;

    async function loadCaptcha() {
        try {
            const res = await fetch('/api/auth/captcha');
            const data = await res.json();
            
            renderSlider(data);
        } catch (e) {
            container.innerHTML = '<p style="color:red;font-size:12px;text-align:center;">验证码加载失败，请刷新页面</p>';
        }
    }

    function renderSlider(data) {
        container.innerHTML = `
            <div class="slider-captcha">
                <div class="slider-bg-container">
                    <img src="${data.bg}" class="slider-bg-img">
                    <img src="${data.slider}" class="slider-piece-img" id="sliderPiece" style="top: ${data.y}px">
                </div>
                <div class="slider-track" id="sliderTrack">
                    <div class="slider-fill" id="sliderFill"></div>
                    <div class="slider-text">向右滑动完成验证</div>
                    <div class="slider-handle" id="sliderHandle">
                        <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M8.59,16.59L13.17,12L8.59,7.41L10,6L16,12L10,18L8.59,16.59Z"/></svg>
                    </div>
                </div>
            </div>
        `;

        const handle = document.getElementById('sliderHandle');
        const piece = document.getElementById('sliderPiece');
        const track = document.getElementById('sliderTrack');
        const fill = document.getElementById('sliderFill');
        
        let startX = 0;
        let moveX = 0;
        const maxMove = track.offsetWidth - handle.offsetWidth;
        // 滑动轨迹：[[x, timeStamp], ...]
        // 服务端据此判断是否由真人操作 —— 脚本直接设置 left 属性时，
        // 轨迹要么为空，要么是一条完美的等速直线
        let trail = [];
        const trailStart = Date.now();

        function onDown(e) {
            if (isVerified) return;
            const clientX = e.type === 'touchstart' ? e.touches[0].clientX : e.clientX;
            startX = clientX;
            trail = [[0, Date.now() - trailStart]];

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onEnd);
            document.addEventListener('touchmove', onMove);
            document.addEventListener('touchend', onEnd);
        }

        function onMove(e) {
            const clientX = e.type === 'touchmove' ? e.touches[0].clientX : e.clientX;
            moveX = clientX - startX;

            if (moveX < 0) moveX = 0;
            if (moveX > maxMove) moveX = maxMove;

            handle.style.left = moveX + 'px';
            piece.style.left = moveX + 'px';
            fill.style.width = (moveX + 20) + 'px'; // 加上 handle 的一半左右，视觉更自然

            // 记录轨迹，节流到每次移动都记（数据量小，一次拖动几十个点）
            trail.push([Math.round(moveX), Date.now() - trailStart]);
        }

        async function onEnd() {
            document.removeEventListener('mousemove', onMove);
            document.removeEventListener('mouseup', onEnd);
            document.removeEventListener('touchmove', onMove);
            document.removeEventListener('touchend', onEnd);

            if (isVerified) return;

            // 调用后端接口实时校验位置
            try {
                const res = await fetch('/api/auth/captcha/verify', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        id: data.id,
                        x: Math.round(moveX),
                        trail: trail
                    })
                });
                const result = await res.json();

                if (result.success) {
                    // 校验成功
                    isVerified = true;
                    captchaIdInput.value = data.id;
                    captchaXInput.value = Math.round(moveX);
                    if (captchaTrailInput) {
                        captchaTrailInput.value = JSON.stringify(trail);
                    }
                    
                    track.classList.add('slider-success');
                    // 验证成功后保持在当前滑动位置，不再铺满全长
                    handle.innerHTML = '✔';
                    handle.style.background = 'var(--primary)';
                    loginBtn.disabled = false;
                } else {
                    // 校验失败，重置位置
                    handle.style.transition = 'left 0.3s';
                    piece.style.transition = 'left 0.3s';
                    fill.style.transition = 'width 0.3s';
                    handle.style.left = '0';
                    piece.style.left = '0';
                    fill.style.width = '0';
                    
                    // 抖动一下轨道
                    track.classList.add('slider-error');
                    setTimeout(() => {
                        track.classList.remove('slider-error');
                        handle.style.transition = '';
                        piece.style.transition = '';
                        fill.style.transition = '';
                    }, 300);
                    
                    moveX = 0;
                    captchaIdInput.value = '';
                    captchaXInput.value = '';
                }
            } catch (e) {
                console.error('Verify failed:', e);
                // 网络错误重置
                handle.style.left = '0';
                piece.style.left = '0';
                fill.style.width = '0';
                moveX = 0;
            }
        }

        handle.addEventListener('mousedown', onDown);
        handle.addEventListener('touchstart', onDown, { passive: true });
    }

    loadCaptcha();
}

// 页面加载初始化
document.addEventListener('DOMContentLoaded', () => {
    initSliderCaptcha();
});
