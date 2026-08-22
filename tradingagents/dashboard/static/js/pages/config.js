/**
 * TradingAgents Dashboard — Config Page
 *
 * Read-only view of the daemon's current configuration.
 * Organized into sections: Runtime, LLM, Guardrails, Screening,
 * Strategy, Schedule, Data Vendors.
 *
 * Accessed via /config
 */

const ConfigPage = {
    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'Configuration';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Current daemon settings (read-only)';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        const content = document.createElement('div');
        content.className = 'fade-in';
        content.id = 'config-content';
        content.appendChild(Components.emptyState('\u23F3', 'Loading config...'));
        container.appendChild(content);

        const config = await API.getConfig();
        this._renderConfig(content, config);
    },

    destroy() {},

    _renderConfig(container, config) {
        container.innerHTML = '';

        if (!config) {
            container.appendChild(Components.emptyState('\u274C', 'Failed to load config'));
            return;
        }

        const grid = document.createElement('div');
        grid.className = 'bento-grid bento-grid--2col';

        // ── Runtime ───────────────────────────────────────────
        const runtime = config._runtime || {};
        const rtCard = Components.card({ title: 'Runtime', icon: '\u2699\uFE0F', id: 'config-runtime' });

        const rtFields = [
            { label: 'Pipeline Mode', value: runtime.pipeline_mode || 'full', badge: runtime.pipeline_mode === 'quant' ? 'warning' : 'success' },
            { label: 'LLM Provider', value: runtime.llm_provider || 'openai' },
            { label: 'Deep Think Model', value: runtime.deep_think_llm || '(default)' },
            { label: 'Quick Think Model', value: runtime.quick_think_llm || '(default)' },
            { label: 'Dashboard Port', value: runtime.dashboard_port || '8050' },
            { label: 'Instance Label', value: runtime.instance_label || '(not set)' },
            { label: 'Peer URL', value: runtime.peer_url || '(not configured)' },
            { label: 'DB Path', value: runtime.db_path || '(default)' },
            { label: 'Alpaca', value: runtime.alpaca_configured ? 'Configured' : 'Not set', badge: runtime.alpaca_configured ? 'success' : 'danger' },
            { label: 'Finnhub', value: runtime.finnhub_configured ? 'Configured' : 'Not set', badge: runtime.finnhub_configured ? 'success' : 'danger' },
        ];

        rtFields.forEach(f => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = document.createElement('span');
            label.className = 'stat-row__label';
            label.textContent = f.label;
            row.appendChild(label);

            if (f.badge) {
                row.appendChild(Components.badge(f.value, f.badge));
            } else {
                const val = document.createElement('span');
                val.className = 'stat-row__value';
                val.style.fontFamily = 'var(--font-mono)';
                val.style.fontSize = '0.8rem';
                val.textContent = f.value;
                row.appendChild(val);
            }

            rtCard.appendChild(row);
        });

        grid.appendChild(rtCard);

        // ── Guardrails ────────────────────────────────────────
        const gr = config.guardrails || {};
        const grCard = Components.card({ title: 'Guardrails', icon: '\u{1F6E1}\uFE0F', id: 'config-guardrails' });

        const grFields = [
            { label: 'Max Position Size', value: this._pct(gr.max_position_pct) },
            { label: 'Max Exposure', value: this._pct(gr.max_exposure_pct) },
            { label: 'Max Daily Loss', value: this._pct(gr.max_daily_loss_pct) },
            { label: 'Max Risk/Trade', value: this._pct(gr.max_risk_per_trade_pct) },
            { label: 'Target Risk/Trade', value: this._pct(gr.target_risk_per_trade_pct) },
            { label: 'Max Portfolio Heat', value: this._pct(gr.max_portfolio_heat_pct) },
            { label: 'Max Sector Exposure', value: this._pct(gr.max_sector_exposure_pct) },
            { label: 'Max Concurrent Positions', value: gr.max_concurrent_positions || '\u2014' },
            { label: 'Min Dollar Volume', value: gr.min_dollar_volume ? `$${(gr.min_dollar_volume / 1e6).toFixed(0)}M` : '\u2014' },
        ];

        grFields.forEach(f => this._appendRow(grCard, f.label, f.value));
        grid.appendChild(grCard);

        // ── Screening ─────────────────────────────────────────
        const sc = config.screening || {};
        const scCard = Components.card({ title: 'Screening', icon: '\u{1F50D}', id: 'config-screening' });

        [
            { label: 'Source', value: sc.source || '\u2014' },
            { label: 'Max Candidates', value: sc.max_candidates || '\u2014' },
            { label: 'Max Pipeline Runs', value: sc.max_pipeline_runs || '\u2014' },
            { label: 'Max Workers', value: sc.max_workers || '\u2014' },
            { label: 'Watchlist', value: sc.watchlist?.length > 0 ? sc.watchlist.join(', ') : '(empty)' },
        ].forEach(f => this._appendRow(scCard, f.label, f.value));
        grid.appendChild(scCard);

        // ── Schedule ──────────────────────────────────────────
        const sched = config.trading_schedule || {};
        const schedCard = Components.card({ title: 'Trading Schedule', icon: '\u{1F552}', id: 'config-schedule' });

        [
            { label: 'Pre-Market', value: sched.pre_market || '\u2014' },
            { label: 'Entry Window', value: sched.entry_window || '\u2014' },
            { label: 'Midday Check', value: sched.midday_check || '\u2014' },
            { label: 'EOD Check', value: sched.eod_check || '\u2014' },
            { label: 'Post-Market', value: sched.post_market || '\u2014' },
        ].forEach(f => this._appendRow(schedCard, f.label, f.value));

        const tz = document.createElement('div');
        tz.style.cssText = 'font-size: 0.7rem; color: var(--text-muted); margin-top: 8px;';
        tz.textContent = 'All times Eastern (US/Eastern)';
        schedCard.appendChild(tz);
        grid.appendChild(schedCard);

        // ── Strategy ──────────────────────────────────────────
        const strat = config.swing_strategy || {};
        const stratCard = Components.card({ title: 'Swing Strategy', icon: '\u{1F4C8}', id: 'config-strategy' });
        stratCard.classList.add('bento-grid__full');

        const stratGrid = document.createElement('div');
        stratGrid.style.cssText = 'display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 24px;';

        const stratFields = [
            { label: 'Min Prior Uptrend', value: this._pct(strat.min_prior_uptrend_pct) },
            { label: 'Min RS Percentile', value: this._pct(strat.min_rs_percentile) },
            { label: 'Min ADR', value: this._pct(strat.min_adr_pct) },
            { label: 'Min Price', value: strat.min_price ? `$${strat.min_price}` : '\u2014' },
            { label: 'Max Price', value: strat.max_price ? `$${strat.max_price}` : '\u2014' },
            { label: 'ORH Window', value: strat.orh_window_minutes ? `${strat.orh_window_minutes} min` : '\u2014' },
            { label: 'Day 1 Red Exit', value: strat.day1_red_close_exit ? 'Yes' : 'No' },
            { label: 'Partial Profit Day', value: strat.partial_profit_day || '\u2014' },
            { label: 'Partial Profit %', value: this._pct(strat.partial_profit_pct) },
            { label: 'Trailing MA', value: strat.trailing_ma_period ? `${strat.trailing_ma_period}-day SMA` : '\u2014' },
            { label: 'Trail Exit On', value: strat.trailing_ma_exit_on || '\u2014' },
            { label: 'Max Extension', value: strat.max_extension_adr_multiple ? `${strat.max_extension_adr_multiple}x ADR` : '\u2014' },
        ];

        stratFields.forEach(f => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            row.innerHTML = `<span class="stat-row__label">${f.label}</span><span class="stat-row__value" style="font-family: var(--font-mono); font-size: 0.8rem;">${f.value}</span>`;
            stratGrid.appendChild(row);
        });

        stratCard.appendChild(stratGrid);
        grid.appendChild(stratCard);

        // ── Data Vendors ──────────────────────────────────────
        const dv = config.data_vendors || {};
        const dvCard = Components.card({ title: 'Data Vendors', icon: '\u{1F4E1}', id: 'config-vendors' });
        dvCard.classList.add('bento-grid__full');

        const dvGrid = document.createElement('div');
        dvGrid.style.cssText = 'display: grid; grid-template-columns: repeat(3, 1fr); gap: 0 24px;';

        Object.entries(dv).forEach(([key, value]) => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
            row.innerHTML = `<span class="stat-row__label">${label}</span>`;
            row.appendChild(Components.badge(value, value === 'yfinance' ? 'success' : 'info'));
            dvGrid.appendChild(row);
        });

        dvCard.appendChild(dvGrid);
        grid.appendChild(dvCard);

        container.appendChild(grid);
    },

    _pct(val) {
        if (val == null) return '\u2014';
        return (val * 100).toFixed(1) + '%';
    },

    _appendRow(card, label, value) {
        const row = document.createElement('div');
        row.className = 'stat-row';
        row.innerHTML = `<span class="stat-row__label">${label}</span><span class="stat-row__value" style="font-family: var(--font-mono); font-size: 0.8rem;">${value}</span>`;
        card.appendChild(row);
    },
};

window.ConfigPage = ConfigPage;
