/* Manual translation only: this page never sends dialogue to a translation provider. */
let processingJobId = null;
let processingDetails = null;
let processingBatch = 0;
let processingGeneration = 0;
let processingBusy = false;
let processingLocateMissing = false;
const processingDrafts = new Map();
const processingDraftCueIds = new Map();
const processingValidationEnabled = new Set();
const processingValidationErrors = new Map();
let missingTranslationBoard = null;
let missingBoardGeneration = 0;
const missingBoardDrafts = new Map();

const processingElement = id => document.getElementById(id);

function processingMessage(text, kind = 'info') {
    const element = processingElement('message');
    element.textContent = text;
    element.dataset.kind = kind;
}

function processingDraftKey(details) {
    return `manual_translation:${processingJobId}:${details.source_fingerprint}:${details.batch}`;
}

function saveProcessingDraft() {
    if (!processingDetails) return;
    const key = processingDraftKey(processingDetails);
    const value = processingElement('khmer-text').value;
    processingDrafts.set(key, value);
    processingValidationEnabled.add(key);
    processingValidationErrors.delete(key);
    try { sessionStorage.setItem(key, value); } catch {}
    processingElement('draft-status').textContent = 'Unsaved · kept in this tab';
    renderProcessingValidation();
}

async function processingFetch(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) {
        const error = new Error(typeof data.detail === 'string' ? data.detail : 'Could not update this task.');
        error.status = response.status;
        throw error;
    }
    return data;
}

async function processingRequest(path, body, method = 'POST') {
    const options = body === undefined ? {} : {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    };
    return processingFetch(`/api/video/job/${encodeURIComponent(processingJobId)}/${path}`, options);
}

function formatProcessingTime(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
}

function numberedKhmerEntries(text) {
    const source = text.replace(/^\uFEFF/u, '');
    const markers = [...source.matchAll(/\[\s*([0-9០-៩]+)\s*\]|【\s*([0-9០-៩]+)\s*】|^[ \t]*([0-9០-៩]+)[.)។៖:：、][ \t]*/gmu)];
    const entries = markers.map((marker, index) => {
        const digits = (marker[1] ?? marker[2] ?? marker[3]).replace(/[០-៩]/gu, digit => String(digit.charCodeAt(0) - 0x17e0));
        const end = index + 1 < markers.length ? markers[index + 1].index : source.length;
        return { id: Number(digits), words: source.slice(marker.index + marker[0].length, end).trim().replace(/\s+/gu, ' ') };
    });
    return { source, markers, entries };
}

function formatNumberedKhmer(text, cues) {
    const { source, markers, entries } = numberedKhmerEntries(text);
    if (!markers.length || source.slice(0, markers[0].index).trim()) {
        throw new Error('Keep a numbered marker such as [12] before each translated sentence.');
    }
    const expected = new Set(cues.map(cue => cue.id));
    const translations = new Map();
    for (const { id, words } of entries) {
        if (translations.has(id)) throw new Error(`Cue [${id}] appears more than once. Your pasted text is unchanged.`);
        if (!expected.has(id)) throw new Error(`Cue [${id}] does not belong to this batch. Your pasted text is unchanged.`);
        if (!words) throw new Error(`Cue [${id}] has no translation. Your pasted text is unchanged.`);
        if (!/[\p{L}\p{N}]/u.test(words)) throw new Error(`Cue [${id}] contains only punctuation. Paste Khmer words instead of "...". Your pasted text is unchanged.`);
        translations.set(id, words);
    }
    const missing = cues.filter(cue => !translations.has(cue.id)).map(cue => cue.id);
    return {
        text: cues.filter(cue => translations.has(cue.id)).map(cue => `[${cue.id}] ${translations.get(cue.id)}`).join('\n'),
        count: translations.size, missing,
    };
}

function missingBoardKey(board) {
    return `manual_missing:${processingJobId}:${board.source_fingerprint}:${board.cues.map(cue=>cue.id).join(',')}`;
}

function missingBoardMessage(text, kind = 'info') {
    const element = processingElement('missing-board-message');
    element.textContent = text;
    element.dataset.kind = kind;
}

function saveMissingBoardDraft() {
    if (!missingTranslationBoard) return;
    const key = missingBoardKey(missingTranslationBoard);
    const text = processingElement('missing-board-input').value;
    missingBoardDrafts.set(key,text);
    try { sessionStorage.setItem(key,text); } catch {}
}

