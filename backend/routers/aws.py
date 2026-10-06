"""AWS CLI connection — per-user encrypted credentials, STS-checked."""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from crypto_utils import encrypt_api_key
from database import delete_user_api_key, set_user_api_key
from services.aws_cli import (
    AWS_REGIONS,
    _load_creds,
    check_connect_fields,
    mask_access_key_id,
    run_aws_cli,
    sts_identity,
)

router = APIRouter()


class ConnectBody(BaseModel):
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"
    session_token: str = ""


class ExecBody(BaseModel):
    command: str = Field(default="")


def _user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", "") or ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id


@router.post("/connect")
def aws_connect(body: ConnectBody, request: Request):
    user_id = _user_id(request)
    try:
        fields = check_connect_fields(
            body.access_key_id, body.secret_access_key, body.region, body.session_token,
        )
        ident = sts_identity(
            fields["access_key_id"], fields["secret_access_key"],
            fields["region"], fields["session_token"],
        )
    except ValueError as exc:
        msg = str(exc)
        status = 401 if "rejected by AWS" in msg else 400
        raise HTTPException(status_code=status, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not reach AWS.") from exc
    set_user_api_key(user_id, "aws", encrypt_api_key(json.dumps(fields)))
    return {
        "ok": True,
        "account": ident["account"],
        "arn": ident["arn"],
        "region": fields["region"],
    }


@router.get("/status")
def aws_status(request: Request):
    user_id = _user_id(request)
    try:
        creds = _load_creds(user_id)
    except Exception:
        return {"connected": False}
    if not creds:
        return {"connected": False}
    try:
        ident = sts_identity(
            creds.get("access_key_id") or "",
            creds.get("secret_access_key") or "",
            creds.get("region") or "us-east-1",
            creds.get("session_token") or "",
        )
    except Exception:
        return {"connected": False}
    return {
        "connected": True,
        "account": ident["account"],
        "arn": ident["arn"],
        "region": creds.get("region") or "us-east-1",
        "access_key_id": mask_access_key_id(creds.get("access_key_id") or ""),
        "regions": list(AWS_REGIONS),
    }


@router.delete("/disconnect")
def aws_disconnect(request: Request):
    delete_user_api_key(_user_id(request), "aws")
    return {"ok": True}


@router.post("/exec")
def aws_exec(body: ExecBody, request: Request):
    return run_aws_cli(_user_id(request), body.command)
