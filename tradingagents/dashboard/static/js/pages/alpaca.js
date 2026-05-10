/**
 * TradingAgents Dashboard — Alpaca Account Page
 *
 * Shows live data from the Alpaca brokerage API:
 * - Account overview (equity, cash, buying power)
 * - Account status and trading permissions
 * - P&L
 * - Live orders with bracket leg expansion (stop/TP prices)
 *
 * Accessed via /alpaca
 */

const AlpacaPage = {
    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'Alpaca Account';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Live brokerage data — the source of truth';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        // Content
        const content = document.createElement('div');
        content.className = 'fade-in';
        content.id = 'alpaca-content';
        content.appendChild(Components.emptyState('\u23F3', 'Loading account data...'));
        container.appendChild(content);

        // Load data
        const account = await API.getAccount();
        this._renderAccount(content, account);
    },

    destroy() {},

    _renderAccount(container, account) {
        container.innerHTML = '';

        if (!account || account.detail) {
            const errorCard = Components.card({ id: 'alpaca-error' });
            const msg = account && account.detail ? account.detail : 'Alpaca client not available';

            const errorContent = document.createElement('div');
            errorContent.style.cssText = 'text-align: center; padding: 40px 0;';

            const icon = document.createElement('div');
            icon.style.cssText = 'font-size: 3rem; margin-bottom: 16px; opacity: 0.4;';
            icon.textContent = '\u{1F50C}';
            errorContent.appendChild(icon);

            const text = document.createElement('div');
            text.style.cssText = 'color: var(--text-secondary); font-size: 0.95rem; max-width: 400px; margin: 0 auto;';
            text.textContent = msg;
            errorContent.appendChild(text);

            const hint = document.createElement('div');
            hint.style.cssText = 'color: var(--text-muted); font-size: 0.8rem; margin-top: 12px;';
            hint.textContent = 'Requires alpaca-py installed and ALPACA_API_KEY / ALPACA_SECRET_KEY set in .env';
            errorContent.appendChild(hint);

            errorCard.appendChild(errorContent);
            container.appendChild(errorCard);
            return;
        }

        // ── Top row: Account cards (2-col grid) ───────────────
        const grid = document.createElement('div');
        grid.className = 'bento-grid bento-grid--2col';

        // Card 1: Portfolio
        const overviewCard = Components.card({ title: 'Portfolio', icon: '\u{1F4B0}', id: 'alpaca-overview' });

        const overviewStats = [
            { label: 'Equity', value: this._fmt(account.equity), large: true },
            { label: 'Cash', value: this._fmt(account.cash) },
            { label: 'Buying Power', value: this._fmt(account.buying_power) },
            { label: 'Portfolio Value', value: this._fmt(account.portfolio_value) },
            { label: 'Long Market Value', value: this._fmt(account.long_market_value) },
            { label: 'Short Market Value', value: this._fmt(account.short_market_value) },
        ];

        overviewStats.forEach(s => {
            if (s.large) {
                overviewCard.appendChild(Components.stat({ value: s.value, label: s.label, large: true }));
                const spacer = document.createElement('div');
                spacer.style.height = '12px';
                overviewCard.appendChild(spacer);
            } else {
                const row = document.createElement('div');
                row.className = 'stat-row';
                row.innerHTML = `<span class="stat-row__label">${s.label}</span><span class="stat-row__value">${s.value}</span>`;
                overviewCard.appendChild(row);
            }
        });

        grid.appendChild(overviewCard);

        // Card 2: Account Status
        const statusCard = Components.card({ title: 'Account Status', icon: '\u{1F6E1}\uFE0F', id: 'alpaca-status' });

        const statusFields = [
            { label: 'Account Number', value: this._str(account.account_number) || '\u2014' },
            { label: 'Status', value: this._str(account.status) || '\u2014', badge: this._statusVariant(account.status) },
            { label: 'Currency', value: this._str(account.currency) || 'USD' },
            { label: 'Pattern Day Trader', value: account.pattern_day_trader ? 'Yes' : 'No', badge: account.pattern_day_trader ? 'danger' : 'success' },
            { label: 'Trading Blocked', value: account.trading_blocked ? 'BLOCKED' : 'Active', badge: account.trading_blocked ? 'danger' : 'success' },
            { label: 'Account Blocked', value: account.account_blocked ? 'BLOCKED' : 'Active', badge: account.account_blocked ? 'danger' : 'success' },
            { label: 'Shorting Enabled', value: account.shorting_enabled ? 'Yes' : 'No' },
            { label: 'Crypto Status', value: this._str(account.crypto_status) || '\u2014' },
        ];

        statusFields.forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            const label = document.createElement('span');
            label.className = 'stat-row__label';
            label.textContent = s.label;
            row.appendChild(label);

            if (s.badge) {
                row.appendChild(Components.badge(s.value, s.badge));
            } else {
                const val = document.createElement('span');
                val.className = 'stat-row__value';
                val.textContent = s.value;
                row.appendChild(val);
            }

            statusCard.appendChild(row);
        });

        grid.appendChild(statusCard);

        // Card 3: P&L
        const pnlCard = Components.card({ title: 'P&L', icon: '\u{1F4C8}', id: 'alpaca-pnl' });

        const lastEquity = parseFloat(account.last_equity) || 0;
        const equity = parseFloat(account.equity) || 0;
        const dailyChange = equity - lastEquity;
        const dailyChangePct = lastEquity > 0 ? (dailyChange / lastEquity * 100) : 0;

        pnlCard.appendChild(Components.stat({
            value: Components.formatMoney(dailyChange),
            label: 'Today\'s Change',
            change: dailyChangePct,
        }));

        const spacer2 = document.createElement('div');
        spacer2.style.height = '12px';
        pnlCard.appendChild(spacer2);

        [
            { label: 'Last Equity (prev close)', value: this._fmt(account.last_equity) },
            { label: 'Pending Transfer In', value: this._fmt(account.pending_transfer_in) },
            { label: 'Pending Transfer Out', value: this._fmt(account.pending_transfer_out) },
            { label: 'Accrued Fees', value: this._fmt(account.accrued_fees) },
        ].forEach(s => {
            const row = document.createElement('div');
            row.className = 'stat-row';
            row.innerHTML = `<span class="stat-row__label">${s.label}</span><span class="stat-row__value">${s.value}</span>`;
            pnlCard.appendChild(row);
        });

        grid.appendChild(pnlCard);

        // Card 4: Raw Data
        const rawCard = Components.card({ title: 'Raw Account Data', icon: '\u{1F4C4}', id: 'alpaca-raw' });

        const toggleBtn = document.createElement('button');
        toggleBtn.className = 'chart-controls__btn';
        toggleBtn.textContent = 'Show Raw JSON';
        toggleBtn.addEventListener('click', () => {
            const pre = document.getElementById('raw-json');
            if (pre) {
                const isHidden = pre.style.display === 'none';
                pre.style.display = isHidden ? '' : 'none';
                toggleBtn.textContent = isHidden ? 'Hide Raw JSON' : 'Show Raw JSON';
            }
        });
        rawCard.appendChild(toggleBtn);

        const pre = document.createElement('pre');
        pre.id = 'raw-json';
        pre.className = 'raw-json';
        pre.style.display = 'none';
        pre.textContent = JSON.stringify(account, null, 2);
        rawCard.appendChild(pre);

        grid.appendChild(rawCard);
        container.appendChild(grid);

        // ── Alpaca Orders Section (full width, below grid) ────
        const ordersSection = document.createElement('div');
        ordersSection.style.marginTop = 'var(--space-lg)';

        // Filter bar
        const filterCard = Components.card({ id: 'alpaca-orders-filters' });

        const filterBar = document.createElement('div');
        filterBar.className = 'filter-bar';

        const statusSelect = document.createElement('select');
        statusSelect.className = 'filter-select';
        statusSelect.id = 'alpaca-order-filter';
        [
            { value: 'all', label: 'All Orders' },
            { value: 'open', label: 'Open Only' },
            { value: 'closed', label: 'Closed (Filled/Cancelled)' },
        ].forEach(opt => {
            const option = document.createElement('option');
            option.value = opt.value;
            option.textContent = opt.label;
            statusSelect.appendChild(option);
        });
        statusSelect.addEventListener('change', () => this._loadAlpacaOrders(statusSelect.value));
        filterBar.appendChild(statusSelect);

        const src = document.createElement('span');
        src.className = 'filter-source';
        src.innerHTML = '<span class="status-dot status-dot--running"></span> Live from Alpaca API';
        filterBar.appendChild(src);

        filterCard.appendChild(filterBar);
        ordersSection.appendChild(filterCard);

        // Orders list
        const ordersCard = Components.card({ title: 'Alpaca Orders', icon: '\u{1F4DC}', id: 'alpaca-orders-card' });
        const ordersList = document.createElement('div');
        ordersList.id = 'alpaca-orders-list';
        ordersList.appendChild(Components.emptyState('\u23F3', 'Loading orders...'));
        ordersCard.appendChild(ordersList);
        ordersSection.appendChild(ordersCard);

        container.appendChild(ordersSection);

        // Load orders
        this._loadAlpacaOrders('all');
    },

    // ── Alpaca Orders ─────────────────────────────────────────

    async _loadAlpacaOrders(status) {
        const data = await API.getAlpacaOrders({ status });
        const container = document.getElementById('alpaca-orders-list');
        if (!container) return;
        container.innerHTML = '';

        if (!data || !data.orders || data.orders.length === 0) {
            container.appendChild(Components.emptyState('\u{1F4DC}', 'No orders in Alpaca'));
            return;
        }

        // Stats summary
        const summary = data.summary || {};
        const statsBar = document.createElement('div');
        statsBar.className = 'orders-stats-row';
        statsBar.style.marginBottom = 'var(--space-md)';

        [
            { label: 'Total', value: summary.total || 0, icon: '\u{1F4CB}' },
            { label: 'Filled', value: summary.filled || 0, icon: '\u2705' },
            { label: 'Open', value: summary.open || 0, icon: '\u{1F7E2}' },
            { label: 'Cancelled', value: summary.canceled || 0, icon: '\u274C' },
            { label: 'Bracket', value: summary.bracket_orders || 0, icon: '\u{1F3AF}' },
        ].forEach(s => {
            const stat = document.createElement('div');
            stat.className = 'orders-stat';
            stat.innerHTML = `
                <span class="orders-stat__icon">${s.icon}</span>
                <div class="orders-stat__value">${s.value}</div>
                <div class="orders-stat__label">${s.label}</div>
            `;
            statsBar.appendChild(stat);
        });
        container.appendChild(statsBar);

        // Order rows
        data.orders.forEach(order => {
            container.appendChild(this._createOrderRow(order));
        });
    },

    _createOrderRow(order) {
        const wrapper = document.createElement('div');
        wrapper.className = 'order-row';

        const main = document.createElement('div');
        main.className = 'order-row__main';

        // Left: symbol + side + type
        const left = document.createElement('div');
        left.className = 'order-row__left';

        const symbol = document.createElement('span');
        symbol.className = 'order-row__symbol';
        symbol.textContent = order.symbol || '\u2014';
        left.appendChild(symbol);

        const side = this._str(order.side);
        left.appendChild(Components.badge(side.toUpperCase(), side.toLowerCase() === 'buy' ? 'success' : 'danger'));

        const type = document.createElement('span');
        type.className = 'order-row__type';
        type.textContent = this._str(order.order_type || order.type).toUpperCase();
        left.appendChild(type);

        if (order.legs && order.legs.length > 0) {
            left.appendChild(Components.badge('BRACKET', 'info'));
        }

        main.appendChild(left);

        // Center: qty and price
        const center = document.createElement('div');
        center.className = 'order-row__center';

        const qty = document.createElement('span');
        qty.className = 'order-row__qty';
        qty.textContent = `${order.filled_qty || 0}/${order.qty || 0} shares`;
        center.appendChild(qty);

        if (order.filled_avg_price) {
            const price = document.createElement('span');
            price.className = 'order-row__price';
            price.textContent = `@ ${Components.formatMoney(order.filled_avg_price)}`;
            center.appendChild(price);
        }

        // Show SL/TP inline if present
        if (order.stop_loss_price) {
            const sl = document.createElement('span');
            sl.className = 'order-row__price text-danger';
            sl.textContent = `SL: ${Components.formatMoney(order.stop_loss_price)}`;
            center.appendChild(sl);
        }
        if (order.take_profit_price) {
            const tp = document.createElement('span');
            tp.className = 'order-row__price text-success';
            tp.textContent = `TP: ${Components.formatMoney(order.take_profit_price)}`;
            center.appendChild(tp);
        }

        main.appendChild(center);

        // Right: status + time
        const right = document.createElement('div');
        right.className = 'order-row__right';

        const status = this._str(order.status);
        right.appendChild(Components.badge(status.toUpperCase(), this._orderStatusVariant(status)));

        const time = document.createElement('span');
        time.className = 'order-row__time';
        time.textContent = this._fmtTime(order.submitted_at || order.created_at);
        right.appendChild(time);

        main.appendChild(right);

        // Click to expand bracket legs
        if (order.legs && order.legs.length > 0) {
            main.style.cursor = 'pointer';
            main.addEventListener('click', () => {
                const detail = wrapper.querySelector('.order-row__legs');
                if (detail) {
                    detail.style.display = detail.style.display === 'none' ? '' : 'none';
                }
            });
        }

        wrapper.appendChild(main);

        // Bracket legs (expandable)
        if (order.legs && order.legs.length > 0) {
            const legsContainer = document.createElement('div');
            legsContainer.className = 'order-row__legs';
            legsContainer.style.display = 'none';

            // Summary chips
            const summary = document.createElement('div');
            summary.className = 'bracket-summary';

            if (order.stop_loss_price) {
                const sl = document.createElement('div');
                sl.className = 'bracket-summary__item bracket-summary__item--stop';
                sl.innerHTML = `
                    <span class="bracket-summary__label">\u{1F6D1} Stop Loss</span>
                    <span class="bracket-summary__price">${Components.formatMoney(order.stop_loss_price)}</span>
                    <span class="bracket-summary__status">${this._str(order.stop_loss_status).toUpperCase()}</span>
                `;
                summary.appendChild(sl);
            }

            if (order.take_profit_price) {
                const tp = document.createElement('div');
                tp.className = 'bracket-summary__item bracket-summary__item--tp';
                tp.innerHTML = `
                    <span class="bracket-summary__label">\u{1F3AF} Take Profit</span>
                    <span class="bracket-summary__price">${Components.formatMoney(order.take_profit_price)}</span>
                    <span class="bracket-summary__status">${this._str(order.take_profit_status).toUpperCase()}</span>
                `;
                summary.appendChild(tp);
            }

            legsContainer.appendChild(summary);

            // Individual legs
            order.legs.forEach(leg => {
                const legRow = document.createElement('div');
                legRow.className = 'order-leg';

                const legType = this._str(leg.order_type || leg.type);
                const isStop = legType.toLowerCase().includes('stop');
                const isLimit = legType.toLowerCase().includes('limit');

                legRow.innerHTML = `
                    <span class="order-leg__type ${isStop ? 'text-danger' : isLimit ? 'text-success' : ''}">${legType.toUpperCase()}</span>
                    <span class="order-leg__side">${this._str(leg.side).toUpperCase()}</span>
                    <span class="order-leg__qty">${leg.qty || 0} shares</span>
                    ${leg.stop_price ? `<span class="order-leg__price text-danger">Stop: ${Components.formatMoney(leg.stop_price)}</span>` : ''}
                    ${leg.limit_price ? `<span class="order-leg__price text-success">Limit: ${Components.formatMoney(leg.limit_price)}</span>` : ''}
                    ${leg.filled_avg_price ? `<span class="order-leg__price">Filled: ${Components.formatMoney(leg.filled_avg_price)}</span>` : ''}
                `;

                const legStatus = this._str(leg.status);
                legRow.appendChild(Components.badge(legStatus.toUpperCase(), this._orderStatusVariant(legStatus)));

                legsContainer.appendChild(legRow);
            });

            wrapper.appendChild(legsContainer);
        }

        return wrapper;
    },

    // ── Helpers ────────────────────────────────────────────────

    _fmt(val) {
        if (val == null || val === '') return '$\u2014';
        if (typeof val === 'object') return '$\u2014';
        const num = parseFloat(val);
        return isNaN(num) ? String(val) : Components.formatMoney(num);
    },

    _str(val) {
        if (val == null) return '';
        if (typeof val === 'string') return val;
        if (typeof val === 'object') {
            if (val.value) return String(val.value);
            const keys = Object.keys(val);
            return keys.length > 0 ? String(val[keys[0]]) : '';
        }
        return String(val);
    },

    _statusVariant(status) {
        const s = this._str(status).toUpperCase();
        if (!s) return 'neutral';
        if (s === 'ACTIVE') return 'success';
        if (s === 'INACTIVE' || s === 'DISABLED') return 'danger';
        return 'warning';
    },

    _orderStatusVariant(status) {
        const s = this._str(status).toLowerCase();
        if (s === 'filled') return 'success';
        if (s === 'new' || s === 'accepted' || s === 'partially_filled') return 'warning';
        if (s === 'canceled' || s === 'cancelled' || s === 'expired') return 'danger';
        return 'neutral';
    },

    _fmtTime(isoStr) {
        if (!isoStr) return '\u2014';
        try {
            return new Date(isoStr).toLocaleDateString('en-US', {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            });
        } catch {
            return isoStr;
        }
    },
};

window.AlpacaPage = AlpacaPage;
