const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const noop = () => {};

function harness(preferences = {}) {
    const elements = new Map();
    const storage = new Map([['khmer_translator_settings', JSON.stringify(preferences)]]);
    const requests = [];
    const intervals = new Map();
    function withInnerHTML(element) {
        let html = '';
        Object.defineProperty(element, 'innerHTML', {
            get: () => html,
            set(value) { html = value; this.children = []; },
        });
        return element;
    }
    function node() {
        return withInnerHTML({
            textContent: '', innerHTML: '', style: {}, children: [],
            setAttribute: noop,
            appendChild(child) { this.children.push(child); },
        });
    }
    function element(id) {
        if (!elements.has(id)) elements.set(id, withInnerHTML({
            value: '', textContent: '', innerHTML: '', style: {}, files: [], checked: false,
            classList: { add: noop, remove: noop, toggle: noop },
            children: [], addEventListener: noop,
            options: [], removeAttribute: noop, scrollIntoView: noop, load: noop, pause: noop, play: async () => {},
            appendChild(child) { this.children.push(child); this.options.push(child); }, getAttribute: () => '',
        }));
        return elements.get(id);
    }
    class FormDataStub extends Map {
        append(key, value) { this.set(key, value); }
    }
    const context = vm.createContext({
        console, AbortController, FormData: FormDataStub,
        document: { addEventListener: noop, getElementById: element, querySelectorAll: () => [], createElement: node },
        localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
        window: { confirm: () => true },
        setTimeout: fn => { fn(); return 1; }, clearTimeout: noop,
        setInterval: fn => { const id = intervals.size + 1; intervals.set(id, fn); return id; },
        clearInterval: id => intervals.delete(id),
        fetch: async (url, options) => {
            requests.push({ url, body: options.body instanceof FormDataStub ? Object.fromEntries(options.body) : JSON.parse(options.body) });
            return { ok: true, json: async () => ({ translated_text: 'សួស្តី', engine_used: 'openai', job_id: 'test', segments: [] }) };
        },
    });
    for (const filename of ['app.js', 'srt_parser.js', 'video_dubber.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js', filename), 'utf8'), context);
    }
    const renderStudio = context.renderStudioCueCards;
    const pollJob = context.startPollingJobStatus;
    vm.runInContext(`showToast = () => {}; setPipelineUIState = () => {}; startPollingJobStatus = () => {};
        markStudioDirty = () => {}; renderStudioCueCards = () => {};`, context);
    element('source-textarea').value = 'Hello';
    element('source-lang-select').value = 'en';
    element('target-lang-select').value = 'km';
    return { context, element, storage, requests, poll: pollJob, render: () => renderStudio(), run: source => vm.runInContext(source, context) };
}

const preferences = { engine: 'openai', openaiKey: 'test-key', openaiUrl: 'http://localhost:11434/v1', openaiModel: 'local-model' };

function assertProvider(body) {
    assert.equal(body.engine, 'openai');
    assert.equal(body.api_key, preferences.openaiKey);
    assert.equal(body.base_url, preferences.openaiUrl);
    assert.equal(body.model, preferences.openaiModel);
}

test('text translation sends the selected provider settings', async () => {
    const h = harness(preferences);
    await h.run('executeTranslation()');
    assertProvider(h.requests[0].body);
    assert.equal(h.element('target-output').textContent, 'សួស្តី');
});

test('subtitle translation uses the same provider settings', async () => {
    const h = harness(preferences);
    h.run(`loadedSubtitles = [{id: 1, timestamp: '00:00:01,000 --> 00:00:02,000', text: 'Hello', translation: ''}];`);
    await h.run('startBatchTranslate()');
    assertProvider(h.requests[0].body);
    assert.equal(h.run('loadedSubtitles[0].translation'), 'សួស្តី');
});

test('URL dubbing sends provider settings', async () => {
    const h = harness(preferences);
    h.element('video-url-input').value = 'https://example.com/movie.mp4';
    await h.run('handleUrlDubbing()');
    assertProvider(h.requests[0].body);
});

test('uploaded dubbing sends provider settings in multipart form data', async () => {
    const h = harness(preferences);
    h.element('video-file-input').files = [{ name: 'movie.mp4' }];
    await h.run('handleFileDubbing()');
    assertProvider(h.requests[0].body);
});

