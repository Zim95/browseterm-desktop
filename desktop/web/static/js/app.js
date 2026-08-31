/**
 * BrowseTerm Desktop app shell. Talks to Python only through `window.pywebview.api`
 * (desktop/api.py) -- no direct network calls from here.
 */
function formatBytes(bytes) {
    if (bytes === null || bytes === undefined) return '-';
    const gb = bytes / (1024 ** 3);
    return `${gb.toFixed(1)} GB`;
}

function statusLabel(status) {
    const labels = {
        active: 'Active',
        inactive: 'Inactive',
        revoked: 'Revoked',
        not_registered: 'Not Activated',
    };
    return labels[status] || 'Unknown';
}

function render(data) {
    const hw = data.hardware;
    document.getElementById('deviceName').textContent = hw.device_name;
    document.getElementById('deviceOs').textContent = hw.os;
    document.getElementById('deviceArch').textContent = hw.architecture;
    document.getElementById('deviceCpu').textContent = `${hw.total_cpu} cores`;
    document.getElementById('deviceMemory').textContent = formatBytes(hw.total_memory_bytes);
    document.getElementById('deviceStorage').textContent = formatBytes(hw.total_storage_bytes);

    const badge = document.getElementById('statusBadge');
    badge.className = `status-badge ${data.status}`;
    badge.textContent = statusLabel(data.status);

    const btn = document.getElementById('activateBtn');
    btn.disabled = data.status === 'active';
    btn.textContent = data.status === 'active' ? 'Active on this device' : 'Activate';

    const errorEl = document.getElementById('errorMessage');
    errorEl.textContent = data.error || '';
}

async function loadDeviceInfo() {
    const data = await window.pywebview.api.device_info();
    render(data);
}

async function activateDevice() {
    const btn = document.getElementById('activateBtn');
    btn.disabled = true;
    btn.textContent = 'Activating...';
    const data = await window.pywebview.api.activate_device();
    render(data);
}

function logout() {
    window.pywebview.api.logout();
}

function init() {
    document.getElementById('activateBtn').addEventListener('click', activateDevice);
    document.getElementById('logoutBtn').addEventListener('click', logout);
    loadDeviceInfo();
}

if (window.pywebview) {
    init();
} else {
    window.addEventListener('pywebviewready', init);
}
