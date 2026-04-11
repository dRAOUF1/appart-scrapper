# AGENTS.md — Guidelines for AI coding agents

## Project Overview
SeLoger Tracker — Flask web app + REST API that parses real estate listing HTML, stores in PostgreSQL, deduplicates, and sends push notifications via ntfy.sh. Deployed on Render via Docker.

## Build / Run / Test Commands

### Run the app
```bash
pip install -r requirements.txt
python main.py              # dev server on :10000
```

### Docker
```bash
docker build -t appart-scrapper .
docker run -p 10000:10000 appart-scrapper
```

### Tests
There is **no test framework** configured. If you add tests, use `pytest` and place them in a `tests/` directory.

### Linting / Formatting
There is **no linter or formatter** configured. If adding one, prefer `ruff` for both linting and formatting.

## Code Style & Conventions

### Language
- Python 3.12+. Use modern syntax: `str | None` instead of `Optional[str]`, `list[dict]` instead of `List[dict]`.
- Always include `from __future__ import annotations` at the top of each file.

### Imports
- Standard library first, then third-party, then local imports — each group separated by a blank line.
- Import from `flask` using grouped parenthesized imports (see `main.py:15-18`).
- Local imports inside functions when they reference `current_app` (avoids circular imports).

### Formatting
- 4-space indentation, no tabs.
- Max line length: ~100 chars (follow existing file patterns).
- Use double quotes for strings, single quotes only inside f-strings or when natural.

### Naming Conventions
- **Modules/files**: `snake_case.py` (e.g., `config.py`, `seloger.py`)
- **Classes**: `PascalCase` (e.g., `BaseParser`, `SeLogerParser`, `Listing`)
- **Functions/methods**: `snake_case` (e.g., `create_user`, `parse_html`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `SOURCE_ID`, `SELECTORS`)
- **Private methods**: prefix with `_` (e.g., `_extract_listings_from_html`)

### Type Hints
- Use type hints on all function signatures (parameters and return types).
- Use `dict` for raw DB rows (returned via `psycopg2.extras.RealDictCursor`), not TypedDict.
- Use Pydantic `BaseModel` for configuration objects (see `config.py`).

### Error Handling
- Use `try/except` with specific exceptions (`psycopg2.IntegrityError`, `requests.RequestException`).
- Return JSON error responses with `{"error": "message"}` and appropriate HTTP status codes (400, 401, 404, 409).
- Use `logger.debug()` for verbose/internal errors, `logger.info()` for operations, `logger.warning()`/`logger.error()` for problems.
- Never crash the app on bad input — validate and return 400.

### Logging
- Use `loguru` exclusively (not `logging` module, except to silence werkzeug).
- Format: `<time> | <level> | <module>:<function> | <message>` (configured in `main.py:35-45`).

### Database
- Raw PostgreSQL via `psycopg2` (no ORM).
- Use parameterized queries (`%s` placeholders) — **never** string interpolation.
- Foreign keys enforced by PostgreSQL, transactions via `self._conn.commit()`.
- `psycopg2.extras.RealDictCursor` → convert to `dict()` before returning.

### Architecture
- **Flask app factory** pattern (`create_app()` in `main.py`).
- Two blueprints: `api_bp` (prefix `/api`) and `web_bp` (prefix `/`).
- Auth via `X-API-Token` header for API, Flask `session` for web.
- Modular parsers in `parsers/` — use the registry pattern (`BaseParser` + `@ParserRegistry.register`).

### Adding a New Parser Source
1. Create `parsers/my_source.py`
2. Subclass `BaseParser`, set `SOURCE_ID`, `SOURCE_NAME`, `SOURCE_DESCRIPTION`
3. Implement `parse(html: str) -> list[Listing]`
4. Import in `parsers/__init__.py`
5. See `parsers/template_parser.py` for a full template.

### Configuration
- Edit `config.yaml` for defaults (ntfy server, database URL, log level).
- Override via env vars: `SECRET_KEY`, `ADMIN_USERNAME`, `PORT`, `DATABASE_URL`.

### Docstrings
- Use triple-double-quoted docstrings for modules, classes, and public methods.
- Keep them concise — describe purpose, args, and return value.
- Comments in French are acceptable (consistent with existing codebase).

### Git
- Do NOT commit changes unless explicitly asked.
- Do NOT commit `venv/`, `__pycache__/`, or `.log` files.
