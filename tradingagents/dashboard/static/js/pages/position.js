/**
 * TradingAgents Dashboard — Position Detail Page
 *
 * Detailed view for a single position. Shows:
 * - Position header with key stats
 * - Bracket order card (parent + stop/TP legs) — hero feature
 * - Visual price bar (stop < entry < current < TP)
 * - Position lifecycle timeline
 * - Related orders table
 *
 * Accessed via /positions/{symbol}
 */

const PositionPage = {
    async render(container, symbol) {
        if (!symbol) {
            container.innerHTML = '';
            container.appendChild(Components.emptyState('\u{1F50D}', 'No symbol specified'));
            return;
        }

        container.innerHTML = '';
        symbol = symbol.toUpperCase();

        // Page header with back button
        const header = document.createElement('div');
        header.className = 'page-header fade-in';

        const backRow = document.createElement('div');
        backRow.style.cssText = 'display: flex; align-items: center; gap: 12px;';

        const backBtn = document.createElement('button');
        backBtn.className = 'chart-controls__btn';
        backBtn.textContent = '\u2190 Back';
        backBtn.addEventListener('click', () => App.navigate('/'));
        backRow.appendChild(backBtn);

        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = symbol;
        backRow.appendChild(title);

        header.appendChild(backRow);

        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Position detail with bracket order legs';
        header.appendChild(subtitle);
        container.appendChild(header);

        // Content container
        const content = document.createElement('div');
        content.className = 'position-detail fade-in';
        content.id = 'position-detail';
        content.appendChild(Components.emptyState('\u23F3', 'Loading position data...'));
        container.appendChild(content);

        // Load data
        const data = await API.getPosition(symbol);
        if (!data) {
            content.innerHTML = '';
            content.appendChild(Components.emptyState('\u274C', `Position "${symbol}" not found`));
            return;
        }

        this._renderDetail(content, data, symbol);
    },

    destroy() {},

    _renderDetail(container, data, symbol) {
        container.innerHTML = '';
        const pos = data.position || {};
        const live = data.live || {};
        const bracket = data.bracket || {};
        const orders = data.orders || [];

        const grid = document.createElement('div');
        grid.className = 'bento-grid';

        // ── Card 1: Position Stats ────────────────────────────
        const statsCard = Components.card({ title: 'Position', icon: '\u{1F4CA}', id: 'pos-stats' });

        const entryPrice = pos.entry_price || 0;
        const currentPrice = live.current_price || entryPrice;
        const qty = pos.current_qty || 0;
        const unrealizedPl = live.unrealized_pl || ((currentPrice - entryPrice) * qty);
        const unrealizedPlPct = live.unrealized_plpc || (entryPrice > 0 ? ((currentPrice - entryPrice) / entryPrice * 100) : 0);

        // Stats grid
        const statsGrid = document.createElement('div');
        statsGrid.className = 'pos-stats-grid';

        const statItems = [
            { label: 'Entry Price', value: Components.formatMoney(entryPrice) },
            { label: 'Current Price', value: Components.formatMoney(currentPrice) },
            { label: 'Unrealized P&L', value: Components.formatMoney(unrealizedPl), pnl: unrealizedPl },
            { label: 'P&L %', value: Components.formatPercent(unrealizedPlPct), pnl: unrealizedPlPct },
            { label: 'Shares', value: `${qty} / ${pos.original_qty || qty}` },
            { label: 'Day Count', value: `D${pos.day_count || 1}` },
            { label: 'Entry Date', value: pos.entry_date || '\u2014' },
            { label: 'Market Value', value: Components.formatMoney(live.market_value || currentPrice * qty) },
        ];

        statItems.forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = document.createElement('span');
            label.className = 'stat-row__label';
            label.textContent = s.label;
            row.appendChild(label);
            const value = document.createElement('span');
            value.className = 'stat-row__value';
            if (s.pnl !== undefined) {
                value.className += s.pnl > 0 ? ' text-success' : s.pnl < 0 ? ' text-danger' : '';
            }
            value.textContent = s.value;
            row.appendChild(value);
            statsGrid.appendChild(row);
        });

        statsCard.appendChild(statsGrid);

        // Status badges
        const badges = document.createElement('div');
        badges.style.cssText = 'display: flex; gap: 6px; margin-top: 12px; flex-wrap: wrap;';
        if (pos.trimmed) badges.appendChild(Components.badge('Trimmed', 'warning'));
        if (pos.breakeven_stop_active) badges.appendChild(Components.badge('BE Stop', 'primary'));
        if (pos.trailing_stop_active) badges.appendChild(Components.badge('Trailing', 'info'));
        badges.appendChild(Components.badge(pos.status === 'OPEN' ? 'Open' : 'Closed', pos.status === 'OPEN' ? 'success' : 'neutral'));
        badges.appendChild(Components.badge(pos.pipeline_mode === 'full' ? 'Full LLM' : 'Quant', 'primary'));
        statsCard.appendChild(badges);

        grid.appendChild(statsCard);

        // ── Card 2: Bracket Order Legs ────────────────────────
        const bracketCard = Components.card({ title: 'Bracket Order Legs', icon: '\u{1F3AF}', id: 'pos-bracket' });

        if (bracket.parent_order || bracket.legs) {
            this._renderBracketLegs(bracketCard, bracket, pos);
        } else {
            // Fallback: show entry ORL as stop reference
            const fallback = document.createElement('div');
            fallback.className = 'bracket-card__legs';

            const entryLeg = this._createLeg('Entry (Parent)', Components.formatMoney(entryPrice), 'FILLED', 'parent');
            fallback.appendChild(entryLeg);

            if (pos.entry_orl) {
                const stopLeg = this._createLeg('Initial Stop (ORL)', Components.formatMoney(pos.entry_orl), 'Set from ORL', 'stop-loss');
                fallback.appendChild(stopLeg);
            }

            if (pos.entry_lod) {
                const lodLeg = this._createLeg('Day 1 LOD Stop', Components.formatMoney(pos.entry_lod), 'Post Day-1', 'stop-loss');
                fallback.appendChild(lodLeg);
            }

            const noAlpaca = document.createElement('div');
            noAlpaca.className = 'empty-state__text';
            noAlpaca.style.cssText = 'margin-top: 12px; font-size: 0.75rem; opacity: 0.6;';
            noAlpaca.textContent = 'Live bracket legs available when Alpaca is connected';
            fallback.appendChild(noAlpaca);

            bracketCard.appendChild(fallback);
        }

        grid.appendChild(bracketCard);

        // ── Visual Price Bar (full width) ─────────────────────
        const priceCard = Components.card({ title: 'Price Range', icon: '\u{1F4CF}', id: 'pos-price-bar' });
        priceCard.classList.add('bento-grid__full');
        this._renderPriceBar(priceCard, pos, live);
        grid.appendChild(priceCard);

        // ── Card 3: Position Timeline ─────────────────────────
        const timelineCard = Components.card({ title: 'Lifecycle', icon: '\u{1F552}', id: 'pos-timeline' });
        timelineCard.classList.add('bento-grid__full');
        this._renderTimeline(timelineCard, pos);
        grid.appendChild(timelineCard);

        // ── Related Orders (full width) ───────────────────────
        const ordersCard = Components.card({ title: 'Related Orders', icon: '\u{1F4CB}', id: 'pos-orders' });
        ordersCard.classList.add('bento-grid__full');

        if (orders.length === 0) {
            ordersCard.appendChild(Components.emptyState('\u{1F4DC}', 'No orders recorded'));
        } else {
            const tableScroll = document.createElement('div');
            tableScroll.className = 'table-scroll';

            const headers = ['Time', 'Side', 'Qty', 'Type', 'Price', 'Status', 'Signal'];
            const rows = orders.map(o => [
                o.submitted_at ? new Date(o.submitted_at).toLocaleDateString('en-US', {
                    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
                }) : '\u2014',
                Components.badge(o.side, o.side === 'BUY' ? 'success' : 'danger'),
                (o.qty || 0).toString(),
                o.order_type || '\u2014',
                o.filled_price ? Components.formatMoney(o.filled_price) : '\u2014',
                Components.badge(o.status, o.status === 'FILLED' ? 'success' : o.status === 'CANCELLED' ? 'danger' : 'warning'),
                o.signal || '\u2014',
            ]);

            tableScroll.appendChild(Components.table(headers, rows));
            ordersCard.appendChild(tableScroll);
        }

        grid.appendChild(ordersCard);
        container.appendChild(grid);
    },

    // ── Bracket Legs ──────────────────────────────────────────

    _renderBracketLegs(card, bracket, pos) {
        const legsContainer = document.createElement('div');
        legsContainer.className = 'bracket-card__legs';

        // Parent order
        const parent = bracket.parent_order || {};
        const parentLeg = this._createLeg(
            'Entry (Parent)',
            Components.formatMoney(parent.filled_avg_price || pos.entry_price),
            parent.status || 'FILLED',
            'parent'
        );
        legsContainer.appendChild(parentLeg);

        // Child legs
        const legs = bracket.legs || [];
        legs.forEach(leg => {
            const orderType = (leg.order_type || '').toLowerCase();
            const isStop = orderType.includes('stop');
            const isLimit = orderType.includes('limit');

            const label = isStop ? 'Stop Loss' : isLimit ? 'Take Profit' : `Leg (${leg.order_type})`;
            const price = isStop
                ? Components.formatMoney(leg.stop_price)
                : isLimit
                    ? Components.formatMoney(leg.limit_price)
                    : Components.formatMoney(leg.filled_avg_price || 0);
            const variant = isStop ? 'stop-loss' : 'take-profit';

            legsContainer.appendChild(this._createLeg(label, price, leg.status || 'NEW', variant));
        });

        card.appendChild(legsContainer);
    },

    _createLeg(label, price, status, variant) {
        const leg = document.createElement('div');
        leg.className = `bracket-leg bracket-leg--${variant}`;

        const labelEl = document.createElement('span');
        labelEl.className = 'bracket-leg__label';
        labelEl.textContent = label;
        leg.appendChild(labelEl);

        const priceEl = document.createElement('span');
        priceEl.className = 'bracket-leg__price';
        priceEl.textContent = price;
        leg.appendChild(priceEl);

        const statusEl = Components.badge(
            status,
            status === 'FILLED' ? 'success' : status === 'NEW' ? 'info' : 'neutral'
        );
        statusEl.className += ' bracket-leg__status';
        leg.appendChild(statusEl);

        return leg;
    },

    // ── Visual Price Bar ──────────────────────────────────────

    _renderPriceBar(card, pos, live) {
        const entry = pos.entry_price || 0;
        const current = live.current_price || entry;
        const stop = pos.entry_orl || pos.entry_lod || 0;
        const tp = entry * 1.1; // Default 10% target if no TP set

        if (!entry || entry === 0) {
            card.appendChild(Components.emptyState('\u{1F4CF}', 'No price data'));
            return;
        }

        // Normalize to 0-100% range
        const min = Math.min(stop || entry * 0.9, entry, current) * 0.995;
        const max = Math.max(tp, entry, current) * 1.005;
        const range = max - min;
        const toPercent = (val) => ((val - min) / range * 100);

        // Labels row
        const labels = document.createElement('div');
        labels.style.cssText = 'display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 8px;';

        if (stop) {
            const stopLabel = document.createElement('span');
            stopLabel.className = 'text-danger';
            stopLabel.textContent = `Stop: ${Components.formatMoney(stop)}`;
            labels.appendChild(stopLabel);
        }

        const entryLabel = document.createElement('span');
        entryLabel.className = 'text-secondary';
        entryLabel.textContent = `Entry: ${Components.formatMoney(entry)}`;
        labels.appendChild(entryLabel);

        const currentLabel = document.createElement('span');
        currentLabel.className = current >= entry ? 'text-success' : 'text-danger';
        currentLabel.textContent = `Current: ${Components.formatMoney(current)}`;
        labels.appendChild(currentLabel);

        card.appendChild(labels);

        // Bar
        const bar = document.createElement('div');
        bar.className = 'price-bar';

        // Risk segment (stop to entry)
        if (stop && stop < entry) {
            const risk = document.createElement('div');
            risk.className = 'price-bar__segment price-bar__segment--risk';
            risk.style.left = `${toPercent(stop)}%`;
            risk.style.width = `${toPercent(entry) - toPercent(stop)}%`;
            bar.appendChild(risk);
        }

        // Profit segment (entry to current, if positive)
        if (current > entry) {
            const profit = document.createElement('div');
            profit.className = 'price-bar__segment price-bar__segment--profit';
            profit.style.left = `${toPercent(entry)}%`;
            profit.style.width = `${toPercent(current) - toPercent(entry)}%`;
            bar.appendChild(profit);
        }

        // Markers
        if (stop) {
            const stopMarker = document.createElement('div');
            stopMarker.className = 'price-bar__marker price-bar__marker--stop';
            stopMarker.style.left = `${toPercent(stop)}%`;
            stopMarker.title = `Stop: ${Components.formatMoney(stop)}`;
            bar.appendChild(stopMarker);
        }

        const entryMarker = document.createElement('div');
        entryMarker.className = 'price-bar__marker price-bar__marker--entry';
        entryMarker.style.left = `${toPercent(entry)}%`;
        entryMarker.title = `Entry: ${Components.formatMoney(entry)}`;
        bar.appendChild(entryMarker);

        const currentMarker = document.createElement('div');
        currentMarker.className = 'price-bar__marker price-bar__marker--current';
        currentMarker.style.left = `${toPercent(current)}%`;
        currentMarker.title = `Current: ${Components.formatMoney(current)}`;
        bar.appendChild(currentMarker);

        card.appendChild(bar);
    },

    // ── Position Timeline ─────────────────────────────────────

    _renderTimeline(card, pos) {
        const timeline = document.createElement('div');
        timeline.className = 'timeline';

        const events = [];

        // Entry
        events.push({
            label: 'Opened',
            detail: `${pos.original_qty || pos.current_qty} shares @ ${Components.formatMoney(pos.entry_price)}`,
            date: pos.entry_date,
            variant: 'primary',
        });

        // ORL stop set
        if (pos.entry_orl) {
            events.push({
                label: 'Initial Stop (ORL)',
                detail: Components.formatMoney(pos.entry_orl),
                date: pos.entry_date,
                variant: 'danger',
            });
        }

        // Day 1 LOD
        if (pos.entry_lod) {
            events.push({
                label: 'Day 1 LOD Set',
                detail: `LOD stop: ${Components.formatMoney(pos.entry_lod)}`,
                date: '',
                variant: 'warning',
            });
        }

        // Trim
        if (pos.trimmed) {
            events.push({
                label: 'Trimmed 50%',
                detail: `${pos.current_qty} shares remaining`,
                date: pos.trim_date || '',
                variant: 'warning',
            });
        }

        // BE stop
        if (pos.breakeven_stop_active) {
            events.push({
                label: 'Breakeven Stop Active',
                detail: `Stop moved to entry: ${Components.formatMoney(pos.entry_price)}`,
                date: '',
                variant: 'info',
            });
        }

        // Trailing
        if (pos.trailing_stop_active) {
            events.push({
                label: 'Trailing Stop Active',
                detail: 'Tracking 10-SMA',
                date: '',
                variant: 'success',
            });
        }

        // Closed
        if (pos.status === 'CLOSED') {
            events.push({
                label: 'Closed',
                detail: pos.close_reason || 'Manual',
                date: pos.closed_at || '',
                variant: 'neutral',
            });
        }

        events.forEach((event, i) => {
            const item = document.createElement('div');
            item.className = 'timeline__item';

            const dot = document.createElement('div');
            dot.className = `timeline__dot timeline__dot--${event.variant}`;
            item.appendChild(dot);

            const content = document.createElement('div');
            content.className = 'timeline__content';

            const labelRow = document.createElement('div');
            labelRow.style.cssText = 'display: flex; justify-content: space-between; align-items: center;';

            const label = document.createElement('span');
            label.className = 'timeline__label';
            label.textContent = event.label;
            labelRow.appendChild(label);

            if (event.date) {
                const date = document.createElement('span');
                date.className = 'timeline__date';
                date.textContent = event.date;
                labelRow.appendChild(date);
            }

            content.appendChild(labelRow);

            const detail = document.createElement('div');
            detail.className = 'timeline__detail';
            detail.textContent = event.detail;
            content.appendChild(detail);

            item.appendChild(content);

            // Connector line (except last)
            if (i < events.length - 1) {
                const line = document.createElement('div');
                line.className = 'timeline__line';
                item.appendChild(line);
            }

            timeline.appendChild(item);
        });

        card.appendChild(timeline);
    },
};

window.PositionPage = PositionPage;