test('dialogue polishing sends provider settings', async () => {
    const h = harness(preferences);
    h.run(`studioJobId = 'test';`);
    await h.run('studioPolishDialogue()');
    assertProvider(h.requests[0].body);
});

test('obsolete provider preferences are removed without losing supported settings', () => {
    const h = harness({ ...preferences, engine: 'obsolete', obsoleteKey: 'discard-me', obsoleteModel: 'old-model' });
    const settings = JSON.parse(h.run('JSON.stringify(getSavedSettings())'));
    assert.deepEqual(settings, { ...preferences, engine: 'auto' });
    assert.deepEqual(JSON.parse(h.storage.get('khmer_translator_settings')), settings);
});

test('free mode sends no provider credentials', () => {
    const h = harness({ ...preferences, engine: 'free' });
    assert.deepEqual(JSON.parse(h.run('JSON.stringify(getTranslationOptions())')), { engine: 'free', api_key: null, base_url: null, model: null });
});

test('settings dialog saves and restores the OpenAI model', () => {
    const h = harness(preferences);
    h.run('openSettingsModal()');
    assert.equal(h.element('setting-openai-model').value, 'local-model');
    assert.equal(h.element('openai-key-group').style.display, 'flex');
    h.element('setting-openai-model').value = 'another-model';
    h.run('checkBackendStatus = () => {}; saveSettings()');
    assert.equal(JSON.parse(h.storage.get('khmer_translator_settings')).openaiModel, 'another-model');
});

test('time edits, nudging, shifting, and merging update cue anchors', () => {
    const h = harness();
    h.run(`studioSegments = [
        {id:1,start:1,end:2,target_start:1,target_end:2,text:'one',translation:'one'},
        {id:2,start:3,end:4,target_start:3,target_end:4,text:'two',translation:'two'}
    ];`);
    h.run("studioSetTime(0, 'start', '1.5')");
    assert.equal(h.run('studioSegments[0].target_start'), 1.5);
    h.run("studioNudgeTime(0, 'end', 0.5)");
    assert.equal(h.run('studioSegments[0].target_end'), 2.5);
    h.run('studioShiftFixCue(1)');
    assert.equal(h.run('studioSegments[1].target_start'), 2.58);
    h.run('studioMergeWithNext(0)');
    assert.equal(h.run('studioSegments[0].target_end'), 3.58);
});

test('missing translations show a count, warning, and enabled repair action', () => {
    const h = harness();
    h.run(`studioSegments = [{id:1404,start:1,end:2,text:'书籍乃是圣道载体'}];`);
    h.render();
    assert.match(h.element('studio-cues-count').textContent, /1 untranslated/);
    assert.match(h.element('studio-cue-cards-list').children[0].innerHTML, /Khmer translation is missing/);
    assert.equal(h.element('btn-translate-missing').disabled, false);
});

test('repair fills missing dialogue and preserves existing edits', async () => {
    const h = harness(preferences);
    h.run(`studioJobId = 'test'; studioSegments = [
        {id:1,start:1,end:2,text:'one',translation:'keep this edit'},
        {id:2,start:2,end:3,text:'two'}
    ];`);
    h.context.fetch = async (url, options) => {
        assert.match(url, /\/translate-missing$/);
        const body = JSON.parse(options.body);
        assertProvider(body);
        assert.equal(body.segments[0].translation, 'keep this edit');
        return { ok: true, json: async () => ({repaired_count:1,remaining_count:0,segments:[
            {...body.segments[0],translation:'must not overwrite'},
            {...body.segments[1],translation:'ពីរ',_dirty:true,audio_file:null}
        ]}) };
    };
    await h.run('studioTranslateMissing()');
    assert.equal(h.run('studioSegments[0].translation'), 'keep this edit');
    assert.equal(h.run('studioSegments[1].translation'), 'ពីរ');
    assert.equal(h.run('studioSegments[1]._dirty'), true);
});

test('repair keeps text and timing edited while the request was running', async () => {
    const h = harness(preferences);
    h.run(`studioJobId = 'test'; studioSegments = [{id:1,start:1,end:2,text:'one'}];`);
    h.context.fetch = async () => {
        h.run(`studioSegments[0].translation = 'typed during repair'; studioSegments[0].start = 1.5;`);
        return {ok:true,json:async()=>({repaired_count:1,remaining_count:0,segments:[{id:1,start:1,end:2,text:'one',translation:'មួយ'}]})};
    };
    await h.run('studioTranslateMissing()');
    assert.equal(h.run('studioSegments[0].translation'), 'typed during repair');
    assert.equal(h.run('studioSegments[0].start'), 1.5);
});

