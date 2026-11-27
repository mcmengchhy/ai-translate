'use strict';

const reuseJobs = new Map();
const reuseSelected = new Set();
let reuseExportsTimer = null;
let reuseLoadingExports = false;
let reuseExportsRefreshPending = false;
let reuseRendering = false;
let reuseExporting = false;
const reuseElement = id => document.getElementById(id);

function reuseMessage(text, kind = 'info') {
    const element = reuseElement('message');
    element.textContent = text;
    element.dataset.kind = kind;
}

async function reuseRequest(url, options) {
    const response = await fetch(url, options);
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('The server returned an unreadable response.'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.');
    return data;
}

function reuseSelectionChanged() {
    reuseElement('selection-count').textContent = `${reuseSelected.size} selected`;
    reuseElement('export-selected').disabled = reuseExporting || !reuseSelected.size;
    const ready = [...reuseJobs.values()].filter(job => job.export_ready);
    const all = reuseElement('select-ready');
    all.checked = ready.length > 0 && ready.every(job => reuseSelected.has(job.id));
    all.indeterminate = reuseSelected.size > 0 && !all.checked;
    all.disabled = !ready.length;
}

function reuseRenderJobs() {
    const list = reuseElement('job-list');
    const dropdown = reuseElement('replacement-job');
    const chosen = dropdown.value;
    list.replaceChildren();
    dropdown.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Choose a ready job';
    dropdown.appendChild(placeholder);
    for (const job of reuseJobs.values()) {
        const row = document.createElement('div');
        row.className = 'job';
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = reuseSelected.has(job.id);
        check.disabled = !job.export_ready;
        check.setAttribute('aria-label', `Select ${job.title}`);
        check.onchange = () => {
            if (check.checked) reuseSelected.add(job.id); else reuseSelected.delete(job.id);
            reuseSelectionChanged();
        };
        const info = document.createElement('div');
        info.className = 'job-info';
        const title = document.createElement('strong');
        title.className = 'job-title';
        title.textContent = job.title;
        const detail = document.createElement('span');
        detail.className = 'job-detail' + (job.export_ready ? ' ready' : '');
        detail.textContent = job.export_ready ? `Ready · ${job.ready_voices} Khmer voices · ${job.id}` :
            `${job.status || 'Saved'} · ${job.ready_voices}/${job.voice_cues} voices ready · ${job.id}`;
        info.append(title, detail);
        const open = document.createElement('a');
        open.href = `/?job=${encodeURIComponent(job.id)}`;
        open.textContent = 'Open job';
        row.append(check, info, open);
        list.appendChild(row);
        if (job.export_ready) {
            const option = document.createElement('option');
            option.value = job.id;
            option.textContent = `${job.title} · ${job.id}`;
            dropdown.appendChild(option);
        }
    }
    if (!reuseJobs.size) {
        const empty = document.createElement('p');
        empty.textContent = 'No saved jobs yet. Generate Khmer voices in the editor first.';
        list.appendChild(empty);
    }
    dropdown.value = reuseJobs.get(chosen)?.export_ready ? chosen : '';
    reuseElement('render-replacement').disabled = reuseRendering || ![...reuseJobs.values()].some(job => job.export_ready);
    reuseSelectionChanged();
}

async function reuseLoadJobs() {
    const button = reuseElement('refresh-jobs');
    button.disabled = true;
    try {
        const data = await reuseRequest('/api/video/export-jobs');
        reuseJobs.clear();
        for (const job of data.jobs) reuseJobs.set(job.id, job);
        for (const id of reuseSelected) if (!reuseJobs.get(id)?.export_ready) reuseSelected.delete(id);
        reuseRenderJobs();
    } catch (error) { reuseMessage(error.message, 'error'); }
    finally { button.disabled = false; }
}

function reuseDownloadLink(text, href, className = '') {
    const link = document.createElement('a');
    link.textContent = text;
    link.href = href;
    link.download = '';
    link.className = className;
    return link;
}

function reuseRenderExports(exports) {
    const list = reuseElement('export-list');
    list.replaceChildren();
    for (const item of exports) {
        const row = document.createElement('article');
        row.className = 'export';
        const title = document.createElement('strong');
        title.textContent = `${item.kind === 'clip-edit' ? 'Edited clip' : item.kind === 'replacement' ? 'Replacement video' : 'Khmer bundle'} · ${item.jobs.length} job${item.jobs.length === 1 ? '' : 's'}`;
        const stage = document.createElement('p');
        stage.className = 'export-detail' + (item.status === 'failed' ? ' error' : '');
        stage.textContent = item.error || item.stage;
        row.append(title, stage);
        const base = `/api/video/exports/${encodeURIComponent(item.id)}`;
        if (item.status === 'completed') {
            row.appendChild(reuseDownloadLink('Download bundle (.zip)', `${base}/download`, 'button'));
            const files = document.createElement('div');
            files.className = 'export-files';
            item.jobs.forEach((job, index) => {
                const entry = document.createElement('div');
                const name = document.createElement('p');
                name.textContent = `${job.title} · ${Math.round(job.duration)} seconds`;
                entry.appendChild(name);
                if (item.kind !== 'clip-edit') entry.append(
                    reuseDownloadLink('Khmer audio (.wav)', `${base}/files/${index}/audio`),
                    reuseDownloadLink('Subtitles (.srt)', `${base}/files/${index}/subtitles`));
                if (item.kind === 'replacement') entry.appendChild(reuseDownloadLink('Replacement video (.mp4)', `${base}/files/${index}/video`));
                if (item.kind === 'clip-edit') entry.appendChild(reuseDownloadLink('Edited clip (.mp4)', `${base}/files/${index}/video`));
                files.appendChild(entry);
            });
            row.appendChild(files);
        } else if (['queued', 'processing'].includes(item.status)) {
            const progress = document.createElement('progress');
            progress.max = 100;
            progress.value = item.progress;
            progress.setAttribute('aria-label', 'Export progress');
            row.appendChild(progress);
        }
        list.appendChild(row);
    }
    if (!exports.length) {
        const empty = document.createElement('p');
        empty.textContent = 'No exports yet.';
        list.appendChild(empty);
    }
}

async function reuseLoadExports() {
    if (reuseLoadingExports) { reuseExportsRefreshPending = true; return; }
    reuseLoadingExports = true;
    clearTimeout(reuseExportsTimer);
    reuseElement('refresh-exports').disabled = true;
    try {
        const data = await reuseRequest('/api/video/exports');
        reuseRenderExports(data.exports);
        if (data.exports.some(item => ['queued', 'processing'].includes(item.status))) {
            reuseExportsTimer = setTimeout(reuseLoadExports, 2000);
        }
    } catch (error) { reuseMessage(error.message, 'error'); }
    finally {
        reuseLoadingExports = false;
        reuseElement('refresh-exports').disabled = false;
        if (reuseExportsRefreshPending) {
            reuseExportsRefreshPending = false;
            clearTimeout(reuseExportsTimer);
            reuseExportsTimer = setTimeout(reuseLoadExports, 0);
        }
    }
}

async function reuseExportSelected() {
    if (reuseExporting || !reuseSelected.size) return;
    if (reuseSelected.size > 100) { reuseMessage('Select up to 100 jobs per bundle.', 'error'); return; }
    reuseExporting = true;
    reuseSelectionChanged();
    try {
        await reuseRequest('/api/video/exports', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ job_ids: [...reuseSelected] }) });
        reuseMessage('Export queued. Your downloads will appear below.');
        await reuseLoadExports();
    } catch (error) { reuseMessage(error.message, 'error'); }
    finally { reuseExporting = false; reuseSelectionChanged(); }
}

