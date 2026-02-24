/**
 * MetrQ Dashboard JavaScript Utilities
 * Includes authentication, token refresh, and API helpers
 */

const CONFIG = {
    POLL_INTERVAL: 2000,
    MAX_POLL_ATTEMPTS: 150,
    API_BASE_URL: '/api',
    RATE_LIMIT_WARNING_THRESHOLD: 10
};


/**
 * Modal window in User Dashboard
 */
function showErrorToast(message) {
    // Creates a div with Tailwind classes
    const toast = document.createElement('div');
    toast.className = 'fixed top-4 right-4 bg-red-500 text-white p-4 rounded-lg shadow-lg z-50';
    toast.textContent = message;
    document.body.appendChild(toast);

    // Auto delete after 3 seconds
    setTimeout(() => toast.remove(), 3000);
}

/**
 * Get authentication tokens from storage
 */
function getAuthToken() {
    return localStorage.getItem('metrq_token') || '';
}

function getRefreshToken() {
    return localStorage.getItem('metrq_refresh_token') || '';
}

/**
 * Store authentication tokens
 */
function setAuthTokens(access, refresh) {
    localStorage.setItem('metrq_token', access);
    if (refresh) {
        localStorage.setItem('metrq_refresh_token', refresh);
    }
    document.cookie = `token=${access}; path=/; max-age=86400`;
}

/**
 * Clear all authentication data
 */
function clearAuthData() {
    localStorage.removeItem('metrq_token');
    localStorage.removeItem('metrq_refresh_token');
    // Clear all cookies
    document.cookie.split(";").forEach(c => {
        document.cookie = c.replace(/^ +/, "").replace(/=.*/, "=;expires=" + new Date().toUTCString() + ";path=/");
    });
}

/**
 * Refresh the access token using the refresh token
 */
async function refreshAccessToken() {
    const refreshToken = getRefreshToken();
    if (!refreshToken) {
        throw new Error('No refresh token available');
    }

    const response = await fetch('/api/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh: refreshToken })
    });

    if (!response.ok) {
        // Refresh failed - token might be blacklisted or expired
        throw new Error('Failed to refresh token');
    }

    const data = await response.json();
    setAuthTokens(data.access, refreshToken); // Keep same refresh token
    return data.access;
}

/**
 * Fetch wrapper with automatic token refresh on 401
 */
async function fetchWithAuth(url, options = {}) {
    let token = getAuthToken();
    let retries = 0;
    const maxRetries = 1;

    while (retries <= maxRetries) {
        const headers = {
            ...options.headers,
            'Authorization': `Bearer ${token}`,
            'Accept': 'application/json',
        };

        // Don't override Content-Type if it's a FormData
        if (!(options.body instanceof FormData)) {
            headers['Content-Type'] = options.headers?.['Content-Type'] || 'application/json';
        }

        const response = await fetch(url, {
            ...options,
            headers
        });

        // If unauthorized and we haven't retried yet, try to refresh
        if (response.status === 401 && retries < maxRetries) {
            try {
                token = await refreshAccessToken();
                retries++;
                continue; // Retry with new token
            } catch (refreshError) {
                // Refresh failed, logout user
                console.error('Token refresh failed:', refreshError);
                await logoutUser();
                throw new Error('Session expired. Please log in again.');
            }
        }

        return response;
    }

    throw new Error('Failed to authenticate after retry');
}

/**
 * Logout user - clears tokens and redirects to home
 * This function is safe to call even if user is already logged out
 */
async function logoutUser() {
    try {
        const refreshToken = getRefreshToken();

        // Call logout endpoint to blacklist token (best effort)
        if (refreshToken) {
            await fetch('/api/auth/logout', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh: refreshToken })
            });
        }
    } catch (error) {
        // Ignore errors - we still want to clear client-side data
        console.log('Logout API call failed (ignored):', error);
    } finally {
        // Always clear client-side storage
        clearAuthData();

        // Redirect to home page
        window.location.href = '/';
    }
}

/**
 * Check if user is authenticated
 */
function isAuthenticated() {
    return !!getAuthToken();
}