test('failed jobs retain an editable studio for translation recovery', () => {
    const h = harness();
    h.run(`displayCompletedVideo({id:'test',status:'failed',result:null,segments:[{id:1,start:1,end:2,text:'one'}]})`);
    assert.equal(h.run('studioJobId'), 'test');
    assert.equal(h.run('studioSegments.length'), 1);
    assert.equal(h.element('video-results-card').style.display, 'block');
    assert.equal(h.element('dubbed-video-player').style.display, 'none');
    assert.equal(h.element('download-dubbed-video-btn').disabled, true);
});

test('URL and upload requests include logo, blur, and caption preferences', async () => {
    for (const mode of ['url', 'upload']) {
        const h = harness(preferences);
        h.element('video-add-logo').checked = true;
        h.element('video-logo-id').value = 'khmerdub';
        h.element('video-blur-subtitles').checked = true;
        h.element('video-blur-y').value = '75';
        h.element('burn-subs-checkbox').checked = true;
        if (mode === 'url') {
            h.element('video-url-input').value = 'https://example.com/movie.mp4';
            await h.run('handleUrlDubbing()');
        } else {
            h.element('video-file-input').files = [{name:'movie.mp4'}];
            await h.run('handleFileDubbing()');
        }
        const body = h.requests[0].body;
        assert.equal(String(body.add_logo), 'true');
        assert.equal(body.logo_id, 'khmerdub');
        assert.equal(String(body.blur_chinese_subtitles), 'true');
        assert.equal(Number(body.blur_y), .75);
        assert.equal(String(body.burn_subtitles), 'true');
    }
});

test('effects persist for future videos and saved jobs restore their settings', () => {
    const h = harness();
    h.element('video-add-logo').checked = true;
    h.element('video-logo-id').value = 'khmerdub';
    h.run("onVideoEffectsChange('video')");
    assert.equal(JSON.parse(h.storage.get('khmer_video_render_options')).add_logo, true);
    h.run(`displayCompletedVideo({id:'test',result:{download_url:'test.mp4'},segments:[],render_options:{logo_id:'khmerdub',add_logo:true,blur_chinese_subtitles:true,burn_subtitles:true}})`);
    assert.equal(h.element('studio-add-logo').checked, true);
    assert.equal(h.element('studio-logo-id').value, 'khmerdub');
    assert.equal(h.element('studio-burn-subs-checkbox').checked, true);
});

test('visual-only re-rendering reuses the completed audio track', async () => {
    const h = harness();
    h.run(`displayCompletedVideo({id:'test',result:{download_url:'test.mp4',dubbed_video_path:'test.mp4'},segments:[]})`);
    h.element('studio-add-logo').checked = true;
    h.context.fetch = async (url, options) => {
        assert.equal(url, '/api/video/job/test/visual-update');
        const body = JSON.parse(options.body);
        assert.equal(body.add_logo, true);
        assert.equal(body.voice_rate, undefined);
        return {ok:true,json:async()=>({status:'processing',job_id:'test'})};
    };
    await h.run('studioRerenderVideo()');
});

test('a changed dialogue timestamp requires rebuilding the audio timeline', async () => {
    const h = harness();
    h.run(`displayCompletedVideo({id:'test',result:{download_url:'test.mp4',dubbed_video_path:'test.mp4'},segments:[{id:1,start:1,end:2,text:'one',translation:'មួយ'}]}); studioSegments[0].start=1.5;`);
    h.context.fetch = async url => {
        assert.equal(url, '/api/video/job/test/resume');
        return {ok:true,json:async()=>({status:'processing',job_id:'test'})};
    };
    await h.run('studioRerenderVideo()');
});

test('custom logos are uploaded and selected without using a client file path', async () => {
    const h = harness();
    h.context.fetch = async url => {
        assert.equal(url, '/api/video/logo');
        return {ok:true,json:async()=>({logo_id:'custom_11111111111111111111111111111111'})};
    };
    h.context.testLogo = {name:'logo.png'};
    await h.run("uploadVideoLogo('video', testLogo)");
    assert.equal(h.element('video-logo-id').value, 'custom_11111111111111111111111111111111');
    assert.equal(h.element('video-add-logo').checked, true);
});

