// --- Server Settings Tab Logic ---
window.serverSettingsApp = function serverSettingsApp() {
    return {
        profiles: [],
        profileId: '',
        profile: null,
        fields: [],
        serverInstalled: false,
        serverRunning: false,
        managedKeys: [],
        loading: false,
        saving: false,
        error: '',
        dirty: false,
        settingsToken: 0,
        searchQuery: '',
        broadcastMessage: '',
        broadcastPrefix: '[Server]',
        broadcastType: 'chat',
        broadcastColor: 'gold',
        broadcastBold: false,
        broadcastItalic: false,
        sendingBroadcast: false,

        categories: [
            {
                id: 'world',
                label: 'World & Generation',
                icon: 'fa-earth-americas',
                description: 'World directory, seed, generator preset, world border, and Nether travel.'
            },
            {
                id: 'gameplay',
                label: 'Gameplay & Rules',
                icon: 'fa-gamepad',
                description: 'Difficulty, default game mode, PvP, hardcore, flight, and command blocks.'
            },
            {
                id: 'spawns',
                label: 'Mobs & Spawning',
                icon: 'fa-paw',
                description: 'Hostile monsters, passive animals, villagers, and spawn protection.'
            },
            {
                id: 'multiplayer',
                label: 'Multiplayer & Player Policies',
                icon: 'fa-users',
                description: 'Online mode, AFK timeout, player limit, privacy, and proxy protection.'
            },
            {
                id: 'performance',
                label: 'Performance & Crash Prevention',
                icon: 'fa-gauge-high',
                description: 'Render & simulation distances, watchdog tick limit, network compression, and chunk saving.'
            },
            {
                id: 'admin',
                label: 'Admin & Broadcasts',
                icon: 'fa-bullhorn',
                description: 'Operator alerts, RCON notifications, and live in-game server announcements.'
            }
        ],

        fieldsByCategory(catId) {
            const query = (this.searchQuery || '').trim().toLowerCase();
            return this.fields.filter(f => {
                if ((f.category || 'gameplay') !== catId) return false;
                if (!query) return true;
                return (
                    (f.label && f.label.toLowerCase().includes(query)) ||
                    (f.key && f.key.toLowerCase().includes(query)) ||
                    (f.description && f.description.toLowerCase().includes(query))
                );
            });
        },

        categoryVisible(catId) {
            return this.fieldsByCategory(catId).length > 0;
        },

        matchingCount() {
            const query = (this.searchQuery || '').trim().toLowerCase();
            if (!query) return this.fields.length;
            return this.fields.filter(f => (
                (f.label && f.label.toLowerCase().includes(query)) ||
                (f.key && f.key.toLowerCase().includes(query)) ||
                (f.description && f.description.toLowerCase().includes(query))
            )).length;
        },

        // Server identity (icon + description)
        description: '',
        hasIcon: false,
        identityDirty: false,
        savingIdentity: false,
        pendingIconFile: null,
        pendingIconUrl: null,
        iconVersion: 0,

        get iconUrl() {
            return `/api/profiles/${encodeURIComponent(this.profileId)}/icon?v=${this.iconVersion}`;
        },

        init() {
            this.refreshProfiles();
            window.addEventListener('sshmc-settings-enter', () => this.refreshProfiles());
            if (window.sshmc && typeof window.sshmc.on === 'function') {
                window.sshmc.on('tab-change', (e) => {
                    if (e.detail?.tab === 'settings') this.refreshProfiles();
                });
            }
        },

        async readJson(response, fallback) {
            const raw = await response.text();
            try { return raw ? JSON.parse(raw) : {}; }
            catch (_) {
                if (!response.ok) return { detail: `Request failed (${response.status}).` };
                throw new Error(fallback);
            }
        },

        async refreshProfiles() {
            try {
                const response = await fetch('/api/profiles');
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not load profiles.'));
                const data = await this.readJson(response, 'Could not load profiles.');
                this.profiles = Array.isArray(data) ? data : [];
                if (this.profileId && this.profiles.some(p => p.id === this.profileId)) {
                    // keep current selection
                } else if (this.profiles.length > 0) {
                    this.profileId = this.profiles[0].id;
                } else {
                    this.profileId = '';
                    this.profile = null;
                    this.fields = [];
                }
                if (this.profileId) await this.selectProfile();
            } catch (error) {
                this.error = error.message || 'Could not load profiles.';
            }
        },

        async selectProfile() {
            if (!this.profileId) { this.profile = null; this.fields = []; this.searchQuery = ''; return; }
            this.syncSelectedProfile();
            this.searchQuery = '';
            // Reset identity state from the previous profile so the icon
            // preview can't fetch the new profile's icon before settings load.
            this.hasIcon = false;
            this.identityDirty = false;
            this.clearPendingIcon();
            await this.loadSettings();
        },

        syncSelectedProfile() {
            const found = this.profiles.find(p => p.id === this.profileId);
            this.profile = found ? { ...found } : null;
        },

        async loadSettings() {
            const requestId = ++this.settingsToken;
            this.loading = true;
            this.error = '';
            this.dirty = false;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/server-settings`);
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not load server settings.'));
                const data = await this.readJson(response, 'Could not load server settings.');
                if (requestId !== this.settingsToken) return;
                this.applySettings(data);
            } catch (error) {
                if (requestId === this.settingsToken) this.error = error.message || 'Could not load server settings.';
            } finally {
                if (requestId === this.settingsToken) this.loading = false;
            }
        },

        applySettings(data) {
            this.serverInstalled = !!data.server_installed;
            this.serverRunning = !!data.server_running;
            this.managedKeys = data.managed_keys || [];
            this.description = data.description || '';
            this.hasIcon = !!data.has_icon;
            this.identityDirty = false;
            this.clearPendingIcon();
            this.fields = (data.fields || []).map(field => {
                let value = field.value;
                if (field.type === 'bool') value = String(value).toLowerCase() === 'true';
                else if (field.type === 'int') {
                    const parsed = parseInt(value, 10);
                    value = Number.isNaN(parsed) ? 0 : parsed;
                }
                else value = String(value ?? '');
                return { ...field, value };
            });
        },

        clearPendingIcon() {
            if (this.pendingIconUrl) { URL.revokeObjectURL(this.pendingIconUrl); this.pendingIconUrl = null; }
            this.pendingIconFile = null;
            const input = document.getElementById('server-icon-input');
            if (input) input.value = '';
        },

        onIconChosen(event) {
            const file = event.target.files && event.target.files[0];
            if (!file) { this.clearPendingIcon(); return; }
            if (file.type !== 'image/png') {
                showToast('The server icon must be a PNG image.', 'error');
                this.clearPendingIcon();
                return;
            }
            if (file.size > 256 * 1024) {
                showToast('The server icon must be smaller than 256 KB.', 'error');
                this.clearPendingIcon();
                return;
            }
            this.pendingIconFile = file;
            if (this.pendingIconUrl) URL.revokeObjectURL(this.pendingIconUrl);
            this.pendingIconUrl = URL.createObjectURL(file);
        },

        async uploadIcon() {
            if (!this.pendingIconFile || this.savingIdentity || !this.profileId) return;
            this.savingIdentity = true;
            try {
                const formData = new FormData();
                formData.append('icon', this.pendingIconFile);
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/icon`, {
                    method: 'POST',
                    body: formData,
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const data = await this.readJson(response, 'Could not upload the server icon.');
                if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not upload the server icon.');
                this.hasIcon = true;
                this.iconVersion++;
                this.clearPendingIcon();
                showToast('Server icon saved. It applies the next time the server starts.', 'success');
                this.refreshProfileCards();
            } catch (error) {
                showToast(error.message || 'Could not upload the server icon.', 'error');
            } finally {
                this.savingIdentity = false;
            }
        },

        async removeIcon() {
            if (this.savingIdentity || !this.profileId) return;
            this.savingIdentity = true;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/icon`, { method: 'DELETE' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok && response.status !== 404) {
                    const data = await this.readJson(response, 'Could not remove the server icon.');
                    throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not remove the server icon.');
                }
                this.hasIcon = false;
                this.iconVersion++;
                showToast('Server icon removed.', 'success');
                this.refreshProfileCards();
            } catch (error) {
                showToast(error.message || 'Could not remove the server icon.', 'error');
            } finally {
                this.savingIdentity = false;
            }
        },

        async saveDescription() {
            if (this.savingIdentity || !this.profileId) return;
            this.savingIdentity = true;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/server-identity`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ description: this.description }),
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const data = await this.readJson(response, 'Could not save the description.');
                if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save the description.');
                this.description = data.description || '';
                this.identityDirty = false;
                this.syncSelectedProfile();
                if (this.profile) this.profile.description = this.description;
                showToast(
                    data.applies_after_restart
                        ? 'Description saved. The server MOTD updates on the next restart.'
                        : 'Description saved. It is used as the server MOTD from the next Start or Restart on.',
                    'success'
                );
                this.refreshProfileCards();
            } catch (error) {
                showToast(error.message || 'Could not save the description.', 'error');
            } finally {
                this.savingIdentity = false;
            }
        },

        discardDescription() {
            this.loadSettings();
        },

        refreshProfileCards() {
            if (window.htmx) {
                window.htmx.trigger('#profile-list', 'refresh');
            }
        },

        async saveSettings() {
            if (!this.profileId || this.saving) return;
            const properties = {};
            for (const field of this.fields) {
                if (field.type === 'bool') properties[field.key] = field.value ? 'true' : 'false';
                else if (field.type === 'int') properties[field.key] = String(parseInt(field.value, 10));
                else properties[field.key] = String(field.value ?? '').trim();
            }
            this.saving = true;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/server-settings`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ properties }),
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const data = await this.readJson(response, 'Could not save server settings.');
                if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not save server settings.');
                this.dirty = false;
                this.serverRunning = !!data.applies_after_restart;
                showToast(
                    data.applies_after_restart
                        ? 'Server settings saved. They apply the next time this server restarts.'
                        : 'Server settings saved. They apply on the next Start or Restart.',
                    'success'
                );
            } catch (error) {
                showToast(error.message || 'Could not save server settings.', 'error');
            } finally {
                this.saving = false;
            }
        },

        async discardChanges() {
            await this.loadSettings();
        },

        broadcastPreviewColor() {
            const colors = {
                gold: '#FFAA00',
                yellow: '#FFFF55',
                red: '#FF5555',
                green: '#55FF55',
                aqua: '#55FFFF',
                light_purple: '#FF55FF',
                white: '#FFFFFF',
                gray: '#AAAAAA',
            };
            return colors[this.broadcastColor] || '#FFAA00';
        },

        async sendBroadcast() {
            const msg = (this.broadcastMessage || '').trim();
            if (!msg || !this.profileId || this.sendingBroadcast) return;
            if (!this.serverRunning) {
                showToast('The server must be running to send live in-game broadcasts.', 'error');
                return;
            }
            this.sendingBroadcast = true;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(this.profileId)}/broadcast`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        message: msg,
                        prefix: this.broadcastPrefix,
                        display_type: this.broadcastType,
                        color: this.broadcastColor,
                        bold: this.broadcastBold,
                        italic: this.broadcastItalic,
                    }),
                });
                if (response.status === 401) { window.location.assign('/login'); return; }
                const data = await this.readJson(response, 'Could not send broadcast.');
                if (!response.ok) {
                    throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not send broadcast.');
                }
                showToast('Broadcast delivered to in-game server!', 'success');
                this.broadcastMessage = '';
            } catch (err) {
                showToast(err.message || 'Could not send broadcast.', 'error');
            } finally {
                this.sendingBroadcast = false;
            }
        },
    };
};
