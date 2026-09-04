# Editor and terminal integration

memware ships **no editor plugins**. It ships two machine-readable output modes —
`--plain` (tab-separated, one record per line, id-first) and `--json` — and those are the
integration surface. Everything below is a copy-paste recipe built on the CLI, so nothing
here goes stale against an editor's plugin API. See [Why no plugin](#why-no-plugin) at the end.

The `--plain` column order for `recall` is fixed:

```text
id  kind  score  session  when  role  subject  relation  source  text
```

so `cut -f1` is the id, `cut -f4` is the session, and `cut -f10` is the text. The `read`
command takes a **session** id, so `cut -f4 | xargs memware read` reads a hit back in context.

## Shell completions

`memware completions {bash,zsh,fish}` prints a completion script (generated with `shtab`).
It needs the `[shell]` extra:

```bash
uv tool install "memware[mcp,shell]"    # or: pipx install "memware[shell]"  /  pip install "memware[shell]"
```

Without `shtab` the command exits 2. Install recipes:

```bash
# zsh
memware completions zsh > ~/.zfunc/_memware
# then in ~/.zshrc:  fpath+=~/.zfunc   and   autoload -Uz compinit && compinit

# bash
memware completions bash | sudo tee /etc/bash_completion.d/memware
# or a user dir:  memware completions bash > ~/.local/share/bash-completion/completions/memware

# fish
memware completions fish > ~/.config/fish/completions/memware.fish
```

## fzf

Pick a hit interactively and read its session back:

```bash
memware recall "which port does the api use" --plain \
  | fzf --delimiter='\t' --with-nth=10 \
  | cut -f4 | xargs -r memware read
```

`--with-nth=10` shows only the text column in the picker; the full tab record is still on the
selected line, so `cut -f4` recovers the session for `read`.

A reusable function that echoes the chosen id and text (drop it in `~/.zshrc` or
`~/.bashrc`), passing any recall args through:

```bash
mw-recall() {
  local hit
  hit=$(memware recall "$@" --plain | fzf --delimiter='\t' --with-nth=10) || return
  printf 'id\t%s\n'   "$(printf '%s' "$hit" | cut -f1)"
  printf 'text\t%s\n' "$(printf '%s' "$hit" | cut -f10)"
}
# mw-recall "api port" "gateway listen port"
```

## Emacs

A small command that reads a query, offers the hits with `completing-read`, and inserts the
chosen record's text at point:

```elisp
(defun memware-recall (queries)
  "Recall from memware and insert the chosen record's text at point."
  (interactive "sQueries: ")
  (let* ((lines  (split-string
                  (shell-command-to-string
                   (format "memware recall %s --plain"
                           (shell-quote-argument queries)))
                  "\n" t))
         (choice (completing-read "hit: " lines)))
    ;; text is the 10th tab-separated field (0-based index 9)
    (insert (nth 9 (split-string choice "\t")))))
```

For a grep-style buffer, point `grep-command` at recall's plain output and use `M-x grep`;
you get the hits in a `compilation-mode` buffer you can navigate as text (the records are
id-first tab lines, not `file:line`, so there is nothing to jump to):

```elisp
(setq grep-command "memware recall --plain ")
```

Consult users can wrap the same `memware recall … --plain` lines as a `consult--read` source
for live minibuffer recall.

## Vim

A user command that opens plain recall output in a scratch buffer:

```vim
command! -nargs=+ MemwareRecall call s:MemwareRecall(<q-args>)
function! s:MemwareRecall(args) abort
  let l:out = system('memware recall ' . shellescape(a:args) . ' --plain')
  new
  setlocal buftype=nofile bufhidden=wipe noswapfile
  call setline(1, split(l:out, "\n"))
endfunction
" :MemwareRecall api port
```

With [fzf.vim](https://github.com/junegunn/fzf.vim), pick a hit in a fuzzy window:

```vim
command! -nargs=+ MemwareFzf call fzf#run(fzf#wrap({
      \ 'source':  'memware recall ' . shellescape(<q-args>) . ' --plain',
      \ 'options': ['--delimiter', "\t", '--with-nth', '10']}))
```

## Neovim

The Lua equivalent, writing plain recall output into a scratch buffer:

```lua
vim.api.nvim_create_user_command('MemwareRecall', function(opts)
  local out = vim.fn.system({ 'memware', 'recall', opts.args, '--plain' })
  vim.cmd('new')
  vim.bo.buftype   = 'nofile'
  vim.bo.bufhidden = 'wipe'
  vim.bo.swapfile  = false
  vim.api.nvim_buf_set_lines(0, 0, -1, false, vim.split(out, '\n'))
end, { nargs = '+' })
-- :MemwareRecall api port
```

Telescope users can wrap `memware recall … --plain` as a custom finder over the same lines.

## Bulk-edit facts through `$EDITOR`

Dump the beliefs, edit them in your editor, and feed them back. `beliefs --plain` is id-first,
while `assert -` reads `subject<TAB>relation<TAB>value[<TAB>source]`, so drop the id and the
audit columns with `cut` first — fields 2,3,4,9 are exactly subject, relation, value, source:

```bash
memware beliefs --plain | cut -f2,3,4,9 > /tmp/f   # -> subject relation value source
$EDITOR /tmp/f
memware assert - < /tmp/f
```

`assert -` skips blank lines and lines starting with `#`. This **asserts** every edited or
added row (each goes through the normal supersession rule); it does **not** delete — removing a
line from the file does not retract a belief.

## Why no plugin

The CLI is the stable contract. `--plain` and `--json` are the whole integration surface, so a
twenty-line recipe against them keeps working across editor releases in a way a plugin tracking
an editor's evolving API would not. There is nothing to update when your editor updates.
