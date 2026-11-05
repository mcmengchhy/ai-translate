/**
 * Bilibili High-Speed Download & Authentication Manager
 * Handles QR Code scan login, polling, manual cookie import, and UI state synchronization.
 */

let bilibiliPollTimer = null;
let currentBiliQrKey = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    checkBilibiliAuthStatus();
});

/**
 * Fetches current Bilibili login status from backend
 */
async function checkBilibiliAuthStatus(forceRefresh = false) {
    try {
        const resp = await fetch(`/api/bilibili/status?refresh=${forceRefresh ? 'true' : 'false'}`);
        if (!resp.ok) return;
        const data = await resp.json();
        updateBilibiliUIState(data);
    } catch (e) {
        console.warn('Failed to check Bilibili status:', e);
    }
}

/**
 * Updates UI buttons and badges across the application
 */
function updateBilibiliUIState(statusData) {
    const isLoggedIn = Boolean(statusData && statusData.logged_in && statusData.user);
    const user = statusData ? statusData.user : null;

    // 1. Top Navbar Button
    const navDot = document.getElementById('nav-bilibili-dot');
    const navText = document.getElementById('nav-bilibili-text');
    if (navDot && navText) {
        if (isLoggedIn) {
            navDot.classList.add('active');
            navText.textContent = user.uname ? `Bilibili (${user.uname})` : 'Bilibili (Active)';
        } else {
            navDot.classList.remove('active');
            navText.textContent = 'Bilibili Login (Fast)';
        }
    }

    // 2. Video Dubber Sample Row Button
    const badgeBtn = document.getElementById('bilibili-login-badge-btn');
    const badgeLabel = document.getElementById('bilibili-badge-label');
    if (badgeLabel) {
        if (isLoggedIn) {
            badgeLabel.textContent = user.uname ? `⚡ ${user.uname} (1080P Active)` : '⚡ Bilibili (1080P Active)';
            if (badgeBtn) {
                badgeBtn.style.borderColor = 'rgba(16, 185, 129, 0.4)';
                badgeBtn.style.color = '#34d399';
            }
        } else {
            badgeLabel.textContent = '⚡ Bilibili Login (Fast Speed)';
            if (badgeBtn) {
                badgeBtn.style.borderColor = 'rgba(0, 161, 214, 0.4)';
                badgeBtn.style.color = '#38bdf8';
            }
        }
    }

    // 3. Platform bar inline badge
    const inlineStatus = document.getElementById('bilibili-inline-status');
    if (inlineStatus) {
        if (isLoggedIn) {
            inlineStatus.textContent = '⚡ 1080P Active';
            inlineStatus.style.color = '#34d399';
        } else {
            inlineStatus.textContent = '⚡ Login for Speed';
            inlineStatus.style.color = '#fbbf24';
        }
    }

    // 4. Update modal contents if currently open
    renderModalState(isLoggedIn, user);
}

/**
 * Opens the Bilibili Authentication Modal
 */
async function openBilibiliModal() {
    const modal = document.getElementById('bilibili-modal');
    if (!modal) return;
    modal.style.display = 'flex';
    modal.classList.add('active');

    // Check status
    try {
        const resp = await fetch('/api/bilibili/status');
        const data = await resp.json();
        updateBilibiliUIState(data);

        // If not logged in, auto generate QR code
        if (!data.logged_in) {
            generateNewBilibiliQR();
        }
    } catch (e) {
        generateNewBilibiliQR();
    }
}

/**
 * Closes the Bilibili Modal
 */
function closeBilibiliModal() {
    const modal = document.getElementById('bilibili-modal');
    if (modal) {
        modal.classList.remove('active');
        modal.style.display = 'none';
    }
    stopBilibiliPolling();
}

/**
 * Renders the modal view based on login state
 */
