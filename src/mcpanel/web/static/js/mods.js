window.modsApp = function modsApp(initialProfileId = '', initialProfileName = '', initialProfileBuild = '', initialProfileMcVersion = '') {
    const hasInitial = Boolean(initialProfileId && initialProfileName);
    const initialProfiles = hasInitial ? [{
        id: initialProfileId,
        name: initialProfileName,
        build: initialProfileBuild,
        mc_version: initialProfileMcVersion
    }] : [];
    const initialProfile = hasInitial ? initialProfiles[0] : null;

    return {
        profiles: initialProfiles,
        profileId: initialProfileId || '',
        profile: initialProfile,
        view: 'browse',
        query: '',
        sort: 'relevance',
        category: '',
        installedQuery: '',
        installedSource: 'all',
        selectedForInstall: [],
        bulkBusy: false,
        planCache: {},
        transitioning: false,
        running: false,
        serverOnly: true,
        categories: [],
        categoriesLoaded: false,
        results: [],
        installed: [],
        installedWarnings: [],
        offset: 0,
        total: 0,
        loading: false,
        error: '',
        detail: null,
        versions: [],
        installing: false,
        jobs: [],
        jobStatuses: {},
        pollingJobs: [],
        dismissedJobs: [],
        queuedProjects: [],
        confirmation: null,
        confirmationResolver: null,
        searchToken: 0,
        installedToken: 0,
        projectToken: 0,
        init() {
            this.refreshProfiles();
            window.addEventListener('sshmc-mods-enter', () => this.refreshProfiles());
            if (window.sshmc && typeof window.sshmc.on === 'function') {
                window.sshmc.on('tab-change', (e) => {
                    if (e.detail?.tab === 'mods') this.refreshProfiles();
                });
                window.sshmc.on('mods-select-profile', (e) => {
                    const pid = e.detail?.profileId;
                    if (pid) {
                        this.profileId = pid;
                        this.selectProfile();
                    }
                    if (e.detail?.view) {
                        this.view = e.detail.view;
                        if (this.view === 'installed') this.loadInstalled();
                    }
                });
            }
        },
        async readJson(response, fallback = 'The server returned an invalid response.') {
            const raw = await response.text();
            try { return raw ? JSON.parse(raw) : {}; }
            catch (_) {
                if (!response.ok) return { detail: `Request failed (${response.status}). Check the panel logs.` };
                throw new Error(fallback);
            }
        },
        async refreshProfiles() {
            const previousId = this.profileId;
            try {
                const response = await fetch('/api/profiles');
                if (!response.ok) throw new Error(response.status === 401 ? 'Your session expired. Reload and sign in again.' : 'Could not refresh profiles.');
                const profiles = await this.readJson(response, 'Could not read the profile list.');
                this.profiles = Array.isArray(profiles) ? profiles : [];
                if (previousId && this.profiles.some(p => p.id === previousId)) {
                    this.profileId = previousId;
                } else if (this.profiles.length > 0) {
                    this.profileId = this.profiles[0].id;
                } else {
                    this.profileId = '';
                }
                this.syncProfile();
                if (this.profileId && this.profile) { this.loadInstalled(); this.search(); this.resumeJobs(this.profileId); }
                else { this.loading = false; this.error = ''; }
            } catch (error) { this.error = error.message || 'Could not load profiles.'; }
            if (!this.categoriesLoaded) this.loadCategories();
        },
        syncProfile() { this.profile = this.profiles.find(p => p.id === this.profileId) || null; this.detail = null; this.versions = []; this.results = []; this.installed = []; this.installedWarnings = []; this.transitioning = false; this.searchToken++; },
        selectProfile() { this.syncProfile(); if (this.profileId && this.profile) { this.loadInstalled(); this.search(); this.resumeJobs(this.profileId); } },
        async loadCategories() { try { const response = await fetch('/api/mods/categories'); if (!response.ok) throw new Error(); this.categories = (await this.readJson(response, 'Could not read Modrinth categories.')).filter(x => x.project_type === 'mod'); this.categoriesLoaded = true; } catch (_) {} },
        async search(append = false, requestedOffset = null) {
            if (!this.profile || this.profile.build === 'vanilla') return;
            const offset = requestedOffset ?? (append ? this.offset : 0);
            if (!append) { this.offset = 0; this.results = []; }
            const token = ++this.searchToken;
            this.loading = true; this.error = '';
            try {
                const query = new URLSearchParams({
                    q: this.query,
                    profile_id: this.profileId,
                    sort: this.sort,
                    category: this.category,
                    offset: String(offset),
                    server_only: String(this.serverOnly)
                });
                const response = await fetch('/api/mods/search?' + query);
                const data = await this.readJson(response, 'Mod search returned an invalid response.'); if (token !== this.searchToken) return; if (!response.ok) throw new Error(data.detail || 'Mod search failed.');
                this.results = append ? [...this.results, ...data.hits] : data.hits; this.total = data.total_hits; return true;
            } catch (error) { if (token === this.searchToken) this.error = error.message; return false; } finally { if (token === this.searchToken) this.loading = false; }
        },
        async loadMore() { const next = this.offset + 20; if (await this.search(true, next)) this.offset = next; },
        async loadInstalled() {
            if (!this.profileId) return;
            const profileId = this.profileId, token = ++this.installedToken;
            try { const response = await fetch(`/api/profiles/${profileId}/mods`); const data = await this.readJson(response, 'Could not read the installed mods list.'); if (!response.ok) throw new Error(data.detail || 'Could not load installed mods.'); if (token === this.installedToken && profileId === this.profileId) { this.installed = data.mods || []; this.installedWarnings = data.warnings || []; this.transitioning = Boolean(data.transitioning); this.running = Boolean(data.running); } }
            catch (error) { if (token === this.installedToken) this.error = error.message; }
        },
        isInstalled(projectId) { return this.installed.some(x => x.project_id === projectId); },
        isQueued(projectId) { return this.queuedProjects.includes(projectId) || this.jobs.some(job => job.profileId === this.profileId && this.jobStatuses[job.id]?.project_id === projectId && ['queued', 'downloading'].includes(this.jobStatuses[job.id]?.status)); },
        planKey(projectId) { return `${this.profileId}:${projectId}`; },
        async loadPlan(projectId) {
            const key = this.planKey(projectId);
            if (this.planCache[key] || !this.profileId) return;
            try {
                const response = await fetch(`/api/profiles/${this.profileId}/mods/${encodeURIComponent(projectId)}/plan`);
                if (!response.ok) { this.planCache[key] = { compatible: false, reason: 'Preview unavailable.', required: [] }; return; }
                this.planCache[key] = await response.json();
            } catch (_) {
                this.planCache[key] = { compatible: false, reason: 'Preview unavailable.', required: [] };
            }
        },
        installPlanTooltip(projectId) {
            const plan = this.planCache[this.planKey(projectId)];
            if (!plan) return 'Loading dependency preview…';
            if (!plan.compatible) return plan.reason || 'Cannot install on this profile.';
            const count = (plan.required || []).length;
            if (!count) return `No required dependencies · ${plan.version_name || ''}`.trim();
            const titles = (plan.required || []).slice(0, 4).map(d => d.title).join(', ');
            const more = plan.required.length > 4 ? ` +${plan.required.length - 4} more` : '';
            return `Adds ${count} required dependencies (${titles}${more})`;
        },
        sourceLabel(source) { return ({ git: 'Git', pack: 'Pack', modrinth: 'Modrinth', link: 'Link', manual: 'Manual' })[source] || 'Manual'; },
        get hasPendingRemovals() { return this.installed.some(m => m.pending_removal); },
        get installedCount() { return this.installed.filter(m => !m.missing_file).length; },
        get filteredInstalled() {
            const source = this.installedSource;
            const query = this.installedQuery.trim().toLowerCase();
            return this.installed.filter(m => {
                if (source !== 'all' && (m.source || 'manual') !== source) return false;
                if (!query) return true;
                const haystack = [m.name, m.filename, m.version, m.project_id, m.sha256].filter(Boolean).join(' ').toLowerCase();
                return query.split(/\s+/).every(part => haystack.includes(part));
            });
        },
        askConfirmation(title, message, details = [], confirmLabel = 'Continue', dependencyPlan = null) {
            this.confirmation = { title, message, details, confirmLabel, dependencyPlan };
            return new Promise(resolve => { this.confirmationResolver = resolve; });
        },
        installedNameFor(projectId) {
            const match = this.installed.find(m => m.project_id === projectId);
            return match ? (match.name || match.filename || projectId) : projectId;
        },
        finishConfirmation(approved) {
            const resolve = this.confirmationResolver; this.confirmationResolver = null; this.confirmation = null;
            if (resolve) resolve(approved);
        },
        dismissJob(id) { if (!this.dismissedJobs.includes(id)) this.dismissedJobs.push(id); },
        async openProject(id) {
            this.error = '';
            const token = ++this.projectToken;
            try {
                const [project, versions] = await Promise.all([
                    fetch(`/api/mods/project/${encodeURIComponent(id)}`).then(r => this.readJson(r, 'Could not read the project details.')),
                    fetch(`/api/mods/project/${encodeURIComponent(id)}/versions?profile_id=${encodeURIComponent(this.profileId)}`).then(r => this.readJson(r, 'Could not read compatible versions.'))
                ]);
                if (token !== this.projectToken) return;
                if (project.detail) throw new Error(project.detail);
                this.detail = project; this.versions = versions;
            } catch (error) { this.error = error.message; }
        },
        async installProject(id) { await this.openProject(id); if (this.versions.length) await this.installVersion(this.versions[0]); },
        async installVersion(version) {
            const key = version.project_id || this.detail?.id || version.id;
            if (this.isInstalled(key) || this.isQueued(key)) return;
            this.installing = true;
            this.queuedProjects.push(key);
            let warnedRunning = false;
            try {
                let response = await fetch(`/api/profiles/${this.profileId}/mods/install`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version_id: version.id }) });
                let data = await this.readJson(response, 'The install request returned an invalid response.');
                if (data.needs_confirmation) {
                    const planInstall = Array.isArray(data.dependencies) ? data.dependencies : [];
                    const planSkip = Array.isArray(data.satisfied) ? data.satisfied : [];
                    const depTotal = planInstall.reduce((sum, d) => sum + (Number(d.size_bytes) || 0), 0);
                    const notes = [];
                    if (data.unsupported_server) notes.push('Warning: this mod marks server-side use as unsupported.');
                    if (data.running) notes.push('This profile is running; installed mods take effect after restart.');
                    const needsDecision = planInstall.length > 0 || data.unsupported_server || data.running;
                    if (needsDecision) {
                        const message = planInstall.length
                            ? 'The following required dependencies will also be installed:'
                            : 'Review this installation before continuing:';
                        if (!await this.askConfirmation(`Install ${version.name || version.version_number}?`, message, notes, 'Install mods', { install: planInstall, skip: planSkip, totalBytes: depTotal })) return;
                        warnedRunning = Boolean(data.running);
                    }
                    response = await fetch(`/api/profiles/${this.profileId}/mods/install`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ version_id: version.id, confirm: true, skip_unsupported: data.unsupported_server }) });
                    data = await this.readJson(response, 'The install request returned an invalid response.');
                }
                if (!response.ok) throw new Error(data.detail || 'Install failed.');
                if (data.running && !warnedRunning && !await this.askConfirmation('Profile is running', 'Mods will download now and become active after the next restart.', [], 'Queue download')) return;
                const job = { id: data.job_id, profileId: this.profileId };
                this.jobs.push(job); this.jobStatuses[data.job_id] = { status: 'queued', project_id: key, profile_id: this.profileId, items: [] }; this.pollJob(job.id, job.profileId); this.notify(data.applies_on_next_restart ? 'Mod download queued. It becomes active on the next restart.' : 'Mod download queued.');
            } catch (error) { this.notify(error.message, true); } finally { this.queuedProjects = this.queuedProjects.filter(id => id !== key); this.installing = false; }
        },
        resumeJobs(profileId) { this.jobs.filter(job => job.profileId === profileId && ['queued', 'downloading'].includes(this.jobStatuses[job.id]?.status)).forEach(job => this.pollJob(job.id, job.profileId)); },
        async pollJob(id, profileId) {
            if (this.pollingJobs.includes(id)) return;
            this.pollingJobs.push(id);
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.requestFastPolling('mod-job-' + id);
            }
            const poll = async () => {
                let response;
                try { response = await fetch(`/api/profiles/${profileId}/mods/jobs/${id}`); }
                catch (_) { window.setTimeout(poll, 2000); return; }
                if (!response.ok) {
                    if (response.status === 404) {
                        this.jobStatuses[id] = { ...this.jobStatuses[id], status: 'failed', error: 'Profile or install job no longer exists.' };
                        this.pollingJobs = this.pollingJobs.filter(jobId => jobId !== id);
                        if (window.sshmc && window.sshmc.poller) {
                            window.sshmc.poller.releaseFastPolling('mod-job-' + id);
                        }
                        return;
                    }
                    window.setTimeout(poll, 2000); return;
                }
                let job;
                try { job = await this.readJson(response, 'The job status response was invalid.'); } catch (_) { window.setTimeout(poll, 2000); return; }
                this.jobStatuses[id] = job;
                if (job.status === 'done' || job.status === 'failed') {
                    this.pollingJobs = this.pollingJobs.filter(jobId => jobId !== id);
                    if (window.sshmc && window.sshmc.poller) {
                        window.sshmc.poller.releaseFastPolling('mod-job-' + id);
                    }
                    if (this.profileId === profileId) this.loadInstalled();
                    this.notify(job.status === 'done' ? 'Mod installation finished.' : (job.error || 'Mod installation failed.'), job.status === 'failed' || job.items?.some(x => x.status === 'failed'));
                    if (job.status === 'done') {
                        window.setTimeout(() => this.dismissJob(id), 3000);
                    }
                    return;
                }
                window.setTimeout(poll, 1000);
            };
            window.setTimeout(poll, 500);
        },
        async removeMod(mod) {
            if (!mod.remove_allowed) return;
            // Check if any other installed mod depends on this one (required dependency)
            const dependents = this.installed.filter(m =>
                m.project_id && m.project_id !== mod.project_id &&
                Array.isArray(m.dependencies) && m.dependencies.some(d =>
                    d.dependency_type === 'required' && d.project_id === mod.project_id
                )
            );
            if (dependents.length) {
                const details = dependents.map(d => `${d.name || d.filename || d.project_id} requires this mod`);
                if (!await this.askConfirmation('Remove required mod?',
                    `${mod.name || mod.filename} is required by other installed mods:`,
                    details, 'Remove anyway')) return;
            } else {
                const serverNote = this.running ? 'The live mods folder will be updated on the next Start or Restart.' : 'The mod file will be removed from server/mods immediately.';
                if (!await this.askConfirmation('Remove mod?', `Remove ${mod.name || mod.filename}? ${serverNote}`, [], 'Remove')) return;
            }
            try {
                const response = await fetch(`/api/profiles/${this.profileId}/mods/${encodeURIComponent(mod.sha256)}`, { method: 'DELETE' });
                const data = await this.readJson(response, 'The remove request returned an invalid response.');
                if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not remove the mod.');
                await this.loadInstalled();
                this.notify(data.applies_on_next_restart ? 'Mod removal queued — it takes effect on the next Start or Restart.' : 'Mod removed.');
            } catch (error) { this.notify(error.message, true); }
        },
        toggleSelected(projectId) {
            if (this.isInstalled(projectId) || this.isQueued(projectId)) return;
            const index = this.selectedForInstall.indexOf(projectId);
            if (index >= 0) this.selectedForInstall.splice(index, 1);
            else this.selectedForInstall.push(projectId);
        },
        clearSelection() { this.selectedForInstall = []; },
        async installSelected() {
            if (this.bulkBusy || !this.selectedForInstall.length) return;
            const ids = [...this.selectedForInstall];
            this.bulkBusy = true;
            try {
                // 1. Gather a plan for every selected mod (no downloads yet)
                const plans = [];
                for (const id of ids) {
                    try {
                        const response = await fetch(`/api/profiles/${this.profileId}/mods/install`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ project_id: id }),
                        });
                        const data = await this.readJson(response, 'The install request returned an invalid response.');
                        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Install failed.');
                        plans.push({ id, data });
                    } catch (error) {
                        this.notify(`${id}: ${error.message || 'Install failed.'}`, true);
                    }
                }
                // 2. Drop unsupported plans (these need the single-mod flow for explicit approval)
                const runnable = plans.filter(p => !p.data.unsupported_server);
                if (plans.length && !runnable.length) {
                    this.notify('All selected mods are marked server-incompatible by their authors and can only be installed individually.', true);
                    return;
                }
                if (plans.length - runnable.length) {
                    this.notify(`${plans.length - runnable.length} selected mod(s) were skipped: server-incompatible. Install them individually to confirm.`, true);
                }
                if (!runnable.length) return;
                // 3. Aggregate dependency plan for the confirmation dialog
                const depMap = new Map();
                const satisfiedSet = new Map();
                for (const plan of runnable) {
                    for (const dep of plan.data.dependencies || []) depMap.set(dep.project_id, dep);
                    if (plan.data.satisfied) for (const s of plan.data.satisfied) satisfiedSet.set(s.project_id, s);
                }
                for (const s of satisfiedSet.keys()) depMap.delete(s);
                const depInstall = [...depMap.values()];
                const depTotal = depInstall.reduce((sum, d) => sum + (Number(d.size_bytes) || 0), 0);
                const notes = [];
                if (runnable.some(p => p.data.running)) notes.push('This profile is running; installed mods take effect after restart.');
                const skipList = [...satisfiedSet.values()];
                const confirmed = await this.askConfirmation(
                    `Install ${runnable.length} mods?`,
                    'The following required dependencies will also be installed:',
                    notes,
                    'Install all',
                    { install: depInstall, skip: skipList, totalBytes: depTotal }
                );
                if (!confirmed) return;
                // 4. Confirm installs sequentially, collecting jobs
                let jobsStarted = 0;
                let jobsFailed = 0;
                for (const plan of runnable) {
                    try {
                        const response = await fetch(`/api/profiles/${this.profileId}/mods/install`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ project_id: plan.id, confirm: true }),
                        });
                        const data = await this.readJson(response, 'The install request returned an invalid response.');
                        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Install failed.');
                        if (data.job_id) {
                            const job = { id: data.job_id, profileId: this.profileId };
                            this.jobs.push(job);
                            this.jobStatuses[data.job_id] = { status: 'queued', project_id: plan.id, profile_id: this.profileId, items: [] };
                            this.pollJob(job.id, job.profileId);
                            jobsStarted += 1;
                        }
                    } catch (error) {
                        jobsFailed += 1;
                        this.notify(`${plan.id}: ${error.message || 'Install failed.'}`, true);
                    }
                }
                this.selectedForInstall = [];
                const runningNote = this.profile && this.running ? ' It takes effect after the next restart.' : '';
                if (jobsStarted) this.notify(`${jobsStarted} mod install(s) queued.${runningNote}`);
                else if (jobsFailed) this.notify('No installs were queued; see the errors above.', true);
            } finally {
                this.bulkBusy = false;
            }
        },
        async copyModList() {
            const profile = this.profile;
            const build = profile ? profile.build : 'unknown';
            const header = [
                `# ${profile ? profile.name : 'Mods'} (${build} ${profile ? profile.mc_version : ''})`,
                `# ${this.installed.length} mods`,
                '',
            ];
            const lines = this.installed.map(mod => {
                const name = mod.name || mod.filename;
                const version = mod.version_number || mod.version || 'unknown version';
                const source = mod.source ? ` [${mod.source}]` : '';
                return `- ${name} ${version}${source}`;
            });
            const text = [...header, ...lines].join('\n');
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(text);
                } else {
                    const area = document.createElement('textarea');
                    area.value = text;
                    area.setAttribute('readonly', '');
                    area.style.position = 'fixed';
                    area.style.opacity = '0';
                    document.body.appendChild(area);
                    area.select();
                    document.execCommand('copy');
                    area.remove();
                }
                this.notify('Mod list copied to the clipboard.');
            } catch (_) {
                this.notify('Could not copy the mod list to the clipboard.', true);
            }
        },
        formatSize(bytes) { if (!bytes) return '0 B'; const units = ['B', 'KiB', 'MiB', 'GiB']; let n = bytes, i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; } return `${n.toFixed(1)} ${units[i]}`; },
        renderDescription(markdown = '') {
            const escape = value => value.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
            let html = escape(String(markdown));
            html = html.replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/\*([^*]+)\*/g, '<em>$1</em>');
            html = html.replace(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
            return html.split(/\n{2,}/).map(block => `<p>${block.replace(/\n/g, '<br>')}</p>`).join('');
        },
        notify(message, error = false) { if (typeof showToast === 'function') showToast(message, error ? 'error' : 'success'); else console.error(message); }
    };
}
