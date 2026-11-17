window.modPackUploadActions = {

                async handleFiles(files, fromFolder = false) {
                    if (!files.length || this.uploading || this.importing) return;
                    const zips = fromFolder ? [] : files.filter(file => file.name.toLowerCase().endsWith('.zip'));
                    if (zips.length) {
                        if (files.length !== 1 || zips.length !== 1) {
                            this.uploadError = 'Upload a ZIP by itself. JARs and folders can be selected together.';
                            return;
                        }
                        if (this.uploadId || this.uploadSession) await this.discardUpload();
                        this.chooseFile(zips[0]);
                        return;
                    }
                    if (this.uploadId || this.uploadSession) await this.discardUpload();
                    this.importResult = null; this.uploadError = ''; this.conflict = false; this.skippedFiles = [];
                    const records = [];
                    const seen = new Set();
                    for (const file of files) {
                        const raw = file._sshRelativePath || file.webkitRelativePath || file.name;
                        const parts = raw.replaceAll('\\', '/').split('/').filter(part => part && part !== '.');
                        if (parts.some(part => part === '..' || part.includes('..'))) {
                            this.skippedFiles.push(file.name + ' (unsafe path)');
                            continue;
                        }
                        const contentIndex = parts.findIndex(part => part === 'config' || part === 'datapacks');
                        let relativePath = '';
                        if (file.name.toLowerCase().endsWith('.jar')) {
                            relativePath = file.name;
                        } else if (contentIndex >= 0 && contentIndex < parts.length - 1) {
                            relativePath = parts.slice(contentIndex).join('/');
                        } else {
                            this.skippedFiles.push(raw);
                            continue;
                        }
                        const key = (relativePath.toLowerCase().endsWith('.jar') ? 'mods/' + file.name : relativePath).toLowerCase();
                        if (seen.has(key)) {
                            this.skippedFiles.push(raw + ' (duplicate destination)');
                            continue;
                        }
                        seen.add(key);
                        if (file.name.toLowerCase().endsWith('.jar') && file.size > this.maxSingleModBytes) {
                            this.skippedFiles.push(raw + ' (larger than ' + this.formatSize(this.maxSingleModBytes) + ')');
                            continue;
                        }
                        records.push({ file, filename: file.name, relativePath });
                    }
                    if (!records.length) {
                        this.uploadError = this.skippedFiles.length ? 'No supported JARs or config/datapack files were found.' : 'Choose at least one file.';
                        return;
                    }
                    this.uploadSession = crypto.randomUUID().replaceAll('-', '');
                    this.uploadId = '';
                    this.fileUploads = records.map((record, index) => ({
                        ...record, key: index + ':' + record.relativePath, label: record.relativePath,
                        progress: 0, status: 'Queued', error: ''
                    }));
                    this.selectedFilename = records.length + ' files';
                    this.packName = '';
                    this.destinationMode = this.selectedPack ? 'existing' : 'new';
                    this.targetLoader = '';
                    this.uploading = true;
                    let nextIndex = 0;
                    const workers = Array.from({ length: Math.min(3, records.length) }, async () => {
                        while (nextIndex < this.fileUploads.length && this.uploading) {
                            const index = nextIndex++;
                            await this.uploadOneFile(this.fileUploads[index]);
                        }
                    });
                    await Promise.all(workers);
                    this.uploading = false;
                    this.fileXhrs = [];
                    const succeeded = this.fileUploads.filter(file => file.status === 'Uploaded').length;
                    this.selectedFilename = succeeded + ' of ' + records.length + ' files uploaded';
                    if (!succeeded) {
                        this.uploadError = 'No files uploaded successfully. Failed files can be selected again.';
                        await this.discardUpload();
                        return;
                    }
                    if (this.destinationMode === 'existing') this.setTargetFromPack();
                    if (this.fileUploads.some(file => file.status === 'Failed')) {
                        this.uploadError = 'Some files failed after one retry. You can import the uploaded files or cancel and retry the failed files.';
                    }
                },
                async handleDrop(event) {
                    const items = Array.from(event.dataTransfer.items || []);
                    const entries = items.map(item => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
                    if (!entries.length) {
                        await this.handleFiles(Array.from(event.dataTransfer.files || []));
                        return;
                    }
                    const readEntry = entry => new Promise((resolve, reject) => {
                        if (entry.isFile) {
                            entry.file(file => { file._sshRelativePath = entry.fullPath.replace(/^\/+/, ''); resolve([file]); }, reject);
                        } else if (entry.isDirectory) {
                            const reader = entry.createReader();
                            const all = [];
                            const readBatch = () => reader.readEntries(async batch => {
                                if (!batch.length) { resolve((await Promise.all(all)).flat()); return; }
                                all.push(...batch.map(readEntry));
                                readBatch();
                            }, reject);
                            readBatch();
                        } else resolve([]);
                    });
                    try {
                        const files = [];
                        for (const entry of entries) files.push(...await readEntry(entry));
                        await this.handleFiles(files, entries.some(entry => entry.isDirectory));
                    } catch (_) { this.uploadError = 'Could not read the dropped folder. Try the Choose folder button.'; }
                },
                async uploadOneFile(file) {
                    for (let attempt = 0; attempt < 2 && this.uploading; attempt++) {
                        file.status = attempt ? 'Retrying…' : 'Uploading';
                        file.progress = 0;
                        try {
                            await new Promise((resolve, reject) => {
                                const query = new URLSearchParams({ filename: file.filename, relative_path: file.relativePath });
                                const xhr = new XMLHttpRequest();
                                this.fileXhrs.push(xhr);
                                xhr.open('POST', '/api/modpacks/sessions/' + this.uploadSession + '/files?' + query);
                                xhr.withCredentials = true;
                                xhr.setRequestHeader('Content-Type', 'application/octet-stream');
                                xhr.upload.onprogress = event => {
                                    if (event.lengthComputable) file.progress = Math.round(event.loaded / event.total * 100);
                                };
                                xhr.onload = () => {
                                    let payload = {};
                                    try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
                                    if (xhr.status === 401) { window.location.assign('/login'); reject(new Error('Authentication required')); }
                                    else if (xhr.status < 200 || xhr.status >= 300) reject(new Error(typeof payload.detail === 'string' ? payload.detail : 'Upload failed (' + xhr.status + ').'));
                                    else { file.progress = 100; resolve(); }
                                };
                                xhr.onerror = () => reject(new Error('Connection failed.'));
                                xhr.onabort = () => reject(new DOMException('Upload cancelled.', 'AbortError'));
                                xhr.send(file.file);
                            });
                            file.status = 'Uploaded';
                            return;
                        } catch (error) {
                            if (error.name === 'AbortError' || !this.uploading) { file.status = 'Cancelled'; return; }
                            file.error = error.message;
                            if (attempt === 1) file.status = 'Failed';
                        }
                    }
                },
};

if (window.sshmc) {
    window.sshmc.modPackUploadActions = window.modPackUploadActions;
}

