/**
 * TradingAgents Dashboard — SPA Router & SSE Connection
 *
 * History API client-side routing (/dashboard, /orders, etc.)
 * and Server-Sent Events connection for real-time data push.
 *
 * Clean URLs — no # in the address bar.
 */

const App = {
    /** Current page instance (for cleanup). */
    _currentPage: null,
    /** SSE EventSource instance. */
    _eventSource: null,
    /** Registered page modules. */
    _pages: {},

    /**
     * Initialise the app: set up routing, SSE, and render first page.
     */
    init() {
        // Register page modules
        this._pages = {
            'dashboard': DashboardPage,
            // Phase 3+: orders, position detail, alpaca, compare, config
        };

        // Intercept nav link clicks for client-side routing
        document.addEventListener('click', (e) => {
            const link = e.target.closest('a[data-page]');
            if (link) {
                e.preventDefault();
                const href = link.getAttribute('href');
                if (href !== window.location.pathname) {
                    history.pushState(null, '', href);
                    this._onRouteChange();
                }
            }
        });

        // Handle browser back/forward
        window.addEventListener('popstate', () => this._onRouteChange());

        // Connect SSE
        this._connectSSE();

        // Initial route
        this._onRouteChange();

        console.log('[TradingAgents] Dashboard initialised');
    },

    // ── Routing ───────────────────────────────────────────────

    /**
     * Parse the current pathname and render the appropriate page.
     */
    _onRouteChange() {
        const path = window.location.pathname;
        const segments = path.split('/').filter(Boolean); // e.g. '/orders' → ['orders']
        const pageName = segments[0] || 'dashboard';
        const params = segments.slice(1); // e.g. '/positions/NVDA' → ['NVDA']

        // Update nav link highlighting
        document.querySelectorAll('.topbar__nav-link').forEach(link => {
            const linkPage = link.dataset.page;
            link.classList.toggle('active', linkPage === pageName);
        });

        // Cleanup current page
        if (this._currentPage && typeof this._currentPage.destroy === 'function') {
            this._currentPage.destroy();
        }

        // Get the app container
        const container = document.getElementById('app');

        // Find and render the page
        const page = this._pages[pageName];
        if (page) {
            this._currentPage = page;
            page.render(container, ...params);
        } else {
            // Page not yet implemented — show placeholder
            container.innerHTML = '';
            const placeholder = document.createElement('div');
            placeholder.className = 'fade-in';
            placeholder.style.cssText = 'display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh;';

            const icon = document.createElement('div');
            icon.style.cssText = 'font-size: 3rem; margin-bottom: 16px; opacity: 0.3;';
            icon.textContent = '\u{1F6A7}';
            placeholder.appendChild(icon);

            const title = document.createElement('h2');
            title.style.cssText = 'color: var(--text-secondary); font-size: 1.25rem; margin-bottom: 8px;';
            title.textContent = `${pageName.charAt(0).toUpperCase() + pageName.slice(1)} Page`;
            placeholder.appendChild(title);

            const msg = document.createElement('p');
            msg.style.cssText = 'color: var(--text-muted); font-size: 0.875rem;';
            msg.textContent = 'Coming in Phase 3+';
            placeholder.appendChild(msg);

            container.appendChild(placeholder);
        }
    },

    // ── SSE (Server-Sent Events) ──────────────────────────────

    /**
     * Connect to the SSE stream and wire up event handlers.
     */
    _connectSSE() {
        if (this._eventSource) {
            this._eventSource.close();
        }

        const es = new EventSource('/api/stream');
        this._eventSource = es;

        // Connection status
        es.onopen = () => {
            console.log('[SSE] Connected');
            this._updateDaemonStatus('running');
        };

        es.onerror = (err) => {
            console.warn('[SSE] Connection error — will auto-reconnect', err);
            this._updateDaemonStatus('loading');
        };

        // ── Event handlers ────────────────────────────────────

        es.addEventListener('portfolio_update', (e) => {
            try {
                const data = JSON.parse(e.data);
                this._onPortfolioUpdate(data);
            } catch (err) {
                console.error('[SSE] Failed to parse portfolio_update:', err);
            }
        });

        es.addEventListener('positions_update', (e) => {
            try {
                const data = JSON.parse(e.data);
                this._onPositionsUpdate(data);
            } catch (err) {
                console.error('[SSE] Failed to parse positions_update:', err);
            }
        });

        es.addEventListener('daemon_status', (e) => {
            try {
                const data = JSON.parse(e.data);
                this._onDaemonStatus(data);
            } catch (err) {
                console.error('[SSE] Failed to parse daemon_status:', err);
            }
        });

        es.addEventListener('clock_update', (e) => {
            try {
                const data = JSON.parse(e.data);
                this._onClockUpdate(data);
            } catch (err) {
                console.error('[SSE] Failed to parse clock_update:', err);
            }
        });

        es.addEventListener('error_event', (e) => {
            try {
                const data = JSON.parse(e.data);
                console.error('[SSE] Server error:', data.message);
            } catch (err) {
                console.error('[SSE] Parse error:', err);
            }
        });
    },

    // ── SSE Event Handlers ────────────────────────────────────

    _onPortfolioUpdate(data) {
        // Update topbar and dashboard cards if visible
        if (this._currentPage === DashboardPage) {
            DashboardPage._updatePortfolioCards(data);
        }

        // Update footer timestamp
        const lastUpdated = document.getElementById('last-updated');
        if (lastUpdated) {
            const time = new Date().toLocaleTimeString();
            lastUpdated.textContent = `Last updated: ${time}`;
        }
    },

    _onPositionsUpdate(data) {
        // Update dashboard positions table if visible
        if (this._currentPage === DashboardPage) {
            DashboardPage._updatePositionsTable(data.positions || []);
        }
    },

    _onDaemonStatus(data) {
        const modeEl = document.querySelector('#pipeline-mode .mode-badge');
        if (modeEl) {
            const mode = data.pipeline_mode || 'full';
            modeEl.textContent = mode === 'full' ? 'Full LLM' : 'Quant';
        }
        this._updateDaemonStatus('running');
    },

    _onClockUpdate(data) {
        const statusEl = document.getElementById('market-status');
        if (statusEl) {
            statusEl.textContent = data.is_open ? 'Open' : 'Closed';
            statusEl.className = `clock-status ${data.is_open ? 'text-success' : 'text-danger'}`;
        }
    },

    // ── UI Helpers ─────────────────────────────────────────────

    _updateDaemonStatus(status) {
        const container = document.getElementById('daemon-status');
        if (!container) return;

        const dot = container.querySelector('.status-dot');
        const text = container.querySelector('.status-text');

        if (dot) {
            dot.className = `status-dot status-dot--${status}`;
        }
        if (text) {
            const labels = {
                'running': 'Connected',
                'idle': 'Idle',
                'stopped': 'Disconnected',
                'loading': 'Reconnecting\u2026',
            };
            text.textContent = labels[status] || status;
        }
    },

    /**
     * Navigate to a path programmatically.
     * @param {string} path - e.g. '/orders' or '/positions/NVDA'
     */
    navigate(path) {
        history.pushState(null, '', path);
        this._onRouteChange();
    },
};

// ── Boot ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    App.init();
});

// Make globally available for debugging
window.App = App;
