// --- Form Logic (Create Profile) ---
        function formApp() {
            return {
                currentBuild: 'vanilla', currentMcVersion: '', loaders: ['latest'], selectedLoader: 'latest', isLoading: false, loaderPending: false, loaderError: '', isSubmitting: false, loaderRequest: 0,
                modDownloads: [{ url: '', filename: '', sha256: '', skip_on_start: false }],
                addModDownload() { if (this.modDownloads.length < 200) this.modDownloads.push({ url: '', filename: '', sha256: '', skip_on_start: false }); },
                removeModDownload(index) { if (this.modDownloads.length > 1) this.modDownloads.splice(index, 1); },
                serializeModDownloads() { return JSON.stringify(this.modDownloads.filter(item => item.url.trim()).map(item => ({ url: item.url.trim(), filename: item.filename.trim() || null, sha256: item.sha256.trim() || null, skip_on_start: false }))); },
                async fetchLoaders() {
                    const requestId = ++this.loaderRequest;
                    this.loaderPending = false;
                    this.loaderError = '';
                    if (this.currentBuild === 'vanilla') { this.loaders = ['latest']; this.selectedLoader = 'latest'; this.isLoading = false; return; }
                    if (this.currentMcVersion.trim().length < 3) { this.loaders = []; this.selectedLoader = ''; this.isLoading = false; return; }
                    this.isLoading = true;
                    try {
                        const query = new URLSearchParams({ build: this.currentBuild, mc_version: this.currentMcVersion.trim() });
                        const res = await fetch(`/api/profiles/loaders?${query}`);
                        if (!res.ok) throw new Error(`Loader lookup failed (${res.status}).`);
                        const data = await res.json();
                        if (requestId !== this.loaderRequest) return;
                        this.loaders = data.versions || [];
                        this.selectedLoader = this.loaders.length > 0 ? this.loaders[0] : '';
                        if (!this.loaders.length) this.loaderError = data.error || 'No compatible loader versions were returned.';
                    } catch (e) { if (requestId === this.loaderRequest) { this.loaders = []; this.selectedLoader = ''; this.loaderError = e.message || 'Could not load loader versions.'; } }
                    finally { if (requestId === this.loaderRequest) this.isLoading = false; }
                },
                resetForm() { this.currentBuild = 'vanilla'; this.currentMcVersion = ''; this.loaders = ['latest']; this.selectedLoader = 'latest'; this.loaderPending = false; this.loaderError = ''; this.isSubmitting = false; this.modDownloads = [{ url: '', filename: '', sha256: '', skip_on_start: false }]; }
            }
        }
