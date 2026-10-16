import json
import time
import frappe
from frappe import _
from frappe.utils import now_datetime

# Redis Queue & Cache Keys
STALL_QUEUE_KEY = "connectfest:stall_queue"
VISITOR_QUEUE_KEY = "connectfest:visitor_queue"
STALL_CACHE_PREFIX = "connectfest:stall:"
VISITOR_CACHE_PREFIX = "connectfest:visitor:"
STALL_COUNTER_KEY = "connectfest:stall_counter"
VISITOR_COUNTER_KEY = "connectfest:visitor_counter"
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


def generate_stall_id(cache):
	try:
		seq = cache.incr(STALL_COUNTER_KEY)
	except Exception:
		seq = int(time.time() * 1000) % 1000000
	ym = now_datetime().strftime("%y%m")
	return f"CF-{ym}-{seq:06d}"


def generate_visitor_id(cache):
	try:
		seq = cache.incr(VISITOR_COUNTER_KEY)
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


# ==============================================================================
# 1. STALL REGISTRATION API
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def register_stall(**kwargs):
	"""
	Stall Registration API (Optimized for 1,000+ RPS).
	- Mandatory: Name, Company Name, Source of Info
	- Optional: Location (removed mandatory), Mobile No, Email, Industry, Segment
	- BNI Member: Only Region Name & Chapter Name are mandatory if BNI Member = Yes.
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

	source_of_info = (data.get("source_of_info") or data.get("source") or "").strip()
	mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()
	email = (data.get("email") or "").strip()
	sync_mode = parse_boolean(data.get("sync", 0))

	errors = []
	if not full_name:
		errors.append("Name (full_name) is required")
	if not company_name:
		errors.append("Company Name (company_name) is required")
	if not source_of_info:
		errors.append("How did you know about this event? (source_of_info) is required")

	# If BNI Member is Yes: ONLY Region Name and Chapter Name are mandatory
	if is_bni_member:
		if not region_name:
			errors.append("Region Name (region_name) is required for BNI members")
		if not chapter_name:
			errors.append("Chapter Name (chapter_name) is required for BNI members")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

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
			cache.setex(f"{STALL_CACHE_PREFIX}{reg_id}", 86400, record_json)
			return {
				"status": "success",
				"type": "stall",
				"message": "Stall registration successful (direct)",
				"registration_id": reg_id,
				"data": record,
			}
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

		return {
			"status": "success",
			"type": "stall",
			"message": "Stall registration received successfully!",
			"registration_id": reg_id,
			"data": record,
		}
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
# 2. VISITOR REGISTRATION API
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
			cache.setex(f"{VISITOR_CACHE_PREFIX}{reg_id}", 86400, record_json)
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
# 3. LOOKUP & STATS APIS
# ==============================================================================
@frappe.whitelist(allow_guest=True)
def get_registration(registration_id):
	"""Universal lookup by Registration ID for both Stalls and Visitors."""
	if not registration_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	cache = frappe.cache()

	# 1. Check Visitor Cache
	v_cached = cache.get(f"{VISITOR_CACHE_PREFIX}{registration_id}")
	if v_cached:
		if isinstance(v_cached, bytes):
			v_cached = v_cached.decode("utf-8")
		return {"status": "success", "type": "visitor", "source": "cache", "data": json.loads(v_cached)}

	# 2. Check Stall Cache
	s_cached = cache.get(f"{STALL_CACHE_PREFIX}{registration_id}")
	if s_cached:
		if isinstance(s_cached, bytes):
			s_cached = s_cached.decode("utf-8")
		return {"status": "success", "type": "stall", "source": "cache", "data": json.loads(s_cached)}

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
	Worker task: Drains up to batch_size items from Redis lists in single roundtrips,
	and executes high-speed bulk inserts into MariaDB.
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
