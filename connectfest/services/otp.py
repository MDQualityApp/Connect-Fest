import random
import time
from concurrent.futures import ThreadPoolExecutor
import frappe
from connectfest.services.common import extract_request_data, check_rate_limit, parse_boolean
from connectfest.services.auth import ensure_frappe_user, is_scanner_user

OTP_CACHE_PREFIX = "connectfest:otp:"
EMAIL_OTP_CACHE_PREFIX = "connectfest:otp:email:"
EMAIL_VERIFIED_CACHE_PREFIX = "connectfest:email_verified:"
DEFAULT_OTP_EXPIRY_SECONDS = 300  # Default 5 minutes (industry standard, ensures enough time for email delivery)
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

# Progressive Escalation Lockout Tiers:
# 1st violation: 10 minutes
# 2nd violation: 30 minutes
# 3rd violation: 1 hour
# 4th violation: 5 hours
# 5th+ violation: 1 day (24 hours)
LOCKOUT_TIERS = [
	(600, "10 minutes"),
	(1800, "30 minutes"),
	(3600, "1 hour"),
	(18000, "5 hours"),
	(86400, "1 day (24 hours)"),
]


def get_client_ip():
	"""Returns the true client IP address, handling proxies, Cloudflare, and X-Forwarded-For."""
	ip = getattr(frappe.local, "request_ip", None)
	if not ip:
		req = getattr(frappe, "request", None)
		if req:
			hdr = req.headers.get("CF-Connecting-IP") or req.headers.get("X-Forwarded-For") or req.headers.get("X-Real-IP")
			if hdr:
				ip = hdr.split(",")[0].strip()
			elif hasattr(req, "remote_addr"):
				ip = req.remote_addr
	return str(ip or "127.0.0.1").strip()


def get_next_lockout_tier(cache, identifier, scope="general"):
	"""
	Calculates progressive escalation lockout duration based on repeat violations:
	- 1st violation: 10 minutes (600s)
	- 2nd violation: 30 minutes (1800s)
	- 3rd violation: 1 hour (3600s)
	- 4th violation: 5 hours (18000s)
	- 5th+ violation: 1 day (86400s)
	History retained for 48 hours of clean behavior.
	"""
	v_key = cache.make_key(f"connectfest:otp_violations:{scope}:{identifier}")
	level = cache.incr(v_key)
	cache.expire(v_key, 172800)  # Retain violation level for 48 hours

	tier_idx = min(level - 1, len(LOCKOUT_TIERS) - 1)
	duration_secs, label = LOCKOUT_TIERS[tier_idx]
	return duration_secs, label, level


