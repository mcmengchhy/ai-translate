/**
 * Khmer AI Translator - Main Application Logic
 */

let debounceTimer = null;
let currentAbortController = null;
let activeTone = "standard";

// Sample sentences for demonstration
const SAMPLE_PRESETS = {
    greeting: "Good morning, everyone! Welcome to Cambodia. I hope you have a wonderful and memorable stay here.",
    business: "Notice: The Ministry of Economy and Finance has issued new guidelines regarding digital taxation effective next month. All registered enterprises must comply.",
    idiom: "Where there is a will, there is a way. Knowledge is the most precious treasure.",
    technology: "Artificial Intelligence and Machine Learning models are rapidly transforming natural language processing for low-resource languages worldwide."
};

// Initial setup on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    initEventListeners();
    loadSavedSettings();
    checkBackendStatus();
    updateCharCounts();
});

function initEventListeners() {
    const sourceTextarea = document.getElementById('source-textarea');
    const translateBtn = document.getElementById('translate-action-btn');
    const swapBtn = document.getElementById('swap-lang-btn');
    const clearBtn = document.getElementById('clear-source-btn');
    const copyBtn = document.getElementById('copy-target-btn');
    const listenSourceBtn = document.getElementById('listen-source-btn');
    const listenTargetBtn = document.getElementById('listen-target-btn');
    const convertNumBtn = document.getElementById('convert-num-btn');
    const toggleZwsp = document.getElementById('toggle-zwsp');
    const presetsBtn = document.getElementById('presets-btn');

    // Debounced live translation on typing
    sourceTextarea.addEventListener('input', () => {
        updateCharCounts();
        clearTimeout(debounceTimer);
        const text = sourceTextarea.value.trim();
        if (text.length > 2 && text.length < 500) {
            debounceTimer = setTimeout(() => {
                executeTranslation();
            }, 800);
        }
    });

    translateBtn.addEventListener('click', () => executeTranslation());
    swapBtn.addEventListener('click', swapLanguages);
    clearBtn.addEventListener('click', clearSource);
    copyBtn.addEventListener('click', copyTranslation);
    listenSourceBtn.addEventListener('click', () => speakText(sourceTextarea.value, document.getElementById('source-lang-select').value));
    listenTargetBtn.addEventListener('click', () => speakText(document.getElementById('target-output').textContent, document.getElementById('target-lang-select').value));
    convertNumBtn.addEventListener('click', toggleTargetNumerals);

    if (toggleZwsp) {
        toggleZwsp.addEventListener('change', () => {
            const current = document.getElementById('target-output').textContent;
            if (current.trim()) {
                executeTranslation();
            }
        });
    }

    // Tone pills selector
    const tonePills = document.querySelectorAll('.tone-pill');
    tonePills.forEach(pill => {
        pill.addEventListener('click', () => {
            tonePills.forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            activeTone = pill.getAttribute('data-tone');
            document.getElementById('detected-tone-info').textContent = `Tone: ${pill.textContent.split(' ')[0]}`;
            if (sourceTextarea.value.trim()) {
                executeTranslation();
            }
        });
    });

    // Examples dropdown
    presetsBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        document.getElementById('presets-menu').classList.toggle('active');
    });

    document.addEventListener('click', () => {
        const menu = document.getElementById('presets-menu');
        if (menu) menu.classList.remove('active');
    });

    // Keyboard shortcuts: Ctrl+Enter or Cmd+Enter to translate; Ctrl+S or Cmd+S to swap
    document.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            executeTranslation();
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && !e.shiftKey) {
            e.preventDefault();
            swapLanguages();
        }
    });

    // File dropzone events
    const dropzone = document.getElementById('file-dropzone');
    const fileInput = document.getElementById('file-input');

    if (dropzone && fileInput) {
        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        });
        dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                handleFileUpload(e.dataTransfer.files[0]);
            }
        });
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFileUpload(e.target.files[0]);
            }
        });
    }

    const startBatchBtn = document.getElementById('start-batch-translate-btn');
    const cancelBatchBtn = document.getElementById('cancel-batch-btn');
    const exportSrtBtn = document.getElementById('export-srt-btn');

    if (startBatchBtn) startBatchBtn.addEventListener('click', startBatchTranslate);
    if (cancelBatchBtn) cancelBatchBtn.addEventListener('click', clearBatch);
    if (exportSrtBtn) exportSrtBtn.addEventListener('click', exportSubtitles);
}

function getActiveTone() {
    return activeTone;
}

