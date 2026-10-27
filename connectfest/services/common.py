import json
import time
import functools
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
	ym = now_datetime().strftime("%y%m")
	for _ in range(100):
		try:
			seq = cache.incr(cache.make_key(STALL_COUNTER_KEY))
		except Exception:
			seq = int(time.time() * 1000) % 1000000
		candidate = f"CF-{ym}-{seq:06d}"
		if not frappe.db.exists("ConnectFest Registration", {"registration_id": candidate}):
			return candidate
	return f"CF-{ym}-{int(time.time() * 1000) % 1000000:06d}"


def generate_visitor_id(cache):
	ym = now_datetime().strftime("%y%m")
	for _ in range(100):
		try:
			seq = cache.incr(cache.make_key(VISITOR_COUNTER_KEY))
		except Exception:
			seq = int(time.time() * 1000) % 1000000
		candidate = f"VIS-{ym}-{seq:06d}"
		if not frappe.db.exists("ConnectFest Visitor Registration", {"registration_id": candidate}):
			return candidate
	return f"VIS-{ym}-{int(time.time() * 1000) % 1000000:06d}"


def generate_security_pin():
	"""Generates a secure 6-digit access PIN for attendees to safeguard their dashboard."""
	import random
	return f"{random.randint(100000, 999999)}"


def mask_phone(phone):
	"""Masks phone number e.g. 9845123456 -> 98******56."""
	if not phone:
		return ""
	s = str(phone).strip()
	if len(s) >= 8:
		return s[:2] + ("*" * (len(s) - 4)) + s[-2:]
	return s[:1] + ("*" * max(1, len(s) - 2)) + s[-1:]


def mask_email(email):
	"""Masks email address e.g. manoj@apex.com -> m***j@apex.com."""
	if not email or "@" not in email:
		return email or ""
	user, domain = email.split("@", 1)
	if len(user) <= 2:
		masked_user = user[:1] + "*"
	else:
		masked_user = user[0] + ("*" * (len(user) - 2)) + user[-1]
	return f"{masked_user}@{domain}"


def check_rate_limit(action="lookup", limit=30, window_secs=60):
	"""
	Redis-based sliding window rate limiter against brute-force scanning.
	Returns True if allowed, False if exceeded.
	"""
	try:
		ip = getattr(frappe.local, "request_ip", None) or "127.0.0.1"
		cache = frappe.cache()
		key = cache.make_key(f"connectfest:ratelimit:{action}:{ip}")
		current = cache.incr(key)
		if current == 1:
			cache.expire(key, window_secs)
		if current > limit:
			return False
		return True
	except Exception:
		return True


def save_image_from_base64_or_url(image_data=None, file_name_prefix="logo", allowed_extensions=("jpg", "jpeg", "png", "webp", "gif", "svg")):
	"""
	Safely saves images from:
	1. Direct werkzeug FileStorage object, or checks frappe.request.files (multipart/form-data).
	2. Base64 data URLs as public files in Frappe and returns the file_url (/files/...).
	3. Pre-existing valid public URL or path, validates against XSS (no javascript:) and returns sanitized URL.
	"""
	import base64
	import uuid
	from frappe.utils.file_manager import save_file

	# 1. Check if image_data is a FileStorage object or check frappe.request.files
	uploaded_file = None
	if hasattr(image_data, "read") and hasattr(image_data, "filename"):
		uploaded_file = image_data
	elif getattr(frappe, "request", None) and getattr(frappe.request, "files", None):
		for key in ("visitor_image", "file", "image", "photo", "company_logo", "logo", "filedata", "avatar"):
			if key in frappe.request.files:
				f = frappe.request.files.get(key)
				if f and getattr(f, "filename", ""):
					uploaded_file = f
					break

	if uploaded_file:
		try:
			raw_bytes = uploaded_file.read()
			if raw_bytes and len(raw_bytes) <= 10 * 1024 * 1024:
				orig_name = getattr(uploaded_file, "filename", "") or ""
				ext = orig_name.split(".")[-1].lower() if "." in orig_name else "jpg"
				if ext not in allowed_extensions:
					ext = "jpg"
				fname = f"{file_name_prefix}_{uuid.uuid4().hex[:8]}.{ext}"
				saved = save_file(fname, raw_bytes, dt=None, dn=None, is_private=0)
				return saved.file_url
		except Exception as e:
			frappe.log_error("Uploaded Image Save Error", frappe.get_traceback())
			return ""

	if not image_data or not isinstance(image_data, str):
		return ""
	val = image_data.strip()
	if not val:
		return ""

	lower_val = val.lower()
	if lower_val.startswith("javascript:") or lower_val.startswith("vbscript:") or "<script" in lower_val:
		return ""

	if val.startswith("data:image/"):
		try:
			header, encoded = val.split(",", 1)
			ext = "png"
			if "jpeg" in header or "jpg" in header:
				ext = "jpg"
			elif "webp" in header:
				ext = "webp"
			elif "svg" in header:
				ext = "svg"
			elif "gif" in header:
				ext = "gif"
			raw_bytes = base64.b64decode(encoded)
			if len(raw_bytes) > 10 * 1024 * 1024:
				return ""
			fname = f"{file_name_prefix}_{uuid.uuid4().hex[:8]}.{ext}"
			saved = save_file(fname, raw_bytes, dt=None, dn=None, is_private=0)
			return saved.file_url
		except Exception as e:
			frappe.log_error("Image Save Error", frappe.get_traceback())
			return ""

	if len(val) > 500:
		return ""
	return val