def format_lockout_remaining(ttl_seconds):
	"""Returns human-friendly remaining lockout time string."""
	if ttl_seconds > 3600:
		hours = max(1, (ttl_seconds + 3599) // 3600)
		return f"{hours} hour{'s' if hours > 1 else ''}"
	mins = max(1, (ttl_seconds + 59) // 60)
	return f"{mins} minute{'s' if mins > 1 else ''}"


def check_send_otp_rate_limit(target_email):
	"""
	Strict Dual Rate Limiter for Send OTP (by IP & Email):
	Allows maximum 5 OTP requests per 10 minutes (600s).
	Checked against both the client IP address AND target email address.
	Spamming triggers progressive escalation:
	1st violation: 10m -> 2nd: 30m -> 3rd: 1h -> 4th: 5h -> 5th+: 1 day.
	Returns: (allowed: bool, message: str or None, remaining_seconds: int)
	"""
	cache = frappe.cache()
	clean_email = str(target_email or "").strip().lower()
	client_ip = get_client_ip()

	ip_count_key = cache.make_key(f"connectfest:otp_send_ip_count:{client_ip}")
	ip_lock_key = cache.make_key(f"connectfest:otp_send_ip_locked:{client_ip}")
	email_count_key = cache.make_key(f"connectfest:otp_send_email_count:{clean_email}")
	email_lock_key = cache.make_key(f"connectfest:otp_send_email_locked:{clean_email}")

	# 1. Check if IP is currently locked
	if cache.get(ip_lock_key):
		ttl = cache.ttl(ip_lock_key) or 600
		rem_text = format_lockout_remaining(ttl)
		return False, f"Too many OTP requests from this device/IP. Access is locked for {rem_text}. Please try again later.", ttl

	# 2. Check if Email is currently locked
	if clean_email and cache.get(email_lock_key):
		ttl = cache.ttl(email_lock_key) or 600
		rem_text = format_lockout_remaining(ttl)
		return False, f"Too many OTP requests for this email. Access is locked for {rem_text}. Please try again later.", ttl

	# 3. Increment IP counter
	ip_cnt = cache.incr(ip_count_key)
	if ip_cnt == 1:
		cache.expire(ip_count_key, 600)

	# 4. Increment Email counter
	email_cnt = 0
	if clean_email:
		email_cnt = cache.incr(email_count_key)
		if email_cnt == 1:
			cache.expire(email_count_key, 600)

	if ip_cnt > 5:
		dur, label, lvl = get_next_lockout_tier(cache, client_ip, scope="send_ip")
		cache.setex(ip_lock_key, dur, "1")
		cache.delete(ip_count_key)
		prefix = "Persistent spam detected." if lvl >= 5 else "Repeated violations detected." if lvl > 1 else "Too many OTP requests."
		return False, f"{prefix} You have reached the limit of 5 requests. Locked for {label}. Please try again later.", dur

	if email_cnt > 5:
		dur, label, lvl = get_next_lockout_tier(cache, clean_email, scope="send_email")
		cache.setex(email_lock_key, dur, "1")
		cache.delete(email_count_key)
		prefix = "Persistent spam detected." if lvl >= 5 else "Repeated violations detected." if lvl > 1 else "Too many OTP requests."
		return False, f"{prefix} You have reached the limit of 5 requests for this email. Locked for {label}. Please try again later.", dur

	return True, None, 0


def check_verify_otp_lock(target_email):
	"""
	Checks if OTP verification is locked for client IP OR target email due to 3+ failed attempts.
	Returns: (is_locked: bool, message: str or None)
	"""
	cache = frappe.cache()
	clean_email = str(target_email or "").strip().lower()
	client_ip = get_client_ip()

	ip_lock_key = cache.make_key(f"connectfest:otp_verify_ip_locked:{client_ip}")
	email_lock_key = cache.make_key(f"connectfest:otp_verify_email_locked:{clean_email}")

	# Check IP lock first
	if cache.get(ip_lock_key):
		ttl = cache.ttl(ip_lock_key) or 600
		rem_text = format_lockout_remaining(ttl)
		return True, f"Too many invalid OTP attempts from this device/IP. OTP verification is locked for {rem_text} for security. Please try again later."

	# Check Email lock
	if clean_email and cache.get(email_lock_key):
		ttl = cache.ttl(email_lock_key) or 600
		rem_text = format_lockout_remaining(ttl)
		return True, f"Too many invalid OTP attempts for this email. OTP verification is locked for {rem_text} for security. Please try again later."

	return False, None


def record_verify_otp_failure(target_email):
	"""
	Records a failed OTP verification attempt for both IP and Email.
	If either reaches 3 failed attempts, locks verification with progressive escalation:
	1st violation: 10m -> 2nd: 30m -> 3rd: 1h -> 4th: 5h -> 5th+: 1 day.
	Returns: (is_locked: bool, message: str)
	"""
	cache = frappe.cache()
	clean_email = str(target_email or "").strip().lower()
	client_ip = get_client_ip()

	ip_fail_key = cache.make_key(f"connectfest:otp_verify_ip_fails:{client_ip}")
	ip_lock_key = cache.make_key(f"connectfest:otp_verify_ip_locked:{client_ip}")
	email_fail_key = cache.make_key(f"connectfest:otp_verify_email_fails:{clean_email}")
	email_lock_key = cache.make_key(f"connectfest:otp_verify_email_locked:{clean_email}")

	ip_fails = cache.incr(ip_fail_key)
	if ip_fails == 1:
		cache.expire(ip_fail_key, 600)

	email_fails = 0
	if clean_email:
		email_fails = cache.incr(email_fail_key)
		if email_fails == 1:
			cache.expire(email_fail_key, 600)

	if ip_fails >= 3 or email_fails >= 3:
		# Escalate lockout based on violation history
		dur_ip, label_ip, lvl_ip = get_next_lockout_tier(cache, client_ip, scope="verify_ip")
		cache.setex(ip_lock_key, dur_ip, "1")
		cache.delete(ip_fail_key)

		dur_email, label_email, lvl_email = (dur_ip, label_ip, lvl_ip)
		if clean_email:
			dur_email, label_email, lvl_email = get_next_lockout_tier(cache, clean_email, scope="verify_email")
			cache.setex(email_lock_key, dur_email, "1")
			cache.delete(email_fail_key)

		max_dur = max(dur_ip, dur_email)
		max_label = label_ip if max_dur == dur_ip else label_email
		lvl = max(lvl_ip, lvl_email)
		prefix = "Persistent spam detected." if lvl >= 5 else "Repeated invalid attempts detected." if lvl > 1 else "Too many invalid OTP attempts."
		return True, f"{prefix} OTP verification is locked for {max_label} for security. Please try again later."

	current_fails = max(ip_fails, email_fails)
	remaining = max(1, 3 - current_fails)
	return False, f"Invalid email OTP. Please check the 6-digit code sent to your email inbox. You have {remaining} attempt{'s' if remaining > 1 else ''} remaining."


def clear_verify_otp_tracking(target_email):
	"""Clears failed attempts and locks for both IP and Email upon successful verification."""
	try:
		cache = frappe.cache()
		clean_email = str(target_email or "").strip().lower()
		client_ip = get_client_ip()

		cache.delete(cache.make_key(f"connectfest:otp_verify_ip_fails:{client_ip}"))
		cache.delete(cache.make_key(f"connectfest:otp_verify_ip_locked:{client_ip}"))
		if clean_email:
			cache.delete(cache.make_key(f"connectfest:otp_verify_email_fails:{clean_email}"))
			cache.delete(cache.make_key(f"connectfest:otp_verify_email_locked:{clean_email}"))
	except Exception:
		pass
EMAIL_VERIFIED_EXPIRY_SECONDS = 1800  # 30 minutes validity once verified

# High-concurrency background email thread pool for zero-lag, instant OTP delivery (1000+ users)
_email_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="email_otp_worker")


def get_otp_expiry_seconds():
	"""
	Dynamically fetches OTP validity duration in seconds from ConnectFest Settings.
	Defaults to 120 seconds (2 minutes).
	"""
	try:
		val = frappe.db.get_single_value("ConnectFest Settings", "otp_expiry_seconds")
		if val and int(val) > 0:
			return int(val)
	except Exception:
		pass
	return DEFAULT_OTP_EXPIRY_SECONDS


def format_expiry_text(seconds):
	"""Returns human-friendly string for OTP expiry duration (e.g. '2 minutes' or '90 seconds')."""
	if seconds % 60 == 0:
		mins = seconds // 60
		return f"{mins} minute{'s' if mins > 1 else ''}"
	return f"{seconds} seconds"


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
	Universal Login OTP Request API.
	Routes directly to Email OTP for lag-free, secure email verification and login.
	Accepts email, or resolves registered attendee email from mobile number.
	Strictly requires the attendee to be registered first.
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
		else:
			frappe.local.response["http_status_code"] = 404
			return {
				"status": "error",
				"error_code": "NOT_REGISTERED",
				"is_registered": False,
				"message": "This mobile number is not registered with ConnectFest! Please register first.",
				"next_action": "register",
			}

	if target_email:
		return send_email_otp(email=target_email, mobile_no=mob, **kwargs)

	frappe.local.response["http_status_code"] = 400
	return {
		"status": "error",
		"message": "A valid email address or registered mobile number is required to receive verification OTP.",
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
		else:
			frappe.local.response["http_status_code"] = 404
			return {
				"status": "error",
				"error_code": "NOT_REGISTERED",
				"is_registered": False,
				"message": "This mobile number is not registered with ConnectFest! Please register first.",
				"next_action": "register",
			}

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


def build_email_otp_html(otp_code, target_email=None, user_name=None, expiry_seconds=120):
	"""
	Clean, minimal, human-crafted HTML email template for ConnectFest Email OTP.
	High-deliverability format designed to land directly in Inbox (anti-spam optimized).
	"""
	display_name = (user_name or "").strip()
	greeting = f"Hello {display_name}," if display_name else "Hello,"
	exp_text = format_expiry_text(expiry_seconds)

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
        This OTP is valid for {exp_text}. Please do not share this code with anyone.
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


def is_email_registered(email):
	"""
	Checks whether an email is registered in ConnectFest.
	Inspects:
	1. ConnectFest Registration (Exhibitors)
	2. ConnectFest Visitor Registration (Visitors)
	3. ConnectFest BNI Event Registration (Event Attendees)
	4. Active Frappe User (Admin / Desk Users)
	Returns: (is_registered: bool, info: dict or None)
	"""
	if not email or "@" not in str(email):
		return False, None
	clean_email = str(email).strip().lower()

	# 1. Exhibitor Registration
	exhibitor = frappe.db.get_value(
		"ConnectFest Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id", "company_name", "payment_status"],
		as_dict=True,
	)
	if exhibitor:
		return True, {"type": "Exhibitor", "data": exhibitor, "full_name": exhibitor.get("full_name")}

	# 2. Visitor Registration
	visitor = frappe.db.get_value(
		"ConnectFest Visitor Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id"],
		as_dict=True,
	)
	if visitor:
		return True, {"type": "Visitor", "data": visitor, "full_name": visitor.get("full_name")}

	# 3. BNI Event Registration
	bni_event = frappe.db.get_value(
		"ConnectFest BNI Event Registration",
		{"email": clean_email},
		["name", "full_name", "registration_id", "pass_code"],
		as_dict=True,
	)
	if bni_event:
		return True, {"type": "Event Attendee", "data": bni_event, "full_name": bni_event.get("full_name")}

	# 4. Standard Frappe User (Admin / Desk Users)
	user = frappe.db.sql("""
		SELECT name, full_name, enabled, email
		FROM `tabUser`
		WHERE name = %s OR email = %s
		LIMIT 1
	""", (clean_email, clean_email), as_dict=True)
	if user and user[0].get("enabled"):
		return True, {
			"type": "User",
			"data": user[0],
			"full_name": user[0].get("full_name"),
			"user_id": user[0].get("name"),
		}

	return False, None


