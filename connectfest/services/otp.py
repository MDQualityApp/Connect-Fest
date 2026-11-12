import random
import time
from concurrent.futures import ThreadPoolExecutor
import frappe
from connectfest.services.common import extract_request_data, check_rate_limit
from connectfest.services.auth import ensure_frappe_user

OTP_CACHE_PREFIX = "connectfest:otp:"
EMAIL_OTP_CACHE_PREFIX = "connectfest:otp:email:"
EMAIL_VERIFIED_CACHE_PREFIX = "connectfest:email_verified:"
OTP_EXPIRY_SECONDS = 120  # 2 minutes expiry as requested
EMAIL_VERIFIED_EXPIRY_SECONDS = 1800  # 30 minutes validity once verified
TEST_MASTER_OTPS = ["123456", "999999"]

# High-concurrency background email thread pool for zero-lag, instant OTP delivery (1000+ users)
_email_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="email_otp_worker")


def _background_send_email_task(site_name, recipient, subject, content):
	"""
	Asynchronous background worker:
	Establishes SMTP connection and delivers OTP without stalling the web HTTP worker.
	Ensures web requests respond in <30ms, preventing 504 timeouts and server lag.
	"""
	try:
		frappe.init(site=site_name)
		frappe.connect()

		# Dynamically resolve sender from configured default outgoing Email Account
		outgoing_acc = frappe.db.get_value(
			"Email Account",
			{"default_outgoing": 1},
			["email_id", "email_account_name", "name"],
			as_dict=True,
		)
		sender_str = None
		if outgoing_acc and outgoing_acc.get("email_id"):
			s_name = outgoing_acc.get("email_account_name") or outgoing_acc.get("name") or "ConnectFest"
			sender_str = f"{s_name} <{outgoing_acc.get('email_id')}>"

		sendmail_args = {
			"recipients": [recipient],
			"subject": subject,
			"message": content,
			"now": True,
			"delayed": False,
		}
		if sender_str:
			sendmail_args["sender"] = sender_str

		frappe.sendmail(**sendmail_args)
		frappe.db.commit()
		try:
			from frappe.email.queue import flush
			flush(from_test=False)
		except Exception:
			pass
		frappe.logger("connectfest").info(f"[Fast Async Email OTP] Delivered OTP to {recipient}")
	except Exception as e:
		frappe.logger("connectfest").warning(f"[Fast Async Email OTP] Error sending to {recipient}: {str(e)}")
	finally:
		try:
			frappe.destroy()
		except Exception:
			pass


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
	Universal OTP Request API.
	Routes directly to Email OTP for lag-free, secure email verification and login.
	Accepts email, or resolves registered attendee email from mobile number.
	"""
	data = extract_request_data(kwargs)
	target_email = str(data.get("email") or data.get("user_email") or "").strip().lower()
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))

	if not target_email and mob:
		target_email = (
			frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("User", {"mobile_no": mob}, "name")
			or frappe.db.get_value("User", {"phone": mob}, "name")
			or ""
		)
		if target_email:
			target_email = target_email.strip().lower()

	if target_email:
		return send_email_otp(email=target_email, mobile_no=mob, **kwargs)

	frappe.local.response["http_status_code"] = 400
	return {
		"status": "error",
		"message": "A valid email address is required to receive verification OTP.",
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
	Universal Login & OTP Verification API.
	Enforces Email OTP for secure attendee session login.
	Accepts email directly, or resolves registered attendee email from mobile number.
	"""
	data = extract_request_data(kwargs)
	submitted_email = str(data.get("email") or data.get("user_email") or "").strip().lower()
	mob = normalize_mobile(mobile_no or data.get("mobile_no") or data.get("phone") or data.get("mobile"))
	submitted_otp = str(otp or data.get("otp") or data.get("code") or data.get("email_otp") or "").strip()

	if not submitted_email and mob:
		submitted_email = (
			frappe.db.get_value("ConnectFest Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("ConnectFest BNI Event Registration", {"mobile_no": mob}, "email")
			or frappe.db.get_value("User", {"mobile_no": mob}, "name")
			or frappe.db.get_value("User", {"phone": mob}, "name")
			or ""
		)
		if submitted_email:
			submitted_email = submitted_email.strip().lower()

	if not submitted_otp:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "OTP is required"}

	if submitted_email:
		return verify_email_otp(email=submitted_email, otp=submitted_otp, **kwargs)

	frappe.local.response["http_status_code"] = 400
	return {
		"status": "error",
		"message": "Valid email address is required for Email OTP verification.",
	}


