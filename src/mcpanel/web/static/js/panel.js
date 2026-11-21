function panelApp() {
    return {
        consoleOpen: false,
        consoleFullscreen: false,
        consoleProfileId: '',
        consoleProfileName: '',
        consoleFilterText: '',
        autoScroll: true,
        logs: [],
        ws: null,
        maxLogs: 1000,

        newProfileModalOpen: false,
        mobileNavOpen: false,

        preflightModalOpen: false,
        preflightFailure: null,

        modDownloadPromptOpen: false,
        modDownloadFailure: {},
        modDownloadAction: '',
        skippedModIndexes: [],
        modDownloadWorkflow: false,
        isResolvingModFailure: false,

        deleteModalOpen: false,
        deleteId: null,
        deleteName: '',
        deleteFiles: false,
        isDeleting: false,

        editModalOpen: false,
        monitoringAvailableBytes: null,
        shutdownPending: false,

        activeTab: 'profiles',
        sidebarCollapsed: localStorage.getItem('sshmc-sidebar-collapsed') === 'true',
        profileDetailsState: {},

        totalCount: 0,
        runningCount: 0,
        connectionLost: false,

        backupDialogOpen: false,
        backupProfileId: '',
        backupProfileName: '',
        backupSelection: { worlds: true, mods: false, config: false, datapacks: false, server_settings: true },
        backupLabel: '',
        backupBusy: false,
        backupElapsed: 0,
        backupTimer: null,

        init() {
            if (window.sshmc) {
                window.sshmc.state.activeTab = this.activeTab;
            }
            this.$el.addEventListener('close-edit', () => { this.editModalOpen = false; });

            // Subscribe to Event Bus
            if (window.sshmc && typeof window.sshmc.on === 'function') {
                window.sshmc.on('open-preflight-modal', e => this.showPreflightModal(e.detail));
                window.sshmc.on('mod-download-failure', e => this.showModDownloadFailure(e.detail.detail, e.detail.path));
                window.sshmc.on('profile-created', () => { this.newProfileModalOpen = false; });
                window.sshmc.on('host-memory-update', e => { this.monitoringAvailableBytes = e.detail?.availableBytes ?? null; });
                window.sshmc.on('counts-updated', e => {
                    if (e.detail) {
                        this.runningCount = e.detail.running ?? this.runningCount;
                        this.totalCount = e.detail.total ?? this.totalCount;
                    }
                });
                window.sshmc.on('connection-status', e => {
                    this.connectionLost = !e.detail?.online;
                });
            }

            document.body.addEventListener('htmx:beforeSwap', event => {
                if (event.detail.target?.id !== 'profile-list') return;
                this.profileDetailsState = Object.fromEntries(
                    Array.from(document.querySelectorAll('[data-profile-details]'))
                        .map(details => [details.dataset.profileDetails, details.open])
                );
            });

            document.body.addEventListener('htmx:afterSwap', event => {
                if (event.detail.target?.id !== 'profile-list') return;
                document.querySelectorAll('[data-profile-details]').forEach(details => {
                    const saved = this.profileDetailsState[details.dataset.profileDetails];
                    details.open = saved === undefined ? false : saved;
                });
                this.updateCounts();
            });

            const savedLimit = Number(localStorage.getItem('sshmc-log-limit'));
            if ([100, 500, 1000, 2000, 5000].includes(savedLimit)) this.maxLogs = savedLimit;

            document.addEventListener('fullscreenchange', () => {
                this.consoleFullscreen = document.fullscreenElement === this.$refs.consoleContainer;
            });

            this.$nextTick(() => this.updateCounts());
        },

        updateCounts() {
            const cards = document.querySelectorAll('.profile-card[data-profile-id]');
            this.totalCount = cards.length;
            this.runningCount = document.querySelectorAll('.profile-card.running').length;
        },

        setTab(tab) {
            this.activeTab = tab;
            if (window.sshmc) {
                window.sshmc.state.activeTab = tab;
                window.sshmc.emit('tab-change', { tab });
            }
            this.mobileNavOpen = false;
            if (tab === 'mods') window.dispatchEvent(new Event('sshmc-mods-enter'));
            if (tab === 'players') window.dispatchEvent(new Event('sshmc-players-enter'));
            if (tab === 'storage') window.dispatchEvent(new Event('sshmc-storage-enter'));
            if (tab === 'settings') window.dispatchEvent(new Event('sshmc-settings-enter'));
        },

        toggleSidebar() {
            this.sidebarCollapsed = !this.sidebarCollapsed;
            localStorage.setItem('sshmc-sidebar-collapsed', this.sidebarCollapsed ? 'true' : 'false');
        },

        toggleMobileNav() {
            this.mobileNavOpen = !this.mobileNavOpen;
        },

        showPreflightModal(detail) {
            this.preflightFailure = detail;
            this.preflightModalOpen = true;
        },

        closePreflightModal() {
            this.preflightModalOpen = false;
        },

        goToModsForProfile(profileId) {
            this.preflightModalOpen = false;
            this.setTab('mods');
            if (window.sshmc) {
                window.sshmc.emit('mods-select-profile', { profileId, view: 'installed' });
            }
        },

        openNewProfileModal() {
            this.newProfileModalOpen = true;
        },

        closeNewProfileModal() {
            this.newProfileModalOpen = false;
        },

        async deleteBackup(profileId, filename) {
            if (!await window.sshmcConfirm({ title: 'Delete backup?', message: `Delete backup ${filename}?`, confirmLabel: 'Delete', danger: true })) return;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(profileId)}/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not delete backup.'));
                htmx.trigger('#profile-list', 'refresh');
                window.sshmc.emit('toast', { message: `Deleted backup ${filename}.`, type: 'success' });
            } catch (error) { window.sshmc.emit('toast', { message: error.message || 'Could not delete backup.', type: 'error' }); }
        },

        async shutdownPanel() {
            if (!await window.sshmcConfirm({ title: 'Shut down SSHMC?', message: 'The panel will close. Running Minecraft servers will keep running.', confirmLabel: 'Shut down', danger: true })) return;
            this.shutdownPending = true;
            try {
                const response = await fetch('/api/panel/shutdown', { method: 'POST' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Shutdown failed (${response.status}).`);
                window.sshmc.emit('toast', { message: 'SSHMC is shutting down. Minecraft servers will keep running.', type: 'warning' });
            } catch (error) {
                this.shutdownPending = false;
                window.sshmc.emit('toast', { message: error.message || 'Could not shut down SSHMC.', type: 'error' });
            }
        },

        openBackupModal(profileId, profileName) {
            this.backupProfileId = profileId;
            this.backupProfileName = profileName;
            this.backupSelection = { worlds: true, mods: false, config: false, datapacks: false, server_settings: true };
            this.backupLabel = '';
            this.backupElapsed = 0;
            this.backupDialogOpen = true;
        },

        async createManualBackup() {
            if (this.backupBusy) return;
            this.backupBusy = true;
            this.backupElapsed = 0;
            this.backupTimer = window.setInterval(() => { this.backupElapsed++; }, 1000);
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.backupProfileId)}/backups`, {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...this.backupSelection, label: this.backupLabel.slice(0, 40) })
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `Backup failed (${response.status}).`);
                const mib = (payload.size_bytes / 1024 / 1024).toFixed(1);
                window.sshmc.emit('toast', { message: `Backup created: ${payload.name} (${mib} MiB).`, type: 'success' });
                this.backupDialogOpen = false;
                htmx.trigger('#profile-list', 'refresh');
            } catch (error) {
                window.sshmc.emit('toast', { message: error.message || 'Could not create this backup.', type: 'error' });
            } finally {
                this.backupBusy = false;
                if (this.backupTimer) window.clearInterval(this.backupTimer);
            }
        },

        async cancelActiveBackup() {
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.backupProfileId)}/backups/cancel`, { method: 'POST' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not cancel backup.'));
                window.sshmc.emit('toast', { message: 'Cancelling backup...', type: 'warning' });
                this.backupDialogOpen = false;
                htmx.trigger('#profile-list', 'refresh');
            } catch (error) {
                window.sshmc.emit('toast', { message: error.message || 'Could not cancel this backup.', type: 'error' });
            }
        },

        showModDownloadFailure(failure, requestPath = '') {
            this.modDownloadFailure = failure;
            this.modDownloadAction = requestPath.includes('/restart') ? 'restart' : 'start';
            this.skippedModIndexes = [];
            this.modDownloadWorkflow = true;
            this.isResolvingModFailure = false;
            this.modDownloadPromptOpen = true;
        },

        async cancelModDownloadStartup() {
            const profileId = this.modDownloadFailure.profile_id;
            this.isResolvingModFailure = true;
            try {
                const response = await fetch(`/api/lifecycle/${encodeURIComponent(profileId)}/cancel-start`, { method: 'POST' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                this.modDownloadPromptOpen = false;
                this.modDownloadWorkflow = false;
                this.skippedModIndexes = [];
                htmx.trigger('#profile-list', 'refresh');
                htmx.trigger('#stats-bar', 'refresh');
                window.sshmc.emit('toast', { message: 'Startup stopped. Files on disk were left intact.', type: 'warning' });
            } catch (error) {
                window.sshmc.emit('toast', { message: error.message || 'Could not cancel startup.', type: 'error' });
            } finally {
                this.isResolvingModFailure = false;
            }
        },

        async continueWithoutFailedMod() {
            const profileId = this.modDownloadFailure.profile_id;
            const failedIndex = Number(this.modDownloadFailure.index);
            if (Number.isInteger(failedIndex) && !this.skippedModIndexes.includes(failedIndex)) {
                this.skippedModIndexes.push(failedIndex);
            }
            this.isResolvingModFailure = true;
            try {
                const action = this.modDownloadAction === 'restart' ? 'restart' : 'start';
                const url = `/api/lifecycle/${encodeURIComponent(profileId)}/${action}`;
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ skip_link_indexes: this.skippedModIndexes })
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const payload = await response.json().catch(() => ({}));
                if (response.ok) {
                    this.modDownloadPromptOpen = false;
                    this.modDownloadWorkflow = false;
                    this.skippedModIndexes = [];
                    this.isResolvingModFailure = false;
                    htmx.trigger('#profile-list', 'refresh');
                    htmx.trigger('#stats-bar', 'refresh');
                    window.sshmc.emit('toast', { message: 'Startup launched. The profile will show Running when Minecraft is ready.', type: 'success' });
                } else if (response.status === 409 && payload.detail && payload.detail.code === 'mod_download_failed') {
                    this.modDownloadFailure = payload.detail;
                    this.isResolvingModFailure = false;
                    htmx.trigger('#profile-list', 'refresh');
                } else if (response.status === 409 && payload.detail && payload.detail.code === 'source_preflight_failed') {
                    this.modDownloadPromptOpen = false;
                    this.modDownloadWorkflow = false;
                    this.isResolvingModFailure = false;
                    window.sshmc.emit('preflight-failure', payload.detail);
                    htmx.trigger('#profile-list', 'refresh');
                } else {
                    this.modDownloadPromptOpen = false;
                    this.modDownloadWorkflow = false;
                    this.isResolvingModFailure = false;
                    this.skippedModIndexes = [];
                    window.sshmc.emit('toast', { message: typeof payload.detail === 'string' ? payload.detail : `Startup failed (${response.status}).`, type: 'error' });
                    htmx.trigger('#profile-list', 'refresh');
                    htmx.trigger('#stats-bar', 'refresh');
                }
            } catch (error) {
                this.modDownloadPromptOpen = false;
                this.modDownloadWorkflow = false;
                this.isResolvingModFailure = false;
                window.sshmc.emit('toast', { message: error.message || 'Could not continue server startup.', type: 'error' });
            }
        },

        async openEditModal(profileId) {
            try {
                const [response, packsResponse] = await Promise.all([
                    fetch(`/api/profiles/${encodeURIComponent(profileId)}/settings`),
                    fetch('/api/modpacks')
                ]);
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not load profile settings.'));
                if (packsResponse.status === 401) { window.location.assign('/login'); return; }
                if (!packsResponse.ok) throw new Error(await responseMessage(packsResponse, 'Could not load mod packs.'));
                const profileData = await response.json();
                const packData = await packsResponse.json();
                this.editModalOpen = true;
                this.$nextTick(() => {
                    window.sshmc.emit('edit-profile', { profile: profileData, packs: packData.packs || [] });
                });
            } catch (error) {
                window.sshmc.emit('toast', { message: error.message || 'Could not load profile settings.', type: 'error' });
            }
        },

        async toggleConsoleFullscreen() {
            try {
                if (document.fullscreenElement) await document.exitFullscreen();
                else await this.$refs.consoleContainer.requestFullscreen();
            } catch (error) { window.sshmc.emit('toast', { message: error.message || 'Fullscreen is unavailable in this browser.', type: 'error' }); }
        },

        openConsole(profileId, profileName = '') {
            if (this.ws) { this.ws.onclose = null; this.ws.close(); this.ws = null; }
            this.logs = [];
            this.consoleOpen = true;
            this.consoleFullscreen = false;
            this.consoleProfileId = profileId;
            this.consoleProfileName = profileName;
            this.consoleFilterText = '';

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            this.ws = new WebSocket(`${protocol}//${window.location.host}/ws/${profileId}/console`);
            this.ws.onopen = () => this.appendLog(`--- Connected to ${profileName || 'server'} console ---`);
            this.ws.onmessage = (e) => this.appendLog(e.data);
            this.ws.onerror = () => {
                this.appendLog('--- Console connection failed; check panel authentication and server log files. ---');
                window.sshmc.emit('toast', { message: 'Could not connect to the server console.', type: 'error' });
            };
            this.ws.onclose = () => { if (this.consoleOpen) this.appendLog('--- Connection closed ---'); };
        },

        appendLog(line) {
            this.logs.push(line);
            if (this.logs.length > this.maxLogs) this.logs.splice(0, this.logs.length - this.maxLogs);
            if (this.autoScroll) {
                this.$nextTick(() => {
                    const el = document.getElementById('console-output');
                    if (el) el.scrollTop = el.scrollHeight;
                });
            }
        },

        closeConsole() {
            this.consoleOpen = false;
            if (document.fullscreenElement === this.$refs.consoleContainer) document.exitFullscreen().catch(() => {});
            this.consoleFullscreen = false;
            if (this.ws) { this.ws.onclose = null; this.ws.close(); this.ws = null; }
        },

        setLogLimit(value) {
            const parsed = Number(value);
            if (![100, 500, 1000, 2000, 5000].includes(parsed)) return;
            this.maxLogs = parsed;
            localStorage.setItem('sshmc-log-limit', String(parsed));
            if (this.logs.length > parsed) this.logs.splice(0, this.logs.length - parsed);
        },

        async copyConsoleLogs() {
            const text = this.filteredLogs().join('\n');
            try {
                await navigator.clipboard.writeText(text);
                window.sshmc.emit('toast', { message: 'Console logs copied to clipboard.', type: 'success' });
            } catch (_) {
                window.sshmc.emit('toast', { message: 'Could not copy to clipboard.', type: 'error' });
            }
        },

        downloadConsoleLogs() {
            const text = this.filteredLogs().join('\n');
            const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${this.consoleProfileName || 'server'}-console-${Date.now()}.log`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            window.sshmc.emit('toast', { message: 'Console logs downloaded.', type: 'info' });
        },

        logLevelClass(line) {
            if (typeof line !== 'string') return '';
            const upper = line.toUpperCase();
            if (upper.includes('/WARN') || upper.includes('[WARN]')) return 'level-warn';
            if (upper.includes('/ERROR') || upper.includes('[ERROR]') || upper.includes('EXCEPTION') || upper.includes('FATAL')) return 'level-error';
            if (upper.includes('/INFO') || upper.includes('[INFO]')) return 'level-info';
            return '';
        },

        filteredLogs() {
            if (!this.consoleFilterText) return this.logs;
            const q = this.consoleFilterText.toLowerCase();
            return this.logs.filter(line => typeof line === 'string' && line.toLowerCase().includes(q));
        },

        openDeleteModal(id, name) {
            this.deleteId = id;
            this.deleteName = name;
            this.deleteFiles = false;
            this.isDeleting = false;
            this.deleteModalOpen = true;
        },

        async confirmDelete() {
            this.isDeleting = true;
            try {
                const res = await fetch(`/api/profiles/${this.deleteId}?delete_files=${this.deleteFiles}`, { method: 'DELETE' });
                if (res.ok) {
                    this.deleteModalOpen = false;
                    htmx.trigger('#profile-list', 'refresh');
                    htmx.trigger('#stats-bar', 'refresh');
                    window.sshmc.emit('toast', { message: 'Profile deleted.', type: 'success' });
                } else {
                    window.sshmc.emit('toast', { message: await responseMessage(res, 'Could not delete profile.'), type: 'error' });
                }
            } catch (e) {
                window.sshmc.emit('toast', { message: e.message || 'Could not delete profile.', type: 'error' });
            } finally {
                this.isDeleting = false;
            }
        }
    };
}
