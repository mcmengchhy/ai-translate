/**
 * Khmer AI Video Dubber & Bilibili Translator Client
 */

let activeVideoJobId = null;
let pollInterval = null;

function escapeHtml(str) {
    if (!str) return '';
    return String(str).replace(/[&<>'"]/g, tag => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;'
    }[tag] || tag));
}

document.addEventListener('DOMContentLoaded', () => {
    initVideoDubberEvents();
    initVoiceTunerControls();
    loadRecentJobsList();

    // Guarantee default audio mode is cinema_3 (3% Background + Anti-Copyright Effect)
    const audioModeSelect = document.getElementById('video-audio-mode');
    if (audioModeSelect && (!audioModeSelect.value || audioModeSelect.value === 'constant_25' || audioModeSelect.value === 'cinema_10' || audioModeSelect.value === 'cinema_4')) {
        audioModeSelect.value = 'cinema_3';
    }

    const voiceSelect = document.getElementById('video-voice-select');
    if (voiceSelect && (!voiceSelect.value || voiceSelect.value === 'male')) {
        voiceSelect.value = 'multi';
    }
});

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
        videoDropzone.addEventListener('click', () => videoFileInput.click());
        videoFileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                const file = e.target.files[0];
                document.getElementById('selected-video-filename').textContent = `Selected: ${file.name} (${(file.size / (1024*1024)).toFixed(1)} MB)`;
                document.getElementById('video-upload-action-wrap').style.display = 'block';
            }
        });
    }

    if (subFileInput) {
        subFileInput.addEventListener('change', (e) => {
            const subLabel = document.getElementById('selected-sub-filename');
            if (e.target.files.length > 0 && subLabel) {
                subLabel.textContent = `Attached Subtitle: ${e.target.files[0].name}`;
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
    const effectiveVoice = voiceSelect === 'multi' ? 'male' : voiceSelect;

    const voiceLabels = {
        'multi': 'Multi-Cast Preview (Piseth Hero)',
        'male': 'Piseth (Lead Hero)',
        'male_deep': 'Piseth (Mature Father / Authority -10Hz)',
        'male_young': 'Piseth (Energetic Boy / Youth +10Hz)',
        'female': 'Sreymom (Lead Heroine)',
        'female_sweet': 'Sreymom (Sweet Young Girl +10Hz)',
        'female_elder': 'Sreymom (Mature Mother / Elder -7Hz)',
    };

    try {
        const resp = await fetch('/api/video/preview-voice', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: "សួស្តីបងប្អូនទាំងអស់គ្នា សូមស្វាគមន៍មកកាន់វីដេអូបកប្រែជាភាសាខ្មែរ",
                voice_type: effectiveVoice,
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
        const vLabel = voiceLabels[effectiveVoice] || effectiveVoice;
        showToast(`Playing persona: ${vLabel} [Rate: ${tuner.rate}, Pitch: ${tuner.pitch}, Vol: ${tuner.volume}]`, 'success');
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
async function handleUrlDubbing() {
    const urlInput = document.getElementById('video-url-input');
    const url = urlInput.value.trim();

    if (!url) {
        showToast("Please enter a video URL (YouTube, Bilibili, TikTok, Facebook, etc.)!", "error");
        return;
    }

    const platform = detectPlatformClient(url);
    const voiceType = document.getElementById('video-voice-select').value;
    const tone = document.getElementById('video-tone-select').value;
    let audioMode = document.getElementById('video-audio-mode').value;
    const removeOriginSound = document.getElementById('remove-origin-sound-checkbox')?.checked ?? true;
    if (removeOriginSound) {
        audioMode = "replace";
    }
    const burnSubtitles = document.getElementById('burn-subs-checkbox')?.checked ?? false;
    const maskHardsubs = document.getElementById('mask-hardsubs-checkbox')?.checked ?? true;
    const antiCopyrightVideo = document.getElementById('anti-copyright-video-checkbox')?.checked ?? true;
    const mirrorVideo = document.getElementById('mirror-video-checkbox')?.checked ?? false;
    const maxDur = parseInt(document.getElementById('video-duration-select')?.value ?? '0', 10);
    const splitDur = parseInt(document.getElementById('video-split-duration')?.value ?? '1800', 10);
    const settings = getSavedSettings();
    const tuner = getVoiceTunerValues();

    setPipelineUIState('active');

    try {
        const resp = await fetch('/api/video/dub-url', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: url,
                voice_type: voiceType,
                tone: tone,
                audio_mode: audioMode,
                remove_origin_sound: removeOriginSound,
                burn_subtitles: burnSubtitles,
                mask_hardsubs: maskHardsubs,
                anti_copyright_video: antiCopyrightVideo,
                mirror_video: mirrorVideo,
                max_duration: maxDur,
                api_key: settings.geminiKey || null,
                voice_rate: tuner.rate,
                voice_pitch: tuner.pitch,
                voice_volume: tuner.volume,
                split_duration: splitDur
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Request failed" }));
            throw new Error(err.detail || "Failed to start dubbing job");
        }

        const data = await resp.json();
        activeVideoJobId = data.job_id;
        startPollingJobStatus(activeVideoJobId);
        showToast(`🎉 [${platform}] Dubbing job queued! Downloading, translating and applying shield...`, "success");
    } catch (err) {
        setPipelineUIState('idle');
        showToast(err.message, "error");
    }
}

// Start Dubbing from Uploaded File
async function handleFileDubbing() {
    const fileInput = document.getElementById('video-file-input');
    if (!fileInput.files || fileInput.files.length === 0) {
        showToast("Please select a video file first", "error");
        return;
    }

    const file = fileInput.files[0];
    const voiceType = document.getElementById('video-voice-select').value;
    const tone = document.getElementById('video-tone-select').value;
    let audioMode = document.getElementById('video-audio-mode').value;
    const removeOriginSound = document.getElementById('remove-origin-sound-checkbox')?.checked ?? true;
    if (removeOriginSound) {
        audioMode = "replace";
    }
    const burnSubtitles = document.getElementById('burn-subs-checkbox')?.checked ?? false;
    const maskHardsubs = document.getElementById('mask-hardsubs-checkbox')?.checked ?? true;
    const antiCopyrightVideo = document.getElementById('anti-copyright-video-checkbox')?.checked ?? true;
    const mirrorVideo = document.getElementById('mirror-video-checkbox')?.checked ?? false;
    const splitDur = parseInt(document.getElementById('video-split-duration')?.value ?? '1800', 10);
    const settings = getSavedSettings();
    const tuner = getVoiceTunerValues();

    const formData = new FormData();
    formData.append('file', file);
    formData.append('voice_type', voiceType);
    formData.append('tone', tone);
    formData.append('audio_mode', audioMode);
    formData.append('remove_origin_sound', removeOriginSound);
    formData.append('burn_subtitles', burnSubtitles);
    formData.append('mask_hardsubs', maskHardsubs);
    formData.append('anti_copyright_video', antiCopyrightVideo);
    formData.append('mirror_video', mirrorVideo);
    formData.append('split_duration', splitDur);
    if (settings.geminiKey) formData.append('api_key', settings.geminiKey);
    formData.append('voice_rate', tuner.rate);
    formData.append('voice_pitch', tuner.pitch);
    formData.append('voice_volume', tuner.volume);
    const subFileInput = document.getElementById('video-sub-file-input');
    if (subFileInput && subFileInput.files && subFileInput.files.length > 0) {
        formData.append('subtitle_file', subFileInput.files[0]);
    }
    formData.append('voice_type', voiceType);
    formData.append('tone', tone);
    formData.append('audio_mode', audioMode);
    formData.append('burn_subtitles', burnSubtitles ? 'true' : 'false');
    formData.append('voice_rate', tuner.rate);
    formData.append('voice_pitch', tuner.pitch);
    formData.append('voice_volume', tuner.volume);
    if (settings.geminiKey) formData.append('api_key', settings.geminiKey);

    setPipelineUIState('active');

    try {
        const resp = await fetch('/api/video/dub-upload', {
            method: 'POST',
            body: formData
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Upload failed" }));
            throw new Error(err.detail || "Upload failed");
        }

        const data = await resp.json();
        activeVideoJobId = data.job_id;
        startPollingJobStatus(activeVideoJobId);
        showToast("Video uploaded! Starting transcription and dubbing...", "success");
    } catch (err) {
        setPipelineUIState('idle');
        showToast(err.message, "error");
    }
}

// Poll Job Status with overlap protection
let isPollingActive = false;

function startPollingJobStatus(jobId) {
    activeVideoJobId = jobId;
    if (pollInterval) clearInterval(pollInterval);
    isPollingActive = false;

    // Reset error state
    const errorBox = document.getElementById('pipeline-error-box');
    if (errorBox) errorBox.style.display = 'none';
    const pulsingDot = document.getElementById('pipeline-pulsing-dot');
    if (pulsingDot) pulsingDot.style.background = 'var(--accent-cyan)';

    pollInterval = setInterval(async () => {
        if (isPollingActive) return; // Prevent request pile-up
        isPollingActive = true;

        try {
            const resp = await fetch(`/api/video/status/${jobId}`);
            if (!resp.ok) return;

            const job = await resp.json();
            updatePipelineProgress(job);

            if (job.status === 'completed') {
                clearInterval(pollInterval);
                displayCompletedVideo(job);
                loadRecentJobsList();
                showToast("🎉 Video dubbing complete! Ready to watch & download.", "success");
            } else if (job.status === 'cancelled') {
                clearInterval(pollInterval);
                setPipelineUIState('idle');
                const pipelineCard = document.getElementById('video-pipeline-card');
                if (pipelineCard) pipelineCard.style.display = 'none';
                loadRecentJobsList();
                showToast("⛔ Process cancelled by user.", "info");
            } else if (job.status === 'failed') {
                clearInterval(pollInterval);
                const errorBox = document.getElementById('pipeline-error-box');
                const errorMsg = document.getElementById('pipeline-error-msg');
                const pulsingDot = document.getElementById('pipeline-pulsing-dot');
                if (errorBox) errorBox.style.display = 'block';
                if (errorMsg) errorMsg.textContent = job.error || "Process stopped. All transcribed cues & audio are safely saved.";
                if (pulsingDot) pulsingDot.style.background = '#ef4444';
                loadRecentJobsList();
                showToast(`Dubbing paused: ${job.error || "Click Resume to continue"}`, "warning");
            }
        } catch (err) {
            console.error("Polling error:", err);
        } finally {
            isPollingActive = false;
        }
    }, 800);
}

// ─────────────────────────────────────────────
// Checkpoint & Auto-Resume Handlers
// ─────────────────────────────────────────────
async function resumeCurrentJob() {
    let jobId = activeVideoJobId;
    if (!jobId) {
        const select = document.getElementById('recent-jobs-select');
        if (select && select.value) jobId = select.value;
    }
    if (!jobId) {
        try {
            const resp = await fetch('/api/video/jobs');
            if (resp.ok) {
                const jobs = await resp.json();
                if (jobs.length > 0) jobId = jobs[0].id;
            }
        } catch (e) {}
    }
    if (!jobId) {
        showToast("No active job selected to resume", "warning");
        return;
    }
    await resumeJobById(jobId);
}

async function resumeJobById(jobId) {
    if (!jobId) return;

    try {
        const errorBox = document.getElementById('pipeline-error-box');
        if (errorBox) errorBox.style.display = 'none';
        const pulsingDot = document.getElementById('pipeline-pulsing-dot');
        if (pulsingDot) pulsingDot.style.background = 'var(--accent-cyan)';

        showToast(`⚡ Resuming job [${jobId}] from saved checkpoint...`, "info");
        setPipelineUIState('processing');

        const resp = await fetch(`/api/video/job/${jobId}/resume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            throw new Error(errData.detail || `Failed to resume (HTTP ${resp.status})`);
        }

        activeVideoJobId = jobId;
        startPollingJobStatus(jobId);
        showToast(`⚡ Resumed job ${jobId}! Processing from saved cues and audio...`, "success");
    } catch (err) {
        console.error("Resume error:", err);
        showToast(err.message, "error");
    }
}

async function loadRecentJobsList(autoSelectActiveOnly = true) {
    const select = document.getElementById('recent-jobs-select');
    if (!select) return;

    try {
        const resp = await fetch('/api/video/jobs');
        if (!resp.ok) return;
        const jobs = await resp.json();

        select.innerHTML = '<option value="">-- Select a past or pending job --</option>';
        jobs.forEach(j => {
            const opt = document.createElement('option');
            opt.value = j.id;
            const statusIcon = j.status === 'completed' ? '✅' : (j.status === 'cancelled' ? '⛔' : (j.status === 'failed' ? '⚠️' : '⏳'));
            opt.textContent = `${statusIcon} [${j.id}] ${j.title || 'Video'} (${j.progress}%, ${j.segments_count} cues)`;
            select.appendChild(opt);
        });

        // ONLY auto-select and show if a job is actively running or queued!
        const runningJob = jobs.find(j => j.status === 'processing' || j.status === 'queued');
        if (runningJob) {
            select.value = runningJob.id;
            onSelectRecentJob(runningJob.id);
        } else {
            // Keep idle so user has a clean interface to start new jobs
            select.value = "";
            const resumeBtn = document.getElementById('btn-quick-resume');
            const quickCancelBtn = document.getElementById('btn-quick-cancel');
            if (resumeBtn) resumeBtn.style.display = 'none';
            if (quickCancelBtn) quickCancelBtn.style.display = 'none';
        }
    } catch (err) {
        console.warn("Could not load recent jobs:", err);
    }
}

async function onSelectRecentJob(jobId) {
    const resumeBtn = document.getElementById('btn-quick-resume');
    const quickCancelBtn = document.getElementById('btn-quick-cancel');
    if (!jobId) {
        if (resumeBtn) resumeBtn.style.display = 'none';
        if (quickCancelBtn) quickCancelBtn.style.display = 'none';
        dismissCurrentJob();
        return;
    }

    // Immediately fetch and switch UI to this job
    activeVideoJobId = jobId;
    try {
        const resp = await fetch(`/api/video/status/${jobId}`);
        if (!resp.ok) return;
        const job = await resp.json();

        setPipelineUIState('active');
        updatePipelineProgress(job);

        const errorBox = document.getElementById('pipeline-error-box');
        const pulsingDot = document.getElementById('pipeline-pulsing-dot');
        const errorTitle = document.getElementById('pipeline-error-title');
        const errorMsg = document.getElementById('pipeline-error-msg');

        if (job.status === 'processing' || job.status === 'queued') {
            if (resumeBtn) resumeBtn.style.display = 'none';
            if (quickCancelBtn) quickCancelBtn.style.display = 'inline-flex';
            if (errorBox) errorBox.style.display = 'none';
            if (pulsingDot) {
                pulsingDot.style.background = job.status === 'queued' ? '#f59e0b' : 'var(--accent-cyan)';
                pulsingDot.style.opacity = '1';
            }
            startPollingJobStatus(jobId);
        } else if (job.status === 'completed') {
            if (quickCancelBtn) quickCancelBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = 'none';
            if (errorBox) errorBox.style.display = 'none';
            if (pulsingDot) {
                pulsingDot.style.background = '#10b981';
            }
            displayCompletedVideo(job);
        } else if (job.status === 'cancelled') {
            if (quickCancelBtn) quickCancelBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = 'inline-flex';
            if (pulsingDot) {
                pulsingDot.style.background = '#ef4444';
            }
            if (errorBox) {
                errorBox.style.display = 'block';
                if (errorTitle) errorTitle.textContent = "Pipeline Cancelled by User";
                if (errorMsg) errorMsg.textContent = "This dubbing job was cancelled. All generated transcripts & audio chunks are safely saved. Click 'Resume Job' to continue, or 'Dismiss' to clear.";
            }
        } else if (job.status === 'failed') {
            if (quickCancelBtn) quickCancelBtn.style.display = 'none';
            if (resumeBtn) resumeBtn.style.display = 'inline-flex';
            if (pulsingDot) {
                pulsingDot.style.background = '#ef4444';
            }
            if (errorBox) {
                errorBox.style.display = 'block';
                if (errorTitle) errorTitle.textContent = "Pipeline Paused / Interrupted";
                if (errorMsg) errorMsg.textContent = job.error || "Job was interrupted. Click Resume to continue.";
            }
        }
    } catch (err) {
        console.error("Error switching to selected job:", err);
    }
}

function dismissCurrentJob() {
    activeVideoJobId = null;
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
    setPipelineUIState('idle');
    const select = document.getElementById('recent-jobs-select');
    if (select) select.value = "";
    const pipelineCard = document.getElementById('video-pipeline-card');
    if (pipelineCard) pipelineCard.style.display = 'none';
    const episodicCard = document.getElementById('episodic-episodes-card');
    if (episodicCard) episodicCard.style.display = 'none';
    const resumeBtn = document.getElementById('btn-quick-resume');
    const quickCancelBtn = document.getElementById('btn-quick-cancel');
    if (resumeBtn) resumeBtn.style.display = 'none';
    if (quickCancelBtn) quickCancelBtn.style.display = 'none';
    showToast("Cleared active pipeline view. Ready for new video.", "info");
}

async function resumeSelectedRecentJob() {
    const select = document.getElementById('recent-jobs-select');
    if (!select || !select.value) {
        showToast("Please select a job from the list first", "warning");
        return;
    }
    await resumeJobById(select.value);
}

async function cancelCurrentJob() {
    let jobId = activeVideoJobId;
    if (!jobId) {
        const select = document.getElementById('recent-jobs-select');
        if (select && select.value) jobId = select.value;
    }
    if (!jobId) {
        showToast("No active job to cancel", "warning");
        return;
    }

    if (!confirm("Are you sure you want to cancel the dubbing process?")) {
        return;
    }

    const cancelBtn = document.getElementById('btn-cancel-pipeline');
    const quickCancelBtn = document.getElementById('btn-quick-cancel');
    if (cancelBtn) {
        cancelBtn.disabled = true;
        cancelBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Cancelling...';
    }
    if (quickCancelBtn) {
        quickCancelBtn.disabled = true;
        quickCancelBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Cancelling...';
    }

    try {
        const resp = await fetch(`/api/video/job/${jobId}/cancel`, {
            method: 'POST'
        });

        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }

        if (resp.ok) {
            showToast("Process successfully cancelled.", "info");
            dismissCurrentJob();
            loadRecentJobsList(true);
        } else {
            const err = await resp.json().catch(() => ({ detail: "Failed to cancel" }));
            showToast(err.detail || "Failed to cancel process", "error");
        }
    } catch (err) {
        showToast(err.message, "error");
    } finally {
        if (cancelBtn) {
            cancelBtn.disabled = false;
            cancelBtn.innerHTML = '<i class="ph ph-x-circle"></i> Cancel Process';
        }
        if (quickCancelBtn) {
            quickCancelBtn.disabled = false;
            quickCancelBtn.innerHTML = '<i class="ph ph-x-circle"></i> Cancel';
        }
    }
}

// Update Visual Pipeline Stages
function updatePipelineProgress(job) {
    const stageText = document.getElementById('pipeline-stage-text');
    const percentText = document.getElementById('pipeline-percent-text');
    const progressBar = document.getElementById('pipeline-progress-fill');
    const jobTitle = document.getElementById('pipeline-job-title');
    const cancelPipelineBtn = document.getElementById('btn-cancel-pipeline');

    if (stageText) stageText.textContent = job.stage || "Processing...";
    if (percentText) percentText.textContent = `${job.progress}%`;
    if (progressBar) progressBar.style.width = `${job.progress}%`;
    if (jobTitle && job.title) jobTitle.textContent = job.title;

    if (cancelPipelineBtn) {
        if (job.status === 'processing' || job.status === 'queued') {
            cancelPipelineBtn.style.display = 'inline-flex';
        } else {
            cancelPipelineBtn.style.display = 'none';
        }
    }

    // Auto-hide error banner if the job is active
    const errorBox = document.getElementById('pipeline-error-box');
    if (errorBox && (job.status === 'processing' || job.status === 'queued' || job.status === 'completed')) {
        errorBox.style.display = 'none';
    }
    const pulsingDot = document.getElementById('pipeline-pulsing-dot');
    if (pulsingDot && (job.status === 'processing' || job.status === 'queued')) {
        pulsingDot.style.background = 'var(--accent-cyan)';
    }

    // Live Metrics Strip (Speed, ETA, Details)
    const speedChip = document.getElementById('metric-speed');
    const speedVal = document.getElementById('metric-speed-val');
    if (speedChip && speedVal) {
        if (job.speed) {
            speedVal.textContent = job.speed;
            speedChip.style.display = 'inline-flex';
        } else {
            speedChip.style.display = 'none';
        }
    }

    const etaChip = document.getElementById('metric-eta');
    const etaVal = document.getElementById('metric-eta-val');
    if (etaChip && etaVal) {
        if (job.eta) {
            etaVal.textContent = job.eta;
            etaChip.style.display = 'inline-flex';
        } else {
            etaChip.style.display = 'none';
        }
    }

    const detailChip = document.getElementById('metric-detail');
    const detailVal = document.getElementById('metric-detail-val');
    if (detailChip && detailVal) {
        if (job.detail) {
            detailVal.textContent = job.detail;
            detailChip.style.display = 'inline-flex';
        } else {
            detailChip.style.display = 'none';
        }
    }

    // Stepper icons highlight
    const steps = [
        { id: 'step-dl', threshold: 10 },
        { id: 'step-stt', threshold: 30 },
        { id: 'step-trans', threshold: 55 },
        { id: 'step-tts', threshold: 75 },
        { id: 'step-mix', threshold: 90 },
    ];

    steps.forEach((s) => {
        const el = document.getElementById(s.id);
        if (el) {
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

    // Render Episodic 30-minute Part Cards if video is chunked
    if (job.is_episodic || (job.episodes && job.episodes.length > 0)) {
        renderEpisodicCards(job);
    }
}

// ─────────────────────────────────────────────
// Episodic 30-Minute Part Cards & Multi-Download Engine
// ─────────────────────────────────────────────
let selectedEpisodeIndices = new Set();
let latestMergedVideoUrl = null;

function renderEpisodicCards(job) {
    const card = document.getElementById('episodic-episodes-card');
    const grid = document.getElementById('episodic-cards-grid');
    const summaryText = document.getElementById('episodic-summary-text');
    const zipBtn = document.getElementById('btn-download-all-zip');
    const mergeBtn = document.getElementById('btn-merge-selected-episodes');
    const selectStrip = document.getElementById('episodic-batch-select-strip');
    const selectedLabel = document.getElementById('selected-episodes-label');
    const selectAllCheckbox = document.getElementById('checkbox-select-all-episodes');
    if (!card || !grid) return;

    const episodes = job.episodes || [];
    if (!job.is_episodic && episodes.length === 0) {
        card.style.display = 'none';
        return;
    }

    card.style.display = 'block';

    const completed = episodes.filter(e => e.status === 'completed');
    const total = episodes.length;

    // Prune invalid selected indices
    const completedIndices = new Set(completed.map(e => e.index));
    for (const idx of selectedEpisodeIndices) {
        if (!completedIndices.has(idx)) {
            selectedEpisodeIndices.delete(idx);
        }
    }

    if (summaryText) {
        if (job.status === 'cancelled') {
            summaryText.innerHTML = `<span style="color: #ef4444; font-weight: 600;">⛔ Process was cancelled.</span> <b>${completed.length} of ${total}</b> episodes completed before cancellation. Click <b>"Resume Job"</b> above to continue anytime.`;
        } else {
            summaryText.innerHTML = `<b>${completed.length} of ${total}</b> episodes completed (${Math.round((completed.length/Math.max(1, total))*100)}%) — Ready to download part-by-part or merge into 1 clip!`;
        }
    }

    // Toggle Batch Strip and Action Buttons
    if (completed.length > 0) {
        if (selectStrip) selectStrip.style.display = 'flex';
        if (zipBtn) {
            zipBtn.style.display = 'inline-flex';
            zipBtn.innerHTML = `<i class="ph ph-package"></i> Download All (.ZIP)`;
        }
    } else {
        if (selectStrip) selectStrip.style.display = 'none';
        if (zipBtn) zipBtn.style.display = 'none';
    }

    updateMergeButtonUI(completed.length);

    // Render cards
    grid.innerHTML = '';
    const isJobCancelled = job.status === 'cancelled';

    episodes.forEach((ep) => {
        const isDone = ep.status === 'completed';
        const isCancelled = ep.status === 'cancelled' || (isJobCancelled && !isDone);
        const isProcessing = ep.status === 'processing' && !isCancelled;
        const isChecked = selectedEpisodeIndices.has(ep.index);

        const cardEl = document.createElement('div');
        cardEl.className = 'episode-item-card';
        cardEl.style.cssText = `
            background: ${isDone ? (isChecked ? 'rgba(16, 185, 129, 0.12)' : 'rgba(16, 185, 129, 0.05)') : (isCancelled ? 'rgba(239, 68, 68, 0.03)' : (isProcessing ? 'rgba(56, 189, 248, 0.05)' : 'rgba(255, 255, 255, 0.02)'))};
            border: 1px solid ${isDone ? (isChecked ? '#10b981' : 'rgba(16, 185, 129, 0.35)') : (isCancelled ? 'rgba(239, 68, 68, 0.25)' : (isProcessing ? 'rgba(56, 189, 248, 0.35)' : 'var(--border-subtle)'))};
            border-radius: 10px;
            padding: 14px 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: all 0.2s ease;
        `;

        let statusBadge = '';
        if (isDone) {
            statusBadge = `<span style="font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: rgba(16, 185, 129, 0.2); color: #10b981; display: inline-flex; align-items: center; gap: 4px;">
                <i class="ph ph-check-circle"></i> Ready to Download
            </span>`;
        } else if (isCancelled) {
            statusBadge = `<span style="font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: rgba(239, 68, 68, 0.15); color: #ef4444; display: inline-flex; align-items: center; gap: 4px;">
                <i class="ph ph-x-circle"></i> Cancelled
            </span>`;
        } else if (isProcessing) {
            statusBadge = `<span style="font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 6px; background: rgba(56, 189, 248, 0.2); color: #38bdf8; display: inline-flex; align-items: center; gap: 4px;">
                <i class="ph ph-spinner ph-spin"></i> Dubbing Episode... (${ep.progress || 0}%)
            </span>`;
        } else {
            statusBadge = `<span style="font-size: 11px; font-weight: 500; padding: 3px 8px; border-radius: 6px; background: rgba(255, 255, 255, 0.05); color: var(--text-muted); display: inline-flex; align-items: center; gap: 4px;">
                <i class="ph ph-clock"></i> In Queue
            </span>`;
        }

        const durMin = Math.round((ep.duration || 1800) / 60);
        const cuesInfo = ep.cues_count ? ` • ${ep.cues_count} cues` : '';
        const sizeInfo = ep.size_mb ? ` • ${ep.size_mb} MB` : '';

        let actionsHtml = '';
        if (isDone && ep.download_url) {
            actionsHtml = `
                <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px;">
                    <a href="${ep.download_url}" download class="btn-primary btn-xs" style="background: #10b981; border-color: #10b981; text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                        <i class="ph ph-download-simple"></i> Download MP4
                    </a>
                    ${ep.srt_url ? `
                    <a href="${ep.srt_url}" download class="btn-secondary btn-xs" style="text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                        <i class="ph ph-subtitles"></i> SRT
                    </a>` : ''}
                    <button type="button" class="btn-secondary btn-xs" onclick="previewEpisodeVideo('${ep.download_url}', '${escapeHtml(ep.title)}')">
                        <i class="ph ph-play"></i> Play
                    </button>
                </div>
            `;
        } else if (isCancelled) {
            actionsHtml = `
                <div style="font-size: 11px; color: #ef4444; font-style: italic;">
                    Stopped (${ep.progress ? ep.progress + '%' : 'Cancelled by user'})
                </div>
            `;
        } else if (isProcessing) {
            actionsHtml = `
                <div style="font-size: 11px; color: var(--text-muted); font-style: italic;">
                    ${ep.stage || 'Translating and generating voices...'}
                </div>
            `;
        } else {
            actionsHtml = `
                <div style="font-size: 11px; color: var(--text-muted);">
                    Will automatically start once earlier parts complete
                </div>
            `;
        }

        const checkboxHtml = isDone ? `
            <input type="checkbox" class="episode-check-input" value="${ep.index}"
                ${isChecked ? 'checked' : ''}
                onchange="onToggleEpisodeSelection(${ep.index}, this.checked)"
                style="width: 18px; height: 18px; cursor: pointer; accent-color: #10b981; flex-shrink: 0;">
        ` : `
            <input type="checkbox" disabled style="width: 18px; height: 18px; opacity: 0.3; cursor: not-allowed; flex-shrink: 0;">
        `;

        cardEl.innerHTML = `
            <div style="display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;">
                <label style="display: flex; align-items: flex-start; gap: 8px; cursor: ${isDone ? 'pointer' : 'default'}; margin: 0; flex: 1;">
                    ${checkboxHtml}
                    <div>
                        <div style="font-weight: 600; font-size: 14px; color: ${isChecked ? '#10b981' : 'var(--text-primary)'};">${ep.title}</div>
                        <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">~${durMin} Mins${cuesInfo}${sizeInfo}</div>
                    </div>
                </label>
                ${statusBadge}
            </div>
            ${actionsHtml}
        `;

        grid.appendChild(cardEl);
    });
}

function updateMergeButtonUI(completedCount) {
    const mergeBtn = document.getElementById('btn-merge-selected-episodes');
    const selectedLabel = document.getElementById('selected-episodes-label');
    const selectAllCheckbox = document.getElementById('checkbox-select-all-episodes');

    const count = selectedEpisodeIndices.size;
    if (selectedLabel) {
        selectedLabel.textContent = `${count} parts selected`;
    }

    if (selectAllCheckbox) {
        selectAllCheckbox.checked = completedCount > 0 && count === completedCount;
    }

    if (mergeBtn) {
        if (count >= 1) {
            mergeBtn.style.display = 'inline-flex';
            const partWord = count === 1 ? 'Part' : 'Parts';
            mergeBtn.innerHTML = `<i class="ph ph-intersect"></i> Merge Selected (${count} ${partWord}) into 1 Video`;
        } else {
            mergeBtn.style.display = 'none';
        }
    }
}

function onToggleEpisodeSelection(epIndex, isChecked) {
    if (isChecked) {
        selectedEpisodeIndices.add(epIndex);
    } else {
        selectedEpisodeIndices.delete(epIndex);
    }
    // Re-render cards without losing state
    if (activeVideoJobId) {
        fetchEpisodicProgress();
    }
}

function toggleSelectAllEpisodes(isChecked) {
    const checkboxes = document.querySelectorAll('.episode-check-input');
    checkboxes.forEach((cb) => {
        const idx = parseInt(cb.value, 10);
        if (isChecked) {
            selectedEpisodeIndices.add(idx);
        } else {
            selectedEpisodeIndices.delete(idx);
        }
    });
    if (activeVideoJobId) {
        fetchEpisodicProgress();
    }
}

async function mergeSelectedEpisodes() {
    if (!activeVideoJobId) {
        showToast("No active job selected", "warning");
        return;
    }

    const indices = Array.from(selectedEpisodeIndices).sort((a, b) => a - b);
    if (indices.length === 0) {
        showToast("Please select at least 1 completed episode part to merge!", "warning");
        return;
    }

    const mergeBtn = document.getElementById('btn-merge-selected-episodes');
    const originalHtml = mergeBtn ? mergeBtn.innerHTML : '';
    if (mergeBtn) {
        mergeBtn.disabled = true;
        mergeBtn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Merging ${indices.length} Clips (Fast Stream Copy)...`;
    }

    showToast(`🔗 Merging ${indices.length} parts into 1 video...`, "info");

    try {
        const resp = await fetch(`/api/video/job/${activeVideoJobId}/merge-episodes`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                episode_indices: indices,
            })
        });

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            throw new Error(errData.detail || `Merge failed (HTTP ${resp.status})`);
        }

        const data = await resp.json();
        latestMergedVideoUrl = data.download_url;

        // Show merged banner
        const banner = document.getElementById('episodic-merged-banner');
        const titleEl = document.getElementById('merged-result-title');
        const metaEl = document.getElementById('merged-result-meta');
        const dlMp4Btn = document.getElementById('btn-download-merged-mp4');
        const dlSrtBtn = document.getElementById('btn-download-merged-srt');

        if (titleEl) titleEl.textContent = `🎉 Merged ${data.episodes_count} Parts into 1 Video (${data.filename})!`;
        if (metaEl) metaEl.textContent = `Duration: ${data.duration_formatted} • Size: ${data.size_mb} MB • ${data.subtitles_count} Khmer dialogue cues aligned`;
        if (dlMp4Btn) dlMp4Btn.href = data.download_url;
        if (dlSrtBtn) {
            if (data.srt_url) {
                dlSrtBtn.href = data.srt_url;
                dlSrtBtn.style.display = 'inline-flex';
            } else {
                dlSrtBtn.style.display = 'none';
            }
        }
        if (banner) {
            banner.style.display = 'block';
            banner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }

        // Auto trigger download of the merged video
        const downloadLink = document.createElement('a');
        downloadLink.href = data.download_url;
        downloadLink.download = data.filename;
        document.body.appendChild(downloadLink);
        downloadLink.click();
        document.body.removeChild(downloadLink);

        showToast(`🎉 Merged ${data.episodes_count} parts successfully! Starting download...`, "success");
    } catch (err) {
        console.error("Merge error:", err);
        showToast(err.message, "error");
    } finally {
        if (mergeBtn) {
            mergeBtn.disabled = false;
            mergeBtn.innerHTML = originalHtml;
        }
    }
}

function playMergedVideoNow() {
    if (!latestMergedVideoUrl) return;
    previewEpisodeVideo(latestMergedVideoUrl, "Merged Continuous Video");
}

function previewEpisodeVideo(url, title) {
    const videoPlayer = document.getElementById('dubbed-video-player');
    const resultsCard = document.getElementById('video-results-card');
    if (videoPlayer) {
        videoPlayer.src = url;
        if (resultsCard) resultsCard.style.display = 'block';
        videoPlayer.scrollIntoView({ behavior: 'smooth', block: 'center' });
        videoPlayer.play().catch(() => {});
        showToast(`Playing ${title}`, 'info');
    }
}

function downloadAllEpisodesZip() {
    if (!activeVideoJobId) {
        showToast("No active job selected", "warning");
        return;
    }
    showToast("Packaging all finished episodes into ZIP...", "info");
    window.location.href = `/api/video/job/${activeVideoJobId}/download-all-zip`;
}

async function fetchEpisodicProgress() {
    if (!activeVideoJobId) return;
    try {
        const resp = await fetch(`/api/video/job/${activeVideoJobId}/episodes`);
        if (!resp.ok) return;
        const data = await resp.json();
        renderEpisodicCards({ is_episodic: true, episodes: data.episodes });
    } catch (e) {}
}

// =========================================================================
// CAPCUT-STYLE KHMER DUBBING STUDIO ENGINE
// =========================================================================

let studioJobId = null;
let studioSegments = [];
let studioDirty = false;
let activeCueIndex = -1;

function displayCompletedVideo(job) {
    const resultsCard = document.getElementById('video-results-card');
    const videoPlayer = document.getElementById('dubbed-video-player');
    const downloadVideoBtn = document.getElementById('download-dubbed-video-btn');
    const downloadSrtBtn = document.getElementById('download-dubbed-srt-btn');

    if (!job || !job.result) return;

    currentDownloadJob = job;
    studioJobId = job.id;
    studioSegments = JSON.parse(JSON.stringify(job.segments || []));

    // Ensure every segment has character voice_type based on detected gender
    studioSegments.forEach(s => {
        if (!s.voice_type) {
            s.voice_type = (s.gender === "female") ? "female" : "male";
        }
    });

    videoPlayer.src = job.result.download_url;

    // Attach native WebVTT track for synchronized on-screen subtitle rendering
    const oldTracks = videoPlayer.querySelectorAll('track');
    oldTracks.forEach(t => t.remove());

    if (job.result.vtt_url) {
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = 'Khmer (ខ្មែរ)';
        track.srclang = 'km';
        track.src = job.result.vtt_url;
        track.default = true;
        videoPlayer.appendChild(track);
        try {
            if (videoPlayer.textTracks && videoPlayer.textTracks[0]) {
                videoPlayer.textTracks[0].mode = 'showing';
            }
        } catch (e) {
            console.debug("textTracks mode error:", e);
        }
    }
    if (downloadVideoBtn) {
        downloadVideoBtn.onclick = (e) => openDownloadModal('video', e);
    }
    if (downloadSrtBtn) {
        downloadSrtBtn.onclick = (e) => openDownloadModal('srt', e);
    }

    // Attach video player time listener for subtitle sync
    setupVideoSyncListener(videoPlayer);

    // Render interactive CapCut dialogue cue cards
    renderStudioCueCards();

    // Render episode parts list if available
    const parts = job.parts || job.result?.parts;
    if (parts && parts.length > 0) {
        renderVideoPartsList(parts);
    } else {
        const partsCard = document.getElementById('video-parts-card');
        if (partsCard) partsCard.style.display = 'none';
    }

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
                    ${seg.speaker ? `<span class="cue-speaker-badge" style="font-size:0.75rem; padding: 2px 6px; border-radius: 4px; background: rgba(59,130,246,0.12); color: #60a5fa; border: 1px solid rgba(59,130,246,0.25); margin-right: 4px;"><i class="ph ph-user"></i> ${escapeHtml(seg.speaker)}</span>` : ''}
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
                        <div class="cue-asr-hint">Audio speech heard: "${escapeHtml(seg.asr_text)}" (corrected by on-screen subtitle)</div>
                    ` : ''}
                </div>
                ${seg.english_intent ? `
                    <div class="cue-english-intent" title="Step 1: English Contextual Intent & Reasoning">
                        <span class="badge-intent-tag">🇬🇧 English Intent:</span>
                        <span>${escapeHtml(seg.english_intent)}</span>
                    </div>
                ` : ''}
                <div class="cue-khmer-wrap">
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
                        <option value="male" ${(!seg.voice_type || seg.voice_type === 'male') ? 'selected' : ''}>👨 Piseth (Lead Hero)</option>
                        <option value="male_deep" ${seg.voice_type === 'male_deep' ? 'selected' : ''}>🧔 Piseth (Deep Father / Authority -10Hz)</option>
                        <option value="male_young" ${seg.voice_type === 'male_young' ? 'selected' : ''}>👦 Piseth (Energetic Boy / Youth +10Hz)</option>
                        <option value="female" ${seg.voice_type === 'female' ? 'selected' : ''}>👩 Sreymom (Lead Heroine)</option>
                        <option value="female_sweet" ${seg.voice_type === 'female_sweet' ? 'selected' : ''}>👧 Sreymom (Sweet Young Girl +10Hz)</option>
                        <option value="female_elder" ${seg.voice_type === 'female_elder' ? 'selected' : ''}>👵 Sreymom (Mature Mother / Elder -7Hz)</option>
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

    if (countEl) countEl.textContent = `${studioSegments.length} Cues`;
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
function studioNudgeTime(index, field, delta) {
    if (!studioSegments[index]) return;
    studioSegments[index][field] = Math.max(0, parseFloat((studioSegments[index][field] + delta).toFixed(2)));
    if (field === 'start' && studioSegments[index].start >= studioSegments[index].end) {
        studioSegments[index].end = studioSegments[index].start + 0.5;
    }
    markStudioDirty();
    renderStudioCueCards();
}

function studioSetTime(index, field, val) {
    const num = parseFloat(val);
    if (!isNaN(num) && studioSegments[index]) {
        studioSegments[index][field] = Math.max(0, parseFloat(num.toFixed(2)));
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
    markStudioDirty();
    renderStudioCueCards();
    showToast(`Shifted cue #${index + 1} to start at ${studioSegments[index].start}s`, "success");
}

// Preview Single Khmer Voice Line via Edge-TTS
async function studioPreviewCueVoice(index) {
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
        const resp = await fetch(`/api/video/job/${studioJobId}/preview-segment`, {
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
            throw new Error(err.detail);
        }

        const data = await resp.json();
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

    const btn = document.getElementById('btn-auto-fix-overlaps');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
        btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Fixing...`;
        btn.disabled = true;
    }

    try {
        // Save current segments first
        await fetch(`/api/video/job/${studioJobId}/segments`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ segments: studioSegments })
        });

        const resp = await fetch(`/api/video/job/${studioJobId}/auto-fix-overlaps`, {
            method: 'POST'
        });

        if (!resp.ok) throw new Error("Failed to auto-fix overlaps");

        const data = await resp.json();
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

    const btn = document.getElementById('btn-studio-rerender');
    const origHtml = btn.innerHTML;
    btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Re-rendering Video...`;
    btn.disabled = true;

    const bgVol = parseFloat(document.getElementById('studio-bg-volume').value || "0.03");
    let audioMode = "cinema_3";
    if (bgVol <= 0.001 && bgVol >= 0) {
        audioMode = "replace";
    } else if (Math.abs(bgVol - 0.03) < 0.01) {
        audioMode = "cinema_3";
    } else if (Math.abs(bgVol - 0.04) < 0.01) {
        audioMode = "cinema_4";
    } else if (Math.abs(bgVol - 0.10) < 0.01) {
        audioMode = "cinema_10";
    } else if (bgVol < 0) {
        audioMode = "smart_cinema";
    } else if (bgVol >= 0.20) {
        audioMode = "constant_25";
    }

    try {
        const tuner = getVoiceTunerValues();
        const removeOriginSound = document.getElementById('remove-origin-sound-checkbox')?.checked ?? (bgVol <= 0.001);
        const maskHardsubs = document.getElementById('mask-hardsubs-checkbox')?.checked ?? true;
        const antiCopyrightVideo = document.getElementById('anti-copyright-video-checkbox')?.checked ?? true;
        const mirrorVideo = document.getElementById('mirror-video-checkbox')?.checked ?? false;
        const burnSubtitles = document.getElementById('burn-subs-checkbox')?.checked ?? false;

        const resp = await fetch(`/api/video/job/${studioJobId}/rerender`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                segments: studioSegments,
                audio_mode: removeOriginSound ? 'replace' : audioMode,
                remove_origin_sound: removeOriginSound,
                bg_volume: removeOriginSound ? 0.0 : bgVol,
                burn_subtitles: burnSubtitles,
                mask_hardsubs: maskHardsubs,
                anti_copyright_video: antiCopyrightVideo,
                mirror_video: mirrorVideo,
                voice_rate: tuner.rate,
                voice_pitch: tuner.pitch,
                voice_volume: tuner.volume
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: "Re-render failed" }));
            throw new Error(err.detail);
        }

        const data = await resp.json();
        const videoPlayer = document.getElementById('dubbed-video-player');
        const dlVideoBtn = document.getElementById('download-dubbed-video-btn');
        const dlSrtBtn = document.getElementById('download-dubbed-srt-btn');

        // Reload video stream with cache-busting timestamp
        const cacheBustUrl = `${data.result.download_url}?t=${Date.now()}`;
        videoPlayer.src = cacheBustUrl;
        videoPlayer.load();
        videoPlayer.play();

        if (currentDownloadJob) {
            currentDownloadJob.result = data.result;
        }

        studioSegments = data.segments;
        studioDirty = false;
        renderStudioCueCards();

        showToast("🎉 Video successfully re-rendered with your custom edits and zero voice collisions!", "success");
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
    const btn = document.getElementById('btn-polish-dialogue');
    const origHtml = btn.innerHTML;
    btn.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Polishing...`;
    btn.disabled = true;

    const settings = getSavedSettings();
    try {
        const resp = await fetch(`/api/video/job/${studioJobId}/ai-polish-dialogue`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_key: settings.geminiKey || null,
                tone: 'casual'
            })
        });
        if (!resp.ok) throw new Error("Polishing dialogue failed");
        const data = await resp.json();
        studioSegments = data.segments;
        markStudioDirty();
        renderStudioCueCards();
        showToast("✨ Dialogue polished! Translations now have natural conversational logic.", "success");
    } catch (e) {
        showToast("Error: " + e.message, "error");
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
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
    const startUrlBtn = document.getElementById('start-dub-btn');
    const startFileBtn = document.getElementById('start-file-dub-btn');

    if (state === 'active' || state === 'processing') {
        if (pipelineCard) pipelineCard.style.display = 'block';
        const resCard = document.getElementById('video-results-card');
        if (resCard) resCard.style.display = 'none';
        if (startUrlBtn) {
            startUrlBtn.disabled = true;
            startUrlBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Dubbing in Progress...';
        }
        if (startFileBtn) {
            startFileBtn.disabled = true;
            startFileBtn.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Dubbing in Progress...';
        }
    } else {
        if (pipelineCard) pipelineCard.style.display = 'none';
        if (startUrlBtn) {
            startUrlBtn.disabled = false;
            startUrlBtn.innerHTML = '<i class="ph ph-film-strip"></i> Start Dubbing Video URL';
        }
        if (startFileBtn) {
            startFileBtn.disabled = false;
            startFileBtn.innerHTML = '<i class="ph ph-film-strip"></i> Start Dubbing Uploaded Video';
        }
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
    try {
        const resp = await fetch(`/api/video/status/${jobId}`);
        if (!resp.ok) {
            showToast("Job not found or expired", "error");
            return;
        }
        const job = await resp.json();
        if (job.status === 'completed') {
            displayCompletedVideo(job);
            showToast(`Loaded CapCut Dubbing Studio for ${job.title || jobId}!`, "success");
        } else if (job.status === 'processing' || job.status === 'queued') {
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
    const jobParam = urlParams.get('job');
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

// ─────────────────────────────────────────────
// Video Splitter & Episode Cutter Handlers
// ─────────────────────────────────────────────
function openStudioSplitModal() {
    const modal = document.getElementById('split-video-modal');
    if (modal) modal.style.display = 'flex';
}

function closeStudioSplitModal() {
    const modal = document.getElementById('split-video-modal');
    if (modal) modal.style.display = 'none';
    const progressBox = document.getElementById('split-progress-box');
    if (progressBox) progressBox.style.display = 'none';
    const btn = document.getElementById('btn-execute-split');
    if (btn) btn.disabled = false;
}

async function executeSplitCurrentJob() {
    const jobId = activeVideoJobId || studioJobId;
    if (!jobId) {
        showToast("Please select or run a video job first before splitting", "warning");
        return;
    }

    const lengthSelect = document.getElementById('modal-split-length');
    const targetSelect = document.getElementById('modal-split-target');
    const splitDur = parseInt(lengthSelect?.value || '3600', 10);
    const target = targetSelect?.value || 'auto';

    const progressBox = document.getElementById('split-progress-box');
    const progressText = document.getElementById('split-progress-text');
    const btn = document.getElementById('btn-execute-split');

    if (progressBox) progressBox.style.display = 'block';
    if (btn) btn.disabled = true;
    if (progressText) progressText.innerHTML = `<i class="ph ph-spinner ph-spin"></i> Splitting video into ${Math.round(splitDur / 60)}m parts with fast stream copy...`;

    try {
        const resp = await fetch(`/api/video/job/${jobId}/split`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                split_duration: splitDur,
                target: target
            })
        });

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({}));
            throw new Error(errData.detail || `Split failed (HTTP ${resp.status})`);
        }

        const data = await resp.json();
        closeStudioSplitModal();
        renderVideoPartsList(data.parts);
        showToast(`🎉 Video successfully split into ${data.total_parts} episodes (${Math.round(splitDur / 60)}m each)!`, "success");

        const partsCard = document.getElementById('video-parts-card');
        if (partsCard) {
            partsCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    } catch (err) {
        console.error("Split error:", err);
        showToast("Error splitting video: " + err.message, "error");
        if (progressBox) progressBox.style.display = 'none';
        if (btn) btn.disabled = false;
    }
}

function renderVideoPartsList(parts) {
    const partsCard = document.getElementById('video-parts-card');
    const partsCount = document.getElementById('video-parts-count');
    const partsList = document.getElementById('video-parts-list');

    if (!partsCard || !partsList || !parts || parts.length === 0) {
        if (partsCard) partsCard.style.display = 'none';
        return;
    }

    partsCard.style.display = 'block';
    const durMinsFirst = parts[0]?.duration ? Math.round(parts[0].duration / 60) : 60;
    if (partsCount) partsCount.textContent = `${parts.length} Parts (~${durMinsFirst}m each)`;

    partsList.innerHTML = '';
    parts.forEach(p => {
        const card = document.createElement('div');
        card.className = 'part-episode-card';
        card.style.cssText = `
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
            transition: all 0.2s ease;
        `;

        const durMins = Math.round(p.duration / 60);
        const sizeStr = p.size_mb > 0 ? `${p.size_mb} MB` : '';

        card.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                <div>
                    <span style="display: inline-block; padding: 2px 8px; border-radius: 4px; background: rgba(56, 189, 248, 0.15); color: #38bdf8; font-size: 11px; font-weight: 700; text-transform: uppercase;">
                        Part ${p.part_number}
                    </span>
                    <h5 style="margin: 6px 0 2px; font-size: 13px; font-weight: 600; color: #fff;">
                        ${p.start_time} - ${p.end_time}
                    </h5>
                    <span style="font-size: 11px; color: var(--text-muted);">
                        ⏱️ ${durMins} mins ${sizeStr ? '• 💾 ' + sizeStr : ''}
                    </span>
                </div>
                <button class="btn-ghost btn-xs" onclick="playPartInPlayer('${p.download_url}', '${p.title}')" title="Play Part in Player" style="padding: 4px 8px; border: 1px solid rgba(255,255,255,0.1);">
                    <i class="ph ph-play" style="color: var(--accent-emerald);"></i> Play
                </button>
            </div>
            <div style="display: flex; gap: 6px; margin-top: auto; border-top: 1px solid rgba(255,255,255,0.06); padding-top: 8px;">
                <a href="${p.download_url}" download="${p.filename}" class="btn-primary btn-xs" style="flex: 1; text-align: center; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; gap: 4px; padding: 6px 10px;">
                    <i class="ph ph-download-simple"></i> MP4
                </a>
                ${p.srt_url ? `
                <a href="${p.srt_url}" download="${p.filename.replace('.mp4', '.srt')}" class="btn-secondary btn-xs" style="text-decoration: none; display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px;" title="Download Khmer Subtitles">
                    <i class="ph ph-file-text"></i> SRT
                </a>` : ''}
            </div>
        `;
        partsList.appendChild(card);
    });
}

function playPartInPlayer(videoUrl, partTitle) {
    const videoPlayer = document.getElementById('dubbed-video-player');
    if (!videoPlayer) return;

    videoPlayer.src = videoUrl;
    videoPlayer.play().catch(() => {});
    showToast(`▶ Playing ${partTitle}`, "info");

    const playerCard = document.querySelector('.player-container');
    if (playerCard) {
        playerCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}
