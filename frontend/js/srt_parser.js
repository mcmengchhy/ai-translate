/**
 * Subtitles (.srt, .vtt) & Document Parser and Batch Translator
 */

let loadedSubtitles = [];
let isBatchRunning = false;
let currentLoadedFileName = "";

function parseSRT(data) {
    // Normalize newlines
    data = data.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    const blocks = data.split('\n\n').filter(b => b.trim());
    const cues = [];

    for (let block of blocks) {
        const lines = block.split('\n').filter(l => l.trim());
        if (lines.length >= 2) {
            let id = lines[0].trim();
            let timestampLine = lines[1];
            let textLines = lines.slice(2).join('\n');

            // Check if line 0 is already the timestamp (e.g. in some VTT files)
            if (id.includes('-->')) {
                timestampLine = id;
                id = String(cues.length + 1);
                textLines = lines.slice(1).join('\n');
            }

            if (timestampLine.includes('-->')) {
                cues.push({
                    id: id,
                    timestamp: timestampLine.trim(),
                    text: textLines.trim(),
                    translation: "",
                    status: "pending"
                });
            }
        }
    }
    return cues;
}

function renderSubtitlesTable() {
    const tbody = document.getElementById('subtitles-tbody');
    tbody.innerHTML = '';

    loadedSubtitles.forEach((cue, index) => {
        const tr = document.createElement('tr');
        tr.id = `cue-row-${index}`;
        tr.innerHTML = `
            <td>${cue.id}</td>
            <td class="timestamp-cell">${escapeHtml(cue.timestamp)}</td>
            <td>${escapeHtml(cue.text)}</td>
            <td class="khmer-cell" id="cue-trans-${index}">${escapeHtml(cue.translation || '—')}</td>
            <td><span class="status-tag ${cue.status}" id="cue-status-${index}">${cue.status}</span></td>
        `;
        tbody.appendChild(tr);
    });

    document.getElementById('subtitle-count').textContent = `${loadedSubtitles.length} cues`;
    document.getElementById('subtitles-workspace').style.display = 'block';
}

async function handleFileUpload(file) {
    if (!file) return;
    currentLoadedFileName = file.name;
    document.getElementById('current-filename').textContent = file.name;

    const reader = new FileReader();
    reader.onload = async (e) => {
        const content = e.target.result;
        if (file.name.endsWith('.srt') || file.name.endsWith('.vtt')) {
            loadedSubtitles = parseSRT(content);
            if (loadedSubtitles.length === 0) {
                showToast("Could not parse subtitle cues. Check file format.", "error");
                return;
            }
            renderSubtitlesTable();
            showToast(`Loaded ${loadedSubtitles.length} subtitle cues!`, "success");
        } else if (file.name.endsWith('.json')) {
            try {
                const parsed = JSON.parse(content);
                let count = 0;
                loadedSubtitles = [];
                for (let key in parsed) {
                    count++;
                    loadedSubtitles.push({
                        id: count,
                        timestamp: `Key: ${key}`,
                        text: String(parsed[key]),
                        translation: "",
                        status: "pending"
                    });
                }
                renderSubtitlesTable();
                showToast(`Loaded ${count} JSON entries!`, "success");
            } catch (err) {
                showToast("Invalid JSON file", "error");
            }
        } else {
            // Plain text or markdown: split by lines/paragraphs
            const lines = content.split('\n\n').filter(l => l.trim());
            loadedSubtitles = lines.map((line, idx) => ({
                id: idx + 1,
                timestamp: `Paragraph ${idx + 1}`,
                text: line.trim(),
                translation: "",
                status: "pending"
            }));
            renderSubtitlesTable();
            showToast(`Loaded ${loadedSubtitles.length} paragraphs!`, "success");
        }
    };
    reader.readAsText(file);
}

// Start Batch Translation of Subtitles
async function startBatchTranslate() {
    if (isBatchRunning) return;
    if (loadedSubtitles.length === 0) {
        showToast("No file loaded!", "error");
        return;
    }

    isBatchRunning = true;
    const startBtn = document.getElementById('start-batch-translate-btn');
    startBtn.disabled = true;
    startBtn.innerHTML = '<i class="ph ph-spinner"></i> Translating...';

    const progressBarWrap = document.getElementById('batch-progress-bar-wrap');
    const progressFill = document.getElementById('batch-progress-fill');
    progressBarWrap.style.display = 'block';

    const sourceLang = document.getElementById('source-lang-select').value;
    const targetLang = document.getElementById('target-lang-select').value;
    const tone = getActiveTone();
    const settings = getSavedSettings();

    let completed = 0;
    const total = loadedSubtitles.length;

    for (let i = 0; i < total; i++) {
        if (!isBatchRunning) break;
        const cue = loadedSubtitles[i];

        if (!cue.text.trim()) {
            cue.translation = "";
            cue.status = "done";
            completed++;
            continue;
        }

        try {
            const resp = await fetch('/api/translate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: cue.text,
                    source_lang: sourceLang,
                    target_lang: targetLang,
                    engine: settings.engine || 'auto',
                    tone: tone,
                    api_key: settings.geminiKey || null,
                    model: settings.geminiModel || null
                })
            });

            if (resp.ok) {
                const data = await resp.json();
                cue.translation = data.translated_text;
                cue.status = "done";
            } else {
                cue.translation = "[Translation Error]";
                cue.status = "error";
            }
        } catch (err) {
            cue.translation = "[Network Error]";
            cue.status = "error";
        }

        // Update DOM row
        const transCell = document.getElementById(`cue-trans-${i}`);
        const statusCell = document.getElementById(`cue-status-${i}`);
        if (transCell) transCell.textContent = cue.translation;
        if (statusCell) {
            statusCell.className = `status-tag ${cue.status}`;
            statusCell.textContent = cue.status;
        }

        completed++;
        const percent = Math.round((completed / total) * 100);
        progressFill.style.width = `${percent}%`;

        // Small interval pause to avoid browser lag
        await new Promise(r => setTimeout(r, 60));
    }

    isBatchRunning = false;
    startBtn.disabled = false;
    startBtn.innerHTML = '<i class="ph ph-check"></i> Finished';
    document.getElementById('export-srt-btn').disabled = false;
    showToast("Batch translation completed!", "success");
}

function exportSubtitles() {
    if (loadedSubtitles.length === 0) return;

    let srtContent = "";
    for (let cue of loadedSubtitles) {
        srtContent += `${cue.id}\n${cue.timestamp}\n${cue.translation || cue.text}\n\n`;
    }

    const blob = new Blob([srtContent], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = currentLoadedFileName ? `km_${currentLoadedFileName}` : "translated_subtitles.srt";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast("Exported translated subtitle file!", "success");
}

function clearBatch() {
    loadedSubtitles = [];
    currentLoadedFileName = "";
    document.getElementById('subtitles-workspace').style.display = 'none';
    document.getElementById('file-input').value = "";
    document.getElementById('export-srt-btn').disabled = true;
    document.getElementById('batch-progress-bar-wrap').style.display = 'none';
}
