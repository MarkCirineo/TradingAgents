/**
 * TradingAgents Dashboard — Logs Page
 *
 * Displays systemd journal logs for the daemon and dashboard services.
 * Auto-refreshes every 15 seconds.
 *
 * Accessed via /logs
 */

const LogsPage = {
    _refreshTimer: null,
    _autoScroll: true,

    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'System Logs';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Live journal output from daemon and dashboard services';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        // Controls bar
        const controls = document.createElement('div');
        controls.className = 'fade-in';
        controls.style.cssText = 'display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap;';

        // Line count selector
        const lineLabel = document.createElement('span');
        lineLabel.style.cssText = 'color: var(--text-secondary); font-size: 0.8rem;';
        lineLabel.textContent = 'Lines:';
        controls.appendChild(lineLabel);

        const lineSelect = document.createElement('select');
        lineSelect.id = 'log-line-count';
        lineSelect.style.cssText = 'background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); padding: 4px 8px; font-size: 0.8rem; font-family: var(--font-family);';
        [100, 200, 500, 1000].forEach(n => {
            const opt = document.createElement('option');
            opt.value = n;
            opt.textContent = n;
            if (n === 200) opt.selected = true;
            lineSelect.appendChild(opt);
        });
        controls.appendChild(lineSelect);

        // Refresh button
        const refreshBtn = document.createElement('button');
        refreshBtn.className = 'btn btn--sm';
        refreshBtn.style.cssText = 'background: var(--primary-dim); color: var(--primary); border: 1px solid var(--glass-border); border-radius: var(--radius-sm); padding: 4px 12px; font-size: 0.8rem; cursor: pointer;';
        refreshBtn.textContent = '↻ Refresh';
        refreshBtn.addEventListener('click', () => this._loadLogs());
        controls.appendChild(refreshBtn);

        // Auto-scroll toggle
        const scrollLabel = document.createElement('label');
        scrollLabel.style.cssText = 'display: flex; align-items: center; gap: 6px; color: var(--text-secondary); font-size: 0.8rem; cursor: pointer; margin-left: auto;';
        const scrollCheck = document.createElement('input');
        scrollCheck.type = 'checkbox';
        scrollCheck.checked = true;
        scrollCheck.addEventListener('change', (e) => { this._autoScroll = e.target.checked; });
        scrollLabel.appendChild(scrollCheck);
        scrollLabel.appendChild(document.createTextNode('Auto-scroll'));
        controls.appendChild(scrollLabel);

        // Auto-refresh indicator
        const refreshInfo = document.createElement('span');
        refreshInfo.style.cssText = 'color: var(--text-muted); font-size: 0.75rem;';
        refreshInfo.textContent = 'Auto-refresh: 15s';
        controls.appendChild(refreshInfo);

        container.appendChild(controls);

        // Log tabs
        const tabs = document.createElement('div');
        tabs.className = 'fade-in';
        tabs.style.cssText = 'display: flex; gap: 4px; margin-bottom: 8px;';

        const daemonTab = document.createElement('button');
        daemonTab.id = 'tab-daemon';
        daemonTab.className = 'log-tab log-tab--active';
        daemonTab.textContent = 'Daemon';
        daemonTab.addEventListener('click', () => this._switchTab('daemon'));

        const dashTab = document.createElement('button');
        dashTab.id = 'tab-dashboard';
        dashTab.className = 'log-tab';
        dashTab.textContent = 'Dashboard';
        dashTab.addEventListener('click', () => this._switchTab('dashboard'));

        tabs.appendChild(daemonTab);
        tabs.appendChild(dashTab);
        container.appendChild(tabs);

        // Log output
        const logContainer = document.createElement('div');
        logContainer.className = 'fade-in';
        logContainer.id = 'log-container';

        const daemonPre = document.createElement('pre');
        daemonPre.id = 'daemon-log-output';
        daemonPre.className = 'log-output';
        daemonPre.textContent = 'Loading logs...';

        const dashPre = document.createElement('pre');
        dashPre.id = 'dashboard-log-output';
        dashPre.className = 'log-output';
        dashPre.style.display = 'none';
        dashPre.textContent = 'Loading logs...';

        logContainer.appendChild(daemonPre);
        logContainer.appendChild(dashPre);
        container.appendChild(logContainer);

        // Load initial data
        await this._loadLogs();

        // Auto-refresh every 15 seconds
        this._refreshTimer = setInterval(() => this._loadLogs(), 15000);

        // Line count change handler
        lineSelect.addEventListener('change', () => this._loadLogs());
    },

    destroy() {
        if (this._refreshTimer) {
            clearInterval(this._refreshTimer);
            this._refreshTimer = null;
        }
    },

    _switchTab(tab) {
        const daemonPre = document.getElementById('daemon-log-output');
        const dashPre = document.getElementById('dashboard-log-output');
        const daemonTab = document.getElementById('tab-daemon');
        const dashTab = document.getElementById('tab-dashboard');

        if (tab === 'daemon') {
            daemonPre.style.display = 'block';
            dashPre.style.display = 'none';
            daemonTab.classList.add('log-tab--active');
            dashTab.classList.remove('log-tab--active');
        } else {
            daemonPre.style.display = 'none';
            dashPre.style.display = 'block';
            daemonTab.classList.remove('log-tab--active');
            dashTab.classList.add('log-tab--active');
        }
    },

    async _loadLogs() {
        const lineCount = document.getElementById('log-line-count')?.value || 200;

        try {
            const resp = await fetch(`/api/logs?lines=${lineCount}`);
            const data = await resp.json();

            const daemonPre = document.getElementById('daemon-log-output');
            const dashPre = document.getElementById('dashboard-log-output');

            if (daemonPre) {
                daemonPre.textContent = '';
                if (data.daemon_logs && data.daemon_logs.length > 0) {
                    data.daemon_logs.forEach(line => {
                        const span = document.createElement('span');
                        span.className = this._getLogClass(line);
                        span.textContent = line + '\n';
                        daemonPre.appendChild(span);
                    });
                } else {
                    daemonPre.textContent = 'No logs available';
                }

                if (this._autoScroll) {
                    daemonPre.scrollTop = daemonPre.scrollHeight;
                }
            }

            if (dashPre) {
                dashPre.textContent = '';
                if (data.dashboard_logs && data.dashboard_logs.length > 0) {
                    data.dashboard_logs.forEach(line => {
                        const span = document.createElement('span');
                        span.className = this._getLogClass(line);
                        span.textContent = line + '\n';
                        dashPre.appendChild(span);
                    });
                } else {
                    dashPre.textContent = 'No logs available';
                }

                if (this._autoScroll) {
                    dashPre.scrollTop = dashPre.scrollHeight;
                }
            }

            // Update tab labels with service names
            const daemonTab = document.getElementById('tab-daemon');
            const dashTab = document.getElementById('tab-dashboard');
            if (daemonTab) daemonTab.textContent = `Daemon (${data.service || '?'})`;
            if (dashTab) dashTab.textContent = `Dashboard (${data.dashboard_service || '?'})`;

        } catch (err) {
            console.error('Failed to load logs:', err);
            const daemonPre = document.getElementById('daemon-log-output');
            if (daemonPre) daemonPre.textContent = 'Failed to load logs — API error';
        }
    },

    _getLogClass(line) {
        if (line.includes('[ERROR]') || line.includes('ERROR')) return 'log-line--error';
        if (line.includes('[WARNING]') || line.includes('WARNING')) return 'log-line--warning';
        if (line.includes('[INFO]') || line.includes('INFO')) return 'log-line--info';
        return 'log-line';
    },
};

window.LogsPage = LogsPage;
