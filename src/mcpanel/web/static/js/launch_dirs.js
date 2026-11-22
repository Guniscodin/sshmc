// --- Imported launch directories (whole server pack folders) ---
window.launchDirsApp = function launchDirsApp() {
    return {
        directories: [],
        loading: true,
        uploading: false,
        importing: false,
        dragging: false,
        error: '',
        conflict: false,
        label: '',
        name: '',
        mcVersion: '',
        build: 'fabric',
        selectedFiles: [],
        selectedCount: 0,
        uploadSession: '',
        uploadedCount: 0,
        uploadError: '',
        result: null,
        maxUploadBytes: 4 * 1024 ** 3,

        init() {
            this.load();
        },

        async load() {
            this.loading = true;
            try {
                const response = await fetch('/api/launch-dirs');
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not load launch directories.'));
                const data = await response.json();
                this.directories = data.directories || [];
                this.maxUploadBytes = data.max_upload_bytes || this.maxUploadBytes;
            } catch (error) {
                this.error = error.message || 'Could not load launch directories.';
            } finally {
                this.loading = false;
            }
        },

        pickFolder() {
            this.$refs.launchFolderPicker.click();
        },

        resetImport() {
            this.uploading = false;
            this.importing = false;
            this.dragging = false;
            this.error = '';
            this.conflict = false;
            this.selectedFiles = [];
            this.selectedCount = 0;
            this.uploadSession = '';
            this.uploadedCount = 0;
            this.uploadError = '';
            this.result = null;
        },

        folderNameFrom(files) {
            const first = files[0] && (files[0]._sshRelativePath || files[0].webkitRelativePath || files[0].name);
            if (!first) return '';
            const parts = String(first).replaceAll('\\', '/').split('/').filter(Boolean);
            return parts.length > 1 ? parts[0] : '';
        },

        shouldSkip(relativePath) {
            const parts = relativePath.replaceAll('\\', '/').split('/').filter(Boolean);
            if (!parts.length) return true;
            const knownRoots = ['mods', 'config', 'datapacks', 'defaultconfigs', 'kubejs', 'scripts', 'resourcepacks', 'shaderpacks', 'structures'];
            const checkIndex = (parts.length > 1 && !knownRoots.includes(parts[0].toLowerCase())) ? 1 : 0;
            const top = (parts[checkIndex] || '').toLowerCase();
            const excludedDirs = ['logs', 'crash-reports', 'libraries', 'versions', '.git', '.fabric', 'downloaded_mods'];
            if (excludedDirs.includes(top) || top === 'world' || top.startsWith('world_') || top.startsWith('.')) return true;
            const file = parts[parts.length - 1].toLowerCase();
            const excludedFiles = [
                'server.jar', 'run.sh', 'run.bat', 'user_jvm_args.txt', 'eula.txt',
                'server.properties', 'profile_meta.json', 'installer.jar', 'installer.jar.log',
                'banned-ips.json', 'banned-players.json', 'ops.json', 'whitelist.json', 'usercache.json'
            ];
            if (excludedFiles.includes(file) && (checkIndex === 0 ? parts.length === 1 : parts.length === 2)) return true;
            return false;
        },

        async handleDrop(event) {
            const items = Array.from(event.dataTransfer?.items || []);
            const entries = items.map(item => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
            if (!entries.length) {
                await this.handleFiles(Array.from(event.dataTransfer?.files || []));
                return;
            }
            const readEntry = entry => new Promise((resolve, reject) => {
                if (entry.isFile) {
                    entry.file(file => {
                        file._sshRelativePath = entry.fullPath.replace(/^\/+/, '');
                        resolve([file]);
                    }, reject);
                } else if (entry.isDirectory) {
                    const reader = entry.createReader();
                    const all = [];
                    const readBatch = () => reader.readEntries(async batch => {
                        if (!batch.length) {
                            resolve((await Promise.all(all)).flat());
                            return;
                        }
                        all.push(...batch.map(readEntry));
                        readBatch();
                    }, reject);
                    readBatch();
                } else {
                    resolve([]);
                }
            });
            try {
                const files = (await Promise.all(entries.map(readEntry))).flat();
                await this.handleFiles(files);
            } catch (error) {
                this.error = error.message || 'Could not read dropped folder.';
            }
        },

        async handleFiles(files) {
            if (!files.length || this.uploading || this.importing) return;
            this.resetImport();
            const records = [];
            const seen = new Set();
            for (const file of files) {
                const raw = (file._sshRelativePath || file.webkitRelativePath || file.name).replaceAll('\\', '/');
                const parts = raw.split('/').filter(part => part && part !== '.');
                if (!parts.length || parts.some(part => part === '..' || part.includes('..'))) continue;
                const relativePath = parts.join('/');
                if (this.shouldSkip(relativePath)) continue;
                const key = relativePath.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                records.push({ file, filename: file.name, relativePath });
            }
            if (!records.length) {
                this.error = 'No importable server files were found. World saves, logs, and server runtime files are excluded.';
                return;
            }
            const folderName = this.folderNameFrom(files);
            this.label = folderName || '';
            this.name = (folderName || '').replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '').slice(0, 64);
            this.selectedFiles = records;
            this.selectedCount = records.length;
            this.uploadSession = crypto.randomUUID().replaceAll('-', '');
            this.uploading = true;
            let next = 0;
            const workers = Array.from({ length: Math.min(3, records.length) }, async () => {
                while (next < records.length && this.uploading) {
                    const index = next++;
                    await this.uploadOne(records[index]);
                }
            });
            await Promise.all(workers);
            this.uploading = false;
            if (this.uploadError && this.uploadedCount === 0) {
                this.error = this.uploadError;
            }
        },

        async uploadOne(record) {
            const query = new URLSearchParams({
                filename: record.filename,
                relative_path: record.relativePath,
                kind: 'launch',
            });
            for (let attempt = 0; attempt < 2 && this.uploading; attempt++) {
                try {
                    await new Promise((resolve, reject) => {
                        const xhr = new XMLHttpRequest();
                        xhr.open('POST', '/api/modpacks/sessions/' + this.uploadSession + '/files?' + query);
                        xhr.withCredentials = true;
                        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                        xhr.onload = () => {
                            let payload = {};
                            try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
                            if (xhr.status === 401) { window.location.assign('/login'); reject(new Error('Authentication required')); }
                            else if (xhr.status < 200 || xhr.status >= 300) reject(new Error(typeof payload.detail === 'string' ? payload.detail : 'Upload failed (' + xhr.status + ').'));
                            else resolve();
                        };
                        xhr.onerror = () => reject(new Error('Connection failed.'));
                        xhr.send(record.file);
                    });
                    this.uploadedCount += 1;
                    return;
                } catch (error) {
                    if (attempt === 1) this.uploadError = error.message || 'Upload failed.';
                }
            }
        },

        async importDirectory(replace = false) {
            if (this.importing || this.uploading) return;
            this.error = '';
            this.conflict = false;
            if (!this.name.trim()) { this.error = 'Enter a directory name.'; return; }
            if (!this.mcVersion.trim()) { this.error = 'Enter the Minecraft version this pack targets.'; return; }
            this.importing = true;
            try {
                const response = await fetch('/api/launch-dirs/import', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        session_id: this.uploadSession,
                        name: this.name.trim(),
                        label: (this.label || '').trim() || undefined,
                        mc_version: this.mcVersion.trim(),
                        build: this.build,
                        replace,
                    }),
                });
                const payload = await response.json().catch(() => ({}));
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (response.status === 409) {
                    const detail = payload.detail;
                    this.error = typeof detail === 'string' ? detail : (detail && detail.message) || 'Directory already exists.';
                    if (detail && detail.code === 'launch_dir_exists') this.conflict = true;
                    return;
                }
                if (!response.ok) {
                    throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Could not import this directory.');
                }
                this.result = payload.directory;
                this.uploadSession = '';
                this.selectedFiles = [];
                this.selectedCount = 0;
                this.uploadedCount = 0;
                this.name = '';
                this.label = '';
                this.mcVersion = '';
                await this.load();
            } catch (error) {
                this.error = error.message || 'Could not import this directory.';
            } finally {
                this.importing = false;
            }
        },

        async discardUpload() {
            if (this.uploadSession) {
                try { await fetch('/api/modpacks/sessions/' + this.uploadSession, { method: 'DELETE' }); } catch (_) {}
            }
            this.resetImport();
        },

        async deleteDirectory(directory) {
            if (directory.assigned_profile_id) {
                this.error = `'${directory.label || directory.name}' is used by profile '${directory.assigned_profile_name}'. Reset that profile first.`;
                return;
            }
            if (!await window.sshmcConfirm({
                title: 'Delete launch directory?',
                message: `This permanently removes the imported folder '${directory.label || directory.name}' from disk.`,
                confirmLabel: 'Delete directory',
                danger: true,
            })) return;
            this.error = '';
            try {
                const response = await fetch('/api/launch-dirs/' + encodeURIComponent(directory.name), { method: 'DELETE' });
                if (response.status === 401) { window.location.assign('/login'); return; }
                if (!response.ok) throw new Error(await responseMessage(response, 'Could not delete the directory.'));
                await this.load();
            } catch (error) {
                this.error = error.message || 'Could not delete the directory.';
            }
        },

        formatBytes(bytes) {
            const value = Number(bytes) || 0;
            if (value >= 1024 ** 3) return (value / 1024 ** 3).toFixed(1) + ' GiB';
            if (value >= 1024 ** 2) return (value / 1024 ** 2).toFixed(1) + ' MiB';
            if (value >= 1024) return (value / 1024).toFixed(1) + ' KiB';
            return value + ' B';
        },
    };
};

if (window.sshmc) {
    window.sshmc.launchDirsApp = window.launchDirsApp;
}
