# vendor/

Upstream code we depend on, kept locally rather than installed system-wide so we can read what runs.

Contents of `vendor/` are **gitignored** — the upstream projects are public and large; no value in committing them to this repo. To restore on a fresh clone:

## hermes-agent (Nous Research)

Just run `./setup.sh` from the project root — it does all of this, pinned to the tested commit. Manual equivalent:

```bash
cd vendor
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent
git checkout d62808c37383ea44777229ee99a2c4cfe28d2783   # the commit this project was built/tested against
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install websockets pyyaml
```

The pin matters: cloning bare `HEAD` gives you whatever upstream shipped today, which may not match the skills. To intentionally move to a newer Hermes, run `HERMES_REF=<tag-or-commit> ./setup.sh` and re-test.

Then verify with `.venv/bin/hermes doctor`. See [docs/architecture.md](../docs/architecture.md) and the project [README](../README.md) for what Hermes is used for.
