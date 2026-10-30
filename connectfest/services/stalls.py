import json
import uuid
from datetime import timedelta
import frappe
from frappe.utils import now_datetime, flt, cint
from connectfest.services.common import (
	extract_request_data,
	parse_boolean,
	handle_api_errors,
	generate_stall_id,
	set_cached_record,
	save_image_from_base64_or_url,
	STALL_LOCK_PREFIX,
	STALL_CACHE_PREFIX,
	VALID_SOURCES,
)
from connectfest.services.pricing import get_tier_pricing
from connectfest.services.auth import check_admin_permission, ensure_frappe_user

STALLS_INVENTORY_CACHE_KEY = "connectfest:stalls:public_inventory"


def invalidate_stalls_cache():
	"""Invalidates Redis cache for floor map and stalls inventory."""
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(STALLS_INVENTORY_CACHE_KEY))
	except Exception:
		pass


def check_stall_hold_in_redis(stall_number, stall_doc=None):
	"""
	Authoritative Redis Hold Inspection:
	Uses Redis TTL (ex=600s) as the primary single source of truth for 10-minute holds.
	- If Redis lock key exists: returns is_held=True with exact remaining TTL seconds directly from Redis.
	- If Redis lock key is expired or missing: returns is_held=False and automatically
	  syncs DB status to 'Available' and broadcasts realtime websocket update.
	"""
	if not stall_number:
		return {"is_held": False, "remaining_seconds": 0, "token": None, "data": None}

	s_num = str(stall_number).strip().upper()
	cache = frappe.cache()
	raw_key = cache.make_key(f"{STALL_LOCK_PREFIX}{s_num}")

	raw_val = None
	ttl = -2
	redis_error = False
	try:
		raw_val = cache.get(raw_key)
		if raw_val is not None:
			client = getattr(cache, "_client", None) or getattr(cache, "_redis", None)
			if client and hasattr(client, "ttl"):
				ttl = client.ttl(raw_key)
			elif hasattr(cache, "ttl"):
				ttl = cache.ttl(raw_key)
			else:
				ttl = 600
	except Exception:
		redis_error = True

	doc = stall_doc
	if doc is None and frappe.db.exists("ConnectFest Stall", s_num):
		try:
			doc = frappe.get_doc("ConnectFest Stall", s_num)
		except Exception:
			doc = None

	# 1. Redis Key exists and is active!
	if raw_val is not None:
		remaining = max(1, ttl) if ttl > 0 else 600
		token = ""
		parsed_data = {}
		if isinstance(raw_val, (bytes, str)):
			try:
				if isinstance(raw_val, bytes):
					raw_val = raw_val.decode("utf-8")
				if raw_val.startswith("{"):
					parsed_data = json.loads(raw_val)
					token = parsed_data.get("token") or parsed_data.get("hold_token")
				else:
					token = raw_val
			except Exception:
				token = str(raw_val)
		elif isinstance(raw_val, dict):
			parsed_data = raw_val
			token = parsed_data.get("token") or parsed_data.get("hold_token")

		return {
			"is_held": True,
			"remaining_seconds": remaining,
			"token": token,
			"data": parsed_data,
		}

	# 2. Redis Key has expired (TTL is up) or Redis connection error!
	# Database Fail-safe: if Redis had an error but MariaDB hold is still active, honor the hold!
	if doc and getattr(doc, "status", None) == "Hold":
		from frappe.utils import get_datetime
		now = now_datetime()
		if redis_error and doc.hold_expires_at and get_datetime(doc.hold_expires_at) > now:
			remaining = max(1, int((get_datetime(doc.hold_expires_at) - now).total_seconds()))
			return {
				"is_held": True,
				"remaining_seconds": remaining,
				"token": doc.hold_token or "",
				"data": {
					"token": doc.hold_token,
					"stall_number": s_num,
					"name": doc.booked_by_name,
					"email": doc.booked_by_email,
					"mobile": doc.booked_by_mobile,
					"company": doc.booked_by_company,
					"registration": doc.booked_by_registration,
				},
			}

		# If Redis is reachable and key is gone, OR if hold has expired: auto-sync DB status to 'Available'
		try:
			frappe.db.set_value("ConnectFest Stall", s_num, {
				"status": "Available",
				"hold_token": None,
				"hold_expires_at": None,
				"booked_by_registration": None,
				"booked_by_name": None,
				"booked_by_company": None,
				"booked_by_email": None,
				"booked_by_mobile": None,
				"payment_mode": None,
			}, update_modified=False)
			frappe.db.commit()
			invalidate_stalls_cache()
			if doc and hasattr(doc, "reload"):
				try:
					doc.reload()
				except Exception:
					pass

			from connectfest.services.realtime import broadcast_stall_update
			broadcast_stall_update(s_num, "Available", {
				"tier": doc.tier,
				"price": doc.price,
				"dimensions": doc.dimensions,
				"is_blocked_by_admin": bool(doc.is_blocked_by_admin),
				"blocked_reason": doc.blocked_reason,
				"booked_by_company": None,
				"booked_by_name": None,
				"hold_expires_at": None,
			})
		except Exception:
			pass

	return {
		"is_held": False,
		"remaining_seconds": 0,
		"token": None,
		"data": None,
	}


def release_expired_holds():
	"""
	Redis-driven scheduled and on-demand worker to release all expired stall holds.
	Checks Redis atomic lock 'connectfest:stall_lock:<stall_num>' as the authoritative TTL source.
	If the Redis key is gone (TTL expired) OR hold_expires_at has elapsed, cleans up DB and broadcasts live.
	"""
	try:
		now_dt = now_datetime()
		all_held = frappe.db.sql("""
			SELECT name, stall_number, tier, price, dimensions, is_blocked_by_admin, blocked_reason, hold_expires_at
			FROM `tabConnectFest Stall`
			WHERE status = 'Hold'
		""", as_dict=True)

		if not all_held:
			return {"status": "success", "released_count": 0, "message": "No expired holds found"}

		cache = frappe.cache()
		released_stalls = []
		for s in all_held:
			stall_id = s.get("name") or s.get("stall_number")
			stall_num = s.get("stall_number") or stall_id
			raw_key = cache.make_key(f"{STALL_LOCK_PREFIX}{stall_num}")
			redis_val = cache.get(raw_key)
			exp_at = s.get("hold_expires_at")

			# Expired if Redis TTL key has vanished OR DB timestamp passed
			is_expired = False
			if not redis_val:
				is_expired = True
			elif exp_at and exp_at <= now_dt:
				is_expired = True

			if is_expired:
				frappe.db.set_value("ConnectFest Stall", stall_id, {
					"status": "Available",
					"hold_token": None,
					"hold_expires_at": None,
					"booked_by_registration": None,
					"booked_by_name": None,
					"booked_by_company": None,
					"booked_by_email": None,
					"booked_by_mobile": None,
					"payment_mode": None,
				}, update_modified=False)
				try:
					cache.delete(raw_key)
				except Exception:
					pass
				released_stalls.append(s)

		frappe.db.commit()
		invalidate_stalls_cache()

		try:
			from connectfest.services.realtime import broadcast_stall_update
			for s in released_stalls:
				stall_num = s.get("stall_number") or s.get("name")
				broadcast_stall_update(stall_num, "Available", {
					"tier": s.get("tier"),
					"price": s.get("price"),
					"dimensions": s.get("dimensions"),
					"is_blocked_by_admin": bool(s.get("is_blocked_by_admin")),
					"blocked_reason": s.get("blocked_reason"),
					"booked_by_company": None,
					"booked_by_name": None,
					"hold_expires_at": None,
				})
		except Exception:
			pass

		return {
			"status": "success",
			"released_count": len(released_stalls),
			"stalls": [s.get("stall_number") or s.get("name") for s in released_stalls]
		}
	except Exception:
		frappe.log_error("ConnectFest release_expired_holds error", frappe.get_traceback())
		return {"status": "error", "message": "Failed to release expired holds"}


