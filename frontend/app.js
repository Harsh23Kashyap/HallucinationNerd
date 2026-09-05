// HallucinationNerd Web — Client-side logic

const form = document.getElementById('uploadForm');
const fileInput = document.getElementById('fileInput');
const dropZone = document.getElementById('dropZone');
const fileLabel = document.getElementById('fileLabel');
const submitBtn = document.getElementById('submitBtn');
const emptyState = document.getElementById('emptyState');
const loadingState = document.getElementById('loadingState');
const summaryBar = document.getElementById('summaryBar');
const claimsList = document.getElementById('claimsList');
const errorState = document.getElementById('errorState');

// File upload handling
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('border-blue-400', 'bg-blue-50');
});
dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('border-blue-400', 'bg-blue-50');
});
dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('border-blue-400', 'bg-blue-50');
    if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        fileLabel.textContent = e.dataTransfer.files[0].name;
    }
});
fileInput.addEventListener('change', () => {
    if (fileInput.files.length) {
        fileLabel.textContent = fileInput.files[0].name;
    }
});

// Form submission
form.addEventListener('submit', async (e) => {
    e.preventDefault();

    if (!fileInput.files.length) {
        alert('Please select a file to upload.');
        return;
    }

    // Show loading
    emptyState.classList.add('hidden');
    errorState.classList.add('hidden');
    summaryBar.classList.add('hidden');
    claimsList.innerHTML = '';
    document.getElementById('detailsHeader').style.display = 'none';
    document.getElementById('categoryReport').querySelectorAll('[id^="cat"]').forEach(el => el.classList.add('hidden'));
    loadingState.classList.remove('hidden');
    submitBtn.disabled = true;

    // Animated progress messages
    const loadingMsg = document.getElementById('loadingMsg');
    const stages = [
        'Extracting text from document...',
        'Identifying claims with citations...',
        'Resolving cited references...',
        'Downloading source papers from arXiv...',
        'Verifying claims against sources...',
        'Checking claim 1...',
        'Checking claim 2...',
        'Checking claim 3...',
        'Still verifying (this can take 1-2 minutes for large papers)...',
        'Almost done...',
    ];
    let stageIdx = 0;
    const progressInterval = setInterval(() => {
        if (stageIdx < stages.length) {
            loadingMsg.textContent = stages[stageIdx];
            stageIdx++;
        }
    }, 8000);

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    formData.append('source_type', document.getElementById('sourceType').value);
    const selectedDbs = [...document.querySelectorAll('.dbCheck:checked')].map(c => c.value);
    formData.append('databases', selectedDbs.join(','));
    formData.append('custom_database', document.getElementById('customDatabase').value.trim());

    try {
        const apiBase = (window.env && window.env.API_URL) ? window.env.API_URL : '';
        const response = await fetch(apiBase + '/verify', { method: 'POST', body: formData });
        const data = await response.json();

        clearInterval(progressInterval);
        loadingState.classList.add('hidden');
        submitBtn.disabled = false;

        if (data.error) {
            showError(data.error);
            return;
        }

        renderResults(data);
    } catch (err) {
        clearInterval(progressInterval);
        loadingState.classList.add('hidden');
        submitBtn.disabled = false;
        showError('Connection error. Please try again.');
    }
});

function showError(msg) {
    errorState.classList.remove('hidden');
    document.getElementById('errorMsg').textContent = msg;
}

