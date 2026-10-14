# Employee Attendance & Analytics API

A FastAPI service for employee records, attendance punches, manual corrections, and attendance analytics. MongoDB stores employee and attendance documents; PyMongo performs CRUD operations and aggregation. The application, including the built-in browser console, is in `app/main.py`.

The HTTP contract and business rules are in `openapi.yaml`. Stored document shapes and sample records are described in `DATA_MODEL.md`.

## Contents

- [Requirements](#requirements)
- [Setup and run](#setup-and-run)
- [Configuration](#configuration)
- [How the application works](#how-the-application-works)
- [API routes](#api-routes)
- [Time and attendance rules](#time-and-attendance-rules)
- [MongoDB indexes and concurrency](#mongodb-indexes-and-concurrency)
- [Analytics implementation](#analytics-implementation)
- [Browser console](#browser-console)
- [Tests and sample data](#tests-and-sample-data)
- [Project files](#project-files)

## Requirements

- Python 3.11 or later.
- MongoDB 6.0 or later. MongoDB 7.0 is suitable. The trend pipeline uses `$dateAdd` and `$setWindowFields`.
- Dependencies from `requirements.txt`.

## Setup and run

Start a MongoDB server that the application can reach. For a local server, the default URI is `mongodb://localhost:27017`.

### Windows PowerShell

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### macOS or Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

When the server starts, it attempts to create the required indexes. Check readiness at <http://127.0.0.1:8000/health>. The interactive API documentation is at <http://127.0.0.1:8000/docs>; the generated OpenAPI document is at <http://127.0.0.1:8000/openapi.json>.

To stop the server, press `Ctrl+C` in the terminal running Uvicorn.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

The application calls `load_dotenv()` so a local `.env` file can provide these values. Values already set in the process environment take precedence. `.env.example` is a template; copy it to `.env` for local use and never commit credentials or `.env` itself. Atlas URIs work as long as the machine running the application can connect to the cluster.

The Mongo client is timezone-aware and treats stored instants as UTC. Its server-selection timeout is five seconds. Database exceptions are returned as HTTP 503 responses; `/health` pings MongoDB and returns 503 when it cannot be reached.

## How the application works

All API behavior is implemented in `app/main.py`:

1. **Configuration** loads environment values, creates a shared `MongoClient`, and selects the database.
2. **Time helpers** convert between epoch milliseconds and UTC datetimes, truncate incoming instants to whole seconds, compute attendance dates in IST, and perform half-up rounding.
3. **Business-rule helpers** calculate lateness, work hours, overtime, and half-day status. `derive_fields()` is the shared calculation path for punch-in, punch-out, and correction.
4. **Serializers** convert MongoDB documents to API responses. They omit MongoDB `_id`, turn datetimes into epoch milliseconds, and supply defaults for legacy `history` and `half_day` fields.
5. **Pydantic request models** validate request types, employee codes, timestamps, shift times, dates, and status values before endpoint logic runs.
6. **Application lifespan** creates indexes. The health route can retry index creation if startup could not reach MongoDB.
7. **Route handlers** implement employee and attendance writes, listing, corrections, and analytics.
8. **Pipeline builders** define the MongoDB aggregation operations. The explain route reuses these builders so it explains the same operations used by the corresponding endpoints.

Employee identity is the client-supplied `emp_code`, not MongoDB `_id`. An attendance document is addressed by the pair (`emp_code`, `date`). API instants are integer epoch milliseconds; calendar dates and shift times are strings (`YYYY-MM-DD` and `HH:MM`).

## API routes

All errors use FastAPI's standard `{"detail": ...}` response. Invalid request parameters or bodies normally return 422; missing resources return 404; conflicting writes return 409; database availability failures return 503.

| Method and path | Purpose | Important behavior |
|---|---|---|
| `GET /` | Serve the browser console | HTML is served by FastAPI; this route is not part of the OpenAPI contract. |
| `GET /health` | Readiness check | Pings MongoDB and returns `{"status":"ok"}` only when the database is reachable. |
| `POST /employees` | Create an employee | Validates the supplied `EMP` code, email, shift and join date; sets `created_at`; returns 409 for a duplicate code. |
| `GET /employees` | List employees | Optional exact department filter; sorted by `emp_code`; page starts at 1 and page size is 1-100. |
| `POST /attendance/punch-in` | Start an attendance record | Accepts `PRESENT`, `WFH` or `ON_DUTY`; calculates the IST attendance date and late minutes; duplicate employee/day returns 409. |
| `POST /attendance/punch-out` | Close an open record | Selects the latest punch-in at or before the requested instant, including across midnight; calculates work hours, overtime and half-day status. |
| `GET /attendance` | List attendance records | Optional employee, inclusive date range and status filters; sorted by date descending, then employee code ascending; paginated. |
| `PATCH /attendance/{emp_code}/{date}` | Correct one attendance record | Validates the final status and punches, recomputes derived fields, and appends one audit-history entry. |
| `GET /analytics/employees/{emp_code}/monthly` | Monthly employee summary | Returns working days, present/leave days, lateness, overtime and attendance percentage. |
| `GET /analytics/departments/summary` | Monthly department summary | Includes eligible employees with no attendance records; optional exact department filter. |
| `GET /analytics/leaderboard/late` | Monthly lateness ranking | Uses competition ranks; ties share rank and all employees tied at the cutoff are returned. |
| `GET /analytics/departments/{department}/trend` | Daily department trend | Includes every requested date, including weekends and days without records; range is limited to 92 days. |
| `GET /admin/explain/{endpoint}` | Explain a supported query | Returns MongoDB `executionStats` explain output for the selected endpoint's main operation. |

The `month` query parameter is `YYYY-MM`. The trend route uses inclusive `from` and `to` dates. The explain route accepts one of `attendance_list`, `employee_monthly`, `department_summary`, `late_leaderboard`, or `department_trend`, plus the parameters needed by that operation.

### Example workflow

Create an employee:

```bash
curl -X POST http://127.0.0.1:8000/employees \
	-H "Content-Type: application/json" \
	-d '{"emp_code":"EMP0007","name":"Priya Menon","email":"priya@example.com","department":"Engineering","joined_on":"2026-01-05"}'
```

Punch in using an epoch-millisecond timestamp (omit `punched_at` to use the server's current time):

```bash
curl -X POST http://127.0.0.1:8000/attendance/punch-in \
	-H "Content-Type: application/json" \
	-d '{"emp_code":"EMP0007","status":"PRESENT","punched_at":1783312500000}'
```

Punch out, then query the employee's monthly summary:

```bash
curl -X POST http://127.0.0.1:8000/attendance/punch-out \
	-H "Content-Type: application/json" \
	-d '{"emp_code":"EMP0007","punched_at":1783344900000}'

curl "http://127.0.0.1:8000/analytics/employees/EMP0007/monthly?month=2026-07"
```

## Time and attendance rules

- Shift times and attendance dates use IST (UTC+05:30). BSON datetimes are stored as UTC instants; the API returns instants as epoch milliseconds.
- Incoming instants must be strict integer milliseconds within the supported range. Seconds-looking values, floats, strings and booleans are rejected. Instants are truncated to whole seconds before calculations and storage.
- For an overnight shift (`shift_end <= shift_start`), a punch-in earlier than the shift end belongs to the previous day's shift. The record date is the shift's start date.
- Lateness is zero through the first ten minutes. It becomes lateness only when the punch-in is strictly more than ten minutes after shift start; the reported minutes are floored from shift start.
- Overtime is floored to whole minutes and is counted only when it is at least 30 minutes past shift end.
- Work hours are the elapsed seconds divided by 3,600, rounded to two decimals using half-up rounding. Half day is true when that rounded value is below 4.50.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence. `ABSENT` and `LEAVE` do not. Half days count as 0.5 present days. Monthly present days count only weekdays, but weekend records still contribute to lateness and overtime totals.
- Working days are Monday through Friday, starting no earlier than `joined_on`; there is no holiday calendar. Department headcount includes employees who joined by the period end, even if they have no logs.
- Analytics use the stored derived fields in attendance documents. They do not recalculate historical metrics from punch timestamps.
- Manual corrections append to `history`; punch-in and punch-out do not. Each entry records the actor, reason, timestamp, and before/after values for fields that actually changed.

## MongoDB indexes and concurrency

Indexes are created idempotently during application startup:

| Collection | Index | Use |
|---|---|---|
| `employees` | Unique `emp_code` | Employee identity and duplicate-create protection. |
| `employees` | (`department`, `emp_code`) | Department filtering and employee listing order. |
| `employees` | `joined_on` | Headcount eligibility by period. |
| `attendance_logs` | Unique (`emp_code`, `date`) | One record per employee per attendance day; resolves simultaneous punch-ins. |
| `attendance_logs` | (`date` descending, `emp_code` ascending) | Attendance date scans and list ordering. |
| `attendance_logs` | (`status`, `date` descending, `emp_code` ascending) | Status-filtered attendance queries. |

Employee creation and punch-in rely on unique database indexes, not a read-then-insert check. Punch-out uses a conditional `find_one_and_update`; only a request that finds the record still open can close it. Regularization uses optimistic concurrency: it updates only if the values and history length still match what it read, then retries after a conflict so concurrent corrections do not silently overwrite history. A retry limit can return 409 when the record remains under contention.

## Analytics implementation

Analytics are calculated with MongoDB aggregation pipelines rather than loading all attendance documents into application memory. The month-specific pipelines match the indexed string date range and use stored metric fields.

- **Employee monthly:** groups one employee's monthly records for present-day weight, leave count, late count/minutes and overtime. The endpoint calculates the weekday count from the employee's join date and then computes the percentage.
- **Department summary:** starts from eligible employees, looks up each employee's monthly logs, then groups by department. Starting from employees keeps people with no logs in the headcount. Average work hours are computed over qualifying records, not over employee averages.
- **Late leaderboard:** groups late records by employee, joins employee details, optionally filters to a department, assigns MongoDB `$rank`, and applies the rank cutoff after ranking so ties remain included.
- **Department trend:** validates the department and date range, groups matching logs by day, generates the requested calendar dates in MongoDB, looks up each day's headcount, and uses `$setWindowFields` for the rolling seven-row average. Rates and averages are rounded to four decimals using integer arithmetic.
- **Explain:** builds the same query or pipeline used by its endpoint and asks MongoDB for `executionStats` verbosity. Use it to inspect whether an index scan is selected for the supplied parameters.

For an unfiltered list request, the current implementation uses MongoDB's `estimated_document_count()` for the `total`; filtered list requests use `count_documents()` with the applied filter. The unfiltered total is therefore an estimate rather than an exact count.

## Browser console

Open <http://127.0.0.1:8000/> while the server is running. The console is embedded in `app/main.py` and does not require a separate frontend build. Its sections provide a dashboard, employee creation/listing, punch desk, attendance listing, regularization, analytics, and explain-plan form. It calls the same HTTP API routes documented above. Errors are shown in the console as notifications; the health indicator checks MongoDB periodically.

## Tests and sample data

Run the fast local tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest extras/test_rules.py extras/test_api_mock.py -q
```

Or on macOS/Linux:

```bash
.venv/bin/python -m pytest extras/test_rules.py extras/test_api_mock.py -q
```

`test_rules.py` covers the pure time and attendance calculations. `test_api_mock.py` uses `mongomock` to exercise API validation, CRUD, punch flows, regularization, pagination and legacy record defaults. Aggregation behavior requires a real MongoDB server.

`sample_seed.py` loads the small Extended JSON fixtures in `sample_data/`. **It deletes all documents from the selected database's `employees` and `attendance_logs` collections before inserting the samples.** Set `MONGO_DB` to a disposable database before running it.

`extras/verify_live.py` is the larger integration verifier. It creates about 100,000 attendance records, compares analytics against Python reference calculations, checks explain plans and speed, and tests concurrent writes. **It deletes and replaces both collections in the database named by `MONGO_DB` (default: `attendance_verify`). Never point it at data you need.** Run it only after starting the API with the same isolated database and setting `BASE_URL` to that server. For example, use a separate terminal/server port and a unique database name:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_verify_scratch"
$env:BASE_URL = "http://127.0.0.1:8001"
```

Start Uvicorn in another terminal with those same `MONGO_URI` and `MONGO_DB` values, then run:

```powershell
.\.venv\Scripts\python.exe extras\verify_live.py
```

The live verifier needs `httpx` and a reachable MongoDB 6.0+ instance. Remove the disposable test database after the run if it is no longer needed.

## Project files

| File or directory | Contents |
|---|---|
| `app/main.py` | FastAPI app, request models, time/business-rule helpers, serializers, routes, aggregation pipelines, and embedded console. |
| `requirements.txt` | Runtime and test dependencies. |
| `openapi.yaml` | API contract, schemas, status codes, parameters, and rules R1-R10. |
| `DATA_MODEL.md` | MongoDB collections, field types, and sample document descriptions. |
| `sample_data/` | Small MongoDB Extended JSON fixtures. |
| `sample_seed.py` | Loads fixtures into the configured database; clears the two collections first. |
| `extras/test_rules.py` | Unit tests for calculation helpers. |
| `extras/test_api_mock.py` | API tests using an in-memory MongoDB mock. |
| `extras/verify_live.py` | Destructive, large-data integration and performance verifier. |
| `REVIEW.md` | Defects identified in the supplied starter and their fixes. |
| `DECISIONS.md` | Index, concurrency, ranking, headcount, and scaling decisions. |
