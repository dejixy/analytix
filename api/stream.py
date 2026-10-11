"""
WebSocket push: browsers connect to /ws?coin=BTC and receive a full snapshot
on connect, then every BROADCAST_INTERVAL_S. A client can switch coin without
reconnecting by sending {"coin": "SOL"}.
"""
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def stream(ws: WebSocket):
    rt = ws.app.state.runtime
    coin = ws.query_params.get("coin")
    coin = coin if coin and rt.open_coin(coin) else rt.default_coin
    await ws.accept()
    await ws.send_text(json.dumps(rt.snapshot(coin), separators=(",", ":")))
    rt.clients[ws] = coin
    try:
        while True:
            msg = await ws.receive_text()
            try:
                wanted = json.loads(msg).get("coin")
            except (ValueError, AttributeError):
                continue
            if wanted and rt.open_coin(wanted):
                rt.clients[ws] = wanted
                await ws.send_text(json.dumps(rt.snapshot(wanted), separators=(",", ":")))
    except WebSocketDisconnect:
        pass
    finally:
        rt.clients.pop(ws, None)
