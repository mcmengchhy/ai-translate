const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function details(overrides = {}) {
    return {
        job_id: 'manual', title: '<script>source</script>', status: 'awaiting_translation', stage: 'Waiting',
        manual_translation: true, source_language: 'zh', source_fingerprint: 'a'.repeat(64),
        batch: 0, batch_count: 2, total_cues: 2, remaining_cues: 1,
        source_text: '[7] 今天下雨了。', translation_text: '[7] ', can_edit: true, can_generate: false,
        cues: [{ id: 7, start: 1.2, end: 2.6, text: '<img src=x>', translation: '' }], ...overrides,
    };
}

function missingBoard(overrides = {}) {
    return {source_fingerprint:'a'.repeat(64),missing_fingerprint:'b'.repeat(64),remaining_cues:2,can_edit:true,
        source_text:'[7] 今天下雨了。\n[355] 最后一句。',
        cues:[{id:7,start:1,end:2,text:'今天下雨了。',translation:'...'},
              {id:355,start:900,end:902,text:'最后一句。',translation:''}],...overrides};
}

function showMissingBoard(h, board = missingBoard()) {
    h.context.boardFixture = board;
    h.run('missingTranslationBoard = boardFixture');
    h.render(details({remaining_cues:2}));
}

function harness() {
    const elements = new Map(), storage = new Map(), requests = [];
    let ready;
    function node() {
        return { textContent: '', value: '', dataset: {}, children: [], hidden: false, disabled: false,
            attributes: {}, setAttribute(name, value) { this.attributes[name] = value; },
            replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); },
            listeners: {}, addEventListener(name, listener) { this.listeners[name] = listener; }, select() {},
            focus() { this.focused = true; }, scrollIntoView() { this.scrolled = true; },
            setSelectionRange(start,end) { this.selectionStart = start; this.selectionEnd = end; } };
    }
    function element(id) {
        if (!elements.has(id)) elements.set(id, node());
        return elements.get(id);
    }
    const context = vm.createContext({
        console, URLSearchParams,
        document: { getElementById: element, createElement: node, addEventListener(name, listener) { if (name === 'DOMContentLoaded') ready = listener; } },
        window: { location: { search: '?job=manual' } },
        localStorage: { setItem() {} }, setInterval() {},
        sessionStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        fetch: async (url, options = {}) => {
            requests.push({ url, body: options.body ? JSON.parse(options.body) : null });
            return { ok: true, json: async () => details() };
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/processing.js'), 'utf8'), context);
    const run = source => vm.runInContext(source, context);
    run("processingJobId = 'manual'");
    return { context, element, storage, requests, run, start() { ready(); }, render(data) { context.renderProcessingDetails(data); } };
}

test('formatter sorts inline Khmer-numbered cues into source order without changing words', () => {
    const h = harness();
    h.context.cues = [{ id: 7 }, { id: 12 }];
    h.context.pasted = '[១២] កុំភ្លេច\nយកឆ័ត្រ។ [៧] ថ្ងៃនេះមានភ្លៀងធ្លាក់។';
    const formatted = JSON.parse(h.run('JSON.stringify(formatNumberedKhmer(pasted, cues))'));
    assert.equal(formatted.text, '[7] ថ្ងៃនេះមានភ្លៀងធ្លាក់។\n[12] កុំភ្លេច យកឆ័ត្រ។');
    assert.equal(formatted.count, 2);
    assert.deepEqual(formatted.missing, []);
});

test('formatter reports missing cues without inventing text or renumbering another sentence', () => {
    const h = harness();
    h.context.cues = [{ id: 7 }, { id: 8 }, { id: 12 }];
    h.context.pasted = '[12] អរគុណ។ [7] សួស្តី។';
    const formatted = JSON.parse(h.run('JSON.stringify(formatNumberedKhmer(pasted, cues))'));
    assert.equal(formatted.text, '[7] សួស្តី។\n[12] អរគុណ។');
    assert.deepEqual(formatted.missing, [8]);
});

test('missing cues appear beside the Khmer text with all numbers and original dialogue', async () => {
    const h = harness();
    const cues = Array.from({length: 16}, (_, index) => ({id:index + 7, start:index * 2, end:index * 2 + 1, text:`Original ${index + 7}`}));
    h.render(details({cues}));
    assert.equal(h.element('translation-validation').hidden, true);
    h.element('khmer-text').value = '[7] សួស្តី';
    await h.run('saveProcessingTranslations()');
    assert.equal(h.requests.length, 0, 'Incomplete text must not be saved');
    assert.equal(h.element('translation-validation').hidden, false);
    assert.equal(h.element('translation-validation-title').textContent, '15 missing cues in this batch');
    const rows = h.element('missing-cue-list').children;
    assert.deepEqual(rows.map(row => row.children[0].children[0].textContent), cues.slice(1).map(cue => `[${cue.id}]`));
    assert.equal(rows[0].children[1].textContent, 'Original 8');
    assert.equal(rows[0].children[0].children[1].textContent, '0:02–0:03');
    assert.equal(h.element('khmer-text').value, '[7] សួស្តី');
    assert.equal(h.element('khmer-text').attributes['aria-invalid'], 'true');
});

test('missing-cue panel updates while typing and clears as the final cue is filled', () => {
    const h = harness();
    h.run('loadProcessingDetails = () => {}');
    h.start();
    h.render(details({cues:[{id:7,text:'First'},{id:8,text:'Second'}]}));
    h.element('khmer-text').value = '[7] សួស្តី [៨]';
    h.element('khmer-text').listeners.input({inputType:'insertText'});
    assert.equal(h.element('translation-validation').hidden, false);
    assert.equal(h.element('translation-validation-title').textContent, '1 missing cue in this batch');
    assert.equal(h.element('missing-cue-list').children[0].children[0].children[0].textContent, '[8]');
    h.element('khmer-text').value += ' អរគុណ';
    h.element('khmer-text').listeners.input({inputType:'insertText'});
    assert.equal(h.element('translation-validation').hidden, true);
    assert.equal(h.element('missing-cue-list').children.length, 0);
    assert.equal(h.element('khmer-text').attributes['aria-invalid'], 'false');
    assert.equal(h.requests.length, 0);
});

test('missing-cue panel follows the selected batch and survives refresh with its draft', () => {
    const h = harness();
    const first = details({cues:[{id:7,text:'First'},{id:8,text:'Second'}]});
    h.render(first);
    h.element('khmer-text').value = '[7] សួស្តី';
    h.run('formatProcessingTranslation()');
    h.render(details({batch:1,cues:[{id:12,text:'Next'}],translation_text:'[12] '}));
    assert.equal(h.element('translation-validation').hidden, true);
    assert.equal(h.element('missing-cue-list').children.length, 0);
    h.render(first);
    assert.equal(h.element('translation-validation').hidden, false);
    assert.equal(h.element('missing-cue-list').children[0].children[0].children[0].textContent, '[8]');
});

test('punctuation-only Khmer placeholders are flagged before any voice or save request', async () => {
    const h = harness();
    h.render(details({cues:[{id:127,start:1,end:2,text:'Actual spoken dialogue',translation:'...'}],translation_text:'[127] ...'}));
    assert.equal(h.element('translation-validation').hidden, false);
    assert.equal(h.element('missing-cue-list').children[0].children[0].children[0].textContent, '[127]');
    await h.run('saveProcessingTranslations()');
    assert.equal(h.requests.length, 0);
    assert.match(h.element('translation-validation-message').textContent, /only punctuation/);
    assert.equal(h.element('khmer-text').value, '[127] ...');
});

test('Khmer and standard cue digits normalize to the same number and extra spaces disappear', () => {
    const h = harness();
    h.context.cues = [{ id: 1180 }, { id: 1181 }];
    for (const marker of ['[១១៨០]', '[1180]']) {
        h.context.pasted = `\n\n[១១៨១]   អរគុណ។\n\n\n${marker}    សួស្តី    បង។   `;
        const formatted = JSON.parse(h.run('JSON.stringify(formatNumberedKhmer(pasted, cues))'));
        assert.equal(formatted.text, '[1180] សួស្តី បង។\n[1181] អរគុណ។');
        assert.deepEqual(formatted.missing, []);
    }
    h.context.pasted = '[១១៨០] សួស្តី [1180] អរគុណ';
    assert.throws(() => h.run('formatNumberedKhmer(pasted, cues)'), /appears more than once/);
});

test('duplicate or unexpected cue formatting preserves the original paste', () => {
    const h = harness();
    h.render(details());
    for (const text of ['[7] សួស្តី [៧] អរគុណ', '[99] សួស្តី']) {
        h.element('khmer-text').value = text;
        assert.equal(h.run('formatProcessingTranslation()'), null);
        assert.equal(h.element('khmer-text').value, text);
        assert.equal(h.element('message').dataset.kind, 'error');
        assert.equal(h.element('translation-validation').hidden, false);
        assert.match(h.element('translation-validation-message').textContent, /more than once|does not belong/);
    }
});

test('pasting automatically formats Khmer and keeps an unsaved draft without a network request', () => {
    const h = harness();
    h.run('loadProcessingDetails = () => {}');
    h.start();
    h.render(details({ cues: [{ id: 7 }, { id: 12 }] }));
    h.element('khmer-text').value = '[12] អរគុណ។ [៧] សួស្តី។';
    h.element('khmer-text').listeners.input({ inputType: 'insertFromPaste' });
    assert.equal(h.element('khmer-text').value, '[7] សួស្តី។\n[12] អរគុណ។');
    assert.equal([...h.storage.values()][0], h.element('khmer-text').value);
    assert.equal(h.requests.length, 0);
    assert.equal(h.element('generate-video').disabled, true);
});

test('switching text batches and refreshing preserve an unsaved Khmer paste', () => {
    const h = harness();
    h.render(details());
    h.element('khmer-text').value = '[7] ថ្ងៃនេះមានភ្លៀងធ្លាក់។';
    h.run('saveProcessingDraft()');
    h.render(details({ batch: 1, source_text: '[12] 记得带伞。' }));
    h.element('khmer-text').value = '[12] សួស្តី';
    h.run('saveProcessingDraft()');
    h.render(details());
    assert.equal(h.element('khmer-text').value, '[7] ថ្ងៃនេះមានភ្លៀងធ្លាក់។');
    assert.equal(h.element('generate-video').disabled, true);
});

test('a rejected import keeps the pasted text and does not generate a video', async () => {
    const h = harness();
    h.render(details());
    h.element('khmer-text').value = '[7] សួស្តី';
    h.context.fetch = async () => ({ ok: false, json: async () => ({ detail: 'Cue numbers do not match' }) });
    await h.run('saveProcessingTranslations()');
    assert.equal(h.element('khmer-text').value, '[7] សួស្តី');
    assert.match(h.element('message').textContent, /Cue numbers/);
    assert.equal(h.element('translation-validation').hidden, false);
    assert.match(h.element('translation-validation-message').textContent, /Cue numbers/);
    assert.equal(h.element('generate-video').disabled, true);
    assert.equal(h.run('processingBusy'), false);
});

test('successful import sends its source fingerprint and clears only the saved draft', async () => {
    const h = harness();
    h.render(details());
    h.element('khmer-text').value = '[7] សួស្តី';
    h.run('saveProcessingDraft()');
    h.context.fetch = async (url, options) => {
        h.requests.push({ url, body: JSON.parse(options.body) });
        return { ok: true, json: async () => details({ remaining_cues: 0, can_generate: true, translation_text: '[7] សួស្តី' }) };
    };
    await h.run('saveProcessingTranslations()');
    assert.deepEqual(h.requests[0].body, { batch: 0, source_fingerprint: 'a'.repeat(64), text: '[7] សួស្តី' });
    assert.equal(h.storage.size, 0);
    assert.equal(h.element('generate-video').disabled, false);
    assert.equal(h.element('translation-validation').hidden, true);
});

test('late details responses cannot replace the newly selected text batch', async () => {
    const h = harness();
    let release;
    h.context.fetch = url => url.endsWith('batch=0') ? new Promise(resolve => { release = resolve; }) :
        Promise.resolve({ ok: true, json: async () => details({ batch: 1, source_text: '[12] New batch' }) });
    const old = h.run('loadProcessingDetails(0)');
    await h.run('loadProcessingDetails(1)');
    release({ ok: true, json: async () => details() });
    await old;
    assert.equal(h.element('source-text').value, '[12] New batch');
});

test('Fix and continue opens the first incomplete batch and selects its placeholder', async () => {
    const h = harness();
    const paths = [];
    h.context.fetch = async url => {
        paths.push(url);
        return {ok:true,json:async()=>url.endsWith('batch=0') ?
            details({remaining_cues:1,cues:[{id:7,text:'First',translation:'សួស្តី'}],translation_text:'[7] សួស្តី'}) :
            details({batch:1,remaining_cues:1,cues:[{id:12,text:'Missing dialogue',translation:'...'}],translation_text:'[12] ...'})};
    };
    h.run('processingLocateMissing = true');
    await h.run('loadProcessingDetails()');
    assert.equal(h.run('processingBatch'),1);
    assert.deepEqual(paths.filter(url=>url.includes('manual-translation?')),['/api/video/job/manual/manual-translation?batch=0','/api/video/job/manual/manual-translation?batch=1']);
    assert.equal(h.element('translation-validation').hidden,false);
    assert.equal(h.element('translation-validation').scrolled,true);
    const input = h.element('khmer-text');
    assert.equal(input.focused,true);
    assert.equal(input.value.slice(input.selectionStart,input.selectionEnd).trim(),'...');
    assert.equal(h.element('generate-video').disabled,true);
});

test('Fix and continue preserves an unsaved correction already in the target batch', async () => {
    const h = harness();
    const second = details({batch:1,cues:[{id:12,text:'Second',translation:'...'}],translation_text:'[12] ...'});
    h.render(second);
    h.element('khmer-text').value='[12] អរគុណ';
    h.run('saveProcessingDraft()');
    h.context.fetch = async url => ({ok:true,json:async()=>url.endsWith('batch=0') ?
        details({cues:[{id:7,text:'First',translation:'សួស្តី'}],translation_text:'[7] សួស្តី'}) : second});
    h.run('processingLocateMissing = true');
    await h.run('loadProcessingDetails(0)');
    assert.equal(h.run('processingBatch'),1);
    assert.equal(h.element('khmer-text').value,'[12] អរគុណ');
    assert.equal(h.element('translation-validation').hidden,true);
    assert.equal(h.element('generate-video').disabled,true,'The correction must be saved before generating');
});

test('generation explicitly requests manual mode without sending a provider or model', async () => {
    const h = harness();
    h.render(details({ remaining_cues: 0, can_generate: true }));
    await h.run("processingAction('resume')");
    assert.equal(h.requests[0].url, '/api/video/job/manual/resume');
    assert.deepEqual(h.requests[0].body, { manual_translation: true });
});

test('original dialogue and titles are rendered as text rather than HTML', () => {
    const h = harness();
    h.render(details());
    assert.equal(h.element('job-title').textContent, '<script>source</script>');
    const sourceCell = h.element('review-rows').children[0].children[1];
    assert.equal(sourceCell.textContent, '<img src=x>');
    assert.equal(sourceCell.innerHTML, undefined);
});

test('missing board copies all missing original lines across batches with their real cue numbers', async () => {
    const h = harness();
    h.run('loadProcessingDetails = () => {}');
    let copied;
    h.context.navigator = {clipboard:{writeText:async text=>{copied=text}}};
    h.start();
    showMissingBoard(h);
    assert.equal(h.element('missing-board').hidden,false);
    assert.match(h.element('missing-board-count').textContent,/2 missing cues · all batches/);
    await h.element('copy-missing-cues').listeners.click();
    assert.equal(copied,'[7] 今天下雨了。\n[355] 最后一句。');
    assert.equal(h.requests.length,0);
});

test('pasting numbered repairs formats just the missing cues and keeps a draft without saving', () => {
    const h = harness();
    h.run('loadProcessingDetails = () => {}');
    h.start();
    showMissingBoard(h);
    h.element('missing-board-input').value='[៣៥៥] អរគុណ។ [៧] សួស្តី។';
    h.element('missing-board-input').listeners.input({inputType:'insertFromPaste'});
    assert.equal(h.element('missing-board-input').value,'[7] សួស្តី។\n[355] អរគុណ។');
    assert.ok([...h.storage.keys()].some(key=>key.startsWith('manual_missing:')));
    assert.equal(h.requests.length,0);
});

test('missing board submits only the requested repairs and clears its draft after success', async () => {
    const h = harness();
    showMissingBoard(h);
    h.element('missing-board-input').value='[355] អរគុណ [7] សួស្តី';
    const writes = [];
    h.context.fetch = async (url,options={}) => {
        if (options.method==='POST') {
            writes.push({url,body:JSON.parse(options.body)});
            return {ok:true,json:async()=>missingBoard({remaining_cues:0,cues:[],source_text:'',updated_cue_ids:[7,355]})};
        }
        return {ok:true,json:async()=>url.endsWith('/missing-translations') ?
            missingBoard({remaining_cues:0,cues:[],source_text:''}) : details({remaining_cues:0,can_generate:true,translation_text:'[7] សួស្តី'})};
    };
    await h.run('saveMissingBoardTranslations()');
    assert.deepEqual(writes,[{url:'/api/video/job/manual/missing-translations',body:{
        source_fingerprint:'a'.repeat(64),missing_fingerprint:'b'.repeat(64),text:'[7] សួស្តី\n[355] អរគុណ'}}]);
    assert.equal(h.element('missing-board-grid').hidden,true);
    assert.equal(h.element('missing-board-empty').hidden,false);
    assert.match(h.element('missing-board-message').textContent,/Updated 2 missing translations/);
    assert.equal(h.storage.size,0);
});

test('missing board rejects duplicates, wrong numbers, Chinese, and incomplete repairs before saving', async () => {
    for (const text of ['[7] សួស្តី','[7] សួស្តី [7] អរគុណ [355] លាហើយ',
        '[7] សួស្តី [12] អរគុណ','[7] 今天下雨了 [355] អរគុណ','[7] ... [355] អរគុណ']) {
        const h = harness();
        showMissingBoard(h);
        h.element('missing-board-input').value=text;
        await h.run('saveMissingBoardTranslations()');
        assert.equal(h.requests.length,0);
        assert.equal(h.element('missing-board-message').dataset.kind,'error');
        assert.ok(h.element('missing-board-input').value);
    }
});

test('a rejected repair keeps the pasted text and its tab draft for correction', async () => {
    const h = harness();
    showMissingBoard(h);
    h.element('missing-board-input').value='[7] សួស្តី\n[355] អរគុណ';
    h.context.fetch = async()=>({ok:false,status:409,json:async()=>({detail:'The missing cues changed'})});
    await h.run('saveMissingBoardTranslations()');
    assert.equal(h.element('missing-board-input').value,'[7] សួស្តី\n[355] អរគុណ');
    assert.match(h.element('missing-board-message').textContent,/missing cues changed/);
    assert.ok([...h.storage.keys()].some(key=>key.startsWith('manual_missing:')));
    assert.equal(h.run('processingBusy'),false);
});

test('repairs update placeholder drafts while preserving other unsaved Khmer edits', () => {
    const h = harness();
    const batch = details({cues:[{id:7,translation:'...'},{id:12,translation:'Saved'}],translation_text:'[7] ...\n[12] Saved'});
    h.render(batch);
    h.element('khmer-text').value='[7] ...\n[12] My unsaved edit';
    h.run('saveProcessingDraft()');
    h.context.boardFixture=missingBoard();
    h.run("mergeMissingCorrectionsIntoDrafts(boardFixture,'[7] សួស្តី [355] អរគុណ')");
    h.render({...batch,translation_text:'[7] សួស្តី\n[12] Saved'});
    assert.equal(h.element('khmer-text').value,'[7] សួស្តី\n[12] My unsaved edit');
    assert.equal(h.element('generate-video').disabled,true);
});

test('the board works with existing workers and sends unchanged successful translations and voices', async () => {
    const h = harness();
    let job = {status:'paused',segments:[
        {id:7,start:1,end:2,text:'First',translation:'...',tts_error:'no audio'},
        {id:12,start:3,end:4,text:'Good',translation:'កុំភ្លេច',audio_file:'keep.mp3',prosody:{rate:'+10%'}},
        {id:355,start:900,end:902,text:'Last',translation:'...'}]};
    const good = JSON.parse(JSON.stringify(job.segments[1]));
    const writes = [];
    h.context.fetch = async (url,options={}) => {
        if (url.endsWith('/missing-translations')) return {ok:false,status:404,json:async()=>({detail:'Not Found'})};
        if (url==='/api/video/status/manual') return {ok:true,json:async()=>JSON.parse(JSON.stringify(job))};
        if (options.method==='PUT') {
            const body=JSON.parse(options.body);writes.push(body);job.segments=body.segments;
            return {ok:true,json:async()=>({status:'ok'})};
        }
        const ready = job.segments.every(cue=>cue.translation!=='...');
        return {ok:true,json:async()=>details({remaining_cues:ready ? 0 : 2,can_generate:ready,
            cues:job.segments.slice(0,2),translation_text:job.segments.slice(0,2).map(cue=>`[${cue.id}] ${cue.translation}`).join('\n')})};
    };
    h.render(details());
    await h.run('loadMissingTranslationBoard()');
    assert.equal(h.element('missing-board-source').value,'[7] First\n[355] Last');
    h.element('missing-board-input').value='[7] សួស្តី [355] អរគុណ';
    await h.run('saveMissingBoardTranslations()');
    assert.equal(writes.length,1);
    assert.deepEqual(writes[0].segments[1],good);
    assert.equal(writes[0].segments[0].translation,'សួស្តី');
    assert.equal(writes[0].segments[2].translation,'អរគុណ');
    assert.equal(h.element('missing-board-empty').hidden,false);
});
