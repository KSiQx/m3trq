/**
 * MetrQ Dashboard JavaScript Utilities
 */

const CONFIG = {
    POLL_INTERVAL: 2000,
    MAX_POLL_ATTEMPTS: 150,
    API_BASE_URL: '/api',
    RATE_LIMIT_WARNING_THRESHOLD: 10
};

async function pollReportStatus(jobId, onProgress, onComplete, onError) {
    let attempts = 0;
    const token = getAuthToken();

    const poll = async () => {
        try {
            const response = await fetch(`${CONFIG.API_BASE_URL}/reports/status/${jobId}`, {
                headers: { 'Authorization': `Bearer ${token}`, 'Accept': 'application/json' }
            });

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
    const token = getAuthToken();
    const response = await fetch(`${CONFIG.API_BASE_URL}/reports/request`, {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({ type, format })
    });

    if (!response.ok) {
        const error = await response.json();
        if (response.status === 429) showUpgradePrompt(error);
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
        if (element) element.textContent = `${remaining}/${limit}`;

        const bar = document.getElementById('rate-limit-bar');
        if (bar) {
            const percentage = (parseInt(remaining) / parseInt(limit)) * 100;
            bar.style.width = `${percentage}%`;
        }
    }
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
    const modal = document.createElement('div');
    modal.className = 'fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50';
    modal.innerHTML = `
        <div class="bg-white rounded-lg p-6 max-w-md w-full mx-4">
            <h3 class="text-lg font-semibold mb-4">Rate Limit Exceeded</h3>
            <p class="text-gray-600 mb-4">You've used ${errorData.limit} requests today.</p>
            <div class="flex justify-end space-x-3">
                <button onclick="this.closest('.fixed').remove()" class="px-4 py-2 text-gray-600">Dismiss</button>
                <a href="${errorData.upgrade_url || '/upgrade'}" class="px-4 py-2 bg-blue-600 text-white rounded">Upgrade</a>
            </div>
        </div>`;
    document.body.appendChild(modal);
}

function getAuthToken() {
    return localStorage.getItem('metrq_token') ||
           document.cookie.split('; ').find(row => row.startsWith('token='))?.split('=')[1] || '';
}

function initDashboard() {
    const token = getAuthToken();
    if (!token && !window.location.pathname.includes('/login')) {
        window.location.href = '/login';
        return;
    }

    const reportForm = document.getElementById('report-form');
    if (reportForm) {
        reportForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const type = document.getElementById('report-type').value;
            const format = document.getElementById('report-format').value;
            const submitBtn = reportForm.querySelector('button[type="submit"]');
            const statusDiv = document.getElementById('report-status');

            submitBtn.disabled = true;
            try {
                const job = await requestReport(type, format);
                statusDiv.innerHTML = `<div class="p-4 bg-blue-50">Generating ${format.toUpperCase()}... Job: ${job.job_id}</div>`;

                pollReportStatus(
                    job.job_id,
                    (progress, status) => {
                        const bar = document.getElementById('progress-bar');
                        if (bar) bar.style.width = `${progress}%`;
                    },
                    (url) => {
                        statusDiv.innerHTML = `<div class="p-4 bg-green-50"><a href="${url}" class="text-blue-600">Download Ready</a></div>`;
                        submitBtn.disabled = false;
                    },
                    (error) => {
                        statusDiv.innerHTML = `<div class="p-4 bg-red-50">Error: ${error}</div>`;
                        submitBtn.disabled = false;
                    }
                );
            } catch (error) {
                statusDiv.innerHTML = `<div class="p-4 bg-red-50">${error.message}</div>`;
                submitBtn.disabled = false;
            }
        });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDashboard);
} else {
    initDashboard();
}

window.MetrQDashboard = { pollReportStatus, requestReport, renderSentimentGauge, getAuthToken };