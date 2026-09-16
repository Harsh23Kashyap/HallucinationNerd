# HallucinationNerd

## About

HallucinationNerd is a collaborative research project that checks whether cited sources support the claims attributed to them. The project is developed by:

- **Taranum Wasu** - Thapar Institute of Engineering and Technology, Patiala, India
- **Harsh Kashyap** - Thapar Institute of Engineering and Technology, Patiala, India
- **Vishnu Chennur** - Downingtown STEM Academy, Downingtown, PA, USA
- **Dennis Shasha** - Department of Computer Science, New York University, New York, USA

**A citation-level hallucination verification engine for synopsis-generating systems.**

HallucinationNerd checks whether cited sources actually support the claims attributed to them. Given any text with inline citations — a RAG synopsis, a research paper's Related Work, or an agentic system's report — it verifies each (claim, source) pair and returns a per-claim conclusion (verified correct, verified incorrect, cited article does not exist, or unknown) with evidence pointers.

![Architecture](figures/hallucinationnerd-architecture.png)

## Key Results

Evaluated on two benchmarks built from **100 randomly sampled arXiv papers** (one genuine claim per paper). For each paper, half of its arXiv citations are replaced with arXiv papers chosen uniformly at random, producing objective, annotation-free ground truth: a citation is **correct** or **incorrect**. A citation whose source cannot be accessed is excluded from scoring (not counted as a hallucination), matching the evaluation in the paper.

| Benchmark | Accuracy | Precision | Recall |
|-----------|----------|-----------|--------|
| Cross-category (out-of-field) — 200 pairs (185 scored) | **93.0%** | 88.1% | 100% |
| Same-field (in-field) — 196 pairs (181 scored) | **92.8%** | 87.6% | 100% |

Against the systems we ran on identical data — **MiniCheck** and **RAGAS** — HallucinationNerd is far ahead: both competitors sit near chance (55–58% accuracy) because they flag roughly half of the *correct* citations, while HallucinationNerd keeps false positives low. Paired non-parametric permutation tests confirm the gap is significant (p < 0.0001 on both benchmarks). Additional NLI baselines (HHEM 2.1, AlignScore, SummaCConv) are evaluated in the paper.

| System | Cross-category Acc | Same-field Acc |
|---|---|---|
| **HallucinationNerd** | **93.0%** | **92.8%** |
| RAGAS | 58.4% | 58.0% |
| MiniCheck | 55.7% | 54.7% |

## How It Works

1. **Claim Extraction** — Decomposes text into atomic, decontextualized claims with citation markers
2. **Reference Matching** — Retrieves source content (PDF, web, text) and finds relevant passages via keyword-overlap chunking
3. **Document Interrogation** — Asks the source document itself whether it supports the claim using a structured LLM judge
4. **Aggregation** — Produces per-claim conclusions, evidence quotes, and overall reliability scores

For uncited claims, the system runs a backup search over user-selectable databases — PubMed, arXiv, and Semantic Scholar (plus an optional user-supplied search URL) — via `--search-backup` (CLI) or the source-database checkboxes in the web app.

## Installation

```bash
pip install -r requirements.txt
```

Optional, for PDF and web-page source support:
```bash
pip install pymupdf beautifulsoup4 requests
```

Requires an OpenAI API key in `.env`:
```
OPENAI_API_KEY=your-key-here
```

## Usage

```bash
# Verify citations in a document
python verify_hallucinations.py --input data.json --output-dir results/ --task citation

# Also run backup search (PubMed / arXiv / Semantic Scholar) for uncited claims
python verify_hallucinations.py --input data.json --output-dir results/ --task citation --search-backup

# Strictness: 'lenient' (default, precision-oriented) or 'strict' (recall-oriented)
python verify_hallucinations.py --input data.json --output-dir results/ --task citation --strictness strict
```

## Input Format

```json
[
  {
    "question_id": "Q1",
    "question": "What was asked",
    "synopsis": "The answer with [1] citations to verify",
    "retrieved_articles": [
      { "id": "1", "title": "Source title", "content": "Full text...", "url": "https://...", "path": "/path/to/file.pdf" }
    ]
  }
]
```

Sources can be pre-extracted text (`content`), URLs to fetch (`url`), or PDF/text file paths (`path`).

## Verdict Taxonomy

| Verdict | Meaning |
|---------|---------|
| `SUPPORTED` | Source directly states or logically implies the claim |
| `PARTIALLY_SUPPORTED` | Source discusses the topic; gist correct but specifics differ |
| `NOT_SUPPORTED` | Source does not contain information relevant to this claim |
| `CONTRADICTED` | Source explicitly states the opposite |
| `UNVERIFIABLE` | Source content could not be accessed (excluded from scoring) |
| `BACKUP_FOUND` / `NO_BACKUP_FOUND` | Uncited claim: backup search over the selected databases found / did not find supporting evidence |

## Benchmarks & Reproduction

`datasets/` holds the two benchmarks, each as a self-contained `*_input.json` (claim + embedded source text) and `*_gt.json` (CORRECT / INCORRECT labels):

- `crosscat_*` — cross-category (out-of-field), 200 pairs (100 correct + 100 incorrect)
- `samefield_*` — same-field (in-field), 196 pairs (100 correct + 96 incorrect)

```bash
# Score any NLI/LLM system against a benchmark (edit the tool call inside)
python benchmark_template.py

# Reproduce HallucinationNerd verdicts + competitor baselines (needs: pip install minicheck ragas datasets)
python run_v100.py
python run_competitors_v100.py v100_crosscat
python run_competitors_v100.py v100_samefield
```

`build_v100_benchmark.py` documents how the benchmarks were sampled and quality-filtered from arXiv (it downloads source PDFs at run time; the raw PDFs are not shipped here since their text is already embedded in the dataset JSONs).

## Statistical Validation

```bash
python statistical_tests/Diff2MeanSig.py    # permutation test (p-value)
python statistical_tests/Diff2MeanConf.py   # bootstrap confidence interval
```

## Citation

```
@article{wasu2026hallucinationnerd,
  title={HallucinationNerd: A Framework and Tool for Detecting Citation Hallucinations in Synopsis-Generating Systems},
  author={Wasu, Taranum and Kashyap, Harsh and Chennur, Vishnu and Shasha, Dennis},
  year={2026}
}
```

## Authors

- **Taranum Wasu** — Thapar Institute of Engineering and Technology, Patiala, India
- **Harsh Kashyap** — Thapar Institute of Engineering and Technology, Patiala, India
- **Vishnu Chennur** — Downingtown STEM Academy, Downingtown, PA, USA
- **Dennis Shasha** — Department of Computer Science, New York University, New York, USA
