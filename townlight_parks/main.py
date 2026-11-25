"""FastAPI runtime foundation for Townlight Parks."""

import os

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from townlight_core import __version__ as TOWNLIGHT_CORE_VERSION

from townlight_parks import __version__
from townlight_parks.maintenance import triage_maintenance_request
from townlight_parks.persistence import (
    ParksWorkpaperRepository,
    StoredMaintenanceTriage,
    StoredRegistrationAssistance,
)
from townlight_parks.policy import ParksPolicySource, answer_policy_question
from townlight_parks.programs import ParksProgram, answer_program_question
from townlight_parks.public_ui import render_public_lookup_page
from townlight_parks.registration import RecreationProgram, draft_registration_assistance

app = FastAPI(
    title='Townlight Parks',
    version=__version__,
    description='Parks and recreation policy, program, registration-link, and maintenance-triage foundation.',
)

_workpaper_repository: ParksWorkpaperRepository | None = None
_workpaper_db_url: str | None = None

@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Return an empty favicon response so browser QA has a clean console."""

    return Response(status_code=204)

POLICY_SOURCES = [
    ParksPolicySource(
        "policy-1",
        "Park rental policy",
        "Facility rentals require staff confirmation and published fee schedules.",
        "Parks Policy 1",
    )
]


class PolicyQuestionRequest(BaseModel):
    question: str


class ProgramQuestionRequest(BaseModel):
    question: str
    programs: list[ParksProgram]


class RegistrationAssistanceRequest(BaseModel):
    program: RecreationProgram


class MaintenanceTriageRequest(BaseModel):
    issue: str
    location: str


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "Townlight Parks",
        "version": __version__,
        "status": "parks and recreation support foundation",
        "message": (
            "Townlight Parks policy Q&A, program Q&A, registration-link assistance, "
            "maintenance triage, optional database-backed registration/maintenance workpapers, "
            "and public UI foundation are online; payments, "
            "registrations, participant records, reservation writes, crew dispatch, "
            "live LLM calls, and connector runtime are not implemented yet."
        ),
        "next_step": "Post-v0.2.0 roadmap: registration-system and Townlight 311 handoff adapter design",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "townlight_parks",
        "version": __version__,
        "townlight_core_version": TOWNLIGHT_CORE_VERSION,
    }


@app.get("/townlight_parks", response_class=HTMLResponse)
def public_page() -> str:
    return render_public_lookup_page()


@app.post("/api/v1/townlight_parks/policy-answer")
def policy_answer(request: PolicyQuestionRequest) -> dict[str, object]:
    return answer_policy_question(request.question, POLICY_SOURCES).__dict__


@app.post("/api/v1/townlight_parks/program-answer")
def program_answer(request: ProgramQuestionRequest) -> dict[str, object]:
    return answer_program_question(request.question, request.programs).__dict__


@app.post("/api/v1/townlight_parks/registration-assistance")
def registration_assistance(request: RegistrationAssistanceRequest) -> dict[str, object]:
    if _workpaper_database_url() is not None:
        return _stored_registration_response(_get_workpaper_repository().create_registration(program=request.program))
    payload = draft_registration_assistance(request.program).__dict__
    payload["assistance_id"] = None
    return payload

@app.get("/api/v1/townlight_parks/registration-assistance/{assistance_id}")
def get_registration_assistance(assistance_id: str) -> dict[str, object]:
    if _workpaper_database_url() is None:
        raise HTTPException(status_code=503, detail={"message":"Townlight Parks workpaper persistence is not configured.","fix":"Set TOWNLIGHT_PARKS_WORKPAPER_DB_URL to retrieve persisted registration assistance."})
    stored = _get_workpaper_repository().get_registration(assistance_id)
    if stored is None:
        raise HTTPException(status_code=404, detail={"message":"Registration assistance record not found.","fix":"Use an assistance_id returned by POST /api/v1/townlight_parks/registration-assistance."})
    return _stored_registration_response(stored)


@app.post("/api/v1/townlight_parks/maintenance-triage")
def maintenance_triage(request: MaintenanceTriageRequest) -> dict[str, object]:
    if _workpaper_database_url() is not None:
        return _stored_maintenance_response(_get_workpaper_repository().create_maintenance(issue=request.issue, location=request.location))
    payload = triage_maintenance_request(request.issue, request.location).__dict__
    payload["triage_id"] = None
    return payload

@app.get("/api/v1/townlight_parks/maintenance-triage/{triage_id}")
def get_maintenance_triage(triage_id: str) -> dict[str, object]:
    if _workpaper_database_url() is None:
        raise HTTPException(status_code=503, detail={"message":"Townlight Parks workpaper persistence is not configured.","fix":"Set TOWNLIGHT_PARKS_WORKPAPER_DB_URL to retrieve persisted maintenance triage records."})
    stored = _get_workpaper_repository().get_maintenance(triage_id)
    if stored is None:
        raise HTTPException(status_code=404, detail={"message":"Maintenance triage record not found.","fix":"Use a triage_id returned by POST /api/v1/townlight_parks/maintenance-triage."})
    return _stored_maintenance_response(stored)

def _workpaper_database_url() -> str | None:
    return os.environ.get("TOWNLIGHT_PARKS_WORKPAPER_DB_URL")

def _get_workpaper_repository() -> ParksWorkpaperRepository:
    global _workpaper_db_url, _workpaper_repository
    db_url = _workpaper_database_url()
    if db_url is None:
        raise RuntimeError("TOWNLIGHT_PARKS_WORKPAPER_DB_URL is not configured.")
    if _workpaper_repository is None or db_url != _workpaper_db_url:
        _dispose_workpaper_repository()
        _workpaper_db_url = db_url
        _workpaper_repository = ParksWorkpaperRepository(db_url=db_url)
    return _workpaper_repository

def _dispose_workpaper_repository() -> None:
    global _workpaper_repository
    if _workpaper_repository is not None:
        _workpaper_repository.engine.dispose()
        _workpaper_repository = None

def _stored_registration_response(stored: StoredRegistrationAssistance) -> dict[str, object]:
    return {**stored.__dict__, "created_at": stored.created_at.isoformat()}

def _stored_maintenance_response(stored: StoredMaintenanceTriage) -> dict[str, object]:
    return {**stored.__dict__, "created_at": stored.created_at.isoformat()}
