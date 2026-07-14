/* ============================================
   Owl Post — Frontend Logic
   ============================================ */

(function () {
    'use strict';

    // ============================================
    // Close-on-hotkey (mirrors the Python hotkey so overlay can be toggled closed)
    // ============================================

    const HOTKEY_CODE_MAP = {
        'backquote': 'Backquote', 'tilde': 'Backquote', '`': 'Backquote', '~': 'Backquote',
        'f1': 'F1', 'f2': 'F2', 'f3': 'F3', 'f4': 'F4', 'f5': 'F5', 'f6': 'F6',
        'f7': 'F7', 'f8': 'F8', 'f9': 'F9', 'f10': 'F10', 'f11': 'F11', 'f12': 'F12',
        'home': 'Home', 'end': 'End', 'insert': 'Insert',
    };

    async function setupCloseHotkey() {
        try {
            const resp = await fetch('/owlpost/api/hotkey');
            if (!resp.ok) return;
            const data = await resp.json();
            const code = HOTKEY_CODE_MAP[data.hotkey] || HOTKEY_CODE_MAP['backquote'];

            document.addEventListener('keydown', (e) => {
                if (e.code === code && !e.ctrlKey && !e.altKey && !e.metaKey) {
                    e.preventDefault();
                    window.close();
                }
            });
        } catch (err) {
            // Fallback: backtick always closes
            document.addEventListener('keydown', (e) => {
                if (e.code === 'Backquote' && !e.ctrlKey && !e.altKey && !e.metaKey) {
                    e.preventDefault();
                    window.close();
                }
            });
        }
    }

    // ============================================
    // Game Time Display
    // ============================================

    const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

    let gameTimePollTimer = null;
    let currentGameMinutesGlobal = 0;

    async function pollGameTime() {
        try {
            const resp = await fetch('/health');
            if (!resp.ok) return;
            const data = await resp.json();
            updateGameTimeHeader(data.game_time);
        } catch (_) {}
    }

    function updateGameTimeHeader(data) {
        const el = document.getElementById('owlGameTime');
        if (!el) return;

        if (!data || !data.available || !data.gameTime) {
            el.classList.remove('visible');
            return;
        }

        const dayName = DAYS[data.dayOfWeek] || '';
        const monthName = MONTHS[(data.month || 1) - 1] || '';

        el.textContent = `${dayName}, ${monthName} ${data.day} — ${data.gameTime}`;
        el.classList.add('visible');

        // Compute game minutes for proposal expiry checks
        const year = data.year || 1890;
        const month = data.month || 1;
        const day = data.day || 1;
        const gm = parseGameDatetimeToMinutes(`${month}/${day}/${year} ${data.gameTime}`);
        if (gm != null) currentGameMinutesGlobal = gm;
    }

    function startGameTimePoll() {
        pollGameTime();
        gameTimePollTimer = setInterval(pollGameTime, 10000);
    }

    // ============================================
    // Sounds
    // ============================================

    const sndPageTurn = new Audio('/owlpost/sounds/page-turn.mp3');
    const sndBirdWings = new Audio('/owlpost/sounds/bird-wings.mp3');

    function playSound(snd) {
        snd.currentTime = 0;
        snd.play().catch(() => {});
    }

    function playOwlSendAnimation() {
        const el = document.getElementById('owlSend');
        if (!el) return;
        // Reset: remove classes, force reflow, then trigger
        el.classList.remove('animating');
        el.classList.add('hidden');
        void el.offsetWidth; // force reflow
        playSound(sndBirdWings);
        el.classList.remove('hidden');
        el.classList.add('animating');
        // Clean up after animation ends
        el.addEventListener('animationend', () => {
            el.classList.remove('animating');
            el.classList.add('hidden');
        }, { once: true });
    }

    // ============================================
    // State
    // ============================================

    let voiceIds = [];
    let displayNames = {};  // recipient ID -> display name
    let currentMailThread = null;   // thread_id being viewed
    let currentMailRecipient = null; // the NPC in the current mail thread
    let currentMailSubject = null;   // subject for replies
    let currentBoardSlug = null;    // board slug being viewed
    let currentBoardName = '';
    let currentThreadRoot = null;   // root_post_id being viewed
    let currentThreadTitle = '';

    let mailPollTimer = null;
    let boardPollTimer = null;
    let currentThreadMsgCount = 0; // track message count to detect new arrivals

    // ============================================
    // DOM references
    // ============================================

    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    // Nav
    const boardsEnabled = new URLSearchParams(location.search).get('boards') !== '0';
    const tabMail = $('[data-view="mail"]');
    const tabBoards = $('[data-view="boards"]');
    if (!boardsEnabled && tabBoards) tabBoards.style.display = 'none';

    // Mail
    const mailView = $('#mailView');
    const mailInbox = $('#mailInbox');
    const mailList = $('#mailList');
    const mailThread = $('#mailThread');
    const mailThreadSubject = $('#mailThreadSubject');
    const mailThreadMessages = $('#mailThreadMessages');
    const mailBackToInbox = $('#mailBackToInbox');
    const mailThreadDelete = $('#mailThreadDelete');

    // Compose
    const composeToggle = $('#composeToggle');
    const composeView = $('#composeView');
    const composeBackToInbox = $('#composeBackToInbox');
    const composeRecipient = $('#composeRecipient');
    const composeSubject = $('#composeSubject');
    const composeBody = $('#composeBody');
    const composeSend = $('#composeSend');

    // Boards
    const boardsView = $('#boardsView');
    const boardsList = $('#boardsList');
    const boardListContainer = $('#boardListContainer');
    const boardThreadList = $('#boardThreadList');
    const boardThreadListName = $('#boardThreadListName');
    const boardBackToList = $('#boardBackToList');
    const threadListContainer = $('#threadListContainer');
    const newThreadToggle = $('#newThreadToggle');
    const newThreadForm = $('#newThreadForm');
    const newThreadTitle = $('#newThreadTitle');
    const newThreadBody = $('#newThreadBody');
    const newThreadCancel = $('#newThreadCancel');
    const newThreadSubmit = $('#newThreadSubmit');

    // Board thread detail
    const boardThreadDetail = $('#boardThreadDetail');
    const threadBackToBoards = $('#threadBackToBoards');
    const threadBackToBoard = $('#threadBackToBoard');
    const threadDetailTitle = $('#threadDetailTitle');
    const boardThreadMessages = $('#boardThreadMessages');
    const boardReplyBody = $('#boardReplyBody');
    const boardReplySend = $('#boardReplySend');

    // Password modal
    const passwordModal = $('#passwordModal');
    const passwordModalReason = $('#passwordModalReason');
    const passwordInput = $('#passwordInput');
    const passwordError = $('#passwordError');
    const passwordCancel = $('#passwordCancel');
    const passwordSubmit = $('#passwordSubmit');

    // Toast
    const toastEl = $('#toast');

    // ============================================
    // Utility
    // ============================================

    function showToast(msg, isError) {
        toastEl.textContent = msg;
        toastEl.classList.toggle('error', !!isError);
        toastEl.classList.add('visible');
        setTimeout(() => toastEl.classList.remove('visible'), 3000);
    }

    async function api(url, options, uiOptions) {
        try {
            const resp = await fetch(url, options);
            const data = await resp.json();
            if (!resp.ok) {
                const errMsg = data.error || `Error ${resp.status}`;
                const suppressErrorToast =
                    uiOptions?.suppressErrorToast === true
                    || (typeof uiOptions?.suppressErrorToast === 'function' && uiOptions.suppressErrorToast(resp, data));
                if (!suppressErrorToast) {
                    if (resp.status === 403) {
                        showToast(errMsg, true);
                    } else if (resp.status === 503) {
                        showToast('Game not connected. Please ensure the game is running.', true);
                    } else {
                        showToast(errMsg, true);
                    }
                }
                return { _error: true, status: resp.status, message: errMsg };
            }
            return data;
        } catch (err) {
            showToast('Connection error. Is the server running?', true);
            return { _error: true, status: 0, message: err.message };
        }
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function renderInlineMarkdown(str) {
        // Escape HTML first, then convert *text* to <em>text</em>
        return escapeHtml(str).replace(/\*([^*]+)\*/g, '<em>$1</em>');
    }

    const MONTH_NAMES = [
        '', 'January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'
    ];

    const MONTH_ABBR = [
        '', 'Jan.', 'Feb.', 'Mar.', 'Apr.', 'May', 'Jun.',
        'Jul.', 'Aug.', 'Sep.', 'Oct.', 'Nov.', 'Dec.'
    ];

    function formatGameTime(totalMinutes, short) {
        if (totalMinutes == null) return '';
        // Reverse the encoding from _game_datetime_to_minutes:
        // days = (year-1890)*365 + (month-1)*30 + (day-1)
        // totalMinutes = days*1440 + hour*60 + minute
        const days = Math.floor(totalMinutes / 1440);
        const remaining = totalMinutes % 1440;
        const hour = Math.floor(remaining / 60);
        const minute = remaining % 60;

        const year = 1890 + Math.floor(days / 365);
        const remDays = days % 365;
        const month = Math.min(Math.max(1 + Math.floor(remDays / 30), 1), 12);
        const day = Math.min(Math.max(1 + remDays % 30, 1), 31);

        const period = hour >= 12 ? 'PM' : 'AM';
        const h12 = hour % 12 || 12;
        const monthStr = short ? MONTH_ABBR[month] : MONTH_NAMES[month];

        return `${monthStr} ${day}, ${year} — ${h12}:${String(minute).padStart(2, '0')} ${period}`;
    }

    function parseGameDatetimeToMinutes(datetimeStr) {
        // Parses "M/D/YYYY H:MM AM/PM" to game minutes
        // Returns null on failure
        if (!datetimeStr) return null;
        const m = datetimeStr.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)$/i);
        if (!m) return null;
        const month = parseInt(m[1], 10);
        const day = parseInt(m[2], 10);
        const year = parseInt(m[3], 10);
        let hour = parseInt(m[4], 10);
        const minute = parseInt(m[5], 10);
        const period = m[6].toUpperCase();
        if (period === 'PM' && hour !== 12) hour += 12;
        if (period === 'AM' && hour === 12) hour = 0;
        // Same encoding as _game_datetime_to_minutes
        const days = (year - 1890) * 365 + (month - 1) * 30 + (day - 1);
        return days * 1440 + hour * 60 + minute;
    }

    function formatSenderName(name) {
        if (!name) return 'Unknown';
        if (name === 'player') return 'You';
        return displayNames[name] || name.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2');
    }

    // ============================================
    // View Switching
    // ============================================

    function switchView(view) {
        stopLetterReadback();

        // Update tabs
        tabMail.classList.toggle('active', view === 'mail');
        tabBoards.classList.toggle('active', view === 'boards');

        // Show/hide views
        mailView.classList.toggle('active', view === 'mail');
        boardsView.classList.toggle('active', view === 'boards');

        // Reset sub-views
        if (view === 'mail') {
            showMailInbox();
            loadMail();
            startMailPoll();
            stopBoardPoll();
        } else {
            showBoardList();
            loadBoards();
            startBoardPoll();
            stopMailPoll();
        }
    }

    // ============================================
    // Mail — Inbox
    // ============================================

    function showMailInbox() {
        stopLetterReadback();
        mailInbox.style.display = '';
        mailThread.classList.remove('active');
        composeView.classList.remove('active');
        currentMailThread = null;
        currentThreadMsgCount = 0;
    }

    async function loadMail() {
        const data = await api('/owlpost/api/mail');
        if (data._error) return;

        const mail = data.mail || [];

        // Update tab badge
        const unread = mail.filter(m => !m.read && m.sender !== 'player' && !m.in_flight).length;
        updateTabBadge('mail', unread);

        if (mail.length === 0) {
            mailList.innerHTML = '<div class="empty-state"><div class="empty-icon">&#9993;</div>No mail yet. Send a letter or check back later!</div>';
            return;
        }

        // Group by thread, show most recent per thread
        const threads = new Map();
        for (const m of mail) {
            const tid = m.thread_id;
            if (!threads.has(tid)) {
                threads.set(tid, {
                    thread_id: tid,
                    subject: m.subject || '(No Subject)',
                    latest: m,
                    hasUnread: false,
                    hasInFlight: false,
                    hasPendingProposal: false,
                    mailType: 'letter',
                });
            }
            const t = threads.get(tid);
            // Track unread received mail
            if (!m.read && m.sender !== 'player' && !m.in_flight) {
                t.hasUnread = true;
            }
            // Track in-flight mail (sent by player, not yet arrived)
            if (m.sender === 'player' && m.in_flight) {
                t.hasInFlight = true;
            }
            if ((m.pending_proposals || 0) > 0) {
                t.hasPendingProposal = true;
            }
            if (m.mail_type && m.mail_type !== 'letter' && m.sender !== 'player') {
                t.mailType = m.mail_type;
            }
            // Keep track of latest message
            if (m.sent_at > t.latest.sent_at) {
                t.latest = m;
            }
        }

        // Sort threads by most recent first
        const sorted = [...threads.values()].sort((a, b) => b.latest.sent_at - a.latest.sent_at);

        let html = '';
        for (const t of sorted) {
            const m = t.latest;
            const otherParty = m.sender === 'player' ? m.recipient : m.sender;
            const unreadCls = t.hasUnread ? ' unread' : '';
            const flightCls = t.hasInFlight ? ' in-flight' : '';

            const typeCls = t.mailType !== 'letter' ? ` ${t.mailType}` : '';
            html += `<div class="mail-item${unreadCls}${flightCls}${typeCls}" data-thread="${escapeHtml(t.thread_id)}">`;
            html += `<div class="mail-envelope-text">`;
            html += `<div class="mail-sender">${escapeHtml(formatSenderName(otherParty))}</div>`;
            html += `<div class="mail-subject">${escapeHtml(t.subject)}</div>`;
            html += `<div class="mail-date">${escapeHtml(formatGameTime(m.sent_at, true))}</div>`;
            html += `</div>`;
            if (t.hasUnread) {
                const sealImg = t.hasPendingProposal ? 'seal-stamped.webp' : 'seal.webp';
                html += `<img class="mail-seal" src="/owlpost/images/${sealImg}" alt="">`;
            }
            if (t.hasInFlight) {
                html += `<div class="mail-flight">&#129417;</div>`;
            }
            html += `</div>`;
        }

        mailList.innerHTML = html;

        // Attach click handlers
        mailList.querySelectorAll('.mail-item').forEach(el => {
            el.addEventListener('click', () => {
                const threadId = el.dataset.thread;
                loadMailThread(threadId);
            });
        });
    }

    // ============================================
    // Mail — Thread Detail
    // ============================================

    async function loadMailThread(threadId) {
        playSound(sndPageTurn);
        currentMailThread = threadId;
        mailInbox.style.display = 'none';
        mailThread.classList.add('active');

        mailThreadMessages.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading...</div>';

        const data = await api(`/owlpost/api/mail/thread/${encodeURIComponent(threadId)}`);
        if (data._error) return;

        const messages = data.messages || [];
        currentThreadMsgCount = messages.length;
        if (messages.length > 0) {
            mailThreadSubject.textContent = messages[0].subject || 'Thread';
            const threadType = (messages.find(m => m.mail_type && m.mail_type !== 'letter' && m.sender !== 'player') || {}).mail_type;
            mailThreadSubject.classList.toggle('howler', threadType === 'howler');
            currentMailSubject = messages[0].subject || 'Re';
            // Find the NPC in this thread (the non-player party)
            const npc = messages.find(m => m.sender !== 'player');
            currentMailRecipient = npc ? npc.sender : (messages[0].recipient !== 'player' ? messages[0].recipient : null);
        }

        // Find the last message to place the reply stamp on it
        const lastIdx = messages.length - 1;

        let html = '';
        for (let i = 0; i < messages.length; i++) {
            const m = messages[i];
            const isPlayer = m.sender === 'player';
            const playerCls = isPlayer ? ' from-player' : '';
            const transitCls = m.in_flight ? ' in-transit' : '';
            const unreadCls = (!m.read && !isPlayer && !m.in_flight) ? ' unread' : '';

            const msgTypeCls = (m.mail_type && m.mail_type !== 'letter') ? ` ${m.mail_type}` : '';
            html += `<div class="message-bubble${playerCls}${transitCls}${unreadCls}${msgTypeCls}">`;
            if (!m.in_flight) {
                html += `<button class="mail-delete-btn" data-mail-id="${m.id}" title="Delete letter"><i data-lucide="x"></i></button>`;
            }
            html += `<div class="message-author">${escapeHtml(formatSenderName(m.sender))}</div>`;
            html += `<div class="message-body">${renderInlineMarkdown(m.body)}</div>`;
            // Commitment proposals
            const proposals = m.proposals || [];
            for (const p of proposals) {
                const proposalMinutes = parseGameDatetimeToMinutes(p.datetime);
                const isExpired = p.status === 'pending' && proposalMinutes != null && proposalMinutes < currentGameMinutesGlobal;
                const isPending = p.status === 'pending' && !isExpired;
                const statusCls = p.status === 'accepted' ? 'accepted'
                    : p.status === 'declined' ? 'declined'
                    : isExpired ? 'expired' : '';
                html += `<div class="proposal-card ${statusCls}">`;
                html += `<div class="proposal-header">Meeting Proposal</div>`;
                html += `<div class="proposal-details">`;
                html += `<span class="proposal-location">${escapeHtml(p.location)}</span>`;
                html += `<span class="proposal-datetime">${escapeHtml(p.datetime)}</span>`;
                html += `</div>`;
                if (isPending) {
                    html += `<div class="proposal-actions">`;
                    html += `<button class="proposal-btn accept" data-proposal-id="${p.id}">Accept</button>`;
                    html += `<button class="proposal-btn decline" data-proposal-id="${p.id}">Decline</button>`;
                    html += `</div>`;
                } else if (isExpired) {
                    html += `<div class="proposal-status">The hour has passed</div>`;
                } else {
                    html += `<div class="proposal-status">${p.status === 'accepted' ? 'Accepted' : 'Declined'}</div>`;
                }
                html += `</div>`;
            }
            html += `<div class="message-time">`;
            if (m.in_flight) {
                html += `&#129417; Owl en route...`;
            } else {
                html += escapeHtml(formatGameTime(m.sent_at));
            }
            html += `</div>`;

            html += `</div>`;

            // Reply stamp after the last letter (outside the bubble)
            if (i === lastIdx && currentMailRecipient) {
                html += `<button class="reply-stamp" id="mailReplyBtn" title="Write a reply"><img src="/owlpost/images/reply-to-owl-mail.webp" alt="Reply"></button>`;
            }

            // Mark as read if unread and arrived
            if (!m.read && !m.in_flight && !isPlayer) {
                api(`/owlpost/api/mail/${m.id}/read`, { method: 'POST' });
            }
        }

        mailThreadMessages.innerHTML = html;
        // Scroll to first unread, or top if all read
        const firstUnread = mailThreadMessages.querySelector('.unread');
        if (firstUnread) {
            firstUnread.scrollIntoView({ block: 'start' });
        } else {
            mailThreadMessages.scrollTop = 0;
        }

        // --- TTS: dual-mode readback buttons ---
        const bubbles = mailThreadMessages.querySelectorAll('.message-bubble');
        let firstUnreadNpcId = null;
        const npcEntries = [];

        for (let i = 0; i < messages.length; i++) {
            const m = messages[i];
            if (m.sender === 'player' || m.in_flight) continue;
            npcEntries.push({ msg: m, bubble: bubbles[i] });
            if (!m.read && !firstUnreadNpcId) firstUnreadNpcId = m.id;
        }

        // Check cache status for all NPC messages in parallel
        const checks = await Promise.all(
            npcEntries.map(e => api(`/owlpost/api/mail/${e.msg.id}/check-audio`))
        );

        npcEntries.forEach((e, idx) => {
            const audioData = checks[idx];
            const btn = document.createElement('button');
            btn.type = 'button';

            if (!audioData._error && audioData.cached) {
                btn.className = 'history-audio-toggle';
                btn.dataset.audioUrl = audioData.url;
                btn.dataset.mailId = e.msg.id;
                btn.dataset.state = 'stopped';
                btn.title = 'Play archived audio';
            } else {
                btn.className = 'letter-audio-btn';
                btn.dataset.mailId = e.msg.id;
                btn.dataset.state = 'stopped';
                btn.title = 'Read aloud';
            }

            btn.innerHTML = '<i data-lucide="play"></i>';
            e.bubble.appendChild(btn);
        });

        if (window.lucide) lucide.createIcons({ nodes: [mailThreadMessages] });

        // Auto-play the first unread NPC letter (matches scroll position)
        if (firstUnreadNpcId) {
            const cachedBtn = mailThreadMessages.querySelector(
                `.history-audio-toggle[data-mail-id="${firstUnreadNpcId}"]`
            );
            if (cachedBtn) {
                cachedBtn.click();
            } else {
                startLetterReadback(firstUnreadNpcId);
            }
        }
    }

    // ============================================
    // Mail — Letter TTS Readback (dual-mode: stream or cached)
    // ============================================

    let _activeReadbackId = null;
    let _readbackPollTimer = null;

    async function startLetterReadback(mailId) {
        // Stop any current readback first
        await stopLetterReadback();

        _activeReadbackId = mailId;
        _updateReadbackButtons(mailId, 'playing');

        const data = await api(
            `/owlpost/api/mail/${mailId}/read-aloud`,
            { method: 'POST' },
            {
                suppressErrorToast: (resp, data) =>
                    resp.status === 404
                    && typeof data?.error === 'string'
                    && data.error.startsWith('No TTS voice available for '),
            }
        );
        if (data._error) {
            _activeReadbackId = null;
            _updateReadbackButtons(mailId, 'stopped');
            if (data.status === 409) {
                showToast('Voice currently in use', true);
            }
            return;
        }

        if (data.mode === 'cached') {
            _activeReadbackId = null;
            _updateReadbackButtons(mailId, 'stopped');
            _swapToCachedButton(mailId, data.url, true);
            return;
        }

        if (data.mode === 'pending') {
            _activeReadbackId = null;
            _updateReadbackButtons(mailId, 'stopped');
            return;
        }

        // mode === 'streaming' — poll for completion
        _readbackPollTimer = setInterval(async () => {
            const status = await api('/owlpost/api/mail/reading-status');
            if (!status.playing || status.mail_id !== mailId) {
                clearInterval(_readbackPollTimer);
                _readbackPollTimer = null;
                _activeReadbackId = null;
                _updateReadbackButtons(mailId, 'stopped');

                // Check if cache is now available → swap button
                const check = await api(`/owlpost/api/mail/${mailId}/check-audio`);
                if (!check._error && check.cached) {
                    _swapToCachedButton(mailId, check.url);
                }
            }
        }, 1000);
    }

    async function stopLetterReadback() {
        if (_readbackPollTimer) {
            clearInterval(_readbackPollTimer);
            _readbackPollTimer = null;
        }
        const prevId = _activeReadbackId;
        _activeReadbackId = null;
        if (prevId) {
            _updateReadbackButtons(prevId, 'stopped');
        }
        await api('/owlpost/api/mail/stop-reading', { method: 'POST' });
        if (window.HistoryAudioPlayer) window.HistoryAudioPlayer.stopAll();
    }

    function _updateReadbackButtons(mailId, state) {
        const btns = mailThreadMessages.querySelectorAll(`.letter-audio-btn[data-mail-id="${mailId}"]`);
        btns.forEach(btn => {
            btn.dataset.state = state;
            btn.title = state === 'playing' ? 'Stop reading' : 'Read aloud';
            btn.innerHTML = state === 'playing' ? '<i data-lucide="square"></i>' : '<i data-lucide="play"></i>';
            if (window.lucide) lucide.createIcons({ nodes: [btn] });
        });
    }

    function _swapToCachedButton(mailId, url, autoPlay) {
        const btns = mailThreadMessages.querySelectorAll(`.letter-audio-btn[data-mail-id="${mailId}"]`);
        btns.forEach(btn => {
            const newBtn = document.createElement('button');
            newBtn.type = 'button';
            newBtn.className = 'history-audio-toggle';
            newBtn.dataset.audioUrl = url;
            newBtn.dataset.mailId = mailId;
            newBtn.dataset.state = 'stopped';
            newBtn.title = 'Play archived audio';
            newBtn.innerHTML = '<i data-lucide="play"></i>';
            btn.replaceWith(newBtn);
            if (window.lucide) lucide.createIcons({ nodes: [newBtn] });
            if (autoPlay) newBtn.click();
        });
    }

    // Click handler for letter audio buttons (delegated)
    document.addEventListener('click', (e) => {
        const btn = e.target.closest('.letter-audio-btn');
        if (!btn) return;
        e.preventDefault();
        const mailId = parseInt(btn.dataset.mailId, 10);
        if (!mailId) return;

        if (_activeReadbackId === mailId) {
            stopLetterReadback();
        } else {
            startLetterReadback(mailId);
        }
    });

    // ============================================
    // Mail — Compose
    // ============================================

    function ensureComposeRecipientOption(recipientId) {
        if (!recipientId || recipientId === 'player') return;
        const existing = Array.from(composeRecipient.options || []).find(option => option.value === recipientId);
        if (existing) return;

        const opt = document.createElement('option');
        opt.value = recipientId;
        opt.textContent = formatSenderName(recipientId);
        composeRecipient.appendChild(opt);
    }

    async function loadRecipients() {
        // Load display names first
        try {
            const namesResp = await fetch('/owlpost/api/display-names', { cache: 'no-store' });
            if (namesResp.ok) {
                displayNames = await namesResp.json();
            }
        } catch (err) {
            console.error('Failed to load display names:', err);
        }

        // Load allowed Owl Mail recipients
        let recipients = [];
        try {
            const resp = await fetch('/owlpost/api/recipients', { cache: 'no-store' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            recipients = Array.isArray(data?.recipients) ? data.recipients : [];
            recipients.forEach(recipient => {
                if (recipient?.id && recipient?.name) {
                    displayNames[recipient.id] = recipient.name;
                }
            });
            voiceIds = recipients.map(recipient => recipient.id).filter(Boolean);
        } catch (err) {
            console.error('Failed to load Owl Post recipients:', err);
            voiceIds = [];
        }

        // Populate dropdown — display names shown, recipient IDs as values
        composeRecipient.innerHTML = '<option value="">Select a recipient...</option>';
        for (const recipient of recipients) {
            const opt = document.createElement('option');
            opt.value = recipient.id;
            opt.textContent = recipient.name || displayNames[recipient.id] || recipient.id;
            composeRecipient.appendChild(opt);
        }
    }

    let composeThreadId = null; // set when replying, null when composing new

    async function sendMail() {
        const recipient = composeRecipient.value;
        const subject = composeSubject.value.trim();
        const body = composeBody.value.trim();

        if (!recipient) { showToast('Please select a recipient.', true); return; }
        if (!subject) { showToast('Please enter a subject.', true); return; }
        if (!body) { showToast('Please write a message.', true); return; }

        composeSend.disabled = true;
        composeSend.textContent = 'Sending...';

        const payload = { recipient, subject, body };
        if (composeThreadId) payload.thread_id = composeThreadId;

        const data = await api('/owlpost/api/mail', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        composeSend.disabled = false;
        composeSend.textContent = 'Send Owl';

        if (data._error) return;

        playOwlSendAnimation();
        showToast('Owl dispatched!');
        composeSubject.value = '';
        composeBody.value = '';
        composeThreadId = null;

        // Back to inbox
        showMailInbox();
        loadMail();
    }

    // ============================================
    // Mail — Reply (opens compose view pre-filled)
    // ============================================

    function openMailReply() {
        if (!currentMailRecipient) { showToast('No recipient found'); return; }

        // Pre-fill the compose parchment
        ensureComposeRecipientOption(currentMailRecipient);
        composeRecipient.value = currentMailRecipient;
        composeSubject.value = currentMailSubject ? `Re: ${currentMailSubject.replace(/^Re:\s*/i, '')}` : '';
        composeBody.value = '';
        composeThreadId = currentMailThread; // reply stays in same thread

        // Switch to compose view
        mailInbox.style.display = 'none';
        mailThread.classList.remove('active');
        composeView.classList.add('active');
    }

    // ============================================
    // Boards — List
    // ============================================

    function showBoardList() {
        boardsList.style.display = '';
        boardThreadList.classList.remove('active');
        boardThreadDetail.classList.remove('active');
        currentBoardSlug = null;
        currentThreadRoot = null;
    }

    async function loadBoards() {
        const data = await api('/owlpost/api/boards');
        if (data._error) return;

        const boards = data.boards || [];

        // Update tab badge
        const totalUnread = boards.reduce((sum, b) => sum + (b.accessible ? (b.unread_count || 0) : 0), 0);
        updateTabBadge('boards', totalUnread);

        if (boards.length === 0) {
            boardListContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128220;</div>No notice boards available.</div>';
            return;
        }

        let html = '';
        for (const b of boards) {
            const locked = !b.accessible;
            const isPassword = b.access_type === 'password_locked';
            let cls = 'board-item';
            if (locked) {
                cls += ' locked';
                if (isPassword) cls += ' password-locked';
            }

            html += `<div class="${cls}" data-slug="${escapeHtml(b.slug)}" data-access="${escapeHtml(b.access_type)}" data-name="${escapeHtml(b.name)}">`;

            // Lock seal overlay for locked boards
            if (locked) {
                html += `<img class="board-lock-seal" src="/owlpost/images/seal.webp" alt="Locked" title="${escapeHtml(b.locked_reason || '')}">`;
            }

            // Info
            html += `<div class="board-info">`;
            html += `<div class="board-name">${escapeHtml(b.name)}</div>`;
            if (locked && b.locked_reason) {
                html += `<div class="board-desc">${escapeHtml(b.locked_reason)}</div>`;
            } else if (b.description) {
                html += `<div class="board-desc">${escapeHtml(b.description)}</div>`;
            }
            html += `</div>`;

            // Unread badge (only for accessible boards)
            if (b.accessible && b.unread_count > 0) {
                html += `<span class="board-unread">${b.unread_count}</span>`;
            }

            html += `</div>`;
        }

        boardListContainer.innerHTML = html;

        // Attach click handlers
        boardListContainer.querySelectorAll('.board-item').forEach(el => {
            el.addEventListener('click', () => {
                const slug = el.dataset.slug;
                const access = el.dataset.access;
                const name = el.dataset.name;

                if (el.classList.contains('locked')) {
                    if (access === 'password_locked') {
                        openPasswordModal(slug, name);
                    }
                    // decorative or house-locked: no action
                    return;
                }

                loadBoard(slug, name);
            });
        });
    }

    // ============================================
    // Boards — Thread List
    // ============================================

    async function loadBoard(slug, boardName) {
        currentBoardSlug = slug;
        currentBoardName = boardName || slug;
        currentThreadRoot = null;

        boardsList.style.display = 'none';
        boardThreadDetail.classList.remove('active');
        boardThreadList.classList.add('active');

        boardThreadListName.textContent = currentBoardName;
        threadListContainer.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading threads...</div>';

        // Reset new thread form
        newThreadForm.classList.remove('open');

        const data = await api(`/owlpost/api/boards/${encodeURIComponent(slug)}`);
        if (data._error) {
            if (data.status === 403) {
                showBoardList();
                loadBoards();
            }
            return;
        }

        if (data.board) {
            currentBoardName = data.board.name || currentBoardName;
            boardThreadListName.textContent = currentBoardName;
        }

        const threads = data.threads || [];

        if (threads.length === 0) {
            threadListContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">&#128220;</div>No threads yet. Start one or check back later!</div>';
            return;
        }

        let html = '';
        for (const t of threads) {
            const unreadCls = t.unread_count > 0 ? ' has-unread' : '';
            html += `<div class="thread-item${unreadCls}" data-root="${t.root_post_id}" data-title="${escapeHtml(t.title || '(Untitled)')}">`;
            html += `<div class="thread-card-text">`;
            html += `<div class="thread-card-title">${escapeHtml(t.title || '(Untitled)')}</div>`;
            html += `<div class="thread-card-author">— ${escapeHtml(formatSenderName(t.author))}</div>`;
            html += `</div>`;
            html += `<div class="thread-card-footer">`;
            html += `<span class="thread-card-replies">&#9993; ${t.reply_count || 0}</span>`;
            html += `</div>`;
            if (t.unread_count > 0) {
                html += `<img class="thread-seal" src="/owlpost/images/seal.webp" alt="">`;
            }
            html += `</div>`;
        }

        threadListContainer.innerHTML = html;

        // Attach click handlers
        threadListContainer.querySelectorAll('.thread-item').forEach(el => {
            el.addEventListener('click', () => {
                const rootId = parseInt(el.dataset.root, 10);
                const title = el.dataset.title;
                loadThread(currentBoardSlug, rootId, title);
            });
        });
    }

    // ============================================
    // Boards — Thread Detail
    // ============================================

    async function loadThread(slug, rootPostId, title) {
        currentThreadRoot = rootPostId;
        currentThreadTitle = title || 'Thread';

        boardThreadList.classList.remove('active');
        boardThreadDetail.classList.add('active');

        threadBackToBoard.textContent = currentBoardName;
        threadDetailTitle.textContent = currentThreadTitle;

        boardThreadMessages.innerHTML = '<div class="loading-state"><span class="spinner"></span> Loading...</div>';
        boardReplyBody.value = '';

        const data = await api(`/owlpost/api/boards/${encodeURIComponent(slug)}/${rootPostId}`);
        if (data._error) return;

        const posts = data.posts || [];

        // Mark thread as read
        api(`/owlpost/api/boards/${encodeURIComponent(slug)}/${rootPostId}/read-all`, { method: 'POST' });

        if (posts.length === 0) {
            boardThreadMessages.innerHTML = '<div class="empty-state">No posts found.</div>';
            return;
        }

        let html = '';
        for (const p of posts) {
            const isPlayer = p.author === 'player';
            const playerCls = isPlayer ? ' from-player' : '';
            const unreadCls = (!p.read && !isPlayer) ? ' unread' : '';
            html += `<div class="message-bubble${playerCls}${unreadCls}">`;
            html += `<div class="message-author">${escapeHtml(formatSenderName(p.author))}</div>`;
            html += `<div class="message-body">${renderInlineMarkdown(p.body)}</div>`;
            html += `<div class="message-time">${escapeHtml(formatGameTime(p.visible_at))}</div>`;
            html += `</div>`;
        }

        boardThreadMessages.innerHTML = html;
        // Scroll to first unread, or top if all read
        const firstUnread = boardThreadMessages.querySelector('.unread');
        if (firstUnread) {
            firstUnread.scrollIntoView({ block: 'start' });
        } else {
            boardThreadMessages.scrollTop = 0;
        }
    }

    // ============================================
    // Boards — Post / Reply
    // ============================================

    async function postToBoard(slug, rootPostId, parentId) {
        let body, titleVal;

        if (rootPostId == null) {
            // New thread
            titleVal = newThreadTitle.value.trim();
            body = newThreadBody.value.trim();
            if (!titleVal) { showToast('Please enter a thread title.', true); return; }
            if (!body) { showToast('Please write a message.', true); return; }
            newThreadSubmit.disabled = true;
        } else {
            // Reply
            body = boardReplyBody.value.trim();
            if (!body) { showToast('Please write a reply.', true); return; }
            boardReplySend.disabled = true;
        }

        const payload = { body };
        if (titleVal) payload.title = titleVal;
        if (rootPostId != null) payload.root_post_id = rootPostId;
        if (parentId != null) payload.parent_id = parentId;

        const data = await api(`/owlpost/api/boards/${encodeURIComponent(slug)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        if (rootPostId == null) {
            newThreadSubmit.disabled = false;
        } else {
            boardReplySend.disabled = false;
        }

        if (data._error) return;

        if (rootPostId == null) {
            // New thread created
            showToast('Thread posted!');
            newThreadTitle.value = '';
            newThreadBody.value = '';
            newThreadForm.classList.remove('open');
            loadBoard(slug, currentBoardName);
        } else {
            // Reply created
            showToast('Reply posted!');
            boardReplyBody.value = '';
            loadThread(slug, rootPostId, currentThreadTitle);
        }
    }

    // ============================================
    // Password Modal
    // ============================================

    let pendingUnlockSlug = null;
    let pendingUnlockName = null;

    function openPasswordModal(slug, name) {
        pendingUnlockSlug = slug;
        pendingUnlockName = name;
        passwordInput.value = '';
        passwordError.textContent = '';
        passwordModal.classList.remove('hidden');
        setTimeout(() => passwordInput.focus(), 50);
    }

    function closePasswordModal() {
        passwordModal.classList.add('hidden');
        pendingUnlockSlug = null;
        pendingUnlockName = null;
    }

    async function unlockBoard() {
        const password = passwordInput.value.trim();
        if (!password) { passwordError.textContent = 'Please enter a password.'; return; }

        passwordSubmit.disabled = true;
        passwordError.textContent = '';

        const data = await api(`/owlpost/api/boards/${encodeURIComponent(pendingUnlockSlug)}/unlock`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });

        passwordSubmit.disabled = false;

        if (data._error) {
            passwordError.textContent = data.message || 'Incorrect password.';
            return;
        }

        const slug = pendingUnlockSlug;
        const name = pendingUnlockName;
        closePasswordModal();
        showToast('Board unlocked!');
        loadBoard(slug, name);
    }

    // ============================================
    // Tab Badge
    // ============================================

    function updateTabBadge(view, count) {
        const tab = view === 'mail' ? tabMail : tabBoards;
        let badge = tab.querySelector('.tab-badge');
        if (count > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'tab-badge';
                tab.appendChild(badge);
            }
            badge.textContent = count > 99 ? '99+' : count;
        } else if (badge) {
            badge.remove();
        }
    }

    // ============================================
    // Polling
    // ============================================

    function startMailPoll() {
        stopMailPoll();
        mailPollTimer = setInterval(async () => {
            if (currentMailThread) {
                // Only reload thread if new messages arrived
                const data = await api(`/owlpost/api/mail/thread/${encodeURIComponent(currentMailThread)}`);
                if (!data._error && (data.messages || []).length !== currentThreadMsgCount) {
                    loadMailThread(currentMailThread);
                }
            } else {
                loadMail();
            }
        }, 30000);
    }

    function stopMailPoll() {
        if (mailPollTimer) {
            clearInterval(mailPollTimer);
            mailPollTimer = null;
        }
    }

    function startBoardPoll() {
        stopBoardPoll();
        boardPollTimer = setInterval(() => {
            if (currentThreadRoot != null) {
                loadThread(currentBoardSlug, currentThreadRoot, currentThreadTitle);
            } else if (currentBoardSlug) {
                loadBoard(currentBoardSlug, currentBoardName);
            } else {
                loadBoards();
            }
        }, 60000);
    }

    function stopBoardPoll() {
        if (boardPollTimer) {
            clearInterval(boardPollTimer);
            boardPollTimer = null;
        }
    }

    // ============================================
    // Event Bindings
    // ============================================

    function bindEvents() {
        // Tab switching
        tabMail.addEventListener('click', () => switchView('mail'));
        tabBoards.addEventListener('click', () => switchView('boards'));

        // Compose — open letter writing view (new thread)
        composeToggle.addEventListener('click', () => {
            composeThreadId = null; // new compose = new thread
            mailInbox.style.display = 'none';
            mailThread.classList.remove('active');
            composeView.classList.add('active');
        });

        // Compose — back to inbox
        composeBackToInbox.addEventListener('click', () => {
            showMailInbox();
            loadMail();
        });

        // Send mail
        composeSend.addEventListener('click', sendMail);

        // Mail reply — delegated since stamp is dynamically rendered
        mailThreadMessages.addEventListener('click', (e) => {
            if (e.target.closest('.reply-stamp')) openMailReply();
        });

        // Mail delete — delegated
        mailThreadMessages.addEventListener('click', async (e) => {
            const btn = e.target.closest('.mail-delete-btn');
            if (!btn) return;
            e.preventDefault();
            const mailId = parseInt(btn.dataset.mailId, 10);
            if (!mailId) return;

            const data = await api(`/owlpost/api/mail/${mailId}`, { method: 'DELETE' });
            if (data._error) return;

            showToast('Letter deleted');
            // Reload thread; if empty, return to inbox
            const threadData = await api(`/owlpost/api/mail/thread/${encodeURIComponent(currentMailThread)}`);
            if (!threadData._error && (threadData.messages || []).length > 0) {
                loadMailThread(currentMailThread);
            } else {
                showMailInbox();
                loadMail();
            }
        });

        // Proposal accept/decline — delegated
        mailThreadMessages.addEventListener('click', async (e) => {
            const btn = e.target.closest('.proposal-btn');
            if (!btn) return;
            e.preventDefault();

            const proposalId = parseInt(btn.dataset.proposalId, 10);
            if (!proposalId) return;

            const isAccept = btn.classList.contains('accept');
            const endpoint = isAccept ? 'accept' : 'decline';

            btn.disabled = true;
            const siblingBtn = btn.parentElement.querySelector(isAccept ? '.decline' : '.accept');
            if (siblingBtn) siblingBtn.disabled = true;

            const data = await api(`/owlpost/api/mail/proposals/${proposalId}/${endpoint}`, { method: 'POST' });

            if (data._error) {
                btn.disabled = false;
                if (siblingBtn) siblingBtn.disabled = false;
                return;
            }

            showToast(isAccept ? 'Meeting confirmed!' : 'Proposal declined.');
            // Reload thread to update proposal state
            loadMailThread(currentMailThread);
        });

        // Mail thread delete
        mailThreadDelete.addEventListener('click', async () => {
            if (!currentMailThread) return;
            const data = await api(`/owlpost/api/mail/thread/${encodeURIComponent(currentMailThread)}`, { method: 'DELETE' });
            if (data._error) return;
            showToast('Thread deleted');
            showMailInbox();
            loadMail();
        });

        // Mail back to inbox
        mailBackToInbox.addEventListener('click', () => {
            showMailInbox();
            loadMail();
        });

        // Board back to list
        boardBackToList.addEventListener('click', () => {
            showBoardList();
            loadBoards();
        });

        // Thread back to boards list
        threadBackToBoards.addEventListener('click', () => {
            showBoardList();
            loadBoards();
        });

        // Thread back to board
        threadBackToBoard.addEventListener('click', () => {
            if (currentBoardSlug) {
                boardThreadDetail.classList.remove('active');
                boardThreadList.classList.add('active');
                loadBoard(currentBoardSlug, currentBoardName);
            }
        });

        // New thread toggle
        newThreadToggle.addEventListener('click', () => {
            newThreadForm.classList.toggle('open');
        });

        // New thread cancel
        newThreadCancel.addEventListener('click', () => {
            newThreadForm.classList.remove('open');
            newThreadTitle.value = '';
            newThreadBody.value = '';
        });

        // New thread submit
        newThreadSubmit.addEventListener('click', () => {
            postToBoard(currentBoardSlug, null, null);
        });

        // Board reply send
        boardReplySend.addEventListener('click', () => {
            postToBoard(currentBoardSlug, currentThreadRoot, currentThreadRoot);
        });

        // Password modal
        passwordCancel.addEventListener('click', closePasswordModal);
        passwordSubmit.addEventListener('click', unlockBoard);
        passwordInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') unlockBoard();
        });

        // Close modal on backdrop click
        passwordModal.addEventListener('click', (e) => {
            if (e.target === passwordModal) closePasswordModal();
        });

        // Keyboard shortcut: Escape to close modal
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !passwordModal.classList.contains('hidden')) {
                closePasswordModal();
            }
        });
    }

    // ============================================
    // Initialization
    // ============================================

    // Precache images — critical ones block first render, rest are fire-and-forget
    function preloadImage(src) {
        return new Promise(resolve => {
            const img = new Image();
            img.onload = resolve;
            img.onerror = resolve; // don't block on failure
            img.src = src;
        });
    }

    function precacheImages() {
        const extras = [
            '/owlpost/images/owl-post-background.webp',
            '/owlpost/images/letter.webp',
            '/owlpost/images/letter-portrait.webp',
            '/owlpost/images/opened-scroll.webp',
            '/owlpost/images/opened-scroll-cord-layer.png',
            '/owlpost/images/send-owl-mail.webp',
            '/owlpost/images/reply-to-owl-mail.webp',
            '/owlpost/images/write-owl-mail.webp',
            '/owlpost/images/owl-mail-center.webp',
            '/owlpost/images/parchment-pinned.webp',
            '/owlpost/images/howler.webp',
            '/owlpost/images/howler-open.webp',
            '/owlpost/images/howler-open-envelope-layer.webp',
            '/owlpost/images/howler-letter.webp',
        ];
        for (const src of extras) {
            const img = new Image();
            img.src = src;
        }
    }

    async function init() {
        // Wait for images used in the mail grid before rendering
        const criticalImages = [
            '/owlpost/images/mail.webp',
            '/owlpost/images/mail-open.webp',
            '/owlpost/images/seal.webp',
            '/owlpost/images/owl-feet.webp',
        ];
        await Promise.all([
            Promise.all(criticalImages.map(preloadImage)),
            loadRecipients(),
            // Force-load fonts used in mail grid so layout is stable on first render
            document.fonts.load("1rem 'Crimson Text'").catch(() => {}),
            document.fonts.load("1rem 'MagicGlass'").catch(() => {}),
            document.fonts.load("1rem 'Cinzel'").catch(() => {}),
        ]);
        precacheImages();

        setupCloseHotkey();
        startGameTimePoll();
        bindEvents();
        await loadMail();
        // Fetch boards badge count without rendering the boards view
        if (boardsEnabled) {
            api('/owlpost/api/boards').then(data => {
                if (data._error) return;
                const total = (data.boards || []).reduce((s, b) => s + (b.accessible ? (b.unread_count || 0) : 0), 0);
                updateTabBadge('boards', total);
            });
        }
        startMailPoll();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
