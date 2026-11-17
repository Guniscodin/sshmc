// Toast Notification System
function renderToast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        container.setAttribute('aria-live', 'polite');
        document.body.appendChild(container);
    }

    // Limit to max 3 visible toasts (user requirement: max 3 at once)
    while (container.children.length >= 3) {
        container.removeChild(container.firstChild);
    }

    const toast = document.createElement('div');
    toast.className = `toast-item toast-${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

    const iconClass = {
        success: 'fa-solid fa-circle-check',
        error: 'fa-solid fa-circle-exclamation',
        warning: 'fa-solid fa-triangle-exclamation',
        info: 'fa-solid fa-circle-info'
    }[type] || 'fa-solid fa-circle-info';

    const icon = document.createElement('i');
    icon.className = `toast-icon ${iconClass}`;

    const text = document.createElement('div');
    text.className = 'toast-message';
    text.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'toast-close btn-ghost btn-icon';
    closeBtn.setAttribute('aria-label', 'Dismiss notification');
    closeBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';

    toast.appendChild(icon);
    toast.appendChild(text);
    toast.appendChild(closeBtn);

    let timer = null;
    const removeToast = () => {
        if (timer) clearTimeout(timer);
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(24px)';
        setTimeout(() => {
            if (toast.parentElement) {
                toast.parentElement.removeChild(toast);
            }
        }, 150);
    };

    closeBtn.addEventListener('click', removeToast);

    // Auto-dismiss after 3 seconds for all notifications
    const progress = document.createElement('div');
    progress.className = 'toast-progress';
    progress.style.animationDuration = '3000ms';
    toast.appendChild(progress);

    timer = setTimeout(removeToast, 3000);
    toast.addEventListener('mouseenter', () => {
        progress.style.animationPlayState = 'paused';
        clearTimeout(timer);
    });
    toast.addEventListener('mouseleave', () => {
        progress.style.animationPlayState = 'running';
        timer = setTimeout(removeToast, 1200);
    });

    container.appendChild(toast);
}

// Bind to event bus
if (window.sshmc && typeof window.sshmc.on === 'function') {
    window.sshmc.on('toast', (e) => {
        renderToast(e.detail?.message || '', e.detail?.type || 'info');
    });
} else {
    window.addEventListener('sshmc:toast', (e) => {
        renderToast(e.detail?.message || '', e.detail?.type || 'info');
    });
}

async function responseMessage(response, fallback) {
    try {
        const data = typeof response.json === 'function'
            ? await response.json()
            : JSON.parse(response.responseText || '{}');
        if (typeof data.detail === 'string') return data.detail;
        if (data.detail && typeof data.detail.message === 'string') return data.detail.message;
        if (data.detail) return JSON.stringify(data.detail);
    } catch (_) {}
    return fallback;
}

/**
 * Preflight Failure Card Banners
 * Renders dismissible preflight warning cards inside profile cards based on state stored in sshmc.state.preflightFailures.
 */
function renderPreflightCardBanners() {
    const failures = (window.sshmc && window.sshmc.state && window.sshmc.state.preflightFailures) || {};
    for (const [profileId, detail] of Object.entries(failures)) {
        const card = document.querySelector(`.profile-card[data-profile-id="${profileId}"]`);
        if (!card) continue;
        if (card.querySelector('.source-preflight-alert')) continue;

        const alert = document.createElement('section');
        alert.className = 'source-preflight-alert';
        alert.setAttribute('role', 'alert');
        alert.setAttribute('aria-live', 'assertive');

        const heading = document.createElement('h4');
        heading.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Startup stopped: mod sources need attention';
        alert.append(heading);

        const summary = document.createElement('p');
        summary.textContent = detail.message || 'Resolve these source problems, then start again.';
        alert.append(summary);

        const groups = detail.issues_by_jar || {};
        for (const [jar, issues] of Object.entries(groups)) {
            const group = document.createElement('div');
            group.className = 'source-preflight-group';
            const label = document.createElement('strong');
            label.textContent = jar;
            group.append(label);
            const list = document.createElement('ul');
            for (const issue of issues || []) {
                const row = document.createElement('li');
                row.textContent = issue;
                list.append(row);
            }
            group.append(list);
            alert.append(group);
        }
        if (!Object.keys(groups).length) {
            for (const conflict of detail.conflicts || []) {
                const row = document.createElement('p');
                row.textContent = conflict;
                alert.append(row);
            }
        }
        for (const warning of detail.warnings || []) {
            const row = document.createElement('p');
            row.className = 'source-preflight-warning';
            row.textContent = `Warning: ${warning}`;
            alert.append(row);
        }

        const btnRow = document.createElement('div');
        btnRow.className = 'banner-actions';

        const openModalBtn = document.createElement('button');
        openModalBtn.type = 'button';
        openModalBtn.className = 'btn btn-secondary btn-sm';
        openModalBtn.textContent = 'View details';
        openModalBtn.addEventListener('click', () => {
            window.sshmc.emit('open-preflight-modal', detail);
        });
        btnRow.append(openModalBtn);

        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'btn btn-ghost btn-sm';
        dismiss.textContent = 'Dismiss';
        dismiss.addEventListener('click', () => {
            window.sshmc.emit('preflight-cleared', { profile_id: profileId });
            alert.remove();
        });
        btnRow.append(dismiss);

        alert.append(btnRow);
        card.querySelector('.profile-actions')?.before(alert);
    }
}

// Listen to preflight failure events to render banners and open modal
if (window.sshmc && typeof window.sshmc.on === 'function') {
    window.sshmc.on('preflight-failure', (e) => {
        window.sshmc.emit('open-preflight-modal', e.detail);
        requestAnimationFrame(renderPreflightCardBanners);
    });
    window.sshmc.on('preflight-cleared', (e) => {
        if (e.detail?.profile_id) {
            const card = document.querySelector(`.profile-card[data-profile-id="${e.detail.profile_id}"]`);
            card?.querySelector('.source-preflight-alert')?.remove();
        }
    });
}

function handleHtmxFailure(detail) {
    const xhr = detail.xhr;
    if (!xhr) return;
    const path = (detail.pathInfo && detail.pathInfo.requestPath)
        || (detail.requestConfig && detail.requestConfig.path)
        || (xhr && xhr.responseURL)
        || (detail.elt && (detail.elt.getAttribute('hx-post') || detail.elt.getAttribute('hx-get') || detail.elt.getAttribute('action')))
        || '';
    const eltProfileId = detail.elt?.closest?.('[data-profile-id]')?.dataset?.profileId;

    if (xhr.status === 401) {
        window.location.assign('/login');
        return;
    }

    if (xhr.status === 409) {
        try {
            const payload = JSON.parse(xhr.responseText || '{}');
            if (payload.detail && payload.detail.code === 'mod_download_failed') {
                window.sshmc.emit('mod-download-failure', { detail: payload.detail, path });
                htmx.trigger('#profile-list', 'refresh');
                htmx.trigger('#stats-bar', 'refresh');
                return;
            }
            if (payload.detail && payload.detail.code === 'source_preflight_failed') {
                const failureData = { ...payload.detail };
                if (!failureData.profile_id && eltProfileId) {
                    failureData.profile_id = eltProfileId;
                }
                window.sshmc.emit('preflight-failure', failureData);
                window.sshmc.emit('toast', { message: failureData.message || 'Startup stopped: mod sources need attention.', type: 'error' });
                htmx.trigger('#profile-list', 'refresh');
                htmx.trigger('#stats-bar', 'refresh');
                return;
            }
        } catch (_) {}
    }

    responseMessage(xhr, `Request failed (${xhr.status}).`).then(msg => {
        window.sshmc.emit('toast', { message: msg, type: 'error' });
    });
}

document.body.addEventListener('htmx:responseError', (event) => {
    event.detail._handled = true;
    handleHtmxFailure(event.detail);
});

document.body.addEventListener('htmx:afterRequest', async (event) => {
    const detail = event.detail;
    const path = (detail.pathInfo && detail.pathInfo.requestPath)
        || (detail.requestConfig && detail.requestConfig.path)
        || (detail.xhr && detail.xhr.responseURL)
        || (detail.elt && (detail.elt.getAttribute('hx-post') || detail.elt.getAttribute('hx-get') || detail.elt.getAttribute('action')))
        || '';
    const eltProfileId = detail.elt?.closest?.('[data-profile-id]')?.dataset?.profileId;

    if (detail.elt && detail.elt.id === 'create-profile-form') {
        const form = detail.elt;
        if (detail.successful) {
            form.reset();
            window.sshmc.emit('profile-created');
            htmx.trigger('#profile-list', 'refresh');
            htmx.trigger('#stats-bar', 'refresh');
            window.sshmc.emit('toast', { message: 'Profile created. Press Start to install the server and create its world.', type: 'success' });
        }
    }

    if (!detail.successful) {
        if (!detail._handled) {
            handleHtmxFailure(detail);
        }
    } else if (path.includes('/api/lifecycle/')) {
        const match = path.match(/\/api\/lifecycle\/([^/]+)\/(?:start|restart|cancel-start|stop)/);
        const pid = eltProfileId || (match && match[1]);
        if (pid) {
            window.sshmc.emit('preflight-cleared', { profile_id: pid });
        }
        htmx.trigger('#profile-list', 'refresh');
        htmx.trigger('#stats-bar', 'refresh');
        if (path.endsWith('/cancel-start')) {
            window.sshmc.emit('toast', { message: 'Startup cancelled. Mods and existing world files were kept; late cancellation can leave a partial world.', type: 'warning' });
        } else if (path.endsWith('/start')) {
            window.sshmc.emit('toast', { message: 'Server starting...', type: 'info' });
        } else if (path.endsWith('/restart')) {
            window.sshmc.emit('toast', { message: 'Server restarting...', type: 'info' });
        } else if (path.endsWith('/stop')) {
            window.sshmc.emit('toast', { message: 'Server stopping...', type: 'info' });
        }
    }
});

document.body.addEventListener('htmx:afterSwap', (event) => {
    if (!event.detail.target || event.detail.target.id !== 'profile-list') return;
    renderPreflightCardBanners();
    window.sshmc.emit('profile-list-swapped');
});