test('Khmer polishing receives the current edited cue text', async () => {
    const h = harness(preferences);
    h.run(`studioJobId='test';studioSegments=[{id:1,start:1,end:2,text:'original',translation:'my edit'}]`);
    await h.run('studioPolishDialogue()');
    assert.equal(h.requests[0].body.segments[0].translation, 'my edit');
});

test('a failed job continues in the background without another upload', async () => {
    const h = harness();
    h.run(`displayCompletedVideo({id:'failed-job',status:'failed',result:null,segments:[{id:1,start:1,end:2,text:'one',translation:'មួយ'}]})`);
    h.context.fetch = async url => {
        assert.equal(url, '/api/video/job/failed-job/resume');
        return {ok:true,json:async()=>({job_id:'failed-job',status:'processing'})};
    };
    await h.run('studioRerenderVideo()');
    assert.equal(h.run('activeVideoJobId'), 'failed-job');
});

test('original-speech recovery is visible in the cue editor', () => {
    const h = harness();
    h.run(`studioSegments=[{id:1,start:0,end:1,text:'source',english_intent:'Hello',translation_error:'retry exhausted',audio_source:'original_fallback',generation_warning:'Original speech used after retries; review this cue.'}];`);
    h.render();
    assert.match(h.element('studio-cue-cards-list').children[0].innerHTML, /Original speech used after retries/);
});

test('repeated clicks during submission send one upload request', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name:'video.mp4'}];
    let finish;
    let requests = 0;
    h.context.fetch = async () => {
        requests++;
        return await new Promise(resolve => { finish = resolve; });
    };
    const first = h.run('handleFileDubbing()');
    await h.run('handleFileDubbing()');
    assert.equal(requests, 1);
    assert.equal(h.element('start-file-dub-btn').disabled, true);
    finish({ok:true,json:async()=>({job_id:'same-job'})});
    await first;
    assert.equal(h.element('start-file-dub-btn').disabled, false);
});

test('a duplicate offers the saved job and only starts a new run after choosing it', async () => {
    const h = harness();
    h.element('video-url-input').value = 'https://example.com/video.mp4';
    const requests = [];
    h.context.fetch = async (url, options) => {
        const body = JSON.parse(options.body);
        requests.push(body);
        return body.force_new
            ? {ok:true,json:async()=>({job_id:'new-job'})}
            : {ok:false,status:409,json:async()=>({detail:{job_id:'saved-job',status:'interrupted',progress:71,stage:'Generating voices'}})};
    };
    await h.run('handleUrlDubbing()');
    assert.equal(h.element('duplicate-job-card').style.display, 'block');
    assert.match(h.element('duplicate-job-details').textContent, /saved-job.*71%/);
    assert.equal(h.run('activeVideoJobId'), null);
    await h.run("chooseDuplicateJob('new')");
    assert.equal(requests.length, 2);
    assert.equal(requests[1].force_new, true);
    assert.equal(h.run('activeVideoJobId'), 'new-job');
});

test('a late reply from another job cannot roll back the currently tracked progress', async () => {
    const h = harness();
    let finishOld;
    const urls = [];
    h.context.fetch = async url => {
        urls.push(url);
        if (url.includes('/old?')) return await new Promise(resolve => { finishOld = resolve; });
        return {ok:true,json:async()=>({id:'current',status:'processing',progress:60,title:'Current video',stage:'Translating'})};
    };
    h.poll('old');
    h.poll('current');
    await new Promise(setImmediate);
    finishOld({ok:true,json:async()=>({id:'old',status:'processing',progress:25,title:'Old video'})});
    await new Promise(setImmediate);
    assert.equal(h.element('pipeline-percent-text').textContent, '60%');
    assert.equal(h.element('pipeline-job-title').textContent, 'Current video');
    assert.equal(h.element('pipeline-job-id').textContent, 'Job current');
    assert.equal(h.storage.get('khmer_active_video_job'), 'current');
    assert.ok(urls.every(url => url.endsWith('?summary=true')));
});

test('a successful duplicate response opens the choice panel without reporting upload failure', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name:'video.mp4'}];
    h.context.fetch = async () => ({ok:true,json:async()=>({
        job_id:'saved-job',status:'completed',reused:true,
        existing_job:{job_id:'saved-job',status:'completed',progress:100,stage:'Complete'}
    })});
    await h.run('handleFileDubbing()');
    assert.equal(h.element('duplicate-job-card').style.display, 'block');
    assert.match(h.element('duplicate-job-details').textContent, /saved-job.*100%/);
    assert.equal(h.run('activeVideoJobId'), null);
    assert.equal(h.element('start-file-dub-btn').disabled, false);
});

