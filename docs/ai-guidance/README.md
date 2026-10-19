# Local AI guidance pack

Version: 0.1 — draft for human review.

Goal: produce a case summary a person can understand, with a JSON structure a program can preserve. The JSON is the container; its prose should not read like a database dump.

## Status: documentation, not runtime configuration

CDS does **not** currently read this folder at startup or send these files to Ollama. Active prompts remain in `cds/case_brief.py` and `cds/learning.py`. Creating or editing these documents does not train model weights, change generation limits, or fix incomplete replies. Nothing here authorizes weakening validation or hiding missing evidence.

This pack is a proposed behavior specification and example set. Human review comes first; prompt integration and an evaluation against the current baseline are separate changes. Actual fine-tuning would require a curated dataset, held-out evaluation, a training process and a separately versioned model. That work has not been performed.

## Files and intended use

| File | Purpose |
| --- | --- |
| [brief-guidelines.md](brief-guidelines.md) | What the case brief should say, how readable it should be, and where inference belongs. |
| [chat-guidelines.md](chat-guidelines.md) | How to explain evidence, concepts and a selected prior brief without treating AI text as evidence. |
| [examples.json](examples.json) | Synthetic inputs, preferred outputs, and specifically rejected wording. These are authored examples, not real model results. |
| [evaluation.md](evaluation.md) | Review rubric, regression scenarios and a safe path to future integration/training. |

Use the brief rules for assessment generation and the chat rules for chat. Do not blindly concatenate the entire pack into every request: schema, reference text, history and instructions share a bounded context. Rejected examples and reviewer notes should not accidentally become answer templates.

## Output contracts

The examples describe the **model response**, not a complete saved case document. Brief model responses use `statement` and `facts`; CDS assigns saved item IDs and converts accepted statements to `text`. The application, not the model, attaches case identity, version, snapshots, digests, generation metadata and verification labels.

Chat model responses use `answer` plus `cannot_answer`. A refusal has `cannot_answer: true` and an empty `answer`; a supported answer has `cannot_answer: false`. Do not mix the two.

Current limits and routes are documented in [local-ai.md](../local-ai.md). The 900-token output limit and conservative input reservation are unchanged by this documentation work. Examples must not be padded to fill allowed item counts. A transport-truncated answer must still be rejected rather than repaired into an apparently complete assessment.

## Sources and boundaries

These rules combine the CDS data contract with general forensic limitations:

- [NIST SP 800-86](https://csrc.nist.gov/pubs/sp/800/86/final): forensic process background; not certification of CDS.
- [NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final): secure hash algorithms; a recorded digest alone is not a verified comparison or safety assessment.
- [Sleuth Kit fls](https://www.sleuthkit.org/sleuthkit/man/fls.html): listing filesystem entries, including deleted entries; a listing is not proof of recovered content.
- [Microsoft RegQueryInfoKey](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regqueryinfokeyw): key last-write information, not an independent per-value change timestamp.
- [Microsoft file times](https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times): timestamp kinds and filesystem-dependent behavior.

Examples intentionally simplify inputs. They teach interpretation, not parser correctness, legal conclusions, chain-of-custody certification or universal filesystem behavior. No user evidence, credentials or machine-specific paths belong in this pack.
