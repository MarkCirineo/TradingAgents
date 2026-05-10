/**
 * TradingAgents Dashboard — A/B Comparison Page
 *
 * Side-by-side comparison of two daemon instances:
 * - Local instance (this one)
 * - Peer instance (configured via PEER_DASHBOARD_URL)
 *
 * Compares portfolio value, P&L, positions, and order counts.
 * Shows "not configured" state when no peer URL is set.
 *
 * Accessed via /ab
 */

const ABPage = {
    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'A/B Comparison';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Side-by-side performance: Full LLM vs Quant-Only';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        // Content
        const content = document.createElement('div');
        content.className = 'fade-in';
        content.id = 'ab-content';
        content.appendChild(Components.emptyState('\u23F3', 'Loading comparison data...'));
        container.appendChild(content);

        const data = await API.getComparison();
        this._renderComparison(content, data);
    },

    destroy() {},

    _renderComparison(container, data) {
        container.innerHTML = '';

        if (!data) {
            container.appendChild(Components.emptyState('\u274C', 'Failed to load comparison data'));
            return;
        }

        const local = data.local || {};
        const peer = data.peer || {};

        // ── Not configured state ──────────────────────────────
        if (!data.peer_configured) {
            this._renderSetupGuide(container, local);
            return;
        }

        // ── Comparison header ─────────────────────────────────
        const headerRow = document.createElement('div');
        headerRow.className = 'ab-header-row';

        // Local label
        const localLabel = this._createInstanceLabel(
            local.label || 'Instance A',
            local.mode || 'full',
            true
        );
        headerRow.appendChild(localLabel);

        // VS
        const vs = document.createElement('div');
        vs.className = 'ab-vs';
        vs.textContent = 'VS';
        headerRow.appendChild(vs);

        // Peer label
        const peerLabel = this._createInstanceLabel(
            peer.label || 'Instance B',
            peer.mode || 'unknown',
            peer.available
        );
        headerRow.appendChild(peerLabel);

        container.appendChild(headerRow);

        // ── Comparison cards ──────────────────────────────────
        const grid = document.createElement('div');
        grid.className = 'bento-grid bento-grid--2col';

        // Portfolio Value
        grid.appendChild(this._createCompareCard(
            'Portfolio Value', '\u{1F4B0}',
            this._fmtMoney(local.portfolio?.portfolio_value),
            peer.available ? this._fmtMoney(peer.portfolio?.portfolio_value) : '\u2014',
            local.portfolio?.portfolio_value,
            peer.portfolio?.portfolio_value
        ));

        // Daily P&L
        grid.appendChild(this._createCompareCard(
            'Daily P&L', '\u{1F4C8}',
            this._fmtMoney(local.portfolio?.daily_pnl),
            peer.available ? this._fmtMoney(peer.portfolio?.daily_pnl) : '\u2014',
            local.portfolio?.daily_pnl,
            peer.portfolio?.daily_pnl,
            true // pnl coloring
        ));

        // Open Positions
        grid.appendChild(this._createCompareCard(
            'Open Positions', '\u{1F4CA}',
            local.positions?.open?.toString() || '0',
            peer.available ? (peer.positions?.open?.toString() || '0') : '\u2014'
        ));

        // Filled Orders
        grid.appendChild(this._createCompareCard(
            'Filled Orders', '\u{1F4CB}',
            local.orders?.filled?.toString() || '0',
            peer.available ? (peer.orders?.filled?.toString() || '0') : '\u2014'
        ));

        container.appendChild(grid);

        // ── Detail cards ──────────────────────────────────────
        const detailGrid = document.createElement('div');
        detailGrid.className = 'bento-grid bento-grid--2col';
        detailGrid.style.marginTop = 'var(--space-lg)';

        // Local positions
        const localPosCard = Components.card({ title: `${local.label || 'Local'} — Positions`, icon: '\u{1F4CA}', id: 'ab-local-pos' });
        if (local.positions?.symbols?.length > 0) {
            const badges = document.createElement('div');
            badges.style.cssText = 'display: flex; gap: 6px; flex-wrap: wrap;';
            local.positions.symbols.forEach(sym => {
                badges.appendChild(Components.badge(sym, 'primary'));
            });
            localPosCard.appendChild(badges);
        } else {
            localPosCard.appendChild(Components.emptyState('\u{1F4CA}', 'No open positions'));
        }
        const localStats = document.createElement('div');
        localStats.style.marginTop = 'var(--space-md)';
        [
            { label: 'Cash', value: this._fmtMoney(local.portfolio?.cash) },
            { label: 'Total Orders', value: local.orders?.total || 0 },
            { label: 'Buys', value: local.orders?.buys || 0 },
            { label: 'Sells', value: local.orders?.sells || 0 },
            { label: 'Closed Positions', value: local.positions?.closed || 0 },
        ].forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            row.innerHTML = `<span class="stat-row__label">${s.label}</span><span class="stat-row__value">${s.value}</span>`;
            localStats.appendChild(row);
        });
        localPosCard.appendChild(localStats);
        detailGrid.appendChild(localPosCard);

        // Peer positions
        const peerPosCard = Components.card({ title: `${peer.label || 'Peer'} — Positions`, icon: '\u{1F4CA}', id: 'ab-peer-pos' });
        if (peer.available && peer.positions?.symbols?.length > 0) {
            const badges = document.createElement('div');
            badges.style.cssText = 'display: flex; gap: 6px; flex-wrap: wrap;';
            peer.positions.symbols.forEach(sym => {
                badges.appendChild(Components.badge(sym, 'info'));
            });
            peerPosCard.appendChild(badges);
        } else {
            peerPosCard.appendChild(Components.emptyState(
                peer.available ? '\u{1F4CA}' : '\u{1F50C}',
                peer.available ? 'No open positions' : 'Peer not available'
            ));
        }
        if (peer.available) {
            const peerStats = document.createElement('div');
            peerStats.style.marginTop = 'var(--space-md)';
            [
                { label: 'Cash', value: this._fmtMoney(peer.portfolio?.cash) },
                { label: 'Total Orders', value: peer.orders?.total || 0 },
                { label: 'Filled', value: peer.orders?.filled || 0 },
                { label: 'Cancelled', value: peer.orders?.cancelled || 0 },
            ].forEach(s => {
                const row = document.createElement('div');
                row.className = 'stat-row';
                row.innerHTML = `<span class="stat-row__label">${s.label}</span><span class="stat-row__value">${s.value}</span>`;
                peerStats.appendChild(row);
            });
            peerPosCard.appendChild(peerStats);
        }
        detailGrid.appendChild(peerPosCard);

        container.appendChild(detailGrid);
    },

    // ── Setup guide (no peer configured) ──────────────────────

    _renderSetupGuide(container, local) {
        const card = Components.card({ title: 'A/B Testing Setup', icon: '\u{1F52C}', id: 'ab-setup' });

        // Show local instance info
        const localInfo = document.createElement('div');
        localInfo.style.marginBottom = 'var(--space-lg)';
        localInfo.innerHTML = `
            <div style="margin-bottom: 12px;">
                <span style="font-size: 0.8rem; color: var(--text-muted);">This Instance</span>
                <div style="font-size: 1.25rem; font-weight: 700; color: var(--text-primary);">${local.label || 'Instance A'}</div>
                <div style="margin-top: 4px;">${Components.badge((local.mode || 'full') === 'full' ? 'Full LLM' : 'Quant Only', 'primary').outerHTML}</div>
            </div>
        `;

        [
            { label: 'Portfolio Value', value: this._fmtMoney(local.portfolio?.portfolio_value) },
            { label: 'Open Positions', value: local.positions?.open || 0 },
            { label: 'Filled Orders', value: local.orders?.filled || 0 },
        ].forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            row.innerHTML = `<span class="stat-row__label">${s.label}</span><span class="stat-row__value">${s.value}</span>`;
            localInfo.appendChild(row);
        });

        card.appendChild(localInfo);

        // Setup instructions
        const instructions = document.createElement('div');
        instructions.style.cssText = 'background: var(--bg-elevated); border-radius: var(--radius-md); padding: var(--space-md); margin-top: var(--space-md);';
        instructions.innerHTML = `
            <div style="font-weight: 600; color: var(--text-primary); margin-bottom: 8px;">\u{1F4CB} To enable A/B comparison:</div>
            <div style="font-size: 0.8rem; color: var(--text-secondary); line-height: 1.8;">
                1. Run a second daemon instance on a different port<br>
                2. Add to this instance's <code style="background: var(--bg-card); padding: 2px 6px; border-radius: 4px;">.env</code>:<br>
                <code style="background: var(--bg-card); padding: 4px 8px; border-radius: 4px; display: inline-block; margin: 4px 0 4px 16px; font-family: var(--font-mono);">PEER_DASHBOARD_URL=http://localhost:8051</code><br>
                3. Optionally set labels:<br>
                <code style="background: var(--bg-card); padding: 4px 8px; border-radius: 4px; display: inline-block; margin: 4px 0 4px 16px; font-family: var(--font-mono);">INSTANCE_LABEL=Full LLM</code><br>
                <code style="background: var(--bg-card); padding: 4px 8px; border-radius: 4px; display: inline-block; margin: 4px 0 4px 16px; font-family: var(--font-mono);">PEER_LABEL=Quant Only</code><br>
                4. Restart the dashboard
            </div>
        `;
        card.appendChild(instructions);

        container.appendChild(card);
    },

    // ── Component builders ─────────────────────────────────────

    _createInstanceLabel(label, mode, available) {
        const el = document.createElement('div');
        el.className = 'ab-instance';

        const dot = document.createElement('span');
        dot.className = `status-dot status-dot--${available ? 'running' : 'stopped'}`;
        el.appendChild(dot);

        const name = document.createElement('span');
        name.className = 'ab-instance__name';
        name.textContent = label;
        el.appendChild(name);

        el.appendChild(Components.badge(
            mode === 'full' ? 'Full LLM' : mode === 'quant' ? 'Quant' : mode,
            'primary'
        ));

        return el;
    },

    _createCompareCard(title, icon, localVal, peerVal, localNum, peerNum, isPnl) {
        const card = Components.card({ title, icon });

        const row = document.createElement('div');
        row.className = 'ab-compare-row';

        // Local value
        const localEl = document.createElement('div');
        localEl.className = 'ab-compare-value';
        if (isPnl && localNum != null) {
            localEl.className += localNum > 0 ? ' text-success' : localNum < 0 ? ' text-danger' : '';
        }
        localEl.textContent = localVal;
        row.appendChild(localEl);

        // Divider
        const divider = document.createElement('div');
        divider.className = 'ab-compare-divider';
        row.appendChild(divider);

        // Peer value
        const peerEl = document.createElement('div');
        peerEl.className = 'ab-compare-value';
        if (isPnl && peerNum != null) {
            peerEl.className += peerNum > 0 ? ' text-success' : peerNum < 0 ? ' text-danger' : '';
        }
        peerEl.textContent = peerVal;
        row.appendChild(peerEl);

        card.appendChild(row);

        // Winner indicator
        if (localNum != null && peerNum != null && localNum !== peerNum) {
            const winner = document.createElement('div');
            winner.className = 'ab-winner';
            const diff = localNum - peerNum;
            winner.textContent = diff > 0 ? '\u2190 Local leads' : 'Peer leads \u2192';
            winner.className += diff > 0 ? ' text-success' : ' text-danger';
            card.appendChild(winner);
        }

        return card;
    },

    _fmtMoney(val) {
        if (val == null) return '$\u2014';
        return Components.formatMoney(val);
    },
};

window.ABPage = ABPage;
