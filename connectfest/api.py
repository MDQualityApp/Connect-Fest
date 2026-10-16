import hmac
import hashlib
import json
import time
import frappe
from frappe import _
from frappe.utils import now_datetime, cint, flt
import razorpay

# Redis Queue & Cache Keys
STALL_QUEUE_KEY = "connectfest:stall_queue"
VISITOR_QUEUE_KEY = "connectfest:visitor_queue"
STALL_CACHE_PREFIX = "connectfest:stall:"
VISITOR_CACHE_PREFIX = "connectfest:visitor:"
STALL_COUNTER_KEY = "connectfest:stall_counter"
VISITOR_COUNTER_KEY = "connectfest:visitor_counter"
BATCH_CHUNK_SIZE = 500

STALL_PRICING = {
	"Regular": 25000.0,
	"Standard": 35000.0,
	"Premium": 45000.0,
	"Silver": 100000.0,
}

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


def get_razorpay_config():
	"""Fetch Razorpay settings from DocType or site configuration."""
	key_id = None
	key_secret = None
	mock_mode = True

	try:
		settings = frappe.get_single("ConnectFest Settings")
		key_id = settings.razorpay_key_id
		key_secret = settings.get_password("razorpay_key_secret", raise_exception=False)
		mock_mode = bool(cint(settings.mock_payments))
	except Exception:
		pass

	# Site config fallback
	if not key_id:
		key_id = frappe.conf.get("razorpay_key_id")
	if not key_secret:
		key_secret = frappe.conf.get("razorpay_key_secret")

	# If keys exist and mock_mode not explicitly forced, use live/test keys
	if key_id and key_secret:
		mock_mode = False

	return {
		"key_id": key_id or "rzp_test_ConnectFestMockKey",
		"key_secret": key_secret or "mock_secret_ConnectFest123",
		"mock_mode": mock_mode,
	}


# ==============================================================================
# 1. STALL REGISTRATION API
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def register_stall(**kwargs):
	"""
	Stall Registration API (Optimized for 1,000+ RPS).
	Pricing Tiers:
	- Regular:  ₹25,000
	- Standard: ₹35,000
	- Premium:  ₹45,000
	- Silver:   ₹1,00,000
	"""
	data = extract_request_data(kwargs)

	full_name = (data.get("full_name") or data.get("name") or "").strip()
	company_name = (data.get("company_name") or "").strip()
	location = (data.get("location") or "").strip()
	is_bni_member = parse_boolean(data.get("is_bni_member", 0))

	region_name = (data.get("region_name") or "").strip()
	chapter_name = (data.get("chapter_name") or "").strip()
	industry = (data.get("industry") or "").strip()
	segment = (data.get("segment") or "").strip()

	raw_category = (data.get("stall_category") or data.get("category") or data.get("tier") or "Regular").strip().capitalize()
	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()
	mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()
	email = (data.get("email") or "").strip()
	sync_mode = parse_boolean(data.get("sync", 0))
	create_order = parse_boolean(data.get("create_order", 0))

	# Validations
	errors = []
	if not full_name:
		errors.append("Name (full_name) is required")
	if not company_name:
		errors.append("Company Name (company_name) is required")
	if not source_of_info:
		errors.append("How did you know about this event? (source_of_info) is required")

	if raw_category not in STALL_PRICING:
		valid_tiers = ", ".join(STALL_PRICING.keys())
		errors.append(f"Invalid stall_category '{raw_category}'. Allowed: {valid_tiers}")

	if is_bni_member:
		if not region_name:
			errors.append("Region Name (region_name) is required for BNI members")
		if not chapter_name:
			errors.append("Chapter Name (chapter_name) is required for BNI members")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	stall_category = raw_category
	amount = STALL_PRICING[stall_category]

	matched_source = None
	for src in VALID_SOURCES:
		if src.lower() == source_of_info.lower():
			matched_source = src
			break
	if not matched_source:
		matched_source = source_of_info if source_of_info in VALID_SOURCES else "Other"

	cache = frappe.cache()
	reg_id = generate_stall_id(cache)
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"company_name": company_name,
		"location": location,
		"mobile_no": mobile_no,
		"email": email,
		"is_bni_member": is_bni_member,
		"region_name": region_name if is_bni_member else "",
		"chapter_name": chapter_name if is_bni_member else "",
		"industry": industry,
		"segment": segment,
		"source_of_info": matched_source,
		"stall_category": stall_category,
		"amount": amount,
		"payment_status": "Pending",
		"razorpay_order_id": "",
		"razorpay_payment_id": "",
		"razorpay_signature": "",
		"payment_date": None,
		"status": "Registered",
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

	record_json = json.dumps(record)

	# Direct Synchronous Mode
	if sync_mode:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Registration", **record})
			doc.flags.ignore_permissions = True
			doc.insert()
			set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", record)
			response_data = {
				"status": "success",
				"type": "stall",
				"message": "Stall registration successful (direct)",
				"registration_id": reg_id,
				"data": record,
			}
			if create_order:
				order_info = create_razorpay_order(registration_id=reg_id)
				response_data["razorpay_order"] = order_info.get("order")
			return response_data
		except Exception as e:
			frappe.log_error("ConnectFest Stall Sync Error", str(e))
			frappe.local.response["http_status_code"] = 500
			return {"status": "error", "message": str(e)}

	# In-Memory Queue High-Throughput Ingestion (1,000+ RPS)
	try:
		raw_queue_key = cache.make_key(STALL_QUEUE_KEY)
		raw_cache_key = cache.make_key(f"{STALL_CACHE_PREFIX}{reg_id}")
		pipe = cache.pipeline()
		pipe.rpush(raw_queue_key, record_json)
		pipe.setex(raw_cache_key, 86400, record_json)
		results = pipe.execute()
		queue_len = results[0]

		if queue_len >= 50:
			trigger_async_flush()

		response_data = {
			"status": "success",
			"type": "stall",
			"message": "Stall registration received successfully! Please proceed to payment.",
			"registration_id": reg_id,
			"data": record,
		}

		if create_order:
			order_info = create_razorpay_order(registration_id=reg_id)
			response_data["razorpay_order"] = order_info.get("order")

		return response_data
	except Exception as e:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Registration", **record})
			doc.flags.ignore_permissions = True
			doc.insert()
			return {
				"status": "success",
				"type": "stall",
				"message": "Stall registration successful (fallback)",
				"registration_id": reg_id,
				"data": record,
			}
		except Exception as db_err:
			frappe.local.response["http_status_code"] = 500
			return {"status": "error", "message": str(db_err)}


