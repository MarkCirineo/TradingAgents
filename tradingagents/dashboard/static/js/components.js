/**
 * TradingAgents Dashboard — Reusable DOM Components
 *
 * Factory functions that create DOM elements for cards, tables,
 * badges, stat displays, and bracket order visualisations.
 * All functions return HTMLElements — no innerHTML for safety.
 */

const Components = {
    /**
     * Create a glass card container.
     * @param {object} opts
     * @param {string} opts.title - Card header title
     * @param {string} opts.icon - Emoji icon (optional)
     * @param {boolean} opts.interactive - Add hover effect
     * @param {string} opts.id - Element ID
     * @returns {HTMLElement}
     */
    card({ title = '', icon = '', interactive = false, id = '' } = {}) {
        const card = document.createElement('div');
        card.className = `card fade-in${interactive ? ' card--interactive' : ''}`;
        if (id) card.id = id;

        if (title || icon) {
            const header = document.createElement('div');
            header.className = 'card__header';

            const titleEl = document.createElement('span');
            titleEl.className = 'card__title';
            titleEl.textContent = title;
            header.appendChild(titleEl);

            if (icon) {
                const iconEl = document.createElement('span');
                iconEl.className = 'card__icon';
                iconEl.textContent = icon;
                header.appendChild(iconEl);
            }

            card.appendChild(header);
        }

        return card;
    },

    /**
     * Create a stat value display.
     * @param {object} opts
     * @param {string|number} opts.value
     * @param {string} opts.label
     * @param {number} opts.change - Change value for +/- coloring
     * @param {string} opts.changeText - Override change display text
     * @param {boolean} opts.large - Use larger font
     * @returns {HTMLElement}
     */
    stat({ value = '', label = '', change = null, changeText = '', large = false } = {}) {
        const stat = document.createElement('div');
        stat.className = 'stat';

        const valueEl = document.createElement('div');
        valueEl.className = `stat__value${large ? ' stat__value--large' : ''}`;
        valueEl.textContent = value;
        stat.appendChild(valueEl);

        if (change !== null || changeText) {
            const changeEl = document.createElement('span');
            const isPositive = change > 0;
            const isNegative = change < 0;
            changeEl.className = `stat__change ${isPositive ? 'stat__change--positive' : isNegative ? 'stat__change--negative' : 'stat__change--neutral'}`;
            const arrow = isPositive ? '↑' : isNegative ? '↓' : '→';
            changeEl.textContent = changeText || `${arrow} ${Math.abs(change).toFixed(2)}%`;
            stat.appendChild(changeEl);
        }

        if (label) {
            const labelEl = document.createElement('div');
            labelEl.className = 'stat__label';
            labelEl.textContent = label;
            stat.appendChild(labelEl);
        }

        return stat;
    },

    /**
     * Create a badge element.
     * @param {string} text
     * @param {string} variant - success, danger, warning, info, primary, neutral
     * @returns {HTMLElement}
     */
    badge(text, variant = 'neutral') {
        const badge = document.createElement('span');
        badge.className = `badge badge--${variant}`;
        badge.textContent = text;
        return badge;
    },

    /**
     * Create an exposure bar.
     * @param {number} percent - 0-100
     * @returns {HTMLElement}
     */
    exposureBar(percent) {
        const bar = document.createElement('div');
        bar.className = 'exposure-bar';

        const fill = document.createElement('div');
        fill.className = `exposure-bar__fill${percent > 50 ? ' exposure-bar__fill--warning' : ''}`;
        fill.style.width = `${Math.min(percent, 100)}%`;
        bar.appendChild(fill);

        return bar;
    },

    /**
     * Create a data table from headers and rows.
     * @param {string[]} headers
     * @param {Array<Array<string|HTMLElement>>} rows
     * @param {object} opts
     * @param {Function} opts.onRowClick - Click handler (row index)
     * @returns {HTMLElement}
     */
    table(headers, rows, { onRowClick = null } = {}) {
        const table = document.createElement('table');
        table.className = 'data-table';

        // Header
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        headers.forEach(h => {
            const th = document.createElement('th');
            th.textContent = h;
            headerRow.appendChild(th);
        });
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Body
        const tbody = document.createElement('tbody');
        rows.forEach((row, idx) => {
            const tr = document.createElement('tr');
            if (onRowClick) {
                tr.className = 'clickable';
                tr.addEventListener('click', () => onRowClick(idx, row));
            }
            row.forEach(cell => {
                const td = document.createElement('td');
                if (cell instanceof HTMLElement) {
                    td.appendChild(cell);
                } else {
                    td.textContent = cell;
                }
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);

        return table;
    },

    /**
     * Create a screening funnel visualisation.
     * @param {object} stages - { screened, filtered, analyzed, entries }
     * @returns {HTMLElement}
     */
    funnel(stages) {
        const funnel = document.createElement('div');
        funnel.className = 'funnel';

        const stageData = [
            { count: stages.screened || 0, label: 'Screened' },
            { count: stages.filtered || 0, label: 'Filtered' },
            { count: stages.analyzed || 0, label: 'Analyzed' },
            { count: stages.entries || 0, label: 'Entries' },
        ];

        stageData.forEach((s, i) => {
            const stage = document.createElement('div');
            stage.className = 'funnel__stage';

            const count = document.createElement('div');
            count.className = 'funnel__count';
            count.textContent = s.count;
            stage.appendChild(count);

            const label = document.createElement('div');
            label.className = 'funnel__label';
            label.textContent = s.label;
            stage.appendChild(label);

            funnel.appendChild(stage);

            if (i < stageData.length - 1) {
                const arrow = document.createElement('span');
                arrow.className = 'funnel__arrow';
                arrow.textContent = '→';
                funnel.appendChild(arrow);
            }
        });

        return funnel;
    },

    /**
     * Create an empty state placeholder.
     * @param {string} icon - Emoji
     * @param {string} text
     * @returns {HTMLElement}
     */
    emptyState(icon, text) {
        const el = document.createElement('div');
        el.className = 'empty-state';

        const iconEl = document.createElement('div');
        iconEl.className = 'empty-state__icon';
        iconEl.textContent = icon;
        el.appendChild(iconEl);

        const textEl = document.createElement('div');
        textEl.className = 'empty-state__text';
        textEl.textContent = text;
        el.appendChild(textEl);

        return el;
    },

    /**
     * Format a dollar amount with $ sign and commas.
     * @param {number} value
     * @param {number} decimals
     * @returns {string}
     */
    formatMoney(value, decimals = 2) {
        if (value == null || isNaN(value)) return '$—';
        const sign = value < 0 ? '-' : '';
        return sign + '$' + Math.abs(value).toLocaleString('en-US', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
    },

    /**
     * Format a percentage with sign.
     * @param {number} value
     * @returns {string}
     */
    formatPercent(value) {
        if (value == null || isNaN(value)) return '—%';
        const sign = value > 0 ? '+' : '';
        return sign + value.toFixed(2) + '%';
    },

    /**
     * Create a P&L text element with appropriate coloring.
     * @param {number} value - Dollar or percent value
     * @param {string} type - 'money' or 'percent'
     * @returns {HTMLElement}
     */
    pnl(value, type = 'money') {
        const el = document.createElement('span');
        const formatted = type === 'money' ? this.formatMoney(value) : this.formatPercent(value);
        el.textContent = formatted;
        el.className = value > 0 ? 'text-success font-semibold' : value < 0 ? 'text-danger font-semibold' : 'text-secondary';
        return el;
    },
};

// Make globally available
window.Components = Components;
