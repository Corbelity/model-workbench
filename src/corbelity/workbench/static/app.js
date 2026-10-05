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

    // Video. The settings block and the job list are both built at runtime, so these are
    // the containers rather than the controls.
    const videoGroup = document.getElementById('videoGroup');
    const videoSettings = document.getElementById('videoSettings');
    const videoNegative = document.getElementById('videoNegative');
    const videoCheck = document.getElementById('videoCheck');
    const videoFrames = document.getElementById('videoFrames');
    const videoJobs = document.getElementById('videoJobs');
    const videoJobList = document.getElementById('videoJobList');
    const videoJobsSummary = document.getElementById('videoJobsSummary');
    const refreshJobsBtn = document.getElementById('refreshJobsBtn');
    const payloadDrawer = document.getElementById('payloadDrawer');

    const RESULTS_PLACEHOLDER = 'Run a prompt to see text, image, or sound results here.';
    const PAYLOAD_PLACEHOLDER = '// The serialized payload appears here after a run.';

    let activeModality = 'text';

    // The catalog entries, whole, keyed by model id. The <option> dataset carries the flat
    // fields it needs, but a model's `video` block is a nested object with a constraint
    // table in it -- that does not belong in a dataset string, and the video panel reads
    // it to build its controls.
    const catalog = new Map();

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

            catalog.clear();
            models.forEach(m => catalog.set(m.id, m));

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
                    // Absent on an older server, which should read as "assume they work"
                    // rather than greying every slider out.
                    opt.dataset.honorsSampling = m.honors_sampling === false ? 'false' : 'true';
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

    // Sampling controls, and the hint each one shows when it still does something. The
    // defaults are read from the DOM so the wording lives in index.html only.
    const SAMPLING_NA_HINT =
        'Not used by this model: the provider ignores sampling settings, so this slider '
        + 'has no effect on the request.';
    const samplingControls = [
        { group: document.getElementById('tempGroup'), input: tempSlider, hint: document.getElementById('tempHint') },
        { group: document.getElementById('topPGroup'), input: topPSlider, hint: document.getElementById('topPHint') },
    ].filter(control => control.group && control.input && control.hint);
    samplingControls.forEach(control => { control.defaultHint = control.hint.textContent; });

    // Max tokens is applicable to text and image but not to video, which is why it is
    // kept apart from samplingControls -- those two are driven by the model's provider,
    // this one by the modality.
    const tokensControl = (() => {
        const group = document.getElementById('tokensGroup');
        const hint = document.getElementById('tokensHint');
        if (!group || !hint) return null;
        return { group, input: maxTokensInput, hint, defaultHint: hint.textContent };
    })();

    // Short on purpose: it is shown under three controls at once, and the video settings
    // block above them already says what a video request does carry.
    const VIDEO_NA_HINT = 'Not sent for a video request.';

    function updateSamplingEnablement() {
        const option = modelSelect.options[modelSelect.selectedIndex];
        // Default to enabled: an unknown or unselected model should not look broken.
        const honors = option?.dataset.honorsSampling !== 'false';
        const video = activeModality === 'video';
        samplingControls.forEach(control => {
            // Two independent reasons a slider does nothing, and they get different
            // wording: the provider ignores it, or this modality has no such setting.
            const live = honors && !video;
            control.group.classList.toggle('not-applicable', !live);
            control.input.disabled = !live;
            control.hint.textContent = live
                ? control.defaultHint
                : (video ? VIDEO_NA_HINT : SAMPLING_NA_HINT);
        });
        if (tokensControl) {
            tokensControl.group.classList.toggle('not-applicable', video);
            tokensControl.input.disabled = video;
            tokensControl.hint.textContent = video ? VIDEO_NA_HINT : tokensControl.defaultHint;
        }
    }

    function selectedAcceptsImages() {
        const option = modelSelect.options[modelSelect.selectedIndex];
        return option?.dataset.acceptsImages === 'true';
    }

    function attachmentBlockReason() {
        // Reference images condition an image generation, so the gate is the model's
        // accepts_images flag rather than the modality. Only speech takes neither.
        if (activeModality === 'sound') {
            return 'Speech generation takes no image input.';
        }
        if (activeModality === 'video') {
            // Video takes its inputs BY ROLE, not as one list, so it has its own pickers
            // below. This strip would send them as the wrong thing.
            return 'Video takes frames by role -- use the frame pickers below the prompt.';
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

        // Say why they are inert. Without this the thumbnails just look broken, and the
        // summary keeps reporting a size for images that are not being sent.
        if (blocked && attachments.length) {
            const count = `${attachments.length} image${attachments.length === 1 ? '' : 's'}`;
            attachSummary.textContent = `${count} not sent to this model`;
            attachSummary.className = 'attach-summary warn';
        }
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
        updateSamplingEnablement();
        // The video controls are generated from the selected model's own capabilities, so
        // they are rebuilt whenever the model changes -- not when the pill does.
        renderVideoSettings();
        scheduleVideoCheck();
    }

    modelSelect.addEventListener('change', updateSelectedService);
    loadModels();

    // -------------------------------------------------------------
    // 2. SERVER CONFIG & LOCAL-STORAGE KEY OVERRIDES
    // -------------------------------------------------------------
    // Fields the UI can override, in the order they appear. `ollamaUrl` is an endpoint,
    // not a key, and its element id has no "Key" suffix.
    const keyNames = ['openrouter', 'anthropic', 'openai', 'gemini', 'huggingface', 'ollamaUrl'];

    // Services with a key field and a .env badge. Derived from keyNames so a new provider
    // is added in one place; ollamaUrl is excluded because it is an endpoint.
    const keyServices = keyNames.filter(name => name !== 'ollamaUrl');

    fetch('/api/config')
        .then(res => res.json())
        .then(data => {
            const status = data.env_status;
            if (!status) return;

            keyServices.forEach(name => {
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
            updateSamplingEnablement();
            updateVideoMode();
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
    // The six fields every endpoint that can carry a credential reads, matching the
    // Credentials model on the server. Built in one place so a key typed into the sidebar
    // reaches /api/generate and /api/video/* alike -- two copies would drift, and the
    // symptom would be a key that works on one endpoint and is ignored on the other.
    function credentialFields() {
        return {
            openrouter_key: document.getElementById('openrouterKey')?.value || null,
            anthropic_key: document.getElementById('anthropicKey')?.value || null,
            openai_key: document.getElementById('openaiKey')?.value || null,
            gemini_key: document.getElementById('geminiKey')?.value || null,
            huggingface_key: document.getElementById('huggingfaceKey')?.value || null,
            ollama_url: document.getElementById('ollamaUrl')?.value || null,
        };
    }

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

        // Video branches off before any of what follows. It has no history, no sampling
        // settings, no metrics and no single result -- and it returns as soon as the job
        // is accepted rather than when the video exists.
        if (activeModality === 'video') {
            executeBtn.disabled = true;
            setStatus('busy');
            try {
                await submitVideo(promptText);
            } finally {
                setStatus('ready');
                // Back to whatever the last check said, not unconditionally enabled: the
                // button is the check's to control in video mode.
                executeBtn.disabled = !videoSubmitAllowed;
            }
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
            // Reference images ride along with an image generation too; only speech
            // takes none. The server refuses them for sound, so don't send them.
            images: activeModality === 'sound'
                ? []
                : attachments.map(a => (a.data_url
                    ? { data_url: a.data_url, name: a.name }
                    : { url: a.url, name: a.name })),
            modality: activeModality,
            temperature: parseFloat(tempSlider.value),
            top_p: parseFloat(topPSlider.value),
            max_tokens: parseInt(maxTokensInput.value, 10),
            ...credentialFields(),
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
            if (!appendContextToggle.checked) {
                console.info('Not appending to the conversation: "Append responses" is off.');
            } else if (!data.context_entry) {
                // The server sends context_entry on every 200, so its absence means the
                // response is not the shape this page expects -- most often a stale
                // cached app.js, or a server older than this file.
                console.warn('Response had no context_entry, so nothing was appended.',
                             'Keys present:', Object.keys(data).join(', '));
            } else {
                // The server formats the user turn so the attachment placeholder has one
                // definition; fall back for safety if an older server omits the field.
                conversation.push(data.user_context_entry
                    || { role: 'user', content: promptText });
                conversation.push(data.context_entry);
                saveContext();
                renderContext();
            }
        } catch (err) {
            // Includes anything renderResults() threw, which runs BEFORE the append above,
            // so a rendering failure also costs you the conversation turn. Logged with the
            // stack because "Network error" on screen is misleading when the network was
            // fine.
            console.error('runExecution failed after the response arrived:', err);
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
    // 6. VIDEO: SUBMIT, WATCH, PLAY
    // -------------------------------------------------------------
    // Video cannot be a request that returns its own result -- a generation runs for tens
    // of seconds to minutes -- so this half of the page submits a job, watches it, and
    // plays it when it lands. The job lives on the server and outlives this page, which is
    // why the list is repopulated from the server on load rather than from local storage.
    //
    // The one rule that shapes everything here: no copy of the catalog's constraint table.
    // Whether a combination is allowed is answered by /api/video/check, which runs the
    // library's own resolve_video_request() -- the same function a real submission runs.
    // A rule restated in this file is the one that would go stale.

    // The settings this panel offers. `axis` names the capability list on the model's
    // catalog `video` block, so the options come from the model rather than from here.
    const VIDEO_SETTING_FIELDS = [
        { name: 'aspect_ratio', label: 'Aspect', axis: 'aspect_ratios' },
        { name: 'resolution', label: 'Resolution', axis: 'resolutions' },
        { name: 'duration_seconds', label: 'Duration', axis: 'durations', numeric: true, unit: 's' },
    ];

    // The check is re-asked on every control change, so it is debounced: typing in a free
    // text axis should send one request, not one per keystroke.
    const VIDEO_CHECK_DEBOUNCE_MS = 250;

    // How often a running job is polled. Slow on purpose -- a video takes tens of seconds
    // at best, and the library's own note is that a five-minute job polled every ten
    // seconds writes thirty identical records.
    const VIDEO_POLL_MS = 4000;

    // Frames by role. Deliberately not merged with `attachments`: those are reference
    // images that go out as one list, while these are addressed by role and only one of
    // each is meaningful.
    const videoFrameFiles = new Map();

    // What the page knows about each job, and any note to show on its card that is not
    // part of the stored row (a transient poll failure, say).
    const videoJobRows = new Map();
    const videoJobNotes = new Map();

    // job id -> timeout handle. Keyed so a re-render can never start a second poll loop
    // for the same job.
    const videoPolls = new Map();

    // Jobs submitted from THIS page session. Only these may write turns into the
    // conversation when they finish: a job recovered from a previous session is shown and
    // played, but the conversation in this browser has nothing to do with it, and
    // appending for it would drop a stranger's prompt into the transcript.
    const videoWatched = new Set();

    let videoCheckTimer = null;
    let videoSubmitAllowed = false;

    const RUN_LABEL = executeBtn.textContent;

    function videoCapabilities() {
        const model = catalog.get(modelSelect.value);
        return (model && model.video) || null;
    }

    // In the library's own order, which is the order a constraint's `when` is matched
    // against.
    function videoRoles() {
        return ['first_frame', 'last_frame'].filter(role => videoFrameFiles.has(role));
    }

    function videoSettingValues() {
        const values = {};
        VIDEO_SETTING_FIELDS.forEach(field => {
            const el = videoSettings.querySelector(`[data-setting="${field.name}"]`);
            const raw = el ? String(el.value).trim() : '';
            // Empty means NOT REQUESTED: the key is left out of the request and the
            // provider's own default applies. That is a different request from any value
            // this page could pick, so it has to stay reachable.
            if (!raw) return;
            if (field.numeric) {
                const parsed = Number(raw);
                // A free text axis can hold anything. Send what was typed when it is not a
                // whole number, so the server names the field rather than this page
                // quietly sending NaN as null.
                values[field.name] = Number.isInteger(parsed) ? parsed : raw;
            } else {
                values[field.name] = raw;
            }
        });
        const negative = videoNegative.value.trim();
        if (negative) values.negative_prompt = negative;
        return values;
    }

    // Built from the model's own capability block, so a model offering different
    // resolutions gets different options with no change here. An axis the catalog does not
    // state becomes a FREE TEXT field rather than a disabled one: "not stated" means the
    // provider decides, not that nothing may be asked for -- the same rule the library
    // applies, and the check judges whatever is typed.
    function renderVideoSettings() {
        if (!videoSettings) return;
        const caps = videoCapabilities();
        const previous = videoSettingValues();
        videoSettings.replaceChildren();

        VIDEO_SETTING_FIELDS.forEach(field => {
            const wrap = makeEl('div', 'video-field');
            wrap.dataset.field = field.name;

            const id = `video_${field.name}`;
            const label = makeEl('label', undefined, field.label);
            label.htmlFor = id;
            wrap.appendChild(label);

            const offered = caps ? caps[field.axis] : null;
            let control;
            if (Array.isArray(offered) && offered.length) {
                control = makeEl('select', 'input-control');
                const unset = makeEl('option', undefined, 'provider default');
                unset.value = '';
                control.appendChild(unset);
                offered.forEach(value => {
                    const option = makeEl('option', undefined,
                        field.unit ? `${value}${field.unit}` : String(value));
                    option.value = String(value);
                    control.appendChild(option);
                });
            } else {
                control = makeEl('input', 'input-control');
                control.type = 'text';
                control.placeholder = caps
                    ? 'not stated \u2014 judged by the provider'
                    : 'provider default';
            }
            control.id = id;
            control.dataset.setting = field.name;
            // Carried across a model change where the new model still offers it; a select
            // simply will not hold a value the new model does not list, and the check then
            // reports what is left.
            if (previous[field.name] !== undefined) control.value = String(previous[field.name]);
            control.addEventListener('change', scheduleVideoCheck);
            control.addEventListener('input', scheduleVideoCheck);
            wrap.appendChild(control);
            videoSettings.appendChild(wrap);
        });
    }

    function scheduleVideoCheck() {
        if (videoCheckTimer) clearTimeout(videoCheckTimer);
        videoCheckTimer = setTimeout(runVideoCheck, VIDEO_CHECK_DEBOUNCE_MS);
    }

    function setVideoCheck(message, kind, allowed) {
        videoCheck.textContent = message;
        videoCheck.className = `field-hint video-check ${kind}`;
        videoSubmitAllowed = Boolean(allowed);
        if (activeModality === 'video') executeBtn.disabled = !videoSubmitAllowed;
    }

    // What will actually be SENT, which is not always what was asked for: a catalog
    // constraint fills a setting left unset. Saying so is the point -- otherwise the eight
    // seconds that 1080p forces is invisible until the bill arrives.
    function describeResolved(resolved) {
        const asked = videoSettingValues();
        const parts = [];
        VIDEO_SETTING_FIELDS.forEach(field => {
            const value = resolved[field.name];
            if (value === undefined) return;
            const shown = field.unit ? `${value}${field.unit}` : value;
            parts.push(asked[field.name] === undefined
                ? `${field.label.toLowerCase()} ${shown} (required)`
                : `${field.label.toLowerCase()} ${shown}`);
        });
        if (!parts.length) {
            return 'Ready. Nothing set, so the provider\u2019s own defaults apply.';
        }
        return `Ready. Will send: ${parts.join(', ')}.`;
    }

    // A setting the catalog forced, as opposed to one that was chosen. Marked on the field
    // and the implied value written into a data attribute the stylesheet prints, rather
    // than typed into the control: putting it in the control would make it look like the
    // user's choice on the next check, and then a conflicting edit would read as their
    // mistake.
    function markForcedSettings(resolved) {
        const asked = videoSettingValues();
        VIDEO_SETTING_FIELDS.forEach(field => {
            const wrap = videoSettings.querySelector(`[data-field="${field.name}"]`);
            const label = wrap && wrap.querySelector('label');
            if (!wrap || !label) return;
            const value = resolved[field.name];
            const forced = value !== undefined && asked[field.name] === undefined;
            wrap.classList.toggle('forced', forced);
            if (forced) {
                label.dataset.forced = field.unit ? `${value}${field.unit}` : String(value);
            } else {
                delete label.dataset.forced;
            }
        });
    }

    async function runVideoCheck() {
        if (activeModality !== 'video') return;
        if (!modelSelect.value) {
            setVideoCheck('Select a video model.', 'refused', false);
            return;
        }
        const body = {
            model: modelSelect.value,
            roles: videoRoles(),
            ...videoSettingValues(),
        };
        try {
            const res = await fetch('/api/video/check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok) {
                // A non-200 here means the QUESTION was malformed -- an unknown model, a
                // misspelled role -- not that the combination was refused. Those come back
                // as 200 with ok=false, which is the branch below.
                markForcedSettings({});
                setVideoCheck(formatDetail(data.detail), 'broken', false);
                return;
            }
            if (!data.ok) {
                markForcedSettings({});
                setVideoCheck(data.detail, 'refused', false);
                return;
            }
            const resolved = data.resolved || {};
            markForcedSettings(resolved);
            setVideoCheck(describeResolved(resolved), 'ok', true);
        } catch (err) {
            console.error('Could not check the video request:', err);
            setVideoCheck(`Could not reach the server to check this: ${err.message}`,
                'broken', false);
        }
    }

    // -------------------------------- frames ----------------------------------
    function renderVideoFrames() {
        videoFrames.querySelectorAll('[data-frame-name]').forEach(node => {
            const role = node.dataset.frameName;
            const frame = videoFrameFiles.get(role);
            node.textContent = frame ? frame.name : 'none';
            node.classList.toggle('empty', !frame);
            const clear = videoFrames.querySelector(`[data-frame-clear="${role}"]`);
            if (clear) clear.hidden = !frame;
        });
    }

    // Read to a data URL on pick rather than at submit, so submitting stays synchronous
    // and an unreadable file is reported while the user is still looking at the picker.
    function setVideoFrame(role, file) {
        if (file.size > MAX_IMAGE_BYTES) {
            alert(`${file.name} is ${(file.size / 1048576).toFixed(1)} MB, over the `
                + `${MAX_IMAGE_BYTES / 1048576} MB per-image limit.`);
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            videoFrameFiles.set(role, {
                data_url: reader.result, name: file.name, bytes: file.size,
            });
            renderVideoFrames();
            scheduleVideoCheck();
        };
        reader.onerror = () => alert(`Could not read ${file.name}.`);
        reader.readAsDataURL(file);
    }

    videoFrames.querySelectorAll('[data-frame-pick]').forEach(button => {
        const role = button.dataset.framePick;
        button.addEventListener('click', () => {
            videoFrames.querySelector(`[data-frame-input="${role}"]`)?.click();
        });
    });

    videoFrames.querySelectorAll('[data-frame-input]').forEach(input => {
        const role = input.dataset.frameInput;
        input.addEventListener('change', () => {
            if (input.files && input.files[0]) setVideoFrame(role, input.files[0]);
            // Cleared so picking the same file twice in a row still fires a change.
            input.value = '';
        });
    });

    videoFrames.querySelectorAll('[data-frame-clear]').forEach(button => {
        const role = button.dataset.frameClear;
        button.addEventListener('click', () => {
            videoFrameFiles.delete(role);
            renderVideoFrames();
            scheduleVideoCheck();
        });
    });

    // ------------------------------- the jobs ---------------------------------
    function videoExtension(mime) {
        const known = {
            'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
        };
        return known[String(mime || '').toLowerCase()] || '.mp4';
    }

    function upsertJob(job) {
        videoJobRows.set(job.id, job);
        // A fresh row supersedes whatever transient note was on the card.
        videoJobNotes.delete(job.id);
        renderVideoJobs();
    }

    function noteOnJob(jobId, message, kind) {
        videoJobNotes.set(jobId, { message, kind });
        renderVideoJobs();
    }

    function buildJobCard(job) {
        const card = makeEl('div', 'video-job');

        const head = makeEl('div', 'video-job-head');
        head.appendChild(makeEl('span', 'video-job-model', job.model));
        head.appendChild(makeEl('span', `video-job-state ${job.state}`, job.state));
        head.appendChild(makeEl('span', 'video-job-meta',
            // Null when the job was resumed from a bare operation id and nobody knows when
            // it started. Reported as unknown rather than as zero.
            job.elapsed_s === null || job.elapsed_s === undefined
                ? 'age unknown'
                : `${Number(job.elapsed_s).toFixed(1)}s`));
        card.appendChild(head);

        card.appendChild(makeEl('div', 'video-job-prompt', job.prompt));

        const settings = Object.entries(job.requested || {})
            .map(([name, value]) => `${name}: ${value}`).join('   \u00b7   ');
        if (settings) card.appendChild(makeEl('div', 'video-job-settings', settings));

        if (job.video_url) {
            const player = document.createElement('video');
            player.className = 'result-video';
            player.controls = true;
            // Not autoplay and not preload="auto": a clip is tens of megabytes, and a page
            // holding several finished jobs would fetch all of them on render.
            player.preload = 'metadata';
            player.src = job.video_url;
            card.appendChild(player);

            const download = makeEl('a', 'btn-secondary', 'Download video');
            download.href = job.video_url;
            // The endpoint deliberately sends no Content-Disposition, so that the same URL
            // can be played inline. The filename belongs here instead.
            download.download =
                `${job.model}-${String(job.id).slice(0, 8)}${videoExtension(job.mime_type)}`;
            card.appendChild(download);
        }

        if (job.state === 'failed' && job.error) {
            card.appendChild(makeEl('div', 'video-job-note failed', job.error));
        }
        if (job.state === 'filtered') {
            const reasons = (job.filtered_reasons || []).join(', ');
            card.appendChild(makeEl('div', 'video-job-note filtered',
                reasons ? `Filtered by the provider: ${reasons}` : 'Filtered by the provider.'));
        }
        const note = videoJobNotes.get(job.id);
        if (note) card.appendChild(makeEl('div', `video-job-note ${note.kind}`, note.message));

        return card;
    }

    function renderVideoJobs() {
        const jobs = [...videoJobRows.values()]
            .sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
        videoJobList.replaceChildren(...jobs.map(buildJobCard));

        const running = jobs.filter(job => job.state === 'running').length;
        videoJobsSummary.textContent = jobs.length === 0
            ? 'no jobs yet'
            : `${jobs.length} job${jobs.length === 1 ? '' : 's'}`
            + (running ? `, ${running} running` : '');
    }

    function stopPolling(jobId) {
        const handle = videoPolls.get(jobId);
        if (handle) clearTimeout(handle);
        videoPolls.delete(jobId);
    }

    function schedulePoll(jobId) {
        stopPolling(jobId);
        videoPolls.set(jobId, setTimeout(() => pollJob(jobId), VIDEO_POLL_MS));
    }

    // A job this page submitted has finished. Appending is gated on having submitted it
    // here -- see videoWatched.
    function finishWatching(job) {
        if (!videoWatched.has(job.id)) return;
        videoWatched.delete(job.id);
        if (!appendContextToggle.checked) {
            console.info('Not appending the video turn: "Append responses" is off.');
            return;
        }
        // Null for a failed or filtered job: a generation that produced nothing must not
        // enter the transcript, which is the rule the text path already follows. The
        // server decides, so there is one definition of what the turn says.
        if (!job.transcript_entries) return;
        job.transcript_entries.forEach(entry => conversation.push(entry));
        saveContext();
        renderContext();
    }

    async function pollJob(jobId) {
        stopPolling(jobId);
        try {
            const res = await fetch(`/api/video/jobs/${encodeURIComponent(jobId)}/poll`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // A key typed into the sidebar is never stored with the job, so a poll
                // after a restart has to carry it again. That is also why this is a POST:
                // a credential does not belong in a URL.
                body: JSON.stringify(credentialFields()),
            });
            const data = await res.json();

            if (res.status === 503) {
                // The server says so in as many words: the job is still running and
                // polling again is safe. A note on the card, not a failure.
                noteOnJob(jobId, formatDetail(data.detail), 'transient');
                schedulePoll(jobId);
                return;
            }
            if (!res.ok) {
                // 410 expired, 400 missing credential, 404 unknown id. None of them
                // improve by asking again, so the loop stops and the card says why.
                noteOnJob(jobId, formatDetail(data.detail), 'failed');
                // The row may have changed -- a 410 is recorded as failed server-side --
                // so read it back rather than leaving the card claiming it is running.
                try {
                    const fresh = await fetch(`/api/video/jobs/${encodeURIComponent(jobId)}`);
                    if (fresh.ok) {
                        const row = await fresh.json();
                        videoJobRows.set(row.id, row);
                        renderVideoJobs();
                    }
                } catch (err) {
                    console.warn('Could not re-read the job row:', err);
                }
                return;
            }

            upsertJob(data);
            if (data.state === 'running') {
                schedulePoll(jobId);
            } else {
                finishWatching(data);
            }
        } catch (err) {
            // A dropped connection, not a verdict about the job.
            console.error(`Polling video job ${jobId} failed:`, err);
            noteOnJob(jobId, `Could not reach the server: ${err.message}`, 'transient');
            schedulePoll(jobId);
        }
    }

    // The page's half of surviving a restart: the server kept the job, and this is what
    // finds it again -- including one that finished while the page was closed. Called on
    // load whatever modality is active, because a job left running should be collected
    // regardless of which pill happens to be selected.
    async function loadVideoJobs() {
        try {
            const res = await fetch('/api/video/jobs');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const jobs = data.jobs || [];
            videoJobRows.clear();
            jobs.forEach(job => videoJobRows.set(job.id, job));
            renderVideoJobs();
            jobs.filter(job => job.state === 'running')
                .forEach(job => schedulePoll(job.id));
        } catch (err) {
            console.error('Could not load video jobs:', err);
            videoJobsSummary.textContent = 'could not reach the server';
        }
    }

    async function submitVideo(promptText) {
        const payload = {
            model: modelSelect.value,
            prompt: promptText,
            ...videoSettingValues(),
            ...credentialFields(),
        };
        const first = videoFrameFiles.get('first_frame');
        const last = videoFrameFiles.get('last_frame');
        if (first) payload.first_frame = { data_url: first.data_url, name: first.name };
        if (last) payload.last_frame = { data_url: last.data_url, name: last.name };

        try {
            const res = await fetch('/api/video/submit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) {
                // Left submittable on purpose. The check judges the COMBINATION and not
                // the frames, so a refusal here is often about a frame -- and disabling Run
                // would leave no way to retry after replacing it.
                setVideoCheck(`Submission refused: ${formatDetail(data.detail)}`,
                    'broken', true);
                return;
            }
            upsertJob(data);
            videoWatched.add(data.id);
            schedulePoll(data.id);
            setVideoCheck('Submitted. Watching it below.', 'ok', true);
        } catch (err) {
            console.error('Video submission failed:', err);
            setVideoCheck(`Could not reach the server: ${err.message}`, 'broken', true);
        }
    }

    function updateVideoMode() {
        const video = activeModality === 'video';

        videoGroup.classList.toggle('hidden', !video);
        videoFrames.classList.toggle('hidden', !video);
        videoJobs.classList.toggle('hidden', !video);

        // The single-shot panes describe a call that returned something. A submitted job
        // has not, so they are put away rather than left showing the last text run's
        // numbers underneath a video. Metrics are only ever ADDED to here: renderResults
        // reveals them on the next run that has any.
        attachRow.classList.toggle('hidden', video);
        attachList.classList.toggle('hidden', video);
        resultsContainer.classList.toggle('hidden', video);
        if (payloadDrawer) payloadDrawer.classList.toggle('hidden', video);
        if (video) metricsBar.classList.add('hidden');

        // "Run" overstates what the button does here: it submits and returns, and the
        // video arrives minutes later.
        executeBtn.textContent = video ? 'Submit video' : RUN_LABEL;

        if (video) {
            renderVideoFrames();
            renderVideoSettings();
            runVideoCheck();
        } else {
            executeBtn.disabled = false;
        }
    }

    refreshJobsBtn.addEventListener('click', loadVideoJobs);
    videoNegative.addEventListener('input', scheduleVideoCheck);

    renderVideoFrames();
    loadVideoJobs();

    // -------------------------------------------------------------
    // 7. PRESETS
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
        video: {
            // The system instruction is not part of a video request; it is set anyway so
            // switching back to a text preset does not inherit a blank one.
            system: 'You are a helpful assistant.',
            prompt: 'A slow aerial push-in over a misty pine forest at dawn, low sun '
                + 'breaking through the trees.',
            modality: 'video',
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