function renderMissingTranslationBoard(board) {
    const panel = processingElement('missing-board');
    panel.hidden = !board;
    if (!board) return;
    const count = board.remaining_cues;
    processingElement('missing-board-count').textContent = `${count} missing cue${count===1 ? '' : 's'} · all batches`;
    processingElement('missing-board-grid').hidden = !count;
    processingElement('missing-board-empty').hidden = !!count;
    processingElement('missing-board-source').value = board.source_text;
    let draft = missingBoardDrafts.get(missingBoardKey(board));
    if (draft === undefined) {
        try { draft = sessionStorage.getItem(missingBoardKey(board)); } catch {}
    }
    processingElement('missing-board-input').value = draft ?? '';
    processingElement('missing-board-input').disabled = processingBusy || !board.can_edit;
    processingElement('copy-missing-cues').disabled = processingBusy || !count;
    processingElement('format-missing-cues').disabled = processingBusy || !board.can_edit || !count;
    processingElement('save-missing-cues').disabled = processingBusy || !board.can_edit || !count;
}

function legacyMissingTranslationBoard(job, fingerprint) {
    const cues = (job.segments || []).filter(cue => (cue.text || '').trim() &&
        (!/[\p{L}\p{N}]/u.test(cue.translation || '') || cue.translation_error));
    return {legacy:true,source_fingerprint:fingerprint,
        missing_fingerprint:JSON.stringify(cues.map(cue=>[cue.id,cue.start,cue.end,cue.text,cue.translation,cue.translation_error])),
        remaining_cues:cues.length,cues,source_text:cues.map(cue=>`[${cue.id}] ${cue.text.trim().replace(/\s+/gu,' ')}`).join('\n'),
        can_edit:!['queued','processing','running','rendering','pausing'].includes(job.status)};
}

async function loadMissingTranslationBoard(parentGeneration = processingGeneration) {
    const generation = ++missingBoardGeneration;
    try {
        let board;
        try { board = await processingRequest('missing-translations'); }
        catch (error) {
            if (error.status !== 404 && error.status !== 405) throw error;
            // Keep the board usable while older workers finish their videos.
            const job = await processingFetch(`/api/video/status/${encodeURIComponent(processingJobId)}`);
            board = legacyMissingTranslationBoard(job,processingDetails.source_fingerprint);
        }
        if (generation !== missingBoardGeneration || parentGeneration !== processingGeneration) return;
        if (!board.missing_fingerprint || !Array.isArray(board.cues)) return;
        missingTranslationBoard = board;
        renderMissingTranslationBoard(board);
    } catch (error) {
        if (generation === missingBoardGeneration && parentGeneration === processingGeneration) missingBoardMessage(error.message,'error');
    }
}

function formatMissingBoardTranslation() {
    if (processingBusy || !missingTranslationBoard?.can_edit) return null;
    try {
        const formatted = formatNumberedKhmer(processingElement('missing-board-input').value,missingTranslationBoard.cues);
        for (const entry of numberedKhmerEntries(formatted.text).entries) {
            if (/[\u3400-\u9fff]/u.test(entry.words)) throw new Error(`Cue [${entry.id}] still contains Chinese text. Paste its Khmer translation instead.`);
        }
        processingElement('missing-board-input').value = formatted.text;
        saveMissingBoardDraft();
        const missing = formatted.missing.map(id=>`[${id}]`).join(', ');
        missingBoardMessage(missing ? `Still missing Khmer text for ${missing}. Your paste is kept.` :
            `${formatted.count} corrections checked. Select Update missing translations to save them.`,missing ? 'error' : 'success');
        return formatted;
    } catch (error) { missingBoardMessage(error.message,'error'); return null; }
}

async function saveLegacyMissingTranslations(board,text) {
    const metadata = await processingRequest(`manual-translation?batch=${processingBatch}`);
    const job = await processingFetch(`/api/video/status/${encodeURIComponent(processingJobId)}`);
    const current = legacyMissingTranslationBoard(job,metadata.source_fingerprint);
    if (!current.can_edit) throw new Error('Pause this task before updating missing translations.');
    if (metadata.source_fingerprint !== board.source_fingerprint || current.missing_fingerprint !== board.missing_fingerprint) {
        throw new Error('The missing cues changed. Refresh the board and review your pasted text before updating.');
    }
    const translated = new Map(numberedKhmerEntries(text).entries.map(entry=>[entry.id,entry.words]));
    const segments = job.segments.map(cue=>translated.has(cue.id) ?
        {...cue,translation:translated.get(cue.id),translation_source:'manual'} : cue);
    await processingRequest('segments',{segments},'PUT');
    await processingRequest('manual-mode',{});
}

