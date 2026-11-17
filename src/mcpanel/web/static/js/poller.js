/**
 * SSHMC Central Poller
 *
 * Coordinates all recurring background polling into a single timer:
 * - Emits `sshmc:heartbeat-tick` on successful heartbeat
 * - Detects profile status changes and only triggers HTMX list refresh when statuses change
 * - Handles exponential error backoff (3s -> 6s -> 12s -> 24s) and updates connection status
 * - Pauses when `document.hidden` and triggers an immediate tick on resume
 * - Allows individual tabs to register callbacks that execute ONLY when that tab is active
 * - Supports temporary fast-polling mode (e.g. 1s) for active download/install jobs
 */

(function () {
    const NORMAL_INTERVAL = 3000;
    const FAST_INTERVAL = 1000;
    const MAX_BACKOFF = 24000;

    let timerId = null;
    let isTicking = false;
    let currentBackoff = NORMAL_INTERVAL;
    let isConnected = true;

    let lastStatuses = null;
    let lastTotalCount = null;
    let lastRunningCount = null;

    const tabCallbacks = new Map();
    const activeFastJobs = new Set();

    async function tick() {
        if (document.hidden) return;
        if (isTicking) return;
        isTicking = true;

        try {
            const response = await fetch('/api/heartbeat');
            if (response.status === 401) {
                window.location.assign('/login');
                return;
            }
            if (!response.ok) {
                throw new Error(`Heartbeat failed: ${response.status}`);
            }

            const data = await response.json();

            // Heartbeat succeeded
            if (!isConnected) {
                isConnected = true;
                window.sshmc.emit('connection-status', { online: true });
            }
            currentBackoff = NORMAL_INTERVAL;

            // 1. Process startup/preflight events from runtime
            if (Array.isArray(data.events) && data.events.length > 0) {
                for (const item of data.events) {
                    const isFail = item.message && item.message.toLowerCase().includes('fail');
                    window.sshmc.emit('toast', {
                        message: `${item.profile_name}: ${item.message}`,
                        type: isFail ? 'error' : 'info'
                    });
                }
                // Status changed due to event: refresh lists
                htmx.trigger('#profile-list', 'refresh');
                htmx.trigger('#stats-bar', 'refresh');
            }

            // 2. Check if profile status or count changed
            const statusesJson = JSON.stringify(data.statuses || {});
            const statusChanged = lastStatuses !== null && lastStatuses !== statusesJson;
            const countChanged = lastTotalCount !== null && (lastTotalCount !== data.total_count || lastRunningCount !== data.running_count);

            lastStatuses = statusesJson;
            lastTotalCount = data.total_count;
            lastRunningCount = data.running_count;

            window.sshmc.emit('counts-updated', {
                running: data.running_count,
                total: data.total_count
            });

            if (statusChanged || countChanged) {
                htmx.trigger('#profile-list', 'refresh');
                htmx.trigger('#stats-bar', 'refresh');
            }

            // 3. Emit heartbeat tick for interested subscribers
            window.sshmc.emit('heartbeat-tick', data);

            // 4. Run callback for current active tab ONLY
            const currentTab = window.sshmc.state.activeTab || 'profiles';
            const callback = tabCallbacks.get(currentTab);
            if (typeof callback === 'function') {
                try {
                    await callback(data);
                } catch (err) {
                    console.warn(`Tab callback for "${currentTab}" failed:`, err);
                }
            }
        } catch (err) {
            if (isConnected) {
                isConnected = false;
                window.sshmc.emit('connection-status', { online: false, error: err.message });
            }
            // Double backoff up to max
            currentBackoff = Math.min(currentBackoff * 2, MAX_BACKOFF);
        } finally {
            isTicking = false;
            scheduleNext();
        }
    }

    function scheduleNext(immediate = false) {
        if (timerId) clearTimeout(timerId);
        timerId = null;
        if (document.hidden) return;

        if (immediate) {
            tick();
            return;
        }

        const interval = isConnected
            ? (activeFastJobs.size > 0 ? FAST_INTERVAL : NORMAL_INTERVAL)
            : currentBackoff;

        timerId = setTimeout(tick, interval);
    }

    // Visibility change handling
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (timerId) {
                clearTimeout(timerId);
                timerId = null;
            }
        } else {
            // Immediate tick when tab becomes visible again
            scheduleNext(true);
        }
    });

    // Public API
    window.sshmc.poller = {
        registerTabCallback(tab, callback) {
            tabCallbacks.set(tab, callback);
        },
        unregisterTabCallback(tab) {
            tabCallbacks.delete(tab);
        },
        requestFastPolling(jobKey) {
            activeFastJobs.add(jobKey);
            // If currently waiting on a normal 3s timer, accelerate
            if (timerId && isConnected && currentBackoff === NORMAL_INTERVAL) {
                clearTimeout(timerId);
                timerId = setTimeout(tick, FAST_INTERVAL);
            }
        },
        releaseFastPolling(jobKey) {
            activeFastJobs.delete(jobKey);
        },
        triggerImmediate() {
            scheduleNext(true);
        }
    };

    // Auto-start poller on DOMContentLoaded or immediately
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => scheduleNext(true));
    } else {
        scheduleNext(true);
    }
})();
