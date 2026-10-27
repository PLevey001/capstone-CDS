# Guidance evaluation and later training

Version 0.2. The compact runtime subset is integrated; these full documents and examples are not model inputs. No model weights have been trained.

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
- Optional meaningful hypotheses (matching content with uncertain transfer history) and sparse Registry counts that do not measure user activity or duration.
- A misleading prior brief that chat must not turn into evidence.
- Long inputs, omitted detail, stopped generation and a question with several parts.

The example set in this folder is development material. Do not present answers on the same examples used for prompt design as held-out accuracy. Reserve new fixtures, paraphrases and combinations for later evaluation.

## Practical adoption and edit workflow

1. Read [shared rules](shared-rules.md), the mode guidelines and [examples.json](examples.json). The examples are independently authored specifications, not generated successes. Validate preferred outputs with the actual application validators using `tests/test_guidance_examples.py`; semantically rejected wording can still pass those mechanical checks.
2. For a compatible installed model, keep the same trusted SYSTEM composition and supplied JSON schema. Model-independent guidance does not add a provider adapter. Check the existing local completion/JSON contract in [local-ai.md](../local-ai.md); do not change a model or download weights as part of a guidance edit.
3. Edit only the relevant compact file(s) under `cds/prompts` and the reader-facing specification. Shared changes belong in `common.txt`; mode rules belong in their fixed files. Never concatenate this documentation or rejected examples into requests. Count the exact composed instructions with schema, history and references; retain the 900/1800 output reservations and whole-source guards.
4. Bump affected prompt versions and the guidance pack revision in `cds/prompt_guidance.py`; keep documentation and tests in sync. Restart the process. Check returned version/checksum metadata against the actual outgoing SYSTEM text, including selected-assessment mode. Previously saved documents retain their original bytes and metadata.
5. Run focused prompt, example, evaluation, case/chat and saved-document tests, then `.venv/bin/python -m pytest -q -ra --require-image-tests` and `git diff --check`. Tests use scripted replies; passing does not measure model semantic quality.
6. First inspect a no-model evaluation manifest. For a separate explicitly authorized real-model comparison, use the same model digest, runtime, settings and synthetic inputs before and after. Preserve every attempt, including errors, refusals and length stops. Review output meaning against the pre-authored rubric; do not cherry-pick retries.
7. Check saved JSON/download byte stability, selected-brief ownership/staleness, conversation guards and context omissions. Human semantic review and live API acceptance remain separate from unit tests.

## Synthetic runner

`scripts/evaluate_local_ai.py` exercises brief generation (`assessment`), `case_chat`, `concept_chat` and `prior_assessment_chat`. Twelve development scenarios cover the original six cases plus sparse Registry counts, mixed known/unknown case questions, a false hash premise, Registry concept explanation, a mixed concept/case question and an intentionally wrong authored prior assessment. Prior AI is fixture data, never a model-measured success or evidence. Concept operations use the production glossary; case operations use neutral synthetic fact labels. Names and rubrics never go to the model.

Default invocation makes no model calls:

```bash
.venv/bin/python scripts/evaluate_local_ai.py --output /tmp/cds-guidance-manifest.json
```

Filters are repeatable; only supported scenario/operation pairs run. An unknown or empty selection is an error. For example, to inspect the sparse-case manifest:

```bash
.venv/bin/python scripts/evaluate_local_ai.py --scenario sparse_registry --operation case_chat --output /tmp/cds-registry-manifest.json
```

Adding `--real-model` explicitly enables calls to the configured local model. Use a new report path outside the repository; reports are created exclusively and never overwritten. No evidence or case database is opened. Every operation records its full-system version/checksum and guidance revision before attempting generation, even if context, transport, completion or validation rejects it. Parsed raw model replies (including rejected content), errors, runtime discovery information and duration are retained when available. A transport failure may yield no raw reply.

Record per-operation `manual_status` (pass/fail/inconclusive) and `manual_findings` separately from `mechanical_status`; scenario-level fields allow an overall review note. An accepted unverified response is not a semantic pass, and an unnecessary refusal is not automatically safe success. These fixtures are development cases used for prompt work, **not held-out accuracy**. Reserve new cases and paraphrases for later evaluation.

## If fine-tuning is considered later

Start only after prompt-based behavior and a held-out evaluation are established. Build reviewed input/output pairs from synthetic or explicitly authorized, de-identified material. Never train on private evidence or credentials by default. Track provenance and permission, split by underlying case rather than individual paraphrase, and keep evaluation cases out of training.

Transform approved examples into the selected trainer's format in a separate, reviewed step. Do not train on rejected wording as if it were a target answer. Validate both preferred outputs and realistic refusals. Track model/license compatibility, training configuration, base-model digest, dataset revision and resulting model identifier. Keep a rollback model and compare on factuality, refusals, readability, latency and memory. Fine-tuning does not fix context exhaustion or guarantee evidence-grounded reasoning.

## Remaining limits

The runtime reserves 1800 output tokens for briefs, uses a shorter file-based prompt, and retains 900 for chat. Earlier API acceptance does not establish the semantic quality of this guidance revision. Length stops still reject incomplete replies; larger output is not a guarantee of completion or factual accuracy. Further quality evaluation remains required.


## Pre-release findings and unresolved reliability

Supervisor comparisons using the same local Qwen2.5:7b digest, Ollama 0.40.6, temperature 0, seed 0 and context 32768 found mixed behavior. The initial shared guidance corrected an earlier unsupported Registry-count inference in selected-brief chat. Another case answer gave the parsed count but omitted the requested activity limitation. A brief invented generic speculation about additional sources and failed a numeric check on a derived count. Two experimental compact candidates were rejected for semantic regressions, including invented interaction/corruption, endorsement of unsupported limited activity and unnecessary concept refusal.

The current compact composition preserves the initial concept/prior correction instructions, tells the model to answer both parts of count-and-use questions and avoid generic gap speculation and derived counts, and keeps the composed brief below 1500 UTF-8 bytes. The initial larger composition caused a real whole-source context regression, including at 32768 for a combined case and at 8192 for selected-assessment discussion. Shorter prompts address that integration constraint without changing validators, output reservations or omission guards. These edits were then exercised through the normal launcher and real local-model API; **factual reliability remains unresolved**. The combined brief fit all source identities and mandatory scope (110 of 145 computed facts sent, 35 details explicitly omitted), saved and downloaded with the composed prompt checksum, and preserved two older exports byte-for-byte. However, it contained an unfinished `"The "` observation, repetitive prose and weakly supported inferences despite mechanical acceptance. Case chat unnecessarily declined a question about record counts and activity; concept chat distinguished a hash match from software safety. Chat about that large saved assessment was visibly refused because its assessment text plus mandatory source scope exceeded the context budget. Small selected-assessment and follow-up cases remain covered by tests. No guard was relaxed and no output was silently repaired. Passing contract, packaging or budget tests is not evidence of semantic improvement.

Brief validation rejects the whole assessment on structural/reference/coarse-number failures. Chat can withhold individual invalid answer items and retain others; if none remain, it rejects. Neither path verifies meaning. A plausible, cited but unsupported statement can survive mechanical validation.