function mergeMissingCorrectionsIntoDrafts(board,text) {
    const corrections = new Map(numberedKhmerEntries(text).entries.map(entry=>[entry.id,entry.words]));
    const originals = new Map(board.cues.map(cue=>[cue.id,(cue.translation || '').trim().replace(/\s+/gu,' ')]));
    const prefix = `manual_translation:${processingJobId}:${board.source_fingerprint}:`;
    const keys = new Set([...processingDrafts.keys()].filter(key=>key.startsWith(prefix)));
    if (processingDetails) keys.add(processingDraftKey(processingDetails));
    for (const key of keys) {
        let draft = processingDrafts.get(key);
        if (draft === undefined) { try { draft = sessionStorage.getItem(key); } catch {} }
        if (draft == null) continue;
        const parsed = numberedKhmerEntries(draft);
        let updated = parsed.source;
        for (let index=parsed.entries.length-1; index>=0; index--) {
            const entry = parsed.entries[index];
            if (!corrections.has(entry.id) || (entry.words !== originals.get(entry.id) && /[\p{L}\p{N}]/u.test(entry.words))) continue;
            const start = parsed.markers[index].index + parsed.markers[index][0].length;
            const end = index+1 < parsed.markers.length ? parsed.markers[index+1].index : updated.length;
            updated = updated.slice(0,start) + ' ' + corrections.get(entry.id) + '\n' + updated.slice(end);
        }
        const present = new Set(parsed.entries.map(entry=>entry.id));
        for (const id of processingDraftCueIds.get(key) || []) {
            if (corrections.has(id) && !present.has(id)) updated += `\n[${id}] ${corrections.get(id)}`;
        }
        if (updated.trimEnd() !== draft) processingValidationErrors.delete(key);
        processingDrafts.set(key,updated.trimEnd());
        try { sessionStorage.setItem(key,updated.trimEnd()); } catch {}
    }
}

async function saveMissingBoardTranslations() {
    if (processingBusy || !missingTranslationBoard?.can_edit) return;
    const formatted = formatMissingBoardTranslation();
    if (!formatted || formatted.missing.length) return;
    const submitted = missingTranslationBoard;
    const text = processingElement('missing-board-input').value;
    processingGeneration++;
    missingBoardGeneration++;
    processingBusy = true;
    renderProcessingDetails(processingDetails);
    try {
        if (submitted.legacy) await saveLegacyMissingTranslations(submitted,text);
        else await processingRequest('missing-translations',{
            source_fingerprint:submitted.source_fingerprint,missing_fingerprint:submitted.missing_fingerprint,text
        });
        mergeMissingCorrectionsIntoDrafts(submitted,text);
        const key = missingBoardKey(submitted);
        missingBoardDrafts.delete(key);
        try { sessionStorage.removeItem(key); } catch {}
        processingBusy = false;
        await loadProcessingDetails();
        missingBoardMessage(`Updated ${formatted.count} missing translations. Your other translations and saved voice clips are kept.`,'success');
    } catch (error) { missingBoardMessage(error.message,'error'); }
    finally {
        processingBusy = false;
        if (processingDetails) renderProcessingDetails(processingDetails);
    }
}

function renderProcessingValidation(errorMessage = null) {
    const panel = processingElement('translation-validation');
    const list = processingElement('missing-cue-list');
    const input = processingElement('khmer-text');
    list.replaceChildren();
    panel.hidden = true;
    input.setAttribute('aria-invalid', 'false');
    if (!processingDetails) return;
    const key = processingDraftKey(processingDetails);
    if (errorMessage) {
        processingValidationEnabled.add(key);
        processingValidationErrors.set(key, errorMessage);
    }
    if (!processingValidationEnabled.has(key)) return;
    const text = input.value;
    const translated = new Set(numberedKhmerEntries(text).entries.filter(entry => /[\p{L}\p{N}]/u.test(entry.words)).map(entry => entry.id));
    const missing = processingDetails.cues.filter(cue => !translated.has(cue.id));
    let message = processingValidationErrors.get(key) || '';
    if (!message) {
        try { formatNumberedKhmer(text, processingDetails.cues); }
        catch (error) { message = error.message; }
    }
    if (!missing.length && !message) return;
    panel.hidden = false;
    input.setAttribute('aria-invalid', 'true');
    processingElement('translation-validation-title').textContent = missing.length ?
        `${missing.length} missing cue${missing.length === 1 ? '' : 's'} in this batch` : 'Check this translation';
    processingElement('translation-validation-message').textContent = message ||
        'Add Khmer text for each cue below, keeping its numbered marker. Your pasted text is kept.';
    for (const cue of missing) {
        const row = document.createElement('div');
        row.className = 'missing-cue';
        const heading = document.createElement('div');
        heading.className = 'missing-cue-heading';
        const number = document.createElement('strong');
        number.textContent = `[${cue.id}]`;
        const time = document.createElement('span');
        time.textContent = `${formatProcessingTime(cue.start)}–${formatProcessingTime(cue.end)}`;
        heading.appendChild(number);
        heading.appendChild(time);
        const original = document.createElement('p');
        original.textContent = cue.text || 'Original dialogue unavailable for this cue.';
        row.appendChild(heading);
        row.appendChild(original);
        list.appendChild(row);
    }
}

