# Accessibility

memware is a command-line tool, so it is accessible by nature: keyboard-only, no mouse, no
GUI, and fully scriptable. Beyond that baseline, these are commitments about how it behaves.
They are CLI accessibility practices — not WCAG conformance, which is a standard for web UIs.

## No colour, ever

memware emits **no colour escape sequences at all**, under any flag, terminal, or theme. No
information is ever carried by colour, so output is legible under any palette, to colour-blind
users, and in monochrome terminals. `NO_COLOR` is honoured **by construction**: there is
nothing to disable, and nothing that could regress it.

## Screen-reader-friendly default output

The default human output for the record listers (`recall`, `beliefs`, `read`) is **labeled**:
one `field : value` per line, a blank line between records, and empty fields skipped. Nothing
is aligned into columns by eye. A screen reader reads it linearly and unambiguously — there is
no spatial layout to get lost in.

Two machine formats are available when you want structure instead of prose:

- `--plain` — tab-separated, one record per line, id-first, in a fixed column order; tabs and
  newlines inside values collapse to spaces. Stable for piping to `cut`, `awk`, `fzf`, or a
  braille display's filter.
- `--json` — machine-readable JSON for programmatic consumers.

## ASCII fallback for glyphs

The one non-ASCII glyph in normal output is the elision mark `…`. `--ascii` (or
`MEMWARE_ASCII=1`) replaces it with `...`, so it is not mispronounced by a screen reader or
mojibaked by a terminal that cannot render it. This also switches on **automatically** when the
locale is not UTF-8, so a stripped-down or remote environment gets ASCII without any flag.

## stdout / stderr separation

Data goes to **stdout**; diagnostics and progress go to **stderr**. Assistive tooling and
scripts can consume results without narration or warnings mixed in, and a screen reader driving
a pipeline hears only the data.

## Documented, meaningful exit codes

Exit status is `0` on success and `2` on a usage error (for example, `assert` missing its VALUE
without `-`, or `completions` when `shtab` is not installed). Automation and assistive wrappers
can branch on the status without parsing text.

## Plain-text integration surfaces

The MCP server's tool outputs and the prompt-time hook's injected context are plain text — the
same accessible, colourless, linear content, not a rendered widget.

## Feedback

If any output is hard to read with a screen reader, a braille display, or a non-UTF-8 terminal,
that is a bug. Please open an issue at
<https://github.com/ericwalisko/memware>. Accessibility regressions are treated as bugs, not
enhancements.