test('seven videos upload three at a time and each gets a separate tracked job', async () => {
    const h = harness(preferences);
    const files = Array.from({length: 7}, (_, index) => ({name: `video${index}.mp4`, size: 100}));
    h.element('video-file-input').files = files;
    const pending = [];
    let active = 0, peak = 0, submitted = 0;
    h.context.fetch = async (url, options) => {
        assert.equal(url, '/api/video/dub-upload');
        assertProvider(Object.fromEntries(options.body));
        const file = options.body.get('file');
        submitted++;
        peak = Math.max(peak, ++active);
        await new Promise(resolve => pending.push(() => { active--; resolve(); }));
        return {ok: true, json: async () => ({job_id: file.name, status: 'queued'})};
    };
    const uploading = h.run('handleFileDubbing()');
    assert.equal(submitted, 3);
    await h.run('handleFileDubbing()');
    assert.equal(submitted, 3);
    while (pending.length) {
        pending.shift()();
        await new Promise(setImmediate);
    }
    await uploading;
    assert.equal(submitted, 7);
    assert.equal(peak, 3);
    assert.equal(h.run('videoJobSummaries.size'), 7);
    assert.equal(h.run('activeVideoJobId'), 'video0.mp4');
    assert.equal(h.run('selectedVideoFiles.length'), 0);
    assert.match(h.element('video-upload-status').textContent, /7\/7.*7 added.*0 failed/);
    assert.equal(h.element('video-file-input').disabled, false);
});

test('failed uploads retain their matching subtitles for retry while other jobs are kept', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name: 'one.mp4'}, {name: 'two.mp4'}, {name: 'three.mp4'}];
    h.element('video-sub-file-input').files = [{name: 'THREE.srt'}, {name: 'two.vtt'}, {name: 'one.srt'}];
    const sent = [];
    let failTwo = true;
    h.context.fetch = async (url, options) => {
        const file = options.body.get('file');
        sent.push([file.name, options.body.get('subtitle_file').name]);
        return file.name === 'two.mp4' && failTwo
            ? {ok:false, status:500, json:async()=>({detail:'Upload unavailable'})}
            : {ok:true, json:async()=>({job_id:file.name, status:'queued'})};
    };
    await h.run('handleFileDubbing()');
    assert.equal(h.run('videoJobSummaries.size'), 2);
    assert.equal(h.run('selectedVideoFiles[0].name'), 'two.mp4');
    assert.match(h.element('video-upload-status').textContent, /two.mp4: Upload unavailable/);
    assert.deepEqual(sent, [['one.mp4','one.srt'], ['two.mp4','two.vtt'], ['three.mp4','THREE.srt']]);
    failTwo = false;
    await h.run('handleFileDubbing()');
    assert.deepEqual(sent[3], ['two.mp4', 'two.vtt']);
    assert.equal(sent.length, 4);
    assert.equal(h.run('videoJobSummaries.size'), 3);
});

test('ambiguous or unmatched sidecar subtitles are rejected before sending any videos', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name:'one.mp4'}, {name:'two.mp4'}];
    h.element('video-sub-file-input').files = [{name:'unrelated.srt'}];
    await h.run('handleFileDubbing()');
    assert.equal(h.requests.length, 0);
    h.element('video-sub-file-input').files = [{name:'one.srt'}, {name:'one.vtt'}];
    await h.run('handleFileDubbing()');
    assert.equal(h.requests.length, 0);
});

test('a duplicate in a multi-video upload reuses that job and still queues the other videos', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name:'saved.mp4'}, {name:'new.mp4'}, {name:'third.mp4'}];
    h.context.fetch = async (url, options) => {
        const file = options.body.get('file');
        return file.name === 'saved.mp4'
            ? {ok:true,json:async()=>({reused:true,existing_job:{job_id:'saved',status:'interrupted',progress:71}})}
            : {ok:true,json:async()=>({job_id:file.name,status:'queued'})};
    };
    await h.run('handleFileDubbing()');
    assert.equal(h.run('videoJobSummaries.size'), 3);
    assert.equal(h.run("videoJobSummaries.get('saved').progress"), 71);
    assert.match(h.element('video-upload-status').textContent, /2 added.*1 saved jobs.*0 failed/);
});