function formatProcessingTranslation() {
    if (processingBusy || !processingDetails?.can_edit) return null;
    try {
        const formatted = formatNumberedKhmer(processingElement('khmer-text').value, processingDetails.cues);
        processingElement('khmer-text').value = formatted.text;
        saveProcessingDraft();
        processingElement('generate-video').disabled = true;
        processingMessage(formatted.missing.length ?
            `Formatted ${formatted.count} cues. Still missing: ${formatted.missing.slice(0, 12).map(id => `[${id}]`).join(', ')}. Restore these translations before saving.` :
            `Formatted ${formatted.count} cues in original dialogue order. Review the text, then save this batch.`,
            formatted.missing.length ? 'error' : 'success');
        return formatted;
    } catch (error) {
        renderProcessingValidation(error.message);
        processingMessage(error.message, 'error');
        return null;
    }
}

function renderProcessingDetails(data) {
    processingDetails = data;
    processingBatch = data.batch;
    processingElement('job-title').textContent = data.title || processingJobId;
    processingElement('job-number').textContent = `Job ${processingJobId}`;
    processingElement('job-stage').textContent = data.stage || data.status;
    processingElement('translation-count').textContent = `${data.total_cues - data.remaining_cues} / ${data.total_cues} cues ready`;
    processingElement('source-language').textContent = `Source: ${data.source_language}`;
    processingElement('empty-state').hidden = data.total_cues > 0;
    processingElement('translation-workspace').hidden = !data.total_cues;
    const pause = processingElement('pause-job');
    pause.hidden = data.can_edit;
    pause.disabled = processingBusy || data.status === 'pausing';
    const mode = processingElement('enable-manual');
    mode.hidden = data.manual_translation || !data.total_cues;
    mode.disabled = processingBusy || !data.can_edit;
    const select = processingElement('batch-select');
    select.replaceChildren();
    for (let index = 0; index < data.batch_count; index++) {
        const option = document.createElement('option');
        option.value = index;
        option.textContent = `${index + 1} of ${data.batch_count}`;
        select.appendChild(option);
    }
    select.value = data.batch;
    select.disabled = processingBusy;
    processingElement('previous-batch').disabled = processingBusy || data.batch === 0;
    processingElement('next-batch').disabled = processingBusy || data.batch + 1 >= data.batch_count;
    processingElement('source-text').value = data.source_text;
    const draftKey = processingDraftKey(data);
    processingDraftCueIds.set(draftKey,data.cues.map(cue=>cue.id));
    let draft = processingDrafts.get(draftKey) ?? null;
    if (draft === null) {
        try { draft = sessionStorage.getItem(draftKey); } catch {}
    }
    if (draft !== null) {
        try {
            if (formatNumberedKhmer(draft,data.cues).text === formatNumberedKhmer(data.translation_text,data.cues).text) {
                processingDrafts.delete(draftKey);
                processingValidationEnabled.delete(draftKey);
                processingValidationErrors.delete(draftKey);
                try { sessionStorage.removeItem(draftKey); } catch {}
                draft = null;
            }
        } catch {}
    }
    processingElement('khmer-text').value = draft ?? data.translation_text;
    if (draft !== null || data.cues.some(cue => (cue.translation || '').trim() && !/[\p{L}\p{N}]/u.test(cue.translation))) processingValidationEnabled.add(draftKey);
    renderProcessingValidation();
    processingElement('draft-status').textContent = draft === null ? 'Saved text / cue template' : 'Unsaved · kept in this tab';
    processingElement('khmer-text').disabled = processingBusy || !data.can_edit;
    processingElement('save-translations').disabled = processingBusy || !data.can_edit;
    processingElement('format-translations').disabled = processingBusy || !data.can_edit;
    processingElement('translation-file').disabled = processingBusy || !data.can_edit;
    processingElement('refresh-details').disabled = processingBusy;
    const base = `/api/video/job/${encodeURIComponent(processingJobId)}/transcript.txt`;
    processingElement('download-all').href = base;
    processingElement('download-batch').href = `${base}?batch=${data.batch}`;
    processingElement('generate-video').disabled = processingBusy || !data.can_generate || !data.manual_translation || draft !== null;
    processingElement('finish-help').textContent = data.status === 'completed' ? 'Video complete. Open the video editor to watch or download it.' :
        !data.can_edit ? 'This task is running. Completed translations stay saved.' :
        data.remaining_cues ? `${data.remaining_cues} cue(s) still need Khmer text. Save every batch before generating.` :
        draft !== null ? 'Save or review the text in this batch before generating.' :
        data.manual_translation ? 'All dialogue has translations. Review the saved text, then generate the voiceover.' :
        'Choose Use manual translation to generate from the saved Khmer text without automatic translation.';
    const body = processingElement('review-rows');
    body.replaceChildren();
    for (const cue of data.cues) {
        const row = document.createElement('tr');
        for (const text of [`[${cue.id}] ${formatProcessingTime(cue.start)}–${formatProcessingTime(cue.end)}`, cue.text, cue.translation || 'Not translated']) {
            const cell = document.createElement('td');
            cell.textContent = text;
            row.appendChild(cell);
        }
        body.appendChild(row);
    }
    renderMissingTranslationBoard(missingTranslationBoard);
}

