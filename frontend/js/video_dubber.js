/**
 * Khmer AI Video Dubber & Bilibili Translator Client
 */

let activeVideoJobId = null;
let pollInterval = null;
let isSubmittingVideo = false;
let duplicateVideoSubmission = null;
const savedPipelineProgress = new Map();
let selectedVideoFiles = null;
let selectedVideoSubtitles = null;
let videoJobsRefreshing = false;
const videoJobSummaries = new Map();
const removingVideoJobs = new Set();
let videoJobsGeneration = 0;
const changingVideoJobs = new Set();
const expandedVideoJobSteps = new Set();

function setVideoSubmissionBusy(busy) {
    isSubmittingVideo = busy;
    for (const id of ['start-url-dub-btn', 'start-file-dub-btn']) {
        const button = document.getElementById(id);
        if (button) button.disabled = busy;
    }
    for (const id of ['video-file-input', 'video-sub-file-input']) {
        const input = document.getElementById(id);
        if (input) input.disabled = busy;
    }
}

function showDuplicateVideoJob(detail, kind, file = null) {
    duplicateVideoSubmission = { ...detail, kind, file };
    document.getElementById('duplicate-job-card').style.display = 'block';
    document.getElementById('duplicate-job-details').textContent =
        `Job ${detail.job_id} · ${detail.progress}% · ${detail.stage || detail.status}. ` +
        (detail.options_match ? 'Your processing settings match.' : 'Review the saved job settings before continuing.');
    document.getElementById('duplicate-resume-btn').style.display =
        ['interrupted', 'failed', 'paused'].includes(detail.status) ? '' : 'none';
}

async function chooseDuplicateJob(action) {
    const duplicate = duplicateVideoSubmission;
    if (!duplicate) return;
    duplicateVideoSubmission = null;
    document.getElementById('duplicate-job-card').style.display = 'none';
    if (action === 'new') {
        return duplicate.kind === 'file' ? handleFileDubbing(true, duplicate.file ? [duplicate.file] : null) : handleUrlDubbing(true);
    }
    if (action === 'resume') return resumeTrackedJob(duplicate.job_id);
    return loadCompletedDemoJob(duplicate.job_id);
}

async function resumeTrackedJob(jobId = activeVideoJobId) {
    if (!jobId || isSubmittingVideo || changingVideoJobs.has(jobId) || removingVideoJobs.has(jobId)) return;
    changingVideoJobs.add(jobId);
    const selectionGeneration = trackingGeneration;
    setVideoSubmissionBusy(true);
    renderVideoJobs();
    try {
        const response = await fetch(`/api/video/job/${jobId}/resume`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(getTranslationOptions())
        });
        const data = await response.json();
        if (!response.ok) throw new Error(videoApiError(data, 'Could not resume saved work'));
        if (selectionGeneration === trackingGeneration) {
            activeVideoJobId = jobId;
            setPipelineUIState('active');
            startPollingJobStatus(jobId);
        }
        refreshVideoJobs();
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        setVideoSubmissionBusy(false);
        changingVideoJobs.delete(jobId);
        renderVideoJobs();
    }
}

