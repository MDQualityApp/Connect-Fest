import uuid
from datetime import timedelta
import frappe
from frappe.utils import now_datetime, flt
from connectfest.services.common import extract_request_data, STALL_LOCK_PREFIX
from connectfest.services.pricing import get_tier_pricing


def get_stalls(**kwargs):
	"""
	Universal Floor Map & Real-Time Inventory API.
	Returns all 195 stalls with zone, dimensions, live booking status, and pricing.
	"""
	try:
		from frappe.utils import cint
		data = extract_request_data(kwargs)
		filter_tier = (data.get("tier") or data.get("stall_category") or "").strip()
		filter_status = (data.get("status") or "").strip().capitalize()
		search_term = (data.get("search") or data.get("query") or "").strip().lower()

		raw_page = data.get("page")
		raw_limit = data.get("limit") or data.get("page_size")
		page = max(1, cint(raw_page)) if raw_page else 1
		limit = cint(raw_limit) if raw_limit and str(raw_limit).lower() != "all" else 0

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
				"booked_by_registration",
				"booked_by_name",
				"booked_by_company",
				"hold_expires_at",
			],
			order_by="stall_number ASC",
			limit=250,
		)

		now_dt = now_datetime()
		cache = frappe.cache()
		pricing = get_tier_pricing()

		total_stalls = len(stalls)
		total_booked = 0
		total_revenue = 0.0

		tier_summary = {
			"Silver": {"total": 0, "available": 0, "booked": 0, "price": pricing.get("Silver", 100000.0)},
			"Premium": {"total": 0, "available": 0, "booked": 0, "price": pricing.get("Premium", 45000.0)},
			"Standard": {"total": 0, "available": 0, "booked": 0, "price": pricing.get("Standard", 35000.0)},
			"Regular": {"total": 0, "available": 0, "booked": 0, "price": pricing.get("Regular", 25000.0)},
		}

		for stall in stalls:
			t = stall.tier or "Regular"
			if t in tier_summary:
				tier_summary[t]["total"] += 1
				tier_price = pricing.get(t, stall.price)
				stall["price"] = tier_price

			# Release expired hold
			if stall.status == "Hold" and stall.hold_expires_at and stall.hold_expires_at <= now_dt:
				frappe.db.set_value("ConnectFest Stall", stall.stall_number, {
					"status": "Available",
					"hold_token": None,
					"hold_expires_at": None,
				}, update_modified=False)
				stall["status"] = "Available"
				cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall.stall_number}"))

			if stall.status == "Booked":
				total_booked += 1
				total_revenue += flt(stall.price)
				if t in tier_summary:
					tier_summary[t]["booked"] += 1
			elif stall.status == "Available" and not stall.is_blocked_by_admin:
				if t in tier_summary:
					tier_summary[t]["available"] += 1

			# Zone assignment
			if t == "Silver":
				stall["zone"] = "Zone D - East Pavilions"
			elif t == "Premium":
				stall["zone"] = "Zone C - Center Aisles"
			elif t == "Standard":
				stall["zone"] = "Zone B - West Aisles"
			else:
				stall["zone"] = "Zone E - South Entrance Corridor"

		frappe.db.commit()

		# Apply Filters
		filtered_stalls = []
		for s in stalls:
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

		return {
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
	except Exception as e:
		frappe.log_error("ConnectFest Get Stalls Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
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

		if not stall_num:
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": "stall_number is required"}

		if not frappe.db.exists("ConnectFest Stall", stall_num):
			frappe.local.response["http_status_code"] = 404
			return {"status": "error", "message": f"Stall '{stall_num}' does not exist"}

		# Enforce 1 User = 1 Stall rule
		if email or mobile_no:
			existing = frappe.db.sql("""
				SELECT registration_id, stall_number, stall_category FROM `tabConnectFest Registration`
				WHERE (email = %(email)s OR mobile_no = %(mobile)s) AND payment_status = 'Paid'
				LIMIT 1
			""", {"email": email, "mobile": mobile_no}, as_dict=True)
			if existing:
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": f"Each exhibitor is limited to 1 stall! You already have an active booked stall: {existing[0].stall_number}."
				}

		stall = frappe.get_doc("ConnectFest Stall", stall_num)

		if stall.is_blocked_by_admin:
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

		now_dt = now_datetime()
		existing_token = data.get("hold_token")
		if stall.status == "Hold" and stall.hold_expires_at and stall.hold_expires_at > now_dt:
			if not existing_token or existing_token != stall.hold_token:
				frappe.local.response["http_status_code"] = 409
				return {
					"status": "error",
					"message": f"Stall {stall_num} is currently held by another attendee completing payment. Please select a different stall."
				}

		# Acquire Redis Atomic Lock (Primary 10,000+ RPS Defense)
		cache = frappe.cache()
		lock_key = f"{STALL_LOCK_PREFIX}{stall_num}"
		new_token = f"hold_{uuid.uuid4().hex[:12]}"
		expires_at = now_dt + timedelta(seconds=600)

		# Atomic SETNX with 600s TTL
		try:
			raw_key = cache.make_key(lock_key)
			redis_acquired = cache.set(raw_key, new_token, nx=True, ex=600)
		except Exception:
			redis_acquired = True

		if not redis_acquired and (not existing_token or existing_token != stall.hold_token):
			frappe.local.response["http_status_code"] = 409
			return {
				"status": "error",
				"message": f"Stall {stall_num} is currently held by another attendee completing payment. Please select a different stall."
			}

		stall.status = "Hold"
		stall.hold_token = new_token
		stall.hold_expires_at = expires_at
		stall.booked_by_name = full_name
		stall.booked_by_email = email
		stall.booked_by_mobile = mobile_no
		stall.booked_by_company = company_name
		stall.save(ignore_permissions=True)
		frappe.db.commit()

		dynamic_prices = get_tier_pricing()
		current_price = dynamic_prices.get(stall.tier, stall.price)

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
		frappe.local.response["http_status_code"] = 500
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
			stall.status = "Available"
			stall.hold_token = None
			stall.hold_expires_at = None
			stall.save(ignore_permissions=True)
			frappe.db.commit()

	cache = frappe.cache()
	cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))

	return {"status": "success", "message": f"Stall {stall_num} hold released"}