@frappe.whitelist(allow_guest=True)
def register(**kwargs):
	"""Default registration alias pointing to register_stall."""
	return register_stall(**kwargs)


# ==============================================================================
# 2. RAZORPAY PAYMENT APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def create_razorpay_order(registration_id=None, stall_category=None, **kwargs):
	"""
	Creates a Razorpay Order for a stall registration based on its pricing tier.
	- Regular:  ₹25,000  (2500000 paise)
	- Standard: ₹35,000  (3500000 paise)
	- Premium:  ₹45,000  (4500000 paise)
	- Silver:   ₹1,00,000 (10000000 paise)
	"""
	data = extract_request_data(kwargs)
	reg_id = (registration_id or data.get("registration_id") or "").strip()
	new_category = (stall_category or data.get("stall_category") or "").strip().capitalize()

	if not reg_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	# Fetch registration details from cache or DB
	cache = frappe.cache()
	record = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")

	if not record:
		db_records = frappe.get_all(
			"ConnectFest Registration",
			filters={"registration_id": reg_id},
			fields=["*"],
			limit=1,
		)
		if db_records:
			record = db_records[0]

	if not record:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall registration '{reg_id}' not found"}

	# If category is updated
	if new_category and new_category in STALL_PRICING:
		record["stall_category"] = new_category
		record["amount"] = STALL_PRICING[new_category]

	category = record.get("stall_category") or "Regular"
	amount = flt(record.get("amount") or STALL_PRICING.get(category, 25000.0))
	amount_in_paise = int(amount * 100)

	cfg = get_razorpay_config()
	order_id = None

	if not cfg["mock_mode"]:
		try:
			client = razorpay.Client(auth=(cfg["key_id"], cfg["key_secret"]))
			order_data = {
				"amount": amount_in_paise,
				"currency": "INR",
				"receipt": reg_id,
				"notes": {
					"registration_id": reg_id,
					"stall_category": category,
					"full_name": record.get("full_name"),
					"company_name": record.get("company_name"),
				},
			}
			order = client.order.create(data=order_data)
			order_id = order.get("id")
		except Exception as e:
			frappe.log_error("Razorpay Order Creation Error", str(e))
			# Fallback to test order ID if live credentials have issues
			order_id = f"order_mock_{reg_id}_{int(time.time())}"
	else:
		order_id = f"order_mock_{reg_id}_{int(time.time())}"

	# Update order ID in record & cache
	record["razorpay_order_id"] = order_id
	set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", record)

	# Update in MariaDB if already persisted
	if frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		frappe.db.set_value(
			"ConnectFest Registration",
			{"registration_id": reg_id},
			{
				"razorpay_order_id": order_id,
				"stall_category": category,
				"amount": amount,
			},
			update_modified=False,
		)
		frappe.db.commit()

	return {
		"status": "success",
		"message": "Razorpay order created successfully",
		"registration_id": reg_id,
		"order": {
			"id": order_id,
			"entity": "order",
			"amount": amount_in_paise,
			"amount_due": amount_in_paise,
			"amount_paid": 0,
			"amount_in_rupees": amount,
			"currency": "INR",
			"receipt": reg_id,
			"status": "created",
			"stall_category": category,
			"key_id": cfg["key_id"],
			"mock_mode": cfg["mock_mode"],
		},
		"customer": {
			"name": record.get("full_name"),
			"email": record.get("email"),
			"contact": record.get("mobile_no"),
		},
	}


