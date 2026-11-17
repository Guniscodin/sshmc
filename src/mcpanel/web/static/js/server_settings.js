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
            if (!this.profileId) { this.profile = null; this.fields = []; return; }
            this.syncSelectedProfile();
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
            this.fields = (data.fields || []).map(field => {
                let value = field.value;
                if (field.type === 'bool') value = String(value).toLowerCase() === 'true';
                else if (field.type === 'int') value = parseInt(value, 10) || 0;
                else value = String(value ?? '');
                return { ...field, value };
            });
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
    };
};
