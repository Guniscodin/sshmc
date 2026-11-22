function modPacksApp() {
            return {
                packs: [], loadingPacks: true, uploadLimit: '4 GiB', maxUploadBytes: 4 * 1024 ** 3,
                maxSingleModBytes: 1024 ** 3, uploading: false, importing: false, uploadProgress: 0,
                uploadId: '', uploadSession: '', selectedFilename: '', packName: '', targetLoader: '',
                destinationMode: 'new', selectedPack: '', fileUploads: [], skippedFiles: [], fileXhrs: [],
                uploadError: '', importResult: null, conflict: false, dragging: false, xhr: null, importController: null,
                async init() { await this.loadPacks(); },
                async loadPacks() {
                    this.loadingPacks = true;
                    try {
                        const response = await fetch('/api/modpacks');
                        if (response.status === 401) { window.location.assign('/login'); return; }
                        if (!response.ok) throw new Error(await responseMessage(response, 'Could not load mod packs.'));
                        const data = await response.json();
                        this.packs = data.packs || [];
                        const bytes = data.max_upload_bytes || 4 * 1024 ** 3;
                        this.maxUploadBytes = bytes;
                        this.maxSingleModBytes = data.max_single_mod_upload_bytes || 1024 ** 3;
                        this.uploadLimit = bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GiB` : `${Math.round(bytes / 1024 ** 2)} MiB`;
                    } catch (error) { showToast(error.message || 'Could not load mod packs.'); }
                    finally { this.loadingPacks = false; }
                },
                clearSkippedFiles() { this.skippedFiles = []; },
                ...window.modPackUploadActions,
                batchProgress() {
                    const total = this.fileUploads.reduce((sum, file) => sum + file.file.size, 0);
                    if (!total) return 0;
                    return Math.round(this.fileUploads.reduce((sum, file) => sum + file.file.size * file.progress / 100, 0) / total * 100);
                },
                formatSize(bytes) {
                    if (!Number.isFinite(bytes) || bytes < 0) return '—';
                    if (bytes < 1024) return bytes + ' B';
                    const units = ['KiB', 'MiB', 'GiB', 'TiB'];
                    let value = bytes, unit = -1;
                    do { value /= 1024; unit++; } while (value >= 1024 && unit < units.length - 1);
                    return value.toFixed(1) + ' ' + units[unit];
                },
                setTargetFromPack() {
                    if (this.destinationMode !== 'existing' || !this.selectedPack) return;
                    const pack = this.packs.find(item => item.name === this.selectedPack);
                    if (pack) this.targetLoader = pack.target_loader;
                },
                selectPackForAdd(name) {
                    this.destinationMode = 'existing';
                    this.selectedPack = name;
                    this.setTargetFromPack();
                    this.$refs.multiPicker.click();
                },
                selectPackForAddFolder(name) {
                    this.destinationMode = 'existing';
                    this.selectedPack = name;
                    this.setTargetFromPack();
                    this.$refs.folderPicker.click();
                },
                chooseFile(file) {
                    if (!file || this.uploading || this.importing) return;
                    if (!file.name.toLowerCase().endsWith('.zip')) { this.uploadError = 'Choose a .zip archive.'; return; }
                    if (file.size > this.maxUploadBytes) { this.uploadError = `This ZIP is larger than the ${this.uploadLimit} upload limit.`; return; }
                    if (this.uploadId) fetch(`/api/modpacks/uploads/${this.uploadId}`, { method: 'DELETE' }).catch(() => {});
                    this.importResult = null; this.uploadError = ''; this.conflict = false;
                    this.uploadId = crypto.randomUUID().replaceAll('-', '');
                    this.uploadSession = '';
                    this.fileUploads = []; this.skippedFiles = [];
                    this.destinationMode = 'new'; this.selectedPack = '';
                    this.selectedFilename = file.name;
                    this.packName = file.name.replace(/\.zip$/i, '').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
                    this.targetLoader = '';
                    this.uploading = true; this.uploadProgress = 0;
                    const xhr = new XMLHttpRequest();
                    this.xhr = xhr;
                    xhr.open('POST', '/api/modpacks/uploads');
                    xhr.withCredentials = true;
                    xhr.setRequestHeader('Content-Type', 'application/zip');
                    xhr.setRequestHeader('X-Upload-Filename', file.name.replace(/[^A-Za-z0-9._-]/g, '_'));
                    xhr.setRequestHeader('X-Upload-ID', this.uploadId);
                    xhr.upload.onprogress = event => { if (event.lengthComputable) this.uploadProgress = Math.round(event.loaded / event.total * 100); };
                    xhr.onload = async () => {
                        this.uploading = false; this.xhr = null;
                        let payload = {};
                        try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
                        if (xhr.status === 401) { window.location.assign('/login'); return; }
                        if (xhr.status < 200 || xhr.status >= 300) {
                            this.uploadError = typeof payload.detail === 'string' ? payload.detail : `Upload failed (${xhr.status}).`;
                            this.uploadId = '';
                            return;
                        }
                        this.uploadId = payload.upload_id;
                    };
                    xhr.onerror = () => {
                        this.uploading = false; this.xhr = null;
                        if (this.uploadId) fetch(`/api/modpacks/uploads/${this.uploadId}`, { method: 'DELETE' }).catch(() => {});
                        this.uploadId = ''; this.uploadError = 'Upload failed. Check the connection and try again.';
                    };
                    xhr.onabort = () => { this.uploading = false; this.xhr = null; };
                    xhr.send(file);
                },
                async cancelUpload() {
                    const uploadId = this.uploadId;
                    const sessionId = this.uploadSession;
                    if (this.xhr) this.xhr.abort();
                    for (const xhr of this.fileXhrs) xhr.abort();
                    if (this.importController) this.importController.abort();
                    this.uploading = false;
                    if (uploadId) fetch(`/api/modpacks/uploads/${uploadId}`, { method: 'DELETE' }).catch(() => {});
                    if (sessionId) fetch(`/api/modpacks/sessions/${sessionId}`, { method: 'DELETE' }).catch(() => {});
                    this.uploadId = ''; this.uploadSession = ''; this.fileXhrs = []; this.fileUploads = [];
                    this.selectedFilename = ''; this.uploadError = ''; this.importing = false; this.importController = null;
                },
                async discardUpload() {
                    if (this.uploadId) await fetch(`/api/modpacks/uploads/${this.uploadId}`, { method: 'DELETE' }).catch(() => {});
                    if (this.uploadSession) await fetch(`/api/modpacks/sessions/${this.uploadSession}`, { method: 'DELETE' }).catch(() => {});
                    this.uploadId = ''; this.uploadSession = ''; this.selectedFilename = '';
                    this.fileUploads = []; this.fileXhrs = []; this.uploadError = ''; this.conflict = false;
                },
                async importPack(replace = false, replaceFiles = false) {
                    if (!this.uploadId && !this.uploadSession) return;
                    this.importing = true; this.uploadError = ''; this.conflict = false;
                    const controller = new AbortController();
                    this.importController = controller;
                    try {
                        const addToExisting = !!this.uploadSession && this.destinationMode === 'existing';
                        const url = this.uploadId ? '/api/modpacks/import' : (addToExisting ? '/api/modpacks/' + encodeURIComponent(this.selectedPack) + '/files' : '/api/modpacks/import-files');
                        const body = this.uploadId
                            ? { upload_id: this.uploadId, name: this.packName, target_loader: this.targetLoader, replace }
                            : (addToExisting
                                ? { session_id: this.uploadSession, replace_filenames: replaceFiles }
                                : { session_id: this.uploadSession, name: this.packName, target_loader: this.targetLoader, replace });
                        const response = await fetch(url, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(body),
                            signal: controller.signal
                        });
                        if (response.status === 401) { window.location.assign('/login'); return; }
                        const payload = await response.json().catch(() => ({}));
                        if (response.status === 409 && payload.detail?.code === 'pack_exists') {
                            this.conflict = true; this.uploadError = payload.detail.message; return;
                        }
                        if (response.status === 409 && payload.detail?.code === 'jar_conflicts') {
                            const names = (payload.detail.filenames || []).join(', ');
                            if (await window.sshmcConfirm({ title: 'Replace existing JARs?', message: `Replace same-named files in ${this.selectedPack}?`, details: (payload.detail.filenames || []), confirmLabel: 'Replace', danger: true })) {
                                this.importing = false;
                                return await this.importPack(false, true);
                            }
                            this.uploadError = 'Existing JARs were left unchanged.';
                            return;
                        }
                        if (!response.ok) {
                            this.uploadError = typeof payload.detail === 'string' ? payload.detail : `Import failed (${response.status}).`;
                            return;
                        }
                        this.importResult = payload.pack;
                        const wasAdding = addToExisting;
                        this.uploadId = ''; this.uploadSession = ''; this.selectedFilename = ''; this.packName = '';
                        this.destinationMode = 'new'; this.selectedPack = '';
                        this.fileUploads = []; this.fileXhrs = []; this.skippedFiles = [];
                        await this.loadPacks();
                        htmx.trigger('#profile-list', 'refresh');
                        showToast(wasAdding ? `Updated mod pack ${payload.pack.name}; linked profiles apply it on their next Start or Restart.` : `Imported mod pack ${payload.pack.name}. Linked profiles apply it on their next Start or Restart.`);
                    } catch (error) { if (error.name !== 'AbortError') this.uploadError = error.message || 'Could not import this mod pack.'; }
                    finally { this.importing = false; if (this.importController === controller) this.importController = null; }
                },
                async deletePackJar(packName, filename) {
                    if (!await window.sshmcConfirm({ title: 'Delete mod file?', message: `Delete ${filename} from ${packName}? Linked profiles receive this change on their next Start or Restart.`, confirmLabel: 'Delete', danger: true })) return;
                    try {
                        const response = await fetch(`/api/modpacks/${encodeURIComponent(packName)}/jars/${encodeURIComponent(filename)}`, { method: 'DELETE' });
                        if (response.status === 401) { window.location.assign('/login'); return; }
                        if (!response.ok) throw new Error(await responseMessage(response, 'Could not delete this JAR.'));
                        await this.loadPacks();
                        htmx.trigger('#profile-list', 'refresh');
                        showToast(`Removed ${filename} from ${packName}. Linked profiles update on their next Start or Restart.`);
                    } catch (error) { showToast(error.message || 'Could not delete this JAR.'); }
                },
                async deletePack(name) {
                    if (!await window.sshmcConfirm({ title: 'Delete mod pack?', message: `Delete ${name}? Linked profiles will be unlinked. If a profile has a Git repository, Git mods/ becomes its base source again on the next Start or Restart.`, confirmLabel: 'Delete', danger: true })) return;
                    try {
                        const response = await fetch(`/api/modpacks/${encodeURIComponent(name)}`, { method: 'DELETE' });
                        if (response.status === 401) { window.location.assign('/login'); return; }
                        const data = response.ok ? await response.json().catch(() => ({})) : null;
                        if (!response.ok) throw new Error(await responseMessage(response, 'Could not delete mod pack.'));
                        await this.loadPacks();
                        htmx.trigger('#profile-list', 'refresh');
                        showToast(data?.running_profiles?.length ? `Deleted ${name}. It is unlinked now and running profiles apply the change on their next restart.` : `Deleted mod pack ${name}.`);
                    } catch (error) { showToast(error.message || 'Could not delete mod pack.'); }
                }
            }
        }
