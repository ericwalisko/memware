# Recall election report

Source: `eval/recall_election/results/2026-09-10/results.jsonl`

384 cells, 382 valid, 2 invalid (excluded), 0 transcript/stub-log mismatches. Variants: baseline, control, cost-and-default, grep-contrast, memory-persona, must-gate, question-forms, terse-triggers. Models: opus, sonnet.

Invalid cells by reason:

- 2 x `rc 1: ed":{"depth_limit":0,"concurrency_limit":0,"budget":0}`

## Per variant and model

TPR = recall elected on positives, FPR = recall elected on negatives, both with Wilson 95% intervals. Balanced accuracy = (TPR + (1 - FPR)) / 2. Phrasings = mean queries per recall call. First = share of positives whose first tool call was recall. Mismatch = cells where the transcript and the stub's call log disagree (should be 0).

| variant | model | n | pos/neg | TPR | FPR | bal. acc | phrasings | first | mismatch |
|---|---|---|---|---|---|---|---|---|---|
| baseline | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.42 [0.19, 0.68] | 0.79 | 4.8 (n=17) | 0.83 | 0 |
| baseline | sonnet | 24 | 12/12 | 0.83 [0.55, 0.95] | 0.08 [0.01, 0.35] | 0.88 | 4.4 (n=11) | 0.83 | 0 |
| control | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.17 [0.05, 0.45] | 0.92 | 4.9 (n=14) | 0.83 | 0 |
| control | sonnet | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.00 [0.00, 0.24] | 1.00 | 4.6 (n=12) | 0.75 | 0 |
| cost-and-default | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.75 [0.47, 0.91] | 0.62 | 4.7 (n=21) | 0.92 | 0 |
| cost-and-default | sonnet | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.17 [0.05, 0.45] | 0.92 | 3.7 (n=14) | 0.83 | 0 |
| grep-contrast | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.00 [0.00, 0.24] | 1.00 | 4.6 (n=12) | 0.83 | 0 |
| grep-contrast | sonnet | 23 | 12/11 | 1.00 [0.76, 1.00] | 0.09 [0.02, 0.38] | 0.95 | 4.5 (n=13) | 0.75 | 0 |
| memory-persona | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.17 [0.05, 0.45] | 0.92 | 4.8 (n=14) | 0.83 | 0 |
| memory-persona | sonnet | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.00 [0.00, 0.24] | 1.00 | 4.1 (n=12) | 0.83 | 0 |
| must-gate | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.17 [0.05, 0.45] | 0.92 | 4.9 (n=14) | 0.83 | 0 |
| must-gate | sonnet | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.08 [0.01, 0.35] | 0.96 | 4.9 (n=13) | 0.92 | 0 |
| question-forms | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.25 [0.09, 0.53] | 0.88 | 4.7 (n=15) | 0.83 | 0 |
| question-forms | sonnet | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.08 [0.01, 0.35] | 0.96 | 4.3 (n=13) | 0.92 | 0 |
| terse-triggers | opus | 24 | 12/12 | 1.00 [0.76, 1.00] | 0.00 [0.00, 0.24] | 1.00 | 4.8 (n=12) | 0.92 | 0 |
| terse-triggers | sonnet | 23 | 12/11 | 1.00 [0.76, 1.00] | 0.09 [0.02, 0.38] | 0.95 | 3.4 (n=13) | 0.75 | 0 |

## Election rate per class

Share of cells in each scenario class where recall was called (positives should be high, negatives low; the class says which).

| variant | model | cross_repo | earlier_session | edit_task | explain_file | find_code | not_in_tree | past_decision | rationale | refactor | rejected_alternative | run_tests | style_question |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | opus | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.50 (1/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| baseline | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| control | opus | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| control | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| cost-and-default | opus | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 0.50 (1/2) |
| cost-and-default | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.50 (1/2) | 0.50 (1/2) |
| grep-contrast | opus | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| grep-contrast | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/1) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| memory-persona | opus | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| memory-persona | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| must-gate | opus | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 0.00 (0/2) | 0.50 (1/2) |
| must-gate | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| question-forms | opus | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 0.50 (1/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.50 (1/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| question-forms | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |
| terse-triggers | opus | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) |
| terse-triggers | sonnet | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/2) | 0.00 (0/2) | 0.00 (0/2) | 1.00 (2/2) | 1.00 (2/2) | 1.00 (2/2) | 0.00 (0/1) | 1.00 (2/2) | 0.50 (1/2) | 0.00 (0/2) |

