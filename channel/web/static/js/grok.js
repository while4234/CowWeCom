/* Grok gray-login page. This page never displays tokens or authorization code. */

const output = document.getElementById('output');
let loginPollTimer = null;
let loginPollAttempts = 0;
const LOGIN_POLL_INTERVAL_MS = 1500;
const LOGIN_POLL_MAX_ATTEMPTS = 120;

function writeOutput(value) {
    const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    output.textContent = redactSecrets(text);
}

function redactSecrets(value) {
    return String(value || '')
        .replace(/(Authorization\s*:\s*Bearer\s+)[^\s"',}]+/ig, '$1***')
        .replace(/(\bBearer\s+)[^\s"',}]+/ig, '$1***')
        .replace(/((?:access_token|refresh_token|authorization_code|code_verifier|id_token|api_key|code)=)[^&\s"',}]+/ig, '$1***')
        .replace(/("?(?:access_token|refresh_token|authorization_code|code_verifier|id_token|api_key|code)"?\s*:\s*")[^"]+"/ig, '$1***"');
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
    if (data && data.status === 'error') throw new Error(data.message || 'Grok request failed');
    return data;
}

async function refreshStatus() {
    const status = await requestJson('/api/grok/status');
    const loggedIn = Boolean(status.logged_in);
    setText('status-line', loggedIn ? '已登录' : (status.needs_reauth ? '需要重新登录' : '未登录'));
    setText('provider', status.provider);
    setText('base-url', status.base_url);
    setText('email', loggedIn ? (status.email || 'xAI 未返回') : '-');
    setText('expires-at', formatExpires(status.expires_at));
    setLoginInputsVisible(!loggedIn);
    writeOutput(status);
}

function stopLoginPolling() {
    if (loginPollTimer) {
        window.clearInterval(loginPollTimer);
        loginPollTimer = null;
    }
    loginPollAttempts = 0;
}

async function startLogin() {
    const box = document.getElementById('login-box');
    const link = document.getElementById('authorize-url');
    const messageElement = document.getElementById('login-message');
    box.classList.remove('hidden');
    link.removeAttribute('href');
    link.textContent = '';
    messageElement.textContent = '正在请求 Grok 登录链接...';
    try {
        const data = await requestJson('/api/grok/login/start', { method: 'POST', body: '{}' });
        if (!data.authorize_url) throw new Error(data.message || 'Grok login did not return an authorization link.');
        link.href = data.authorize_url;
        link.textContent = data.authorize_url;
        messageElement.textContent = data.message || '打开链接完成登录；如果浏览器无法访问本机 loopback，请复制最终 callback URL，或复制包含 code 和 state 的查询字符串到下方。';
        writeOutput({
            status: data.status,
            redirect_uri: data.redirect_uri,
            manual_paste_supported: data.manual_paste_supported,
        });
        startLoginPolling();
    } catch (error) {
        const message = error.message || String(error);
        messageElement.textContent = message;
        writeOutput(message);
    }
}

function startLoginPolling() {
    stopLoginPolling();
    loginPollTimer = window.setInterval(async () => {
        loginPollAttempts += 1;
        try {
            const data = await pollLogin({ silent: true });
            if (data.status === 'complete' || data.status === 'failed' || loginPollAttempts >= LOGIN_POLL_MAX_ATTEMPTS) {
                stopLoginPolling();
                if (loginPollAttempts >= LOGIN_POLL_MAX_ATTEMPTS && data.status !== 'complete') {
                    writeOutput('Grok login is still pending. Click poll or paste the full callback URL/query string if needed.');
                }
            }
        } catch (error) {
            stopLoginPolling();
            writeOutput(error.message || String(error));
        }
    }, LOGIN_POLL_INTERVAL_MS);
}

async function pollLogin(options = {}) {
    const data = await requestJson('/api/grok/login/poll');
    if (!options.silent || data.status !== 'pending') writeOutput(data);
    if (data.status === 'complete') {
        stopLoginPolling();
        await refreshStatus();
    }
    if (data.status === 'failed') stopLoginPolling();
    return data;
}

async function manualLogin() {
    const callbackUrl = document.getElementById('callback-url').value.trim();
    if (!callbackUrl) {
        writeOutput('callback URL or query string with code and state required');
        return;
    }
    const data = await requestJson('/api/grok/login/manual', {
        method: 'POST',
        body: JSON.stringify({ callback_url: callbackUrl }),
    });
    document.getElementById('callback-url').value = '';
    writeOutput(data);
    stopLoginPolling();
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

function setLoginInputsVisible(visible) {
    document.querySelectorAll('.login-only').forEach(element => {
        element.classList.toggle('hidden', !visible);
    });
    if (visible) return;
    stopLoginPolling();
    document.getElementById('login-box').classList.add('hidden');
    document.getElementById('authorize-url').textContent = '';
    document.getElementById('authorize-url').removeAttribute('href');
    document.getElementById('login-message').textContent = '';
    document.getElementById('callback-url').value = '';
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
