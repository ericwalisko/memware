# Mutation testing the safety modules

Three modules carry the safety properties in [security.md](security.md):

- `memware.fsperm`: file modes (MW-SEC-1).
- `memware.instruction`: the instruction filter (MW-SEC-4).
- `memware.volatile`: the staleness gate, and the injection gate that applies both filters.

The agents that wrote these modules also wrote their tests. To check what those tests actually pin,
each module was mutated and its own tests were run against every mutant. A mutant that no test
kills is a change to the code that nothing notices. Each survivor was sorted into one of these:

- **A real gap.** A promised property can change and no test fails.
- **Equivalent or unreachable.** No input tells the mutant from the original, or no caller reaches
  the difference.
- **Cosmetic.** Only explanation text changes, meaning the words `memware beliefs --explain` prints.

Every real gap got a test, phrased as the property it protects. Each test passes on main and fails
against its mutant. Nothing under `src/` changed.

Run on 2026-10-03, on Linux with Python 3.11.

## Method

**mutmut 3.8.0** ran in a scratch copy of the repository, so its config never entered
`pyproject.toml`, and it is not a dependency or a CI step. The config was:

```toml
[tool.mutmut]
paths_to_mutate = ["src/"]
do_not_mutate = ["src/memware/[!fiv]*", "src/memware/cli/*", "src/memware/ingest/*",
                 "src/memware/index.py", "src/memware/__init__.py", "src/memware/__main__.py"]
tests_dir = ["tests/test_security_permissions.py", "tests/test_security_injection.py",
             "tests/test_volatile.py", "tests/test_volatility_corpus.py",
             "tests/test_explain.py", "tests/test_injection.py"]
```

The command was `mutmut run --max-children 4`, which took about 8½ minutes for 1,206 mutants.
The test files listed are the ones that import these modules. mutmut runs a mutant only against
the tests that reach the mutated function. Mutant names, such as `x_tighten__mutmut_11`, are
specific to mutmut 3.8.0 and to this source.

mutmut 3 mutates only function bodies, never module-level constants. Nearly all of
`memware.instruction` is module-level regular expressions, so a second pass covered them. A
60-line script (not committed) deleted one alternative of one `|` group at a time, in every
pattern and in the top level of each. It also deleted each whole category. It patched the module
before collection and ran `pytest -x tests/test_security_injection.py` against each of its 315
mutants. That took about 1 minute.

Neither pass mutated `volatile`'s word lists and patterns, such as `QUALIFIERS`,
`STATUS_VALUE_WORDS` and `_TREE_STATE`. The labeled corpus in `tests/data/volatility_cases.jsonl`
remains their check. The `0o600` and `0o700` constants in `fsperm` weren't mutated either, but
`tests/test_security_permissions.py` asserts both literally.

## Results

| | Before | After |
|---|---|---|
| `fsperm` (mutmut) | 39 / 56 killed | 48 / 56 |
| `instruction` functions (mutmut) | 48 / 62 | 53 / 62 |
| `volatile` (mutmut) | 911 / 1,088 | 962 / 1,088 |
| **mutmut total** | **998 / 1,206 (82.8%)** | **1,063 / 1,206 (88.1%)** |
| `instruction` patterns (alternative deletion) | 23 / 315 (7.3%) | 313 / 315 (99.4%) |

The pattern pass found the largest gap. The attack corpus only asserted that each attack was
flagged, and most attacks fall into two or three categories. Removing any one of three whole
categories failed no test: "an order addressed to the agent", "hides an action from the user" and
"deletes a home or root directory".

The suite went from 1,316 passed and 4 skipped to 1,837 passed and 4 skipped. Most of the new
tests are parametrized cases of `test_each_documented_category_is_recognized_on_its_own`.

## Gaps closed

`tests/test_security_permissions.py` (POSIX only, like the rest of the file):

- `test_create_private_makes_a_new_file_owner_only_and_reports_it`: the file is 0600 from the
  moment it is created. The return value says whether this call created it, and the relevance
  log relies on that to tighten an older log (`create_private` 7, 10, 12).
- `test_tighten_never_changes_a_file_another_user_owns`: the ownership check (`tighten` 11).
- `test_tighten_clears_every_bit_the_mode_does_not_grant`: the mask, even when the owner holds
  none of the bits (`tighten` 15).
- `test_every_sqlite_file_beside_the_store_is_tightened`: the rollback journal is tightened as
  well as the WAL and shared memory (`tighten_store` 6, 7).

`tests/test_security_injection.py`:

- `test_each_documented_category_is_recognized_on_its_own`: one case per word or alternative in
  each category, with the reason asserted. A dropped word can't fall through to another category
  unnoticed. This kills 288 pattern mutants.
- `test_a_relation_or_value_that_opens_with_a_role_marker_is_one`: `system:`, `human:` and
  `developer:` at the start of a field (2 pattern mutants).
- `test_the_mark_reads_the_subject_too`: the `instruction` mark in recall reads the subject. The
  Hermes prefetch refuses on that mark (`volatility` 2).
- `test_an_injected_line_turns_every_control_character_into_a_space`: every control character
  becomes a space, not only line breaks (`one_line` 15, 19–21).
- `test_an_invisible_character_goes_and_the_text_after_it_stays`: removing an invisible
  character keeps the text after it (`one_line` 13).