function renderModalState(isLoggedIn, user) {
    const loggedInView = document.getElementById('bilibili-logged-in-view');
    const loginView = document.getElementById('bilibili-login-view');

    if (isLoggedIn && user) {
        if (loggedInView) loggedInView.style.display = 'block';
        if (loginView) loginView.style.display = 'none';
        stopBilibiliPolling();

        // Populate user details
        const avatarEl = document.getElementById('bilibili-user-avatar');
        if (avatarEl) avatarEl.src = user.face || 'https://static.hdslb.com/images/akari.jpg';

        const nameEl = document.getElementById('bilibili-user-name');
        if (nameEl) nameEl.textContent = user.uname || 'Bilibili Member';

        const midEl = document.getElementById('bilibili-user-mid');
        if (midEl) midEl.textContent = `UID: ${user.mid || '--'}`;

        const levelEl = document.getElementById('bilibili-user-level');
        if (levelEl) levelEl.textContent = `Lv ${user.level ?? 0}`;

        const vipBadge = document.getElementById('bilibili-user-vip-badge');
        if (vipBadge) {
            vipBadge.textContent = user.vip_label || (user.is_vip ? 'VIP' : 'Member');
            vipBadge.style.display = user.is_vip ? 'inline-block' : 'none';
        }
    } else {
        if (loggedInView) loggedInView.style.display = 'none';
        if (loginView) loginView.style.display = 'block';
    }
}

/**
 * Switches login tab (QR, Manual, Browser)
 */
function switchBiliTab(tabName) {
    const tabs = ['qr', 'manual', 'browser'];
    tabs.forEach(t => {
        const btn = document.getElementById(`bili-tab-${t}-btn`);
        const pane = document.getElementById(`bili-pane-${t}`);
        if (btn) btn.classList.toggle('active', t === tabName);
        if (pane) {
            pane.classList.toggle('active', t === tabName);
            pane.style.display = (t === tabName) ? 'block' : 'none';
        }
    });

    if (tabName === 'qr') {
        if (!currentBiliQrKey) {
            generateNewBilibiliQR();
        }
    } else {
        stopBilibiliPolling();
    }
}

/**
 * Generates a new official Bilibili QR code and starts polling
 */
async function generateNewBilibiliQR() {
    stopBilibiliPolling();

    const qrContainer = document.getElementById('bilibili-qr-container');
    const statusText = document.getElementById('bilibili-qr-status-text');
    const statusDot = document.querySelector('.qr-status-dot');

    if (qrContainer) {
        qrContainer.innerHTML = `
            <div class="qr-loading-spinner">
                <i class="ph ph-spinner ph-spin"></i>
                <span>Generating QR...</span>
            </div>
        `;
    }
    if (statusText) statusText.textContent = 'Contacting Bilibili passport...';
    if (statusDot) statusDot.className = 'qr-status-dot';

    try {
        const resp = await fetch('/api/bilibili/qrcode/generate');
        if (!resp.ok) throw new Error(`Server returned ${resp.status}`);

        const data = await resp.json();
        currentBiliQrKey = data.qrcode_key;
        const qrUrl = data.url;

        // Render QR Code using QRCode library or fallback
        if (qrContainer) {
            qrContainer.innerHTML = '';
            if (typeof QRCode !== 'undefined') {
                new QRCode(qrContainer, {
                    text: qrUrl,
                    width: 170,
                    height: 170,
                    colorDark: '#000000',
                    colorLight: '#ffffff',
                    correctLevel: QRCode.CorrectLevel.M
                });
            } else {
                // Image fallback
                const img = document.createElement('img');
                img.src = `https://api.qrserver.com/v1/create-qr-code/?size=170x170&margin=0&data=${encodeURIComponent(qrUrl)}`;
                img.alt = 'Scan with Bilibili App';
                img.style.width = '170px';
                img.style.height = '170px';
                qrContainer.appendChild(img);
            }
        }

        if (statusText) statusText.textContent = 'Waiting for scan in Bilibili app...';
        startBilibiliPolling(currentBiliQrKey);
    } catch (err) {
        console.error('Failed to generate Bilibili QR code:', err);
        if (statusText) statusText.textContent = 'Failed to generate QR: ' + err.message;
        if (qrContainer) {
            qrContainer.innerHTML = `
                <div style="text-align: center; color: #ef4444; font-size: 12px; padding: 10px;">
                    <i class="ph ph-warning-circle" style="font-size: 28px;"></i><br>
                    Generation Failed<br>
                    <button class="btn-ghost btn-sm" onclick="generateNewBilibiliQR()" style="margin-top: 8px;">Retry</button>
                </div>
            `;
        }
    }
}

/**
 * Polls QR code status every 2 seconds
 */
