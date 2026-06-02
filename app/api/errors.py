"""Map domain errors to JSON HTTP responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.errors import LedgerError


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerError)
    async def _handle_ledger_error(_: Request, exc: LedgerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