@frappe.whitelist(allow_guest=True)
def verify_razorpay_payment(**kwargs):
	"""
	Verifies Razorpay payment signature and marks the stall registration as Paid & Confirmed.
	Expects:
	- registration_id
	- razorpay_order_id
	- razorpay_payment_id
	- razorpay_signature
	"""
	data = extract_request_data(kwargs)

	reg_id = (data.get("registration_id") or "").strip()
	order_id = (data.get("razorpay_order_id") or "").strip()
	payment_id = (data.get("razorpay_payment_id") or "").strip()
	signature = (data.get("razorpay_signature") or "").strip()

	if not (reg_id and order_id and payment_id and signature):
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Missing required payment verification parameters: registration_id, razorpay_order_id, razorpay_payment_id, razorpay_signature",
		}

	cfg = get_razorpay_config()
	is_verified = False

	# Signature Verification
	if not cfg["mock_mode"]:
		try:
			client = razorpay.Client(auth=(cfg["key_id"], cfg["key_secret"]))
			client.utility.verify_payment_signature(
				{
					"razorpay_order_id": order_id,
					"razorpay_payment_id": payment_id,
					"razorpay_signature": signature,
				}
			)
			is_verified = True
		except Exception as e:
			frappe.log_error("Razorpay Signature Verification Failed", str(e))
			is_verified = False
	else:
		# In Mock/Test mode: verify signature against test secret or accept mock signature
		expected_mock_sig = hmac.new(
			cfg["key_secret"].encode(),
			f"{order_id}|{payment_id}".encode(),
			hashlib.sha256,
		).hexdigest()
		is_verified = (signature == expected_mock_sig or signature == "mock_signature_valid")

	if not is_verified:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "Payment verification failed. Invalid Razorpay signature.",
		}

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

	# Update Redis Cache first
	cache = frappe.cache()
	cached_item = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
	if cached_item:
		cached_item["payment_status"] = "Paid"
		cached_item["status"] = "Confirmed"
		cached_item["razorpay_order_id"] = order_id
		cached_item["razorpay_payment_id"] = payment_id
		cached_item["razorpay_signature"] = signature
		cached_item["payment_date"] = now_str
		set_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}", cached_item)

	# Update or Insert in MariaDB
	if frappe.db.exists("ConnectFest Registration", {"registration_id": reg_id}):
		frappe.db.set_value(
			"ConnectFest Registration",
			{"registration_id": reg_id},
			{
				"payment_status": "Paid",
				"status": "Confirmed",
				"razorpay_order_id": order_id,
				"razorpay_payment_id": payment_id,
				"razorpay_signature": signature,
				"payment_date": now_str,
			},
		)
		frappe.db.commit()
	elif cached_item:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Registration", **cached_item})
			doc.flags.ignore_permissions = True
			doc.insert(ignore_if_duplicate=True)
			frappe.db.commit()
		except Exception:
			pass

	return {
		"status": "success",
		"message": "Payment verified successfully! Stall registration is confirmed.",
		"registration_id": reg_id,
		"payment_status": "Paid",
		"status": "Confirmed",
		"razorpay_order_id": order_id,
		"razorpay_payment_id": payment_id,
		"payment_date": now_str,
	}


