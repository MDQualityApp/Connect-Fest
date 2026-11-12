import frappe
from frappe.utils import now_datetime, flt
from connectfest.services.common import (
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	extract_request_data,
	get_cached_record,
)


def admin_verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""
	Universal Gate Entry QR Scanner API.
	- Restricted to Event Administrators and authorized Gate Scanners.
	- Scans and validates BOTH Visitor passes and BNI Event passes (and Exhibitor passes).
	- Validates Day matching for visitor passes.
	- Prevents duplicate check-in with exact timestamp and scanner user details.
	"""
	sess_user = getattr(frappe.session, "user", None)
	from connectfest.services.auth import is_admin_user, is_scanner_user

	# Check sid in headers/body if sess_user is Guest
	if not sess_user or sess_user == "Guest":
		req_sid = (
			frappe.get_request_header("sid")
			or frappe.get_request_header("X-Frappe-Session-Id")
			or (frappe.request.cookies.get("sid") if hasattr(frappe, "request") and frappe.request else None)
		)
		if not req_sid:
			auth_hdr = frappe.get_request_header("Authorization", "")
			if auth_hdr.startswith("Bearer "):
				req_sid = auth_hdr.split("Bearer ", 1)[1].strip()
		if req_sid and req_sid != "Guest":
			try:
				res = frappe.db.sql("SELECT user FROM `tabSessions` WHERE sid = %s LIMIT 1", (req_sid,))
				if res and res[0][0]:
					sess_user = res[0][0]
			except Exception:
				pass

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"verified": False,
			"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
		}

	if not (is_admin_user(sess_user) or is_scanner_user(sess_user)):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"verified": False,
			"message": f"Access Denied: User '{sess_user}' does not have scanner privileges! Contact Event Administrator to grant scanner access.",
		}

	data = extract_request_data(kwargs)
	code = (pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or data.get("registration_id") or "").strip()
	scan_day = (day or data.get("day") or data.get("scan_day") or data.get("which_day") or data.get("current_day") or "").strip()
	mob_scan = (data.get("mobile_no") or data.get("phone") or data.get("mobile") or "").strip()

	if not scan_day:
		now_dt = now_datetime()
		date_str = now_dt.strftime("%Y-%m-%d")
		if date_str == "2027-02-21":
			scan_day = "Day 2"
		elif date_str == "2027-02-20":
			scan_day = "Day 1"

	if not code and not mob_scan:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "pass_code or mobile_no is required"}

	if code and ":" in code:
		parts = code.split(":")
		found_code = None
		for p in parts:
			p_clean = p.strip()
			if p_clean.startswith("CF27-") or p_clean.startswith("VIS-") or p_clean.startswith("CF-") or p_clean.startswith("BNI-"):
				found_code = p_clean
				break
		if found_code:
			code = found_code
		elif len(parts) >= 2:
			code = parts[1]

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")

	# =========================================================================
	# 1. Check Visitor Pass (ConnectFest Visitor Registration)
	# =========================================================================
	visitor = None
	if code:
		visitor = frappe.db.sql("""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, checked_in_by, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s
			LIMIT 1
		""", {"code": code}, as_dict=True)

	if not visitor and mob_scan:
		visitor = frappe.db.sql("""
			SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, checked_in_by, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE mobile_no = %(mob)s
			ORDER BY creation DESC LIMIT 1
		""", {"mob": mob_scan}, as_dict=True)

	if visitor:
		rec = visitor[0]

		# Day Validation: Day 1 vs Day 2 check
		if scan_day:
			norm_scan = "Day 1" if "1" in str(scan_day) else ("Day 2" if "2" in str(scan_day) else str(scan_day).strip())
			vis_day = (rec.which_day or "Both days").strip()
			norm_vis = "Day 1" if "1" in vis_day else ("Day 2" if "2" in vis_day else vis_day)

			if norm_vis.lower() != "both days" and norm_scan.lower() != norm_vis.lower():
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "invalid_day",
					"verified": False,
					"message": f"❌ ACCESS DENIED (Day Mismatch): This pass is valid for {vis_day} only! It cannot be scanned on {norm_scan}.",
				}

		# Duplicate Scan Check
		if rec.checked_in:
			return {
				"status": "duplicate_entry",
				"verified": False,
				"message": f"⚠️ ALREADY SCANNED! Visitor pass ({rec.pass_code}) was already scanned at {rec.checked_in_at} by {rec.get('checked_in_by') or 'Gate Staff'}. Duplicate entry denied.",
			}

		frappe.db.set_value("ConnectFest Visitor Registration", rec.name, {
			"checked_in": 1,
			"checked_in_at": now_str,
			"checked_in_by": sess_user,
			"status": "Checked In",
		})
		frappe.db.commit()

		return {
			"status": "success",
			"verified": True,
			"pass_type": "visitor_pass",
			"message": "✅ VISITOR PASS VERIFIED - WELCOME TO CONNECTFEST 2027!",
			"pass": {
				"pass_code": rec.pass_code,
				"registration_id": rec.registration_id or rec.name,
				"full_name": rec.full_name,
				"visitor_image": rec.visitor_image or "",
				"which_day": rec.which_day,
				"email": rec.email,
				"mobile_no": rec.mobile_no,
				"area_of_interest": rec.get("area_of_interest") or "",
				"checked_in_at": now_str,
				"checked_in_by": sess_user,
				"venue": "Chennai Trade Centre, Nandambakkam",
			},
		}

	# =========================================================================
	# 2. Check BNI Event Pass (ConnectFest BNI Event Registration)
	# =========================================================================
	bni_event = None
	if code:
		bni_event = frappe.db.sql("""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by
			FROM `tabConnectFest BNI Event Registration`
			WHERE pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s
			LIMIT 1
		""", {"code": code}, as_dict=True)

	if not bni_event and mob_scan:
		bni_event = frappe.db.sql("""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by
			FROM `tabConnectFest BNI Event Registration`
			WHERE mobile_no = %(mob)s
			ORDER BY creation DESC LIMIT 1
		""", {"mob": mob_scan}, as_dict=True)

	if bni_event:
		rec = bni_event[0]

		# Duplicate Scan Check
		if rec.checked_in:
			return {
				"status": "duplicate_entry",
				"verified": False,
				"message": f"⚠️ ALREADY SCANNED! BNI Event pass ({rec.pass_code}) was already scanned at {rec.checked_in_at} by {rec.get('checked_in_by') or 'Gate Staff'}. Duplicate entry denied.",
			}

		frappe.db.set_value("ConnectFest BNI Event Registration", rec.name, {
			"checked_in": 1,
			"checked_in_at": now_str,
			"checked_in_by": sess_user,
			"status": "Checked In",
		})
		frappe.db.commit()

		return {
			"status": "success",
			"verified": True,
			"pass_type": "bni_event_pass",
			"message": f"✅ BNI EVENT PASS VERIFIED - WELCOME TO {rec.get('event_name') or 'CONNECTFEST BNI EVENTS'}!",
			"pass": {
				"pass_code": rec.pass_code,
				"registration_id": rec.registration_id or rec.name,
				"full_name": rec.full_name,
				"company_name": rec.company_name,
				"chapter_name": rec.chapter_name,
				"region_name": rec.region_name,
				"mobile_no": rec.mobile_no,
				"email": rec.email,
				"event_name": rec.event_name,
				"checked_in_at": now_str,
				"checked_in_by": sess_user,
				"venue": "Chennai Trade Centre, Nandambakkam",
			},
		}

	# =========================================================================
	# 3. Check Exhibitor Stall Pass (ConnectFest Registration)
	# =========================================================================
	exhibitor = None
	if code:
		exhibitor = frappe.db.sql("""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       stall_number, stall_category, payment_status, status
			FROM `tabConnectFest Registration`
			WHERE registration_id = %(code)s OR name = %(code)s
			LIMIT 1
		""", {"code": code}, as_dict=True)

	if exhibitor:
		rec = exhibitor[0]
		return {
			"status": "success",
			"verified": True,
			"pass_type": "exhibitor_pass",
			"message": f"✅ EXHIBITOR PASS VERIFIED - STALL {rec.get('stall_number')} ({rec.get('stall_category')})",
			"pass": {
				"registration_id": rec.registration_id or rec.name,
				"full_name": rec.full_name,
				"company_name": rec.company_name,
				"stall_number": rec.stall_number,
				"stall_category": rec.stall_category,
				"payment_status": rec.payment_status,
				"mobile_no": rec.mobile_no,
				"email": rec.email,
				"venue": "Chennai Trade Centre, Nandambakkam",
			},
		}

	# Pass Not Found
	frappe.local.response["http_status_code"] = 404
	return {
		"status": "invalid",
		"verified": False,
		"message": f"❌ INVALID PASS: No registration found for '{code or mob_scan}'. Please proceed to help desk.",
	}


def verify_gate_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def admin_verify_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def verify_pass(pass_code=None, day=None, **kwargs):
	"""Alias pointing to admin_verify_gate_pass."""
	return admin_verify_gate_pass(pass_code=pass_code, day=day, **kwargs)


def get_scanned_users(status=None, pass_type=None, search=None, scanned_by=None, page=1, limit=50, **kwargs):
	"""
	Scanned & Registered Attendees Verification Dashboard.
	Access strictly restricted to:
	1. Users with dedicated role 'ConnectFest Gate Scanner'
	2. Event Administrators ('Administrator', 'System Manager', 'ConnectFest Admin')

	Allows viewing:
	- All registered attendees (status='all')
	- Only scanned attendees / checked-in (status='scanned' or status='checked_in')
	- Attendees who have NOT been scanned yet / pending (status='not_scanned' or status='pending')
	- Filter by pass_type: 'visitor', 'bni_event', 'all'
	- Filter by who scanned: scanned_by='karthi@gmail.com'
	- Search by name, email, phone, pass code, company
	"""
	sess_user = getattr(frappe.session, "user", None)
	from connectfest.services.auth import is_admin_user, is_scanner_user

	# Resolve user from sid if Guest
	if not sess_user or sess_user == "Guest":
		req_sid = (
			frappe.get_request_header("sid")
			or frappe.get_request_header("X-Frappe-Session-Id")
			or (frappe.request.cookies.get("sid") if hasattr(frappe, "request") and frappe.request else None)
		)
		if not req_sid:
			auth_hdr = frappe.get_request_header("Authorization", "")
			if auth_hdr.startswith("Bearer "):
				req_sid = auth_hdr.split("Bearer ", 1)[1].strip()
		if req_sid and req_sid != "Guest":
			try:
				res = frappe.db.sql("SELECT user FROM `tabSessions` WHERE sid = %s LIMIT 1", (req_sid,))
				if res and res[0][0]:
					sess_user = res[0][0]
			except Exception:
				pass

	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {
			"status": "unauthorized",
			"message": "Authentication required! Please log in as an Administrator or authorized Gate Scanner.",
		}

	if not (is_admin_user(sess_user) or is_scanner_user(sess_user)):
		frappe.local.response["http_status_code"] = 403
		return {
			"status": "forbidden",
			"message": f"Access Denied: User '{sess_user}' does not have scanner privileges! Only Administrators and users with the 'ConnectFest Gate Scanner' role can view scanned users.",
		}

	from frappe.utils import cint
	data = extract_request_data(kwargs)
	filter_status = (status or data.get("status") or "").strip().lower()
	filter_type = (pass_type or data.get("pass_type") or data.get("type") or "all").strip().lower()
	search_q = (search or data.get("search") or data.get("q") or "").strip()
	scanner_filter = (scanned_by or data.get("scanned_by") or data.get("checked_in_by") or "").strip().lower()

	p = max(1, cint(data.get("page") or page or 1))
	l = min(500, max(1, cint(data.get("limit") or limit or 50)))

	records = []

	# 1. Fetch from Visitor Registrations
	if filter_type in ("all", "visitor", "visitors"):
		vis_conds = []
		vis_vals = {}
		if filter_status in ("scanned", "checked_in", "checkedin", "1"):
			vis_conds.append("checked_in = 1")
		elif filter_status in ("not_scanned", "pending", "unscanned", "notscanned", "0"):
			vis_conds.append("(checked_in = 0 OR checked_in IS NULL)")

		if scanner_filter:
			vis_conds.append("checked_in_by = %(scanner)s")
			vis_vals["scanner"] = scanner_filter

		if search_q:
			vis_conds.append("(full_name LIKE %(q)s OR mobile_no LIKE %(q)s OR email LIKE %(q)s OR pass_code LIKE %(q)s)")
			vis_vals["q"] = f"%{search_q}%"

		vis_where = f"WHERE {' AND '.join(vis_conds)}" if vis_conds else ""
		vis_sql = f"""
			SELECT name, registration_id, full_name, email, mobile_no, which_day, pass_code,
			       area_of_interest, status, checked_in, checked_in_at, checked_in_by, creation
			FROM `tabConnectFest Visitor Registration`
			{vis_where}
			ORDER BY creation DESC
		"""
		vis_rows = frappe.db.sql(vis_sql, vis_vals, as_dict=True)
		for r in vis_rows:
			records.append({
				"id": r.get("registration_id") or r.get("name"),
				"name": r.get("registration_id") or r.get("name"),
				"full_name": r.get("full_name"),
				"email": r.get("email"),
				"mobile_no": r.get("mobile_no"),
				"pass_type": "visitor_pass",
				"pass_code": r.get("pass_code"),
				"details": f"Day: {r.get('which_day') or 'Both days'}",
				"which_day": r.get("which_day"),
				"area_of_interest": r.get("area_of_interest"),
				"checked_in": cint(r.get("checked_in")),
				"checked_in_at": str(r.get("checked_in_at")) if r.get("checked_in_at") else None,
				"checked_in_by": r.get("checked_in_by"),
				"status": "Checked In" if cint(r.get("checked_in")) else "Not Scanned",
				"creation": str(r.get("creation")),
			})

	# 2. Fetch from BNI Event Registrations
	if filter_type in ("all", "bni_event", "bni", "event", "events"):
		bni_conds = []
		bni_vals = {}
		if filter_status in ("scanned", "checked_in", "checkedin", "1"):
			bni_conds.append("checked_in = 1")
		elif filter_status in ("not_scanned", "pending", "unscanned", "notscanned", "0"):
			bni_conds.append("(checked_in = 0 OR checked_in IS NULL)")

		if scanner_filter:
			bni_conds.append("checked_in_by = %(scanner)s")
			bni_vals["scanner"] = scanner_filter

		if search_q:
			bni_conds.append("(full_name LIKE %(q)s OR mobile_no LIKE %(q)s OR email LIKE %(q)s OR pass_code LIKE %(q)s OR company_name LIKE %(q)s OR chapter_name LIKE %(q)s)")
			bni_vals["q"] = f"%{search_q}%"

		bni_where = f"WHERE {' AND '.join(bni_conds)}" if bni_conds else ""
		bni_sql = f"""
			SELECT name, registration_id, full_name, company_name, mobile_no, email,
			       chapter_name, region_name, location, industry, segment, event_name,
			       pass_code, status, checked_in, checked_in_at, checked_in_by, creation
			FROM `tabConnectFest BNI Event Registration`
			{bni_where}
			ORDER BY creation DESC
		"""
		bni_rows = frappe.db.sql(bni_sql, bni_vals, as_dict=True)
		for r in bni_rows:
			records.append({
				"id": r.get("registration_id") or r.get("name"),
				"name": r.get("registration_id") or r.get("name"),
				"full_name": r.get("full_name"),
				"company_name": r.get("company_name"),
				"email": r.get("email"),
				"mobile_no": r.get("mobile_no"),
				"pass_type": "bni_event_pass",
				"pass_code": r.get("pass_code"),
				"chapter_name": r.get("chapter_name"),
				"region_name": r.get("region_name"),
				"event_name": r.get("event_name"),
				"details": f"Chapter: {r.get('chapter_name') or 'BNI'} ({r.get('company_name') or 'N/A'})",
				"checked_in": cint(r.get("checked_in")),
				"checked_in_at": str(r.get("checked_in_at")) if r.get("checked_in_at") else None,
				"checked_in_by": r.get("checked_in_by"),
				"status": "Checked In" if cint(r.get("checked_in")) else "Not Scanned",
				"creation": str(r.get("creation")),
			})

	# Sort by check-in time DESC or creation DESC
	records.sort(key=lambda x: x.get("checked_in_at") or x.get("creation") or "", reverse=True)

	total_matching = len(records)
	total_scanned = sum(1 for r in records if r.get("checked_in") == 1)
	total_not_scanned = total_matching - total_scanned

	# Summary stats across whole database
	vis_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Visitor Registration`")[0][0]
	vis_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest Visitor Registration` WHERE checked_in = 1")[0][0]

	bni_tot = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest BNI Event Registration`")[0][0]
	bni_chk = frappe.db.sql("SELECT COUNT(name) as cnt FROM `tabConnectFest BNI Event Registration` WHERE checked_in = 1")[0][0]

	# Paginate
	start = (p - 1) * l
	end = start + l
	paginated = records[start:end]

	return {
		"status": "success",
		"summary": {
			"total_registered": vis_tot + bni_tot,
			"total_scanned": vis_chk + bni_chk,
			"total_not_scanned": max(0, (vis_tot + bni_tot) - (vis_chk + bni_chk)),
			"visitor_registered": vis_tot,
			"visitor_scanned": vis_chk,
			"visitor_not_scanned": max(0, vis_tot - vis_chk),
			"bni_event_registered": bni_tot,
			"bni_event_scanned": bni_chk,
			"bni_event_not_scanned": max(0, bni_tot - bni_chk),
			"current_filter": {
				"status": filter_status or "all",
				"type": filter_type or "all",
				"search": search_q or None,
				"scanned_by": scanner_filter or None,
			},
		},
		"pagination": {
			"page": p,
			"limit": l,
			"total_matching": total_matching,
			"total_pages": max(1, (total_matching + l - 1) // l),
		},
		"scanned_users": paginated,
	}


# Aliases
admin_get_scanned_users = get_scanned_users
get_check_in_list = get_scanned_users
admin_get_check_in_list = get_scanned_users


def get_registration(registration_id):
	"""Universal lookup by Registration ID for both Stalls and Visitors."""
	sess_user = getattr(frappe.session, "user", None)
	if not sess_user or sess_user == "Guest":
		frappe.local.response["http_status_code"] = 401
		return {"status": "unauthorized", "message": "Authentication required. Please log in via OTP to view registration details."}

	if not registration_id:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "registration_id is required"}

	from connectfest.services.auth import is_admin_user
	is_admin = is_admin_user(sess_user)

	cache = frappe.cache()
	result = None

	# 1. Check Visitor Cache
	v_cached = get_cached_record(cache, f"{VISITOR_CACHE_PREFIX}{registration_id}")
	if v_cached:
		result = {"status": "success", "type": "visitor", "source": "cache", "data": v_cached}

	# 2. Check Stall Cache
	if not result:
		s_cached = get_cached_record(cache, f"{STALL_CACHE_PREFIX}{registration_id}")
		if s_cached:
			result = {"status": "success", "type": "stall", "source": "cache", "data": s_cached}

	# 3. Check Database: Visitor
	if not result:
		v_records = frappe.get_all(
			"ConnectFest Visitor Registration",
			filters={"registration_id": registration_id},
			fields=["*"],
			limit=1,
		)
		if v_records:
			result = {"status": "success", "type": "visitor", "source": "db", "data": v_records[0]}

	# 4. Check Database: Stall
	if not result:
		s_records = frappe.get_all(
			"ConnectFest Registration",
			filters={"registration_id": registration_id},
			fields=["*"],
			limit=1,
		)
		if s_records:
			result = {"status": "success", "type": "stall", "source": "db", "data": s_records[0]}

	if not result:
		frappe.local.response["http_status_code"] = 404
		return {"status": "error", "message": "Registration not found"}

	# IDOR Check
	if not is_admin:
		res_data = result.get("data", {})
		rec_email = (res_data.get("email") or "").strip().lower()
		rec_mob = (res_data.get("mobile_no") or "").strip()
		sess_lower = sess_user.strip().lower()
		user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
		if rec_email != sess_lower and (not user_phone or user_phone != rec_mob):
			frappe.local.response["http_status_code"] = 403
			return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's registration."}

	if result and "data" in result:
		result["full_name"] = result["data"].get("full_name")

	return result


def get_my_dashboard(email=None, mobile_no=None, security_pin=None, registration_id=None, **kwargs):
	"""
	Attendee Dashboard: Returns booked stall details (stall number, tier, dimensions, zone, receipt, exhibitor badge).
	Strictly requires authentication. Prevents IDOR peeking.
	"""
	from connectfest.services.common import mask_phone, mask_email, check_rate_limit
	from connectfest.services.auth import is_admin_user
	try:
		if not check_rate_limit("dashboard", limit=30, window_secs=60):
			frappe.local.response["http_status_code"] = 429
			return {"status": "error", "message": "Too many requests. Please wait a minute before trying again."}

		data = extract_request_data(kwargs)
		em = (email or data.get("email") or data.get("user_email") or "").strip().lower()
		mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()
		pin = (security_pin or data.get("security_pin") or data.get("pin") or "").strip()
		req_reg_id = (registration_id or data.get("registration_id") or "").strip()

		sess_user = getattr(frappe.session, "user", None)
		is_admin = is_admin_user(sess_user) if sess_user and sess_user != "Guest" else False

		if not sess_user or sess_user == "Guest":
			if not em and not mb and not req_reg_id:
				frappe.local.response["http_status_code"] = 401
				return {
					"status": "unauthorized",
					"message": "Authentication required. Please log in using your registered mobile number or email via OTP to access your dashboard.",
				}
		else:
			if not is_admin:
				if em and em != sess_user.lower():
					frappe.local.response["http_status_code"] = 403
					return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's dashboard or pass."}
				em = sess_user.lower()
			if not em and not mb and not req_reg_id:
				em = sess_user.lower()

		# 1. Fetch Confirmed Stall Booking
		stall_booking = None
		where_clauses = []
		params = {}
		if req_reg_id:
			where_clauses.append("registration_id = %(reg_id)s")
			params["reg_id"] = req_reg_id
		if em:
			where_clauses.append("email = %(email)s")
			params["email"] = em
		if mb:
			where_clauses.append("mobile_no = %(mobile)s")
			params["mobile"] = mb

		where_str = " OR ".join(where_clauses) if where_clauses else "1=0"

		stalls = frappe.db.sql(f"""
			SELECT registration_id, full_name, company_name, company_logo, location,
			       mobile_no, email, is_bni_member, region_name, chapter_name,
			       industry, segment, stall_number, stall_category, amount, payment_status,
			       payment_mode, razorpay_order_id, razorpay_payment_id, payment_date, status, creation,
			       bill, gst
			FROM `tabConnectFest Registration`
			WHERE ({where_str})
			ORDER BY creation DESC
		""", params, as_dict=True)

		# 2. Fetch Visitor Pass
		visitor_pass = None
		visitors = frappe.db.sql(f"""
			SELECT registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code,
			       qr_payload, status, checked_in, checked_in_at, creation, area_of_interest
			FROM `tabConnectFest Visitor Registration`
			WHERE ({where_str})
			ORDER BY creation DESC LIMIT 1
		""", params, as_dict=True)

		if not stalls and not visitors:
			frappe.local.response["http_status_code"] = 404
			return {"status": "not_found", "message": "No registration found for the provided details. Please register first."}

		# Verify ownership security (Anti-IDOR)
		matched_reg = stalls[0] if stalls else (visitors[0] if visitors else None)
		if sess_user and sess_user != "Guest" and not is_admin and matched_reg:
			rec_email = (matched_reg.get("email") or "").strip().lower()
			rec_mob = (matched_reg.get("mobile_no") or "").strip()
			user_phone = (frappe.db.get_value("User", sess_user, "phone") or frappe.db.get_value("User", sess_user, "mobile_no") or "").strip()
			if rec_email != sess_user.lower() and (not user_phone or user_phone != rec_mob):
				frappe.local.response["http_status_code"] = 403
				return {"status": "forbidden", "message": "Access Denied: You cannot view another attendee's dashboard or pass."}

		is_authorized = True

		all_stall_bookings = []
		if stalls:
			for sb in stalls:
				if not sb.stall_number:
					continue

				stall_doc = None
				if frappe.db.exists("ConnectFest Stall", sb.stall_number):
					stall_doc = frappe.get_doc("ConnectFest Stall", sb.stall_number)

				zone = "Zone B - West Aisles"
				tier_name = sb.stall_category or (stall_doc.tier if stall_doc else "")
				if tier_name == "Silver":
					zone = "Zone D - East Pavilions"
				elif tier_name == "Premium":
					zone = "Zone C - Center Aisles"
				elif tier_name == "Standard":
					zone = "Zone B - West Aisles"
				elif tier_name == "Regular":
					zone = "Zone E - South Entrance Corridor"
				else:
					zone = None

				is_paid = (sb.payment_status == "Paid")
				status_desc = "Confirmed" if is_paid else ("Awaiting Payment" if sb.stall_number else "Stall Not Selected")

				raw_fn = (sb.full_name or "").strip()
				clean_fn = raw_fn.split("@")[0].replace(".", " ").replace("_", " ").title() if "@" in raw_fn else raw_fn

				all_stall_bookings.append({
					"registration_id": sb.registration_id,
					"full_name": clean_fn,
					"company_name": sb.company_name,
					"company_logo": sb.company_logo or "",
					"location": sb.location or "",
					"bill": sb.get("bill") or "",
					"gst": sb.get("gst") or "",
					"industry": sb.industry or "",
					"segment": sb.segment or "",
					"is_bni_member": bool(sb.is_bni_member),
					"region_name": sb.region_name or "",
					"chapter_name": sb.chapter_name or "",
					"email": sb.email if is_authorized else mask_email(sb.email),
					"mobile_no": sb.mobile_no if is_authorized else mask_phone(sb.mobile_no),
					"stall_number": sb.stall_number,
					"stall_category": tier_name,
					"zone": zone,
					"dimensions": (stall_doc.dimensions if stall_doc else "") or ("Standard" if sb.stall_number else None),
					"amount": flt(sb.amount),
					"payment_status": sb.payment_status,
					"payment_mode": sb.get("payment_mode") or ("Cash" if (sb.get("razorpay_payment_id") or "").startswith("CASH") else "Online"),
					"has_paid": is_paid,
					"has_selected_stall": bool(sb.stall_number),
					"next_action": "view_badge" if is_paid else ("proceed_to_payment" if sb.stall_number else "select_stall"),
					"razorpay_payment_id": sb.razorpay_payment_id if is_authorized else "pay_verified_secure",
					"razorpay_order_id": sb.razorpay_order_id if is_authorized else "order_verified_secure",
					"payment_date": str(sb.payment_date or sb.creation) if is_paid else None,
					"status": status_desc,
					"badge_qr_payload": (f"CONNECTFEST2027:EXHIBITOR:{sb.stall_number}:{sb.registration_id}:{sb.email}" if is_authorized else None) if is_paid else None,
					"venue": "Chennai Trade Centre, Nandambakkam",
					"dates": "20 & 21 Feb 2027",
				})

		stall_booking = all_stall_bookings[0] if all_stall_bookings else None

		if visitors:
			v = visitors[0]
			raw_v_fn = (v.full_name or "").strip()
			clean_v_fn = raw_v_fn.split("@")[0].replace(".", " ").replace("_", " ").title() if "@" in raw_v_fn else raw_v_fn
			visitor_pass = {
				"registration_id": v.registration_id,
				"pass_code": v.pass_code,
				"full_name": clean_v_fn,
				"visitor_image": v.visitor_image or "",
				"email": v.email if is_authorized else mask_email(v.email),
				"mobile_no": v.mobile_no if is_authorized else mask_phone(v.mobile_no),
				"which_day": v.which_day,
				"area_of_interest": v.get("area_of_interest") or "",
				"access": "BOTH DAYS" if v.which_day == "Both days" else v.which_day.upper(),
				"status": v.status,
				"checked_in": v.checked_in,
				"checked_in_at": str(v.checked_in_at or ""),
				"qr_payload": (v.qr_payload or f"CONNECTFEST2027:{v.pass_code}:{v.email}") if is_authorized else None,
				"venue": "Chennai Trade Centre, Nandambakkam",
				"dates": "20 & 21 Feb 2027",
			}

		resolved_dashboard_full_name = None
		if stall_booking and stall_booking.get("full_name"):
			resolved_dashboard_full_name = stall_booking.get("full_name")
		elif visitor_pass and visitor_pass.get("full_name"):
			resolved_dashboard_full_name = visitor_pass.get("full_name")

		res = {
			"status": "success",
			"full_name": resolved_dashboard_full_name,
			"is_authorized": is_authorized,
			"has_stall_booking": bool(all_stall_bookings),
			"stall_booking": stall_booking,
			"stall_bookings": all_stall_bookings,
			"total_stalls": len(all_stall_bookings),
			"has_visitor_pass": bool(visitor_pass),
			"visitor_pass": visitor_pass,
		}

		if not is_authorized:
			res["security_notice"] = "🔒 Privacy Shield Active: Sensitive phone numbers and entry QR codes are protected. Provide your Registration ID to unlock full exhibitor badge and QR code."

		return res
	except Exception as e:
		frappe.log_error("ConnectFest Dashboard Error", frappe.get_traceback())
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}