async function loadProcessingDetails(batch = processingBatch) {
    if (processingBusy) return;
    const generation = ++processingGeneration;
    const locateMissing = processingLocateMissing;
    processingLocateMissing = false;
    try {
        let data = await processingRequest(`manual-translation?batch=${batch}`);
        if (generation !== processingGeneration) return;
        if (locateMissing && data.remaining_cues > 0) {
            const needsText = details => details.cues.some(cue =>
                (cue.text || '').trim() && (!/[\p{L}\p{N}]/u.test(cue.translation || '') || cue.translation_error));
            if (!needsText(data)) {
                for (let index = 0; index < data.batch_count; index++) {
                    if (index === data.batch) continue;
                    const candidate = await processingRequest(`manual-translation?batch=${index}`);
                    if (generation !== processingGeneration) return;
                    if (needsText(candidate)) { data = candidate; break; }
                }
            }
            processingValidationEnabled.add(processingDraftKey(data));
        }
        renderProcessingDetails(data);
        await loadMissingTranslationBoard(generation);
        if (generation !== processingGeneration) return;
        if (locateMissing) {
            if (missingTranslationBoard?.remaining_cues) {
                processingElement('missing-board').scrollIntoView?.({block:'start'});
                processingElement('missing-board-input').focus?.({preventScroll:true});
                return;
            }
            const panel = processingElement('translation-validation');
            (panel.hidden ? processingElement('khmer-text') : panel).scrollIntoView?.({ block:'center' });
            const input = processingElement('khmer-text');
            const parsed = numberedKhmerEntries(input.value);
            const index = parsed.entries.findIndex(entry => !/[\p{L}\p{N}]/u.test(entry.words));
            if (index >= 0) {
                const start = parsed.markers[index].index + parsed.markers[index][0].length;
                const end = index + 1 < parsed.markers.length ? parsed.markers[index+1].index : input.value.length;
                input.focus?.({ preventScroll:true });
                input.setSelectionRange?.(start,end);
            }
        }
    } catch (error) {
        if (generation === processingGeneration) processingMessage(error.message, 'error');
    }
}

async function saveProcessingTranslations() {
    if (processingBusy || !processingDetails) return;
    const formatted = formatProcessingTranslation();
    if (!formatted || formatted.missing.length) return;
    saveProcessingDraft();
    processingGeneration++;
    processingBusy = true;
    const submitted = processingDetails;
    const text = processingElement('khmer-text').value;
    renderProcessingDetails(submitted);
    try {
        const data = await processingRequest('manual-translation', {
            batch: submitted.batch, source_fingerprint: submitted.source_fingerprint, text
        });
        try { sessionStorage.removeItem(processingDraftKey(submitted)); } catch {}
        processingDrafts.delete(processingDraftKey(submitted));
        processingValidationEnabled.delete(processingDraftKey(submitted));
        processingValidationErrors.delete(processingDraftKey(submitted));
        processingBusy = false;
        renderProcessingDetails(data);
        await loadMissingTranslationBoard();
        processingMessage(`Batch ${data.batch + 1} saved. Original cue timing is unchanged.`, 'success');
    } catch (error) {
        renderProcessingValidation(error.message);
        processingMessage(error.message, 'error');
    } finally {
        processingBusy = false;
        renderProcessingDetails(processingDetails);
    }
}