test('choosing a new run for a duplicate file retains the original file if selection changes', async () => {
    const h = harness();
    h.element('video-file-input').files = [{name:'original.mp4'}];
    const uploads = [];
    h.context.fetch = async (url, options) => {
        uploads.push(options.body.get('file').name);
        return options.body.get('force_new') === 'true'
            ? {ok:true,json:async()=>({job_id:'new'})}
            : {ok:true,json:async()=>({reused:true,existing_job:{job_id:'old',status:'completed',progress:100}})};
    };
    await h.run('handleFileDubbing()');
    h.element('video-file-input').files = [{name:'different.mp4'}];
    await h.run("chooseDuplicateJob('new')");
    assert.deepEqual(uploads, ['original.mp4', 'original.mp4']);
});

test('refreshing all jobs preserves the active studio and shows independent progress and downloads', async () => {
    const h = harness();
    h.run("activeVideoJobId = 'editing'; studioDirty = true;");
    h.context.fetch = async () => ({ok:true,json:async()=>({active_count:2,queued_count:1,max_concurrent_jobs:3,jobs:[
        {id:'one',title:'<img src=x>',status:'processing',progress:45,stage:'Translating'},
        {id:'two',title:'Second',status:'queued',progress:0},
        {id:'ready',title:'Ready',status:'completed',progress:100,download_url:'/static/dubbed/ready.mp4'}
    ]})});
    await h.run('refreshVideoJobs()');
    assert.equal(h.run('activeVideoJobId'), 'editing');
    assert.equal(h.run('studioDirty'), true);
    assert.equal(h.run("videoJobSummaries.get('one').progress"), 45);
    assert.match(h.element('video-jobs-summary').textContent, /1 \/ 3 running.*1 queued/);
    const rows = h.element('video-jobs-list').children;
    assert.equal(rows[0].children[0].children[0].textContent, '<img src=x>');
    assert.equal(rows[2].children[1].children[1].href, '/static/dubbed/ready.mp4');
});

for (const action of ['studioPolishDialogue()', 'studioTranslateMissing()', 'studioAutoFixOverlaps()']) {
    test(`switching tasks while ${action} runs preserves the newly opened task`, async () => {
        const h = harness();
        h.run("studioJobId = 'first'; studioSegments = [{id:1,start:0,end:1,text:'First'}];");
        let finish;
        const urls = [];
        h.context.fetch = async url => {
            urls.push(url);
            if (url.endsWith('/segments')) return {ok:true};
            return await new Promise(resolve => { finish = resolve; });
        };
        const pending = h.run(action);
        await new Promise(setImmediate);
        h.run("studioJobId = 'second'; trackingGeneration++; studioSegments = [{id:1,text:'Second'}];");
        finish({ok:true,json:async()=>({segments:[{id:1,text:'First',translation:'Original task result'}],repaired_count:1})});
        await pending;
        assert.ok(urls.every(url => url.includes('/job/first/')));
        assert.equal(h.run('studioSegments[0].text'), 'Second');
        assert.equal(h.run('studioSegments[0].translation'), undefined);
    });
}

test('a render acknowledgement for another task does not switch the selected job', async () => {
    const h = harness();
    h.run("studioJobId = 'first'; activeVideoJobId = 'first';");
    let finish;
    h.context.fetch = async () => await new Promise(resolve => { finish = resolve; });
    const pending = h.run('studioRerenderVideo()');
    h.run("studioJobId = 'second'; activeVideoJobId = 'second'; trackingGeneration++;");
    finish({ok:true,json:async()=>({job_id:'first',status:'queued'})});
    await pending;
    assert.equal(h.run('activeVideoJobId'), 'second');
});

test('running tasks appear before queued and completed jobs with their own actions', async () => {
    const h = harness();
    h.run(`activeVideoJobId = 'b';
        videoJobSummaries.set('done',{id:'done',status:'completed',progress:100});
        videoJobSummaries.set('waiting',{id:'waiting',status:'queued',progress:20});
        for (const id of ['a','b','c']) videoJobSummaries.set(id,{id,status:'processing',progress:30});
        renderVideoJobs();`);
    const rows = h.element('video-jobs-list').children;
    assert.deepEqual(rows.map(row => row.children[0].children[0].textContent), ['a','b','c','waiting','done']);
    assert.equal(rows[1].children[1].children[0].textContent, 'Viewing');
    assert.match(rows[0].children[0].children[1].textContent, /^Running/);
    assert.match(rows[3].children[0].children[1].textContent, /^Queued/);
});