def handle_api_errors(title_or_fn="ConnectFest Error"):
	"""
	Decorator that catches any unhandled exceptions in API service functions.
	- Logs the complete Python traceback to Frappe's Error Log (`tabError Log`) so admins can view it in desk.
	- Sets HTTP status code to 500.
	- Returns a safe, user-friendly JSON error response:
	  {"status": "error", "message": "Something went wrong. Please try again or contact event support."}
	- Prevents leaking internal database errors or stack traces to end users.
	Can be used as @handle_api_errors or @handle_api_errors("Custom Title")
	"""
	if callable(title_or_fn):
		fn = title_or_fn
		title = f"ConnectFest API Error: {fn.__name__}"

		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			try:
				return fn(*args, **kwargs)
			except Exception:
				try:
					frappe.db.rollback()
				except Exception:
					pass
				frappe.log_error(title=title, message=frappe.get_traceback())
				if hasattr(frappe, "local") and hasattr(frappe.local, "response"):
					frappe.local.response["http_status_code"] = 500
				return {
					"status": "error",
					"message": "Something went wrong. Please try again or contact event support.",
				}

		return wrapper

	title = title_or_fn or "ConnectFest Error"

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			try:
				return fn(*args, **kwargs)
			except Exception:
				try:
					frappe.db.rollback()
				except Exception:
					pass
				frappe.log_error(title=title, message=frappe.get_traceback())
				if hasattr(frappe, "local") and hasattr(frappe.local, "response"):
					frappe.local.response["http_status_code"] = 500
				return {
					"status": "error",
					"message": "Something went wrong. Please try again or contact event support.",
				}

		return wrapper

	return decorator


def get_base_url():
	"""
	Dynamically resolves the environment base URL:
	- When executed on Local (localhost / 127.0.0.1 / connectfest.localhost), returns Local URL (e.g. http://localhost:8080).
	- When executed on Live (connectfestapp.mdqapps.online or custom domain), returns Live HTTPS URL.
	"""
	# 1. Check HTTP Request context
	if hasattr(frappe, "request") and getattr(frappe, "request", None):
		host = (frappe.request.headers.get("Host") or getattr(frappe.request, "host", "") or "").strip()
		if host:
			if "localhost" in host or "127.0.0.1" in host:
				return f"http://{host}".rstrip("/")
			elif "mdqapps.online" in host:
				return f"https://{host}".rstrip("/")
			else:
				scheme = getattr(frappe.request, "scheme", "https")
				return f"{scheme}://{host}".rstrip("/")

	# 2. Check site config / frappe get_url()
	try:
		site_url = (frappe.utils.get_url() or "").strip().rstrip("/")
		if site_url:
			if "localhost" in site_url or "127.0.0.1" in site_url:
				return "http://localhost:8080"
			elif "mdqapps.online" in site_url:
				return "https://connectfestapp.mdqapps.online"
			elif site_url.startswith("http"):
				return site_url
	except Exception:
		pass

	# 3. Fallback based on local site
	site_name = getattr(frappe.local, "site", "") or ""
	if "localhost" in site_name or "local" in site_name:
		return "http://localhost:8080"

	return "https://connectfestapp.mdqapps.online"


def check_expired_holds_lazy():
	"""
	Lightweight lazy safety net:
	Every 10 seconds, checks and releases expired stall holds across incoming traffic,
	ensuring zero stalls stay stuck in 'Hold' even if background workers or crons are paused.
	"""
	try:
		cache = frappe.cache()
		if cache.set(cache.make_key("cf_hold_cleanup_throttle"), "1", nx=True, ex=10):
			from connectfest.services.stalls import release_expired_holds
			release_expired_holds()
	except Exception:
		pass


def before_request():
	"""
	Runtime hook executed before processing any HTTP request.
	Ensures CORS is always enabled for ConnectFest APIs and prevents CSRF blocking on REST APIs,
	even if site_config.json does not have explicit flags.
	Also performs lightweight background cleanup of expired holds.
	"""
	frappe.local.allow_cors = "*"
	if hasattr(frappe.local, "conf") and frappe.local.conf:
		frappe.local.conf.allow_cors = "*"
		req = getattr(frappe, "request", None)
		if req and hasattr(req, "path") and req.path:
			if req.path.startswith("/api/method/connectfest") or req.path.startswith("/api/v2"):
				frappe.local.conf.ignore_csrf = True

	check_expired_holds_lazy()



