from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import (
    create_session,
    delete_all_sessions,
    delete_session,
    generate_token,
    is_password_set,
    require_auth,
    set_password,
    verify_current_password,
    verify_password,
)
from backend.db import get_connection

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LENGTH = 8


class AuthStatusOut(BaseModel):
    setup_required: bool


class TokenOut(BaseModel):
    token: str


class SetupIn(BaseModel):
    password: str


class LoginIn(BaseModel):
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


@router.get("/status", response_model=AuthStatusOut)
def get_auth_status():
    return AuthStatusOut(setup_required=not is_password_set())


@router.post("/setup", response_model=TokenOut)
def setup(body: SetupIn):
    if is_password_set():
        raise HTTPException(status_code=409, detail="Password already set")
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    set_password(body.password)
    token = generate_token()
    create_session(token)
    return TokenOut(token=token)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn):
    conn = get_connection()
    try:
        row = conn.execute("SELECT password_hash FROM auth_config WHERE id = 1").fetchone()
    finally:
        conn.close()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect password")
    token = generate_token()
    create_session(token)
    return TokenOut(token=token)


@router.post("/logout", status_code=204)
def logout(token: str = Depends(require_auth)):
    delete_session(token)


@router.post("/change-password", response_model=TokenOut)
def change_password(body: ChangePasswordIn, _token: str = Depends(require_auth)):
    if not verify_current_password(body.current_password):
        raise HTTPException(status_code=401, detail="Incorrect password")
    if len(body.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    set_password(body.new_password)
    delete_all_sessions()
    token = generate_token()
    create_session(token)
    return TokenOut(token=token)
