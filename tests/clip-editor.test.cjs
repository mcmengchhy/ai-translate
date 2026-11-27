const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function harness(saved = {}) {
    const elements = new Map(), requests = [], timers = new Map(), revoked = [];
    const storage = new Map([['khmer_clip_editor_settings', JSON.stringify(saved)]]);
    const choices = {
        'clip-flip': ['horizontal', 'none', 'vertical', 'both'],
        'clip-speed': ['0.25', '0.5', '0.75', '1', '1.1', '1.25', '1.5', '2', '3', '4'],
        'clip-color': [...fs.readFileSync(path.join(__dirname, '../frontend/clip-editor.html'), 'utf8')
            .match(/<select id="clip-color">([\s\S]*?)<\/select>/)[1].matchAll(/value="([^"]+)"/g)].map(match => match[1]),
        'clip-effect': ['none', 'ambience', 'transitions', 'both'],
    };
    let ready;
    function node() {
        return {tagName: 'DIV', children: [], dataset: {}, style: {}, files: [], value: '', duration: NaN, paused: true,
            textContent: '', attributes: {}, setAttribute(key, value) {this.attributes[key] = value;},
            append(...children) {this.children.push(...children);}, appendChild(child) {this.children.push(child);},
            replaceChildren(...children) {this.children = children;},
            removeAttribute(key) {delete this[key];}, load() {}, pause() {this.paused = true;}, async play() {this.paused = false;}};
    }
    function element(id) {
        if (!elements.has(id)) {
            const result = node();
            if (choices[id]) {result.tagName = 'SELECT'; result.options = choices[id].map(value => ({value}));}
            elements.set(id, result);
        }
        return elements.get(id);
    }
    class Form {
        constructor() {this.fields = {};}
        append(key, value) {this.fields[key] = value;}
    }
    const context = vm.createContext({console, FormData: Form,
        document: {getElementById: element, createElement: node, addEventListener(name, fn) {ready = fn;}},
        window: {addEventListener() {}}, URL: {createObjectURL: file => `blob:${file.name}`, revokeObjectURL: url => revoked.push(url)},
        localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        setTimeout(fn, delay) {const id = timers.size + 1; timers.set(id, {fn, delay}); return id;},
        clearTimeout(id) {timers.delete(id);},
        fetch: async (url, options = {}) => {
            requests.push({url, options});
            return {ok: true, json: async () => options.method ? {id: 'a'.repeat(32), status: 'queued'} : {exports: []}};
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/clip-editor.js'), 'utf8'), context);
    return {context, element, requests, timers, storage, revoked, run: code => vm.runInContext(code, context), start: () => ready()};
}

test('a direct upload previews saved flip, speed and color without needing a dubbing job', async () => {
    const h = harness({flip: 'both', speed: '1.5', color_filter: 'grayscale', original_volume: '35', sound_effect: 'ambience'});
    h.start();
    const file = {name: 'clip.mp4', size: 2 * 1024 * 1024};
    h.element('clip-file').files = [file];
    h.element('clip-preview').duration = 12;
    h.context.clipChooseFile();
    assert.equal(h.element('clip-preview').src, 'blob:clip.mp4');
    assert.equal(h.element('clip-preview').style.transform, 'scale(-1, -1)');
    assert.equal(h.element('clip-preview').style.filter, 'grayscale(1)');
    assert.equal(h.element('clip-preview').playbackRate, 1.5);
    assert.equal(h.element('clip-preview').volume, .35);
    assert.equal(h.element('clip-preview').preservesPitch, true);
    assert.equal(h.element('clip-render').disabled, false);
    assert.match(h.element('clip-file-info').textContent, /8 seconds/);
    await h.context.clipSubmit({preventDefault() {}});
    const request = h.requests.find(item => item.options.method === 'POST');
    assert.equal(request.url, '/api/video/clip-edits');
    assert.equal(request.options.body.fields.file, file);
    assert.equal(request.options.body.fields.flip, 'both');
    assert.equal(request.options.body.fields.speed, '1.5');
    assert.equal(request.options.body.fields.color_filter, 'grayscale');
    assert.equal(request.options.body.fields.original_volume, '35');
});

test('render requires a file and duplicate clicks send only one upload', async () => {
    const h = harness();
    h.start();
    await h.context.clipSubmit({preventDefault() {}});
    assert.equal(h.requests.some(item => item.options.method), false);
    assert.match(h.element('clip-message').textContent, /Choose a video/);
    h.element('clip-file').files = [{name: 'one.mp4', size: 100}];
    let release;
    h.context.fetch = (url, options) => {
        h.requests.push({url, options});
        return new Promise(resolve => {release = resolve;});
    };
    const pending = h.context.clipSubmit({preventDefault() {}});
    await h.context.clipSubmit({preventDefault() {}});
    assert.equal(h.requests.filter(item => item.options?.method === 'POST').length, 1);
    release({ok: false, json: async () => ({detail: 'Upload failed'})});
    await pending;
    assert.equal(h.element('clip-message').textContent, 'Upload failed');
    assert.equal(h.element('clip-render').disabled, false);
    assert.equal(h.element('clip-file').files[0].name, 'one.mp4');
});

test('invalid saved settings recover to valid defaults and reset is persistent', () => {
    const h = harness({flip: 'bad', speed: '0', color_filter: 'script', original_volume: '-3', effect_volume: 'NaN'});
    h.context.clipRestoreSettings();
    assert.equal(h.element('clip-flip').value, 'horizontal');
    assert.equal(h.element('clip-speed').value, '1');
    assert.equal(h.element('clip-color').value, 'normal');
    assert.equal(h.element('clip-original-volume').value, '100');
    assert.equal(h.element('clip-effect-volume').value, '8');
    h.element('clip-color').value = 'sepia';
    h.context.clipRestoreSettings(true);
    assert.equal(JSON.parse(h.storage.get('khmer_clip_editor_settings')).color_filter, 'normal');
});

test('changing uploads releases the previous preview and clearing disables rendering', () => {
    const h = harness();
    h.context.clipRestoreSettings();
    h.element('clip-file').files = [{name: 'one.mp4', size: 100}];
    h.context.clipChooseFile();
    h.element('clip-file').files = [{name: 'two.mp4', size: 200}];
    h.context.clipChooseFile();
    assert.deepEqual(h.revoked, ['blob:one.mp4']);
    h.element('clip-file').files = [];
    h.context.clipChooseFile();
    assert.equal(h.element('clip-render').disabled, true);
    assert.equal(h.element('clip-preview-controls').hidden, true);
});

test('completed clip history shows an MP4 preview and download using safe text', () => {
    const h = harness();
    const id = 'b'.repeat(32);
    const state = {id, kind: 'clip-edit', status: 'completed', stage: 'Ready', jobs: [{title: '<script>clip</script>', duration: 5}], edit_options: {speed: 2}};
    h.context.stateFixture = [state];
    h.run('clipRenderHistory(stateFixture)');
    const row = h.element('clip-history').children[0];
    assert.equal(row.children[0].textContent, '<script>clip</script>');
    assert.equal(row.children[2].src, `/api/video/exports/${id}/files/0/video`);
    assert.equal(row.children[3].href, `/api/video/exports/${id}/files/0/video`);
    h.run('clipRenderHistory([...stateFixture, {id:"new",kind:"clip-edit",status:"processing",progress:20,stage:"Rendering",jobs:[{}]}])');
    assert.equal(h.element('clip-history').children[0], row, 'An existing result preview is retained');
});

test('history polling restores clips after reopening and retries temporary server failures', async () => {
    const h = harness();
    h.context.fetch = async () => ({ok: true, json: async () => ({exports: [{id: 'c'.repeat(32), kind: 'clip-edit', status: 'processing', stage: 'Rendering', progress: 15, jobs: [{}]}]})});
    await h.context.clipLoadHistory();
    assert.ok([...h.timers.values()].some(timer => timer.delay === 2000));
    h.context.fetch = async () => {throw new Error('Disconnected');};
    await h.context.clipLoadHistory();
    assert.ok([...h.timers.values()].some(timer => timer.delay === 5000));
    assert.equal(h.element('clip-message').textContent, 'Disconnected');
});
