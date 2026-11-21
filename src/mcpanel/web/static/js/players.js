// --- Players Tab (whitelist + operators) ---
window.playersApp = function playersApp() {
    const emptyState = () => ({
        phase: 'stopped',
        server_running: false,
        busy: false,
        can_edit_bypass: true,
        server_installed: false,
        online_mode: true,
        whitelist: { enabled: false, players: [] },
        operators: [],
        warnings: []
    });

    return {
        profiles: [],
        profileId: '',
        profile: null,
        state: emptyState(),
        loading: false,
        busy: false,
        error: '',
        whitelistName: '',
        opName: '',
        opLevel: 4,
        editingWhitelist: null,
        whitelistEditName: '',
        editingOperator: null,
        operatorEditName: '',
        token: 0,
        pollTimer: null,
        visible: false,
        levelOptions: [
            {
                value: 1,
                short: 'L1',
                label: 'Level 1 · Spawn protection',
                description: 'Can build and break blocks inside the spawn-protection radius. No extra commands.'
            },
            {
                value: 2,
                short: 'L2',
                label: 'Level 2 · Command blocks & cheats',
                description: 'Can use command blocks and most gameplay commands such as /give, /tp, /gamemode, /time and /weather, but cannot manage other players.'
            },
            {
                value: 3,
                short: 'L3',
                label: 'Level 3 · Manage players',
                description: 'Everything above, plus /kick, /ban, /op, /deop and whitelist changes.'
            },
            {
                value: 4,
                short: 'L4',
                label: 'Level 4 · Full admin',
                description: 'Can run every server command, including /stop and /reload. Only give this to people you fully trust.'
            }
        ],

        init() {
            this.refreshProfiles();
            window.addEventListener('sshmc-players-enter', () => this.enter());
            if (window.sshmc && typeof window.sshmc.on === 'function') {
                window.sshmc.on('tab-change', (e) => {
                    if (e.detail?.tab === 'players') this.enter();
                    else this.leave();
                });
            }
            if (window.sshmc?.state?.activeTab === 'players') this.enter();
        },

        enter() {
            this.visible = true;
            this.refreshProfiles();
            this.startPolling();
        },

        leave() {
            this.visible = false;
            this.cancelWhitelistEdit();
            this.cancelOperatorEdit();
            this.stopPolling();
        },

        startPolling() {
            this.stopPolling();
            this.pollTimer = setInterval(() => {
                if (!this.visible || !this.profileId || this.busy) return;
                // Do not replace the state under an in-progress rename.
                if (this.editingWhitelist || this.editingOperator) return;
                this.loadState(true);
            }, 15000);
        },

        stopPolling() {
            if (this.pollTimer) clearInterval(this.pollTimer);
            this.pollTimer = null;
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
                    this.profile = this.profiles.find(p => p.id === this.profileId);
                } else if (this.profiles.length > 0) {
                    this.profileId = this.profiles[0].id;
                    this.profile = this.profiles[0];
                    this.state = emptyState();
                    this.loadState();
                } else {
                    this.profileId = '';
                    this.profile = null;
                    this.state = emptyState();
                }
            } catch (error) {
                this.error = error.message || 'Could not load profiles.';
            }
        },

        selectProfile() {
            this.profile = this.profiles.find(p => p.id === this.profileId) || null;
            this.state = emptyState();
            this.whitelistName = '';
            this.opName = '';
            this.cancelWhitelistEdit();
            this.cancelOperatorEdit();
            this.error = '';
            if (this.profileId && this.profile) this.loadState();
        },

        async loadState(silent = false) {
            if (!this.profileId) return;
            const profileId = this.profileId;
            const token = ++this.token;
            if (!silent) this.loading = true;
            try {
                const response = await fetch(`/api/profiles/${encodeURIComponent(profileId)}/players`);
                if (response.status === 401) { window.location.assign('/login'); return; }
                const data = await this.readJson(response, 'Could not read player data.');
                if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${response.status}).`);
                if (token !== this.token || profileId !== this.profileId) return;
                this.state = { ...emptyState(), ...data };
                // Drop an inline edit if that player disappeared (removed on
                // another tab, server change, or a nickname update).
                if (this.editingWhitelist && !(data.whitelist?.players || []).some(p => this.entryKey(p) === this.editingWhitelist)) {
                    this.cancelWhitelistEdit();
                }
                if (this.editingOperator && !(data.operators || []).some(o => this.entryKey(o) === this.editingOperator)) {
                    this.cancelOperatorEdit();
                }
                if (!silent) this.error = '';
            } catch (error) {
                if (token === this.token && !silent) this.error = error.message || 'Could not load players.';
            } finally {
                if (token === this.token) this.loading = false;
            }
        },

        async send(method, path, body) {
            const response = await fetch(path, {
                method,
                headers: body ? { 'Content-Type': 'application/json' } : undefined,
                body: body ? JSON.stringify(body) : undefined
            });
            if (response.status === 401) { window.location.assign('/login'); throw new Error('Session expired.'); }
            if (!response.ok) {
                throw new Error(await responseMessage(response, `Request failed (${response.status}).`));
            }
            if (response.status === 204) return null;
            return this.readJson(response, 'The server returned an unexpected response.');
        },

        async mutate(action, successMessage) {
            if (this.busy) return;
            this.busy = true;
            this.error = '';
            try {
                await action();
                if (successMessage) showToast(successMessage, 'success');
            } catch (error) {
                this.error = error.message || 'That change could not be saved.';
                showToast(this.error, 'error');
            } finally {
                this.busy = false;
                // Silent: a failed action must keep its error visible.
                await this.loadState(true);
            }
        },

        validName(name) {
            const text = (name || '').trim();
            if (!text) return false;
            if (!/^[A-Za-z0-9_.+*-]{1,32}$/.test(text)) {
                const message = 'Player names may only contain letters, numbers, ".", "_", "+", "*" and "-" (up to 32 characters).';
                this.error = message;
                showToast(message, 'error');
                return false;
            }
            return true;
        },

        basePath(suffix = '') {
            return `/api/profiles/${encodeURIComponent(this.profileId)}/players${suffix}`;
        },

        entryKey(player) {
            return player.uuid || player.name;
        },

        isEditingWhitelist(player) {
            return Boolean(this.editingWhitelist && this.editingWhitelist === this.entryKey(player));
        },

        isEditingOperator(operator) {
            return Boolean(this.editingOperator && this.editingOperator === this.entryKey(operator));
        },

        levelDescription(value) {
            const option = this.levelOptions.find(item => item.value === Number(value));
            return option ? option.description : '';
        },

        async startWhitelistEdit(player) {
            if (this.busy || this.state.busy) return;
            this.cancelOperatorEdit();
            this.editingWhitelist = this.entryKey(player);
            this.whitelistEditName = player.name;
            await this.$nextTick();
            const input = this.$el.querySelector('.player-edit-input');
            if (input) {
                input.focus();
                input.select();
            }
        },

        cancelWhitelistEdit() {
            this.editingWhitelist = null;
            this.whitelistEditName = '';
        },

        async renameWhitelisted(player) {
            const name = this.whitelistEditName.trim();
            if (name === player.name) {
                this.cancelWhitelistEdit();
                return;
            }
            if (!this.validName(name)) return;
            const original = player.name;
            await this.mutate(
                () => this.send('PUT', this.basePath(`/whitelist/${encodeURIComponent(original)}`), { name }),
                `${original} was renamed to ${name}.`
            );
            if (!this.error) this.cancelWhitelistEdit();
        },

        async startOperatorEdit(operator) {
            if (this.busy || this.state.busy) return;
            this.cancelWhitelistEdit();
            this.editingOperator = this.entryKey(operator);
            this.operatorEditName = operator.name;
            await this.$nextTick();
            const input = this.$el.querySelector('.player-edit-input');
            if (input) {
                input.focus();
                input.select();
            }
        },

        cancelOperatorEdit() {
            this.editingOperator = null;
            this.operatorEditName = '';
        },

        async renameOperator(operator) {
            const name = this.operatorEditName.trim();
            if (name === operator.name) {
                this.cancelOperatorEdit();
                return;
            }
            if (!this.validName(name)) return;
            const original = operator.name;
            await this.mutate(
                () => this.send('PUT', this.basePath(`/ops/${encodeURIComponent(original)}`), { name }),
                `${original} is now known as ${name}.`
            );
            if (!this.error) this.cancelOperatorEdit();
        },

        async setWhitelist(event) {
            const enabled = Boolean(event.target.checked);
            if (enabled && this.state.whitelist.players.length === 0) {
                const confirmed = await window.sshmcConfirm({
                    title: 'Enable an empty whitelist?',
                    message: 'Nobody is on the whitelist yet, so no regular player will be able to join. Operators are not exempt.',
                    confirmLabel: 'Enable anyway',
                    danger: true
                });
                if (!confirmed) {
                    event.target.checked = false;
                    return;
                }
            }
            await this.mutate(
                () => this.send('PUT', this.basePath('/whitelist/settings'), { enabled }),
                enabled ? 'Whitelist enabled.' : 'Whitelist disabled.'
            );
        },

        async addWhitelisted() {
            const name = this.whitelistName.trim();
            if (!this.validName(name)) return;
            await this.mutate(async () => {
                const player = await this.send('POST', this.basePath('/whitelist'), { name });
                this.whitelistName = '';
                return player;
            }, `${name} was added to the whitelist.`);
        },

        async removeWhitelisted(player) {
            await this.mutate(
                () => this.send('DELETE', this.basePath(`/whitelist/${encodeURIComponent(player.name)}`)),
                `${player.name} was removed from the whitelist.`
            );
        },

        async addOperator() {
            const name = this.opName.trim();
            if (!this.validName(name)) return;
            const level = Number(this.opLevel) || 4;
            await this.mutate(async () => {
                await this.send('POST', this.basePath('/ops'), { name, level });
                this.opName = '';
            }, `${name} is now an operator (level ${level}).`);
        },

        async changeLevel(operator, value) {
            const level = Number(value);
            if (!level || level === operator.level) return;
            await this.mutate(
                () => this.send('PUT', this.basePath(`/ops/${encodeURIComponent(operator.name)}`), { level }),
                `${operator.name} is now level ${level}.`
            );
        },

        async changeBypass(operator, checked) {
            if (!this.state.can_edit_bypass) {
                showToast('Stop the server to change the bypass limit.', 'error');
                return;
            }
            await this.mutate(
                () => this.send('PUT', this.basePath(`/ops/${encodeURIComponent(operator.name)}`), { bypasses_player_limit: checked }),
                checked ? `${operator.name} may now join a full server.` : `${operator.name} no longer bypasses the player limit.`
            );
        },

        async removeOperator(operator) {
            await this.mutate(
                () => this.send('DELETE', this.basePath(`/ops/${encodeURIComponent(operator.name)}`)),
                `${operator.name} is no longer an operator.`
            );
        },

        avatarUrl(uuid) {
            if (!uuid) return 'data:image/gif;base64,R0lGODlhAQABAAAAACw=';
            return `https://mc-heads.net/avatar/${encodeURIComponent(uuid)}/32`;
        },

        initial(name) {
            return (name || '?').trim().charAt(0).toUpperCase() || '?';
        }
    };
};
