import json
import frappe
from frappe.utils import now_datetime, flt
from frappe.utils.background_jobs import get_redis_connection_without_auth

STALL_UPDATE_CHANNEL = "connectfest:stall_updates"
RECENT_UPDATES_KEY = "connectfest:recent_stall_updates"


def broadcast_stall_update(stall_number, status, extra_data=None):
	"""
	Broadcasts real-time stall status change across:
	1. Native WebSocket server (via Redis Pub/Sub channel 'connectfest:stall_updates')
	2. Frappe Socket.IO server (via frappe.publish_realtime)
	3. Redis recent updates cache (for fast recovery / reconnection)

	Payload format for Frontend:
	{
	    "event": "stall_held" | "stall_released" | "stall_booked" | "stall_blocked",
	    "stall_number": "A-01",
	    "status": "Hold" | "Available" | "Booked" | "Blocked",
	    "tier": "Standard",
	    "hold_expires_at": "...",
	    "booked_by_name": "...",
	    "booked_by_company": "...",
	    "timestamp": "2026-10-22 15:30:00"
	}
	"""
	if not stall_number:
		return

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

	# Event mapping
	status_event_map = {
		"Hold": "stall_held",
		"Available": "stall_released",
		"Booked": "stall_booked",
		"Blocked": "stall_blocked",
	}
	event_name = status_event_map.get(status, "stall_updated")

	tier = ""
	price = 0.0
	dimensions = ""
	if frappe.db.exists("ConnectFest Stall", stall_number):
		s_data = frappe.db.get_value("ConnectFest Stall", stall_number, ["tier", "price", "dimensions"], as_dict=True) or {}
		tier = s_data.get("tier") or ""
		price = flt(s_data.get("price"))
		dimensions = s_data.get("dimensions") or ""

	payload = {
		"event": event_name,
		"stall_number": stall_number,
		"status": status,
		"tier": tier,
		"price": price,
		"dimensions": dimensions,
		"timestamp": now_str,
	}

	if extra_data and isinstance(extra_data, dict):
		payload.update(extra_data)

	json_payload = json.dumps(payload)

	# 1. Publish to Redis Pub/Sub for Native WebSocket Server
	try:
		cache = frappe.cache()
		cache.publish(STALL_UPDATE_CHANNEL, json_payload)
		cache.lpush(RECENT_UPDATES_KEY, json_payload)
		cache.ltrim(RECENT_UPDATES_KEY, 0, 49)
	except Exception as redis_err:
		frappe.log_error("Broadcast Stall Redis Cache Error", str(redis_err))

	try:
		r = get_redis_connection_without_auth()
		r.publish(STALL_UPDATE_CHANNEL, json_payload)
	except Exception:
		pass

	# 2. Publish to Frappe Realtime / Socket.IO
	try:
		from frappe.realtime import publish_realtime
		publish_realtime(
			event="connectfest_stall_update",
			message=payload,
			room="all",
			after_commit=False,
		)
	except Exception as realtime_err:
		frappe.log_error("Broadcast Stall Realtime Error", str(realtime_err))

	return payload


@frappe.whitelist(allow_guest=True)
def get_live_stall_updates(limit=50, since=None):
	"""
	Lightweight API for frontends to retrieve recent real-time stall updates.
	Enables instant sync if a client reconnects or loads the page.
	"""
	limit = min(int(limit or 50), 100)
	updates = []

	try:
		cache = frappe.cache()
		raw_items = cache.lrange(RECENT_UPDATES_KEY, 0, limit - 1)
		for item in raw_items:
			try:
				if isinstance(item, bytes):
					item = item.decode("utf-8")
				parsed = json.loads(item)
				if since and parsed.get("timestamp") and parsed["timestamp"] <= str(since):
					continue
				updates.append(parsed)
			except Exception:
				continue
	except Exception:
		pass

	# Also return current active holds
	active_holds = []
	try:
		now_dt = now_datetime()
		holds = frappe.db.sql("""
			SELECT stall_number, tier, status, hold_expires_at
			FROM `tabConnectFest Stall`
			WHERE status = 'Hold' AND hold_expires_at > %s
		""", (now_dt,), as_dict=True)
		for h in holds:
			active_holds.append({
				"stall_number": h["stall_number"],
				"status": "Hold",
				"tier": h.get("tier"),
				"hold_expires_at": str(h.get("hold_expires_at")),
			})
	except Exception:
		pass

	return {
		"status": "success",
		"timestamp": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
		"active_holds": active_holds,
		"recent_updates": updates,
	}