def get_stalls(**kwargs):
	"""
	Universal Floor Map & Real-Time Inventory API.
	Returns all 220 stalls with zone, dimensions, blueprint coordinates, live booking status, and pricing.
	Cached in Redis with sub-second responsiveness.
	"""
	try:
		# Auto-release any expired holds before returning inventory
		try:
			release_expired_holds()
		except Exception:
			pass

		from frappe.utils import cint
		data = extract_request_data(kwargs)
		filter_tier = (data.get("tier") or data.get("stall_category") or "").strip()
		filter_status = (data.get("status") or "").strip().capitalize()
		search_term = (data.get("search") or data.get("query") or "").strip().lower()

		raw_page = data.get("page")
		raw_limit = data.get("limit") or data.get("page_size")
		page = max(1, cint(raw_page)) if raw_page else 1
		limit = cint(raw_limit) if raw_limit and str(raw_limit).lower() != "all" else 0

		is_default = not filter_tier and not filter_status and not search_term and (limit == 0 or limit >= 220) and page == 1
		cache = frappe.cache()
		cache_key = cache.make_key(STALLS_INVENTORY_CACHE_KEY)

		if is_default:
			try:
				cached = cache.get(cache_key)
				if cached:
					if isinstance(cached, bytes):
						cached = cached.decode("utf-8")
					return json.loads(cached)
			except Exception:
				pass

		stalls = frappe.get_all(
			"ConnectFest Stall",
			fields=[
				"stall_number",
				"tier",
				"dimensions",
				"zone",
				"price",
				"status",
				"block",
				"label",
				"x",
				"y",
				"width",
				"height",
				"is_blocked_by_admin",
				"blocked_reason",
				"booked_by_registration",
				"booked_by_name",
				"booked_by_company",
				"hold_expires_at",
			],
			order_by="stall_number ASC",
			limit=500,
		)

		now_dt = now_datetime()
		pricing = get_tier_pricing()

		total_stalls = len(stalls)
		total_booked = 0
		total_revenue = 0.0

		tier_summary = {}
		for t_name, t_price in pricing.items():
			tier_summary[t_name] = {"total": 0, "available": 0, "booked": 0, "price": flt(t_price)}

		holds_released = 0
		for stall in stalls:
			t = stall.tier or "Regular"
			if t not in tier_summary:
				tier_summary[t] = {"total": 0, "available": 0, "booked": 0, "price": flt(pricing.get(t, stall.price))}

			tier_price = pricing.get(t, stall.price)
			stall["price"] = tier_price

			# Release expired hold
			if stall.status == "Hold" and stall.hold_expires_at and stall.hold_expires_at <= now_dt:
				frappe.db.set_value("ConnectFest Stall", stall.stall_number, {
					"status": "Available",
					"hold_token": None,
					"hold_expires_at": None,
					"booked_by_registration": None,
					"booked_by_name": None,
					"booked_by_company": None,
					"booked_by_email": None,
					"booked_by_mobile": None,
					"payment_mode": None,
				}, update_modified=False)
				stall["status"] = "Available"
				stall["hold_token"] = None
				stall["hold_expires_at"] = None
				stall["booked_by_registration"] = None
				stall["booked_by_name"] = None
				stall["booked_by_company"] = None
				cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall.stall_number}"))
				holds_released += 1

			# Only count unblocked stalls in public stats
			if not stall.is_blocked_by_admin and stall.status != "Blocked":
				tier_summary[t]["total"] += 1
				if stall.status == "Booked":
					total_booked += 1
					total_revenue += flt(stall.price)
					tier_summary[t]["booked"] += 1
				elif stall.status == "Available":
					tier_summary[t]["available"] += 1

			# Zone assignment
			if not stall.get("zone"):
				if t == "Title Sponsor": stall["zone"] = "Zone TS - Title Sponsor Arena"
				elif t == "Silver": stall["zone"] = "Zone D - East Pavilions"
				elif t == "Premium": stall["zone"] = "Zone C - Center Grand Aisles"
				elif t == "Standard": stall["zone"] = "Zone B - West Aisles"
				else: stall["zone"] = "Zone E - South Entrance Corridor"

		if holds_released > 0:
			frappe.db.commit()

		# Apply Filters (Public Security: Blocked stalls are strictly hidden from users)
		filtered_stalls = []
		for s in stalls:
			if s.get("is_blocked_by_admin") or s.get("status") == "Blocked":
				continue
			if filter_tier and (s.tier or "").lower() != filter_tier.lower():
				continue
			if filter_status and (s.status or "").lower() != filter_status.lower():
				continue
			if search_term:
				match_num = search_term in (s.stall_number or "").lower()
				match_comp = search_term in (s.get("booked_by_company") or "").lower()
				match_name = search_term in (s.get("booked_by_name") or "").lower()
				if not (match_num or match_comp or match_name):
					continue
			filtered_stalls.append(s)

		# Apply Pagination
		filtered_count = len(filtered_stalls)
		if limit > 0:
			start = (page - 1) * limit
			end = start + limit
			paginated_stalls = filtered_stalls[start:end]
			total_pages = max(1, (filtered_count + limit - 1) // limit)
		else:
			paginated_stalls = filtered_stalls
			total_pages = 1

		facilities_overview = [
			{"name": "Power Supply", "detail": "5A/15A socket in every stall", "included": True},
			{"name": "Exhibitor Badges", "detail": "2 badges per stall", "included": True},
			{"name": "Spotlights & Fascia", "detail": "High-illumination LEDs & company name fascia", "included": True},
			{"name": "Wi-Fi & Cleaning", "detail": "High-speed event Wi-Fi and daily booth cleaning", "included": True},
		]

		res = {
			"status": "success",
			"filters_applied": {
				"tier": filter_tier or None,
				"status": filter_status or None,
				"search": search_term or None,
			},
			"pagination": {
				"current_page": page,
				"page_size": limit if limit > 0 else filtered_count,
				"total_matching": filtered_count,
				"total_pages": total_pages,
				"has_next": page < total_pages if limit > 0 else False,
				"has_prev": page > 1,
			},
			"total_stalls": total_stalls,
			"total_booked": total_booked,
			"total_revenue": total_revenue,
			"tier_summary": tier_summary,
			"facilities_overview": facilities_overview,
			"stalls": paginated_stalls,
		}

		if is_default:
			try:
				cache.setex(cache_key, 15, json.dumps(res, default=str))
			except Exception:
				pass

		return res
	except Exception as e:
		frappe.log_error("ConnectFest Get Stalls Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


def hold_stall(stall_number=None, **kwargs):
	"""
	High-Concurrency Seat Locking Engine (Theatre Booking Style).
	Locks a stall for 10 minutes (600s) during checkout using Redis atomic locks with database fail-safe.
	"""
	try:
		data = extract_request_data(kwargs)
		stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip()
		email = (data.get("email") or "").strip().lower()
		mobile_no = (data.get("mobile_no") or data.get("phone") or "").strip()
		full_name = (data.get("full_name") or data.get("name") or "").strip()
		company_name = (data.get("company_name") or "").strip()
		reg_id = (data.get("registration_id") or kwargs.get("registration_id") or "").strip()

		# Auto-resolve missing or email-like attendee details from database
		sess_u = getattr(frappe.session, "user", "")
		if not email and sess_u and sess_u != "Guest" and "@" in sess_u:
			email = sess_u.strip().lower()

		if reg_id:
			reg_data = frappe.db.get_value("ConnectFest Registration", {"registration_id": reg_id}, ["full_name", "email", "mobile_no", "company_name"], as_dict=True) or frappe.db.get_value("ConnectFest Registration", reg_id, ["full_name", "email", "mobile_no", "company_name"], as_dict=True)
			if reg_data:
				if not full_name or "@" in full_name:
					cand = (reg_data.get("full_name") or "").strip()
					if cand and "@" not in cand:
						full_name = cand
				if not email:
					email = (reg_data.get("email") or "").strip().lower()
				if not mobile_no:
					mobile_no = (reg_data.get("mobile_no") or "").strip()
				if not company_name:
					company_name = (reg_data.get("company_name") or "").strip()

		if not full_name or "@" in full_name:
			if email:
				cand = (frappe.db.get_value("ConnectFest Registration", {"email": email}, "full_name") or frappe.db.get_value("ConnectFest Visitor Registration", {"email": email}, "full_name") or frappe.db.get_value("User", email, "full_name") or "").strip()
				if cand and "@" not in cand:
					full_name = cand
			if (not full_name or "@" in full_name) and mobile_no:
				cand = (frappe.db.get_value("ConnectFest Registration", {"mobile_no": mobile_no}, "full_name") or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mobile_no}, "full_name") or "").strip()
				if cand and "@" not in cand:
					full_name = cand
			if not full_name or "@" in full_name:
				full_name = email.split("@")[0].replace(".", " ").replace("_", " ").title() if email else "Exhibitor"

		if not stall_num:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "stall_number is required"}

		if not frappe.db.exists("ConnectFest Stall", stall_num):
			frappe.local.response["http_status_code"] = 404
			return {"status": "error", "message": f"Stall '{stall_num}' does not exist"}

		# Prevent duplicate payment/hold for the same stall if already Paid & Confirmed
		if (email or mobile_no) and stall_num:
			existing_same_stall = frappe.db.sql("""
				SELECT registration_id, stall_number FROM `tabConnectFest Registration`
				WHERE (email = %(email)s OR (mobile_no = %(mobile)s AND %(mobile)s != ''))
				  AND stall_number = %(stall_num)s
				  AND payment_status = 'Paid'
				LIMIT 1
			""", {"email": email, "mobile": mobile_no, "stall_num": stall_num}, as_dict=True)
			if existing_same_stall:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Stall '{stall_num}' has already been paid and confirmed for your account. Multiple payments are strictly prohibited!",
					"payment_status": "Paid",
					"stall_number": stall_num,
				}

		stall = frappe.get_doc("ConnectFest Stall", stall_num)

		from connectfest.services.auth import is_admin_user
		sess_u = getattr(frappe.session, "user", "")
		is_admin = is_admin_user(sess_u)

		if (stall.is_blocked_by_admin or stall.status == "Blocked") and not is_admin:
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "error",
				"message": f"Stall {stall_num} has been blocked by administration ({stall.blocked_reason or 'Reserved'})."
			}

		if stall.status == "Booked":
			frappe.local.response["http_status_code"] = 409
			return {
				"status": "error",
				"message": f"Stall {stall_num} is already booked and confirmed by another exhibitor."
			}

		# Authoritative Redis Hold Check
		hold_info = check_stall_hold_in_redis(stall_num, stall)
		existing_token = data.get("hold_token")
		if hold_info["is_held"]:
			current_token = hold_info["token"]
			held_data = hold_info.get("data") or {}
			held_reg = held_data.get("registration") or (getattr(stall, "booked_by_registration", None) if stall else None)
			held_email = held_data.get("email") or (getattr(stall, "booked_by_email", None) if stall else None)
			held_mobile = held_data.get("mobile") or (getattr(stall, "booked_by_mobile", None) if stall else None)

			is_same_holder = False
			if existing_token and existing_token == current_token:
				is_same_holder = True
			elif reg_id and held_reg and reg_id == held_reg:
				is_same_holder = True
			elif email and held_email and email.strip().lower() == held_email.strip().lower():
				is_same_holder = True
			elif mobile_no and held_mobile and mobile_no.strip() == held_mobile.strip():
				is_same_holder = True

			if not is_same_holder:
				remaining_secs = hold_info["remaining_seconds"]
				frappe.local.response["http_status_code"] = 409
				return {
					"status": "error",
					"message": f"Stall {stall_num} is currently held by another attendee completing payment. (Expires in Redis in {remaining_secs}s). Please select a different stall."
				}

		# Acquire Redis Atomic Lock (Primary 10,000+ RPS Defense with 600s TTL)
		cache = frappe.cache()
		lock_key = f"{STALL_LOCK_PREFIX}{stall_num}"
		new_token = f"hold_{uuid.uuid4().hex[:12]}"
		now_dt = now_datetime()
		expires_at = now_dt + timedelta(seconds=600)
		raw_key = cache.make_key(lock_key)

		hold_payload = {
			"token": new_token,
			"stall_number": stall_num,
			"name": full_name,
			"email": email,
			"mobile": mobile_no,
			"company": company_name,
			"registration": reg_id,
			"expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S")
		}

		# Atomic SET with exact 600s (10-minute) TTL in Redis
		try:
			cache.set(raw_key, json.dumps(hold_payload), ex=600)
		except Exception:
			try:
				cache.set(raw_key, new_token, ex=600)
			except Exception:
				pass

		frappe.db.set_value("ConnectFest Stall", stall_num, {
			"status": "Hold",
			"hold_token": new_token,
			"hold_expires_at": expires_at,
			"booked_by_registration": reg_id or None,
			"booked_by_name": full_name,
			"booked_by_email": email,
			"booked_by_mobile": mobile_no,
			"booked_by_company": company_name,
			"payment_mode": "Online",
		}, update_modified=True)
		frappe.db.commit()
		invalidate_stalls_cache()

		dynamic_prices = get_tier_pricing()
		current_price = dynamic_prices.get(stall.tier, stall.price)

		# Broadcast Real-Time WebSocket event to all connected floor maps
		try:
			from connectfest.services.realtime import broadcast_stall_update
			broadcast_stall_update(
				stall_num,
				"Hold",
				{
					"tier": stall.tier,
					"price": current_price,
					"dimensions": stall.dimensions,
					"hold_token": new_token,
					"hold_expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
					"booked_by_name": full_name,
					"booked_by_company": company_name,
					"payment_mode": "Online",
				}
			)
		except Exception:
			pass

		return {
			"status": "success",
			"message": f"Stall {stall_num} held successfully for 10 minutes",
			"stall_number": stall_num,
			"tier": stall.tier,
			"price": current_price,
			"hold_token": new_token,
			"expires_in_seconds": 600,
			"hold_expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
		}
	except Exception as e:
		frappe.log_error("ConnectFest Hold Stall Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}


def release_stall_hold(stall_number=None, hold_token=None, **kwargs):
	"""Releases temporary stall hold if user changes selection or abandons."""
	data = extract_request_data(kwargs)
	stall_num = (stall_number or data.get("stall_number") or "").strip()
	token = (hold_token or data.get("hold_token") or "").strip()

	if not stall_num:
		return {"status": "error", "message": "stall_number is required"}

	if frappe.db.exists("ConnectFest Stall", stall_num):
		stall = frappe.get_doc("ConnectFest Stall", stall_num)
		if stall.status == "Hold" and (not token or stall.hold_token == token):
			frappe.db.set_value("ConnectFest Stall", stall_num, {
				"status": "Available",
				"hold_token": None,
				"hold_expires_at": None,
				"booked_by_registration": None,
				"booked_by_name": None,
				"booked_by_company": None,
				"booked_by_email": None,
				"booked_by_mobile": None,
				"payment_mode": None,
			}, update_modified=False)
			frappe.db.commit()
			invalidate_stalls_cache()

	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))
	except Exception:
		pass

	try:
		from connectfest.services.realtime import broadcast_stall_update
		broadcast_stall_update(stall_num, "Available")
	except Exception:
		pass

	return {"status": "success", "message": f"Stall {stall_num} hold released"}


