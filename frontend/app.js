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
dropZone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        fileInput.click();
    }
});
dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});
dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});
dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
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
        showError('Please select a file to upload.');
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

    emptyState.classList.add('hidden');

    // Summary
    summaryBar.classList.remove('hidden');
    document.getElementById('summaryText').textContent =
        `${s.total_claims} claims analyzed from "${data.filename}"`;
    document.getElementById('summaryCount').textContent = s.total_claims;
    document.getElementById('summaryFile').textContent = data.filename;

    const pct = s.reliability_percent;
    const tone = pct >= 80 ? 'tone-good' : pct >= 50 ? 'tone-mid' : 'tone-bad';
    const pctEl = document.getElementById('reliabilityPct');
    pctEl.className = `reliability-num ${tone}`;
    pctEl.innerHTML = `${pct}%<small>reliable</small>`;

    const bar = document.getElementById('reliabilityBar');
    bar.style.width = `${pct}%`;
    bar.className = `meter-fill ${tone}`;

    // Verdict counts -> stat grid (real numbers, no blank bars)
    let nSupported = 0, nPartial = 0, nNotSupported = 0, nUncited = 0;
    data.claims.forEach(c => {
        const noRefs = (!c.cited_refs || c.cited_refs.length === 0);
        if (noRefs) { nUncited++; return; }
        if (c.verdict === 'SUPPORTED') nSupported++;
        else if (c.verdict === 'PARTIALLY_SUPPORTED') nPartial++;
        else if (c.verdict === 'NOT_SUPPORTED' || c.verdict === 'CONTRADICTED') nNotSupported++;
    });
    document.getElementById('statTotal').textContent = data.claims.length;
    document.getElementById('statSupported').textContent = nSupported;
    document.getElementById('statPartial').textContent = nPartial;
    document.getElementById('statNotSupported').textContent = nNotSupported;
    document.getElementById('statUncited').textContent = nUncited;

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
            document.getElementById(id + 'Refs').textContent = sorted.map(r => `[${r}]`).join(', ');
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
            `${noCitationCount} claim${noCitationCount === 1 ? '' : 's'} with no inline citation`;
    }
    if (backupFoundCount > 0) {
        document.getElementById('catBackupFound').classList.remove('hidden');
        document.getElementById('catBackupFoundRefs').textContent =
            `${backupFoundCount} uncited claim${backupFoundCount === 1 ? '' : 's'} matched to a supporting source`;
    }
    if (noBackupCount > 0) {
        document.getElementById('catNoBackup').classList.remove('hidden');
        document.getElementById('catNoBackupRefs').textContent =
            `${noBackupCount} uncited claim${noBackupCount === 1 ? '' : 's'} with no supporting source found`;
    }

    // Show detailed claims header
    document.getElementById('detailsHeader').style.display = 'block';

    // Render each claim card
    data.claims.forEach((claim, i) => {
        const card = document.createElement('div');
        card.className = `claim-card fade-in ${getVerdictClass(claim.verdict)}`;
        card.style.animationDelay = `${i * 0.05}s`;

        const noRefs = (!claim.cited_refs || claim.cited_refs.length === 0);
        let verdictBadge;
        if (claim.verdict === 'BACKUP_FOUND') {
            verdictBadge = { label: `✓ Backup Source Found${claim.backup_source ? ' — ' + claim.backup_source : ''}`, class: 'badge-backup-found' };
        } else if (claim.verdict === 'NO_BACKUP_FOUND') {
            verdictBadge = { label: '✗ No Backup Source', class: 'badge-no-backup' };
        } else if (noRefs) {
            verdictBadge = { label: '— No Citation Provided', class: 'badge-no-citation' };
        } else {
            verdictBadge = getVerdictBadge(claim.verdict);
        }
        const confPct = claim.confidence ? Math.round(claim.confidence * 100) : 0;
        const confHtml = claim.confidence
            ? `<span class="conf"><span class="conf-track"><span class="conf-fill" style="width:${confPct}%"></span></span>${confPct}% conf.</span>`
            : '';

        card.innerHTML = `
            <div class="claim-top">
                <span class="badge ${verdictBadge.class}">${verdictBadge.label}</span>
                ${confHtml}
            </div>
            <p class="claim-text">"${escapeHtml(claim.claim)}"</p>
            ${claim.cited_refs && claim.cited_refs.length ? `<p class="claim-refs">Cited: [${claim.cited_refs.join(', ')}]</p>` : ''}
            ${claim.evidence_quote ? `
                <details>
                    <summary>Show evidence</summary>
                    <blockquote>${escapeHtml(claim.evidence_quote)}</blockquote>
                </details>
            ` : ''}
            ${claim.reasoning ? `
                <details class="secondary">
                    <summary>Reasoning</summary>
                    <p class="reasoning">${escapeHtml(claim.reasoning)}</p>
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
        case 'SUPPORTED': return { label: '✓ Verified', class: 'badge-supported' };
        case 'PARTIALLY_SUPPORTED': return { label: '◐ Partially Verified', class: 'badge-partial' };
        case 'NOT_SUPPORTED': return { label: '✗ Not Supported', class: 'badge-not-supported' };
        case 'CONTRADICTED': return { label: '⚠ Contradicted', class: 'badge-contradicted' };
        default: return { label: '? Could Not Verify', class: 'badge-unverifiable' };
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
