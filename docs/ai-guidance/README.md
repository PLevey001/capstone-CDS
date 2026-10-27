# Shared model-independent CDS guidance

Version: 0.2 — implemented runtime subset and reader-facing specification.

The goal is a case brief a person can understand and educational/case chat that answers what the supplied references support. JSON preserves the structure; readable prose explains the result. These rules describe application model behavior. They are not developer `AGENTS.md` instructions and do not give a coding agent permissions.

## Index

| File | Purpose |
| --- | --- |
| [shared-rules.md](shared-rules.md) | Common forensic distinctions and trust boundaries for every operation. |
| [brief-guidelines.md](brief-guidelines.md) | Concise takeaways, coverage gaps and optional meaningful hypotheses. |
| [chat-guidelines.md](chat-guidelines.md) | Partial answers, concepts, case snapshots and prior AI interpretation. |
| [examples.json](examples.json) | Authored positive/rejected synthetic examples; not measured model successes. |
| [evaluation.md](evaluation.md) | Practical adoption/edit/evaluation steps, human rubric and limitations. |

## What actually runs

[`cds/prompt_guidance.py`](../../cds/prompt_guidance.py) reads six fixed, allow-listed UTF-8 files from [`cds/prompts`](../../cds/prompts) once at process import/startup. Missing, unreadable, invalid UTF-8, blank or symlink files fail clearly; there is no fallback. Requests and environment settings cannot select a filename. The application chooses the operation from its brief route, validated chat mode and server-resolved selected brief.

The exact composition order is:

| Operation | System content, in order |
| --- | --- |
| Saved brief (`brief`) | `common.txt` + `case-brief.txt` |
| Concept chat (`concepts`) | `common.txt` + `chat-common.txt` + `chat-concepts.txt` |
| Case chat (`case`) | `common.txt` + `chat-common.txt` + `chat-case.txt` |
| Selected prior assessment chat (`prior_assessment`) | `common.txt` + `chat-common.txt` + `chat-case.txt` + `chat-prior-assessment.txt` |

Each file is decoded as UTF-8 using Python text-mode newline normalization, stripped of leading/trailing whitespace, then joined with two newline characters. That final text is the single SYSTEM message, is included in the conservative context budget, and is hashed as UTF-8 SHA-256. There are no hidden mode instructions appended after hashing. References, questions, history and prior AI remain user-message data, separate from this trusted guidance.

Brief metadata uses `prompt_version: case-assessment.v4`; chat uses `learning.v3`. Both return `prompt_sha256` for the entire composed system message and `guidance_version: 0.2`. Chat mode and selecting a prior assessment change the checksum. These hashes describe instructions, not facts, schema, model weights or semantic quality. Existing input/fact/model metadata retain their separate meanings.

Successful briefs save that metadata in the immutable JSON document and download the same stored bytes. Existing saved documents are not rewritten or assigned new metadata. Chat returns its own metadata even when the model gives a valid refusal. Evaluation manifests record versions/checksums before every operation, including rejected attempts and operations not run.

**Restart CDS after any runtime-file edit.** There is no live file watching or request-driven reload. Changing this documentation or its examples alone does not change prompts or train weights. The full documents are the reader-facing specification; the compact text files are its runtime subset. Documentation, rejected wording and examples are never automatically sent to the model.

## Contracts and compatibility

The model response is not a complete saved document. Brief JSON has `overall_summary`, `observations`, `inferences`, `review_next`; each item has `statement` and `facts`. Inferences additionally require `reasoning`, `alternatives`, `limitations`. CDS assigns item IDs, preserves accepted prose as `text`, and attaches provenance and unverified labels. The JSON schema and server validators remain authoritative; prompt count preferences are deliberately smaller than permitted maxima.

Chat JSON has `answer` and `cannot_answer`. Use `cannot_answer: true` with an empty `answer` only when no supported answer is available. Mixed questions should receive supported portions and explicit unknown portions. There is no whole-answer refusal merely because one part is unknown.

The guidance is independent of model weights or a model-specific prompt dialect. This does **not** add arbitrary provider support or make every model reliable. The existing runtime still requires an installed local Ollama-compatible GGUF completion model with JSON-schema output and normal completion metadata; cloud fallback and new providers are not introduced. Schema adherence, instruction following, latency and factual quality must be checked for each model/runtime combination. [Pre-release comparisons found semantic failures](evaluation.md#pre-release-findings-and-unresolved-reliability); factual reliability remains unresolved.

Chat still reserves 900 output tokens; briefs reserve 1800. System instructions, schema, references and conversation share the input budget. Whole-source and mandatory-scope guards remain in force, and length stops reject partial output. See [local-ai.md](../local-ai.md) for setup, API and operational limits.

## Sources and boundaries

These rules combine the CDS data contract with general forensic limitations:

- [NIST SP 800-86](https://csrc.nist.gov/pubs/sp/800/86/final): forensic process background; not certification of CDS.
- [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final): secure hash algorithms; a recorded digest alone is not a verified comparison or safety assessment.
- [Sleuth Kit fls](https://www.sleuthkit.org/sleuthkit/man/fls.html): listing filesystem entries, including deleted entries; a listing is not proof of recovered content.
- [Microsoft RegQueryInfoKey](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regqueryinfokeyw): key last-write information, not an independent per-value change timestamp.
- [Microsoft file times](https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times): timestamp kinds and filesystem-dependent behavior.

Examples intentionally simplify inputs. They teach interpretation, not parser correctness, legal conclusions, chain-of-custody certification or universal filesystem behavior. No user evidence, credentials or machine-specific paths belong in this pack.
