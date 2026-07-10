import asyncio
import websockets
import json

async def test():
    async with websockets.connect('ws://127.0.0.1:8000/ws/upload') as ws:
        await ws.send(json.dumps({"target": "ota", "value": "192.168.0.125"}))
        while True:
            try:
                msg = await ws.recv()
                print(msg)
            except websockets.exceptions.ConnectionClosed:
                print("Closed")
                break

asyncio.run(test())