async function processingAction(action) {
    if (processingBusy) return;
    processingGeneration++;
    processingBusy = true;
    if (processingDetails) renderProcessingDetails(processingDetails);
    try {
        await processingRequest(action, action === 'resume' ? { manual_translation: true } : {});
        processingMessage(action === 'resume' ? 'Generating Khmer voices and video from your saved translations.' :
            action === 'pause' ? 'Task paused. Saved work is retained.' : 'Manual translation enabled. Automatic translation is skipped.');
    } catch (error) {
        processingMessage(error.message, 'error');
    } finally {
        processingBusy = false;
        await loadProcessingDetails();
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const parameters = new URLSearchParams(window.location.search);
    processingJobId = parameters.get('job');
    processingLocateMissing = parameters.get('fix') === 'missing';
    if (!processingJobId) {
        processingMessage('Open Processing details from a task in Video Jobs.', 'error');
        return;
    }
    processingElement('open-editor').addEventListener('click', () => localStorage.setItem('khmer_active_video_job', processingJobId));
    processingElement('missing-board-input').addEventListener('input',event=>{
        saveMissingBoardDraft();
        if (event.inputType === 'insertFromPaste') formatMissingBoardTranslation();
    });
    processingElement('format-missing-cues').addEventListener('click',formatMissingBoardTranslation);
    processingElement('save-missing-cues').addEventListener('click',saveMissingBoardTranslations);
    processingElement('copy-missing-cues').addEventListener('click',async()=>{
        try {
            await navigator.clipboard.writeText(processingElement('missing-board-source').value);
            missingBoardMessage('All missing original cues copied. Translate them to Khmer and keep every [cue number].');
        } catch {
            processingElement('missing-board-source').select();
            missingBoardMessage('Press Ctrl+C or ⌘C to copy the selected missing cues.');
        }
    });
    processingElement('khmer-text').addEventListener('input', event => {
        saveProcessingDraft();
        processingElement('generate-video').disabled = true;
        if (event.inputType === 'insertFromPaste') formatProcessingTranslation();
    });
    processingElement('batch-select').addEventListener('change', event => loadProcessingDetails(Number(event.target.value)));
    processingElement('previous-batch').addEventListener('click', () => loadProcessingDetails(processingBatch - 1));
    processingElement('next-batch').addEventListener('click', () => loadProcessingDetails(processingBatch + 1));
    processingElement('refresh-details').addEventListener('click', () => loadProcessingDetails());
    processingElement('save-translations').addEventListener('click', saveProcessingTranslations);
    processingElement('format-translations').addEventListener('click', formatProcessingTranslation);
    processingElement('enable-manual').addEventListener('click', () => processingAction('manual-mode'));
    processingElement('pause-job').addEventListener('click', () => processingAction('pause'));
    processingElement('generate-video').addEventListener('click', () => processingAction('resume'));
    processingElement('copy-original').addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(processingElement('source-text').value);
            processingMessage('Original text copied. Paste it into Google Translate and choose Khmer.');
        } catch {
            processingElement('source-text').select();
            processingMessage('Select the original text and press Ctrl+C or ⌘C to copy.');
        }
    });
    processingElement('translation-file').addEventListener('change', async event => {
        const file = event.target.files[0];
        if (!file) return;
        if (file.size > 1_000_000) {
            processingMessage('Choose a translated text batch smaller than 1 MB.', 'error');
            return;
        }
        const snapshot = processingDetails;
        try {
            const text = await file.text();
            if (processingDetails !== snapshot) return;
            processingElement('khmer-text').value = text;
            saveProcessingDraft();
            processingElement('generate-video').disabled = true;
            formatProcessingTranslation();
        } catch (error) { processingMessage(error.message, 'error'); }
        event.target.value = '';
    });
    loadProcessingDetails();
    setInterval(() => {
        if (!processingBusy && processingDetails && (!processingDetails.can_edit || !processingDetails.total_cues)) loadProcessingDetails();
    }, 4000);
});