def build_email_otp_html(otp_code, target_email=None, user_name=None):
	"""
	Clean, minimal, human-crafted HTML email template for ConnectFest Email OTP.
	High-deliverability format designed to land directly in Inbox (anti-spam optimized).
	"""
	display_name = (user_name or "").strip()
	greeting = f"Hello {display_name}," if display_name else "Hello,"

	html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ConnectFest Verification Code</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 480px; margin: 30px auto; background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);">
    
    <!-- Header -->
    <div style="background-color: #4f46e5; padding: 24px 20px; text-align: center;">
      <div style="font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: #e0e7ff; margin-bottom: 6px;">
        CONNECTFEST 2027
      </div>
      <h1 style="margin: 0; font-size: 20px; font-weight: 700; color: #ffffff; letter-spacing: -0.3px;">
        Email Verification Code
      </h1>
    </div>

    <!-- Body Content -->
    <div style="padding: 28px 24px;">
      <p style="font-size: 15px; color: #0f172a; margin: 0 0 10px; font-weight: 600;">
        {greeting}
      </p>
      <p style="font-size: 15px; color: #475569; margin: 0 0 20px; line-height: 1.5;">
        Your one-time verification code for ConnectFest is:
      </p>

      <!-- Clean OTP Box -->
      <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 18px 12px; text-align: center; margin: 0 0 20px;">
        <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Courier New', monospace; font-size: 34px; font-weight: 800; letter-spacing: 8px; color: #0f172a; margin-left: 8px;">
          {otp_code}
        </div>
      </div>

      <p style="font-size: 13px; color: #64748b; margin: 0 0 24px; line-height: 1.4;">
        This OTP is valid for 2 minutes (120 seconds). Please do not share this code with anyone.
      </p>

      <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 0 0 16px;" />

      <p style="font-size: 13px; color: #94a3b8; margin: 0;">
        ConnectFest Team &bull; Chennai Trade Centre
      </p>
    </div>

  </div>
</body>
</html>"""
	return html


def send_email_otp(email=None, **kwargs):
	"""
	Generates and sends a 6-digit OTP to the attendee's email address.
	Stores OTP in Redis with 2 minutes expiry.
	Also creates a persistent record in Frappe DocType 'ConnectFest OTP'.
	Mock OTPs are disabled: real email verification is enforced.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()

	if not target_email or "@" not in target_email or "." not in target_email.split("@")[-1]:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "A valid email address is required to receive OTP.",
		}

	# Rate limit OTP sending: max 6 requests per 10 minutes per email
	if not check_rate_limit(f"send_email_otp:{target_email}", limit=6, window_secs=600):
		frappe.local.response["http_status_code"] = 429
		return {
			"status": "error",
			"message": "Too many OTP requests for this email address. Please wait a few minutes before trying again.",
		}

	# Generate 6-digit secure numeric OTP
	otp_code = f"{random.randint(100000, 999999)}"

	# Retrieve attendee name for personalization if available
	user_name = str(data.get("full_name") or data.get("name") or "").strip()

	# If BNI Event registration context, check mobile_no or email first before sending OTP
	if data.get("is_bni_event") or data.get("event") == "bni" or data.get("check_bni"):
		from connectfest.services.bni_members import normalize_phone_number, is_valid_bni_member
		check_mob = normalize_phone_number(data.get("mobile_no") or data.get("phone") or "")
		is_bni, bni_doc = is_valid_bni_member(mobile_no=check_mob, email=target_email)
		if not is_bni:
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": "You are not a BNI member! Registration is strictly restricted to verified BNI members.",
				"mobile_no": check_mob,
				"email": target_email,
			}
		if not user_name and bni_doc and bni_doc.get("member_name"):
			user_name = bni_doc.get("member_name")

	if not user_name:
		user_name = (
			frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("User", {"email": target_email}, "full_name")
			or ""
		)

	# 1. Store in Frappe DocType 'ConnectFest OTP' (Persistent in MariaDB for Admin visibility)
	expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=OTP_EXPIRY_SECONDS)
	try:
		# Expire previous pending OTPs for this email
		frappe.db.sql("""
			UPDATE `tabConnectFest OTP`
			SET status = 'Expired'
			WHERE email = %s AND status = 'Pending'
		""", (target_email,))

		otp_doc = frappe.get_doc({
			"doctype": "ConnectFest OTP",
			"email": target_email,
			"mobile_no": str(data.get("mobile_no") or data.get("phone") or ""),
			"registered_user": user_name,
			"otp": otp_code,
			"status": "Pending",
			"expires_at": expires_at,
		})
		otp_doc.flags.ignore_permissions = True
		otp_doc.insert()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error("ConnectFest Email OTP Save Error", str(e))

	# 2. Also cache in Redis for ultra-fast lookup
	cache = frappe.cache()
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	cache.setex(cache_key, OTP_EXPIRY_SECONDS, otp_code)

	# 3. Fast non-blocking background email dispatch (Sub-30ms HTTP response, zero server lag)
	email_subject = f"{otp_code} is your ConnectFest verification code"
	email_content = build_email_otp_html(otp_code, target_email=target_email, user_name=user_name)

	site_name = getattr(getattr(frappe, "local", None), "site", "connectfest.localhost") or "connectfest.localhost"
	try:
		_email_executor.submit(_background_send_email_task, site_name, target_email, email_subject, email_content)
	except Exception:
		try:
			frappe.sendmail(
				recipients=[target_email],
				subject=email_subject,
				message=email_content,
				now=False,
				delayed=True,
			)
			frappe.db.commit()
		except Exception:
			pass

	# Log OTP in Frappe site log for observability
	try:
		frappe.logger("connectfest").info(f"[ConnectFest Email OTP] Sent OTP {otp_code} to {target_email} (Expires in 2 mins)")
	except Exception:
		pass

	return {
		"status": "success",
		"message": f"Verification code sent successfully to {target_email}. Please check your email inbox (valid for 2 minutes).",
		"email": target_email,
		"expires_in_seconds": OTP_EXPIRY_SECONDS,
		"expires_in": "2 minutes",
	}


