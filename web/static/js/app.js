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
const retryOffer = document.getElementById('retryOffer');
const patientRetryBtn = document.getElementById('patientRetryBtn');
const reviewTools = document.getElementById('reviewTools');
const sampleNotice = document.getElementById('sampleNotice');
let currentReport = null;
let isSampleReport = false;
let reviewOrder = 'document';

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
    await runVerification(false);
});

patientRetryBtn.addEventListener('click', async () => {
    await runVerification(true);
});

async function runVerification(patientWait) {

    if (!fileInput.files.length) {
        showError('Please select a file to upload.');
        return;
    }

    // Show loading
    isSampleReport = false; currentReport = null;
    sampleNotice.classList.add('hidden'); reviewTools.classList.add('hidden');
    emptyState.classList.add('hidden');
    errorState.classList.add('hidden');
    summaryBar.classList.add('hidden');
    retryOffer.classList.add('hidden');
    const _df = document.getElementById('doneFlag'); if (_df) _df.classList.add('hidden');
    claimsList.replaceChildren();
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
    const customDb = document.getElementById('customDatabase');  // absent in private mode
    formData.append('custom_database', customDb ? customDb.value.trim() : '');
    formData.append('patient_wait', patientWait ? 'true' : 'false');

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
        if (data.retry_offer) retryOffer.classList.remove('hidden');
    } catch (err) {
        clearInterval(progressInterval);
        loadingState.classList.add('hidden');
        submitBtn.disabled = false;
        showError('Connection error. Please try again.');
    }
}

function showError(msg) {
    errorState.classList.remove('hidden');
    document.getElementById('errorMsg').textContent = msg;
}

