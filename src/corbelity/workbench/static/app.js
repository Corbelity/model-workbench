document.addEventListener('DOMContentLoaded', () => {
    // DOM Element References
    const modelSelect = document.getElementById('modelSelect');
    const serviceBadge = document.getElementById('serviceBadge');
    const tempSlider = document.getElementById('tempSlider');
    const tempValue = document.getElementById('tempValue');
    const topPSlider = document.getElementById('topPSlider');
    const topPValue = document.getElementById('topPValue');
    const maxTokensInput = document.getElementById('maxTokensInput');
    const tokensValue = document.getElementById('tokensValue');

    const executeBtn = document.getElementById('executeBtn');
    const clearBtn = document.getElementById('clearBtn');
    const systemPrompt = document.getElementById('systemPrompt');
    const userPrompt = document.getElementById('userPrompt');
    const toggleSystemBtn = document.getElementById('toggleSystemBtn');
    const statusIndicator = document.getElementById('statusIndicator');
    const statusText = document.getElementById('statusText');

    const resultsContainer = document.getElementById('resultsContainer');
    const metricsBar = document.getElementById('metricsBar');
    const metricTime = document.getElementById('metricTime');
    const metricTokens = document.getElementById('metricTokens');
    const metricCost = document.getElementById('metricCost');
    const contextJson = document.getElementById('contextJson');   // request payload drawer

    // Conversation context panel
    const contextBlock = document.getElementById('contextBlock');
    const contextList = document.getElementById('contextList');
    const contextSummary = document.getElementById('contextSummary');
    const appendContextToggle = document.getElementById('appendContextToggle');
    const copyContextBtn = document.getElementById('copyContextBtn');
    const clearContextBtn = document.getElementById('clearContextBtn');

    // Image attachments (current turn only)
    const attachRow = document.getElementById('attachRow');
    const attachBtn = document.getElementById('attachBtn');
    const attachInput = document.getElementById('attachInput');
    const attachUrl = document.getElementById('attachUrl');
    const attachUrlBtn = document.getElementById('attachUrlBtn');
    const attachSummary = document.getElementById('attachSummary');
    const attachList = document.getElementById('attachList');

    const RESULTS_PLACEHOLDER = 'Run a prompt to see text, image, or sound results here.';
    const PAYLOAD_PLACEHOLDER = '// The serialized payload appears here after a run.';

    let activeModality = 'text';

    // -------------------------------------------------------------
    // 0. SMALL HELPERS
    // -------------------------------------------------------------
    // Everything this page keeps in the browser lives under one prefix, so it can be found
    // (and cleared) as a set. localStorage can throw -- private modes, disabled storage,
    // a full quota -- and the page must still work without it.
    const STORAGE_PREFIX = 'corbelity_workbench_';
    const store = {
        get(key) {
            try {
                return localStorage.getItem(STORAGE_PREFIX + key);
            } catch (err) {
                console.warn('Local storage unavailable:', err);
                return null;
            }
        },
        set(key, value) {
            try {
                localStorage.setItem(STORAGE_PREFIX + key, value);
            } catch (err) {
                console.warn('Could not persist', key, err);
            }
        },
    };

    function makeEl(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function escapeHtml(text) {
        const scratch = document.createElement('div');
        scratch.textContent = text;
        return scratch.innerHTML;
    }

    function setStatus(state) {
        const busy = state === 'busy';
        statusIndicator.classList.toggle('busy', busy);
        statusText.textContent = busy ? 'Running' : 'Ready';
    }

    function showPlaceholder(message) {
        resultsContainer.replaceChildren(makeEl('div', 'placeholder-text', message));
    }

    // FastAPI reports a validation failure (422) as an array of objects, not a string, and
    // interpolating that gives "[object Object]".
    function formatDetail(detail) {
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) {
            return detail.map(item => {
                const where = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : '';
                return where ? `${where}: ${item.msg}` : (item.msg || JSON.stringify(item));
            }).join('\n');
        }
        return detail ? JSON.stringify(detail) : 'Execution failed';
    }

    // textContent, never innerHTML: the message can carry provider or exception text.
    function showError(message) {
        const box = makeEl('div', 'result-error');
        box.appendChild(makeEl('strong', undefined, 'Error: '));
        box.appendChild(document.createTextNode(message));
        resultsContainer.replaceChildren(box);
    }

    // Model output is untrusted -- a prompt-injected response can contain HTML, and this
    // page holds API-key overrides in local storage. So it is sanitized before it reaches
    // the DOM, and if the sanitizer did not load this fails CLOSED to escaped plain text
    // rather than falling back to raw HTML.
    function renderMarkdown(text) {
        if (!window.marked || !window.DOMPurify) {
            return `<pre class="plain-output">${escapeHtml(text)}</pre>`;
        }
        return DOMPurify.sanitize(marked.parse(text));
    }

    function imageExtension(dataUrl) {
        const mime = /^data:(image\/[a-z0-9.+-]+);/i.exec(dataUrl);
        const known = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif' };
        return (mime && known[mime[1].toLowerCase()]) || 'png';
    }

    // -------------------------------------------------------------
    // 1. DYNAMIC MODEL LOADING FROM THE MODEL CATALOG VIA /api/models
    // -------------------------------------------------------------
    function setSelectMessage(message) {
        const option = makeEl('option', undefined, message);
        option.value = '';
        option.disabled = true;
        option.selected = true;
        modelSelect.replaceChildren(option);
    }

    async function loadModels() {
        try {
            setSelectMessage('Loading models…');
            const res = await fetch('/api/models');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const models = data.models || [];

            if (models.length === 0) {
                setSelectMessage('No models in the catalog');
                updateSelectedService();
                return;
            }

            modelSelect.replaceChildren();

            // Group models by service
            const grouped = {};
            models.forEach(m => {
                const service = m.service || 'general';
                if (!grouped[service]) grouped[service] = [];
                grouped[service].push(m);
            });

            for (const [service, modelList] of Object.entries(grouped)) {
                const optgroup = document.createElement('optgroup');
                optgroup.label = service.toUpperCase();

                modelList.forEach(m => {
                    const opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.name || m.id;
                    opt.dataset.service = m.service;
                    opt.dataset.modality = m.modality;
                    opt.dataset.acceptsImages = m.accepts_images ? 'true' : 'false';
                    optgroup.appendChild(opt);
                });

                modelSelect.appendChild(optgroup);
            }

            updateSelectedService();
        } catch (err) {
            // No hardcoded fallback list: the server only accepts catalog ids, so offering
            // made-up ones would turn a load failure into a confusing 404 on the first run.
            console.error('Failed to load the model catalog:', err);
            setSelectMessage('Could not load models. Is the server running?');
            updateSelectedService();
        }
    }

    function selectedAcceptsImages() {
        const option = modelSelect.options[modelSelect.selectedIndex];
        return option?.dataset.acceptsImages === 'true';
    }

    function attachmentBlockReason() {
        if (activeModality !== 'text') {
            return 'Image input applies to text generation only.';
        }
        if (!selectedAcceptsImages()) {
            return `${modelSelect.value || 'This model'} is not registered as accepting image input.`;
        }
        return '';
    }

    function updateAttachEnablement() {
        const reason = attachmentBlockReason();
        const blocked = Boolean(reason);
        attachRow.classList.toggle('disabled', blocked);
        [attachBtn, attachUrl, attachUrlBtn].forEach(el => {
            el.disabled = blocked;
            el.title = reason;
        });
        // Existing attachments are greyed, not discarded - see style.css.
        attachList.querySelectorAll('.attach-item')
            .forEach(node => node.classList.toggle('blocked', blocked));
    }

    function updateSelectedService() {
        const selectedOption = modelSelect.options[modelSelect.selectedIndex];
        if (selectedOption && selectedOption.dataset.service) {
            const service = selectedOption.dataset.service;
            if (serviceBadge) serviceBadge.textContent = `Service: ${service}`;

            // Auto-switch modality tab if specified by target model
            const targetModality = selectedOption.dataset.modality;
            if (targetModality) {
                const pillBtn = document.querySelector(`.pill-btn[data-modality="${targetModality}"]`);
                if (pillBtn) pillBtn.click();
            }
        } else if (serviceBadge) {
            serviceBadge.textContent = 'Service: --';
        }
        updateAttachEnablement();
    }

    modelSelect.addEventListener('change', updateSelectedService);
    loadModels();

    // -------------------------------------------------------------
    // 2. SERVER CONFIG & LOCAL-STORAGE KEY OVERRIDES
    // -------------------------------------------------------------
    // Fields the UI can override, in the order they appear. `ollamaUrl` is an endpoint,
    // not a key, and its element id has no "Key" suffix.
    const keyNames = ['openrouter', 'anthropic', 'huggingface', 'ollamaUrl'];

    fetch('/api/config')
        .then(res => res.json())
        .then(data => {
            const status = data.env_status;
            if (!status) return;

            ['openrouter', 'anthropic', 'huggingface'].forEach(name => {
                const badge = document.getElementById(`${name}Badge`);
                if (badge && status[name]) badge.classList.add('active');
            });

            const ollamaInput = document.getElementById('ollamaUrl');
            if (status.ollama_url && ollamaInput && !store.get('ollamaUrl')) {
                ollamaInput.value = status.ollama_url;
            }
        })
        .catch(err => console.warn('Config fetch skipped:', err));

    // Restore key overrides from local storage
    keyNames.forEach(name => {
        const el = document.getElementById(name === 'ollamaUrl' ? 'ollamaUrl' : `${name}Key`);
        if (!el) return;
        const saved = store.get(name);
        if (saved) el.value = saved;
        el.addEventListener('change', () => store.set(name, el.value));
    });

    // -------------------------------------------------------------
    // 2b. CONVERSATION CONTEXT
    // Prior turns, owned by the browser and sent with every text call. The server
    // stays stateless, which is what lets you switch models mid-conversation.
    // -------------------------------------------------------------
    const TOKEN_WARN = 8000;      // approximate; display only, nothing is truncated
    const TOKEN_DANGER = 16000;

    // Mirrors the server's caps in app.py, so an oversized file is refused before it is
    // read rather than after a round trip. Keep the two in step.
    const MAX_INPUT_IMAGES = 4;
    const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
    const MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024;

    // Attachments for the CURRENT turn only - they are never written to local storage,
    // because images do not enter the conversation (a photo is 1-3 MB as base64).
    let attachments = [];

    let conversation = loadContext();
    let editingIndex = null;

    function loadContext() {
        try {
            const saved = JSON.parse(store.get('context') || '[]');
            if (!Array.isArray(saved)) throw new Error('stored context is not an array');
            // Re-validate on the way in: a half-written or hand-edited entry must not
            // break the page on boot.
            return saved
                .filter(m => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
                .map(m => ({ role: m.role, content: m.content }));
        } catch (err) {
            console.warn('Discarding unreadable saved context:', err);
            return [];
        }
    }

    function saveContext() {
        store.set('context', JSON.stringify(conversation));
    }

    function approxTokens() {
        // chars/4 rule of thumb. Labelled '~' in the UI so it is never confused with
        // the real count the provider reports in the metrics bar.
        return Math.round(conversation.reduce((n, m) => n + m.content.length, 0) / 4);
    }

    function iconBtn(glyph, title, onClick, extraClass) {
        const button = makeEl('button', 'icon-btn' + (extraClass ? ' ' + extraClass : ''), glyph);
        button.type = 'button';
        button.title = title;
        button.setAttribute('aria-label', title);
        button.addEventListener('click', onClick);
        return button;
    }

    function buildCard(message, index) {
        const card = makeEl('div', 'context-card');
        card.appendChild(makeEl('span', `context-role ${message.role}`, message.role));

        // textContent, never innerHTML: this is raw model output.
        const content = makeEl('div', 'context-content', message.content);
        content.title = 'Click to expand or collapse';
        content.addEventListener('click', () => content.classList.toggle('expanded'));
        card.appendChild(content);

        const actions = makeEl('div', 'context-actions');
        actions.appendChild(iconBtn('✎', 'Edit this turn', () => {
            editingIndex = index;
            renderContext();
        }));
        actions.appendChild(iconBtn('×', 'Delete this turn', () => {
            conversation.splice(index, 1);
            editingIndex = null;
            saveContext();
            renderContext();
        }, 'danger'));
        card.appendChild(actions);
        return card;
    }

    function buildEditCard(message, index) {
        const card = makeEl('div', 'context-card');
        card.appendChild(makeEl('span', `context-role ${message.role}`, message.role));

        const editor = makeEl('textarea', 'context-edit');
        editor.rows = 4;
        editor.value = message.content;
        card.appendChild(editor);

        const actions = makeEl('div', 'context-actions');
        actions.appendChild(iconBtn('✓', 'Save', () => {
            conversation[index] = { role: message.role, content: editor.value };
            editingIndex = null;
            saveContext();
            renderContext();
        }));
        actions.appendChild(iconBtn('✗', 'Cancel', () => {
            editingIndex = null;
            renderContext();
        }));
        card.appendChild(actions);

        setTimeout(() => editor.focus(), 0);
        return card;
    }

    function renderContext() {
        contextBlock.classList.toggle('is-empty', conversation.length === 0);

        const tokens = approxTokens();
        contextSummary.textContent = conversation.length === 0
            ? 'empty'
            : `${conversation.length} message${conversation.length === 1 ? '' : 's'} · ~${tokens.toLocaleString()} tok`;
        contextSummary.className = 'context-summary'
            + (tokens > TOKEN_DANGER ? ' danger' : tokens > TOKEN_WARN ? ' warn' : '');

        contextList.replaceChildren();
        if (conversation.length === 0) {
            contextList.appendChild(makeEl('div', 'context-empty',
                'No prior turns. Responses are appended here automatically.'));
            return;
        }
        conversation.forEach((message, index) => {
            contextList.appendChild(index === editingIndex
                ? buildEditCard(message, index)
                : buildCard(message, index));
        });
    }

    function attachmentBytes() {
        return attachments.reduce((total, item) => total + (item.bytes || 0), 0);
    }

    function renderAttachSummary() {
        if (attachments.length === 0) {
            attachSummary.textContent = '';
            attachSummary.className = 'attach-summary';
            return;
        }
        const total = attachmentBytes();
        const label = `${attachments.length} image${attachments.length === 1 ? '' : 's'}`;
        // A URL's size is unknown until the server fetches it, so it contributes 0 here.
        attachSummary.textContent = total
            ? `${label} · ${(total / (1024 * 1024)).toFixed(1)} MB`
            : label;
        const near = attachments.length >= MAX_INPUT_IMAGES - 1
            || total > MAX_TOTAL_IMAGE_BYTES * 0.75;
        attachSummary.className = 'attach-summary' + (near ? ' warn' : '');
    }

    function renderAttachments() {
        attachList.replaceChildren();
        attachments.forEach((item, index) => {
            const card = makeEl('div', 'attach-item');

            const thumb = document.createElement('img');
            thumb.src = item.data_url || item.url;
            thumb.alt = item.name || 'attachment';
            // Remote images are often hotlink-protected. A failed preview must read as a
            // chip, not as a broken feature - the server fetches it either way.
            thumb.addEventListener('error', () => {
                thumb.replaceWith(makeEl('div', 'attach-fallback', 'URL'));
            });
            card.appendChild(thumb);

            card.appendChild(makeEl('div', 'attach-name', item.name || 'image'));

            const remove = makeEl('button', 'attach-remove', '×');
            remove.type = 'button';
            remove.title = 'Remove this image';
            remove.setAttribute('aria-label', 'Remove this image');
            remove.addEventListener('click', () => {
                attachments.splice(index, 1);
                renderAttachments();
            });
            card.appendChild(remove);

            attachList.appendChild(card);
        });
        renderAttachSummary();
        updateAttachEnablement();
    }

    function addFiles(files) {
        // FileReader is async, so attachments.length lags behind. Project the running
        // totals forward instead, or selecting five files at once would let them all in.
        let count = attachments.length;
        let bytes = attachmentBytes();

        for (const file of files) {
            if (count >= MAX_INPUT_IMAGES) {
                alert(`At most ${MAX_INPUT_IMAGES} images per request.`);
                break;
            }
            if (file.size > MAX_IMAGE_BYTES) {
                alert(`${file.name} is ${(file.size / 1048576).toFixed(1)} MB, over the `
                    + `${MAX_IMAGE_BYTES / 1048576} MB per-image limit.`);
                continue;
            }
            if (bytes + file.size > MAX_TOTAL_IMAGE_BYTES) {
                alert(`Adding ${file.name} would exceed the total size limit for one request.`);
                continue;
            }
            count += 1;
            bytes += file.size;

            const reader = new FileReader();
            reader.onload = () => {
                attachments.push({ data_url: reader.result, name: file.name, bytes: file.size });
                renderAttachments();
            };
            reader.onerror = () => alert(`Could not read ${file.name}.`);
            reader.readAsDataURL(file);
        }
    }

    function addUrl() {
        const raw = attachUrl.value.trim();
        if (!raw) return;
        if (!/^https?:\/\//i.test(raw)) {
            alert('Image URL must start with http:// or https://');
            return;
        }
        if (attachments.length >= MAX_INPUT_IMAGES) {
            alert(`At most ${MAX_INPUT_IMAGES} images per request.`);
            return;
        }
        let name = 'image';
        try {
            name = new URL(raw).pathname.split('/').filter(Boolean).pop() || 'image';
        } catch (err) {
            console.warn('Could not derive a filename from the URL:', err);
        }
        // bytes stays 0: the size is unknown until the server fetches it.
        attachments.push({ url: raw, name, bytes: 0 });
        attachUrl.value = '';
        renderAttachments();
    }

    attachBtn.addEventListener('click', () => attachInput.click());
    attachInput.addEventListener('change', () => {
        addFiles(attachInput.files);
        attachInput.value = '';      // so re-picking the same file fires 'change' again
    });
    attachUrlBtn.addEventListener('click', addUrl);
    attachUrl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addUrl();
        }
    });

    renderAttachments();

    copyContextBtn.addEventListener('click', async () => {
        // Exported with the system prompt at index 0 so what you paste is a complete,
        // portable conversation rather than a fragment.
        const payload = JSON.stringify(
            [{ role: 'system', content: systemPrompt.value.trim() }, ...conversation], null, 2);

        const confirmCopied = () => {
            copyContextBtn.textContent = 'Copied';
            setTimeout(() => { copyContextBtn.textContent = 'Copy'; }, 1500);
        };

        try {
            await navigator.clipboard.writeText(payload);
            confirmCopied();
        } catch (err) {
            // The Clipboard API needs a secure context; 127.0.0.1 qualifies, but fall
            // back rather than failing silently if it is unavailable.
            const scratch = document.createElement('textarea');
            scratch.value = payload;
            document.body.appendChild(scratch);
            scratch.select();
            document.execCommand('copy');
            scratch.remove();
            confirmCopied();
        }
    });

    clearContextBtn.addEventListener('click', () => {
        if (conversation.length === 0) return;
        if (!confirm(`Delete all ${conversation.length} messages from the conversation?`)) return;
        conversation = [];
        editingIndex = null;
        saveContext();
        renderContext();
    });

    renderContext();

    // -------------------------------------------------------------
    // 3. HYPERPARAMETER SLIDER SYNCHRONIZATION
    // -------------------------------------------------------------
    tempSlider.addEventListener('input', (e) => tempValue.textContent = parseFloat(e.target.value).toFixed(2));
    topPSlider.addEventListener('input', (e) => topPValue.textContent = parseFloat(e.target.value).toFixed(2));
    maxTokensInput.addEventListener('input', (e) => tokensValue.textContent = e.target.value);

    // Modality pills toggle
    document.querySelectorAll('.pill-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.pill-btn').forEach(b => {
                b.classList.remove('active');
                b.setAttribute('aria-pressed', 'false');
            });
            btn.classList.add('active');
            btn.setAttribute('aria-pressed', 'true');
            activeModality = btn.dataset.modality;
            updateAttachEnablement();
        });
    });

    // System Prompt Toggle
    if (toggleSystemBtn) {
        toggleSystemBtn.addEventListener('click', () => {
            if (systemPrompt.style.display === 'none') {
                systemPrompt.style.display = 'block';
                toggleSystemBtn.textContent = 'Collapse';
            } else {
                systemPrompt.style.display = 'none';
                toggleSystemBtn.textContent = 'Expand';
            }
        });
    }

    // Keyboard shortcut Ctrl+Enter / Cmd+Enter
    userPrompt.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            runExecution();
        }
    });

    executeBtn.addEventListener('click', runExecution);

    clearBtn.addEventListener('click', () => {
        userPrompt.value = '';
        // Attachments survive a send on purpose (images never enter history, so this
        // strip is the only place one lives). Clear is the explicit gesture for it.
        attachments = [];
        renderAttachments();
        showPlaceholder(RESULTS_PLACEHOLDER);
        metricsBar.classList.add('hidden');
        contextJson.textContent = PAYLOAD_PLACEHOLDER;
    });

    // -------------------------------------------------------------
    // 4. EXECUTION HANDLER
    // -------------------------------------------------------------
    async function runExecution() {
        // The Run button is disabled mid-flight, but Ctrl+Enter is not a button.
        if (executeBtn.disabled) return;

        const promptText = userPrompt.value.trim();
        if (!promptText) {
            alert('Please enter a user prompt before running.');
            return;
        }
        if (!modelSelect.value) {
            alert('Select a model before running.');
            return;
        }

        executeBtn.disabled = true;
        setStatus('busy');
        showPlaceholder('Running…');

        const payload = {
            model: modelSelect.value,
            system_prompt: systemPrompt.value.trim(),
            prompt: promptText,
            // Only text calls are conversational; image/audio generation is single-shot,
            // and sending history there would make an unrelated edit block the call.
            history: activeModality === 'text' ? conversation : [],
            // Same guard as history: media generation is single-shot and the server
            // refuses attachments on it.
            images: activeModality === 'text'
                ? attachments.map(a => (a.data_url
                    ? { data_url: a.data_url, name: a.name }
                    : { url: a.url, name: a.name }))
                : [],
            modality: activeModality,
            temperature: parseFloat(tempSlider.value),
            top_p: parseFloat(topPSlider.value),
            max_tokens: parseInt(maxTokensInput.value, 10),
            openrouter_key: document.getElementById('openrouterKey')?.value || null,
            anthropic_key: document.getElementById('anthropicKey')?.value || null,
            huggingface_key: document.getElementById('huggingfaceKey')?.value || null,
            ollama_url: document.getElementById('ollamaUrl')?.value || null,
        };

        try {
            const response = await fetch('/api/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            const data = await response.json();

            if (!response.ok) {
                showError(formatDetail(data.detail));
                return;
            }

            renderResults(data);

            // Appended only on success, so a failed call never enters the transcript.
            // The server decides what the assistant turn says -- for media that is a
            // placeholder, not the payload.
            if (appendContextToggle.checked && data.context_entry) {
                // The server formats the user turn so the attachment placeholder has one
                // definition; fall back for safety if an older server omits the field.
                conversation.push(data.user_context_entry
                    || { role: 'user', content: promptText });
                conversation.push(data.context_entry);
                saveContext();
                renderContext();
            }
        } catch (err) {
            showError(`Network error: ${err.message}`);
        } finally {
            executeBtn.disabled = false;
            setStatus('ready');
        }
    }

    // -------------------------------------------------------------
    // 5. RENDER MULTI-MODAL RESULTS & METRICS
    // -------------------------------------------------------------
    function renderResults(data) {
        if (data.result_type === 'text') {
            const body = makeEl('div', 'markdown-body');
            body.innerHTML = renderMarkdown(data.content || '');
            resultsContainer.replaceChildren(body);
            if (window.hljs) {
                body.querySelectorAll('pre code').forEach(block => hljs.highlightElement(block));
            }
        } else if (data.result_type === 'image') {
            // Built with DOM APIs rather than an HTML string, so nothing in the payload is
            // ever parsed as markup.
            const wrap = makeEl('div', 'result-media');

            const img = document.createElement('img');
            img.className = 'result-image';
            img.alt = 'Generated output';
            img.src = data.content;
            wrap.appendChild(img);

            wrap.appendChild(document.createElement('br'));

            const download = makeEl('a', 'btn-secondary', 'Download image');
            download.href = data.content;
            download.download = `generated_image.${imageExtension(data.content)}`;
            wrap.appendChild(download);

            resultsContainer.replaceChildren(wrap);
        } else if (data.result_type === 'sound') {
            const wrap = document.createElement('div');
            wrap.appendChild(makeEl('h4', 'result-heading', 'Generated audio'));

            const audio = document.createElement('audio');
            audio.className = 'result-audio';
            audio.controls = true;
            audio.autoplay = true;
            audio.src = data.content;
            wrap.appendChild(audio);

            resultsContainer.replaceChildren(wrap);
        }

        // Update Metrics
        metricsBar.classList.remove('hidden');
        metricTime.textContent = `${data.metrics.time_seconds}s`;
        metricTokens.textContent = `${data.metrics.prompt_tokens} P / ${data.metrics.completion_tokens} C (${data.metrics.total_tokens} Total)`;
        metricCost.textContent = `$${data.metrics.estimated_cost_usd.toFixed(6)}`;

        // Update the request payload drawer. highlight.js refuses to re-highlight an
        // element it has already done unless that mark is cleared first.
        contextJson.textContent = JSON.stringify(data.context_payload, null, 2);
        if (window.hljs) {
            delete contextJson.dataset.highlighted;
            hljs.highlightElement(contextJson);
        }
    }

    // -------------------------------------------------------------
    // 6. PRESETS
    // -------------------------------------------------------------
    const PRESETS = {
        text: {
            system: 'You are a helpful chatbot.',
            prompt: 'Write a paragraph summarizing the building of the Eiffel Tower.',
            modality: 'text',
        },
        creative: {
            system: 'You are a master storyteller.',
            prompt: 'Write a 2-paragraph story about an astronaut discovering a glowing artifact on Europa.',
            modality: 'text',
        },
        image: {
            system: 'Formulate detailed image prompts for diffusion models.',
            prompt: 'Cinematic shot of a cozy cabin in a snowy pine forest at twilight, 8K resolution.',
            modality: 'image',
        },
        audio: {
            system: 'Generate audio synthesis text.',
            prompt: 'Attention passengers, flight 402 to Tokyo is now boarding at Gate 14.',
            modality: 'sound',
        },
    };

    document.querySelectorAll('[data-preset]').forEach(button => {
        button.addEventListener('click', () => {
            const preset = PRESETS[button.dataset.preset];
            if (!preset) {
                // The button's data-preset has no matching entry above, so index.html and
                // this file disagree about the key. Said out loud because the alternative
                // is a button that silently does nothing, which reads as a dead control
                // rather than a typo -- or, more often, as a stale cached copy of this file.
                console.warn(`No preset named "${button.dataset.preset}".`,
                             'Known presets:', Object.keys(PRESETS).join(', '));
                return;
            }
            systemPrompt.value = preset.system;
            userPrompt.value = preset.prompt;
            document.querySelector(`.pill-btn[data-modality="${preset.modality}"]`)?.click();
        });
    });
});
