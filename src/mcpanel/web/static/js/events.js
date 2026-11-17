/**
 * SSHMC Central Event Bus
 *
 * All cross-component and cross-island communication is handled via CustomEvents on `window`
 * prefixed with "sshmc:".
 *
 * Supported Events:
 * - sshmc:toast               { message: string, type: 'info'|'success'|'warning'|'error' }
 * - sshmc:preflight-failure   { profile_id: string, message: string, ... }
 * - sshmc:preflight-cleared   { profile_id: string }
 * - sshmc:tab-change          { tab: 'profiles'|'monitoring'|'mods'|'storage' }
 * - sshmc:mods-select-profile { profileId: string }
 * - sshmc:edit-profile        { profile: object }
 * - sshmc:profile-created     { profileId?: string }
 * - sshmc:mod-download-failure { detail: object, path: string }
 * - sshmc:host-memory-update  { availableBytes: number|null }
 * - sshmc:connection-status   { online: boolean, error?: string }
 * - sshmc:heartbeat-tick      { data: object }
 */

(function () {
    const EVENT_PREFIX = 'sshmc:';

    // State holder for preflight failures across HTMX swaps
    const preflightFailures = {};

    function emit(name, detail = {}) {
        const eventName = name.startsWith(EVENT_PREFIX) ? name : EVENT_PREFIX + name;
        window.dispatchEvent(new CustomEvent(eventName, { detail, bubbles: true }));
    }

    function on(name, handler) {
        const eventName = name.startsWith(EVENT_PREFIX) ? name : EVENT_PREFIX + name;
        window.addEventListener(eventName, handler);
        return function off() {
            window.removeEventListener(eventName, handler);
        };
    }

    window.sshmc = {
        emit,
        on,
        state: {
            activeTab: 'profiles',
            get preflightFailures() {
                return preflightFailures;
            },
            setPreflightFailure(profileId, failure) {
                if (profileId) preflightFailures[profileId] = failure;
            },
            clearPreflightFailure(profileId) {
                if (profileId) delete preflightFailures[profileId];
            }
        },
        toast(message, type = 'info') {
            emit('toast', { message, type });
        }
    };

    // Backward-compatible global shim for showToast
    window.showToast = function (message, type = 'info') {
        emit('toast', { message, type });
    };

    // Track active tab
    on('tab-change', (e) => {
        if (e.detail?.tab) {
            window.sshmc.state.activeTab = e.detail.tab;
            window.sshmcActiveTab = e.detail.tab;
        }
    });

    // Track preflight failure state
    on('preflight-failure', (e) => {
        if (e.detail?.profile_id) {
            window.sshmc.state.setPreflightFailure(e.detail.profile_id, e.detail);
        }
    });
    on('preflight-cleared', (e) => {
        if (e.detail?.profile_id) {
            window.sshmc.state.clearPreflightFailure(e.detail.profile_id);
        }
    });
})();
