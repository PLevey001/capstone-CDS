# Optional local AI

Case brief phrases computed saved-result facts. Learn with AI answers questions and follow-ups in two explicit modes:

- **Explain concepts** sends local glossary text and the conversation, with no case facts or case name. It gives general education, not observations about a case. Do not paste evidence into questions if you want this mode to remain free of case information.
- **Ask about this case** builds fresh facts from the selected case's saved database results. Source references open the exact saved run and, when available, artifact. Client-supplied facts are rejected. Conversation and earlier answers are untrusted context, never evidence.

Every generated statement is **unverified**. Valid references and matching number tokens do not establish meaning, intent, identity, guilt, or accuracy. For example, a statement about deliberate destruction can pass mechanical checks while citing an inventory count. No semantic verification is claimed. Read the reference text, saved run and coverage yourself.

Concept answers show the cited reviewed local text beside the generated explanation by default, using the reference snapshot returned with that answer. SHA-256 notes distinguish integrity relative to a reference from trust in that reference or software safety. Registry notes distinguish a key's last-write time from an individual value's change time. The model can still contradict these notes; visible references and warnings do not establish answer quality. Unsupported concept questions receive a reference-limitation message, without assuming the question was case-specific.

## Setup

Install Ollama using its [official installation instructions](https://ollama.com/download). Record `ollama --version` when validating a deployment. CDS uses Ollama's [chat API](https://docs.ollama.com/api/chat) with JSON schema output and requires `done: true`, `done_reason: "stop"`, and the configured model in a complete reply. It does not retry arbitrary HTTP 400 responses or fall back to a cloud service.

Run Ollama bound only to loopback, with cloud features disabled:

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 ollama serve
```

In a separate terminal, download the default model once (installation requires internet access):

```bash
OLLAMA_HOST=127.0.0.1:11434 ollama pull llama3.2:3b
```

If Ollama already runs as a service, set these environment variables for that service and restart it instead of launching a second instance. See [Ollama configuration and disabling cloud features](https://docs.ollama.com/faq). CDS cannot enforce how a separately administered Ollama service is configured.

Start CDS normally. The AI panels show model availability and offer **Check again**. No model is necessary for the fact sheet, local glossary, inventory, timeline, records, extraction, or export. CPU generation may take a minute or more; latency and quality depend on hardware, model and runtime version. Real-model quality/latency must be evaluated separately from scripted tests.

| CDS environment setting | Default | Meaning |
| --- | --- | --- |
| `CDS_AI_URL` | `http://127.0.0.1:11434` | Plain loopback HTTP only; no credentials, paths or queries. `localhost` is normalized to `127.0.0.1` before connecting. IPv6 `http://[::1]:11434` is also accepted. |
| `CDS_AI_MODEL` | `llama3.2:3b` | An installed local GGUF model advertising completion support through `/api/show`. Cloud names or remote metadata are refused. |
| `CDS_AI_TIMEOUT` | `180` | Overall discovery plus generation deadline in seconds, including response reads. |
| `CDS_AI_CONTEXT` | `8192` | Context tokens, configurable from 4096 through 32768. Smaller settings can reject glossary/history requests; larger settings use more memory. |

Use a model/runtime supporting the documented schema chat contract. There is no claim of compatibility with every Ollama version or custom model template. Model discovery uses [tags](https://docs.ollama.com/api/tags) and [show](https://docs.ollama.com/api-reference/show-model-details). No runtime is required by automated tests. Do not expose either service to a network, configure reverse proxies to it, or substitute a remotely forwarded loopback endpoint. The client makes direct connections without system proxies and rejects redirects.

## Lifetime, boundaries and limits

Case questions are rejected before model generation if the bounded context would omit any entire evidence source. Review the full computed fact sheet, clear history, or increase `CDS_AI_CONTEXT` within the supported range. This prevents a partial source set being used for a whole-case question; it does not verify generated meaning or guarantee that all details fit. Concept questions remain available. Case briefs may still summarize a visibly disclosed subset.

Chat lives in browser memory only and clears on reload, tab exit, case/mode/name changes, or observed source revisions. Clear conversation also discards pending browser replies. The server may still be finishing an interrupted request; brief and chat share one generation slot and another request receives 409 until that work ends. No raw conversation or generated chat answer is written to the CDS database or audit. Brief generation records only model/count/digest metadata. Ollama has its own process memory and service logging configuration, outside CDS's persistence controls.

The model receives no raw evidence files and has no tools, web access, command execution, or evidence mutation capability. CDS does not make external model calls or fetch educational references during generation. Glossary links are static local content and open only when clicked. Evidence names, record summaries and conversation text can contain hostile instructions: fixed system instructions and reference validation reduce exposure but do not make prompt injection impossible.

Input is bounded in UTF-8 bytes, conservatively allowing one byte per input token after reserving 900 output tokens and 1024 tokens for chat-template overhead. The input budget includes system instructions, glossary/facts, history, question and schema. This is a conservative budgeting rule, not an exact tokenizer for every custom model. The full computed fact sheet remains accessible in Case brief without AI. Model selection preserves case and general scope, then source identities together with their saved coverage scopes, parser details and warnings, then other details in stable order. A source's identity and coverage notes are indivisible: if they cannot fit, that source and its aggregates are omitted from model input. Each selection pass stops at the first item that exceeds the remaining budget or 60 facts. Returned context metadata/UI state counts omitted facts and sources, including sources with omitted details. Selection can change with the length of a follow-up; citations use the references returned with that answer. The brief audit records the actual sent and omitted fact counts alongside the full-sheet digest.

Chat requests are capped at 16 KiB before parsing (including chunked requests), eight prior history messages in complete user/assistant pairs, and 2000 UTF-8 bytes per message. These are simultaneous upper bounds, not a promise that four full-length prior turns plus a question fit. JSON field names, punctuation and escaping also count toward the body cap; model context additionally includes the fixed instructions and references. A request can satisfy each field limit yet exceed the body or context limit and receive 422. Shorten the question or clear the conversation when needed. Clients cannot submit system messages or extra fact fields. History is rejected when over budget, never silently trimmed. Model HTTP requests/replies are capped at 128/256 KiB; generated JSON content at 16 KiB; displayed chat prose at 2000 UTF-8 bytes. Statements over 400 characters are withheld whole, as are malformed statements, any unknown/mixed references, numeric mismatches and overflow. Omission counts include every withheld list item. These checks can reject valid wording and can accept false wording.

Case follow-ups require the previous fact digest. Canonical hashing includes every fact's text and stable provenance, including case/source/run/artifact identifiers and current-job identity/status. Responses return the digest and provenance snapshot. A case change during generation or a stale/cross-case digest returns 409. The UI invalidates brief references during refresh/failure and rejects late results for cleared/replaced chat sessions.

An unmeasured inventory is reported as unavailable, not zero. Saved results may predate the current job. Source-specific coverage scopes, parser details and warnings remain visible even for completed analyses; missing data does not establish absence. Browser host totals exclude truncated, missing and unclassifiable URLs and disclose those counts. File time extrema include representable pre-1970 values and exclude unavailable zero or out-of-range values before aggregation. Itemized deleted entries and top host/event lists retain their existing caps. Full-sheet aggregation still visits all sources and scans saved artifact/record rows (including a per-source host tally); memory and database work grow with case size. This change bounds model context, request/reply sizes and displayed histories, not total forensic workspace size or full-sheet query latency.

API errors: missing case 404; invalid/blank/oversized request or context 422; unavailable model 503; concurrent generation/stale facts 409; timeout 504; malformed/incomplete model reply 502. The existing `X-CDS-Request: local-ui` guard applies. Generation errors release the shared slot. An unsupported question can return a fixed uncertainty message with no generated assertions.

## Reviewed local lessons and verification

The glossary explains SHA-256 ([NIST FIPS 180-4](https://csrc.nist.gov/pubs/fips/180-4/upd1/final)), deleted entries ([TSK fls](https://www.sleuthkit.org/sleuthkit/man/fls.html)), timestamp kinds/UTC ([Microsoft file times](https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times)), browser history attribution ([Chrome history](https://support.google.com/chrome/answer/95589?hl=en)), Registry key/value time distinctions ([RegQueryInfoKey](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regqueryinfokeyw)) and event IDs ([Microsoft event schema](https://learn.microsoft.com/en-us/windows/win32/wes/eventschema-eventid-systempropertiestype-element)). Coverage and saved-run explanations describe CDS behavior; [NIST SP 800-86](https://csrc.nist.gov/pubs/sp/800/86/final) supplies general forensic process background, not certification of CDS.

Run the backend, frontend formatting/build, and Playwright checks listed in README. Tests use synthetic cases and a scripted local HTTP stub with realistic completion metadata. Green tests verify the exercised software behavior, not the truth of model statements. A real Ollama smoke test, runtime version and model-quality/latency review remain separate deployment checks.