async function changeVideoCheckpoint(jobId, action, step = null) {
    if (changingVideoJobs.has(jobId) || removingVideoJobs.has(jobId)) return;
    changingVideoJobs.add(jobId);
    renderVideoJobs();
    try {
        const path = step ? `steps/${encodeURIComponent(step)}` : action;
        const response = await fetch(`/api/video/job/${encodeURIComponent(jobId)}/${path}`, {
            method: step ? 'DELETE' : 'POST'
        });
        const data = await response.json();
        if (!response.ok) throw new Error(videoApiError(data, 'Could not update saved work'));
        videoJobsGeneration++;
        const job = { ...videoJobSummaries.get(jobId), ...data, id: jobId };
        if (step) {
            delete job.download_url;
            delete job.srt_url;
        }
        if (!step) job.stage = 'Paused — resume saved work';
        videoJobSummaries.set(jobId, job);
        savedPipelineProgress.set(jobId, job);
        if (activeVideoJobId === jobId) {
            trackingGeneration++;
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = null;
            updatePipelineProgress(job);
        }
        if (step && studioJobId === jobId) {
            studioJobId = null;
            studioSegments = [];
            studioDirty = false;
            currentDownloadJob = null;
            document.getElementById('dubbed-video-player')?.pause();
            document.getElementById('video-results-card').style.display = 'none';
        }
        showToast(step ? 'Saved step cleared. Resume to regenerate it.' : 'Paused. Completed work is saved.', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        changingVideoJobs.delete(jobId);
        renderVideoJobs();
        refreshVideoJobs();
    }
}

function renderJobCheckpoints(job) {
    const details = document.createElement('details');
    details.className = 'video-job-checkpoints';
    details.open = expandedVideoJobSteps.has(job.id);
    details.ontoggle = () => {
        if (details.open) expandedVideoJobSteps.add(job.id);
        else expandedVideoJobSteps.delete(job.id);
    };
    const summary = document.createElement('summary');
    summary.textContent = 'Processing steps';
    details.appendChild(summary);
    const help = document.createElement('p');
    help.textContent = 'Clear saved steps from the bottom up, then Resume to regenerate. Pause running tasks first. Source videos and previous exports are kept.';
    details.appendChild(help);
    const latest = [...(job.steps || [])].reverse().find(step => step.state !== 'pending');
    for (const step of job.steps || []) {
        const row = document.createElement('div');
        row.className = 'video-job-checkpoint';
        row.setAttribute('data-step', step.id);
        row.setAttribute('data-state', step.state);
        const label = document.createElement('span');
        const state = { complete: 'Saved', partial: 'Partial', pending: 'Not started' }[step.state] || step.state;
        label.textContent = `${step.number}. ${step.label} · ${state}`;
        row.appendChild(label);
        if (step.state !== 'pending') {
            const clear = document.createElement('button');
            clear.className = step.can_clear ? 'btn-secondary btn-sm' : 'btn-ghost btn-sm';
            clear.textContent = 'Clear';
            clear.setAttribute('aria-label', `Clear step ${step.number}: ${step.label}`);
            clear.disabled = !step.can_clear || changingVideoJobs.has(job.id) || removingVideoJobs.has(job.id);
            clear.title = step.can_clear ? `Clear saved ${step.label.toLowerCase()} and regenerate it on Resume` :
                latest?.id !== step.id ? `Clear step ${latest?.number} (${latest?.label}) first` : 'Pause this task before clearing saved work';
            clear.onclick = () => changeVideoCheckpoint(job.id, 'clear', step.id);
            row.appendChild(clear);
        }
        details.appendChild(row);
    }
    return details;
}

document.addEventListener('DOMContentLoaded', () => {
    initVideoDubberEvents();
    initVoiceTunerControls();
    let preferences = {};
    try { preferences = JSON.parse(localStorage.getItem('khmer_video_render_options') || '{}'); } catch {}
    applyVideoRenderOptions('video', preferences);
    refreshVideoJobs();
    setInterval(refreshVideoJobs, 2500);
});

function renderVideoJobs() {
    const list = document.getElementById('video-jobs-list');
    if (!list) return;
    list.innerHTML = '';
    const priority = status => ['processing', 'running', 'rendering'].includes(status) ? 0 : status === 'queued' ? 1 : 2;
    const jobs = Array.from(videoJobSummaries.values()).sort((a, b) => priority(a.status) - priority(b.status));
    for (const job of jobs) {
        const row = document.createElement('div');
        row.className = 'video-job-row';
        row.setAttribute('data-status', job.status || 'queued');
        row.setAttribute('aria-current', String(job.id === activeVideoJobId));
        const info = document.createElement('div');
        info.className = 'video-job-info';
        const title = document.createElement('strong');
        title.textContent = job.title || job.id;
        const status = document.createElement('span');
        const label = ['processing', 'running', 'rendering'].includes(job.status) ? 'Running' :
            { queued: 'Queued', paused: 'Paused', pausing: 'Pausing' }[job.status] || job.status || 'Queued';
        status.textContent = `${label} · ${job.progress || 0}% · ${job.stage || 'Queued'} · Job ${job.id}`;
        info.appendChild(title);
        info.appendChild(status);
        if (job.error) {
            const error = document.createElement('span');
            error.className = 'video-job-error';
            error.textContent = job.error;
            info.appendChild(error);
        }
        const progress = document.createElement('progress');
        progress.max = 100;
        progress.value = job.progress || 0;
        progress.setAttribute('aria-label', `Progress for ${job.title || job.id}`);
        info.appendChild(progress);
        const actions = document.createElement('div');
        actions.className = 'video-job-actions';
        const open = document.createElement('button');
        open.className = 'btn-secondary btn-sm';
        open.textContent = job.id === activeVideoJobId ? 'Viewing' : 'Open';
        open.disabled = removingVideoJobs.has(job.id) || changingVideoJobs.has(job.id);
        open.onclick = () => loadCompletedDemoJob(job.id);
        actions.appendChild(open);
        if (['queued', 'processing', 'running', 'rendering', 'pausing'].includes(job.status)) {
            const pause = document.createElement('button');
            pause.className = 'btn-secondary btn-sm';
            pause.textContent = job.status === 'pausing' ? 'Pausing…' : 'Pause';
            pause.disabled = job.status === 'pausing' || changingVideoJobs.has(job.id) || removingVideoJobs.has(job.id);
            pause.onclick = () => changeVideoCheckpoint(job.id, 'pause');
            actions.appendChild(pause);
        }
        if (['failed', 'interrupted', 'paused'].includes(job.status)) {
            const resume = document.createElement('button');
            resume.className = 'btn-primary btn-sm';
            resume.textContent = 'Resume';
            resume.disabled = isSubmittingVideo || removingVideoJobs.has(job.id) || changingVideoJobs.has(job.id);
            resume.onclick = () => resumeTrackedJob(job.id);
            actions.appendChild(resume);
        }
        for (const [field, label] of [['download_url', 'Video'], ['srt_url', 'Subtitles']]) {
            if (!job[field]) continue;
            const link = document.createElement('a');
            link.className = 'btn-secondary btn-sm';
            link.textContent = label;
            link.href = job[field];
            link.download = '';
            actions.appendChild(link);
        }
        const remove = document.createElement('button');
        remove.className = 'btn-ghost btn-sm video-task-remove';
        remove.textContent = removingVideoJobs.has(job.id) ? 'Removing…' : 'Remove';
        remove.disabled = removingVideoJobs.has(job.id) || changingVideoJobs.has(job.id);
        remove.title = 'Stop and remove this task. Uploaded videos and completed exports are kept.';
        remove.onclick = () => removeVideoTask(job.id);
        actions.appendChild(remove);
        row.appendChild(info);
        row.appendChild(actions);
        if (job.steps?.length) row.appendChild(renderJobCheckpoints(job));
        list.appendChild(row);
    }
    if (!videoJobSummaries.size) list.textContent = 'No video jobs yet. Upload videos to start.';
}

async function refreshVideoJobs() {
    if (videoJobsRefreshing) return;
    videoJobsRefreshing = true;
    const generation = videoJobsGeneration;
    try {
        const response = await fetch('/api/video/jobs');
        if (response.status === 404) {
            document.getElementById('video-jobs-summary').textContent = 'Restart the server to activate the updated job list.';
            return;
        }
        if (!response.ok) throw new Error('Could not load video jobs');
        const data = await response.json();
        if (generation !== videoJobsGeneration) return;
        videoJobSummaries.clear();
        for (const job of data.jobs) videoJobSummaries.set(job.id, job);
        renderVideoJobs();
        const running = data.processing_count ?? Math.max(0, data.active_count - data.queued_count);
        document.getElementById('video-jobs-summary').textContent =
            `${running} / ${data.max_concurrent_jobs} running · ${data.queued_count} queued`;
        const clearButton = document.getElementById('clear-video-tasks-btn');
        if (clearButton) {
            clearButton.disabled = data.active_count > 0 || isSubmittingVideo || removingVideoJobs.size > 0 || changingVideoJobs.size > 0;
            clearButton.title = data.active_count > 0 ? 'Remove active tasks before clearing caches.' :
                'Clear task history and processing caches. Uploaded videos and exports are kept.';
        }
    } catch (error) {
        const summary = document.getElementById('video-jobs-summary');
        if (summary) summary.textContent = 'Reconnecting to server...';
    } finally {
        videoJobsRefreshing = false;
    }
}

function forgetVideoTask(jobId) {
    videoJobSummaries.delete(jobId);
    savedPipelineProgress.delete(jobId);
    expandedVideoJobSteps.delete(jobId);
    if (activeVideoJobId === jobId) {
        trackingGeneration++;
        if (pollInterval) clearInterval(pollInterval);
        pollInterval = null;
        activeVideoJobId = null;
        localStorage.removeItem('khmer_active_video_job');
        setPipelineUIState('idle');
    }
    if (studioJobId === jobId) {
        studioJobId = null;
        studioSegments = [];
        studioDirty = false;
        currentDownloadJob = null;
        const player = document.getElementById('dubbed-video-player');
        player?.pause();
        player?.removeAttribute('src');
        document.getElementById('video-results-card').style.display = 'none';
    }
    if (duplicateVideoSubmission?.job_id === jobId) {
        duplicateVideoSubmission = null;
        document.getElementById('duplicate-job-card').style.display = 'none';
    }
}

async function removeVideoTask(jobId) {
    if (removingVideoJobs.has(jobId) || changingVideoJobs.has(jobId)) return;
    removingVideoJobs.add(jobId);
    renderVideoJobs();
    try {
        const response = await fetch(`/api/video/job/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        const data = await response.json();
        if (!response.ok && response.status !== 404) throw new Error(videoApiError(data, 'Could not remove task'));
        videoJobsGeneration++;
        forgetVideoTask(jobId);
        showToast('Task removed. Uploaded videos and completed exports are kept.', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        removingVideoJobs.delete(jobId);
        renderVideoJobs();
    }
}

async function clearVideoTasksAndCache() {
    if (isSubmittingVideo || removingVideoJobs.size || changingVideoJobs.size) return;
    if (!window.confirm('Clear all task history, saved transcripts, generated voices and processing caches? Uploaded videos and completed exports will be kept.')) return;
    const button = document.getElementById('clear-video-tasks-btn');
    button.disabled = true;
    setVideoSubmissionBusy(true);
    try {
        const response = await fetch('/api/video/jobs', { method: 'DELETE' });
        const data = await response.json();
        if (!response.ok) throw new Error(videoApiError(data, 'Could not clear task caches'));
        videoJobsGeneration++;
        for (const id of new Set([...videoJobSummaries.keys(), activeVideoJobId, studioJobId])) {
            if (id) forgetVideoTask(id);
        }
        videoJobSummaries.clear();
        savedPipelineProgress.clear();
        localStorage.removeItem('khmer_active_video_job');
        selectVideoFiles([]);
        selectedVideoSubtitles = [];
        for (const id of ['video-file-input', 'video-sub-file-input']) document.getElementById(id).value = '';
        document.getElementById('selected-sub-filename').style.display = 'none';
        document.getElementById('video-upload-status').textContent = '';
        showToast('Tasks and processing caches cleared. Ready for new videos.', 'success');
        renderVideoJobs();
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        setVideoSubmissionBusy(false);
        button.disabled = false;
        refreshVideoJobs();
    }
}

function selectVideoFiles(files) {
    selectedVideoFiles = Array.from(files);
    const totalMB = selectedVideoFiles.reduce((total, file) => total + file.size, 0) / (1024 * 1024);
    document.getElementById('selected-video-filename').textContent = selectedVideoFiles.length
        ? `${selectedVideoFiles.length} selected (${totalMB.toFixed(1)} MB): ${selectedVideoFiles.map(file => file.name).join(', ')}`
        : 'Click or drag videos here';
    document.getElementById('video-upload-action-wrap').style.display = selectedVideoFiles.length ? 'block' : 'none';
}

const DEFAULT_VIDEO_RENDER_OPTIONS = {
    add_logo: false, logo_id: 'cinekhmer', blur_chinese_subtitles: false,
    blur_x: 0.10, blur_y: 0.80, blur_width: 0.80, blur_height: 0.14, burn_subtitles: false
};

function getVideoRenderOptions(prefix = 'video') {
    const options = { ...DEFAULT_VIDEO_RENDER_OPTIONS };
    options.add_logo = Boolean(document.getElementById(`${prefix}-add-logo`)?.checked);
    options.logo_id = document.getElementById(`${prefix}-logo-id`)?.value || options.logo_id;
    options.blur_chinese_subtitles = Boolean(document.getElementById(`${prefix}-blur-subtitles`)?.checked);
    const burnId = prefix === 'video' ? 'burn-subs-checkbox' : 'studio-burn-subs-checkbox';
    options.burn_subtitles = Boolean(document.getElementById(burnId)?.checked);
    for (const name of ['x', 'y', 'width', 'height']) {
        const input = document.getElementById(`${prefix}-blur-${name}`);
        const value = parseFloat(input?.value);
        if (Number.isFinite(value)) options[`blur_${name}`] = value / 100;
    }
    return options;
}

function applyVideoRenderOptions(prefix, values = {}) {
    const options = { ...DEFAULT_VIDEO_RENDER_OPTIONS, ...values };
    const logo = document.getElementById(`${prefix}-logo-id`);
    if (logo) {
        if (options.logo_id.startsWith('custom_') && !Array.from(logo.options).some(item => item.value === options.logo_id)) {
            const item = document.createElement('option');
            item.value = options.logo_id;
            item.textContent = 'Your uploaded logo';
            logo.appendChild(item);
        }
        logo.value = options.logo_id;
    }
    for (const [suffix, value] of [['add-logo', options.add_logo], ['blur-subtitles', options.blur_chinese_subtitles]]) {
        const input = document.getElementById(`${prefix}-${suffix}`);
        if (input) input.checked = value;
    }
    const burn = document.getElementById(prefix === 'video' ? 'burn-subs-checkbox' : 'studio-burn-subs-checkbox');
    if (burn) burn.checked = options.burn_subtitles;
    for (const name of ['x', 'y', 'width', 'height']) {
        const input = document.getElementById(`${prefix}-blur-${name}`);
        if (input) input.value = Math.round(options[`blur_${name}`] * 100);
    }
    const preview = document.getElementById(`${prefix}-logo-preview`);
    if (preview) preview.src = `/api/video/logo/${encodeURIComponent(options.logo_id)}`;
}

function onVideoEffectsChange(prefix) {
    const options = getVideoRenderOptions(prefix);
    const preview = document.getElementById(`${prefix}-logo-preview`);
    if (preview) preview.src = `/api/video/logo/${encodeURIComponent(options.logo_id)}`;
    try { localStorage.setItem('khmer_video_render_options', JSON.stringify(options)); } catch {}
    if (prefix === 'studio') markStudioDirty();
}

function videoApiError(data, fallback) {
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map(error => error.msg?.replace(/^Value error, /, '') || fallback).join('; ');
    return fallback;
}

async function uploadVideoLogo(prefix, file) {
    if (!file) return;
    const uploadInput = document.getElementById(`${prefix}-logo-file`);
    uploadInput.disabled = true;
    try {
        const form = new FormData();
        form.append('file', file);
        const response = await fetch('/api/video/logo', { method: 'POST', body: form });
        const data = await response.json();
        if (!response.ok) throw new Error(videoApiError(data, 'Logo upload failed'));
        const options = { ...getVideoRenderOptions(prefix), add_logo: true, logo_id: data.logo_id };
        applyVideoRenderOptions(prefix, options);
        onVideoEffectsChange(prefix);
        showToast('Logo ready: 40×40 px in the top-right corner.', 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        uploadInput.disabled = false;
    }
}

// ─────────────────────────────────────────────
// Voice Tuner Knobs (Rate, Pitch, Volume)
// ─────────────────────────────────────────────
function getVoiceTunerValues() {
    const pitchSlider = document.getElementById('voice-pitch-slider');
    const speedSlider = document.getElementById('voice-speed-slider');
    const volSlider = document.getElementById('voice-volume-slider');

    const pitchVal = parseInt(pitchSlider?.value || '0', 10);
    const speedVal = parseInt(speedSlider?.value || '10', 10);
    const volVal = parseInt(volSlider?.value || '5', 10);

    const rate = speedVal === 0 ? "+0%" : (speedVal > 0 ? `+${speedVal}%` : `${speedVal}%`);
    const pitch = pitchVal === 0 ? "+0Hz" : (pitchVal > 0 ? `+${pitchVal}Hz` : `${pitchVal}Hz`);
    const volume = volVal === 0 ? "+0%" : (volVal > 0 ? `+${volVal}%` : `${volVal}%`);

    return { rate, pitch, volume, speedVal, pitchVal, volVal };
}

function initVoiceTunerControls() {
    const toggleBtn = document.getElementById('btn-toggle-voice-tuner');
    const drawer = document.getElementById('voice-tuner-drawer');
    const pitchSlider = document.getElementById('voice-pitch-slider');
    const speedSlider = document.getElementById('voice-speed-slider');
    const volSlider = document.getElementById('voice-volume-slider');
    const resetBtn = document.getElementById('btn-reset-voice-tuner');
    const testTunedBtn = document.getElementById('btn-test-tuned-voice');

    if (toggleBtn && drawer) {
        toggleBtn.addEventListener('click', () => {
            const isHidden = drawer.style.display === 'none' || !drawer.style.display;
            drawer.style.display = isHidden ? 'block' : 'none';
            toggleBtn.classList.toggle('active', isHidden);
        });
    }

    if (pitchSlider) {
        pitchSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            const badge = document.getElementById('voice-pitch-val');
            if (badge) {
                if (val === 0) badge.textContent = "0 Hz (Natural)";
                else if (val > 0) badge.textContent = `+${val} Hz (Higher)`;
                else badge.textContent = `${val} Hz (Deeper)`;
            }
        });
    }

    if (speedSlider) {
        speedSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            const badge = document.getElementById('voice-speed-val');
            if (badge) {
                if (val === 0) badge.textContent = "0% (Natural)";
                else if (val > 0) badge.textContent = `+${val}% (Faster)`;
                else badge.textContent = `${val}% (Slower)`;
            }
        });
    }

    if (volSlider) {
        volSlider.addEventListener('input', (e) => {
            const val = parseInt(e.target.value, 10);
            const badge = document.getElementById('voice-volume-val');
            if (badge) {
                if (val === 0) badge.textContent = "+0% (Standard)";
                else if (val > 0) badge.textContent = `+${val}% (Clear Boost)`;
                else badge.textContent = `${val}% (Softer)`;
            }
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            if (pitchSlider) pitchSlider.value = 0;
            if (speedSlider) speedSlider.value = 10;
            if (volSlider) volSlider.value = 5;
            document.getElementById('voice-pitch-val').textContent = "0 Hz (Natural)";
            document.getElementById('voice-speed-val').textContent = "+10% (Fluent / Conversational)";
            document.getElementById('voice-volume-val').textContent = "+5% (Clear Boost)";
            showToast("Voice adjustments reset to natural studio defaults", "info");
        });
    }

    if (testTunedBtn) {
        testTunedBtn.addEventListener('click', handleVoicePreview);
    }
}

function initVideoDubberEvents() {
    const dubUrlBtn = document.getElementById('start-url-dub-btn');
    const dubUploadBtn = document.getElementById('start-file-dub-btn');
    const previewVoiceBtn = document.getElementById('preview-voice-btn');
    const videoDropzone = document.getElementById('video-file-dropzone');
    const videoFileInput = document.getElementById('video-file-input');
    const subFileInput = document.getElementById('video-sub-file-input');

    if (dubUrlBtn) {
        dubUrlBtn.addEventListener('click', handleUrlDubbing);
    }

    if (previewVoiceBtn) {
        previewVoiceBtn.addEventListener('click', handleVoicePreview);
    }

    if (videoDropzone && videoFileInput) {
        videoDropzone.addEventListener('click', (event) => {
            if (event.target !== videoFileInput && !isSubmittingVideo) videoFileInput.click();
        });
        videoFileInput.addEventListener('change', (e) => {
            selectVideoFiles(e.target.files);
        });
        videoDropzone.addEventListener('dragover', (event) => event.preventDefault());
        videoDropzone.addEventListener('drop', (event) => {
            event.preventDefault();
            if (!isSubmittingVideo) selectVideoFiles(event.dataTransfer.files);
        });
    }

    if (subFileInput) {
        subFileInput.addEventListener('change', (e) => {
            selectedVideoSubtitles = Array.from(e.target.files);
            const subLabel = document.getElementById('selected-sub-filename');
            if (e.target.files.length > 0 && subLabel) {
                subLabel.textContent = `Attached subtitles: ${Array.from(e.target.files).map(file => file.name).join(', ')}`;
                subLabel.style.display = 'block';
            }
        });
    }

    if (dubUploadBtn) {
        dubUploadBtn.addEventListener('click', handleFileDubbing);
    }
}

// Preview Khmer Neural Voice with Active Tuner Settings
async function handleVoicePreview() {
    const voiceSelect = document.getElementById('video-voice-select').value;
    const previewBtn = document.getElementById('preview-voice-btn');
    const testTunedBtn = document.getElementById('btn-test-tuned-voice');
    const activeBtn = testTunedBtn || previewBtn;
    const originalHtml = activeBtn ? activeBtn.innerHTML : '';

    if (activeBtn) {
        activeBtn.disabled = true;
        activeBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Generating...';
    }

    const tuner = getVoiceTunerValues();

    try {
        const resp = await fetch('/api/video/preview-voice', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: "សួស្តីបងប្អូនទាំងអស់គ្នា សូមស្វាគមន៍មកកាន់វីដេអូបកប្រែជាភាសាខ្មែរ",
                voice_type: voiceSelect,
                rate: tuner.rate,
                pitch: tuner.pitch,
                volume: tuner.volume,
                enhance: true
            })
        });

        if (!resp.ok) throw new Error("Failed to generate voice preview");
        const data = await resp.json();

        const audio = new Audio(data.audio_url);
        audio.play();
        const voiceName = voiceSelect === 'male' ? 'Piseth (Male)' : 'Sreymom (Female)';
        showToast(`Playing sample: ${voiceName} [Rate: ${tuner.rate}, Pitch: ${tuner.pitch}, Vol: ${tuner.volume}]`, 'success');
    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        if (activeBtn) {
            activeBtn.disabled = false;
            activeBtn.innerHTML = originalHtml;
        }
    }
}

// Client-side video platform detector
function detectPlatformClient(url) {
    const lower = (url || '').toLowerCase().trim();
    if (lower.includes('youtube.com') || lower.includes('youtu.be')) return 'YouTube';
    if (lower.includes('bilibili.com') || lower.includes('b23.tv') || /^bv[a-za-z0-9]{10}$/i.test(url.trim())) return 'Bilibili';
    if (lower.includes('tiktok.com')) return 'TikTok';
    if (lower.includes('douyin.com')) return 'Douyin';
    if (lower.includes('facebook.com') || lower.includes('fb.watch')) return 'Facebook';
    if (lower.includes('instagram.com')) return 'Instagram';
    if (lower.includes('twitter.com') || lower.includes('x.com')) return 'X (Twitter)';
    if (/\.(mp4|mov|webm|m3u8|mkv)($|\?)/i.test(lower)) return 'Direct Video';
    return 'Web Video';
}

// Start Dubbing from YouTube, Bilibili or Web URL
async function handleUrlDubbing(forceNew = false) {
    if (isSubmittingVideo) return;
    const urlInput = document.getElementById('video-url-input');
    const url = urlInput.value.trim();

    if (!url) {
        showToast("Please enter a video URL (YouTube, Bilibili, TikTok, Facebook, etc.)!", "error");
        return;
    }

    const platform = detectPlatformClient(url);
    const voiceType = document.getElementById('video-voice-select').value;
    const tone = document.getElementById('video-tone-select').value;
    const audioMode = document.getElementById('video-audio-mode').value;
    const maxDur = parseInt(document.getElementById('video-duration-select')?.value ?? '0', 10);
    const tuner = getVoiceTunerValues();

    setVideoSubmissionBusy(true);

    try {
        const resp = await fetch('/api/video/dub-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: url,
                force_new: forceNew === true,
                voice_type: voiceType,
                tone: tone,
                audio_mode: audioMode,
                ...getVideoRenderOptions('video'),
                max_duration: maxDur,
                ...getTranslationOptions(),
                voice_rate: tuner.rate,
                voice_pitch: tuner.pitch,
                voice_volume: tuner.volume
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Request failed" }));
            if (resp.status === 409 && err.detail?.job_id) {
                showDuplicateVideoJob(err.detail, 'url');
                return;
            }
            throw new Error(videoApiError(err, "Failed to start dubbing job"));
        }

        const data = await resp.json();
        if (data.reused && data.existing_job) {
            showDuplicateVideoJob(data.existing_job, 'url');
            return;
        }
        activeVideoJobId = data.job_id;
        setPipelineUIState('active');
        startPollingJobStatus(activeVideoJobId);
        showToast(`🎉 [${platform}] Dubbing job queued! Downloading and translating...`, "success");
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        setVideoSubmissionBusy(false);
    }
}

// Each selected video uses the existing upload API, with bounded parallel uploads.
function videoSubtitleAssignments(files, subtitles) {
    const stem = name => name.replace(/\.[^.]+$/, '').toLowerCase();
    if (files.length === 1 && subtitles.length <= 1) return [subtitles[0] || null];
    const assignments = files.map(file => {
        const matches = subtitles.filter(subtitle => stem(subtitle.name) === stem(file.name));
        if (matches.length > 1) throw new Error(`Choose only one subtitle for ${file.name}.`);
        return matches[0] || null;
    });
    for (const subtitle of subtitles) {
        const count = assignments.filter(assigned => assigned === subtitle).length;
        if (count !== 1) throw new Error(`Subtitle ${subtitle.name} must match exactly one video filename.`);
    }
    return assignments;
}

async function handleFileDubbing(forceNew = false, retryFiles = null) {
    if (isSubmittingVideo) return;
    const fileInput = document.getElementById('video-file-input');
    const files = retryFiles || selectedVideoFiles || Array.from(fileInput.files || []);
    if (!files.length) {
        showToast('Please select video files first', 'error');
        return;
    }
    const subtitles = selectedVideoSubtitles || Array.from(document.getElementById('video-sub-file-input')?.files || []);
    let assignments;
    try {
        assignments = videoSubtitleAssignments(files, subtitles);
    } catch (error) {
        showToast(error.message, 'error');
        return;
    }
    const tuner = getVoiceTunerValues();
    const settings = {
        force_new: String(forceNew === true),
        voice_type: document.getElementById('video-voice-select').value,
        tone: document.getElementById('video-tone-select').value,
        audio_mode: document.getElementById('video-audio-mode').value,
        max_duration: parseInt(document.getElementById('video-duration-select')?.value ?? '0', 10),
        voice_rate: tuner.rate, voice_pitch: tuner.pitch, voice_volume: tuner.volume,
        ...getVideoRenderOptions('video'), ...getTranslationOptions()
    };
    const status = document.getElementById('video-upload-status');
    let nextIndex = 0, finished = 0, accepted = 0, reused = 0;
    let firstJobId = null, singleDuplicate = false;
    const failures = [];
    setVideoSubmissionBusy(true);
    if (status) status.textContent = `Uploading ${files.length} video(s)...`;

    async function uploadWorker() {
        while (nextIndex < files.length) {
            const index = nextIndex++;
            const file = files[index];
            const formData = new FormData();
            formData.append('file', file);
            if (assignments[index]) formData.append('subtitle_file', assignments[index]);
            for (const [key, value] of Object.entries(settings)) {
                if (value !== null && value !== undefined) formData.append(key, value);
            }
            try {
                const response = await fetch('/api/video/dub-upload', { method: 'POST', body: formData });
                const data = await response.json();
                const duplicate = response.status === 409 ? data.detail : data.reused ? data.existing_job : null;
                if (duplicate?.job_id) {
                    reused++;
                    if (files.length === 1) {
                        singleDuplicate = true;
                        showDuplicateVideoJob(duplicate, 'file', file);
                    } else {
                        firstJobId ||= duplicate.job_id;
                        videoJobSummaries.set(duplicate.job_id, { ...duplicate, id: duplicate.job_id, title: file.name });
                    }
                } else {
                    if (!response.ok) throw new Error(videoApiError(data, 'Upload failed'));
                    accepted++;
                    firstJobId ||= data.job_id;
                    videoJobSummaries.set(data.job_id, {
                        id: data.job_id, title: file.name, status: data.status || 'queued', progress: 0, stage: 'Queued'
                    });
                }
                renderVideoJobs();
            } catch (error) {
                failures.push({ file, error: error.message });
            } finally {
                finished++;
                if (status) status.textContent = `${finished}/${files.length} uploads checked · ${accepted} added · ${reused} saved jobs · ${failures.length} failed`;
            }
        }
    }

    try {
        await Promise.all(Array.from({ length: Math.min(3, files.length) }, () => uploadWorker()));
        if (firstJobId) {
            activeVideoJobId = firstJobId;
            setPipelineUIState('active');
            startPollingJobStatus(firstJobId);
        }
        if (!singleDuplicate) {
            selectVideoFiles(failures.map(failure => failure.file));
            fileInput.value = '';
            // Retain only sidecars for files that need retrying.
            selectedVideoSubtitles = failures.length
                ? failures.map(failure => assignments[files.indexOf(failure.file)]).filter(Boolean) : [];
            const subInput = document.getElementById('video-sub-file-input');
            if (subInput) subInput.value = '';
            const subLabel = document.getElementById('selected-sub-filename');
            if (subLabel) {
                subLabel.textContent = selectedVideoSubtitles.map(file => file.name).join(', ');
                subLabel.style.display = selectedVideoSubtitles.length ? 'block' : 'none';
            }
        }
        if (failures.length) {
            if (status) status.textContent += '. Failed: ' + failures.map(({file, error}) => `${file.name}: ${error}`).join('; ') + '. Click Add Videos to Queue to retry these files.';
            showToast(`${failures.length} upload(s) failed. Other jobs continue processing.`, 'error');
        } else if (accepted) {
            showToast(`${accepted} video(s) added to the queue. Track each one in Video Jobs.`, 'success');
        } else if (reused > 1) {
            showToast(`${reused} saved jobs found. Open or resume them in Video Jobs.`, 'info');
        }
    } finally {
        setVideoSubmissionBusy(false);
    }
}

// Each tracker owns its requests; stale replies cannot replace another job's progress.
let trackingGeneration = 0;

function startPollingJobStatus(jobId) {
    if (pollInterval) clearInterval(pollInterval);
    const generation = ++trackingGeneration;
    activeVideoJobId = jobId;
    localStorage.setItem('khmer_active_video_job', jobId);
    let busy = false;
    const poll = async () => {
        if (busy || generation !== trackingGeneration) return;
        busy = true;
        try {
            const resp = await fetch(`/api/video/status/${jobId}?summary=true`);
            if (resp.status === 404 && generation === trackingGeneration) {
                forgetVideoTask(jobId);
                return;
            }
            if (!resp.ok) return;
            let job = await resp.json();
            if (generation !== trackingGeneration) return;
            if (job.status === 'completed' || job.status === 'failed') {
                const fullResponse = await fetch(`/api/video/status/${jobId}`);
                if (!fullResponse.ok) return;
                job = await fullResponse.json();
                if (generation !== trackingGeneration) return;
            }
            updatePipelineProgress(job);
            if (job.status === 'completed') {
                clearInterval(pollInterval);
                displayCompletedVideo(job);
                showToast(job.fallback_count ? `Video ready. ${job.fallback_count} cues use original speech after retries; review the flagged cues.` : "Video dubbing complete! Ready to watch & download.", job.fallback_count ? "warning" : "success");
            } else if (job.status === 'failed') {
                clearInterval(pollInterval);
                if (job.segments?.length) displayCompletedVideo(job);
                showToast(`Dubbing failed: ${job.error || "Unknown error"}`, "error");
            } else if (['interrupted', 'paused'].includes(job.status)) {
                clearInterval(pollInterval);
                document.getElementById('pipeline-resume-btn').style.display = '';
                showToast(job.status === 'paused' ? 'Paused. Resume saved work when ready.' :
                    'Processing was interrupted. Resume saved work to continue this job.', 'info');
            }
        } catch (error) {
            console.error("Polling error:", error);
        } finally {
            busy = false;
        }
    };
    pollInterval = setInterval(poll, 1500);
    poll();
}

// Update Visual Pipeline Stages
function updatePipelineProgress(job) {
    if (job.id) savedPipelineProgress.set(job.id, job);
    const stageText = document.getElementById('pipeline-stage-text');
    const percentText = document.getElementById('pipeline-percent-text');
    const progressBar = document.getElementById('pipeline-progress-fill');
    const jobTitle = document.getElementById('pipeline-job-title');
    const jobLabel = document.getElementById('pipeline-job-id');
    if (jobLabel) jobLabel.textContent = job.id ? `Job ${job.id}` : '';
    const resumeButton = document.getElementById('pipeline-resume-btn');
    if (resumeButton) resumeButton.style.display = ['interrupted', 'failed', 'paused'].includes(job.status) ? '' : 'none';
    const pauseButton = document.getElementById('pipeline-pause-btn');
    if (pauseButton) {
        pauseButton.style.display = ['queued', 'processing', 'running', 'rendering', 'pausing'].includes(job.status) ? '' : 'none';
        pauseButton.textContent = job.status === 'pausing' ? 'Pausing…' : 'Pause';
        pauseButton.disabled = job.status === 'pausing' || changingVideoJobs.has(job.id);
    }

    if (stageText) stageText.textContent = job.stage || "Processing...";
    if (percentText) percentText.textContent = `${job.progress}%`;
    if (progressBar) progressBar.style.width = `${job.progress}%`;
    if (jobTitle && job.title) jobTitle.textContent = job.title;

    // Stepper icons highlight
    const steps = [
        { id: 'step-dl', threshold: 10 },
        { id: 'step-stt', threshold: 30 },
        { id: 'step-trans', threshold: 55 },
        { id: 'step-tts', threshold: 75 },
        { id: 'step-mix', threshold: 90 },
    ];

    steps.forEach((s, index) => {
        const el = document.getElementById(s.id);
        if (el) {
            el.classList.remove('completed', 'active');
            const key = ['prepare', 'transcription', 'translation', 'voices', 'render'][index];
            const checkpoint = job.steps?.[index] || (job.step_states?.[key] ? { state: job.step_states[key] } : null);
            if (checkpoint) {
                if (checkpoint.state === 'complete') el.classList.add('completed');
                else if (checkpoint.state === 'partial') el.classList.add('active');
                return;
            }
            if (job.progress >= s.threshold) {
                el.classList.add('completed');
                el.classList.remove('active');
            } else if (job.progress >= s.threshold - 20) {
                el.classList.add('active');
            } else {
                el.classList.remove('completed', 'active');
            }
        }
    });
}

// =========================================================================
// CAPCUT-STYLE KHMER DUBBING STUDIO ENGINE
// =========================================================================

let studioJobId = null;
let studioSegments = [];
let studioDirty = false;
let isRepairingMissing = false;
let studioVoiceTunerSnapshot = null;
let studioAudioSnapshot = '';
let studioBackgroundSnapshot = '';
let activeCueIndex = -1;

function studioAudioFingerprint() {
    return JSON.stringify(studioSegments.map(seg => ({
        id: seg.id, start: seg.start, end: seg.end, translation: seg.translation,
        voice_type: seg.voice_type, rate: seg.rate, pitch: seg.pitch, volume: seg.volume
    })));
}

function displayCompletedVideo(job) {
    const resultsCard = document.getElementById('video-results-card');
    const videoPlayer = document.getElementById('dubbed-video-player');
    const downloadVideoBtn = document.getElementById('download-dubbed-video-btn');
    const downloadSrtBtn = document.getElementById('download-dubbed-srt-btn');

    if (!job || (!job.result && !job.segments?.length)) return;

    currentDownloadJob = job;
    studioJobId = job.id;
    studioSegments = JSON.parse(JSON.stringify(job.segments || []));
    studioVoiceTunerSnapshot = getVoiceTunerValues();
    applyVideoRenderOptions('studio', job.render_options || job.result?.render_options || {});

    // Ensure every segment has voice_type
    studioSegments.forEach(s => {
        if (!s.voice_type) s.voice_type = job.voice_type || "male";
    });
    studioAudioSnapshot = studioAudioFingerprint();
    studioBackgroundSnapshot = document.getElementById('studio-bg-volume')?.value || '';

    videoPlayer.style.display = job.result ? '' : 'none';
    if (job.result) videoPlayer.src = job.result.download_url;
    else videoPlayer.removeAttribute('src');
    if (downloadVideoBtn) {
        downloadVideoBtn.disabled = !job.result;
        downloadVideoBtn.onclick = (e) => openDownloadModal('video', e);
    }
    if (downloadSrtBtn) {
        downloadSrtBtn.disabled = !job.result;
        downloadSrtBtn.onclick = (e) => openDownloadModal('srt', e);
    }

    // Attach video player time listener for subtitle sync
    setupVideoSyncListener(videoPlayer);

    // Render interactive CapCut dialogue cue cards
    renderStudioCueCards();

    resultsCard.style.display = 'block';
    resultsCard.scrollIntoView({ behavior: 'smooth' });
}

function setupVideoSyncListener(videoPlayer) {
    videoPlayer.ontimeupdate = () => {
        const currentTime = videoPlayer.currentTime;
        updateActiveSubtitleDisplay(currentTime);
    };
}

function updateActiveSubtitleDisplay(currentTime) {
    const subTimeEl = document.getElementById('studio-sub-time');
    const subTextEl = document.getElementById('studio-sub-text');

    const activeSegIndex = studioSegments.findIndex(s => currentTime >= s.start && currentTime <= s.end);

    if (activeSegIndex !== -1) {
        const seg = studioSegments[activeSegIndex];
        if (subTimeEl) subTimeEl.textContent = `${formatSeconds(seg.start)} - ${formatSeconds(seg.end)}`;
        if (subTextEl) subTextEl.textContent = seg.translation || seg.text || "";

        if (activeSegIndex !== activeCueIndex) {
            highlightActiveCueCard(activeSegIndex);
            activeCueIndex = activeSegIndex;
        }
    } else {
        if (subTimeEl) subTimeEl.textContent = formatSeconds(currentTime);
        if (subTextEl) subTextEl.textContent = "(គ្មានសំឡេង / Silence)";
        clearActiveCueHighlight();
        activeCueIndex = -1;
    }
}

function highlightActiveCueCard(index) {
    document.querySelectorAll('.studio-cue-card').forEach((card, idx) => {
        if (idx === index) {
            card.classList.add('active');
            card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
            card.classList.remove('active');
        }
    });
}

function clearActiveCueHighlight() {
    document.querySelectorAll('.studio-cue-card.active').forEach(c => c.classList.remove('active'));
}

// Render CapCut Dialogue Cards with Overlap Detection
function renderStudioCueCards() {
    const container = document.getElementById('studio-cue-cards-list');
    const countEl = document.getElementById('studio-cues-count');
    const statusEl = document.getElementById('studio-overlap-status');

    if (!container) return;
    container.innerHTML = '';

    let overlapCount = 0;
    const missingCount = studioSegments.filter(seg => (seg.text || '').trim() && !(seg.translation || '').trim()).length;
    const repairBtn = document.getElementById('btn-translate-missing');
    if (repairBtn) repairBtn.disabled = isRepairingMissing || missingCount === 0;
    const filterText = (document.getElementById('studio-search-input')?.value || '').toLowerCase().trim();

    studioSegments.forEach((seg, idx) => {
        const prevSeg = idx > 0 ? studioSegments[idx - 1] : null;
        const isOverlap = prevSeg && (seg.start < prevSeg.end - 0.05);

        if (isOverlap) overlapCount++;

        // Filter search
        if (filterText) {
            const matchOrig = (seg.text || '').toLowerCase().includes(filterText);
            const matchKhmer = (seg.translation || '').toLowerCase().includes(filterText);
            if (!matchOrig && !matchKhmer) return;
        }

        const duration = Math.max(0.1, (seg.end - seg.start)).toFixed(2);
        const card = document.createElement('div');
        card.className = `studio-cue-card ${isOverlap ? 'has-overlap' : ''}`;
        card.id = `cue-card-${idx}`;

        card.innerHTML = `
            <div class="cue-card-header">
                <div class="cue-card-identity">
                    <span class="cue-index-badge">#${idx + 1}</span>
                    <div class="cue-timing-controls">
                        <div class="time-nudge-wrap">
                            <button type="button" class="btn-nudge" onclick="studioNudgeTime(${idx}, 'start', -0.1)" title="-0.1s">-</button>
                            <input type="text" class="time-input-styled" value="${seg.start.toFixed(2)}" onchange="studioSetTime(${idx}, 'start', this.value)">
                            <button type="button" class="btn-nudge" onclick="studioNudgeTime(${idx}, 'start', 0.1)" title="+0.1s">+</button>
                        </div>
                        <span class="cue-arrow">→</span>
                        <div class="time-nudge-wrap">
                            <button type="button" class="btn-nudge" onclick="studioNudgeTime(${idx}, 'end', -0.1)" title="-0.1s">-</button>
                            <input type="text" class="time-input-styled" value="${seg.end.toFixed(2)}" onchange="studioSetTime(${idx}, 'end', this.value)">
                            <button type="button" class="btn-nudge" onclick="studioNudgeTime(${idx}, 'end', 0.1)" title="+0.1s">+</button>
                        </div>
                        <span class="cue-dur-tag">${duration}s</span>
                    </div>
                </div>

                <div class="cue-card-top-actions">
                    ${isOverlap ? `
                        <span class="cue-overlap-warning" onclick="studioShiftFixCue(${idx})" title="Click to shift past previous speech cue">
                            <i class="ph ph-warning"></i> Overlap ${(prevSeg.end - seg.start).toFixed(2)}s (Fix)
                        </span>
                    ` : ''}
                    ${idx < studioSegments.length - 1 ? `
                        <button type="button" class="btn-icon-xs" onclick="studioMergeWithNext(${idx})" title="Merge with next line (CapCut style)">
                            <i class="ph ph-intersect"></i> Merge
                        </button>
                    ` : ''}
                    <button type="button" class="btn-icon-xs btn-icon-danger" onclick="studioDeleteCue(${idx})" title="Delete this cue">
                        <i class="ph ph-trash"></i>
                    </button>
                </div>
            </div>

            <div class="cue-card-body">
                <div class="cue-orig-text" title="Source Dialogue (Subtitle OCR / Speech Recognition)">
                    <div class="cue-source-row">
                        <span>🇨🇳 ${escapeHtml(seg.text || '')}</span>
                        ${seg.ocr_detected ? '<span class="cue-ocr-badge"><i class="ph ph-subtitles"></i> Subtitle OCR</span>' : '<span class="cue-ocr-badge" style="color: #a855f7; background: rgba(168,85,247,0.12); border-color: rgba(168,85,247,0.25);"><i class="ph ph-microphone"></i> Audio</span>'}
                    </div>
                    ${seg.asr_text && seg.asr_text !== seg.text ? `
                        <div class="cue-asr-hint">Audio speech heard: "${escapeHtml(seg.asr_text)}"</div>
                    ` : ''}
                </div>
                ${seg.english_intent ? `
                    <div class="cue-english-intent" title="Optional English reference. Direct Khmer translations use the original dialogue.">
                        <span class="badge-intent-tag">🇬🇧 English reference:</span>
                        <span>${escapeHtml(seg.english_intent)}</span>
                    </div>
                ` : ''}
                <div class="cue-khmer-wrap">
                    ${seg.translation_error || ((seg.text || '').trim() && !(seg.translation || '').trim()) ? `
                        <div class="cue-translation-warning" role="status">
                            <i class="ph ph-warning-circle"></i>
                            ${escapeHtml(seg.translation_error || 'Khmer translation is missing. Use Translate Missing to retry.')}
                        </div>
                    ` : ''}
                    ${seg.translation_warning ? `<div class="cue-translation-warning" role="status"><i class="ph ph-info"></i> ${escapeHtml(seg.translation_warning)}</div>` : ''}
                    ${seg.source_warning ? `<div class="cue-translation-warning" role="status"><i class="ph ph-warning-circle"></i> ${escapeHtml(seg.source_warning)}</div>` : ''}
                    ${seg.generation_warning ? `<div class="cue-translation-warning" role="status"><i class="ph ph-warning-circle"></i> ${escapeHtml(seg.generation_warning)}</div>` : ''}
                    <textarea 
                        class="cue-khmer-textarea" 
                        placeholder="Enter or edit Khmer translation..." 
                        oninput="onCueTextChange(${idx}, this.value)"
                    >${escapeHtml(seg.translation || '')}</textarea>
                </div>
            </div>

            <div class="cue-card-footer">
                <div class="cue-footer-left">
                    <select class="cue-voice-picker" onchange="onCueVoiceChange(${idx}, this.value)">
                        <option value="male" ${seg.voice_type === 'male' ? 'selected' : ''}>👨 Piseth (Male)</option>
                        <option value="female" ${seg.voice_type === 'female' ? 'selected' : ''}>👩 Sreymom (Female)</option>
                    </select>
                </div>
                <div class="cue-footer-right">
                    <button type="button" class="btn-preview-voice" id="btn-preview-${idx}" onclick="studioPreviewCueVoice(${idx})" title="Preview this Khmer voice audio">
                        <i class="ph ph-headphones"></i> Preview Audio
                    </button>
                    <button type="button" class="btn-jump-video" onclick="studioJumpToCue(${idx})" title="Jump video to this timestamp">
                        <i class="ph ph-video"></i> Jump
                    </button>
                </div>
            </div>
        `;

        container.appendChild(card);
    });

    if (countEl) countEl.textContent = `${studioSegments.length} Cues${missingCount ? ` · ${missingCount} untranslated` : ''}`;
    if (statusEl) {
        if (overlapCount > 0) {
            statusEl.className = 'badge-status has-collision';
            statusEl.innerHTML = `<i class="ph ph-warning-circle"></i> ${overlapCount} Overlaps`;
        } else {
            statusEl.className = 'badge-status';
            statusEl.innerHTML = `<i class="ph ph-check-circle" style="color: var(--accent-emerald);"></i> Zero Collisions`;
        }
    }
}

// Cue Text Editing
function onCueTextChange(index, val) {
    if (!studioSegments[index]) return;
    studioSegments[index].translation = val;
    studioSegments[index]._dirty = true;
    markStudioDirty();
}

// Cue Voice Selection (Male / Female character voice)
function onCueVoiceChange(index, val) {
    if (!studioSegments[index]) return;
    studioSegments[index].voice_type = val;
    studioSegments[index]._dirty = true;
    markStudioDirty();
    showToast(`Segment #${index + 1} voice set to ${val === 'male' ? 'Piseth (Male)' : 'Sreymom (Female)'}`, "success");
}

// Time Adjustments
function syncCueTiming(seg) {
    seg.end = Math.max(seg.start + 0.1, seg.end);
    seg.target_start = seg.start;
    seg.target_end = seg.end;
}

function studioNudgeTime(index, field, delta) {
    if (!studioSegments[index]) return;
    studioSegments[index][field] = Math.max(0, parseFloat((studioSegments[index][field] + delta).toFixed(2)));
    if (field === 'start' && studioSegments[index].start >= studioSegments[index].end) {
        studioSegments[index].end = studioSegments[index].start + 0.5;
    }
    syncCueTiming(studioSegments[index]);
    markStudioDirty();
    renderStudioCueCards();
}

function studioSetTime(index, field, val) {
    const num = parseFloat(val);
    if (!isNaN(num) && studioSegments[index]) {
        studioSegments[index][field] = Math.max(0, parseFloat(num.toFixed(2)));
        syncCueTiming(studioSegments[index]);
        markStudioDirty();
        renderStudioCueCards();
    }
}

function studioShiftFixCue(index) {
    if (index <= 0 || !studioSegments[index]) return;
    const prev = studioSegments[index - 1];
    const dur = Math.max(0.5, studioSegments[index].end - studioSegments[index].start);
    studioSegments[index].start = parseFloat((prev.end + 0.08).toFixed(2));
    studioSegments[index].end = parseFloat((studioSegments[index].start + dur).toFixed(2));
    syncCueTiming(studioSegments[index]);
    markStudioDirty();
    renderStudioCueCards();
    showToast(`Shifted cue #${index + 1} to start at ${studioSegments[index].start}s`, "success");
}

// Preview Single Khmer Voice Line via Edge-TTS
async function studioPreviewCueVoice(index) {
    const jobId = studioJobId;
    const generation = trackingGeneration;
    const seg = studioSegments[index];
    if (!seg || !seg.translation.trim()) {
        showToast("Cannot preview empty translation", "error");
        return;
    }

    const btn = document.getElementById(`btn-preview-${index}`);
    const originalHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Synthesizing...`;
        btn.disabled = true;
    }

    try {
        const tuner = getVoiceTunerValues();
        const resp = await fetch(`/api/video/job/${jobId}/preview-segment`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                segment_id: seg.id || (index + 1),
                text: seg.translation,
                voice_type: seg.voice_type || "male",
                rate: seg.rate || tuner.rate,
                pitch: seg.pitch || tuner.pitch,
                volume: seg.volume || tuner.volume,
                enhance: true
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Preview failed" }));
            throw new Error(videoApiError(err, "Voice preview failed"));
        }

        const data = await resp.json();
        if (studioJobId !== jobId || generation !== trackingGeneration) return;
        const player = document.getElementById('segment-preview-player');
        if (player) {
            player.src = data.audio_url;
            player.play();
            if (btn) btn.classList.add('playing');
            player.onended = () => {
                if (btn) btn.classList.remove('playing');
            };
        }
    } catch (e) {
        showToast("Preview error: " + e.message, "error");
    } finally {
        if (btn) {
            btn.innerHTML = originalHtml;
            btn.disabled = false;
        }
    }
}

// Seek Video to Cue
function studioJumpToCue(index) {
    const seg = studioSegments[index];
    const player = document.getElementById('dubbed-video-player');
    if (seg && player) {
        player.currentTime = seg.start;
        player.play();
        highlightActiveCueCard(index);
    }
}

// Merge Two Adjacent Dialogue Cues
function studioMergeWithNext(index) {
    if (index >= studioSegments.length - 1) return;
    const seg1 = studioSegments[index];
    const seg2 = studioSegments[index + 1];

    seg1.end = seg2.end;
    syncCueTiming(seg1);
    seg1.text = `${seg1.text} ${seg2.text}`.trim();
    seg1.translation = `${seg1.translation} ${seg2.translation}`.trim();
    seg1._dirty = true;

    studioSegments.splice(index + 1, 1);
    markStudioDirty();
    renderStudioCueCards();
    showToast(`Merged cues #${index + 1} and #${index + 2}`, "success");
}

// Delete Cue
function studioDeleteCue(index) {
    if (!confirm(`Delete dialogue cue #${index + 1}?`)) return;
    studioSegments.splice(index, 1);
    markStudioDirty();
    renderStudioCueCards();
    showToast(`Deleted cue #${index + 1}`, "success");
}

// Add New Cue at Current Playhead
function studioAddNewCue() {
    const player = document.getElementById('dubbed-video-player');
    const curTime = player ? parseFloat(player.currentTime.toFixed(2)) : 0.0;

    const newCue = {
        id: Date.now(),
        start: curTime,
        end: parseFloat((curTime + 2.0).toFixed(2)),
        text: "New dialogue cue",
        translation: "សំឡេងថ្មី",
        voice_type: "male",
        _dirty: true
    };

    syncCueTiming(newCue);

    // Insert sorted by start time
    const insertIdx = studioSegments.findIndex(s => s.start > curTime);
    if (insertIdx === -1) {
        studioSegments.push(newCue);
    } else {
        studioSegments.splice(insertIdx, 0, newCue);
    }

    markStudioDirty();
    renderStudioCueCards();
    showToast(`Added new cue at ${curTime}s`, "success");
}

// Auto-Fix All Overlaps via Non-Collision Algorithm
async function studioAutoFixOverlaps() {
    if (!studioJobId) return;
    const jobId = studioJobId;
    const generation = trackingGeneration;

    const btn = document.getElementById('btn-auto-fix-overlaps');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Fixing...`;
        btn.disabled = true;
    }

    try {
        // Save current segments first
        const saved = await fetch(`/api/video/job/${jobId}/segments`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ segments: studioSegments })
        });
        if (!saved.ok) throw new Error('Could not save cue changes');

        const resp = await fetch(`/api/video/job/${jobId}/auto-fix-overlaps`, {
            method: 'POST'
        });

        if (!resp.ok) throw new Error("Failed to auto-fix overlaps");

        const data = await resp.json();
        if (studioJobId !== jobId || generation !== trackingGeneration) return;
        studioSegments = data.segments;
        renderStudioCueCards();
        showToast("⚡ All speech overlaps resolved! Zero collisions guaranteed.", "success");
    } catch (e) {
        showToast("Error: " + e.message, "error");
    } finally {
        if (btn) {
            btn.innerHTML = origHtml;
            btn.disabled = false;
        }
    }
}

// Re-render Video with All Studio Edits
async function studioRerenderVideo() {
    if (!studioJobId) return;
    const jobId = studioJobId;
    const generation = trackingGeneration;

    const btn = document.getElementById('btn-studio-rerender');
    const origHtml = btn.innerHTML;
    btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Re-rendering Video...`;
    btn.disabled = true;

    const bgVol = parseFloat(document.getElementById('studio-bg-volume').value || "0.0");
    let audioMode = "replace";
    if (bgVol <= 0.001 && bgVol >= 0) {
        audioMode = "replace";
    } else if (bgVol < 0) {
        audioMode = "smart_sidechain";
    } else if (bgVol > 0 && bgVol <= 0.06) {
        audioMode = "duck_subtle";
    } else if (bgVol > 0.06) {
        audioMode = "ducking";
    }

    try {
        const tuner = getVoiceTunerValues();
        const recovery = currentDownloadJob?.status === 'failed';
        const visualOnly = !recovery && currentDownloadJob?.result?.dubbed_video_path &&
            !currentDownloadJob.needs_rerender && studioAudioFingerprint() === studioAudioSnapshot &&
            studioVoiceTunerSnapshot?.rate === tuner.rate && studioVoiceTunerSnapshot?.pitch === tuner.pitch &&
            studioVoiceTunerSnapshot?.volume === tuner.volume &&
            (document.getElementById('studio-bg-volume')?.value || '') === studioBackgroundSnapshot;
        const action = visualOnly ? 'visual-update' : 'resume';
        const resp = await fetch(`/api/video/job/${jobId}/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(visualOnly ? getVideoRenderOptions('studio') : {
                ...getTranslationOptions(),
                segments: studioSegments,
                audio_mode: audioMode,
                bg_volume: bgVol,
                ...getVideoRenderOptions('studio'),
                voice_rate: studioVoiceTunerSnapshot?.rate === tuner.rate ? 'auto' : tuner.rate,
                voice_pitch: studioVoiceTunerSnapshot?.pitch === tuner.pitch ? 'auto' : tuner.pitch,
                voice_volume: studioVoiceTunerSnapshot?.volume === tuner.volume ? 'auto' : tuner.volume
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Re-render failed" }));
            throw new Error(videoApiError(err, "Re-render failed"));
        }

        await resp.json();
        if (studioJobId === jobId && generation === trackingGeneration) {
            activeVideoJobId = jobId;
            setPipelineUIState('active');
            startPollingJobStatus(jobId);
        }
        showToast(visualOnly ? 'Applying visual effects while preserving your finished audio.' :
            'Continuing saved dialogue with automatic retries for failed cues.', 'success');
    } catch (e) {
        showToast("Re-render error: " + e.message, "error");
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
}

// Video Transport Controls
function studioTogglePlay() {
    const p = document.getElementById('dubbed-video-player');
    if (!p) return;
    if (p.paused) p.play();
    else p.pause();
}

function studioJumpPrevCue() {
    const p = document.getElementById('dubbed-video-player');
    if (!p) return;
    const cur = p.currentTime;
    for (let i = studioSegments.length - 1; i >= 0; i--) {
        if (studioSegments[i].start < cur - 0.5) {
            studioJumpToCue(i);
            return;
        }
    }
}

function studioJumpNextCue() {
    const p = document.getElementById('dubbed-video-player');
    if (!p) return;
    const cur = p.currentTime;
    const nextIdx = studioSegments.findIndex(s => s.start > cur + 0.2);
    if (nextIdx !== -1) studioJumpToCue(nextIdx);
}

function studioFilterCues() {
    renderStudioCueCards();
}

function studioAlternateSpeakers() {
    if (!studioSegments || studioSegments.length === 0) return;
    studioSegments.forEach((seg, idx) => {
        seg.voice_type = (idx % 2 === 0) ? "male" : "female";
        seg._dirty = true;
    });
    markStudioDirty();
    renderStudioCueCards();
    showToast("✨ Auto-assigned: Piseth (👨) for even lines & Sreymom (👩) for odd lines!", "success");
}

async function studioPolishDialogue() {
    if (!studioJobId) return;
    const jobId = studioJobId;
    const generation = trackingGeneration;
    const btn = document.getElementById('btn-polish-dialogue');
    const origHtml = btn.innerHTML;
    btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Polishing...`;
    btn.disabled = true;

    try {
        const resp = await fetch(`/api/video/job/${jobId}/ai-polish-dialogue`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ...getTranslationOptions(),
                tone: 'casual',
                segments: studioSegments
            })
        });
        if (!resp.ok) throw new Error("Polishing dialogue failed");
        const data = await resp.json();
        if (studioJobId !== jobId || generation !== trackingGeneration) return;
        studioSegments = data.segments;
        markStudioDirty();
        renderStudioCueCards();
        showToast(data.message || "Dialogue translated directly from the source. Review the Khmer before re-rendering.", data.status === 'partial' ? "warning" : "success");
    } catch (e) {
        showToast("Error: " + e.message, "error");
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
}

async function studioTranslateMissing() {
    if (!studioJobId || isRepairingMissing) return;
    const jobId = studioJobId;
    const generation = trackingGeneration;
    const btn = document.getElementById('btn-translate-missing');
    const originalHtml = btn.innerHTML;
    const initialSegments = JSON.parse(JSON.stringify(studioSegments));
    isRepairingMissing = true;
    btn.disabled = true;
    btn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Translating missing cues...';
    try {
        const response = await fetch(`/api/video/job/${jobId}/translate-missing`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...getTranslationOptions(), segments: initialSegments, tone: 'casual' })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Could not translate missing dialogue');
        if (studioJobId !== jobId || generation !== trackingGeneration) return;
        // Preserve any text typed while the repair request was running.
        const originals = new Map(initialSegments.map(seg => [seg.id, seg]));
        const repaired = new Map(data.segments.map(seg => [seg.id, seg]));
        studioSegments = studioSegments.map(seg => {
            const original = originals.get(seg.id);
            const replacement = repaired.get(seg.id);
            if (!original || !replacement || ((original.translation || '').trim() && !original.translation_error) ||
                seg.translation !== original.translation || seg.text !== original.text) return seg;
            return { ...seg, translation: replacement.translation, english_intent: replacement.english_intent,
                translation_flow: replacement.translation_flow, translation_source_key: replacement.translation_source_key,
                translation_source_text: replacement.translation_source_text, source_warning: replacement.source_warning,
                translation_warning: replacement.translation_warning, english_evidence_key: replacement.english_evidence_key,
                english_reference_error: replacement.english_reference_error,
                translation_error: replacement.translation_error, audio_file: replacement.audio_file,
                audio_source: replacement.audio_source, generation_warning: replacement.generation_warning,
                review_required: replacement.review_required, fallback_subtitle: replacement.fallback_subtitle,
                _dirty: replacement._dirty };
        });
        if (data.repaired_count) markStudioDirty();
        const message = data.remaining_count
            ? `${data.repaired_count} cues translated; ${data.remaining_count} still need a retry.`
            : `${data.repaired_count} missing cues translated. Re-render to update the video audio.`;
        showToast(message, data.remaining_count ? 'error' : 'success');
    } catch (error) {
        showToast(error.message, 'error');
    } finally {
        isRepairingMissing = false;
        btn.innerHTML = originalHtml;
        renderStudioCueCards();
    }
}

function markStudioDirty() {
    studioDirty = true;
}

function formatSeconds(sec) {
    const m = Math.floor(sec / 60);
    const s = (sec % 60).toFixed(2);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(5, '0')}`;
}

function setPipelineUIState(state) {
    const pipelineCard = document.getElementById('video-pipeline-card');
    if (!pipelineCard) return;

    if (state === 'active') {
        pipelineCard.style.display = 'block';
        document.getElementById('video-results-card').style.display = 'none';
        document.getElementById('pipeline-progress-fill').style.width = '0%';
        document.getElementById('pipeline-percent-text').textContent = '…';
        document.getElementById('pipeline-stage-text').textContent = 'Loading saved progress...';
        document.getElementById('pipeline-resume-btn').style.display = 'none';
        const pauseButton = document.getElementById('pipeline-pause-btn');
        if (pauseButton) pauseButton.style.display = 'none';
        document.getElementById('pipeline-job-title').textContent = 'Video task';
        document.getElementById('pipeline-job-id').textContent = activeVideoJobId ? `Job ${activeVideoJobId}` : '';
        for (const id of ['step-dl', 'step-stt', 'step-trans', 'step-tts', 'step-mix']) {
            document.getElementById(id)?.classList.remove('completed', 'active');
        }
        if (savedPipelineProgress.has(activeVideoJobId)) updatePipelineProgress(savedPipelineProgress.get(activeVideoJobId));
    } else {
        pipelineCard.style.display = 'none';
    }
}

function fillYouTubeSample() {
    const sampleUrl = "https://www.youtube.com/watch?v=jNQXAC9IVRw";
    const urlInput = document.getElementById('video-url-input');
    if (urlInput) urlInput.value = sampleUrl;
    showToast("Loaded YouTube sample video! Ready to dub.", "success");
}

function fillBilibiliSample() {
    const sampleUrl = "https://www.bilibili.com/video/BV1Foa16yEAh";
    document.getElementById('video-url-input').value = sampleUrl;
    showToast("Loaded Bilibili BV1Foa16yEAh!", "success");
}

async function loadCompletedDemoJob(jobId = 'bea1cc9b') {
    const generation = ++trackingGeneration;
    if (pollInterval) clearInterval(pollInterval);
    try {
        const resp = await fetch(`/api/video/status/${jobId}`);
        if (!resp.ok) {
            if (resp.status === 404 && generation === trackingGeneration) {
                forgetVideoTask(jobId);
                return;
            }
            showToast("Job not found or expired", "error");
            return;
        }
        const job = await resp.json();
        if (generation !== trackingGeneration) return;
        activeVideoJobId = jobId;
        renderVideoJobs();
        localStorage.setItem('khmer_active_video_job', jobId);
        if (job.status === 'completed' || (job.status === 'failed' && job.segments?.length)) {
            displayCompletedVideo(job);
            showToast(`Loaded CapCut Dubbing Studio for ${job.title || jobId}!`, "success");
        } else if (['processing', 'queued', 'interrupted', 'failed', 'paused', 'pausing', 'running', 'rendering'].includes(job.status)) {
            setPipelineUIState('active');
            updatePipelineProgress(job);
            startPollingJobStatus(jobId);
            showToast(`Tracking live dubbing job ${jobId}...`, "info");
        } else {
            showToast(`Job ${jobId} status: ${job.status}`, "info");
        }
    } catch (e) {
        showToast("Error loading job: " + e.message, "error");
    }
}

// Auto-check for job parameter in URL on load
document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    const jobParam = urlParams.get('job') || localStorage.getItem('khmer_active_video_job');
    if (jobParam) {
        loadCompletedDemoJob(jobParam);
    }

    // Bind Enter key in download filename input
    document.getElementById('download-filename-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            executeDownloadWithAnimation();
        }
    });
});

// =========================================================================
// CUSTOM DOWNLOAD DIALOG & PROGRESS ANIMATION
// =========================================================================
let currentDownloadJob = null;
let currentDownloadType = 'video'; // 'video' | 'srt'

function openDownloadModal(type = 'video', event = null) {
    if (event) event.preventDefault();
    currentDownloadType = type;

    const modal = document.getElementById('download-modal');
    if (!modal) return;

    const titleEl = document.getElementById('download-modal-title');
    const iconEl = document.getElementById('download-file-icon');
    const typeEl = document.getElementById('download-file-type');
    const metaEl = document.getElementById('download-file-meta');
    const extBadge = document.getElementById('download-ext-badge');
    const extSuffix = document.getElementById('filename-ext-suffix');
    const filenameInput = document.getElementById('download-filename-input');
    const progressBox = document.getElementById('download-progress-box');
    const confirmBtn = document.getElementById('download-confirm-btn');
    const cancelBtn = document.getElementById('download-cancel-btn');

    // Reset progress & state
    if (progressBox) progressBox.style.display = 'none';
    if (cancelBtn) cancelBtn.disabled = false;
    if (confirmBtn) {
        confirmBtn.classList.remove('downloading', 'success');
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = '<span class="btn-content"><i class="ph ph-download-simple"></i> Download Now</span>';
    }

    const jobTitle = currentDownloadJob?.title || 'dubbed_video';
    const cleanTitle = jobTitle.replace(/[^a-zA-Z0-9_\-\u1780-\u17FF\s]/g, '').trim().replace(/\s+/g, '_') || 'video';

    if (type === 'video') {
        if (titleEl) titleEl.innerHTML = '<i class="ph ph-download-simple"></i> Download Dubbed Video';
        if (iconEl) iconEl.innerHTML = '<i class="ph ph-video-camera"></i>';
        if (typeEl) typeEl.textContent = 'MP4 Video (Dubbed Khmer Voice)';
        if (metaEl) metaEl.textContent = 'Full HD Video Track • 256kbps Master Audio';
        if (extBadge) extBadge.textContent = '.mp4';
        if (extSuffix) extSuffix.textContent = '.mp4';
        if (filenameInput) filenameInput.value = `${cleanTitle}_khmer_dubbed`;
    } else {
        if (titleEl) titleEl.innerHTML = '<i class="ph ph-file-text"></i> Download Khmer Subtitles';
        if (iconEl) iconEl.innerHTML = '<i class="ph ph-file-text"></i>';
        if (typeEl) typeEl.textContent = 'SubRip Subtitle File (.srt)';
        if (metaEl) metaEl.textContent = 'Synchronized UTF-8 Khmer Dialogue Subtitles';
        if (extBadge) extBadge.textContent = '.srt';
        if (extSuffix) extSuffix.textContent = '.srt';
        if (filenameInput) filenameInput.value = `${cleanTitle}_khmer_subtitles`;
    }

    modal.classList.add('active');
    setTimeout(() => {
        if (filenameInput) {
            filenameInput.focus();
            filenameInput.select();
        }
    }, 120);
}

function closeDownloadModal() {
    const modal = document.getElementById('download-modal');
    if (modal) modal.classList.remove('active');
}

async function executeDownloadWithAnimation() {
    const filenameInput = document.getElementById('download-filename-input');
    const rawName = (filenameInput?.value || '').trim() || (currentDownloadType === 'video' ? 'dubbed_video' : 'subtitles');
    const ext = currentDownloadType === 'video' ? '.mp4' : '.srt';
    const cleanBase = rawName.replace(new RegExp(`\\${ext}$`, 'i'), '').trim();
    const finalFilename = `${cleanBase}${ext}`;

    const downloadUrl = currentDownloadType === 'video'
        ? currentDownloadJob?.result?.download_url
        : currentDownloadJob?.result?.srt_url;

    if (!downloadUrl) {
        showToast("Download URL not found for this job.", "error");
        return;
    }

    const progressBox = document.getElementById('download-progress-box');
    const animFill = document.getElementById('download-anim-fill');
    const statusText = document.getElementById('download-status-text');
    const percentText = document.getElementById('download-percent-text');
    const confirmBtn = document.getElementById('download-confirm-btn');
    const cancelBtn = document.getElementById('download-cancel-btn');

    if (progressBox) progressBox.style.display = 'flex';
    if (confirmBtn) {
        confirmBtn.classList.add('downloading');
        confirmBtn.disabled = true;
        confirmBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Downloading...';
    }
    if (cancelBtn) cancelBtn.disabled = true;

    if (animFill) animFill.style.width = '12%';
    if (percentText) percentText.textContent = '12%';
    if (statusText) statusText.innerHTML = '<i class="ph ph-arrow-circle-down ph-pulse"></i> Initializing transfer...';

    try {
        const response = await fetch(downloadUrl);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const contentLength = +response.headers.get('Content-Length');
        let blob;

        if (contentLength && window.ReadableStream && response.body) {
            const reader = response.body.getReader();
            let receivedLength = 0;
            const chunks = [];

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value);
                receivedLength += value.length;
                const pct = Math.min(98, Math.round((receivedLength / contentLength) * 100));
                if (animFill) animFill.style.width = `${pct}%`;
                if (percentText) percentText.textContent = `${pct}%`;
                const mb = (receivedLength / (1024 * 1024)).toFixed(1);
                const totalMb = (contentLength / (1024 * 1024)).toFixed(1);
                if (statusText) statusText.innerHTML = `<i class="ph ph-arrow-circle-down ph-spin"></i> Transferring ${mb} MB / ${totalMb} MB...`;
            }
            blob = new Blob(chunks, { type: currentDownloadType === 'video' ? 'video/mp4' : 'text/plain;charset=utf-8' });
        } else {
            let sim = 20;
            const interval = setInterval(() => {
                if (sim < 92) {
                    sim += Math.floor(Math.random() * 18) + 6;
                    if (animFill) animFill.style.width = `${sim}%`;
                    if (percentText) percentText.textContent = `${sim}%`;
                }
            }, 100);
            blob = await response.blob();
            clearInterval(interval);
        }

        // 100% Success Celebration
        if (animFill) animFill.style.width = '100%';
        if (percentText) percentText.textContent = '100%';
        if (statusText) statusText.innerHTML = '<i class="ph ph-check-circle" style="color: #10b981;"></i> Download ready!';

        if (confirmBtn) {
            confirmBtn.classList.remove('downloading');
            confirmBtn.classList.add('success');
            confirmBtn.innerHTML = '<i class="ph ph-check-bold"></i> Saved!';
        }

        // Trigger native download with user's custom name
        const blobUrl = URL.createObjectURL(blob);
        const tempLink = document.createElement('a');
        tempLink.href = blobUrl;
        tempLink.download = finalFilename;
        document.body.appendChild(tempLink);
        tempLink.click();
        document.body.removeChild(tempLink);
        setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

        showToast(`🎉 Successfully saved as "${finalFilename}"`, "success");

        setTimeout(() => {
            closeDownloadModal();
            if (cancelBtn) cancelBtn.disabled = false;
        }, 1200);

    } catch (err) {
        console.warn("Direct stream fetch failed, using anchor fallback:", err);
        const tempLink = document.createElement('a');
        tempLink.href = downloadUrl;
        tempLink.download = finalFilename;
        document.body.appendChild(tempLink);
        tempLink.click();
        document.body.removeChild(tempLink);

        if (animFill) animFill.style.width = '100%';
        if (percentText) percentText.textContent = '100%';
        if (statusText) statusText.innerHTML = '<i class="ph ph-check-circle" style="color: #10b981;"></i> Download triggered!';
        if (confirmBtn) {
            confirmBtn.classList.remove('downloading');
            confirmBtn.classList.add('success');
            confirmBtn.innerHTML = '<i class="ph ph-check-bold"></i> Started!';
        }

        setTimeout(() => {
            closeDownloadModal();
            if (cancelBtn) cancelBtn.disabled = false;
        }, 1200);
    }
}
