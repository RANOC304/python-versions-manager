'use strict';

let allPythons = [];
let availVersions = [];
let customPaths = [];
let appInfo = null;
let bootstrapInfo = null;
let selectedVersion = null;
let pendingUninstall = null;
let showEolFilter = 'all';
let activeChip = 'all';
let jobPoll = null;

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function api(method, path, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const response = await fetch(path, opts);
  const data = await response.json().catch(() => ({ success: false, error: response.statusText }));
  if (!response.ok && data.success !== true) {
    throw new Error(data.error || response.statusText);
  }
  return data;
}

const GET = (path) => api('GET', path);
const POST = (path, body) => api('POST', path, body);
const DELETE = (path, body) => api('DELETE', path, body);

const SVG = {
  folder: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M1 4a1 1 0 0 1 1-1h4.6a1 1 0 0 1 .7.3L8.7 4.7A1 1 0 0 0 9.4 5H14a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H2a1 1 0 0 1-1-1V4z" stroke="currentColor" stroke-width="1.3"/></svg>',
  terminal: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="1" y="2" width="14" height="12" rx="2" stroke="currentColor" stroke-width="1.3"/><path d="M4 6l3 2.5L4 11M9 11h4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  trash: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 4h12M6 4V2h4v2M5 4v9a1 1 0 0 0 1 1h4a1 1 0 0 0 1-1V4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  copy: '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="5" y="5" width="8" height="9" rx="1.5" stroke="currentColor" stroke-width="1.3"/><path d="M3 11H2.5A1.5 1.5 0 0 1 1 9.5v-7A1.5 1.5 0 0 1 2.5 1h6A1.5 1.5 0 0 1 10 2.5V3" stroke="currentColor" stroke-width="1.3"/></svg>',
  path: '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M2 8h12M8 2v12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity=".35"/><circle cx="8" cy="8" r="2" fill="currentColor" opacity=".7"/></svg>',
  pip: '<svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true"><rect x="1" y="1" width="14" height="14" rx="3" stroke="currentColor" stroke-width="1.3"/><path d="M5 8h6M8 5v6" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  ok: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/><path d="M7 12.5l3.5 3.5L17 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  err: '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/><path d="M8 8l8 8M16 8l-8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
};

function sourceClass(item) {
  const type = String(item.type || '').toLowerCase();
  const source = String(item.source || '').toLowerCase();
  if (type.includes('uv') || source.includes('uv')) return 'uv';
  if (type.includes('pymanager') || source.includes('install manager')) return 'manager';
  if (type.includes('pyenv') || source.includes('pyenv')) return 'pyenv';
  if (type.includes('asdf') || source.includes('asdf')) return 'asdf';
  if (type.includes('conda') || source.includes('conda') || source.includes('anaconda') || source.includes('miniforge')) return 'conda';
  if (type.includes('scoop') || source.includes('scoop')) return 'scoop';
  if (type.includes('system') || source.includes('system')) return 'system';
  if (type.includes('virtual')) return 'env';
  if (type.includes('custom') || source.includes('custom')) return 'custom';
  if (source.includes('store') || type.includes('store')) return 'store';
  if (type === 'path' || source === 'path') return 'path';
  return 'python';
}

function cardStripeColor(item) {
  const colors = {
    python: 'var(--accent)',
    manager: 'var(--bluegreen)',
    uv: 'var(--green)',
    pyenv: 'var(--teal)',
    asdf: 'var(--lime)',
    conda: 'var(--orange)',
    scoop: 'var(--pink)',
    system: 'var(--steel)',
    env: 'var(--purple)',
    custom: 'var(--yellow)',
    store: 'var(--win-blue)',
    path: 'var(--violet)',
  };
  return colors[sourceClass(item)] || 'var(--border-2)';
}

