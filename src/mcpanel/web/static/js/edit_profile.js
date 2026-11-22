// --- Edit Profile Logic ---
        function editApp() {
            return {
                profile: {}, packs: [], launchDirs: [],
                loaders: [],
                isLoading: false,
                loaderPending: false,
                isSaving: false,
                isClearingCache: false,
                loaderError: '', loaderRequest: 0,
                originalBuild: '', originalMcVersion: '', originalLoaderVersion: '', originalModpackName: '',
                pendingReplacement: false,
                _offEdit: null,
                init() {
                    if (window.sshmc && typeof window.sshmc.on === 'function') {
                        this._offEdit = window.sshmc.on('edit-profile', (e) => {
                            if (e.detail) {
                                if (e.detail.packs) this.packs = e.detail.packs;
                                if (e.detail.launchDirs) this.launchDirs = e.detail.launchDirs;
                                this.setProfile(e.detail.profile || e.detail);
                            }
                        });
                    }
                },
                destroy() {
                    if (this._offEdit) this._offEdit();
                },
                setProfile(profile) {
                    this.profile = { ...profile };
                    this.pendingReplacement = !!profile.server_replacement_pending;
                    if (!this.pendingReplacement) {
                        this.profile.backup_worlds_on_change = true;
                        this.profile.backup_mods_on_change = false;
                        this.profile.backup_config_on_change = false;
                        this.profile.backup_datapacks_on_change = false;
                        this.profile.backup_server_settings_on_change = false;
                    }
                    this.profile.modpack_name = profile.modpack_name || '';
                    this.profile.launch_dir = profile.launch_dir || '';
                    this.profile.mod_downloads = (profile.mod_downloads || []).map(item => ({ ...item, filename: item.filename || '', sha256: item.sha256 || '', skip_on_start: !!item.skip_on_start }));
                    if (!this.profile.mod_downloads.length) this.profile.mod_downloads = [{ url: '', filename: '', sha256: '', skip_on_start: false }];
                    this.loaderPending = false;
                    this.originalBuild = profile.build;
                    this.originalMcVersion = profile.mc_version;
                    this.originalLoaderVersion = profile.loader_version;
                    this.originalModpackName = profile.modpack_name || '';
                    this.fetchLoaders(true);
                },
                needsReset() {
                    return this.pendingReplacement || this.profile.build !== this.originalBuild || this.profile.mc_version !== this.originalMcVersion || this.profile.loader_version !== this.originalLoaderVersion;
                },
                compatibleLaunchDirs() {
                    const version = (this.profile.mc_version || '').trim();
                    return (this.launchDirs || []).filter(directory =>
                        directory.build === this.profile.build
                        && directory.mc_version === version
                        && (!directory.assigned_profile_id || directory.assigned_profile_id === this.profile.id));
                },
                resetLaunchDir() {
                    this.profile.launch_dir = '';
                },
                packWarnings() {
                    const pack = this.packs.find(item => item.name === this.profile.modpack_name);
                    if (!pack) return [];
                    const selectedLoader = this.profile.build;
                    const warnings = [...(pack.warnings || [])];
                    if (pack.target_loader !== selectedLoader) warnings.unshift(`Pack target is ${pack.target_loader}; profile uses ${selectedLoader}.`);
                    warnings.push(...pack.jars.flatMap(jar => {
                        if (jar.loader === 'unknown') return [`${jar.filename}: no supported loader metadata was found.`];
                        if (jar.loader !== selectedLoader) return [`${jar.filename}: detected ${jar.loader}; profile uses ${selectedLoader}.`];
                        return [];
                    }));
                    return [...new Set(warnings)];
                },
                addModDownload() { if (!this.profile.mod_downloads) this.profile.mod_downloads = []; if (this.profile.mod_downloads.length < 200) this.profile.mod_downloads.push({ url: '', filename: '', sha256: '', skip_on_start: false }); },
                removeModDownload(index) { if (this.profile.mod_downloads.length > 1) this.profile.mod_downloads.splice(index, 1); },
                serializeModDownloads() { return JSON.stringify((this.profile.mod_downloads || []).filter(item => item.url.trim()).map(item => ({ url: item.url.trim(), filename: item.filename.trim() || null, sha256: item.sha256.trim() || null, skip_on_start: !!item.skip_on_start }))); },
                async clearModDownloadCache() {
                    this.isClearingCache = true;
                    try {
                        const response = await fetch(`/api/profiles/${this.profile.id}/mod-download-cache/clear`, { method: 'POST' });
                        if (response.status === 401) { window.location.assign('/login'); return; }
                        if (!response.ok) throw new Error(await responseMessage(response, 'Could not clear cached mod downloads.'));
                        showToast('Mod download cache cleared. Links will download on the next manual Start or Restart.');
                    } catch (error) { showToast(error.message || 'Could not clear cached mod downloads.'); }
                    finally { this.isClearingCache = false; }
                },
                async fetchLoaders(preserveSelection = false) {
                    const requestId = ++this.loaderRequest;
                    this.loaderPending = false;
                    this.loaderError = '';
                    if (!this.profile.mc_version || this.profile.mc_version.trim().length < 3) { this.loaders = []; this.isLoading = false; return; }
                    if (this.profile.build === 'vanilla') { this.loaders = ['latest']; this.profile.loader_version = 'latest'; this.isLoading = false; return; }
                    this.isLoading = true;
                    try {
                        const query = new URLSearchParams({ build: this.profile.build, mc_version: this.profile.mc_version.trim() });
                        const res = await fetch(`/api/profiles/loaders?${query}`);
                        if (!res.ok) throw new Error(`Loader lookup failed (${res.status}).`);
                        const data = await res.json();
                        if (requestId !== this.loaderRequest) return;
                        this.loaders = data.versions || [];
                        if (preserveSelection && this.profile.loader_version && !this.loaders.includes(this.profile.loader_version)) {
                            this.loaders.unshift(this.profile.loader_version);
                        } else if (!this.loaders.includes(this.profile.loader_version) && this.loaders.length > 0) {
                            this.profile.loader_version = this.loaders[0];
                        }
                        if (!this.loaders.length) this.loaderError = data.error || 'No compatible loader versions were returned.';
                    } catch (e) { if (requestId === this.loaderRequest) { this.loaders = []; this.loaderError = e.message || 'Could not load loader versions.'; } }
                    finally { if (requestId === this.loaderRequest) this.isLoading = false; }
                },
                async saveEdit() {
                    const replacing = this.needsReset();
                    if (replacing && !await window.sshmcConfirm({
                        title: 'Change server version or loader?',
                        message: 'The Git checkout and direct-link cache will be cleared. The Modrinth manifest and cache will also be cleared; a linked mod pack stays attached and must pass source preflight on the next Start.',
                        confirmLabel: 'Save version change', danger: true
                    })) return;
                    const unlinkingPack = Boolean(this.originalModpackName && !this.profile.modpack_name && !replacing);
                    if (unlinkingPack && !await window.sshmcConfirm({
                        title: 'Unlink this mod pack?',
                        message: this.profile.git_url ? 'Git mods/ will become the base mod source again. Git config and datapacks remain active.' : 'The profile will have no base mod source until you connect a Git repository or another pack.',
                        confirmLabel: 'Unlink pack', danger: true
                    })) return;
                    this.isSaving = true;
                    if (replacing) this.profile.modpack_name = this.originalModpackName;
                    if (this.profile.launch_dir && !this.compatibleLaunchDirs().some(directory => directory.name === this.profile.launch_dir)) {
                        this.profile.launch_dir = '';
                    }
                    const formData = new FormData();
                    const fields = ['name', 'build', 'mc_version', 'loader_version', 'git_url', 'modpack_name', 'launch_dir', 'java_path', 'ram_mb', 'port', 'backup_worlds_on_change', 'backup_mods_on_change', 'backup_config_on_change', 'backup_datapacks_on_change', 'backup_server_settings_on_change', 'backup_on_restart', 'max_backups'];
                    for (const key of fields) {
                        const value = this.profile[key];
                        formData.append(key, typeof value === 'boolean' ? String(value) : (value ?? ''));
                    }
                    formData.append('mod_downloads', this.serializeModDownloads());
                    
                    try {
                        const res = await fetch(`/api/profiles/${this.profile.id}`, { method: 'PUT', body: formData });
                        if (res.ok) {
                            const saved = await res.json().catch(() => ({}));
                            this.$dispatch('close-edit');
                            htmx.trigger('#profile-list', 'refresh');
                            htmx.trigger('#stats-bar', 'refresh');
                            showToast(replacing ? 'Profile saved. Git was disconnected; Modrinth sources and the direct-link cache were cleared. Configured links will download again, and the linked pack stays attached for preflight.' : (saved.applies_on_next_restart ? 'Profile saved. Source changes apply on the next restart.' : 'Profile saved. Changes apply on your next manual Start or Restart.'));
                        } else { showToast(await responseMessage(res, 'Could not update profile.')); }
                    } catch (e) { showToast(e.message || 'Could not update profile.'); } finally { this.isSaving = false; }
                }
            }
        }
