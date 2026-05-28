/* Grok gray-login page. This page never displays tokens or authorization code. */

const output = document.getElementById('output');

function writeOutput(value) {
    output.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function setText(id, text) {
    document.getElementById(id).textContent = text || '-';
}

function formatExpires(value) {
    if (!value) return '-';
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, {
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
    return data;
}

async function refreshStatus() {
    const status = await requestJson('/api/grok/status');
    setText('status-line', status.logged_in ? '已登录' : (status.needs_reauth ? '需要重新登录' : '未登录'));
    setText('provider', status.provider);
    setText('base-url', status.base_url);
    setText('email', status.email);
    setText('expires-at', formatExpires(status.expires_at));
    writeOutput(status);
}

async function startLogin() {
    const data = await requestJson('/api/grok/login/start', { method: 'POST', body: '{}' });
    const box = document.getElementById('login-box');
    const link = document.getElementById('authorize-url');
    box.classList.remove('hidden');
    link.href = data.authorize_url || '#';
    link.textContent = data.authorize_url || '';
    document.getElementById('login-message').textContent = data.message || '打开链接完成登录；如果浏览器无法访问本机 loopback，请复制最终 callback URL，或复制 Grok Build 页面显示的授权码到下方。';
    writeOutput({ status: data.status, redirect_uri: data.redirect_uri, manual_paste_supported: data.manual_paste_supported });
}

async function pollLogin() {
    const data = await requestJson('/api/grok/login/poll');
    writeOutput(data);
    if (data.status === 'complete') await refreshStatus();
}

async function manualLogin() {
    const callbackUrl = document.getElementById('callback-url').value.trim();
    if (!callbackUrl) {
        writeOutput('callback URL or authorization code required');
        return;
    }
    const data = await requestJson('/api/grok/login/manual', {
        method: 'POST',
        body: JSON.stringify({ callback_url: callbackUrl }),
    });
    document.getElementById('callback-url').value = '';
    writeOutput(data);
    await refreshStatus();
}

async function testCredentials() {
    const data = await requestJson('/api/grok/test', { method: 'POST', body: '{}' });
    writeOutput(data);
}

async function logout() {
    const data = await requestJson('/api/grok/logout', { method: 'POST', body: '{}' });
    writeOutput(data);
    await refreshStatus();
}

function bind(id, handler) {
    document.getElementById(id).addEventListener('click', async () => {
        try {
            await handler();
        } catch (error) {
            writeOutput(error.message || String(error));
        }
    });
}

bind('refresh-btn', refreshStatus);
bind('login-btn', startLogin);
bind('poll-btn', pollLogin);
bind('manual-btn', manualLogin);
bind('test-btn', testCredentials);
bind('logout-btn', logout);

refreshStatus().catch(error => writeOutput(error.message || String(error)));
