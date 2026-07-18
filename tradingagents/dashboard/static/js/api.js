/**
 * TradingAgents Dashboard — API Client
 *
 * Centralised fetch wrapper for all API calls. Handles errors,
 * JSON parsing, and provides typed methods for each endpoint.
 */

const API = {
    /**
     * Base fetch with error handling.
     * @param {string} url
     * @param {object} options
     * @returns {Promise<any>}
     */
    async _fetch(url, options = {}) {
        try {
            const res = await fetch(url, {
                headers: { 'Content-Type': 'application/json', ...options.headers },
                ...options,
            });
            if (!res.ok) {
                const errorBody = await res.text();
                console.error(`API error ${res.status} on ${url}:`, errorBody);
                return null;
            }
            return await res.json();
        } catch (err) {
            console.error(`API fetch failed for ${url}:`, err);
            return null;
        }
    },

    // ── Portfolio & Account ───────────────────────────────────

    /** Get portfolio summary (value, cash, P&L, exposure). */
    async getPortfolio() {
        return this._fetch('/api/portfolio');
    },

    /** Get full Alpaca account details. */
    async getAccount() {
        return this._fetch('/api/account');
    },

    // ── Positions ─────────────────────────────────────────────

    /** Get all open positions (merged DB + Alpaca + bracket legs). */
    async getPositions() {
        return this._fetch('/api/positions');
    },

    /** Get single position detail with bracket legs & related orders. */
    async getPosition(symbol) {
        return this._fetch(`/api/positions/${encodeURIComponent(symbol)}`);
    },

    // ── Market Clock ──────────────────────────────────────────

    /** Get market clock (is_open, next_open, next_close). */
    async getClock() {
        return this._fetch('/api/clock');
    },

    // ── Market Regime ─────────────────────────────────────────

    /** Get the market regime (SPY MA stacking + VIX, cached ~10 min). */
    async getRegime() {
        return this._fetch('/api/regime');
    },

    // ── Orders ────────────────────────────────────────────────

    /** Get orders directly from Alpaca API with bracket leg expansion. */
    async getAlpacaOrders(params = {}) {
        const query = new URLSearchParams(params).toString();
        return this._fetch(`/api/alpaca/orders${query ? '?' + query : ''}`);
    },

    // ── Snapshots ─────────────────────────────────────────────

    /** Get daily portfolio snapshots. */
    async getSnapshots(days = 30) {
        return this._fetch(`/api/snapshots?days=${days}`);
    },

    /** Get equity curve data for charting. */
    async getEquityCurve(days = 90) {
        return this._fetch(`/api/equity-curve?days=${days}`);
    },

    // ── Screening ─────────────────────────────────────────────

    /** Get screening results for a date. */
    async getScreening(date) {
        return this._fetch(`/api/screening/${encodeURIComponent(date)}`);
    },

    /** Get today's screening results. */
    async getScreeningLatest() {
        return this._fetch('/api/screening/latest');
    },

    // ── Daemon ────────────────────────────────────────────────

    /** Get daemon status. */
    async getDaemonStatus() {
        return this._fetch('/api/daemon/status');
    },

    /** Get current config (read-only). */
    async getConfig() {
        return this._fetch('/api/config');
    },

    /** Get A/B comparison data (local vs peer). */
    async getComparison() {
        return this._fetch('/api/comparison');
    },
};

// Make globally available
window.API = API;
