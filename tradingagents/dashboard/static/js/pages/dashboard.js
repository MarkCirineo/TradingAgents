/**
 * TradingAgents Dashboard — Dashboard Page (Home)
 *
 * The main command centre. Bento-grid layout with:
 * - Portfolio overview cards (value, cash, regime)
 * - Equity curve chart
 * - Open positions table (with bracket stop/TP prices)
 * - Today's activity panel
 *
 * This is the skeleton for Phase 1. Phase 2 fills in the details.
 */

const DashboardPage = {
    /** Refresh interval handle. */
    _refreshTimer: null,

    /**
     * Render the dashboard page into the given container.
     * @param {HTMLElement} container
     */
    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'Dashboard';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Autonomous trading command centre';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        // Bento grid
        const grid = document.createElement('div');
        grid.className = 'bento-grid';
        grid.id = 'dashboard-grid';
        container.appendChild(grid);

        // Row 1: Portfolio overview cards (3 columns)
        this._renderPortfolioCards(grid);

        // Row 2: Equity curve (full width)
        this._renderEquityCurve(grid);

        // Row 3: Positions + Activity (2 columns)
        this._renderPositionsAndActivity(grid);

        // Load data
        await this.refresh();

        // Auto-refresh every 60 seconds (SSE handles live updates,
        // but this catches anything SSE misses)
        this._refreshTimer = setInterval(() => this.refresh(), 60000);
    },

    /**
     * Refresh all dashboard data.
     */
    async refresh() {
        const [portfolio, positionsData] = await Promise.all([
            API.getPortfolio(),
            API.getPositions(),
        ]);

        if (portfolio) {
            this._updatePortfolioCards(portfolio);
        }

        if (positionsData) {
            this._updatePositionsTable(positionsData.positions || []);
        }
    },

    /**
     * Cleanup when navigating away.
     */
    destroy() {
        if (this._refreshTimer) {
            clearInterval(this._refreshTimer);
            this._refreshTimer = null;
        }
        Charts.destroyAll();
    },

    // ── Internal: Portfolio Cards ──────────────────────────────

    _renderPortfolioCards(grid) {
        // Card 1: Portfolio Value
        const valueCard = Components.card({ title: 'Portfolio Value', icon: '📊', id: 'card-portfolio-value' });
        const valueStat = Components.stat({ value: '$—', label: 'Total equity', large: true });
        valueStat.id = 'stat-portfolio-value';
        valueCard.appendChild(valueStat);
        grid.appendChild(valueCard);

        // Card 2: Cash & Exposure
        const cashCard = Components.card({ title: 'Cash & Exposure', icon: '💰', id: 'card-cash' });
        const cashContent = document.createElement('div');
        cashContent.id = 'cash-content';

        const cashStat = Components.stat({ value: '$—', label: 'Available cash' });
        cashStat.id = 'stat-cash';
        cashContent.appendChild(cashStat);

        const exposureLabel = document.createElement('div');
        exposureLabel.style.cssText = 'margin-top: 12px; display: flex; justify-content: space-between; font-size: 0.8rem;';
        exposureLabel.innerHTML = '<span style="color: var(--text-secondary)">Exposure</span><span id="exposure-pct" style="color: var(--text-primary); font-weight: 600;">—%</span>';
        cashContent.appendChild(exposureLabel);

        const exposureBar = Components.exposureBar(0);
        exposureBar.id = 'exposure-bar';
        cashContent.appendChild(exposureBar);

        cashCard.appendChild(cashContent);
        grid.appendChild(cashCard);

        // Card 3: Market Regime
        const regimeCard = Components.card({ title: 'Market Regime', icon: '🎯', id: 'card-regime' });
        const regimeContent = document.createElement('div');
        regimeContent.id = 'regime-content';

        const regime = document.createElement('div');
        regime.className = 'regime';
        regime.innerHTML = `
            <div class="regime__dot regime__dot--normal" id="regime-dot"></div>
            <div>
                <div class="regime__label" id="regime-label">—</div>
                <div class="regime__detail" id="regime-detail">Waiting for data…</div>
            </div>
        `;
        regimeContent.appendChild(regime);
        regimeCard.appendChild(regimeContent);
        grid.appendChild(regimeCard);
    },

    _updatePortfolioCards(data) {
        // Portfolio value
        const valueStat = document.getElementById('stat-portfolio-value');
        if (valueStat) {
            const valueEl = valueStat.querySelector('.stat__value');
            if (valueEl) valueEl.textContent = Components.formatMoney(data.portfolio_value, 0);

            // Remove old change badge and add new
            const oldChange = valueStat.querySelector('.stat__change');
            if (oldChange) oldChange.remove();

            const change = document.createElement('span');
            const pnl = data.daily_pnl || 0;
            const pnlPct = data.daily_pnl_pct || 0;
            const isPos = pnl > 0;
            const isNeg = pnl < 0;
            change.className = `stat__change ${isPos ? 'stat__change--positive' : isNeg ? 'stat__change--negative' : 'stat__change--neutral'}`;
            const arrow = isPos ? '↑' : isNeg ? '↓' : '→';
            change.textContent = `${arrow} ${Components.formatMoney(Math.abs(pnl), 0)} (${Components.formatPercent(pnlPct)})`;
            // Insert after value element
            valueEl.insertAdjacentElement('afterend', change);
        }

        // Cash
        const cashStat = document.getElementById('stat-cash');
        if (cashStat) {
            const cashEl = cashStat.querySelector('.stat__value');
            if (cashEl) cashEl.textContent = Components.formatMoney(data.cash, 0);
        }

        // Exposure
        const exposurePct = document.getElementById('exposure-pct');
        if (exposurePct) exposurePct.textContent = (data.exposure_pct || 0).toFixed(1) + '%';

        const exposureBar = document.getElementById('exposure-bar');
        if (exposureBar) {
            const fill = exposureBar.querySelector('.exposure-bar__fill');
            if (fill) {
                fill.style.width = `${Math.min(data.exposure_pct || 0, 100)}%`;
                fill.className = `exposure-bar__fill${(data.exposure_pct || 0) > 50 ? ' exposure-bar__fill--warning' : ''}`;
            }
        }

        // Market status
        const marketStatus = document.getElementById('market-status');
        if (marketStatus) {
            marketStatus.textContent = data.market_open ? 'Open' : 'Closed';
            marketStatus.className = `clock-status ${data.market_open ? 'text-success' : 'text-danger'}`;
        }
    },

    // ── Internal: Equity Curve ─────────────────────────────────

    _renderEquityCurve(grid) {
        const card = Components.card({ title: 'Equity Curve', icon: '📈', id: 'card-equity' });
        card.classList.add('bento-grid__full');

        // Chart controls
        const controls = document.createElement('div');
        controls.className = 'chart-controls';
        ['7D', '30D', '90D', 'All'].forEach((label, i) => {
            const btn = document.createElement('button');
            btn.className = `chart-controls__btn${i === 1 ? ' active' : ''}`;
            btn.textContent = label;
            btn.addEventListener('click', () => {
                controls.querySelectorAll('.chart-controls__btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                // TODO: Phase 2 — reload chart with different date range
            });
            controls.appendChild(btn);
        });
        card.appendChild(controls);

        // Chart container
        const chartContainer = document.createElement('div');
        chartContainer.className = 'chart-container';
        chartContainer.id = 'equity-chart';
        card.appendChild(chartContainer);

        grid.appendChild(card);

        // Render placeholder chart with sample data
        requestAnimationFrame(() => {
            Charts.createAreaChart(chartContainer, [], { id: 'equity' });
        });
    },

    // ── Internal: Positions & Activity ────────────────────────

    _renderPositionsAndActivity(grid) {
        // Left: Open Positions
        const posCard = Components.card({ title: 'Open Positions', icon: '📋', id: 'card-positions' });
        posCard.classList.add('bento-grid__span-2');

        const posTable = document.createElement('div');
        posTable.id = 'positions-table';
        posTable.appendChild(Components.emptyState('📭', 'No open positions'));
        posCard.appendChild(posTable);
        grid.appendChild(posCard);

        // Right: Today's Activity
        const actCard = Components.card({ title: "Today's Activity", icon: '⚡', id: 'card-activity' });

        const actContent = document.createElement('div');
        actContent.id = 'activity-content';
        actContent.appendChild(Components.emptyState('📊', 'Waiting for today\'s data…'));
        actCard.appendChild(actContent);
        grid.appendChild(actCard);
    },

    _updatePositionsTable(positions) {
        const container = document.getElementById('positions-table');
        if (!container) return;
        container.innerHTML = '';

        if (!positions || positions.length === 0) {
            container.appendChild(Components.emptyState('📭', 'No open positions'));
            return;
        }

        const headers = ['Symbol', 'Entry', 'Current', 'P&L', 'Shares', 'Day', 'Stop', 'TP'];
        const rows = positions.map(pos => [
            pos.symbol,
            Components.formatMoney(pos.entry_price),
            Components.formatMoney(pos.current_price),
            Components.pnl(pos.unrealized_pl, 'money'),
            pos.current_qty.toString(),
            `D${pos.day_count}`,
            pos.stop_price ? Components.formatMoney(pos.stop_price) : '—',
            pos.take_profit_price ? Components.formatMoney(pos.take_profit_price) : '—',
        ]);

        const table = Components.table(headers, rows, {
            onRowClick: (idx) => {
                const symbol = positions[idx].symbol;
                App.navigate(`/positions/${symbol}`);
            },
        });

        container.appendChild(table);
    },
};

// Make globally available
window.DashboardPage = DashboardPage;