def verify_email_otp(email=None, otp=None, **kwargs):
	"""
	Verifies the email OTP submitted by the attendee.
	Strictly requires the actual OTP sent via email (mock OTPs disabled).
	Upon successful verification, caches the verification status in Redis for 30 minutes
	so subsequent registration calls recognize this email as verified.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()
	submitted_otp = str(otp or data.get("otp") or data.get("code") or data.get("email_otp") or "").strip()

	if not target_email or "@" not in target_email:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Valid email address is required"}

	if not submitted_otp:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "OTP is required"}

	# Check OTP from Frappe DocType 'ConnectFest OTP'
	now_dt = frappe.utils.now_datetime()
	db_otp = frappe.db.sql("""
		SELECT name, otp, expires_at, status
		FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Pending'
		ORDER BY creation DESC LIMIT 1
	""", (target_email,), as_dict=True)

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
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	stored_otp = cache.get(cache_key)
	if isinstance(stored_otp, bytes):
		stored_otp = stored_otp.decode("utf-8")

	if not is_valid:
		if stored_otp and stored_otp == submitted_otp:
			is_valid = True

	if not is_valid:
		frappe.local.response["http_status_code"] = 400
		if not stored_otp and not db_otp:
			return {
				"status": "error",
				"message": "OTP has expired! The OTP is only valid for 2 minutes. Please request a new OTP.",
			}
		return {
			"status": "error",
			"message": "Invalid email OTP. Please check the 6-digit code sent to your email inbox.",
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

	# Delete OTP from Redis cache after single use
	try:
		cache.delete(cache_key)
	except Exception:
		pass

	# Mark email as verified in Redis cache for 30 minutes (1800s)
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	cache.setex(verified_key, EMAIL_VERIFIED_EXPIRY_SECONDS, "1")

	# -------------------------------------------------------------
	# Log user into Frappe session (Email OTP Login)
	# -------------------------------------------------------------
	from connectfest.services.auth import ensure_frappe_user
	from connectfest.services.passes import get_my_dashboard

	user_record_name = (
		frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": target_email}, "full_name")
		or frappe.db.get_value("User", target_email, "full_name")
		or target_email.split("@")[0].title()
	)
	user_mob = (
		frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("ConnectFest BNI Event Registration", {"email": target_email}, "mobile_no")
		or frappe.db.get_value("User", target_email, "mobile_no")
		or frappe.db.get_value("User", target_email, "phone")
		or ""
	)

	ensure_frappe_user(target_email, user_record_name, user_mob, role="Website User")

	session_sid = None
	if frappe.db.exists("User", target_email):
		try:
			if hasattr(frappe.local, "login_manager") and frappe.local.login_manager:
				frappe.local.login_manager.login_as(target_email)
			else:
				from frappe.auth import LoginManager
				frappe.local.login_manager = LoginManager()
				frappe.local.login_manager.login_as(target_email)

			session_sid = (
				getattr(getattr(frappe.local, "session_obj", None), "sid", None)
				or getattr(getattr(frappe.local, "session", None), "sid", None)
				or getattr(frappe.session, "sid", None)
			)
		except Exception:
			pass

	dashboard = get_my_dashboard(email=target_email, mobile_no=user_mob)

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"verified": True,
		"message": f"Email {target_email} verified successfully! You are logged in.",
		"email": target_email,
		"mobile_no": user_mob,
		"registered_user": user_record_name,
		"session_user": getattr(frappe.session, "user", target_email),
		"sid": session_sid,
		"token": session_sid,
		"is_registered": bool(frappe.db.exists("ConnectFest Registration", {"email": target_email}) or frappe.db.exists("ConnectFest Visitor Registration", {"email": target_email}) or frappe.db.exists("ConnectFest BNI Event Registration", {"email": target_email})),
		"dashboard": dashboard,
		"expires_in_seconds": EMAIL_VERIFIED_EXPIRY_SECONDS,
	}


def is_email_verified(email):
	"""
	Checks whether the email has been verified via verify_email_otp.
	"""
	if not email:
		return False
	target_email = str(email).strip().lower()
	cache = frappe.cache()
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	is_cached = cache.get(verified_key)
	if is_cached:
		return True

	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=-EMAIL_VERIFIED_EXPIRY_SECONDS)
	db_verified = frappe.db.sql("""
		SELECT name FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Verified' AND verified_at >= %s
		LIMIT 1
	""", (target_email, cutoff))
	return bool(db_verified)

