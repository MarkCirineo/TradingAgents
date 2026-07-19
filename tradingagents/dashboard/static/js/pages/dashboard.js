/**
 * TradingAgents Dashboard — Dashboard Page (Home)
 *
 * The main command centre. Bento-grid layout with:
 * - Portfolio overview cards (value, cash/exposure, regime)
 * - Equity curve chart (TradingView Lightweight Charts)
 * - Open positions table (with bracket stop/TP prices)
 * - Today's activity panel (screening funnel + recent orders)
 * - Screening log (expandable, full width)
 */

const DashboardPage = {
    /** Refresh interval handle. */
    _refreshTimer: null,
    /** Current chart date range in days. */
    _chartDays: 30,

    /** Current benchmark overlay: 'none' | 'spy' | 'blend' (default raw SPY). */
    _benchmark: 'spy',

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

        // Row 4: Screening log (full width)
        this._renderScreeningLog(grid);

        // Load all data
        await this.refresh();

        // Auto-refresh every 60 seconds
        this._refreshTimer = setInterval(() => this.refresh(), 60000);
    },

    /**
     * Refresh all dashboard data.
     */
    async refresh() {
        const [portfolio, positionsData, equityData, screeningData, regime] = await Promise.all([
            API.getPortfolio(),
            API.getPositions(),
            API.getEquityCurve(this._chartDays, this._benchmark),
            API.getScreeningLatest(),
            API.getRegime(),
        ]);

        if (portfolio) this._updatePortfolioCards(portfolio);
        if (positionsData) this._updatePositionsTable(positionsData.positions || []);
        if (equityData) this._renderEquityResponse(equityData);
        if (screeningData) {
            this._updateActivity(screeningData);
            this._updateScreeningLog(screeningData.screening || []);
        }
        if (regime) this._updateRegimeCard(regime);
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

    // ══════════════════════════════════════════════════════════
    // Row 1: Portfolio Cards
    // ══════════════════════════════════════════════════════════

    _renderPortfolioCards(grid) {
        // Card 1: Portfolio Value
        const valueCard = Components.card({ title: 'Portfolio Value', icon: '\u{1F4CA}', id: 'card-portfolio-value' });
        const valueStat = Components.stat({ value: '$\u2014', label: 'Total equity', large: true });
        valueStat.id = 'stat-portfolio-value';
        valueCard.appendChild(valueStat);
        grid.appendChild(valueCard);

        // Card 2: Cash & Exposure
        const cashCard = Components.card({ title: 'Cash & Exposure', icon: '\u{1F4B0}', id: 'card-cash' });
        const cashContent = document.createElement('div');
        cashContent.id = 'cash-content';

        const cashStat = Components.stat({ value: '$\u2014', label: 'Available cash' });
        cashStat.id = 'stat-cash';
        cashContent.appendChild(cashStat);

        // Buying power row
        const bpRow = document.createElement('div');
        bpRow.className = 'stat-row';
        bpRow.id = 'buying-power-row';
        bpRow.innerHTML = '<span class="stat-row__label">Buying Power</span><span class="stat-row__value" id="buying-power">$\u2014</span>';
        cashContent.appendChild(bpRow);

        // Exposure bar
        const exposureLabel = document.createElement('div');
        exposureLabel.className = 'stat-row';
        exposureLabel.innerHTML = '<span class="stat-row__label">Exposure</span><span class="stat-row__value" id="exposure-pct">\u2014%</span>';
        cashContent.appendChild(exposureLabel);

        const exposureBar = Components.exposureBar(0);
        exposureBar.id = 'exposure-bar';
        cashContent.appendChild(exposureBar);

        cashCard.appendChild(cashContent);
        grid.appendChild(cashCard);

        // Card 3: Market Regime
        const regimeCard = Components.card({ title: 'Market Regime', icon: '\u{1F3AF}', id: 'card-regime' });
        const regimeContent = document.createElement('div');
        regimeContent.id = 'regime-content';

        const regime = document.createElement('div');
        regime.className = 'regime';

        const regimeDot = document.createElement('div');
        regimeDot.className = 'regime__dot regime__dot--normal';
        regimeDot.id = 'regime-dot';
        regime.appendChild(regimeDot);

        const regimeInfo = document.createElement('div');
        const regimeLabel = document.createElement('div');
        regimeLabel.className = 'regime__label';
        regimeLabel.id = 'regime-label';
        regimeLabel.textContent = '\u2014';
        regimeInfo.appendChild(regimeLabel);

        const regimeDetail = document.createElement('div');
        regimeDetail.className = 'regime__detail';
        regimeDetail.id = 'regime-detail';
        regimeDetail.textContent = 'Waiting for data\u2026';
        regimeInfo.appendChild(regimeDetail);

        regime.appendChild(regimeInfo);
        regimeContent.appendChild(regime);

        // Regime detail rows (SPY MAs, VIX, risk knobs) \u2014 filled on refresh
        const regimeParams = document.createElement('div');
        regimeParams.id = 'regime-params';
        regimeParams.style.marginTop = '12px';
        regimeContent.appendChild(regimeParams);

        // Positions count
        const posCount = document.createElement('div');
        posCount.className = 'stat-row';
        posCount.style.marginTop = '4px';
        posCount.innerHTML = '<span class="stat-row__label">Open Positions</span><span class="stat-row__value" id="positions-count">\u2014</span>';
        regimeContent.appendChild(posCount);

        regimeCard.appendChild(regimeContent);
        grid.appendChild(regimeCard);
    },

    /**
     * Update the Market Regime card with live data from /api/regime.
     * Shows the bot's actual go/no-go signal: SPY MA stacking + VIX.
     */
    _updateRegimeCard(data) {
        const checks = data.checks || {};
        const regime = data.regime || {};
        const label = regime.label || 'Unknown';
        const favorable = data.favorable;

        // Dot colour: green when trading, orange when cautious, red when off
        const dot = document.getElementById('regime-dot');
        if (dot) {
            let dotClass = 'regime__dot--normal';
            if (favorable === false || regime.pause_entries) {
                dotClass = 'regime__dot--panic';
            } else if (label === 'Elevated') {
                dotClass = 'regime__dot--elevated';
            }
            dot.className = `regime__dot ${dotClass}`;
        }

        // Label: the go/no-go verdict
        const labelEl = document.getElementById('regime-label');
        if (labelEl) {
            if (favorable === null || favorable === undefined) {
                labelEl.textContent = 'Unavailable';
            } else {
                labelEl.textContent = favorable
                    ? `Favorable \u2014 ${label}`
                    : `No Entries \u2014 ${label}`;
            }
            labelEl.className = 'regime__label ' +
                (favorable ? 'text-success' : favorable === false ? 'text-danger' : '');
        }

        // Detail line: the two SPY gates + VIX
        const detailEl = document.getElementById('regime-detail');
        if (detailEl) {
            if (checks.spy_close) {
                const above = checks.spy_above_20ma ? '\u2713' : '\u2717';
                const stacked = checks.spy_10_above_20 ? '\u2713' : '\u2717';
                const vix = checks.vix_level ? checks.vix_level.toFixed(1) : '\u2014';
                detailEl.textContent =
                    `SPY > 20MA ${above} \u00b7 10MA > 20MA ${stacked} \u00b7 VIX ${vix}`;
            } else {
                detailEl.textContent = 'Regime data unavailable';
            }
        }

        // Risk knobs the regime sets for today
        const params = document.getElementById('regime-params');
        if (params) {
            params.innerHTML = '';
            if (regime.label) {
                const rows = [
                    ['Risk per trade', `${((regime.risk_pct || 0) * 100).toFixed(2)}%`],
                    ['Max positions', `${regime.max_positions ?? '\u2014'}`],
                    ['Max exposure', `${((regime.max_exposure_pct || 0) * 100).toFixed(0)}%`],
                ];
                rows.forEach(([l, v]) => {
                    const row = document.createElement('div');
                    row.className = 'stat-row';
                    row.innerHTML =
                        `<span class="stat-row__label">${l}</span>` +
                        `<span class="stat-row__value">${v}</span>`;
                    params.appendChild(row);
                });
            }
        }
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

            const pnl = data.daily_pnl || 0;
            const pnlPct = data.daily_pnl_pct || 0;
            const isPos = pnl > 0;
            const isNeg = pnl < 0;
            const change = document.createElement('span');
            change.className = `stat__change ${isPos ? 'stat__change--positive' : isNeg ? 'stat__change--negative' : 'stat__change--neutral'}`;
            const arrow = isPos ? '\u2191' : isNeg ? '\u2193' : '\u2192';
            change.textContent = `${arrow} ${Components.formatMoney(Math.abs(pnl), 0)} (${Components.formatPercent(pnlPct)})`;
            valueEl.insertAdjacentElement('afterend', change);
        }

        // Cash
        const cashEl = document.querySelector('#stat-cash .stat__value');
        if (cashEl) cashEl.textContent = Components.formatMoney(data.cash, 0);

        // Buying power
        const bpEl = document.getElementById('buying-power');
        if (bpEl) bpEl.textContent = Components.formatMoney(data.buying_power, 0);

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

        // Market status (topbar)
        const marketStatus = document.getElementById('market-status');
        if (marketStatus) {
            marketStatus.textContent = data.market_open ? 'Open' : 'Closed';
            marketStatus.className = `clock-status ${data.market_open ? 'text-success' : 'text-danger'}`;
        }

        // Positions count
        const posCount = document.getElementById('positions-count');
        if (posCount) posCount.textContent = data.positions_count || '0';
    },

    // ══════════════════════════════════════════════════════════
    // Row 2: Equity Curve
    // ══════════════════════════════════════════════════════════

    _renderEquityCurve(grid) {
        const card = Components.card({ title: 'Equity Curve', icon: '\u{1F4C8}', id: 'card-equity' });
        card.classList.add('bento-grid__full');

        // Controls row: date ranges (left) + benchmark selector (right)
        const controls = document.createElement('div');
        controls.className = 'chart-controls';
        controls.id = 'equity-controls';

        const ranges = [
            { label: '7D', days: 7 },
            { label: '30D', days: 30 },
            { label: '90D', days: 90 },
            { label: 'All', days: 9999 },
        ];
        ranges.forEach((range) => {
            const btn = document.createElement('button');
            btn.className = `chart-controls__btn${range.days === this._chartDays ? ' active' : ''}`;
            btn.textContent = range.label;
            btn.dataset.range = String(range.days);
            btn.addEventListener('click', () => {
                controls.querySelectorAll('[data-range]').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this._chartDays = range.days;
                this._loadEquity();
            });
            controls.appendChild(btn);
        });

        // Spacer pushes the benchmark selector to the right edge
        const spacer = document.createElement('div');
        spacer.style.flex = '1';
        controls.appendChild(spacer);

        const benchLabel = document.createElement('span');
        benchLabel.className = 'chart-controls__label';
        benchLabel.textContent = 'vs';
        controls.appendChild(benchLabel);

        const benchmarks = [
            { label: 'Off', mode: 'none' },
            { label: 'SPY', mode: 'spy' },
            { label: '60/40', mode: 'blend' },
        ];
        benchmarks.forEach((b) => {
            const btn = document.createElement('button');
            btn.className = `chart-controls__btn${b.mode === this._benchmark ? ' active' : ''}`;
            btn.textContent = b.label;
            btn.dataset.bench = b.mode;
            btn.addEventListener('click', () => {
                controls.querySelectorAll('[data-bench]').forEach(x => x.classList.remove('active'));
                btn.classList.add('active');
                this._benchmark = b.mode;
                this._loadEquity();
            });
            controls.appendChild(btn);
        });
        card.appendChild(controls);

        // Benchmark summary line (You vs benchmark, with the delta)
        const summary = document.createElement('div');
        summary.className = 'equity-summary';
        summary.id = 'equity-summary';
        card.appendChild(summary);

        // Chart container
        const chartContainer = document.createElement('div');
        chartContainer.className = 'chart-container';
        chartContainer.id = 'equity-chart';
        card.appendChild(chartContainer);

        grid.appendChild(card);

        // Initialize empty chart
        requestAnimationFrame(() => {
            Charts.createAreaChart(chartContainer, [], { id: 'equity' });
        });
    },

    /** Fetch the equity curve (+ benchmark) for the current range and render. */
    async _loadEquity() {
        const resp = await API.getEquityCurve(this._chartDays, this._benchmark);
        if (resp) this._renderEquityResponse(resp);
    },

    /** Render an equity-curve API response: account line + benchmark overlay. */
    _renderEquityResponse(resp) {
        this._updateEquityCurve(resp.data || []);

        if (this._benchmark !== 'none' && resp.benchmark && resp.benchmark.length) {
            const color = this._benchmark === 'blend' ? '#a78bfa' : '#f59e0b';
            Charts.setBenchmark('equity', resp.benchmark, { color });
            this._updateBenchmarkSummary(resp.summary, this._benchmark);
        } else {
            Charts.clearBenchmark('equity');
            this._updateBenchmarkSummary(null);
        }
    },

    _updateEquityCurve(data) {
        if (!data || data.length === 0) return;
        const container = document.getElementById('equity-chart');
        if (!container) return;

        // Check if chart exists, if not create it
        if (!Charts._instances['equity']) {
            Charts.createAreaChart(container, data, { id: 'equity' });
        } else {
            Charts.updateData('equity', data);
        }
    },

    /** Render the "You +X% · SPY +Y% · beating by Z%" line under the controls. */
    _updateBenchmarkSummary(summary, mode) {
        const el = document.getElementById('equity-summary');
        if (!el) return;
        if (!summary) { el.innerHTML = ''; return; }

        const name = mode === 'blend' ? '60/40' : 'SPY';
        const pct = (v) => (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
        const cls = (v) => (v >= 0 ? 'text-success' : 'text-danger');
        const d = summary.delta;
        const verdict = d >= 0 ? 'beating' : 'trailing';

        el.innerHTML =
            `<span class="equity-summary__item">You <b class="${cls(summary.your_return)}">${pct(summary.your_return)}</b></span>` +
            `<span class="equity-summary__item">${name} <b class="${cls(summary.benchmark_return)}">${pct(summary.benchmark_return)}</b></span>` +
            `<span class="equity-summary__delta ${cls(d)}">${verdict} the market by ${(Math.abs(d) * 100).toFixed(1)}%</span>`;
    },

    // ══════════════════════════════════════════════════════════
    // Row 3: Positions & Activity
    // ══════════════════════════════════════════════════════════

    _renderPositionsAndActivity(grid) {
        // Left: Open Positions (wider)
        const posCard = Components.card({ title: 'Open Positions', icon: '\u{1F4CB}', id: 'card-positions' });
        posCard.classList.add('bento-grid__span-2');

        const posContent = document.createElement('div');
        posContent.className = 'table-scroll';
        const posTable = document.createElement('div');
        posTable.id = 'positions-table';
        posTable.appendChild(Components.emptyState('\u{1F4ED}', 'No open positions'));
        posContent.appendChild(posTable);
        posCard.appendChild(posContent);
        grid.appendChild(posCard);

        // Right: Today's Activity
        const actCard = Components.card({ title: "Today's Activity", icon: '\u26A1', id: 'card-activity' });

        const actContent = document.createElement('div');
        actContent.id = 'activity-content';
        actContent.appendChild(Components.emptyState('\u{1F4CA}', 'No activity yet today'));
        actCard.appendChild(actContent);
        grid.appendChild(actCard);
    },

    _updatePositionsTable(positions) {
        const container = document.getElementById('positions-table');
        if (!container) return;
        container.innerHTML = '';

        if (!positions || positions.length === 0) {
            container.appendChild(Components.emptyState('\u{1F4ED}', 'No open positions'));
            return;
        }

        const headers = ['Symbol', 'Entry', 'Current', 'P&L', 'Shares', 'Day', 'Stop', 'Status'];
        const rows = positions.map(pos => {
            const isPending = pos.status === 'PENDING';

            // Build status badges
            const statusContainer = document.createElement('span');
            if (isPending) {
                statusContainer.appendChild(Components.badge('Pending Fill', 'warning'));
            }
            if (pos.trimmed) {
                statusContainer.appendChild(Components.badge('Trimmed', 'warning'));
            }
            if (pos.trailing_stop_active) {
                statusContainer.appendChild(Components.badge('Trailing', 'info'));
            }
            if (pos.breakeven_stop_active) {
                statusContainer.appendChild(Components.badge('BE Stop', 'primary'));
            }
            statusContainer.appendChild(
                Components.badge(pos.pipeline_mode === 'full' ? 'LLM' : 'Quant', 'neutral')
            );

            return [
                pos.symbol,
                Components.formatMoney(pos.entry_price),
                isPending ? '\u2014' : Components.formatMoney(pos.current_price),
                isPending ? '\u2014' : Components.pnl(pos.unrealized_pl, 'money'),
                (pos.current_qty || 0).toString(),
                isPending ? '\u2014' : `D${pos.day_count}`,
                this._formatStopCell(pos),
                statusContainer,
            ];
        });

        const table = Components.table(headers, rows, {
            onRowClick: (idx) => {
                const symbol = positions[idx].symbol;
                App.navigate(`/positions/${symbol}`);
            },
        });

        container.appendChild(table);
    },

    /**
     * Format the Stop column with price + type badge.
     * Shows: "$142.50 [BE]" or "$156.30 [10SMA]" or "$138.00 [Pivot]"
     * The price comes from the live Alpaca stop order when one exists,
     * falling back to the DB-tracked stop.
     */
    _formatStopCell(pos) {
        const container = document.createElement('span');
        container.style.whiteSpace = 'nowrap';
        const price = pos.stop_price || pos.current_stop || pos.entry_orl;

        if (!price) {
            container.textContent = '\u2014';
            return container;
        }

        // Determine stop type from DB flags
        let typeLabel = 'Pivot';
        let badgeVariant = 'neutral';
        if (pos.status === 'PENDING') {
            typeLabel = 'on fill';
        } else if (pos.trailing_stop_active) {
            typeLabel = '10SMA';
            badgeVariant = 'info';
        } else if (pos.breakeven_stop_active) {
            typeLabel = 'BE';
            badgeVariant = 'primary';
        } else if (pos.entry_lod && Math.abs(price - pos.entry_lod) < 0.01) {
            typeLabel = 'LOD';
            badgeVariant = 'warning';
        }

        const priceSpan = document.createElement('span');
        priceSpan.textContent = Components.formatMoney(price) + ' ';
        container.appendChild(priceSpan);
        container.appendChild(Components.badge(typeLabel, badgeVariant));

        return container;
    },

    _updateActivity(data) {
        const container = document.getElementById('activity-content');
        if (!container) return;
        container.innerHTML = '';

        const funnel = data.funnel || {};
        const orders = data.orders || [];
        const hasData = funnel.screened > 0 || orders.length > 0;

        if (!hasData) {
            container.appendChild(Components.emptyState('\u{1F4CA}', 'No activity yet today'));
            return;
        }

        // Screening funnel
        if (funnel.screened > 0) {
            const funnelLabel = document.createElement('div');
            funnelLabel.className = 'activity-section-label';
            funnelLabel.textContent = 'Screening Pipeline';
            container.appendChild(funnelLabel);

            container.appendChild(Components.funnel(funnel));
        }

        // Recent orders list
        if (orders.length > 0) {
            const ordersLabel = document.createElement('div');
            ordersLabel.className = 'activity-section-label';
            ordersLabel.style.marginTop = '16px';
            ordersLabel.textContent = `Today's Orders (${orders.length})`;
            container.appendChild(ordersLabel);

            const ordersList = document.createElement('div');
            ordersList.className = 'activity-orders';

            orders.slice(0, 8).forEach(order => {
                const item = document.createElement('div');
                item.className = 'activity-order';

                const left = document.createElement('div');
                left.className = 'activity-order__left';

                const symbol = document.createElement('span');
                symbol.className = 'activity-order__symbol';
                symbol.textContent = order.symbol;
                left.appendChild(symbol);

                const side = Components.badge(
                    order.side,
                    order.side === 'BUY' ? 'success' : 'danger'
                );
                left.appendChild(side);

                const type = document.createElement('span');
                type.className = 'activity-order__type';
                type.textContent = `${order.qty} @ ${order.filled_price ? Components.formatMoney(order.filled_price) : order.order_type}`;
                left.appendChild(type);

                item.appendChild(left);

                const status = Components.badge(
                    order.status,
                    order.status === 'FILLED' ? 'success' : order.status === 'CANCELLED' ? 'danger' : 'warning'
                );
                item.appendChild(status);

                ordersList.appendChild(item);
            });

            container.appendChild(ordersList);
        }
    },

    // ══════════════════════════════════════════════════════════
    // Row 4: Screening Log
    // ══════════════════════════════════════════════════════════

    _renderScreeningLog(grid) {
        const card = Components.card({ title: 'Screening Log', icon: '\u{1F50D}', id: 'card-screening' });
        card.classList.add('bento-grid__full');

        // Toggle button
        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'chart-controls__btn';
        toggleBtn.textContent = 'Show Details';
        toggleBtn.style.marginBottom = '12px';
        toggleBtn.addEventListener('click', () => {
            const table = document.getElementById('screening-table');
            if (table) {
                const isHidden = table.style.display === 'none';
                table.style.display = isHidden ? '' : 'none';
                toggleBtn.textContent = isHidden ? 'Hide Details' : 'Show Details';
            }
        });
        card.appendChild(toggleBtn);

        const tableContainer = document.createElement('div');
        tableContainer.id = 'screening-table';
        tableContainer.className = 'table-scroll';
        tableContainer.style.display = 'none'; // collapsed by default
        tableContainer.appendChild(Components.emptyState('\u{1F50D}', 'No screening data for today'));
        card.appendChild(tableContainer);

        grid.appendChild(card);
    },

    _updateScreeningLog(screening) {
        const container = document.getElementById('screening-table');
        if (!container) return;
        container.innerHTML = '';

        if (!screening || screening.length === 0) {
            container.appendChild(Components.emptyState('\u{1F50D}', 'No screening data for today'));
            return;
        }

        const headers = ['Symbol', 'Source', 'Score', 'Pipeline', 'Signal'];
        const rows = screening.map(s => {
            const selected = s.selected_for_pipeline;
            const signal = s.signal_result;

            // Signal badge
            let signalBadge;
            if (!selected) {
                signalBadge = Components.badge('Skipped', 'neutral');
            } else if (signal === 'Buy' || signal === 'Overweight') {
                signalBadge = Components.badge(signal, 'success');
            } else if (signal === 'Sell' || signal === 'Underweight') {
                signalBadge = Components.badge(signal, 'danger');
            } else if (signal === 'Hold') {
                signalBadge = Components.badge(signal, 'warning');
            } else {
                signalBadge = Components.badge(signal || 'Pending', 'neutral');
            }

            return [
                s.symbol,
                s.source || '\u2014',
                (s.score || 0).toFixed(1),
                selected ? Components.badge('Yes', 'primary') : Components.badge('No', 'neutral'),
                signalBadge,
            ];
        });

        container.appendChild(Components.table(headers, rows));
    },
};

// Make globally available
window.DashboardPage = DashboardPage;
