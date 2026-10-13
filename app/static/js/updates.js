(() => {
    const card = document.getElementById('systemUpdate');
    if (!card) return;
    const find = id => document.getElementById(id);
    const busyStates = new Set(['checking','downloading','backing_up','queued','preparing','applying','restarting','rolling_back']);
    let state = null;
    let timer = null;
    let sending = false;
    let retrying = false;
    let disconnectedAt = null;
    const initialVersion = {value: null};

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
        const busy = busyStates.has(data.state) || sending;
        find('updateProgress').hidden = !busy;
        find('updateProgress').value = data.progress || 0;
        find('updateCapability').textContent = data.capability.reason;
        find('updateCheck').disabled = busy;
        find('updateDownload').disabled = busy || !data.release?.has_update;
        find('updateApply').disabled = busy || data.state !== 'ready' || !data.capability.can_apply;
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
        retrying = busyStates.has(data.state);
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
            render(await response.json());
        } catch (error) {
            disconnectedAt ??= Date.now();
            if (retrying && Date.now()-disconnectedAt < 20 * 60 * 1000) {
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
        if (sending) return;
        sending = true;
        if (state) render(state);
        try {
            const response = await fetch('/api/system/update/' + action, {
                method:'POST', headers:{'Content-Type':'application/json','X-Update-Request':'1'},
                body:body ? JSON.stringify(body) : undefined, signal:AbortSignal.timeout(15000),
            });
            const data = await response.json();
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作未能提交');
            sending = false;
            render(data);
            retrying = true;
            timer = setTimeout(refresh, 1000);
        } catch (error) {
            sending = false;
            if (state) render(state);
            message(error.message || '操作未能提交，请检查状态后重试', true);
        }
    }
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
