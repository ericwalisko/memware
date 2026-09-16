# Recall election report

Source: `eval/recall_election/results/2026-09-15/results.jsonl`

600 cells, 600 valid, 0 invalid (excluded), 0 transcript/stub-log mismatches, 0 recall count mismatches, 0 cells with memory paths in init. Variants: control, grep-contrast, memory-persona, synthesized, terse-triggers. Models: opus.

## Per variant and model

TPR = recall elected on positives, FPR = recall elected on negatives, both with Wilson 95% intervals. Balanced accuracy = (TPR + (1 - FPR)) / 2. Phrasings = mean queries in the first recall call, over positives that elected recall. First = share of positives whose first tool call was recall. Mismatch = cells where the transcript and the stub's call log disagree on whether recall ran; count = on how many times (both should be 0).

| variant | model | n | pos/neg | TPR | FPR | bal. acc | phrasings (pos) | first | mismatch | count |
|---|---|---|---|---|---|---|---|---|---|---|
| control | opus | 120 | 60/60 | 1.00 [0.94, 1.00] | 0.30 [0.20, 0.43] | 0.85 | 4.8 (n=60) | 0.72 | 0 | 0 |
| grep-contrast | opus | 120 | 60/60 | 1.00 [0.94, 1.00] | 0.37 [0.26, 0.49] | 0.82 | 4.6 (n=60) | 0.75 | 0 | 0 |
| memory-persona | opus | 120 | 60/60 | 1.00 [0.94, 1.00] | 0.40 [0.29, 0.53] | 0.80 | 4.7 (n=60) | 0.75 | 0 | 0 |
| synthesized | opus | 120 | 60/60 | 1.00 [0.94, 1.00] | 0.33 [0.23, 0.46] | 0.83 | 4.5 (n=60) | 0.75 | 0 | 0 |
| terse-triggers | opus | 120 | 60/60 | 1.00 [0.94, 1.00] | 0.32 [0.21, 0.44] | 0.84 | 4.3 (n=60) | 0.75 | 0 | 0 |

## Election rate per class

Share of cells in each scenario class where recall was called (positives should be high, negatives low; the class says which).

| variant | model | edit_task | explain_file | find_code | history_bait | rationale | refactor | run_tests | style_question |
|---|---|---|---|---|---|---|---|---|---|
| control | opus | 0.00 (0/10) | 0.10 (1/10) | 0.00 (0/5) | 1.00 (15/15) | 1.00 (60/60) | 0.00 (0/10) | 0.00 (0/5) | 0.40 (2/5) |
| grep-contrast | opus | 0.00 (0/10) | 0.20 (2/10) | 0.00 (0/5) | 1.00 (15/15) | 1.00 (60/60) | 0.00 (0/10) | 0.00 (0/5) | 1.00 (5/5) |
| memory-persona | opus | 0.00 (0/10) | 0.40 (4/10) | 0.00 (0/5) | 1.00 (15/15) | 1.00 (60/60) | 0.00 (0/10) | 0.00 (0/5) | 1.00 (5/5) |
| synthesized | opus | 0.00 (0/10) | 0.20 (2/10) | 0.00 (0/5) | 1.00 (15/15) | 1.00 (60/60) | 0.00 (0/10) | 0.00 (0/5) | 0.60 (3/5) |
| terse-triggers | opus | 0.00 (0/10) | 0.00 (0/10) | 0.00 (0/5) | 1.00 (15/15) | 1.00 (60/60) | 0.00 (0/10) | 0.00 (0/5) | 0.80 (4/5) |

## Per scenario across variants

Election rate pooled over variants, models and repeats. 22 scenario(s) show no signal: every variant elects (or never elects) them, so they cannot separate variants; consider replacing them.

| scenario | class | expect | n | election rate | per variant | flag |
|---|---|---|---|---|---|---|
| edit_task_1 | edit_task | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| edit_task_2 | edit_task | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| explain_file_1 | explain_file | no recall | 25 | 0.36 [0.20, 0.55] | control=0.20 grep-contrast=0.40 memory-persona=0.80 synthesized=0.40 terse-triggers=0.00 |  |
| explain_file_2 | explain_file | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| find_code_1 | find_code | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| history_bait_1 | history_bait | no recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| history_bait_2 | history_bait | no recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| history_bait_3 | history_bait | no recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_1 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_10 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_11 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_12 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_2 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_3 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_4 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_5 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_6 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_7 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_8 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_9 | rationale | recall | 25 | 1.00 [0.87, 1.00] | control=1.00 grep-contrast=1.00 memory-persona=1.00 synthesized=1.00 terse-triggers=1.00 | no signal (all 1) |
| refactor_1 | refactor | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| refactor_2 | refactor | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| run_tests_1 | run_tests | no recall | 25 | 0.00 [0.00, 0.13] | control=0.00 grep-contrast=0.00 memory-persona=0.00 synthesized=0.00 terse-triggers=0.00 | no signal (all 0) |
| style_question_1 | style_question | no recall | 25 | 0.76 [0.57, 0.89] | control=0.40 grep-contrast=1.00 memory-persona=1.00 synthesized=0.60 terse-triggers=0.80 |  |

## Paired sign test against control

Per model, each variant against `control` on matched (scenario, repeat) cells where both are valid. A cell is correct when recall was elected exactly when the scenario expects it. Only discordant pairs count; p is the exact two-sided binomial p. Positives elected are over the same matched positive pairs.

| model | variant | pairs | variant right, control wrong | control right, variant wrong | p | positives elected (variant vs control) |
|---|---|---|---|---|---|---|
| opus | grep-contrast | 120 | 1 | 5 | 0.2188 | 60/60 vs 60/60 |
| opus | memory-persona | 120 | 0 | 6 | 0.0312 | 60/60 vs 60/60 |
| opus | synthesized | 120 | 1 | 3 | 0.6250 | 60/60 vs 60/60 |
| opus | terse-triggers | 120 | 1 | 2 | 1.0000 | 60/60 vs 60/60 |

## Decision

SHIP `synthesized` only if, on each model, it has more correct discordant pairs than `control` with sign-test p < 0.05 and its positive election rate over the matched positives is not lower than `control`'s. Otherwise KEEP `control`.

- opus: 120 matched pairs; discordant 1 for synthesized, 3 for control, p = 0.6250 (not better at p < 0.05); positives elected 60/60 vs 60/60 (no drop) -> fail

VERDICT: KEEP control
