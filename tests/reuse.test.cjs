const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness() {
    const elements = new Map(), requests = [], timers = new Map();
    let ready;
    function node() {
        return { value: '', textContent: '', children: [], files: [], dataset: {}, disabled: false,
            appendChild(child) { this.children.push(child); }, append(...children) { this.children.push(...children); },
            replaceChildren() { this.children = []; }, setAttribute() {} };
    }
    function element(id) { if (!elements.has(id)) elements.set(id, node()); return elements.get(id); }
    class Form {
        constructor() { this.fields = {}; }
        append(key, value) { this.fields[key] = value; }
    }
    const context = vm.createContext({ console, URLSearchParams, FormData: Form,
        window: { location: { search: '?job=ready' } },
        document: { getElementById: element, createElement: node, addEventListener(name, fn) { ready = fn; } },
        setTimeout(fn, delay) { const id = timers.size + 1; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); },
        fetch: async (url, options = {}) => {
            requests.push({url, options});
            const data = url === '/api/video/export-jobs' ? {jobs: [
                {id: 'ready', title: '<script>Episode 1</script>', status: 'completed', export_ready: true, ready_voices: 5, voice_cues: 5},
                {id: 'missing', title: 'Episode 2', status: 'paused', export_ready: false, ready_voices: 3, voice_cues: 5},
            ]} : url === '/api/video/exports' && !options.method ? {exports: []} : {id: 'a'.repeat(32), status: 'queued'};
            return {ok: true, json: async () => data};
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/reuse.js'), 'utf8'), context);
    return {context, element, requests, timers, run: code => vm.runInContext(code, context), start: () => ready()};
}

test('linked job is selected and only ready jobs can be exported', async () => {
    const h = harness();
    await h.start();
    assert.equal(h.element('selection-count').textContent, '1 selected');
    const rows = h.element('job-list').children;
    assert.equal(rows[0].children[0].checked, true);
    assert.equal(rows[1].children[0].disabled, true);
    assert.equal(rows[0].children[1].children[0].textContent, '<script>Episode 1</script>');
    assert.equal(h.element('replacement-job').value, 'ready');
    assert.equal(h.element('replacement-job').children.length, 2);
    await h.run('reuseExportSelected()');
    const post = h.requests.find(request => request.options.method === 'POST');
    assert.deepEqual(JSON.parse(post.options.body), {job_ids: ['ready']});
});

test('replacement upload sends the chosen visual file, ending and caption preference', async () => {
    const h = harness();
    await h.start();
    const file = {name: 'own-footage.mp4'};
    h.element('replacement-file').files = [file];
    h.element('replacement-ending').value = 'freeze';
    h.element('replacement-captions').checked = true;
    await h.context.reuseRenderReplacement({preventDefault() {}});
    const post = h.requests.find(request => request.options.method === 'POST');
    assert.equal(post.url, '/api/video/job/ready/replacement-video');
    assert.equal(post.options.body.fields.file, file);
    assert.equal(post.options.body.fields.ending, 'freeze');
    assert.equal(post.options.body.fields.burn_subtitles, 'true');
    assert.equal(h.element('render-replacement').disabled, false);
});

test('failed requests preserve selections and show the server explanation', async () => {
    const h = harness();
    await h.start();
    h.context.fetch = async () => ({ok: false, json: async () => ({detail: 'Retry missing Khmer voices first'})});
    await h.run('reuseExportSelected()');
    assert.equal(h.element('message').textContent, 'Retry missing Khmer voices first');
    assert.equal(h.element('message').dataset.kind, 'error');
    assert.equal(h.element('selection-count').textContent, '1 selected');
    assert.equal(h.element('export-selected').disabled, false);
});

test('recent exports restore download links and active tasks keep polling', async () => {
    const h = harness();
    const id = 'b'.repeat(32);
    h.context.fetch = async () => ({ok: true, json: async () => ({exports: [
        {id, kind: 'replacement', status: 'completed', stage: 'Ready', jobs: [{title: 'Episode 1', duration: 20}]},
        {id: 'c'.repeat(32), kind: 'bundle', status: 'processing', progress: 30, stage: 'Exporting', jobs: [{}]},
    ]})});
    await h.run('reuseLoadExports()');
    const completed = h.element('export-list').children[0];
    assert.equal(completed.children[2].href, `/api/video/exports/${id}/download`);
    const files = completed.children[3].children[0].children;
    assert.equal(files[1].href, `/api/video/exports/${id}/files/0/audio`);
    assert.equal(files[2].href, `/api/video/exports/${id}/files/0/subtitles`);
    assert.equal(files[3].href, `/api/video/exports/${id}/files/0/video`);
    assert.ok([...h.timers.values()].some(timer => timer.delay === 2000));
});

test('a refresh requested during an older history request is replayed', async () => {
    const h = harness();
    let resolve;
    h.context.fetch = () => new Promise(done => {resolve = done;});
    const first = h.run('reuseLoadExports()');
    await h.run('reuseLoadExports()');
    resolve({ok: true, json: async () => ({exports: []})});
    await first;
    assert.ok([...h.timers.values()].some(timer => timer.delay === 0));
});

test('more than 100 selected jobs explains the batch limit without submitting', async () => {
    const h = harness();
    h.run('for (let i = 0; i < 101; i++) reuseSelected.add(String(i))');
    await h.run('reuseExportSelected()');
    assert.equal(h.requests.length, 0);
    assert.match(h.element('message').textContent, /up to 100/);
});