/**
 * Redirect to login if not authenticated
 */
function requireAuth() {
    if (!isAuthenticated()) {
        window.location.href = '/login/';
        return false;
    }
    return true;
}

/**
 * Load dashboard data using fetchWithAuth
 */
async function loadDashboard() {
    try {
        const response = await fetchWithAuth(`${CONFIG.API_BASE_URL}/dashboard/`);
        if (!response.ok) throw new Error('Failed to load dashboard');

        // Update rate limit from Headers MUST be before data from response.json
        updateRateLimitFromHeaders(response.headers)

        const data = await response.json();

        // Update metrics
        document.getElementById('metric-articles').textContent = data.metrics.articles_24h;

        const sentimentEl = document.getElementById('metric-sentiment');
        sentimentEl.textContent = data.metrics.avg_sentiment.toFixed(2);
        sentimentEl.className = 'text-3xl font-bold ' +
            (data.metrics.avg_sentiment > 0.3 ? 'sentiment-positive' :
             data.metrics.avg_sentiment < -0.3 ? 'sentiment-negative' : 'sentiment-neutral');

        // Update top entities
        const entitiesContainer = document.getElementById('metric-entities');
        entitiesContainer.innerHTML = data.metrics.top_entities
            .slice(0, 5)
            .map(e => `<div class="truncate">${e}</div>`)
            .join('');


        // Update upgrade buttons based on tier
        document.getElementById('user-tier').textContent = data.tier;
        updateUpgradeButtons(data.tier);

        // Rendering articles by language
        renderLanguageBlocks(data.recent_articles);

        // Load report History
        loadReportHistory();

        // Save the tariff for further use (update interval)
        localStorage.setItem('user_tier', data.tier);

        // Setting an adaptive refresh interval
        setAdaptiveRefreshInterval(data.tier);

    } catch (error) {
        console.error('Dashboard load error:', error);
        if (error.message.includes('401') || error.message.includes('Session expired')) {
            // Already handled by fetchWithAuth
            return;
        }
        // Дополнительная обработка других ошибок
        showErrorToast('Failed to load dashboard data');
    }
}

// Legacy functions (keep for compatibility)
async function pollReportStatus(jobId, onProgress, onComplete, onError) {
    let attempts = 0;

    const poll = async () => {
        try {
            const response = await fetchWithAuth(`${CONFIG.API_BASE_URL}/reports/status/${jobId}`);

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to fetch status');
            }

            const data = await response.json();
            if (onProgress) onProgress(data.progress || 0, data.status);

            if (data.status === 'done') {
                if (onComplete) onComplete(data.download_url);
                return;
            }

            if (data.status === 'failed') {
                throw new Error(data.error || 'Report generation failed');
            }

            attempts++;
            if (attempts < CONFIG.MAX_POLL_ATTEMPTS) {
                setTimeout(poll, CONFIG.POLL_INTERVAL);
            } else {
                throw new Error('Report generation timeout');
            }
        } catch (error) {
            if (onError) onError(error.message);
        }
    };

    poll();
}

