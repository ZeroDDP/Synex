import hashlib
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.core.errors import register_error_handlers
from app.external.ipc import HostError
from app.external.packages import Previews
from app.external.routes import router


async def test_preview_one_use_and_cleanup():
    class Host:
        async def request(self, method, params):
            if method == "list":
                return {"plugins": []}
            path = Path(params["path"])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if method == "inspect":
                return {"sha256": digest, "manifest": {"id": "local.test"}}
            if digest != params["sha256"]:
                raise HostError("hash_mismatch", "Package changed")
            return {"state": "disabled"}

    app = FastAPI()
    app.include_router(router)
    register_error_handlers(app)
    app.state.plugin_host = Host()
    previews = app.state.previews = Previews()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test/external-plugins/"
        ) as client:
            for tampered in (False, True):
                response = await client.post("inspect", files={"file": ("x.zip", b"abc")})
                assert response.status_code == 200, response.text
                token = response.json()["preview_id"]
                path = previews.items[token][0]
                if tampered:
                    path.write_bytes(b"changed")
                confirm = {"preview_id": token}
                response = await client.post("install", json=confirm)
                assert response.status_code == (422 if tampered else 201)
                assert not path.exists()
                assert (await client.post("install", json=confirm)).status_code == 410
            response = await client.post(
                "inspect", files={"file": ("a", b"x"), "extra": ("b", b"y")}
            )
            assert response.status_code == 422
    finally:
        await previews.close()