# ==============================================================================
# 3. VISITOR REGISTRATION API
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def register_visitor(**kwargs):
	"""
	Visitor Registration API (Optimized for 1,000+ RPS).
	- Mandatory: Full Name, Email Address, Mobile Number
	- Optional: Source of Info
	"""
	data = extract_request_data(kwargs)

	full_name = (data.get("full_name") or data.get("name") or "").strip()
	email = (data.get("email") or data.get("email_address") or "").strip()
	mobile_no = (data.get("mobile_no") or data.get("mobile") or data.get("phone") or "").strip()
	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()
	sync_mode = parse_boolean(data.get("sync", 0))

	errors = []
	if not full_name:
		errors.append("Full Name (full_name) is required")
	if not email:
		errors.append("Email Address (email) is required")
	elif "@" not in email:
		errors.append("Valid Email Address is required")
	if not mobile_no:
		errors.append("Mobile Number (mobile_no) is required")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	cache = frappe.cache()
	reg_id = generate_visitor_id(cache)
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S.%f")

	record = {
		"name": reg_id,
		"registration_id": reg_id,
		"full_name": full_name,
		"email": email,
		"mobile_no": mobile_no,
		"source_of_info": source_of_info,
		"status": "Registered",
		"creation": now_str,
		"modified": now_str,
		"modified_by": "Administrator",
		"owner": "Administrator",
		"docstatus": 0,
		"idx": 0,
	}

	record_json = json.dumps(record)

	# Direct Synchronous Mode
	if sync_mode:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Visitor Registration", **record})
			doc.flags.ignore_permissions = True
			doc.insert()
			set_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{reg_id}", record)
			return {
				"status": "success",
				"type": "visitor",
				"message": "Visitor registration successful (direct)",
				"registration_id": reg_id,
				"data": record,
			}
		except Exception as e:
			frappe.log_error("ConnectFest Visitor Sync Error", str(e))
			frappe.local.response["http_status_code"] = 500
			return {"status": "error", "message": str(e)}

	# In-Memory Queue High-Throughput Ingestion (1,000+ RPS)
	try:
		raw_queue_key = cache.make_key(VISITOR_QUEUE_KEY)
		raw_cache_key = cache.make_key(f"{VISITOR_CACHE_PREFIX}{reg_id}")
		pipe = cache.pipeline()
		pipe.rpush(raw_queue_key, record_json)
		pipe.setex(raw_cache_key, 86400, record_json)
		results = pipe.execute()
		queue_len = results[0]

		if queue_len >= 50:
			trigger_async_flush()

		return {
			"status": "success",
			"type": "visitor",
			"message": "Visitor registration received successfully!",
			"registration_id": reg_id,
			"data": record,
		}
	except Exception as e:
		try:
			doc = frappe.get_doc({"doctype": "ConnectFest Visitor Registration", **record})
			doc.flags.ignore_permissions = True
			doc.insert()
			return {
				"status": "success",
				"type": "visitor",
				"message": "Visitor registration successful (fallback)",
				"registration_id": reg_id,
				"data": record,
			}
		except Exception as db_err:
			frappe.local.response["http_status_code"] = 500
			return {"status": "error", "message": str(db_err)}


# ==============================================================================
# 4. LOOKUP & STATS APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_registration(registration_id):
	"""Universal lookup by Registration ID for both Stalls and Visitors."""
	if not registration_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	cache = frappe.cache()

	# 1. Check Visitor Cache
	v_cached = get_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{registration_id}")
	if v_cached:
		return {"status": "success", "type": "visitor", "source": "cache", "data": v_cached}

	# 2. Check Stall Cache
	s_cached = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{registration_id}")
	if s_cached:
		return {"status": "success", "type": "stall", "source": "cache", "data": s_cached}

	# 3. Check Database: Visitor
	v_records = frappe.get_all(
		"ConnectFest Visitor Registration",
		filters={"registration_id": registration_id},
		fields=["*"],
		limit=1,
	)
	if v_records:
		return {"status": "success", "type": "visitor", "source": "db", "data": v_records[0]}

	# 4. Check Database: Stall
	s_records = frappe.get_all(
		"ConnectFest Registration",
		filters={"registration_id": registration_id},
		fields=["*"],
		limit=1,
	)
	if s_records:
		return {"status": "success", "type": "stall", "source": "db", "data": s_records[0]}

	frappe.local.response["http_status_code"] = 404
	return {"status": "error", "message": "Registration not found"}


