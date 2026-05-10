/**
 * TradingAgents Dashboard — Chart Utilities
 *
 * Wraps TradingView Lightweight Charts for the equity curve
 * and any other chart needs. Handles dark theme, responsive
 * sizing, and data formatting.
 */

const Charts = {
    /** Store active chart instances for cleanup. */
    _instances: {},

    /**
     * Colour tokens matching the CSS design system.
     */
    theme: {
        background: '#0a0f1e',
        textColor: '#94a3b8',
        gridColor: 'rgba(59, 130, 246, 0.06)',
        crosshairColor: 'rgba(59, 130, 246, 0.3)',
        primaryLine: '#3b82f6',
        primaryArea: 'rgba(59, 130, 246, 0.08)',
        successLine: '#10b981',
        dangerLine: '#ef4444',
    },

    /**
     * Create an area chart (equity curve).
     *
     * @param {HTMLElement} container - DOM element to render into
     * @param {Array<{time: string, value: number}>} data
     * @param {object} opts
     * @param {string} opts.id - Chart instance ID for cleanup
     * @param {string} opts.lineColor
     * @param {string} opts.areaColor
     * @returns {object} chart instance
     */
    createAreaChart(container, data = [], opts = {}) {
        const id = opts.id || 'default';

        // Cleanup previous instance
        if (this._instances[id]) {
            this._instances[id].remove();
            delete this._instances[id];
        }

        // Guard: check if library loaded
        if (typeof LightweightCharts === 'undefined') {
            container.innerHTML = '<div class="empty-state"><div class="empty-state__text">Chart library not loaded</div></div>';
            return null;
        }

        const chart = LightweightCharts.createChart(container, {
            layout: {
                background: { color: 'transparent' },
                textColor: this.theme.textColor,
                fontSize: 11,
                fontFamily: "'Inter', sans-serif",
            },
            grid: {
                vertLines: { color: this.theme.gridColor },
                horzLines: { color: this.theme.gridColor },
            },
            crosshair: {
                vertLine: { color: this.theme.crosshairColor, width: 1, style: 2 },
                horzLine: { color: this.theme.crosshairColor, width: 1, style: 2 },
            },
            rightPriceScale: {
                borderColor: this.theme.gridColor,
                scaleMargins: { top: 0.1, bottom: 0.1 },
            },
            timeScale: {
                borderColor: this.theme.gridColor,
                timeVisible: false,
                rightOffset: 2,
            },
            handleScale: true,
            handleScroll: true,
        });

        const series = chart.addAreaSeries({
            lineColor: opts.lineColor || this.theme.primaryLine,
            topColor: opts.areaColor || this.theme.primaryArea,
            bottomColor: 'transparent',
            lineWidth: 2,
            priceFormat: {
                type: 'custom',
                formatter: (price) => '$' + price.toLocaleString('en-US', { maximumFractionDigits: 0 }),
            },
        });

        if (data.length > 0) {
            series.setData(data);
            chart.timeScale().fitContent();
        }

        // Responsive resize
        const resizeObserver = new ResizeObserver(entries => {
            for (const entry of entries) {
                const { width, height } = entry.contentRect;
                chart.resize(width, height);
            }
        });
        resizeObserver.observe(container);

        this._instances[id] = chart;
        chart._series = series;
        chart._resizeObserver = resizeObserver;

        return chart;
    },

    /**
     * Update chart data.
     * @param {string} id - Chart instance ID
     * @param {Array<{time: string, value: number}>} data
     */
    updateData(id, data) {
        const chart = this._instances[id];
        if (chart && chart._series) {
            chart._series.setData(data);
            chart.timeScale().fitContent();
        }
    },

    /**
     * Cleanup a chart instance.
     * @param {string} id
     */
    destroy(id) {
        const chart = this._instances[id];
        if (chart) {
            if (chart._resizeObserver) {
                chart._resizeObserver.disconnect();
            }
            chart.remove();
            delete this._instances[id];
        }
    },

    /**
     * Destroy all chart instances.
     */
    destroyAll() {
        Object.keys(this._instances).forEach(id => this.destroy(id));
    },
};

// Make globally available
window.Charts = Charts;
