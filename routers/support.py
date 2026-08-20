"""
routers/support.py — Public support board.

  GET  /support                 — list all queries (public, no login needed)
  GET  /support/new             — ask a question (doctor login required)
  POST /support/new             — create a query (doctor login required)
  GET  /support/{query_id}      — thread view (public, no login needed)
  POST /support/{query_id}/reply — reply, or (admin only) post the official
                                    answer and close the query

Any doctor can post a query and it's visible to everyone immediately,
including logged-out visitors. Other doctors can reply while it's open.
Only an admin doctor (Doctor.is_admin) can post the official answer, which
locks the query — the whole thread, including that answer, stays visible
forever after.
"""
from datetime import datetime

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Doctor, SupportQuery, SupportReply
from services.auth_service import get_current_doctor, decode_token

router = APIRouter(tags=["support"])
templates = Jinja2Templates(directory="templates")


def get_current_doctor_optional(request: Request, db: Session = Depends(get_db)):
    """Like get_current_doctor, but returns None instead of raising 401 —
    the support board is public, so an anonymous visitor is a normal case,
    not an error."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    doctor = db.query(Doctor).filter(Doctor.id == payload.get("doctor_id")).first()
    if not doctor or not doctor.is_active:
        return None
    return doctor


@router.get("/support", response_class=HTMLResponse)
def support_list(request: Request, viewer: Doctor = Depends(get_current_doctor_optional), db: Session = Depends(get_db)):
    queries = (
        db.query(SupportQuery)
        # "open" sorts before "closed" descending (o > c) — open questions
        # are the ones that need eyes, so they belong at the top.
        .order_by(SupportQuery.status.desc(), SupportQuery.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(request, "support_list.html", {
        "queries": queries,
        "viewer": viewer,
    })


@router.get("/support/new", response_class=HTMLResponse)
def support_new_form(request: Request, doctor: Doctor = Depends(get_current_doctor)):
    return templates.TemplateResponse(request, "support_new.html", {
        "viewer": doctor,
        "error": None,
    })


@router.post("/support/new", response_class=HTMLResponse)
def support_new_submit(
    request: Request,
    title: str = Form(...),
    body: str = Form(...),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    title = title.strip()[:200]
    body = body.strip()[:5000]
    if not title or not body:
        return templates.TemplateResponse(request, "support_new.html", {
            "viewer": doctor,
            "error": "Please fill in both the title and your question.",
            "title": title,
            "body": body,
        }, status_code=400)

    query = SupportQuery(doctor_id=doctor.id, title=title, body=body)
    db.add(query)
    db.commit()
    db.refresh(query)
    return RedirectResponse(url=f"/support/{query.id}", status_code=303)


@router.get("/support/{query_id}", response_class=HTMLResponse)
def support_detail(
    query_id: int,
    request: Request,
    viewer: Doctor = Depends(get_current_doctor_optional),
    db: Session = Depends(get_db),
):
    query = db.query(SupportQuery).filter(SupportQuery.id == query_id).first()
    if not query:
        return templates.TemplateResponse(request, "support_detail.html", {
            "query": None, "viewer": viewer,
        }, status_code=404)
    return templates.TemplateResponse(request, "support_detail.html", {
        "query": query,
        "viewer": viewer,
    })


@router.post("/support/{query_id}/reply", response_class=HTMLResponse)
def support_reply(
    query_id: int,
    request: Request,
    body: str = Form(...),
    close_as_official: str = Form(default=""),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    query = db.query(SupportQuery).filter(SupportQuery.id == query_id).first()
    if not query or query.status == "closed":
        # Nothing sensible to do — bounce back to the thread, which will
        # just show it as closed / not found.
        return RedirectResponse(url=f"/support/{query_id}", status_code=303)

    body = body.strip()[:5000]
    if not body:
        return RedirectResponse(url=f"/support/{query_id}", status_code=303)

    # Never trust the client for this — only an admin account can actually
    # post the official, query-closing answer, regardless of what the form
    # submitted.
    is_official = bool(close_as_official) and doctor.is_admin

    reply = SupportReply(query_id=query.id, doctor_id=doctor.id, body=body, is_official=is_official)
    db.add(reply)
    if is_official:
        query.status = "closed"
        query.closed_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/support/{query_id}", status_code=303)