function updateCharCounts() {
    const src = document.getElementById('source-textarea').value;
    const tgt = document.getElementById('target-output').textContent;

    document.getElementById('source-char-count').textContent = `${src.length} characters`;
    const words = src.trim() ? src.trim().split(/\s+/).length : 0;
    document.getElementById('source-word-count').textContent = `${words} words`;
    document.getElementById('target-char-count').textContent = `${tgt.length} characters`;
}

// Main Translation Execution
async function executeTranslation() {
    const sourceText = document.getElementById('source-textarea').value.trim();
    const targetOutput = document.getElementById('target-output');
    const spinner = document.getElementById('loading-spinner');
    const engineTag = document.getElementById('result-engine-tag');

    if (!sourceText) {
        targetOutput.textContent = "";
        updateCharCounts();
        return;
    }

    const sourceLang = document.getElementById('source-lang-select').value;
    const targetLang = document.getElementById('target-lang-select').value;
    const injectZwsp = document.getElementById('toggle-zwsp') ? document.getElementById('toggle-zwsp').checked : false;
    const settings = getSavedSettings();

    // Cancel existing request if still pending
    if (currentAbortController) {
        currentAbortController.abort();
    }
    currentAbortController = new AbortController();

    spinner.classList.add('active');
    engineTag.textContent = "Translating...";
    targetOutput.textContent = "";

    try {
        const resp = await fetch('/api/translate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: sourceText,
                source_lang: sourceLang,
                target_lang: targetLang,
                engine: settings.engine || 'auto',
                tone: activeTone,
                inject_zwsp: injectZwsp,
                api_key: settings.geminiKey || null,
                model: settings.geminiModel || null
            }),
            signal: currentAbortController.signal
        });

        if (!resp.ok) {
            const errData = await resp.json().catch(() => ({ detail: `Error ${resp.status}` }));
            throw new Error(errData.detail || "Translation request failed");
        }

        const data = await resp.json();
        targetOutput.textContent = data.translated_text;
        engineTag.textContent = `${data.engine_used.toUpperCase()}`;
        updateCharCounts();
    } catch (err) {
        if (err.name === 'AbortError') return;
        targetOutput.textContent = `Translation Error: ${err.message}`;
        engineTag.textContent = "Error";
        showToast(err.message, "error");
    } finally {
        spinner.classList.remove('active');
    }
}

// Swap languages
function swapLanguages() {
    const srcSelect = document.getElementById('source-lang-select');
    const tgtSelect = document.getElementById('target-lang-select');
    const srcArea = document.getElementById('source-textarea');
    const tgtOutput = document.getElementById('target-output');

    let oldSrc = srcSelect.value;
    let oldTgt = tgtSelect.value;

    if (oldSrc === 'auto') {
        oldSrc = 'en';
    }

    srcSelect.value = oldTgt;
    tgtSelect.value = oldSrc;

    // Swap text content as well
    const translatedText = tgtOutput.textContent.trim();
    if (translatedText && !translatedText.startsWith("Translation Error")) {
        srcArea.value = translatedText;
        executeTranslation();
    }
    updateCharCounts();
}

function clearSource() {
    document.getElementById('source-textarea').value = '';
    document.getElementById('target-output').textContent = '';
    updateCharCounts();
    document.getElementById('result-engine-tag').textContent = 'Ready';
}

function copyTranslation() {
    const text = document.getElementById('target-output').textContent.trim();
    if (!text) {
        showToast("Nothing to copy!", "error");
        return;
    }
    navigator.clipboard.writeText(text).then(() => {
        showToast("Copied translation to clipboard!", "success");
    }).catch(() => {
        showToast("Failed to copy", "error");
    });
}

// Toggle Khmer / Western Numerals in the translated output
async function toggleTargetNumerals() {
    const tgtOutput = document.getElementById('target-output');
    const text = tgtOutput.textContent.trim();
    if (!text) return;

    // Check if contains Khmer digits
    const hasKhmerDigits = /[០-៩]/.test(text);
    const action = hasKhmerDigits ? "to_arabic_num" : "to_khmer_num";

    try {
        const resp = await fetch('/api/tools', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, action })
        });
        if (resp.ok) {
            const data = await resp.json();
            tgtOutput.textContent = data.result;
            showToast(`Converted numbers to ${hasKhmerDigits ? 'Western (0-9)' : 'Khmer (០-៩)'}`, "success");
        }
    } catch (err) {
        showToast("Numeral conversion error", "error");
    }
}

