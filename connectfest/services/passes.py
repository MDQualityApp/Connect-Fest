import frappe
from frappe.utils import now_datetime, flt
from connectfest.services.common import (
	STALL_CACHE_PREFIX,
	VISITOR_CACHE_PREFIX,
	extract_request_data,
	get_cached_record,
)


def verify_gate_pass(pass_code=None, **kwargs):
	"""
	Gate Entry QR Scanner API.
	Validates visitor pass QR code at gate, marks checked_in = 1, and prevents duplicate re-entry.
	"""
	data = extract_request_data(kwargs)
	code = (pass_code or data.get("pass_code") or data.get("qr_code") or data.get("code") or "").strip()

	if not code:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "pass_code is required"}

	if ":" in code:
		parts = code.split(":")
		if len(parts) >= 2:
			code = parts[1]

	visitor = frappe.db.sql("""
		SELECT name, registration_id, full_name, email, mobile_no, which_day, pass_code, status, checked_in, checked_in_at
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

	if rec.checked_in:
		return {
			"status": "duplicate_entry",
			"verified": False,
			"message": f"⚠️ ALREADY CHECKED IN! This pass ({rec.pass_code}) was already scanned at {rec.checked_in_at}. Duplicate entry denied.",
			"data": rec,
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
			"which_day": rec.which_day,
			"email": rec.email,
			"mobile_no": rec.mobile_no,
			"checked_in_at": now_str,
			"venue": "Chennai Trade Centre, Nandambakkam",
		},
	}


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


def get_my_dashboard(email=None, mobile_no=None, **kwargs):
	"""
	Attendee Dashboard: Returns booked stall details (stall number, tier, dimensions, zone, receipt, exhibitor badge)
	and free visitor pass for the logged-in user or attendee lookup.
	"""
	try:
		data = extract_request_data(kwargs)
		em = (email or data.get("email") or data.get("user_email") or "").strip().lower()
		mb = (mobile_no or data.get("mobile_no") or data.get("phone") or "").strip()

		# Fallback to Frappe session user if available
		if not em and getattr(frappe.session, "user", None) != "Guest":
			em = frappe.session.user

		if not em and not mb:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "Please log in or provide your registered email/phone number to view your dashboard.",
			}

		# 1. Fetch Confirmed Stall Booking
		stall_booking = None
		stalls = frappe.db.sql("""
			SELECT registration_id, full_name, company_name, email, mobile_no,
			       stall_number, stall_category, amount, payment_status,
			       razorpay_order_id, razorpay_payment_id, payment_date, status, creation
			FROM `tabConnectFest Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s) AND payment_status = 'Paid'
			ORDER BY creation DESC LIMIT 1
		""", {"email": em, "mobile": mb}, as_dict=True)

		if stalls:
			sb = stalls[0]
			stall_doc = None
			if sb.stall_number and frappe.db.exists("ConnectFest Stall", sb.stall_number):
				stall_doc = frappe.get_doc("ConnectFest Stall", sb.stall_number)

			zone = "Zone B - West Aisles"
			tier_name = sb.stall_category or (stall_doc.tier if stall_doc else "Regular")
			if tier_name == "Silver":
				zone = "Zone D - East Pavilions"
			elif tier_name == "Premium":
				zone = "Zone C - Center Aisles"
			elif tier_name == "Standard":
				zone = "Zone B - West Aisles"
			else:
				zone = "Zone E - South Entrance Corridor"

			stall_booking = {
				"registration_id": sb.registration_id,
				"full_name": sb.full_name,
				"company_name": sb.company_name,
				"email": sb.email,
				"mobile_no": sb.mobile_no,
				"stall_number": sb.stall_number,
				"stall_category": tier_name,
				"zone": zone,
				"dimensions": (stall_doc.dimensions if stall_doc else "") or "Standard",
				"amount": flt(sb.amount),
				"payment_status": sb.payment_status,
				"razorpay_payment_id": sb.razorpay_payment_id,
				"razorpay_order_id": sb.razorpay_order_id,
				"payment_date": str(sb.payment_date or sb.creation),
				"status": "Confirmed",
				"badge_qr_payload": f"CONNECTFEST2027:EXHIBITOR:{sb.stall_number}:{sb.registration_id}:{sb.email}",
				"venue": "Chennai Trade Centre, Nandambakkam",
				"dates": "20 & 21 Feb 2027",
			}

		# 2. Fetch Visitor Pass
		visitor_pass = None
		visitors = frappe.db.sql("""
			SELECT registration_id, full_name, email, mobile_no, which_day, pass_code,
			       qr_payload, status, checked_in, checked_in_at, creation
			FROM `tabConnectFest Visitor Registration`
			WHERE (email = %(email)s OR mobile_no = %(mobile)s)
			ORDER BY creation DESC LIMIT 1
		""", {"email": em, "mobile": mb}, as_dict=True)

		if visitors:
			v = visitors[0]
			visitor_pass = {
				"registration_id": v.registration_id,
				"pass_code": v.pass_code,
				"full_name": v.full_name,
				"email": v.email,
				"mobile_no": v.mobile_no,
				"which_day": v.which_day,
				"access": "BOTH DAYS" if v.which_day == "Both days" else v.which_day.upper(),
				"status": v.status,
				"checked_in": v.checked_in,
				"checked_in_at": str(v.checked_in_at or ""),
				"qr_payload": v.qr_payload or f"CONNECTFEST2027:{v.pass_code}:{v.email}",
				"venue": "Chennai Trade Centre, Nandambakkam",
				"dates": "20 & 21 Feb 2027",
			}

		return {
			"status": "success",
			"is_logged_in": True,
			"user_email": em or (stall_booking["email"] if stall_booking else (visitor_pass["email"] if visitor_pass else "")),
			"has_stall_booking": bool(stall_booking),
			"stall_booking": stall_booking,
			"has_visitor_pass": bool(visitor_pass),
			"visitor_pass": visitor_pass,
		}
	except Exception as e:
		frappe.log_error("ConnectFest Dashboard Error", frappe.get_traceback())
		frappe.local.response["http_status_code"] = 500
		return {"status": "error", "message": "Something went wrong. Please try again or contact event support."}