test('removing the selected task clears its studio and ignores an outstanding progress response', async () => {
    const h = harness();
    h.run("studioJobId = 'remove'; studioSegments = [{id:1,text:'old'}]; videoJobSummaries.set('remove',{id:'remove'}); videoJobSummaries.set('keep',{id:'keep'});");
    let stale;
    h.context.fetch = async (url, options) => {
        if (options?.method === 'DELETE') return {ok:true,json:async()=>({status:'removed'})};
        return await new Promise(resolve => { stale = resolve; });
    };
    h.poll('remove');
    await h.run("removeVideoTask('remove')");
    stale({ok:true,json:async()=>({id:'remove',status:'processing',progress:90})});
    await new Promise(setImmediate);
    assert.equal(h.run('activeVideoJobId'), null);
    assert.equal(h.run('studioJobId'), null);
    assert.equal(h.run('studioSegments.length'), 0);
    assert.equal(h.run('videoJobSummaries.size'), 1);
    assert.equal(h.storage.has('khmer_active_video_job'), false);
    assert.equal(h.element('video-jobs-list').children.length, 1);
});

test('a failed task deletion preserves the task and current selection', async () => {
    const h = harness();
    h.run("activeVideoJobId = 'keep'; videoJobSummaries.set('keep',{id:'keep'});");
    h.context.fetch = async () => ({ok:false,status:500,json:async()=>({detail:'Could not stop worker'})});
    await h.run("removeVideoTask('keep')");
    assert.equal(h.run('activeVideoJobId'), 'keep');
    assert.equal(h.run('videoJobSummaries.has("keep")'), true);
    assert.equal(h.run('removingVideoJobs.size'), 0);
});

test('a task list response from before deletion cannot bring the removed card back', async () => {
    const h = harness();
    let stale;
    h.context.fetch = async (url, options) => options?.method === 'DELETE'
        ? {ok:true,json:async()=>({status:'removed'})}
        : await new Promise(resolve => { stale = resolve; });
    const refreshing = h.run('refreshVideoJobs()');
    await h.run("removeVideoTask('old')");
    stale({ok:true,json:async()=>({jobs:[{id:'old'}],active_count:0,queued_count:0,max_concurrent_jobs:3})});
    await refreshing;
    assert.equal(h.run('videoJobSummaries.has("old")'), false);
});

test('clearing tasks resets selections and progress but retains provider preferences', async () => {
    const h = harness(preferences);
    h.run("activeVideoJobId = 'old'; studioJobId = 'old'; videoJobSummaries.set('old',{id:'old'}); selectedVideoFiles = [{name:'old.mp4'}];");
    h.storage.set('khmer_active_video_job', 'old');
    h.context.fetch = async (url, options) => ({ok:true,json:async()=> options?.method === 'DELETE'
        ? {status:'cleared',cache:{deleted_files:10}}
        : {jobs:[],active_count:0,processing_count:0,queued_count:0,max_concurrent_jobs:3}});
    await h.run('clearVideoTasksAndCache()');
    await new Promise(setImmediate);
    assert.equal(h.run('videoJobSummaries.size'), 0);
    assert.equal(h.run('savedPipelineProgress.size'), 0);
    assert.equal(h.run('selectedVideoFiles.length'), 0);
    assert.equal(h.run('activeVideoJobId'), null);
    assert.equal(h.storage.has('khmer_active_video_job'), false);
    assert.equal(JSON.parse(h.storage.get('khmer_translator_settings')).openaiKey, preferences.openaiKey);
});