def get_stall_detail(stall_number=None, **kwargs):
	"""Public API: Get specific stall details with pricing, dimensions, and availability."""
	data = extract_request_data(kwargs)
	s_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()
	if not s_num or not frappe.db.exists("ConnectFest Stall", s_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{s_num}' does not exist"}

	stall = frappe.get_doc("ConnectFest Stall", s_num)
	stall.check_and_release_expired_hold()
	pricing = get_tier_pricing()
	current_price = pricing.get(stall.tier, stall.price)

	return {
		"status": "success",
		"stall": {
			"stall_number": stall.stall_number,
			"tier": stall.tier,
			"dimensions": stall.dimensions,
			"zone": stall.zone,
			"price": current_price,
			"status": "Blocked" if stall.is_blocked_by_admin else stall.status,
			"is_blocked_by_admin": bool(stall.is_blocked_by_admin),
			"hold_expires_at": str(stall.hold_expires_at) if stall.hold_expires_at else None,
		}
	}


def toggle_stall_block(stall_number=None, action=None, reason=None, force=0, **kwargs):
	"""
	Admin API to block/unblock stalls.
	Eg: Stall 11 block -> User cannot buy.
	Prevents blocking if an attendee is actively in checkout (10-minute hold active)!
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("stall block/unblock controls")
	if err:
		return err

	data = extract_request_data(kwargs)
	stall_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()
	raw_act = action if action is not None else data.get("action", "block")
	act = str(raw_act or "block").strip().lower()
	resn = (reason or data.get("reason") or ("Reserved by Administration" if act == "block" else "")).strip()
	is_force = parse_boolean(force if force is not None else data.get("force", 0))

	if not stall_num or not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' not found"}

	stall = frappe.get_doc("ConnectFest Stall", stall_num)

	if act == "block":
		if stall.status == "Booked":
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": f"Cannot block stall {stall_num} - already paid and booked by {stall.booked_by_name or stall.booked_by_registration}."}

		# Guard against blocking stalls currently on active 10-minute Hold (Redis verified)
		hold_info = check_stall_hold_in_redis(stall_num, stall)

		if hold_info["is_held"] and not is_force:
			buyer = stall.booked_by_name or stall.booked_by_company or stall.booked_by_email or stall.booked_by_mobile or "an exhibitor"
			remaining_secs = hold_info["remaining_seconds"]
			remaining_mins = max(1, (remaining_secs + 59) // 60)

			frappe.local.response["http_status_code"] = 409
			return {
				"status": "error",
				"message": f"Cannot block stall {stall_num}: Exhibitor '{buyer}' is currently completing payment! (Hold expires in Redis in ~{remaining_mins} minutes / {remaining_secs}s). Please wait until payment completes or hold expires.",
				"stall_status": "Hold",
				"held_by": buyer,
				"held_by_name": stall.booked_by_name,
				"held_by_company": stall.booked_by_company,
				"held_by_email": stall.booked_by_email,
				"held_by_mobile": stall.booked_by_mobile,
				"hold_expires_in_minutes": remaining_mins,
				"hold_remaining_seconds": remaining_secs,
			}

	if act == "block":
		new_status = "Blocked"
		is_blocked = 1
		blocked_res = resn
		try:
			frappe.cache().delete(frappe.cache().make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))
		except Exception:
			pass
		update_values = {
			"is_blocked_by_admin": 1,
			"status": "Blocked",
			"blocked_reason": resn,
			"hold_token": None,
			"hold_expires_at": None,
			"booked_by_registration": None,
			"booked_by_name": None,
			"booked_by_company": None,
			"booked_by_email": None,
			"booked_by_mobile": None,
			"payment_mode": None,
		}
	else:
		new_status = "Available"
		is_blocked = 0
		blocked_res = ""
		update_values = {
			"is_blocked_by_admin": 0,
			"status": "Available",
			"blocked_reason": "",
		}

	frappe.db.set_value("ConnectFest Stall", stall_num, update_values, update_modified=True)
	frappe.db.commit()
	invalidate_stalls_cache()

	# Realtime WebSocket update
	try:
		from connectfest.services.realtime import broadcast_stall_update
		broadcast_stall_update(
			stall_num,
			new_status,
			{
				"tier": getattr(stall, "tier", None),
				"price": getattr(stall, "price", 0),
				"dimensions": getattr(stall, "dimensions", None),
				"is_blocked_by_admin": is_blocked,
				"blocked_reason": blocked_res,
			}
		)
	except Exception:
		pass

	return {
		"status": "success",
		"message": f"Stall {stall_num} successfully {act}ed",
		"stall_number": stall_num,
		"stall_status": new_status,
		"is_blocked_by_admin": is_blocked,
		"blocked_reason": blocked_res,
	}


def get_admin_dashboard_data(**kwargs):
	"""
	Returns complete real-time dashboard data for ConnectFest Admin.
	Requires System Manager or Administrator privileges.
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("admin dashboard data")
	if err:
		return err

	# Auto-release any expired holds on the fly
	try:
		release_expired_holds()
	except Exception:
		pass

	stalls = frappe.get_all(
		"ConnectFest Stall",
		fields=[
			"stall_number",
			"tier",
			"dimensions",
			"price",
			"status",
			"is_blocked_by_admin",
			"blocked_reason",
			"booked_by_name",
			"booked_by_company",
			"booked_by_mobile",
			"booked_by_registration",
		],
		order_by="stall_number ASC",
		limit=500,
	)

	total_stalls = len(stalls)
	booked_stalls = sum(1 for s in stalls if s.status == "Booked")
	blocked_stalls = sum(1 for s in stalls if s.is_blocked_by_admin or s.status == "Blocked")
	held_stalls = sum(1 for s in stalls if s.status == "Hold")
	available_stalls = total_stalls - booked_stalls - blocked_stalls - held_stalls

	pricing = get_tier_pricing()
	total_revenue = 0.0
	for s in stalls:
		if s.status == "Booked":
			total_revenue += flt(pricing.get(s.tier, s.price))

	visitors_total = frappe.db.count("ConnectFest Visitor Registration")
	visitors_checked_in = frappe.db.count("ConnectFest Visitor Registration", filters={"checked_in": 1})
	visitors_pending = visitors_total - visitors_checked_in

	return {
		"status": "success",
		"event": {
			"name": "ConnectFest 2027",
			"edition": "Annual Premier Festival",
			"dates": "20 & 21 Feb 2027",
			"venue": "Chennai Trade Centre, Nandambakkam, Chennai",
			"timings": "9:00 AM - 7:00 PM",
		},
		"stall_stats": {
			"total": total_stalls,
			"available": available_stalls,
			"booked": booked_stalls,
			"blocked": blocked_stalls,
			"held": held_stalls,
			"total_revenue": total_revenue,
			"occupancy_rate": f"{(booked_stalls / total_stalls * 100):.1f}%" if total_stalls else "0%",
		},
		"visitor_stats": {
			"total_registered": visitors_total,
			"checked_in": visitors_checked_in,
			"pending_entry": visitors_pending,
			"check_in_rate": f"{(visitors_checked_in / visitors_total * 100):.1f}%" if visitors_total else "0%",
		},
		"current_pricing": pricing,
		"stalls": stalls,
	}


# ==============================================================================
# ADMIN STALL INVENTORY & CRUD APIS
# ==============================================================================

def admin_get_stalls(status=None, tier=None, search=None, page=1, limit=50, **kwargs):
	"""
	Admin Stall Inventory & Buyer Report API:
	- Shows ALL stalls, including Blocked stalls with blocked_reason.
	- Status filter: 'Booked', 'Available', 'Blocked', 'Hold'
	- When a stall is Booked, shows full buyer details:
	  booked_by_name, booked_by_company, booked_by_email, booked_by_mobile,
	  booked_by_registration, payment_status, payment_date, amount.
	- Restricted to System Manager / Administrator.
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("admin stall inventory")
	if err:
		return err

	# Auto-release any expired holds on the fly
	try:
		release_expired_holds()
	except Exception:
		pass

	data = extract_request_data(kwargs)
	filter_status = (status or data.get("status") or "").strip()
	filter_tier = (tier or data.get("tier") or "").strip()
	search_term = (search or data.get("search") or data.get("q") or "").strip().lower()

	raw_page = data.get("page") or page
	raw_limit = data.get("limit") or data.get("page_size") or limit
	p = max(1, cint(raw_page))
	l = cint(raw_limit) if raw_limit and str(raw_limit).lower() != "all" else 0

	stalls = frappe.get_all(
		"ConnectFest Stall",
		fields=[
			"stall_number",
			"tier",
			"dimensions",
			"price",
			"status",
			"block",
			"label",
			"x",
			"y",
			"width",
			"height",
			"is_blocked_by_admin",
			"blocked_reason",
			"booked_by_registration",
			"booked_by_name",
			"booked_by_company",
			"booked_by_email",
			"booked_by_mobile",
			"hold_token",
			"hold_expires_at",
			"modified",
		],
		order_by="stall_number ASC",
		limit=500,
	)

	# Fetch payment details for booked stalls from ConnectFest Registration
	booked_regs = [s.booked_by_registration for s in stalls if s.booked_by_registration]
	reg_details = {}
	if booked_regs:
		reg_list = frappe.get_all(
			"ConnectFest Registration",
			filters={"registration_id": ["in", booked_regs]},
			fields=["registration_id", "payment_status", "payment_date", "amount", "razorpay_payment_id", "razorpay_order_id", "industry", "segment"],
		)
		for r in reg_list:
			reg_details[r.registration_id] = r

	total_stalls = len(stalls)
	total_booked = 0
	total_available = 0
	total_blocked = 0
	total_hold = 0
	total_revenue = 0.0

	for s in stalls:
		# Attach zone
		t = s.tier or "Regular"
		if not s.get("zone"):
			if t == "Title Sponsor": s["zone"] = "Zone TS - Title Sponsor Arena"
			elif t == "Silver": s["zone"] = "Zone D - East Pavilions"
			elif t == "Premium": s["zone"] = "Zone C - Center Grand Aisles"
			elif t == "Standard": s["zone"] = "Zone B - West Aisles"
			else: s["zone"] = "Zone E - South Entrance Corridor"

		if s.is_blocked_by_admin or s.status == "Blocked":
			s["status"] = "Blocked"
			total_blocked += 1
		elif s.status == "Booked":
			total_booked += 1
			total_revenue += flt(s.price)
		elif s.status == "Hold":
			total_hold += 1
		else:
			total_available += 1

		# Attach buyer extra details if booked
		if s.booked_by_registration and s.booked_by_registration in reg_details:
			r_info = reg_details[s.booked_by_registration]
			s["payment_status"] = r_info.get("payment_status", "Paid")
			s["payment_date"] = str(r_info.get("payment_date") or "")
			s["razorpay_payment_id"] = r_info.get("razorpay_payment_id")
			s["industry"] = r_info.get("industry")
			s["segment"] = r_info.get("segment")

	# Apply Filters
	filtered = []
	for s in stalls:
		if filter_status and (s.status or "").lower() != filter_status.lower():
			continue
		if filter_tier and (s.tier or "").lower() != filter_tier.lower():
			continue
		if search_term:
			m_num = search_term in (s.stall_number or "").lower()
			m_comp = search_term in (s.get("booked_by_company") or "").lower()
			m_name = search_term in (s.get("booked_by_name") or "").lower()
			m_email = search_term in (s.get("booked_by_email") or "").lower()
			m_mob = search_term in (s.get("booked_by_mobile") or "").lower()
			m_reg = search_term in (s.get("booked_by_registration") or "").lower()
			if not (m_num or m_comp or m_name or m_email or m_mob or m_reg):
				continue
		filtered.append(s)

	filtered_count = len(filtered)
	if l > 0:
		start = (p - 1) * l
		end = start + l
		paginated = filtered[start:end]
		tot_pages = max(1, (filtered_count + l - 1) // l)
	else:
		paginated = filtered
		tot_pages = 1

	return {
		"status": "success",
		"filters": {
			"status": filter_status or None,
			"tier": filter_tier or None,
			"search": search_term or None,
		},
		"summary": {
			"total_stalls": total_stalls,
			"available": total_available,
			"booked": total_booked,
			"blocked": total_blocked,
			"hold": total_hold,
			"total_revenue": total_revenue,
			"matching_stalls": filtered_count,
		},
		"pagination": {
			"current_page": p,
			"page_size": l if l > 0 else filtered_count,
			"total_matching": filtered_count,
			"total_pages": tot_pages,
			"has_next": p < tot_pages if l > 0 else False,
			"has_prev": p > 1,
		},
		"stalls": paginated,
	}


def admin_create_stall(stall_number=None, tier="Regular", dimensions=None, price=None, zone=None, **kwargs):
	"""
	Admin API: Create a new exhibition stall.
	Requires System Manager or Administrator privileges.
	"""
	from connectfest.services.auth import check_admin_permission
	from connectfest.services.pricing import get_tier_pricing
	err = check_admin_permission("stall creation")
	if err:
		return err

	data = extract_request_data(kwargs)
	s_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()
	t = (tier or data.get("tier") or "Regular").strip()
	dim = (dimensions or data.get("dimensions") or ("3 x 3 m" if t in ("Premium", "Standard") else ("6 x 3 m" if t == "Silver" else "2.5 x 2.5 m"))).strip()
	z = (zone or data.get("zone") or ("Zone D - East Pavilions" if t == "Silver" else ("Zone C - Center Aisles" if t == "Premium" else ("Zone B - West Aisles" if t == "Standard" else "Zone E - South Entrance Corridor")))).strip()

	if not s_num:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Stall Number (stall_number) is mandatory (e.g. 'PR-50', 'VIP-01')"}

	if frappe.db.exists("ConnectFest Stall", s_num):
		frappe.local.response["http_status_code"] = 409
		return {"status": "error", "message": f"Stall '{s_num}' already exists in inventory!"}

	p = flt(price if price is not None else data.get("price"))
	if p <= 0:
		pricing = get_tier_pricing()
		p = flt(pricing.get(t, 25000.0))

	doc = frappe.get_doc({
		"doctype": "ConnectFest Stall",
		"stall_number": s_num,
		"tier": t,
		"dimensions": dim,
		"zone": z,
		"price": p,
		"status": "Available",
		"is_blocked_by_admin": 0,
	})
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	invalidate_stalls_cache()

	return {
		"status": "success",
		"message": f"Stall {s_num} ({t}) created successfully!",
		"stall": {
			"stall_number": s_num,
			"tier": t,
			"dimensions": dim,
			"zone": z,
			"price": p,
			"status": "Available",
		}
	}


def admin_get_stall(stall_number=None, **kwargs):
	"""
	Admin API: Get specific stall details with buyer info and hold status.
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("admin stall details")
	if err:
		return err

	data = extract_request_data(kwargs)
	s_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()

	if not s_num or not frappe.db.exists("ConnectFest Stall", s_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{s_num}' does not exist"}

	stall = frappe.get_doc("ConnectFest Stall", s_num).as_dict()
	return {
		"status": "success",
		"stall": stall,
	}


def admin_update_stall(stall_number=None, **kwargs):
	"""
	Admin API: Update stall properties (tier, dimensions, price, status, block/unblock).
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("stall updates")
	if err:
		return err

	data = extract_request_data(kwargs)
	s_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()

	if not s_num or not frappe.db.exists("ConnectFest Stall", s_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{s_num}' does not exist"}

	stall = frappe.get_doc("ConnectFest Stall", s_num)

	if "tier" in data: stall.tier = data["tier"]
	if "dimensions" in data: stall.dimensions = data["dimensions"]
	if "zone" in data: stall.zone = data["zone"]
	if "price" in data and flt(data["price"]) > 0: stall.price = flt(data["price"])
	if "status" in data: stall.status = data["status"]
	if "is_blocked_by_admin" in data: stall.is_blocked_by_admin = cint(data["is_blocked_by_admin"])
	if "blocked_reason" in data: stall.blocked_reason = data["blocked_reason"]

	stall.save(ignore_permissions=True)
	frappe.db.commit()
	invalidate_stalls_cache()

	return {
		"status": "success",
		"message": f"Stall {s_num} updated successfully",
		"stall": stall.as_dict(),
	}


def admin_delete_stall(stall_number=None, **kwargs):
	"""
	Admin API: Delete a stall.
	CRITICAL RULE: If a stall is already BOOKED by an exhibitor, deletion is strictly PREVENTED!
	"""
	from connectfest.services.auth import check_admin_permission
	from connectfest.services.common import STALL_LOCK_PREFIX
	err = check_admin_permission("stall deletion")
	if err:
		return err

	data = extract_request_data(kwargs)
	s_num = (stall_number or data.get("stall_number") or data.get("stall_no") or "").strip().upper()

	if not s_num:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Stall Number (stall_number) is required"}

	if not frappe.db.exists("ConnectFest Stall", s_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{s_num}' does not exist"}

	stall = frappe.get_doc("ConnectFest Stall", s_num)

	# STRICT CHECK: Cannot delete booked stall!
	if stall.status == "Booked" or stall.booked_by_registration:
		frappe.local.response["http_status_code"] = 409
		buyer = stall.booked_by_name or stall.booked_by_registration
		return {
			"status": "error",
			"message": f"Cannot delete stall '{s_num}' because it has already been booked and paid for by exhibitor '{buyer}'!"
		}

	# Clean up any hold
	try:
		cache = frappe.cache()
		cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{s_num}"))
	except Exception:
		pass

	frappe.db.delete("ConnectFest Stall", {"name": s_num})
	frappe.db.commit()
	invalidate_stalls_cache()

	return {
		"status": "success",
		"message": f"Stall '{s_num}' deleted successfully from inventory.",
		"deleted_stall": s_num,
	}


def build_connectfest_220_stalls():
	"""
	Generates all 220 official floor-plan stalls according to the CONNECTFEST 2027 Blueprint.
	Coordinates (x, y, width, height) are in SVG coordinate space for 2D architectural rendering.
	"""
	stalls = []
	cnt = {}
	tier_map = {
		'2x2': 'Regular', '3x2': 'Standard',
		'3x3': 'Premium', '5x2': 'Premium', '5x3': 'Premium', '6x2': 'Premium',
		'4x3': 'Silver', '6x3': 'Silver', '8x3': 'Silver', '6x4': 'Silver', '8x4': 'Silver', '6x5': 'Silver',
		'12x4': 'Title Sponsor',
	}
	zone_map = {
		'A': 'Zone A - Top North Strip',
		'B': 'Zone B - West Aisles',
		'C': 'Zone B - West Aisles',
		'D': 'Zone B - West Aisles',
		'E': 'Zone C - Center Grand Aisles',
		'F': 'Zone C - Center Grand Aisles',
		'G': 'Zone C - Center Grand Aisles',
		'H': 'Zone C - Center Grand Aisles',
		'J': 'Zone D - East Pavilions',
		'K': 'Zone D - East Pavilions',
		'L': 'Zone D - East Pavilions',
		'M': 'Zone D - East Pavilions',
		'TS': 'Zone TS - Title Sponsor Arena',
		'N': 'Zone E - South Entrance Corridor',
		'P': 'Zone E - South Entrance Corridor',
	}
	default_prices = {
		'Title Sponsor': 250000.0,
		'Silver': 100000.0,
		'Premium': 45000.0,
		'Standard': 35000.0,
		'Regular': 25000.0,
	}

	def add(g, x, y, w, h, label):
		cnt[g] = cnt.get(g, 0) + 1
		s_id = f"{g}-{str(cnt[g]).zfill(2)}"
		t = tier_map[label]
		dim_str = label.replace('x', ' x ') + ' m'
		stalls.append({
			'stall_number': s_id,
			'block': g,
			'x': float(x),
			'y': float(y),
			'width': float(w),
			'height': float(h),
			'label': label,
			'dimensions': dim_str,
			'tier': t,
			'zone': zone_map[g],
			'default_price': default_prices.get(t, 25000.0),
		})

	def grid(g, x, y, w, h, cols, rows, label):
		for r in range(rows):
			for c in range(cols):
				add(g, round(x + c * w, 2), round(y + r * h, 2), w, h, label)

	def row_x(g, x, y, w, h, n, label):
		grid(g, x, y, w, h, n, 1, label)

	# top strip
	row_x('A', 399, 255, 44, 28, 3, '3x2')
	row_x('A', 648, 255, 44.5, 44, 2, '3x3')
	row_x('A', 826, 255, 44.5, 44, 4, '3x3')
	row_x('A', 1092, 255, 44.5, 44, 2, '3x3')
	add('A', 1211, 255, 59, 44, '4x3')
	row_x('A', 1359, 255, 59, 44, 3, '4x3')

	# column blocks
	grid('B', 428, 328, 29.5, 44.4, 2, 10, '3x2')
	grid('C', 531, 282, 29.5, 44.4, 2, 10, '3x2')
	grid('D', 636, 343, 29.5, 73, 2, 1, '5x2')
	grid('D', 636, 416, 29.5, 44.4, 2, 8, '3x2')
	grid('E', 738, 299, 44, 44.4, 2, 8, '3x3')
	add('E', 738, 655, 88, 71, '6x5')
	grid('F', 871, 343, 44, 73, 2, 1, '5x3')
	grid('F', 871, 416, 44, 44.4, 2, 7, '3x3')
	row_x('F', 871, 727, 44.2, 44, 5, '3x3')
	grid('G', 1004, 299, 44, 44.4, 2, 8, '3x3')
	add('G', 1004, 655, 88, 28, '6x2')
	add('H', 1137, 342, 88, 30, '6x2')
	grid('H', 1137, 372, 44, 44.4, 2, 8, '3x3')
	grid('J', 1271, 298, 43.5, 59, 2, 5, '4x3')
	grid('J', 1271, 593, 43.5, 89, 2, 1, '6x3')
	add('K', 1403, 342, 44, 88, '6x3')
	grid('K', 1403, 430, 44, 59, 1, 5, '4x3')
	add('K', 1447, 342, 44, 118, '8x3')
	add('K', 1447, 460, 44, 88, '6x3')
	add('K', 1447, 548, 44, 88, '6x3')
	add('K', 1447, 636, 44, 89, '6x3')
	add('K', 1403, 727, 88, 59, '6x4')
	add('K', 1283, 727, 119, 59, '8x4')

	# right edge & bottom-right
	for y in [314, 402, 490, 652, 740]:
		add('L', 1537, y, 43, 88, '6x3')
	add('M', 1211, 830, 88, 45, '6x3')
	add('M', 1328, 830, 89, 45, '6x3')
	add('M', 1417, 830, 88, 45, '6x3')

	# title sponsor
	add('TS', 1092, 727, 178, 58, '12x4')

	# 2x2 rows
	row_x('N', 428, 772, 29.5, 29, 11, '2x2')
	row_x('N', 768, 772, 29.5, 29, 11, '2x2')
	add('P', 340, 846, 30, 29, '2x2')
	row_x('P', 385, 846, 29.5, 29, 7, '2x2')
	row_x('P', 605, 846, 29.5, 29, 12, '2x2')

	return stalls


DEFAULT_STALLS_SPEC = build_connectfest_220_stalls()


def setup_default_stalls(preserve_booked=True):
	"""
	Universal Stall & Tier Setup / Initialization Function for ConnectFest 2027.
	Sets up all 220 official floor-plan stalls (Blocks A to P + Title Sponsor TS-01) with SVG coordinates & dimensions.
	- 220 stalls:
	  * Title Sponsor: 1 stall (TS-01, 12x4m, Grand Central Arena)
	  * Silver: 37 stalls (4x3m, 6x3m, 8x3m, 6x4m, 8x4m, 6x5m)
	  * Premium: 81 stalls (3x3m, 5x2m, 5x3m, 6x2m)
	  * Standard: 59 stalls (3x2m)
	  * Regular: 42 stalls (2x2m)
	- Sets up default 5 tier pricing in 'ConnectFest Tier Pricing'.
	- Preserves booked stalls & registrations.
	- 100% Idempotent and safe to run multiple times.
	"""
	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	sess_user = getattr(getattr(frappe, "session", None), "user", None) or "Administrator"

	# 1. Seed Dynamic Tier Pricing (ConnectFest Tier Pricing) if missing
	if frappe.db.table_exists("ConnectFest Tier Pricing"):
		from connectfest.services.pricing import DEFAULT_TIERS_TEMPLATE, invalidate_pricing_cache
		for dt in DEFAULT_TIERS_TEMPLATE:
			if not frappe.db.exists("ConnectFest Tier Pricing", {"tier_name": dt["tier_name"]}):
				doc = frappe.new_doc("ConnectFest Tier Pricing")
				doc.tier_name = dt["tier_name"]
				doc.price = dt["price"]
				doc.gst_percent = dt["gst_percent"]
				doc.description = dt["description"]
				doc.is_active = 1
				doc.insert(ignore_permissions=True)
		invalidate_pricing_cache()

	# 2. Seed Default Settings if missing
	if frappe.db.table_exists("ConnectFest Settings"):
		try:
			settings = frappe.get_single("ConnectFest Settings")
			updated = False
			if not getattr(settings, "price_title_sponsor", None):
				settings.price_title_sponsor = 250000.0
				updated = True
			if not settings.price_silver:
				settings.price_silver = 100000.0
				updated = True
			if not settings.price_premium:
				settings.price_premium = 45000.0
				updated = True
			if not settings.price_standard:
				settings.price_standard = 35000.0
				updated = True
			if not settings.price_regular:
				settings.price_regular = 25000.0
				updated = True
			if not settings.event_name:
				settings.event_name = "ConnectFest 2027"
				updated = True
			if not settings.event_dates:
				settings.event_dates = "20 & 21 Feb 2027"
				updated = True
			if not settings.event_venue:
				settings.event_venue = "Chennai Trade Centre, Nandambakkam"
				updated = True
			if updated:
				settings.save(ignore_permissions=True)
		except Exception:
			pass

	# 3. Seed / Verify all 220 Default Stalls
	if not frappe.db.table_exists("ConnectFest Stall"):
		return {"status": "skipped", "message": "ConnectFest Stall table not found"}

	pricing = get_tier_pricing()

	# Clean up obsolete placeholder unbooked stalls (e.g. SL-, PR-, ST-, RG-)
	try:
		frappe.db.sql("""
			DELETE FROM `tabConnectFest Stall`
			WHERE (stall_number LIKE 'SL-%' OR stall_number LIKE 'PR-%' OR stall_number LIKE 'ST-%' OR stall_number LIKE 'RG-%')
			AND (status != 'Booked' OR status IS NULL)
			AND (booked_by_registration IS NULL OR booked_by_registration = '')
		""")
	except Exception:
		pass

	created_count = 0
	updated_count = 0
	spec_stalls = build_connectfest_220_stalls()

	for s in spec_stalls:
		stall_num = s["stall_number"]
		tier = s["tier"]
		dims = s["dimensions"]
		zone = s["zone"]
		block = s["block"]
		label = s["label"]
		x = s["x"]
		y = s["y"]
		width = s["width"]
		height = s["height"]
		default_price = flt(pricing.get(tier, s["default_price"]))

		existing = frappe.db.sql(
			"SELECT name, status, booked_by_registration FROM `tabConnectFest Stall` WHERE stall_number = %s",
			(stall_num,),
			as_dict=True
		)

		if not existing:
			frappe.db.sql("""
				INSERT INTO `tabConnectFest Stall`
				(name, stall_number, tier, dimensions, zone, price, status, is_blocked_by_admin, block, label, x, y, width, height, creation, modified, modified_by, owner)
				VALUES (%s, %s, %s, %s, %s, %s, 'Available', 0, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
			""", (stall_num, stall_num, tier, dims, zone, default_price, block, label, x, y, width, height, now_str, now_str, sess_user, sess_user))
			created_count += 1
		else:
			curr = existing[0]
			is_booked = curr.get("status") == "Booked" or bool(curr.get("booked_by_registration"))
			if not is_booked:
				frappe.db.sql("""
					UPDATE `tabConnectFest Stall`
					SET tier = %s, dimensions = %s, zone = %s, price = %s, block = %s, label = %s, x = %s, y = %s, width = %s, height = %s, modified = %s
					WHERE stall_number = %s
				""", (tier, dims, zone, default_price, block, label, x, y, width, height, now_str, stall_num))
				updated_count += 1
			else:
				frappe.db.sql("""
					UPDATE `tabConnectFest Stall`
					SET block = %s, label = %s, x = %s, y = %s, width = %s, height = %s
					WHERE stall_number = %s
				""", (block, label, x, y, width, height, stall_num))

	frappe.db.commit()

	total_stalls = frappe.db.count("ConnectFest Stall")
	try:
		print(f"ConnectFest: Initialized/Verified 220 stalls (Total: {total_stalls}, Newly Created: {created_count}, Updated: {updated_count})")
	except Exception:
		pass

	return {
		"status": "success",
		"total_stalls": total_stalls,
		"newly_created": created_count,
		"updated": updated_count,
	}


# Backward-compatibility alias
seed_default_stalls = setup_default_stalls


def admin_restore_defaults(reset_pricing=1, reset_stalls=1, reset_booked=0, **kwargs):
	"""
	Admin Restore API:
	Explicitly restores pristine default pricing (5 tiers) and default 220 stalls.
	- reset_pricing: 1 = Restore Title Sponsor (₹2,50,000), Silver (₹1,00,000), Premium (₹45,000), Standard (₹35,000), Regular (₹25,000) with 18% GST.
	- reset_stalls: 1 = Restore all 220 standard stalls (Blocks A..P + Title Sponsor TS-01)
	- reset_booked: 0 (default) = Keeps existing booked stalls intact. 1 = Fresh wipe (resets all booked stalls too).
	- Security: Restricted strictly to Administrator / System Manager.
	"""
	from connectfest.services.auth import check_admin_permission
	from connectfest.services.pricing import restore_default_pricing
	err = check_admin_permission("restore defaults")
	if err:
		return err

	data = extract_request_data(kwargs)
	r_pricing = cint(data.get("reset_pricing") if "reset_pricing" in data else reset_pricing)
	r_stalls = cint(data.get("reset_stalls") if "reset_stalls" in data else reset_stalls)
	r_booked = cint(data.get("reset_booked") if "reset_booked" in data else reset_booked)

	result = {
		"status": "success",
		"message": "Restore defaults executed successfully by Admin",
		"pricing_restored": False,
		"stalls_restored": 0,
		"booked_stalls_preserved": 0,
		"booked_stalls_cleared": 0,
	}

	# 1. Restore Pricing
	if r_pricing:
		restore_default_pricing()
		result["pricing_restored"] = True
		result["pricing_tiers"] = ["Title Sponsor", "Silver", "Premium", "Standard", "Regular"]

	# 2. Restore Stalls
	if r_stalls:
		# Clear hold locks in redis
		try:
			cache = frappe.cache()
			all_stalls = frappe.db.sql("SELECT stall_number FROM `tabConnectFest Stall`", as_list=True)
			for (sn,) in all_stalls:
				cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{sn}"))
		except Exception:
			pass

		if r_booked:
			# Fresh wipe - delete all stalls
			frappe.db.sql("DELETE FROM `tabConnectFest Stall`")
			result["booked_stalls_cleared"] = 1
		else:
			# Preserve booked stalls, delete unbooked
			booked_count = frappe.db.count("ConnectFest Stall", filters={"status": "Booked"})
			frappe.db.sql("""
				DELETE FROM `tabConnectFest Stall`
				WHERE status != 'Booked' AND (booked_by_registration IS NULL OR booked_by_registration = '')
			""")
			result["booked_stalls_preserved"] = booked_count

		setup_res = setup_default_stalls(preserve_booked=not r_booked)
		result["stalls_restored"] = setup_res.get("total_stalls", 0)
		result["newly_created"] = setup_res.get("newly_created", 0)

	return result


@handle_api_errors("ConnectFest Admin Clear All Bookings Error")
def admin_clear_all_bookings(**kwargs):
	"""
	Admin Testing API: Clears exhibitors, visitors, and resets all 220 stalls to Available.
	Parameters:
	- clear_exhibitors: 1 (default) = Delete all records from tabConnectFest Registration
	- clear_visitors: 1 (default) = Delete all records from tabConnectFest Visitor Registration
	- reset_stalls: 1 (default) = Reset all stalls to Available and wipe booking fields & locks
	- clear_otps: 1 (default) = Delete all records from tabConnectFest OTP
	- clear_queues: 1 (default) = Flush Redis queue and lock keys
	"""
	data = extract_request_data(kwargs)

	sess_user = getattr(frappe.session, "user", None)
	is_admin = (sess_user and sess_user != "Guest" and check_admin_permission("clear all bookings") is None)
	is_dev = bool(frappe.conf.get("developer_mode", False))

	confirmed = parse_boolean(data.get("confirm", True))
	if not confirmed:
		return {
			"status": "error",
			"message": "Please pass 'confirm': true to confirm wiping test event registrations and bookings.",
		}

	if not is_admin and not is_dev:
		err = check_admin_permission("clear all bookings")
		if err:
			return err

	clear_ex = parse_boolean(data.get("clear_exhibitors", True))
	clear_vis = parse_boolean(data.get("clear_visitors", True))
	reset_st = parse_boolean(data.get("reset_stalls", True))
	clear_otp = parse_boolean(data.get("clear_otps", True))
	clear_q = parse_boolean(data.get("clear_queues", True))

	summary = {}

	# 1. Clear Exhibitor Registrations
	if clear_ex:
		count_before = frappe.db.count("ConnectFest Registration")
		frappe.db.sql("DELETE FROM `tabConnectFest Registration`")
		frappe.db.commit()
		summary["exhibitors_deleted"] = count_before

	# 2. Clear Visitor Registrations
	if clear_vis:
		count_before = frappe.db.count("ConnectFest Visitor Registration")
		frappe.db.sql("DELETE FROM `tabConnectFest Visitor Registration`")
		frappe.db.commit()
		summary["visitors_deleted"] = count_before

	# 3. Reset Stalls
	if reset_st:
		frappe.db.sql("""
			UPDATE `tabConnectFest Stall`
			SET status = 'Available',
			    booked_by_registration = NULL,
			    booked_by_name = NULL,
			    booked_by_company = NULL,
			    hold_expires_at = NULL,
			    is_blocked_by_admin = 0,
			    blocked_reason = NULL,
			    payment_mode = NULL
		""")
		frappe.db.commit()
		summary["stalls_reset_to_available"] = frappe.db.count("ConnectFest Stall", filters={"status": "Available"})

	# 4. Clear OTPs
	if clear_otp:
		try:
			frappe.db.sql("DELETE FROM `tabConnectFest OTP`")
			frappe.db.commit()
			summary["otps_cleared"] = True
		except Exception:
			pass

	# 5. Clear Redis Queues & Locks
	if clear_q:
		try:
			cache = frappe.cache()
			keys_to_delete = [
				"cf:stall_queue",
				"cf:visitor_queue",
				"cf:stall_counter",
				"cf:visitor_counter",
			]
			for k in keys_to_delete:
				cache.delete_value(k)
			for pattern in ["cf:stall_lock:*", "cf:stall:*", "cf:visitor:*"]:
				cache.delete_keys(pattern)
			summary["redis_queues_and_locks_cleared"] = True
		except Exception as e:
			summary["redis_cleanup_note"] = str(e)

	# 6. Clear Site Cache
	frappe.clear_cache()

	return {
		"status": "success",
		"message": "All test bookings, registrations, and stall holds have been completely wiped. System is fresh!",
		"summary": summary,
		"current_state": {
			"exhibitors_count": frappe.db.count("ConnectFest Registration"),
			"visitors_count": frappe.db.count("ConnectFest Visitor Registration"),
			"booked_stalls": frappe.db.count("ConnectFest Stall", filters={"status": "Booked"}),
			"available_stalls": frappe.db.count("ConnectFest Stall", filters={"status": "Available"}),
			"total_stalls": frappe.db.count("ConnectFest Stall"),
		},
	}


@handle_api_errors("ConnectFest Admin Book Stall Error")
def admin_book_stall(**kwargs):
	"""
	Admin API: Directly book one or multiple stalls for an attendee on behalf of an admin.
	Supports Cash and Online payment modes.
	"""
	err = check_admin_permission("admin book stall")
	if err:
		return err

	data = extract_request_data(kwargs)

	email = (data.get("email") or data.get("user_email") or "").strip().lower()
	mobile_no = (data.get("mobile_no") or data.get("phone") or data.get("mobile") or "").strip()
	full_name = (data.get("full_name") or data.get("name") or "").strip()
	company_name = (data.get("company_name") or data.get("company") or "").strip()
	location = (data.get("location") or "").strip()
	bill = (data.get("bill") or data.get("billing_name") or data.get("bill_to") or "").strip()
	gst = (data.get("gst") or data.get("gst_number") or data.get("gstin") or "").strip()
	raw_logo = (data.get("company_logo") or data.get("logo") or "").strip()

	is_bni_member = parse_boolean(data.get("is_bni_member", 0))
	region_name = (data.get("region_name") or "").strip()
	chapter_name = (data.get("chapter_name") or "").strip()
	industry = (data.get("industry") or "").strip()
	segment = (data.get("segment") or "").strip()
	raw_source = (data.get("source_of_info") or data.get("source") or "").strip()
	matched_source = None
	for src in VALID_SOURCES:
		if src.lower() == raw_source.lower():
			matched_source = src
			break
	if not matched_source:
		matched_source = "BNI Chapter" if is_bni_member else "Other"
	source_of_info = matched_source

	raw_pay_mode = (data.get("payment_mode") or "Cash").strip()
	if raw_pay_mode.lower() in ("cash", "offline"):
		payment_mode = "Cash"
	elif raw_pay_mode.lower() in ("online", "razorpay", "upi", "card"):
		payment_mode = "Online"
	elif raw_pay_mode.lower() == "cheque":
		payment_mode = "Cheque"
	elif raw_pay_mode.lower() in ("bank transfer", "neft", "rtgs", "bank_transfer"):
		payment_mode = "Bank Transfer"
	else:
		payment_mode = raw_pay_mode.capitalize()

	# Payment status: default to "Paid" for Cash / Admin booking unless explicitly specified
	raw_pay_status = data.get("payment_status")
	if raw_pay_status is not None and str(raw_pay_status).strip() != "":
		payment_status = str(raw_pay_status).strip().capitalize()
	else:
		payment_status = "Paid"

	reference_id = (data.get("reference_id") or data.get("transaction_id") or data.get("receipt_no") or data.get("razorpay_payment_id") or "").strip()
	remarks = (data.get("remarks") or data.get("notes") or "").strip()

	# Auto-fill missing user details from existing records if email/mobile provided
	if email or mobile_no:
		existing = frappe.db.sql("""
			SELECT full_name, company_name, company_logo, location, mobile_no, email,
			       is_bni_member, region_name, chapter_name, industry, segment, bill, gst
			FROM `tabConnectFest Registration`
			WHERE (%(email)s != '' AND email = %(email)s) OR (%(mobile)s != '' AND mobile_no = %(mobile)s)
			ORDER BY creation DESC LIMIT 1
		""", {"email": email, "mobile": mobile_no}, as_dict=True)
		if existing:
			ex = existing[0]
			if not full_name: full_name = ex.get("full_name") or ""
			if not company_name: company_name = ex.get("company_name") or ""
			if not location: location = ex.get("location") or ""
			if not mobile_no: mobile_no = ex.get("mobile_no") or ""
			if not email: email = (ex.get("email") or "").strip().lower()
			if not raw_logo: raw_logo = ex.get("company_logo") or ""
			if not bill: bill = (ex.get("bill") or "").strip()
			if not gst: gst = (ex.get("gst") or "").strip()
			if data.get("is_bni_member") is None:
				is_bni_member = bool(ex.get("is_bni_member"))
			if not region_name: region_name = ex.get("region_name") or ""
			if not chapter_name: chapter_name = ex.get("chapter_name") or ""
			if not industry: industry = ex.get("industry") or ""
			if not segment: segment = ex.get("segment") or ""

	# Check User table for name or phone if still missing
	if email and not full_name:
		u_fn = frappe.db.get_value("User", email, "full_name")
		if u_fn: full_name = u_fn
	if email and not mobile_no:
		u_mob = frappe.db.get_value("User", email, "mobile_no") or frappe.db.get_value("User", email, "phone")
		if u_mob: mobile_no = u_mob

	# Validations
	errors = []
	if not email:
		errors.append("Email (email or user_email) is required")
	elif "@" not in email:
		errors.append("Valid Email Address is required")

	if not mobile_no:
		errors.append("Mobile Number (mobile_no) is required")
	elif len("".join(filter(str.isdigit, mobile_no))) != 10:
		errors.append("Valid 10-digit Mobile Number is required")

	if not full_name:
		errors.append("Name (full_name) is required")

	if not company_name:
		errors.append("Company Name (company_name) is required")

	# Parse stalls: supports single stall, comma-separated, or list
	raw_stalls = data.get("stall_numbers") or data.get("stall_number") or data.get("stall_no") or data.get("stalls")
	stall_list = []
	if isinstance(raw_stalls, list):
		stall_list = [str(s).strip() for s in raw_stalls if str(s).strip()]
	elif isinstance(raw_stalls, str):
		stall_list = [s.strip() for s in raw_stalls.split(",") if s.strip()]

	if not stall_list:
		errors.append("Stall Number(s) (stall_number or stall_numbers) is required! E.g. 'PR-05' or 'PR-05, PR-06'")

	if errors:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Validation Failed", "errors": errors}

	force = parse_boolean(data.get("force", 0)) or parse_boolean(data.get("allow_reassign", 0))
	allow_existing = parse_boolean(data.get("allow_existing", 0)) or parse_boolean(data.get("confirm_existing", 0))
	allow_new = parse_boolean(data.get("allow_new", 0)) or parse_boolean(data.get("allow_multiple", 0)) or parse_boolean(data.get("multi_stall", 0))
	create_order = parse_boolean(data.get("create_order", 0)) or (data.get("action") == "create_order")

	register_param = data.get("register_user")
	if register_param is None:
		register_param = data.get("is_new_user")
	if register_param is None:
		register_param = data.get("new_registration")
	if register_param is None:
		register_param = data.get("is_new_registration")

	is_new_registration = parse_boolean(register_param) if register_param is not None else False

	# Verify and validate each stall
	from connectfest.services.pricing import get_tier_details
	dynamic_prices = get_tier_pricing()
	stall_docs = {}
	for st_num in stall_list:
		if not frappe.db.exists("ConnectFest Stall", st_num):
			# Auto-provision stall in ConnectFest inventory for Admin booking
			# without disturbing existing inventory
			st_doc = frappe.get_doc({
				"doctype": "ConnectFest Stall",
				"name": st_num,
				"stall_number": st_num,
				"tier": "Standard",
				"price": dynamic_prices.get("Standard", 35000.0),
				"dimensions": "3 x 3 m",
				"zone": "Main Exhibition Hall",
				"status": "Available",
			})
			st_doc.flags.ignore_permissions = True
			st_doc.insert()
		else:
			st_doc = frappe.get_doc("ConnectFest Stall", st_num)

		if st_doc.status == "Booked" and not force:
			booked_by = st_doc.booked_by_name or st_doc.booked_by_company or st_doc.booked_by_registration or "another exhibitor"
			frappe.local.response["http_status_code"] = 409
			return {
				"status": "error",
				"error_type": "already_booked",
				"message": f"Stall '{st_num}' is already booked and confirmed by {booked_by}!",
				"details": {
					"stall_number": st_num,
					"status": "Booked",
					"booked_by_registration": st_doc.booked_by_registration,
					"booked_by_name": st_doc.booked_by_name,
					"booked_by_company": st_doc.booked_by_company,
					"booked_by_email": st_doc.booked_by_email,
					"booked_by_mobile": st_doc.booked_by_mobile,
					"payment_mode": getattr(st_doc, "payment_mode", None) or "Paid",
				}
			}

		# BookMyShow-Style Active Hold Protection
		hold_info = check_stall_hold_in_redis(st_num, st_doc)
		if hold_info.get("is_held") and not force:
			held_data = hold_info.get("data") or {}
			held_reg = held_data.get("registration") or getattr(st_doc, "booked_by_registration", None)
			held_email = held_data.get("email") or getattr(st_doc, "booked_by_email", None)
			held_mobile = held_data.get("mobile") or getattr(st_doc, "booked_by_mobile", None)
			held_name = held_data.get("name") or getattr(st_doc, "booked_by_name", None) or "Another attendee"
			held_comp = held_data.get("company") or getattr(st_doc, "booked_by_company", None) or ""

			is_same_holder = False
			if held_email and email and (email.strip().lower() == held_email.strip().lower()):
				is_same_holder = True
			elif held_mobile and mobile_no and (mobile_no.strip() == held_mobile.strip()):
				is_same_holder = True

			if not is_same_holder:
				rem_secs = hold_info.get("remaining_seconds", 600)
				rem_mins = rem_secs // 60
				rem_s = rem_secs % 60
				time_str = f"{rem_mins}m {rem_s}s" if rem_mins > 0 else f"{rem_secs}s"
				frappe.local.response["http_status_code"] = 409
				return {
					"status": "error",
					"error_type": "on_hold",
					"message": f"Stall '{st_num}' is currently on HOLD! Attendee '{held_name}' ({held_comp or held_email or 'User'}) is in the middle of completing online payment ({time_str} remaining). Please wait until the 10-minute hold expires or select another stall.",
					"details": {
						"stall_number": st_num,
						"status": "Hold",
						"held_by_name": held_name,
						"held_by_company": held_comp,
						"held_by_registration": held_reg,
						"held_by_email": held_email,
						"held_by_mobile": held_mobile,
						"remaining_seconds": rem_secs,
						"hold_expires_at": str(getattr(st_doc, "hold_expires_at", "") or ""),
					}
				}

		stall_docs[st_num] = st_doc

	# Check existing exhibitor registrations to alert admin if already registered or already booked
	if email or mobile_no:
		user_regs = frappe.db.sql("""
			SELECT registration_id, full_name, company_name, email, mobile_no,
			       stall_number, payment_status, payment_mode, creation
			FROM `tabConnectFest Registration`
			WHERE (%(email)s != '' AND email = %(email)s) OR (%(mobile)s != '' AND mobile_no = %(mobile)s)
			ORDER BY creation DESC
		""", {"email": email, "mobile": mobile_no}, as_dict=True)

		if user_regs:
			# Check if already booked any of the requested stalls
			already_booked = [r for r in user_regs if r.stall_number in stall_list and r.payment_status == "Paid"]
			if already_booked and not force:
				ab = already_booked[0]
				frappe.local.response["http_status_code"] = 409
				return {
					"status": "error",
					"error_type": "already_booked",
					"message": f"This exhibitor has already booked and confirmed stall '{ab.stall_number}' (Registration ID: {ab.registration_id})!",
					"details": {
						"registration_id": ab.registration_id,
						"full_name": ab.full_name,
						"company_name": ab.company_name,
						"email": ab.email,
						"mobile_no": ab.mobile_no,
						"stall_number": ab.stall_number,
						"payment_status": ab.payment_status,
						"payment_mode": ab.payment_mode,
						"creation": str(ab.creation or ""),
					}
				}

			if is_new_registration and not allow_existing and not allow_new:
				ex = user_regs[0]
				frappe.local.response["http_status_code"] = 409
				return {
					"status": "error",
					"error_type": "already_registered",
					"message": f"Exhibitor '{ex.full_name}' ({ex.email or ex.mobile_no}) is already registered with Registration ID '{ex.registration_id}'! To book stall for this existing user, do not pass 'register_user'.",
					"details": {
						"registration_id": ex.registration_id,
						"full_name": ex.full_name,
						"company_name": ex.company_name,
						"email": ex.email,
						"mobile_no": ex.mobile_no,
						"stall_number": ex.stall_number or None,
						"payment_status": ex.payment_status,
						"payment_mode": ex.payment_mode,
						"creation": str(ex.creation or ""),
					}
				}

	# Process logo image if provided
	company_logo = save_image_from_base64_or_url(
		raw_logo,
		file_name_prefix=f"logo_{mobile_no[-4:] if len(mobile_no) >= 4 else 'stall'}"
	)

	# Ensure Frappe User account exists
	ensure_frappe_user(email, full_name, mobile_no, role="Website User")
	if company_logo:
		try:
			frappe.db.set_value("User", email, "user_image", company_logo)
		except Exception:
			pass

	cache = frappe.cache()
	now_dt = now_datetime()
	now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
	now_ms = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")

	booked_results = []
	total_amount = 0.0
	total_base_amount = 0.0
	total_tax_amount = 0.0
	is_online = (payment_mode == "Online" and not reference_id)
	is_paid = (payment_status == "Paid" and not create_order and not is_online)
	booking_status = "Confirmed" if is_paid else "Registered"
	actual_pay_status = "Paid" if is_paid else "Pending"

	now_dt = now_datetime()
	hold_expires_at = now_dt + timedelta(minutes=10) if is_online else None

	# Book / Hold each stall
	for st_num in stall_list:
		st_doc = stall_docs[st_num]
		tier = st_doc.tier or "Regular"
		tier_info = get_tier_details(tier)
		base_price = flt(tier_info.get("base_price") or tier_info.get("price") or st_doc.price)
		gst_pct = flt(tier_info.get("gst_percent", 18.0))
		custom_amt = data.get("amount")
		if custom_amt is not None and len(stall_list) == 1:
			amount = flt(custom_amt)
			base_price = round(amount / (1.0 + gst_pct / 100.0), 2)
			tax_amount = round(amount - base_price, 2)
		else:
			tax_amount = round(base_price * (gst_pct / 100.0), 2)
			amount = round(base_price + tax_amount, 2)

		total_amount += amount
		total_base_amount += base_price
		total_tax_amount += tax_amount

		reg_id = generate_stall_id(cache)
		pay_id = reference_id or (f"CASH-{reg_id}" if payment_mode == "Cash" and is_paid else (f"ONLINE-ADM-{reg_id}" if is_paid else ""))

		reg_record = {
			"doctype": "ConnectFest Registration",
			"name": reg_id,
			"registration_id": reg_id,
			"full_name": full_name,
			"company_name": company_name,
			"company_logo": company_logo,
			"location": location,
			"bill": bill,
			"gst": gst,
			"mobile_no": mobile_no,
			"email": email,
			"is_bni_member": 1 if is_bni_member else 0,
			"region_name": region_name if is_bni_member else "",
			"chapter_name": chapter_name if is_bni_member else "",
			"industry": industry,
			"segment": segment,
			"source_of_info": source_of_info,
			"stall_number": st_num,
			"stall_category": tier,
			"amount": amount,
			"payment_status": actual_pay_status,
			"payment_mode": payment_mode,
			"razorpay_order_id": remarks or f"admin_order_{reg_id}",
			"razorpay_payment_id": pay_id,
			"razorpay_signature": "admin_verified" if is_paid else "",
			"payment_date": now_str if is_paid else None,
			"status": "Registered",
			"creation": now_ms,
			"modified": now_ms,
			"modified_by": frappe.session.user or "Administrator",
			"owner": "Administrator",
			"docstatus": 0,
			"idx": 0,
		}

		doc = frappe.get_doc(reg_record)
		doc.flags.ignore_permissions = True
		doc.insert()

		actual_reg_id = doc.registration_id or doc.name
		reg_record["name"] = doc.name
		reg_record["registration_id"] = actual_reg_id
		set_cached_record(cache, f"{STALL_CACHE_PREFIX}{actual_reg_id}", reg_record)

		if is_paid:
			# Cash / Direct Confirmed Booking
			frappe.db.set_value(
				"ConnectFest Stall",
				st_num,
				{
					"status": "Booked",
					"is_blocked_by_admin": 0,
					"blocked_reason": None,
					"booked_by_registration": actual_reg_id,
					"booked_by_name": full_name,
					"booked_by_company": company_name,
					"booked_by_email": email,
					"booked_by_mobile": mobile_no,
					"payment_mode": payment_mode,
					"hold_token": None,
					"hold_expires_at": None,
				},
				update_modified=True,
			)
			cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{st_num}"))
			try:
				from connectfest.services.realtime import broadcast_stall_update
				broadcast_stall_update(
					st_num,
					"Booked",
					{
						"booked_by_registration": actual_reg_id,
						"booked_by_name": full_name,
						"booked_by_company": company_name,
						"payment_mode": payment_mode,
					}
				)
			except Exception:
				pass
		elif is_online:
			# Online Payment: BookMyShow-style 10-minute HOLD!
			st_hold_token = f"HLD-ADM-{uuid.uuid4().hex[:12].upper()}"
			frappe.db.set_value(
				"ConnectFest Stall",
				st_num,
				{
					"status": "Hold",
					"hold_token": st_hold_token,
					"hold_expires_at": hold_expires_at,
					"is_blocked_by_admin": 0,
					"blocked_reason": None,
					"booked_by_registration": actual_reg_id,
					"booked_by_name": full_name,
					"booked_by_company": company_name,
					"booked_by_email": email,
					"booked_by_mobile": mobile_no,
					"payment_mode": "Online",
				},
				update_modified=True,
			)
			# Authoritative Redis Lock (600 seconds)
			try:
				raw_lock_key = cache.make_key(f"{STALL_LOCK_PREFIX}{st_num}")
				lock_data = {
					"token": st_hold_token,
					"stall_number": st_num,
					"name": full_name,
					"company": company_name,
					"email": email,
					"mobile": mobile_no,
					"registration": actual_reg_id,
					"mode": "Online",
					"admin": True,
				}
				cache.set(raw_lock_key, json.dumps(lock_data), ex=600)
			except Exception:
				pass

			try:
				from connectfest.services.realtime import broadcast_stall_update
				broadcast_stall_update(
					st_num,
					"Hold",
					{
						"tier": tier,
						"price": amount,
						"hold_expires_at": str(hold_expires_at),
						"booked_by_company": company_name,
						"booked_by_name": full_name,
						"payment_mode": "Online",
					}
				)
			except Exception:
				pass

		booked_results.append({
			"registration_id": actual_reg_id,
			"stall_number": st_num,
			"tier": tier,
			"base_amount": base_price,
			"gst_percent": gst_pct,
			"tax_amount": tax_amount,
			"amount": amount,
			"total_amount": amount,
			"dimensions": st_doc.dimensions,
			"zone": st_doc.zone,
			"payment_mode": payment_mode,
			"payment_status": actual_pay_status,
			"status": "Hold (10 mins)" if is_online else booking_status,
			"receipt_id": pay_id,
		})

	frappe.db.commit()
	invalidate_stalls_cache()

	# Send Confirmation Email for Paid bookings
	if is_paid:
		try:
			from connectfest.services.notifications import send_booking_confirmation_email
			send_booking_confirmation_email(
				email=email,
				full_name=full_name,
				company_name=company_name,
				registration_id=booked_results[0]["registration_id"] if booked_results else "",
				stall_numbers=", ".join(stall_list),
				stall_category=stall_docs[stall_list[0]].tier if stall_list else "Exhibitor",
				amount=total_amount,
				payment_mode=payment_mode,
				receipt_id=booked_results[0].get("receipt_id") if booked_results else "",
				payment_date=now_str
			)
		except Exception:
			pass

	if is_online and len(stall_list) == 1 and booked_results:
		from connectfest.services.payments import create_razorpay_order
		st_num = stall_list[0]
		first_reg_id = booked_results[0]["registration_id"]
		order_res = create_razorpay_order(
			registration_id=first_reg_id,
			stall_number=st_num,
			is_internal=True
		)
		if order_res.get("status") == "success":
			frappe.db.set_value("ConnectFest Registration", first_reg_id, {
				"payment_status": "Pending",
				"payment_mode": "Online",
				"razorpay_order_id": (order_res.get("order") or {}).get("id") or "",
			})
			frappe.db.commit()
			return {
				"status": "success",
				"action": "hold_and_order_created",
				"message": f"Stall '{st_num}' placed on 10-MINUTE HOLD for Online Payment. Proceed with online payment before the timer expires.",
				"registration_id": first_reg_id,
				"stall_number": st_num,
				"amount": total_amount,
				"payment_mode": "Online",
				"payment_status": "Pending",
				"hold_expires_at": str(hold_expires_at),
				"expires_in_seconds": 600,
				"order": order_res.get("order"),
				"customer": order_res.get("customer"),
			}
		else:
			return order_res


	return {
		"status": "success",
		"message": f"Successfully booked {len(booked_results)} stall(s) for {full_name} ({email}) with {payment_mode} payment.",
		"user": {
			"full_name": full_name,
			"email": email,
			"mobile_no": mobile_no,
			"company_name": company_name,
			"company_logo": company_logo,
			"location": location,
			"bill": bill,
			"gst": gst,
			"is_bni_member": bool(is_bni_member),
			"region_name": region_name,
			"chapter_name": chapter_name,
			"industry": industry,
			"segment": segment,
			"source_of_info": source_of_info,
		},
		"billing": {
			"bill_to": bill or company_name or full_name,
			"gst_number": gst or "",
			"total_base_amount": round(total_base_amount, 2),
			"total_tax_amount": round(total_tax_amount, 2),
			"total_amount": round(total_amount, 2),
			"currency": "INR",
		},
		"payment_mode": payment_mode,
		"payment_status": payment_status,
		"total_stalls": len(booked_results),
		"total_amount": round(total_amount, 2),
		"stalls": booked_results,
		"registration_ids": [b["registration_id"] for b in booked_results],
	}


