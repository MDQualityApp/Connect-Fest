import uuid
from datetime import timedelta
import frappe
from frappe.utils import now_datetime, flt, cint
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
				"zone",
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

		tier_summary = {}
		for t_name, t_price in pricing.items():
			tier_summary[t_name] = {"total": 0, "available": 0, "booked": 0, "price": flt(t_price)}

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
				}, update_modified=False)
				stall["status"] = "Available"
				cache.delete(cache.make_key(f"{STALL_LOCK_PREFIX}{stall.stall_number}"))

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
				if t == "Silver": stall["zone"] = "Zone D - East Pavilions"
				elif t == "Premium": stall["zone"] = "Zone C - Center Aisles"
				elif t == "Standard": stall["zone"] = "Zone B - West Aisles"
				else: stall["zone"] = "Zone E - South Entrance Corridor"

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
	from connectfest.services.auth import is_admin_user
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can access admin stall inventory!"}

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
		if t == "Silver": s["zone"] = "Zone D - East Pavilions"
		elif t == "Premium": s["zone"] = "Zone C - Center Aisles"
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
	from connectfest.services.auth import is_admin_user
	from connectfest.services.pricing import get_tier_pricing
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can create stalls!"}

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
	from connectfest.services.auth import is_admin_user
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can view admin stall details!"}

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
	from connectfest.services.auth import is_admin_user
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can update stalls!"}

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
	from connectfest.services.auth import is_admin_user
	from connectfest.services.common import STALL_LOCK_PREFIX
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can delete stalls!"}

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

	frappe.delete_doc("ConnectFest Stall", s_num, ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "success",
		"message": f"Stall '{s_num}' deleted successfully from inventory.",
		"deleted_stall": s_num,
	}


DEFAULT_STALLS_SPEC = [
	{"prefix": "SL", "count": 20, "tier": "Silver", "dimensions": "4 x 3 m", "price": 100000.0, "zone": "Zone D - East Pavilions"},
	{"prefix": "PR", "count": 45, "tier": "Premium", "dimensions": "3 x 3 m", "price": 45000.0, "zone": "Zone C - Center Aisles"},
	{"prefix": "ST", "count": 60, "tier": "Standard", "dimensions": "3 x 2 m", "price": 35000.0, "zone": "Zone B - West Aisles"},
	{"prefix": "RG", "count": 70, "tier": "Regular", "dimensions": "2 x 2 m", "price": 25000.0, "zone": "Zone E - South Entrance Corridor"},
]


def admin_restore_defaults(reset_pricing=1, reset_stalls=1, reset_booked=0, **kwargs):
	"""
	Admin Restore API:
	Explicitly restores pristine default pricing (4 tiers) and default 195 stalls.
	- reset_pricing: 1 = Restore Silver (₹1,00,000), Premium (₹45,000), Standard (₹35,000), Regular (₹25,000) with 18% GST.
	- reset_stalls: 1 = Restore all 195 standard stalls (SL-01..20, PR-01..45, ST-01..60, RG-01..70)
	- reset_booked: 0 (default) = Keeps existing booked stalls intact. 1 = Fresh wipe (resets all booked stalls too).
	- Security: Restricted strictly to Administrator / System Manager.
	"""
	from connectfest.services.auth import is_admin_user
	from connectfest.services.pricing import restore_default_pricing

	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or not is_admin_user(sess_user):
		frappe.local.response["http_status_code"] = 403
		return {"status": "forbidden", "message": "Access Denied: Only administrators can restore defaults!"}

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
		result["pricing_tiers"] = ["Silver", "Premium", "Standard", "Regular"]

	# 2. Restore Stalls
	if r_stalls:
		now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

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

		# Re-seed all 195 default stalls
		created_count = 0
		for spec in DEFAULT_STALLS_SPEC:
			prefix = spec["prefix"]
			count = spec["count"]
			tier = spec["tier"]
			dims = spec["dimensions"]
			price = spec["price"]
			zone = spec.get("zone")

			for i in range(1, count + 1):
				stall_num = f"{prefix}-{i:02d}"
				existing = frappe.db.sql("SELECT name FROM `tabConnectFest Stall` WHERE stall_number = %s", (stall_num,), as_dict=True)
				if existing:
					frappe.db.sql("""
						UPDATE `tabConnectFest Stall`
						SET tier = %s, dimensions = %s, zone = %s, price = %s, modified = %s
						WHERE stall_number = %s AND status != 'Booked'
					""", (tier, dims, zone, price, now_str, stall_num))
				else:
					frappe.db.sql("""
						INSERT INTO `tabConnectFest Stall`
						(name, stall_number, tier, dimensions, zone, price, status, is_blocked_by_admin, creation, modified, modified_by, owner)
						VALUES (%s, %s, %s, %s, %s, %s, 'Available', 0, %s, %s, %s, %s)
					""", (stall_num, stall_num, tier, dims, zone, price, now_str, now_str, sess_user, sess_user))
					created_count += 1

		frappe.db.commit()
		result["stalls_restored"] = frappe.db.count("ConnectFest Stall")
		result["newly_created"] = created_count

	return result


