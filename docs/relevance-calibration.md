# Calibrating what the prompt hook injects

The prompt hook and Hermes `prefetch` inject beliefs nobody asked for, so every injected line that
is not relevant costs tokens on every prompt and teaches the model to skim the block. This page
records how the default subject rule and the recommended relevance-filter threshold were chosen.

**These numbers come from one user's ledger.** They are a measurement of one store and one way of
working, not a guarantee for yours. The last section says how to measure your own.

## Method

- **Prompts:** 198 real prompts from the relevance filter's shadow log
  (`<home>/relevance-log.jsonl`).
- **Pairs:** the 1,074 prompt-belief pairs the default hook injected for them, under the subject
  test before this rule (one subject word shared with the prompt was enough).
- **Labels:** five blind Opus passes labeled each pair relevant or noise. The user then
  adjudicated the 20 pairs the passes disagreed on.
- **Rules:** each candidate rule was scored on those labels as *relevant kept* (the share of
  relevant pairs it still injects) and *noise cut* (the share of noise pairs it drops).

What the labels showed:

- **Most of the injected block was noise.** Among the 1,029 pairs that also carry a relevance
  filter (Jev) score, 113 were relevant and 916 were noise.
- **88% of injected pairs matched the prompt on exactly one subject word.** That one word is where
  the noise gets in: issue #47's `feedback to file` injecting `config file location`.
- **Rarity across belief subjects does not separate noise.** In a varied ledger even a common word
  appears in few subjects. This was the fix issue #47 suggested.
- **Rarity across the user's conversations does.** The measure is the share of indexed passages
  that hold the word.

## The default subject rule

| rule | relevant kept | noise cut |
|---|---|---|
| one shared subject word (before; these are the pairs it injected) | 100% | 0% |
| two shared subject words, or one in at most 10% of indexed passages (now the default) | 100% | 30.3% |

A belief is injected unsolicited only if its subject and the prompt share either:

- **two or more distinct words**, or
- **one word that at most 10% of the indexed passages hold.** The share is read from the passage
  index (an `fts5vocab` view over `passage_fts`). A word the index does not hold, as written,
  counts as rare. Ids, versions and paths (`t_b6b2f934`, `memware-0.7.0`, `etc/app/conf.d`)
  split into pieces there, so they are never found whole, and they are specific.

The index stores porter stems, and a word is looked up as the subject writes it. So a word the
stemmer changes (`service` is indexed as `servic`) is not found, and it counts as rare however
often it is used. That is how the rule was measured, and it is deliberate: see
[why the lookup is not stem-aware](#why-the-lookup-is-not-stem-aware).

**Small stores.** Under 1,000 indexed passages the share is not read, and one shared word is
enough, as in 0.9.0. At 1,000 passages the 10% line sits 100 passages up, and a word's share is
known to about ±2 points (binomial, 95%). At 100 passages it is ±6, so a word in 5% of passages
cannot be told from one in 15%. Passages come in conversations, which makes the real error
larger, and a young store is a few conversations whose own subject would read as a common word.

The rule applies wherever memware injects unsolicited: the prompt hook (`memware context`),
Hermes `prefetch`, and the beliefs context of `memware eval`. Recall on demand (`recall`,
`memware beliefs`) is unchanged, and so is the session-start digest, which matches the project's
own name. `memware.index.subject_passes(store, prompt, subject)` answers
for one pair. It writes nothing and runs on a read-only connection, so a labeled set can be
re-scored against it.

### Why the lookup is not stem-aware

The shipped rule was re-scored on the adjudicated labels (1,068 pairs: 117 relevant, 951 noise),
beside a stem-aware variant that counts `MATCH '"term"'` over `passage_fts`:

| lookup | relevant kept | noise cut |
|---|---|---|
| as written, share <= 10% (the default) | 100% (0 dropped) | 31.1% |
| stem-aware, share <= 10% | 91.5% (10 dropped) | 32.1% |
| stem-aware, share <= 5% | 65.8% (40 dropped) | 69.9% |
| stem-aware, share <= 30% | 100% | 0% |

Relevant facts often match on a project name (`memware`), which is common in the user's
conversations but stored under a stem, so the as-written lookup reads it as rare and keeps the
fact. The words it does catch are plain words the index stores unchanged, such as `file`,
`report`, `path` and `board`: the kind of word that collided in #47.
`test_a_word_is_looked_up_as_written_so_a_stemmed_word_reads_as_rare` in
`tests/test_subject_rule.py` pins the as-written lookup.

## The relevance filter on top

The [relevance filter](../README.md#optional-a-relevance-filter-for-prompt-time-injection) is off
by default. It was scored on the same labels:

| setting | relevant kept | noise cut |
|---|---|---|
| Jev >= 0.2 | 100% | 56.8% |
| word rule + Jev >= 0.2 | 100% | 68.9% |
| word rule + Jev >= 0.3 | 99.1% | 81.3% |
| word rule + Jev >= 0.35 | 98.2% | 85.6% |

**Recommended threshold: 0.2** (`memware config relevance.threshold 0.2` with `relevance.mode
filter`). On these labels it kept every relevant pair, and with the word rule it cut 68.9% of the
noise. Higher thresholds cut more and start losing relevant beliefs. The shipped default stays
0.5; this page does not change it.

## Measuring your own

Shadow mode (`memware config relevance.mode shadow`) logs each prompt and candidate with a stable
`pair_id` to `<home>/relevance-log.jsonl`. Label a few dozen pairs as relevant or noise, and keep
the labels in `<home>/labels/`. The log and the labels hold the text of your prompts: delete them
when you are done. `memware nuke` removes both, with everything else.
