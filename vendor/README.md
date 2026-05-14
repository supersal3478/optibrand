# vendor/

Upstream code we depend on, kept locally rather than installed system-wide so we can read what runs.

Contents of `vendor/` are **gitignored** — the upstream projects are public and large; no value in committing them to this repo. To restore on a fresh clone:

## hermes-agent (Nous Research)

```bash
cd vendor
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then verify with `.venv/bin/hermes doctor`. See [docs/architecture.md](../docs/architecture.md) and the project [README](../README.md) for what Hermes is used for.
