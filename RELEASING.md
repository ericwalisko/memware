# Releasing memware

memware is published to PyPI by GitHub Actions using **trusted publishing**: PyPI
verifies a short-lived OIDC token minted by the workflow run itself, so there is no
API token to store, rotate, or leak. `.github/workflows/release.yml` is the only
thing allowed to upload, and only from a `v*` tag.

The binding is pinned to four values that must match **exactly**:

| Field | Value |
|---|---|
| PyPI project name | `memware` |
| Owner | `ericwalisko` |
| Repository name | `memware` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

Rename the workflow file, move the repo, or change the environment name, and
uploads stop with `invalid-publisher` until the binding is updated to match.

---

## One-time setup (Eric — account actions, cannot be automated)

Do these **before** the first tag. The project does not exist on PyPI yet, so
step 2 registers a *pending* publisher, which becomes a normal trusted publisher
the moment the first upload succeeds and creates the project.

### 1. PyPI account

- Log in at <https://pypi.org/> (create the account if needed).
- Enable 2FA — PyPI requires it for anyone who uploads.

### 2. Add the pending trusted publisher

Go to <https://pypi.org/manage/account/publishing/> → **Add a new pending publisher**
→ GitHub tab, and fill in exactly:

```
PyPI Project Name:  memware
Owner:              ericwalisko
Repository name:    memware
Workflow name:      release.yml
Environment name:   pypi
```

`Workflow name` is the **filename**, not the `name:` inside the YAML — enter
`release.yml`, not `Release`. `Environment name` is not optional here: the workflow
declares `environment: pypi`, and a binding without it will reject the upload.

### 3. Create the `pypi` GitHub environment

<https://github.com/ericwalisko/memware/settings/environments> → **New environment**
→ name it `pypi`.

Recommended protection, so an accidental push cannot publish:

- **Deployment branches and tags** → *Selected branches and tags* → add tag rule `v*`.
- Optionally add yourself as a **required reviewer**, which makes every upload pause
  for a manual approval in the Actions UI.

No secrets go in this environment. It exists to scope the OIDC identity and to give
the publish step an approval gate.

### 4. Confirm

Nothing to run. Setup is correct when the first release publishes; if it fails, see
Troubleshooting below.

---

## Cutting a release

1. **Bump the version.** The package version lives in:

   ```
   src/memware/__init__.py   →   __version__ = "0.1.1"
   ```

   `pyproject.toml` reads it via `[tool.hatch.version]`. The Claude Code plugin also carries
   its own copy of the version in two manifests, and `tests/test_plugin_manifest.py` fails CI
   unless both equal the package version — so bump all three together:

   ```
   integrations/claude-code/.claude-plugin/plugin.json   →   "version"
   .claude-plugin/marketplace.json                       →   plugins[memware].version
   ```

   `claude plugin update` only reinstalls when the manifest version rises, so a plugin or hook
   change that skips this bump never reaches an installed machine.

   The Hermes plugin names the release as its dependency floor, and the same test file fails CI
   unless the floor equals the package version:

   ```
   integrations/hermes/memware/plugin.yaml               →   python_dependencies: "memware>=0.1.1"
   ```

   Hermes installs memware into its own venv from that line. It keeps the version already in
   its lock as long as the floor is met, so a release that skips this bump never reaches Hermes.

2. **Cut the CHANGELOG.** Move the `## [Unreleased]` entries under a new
   `## [X.Y.Z] - YYYY-MM-DD` heading and leave `## [Unreleased]` empty above it.

3. **Open a PR, get CI green, merge to `main`.**

4. **Tag the merge commit and push the tag:**

   ```bash
   git checkout main && git pull
   git tag -a v0.1.1 -m "memware 0.1.1"
   git push origin v0.1.1
   ```

   The tag must match the package version — the workflow compares them and fails the
   build rather than publishing a mislabelled artifact.

5. **Watch the run:** <https://github.com/ericwalisko/memware/actions/workflows/release.yml>.
   `build` produces the sdist and wheel, runs `twine check --strict`, installs the
   wheel into a clean venv with the index disabled, asserts `memware --version`, and
   runs the test suite against the installed distribution. `publish` then uploads
   under the `pypi` environment.

6. **Verify from the outside:**

   ```bash
   python -m venv /tmp/mw && /tmp/mw/bin/pip install "memware==0.1.1"
   /tmp/mw/bin/memware --version    # -> 0.1.1
   ```

