<a id="civicparks"></a>

# Townlight Parks

Townlight Parks v0.1.1 ships the municipal parks and recreation support foundation for Townlight: cited parks policy Q&A, program and facility Q&A, registration assistance that links to existing systems, optional database-backed registration/maintenance workpapers, maintenance request triage for Townlight 311-style handoff, FastAPI runtime, public sample UI, docs, tests, browser QA, and release gates.

It is not a registration system, payment processor, reservation system, participant-record store, work-order system, live LLM runtime, or parks connector.

Install:

```bash
python -m pip install -e ".[dev]"
python -m uvicorn civicparks.main:app --host 127.0.0.1 --port 8143
```

Townlight Parks v0.1.1 is pinned to `civiccore==0.3.0`.

Set `CIVICPARKS_WORKPAPER_DB_URL` to persist registration assistance and maintenance triage records. Without it, Townlight Parks remains deterministic and stateless.

## License

Code: AGPL-3.0-only, with the TownLight Public Institution Exception for qualifying deployments. See [LICENSE](LICENSE) and [LICENSE-EXCEPTION.md](LICENSE-EXCEPTION.md).
Documentation: CC BY 4.0 ([LICENSE-DOCS](LICENSE-DOCS)).