@frappe.whitelist(allow_guest=True)
def get_queue_stats():
	"""Monitor queue lengths and total database counts for both Stalls and Visitors."""
	cache = frappe.cache()
	try:
		pending_stalls = cache.llen(STALL_QUEUE_KEY)
	except Exception:
		pending_stalls = 0

	try:
		pending_visitors = cache.llen(VISITOR_QUEUE_KEY)
	except Exception:
		pending_visitors = 0

	total_stalls_db = frappe.db.count("ConnectFest Registration")
	total_visitors_db = frappe.db.count("ConnectFest Visitor Registration")

	return {
		"status": "success",
		"pending_stall_queue": pending_stalls,
		"pending_visitor_queue": pending_visitors,
		"total_stalls_in_db": total_stalls_db,
		"total_visitors_in_db": total_visitors_db,
		"batch_chunk_size": BATCH_CHUNK_SIZE,
		"stall_pricing": STALL_PRICING,
	}


@frappe.whitelist(allow_guest=True)
def flush_queue():
	"""Manually flush both stall and visitor queues."""
	stalls_flushed, visitors_flushed = flush_registration_queue()
	return {
		"status": "success",
		"stalls_flushed": stalls_flushed,
		"visitors_flushed": visitors_flushed,
	}


def trigger_async_flush():
	"""Trigger non-blocking background queue drain."""
	try:
		frappe.enqueue(
			"connectfest.api.flush_registration_queue",
			queue="short",
			now=False,
			enqueue_after_commit=False,
		)
	except Exception:
		pass


def flush_registration_queue(batch_size=BATCH_CHUNK_SIZE):
	"""
	Worker task: Drains items from Redis lists and bulk inserts into MariaDB.
	"""
	cache = frappe.cache()
	total_stalls = _flush_single_queue(
		cache,
		STALL_QUEUE_KEY,
		"ConnectFest Registration",
		[
			"name",
			"registration_id",
			"full_name",
			"company_name",
			"location",
			"mobile_no",
			"email",
			"is_bni_member",
			"region_name",
			"chapter_name",
			"industry",
			"segment",
			"source_of_info",
			"stall_category",
			"amount",
			"payment_status",
			"razorpay_order_id",
			"razorpay_payment_id",
			"razorpay_signature",
			"payment_date",
			"status",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
		],
		batch_size,
	)

	total_visitors = _flush_single_queue(
		cache,
		VISITOR_QUEUE_KEY,
		"ConnectFest Visitor Registration",
		[
			"name",
			"registration_id",
			"full_name",
			"email",
			"mobile_no",
			"status",
			"source_of_info",
			"creation",
			"modified",
			"modified_by",
			"owner",
			"docstatus",
			"idx",
		],
		batch_size,
	)

	return total_stalls, total_visitors


def _flush_single_queue(cache, queue_key_name, doctype, db_fields, batch_size):
	raw_key = cache.make_key(queue_key_name)
	processed = 0

	while True:
		queue_len = cache.llen(queue_key_name)
		if not queue_len:
			break

		fetch_count = min(queue_len, batch_size)
		pipe = cache.pipeline()
		for _ in range(fetch_count):
			pipe.lpop(raw_key)
		raw_items = pipe.execute()

		rows_to_insert = []
		for raw in raw_items:
			if not raw:
				continue
			try:
				if isinstance(raw, bytes):
					raw = raw.decode("utf-8")
				item = json.loads(raw)
				reg_id = item.get("registration_id")
				if reg_id and doctype == "ConnectFest Registration":
					cached_item = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{reg_id}")
					if cached_item:
						item.update(cached_item)
				row = tuple(item.get(f) for f in db_fields)
				rows_to_insert.append(row)
			except Exception as parse_err:
				frappe.log_error(f"{doctype} Queue Parse Error", str(parse_err))

		if rows_to_insert:
			try:
				frappe.db.bulk_insert(
					doctype,
					fields=db_fields,
					values=rows_to_insert,
					ignore_duplicates=True,
				)
				frappe.db.commit()
				processed += len(rows_to_insert)
			except Exception as insert_err:
				frappe.log_error(f"{doctype} Bulk Insert Error", str(insert_err))
				frappe.db.rollback()
				restore_pipe = cache.pipeline()
				for r in reversed(raw_items):
					if r:
						restore_pipe.lpush(raw_key, r)
				restore_pipe.execute()
				break

		if fetch_count < batch_size:
			break

	return processed
