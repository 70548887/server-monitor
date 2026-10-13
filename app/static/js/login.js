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

// 点击登录才请求挑战；验证通过后提交原表单，密码不会发到验证码接口。
function initSliderCaptcha() {
    const form = document.getElementById('loginForm');
    const dialog = document.getElementById('captchaDialog');
    const container = document.getElementById('captcha-container');
    const loginBtn = document.getElementById('loginBtn');
    const closeBtn = document.getElementById('captchaClose');
    const refreshBtn = document.getElementById('captchaRefresh');
    const status = document.getElementById('captchaStatus');
    const idInput = document.getElementById('captcha_id');
    const xInput = document.getElementById('captcha_x');
    const trailInput = document.getElementById('captcha_trail');
    if (!form || !dialog || !container) return;

    const arrow = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d="m6 8 4 4-4 4m7-8 4 4-4 4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    let generation = 0;
    let request = null;
    let timer = null;
    let phase = 'closed';
    let verified = false;
    let submitting = false;
    let cancelDrag = null;

    function clearProof() {
        verified = false;
        idInput.value = '';
        xInput.value = '';
        trailInput.value = '';
    }

    function setStatus(message, state = '') {
        status.textContent = message;
        dialog.dataset.state = state;
    }

    function invalidate() {
        generation++;
        request?.abort();
        request = null;
        clearTimeout(timer);
        timer = null;
        if (cancelDrag) cancelDrag();
        cancelDrag = null;
        clearProof();
    }

    function reset() {
        invalidate();
        phase = 'closed';
        submitting = false;
        container.replaceChildren();
        container.setAttribute('aria-busy', 'false');
        loginBtn.disabled = false;
        loginBtn.textContent = '登 录';
        document.body.classList.remove('captcha-open');
    }

    function closeDialog() {
        if (submitting) return;
        reset();
        dialog.close();
        loginBtn.focus();
    }

    function fail(message, ticket) {
        if (ticket !== generation || !dialog.open) return;
        if (phase === 'loading') {
            container.innerHTML = '<div class="captcha-loading"><span aria-hidden="true">↻</span><span>暂时未能加载拼图</span></div>';
        }
        phase = 'error';
        clearProof();
        container.setAttribute('aria-busy', 'false');
        refreshBtn.disabled = false;
        container.querySelector('#sliderHandle')?.setAttribute('disabled', '');
        container.querySelector('.slider-track')?.classList.add('slider-error');
        setStatus(message, 'error');
        // 保留失败反馈，由用户换图重试，不自动闪烁或不断请求验证码。
        refreshBtn.focus();
    }

    async function loadCaptcha() {
        invalidate();
        phase = 'loading';
        const ticket = generation;
        const controller = new AbortController();
        request = controller;
        refreshBtn.disabled = true;
        container.setAttribute('aria-busy', 'true');
        container.innerHTML = '<div class="captcha-loading"><span class="captcha-spinner" aria-hidden="true"></span><span>正在加载拼图</span></div>';
        setStatus('正在加载拼图…');
        timer = setTimeout(() => {
            controller.abort();
            fail('加载超时，请点击换一张重试', ticket);
        }, 10000);
        try {
            const response = await fetch('/api/auth/captcha', {signal: controller.signal, cache: 'no-store'});
            if (!response.ok) throw new Error('load failed');
            const data = await response.json();
            const background = new Image();
            const piece = new Image();
            background.src = data.bg;
            piece.src = data.slider;
            await Promise.all([background.decode(), piece.decode()]);
            if (ticket !== generation || !dialog.open || controller.signal.aborted) return;
            clearTimeout(timer);
            renderSlider(data, background, piece, ticket);
        } catch (error) {
            if (ticket !== generation || !dialog.open || controller.signal.aborted) return;
            clearTimeout(timer);
            fail('拼图加载失败，请点击换一张重试', ticket);
        }
    }

    function renderSlider(data, background, piece, ticket) {
        const width = background.naturalWidth;
        const height = background.naturalHeight;
        const pieceWidth = piece.naturalWidth;
        container.innerHTML = `<div class="slider-captcha">
            <div class="slider-bg-container" id="sliderScene"></div>
            <div class="slider-track" id="sliderTrack">
                <div class="slider-fill" id="sliderFill"></div>
                <span class="slider-text" id="sliderText">按住滑块，向右拖动</span>
                <button type="button" class="slider-handle" id="sliderHandle" aria-label="向右拖动拼图滑块">${arrow}</button>
            </div>
        </div>`;
        const scene = container.querySelector('#sliderScene');
        const track = container.querySelector('#sliderTrack');
        const handle = container.querySelector('#sliderHandle');
        const fill = container.querySelector('#sliderFill');
        const hint = container.querySelector('#sliderText');
        scene.style.aspectRatio = `${width} / ${height}`;
        background.className = 'slider-bg-img';
        background.alt = '将拼图移到图片中的缺口';
        background.draggable = false;
        piece.className = 'slider-piece-img';
        piece.id = 'sliderPiece';
        piece.alt = '';
        piece.draggable = false;
        piece.style.width = `${pieceWidth / width * 100}%`;
        piece.style.top = `${data.y / height * 100}%`;
        scene.append(background, piece);
        container.setAttribute('aria-busy', 'false');
        phase = 'ready';
        refreshBtn.disabled = false;
        setStatus('验证通过后自动登录');
        timer = setTimeout(() => fail('拼图已过期，请换一张后重试', ticket), 115000);

        let pointerId = null;
        let startX = 0;
        let startedAt = 0;
        let position = 0;
        let travel = 0;
        let trackWidth = 0;
        let trail = [];

        function releasePointer() {
            if (pointerId !== null && handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
            pointerId = null;
        }
        cancelDrag = releasePointer;

        function move(clientX) {
            const distance = Math.max(0, Math.min(travel, clientX - startX));
            position = Math.round(distance / travel * (width - pieceWidth));
            handle.style.transform = `translateX(${distance}px)`;
            // 屏幕缩放/窄屏都按原图坐标提交，与实际拼图位置保持一致。
            piece.style.left = `${position / width * 100}%`;
            fill.style.width = `${distance + handle.offsetWidth}px`;
            const point = [position, Math.round(performance.now() - startedAt)];
            if (point[1] > trail[trail.length - 1][1]) {
                if (trail.length >= 1900) trail = trail.filter((_, index) => index % 2 === 0);
                trail.push(point);
            }
        }

        handle.addEventListener('pointerdown', event => {
            if (phase !== 'ready' || !event.isPrimary || event.button !== 0) return;
            event.preventDefault();
            phase = 'dragging';
            pointerId = event.pointerId;
            startX = event.clientX;
            startedAt = performance.now();
            position = 0;
            trail = [[0, 0]];
            trackWidth = track.clientWidth;
            travel = trackWidth - handle.offsetWidth;
            if (travel <= 0) { fail('显示尺寸已变化，请换一张重试', ticket); return; }
            track.classList.add('is-dragging');
            refreshBtn.disabled = true;
            hint.textContent = '对齐缺口后松开';
            handle.setPointerCapture(pointerId);
        });

        handle.addEventListener('pointermove', event => {
            if (phase !== 'dragging' || event.pointerId !== pointerId) return;
            move(event.clientX);
        });

        async function verify() {
            phase = 'verifying';
            clearTimeout(timer);
            handle.disabled = true;
            track.classList.remove('is-dragging');
            hint.textContent = '正在验证…';
            setStatus('正在验证，请稍候');
            const controller = new AbortController();
            request = controller;
            timer = setTimeout(() => {
                controller.abort();
                fail('验证超时，请换一张重试', ticket);
            }, 10000);
            try {
                const response = await fetch('/api/auth/captcha/verify', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id: data.id, x: position, trail}), signal: controller.signal,
                });
                if (!response.ok) throw new Error('verify failed');
                const result = await response.json();
                if (ticket !== generation || !dialog.open || controller.signal.aborted) return;
                clearTimeout(timer);
                if (!result.success) {
                    hint.textContent = '请换一张后重试';
                    fail(result.message || '没有对齐，请换一张重试', ticket);
                    return;
                }
                phase = 'verified';
                verified = true;
                idInput.value = data.id;
                xInput.value = String(position);
                trailInput.value = JSON.stringify(trail);
                track.classList.add('slider-success');
                handle.textContent = '✓';
                hint.textContent = '验证通过';
                setStatus('验证通过，正在登录…', 'success');
                timer = setTimeout(() => {
                    if (ticket !== generation || !dialog.open) return;
                    if (!form.checkValidity()) { closeDialog(); form.reportValidity(); return; }
                    form.requestSubmit();
                }, 350);
            } catch (error) {
                if (ticket !== generation || !dialog.open || controller.signal.aborted) return;
                clearTimeout(timer);
                hint.textContent = '请换一张后重试';
                fail('网络连接失败，请换一张重试', ticket);
            }
        }

        handle.addEventListener('pointerup', event => {
            if (phase !== 'dragging' || event.pointerId !== pointerId) return;
            if (Math.abs(track.clientWidth - trackWidth) > 1) {
                releasePointer();
                fail('窗口尺寸已变化，请换一张重试', ticket);
                return;
            }
            move(event.clientX);
            releasePointer();
            verify();
        });
        handle.addEventListener('pointercancel', () => {
            if (phase !== 'dragging') return;
            releasePointer();
            fail('拖动已取消，请换一张重试', ticket);
        });
    }

    form.addEventListener('submit', event => {
        if (submitting) { event.preventDefault(); return; }
        if (verified) {
            submitting = true;
            loginBtn.disabled = true;
            loginBtn.textContent = '正在登录…';
            refreshBtn.disabled = true;
            closeBtn.disabled = true;
            return;
        }
        event.preventDefault();
        if (!form.reportValidity() || dialog.open) return;
        dialog.showModal();
        document.body.classList.add('captcha-open');
        loadCaptcha();
    });
    refreshBtn.addEventListener('click', loadCaptcha);
    closeBtn.addEventListener('click', closeDialog);
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
    dialog.addEventListener('close', () => { if (!submitting) reset(); });
    window.addEventListener('pageshow', event => {
        if (event.persisted) { submitting = false; reset(); closeBtn.disabled = false; dialog.close(); }
    });
}

document.addEventListener('DOMContentLoaded', initSliderCaptcha);