async function reuseRenderReplacement(event) {
    event.preventDefault();
    if (reuseRendering) return;
    const id = reuseElement('replacement-job').value;
    const file = reuseElement('replacement-file').files[0];
    if (!id || !file) { reuseMessage('Choose a ready job and a replacement video.', 'error'); return; }
    reuseRendering = true;
    const button = reuseElement('render-replacement');
    button.disabled = true;
    button.textContent = 'Uploading replacement…';
    try {
        const form = new FormData();
        form.append('file', file);
        form.append('ending', reuseElement('replacement-ending').value);
        form.append('burn_subtitles', String(reuseElement('replacement-captions').checked));
        await reuseRequest(`/api/video/job/${encodeURIComponent(id)}/replacement-video`, { method: 'POST', body: form });
        reuseMessage('Replacement render queued. Your original job and video are kept.');
        await reuseLoadExports();
    } catch (error) { reuseMessage(error.message, 'error'); }
    finally { reuseRendering = false; button.disabled = false; button.textContent = 'Render replacement video'; }
}

document.addEventListener('DOMContentLoaded', async () => {
    reuseElement('refresh-jobs').onclick = reuseLoadJobs;
    reuseElement('refresh-exports').onclick = reuseLoadExports;
    reuseElement('export-selected').onclick = reuseExportSelected;
    reuseElement('replacement-form').onsubmit = reuseRenderReplacement;
    reuseElement('select-ready').onchange = event => {
        reuseSelected.clear();
        if (event.target.checked) for (const job of reuseJobs.values()) if (job.export_ready) reuseSelected.add(job.id);
        reuseRenderJobs();
    };
    await Promise.allSettled([reuseLoadJobs(), reuseLoadExports()]);
    const requested = new URLSearchParams(window.location.search).get('job');
    if (reuseJobs.get(requested)?.export_ready) {
        reuseSelected.add(requested);
        reuseElement('replacement-job').value = requested;
        reuseRenderJobs();
    }
});
