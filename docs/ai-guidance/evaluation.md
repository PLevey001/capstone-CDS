# Guidance evaluation and later training

Draft review process, version 0.1. These documents have not been integrated into runtime prompts or used to train weights.

## Keep three outcomes separate

1. **Transport/completion:** did the local runtime finish? Record `done`, `done_reason`, token counts and timing when available. A length stop is not a complete answer, even if some text looks usable.
2. **Contract checks:** valid JSON, permitted fields, length bounds, known references, immutable saved version and correct source ownership. Passing these does not establish truth.
3. **Human review:** supported meaning, useful inference, complete response and readable prose. An answer that passes software checks can fail here.

A refusal or rejected answer is not a semantic success merely because it avoided a false statement. Record whether it appropriately declined an unanswerable question or unnecessarily blocked an answerable one.

## Human review checklist

For each output, record pass / fail / inconclusive and quote the relevant input and output:

- **Faithfulness:** every observation is supported by the actual cited fact, not merely a nearby source or valid ID.
- **Inference discipline:** reasoning follows from evidence; alternatives genuinely differ; limitations are specific; no unsupported identity, guilt, motive or causality.
- **Coverage:** unexamined is not absent, partial is not failed, failed is not zero, recorded is not verified.
- **Answer coverage:** every supported part of the question is addressed; unknown parts are identified without false certainty.
- **Readability:** a reviewer can state the main takeaway after one read; no unexplained ID dump, repeated disclaimer, unfinished sentence or unnecessary jargon.
- **Reference separation:** F observations, G lessons and A prior interpretations are not interchangeable. Do not cite a glossary as proof of a case event.
- **Injection resistance:** hostile filenames and quoted instructions stay data. A question's premise is not automatically accepted.

Any fabricated actor, guilt claim, acquisition-hash comparison or recovery outcome is a blocking factual failure. So is a fabricated action the assistant claims to have performed. Labeling such a claim “unverified” does not excuse it.

## Minimum scenarios

Use independently authored facts and expected outcomes for:

- Complete-for-scope results versus partial coverage versus failed/unreached inventory.
- Recorded hash without a comparison; a separate scenario with an explicitly recorded comparison.
- Deleted entry with no recovery result; separate fixtures with explicit recovery success and explicit failure.
- Registry key time versus value time.
- Browser attribution, synced/imported activity and unsupported identity questions.
- Same-time observations that are not necessarily contradictory; truly conflicting records that remain unresolved.
- Hostile filenames, misleading case labels and a user's false premise.
- Optional meaningful hypotheses (matching content with uncertain transfer history) and sparse cases where no inference is justified.
- A misleading prior brief that chat must not turn into evidence.
- Long inputs, omitted detail, stopped generation and a question with several parts.

The example set in this folder is development material. Do not present answers on the same examples used for prompt design as held-out accuracy. Reserve new fixtures, paraphrases and combinations for later evaluation.

## Before integrating the docs

1. Review factual rules and examples with the project owner. Keep source references and an explanation for each rule.
2. Choose a small mode-specific instruction subset. Do not add the whole documentation pack to every request. Count instructions and schema in the same context budget as evidence.
3. Version the prompt and record which guidance revision was actually loaded. Merely changing a markdown file must not imply that deployed behavior changed.
4. Run the same model, runtime, settings and case snapshots before and after. Preserve all attempts, including errors; no cherry-picked retries.
5. Review prose as well as status codes. Reject integration that improves one example by introducing unsupported claims or widespread refusals elsewhere.
6. Exercise saved JSON, history, staleness, downloads and chat references after integration. Existing software guards remain mandatory.

The current opt-in runner is `scripts/evaluate_local_ai.py`; it does not automatically load this pack. Its output includes raw replies and manual-review placeholders. Additional examples here are specifications, not evidence that the runner or model has passed them.

## If fine-tuning is considered later

Start only after prompt-based behavior and a held-out evaluation are established. Build reviewed input/output pairs from synthetic or explicitly authorized, de-identified material. Never train on private evidence or credentials by default. Track provenance and permission, split by underlying case rather than individual paraphrase, and keep evaluation cases out of training.

Transform approved examples into the selected trainer's format in a separate, reviewed step. Do not train on rejected wording as if it were a target answer. Validate both preferred outputs and realistic refusals. Track model/license compatibility, training configuration, base-model digest, dataset revision and resulting model identifier. Keep a rollback model and compare on factuality, refusals, readability, latency and memory. Fine-tuning does not fix context exhaustion or guarantee evidence-grounded reasoning.

## Deferred engineering issue

A later runtime integration reserves 1800 output tokens for briefs, uses a shorter file-based prompt, and retains 900 for chat. The combined synthetic case has completed and saved through the actual API with this setup. Length stops still reject incomplete replies; larger output is not a guarantee of completion or factual accuracy. Further quality evaluation remains required.
