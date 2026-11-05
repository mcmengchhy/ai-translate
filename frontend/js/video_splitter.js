/**
 * Video Splitter & Clip Cutter Frontend Controller
 * Handles drag-and-drop video upload, duration calculation,
 * exact-minute clip slicing with FFmpeg, and automatic ZIP download.
 */

(function () {
    let selectedSplitFile = null;
    let selectedVideoDuration = 0; // in seconds
    let uploadStartTime = null;

    // DOM Elements cache
    let dropzone, fileInput, minutesInput, autoZipCheck, fileInfoBox;
    let fileNameEl, fileSizeEl, fileDurEl, calcSummaryEl, breakdownListEl;
    let actionBtn, progressCard, progressBar, progressPercentEl, progressStatusEl, progressSpeedEl;
    let resultsCard, zipDownloadBtn, zipSummaryEl, partsGridEl;

    function initSplitter() {
        dropzone = document.getElementById('splitter-dropzone');
        fileInput = document.getElementById('splitter-file-input');
        minutesInput = document.getElementById('splitter-minutes-input');
        autoZipCheck = document.getElementById('splitter-autozip-check');
        fileInfoBox = document.getElementById('splitter-file-info');
        fileNameEl = document.getElementById('splitter-filename');
        fileSizeEl = document.getElementById('splitter-filesize');
        fileDurEl = document.getElementById('splitter-fileduration');
        calcSummaryEl = document.getElementById('splitter-calc-summary');
        breakdownListEl = document.getElementById('splitter-breakdown-list');
        actionBtn = document.getElementById('splitter-action-btn');
        progressCard = document.getElementById('splitter-progress-card');
        progressBar = document.getElementById('splitter-progress-bar');
        progressPercentEl = document.getElementById('splitter-progress-percent');
        progressStatusEl = document.getElementById('splitter-progress-status');
        progressSpeedEl = document.getElementById('splitter-progress-speed');
        resultsCard = document.getElementById('splitter-results-card');
        zipDownloadBtn = document.getElementById('splitter-zip-download-btn');
        zipSummaryEl = document.getElementById('splitter-zip-summary');
        partsGridEl = document.getElementById('splitter-parts-grid');

        if (!dropzone || !fileInput) return;

        // Click to browse
        dropzone.addEventListener('click', (e) => {
            if (e.target.closest('#splitter-clear-btn')) return;
            fileInput.click();
        });

        // File selection
        fileInput.addEventListener('change', (e) => {
            if (e.target.files && e.target.files[0]) {
                handleFileSelected(e.target.files[0]);
            }
        });

        // Drag and drop events
        ['dragenter', 'dragover'].forEach(name => {
            dropzone.addEventListener(name, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.add('drag-active');
            });
        });

        ['dragleave', 'drop'].forEach(name => {
            dropzone.addEventListener(name, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.remove('drag-active');
            });
        });

        dropzone.addEventListener('drop', (e) => {
            const dt = e.dataTransfer;
            if (dt && dt.files && dt.files.length > 0) {
                handleFileSelected(dt.files[0]);
            }
        });

        // Minutes input change
        if (minutesInput) {
            minutesInput.addEventListener('input', () => {
                updateSplitCalculation();
            });
        }

        // Preset chips
        document.querySelectorAll('.splitter-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.querySelectorAll('.splitter-chip').forEach(c => c.classList.remove('active'));
                chip.classList.add('active');
                const mins = parseFloat(chip.getAttribute('data-mins'));
                if (minutesInput) {
                    minutesInput.value = mins;
                    updateSplitCalculation();
                }
            });
        });

        // Action button
        if (actionBtn) {
            actionBtn.addEventListener('click', startSplittingProcess);
        }

        // Clear file button
        const clearBtn = document.getElementById('splitter-clear-btn');
        if (clearBtn) {
            clearBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                clearSelectedFile();
            });
        }

        // Reset / Split another button
        const resetBtn = document.getElementById('splitter-reset-btn');
        if (resetBtn) {
            resetBtn.addEventListener('click', resetSplitterView);
        }
    }

    function formatBytes(bytes) {
        if (!bytes || bytes === 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    }

    function formatTime(seconds) {
        seconds = Math.max(0, Math.floor(seconds || 0));
        const hrs = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;
        if (hrs > 0) {
            return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }

    function handleFileSelected(file) {
        // Basic check for video file
        const ext = file.name.split('.').pop().toLowerCase();
        const validExts = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'flv', 'ts', 'm4v', '3gp'];
        if (!file.type.startsWith('video/') && !validExts.includes(ext)) {
            alert('Please select a valid video file (MP4, MKV, MOV, WEBM, FLV, AVI, etc.)');
            return;
        }

        selectedSplitFile = file;
        fileNameEl.textContent = file.name;
        fileSizeEl.textContent = formatBytes(file.size);
        fileDurEl.textContent = 'Analyzing duration...';

        // Show file info card, hide empty state
        fileInfoBox.style.display = 'block';
        const emptyState = document.getElementById('splitter-empty-state');
        if (emptyState) emptyState.style.display = 'none';

        // Enable action button
        actionBtn.disabled = false;

        // Instant duration detection using HTML5 Video
        const v = document.createElement('video');
        v.preload = 'metadata';
        const blobUrl = URL.createObjectURL(file);
        v.onloadedmetadata = function () {
            URL.revokeObjectURL(blobUrl);
            selectedVideoDuration = v.duration;
            fileDurEl.textContent = formatTime(selectedVideoDuration);
            updateSplitCalculation();
        };
        v.onerror = function () {
            URL.revokeObjectURL(blobUrl);
            fileDurEl.textContent = 'Ready to split';
            selectedVideoDuration = 0;
            updateSplitCalculation();
        };
        v.src = blobUrl;
    }

    function clearSelectedFile() {
        selectedSplitFile = null;
        selectedVideoDuration = 0;
        if (fileInput) fileInput.value = '';
        fileInfoBox.style.display = 'none';
        const emptyState = document.getElementById('splitter-empty-state');
        if (emptyState) emptyState.style.display = 'flex';
        actionBtn.disabled = true;
        calcSummaryEl.innerHTML = '';
        breakdownListEl.innerHTML = '';
        breakdownListEl.style.display = 'none';
    }

    function updateSplitCalculation() {
        if (!selectedSplitFile) {
            calcSummaryEl.innerHTML = '';
            breakdownListEl.innerHTML = '';
            return;
        }

        const mins = parseFloat(minutesInput ? minutesInput.value : 30) || 30;
        const clipSecs = mins * 60;

        if (selectedVideoDuration > 0) {
            const count = Math.ceil(selectedVideoDuration / clipSecs);
            calcSummaryEl.innerHTML = `
                <div class="calc-badge">
                    <i class="ph ph-scissors"></i>
                    <span>At <strong>${mins} minutes</strong> per clip, this video will produce <strong>${count} clips</strong></span>
                </div>
            `;

            // Build preview schedule
            let breakdownHtml = '<div class="split-breakdown-grid">';
            let cur = 0;
            for (let i = 1; i <= count; i++) {
                let end = Math.min(cur + clipSecs, selectedVideoDuration);
                let dur = end - cur;
                breakdownHtml += `
                    <div class="split-preview-item">
                        <span class="preview-part-num">Part ${i}</span>
                        <span class="preview-part-range">${formatTime(cur)} → ${formatTime(end)}</span>
                        <span class="preview-part-dur">${Math.round(dur / 60)} min</span>
                    </div>
                `;
                cur = end;
            }
            breakdownHtml += '</div>';
            breakdownListEl.innerHTML = breakdownHtml;
            breakdownListEl.style.display = 'block';
        } else {
            calcSummaryEl.innerHTML = `
                <div class="calc-badge">
                    <i class="ph ph-scissors"></i>
                    <span>Will be split into consecutive clips of <strong>${mins} minutes</strong> each.</span>
                </div>
            `;
            breakdownListEl.style.display = 'none';
        }
    }

    function startSplittingProcess() {
        if (!selectedSplitFile) return;

        const mins = parseFloat(minutesInput ? minutesInput.value : 30) || 30;

        // Switch to progress view
        actionBtn.disabled = true;
        progressCard.style.display = 'block';
        resultsCard.style.display = 'none';
        progressBar.style.width = '0%';
        progressPercentEl.textContent = '0%';
        progressStatusEl.innerHTML = '<i class="ph ph-upload-simple"></i> Uploading video to server...';
        progressSpeedEl.textContent = 'Calculating speed...';

        uploadStartTime = Date.now();

        const formData = new FormData();
        formData.append('file', selectedSplitFile);
        formData.append('clip_minutes', mins);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/video/split-video');

        xhr.upload.onprogress = function (e) {
            if (e.lengthComputable) {
                const percent = Math.round((e.loaded / e.total) * 92); // 0% to 92% for upload
                progressBar.style.width = percent + '%';
                progressPercentEl.textContent = percent + '%';

                const elapsed = (Date.now() - uploadStartTime) / 1000;
                if (elapsed > 0.5) {
                    const speed = e.loaded / elapsed;
                    const remainingBytes = e.total - e.loaded;
                    const eta = speed > 0 ? Math.round(remainingBytes / speed) : 0;
                    progressSpeedEl.textContent = `${formatBytes(speed)}/s • ETA: ${eta}s`;
                }

                if (percent >= 90) {
                    progressStatusEl.innerHTML = '<i class="ph ph-scissors ph-spin"></i> Upload complete! Splitting video with FFmpeg stream-copy (fast & lossless)...';
                }
            }
        };

        xhr.onload = function () {
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const res = JSON.parse(xhr.responseText);
                    progressBar.style.width = '100%';
                    progressPercentEl.textContent = '100%';
                    progressStatusEl.innerHTML = '<i class="ph ph-check-circle" style="color: var(--accent-emerald);"></i> Slicing completed & ZIP archive ready!';
                    progressSpeedEl.textContent = 'Complete';

                    setTimeout(() => {
                        handleSplitSuccess(res);
                    }, 500);
                } catch (err) {
                    handleSplitError('Invalid server response: ' + err.message);
                }
            } else {
                let errMsg = 'Splitting failed (' + xhr.status + ')';
                try {
                    const json = JSON.parse(xhr.responseText);
                    if (json.detail) errMsg = json.detail;
                } catch (_) {}
                handleSplitError(errMsg);
            }
        };

        xhr.onerror = function () {
            handleSplitError('Network error occurred while uploading. Please check server connection.');
        };

        xhr.send(formData);
    }

    function handleSplitSuccess(data) {
        progressCard.style.display = 'none';
        resultsCard.style.display = 'block';

        // Update ZIP button
        zipDownloadBtn.href = data.zip_download_url;
        zipDownloadBtn.setAttribute('download', data.zip_filename);
        zipSummaryEl.textContent = `${data.total_parts} clips (~${data.clip_minutes} mins each) • Archive size: ${data.zip_size_mb} MB`;

        // Render individual parts grid
        partsGridEl.innerHTML = '';
        if (data.parts && data.parts.length > 0) {
            data.parts.forEach(part => {
                const item = document.createElement('div');
                item.className = 'splitter-part-card';
                item.innerHTML = `
                    <div class="part-card-header">
                        <span class="part-badge">Part ${part.part_index}</span>
                        <span class="part-size">${part.size_mb} MB</span>
                    </div>
                    <div class="part-card-title" title="${part.filename}">${part.filename}</div>
                    <div class="part-card-meta">
                        <span><i class="ph ph-clock"></i> ${part.start_time_formatted} → ${part.end_time_formatted}</span>
                        <span><i class="ph ph-timer"></i> ${part.duration_formatted}</span>
                    </div>
                    <div class="part-card-actions">
                        <a href="${part.download_url}" download="${part.filename}" class="btn-part-dl">
                            <i class="ph ph-download-simple"></i> Download Clip
                        </a>
                        <button type="button" class="btn-part-play" onclick="window.previewSplitClip('${part.download_url}', '${part.filename}')">
                            <i class="ph ph-play"></i>
                        </button>
                    </div>
                `;
                partsGridEl.appendChild(item);
            });
        }

        // Auto download ZIP if checked
        if (autoZipCheck && autoZipCheck.checked) {
            const tempLink = document.createElement('a');
            tempLink.href = data.zip_download_url;
            tempLink.download = data.zip_filename;
            document.body.appendChild(tempLink);
            tempLink.click();
            document.body.removeChild(tempLink);
        }
    }

    function handleSplitError(msg) {
        progressCard.style.display = 'none';
        actionBtn.disabled = false;
        alert('Split Error: ' + msg);
    }

    function resetSplitterView() {
        resultsCard.style.display = 'none';
        progressCard.style.display = 'none';
        clearSelectedFile();
    }

    // Clip preview helper
    window.previewSplitClip = function (url, title) {
        let modal = document.getElementById('splitter-preview-modal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'splitter-preview-modal';
            modal.className = 'modal-backdrop';
            modal.innerHTML = `
                <div class="modal-card" style="max-width: 720px; width: 90%;">
                    <div class="modal-header">
                        <h3 id="splitter-preview-title" style="font-size: 16px; margin: 0;">Clip Preview</h3>
                        <button class="icon-button" onclick="document.getElementById('splitter-preview-modal').classList.remove('active')"><i class="ph ph-x"></i></button>
                    </div>
                    <div class="modal-body" style="padding: 16px; text-align: center;">
                        <video id="splitter-preview-player" controls autoplay style="width: 100%; border-radius: 8px; background: #000; max-height: 480px;"></video>
                    </div>
                </div>
            `;
            document.body.appendChild(modal);
        }
        document.getElementById('splitter-preview-title').textContent = title;
        const player = document.getElementById('splitter-preview-player');
        player.src = url;
        modal.classList.add('active');
    };

    // Auto-init on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initSplitter);
    } else {
        initSplitter();
    }
})();

