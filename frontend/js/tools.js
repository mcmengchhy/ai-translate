/**
 * Khmer Linguistic Tools (Client-side handlers)
 */

async function runKhmerTool(action) {
    let inputEl, outputEl;

    if (action === 'normalize' || action === 'add_zwsp' || action === 'strip_zwsp') {
        inputEl = document.getElementById('tool-norm-input');
        outputEl = document.getElementById('tool-norm-output');
    } else {
        inputEl = document.getElementById('tool-num-input');
        outputEl = document.getElementById('tool-num-output');
    }

    const text = inputEl.value.trim();
    if (!text) {
        showToast('Please enter some text first!', 'error');
        return;
    }

    outputEl.textContent = 'Processing...';

    try {
        const resp = await fetch('/api/tools', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, action })
        });

        if (!resp.ok) {
            throw new Error(`Server returned ${resp.status}`);
        }

        const data = await resp.json();
        outputEl.textContent = data.result;
        showToast('Action completed successfully!', 'success');
    } catch (err) {
        outputEl.textContent = 'Error: ' + err.message;
        showToast(err.message, 'error');
    }
}
