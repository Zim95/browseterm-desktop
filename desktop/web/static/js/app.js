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

let podPollHandle = null;

function clusterStatusLabel(exists) {
    return exists ? 'Running' : 'Stopped';
}

function renderCluster(status) {
    document.getElementById('cpuSlider').max = status.total_cpu;
    document.getElementById('memorySlider').max = status.total_memory_gb;
    document.getElementById('storageSlider').max = status.total_storage_gb;

    if (!clusterSlidersTouched) {
        document.getElementById('cpuSlider').value = status.allocated_cpu;
        document.getElementById('memorySlider').value = status.allocated_memory_gb;
        document.getElementById('storageSlider').value = status.allocated_storage_gb;
    }
    document.getElementById('cpuValue').textContent = `${document.getElementById('cpuSlider').value} cores`;
    document.getElementById('memoryValue').textContent = `${document.getElementById('memorySlider').value} GB`;
    document.getElementById('storageValue').textContent = `${document.getElementById('storageSlider').value} GB`;

    const sliders = ['cpuSlider', 'memorySlider', 'storageSlider'];
    sliders.forEach((id) => { document.getElementById(id).disabled = status.cluster_exists; });

    const badge = document.getElementById('clusterStatusBadge');
    badge.className = `status-badge ${status.cluster_exists ? 'active' : 'not_registered'}`;
    badge.textContent = clusterStatusLabel(status.cluster_exists);

    const btn = document.getElementById('clusterBtn');
    if (!btn.dataset.busy) {
        btn.textContent = status.cluster_exists ? 'Teardown' : 'Setup';
        btn.disabled = false;
    }
    document.getElementById('openBrowserBtn').hidden = !status.cluster_exists;

    document.getElementById('clusterError').textContent = status.error || '';

    document.getElementById('podTableWrap').hidden = !status.cluster_exists;
    if (status.cluster_exists) {
        startPodPolling();
    } else {
        stopPodPolling();
    }
}

let clusterSlidersTouched = false;

async function loadClusterStatus() {
    const status = await window.pywebview.api.cluster_status();
    renderCluster(status);
}

async function toggleCluster() {
    const btn = document.getElementById('clusterBtn');
    const settingUp = btn.textContent === 'Setup';
    btn.dataset.busy = '1';
    btn.disabled = true;
    btn.textContent = settingUp ? 'Setting up...' : 'Tearing down...';

    if (settingUp) resetSetupSteps();

    const status = settingUp
        ? await window.pywebview.api.setup_cluster(
            Number(document.getElementById('cpuSlider').value),
            Number(document.getElementById('memorySlider').value),
            Number(document.getElementById('storageSlider').value),
        )
        : await window.pywebview.api.teardown_cluster();

    delete btn.dataset.busy;
    clusterSlidersTouched = false;
    renderCluster(status);
}

/**
 * Live Setup progress. Python pushes each step via evaluate_js (desktop/app.py's
 * _handle_setup_step) as window.onSetupStep(stepName, status, detail) - status one of
 * "started"/"succeeded"/"failed". Steps aren't known ahead of time (cluster_manager.py/
 * local_stack.py own the real list), so rows are created on first sight rather than hardcoded
 * here, in the order they actually arrive.
 */
const _setupStepOrder = [];
const _setupStepRows = {};

function resetSetupSteps() {
    _setupStepOrder.length = 0;
    for (const key of Object.keys(_setupStepRows)) delete _setupStepRows[key];
    const list = document.getElementById('setupSteps');
    list.innerHTML = '';
    list.hidden = false;
}

function _stepIcon(status) {
    if (status === 'succeeded') return '✓';
    if (status === 'failed') return '✕';
    if (status === 'started') return '●';
    return '○';
}

window.onSetupStep = function onSetupStep(stepName, status, detail) {
    const list = document.getElementById('setupSteps');
    let row = _setupStepRows[stepName];
    if (!row) {
        row = document.createElement('li');
        row.className = 'setup-step';
        row.innerHTML = '<span class="setup-step-icon"></span><span class="setup-step-name"></span><span class="setup-step-detail"></span>';
        _setupStepRows[stepName] = row;
        _setupStepOrder.push(stepName);
        list.appendChild(row);
    }
    row.className = `setup-step setup-step-${status}`;
    row.querySelector('.setup-step-icon').textContent = _stepIcon(status);
    row.querySelector('.setup-step-name').textContent = stepName;
    row.querySelector('.setup-step-detail').textContent = status === 'failed' ? (detail || '') : '';
};

function renderPods(pods) {
    const body = document.getElementById('podTableBody');
    body.innerHTML = '';
    pods.forEach((pod) => {
        const row = document.createElement('tr');
        // Only offer Restart on an actual failure signal (CrashLoopBackOff, ImagePullBackOff,
        // phase Failed, ...) -- desktop/cluster_manager.py's `crashing` field, not merely "not
        // Running yet", so the button doesn't flicker in and out during a completely normal
        // Setup run while pods are still Pending/ContainerCreating on their way up.
        const showRestart = pod.crashing;
        row.innerHTML = `
            <td>${pod.namespace}</td>
            <td>${pod.name}</td>
            <td>${pod.ready}</td>
            <td>${pod.phase}</td>
            <td>${pod.restarts}</td>
            <td>${showRestart ? '<button class="pod-restart-btn">Restart</button>' : ''}</td>
        `;
        if (showRestart) {
            row.querySelector('.pod-restart-btn').addEventListener('click', () => restartPod(pod.namespace, pod.name));
        }
        body.appendChild(row);
    });
    document.getElementById('podTableEmpty').hidden = pods.length > 0;
}

async function pollPods() {
    const data = await window.pywebview.api.list_cluster_pods();
    if (data.error) {
        document.getElementById('clusterError').textContent = data.error;
        return;
    }
    renderPods(data.pods);
}

async function restartPod(namespace, name) {
    const data = await window.pywebview.api.restart_workload_pod(namespace, name);
    document.getElementById('clusterError').textContent = data.error || '';
    renderPods(data.pods);
}

function startPodPolling() {
    if (podPollHandle) return;
    pollPods();
    podPollHandle = setInterval(pollPods, 5000);
}

function stopPodPolling() {
    if (!podPollHandle) return;
    clearInterval(podPollHandle);
    podPollHandle = null;
}

function initClusterSliders() {
    ['cpuSlider', 'memorySlider', 'storageSlider'].forEach((id) => {
        document.getElementById(id).addEventListener('input', (e) => {
            clusterSlidersTouched = true;
            const labelId = { cpuSlider: 'cpuValue', memorySlider: 'memoryValue', storageSlider: 'storageValue' }[id];
            const unit = id === 'cpuSlider' ? 'cores' : 'GB';
            document.getElementById(labelId).textContent = `${e.target.value} ${unit}`;
        });
    });
    document.getElementById('clusterBtn').addEventListener('click', toggleCluster);
    document.getElementById('openBrowserBtn').addEventListener('click', () => window.pywebview.api.open_browser());
}

function init() {
    document.getElementById('activateBtn').addEventListener('click', activateDevice);
    document.getElementById('logoutBtn').addEventListener('click', logout);
    initClusterSliders();
    loadDeviceInfo();
    loadClusterStatus();
}

if (window.pywebview) {
    init();
} else {
    window.addEventListener('pywebviewready', init);
}