test('paused cards offer Resume and allow only the latest saved checkpoint to be cleared', () => {
    const h = harness();
    h.run(`videoJobSummaries.set('paused', {id:'paused',status:'paused',progress:80,steps:[
        {id:'prepare',number:1,label:'Prepare video',state:'complete',can_clear:false},
        {id:'transcription',number:2,label:'Transcription',state:'complete',can_clear:false},
        {id:'translation',number:3,label:'Khmer translation',state:'complete',can_clear:false},
        {id:'voices',number:4,label:'Khmer voices',state:'partial',can_clear:true},
        {id:'render',number:5,label:'Final video',state:'pending',can_clear:false}
    ]}); expandedVideoJobSteps.add('paused'); renderVideoJobs();`);
    const row = h.element('video-jobs-list').children[0];
    const buttons = row.children[1].children.map(button => button.textContent);
    assert.ok(buttons.includes('Resume'));
    assert.ok(!buttons.includes('Pause'));
    const details = row.children[2];
    assert.equal(details.open, true);
    const translation = details.children[4];
    const voices = details.children[5];
    assert.equal(translation.children[1].disabled, true);
    assert.match(translation.children[1].title, /step 4/);
    assert.equal(voices.children[1].disabled, false);
    h.run('renderVideoJobs()');
    assert.equal(h.element('video-jobs-list').children[0].children[2].open, true);
});

test('running cards offer Pause and lock checkpoint clearing', () => {
    const h = harness();
    h.run(`videoJobSummaries.set('running',{id:'running',status:'processing',progress:60,
        steps:[{id:'translation',number:3,label:'Khmer translation',state:'partial',can_clear:false}]}); renderVideoJobs();`);
    const row = h.element('video-jobs-list').children[0];
    assert.ok(row.children[1].children.some(button => button.textContent === 'Pause'));
    const clear = row.children[2].children[2].children[1];
    assert.equal(clear.disabled, true);
    assert.match(clear.title, /Pause/);
});

test('clearing a checkpoint sends one scoped DELETE and discards stale studio and progress state', async () => {
    const h = harness();
    h.run(`activeVideoJobId='selected'; studioJobId='selected'; studioSegments=[{id:1,translation:'old'}];
        videoJobSummaries.set('selected',{id:'selected',status:'paused',progress:85});
        videoJobSummaries.set('keep',{id:'keep',status:'paused'});`);
    const calls = [];
    h.context.fetch = async (url, options) => {
        calls.push({url,method:options?.method});
        if (options?.method === 'DELETE') return {ok:true,json:async()=>({status:'paused',progress:70,stage:'Voices cleared',run_id:'new',steps:[]})};
        return {ok:true,json:async()=>({jobs:[{id:'selected',status:'paused',progress:70},{id:'keep',status:'paused'}],active_count:0,queued_count:0,max_concurrent_jobs:3})};
    };
    await h.run("changeVideoCheckpoint('selected','clear','voices')");
    await new Promise(setImmediate);
    assert.deepEqual(calls.filter(call => call.method === 'DELETE'), [{url:'/api/video/job/selected/steps/voices',method:'DELETE'}]);
    assert.equal(h.run('studioSegments.length'), 0);
    assert.equal(h.run('studioJobId'), null);
    assert.equal(h.run('activeVideoJobId'), 'selected');
    assert.equal(h.run("videoJobSummaries.has('keep')"), true);
    assert.equal(h.element('pipeline-percent-text').textContent, '70%');
    assert.equal(h.run('changingVideoJobs.size'), 0);
});

test('repeated Pause clicks send one request while pause is pending', async () => {
    const h = harness();
    let release;
    let pauses = 0;
    h.context.fetch = async (url, options) => {
        if (options?.method === 'POST') {
            pauses++;
            return await new Promise(resolve => { release = resolve; });
        }
        return {ok:true,json:async()=>({jobs:[{id:'one',status:'paused'}],active_count:0,queued_count:0,max_concurrent_jobs:3})};
    };
    const first = h.run("changeVideoCheckpoint('one','pause')");
    await h.run("changeVideoCheckpoint('one','pause')");
    assert.equal(pauses, 1);
    release({ok:true,json:async()=>({status:'paused'})});
    await first;
    assert.equal(h.run('changingVideoJobs.size'), 0);
});

test('checkpoint failures preserve current dialogue and percentage', async () => {
    const h = harness();
    h.run("activeVideoJobId='keep'; studioJobId='keep'; studioSegments=[{id:1,translation:'saved'}];");
    h.element('pipeline-percent-text').textContent = '85%';
    h.context.fetch = async () => ({ok:false,status:409,json:async()=>({detail:'Clear step 5 first'})});
    await h.run("changeVideoCheckpoint('keep','clear','translation')");
    assert.equal(h.run('studioSegments[0].translation'), 'saved');
    assert.equal(h.element('pipeline-percent-text').textContent, '85%');
    assert.equal(h.run('changingVideoJobs.size'), 0);
});