`tests/test_volatile.py`:

- `test_the_classes_hold_at_the_edges_of_their_rules`: covers these edges:
  - a "number of" count (`measurement_test` 63, 67, 68)
  - a one-word subject's noun (87)
  - N of M over the subject's last word (154)
  - "on main" (`moving_version_test` 31)
  - a version needs a version noun (17)
  - a one-word instance (`_instance` 9, 10)
  - a three-word compound status (`status_test` 44)
  - "clean" alone is not a working-tree reading (`_tree_state` 16, 20)
- `test_a_relation_with_no_qualifier_names_no_setting`: `names_setting` 2.
- `test_a_reliability_that_is_not_a_number_is_not_a_person`: the person exemption fails closed
  (`person_checks` 7).
- `test_the_mark_in_recall_honours_the_person_exemptions`: reliability and confirmation exempt
  a belief in the mark as they do in the gate (`row_human_stated` 1, 3, 6).
- `test_older_version_compares_as_versions_without_packaging`: memware has no runtime
  dependencies, so the fallback comparison is the live path without `packaging`. This test
  covers `older_version` 9–17 and 19–23, and `_release` 1, 7, 8, 10.
- `test_the_manifest_rule_compares_only_the_package_it_names`: another subject's version is never
  compared. The package name matches in any case, and a contradiction needs a version noun
  (`manifest_rule` 3, 6, 23, 39).
- `test_the_window_is_a_strict_bound_on_age`: a reading exactly `inject.volatile_days` old is
  outside the window, to the second (`_window` 47, `admits_hit` 16, `_age_days` 16).
- `test_the_window_reads_a_timestamp_with_no_zone_as_utc_and_keeps_an_offset`: `_age_days` 11,
  12, 13.
- `test_a_reading_with_no_recorded_date_is_never_inside_the_window`: a missing date fails closed
  (`_window` 45).
- `test_the_window_measures_from_the_clock_when_no_time_is_given`: `admits_hit` 13.

## Survivors left

There are 143 mutmut survivors and 2 pattern survivors.

**Equivalent or unreachable (33).**

- `fsperm`:
  - `private_dir` 5: walking past an existing directory only retries `mkdir` on directories that
    exist, and that error is suppressed.
  - `private_dir` 11: reachable only if another process creates the directory between
    `exists()` and `mkdir()`.
  - `tighten` 1, 3, 4: the `win32` test is redundant with `os.name != "posix"` on every
    CPython.
  - `tighten` 6, 14, 23: they change only `tighten`'s return value, which no caller reads. The
    mode it sets is identical.
- `instruction`:
  - `one_line` 22–27: U+2028 and U+2029 are whitespace to `str.split`, so they become a space
    either way.
  - `one_line` 4: clean text takes the slow path to the same output.
  - Pattern `-D` in `base64 -D`: text is casefolded before matching.
  - Pattern `^` in the role-marker alternation: `_ROLE_OPENS` catches the subject's start with
    the same reason.
- `volatile`:
  - `wordset` 1: it runs only at import, before a mutant is switched on.
  - `_status_value` 7: differs only for a value holding every status and filler word.
  - `status_test` 42: an exact relation returns before the compound flag is read.
  - `status_test` 87, `person_checks` 6 and 12: two falsy values swap.
  - `_window` 13: a durable triple is already decided by its class.
  - `parse_days` 14: `float("INF")` is infinity.
  - `_age_days` 8, 9: Python 3.11's `fromisoformat` reads `Z`.
  - `_release` 9: `re.I` makes `V?` and `v?` the same.
  - `older_version` 1, 3: `Version(None)` raises `InvalidVersion`, and the fallback gives the
    same answer.
  - `older_version` 18: trailing zeros never change the order.
  - `manifest_rule` 40: the word set never equals the whole list.
  - `person_checks` 1: no caller relies on that default.

**Precision boundaries left (7).** These mutants widen a rule toward hiding durable beliefs. The
module's precision promise covers that direction, but no labeled durable case has these shapes,
and choosing one is a corpus-labeling decision, not a test decision.

- `measurement_test` 88: a work-item word second to last in the subject.
- `_tree_state` 3 and 8: a working-tree word under `state` for a subject that names no checkout.
- `_tree_state` 6, 7 and 9: "working trees" in the plural.
- `_status_value` 10: a value made only of filler words, such as "not yet".

**Withheld (1).** One finding withheld; details sent privately.

**Cosmetic (104).** These mutants change only explanation text:

- a test's `because`
- the class or `fired` of a test that did not fire, which only `--explain` reads
- the text or `applies` of the manifest and window checks
- a verdict's detail
- `Veto.__str__`
- which word an identifier veto names (`_identifier_qualifier` 4)
- which older version is named when two terms of a package name each name one (`manifest_rule`
  10, 13–15)
- `one_line` 3 and 5, a printable value with a double space or edge spaces returned as is

None of them changes what is injected.

## Re-running it

Copy the repository to a scratch directory and add the `[tool.mutmut]` table above to the copy's
`pyproject.toml`. Then run `pip install -e ".[dev,mcp]" mutmut==3.8.0` and
`mutmut run --max-children 4`. To try one mutant against the full suite, run this from the
copy's `mutants/` directory:
`MUTANT_UNDER_TEST=<name> PYTHONPATH=src pytest tests`.
