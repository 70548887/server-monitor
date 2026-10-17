(() => {
    const card = document.getElementById('systemUpdate');
    if (!card) return;
    const find = id => document.getElementById(id);
    const busyStates = new Set(['checking','downloading','backing_up','queued','preparing','applying','restarting','rolling_back']);
    let state = null;
    let proxyDirty = false;
    let proxySaving = false;
    const proxyForm = find('updateProxyForm');

    function setProxyVisibility(visible) {
        find('updateProxyUrl').type = visible ? 'text' : 'password';
        const button = find('updateProxyVisibility');
        button.textContent = visible ? '隐藏' : '显示';
        button.setAttribute('aria-pressed', String(visible));
        button.setAttribute('aria-label', visible ? '隐藏代理地址' : '显示代理地址');
    }
    function proxyMessage(text, error = false) {
        if (!proxyForm) return;
        find('updateProxyMessage').textContent = text;
        find('updateProxyMessage').classList.toggle('is-error', error);
    }
    function renderProxy(data, busy) {
        if (!proxyForm) return;
        const proxy = data.proxy || {enabled:false, configured:false, display_url:'', has_auth:false};
        if (!proxyDirty) find('updateProxyEnabled').checked = proxy.enabled;
        find('updateProxyUrl').placeholder = proxy.configured
            ? '代理已保存；输入新地址可替换，留空保持不变'
            : 'http://用户名:密码@主机:端口';
        find('updateProxySummary').textContent = proxy.configured
            ? `${proxy.enabled ? '使用代理' : '已关闭，保留配置'}：${proxy.display_url}${proxy.has_auth ? '（已配置认证）' : ''}`
            : '未配置代理';
        for (const id of ['updateProxyEnabled', 'updateProxyUrl', 'updateProxySave', 'updateProxyVisibility']) {
            find(id).disabled = busy || proxySaving;
        }
        find('updateProxyClear').disabled = busy || proxySaving || !proxy.configured;
    }
    async function saveProxy(clear = false, enableForUpdate = false) {
        if (sending || proxySaving || (state && busyStates.has(state.state))) return false;
        const value = find('updateProxyUrl').value.trim();
        const payload = {enabled: clear ? false : enableForUpdate || find('updateProxyEnabled').checked};
        if (clear) payload.url = '';
        else if (value) payload.url = value;
        proxySaving = true;
        if (state) render(state);
        else renderProxy({}, false);
        proxyMessage('正在保存…');
        try {
            const response = await fetch('/api/system/update/proxy', {
                method:'POST', headers:{'Content-Type':'application/json','X-Update-Request':'1'},
                body:JSON.stringify(payload), signal:AbortSignal.timeout(15000),
            });
            const data = await response.json().catch(() => null);
            if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : '代理配置保存失败，请检查输入后重试');
            if (!data?.proxy) throw new Error('代理配置响应异常，请刷新后确认保存结果');
            proxyDirty = false;
            find('updateProxyUrl').value = '';
            setProxyVisibility(false);
            render(data);
            proxyMessage(clear ? '代理已清除，更新将使用直连。' : data.proxy.enabled ? '代理已保存并启用，可以检查更新。' : '代理已关闭；检查或下载时需先启用。');
            return true;
        } catch (error) {
            proxyMessage(error.name === 'TimeoutError' ? '保存超时，请稍后重试。' : error.message || '代理保存失败，请稍后重试。', true);
            return false;
        } finally {
            proxySaving = false;
            if (state) render(state);
            else renderProxy({}, false);
        }
    }
    let timer = null;
    let sending = false;
    let submissionUncertain = false;
    let retrying = false;
    let disconnectedAt = null;
    const initialVersion = {value: null};

    function pipelineRunning(data) {
        return data.workflow?.mode === 'upgrade' &&
            (busyStates.has(data.state) || ['available', 'ready'].includes(data.state));
    }
    function renderWorkflow(data) {
        const steps = [...document.querySelectorAll('#updateSteps [data-step]')];
        if (!steps.length) return;
        const mapping = {checking:0, available:1, downloading:1, ready:2, queued:2,
            preparing:2, backing_up:2, applying:2, restarting:3, rolling_back:3, complete:4};
        const index = mapping[data.state] ?? -1;
        const failed = data.state === 'error';
        const failedIndex = mapping[data.failed_state] ?? 0;
        for (let i = 0; i < steps.length; i++) {
            let status = 'pending';
            if (data.state === 'complete') status = 'done';
            else if (data.state === 'up_to_date') status = i === 0 ? 'done' : 'pending';
            else if (failed) status = i < failedIndex ? 'done' : i === failedIndex ? 'error' : 'pending';
            else if (index >= 0) status = i < index ? 'done' : i === index ? 'current' : 'pending';
            steps[i].dataset.state = status;
            if (status === 'current' || status === 'error') steps[i].setAttribute('aria-current', 'step');
            else steps[i].removeAttribute('aria-current');
        }
    }
    function renderSetup(data) {
        const setup = find('updateSetup');
        if (!setup) return;
        const capability = data.capability;
        const managerRequired = capability.code === 'manager_required' ||
            (!capability.code && !capability.can_apply && capability.reason.includes('scripts/run_server.py'));
        setup.hidden = !managerRequired;
        if (!managerRequired) return;
        find('updateSetupExplanation').textContent = data.state === 'ready'
            ? '升级包已校验成功，当前缺少升级管理器，因此应用更新暂不可用。完成下面的一次性启动设置后即可继续。'
            : '当前启动方式只支持检查和下载，在线应用需要升级管理器接管服务。完成下面的一次性设置后即可使用一键更新。';
        find('updateSetupDirectory').textContent = capability.setup?.working_directory || '保持宝塔中原项目目录';
        find('updateSetupCommand').textContent = capability.setup?.command || 'python scripts/run_server.py --host 0.0.0.0 --port 8080';
    }
    async function copySetupCommand() {
        const text = find('updateSetupCommand').textContent;
        try {
            if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
            else {
                const field = document.createElement('textarea');
                field.value = text;
                field.style.position = 'fixed';
                field.style.left = '-9999px';
                document.body.append(field);
                try {
                    field.select();
                    if (!document.execCommand('copy')) throw new Error('copy unavailable');
                } finally { field.remove(); }
            }
            find('updateSetupCopyMessage').textContent = '启动命令已复制，粘贴到宝塔项目的启动命令中。';
        } catch (_) {
            find('updateSetupCopyMessage').textContent = '自动复制不可用，请选中上方命令手动复制。';
        }
    }
    function message(text, error = false) {
        find('updateMessage').textContent = text;
        card.classList.toggle('update-error', error);
    }
    function render(data) {
        state = data;
        disconnectedAt = null;
        if (initialVersion.value === null) initialVersion.value = data.current_version;
        find('updateCurrent').textContent = 'v' + data.current_version;
        find('updateLatest').textContent = data.release ? 'v' + data.release.version : '尚未检查';
        message(data.message + (data.state === 'complete' && initialVersion.value !== data.current_version ? '，刷新页面后使用新版本。' : ''), data.state === 'error');
        const busy = busyStates.has(data.state) || pipelineRunning(data) || sending || submissionUncertain || proxySaving;
        renderWorkflow(data);
        renderSetup(data);
        renderProxy(data, busy);
        const enableFirst = data.proxy?.configured && !data.proxy.enabled;
        find('updateCheck').textContent = enableFirst ? '启用代理并检查' : '检查更新';
        find('updateDownload').textContent = enableFirst ? '启用代理并下载' : '下载更新';
        find('updateApply').textContent = enableFirst ? '启用代理并应用' : '应用更新';
        const primary = find('updateUpgrade');
        if (primary) {
            primary.textContent = enableFirst ? '启用代理并更新' : data.state === 'ready' ? '继续更新' : '一键更新';
            primary.disabled = busy || !data.capability.can_apply;
            primary.title = data.capability.can_apply ? '检查、下载校验、备份应用、重启验证连续完成' : data.capability.reason;
        }
        find('updateProgress').hidden = !busy;
        const progress = Math.max(0, Math.min(100, data.progress || 0));
        const workflowProgress = data.state === 'checking' ? Math.min(10, progress * .1)
            : data.state === 'available' ? 10
            : data.state === 'downloading' ? 10 + progress * .35
            : ['ready', 'queued', 'preparing', 'backing_up'].includes(data.state) ? 45
            : data.state === 'applying' ? 65 : data.state === 'restarting' ? 85 : progress;
        find('updateProgress').value = data.workflow?.mode === 'upgrade' ? workflowProgress : progress;
        find('updateCapability').textContent = data.capability.reason;
        find('updateCheck').disabled = busy;
        find('updateDownload').disabled = busy || !data.release?.has_update;
        find('updateApply').disabled = busy || data.state !== 'ready' || !data.capability.can_apply;
        find('updateApply').title = data.capability.can_apply
            ? data.state === 'ready' ? '应用已校验的更新并自动重启' : '请先下载并校验升级包'
            : data.capability.reason;
        find('updateRestart').disabled = busy || !data.capability.can_apply;
        find('updateBackup').disabled = busy;
        const release = find('updateRelease');
        release.hidden = !data.release;
        if (data.release) {
            find('updateNotes').textContent = data.release.notes || '此版本未填写更新说明。';
            find('updateReleaseLink').href = data.release.url;
        }
        const list = find('updateBackups');
        list.replaceChildren();
        for (const backup of data.backups || []) {
            const row = document.createElement('li');
            const label = document.createElement('span');
            label.textContent = `v${backup.version} · ${new Date(backup.created_at * 1000).toLocaleString()}`;
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-secondary';
            button.textContent = '回滚';
            button.disabled = busy || !data.capability.can_apply;
            button.addEventListener('click', () => {
                if (confirm(`回滚程序到 v${backup.version} 并重启？配置与数据库保持原样，当前程序会先备份。`)) act('rollback', {backup:backup.name});
            });
            row.append(label, button);
            list.append(row);
        }
        if (!list.children.length) {
            const empty = document.createElement('li');
            empty.textContent = '暂无程序备份';
            list.append(empty);
        }
        retrying = busyStates.has(data.state) || pipelineRunning(data);
    }
    async function refresh() {
        clearTimeout(timer);
        try {
            const response = await fetch('/api/system/update/status', {cache:'no-store', signal:AbortSignal.timeout(8000)});
            if (response.status === 401) {
                message('服务已响应，请重新登录后查看升级结果。', true);
                retrying = false;
                return;
            }
            if (!response.ok) throw new Error('状态读取失败');
            const data = await response.json();
            submissionUncertain = false;
            render(data);
        } catch (error) {
            disconnectedAt ??= Date.now();
            if (retrying && Date.now()-disconnectedAt < 90 * 60 * 1000) {
                message('服务正在切换或暂时无法连接，正在等待重新连接…');
            } else {
                message('暂时无法连接服务。请稍后刷新；后台升级任务不会因关闭此页而停止。', true);
                retrying = false;
            }
        } finally {
            if (retrying) timer = setTimeout(refresh, 2500);
        }
    }
    async function act(action, body) {
        if (sending || proxySaving) return;
        if (['check', 'download', 'apply', 'upgrade'].includes(action)) {
            if (proxyDirty) {
                proxyMessage('代理设置尚未保存，请先保存代理，再进行更新。', true);
                return;
            }
            if (state?.proxy?.configured && !state.proxy.enabled) {
                // 按钮明确提示“启用代理并…”，先保存启用状态，再提交网络操作。
                if (!await saveProxy(false, true)) return;
            }
        }
        clearTimeout(timer);
        sending = true;
        if (state) render(state);
        try {
            const response = await fetch('/api/system/update/' + action, {
                method:'POST', headers:{'Content-Type':'application/json','X-Update-Request':'1'},
                body:body ? JSON.stringify(body) : undefined, signal:AbortSignal.timeout(15000),
            });
            const data = await response.json();
            if (!response.ok) {
                const error = new Error(typeof data.detail === 'string' ? data.detail : '操作未能提交');
                error.submissionRejected = true;
                throw error;
            }
            sending = false;
            render(data);
            if (retrying) timer = setTimeout(refresh, 1000);
        } catch (error) {
            sending = false;
            submissionUncertain = !error.submissionRejected;
            if (state) render(state);
            if (submissionUncertain) {
                message('更新请求结果尚未确认，正在重新连接查看后台状态…');
                retrying = true;
                timer = setTimeout(refresh, 1000);
            } else {
                message(error.message || '操作未能提交，请检查状态后重试', true);
            }
        }
    }
    if (proxyForm) {
        find('updateProxyVisibility').addEventListener('click', () => {
            setProxyVisibility(find('updateProxyUrl').type === 'password');
        });
        proxyForm.addEventListener('submit', event => { event.preventDefault(); saveProxy(); });
        for (const id of ['updateProxyEnabled', 'updateProxyUrl']) {
            find(id).addEventListener('input', () => { proxyDirty = true; proxyMessage('修改尚未保存'); });
        }
        find('updateProxyClear').addEventListener('click', () => saveProxy(true));
    }
    find('updateUpgrade')?.addEventListener('click', () => {
        if (proxyDirty) {
            proxyMessage('代理设置尚未保存，请先保存代理，再进行更新。', true);
            return;
        }
        if (confirm('开始一键更新？将连续检查、下载校验、备份并应用更新，随后自动重启；期间监控会短暂停止，失败会停止或恢复。')) act('upgrade');
    });
    find('updateSetupCopy')?.addEventListener('click', copySetupCommand);
    find('updateCheck').addEventListener('click', () => act('check'));
    find('updateDownload').addEventListener('click', () => act('download'));
    find('updateBackup').addEventListener('click', () => act('backup'));
    find('updateApply').addEventListener('click', () => {
        if (confirm('应用已校验的更新并重启？期间监控会短暂停止，配置和数据保留，程序与依赖会自动备份。')) act('apply');
    });
    find('updateRestart').addEventListener('click', () => {
        if (confirm('现在重启监控服务？期间会短暂停止检测。')) act('restart');
    });
    window.addEventListener('pagehide', () => clearTimeout(timer));
    refresh();
})();