function startBilibiliPolling(qrcodeKey) {
    stopBilibiliPolling();

    bilibiliPollTimer = setInterval(async () => {
        try {
            const resp = await fetch(`/api/bilibili/qrcode/poll?qrcode_key=${encodeURIComponent(qrcodeKey)}`);
            if (!resp.ok) return;

            const res = await resp.json();
            const statusText = document.getElementById('bilibili-qr-status-text');
            const statusDot = document.querySelector('.qr-status-dot');

            if (res.code === 86101) {
                // Waiting for scan
                if (statusText) statusText.textContent = 'Waiting for scan with Bilibili app...';
                if (statusDot) statusDot.className = 'qr-status-dot';
            } else if (res.code === 86090) {
                // Scanned, waiting confirmation
                if (statusText) statusText.textContent = '📱 Scanned! Please tap Confirm on your phone...';
                if (statusDot) statusDot.className = 'qr-status-dot scanned';
            } else if (res.code === 86038) {
                // Expired
                if (statusText) statusText.textContent = '⚠️ QR Code expired. Click below to refresh.';
                if (statusDot) statusDot.className = 'qr-status-dot expired';
                stopBilibiliPolling();
            } else if (res.code === 0 && res.status === 'success') {
                // Logged in!
                if (statusText) statusText.textContent = '🎉 Login successful! Initializing high-speed CDN...';
                if (statusDot) statusDot.className = 'qr-status-dot success';
                stopBilibiliPolling();

                showToast(`Logged into Bilibili as ${res.user?.uname || 'User'}! ⚡ 1080P & Uncapped Speed Active.`, 'success');
                updateBilibiliUIState({ logged_in: true, user: res.user });
            }
        } catch (e) {
            console.warn('Poll error:', e);
        }
    }, 2000);
}

function stopBilibiliPolling() {
    if (bilibiliPollTimer) {
        clearInterval(bilibiliPollTimer);
        bilibiliPollTimer = null;
    }
}

/**
 * Manually imports SESSDATA or Cookie string
 */
async function saveManualBilibiliCookie() {
    const input = document.getElementById('bilibili-manual-input');
    const val = (input ? input.value : '').trim();

    if (!val) {
        showToast('Please enter your Bilibili SESSDATA or cookie string!', 'error');
        return;
    }

    showToast('Validating cookies with Bilibili...', 'info');

    try {
        const resp = await fetch('/api/bilibili/cookies', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cookies: val }),
        });

        const res = await resp.json();
        if (!resp.ok) {
            throw new Error(res.detail || 'Cookie validation failed');
        }

        showToast(`Authenticated as ${res.user?.uname || 'User'}! ⚡ Uncapped download speed active.`, 'success');
        updateBilibiliUIState({ logged_in: true, user: res.user });
        if (input) input.value = '';
    } catch (err) {
        showToast(`Authentication failed: ${err.message}`, 'error');
    }
}

/**
 * Imports Bilibili session cookies from local browser
 */
async function importFromSelectedBrowser() {
    const browserSelect = document.getElementById('bili-browser-select');
    const browser = browserSelect ? browserSelect.value : 'chrome';

    showToast(`Reading Bilibili cookies from ${browser.toUpperCase()}...`, 'info');

    try {
        const resp = await fetch('/api/bilibili/browser-import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ browser: browser }),
        });

        const res = await resp.json();
        if (!resp.ok) {
            throw new Error(res.detail || `Could not read session from ${browser}`);
        }

        showToast(`Imported Bilibili session for ${res.user?.uname || 'User'}! ⚡ Fast downloads enabled.`, 'success');
        updateBilibiliUIState({ logged_in: true, user: res.user });
    } catch (err) {
        showToast(`Browser import error: ${err.message}`, 'error');
    }
}

/**
 * Re-validates login against Bilibili API
 */
async function refreshBilibiliStatus() {
    showToast('Refreshing Bilibili account status...', 'info');
    await checkBilibiliAuthStatus(true);
    showToast('Bilibili status updated!', 'success');
}

/**
 * Logs out of Bilibili
 */
async function logoutBilibili() {
    try {
        await fetch('/api/bilibili/logout', { method: 'POST' });
        showToast('Logged out of Bilibili. Reverted to guest download mode.', 'info');
        updateBilibiliUIState({ logged_in: false, user: null });
        generateNewBilibiliQR();
    } catch (e) {
        showToast('Error logging out: ' + e.message, 'error');
    }
}