function compareVersions(a, b) {
  const aa = String(a).split('.').map((part) => parseInt(part, 10) || 0);
  const bb = String(b).split('.').map((part) => parseInt(part, 10) || 0);
  const max = Math.max(aa.length, bb.length);
  for (let i = 0; i < max; i += 1) {
    const diff = (bb[i] || 0) - (aa[i] || 0);
    if (diff) return diff;
  }
  return 0;
}

function renderCard(item) {
  const parts = String(item.version || '?.?.?').split('.');
  const major = esc(parts.slice(0, 2).join('.'));
  const patch = parts.slice(2).length ? `.${esc(parts.slice(2).join('.'))}` : '';
  const cls = sourceClass(item);
  const canRemove = Boolean(item.can_remove);
  const removeTitle = canRemove ? `Remove with ${item.remove_mode}` : 'This interpreter is detected but not safe to remove automatically';
  const removeDisabled = canRemove ? '' : ' disabled';

  return `
    <article class="py-card ${item.is_current ? 'is-current' : ''}" style="--card-stripe:${cardStripeColor(item)}" data-id="${esc(item.id)}">
      <div class="card-top">
        <div class="version-display">
          <span class="version-major">${major}</span>
          <span class="version-patch">${patch}</span>
        </div>
        <div class="badge-group">
          <span class="badge badge-source-${cls}">${esc(item.source)}</span>
          <span class="badge badge-arch">${esc(item.architecture || 'unknown')}</span>
        </div>
      </div>

      <div class="detail-list">
        <div class="detail-row">
          <span class="detail-icon">${SVG.path}</span>
          <span class="detail-label">Folder</span>
          <span class="detail-text" title="${esc(item.path)}">${esc(item.path)}</span>
        </div>
        <div class="detail-row">
          <span class="detail-icon">${SVG.terminal}</span>
          <span class="detail-label">Exe</span>
          <span class="detail-text" title="${esc(item.executable)}">${esc(item.executable)}</span>
        </div>
      </div>

      <div class="card-meta">
        ${item.pip_version ? `<span class="meta-item">${SVG.pip} pip ${esc(item.pip_version)}</span>` : '<span class="meta-item muted">pip not detected</span>'}
        <span class="meta-item muted">${esc(item.remove_mode)}</span>
      </div>

      <div class="card-actions">
        <button class="action-btn" data-action="folder" data-path="${esc(item.path)}" title="Open folder">${SVG.folder} Explorer</button>
        <button class="action-btn" data-action="terminal" data-exe="${esc(item.executable)}" data-path="${esc(item.path)}" title="Open terminal">${SVG.terminal} Terminal</button>
        <button class="action-btn icon-only" data-action="copy" data-exe="${esc(item.executable)}" title="Copy executable path">${SVG.copy}</button>
        <div class="action-spacer"></div>
        <button class="action-btn danger" data-action="uninstall" data-id="${esc(item.id)}" title="${esc(removeTitle)}"${removeDisabled}>${SVG.trash} Remove</button>
      </div>
    </article>`;
}