// Text-to-Speech
function speakText(text, langCode) {
    if (!text || !window.speechSynthesis) {
        showToast("Speech synthesis not supported on this device/text", "error");
        return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    if (langCode === 'km') {
        utterance.lang = 'km-KH';
    } else if (langCode === 'en') {
        utterance.lang = 'en-US';
    } else {
        utterance.lang = langCode;
    }
    utterance.rate = 0.95;
    window.speechSynthesis.speak(utterance);
    showToast("Playing pronunciation...", "success");
}

function loadSample(key) {
    const srcArea = document.getElementById('source-textarea');
    if (SAMPLE_PRESETS[key]) {
        srcArea.value = SAMPLE_PRESETS[key];
        document.getElementById('source-lang-select').value = 'en';
        document.getElementById('target-lang-select').value = 'km';
        updateCharCounts();
        executeTranslation();
    }
}

// Navigation Tabs
function switchMainTab(tab) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-view').forEach(view => view.classList.remove('active'));

    const tabBtn = document.getElementById(`tab-${tab}-btn`);
    const tabView = document.getElementById(`view-${tab}`);

    if (tabBtn) tabBtn.classList.add('active');
    if (tabView) tabView.classList.add('active');
}

// Settings Modal Management
function openSettingsModal() {
    const modal = document.getElementById('settings-modal');
    const settings = getSavedSettings();

    document.getElementById('setting-engine-select').value = settings.engine || 'auto';
    document.getElementById('setting-gemini-key').value = settings.geminiKey || '';
    document.getElementById('setting-gemini-model').value = settings.geminiModel || 'gemini-2.0-flash';
    document.getElementById('setting-openai-key').value = settings.openaiKey || '';
    document.getElementById('setting-openai-url').value = settings.openaiUrl || '';

    toggleEngineFormVisibility();
    modal.classList.add('active');
}

function closeSettingsModal() {
    document.getElementById('settings-modal').classList.remove('active');
}

function toggleEngineFormVisibility() {
    const engine = document.getElementById('setting-engine-select').value;
    const geminiGroup = document.getElementById('gemini-key-group');
    const modelGroup = document.getElementById('gemini-model-group');
    const openaiGroup = document.getElementById('openai-key-group');

    if (engine === 'openai') {
        geminiGroup.style.display = 'none';
        modelGroup.style.display = 'none';
        openaiGroup.style.display = 'flex';
    } else if (engine === 'free') {
        geminiGroup.style.display = 'none';
        modelGroup.style.display = 'none';
        openaiGroup.style.display = 'none';
    } else {
        geminiGroup.style.display = 'flex';
        modelGroup.style.display = 'flex';
        openaiGroup.style.display = 'none';
    }
}

document.getElementById('setting-engine-select')?.addEventListener('change', toggleEngineFormVisibility);

function saveSettings() {
    const engine = document.getElementById('setting-engine-select').value;
    const geminiKey = document.getElementById('setting-gemini-key').value.trim();
    const geminiModel = document.getElementById('setting-gemini-model').value;
    const openaiKey = document.getElementById('setting-openai-key').value.trim();
    const openaiUrl = document.getElementById('setting-openai-url').value.trim();

    const config = { engine, geminiKey, geminiModel, openaiKey, openaiUrl };
    localStorage.setItem('khmer_translator_settings', JSON.stringify(config));
    closeSettingsModal();
    showToast("Settings saved successfully!", "success");
    checkBackendStatus();
}

function getSavedSettings() {
    try {
        const item = localStorage.getItem('khmer_translator_settings');
        return item ? JSON.parse(item) : { engine: 'auto', geminiKey: '', geminiModel: 'gemini-2.0-flash' };
    } catch {
        return { engine: 'auto', geminiKey: '', geminiModel: 'gemini-2.0-flash' };
    }
}

function loadSavedSettings() {
    const s = getSavedSettings();
    if (s.engine && s.engine !== 'auto') {
        document.getElementById('current-engine-label').textContent = s.engine.toUpperCase();
    }
}

async function checkBackendStatus() {
    try {
        const resp = await fetch('/api/status');
        if (resp.ok) {
            const data = await resp.json();
            const settings = getSavedSettings();
            const hasUserKey = Boolean(settings.geminiKey);
            const isGeminiReady = data.engines.gemini || hasUserKey;

            const label = document.getElementById('current-engine-label');
            if (settings.engine === 'gemini' || (settings.engine === 'auto' && isGeminiReady)) {
                label.textContent = "Gemini 2.0 AI";
            } else if (settings.engine === 'openai') {
                label.textContent = "OpenAI / Ollama";
            } else {
                label.textContent = "Free Engine";
            }
        }
    } catch (err) {
        document.getElementById('current-engine-label').textContent = "Offline";
    }
}

function showToast(message, type = "success") {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${type}`;
    setTimeout(() => {
        toast.className = 'toast';
    }, 3200);
}

function escapeHtml(text) {
    if (!text) return '';
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
