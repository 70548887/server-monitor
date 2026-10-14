(() => {
    const tabs = [...document.querySelectorAll('[data-settings-tab]')];
    const allowed = new Set(tabs.map(tab => tab.dataset.settingsTab));
    function activate(name, focus = false, writeHistory = false) {
        if (!allowed.has(name)) name = 'account';
        for (const tab of tabs) {
            const selected = tab.dataset.settingsTab === name;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            document.getElementById(tab.getAttribute('aria-controls')).hidden = !selected;
            if (selected && focus) tab.focus();
        }
        if (writeHistory) {
            const url = new URL(window.location.href);
            url.searchParams.set('tab', name);
            url.searchParams.delete('success');
            url.searchParams.delete('error');
            window.history.pushState(null, '', url);
        }
    }
    for (const tab of tabs) {
        tab.addEventListener('click', () => activate(tab.dataset.settingsTab, false, true));
        tab.addEventListener('keydown', event => {
            const index = tabs.indexOf(tab);
            let next;
            if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
            else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = tabs.length - 1;
            else return;
            event.preventDefault();
            activate(tabs[next].dataset.settingsTab, true, true);
        });
    }
    window.addEventListener('popstate', () => activate(new URL(window.location.href).searchParams.get('tab')));
    if (tabs.length) activate(new URL(window.location.href).searchParams.get('tab'));

    const form = document.querySelector('form[action="/settings/password"]');
    if (!form) return;
    const feedback = document.getElementById('passwordFeedback');
    form.addEventListener('input', () => { feedback.hidden = true; });
    form.addEventListener('submit', event => {
        const password = document.getElementById('new_password');
        const confirmation = document.getElementById('confirm_password');
        const message = password.value.length < 6 ? '新密码长度不能少于 6 位'
            : password.value !== confirmation.value ? '两次输入的新密码不一致' : '';
        if (message) {
            event.preventDefault();
            feedback.textContent = message;
            feedback.hidden = false;
            confirmation.focus();
        }
    });
})();
