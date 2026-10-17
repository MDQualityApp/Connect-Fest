import json
import time
import frappe
from frappe.utils import now_datetime, cint, flt

# Redis Queue & Cache Keys
STALL_QUEUE_KEY = "connectfest:stall_queue"
VISITOR_QUEUE_KEY = "connectfest:visitor_queue"
STALL_CACHE_PREFIX = "connectfest:stall:"
VISITOR_CACHE_PREFIX = "connectfest:visitor:"
STALL_COUNTER_KEY = "connectfest:stall_counter"
VISITOR_COUNTER_KEY = "connectfest:visitor_counter"
STALL_LOCK_PREFIX = "connectfest:stall_lock:"
BATCH_CHUNK_SIZE = 500

VALID_SOURCES = [
	"Social Media",
	"BNI Chapter",
	"Friends / Family",
	"WhatsApp",
	"Email Campaign",
	"Website",
	"Word of Mouth",
	"Other",
]


def parse_boolean(val):
	if isinstance(val, bool):
		return 1 if val else 0
	s = str(val).strip().lower()
	if s in ("yes", "y", "true", "1"):
		return 1
	return 0


def extract_request_data(kwargs):
	data = {}
	if frappe.request and getattr(frappe.request, "data", None):
		try:
			data = json.loads(frappe.request.data.decode("utf-8"))
		except Exception:
			data = {}
	data.update(frappe.form_dict or {})
	data.update(kwargs)
	return data


def get_cached_record(cache, key):
	"""Safely fetch and deserialize a JSON cached record using make_key."""
	try:
		raw = cache.get(cache.make_key(key))
		if raw:
			if isinstance(raw, bytes):
				raw = raw.decode("utf-8")
			return json.loads(raw)
	except Exception:
		pass
	return None


def set_cached_record(cache, key, data, ex=86400):
	"""Safely serialize and cache a JSON record using make_key."""
	try:
		val = json.dumps(data, default=str) if isinstance(data, (dict, list)) else data
		cache.setex(cache.make_key(key), ex, val)
	except Exception:
		pass


def generate_stall_id(cache):
	try:
		seq = cache.incr(cache.make_key(STALL_COUNTER_KEY))
	except Exception:
		seq = int(time.time() * 1000) % 1000000
	ym = now_datetime().strftime("%y%m")
	return f"CF-{ym}-{seq:06d}"


def generate_visitor_id(cache):
	try:
		seq = cache.incr(cache.make_key(VISITOR_COUNTER_KEY))
	except Exception:
		seq = int(time.time() * 1000) % 1000000
	ym = now_datetime().strftime("%y%m")
	return f"VIS-{ym}-{seq:06d}"