function renderResults(data) {
    const s = data.summary;
    currentReport = data;
    reviewOrder = 'document';
    reviewTools.classList.remove('hidden');
    updateReviewOrderButtons();

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
    claimsList.replaceChildren();
    data.claims.forEach((claim, i) => {
        const card = document.createElement('div');
        card.className = `claim-card fade-in ${getVerdictClass(claim.verdict)}`;
        card.dataset.cat = categoryOf(claim.verdict);
        card.dataset.order = String(i);
        card.id = `result-claim-${i+1}`;
        // Bound the entrance sequence on long reports (PR#10), keep ordering ids (PR#9).
        if (i < 8) card.style.animationDelay = `${i * 0.045}s`;
        else card.classList.remove('fade-in');

        const noRefs = (!claim.cited_refs || claim.cited_refs.length === 0);
        let verdictBadge;
        if (claim.verdict === 'BACKUP_FOUND') {
            verdictBadge = { label: `Backup source found${claim.backup_source ? ' — ' + escapeHtml(claim.backup_source) : ''}`, class: 'badge-backup-found' };
        } else if (claim.verdict === 'BACKUP_PARTIAL') {
            verdictBadge = { label: `Backup source — partial${claim.backup_source ? ' — ' + escapeHtml(claim.backup_source) : ''}`, class: 'badge-hn-partial' };
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
        const confPct = claim.confidence ? Math.max(0, Math.min(100, Math.round(Number(claim.confidence) * 100) || 0)) : 0;
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
    renderSourceMap(data);
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


// Review results without changing the underlying verdicts or document order.
const repairPriority = {CONTRADICTED:0, NOT_SUPPORTED:1, CITATION_NOT_FOUND:2, NO_BACKUP_FOUND:2, INACCESSIBLE:3, UNVERIFIABLE:3, PARTIALLY_SUPPORTED:4, BACKUP_PARTIAL:4, BACKUP_FOUND:5, SUPPORTED:6};
function updateReviewOrderButtons() {
    const fix = reviewOrder === 'fix';
    document.getElementById('fixFirstBtn').setAttribute('aria-pressed', String(fix));
    document.getElementById('originalOrderBtn').setAttribute('aria-pressed', String(!fix));
    document.getElementById('reviewHint').textContent = fix
        ? 'Repair queue: contradicted, unsupported, missing source, inaccessible, then other verdicts. Original claim numbers stay attached.'
        : 'Document order shows claims as they appeared in the input.';
    if (!currentReport) return;
    const cards = [...claimsList.querySelectorAll('.claim-card')];
    cards.sort((a,b) => fix
        ? (repairPriority[currentReport.claims[Number(a.dataset.order)].verdict] ?? 4) - (repairPriority[currentReport.claims[Number(b.dataset.order)].verdict] ?? 4) || Number(a.dataset.order) - Number(b.dataset.order)
        : Number(a.dataset.order) - Number(b.dataset.order));
    cards.forEach(card => claimsList.appendChild(card));
}
document.getElementById('fixFirstBtn').addEventListener('click', () => { reviewOrder = 'fix'; updateReviewOrderButtons(); });
document.getElementById('originalOrderBtn').addEventListener('click', () => { reviewOrder = 'document'; updateReviewOrderButtons(); });

function safeSourceUrl(value) {
    try { const u = new URL(String(value || '')); return u.protocol === 'https:' ? u.href : ''; }
    catch { return ''; }
}
function renderSourceMap(data) {
    const groups = document.getElementById('sourceMapGroups');
    groups.replaceChildren();
    const byRef = new Map();
    (data.claims || []).forEach((claim, i) => {
        const refs = Array.isArray(claim.cited_refs) ? [...new Set(claim.cited_refs.map(String))] : [];
        if (!refs.length) refs.push('uncited');
        refs.forEach(ref => { if (!byRef.has(ref)) byRef.set(ref, []); byRef.get(ref).push(i); });
    });
    document.getElementById('sourceMapCount').textContent = `(${byRef.size})`;
    for (const [ref, indexes] of byRef) {
        const meta = ref === 'uncited' ? {} : (data.source_map || {})[ref] || {};
        const section = document.createElement('section'); section.className = 'source-map-group';
        const heading = document.createElement('h4');
        heading.textContent = ref === 'uncited' ? 'No inline citation' : `Reference [${ref}]${meta.title ? ' · ' + meta.title : ''}`;
        section.append(heading);
        const url = safeSourceUrl(meta.url) || (meta.doi && /^10\.\d{4,9}\//.test(meta.doi) ? safeSourceUrl(`https://doi.org/${encodeURI(meta.doi)}`) : '');
        if (url) { const link = document.createElement('a'); link.href=url; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent='Open source'; section.append(link); }
        const list = document.createElement('ul');
        indexes.forEach(i => { const li=document.createElement('li'); const a=document.createElement('a'); a.href=`#result-claim-${i+1}`; a.addEventListener('click', () => { if (selectedCats.size) { resetCategoryFilter(); applyClaimFilter(); } }); a.textContent=`Claim ${i+1}: ${(data.claims[i].claim || '').slice(0,95)}`; li.append(a); list.append(li); });
        section.append(list); groups.append(section);
    }
    document.getElementById('sourceMapPanel').hidden = byRef.size === 0;
}

document.getElementById('tryDemoBtn').addEventListener('click', async () => {
    try {
        const response = await fetch('/static/demo-report.json');
        if (!response.ok) throw new Error('Sample unavailable');
        const data = await response.json();
        isSampleReport = true;
        loadingState.classList.add('hidden'); errorState.classList.add('hidden'); retryOffer.classList.add('hidden');
        renderResults(data);
        sampleNotice.classList.remove('hidden');
        document.getElementById('reviewTools').scrollIntoView({block:'start',behavior:'smooth'});
    } catch { showError('Could not open the sample. Please try again.'); }
});


// A portable, script-free report. No remote assets or live API calls are needed to reopen it.
function exportHtmlReport(data, sample) {
    const esc = value => escapeHtml(String(value ?? '')).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const cite = claim => Array.isArray(claim.cited_refs) && claim.cited_refs.length
        ? claim.cited_refs.map(ref => `[${esc(ref)}]`).join(' ') : 'No inline citation';
    const sourceRows = Object.entries(data.source_map || {}).map(([key, source]) =>
        `<li><b>[${esc(key)}]</b> ${esc(source.title || 'Reference details unavailable')}` +
        `${source.doi ? ` · DOI: ${esc(source.doi)}` : ''}${source.url ? ` · URL: ${esc(source.url)}` : ''}</li>`).join('');
    const claims = (data.claims || []).map((claim, i) => `<article>
        <div class="meta">Claim ${i + 1} · ${esc(claim.verdict || 'Unknown')} · ${cite(claim)}</div>
        <h2>${esc(claim.claim)}</h2>
        ${claim.evidence_quote ? `<h3>Evidence</h3><blockquote>${esc(claim.evidence_quote)}</blockquote>` : ''}
        ${claim.reasoning ? `<h3>Reasoning</h3><p>${esc(claim.reasoning)}</p>` : ''}
    </article>`).join('');
    const count = Number(data.summary?.total_claims) || (data.claims || []).length;
    return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claim review · ${esc(data.filename)}</title><style>
:root{color-scheme:light}body{margin:0 auto;max-width:820px;padding:32px 20px 80px;background:#faf8f3;color:#242623;font:16px/1.55 system-ui,sans-serif}header{border-bottom:3px solid #a73f2c;padding-bottom:22px}h1{margin:0;font-size:28px}h2{font-size:18px;line-height:1.4;margin:8px 0}h3{font-size:13px;text-transform:uppercase;letter-spacing:.05em;margin:18px 0 5px}.meta,.fine{font-size:13px;color:#585b58;overflow-wrap:anywhere}article{background:white;padding:22px;margin-top:16px;border:1px solid #dad9d3;border-radius:9px;break-inside:avoid;overflow-wrap:anywhere}blockquote{margin:5px 0;padding:10px 16px;background:#f4f1ea;border-left:3px solid #a73f2c}li{margin:9px 0;overflow-wrap:anywhere}.warning{background:#fff2d8;padding:13px;border-left:4px solid #ae7528}section{margin-top:30px}@media print{body{background:white}article{border-color:#888}}
</style></head><body><header><div class="fine">HALLUCINATIONNERD · ${sample ? 'ILLUSTRATIVE SAMPLE' : 'DOCUMENT REPORT'}</div><h1>Claim review</h1><p>${esc(data.filename)} · ${count} claims</p><div class="warning">This file may contain private document text, evidence and source details. Store and share it carefully. ${sample ? 'These are illustrative results, not a live verification.' : 'Verdicts are automated and should be reviewed against the cited sources.'}</div></header>
<section><h2>Sources in this document</h2><p class="fine">Reference labels are local to this document. They do not establish source identity across documents.</p>${sourceRows ? `<ol>${sourceRows}</ol>` : '<p>No source metadata available.</p>'}</section>
<section><h2>Claims in document order</h2>${claims || '<p>No claims found.</p>'}</section></body></html>`;
}
document.getElementById('exportReportBtn').addEventListener('click', () => {
    if (!currentReport) return;
    const html = exportHtmlReport(currentReport, isSampleReport);
    const blob = new Blob([html], {type: 'text/html;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const base = String(currentReport.filename || 'report').replace(/[^a-z0-9._-]/gi, '-').slice(0,70);
    link.download = `${base || 'report'}-claim-review.html`;
    link.href = url; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
});
