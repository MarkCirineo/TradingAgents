/**
 * TradingAgents Dashboard — Position Detail Page
 *
 * Detailed view for a single position. Shows:
 * - Position stats (actual fill price from Alpaca, P&L, day count)
 * - Protection card: initial stop (pivot floor) vs current live stop,
 *   risk per share and current R-multiple
 * - Visual price bar (stops < entry < current)
 * - Lifecycle timeline derived from real Alpaca order history
 * - Full Alpaca order history for the symbol (entry, stop raises, exits)
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
        backBtn.textContent = '← Back';
        backBtn.addEventListener('click', () => App.navigate('/'));
        backRow.appendChild(backBtn);

        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = symbol;
        backRow.appendChild(title);

        header.appendChild(backRow);

        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.id = 'position-subtitle';
        subtitle.textContent = 'Position detail';
        header.appendChild(subtitle);
        container.appendChild(header);

        // Content container
        const content = document.createElement('div');
        content.className = 'position-detail fade-in';
        content.id = 'position-detail';
        content.appendChild(Components.emptyState('⏳', 'Loading position data...'));
        container.appendChild(content);

        // Load data
        const data = await API.getPosition(symbol);
        if (!data) {
            content.innerHTML = '';
            content.appendChild(Components.emptyState('❌', `Position "${symbol}" not found`));
            return;
        }

        this._renderDetail(content, data, symbol);
    },

    destroy() {},

    // ── Helpers ───────────────────────────────────────────────

    _formatTime(ts) {
        if (!ts) return '—';
        // Date-only strings (e.g. "2026-07-17") must not go through
        // Date() — it treats them as UTC midnight and shifts the day.
        if (/^\d{4}-\d{2}-\d{2}$/.test(ts)) return ts;
        const d = new Date(ts);
        if (isNaN(d.getTime())) return ts;
        return d.toLocaleString('en-US', {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    },

    _statusBadge(status) {
        const s = String(status || '').replace('OrderStatus.', '').toUpperCase();
        if (s === 'FILLED') return Components.badge('Filled', 'success');
        if (['NEW', 'ACCEPTED', 'HELD', 'PENDING_NEW', 'PARTIALLY_FILLED'].includes(s)) {
            return Components.badge(s === 'PARTIALLY_FILLED' ? 'Partial' : 'Working', 'info');
        }
        if (['CANCELED', 'CANCELLED', 'EXPIRED', 'REPLACED', 'REJECTED'].includes(s)) {
            return Components.badge(s.charAt(0) + s.slice(1).toLowerCase(), 'neutral');
        }
        return Components.badge(s || '—', 'neutral');
    },

    _int(v) {
        const n = parseInt(v, 10);
        return isNaN(n) ? 0 : n;
    },

    // ── Main render ───────────────────────────────────────────

    _renderDetail(container, data, symbol) {
        container.innerHTML = '';
        const pos = data.position || {};
        const live = data.live || {};
        const protection = data.protection || {};
        const orders = data.orders || [];
        const events = data.events || [];
        const isPending = pos.status === 'PENDING';

        const subtitle = document.getElementById('position-subtitle');
        if (subtitle) {
            subtitle.textContent = isPending
                ? 'Entry order submitted — waiting for pivot breakout'
                : 'Position detail — live data from Alpaca';
        }

        const grid = document.createElement('div');
        grid.className = 'bento-grid';

        this._renderStatsCard(grid, pos, live, isPending);
        this._renderProtectionCard(grid, pos, protection, live, isPending);
        this._renderPriceBar(grid, pos, protection, live);
        this._renderTimeline(grid, events);
        this._renderOrderHistory(grid, orders);

        container.appendChild(grid);
    },

    // ── Card 1: Position Stats ────────────────────────────────

    _renderStatsCard(grid, pos, live, isPending) {
        const card = Components.card({ title: 'Position', icon: '\u{1F4CA}', id: 'pos-stats' });

        const entryPrice = pos.entry_price || 0;
        const qty = pos.current_qty || 0;

        const statItems = [
            {
                label: isPending ? 'Entry Trigger (intended)' : 'Entry Price (avg fill)',
                value: Components.formatMoney(entryPrice),
            },
            {
                label: 'Current Price',
                value: live.current_price ? Components.formatMoney(live.current_price) : '—',
            },
            {
                label: 'Unrealized P&L',
                value: isPending || live.unrealized_pl === undefined
                    ? '—' : Components.formatMoney(live.unrealized_pl),
                pnl: isPending ? undefined : live.unrealized_pl,
            },
            {
                label: 'P&L %',
                value: isPending || live.unrealized_plpc === undefined
                    ? '—' : Components.formatPercent(live.unrealized_plpc),
                pnl: isPending ? undefined : live.unrealized_plpc,
            },
            { label: 'Shares', value: `${qty} / ${pos.original_qty || qty}` },
            { label: 'Day Count', value: isPending ? '—' : `D${pos.day_count || 1}` },
            { label: 'Entry Date', value: pos.entry_date || '—' },
            {
                label: 'Market Value',
                value: live.market_value ? Components.formatMoney(live.market_value) : '—',
            },
        ];

        const statsGrid = document.createElement('div');
        statsGrid.className = 'pos-stats-grid';

        statItems.forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = document.createElement('span');
            label.className = 'stat-row__label';
            label.textContent = s.label;
            row.appendChild(label);
            const value = document.createElement('span');
            value.className = 'stat-row__value';
            if (s.pnl !== undefined && s.pnl !== null) {
                value.className += s.pnl > 0 ? ' text-success' : s.pnl < 0 ? ' text-danger' : '';
            }
            value.textContent = s.value;
            row.appendChild(value);
            statsGrid.appendChild(row);
        });

        card.appendChild(statsGrid);

        // Status badges
        const badges = document.createElement('div');
        badges.style.cssText = 'display: flex; gap: 6px; margin-top: 12px; flex-wrap: wrap;';

        const statusMap = {
            OPEN: ['Open', 'success'],
            PENDING: ['Pending Fill', 'warning'],
            CLOSED: ['Closed', 'neutral'],
            CANCELLED: ['Entry Cancelled', 'neutral'],
        };
        const [statusLabel, statusVariant] = statusMap[pos.status] || ['Open', 'success'];
        badges.appendChild(Components.badge(statusLabel, statusVariant));
        badges.appendChild(
            Components.badge(pos.pipeline_mode === 'full' ? 'LLM' : 'Quant', 'primary')
        );
        if (pos.trimmed) badges.appendChild(Components.badge('Trimmed', 'warning'));
        if (pos.breakeven_stop_active) badges.appendChild(Components.badge('BE Stop', 'primary'));
        if (pos.trailing_stop_active) badges.appendChild(Components.badge('Trailing 10SMA', 'info'));
        card.appendChild(badges);

        grid.appendChild(card);
    },

    // ── Card 2: Protection ────────────────────────────────────

    _renderProtectionCard(grid, pos, protection, live, isPending) {
        const card = Components.card({ title: 'Protection', icon: '\u{1F6E1}', id: 'pos-protection' });

        const legs = document.createElement('div');
        legs.className = 'bracket-card__legs';

        if (isPending) {
            // Entry order still working — bracket stop activates on fill
            legs.appendChild(this._createLeg(
                'Entry (buy-stop at pivot)',
                Components.formatMoney(pos.entry_price),
                'Waiting for breakout', 'parent'
            ));
            if (protection.initial_stop) {
                legs.appendChild(this._createLeg(
                    'Stop on fill (pivot floor)',
                    Components.formatMoney(protection.initial_stop),
                    'Bracket leg', 'stop-loss'
                ));
            }
        } else {
            // Current live stop
            const stopOrder = protection.stop_order;
            if (stopOrder) {
                const tif = String(stopOrder.time_in_force || 'GTC')
                    .replace('TimeInForce.', '').toUpperCase();
                legs.appendChild(this._createLeg(
                    'Current Stop',
                    Components.formatMoney(protection.current_stop),
                    `${tif} · ${this._int(stopOrder.qty)} shares`,
                    'stop-loss'
                ));
            } else if (protection.current_stop) {
                legs.appendChild(this._createLeg(
                    'Current Stop (tracked)',
                    Components.formatMoney(protection.current_stop),
                    'No live order', 'stop-loss'
                ));
            }

            // Initial stop reference (only when it differs from current)
            if (protection.initial_stop &&
                protection.initial_stop !== protection.current_stop) {
                const initialLeg = this._createLeg(
                    'Initial Stop (pivot floor)',
                    Components.formatMoney(protection.initial_stop),
                    'At entry', 'parent'
                );
                initialLeg.style.opacity = '0.65';
                legs.appendChild(initialLeg);
            }
        }

        card.appendChild(legs);

        // No-stop warning for open positions
        if (!isPending && pos.status === 'OPEN' && !protection.stop_order) {
            const warning = document.createElement('div');
            warning.style.cssText =
                'margin-top: 10px; font-size: 0.78rem; color: var(--danger, #e5484d);';
            warning.textContent =
                '⚠ No live stop order at Alpaca — daemon safety net will replace it at post-market.';
            card.appendChild(warning);
        }

        // Risk metrics
        const metrics = document.createElement('div');
        metrics.style.marginTop = '12px';

        const rows = [];
        if (protection.risk_per_share) {
            rows.push(['Risk / share (entry → initial stop)',
                Components.formatMoney(protection.risk_per_share)]);
        }
        if (protection.r_multiple !== null && protection.r_multiple !== undefined) {
            rows.push(['Current R-multiple',
                `${protection.r_multiple > 0 ? '+' : ''}${protection.r_multiple.toFixed(2)}R`]);
        }
        if (protection.current_stop && live.current_price) {
            const dist = ((live.current_price - protection.current_stop) / live.current_price) * 100;
            rows.push(['Distance to stop', `${dist.toFixed(1)}%`]);
        }

        rows.forEach(([label, value]) => {
            const row = document.createElement('div');
            row.className = 'stat-row';

            const labelEl = document.createElement('span');
            labelEl.className = 'stat-row__label';
            labelEl.textContent = label;
            row.appendChild(labelEl);

            const valueEl = document.createElement('span');
            valueEl.className = 'stat-row__value';
            valueEl.textContent = value;
            row.appendChild(valueEl);

            metrics.appendChild(row);
        });
        card.appendChild(metrics);

        grid.appendChild(card);
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
            status === 'FILLED' ? 'success' : 'neutral'
        );
        statusEl.className += ' bracket-leg__status';
        leg.appendChild(statusEl);

        return leg;
    },

    // ── Visual Price Bar ──────────────────────────────────────

    _renderPriceBar(grid, pos, protection, live) {
        const card = Components.card({ title: 'Price Range', icon: '\u{1F4CF}', id: 'pos-price-bar' });
        card.classList.add('bento-grid__full');

        const entry = pos.entry_price || 0;
        const current = live.current_price || 0;
        const currentStop = protection.current_stop || 0;
        const initialStop = protection.initial_stop || 0;

        if (!entry || !current) {
            card.appendChild(Components.emptyState('\u{1F4CF}',
                pos.status === 'PENDING' ? 'Waiting for entry fill' : 'No live price data'));
            grid.appendChild(card);
            return;
        }

        const lowStop = Math.min(currentStop || entry, initialStop || entry);
        const min = Math.min(lowStop, entry, current) * 0.995;
        const max = Math.max(entry, current) * 1.005;
        const range = max - min || 1;
        const toPercent = (val) => ((val - min) / range * 100);

        // Labels row
        const labels = document.createElement('div');
        labels.style.cssText =
            'display: flex; justify-content: space-between; font-size: 0.75rem; margin-bottom: 8px;';

        if (currentStop) {
            const stopLabel = document.createElement('span');
            stopLabel.className = 'text-danger';
            stopLabel.textContent = `Stop: ${Components.formatMoney(currentStop)}`;
            labels.appendChild(stopLabel);
        }

        const entryLabel = document.createElement('span');
        entryLabel.className = 'text-secondary';
        entryLabel.textContent = `Entry: ${Components.formatMoney(entry)}`;
        labels.appendChild(entryLabel);

        const currentLabel = document.createElement('span');
        currentLabel.className = current >= entry ? 'text-success' : 'text-danger';
        let currentText = `Current: ${Components.formatMoney(current)}`;
        if (protection.r_multiple !== null && protection.r_multiple !== undefined) {
            currentText += ` (${protection.r_multiple > 0 ? '+' : ''}${protection.r_multiple.toFixed(2)}R)`;
        }
        currentLabel.textContent = currentText;
        labels.appendChild(currentLabel);

        card.appendChild(labels);

        // Bar
        const bar = document.createElement('div');
        bar.className = 'price-bar';

        // Risk segment (current stop to entry)
        if (currentStop && currentStop < entry) {
            const risk = document.createElement('div');
            risk.className = 'price-bar__segment price-bar__segment--risk';
            risk.style.left = `${toPercent(currentStop)}%`;
            risk.style.width = `${toPercent(entry) - toPercent(currentStop)}%`;
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

        // Markers: initial stop (faded), current stop, entry, current
        if (initialStop && initialStop !== currentStop) {
            const m = document.createElement('div');
            m.className = 'price-bar__marker price-bar__marker--stop';
            m.style.left = `${toPercent(initialStop)}%`;
            m.style.opacity = '0.4';
            m.title = `Initial stop: ${Components.formatMoney(initialStop)}`;
            bar.appendChild(m);
        }
        if (currentStop) {
            const m = document.createElement('div');
            m.className = 'price-bar__marker price-bar__marker--stop';
            m.style.left = `${toPercent(currentStop)}%`;
            m.title = `Current stop: ${Components.formatMoney(currentStop)}`;
            bar.appendChild(m);
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
        grid.appendChild(card);
    },

    // ── Lifecycle Timeline (server-derived events) ────────────

    _renderTimeline(grid, events) {
        const card = Components.card({ title: 'Lifecycle', icon: '\u{1F552}', id: 'pos-timeline' });
        card.classList.add('bento-grid__full');

        if (!events || events.length === 0) {
            card.appendChild(Components.emptyState('\u{1F552}', 'No lifecycle events'));
            grid.appendChild(card);
            return;
        }

        const timeline = document.createElement('div');
        timeline.className = 'timeline';

        events.forEach((event, i) => {
            const item = document.createElement('div');
            item.className = 'timeline__item';

            const dot = document.createElement('div');
            dot.className = `timeline__dot timeline__dot--${event.variant || 'neutral'}`;
            item.appendChild(dot);

            const content = document.createElement('div');
            content.className = 'timeline__content';

            const labelRow = document.createElement('div');
            labelRow.style.cssText =
                'display: flex; justify-content: space-between; align-items: center;';

            const label = document.createElement('span');
            label.className = 'timeline__label';
            label.textContent = event.label;
            labelRow.appendChild(label);

            if (event.ts) {
                const date = document.createElement('span');
                date.className = 'timeline__date';
                date.textContent = this._formatTime(event.ts);
                labelRow.appendChild(date);
            }

            content.appendChild(labelRow);

            const detail = document.createElement('div');
            detail.className = 'timeline__detail';
            detail.textContent = event.detail;
            content.appendChild(detail);

            item.appendChild(content);

            if (i < events.length - 1) {
                const line = document.createElement('div');
                line.className = 'timeline__line';
                item.appendChild(line);
            }

            timeline.appendChild(item);
        });

        card.appendChild(timeline);
        grid.appendChild(card);
    },

    // ── Order History (full Alpaca history for this symbol) ───

    _renderOrderHistory(grid, orders) {
        const card = Components.card({ title: 'Order History', icon: '\u{1F4CB}', id: 'pos-orders' });
        card.classList.add('bento-grid__full');

        if (!orders || orders.length === 0) {
            card.appendChild(Components.emptyState('\u{1F4DC}', 'No orders at Alpaca for this symbol'));
            grid.appendChild(card);
            return;
        }

        const tableScroll = document.createElement('div');
        tableScroll.className = 'table-scroll';

        const headers = ['Time', 'Side', 'Type', 'Qty', 'Trigger / Limit', 'Fill', 'Status'];
        const rows = [];
        orders.forEach(order => {
            rows.push(this._orderRow(order, false));
            (order.legs || []).forEach(leg => rows.push(this._orderRow(leg, true)));
        });

        tableScroll.appendChild(Components.table(headers, rows));
        card.appendChild(tableScroll);
        grid.appendChild(card);
    },

    _orderRow(order, isLeg) {
        const side = String(order.side || '').replace('OrderSide.', '').toUpperCase();
        const type = String(order.order_type || order.type || '')
            .replace('OrderType.', '').toUpperCase();

        const typeCell = document.createElement('span');
        typeCell.textContent = (isLeg ? '↳ ' : '') + type;
        if (isLeg) typeCell.style.opacity = '0.7';

        let trigger = '—';
        if (order.stop_price) trigger = `stop ${Components.formatMoney(order.stop_price)}`;
        else if (order.limit_price) trigger = `limit ${Components.formatMoney(order.limit_price)}`;

        return [
            this._formatTime(order.submitted_at),
            Components.badge(side, side === 'BUY' ? 'success' : 'danger'),
            typeCell,
            `${this._int(order.filled_qty)}/${this._int(order.qty)}`,
            trigger,
            order.filled_avg_price ? Components.formatMoney(order.filled_avg_price) : '—',
            this._statusBadge(order.status),
        ];
    },
};

window.PositionPage = PositionPage;
