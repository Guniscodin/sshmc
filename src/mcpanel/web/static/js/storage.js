function storageApp() {
    return {
        stats: {}, busy: false, scanTimer: null, scanCheckTimer: null, loadSequence: 0,
        init() {
            this.load();
            window.addEventListener('sshmc-storage-enter', () => this.load());
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.registerTabCallback('storage', async () => {
                    if (this.pendingScanPrevious && this.scanRemaining > 0) {
                        await this.load();
                        this.scanRemaining--;
                        if (this.stats.scanned_at !== this.pendingScanPrevious || this.scanRemaining <= 0) {
                            this.pendingScanPrevious = null;
                            window.sshmc.poller.releaseFastPolling('storage-scan');
                        }
                    }
                });
            }
        },
        destroy() {
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.unregisterTabCallback('storage');
                window.sshmc.poller.releaseFastPolling('storage-scan');
            }
        },
        async readError(response, fallback) {
            try {
                const payload = await response.json();
                return payload.detail || fallback;
            } catch (_) {
                return response.status ? `Request failed (${response.status}).` : fallback;
            }
        },
        async load() {
            const sequence = ++this.loadSequence;
            try {
                const response = await fetch('/api/storage');
                if (sequence !== this.loadSequence) return;
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await this.readError(response, 'Could not load storage information.'));
                const snapshot = await response.json();
                if (sequence !== this.loadSequence) return;
                this.stats = snapshot;
                if (this.stats.scanning || !this.stats.scanned_at) {
                    if (this.scanTimer) window.clearTimeout(this.scanTimer);
                    this.scanTimer = window.setTimeout(() => this.load(), 1500);
                } else if (this.scanTimer) {
                    window.clearTimeout(this.scanTimer);
                    this.scanTimer = null;
                }
            } catch (error) {
                if (sequence === this.loadSequence) showToast(error.message || 'Could not load storage information.');
            }
        },
        async refresh(waitForScan = false) {
            if (this.busy) return;
            this.busy = true;
            const previous = this.stats.scanned_at;
            try {
                if (waitForScan) {
                    const response = await fetch('/api/storage/refresh', { method: 'POST' });
                    if (response.status === 401) { window.location.assign('/login'); return; }
                    if (!response.ok) throw new Error(await this.readError(response, 'Could not start a storage scan.'));
                }
                await this.load();
                if (waitForScan) {
                    for (let attempt = 0; attempt < 60 && this.stats.scanned_at === previous; attempt++) {
                        await new Promise(resolve => window.setTimeout(resolve, 1000));
                        await this.load();
                    }
                }
            } catch (error) {
                showToast(error.message || 'Could not refresh storage information.');
            } finally { this.busy = false; }
        },
        formatSize(bytes = 0) {
            if (!Number.isFinite(Number(bytes)) || Number(bytes) < 0) return '—';
            const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
            let value = Number(bytes), index = 0;
            while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
            return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
        },
        get categoryPercentages() {
            const total = this.stats.combined_total_bytes || this.stats.total_bytes || 1;
            const profiles = Math.max(0, ((this.stats.profiles_bytes || 0) / total) * 100);
            const packs = Math.max(0, ((this.stats.modpacks_bytes || 0) / total) * 100);
            const logs = Math.max(0, ((this.stats.panel_logs_bytes || 0) / total) * 100);
            const uploads = Math.max(0, ((this.stats.uploads_bytes || 0) / total) * 100);
            const other = Math.max(0, ((this.stats.other_bytes || 0) / total) * 100);
            return {
                profiles: profiles.toFixed(1),
                packs: packs.toFixed(1),
                logs: logs.toFixed(1),
                uploads: uploads.toFixed(1),
                other: other.toFixed(1)
            };
        },
        profileRunning(profile) { return ['RUNNING', 'STARTING', 'STOPPING'].includes(profile.status); },
        worldEntries(profile) {
            return Object.entries(profile.worlds || {}).map(([name, size_bytes]) => ({ name, size_bytes }));
        },
        otherEntries(profile) {
            return Object.entries(profile.other_items || {}).map(([name, size_bytes]) => ({ name, size_bytes }));
        },
        async clearPanelLogs() {
            if (!await window.sshmcConfirm({ title: 'Clear panel logs?', message: 'This removes panel.log and all rotated panel logs. A new empty log will be created.', confirmLabel: 'Clear logs', danger: true })) return;
            await this.runAction('/api/storage/panel-logs', { method: 'DELETE' }, 'Panel logs cleared.');
        },
        async clearProfileLogs(profile) {
            if (!await window.sshmcConfirm({ title: 'Clear server logs?', message: `Remove logs and crash reports for ${profile.name}? The server must be stopped.`, confirmLabel: 'Clear files', danger: true })) return;
            await this.runAction(`/api/storage/profiles/${profile.id}/logs`, { method: 'DELETE' }, `${profile.name} logs and crash reports cleared.`);
        },
        async clearDownloadCache(profile) {
            if (!await window.sshmcConfirm({ title: 'Clear direct-link cache?', message: `Remove cached mod downloads for ${profile.name}? Its configured links will download again on a future start.`, confirmLabel: 'Clear cache', danger: true })) return;
            await this.runAction(`/api/profiles/${profile.id}/mod-download-cache/clear`, { method: 'POST' }, `${profile.name} direct-link cache cleared.`);
        },
        async clearModrinthCache(profile) {
            if (!await window.sshmcConfirm({ title: 'Clear Modrinth sources?', message: `Remove cached Modrinth JARs and their source manifest for ${profile.name}? Its current server files stay in place until the next Start or Restart.`, confirmLabel: 'Clear Modrinth cache', danger: true })) return;
            await this.runAction(`/api/storage/profiles/${profile.id}/modrinth-cache`, { method: 'DELETE' }, `${profile.name} Modrinth sources and cache cleared. Changes apply on the next Start or Restart.`);
        },
        async deleteWorld(profile, world) {
            if (!await window.sshmcConfirm({ title: 'Delete world folder?', message: `Permanently delete ${world.name} (${this.formatSize(world.size_bytes)}) from ${profile.name}? This cannot be undone.`, confirmLabel: 'Delete world', danger: true })) return;
            await this.runAction(`/api/storage/profiles/${profile.id}/worlds/${encodeURIComponent(world.name)}`, { method: 'DELETE' }, `Deleted ${world.name} from ${profile.name}.`);
        },
        async deleteBackup(profile, backup) {
            if (!await window.sshmcConfirm({ title: 'Delete backup?', message: `Permanently delete ${backup.filename} (${this.formatSize(backup.size_bytes)})?`, confirmLabel: 'Delete backup', danger: true })) return;
            await this.runAction(`/api/profiles/${profile.id}/backups/${encodeURIComponent(backup.filename)}`, { method: 'DELETE' }, 'Backup deleted.');
        },
        async deleteOrphan(profile) {
            if (!await window.sshmcConfirm({ title: 'Delete unlinked profile files?', message: `Permanently remove all files for ${profile.name}, including its worlds, cache, repository, and backups?`, confirmLabel: 'Delete files', danger: true })) return;
            await this.runAction(`/api/storage/profiles/${profile.id}/orphaned`, { method: 'DELETE' }, 'Unlinked profile files removed.');
        },
        async deletePack(pack) {
            if (!await window.sshmcConfirm({ title: 'Delete mod pack?', message: `Delete ${pack.name} (${this.formatSize(pack.size_bytes)})? Linked profiles will be unlinked. If a profile has a Git repository, Git mods/ becomes its base source again on the next Start or Restart.`, confirmLabel: 'Delete pack', danger: true })) return;
            await this.runAction(`/api/modpacks/${encodeURIComponent(pack.name)}`, { method: 'DELETE' }, payload => payload.running_profiles?.length ? `Deleted ${pack.name}. Running profiles apply the unlink on their next restart.` : `Deleted ${pack.name}.`);
        },
        async cleanUploads() {
            await this.runAction('/api/storage/uploads/cleanup', { method: 'POST' }, 'Expired upload sessions removed.');
        },
        async runAction(url, options, successMessage) {
            if (this.busy) return;
            this.busy = true;
            const previousScan = this.stats.scanned_at;
            try {
                const response = await fetch(url, options);
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await this.readError(response, 'Storage cleanup failed.'));
                const payload = response.headers.get('content-type')?.includes('application/json')
                    ? await response.json() : {};
                if (payload.failed?.length) {
                    const names = payload.failed.slice(0, 3).join(', ');
                    const remainder = payload.failed.length > 3 ? ` and ${payload.failed.length - 3} more` : '';
                    showToast(`Some log files could not be removed: ${names}${remainder}.`, 'error');
                } else {
                    showToast(typeof successMessage === 'function' ? successMessage(payload) : successMessage);
                }
                const refreshResponse = await fetch('/api/storage/refresh', { method: 'POST' });
                if (refreshResponse.status === 401) { window.location.assign('/login'); return; }
                if (!refreshResponse.ok) throw new Error(await this.readError(refreshResponse, 'Cleanup succeeded, but the storage scan could not be refreshed.'));
                await this.load();
                this.scheduleScanCheck(previousScan);
            } catch (error) {
                showToast(error.message || 'Storage cleanup failed.', 'error');
            } finally { this.busy = false; }
        },
        scheduleScanCheck(previousScan, remaining = 30) {
            if (remaining <= 0 || this.stats.scanned_at !== previousScan) return;
            this.pendingScanPrevious = previousScan;
            this.scanRemaining = remaining;
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.requestFastPolling('storage-scan');
            }
        }
    };
}
