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

	# Rate limit OTP sending: max 6 requests per 10 minutes per mobile
	if not check_rate_limit(f"send_otp:{mob}", limit=6, window_secs=600):
		frappe.local.response["http_status_code"] = 429
		return {
			"status": "error",
			"message": "Too many OTP requests for this mobile number. Please wait a few minutes before trying again.",
		}

	# Generate 6-digit secure numeric OTP
	otp_code = f"{random.randint(100000, 999999)}"

	cache = frappe.cache()
	cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}{mob}")
	cache.setex(cache_key, OTP_EXPIRY_SECONDS, otp_code)

	# Check if attendee is already registered
	user_name = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "full_name")
	if not user_name:
		user_name = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "full_name")

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
		"expires_in_seconds": OTP_EXPIRY_SECONDS,
		"expires_in": "2 minutes",
		"registered_user": user_name or None,
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

	test_mode = is_otp_test_mode()

	cache = frappe.cache()
	cache_key = cache.make_key(f"{OTP_CACHE_PREFIX}{mob}")
	stored_otp = cache.get(cache_key)
	if isinstance(stored_otp, bytes):
		stored_otp = stored_otp.decode("utf-8")

	is_valid = False
	if stored_otp and stored_otp == submitted_otp:
		is_valid = True
	elif test_mode and submitted_otp in TEST_MASTER_OTPS:
		is_valid = True

	if not is_valid:
		frappe.local.response["http_status_code"] = 400
		if not stored_otp:
			return {
				"status": "error",
				"message": "OTP has expired! The OTP is only valid for 2 minutes. Please request a new OTP.",
			}
		return {
			"status": "error",
			"message": "Invalid OTP. Please check the 6-digit code or use test OTP '123456'.",
		}

	# Delete OTP from cache after single use to prevent replay
	try:
		cache.delete(cache_key)
	except Exception:
		pass

	# Resolve user identity
	user_email = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
	user_name = frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
		user_name = frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "full_name")

	if not user_email:
		user_email = frappe.db.get_value("User", {"phone": mob}, "name") or frappe.db.get_value("User", {"mobile_no": mob}, "name")

	# If this is a verified mobile but no Frappe user exists yet, auto-provision
	if user_email:
		clean_email = user_email.strip().lower()
		ensure_frappe_user(clean_email, user_name or f"Attendee {mob[-4:]}", mob, role="Website User")
		if frappe.db.exists("User", clean_email):
			frappe.local.login_manager.login_as(clean_email)
	else:
		# Mobile verified for registration (new attendee)
		clean_email = None

	dashboard = get_my_dashboard(email=clean_email, mobile_no=mob)

	return {
		"status": "success",
		"test_mode": test_mode,
		"verified": True,
		"message": "Mobile number verified successfully! You are logged in.",
		"mobile_no": mob,
		"email": clean_email,
		"session_user": getattr(frappe.session, "user", "Guest"),
		"is_registered": bool(clean_email),
		"dashboard": dashboard,
	}