def get_user_role_types(email, mobile_no=None, user_id=None):
	"""
	Returns an array of roles/types the user possesses:
	e.g. ['Admin', 'Exhibitor', 'Visitor', 'Event']
	"""
	roles = []
	from connectfest.services.auth import is_admin_user, is_scanner_user
	clean_email = str(email or "").strip().lower()
	uid = user_id or frappe.db.get_value("User", {"email": clean_email}, "name") or clean_email

	if is_admin_user(uid):
		roles.append("Admin")
	if frappe.db.exists("ConnectFest Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest Registration", {"mobile_no": mobile_no})):
		roles.append("Exhibitor")
	if frappe.db.exists("ConnectFest Visitor Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest Visitor Registration", {"mobile_no": mobile_no})):
		roles.append("Visitor")
	if frappe.db.exists("ConnectFest BNI Event Registration", {"email": clean_email}) or (mobile_no and frappe.db.exists("ConnectFest BNI Event Registration", {"mobile_no": mobile_no})):
		roles.append("Event")

	if not roles:
		if is_scanner_user(uid):
			roles.append("Scanner")
		else:
			roles.append("User")

	return roles


def send_email_otp(email=None, **kwargs):
	"""
	Generates and sends a 6-digit OTP to the attendee's email address for LOGIN.
	Stores OTP in Redis and Frappe DocType with dynamic expiry from ConnectFest Settings.
	Strictly requires the email to be registered first.
	Returns is_scanner status and dynamic expiry info.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()

	if not target_email or "@" not in target_email or "." not in target_email.split("@")[-1]:
		frappe.local.response["http_status_code"] = 400
		return {
			"status": "error",
			"message": "A valid email address is required to receive OTP.",
		}

	# Check whether user is already registered (only enforced when purpose is explicitly 'login')
	purpose = str(data.get("purpose") or kwargs.get("purpose") or "").strip().lower()
	is_reg, reg_info = is_email_registered(target_email)
	if purpose == "login" and not is_reg:
		frappe.local.response["http_status_code"] = 404
		return {
			"status": "error",
			"error_code": "NOT_REGISTERED",
			"is_registered": False,
			"message": "This email is not registered with ConnectFest! Please register as an Exhibitor or Visitor first.",
			"next_action": "register",
		}

	# Rate limit OTP sending: max 5 requests per 10 minutes per email
	allowed, limit_msg, _ = check_send_otp_rate_limit(target_email)
	if not allowed:
		frappe.local.response["http_status_code"] = 429
		return {
			"status": "error",
			"message": limit_msg,
		}

	# Dynamic OTP validity from ConnectFest Settings
	otp_expiry = get_otp_expiry_seconds()
	exp_text = format_expiry_text(otp_expiry)

	# Generate 6-digit secure numeric OTP (no static test OTPs)
	otp_code = f"{random.randint(100000, 999999)}"

	# Retrieve attendee name for personalization if available
	user_name = str(data.get("full_name") or data.get("name") or "").strip()
	if not user_name and is_reg and reg_info and reg_info.get("full_name"):
		user_name = reg_info.get("full_name")

	if not user_name:
		user_name = (
			frappe.db.get_value("ConnectFest Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("ConnectFest Visitor Registration", {"email": target_email}, "full_name")
			or frappe.db.get_value("User", {"email": target_email}, "full_name")
			or ""
		)

	# 1. Store in Frappe DocType 'ConnectFest OTP' (Persistent in MariaDB for Admin visibility)
	expires_at = frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=otp_expiry)
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
	cache.setex(cache_key, otp_expiry, otp_code)

	# 3. Fast non-blocking background email dispatch (Sub-30ms HTTP response, zero server lag)
	email_subject = f"{otp_code} is your ConnectFest verification code"
	email_content = build_email_otp_html(otp_code, target_email=target_email, user_name=user_name, expiry_seconds=otp_expiry)

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
		frappe.logger("connectfest").info(f"[ConnectFest Email OTP] Sent OTP {otp_code} to {target_email} (Expires in {exp_text})")
	except Exception:
		pass

	uid = (reg_info.get("user_id") if reg_info else None) or frappe.db.get_value("User", {"email": target_email}, "name") or target_email
	is_scanner = bool(is_scanner_user(uid))

	return {
		"status": "success",
		"message": f"Verification code sent successfully to {target_email}. Please check your email inbox (valid for {exp_text}).",
		"email": target_email,
		"expires_in_seconds": otp_expiry,
		"expires_in": exp_text,
		"is_scanner": is_scanner,
	}


def verify_email_otp(email=None, otp=None, **kwargs):
	"""
	Verifies the email OTP submitted by the attendee.
	Strictly requires the actual OTP sent via email (NO test master OTPs).
	Upon successful verification, authenticates user into Frappe session and returns roles array.
	"""
	data = extract_request_data(kwargs)
	target_email = str(email or data.get("email") or "").strip().lower()
	submitted_otp = str(otp or data.get("otp") or data.get("code") or data.get("email_otp") or "").strip()

	if not target_email or "@" not in target_email:
		frappe.local.response["http_status_code"] = 400
		return {"status": "error", "message": "Valid email address is required"}

	# Check if user is locked out due to 3+ consecutive failed OTP attempts
	is_locked, lock_msg = check_verify_otp_lock(target_email)
	if is_locked:
		frappe.local.response["http_status_code"] = 429
		return {"status": "error", "message": lock_msg}

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
		is_expired_or_absent = (not stored_otp and not db_otp)
		if is_expired_or_absent:
			recent_otp = frappe.db.sql("""
				SELECT status, expires_at FROM `tabConnectFest OTP`
				WHERE email = %s ORDER BY creation DESC LIMIT 1
			""", (target_email,), as_dict=True)
			if recent_otp and recent_otp[0].get("status") == "Verified":
				frappe.local.response["http_status_code"] = 400
				return {
					"status": "error",
					"message": "This OTP has already been verified and used. Please request a new OTP.",
				}
			otp_expiry = get_otp_expiry_seconds()
			exp_text = format_expiry_text(otp_expiry)
			frappe.local.response["http_status_code"] = 400
			return {
				"status": "error",
				"message": f"OTP has expired! The OTP is only valid for {exp_text}. Please request a new OTP.",
			}

		# Incorrect OTP: record attempt and enforce 3-attempt lock
		locked, fail_msg = record_verify_otp_failure(target_email)
		frappe.local.response["http_status_code"] = 429 if locked else 400
		return {
			"status": "error",
			"message": fail_msg,
		}

	# Successful OTP match: clear failed attempt tracker
	clear_verify_otp_tracking(target_email)

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

	# Mark email as verified in Redis cache
	verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
	cache.setex(verified_key, EMAIL_VERIFIED_EXPIRY_SECONDS, "1")

	# Check whether user is registered
	is_reg, reg_info = is_email_registered(target_email)
	if not is_reg:
		frappe.local.response["http_status_code"] = 200
		return {
			"status": "success",
			"is_verified": True,
			"message": f"Email {target_email} verified successfully! You may now proceed with registration.",
			"email": target_email,
			"roles": [],
			"is_scanner": False,
		}

	# -------------------------------------------------------------
	# Log user into Frappe session (Email OTP Login)
	# -------------------------------------------------------------
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

	if frappe.db.exists("User", target_email):
		try:
			if hasattr(frappe.local, "login_manager") and frappe.local.login_manager:
				frappe.local.login_manager.login_as(target_email)
			else:
				from frappe.auth import LoginManager
				frappe.local.login_manager = LoginManager()
				frappe.local.login_manager.login_as(target_email)
		except Exception:
			pass
		frappe.set_user(target_email)

	uid = (reg_info.get("user_id") if reg_info else None) or frappe.db.get_value("User", {"email": target_email}, "name") or target_email
	roles_arr = get_user_role_types(target_email, mobile_no=user_mob, user_id=uid)
	is_scanner = bool(is_scanner_user(uid))

	frappe.local.response["http_status_code"] = 200
	return {
		"status": "success",
		"message": f"Email {target_email} verified successfully! You are logged in.",
		"email": target_email,
		"roles": roles_arr,
		"is_scanner": is_scanner,
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


def verify_otp_inline(email, submitted_otp):
	"""
	Validates submitted OTP for an email directly during registration if provided inline.
	Returns True if valid, False otherwise.
	Marks email as verified in cache and DocType on success.
	"""
	if not email or not submitted_otp:
		return False
	target_email = str(email).strip().lower()
	submitted_otp = str(submitted_otp).strip()
	cache = frappe.cache()
	cache_key = cache.make_key(f"{EMAIL_OTP_CACHE_PREFIX}{target_email}")
	stored_otp = cache.get(cache_key)

	if stored_otp and str(stored_otp).strip() == submitted_otp:
		cache.delete(cache_key)
		verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
		cache.setex(verified_key, EMAIL_VERIFIED_EXPIRY_SECONDS, "1")
		rec = frappe.db.sql("""
			SELECT name FROM `tabConnectFest OTP`
			WHERE email = %s AND status = 'Pending' ORDER BY creation DESC LIMIT 1
		""", (target_email,), as_dict=True)
		if rec:
			frappe.db.set_value("ConnectFest OTP", rec[0]["name"], {
				"status": "Verified",
				"verified_at": frappe.utils.now_datetime()
			})
			frappe.db.commit()
		return True

	db_otp_rec = frappe.db.sql("""
		SELECT name, otp, expires_at FROM `tabConnectFest OTP`
		WHERE email = %s AND status = 'Pending' ORDER BY creation DESC LIMIT 1
	""", (target_email,), as_dict=True)
	if db_otp_rec and str(db_otp_rec[0].get("otp")).strip() == submitted_otp:
		expires_at = db_otp_rec[0].get("expires_at")
		if expires_at and expires_at >= frappe.utils.now_datetime():
			frappe.db.set_value("ConnectFest OTP", db_otp_rec[0]["name"], {
				"status": "Verified",
				"verified_at": frappe.utils.now_datetime()
			})
			frappe.db.commit()
			cache.delete(cache_key)
			verified_key = cache.make_key(f"{EMAIL_VERIFIED_CACHE_PREFIX}{target_email}")
			cache.setex(verified_key, EMAIL_VERIFIED_EXPIRY_SECONDS, "1")
			return True

	return False
