from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..util import hash_password, verify_password
from ..web import form, redirect, render

router = APIRouter()


@router.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    if db.query(User.id).first():
        return redirect("/login")
    return render(request, "setup.html")


@router.post("/setup")
async def setup_submit(request: Request, db: Session = Depends(get_db)):
    if db.query(User.id).first():
        return redirect("/login")
    f = await form(request)
    username = (f.get("username") or "").strip()
    pw = f.get("password") or ""
    if len(username) < 2 or len(pw) < 8 or pw != f.get("password2"):
        return render(request, "setup.html", {"error": "Username ≥ 2 characters, password ≥ 8 characters, and both passwords must match.", "username": username})
    db.add(User(username=username, password_hash=hash_password(pw), is_admin=True))
    db.commit()
    request.session["user"] = username
    return redirect("/settings/ebay", request, "Welcome! Next, connect your eBay developer keys.")


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if not db.query(User.id).first():
        return redirect("/setup")
    return render(request, "login.html", {"next": request.query_params.get("next", "/")})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    f = await form(request)
    u = db.query(User).filter_by(username=(f.get("username") or "").strip()).first()
    nxt = f.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if not u or not verify_password(f.get("password") or "", u.password_hash):
        return render(request, "login.html", {"error": "Invalid username or password.", "next": nxt}, 401)
    request.session["user"] = u.username
    return redirect(nxt)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")
