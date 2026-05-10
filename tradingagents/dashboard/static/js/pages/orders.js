/**
 * TradingAgents Dashboard — Orders Page
 *
 * Full order history with filtering, bracket expansion,
 * and CSV export. Shows summary stats at the top.
 */

const OrdersPage = {
    _filters: { status: '', symbol: '' },

    async render(container) {
        container.innerHTML = '';

        // Page header
        const header = document.createElement('div');
        header.className = 'page-header fade-in';
        const title = document.createElement('h1');
        title.className = 'page-header__title';
        title.textContent = 'Orders';
        const subtitle = document.createElement('div');
        subtitle.className = 'page-header__subtitle';
        subtitle.textContent = 'Complete order history with bracket leg details';
        header.appendChild(title);
        header.appendChild(subtitle);
        container.appendChild(header);

        // Summary stats row
        const statsCard = Components.card({ id: 'orders-stats' });
        statsCard.classList.add('fade-in');
        const statsRow = document.createElement('div');
        statsRow.className = 'orders-stats-row';
        statsRow.id = 'orders-stats-row';
        statsCard.appendChild(statsRow);
        container.appendChild(statsCard);

        // Filter bar
        const filterCard = Components.card({ id: 'orders-filters' });
        filterCard.classList.add('fade-in');
        filterCard.appendChild(this._buildFilterBar());
        container.appendChild(filterCard);

        // Orders table
        const tableCard = Components.card({ title: 'Order History', icon: '\u{1F4DC}', id: 'orders-table-card' });
        tableCard.classList.add('fade-in');
        const tableScroll = document.createElement('div');
        tableScroll.className = 'table-scroll';
        const tableContainer = document.createElement('div');
        tableContainer.id = 'orders-table';
        tableContainer.appendChild(Components.emptyState('\u{1F4DC}', 'No orders found'));
        tableScroll.appendChild(tableContainer);
        tableCard.appendChild(tableScroll);
        container.appendChild(tableCard);

        await this.refresh();
    },

    async refresh() {
        const params = {};
        if (this._filters.status) params.status = this._filters.status;
        if (this._filters.symbol) params.symbol = this._filters.symbol;

        const data = await API.getOrders(params);
        if (data) {
            this._updateStats(data.summary || {});
            this._updateTable(data.orders || []);
        }
    },

    destroy() {},

    // ── Filter Bar ────────────────────────────────────────────

    _buildFilterBar() {
        const bar = document.createElement('div');
        bar.className = 'filter-bar';

        // Status filter
        const statusSelect = document.createElement('select');
        statusSelect.className = 'filter-select';
        statusSelect.id = 'filter-status';
        [
            { value: '', label: 'All Statuses' },
            { value: 'FILLED', label: 'Filled' },
            { value: 'CANCELLED', label: 'Cancelled' },
            { value: 'SUBMITTED', label: 'Submitted' },
            { value: 'EXPIRED', label: 'Expired' },
        ].forEach(opt => {
            const option = document.createElement('option');
            option.value = opt.value;
            option.textContent = opt.label;
            statusSelect.appendChild(option);
        });
        statusSelect.addEventListener('change', () => {
            this._filters.status = statusSelect.value;
            this.refresh();
        });
        bar.appendChild(statusSelect);

        // Symbol search
        const symbolInput = document.createElement('input');
        symbolInput.className = 'filter-input';
        symbolInput.id = 'filter-symbol';
        symbolInput.type = 'text';
        symbolInput.placeholder = 'Filter by symbol...';
        let debounce;
        symbolInput.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                this._filters.symbol = symbolInput.value.trim();
                this.refresh();
            }, 300);
        });
        bar.appendChild(symbolInput);

        // CSV export button
        const exportBtn = document.createElement('button');
        exportBtn.className = 'chart-controls__btn';
        exportBtn.textContent = '\u{1F4E5} Export CSV';
        exportBtn.addEventListener('click', () => {
            const params = new URLSearchParams();
            if (this._filters.status) params.set('status', this._filters.status);
            if (this._filters.symbol) params.set('symbol', this._filters.symbol);
            window.open(`/api/orders/export/csv?${params}`, '_blank');
        });
        bar.appendChild(exportBtn);

        return bar;
    },

    // ── Stats ─────────────────────────────────────────────────

    _updateStats(summary) {
        const row = document.getElementById('orders-stats-row');
        if (!row) return;
        row.innerHTML = '';

        const stats = [
            { label: 'Total', value: summary.total || 0, icon: '\u{1F4CB}' },
            { label: 'Filled', value: summary.filled || 0, icon: '\u2705' },
            { label: 'Cancelled', value: summary.cancelled || 0, icon: '\u274C' },
            { label: 'Buys', value: summary.buys || 0, icon: '\u{1F7E2}' },
            { label: 'Sells', value: summary.sells || 0, icon: '\u{1F534}' },
        ];

        stats.forEach(s => {
            const stat = document.createElement('div');
            stat.className = 'orders-stat';

            const icon = document.createElement('span');
            icon.className = 'orders-stat__icon';
            icon.textContent = s.icon;
            stat.appendChild(icon);

            const value = document.createElement('div');
            value.className = 'orders-stat__value';
            value.textContent = s.value;
            stat.appendChild(value);

            const label = document.createElement('div');
            label.className = 'orders-stat__label';
            label.textContent = s.label;
            stat.appendChild(label);

            row.appendChild(stat);
        });
    },

    // ── Table ─────────────────────────────────────────────────

    _updateTable(orders) {
        const container = document.getElementById('orders-table');
        if (!container) return;
        container.innerHTML = '';

        if (!orders || orders.length === 0) {
            container.appendChild(Components.emptyState('\u{1F4DC}', 'No orders found'));
            return;
        }

        const headers = ['Time', 'Symbol', 'Side', 'Qty', 'Type', 'Price', 'Signal', 'Status', 'Guardrail'];
        const rows = orders.map(o => {
            // Format timestamp
            const time = o.submitted_at ? new Date(o.submitted_at).toLocaleDateString('en-US', {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
            }) : '\u2014';

            // Side badge
            const sideBadge = Components.badge(
                o.side || '\u2014',
                o.side === 'BUY' ? 'success' : 'danger'
            );

            // Status badge
            let statusVariant = 'neutral';
            if (o.status === 'FILLED') statusVariant = 'success';
            else if (o.status === 'CANCELLED') statusVariant = 'danger';
            else if (o.status === 'SUBMITTED') statusVariant = 'warning';
            const statusBadge = Components.badge(o.status || '\u2014', statusVariant);

            // Signal badge
            let signalVariant = 'neutral';
            if (o.signal === 'Buy' || o.signal === 'Overweight') signalVariant = 'success';
            else if (o.signal === 'Sell' || o.signal === 'Underweight') signalVariant = 'danger';
            else if (o.signal === 'Hold') signalVariant = 'warning';
            const signalBadge = o.signal ? Components.badge(o.signal, signalVariant) : '\u2014';

            // Guardrail
            const guardrail = o.guardrail_result || '\u2014';
            const grEl = document.createElement('span');
            grEl.className = guardrail.startsWith('APPROVED') ? 'text-success' : guardrail.startsWith('BLOCKED') ? 'text-danger' : '';
            grEl.style.fontSize = '0.75rem';
            grEl.textContent = guardrail.length > 20 ? guardrail.substring(0, 20) + '\u2026' : guardrail;

            return [
                time,
                o.symbol || '\u2014',
                sideBadge,
                (o.qty || 0).toString(),
                o.order_type || '\u2014',
                o.filled_price ? Components.formatMoney(o.filled_price) : '\u2014',
                signalBadge,
                statusBadge,
                grEl,
            ];
        });

        container.appendChild(Components.table(headers, rows));
    },
};

window.OrdersPage = OrdersPage;
