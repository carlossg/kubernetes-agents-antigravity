"""Trusted HTTP-to-Substrate CONNECT adapter; never runs inside the actor."""
import asyncio
import http.client
import socket
import ssl

from fastapi import FastAPI, HTTPException, Request, Response

ROUTER = "atenet-router.ate-system.svc"
ACTOR = "kubernetes-agent-logs"
ATESPACE = "argo-rollouts"
MAX_BYTES = 128_000
app = FastAPI()


class ActorConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__(ACTOR, 8080, timeout=90)

    def connect(self):
        # Reload projected credentials on every request, including after kubelet rotation.
        context = ssl.create_default_context(cafile="/var/run/substrate/router-ca.pem")
        context.load_cert_chain("/var/run/substrate/credential-bundle.pem")
        raw = socket.create_connection((ROUTER, 8444), timeout=self.timeout)
        try:
            self.sock = context.wrap_socket(raw, server_hostname=ROUTER)
            self.sock.sendall(
                f"CONNECT {ACTOR}:8080 HTTP/1.1\r\nHost: {ACTOR}:8080\r\n"
                f"ate-target-actor: {ATESPACE}/{ACTOR}\r\n\r\n".encode("ascii")
            )
            response = http.client.HTTPResponse(self.sock, method="CONNECT")
            response.begin()
            status = response.status
            response.close()
            if not 200 <= status < 300:
                raise OSError(f"Substrate CONNECT refused: {status}")
        except Exception:
            raw.close()
            self.close()
            raise


def forward(method: str, path: str, body: bytes) -> Response:
    connection = ActorConnection()
    try:
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise OSError("Substrate response exceeds the collector response limit")
        return Response(data, status_code=response.status, media_type="application/json")
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(status_code=502, detail="Substrate log collector unavailable") from exc
    finally:
        connection.close()


@app.get("/")
def ready():
    return forward("GET", "/", b"")


@app.post("/analyze/logs")
async def collect(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise HTTPException(status_code=413, detail="Collector request too large")
    return await asyncio.to_thread(forward, "POST", "/analyze/logs", bytes(body))
