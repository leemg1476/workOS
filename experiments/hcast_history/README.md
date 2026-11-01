# HCAST history PoC reproduction

HCAST source is an evaluation dataset, not training data. Credit: METR, [hcast-public](https://github.com/METR/hcast-public), MIT license for repository code. Research papers included as task assets have their own licenses. Do not copy the corpus into a training set. The outputs here do not include source passages.

## Verify recorded results without a model call

Requires Python 3.10, scikit-learn 1.7.2, pypdf 6.14.0, beautifulsoup4 4.14.3, the backend dependencies, Git, network access, and a disposable local directory. The commands below use PowerShell. Do not point these paths at unrelated files.

```powershell
$env:HCAST_ROOT = Join-Path $env:TEMP 'workos-hcast-public'
$env:HCAST_TEX_ASSETS = Join-Path $env:TEMP 'workos-hcast-assets'
git clone https://github.com/METR/hcast-public.git $env:HCAST_ROOT
git -C $env:HCAST_ROOT checkout 03438239d200fb3e89a5ddfc62ab4d23cc1866fb
python experiments/hcast_history/fetch_assets.py
$env:PYTHONPATH = 'backend;experiments/hcast_history'
python experiments/hcast_history/verify.py
```

If the checkout already exists, verify its commit before use rather than cloning over it. The fetcher checks the pinned DVC directory and per-file MD5 and refuses to overwrite different files. It fetches the 23 `.tex`/`.bbl` assets used by this experiment, not all original benchmark resources. `verify.py` recomputes the retrieval, official scores, previous-only history links, source hashes, artifact hashes and paired results. This is reproducibility and internal consistency, not a provider-authenticated proof of the recorded model responses.

## Rerun the model experiment

**Caution:** A rerun incurs model charges, calls an external provider with the public inputs, overwrites recorded JSON files, and produces nondeterministic new results. Preserve the existing evidence before rerunning. It must use a fresh, disposable PostgreSQL database named `hcast_history_poc`, and environment variables `DATABASE_URL`, `openai_api_key` / `OPENAI_API_KEY` and `openai_model` / `OPENAI_MODEL` configured outside Git. Never print or commit credentials. Use `WORKOS_MODE=MOCK` for local services and register the task as `LIVE_LLM` as the script does.

After preparing the assets and environment, execute the scripts **in order**: `python experiments/hcast_history/run.py`, then `python experiments/hcast_history/followup.py`, then `python experiments/hcast_history/candidate.py`, then `python experiments/hcast_history/recovery.py`. Followup requires the DB and artifacts from the first script; candidate and recovery require the accumulated feedback in that same DB. The history table refuses overwriting existing observations; use a fresh disposable DB, not a production or user DB. This code creates experimental tables and imports prior scored outcomes as simulated feedback; it does not implement the product's automatic history selection.
