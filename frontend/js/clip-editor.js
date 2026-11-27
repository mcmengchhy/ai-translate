'use strict';

const clipDefaults = {flip: 'horizontal', speed: '1', color_filter: 'normal', sound_effect: 'none', original_volume: '100', effect_volume: '8'};
const clipFields = {flip: 'clip-flip', speed: 'clip-speed', color_filter: 'clip-color', sound_effect: 'clip-effect', original_volume: 'clip-original-volume', effect_volume: 'clip-effect-volume'};
const clipColors = {
    normal: 'none',
    vivid: 'contrast(1.12) saturate(1.35) brightness(1.01)',
    warm: 'sepia(0.22) saturate(1.1)',
    cool: 'sepia(0.15) hue-rotate(165deg)',
    grayscale: 'grayscale(1)',
    sepia: 'sepia(1)',
    cinematic: 'contrast(1.18) saturate(0.82) brightness(0.96)',
    vintage: 'sepia(0.3) contrast(0.9) saturate(0.72) brightness(1.07)',
    soft: 'contrast(0.88) saturate(0.92) brightness(1.05)',
    high_contrast: 'contrast(1.35) saturate(1.1) brightness(0.95)',
    muted: 'saturate(0.55) contrast(0.98)',
    bright: 'brightness(1.18) saturate(1.05)',
    dark: 'brightness(0.8) saturate(0.9)',
    golden_hour: 'sepia(0.35) saturate(1.25) brightness(1.06)',
    teal_orange: 'sepia(0.22) hue-rotate(335deg) contrast(1.08) saturate(1.25)',
    fade: 'contrast(0.76) brightness(1.13) saturate(0.78)',
};
const clipElement = id => document.getElementById(id);
let clipPreviewURL = null;
let clipUploading = false;
let clipHistoryLoading = false;
let clipHistoryPending = false;
let clipHistoryTimer = null;
let clipHistorySignature = null;

function clipMessage(message, kind = 'info') {
    clipElement('clip-message').textContent = message;
    clipElement('clip-message').dataset.kind = kind;
}

function clipOptions() {
    return Object.fromEntries(Object.entries(clipFields).map(([key, id]) => [key, clipElement(id).value]));
}