## Per scenario across variants

Election rate pooled over variants, models and repeats. 13 scenario(s) show no signal: every variant elects (or never elects) them, so they cannot separate variants; consider replacing them.

| scenario | class | expect | n | election rate | per variant | flag |
|---|---|---|---|---|---|---|
| cross_repo_1 | cross_repo | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| cross_repo_2 | cross_repo | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| earlier_session_1 | earlier_session | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| earlier_session_2 | earlier_session | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| edit_task_1 | edit_task | no recall | 16 | 0.12 [0.03, 0.36] | baseline=0.50 control=0.00 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 |  |
| edit_task_2 | edit_task | no recall | 15 | 0.27 [0.11, 0.52] | baseline=0.50 control=0.50 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.50 terse-triggers=0.00 |  |
| explain_file_1 | explain_file | no recall | 16 | 0.12 [0.03, 0.36] | baseline=0.00 control=0.00 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.50 terse-triggers=0.00 |  |
| explain_file_2 | explain_file | no recall | 16 | 0.06 [0.01, 0.28] | baseline=0.00 control=0.00 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 |  |
| find_code_1 | find_code | no recall | 16 | 0.12 [0.03, 0.36] | baseline=0.50 control=0.00 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 |  |
| find_code_2 | find_code | no recall | 16 | 0.00 [0.00, 0.19] | baseline=0.00 control=0.00 cost-and-default=0.00 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 | no signal (all 0) |
| not_in_tree_1 | not_in_tree | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| not_in_tree_2 | not_in_tree | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| past_decision_1 | past_decision | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| past_decision_2 | past_decision | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| rationale_1 | rationale | recall | 16 | 0.94 [0.72, 0.99] | baseline=0.50 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 |  |
| rationale_2 | rationale | recall | 16 | 0.94 [0.72, 0.99] | baseline=0.50 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 |  |
| refactor_1 | refactor | no recall | 16 | 0.06 [0.01, 0.28] | baseline=0.00 control=0.00 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 |  |
| refactor_2 | refactor | no recall | 15 | 0.40 [0.20, 0.64] | baseline=0.50 control=0.50 cost-and-default=0.50 grep-contrast=0.00 memory-persona=0.50 must-gate=0.50 question-forms=0.50 terse-triggers=0.00 |  |
| rejected_alternative_1 | rejected_alternative | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| rejected_alternative_2 | rejected_alternative | recall | 16 | 1.00 [0.81, 1.00] | baseline=1.00 control=1.00 cost-and-default=1.00 grep-contrast=1.00 memory-persona=1.00 must-gate=1.00 question-forms=1.00 terse-triggers=1.00 | no signal (all 1) |
| run_tests_1 | run_tests | no recall | 16 | 0.56 [0.33, 0.77] | baseline=1.00 control=0.00 cost-and-default=1.00 grep-contrast=0.50 memory-persona=0.50 must-gate=0.50 question-forms=0.50 terse-triggers=0.50 |  |
| run_tests_2 | run_tests | no recall | 16 | 0.00 [0.00, 0.19] | baseline=0.00 control=0.00 cost-and-default=0.00 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 | no signal (all 0) |
| style_question_1 | style_question | no recall | 16 | 0.19 [0.07, 0.43] | baseline=0.00 control=0.00 cost-and-default=1.00 grep-contrast=0.00 memory-persona=0.00 must-gate=0.50 question-forms=0.00 terse-triggers=0.00 |  |
| style_question_2 | style_question | no recall | 16 | 0.00 [0.00, 0.19] | baseline=0.00 control=0.00 cost-and-default=0.00 grep-contrast=0.00 memory-persona=0.00 must-gate=0.00 question-forms=0.00 terse-triggers=0.00 | no signal (all 0) |