async function requestReport(type, format) {
    const response = await fetchWithAuth(`${CONFIG.API_BASE_URL}/reports/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, format })
    });

    if (!response.ok) {
        const error = await response.json();
        // Only show upgrade prompt for 403 (quota) or 429 (rate limit) on report requests
        if (response.status === 403 || response.status === 429) {
            showUpgradePrompt(error);
        }
        throw new Error(error.error || 'Failed to request report');
    }
    updateRateLimitFromHeaders(response.headers);
    return await response.json();
}

function updateRateLimitFromHeaders(headers) {
    const limit = headers.get('X-RateLimit-Limit');
    const remaining = headers.get('X-RateLimit-Remaining');

    if (limit && remaining) {
        const element = document.getElementById('rate-limit-display');
        if (element) element.textContent = remaining === '-1' ? 'Unlimited' : `${remaining}/${limit}`;
        const bar = document.getElementById('rate-limit-bar');
        if (bar) {
          const percentage = (parseInt(remaining) / parseInt(limit)) * 100;
          bar.style.width = `${percentage}%`;
        }
    }
}

function renderLanguageBlocks(articles) {
    const container = document.getElementById('language-blocks');
    const languages = {
        'zh_cn': { name: 'Chinese (Simplified)', color: 'red' },
        'zh_tw': { name: 'Chinese (Traditional)', color: 'blue' },
        'en': { name: 'English', color: 'green' },
        'ru': { name: 'Russian', color: 'yellow' }
    };

    container.innerHTML = Object.entries(languages).map(([code, info]) => {
        const langArticles = articles[code] || [];
        return `
            <div class="bg-white rounded-lg shadow overflow-hidden">
                <div class="bg-${info.color}-50 px-4 py-3 border-b">
                    <div class="flex justify-between items-center">
                        <h3 class="font-semibold text-gray-900">${info.name}</h3>
                        <span class="text-xs text-gray-500">${langArticles.length} articles</span>
                    </div>
                </div>
                <div class="p-4 space-y-3 max-h-96 overflow-y-auto">
                    ${langArticles.map(article => `
                        <article class="article-card border rounded p-3 text-sm">
                            <div class="flex justify-between items-start mb-1">
                                <span class="font-medium text-gray-900 truncate flex-1 mr-2">
                                    ${article.news_provider}
                                </span>
                                <span class="text-xs ${article.sentiment > 0.3 ? 'text-green-600' : article.sentiment < -0.3 ? 'text-red-600' : 'text-gray-500'}">
                                    ${article.sentiment.toFixed(2)}
                                </span>
                            </div>
                            <a href="${article.url}" target="_blank"
                               class="text-blue-600 hover:underline line-clamp-2">
                                ${article.title_translated}
                            </a>
                            <div class="mt-1 text-xs text-gray-500">
                                Bias: ${(article.article_bias_profile * 100).toFixed(0)}%
                            </div>
                        </article>
                    `).join('')}
                </div>
                <div class="px-4 py-2 bg-gray-50 border-t">
                    <a href="/dashboard/articles/${code}"
                       class="text-sm text-blue-600 hover:text-blue-800 font-medium">
                        View all →
                    </a>
                </div>
            </div>
        `;
    }).join('');
}


function setAdaptiveRefreshInterval(tier) {
    // If the interval is already set, do not create a new one
    if (window._refreshInterval) clearInterval(window._refreshInterval);

    const intervals = window.DASHBOARD_CONFIG?.REFRESH_INTERVALS ||
                     { free: 1800000, pro: 300000, enterprise: 60000 };
    const interval = intervals[tier] || intervals.free;
    window._refreshInterval = setInterval(loadDashboard, interval);
    console.log(`Dashboard refresh interval set to ${interval/1000}s for ${tier} tier`);
}

function renderSentimentGauge(value, elementId) {
    const container = document.getElementById(elementId);
    if (!container) return;

    const percentage = ((value + 1) / 2) * 100;
    let colorClass = 'bg-gray-500', label = 'Neutral';

    if (value > 0.3) { colorClass = 'bg-green-500'; label = 'Positive'; }
    else if (value < -0.3) { colorClass = 'bg-red-500'; label = 'Negative'; }

    container.innerHTML = `
        <div class="relative pt-1">
            <div class="flex mb-2 items-center justify-between">
                <span class="text-xs font-semibold py-1 px-2 rounded-full bg-gray-200">${label}</span>
                <span class="text-xs font-semibold">${value.toFixed(2)}</span>
            </div>
            <div class="overflow-hidden h-2 mb-4 flex rounded bg-gray-200">
                <div style="width: ${percentage}%" class="${colorClass} h-full transition-all"></div>
            </div>
        </div>`;
}

function showUpgradePrompt(errorData) {

    // Prevent multiple modals
    if (document.getElementById('upgrade-modal')) return;

    const modal = document.createElement('div');
    modal.id = 'upgrade-modal';
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';

    // Handle both rate limit (429) and report quota (403) error formats
    const isRateLimit = errorData.limit !== undefined;
    const title = isRateLimit ? 'Rate Limit Exceeded' : 'Report Limit Reached';
    const message = isRateLimit
        ? `You have used ${errorData.limit} requests today`
        : `${errorData.error}<br>Used: ${errorData.reports_used || 0} / ${errorData.max_reports || 'Unlimited'}`;

    // Determine the correct URL for the Upgrade btn depending on the tariff
    let upgradeUrl = '/pricing';
    if (!isRateLimit && errorData.tier) {
        // Report limit error (403) and the tariff is known
        if (errorData.tier === 'free') {
            upgradeUrl = '/pricing';
        } else if (errorData.tier === 'pro') {
            upgradeUrl = '/enterprise-info';
        } else {
            // Just in case the tariff is different (enterprise) - but enterprise has limit 999 and need to update limits
            upgradeUrl = errorData.upgrade_url || '/enterprise-info';
        }
    } else {
        // For rate limit (429) we use upgrade_url from the response or /enterprise-info
        upgradeUrl = errorData.upgrade_url || '/enterprise-info';
    }


    modal.innerHTML = `
        <div class="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 class="text-lg font-semibold mb-4">${title}</h3>
            <p class="text-gray-600 mb-4">${message}</p>
            <div class="flex justify-end space-x-3">
                <button onclick="document.getElementById('upgrade-modal').remove()" class="px-4 py-2 text-gray-600 hover:text-gray-800">Dismiss</button>
                <a href="${upgradeUrl}" class="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700">Upgrade</a>
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function updateUpgradeButtons(tier) {
    const container = document.getElementById('upgrade-button-container');
    if (!container) return;

    container.innerHTML = '';

    if (tier === 'free') {
        const proButton = document.createElement('a');
        proButton.href = '/pricing';
        proButton.className = 'px-3 py-1 bg-green-600 hover:bg-green-700 text-white text-xs font-medium rounded-full transition';
        proButton.textContent = 'Upgrade to Pro';
        container.appendChild(proButton);
    } else if (tier === 'pro') {
        const enterpriseButton = document.createElement('a');
        enterpriseButton.href = '/enterprise-info';
        enterpriseButton.className = 'px-3 py-1 bg-purple-600 hover:bg-purple-700 text-white text-xs font-medium rounded-full transition';
        enterpriseButton.textContent = 'Upgrade to Enterprise';
        container.appendChild(enterpriseButton);
    } else if (tier === 'enterprise') {
        const enterpriseBadge = document.createElement('span');
        enterpriseBadge.className = 'px-3 py-1 bg-gray-600 text-gray-300 text-xs font-medium rounded-full cursor-default';
        enterpriseBadge.textContent = 'Enterprise';
        container.appendChild(enterpriseBadge);
    }
}

async function loadReportHistory() {
    const container = document.getElementById('report-history');
    if (!container) return;

    // Show loading indicator
    container.innerHTML = '<p class="text-gray-500">Loading reports...</p>';

    try {
      const response = await fetchWithAuth(`${CONFIG.API_BASE_URL}/reports/history`);
      const reports = await response.json();

      if (reports.length === 0) {
        container.innerHTML = '<p class="text-gray-500">No reports generated yet.</p>';
        return;
      }

      // Preserve scroll position if needed (optional enhancement)
      const scrollTop = container.scrollTop;

      container.innerHTML = reports.map(r => `
        <div class="flex justify-between items-center bg-white p-4 rounded shadow-sm">
          <div>
            <p class="font-medium text-gray-900">${r.type} Report (${r.format.toUpperCase()})</p>
            <p class="text-sm text-gray-500">${new Date(r.created_at).toLocaleString()}</p>
          </div>
          <div>
            ${r.status === 'done' && r.download_url ?
              `<a href="${r.download_url}" class="text-blue-600 hover:text-blue-800 font-medium text-sm">Download</a>` :
              `<span class="text-sm text-gray-500 capitalize">${r.status}</span>`
            }
          </div>
        </div>
      `).join('');

      // Restore scroll (if was scrolled)
      container.scrollTop = scrollTop;
    } catch (e) {
      console.error('History load failed', e);
      container.innerHTML = '<p class="text-red-500 text-sm">Failed to load reports. Please refresh.</p>';
    }
}


/**
 * Load and display announcement banner
 */
async function loadAnnouncement() {
    const bannerContainer = document.getElementById('announcement-banner');
    if (!bannerContainer) return;

    // Check if we already have a banner rendered (avoid duplicates)
    if (bannerContainer.dataset.loaded === 'true') return;

    try {
        const response = await fetchWithAuth(`${CONFIG.API_BASE_URL}/dashboard/announcement`);

        // Handle 204 No Content (no active announcement)
        if (response.status === 204) {
            bannerContainer.classList.add('hidden');
            bannerContainer.innerHTML = '';
            return;
        }

        if (!response.ok) throw new Error('Failed to load announcement');

        const announcement = await response.json();

        // Check if user dismissed this announcement in this session
        const dismissedKey = `dismissed_announcement_${announcement.id}`;
        if (localStorage.getItem(dismissedKey) === 'true') {
            bannerContainer.classList.add('hidden');
            return;
        }

        // Render banner
        renderBanner(announcement, bannerContainer, dismissedKey);

    } catch (error) {
        console.error('Announcement load error:', error);
        // Fail silently - banner is not critical
        bannerContainer.classList.add('hidden');
    }
}

/**
 * Render announcement banner HTML
 */
function renderBanner(announcement, container, dismissedKey) {
    const hasLink = announcement.link_url && announcement.link_url.trim() !== '';

    // Create banner HTML structure
    const bannerHTML = `
        <div class="bg-gradient-to-r from-blue-50 to-indigo-50 border-b border-blue-100 animate-fade-in">
            <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
                <div class="flex items-center justify-between py-3">
                    <div class="flex-1 flex items-center justify-center">
                        ${hasLink ? `
                            <a href="${escapeHtml(announcement.link_url)}"
                               target="_blank"
                               rel="noopener noreferrer"
                               class="flex items-center text-sm text-blue-800 hover:text-blue-900 font-medium cursor-pointer group">
                                <svg class="w-4 h-4 mr-2 flex-shrink-0 text-blue-500 group-hover:text-blue-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                                </svg>
                                <span class="text-center">${announcement.message}</span>
                                <svg class="w-3 h-3 ml-1 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path>
                                </svg>
                            </a>
                        ` : `
                            <div class="flex items-center text-sm text-blue-800 font-medium">
                                <svg class="w-4 h-4 mr-2 flex-shrink-0 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                                </svg>
                                <span class="text-center">${announcement.message}</span>
                            </div>
                        `}
                    </div>
                    <button onclick="dismissAnnouncement('${dismissedKey}', this)"
                            class="ml-4 p-1 rounded-full text-blue-400 hover:text-blue-600 hover:bg-blue-100 focus:outline-none focus:ring-2 focus:ring-blue-500 transition-colors"
                            aria-label="Dismiss announcement">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                        </svg>
                    </button>
                </div>
            </div>
        </div>
    `;

    container.innerHTML = bannerHTML;
    container.classList.remove('hidden');
    container.dataset.loaded = 'true';
}

/**
 * Dismiss announcement and store in localStorage
 */
function dismissAnnouncement(storageKey, buttonElement) {
    // Store dismissal
    localStorage.setItem(storageKey, 'true');

    // Animate out
    const banner = buttonElement.closest('.animate-fade-in');
    if (banner) {
        banner.style.transition = 'all 0.3s ease-out';
        banner.style.opacity = '0';
        banner.style.transform = 'translateY(-100%)';

        setTimeout(() => {
            const container = document.getElementById('announcement-banner');
            if (container) {
                container.classList.add('hidden');
                container.innerHTML = '';
            }
        }, 300);
    } else {
        const container = document.getElementById('announcement-banner');
        if (container) {
            container.classList.add('hidden');
            container.innerHTML = '';
        }
    }
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


// Initialize dashboard
function initDashboard() {
    // Check authentication
    if (!isAuthenticated() && !window.location.pathname.includes('/login')) {
        window.location.href = '/login';
        return;
    }

    // Load announcement banner (before other content)
    loadAnnouncement();

    // Attach logout handler if element exists
    const logoutLink = document.getElementById('logout-link');
    if (logoutLink) {
        logoutLink.addEventListener('click', (e) => {
            e.preventDefault();
            logoutUser();
        });
    }


    // Initialize button-based report generation
    const generateBtn = document.getElementById('generate-report-btn');
    if (generateBtn) {
        generateBtn.addEventListener('click', async (e) => {
            e.preventDefault();

            // Prevent double-click while processing
            if (generateBtn.disabled) return;

            const typeSelect = document.getElementById('report-type');
            const formatSelect = document.getElementById('report-format');
            const statusDiv = document.getElementById('report-status');

            if (!typeSelect || !formatSelect) {
                console.error('Report type or format select not found');
                return;
            }

            const type = typeSelect.value;
            const format = formatSelect.value;

            // Show status area
            if (statusDiv) {
                statusDiv.classList.remove('hidden');
            }
            generateBtn.disabled = true;

            try {
                // Request report
                const job = await requestReport(type, format);

                // Reset status area for polling
                if (statusDiv) {
                    statusDiv.innerHTML = `
                        <div class="flex items-center space-x-3 mb-2">
                            <div class="w-full bg-gray-200 rounded-full h-2.5">
                                <div id="progress-bar" class="bg-blue-600 h-2.5 rounded-full" style="width: 0%"></div>
                            </div>
                            <span id="progress-text" class="text-sm font-medium w-16">0%</span>
                        </div>
                        <p id="status-message" class="text-sm text-gray-600">Queued...</p>
                    `;
                }

                // Refresh history to show queued report
                await loadReportHistory();

                // Poll status with history refresh on completion/error
                pollReportStatus(
                    job.job_id,
                    (progress, status) => {
                        const bar = document.getElementById('progress-bar');
                        const text = document.getElementById('progress-text');
                        if (bar) bar.style.width = `${progress}%`;
                        if (text) text.textContent = `${progress}%`;

                        const statusMsg = document.getElementById('status-message');
                        if (statusMsg) {
                            const messages = {
                                'queued': 'Waiting in queue...',
                                'processing': 'Generating your report...',
                                'done': 'Ready for download!',
                                'failed': 'Generation failed. Please try again.'
                            };
                            statusMsg.textContent = messages[status] || status;
                        }
                    },
                    async (url) => { // onComplete
                        const statusMsg = document.getElementById('status-message');
                        if (statusMsg) {
                            statusMsg.innerHTML = `<a href="${url}" class="text-blue-600 font-medium hover:underline">Download Report</a>`;
                            statusMsg.className = 'text-sm text-green-600';
                        }
                        generateBtn.disabled = false;
                        // Refresh history on completion
                        await loadReportHistory();
                    },
                    async (error) => { // onError
                        const statusMsg = document.getElementById('status-message');
                        if (statusMsg) {
                            statusMsg.textContent = `Error: ${error}`;
                            statusMsg.className = 'text-sm text-red-600';
                        }
                        generateBtn.disabled = false;
                        // Refresh history on error too
                        await loadReportHistory();
                    }
                );
            } catch (error) {
                // Error already handled by showUpgradePrompt for 403/429
                // Just update status message
                const statusMsg = document.getElementById('status-message');
                if (statusMsg) {
                    statusMsg.textContent = error.message;
                    statusMsg.className = 'text-sm text-red-600';
                }
                generateBtn.disabled = false;
                // Don't refresh history here - it was already handled by onError or not needed
            }
        });
    }
    loadDashboard();
}


//// Auto-initialize on DOM ready
//if (document.readyState === 'loading') {
//    document.addEventListener('DOMContentLoaded', initDashboard);
//} else {
//    initDashboard();
//}

// Export utilities for use in other scripts
window.MetrQDashboard = {
    initDashboard,
    loadDashboard,
    loadReportHistory,
    requestReport,
    pollReportStatus,
    renderSentimentGauge,
    getAuthToken,
    getRefreshToken,
    fetchWithAuth,
    logoutUser,
    isAuthenticated,
    requireAuth,
    updateUpgradeButtons
};
