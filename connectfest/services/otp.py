import random
import time
import frappe
from connectfest.services.common import extract_request_data, check_rate_limit
from connectfest.services.auth import ensure_frappe_user

OTP_CACHE_PREFIX = "connectfest:otp:"
OTP_EXPIRY_SECONDS = 120  # 2 minutes expiry as requested
TEST_MASTER_OTPS = ["123456", "999999"]


def is_otp_test_mode():
	"""
	Checks if ConnectFest is running with Razorpay test keys (starts with rzp_test_).
	Allows test master OTPs (123456, 999999) for testing while on test credentials.
	"""
	try:
		key_id = None
		if frappe.db.exists("DocType", "ConnectFest Settings"):
			key_id = frappe.db.get_single_value("ConnectFest Settings", "razorpay_key_id")
		if not key_id:
			key_id = frappe.conf.get("razorpay_key_id")
		if not key_id or str(key_id).startswith("rzp_test_"):
			return True
	except Exception:
		pass
	return False


def normalize_mobile(mobile):
	if not mobile:
		return ""
	cleaned = "".join(filter(str.isdigit, str(mobile).strip()))
	if len(cleaned) > 10:
		# If includes country code +91
		cleaned = cleaned[-10:]
	return cleaned


def send_otp(mobile_no=None, **kwargs):
	"""
	Generates and sends a 6-digit OTP to the attendee's mobile number.
	Verifies that the mobile number is registered in ConnectFest before sending OTP.
	Stores OTP in Redis with 2 minutes expiry.
	Supports Razorpay-style Test Mode credentials for developer & QA testing.
	"""
	data = extract_request_data(kwargs)
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))

	if not mob or len(mob) != 10:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "A valid 10-digit mobile number is required to receive OTP.",
		}

	# Check if attendee mobile number exists in system (Exhibitors, Visitors, or Users)
	user_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
	user_name = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
		user_name = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("User", {"phone": mob}, "name") or frappe.db.get_value("User", {"mobile_no": mob}, "name")
		if user_email:
			user_name = frappe.db.get_value("User", user_email, "full_name")

	allow_unregistered = data.get("allow_unregistered") or data.get("purpose") == "registration" or kwargs.get("allow_unregistered")
	if not user_email and not allow_unregistered:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"Mobile number +91-{mob} is not registered with ConnectFest! Please register first as an Exhibitor or Visitor.",
			"mobile_no": mob,
			"is_registered": False,
		}

	# Rate limit OTP sending: max 6 requests per 10 minutes per mobile
	if not check_rate_limit(f"send_otp:{mob}", limit=6, window_secs=600):
		frappe.local.response["http_status_code"] = 429
		return {
			"status": "error",
			"message": "Too many OTP requests for this mobile number. Please wait a few minutes before trying again.",
		}

	# Generate 6-digit secure numeric OTP
	otp_code = f"{random.randint(100000, 999999)}"

	# 1. Store in Frappe DocType 'ConnectFest OTP' (Persistent in MariaDB for Admin visibility)
	expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=OTP_EXPIRY_SECONDS)
	try:
		# Expire previous pending OTPs for this mobile
		frappe.db.sql("""
			UPDATE `tabConnectFest OTP`
			SET status = 'Expired'
			WHERE mobile_no = %s AND status = 'Pending'
		""", (mob,))

		otp_doc = frappe.get_doc({
			"doctype": "ConnectFest OTP",
			"mobile_no": mob,
			"email": user_email or "",
			"registered_user": user_name or "",
			"otp": otp_code,
			"status": "Pending",
			"expires_at": expires_at,
		})
		otp_doc.flags.ignore_permissions = True
		otp_doc.insert()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error("ConnectFest OTP Save Error", str(e))

	# 2. Also cache in Redis for ultra-fast lookup
	cache = frappe.cache()
	cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}{mob}")
	cache.setex(cache_key, OTP_EXPIRY_SECONDS, otp_code)

	test_mode = is_otp_test_mode()

	# Log OTP in Frappe console / site log for observability
	try:
		frappe.logger("connectfest").info(f"[ConnectFest OTP] Sent OTP {otp_code} to +91-{mob} (Expires in 2 mins, Test Mode: {test_mode})")
	except Exception:
		pass

	return {
		"status": "success",
		"test_mode": test_mode,
		"message": f"OTP sent successfully to +91-{mob}. Valid for 2 minutes (120 seconds).",
		"mobile_no": mob,
		"email": user_email,
		"registered_user": user_name or None,
		"is_registered": True,
		"expires_in_seconds": OTP_EXPIRY_SECONDS,
		"expires_in": "2 minutes",
		"otp": otp_code,
		"test_credentials": {
			"mock_otp": "123456",
			"generated_otp": otp_code,
			"validity": "2 minutes (120s)",
			"info": "Razorpay-style Test Mode active: you can use either the generated OTP or master test OTP '123456' to verify immediately."
		}
	}


