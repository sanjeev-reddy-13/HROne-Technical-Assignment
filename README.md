# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"            # Windows PowerShell: $env:MONGO_URI="..."
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$setWindowFields`, `$dateAdd`). A local `.env` with the same two variables also works; real environment variables win. Indexes are created at startup. Interactive docs: http://localhost:8000/docs

## Status
All endpoints in `openapi.yaml` are implemented, including the explain endpoint. Nothing is intentionally missing.

## Notes / interpretations
- Analytics read the **stored** derived values (`late_minutes`, `work_hours`, ...), they never recompute them.
- Half-up rounding is done with integer arithmetic (MongoDB's `$round` is banker's rounding).
- `moving_avg_7d` averages the **rounded** (4 dp) `attendance_rate` values shown in the rows, as the contract words it.
- A punch-out earlier than every punch-in of the employee returns 404 ("no punch-in found at or before it").
- Concurrent corrections use optimistic concurrency and retry, so both history entries are kept.
