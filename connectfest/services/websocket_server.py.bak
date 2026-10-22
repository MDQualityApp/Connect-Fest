#!/usr/bin/env python3
"""
ConnectFest Real-time Stall WebSocket Gateway
Port: 8085 (ws://0.0.0.0:8085)
Subscribes to Redis 'connectfest:stall_updates' and broadcasts instant stall status changes
(Hold, Available, Booked, Blocked) to all connected frontends with ZERO delay and ZERO friction.
"""

import asyncio
import json
import os
import sys

# Remove script directory from sys.path[0] to prevent shadowing stdlib 'queue' module
script_dir = os.path.dirname(os.path.abspath(__file__))
while sys.path and (sys.path[0] == script_dir or sys.path[0] == ""):
	sys.path.pop(0)

import websockets
import redis.asyncio as aioredis

PORT = int(os.environ.get("CONNECTFEST_WS_PORT", 8085))
HOST = "0.0.0.0"
REDIS_URL = os.environ.get("REDIS_CACHE_URL", "redis://127.0.0.1:13080")
CHANNEL = "connectfest:stall_updates"

connected_clients = set()


async def client_handler(websocket):
	"""Handles individual client WebSocket connections."""
	connected_clients.add(websocket)
	client_ip = getattr(websocket, "remote_address", "client")
	print(f"[WS] Client connected from {client_ip}. Total active: {len(connected_clients)}")

	try:
		# 1. Send welcome & ready handshake
		handshake = {
			"type": "connection_ack",
			"event": "connected",
			"message": "Connected to ConnectFest Realtime Stall Gateway",
			"channel": CHANNEL,
			"active_clients": len(connected_clients),
		}
		await websocket.send(json.dumps(handshake))

		# 2. Listen for incoming client messages (e.g. heartbeat / ping)
		async for msg_str in websocket:
			try:
				if msg_str.strip().lower() in ("ping", '{"type":"ping"}'):
					await websocket.send(json.dumps({"type": "pong"}))
			except Exception:
				pass

	except websockets.exceptions.ConnectionClosed:
		pass
	except Exception as err:
		print(f"[WS] Client error: {err}")
	finally:
		connected_clients.discard(websocket)
		print(f"[WS] Client disconnected. Total active: {len(connected_clients)}")


async def redis_listener():
	"""Subscribes to Redis Pub/Sub and broadcasts messages to all WebSocket clients."""
	print(f"[WS] Connecting to Redis at {REDIS_URL} on channel '{CHANNEL}'...")
	while True:
		try:
			r = aioredis.from_url(REDIS_URL)
			pubsub = r.pubsub()
			await pubsub.subscribe(CHANNEL)
			print(f"[WS] Successfully subscribed to Redis channel '{CHANNEL}'. Ready to broadcast!")

			async for msg in pubsub.listen():
				if msg.get("type") == "message":
					data = msg.get("data")
					if isinstance(data, bytes):
						data = data.decode("utf-8")

					if connected_clients:
						# Broadcast concurrently to all connected clients
						targets = list(connected_clients)
						results = await asyncio.gather(
							*[c.send(data) for c in targets],
							return_exceptions=True
						)
						for client, res in zip(targets, results):
							if isinstance(res, Exception):
								connected_clients.discard(client)

		except Exception as e:
			print(f"[WS] Redis connection error: {e}. Reconnecting in 2s...")
			await asyncio.sleep(2)


async def main():
	print(f"=== ConnectFest Real-time WebSocket Gateway ===")
	print(f"Starting server on ws://{HOST}:{PORT}...")
	async with websockets.serve(client_handler, HOST, PORT):
		print(f"Server live and listening on ws://{HOST}:{PORT}")
		await redis_listener()


if __name__ == "__main__":
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print("\n[WS] Server stopped.")
