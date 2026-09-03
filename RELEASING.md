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

1. **Bump the version.** It lives in exactly one place:

   ```
   src/memware/__init__.py   →   __version__ = "0.1.1"
   ```

   `pyproject.toml` reads it via `[tool.hatch.version]`, so nothing else needs editing.

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

7. Optionally create the GitHub Release from the tag and paste the CHANGELOG section.

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