function renderResults(data) {
    const s = data.summary;

    // Summary bar
    summaryBar.classList.remove('hidden');
    document.getElementById('summaryText').textContent =
        `${s.total_claims} claims analyzed from "${data.filename}"`;

    const pct = s.reliability_percent;
    const pctEl = document.getElementById('reliabilityPct');
    pctEl.textContent = `${pct}% reliable`;
    pctEl.className = `text-sm font-bold ${pct >= 80 ? 'text-green-600' : pct >= 50 ? 'text-yellow-600' : 'text-red-600'}`;

    const bar = document.getElementById('reliabilityBar');
    bar.style.width = `${pct}%`;
    bar.className = `h-2.5 rounded-full transition-all duration-500 ${pct >= 80 ? 'bg-green-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-red-500'}`;

    // Build categorized report (professor's format)
    const existingRefs = new Set();
    const nonExistentRefs = new Set();
    const supportedRefs = new Set();
    const partialRefs = new Set();
    const notSupportedRefs = new Set();
    const inaccessibleRefs = new Set();
    let noCitationCount = 0;
    let backupFoundCount = 0;
    let noBackupCount = 0;

    data.claims.forEach(c => {
        const refs = c.cited_refs || [];
        if (refs.length === 0) {
            // Uncited claim: bucket by backup-search outcome
            if (c.verdict === 'BACKUP_FOUND') backupFoundCount++;
            else if (c.verdict === 'NO_BACKUP_FOUND') noBackupCount++;
            else noCitationCount++;
            return;
        }
        refs.forEach(r => {
            if (c.citation_exists === true) {
                existingRefs.add(r);
            } else if (c.citation_exists === false || c.citation_exists === null || c.citation_exists === undefined) {
                inaccessibleRefs.add(r);
            }
        });

        if (c.verdict === 'SUPPORTED') {
            refs.forEach(r => supportedRefs.add(r));
        } else if (c.verdict === 'PARTIALLY_SUPPORTED') {
            refs.forEach(r => partialRefs.add(r));
        } else if (c.verdict === 'NOT_SUPPORTED' || c.verdict === 'CONTRADICTED') {
            refs.forEach(r => notSupportedRefs.add(r));
        }
    });

    // Show each category if it has entries
    function showCat(id, refsSet) {
        if (refsSet.size > 0) {
            const el = document.getElementById(id);
            el.classList.remove('hidden');
            const sorted = [...refsSet].sort((a, b) => a - b);
            document.getElementById(id + 'Refs').textContent = ' ' + sorted.map(r => `[${r}]`).join(', ');
        }
    }
    showCat('catExisting', existingRefs);
    showCat('catNonExistent', nonExistentRefs);
    showCat('catSupported', supportedRefs);
    showCat('catPartial', partialRefs);
    showCat('catNotSupported', notSupportedRefs);
    showCat('catInaccessible', inaccessibleRefs);
    if (noCitationCount > 0) {
        document.getElementById('catNoCitation').classList.remove('hidden');
        document.getElementById('catNoCitationRefs').textContent =
            ` ${noCitationCount} claim${noCitationCount === 1 ? '' : 's'} with no inline citation`;
    }
    if (backupFoundCount > 0) {
        document.getElementById('catBackupFound').classList.remove('hidden');
        document.getElementById('catBackupFoundRefs').textContent =
            ` ${backupFoundCount} uncited claim${backupFoundCount === 1 ? '' : 's'} matched to a supporting source`;
    }
    if (noBackupCount > 0) {
        document.getElementById('catNoBackup').classList.remove('hidden');
        document.getElementById('catNoBackupRefs').textContent =
            ` ${noBackupCount} uncited claim${noBackupCount === 1 ? '' : 's'} with no supporting source found`;
    }

    // Show detailed claims header
    document.getElementById('detailsHeader').style.display = 'block';

    // Render each claim card
    data.claims.forEach((claim, i) => {
        const card = document.createElement('div');
        card.className = `p-4 rounded-lg fade-in ${getVerdictClass(claim.verdict)}`;
        card.style.animationDelay = `${i * 0.05}s`;

        const noRefs = (!claim.cited_refs || claim.cited_refs.length === 0);
        let verdictBadge;
        if (claim.verdict === 'BACKUP_FOUND') {
            verdictBadge = { label: `✓ Backup Source Found${claim.backup_source ? ' — ' + claim.backup_source : ''}`, class: 'bg-teal-100 text-teal-700' };
        } else if (claim.verdict === 'NO_BACKUP_FOUND') {
            verdictBadge = { label: '✗ No Backup Source', class: 'bg-orange-100 text-orange-700' };
        } else if (noRefs) {
            verdictBadge = { label: '— No Citation Provided', class: 'bg-purple-100 text-purple-700' };
        } else {
            verdictBadge = getVerdictBadge(claim.verdict);
        }
        const confidence = claim.confidence ? `${Math.round(claim.confidence * 100)}%` : '';

        card.innerHTML = `
            <div class="flex items-start justify-between mb-2">
                <span class="text-xs font-medium px-2 py-0.5 rounded ${verdictBadge.class}">${verdictBadge.label}</span>
                ${confidence ? `<span class="text-xs text-gray-400">${confidence} confidence</span>` : ''}
            </div>
            <p class="text-sm text-gray-800 mb-2">"${escapeHtml(claim.claim)}"</p>
            ${claim.cited_refs && claim.cited_refs.length ? `<p class="text-xs text-gray-500 mb-2">Cited: [${claim.cited_refs.join(', ')}]</p>` : ''}
            ${claim.evidence_quote ? `
                <details class="mt-2">
                    <summary class="text-xs text-blue-600 cursor-pointer hover:underline">Show evidence</summary>
                    <blockquote class="mt-1 pl-3 border-l-2 border-gray-300 text-xs text-gray-600 italic">${escapeHtml(claim.evidence_quote)}</blockquote>
                </details>
            ` : ''}
            ${claim.reasoning ? `
                <details class="mt-1">
                    <summary class="text-xs text-gray-500 cursor-pointer hover:underline">Reasoning</summary>
                    <p class="mt-1 text-xs text-gray-500">${escapeHtml(claim.reasoning)}</p>
                </details>
            ` : ''}
        `;

        claimsList.appendChild(card);
    });
}

function getVerdictClass(verdict) {
    switch (verdict) {
        case 'SUPPORTED': return 'verdict-supported';
        case 'PARTIALLY_SUPPORTED': return 'verdict-partial';
        case 'NOT_SUPPORTED': return 'verdict-not-supported';
        case 'CONTRADICTED': return 'verdict-contradicted';
        case 'BACKUP_FOUND': return 'verdict-backup-found';
        case 'NO_BACKUP_FOUND': return 'verdict-no-backup';
        default: return 'verdict-unverifiable';
    }
}

function getVerdictBadge(verdict) {
    switch (verdict) {
        case 'SUPPORTED': return { label: '✓ Verified', class: 'bg-green-100 text-green-700' };
        case 'PARTIALLY_SUPPORTED': return { label: '◐ Partially Verified', class: 'bg-yellow-100 text-yellow-700' };
        case 'NOT_SUPPORTED': return { label: '✗ Not Supported', class: 'bg-red-100 text-red-700' };
        case 'CONTRADICTED': return { label: '⚠ Contradicted', class: 'bg-red-200 text-red-800' };
        default: return { label: '? Could Not Verify', class: 'bg-gray-100 text-gray-600' };
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
