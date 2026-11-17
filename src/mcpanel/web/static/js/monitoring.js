function monitoringApp() {
    return {
        stats: { host: null, disks: [] },
        history: [],
        busy: false,
        perCoreExpanded: false,
        _unsubList: [],

        init() {
            this.refresh();

            // Register tab callback on central poller (runs ONLY while on monitoring tab)
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.registerTabCallback('monitoring', () => this.refresh());
            }

            if (window.sshmc && typeof window.sshmc.on === 'function') {
                this._unsubList.push(
                    window.sshmc.on('profile-list-swapped', () => this.updateServerCards())
                );
            }
        },

        destroy() {
            if (window.sshmc && window.sshmc.poller) {
                window.sshmc.poller.unregisterTabCallback('monitoring');
            }
            this._unsubList.forEach(fn => fn());
            this._unsubList = [];
        },

        async refresh() {
            if (document.hidden || this.busy) return;
            this.busy = true;
            try {
                const [statsResponse, historyResponse] = await Promise.all([
                    fetch('/api/monitoring/stats'),
                    fetch('/api/monitoring/history')
                ]);
                if (statsResponse.status === 401 || historyResponse.status === 401) {
                    window.location.assign('/login');
                    return;
                }
                if (!statsResponse.ok || !historyResponse.ok) return;
                this.stats = await statsResponse.json();
                this.history = (await historyResponse.json()).samples || [];

                if (window.sshmc) {
                    window.sshmc.emit('host-memory-update', {
                        availableBytes: this.stats.host ? this.stats.host.memory.available_bytes : null
                    });
                }
                this.updateServerCards();
            } catch (_) {
                // Keep last successful sample visible
            } finally {
                this.busy = false;
            }
        },

        formatBytes(bytes) {
            if (!Number.isFinite(bytes) || bytes < 0) return '—';
            if (bytes < 1024) return `${bytes} B`;
            const units = ['KiB', 'MiB', 'GiB', 'TiB'];
            let value = bytes;
            let unit = -1;
            do {
                value /= 1024;
                unit++;
            } while (value >= 1024 && unit < units.length - 1);
            return `${value.toFixed(1)} ${units[unit]}`;
        },

        colorThreshold(pct) {
            if (pct >= 90) return 'over-90';
            if (pct >= 70) return 'amber-70-90';
            return 'under-70';
        },

        get cpuCurrent() {
            return this.stats.host?.cpu_percent ?? 0;
        },

        get memoryCurrent() {
            return this.stats.host?.memory?.percent ?? 0;
        },

        get cpuMinMax() {
            if (!this.history.length) return { min: 0, max: 0 };
            const values = this.history.map(s => s.host.cpu_percent || 0);
            return {
                min: Math.min(...values).toFixed(1),
                max: Math.max(...values).toFixed(1)
            };
        },

        get memoryMinMax() {
            if (!this.history.length) return { min: 0, max: 0 };
            const values = this.history.map(s => {
                const total = s.host.memory_total_bytes || 1;
                return (s.host.memory_used_bytes / total) * 100;
            });
            return {
                min: Math.min(...values).toFixed(1),
                max: Math.max(...values).toFixed(1)
            };
        },

        buildSvgSparkline(values, width = 300, height = 54) {
            if (!values || !values.length) {
                return { path: `M 0,${height} L ${width},${height}`, fill: `M 0,${height} L ${width},${height} Z` };
            }
            const n = values.length;
            const points = values.map((val, idx) => {
                const x = n === 1 ? width : (idx * width) / (n - 1);
                const clamped = Math.max(0, Math.min(100, val));
                const y = height - (clamped / 100) * (height - 4) - 2;
                return [x, y];
            });

            const pathD = points.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt[0].toFixed(1)},${pt[1].toFixed(1)}`).join(' ');
            const fillD = `${pathD} L ${width},${height} L 0,${height} Z`;

            return { path: pathD, fill: fillD };
        },

        cpuSparklineData() {
            const vals = this.history.map(s => s.host.cpu_percent || 0);
            return this.buildSvgSparkline(vals);
        },

        memorySparklineData() {
            const vals = this.history.map(s => {
                const total = s.host.memory_total_bytes || 1;
                return (s.host.memory_used_bytes / total) * 100;
            });
            return this.buildSvgSparkline(vals);
        },

        serverSparklineData(profileId, metric = 'cpu') {
            const vals = this.history.map(s => {
                const srv = (s.servers || []).find(x => x.profile_id === profileId);
                if (!srv) return 0;
                if (metric === 'cpu') return srv.cpu_percent_machine || 0;
                const total = this.stats.host?.memory?.total_bytes || 1;
                return ((srv.rss_bytes || 0) / total) * 100;
            });
            return this.buildSvgSparkline(vals, 200, 36);
        },

        updateServerCards() {
            const sizes = new Map((this.stats.profile_sizes || []).map(item => [item.profile_id, item]));
            for (const card of document.querySelectorAll('[data-server-monitor]')) {
                const id = card.dataset.serverMonitor;
                const server = (this.stats.servers || []).find(item => item.profile_id === id);
                const size = sizes.get(id);

                const cpu = server && server.available
                    ? `${server.cpu_percent_one_core.toFixed(1)}% core · ${server.cpu_percent_machine.toFixed(1)}% host`
                    : '—';
                const rss = server && server.available
                    ? `${this.formatBytes(server.rss_bytes)} / ${this.formatBytes(server.ram_allocation_bytes)}`
                    : '—';

                const cpuEl = card.querySelector('[data-monitor-cpu]');
                if (cpuEl) cpuEl.textContent = cpu;
                const rssEl = card.querySelector('[data-monitor-rss]');
                if (rssEl) rssEl.textContent = rss;
                const srvSizeEl = card.querySelector('[data-monitor-server-size]');
                if (srvSizeEl) srvSizeEl.textContent = size ? this.formatBytes(size.server_bytes) : '—';
                const worldSizeEl = card.querySelector('[data-monitor-world-size]');
                if (worldSizeEl) worldSizeEl.textContent = size ? this.formatBytes(size.world_bytes) : '—';
                const backupSizeEl = card.querySelector('[data-monitor-backup-size]');
                if (backupSizeEl) backupSizeEl.textContent = size ? this.formatBytes(size.backups_bytes) : '—';
            }
        }
    };
}
