# Contributing

Thanks for taking a look! This is a personal learning project, but issues and PRs are welcome.

## Dev setup

```bash
cp .env.example .env   # a fake LLM_API_KEY is fine for the tool tests
pip install -r requirements.txt pytest
python -m pytest tests/ -q
./start.sh
```

## Ground rules

- New tools go in `backend/tools.py`: add a JSON schema in `TOOL_SCHEMAS` and a branch in `run_tool`, and keep every file/command operation inside the workspace sandbox (`_safe_path`).
- Tool failures must return an error string instead of raising — the model uses it to self-correct.
- No secrets in code, tests, or commits. `.env` is ignored on purpose.
- Keep the frontend build-free (plain HTML/JS).
