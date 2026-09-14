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
    const _df = document.getElementById('doneFlag'); if (_df) _df.classList.add('hidden');
    claimsList.innerHTML = '';
    document.getElementById('detailsHeader').style.display = 'none';
    loadingState.classList.remove('hidden');
    submitBtn.disabled = true;

    // Animated progress messages
    const loadingMsg = document.getElementById('loadingMsg');
    const stages = [
        'Extracting text from the document...',
        'Identifying claims and their citations...',
        'Resolving the cited references...',
        'Retrieving the cited source documents...',
        'Checking each claim against its cited source...',
        'Still working — larger papers can take 1–2 minutes...',
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
        const response = await fetch('/verify', { method: 'POST', body: formData });
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
    const doneFlag = document.getElementById('doneFlag');
    if (doneFlag) doneFlag.classList.remove('hidden');

    // Summary
    summaryBar.classList.remove('hidden');
    document.getElementById('summaryText').textContent =
        `${s.total_claims} claims analyzed from "${data.filename}"`;
    document.getElementById('summaryCount').textContent = s.total_claims;
    document.getElementById('summaryFile').textContent = data.filename;

    // Numerical summary — the paper's six verdict categories, with counts.
    document.getElementById('vcSupported').textContent = s.supported || 0;
    document.getElementById('vcPartial').textContent = s.partially_supported || 0;
    document.getElementById('vcNotSupported').textContent = s.not_supported || 0;
    document.getElementById('vcContradicted').textContent = s.contradicted || 0;
    document.getElementById('vcNotFound').textContent = s.citation_not_found || 0;
    document.getElementById('vcNoAccess').textContent = (s.inaccessible || 0) + (s.unverifiable || 0);

    // Show detailed claims header
    document.getElementById('detailsHeader').style.display = 'block';
    resetCategoryFilter();

    // Render each claim card
    data.claims.forEach((claim, i) => {
        const card = document.createElement('div');
        card.className = `claim-card fade-in ${getVerdictClass(claim.verdict)}`;
        card.dataset.cat = categoryOf(claim.verdict);
        card.style.animationDelay = `${i * 0.05}s`;

        const noRefs = (!claim.cited_refs || claim.cited_refs.length === 0);
        let verdictBadge;
        if (claim.verdict === 'BACKUP_FOUND') {
            verdictBadge = { label: `Backup source found${claim.backup_source ? ' — ' + claim.backup_source : ''}`, class: 'badge-backup-found' };
        } else if (claim.verdict === 'BACKUP_PARTIAL') {
            verdictBadge = { label: `Backup source — partial${claim.backup_source ? ' — ' + claim.backup_source : ''}`, class: 'badge-hn-partial' };
        } else if (claim.verdict === 'NO_BACKUP_FOUND') {
            verdictBadge = { label: 'No backup source', class: 'badge-no-backup' };
        } else if (claim.verdict === 'CITATION_NOT_FOUND') {
            verdictBadge = { label: "Cited article doesn't exist", class: 'badge-hn-noexist' };
        } else if (claim.verdict === 'INACCESSIBLE' || claim.verdict === 'UNVERIFIABLE') {
            verdictBadge = { label: 'Could not access', class: 'badge-hn-noaccess' };
        } else if (noRefs) {
            verdictBadge = { label: 'No citation provided', class: 'badge-no-citation' };
        } else {
            verdictBadge = getVerdictBadge(claim.verdict);
        }
        const confPct = claim.confidence ? Math.round(claim.confidence * 100) : 0;
        const confHtml = claim.confidence
            ? `<span class="conf"><span class="conf-track"><span class="conf-fill" style="width:${confPct}%"></span></span>${confPct}%</span>`
            : '';

        const refChips = (claim.cited_refs && claim.cited_refs.length)
            ? `<p class="claim-refs">${claim.cited_refs.map(r => `<span class="ref-chip">${escapeHtml(r)}</span>`).join('')}</p>`
            : '';

        card.innerHTML = `
            <div class="claim-top">
                <span class="claim-idx">${String(i + 1).padStart(2, '0')}</span>
                <span class="verdict-wordmark ${verdictBadge.class}"><span class="vw-dot"></span>${verdictBadge.label}</span>
                ${confHtml}
            </div>
            <p class="claim-text">${escapeHtml(claim.claim)}</p>
            ${refChips}
            ${claim.evidence_quote ? `
                <details>
                    <summary>Evidence</summary>
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
    applyClaimFilter();
}

function getVerdictClass(verdict) {
    switch (verdict) {
        case 'SUPPORTED': return 'verdict-supported';
        case 'PARTIALLY_SUPPORTED': return 'verdict-partial';
        case 'NOT_SUPPORTED': return 'verdict-not-supported';
        case 'CONTRADICTED': return 'verdict-contradicted';
        case 'CITATION_NOT_FOUND': return 'verdict-not-found';
        case 'BACKUP_FOUND': return 'verdict-backup-found';
        case 'BACKUP_PARTIAL': return 'verdict-partial';
        case 'NO_BACKUP_FOUND': return 'verdict-no-backup';
        case 'INACCESSIBLE': return 'verdict-noaccess';
        case 'UNVERIFIABLE': return 'verdict-noaccess';
        default: return 'verdict-noaccess';
    }
}

function getVerdictBadge(verdict) {
    switch (verdict) {
        case 'SUPPORTED': return { label: 'Supported', class: 'badge-hn-supported' };
        case 'PARTIALLY_SUPPORTED': return { label: 'Partially supported', class: 'badge-hn-partial' };
        case 'NOT_SUPPORTED': return { label: 'Not supported', class: 'badge-hn-notsupp' };
        case 'CONTRADICTED': return { label: 'Contradicted', class: 'badge-hn-contra' };
        default: return { label: 'Could not access', class: 'badge-hn-noaccess' };
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


// ---- Category-box filtering: click a summary box to filter the claim cards ----
function categoryOf(verdict) {
    switch (verdict) {
        case 'SUPPORTED': return 'SUPPORTED';
        case 'PARTIALLY_SUPPORTED': return 'PARTIALLY_SUPPORTED';
        case 'NOT_SUPPORTED': return 'NOT_SUPPORTED';
        case 'CONTRADICTED': return 'CONTRADICTED';
        case 'CITATION_NOT_FOUND': return 'CITATION_NOT_FOUND';
        case 'INACCESSIBLE':
        case 'UNVERIFIABLE': return 'NOACCESS';
        default: return 'OTHER';
    }
}

const selectedCats = new Set();

function applyClaimFilter() {
    const cards = claimsList.querySelectorAll('.claim-card');
    cards.forEach(c => {
        const show = selectedCats.size === 0 || selectedCats.has(c.dataset.cat);
        c.style.display = show ? '' : 'none';
    });
    const hint = document.getElementById('filterHint');
    if (hint) {
        hint.textContent = selectedCats.size === 0
            ? 'Tip: click a category to show only those claims (click again to clear).'
            : 'Filtering by ' + selectedCats.size + ' categor' + (selectedCats.size === 1 ? 'y' : 'ies') + ' — click a highlighted box to clear it.';
    }
}

function resetCategoryFilter() {
    selectedCats.clear();
    document.querySelectorAll('#verdictSummary .vrow').forEach(row => {
        row.classList.remove('selected');
        const el = row.querySelector('.vcount');
        const cnt = parseInt((el && el.textContent) || '0', 10);
        row.classList.toggle('disabled', cnt === 0);
    });
}

function setupCategoryFilter() {
    document.querySelectorAll('#verdictSummary .vrow').forEach(row => {
        row.addEventListener('click', () => {
            if (row.classList.contains('disabled')) return;
            const cat = row.dataset.cat;
            if (selectedCats.has(cat)) { selectedCats.delete(cat); row.classList.remove('selected'); }
            else { selectedCats.add(cat); row.classList.add('selected'); }
            applyClaimFilter();
        });
    });
}
setupCategoryFilter();
