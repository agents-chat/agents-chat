# Contributing to Agents Chat

Agents Chat Community Edition is a public beta. Small, focused changes with
tests are welcome.

## Before opening a change

- Search existing issues first. If you are unsure whether a change belongs,
  open a structured [question](https://github.com/agents-chat/agents-chat/issues/new?template=question.yml)
  before starting non-trivial work so effort is not duplicated.
- Keep credentials, chat data, customer information, and machine-specific paths
  out of commits, fixtures, screenshots, and logs.
- Use a separate branch and avoid mixing unrelated work.
- Add or update tests for behavior changes.
- Run the focused tests, then the full verification suite when practical.

## Development setup

Install Python 3.12, [`uv` 0.11.32](https://docs.astral.sh/uv/), Node.js, and the
npm version declared in `package.json`. From the repository root on macOS or
Linux:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements.lock
uv pip install --python .venv/bin/python --require-hashes -r requirements-dev.lock
npm ci --ignore-scripts
```

On Windows, use `.venv\Scripts\python.exe` wherever the commands above or below
use `.venv/bin/python`. Keep development data and configuration outside the
source tree.

## Verification

Run all four checks before opening a pull request:

```sh
.venv/bin/python scripts/community/verify_release.py --source
.venv/bin/python -m pytest -q
npm test
npm run typecheck
```

When a Python dependency changes, regenerate and commit both lockfiles with the
same Python target and universal hashes used by CI:

```sh
uv pip compile requirements.txt --python-version 3.12 --universal --generate-hashes --output-file requirements.lock
uv pip compile requirements-dev.txt --python-version 3.12 --universal --generate-hashes --output-file requirements-dev.lock
```

The public repository is already an exported Community tree. In a maintainer
source checkout that includes the private release builder, CI also builds a
fresh export and runs every shipped Python test inside it with the release
contract required.

## Pull requests

Explain the user-visible result, risks, verification performed, and any follow-up
work. A pull request is not ready when it depends on local-only files or secrets.

By submitting a contribution, you agree that it may be distributed under the
repository's MIT License.
