import uuid
from datetime import timedelta
import frappe
from frappe.utils import now_datetime, flt, cint
from connectfest.services.common import extract_request_data, parse_boolean, STALL_LOCK_PREFIX
from connectfest.services.pricing import get_tier_pricing


def get_stalls(**kwargs):
	"""
	Universal Floor Map & Real-Time Inventory API.
	Returns all 220 stalls with zone, dimensions, blueprint coordinates, live booking status, and pricing.
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
				if t == "Title Sponsor": stall["zone"] = "Zone TS - Title Sponsor Arena"
				elif t == "Silver": stall["zone"] = "Zone D - East Pavilions"
				elif t == "Premium": stall["zone"] = "Zone C - Center Grand Aisles"
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

		# Guard against blocking stalls currently on active 10-minute Hold!
		now_dt = now_datetime()
		cache = frappe.cache()
		has_redis_lock = bool(cache.get(cache.make_key(f"{STALL_LOCK_PREFIX}{stall_num}")))
		has_db_hold = (stall.status == "Hold" and stall.hold_expires_at and stall.hold_expires_at > now_dt)

		if (has_redis_lock or has_db_hold) and not is_force:
			buyer = stall.booked_by_name or stall.booked_by_company or stall.booked_by_email or stall.booked_by_mobile or "an exhibitor"
			remaining_mins = 10
			if stall.hold_expires_at and stall.hold_expires_at > now_dt:
				remaining_secs = max(1, int((stall.hold_expires_at - now_dt).total_seconds()))
				remaining_mins = max(1, (remaining_secs + 59) // 60)

			frappe.local.response["http_status_code"] = 409
			return {
				"status": "error",
				"message": f"Cannot block stall {stall_num}: Exhibitor '{buyer}' is currently completing payment! (Hold expires in ~{remaining_mins} minutes). Please wait until payment completes or hold expires.",
				"stall_status": "Hold",
				"held_by": buyer,
				"held_by_name": stall.booked_by_name,
				"held_by_company": stall.booked_by_company,
				"held_by_email": stall.booked_by_email,
				"held_by_mobile": stall.booked_by_mobile,
				"hold_expires_in_minutes": remaining_mins,
			}

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
	Requires System Manager or Administrator privileges.
	"""
	from connectfest.services.auth import check_admin_permission
	err = check_admin_permission("admin dashboard data")
	if err:
		return err

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


def seed_default_stalls(preserve_booked=True):
	"""
	Universal Stall & Tier Seeding Function for ConnectFest 2027.
	Seeds all 220 official floor-plan stalls (Blocks A to P + Title Sponsor TS-01) with SVG coordinates & dimensions.
	- 220 stalls:
	  * Title Sponsor: 1 stall (TS-01, 12x4m, Grand Central Arena)
	  * Silver: 37 stalls (4x3m, 6x3m, 8x3m, 6x4m, 8x4m, 6x5m)
	  * Premium: 81 stalls (3x3m, 5x2m, 5x3m, 6x2m)
	  * Standard: 59 stalls (3x2m)
	  * Regular: 42 stalls (2x2m)
	- Seeds default 5 tier pricing in 'ConnectFest Tier Pricing'.
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
		print(f"ConnectFest: Seeded/Verified 220 stalls (Total: {total_stalls}, Newly Created: {created_count}, Updated: {updated_count})")
	except Exception:
		pass

	return {
		"status": "success",
		"total_stalls": total_stalls,
		"newly_created": created_count,
		"updated": updated_count,
	}


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

		seed_res = seed_default_stalls(preserve_booked=not r_booked)
		result["stalls_restored"] = seed_res.get("total_stalls", 0)
		result["newly_created"] = seed_res.get("newly_created", 0)

	return result


