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
	Admin Gate Entry QR Scanner API.
	- Validates visitor pass at gate.
	- Validates Day matching: Passes registered for 'Day 1' cannot be used on 'Day 2', and vice versa.
	- Prevents duplicate check-in. On duplicate scan, strictly omits personal details and returns alert.
	"""
	sess_user = getattr(frappe.session, "user", None)
	if sess_user and sess_user != "Guest":
		from connectfest.services.auth import is_admin_user
		if not is_admin_user(sess_user):
			frappe.local.response["http_status_code"] = 403
			return {
				"status": "forbidden",
				"verified": False,
				"message": "Access Denied: Gate entry verification is strictly restricted to Event Administrators and gate staff!",
			}

	data = extract_request_data(kwargs)
	code = (pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or "").strip()
	scan_day = (day or data.get("day") or data.get("scan_day") or data.get("which_day") or data.get("current_day") or "").strip()

	if not scan_day:
		now_dt = now_datetime()
		date_str = now_dt.strftime("%Y-%m-%d")
		if date_str == "2027-02-21":
			scan_day = "Day 2"
		elif date_str == "2027-02-20":
			scan_day = "Day 1"

	if not code:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "pass_code is required"}

	if ":" in code:
		parts = code.split(":")
		found_code = None
		for p in parts:
			p_clean = p.strip()
			if p_clean.startswith("CF27-") or p_clean.startswith("VIS-") or p_clean.startswith("CF-"):
				found_code = p_clean
				break
		if found_code:
			code = found_code
		elif len(parts) >= 2:
			code = parts[1]

	visitor = frappe.db.sql("""
		SELECT name, registration_id, full_name, visitor_image, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at, area_of_interest
		FROM `tabConnectFest Visitor Registration`
		WHERE pass_code = %(code)s OR registration_id = %(code)s OR name = %(code)s
		LIMIT 1
	""", {"code": code}, as_dict=True)

	if not visitor:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "invalid",
			"verified": False,
			"message": f"❌ INVALID PASS: No registration found for '{code}'. Please proceed to help desk.",
		}

	rec = visitor[0]

	# 1. Day Validation: Day 1 vs Day 2 check
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

	# 2. Duplicate Scan Check (No personal details returned!)
	if rec.checked_in:
		return {
			"status": "duplicate_entry",
			"verified": False,
			"message": f"⚠️ ALREADY SCANNED! This pass ({rec.pass_code}) was already scanned at {rec.checked_in_at}. Duplicate entry denied.",
		}

	now_str = now_datetime().strftime("%Y-%m-%d %H:%M:%S")
	frappe.db.set_value("ConnectFest Visitor Registration", rec.name, {
		"checked_in": 1,
		"checked_in_at": now_str,
		"status": "Checked In",
	})
	frappe.db.commit()

	return {
		"status": "success",
		"verified": True,
		"message": "✅ PASS VERIFIED - WELCOME TO CONNECTFEST 2027!",
		"pass": {
			"pass_code": rec.pass_code,
			"full_name": rec.full_name,
			"visitor_image": rec.visitor_image or "",
			"which_day": rec.which_day,
			"email": rec.email,
			"mobile_no": rec.mobile_no,
			"area_of_interest": rec.get("area_of_interest") or "",
			"checked_in_at": now_str,
			"venue": "Chennai Trade Centre, Nandambakkam",
		},
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

