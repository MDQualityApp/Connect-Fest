#!/usr/bin/env python3
"""
ConnectFest Real-time Stall WebSocket Gateway
Port: 8085 (ws://0.0.0.0:8085)
Subscribes to Redis 'connectfest:stall_updates' and broadcasts instant stall status changes
(Hold, Available, Booked, Blocked, Selected, Unselected) to all connected frontends with ZERO delay.
Also handles client-side real-time stall selection so when User 1 selects Stall 1,
all other connected users see it immediately.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime

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
# In-memory tracking of active peer selections: stall_number -> selection_dict
active_selections = {}
# Mapping websocket -> set of stall_numbers selected by this client
client_selections = {}
redis_pub_client = None


async def get_redis_pub():
	global redis_pub_client
	if redis_pub_client is None:
		try:
			redis_pub_client = aioredis.from_url(REDIS_URL)
		except Exception as e:
			print(f"[WS] Failed to connect to Redis publisher: {e}")
	return redis_pub_client


async def broadcast_to_clients(data_str, exclude_ws=None):
	"""Broadcasts a JSON string to all connected WebSocket clients (optionally excluding sender)."""
	if not connected_clients:
		return
	targets = [c for c in connected_clients if c != exclude_ws]
	if not targets:
		return
	results = await asyncio.gather(
		*[c.send(data_str) for c in targets],
		return_exceptions=True
	)
	for client, res in zip(targets, results):
		if isinstance(res, Exception):
			connected_clients.discard(client)
			client_selections.pop(client, None)


async def client_handler(websocket):
	"""Handles individual client WebSocket connections."""
	connected_clients.add(websocket)
	client_selections[websocket] = set()
	client_ip = getattr(websocket, "remote_address", "client")
	print(f"[WS] Client connected from {client_ip}. Total active: {len(connected_clients)}")

	try:
		# 1. Send welcome & ready handshake including current active selections
		handshake = {
			"type": "connection_ack",
			"event": "connected",
			"message": "Connected to ConnectFest Realtime Stall Gateway",
			"channel": CHANNEL,
			"active_clients": len(connected_clients),
			"active_selections": active_selections,
			"server_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
		}
		await websocket.send(json.dumps(handshake))

		# 2. Listen for incoming client messages
		async for msg_str in websocket:
			try:
				raw_str = msg_str.strip()
				if raw_str.lower() in ("ping", '{"type":"ping"}'):
					await websocket.send(json.dumps({"type": "pong"}))
					continue

				data = json.loads(raw_str)
				action = data.get("action") or data.get("type") or data.get("event")
				stall_num = (data.get("stall_number") or data.get("stall_no") or data.get("stall") or "").strip()

				# A. Stall Selection (User clicked / picked a stall)
				if action in ("select_stall", "stall_selected", "stall_selecting") and stall_num:
					user_id = str(data.get("user_id") or "user").strip()
					user_name = str(data.get("user_name") or data.get("full_name") or "User").strip()
					timestamp = data.get("timestamp") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

					selection_payload = {
						"event": "stall_selected",
						"stall_number": stall_num,
						"user_id": user_id,
						"user_name": user_name,
						"timestamp": timestamp,
					}

					# Track selection
					active_selections[stall_num] = selection_payload
					client_selections[websocket].add(stall_num)

					# Broadcast immediately to peers
					payload_str = json.dumps(selection_payload)
					await broadcast_to_clients(payload_str, exclude_ws=websocket)

					# Also publish to Redis for other worker processes
					try:
						pub = await get_redis_pub()
						if pub:
							await pub.publish(CHANNEL, payload_str)
					except Exception:
						pass

				# B. Stall Deselection (User unpicked or cancelled)
				elif action in ("deselect_stall", "stall_unselected", "stall_unselecting") and stall_num:
					user_id = str(data.get("user_id") or "").strip()
					active_selections.pop(stall_num, None)
					client_selections[websocket].discard(stall_num)

					deselection_payload = {
						"event": "stall_unselected",
						"stall_number": stall_num,
						"user_id": user_id,
						"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
					}
					payload_str = json.dumps(deselection_payload)
					await broadcast_to_clients(payload_str, exclude_ws=websocket)

					try:
						pub = await get_redis_pub()
						if pub:
							await pub.publish(CHANNEL, payload_str)
					except Exception:
						pass

				# C. Request Active Selections Refresh
				elif action in ("get_selections", "get_active_selections"):
					await websocket.send(json.dumps({
						"event": "active_selections_sync",
						"active_selections": active_selections,
					}))

			except Exception as parse_err:
				pass

	except websockets.exceptions.ConnectionClosed:
		pass
	except Exception as err:
		print(f"[WS] Client error: {err}")
	finally:
		# Auto-cleanup any selections made by this disconnected client
		stalls_to_release = client_selections.pop(websocket, set())
		connected_clients.discard(websocket)
		print(f"[WS] Client disconnected. Total active: {len(connected_clients)}")

		for s_num in stalls_to_release:
			active_selections.pop(s_num, None)
			cleanup_payload = {
				"event": "stall_unselected",
				"stall_number": s_num,
				"reason": "client_disconnected",
				"timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
			}
			payload_str = json.dumps(cleanup_payload)
			try:
				asyncio.create_task(broadcast_to_clients(payload_str))
				pub = await get_redis_pub()
				if pub:
					asyncio.create_task(pub.publish(CHANNEL, payload_str))
			except Exception:
				pass


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

					# Clean up active_selections if stall was held, booked, or released
					try:
						parsed = json.loads(data)
						s_num = parsed.get("stall_number")
						s_event = parsed.get("event")
						s_status = parsed.get("status")

						if s_num and (s_status in ("Hold", "Booked", "Blocked", "Available") or s_event in ("stall_held", "stall_booked", "stall_blocked", "stall_released")):
							active_selections.pop(s_num, None)
						elif s_num and s_event == "stall_selected":
							active_selections[s_num] = parsed
						elif s_num and s_event == "stall_unselected":
							active_selections.pop(s_num, None)
					except Exception:
						pass

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
								client_selections.pop(client, None)

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