7. **Deploy to this Mac.** Work through [Deploy](#deploy) below: every consumer, each with its
   check. A release card cites this step and adds only the version, the date and the headline
   list. It does not restate the commands.

8. Optionally create the GitHub Release from the tag and paste the CHANGELOG section.

## Deploy

Publishing moves nothing on Eric's Mac. memware runs there in four places, and each one is
updated separately. Run these in order once the release is on PyPI, with `V` set to the version
just released. Each step has a check, and a step is done only when its check passes.

```bash
V=0.1.1
M=~/Developer/personal/memware        # any clone that has the release tag
```

| # | Consumer | What updates it |
|---|---|---|
| 1 | CLI and MCP server (`~/.local/bin/memware*`) | `uv tool install` |
| 2 | Claude Code plugin (hooks, skills, MCP config) | `claude plugin update` |
| 3 | Hermes plugin copy (`~/.hermes/plugins/memware/`) | a copy from the tag |
| 4 | Hermes gateway's venv (the `memware` package Hermes imports) | `hermes pm install`, from step 3's `plugin.yaml` |

Steps 5 and 6 restart the gateway and check it.

**1. The CLI.** The uv tool is pinned with `==`, so `uv tool upgrade` would leave it where it is.

```bash
uv tool install --force "memware[mcp]==$V"
memware --version                                   # -> $V
```

A Claude Code session that was already running keeps its old `memware-mcp` until it restarts.

**2. The Claude Code plugin.** The marketplace has to be refreshed first, or the update sees no
new version.

```bash
claude plugin marketplace update memware
claude plugin update memware@memware
python3 -c 'import json, os; print(json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json")))["plugins"]["memware@memware"][0]["version"])'   # -> $V
```

**3. The Hermes plugin copy.** Hermes loads the provider from its own copy of
`integrations/hermes/memware/`, not from a checkout. Copy it from the tag. Do not copy
`integrations/hermes/upstream/`: that is the in-tree packaging staged for the upstream PR, and it
differs from this copy on purpose.

```bash
P=~/.hermes/plugins/memware
git -C "$M" fetch --tags origin
git -C "$M" archive "v$V" integrations/hermes/memware | tar -x --strip-components 3 -C "$P"
for f in $(git -C "$M" ls-tree --name-only "v$V" integrations/hermes/memware/); do
  git -C "$M" show "v$V:$f" | cmp -s - "$P/${f##*/}" && echo "ok  $f" || echo "DIFFERS  $f"
done
grep '"memware>=' "$P/plugin.yaml"                  # -> - "memware>=$V"
```

**4. The gateway's venv.** Hermes' package manager (pm) builds Hermes' venv. It installs every
enabled plugin's `python_dependencies`, and the provider named by `memory.provider` counts as
enabled. Step 3 raised the floor, so this resolves `memware==$V` from PyPI into a new venv
generation.

```bash
hermes pm install
venv=$(python3 -c 'import glob, json, os; print(json.load(open(glob.glob(os.path.expanduser("~/.hermes/installs/*/facts.json"))[0]))["packages"]["venv"]["environment"])')
"$venv/bin/python" -I -c 'import memware; print(memware.__version__, memware.__file__)'
# -> $V …/venv/lib/python3.*/site-packages/memware/__init__.py
```

A path under `~/Developer/` instead of `site-packages` means the gateway still runs an editable
install from a checkout. `hermes pm` does not know about that install, and the next venv rebuild
drops it. Do not use `hermes pm repair` here. Repair rebuilds the package set pm already
recorded, so it cannot add a floor pm has not seen, and it drops anything installed by hand.

**5. Restart the gateway.** The gateway keeps the memware it imported at start, and it picks up
the new venv only when it restarts. Restart it only while nothing is in flight: no reply being
written, no cron job or kanban dispatch running. A restart kills any of them mid-turn. Ask Eric
first.

```bash
since=$(date '+%Y-%m-%d %H:%M:%S')
launchctl kickstart -k gui/$UID/ai.hermes.gateway
```

**6. Check the gateway after the restart.**

```bash
hermes memory status | grep -E 'Provider:|Status:'  # -> Provider: memware / Status: available ✓
awk -v t="$since" 'substr($0, 1, 19) >= t' ~/.hermes/logs/agent.log | grep "Memory provider 'memware'"
# after the next agent session (send the gateway any message):
# -> … INFO run_agent: Memory provider 'memware' activated
```

A `reports unavailable` line after `$since` is a failure: the gateway started without memware.
Run step 4 again. If step 4's check passes and the line still appears, the gateway is not
running the venv that `facts.json` selects.

### After a Hermes desktop-app update

A Hermes update rebuilds the gateway's venv and restarts the gateway itself. The rebuild installs
enabled plugins' `python_dependencies` the same way `hermes pm install` does, and the update
leaves `~/.hermes/plugins/` alone. So memware comes back as long as the plugin copy carries its
floor. Run the step 4 check and then step 6. If memware is missing, run `hermes pm install`,
then steps 5 and 6. The 2026-09-24 update (receipt step `historical_takeover`) dropped memware
because the plugin copy carried no floor then. The gateway had been importing an editable
install that pm never recorded.

## What the workflow will not do

- **Never publishes from a branch.** The `publish` job is gated on
  `github.ref_type == 'tag'`; `workflow_dispatch` on a branch builds and verifies only,
  which is the safe way to rehearse a release.
- **Never publishes a version that disagrees with its tag.**
- **Never publishes twice.** PyPI refuses to overwrite an existing file, so a re-run of
  an already-published tag fails loudly. A bad release is superseded by a new version —
  yanking on PyPI hides a release from resolvers but never frees the version number.

## Troubleshooting

**`invalid-publisher` / `not a valid publisher for this project`** — the binding does not
match the run. Check all five fields in step 2, especially `release.yml` (filename, not
workflow title) and `pypi` (environment name, easy to leave blank).

**`Trusted publishing exchange failure` with no other detail** — the `publish` job is
missing `permissions: id-token: write`, or the job did not declare `environment: pypi`.

**Build fails at the version check** — the tag and `__version__` disagree. Delete the tag
(`git push origin :refs/tags/vX.Y.Z`), fix the version, and re-tag.

**`File already exists`** — that version is published. Bump to the next patch version;
PyPI never lets a version number be reused, even after deletion.