function clipUpdatePreview() {
    const options = clipOptions();
    const video = clipElement('clip-preview');
    const transforms = {none: 'none', horizontal: 'scaleX(-1)', vertical: 'scaleY(-1)', both: 'scale(-1, -1)'};
    video.style.transform = transforms[options.flip] || 'none';
    video.style.filter = clipColors[options.color_filter] || 'none';
    video.playbackRate = Number(options.speed);
    video.preservesPitch = true;
    video.volume = Number(options.original_volume) / 100;
    clipElement('clip-original-volume-label').textContent = `${options.original_volume}%`;
    clipElement('clip-effect-volume-label').textContent = `${options.effect_volume}%`;
    clipElement('clip-effect-volume').disabled = options.sound_effect === 'none';
    clipElement('clip-render').disabled = clipUploading || !clipElement('clip-file').files.length;
    const file = clipElement('clip-file').files[0];
    if (file) {
        const duration = Number.isFinite(video.duration) ? ` · about ${Math.ceil(video.duration / Number(options.speed))} seconds after editing` : '';
        clipElement('clip-file-info').textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB${duration}`;
    }
    try { localStorage.setItem('khmer_clip_editor_settings', JSON.stringify(options)); } catch (_) {}
}

function clipRestoreSettings(reset = false) {
    let saved = {};
    if (!reset) {
        try { saved = JSON.parse(localStorage.getItem('khmer_clip_editor_settings') || '{}') || {}; } catch (_) {}
    }
    for (const [key, id] of Object.entries(clipFields)) {
        const element = clipElement(id);
        const value = String(saved[key] ?? clipDefaults[key]);
        if (element.tagName === 'SELECT') {
            element.value = [...element.options].some(option => option.value === value) ? value : clipDefaults[key];
        } else {
            element.value = Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 100 ? value : clipDefaults[key];
        }
    }
    clipUpdatePreview();
}

function clipChooseFile() {
    if (clipPreviewURL) URL.revokeObjectURL(clipPreviewURL);
    clipPreviewURL = null;
    const file = clipElement('clip-file').files[0];
    const video = clipElement('clip-preview');
    video.pause();
    clipElement('clip-preview-empty').hidden = Boolean(file);
    clipElement('clip-preview-frame').hidden = !file;
    clipElement('clip-preview-controls').hidden = !file;
    clipElement('clip-preview-seek').value = '0';
    if (file) {
        clipPreviewURL = URL.createObjectURL(file);
        video.src = clipPreviewURL;
        clipMessage('Your clip is ready. Choose your edits and render.');
    } else {
        video.removeAttribute('src');
        video.load();
        clipElement('clip-file-info').textContent = '';
    }
    clipUpdatePreview();
}

async function clipRequest(url, options) {
    const response = await fetch(url, options);
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('The server returned an unreadable response.'); }
    if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map(item => `${item.loc?.at(-1) || 'Settings'}: ${item.msg}`).join('; ') : 'The request could not be completed.';
        throw new Error(detail);
    }
    return data;
}

function clipRenderHistory(exports) {
    const signature = JSON.stringify(exports);
    if (signature === clipHistorySignature) return;
    // Keep an already playing result in place when another clip's progress changes.
    const previous = new Map([...clipElement('clip-history').children].filter(node => node.dataset?.exportId).map(node => [node.dataset.exportId, node]));
    const rows = [];
    for (const item of exports) {
        if (item.status === 'completed' && previous.get(item.id)?.dataset.status === 'completed') {
            rows.push(previous.get(item.id));
            continue;
        }
        const row = document.createElement('article');
        row.className = 'export';
        row.dataset.exportId = item.id;
        row.dataset.status = item.status;
        const title = document.createElement('strong');
        title.textContent = item.jobs[0]?.title || 'Uploaded clip';
        const stage = document.createElement('p');
        stage.className = 'export-detail' + (item.status === 'failed' ? ' error' : '');
        stage.textContent = item.error || item.stage;
        row.append(title, stage);
        if (item.status === 'completed') {
            const url = `/api/video/exports/${encodeURIComponent(item.id)}/files/0/video`;
            const video = document.createElement('video');
            video.className = 'clip-result-video';
            video.controls = true;
            video.playsInline = true;
            video.preload = 'none';
            video.src = url;
            const download = document.createElement('a');
            download.className = 'button';
            download.textContent = 'Download edited clip (.mp4)';
            download.href = url;
            download.download = '';
            const detail = document.createElement('p');
            detail.className = 'hint';
            const options = item.edit_options || {};
            detail.textContent = `${Math.ceil(item.jobs[0]?.duration || 0)} seconds · ${options.speed || 1}× speed · ${options.color_filter || 'normal'} color · ${options.flip || 'none'} flip`;
            row.append(video, download, detail);
        } else if (['queued', 'processing'].includes(item.status)) {
            const progress = document.createElement('progress');
            progress.max = 100;
            progress.value = item.progress;
            progress.setAttribute('aria-label', 'Clip render progress');
            row.appendChild(progress);
        }
        rows.push(row);
    }
    if (!rows.length) {
        const empty = document.createElement('p');
        empty.textContent = 'No edited clips yet.';
        rows.push(empty);
    }
    clipElement('clip-history').replaceChildren(...rows);
    clipHistorySignature = signature;
}

async function clipLoadHistory() {
    if (clipHistoryLoading) { clipHistoryPending = true; return; }
    clipHistoryLoading = true;
    clearTimeout(clipHistoryTimer);
    clipElement('clip-refresh').disabled = true;
    try {
        const data = await clipRequest('/api/video/exports?kind=clip-edit');
        const exports = data.exports.filter(item => item.kind === 'clip-edit');
        clipRenderHistory(exports);
        if (exports.some(item => ['queued', 'processing'].includes(item.status))) clipHistoryTimer = setTimeout(clipLoadHistory, 2000);
    } catch (error) {
        clipMessage(error.message, 'error');
        clipHistoryTimer = setTimeout(clipLoadHistory, 5000);
    } finally {
        clipHistoryLoading = false;
        clipElement('clip-refresh').disabled = false;
        if (clipHistoryPending) {
            clipHistoryPending = false;
            clearTimeout(clipHistoryTimer);
            clipHistoryTimer = setTimeout(clipLoadHistory, 0);
        }
    }
}

async function clipSubmit(event) {
    event.preventDefault();
    if (clipUploading) return;
    const file = clipElement('clip-file').files[0];
    if (!file) { clipMessage('Choose a video clip first.', 'error'); return; }
    clipUploading = true;
    const button = clipElement('clip-render');
    button.disabled = true;
    button.textContent = 'Uploading clip…';
    clipMessage('Uploading your clip. Keep this page open until the upload finishes.');
    try {
        const form = new FormData();
        form.append('file', file);
        for (const [key, value] of Object.entries(clipOptions())) form.append(key, value);
        await clipRequest('/api/video/clip-edits', {method: 'POST', body: form});
        clipMessage('Clip queued. Your edited MP4 will appear below when it is ready.');
        await clipLoadHistory();
    } catch (error) { clipMessage(error.message, 'error'); }
    finally {
        clipUploading = false;
        button.textContent = 'Render edited clip';
        clipUpdatePreview();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    clipElement('clip-form').onsubmit = clipSubmit;
    clipElement('clip-file').onchange = clipChooseFile;
    clipElement('clip-preview').onloadedmetadata = clipUpdatePreview;
    clipElement('clip-preview-play').onclick = async () => {
        const video = clipElement('clip-preview');
        if (!video.paused) { video.pause(); return; }
        try { await video.play(); } catch (_) { clipMessage('This browser cannot play the preview. You can still try rendering the file.', 'error'); }
    };
    clipElement('clip-preview').onplay = () => { clipElement('clip-preview-play').textContent = 'Pause preview'; };
    clipElement('clip-preview').onpause = () => { clipElement('clip-preview-play').textContent = 'Play preview'; };
    clipElement('clip-preview').ontimeupdate = () => {
        const video = clipElement('clip-preview');
        if (Number.isFinite(video.duration) && video.duration > 0) clipElement('clip-preview-seek').value = String(video.currentTime / video.duration * 100);
    };
    clipElement('clip-preview-seek').oninput = event => {
        const video = clipElement('clip-preview');
        if (Number.isFinite(video.duration)) video.currentTime = Number(event.target.value) / 100 * video.duration;
    };
    clipElement('clip-preview').onerror = () => clipMessage('This browser cannot preview the file. You can still try rendering it.', 'error');
    clipElement('clip-reset').onclick = () => clipRestoreSettings(true);
    clipElement('clip-refresh').onclick = clipLoadHistory;
    for (const id of Object.values(clipFields)) clipElement(id).oninput = clipUpdatePreview;
    clipRestoreSettings();
    clipLoadHistory();
});

window.addEventListener('beforeunload', () => { if (clipPreviewURL) URL.revokeObjectURL(clipPreviewURL); });