function renderGrid(list) {
  const grid = document.getElementById('python-grid');
  if (!list.length) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">PY</div>
        <strong>No matching Python installations</strong>
        <p class="text-muted">Try rescanning or tracking a folder from Add Python.</p>
      </div>`;
    return;
  }
  grid.innerHTML = list.map(renderCard).join('');
}

function updateStats() {
  document.getElementById('stat-total').textContent = allPythons.length;
  document.getElementById('stat-sources').textContent = new Set(allPythons.map(sourceClass)).size;
  document.getElementById('stat-latest').textContent = allPythons[0]?.version || '-';

  const groups = { all: allPythons.length };
  for (const item of allPythons) {
    const group = sourceClass(item);
    groups[group] = (groups[group] || 0) + 1;
  }

  const labels = {
    all: 'All',
    python: 'Python.org',
    manager: 'Py Manager',
    uv: 'uv',
    pyenv: 'pyenv',
    asdf: 'asdf',
    conda: 'Conda',
    scoop: 'Scoop',
    system: 'System',
    env: 'Envs',
    custom: 'Custom',
    store: 'Store',
    path: 'PATH',
  };

  if (activeChip !== 'all' && !groups[activeChip]) activeChip = 'all';

  const chips = document.getElementById('filter-chips');
  chips.innerHTML = '';
  for (const [key, count] of Object.entries(groups)) {
    const button = document.createElement('button');
    button.className = `chip chip-${key}${activeChip === key ? ' active' : ''}`;
    button.dataset.chip = key;
    button.textContent = `${labels[key] || key} `;
    const strong = document.createElement('strong');
    strong.textContent = count;
    button.appendChild(strong);
    chips.appendChild(button);
  }
}

function applyFilters() {
  const query = document.getElementById('search-input').value.toLowerCase().trim();
  let list = allPythons;

  if (activeChip !== 'all') {
    list = list.filter((item) => sourceClass(item) === activeChip);
  }
  if (query) {
    list = list.filter((item) => [
      item.version,
      item.path,
      item.executable,
      item.source,
      item.type,
      item.architecture,
    ].some((value) => String(value || '').toLowerCase().includes(query)));
  }

  updateStats();
  renderGrid(list);
}

async function loadPythons() {
  const grid = document.getElementById('python-grid');
  grid.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <p>Scanning Python installations...</p>
    </div>`;
  document.getElementById('stat-total').textContent = '-';
  document.getElementById('stat-sources').textContent = '-';
  document.getElementById('stat-latest').textContent = '-';
  document.getElementById('filter-chips').innerHTML = '';

  try {
    const data = await GET('/api/pythons');
    if (!data.success) throw new Error(data.error || 'Scan failed');
    allPythons = data.pythons.sort((a, b) => compareVersions(a.version, b.version));
    applyFilters();
  } catch (error) {
    grid.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">!</div>
        <strong>Scan failed</strong>
        <p class="text-muted">${esc(error.message)}</p>
      </div>`;
  }
}

async function loadPlatformInfo() {
  try {
    const data = await GET('/api/platform');
    if (!data.success) throw new Error(data.error || 'Could not detect platform');
    appInfo = data;
    renderInstallMethods();
  } catch (error) {
    appInfo = { platform: 'unknown', install_methods: [] };
    renderInstallMethods();
    console.error(error);
  }
}

function setupActionText() {
  const uvOk = Boolean(bootstrapInfo?.uv?.installed);
  return uvOk ? 'Create app venv' : 'Install uv';
}

function renderBootstrapStatus() {
  const banner = document.getElementById('setup-banner');
  const title = document.getElementById('setup-title');
  const desc = document.getElementById('setup-desc');
  const mainButton = document.getElementById('install-uv-main');
  const modalButton = document.getElementById('install-uv-btn');
  const status = document.getElementById('tooling-status');

  if (!banner || !title || !desc || !mainButton || !modalButton || !status) return;

  const uvOk = Boolean(bootstrapInfo?.uv?.installed);
  const venvOk = Boolean(bootstrapInfo?.app_venv?.exists);
  const uvVersion = bootstrapInfo?.uv?.version || 'not installed';
  const uvPath = bootstrapInfo?.uv?.path || 'uv not found';
  const venvVersion = bootstrapInfo?.app_venv?.version || 'not created';
  const venvPath = bootstrapInfo?.app_venv?.python || '';

  if (uvOk && venvOk) {
    banner.classList.add('hidden');
  } else {
    banner.classList.remove('hidden');
    if (!uvOk) {
      title.textContent = 'uv is not installed';
      desc.textContent = 'Install uv, then PyManager will create or check the app venv.';
    } else {
      title.textContent = 'App venv is missing';
      desc.textContent = 'Create the local app virtual environment with uv.';
    }
  }

  const actionText = setupActionText();
  mainButton.innerHTML = `
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M8 2V11M8 11L4.5 7.5M8 11L11.5 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M3 13.5H13" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    </svg>
    ${esc(actionText)}`;
  modalButton.textContent = uvOk ? 'Create/check app venv' : 'Install uv and app venv';

  mainButton.disabled = uvOk && venvOk;
  modalButton.disabled = uvOk && venvOk;
  modalButton.classList.toggle('hidden', uvOk && venvOk);

  status.innerHTML = `
    <div class="tooling-row ${uvOk ? 'is-ok' : 'is-warn'}">
      <span class="tooling-dot"></span>
      <div class="tooling-text">
        <strong>uv</strong>
        <small title="${esc(uvPath)}">${esc(uvVersion)}${uvOk ? ` - ${esc(uvPath)}` : ''}</small>
      </div>
    </div>
    <div class="tooling-row ${venvOk ? 'is-ok' : 'is-warn'}">
      <span class="tooling-dot"></span>
      <div class="tooling-text">
        <strong>App venv</strong>
        <small title="${esc(venvPath)}">${venvOk ? `Python ${esc(venvVersion)} - ${esc(venvPath)}` : 'Not created yet'}</small>
      </div>
    </div>`;
}

async function loadBootstrapInfo() {
  try {
    const data = await GET('/api/bootstrap');
    if (!data.success) throw new Error(data.error || 'Could not detect tooling');
    bootstrapInfo = data;
  } catch (error) {
    bootstrapInfo = {
      platform: 'unknown',
      uv: { installed: false, path: null, version: null },
      app_venv: { exists: false, python: null, version: null },
    };
    console.error(error);
  }
  renderBootstrapStatus();
}

async function loadCustomPaths() {
  try {
    const data = await GET('/api/custom-paths');
    if (!data.success) throw new Error(data.error || 'Could not load paths');
    customPaths = data.paths || [];
    renderCustomPaths();
  } catch (error) {
    setCustomPathFeedback(error.message, true);
  }
}

function renderCustomPaths() {
  const list = document.getElementById('custom-path-list');
  if (!customPaths.length) {
    list.innerHTML = '';
    return;
  }
  list.innerHTML = customPaths.map((path) => `
    <div class="custom-path-item">
      <span title="${esc(path)}">${esc(path)}</span>
      <button class="icon-btn small" data-custom-remove="${esc(path)}" title="Stop tracking">
        <svg width="14" height="14" viewBox="0 0 18 18" fill="none" aria-hidden="true">
          <path d="M4 4L14 14M14 4L4 14" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/>
        </svg>
      </button>
    </div>`).join('');
}

function setCustomPathFeedback(message, isError = false) {
  const feedback = document.getElementById('custom-path-feedback');
  feedback.textContent = message || '';
  feedback.classList.toggle('error', Boolean(isError));
}

async function addCustomPath() {
  const input = document.getElementById('custom-path-input');
  const path = input.value.trim();
  if (!path) return;

  setCustomPathFeedback('Checking path...');
  try {
    const data = await POST('/api/custom-paths', { path });
    if (!data.success) throw new Error(data.error || 'Path was not added');
    input.value = '';
    customPaths = data.paths || [];
    renderCustomPaths();
    setCustomPathFeedback('Path tracked.');
    await loadPythons();
  } catch (error) {
    setCustomPathFeedback(error.message, true);
  }
}

async function removeCustomPath(path) {
  try {
    const data = await DELETE('/api/custom-paths', { path });
    if (!data.success) throw new Error(data.error || 'Path was not removed');
    customPaths = data.paths || [];
    renderCustomPaths();
    setCustomPathFeedback('Path removed.');
    await loadPythons();
  } catch (error) {
    setCustomPathFeedback(error.message, true);
  }
}

async function openInstallModal() {
  selectedVersion = null;
  const display = document.getElementById('selected-version-display');
  display.textContent = 'No version selected';
  display.classList.remove('has-version');
  document.getElementById('confirm-install').disabled = true;
  document.getElementById('install-overlay').classList.remove('hidden');
  setCustomPathFeedback('');
  await loadBootstrapInfo();
  if (!appInfo) await loadPlatformInfo();
  renderInstallMethods();
  loadCustomPaths();

  if (!availVersions.length) await fetchAvailableVersions();
  renderVersionList();
}

function renderInstallMethods() {
  const container = document.getElementById('install-method-list');
  if (!container) return;

  const methods = appInfo?.install_methods || [];
  if (!methods.length) {
    container.innerHTML = '<div class="loading-methods">No supported install manager was detected.</div>';
    document.getElementById('confirm-install').disabled = true;
    return;
  }

  const firstAvailable = methods.find((method) => method.available);
  container.innerHTML = methods.map((method) => {
    const checked = firstAvailable && method.id === firstAvailable.id ? ' checked' : '';
    const active = firstAvailable && method.id === firstAvailable.id ? ' active' : '';
    const disabled = method.available ? '' : ' disabled';
    const unavailable = method.available ? '' : ' unavailable';
    const note = method.available ? method.description : `${method.description} not found`;
    return `
      <label class="radio-item${active}${unavailable}">
        <input type="radio" name="method" value="${esc(method.id)}"${checked}${disabled}>
        <span class="radio-symbol">${esc(method.short || method.label.slice(0, 2).toUpperCase())}</span>
        <span class="radio-text">
          <strong>${esc(method.label)}</strong>
          <small>${esc(note)}</small>
        </span>
      </label>`;
  }).join('');

  container.querySelectorAll('input[type=radio]').forEach((input) => {
    input.addEventListener('change', syncMethodUi);
  });
  syncMethodUi();
}

function syncMethodUi() {
  document.querySelectorAll('#install-method-list .radio-item').forEach((item) => {
    item.classList.toggle('active', item.querySelector('input')?.checked);
  });

  const method = document.querySelector('input[name=method]:checked')?.value;
  const archGroup = document.getElementById('arch-group');
  if (archGroup) {
    archGroup.classList.toggle('hidden-field', method !== 'direct');
  }

  const hasMethod = Boolean(method);
  document.getElementById('confirm-install').disabled = !selectedVersion || !hasMethod;
}

function closeInstallModal() {
  document.getElementById('install-overlay').classList.add('hidden');
}

async function fetchAvailableVersions() {
  const list = document.getElementById('version-list');
  list.innerHTML = '<div class="loading-state small"><div class="spinner"></div><p>Loading versions...</p></div>';

  try {
    const data = await GET('/api/available-versions');
    if (!data.success) throw new Error(data.error || 'Could not load versions');
    availVersions = (data.versions || []).sort((a, b) => compareVersions(a.cycle, b.cycle));
  } catch (error) {
    availVersions = [];
    list.innerHTML = `<div class="loading-state small"><p class="error-text">${esc(error.message)}</p></div>`;
  }
}

function renderVersionList() {
  const versions = showEolFilter === 'supported'
    ? availVersions.filter((version) => version.eol === false)
    : availVersions;

  const list = document.getElementById('version-list');
  if (!versions.length) {
    list.innerHTML = '<div class="loading-state small"><p>No versions available.</p></div>';
    return;
  }

  const installedCycles = new Set(allPythons.map((item) => item.version.split('.').slice(0, 2).join('.')));
  list.innerHTML = '';

  for (const version of versions) {
    const row = document.createElement('button');
    row.className = `version-row${selectedVersion === version.latest ? ' selected' : ''}`;
    row.dataset.version = version.latest;
    row.type = 'button';

    const cycle = document.createElement('span');
    cycle.className = 'vr-cycle';
    cycle.textContent = version.cycle;
    row.appendChild(cycle);

    const latest = document.createElement('span');
    latest.className = 'vr-latest';
    latest.textContent = version.latest;
    row.appendChild(latest);

    if (installedCycles.has(version.cycle)) {
      const installed = document.createElement('span');
      installed.className = 'vr-badge vr-badge-installed';
      installed.textContent = 'Installed';
      row.appendChild(installed);
    }

    const badge = document.createElement('span');
    badge.className = `vr-badge ${version.eol ? 'vr-badge-eol' : 'vr-badge-supported'}`;
    badge.textContent = version.eol ? 'EOL' : 'Supported';
    row.appendChild(badge);

    row.addEventListener('click', () => selectVersion(version.latest));
    list.appendChild(row);
  }
}

function selectVersion(version) {
  selectedVersion = version;
  document.querySelectorAll('.version-row').forEach((row) => {
    row.classList.toggle('selected', row.dataset.version === version);
  });
  const display = document.getElementById('selected-version-display');
  display.textContent = `Python ${version} selected`;
  display.classList.add('has-version');
  syncMethodUi();
}

async function doInstall() {
  if (!selectedVersion) return;
  const method = document.querySelector('input[name=method]:checked')?.value;
  if (!method) {
    window.alert('No available install method was detected on this system.');
    return;
  }
  const arch = document.querySelector('input[name=arch]:checked')?.value || '64';
  closeInstallModal();

  try {
    const data = await POST('/api/pythons/install', { version: selectedVersion, method, arch });
    if (!data.success) throw new Error(data.error || 'Install did not start');
    openJobModal(`Installing Python ${selectedVersion}`, data.job_id, loadPythons);
  } catch (error) {
    window.alert(`Failed to start install: ${error.message}`);
  }
}

async function installUvAndVenv() {
  try {
    const data = await POST('/api/uv/install', {});
    if (!data.success) throw new Error(data.error || 'uv install did not start');
    openJobModal('Installing uv and app venv', data.job_id, async () => {
      await loadBootstrapInfo();
      await loadPlatformInfo();
      await loadPythons();
    });
  } catch (error) {
    window.alert(`Failed to start uv setup: ${error.message}`);
  }
}

function confirmUninstall(id) {
  pendingUninstall = allPythons.find((item) => item.id === id);
  if (!pendingUninstall || !pendingUninstall.can_remove) return;

  document.getElementById('confirm-version').textContent = `Python ${pendingUninstall.version}`;
  document.getElementById('confirm-mode').textContent = pendingUninstall.remove_mode;
  document.getElementById('confirm-path').textContent = pendingUninstall.path;
  document.getElementById('confirm-overlay').classList.remove('hidden');
}

function cancelUninstall() {
  pendingUninstall = null;
  document.getElementById('confirm-overlay').classList.add('hidden');
}

async function doUninstall() {
  if (!pendingUninstall) return;
  const item = pendingUninstall;
  pendingUninstall = null;
  document.getElementById('confirm-overlay').classList.add('hidden');

  try {
    const data = await POST('/api/pythons/uninstall', {
      executable: item.executable,
      path: item.path,
      version: item.version,
      source: item.source,
      type: item.type,
    });
    if (!data.success) throw new Error(data.error || 'Removal did not start');
    openJobModal(`Removing Python ${item.version}`, data.job_id, loadPythons);
  } catch (error) {
    window.alert(`Failed to start removal: ${error.message}`);
  }
}

function openJobModal(title, jobId, onComplete) {
  if (jobPoll) clearInterval(jobPoll);

  document.getElementById('job-title').textContent = title;
  document.getElementById('job-terminal').innerHTML = '';
  document.getElementById('close-job').disabled = true;

  const status = document.getElementById('job-status-icon');
  status.className = 'job-status-icon running';
  status.innerHTML = '<div class="spinner"></div>';
  document.getElementById('job-overlay').classList.remove('hidden');

  let lastLineCount = 0;
  jobPoll = setInterval(async () => {
    try {
      const data = await GET(`/api/jobs/${jobId}`);
      if (!data.success) return;
      const job = data.job;
      const terminal = document.getElementById('job-terminal');
      const newLines = job.lines.slice(lastLineCount);
      lastLineCount = job.lines.length;

      for (const line of newLines) {
        const span = document.createElement('span');
        span.textContent = `${line}\n`;
        if (line.startsWith('[ok]')) span.className = 't-success';
        else if (line.startsWith('[error]')) span.className = 't-error';
        else if (line.startsWith('>')) span.className = 't-info';
        terminal.appendChild(span);
      }
      terminal.scrollTop = terminal.scrollHeight;

      if (job.status === 'done') {
        clearInterval(jobPoll);
        jobPoll = null;
        document.getElementById('close-job').disabled = false;
        if (job.success) {
          status.className = 'job-status-icon done-ok';
          status.innerHTML = SVG.ok;
          if (onComplete) onComplete();
        } else {
          status.className = 'job-status-icon done-err';
          status.innerHTML = SVG.err;
        }
      }
    } catch (error) {
      console.error(error);
    }
  }, 500);
}

function closeJobModal() {
  document.getElementById('job-overlay').classList.add('hidden');
}

async function openFolder(path) {
  try {
    const data = await POST('/api/pythons/open-folder', { path });
    if (!data.success) throw new Error(data.error || 'Could not open folder');
  } catch (error) {
    window.alert(`Could not open folder: ${error.message}`);
  }
}

async function openTerminal(executable, path) {
  try {
    const data = await POST('/api/pythons/open-terminal', { executable, path });
    if (!data.success) throw new Error(data.error || 'Could not open terminal');
  } catch (error) {
    window.alert(`Could not open terminal: ${error.message}`);
  }
}

async function copyPath(executable) {
  try {
    await navigator.clipboard.writeText(executable);
  } catch {
    window.prompt('Executable path', executable);
  }
}

document.addEventListener('click', (event) => {
  const actionButton = event.target.closest('[data-action]');
  if (actionButton && !actionButton.disabled) {
    const { action, path, exe, id } = actionButton.dataset;
    if (action === 'folder') openFolder(path);
    if (action === 'terminal') openTerminal(exe, path);
    if (action === 'copy') copyPath(exe);
    if (action === 'uninstall') confirmUninstall(id);
  }

  const customRemove = event.target.closest('[data-custom-remove]');
  if (customRemove) removeCustomPath(customRemove.dataset.customRemove);
});

document.addEventListener('DOMContentLoaded', () => {
  loadBootstrapInfo();
  loadPlatformInfo();
  loadPythons();

  document.getElementById('scan-btn').addEventListener('click', loadPythons);
  document.getElementById('add-btn').addEventListener('click', openInstallModal);
  document.getElementById('install-uv-main').addEventListener('click', installUvAndVenv);
  document.getElementById('search-input').addEventListener('input', applyFilters);

  document.getElementById('close-install').addEventListener('click', closeInstallModal);
  document.getElementById('cancel-install').addEventListener('click', closeInstallModal);
  document.getElementById('confirm-install').addEventListener('click', doInstall);
  document.getElementById('install-uv-btn').addEventListener('click', installUvAndVenv);
  document.getElementById('add-custom-path').addEventListener('click', addCustomPath);
  document.getElementById('custom-path-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') addCustomPath();
  });

  document.getElementById('close-job').addEventListener('click', closeJobModal);
  document.getElementById('cancel-uninstall').addEventListener('click', cancelUninstall);
  document.getElementById('confirm-uninstall-btn').addEventListener('click', doUninstall);

  document.getElementById('filter-chips').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-chip]');
    if (!chip) return;
    activeChip = chip.dataset.chip;
    applyFilters();
  });

  document.querySelectorAll('#arch-group input[type=radio]').forEach((input) => {
    input.addEventListener('change', () => {
      const group = input.closest('.radio-group');
      group.querySelectorAll('.radio-item').forEach((item) => {
        item.classList.toggle('active', item.querySelector('input')?.checked);
      });
    });
  });

  document.querySelectorAll('.version-filter-btns .filter-btn').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.version-filter-btns .filter-btn').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      showEolFilter = button.dataset.eol;
      renderVersionList();
    });
  });

  document.getElementById('install-overlay').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeInstallModal();
  });
  document.getElementById('confirm-overlay').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) cancelUninstall();
  });
});
