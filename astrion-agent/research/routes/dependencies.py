from contextlib import contextmanager
import sqlite3
from fastapi import HTTPException


@contextmanager
def database(request):
    store=request.app.state.store_factory()
    try: yield store
    except (ValueError, sqlite3.IntegrityError, UnicodeError) as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    finally: store.close()