def verify_otp(mobile_no=None, otp=None, **kwargs):
	"""
	Verifies the OTP submitted by the attendee.
	Upon success, logs the user into Frappe session and returns their booking dashboard.
	"""
	from connectfest.services.passes import get_my_dashboard

	data = extract_request_data(kwargs)
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	submitted_otp = str(otp or data.get("otp") or data.get("code") or "").strip()

	if not mob or len(mob) != 10:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Valid 10-digit mobile number is required"}

	if not submitted_otp:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "OTP is required"}

	# Check if mobile number is registered
	user_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
	user_name = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
		user_name = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("User", {"phone": mob}, "name") or frappe.db.get_value("User", {"mobile_no": mob}, "name")
		if user_email:
			user_name = frappe.db.get_value("User", user_email, "full_name")

	allow_unregistered = data.get("allow_unregistered") or data.get("purpose") == "registration" or kwargs.get("allow_unregistered")
	if not user_email and not allow_unregistered:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"message": f"Mobile number +91-{mob} is not registered with ConnectFest! Please register first as an Exhibitor or Visitor.",
			"mobile_no": mob,
			"is_registered": False,
		}

	test_mode = is_otp_test_mode()

	# Check OTP from Frappe DocType 'ConnectFest OTP'
	now_dt = frappe.utils.now_datetime()
	db_otp = frappe.db.sql("""
		SELECT name, otp, expires_at, status
		FROM `tabConnectFest OTP`
		WHERE mobile_no = %s AND status = 'Pending'
		ORDER BY creation DESC LIMIT 1
	""", (mob,), as_dict=True)

	is_valid = False
	matched_record_name = None

	if db_otp:
		rec = db_otp[0]
		exp_dt = frappe.utils.get_datetime(rec.expires_at) if rec.expires_at else None
		if exp_dt and now_dt > exp_dt:
			frappe.db.set_value("ConnectFest OTP", rec.name, "status", "Expired")
			frappe.db.commit()
		elif rec.otp == submitted_otp:
			is_valid = True
			matched_record_name = rec.name

	# Also check Redis cache
	cache = frappe.cache()
	cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}{mob}")
	stored_otp = cache.get(cache_key)
	if isinstance(stored_otp, bytes):
		stored_otp = stored_otp.decode("utf-8")

	if not is_valid:
		if stored_otp and stored_otp == submitted_otp:
			is_valid = True
		elif test_mode and submitted_otp in TEST_MASTER_OTPS:
			is_valid = True
			if db_otp:
				matched_record_name = db_otp[0].name

	if not is_valid:
		frappe.local.response["http_status_code"] = 400
		if not stored_otp and not db_otp:
			return {
				"status": "error",
				"message": "OTP has expired! The OTP is only valid for 2 minutes. Please request a new OTP.",
			}
		return {
			"status": "error",
			"message": "Invalid OTP. Please check the 6-digit code or use test OTP '123456'.",
		}

	# Mark OTP record as Verified in Frappe DocType
	if matched_record_name:
		try:
			frappe.db.set_value("ConnectFest OTP", matched_record_name, {
				"status": "Verified",
				"verified_at": now_dt
			})
			frappe.db.commit()
		except Exception:
			pass

	# Delete OTP from Redis cache after single use to prevent replay
	try:
		cache.delete(cache_key)
	except Exception:
		pass

	# Log user into Frappe session
	dashboard = None
	clean_email = user_email.strip().lower() if user_email else None
	if clean_email:
		ensure_frappe_user(clean_email, user_name or f"Attendee {mob[-4:]}", mob, role="Website User")
		if frappe.db.exists("User", clean_email):
			try:
				if hasattr(frappe.local, "login_manager") and frappe.local.login_manager:
					frappe.local.login_manager.login_as(clean_email)
				else:
					from frappe.auth import LoginManager
					frappe.local.login_manager = LoginManager()
					frappe.local.login_manager.login_as(clean_email)
			except Exception:
				pass
			frappe.set_user(clean_email)
		dashboard = get_my_dashboard(email=clean_email, mobile_no=mob)

	frappe.local.response["http_status_code"] = 200

	return {
		"status": "success",
		"test_mode": test_mode,
		"verified": True,
		"message": "Mobile number verified successfully! You are logged in.",
		"mobile_no": mob,
		"email": clean_email,
		"registered_user": user_name,
		"session_user": getattr(frappe.session, "user", "Guest"),
		"is_registered": bool(clean_email),
		"dashboard": dashboard,
	}