def toggle_stall_block(stall_number=None, action="block", reason=None, **kwargs):
	"""
	Admin API to block/unblock stalls.
	Eg: Stall 11 block -> User cannot buy.
	"""
	data = extract_request_data(kwargs)
	stall_num = (stall_number or data.get("stall_number") or "").strip()
	act = (action or data.get("action") or "block").strip().lower()
	resn = (reason or data.get("reason") or "Reserved by Administration").strip()

	if not stall_num or not frappe.db.exists("ConnectFest Stall", stall_num):
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": f"Stall '{stall_num}' not found"}

	stall = frappe.get_doc("ConnectFest Stall", stall_num)

	if act == "block":
		if stall.status == "Booked":
			frappe.local.response["http_status_code"] = 400
			return {"status": "error", "message": f"Cannot block stall {stall_num} - already paid and booked by {stall.booked_by_name}."}
		stall.is_blocked_by_admin = 1
		stall.status = "Blocked"
		stall.blocked_reason = resn
		stall.hold_token = None
		stall.hold_expires_at = None
		frappe.cache().delete(frappe.cache().make_key(f"{STALL_LOCK_PREFIX}{stall_num}"))
	else:
		stall.is_blocked_by_admin = 0
		stall.status = "Available"
		stall.blocked_reason = ""

	stall.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Stall {stall_num} successfully {act}ed",
		"stall_number": stall_num,
		"status": stall.status,
		"is_blocked_by_admin": stall.is_blocked_by_admin,
		"blocked_reason": stall.blocked_reason,
	}


def get_admin_dashboard_data(**kwargs):
	"""
	Returns complete real-time dashboard data for ConnectFest Admin.
	"""
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
		limit=250,
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
